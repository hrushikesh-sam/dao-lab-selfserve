"""Build plan/design/p8-impl/B-104-DDL.md (v4.1, final) from DDL v4 with a fixed list of edits.

Every edit is an exact (old -> new) replacement that must match exactly once, so nothing
else in v4 can change. Usage: python3 -I make_final_doc.py <v4.md> <out.md>"""
import sys

src, out = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
applied = []


def rep(tag, old, new):
    global s
    n = s.count(old)
    assert n == 1, (tag, n, old[:90])
    s = s.replace(old, new)
    applied.append(tag)


# ---- (a) title, status, base; (h) line-number note; history; (i) citation legend ---------------
rep("a-title", "# B-104 — DDL (v4)\n", "# B-104 — DDL (v4.1)\n")
rep("a-status",
    "Date: 2026-11-23. Status: **v4, signed off by Rama 2026-11-23, with conditions applied**. Nothing is built.",
    "Date: 2026-11-23 (v4); 2026-11-24 (v4.1). Status: **v4.1, signed off by Rama 23-24 Nov. "
    "Built in PR B on origin/rama a67d4ac.**")
rep("a-base",
    "- **Base:** `origin/rama` `a03f858` (PR #8, B-103, merged).\n"
    "  - `git diff --stat 747d10d a03f858 -- service tests/test_migrations.py tests/test_reservation_states.py "
    "plan/10-backend-tasksheet.md` is empty, so every file:line citation made against `747d10d` still holds.\n",
    "- **Base:** built in PR B on `origin/rama` `a67d4ac` (PR #9, B-104 PR A, merged). "
    "`service/migrations.py` line numbers refer to `a67d4ac`.\n"
    "  - Other file:line citations were made against `747d10d` / `a03f858` and still hold at `a67d4ac`: "
    "PR A changed `cli.py:46` in place and added `engine.py:634-638` after the cited lines.\n")
rep("history", "`B-104-DDL.v2-v3.diff`, `B-104-DDL.v3-v4.diff`.",
    "`B-104-DDL.v2-v3.diff`, `B-104-DDL.v3-v4.diff`, `B-104-DDL.v4-v4.1.diff`.")
rep("i-legend",
    "- **E-n (errata:NN)** = `~/notes/p8-design-errata-v1.2.md` (signed off by Rama, 23 Nov 2026), line NN.",
    "- **E-n (errata:NN)** = errata v1.2 (signed off by Rama, 23 Nov 2026; Rama's errata docs PR pending), line NN.")

rep("i-review-files",
    "- **Q-n** = an item in §7. **N-n** = an item in the B-105 / B-107 notes (§8, §9).\n",
    "- **Q-n** = an item in §7. **N-n** = an item in the B-105 / B-107 notes (§8, §9).\n"
    "- **Review files:** FINDINGS.md, probe.py, ddl-check.py, their outputs (probe-out.txt, "
    "ddl-check-out.txt) and the v1-v2 and v2-v3 diffs are on `origin/akshay-p8-gate` in "
    "`plan/design/p8-impl/b104-akshay/`; this folder holds only this document and the v3-v4 and "
    "v4-v4.1 diffs.\n")
rep("6.3-path", "`~/notes/b104/ddl-check.py` parses this file's", "`ddl-check.py` parses this file's")
rep("6-local-path", "- **Never** `~/dao-lab-selfserve/var`, `var/showcase.db*` or any real DB.",
    "- **Never** a real checkout's `var/` (e.g. `var/showcase.db*`) or any real DB.")

# ---- (b) §0: Q-13 / sweep / order signed off --------------------------------------------------------
rep("b-39",
    "- **Not in the sign-off list:** Q-13 (where holds live). Its text is updated for `hold_approval_id` "
    "and its status is unchanged (proposed). See §7.",
    "- **SIGNED OFF (24 Nov):** Q-13 (where holds live), the delete-sweep rule (§3.3, §3.5) and the table "
    "order (§3.1). See §7.\n"
    "- **v4.1 (signed off 24 Nov):** v4.1-1..6, see §11.")

# ---- (h) accuracy: names, line numbers, engine coverage, files ---------------------------------------
rep("h-50", "used it, so it stays a no-op forever. `_refuse_reverted_schema` keeps those DBs out.",
    "used it, so it stays a no-op forever. `refuse_reverted_schema` keeps those DBs out.")
rep("h-61", "(`service/migrations.py:273-280`)", "(`service/migrations.py:288-295`)")
rep("h-63", "`apply()` still bumps `user_version` in the same transaction (`migrations.py:308-315`).",
    "`apply()` still bumps `user_version` in the same transaction (`migrations.py:360-367`).")
