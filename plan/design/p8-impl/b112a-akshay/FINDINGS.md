# B-112a Stage 0 findings: the tripwire checker and baseline format

- Date: 2026-11-27 (UTC day)
- Worktree `~/b112a`, branch `b112a`, HEAD `65e47fc` = `origin/rama` `65e47fc` (checked after `git fetch origin`). Working tree clean.
- Stage 0 was read-only. Nothing in the repo was changed. This file is the only thing written.
- Not read: `.env`, `var/**`, `~/.config/daolab/**`. Nothing was run against the DPU, and nothing used sudo.

## 1. Summary

**What B-112a builds.** A small checker script that runs on the board after cleanup. It looks at 11 areas outside `/home/lab`: users, sudo, ssh, scheduled jobs, boot units, setuid and local binaries, kernel modules, mounts, processes, temp files and `/etc/daolab`. It compares what it finds with a list of fingerprints taken from a known-good state (the baseline). It prints only "area: N differences". The controller side gets three things: a baseline loader with a sha256 of the file, an output parser, and a baseline builder. Nothing calls any of this yet; B-112b wires it in.

**Does the output fit the existing residue code?** Yes. `parse_residue` splits on the first `:`, so `tripwire:sudo:2` becomes kind `tripwire` and ident `sudo:2`, once B-112b adds `tripwire` to `RESIDUE_KINDS`. Error lines and `tripwire:ok` must never become residue items: `parse_output` turns them into `[]` or an exception.

**The big noise risks, from the repo's own playbooks:**
1. Every deploy rewrites `/etc/daolab/otelcol.yaml` with the reservation id and user name, and cleanup leaves it behind. That gives `daolab` a count of 2 after every booking that has a collector.
2. Every deploy rewrites the `daolab-app-exporter.service` unit with arguments that depend on the app, and cleanup only disables it. That gives `boot` a count of 2 whenever the last app differs from the one in the baseline.
3. SmartNIC leaves `/tmp/dao-smart-nic-ansible.log` and `/tmp/pci-after.txt` behind (`tmp`).
4. ml-tvm leaves `/usr/local/bin/daolab-ml-smoke` behind (`bin`). It stays stable after the first ml-tvm deploy.
5. Kernel modules, mounts and processes depend on boot history and on how the checker is run.

**Spec conflicts (§3).** I found 10. None blocks the standalone checker, but several change exact test counts, so §10 lists the questions with my proposed default for each.

---

## 2. Facts at the base (`65e47fc`)

