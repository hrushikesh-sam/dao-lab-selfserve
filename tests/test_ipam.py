"""B-40: IP pool + lease schema — plan/09 IPAM.

`(pool, ip)` is unique among *live* leases (leased or quarantined), and
`(reservation, role)` is unique among live leases so re-leasing is idempotent.
"""
import sqlite3

import pytest

from service import migrations, models
from tests.conftest import T, mk_res, mk_setup, mk_usecase


@pytest.fixture
def ctx(db):
    """In-memory DB (with user u1 from conftest) + setup/usecase/reservation + one pool."""
    sid = mk_setup(db, "s1", ["wire"])
    cid = mk_usecase(db, "uc1", ["wire"])
    res = mk_res(db, sid, cid, T(10), T(12))
    pid = models.insert(db, "ip_pools", {
        "name": "lab1", "range_start": "10.0.0.100", "range_end": "10.0.0.150"})
    return db, pid, res


def _lease(conn, pid, res, ip, role="vm1", state="leased"):
    return models.insert(conn, "ip_leases", {
        "pool_id": pid, "ip": ip, "reservation_id": res, "role": role, "state": state})


def test_migration_creates_tables(db):
    assert migrations.current_version(db) >= 2
    assert migrations.has_table(db, "ip_pools")
    assert migrations.has_table(db, "ip_leases")


def test_duplicate_live_ip_fails(ctx):
    conn, pid, res = ctx
    _lease(conn, pid, res, "10.0.0.100", role="a")
    with pytest.raises(sqlite3.IntegrityError):
        _lease(conn, pid, res, "10.0.0.100", role="b")


def test_quarantined_ip_not_reissued(ctx):
    conn, pid, res = ctx
    _lease(conn, pid, res, "10.0.0.100", role="a", state="quarantined")
    with pytest.raises(sqlite3.IntegrityError):
        _lease(conn, pid, res, "10.0.0.100", role="b")


def test_released_ip_is_reusable(ctx):
    conn, pid, res = ctx
    lid = _lease(conn, pid, res, "10.0.0.100", role="a")
    models.update(conn, "ip_leases", lid, {"state": "released"})
    _lease(conn, pid, res, "10.0.0.100", role="b")            # no error


def test_reservation_role_unique_among_live(ctx):
    conn, pid, res = ctx
    _lease(conn, pid, res, "10.0.0.100", role="a")
    with pytest.raises(sqlite3.IntegrityError):
        _lease(conn, pid, res, "10.0.0.101", role="a")


def test_reservation_role_reusable_after_release(ctx):
    conn, pid, res = ctx
    lid = _lease(conn, pid, res, "10.0.0.100", role="a")
    models.update(conn, "ip_leases", lid, {"state": "released"})
    _lease(conn, pid, res, "10.0.0.101", role="a")            # no error


def test_same_ip_allowed_in_different_pool(ctx):
    conn, pid, res = ctx
    pid2 = models.insert(conn, "ip_pools", {
        "name": "lab2", "range_start": "10.0.1.0", "range_end": "10.0.1.10"})
    _lease(conn, pid, res, "10.0.0.100", role="a")
    _lease(conn, pid2, res, "10.0.0.100", role="c")           # no error


def test_live_leases_helper_excludes_released(ctx):
    conn, pid, res = ctx
    a = _lease(conn, pid, res, "10.0.0.100", role="a")
    _lease(conn, pid, res, "10.0.0.101", role="b")
    models.update(conn, "ip_leases", a, {"state": "released"})
    ips = [r["ip"] for r in models.live_leases(conn, pid)]
    assert ips == ["10.0.0.101"]


# ---- B-41: ipam.py — lease / release / quarantine / mark_conflict --------------------------

from service import ipam                                         # noqa: E402


@pytest.fixture
def pool(db):
    """A 4-address default pool plus two reservations on one setup."""
    sid = mk_setup(db, "s1", ["wire"])
    cid = mk_usecase(db, "uc1", ["wire"])
    r1 = mk_res(db, sid, cid, T(10), T(12))
    r2 = mk_res(db, sid, cid, T(13), T(14))
    models.insert(db, "ip_pools", {"name": ipam.DEFAULT_POOL,
                                   "range_start": "10.30.0.100", "range_end": "10.30.0.103"})
    return db, r1, r2


