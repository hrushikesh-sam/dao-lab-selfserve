# Operator-only runbook: P-0 (expanded) + F-5-1 + fact collection

**Status of every claim here:** nothing in this file has been run by the author as part of this deliverable.
Expected outcomes are expectations, **UNVERIFIED** until you run them. Commands are bash (Linux, WSL or Git Bash).
No commit, push, merge or PR is part of this runbook; P-0 may be committed only after the gate in §7.

Files (all in the delivery folder with this file): `P-0-final.patch` (on HEAD `40ebd6d`), `F-5-1.patch` (applies on top of
P-0), `EXTRAVAR-MATRIX.md`, `audit_stored_inputs.py`, `diffstat-final-corrected.txt`, `M-VM-NAMES-HARNESS.md` and
`test_orphans_canary_DRAFT.py`. Both patches carry standard git metadata (`diff --git`, `index`, `new file mode`);
whether they apply on your tree is UNVERIFIED until §1.

## 1. Apply the patches (local working tree only)

```bash
cd /path/to/dao-lab-selfserve
git rev-parse HEAD                      # expect 40ebd6dcc8877632e0270155cf5dfa797d1012dc
git status --porcelain                  # expect empty
git switch -c wip/p0-extension          # local branch, optional; nothing is committed or pushed
P=/path/to/P-0-final.patch; Q=/path/to/F-5-1.patch
git apply --check "$P"                  # expect no output
```
If Windows autocrlf rewrote line endings and the check fails, retry with `git apply --check --ignore-whitespace`.

## 2. Two virtual environments (the floor is not raised yet: B-103 is blocked)

```bash
for v in 3.11 3.12; do python$v -m venv .venv-$v && .venv-$v/bin/pip install -q -e ".[ansible]"; done
.venv-3.11/bin/pip list --format=freeze | grep -i -E '^(ansible-core|ansible-runner|pytest|fastapi)=='
.venv-3.12/bin/pip list --format=freeze | grep -i -E '^(ansible-core|ansible-runner|pytest|fastapi)=='
mkdir -p evidence/p0 evidence/f51 evidence/tests evidence/ci
```

## 3. Red / green canary tests

The canary tests run real playbook code and look for a file the payload would create; the payload only `touch`es a
file inside pytest's temporary directory.

### 3.1 P-0 RED (tests only, fix absent)
```bash
git apply --include='tests/*' "$P"      # adds tests/test_pubkey_strict.py, edits tests/test_deploy_access.py
for v in 3.11 3.12; do echo "## $v"; .venv-$v/bin/python -m pytest tests/test_pubkey_strict.py tests/test_deploy_access.py \
   -q -p no:cacheprovider --tb=line -rf; done 2>&1 | tee evidence/p0/before.txt | tail -n 30
```
Expected (UNVERIFIED): many failures per interpreter, including `test_canary_diy_templates_never_template_in_the_real_plan_vms`
and `test_canary_ssh_key_comment_never_templates_in_the_real_dpu_access_role` ("evaluated on the controller"), the API tests
(`201 == 422`), and the stored-row tests. If the two canary tests PASS here, stop and report: the tests are not
reproducing the issue on your tree.
```bash
git checkout -- tests/test_deploy_access.py && rm tests/test_pubkey_strict.py     # back to a clean tree
```

### 3.2 P-0 GREEN
```bash
git apply "$P"
for v in 3.11 3.12; do echo "## $v"; .venv-$v/bin/python -m pytest tests/test_pubkey_strict.py tests/test_deploy_access.py \
   tests/test_access.py tests/test_cli_users.py tests/test_api.py -q -p no:cacheprovider -rf; done 2>&1 | tee evidence/p0/after.txt | tail -n 12
git status --porcelain | tee evidence/p0/status.txt         # archive this: it lists the untracked NEW test file
git add -N tests/test_pubkey_strict.py                       # intent-to-add: makes the new file visible to diff (not a commit)
git diff --stat | tee evidence/p0/diffstat.txt
git reset -q -- tests/test_pubkey_strict.py                  # undo the intent-to-add
```
`git diff --stat` on its own **omits untracked new files**: without the `git add -N` line it would list only the three
service files and `tests/test_deploy_access.py`. (For a one-off stat of a single new file without touching the index:
`git diff --stat --no-index /dev/null tests/test_pubkey_strict.py`; it exits 1 when files differ, which is normal.)