### 2.1 Residue contract today
| Fact | Where |
|---|---|
| `RESIDUE_KINDS = ("vm", "vf", "dpu_session", "dpu_app", "dpu_hugepages", "ip")`, no `tripwire` yet | `service/runner.py:75` |
| `CHECK_FOR_KIND` maps a kind to the mock's verify step name (B-112b needs an entry) | `service/runner.py:76-78` |
| `residue_item()` refuses unknown kinds | `service/runner.py:81-84` |
| `parse_residue()`: `partition(":")`, so an unknown kind gives `("other", item)`. Today `tripwire:sudo:2` → `("other", "tripwire:sudo:2")` | `service/runner.py:87-90` |
| A real-run verify with no residue but a failed run is "not clean either" (the model for `TripwireError` in B-112b) | `service/runner.py:644-650` |
| Residue items are joined into the job error string, `residue after cleanup: <items>`, so items must be safe text. `tripwire:<class>:<count>` is. | `service/engine.py:307-311` |
| Audit rows store `"residue": list(verified.residue)` | `service/engine.py:359`, `:377` |
| Quarantine and free-again paths | `service/engine.py:314-321`, `:324-328` |
| Cleanup re-runs `verify_clean` after each reconcile (up to `reconcile_attempts`), so the tripwire would run 1+N times per cleanup and its run time counts against the cleanup window | `service/engine.py:331-345` |
| Approvals already know `tripwire_accept` / `tripwire_rebaseline`; target regex `^[a-z0-9._:-]{1,128}$` accepts a hw_class | `service/approvals.py:8-10` |
| hw_class shape is kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$` (`test` matches) | `service/catalog.py:15`, `:284-286` |
| The runner passes `hw_class` to playbooks | `service/runner.py:512` |
| No `DpuTransport` at the base (B-110 not landed) | `grep -ri dputransport service/` gives 0 hits |

### 2.2 Spec sources read
- Design v1.1 §3 (`origin/akshay-p8-gate:plan/design/p8-gate-review/p8-snapshot-design-v1.1.md:49`), §5 rule 5 (`:77`), §6 save step 8 (`:95`), failure (`:101`). In this tree only `plan/design/p8-gate-review/snapshot-design.md:27` (the B-112 row of the task table) exists.
- Task list: B-112 row (`p8-task-breakdown.md` §B-112), §5.7 residue kinds, §6 Q-BASELINE, Q-PLANTS (all accepted by Rama 24 Nov).
- Prep brief `p8-akshay-tasks-prep.md` §4 (class table, "the user was root" trap).
- DDL `plan/design/p8-impl/B-104-DDL.md:26` (R-3 synthetic only), `:597-608` (residue kinds, quarantine), `:479-483` (approval effects, audit contents).
- Errata E-6 (`plan/design/p8-gate-review/p8-design-errata-v1.2.md:24`): root-owned files in `/home/lab` are user data, not a tripwire class.

### 2.3 Test environment facts
- `tests/__init__.py` exists, so `tests/test_tripwire.py` imports as `tests.test_tripwire`. `tests/dpu/` does not exist yet. With no `__init__.py` there, `tests/dpu/test_tripwire.py` imports as the top-level module `test_tripwire`. The names differ, so there is no "import file mismatch" in the full suite. This also holds if B-109a adds `tests/dpu/__init__.py`.
- `tests/conftest.py` imports `dotenv` and `service.models` at module level (`tests/conftest.py:20-29`). That is why the 3.10 run needs `--confcutdir=tests/dpu`. In the full suite (CI) that conftest still applies to `tests/dpu`, which is harmless.
- `pyproject.toml` has no `[tool.pytest]` section, so no `addopts` or `filterwarnings=error` to plan around. `make test` = `python -m pytest tests/ -q` (`Makefile:9-10`).
- CI runs Python 3.12 and 3.13 only (`.github/workflows/ci.yml:24`). The 3.10 run is local evidence only (ci.yml is out of scope).
- No existing test scans `playbooks/files/` or `deploy/` broadly. The C-1 guard scans `usecases`/`setups` only (`tests/test_catalog_template_guard.py:71-82`). `tests/test_docs_references.py` checks named docs only.
- Existing DPU scripts are mode 100755 with `#!/usr/bin/env python3` (`playbooks/files/daolab-app-exporter`). The new script follows that.
- `plan/10-backend-tasksheet.md` has no B-112 row. The Phase 8 rows live in the task list (AGENTS.md says to read the row there; noted, not blocking).

---

## 3. Conflicts between the spec sources

| # | Conflict | What I propose for B-112a |
|---|---|---|
| C-1 | Design §3 says the **checker** is streamed over stdin, with the baseline on the DPU at `/etc/daolab/tripwire-<hw_class>.json`. Q-BASELINE moved the baseline to the controller. B-112a has the **baseline** on stdin. Both cannot be "the stdin" unless something frames them. | The checker reads one JSON document from whatever is left on stdin and never relies on `__file__`. That works three ways: staged root-only and run with stdin = baseline; `python3 -I -B -c '<bootstrap>'` that reads a length-prefixed script and then leaves the JSON; or `ansible.builtin.script`. B-112b chooses. |
| C-2 | The task-list B-112 row says `"ignore": [<rule ids>]`. B-112a says fnmatch globs. | Follow B-112a (newer). |
| C-3 | Baseline file name: Q-BASELINE `deploy/tripwire/<hw_class>.json`, the B-112 row `synthetic-<hw_class>.json`, B-112a `synthetic-test.json`. | Ship `deploy/tripwire/synthetic-test.json`. B-112b's lookup by hw_class picks the naming rule. |
| C-4 | The round trip collect → `build_baseline` → check is listed under `tests/dpu/test_tripwire.py`, but that file must not import `service`. | (a) In `tests/test_tripwire.py` (3.12): the real `build_baseline` with the checker run as a subprocess. (b) In `tests/dpu` (3.10): the same round trip with a 10-line in-test builder. |
| C-5 | proc says "skip if unreadable". The general rule says unreadable entries are counted as `unreadable:<relpath-hash>`. | proc is the exception. Kernel threads and exiting pids have no readable exe, and the relpath contains the pid, so counting them would be pure noise. |
| C-6 | tmp says "regular files". The general rule says special files are recorded by type only. | tmp records regular files (relpath+uid+size) **and** specials (relpath+type). A FIFO or socket left in `/tmp` is residue too. Directories are not recorded. |
| C-7 | Overlaps: a `*.timer` under `etc/systemd/system` matches both sched and boot. A setuid file in `usr/local/bin` matches both bin sub-sets. | One plant gives one record. boot skips `*.timer` entries (they belong to sched). The bin setuid walk skips `usr/local/bin` and `usr/local/sbin`, whose content records already include mode+uid. |
| C-8 | AGENTS.md says to run `make test` after every change. This task says not to run the full suite (shared CPU; no imported module changes). | Follow the task's explicit instruction and say so in the commit message, as planned. |
| C-9 | `synthetic-test.json` is "built by the tests' own fake tree", but file fingerprints include uid and mode. CI's runner uid and umask differ from the laptop's, so the committed hashes cannot be reproduced byte for byte in CI. | The test checks that the committed file passes `load_baseline` with `hw_class` `test`, and that each class's fingerprint **count** equals what the fake tree yields (counts don't depend on uid). The README says how it was made. |
| C-10 | argparse errors print `usage: daolab-tripwire.py ...` (a file name) to stderr and exit 2. Rule 8 forbids that. | Parse arguments by hand. A bad option gives `tripwire:error:bad_input`, exit 2. |

