# B-104 PR B (migration 14): read-only discovery

Date: 2026-11-24. Base: `origin/rama` = `a67d4ac` (PR #9 merged). Spec: DDL v4,
`origin/akshay-p8-gate:plan/design/p8-impl/b104-akshay/B-104-DDL.md` (sha256 `f4169f1d…`).
Rama, 24 Nov: Q-13, the sweep rule and the table order are SIGNED OFF; v4 stands unchanged.

**Method.** Nothing in the repo was edited, checked out or branched. Code was read with
`git show origin/rama:…` / `git grep … origin/rama`. Probes ran with `python -I` on an exported
copy of the base (`git archive origin/rama service | tar -x -C /tmp/b104b-probe/`) and on
`:memory:` or `/tmp/b104b-probe/*.db` databases only. Probe scripts and their raw outputs are
kept next to this file.

**Citation keys.**
- `file:line` with no prefix = `origin/rama` at `a67d4ac`.
- `v4:NN` = DDL v4 line NN.
- `probe_*:Xn` = a labelled check in `~/notes/b104b/probe_*-out.txt`.

---

## 1. Base

- `git rev-parse origin/rama` → `a67d4acc9741c2ee228abb23e30e0982f26700b4`. ✅
- `git diff --stat a03f858 a67d4ac -- service tests plan`: 4 files, +540/−1. This is PR A
  (`migrations.py` +52, `tests/test_migrations_guard.py` +482, plus `cli.py` and `engine.py`).
  So the v4 line citations into `migrations.py` made at `a03f858` are now off (see §9.7).
- `origin/akshay-p8-gate` = `ab844c3`.

## 2. migrations.py: declaration, helpers, runner

**Declaring a migration**
- `@dataclass(frozen=True) class Migration(version: int, name: str, fn)` (`service/migrations.py:32-36`).
- It is appended to `MIGRATIONS` (`:271-285`). Today that is 1..11 plus `Migration(12,
  "burned_reverted_v12", …)` and `Migration(13, "burned_reverted_v13", …)` (`:283-284`).
- Header rules (`:11-17`):
  1. append only, N = previous + 1, never edit or reorder;
  2. `fn` must be idempotent (`add_column` / `CREATE … IF NOT EXISTS`);
  3. `fn` must not manage transactions.

**Helpers** (`:41-58`)
- `has_table(conn, t)`: a `sqlite_master` lookup with `type='table'` (`:41-44`).
- `has_column(conn, t, c)`: `PRAGMA table_info({t})`, comparing names only (`:47-48`).
- `add_column(conn, t, c, ddl)`: if `has_column`, return False; else `ALTER TABLE t ADD COLUMN c ddl`
  and return True (`:51-58`).
  - **It guarantees idempotence by column name only.** It never compares type, default or
    CHECK. An existing column with a different definition is silently kept. That is exactly
    why the reverted-schema guard exists (M6, `:299-302`).
  - On a missing table it raises `OperationalError` from the ALTER.

**Runner `apply()`** (`:344-372`)
- `current_version > latest_version()` → `SchemaTooNew` (`:351-354`).
- `refuse_reverted_schema(conn)` runs on every call, before any migration (`:355`).
- For each pending migration:
  - `BEGIN IMMEDIATE` (`:360`);
  - re-read the version inside the lock and ROLLBACK if it lost a race (`:362-364`);
  - `m.fn(conn)` (`:365`);
  - `PRAGMA user_version = N` in the same transaction (`:366`);
  - `COMMIT` (`:367`);
  - on any `BaseException`: `ROLLBACK` and re-raise (`:368-370`).
- The function expects an autocommit connection (docstring `:347`).

**Connection and FK state during migrations**
- `models.connect` uses `isolation_level=None` (`service/models.py:30`), WAL (`:32`) and
  `PRAGMA foreign_keys=ON` (`:33`).
- `models.init_db` runs `executescript(schema.sql)` first, outside any transaction, then
  `migrations.apply` (`models.py:37-43`).
- So **every migration runs with foreign_keys=ON**: `probe_v13` prints `foreign_keys during init: 1`.
  It cannot be switched off inside the migration's transaction anyway: `PRAGMA foreign_keys=OFF`
  inside `BEGIN IMMEDIATE` is a silent no-op (`probe_features:A9`).
- The test helper `_conn` in `tests/test_migrations_guard.py:97-101` also turns FKs on.

**Callers**
- `cli.cmd_init` → `models.init_db` (`cli.py:42-45`). It catches `SchemaTooNew` and
  `RevertedSchema`, then exits 1 via `_die` (`cli.py:46-47`), and prints `schema: vN …` (`cli.py:52-53`).
- `engine.main` refuses `have != want` with "run `daolab init` first" (`engine.py:629-633`), then
  runs the guard (`engine.py:634-638`, added in PR A).

**`_check_contiguous`**
- It refuses any list that isn't exactly 1..N in order (`:288-293`).
- It is called at import (`:295`), so a `Migration(14)` without 12 and 13 would fail to import.

**`latest_version()`**
- `MIGRATIONS[-1].version if MIGRATIONS else 0` (`:336-337`). It is 13 today and becomes 14
  automatically once the migration is appended.

## 3. How migration 6 rewrote a CHECK "in place"; the jobs.kind text today

**Mechanism: a `writable_schema` text edit, not a table rebuild** (`_m6_reservation_states`, `:171-195`)
1. Read the stored table SQL: `SELECT sql FROM sqlite_master WHERE type='table' AND name='reservations'` (`:180-182`).
2. Skip if already done: `if "'quarantined'" not in sql` guards the rewrite (`:183`).
3. Find the old text: a plain substring test for `_OLD_STATES = "'tearing_down', 'released', 'failed'"`
   (`:184`, `:198`). **If it isn't found**, raise
   `RuntimeError("migration 6: cannot find the reservations.state CHECK to rewrite (expected … in the table definition)")`
   (`:185-187`). `apply()` then rolls back the transaction and `user_version` stays unchanged.
4. `version = PRAGMA schema_version` (`:188`), then `PRAGMA writable_schema = ON` (`:189`).
5. `UPDATE sqlite_master SET sql = ?`, using `sql.replace(_OLD, _NEW, 1)`: one replacement (`:190-192`).
6. `PRAGMA schema_version = version + 1` (`:193`), then `PRAGMA writable_schema = OFF` (`:194`).
7. Data fix-up: `UPDATE reservations SET state='cleaning' WHERE state='tearing_down'` (`:195`).
   It runs on every call, so it is idempotent.

**Is the connection reopened?** No.
- The `schema_version` bump makes SQLite re-parse the schema on the next statement, on this
  connection and on every other one.
- Proven on the jobs shape: on the same connection, inside the same transaction, `'restore'` is
  insertable right after the rewrite (`probe_features:A8`), and a new connection sees it (`:B2`).
- The existing m6 test `test_other_connections_see_the_new_rule` (`tests/test_reservation_states.py:128`)
  proves the same for reservations.
- ROLLBACK undoes both the ADD COLUMN and the text edit (`probe_features:A8`;
  `tests/test_reservation_states.py:161-180`).

**`integrity_check`.** m6 does not run it. SQLite's documented writable_schema procedure ends
with `PRAGMA integrity_check`. On the probe DB after a dry-run migration 14, `integrity_check` is
`ok` and `foreign_key_check` is empty (`probe_features:B5`). Recommendation: assert both in the
PR B tests. This is test-only and no code change.

**Interaction with foreign_keys=ON**
- A text edit reads and writes only `sqlite_master`. **No row of any table is copied, deleted or
  re-inserted, so no FK check runs and no ON DELETE action can fire.** The m6 docstring says so
  too (`:174-176`).
- Proof by contrast (`probe_rebuild-out.txt`): with FKs on, the 12-step **rebuild** of a parent
  table does `DROP TABLE reservations`. That is an implicit `DELETE FROM`, which **cascade-deleted
  the child `jobs` row (1 → 0)**.
- The same shape under m6's text edit kept the row (1), and `integrity_check` was ok.
- On the real schema, `jobs`, `ip_leases` and `health_snapshots` reference `reservations ON DELETE CASCADE`
  (`migrations.py:92`, `:150`, `:230`). So a rebuild with FKs on would silently delete them.
- Nothing references `jobs` today. Still, migration 14 must keep using the text edit for `jobs.kind`,
  as v4 §3.8 says (v4:509-514), and never a rebuild.

**The exact jobs.kind CHECK on a freshly migrated v13 DB** (both venvs; `probe_v13-out.txt`)
- The stored SQL starts `CREATE TABLE jobs (`. SQLite drops the `IF NOT EXISTS` when it stores the text.
- The CHECK line is, byte for byte, with 27 leading spaces:
  ```
                             CHECK (kind IN ('deploy', 'cleanup', 'health_probe', 'reconcile')),
  ```
- `_OLD_JOB_KINDS = "'deploy', 'cleanup', 'health_probe', 'reconcile')"` (v4:505) occurs **exactly
  once**, and `'restore'` occurs 0 times.
- That text comes from m5 (`migrations.py:151-152`). It was introduced in `8466b70` and changed
  by no commit on any branch: `git log --all -G"kind IN \("` shows only `8466b70`.
  `schema.sql` has no jobs table. So every DB at v ≥ 5 has this exact text.
- After `ALTER TABLE jobs ADD COLUMN snapshot_id …`, SQLite appends
  `, snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL` before the closing `)`. The
  CHECK text is untouched and still found (`probe_features:A8`, `:B2`).
- The reverted `4ec115e` did the rewrite *before* its add_column (`4ec115e:service/migrations.py:327-338`).
  v4's order (add_column, then rewrite) works equally well.

## 4. refuse_reverted_schema: markers M1..M6

**How each marker is detected.** `_REVERTED_MARKERS`, a tuple of (fixed label, check) pairs
(`migrations.py:312-320`):

| # | Label | Detection |
|---|---|---|
| M1 | `table user_keys` | `has_table(c, "user_keys")` (`:313`) |
| M2 | `column jobs.secret` | `has_column(c, "jobs", "secret")` (`:314`) |
| M3 | `table snapshots without public_id` | `has_table(snapshots) and not has_column(snapshots, public_id)` (`:315-316`) |
| M4 | `column snapshots.wrapped_dek` | `has_column` (`:317`) |
| M5 | `column setups.snapshot_bdfs` | `has_column` (`:318`) |
| M6 | `reservations.save_snapshot default 1` | `_save_snapshot_default_is_1` (`:307-309`, `:319`) |

**How M6 reads the default**
- `any(row[1] == "save_snapshot" and row[4] == "1" for row in PRAGMA table_info(reservations))`.
- `row[4]` is `dflt_value`, the *SQL text* of the declared default, compared as a string.
- `DEFAULT 1` → `'1'`, so it fires. `DEFAULT 0` → `'0'`, so it doesn't. (A `DEFAULT (1)` or
  `DEFAULT '1'` would give `'(1)'` / `"'1'"` and not fire, but nothing writes those.)
