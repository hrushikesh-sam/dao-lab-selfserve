# X-ML — Stage 1-0 state (read-only), 2026-11-25

- `git fetch origin`; **origin/rama = `8bfcf4b`** ("B-106: single-use challenges and owner cancel
  (re-land of #16 onto rama)"). **HEAD = `8bfcf4b`** (branch `x-ml`).
- rama moved `7a2dbad` → `8bfcf4b`: 9 commits (B-105 `8343e26` + F13-1..3, merge #13 `88d0cb6`,
  B-106 `2168241` + F16-1..2, re-land `8bfcf4b`). `git diff --stat 7a2dbad origin/rama -- <the 20 files>`
  is **empty**: none of the X-ML files changed on rama.
- `git status --short` (venvs excluded): ` M` on the hand-edited script only (1 entry).
- `git diff --stat origin/rama`: 1 file, +1/−1 (the hand-edited script).
- Conflict markers (`^<<<<<<<` / `^>>>>>>>`, venvs and .git excluded): **0**.

## Per-file state (working tree vs `git show <commit>:<path>` and the commit diffs)

The partial port from the interrupted Stage 1 did **not** survive the move onto `8bfcf4b`: every
file in the list is byte-identical to origin/rama (or absent). So every item is **missing**, except
#17.

| # | File | State |
|---|---|---|
| 1 | `inventory/host_vars/dpu-01.yml` | missing (= rama) |
| 2 | `playbooks/apps/ml-tvm/deploy.yml` | missing (absent) |
| 3 | `playbooks/apps/ml-tvm/cleanup.yml` | missing (absent) |
| 4 | `playbooks/deploy.yml` | missing (= rama) |
| 5 | `playbooks/files/daolab-app-exporter` | missing (= rama) |
| 6 | `playbooks/files/daolab-ml-smoke` | missing (absent) |
| 7 | `playbooks/probe.yml` | missing (= rama) |
| 8 | `playbooks/roles/verify/tasks/run.yml` | missing (= rama) |
| 9 | `playbooks/verify_clean.yml` | missing (= rama) |
| 10 | `service/runner.py` | missing (= rama) |
| 11 | `setups/tb-01.yml` | missing (= rama) |
| 12 | `tests/test_catalog.py` | missing (= rama) |
| 13 | `usecases/ml-tvm.yml` | missing (absent) |
| 14 | `COMPLETION_LOG.md` | missing (= rama) |
| 15 | `README.md` | missing (= rama) |
| 16 | `docs/dao-doc-verification.md` | missing (= rama) |
| 17 | the `DAOLAB_DEPLOYABLE` script line | **done** (by Akshay). Its −/+ lines are byte-identical to `e8eb2bc`'s hunk for `scripts/run/`, so that hunk is left out when `e8eb2bc` is ported |
| 18 | `tests/test_playbooks.py` | missing (= rama) |
| 19 | `tests/test_ansible_runner.py` | missing (= rama) |
| 20 | `tests/test_ml_tvm.py` | missing (absent) |

- **Changed files outside the list:** none (the only change is #17).
- **Hunks that contradict the commits:** none (nothing ported yet).
- **Re-check on the new base:** the four commits applied in order to a scratch index made from
  `origin/rama` `8bfcf4b` (`git apply --cached`): rc=0 for all four, 0 unmerged paths.
- `35aa3367` is a patch-file sha256 (A-1), not a commit; it is located in Stage 1-4.

**Result: no STOP condition** (0 conflict markers, 0 unexplained extras, 0 contradicting hunks).
Continue with Stage 1-1.

## Stage 1-1 baseline (origin/rama `8bfcf4b`, clean worktree /tmp/xml-base, removed afterwards)

| Venv | ansible-playbook used | Result | Log |
|---|---|---|---|
| `.venv` | `[core 2.21.6]` | **1578 passed, 4 skipped, 0 failed**, exit 0 (15:45) | `logs/baseline-221.txt` |
| `.venv-220` | `[core 2.20.11]` | **1578 passed, 4 skipped, 0 failed**, exit 0 (15:18) | `logs/baseline-220.txt` |

Header: tree `1c122a23…`, 0 status entries, Python 3.12.13, SQLite 3.53.1, `service` imported from
`/tmp/xml-base/service`. Skips: playwright (1), sshd not installed (3). 1 warning: starlette's httpx
deprecation (fastapi testclient). F = 0: rama has no ml-tvm `become: true` task, so the A-1 failure
cannot occur there (it appears only after the port, red-2).
