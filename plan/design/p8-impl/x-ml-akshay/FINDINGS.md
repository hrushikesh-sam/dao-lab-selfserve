# X-ML — Stage 0 findings (read-only discovery)

- Date: 2026-11-24 (UTC day). Task: port ml-tvm (`9134632`, `c736d4b`, `f9ac36b`, `e8eb2bc` on
  `origin/akshay`) onto `rama` without the reverted snapshot attempt (`4ec115e`, `2490387`,
  `e75c2b5`, reverted in `9c8a089`), plus A-1, Q-ROLES and the gates.
- **Base: `origin/rama` = `7a2dbad`** ("Merge pull request #11 … p8-x-age"), not `5641830`.
  Merge-base with `origin/akshay` = `40ebd6d`.
- Worktree `~/x-ml`, branch `x-ml` (`--no-track`, from `origin/rama`), clean at creation.
- Venvs: `.venv` (ansible-core 2.21.6) and `.venv-220` (2.20.11). Both have Python 3.12.13 (uv),
  ansible-runner 2.4.3, pytest 9.2.0 and SQLite 3.53.1; age is v1.1.1. Their package sets are
  identical to `~/b104b`'s (`build/freeze-*.txt`; 51 packages, the same apart from name spelling).
- Akshay's decisions of 24 Nov are recorded in §6. Everything below `file:line` is at `7a2dbad`
  unless it says otherwise.

## 1. The four commits and their files

| Commit | Files |
|---|---|
| `9134632` ml-tvm use case | `inventory/host_vars/dpu-01.yml`, `playbooks/apps/ml-tvm/cleanup.yml` (new), `playbooks/apps/ml-tvm/deploy.yml` (new), `playbooks/deploy.yml`, `playbooks/files/daolab-app-exporter`, `playbooks/files/daolab-ml-smoke` (new), `playbooks/probe.yml`, `playbooks/roles/verify/tasks/run.yml`, `playbooks/verify_clean.yml`, `service/runner.py`, `setups/tb-01.yml`, `tests/test_catalog.py`, `usecases/ml-tvm.yml` (new) |
| `c736d4b` colour codes | `playbooks/files/daolab-ml-smoke` |
| `f9ac36b` POSIX holder test | `playbooks/apps/ml-tvm/cleanup.yml` |
| `e8eb2bc` docs | `COMPLETION_LOG.md`, `README.md`, `docs/dao-doc-verification.md`, plus the one-line `DAOLAB_DEPLOYABLE` script edit. That edit is **applied by Akshay by hand**; I don't name, read or touch that file |

## 2. Conflicts with rama

**Textual: none.** I applied the four patches in order to a scratch index made from `origin/rama`
(`git apply --cached --check`, then `-3`). All four gave rc=0 with no unmerged paths. A-1 (`35aa3367…`,
checksum verified) also passes `--check`.

**What rama changed since `40ebd6d` in the touched files:**

| File | rama commits | Effect on the port |
|---|---|---|
| `playbooks/deploy.yml` | `c8ed2fc` (F-5-1) | header comment only (`daolab_internal_secret_file`). The ml-tvm hunk (verify `when:`) is unaffected |
| `service/runner.py` | `c8ed2fc`, `4b1a70b`, `20049ad` | F-5-1 / P-0c / P-0d; `MockExporter.GAUGES` (`runner.py:178`) untouched |
| `README.md` | `cd68713` | line 93, the schema count. The ml-tvm hunks are elsewhere |
| `tests/test_playbooks.py` | `20049ad` (P-0d) | `vm_uuid()` at `:42`. The CAN_BECOME port goes between it and `clean_state()` (`:47`) |
| `tests/test_ansible_runner.py` | `c8ed2fc`, `20049ad` | `real_runner()` `:263` is unchanged, so A-1's hunk applies |
| `scripts/run/` (directory stat only) | none | Akshay's one-line edit applies to the line as it was |
| all the others (inventory, apps, exporter, probe, verify `run.yml`, `verify_clean.yml`, `tb-01.yml`, `test_catalog.py`, `COMPLETION_LOG.md`, docs, `usecases/`) | none | — |

