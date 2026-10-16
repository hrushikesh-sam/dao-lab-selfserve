"""B-21: reservation states — `tearing_down` -> `cleaning`, new `quarantined`, and the
compare-and-set transition helper (plan/09 §10). Migration 6 keeps the DB CHECK in step."""
import random
import sqlite3

import pytest

from service import migrations, models
from service.models import ReservationStateError
from tests.conftest import T, mk_res, mk_setup, mk_usecase

STATES = models.RESERVATION_STATES
ALL_PAIRS = [(a, b) for a in STATES for b in STATES]
LEGAL = [(a, b) for a, b in ALL_PAIRS if b in models.RESERVATION_TRANSITIONS[a]]
ILLEGAL = [(a, b) for a, b in ALL_PAIRS if b not in models.RESERVATION_TRANSITIONS[a]]


@pytest.fixture
def ctx(db):
    sid = mk_setup(db, "s1", ["wire"])
    cid = mk_usecase(db, "uc1", ["wire"])
    return db, sid, cid


def _res(ctx, state="queued", hour=10):
    db, sid, cid = ctx
    return mk_res(db, sid, cid, T(hour), T(hour + 1), state=state)


def _state(db, rid):
    return models.reservation_by_id(db, rid)["state"]


# ---- the constants describe one consistent machine ---------------------------

def test_state_lists_are_consistent():
    assert set(STATES) == {"queued", "provisioning", "ready", "active", "cleaning",
                           "quarantined", "released", "failed"}
    assert set(models.BUSY_STATES) | set(models.TERMINAL_STATES) == set(STATES)
    assert not set(models.BUSY_STATES) & set(models.TERMINAL_STATES)
    assert set(models.HELD_STATES) == set(models.BUSY_STATES) - {"queued"}
    assert "tearing_down" not in STATES


def test_exactly_the_plan_09_edges():
    assert set(LEGAL) == {
        ("queued", "provisioning"), ("queued", "released"),
        ("provisioning", "ready"), ("provisioning", "failed"),
        ("ready", "active"), ("ready", "cleaning"),
        ("active", "cleaning"),
        ("cleaning", "released"), ("cleaning", "quarantined"),
        ("quarantined", "released"),
        ("failed", "released")}


