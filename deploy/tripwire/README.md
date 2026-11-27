# Tripwire: checking the board after every cleanup (B-112a)

After a booking is cleaned up, the tripwire looks at the rest of the board, outside `/home/lab`, for
anything the user may have left behind that could affect the next user: an extra sudo rule, an SSH key,
a cron job, a kernel module, a running process. It compares what it finds with a **baseline** (a list
of fingerprints of a known-good board) and reports only **"class X: N differences"**. It never reports
what or where.

- The checker: `playbooks/files/daolab-tripwire.py` (runs on the DPU; Python 3.10 standard library only).
- The controller side: `service/tripwire.py`: `load_baseline`, `parse_output`, `build_baseline`.
- A synthetic baseline for tests: `deploy/tripwire/synthetic-test.json` (hw_class `test`).

Wiring it into the runner/engine (the `tripwire` residue kind, quarantine, the two-admin accept and
re-baseline flow) is B-112b. Nothing calls the tripwire yet.

## Detection only

The tripwire is **detection, not proof of cleanliness** (design v1.1 §2, §3). During the slot the user
was root, so they could have:
- replaced `python3` or a library the checker uses;
- made the board lie to it, e.g. with an LD_PRELOAD hook, a kernel module, or a bind mount over a
  checked path;
- changed things the checker does not look at.

The `bin` class (setuid files, `/usr/local/{bin,sbin}`, `ld.so.preload`/`ld.so.conf`) and the `kmod`
class only *partly* cover tampering with the checker itself. Firmware, the BMC and anything below the OS
are out of scope.

The authoritative baseline lives **on the controller**, never on the DPU (Q-BASELINE). A copy on the DPU
could have been edited by the user. B-112b streams the exact baseline bytes to the checker and records
the file's sha256 (from `load_baseline`) in the audit trail.

## Running the checker

```
daolab-tripwire.py --mode check|collect [--root DIR] [--proc DIR] [--input-fd N]
```

It reads **one JSON document**, from stdin or, with `--input-fd N`, from that file descriptor. The second
form is for when the script itself arrives on stdin (`python3 -I -B -c <bootstrap>`); B-112b chooses the
transport. `--root` (default `/`) and `--proc` (default `/proc`) exist so tests can run it on a fake board.
B-112b should run it as `python3 -I -B`: isolated mode ignores `PYTHON*` variables and the user site, and
`-B` writes no bytecode.

| Mode | Input | Output (exit 0) |
|---|---|---|
| `check` | the baseline | one line `tripwire:<class>:<count>` per class with a count > 0, in the fixed class order; or exactly `tripwire:ok` |
| `collect` | `{"format": "daolab-tripwire-collect/1", "ignore": {"<class>": ["<glob>", ...]}}` | one line `{"classes": {"<class>": {"fingerprints": ["<sorted hex>", ...]}}}` |

**Count semantics:** `count = |current − baseline| + |baseline − current|`. Additions and removals both
count, so **a changed entry counts 2** (the old fingerprint is gone, a new one appeared).

**Errors** print exactly one line and exit 2, with nothing on stderr:
- `tripwire:error:bad_input`: bad options, unreadable or oversized input (16 MiB), not strict JSON
  (UTF-8, no duplicate keys, no NaN, no huge numbers), the wrong `format` for the mode, or a bad collect
  document.
- `tripwire:error:bad_baseline`: the format is right but the contents are wrong. That covers an
  unknown or missing key, a class name not in the 11, `version` not an integer ≥ 1, `hw_class` not
  kebab-case, a fingerprint that is not 64 lowercase hex, duplicates, and a bad glob.
- `tripwire:error:internal`: anything unexpected (no traceback anywhere).

**Safe output:** the checker never prints a path, a file or user name, a uid, a module or unit name, or
a command line. Only the lines above, or collect mode's hex fingerprints.

`service/tripwire.py` `parse_output` turns check output into residue items `tripwire:<class>:<count>`
(`[]` for `tripwire:ok`). It raises `TripwireError(code)` for an error line, and for anything that is
not exactly the contract: an unknown class, a count ≤ 0, extra lines, CR, any other text. The exception
carries only the code, never the raw text.