- `refuse_reverted_schema` raises `RevertedSchema` naming only the labels (`:323-331`). It runs in
  `apply()` (`:355`) and in the engine (`engine.py:634-638`).

**The v4 DDL creates none of M1..M6**
- v4's tables are `keys`, `key_challenges`, `admin_approvals` and `snapshots`. `snapshots` has
  `public_id` (v4:347) and no `wrapped_dek`.
- v4 adds no `jobs.secret` and no `setups.snapshot_bdfs`.
- `save_snapshot` is `DEFAULT 0` (v4:488-489).
- Proven on a v13 file DB after the full v4 DDL: the guard passes, no marker fires individually,
  and `save_snapshot` `dflt_value` = `'0'` (`probe_features:B3`, `B3b`, `B4`; also `A2b`).
- The existing test `test_m6_marker_is_only_default_1` (`tests/test_migrations_guard.py:380-387`)
  already pins "DEFAULT 0 is not a marker".
- `test_g6_guard_passes_on_our_own_latest_schema` (`:293-299`) will automatically cover v14.
- No new index reuses `idx_snapshots_owner` / `idx_snapshots_reservation` (`probe_features:B6`).

## 5. SQLite in both venvs, and every v4 feature

**Versions**
- `~/p8-b104a` has no venv. Both venvs are in **`~/b104a`**: `.venv` and `.venv-220`.
- Both are Python 3.12.13, **SQLite 3.53.1**: uv's python-build-standalone,
  `~/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu`.
