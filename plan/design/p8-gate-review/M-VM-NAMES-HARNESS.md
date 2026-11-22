# M-vm-names canary harness (EXTRAVAR-MATRIX row 25): DRAFT, operator-only

**Status:** a draft test, never executed by its author. Every statement about behaviour is **UNVERIFIED** until you
run it. It is not part of `P-0-final.patch` or `F-5-1.patch`. No commit, push, B-103 or snapshot work is involved.

## Why row 25 is open
`daolab_vm_names` is built from the Xen Orchestra listing (`AnsibleRunner.tagged_vms()` -> `engine._sweep_orphans`
-> `destroy_vms(names)` -> extra var). The only filters on a name are prefix matches (`^daolab-r[0-9]+-` in
`playbooks/orphans.yml` and `engine._TAGGED`), which `daolab-r999-{{ ... }}` satisfies. Static reading cannot say whether
ansible re-templates such a string, nor who can name a VM in XO. So both "user-controlled" and "vulnerable" are UNKNOWN.

## What the draft does
`test_orphans_canary_DRAFT.py` builds a fake Xen Orchestra state (the existing `tests/fakelab.py` `daolab-xo`) with one
benign VM `daolab-r998-y` and one VM named `daolab-r999-{{ lookup('pipe', 'touch <tmp>/canary') }}`, then runs the real
`ansible-playbook` on the real `playbooks/orphans.yml`:
1. list mode (names are read from the fake XO);
2. destroy mode with `daolab_vm_names=[benign, poison]` (what the runner passes);
3. the two halves chained as the engine does (list, then destroy exactly what was reported).
Each test asserts that the canary file does not exist.

## Assumptions (all UNVERIFIED)
1. `tests.test_playbooks` exports `Lab`, `ANSIBLE`, `clean_state` and `_vm` as used (copied from the existing B-73 tests).
2. The fake `daolab-xo vms --prefix` returns names verbatim as JSON, and the state file round-trips braces/quotes/spaces.
3. `orphans.yml` runs to completion on the fake lab with that state (the draft does not assert a return code).
4. The draft covers the controller's evaluation of the name only; it does not model XO's own name restrictions.
5. A passing run proves nothing about other ansible-core versions: run it on every interpreter/core you support.

## Operator commands
```bash
cd /path/to/dao-lab-selfserve            # with P-0 and F-5-1 applied or not: this test does not depend on them
cp /path/to/test_orphans_canary_DRAFT.py tests/test_orphans_canary_DRAFT.py
mkdir -p evidence/m-vm-names
for v in 3.11 3.12; do echo "## python $v"
  .venv-$v/bin/python -m pytest tests/test_orphans_canary_DRAFT.py -q -p no:cacheprovider --tb=short -rf -rs
done 2>&1 | tee evidence/m-vm-names/run.txt
rm tests/test_orphans_canary_DRAFT.py          # it is a draft; keep it out of the tree unless you adopt it
```

### How to read the result
| Outcome | Meaning |
|---|---|
| any test FAILED with "was evaluated" | row 25 is **vulnerable** (stop condition): do not deploy; open a P-0c with the fix below |
| all PASSED | no evaluation observed with this ansible-core on the fake lab (UNVERIFIED for the real lab) |
| ERROR / import error / SKIPPED | the harness assumptions failed; fix the draft, the result says nothing about row 25 |

### Read-only check against the real Xen Orchestra (counts only, no names printed)
```bash
<venv>/bin/python -m service.xo vms --pool "$XO_POOL" --prefix daolab-r | python3 -c '
import json, sys
v = json.load(sys.stdin)
bad = sum(1 for x in v if any(c in x["name"] for c in "{}%#$`;|&<>\\\"\x27") or not x["name"].isascii())
print("tagged VMs:", len(v), "with unsafe characters:", bad)'
```
Needs the same XO environment the engine uses (`DAOLAB_XO_URL`, `DAOLAB_XO_TOKEN_FILE`). Also ask the XO administrator
who may create or rename VMs; that answers the "user-controlled" column.

## Candidate fixes if it fails (NOT implemented; UNVERIFIED)
* Validate every name from `tagged_vms()` with a strict `fullmatch` (for example `daolab-r<digits>-[A-Za-z0-9._-]{1,100}`) in
  `engine._sweep_orphans` / `runner.tagged_vms`; log and skip anything else (never pass it to `destroy_vms`).
* In `orphans.yml`, pass names as IDs rather than names, or select by VM id from the listing, so no free text
  reaches an expression.
* Whether JSON extra vars can carry an "unsafe" marker in your ansible-core is UNVERIFIED; do not rely on it.