## What each class checks

Every walk is under `--root`, uses `lstat` and **never follows a symlink**: a link is recorded as its
target string. Special files are recorded by type only. An entry that can't be read is recorded as
"unreadable" plus its path, so it cannot hide. A fingerprint is the sha256 of a normalized record
(`json.dumps([class, kind, fields...])`, ASCII-escaped), so it never contains a newline.

| Class | What is fingerprinted |
|---|---|
| `identity` | `etc/passwd`, `etc/group`: each whole line. `etc/shadow`, `etc/gshadow`: each name + password **state** only (`locked` if the field starts with `!` or `*`, `empty`, else `set`). The password field is never hashed: it is secret, and lab's rotates every booking. Plus the mode and owner of these four files, and (beyond the brief) `etc/skel` (whole tree), `etc/profile`, `etc/profile.d`, `etc/bash.bashrc`, `etc/environment`: what every new home and every login shell gets. |
| `sudo` | `etc/sudoers` and everything in `etc/sudoers.d`: path + mode + uid + sha256 of the content. |
| `ssh` | Everything under `etc/ssh` (path + mode + uid + content hash), and `.ssh/authorized_keys` and `.ssh/authorized_keys2` in every home listed in `etc/passwd`, **except lab's home**. That home is covered by snapshot capture and wipe, and its keys change every booking. A change to lab's passwd line (e.g. its home) is caught by `identity`. |
| `sched` | `etc/crontab`, `etc/cron.d`, `etc/cron.{hourly,daily,weekly,monthly}`, `var/spool/cron/crontabs`, `var/spool/cron/atjobs` (path + content hash), and every `*.timer` under `etc/systemd/system`. |
| `boot` | Files only, never `systemctl`: everything under `etc/systemd/system` except `*.timer` (those are `sched`). That means links (`*.wants/`, `*.requires/`, aliases, masks) by target, and unit files and `.d/` overrides by content hash. Plus `etc/rc.local`. |
| `bin` | setuid/setgid regular files under `usr/bin`, `usr/sbin`, `usr/local`, `bin`, `sbin`, `opt` (path + mode + uid; a merged-usr `bin -> usr/bin` link is recorded, not walked). Content hashes of everything in `usr/local/bin` and `usr/local/sbin` (not all of `/usr/bin`: too slow). Beyond the brief: `etc/ld.so.preload`, `etc/ld.so.conf`, `etc/ld.so.conf.d`. |
| `kmod` | Loaded module names (`<proc>/modules`, first field). Beyond the brief: `etc/modprobe.d`, `etc/modules`, `etc/modules-load.d` (content). |
| `mount` | Mount point (octal escapes decoded) + fstype from `<proc>/self/mountinfo`. Mount options are not recorded. |
| `proc` | For each process: the executable path (`readlink exe`) + real uid (`Uid:` in `status`). Never the pid, and duplicates collapse. A process with no readable exe (kernel threads, one that just exited) is skipped, and so is the checker itself. |
| `tmp` | Under `tmp`, `var/tmp`, `dev/shm`: regular files (path + uid + size, **not** content), links and special files. Directories alone are not recorded. |
| `daolab` | Everything under `etc/daolab` (path + mode + uid + content hash). |

## Noise rules (`ignore` globs)

Each class has an `ignore` list of `fnmatch` globs (case-sensitive; `*` also matches `/`). They are
matched against each record's **subject**:
- the path relative to `--root` for file records (an ignored directory skips its subtree);
- `<file>:<name>` for identity lines (e.g. `etc/passwd:lab`);
- the module name for loaded modules;
- the mount point and the exe path, each without its leading `/`.

The globs live only in the controller-side baseline. They are never printed.

**The collect run that builds a baseline must use the same ignore globs as the baseline**:
`build_baseline` cannot check this. Otherwise the baseline holds fingerprints that the checker now
skips, and they count as removed on every check.

The synthetic baseline ignores the playbook noise sources found in B-112a's review. They are synthetic
rules, not production decisions:

