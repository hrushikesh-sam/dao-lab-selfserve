"""B-109c: the snapshot lifecycle on the controller (the end-of-slot save).

Design v1.1 §6 "End of slot - save" steps 2-5 and 9; DDL v4.1 §3.5 (states, retention, D-7-4),
§4.1 (S on the controller), §4.3 (who writes which state); interface §5.6 / §5.8.

Files: <snapshot_root>/u<user_id>/<public_id>.age      age(gzip -1(tar)) sealed to S (from the DPU)
                                  <public_id>.key.age  S sealed to the owner's recipient
                                  <public_id>.hdr.json plaintext header (§5.8), no user data
       <snapshot_root>/u<user_id>/.incoming/<public_id>.age.part  the fetch target, then renamed
Every directory is 0700, every file 0600, every write is temp file + os.replace.

S (a fresh age identity per snapshot) exists only in this process: `wrap_new_identity` makes it
with `agecli.keygen`, seals it to the owner's active key with `agecli.encrypt_to` (stdin to age,
never argv) and drops it. Python can't zero memory, so `del` only drops the reference; S is
never written anywhere, never in a log, the DB, a job dir, extravars or the environment. Only
S's PUBLIC recipient goes to the DPU (snapshot_capture.yml).

States (DDL §3.5): capturing -> saved | failed; failed -> capturing on a retry (D-7-4: the same
row, a new public_id, created_at = the new capture); `capturing` whose job no longer runs ->
failed (`fail_stale_captures`, every tick). `saved` is committed before the apps cleanup and the
wipe; the wipe is allowed only for a `saved` row (with its three files) or a home the capture
found equal to skel (`wipe_allowed`). Every failure keeps the home: the caller raises home:lab
and quarantines.

Safe diagnostics only: audits and logs carry ids, public ids, states, counts and fixed reason
codes (REASONS). Never a path, a file name or key material.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass

from . import agecli, config, hostcheck, keys, models

log = logging.getLogger("daolab.snapshots")

FORMAT = "daolab-snapshot/1"
COMPRESSION = "gzip"
KEEP = 3                                   # saved, unheld, per (user, setup) (design:96; Q-16)
HEADROOM_MARGIN = 64 << 20                 # free >= 2 x cap + this before a capture (E-15 headroom)
PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPORT_KEYS = ("status", "reason", "raw_bytes", "age_bytes", "age_sha256")
STATUSES = ("saved", "empty", "refused")
# daolab-capture.py's closed list (B-109a)
SCRIPT_REASONS = ("ok", "empty_home", "path_unsafe", "home_missing", "home_not_dir", "home_owner",
                  "mount_under_home", "sysctl_unsafe", "tar_failed", "cap_exceeded", "age_failed",
                  "out_exists", "bad_args")
# what the controller side adds
CONTROLLER_REASONS = ("already_saved", "save_off", "no_key", "no_runner", "storage_low",
                      "storage_unsafe", "swap_unsafe", "age_error", "runner_error", "bad_report",
                      "size_mismatch", "sha_mismatch", "state_changed", "stale", "internal_error")
REASONS = SCRIPT_REASONS + CONTROLLER_REASONS


class SnapshotError(Exception):
    """A capture problem. str() is exactly one fixed code from REASONS."""

    def __init__(self, reason: str):
        assert reason in REASONS, reason
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Outcome:
    """What `save` did: status saved | empty | refused | failed | skipped, a fixed reason."""
    status: str
    reason: str
    snapshot_id: int | None = None
    public_id: str | None = None
    raw_bytes: int = 0


# ---- paths and files ----------------------------------------------------------------------

def _pid(public_id: str) -> str:
    if not isinstance(public_id, str) or not PUBLIC_ID_RE.fullmatch(public_id):
        raise ValueError("public_id must be 32 lowercase hex")
    return public_id


def user_dir(root: str, user_id: int) -> str:
    return os.path.join(root, f"u{int(user_id)}")


def paths(root: str, user_id: int, public_id: str) -> dict:
    """The three files of one snapshot (§5.6): {"age", "key", "hdr"}."""
    d, pid = user_dir(root, user_id), _pid(public_id)
    return {"age": os.path.join(d, f"{pid}.age"), "key": os.path.join(d, f"{pid}.key.age"),
            "hdr": os.path.join(d, f"{pid}.hdr.json")}


def incoming_path(root: str, user_id: int, public_id: str) -> str:
    """Where snapshot_capture.yml fetches the sealed file to, before the checks and the rename."""
    return os.path.join(user_dir(root, user_id), ".incoming", f"{_pid(public_id)}.age.part")


def ensure_dirs(root: str, user_id: int) -> None:
    """root, u<uid> and u<uid>/.incoming exist as real directories owned by us, mode 0700."""
    os.makedirs(root, mode=0o700, exist_ok=True)
    for d in (root, user_dir(root, user_id), os.path.join(user_dir(root, user_id), ".incoming")):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise SnapshotError("storage_unsafe")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)


def headroom_ok(root: str, cap_bytes: int) -> bool:
    return shutil.disk_usage(root).free >= 2 * int(cap_bytes) + HEADROOM_MARGIN


def write_private(path: str, data: bytes) -> None:
    """`data` into `path`, mode 0600, via a temp file in the same dir + os.replace."""
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def delete_snapshot_files(root: str, user_id: int, public_id: str) -> int:
    """Remove the three files of a snapshot; a missing one is fine. Returns how many went."""
    return sum(_unlink(p) for p in paths(root, user_id, public_id).values())


def delete_attempt_files(root: str, user_id: int, public_id: str) -> None:
    """Everything one capture attempt may have left: the three files and the fetch target."""
    delete_snapshot_files(root, user_id, public_id)
    _unlink(incoming_path(root, user_id, public_id))


# ---- S ------------------------------------------------------------------------------------

def wrap_new_identity(owner_recipient: str) -> tuple[bytes, str]:
    """A fresh S, sealed to the owner: returns (key.age bytes, S's public recipient). E-2: the
    swap check runs first, so S never exists on a controller whose memory could be paged to an
    unencrypted swap."""
    hostcheck.require_safe_swap()
    identity, s_recipient = agecli.keygen()
    try:
        key_age = agecli.encrypt_to(owner_recipient, identity)
    finally:
        del identity                       # drops the only reference; Python can't zero memory
    return key_age, s_recipient


# ---- the report of daolab-capture.py --------------------------------------------------------

def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 63


def check_report(doc) -> dict:
    """The capture script's JSON exactly as its contract says, else bad_report."""
    if not isinstance(doc, dict) or list(doc) != list(REPORT_KEYS):
        raise SnapshotError("bad_report")
    status, reason, sha = doc["status"], doc["reason"], doc["age_sha256"]
    if status not in STATUSES or reason not in SCRIPT_REASONS or not isinstance(sha, str):
        raise SnapshotError("bad_report")
    if not (_count(doc["raw_bytes"]) and _count(doc["age_bytes"])):
        raise SnapshotError("bad_report")
    if status == "saved":
        ok = reason == "ok" and doc["age_bytes"] > 0 and SHA256_RE.fullmatch(sha) is not None
    elif status == "empty":
        ok = reason == "empty_home"
    else:
        ok = reason not in ("ok", "empty_home")
    if not ok:
        raise SnapshotError("bad_report")
    return doc


def verify_fetched(path: str, doc: dict) -> None:
    """The fetched file is a regular file of the reported size and sha256."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise SnapshotError("size_mismatch") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != doc["age_bytes"]:
            raise SnapshotError("size_mismatch")
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            h.update(chunk)
    finally:
        os.close(fd)
    if h.hexdigest() != doc["age_sha256"]:
        raise SnapshotError("sha_mismatch")


def header(ctx, row, doc: dict, s_recipient: str) -> dict:
    """hdr.json (§5.8): plaintext, no user data. `profile` stays null until B-111."""
    return {"format": FORMAT, "compression": COMPRESSION, "public_id": row["public_id"],
            "user_id": row["user_id"], "setup": ctx.get("setup"), "hw_class": ctx.get("hw_class"),
            "usecase": ctx.get("usecase"), "created_at": row["created_at"],
            "raw_bytes": doc["raw_bytes"], "age_sha256": doc["age_sha256"],
            "s_recipient": s_recipient, "profile": None}


# ---- rows ---------------------------------------------------------------------------------

class _txn:
    """BEGIN IMMEDIATE ... COMMIT, or nothing when the caller already holds a transaction."""

    def __init__(self, conn):
        self.conn, self.own = conn, not conn.in_transaction

    def __enter__(self):
        if self.own:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.own:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def row_for_reservation(conn, reservation_id: int):
    return conn.execute("SELECT * FROM snapshots WHERE reservation_id = ?",
                        (reservation_id,)).fetchone()


def begin_capture(conn, res_row, key_row, job_id: int | None = None):
    """A `capturing` row for this reservation, in one transaction (§5.6). A failed (or a stale
    capturing) row of the same reservation is reused (D-7-4) with a new public_id and
    created_at; a saved one is never touched (already_saved). `job_id` links the cleanup job
    (jobs.snapshot_id, DDL §3.8)."""
    with _txn(conn):
        old = row_for_reservation(conn, res_row["id"])
        pid, now = secrets.token_hex(16), models.utcnow()
        if old is None:
            sid = models.insert(conn, "snapshots", {
                "public_id": pid, "user_id": res_row["user_id"], "setup_id": res_row["setup_id"],
                "reservation_id": res_row["id"], "key_id": key_row["id"], "state": "capturing"})
        elif old["state"] in ("failed", "capturing"):
            sid = old["id"]
            cur = conn.execute(
                """UPDATE snapshots SET state = 'capturing', public_id = ?, key_id = ?,
                          created_at = ?, updated_at = ?
                   WHERE id = ? AND state IN ('failed', 'capturing')""",
                (pid, key_row["id"], now, now, sid))
            if cur.rowcount != 1:
                raise SnapshotError("state_changed")
        else:
            raise SnapshotError("already_saved")
        if job_id is not None:
            models.update(conn, "jobs", job_id, {"snapshot_id": sid})
        return conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()


def _move(conn, snapshot_id: int, new_state: str) -> None:
    with _txn(conn):
        cur = conn.execute("UPDATE snapshots SET state = ?, updated_at = ? "
                           "WHERE id = ? AND state = 'capturing'",
                           (new_state, models.utcnow(), snapshot_id))
        if cur.rowcount != 1:
            raise SnapshotError("state_changed")


def mark_saved(conn, snapshot_id: int) -> None:
    """capturing -> saved (compare-and-set)."""
    _move(conn, snapshot_id, "saved")


def mark_failed(conn, snapshot_id: int) -> None:
    """capturing -> failed (compare-and-set)."""
    _move(conn, snapshot_id, "failed")


def audit_capture(conn, job, res_id: int, status: str, reason: str, snapshot_id=None,
                  public_id=None, raw_bytes: int = 0) -> None:
    models.audit(conn, "engine", "snapshot_capture", "reservation", res_id,
                 {"job": job["id"] if job is not None else None, "snapshot_id": snapshot_id,
                  "public_id": public_id, "status": status, "reason": reason,
                  "raw_bytes": raw_bytes},
                 trace_id=job["trace_id"] if job is not None else None)


def fail_stale_captures(conn, root: str) -> int:
    """`capturing` rows no running job works on -> failed, their attempt files removed (DDL §3.5,
    Q-15). A crashed engine's job is put back to `pending` by crash recovery first, so its row
    is failed here and the replayed job reuses it (D-7-4). Returns the count."""
    rows = conn.execute(
        """SELECT * FROM snapshots s WHERE s.state = 'capturing' AND NOT EXISTS (
               SELECT 1 FROM jobs j WHERE j.snapshot_id = s.id AND j.state = 'running')
           ORDER BY s.id""").fetchall()
    n = 0
    for row in rows:
        with _txn(conn):
            cur = conn.execute(
                """UPDATE snapshots SET state = 'failed', updated_at = ?
                   WHERE id = ? AND state = 'capturing' AND NOT EXISTS (
                       SELECT 1 FROM jobs j WHERE j.snapshot_id = snapshots.id
                       AND j.state = 'running')""", (models.utcnow(), row["id"]))
            if cur.rowcount != 1:
                continue
            delete_attempt_files(root, row["user_id"], row["public_id"])
            audit_capture(conn, None, row["reservation_id"], "failed", "stale", row["id"],
                          row["public_id"])
            n += 1
    if n:
        log.warning("event=snapshot_stale count=%d", n)
    return n


# ---- retention and deletion ---------------------------------------------------------------

def delete_snapshot(conn, snap_row, root: str) -> None:
    """Delete one snapshot (§5.6): its three files (missing is fine), then the row, then an audit
    event. Runs in the CALLER's transaction and never opens one (keys.execute_due_resets calls
    its callback inside its own). B-108a's snapshot_holds.delete_snapshot may replace it."""
    delete_snapshot_files(root, snap_row["user_id"], snap_row["public_id"])
    conn.execute("DELETE FROM snapshots WHERE id = ?", (snap_row["id"],))
    models.audit(conn, "engine", "snapshot_delete", "snapshot", snap_row["id"],
                 {"snapshot_id": snap_row["id"], "public_id": snap_row["public_id"],
                  "user_id": snap_row["user_id"], "setup_id": snap_row["setup_id"],
                  "state": snap_row["state"]})