---

## 4. Plan: the checker (`playbooks/files/daolab-tripwire.py`, Python 3.10 stdlib only)

**CLI.** `--mode check|collect` (required), `--root DIR` (default `/`), `--proc DIR` (default `/proc`). A non-existent root or proc dir gives `bad_input`.

**Input.** One JSON document on stdin:
- Read at most 16 MiB. Larger gives `bad_input`.
- It must be UTF-8.
- Duplicate keys are refused (`object_pairs_hook`), and so are `NaN`/`Infinity` (`parse_constant`).

**Error mapping (proposed; Q1):**
- `bad_input`: can't parse, not an object, the `format` is not the one this mode expects (`daolab-tripwire/1` for check, `daolab-tripwire-collect/1` for collect), bad option, a bad collect document.
- `bad_baseline`: the format is right but the content is wrong. That covers unknown or missing keys at any level, an unknown or missing class, `version` that is not an int ≥ 1 (bool refused), `hw_class` that is not kebab-case, a fingerprint that is not 64 lowercase hex, a duplicate fingerprint, and a bad glob (empty, absolute, containing NUL or newline, over 256 chars, more than 256 per class).
- `internal`: any other exception. It is caught at the top with `except BaseException`, excluding `SystemExit`. No traceback, nothing on stderr, `warnings` silenced.

**Output.**
- check: `tripwire:<class>:<count>` lines in the fixed class order for count > 0, else exactly `tripwire:ok`. Exit 0.
- collect: one line `{"classes": {"<class>": {"fingerprints": [sorted hex]}}}` for all 11 classes. Exit 0.
- Nothing else is ever printed.

**Count.** `len(current - baseline) + len(baseline - current)`. A changed file counts 2 (one removal, one addition). The README says so.

**Fingerprint.** `sha256(json.dumps([class, kind, field, ...], ensure_ascii=True, separators=(",", ":")))`.
- No raw newline is possible, and field boundaries can't be ambiguous.
- Non-UTF-8 names go through `os.fsdecode`, and the resulting surrogates are escaped deterministically.
- Content hashes are streamed in 1 MiB chunks. `hashlib.file_digest` is avoided because it is 3.11+.

**Walker.**
- An explicit stack over `os.scandir` with `follow_symlinks=False` everywhere. Not `os.walk`, which drops symlinks to directories from the file list.
- A visited `(st_dev, st_ino)` set for directories guards against bind-mount cycles. Symlinks are never followed, so a symlink loop is just two link records.
- It crosses filesystems (Q10). Mounts themselves are covered by the mount class.
- A walk root that is itself a symlink (e.g. `bin` → `usr/bin` on merged-usr) is recorded as a link record and not entered.
- An `OSError` on `lstat`, `scandir` or `open` records `["unreadable", relpath]`. proc is the exception (C-5).

**Ignore subject per class (Q5).** fnmatch uses `fnmatchcase`, so `*` also matches `/`.
- File classes: the relpath under `--root`, no leading `/`.
- identity: `<file>:<name>`, e.g. `etc/passwd:lab`.
- kmod: the module name.
- mount: the mount point without its leading `/`.
- proc: the exe path without its leading `/`.

**Per-class records.** Each collector is one small function with its own tests.

