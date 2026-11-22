# Part A results: controller = Akshay's laptop (akshay-laptop), 22 Nov 2026

Outputs as Akshay pasted them, condensed. Numbers, ids, versions and modes only; file names are omitted per the review.
These are laptop results. **No other host has been audited.** The production DB audit stays OPEN until a host that serves other people is identified.

## A1 / A1b: patches on the controller
| Check | Result |
|---|---|
| Base | `git rev-parse HEAD` = `40ebd6dcc8877632e0270155cf5dfa797d1012dc` |
| sha256 `P-0-final.patch` / `F-5-1.patch` / `P-0c.patch` (v1) / `F-5-1b.patch` | `cc3b42cf…` / `0c06d643…` / `82cdf3f9…` / `823a54bc…` (all match) |
| `git apply` in order | ok, `git status --short | wc -l` = 13 (before F-5-1b) |
| Python / ansible-core / ansible-runner | 3.12.13 / 2.21.6 / 2.4.3 |
| `vm_name_ok` smoke | `True False` |
| F-5-1 tests / P-0c v1 tests / all gate tests | 25 passed / 35 passed / 323 passed |
| Full suite (P-0 + F-5-1 + P-0c v1) | 1201 passed, 4 skipped (sshd), 0 failed |
| F-5-1b tests | 4 passed |

## A2: stored-data audit (`audit_stored_inputs.py`, sha256 `0f2c8f5f…`)
| DB (copy) | user_version | users | reservations | jobs | exit |
|---|---|---|---|---|---|
| `daolab.db` | 10 | 3 | 1 | 2 | **0** |
| `var/showcase.db` | **13** (reverted B-104) | 3 | 8 | 16 | **0** |

All five checked columns reported `0 row(s)` on both. `showcase.db` was moved aside, unchanged, as `showcase.db.v13-reverted-b104`.

## A3: XO VM names
| Pool | tagged `daolab-r*` | rejected by `vm_name_ok` |
|---|---|---|
| `6769f0a6…` | 0 | 0 |
| `d51166df…` | 0 | 0 |

XO is local (podman, 127.0.0.1:8443, 5.211.1). The token file is mode 600, 43 bytes.
**Still UNKNOWN:** who has access to these XCP-ng pools (the "user-controlled" column).

## F-5-1 exposure on the laptop
- `var/showcase-jobs`: 17 job dirs. **1563 files** contain `DAOLAB_INTERNAL_SECRET`: 1540 `artifacts/<run>/command`, 23 `env/extravars`. Counted with `grep -rlF -f <(…)`; the value was never printed. `var/` is gitignored.
- `command` env: 125 variables. The secrets among them are `DAOLAB_INTERNAL_SECRET`, `DAOLAB_AUTHZ_SECRET` and `DAOLAB_TOKEN`. The rest are paths, settings and local URLs with no userinfo or query; `DAOLAB_WEBHOOK_URL` is empty.
- `.env` was mode 664 and is now 600. `git check-ignore` hits `.gitignore:228`, and `git log --all -- .env` is empty: never committed.

## A4: facts
**Controller:**
- OS: Ubuntu 22.04.3, kernel 6.8.0-149
- `/run`: tmpfs (rw,nosuid,nodev,noexec), 772M; `/dev/shm` 3.8G
- swap: `/swapfile`, 2G, 787M used, **not encrypted** (no dm-crypt devices)
- age: not installed
- `DAOLAB_RUNTIME_DIR`: not set
- `SLOT_MIN` / `CLEANUP_MIN` / `JOB_TIMEOUT_SEC`: 30 / 10 / 1800

**DPU (xeon3-oct-112, 10.68.0.190):**
- OS: Ubuntu 22.04.5, Linux 6.1.67 aarch64
- Tools: GNU tar 1.34, sudo 1.9.9, Python **3.10.12**
- age: not installed; `dpdk-devbind.py`: `/usr/bin`
- `lab` user: yes

**`/home/lab`:**

| Fact | Value |
|---|---|
| Filesystem | `/dev/root` ext4 (the root fs), 234G total, 176G free |
| Submounts | 0 |
| Owner / mode | lab:lab 750 |
| Size | 17,990 bytes |
| Regular files | 6 |
| Files not owned by `lab` | 2, both root:root, type f, mode 644, depth 1, 2026-11-20, 4000 and 1000 bytes. ML inference output from a manual sudo run (not the platform). |
| Files with link count > 1 | 0 |
| `fs.protected_hardlinks` / `fs.protected_symlinks` | 1 / 1 |
