"""B-86: end to end on tb-01, on the mock runner (no hardware) — alice one-click VPP -> ready ->
healthy -> slot end -> released; bob blocked from the DPU throughout.

Everything real except the testbed: the API (TestClient), the engine ticking on a moving clock,
the mock runner leasing and posting through the real /internal endpoints, the DPU key gate
(/internal/authz/keys), health from a stub Prometheus, and one trace per job. Running the same
script against the board itself (and recording it, trace in Jaeger) needs the hardware: see
plan/10 B-86."""
import dataclasses
import hashlib
from datetime import timedelta

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from service import config, engine, models, otel
from service.runner import InternalApi, MockRunner, RunnerError
from service.scheduler import SchedConfig

ALICE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlicesKey00 alice@laptop"
BOB_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBobsKey00000 bob@laptop"
SECRET = {"X-Daolab-Secret": "test-secret"}


class ClientApi(InternalApi):
    def __init__(self, client):
        super().__init__("http://testserver", "test-secret")
        self.client = client

    def post(self, path, body):
        r = self.client.post(path, json=body, headers=SECRET)
        if r.status_code >= 400:
            raise RunnerError(f"{path}: {r.status_code} {r.text}")
        return r.json()


class Prom:
    """Stub Prometheus: the reservation's telemetry is fresh and every rule passes."""
    def __init__(self, clock):
        self.clock = clock

    def __call__(self, promql):
        t = self.clock().timestamp()
        return [({}, t, t - 5)] if "timestamp(" in promql else [({}, t, 1.0)]


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(api_env):
    env = api_env
    for name, key in (("alice", ALICE_KEY), ("bob", BOB_KEY)):
        u = models.user_by_token_hash(env.conn, hashlib.sha256(env.tokens[name].encode()).hexdigest())
        models.update(env.conn, "users", u["id"], {"ssh_pubkey": key})
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    otel.use_provider(provider)
    yield env, spans
    otel.use_provider(None)


def dpu_keys(env, login="lab"):
    r = env.client.get("/internal/authz/keys", params={"host": "dpu-01", "login": login},
                       headers=SECRET)
    return r.text


def test_alice_one_click_vpp_through_to_released_and_bob_never_gets_in(world, monkeypatch):
    env, spans = world
    cfg = SchedConfig.from_settings(config.settings)
    rn = MockRunner(api=ClientApi(env.client))

    # 1. alice: one click (the dashboard sends only the use case + her key)
    r = env.client.post("/reservations", headers=H(env.tokens["alice"]),
                        json={"usecase": "vpp", "ssh_pubkey": ALICE_KEY})
    assert r.status_code == 201, r.text
    res = r.json()
    rid = res["id"]
    start, end = models.parse_ts(res["start_at"]), models.parse_ts(res["end_at"])
    assert res["access"]["ssh"] == "ssh lab@10.68.0.190" and res["access"]["auth"] == "ssh-key"
    now = [start - timedelta(minutes=20)]
    clock = lambda: now[0]                                          # noqa: E731

    monkeypatch.setattr(config, "settings",                     # the telemetry stack is on
                        dataclasses.replace(config.settings, otel_endpoint="otel.lab:4317"))

    def tick(at):
        now[0] = at
        return engine.tick(env.conn, at, rn, cfg, health_query=Prom(clock))

    def state():
        return env.client.get(f"/reservations/{rid}", headers=H(env.tokens["alice"])).json()

    # bob is blocked before anything happens
    assert BOB_KEY not in dpu_keys(env) and dpu_keys(env) == ""

    # 2. the engine provisions it at start - lead: VMs leased from the lab pool, the DPU static
    tick(start - timedelta(minutes=15))
    s = state()
    assert s["state"] == "ready"
    vms = {v["role"]: v for v in s["vms"]}
    assert vms["vpp"]["ip"] == "10.68.0.190"
    assert {vms["sink"]["ip"], vms["tgen"]["ip"]} == {"10.68.1.0", "10.68.1.1"}
    [deploy] = models.jobs_for_reservation(env.conn, rid)
    assert deploy["state"] == "succeeded" and deploy["trace_id"]
    root = [sp for sp in spans.get_finished_spans() if sp.name == "job deploy"]
    assert len(root) == 1 and format(root[0].context.trace_id, "032x") == deploy["trace_id"]
    assert dpu_keys(env) == ""                                  # ready, but the slot has not begun

    # 3. the slot starts: active, alice's key (and only hers) opens the DPU, health is green
    tick(start + timedelta(minutes=1))
    s = state()
    assert s["state"] == "active" and s["access"]["live"] is True
    monkeypatch.setattr("service.api.datetime", _Frozen(now[0]))
    assert dpu_keys(env) == ALICE_KEY + "\n"
    assert BOB_KEY not in dpu_keys(env) and dpu_keys(env, "bob") == ""
    assert s["health"]["verdict"] == "healthy"
    bob_view = env.client.get(f"/reservations/{rid}", headers=H(env.tokens["bob"]))
    assert bob_view.status_code == 403                          # no IPs, no access line for bob

    # 4. the slot ends: access revoked first, cleanup verified, leases released
    tick(end)
    s = state()
    assert s["state"] == "released" and s["access"]["live"] is False
    kinds = [(j["kind"], j["state"]) for j in models.jobs_for_reservation(env.conn, rid)]
    assert kinds == [("deploy", "succeeded"), ("cleanup", "succeeded")]
    cleanup_steps = [st["name"] for st in models.job_steps(
        models.jobs_for_reservation(env.conn, rid)[-1])]
    assert cleanup_steps[0] == "terminate-user"
    assert [l["state"] for l in models.ip_leases_for_reservation(env.conn, rid)] == \
        ["released", "released"]
    monkeypatch.setattr("service.api.datetime", _Frozen(now[0]))
    assert dpu_keys(env) == ""                                  # nobody after the slot
    assert models.setup_by_name(env.conn, "tb-01")["availability"] == "available"
    actions = [e["action"] for e in models.list_audit_events(env.conn)]
    assert "deploy" in actions and "cleanup" in actions


class _Frozen:
    """Stands in for api.datetime so /internal/authz/keys sees the engine's clock."""
    def __init__(self, at):
        self.at = at

    def now(self, tz=None):
        return self.at