| Class | Record(s) |
|---|---|
| identity | `etc/passwd`, `etc/group`: `[file, whole line]` per line (newline stripped). `etc/shadow`, `etc/gshadow`: `[file, name, state]` with state `locked` (field starts `!` or `*`), `empty`, or `set`. The password field is never hashed, and neither are lastchg or expire. |
| sudo | `etc/sudoers` and each direct child of `etc/sudoers.d` (any type): file `[relpath, mode, uid, sha256]`, link `[relpath, target]`, other `[relpath, type]` |
| ssh | Everything under `etc/ssh` (same record shapes), plus `<home>/.ssh/authorized_keys` for every distinct home in `etc/passwd`. The home is resolved under `--root`, and one that escapes root via `..` is refused (recorded as unreadable). Excluded: `lab`'s home, taken from lab's passwd line. A changed lab home line is caught by identity. |
| sched | `etc/crontab`, `etc/cron.d/*`, `etc/cron.{hourly,daily,weekly,monthly}/*`, `var/spool/cron/crontabs/*`, `var/spool/cron/atjobs/*`: `[relpath, sha256]` (links as `[relpath, target]`); `*.timer` anywhere under `etc/systemd/system`: same |
| boot | Everything under `etc/systemd/system` except `*.timer`: link `[relpath, target]` (covers `*.wants/`, `*.requires/`, aliases, masks), file `[relpath, sha256]` (units, `.d/` overrides); `etc/rc.local` `[relpath, sha256]`. Files only: never `systemctl`. |
| bin | setuid/setgid regular files under `usr/bin`, `usr/sbin`, `usr/local` (minus `usr/local/{bin,sbin}`), `bin`, `sbin`, `opt`: `[relpath, mode, uid]`; everything under `usr/local/bin`, `usr/local/sbin`: file `[relpath, mode, uid, sha256]`, link, or type |
| kmod | `<proc>/modules`: `[name]` (first field) |
| mount | `<proc>/self/mountinfo`: `[mount point (field 5, `\NNN` octal decoded), fstype (first field after ` - `)]` |
| proc | each numeric `<proc>/<pid>` except our own pid: `[readlink exe, uid]`. uid = the real uid from the `Uid:` line of `status` (Q4; the tests can plant it without root). An unreadable exe or status skips the process. The result is a set. |
| tmp | `tmp`, `var/tmp`, `dev/shm`: regular `[relpath, uid, size]`, special `[relpath, type]` (C-6); dirs and symlinks: links `[relpath, target]` |
| daolab | everything under `etc/daolab`: file `[relpath, mode, uid, sha256]`, link, type |

---

## 5. Plan: `service/tripwire.py` (controller, 3.12)
- `CLASSES` is a tuple of the 11 names in output order, identical to the checker's. A test checks this by running the checker, not by importing it.
- `TripwireError(code)`. `str()` is the code only, and the raw text is never in the args. Codes:
  - loader: `unreadable`, `bad_format`, `unknown_key`, `missing_key`, `bad_class`, `bad_version`, `hw_class_mismatch`, `bad_hw_class`, `bad_hex`, `duplicate`, `bad_glob`;
  - parser: `bad_output`, plus the checker's own `bad_input`, `bad_baseline`, `internal`.
- `load_baseline(path, hw_class) -> (dict, sha256_hex)`:
  - reads the bytes once and hashes exactly those bytes;
  - applies the same rules as the checker (§4) plus `hw_class == arg`;
  - returns a plain dict.
  - hw_class shape: reuse `catalog.KEBAB` (Q17).
- `parse_output(text) -> list[str]`:
  - accepts one trailing `\n`;
  - `tripwire:ok` alone gives `[]`;
  - otherwise every line must be `tripwire:<known class>:<count>`, with count matching `[1-9][0-9]{0,8}`, the classes in fixed order and no repeats;
  - `tripwire:error:<code>` with a known code raises `TripwireError(code)`;
  - anything else (empty text, CR, extra lines, an unknown class, `0`, a sign, leading zeros, ok mixed with counts) raises `TripwireError("bad_output")`.
- `build_baseline(collect_json, hw_class, version, ignore) -> dict`:
  - `collect_json` is the checker's collect line (str);
  - it validates the shape, `hw_class` and `version`, and the `ignore` map (missing classes get `[]`);
  - it returns the closed-key dict, and the result passes the same validation as `load_baseline`.
  - Caller contract, documented: the collect run must have used the same ignore map (see Q13).

## 6. Plan: tests
**`tests/dpu/test_tripwire.py`** (stdlib + pytest, no conftest, no `service`):
- It runs the script with `sys.executable` (so 3.10 on b109a-310) as a subprocess with `--root`/`--proc` on a tmp_path fake tree, with `timeout=60`.
- A `make_tree()` helper builds a clean board (all 11 classes non-empty). A mini builder turns collect output into a baseline (C-4b).
- For **each class**:
  - one plant gives count 1;
  - one removal gives count 1;
  - plant + removal gives 2;
  - a changed file gives 2.
