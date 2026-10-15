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
