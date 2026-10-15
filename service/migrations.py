"""Schema migrations — the only way the schema changes after the baseline.

`schema.sql` is the frozen v0 baseline (every statement is CREATE ... IF NOT EXISTS).
Everything added afterwards is a numbered migration below. The applied version lives
in SQLite's `PRAGMA user_version`, so `daolab init` is always safe to re-run:

  * fresh DB     -> baseline, then every migration
  * legacy DB    -> baseline is a no-op (user_version 0), pending migrations apply
  * current DB   -> nothing to do

Rules for adding a migration:
  1. Append a `Migration(N, "name", fn)` with N = previous + 1. Never edit or reorder
     an existing one — released DBs have already run it.
  2. Make `fn` idempotent (use `add_column` / `CREATE ... IF NOT EXISTS`), so a DB that
     already has the change is not an error.
  3. Do not manage transactions inside `fn`; `apply()` wraps each one in BEGIN IMMEDIATE
     and bumps user_version in the same transaction (rollback undoes both).
"""
import sqlite3
from dataclasses import dataclass
from typing import Callable


class SchemaTooNew(Exception):
    """The DB was migrated by newer code than this checkout."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    fn: Callable[[sqlite3.Connection], None]


# ---- helpers ---------------------------------------------------------------

def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN unless it already exists. Returns True if added.
    `ddl` is the type + constraints, e.g. "TEXT NOT NULL DEFAULT 'asap'" (SQLite
    requires a constant DEFAULT for NOT NULL columns added to existing rows)."""
    if has_column(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


# ---- migrations (append-only) ----------------------------------------------

def _m1_reservation_mode(conn: sqlite3.Connection) -> None:
    """plan/09 §4-5: booking mode (asap | at) and the turnaround buffer a reservation
    blocks after its end_at. Existing rows are plain asap bookings with no buffer."""
    add_column(conn, "reservations", "mode",
               "TEXT NOT NULL DEFAULT 'asap' CHECK (mode IN ('asap', 'at'))")
    add_column(conn, "reservations", "turnaround_min", "INTEGER NOT NULL DEFAULT 0")


def _m2_ip_leases(conn: sqlite3.Connection) -> None:
    """plan/09 §IPAM: IP pool ranges and leases. `(pool, ip)` is unique among *live*
    leases (leased or quarantined) so no address is handed to two users; `(reservation,
    role)` is unique among live leases, which makes re-leasing idempotent. Released rows
    stay as history and free their address."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_pools (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            range_start TEXT    NOT NULL,
            range_end   TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )""")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_leases (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_id        INTEGER NOT NULL REFERENCES ip_pools(id),
            ip             TEXT    NOT NULL,
            reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
            role           TEXT    NOT NULL,
            state          TEXT    NOT NULL DEFAULT 'leased'
                           CHECK (state IN ('leased', 'released', 'quarantined')),
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        )""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_leases_live_ip "
        "ON ip_leases(pool_id, ip) WHERE state != 'released'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_leases_res_role "
        "ON ip_leases(reservation_id, role) WHERE state != 'released'")


def _m3_setup_availability(conn: sqlite3.Connection) -> None:
    """plan/09 §6: a setup is `available`, `quarantined` (residue survived cleanup) or
    under `maintenance`. Placement is unaffected — the scheduler keeps booking slots —
    but the engine refuses to start provisioning while a setup is not `available`
    (B-18). Existing setups default to available."""
    add_column(conn, "setups", "availability",
               "TEXT NOT NULL DEFAULT 'available' "
               "CHECK (availability IN ('available', 'quarantined', 'maintenance'))")


def _m4_audit_events(conn: sqlite3.Connection) -> None:
    """plan/09 §11 / §audit_events: an append-only log of who did what (deploy, cleanup,
    redeploy, force-clean, admin overrides). There is deliberately no UPDATE or DELETE
    path anywhere in the service, so the history stays a faithful record. The engine
    (B-20/B-23) writes deploy/cleanup/redeploy; the API writes admin overrides."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            actor       TEXT    NOT NULL,   -- username, or 'system'/'engine'
            action      TEXT    NOT NULL,
            object_type TEXT,              -- reservation | setup | user
            object_id   INTEGER,
            detail      TEXT,              -- JSON, free-form
            trace_id    TEXT,
            created_at  TEXT    NOT NULL
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_events_created "
        "ON audit_events(created_at DESC, id DESC)")


def _m5_jobs(conn: sqlite3.Connection) -> None:
    """plan/09 §6 / §11: the unit of work the engine runs (B-20). A job belongs to one
    reservation and has its own small lifecycle (pending -> running -> succeeded | failed).
    `attempt` is fixed when the row is created (a retry is a *new* job with attempt + 1),
    `steps` is a JSON list the runner appends to while the job runs, and `log_path` /
    `trace_id` link to the playbook log and the OpenTelemetry trace. The setup a job runs
    on is derived through `reservations.setup_id`, so it is not duplicated here."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
            kind           TEXT    NOT NULL
                           CHECK (kind IN ('deploy', 'cleanup', 'health_probe', 'reconcile')),
            state          TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
            attempt        INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            trace_id       TEXT,
            log_path       TEXT,
            steps          TEXT    NOT NULL DEFAULT '[]',   -- JSON list of step records
            error          TEXT,                            -- set only in state 'failed'
            started_at     TEXT,                            -- set when the job starts running
            finished_at    TEXT,                            -- set when it succeeds or fails
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_reservation ON jobs(reservation_id, id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, id)")


MIGRATIONS: list[Migration] = [
    Migration(1, "reservation_mode", _m1_reservation_mode),
    Migration(2, "ip_leases", _m2_ip_leases),
    Migration(3, "setup_availability", _m3_setup_availability),
    Migration(4, "audit_events", _m4_audit_events),
    Migration(5, "jobs", _m5_jobs),
]


def _check_contiguous(migrations: list[Migration]) -> None:
    for expected, m in enumerate(migrations, start=1):
        if m.version != expected:
            raise RuntimeError(
                f"migration versions must be 1..N in order; got {m.version} at position {expected}")


_check_contiguous(MIGRATIONS)


# ---- runner ----------------------------------------------------------------

def latest_version() -> int:
    return MIGRATIONS[-1].version if MIGRATIONS else 0


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order; return the versions applied (empty = no-op).

    Expects an autocommit connection (`isolation_level=None`, as `models.connect` makes).
    Each migration runs in its own BEGIN IMMEDIATE transaction together with the
    user_version bump, and the version is re-read inside the lock so two concurrent
    `daolab init` runs cannot apply the same migration twice."""
    if current_version(conn) > latest_version():
        raise SchemaTooNew(
            f"database schema is v{current_version(conn)} but this code only knows up to "
            f"v{latest_version()} — update the checkout before running against this DB")
    applied: list[int] = []
    for m in MIGRATIONS:
        if m.version <= current_version(conn):
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if current_version(conn) >= m.version:      # lost a race to another process
                conn.execute("ROLLBACK")
                continue
            m.fn(conn)
            conn.execute(f"PRAGMA user_version = {int(m.version)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        applied.append(m.version)
    return applied
