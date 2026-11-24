# P8 workspace snapshots: design errata v1.2

Date: 2026-11-24. Status: **v1.2 record**; items marked SIGNED OFF were decided by Rama, 23-24 Nov 2026. Supersedes conflicting v1.1 text.

Status key: **AGREED** = agreed in review; **SIGNED OFF** = decided by Rama; **PARTLY SIGNED OFF** = the main rule is signed but a named sub-decision remains OPEN; **DECIDED** = settled in the signed design/DDL; **CLOSED** = no action remains; **OPEN** = needs a later decision; **UNVERIFIED** = claim not proven here.

## Authority

- The signed note cited as `~/notes/p8-design-errata-v1.2.md` was not present in this repository or any fetched branch.
- This file records the v1.2 decisions as signed off in `plan/design/p8-impl/B-104-DDL.md` §0, §7 and §11, with B-104 as the task authority.
- `design:NN` citations refer to `plan/design/p8-gate-review/p8-snapshot-design-v1.1.md` line NN in the gate evidence bundle.
- The Phase 8 task table called for by B-104 §0 R-1 is `plan/design/p8-gate-review/snapshot-design.md` §8.
- Storage/quota sizing is not in v1.2; it remains an OPEN follow-up.

## Errata

| # | Topic | v1.1 says / assumes | Final v1.2 decision | Status |
|---|---|---|---|---|
| E-1 | age source and version | age CLI v1.1.1 only | Use official GitHub v1.1.1 release assets pinned by hash for linux-amd64 and linux-arm64, plus the lock cross-check and internal binary hashes. No apt. Whether v1.1.1 publishes its own checksums remains UNVERIFIED. X-AGE landed the installer and CI check in PR #11 (`7a2dbad`). | AGREED / LANDED |
| E-2 | Restore tickets on tmpfs | S lives only in tmpfs for at most 5 minutes | tmpfs can swap, so controller startup refuses unless swap is off, encrypted, or zram. Laptop stays dev/fake-lab only. The check belongs in B-107 (R-5, B-104 §9 N-6). | SIGNED OFF |
| E-3 | Python floor | Python >= 3.12 | The controller floor is Python >= 3.12. DPU-side capture/restore shell and tripwire checker run on Python 3.10 with stdlib only, or pure shell; add a 3.10 test. | SIGNED OFF |
| E-4 | Migration numbering | New migrations 12, 13, 14 | Burn 12 and 13. B-104 is one migration 14 with a preflight guard in `migrations.apply()`; a reverted DB at `user_version` 13 is refused, not silently reused. B-104 §7 Q-1 closes E-14. | SIGNED OFF |
| E-5 | How S reaches the DPU at restore | Not specified; v1.1 §5 bans extravars/env/argv/job dirs | CLI unwraps S locally and sends it to the API; API writes a 0600 single-use tmpfs ticket; controller reads and unlinks it, then streams S over SSH stdin into `age -d -i -` on the DPU. No plaintext staging and no S in env, argv, extravars, job dirs or logs. A change needs fresh review. This supersedes the earlier draft wording that said S never reaches the DPU. | SIGNED OFF |
| E-6 | Root-owned files in the home | Capture/wipe run as lab (implied) | Capture and wipe run as root; restore preserves ownership. Root-owned files in `/home/lab` are user data, not a tripwire class. | SIGNED OFF |
| E-7 | Home on the root filesystem | `-xdev` separates the home | Keep `-xdev`. Capture refuses unless `fs.protected_hardlinks=1` and `fs.protected_symlinks=1`. Baseline for hw_class `sdp-pf1-vf8`: submounts = 0. | PROPOSED |
| E-8 | Capture size and timing | 512 MiB cap; must fit cleanup | No change to the raw cap. Capture runs `tar -> raw cap -> gzip -1 -> age` inside `daolab-capture.py`; `hdr.json` records `compression: "gzip"` and restore runs `age -d -> gunzip -> tar`, refusing any other value. Phase 9 timing test uses a synthetic 512 MiB tree, not a real home. | DECIDED |
| E-9 | Admin count vs two-admin rules | Two-admin approvals, 24h holds | More than 3 admins exist; the two-admin rules hold as written. | CLOSED |
| E-10 | Secrets in the process environment | §5 bans secrets in extravars/env/argv/job dirs | F-5-1b scrub after reading is mandatory. File-only secrets (`*_FILE` settings or systemd `LoadCredential=`) apply to new Phase 8 code; CI enforces `SECRET_ENV`. `reservations.access_password` is an existing column and needs a separate decision (B-104 §10). | SIGNED OFF |
| E-11 | Orphan sweep VM names | Prefix filter `^daolab-r[0-9]+-` | Use the combined stricter name filter from P-0c in source, engine and runner; P-0d destroy-by-XO-id is required before Phase 9. | AGREED |
| E-12 | Who can name a VM | Not stated | Anyone with XCP-ng pool access can set VM names. Record in the threat model; ask the pool admin who has access. | OPEN |
| E-13 | Legacy home disposition | Per-setup decision | Default is PRESERVE: `save_snapshot=0` never means opt-out wipe. A wipe needs two-admin `home_discard`. The one-time per-setup decision at enable is still OPEN for individual setups. | PARTLY SIGNED OFF |
| E-14 | Exact DDL | OPEN | Exact DDL is the signed B-104 v4.1 document and migration 14. No B-103..B-114 row existed in `plan/10` before the §8 task table in `snapshot-design.md`. | CLOSED |

## Non-blocking follow-up

- Storage/quota sizing was researched separately. It is not signed into v1.2; the unsigned draft is `plan/design/p8-gate-review/E-15-storage-quota.md`. It records the confirmed Q-E15 root-only DPU ciphertext staging and the gzip compression decision, but peak sizing and size accounting remain OPEN.