**Semantic conflicts and dependencies** (resolved by Akshay, §6):
- **S1, A-1 needs code from the reverted commit.** A-1 imports `CAN_BECOME` from `tests.test_playbooks`.
  That block came from the reverted `2490387` and survived the revert on `akshay`; rama doesn't have it
  (`git grep CAN_BECOME origin/rama` → 0). On the laptop, A-1 alone would raise an ImportError.
- **S2, the known failure.** `test_real_verify_clean_and_reconcile_round_trip`
  (`tests/test_ansible_runner.py:270`) runs the real `verify_clean.yml`. ml-tvm's new task there
  has `become: true` and runs for **every** reservation, including the test's `vpp`. Where sudo asks for
  a password, it fails and the residue list comes back empty. Expect this as the red stage; A-1 plus S1
  makes it green.
- **S3, `runner.py` belongs to Akshay.** The task row didn't list it; the change touches GAUGES only.
- **S4, the docs mix in the reverted design.** `e8eb2bc`'s `COMPLETION_LOG.md` entry (2026-11-20) says
  B-110 decides the ML device via `snapshot_bdfs`, and that the home policy is `keep` until `DAOLAB_SNAPSHOTS`
  (B-108). That contradicts DDL v4.1 R-6 and `setups.snapshot_enabled`. `README.md` and
  `docs/dao-doc-verification.md` agree with rama. In the plan docs, ml-tvm appears only in the root
  `README.md:139` (planned), which this port changes, so there is no README vs plan conflict.
- **S5, the seeded catalog row.** `service/catalog.py:89` seeds `ml-tvm` ("ML model inference via TVM",
  no YAML). `daolab sync` (`catalog.py:495`) has to turn it into the YAML-backed row. Dashboard
  deployability is `api.py:431 _deployable`: `yaml_path is not None` and (no `DAOLAB_DEPLOYABLE`, or
  the name is listed) (`config.py:107`).

## 3. Gates on the ported code (static; proof in Stage 1)

- **P-0 (no user text reaches a template):**
  - **No new runner extravars.** `AnsibleRunner.extravars()` (`runner.py:507`) is unchanged.
  - The new template inputs are 10 inventory host_vars (`ml_*`, set by an admin). EXTRAVAR-MATRIX
    already rates those "NO".
  - The other inputs are rows 10, 11 and 17 (`daolab_roles`, `daolab_verify`, `daolab_app` from
    catalog YAML, under C-1/G-2), plus the facts `daolab_dpu_role` (`playbooks/tasks/plan_vms.yml:28`)
    and `_verify_on_dpu`, which are derived from the catalog.
  - `allow_destructive_changes` (settings bool) reaches only an `assert that:` through `| bool`.
  - So no user-controlled value reaches a new template. Stage 1 proves it by running the P-0 tests on
    the ported tree. If any hit shows up, STOP.
- **C-1/G-2:** `usecases/ml-tvm.yml` `verify.cmd` is `sudo -n /usr/local/bin/daolab-ml-smoke`, with no
  `{{ {% {#`. The guard (`catalog.py:23`, `:342`) and the repo scan
  (`tests/test_catalog_template_guard.py:72`) cover `usecases/*.yml`. Stage 1 shows the scan picks up
  `ml-tvm.yml`.
- **F-5-1:** no secret enters a new playbook. The ml-tvm files don't reference `daolab_internal_secret`.
  Stage 1 checks this with a grep.
- **Residue kinds:** the task reports `dpu_app:ml-tvm`. `dpu_app` is already in `RESIDUE_KINDS`
  (`runner.py:75`), so `parse_residue` (`:87`) accepts it. The reverted `dpu_home` kind isn't
  in these four commits and is not ported.
- **Extravar matrix:** no new rows (above). A static note is enough unless Stage 1 finds a user value
  in a template.
- **Flag-off parity:** these commits touch nothing snapshot-related, so `setups.snapshot_enabled` is not
  involved. For non-ml use cases, `verify_clean` gains one non-`step:` task that is read-only and adds
  nothing to a clean board's residue. Deploy and probe change only through the new `or` branch of the
  verify `when:`, which a VM use case doesn't take. Stage 1 proves the clean residue list is unchanged
  for a `vpp` reservation.

