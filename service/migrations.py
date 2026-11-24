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


class RevertedSchema(Exception):
    """The DB has tables or columns from the reverted Phase 8 attempt (4ec115e)."""


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


def _m6_reservation_states(conn: sqlite3.Connection) -> None:
    """plan/09 §10 (B-21): `tearing_down` becomes `cleaning` and a new `quarantined` state
    appears (cleanup left residue). The baseline `reservations.state` CHECK lists the old
    names and SQLite cannot ALTER a CHECK, so this rewrites the table definition in place
    (the procedure SQLite documents for *relaxing* a constraint: no data is copied, so the
    foreign keys from vms / ip_leases / jobs are never touched), then renames existing
    `tearing_down` rows. Idempotent: an already-rewritten table is left alone. If the
    expected text is not found the migration fails loudly and rolls back instead of
    guessing."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reservations'"
    ).fetchone()[0]
    if "'quarantined'" not in sql:
        if _OLD_STATES not in sql:
            raise RuntimeError(
                "migration 6: cannot find the reservations.state CHECK to rewrite "
                f"(expected {_OLD_STATES!r} in the table definition)")
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'reservations'",
            (sql.replace(_OLD_STATES, _NEW_STATES, 1),))
        conn.execute(f"PRAGMA schema_version = {int(version) + 1}")
        conn.execute("PRAGMA writable_schema = OFF")
    conn.execute("UPDATE reservations SET state = 'cleaning' WHERE state = 'tearing_down'")


_OLD_STATES = "'tearing_down', 'released', 'failed'"
_NEW_STATES = "'cleaning', 'quarantined', 'released', 'failed'"


def _m7_lease_role_unique_while_leased(conn: sqlite3.Connection) -> None:
    """B-41: `(reservation, role)` is unique among *leased* rows only (v2 made it unique
    among live rows, i.e. leased or quarantined). A quarantined lease still blocks its
    address through `(pool, ip)`, but it must not stop the same role from leasing a
    replacement: that is how an address something else answers on is swapped out
    (`ipam.mark_conflict`). Re-leasing stays idempotent: there is at most one `leased` row
    per (reservation, role)."""
    conn.execute("DROP INDEX IF EXISTS idx_ip_leases_res_role")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_leases_res_role_leased "
        "ON ip_leases(reservation_id, role) WHERE state = 'leased'")


def _m8_usecase_health(conn: sqlite3.Connection) -> None:
    """plan/09 §8 (B-64 follow-up, needed by B-65): the use case's `health:` rules, as the
    JSON list `daolab sync` validated. Request handlers and the engine never read YAML, so
    the rules have to live on the row. Existing rows have none until the next sync."""
    add_column(conn, "usecases", "health", "TEXT NOT NULL DEFAULT '[]'")


def _m9_health_snapshots(conn: sqlite3.Connection) -> None:
    """plan/09 §8 / §11 (B-67): the latest health verdict of each reservation, written by the
    engine every tick for `active` reservations. Only the newest row is needed, so there is
    one row per reservation, overwritten in place. `checks` is the per-check JSON list;
    `changed_at` is when the verdict last changed (the webhook fires on a change to down)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS health_snapshots (
            reservation_id INTEGER PRIMARY KEY REFERENCES reservations(id) ON DELETE CASCADE,
            verdict        TEXT    NOT NULL
                           CHECK (verdict IN ('healthy', 'degraded', 'down', 'unknown')),
            reason         TEXT,
            checks         TEXT    NOT NULL DEFAULT '[]',
            last_seen      TEXT,
            evaluated_at   TEXT    NOT NULL,
            changed_at     TEXT    NOT NULL,
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        )""")


def _m10_reservation_access(conn: sqlite3.Connection) -> None:
    """How a reservation's owner gets into the testbed (dashboard Deploy dialog):
    `reservations.ssh_pubkey` is the SSH key given at deploy (NULL: the owner's registered
    key); `reservations.access_password` is the generated password when no key was given
    (shown to the owner while the slot is live, wiped when it ends). `resources.address` is
    a member's management address from the inventory (DPUs are static), which the API shows
    as `ssh lab@<address>`."""
    add_column(conn, "reservations", "ssh_pubkey", "TEXT")
    add_column(conn, "reservations", "access_password", "TEXT")
    add_column(conn, "resources", "address", "TEXT")


def _m11_token_expiry(conn: sqlite3.Connection) -> None:
    """B-101 (mistakes.md J13): bearer tokens expire. `users.token_expires_at` is the UTC
    time (`YYYY-MM-DDTHH:MM:SSZ`) after which the token is refused; NULL = never (tokens
    issued before this migration, or DAOLAB_TOKEN_TTL_DAYS=0)."""
    add_column(conn, "users", "token_expires_at", "TEXT")


def _m12_burned(conn: sqlite3.Connection) -> None:
    """E-4: version 12 is burned. The reverted B-104 attempt (4ec115e, reverted in 9c8a089)
    used it, so it stays a no-op forever. `refuse_reverted_schema` keeps those DBs out."""


def _m13_burned(conn: sqlite3.Connection) -> None:
    """E-4: version 13 is burned; see _m12_burned."""


def _m14_snapshots_v1(conn: sqlite3.Connection) -> None:
    """B-104 (B-104-DDL.md v4.1 §3: v4 signed off by Rama 23 Nov; the v4.1 items, marked
    `# v4.1-N`, signed off by Rama 24 Nov): the Phase 8 snapshot schema, as one migration (Q-5).

    * `keys` - a user's age X25519 public recipients; pending -> active (by another admin)
      -> retired | revoked. Never any private key material (design:55, :72-73; E-10).
    * `key_challenges` - single-use, 10-minute challenges; only the nonce's SHA-256 is kept.
    * `admin_approvals` - two-admin actions (lost-key reset, tripwire, home discard).
    * `snapshots` - one row per saved home; files are derived from `public_id`, never stored.
      Holds live in `delete_after` / `delete_requested_by` / `hold_approval_id` (Q-13);
      the routine delete sweep handles only rows with `hold_approval_id IS NULL`.
    * columns `reservations.save_snapshot` (DEFAULT 0 = PRESERVE), `jobs.snapshot_id`,
      `setups.snapshot_enabled` (admin-controlled, not synced).
    * the `restore` job kind: the jobs.kind CHECK is rewritten in place, the m6 way.

    Order: keys, key_challenges, admin_approvals, snapshots (each table, then its indexes),
    then the three columns, then the jobs.kind rewrite. Idempotent: `IF NOT EXISTS`,
    `add_column` and a rewrite that skips when done. That is safe only because
    `refuse_reverted_schema` has already refused the reverted shapes, and nothing here
    creates any of its markers (M1..M6). One statement per execute(); never executescript(),
    which would COMMIT apply()'s transaction."""
    # -- keys (v4 §3.3) --
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keys (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id),
            recipient    TEXT    NOT NULL UNIQUE                     -- age X25519 public recipient [design:55]
                         CHECK (length(recipient) = 62                -- (inferred) 'age1' + 58 bech32 chars; N-1
                                AND recipient GLOB 'age1*'
                                AND substr(recipient, 5) NOT GLOB '*[^qpzry9x8gf2tvdw0s3jn54khce6mua7l]*'),
            fingerprint  TEXT    NOT NULL UNIQUE                     -- shown by the CLI at enrollment [design:57]
                         CHECK (length(fingerprint) = 64              -- N-1: proposed lowercase sha256 hex
                                AND fingerprint NOT GLOB '*[^0-9a-f]*'), --      of `recipient`
            state        TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending', 'active', 'retired', 'revoked')),  -- [design:57]; Q-6
            activated_by INTEGER REFERENCES users(id),               -- the activating admin [design:57]
            activated_at TEXT,
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            UNIQUE (id, user_id),                                    -- target of the composite FKs below (inferred)
            CHECK (activated_by IS NULL OR activated_by <> user_id), -- "different admin" = not the owner (Q-7)
            CHECK ((state = 'pending') = (activated_by IS NULL)),    -- retired/revoked keys were once active
        """
        # v4.1-2 (signed off by Rama 24 Nov): activated_by and activated_at are set together
        """
            CHECK ((activated_by IS NULL) = (activated_at IS NULL)),
        """
        """
            CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND (activated_at IS NULL OR
                        activated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'))
                                                                     -- timestamp shape, §3.2
        )""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_one_active  ON keys(user_id) "
        "WHERE state = 'active'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_one_pending ON keys(user_id) "
        "WHERE state = 'pending'  -- (inferred)")

    # -- key_challenges (v4 §3.4) --
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS key_challenges (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id),
            key_id       INTEGER NOT NULL,                           -- the key the nonce was sealed to (inferred)
            purpose      TEXT    NOT NULL
                         CHECK (purpose IN ('rotate', 'delete', 'cancel_delete', 'discard_home')),  -- [design:68]
            target_ref   TEXT    NOT NULL                            -- plaintext [design:68, :122]
                         CHECK (length(target_ref) BETWEEN 1 AND 128
                                AND target_ref NOT GLOB '*[^a-z0-9._:-]*'       -- closed lowercase charset (inferred); Q-10
                                AND target_ref NOT LIKE 'age-secret-key-%'),    -- belt: refuses an S-shaped value even if
                                                                                -- Q-10 widens the charset (LIKE ignores ASCII case)
            nonce_sha256 TEXT    NOT NULL UNIQUE                     -- hash only, never the nonce [design:67]
                         CHECK (length(nonce_sha256) = 64 AND nonce_sha256 NOT GLOB '*[^0-9a-f]*'),
            expires_at   TEXT    NOT NULL,                           -- created + 10 min [design:66]; one of the
                                                                     -- hashed fields, so its exact text counts
            consumed_at  TEXT,                                       -- NULL = open; set once by CAS [design:66]
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            FOREIGN KEY (key_id, user_id) REFERENCES keys(id, user_id),   -- the key must be the user's own (inferred)
            CHECK (expires_at > created_at),
            CHECK (consumed_at IS NULL OR consumed_at <= expires_at),
            CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND (consumed_at IS NULL OR
                        consumed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'))
        )""")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_key_challenges_open
            ON key_challenges(user_id, expires_at) WHERE consumed_at IS NULL""")

    # -- admin_approvals (v4 §3.6); before snapshots, which references it --
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_approvals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            action        TEXT    NOT NULL
                          CHECK (action IN ('lost_key_reset', 'tripwire_accept', 'tripwire_rebaseline',
                                            'home_discard')),                    -- Q-11 (final)
            target_ref    TEXT    NOT NULL                                       -- Q-11: """
        # v4.1-4 (signed off by Rama 24 Nov): lost_key_reset targets key:<id> (v4: user:<id>)
        """key:<id>"""
        """, hw_class, """
        # v4.1-5 (signed off by Rama 24 Nov): home_discard targets setup:<name> (v4: setup name)
        """setup:<name>
                          CHECK (length(target_ref) BETWEEN 1 AND 128
                                 AND target_ref NOT GLOB '*[^a-z0-9._:-]*'
                                 AND target_ref NOT LIKE 'age-secret-key-%'),
            requested_by  INTEGER NOT NULL REFERENCES users(id),                 -- first admin
            decided_by    INTEGER REFERENCES users(id),                          -- second admin
            state         TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (state IN ('pending', 'approved', 'rejected', 'cancelled')),
            decided_at    TEXT,
            execute_after TEXT,                                                  -- end of the 24h hold (Q-13); NULL = none
            executed_at   TEXT,                                                  -- single use (inferred)
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL,
            CHECK (decided_by IS NULL OR decided_by <> requested_by),            -- two different admins [design:17-18]
            CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
            CHECK (state <> 'pending' OR decided_by IS NULL),
            CHECK (state NOT IN ('approved', 'rejected') OR decided_by IS NOT NULL),  -- 'cancelled' may follow an
                                                                                      -- approval (owner cancel)
            CHECK (executed_at IS NULL OR state = 'approved'),
            CHECK (executed_at IS NULL OR execute_after IS NULL OR executed_at >= execute_after),
            CHECK (execute_after IS NULL OR execute_after > created_at),
            CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND (decided_at IS NULL OR
                        decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
                   AND (execute_after IS NULL OR
                        execute_after GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
                   AND (executed_at IS NULL OR
                        executed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'))
        )""")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_approvals_one_open
            ON admin_approvals(action, target_ref) """
        # v4.1-1 (signed off by Rama 24 Nov): an approved, not yet executed approval stays
        # "open" (v4: WHERE state = 'pending'; v4's "(inferred)" tag dropped, v4.1-6)
        """WHERE state = 'pending' OR (state = 'approved' AND executed_at IS NULL)""")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_admin_approvals_due
            ON admin_approvals(execute_after) WHERE state = 'approved' AND executed_at IS NULL""")

    # -- snapshots (v4 §3.5) --
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id           TEXT    NOT NULL UNIQUE              -- [design:119]; used in file and ticket names
                                CHECK (length(public_id) = 32        -- Q-9: 32 lowercase hex, no '-'
                                       AND public_id NOT GLOB '*[^0-9a-f]*'),
            user_id             INTEGER NOT NULL REFERENCES users(id),    -- owner; path u<user_id> [design:91]
            setup_id            INTEGER NOT NULL REFERENCES setups(id),   -- retention key [design:96]; NO ACTION (Q-20)
            reservation_id      INTEGER UNIQUE                       -- the slot it was captured from (inferred);
                                REFERENCES reservations(id) ON DELETE SET NULL,  -- sync deletes released reservations
                                                                     -- (catalog.py:564, :578); UNIQUE = D-7-4 row reuse
            key_id              INTEGER NOT NULL,                    -- the key key.age is wrapped to (inferred; §4.1)
            state               TEXT    NOT NULL DEFAULT 'capturing'
                                CHECK (state IN ('capturing', 'saved', 'failed')),   -- [design:120]; Q-15
            delete_after        TEXT,                                -- end of a 24h delete hold; NULL = not held (Q-13)
            delete_requested_by INTEGER REFERENCES users(id),        -- who started the hold (Q-13)
            hold_approval_id    INTEGER REFERENCES admin_approvals(id),  -- set only for a hold placed by an approval
                                                                     -- (lost_key_reset); NULL for an own delete hold (Q-6)
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL,
            FOREIGN KEY (key_id, user_id) REFERENCES keys(id, user_id),
            CHECK ((delete_after IS NULL) = (delete_requested_by IS NULL)),
            CHECK (hold_approval_id IS NULL OR delete_after IS NOT NULL),
            CHECK (delete_after IS NULL OR state = 'saved'),         -- only saved rows are held; unsaved ones
                                                                     -- purge with hold 0 [design:112] (inferred)
            CHECK (delete_after IS NULL OR delete_after > created_at),
            CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                   AND (delete_after IS NULL OR
                        delete_after GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'))
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_user_setup ON snapshots(user_id, setup_id, state, created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_key        ON snapshots(key_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_delete_due ON snapshots(delete_after) "
        "WHERE delete_after IS NOT NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_hold_approval ON snapshots(hold_approval_id) "
        "WHERE hold_approval_id IS NOT NULL")

    # -- columns (v4 §3.7, §3.8, §3.9) --
    add_column(conn, "reservations", "save_snapshot",
               "INTEGER NOT NULL DEFAULT 0 CHECK (save_snapshot IN (0, 1))")
    add_column(conn, "jobs", "snapshot_id",
               "INTEGER REFERENCES snapshots(id) ON DELETE SET NULL")
    add_column(conn, "setups", "snapshot_enabled",
               "INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_enabled IN (0, 1))")

    # -- the restore kind (v4 §3.8, Q-14): rewrite the jobs.kind CHECK text in place, the m6
    # way. A text edit touches no row, so no FK check or ON DELETE action can fire; never
    # rebuild the table. --
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()[0]
    # v4.1-3 (signed off by Rama 24 Nov): skip only on the exact new text (v4: "'restore'" in
    # sql); otherwise the old text must occur exactly once (v4: _OLD_JOB_KINDS not in sql), or
    # raise, and apply() rolls back: user_version and the schema stay unchanged.
    if _NEW_JOB_KINDS in sql:
        return
    if sql.count(_OLD_JOB_KINDS) != 1:
        raise RuntimeError(
            "migration 14: cannot find the jobs.kind CHECK to rewrite "
            f"(expected {_OLD_JOB_KINDS!r} in the table definition)")
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'jobs'",
        (sql.replace(_OLD_JOB_KINDS, _NEW_JOB_KINDS, 1),))
    conn.execute(f"PRAGMA schema_version = {int(version) + 1}")
    conn.execute("PRAGMA writable_schema = OFF")


# Derived from m5's CHECK text (migrations.py `_m5_jobs`), not copied from the reverted commit.
_OLD_JOB_KINDS = "'deploy', 'cleanup', 'health_probe', 'reconcile')"
_NEW_JOB_KINDS = "'deploy', 'cleanup', 'health_probe', 'reconcile', 'restore')"


MIGRATIONS: list[Migration] = [
    Migration(1, "reservation_mode", _m1_reservation_mode),
    Migration(2, "ip_leases", _m2_ip_leases),
    Migration(3, "setup_availability", _m3_setup_availability),
    Migration(4, "audit_events", _m4_audit_events),
    Migration(5, "jobs", _m5_jobs),
    Migration(6, "reservation_states", _m6_reservation_states),
    Migration(7, "lease_role_unique_while_leased", _m7_lease_role_unique_while_leased),
    Migration(8, "usecase_health", _m8_usecase_health),
    Migration(9, "health_snapshots", _m9_health_snapshots),
    Migration(10, "reservation_access", _m10_reservation_access),
    Migration(11, "token_expiry", _m11_token_expiry),
    Migration(12, "burned_reverted_v12", _m12_burned),
    Migration(13, "burned_reverted_v13", _m13_burned),
    Migration(14, "snapshots_v1", _m14_snapshots_v1),
]


def _check_contiguous(migrations: list[Migration]) -> None:
    for expected, m in enumerate(migrations, start=1):
        if m.version != expected:
            raise RuntimeError(
                f"migration versions must be 1..N in order; got {m.version} at position {expected}")


_check_contiguous(MIGRATIONS)


# ---- reverted-schema guard (errata E-4) ------------------------------------
# Schema that only the reverted v12/v13 (4ec115e:service/migrations.py) created. Any one
# of them means refuse: a reverted DB reports user_version 12 or 13, so the loop below
# would skip the burned 12/13 and every later migration's `IF NOT EXISTS` / `add_column`
# would silently keep the old shapes (e.g. save_snapshot DEFAULT 1 inverts PRESERVE).
# Later migrations must never recreate any of these (pinned by
# tests/test_migrations_guard.py::test_g6_guard_passes_on_our_own_latest_schema).
# The labels are fixed text: they are the only DB-derived part of the error message.

def _save_snapshot_default_is_1(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "save_snapshot" and row[4] == "1"
               for row in conn.execute("PRAGMA table_info(reservations)"))


_REVERTED_MARKERS: tuple[tuple[str, Callable[[sqlite3.Connection], bool]], ...] = (
    ("table user_keys", lambda c: has_table(c, "user_keys")),                        # M1
    ("column jobs.secret", lambda c: has_column(c, "jobs", "secret")),               # M2
    ("table snapshots without public_id",
     lambda c: has_table(c, "snapshots") and not has_column(c, "snapshots", "public_id")),  # M3
    ("column snapshots.wrapped_dek", lambda c: has_column(c, "snapshots", "wrapped_dek")),  # M4
    ("column setups.snapshot_bdfs", lambda c: has_column(c, "setups", "snapshot_bdfs")),    # M5
    ("reservations.save_snapshot default 1", _save_snapshot_default_is_1),           # M6
)


def refuse_reverted_schema(conn: sqlite3.Connection) -> None:
    """Raise RevertedSchema if any marker is present. Read-only (sqlite_master and
    PRAGMA table_info), and independent of user_version."""
    found = [label for label, check in _REVERTED_MARKERS if check(conn)]
    if found:
        raise RevertedSchema(
            "database has schema from the reverted Phase 8 snapshot attempt "
            f"({', '.join(found)}); refusing to migrate it. Move this DB aside and start a "
            "fresh one with `daolab init`, or ask an admin. Do not edit it by hand.")


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
    refuse_reverted_schema(conn)       # every call, before any migration (E-4)
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