- Clean tree gives `tripwire:ok`.
- An ignore glob suppresses a planted noise file.
- shadow: a password change on a `set` user gives no count; locked → set gives 2 (a changed record).
- An edit to lab's `authorized_keys` gives no count; another user's gives a count.
- A symlink loop and a link to `..` don't hang or crash. Neither does an unreadable file or dir, which counts as unreadable (skipped when euid is 0).
- Each bad-input case gives the exact error line and exit 2, with an empty stderr.
- Safe output: unique planted tokens (user `zq7plantusr`, uid `54321`, module `zq7plantmod`, path parts `zq7plant...`, a unit `zq7plant.service`, an exe `/opt/zq7plantexe`) never appear in stdout or stderr of any check or collect run.
- Informational timing on about 5000 files, printed in the log and not asserted.

**`tests/test_tripwire.py`** (3.12, may import `service`):
- each loader refusal;
- sha256 equals `hashlib.sha256(path.read_bytes())`;
- `parse_output` accepted and refused cases;
- `TripwireError` never contains the raw text (`str`, `repr`, `args`);
- a cross-check: every loader refusal is also refused by the checker with `bad_baseline`/`bad_input`;
- the round trip (C-4a);
- the committed `synthetic-test.json` loads and its per-class counts match (C-9).
- It uses the fake-tree builder from `tests/dpu/test_tripwire.py`, loaded by file path with `importlib` (no package import, nothing re-collected).

---

## 7. Noise risks per class (from the repo's playbooks) and proposed ignore globs

"Stable" below means the item is the same after every cleanup, so it is noise only if the baseline was taken before the first booking. Globs are **proposals for Rama**, for the synthetic baseline and as input to production. They are not baseline contents.

