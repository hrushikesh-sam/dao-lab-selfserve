"""B-20: the jobs entity — migration v5 + model helpers (plan/09 §6, §11).

A job has its own lifecycle, pending -> running -> succeeded | failed, and nothing else.
"""
import random
import sqlite3
import threading

import pytest

from service import migrations, models
from service.models import JobError
from tests.conftest import T, mk_res, mk_setup, mk_usecase


@pytest.fixture
def res(db):
    sid = mk_setup(db, "s1", ["wire"])
    cid = mk_usecase(db, "uc1", ["wire"])
    return mk_res(db, sid, cid, T(10), T(12))


def _running(db, res):
    jid = models.create_job(db, res, "deploy")
    models.transition_job(db, jid, "running")
    return jid


# ---- migration --------------------------------------------------------------

def test_migration_creates_jobs_table(db):
    assert migrations.has_table(db, "jobs")
    assert migrations.current_version(db) >= 5
    cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    assert cols == {"id", "reservation_id", "kind", "state", "attempt", "trace_id",
                    "log_path", "steps", "error", "started_at", "finished_at",
                    "created_at", "updated_at"}


def test_upgrade_from_v4_keeps_rows_and_is_idempotent(tmp_path):
    """A DB that stopped at v4 gains the jobs table; existing data survives; init twice = no-op."""
    path = str(tmp_path / "old.db")
    conn = models.connect(path)
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    for m in migrations.MIGRATIONS:
        if m.version <= 4:
            m.fn(conn)
    conn.execute("PRAGMA user_version = 4")
    models.insert(conn, "users", {"username": "keepme", "token_hash": "h", "ssh_pubkey": "k"})
    assert not migrations.has_table(conn, "jobs")
    assert models.init_db(conn) == [5]
    assert migrations.has_table(conn, "jobs")
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "keepme"
    assert models.init_db(conn) == []
    conn.close()


# ---- create / read ----------------------------------------------------------

def test_create_job_defaults(db, res):
    jid = models.create_job(db, res, "deploy")
    j = models.job_by_id(db, jid)
    assert (j["reservation_id"], j["kind"], j["state"], j["attempt"]) == (res, "deploy", "pending", 1)
    assert j["trace_id"] is None and j["log_path"] is None and j["error"] is None
    assert j["started_at"] is None and j["finished_at"] is None
    assert j["created_at"] and j["updated_at"]
    assert models.job_steps(j) == []


def test_create_job_with_trace_log_and_attempt(db, res):
    jid = models.create_job(db, res, "cleanup", attempt=3, trace_id="abc123",
                            log_path="/var/log/daolab/job-7.log")
    j = models.job_by_id(db, jid)
    assert (j["attempt"], j["trace_id"], j["log_path"]) == (3, "abc123", "/var/log/daolab/job-7.log")


@pytest.mark.parametrize("kind", models.JOB_KINDS)
def test_every_kind_is_accepted(db, res, kind):
    assert models.job_by_id(db, models.create_job(db, res, kind))["kind"] == kind


def test_bad_kind_and_attempt_rejected(db, res):
    with pytest.raises(JobError):
        models.create_job(db, res, "reboot")
    with pytest.raises(JobError):
        models.create_job(db, res, "deploy", attempt=0)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_database_enforces_the_same_rules(db, res):
    """The CHECKs and the FK are the backstop if someone bypasses the helper."""
    for row in ({"kind": "reboot"}, {"kind": "deploy", "state": "paused"},
                {"kind": "deploy", "attempt": 0}):
        with pytest.raises(sqlite3.IntegrityError):
            models.insert(db, "jobs", {"reservation_id": res, **row})
    with pytest.raises(sqlite3.IntegrityError):                     # unknown reservation
        models.create_job(db, 999999, "deploy")


def test_deleting_a_reservation_deletes_its_jobs(db, res):
    models.create_job(db, res, "deploy")
    models.create_job(db, res, "cleanup")
    db.execute("DELETE FROM reservations WHERE id = ?", (res,))
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_job_by_id_unknown_is_none(db):
    assert models.job_by_id(db, 12345) is None


def test_jobs_for_reservation_order_and_kind_filter(db, res):
    a = models.create_job(db, res, "deploy")
    b = models.create_job(db, res, "health_probe")
    c = models.create_job(db, res, "deploy", attempt=2)
    sid = mk_setup(db, "s2", ["wire"])
    other = mk_res(db, sid, mk_usecase(db, "uc2", ["wire"]), T(14), T(15))
    models.create_job(db, other, "deploy")
    assert [j["id"] for j in models.jobs_for_reservation(db, res)] == [a, b, c]
    assert [j["id"] for j in models.jobs_for_reservation(db, res, "deploy")] == [a, c]
    assert models.jobs_for_reservation(db, res, "reconcile") == []