- SQLite is compiled in: `_sqlite3` is a builtin module with no `__file__` and no `libsqlite3`.
- The laptop's system `/usr/bin/python3` (3.10.12) links **SQLite 3.37.2** (Ubuntu 22.04). It is
  not a valid controller (floor 3.12), but it gives an older-SQLite reference point.

**Results.** Everything passed: **18 PASS / 0 FAIL on both venvs, and 18/0 on system SQLite 3.37.2**
(`probe_features-out.txt`, `probe_features-sys-sqlite3372-out.txt`).

| Feature | Probe | Result |
|---|---|---|
| Partial UNIQUE index (`WHERE state = 'active'`) | A1 | 2nd active refused; many `retired` allowed |
| ADD COLUMN `NOT NULL DEFAULT 0 CHECK (x IN (0,1))` on a table with rows | A2 | legacy rows = 0; `2` refused |
| ADD COLUMN CHECK tested against existing rows | A3 | yes: a bad default with rows present → `CHECK constraint failed`; the same ALTER on an empty table is accepted. (SQLite ≥ 3.37.0 behaviour; harmless here because `DEFAULT 0` satisfies the CHECK) |
| ADD COLUMN `REFERENCES … ON DELETE SET NULL` with FKs on | A4 | works (default NULL); parent delete → NULL; a bad FK is refused |
| … with a non-NULL default | A4b | refused: `Cannot add a REFERENCES column with non-NULL default value`. v4's `jobs.snapshot_id` has no default, so it is fine |
| Composite FK `(key_id, user_id) → keys(id, user_id)` | A5 | own key ok; another user's key refused. Without `UNIQUE (id, user_id)` on `keys`: `foreign key mismatch`, so the v4:229 UNIQUE is required |
| CHECK with `GLOB '[^…]'`, `LIKE`, `length()` | A6 | 29 cases as intended: bech32, hex, the lowercase charset, `NOT LIKE 'age-secret-key-%'` (case-insensitive), the timestamp shape |
| FK to a not-yet-created table at CREATE time | A7 | accepted. So the table order is a convention, not a requirement |
| ADD COLUMN + writable_schema rewrite in one transaction | A8 | works; ROLLBACK undoes both; `SQLITE_DBCONFIG_DEFENSIVE` = False |
| The whole v4 DDL as one migration 14 on a v13 file DB (13 statements: 4 tables, 9 indexes; 3 add_column; the rewrite; run twice in one txn) | B1-B6 | legacy rows `(0, 0, NULL, 'cleanup')`; `restore` accepted, `bogus` refused; guard passes; `integrity_check` ok; FK check empty |

**Caveats (notes, not failures)**
- The timestamp GLOB checks shape only, so `2026-13-99T99:99:99Z` is accepted. That is by design (v4:196).
- A 64-byte **BLOB** of hex characters passes the `nonce_sha256` CHECK (`probe_features:A6`,
  `probe_q9-out.txt`). There is no `typeof()` check. An S-shaped BLOB is still refused by the
  charset/LIKE (`probe_q9`). See §9.9.

## 6. Hard-coded 13 / latest_version / migration list / jobs.kind (service/ and tests/)

