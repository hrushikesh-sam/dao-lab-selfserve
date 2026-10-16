"""B-22: the runner interface, the mock backend, and the DAOLAB_RUNNER switch (plan/09 §6, §9, §13)."""
import dataclasses
import json
import time
import urllib.request

import pytest

from service import config, models, runner
from service.config import Settings
from service.models import JobError
from service.runner import (CLEANUP_STEPS, DEPLOY_STEPS, PROBE_STEPS, VERIFY_CLEAN_STEPS,
                            AnsibleRunner, MockRunner, RunnerError, RunResult)
from tests.conftest import T, mk_res, mk_setup, mk_usecase

RES = {"id": 7}


class Sink:
    """Records every step call, in order."""
    def __init__(self):
        self.calls = []

    def __call__(self, name, state="ok", detail=None):
        self.calls.append((name, state, detail))

    @property
    def names(self):
        return [c[0] for c in self.calls]


def mock(step_sec=0.0, fail=(), sleeps=None):
    return MockRunner(step_sec=step_sec, fail=frozenset(fail),
                      sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# ---- configuration -----------------------------------------------------------

def test_mock_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("DAOLAB_MOCK_STEP_SEC", raising=False)
    monkeypatch.delenv("DAOLAB_MOCK_FAIL", raising=False)
    s = Settings.from_env()
    assert (s.mock_step_sec, s.mock_fail) == (5.0, "")
    monkeypatch.setenv("DAOLAB_MOCK_STEP_SEC", "0.25")
    monkeypatch.setenv("DAOLAB_MOCK_FAIL", "deploy,cleanup")
    s = Settings.from_env()
    assert (s.mock_step_sec, s.mock_fail) == (0.25, "deploy,cleanup")


def test_env_example_lists_the_new_variables():
    text = open(config.__file__.replace("service/config.py", ".env.example")).read()
    assert "DAOLAB_MOCK_STEP_SEC=" in text and "DAOLAB_MOCK_FAIL=" in text


@pytest.mark.parametrize("text,expected", [
    ("", frozenset()), ("  ", frozenset()), ("deploy", {"deploy"}), ("cleanup", {"cleanup"}),
    ("deploy,cleanup", {"deploy", "cleanup"}), (" deploy , cleanup ,", {"deploy", "cleanup"}),
    (None, frozenset())])
def test_parse_mock_fail(text, expected):
    assert runner.parse_mock_fail(text) == frozenset(expected)


@pytest.mark.parametrize("bad", ["reboot", "deploy,reboot", "probe", "Deploy"])
def test_parse_mock_fail_rejects_unknown_names(bad):
    with pytest.raises(RunnerError, match="DAOLAB_MOCK_FAIL"):
        runner.parse_mock_fail(bad)


def _settings(**over):
    return dataclasses.replace(config.settings, **over)


def test_get_runner_picks_the_backend():
    m = runner.get_runner(_settings(runner="mock", mock_step_sec=0.5, mock_fail="cleanup"))
    assert isinstance(m, MockRunner) and m.step_sec == 0.5 and m.fail == {"cleanup"}
    assert isinstance(runner.get_runner(_settings(runner="ansible")), AnsibleRunner)
    with pytest.raises(RunnerError, match="unknown DAOLAB_RUNNER 'docker'"):
        runner.get_runner(_settings(runner="docker"))
    with pytest.raises(RunnerError, match="DAOLAB_MOCK_FAIL"):
        runner.get_runner(_settings(runner="mock", mock_fail="nonsense"))


def test_get_runner_defaults_to_the_live_settings(monkeypatch):
    monkeypatch.setattr(config, "settings", _settings(runner="mock", mock_step_sec=0.0))
    assert isinstance(runner.get_runner(), MockRunner)


def test_negative_step_time_is_refused():
    with pytest.raises(RunnerError):
        MockRunner(step_sec=-1)


@pytest.mark.parametrize("cls", [MockRunner, AnsibleRunner])
def test_both_backends_have_the_same_four_functions(cls):
    for fn in ("deploy", "cleanup", "probe", "verify_clean"):
        assert callable(getattr(cls, fn))


# ---- the mock: the happy path ---------------------------------------------------

@pytest.mark.parametrize("fn,steps", [("deploy", DEPLOY_STEPS), ("cleanup", CLEANUP_STEPS),
                                      ("probe", PROBE_STEPS),
                                      ("verify_clean", VERIFY_CLEAN_STEPS)])
def test_each_function_walks_its_steps_in_order(fn, steps):
    sink = Sink()
    result = getattr(mock(), fn)(RES, sink)
    assert result == RunResult(ok=True)
    assert sink.calls == [(n, "ok", None) for n in steps]


def test_cleanup_terminates_the_dpu_lab_session_first():
    """B-53: the first cleanup step revokes DPU access (`loginctl terminate-user lab`,
    plan/09 §7), so access cannot outlive the slot even if teardown later fails."""
    sink = Sink()
    mock().cleanup(RES, sink)
    assert sink.calls[0][0] == "terminate-user"


def test_it_sleeps_before_every_step_and_the_step_is_visible_as_soon_as_it_finishes():
    events = []
    m = MockRunner(step_sec=2.5, sleep=lambda s: events.append(("sleep", s)))
    m.deploy(RES, lambda name, state="ok", detail=None: events.append(("step", name)))
    expected = []
    for n in DEPLOY_STEPS:
        expected += [("sleep", 2.5), ("step", n)]
    assert events == expected                                       # sleep, step, sleep, step, ...


def test_the_default_deploy_takes_about_half_a_minute(monkeypatch):
    monkeypatch.delenv("DAOLAB_MOCK_STEP_SEC", raising=False)
    assert len(DEPLOY_STEPS) * Settings.from_env().mock_step_sec == 30   # plan/10 P2: "about 30 s"


def test_real_sleeping_actually_takes_time():
    started = time.monotonic()
    MockRunner(step_sec=0.02).probe(RES, Sink())
    assert time.monotonic() - started >= 0.02 * len(PROBE_STEPS) * 0.9


def test_accepts_a_sqlite_row_as_well_as_a_dict(db):
    sid = mk_setup(db, "s1", ["wire"])
    rid = mk_res(db, sid, mk_usecase(db, "uc1", ["wire"]), T(10), T(12))
    row = models.reservation_by_id(db, rid)
    assert mock().deploy(row, Sink()).ok and mock().verify_clean(row, Sink()).ok


# ---- the mock: failure injection ------------------------------------------------

def test_mock_fail_deploy_breaks_deploy_at_create_vms():
    sink = Sink()
    result = mock(fail={"deploy"}).deploy(RES, sink)
    assert result.ok is False and "create_vms" in result.error and "DAOLAB_MOCK_FAIL=deploy" in result.error
    assert sink.names == ["lease_ips", "dpu_prepare", "create_vms"]       # nothing after the failure
    assert [c[1] for c in sink.calls] == ["ok", "ok", "failed"]
    assert "DAOLAB_MOCK_FAIL=deploy" in sink.calls[-1][2]


def test_mock_fail_deploy_leaves_every_other_function_alone():
    m = mock(fail={"deploy"})
    assert m.cleanup(RES, Sink()).ok and m.probe(RES, Sink()).ok
    assert m.verify_clean(RES, Sink()).ok and m.verify_clean(RES, Sink()).residue == ()


def test_mock_fail_cleanup_looks_fine_but_leaves_residue():
    m = mock(fail={"cleanup"})
    assert m.cleanup(RES, Sink()) == RunResult(ok=True)                 # the cleanup itself "worked"
    sink = Sink()
    result = m.verify_clean(RES, sink)
    assert result.ok is False and result.error == "residue found after cleanup"
    assert result.residue == ("vm:daolab-r7-backend", "dpu_session:lab")
    assert sink.names == list(VERIFY_CLEAN_STEPS)                       # every check still runs
    assert [c[1] for c in sink.calls] == ["failed", "ok", "failed", "ok"]
    assert sink.calls[0][2] == "vm:daolab-r7-backend" and sink.calls[2][2] == "dpu_session:lab"


def test_residue_is_reported_every_time_so_reconcile_never_clears_it():
    m = mock(fail={"cleanup"})
    assert [m.verify_clean(RES, Sink()).ok for _ in range(3)] == [False, False, False]


def test_residue_names_the_reservation():
    assert "daolab-r42-backend" in " ".join(
        mock(fail={"cleanup"}).verify_clean({"id": 42}, Sink()).residue)


def test_mock_fail_cleanup_leaves_deploy_alone():
    assert mock(fail={"cleanup"}).deploy(RES, Sink()).ok


def test_both_failures_at_once():
    m = mock(fail={"deploy", "cleanup"})
    assert not m.deploy(RES, Sink()).ok and not m.verify_clean(RES, Sink()).ok


def test_failure_injection_still_sleeps_before_the_failing_step():
    sleeps = []
    mock(step_sec=1.0, fail={"deploy"}, sleeps=sleeps).deploy(RES, Sink())
    assert sleeps == [1.0, 1.0, 1.0]                                    # 2 ok steps + the failing one


# ---- recording steps on a real job -------------------------------------------------

@pytest.fixture
def job(db):
    sid = mk_setup(db, "s1", ["wire"])
    rid = mk_res(db, sid, mk_usecase(db, "uc1", ["wire"]), T(10), T(12))
    jid = models.create_job(db, rid, "deploy")
    models.transition_job(db, jid, "running")
    return db, rid, jid


def test_steps_land_on_the_job_as_they_happen(job):
    db, rid, jid = job
    seen_while_running = []

    def spy_sleep(_):
        seen_while_running.append(len(models.job_steps(models.job_by_id(db, jid))))
    m = MockRunner(step_sec=1.0, sleep=spy_sleep)
    result = m.deploy({"id": rid}, runner.job_sink(db, jid))
    assert result.ok
    assert seen_while_running == list(range(len(DEPLOY_STEPS)))        # one more step visible each time
    steps = models.job_steps(models.job_by_id(db, jid))
    assert [s["name"] for s in steps] == list(DEPLOY_STEPS)
    assert all(s["state"] == "ok" and s["at"] for s in steps)


def test_an_injected_failure_is_recorded_on_the_job(job):
    db, rid, jid = job
    result = mock(fail={"deploy"}).deploy({"id": rid}, runner.job_sink(db, jid))
    steps = models.job_steps(models.job_by_id(db, jid))
    assert [s["name"] for s in steps] == ["lease_ips", "dpu_prepare", "create_vms"]
    assert steps[-1]["state"] == "failed" and "mock failure" in steps[-1]["detail"]
    models.transition_job(db, jid, "failed", error=result.error)
    assert "create_vms" in models.job_by_id(db, jid)["error"]


def test_the_sink_refuses_steps_for_a_job_that_is_not_running(db):
    sid = mk_setup(db, "s1", ["wire"])
    rid = mk_res(db, sid, mk_usecase(db, "uc1", ["wire"]), T(10), T(12))
    pending = models.create_job(db, rid, "deploy")
    with pytest.raises(JobError, match="while pending"):
        mock().deploy({"id": rid}, runner.job_sink(db, pending))


def test_verify_clean_residue_is_recorded_on_a_cleanup_job(db):
    sid = mk_setup(db, "s1", ["wire"])
    rid = mk_res(db, sid, mk_usecase(db, "uc1", ["wire"]), T(10), T(12))
    jid = models.create_job(db, rid, "cleanup")
    models.transition_job(db, jid, "running")
    m = mock(fail={"cleanup"})
    sink = runner.job_sink(db, jid)
    assert m.cleanup({"id": rid}, sink).ok
    result = m.verify_clean({"id": rid}, sink)
    assert not result.ok
    names = [s["name"] for s in models.job_steps(models.job_by_id(db, jid))]
    assert names == list(CLEANUP_STEPS) + list(VERIFY_CLEAN_STEPS)


# ---- dispatch + the not-yet-built backend ------------------------------------------

def test_method_for_maps_job_kinds_to_runner_functions():
    m = mock()
    assert runner.method_for(m, "deploy") == m.deploy
    assert runner.method_for(m, "cleanup") == m.cleanup
    assert runner.method_for(m, "health_probe") == m.probe
    assert runner.method_for(m, "reconcile") == m.reconcile           # B-72


def test_method_for_rejects_an_unknown_kind():
    with pytest.raises(RunnerError, match="unknown job kind"):
        runner.method_for(mock(), "reboot")


def test_every_job_kind_in_the_model_is_handled():
    for kind in models.JOB_KINDS:
        assert callable(runner.method_for(mock(), kind))


# ---- B-72: reconcile ---------------------------------------------------------------

def test_mock_reconcile_walks_its_steps():
    sink = Sink()
    assert mock().reconcile(RES, sink) == RunResult(ok=True)
    assert sink.names == list(runner.RECONCILE_STEPS)


def test_mock_reconcile_cannot_clear_stubborn_residue():
    m = mock(fail={"cleanup"})
    assert m.reconcile(RES, Sink()).ok
    assert not m.verify_clean(RES, Sink()).ok                          # still there: quarantine


def test_mock_reconcile_stops_the_fake_exporter():
    stopped = []

    class Exp:
        def __init__(self, rid): self.rid = rid
        def start(self): return self
        def stop(self): stopped.append(self.rid)

    m = MockRunner(exporter_factory=Exp)
    m.deploy(RES, Sink())
    m.reconcile(RES, Sink())
    assert stopped == [RES["id"]]


# ---- B-68: mock telemetry (a fake exporter per reservation) ------------------------

def test_parse_mock_fail_accepts_health():
    assert runner.parse_mock_fail("health") == frozenset({"health"})
    assert runner.parse_mock_fail("deploy,health") == frozenset({"deploy", "health"})
    assert "health" in runner.MOCK_FAIL_TARGETS


def test_mock_exporter_serves_metrics_and_registers_a_file_sd_target(tmp_path):
    exp = runner.MockExporter(7, labels={"daolab_user": "alice"}, targets_dir=str(tmp_path))
    exp.start()
    try:
        body = urllib.request.urlopen(f"http://{exp.target}/metrics", timeout=5).read().decode()
        assert 'daolab_reservation="7"' in body and 'daolab_user="alice"' in body
        target_file = tmp_path / "res-7.json"
        data = json.loads(target_file.read_text())
        assert data["targets"] == [exp.target]
        assert data["labels"] == {"daolab_reservation": "7", "daolab_user": "alice"}
    finally:
        exp.stop()
    assert not (tmp_path / "res-7.json").exists()          # stopping drops the target


def test_the_mock_exporter_serves_every_series_the_usecase_rules_read(tmp_path):
    """B-68 follow-up: a mock deploy has to read `healthy`, so every metric a shipped health
    rule queries (except `up`, which Prometheus generates) is served, with the rule's labels."""
    import os
    import re
    from service import catalog
    ucs, errors = catalog.load_usecases(os.path.join(os.path.dirname(__file__), "..", "usecases"))
    assert errors == []
    wanted = set()
    for uc in ucs:
        for rule in uc["health"]:
            for metric, labels in re.findall(r"([a-z_]+)\{([^}]*)\}", rule["promql"]):
                if metric != "up":
                    fixed = dict(re.findall(r'([a-z_]+)="([^"$]+)"', labels))
                    wanted.add((metric, tuple(sorted(fixed.items()))))
    exp = runner.MockExporter(7, targets_dir=str(tmp_path))
    exp.start()
    try:
        body = urllib.request.urlopen(f"http://{exp.target}/metrics", timeout=5).read().decode()
    finally:
        exp.stop()
    for metric, fixed in wanted:
        line = next((l for l in body.splitlines() if l.startswith(metric + "{")), None)
        assert line is not None, f"mock exporter does not serve {metric}"
        assert all(f'{k}="{v}"' in line for k, v in fixed), (metric, line)
        assert float(line.rsplit(" ", 1)[1]) >= 0


def test_deploy_starts_the_exporter_and_cleanup_stops_it(tmp_path):
    started = []

    def factory(rid):
        started.append(rid)
        return runner.MockExporter(rid, targets_dir=str(tmp_path))

    m = MockRunner(step_sec=0.0, exporter_factory=factory)
    assert m.deploy({"id": 9}, Sink()).ok
    assert started == [9] and (tmp_path / "res-9.json").exists()
    assert m.cleanup({"id": 9}, Sink()).ok
    assert not (tmp_path / "res-9.json").exists()


def test_mock_fail_health_never_starts_the_exporter(tmp_path):
    started = []

    def factory(rid):
        started.append(rid)
        return runner.MockExporter(rid, targets_dir=str(tmp_path))

    m = MockRunner(step_sec=0.0, fail=frozenset({"health"}), exporter_factory=factory)
    assert m.deploy({"id": 9}, Sink()).ok                  # the deploy itself still "works"
    assert started == [] and not (tmp_path / "res-9.json").exists()


def test_a_failed_deploy_does_not_leave_a_live_exporter(tmp_path):
    started = []

    def factory(rid):
        started.append(rid)
        return runner.MockExporter(rid, targets_dir=str(tmp_path))

    m = MockRunner(step_sec=0.0, fail=frozenset({"deploy"}), exporter_factory=factory)
    assert not m.deploy({"id": 9}, Sink()).ok
    assert started == [] and not (tmp_path / "res-9.json").exists()


def test_get_runner_only_wires_exporters_when_otel_is_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "OTEL_TARGETS_DIR", str(tmp_path))
    off = runner.get_runner(_settings(runner="mock", otel_endpoint=""))
    assert off._exporter_factory is None
    off.deploy({"id": 1}, Sink())
    assert not (tmp_path / "res-1.json").exists()
    on = runner.get_runner(_settings(runner="mock", otel_endpoint="localhost:4317"))
    assert on._exporter_factory is not None
    exp = on._exporter_factory(2)                           # a real exporter, when enabled
    exp.start()
    try:
        assert (tmp_path / "res-2.json").exists()
    finally:
        exp.stop()