## 4. Hardcoded host paths (item 6) at `f9ac36b` / `c736d4b`

| File:line (akshay) | Literal |
|---|---|
| `apps/ml-tvm/cleanup.yml:33-34` | `/proc/[0-9]*/fd/*`, `/dev/vfio/$g` (the **kill** of ML-device holders) |
| `apps/ml-tvm/cleanup.yml:46` | `/var/run/dpdk/<prefix>`, `rm -rfv` |
| `apps/ml-tvm/cleanup.yml:47` | `find /dev/hugepages /mnt/huge … -delete` |
| `apps/ml-tvm/deploy.yml:44` | `/usr/bin/dpdk-test-mldev` (readability check) |
| `apps/ml-tvm/deploy.yml:158,177` | `/usr/local/bin/daolab-ml-smoke` (installed with become) |
| `apps/ml-tvm/deploy.yml:164,177` | `/etc/default/daolab-ml` (written with become) |
| `verify_clean.yml:80-81` | `/proc/[0-9]*/fd/*`, `/dev/vfio/$g` (read-only) |
| `verify_clean.yml:85` | `/var/run/dpdk/$p` |
| `verify_clean.yml:86` | `/dev/hugepages /mnt/huge` |
| `files/daolab-ml-smoke:33-36,58-59` | `/proc`, `/dev/vfio`, `/var/run/dpdk`, `/dev/hugepages /mnt/huge` (a DPU-side bash script; it already honours `DAOLAB_SYSFS`) |

Rama's existing convention is `playbooks/tasks/release_hugepages.yml:19`:
`(daolab_hugetlbfs | default('/dev/hugepages /mnt/huge')).split()`. The Lab already sets
`daolab_sysfs` / `daolab_hugetlbfs` (`tests/test_playbooks.py:102`).

**Risk:** in a root or CI run (become on), `cleanup.yml` could kill real processes holding
`/dev/vfio/<g>` on the test machine, and delete real `ml*` DPDK or hugepage files. That's why §6 item 6 matters.

## 5. Q-ROLES / one source of truth (item 3)

- `inventory/host_vars/dpu-01.yml:23` `bdf:` has `sdp_pf`, `cpt_pf` and `cpt_vfs`, but no `ml`.
- After the port, `0000:00:10.0` would also appear as a literal fallback in:
  - `verify_clean.yml:79` and `probe.yml` (`ml_pci | default('0000:00:10.0')`);
  - the exporter's `--ml-pci` argparse default;
  - `daolab-ml-smoke`'s `PCI=${ML_PCI:-0000:00:10.0}`.

## 6. Akshay's decisions (24 Nov), the plan for Stage 1

1. **CAN_BECOME:** port only `_can_become()` / `CAN_BECOME` and the `ansible_become=False` line in
   `Lab.run` from `akshay` into `tests/test_playbooks.py`. It's test harness only, with no snapshot code;
   say so in the PR notes. Then apply A-1 (`35aa3367`) to `tests/test_ansible_runner.py`.
2. **`service/runner.py`:** change `MockExporter.GAUGES` only.
3. **The ML address:** `bdf.ml: "0000:00:10.0"` is the one source; `ml_pci: "{{ bdf.ml }}"` keeps
   the playbook name. A test checks that they agree.
4. **The extravar matrix:** a static note (§3) plus proof that the existing G-2 repo scan and the P-0
   tests now cover `usecases/ml-tvm.yml`. No new canaries unless a user value reaches a template
   (then STOP).
5. **Host paths:** every hardcoded host path in the ml-tvm playbooks and its `verify_clean` task
   becomes a variable whose default is today's exact value. Reuse `daolab_hugetlbfs` (default
   `'/dev/hugepages /mnt/huge'`), and add `daolab_dpdk_run_dir` (default `/var/run/dpdk`). The tests
   point them at `tmp_path`, and a guard test fails on any literal `/dev/hugepages`, `/mnt/huge` or
   `/var/run/dpdk` outside a `default()`.
6. **Tests:**
   - ml-tvm deploy, cleanup and verify_clean through the fake lab, with path prefixes;
   - the catalog loads `ml-tvm.yml`;
   - `daolab sync` turns the seeded row into the YAML-backed row (`yaml_path` set, no duplicate),
     and it's deployable when listed in `DAOLAB_DEPLOYABLE`;
   - `dpu_app:ml-tvm` is reported and then cleared.
