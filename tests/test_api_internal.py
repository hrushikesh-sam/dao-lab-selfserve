"""The playbook / sshd-facing /internal endpoints (plan/09 §4)."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from service import ipam, models

SECRET = {"X-Daolab-Secret": "test-secret"}


def H(token):
    return {"Authorization": f"Bearer {token}"}


def tomorrow(hour):
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1, hours=hour)


@pytest.fixture
def deploying(api_env):
    """alice's vpp reservation, moved to provisioning with its vms rows (as the engine does)."""
    r = api_env.client.post("/reservations", headers=H(api_env.tokens["alice"]), json={
        "usecase": "vpp", "mode": "at", "start_at": models.format_ts(tomorrow(10)), "hours": 2})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    models.transition_reservation(api_env.conn, rid, "provisioning", from_state="queued")
    models.create_vms(api_env.conn, rid)
    return api_env, rid


def lease(env, rid, role, headers=SECRET):
    return env.client.post("/internal/leases", headers=headers,
                           json={"reservation_id": rid, "role": role})


# ---- B-42: POST /internal/leases --------------------------------------------------------

def test_lease_needs_the_internal_secret(deploying):
    env, rid = deploying
    assert lease(env, rid, "tgen", headers={}).status_code == 401
    assert lease(env, rid, "tgen", headers={"X-Daolab-Secret": "nope"}).status_code == 401
    assert lease(env, rid, "tgen", headers=H(env.tokens["dev-admin"])).status_code == 401


def test_lease_returns_the_lowest_free_address_and_is_idempotent(deploying):
    env, rid = deploying
    first = lease(env, rid, "tgen")
    assert first.status_code == 200
    assert first.json() == {"ip": "10.68.1.0", "reservation_id": rid, "role": "tgen",
                            "state": "leased"}
    assert lease(env, rid, "tgen").json()["ip"] == "10.68.1.0"       # playbook replay
    assert lease(env, rid, "sink").json()["ip"] == "10.68.1.1"


def test_lease_rejects_unknown_reservations_and_roles(deploying):
    env, rid = deploying
    r = lease(env, 9999, "tgen")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = lease(env, rid, "nope")
    assert r.status_code == 404 and "role nope" in r.json()["error"]["message"]


def test_the_dpu_role_has_no_vm_to_lease_for(deploying):
    env, rid = deploying
    r = lease(env, rid, "vpp")
    assert r.status_code == 422 and "runs on the DPU" in r.json()["error"]["message"]


@pytest.mark.parametrize("st", ["queued", "cleaning", "quarantined", "released", "failed"])
def test_lease_rejects_reservations_that_are_not_deploying_or_live(deploying, st):
    env, rid = deploying
    env.conn.execute("UPDATE reservations SET state = ? WHERE id = ?", (st, rid))
    r = lease(env, rid, "tgen")
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"
    assert r.json()["error"]["detail"]["state"] == st
    assert models.ip_leases_for_reservation(env.conn, rid) == []


@pytest.mark.parametrize("st", ["ready", "active"])
def test_a_redeploy_inside_the_slot_can_lease(deploying, st):
    env, rid = deploying
    env.conn.execute("UPDATE reservations SET state = ? WHERE id = ?", (st, rid))
    assert lease(env, rid, "tgen").status_code == 200


def test_an_exhausted_pool_is_a_clear_409(deploying):
    env, rid = deploying
    env.conn.execute("UPDATE ip_pools SET range_end = range_start")      # one address
    assert lease(env, rid, "tgen").status_code == 200
    r = lease(env, rid, "sink")
    assert r.status_code == 409 and r.json()["error"]["code"] == "pool_exhausted"


# ---- B-42: POST /internal/leases/conflict -------------------------------------------------

def conflict(env, ip, headers=SECRET):
    return env.client.post("/internal/leases/conflict", headers=headers, json={"ip": ip})


def test_a_conflicting_address_is_quarantined_and_replaced(deploying):
    env, rid = deploying
    lease(env, rid, "tgen")                                             # .100
    lease(env, rid, "sink")                                             # .101
    r = conflict(env, "10.68.1.0")
    assert r.status_code == 200
    assert r.json() == {"ip": "10.68.1.2", "reservation_id": rid, "role": "tgen",
                        "state": "leased", "replaced": "10.68.1.0"}
    assert lease(env, rid, "tgen").json()["ip"] == "10.68.1.2"       # the new one sticks
    states = {l["ip"]: l["state"] for l in models.ip_leases_for_reservation(env.conn, rid)}
    assert states == {"10.68.1.0": "quarantined", "10.68.1.1": "leased",
                      "10.68.1.2": "leased"}