rep("h-71", "skipped as usual (`migrations.py:306`)", "skipped as usual (`migrations.py:358`)")
rep("h-116", "right after the existing `SchemaTooNew` check (`migrations.py:300-303`) and **before** the loop (`:305`).",
    "right after the existing `SchemaTooNew` check (`migrations.py:351-354`) and **before** the loop (`:357`).")
rep("h-121",
    "- **Not covered by this placement: the engine and the API.**\n"
    "  - The engine already refuses any version mismatch (`engine.py:629-633`), so a reverted-v13 DB (13) "
    "under new code (14) cannot start the engine.\n",
    "- **The engine is covered too, since PR A:** it refuses any version mismatch (`engine.py:629-633`), "
    "then runs the guard (`engine.py:634-638`), so a reverted DB is refused even at the latest version.\n")
rep("h-131",
    "def _refuse_reverted_schema(conn) -> None:\n"
    "    found = [label for label, hit in _REVERTED_MARKERS(conn) if hit]   # fixed labels only\n",
    "def refuse_reverted_schema(conn) -> None:        # public; _REVERTED_MARKERS is a tuple\n"
    "    found = [label for label, check in _REVERTED_MARKERS if check(conn)]   # fixed labels only\n")
rep("h-146",
    "**Files B-104 touches** (proposed):\n"
    "- `service/migrations.py`: placeholders, guard, migration 14;\n"
    "- `service/cli.py:46`: one `except` line;\n"
    "- `tests/test_migrations.py`, or a new `tests/test_migrations_p8.py`.\n",
    "**Files B-104 touched:**\n"
    "- PR A: `service/migrations.py` (placeholders, guard), `service/cli.py:46` (one `except` line), "
    "`service/engine.py:634-638` (the guard at startup), `tests/test_migrations_guard.py`;\n"
    "- PR B: `service/migrations.py` (migration 14), `tests/test_migration_14.py` (new), "
    "`tests/test_jobs.py`, `tests/test_migrations_guard.py`; and this document.\n")
rep("h-182", "1. `apply()` gives each migration one `BEGIN IMMEDIATE` (`migrations.py:308-315`).",
    "1. `apply()` gives each migration one `BEGIN IMMEDIATE` (`migrations.py:360-367`).")
rep("b-186", "**Order inside `_m14_snapshots_v1`** (inferred):", "**Order inside `_m14_snapshots_v1`** (SIGNED OFF, 24 Nov):")

# ---- (c) v4.1-2 in §3.3 ---------------------------------------------------------------------------
rep("c-v41-2",
    "    CHECK ((state = 'pending') = (activated_by IS NULL)),    -- retired/revoked keys were once active\n",
    "    CHECK ((state = 'pending') = (activated_by IS NULL)),    -- retired/revoked keys were once active\n"
    "    CHECK ((activated_by IS NULL) = (activated_at IS NULL)),\n")
rep("b-269",
    "Rows held by an approval go only through that approval's execution, so revoke and delete stay in one "
    "transaction (inferred).",
    "Rows held by an approval go only through that approval's execution, so revoke and delete stay in one "
    "transaction (SIGNED OFF, 24 Nov).")
rep("b-385", "  - **The sweep handles only `hold_approval_id IS NULL` rows** (inferred).",
    "  - **The sweep handles only `hold_approval_id IS NULL` rows** (SIGNED OFF, 24 Nov).")

# ---- (c) v4.1-4/5 comment and v4.1-1 index in §3.6 ------------------------------------------------
rep("c-v41-4-5", "-- Q-11: user:<id>, hw_class, setup name\n", "-- Q-11: key:<id>, hw_class, setup:<name>\n")
rep("c-v41-1",
    "    ON admin_approvals(action, target_ref) WHERE state = 'pending';      -- (inferred)\n",
    "    ON admin_approvals(action, target_ref) WHERE state = 'pending' OR (state = 'approved' AND executed_at IS NULL);\n")
rep("b-472", "**How each action uses it (Q-13; inferred where not stated):**",
    "**How each action uses it (Q-13, SIGNED OFF 24 Nov; the tripwire and home_discard rows are inferred where "
    "not stated):**")

# ---- §3.8: v4.1-3 behaviour, line numbers ----------------------------------------------------------
rep("h-510", "- It uses the m6 method (`migrations.py:167-195`) on `jobs`:",
    "- It uses the m6 method (`migrations.py:171-195`) on `jobs`:")
rep("v41-3-text",
    "  - if `'restore'` is already present, skip;\n"
    "  - else require `_OLD_JOB_KINDS` and raise `RuntimeError(\"migration 14: cannot find the jobs.kind CHECK "
    "to rewrite ...\")` if it is missing;\n",
    "  - if `_NEW_JOB_KINDS` is already present, skip (v4.1-3);\n"
    "  - else require `_OLD_JOB_KINDS` exactly once (v4.1-3), and otherwise raise `RuntimeError(\"migration 14: "
    "cannot find the jobs.kind CHECK to rewrite ...\")`; `apply()` rolls back, so `user_version` and the schema "
    "stay unchanged;\n")