# ---- B-70: structured residue ----------------------------------------------------------

def test_residue_items_are_kind_colon_ident():
    assert runner.residue_item("vm", "daolab-r1-a") == "vm:daolab-r1-a"
    assert runner.parse_residue("vf:0000:5e:01.0") == ("vf", "0000:5e:01.0")   # BDF keeps its colons
    assert runner.parse_residue("ip:10.30.0.101") == ("ip", "10.30.0.101")
    assert runner.parse_residue("vf 0000:03:00.2 still bound") == ("other", "vf 0000:03:00.2 still bound")
    with pytest.raises(ValueError):
        runner.residue_item("disk", "x")


def test_mock_residue_comes_from_the_context_vms_and_leases():
    ctx = {"id": 9, "vms": [{"role": "vpp", "name": None, "ip": "10.0.0.1"},
                            {"role": "sink", "name": "daolab-r9-sink", "ip": "10.30.0.101"}],
           "leases": ["10.30.0.101"]}
    sink = Sink()
    result = mock(fail={"cleanup"}).verify_clean(ctx, sink)
    assert result.residue == ("vm:daolab-r9-sink", "ip:10.30.0.101")
    states = dict((c[0], c[1]) for c in sink.calls)
    assert states == {"check_vms": "failed", "check_vfs": "ok", "check_sessions": "ok",
                      "check_ips": "failed"}


