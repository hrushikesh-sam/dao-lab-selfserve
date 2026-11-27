# B-109a — DPU capture script and the Python 3.10 CI job: FINDINGS

Base: `65e47fc` (rama head, "Merge pull request #14 …"; `origin/rama` local ref = `65e47fc`, not re-fetched).
Worktree: `~/b109a`, branch `b109a`. Nothing committed or pushed.
Files (the task's list, `~/notes/p8-task-breakdown.md:560`): `playbooks/files/daolab-capture.py` (new),
`tests/dpu/test_capture.py` (new), `tests/dpu/conftest.py` (new), `.github/workflows/ci.yml` (new job).

## 0. Stage 0 facts (at 65e47fc)

| Fact | Where |
|---|---|
| The B-109a row is in the §8 task table, not in plan/10 (the §8 table records the Phase 8 split missing from plan/10) | `plan/design/p8-gate-review/snapshot-design.md:17` |
| Q-EMPTY is **DECIDED** (Rama, 24 Nov): "empty = only /etc/skel; missing / not a dir / wrong owner = refuse". The prompt's "Q-EMPTY, proposed" is stale; **§6 of the task list overrides it** | `~/notes/p8-task-breakdown.md:190` |
| Compression and DPU staging DECIDED: raw cap before `gzip -1` inside `daolab-capture.py`; `/var/lib/daolab/capture/<public_id>.age`, dir 0700, file 0600 | `plan/design/p8-gate-review/E-15-storage-quota.md:28-29` |
| E-3 (3.10 stdlib + 3.10 test), E-6 (root; root-owned files are user data), E-7 (sysctls) | `plan/design/p8-gate-review/p8-design-errata-v1.2.md:21,24,25` |
| GNU tar 1.34 on the laptop (= board): `--one-file-system`, `--numeric-owner`, `--acls`, `--xattrs` all present; a test archive rc 0 | `tar --help` |
| age v1.1.1 in `~/.local/bin`; `age -o` onto a pre-created O_EXCL 0600 file keeps 0600; a file age creates under umask 002 is 0664 (matches the 24 Nov board observation) | scratch test |
| pytest: rootdir = repo, configfile = `pyproject.toml` (no pytest table); `make test` = `python -m pytest tests/ -q` collects `tests/dpu` on 3.12 | `Makefile:9-10` |
| **`tests/conftest.py` imports `dotenv` and `service.models`**, so `pytest tests/dpu` on a bare 3.10 fails ("No module named 'dotenv'"); `requires-python = ">=3.12"` rules out installing the service on 3.10 | `tests/conftest.py:22,30`; `pyproject.toml:11` |
| `requires_age` marker + gate live in `tests/conftest.py` | `tests/conftest.py:45-96` |
| CI: one job `test` (3.12/3.13), age via curl + `install-age.sh <tarball> $HOME/.local`, `DAOLAB_REQUIRE_AGE=1` | `.github/workflows/ci.yml:15,24,39,50-51` |
| Two existing tests read `jobs.test` only, so a new job doesn't affect them | `tests/test_platform_floor.py:66`, `tests/test_age_install.py:377` |
| DPU helpers live in `playbooks/files/` (executables are 100755) | `git ls-files -s playbooks/files` |
| No `python` on PATH; `/usr/bin/ansible-playbook` is **2.10.8**, so runs must put the venv's `bin` first on PATH (see §4) | `command -v ansible-playbook` |

## 1. Decisions (Akshay, 27 Nov)

1. **Owner check added** (task list §6: "wrong owner = refuse"). New required `--owner-uid N`, new reason `home_owner`. Only the home directory itself is checked (lstat, no symlink following); files inside with any owner are user data (E-6) and are captured. B-109b passes the lab user's uid.
2. **Strict empty rule:** "empty" only if the home's whole tree equals skel: the same entries at every depth, same types, regular files byte-identical, symlink targets identical. Anything else is captured. Counts only. Tests: same-size edited `.bashrc` → saved; extra file in a subdirectory → saved; identical copy of skel → empty.
3. **`bad_args`** added: argparse errors are caught (no usage text on stdout or stderr), one JSON line, exit 1.
4. **`--out`'s parent** must be owned by `os.geteuid()` and be mode 0700, else `path_unsafe`. Both tested.
5. The Stage 0 defaults were all accepted: tar/age stderr discarded; gzip header without name and with mtime 0; `out_exists` never deletes the existing file; a symlinked home is `home_not_dir`; `--out` inside home refused; refused/empty report `0, 0, ""`; fsync before sha256.

## 2. Decisions taken without asking (the safest option each time)

1. **Check order:** all argument syntax first (`bad_args`, including the recipient shape), then paths, `out_exists`, sysctls, mounts, home, empty, capture. The prompt lists the recipient as step 2; the order only matters when several faults coincide.
2. **Stricter path rule:** besides the prompt's list, also refused: any top-level directory (fewer than 2 components), control characters, a leading `//`, `.` components. Applied to `--skel` too. home/skel may not be inside each other; `--out` may not be inside home or skel, also checked on the resolved path of `--out`'s parent.
3. **`out_exists` timing:** checked with lstat before anything else runs, so an early refusal creates nothing. The O_EXCL create happens only when capture starts; EEXIST there is also `out_exists`. A pre-existing file or dangling symlink is never touched.
4. `--out`'s parent must be a real directory (lstat; a symlink is `path_unsafe`).
5. `--age`, `--mountinfo` and `--sysctl-root` must be clean absolute paths (`bad_args`). An `--age` that can't run is `age_failed`.
6. Every option exactly once: a repeat is `bad_args`, not "last wins"; abbreviations off; `-h`/`--help` are `bad_args`.
7. `--cap-bytes`: digits only, ≥ 1. `--owner-uid`: digits only, ≤ 4294967294.
8. A sysctl must read exactly `1` (one trailing newline allowed): `0`, `2`, `11`, or a missing file are `sysctl_unsafe`.
9. **Mount guard on two paths:** the given home and its realpath (a symlinked parent can't hide a mount). Unreadable, empty or malformed mountinfo, or a bad octal escape, is `mount_under_home` (fail closed).
10. home lstat: ENOENT/ENOTDIR → `home_missing`; any other error → `home_not_dir`.
11. **Empty compare:** FIFOs, sockets and devices never count as matching skel. Files are opened `O_NOFOLLOW|O_NONBLOCK` and checked with fstat, so a FIFO swapped in can't hang it. Any error, or a missing skel, means "not empty" → capture. The walk is bounded by skel (names must match level by level). No extra JSON fields: the schema stays the five keys.
12. **Children:** tar is the fixed `/usr/bin/tar` (no PATH lookup as root). Both children get a fixed environment `{PATH, LC_ALL=C}`, so a caller's `TAR_OPTIONS` can't add options. tar stdin and both stderrs are `/dev/null`.
13. gzip is `zlib.compressobj(1, DEFLATED, 31)`: a gzip container with mtime 0, no name, XFL 4.
14. Cap: `raw > cap` fails; exactly `cap` passes (tested both sides of the boundary).
15. A broken pipe to age is `age_failed` even when age exits 0.
16. After age exits, `--out` must still be the inode this run created, a regular file, mode 0600, size > 0; fsync, then size + sha256 by `pread`.
17. **SIGTERM/SIGINT/SIGHUP** (an Ansible timeout): kill tar and age, remove `--out`, report `refused`/`tar_failed`. No new code was added for this; repeat signals are ignored during cleanup.
18. An unexpected internal error is reported as `refused` with the code of the step that was running; never a traceback.
19. Shebang `#!/usr/bin/python3`, mode 0755 like the other helpers. The docstring tells B-109b to run it with `python3 -I` from a root-owned directory (so nothing next to it can shadow a stdlib module).
20. CI `dpu-scripts`: a single 3.10 run (no matrix), `timeout-minutes: 15`, `pip install pytest` unpinned as specified, `-rf` like the `test` job.
21. `tests/dpu/conftest.py` carries its own copy of the `requires_age` gate (stdlib + pytest). In the main suite both copies run and agree.
22. No `tests/dpu/__init__.py` (not in the file list, and not needed: `tests/dpu/conftest.py` imports as `conftest`, `test_capture` is a unique name).
23. The S-shaped canary is built at run time (`"AGE-SECRET-KEY-1" + "Q" * 58`), so the full key pattern isn't in the source and the full-key search over the patch stays clean.
24. The first baseline run was invalid (system ansible 2.10.8 on PATH); it was re-run with `.venv/bin` first on PATH, and every later run pins PATH the same way (§4).

## 3. Interface (final; for B-109b / Rama)

```
python3 -I daolab-capture.py --home /home/lab --skel /etc/skel --owner-uid <lab uid>
    --recipient <age1 + 58 bech32> --out /var/lib/daolab/capture/<public_id>.age
    --cap-bytes 536870912 --age /usr/local/bin/age
    [--mountinfo /proc/self/mountinfo] [--sysctl-root /proc/sys]
stdout: {"status": "saved"|"empty"|"refused", "reason": <code>, "raw_bytes": n, "age_bytes": n, "age_sha256": hex}
stderr: nothing.  exit: 0 saved/empty, 1 refused.
codes:  ok, empty_home, path_unsafe, home_missing, home_not_dir, home_owner, mount_under_home,
        sysctl_unsafe, tar_failed, cap_exceeded, age_failed, out_exists, bad_args
```
Changes against the prompt: **`--owner-uid` (required), `home_owner`, `bad_args`.**

## 4. Results

Every run had the venv's `bin` first on PATH and `DAOLAB_REQUIRE_AGE=1`. Each log starts with the rule-6 header (date, HEAD, write-tree, venv, python, ansible-core, the `ansible-playbook` actually on PATH, sqlite, tar, age, command).

| Stage | Log | Interpreter / ansible-core | Result |
|---|---|---|---|
| `--confcutdir` proof | `logs/01-confcutdir-proof-py310.log` | 3.10.12, pytest only | 107 collected; the only conftest registered is `tests/dpu/conftest.py`; `tests/conftest.py` mentioned 0 times; `dotenv` not installed |
| Baseline at 65e47fc (`/tmp` worktree) | `logs/00-baseline-65e47fc-venv.log` | 3.12.13 / 2.21.6 | **1711 passed, 4 skipped, 0 failed** (17 min; skips: playwright, 3 × sshd) |
| (invalid first baseline) | `logs/00-baseline-INVALID-system-ansible-2.10.log` | system `ansible-playbook` 2.10.8 on PATH | 52 failed (`end_role` unknown to 2.10). My setup error, not the base; kept for the record |
| Red (tests, no script) | `logs/02-red-venv.log`, `logs/02-red-py310.log` | 3.12.13 / 3.10.12 | **107 failed** each, all for the missing script: 105 "daolab-capture.py is not written yet", 1 `os.access(X_OK)`, 1 SIGTERM test (no `--out` appeared; it now asserts the script first) |
| Green | `logs/03-green-try1-*.log`, then `logs/05-green-*.log` after the test fix in §5 | 3.12.13 / 3.10.12 | **107 passed** each |
| Final A: full suite | `logs/10-final-A-venv-full.log` | 3.12.13 / 2.21.6 | **1818 passed, 4 skipped** (= 1711 + 107) |
| Final B: 19 files that mention ansible + `tests/dpu` (list in `logs/B-files.txt`) | `logs/10-final-B-venv220-ansible-and-dpu.log` | 3.12.13 / 2.20.11 | **930 passed, 1 skipped** |
| Final C: `tests/dpu --confcutdir` | `logs/10-final-C-py310-dpu.log` | 3.10.12 | **107 passed** |
| Patch / tree | `B109A-capture.patch`, `TREE.txt` | — | 4 files, +1325/−0; tested tree `baa80123121d485e496589701a8d5854b126d6af`; fresh `/tmp` worktree at 65e47fc + `git apply --index` → **TREE MATCH**; `git diff --cached --check` clean on both; the script keeps 100755 |

Timing (informational, laptop, 64 MiB random tree, the whole capture): 36.2–38.7 MB/s on 3.12, 30.1–30.8 MB/s on 3.10.

**Log defect (mine):** run A's header has an empty `write-tree:` line. A, B and C started together and each wrapper ran `git write-tree`; A lost the race for `index.lock`. The tree was computed once beforehand (`TREE.txt`), B and C recorded the same `baa80123…`, and after the runs `git write-tree` = `baa80123…` with worktree == index. The log was left unedited.

**Secret checks (counts only, nothing printed):** full S-shaped key pattern in the patch = 0 (also case-insensitive); the `AGE-SECRET-KEY` prefix is on 1 patch line (the runtime-built canary). In the logs, `02-red-venv.log` and `02-red-py310.log` each contain the fake canary exactly once (pytest's failure summary repeats the test's argument), with 0 key-shaped strings that aren't the canary; the final logs contain 0. The test identities lived only in pytest tmp files and appear in no log. Local home paths in the patch: 0. The user's address in the patch: 0 (the 21 "email-like" hits are `@pytest.…` decorators).

**Left behind:** `/tmp/b109a-apply` (the tree-match worktree, detached at 65e47fc with the patch staged). Removing it needs `git worktree remove --force`, which rule 3 forbids, so it's for Akshay. The baseline worktree `/tmp/b109a-base` was clean and has been removed. The scratch mutant copies are in the session scratchpad only.

## 5. Mutation check (extra evidence, `logs/04-mutants-venv.log`)

22 deliberate bugs, each in a scratch copy of the script and tests (the repo was never written). 20 are caught. The first round found one real gap: without `--numeric-owner` the listing still looked numeric. Fixed by asserting empty `uname`/`gname` in the tar headers; that mutant is now caught. The 2 survivors don't change behaviour:
- **M12**, the `_feed` broken-pipe handler swallowing errors: `age.stdin.close()` raises the same broken pipe, so the result is still `age_failed` (the handler is a second line of defence).
- **M21**, the explicit forbidden list removed: every entry is top-level, so the 2-component rule refuses them anyway (the list stays because the spec names it).

## 6. Risks and notes for later tasks

- **Throughput:** gzip -1 in pure Python on random data measured 36 MB/s (3.12) and 30 MB/s (3.10) on the laptop, single-threaded. The board's `tar | age` alone was ~375 MiB/s, so gzip now dominates: a 512 MiB random capture on the ARM board could take roughly 20–60 s. To measure in Phase 9 (E-8, B-114).
- **No internal timeout:** a tar blocked on a hung read blocks the script. B-109b's task timeout bounds it, and SIGTERM cleans up (tested).
- **As root:** the round-trip test's `chown 0:0` / `4242:4242` branch and the "file owned by someone else is still captured" path only run as root. CI and the laptop are non-root, so E-6 is covered here only by numeric-owner headers. Suggest one root run in B-114 or on the board. The unreadable-file test is skipped as root (root reads everything).
- CI's `ubuntu-latest` has GNU tar 1.35; the board and the laptop have 1.34 (all local runs used 1.34).
- **For B-109b:** create `/var/lib/daolab/capture` as root 0700; pass `--owner-uid` of `lab`; run `python3 -I`; treat a missing or unparsable JSON line as a failure; delete `--out` after the fetch; keep the home and quarantine on anything but `saved`/`empty`.
- E-15's "temp/rename write path" is the controller's `snapshot_root` (E-15 §4.5, B-109c), not this DPU staging file.
