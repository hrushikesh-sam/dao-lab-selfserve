"""sqlite helpers — the only code touching JSON-in-TEXT columns and timestamps."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from . import migrations

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

BUSY_STATES = ("queued", "provisioning", "ready", "active", "tearing_down")
HELD_STATES = ("provisioning", "ready", "active", "tearing_down")
TERMINAL_STATES = ("released", "failed")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    if db_path is None:
        from . import config
        db_path = config.settings.db_path
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)  # autocommit; explicit BEGIN where needed (FastAPI deps and handlers may run on different threadpool threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> list[int]:
    """Bring the DB to the latest schema without touching existing data: apply the
    baseline (schema.sql, all IF NOT EXISTS), then pending migrations. Returns the
    migration versions applied this call — empty means nothing changed (idempotent)."""
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return migrations.apply(conn)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def from_json(text: str):
    return json.loads(text)


def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    now = utcnow()
    row = {**row, "created_at": now, "updated_at": now}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, row_id: int, changes: dict) -> None:
    changes = {**changes, "updated_at": utcnow()}
    assign = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE {table} SET {assign} WHERE id = ?", [*changes.values(), row_id])


# ---- typed row helpers -----------------------------------------------------

def user_by_token_hash(conn, token_hash: str):
    return conn.execute("SELECT * FROM users WHERE token_hash = ?", (token_hash,)).fetchone()


def user_by_id(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT u.id, u.username, u.role, u.enabled, u.created_at,
                  (SELECT COUNT(*) FROM reservations r
                   WHERE r.user_id = u.id AND r.state NOT IN ('released', 'failed'))
                  AS active_reservations
           FROM users u ORDER BY u.id"""
    ).fetchall()
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]


def usecase_by_name(conn, name: str):
    return conn.execute("SELECT * FROM usecases WHERE name = ?", (name,)).fetchone()


def list_usecases(conn):
    return conn.execute("SELECT * FROM usecases ORDER BY id").fetchall()


def setup_by_name(conn, name: str):
    return conn.execute("SELECT * FROM setups WHERE name = ?", (name,)).fetchone()


def list_setups(conn):
    return conn.execute("SELECT * FROM setups ORDER BY name").fetchall()


def setup_members(conn, setup_id: int):
    return conn.execute(
        "SELECT * FROM resources WHERE setup_id = ? ORDER BY name", (setup_id,)
    ).fetchall()


def setup_vm_hosts(conn, setup_id: int) -> set[str]:
    rows = conn.execute("SELECT DISTINCT host FROM resources WHERE setup_id = ?", (setup_id,))
    return {r["host"] for r in rows}


def reservation_by_id(conn, res_id: int):
    return conn.execute("SELECT * FROM reservations WHERE id = ?", (res_id,)).fetchone()


def list_reservations(conn, user_id: int | None = None):
    if user_id is None:
        return conn.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM reservations WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()


def held_by(conn, setup_id: int):
    marks = ", ".join("?" for _ in HELD_STATES)
    return conn.execute(
        f"""SELECT * FROM reservations WHERE setup_id = ? AND state IN ({marks})
            ORDER BY start_at, id LIMIT 1""",
        (setup_id, *HELD_STATES),
    ).fetchone()


def queued_for(conn, setup_id: int):
    return conn.execute(
        "SELECT * FROM reservations WHERE setup_id = ? AND state = 'queued' ORDER BY start_at, id",
        (setup_id,),
    ).fetchall()


def vms_for_reservation(conn, res_id: int):
    return conn.execute(
        "SELECT * FROM vms WHERE reservation_id = ? ORDER BY id", (res_id,)
    ).fetchall()


def vm_by_role(conn, res_id: int, role: str):
    return conn.execute(
        "SELECT * FROM vms WHERE reservation_id = ? AND role = ?", (res_id, role)
    ).fetchone()


def set_vm_ip(conn, vm_id: int, ip: str) -> None:
    update(conn, "vms", vm_id, {"ip": ip})


# ---- IP pools / leases (B-40) ----------------------------------------------

def ip_pool_by_name(conn, name: str):
    return conn.execute("SELECT * FROM ip_pools WHERE name = ?", (name,)).fetchone()


def list_ip_pools(conn):
    return conn.execute("SELECT * FROM ip_pools ORDER BY name").fetchall()


def live_leases(conn, pool_id: int):
    """Leases that still occupy an address: leased or quarantined, never released."""
    return conn.execute(
        "SELECT * FROM ip_leases WHERE pool_id = ? AND state != 'released' ORDER BY ip",
        (pool_id,)).fetchall()


def ip_lease_by_role(conn, res_id: int, role: str):
    return conn.execute(
        """SELECT * FROM ip_leases WHERE reservation_id = ? AND role = ? AND state != 'released'
           ORDER BY id LIMIT 1""", (res_id, role)).fetchone()


def ip_leases_for_reservation(conn, res_id: int):
    return conn.execute(
        "SELECT * FROM ip_leases WHERE reservation_id = ? ORDER BY id", (res_id,)).fetchall()