def prune_retention(conn, user_id: int, setup_id: int, delete_snapshot) -> int:
    """Keep the newest KEEP unheld saved snapshots of (user, setup); delete the rest, oldest
    first (DDL v4.1 §3.5 query; Q-16: held rows are neither counted nor pruned). The callback is
    called as delete_snapshot(conn, row), like keys.execute_due_resets does; the engine binds
    snapshot_root. One transaction. Returns how many were deleted."""
    with _txn(conn):
        victims = conn.execute(
            """SELECT * FROM snapshots
               WHERE user_id = ? AND setup_id = ? AND state = 'saved' AND delete_after IS NULL
               ORDER BY created_at DESC, id DESC
               LIMIT -1 OFFSET ?""", (user_id, setup_id, KEEP)).fetchall()
        for row in reversed(victims):      # oldest first
            delete_snapshot(conn, row)
    return len(victims)


# ---- the save (engine cleanup / reconcile) -------------------------------------------------

def save(conn, rn, job, res, ctx, sink, settings=None) -> Outcome:
    """Design §6 save steps 2-5 for one reservation of a snapshot-enabled setup. Returns what
    happened; never raises an Exception (each failure is a fixed reason, the row `failed`, the
    attempt's files removed). A BaseException (a crash) propagates: crash recovery replays the
    job and `fail_stale_captures` fails the row first."""
    s = settings or config.settings
    root = os.path.abspath(s.snapshot_root)
    old = row_for_reservation(conn, res["id"])
    if old is not None and old["state"] == "saved":            # a replay after the commit
        return Outcome("saved", "already_saved", old["id"], old["public_id"])
    if res["save_snapshot"] != 1:                              # legacy / in-flight: PRESERVE
        return _skip(conn, job, res, "save_off")
    key = keys.active_key(conn, res["user_id"])
    if key is None:
        return _skip(conn, job, res, "no_key")
    capture = getattr(rn, "capture", None)
    if not callable(capture):
        with _txn(conn):
            audit_capture(conn, job, res["id"], "failed", "no_runner")
        return Outcome("failed", "no_runner")
    if old is not None:                                        # D-7-4: the old attempt's files go
        delete_attempt_files(root, res["user_id"], old["public_id"])
    try:
        row = begin_capture(conn, res, key, job_id=job["id"])
    except SnapshotError as e:                                 # changed under us: fail closed
        with _txn(conn):
            audit_capture(conn, job, res["id"], "failed", e.reason)
        return Outcome("failed", e.reason)
    sid, pid = row["id"], row["public_id"]
    p = paths(root, res["user_id"], pid)
    part = incoming_path(root, res["user_id"], pid)
    try:
        ensure_dirs(root, res["user_id"])
        if not headroom_ok(root, s.snapshot_cap_bytes):
            raise SnapshotError("storage_low")
        key_age, s_recipient = wrap_new_identity(key["recipient"])
        write_private(p["key"], key_age)
        try:
            result, report = capture(ctx, sink, recipient=s_recipient, public_id=pid,
                                     cap_bytes=int(s.snapshot_cap_bytes), fetch_to=part)
        except Exception:                                      # noqa: BLE001 — fail closed
            raise SnapshotError("runner_error") from None
        doc = check_report(report)
        if doc["status"] == "refused":
            return _fail(conn, job, res, root, sid, pid, "refused", doc["reason"])
        if doc["status"] == "empty":
            if not result.ok:
                raise SnapshotError("runner_error")
            with _txn(conn):
                delete_attempt_files(root, res["user_id"], pid)
                conn.execute("DELETE FROM snapshots WHERE id = ? AND state = 'capturing'", (sid,))
                audit_capture(conn, job, res["id"], "empty", "empty_home", sid, pid)
            return Outcome("empty", "empty_home", None, pid)
        if not result.ok:
            raise SnapshotError("runner_error")
        verify_fetched(part, doc)
        os.chmod(part, 0o600)
        os.replace(part, p["age"])
        write_private(p["hdr"], json.dumps(header(ctx, row, doc, s_recipient),
                                           sort_keys=False).encode())
        with _txn(conn):
            mark_saved(conn, sid)
            audit_capture(conn, job, res["id"], "saved", "ok", sid, pid, doc["raw_bytes"])
        return Outcome("saved", "ok", sid, pid, doc["raw_bytes"])
    except SnapshotError as e:
        return _fail(conn, job, res, root, sid, pid, "failed", e.reason)
    except agecli.AgeError:
        return _fail(conn, job, res, root, sid, pid, "failed", "age_error")
    except hostcheck.HostCheckError:
        return _fail(conn, job, res, root, sid, pid, "failed", "swap_unsafe")
    except Exception as e:                                     # noqa: BLE001 — fail closed
        log.error("snapshot capture of reservation %s failed: %s", res["id"], type(e).__name__)
        return _fail(conn, job, res, root, sid, pid, "failed", "internal_error")


