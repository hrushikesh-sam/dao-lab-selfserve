# B-109b + B-109c: progress (2026-11-28) — DONE, ready for Akshay's review

## Result
- Baseline once at BASE d97085a (.venv): 1818 passed, 4 skipped.
- Final A, the full suite on .venv (ansible-core 2.21.6): 1993 passed, 4 skipped.
- Final B, ansible files + new tests on .venv-220 (2.20.11): 1121 passed, 1 skipped.
- Final C, tests/dpu on Python 3.10.12: 143 passed.
- Patch b109bc.patch (16 files) → fresh BASE worktree: TREE MATCH c8ca04ff….
- Split: commit 1 alone 185 passed, 1 skipped (+ tests/dpu 143); commit 1 + 2 = the tested tree.
- ~/b109bc: everything staged at the tested tree; nothing committed or pushed.
- Details per stage: README.md; per commit: SPLIT.md, COMMIT_MSG_b.txt, COMMIT_MSG_c.txt.

## Stops along the way
1. A read-only grep whose file list named the env template was denied (the `*.env*` rule).
   Akshay ran his own check (SECRET-CHECK.md); afterwards no command named that file.
2. A combined write of README.md + SPLIT.md was denied by the auto-mode classifier
   ("Undermining Oversight"). The cause was text about Akshay's secret check. Per his
   instruction, nothing about that check is written here or in README.md.
3. Two early green-b runs (logs/*-STOPPED.txt) hung on
   test_verify_says_when_the_output_does_not_match while the B-109c code was being written;
   they were stopped on Akshay's instruction and are not counted. Run alone, that test and its
   neighbours passed (logs/hang-recheck-221.txt); the final runs did not hang. The cause is not
   known.

## Decisions taken without asking
- The flag-off bodies of _run_cleanup and _run_reconcile are untouched behind a two-line early
  branch; the snapshot variants carry their own copy of the finish (safest parity).
  `_context`'s body was restructured (same output when the flag is off).
- The engine never starts home_wipe.yml when the wipe is not allowed (the playbook's own gate
  is the second layer).
- Reconcile order: save → reconcile.yml → gated wipe → verify (the leftovers go before the
  wipe, as in the cleanup job).
- A reused row (D-7-4) also gets created_at = now, so retention never prunes a fresh save.
- Stale capturing rows: their attempt files are deleted and `failed/stale` is audited.
- Mock `restore_incomplete` maps to the `check_home` step (B-110 may rename it).
- D-14: a missing or other-owner home on an enabled setup is home:lab.
- verify_clean with the flag off: steps and residue unchanged; the job log gains one
  "PLAY [check the home …]" header line (the play ends at once).
- Capture dir: the playbook sets mode 0700 but not the owner (that would fail without become
  in the Lab); the capture script refuses unless the effective uid owns it.
- tests/test_engine_snapshot.py: I fixed my own parametrize bug (1 parameter, not 2) before
  the first green run; no expectation changed.