7. **`COMPLETION_LOG.md`:** port the 2026-11-20 entry as written, and add directly under it:
   "(24 Nov: superseded. Devices live in the snapshot profile daolab-profile/1, never
   setups.snapshot_bdfs, per DDL v4.1 R-6; snapshots are switched by setups.snapshot_enabled.)"
8. **The catalog item is proven in Stage 1**, inside `~/x-ml`, with no scratch copy.
9. **Baseline at `7a2dbad`:** expected 1488 passed, 4 skipped (age is installed, so `requires_age`
   runs). The actual counts are in §8.

## 7. File list (Stage 1)

| # | File | Origin |
|---|---|---|
| 1 | `inventory/host_vars/dpu-01.yml` | 9134632, plus `bdf.ml` and `ml_pci: "{{ bdf.ml }}"` (Q-ROLES) |
| 2 | `playbooks/apps/ml-tvm/deploy.yml` (new) | 9134632, plus path variables |
| 3 | `playbooks/apps/ml-tvm/cleanup.yml` (new) | 9134632 + f9ac36b, plus path variables |
| 4 | `playbooks/deploy.yml` | 9134632 |
| 5 | `playbooks/files/daolab-app-exporter` | 9134632 |
| 6 | `playbooks/files/daolab-ml-smoke` (new) | 9134632 + c736d4b (path overrides: Q-B) |
| 7 | `playbooks/probe.yml` | 9134632 |
| 8 | `playbooks/roles/verify/tasks/run.yml` | 9134632 |
| 9 | `playbooks/verify_clean.yml` | 9134632, plus path variables |
| 10 | `service/runner.py` | 9134632, `MockExporter.GAUGES` only (approved) |
| 11 | `setups/tb-01.yml` | 9134632 |
| 12 | `tests/test_catalog.py` | 9134632 |
| 13 | `usecases/ml-tvm.yml` (new) | 9134632 |
| 14 | `COMPLETION_LOG.md` | e8eb2bc, plus the superseded line |
| 15 | `README.md` | e8eb2bc |
| 16 | `docs/dao-doc-verification.md` | e8eb2bc |
| 17 | the `DAOLAB_DEPLOYABLE` script line | e8eb2bc, **applied by Akshay** |
| 18 | `tests/test_playbooks.py` | CAN_BECOME harness from 2490387 (approved) |
| 19 | `tests/test_ansible_runner.py` | A-1 `35aa3367` |
| 20 | `tests/test_ml_tvm.py` (new) | the new tests in §6 item 6, plus the path guard and the `bdf.ml` agreement test (name: Q-D) |

No change is planned to `group_vars/all.yml`: the defaults stay inline (`| default(...)`), as
`release_hugepages.yml` does it.

**Stage order:**
1. red: the tests from §6 on plain rama, then the port without A-1, with the S2 failure shown by its
   E-lines;
2. green;
3. the full suite on both venvs;
4. `tests/test_app_exporter.py` on Python 3.10 (`/usr/bin/python3.10`, 3.10.12, in a `.venv-310`
   with pytest only), because the exporter is DPU-side;
5. the canary check (rule 8) isn't triggered: the port has no S or nonce path. I'll state that rather
   than add a test.

## 8. Baseline at `7a2dbad`

| Venv | ansible-playbook actually used | Result | Time |
|---|---|---|---|
| `.venv` | `~/x-ml/.venv/bin/ansible-playbook` `[core 2.21.6]` | **1488 passed, 4 skipped, 0 failed**, exit 0 | 15:43 |
| `.venv-220` | `~/x-ml/.venv-220/bin/ansible-playbook` `[core 2.20.11]` | **1488 passed, 4 skipped, 0 failed**, exit 0 | 15:43 |

- Run details: HEAD `7a2dbad84f99…`, tree `8639e1b40703b7277f34810964fdaeb1ee063976`, 0 status entries.
  Python 3.12.13, SQLite 3.53.1, age v1.1.1.
