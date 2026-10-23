"""OpenTelemetry wiring (B-61).

`setup_tracing()` configures the OTel SDK and optionally instruments a FastAPI app.
It is a **complete no-op** while `DAOLAB_OTEL_ENDPOINT` is unset, so the API and the
tests run with zero tracing cost and without the OTel packages being exercised.

The OTel packages are imported lazily inside `setup_tracing`, so importing this module
(and running the no-op path) never requires them to be installed.
"""
import logging
from contextlib import contextmanager

log = logging.getLogger("daolab.otel")

_configured = False


def _grpc_target(endpoint: str) -> tuple[str, bool]:
    """Split a DAOLAB_OTEL_ENDPOINT into (host:port, insecure). `https://` (or a
    `:443` port) means TLS; everything else is plaintext gRPC, matching the local
    collector stack in deploy/otel/."""
    insecure = True
    target = endpoint
    if target.startswith("https://"):
        target, insecure = target[len("https://"):], False
    elif target.startswith("http://"):
        target = target[len("http://"):]
    return target.rstrip("/"), insecure


def setup_tracing(app=None, service_name: str = "daolab") -> bool:
    """Set up the OTel SDK, and instrument `app` if given. Returns True when tracing
    is active, False when disabled (endpoint unset). Idempotent."""
    global _configured
    from . import config

    endpoint = (config.settings.otel_endpoint or "").strip()
    if not endpoint:
        log.debug("otel: DAOLAB_OTEL_ENDPOINT unset — tracing disabled")
        return False
    if _configured and app is None:
        return True

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    target, insecure = _grpc_target(endpoint)
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=target, insecure=insecure)))
        trace.set_tracer_provider(provider)
        _configured = True
        log.info("otel: tracing enabled -> %s (service=%s)", target, service_name)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore[import-not-found]
        FastAPIInstrumentor.instrument_app(app)
    return True


# ---- one trace per job (B-62) -------------------------------------------------------------
#
# The engine opens a span per job, stores its trace id on the job row (so GET /jobs/{id} links
# to Jaeger) and records every runner step as a child span. A retry of a deploy carries the
# first attempt's trace id, so all attempts land in one trace. The runner reads `traceparent()`
# while the job span is current and hands it to Ansible as TRACEPARENT, which makes each task
# of the OTel callback (B-63) a child of the job span. With tracing off all of this is a no-op
# and the OTel packages are never imported.

_provider = None            # tests inject an SDK TracerProvider here (use_provider)


def use_provider(provider) -> None:
    """Route job spans to `provider` instead of the global one (tests); None resets."""
    global _provider
    _provider = provider


def enabled() -> bool:
    return _provider is not None or _configured


def _tracer():
    if _provider is not None:
        return _provider.get_tracer("daolab.engine")
    from opentelemetry import trace
    return trace.get_tracer("daolab.engine")


def _now_ns() -> int:
    import time
    return time.time_ns()


class JobSpan:
    """The open span of one job; `trace_id` is the 32-hex-digit id stored on the job row."""

    def __init__(self, span):
        self.span = span
        self.trace_id = format(span.get_span_context().trace_id, "032x")
        self._last_ns = _now_ns()

    def step(self, name: str, state: str = "ok", detail: str | None = None) -> None:
        """A finished runner step as a child span from the previous step's end to now."""
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
        end = _now_ns()
        ctx = trace.set_span_in_context(self.span)
        child = _tracer().start_span(f"step {name}", context=ctx, start_time=self._last_ns,
                                     attributes={"daolab.step": name, "daolab.step.state": state})
        if detail:
            child.set_attribute("daolab.step.detail", detail)
        if state == "failed":
            child.set_status(Status(StatusCode.ERROR, detail or "failed"))
        child.end(end_time=end)
        self._last_ns = end

    def finish(self, ok: bool, error: str | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode
        self.span.set_attribute("daolab.job.ok", ok)
        if not ok:
            self.span.set_status(Status(StatusCode.ERROR, error or "failed"))


@contextmanager
def job_span(job, ctx: dict):
    """Open the job's span as the current span; yields a JobSpan, or None with tracing off.

    A job that already has a trace id (a retry, or a replay after crash recovery) continues
    that trace: its span gets a remote parent carrying the stored id."""
    if not enabled():
        yield None
        return
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
    parent = None
    if job["trace_id"]:
        import random
        remote = SpanContext(trace_id=int(job["trace_id"], 16),
                             span_id=random.getrandbits(64) or 1, is_remote=True,
                             trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent = trace.set_span_in_context(NonRecordingSpan(remote))
    attrs = {"daolab.job": job["id"], "daolab.job.kind": job["kind"],
             "daolab.attempt": job["attempt"], "daolab.reservation": ctx["id"]}
    for key in ("usecase", "setup", "user"):
        if ctx.get(key) is not None:
            attrs[f"daolab.{key}"] = ctx[key]
    with _tracer().start_as_current_span(f"job {job['kind']}", context=parent,
                                         attributes=attrs) as span:
        yield JobSpan(span)


def traceparent() -> str | None:
    """The W3C traceparent of the current span (inside `job_span`), or None."""
    if not enabled():
        return None
    from opentelemetry.propagate import inject
    carrier: dict = {}
    inject(carrier)
    return carrier.get("traceparent")
