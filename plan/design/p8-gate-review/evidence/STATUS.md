# Operator evidence run (local checkout, HEAD 40ebd6d, P-0 + F-5-1 applied in working tree, nothing committed)
Gate results (this run, scratch machine, NOT production):
- P-0 red: 189 failed / 74 passed on py3.11 and py3.12; both canary tests failed (before.txt)
- P-0 green: 319 passed on both (after.txt)
- F-5-1 red: 5 failed / 20 passed on both; green: 25 passed on both
- C-1 catalog: grep rc=1, 0 lines (usecases/ and setups/ exist)
- ROW 25 daolab_vm_names harness: FAIL (2 failed, 1 passed, both Pythons): canary created in destroy mode with the extra var
  -> row 25 is VULNERABLE on fake lab. STOP CONDITION. Not fixed (out of scope; candidate fixes in M-VM-NAMES-HARNESS.md).
- Full suite: 1166 passed, 4 skipped on both (junit in ci/)
- OPEN (no production access): db-audit-exit, db-audit-ids, F-5-1 prod grep, fact archive
- Skips (from junit; pytest -rs printed none): dashboard e2e (collection skipped, playwright), 3 sshd-not-installed tests, both Pythons
