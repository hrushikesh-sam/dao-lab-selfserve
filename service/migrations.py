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


MIGRATIONS: list[Migration] = [
    Migration(1, "reservation_mode", _m1_reservation_mode),
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