Expected (UNVERIFIED): all tests pass on both interpreters; `status.txt` shows `service/api.py`, `service/cli.py`,
`service/models.py`, `tests/test_deploy_access.py` as modified and `tests/test_pubkey_strict.py` as untracked
(`??`); `diffstat.txt` (after `add -N`) lists those five files; `playbooks/` is untouched by P-0.

### 3.3 F-5-1 RED then GREEN
```bash
git apply --include='tests/*' "$Q"      # tests/test_ansible_runner.py edit + tests/test_runner_secret.py
for v in 3.11 3.12; do .venv-$v/bin/python -m pytest tests/test_runner_secret.py tests/test_ansible_runner.py \
   -q -p no:cacheprovider --tb=short -rf; done 2>&1 | tee evidence/f51/before.txt | tail -n 30
```
Expected (UNVERIFIED): failures, among them `test_the_real_runner_persists_no_trace_of_the_secret` listing the persisted
files that contain the canary (typically under `env/` and `artifacts/`).
```bash
git checkout -- tests/test_ansible_runner.py && rm tests/test_runner_secret.py
git apply "$Q"
for v in 3.11 3.12; do .venv-$v/bin/python -m pytest tests/test_runner_secret.py tests/test_ansible_runner.py \
   -q -p no:cacheprovider -rf; done 2>&1 | tee evidence/f51/after.txt | tail -n 12
git status --porcelain | tee evidence/f51/status.txt         # two untracked new tests: P-0's and F-5-1's
git add -N tests/test_pubkey_strict.py tests/test_runner_secret.py
git diff --stat | tee evidence/f51/diffstat.txt              # combined P-0 + F-5-1 against HEAD, new files included
git reset -q -- tests/test_pubkey_strict.py tests/test_runner_secret.py
```
Expected (UNVERIFIED): all pass; `diffstat.txt` lists 10 files (8 tracked + the 2 new tests; compare with
`diffstat-final-corrected.txt` in the delivery folder). If the real-runner test fails with "the secret did not reach the
play", the lazy `group_vars/all.yml` lookup is not being applied in your ansible-core: report it, do not deploy F-5-1.

### 3.4 Catalog check C-1 (names only, no content printed)
```bash
grep -rnE '\{\{|\{%|\{#' usecases setups | cut -d: -f1,2 | tee evidence/p0/catalog-delimiters.txt   # expect empty
```

### 3.5 Row 25 (`daolab_vm_names`): M-vm-names harness (DRAFT)
Follow `M-VM-NAMES-HARNESS.md` (copies `test_orphans_canary_DRAFT.py` into `tests/` temporarily). A FAILED test is a stop
condition; a passing run proves nothing about the real lab. Archive `evidence/m-vm-names/run.txt`.

## 4. Full suite and itemised skips
```bash
for v in 3.11 3.12; do
  .venv-$v/bin/python -m pytest tests/ -q -rs -rf -p no:cacheprovider --junitxml=evidence/ci/junit-py$v.xml \
     > evidence/tests/full-py$v.txt 2>&1
  tail -n 1 evidence/tests/full-py$v.txt; grep '^SKIPPED' evidence/tests/full-py$v.txt | tee evidence/tests/skips-py$v.txt
done
```
`tests/test_playbooks.py` is slow (this took the author's environment on the order of 15 minutes per run in an earlier
session; UNVERIFIED for yours). Expected: no FAILED lines; every skip has a reason line in `skips-py*.txt`. The skip
reasons are conditional on installed tools (Playwright/chromium, sshd, curl); the earlier count of 4 is UNVERIFIED for
your machine. To run the dashboard e2e instead of skipping it: `pip install playwright && playwright install chromium`.

## 5. Stored-data audit and F-5-1 production steps

### 5.0 Put the audit script in place (before 5.1)
```bash
mkdir -p evidence/p0
cp /path/to/delivery-folder/audit_stored_inputs.py evidence/p0/audit_stored_inputs.py
sha256sum evidence/p0/audit_stored_inputs.py | tee evidence/p0/audit_stored_inputs.sha256   # compare with the delivered copy
```
The script is not part of either patch and must not be committed into `service/`; it stays in the evidence folder.