def test_pending_jobs_fifo_and_only_pending(db, res):
    ids = [models.create_job(db, res, "deploy") for _ in range(4)]
    models.transition_job(db, ids[1], "running")                    # no longer pending
    assert [j["id"] for j in models.pending_jobs(db)] == [ids[0], ids[2], ids[3]]
    assert [j["id"] for j in models.pending_jobs(db, limit=2)] == [ids[0], ids[2]]


def test_mutating_kinds_exclude_only_the_probe():
    assert set(models.MUTATING_JOB_KINDS) == set(models.JOB_KINDS) - {"health_probe"}


# ---- lifecycle --------------------------------------------------------------

def test_happy_path_stamps_times(db, res):
    jid = models.create_job(db, res, "deploy")
    models.transition_job(db, jid, "running")
    j = models.job_by_id(db, jid)
    assert j["state"] == "running" and j["started_at"] and j["finished_at"] is None
    models.transition_job(db, jid, "succeeded")
    j = models.job_by_id(db, jid)
    assert j["state"] == "succeeded" and j["finished_at"] and j["error"] is None


def test_started_at_survives_finishing(db, res):
    """started_at is written once, on `running`; finishing must not touch it (and must set
    finished_at). Both stamps land in the same second, so plant a sentinel to tell them apart."""
    jid = _running(db, res)
    old = "2020-01-01T00:00:00Z"
    models.update(db, "jobs", jid, {"started_at": old})
    models.transition_job(db, jid, "succeeded")
    j = models.job_by_id(db, jid)
    assert j["started_at"] == old
    assert j["finished_at"] and j["finished_at"] != old


def test_failed_records_error(db, res):
    jid = _running(db, res)
    models.transition_job(db, jid, "failed", error="playbook exited 2")
    j = models.job_by_id(db, jid)
    assert (j["state"], j["error"]) == ("failed", "playbook exited 2")
    assert j["finished_at"]


def test_failed_without_error_text_is_allowed(db, res):
    jid = _running(db, res)
    models.transition_job(db, jid, "failed")
    assert models.job_by_id(db, jid)["error"] is None


ALL_PAIRS = [(a, b) for a in models.JOB_STATES for b in models.JOB_STATES]
ILLEGAL = [(a, b) for a, b in ALL_PAIRS if b not in models.JOB_TRANSITIONS[a]]


def _job_in_state(db, res, state):
    jid = models.create_job(db, res, "deploy")
    if state in ("running", "succeeded", "failed"):
        models.transition_job(db, jid, "running")
    if state in ("succeeded", "failed"):
        models.transition_job(db, jid, state)
    assert models.job_by_id(db, jid)["state"] == state
    return jid


@pytest.mark.parametrize("a,b", ILLEGAL)
def test_every_illegal_transition_raises_and_changes_nothing(db, res, a, b):
    jid = _job_in_state(db, res, a)
    before = tuple(models.job_by_id(db, jid))
    with pytest.raises(JobError):
        models.transition_job(db, jid, b)
    assert tuple(models.job_by_id(db, jid)) == before


def test_exactly_three_legal_transitions():
    legal = [(a, b) for a, b in ALL_PAIRS if b in models.JOB_TRANSITIONS[a]]
    assert legal == [("pending", "running"), ("running", "succeeded"), ("running", "failed")]


def test_transition_validation_errors(db, res):
    jid = models.create_job(db, res, "deploy")
    with pytest.raises(JobError, match="no job"):
        models.transition_job(db, 424242, "running")
    with pytest.raises(JobError, match="unknown job state"):
        models.transition_job(db, jid, "paused")
    with pytest.raises(JobError, match="only recorded for a failed"):
        models.transition_job(db, jid, "running", error="nope")
    assert models.job_by_id(db, jid)["state"] == "pending"


def test_lost_race_is_detected(db, res, monkeypatch):
    """Another writer moves the job after we read it: our UPDATE must not match."""
    jid = models.create_job(db, res, "deploy")
    stale = models.job_by_id(db, jid)                               # snapshot: pending
    models.transition_job(db, jid, "running")                      # the other writer wins
    monkeypatch.setattr(models, "job_by_id", lambda conn, i: stale)
    with pytest.raises(JobError, match="changed concurrently"):
        models.transition_job(db, jid, "running")                  # would have been legal on the snapshot
    monkeypatch.undo()
    assert models.job_by_id(db, jid)["state"] == "running"


def test_random_walk_matches_the_state_machine(db, res):
    """Seeded: 300 random transition attempts on a pool of jobs; the helper accepts exactly
    the moves JOB_TRANSITIONS lists and the stored state always equals our own model."""
    rng = random.Random(20)
    ids = [models.create_job(db, res, rng.choice(models.JOB_KINDS)) for _ in range(6)]
    model = {i: "pending" for i in ids}
    accepted = 0
    for _ in range(300):
        i, target = rng.choice(ids), rng.choice(models.JOB_STATES)
        if target in models.JOB_TRANSITIONS[model[i]]:
            models.transition_job(db, i, target)
            model[i] = target
            accepted += 1
        else:
            with pytest.raises(JobError):
                models.transition_job(db, i, target)
        assert {j["id"]: j["state"] for j in db.execute("SELECT * FROM jobs")} == model
    assert accepted >= 6                                             # the walk actually moved