rep("v41-3-safe", "- **It is safe to skip on `'restore'`** only because",
    "- **It is safe to skip on `_NEW_JOB_KINDS`** only because")
rep("h-516", "both are derived from m5's CHECK text (`migrations.py:147-148`)",
    "both are derived from m5's CHECK text (`migrations.py:151-152`)")
rep("h-533", "handlers never read YAML (`migrations.py:211-215`)", "handlers never read YAML (`migrations.py:215-219`)")

# ---- (j) §6.2: file, v4.1 tests and extras ----------------------------------------------------------
rep("j-file",
    "### 6.2 Migrations 12, 13 and 14\n\n",
    "### 6.2 Migrations 12, 13 and 14\n\n"
    "Implemented in `tests/test_migration_14.py` as `test_tN_*` (T-2 is "
    "`tests/test_migrations_guard.py::test_t2_placeholders_change_nothing_but_user_version`; T-18 is the full "
    "suite). The v4.1 tests and the extras follow the table.\n\n")
rep("j-rows",
    "| T-18 | the full existing suite stays green (`make test`), on both venvs as for B-103 |\n",
    "| T-18 | the full existing suite stays green (`make test`), on both venvs as for B-103 |\n"
    "\n"
    "| ID | Case | pytest |\n"
    "|---|---|---|\n"
    "| V-1 (v4.1-1) | a second `lost_key_reset` for the same target is refused while the first is approved and "
    "not executed; allowed again once it is executed or cancelled | "
    "`test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted[executed]`, `[cancelled]` |\n"
    "| V-2 (v4.1-2) | `activated_by` without `activated_at`, and the reverse, refused | "
    "`test_v41_2_keys_activation_pair[activated_by without activated_at]`, `[activated_at without activated_by]` |\n"
    "| V-3 (v4.1-3) | the rewrite raises (and rolls back) when the old kinds text occurs 0 or 2 times; skips when "
    "the new text is present | `test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once[old text 0 times]`, "
    "`[old text 2 times]`; `test_t12_rewrite_skips_when_new_kinds_already_present` |\n"
    "| V-4 (v4.1-4) | execution when the key is no longer active (retired or revoked): no revoke, no deletion, "
    "every snapshot of the key kept | `test_v41_4_execution_when_key_no_longer_active_deletes_nothing[retired]`, "
    "`[revoked]` |\n"
    "| X-1 | one transaction: a failure injected after the rewrite leaves no new table, index or column, the "
    "old jobs.kind CHECK and user_version 13 | `test_m14_is_one_transaction` |\n"
    "| X-2 | `PRAGMA integrity_check` ok and `foreign_key_check` empty after an upgrade from v11 with rows | "
    "`test_m14_integrity_and_foreign_key_check_clean` |\n")

# ---- §7 -------------------------------------------------------------------------------------------
rep("b-700",
    "- Every item below is **APPROVED** (as proposed) or **APPROVED WITH CHANGES** (the changes are applied in v4).\n"
    "- The one exception is Q-13, which was not in the sign-off list (see item 11).\n",
    "- Every item below is **APPROVED** (as proposed) or **APPROVED WITH CHANGES** (the changes are applied in "
    "v4 and v4.1).\n"
    "- Q-13 (item 11), the delete-sweep rule and the table order: **SIGNED OFF (24 Nov)**.\n")
rep("h-q3",
    "   - (b) the engine calls the guard beside `engine.py:629`, as a one-line change for Akshay (the file owner) "
    "to approve separately.\n"
    "   Until then, the engine is already protected by its version check (a reverted DB is at 12/13, the code at 14).\n",
    "   - (b) the engine calls the guard beside `engine.py:629`: done in PR A (`engine.py:634-638`), approved by "
    "Akshay as the file owner.\n")
rep("d-q11-targets",
    "   - **Targets:** `user:<id>` (reset), the `hw_class` (tripwire), the setup name (discard).\n"
    "   - Pending approvals don't expire. An admin can `cancel` them, and the one-open index stops duplicates.\n",
    "   - **Targets:** `key:<id>` (reset; **APPROVED 24 Nov as a change to Q-11**, was `user:<id>`), the "
    "`hw_class` (tripwire), `setup:<name>` (discard).\n"
    "   - Pending approvals don't expire. An admin can `cancel` them, and the one-open index (pending, or "
    "approved and not yet executed; v4.1-1) stops duplicates.\n"
    "   - **Rama's code rules for the later tasks (B-105/B-106), 24 Nov:**\n"
    "     - the target must resolve to an active key at request and approval;\n"
    "     - at execution, act only if that key is still active; otherwise no-op/cancel with audit and no deletion;\n"
    "     - no rotation while a reset on that key is pending or approved-unexecuted;\n"
    "     - reject a second open reset for the same user (a code rule, since the index is per target, i.e. per key).\n")
