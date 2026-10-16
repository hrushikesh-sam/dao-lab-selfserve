"""OpenTelemetry wiring (B-61).

`setup_tracing()` configures the OTel SDK and optionally instruments a FastAPI app.
It is a **complete no-op** while `DAOLAB_OTEL_ENDPOINT` is unset, so the API and the
tests run with zero tracing cost and without the OTel packages being exercised.

The OTel packages are imported lazily inside `setup_tracing`, so importing this module
(and running the no-op path) never requires them to be installed.
"""
import logging

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
