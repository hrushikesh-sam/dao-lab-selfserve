"""B-61 OTel SDK wiring — plan/09 section 8. The SDK is a no-op unless
DAOLAB_OTEL_ENDPOINT is set, so importing the API and running tests never
depend on a collector being reachable."""
import dataclasses

from fastapi import FastAPI

from service import config, otel


def _settings(endpoint):
    return dataclasses.replace(config.settings, otel_endpoint=endpoint)


def test_noop_when_endpoint_unset(monkeypatch):
    monkeypatch.setattr(config, "settings", _settings(""))
    assert otel.setup_tracing() is False


def test_noop_with_app_when_endpoint_unset(monkeypatch):
    monkeypatch.setattr(config, "settings", _settings(""))
    app = FastAPI()
    assert otel.setup_tracing(app, "daolab-test") is False


def test_grpc_target_scheme_parsing():
    assert otel._grpc_target("http://collector:4317") == ("collector:4317", True)
    assert otel._grpc_target("collector:4317/") == ("collector:4317", True)
    assert otel._grpc_target("https://collector:443") == ("collector:443", False)


# ---- B-62: one trace per job --------------------------------------------------------------

import pytest                                                     # noqa: E402
from opentelemetry.sdk.trace import TracerProvider                # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor    # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import StatusCode                        # noqa: E402

from service import engine, models                                # noqa: E402
from service.runner import DEPLOY_STEPS, MockRunner               # noqa: E402
from service.scheduler import SchedConfig                         # noqa: E402
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase   # noqa: E402


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel.use_provider(provider)
    yield exporter
    otel.use_provider(None)


@pytest.fixture
def booking(db):
    sid = mk_setup(db, "tb", ["wire"], dpu_hw_class="c")
    rid = mk_res(db, sid, mk_usecase(db, "uc", ["wire"], hw_class="c"), T(9, 5), T(10),
                 state="provisioning")
    return db, rid


def _tick(db, rn):
    return engine.tick(db, NOW, rn, SchedConfig())


def _by_name(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def test_tracing_off_leaves_the_job_untraced(booking):
    db, rid = booking
    models.create_job(db, rid, "deploy")
    _tick(db, MockRunner())
    assert models.jobs_for_reservation(db, rid)[0]["trace_id"] is None
    assert otel.traceparent() is None


def test_a_job_gets_its_own_trace_with_one_child_span_per_step(booking, spans):
    db, rid = booking
    jid = models.create_job(db, rid, "deploy")
    _tick(db, MockRunner())
    job = models.job_by_id(db, jid)
    [root] = _by_name(spans, "job ")
    assert root.name == "job deploy" and job["trace_id"] == format(root.context.trace_id, "032x")
    assert root.attributes["daolab.reservation"] == rid and root.attributes["daolab.job"] == jid
    assert root.attributes["daolab.usecase"] == "uc" and root.attributes["daolab.setup"] == "tb"
    steps = _by_name(spans, "step ")
    assert [s.name for s in steps] == [f"step {n}" for n in DEPLOY_STEPS]
    assert all(s.parent.span_id == root.context.span_id for s in steps)
    assert all(s.context.trace_id == root.context.trace_id for s in steps)
    assert all(a.end_time <= b.start_time for a, b in zip(steps, steps[1:]))   # a waterfall


def test_a_failed_step_and_job_are_marked_as_errors(booking, spans):
    db, rid = booking
    models.create_job(db, rid, "deploy")
    _tick(db, MockRunner(fail={"deploy"}))
    [root] = _by_name(spans, "job ")
    [bad] = [s for s in _by_name(spans, "step ") if s.attributes["daolab.step.state"] == "failed"]
    assert bad.status.status_code == StatusCode.ERROR and "DAOLAB_MOCK_FAIL" in bad.status.description
    assert root.status.status_code == StatusCode.ERROR and root.attributes["daolab.job.ok"] is False


def test_retries_and_the_automatic_cleanup_stay_in_the_first_trace(booking, spans, monkeypatch):
    import dataclasses
    db, rid = booking
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=2))
    models.create_job(db, rid, "deploy")
    rn = MockRunner(fail={"deploy"})
    _tick(db, rn)                                                  # attempt 1 fails -> retry
    _tick(db, rn)                                                  # attempt 2 fails -> cleanup
    _tick(db, rn)                                                  # cleanup runs
    jobs = models.jobs_for_reservation(db, rid)
    assert [(j["kind"], j["attempt"]) for j in jobs] == [("deploy", 1), ("deploy", 2), ("cleanup", 1)]
    assert len({j["trace_id"] for j in jobs}) == 1 and jobs[0]["trace_id"] is not None
    roots = _by_name(spans, "job ")
    assert len(roots) == 3
    assert {format(s.context.trace_id, "032x") for s in roots} == {jobs[0]["trace_id"]}


def test_the_runner_sees_the_job_span_as_traceparent(booking, spans):
    db, rid = booking
    models.create_job(db, rid, "deploy")
    seen = []

    class Spy(MockRunner):
        def deploy(self, res, step):
            seen.append(otel.traceparent())
            return super().deploy(res, step)

    _tick(db, Spy())
    [root] = _by_name(spans, "job ")
    ctx = root.context
    [tp] = seen
    head, flags = tp.rsplit("-", 1)
    assert head == f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}" and int(flags, 16) & 1  # sampled
