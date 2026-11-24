# B-104 PR B: evidence (migration 14 + the B-104 DDL v4.1 docs)

PR B is two commits on `origin/rama` `a67d4ac` (PR #9, B-104 PR A, merged). It has not been
committed or pushed yet; the patches and messages are here.

| Commit | What | Tree | Patch (sha256) |
|---|---|---|---|
| 1 | Migration 14 `snapshots_v1`, plus tests | tree1 `5168e893a4181bea994a409326e75797e92d820c` | `patches/B104B-m14.patch` `faed58e16f3a2c0f9fb11a01bbd7fcd10f2530e0c301565ae9c0470af0df298a` |
| 2 | DDL v4.1 doc, its two diffs, `.gitattributes`, `plan/08`, `README.md`, `plan/README.md` | tree2 `5505478e9d4c835e8c763591b72eba6be3135edf` | `patches/B104B-docs.patch` `ca46df7ee4e40a8163d266005f0a01e533116f5e84bfa750a6343e943b40f911` |

**Sign-off.** Rama signed off DDL v4 on 23 Nov. On 24 Nov he signed off v4.1 items 1–6 and the
exact 11-file list (his AGENTS.md authorization).

**`.gitattributes`.** Commit 2's `plan/design/p8-impl/.gitattributes` is `*.diff -text -whitespace`.
That is Akshay's decision, within Rama's file list; Rama saw `*.diff -text`. The diffs are verbatim,
so their whitespace is data.

**Commit messages:** `msg/01-B104B-m14.txt`, `msg/02-B104B-docs.txt`.

## Test runs

All runs used Python 3.12.13 (uv CPython) with SQLite 3.53.1, ansible-runner 2.4.3 and pytest 9.2.0.
The two venvs differ only in ansible-core: 2.21.6 (`*-221`) and 2.20.11 (`*-220`).

| Stage | Files under test | 2.21.6 | 2.20.11 | Tree |
|---|---|---|---|---|
| red-1 (`red1-*`) | final `tests/test_migration_14.py`; the other 3 commit-1 files = `origin/rama` | 13 failed, 91 errors, 187 passed | same | working tree |
| red-2 (`red2-*`) | + final `tests/test_jobs.py`, `tests/test_migrations_guard.py`; `service/migrations.py` = `origin/rama` | 18 failed, 91 errors, 183 passed | same | working tree |
| green (`green-*`) | all final (targeted: the 5 migration/jobs/states test files) | 292 passed | same | working tree |
| full (`full-*`) | `tests/` | **1437 passed, 4 skipped, 0 failed** | **same** | both commits, before the `.gitattributes` change |
| make test (`make-test.txt`) | `tests/` via the Makefile, `.venv` first on PATH | **1437 passed, 4 skipped, 0 failed** | not run | `8519808d…` |
| docs-tests (`docs-tests-final-*`) | the 21 test files that mention `plan/` or `README` | **689 passed** | **same** | **final tree2 `5505478e…`** |

**Which tree each run saw.**
- The red stages ran on base files restored from git: `git show origin/rama:<path>`.
- The final files were then put back, proven by `final-files-restored.txt` against `final-files.sha256`.
- Commit 1's files are identical (tree1) in every run after red-2.
- The full suites and `make test` ran on trees that differ from the final tree only in files under
  `plan/`: `.gitattributes`, `B-104-DDL.md`, its v4-v4.1 diff, and line wrapping in `plan/08`.
- No test opens any of those files. `tests-reading-docs.txt` classifies all 36 mentions on 35 lines
  by file:line: **0 open a file**; all are comments or strings.
- The docs-tests ran on the final tree.

**Reds, and "guard, not red".**
- **Red-1:** all 104 new tests fail ("no such table: keys", `KeyError: 14`, …); every existing test
  passes.
- **Red-2:** the true reds are:
  - `test_jobs.py::test_migration_creates_jobs_table` (no `snapshot_id`);
  - G-7 and G-8 ×2 (new tables `set()` ≠ the four);
  - `test_engine_still_refuses_a_version_mismatch_first[_reverted_v13-13]`. At base, 13 = latest, so
    the engine reaches the guard instead of the version refusal.
- **Guard, not red:** `test_engine_refuses_reverted_schema_at_latest_version` passes at 13. It
  keeps the guard path covered.
- **Full count:** 1437 = the PR #9 baseline 1332 + 104 (new file) + 1 (the new version-mismatch
  case). The 4 skips are playwright and 3 × sshd.

