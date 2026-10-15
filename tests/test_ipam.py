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