### 5.1 Audit (read-only, on a COPY of the newest backup)
```bash
BK="$(ls -1t "$DAOLAB_BACKUP_DIR"/daolab-*.db | head -n 1)"       # backup-db.sh names: daolab-<UTC>.db
cp -- "$BK" /tmp/audit-copy.db && chmod 600 /tmp/audit-copy.db
.venv-3.12/bin/python -I evidence/p0/audit_stored_inputs.py /tmp/audit-copy.db | tee evidence/p0/db-audit-ids.txt
echo "exit=${PIPESTATUS[0]}"
```
`-I` ignores PYTHONPATH: run it in the venv where the patched repo is installed (`pip install -e .`). It prints table,
column and row **ids only**, never values. Exit 0 = clean; **exit 1 = poisoned rows: STOP**; exit 2 = audit failed.

### 5.2 If exit 1
1. Stop the engine so no queued deploy runs (service name: UNKNOWN, no unit file in the repo; `systemctl list-units 'daolab*'`).
2. For each listed reservation id: cancel it (`DELETE /reservations/{id}`, as its owner or an admin) and, if its setup is
   dirty, `POST /admin/setups/{name}/force-clean` (admin). Do not run `deploy` or `redeploy` on them.
3. Inspect the controller for compromise (read-only; keep this output local, it contains controller paths):
```bash
sqlite3 -readonly /tmp/audit-copy.db '.schema audit_events'
find / -xdev -user "$SVC_USER" -newermt "$FIRST_SUSPECT_DATE" -type f ! -path '/proc/*' \
     ! -path "$DAOLAB_ARTIFACTS_DIR/*" -printf '%TY-%Tm-%Td %TH:%TM %p\n' 2>/dev/null | sort | head -200
grep -rlE "lookup\(|\{\{" "$DAOLAB_ARTIFACTS_DIR"/job-*/ 2>/dev/null | head -50      # job dirs that rendered a payload
```
4. Treat evidence of execution as a controller compromise: rotate user tokens (`daolab add-user --rotate`),
   `DAOLAB_INTERNAL_SECRET`, the Xen Orchestra token and the become-password; review the audit trail.

### 5.3 F-5-1 deployment (after §3.3 is green)
1. Give the engine a private runtime directory: either set `DAOLAB_RUNTIME_DIR` to a `0700` directory on tmpfs, or run the
   engine under systemd with `RuntimeDirectory=daolab` (systemd then exports `RUNTIME_DIRECTORY`). Without either, the
   secret file goes to the system temp dir (mode 0600, but not a private tmpfs).
2. Restart api and engine; run one deploy on a **non-production** setup.
3. Prove nothing persisted, without putting the secret on a command line (the secret comes in through a file descriptor):
```bash
grep -rlF -f <(grep '^DAOLAB_INTERNAL_SECRET=' /opt/daolab/.env | cut -d= -f2-) "$DAOLAB_ARTIFACTS_DIR"/job-<new job id>/
echo "hits-exit=$?"        # expect exit 1 (no file matches) for the NEW job dir
ls "${DAOLAB_RUNTIME_DIR:-${RUNTIME_DIRECTORY:-/tmp}}" | grep -c '^daolab-secret-'        # expect 0 after the run
```
4. **Old job directories already contain the secret** (ansible-runner wrote it to `env/extravars` and the command
   records). After F-5-1 is deployed: rotate `DAOLAB_INTERNAL_SECRET` (edit `.env` with an editor or a script that keeps
   the value off argv, then restart api and engine), then delete or scrub job directories older than the rotation
   (same `grep -rlF -f <(old secret)` listing to find them first). Your call; not done by the patches.

## 6. Fact collection (operator-only; **numbers and versions only**, never file names, key comments, tokens, home paths or snapshot contents)

