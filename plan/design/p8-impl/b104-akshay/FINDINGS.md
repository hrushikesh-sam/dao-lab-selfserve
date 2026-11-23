# B-104 step 1 — Findings (read-only discovery)

Date: 2026-11-23. Author: Claude Code for Akshay. Status: draft, for Rama.

**Sources.** Every file was read with `git show <ref>:<path>`. Nothing was checked out, no worktree was touched, and no repo file was edited.

| Ref | Commit | What I read |
|---|---|---|
| `origin/rama` | `747d10d` (merge of PR #7) | `service/migrations.py`, `service/schema.sql`, `service/models.py`, `service/cli.py`, `service/engine.py`, `service/catalog.py`, `service/runner.py`, `tests/test_migrations.py`, `plan/` |
| `origin/akshay-p8-gate` | `9c3f959` | `plan/design/p8-gate-review/p8-snapshot-design-v1.1.md` (cited below as **design:NN**) |
| `4ec115e` (on `origin/akshay`, reverted by `9c8a089`) | — | `service/migrations.py` only, for the schema fingerprint |

**Evidence files** in this folder:
- `probe.py`: synthetic in-memory DBs only; it loads `migrations.py` copies taken with `git show`.
- `probe-out.txt`: its output.
- `effective-ddl-v11.txt`: the effective DDL of a fresh `origin/rama` DB.
- `ddl-check.py` and `ddl-check-out.txt`: they parse the DDL blocks of `B-104-DDL.md` on a synthetic DB and exercise their CHECKs.

SQLite is 3.53.1 on the laptop.

**Errata:** `~/notes/p8-design-errata-v1.2.md` (signed off by Rama, 23 Nov 2026), cited as E-n (errata:NN). The ones this task uses:
- E-4 (errata:13): burn 12/13, start at 14, preflight guard;
- E-6 (errata:15): capture/wipe as root, restore preserves ownership;
- E-10 (errata:19): file-only secrets for new Phase 8 code;
- E-14 (errata:23): the exact DDL is written with B-104.

**Base update:** `origin/rama` has since moved to `a03f858` (PR #8, B-103, merged). `git diff --stat 747d10d a03f858 -- service tests/test_migrations.py tests/test_reservation_states.py plan/10-backend-tasksheet.md` is empty, so every citation below still holds.

---

## 1. How migrations work today

### 1.1 Entry points

| Step | Where |
|---|---|
| `daolab init` → `cmd_init` → `models.init_db(conn)` | `service/cli.py:42-47` |
| `init_db` runs `schema.sql` via `executescript` (baseline, all `IF NOT EXISTS`), then `migrations.apply(conn)` | `service/models.py:37-43` |
| Connections are autocommit (`isolation_level=None`), WAL, `PRAGMA foreign_keys=ON` | `service/models.py:26-34` |
| `cmd_init` catches **only** `migrations.SchemaTooNew` and turns it into `_die(...)` (exit 1). Any other exception is a traceback | `service/cli.py:46-47` |
| The engine refuses to start unless `current_version == latest_version` (it never migrates) | `service/engine.py:629-633` |
| The API has **no** schema-version check (`git grep user_version\|latest_version\|current_version origin/rama -- service/api.py` → nothing) | — |
| Callers of `init_db` / `apply` outside tests: only `cli.py:45` | `git grep` |

### 1.2 `apply()` (`service/migrations.py:293-320`)
- `:300-303`: if `PRAGMA user_version` > `latest_version()`, it raises `SchemaTooNew` with only the two version numbers in the message. This is the **only** pre-flight check today. It looks at the number alone, never at the schema contents.
- `:305-307`: it skips every migration whose `version <= user_version`. **A DB that reports 13 therefore never runs 12 or 13, whatever its tables contain.**
- `:308-315`: each migration gets its own `BEGIN IMMEDIATE`. The version is re-read inside the lock (`:310`, for concurrent inits), then `m.fn(conn)` runs, then `PRAGMA user_version = N` and `COMMIT` in the same transaction.
- `:316-318`: any exception → `ROLLBACK` and re-raise, so the DDL and the version bump revert together. Tested: `tests/test_migrations.py:90-103`.
- Consequence: a crash *between* two migrations leaves a consistent DB at the last committed version (e.g. 12 or 13). That is a legitimate state and must not be refused.

### 1.3 Rules in the header (`service/migrations.py:11-17`)
1. Append `Migration(N, ...)` with N = previous + 1. Never edit or reorder an existing migration.
2. `fn` must be idempotent (`add_column`, `CREATE ... IF NOT EXISTS`).
3. No transaction management inside `fn`; `apply()` owns it.

**Contiguity:** `_check_contiguous()` (`:273-277`) runs at import (`:280`) and raises `RuntimeError` unless the versions are exactly 1..N in order. Tested: `tests/test_migrations.py:128-133`. **Verified:** to start B-104 at 14, versions 12 and 13 must exist as entries (placeholders).

**Helpers:** `has_table` (`:37-40`), `has_column` (`:43-44`, `PRAGMA table_info`), `add_column` (`:47-54`; skips if the column exists, whatever its type or default).

### 1.4 How migration 6 rewrote a CHECK in place (`service/migrations.py:167-195`)
- SQLite cannot `ALTER` a CHECK. m6 reads `sqlite_master.sql` for `reservations` (`:176-178`).
- If `'quarantined'` is already in the SQL, it skips the rewrite (idempotent, `:179`).
- Otherwise it requires the exact old text `_OLD_STATES` (`:194`) and raises `RuntimeError` if that is missing (`:180-183`), so a mismatch fails loudly and rolls back.
- It reads `PRAGMA schema_version`, sets `writable_schema = ON`, `UPDATE sqlite_master SET sql = replace(old, new)`, bumps `schema_version` by 1, then `writable_schema = OFF` (`:184-190`).
- This is SQLite's documented procedure for *relaxing* a constraint: no data copy, and the FKs into `reservations` are untouched.
- Then it updates data (`:191`).
- Tested: `tests/test_reservation_states.py:80-87, :163`.

### 1.5 Other facts that matter to B-104
- `models.insert` / `models.update` (`service/models.py:69-83`) always set `created_at` and `updated_at`, and `update` addresses rows `WHERE id = ?`. New tables should have `id INTEGER PRIMARY KEY AUTOINCREMENT`, `created_at` and `updated_at` to work with them (inferred).
- **`daolab sync` deletes reservations.**
  - Removing a setup from the YAML deletes its reservations, then the setup (`service/catalog.py:564-565`).
  - Removing a use case deletes its reservations, then the use case (`:578-579`).
  - Only `released`/`failed` reservations can be in the way (`:558-563`, `:572-577`), and **saved snapshots come from exactly those reservations.**
  - With `foreign_keys=ON`, a plain FK (`NO ACTION`) into `reservations` makes the sync fail with `FOREIGN KEY constraint failed`. `ON DELETE SET NULL` keeps the row (verified: `probe-out.txt` [4]).
- `catalog.sync` overwrites a fixed `fields` dict on every setup row (`service/catalog.py:520-526`). Any new `setups` column is left alone by sync unless it is added to that dict.
- **Residue kinds are code, not schema:**
  - `RESIDUE_KINDS = ("vm", "vf", "dpu_session", "dpu_app", "dpu_hugepages", "ip")` (`service/runner.py:73`), with `CHECK_FOR_KIND` at `:74-76`.
  - `residue_item` raises on an unknown kind (`:81-84`). `parse_residue` splits on the first `:` (`:87-90`).
  - Residue reaches the DB only as text in `jobs.error` and `audit_events.detail` (`service/engine.py:307-311, 359, 377`).
  - `runner.py` is Akshay-owned (AGENTS.md).
- Job kinds: `models.JOB_KINDS = ("deploy", "cleanup", "health_probe", "reconcile")` (`service/models.py:590`); `create_job` rejects anything else (`:616-617`).
- `plan/` on `origin/rama` has no mention of snapshots, keys, challenges, approvals, `home:lab` or tripwire (grep, 0 hits).
- **No B-103..B-114 row exists in `plan/10-backend-tasksheet.md` on any branch** (`origin/rama`, `origin/akshay`, `origin/akshay-p8-gate`, `origin/main`). Rama will add `snapshot-design.md` §8 in his errata v1.2 docs PR (Rama, 23 Nov 19:5x; `B-104-DDL.md` §0 R-1).

---

## 2. Current DDL (fresh `origin/rama` DB, `user_version` 11)

These are dumped from `sqlite_master` after `schema.sql` plus migrations 1–11 (`effective-ddl-v11.txt`). The comments say where each column comes from.

```sql
CREATE TABLE users (                                     -- schema.sql:5-15
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    role        TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    token_hash  TEXT    NOT NULL UNIQUE,                 -- SHA-256 hex of the bearer token
    ssh_pubkey  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
, token_expires_at TEXT);                                -- m11, migrations.py:251-255

CREATE TABLE setups (                                    -- schema.sql:24-34
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    pool_id      INTEGER NOT NULL REFERENCES pools(id),
    capabilities TEXT    NOT NULL DEFAULT '[]',
    links        TEXT    NOT NULL DEFAULT '[]',
    diagram      TEXT    NOT NULL DEFAULT '',
    yaml_path    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
, availability TEXT NOT NULL DEFAULT 'available'
    CHECK (availability IN ('available', 'quarantined', 'maintenance')));   -- m3, :103-110

CREATE TABLE reservations (                              -- schema.sql:66-83
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    usecase_id         INTEGER NOT NULL REFERENCES usecases(id),
    setup_id           INTEGER NOT NULL REFERENCES setups(id),
    state              TEXT    NOT NULL DEFAULT 'queued'
                       CHECK (state IN ('queued', 'provisioning', 'ready', 'active',
                                        'cleaning', 'quarantined', 'released', 'failed')),  -- m6 rewrite
    requested_start_at TEXT    NOT NULL,
    start_at           TEXT    NOT NULL,
    end_at             TEXT    NOT NULL,
    diy                INTEGER NOT NULL DEFAULT 0 CHECK (diy IN (0, 1)),
    diy_templates      TEXT,
    retries            INTEGER NOT NULL DEFAULT 0,
    failure_reason     TEXT,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
, mode TEXT NOT NULL DEFAULT 'asap' CHECK (mode IN ('asap', 'at')),          -- m1, :59-64
  turnaround_min INTEGER NOT NULL DEFAULT 0,                                 -- m1
  ssh_pubkey TEXT,                                                           -- m10, :239-248
  access_password TEXT);                                                     -- m10
CREATE INDEX idx_reservations_setup ON reservations(setup_id, start_at, end_at);   -- schema.sql:99
CREATE INDEX idx_reservations_state ON reservations(state);                         -- :100
CREATE INDEX idx_reservations_user  ON reservations(user_id);                       -- :101

CREATE TABLE jobs (                                      -- m5, migrations.py:135-164
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL
                   CHECK (kind IN ('deploy', 'cleanup', 'health_probe', 'reconcile')),
    state          TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt        INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    trace_id       TEXT,
    log_path       TEXT,
    steps          TEXT    NOT NULL DEFAULT '[]',
    error          TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE INDEX idx_jobs_reservation ON jobs(reservation_id, id);
CREATE INDEX idx_jobs_state ON jobs(state, id);
```

**Verified:** `git diff 4ec115e^ origin/rama -- service/migrations.py service/schema.sql` is empty. Migrations 1–11 on `rama` are byte-identical to the base the reverted attempt was built on, so the reverted v12/v13 applied to exactly this schema.

Side note, outside B-104: `reservations.access_password` (m10) is an existing plaintext secret column. E-10 covers new Phase 8 code only, so it is not changed here.

---

## 3. Fingerprint of the reverted v12/v13

**Source:** `4ec115e:service/migrations.py` (only this commit of the three touched the schema).
- `git show --stat` shows `2490387` and `e75c2b5` change no migration or schema file.
- `9c8a089` reverts all three; its stat includes `service/migrations.py | 86 -----`.

**Verified on a synthetic DB:** `probe-out.txt` [1] and [3].

### v12 `_m12_snapshots` (`4ec115e:service/migrations.py:258-307`, registered `:353` as `Migration(12, "snapshots", ...)`)

| Object | DDL | Line |
|---|---|---|
| table `user_keys` | `user_id INTEGER PRIMARY KEY REFERENCES users(id)`, `public_key TEXT NOT NULL`, `wrapped_private_key TEXT NOT NULL` (AES-GCM under HKDF(token)), `kdf_salt TEXT NOT NULL`, `key_version INTEGER NOT NULL DEFAULT 1`, `created_at`, `updated_at` | `:271-279` |
| table `snapshots` | `id INTEGER PRIMARY KEY AUTOINCREMENT`, `user_id INTEGER NOT NULL REFERENCES users(id)`, `setup_id INTEGER NOT NULL REFERENCES setups(id)`, `reservation_id INTEGER NOT NULL REFERENCES reservations(id)`, `usecase_id INTEGER NOT NULL REFERENCES usecases(id)`, `hw_class TEXT`, `state TEXT NOT NULL DEFAULT 'saving' CHECK (state IN ('saving', 'saved', 'failed', 'deleted'))`, `path TEXT`, `size_bytes INTEGER`, `sha256 TEXT`, `wrapped_dek TEXT`, `header TEXT NOT NULL DEFAULT '{}'`, `failure_reason TEXT`, `created_at`, `updated_at` | `:282-299` (CHECK `:289-290`) |
| index `idx_snapshots_owner` | `ON snapshots(user_id, setup_id, created_at)` | `:300-302` |
| index `idx_snapshots_reservation` | `ON snapshots(reservation_id)` | `:303-304` |
| column `reservations.save_snapshot` | `INTEGER NOT NULL DEFAULT 1 CHECK (save_snapshot IN (0, 1))`. **Default 1** (`PRAGMA table_info` dflt_value `'1'`) | `:305-306` |
| column `setups.snapshot_bdfs` | `TEXT NOT NULL DEFAULT '[]'` | `:307` |

### v13 `_m13_restore_jobs` (`4ec115e:service/migrations.py:310-339`, registered `:354` as `Migration(13, "restore_jobs", ...)`)

| Object | DDL | Line |
|---|---|---|
| `jobs.kind` CHECK rewritten in place (m6 method) | `'deploy', 'cleanup', 'health_probe', 'reconcile')` → `... 'reconcile', 'restore')` | `:310-311`, `:323-337` |
| column `jobs.snapshot_id` | `INTEGER REFERENCES snapshots(id)` | `:338` |
| column `jobs.secret` | `TEXT`: the snapshot DEK while the job runs (the rejected design) | `:339` |

A reverted-v13 DB is at `user_version` 13. A DB that ran only the reverted v12 is at 12 (`probe-out.txt` [3]: `user_keys` present, `jobs.secret` absent).

### The trap, reproduced (`probe-out.txt` [2])
Take today's `apply()`, add placeholders 12/13 and a naive 14 (`CREATE TABLE IF NOT EXISTS snapshots (... public_id ...)` + `add_column(save_snapshot DEFAULT 0)`), and run it on a reverted-v13 DB:
- the result is `applied [14]`, `user_version 14`;
- `snapshots` still has the **old** columns (no `public_id`, still `wrapped_dek`);
- `save_snapshot` still defaults to **1**.

Nothing errors. This is why the guard is needed, and why it must look at the schema, not at `user_version`.

---

## 4. Name collisions: reverted fingerprint vs the new design

The new names come from design:118-121 (tables `snapshots`, `keys`, `key_challenges`, `admin_approvals`; `reservations.save_snapshot`; `snapshots.public_id`; `key_challenges` fields) and from the brief (jobs changes for capture/restore).

| Name | Reverted | New design | Kind of collision | Effect without a guard |
|---|---|---|---|---|
| table `snapshots` | v12, old columns, state `saving/saved/failed/deleted` | design:118, needs `public_id` and states `capturing`/`saved` (design:119-120) | **same table name, different shape** | `CREATE TABLE IF NOT EXISTS` keeps the old table; inserts with `public_id` fail at runtime, or the old `wrapped_dek` survives |
| `snapshots.user_id`, `.setup_id`, `.reservation_id`, `.state`, `.created_at`, `.updated_at` | v12 | needed by the new table too (inferred) | same column names | masks the difference: a has-column check on these would pass on the old table |
| `snapshots.state` value `'saved'` (and `'failed'`) | v12 CHECK | design:120 `saved`; `failed` inferred from D-7-4 (design:134) | same value, different state machine | code reading `state='saved'` would accept old rows |
| index `idx_snapshots_owner`, `idx_snapshots_reservation` | v12 | not in the design; a natural name to reuse | same index name | `CREATE INDEX IF NOT EXISTS` skips silently, leaving an index on the old columns |
| `reservations.save_snapshot` | v12, `DEFAULT 1` | design:39-41, 119: `CHECK IN (0,1)`, **migration default 0**, 0 = PRESERVE | **same name and CHECK, opposite default** | `add_column` skips (the column exists), so a legacy row inserted later without the column gets 1, which the new engine reads as "stamped, capture and wipe". This **inverts the PRESERVE rule** (design:25, 37-41) |
| `jobs.kind` value `'restore'` | v13 CHECK rewrite | only if B-104 adds a `restore` kind (open question) | same value | an m6-style rewrite with "skip if `'restore'` is already present" would skip on an old DB |
| `jobs.snapshot_id` | v13, `REFERENCES snapshots(id)` (no ON DELETE) | likely needed (inferred) | same column name, different FK action | `add_column` skips and keeps the old FK |
| `jobs.secret` | v13 | **forbidden** (E-10, design:73) | — | a secret column would survive in a "new" DB |
| table `user_keys` | v12 (token-wrapped private key) | none; the design uses `keys` (design:118) | no collision, but a leftover | the rejected key material stays in the DB |
| `setups.snapshot_bdfs` | v12 | not in the design (replay is "roles by name", design:108) | reserved name | if reused later, `add_column` would skip and keep old data |

No collision: `keys`, `key_challenges`, `admin_approvals`. None of these existed in the reverted schema.

**Conclusion:** every new name that *does* collide either keeps an old shape silently or flips a safety default. The guard must refuse a DB with any reverted-only marker before any migration runs (proposal: `B-104-DDL.md` §2). New B-104 code must also avoid the reverted index names and must never create `user_keys`, `jobs.secret`, `snapshots.wrapped_dek` or `setups.snapshot_bdfs`.