def _skip(conn, job, res, reason: str) -> Outcome:
    with _txn(conn):
        audit_capture(conn, job, res["id"], "skipped", reason)
    return Outcome("skipped", reason)


def _fail(conn, job, res, root, sid, pid, status, reason) -> Outcome:
    delete_attempt_files(root, res["user_id"], pid)
    with _txn(conn):
        conn.execute("UPDATE snapshots SET state = 'failed', updated_at = ? "
                     "WHERE id = ? AND state = 'capturing'", (models.utcnow(), sid))
        audit_capture(conn, job, res["id"], status, reason, sid, pid)
    return Outcome(status, reason, sid, pid)


def wipe_allowed(conn, res, root: str, outcome: Outcome) -> bool:
    """The wipe gate (design §6 step 7), re-read from the DB: this reservation's row is `saved`,
    belongs to its owner and setup, and its three files exist; or this run found the home
    equal to skel."""
    if outcome.status == "empty":
        return True
    row = row_for_reservation(conn, res["id"])
    if row is None or row["state"] != "saved":
        return False
    if (row["user_id"], row["setup_id"]) != (res["user_id"], res["setup_id"]):
        return False
    for path in paths(os.path.abspath(root), row["user_id"], row["public_id"]).values():
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return False
        except OSError:
            return False
    return True