def test_every_state_is_reachable_from_queued_and_nothing_re_enters_queued():
    seen, todo = {"queued"}, ["queued"]
    while todo:
        for nxt in models.RESERVATION_TRANSITIONS[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(STATES)
    assert all("queued" not in outs for outs in models.RESERVATION_TRANSITIONS.values())
    assert models.RESERVATION_TRANSITIONS["released"] == ()


# ---- the DB CHECK agrees with the Python constants (migration 6) --------------

def test_fresh_db_accepts_every_state_and_nothing_else(ctx):
    db, sid, cid = ctx
    for st in STATES:
        mk_res(db, sid, cid, T(10), T(11), state=st)
    for bad in ("tearing_down", "paused", ""):
        with pytest.raises(sqlite3.IntegrityError):
            mk_res(db, sid, cid, T(10), T(11), state=bad)


def _v5_db(path):
    """A DB exactly as it was before B-21: baseline + migrations 1-5, user_version 5."""
    conn = models.connect(path)
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    for m in migrations.MIGRATIONS:
        if m.version <= 5:
            m.fn(conn)
    conn.execute("PRAGMA user_version = 5")
    return conn


def _seed_v5(conn):
    models.insert(conn, "users", {"username": "u", "token_hash": "h", "ssh_pubkey": "k"})
    sid = mk_setup(conn, "s1", ["wire"])
    cid = mk_usecase(conn, "uc1", ["wire"])
    ids = {st: mk_res(conn, sid, cid, T(8 + i), T(9 + i), state=st)
           for i, st in enumerate(["queued", "tearing_down", "released", "failed"])}
    models.insert(conn, "vms", {"reservation_id": ids["tearing_down"], "role": "backend",
                                "host": "h", "template": "t", "name": "n"})
    models.create_job(conn, ids["tearing_down"], "cleanup")
    pool = models.insert(conn, "ip_pools", {"name": "p", "range_start": "10.0.0.1",
                                            "range_end": "10.0.0.9"})
    models.insert(conn, "ip_leases", {"pool_id": pool, "ip": "10.0.0.2", "role": "backend",
                                      "reservation_id": ids["tearing_down"], "state": "leased"})
    return ids


def test_upgrade_renames_rows_and_keeps_every_child_row(tmp_path):
    path = str(tmp_path / "v5.db")
    conn = _v5_db(path)
    ids = _seed_v5(conn)
    with pytest.raises(sqlite3.IntegrityError):                      # the old CHECK refuses it
        conn.execute("UPDATE reservations SET state = 'cleaning' WHERE id = ?", (ids["queued"],))
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS if m.version > 5]
    states = {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM reservations")}
    assert states == {ids["queued"]: "queued", ids["tearing_down"]: "cleaning",
                      ids["released"]: "released", ids["failed"]: "failed"}
    for table in ("vms", "jobs", "ip_leases"):                       # nothing cascaded away
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
    assert conn.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute("UPDATE reservations SET state = 'quarantined' WHERE id = ?", (ids["queued"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE reservations SET state = 'tearing_down' WHERE id = ?", (ids["queued"],))
    assert models.init_db(conn) == []                               # second init: no-op
    conn.close()


def test_other_connections_see_the_new_rule(tmp_path):
    path = str(tmp_path / "v5.db")
    conn = _v5_db(path)
    ids = _seed_v5(conn)
    models.init_db(conn)
    other = sqlite3.connect(path)                                    # a brand-new connection
    other.execute("UPDATE reservations SET state = 'quarantined' WHERE id = ?", (ids["queued"],))
    other.commit()
    other.close()
    conn.close()


def test_migration_6_is_idempotent_when_run_again(tmp_path):
    conn = _v5_db(str(tmp_path / "v5.db"))
    _seed_v5(conn)
    m6 = migrations.MIGRATIONS[5]
    assert m6.version == 6
    m6.fn(conn)
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'reservations'").fetchone()[0]
    m6.fn(conn)                                                      # nothing left to change
    after = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'reservations'").fetchone()[0]
    assert before == after and before.count("'quarantined'") == 1
    conn.close()


def test_migration_6_fails_loudly_if_the_check_is_not_found():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reservations (id INTEGER PRIMARY KEY, state TEXT)")
    with pytest.raises(RuntimeError, match="cannot find the reservations.state CHECK"):
        migrations.MIGRATIONS[5].fn(conn)
    conn.close()


def test_a_failure_after_the_rewrite_rolls_the_rewrite_back(tmp_path, monkeypatch):
    """The runner wraps each migration in a transaction: if something later in the same
    migration fails, the table definition and user_version both revert."""
    conn = _v5_db(str(tmp_path / "v5.db"))
    _seed_v5(conn)
    real = migrations.MIGRATIONS[5]

    def rewrite_then_explode(c):
        real.fn(c)
        raise RuntimeError("boom")
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [*migrations.MIGRATIONS[:5],
                         migrations.Migration(6, "reservation_states", rewrite_then_explode)])
    with pytest.raises(RuntimeError, match="boom"):
        migrations.apply(conn)
    assert migrations.current_version(conn) == 5
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'reservations'").fetchone()[0]
    assert "'quarantined'" not in sql and "'tearing_down'" in sql
    assert conn.execute("SELECT COUNT(*) FROM reservations WHERE state = 'tearing_down'"
                        ).fetchone()[0] == 1
    conn.close()


# ---- held_by / busy lists -----------------------------------------------------

@pytest.mark.parametrize("state", STATES)
def test_held_by_matches_held_states(ctx, state):
    """A setup is held exactly while its reservation is in a HELD state — including
    `cleaning` and `quarantined`, which still occupy the hardware."""
    db, sid, _ = ctx
    rid = _res(ctx, state)
    held = models.held_by(db, sid)
    if state in models.HELD_STATES:
        assert held is not None and held["id"] == rid
    else:
        assert held is None


# ---- transition helper --------------------------------------------------------

@pytest.mark.parametrize("a,b", LEGAL)
def test_every_legal_edge_works(ctx, a, b):
    rid = _res(ctx, a)
    models.transition_reservation(ctx[0], rid, b)
    assert _state(ctx[0], rid) == b


@pytest.mark.parametrize("a,b", ILLEGAL)
def test_every_illegal_edge_raises_and_changes_nothing(ctx, a, b):
    rid = _res(ctx, a)
    before = tuple(models.reservation_by_id(ctx[0], rid))
    with pytest.raises(ReservationStateError):
        models.transition_reservation(ctx[0], rid, b)
    assert tuple(models.reservation_by_id(ctx[0], rid)) == before


def test_validation_errors(ctx):
    db = ctx[0]
    rid = _res(ctx)
    with pytest.raises(ReservationStateError, match="no reservation"):
        models.transition_reservation(db, 424242, "provisioning")
    with pytest.raises(ReservationStateError, match="unknown reservation state"):
        models.transition_reservation(db, rid, "tearing_down")
    with pytest.raises(ReservationStateError, match="failure_reason"):
        models.transition_reservation(db, rid, "provisioning", failure_reason="x")
    assert _state(db, rid) == "queued"


def test_failure_reason_is_stored_on_failed_and_kept_afterwards(ctx):
    db = ctx[0]
    rid = _res(ctx, "provisioning")
    models.transition_reservation(db, rid, "failed", failure_reason="deploy exited 2")
    assert models.reservation_by_id(db, rid)["failure_reason"] == "deploy exited 2"
    models.transition_reservation(db, rid, "released")
    row = models.reservation_by_id(db, rid)
    assert (row["state"], row["failure_reason"]) == ("released", "deploy exited 2")


def test_from_state_guard(ctx):
    db = ctx[0]
    rid = _res(ctx, "ready")
    with pytest.raises(ReservationStateError, match="expected state queued, found ready"):
        models.transition_reservation(db, rid, "active", from_state="queued")
    assert _state(db, rid) == "ready"
    models.transition_reservation(db, rid, "active", from_state="ready")
    assert _state(db, rid) == "active"


def test_updated_at_moves_and_other_columns_do_not(ctx):
    db = ctx[0]
    rid = _res(ctx, "ready")
    db.execute("UPDATE reservations SET updated_at = ? WHERE id = ?",
               ("2020-01-01T00:00:00Z", rid))        # models.update() would overwrite it
    assert models.reservation_by_id(db, rid)["updated_at"] == "2020-01-01T00:00:00Z"
    before = dict(models.reservation_by_id(db, rid))
    models.transition_reservation(db, rid, "cleaning")
    after = dict(models.reservation_by_id(db, rid))
    assert after["updated_at"] != "2020-01-01T00:00:00Z"
    assert {k: v for k, v in after.items() if k not in ("state", "updated_at")} == \
           {k: v for k, v in before.items() if k not in ("state", "updated_at")}


def test_lost_race_is_detected(ctx, monkeypatch):
    """Someone else moves the row after we read it: our UPDATE must not match."""
    db = ctx[0]
    rid = _res(ctx, "ready")
    stale = models.reservation_by_id(db, rid)                        # snapshot: ready
    models.transition_reservation(db, rid, "active")                # the other writer wins
    monkeypatch.setattr(models, "reservation_by_id", lambda conn, i: stale)
    with pytest.raises(ReservationStateError, match="changed concurrently"):
        models.transition_reservation(db, rid, "cleaning")          # legal on the snapshot
    monkeypatch.undo()
    assert _state(db, rid) == "active"


def test_random_walk_matches_the_machine(ctx):
    rng = random.Random(21)
    db = ctx[0]
    ids = [_res(ctx) for _ in range(30)]
    model = {i: "queued" for i in ids}
    visited = {"queued"}
    for _ in range(600):
        i = rng.choice(ids)
        legal = models.RESERVATION_TRANSITIONS[model[i]]
        # half the time aim at a legal move so the walk actually travels; otherwise anything
        target = rng.choice(legal) if legal and rng.random() < 0.5 else rng.choice(STATES)
        if target in models.RESERVATION_TRANSITIONS[model[i]]:
            models.transition_reservation(db, i, target)
            model[i] = target
            visited.add(target)
        else:
            with pytest.raises(ReservationStateError):
                models.transition_reservation(db, i, target)
        assert {i: _state(db, i) for i in ids} == model
    assert visited == set(STATES)                                   # the walk reached every state


# ---- API: cancel -----------------------------------------------------------------

def _api_res(api_env, state):
    conn = api_env.conn
    uid = conn.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()["id"]
    sid = models.setup_by_name(conn, "tb-01")["id"]
    cid = models.usecase_by_name(conn, "tls-proxy-nginx")["id"]
    return mk_res(conn, sid, cid, T(10), T(12), state=state, user_id=uid)


def _delete(api_env, rid):
    return api_env.client.delete(f"/reservations/{rid}",
                                 headers={"Authorization": f"Bearer {api_env.user_token}"})


@pytest.mark.parametrize("state,expected", [("queued", "released"), ("ready", "cleaning"),
                                            ("active", "cleaning")])
def test_cancel_moves_through_the_state_machine(api_env, state, expected):
    rid = _api_res(api_env, state)
    assert _delete(api_env, rid).json() == {"id": rid, "state": expected}
    assert _state(api_env.conn, rid) == expected
    assert api_env.conn.execute(
        "SELECT detail FROM audit_events WHERE action = 'cancel' AND object_id = ?",
        (rid,)).fetchone() is not None


@pytest.mark.parametrize("state", ["provisioning", "cleaning", "quarantined", "released", "failed"])
def test_cancel_in_any_other_state_is_a_409(api_env, state):
    rid = _api_res(api_env, state)
    r = _delete(api_env, rid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"
    assert r.json()["error"]["detail"] == {"state": state}
    assert _state(api_env.conn, rid) == state


def test_cancel_loses_a_race_with_the_engine_cleanly(api_env, monkeypatch):
    """The engine moves the reservation between the API's read and write: 409, not an overwrite."""
    from service import api
    rid = _api_res(api_env, "active")
    real_owned = api._owned_reservation

    def read_then_the_engine_moves(conn, res_id, user):
        row = real_owned(conn, res_id, user)                        # the API's read: active
        models.transition_reservation(conn, res_id, "cleaning")     # the engine's move, committed
        return row
    monkeypatch.setattr(api, "_owned_reservation", read_then_the_engine_moves)
    r = _delete(api_env, rid)
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "invalid_state",
                                 "message": "cannot cancel a reservation in state cleaning",
                                 "detail": {"state": "cleaning"}}
    assert _state(api_env.conn, rid) == "cleaning"
    assert models.jobs_for_reservation(api_env.conn, rid) == []    # ours rolled back whole
