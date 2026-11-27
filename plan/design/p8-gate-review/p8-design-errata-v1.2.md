# P8 workspace snapshots: design errata v1.1 → v1.2 (SIGNED OFF by Rama, 23 Nov 2026; E-15 UNSIGNED DRAFT)

Collected during the Phase 8 gate, 22–23 Nov 2026, from facts measured on the laptop controller and on xeon3-oct-112.
Rama's decisions of 23 Nov are recorded in the **Status / decision** column. Where he amended or rejected the proposal, his wording governs.

Status key: **SIGNED OFF / AGREED / DECIDED / CLOSED / OPEN** as recorded in each row; **UNSIGNED DRAFT** = recorded for visibility but not signed into v1.2.

## Authority

- This file records the v1.2 decisions signed off by Rama on 23 Nov 2026. The E-1..E-14 wording below is copied from the signed errata; later decisions are recorded separately with their dates.
- `design:NN` citations refer to `origin/akshay-p8-gate:plan/design/p8-gate-review/p8-snapshot-design-v1.1.md` line NN in the gate evidence bundle.
- The Phase 8 task table called for by B-104 §0 R-1 is `plan/design/p8-gate-review/snapshot-design.md` §8.
- Storage/quota sizing is not in v1.2; E-15 below is an UNSIGNED DRAFT and the full draft is `plan/design/p8-gate-review/E-15-storage-quota.md`.

## Errata