| Class | What the repo writes or changes outside `/home/lab` | Effect after cleanup | Proposed glob |
|---|---|---|---|
| identity | `useradd lab` on the first booking (`roles/dpu_access/tasks/main.yml:10`). `chpasswd` every deploy (`main.yml:94`) and revoke (`revoke.yml:28`). `usermod -L` + `chage -E 0` at revoke (`revoke.yml:36`). VPP's apt install may add a `vpp` group (UNVERIFIED). | lab's shadow state is always `locked` after revoke. The hash, lastchg and expire are not hashed, so it is **stable**. One-time adds. | none (identity is never ignored) |
| sudo | `/etc/sudoers.d/90-daolab-lab` written every deploy with constant content (`main.yml:25`). **Never removed by revoke.** | stable, present | none |
| ssh | `/etc/ssh/sshd_config.d/10-daolab-lab.conf`, constant (`main.yml:57`). lab's real keys live in `/etc/daolab/authorized_keys/lab` (daolab class). openssh upgrades touch `/etc/ssh/*`. | stable; re-baseline after openssh upgrades | none |
| sched | nothing from the playbooks. `at` increments `var/spool/cron/atjobs/.SEQ` whenever anyone uses it. | user-caused only | `var/spool/cron/atjobs/.SEQ` |
| boot | `etc/systemd/system/daolab-app-exporter.service` **rewritten every deploy with an app-specific `ExecStart`** (`tasks/app_exporter.yml:14-24`). Cleanup only does `disable --now` (`apps/dao-vpp/cleanup.yml:29`, `apps/nginx-tls/cleanup.yml:9`, `apps/dao-smartnic/cleanup.yml:14`, `apps/ml-tvm/cleanup.yml:31`). The `enable` wants-links (`app_exporter.yml:34`, `roles/otel_agent/tasks/main.yml:36`) are removed by `disable`. `daolab-otel.service` is a constant template (`main.yml:25`). VPP's package may enable `vpp.service` once (UNVERIFIED). | **count 2 whenever the last app ≠ the baseline's** | `etc/systemd/system/daolab-app-exporter.service`. Better: cleanup removes the unit (playbook owner, not B-112a). |
| bin | `usr/local/bin/daolab-app-exporter` (`app_exporter.yml:8`); `usr/local/bin/daolab-ml-smoke` (`apps/ml-tvm/deploy.yml:166,173`), **not removed** by ml-tvm cleanup (only `state.json`, `cleanup.yml:84-88`); age/age-keygen (`tasks/install_age.yml`); otelcol-contrib (`roles/otel_agent/defaults/main.yml:22`); daolab-keys | stable per repo release; **re-baseline after any release that changes them** | none (bin is the partial tamper guard) |
| kmod | vfio-pci binds: VPP `09_configure_04.yml`, SmartNIC `dpdk-devbind -b vfio-pci` (`smartnic_launch`), ml-tvm `ml_bind` (`deploy.yml` "step: ml_bind"). If the vfio modules are loadable (not built-in), they load on first use and stay. A baseline right after boot then differs from one taken after the first deploy (UNVERIFIED on the CN10K kernel). Also on-demand autoload from user activity. | boot-history dependent | `vfio`, `vfio_pci`, `vfio_pci_core`, `vfio_iommu_type1`, only if Rama confirms (kmod is mock-only per Q-PLANTS) |
| mount | `/run/user/<uid>`: one per logged-in user, so the transport's own session appears whenever the checker runs over ssh. lab's goes at `terminate-user` (`revoke.yml`, step terminate-user). hugetlbfs `/mnt/huge` is added by the CPT script after a reboot (`tasks/release_hugepages.yml:17-18`). snap loop mounts change on refresh (if snapd is installed: UNVERIFIED). binfmt_misc autofs. | transport- and boot-dependent | `run/user/*` (debatable: lab leftovers are already covered by `dpu_session`), `snap/*` |
| proc | transient: the transport chain (sshd, sh, sudo, python3 = the checker's own exe), cron/apt-daily/unattended-upgrades, ` (deleted)` exes after upgrades. Exporter/otel/vpp/smartnic are stopped by cleanup; nginx-tls restarts with the same exe. | **collect and check must use the same transport** | none by default; exact exes later (Rama) |
| tmp | SmartNIC: `/tmp/dao-smart-nic-ansible.log` (`smartnic_launch/tasks/main.yml:69`, size varies per run) and `/tmp/pci-after.txt` (`smartnic_cleanup/tasks/main.yml:57`) are both left behind. VPP's `/dev/shm` files UNVERIFIED. systemd PrivateTmp dirs (normally hold only dirs). | **count after SmartNIC bookings** | `tmp/dao-smart-nic-ansible.log`, `tmp/pci-after.txt`. Better: SmartNIC cleanup removes them. |
| daolab | `/etc/daolab/otelcol.yaml` **rewritten every deploy with `daolab.reservation` and `daolab.user`** (`playbooks/deploy.yml:65-66`, `roles/otel_agent/tasks/main.yml:15-18`, `templates/otelcol.yaml.j2:2,50-51`). Cleanup only disables the service (`playbooks/cleanup.yml:32`). `/etc/daolab/authorized_keys/lab` is rewritten every deploy but truncated by revoke (`revoke.yml:23`). `hw_class` (`bringup.yml:52-54`) is stable. `keys.conf`/`authz-header` change on secret rotation. | **count 2 after every booking with a collector**; rotation needs a re-baseline | `etc/daolab/otelcol.yaml`. Better: cleanup removes it, because ignoring it leaves a root-planted otel exporter undetected. |

Written by the repo but in **no** class, so no noise but also no coverage:
- `/opt/async_nginx.conf` and `.daolab-rest` (`apps/nginx-tls/deploy.yml:32-41`)
- `/etc/default/daolab-ml` (`apps/ml-tvm/deploy.yml:181`)
- `/var/lib/daolab/ml/` (`:118`)
- `/etc/sysctl.d/80-vpp.conf(.disabled)` (`VPP_ansible/playbooks/03_install.yml:52`)
- `/var/run/dpdk/*` and hugepage map files
- `/home/dao-test/...`
- B-109/B-110 staging `/var/lib/daolab/{capture,restore}`

**For B-112b:** if the baseline is ever passed in a temp file, it must not live under `tmp`, `var/tmp` or `dev/shm`. Otherwise the checker counts its own input every time.

## 8. Gaps beyond the spec (for Rama; nothing changes in B-112a, the README lists them honestly)
Each of these lets a root user leave something that affects the next user, and no class looks at it:
- `etc/skel`: a poisoned skel defeats the Q-EMPTY "only skel" check and seeds the next home;
- `etc/profile.d`, `etc/bash.bashrc`, `etc/update-motd.d`: run in, or at, the next user's login;
- `etc/ld.so.preload`;
- `etc/sudo.conf` (sudo plugins);
- `etc/pam.d`;
- `etc/modprobe.d` (`install` hooks run as root at the next vfio load);
- `etc/udev/rules.d`;
- `etc/apt` (sources and hooks; VPP installs via apt);
- `usr/lib/systemd/system` and its `*.wants`;
- `etc/systemd/user`;
- `.ssh/authorized_keys2` (Ubuntu's default `AuthorizedKeysFile` reads it);
- `/opt/async_nginx.conf` and `/var/www/html` (served to the next user by the resting nginx-tls).

Known limits of the spec as written:
- **shadow:** a password change on an account that is already `set` (e.g. the operator's) is not detected (Q9).
- **mount:** a second mount stacked on an existing mount point with the same fstype collapses in the set (Q8).
- **proc:** an extra process running an exe+uid that is already in the baseline collapses too.
- **Hashes are confirmable:** fingerprints of guessable records (e.g. a passwd line) can be confirmed by someone who holds the baseline or collect output (Q14).

## 9. Notes for B-112b (not done here)
- Add `tripwire` to `RESIDUE_KINDS` and `CHECK_FOR_KIND` (`runner.py:75-78`).
- `TripwireError` should mean "could not verify", like `runner.py:650`. The error code may appear as `tripwire:error:<code>` in the job error, and is safe.
- Run the checker with `python3 -I -B` (ignores `PYTHON*` env and user site, writes no `.pyc`).
- The run time adds up to 1+`reconcile_attempts` times per cleanup (`engine.py:335`).
- The collect output (hashes) should stay out of ansible-runner job artifacts.
- Collect and check must use the same transport (proc and mount noise).

---

## 10. Questions (my proposed default in **bold**; I wait for review before coding)
1. **Error codes:** `bad_input` vs `bad_baseline` as mapped in §4?
2. **C-4:** round trip in `tests/test_tripwire.py` (real `build_baseline`) plus a mini builder in `tests/dpu`; `tests/test_tripwire.py` loads the fake-tree builder from the dpu test file by path?
3. **C-5/C-6/C-7:** proc skips unreadable; tmp also records specials by type; one plant gives one record (boot skips `*.timer`; the setuid walk skips `usr/local/{bin,sbin}`)?
4. **proc uid** = the real uid from `<proc>/<pid>/status` (testable without root) rather than the pid dir's owner?
5. **Ignore subjects** for the non-file classes as in §4 (`etc/passwd:<name>`, module name, mount point, exe path, without leading `/`)?
6. **C-9:** `synthetic-test.json` is checked by load + per-class counts, not byte equality?
7. Add `.ssh/authorized_keys2` to ssh? **Recommend yes** (no change to the output contract), but it is outside the written spec, so I need your OK.
8. mount: add mountinfo field 4 (root) so bind mounts are seen? **Default no** (spec as written; documented limit).
9. shadow: also hash lastchg (field 3) for users other than `lab`, so an operator or root password change is seen without touching the password field? **Default no** (spec as written; documented gap; Rama).
10. The walker crosses filesystems, with a `(dev, ino)` cycle guard? **Yes.**
11. Strict JSON (duplicate keys, NaN, bool version, duplicate fingerprints all refused)? **Yes.**
12. The excluded login is hard-coded `lab` (the role's default `daolab_access_login`)? **Yes.**
13. The collect output stays exactly `{"classes": ...}` (no format tag, nothing binding it to the ignore map); `build_baseline` documents the same-ignore contract? **Yes, spec as written.**
14. Salted fingerprints (a per-baseline salt) so the hashes can't be confirmed by guessing? **Default no** (closed format as written; README notes it).
15. `synthetic-test.json` ignore lists hold only the test's noise glob(s); the realistic list in §7 goes in the README as a proposal? **Yes.**
16. One `TripwireError` class with the codes in §5? **Yes.**
17. `service/tripwire.py` imports `catalog.KEBAB` for the hw_class shape (no copy)? **Yes.**

## 11. Verification for this stage
- Base: `git fetch origin`, then `git rev-parse --short origin/rama` gave `65e47fc`. HEAD `65e47fc`; `git status --short` was empty.
- Read-only commands only: `ls`, `cat -n`/`sed -n`/`grep -n` on repo files and `~/notes/*.md`, `git show origin/akshay-p8-gate:<design>`, `git ls-files`, `git ls-tree`.
- Check: `cd ~/b112a && git status --short` should still be empty. This file is outside the repo.

---

## 12. Decisions (Akshay's review, 27 Nov) — these govern Stage 1
- All §10 defaults ACCEPTED, with these changes/additions:
- Q1, Q3, Q4: defaults. Q7: YES, `.ssh/authorized_keys2` joins ssh.
- Collectors added inside EXISTING classes (output format unchanged), each with a planted-addition test;
  listed in the commit message under "Collector additions beyond the brief's table":
  - identity: `etc/skel` (whole tree), `etc/profile`, `etc/profile.d/*`, `etc/bash.bashrc`, `etc/environment`
  - bin: `etc/ld.so.preload`, `etc/ld.so.conf`, `etc/ld.so.conf.d/*`
  - kmod: `etc/modprobe.d/*`, `etc/modules`, `etc/modules-load.d/*`
  - NOT nginx-tls config or `var/www` (change with every tls-proxy deploy) -> "For Rama (production baselines)".
- The 4 playbook noise sources: ignore globs in the SYNTHETIC baseline only. Commit message gets "Follow-ups":
  SmartNIC cleanup should remove `/tmp/dao-smart-nic-ansible.log` + `/tmp/pci-after.txt`; the exporter unit
  and `daolab-ml-smoke` need a defined resting state; `/etc/daolab/otelcol.yaml` keeps the last
  reservation's user name after cleanup (privacy, for Rama).
- stdin: support both — baseline on stdin by default; an option to read it from a file descriptor
  (for when the script itself comes on stdin). B-112b picks.
- Accepted: the round-trip split (C-4), synthetic-test.json "loads + per-class counts" (C-9),
  hand-parsed options (C-10), the task's no-full-suite rule over AGENTS.md (C-8).
- Q17 re-checked: `service/catalog.py` imports only ipam/models/migrations/yaml (not `service.config`,
  which calls `load_dotenv()` at import), so importing `catalog.KEBAB` cannot read `.env`.
- From here: run to the end; other judgement calls -> safest option, recorded under
  "Decisions taken without asking" (commit message + §13 below).

## 13. Stage 1 result and decisions taken without asking (safest option each time)
Result: patch `B112A-tripwire.patch` (6 files, +2747), staged tree `f89725e535995c9f0a1c2b5f5030ca5da8e3db70`
= fresh-worktree tree (TREE MATCH). Red: 170/170 failed (checker missing) on 3.12.13 and 3.10.12, 180/180
errors (`service.tripwire` missing). Green: 170 + 170 + 180 passed. Planted-name grep: 45 runs, 0 hits.

Decisions taken without asking:
1. identity also records the mode + owner of passwd/group/shadow/gshadow (a chmod of shadow is caught).
2. Walked trees record directories too (path+mode+uid; path only in sched/boot) and the walk root itself,
   so a new directory with a file is 2 records; tmp records no directories (spec: files).
3. An ignored directory skips its whole subtree.
4. Synthetic ignore = the playbook globs only (exporter unit, the 2 SmartNIC tmp files, otelcol.yaml) +
   the tests' `tmp/daolab-test-noise-*`. NOT ignored: `daolab-ml-smoke` (bin is the tamper guard; stable
   after the first ml-tvm deploy). Left for Rama: `.SEQ`, vfio modules, `run/user/*`, `snap/*`.
5. Strict JSON on both sides: ints over 18 digits refused (same on every Python, not only >= 3.10.7),
   RecursionError -> bad_input; hw_class <= 128 chars (approvals TARGET_RE) on both sides.
6. `service/tripwire.py` uses `catalog.KEBAB.fullmatch`, not `.match` (KEBAB ends in `$`, which matches
   before a trailing newline; the checker uses fullmatch). Pre-existing, not changed: `catalog.py:285`
   uses `.match`, so a catalog hw_class with a trailing newline would pass there.
7. Files are opened with O_NOFOLLOW|O_NONBLOCK and fstat-checked: a FIFO swapped in after lstat can't hang it.
8. The checker skips its own pid in proc.
9. Options: only `--opt value`; `--opt=value`, `--help`, repeats, positionals -> bad_input. `--input-fd`
   digits only (<= 6). A non-directory or missing --root/--proc -> bad_input, before any input is read.
10. TripwireError is raised outside `except` blocks: no `__context__`, so json's error (which holds the
    document) is never chained; the path is never in the message either.
11. Glob validation also refuses CR (besides NUL, LF, leading `/`, empty, > 256 chars, > 256 globs, dups).
12. build_baseline: an `ignore` that is not a dict -> `bad_class`.
13. Test-side fixes during green (no expectation changed): the `write()` helper could not rewrite its own
    0440 files (now makes them writable, keeps the mode); the proc record kind is `"p"` in the code
    because the test expected `"p"` (internal tag only).
14. `.venv` ran tests/dpu without --confcutdir (as CI will); the 3.10 venv with it.
15. synthetic-test.json was generated outside pytest from `tests/test_tripwire.py::synthetic_baseline`;
    checked in-process that neither `service.config` nor `dotenv` was imported (no .env read).
    Regenerating it on the same machine is byte-identical.