rep("b-754",
    "11. **Q-13 — Where holds live. Not in Rama's sign-off list; status unchanged (proposed).** "
    "The text is updated for the Q-6 changes.",
    "11. **Q-13 — Where holds live. SIGNED OFF (24 Nov).** The text is updated for the Q-6 changes.")
rep("b-758",
    "The generic delete sweep handles only `hold_approval_id IS NULL` rows (inferred).\n"
    "    - Q-6's approval already depends on these columns, so this item is very likely settled in substance. "
    "Ask Rama to confirm it explicitly.\n",
    "The generic delete sweep handles only `hold_approval_id IS NULL` rows (SIGNED OFF, 24 Nov).\n")
rep("h-797", "(migration 10, `service/migrations.py:239-248`; FINDINGS §2)",
    "(migration 10, `service/migrations.py:243-252`; FINDINGS §2)")

# ---- (g) §11 v4.1 changes; (k) §12 operator note -------------------------------------------------
assert s.endswith("\n")
s += """
## 11. v4.1 changes (signed off 24 Nov)

**v4.1-1, the one-open approval index.** `idx_admin_approvals_one_open` keeps its name and columns, and its `WHERE` becomes `state = 'pending' OR (state = 'approved' AND executed_at IS NULL)`. In v4, a second `lost_key_reset` (or `home_discard`) for the same target could be opened and approved while the first was approved but not yet executed, which leaves the execution of two approvals undefined. Test: V-1, `test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted`.

**v4.1-2, the activation pair.** `keys` keeps `CHECK ((state = 'pending') = (activated_by IS NULL))` and adds `CHECK ((activated_by IS NULL) = (activated_at IS NULL))`. v4 accepted an `active` key with `activated_by` set and `activated_at` NULL. `admin_approvals` already pairs `decided_by` with `decided_at`. Test: V-2, `test_v41_2_keys_activation_pair`.

**v4.1-3, an exact jobs.kind rewrite.** The rewrite skips only when the exact `_NEW_JOB_KINDS` text is present, and otherwise requires `_OLD_JOB_KINDS` exactly once. If not, it raises the §3.8 `RuntimeError`, and `apply()` rolls back, so `user_version` and the schema are unchanged. v4 skipped on any `'restore'` and replaced the first of possibly several matches. Tests: V-3, `test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once` and `test_t12_rewrite_skips_when_new_kinds_already_present`; also `test_t4_m14_twice_in_one_transaction_is_a_noop`.

**v4.1-4, `lost_key_reset` targets `key:<id>`.** This was `user:<id>`, and the change is approved as a change to Q-11. The approval names the key under reset, so a later rotation can't change which key a reset revokes. Rama's code rules for B-105/B-106 are in §7 Q-11, and include "at execution act only if that key is still active; otherwise no-op/cancel with audit and no deletion". DDL: a comment in §3.6 only, because the `target_ref` CHECK already admits `key:12`. Tests: T-16, T-19, T-20 and V-1 use `key:<id>`; V-4, `test_v41_4_execution_when_key_no_longer_active_deletes_nothing`, covers the execution rule as the tests implement it.

**v4.1-5, `home_discard` targets `setup:<name>`.** The `admin_approvals` target now matches the `discard_home` challenge's `setup:<name>` (Q-10), instead of the bare setup name. DDL: a comment in §3.6 only. Tests: T-6 and T-17 use `setup:tb-01`.

**v4.1-6, "(inferred)" tags.** Only the DDL tag on `idx_admin_approvals_one_open` is removed (v4.1-1 is signed off). The tag at v4:368, `CHECK (delete_after IS NULL OR state = 'saved')`, "purge with hold 0 [design:112] (inferred)", stays: it is about which rows can be held (design:112), not Q-13. In the prose, the tags on Q-13, the delete-sweep rule and the table order now read SIGNED OFF (24 Nov). Every other tag stays. Test: none, since these are comments; the doc-vs-code check shows this document's SQL is exactly what `_m14_snapshots_v1` sends.

## 12. Operator note

After upgrading to a build that contains this migration, run `daolab init` once per DB before starting the engine. The latest schema version is now 14, and the engine refuses any mismatch (`engine.py:629-633`). `daolab init` is idempotent, applies only the pending migrations (13 → 14 on a current DB), and refuses a DB from the reverted Phase 8 attempt (§2).
"""
open(out, "w", encoding="utf-8").write(s)
print(f"applied {len(applied)} edits:", ", ".join(applied))