| Class | Glob | Why |
|---|---|---|
| `boot` | `etc/systemd/system/daolab-app-exporter.service` | every deploy rewrites the unit with app-specific arguments; cleanup only disables it |
| `tmp` | `tmp/dao-smart-nic-ansible.log`, `tmp/pci-after.txt` | left behind by the SmartNIC launch/cleanup roles |
| `daolab` | `etc/daolab/otelcol.yaml` | every deploy rewrites it with the reservation's id and user; cleanup leaves it |
| `tmp` | `tmp/daolab-test-noise-*` | the tests' own noise rule |

`usr/local/bin/daolab-ml-smoke` (installed by ml-tvm, not removed by its cleanup) is **not** ignored:
`bin` is the partial tamper guard. It is stable after the first ml-tvm deploy.

## Baselines: synthetic and production

- **Synthetic** baselines (`deploy/tripwire/synthetic-<hw_class>.json`) are built from the tests' fake
  board and exist for tests only. They contain no real board's contents.
- **Production** baselines (`/etc/daolab/tripwire/` on the controller, Q-BASELINE) are **OPEN: Rama
  provides them.** B-112a does not invent their contents (Rama, 23 Nov). Taking one needs a decision on
  when (after the first booking, so `lab`, its sudoers drop-in and the installed tools exist), on which
  noise rules apply, and on the transport. Collect and check must run the same way, because the
  transport's own processes and sessions appear in `proc` and `mount`.

### How a synthetic baseline is built

1. Run the checker in collect mode on the board (here: the fake board of `tests/dpu/test_tripwire.py`)
   with the ignore globs the baseline will carry:
   `echo '{"format": "daolab-tripwire-collect/1", "ignore": {...}}' | python3 -I -B daolab-tripwire.py --mode collect --root ROOT --proc PROC`
2. Build the baseline on the controller and write it as `json.dumps(doc, indent=2)` plus a newline:
   `service.tripwire.build_baseline(<collect line>, "<hw_class>", <version>, <same ignore>)`.
3. Check it: `load_baseline(path, "<hw_class>")` must accept it, and a check run on the same board must
   print `tripwire:ok`.

`synthetic-test.json` was made this way by `synthetic_baseline()` in `tests/test_tripwire.py` (fake board
from `make_tree()`, globs `SYNTHETIC_IGNORE`, version 1). Its file fingerprints include the uid and mode
of the machine that built it, so the tests check that it loads, that its ignore lists match, and that
each class has the same number of fingerprints as a fresh fake board. The classes with no uid or mode
in their records (`sched`, `boot`, `kmod`, `mount`, `proc`) must match exactly.

## Known limits (documented, not fixed)

- A password change on an account whose state is already `set` (the operator's, root's) does not count;
  only the state class is compared.
- A second mount stacked on an existing mount point with the same fstype collapses into the set, and so
  does a second process with an exe and uid already in the baseline.
- Fingerprints of guessable records (e.g. a passwd line) can be confirmed by someone holding the baseline
  or the collect output. Keep collect output out of job logs.
- A root user can make the checker slow (millions of files in `/tmp`). B-112b's timeout then means "could
  not verify", which is not clean.

## Not covered (for Rama: production baselines)

These can affect the next user but are in no class:
- the nginx-tls config (`/opt/async_nginx.conf`) and its document root (`/var/www/html`). They change
  with every tls-proxy deploy, so they need a production rule, not a file hash;
- `etc/sudo.conf`, `etc/pam.d`, `etc/update-motd.d`, `etc/udev/rules.d`, `etc/apt` (sources and hooks;
  VPP installs via apt), `usr/lib/systemd/system` and its `*.wants`, `etc/systemd/user`;
- `etc/default/daolab-ml`, `/var/lib/daolab/`.

## Tests

- `tests/dpu/test_tripwire.py`: the checker on a fake board under `tmp_path` (stdlib + pytest, no
  conftest). Run it on the DPU's Python too: `python3.10 -m pytest tests/dpu/test_tripwire.py --confcutdir=tests/dpu`.
- `tests/test_tripwire.py`: `service/tripwire.py`, plus a cross-check that the checker refuses every
  baseline the loader refuses.
