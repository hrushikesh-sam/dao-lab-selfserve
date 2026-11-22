# P-0c operator addendum: row 25 `daolab_vm_names` (DRAFT)

**Status:** `P-0c.patch` is a draft. Nothing in this file is a result; every expected outcome is UNVERIFIED until you
run it. The author did not apply it to your tree or produce evidence for you. No commit, push, merge, PR, B-103 or
snapshot work is part of it. F-1, F-5-1, the production audit, the production grep, the facts manifest, the tripwire
baseline, PR #2 and the per-setup legacy home disposition are unchanged (OPEN where they were OPEN).

## What it changes (static description)
* `service/models.py`: `VM_NAME = daolab-r[0-9]+-[A-Za-z0-9._-]{1,100}` (full match, ASCII, no trailing newline),
  `vm_name_ok()`, `split_vm_names()` (kept names, dropped count).
* `service/runner.py` (`AnsibleRunner`): `tagged_vms()` drops invalid names at the source; `destroy_vms()` is the final
  chokepoint: only valid names go into `daolab_vm_names`; if every name is invalid nothing is run and `ok=False`.
  Logs carry a count only. Ansible "unsafe" markers are not used.
* `service/engine.py` `_sweep_orphans()`: invalid names are skipped before the runner is called (count logged).
* `orphans.yml` is untouched. **OPEN (hardening follow-up):** select VMs by id rather than name.
* Tests: `tests/test_orphans_canary.py` (real ansible-playbook on the fake lab; names pass through
  `AnsibleRunner.destroy_vms`) and `tests/test_vm_names_strict.py` (valid/invalid names, no payload in extravars, no
  invalid name in logs, sweep call site).
* Not covered: the mock runner (`MockRunner`) is not changed; the engine call site filters for every runner.

## 1. Apply (after P-0 and F-5-1; local working tree only)
```bash
cd /path/to/dao-lab-selfserve              # P-0-final.patch and F-5-1.patch already applied, nothing committed
R=/path/to/P-0c.patch
git apply --check "$R"                     # expect no output (UNVERIFIED on your tree)
mkdir -p evidence/p0c evidence/tests evidence/ci
```

## 2. RED: tests only
```bash
git apply --include='tests/*' "$R"
for v in 3.11 3.12; do echo "## $v"; .venv-$v/bin/python -m pytest tests/test_orphans_canary.py tests/test_vm_names_strict.py \
   -q -p no:cacheprovider --tb=short -rf; done 2>&1 | tee evidence/p0c/before.txt | tail -n 30
```
Expected (UNVERIFIED): failures on both interpreters. The meaningful reds are the two canary tests
(`test_destroy_mode_never_evaluates…`, `test_the_two_halves_chained…`); the unit tests fail because `vm_name_ok` and
`split_vm_names` do not exist yet. `test_list_mode_never_evaluates_a_vm_name` is expected to pass in both states.
If the destroy canary test PASSES here, stop and report: the red is not reproducing.
```bash
rm tests/test_orphans_canary.py tests/test_vm_names_strict.py            # back to the P-0 + F-5-1 tree
```

## 3. GREEN: full patch
```bash
git apply "$R"
for v in 3.11 3.12; do echo "## $v"; .venv-$v/bin/python -m pytest tests/test_orphans_canary.py tests/test_vm_names_strict.py \
   tests/test_orphans.py tests/test_ansible_runner.py tests/test_runner_secret.py -q -p no:cacheprovider -rf; done 2>&1 \
   | tee evidence/p0c/after.txt | tail -n 12
git status --porcelain | tee evidence/p0c/status.txt                     # two more untracked tests than the F-5-1 run
git add -N tests/test_pubkey_strict.py tests/test_runner_secret.py tests/test_orphans_canary.py tests/test_vm_names_strict.py
git diff --stat | tee evidence/p0c/diffstat.txt                          # P-0 + F-5-1 + P-0c against HEAD
git reset -q -- tests/test_pubkey_strict.py tests/test_runner_secret.py tests/test_orphans_canary.py tests/test_vm_names_strict.py
```
Expected (UNVERIFIED): all pass on both interpreters; `diffstat.txt` lists the 10 earlier files plus
`service/engine.py`, `service/models.py` (more lines), `service/runner.py` (more lines) and the two new tests.
For the P-0c patch alone, compare with `diffstat-p0c.txt`: 5 files, 193 insertions, 2 deletions.

## 4. Full suite
```bash
for v in 3.11 3.12; do
  .venv-$v/bin/python -m pytest tests/ -q -rs -rf -p no:cacheprovider --junitxml=evidence/ci/junit-p0c-py$v.xml \
     > evidence/tests/p0c-full-py$v.txt 2>&1
  tail -n 1 evidence/tests/p0c-full-py$v.txt
done
python3 - <<'PY'      # itemise skips from junit (pytest -rs printed none in the earlier run)
import xml.etree.ElementTree as ET
for v in ("3.11","3.12"):
    out=[f"SKIPPED {t.get('classname')}::{t.get('name')} | {(t.find('skipped').get('message') or '')[:160]}"
         for t in ET.parse(f"evidence/ci/junit-p0c-py{v}.xml").getroot().iter("testcase") if t.find("skipped") is not None]
    open(f"evidence/tests/p0c-skips-py{v}.txt","w").write("\n".join(out)+"\n")
PY
```
Expected (UNVERIFIED): no FAILED lines. The earlier run took about 15 minutes per interpreter. The earlier skips
(dashboard e2e, three `sshd` tests) are expected to be unchanged.

## 5. Optional read-only check against the real Xen Orchestra (counts only)
See `M-VM-NAMES-HARNESS.md` (the "Read-only check" block). Also ask the XO administrator who may create or rename
VMs: that decides the "user-controlled" column, which stays UNKNOWN until then.

## 6. Evidence paths to attach to the next gate review
`evidence/p0c/before.txt`, `after.txt`, `status.txt`, `diffstat.txt`; `evidence/tests/p0c-full-py3.11.txt`,
`p0c-full-py3.12.txt`, `p0c-skips-py3.11.txt`, `p0c-skips-py3.12.txt`; `evidence/ci/junit-p0c-py3.11.xml`,
`junit-p0c-py3.12.xml`; plus the earlier `evidence/` from the previous run.

## 7. Stop conditions
A canary test FAILED in the green run; the destroy canary test PASSED in the red run (red not reproduced); any other
test FAILED; any invalid VM name or payload text appearing in a log line. No waivers.
