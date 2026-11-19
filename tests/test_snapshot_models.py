"""B-104: migrations 12 (snapshots) + 13 (restore jobs) and the snapshot model functions."""
import sqlite3

import pytest

from service import migrations, models, snapshot
from service.models import SnapshotStateError

from .conftest import T, TB01_CAPS, mk_res, mk_setup, mk_usecase, mk_user


@pytest.fixture
def world(db):
    """alice and bob, the tb-01 setup with its DPU at sdp-pf1-vf8, a second setup, one use
    case. Returns a namespace with their ids and tokens."""
    alice, a_tok = mk_user(db, "alice")
    bob, b_tok = mk_user(db, "bob")
    sid = mk_setup(db, "tb-01", TB01_CAPS, dpu_hw_class="sdp-pf1-vf8")
    other = mk_setup(db, "tb-02", TB01_CAPS, dpu_hw_class="sdp-pf1-vf8")
    uc = mk_usecase(db, "tls-proxy-nginx", ["wire"], hw_class="sdp-pf1-vf8")
    return type("W", (), {"db": db, "alice": alice, "bob": bob, "a_tok": a_tok,
                          "b_tok": b_tok, "sid": sid, "other": other, "uc": uc})


def _res(w, user, state="active", setup=None):
    return mk_res(w.db, setup or w.sid, w.uc, T(10), T(11), state=state, user_id=user)


def _saved(w, res_id, path="/snap/x.snap"):
    snap_id = models.create_snapshot(w.db, res_id)
    models.mark_snapshot_saved(w.db, snap_id, path=path, size_bytes=10, sha256="a" * 64,
                               wrapped_dek="d", header={"files": 3})
    return snap_id


# ---- migrations ----------------------------------------------------------------------------

def test_latest_schema_has_the_snapshot_tables_and_columns(db):
    assert migrations.latest_version() == 13
    assert migrations.has_table(db, "user_keys") and migrations.has_table(db, "snapshots")
    assert migrations.has_column(db, "reservations", "save_snapshot")
    assert migrations.has_column(db, "setups", "snapshot_bdfs")
    assert migrations.has_column(db, "jobs", "snapshot_id")
    assert migrations.has_column(db, "jobs", "secret")


