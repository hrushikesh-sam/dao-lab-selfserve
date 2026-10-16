"""Access control: who may log into which testbed host, right now (B-50, plan/09 §7).

On the DPU everyone shares one login (`lab`). sshd asks the backend for the keys valid *now*
(`AuthorizedKeysCommand` -> `GET /internal/authz/keys`, B-51/B-52), and the answer is
derived from reservations and the clock. Nothing is stored, so there is no grant to forget
to revoke:

  * the host must be a member of a setup (`resources.name`, e.g. ``dpu-01``);
  * the login must be the shared lab login; every other login (root, the service account)
    keeps its own static authorized_keys and gets nothing from here;
  * a reservation on that setup must be `ready` or `active` with
    ``start_at <= now < end_at``. With DAOLAB_EARLY_ACCESS on, a `ready` reservation opens
    the window as soon as it is deployed, before `start_at`;
  * its owner must be enabled.

The answer is the owner's public key(s), or nothing. A slot that has ended stops granting at
`end_at` exactly, whatever cleanup is doing.
"""
import sqlite3
from datetime import datetime

from . import models

LAB_LOGIN = "lab"            # the one shared login on the DPU (plan/09 §7)
GRANTING_STATES = ("ready", "active")


def _setup_of(conn: sqlite3.Connection, host: str):
    row = conn.execute("SELECT setup_id FROM resources WHERE name = ? ORDER BY id LIMIT 1",
                       (host,)).fetchone()
    return row["setup_id"] if row else None


def holders(conn: sqlite3.Connection, host: str, now: datetime,
            early_access: bool = False) -> list[sqlite3.Row]:
    """The reservations whose owner may log into `host` at `now` (normally zero or one: the
    scheduler never lets two live reservations on one setup overlap)."""
    setup_id = _setup_of(conn, host)
    if setup_id is None:
        return []
    marks = ", ".join("?" for _ in GRANTING_STATES)
    rows = conn.execute(
        f"""SELECT r.* FROM reservations r JOIN users u ON u.id = r.user_id
            WHERE r.setup_id = ? AND r.state IN ({marks}) AND u.enabled = 1
            ORDER BY r.start_at, r.id""", (setup_id, *GRANTING_STATES)).fetchall()
    out = []
    for r in rows:
        start, end = models.parse_ts(r["start_at"]), models.parse_ts(r["end_at"])
        if now >= end:
            continue
        if start <= now or (early_access and r["state"] == "ready"):
            out.append(r)
    return out


def _key_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def keys_for(conn: sqlite3.Connection, host: str, login: str, now: datetime,
             early_access: bool | None = None) -> list[str]:
    """The authorized_keys lines valid for `login`@`host` at `now`; empty means no access.
    `early_access` defaults to DAOLAB_EARLY_ACCESS."""
    if login != LAB_LOGIN:
        return []
    if early_access is None:
        from . import config
        early_access = config.settings.early_access
    keys: list[str] = []
    for r in holders(conn, host, now, early_access):
        owner = models.user_by_id(conn, r["user_id"])
        for line in _key_lines(owner["ssh_pubkey"]):
            if line not in keys:
                keys.append(line)
    return keys
