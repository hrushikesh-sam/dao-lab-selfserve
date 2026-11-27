# B-109b + B-109c — the suggested split into two commits

One staged patch (`b109bc.patch`, against BASE `d97085a`) holds both. Commit them in this order,
each with its own message (COMMIT_MSG_b.txt, then COMMIT_MSG_c.txt). Together the two file sets
are exactly the patch's 16 paths, with no overlap.

## Commit 1 — B-109b (playbooks): COMMIT_MSG_b.txt

| File | Status |
|---|---|
| playbooks/files/daolab-wipe.py | new, mode 100755 |
| playbooks/snapshot_capture.yml | new |
| playbooks/home_wipe.yml | new |
| playbooks/verify_clean.yml | modified (gated home:lab check) |
| tests/dpu/test_wipe.py | new |
| tests/test_playbooks_snapshot.py | new |
| tests/fakelab.py | modified (getent uid from the state, default 1001) |

```
git apply --index --include='playbooks/*' --include='tests/dpu/test_wipe.py' \
  --include='tests/test_playbooks_snapshot.py' --include='tests/fakelab.py' b109bc.patch
```

## Commit 2 — B-109c (controller): COMMIT_MSG_c.txt

| File | Status |
|---|---|
| service/snapshots.py | new |
| service/runner.py | modified |
| service/engine.py | modified (_run_cleanup, _run_reconcile, _context, the stale-capture tick step) |
| service/config.py | modified (snapshot_root, snapshot_cap_bytes) |
| service/models.py | modified (audit actions snapshot_capture, snapshot_delete) |
| .env.example | modified (the two settings) |
| tests/test_snapshots.py | new |
| tests/test_runner_snapshot.py | new |
| tests/test_engine_snapshot.py | new |

Commit 2 needs commit 1: its runner runs the B-109b playbooks, and its end-to-end test imports
`tests/test_playbooks_snapshot.py`. Commit 1 needs nothing from commit 2 (the playbooks take only
extra vars; no service code reads them yet).

## How each commit was checked on its own

1. A fresh worktree at BASE; commit 1's files applied from the full patch with `--include`
   (logs/split-apply.txt): 7 files, tree `2068a0173ea06a0094084692069d7a6317056708`.
2. On that tree, with `service` imported from the split tree (BASE's code; the log header shows
   the path): `tests/dpu/test_wipe.py tests/test_playbooks_snapshot.py tests/test_playbooks.py
   tests/test_cleanup_flow.py` on `.venv` → **185 passed, 1 skipped** (sshd not installed)
   (logs/split-b-221.txt); `tests/dpu` on Python 3.10.12 → **143 passed**
   (logs/split-b-dpu-310.txt).
3. Commit 2 applied on top with `--exclude` of commit 1's paths: tree
   `c8ca04ffca29a78acf69a4eb6cc07efe2e3dfe94`, the tested tree (**TREE MATCH**). Commit 2's tests
   are therefore the final verification of that exact tree: A (full suite, .venv) 1993 passed,
   4 skipped; B (.venv-220) 1121 passed, 1 skipped; C (3.10.12) 143 passed.
4. The split worktree was removed.