# ---- steps ------------------------------------------------------------------

def test_steps_append_in_order_with_shape(db, res):
    jid = _running(db, res)
    s1 = models.append_job_step(db, jid, "lease_ip")
    s2 = models.append_job_step(db, jid, "create_vm", detail="daolab-r1-backend")
    s3 = models.append_job_step(db, jid, "verify", state="failed", detail="ssh timeout")
    s4 = models.append_job_step(db, jid, "cleanup_extra", state="skipped")
    steps = models.job_steps(models.job_by_id(db, jid))
    assert steps == [s1, s2, s3, s4]
    assert [s["name"] for s in steps] == ["lease_ip", "create_vm", "verify", "cleanup_extra"]
    assert [s["state"] for s in steps] == ["ok", "ok", "failed", "skipped"]
    assert "detail" not in s1 and s2["detail"] == "daolab-r1-backend"
    assert all(s["at"] for s in steps)


def test_steps_are_refused_unless_running(db, res):
    pending = models.create_job(db, res, "deploy")
    with pytest.raises(JobError, match="while pending"):
        models.append_job_step(db, pending, "too_early")
    done = _running(db, res)
    models.transition_job(db, done, "succeeded")
    with pytest.raises(JobError, match="while succeeded"):
        models.append_job_step(db, done, "too_late")
    failed = _running(db, res)
    models.transition_job(db, failed, "failed", error="x")
    with pytest.raises(JobError, match="while failed"):
        models.append_job_step(db, failed, "too_late")
    assert models.job_steps(models.job_by_id(db, done)) == []


def test_step_validation(db, res):
    jid = _running(db, res)
    with pytest.raises(JobError, match="unknown step state"):
        models.append_job_step(db, jid, "x", state="maybe")
    with pytest.raises(JobError, match="no job"):
        models.append_job_step(db, 424242, "x")
    assert models.job_steps(models.job_by_id(db, jid)) == []


def test_step_append_survives_a_stale_read(db, res, monkeypatch):
    """A writer slips a step in between our read and our write: we retry, losing neither."""
    jid = _running(db, res)
    models.append_job_step(db, jid, "first")
    real = models.job_by_id
    calls = {"n": 0}

    def racy(conn, i):
        row = real(conn, i)
        calls["n"] += 1
        if calls["n"] == 1:                                           # after our read, before our write
            monkeypatch.setattr(models, "job_by_id", real)
            models.append_job_step(conn, i, "sneaked_in")
        return row
    monkeypatch.setattr(models, "job_by_id", racy)
    models.append_job_step(db, jid, "mine")
    names = [s["name"] for s in models.job_steps(real(db, jid))]
    assert names == ["first", "sneaked_in", "mine"]


def test_step_is_refused_if_the_job_finishes_mid_append(db, res, monkeypatch):
    """The job succeeds after we read it as running but before our write. Finishing does not
    change `steps`, so only the `state = 'running'` guard on the UPDATE stops a late step."""
    jid = _running(db, res)
    real = models.job_by_id
    stale = real(db, jid)                                            # snapshot: running
    models.transition_job(db, jid, "succeeded")                      # the job finishes
    calls = {"n": 0}

    def once_stale(conn, i):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(conn, i)
    monkeypatch.setattr(models, "job_by_id", once_stale)
    with pytest.raises(JobError, match="while succeeded"):
        models.append_job_step(db, jid, "late")
    monkeypatch.undo()
    assert models.job_steps(real(db, jid)) == []


def test_concurrent_step_appends_lose_nothing(tmp_path):
    """8 threads (own connection each) append 10 steps to one running job: all 80 survive."""
    path = str(tmp_path / "jobs.db")
    conn = models.connect(path)
    models.init_db(conn)
    models.insert(conn, "users", {"username": "u", "token_hash": "h", "ssh_pubkey": "k"})
    sid = mk_setup(conn, "s1", ["wire"])
    rid = mk_res(conn, sid, mk_usecase(conn, "uc1", ["wire"]), T(10), T(12))
    jid = models.create_job(conn, rid, "deploy")
    models.transition_job(conn, jid, "running")
    errors = []

    def worker(w):
        c = models.connect(path)
        try:
            for n in range(10):
                models.append_job_step(c, jid, f"w{w}-s{n}")
        except Exception as e:                                        # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    names = [s["name"] for s in models.job_steps(models.job_by_id(conn, jid))]
    assert len(names) == 80 and len(set(names)) == 80
    for w in range(8):                                                # each worker's own order kept
        mine = [n for n in names if n.startswith(f"w{w}-")]
        assert mine == [f"w{w}-s{n}" for n in range(10)]
    conn.close()