# ---- B-63: the Ansible OpenTelemetry callback ----------------------------------------------

def test_ansible_env_without_telemetry_only_points_at_the_config():
    env = runner.ansible_env(_settings(otel_endpoint="", playbooks_dir="/opt/daolab/playbooks"))
    assert env["ANSIBLE_CONFIG"] == "/opt/daolab/playbooks/ansible.cfg"
    assert not [k for k in env if k.startswith(("OTEL_", "DAOLAB_ANSIBLE_OTEL", "TRACEPARENT"))]


def test_ansible_env_with_telemetry_turns_the_callback_on_under_the_job_span():
    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    env = runner.ansible_env(_settings(otel_endpoint="collector:4317"), traceparent=tp)
    assert env["DAOLAB_ANSIBLE_OTEL"] == "true"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4317"
    assert env["OTEL_EXPORTER_OTLP_INSECURE"] == "true"
    assert env["OTEL_SERVICE_NAME"] == "daolab-ansible" and env["TRACEPARENT"] == tp


def test_ansible_env_keeps_tls_endpoints_and_skips_an_absent_traceparent():
    env = runner.ansible_env(_settings(otel_endpoint="https://otel.lab:443"))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.lab:443"
    assert env["OTEL_EXPORTER_OTLP_INSECURE"] == "false" and "TRACEPARENT" not in env


def test_the_ansible_cfg_enables_the_callback_from_the_environment():
    import configparser
    import os
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(os.path.dirname(__file__), "..", "playbooks", "ansible.cfg"))
    assert cfg["defaults"]["callbacks_enabled"] == "community.general.opentelemetry"
    assert cfg["callback_opentelemetry"]["enable_from_environment"] == "DAOLAB_ANSIBLE_OTEL"