- Command: `pytest tests/ -q -rsf -p no:cacheprovider`. Logs: `build/baseline-221.txt`, `build/baseline-220.txt`.
- Skips, the same on both venvs:
  - `test_dashboard_e2e.py:17` (no playwright);
  - `test_daolab_keys.py:146`, `:155` and `test_playbooks.py:1134` (sshd not installed).
- This matches Akshay's expected 1488/4. The `requires_age` test ran.

**The first attempt was invalid; it was stopped and its log kept as
`build/INVALID-baseline-221-main-venv-ansible-2.21.5.txt`.**
- This Claude Code session's PATH begins with `~/dao-lab-selfserve/.venv/bin`, which has ansible-core
  **2.21.5** (installed 2026-10-24). It's not set in `~/.bashrc` or `~/.profile`; it comes from the
  environment the session was started in.
- `tests/test_playbooks.py:25` picks `ANSIBLE = DAOLAB_TEST_ANSIBLE_PLAYBOOK or
  shutil.which("ansible-playbook") or <the venv's own>`, so the PATH entry wins over the venv's own
  `ansible-playbook`. As a result, the playbook tests ran 2.21.5 whichever venv's python ran pytest.
  The process tree showed it.
- Fix (evidence tooling only, no repo change): `run-stage.sh` now puts `<venv>/bin` first on PATH,
  sets `DAOLAB_TEST_ANSIBLE_PLAYBOOK` to the venv's `ansible-playbook`, and records both in the
  header (`# ansible-playbook:` and `--version`). Checked with `build/pathcheck.venv*.txt`:
  `[core 2.21.6]` and `[core 2.20.11]`.
- **Open for Akshay and Rama:** `~/b104b`'s `run-stage.sh` (and possibly earlier runners) didn't pin
  PATH, and their headers record only the python venv's ansible-core. If those sessions had the same
  PATH, their "2.20.11" playbook-test runs may have used 2.21.5. The logs can't settle this either way.
  CI (fresh runners) is not affected.

## 9. Questions, answered by Akshay (24 Nov)

- **Q-A yes:** `daolab_procfs`, `daolab_vfio_dir`, `ml_smoke_bin`, `ml_env_file` and `ml_test_mldev`,
  each defaulting to today's value. The tests point them at fake trees, so no real process is ever killed.
- **Q-B yes:** `daolab-ml-smoke` gets environment overrides like `DAOLAB_SYSFS`, with the same defaults.
- **Q-C yes:** the playbooks use `default(bdf.ml)` and pass the BDF explicitly to the exporter and the
  smoke script, so their literal fallback isn't used in a normal run. The agreement test checks that
  all four equal `bdf.ml`.
- **Q-D yes:** `tests/test_ml_tvm.py`.

The questions as asked:

- **Q-A, the other host paths:** §6 item 5 names `daolab_hugetlbfs` and `daolab_dpdk_run_dir`. Its
  "every hardcoded host path" also covers `/proc` and `/dev/vfio` (in the cleanup **kill** and the
  verify_clean holder scan), `/usr/local/bin/daolab-ml-smoke`, `/etc/default/daolab-ml` and
  `/usr/bin/dpdk-test-mldev`. Proposed names, each defaulting to today's value: `daolab_procfs`,
  `daolab_vfio_dir`, `ml_smoke_bin`, `ml_env_file`, `ml_test_mldev`. OK?
- **Q-B, `daolab-ml-smoke`:** it's a script, not a playbook, but it deletes `/var/run/dpdk/<prefix>`
  and hugepage files. Give it the same overrides as environment variables (`DAOLAB_DPDK_RUN_DIR`,
  `DAOLAB_HUGETLBFS`, `DAOLAB_PROCFS`, `DAOLAB_VFIO_DIR`), defaulting to today's values, the way it already reads `DAOLAB_SYSFS`?
- **Q-C, the literal `0000:00:10.0` fallbacks** (§5): change them to `default(bdf.ml)` in the playbooks?
  The exporter and smoke-script defaults would stay as they are, since the playbooks always pass the
  address. Should the agreement test cover all four?
- **Q-D:** name the new test file `tests/test_ml_tvm.py`?

## 10. Follow-ups

