"""B-74: POST /admin/setups/{name}/force-clean — reconcile + clear quarantine, audit-logged."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from service import config, engine, ipam, models
from service.runner import MockRunner
from service.scheduler import SchedConfig


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def quarantined(api_env):
    """alice's slot just ended and its cleanup left residue: tb-01 is quarantined."""
    env = api_env
    alice = models.user_by_token_hash(
        env.conn, hashlib.sha256(env.tokens["alice"].encode()).hexdigest())
    now = datetime.now(timezone.utc)
    rid = models.insert(env.conn, "reservations", {
        "user_id": alice["id"], "usecase_id": models.usecase_by_name(env.conn, "vpp")["id"],
        "setup_id": models.setup_by_name(env.conn, "tb-01")["id"], "state": "active",
        "requested_start_at": models.format_ts(now - timedelta(hours=2)),
        "start_at": models.format_ts(now - timedelta(hours=2)),
        "end_at": models.format_ts(now - timedelta(minutes=1))})
    models.create_vms(env.conn, rid)
    ipam.lease(env.conn, rid, "tgen")
    tick(env, MockRunner(fail={"cleanup"}))
    assert models.reservation_by_id(env.conn, rid)["state"] == "quarantined"
    assert availability(env) == "quarantined"
    return env, rid


def tick(env, rn):
    return engine.tick(env.conn, datetime.now(timezone.utc), rn,
                       SchedConfig.from_settings(config.settings))


def availability(env, name="tb-01"):
    return models.setup_by_name(env.conn, name)["availability"]


def force(env, who="dev-admin", name="tb-01", **body):
    return env.client.post(f"/admin/setups/{name}/force-clean", headers=H(env.tokens[who]),
                           json=body or None)


def test_users_cannot_force_clean(quarantined):
    env, _ = quarantined
    r = force(env, who="alice")
    assert r.status_code == 403 and r.json()["error"]["detail"] == {"required_role": "admin"}


def test_an_unknown_setup_is_404(quarantined):
    env, _ = quarantined
    assert force(env, name="tb-99").status_code == 404


def test_force_clean_queues_a_reconcile_and_frees_the_setup_once_it_is_clean(quarantined):
    env, rid = quarantined
    r = force(env)
    assert r.status_code == 202
    body = r.json()
    assert body["reservations"] == [rid] and body["previous_availability"] == "quarantined"
    assert body["availability"] == "quarantined"                    # not before it is proven
    [job] = body["jobs"]
    assert (job["kind"], job["state"], job["reservation_id"]) == ("reconcile", "pending", rid)
    tick(env, MockRunner())                                          # residue gone now
    assert models.reservation_by_id(env.conn, rid)["state"] == "released"
    assert availability(env) == "available"
    assert [l["state"] for l in models.ip_leases_for_reservation(env.conn, rid)] == ["released"]


def test_if_reconcile_still_finds_residue_the_quarantine_stays(quarantined):
    env, rid = quarantined
    force(env)
    tick(env, MockRunner(fail={"cleanup"}))
    assert models.reservation_by_id(env.conn, rid)["state"] == "quarantined"
    assert availability(env) == "quarantined"


def test_a_second_force_clean_reuses_the_waiting_job(quarantined):
    env, rid = quarantined
    first = force(env).json()["jobs"][0]["id"]
    assert force(env).json()["jobs"][0]["id"] == first
    assert len(models.jobs_for_reservation(env.conn, rid, kind="reconcile")) == 1


def test_force_clean_is_audit_logged(quarantined):
    env, rid = quarantined
    force(env)
    [event] = [e for e in models.list_audit_events(env.conn) if e["action"] == "force_clean"]
    assert event["actor"] == "dev-admin" and event["object_type"] == "setup"
    detail = models.from_json(event["detail"])
    assert detail["setup"] == "tb-01" and detail["reservations"] == [rid]
    assert detail["skip_verify"] is False and len(detail["jobs"]) == 1


def test_a_quarantined_setup_with_nothing_dirty_is_cleared_at_once(api_env):
    env = api_env
    sid = models.setup_by_name(env.conn, "tb-01")["id"]
    models.set_setup_availability(env.conn, sid, "quarantined")
    body = force(env).json()
    assert body["availability"] == "available" and body["jobs"] == []
    assert availability(env) == "available"


def test_skip_verify_releases_everything_now(quarantined):
    env, rid = quarantined
    body = force(env, skip_verify=True).json()
    assert body["availability"] == "available" and body["skip_verify"] is True
    assert body["jobs"] == []
    assert models.reservation_by_id(env.conn, rid)["state"] == "released"
    assert [l["state"] for l in models.ip_leases_for_reservation(env.conn, rid)] == ["released"]
    [event] = [e for e in models.list_audit_events(env.conn) if e["action"] == "force_clean"]
    assert models.from_json(event["detail"])["skip_verify"] is True


def test_maintenance_is_never_lifted_by_force_clean(quarantined):
    env, rid = quarantined
    models.set_setup_availability(env.conn, models.setup_by_name(env.conn, "tb-01")["id"],
                                  "maintenance")
    assert force(env, skip_verify=True).json()["availability"] == "maintenance"
    assert models.reservation_by_id(env.conn, rid)["state"] == "released"


def test_unknown_body_fields_are_refused(quarantined):
    env, _ = quarantined
    assert force(env, everything=True).status_code == 422


def test_get_setups_shows_the_quarantine_and_its_residue(quarantined):
    env, rid = quarantined
    [setup] = env.client.get("/setups", headers=H(env.tokens["dev-admin"])).json()
    assert setup["availability"] == "quarantined"
    [d] = setup["dirty"]
    assert d["id"] == rid and d["state"] == "quarantined"
    assert d["residue"].startswith("residue after cleanup:") and f"daolab-r{rid}-" in d["residue"]


def test_a_queued_booking_starts_only_after_the_reconcile_proved_it_clean(quarantined, monkeypatch):
    env, rid = quarantined
    nxt = env.client.post("/reservations", headers=H(env.tokens["bob"]),
                          json={"usecase": "vpp", "hours": 1}).json()
    force(env)
    start = models.parse_ts(nxt["start_at"])
    engine.tick(env.conn, start, MockRunner(), SchedConfig.from_settings(config.settings))
    # this tick: step 3 still sees the quarantined reservation holding tb-01; step 4 reconciles
    assert models.reservation_by_id(env.conn, nxt["id"])["state"] == "queued"
    engine.tick(env.conn, start + timedelta(seconds=60), MockRunner(),
                SchedConfig.from_settings(config.settings))
    assert models.reservation_by_id(env.conn, nxt["id"])["state"] in ("ready", "active")
