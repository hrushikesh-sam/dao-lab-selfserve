"""The playbook / sshd-facing /internal endpoints (plan/09 §4)."""
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
    assert first.json() == {"ip": "10.30.0.100", "reservation_id": rid, "role": "tgen",
                            "state": "leased"}
    assert lease(env, rid, "tgen").json()["ip"] == "10.30.0.100"       # playbook replay
    assert lease(env, rid, "sink").json()["ip"] == "10.30.0.101"


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
    r = conflict(env, "10.30.0.100")
    assert r.status_code == 200
    assert r.json() == {"ip": "10.30.0.102", "reservation_id": rid, "role": "tgen",
                        "state": "leased", "replaced": "10.30.0.100"}
    assert lease(env, rid, "tgen").json()["ip"] == "10.30.0.102"       # the new one sticks
    states = {l["ip"]: l["state"] for l in models.ip_leases_for_reservation(env.conn, rid)}
    assert states == {"10.30.0.100": "quarantined", "10.30.0.101": "leased",
                      "10.30.0.102": "leased"}


def test_conflict_needs_the_secret_and_a_leased_valid_address(deploying):
    env, rid = deploying
    lease(env, rid, "tgen")
    assert conflict(env, "10.30.0.100", headers={}).status_code == 401
    assert conflict(env, "10.30.0.150").status_code == 404              # not leased
    assert conflict(env, "not-an-ip").status_code == 422
    assert conflict(env, "10.30.0.100").status_code == 200
    assert conflict(env, "10.30.0.100").status_code == 404              # already quarantined


def test_conflict_for_a_reservation_that_moved_on_changes_nothing(deploying):
    env, rid = deploying
    lease(env, rid, "tgen")
    env.conn.execute("UPDATE reservations SET state = 'cleaning' WHERE id = ?", (rid,))
    assert conflict(env, "10.30.0.100").status_code == 409
    [l] = models.ip_leases_for_reservation(env.conn, rid)
    assert l["state"] == "leased"                                       # rolled back