def test_save_snapshot_defaults_on_and_is_a_boolean(world):
    rid = _res(world, world.alice)
    assert models.reservation_by_id(world.db, rid)["save_snapshot"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        world.db.execute("UPDATE reservations SET save_snapshot = 2 WHERE id = ?", (rid,))


def test_snapshot_state_check(world):
    sid = models.create_snapshot(world.db, _res(world, world.alice))
    with pytest.raises(sqlite3.IntegrityError):
        world.db.execute("UPDATE snapshots SET state = 'bogus' WHERE id = ?", (sid,))


def test_jobs_accept_restore_and_still_reject_unknown_kinds(world):
    rid = _res(world, world.alice)
    world.db.execute("INSERT INTO jobs (reservation_id, kind, created_at, updated_at) "
                     "VALUES (?, 'restore', 'x', 'x')", (rid,))
    with pytest.raises(sqlite3.IntegrityError):
        world.db.execute("INSERT INTO jobs (reservation_id, kind, created_at, updated_at) "
                         "VALUES (?, 'reboot', 'x', 'x')", (rid,))


def test_migration_13_keeps_existing_jobs_and_their_foreign_keys():
    """A v11 DB with jobs in it upgrades without losing a row, and the jobs -> reservations
    foreign key still cascades (the CHECK rewrite copies nothing)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    keep = migrations.MIGRATIONS
    try:
        migrations.MIGRATIONS = keep[:11]
        migrations.apply(conn)
    finally:
        migrations.MIGRATIONS = keep
    assert migrations.current_version(conn) == 11
    uid = models.insert(conn, "users", {"username": "u", "token_hash": "h", "ssh_pubkey": "k"})
    sid = mk_setup(conn, "s", ["wire"])
    cid = mk_usecase(conn, "uc", ["wire"])
    rid = mk_res(conn, sid, cid, T(10), T(11), user_id=uid)
    jid = models.create_job(conn, rid, "cleanup")

    assert migrations.apply(conn) == [12, 13]
    assert models.job_by_id(conn, jid)["kind"] == "cleanup"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.execute("DELETE FROM reservations WHERE id = ?", (rid,))
    assert models.job_by_id(conn, jid) is None                  # ON DELETE CASCADE intact


def test_migration_13_fails_loudly_on_an_unexpected_table():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT CHECK (kind IN ('x')))")
    with pytest.raises(RuntimeError, match="migration 13"):
        migrations._m13_restore_jobs(conn)


def test_migrations_12_and_13_are_idempotent(db):
    before = [tuple(r) for r in db.execute("SELECT sql FROM sqlite_master ORDER BY name")]
    migrations._m12_snapshots(db)
    migrations._m13_restore_jobs(db)
    after = [tuple(r) for r in db.execute("SELECT sql FROM sqlite_master ORDER BY name")]
    assert before == after


# ---- job secret ----------------------------------------------------------------------------

@pytest.mark.parametrize("end", ["succeeded", "failed"])
def test_finishing_a_job_wipes_its_secret(world, end):
    rid = _res(world, world.alice)
    jid = models.create_job(world.db, rid, "deploy")
    world.db.execute("UPDATE jobs SET secret = 'dek' WHERE id = ?", (jid,))
    models.transition_job(world.db, jid, "running")
    assert models.job_by_id(world.db, jid)["secret"] == "dek"      # kept while it runs
    models.transition_job(world.db, jid, end, error="x" if end == "failed" else None)
    assert models.job_by_id(world.db, jid)["secret"] is None


def test_crash_recovery_keeps_the_secret_for_the_replay(world):
    rid = _res(world, world.alice)
    jid = models.create_job(world.db, rid, "deploy")
    world.db.execute("UPDATE jobs SET secret = 'dek' WHERE id = ?", (jid,))
    models.transition_job(world.db, jid, "running")
    assert models.requeue_job(world.db, models.job_by_id(world.db, jid))
    assert models.job_by_id(world.db, jid)["secret"] == "dek"


# ---- user keys -----------------------------------------------------------------------------

def test_no_key_until_first_use(world):
    assert models.user_key(world.db, world.alice) is None


def test_ensure_user_key_creates_once(world):
    k1 = models.ensure_user_key(world.db, world.alice, world.a_tok)
    k2 = models.ensure_user_key(world.db, world.alice, world.a_tok)
    assert k1 == k2
    assert world.db.execute("SELECT COUNT(*) FROM user_keys").fetchone()[0] == 1
    snapshot.unwrap_private_key(world.a_tok, k1)                     # the token opens it


def test_keys_are_per_user(world):
    ka = models.ensure_user_key(world.db, world.alice, world.a_tok)
    kb = models.ensure_user_key(world.db, world.bob, world.b_tok)
    assert ka.public_key != kb.public_key
    with pytest.raises(snapshot.SnapshotKeyError):
        snapshot.unwrap_private_key(world.b_tok, ka)


def test_token_is_never_stored(world):
    models.ensure_user_key(world.db, world.alice, world.a_tok)
    dump = "\n".join(world.db.iterdump())
    assert world.a_tok not in dump


def test_rewrap_is_compare_and_set(world):
    old = models.ensure_user_key(world.db, world.alice, world.a_tok)
    new = snapshot.rewrap_user_key(world.a_tok, "tok-alice-2", old)
    assert models.replace_user_key_wrapping(world.db, world.alice, old, new)
    assert not models.replace_user_key_wrapping(world.db, world.alice, old, new)   # stale
    stored = models.user_key(world.db, world.alice)
    snapshot.unwrap_private_key("tok-alice-2", stored)
    row = world.db.execute("SELECT key_version FROM user_keys").fetchone()
    assert row["key_version"] == 2


def test_rewrap_refuses_a_different_keypair(world):
    old = models.ensure_user_key(world.db, world.alice, world.a_tok)
    other = snapshot.new_user_key("tok-alice-2")
    with pytest.raises(SnapshotStateError):
        models.replace_user_key_wrapping(world.db, world.alice, old, other)


# ---- snapshots -----------------------------------------------------------------------------

def test_create_snapshot_stamps_owner_setup_usecase_and_hw_class(world):
    rid = _res(world, world.alice)
    snap = models.snapshot_by_id(world.db, models.create_snapshot(world.db, rid))
    assert (snap["user_id"], snap["setup_id"], snap["reservation_id"], snap["usecase_id"]) == \
        (world.alice, world.sid, rid, world.uc)
    assert snap["hw_class"] == "sdp-pf1-vf8" and snap["state"] == "saving"


def test_create_snapshot_for_a_missing_reservation(world):
    with pytest.raises(SnapshotStateError):
        models.create_snapshot(world.db, 999)


def test_context_binds_user_and_snapshot(world):
    snap = models.snapshot_by_id(world.db, models.create_snapshot(world.db, _res(world, world.alice)))
    assert models.snapshot_context(snap) == f"user:{world.alice}|snapshot:{snap['id']}"


def test_saved_records_the_seal(world):
    sid = _saved(world, _res(world, world.alice))
    snap = models.snapshot_by_id(world.db, sid)
    assert snap["state"] == "saved" and snap["path"] == "/snap/x.snap"
    assert models.from_json(snap["header"]) == {"files": 3}


def test_failed_records_a_reason(world):
    sid = models.create_snapshot(world.db, _res(world, world.alice))
    models.mark_snapshot_failed(world.db, sid, "over the 512 MB cap")
    snap = models.snapshot_by_id(world.db, sid)
    assert snap["state"] == "failed" and snap["failure_reason"] == "over the 512 MB cap"


@pytest.mark.parametrize("first,then", [("saved", "failed"), ("failed", "saved"),
                                        ("saved", "saved")])
def test_illegal_snapshot_moves(world, first, then):
    sid = models.create_snapshot(world.db, _res(world, world.alice))
    move = {"saved": lambda: models.mark_snapshot_saved(
                world.db, sid, path="p", size_bytes=1, sha256="s", wrapped_dek="d"),
            "failed": lambda: models.mark_snapshot_failed(world.db, sid, "x")}
    move[first]()
    with pytest.raises(SnapshotStateError):
        move[then]()


def test_delete_forgets_the_data_key_and_returns_the_path(world):
    sid = _saved(world, _res(world, world.alice), path="/snap/7.snap")
    assert models.mark_snapshot_deleted(world.db, sid) == "/snap/7.snap"
    snap = models.snapshot_by_id(world.db, sid)
    assert snap["state"] == "deleted" and snap["wrapped_dek"] is None
    with pytest.raises(SnapshotStateError):
        models.mark_snapshot_deleted(world.db, sid)


def test_a_saving_snapshot_cannot_be_deleted(world):
    sid = models.create_snapshot(world.db, _res(world, world.alice))
    with pytest.raises(SnapshotStateError):
        models.mark_snapshot_deleted(world.db, sid)


def test_snapshots_for_lists_own_newest_first_and_hides_deleted(world):
    a1 = _saved(world, _res(world, world.alice, "released"))
    a2 = _saved(world, _res(world, world.alice, "released"))
    a3 = _saved(world, _res(world, world.alice, "released", setup=world.other))
    _saved(world, _res(world, world.bob, "released"))
    models.mark_snapshot_deleted(world.db, a1)
    assert [s["id"] for s in models.snapshots_for(world.db, world.alice)] == [a3, a2]
    assert [s["id"] for s in models.snapshots_for(world.db, world.alice, world.sid)] == [a2]


def test_prune_keeps_the_newest_per_user_and_setup(world):
    ids = [_saved(world, _res(world, world.alice, "released"), path=f"/snap/{i}.snap")
           for i in range(5)]
    other_setup = _saved(world, _res(world, world.alice, "released", setup=world.other))
    bobs = _saved(world, _res(world, world.bob, "released"))
    failed = models.create_snapshot(world.db, _res(world, world.alice, "released"))
    models.mark_snapshot_failed(world.db, failed, "x")

    paths = models.prune_snapshots(world.db, world.alice, world.sid, keep=3)
    assert sorted(paths) == ["/snap/0.snap", "/snap/1.snap"]
    state = {i: models.snapshot_by_id(world.db, i)["state"] for i in [*ids, other_setup, bobs, failed]}
    assert [state[i] for i in ids] == ["deleted", "deleted", "saved", "saved", "saved"]
    assert state[other_setup] == state[bobs] == "saved" and state[failed] == "failed"
    assert models.prune_snapshots(world.db, world.alice, world.sid, keep=3) == []


def test_prune_needs_keep_of_at_least_one(world):
    with pytest.raises(SnapshotStateError):
        models.prune_snapshots(world.db, world.alice, world.sid, keep=0)


def test_snapshot_for_reservation_is_the_newest(world):
    rid = _res(world, world.alice, "released")
    models.create_snapshot(world.db, rid)
    newer = models.create_snapshot(world.db, rid)
    assert models.snapshot_for_reservation(world.db, rid)["id"] == newer
    assert models.snapshot_for_reservation(world.db, _res(world, world.bob)) is None


def test_snapshot_bdfs_default_empty_and_read_back(world):
    assert models.snapshot_bdfs(world.db, world.sid) == []
    world.db.execute("UPDATE setups SET snapshot_bdfs = ? WHERE id = ?",
                     (models.to_json(["0002:01:00.1"]), world.sid))
    assert models.snapshot_bdfs(world.db, world.sid) == ["0002:01:00.1"]


# ---- restore_refusal -----------------------------------------------------------------------

def test_restore_allowed_for_own_saved_snapshot_in_own_live_slot(world):
    snap = models.snapshot_by_id(world.db, _saved(world, _res(world, world.alice, "released")))
    for state in ("ready", "active"):
        res = models.reservation_by_id(world.db, _res(world, world.alice, state))
        assert models.restore_refusal(snap, res, world.alice) is None


def test_restore_refusals(world):
    db = world.db
    snap = models.snapshot_by_id(db, _saved(world, _res(world, world.alice, "released")))
    mine = models.reservation_by_id(db, _res(world, world.alice, "active"))
    bobs = models.reservation_by_id(db, _res(world, world.bob, "active"))
    elsewhere = models.reservation_by_id(db, _res(world, world.alice, "active", setup=world.other))
    queued = models.reservation_by_id(db, _res(world, world.alice, "queued"))
    failed = models.create_snapshot(db, _res(world, world.alice, "released"))
    models.mark_snapshot_failed(db, failed, "x")

    assert models.restore_refusal(snap, mine, world.bob) == "no such snapshot"
    assert models.restore_refusal(None, mine, world.alice) == "no such snapshot"
    assert models.restore_refusal(snap, bobs, world.alice) == "no such reservation"
    assert models.restore_refusal(snap, elsewhere, world.alice) == "snapshot was taken on another setup"
    assert models.restore_refusal(snap, queued, world.alice) == "reservation is queued"
    assert models.restore_refusal(models.snapshot_by_id(db, failed), mine, world.alice) == \
        "snapshot is failed"
