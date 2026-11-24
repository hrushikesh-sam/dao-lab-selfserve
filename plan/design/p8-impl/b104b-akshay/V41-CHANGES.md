# B-104 PR B: v4.1 changes vs DDL v4, SIGNED OFF by Rama 24 Nov

Base: `origin/rama` `a67d4ac`. Spec: DDL v4 (`origin/akshay-p8-gate:plan/design/p8-impl/b104-akshay/B-104-DDL.md`,
sha256 `f4169f1d…`). Final doc: `plan/design/p8-impl/B-104-DDL.md` (v4.1), with the v4 → v4.1 diff in
`plan/design/p8-impl/B-104-DDL.v4-v4.1.diff`. Line numbers below are in `~/b104b` as built
(`service/migrations.py`, `tests/test_migration_14.py`).

**Checks:**
- `ddl-verbatim-check.venv*.txt`: `_m14` = DDL v4 §3 plus exactly the v4.1 edits declared in
  `ddl_verbatim_check.py`.
- `doc-vs-code.venv*.txt`: `_m14` = the final doc's SQL, **IDENTICAL with no exceptions**.

Every v4.1 DDL edit is its own Python string piece, preceded by a `# v4.1-N (signed off by Rama 24 Nov)`
comment.

## v4.1-1: the one-open approval index covers approved-but-unexecuted rows
- **v4 (v4:466-467):**
  `CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_approvals_one_open ON admin_approvals(action, target_ref) WHERE state = 'pending';      -- (inferred)`
- **Now (`migrations.py:411-417`):** same name and columns, with
  `WHERE state = 'pending' OR (state = 'approved' AND executed_at IS NULL)`. The `(inferred)` tag is
  dropped (v4.1-6); `:415-416` is the comment.
- **Covered by:** `test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted[executed|cancelled]`
  (`test_migration_14.py:886-900`); `test_t7_uniqueness` (a second pending).
- **Back out:** `:417` → `"""WHERE state = 'pending'"""` + `"""      -- (inferred)"""`; delete
  `:415-416` and the test. A DB already at 14 keeps its stored index (`IF NOT EXISTS` matches by
  name), so that needs a later migration.

## v4.1-2: keys sets activated_by and activated_at together
- **v4 (v4:229-236):** keeps `CHECK ((state = 'pending') = (activated_by IS NULL))` (unchanged, per Rama).
- **Now (`migrations.py:314-318`):** adds `CHECK ((activated_by IS NULL) = (activated_at IS NULL)),` after
  it. `:315` is the comment.
- **Covered by:** `test_v41_2_keys_activation_pair[activated_by without activated_at|activated_at without activated_by]`
  (`:924-935`). Each asserts this CHECK's own text refuses; the second case passes every v4 CHECK.
- **Back out:** delete `:315-318` and the test.

## v4.1-3: an exact jobs.kind rewrite
- **v4 §3.8 (v4:510-514):** skip if `'restore'` is present; else require `_OLD_JOB_KINDS`.
- **Now (`migrations.py:478-496`):**
  - skip only if `_NEW_JOB_KINDS` is present (`:484`);
  - else require `sql.count(_OLD_JOB_KINDS) == 1` (`:486`);
  - else raise the v4 §3.8 `RuntimeError`, and `apply()` rolls back, so `user_version` and the schema are
    unchanged.
  - `:481-483` is the comment.
- **Covered by:**
  - `test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once[old text 0 times|old text 2 times]`
    (`:602`): raises, dump unchanged, v13;
  - `test_t12_rewrite_skips_when_new_kinds_already_present` (`:616`);
  - `test_t4_m14_twice_in_one_transaction_is_a_noop`.
- **Back out:** the two `if` lines → `"'restore'" in sql` / `_OLD_JOB_KINDS not in sql`; drop the `[old text 2 times]` case.

## v4.1-4: lost_key_reset targets `key:<id>`; execution acts only on a still-active key
- **v4 (v4:436, :751):** `user:<id>`.
- **Now:**
  - the SQL comment `-- Q-11: key:<id>, …` (`migrations.py:376-378`; `:377` is the comment). The
    `target_ref` CHECK is unchanged;
  - Rama's code rules (B-105/B-106) are recorded in the doc's §7 Q-11.
  - The tests' `_execute_reset` (`:738-752`) follows his execution rule: revoke only an active key,
    and delete the key's snapshots only if the revoke changed a row.
- **Covered by:**
  - T-16, T-19, T-20 and V-1 use `key:<id>` (`_approve_reset`, `:724`);
  - `test_v41_4_execution_when_key_no_longer_active_deletes_nothing[retired|revoked]` (`:903-921`).
- **Back out:** `:378` → `"""user:<id>"""`; in the tests, `f"key:{…}"` → `f"user:{w.owner}"`; and in
  `_execute_reset`, `if revoked:` → `if executed:` before the DELETE (that also drops V-4).

## v4.1-5: home_discard targets `setup:<name>`
- **v4 (v4:436, :751):** the bare setup name.
- **Now:** the SQL comment `…, setup:<name>` (`migrations.py:379-381`; `:380` is the comment). The CHECK
  is unchanged.
- **Covered by:** T-6 and T-17 use `setup:tb-01`.
- **Back out:** `:381` → `setup name`; in the tests, `"setup:tb-01"` → `"tb-01"`.

## v4.1-6: "(inferred)" tags
- **DDL:** only the `idx_admin_approvals_one_open` tag is removed (with v4.1-1).
- **Kept:** v4:368, `CHECK (delete_after IS NULL OR state = 'saved')` "… [design:112] (inferred)", stays.
  It is about which rows can be held (design:112), not Q-13. Every other DDL tag stays.
- **Final doc prose:** the tags on the table order (v4:186), the delete sweep (v4:269, :385, :758) and
  Q-13 (v4:472) now read SIGNED OFF (24 Nov). 5 tags removed in all; 27 remain (28 occurrences of "(inferred" with the new §11
  mention.
- **Covered by:** `doc-vs-code` (comments only).
