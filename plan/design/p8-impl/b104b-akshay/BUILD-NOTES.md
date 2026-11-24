# B-104 PR B: both commits, final evidence (after Rama's sign-off, 24 Nov)

- **Worktree:** `~/b104b`, branch `b104b` (`--no-track`, no upstream). HEAD `a67d4ac` (= `origin/rama`).
  **Nothing committed**; the 11 files are staged only.
- **Venvs:** Python 3.12.13 (uv CPython), SQLite 3.53.1, ansible-runner 2.4.3, pytest 9.2.0. `.venv`
  has ansible-core 2.21.6, `.venv-220` has 2.20.11. Package sets are identical to `~/b104a`'s
  (`constraints.venv*.txt`).
- The earlier outputs moved to `~/notes/b104b/build-pre-signoff/`. They are history only, not
  evidence. Everything here was regenerated from the final files.

## Files

| Commit | Files (Rama's list) |
|---|---|
| 1 (tree1 `5168e893a4181bea994a409326e75797e92d820c`) | `service/migrations.py`, `tests/test_migration_14.py` (new), `tests/test_jobs.py`, `tests/test_migrations_guard.py` |
| 2 (tree2 `5505478e9d4c835e8c763591b72eba6be3135edf`) | `plan/design/p8-impl/B-104-DDL.md`, `…/B-104-DDL.v3-v4.diff`, `…/B-104-DDL.v4-v4.1.diff`, `…/.gitattributes` (all new); `plan/08-cli-config-ops.md`, `README.md`, `plan/README.md` |

**Patches**
- `B104B-m14.patch` = `git diff --cached --binary origin/rama` (commit 1), sha256
  `faed58e16f3a2c0f9fb11a01bbd7fcd10f2530e0c301565ae9c0470af0df298a`, 4 files, +1253/−15.
- `B104B-docs.patch` = `git diff --binary tree1 tree2`, sha256
  `ca46df7ee4e40a8163d266005f0a01e533116f5e84bfa750a6343e943b40f911`, 7 files, +1556/−12.

**`plan/design/p8-impl/.gitattributes` = `*.diff -text -whitespace`.**
- Akshay's decision (24 Nov), within Rama's file list. Rama saw `*.diff -text`.
- Why: the diffs are verbatim, so their whitespace is data. `-text` alone does not stop
  `git diff --check`.
- Tree2 history:
  - `807873ab…` → `8519808d…`: only `.gitattributes` changed.
  - `8519808d…` → `5505478e…` (final review fixes, no tests re-run):
    - `B-104-DDL.md`: the review-files legend line; §6.3 "`ddl-check.py`"; §6's local path
      reworded, meaning unchanged;
    - its regenerated v4-v4.1 diff;
    - `plan/08`: the Schema-evolution bullet re-wrapped, words unchanged (`plan08-word-diff.txt`).
- **Commit 1 is unchanged:**
  - tree1, re-derived from `origin/rama` + the 4 files, = `5168e893…`;
  - `B104B-m14.patch` is byte-identical to `git diff --binary origin/rama tree1`.

## Stages (logs start with the header from `run-stage.sh`)

| Stage | Files | 2.21.6 | 2.20.11 |
|---|---|---|---|
| red-1 | final `test_migration_14.py`; `migrations.py`, `test_jobs.py`, `test_migrations_guard.py` = `origin/rama` | 13 failed, 91 errors, 187 passed | same |
| red-2 | + final `test_jobs.py`, `test_migrations_guard.py`; `migrations.py` = `origin/rama` | 18 failed, 91 errors, 183 passed | same |
| green | all final | 292 passed | same |
| full suite (`tests/`) | both commits' files, **before the .gitattributes change** | **1437 passed, 4 skipped, 0 failed** | **same** |
| `make test` (`.venv` first on PATH) | tree `8519808d…` (`# tree:` line) | **1437 passed, 4 skipped, 0 failed** (exit 0) | (not run) |

**Red-1.** All 104 new tests fail: 13 failed, 91 setup errors "no such table: keys". That includes
`test_v41_4_…[retired|revoked]`: a true red, since the schema is absent. 0 new tests pass, and every
existing test passes.

**Red-2.** The true reds are:
- `test_jobs` (no `snapshot_id`);
- G-7 and G-8 ×2 (new tables `set()` ≠ the four);
- **the new `test_engine_still_refuses_a_version_mismatch_first[_reverted_v13-13]`**. At base, 13 =
  latest, so the engine takes the guard path and prints the RevertedSchema message instead of the
  version refusal.

`test_engine_refuses_reverted_schema_at_latest_version` passes at 13: **a guard, not a red**. It
exists to keep the guard path covered.

**Base files.** They came from `git show origin/rama:<path>` and were then restored from
`final-files/`. `final-files-restored.txt` = `sha256sum -c final-files.sha256`: all 4 OK.

**Full suite.** 1437 = 1332 (the PR #9 baseline) + 104 (new file) + 1 (the new version-mismatch
case). The 4 skips are playwright and 3 × sshd.

**Which tree each run saw.**
- The full suites (both venvs) ran on the tree before the .gitattributes change.
- `make test` (`.venv`) ran on `8519808d…`.
- The final tree is `5505478e…`.
- The trees differ from the final tree only in files under `plan/`: `.gitattributes`,
  `B-104-DDL.md`, its v4-v4.1 diff, and line wrapping in `plan/08-cli-config-ops.md`.
- No test reads any of them (`tests-reading-docs.txt`, which shows the exact commands and their
  output). Commit 1's files are identical in all of these trees (tree1 `5168e893…`).

**The one warning** in every run is pre-existing and unrelated. It is a `StarletteDeprecationWarning`
from `fastapi/testclient.py:1` ("Using `httpx` with `starlette.testclient` is deprecated").
- In the full suites and `make test`, it is raised when that module is imported.
- In the targeted runs, it is attributed to
  `tests/test_reservation_states.py::test_cancel_releases_a_queued_booking`, the first test there that
  imports it.

Nothing to fix here.

## The "13 / latest" list in `tests/test_migrations_guard.py`

| Line(s) | What | Decision |
|---|---|---|
| `:1-6` module docstring | "versions 12 and 13…" | **changed**: notes PR B, no fixed latest |
| `:5`, `:53`, `:121`, `:141-149` | the reverted v12/v13 fixtures, which set `user_version = 13` | kept: the reverted DB's own version |
| `:216-224` G-3 | asserts the reverted fixture is at 13 | assertion kept (fixture fact); **docstring changed** (no longer "at the latest version") |
| `:225-229` G-4 | `"latest"` parameter, dynamic | kept |
| `:238`, `:252`, `:256` G-5 | sets 13; forbids `"v13"` / `"13"` in the message | kept (message hygiene) |
| `:273` G-9 | reverted v13 fixture | kept |
| `:293-297` G-6 | `range(1, latest_version() + 1)` | kept (dynamic) |
| G-7 / G-8 | dynamic, plus the closed `M14_NEW_*` lists; parametrize `[12, 13]` | kept (the placeholders) |
| G-10b / G-11 | `latest_version() + 1` | kept (dynamic) |
| T-1 | `latest_version() >= 13`, `by_version[13].name` | kept (true at 14; about the placeholders) |
| T-2 | `MIGRATIONS[:13]`, `apply == [12, 13]`, `== 13` | kept (about the placeholders) |
| engine comment block | "latest_version() is 13" | **changed** (both cases described) |
| `test_engine_refuses_reverted_v13_db_at_latest_version` | name | **renamed** `test_engine_refuses_reverted_schema_at_latest_version`; keeps `user_version = latest_version()` |
| `test_engine_still_refuses_a_version_mismatch_first` | parametrize | **case added** `(_reverted_v13, 13)`. That is the natural home: the existing version-mismatch test. It now also asserts DB unchanged for every case |

## Checks

| File | Result |
|---|---|
| `doc-vs-code.venv*.txt` | **IDENTICAL, no exceptions**: 13 CREATEs, 3 add_column (in order), `_OLD/_NEW_JOB_KINDS`; the code's table order = §3.1 |
| `ddl-verbatim-check.venv*.txt` | `_m14` = DDL v4 + exactly the declared v4.1 edits (`ddl_verbatim_check.py`, updated for v4.1-1's dropped tag) |
| `ddl-check-final.venv*.txt` | unedited `ddl-check.py` on the final doc: exit 0, both venvs identical. Diff vs the recorded v4 run (`ddl-check-final-vs-v4.diff`): line 1, base v13 (was v11); and the S-shaped canary on `keys.activated_at` is now refused by the v4.1-2 pair CHECK (was: the timestamp CHECK). Both expected |
| `diff-proof-v3-v4.txt` | the v3-v4 diff is byte-identical to the branch copy (sha `8606064e…`); `patch` on the v3 from `d1bd24d` rebuilds v4 (`f4169f1d…`) byte-exactly |
| `diff-proof-v4-v4.1.txt` | `diff -u --label B-104-DDL.v4.md --label B-104-DDL.md` (22 hunks, no CR); `patch` on v4 rebuilds the final doc (`307b87fe…`) byte-exactly |
| `doc-path-grep.txt` | `grep -nE '~/\|/home/akshay'` on the final doc: **exit 1** (nothing found). The only `/home/` is the DPU's `/home/lab` (E-6's subject), kept |
| `plan08-word-diff.txt` | the re-wrap vs the previous tree2: 1 hunk, 0 word insertions, 0 deletions, identical whole-file word sequence; widest bullet line 95 (file's usual 96). Also the full `--word-diff` vs `HEAD` |
| `table-count.txt` | fresh v14 DB (`python -I`, `/tmp`): **16** tables excluding `sqlite_sequence` |
| `tests-reading-docs.txt` (+ `.sh`) | **no test reads `README.md` or `plan/**`**. Exact commands with output and exit codes: the "opens a file" grep, exit 1; every `.md`, `README` and `plan/` hit is a docstring or comment; `test_api.py:88` counts seeded catalog rows |
| `interdiff-m14.txt` | Akshay's `git diff caf7f972… 5168e893…` (commit 1 before vs after the sign-off fixes; 3 files, +63/−19). Byte-identical to the same command re-run here |
| `diff-check.txt` | `git check-attr -a`: text and whitespace unset on both .diff files. `git diff --cached --check origin/rama` over all 11 staged files: **exit 0** |
| `apply-check.txt` | (1) fresh `origin/rama`: m14 → tree1 **TREE MATCH** → docs → `5505478e…` **TREE MATCH** (tree2); (2) docs patch alone: exit 0. `git apply` warns "76 lines add whitespace errors": the patch creates the .gitattributes itself, so it isn't in effect yet. A warning only; every apply exits 0. Both /tmp worktrees removed (`--force`) |
| `make-test.txt` | `PATH=~/b104b/.venv/bin:$PATH make test`: `# which python` = `~/b104b/.venv/bin/python`; `# tree:` = `8519808d…` (tree2 at the time). Akshay confirmed that `~/b104b` has no env file, so the Makefile's include line is a no-op |

## Decisions taken (Akshay, 24 Nov)
1. **`.gitattributes` → `*.diff -text -whitespace`.**
   - `-text` alone left 71 `git diff --check` findings in the .diff files: 70 blank context lines
     (each a single space) and 1 blank context line at EOF.
   - `-whitespace` turns those checks off. The bytes are identical and they are still text diffs.
   - To be mentioned to Rama in the PR.
2. **`make test`** ran after the change, on the final tree, with the b104b venv first on PATH.
   Akshay checked in his own terminal that `~/b104b` has no env file. The Makefile's include line
   is therefore a no-op.

## Edits beyond the literal prompt (inside Rama's file list), and why
- `B-104-DDL.md` title `(v4)` → `(v4.1)`, to match the status line.
- `plan/08` §daolab init, `:33`: "applies `service/schema.sql` and every pending migration", so the
  description matches the new note.
- `plan/08` Schema-evolution bullet: kept the accurate catalog/state sentence and added the four new
  stateful tables.
- All doc edits are in `make_final_doc.py`: 38 exact replacements on v4, each matching exactly once.
  That includes the final-review fixes: the review-files legend line, the §6.3 path, and the §6
  local path reworded.
