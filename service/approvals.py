"""B-105: generic two-admin approvals and the lost-key reset hold effect."""
import re
import sqlite3
from datetime import timedelta

from . import keys, models

ACTIONS = ("lost_key_reset", "home_discard", "tripwire_accept", "tripwire_rebaseline")
HOLD_ACTIONS = frozenset({"lost_key_reset", "home_discard"})
TARGET_RE = re.compile(r"^[a-z0-9._:-]{1,128}$")
SECRET_SHAPED_RE = re.compile(r"^age-secret-key-", re.IGNORECASE)
HOLD_HOURS = 24


class ApprovalServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409,
                 detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}


def _require_admin(admin_row) -> None:
    if admin_row is None or admin_row["role"] != "admin":
        raise ApprovalServiceError("forbidden", "admin role required", 403)


def _require_non_owner(admin_row, key) -> None:
    if admin_row["id"] == key["user_id"]:
        raise ApprovalServiceError("forbidden", "the key owner cannot approve a reset", 403)


def _target_key_id(target_ref: str) -> int | None:
    if not target_ref.startswith("key:"):
        return None
    suffix = target_ref[4:]
    return int(suffix) if suffix.isdigit() else None


def _execute_after(action: str, now: str) -> str | None:
    if action not in HOLD_ACTIONS:
        return None
    return models.format_ts(models.parse_ts(now) + timedelta(hours=HOLD_HOURS))


def _lost_key_reset_effect(conn: sqlite3.Connection, approval: dict, now: str) -> None:
    key_id = _target_key_id(approval["target_ref"])
    if key_id is None:
        return
    conn.execute(
        """UPDATE snapshots SET delete_after = ?, delete_requested_by = ?, hold_approval_id = ?,
                                updated_at = ?
           WHERE key_id = ? AND state = 'saved' AND delete_after IS NULL""",
        (approval["execute_after"], approval["requested_by"], approval["id"], now, key_id))


EFFECTS = {"lost_key_reset": _lost_key_reset_effect}


def register_effect(action: str, effect) -> None:
    if action not in ACTIONS:
        raise ApprovalServiceError("validation_error", "unknown approval action", 422)
    EFFECTS[action] = effect


def request(conn: sqlite3.Connection, action: str, target_ref: str, admin_row) -> int:
    _require_admin(admin_row)
    if action not in ACTIONS:
        raise ApprovalServiceError("validation_error", "unknown approval action", 422)
    if not TARGET_RE.fullmatch(target_ref) or SECRET_SHAPED_RE.match(target_ref):
        raise ApprovalServiceError("validation_error", "target_ref: invalid", 422)
    conn.execute("BEGIN IMMEDIATE")
    try:
        open_row = conn.execute(
            """SELECT 1 FROM admin_approvals
               WHERE action = ? AND target_ref = ?
                 AND (state = 'pending' OR (state = 'approved' AND executed_at IS NULL))""",
            (action, target_ref)).fetchone()
        if open_row:
            raise ApprovalServiceError("approval_open", "an approval is already open for this target")

        detail = {"action": action, "requested_by": admin_row["id"], "state": "pending"}
        if action == "lost_key_reset":
            key_id = _target_key_id(target_ref)
            if key_id is None:
                raise ApprovalServiceError("validation_error", "target_ref: expected key:<id>", 422)
            key = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
            if key is None:
                raise ApprovalServiceError("not_found", "unknown key", 404, {"key_id": key_id})
            if key["state"] != "active":
                raise ApprovalServiceError("invalid_state", "key is not active")
            _require_non_owner(admin_row, key)
            if keys.reset_open_for_user(conn, key["user_id"]):
                raise ApprovalServiceError("approval_open", "a reset is already open for this user")
            detail.update(key_id=key_id, fingerprint=key["fingerprint"])
        elif action == "home_discard":
            setup = models.setup_by_name(conn, target_ref[len("setup:"):]) if target_ref.startswith("setup:") else None
            if setup is None:
                raise ApprovalServiceError("not_found", "unknown setup", 404)
            detail["setup_id"] = setup["id"]

        approval_id = models.insert(conn, "admin_approvals", {
            "action": action, "target_ref": target_ref, "requested_by": admin_row["id"],
            "state": "pending"})
        detail["approval_id"] = approval_id
        models.audit(conn, admin_row["username"], "approval_request", "approval", approval_id, detail)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ApprovalServiceError("approval_open", "an approval is already open for this target") from exc
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return approval_id


def decide(conn: sqlite3.Connection, approval_id: int, admin_row, approve: bool, now: str) -> None:
    _require_admin(admin_row)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM admin_approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalServiceError("not_found", "unknown approval", 404,
                                       {"approval_id": approval_id})
        if row["requested_by"] == admin_row["id"]:
            raise ApprovalServiceError("invalid_state", "a different admin must decide")
        if row["state"] != "pending":
            raise ApprovalServiceError("invalid_state", "approval is not pending")
        if row["action"] == "lost_key_reset" and approve:
            key_id = _target_key_id(row["target_ref"])
            key = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone() if key_id else None
            if key is None or key["state"] != "active":
                raise ApprovalServiceError("invalid_state", "reset target is no longer an active key")
            requester = conn.execute("SELECT * FROM users WHERE id = ?",
                                     (row["requested_by"],)).fetchone()
            _require_admin(requester)
            _require_non_owner(requester, key)
            _require_non_owner(admin_row, key)

        state = "approved" if approve else "rejected"
        execute_after = _execute_after(row["action"], now) if approve else None
        conn.execute(
            """UPDATE admin_approvals SET state = ?, decided_by = ?, decided_at = ?,
                                          execute_after = ?, updated_at = ?
               WHERE id = ? AND state = 'pending'""",
            (state, admin_row["id"], now, execute_after, now, approval_id))
        if approve:
            effect = EFFECTS.get(row["action"])
            if effect is not None:
                effect(conn, {"id": approval_id, "action": row["action"],
                              "target_ref": row["target_ref"], "requested_by": row["requested_by"],
                              "execute_after": execute_after}, now)
        models.audit(conn, admin_row["username"], "approval_decide", "approval", approval_id, {
            "approval_id": approval_id, "action": row["action"], "state": state,
            "decided_by": admin_row["id"], "execute_after": execute_after})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def cancel_by_owner(conn: sqlite3.Connection, approval_id: int, user_row, now: str) -> None:
    raise NotImplementedError