def test_conflict_needs_the_secret_and_a_leased_valid_address(deploying):
    env, rid = deploying
    lease(env, rid, "tgen")
    assert conflict(env, "10.68.1.0", headers={}).status_code == 401
    assert conflict(env, "10.68.1.50").status_code == 404              # not leased
    assert conflict(env, "not-an-ip").status_code == 422
    assert conflict(env, "10.68.1.0").status_code == 200
    assert conflict(env, "10.68.1.0").status_code == 404              # already quarantined


def test_conflict_for_a_reservation_that_moved_on_changes_nothing(deploying):
    env, rid = deploying
    lease(env, rid, "tgen")
    env.conn.execute("UPDATE reservations SET state = 'cleaning' WHERE id = ?", (rid,))
    assert conflict(env, "10.68.1.0").status_code == 409
    [l] = models.ip_leases_for_reservation(env.conn, rid)
    assert l["state"] == "leased"                                       # rolled back


# ---- B-51: GET /internal/authz/keys ------------------------------------------------------

ALICE_KEY = "ssh-ed25519 AAAAalice alice@laptop"


@pytest.fixture
def slot(api_env):
    """alice holds tb-01 right now (active, started an hour ago, ends in an hour)."""
    env = api_env
    alice = models.user_by_token_hash(
        env.conn, hashlib.sha256(env.tokens["alice"].encode()).hexdigest())
    models.update(env.conn, "users", alice["id"], {"ssh_pubkey": ALICE_KEY})
    now = datetime.now(timezone.utc)
    sid = models.setup_by_name(env.conn, "tb-01")["id"]
    uc = models.usecase_by_name(env.conn, "vpp")["id"]
    rid = models.insert(env.conn, "reservations", {
        "user_id": alice["id"], "usecase_id": uc, "setup_id": sid, "state": "active",
        "requested_start_at": models.format_ts(now - timedelta(hours=1)),
        "start_at": models.format_ts(now - timedelta(hours=1)),
        "end_at": models.format_ts(now + timedelta(hours=1))})
    return env, rid


def keys(env, host="dpu-01", login="lab", headers=SECRET):
    return env.client.get("/internal/authz/keys", params={"host": host, "login": login},
                          headers=headers)


def test_the_slot_owner_gets_exactly_their_key_as_plain_text(slot):
    env, _ = slot
    r = keys(env)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == ALICE_KEY + "\n"


def test_nobody_gets_a_key_outside_the_slot(slot):
    env, rid = slot
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    env.conn.execute("UPDATE reservations SET start_at = ?, end_at = ? WHERE id = ?",
                     (models.format_ts(past), models.format_ts(past + timedelta(hours=1)), rid))
    r = keys(env)
    assert (r.status_code, r.text) == (200, "")


@pytest.mark.parametrize("host,login", [("dpu-01", "root"), ("dpu-01", "alice"),
                                        ("nope", "lab"), ("", "")])
def test_other_logins_and_hosts_get_nothing(slot, host, login):
    env, _ = slot
    r = keys(env, host=host, login=login)
    assert (r.status_code, r.text) == (200, "")


def test_a_bad_secret_gets_an_empty_401(slot):
    env, _ = slot
    for headers in ({}, {"X-Daolab-Secret": "nope"}, H(env.tokens["dev-admin"])):
        r = keys(env, headers=headers)
        assert (r.status_code, r.text) == (401, "")


def test_the_read_only_authz_secret_works_but_cannot_lease(slot, monkeypatch):
    import dataclasses
    from service import config
    env, rid = slot
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, authz_secret="dpu-only"))
    assert keys(env, headers={"X-Daolab-Secret": "dpu-only"}).text == ALICE_KEY + "\n"
    assert keys(env).text == ALICE_KEY + "\n"                          # internal one still works
    r = env.client.post("/internal/leases", headers={"X-Daolab-Secret": "dpu-only"},
                        json={"reservation_id": rid, "role": "tgen"})
    assert r.status_code == 401


def test_any_error_answers_no_keys(slot, monkeypatch):
    from service import access
    env, _ = slot

    def boom(*a, **kw):
        raise RuntimeError("db on fire")
    monkeypatch.setattr(access, "keys_for", boom)
    r = keys(env)
    assert (r.status_code, r.text) == (200, "")


def test_a_disabled_owner_is_locked_out_at_the_next_login(slot):
    env, _ = slot
    env.conn.execute("UPDATE users SET enabled = 0 WHERE username = 'alice'")
    assert keys(env).text == ""