## v4.1 (signed off by Rama 24 Nov; details and back-out steps in `V41-CHANGES.md`)
1. `idx_admin_approvals_one_open` also covers approved, not yet executed approvals.
2. `keys` keeps `CHECK ((state = 'pending') = (activated_by IS NULL))` and adds
   `CHECK ((activated_by IS NULL) = (activated_at IS NULL))`.
3. The jobs.kind rewrite skips only on the exact `_NEW_JOB_KINDS` text; otherwise it requires
   `_OLD_JOB_KINDS` exactly once, else raises (rolled back).
4. `lost_key_reset` targets `key:<id>`. At execution, the key is revoked only if it is still active,
   and its snapshots are deleted only then (tests; the code rules are for B-105/B-106).
5. `home_discard` targets `setup:<name>`.
6. `(inferred)` is dropped from the one-open index only (DDL). The prose for Q-13, the sweep and the
   table order now reads SIGNED OFF.

## Known warnings (not failures)
- **`git apply` of `B104B-docs.patch`** prints "76 lines add whitespace errors". These are the blank
  context lines of the two verbatim `.diff` files. The patch itself creates the `.gitattributes`, so
  `-whitespace` is not yet in effect during the apply. Every apply exits 0, and both TREE checks
  match (`apply-check.txt`). `git diff --cached --check` over all 11 files exits 0 (`diff-check.txt`).
- **`StarletteDeprecationWarning`** from `fastapi/testclient.py:1` ("Using `httpx` with
  `starlette.testclient` is deprecated") appears in every run. It is pre-existing, unrelated, and
  there is nothing to fix.

## File index

| File | What |
|---|---|
| `patches/B104B-m14.patch`, `patches/B104B-docs.patch` | the two commits as patches; docs applies on top of m14, and alone |
| `msg/01-B104B-m14.txt`, `msg/02-B104B-docs.txt` | commit messages |
| `tree1.txt`, `tree2.txt` | `git write-tree` after staging commit 1, then commit 2 |
| `apply-check.txt` | fresh `origin/rama`: m14 → tree1 MATCH → docs → tree2 MATCH; docs alone applies |
| `diff-check.txt` | `git check-attr -a` for the .diff files; `git diff --cached --check` exit 0 |
| `red1-*`, `red2-*`, `green-*`, `full-*`, `make-test.txt`, `docs-tests-final-*` | test logs (headers: date, HEAD, status, venv, versions, command; `docs-tests` also `# tree:`) |
| `final-files.sha256`, `final-files-restored.txt` | the final commit-1 files' hashes, and the check after the red stages |
| `interdiff-m14.txt` | `git diff caf7f972… 5168e893…`: commit 1 before vs after the sign-off fixes |
| `doc-vs-code.venv*.txt` | the doc's SQL vs what `_m14` sends: IDENTICAL, no exceptions |
| `ddl-verbatim-check.venv*.txt` | `_m14` = DDL v4 + exactly the declared v4.1 edits |
| `ddl-check-final.venv*.txt`, `ddl-check-final-vs-v4.diff` | the unedited `ddl-check.py` on the final doc; diff vs the recorded v4 run (2 expected lines) |
| `diff-proof-v3-v4.txt`, `diff-proof-v4-v4.1.txt` | each history diff rebuilds its target byte-exactly with `patch` |
| `doc-path-grep.txt` | `grep -nE '~/\|/home/akshay'` on the final doc: exit 1 |
| `plan08-word-diff.txt` | the plan/08 re-wrap changed only line breaks (0 word changes) |
| `table-count.txt` | 16 tables on a fresh v14 DB |
| `tests-reading-docs.txt` (+ `tests-reading-docs.sh`, `classify_doc_mentions.py`) | which tests read README/plan: none; exact commands and output; per-mention classification |
| `V41-CHANGES.md` | the v4.1 items: lines, tests, back-out |
| `BUILD-NOTES.md` | the build folder's notes: deviations, the 13/latest list, decisions |
| `bundle-named-files-check.txt` | every file named in the msg Evidence lists is present: 0 missing |
| `ddl_verbatim_check.py`, `doc_vs_code_check.py`, `make_final_doc.py`, `run-stage.sh`, `make_bundle.sh` | the scripts that produced the above (`make_final_doc.py` rebuilds the doc from v4) |
| `constraints.venv.txt`, `constraints.venv-220.txt` | the pinned package sets of the two venvs |
| `discovery/` | the read-only discovery: `FINDINGS.md`, the `probe_*.py` scripts and their outputs |
| `.gitattributes` | `* -text` (no line-ending conversion, as in the other evidence folders) |
| `SHA256SUMS` | over every other file |