DPU, per setup, as `lab` (F-D1..F-D8):
```bash
tar --version | head -1; sudo -V | head -1; sudo -n -l | head -3; python3 -c 'import sys; print(sys.version)'
uname -srvm; . /etc/os-release; echo "$PRETTY_NAME"
findmnt -rn -R /home/lab | wc -l; grep -c ' /home/lab' /proc/self/mountinfo; df -hT /home/lab; stat -c '%U:%G %a' /home/lab
tar --help | grep -c -E 'xattrs|acls|one-file-system'
ls -d /etc/sudoers.d /etc/cron.d /etc/systemd/system /usr/local/bin /etc/profile.d /etc/pam.d 2>&1
```
Home size distribution (numbers only), at the end of >=20 reservations or on every existing home:
```bash
sudo du -xsb /home/lab | cut -f1
sudo find /home/lab -xdev -type f -printf '%s\n' | sort -n | awk '{a[NR]=$1} END{print NR, a[int(NR*0.5)], a[int(NR*0.95)], a[NR]}'
sudo find /home/lab -xdev ! -type f ! -type d ! -type l | wc -l
sudo find /home/lab -xdev -type f -links +1 | wc -l
sudo find /home/lab -xdev ! -user lab | wc -l
sudo find /home/lab -xdev -printf '%P\n' | awk '{ if (length($0)>m) m=length($0)} END{print m}'
```
dpdk-devbind before/after a normal app deploy+cleanup (maintenance window, admin present):
```bash
command -v dpdk-devbind.py dpdk-devbind
dpdk-devbind.py --status > before.txt        # run the normal deploy + cleanup, then:
dpdk-devbind.py --status > after.txt
for d in /sys/bus/pci/devices/*; do printf '%s %s %s\n' "${d##*/}" "$(basename "$(readlink "$d/driver" 2>/dev/null)" 2>/dev/null)" "$(cat "$d/sriov_numvfs" 2>/dev/null)"; done
grep -H . /sys/kernel/mm/hugepages/hugepages-*/nr_hugepages /sys/kernel/mm/hugepages/hugepages-*/free_hugepages; cat /proc/cmdline
```
Controller (F-C1..F-C7):
```bash
id; systemctl list-units --type=service 'daolab*' --no-legend; systemctl show -p User -p Group -p ExecStart <unit>   # per unit
. /etc/os-release; echo "$PRETTY_NAME"; uname -r
<venv>/bin/python -VV; <venv>/bin/pip list --format=freeze | grep -i -E '^(ansible|fastapi|uvicorn|starlette|pydantic)'; <venv>/bin/ansible --version | head -3
findmnt -no FSTYPE,OPTIONS -T /run; df -h /run /dev/shm; stat -c '%U:%G %a %n' <ticket_dir_or_runtime_dir>
swapon --show --noheadings; cat /proc/swaps; sysctl vm.swappiness; lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS; dmsetup ls --target crypt
df -hT <snapshot_root>; findmnt -T <snapshot_root> -no FSTYPE,OPTIONS; quota -vs        # or: xfs_quota -x -c 'report -h'
age --version; sha256sum "$(command -v age)" "$(command -v age-keygen)"
```
DB and backup scope:
```bash
grep -E '^DAOLAB_(DB_PATH|BACKUP_DIR|BACKUP_KEEP_DAYS|SLOT_MIN|CLEANUP_MIN|JOB_TIMEOUT_SEC|TOKEN_TTL_DAYS)=' /opt/daolab/.env
ls -l --time-style=long-iso "$DAOLAB_BACKUP_DIR" | tail -n 5
sqlite3 -readonly /tmp/audit-copy.db 'PRAGMA user_version; SELECT count(*) FROM users; SELECT count(*) FROM setups; .schema reservations'
```
Capture-timing plan (no user data): `tar | age` of a **synthetic** 512 MiB tree in a scratch directory (not a home) across
the real DPU -> controller link; record p50 and p95 against `DAOLAB_CLEANUP_MIN` (default 10) and
`DAOLAB_JOB_TIMEOUT_SEC` (default 1800).

## 7. Gate before P-0 may be committed/deployed and B-103 started
All of: audit exit 0 on a real backup copy; patch review done; remaining matrix rows either run clean or accepted by you
(`EXTRAVAR-MATRIX.md`, notably row 25: §3.5); F-5-1 green and the production grep (§5.3) clean; §3.2/§3.3/§4 outputs archived.
