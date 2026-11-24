# B-104 — DDL (v4.1)

Date: 2026-11-23 (v4); 2026-11-24 (v4.1). Status: **v4.1, signed off by Rama 23-24 Nov. Built in PR B on origin/rama a67d4ac.**

- **Base:** built in PR B on `origin/rama` `a67d4ac` (PR #9, B-104 PR A, merged). `service/migrations.py` line numbers refer to `a67d4ac`.
  - Other file:line citations were made against `747d10d` / `a03f858` and still hold at `a67d4ac`: PR A changed `cli.py:46` in place and added `engine.py:634-638` after the cited lines.
- **History:** `B-104-DDL.v1-v2.s3.diff`, `B-104-DDL.v1-v2.s7.diff`, `B-104-DDL.v2-v3.diff`, `B-104-DDL.v3-v4.diff`, `B-104-DDL.v4-v4.1.diff`.
- Read `FINDINGS.md` first: it has the file:line facts this draft relies on.

**Citation keys:**
- **design:NN** = `origin/akshay-p8-gate:plan/design/p8-gate-review/p8-snapshot-design-v1.1.md` line NN.
- **E-n (errata:NN)** = errata v1.2 (signed off by Rama, 23 Nov 2026; Rama's errata docs PR pending), line NN.
- **Rama 19:5x** = Rama's answers of 23 Nov, recorded in §0.
- **(inferred)** = my reasoning, not a decision anyone has made.
- **Q-n** = an item in §7. **N-n** = an item in the B-105 / B-107 notes (§8, §9).
- **Review files:** FINDINGS.md, probe.py, ddl-check.py, their outputs (probe-out.txt, ddl-check-out.txt) and the v1-v2 and v2-v3 diffs are on `origin/akshay-p8-gate` in `plan/design/p8-impl/b104-akshay/`; this folder holds only this document and the v3-v4 and v4-v4.1 diffs.

---

## 0. Rama's answers (23 Nov, 19:5x)

| # | Answer | Where it lands in this draft |
|---|---|---|
| R-1 | **Rama writes the errata v1.2 docs PR and adds `snapshot-design.md` §8** (the task table). The B-103 ID in `docs/dao-doc-verification.md` is stale (`origin/rama:docs/dao-doc-verification.md:160` still calls the device-allocation schema task "B-103") | §7 Q-1 and the file list in §2.3; the errata status line (errata:6) |
| R-2 | **Rotation goes after B-106.** | §4.1, §8 N-3 |
| R-3 | **The tripwire uses synthetic baselines only**; production baselines stay OPEN | §4.4; design:49 "Baseline content: OPEN" is unchanged |
| R-4 | **ML device on restore:** bind the recorded BDF to the recorded driver and verify by readback. **Never invent hugepage counts.** If the hugepages it needs are missing, **fail closed** to `restore_incomplete` plus quarantine | §4.4 |
| R-5 | **The swap check goes in B-107.** | §9 N-6 |
| R-6 **CONFIRMED** (sign-off) | The recorded devices live in the snapshot profile (`daolab-profile/1`), **never in a `setups.snapshot_bdfs` column**. That name stays a guard marker | §2.1 (M5) |

**Sign-off (Rama, 2026-11-23):**
- **Approved as proposed:** Q-1, Q-3, Q-4, Q-5, Q-7, Q-9, Q-10, Q-11, Q-12, Q-14, Q-15, Q-18, Q-19, Q-20, Q-21.
- **Approved with changes (applied in v4):**
  - **Q-6:**
    - a new `snapshots.hold_approval_id`; cancel and execution match rows by it;
    - execution deletes every snapshot of the revoked key;
    - immediate revoke REJECTED;
    - a booking refusal for a later task.
  - **Q-16:** a held row is neither counted nor pruned.
- **SIGNED OFF (24 Nov):** Q-13 (where holds live), the delete-sweep rule (§3.3, §3.5) and the table order (§3.1). See §7.
- **v4.1 (signed off 24 Nov):** v4.1-1..6, see §11.

---

## 1. Placeholders 12 and 13

**What they do:** nothing. Each one is a registered migration whose function returns without touching the DB:

```python
def _m12_burned(conn: sqlite3.Connection) -> None:
    """E-4: version 12 is burned. The reverted B-104 attempt (4ec115e, reverted in 9c8a089)
    used it, so it stays a no-op forever. `refuse_reverted_schema` keeps those DBs out."""

def _m13_burned(conn: sqlite3.Connection) -> None:
    """E-4: version 13 is burned; see _m12_burned."""

Migration(12, "burned_reverted_v12", _m12_burned),
Migration(13, "burned_reverted_v13", _m13_burned),
```

**Why they must exist:**
- E-4 (errata:13): burn 12 and 13; B-104 starts at 14.
- `_check_contiguous()` (`service/migrations.py:288-295`) runs at import and refuses anything but 1..N. Without 12 and 13, a `Migration(14, ...)` makes the module fail to import.

**Why they're safe on every kind of DB.** `apply()` still bumps `user_version` in the same transaction (`migrations.py:360-367`).

| DB | What happens at 12/13 | Safe because |
|---|---|---|
| fresh | `user_version` 11 → 12 → 13, no DDL | no-op |
| at 11 (every real DB today) | same | no-op |
| new code, crashed after 12 or 13 | resumes at the next version | nothing to redo; a no-op is trivially idempotent (header rule 2, `migrations.py:14-15`) |
| reverted v12 or v13 | **never reached**: the guard (§2) refuses first | — |
| already ≥ 14 | skipped as usual (`migrations.py:358`) | — |

Header rule 1 (never edit or reorder, `migrations.py:12-13`) is kept: 12 and 13 are new entries, and existing ones are untouched. Migration names are not stored in the DB (only `user_version` is), so the names may differ from the reverted `"snapshots"` and `"restore_jobs"` without any effect.

---

## 2. The guard

E-4 (errata:13): "Add a preflight guard against the reverted v12/v13 signatures." It is needed because `SchemaTooNew` does not catch a reverted DB at `user_version` 13.

### 2.1 What it checks: schema markers that only the reverted v12/v13 created
Any **one** marker means refuse. Every marker comes from `4ec115e:service/migrations.py`:

| # | Marker | Test | Source line |
|---|---|---|---|
| M1 | table `user_keys` exists | `has_table(conn, "user_keys")` | `:271` |
| M2 | column `jobs.secret` exists | `has_column(conn, "jobs", "secret")` | `:339` |
| M3 | table `snapshots` exists **without** column `public_id` | `has_table(...) and not has_column(conn, "snapshots", "public_id")` | `:282-299` |
| M4 | column `snapshots.wrapped_dek` exists | `has_column(conn, "snapshots", "wrapped_dek")` | `:294` |
| M5 | column `setups.snapshot_bdfs` exists | `has_column(conn, "setups", "snapshot_bdfs")` | `:307` |
| M6 | column `reservations.save_snapshot` exists with declared default `1` | `PRAGMA table_info(reservations)` row for `save_snapshot` has `dflt_value == '1'` | `:305-306` |

**What each marker catches:**
- A reverted-v12 DB hits M1, M3, M4, M5 and M6.
- A reverted-v13 DB hits all six.
- M3 also refuses *any* foreign `snapshots` table, not only the reverted one.
- M6 is the one that protects the PRESERVE default (FINDINGS §4).

**Deliberately not markers:**
- `jobs.kind` containing `'restore'` and `jobs.snapshot_id`. Migration 14 adds both (§3.8), and every DB with the reverted v13 already hits M2.
- `user_version`. A healthy new DB can be at 12 or 13 after a crash (FINDINGS §1.2).

**Invariant this creates for all later Phase 8 code:**
- never create `user_keys`, `jobs.secret` or `snapshots.wrapped_dek`;
- never add `setups.snapshot_bdfs`;
- never create `snapshots` without `public_id`;
- never declare `save_snapshot DEFAULT 1`.

A test pins it: the guard must pass on our own fully migrated DB (§6.1 G-6).

**On M5:**
- The devices a restore rebinds are recorded in the snapshot profile, `daolab-profile/1` (design:108; R-4: "bind the recorded BDF to the recorded driver"), not in the DB. So `setups.snapshot_bdfs` is never needed, and it stays a marker.
- **Confirmed by Rama at sign-off (R-6):** no `setups.snapshot_bdfs` column, ever, and M5 stays a marker.

### 2.2 Where it runs
- **In `migrations.apply()`**, right after the existing `SchemaTooNew` check (`migrations.py:351-354`) and **before** the loop (`:357`).
- On **every** call, including when the DB is already up to date. It costs a few `PRAGMA table_info` reads. It also means a DB that somehow reached ≥14 with reverted tables is still refused.
- `SchemaTooNew` stays first, so a DB from newer code keeps its existing error and tests (`tests/test_migrations.py:120-125, 162-170`).
- It is read-only (`sqlite_master` and `PRAGMA table_info` only), so it needs no transaction (inferred). Outside the threat model, a reverted build running concurrently could race it. That isn't a real deployment case (the reverted code is on no deployable branch).
- `apply()` is reached from `models.init_db` (`models.py:37-43`) → `cli.cmd_init` (`cli.py:45`), the only non-test caller, so `daolab init` is covered.
- **The engine is covered too, since PR A:** it refuses any version mismatch (`engine.py:629-633`), then runs the guard (`engine.py:634-638`), so a reverted DB is refused even at the latest version.
  - The API has no schema check at all. See Q-3.

### 2.3 Exception and message

```python
class RevertedSchema(Exception):
    """The DB has tables or columns from the reverted Phase 8 attempt (4ec115e)."""

def refuse_reverted_schema(conn) -> None:        # public; _REVERTED_MARKERS is a tuple
    found = [label for label, check in _REVERTED_MARKERS if check(conn)]   # fixed labels only
    if found:
        raise RevertedSchema(
            "database has schema from the reverted Phase 8 snapshot attempt "
            f"({', '.join(found)}); refusing to migrate it. Move this DB aside and start a "
            "fresh one with `daolab init`, or ask an admin. Do not edit it by hand.")
```

**What the message may contain:**
- Only the **fixed marker labels** (e.g. `table user_keys`, `column jobs.secret`, `reservations.save_snapshot default 1`), chosen from the closed list in §2.1.
- No row values, usernames, paths (not even the DB path), SQL text or `user_version`. Nothing that came from user data.

**CLI:** `cmd_init` must exit 1 with this message, not a traceback. That is one change in `cli.py:46`: `except (migrations.SchemaTooNew, migrations.RevertedSchema)` (Q-4).

**Files B-104 touched:**
- PR A: `service/migrations.py` (placeholders, guard), `service/cli.py:46` (one `except` line), `service/engine.py:634-638` (the guard at startup), `tests/test_migrations_guard.py`;
- PR B: `service/migrations.py` (migration 14), `tests/test_migration_14.py` (new), `tests/test_jobs.py`, `tests/test_migrations_guard.py`; and this document.

To be checked against the B-104 row when Rama adds `snapshot-design.md` §8 (R-1). No B-103..B-114 row exists in `plan/10` on any branch today (FINDINGS §1.5).

### 2.4 How it treats each kind of DB

| DB | `user_version` | Markers | Result |
|---|---|---|---|
| fresh (baseline only) | 0 | none | pass → 1..11, 12, 13 (no-op), 14 |
| every real DB today | 11 | none | pass → 12, 13 (no-op), 14 |
| new code, crashed after the no-op 12 or 13 | 12 / 13 | none (the placeholders added nothing) | pass → resumes |
| new code, up to date | 14 | none (new `snapshots` has `public_id`; new `save_snapshot` defaults 0) | pass → no-op |
| reverted v12 only | 12 | M1 M3 M4 M5 M6 | **`RevertedSchema`**, DB unchanged |
| reverted v13 | 13 | M1–M6 | **`RevertedSchema`**, DB unchanged |
| reverted tables but `user_version` hand-set to any value ≤ latest | any | ≥1 | **`RevertedSchema`** (version-independent) |
| newer than this code | > latest | — | `SchemaTooNew` (unchanged, checked first) |

Migration 14 is a single transaction (Q-5), so there is no state between 14 and a later number to consider.

**A refused DB:**
- Recovery is manual: move it aside and `daolab init` a fresh DB.
- Nothing in a reverted DB is worth migrating. The reverted code never ran on the board: `9c8a089`'s message says the default home policy was `keep`, so none of it ran there.
- The one known reverted DB, `var/showcase.db.v13-reverted-b104` (session record, Part 1), is left as it is. B-104 doesn't touch it, and neither did I.
- Rama's answers (§0) don't address this; it is stated here as the procedure. Say if it should differ.

---

## 3. DDL for migration 14

### 3.1 One migration (Q-5, approved)
`Migration(14, "snapshots_v1", _m14_snapshots_v1)` holds everything in §3.3–§3.9.

Why:
1. `apply()` gives each migration one `BEGIN IMMEDIATE` (`migrations.py:360-367`). One migration means a DB is either fully pre-snapshot or fully snapshot-capable, with no mid-states for code or the guard to reason about.
2. The objects reference each other: `snapshots → keys`, `snapshots → admin_approvals`, `key_challenges → keys`, `jobs → snapshots`. They form one unit.
3. The design's 12/13/14 split (design:117) is void under E-4 (errata:13). The two deltas signed off in design:122 (`key_challenges.target_ref` plaintext; `admin_approvals`) land in this migration 14. With Rama's sign-off of this DDL, E-14 (errata:23) is closed.

**Order inside `_m14_snapshots_v1`** (SIGNED OFF, 24 Nov):
1. `keys`, `key_challenges`, `admin_approvals`, `snapshots` (FK targets first; `snapshots.hold_approval_id` references `admin_approvals`);
2. the `add_column` calls of §3.7–§3.9;
3. the `jobs.kind` CHECK rewrite of §3.8.

`ddl-check.py` runs them in this order on a synthetic DB (§6.3).

### 3.2 Conventions used below
- Every table has `id INTEGER PRIMARY KEY AUTOINCREMENT`, `created_at TEXT NOT NULL` and `updated_at TEXT NOT NULL`, so `models.insert`/`models.update` work unchanged (`models.py:69-83`) (inferred).
- Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ` (AGENTS.md; `models.utcnow`, `models.py:46-47`). With that fixed format, text comparison equals time comparison, which the CHECKs and the CAS updates below rely on (inferred).
- **Added in v2, part of the DDL signed off on 2026-11-23:** every timestamp column of the four new tables carries a shape CHECK, `GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'` (an exact 20-character match).
  - Existing tables have none. The new ones get it so that no new TEXT column can hold an S-shaped value (§5), and so that the text-ordered comparisons above stay valid.
  - A malformed time then fails at write, rather than comparing wrongly later.
- All FKs are enforced (`PRAGMA foreign_keys=ON`, `models.py:33`).
- All DDL is `IF NOT EXISTS` or `add_column` (header rule 2). That is only safe *because* the guard has already refused the reverted shapes.
- New index names never reuse `idx_snapshots_owner` or `idx_snapshots_reservation` (FINDINGS §4).
- Nothing secret goes in any column: E-10 (errata:19) and design:72-73. Checked in §5.

### 3.3 `keys`
Sources:
- design:54-57: the server stores the public recipient/fingerprint only; activation by a different admin; snapshot booking needs an *active* key.
- design:58-63: rotation.
- design:64: no token-derived keys.
- design:18, :47: the lost-key reset.
- Q-6, Q-7.

```sql
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
    CHECK ((activated_by IS NULL) = (activated_at IS NULL)),
    CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
           AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
           AND (activated_at IS NULL OR
                activated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'))
                                                             -- timestamp shape, §3.2
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_one_active  ON keys(user_id) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_one_pending ON keys(user_id) WHERE state = 'pending';  -- (inferred)
```

**Lifecycle** (Q-6, Q-7):

| From → to | When | Who |
|---|---|---|
| insert `pending` | the CLI enrolls a new identity | the owner |
| `pending → active` | activation | an admin who is **not the owner** (CHECK); "is an admin" is checked in code (Q-7) |
| `active → retired` | rotation: same transaction as the new key's `pending → active` | rotation flow (design:58-63) |
| `active` (unchanged) + 24h hold | **lost-key reset approved by the second admin** (`admin_approvals.action = 'lost_key_reset'`, §3.6). The reset "only starts a 24h hold" (design:18, :47) | the approval flow |
| `active → revoked` | **the hold ends**: the engine executes the approval through the `execute_after` CAS (§3.6) | the engine |
| `pending → active` (new key) | after the old key is `revoked`; `idx_keys_one_active` blocks it while the old key is still active | an admin who is not the owner |

**Effects of a reset (Q-6, approved with changes):**
1. **On approval** (one transaction, test T-16):
   - the key **stays `active`**;
   - every `saved` snapshot with `key_id` = that key and no hold yet gets `delete_after = now + 24h`, `delete_requested_by = <the approval's requested_by>` and **`hold_approval_id = <the approval's id>`**;
   - the approval's `execute_after` gets the same instant.
   - A row already under an earlier hold of its own keeps that hold, with `hold_approval_id` NULL.
2. **During the hold:**
   - The owner may cancel with a `cancel_delete` challenge, `target_ref = approval:<id>`, answered with the still-active key (design:112). The challenge CAS requires an active key, so this works.
   - Cancelling sets the approval to `cancelled` and clears `delete_after`, `delete_requested_by` and `hold_approval_id` on the rows **`WHERE hold_approval_id = <the approval's id>`**, in one transaction (test T-19).
   - The user may enroll a new key as `pending` in the meantime.
3. **When the hold ends** (one transaction, inside the execution CAS on `execute_after`):
   - the key goes `active → revoked`;
   - **every** snapshot whose `key_id` is the revoked key is deleted, whether held by this approval (`hold_approval_id = <id>`), under an own hold, or unheld (e.g. captured during the hold). The engine deletes the files, then the rows, with audit events;
   - `executed_at` is set.
   - From then on, the key's open challenges fail the CAS (it requires `state = 'active'`, §3.4), and the user's `pending` key may be activated.
- **Rows are matched by `hold_approval_id`, never by timestamp.**
- **The generic delete sweep** (§3.5) handles only rows with `hold_approval_id IS NULL`. Rows held by an approval go only through that approval's execution, so revoke and delete stay in one transaction (SIGNED OFF, 24 Nov).
- **Later task, not B-104:** the API/engine refuses snapshot-enabled bookings for a user while a `lost_key_reset` approval for that user is `pending`, or `approved` but not yet executed. That way no new snapshot is wrapped to a key that is about to be revoked.

**Immediate revoke: REJECTED** (Rama, sign-off). The owner could never cancel, because their `cancel_delete` would fail the active-key CAS.

**Rotation** stays after B-106 (R-2). The old identity stays valid until every `key.age` is re-wrapped (design:61-63; §4.1).

**Other notes:**
- A `pending` key is never revoked; when superseded, it is deleted (inferred). No row references a pending key: challenges reference the key the nonce was sealed to, which is the active one, and snapshots reference the key `key.age` is wrapped to.
- A `retired` key can still be the wrap target of some `key.age` files until the re-wrap finishes (design:61-63); `snapshots.key_id` tracks that (§4.1).

### 3.4 `key_challenges`
Sources:
- design:66-68: hash construction, TTL 10 min, single-use CAS, at most 3 open per user, purposes, `target_ref` in plaintext.
- design:14 (v1.1 item 4), design:122: `target_ref` plaintext signed off.
- design:76: ciphertext returned once, never stored.
- Q-10, Q-12.

```sql
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
);
CREATE INDEX IF NOT EXISTS idx_key_challenges_open
    ON key_challenges(user_id, expires_at) WHERE consumed_at IS NULL;
```

**Single-use consume** (code, the same transaction as the action it authorises, design:60):

```sql
UPDATE key_challenges SET consumed_at = :now, updated_at = :now
 WHERE id = :id AND user_id = :uid AND purpose = :purpose AND target_ref = :target
   AND consumed_at IS NULL AND expires_at > :now
   AND key_id IN (SELECT id FROM keys WHERE state = 'active');   -- a revoked/retired key's open
                                                                 -- challenges die with it (inferred)
-- rowcount must be exactly 1
```

- The caller first recomputes `nonce_sha256` from the answered nonce plus the row's fields, and compares it in constant time (inferred).
- **At most 3 open** (`consumed_at IS NULL AND expires_at > now`) is a code check: count inside `BEGIN IMMEDIATE` before the insert, with no trigger (Q-12).
- Consumed and expired rows are kept. They hold only hashes and public refs. Pruning them is a later, separate decision (Q-12).

### 3.5 `snapshots`
Sources:
- design:91: paths `<snapshot_root>/u<user_id>/<public_id>.{age,key.age,hdr.json}`.
- design:92: commit `capturing→saved` before exit 0.
- design:96, :121: 3 per (user, setup).
- design:112: 24h holds; an unsaved purge has hold 0; the engine physically deletes.
- design:119-120: `public_id`; states.
- D-7-4 (design:134): a retry reuses the failed row.
- Q-6, Q-9, Q-13, Q-15, Q-16.

```sql
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
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_setup ON snapshots(user_id, setup_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_key        ON snapshots(key_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_delete_due ON snapshots(delete_after) WHERE delete_after IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_snapshots_hold_approval ON snapshots(hold_approval_id) WHERE hold_approval_id IS NOT NULL;
```

**Holds (Q-13):**
- **An own delete hold:** a user delete (`delete` challenge, `target_ref` = the snapshot's `public_id`) or an admin delete sets `delete_after = now + 24h` and `delete_requested_by` = that user, with `hold_approval_id` NULL.
  - `cancel_delete` on that `public_id` clears both columns.
  - When `delete_after` has passed, the engine's delete sweep deletes the three files and then the row (Q-15).
  - **The sweep handles only `hold_approval_id IS NULL` rows** (SIGNED OFF, 24 Nov).
- **An approval hold** (`lost_key_reset`): the approval's transaction sets the same two columns plus `hold_approval_id` on every unheld `saved` snapshot of the key under reset (§3.3).
  - The owner's `cancel_delete` on `approval:<id>` clears all three on the rows `WHERE hold_approval_id = <id>`.
  - The approval's execution deletes every snapshot of the key.
  - Rows are matched by `hold_approval_id`, never by timestamp.
- `audit_events` keeps a record of every deletion (`models.audit`, `models.py:535-546`).

**States (Q-15):**
- `capturing → saved | failed`; `failed → capturing` on a force-clean retry (D-7-4).
- The engine marks a `capturing` row whose job is gone or timed out as `failed` (inferred).
- `restore_incomplete` is **not** a state. It is a residue item only (§4.4).

**Retention (Q-16, approved with changes):**
- The prune step after a save (design:96) **counts and prunes only `saved` rows with `delete_after IS NULL`** per (user, setup), oldest first.
- A held row is never retention-pruned: its hold's execution (or the delete sweep) deletes it.
- This is code, not a constraint: a 4th row exists briefly before the prune. `idx_snapshots_user_setup` serves the query.

```sql
-- retention prune (Q-16): keep the newest 3 unheld saved rows per (user, setup)
DELETE FROM snapshots WHERE id IN (
    SELECT id FROM snapshots
     WHERE user_id = :uid AND setup_id = :sid AND state = 'saved' AND delete_after IS NULL
     ORDER BY created_at DESC, id DESC
     LIMIT -1 OFFSET 3);
```

The engine deletes the three files for the selected rows before the row delete, as for every deletion (inferred).

**Deliberately absent:**
- No `path` column: paths are derived from `snapshot_root`, `user_id` and `public_id` (design:91).
- No `wrapped_dek` or any key column: `key.age` lives only in the file (design:55, :73).
- No metadata columns (Q-21).
- Verified earlier: `ON DELETE SET NULL` keeps the snapshot row when sync deletes the reservation, while `NO ACTION` would abort the sync (`probe-out.txt` [4]).

### 3.6 `admin_approvals`
Sources:
- design:122: `admin_approvals` signed off; no columns given.
- E-9 (errata:18): more than 3 admins; the two-admin rules hold as written.
- Two-admin actions:
  - lost-key reset (design:18, :47, :54);
  - tripwire accept/re-baseline (design:17, :49, :101);
  - the legacy-home wipe: E-13 (errata:22), "wipe only with explicit two-admin approval".
- Admin home discard (design:112): 24h hold, owner-cancellable.
- Q-11, Q-13.

```sql
CREATE TABLE IF NOT EXISTS admin_approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT    NOT NULL
                  CHECK (action IN ('lost_key_reset', 'tripwire_accept', 'tripwire_rebaseline',
                                    'home_discard')),                    -- Q-11 (final)
    target_ref    TEXT    NOT NULL                                       -- Q-11: key:<id>, hw_class, setup:<name>
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
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_approvals_one_open
    ON admin_approvals(action, target_ref) WHERE state = 'pending' OR (state = 'approved' AND executed_at IS NULL);
CREATE INDEX IF NOT EXISTS idx_admin_approvals_due
    ON admin_approvals(execute_after) WHERE state = 'approved' AND executed_at IS NULL;
```

**How each action uses it (Q-13, SIGNED OFF 24 Nov; the tripwire and home_discard rows are inferred where not stated):**

| Action | On approval | `execute_after` | `executed_at` |
|---|---|---|---|
| `lost_key_reset` | the key **stays `active`**; its unheld `saved` snapshots get `delete_after`, `delete_requested_by` and `hold_approval_id = <this id>` (§3.3). The owner's `cancel_delete` (`approval:<id>`) before `execute_after` → `cancelled`, and the holds on the rows `WHERE hold_approval_id = <this id>` are cleared in the same transaction (T-19) | now + 24h, the **same instant** as those `delete_after`s (one transaction; a CHECK can't span tables, so a test covers it, §6.2 T-16) | set by the execution CAS at the end of the hold, in the same transaction as key → `revoked` and the deletion of **every** snapshot of that key, held or not (T-16, T-20) |
| `home_discard` | nothing yet; visible to the owner (design:112) | now + 24h | set after the engine discards the home. The owner's `cancel_delete` before then → `cancelled` |
| `tripwire_accept` / `tripwire_rebaseline` | quarantine may lift / the baseline is replaced (design:49, :101) | NULL (no hold stated) | set when applied |

- Both users being admins (`role = 'admin'`) is checked in code (Q-7).
- Execution is a CAS: `UPDATE ... SET executed_at = :now WHERE id = :id AND state = 'approved' AND executed_at IS NULL AND (execute_after IS NULL OR execute_after <= :now)`.
- Audit rows for tripwire decisions carry only class, count and baseline version (design:49).

### 3.7 `reservations.save_snapshot`
Sources: design:20 (item 6, "migration default 0, create default 1"), design:25, :37-41, :119; E-13 (errata:22: "defaults to preserve (`save_snapshot=0`)"); Q-19.

```python
add_column(conn, "reservations", "save_snapshot",
           "INTEGER NOT NULL DEFAULT 0 CHECK (save_snapshot IN (0, 1))")
```

- **Legacy rows → 0 = PRESERVE** (design:25, :38; E-13).
- **New rows:** the API writes `1` *explicitly*, and only when the setup is snapshot-enabled **and** the owner has an active key (design:38). Otherwise it writes 0 (non-enabled setup) or refuses (`snapshot_key_required`; explicit `false` refused, design:39).
- The **column** default stays 0. Any insert path that omits the column (scheduler, tests, future code) then lands on PRESERVE (Q-19).

### 3.8 Jobs: `snapshot_id` and the `restore` kind
Sources: design:87-96 (the save sequence is end-of-slot), design:104-108 (restore), design:117 ("jobs rewrite/restore"), E-5 (errata:14), E-6 (errata:15), E-10 (errata:19); Q-14.

```python
add_column(conn, "jobs", "snapshot_id",
           "INTEGER REFERENCES snapshots(id) ON DELETE SET NULL")
```

```python
_OLD_JOB_KINDS = "'deploy', 'cleanup', 'health_probe', 'reconcile')"
_NEW_JOB_KINDS = "'deploy', 'cleanup', 'health_probe', 'reconcile', 'restore')"
```

**The `restore` kind (Q-14, Option A):**
- It uses the m6 method (`migrations.py:171-195`) on `jobs`:
  - read `sqlite_master.sql` for `jobs`;
  - if `_NEW_JOB_KINDS` is already present, skip (v4.1-3);
  - else require `_OLD_JOB_KINDS` exactly once (v4.1-3), and otherwise raise `RuntimeError("migration 14: cannot find the jobs.kind CHECK to rewrite ...")`; `apply()` rolls back, so `user_version` and the schema stay unchanged;
  - else `writable_schema` → replace once → bump `schema_version` → `writable_schema = OFF`.
- **It is safe to skip on `_NEW_JOB_KINDS`** only because the guard already refused every DB where the reverted v13 put `'restore'` there (M2).
- **Same text as the reverted strings:** these two strings are byte-identical to `4ec115e:service/migrations.py:310-311`. That is unavoidable, because both are derived from m5's CHECK text (`migrations.py:151-152`). The constants and function are written from m5/m6, not copied from the reverted commit.

**Other job changes:**
- **Capture stays inside `cleanup`.** There is no `capture` kind (design:87-96).
- `jobs.snapshot_id` names the snapshot a cleanup-capture or restore job works on. It becomes NULL when the row is physically deleted (Q-15); the audit event keeps the id.
- **No `secret` column, ever** (E-10, design:73). S reaches the DPU only over SSH stdin (E-5) (§4.1).
- In later tasks, not B-104 (inferred): `models.JOB_KINDS` / `MUTATING_JOB_KINDS` (`models.py:590-591`) gain `restore`, and the engine (Akshay-owned) runs it.
- **E-6** (capture and wipe run as root; restore preserves ownership) needs no DDL.

### 3.9 Per-setup snapshot enable flag
Sources: design:38-41 ("snapshot-enabled" setup; "Drain before disabling the flag"); Q-18.

```python
add_column(conn, "setups", "snapshot_enabled",
           "INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_enabled IN (0, 1))")
```

- **In the DB:** the API decides `save_snapshot` from this flag at booking time (design:38-39), and handlers never read YAML (`migrations.py:215-219`).
- **Default 0:** every existing setup stays status quo.
- **Admin-controlled (Q-18):** it is set through an admin action (a later task), like `availability`, and is **not** in `catalog.sync`'s `fields` (`catalog.py:520-526`). Sync then never resets it, and an admin can drain the setup before disabling (design:41).
- With E-13 (errata:22): the one-time decision at enable defaults to preserving the current home. A wipe is a two-admin `home_discard` (§3.6).

---

## 4. Interfaces between later tasks

Task numbers follow the brief; Rama adds `snapshot-design.md` §8 (R-1).

### 4.1 Who generates S, and where `key.age` is recorded (B-105/B-107 ↔ B-109/B-110)
- **S is generated on the controller**, once per capture, by `age-keygen`, held only in a pipe or memory (inferred).
  - The DPU seals "to S" (design:90). That needs only S's **public** recipient, so the private S never goes to the DPU at capture.
  - The controller wraps S to the owner's recipient (design:91) and discards it.
- **The DPU receives** S's public recipient (not secret). It must pass the same strict-shape check as other extravar text (P-0 chokepoint) (inferred).
- **`key.age`** = `<snapshot_root>/u<user_id>/<public_id>.key.age` (design:91). It is written by temp-write + rename (design:62).
- **The DB records only *which key* it is wrapped to:** `snapshots.key_id`.
  - Capture sets it to the owner's active key when the row is created as `capturing`.
  - Rotation updates it per file, in the same step as each rename (design:61-62).
  - **Rotation is complete when no `snapshots` row has `key_id` = the old key.** Until then the client keeps the old identity (design:63).
  - Rotation is built after B-106 (R-2).
- **Restore:**
  1. The CLI downloads `key.age` and unwraps S locally (design:105).
  2. It sends S to the API, which writes the ticket (§4.2).
  3. The controller reads S from the ticket and **streams it over SSH stdin** into `age -d -i -` on the DPU. Ciphertext goes by file or FD. There is no plaintext staging and no S in env, argv, extravars, job dirs or logs (E-5, errata:14; any change to this flow needs a fresh review).
- No DB column ever holds S (§5).

### 4.2 Ticket read/unlink (B-107 ↔ B-110)
From design:75 and :106, E-2 (errata:11) and R-5; the details are (inferred):
- **Location:** `<ticket_dir>/<rand32>-<user_id>-<public_id>` on tmpfs; dir 0700, file 0600, TTL 5 min, single use (N-5).
- `rand32` = 32 lowercase hex. With `public_id` also hex (Q-9), the name splits unambiguously on `-`.
- **Controller prerequisite (E-2):** swap off, encrypted swap or zram, because tmpfs pages can be swapped out. **Startup refuses otherwise.** The check is in B-107 (R-5, N-6).
- **API (writer):**
  - creates the file with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600, writes S and closes it;
  - `stat`s it (regular file, 0600, owner = service user, `nlink` 1);
  - writes **nothing** about the ticket to the DB or to logs.
- **Engine/helper (reader):**
  - finds the ticket from the restore job's `snapshot_id` → `(user_id, public_id)`, as exactly one entry matching `*-<user_id>-<public_id>`;
  - opens it `O_NOFOLLOW`, `fstat`s it (same checks, plus mtime ≤ 5 min);
  - reads it, **unlinks it before using S**, then streams S (§4.1).
  - Zero or several matches means fail the job with a safe error (`ticket:missing` / `ticket:ambiguous`).
- **Sweeper (mandatory, design:75):** unlinks entries older than 5 min and logs only a count (N-7).
- **No DB column for the ticket.** The job's `snapshot_id` is the only link.

### 4.3 Snapshot states and holds (B-109/B-110 write; B-108/B-113 read)

| Transition | Writer | When |
|---|---|---|
| insert `capturing` (with `key_id`, `public_id`) | capture (B-109 per the brief) | before the DPU capture starts (inferred) |
| `capturing → saved` | capture | committed **before** the job exits 0 (design:92); wipe is allowed only after this (design:94) |
| `capturing → failed` | capture, or the engine for a stale row (Q-15) | tar rc≠0, mount under home, empty-home guard (design:99); job gone or timed out |
| `failed → capturing` | capture retry via force-clean | reuses the same row (D-7-4, design:134) |
| `saved`, hold set (`delete_after`, `delete_requested_by`; plus `hold_approval_id` for a reset) | API on `delete` (user/admin), or the `lost_key_reset` approval | 24h hold (design:112; Q-6, Q-13) |
| hold cleared | API on `cancel_delete` | before `delete_after` |
| files deleted, then the row deleted, then the audit event | engine | after `delete_after`; `failed` rows at once (hold 0, design:112) |

**Readers:**
- list, restore and download see only `saved` rows **without** a hold (inferred);
- retention counts and prunes only unheld `saved` rows (`delete_after IS NULL`) per (user, setup), oldest first (Q-16).

Restore never changes the snapshot's state (§3.5).

### 4.4 Residue kinds `home:lab`, `tripwire`, `restore_incomplete` (B-109/B-112 ↔ B-108/B-113)
- **No DDL.** Residue is the `kind:ident` list in `RunResult.residue`, persisted as text in `jobs.error` / `audit_events.detail` (`engine.py:307-311, 359, 377`).
- **New kinds for `runner.py:73` `RESIDUE_KINDS`** (Akshay-owned file; a later task):
  - `home` → item `home:lab` (design:99-100).
  - `tripwire` → item `tripwire:<class>:<count>` (design:49). `parse_residue` splits on the first `:` (`runner.py:87-90`), so the ident is `<class>:<count>`.
    - Classes are the closed list in design:49.
    - Root-owned files in `/home/lab` are user data, not a tripwire class (E-6, errata:15).
    - **Baselines are synthetic only for now; production baselines stay OPEN** (R-3).
  - `restore_incomplete` → item `restore_incomplete:<public_id>` (design:107).
    - It is raised on partial extraction (design:107).
    - **It is also raised on the ML device step** (R-4): the restore binds each recorded BDF to its recorded driver and verifies by readback. It never invents hugepage counts. If the hugepages it needs are missing, it fails closed to `restore_incomplete:<public_id>` plus quarantine.
- **Effect:** quarantine through `engine._quarantine_setup` (`engine.py:314-321`). It lifts only after a clean re-run (design:49, :101). For tripwire, it lifts on a two-admin accept (§3.6).
- **Safe output only:** class names, counts and public ids. No file names, key comments or home paths (design:77).

---

## 5. Rules check: no column can hold S, a recovery identity, a nonce or key material
Rules: design:72-73 (never in DB, backups, logs, audit, argv, extravars, env, job rows, job dirs) and E-10 (errata:19: file-only secrets for new Phase 8 code; nothing secret in the DB).

| New column | Holds | Secret? |
|---|---|---|
| `keys.recipient` | age public key | no, public by design (design:55) |
| `keys.fingerprint` | hash of the public key | no |
| `keys.state` (incl. `revoked`), `activated_by`, `activated_at` | lifecycle | no |
| `key_challenges.nonce_sha256` | SHA-256 over the length-delimited (nonce, purpose, target_ref, key_id, user_id, expires_at) | no: a hash, not the nonce. It can't be inverted **if the nonce is ≥ 32 random bytes** (N-2) |
| `key_challenges.target_ref` | fingerprint / public_id / ref, closed lowercase charset, ≤128 | no (plaintext by decision, design:122) |
| `key_challenges` other columns | ids, purpose, timestamps | no |
| `snapshots.*` | ids, `public_id`, state, `key_id`, `delete_after`, `delete_requested_by`, `hold_approval_id` | no; no path, no DEK, no wrapped key |
| `admin_approvals.*` | action, target_ref, admin ids, state, `decided_at`, `execute_after`, `executed_at` | no |
| `reservations.save_snapshot`, `setups.snapshot_enabled` | 0/1 | no |
| `jobs.snapshot_id` | FK id | no |

**Constraints that keep it that way:**
- No new column is free-form: every TEXT column has a closed CHECK (enum, hex, bech32, a timestamp or a bounded lowercase charset).
- **Verified on a synthetic DB** (`ddl-check-out.txt`): S, which is an `AGE-SECRET-KEY-1…` string in uppercase, is refused by `keys.recipient` and by both `target_ref` columns (lowercase charset plus an explicit `NOT LIKE 'age-secret-key-%'`).
  - In v1, my first charset (`[A-Za-z0-9._:-]`) **accepted** an S-shaped string. That is why T-5 has an S-shaped canary for every TEXT column.
- **Timestamp columns** (`created_at`, `updated_at`, `activated_at`, `expires_at`, `consumed_at`, `delete_after`, `decided_at`, `execute_after`, `executed_at`) carry the §3.2 shape CHECK.
  - The first v2 `ddl-check` run, before that CHECK existed, showed six of them **accepting** an S-shaped string. With it, the S-shaped canary is refused by every new TEXT column (`ddl-check-out.txt`, "S-shaped canary").
- No column is named or typed for key material. A schema test asserts no column name matches `secret|dek|private|identity|nonce$|password|wrapped` in the new tables (T-14).
- **Residual risk:** existing free-text columns (`jobs.error`, `jobs.steps`, `audit_events.detail`) could carry a secret only if code wrote one. The DDL can't prevent that; the secret-canary tests in design:167 cover it.

---

## 6. Test plan for the build steps
- Synthetic DBs only: `:memory:` or `tmp_path`.
- **Never** a real checkout's `var/` (e.g. `var/showcase.db*`) or any real DB.
- Reverted shapes are built in the test from **literal DDL fixtures copied from FINDINGS §3** (the fingerprint), not by importing reverted code.
- Red first: each test is committed failing, then the code makes it pass. `make test` after every change.

### 6.1 The guard
**Red cases** (each expects `RevertedSchema`; afterwards the `sqlite_master` dump, `total_changes` and `user_version` are unchanged):

| ID | Case |
|---|---|
| G-1a…f | each marker M1…M6 **alone** on an otherwise clean v11 DB |
| G-2 | full reverted v12 fixture at `user_version` 12 |
| G-3 | full reverted v13 fixture at `user_version` 13. **This is today's trap:** before the guard exists, it yields `applied == [14]` (`probe-out.txt` [2]). That's the red run |
| G-4 | reverted v13 fixture with `user_version` hand-set to 11, and again to 14 (version-independent) |
| G-5 | message hygiene: seed a canary username and canary row values; assert none appear in `str(e)`; assert the expected marker labels do |
| G-9 | `cli.cmd_init` on a reverted on-disk `tmp_path` DB → `SystemExit(1)`, message on stderr, no traceback |

**Green cases:**

| ID | Case |
|---|---|
| G-6 | fresh DB → `init_db` applies 1..14. **The guard passes on our own v14 schema** (pins the §2.1 invariant) |
| G-7 | v11 DB with seeded rows → passes and upgrades |
| G-8 | new-code DB stopped at 12 and at 13 (apply `MIGRATIONS[:12]` / `[:13]`) → passes and resumes |
| G-10 | an up-to-date DB: guard runs (spy) and passes; `init_db` returns `[]` |
| G-11 | a too-new DB still raises `SchemaTooNew`, not `RevertedSchema` (order kept) |

### 6.2 Migrations 12, 13 and 14

Implemented in `tests/test_migration_14.py` as `test_tN_*` (T-2 is `tests/test_migrations_guard.py::test_t2_placeholders_change_nothing_but_user_version`; T-18 is the full suite). The v4.1 tests and the extras follow the table.

| ID | Case |
|---|---|
| T-1 | fresh DB reaches 14; `applied` includes 12 and 13 (existing `test_fresh_db_reaches_latest`, `tests/test_migrations.py:44-50`, keeps passing) |
| T-2 | placeholders change nothing: the dump before and after 12 and 13 is equal except `user_version` |
| T-3 | v11 DB with a seeded reservation, setup and job: after 14, `save_snapshot = 0`, `snapshot_enabled = 0` and `jobs.snapshot_id IS NULL` on the legacy rows; all other columns unchanged; the legacy job's `kind` is still accepted |
| T-4 | idempotence: run `_m14_...` twice inside a transaction; second run is a no-op (including the CHECK rewrite). `init_db` twice gives `[]` (existing `:53-59`) |
| T-5 | CHECK violations raise `IntegrityError`, with **an S-shaped canary for every TEXT column**: `save_snapshot = 2`; `snapshot_enabled = 2`; bad `snapshots.state` (incl. `restore_incomplete`); bad `keys.state`; bad `purpose`; bad `action`; `nonce_sha256` wrong length, uppercase or non-hex; `recipient` bad prefix, length or charset; `fingerprint` non-hex; `public_id` with `-` or uppercase; `target_ref` with a space, `/`, `{`, uppercase or >128 chars; `consumed_at > expires_at`; any timestamp column not in `YYYY-MM-DDTHH:MM:SSZ` form (e.g. `2026-11-23 00:00:00`, a `+00:00` suffix, or an S-shaped string) |
| T-6 | two-admin and different-admin rules: `keys.activated_by = user_id` refused; `active` with no activator refused; `admin_approvals.decided_by = requested_by` refused; `approved` or `rejected` with no `decided_by` refused; `pending` with a `decided_by` refused; `decided_by` without `decided_at` refused; `executed_at` on a non-approved row refused; **`cancelled` after approval (with `decided_by`) accepted** |
| T-7 | uniqueness: a second `active` key per user refused; a second `pending` key refused; duplicate `public_id`, `recipient`, `fingerprint` or `nonce_sha256` refused; a second `pending` approval for the same (action, target_ref) refused; a second snapshot for the same `reservation_id` refused |
| T-8 | cross-user integrity (composite FK): a challenge or snapshot with another user's `key_id` refused |
| T-9 | single-use CAS: the first consume gives rowcount 1, a replay gives 0; expired gives 0; wrong purpose or target gives 0; **a challenge whose key is `revoked` or `retired` gives 0** |
| T-10 | sync interaction: deleting a released reservation (as `catalog.py:564/578` does) keeps the snapshot with `reservation_id` NULL; deleting a setup that has snapshots is refused (Q-20) |
| T-11 | `jobs.snapshot_id` is SET NULL when a snapshot row is deleted |
| T-12 | `restore` kind: `jobs.kind = 'restore'` accepted, `'bogus'` refused, the existing four still accepted; the CHECK rewrite raises and rolls back (version unchanged) when the expected text is missing (mirrors `tests/test_reservation_states.py:163`) |
| T-13 | `jobs` has no `secret` column; no table `user_keys`; `snapshots` has no `wrapped_dek` or `path`; `setups` has no `snapshot_bdfs` |
| T-14 | column-name rule (§5) over the new tables |
| T-15 | **delete holds** (`delete_after`, `delete_requested_by`): set together on a `saved` row, accepted; `delete_after` without `delete_requested_by` refused, and the reverse; a hold on a `capturing` or `failed` row refused; `delete_after <= created_at` refused; `cancel_delete` (both cleared) accepted; `delete_requested_by` of a non-existent user refused (FK); **`hold_approval_id` without `delete_after` refused; `hold_approval_id` of a non-existent approval refused (FK); clearing `delete_after` while `hold_approval_id` is still set refused** |
| T-16 | **lost-key reset, hold then execute.** Approval `pending → approved` (other admin), in one transaction with: the key **still `active`**; its unheld `saved` snapshots get `delete_after` = the approval's `execute_after`, `delete_requested_by` = `requested_by` and **`hold_approval_id` = the approval's id**; a row with an own earlier hold keeps it with `hold_approval_id` NULL. During the hold: the CAS on the key's open challenge succeeds; a new `pending` key can be enrolled but **not** activated (one-active index). At `execute_after`: the execution CAS sets `executed_at`, the key goes `active → revoked` and **every snapshot of that key** is deleted, in one transaction. Afterwards the CAS on the revoked key's open challenge gives rowcount 0, and the pending key can be activated. An execution CAS before `execute_after` gives 0. A `pending → revoked` transition is refused |
| T-19 | **owner cancels a reset during the hold.** After approval, the owner's `cancel_delete` challenge (`target_ref = approval:<id>`, sealed to the still-active key) is consumed by the CAS (rowcount 1). In the same transaction the approval → `cancelled` and the holds on the rows **`WHERE hold_approval_id = <id>`** are cleared (all three columns NULL). An own earlier hold (`hold_approval_id` NULL) stays. The key stays `active`; a later execution CAS on the cancelled approval gives rowcount 0 |
| T-20 | **execution deletes an unheld snapshot of the revoked key.** A `saved` snapshot captured during the hold (no hold, `hold_approval_id` NULL), and one under an own hold, are both deleted by the execution, along with the approval-held rows. Afterwards no snapshot references the revoked key |
| T-21 | **retention ignores held rows.** For one (user, setup), with 4 unheld `saved` rows and 1 older held row, the §3.5 prune query (taken from this document) deletes only the oldest unheld row. 3 unheld rows remain and the held row is neither counted nor pruned |
| T-17 | **`execute_after`**: `executed_at < execute_after` refused; `executed_at >= execute_after` accepted; `execute_after <= created_at` refused; NULL `execute_after` (tripwire) executes at once |
| T-18 | the full existing suite stays green (`make test`), on both venvs as for B-103 |

| ID | Case | pytest |
|---|---|---|
| V-1 (v4.1-1) | a second `lost_key_reset` for the same target is refused while the first is approved and not executed; allowed again once it is executed or cancelled | `test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted[executed]`, `[cancelled]` |
| V-2 (v4.1-2) | `activated_by` without `activated_at`, and the reverse, refused | `test_v41_2_keys_activation_pair[activated_by without activated_at]`, `[activated_at without activated_by]` |
| V-3 (v4.1-3) | the rewrite raises (and rolls back) when the old kinds text occurs 0 or 2 times; skips when the new text is present | `test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once[old text 0 times]`, `[old text 2 times]`; `test_t12_rewrite_skips_when_new_kinds_already_present` |
| V-4 (v4.1-4) | execution when the key is no longer active (retired or revoked): no revoke, no deletion, every snapshot of the key kept | `test_v41_4_execution_when_key_no_longer_active_deletes_nothing[retired]`, `[revoked]` |
| X-1 | one transaction: a failure injected after the rewrite leaves no new table, index or column, the old jobs.kind CHECK and user_version 13 | `test_m14_is_one_transaction` |
| X-2 | `PRAGMA integrity_check` ok and `foreign_key_check` empty after an upgrade from v11 with rows | `test_m14_integrity_and_foreign_key_check_clean` |

### 6.3 What `ddl-check.py` already verifies on the draft
`ddl-check.py` parses this file's `sql` blocks (the `CREATE TABLE` blocks and the §3.5 retention prune), `add_column` calls and `_OLD/_NEW_JOB_KINDS` strings. It applies them to a synthetic v11 DB in the §3.1 order and exercises a sample of T-5…T-21. Output: `ddl-check-out.txt`.

---

## 7. Decisions (signed off by Rama, 2026-11-23)

**Status:**
- Every item below is **APPROVED** (as proposed) or **APPROVED WITH CHANGES** (the changes are applied in v4 and v4.1).
- Q-13 (item 11), the delete-sweep rule and the table order: **SIGNED OFF (24 Nov)**.

The numbers are kept from v1 for traceability:
- Q-2, Q-17 and Q-22 are folded into the text (§2.3, §2.1, §2.4);
- Q-8 → §8 N-1;
- Q-23 → §9 N-5.

1. **Q-1 — Numbering vs the signed-off text. APPROVED.** design:117 planned "12 snapshots/keys/challenges; 13 jobs rewrite/restore; 14 `admin_approvals`", and design:122 signs off "migration-14 `admin_approvals`".
   E-4 (errata:13) supersedes design:117. Every delta signed off in design:122 lands in the single migration 14, and "migration-14" in design:122 now means "the B-104 migration". Rama's errata v1.2 docs PR (R-1) records this. E-14 (errata:23) is closed by this sign-off.
2. **Q-3 — Guard placement beyond `daolab init`. APPROVED.**
   B-104 places the guard in `migrations.apply()` only (covers `daolab init`). Two follow-ups:
   - (a) the API gains a startup check, the same `have == want` test as `engine.py:629-633`, plus the guard, in the task that owns `api.py` startup;
   - (b) the engine calls the guard beside `engine.py:629`: done in PR A (`engine.py:634-638`), approved by Akshay as the file owner.
3. **Q-4 — Exception shape. APPROVED.** A new `RevertedSchema(Exception)`, caught with `SchemaTooNew` in a tuple at `cli.py:46`; `SchemaTooNew` is not reparented.
4. **Q-5 — One migration or several. APPROVED.** One migration 14 (§3.1).
5. **Q-6 — Key lifecycle and the lost-key reset. APPROVED WITH CHANGES.**
   - States `pending / active / retired / revoked`. A two-admin lost-key reset **only starts a 24h hold** (design:18, :47, :112).
   - **On the second admin's approval** (one transaction):
     - the key **stays `active`**;
     - its unheld `saved` snapshots get `delete_after = now + 24h`, `delete_requested_by` = the approval's `requested_by`, and **`hold_approval_id` = the approval's id**;
     - `admin_approvals.execute_after` gets the same instant.
   - **New column (change):** `snapshots.hold_approval_id INTEGER REFERENCES admin_approvals(id)`, nullable.
     - It is set only for holds placed by an approval (`lost_key_reset`), and NULL for a user's or admin's own delete hold.
     - `CHECK (hold_approval_id IS NULL OR delete_after IS NOT NULL)`, plus a partial index (§3.5).
   - **During the hold:** the owner may cancel with `cancel_delete`, `target_ref = approval:<id>`.
     - That sets the approval to `cancelled` and clears the holds on the rows `WHERE hold_approval_id = <id>`, in one transaction.
     - The user may enroll a new key as `pending`.
   - **Matching (change):** cancel and execution match held rows **by `hold_approval_id`, never by timestamp**.
   - **When the hold ends (change):** the engine executes the approval through the `execute_after` CAS. The key → `revoked`, and **every snapshot whose `key_id` is the revoked key is deleted, held or not**. The pending key may then be activated.
   - The challenge CAS keeps its "key must be active" condition. It is right for keys revoked at the end of a hold.
   - **Later task, not B-104 (change):** the API/engine refuses snapshot-enabled bookings while a `lost_key_reset` approval for that user is `pending`, or `approved` but not yet executed.
   - **Immediate revoke: REJECTED.**
   - **Rotation** stays after B-106, and the old identity stays valid until every `key.age` is re-wrapped.
6. **Q-7 — "A different admin activates". APPROVED.** "Different admin" = **not the key owner**, enforced by `CHECK (activated_by IS NULL OR activated_by <> user_id)`. The activator's `role = 'admin'` is checked in code, with no trigger. The same rule applies to both admins of an approval.
7. **Q-9 — `public_id` format. APPROVED.** 32 lowercase hex from `secrets.token_hex(16)`, with no `-`, so ticket names parse unambiguously.
8. **Q-10 — `target_ref` per purpose. APPROVED; final.**

   | Purpose | `target_ref` |
   |---|---|
   | `rotate` | the new key's fingerprint, 64 hex (design:59) |
   | `delete` | the snapshot's `public_id` |
   | `cancel_delete` | the held snapshot's `public_id` (own delete hold), or `approval:<id>` (a hold placed by an approval: `lost_key_reset` or `home_discard`) |
   | `discard_home` | `setup:<name>`, for the owner's own kept/quarantined home on that setup |

   The charset is `[a-z0-9._:-]{1,128}` plus `NOT LIKE 'age-secret-key-%'`. Setup names and `hw_class` values are lowercase; all current ones are (`tb-01`, `sdp-pf1-vf8`, checked on `origin/rama`).
9. **Q-11 — `admin_approvals` actions and targets. APPROVED; final.**
   - **The four actions:** `lost_key_reset`, `tripwire_accept`, `tripwire_rebaseline`, `home_discard`.
   - **`home_discard`:** two admins, a 24h hold (`execute_after = now + 24h`), visible to the owner, owner-cancellable by `cancel_delete` on `approval:<id>` (design:112). It also covers the E-13 legacy-home wipe ("wipe only with explicit two-admin approval", errata:22).
   - **Targets:** `key:<id>` (reset; **APPROVED 24 Nov as a change to Q-11**, was `user:<id>`), the `hw_class` (tripwire), `setup:<name>` (discard).
   - Pending approvals don't expire. An admin can `cancel` them, and the one-open index (pending, or approved and not yet executed; v4.1-1) stops duplicates.
   - **Rama's code rules for the later tasks (B-105/B-106), 24 Nov:**
     - the target must resolve to an active key at request and approval;
     - at execution, act only if that key is still active; otherwise no-op/cancel with audit and no deletion;
     - no rotation while a reset on that key is pending or approved-unexecuted;
     - reject a second open reset for the same user (a code rule, since the index is per target, i.e. per key).
10. **Q-12 — At most 3 open challenges. APPROVED.** A count inside `BEGIN IMMEDIATE` in code, with no trigger. Consumed and expired rows are kept; pruning them is a later decision.
11. **Q-13 — Where holds live. SIGNED OFF (24 Nov).** The text is updated for the Q-6 changes.
    - No fifth table.
    - **Own delete holds:** `snapshots.delete_after` + `snapshots.delete_requested_by` (FK `users`), with `hold_approval_id` NULL.
    - **Approval holds:** the same two columns plus `snapshots.hold_approval_id` (FK `admin_approvals`). `admin_approvals.execute_after` holds the reset and discard deadlines, and for a reset it carries the same instant as the rows' `delete_after` (§3.6).
    - Cancel and execution match rows by `hold_approval_id`. The generic delete sweep handles only `hold_approval_id IS NULL` rows (SIGNED OFF, 24 Nov).
12. **Q-14 — Restore job. APPROVED.** Option A: a `restore` job kind via the m6-style CHECK rewrite (§3.8). Capture stays inside `cleanup`.
13. **Q-15 — Snapshot states. APPROVED.**
    - States: `capturing / saved / failed`.
    - After physical deletion the **row is deleted**, and `audit_events` keeps the record.
    - `restore_incomplete` is a **residue item only**.
    - Stale `capturing` rows are marked `failed` by the engine (inferred).
14. **Q-16 — Retention. APPROVED WITH CHANGES.**
    - Retention pruning **counts and prunes only `saved` rows with `delete_after IS NULL`**, oldest first, per (user, setup).
    - **A held row is never retention-pruned:** its hold's execution (or the delete sweep, for an own hold) deletes it.
    - The prune query is in §3.5, and T-21 tests it.
15. **Q-18 — Enable flag. APPROVED.** `setups.snapshot_enabled`, **admin-controlled**, and **excluded from `catalog.sync`'s `fields`**. That is needed for drain-before-disable (design:41).
16. **Q-19 — `save_snapshot` default. APPROVED.** The column default is 0; "create default 1" (design:20) is the API writing 1 explicitly on enabled setups when the owner has an active key.
17. **Q-20 — FK actions vs `daolab sync`. APPROVED.**
    - Keep `snapshots.reservation_id ... ON DELETE SET NULL` (sync deletes released reservations, `catalog.py:564/578`).
    - Keep `snapshots.setup_id` as `NO ACTION`, so removing a setup that still has snapshots fails. A later `catalog.py` change makes that a clear `SyncError` ("removal blocked by N snapshots") instead of a raw FK error.
    - Users are never deleted: user deletion (design:29) means `enabled = 0` plus held deletes of their snapshots (inferred). So `users` FKs keep `NO ACTION`.
18. **Q-21 — Snapshot metadata in the DB. APPROVED.** None in v1. Size, `sha256` of `.age`, `hw_class`, use case and the device profile live in `hdr.json` and the snapshot's `daolab-profile/1` (R-4, R-6). If a reader later needs a column, it is an additive migration.

---

## 8. B-105 notes

- **N-1 (was Q-8): fingerprint and recipient type.**
  - Proposed: fingerprint = lowercase SHA-256 hex of the recipient string.
  - X25519 `age1…` recipients only (62 chars), no plugin recipients.
  - B-105 must check the `keys.recipient` CHECK against real `age-keygen -y` output from the pinned age v1.1.1 (`deploy/age/`, merged with B-103 in PR #8; E-1, errata:10).
- **N-2: the challenge nonce is ≥ 32 bytes from `secrets.token_bytes`.** `nonce_sha256` is safe to store only on that condition (§5). The hash input is exactly design:67's length-delimited fields, with `expires_at` as its stored text.
- **N-3: rotation goes after B-106** (R-2; confirmed at sign-off). The old identity stays valid until every `key.age` is re-wrapped. The re-wrap updates `snapshots.key_id` per file, and rotation is complete when no row has the old `key_id` (§4.1).

## 9. B-107 notes

- **N-5 (was Q-23): ticket directory.** Proposed: setting `DAOLAB_TICKET_DIR` (file-only secrets don't apply; it's a path), default `/run/daolab/tickets` on tmpfs. It is created 0700 by the service unit (`RuntimeDirectory=`) or by startup, and startup refuses if the mode or owner is wrong.
- **N-6: the swap check is in B-107** (R-5). Startup refuses unless swap is off, encrypted, or zram (E-2, errata:11). The laptop stays dev / fake-lab only.
- **N-7: the orphan sweeper is mandatory** (design:75). It unlinks tickets older than 5 min and logs a count only.

## 10. Outside B-104, for Rama

- **`reservations.access_password` is an existing plaintext secret column** (migration 10, `service/migrations.py:243-252`; FINDINGS §2).
  - It holds the generated deploy password while a slot is live; it is "wiped when it ends" per the m10 docstring.
  - E-10 (errata:19) applies file-only secrets to *new* Phase 8 code, so B-104 leaves it alone. It is still a secret in the DB and in DB backups (`deploy/backup/backup-db.sh`, design:34).
  - Worth a separate decision: hash it, move it out of the DB, or document the exposure window.

## 11. v4.1 changes (signed off 24 Nov)

**v4.1-1, the one-open approval index.** `idx_admin_approvals_one_open` keeps its name and columns, and its `WHERE` becomes `state = 'pending' OR (state = 'approved' AND executed_at IS NULL)`. In v4, a second `lost_key_reset` (or `home_discard`) for the same target could be opened and approved while the first was approved but not yet executed, which leaves the execution of two approvals undefined. Test: V-1, `test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted`.

**v4.1-2, the activation pair.** `keys` keeps `CHECK ((state = 'pending') = (activated_by IS NULL))` and adds `CHECK ((activated_by IS NULL) = (activated_at IS NULL))`. v4 accepted an `active` key with `activated_by` set and `activated_at` NULL. `admin_approvals` already pairs `decided_by` with `decided_at`. Test: V-2, `test_v41_2_keys_activation_pair`.

**v4.1-3, an exact jobs.kind rewrite.** The rewrite skips only when the exact `_NEW_JOB_KINDS` text is present, and otherwise requires `_OLD_JOB_KINDS` exactly once. If not, it raises the §3.8 `RuntimeError`, and `apply()` rolls back, so `user_version` and the schema are unchanged. v4 skipped on any `'restore'` and replaced the first of possibly several matches. Tests: V-3, `test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once` and `test_t12_rewrite_skips_when_new_kinds_already_present`; also `test_t4_m14_twice_in_one_transaction_is_a_noop`.

**v4.1-4, `lost_key_reset` targets `key:<id>`.** This was `user:<id>`, and the change is approved as a change to Q-11. The approval names the key under reset, so a later rotation can't change which key a reset revokes. Rama's code rules for B-105/B-106 are in §7 Q-11, and include "at execution act only if that key is still active; otherwise no-op/cancel with audit and no deletion". DDL: a comment in §3.6 only, because the `target_ref` CHECK already admits `key:12`. Tests: T-16, T-19, T-20 and V-1 use `key:<id>`; V-4, `test_v41_4_execution_when_key_no_longer_active_deletes_nothing`, covers the execution rule as the tests implement it.

**v4.1-5, `home_discard` targets `setup:<name>`.** The `admin_approvals` target now matches the `discard_home` challenge's `setup:<name>` (Q-10), instead of the bare setup name. DDL: a comment in §3.6 only. Tests: T-6 and T-17 use `setup:tb-01`.

**v4.1-6, "(inferred)" tags.** Only the DDL tag on `idx_admin_approvals_one_open` is removed (v4.1-1 is signed off). The tag at v4:368, `CHECK (delete_after IS NULL OR state = 'saved')`, "purge with hold 0 [design:112] (inferred)", stays: it is about which rows can be held (design:112), not Q-13. In the prose, the tags on Q-13, the delete-sweep rule and the table order now read SIGNED OFF (24 Nov). Every other tag stays. Test: none, since these are comments; the doc-vs-code check shows this document's SQL is exactly what `_m14_snapshots_v1` sends.

## 12. Operator note

After upgrading to a build that contains this migration, run `daolab init` once per DB before starting the engine. The latest schema version is now 14, and the engine refuses any mismatch (`engine.py:629-633`). `daolab init` is idempotent, applies only the pending migrations (13 → 14 on a current DB), and refuses a DB from the reverted Phase 8 attempt (§2).
