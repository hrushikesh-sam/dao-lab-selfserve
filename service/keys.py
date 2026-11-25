"""B-105: public age recipients only; the private identity never reaches this module."""
import hashlib
import sqlite3

from . import agecli, models


class KeyServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409,
                 detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}


def fingerprint(recipient: str) -> str:
    return hashlib.sha256(recipient.encode("ascii")).hexdigest()


def _require_admin(admin_row) -> None:
    if admin_row is None or admin_row["role"] != "admin":
        raise KeyServiceError("forbidden", "admin role required", 403)


def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")


def _commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def _rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


def enroll(conn: sqlite3.Connection, user_id: int, recipient: str) -> int:
    if not agecli.RECIPIENT_RE.fullmatch(recipient):
        raise KeyServiceError("validation_error", "recipient: invalid age recipient", 422)
    _transaction(conn)
    try:
        if models.user_by_id(conn, user_id) is None:
            raise KeyServiceError("not_found", "unknown user", 404, {"user_id": user_id})
        if conn.execute("SELECT 1 FROM keys WHERE recipient = ?", (recipient,)).fetchone():
            raise KeyServiceError("key_exists", "recipient is already enrolled")
        conn.execute("DELETE FROM keys WHERE user_id = ? AND state = 'pending'", (user_id,))
        value = fingerprint(recipient)
        key_id = conn.execute(
            """INSERT INTO keys (user_id, recipient, fingerprint, state, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (user_id, recipient, value, models.utcnow(), models.utcnow())).lastrowid
        actor = models.user_by_id(conn, user_id)["username"]
        models.audit(conn, actor, "key_enroll", "key", key_id,
                     {"key_id": key_id, "user_id": user_id, "fingerprint": value, "state": "pending"})
        _commit(conn)
    except BaseException:
        _rollback(conn)
        raise
    return key_id


def activate(conn: sqlite3.Connection, key_id: int, admin_row) -> None:
    _require_admin(admin_row)
    _transaction(conn)
    try:
        row = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise KeyServiceError("not_found", "unknown key", 404, {"key_id": key_id})
        if row["user_id"] == admin_row["id"]:
            raise KeyServiceError("invalid_state", "an admin cannot activate their own key")
        if row["state"] != "pending":
            raise KeyServiceError("invalid_state", "key is not pending")
        if conn.execute("SELECT 1 FROM keys WHERE user_id = ? AND state = 'active'",
                        (row["user_id"],)).fetchone():
            raise KeyServiceError("active_key_exists", "user already has an active key")
        now = models.utcnow()
        conn.execute(
            """UPDATE keys SET state = 'active', activated_by = ?, activated_at = ?, updated_at = ?
               WHERE id = ? AND state = 'pending'""",
            (admin_row["id"], now, now, key_id))
        models.audit(conn, admin_row["username"], "key_activate", "key", key_id,
                     {"key_id": key_id, "user_id": row["user_id"], "fingerprint": row["fingerprint"],
                      "state": "active", "activated_by": admin_row["id"]})
        _commit(conn)
    except BaseException:
        _rollback(conn)
        raise


def active_key(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        """SELECT id, user_id, recipient, fingerprint FROM keys
           WHERE user_id = ? AND state = 'active'""", (user_id,)).fetchone()


def _open_resets(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT id, target_ref FROM admin_approvals
           WHERE action = 'lost_key_reset'
             AND (state = 'pending' OR (state = 'approved' AND executed_at IS NULL))""").fetchall()


def _target_key_id(target_ref: str) -> int | None:
    if not target_ref.startswith("key:"):
        return None
    suffix = target_ref[4:]
    return int(suffix) if suffix.isdigit() else None


def reset_open_for_user(conn: sqlite3.Connection, user_id: int) -> bool:
    for row in _open_resets(conn):
        key_id = _target_key_id(row["target_ref"])
        if key_id is None:
            continue
        if conn.execute("SELECT 1 FROM keys WHERE id = ? AND user_id = ?",
                        (key_id, user_id)).fetchone():
            return True
    return False


def execute_due_resets(conn: sqlite3.Connection, now: str, delete_snapshot) -> int:
    executed = 0
    _transaction(conn)
    try:
        due = conn.execute(
            """SELECT * FROM admin_approvals
               WHERE action = 'lost_key_reset' AND state = 'approved' AND executed_at IS NULL
                 AND (execute_after IS NULL OR execute_after <= ?)
               ORDER BY id""", (now,)).fetchall()
        for row in due:
            cas = conn.execute(
                """UPDATE admin_approvals SET executed_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'approved' AND executed_at IS NULL
                     AND (execute_after IS NULL OR execute_after <= ?)""",
                (now, now, row["id"], now))
            if cas.rowcount != 1:
                continue
            executed += 1
            key_id = _target_key_id(row["target_ref"])
            key = conn.execute("SELECT state, fingerprint FROM keys WHERE id = ?",
                               (key_id,)).fetchone() if key_id is not None else None
            revoked = conn.execute(
                "UPDATE keys SET state = 'revoked', updated_at = ? WHERE id = ? AND state = 'active'",
                (now, key_id)).rowcount if key_id is not None else 0
            if not revoked:
                models.audit(conn, "engine", "key_reset_noop", "approval", row["id"], {
                    "approval_id": row["id"], "key_id": key_id,
                    "key_state": key["state"] if key else "missing",
                    "fingerprint": key["fingerprint"] if key else None})
                continue
            snapshots = conn.execute("SELECT * FROM snapshots WHERE key_id = ? ORDER BY id",
                                     (key_id,)).fetchall()
            for snap in snapshots:
                delete_snapshot(conn, snap)
            models.audit(conn, "engine", "key_reset_execute", "approval", row["id"], {
                "approval_id": row["id"], "key_id": key_id,
                "fingerprint": key["fingerprint"] if key else None,
                "state": "revoked", "snapshots": len(snapshots)})
        _commit(conn)
    except BaseException:
        _rollback(conn)
        raise
    return executed
