# P-0c: row 25 (`daolab_vm_names`), the fix and the local gate evidence

**Scope:** closes EXTRAVAR-MATRIX row 25, the STOP condition in the Phase 8 gate.
**Base:** `40ebd6d` (branch `rama`) with `P-0-final.patch` and `F-5-1.patch` applied, in that order.
Also applies on `akshay` (`9c8a089`) with the same two patches first.
**Status:** local and fake-lab evidence only. **None of this is production evidence** (runbook rule). No code is committed: this is a patch file for review. §1–§4 describe v1; **§5 is v2**, which `P-0c-v2.patch` contains.

| File | sha256 |
|---|---|
| `P-0-final.patch` (Rama, unchanged) | `cc3b42cf7c601bd6c414e46847f3ef78c84bf1d3e65a895ce582dd8cf51c6409` |
| `F-5-1.patch` (Rama, unchanged) | `0c06d643c4d371655eac4280a00b8f440106f378d729cb4625d8efee75d69f0a` |
| `P-0c-v2.patch` (new; supersedes `P-0c.patch` 82cdf3f9…) | `90bebe5c2964c7c5dba4d781259f2bb00939ce53c8238a2d6588973713e9941d` |
| `F-5-1b.patch` (applies on top of P-0c v2) | `823a54bcdefd8ae52a6c3226144b93978d25583e5d37adfbc079dd78471c0f5d` |
| `audit_stored_inputs.py` (Rama, unchanged) | `0f2c8f5fa5b5ca0e53c822c42a9c75f56478cdf0b6768cc873e1dc654e3fab82` |

## 1. What the harness showed (before P-0c)

I ran `test_orphans_canary_DRAFT.py` unchanged on ansible-core 2.19.15 (Python 3.11) and on ansible-core 2.21.6 (Python 3.12):

| Test | Result |
|---|---|
| list mode | PASS: listing does not evaluate a name |
| destroy mode, poisoned name passed as `daolab_vm_names` | **FAIL**: the canary file was created on the controller |
| list then destroy, chained like the engine | **FAIL**: the canary file was created in the destroy half |

The path is: XO VM name → `orphans.yml` list (prefix filter `^daolab-r[0-9]+-` only) → `AnsibleRunner.tagged_vms` → `engine._sweep_orphans` (prefix `_TAGGED.match` only) → `AnsibleRunner.destroy_vms` → extra var `daolab_vm_names` → templated by ansible → `lookup('pipe', ...)` runs on the controller.

Anyone who can create or rename a VM in Xen Orchestra can get controller RCE. That is the same severity as F-1, through a different door.

## 2. The fix (Rama's candidate fix 1, "strict fullmatch")

| Where | Change |
|---|---|
| `service/models.py` | `_VM_NAME = daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}` and `vm_name_ok()` (`fullmatch`). This is exactly the form the platform makes: `plan_vms.yml` builds `'daolab-r' ~ id ~ '-' ~ role`, and a role must already pass `ident_ok`. It accepts ASCII digits only, because `\d` would also accept Arabic-Indic and other Unicode digits. |
| `service/engine.py` `_sweep_orphans` | Untagged names are skipped as before. Tagged names that fail `vm_name_ok` are dropped before `_orphan()`, and only their **count** is logged (`event=orphan_sweep_rejected count=N`). The names are never logged, because they are attacker-chosen text. |
| `service/runner.py` `AnsibleRunner.destroy_vms` | Chokepoint. If any name fails `vm_name_ok`, the whole call is refused: `RunResult(ok=False, error="refused: N VM name(s) ...")` and nothing is run. This should never fire, since the engine has already filtered. It exists so that a future caller can't reopen the hole. |
| `playbooks/orphans.yml` list mode | The same pattern replaces the prefix-only filter, so a poisoned name is never reported in the first place. This is defence in depth: Python is the authority, because Jinja's `match` with `$` still accepts a trailing newline, and the engine rejects that name. |
| `tests/test_vm_names_strict.py` | 35 tests: the accepted and rejected names, the engine filter, the runner refusal, and an **adopted** version of the harness. The adopted harness runs real `ansible-playbook` on the fake lab, and runs the engine's sweep end to end against it. |

A poisoned VM is **left in place** (not destroyed) and the warning is logged every tick. A human has to delete it in XO. That is deliberate: the sweep cannot safely name it to ansible.

## 3. Evidence (all local / scratch)