| # | Topic | v1.1 says / assumes | Found | v1.2 (as decided) | Status / decision |
|---|---|---|---|---|---|
| E-1 | age source and version | "age CLI v1.1.1 only" | Not installed on either host; Ubuntu 22.04 apt has no v1.1.1; the DPU is aarch64. | Official GitHub **age v1.1.1** release assets, **pinned for linux-amd64 and linux-arm64 by hash**, plus the internal binary hashes. No apt. | **AGREED.** Checksum publication is evidence, not a blocker. |
| E-2 | Restore tickets on tmpfs | S lives "only in tmpfs" ≤ 5 min | The laptop has a plain unencrypted `/swapfile` (787M in use) and no dm-crypt. tmpfs can be swapped out, and `mlock` doesn't cover tmpfs files. | **Controller prerequisite:** swap off, encrypted swap, or zram. **Startup refuses otherwise.** | **AGREED.** The laptop stays dev / fake-lab only. |
| E-3 | Python floor (F-5-3) | Python ≥ 3.12 | Controller 3.12.13; **DPU 3.10.12**. | **3.12 is the controller floor only.** DPU-side scripts are Python 3.10 stdlib-only, or shell, **with a 3.10 test**. | **AGREED.** The 3.10 test is deferred to Phase 8. |
| E-4 | Migration numbering (B-104) | New migrations 12, 13, 14 | A DB that ran the reverted B-104 sits at `user_version` 13, and `SchemaTooNew` doesn't catch it. | **Burn 12 and 13; B-104 starts at 14.** Add a **preflight guard against the reverted v12/v13 signatures.** | **SIGNED OFF.** |
| E-5 | How S reaches the DPU at restore | Not specified (v1.1 §5 bans extravars/env/argv/job dirs) | — | **Restore stays S-to-DPU.** The controller reads S from the tmpfs ticket and **streams it over SSH stdin** into `age -d -i -` on the DPU. Ciphertext goes by file or FD. **No plaintext staging. No S in env, argv, extravars, job dirs or logs.** | **REJECTED AS WRITTEN.** The proposal to decrypt on the controller is **not** adopted. Any change to this flow needs a fresh review. |
| E-6 | Root-owned files in the home | Capture and wipe as `lab` (implied) | `/home/lab` holds 2 root:root 644 files (a user's own `sudo` ML output, 20 Nov, MLIP F14). | Root-owned files in `/home/lab` are **normal user data**, **not a tripwire class by themselves**. **Capture and wipe run as root. Restore must preserve ownership**, not re-own everything to `lab`. | **DECIDED** (as amended). |
| E-7 | Home on the root filesystem | `-xdev` separates the home | `/home/lab` is on `/` (ext4): 0 submounts, 0 hard-linked files, `protected_hardlinks` = `protected_symlinks` = 1. | Keep `-xdev`. **Enforce `fs.protected_hardlinks=1` and `fs.protected_symlinks=1`** (else refuse to capture). Baseline for `sdp-pf1-vf8`: submounts = 0. | **AGREED.** |
| E-8 | Capture size and timing | 512 MiB cap within `DAOLAB_CLEANUP_MIN` | Home 17,990 B / 6 files; cleanup 10 min, job timeout 1800 s. | No change. The Phase 9 timing test uses a synthetic 512 MiB tree. | **AGREED / closed.** |
| E-9 | Admin count vs the two-admin rules | Two-admin approvals, 24 h holds | **More than 3 admins.** | The rules hold as written. | **Closed.** |
| E-10 | Secrets in the process environment | §5 bans secrets in extravars/env/argv/job dirs | `load_dotenv()` puts secrets in `os.environ`, and ansible-runner writes the environment into `artifacts/<run>/command` (F-5-1b). systemd `EnvironmentFile=` leaves them in `/proc/<pid>/environ`. | **F-5-1b is mandatory now.** Add a **CI rule: every secret-like setting is listed in `SECRET_ENV`.** **File-only secrets** (`*_FILE` / systemd `LoadCredential=`) are accepted for v1.2 and **must be used by new Phase 8 code**. | **DECIDED.** F-5-1b and the SECRET_ENV rule (G-3) are in PR #7. |
| E-11 | Orphan sweep: VM names | Prefix filter `^daolab-r[0-9]+-` | Row 25 RCE; P-0e (an unbounded id crashes the sweep). | Combined P-0c (bounded name pattern) **and P-0d (destroy by XO VM id only)**. Both are signed off. | **AGREED.** Both are in PR #7. |
| E-12 | Who can name a VM | Not stated | XO mirrors pool VM names: anyone with XCP-ng pool access (XO, XCP-ng Center, `xe`) can set one. | **Document pool access in the threat model**, and ask the pool admin who has XCP-ng pool access. | **OPEN, non-blocking.** |
| E-13 | Legacy home disposition (D-7-5) | Per-setup decision | tb-01: 6 files, the 2 foreign files are a known user's test output. | For **tb-01, treat the current home as the user's data.** The one-time decision at enable **defaults to preserve (`save_snapshot=0`)**; **wipe only with explicit two-admin approval.** | **DECIDED** (as amended). |
| E-14 | Exact DDL | — | — | Written with **B-104, starting at migration 14** (E-4). | **OPEN** until the DDL is written with B-104. |
| E-15 | Storage and quota | Not in v1.1 | Unsigned research draft from the Phase 8 gate. | Persistent snapshots live only on the controller's dedicated `snapshot_root`, never on the DPU; the DPU holds only the live `/home/lab`. 512 MiB raw cap x 3 per (user, setup) gives ~15 GiB for 10 users and ~45 GiB for 30 on one setup. Require free-space/headroom checks, per-user and global quotas, pruning of the oldest unheld saved snapshots, fail closed plus quarantine on insufficient space, and a synthetic 30-user capacity test. Peak is 4 per (user, setup) briefly while the new snapshot is written before the prune, plus held rows that retention does not count (Q-16), so size headroom for the peak. Transient root-only ciphertext staging on the DPU was decided in PR #11 on 24 Nov 2026 (Q-E15); the rest of E-15 remains UNSIGNED DRAFT. Owners: headroom B-109c, quotas B-108a, capacity test B-114. | **UNSIGNED DRAFT.** Not signed into v1.2; tracked in `plan/design/p8-gate-review/E-15-storage-quota.md`. |

## Later decisions

- **24 Nov, E-1:** X-AGE landed, PR #11 (`7a2dbad`).
- **24 Nov, E-8:** capture uses gzip -1 before age; the cap is on the raw tar; `hdr.json` records `"compression": "gzip"`; restore refuses any other value.
- **24 Nov, E-14:** CLOSED by PR #10 (DDL v4.1).
