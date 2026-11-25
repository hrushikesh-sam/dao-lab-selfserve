"""B-106: single-use, age-sealed challenges that prove possession of the user's key."""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import timedelta

from . import agecli, keys, models

PURPOSES = ("rotate", "delete", "cancel_delete", "discard_home")
HOLD_ACTIONS = frozenset({"lost_key_reset", "home_discard"})
KEPT_HOME_STATES = ("quarantined", "failed", "cleaning", "released")
TARGET_RE = re.compile(r"^[a-z0-9._:-]{1,128}$")
SECRET_SHAPED_RE = re.compile(r"^age-secret-key-", re.IGNORECASE)
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TTL_MINUTES = 10
FIXED_MESSAGES = frozenset({"expired", "used", "mismatch"})


class ChallengeError(Exception):
    def __init__(self, message: str, code: str | None = None, http_status: int = 409,
                 detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or (f"challenge:{message}" if message in FIXED_MESSAGES
                             else "challenge_error")
        self.http_status = http_status
        self.detail = detail or {}


def digest(nonce: bytes, purpose: str, target_ref: str, key_id, user_id,
           expires_at: str) -> str:
    values = (nonce, purpose, target_ref, str(key_id), str(user_id), expires_at)
    sha = hashlib.sha256()
    for value in values:
        raw = value if isinstance(value, bytes) else str(value).encode("ascii")
        sha.update(len(raw).to_bytes(8, "big"))
        sha.update(raw)
    return sha.hexdigest()


def _armor(data: bytes) -> bytes:
    text = base64.b64encode(data).decode("ascii")
    lines = [text[i:i + 64] for i in range(0, len(text), 64)] or [""]
    return ("-----BEGIN AGE ENCRYPTED FILE-----\n" + "\n".join(lines)
            + "\n-----END AGE ENCRYPTED FILE-----\n").encode("ascii")


def _stamp(now) -> str:
    return now if isinstance(now, str) else models.format_ts(now)


def _expiry(now: str) -> str:
    return models.format_ts(models.parse_ts(now) + timedelta(minutes=TTL_MINUTES))


def _setup_id(conn: sqlite3.Connection, name: str):
    return conn.execute("SELECT id FROM setups WHERE name = ?", (name,)).fetchone()


def _has_kept_home(conn: sqlite3.Connection, user_id: int, setup_id: int) -> bool:
    marks = ",".join("?" for _ in KEPT_HOME_STATES)
    return conn.execute(
        f"""SELECT 1 FROM reservations WHERE user_id = ? AND setup_id = ?
            AND state IN ({marks}) LIMIT 1""", (user_id, setup_id, *KEPT_HOME_STATES)).fetchone() is not None


def approval_holds_user(conn: sqlite3.Connection, user_id: int, approval_id: int) -> bool:
    row = conn.execute(
        "SELECT action, target_ref, state, executed_at FROM admin_approvals WHERE id = ?",
        (approval_id,)).fetchone()
    if row is None or row["state"] != "approved" or row["executed_at"] is not None:
        return False
    if row["action"] not in HOLD_ACTIONS:
        return False
    target = row["target_ref"]
    if row["action"] == "lost_key_reset":
        if not target.startswith("key:"):
            return False
        suffix = target[4:]
        if not suffix.isdigit():
            return False
        return conn.execute("SELECT 1 FROM keys WHERE id = ? AND user_id = ?",
                            (int(suffix), user_id)).fetchone() is not None
    if not target.startswith("setup:"):
        return False
    setup = _setup_id(conn, target[6:])
    return setup is not None and _has_kept_home(conn, user_id, setup["id"])


def _target_ok(conn: sqlite3.Connection, user_id: int, purpose: str, target_ref: str) -> bool:
    if not isinstance(target_ref, str) or not TARGET_RE.fullmatch(target_ref):
        return False
    if SECRET_SHAPED_RE.match(target_ref):
        return False
    if purpose == "rotate":
        return bool(FINGERPRINT_RE.fullmatch(target_ref)) and conn.execute(
            "SELECT 1 FROM keys WHERE user_id = ? AND state = 'pending' AND fingerprint = ?",
            (user_id, target_ref)).fetchone() is not None
    if purpose == "delete":
        return bool(PUBLIC_ID_RE.fullmatch(target_ref)) and conn.execute(
            """SELECT 1 FROM snapshots WHERE user_id = ? AND public_id = ? AND state = 'saved'
               AND delete_after IS NULL AND hold_approval_id IS NULL""",
            (user_id, target_ref)).fetchone() is not None
    if purpose == "cancel_delete":
        if PUBLIC_ID_RE.fullmatch(target_ref):
            return conn.execute(
                """SELECT 1 FROM snapshots WHERE user_id = ? AND public_id = ? AND state = 'saved'
                   AND delete_after IS NOT NULL AND hold_approval_id IS NULL
                   AND delete_requested_by = ?""",
                (user_id, target_ref, user_id)).fetchone() is not None
        if target_ref.startswith("approval:") and target_ref[9:].isdigit():
            return approval_holds_user(conn, user_id, int(target_ref[9:]))
        return False
    if purpose == "discard_home":
        if not target_ref.startswith("setup:"):
            return False
        setup = _setup_id(conn, target_ref[6:])
        return setup is not None and _has_kept_home(conn, user_id, setup["id"])
    return False


def issue(conn: sqlite3.Connection, user, purpose: str, target_ref: str, now) -> dict:
    if purpose not in PURPOSES:
        raise ChallengeError("invalid_purpose", "validation_error", 422)
    now_text = _stamp(now)
    agecli.version()
    conn.execute("BEGIN IMMEDIATE")
    try:
        key = keys.active_key(conn, user["id"])
        if key is None:
            raise ChallengeError("key_required", "challenge:key_required", 409)
        if not _target_ok(conn, user["id"], purpose, target_ref):
            raise ChallengeError("invalid_target", "validation_error", 422)
        open_count = conn.execute(
            """SELECT COUNT(*) FROM key_challenges
               WHERE user_id = ? AND consumed_at IS NULL AND expires_at > ?""",
            (user["id"], now_text)).fetchone()[0]
        if open_count >= 3:
            raise ChallengeError("too_many_open", "challenge:limit", 409)
        expires_at = _expiry(now_text)
        nonce = secrets.token_bytes(32)
        value = digest(nonce, purpose, target_ref, key["id"], user["id"], expires_at)
        challenge_id = conn.execute(
            """INSERT INTO key_challenges (user_id, key_id, purpose, target_ref, nonce_sha256,
                                           expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], key["id"], purpose, target_ref, value, expires_at,
             now_text, now_text)).lastrowid
        ciphertext = _armor(agecli.encrypt_to(key["recipient"], nonce))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"challenge_id": challenge_id, "expires_at": expires_at, "ciphertext": ciphertext}


def consume(conn: sqlite3.Connection, challenge_id: int, user, purpose: str, target_ref: str,
            nonce: bytes, now) -> None:
    assert conn.in_transaction, "challenge.consume requires the caller's transaction"
    now_text = _stamp(now)
    row = conn.execute("SELECT * FROM key_challenges WHERE id = ? AND user_id = ?",
                       (challenge_id, user["id"])).fetchone()
    if row is None or not isinstance(nonce, (bytes, bytearray)):
        raise ChallengeError("mismatch")
    value = digest(bytes(nonce), row["purpose"], row["target_ref"], row["key_id"],
                   row["user_id"], row["expires_at"])
    if not hmac.compare_digest(value, row["nonce_sha256"]):
        raise ChallengeError("mismatch")
    cur = conn.execute(
        """UPDATE key_challenges SET consumed_at = :now, updated_at = :now
           WHERE id = :id AND user_id = :uid AND purpose = :purpose AND target_ref = :target
             AND consumed_at IS NULL AND expires_at > :now
             AND key_id IN (SELECT id FROM keys WHERE state = 'active')""",
        {"now": now_text, "id": challenge_id, "uid": user["id"], "purpose": purpose,
         "target": target_ref})
    if cur.rowcount != 1:
        if row["consumed_at"] is not None:
            raise ChallengeError("used")
        if row["expires_at"] <= now_text:
            raise ChallengeError("expired")
        raise ChallengeError("mismatch")
