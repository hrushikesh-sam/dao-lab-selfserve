"""B-66: GET /reservations/{id}/health (owner, 15 s cache) + POST .../health/probe."""
import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from service import api, config, engine, health, models
from service.runner import MockRunner
from service.scheduler import SchedConfig


def H(token):
    return {"Authorization": f"Bearer {token}"}


class Prom:
    """Stub Prometheus: every rule passes unless `down` names its metric; counts queries."""
    def __init__(self):
        self.calls, self.down = 0, set()

    def __call__(self, promql):
        self.calls += 1
        t = datetime.now(timezone.utc).timestamp()
        if "timestamp(" in promql:
            return [({}, t, t - 5)]
        return [({}, t, 0.0 if any(m in promql for m in self.down) else 1.0)]


@pytest.fixture
def live(api_env, monkeypatch):
    """alice's vpp reservation, active right now, telemetry stack on, Prometheus stubbed."""
    env = api_env
    api._health_cache.clear()
    prom = Prom()
    monkeypatch.setattr(health, "prometheus", lambda url, timeout=5.0: prom)
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, otel_endpoint="localhost:4317"))
    alice = models.user_by_token_hash(
        env.conn, hashlib.sha256(env.tokens["alice"].encode()).hexdigest())
    now = datetime.now(timezone.utc)
    rid = models.insert(env.conn, "reservations", {
        "user_id": alice["id"], "usecase_id": models.usecase_by_name(env.conn, "vpp")["id"],
        "setup_id": models.setup_by_name(env.conn, "tb-01")["id"], "state": "active",
        "requested_start_at": models.format_ts(now - timedelta(hours=1)),
        "start_at": models.format_ts(now - timedelta(hours=1)),
        "end_at": models.format_ts(now + timedelta(hours=1))})
    yield env, rid, prom
    api._health_cache.clear()


def get_health(env, rid, who="alice"):
    return env.client.get(f"/reservations/{rid}/health", headers=H(env.tokens[who]))


def test_the_owner_gets_the_verdict_with_every_check(live):
    env, rid, _ = live
    r = get_health(env, rid)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "healthy" and body["reservation_id"] == rid
    assert [c["name"] for c in body["checks"]] == ["vms_up", "ssh_reachable", "dpu_app", "datapath"]
    assert all(c["ok"] and c["value"] == 1.0 for c in body["checks"])
    assert body["cached"] is False and body["last_seen"] is not None


def test_a_degraded_and_a_down_verdict(live):
    env, rid, prom = live
    prom.down = {"vpp_interface_rx_packets"}
    assert get_health(env, rid).json()["verdict"] == "degraded"
    api._health_cache.clear()
    prom.down = {"vpp_up"}
    body = get_health(env, rid).json()
    assert body["verdict"] == "down" and body["reason"] == "failing: dpu_app"


def test_answers_are_cached_for_fifteen_seconds(live, monkeypatch):
    env, rid, prom = live
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    get_health(env, rid)
    first = prom.calls
    clock[0] += 14.9
    assert get_health(env, rid).json()["cached"] is True and prom.calls == first
    clock[0] += 0.2                                                     # 15.1 s later
    assert get_health(env, rid).json()["cached"] is False and prom.calls == 2 * first


def test_only_the_owner_or_an_admin(live):
    env, rid, _ = live
    assert get_health(env, rid, "bob").status_code == 403
    assert get_health(env, rid, "dev-admin").status_code == 200
    assert env.client.get("/reservations/999/health",
                          headers=H(env.tokens["alice"])).status_code == 404


def test_the_response_carries_the_deploy_trace_id(live):
    env, rid, _ = live
    models.create_job(env.conn, rid, "deploy", trace_id="ab" * 16)
    assert get_health(env, rid).json()["trace_id"] == "ab" * 16


def test_without_the_telemetry_stack_health_is_the_probe_result(live, monkeypatch):
    env, rid, prom = live
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, otel_endpoint=""))
    body = get_health(env, rid).json()
    assert body["verdict"] == "unknown" and "DAOLAB_OTEL_ENDPOINT" in body["reason"]
    assert body["probe"] is None and prom.calls == 0


def test_a_reservation_that_is_not_live_is_unknown_without_asking(live):
    env, rid, prom = live
    env.conn.execute("UPDATE reservations SET state = 'released' WHERE id = ?", (rid,))
    body = get_health(env, rid).json()
    assert (body["verdict"], body["reason"]) == ("unknown", "reservation is released")
    assert prom.calls == 0


def test_the_reservation_carries_the_engines_latest_verdict(live):
    env, rid, _ = live
    get = lambda: env.client.get(f"/reservations/{rid}", headers=H(env.tokens["alice"])).json()  # noqa: E731
    assert get()["health"] is None
    models.save_health_snapshot(env.conn, rid, {"verdict": "down", "reason": "failing: dpu_app",
                                                "checks": [], "evaluated_at": "2026-10-16T10:00:00Z"})
    summary = get()["health"]
    assert summary["verdict"] == "down" and summary["reason"] == "failing: dpu_app"


# ---- POST .../health/probe ---------------------------------------------------------------

def probe(env, rid, who="alice"):
    return env.client.post(f"/reservations/{rid}/health/probe", headers=H(env.tokens[who]))


def test_probe_queues_a_health_probe_job(live):
    env, rid, _ = live
    r = probe(env, rid)
    assert r.status_code == 202
    job = r.json()["job"]
    assert (job["kind"], job["state"], job["reservation_id"]) == ("health_probe", "pending", rid)


def test_a_second_probe_while_one_waits_returns_the_same_job(live):
    env, rid, _ = live
    first = probe(env, rid).json()["job"]["id"]
    assert probe(env, rid).json()["job"]["id"] == first
    assert len(models.jobs_for_reservation(env.conn, rid, kind="health_probe")) == 1


def test_probe_is_owner_only_and_live_only(live):
    env, rid, _ = live
    assert probe(env, rid, "bob").status_code == 403
    env.conn.execute("UPDATE reservations SET state = 'queued' WHERE id = ?", (rid,))
    r = probe(env, rid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"


def test_the_probe_result_is_recorded_on_the_job_and_shown_with_the_health(live, monkeypatch):
    env, rid, _ = live
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, otel_endpoint=""))
    jid = probe(env, rid).json()["job"]["id"]
    engine.tick(env.conn, datetime.now(timezone.utc), MockRunner(), SchedConfig())
    job = env.client.get(f"/jobs/{jid}", headers=H(env.tokens["alice"])).json()
    assert job["state"] == "succeeded" and [s["name"] for s in job["steps"]] == ["ping_vms",
                                                                               "check_dpu_app"]
    body = get_health(env, rid).json()
    assert body["probe"]["job_id"] == jid and body["probe"]["state"] == "succeeded"
    assert probe(env, rid).json()["job"]["id"] != jid                  # finished: a new one
