# B-109b + B-109c — evidence

BASE `d97085a65f7108d5448296f10dd73a9b9f483c2f` (origin/rama, the merge of PR #19, B-109a).
Controller: Python 3.12.13; `.venv` = ansible-core 2.21.6, `.venv-220` = ansible-core 2.20.11.
DPU scripts: Python 3.10.12 (`b109a-310`, pytest only, `--confcutdir=tests/dpu`). age v1.1.1.
Every log starts with a header: date (UTC), workdir, HEAD, venv, python, ansible-core, the
ansible-playbook pinned on PATH and in DAOLAB_TEST_ANSIBLE_PLAYBOOK, the directory `service` was
imported from, and the exact command.

## Files

| File | What |
|---|---|
| FINDINGS.md | Stage 0 (read-only): crash-point table, script delivery, flag-off parity, questions + defaults (all approved) |
| SPLIT.md | the two commits: which files, in which order, how each was checked |
| COMMIT_MSG_b.txt / COMMIT_MSG_c.txt | the two commit messages |
| b109bc.patch | `git diff --cached --binary BASE`, 16 paths (both commits) |
| PROGRESS.md | the session log of what was done when, and the decisions taken without asking |
| logs/ | every test run, the tree and apply checks (below) |
| SHA256SUMS | `sha256sum -c SHA256SUMS` |

## Stages

| Stage | Log | Venv | Result |
|---|---|---|---|
| Baseline (BASE, once, a /tmp worktree) | logs/baseline-full-221.txt | .venv | **1818 passed, 4 skipped, 0 failed** |
| B-109b red | logs/red-b-221.txt | .venv | 70 failed (no wipe script, no playbooks); 2 passed (the flag-off verify tests, which already hold today) |
| B-109b first run after the code | logs/wip-b.txt | .venv | 72 passed (development run) |
| B-109b green, tests/dpu | logs/green-b-dpu-310.txt | b109a-310 (3.10.12) | 143 passed |
| Hang re-check | logs/hang-recheck-221.txt | .venv | the 5 tests around the hang point, alone: 5 passed |
| B-109c red | logs/red-c-221.txt | .venv | 2 collection errors: ImportError, no service.snapshots |
| B-109c red, runner file | logs/red-c-runner-221.txt | .venv | 23 failed (no capture/wipe, residue kinds, fail switch); 2 passed (flag-off parity) |
| B-109c first run after the code | logs/wip-c.txt | .venv | 103 passed (development run) |
| B-109c green | logs/green-c-221.txt, logs/green-c-220.txt | .venv / .venv-220 | 103 passed / 103 passed |
| **Final A**: full suite | logs/final-A-full-221.txt | .venv | **1993 passed, 4 skipped** (1818 + 175 new; the same 4 skips) |
| **Final B**: ansible files + new tests (list: logs/final-B-files.txt) | logs/final-B-ansible-220.txt | .venv-220 | **1121 passed, 1 skipped** |
| **Final C**: tests/dpu | logs/final-C-dpu-310.txt | b109a-310 | **143 passed** |
| Patch + tree | logs/tree.txt | — | index tree `c8ca04ffca29a78acf69a4eb6cc07efe2e3dfe94`; patch sha256 `979dff78026ad2c76d145aaf3ab3915e3fd42e39e825bf8f437186b3ec47d3e6` |
| Fresh apply at BASE | logs/apply-check.txt | — | apply --check ok, apply --index ok, **TREE MATCH**, diff --cached --check clean |
| Split: commit 1 alone | logs/split-apply.txt, logs/split-b-221.txt, logs/split-b-dpu-310.txt | .venv / b109a-310 | tree `2068a0173ea06a0094084692069d7a6317056708`; 185 passed, 1 skipped; tests/dpu 143 passed |
| Split: commit 2 on top | logs/split-apply.txt | — | **TREE MATCH** with the tested tree |

logs/green-b-221-STOPPED.txt and logs/green-b-220-STOPPED.txt are early green-b runs, stopped after
hanging on test_verify_says_when_the_output_does_not_match; they are not counted.

The final A/B/C runs were on the working tree after everything was staged; the working tree's
`git write-tree` was still `c8ca04ff…` and `git diff` empty after them.

See also: SECRET-CHECK.md.