- verify reads the smoke script's built-in defaults, not the inventory; revisit in B-111 (roles by
  name). (`usecases/ml-tvm.yml` verify.cmd is `sudo -n /usr/local/bin/daolab-ml-smoke`, which does
  not source `/etc/default/daolab-ml`; `tests/test_ml_tvm.py` pins the script's `ML_PCI` default
  to dpu-01's `bdf.ml`. Akshay, 25 Nov: keep as is, do not change verify.cmd.)
- `~/.ansible/tmp`: 24 empty `ansible-tmp-*` module dirs were left by the two red/green runs of
  `test_every_ml_cleanup_delete_target_is_under_the_test_dir` before `ansible_remote_tmp` was
  pointed at the test dir (the rm stand-in refused ansible's own `rm -f -r` of them). They are
  outside the allowed areas, so they were counted, not deleted. Akshay may remove them by hand.

## 11. Decisions taken without asking (25 Nov, Stage 1, under Akshay's "pick the safest option")

1. **2(b) wording.** Akshay's 25 Nov order of work says verify_clean *skips* the ML-device check
   when `ml_pci` resolves to '' and the ml-tvm *deploy* fails closed. My earlier test
   (`…never_clean_without_an_ml_address`, red in red-2) expected verify_clean itself to fail for an
   ml-tvm reservation. Replaced (before any code made it pass) by
   `test_without_an_ml_address_only_the_holder_check_is_skipped` (state, runtime and hugepages
   leftovers are still reported without an address) plus `test_ml_deploy_fails_closed_…`.
2. **The skip's "step reason" is a named debug task, not a `step:` task.** verify_clean runs
   without an ML address in `tests/test_ansible_runner.py`'s round trip, which pins the step list
   to `check_vms, check_sessions, check_ips`. A `step:` task would change that expectation.
   The reason is in the job log: task "ML device holder check skipped (no ML device address)",
   message "ML device check skipped: no ML device address (ml_pci / bdf.ml) …".
3. **Path variable names** are the Q-A names Akshay approved on 24 Nov (`daolab_procfs`,
   `daolab_vfio_dir`, `daolab_dpdk_run_dir`, `ml_smoke_bin`, `ml_env_file`, `ml_test_mldev`), plus the
   existing `daolab_sysfs` / `daolab_hugetlbfs`. Host-wide paths keep the `daolab_` prefix used by
   `release_hugepages.yml`; ml-tvm's own files use `ml_*`. Not renamed to all-`ml_*`.
4. **A real bug found in `9134632`, fixed:** verify_clean's `ls -d $r/$p $r/$p-* … && left=…` fails
   when either pattern is missing, so a leftover `ml` (or `ml-*`) DPDK dir alone was never
   reported. Now `[ -n "$(ls -d … 2>/dev/null)" ]`. Safer: it reports the leftover.
5. **Test assertions corrected to ansible's real output, never loosened:** (a) cleanup's
   "stopping ML device holders" is shell stdout, which ansible doesn't print for an ok task;
   the test now checks `step: stop_app` reports `changed` (the holder's death is still checked by
   `wait`). (b) After a deploy, verify_clean also reports the exporter unit
   (`dpu_app:daolab-app-exporter.service`), which is correct during a reservation; the round-trip
   test now expects both items, and `[]` after cleanup as before.
6. **The ml tests set `ansible_remote_tmp` to the test dir,** so even ansible's own module temp
   files (and their `rm`) stay under tmp_path (see §10).
7. **The smoke test still finds `dpdk-test-mldev` on PATH** (not through `ml_test_mldev`): changing
   which binary the board runs is outside "defaults = today's value". `ml_test_mldev` is used only
   by deploy's readiness check, as Q-A said.
8. **Deploy's fail-closed check** is the first task of the first ML play, before anything is read
   or written. If `ml_pci` is unset but `bdf.ml` is set, the check passes and the later `ml_pci`
   references fail as undefined (still before any write). The inventory always sets both.
9. **The unused red-1 option `-o addopts=""`** was dropped and red-1 re-run (logs replaced).
10. **No unit test for A-1 was added:** this machine needs a sudo password, so the real test is
    red for the right reason (2 × "sudo: a password is required" per venv) and green after A-1.

## 12. Decisions taken without asking (27 Nov, final verification session)

1. **Step 0 found no half-applied edit.** Checked: the `ls -d` fix in `verify_clean.yml`, the
   deploy comment without the word "secret" (grep over the ml-tvm files and `verify_clean.yml`
   returns 0), `ansible_remote_tmp` in `tests/test_ml_tvm.py`, the two corrected assertions
   (`step: stop_app` changed; `during` includes the exporter unit), and A-1's `real_runner` block.
   All changed `.py` files compile, all changed YAML parses, and `daolab-ml-smoke` passes `bash -n`.
   Nothing was edited.
2. **The interrupted full run of 25 Nov was renamed** `logs/full-221.txt` →
   `logs/INTERRUPTED-full-221.txt` (KeyboardInterrupt at 815 passed / 3 skipped). Same convention
   as `build/INVALID-…`. The content was not changed. It doesn't count as this session's full run.
3. **Step 1 (targeted runs) was stopped, on Akshay's instruction (27 Nov).** Step 2 covers the
   same tests: A runs the full suite on `.venv`, and B runs every ansible-touching file on `.venv-220`.
   The partial logs are kept as `logs/STOPPED-targeted-221.txt` / `-220.txt`. They contain no
   result line. The last full targeted results stay the 25 Nov `green2-*.txt` (555 passed, 1 skipped
   on each venv).
4. **The git index-lock clash.** Both targeted runs were started at the same moment, so both
   `run.sh` headers called `git write-tree` at once. The second one hit the index lock, which left an
   empty `write-tree:` field in `STOPPED-targeted-220.txt`. The tests themselves were not affected.
   Afterwards there was no git process and no leftover `index.lock`, so nothing was deleted. The
   status count is still 19. Fix for Step 2: `git write-tree` was computed once beforehand
   (`1c122a2384c206f0ce32371b3c9afd2d285770b0`), and B was started only after A's header was
   complete (no git process running). `run.sh` was not changed. Both headers were then checked
   against that value.
5. **Run B's file set** is the 18 files from `grep -l 'ansible' tests/*.py` plus
   `tests/test_catalog.py`. The task names that file explicitly, but the grep doesn't match it.
   `tests/test_app_exporter.py` doesn't touch ansible, so it runs only in A.
6. **Final log names:** `logs/final-full-221.txt` (A) and `logs/final-ans-220.txt` (B).
7. **`git add -A -- . ':!.venv' ':!.venv-220'` exits 1.** The exclude pathspecs name paths that are
   already gitignored, so git prints its "paths are ignored" advice and returns 1. The staging itself
   was complete: 20 files (5 A, 15 M), no `.venv*` path, nothing left unstaged. That exit code stopped
   my `&&` chain before the patch was written, so I re-ran the remaining Step 3 commands on their own.
8. **The check worktree was removed with `git worktree remove --force /tmp/xml-check`.** Its index
   held the applied patch, and git won't remove a worktree with changes without `--force`. No `rm`
   was used. `git worktree list` no longer shows it.
9. **The targeted line in COMMIT_MSG is the 25 Nov `green2-*` result** (decision 3). The one path
   that starts with `~` in the message, `~/.ansible/tmp`, is kept because the task's Follow-ups text
   asks for it.

## 13. Final results (27 Nov)

- A, `.venv` `[core 2.21.6]`, full suite: **1630 passed, 4 skipped, 0 failed**, exit 0, 18:25
  (`logs/final-full-221.txt`). Baseline 1578 + 52 new tests.
- B, `.venv-220` `[core 2.20.11]`, 19 ansible files: **817 passed, 1 skipped, 0 failed**, exit 0,
  16:58 (`logs/final-ans-220.txt`).
- Both headers: HEAD `8bfcf4b`, write-tree `1c122a23…` (before staging), 19 status entries.
- Staged write-tree `8452ef97c395cad4dbe64797414dfce277ff397b`. `git diff --cached --check`: no output.
  TREE MATCH on a fresh `8bfcf4b` worktree: **yes**.
- `x-ml.patch` sha256 `03a4388bba630b18323af34649a1f0de81debb149f9e9c5ea395231883004b84`, 20 files,
  1338 insertions and 13 deletions.