**service/**
- `migrations.py:283-284` (the list), `:336-337` (latest), `:351-355`; `engine.py:629-638`;
  `cli.py:46`, `:52`. All are dynamic, with **no literal 13**.
- **Job kinds:**
  - `models.JOB_KINDS = ("deploy", "cleanup", "health_probe", "reconcile")` (`models.py:590`);
  - `MUTATING_JOB_KINDS` (`:591`);
  - `create_job` refuses unknown kinds (`models.py:616-617`);
  - `engine.EXPECTED_STATES` (`engine.py:105-110`) and the kind dispatch (`engine.py:436-443`, `else:  # health_probe`);
  - `runner.method_for` (`runner.py:834-836`);
  - literal `('cleanup', 'reconcile')` in SQL at `models.py:522` and `:673`;
  - `api.py:525`, `:623-626`, `:938`, `:967-969`.
  - None changes in PR B: `restore` stays out of `models.JOB_KINDS` by scope.
- **No new column leaks into API JSON.** The only `dict(row)` is `models.list_users`, which selects
  explicit columns (`models.py:95-103`). API responses are built by hand.

**tests/: two existing tests will FAIL when latest becomes 14** (open updates needed)

| Test | Why it fails | Minimal update |
|---|---|---|
| `tests/test_jobs.py:31-37` `test_migration_creates_jobs_table` | asserts the **exact** `jobs` column set; `snapshot_id` is new | add `"snapshot_id"` to the set |
| `tests/test_migrations_guard.py:437-455` `test_engine_refuses_reverted_v13_db_at_latest_version` | `:441` asserts `latest_version() == 13`. At 14, a reverted v13 DB (user_version 13) is refused by the engine's **version** check, not the guard, so the error text differs | after `_reverted_v13(...)`, hand-set `PRAGMA user_version = latest_version()` (a reverted DB at the latest version, the case the engine guard exists for); assert `== migrations.latest_version()`; update the comment at `:420-422` ("latest_version() is 13"). Optional: add `(_reverted_v13, 13)` to the parametrize at `:471` |

**Tests that keep passing (checked)**
- `test_migrations.py:44-59`, `:62-70`, `:82-87`, `:96-116`, `:120-125`, `:136-170` all derive from
  `MIGRATIONS` / `latest_version()`.
- `test_jobs.py:40-56` (v4 → latest), `:78-99`, `:131-132`.
- `test_reservation_states.py:107-180` (v5 → latest; m6 tests index `MIGRATIONS[5]`).
- `test_migrations_guard.py`: G-1…G-11, `test_t1` (`>= 13`, `:396`), `test_t2` (`MIGRATIONS[:13]`,
  `:414`), `test_m6_marker_is_only_default_1`.
- `test_runner.py:278`, `test_recovery.py:88` and `:107-109` loop over `JOB_KINDS` or compare
  `SELECT *` on the same schema.

**Wording that goes stale but still passes**
- The `test_g3` docstring (`test_migrations_guard.py:217-218`): "at the latest version as far as
  user_version goes" is no longer true at 14.
- The module docstring `:1-3`.

## 7. DDL in §3 vs ddl-check.py; local vs branch copies

**ddl-check.py contains no DDL of its own.** It *parses* the doc (`ddl-check.py:27-38`):
- the four ```` ```sql ```` blocks that contain `CREATE TABLE`, indexes included;
- the three `add_column(conn, "t", "c", "ddl")` calls (regex, single-line string);
- the `_OLD/_NEW_JOB_KINDS` strings;
- the §3.5 retention prune (`:288`).

So the §3 DDL and the DDL under test are **identical by construction**. The differences are in
what ddl-check re-implements around the DDL:

1. **Transactions.** It runs the CREATEs (`executescript`, `:31-32`) and the add_columns (`:35-36`)
   in autocommit. Only the jobs.kind rewrite is inside `BEGIN IMMEDIATE` (`:56-59`). So it never
   ran the whole of §3.1 as *one* transaction. `probe_features` Part B did, and it passed.
2. **The rewrite function** (`:41-53`) matches v4 §3.8 (v4:510-514) step for step. Its
   `RuntimeError` text is shorter: it lacks "… to rewrite …" plus the expected-text suffix.
3. **CAS statements.**
   - The first `CAS` (`:119-120`) omits the `user_id` / `purpose` / `target_ref` conditions of v4:322-326.
   - `CAS_FULL` (`:201-203`) has them, with `user_id = 1` hard-coded.
   - `EXEC` (`:204-205`) = v4:481 plus `updated_at`.
4. **Base.** The docstring says "fresh origin/rama v11 DB". The recorded `ddl-check-out.txt` line 1
   is `user_version 11` (base `a03f858`).
   - Re-run on `a67d4ac` (`ddl-check-a67d4ac-.venv*.txt`): the only diff is line 1
     (`user_version 13`). The rest is byte-identical, and both venvs are identical.
5. **Coverage gaps** vs §6.2: T-8 (cross-user composite FK), T-14 (column-name rule), T-3 (legacy
   rows across 14) and most of T-9 (expired, wrong purpose or target) are not exercised. See §8.

**Copies (sha256, first 16)**

| File | Branch | Local `~/notes/b104/` |
|---|---|---|
| `B-104-DDL.md` | `f4169f1dd13177fd` | identical (full `f4169f1d…ea95732`) |
| `ddl-check.py` | `027fce37ea7cbfe1` | identical |
| `probe.py` | `9b8a4f0924a0c047` | identical |
| `B-104-DDL.v3-v4.diff` | `8606064e1f6a5068` | identical |
| `FINDINGS.md`, `ddl-check-out.txt`, `probe-out.txt` | | identical |
| `README.md`, `SHA256SUMS` | | branch only |

## 8. DDL v4 §6.2 T-1..T-21 → tests/test_migration_14.py

All tests use synthetic DBs (`:memory:` / `tmp_path`). SQL that v4 gives as text (the CAS
statements, the §3.5 prune) is **copied literally into the test file**, with a comment citing the
v4 section. Tests must not parse the doc: the doc lands only in commit 2, so a parsing test in
commit 1 would fail on its own.

| T | Proposed test(s) | Replaces ddl-check scenario | Note |
|---|---|---|---|
| T-1 | (exists) `test_migrations_guard.py::test_t1_fresh_db_applies_placeholders_12_and_13`, `test_migrations.py::test_fresh_db_reaches_latest`; new `test_t1_fresh_db_reaches_14_with_new_tables_and_columns` | "DDL parsed:" line | |
| T-2 | (exists) `test_migrations_guard.py::test_t2_placeholders_change_nothing_but_user_version` | — | no new test needed; `MIGRATIONS[:13]` stays right |
| T-3 | `test_t3_upgrade_keeps_legacy_rows_with_preserve_defaults` (parametrize: from v11 via `_at_version`, from v13) | `[jobs] kind cleanup (legacy kind)` (partial) | new coverage |
| T-4 | `test_t4_m14_twice_in_one_transaction_is_a_noop` (dump equal); `init_db` twice = `[]` is covered by `test_migrations.py::test_init_twice_is_noop` | "jobs.kind rewrite, 1st/2nd run" | |
| T-5 | `test_t5_check_violations_raise` (parametrized table of (table, column, bad value)); `test_t5_s_shaped_canary_refused_by_every_new_text_column` (iterate `PRAGMA table_info` of the 4 tables, `TEXT` columns); `test_t5_timestamp_shape_refused` | `[keys]` recipient cases, `[key_challenges]` nonce/target_ref, `[snapshots]` public_id/restore_incomplete, `[admin_approvals]` S-shaped/unknown action, `[timestamp shape]`, `[S-shaped canary]` | |
| T-6 | `test_t6_two_admin_and_different_admin_rules` | `[keys]` self-activation / no activator / other admin; `[admin_approvals]` pending+decided_by, requester decides, approved w/o decided_by, decided_by w/o decided_at, owner cancel after approval, executed_at on cancelled | |
| T-7 | `test_t7_uniqueness` | second pending key, second active key, second pending approval, second snapshot for a reservation | add duplicate `public_id` / `recipient` / `fingerprint` / `nonce_sha256` (not in ddl-check) |
| T-8 | `test_t8_composite_fk_refuses_another_users_key` (challenge and snapshot) | **none** | new coverage |
| T-9 | `test_t9_single_use_challenge_cas` (parametrize: replay, expired, wrong purpose, wrong target, revoked key, retired key) | "CAS consume ch 1 first/replay"; "CAS rowcount" of the revoked key in the T-16 block | ⚠ This tests v4's SQL text, not product code: no helper exists until B-105 |
| T-10 | `test_t10_sync_style_deletes_keep_snapshot_and_block_setup_removal` (raw `DELETE` as `catalog.py:564-565`) | `[FK actions vs daolab sync]` | ⚠ via `catalog.sync` it is a raw `IntegrityError` today, not a `SyncError` (later task, Q-20). Test the SQL, not sync |
| T-11 | `test_t11_jobs_snapshot_id_set_null_on_snapshot_delete` | "jobs.snapshot_id after the snapshot row is deleted" | |
| T-12 | `test_t12_restore_kind_accepted_bogus_refused_old_kinds_kept`; `test_t12_rewrite_fails_loudly_if_kind_check_not_found` (mirrors `test_reservation_states.py:153`); `test_t12_failure_after_rewrite_rolls_back` (mirrors `:161-180`); `test_t12_other_connections_see_restore` (mirrors `:128`) | `[jobs]` | v4:682 cites `test_reservation_states.py:163`; the tests are at `:153` and `:161` |
| T-13 | `test_t13_no_reverted_shapes_after_14` | "jobs has a 'secret' column: False" (partial) | G-6 also pins it |
| T-14 | `test_t14_no_key_material_column_names` (regex `secret\|dek\|private\|identity\|nonce$\|password\|wrapped` over the 4 tables) | **none** | `nonce_sha256` correctly does not match `nonce$` |
| T-15 | `test_t15_delete_hold_constraints` | `[snapshots]` hold rows | ⚠ "clearing delete_after while hold_approval_id is still set" must clear `delete_after` **and** `delete_requested_by`, or the pair CHECK fires first and the test proves nothing (ddl-check does it right, `:239-240`) |
| T-16 | `test_t16_lost_key_reset_hold_then_execute` | T-16/T-19/T-20 block (reset 2) | ⚠ see below |
| T-17 | `test_t17_execute_after_rules` | `[admin_approvals]` executed before execute_after, execute_after ≤ created_at, tripwire approve+execute at once | |
| T-18 | not a pytest test: `make test` / the full suite on both venvs | — | |
| T-19 | `test_t19_owner_cancels_reset_during_hold` | T-16/T-19/T-20 block (reset 1) | |
| T-20 | `test_t20_execution_deletes_every_snapshot_of_the_revoked_key` | T-16/T-19/T-20 block (execution) | |
| T-21 | `test_t21_retention_prune_ignores_held_rows` (prune query copied literally from v4:403-408) | `[T-21 retention]` | |
| extra | `test_m14_integrity_and_foreign_key_check_clean`; `test_m14_is_one_transaction` (a failure injected after the rewrite leaves no new table or column at v13) | "integrity_check" lines | not in the T list; recommended (§3, Q-5) |

**T-n that can't be tested as written**
- **T-16, "a `pending → revoked` transition is refused":** only partly enforceable in DDL.
  `UPDATE keys SET state='revoked'` alone is refused by `CHECK ((state='pending') = (activated_by IS NULL))`.
  But the same UPDATE that also sets `activated_by` and `activated_at` is **accepted** (`probe_q9`).
  The test can assert only the first form. Transitions are a code rule.
- **T-16 / T-19 / T-20 are scenario tests of SQL the test itself runs.** They prove the DDL
  *permits* the flow and blocks the wrong steps (one-active index, the CAS on a revoked key,
  the FK effects). They cannot prove "in one transaction" or "the engine deletes files", because
  no product code exists in PR B.
- The same applies to T-16's "the approval's `execute_after` is the same instant as the rows'
  `delete_after`". A CHECK can't span tables (v4:476), so the test's assertion is about its own
  UPDATE.
- **T-9** likewise tests v4's CAS text (see the table).
- **T-18** is a suite run, not a test.

## 9. Possible issues in the DDL (reported, not fixed)

1. **The lost-key reset does not pin the key.** `admin_approvals` has no `key_id`: its target is
   `user:<id>` (v4:436, :751). Execution revokes "the key" (v4:264).
   - The old key stays `active` during the hold, so a `rotate` challenge stays answerable (v4:325).
   - A rotation during the hold (old → retired, new pending → active, v4:248) would change which
     key the execution revokes, and old-key snapshots (now `key_id` = a retired key) would not be
     "every snapshot of the revoked key".
   - Rotation is after B-106, so this is latent. It needs either a code rule ("no rotation while a
     reset is pending or approved") or a key column. Ask.
2. **A second `lost_key_reset` can be opened and approved while the first is approved but not
   executed.** `idx_admin_approvals_one_open` covers only `state = 'pending'` (v4:466-467).
   - The probe gave 2 open (approved/pending, unexecuted) resets for `user:1`, both accepted (`probe_q9`).
   - The second approval's hold UPDATE touches only unheld rows, so held rows stay matched to the
     first approval. The execution semantics for two approvals are undefined.
   - Q-11 says "the one-open index stops duplicates" (v4:752). It does, but only while pending.
3. **`keys` has no `(activated_by IS NULL) = (activated_at IS NULL)` CHECK.** `active` with
   `activated_by` set and `activated_at` NULL is accepted (`probe_q9`). `admin_approvals` has the
   analogous pair CHECK (v4:450). This is an inconsistency, low severity.
4. **`home_discard` target format.**
   - `admin_approvals.target_ref` is "the setup name", bare (v4:436, :751; ddl-check uses `tb-01`).
   - The matching `discard_home` challenge uses `setup:<name>` (v4:745), while `lost_key_reset`
     uses the prefixed `user:<id>`.
   - Two spellings for the same object across tables; code must translate. Ambiguity.
5. **The jobs.kind skip test is a substring on the whole table SQL.** §3.8 skips "if `'restore'` is
   already present" (v4:512). That is safe today, because the guard refuses the reverted v13 (M2)
   and `'restore'` appears nowhere else in `jobs`.
   - A stricter form would be: skip iff `_NEW_JOB_KINDS in sql`; else require
     `sql.count(_OLD_JOB_KINDS) == 1`. The PR A test fixture already asserts `count == 1`
     (`test_migrations_guard.py:124`).
   - This would change signed-off text, so ask rather than deviate.
6. **The m6 method has no `integrity_check`** (see §3). This is a test-side recommendation only.
7. **Stale citations and names in v4** (mostly from before PR A):
   - `_refuse_reverted_schema` (v4:50, :131) vs the actual public `refuse_reverted_schema` (`migrations.py:323`).
   - `_REVERTED_MARKERS(conn)` used as a function (v4:132) vs a tuple (`:312`).
   - §2.2 "Not covered: the engine" (v4:121-122): the engine *is* covered since PR A (`engine.py:634-638`).
   - §2.3 test-file list (v4:149): actual `tests/test_migrations_guard.py`; PR B proposes `tests/test_migration_14.py`.
   - Line numbers in `migrations.py` (old → now):
     - `_check_contiguous` `:273-280` → `:288-295`;
     - BEGIN IMMEDIATE `:308-315` → `:360-367`;
     - SchemaTooNew `:300-303` → `:351-354`;
     - loop `:305/306` → `:357/358`;
     - m6 `:167-195` → `:171-195`;
     - m5 CHECK `:147-148` → `:151-152`;
     - m8 `:211-215` → `:215-219`;
     - m10 `:239-248` → `:243-252`.
   - The `models.py` / `catalog.py` citations I checked still hold (`models.py:69-83`, `:535-546`,
     `:590-591`; `catalog.py:520-526`, `:564`, `:578`).
   - Status v4:3 says "Nothing is built"; Base v4:5 says `a03f858`.
8. **The `design:NN` and `errata:NN` citations point outside `rama`.** They refer to
   `akshay-p8-gate` and to `~/notes/p8-design-errata-v1.2.md`. At `a67d4ac` there is no
   `plan/design/` at all, so Rama's errata docs PR (R-1) has not landed. In rama, the final doc's
   citations would be unresolvable.
9. **BLOBs pass TEXT CHECKs** (no `typeof(x) = 'text'`). A 64-byte hex-char BLOB is accepted as
   `nonce_sha256` (`probe_q9`). Code binds `str`, so this is low; S-shaped BLOBs are still refused.
10. **CHECK failure messages carry the SQL comments.** `IntegrityError` text includes the CHECK
    expression *with its inline `--` comments*, e.g.
    `… -- closed lowercase charset (inferred); Q-10` (`probe_q9`). Values never appear.
    Cosmetic, but any logged error shows "(inferred)".
11. **No indexes on some FK child columns.** These are scanned on a parent delete or update:
    `jobs.snapshot_id` (SET NULL on every snapshot delete), `key_challenges(key_id, user_id)` (on
    deleting a superseded pending key, v4:277), and `snapshots.delete_requested_by`.
    Performance only, at tiny row counts. Not in v4; leave unless Rama wants them.
12. **The order of the §6.2 table is T-16, T-19, T-20, T-21, T-17** (v4:686-690). Nit.
13. **`plan/08-cli-config-ops.md:270-278` contradicts the code.** It says "no migration framework …
    a hand-applied `ALTER TABLE`", while `service/migrations.py` is the migration framework. This
    is pre-existing. AGENTS.md says to stop and report contradictions, so it is reported here.
14. **No task row.** AGENTS.md says "only edit the files listed for that task", but there is no
    B-103…B-114 row in `plan/10-backend-tasksheet.md` at `a67d4ac`. `git grep` finds only the stale
    `docs/dao-doc-verification.md:160`. The PR B file list therefore needs explicit approval.

## 10. Docs

**(a) Location**
- At `a67d4ac`, `plan/design/` **does not exist** (`git ls-tree origin/rama -- plan/design/` is empty).
- There is **no `.gitattributes` anywhere** in the tree.
- On `akshay-p8-gate`, `plan/design/p8-impl/.gitattributes` = `* -text\n` (8 bytes).

**(b) Every place documenting `daolab init`, upgrades or migrations** (grep at `origin/rama`, no `.env*` path)

| File:line | What it says | State |
|---|---|---|
| `README.md:93` | "schema.sql frozen baseline DDL; migrations.py adds the rest (12 tables, schema v11)" | stale already (v13); at 14 it is 16 tables, v14 |
| `README.md:104` | CLI verbs list | ok |
| `README.md:206` | Phase 0: `daolab init && make test` | ok |
| `plan/README.md:120` | "`service/schema.sql` baseline DDL + `service/migrations.py` (12 tables today: …)" | stale at 14 |
| `plan/README.md:169` | Phase 0 bootstrap incl. `daolab init` | ok |
| `plan/01-domain-model.md:122` | migration 6 renamed `tearing_down` | ok |
| `plan/01-domain-model.md:156-159` | "Full DDL — schema.sql … Applied by `daolab init` (idempotent …)" | baseline only |
| `plan/01-domain-model.md:113`, `:371`; `plan/02-backend-api.md:318`; `plan/06-frontend.md:85`, `:321` | `daolab init` seeds the catalog | ok |
| `plan/08-cli-config-ops.md:12`, `:31-41` | **§ `daolab init`**: "applies `service/schema.sql`, enables WAL, seeds … Idempotent". Sample output lacks the `schema: vN …` line that `cli.py:52-53` prints; migrations are not mentioned | incomplete |
| `plan/08-cli-config-ops.md:262-269` | restore in short: "… delete stale `-wal`/`-shm` files, `daolab init`, start again" | ok |
| `plan/08-cli-config-ops.md:270-278` | "Schema evolution stance — no migration framework … hand-applied `ALTER TABLE`" | **contradicts code** (§9.13) |
| `plan/08-cli-config-ops.md:291` | tokens before schema v11 have no expiry | ok |
| `plan/08-cli-config-ops.md:330` | manual Phase-0 bootstrap | ok |
| `plan/10-backend-tasksheet.md:47`, `:78` (B-03), `:101` (B-20 "v5"), `:102` (B-21 "v6") | migration tasks | historical |
| `deploy/backup/README.md:37` | after restore: `daolab init  # applies any newer migrations; no-op otherwise` | ok: the only place that says "run init to migrate" |
| `scripts/run/dev.sh:78` | runs `.venv/bin/daolab init` on every dev start (read via grep only; not run) | dev path self-migrates |
| `.github/workflows/ci.yml:33` | `pip install --upgrade pip` | grep noise |

- No `deploy/` systemd unit runs `daolab init` (`ExecStart` only in `daolab-backup.service:13`).
- No hits in `docs/`, `AGENTS.md`, `COMPLETION_LOG.md`, `Makefile`.
- **`.env.example` was not read** (per instructions). The note is about a command, not a setting,
  so I don't think it belongs there. Please check whether `.env.example` has any upgrade comment
  that should mention it.

**(c) The single best place for the note:
`plan/08-cli-config-ops.md` § `daolab init` (`:31-41`).**
- That is the operator CLI reference.
- The engine's refusal text literally says "run `daolab init` first" (`engine.py:631-632`), so it
  is where an operator looks.
- It is the section that today fails to mention migrations at all.
- `deploy/backup/README.md:37` covers only the restore path. The DDL doc is a design doc that
  operators won't read.
- Caveats:
  - editing a plan doc needs approval under AGENTS.md (§9.14);
  - fixing the stale `:270-278` and the `:38-41` sample output in the same commit is natural but
    is scope creep. Ask.
- Fallback if plan/08 is off-limits: an "Operator note" section in the final DDL doc, plus the
  PR description.
- Note text (proposed): "After upgrading to a build with a newer schema (e.g. PR B: v13 → v14),
  run `daolab init` once per DB before starting the engine. The engine refuses any version
  mismatch (`engine.py:629-633`). `daolab init` is idempotent and refuses DBs from the reverted
  Phase 8 attempt."

**(d) The v3→v4 diff**
- Location: `origin/akshay-p8-gate:plan/design/p8-impl/b104-akshay/B-104-DDL.v3-v4.diff`,
  389 lines, sha256 `8606064e…`.
- **It is a plain unified diff:** headers `--- B-104-DDL.v3.md` / `+++ B-104-DDL.md` with no
  timestamps and no `a/` `b/` prefixes, 16 hunks, 0 CR, no "No newline" markers.
- Checks:
  - `patch` applied to `~/notes/b104/B-104-DDL.v3.md` rebuilds v4 **byte-exactly** (sha256 `f4169f1d…`);
  - re-running `diff -u v3 v4` gives an identical body.
- Caveats:
  - **`B-104-DDL.v3.md` is not on any branch.** Only the diffs and v4 are, so on rama the diff is
    review material and can't be reproduced without the local v3.
  - The v3 file's own title line says "(v2)", which is cosmetic.

**(e) v4 lines that still read as pending or inferred for Q-13, the sweep and the order**

| v4 line | Text | Final wording |
|---|---|---|
| 3 | "Status: v4, signed off … Nothing is built." | signed off incl. 24 Nov; built in PR B |
| 5 | "Base: `origin/rama` `a03f858`" | `a67d4ac` |
| 39 | "Not in the sign-off list: Q-13 … status is unchanged (proposed)" | Q-13 SIGNED OFF 24 Nov |
| 186 | "Order inside `_m14_snapshots_v1` (inferred):" | SIGNED OFF 24 Nov |
| 269 | "The generic delete sweep … handles only rows with `hold_approval_id IS NULL` … (inferred)" | SIGNED OFF |
| 381 | "Holds (Q-13):" | (heading, fine) |
| 385 | "The sweep handles only `hold_approval_id IS NULL` rows (inferred)." | SIGNED OFF |
| 472 | "How each action uses it (Q-13; inferred where not stated):" | Q-13 SIGNED OFF; keep "inferred" for the tripwire/home_discard columns only if still unsigned |
| 700-702 | "Status: … The one exception is Q-13, which was not in the sign-off list" | all items signed off |
| 754 | "Q-13 — Where holds live. Not in Rama's sign-off list; status unchanged (proposed)." | APPROVED (24 Nov) |
| 758 | "The generic delete sweep handles only `hold_approval_id IS NULL` rows (inferred)." | approved |
| 759 | "… very likely settled in substance. Ask Rama to confirm it explicitly." | delete |

Also update for accuracy: v4:121-123 (engine now guarded), v4:146-151 (file list; still no plan/10
row), and the §9.7 line numbers.

---

## Proposed build plan

Worktree: a new `--no-track` worktree from `origin/rama` `a67d4ac` (never `~/p8gate`). Venvs: the
same pair as `~/b104a` (3.12.13; ansible-core 2.21.6 / 2.20.11; SQLite 3.53.1). Record
`sqlite3.sqlite_version` in every evidence header.

**Commit 1: migration 14 + tests**
- Files:
  - `service/migrations.py`: append `_OLD_JOB_KINDS` / `_NEW_JOB_KINDS`, `_m14_snapshots_v1`
    and `Migration(14, "snapshots_v1", _m14_snapshots_v1)`;
  - `tests/test_migration_14.py` (new);
  - `tests/test_jobs.py:35` (open update);
  - `tests/test_migrations_guard.py:420-422`, `:437-455` (open update).
  - No change to `cli.py`, `engine.py`, `models.py`, `schema.sql`.
- `_m14` shape:
  - one `conn.execute()` per statement, each a separate Python string, written the way m2/m5 are;
  - no `executescript` and no splitting on `;` (Risks 1-2);
  - order: `keys`, `key_challenges`, `admin_approvals`, `snapshots` (each table, then its indexes);
    then `add_column` × 3 (`reservations.save_snapshot`, `jobs.snapshot_id`, `setups.snapshot_enabled`);
    then the m6-style `jobs.kind` rewrite with the v4:513 error text;
  - DDL text copied from v4 §3.3-§3.9 verbatim, comments included unless Rama prefers them stripped (§9.10).
- Stages, each on **both** venvs, logs with headers:
  1. **red-1:** add `tests/test_migration_14.py` only, on unchanged code. Expect every new test to
     fail for the right reason (no table `keys`, `latest_version() == 13`, kind CHECK lacks
     `restore`, …). Run `test_migrations*.py`, `test_jobs.py` and `test_reservation_states.py`
     alongside them; the old ones stay green.
  2. **red-2:** the two open test updates, on unchanged code:
     - `test_jobs.py` + `snapshot_id` → FAIL (column absent): a true red.
     - The engine test with `user_version = latest_version()` still passes on 13: a guard, not a
       red. Say so in the evidence instead of calling it red.
  3. **green:** implement `_m14`. Run the new file plus the migration, jobs and reservation-state
     suites, then the **full suite**. Expect 1332 + N passed, 4 skipped, 0 failed (PR A: 1332/4/0).
  4. **Optional "trap" stage** (as in PR A): `_m14` with `"'restore'" in sql` removed from the skip
     check, to show `test_t4` catches a double rewrite. Only if Rama wants it.
- Then: `git diff --check`; produce the patch; fresh-worktree `git apply --check` → TREE MATCH;
  secret checks in Akshay's terminal; message file.

**Commit 2: docs**
- `plan/design/p8-impl/B-104-DDL.md`: v4 with the §10(e) SIGNED OFF edits, base `a67d4ac`, and the
  §9.7 corrections.
- `plan/design/p8-impl/B-104-DDL.v3-v4.diff`: copied byte-exact from the evidence branch.
- Possibly a `v4-final.diff` (Question 2).
- Possibly `plan/design/p8-impl/.gitattributes` (Question 1).
- The operator note: `plan/08-cli-config-ops.md` § `daolab init` if approved, else a section in
  the DDL doc (§10(c)).
- Verify: `sha256sum` of the copied diff = `8606064e…`; `patch` rebuilds v4 from v3; `git diff --check`.

## Risks

1. **`executescript()` inside a migration silently COMMITs `apply()`'s transaction.** Proven:
   after `executescript`, `in_transaction` is False and ROLLBACK fails with "no transaction is
   active" (`probe_executescript-out.txt`). That would break Q-5's single transaction and
   `apply()`'s rollback (`migrations.py:368-370`). Rule: `_m14` uses `conn.execute` only.
2. **`;` inside the v4 SQL comments** (e.g. `-- [design:57]; Q-6` at v4:224, `(catalog.py:564, :578); UNIQUE`
   at v4:354, `(inferred); Q-10` at v4:296).
   - A naive `split(";")` gives 30 broken pieces instead of 13 statements, and the first one fails
     with `incomplete input`. My own first dry run hit exactly this (`probe_features`).
   - Rule: keep one statement per Python string; if a split is ever needed, use
     `sqlite3.complete_statement`. Never parse the doc in product code.
3. **SQLite version.** 3.53.1 is what uv's python-build-standalone 3.12.13 bundles (statically
   linked). The production controller's SQLite may differ: a distro Python links the system
   library, and Ubuntu 22.04 has 3.37.2.
   - All 18 probes also pass on 3.37.2.
   - Floors that matter: ADD COLUMN CHECK tested against existing rows (3.37.0; harmless here),
     partial indexes (3.8.0).
   - `writable_schema` writes fail if a build enables `SQLITE_DBCONFIG_DEFENSIVE`. m6 already
     depends on this not being set.
   - Record `sqlite_version` on the controller before Phase 9.
4. **Every existing DB needs `daolab init` once before engine start.** The engine refuses
   13 ≠ 14 (`engine.py:629-633`); `scripts/run/dev.sh:78` self-migrates dev DBs.
5. **Two existing tests fail at 14** (§6). Editing them is outside a task-row file list (AGENTS.md).
6. **The DB will allow `restore` before the code knows it.** A `restore` job inserted by raw SQL
   would be claimed (`engine.py:385-398`, moved to `running`), then raise `KeyError` at
   `EXPECTED_STATES[job["kind"]]` (`engine.py:417`), leaving it `running` until stale-job recovery.
   It is unreachable through `models.create_job`, which refuses it (`models.py:616-617`). In scope
   for the later engine task.
7. **`catalog.sync` removing a setup that has snapshots.** It raises a raw FK `IntegrityError` at
   `catalog.py:565`, and the whole sync rolls back (`:580-582`). Accepted by Q-20 with a later
   `SyncError` change. Unreachable until B-109 writes snapshots.
8. **The writable_schema edit is safe only as a relaxing, text-only change.** Adding an IN-list
   value qualifies. Any future CHECK *tightening* or rebuild with FKs on could cascade-delete
   (§3, `probe_rebuild`).
9. **Spec drift.** v4's internal line citations are stale (§9.7), and design/errata citations
   resolve only on the evidence branch (§9.8). An implementer following v4's line numbers
   literally would look in the wrong places.

## Questions for Rama / Akshay

1. **`.gitattributes`:** `plan/design/` doesn't exist on rama. Should PR B add
   `plan/design/p8-impl/.gitattributes` with `* -text` (as on `akshay-p8-gate`), so the v3→v4 diff
   (and any `.diff` / `.patch` there) stays byte-exact? Or should it scope the rule to `*.diff`
   only, leaving `.md` normal?
2. **Final doc vs diff:** marking Q-13, the sweep and the order SIGNED OFF changes the doc, so it is
   no longer byte-equal to v4. Is the v3→v4 diff then enough, or should commit 2 also carry a
   `v4→final` diff? The alternative is to keep v4 byte-exact and add a dated "Sign-off 24 Nov"
   addendum section.
3. **AGENTS.md file list:** OK to edit the two existing tests (`tests/test_jobs.py:35`,
   `tests/test_migrations_guard.py:420-422` and `:437-455`) in commit 1? And to name the new file
   `tests/test_migration_14.py` (v4:149 suggested `test_migrations.py` / `test_migrations_p8.py`)?
4. **Operator note:** in `plan/08-cli-config-ops.md` § `daolab init` (recommended), and if so,
   should the same commit also fix the stale `:270-278` and the `:38-41` sample output? Or should
   the note go only in the DDL doc?
5. **jobs.kind skip condition:** keep v4's `"'restore'" in sql`, or use `_NEW_JOB_KINDS in sql`
   plus `sql.count(_OLD_JOB_KINDS) == 1` (§9.5)?
6. **Lost-key reset edge cases (§9.1, §9.2):** should rotation be blocked during a pending or
   approved reset, and should a second reset for the same user be blocked while one is approved
   but unexecuted? Should these be code rules in a later task (DDL v4 unchanged), or DDL (a key
   column; a one-open index over `pending OR (approved AND executed_at IS NULL)`)?
7. **`keys` activation pair (§9.3):** add `CHECK ((activated_by IS NULL) = (activated_at IS NULL))`,
   or accept as is?
8. **`home_discard` target (§9.4):** bare setup name in `admin_approvals`, but `setup:<name>` in
   `discard_home` challenges. Is that intended?
9. **BLOBs (§9.9), FK child indexes (§9.11), comments inside CHECKs (§9.10):** accept as is?
10. **Citations (§9.8):** in the rama copy, should `design:NN` / `errata:NN` be written as
    branch-qualified paths (`origin/akshay-p8-gate:…`), or wait for Rama's errata docs PR?
11. **Tests copy SQL literally (§8):** confirm that T-9, T-16 and T-21 should embed v4's CAS and
    prune text, rather than parse the doc.
