# B-104 PR A — burned migrations 12/13 and the reverted-schema guard: evidence

Base: `origin/rama` `a03f858` (PR #8, B-103, merged). Spec: B-104-DDL v4 §1, §2, §6.1, §6.2 T-1/T-2 (signed off by Rama; Q-3 and Q-4 approved). The `service/engine.py` change is approved by its owner under Q-3 (b).

Controller: Python 3.12.13, SQLite 3.53.1, ansible-runner 2.4.3, pytest 9.2.0. Every run was done twice:
- `-221` = ansible-core 2.21.6;
- `-220` = ansible-core 2.20.11.

All tests use synthetic DBs (`:memory:` or pytest's `tmp_path`). The reverted v12/v13 shapes are literal DDL copied from the B-104 fingerprint (FINDINGS §3), and no reverted code is imported.

## Files

| File | What it is |
|---|---|
| `B104A-guard.patch` | The change, base tree to tested tree (`git diff --binary --full-index`), 4 paths: `service/migrations.py`, `service/cli.py`, `service/engine.py`, `tests/test_migrations_guard.py` (new) |
| `msg/01-B104A-guard.txt` | The commit / PR message draft |
| `trees.txt` | Base tree (`origin/rama` `a03f858`) and the tested tree the patch produces |
| `apply-check.txt` | The patch applied to a fresh `origin/rama` worktree: `--check`, apply, TREE MATCH, `diff -r` against the tested tree, then the worktree removed |
| `venvs.txt` | Interpreter and package versions of the two venvs |
| `red-221.txt`, `red-220.txt` | Guard stage 1 (red-1): the 25 guard tests against unchanged `service/` |
| `red2-trap-221.txt`, `red2-trap-220.txt` | Guard stage 2 (trap): placeholders 12/13, `RevertedSchema` and the markers defined, but the guard **not called** in `apply()` and `cli.py` unchanged. A temporary state that was never shipped |
| `engine-red-221.txt`, `engine-red-220.txt` | Engine stage 1 (red): the 4 engine tests with `service/engine.py` unchanged (rest of the patch applied) |
| `engine-green-221.txt`, `engine-green-220.txt` | Engine stage 2 (green): the 4 engine tests with the final change |
| `green-221.txt`, `green-220.txt` | Final change: `tests/test_migrations_guard.py` (29) + `tests/test_migrations.py` (13), verbose |
| `full-221.txt`, `full-220.txt` | Full suite on the final change, with a header giving HEAD and the working-tree diff |
| `trap_probe.py` | Probe: runs `models.init_db` on the test file's reverted fixtures (synthetic, in memory) and prints what the code in the tree does |
| `trap-probe-red1.txt` | Probe output on unchanged code |
| `trap-probe-red2.txt` | Probe output at the trap stage |
| `trap-probe-green.txt` | Probe output on the final change |
| `README.md` | This file |
| `SHA256SUMS` | sha256 of every other file here |

The pytest logs and `venvs.txt` contain the local checkout and interpreter paths that pytest and venv print. Nothing in them is a secret.

## Stages

Guard tests. Red-1 and the trap stage ran on the 25 guard tests, before the 4 engine tests were added.

| Stage | Code | Result (221 and 220) |
|---|---|---|
| red-1 | unchanged `origin/rama` | `24 failed, 1 passed` on both. The pass is G-7 (a clean v11 DB upgrades), already true today. Probe: a reverted v13 DB is refused only by `SchemaTooNew` (v13 > v11), and the same tables are `ACCEPTED: init_db -> [], user_version 11` once user_version is hand-set to 11 |
| trap (never shipped) | placeholders, guard not called | `16 failed, 9 passed` on both. G-3: `Failed: DID NOT RAISE RevertedSchema`. Probe: `reverted v13: ACCEPTED: init_db -> [], user_version 13` |
| green | final change | `42 passed` on both (29 in `test_migrations_guard.py` + 13 in `test_migrations.py`). Probe: all three reverted DBs raise `RevertedSchema`, DB unchanged |

Engine tests (`-k engine`):

| Stage | Code | Result (221 and 220) |
|---|---|---|
| red | `service/engine.py` unchanged | `2 failed, 2 passed, 25 deselected` on both. On a reverted v13 DB at user_version 13, `engine.main([])` returned 0 and went on to the tick (`assert 0 == 1`). On a clean DB the guard never ran. The 2 passes are the unchanged version-mismatch refusals |
| green | final change | `4 passed, 25 deselected` on both |

Full suite, final change: `1332 passed, 4 skipped` (0 failed) on both. That is the B-103 baseline 1303/4/0 plus the 29 new tests, with the same 4 skips (playwright, and sshd ×3). The 1 warning is the Starlette `httpx` deprecation notice, also present in B-103.

## How to verify

1. Check the files: `sha256sum -c SHA256SUMS` (every line `OK`).
2. The patch: sha256 `4df433b112d0d5abd24810896ac4fdb03d41c5c5eb946998319addba6ff2cfd6`.
3. Apply it to `origin/rama` `a03f858` in a fresh worktree, then compare trees:
   ```sh
   git fetch origin
   git worktree add --detach <worktree> a03f858
   cd <worktree>
   git apply --check B104A-guard.patch && git apply B104A-guard.patch
   git add -A && git write-tree     # must print ce77b74dd5050634cec05d7f5b51424c21a3d282
   ```
   The base tree is `e5e99c0557cbd8d3f804a7c57a8109aa41030872`.
4. Set up the two venvs in the worktree (Python 3.12):
   ```sh
   python3.12 -m venv .venv     && .venv/bin/python -m pip install -e '.[ansible]'
   python3.12 -m venv .venv-220 && .venv-220/bin/python -m pip install -e '.[ansible]' 'ansible-core==2.20.11'
   ```
5. Run the tests with each venv's python:
   ```sh
   python -m pytest tests/test_migrations_guard.py tests/test_migrations.py -v -rsf -p no:cacheprovider   # 42 passed
   python -m pytest tests/test_migrations_guard.py -k engine -v -rsf -p no:cacheprovider                 # 4 passed, 25 deselected
   python -m pytest tests/ -q -rsf -p no:cacheprovider                                                   # 1332 passed, 4 skipped, 0 failed
   ```
6. Optional probe, from the worktree root: `python -I <evidence-dir>/trap_probe.py .`. The argument is the checkout to import `service` and `tests` from. On the patched tree, all three reverted DBs raise `RevertedSchema`.