| Gate | Before | After |
|---|---|---|
| P-0 red/green (Rama's tests) | 189 failed / 74 passed | 319 passed (both Pythons) |
| F-5-1 red/green | 5 failed / 20 passed (secret found in `job-5/run-1-deploy/env/extravars`) | 25 passed (both Pythons) |
| C-1 catalog delimiters (`usecases`, `setups`) | — | 0 lines: PASS |
| Row 25 (Rama's draft harness) | 2 failed / 1 passed: STOP reproduced | see §4 |
| P-0c v1 (`test_vm_names_strict.py`) | 35 failed (all for the right reason: poisoned names handed on, runner ran them, canary file created) | 35 passed; with the orphan, runner and engine suites, 175 passed (both Pythons). v2 numbers: §5 |
| Full suite, P-0 + F-5-1 + P-0c v1 | — | **1201 passed, 4 skipped, 0 failed** on both Pythons (Rama's 1166 + 35 new). Skips: dashboard e2e (no Playwright), 3 × "sshd not installed" |
| Full suite, P-0 + F-5-1 + P-0c v2 + F-5-1b | — | **1235 passed, 4 skipped, 2 xfailed, 0 failed** on both Pythons |

## 4. Residual items: decisions for Rama

1. **The draft harness test 2 stays red, by design.** It feeds a poisoned `daolab_vm_names` straight into `orphans.yml`. The playbook trusts its caller. The only caller in the service, `AnsibleRunner.destroy_vms`, now refuses such a list (`grep -rn daolab_vm_names service/` shows one producer). I adopted tests 1 and 3, in the form the engine actually runs. To make the playbook itself safe against any caller, the next step would be Rama's candidate fix 2: destroy by XO VM id rather than by name. That changes the `tagged_vms` contract, so I did not do it without agreement.
2. **`reconcile.yml` also accepts `daolab_vm_names`.** No service code passes it today (the orphan sweep uses `orphans.yml`). Its header comment saying the sweep passes it is out of date. If anything starts passing it, it must go through `vm_name_ok` first.
3. **Who can create or rename a VM in XO** is still UNKNOWN. That is a question for whoever administers the XCP-ng pools (XO only mirrors the pools' VM names). The read-only count on the laptop's XO found 0 tagged and 0 rejected names on both pools (`evidence/part-a/PART-A-RESULTS.md`, A3).
4. **Ansible versions:** tested on 2.19.15 and 2.21.6. The design's F-5-3 floor is 2.20, so 2.19 is below it but still covered.
5. **Runbook nit (§4):** `pytest -rs -rf` prints failures only. The last `-r` wins, so the skip list comes out empty. Use `-rsf`. I took the skip reasons from the junit XML instead (`evidence/tests/skips-py*.txt`).

## 5. v2: answers to the review (22 Nov, late)

| Review point | v2 |
|---|---|
| Draft test 2 "must be xfail or deselected" | It was never in the suite: the harness says to remove the draft after running it, and I did. Its intent is now **explicit** as `test_a_playbook_never_evaluates_a_name_passed_straight_in`, an `xfail(strict=True, raises=AssertionError)` parametrized over **orphans.yml and reconcile.yml**. Both XFAIL on the canary assertion itself. Once P-0d (destroy by VM id) lands they XPASS, the strict mark fails the suite, and the xfail gets removed. |
| `reconcile.yml` also takes `daolab_vm_names` | `test_the_playbooks_that_take_daolab_vm_names_are_known` pins the readers to `orphans.yml` and `reconcile.yml`. `test_the_service_has_one_producer_of_daolab_vm_names_and_it_is_checked` pins the producers (string literals in `service/*.py`, so comments don't count) to exactly one, in `AnsibleRunner.destroy_vms`, which calls `vm_name_ok`. A new reader or producer fails these tests. |
| Keep the `orphans.yml` regex in sync | `test_the_orphans_yml_list_filter_is_the_same_pattern_as_vm_name_ok` (literal equality with `models._VM_NAME`) plus a behavioural check over every accepted and rejected name. The one known difference, a single trailing newline (Jinja `$`), is asserted explicitly, and the engine's fullmatch rejects it. |
| The `orphans.yml` change was not required | Correct: Python is the control. It is kept as defence in depth so a poisoned name never appears in the list output. **Your call**; dropping it means deleting that hunk and the two sync tests. |

**Tests:** `test_vm_names_strict.py` plus the orphan, runner, engine and F-5-1b suites: **209 passed, 2 xfailed** on Python 3.11 and 3.12.