def test_migration_7_lets_a_quarantined_role_lease_again(ctx):
    conn, pid, res = ctx
    _lease(conn, pid, res, "10.0.0.100", role="a", state="quarantined")
    _lease(conn, pid, res, "10.0.0.101", role="a")                   # no error since v7
    with pytest.raises(sqlite3.IntegrityError):
        _lease(conn, pid, res, "10.0.0.102", role="a")                # but only one leased


def test_lease_takes_the_lowest_free_address(pool):
    db, r1, r2 = pool
    assert ipam.lease(db, r1, "client")["ip"] == "10.30.0.100"
    assert ipam.lease(db, r1, "backend")["ip"] == "10.30.0.101"
    assert ipam.lease(db, r2, "client")["ip"] == "10.30.0.102"


def test_lease_is_idempotent_per_reservation_and_role(pool):
    db, r1, _ = pool
    first = ipam.lease(db, r1, "client")
    again = ipam.lease(db, r1, "client")
    assert (again["id"], again["ip"]) == (first["id"], first["ip"])
    assert len(models.ip_leases_for_reservation(db, r1)) == 1


def test_a_released_address_is_reused_lowest_first(pool):
    db, r1, r2 = pool
    ipam.lease(db, r1, "client")
    ipam.lease(db, r1, "backend")
    assert ipam.release(db, r1) == 2
    assert ipam.lease(db, r2, "client")["ip"] == "10.30.0.100"


def test_exhaustion_raises_and_writes_nothing(pool):
    db, r1, r2 = pool
    for role in ("a", "b", "c", "d"):
        ipam.lease(db, r1, role)
    with pytest.raises(ipam.PoolExhausted, match="no free address"):
        ipam.lease(db, r2, "client")
    assert models.ip_leases_for_reservation(db, r2) == []
    assert not db.in_transaction                                     # rolled back cleanly


def test_a_quarantined_address_is_never_reissued(pool):
    db, r1, r2 = pool
    ipam.lease(db, r1, "client")                                     # .100
    assert ipam.quarantine_reservation(db, r1) == 1
    assert ipam.release(db, r1) == 0                                 # release keeps it blocked
    got = [ipam.lease(db, r2, role)["ip"] for role in ("a", "b", "c")]
    assert "10.30.0.100" not in got
    with pytest.raises(ipam.PoolExhausted):
        ipam.lease(db, r2, "d")


def test_quarantine_by_ip_is_idempotent_and_needs_a_live_lease(pool):
    db, r1, _ = pool
    ipam.lease(db, r1, "client")
    assert ipam.quarantine(db, "10.30.0.100")["state"] == "quarantined"
    assert ipam.quarantine(db, "10.30.0.100")["state"] == "quarantined"
    with pytest.raises(ipam.IpamError, match="no live lease"):
        ipam.quarantine(db, "10.30.0.103")


def test_mark_conflict_blocks_the_address_and_the_role_gets_a_new_one(pool):
    db, r1, r2 = pool
    old = ipam.lease(db, r1, "client")
    bad = ipam.mark_conflict(db, old["ip"])
    assert (bad["id"], bad["state"]) == (old["id"], "quarantined")
    new = ipam.lease(db, r1, "client")
    assert new["ip"] == "10.30.0.101" and new["id"] != old["id"]
    assert ipam.lease(db, r2, "client")["ip"] == "10.30.0.102"      # .100 stays blocked
    with pytest.raises(ipam.IpamError, match="not leased"):
        ipam.mark_conflict(db, old["ip"])                            # already quarantined


def test_clear_frees_quarantined_addresses_too(pool):
    db, r1, r2 = pool
    ipam.lease(db, r1, "client")
    ipam.lease(db, r1, "backend")
    ipam.quarantine_reservation(db, r1)
    assert ipam.clear(db, r1) == 2
    assert ipam.lease(db, r2, "client")["ip"] == "10.30.0.100"


def test_no_pool_is_a_clear_error(db):
    sid = mk_setup(db, "s1", ["wire"])
    rid = mk_res(db, sid, mk_usecase(db, "uc1", ["wire"]), T(10), T(12))
    with pytest.raises(ipam.IpamError, match="daolab sync"):
        ipam.lease(db, rid, "client")


def test_lease_joins_the_callers_transaction(pool):
    """Inside the caller's transaction ipam neither commits nor rolls back on its own."""
    db, r1, _ = pool
    db.execute("BEGIN IMMEDIATE")
    ipam.lease(db, r1, "client")
    db.execute("ROLLBACK")
    assert models.ip_leases_for_reservation(db, r1) == []
