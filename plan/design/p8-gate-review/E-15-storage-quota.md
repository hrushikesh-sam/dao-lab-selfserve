# E-15 — Snapshot storage and quota (UNSIGNED DRAFT)

Date: 2026-11-24. Status: **OPEN / PROPOSED**; not signed into errata v1.2 and not implemented.

Sources: the confirmed PR #11 answer, `plan/design/p8-impl/B-104-DDL.md` §4.1, §7 Q-21 and §11, `plan/design/p8-gate-review/p8-design-errata-v1.2.md` E-15 and its E-8 later decision, and `plan/design/p8-gate-review/snapshot-design.md` §8. The older research report and local task breakdown are outside the repo and are not cited as resolvable paths. Labels: **CONFIRMED**, **DECIDED**, **SIGNED OFF**, **PROPOSED**, **REFUTED**, **OPEN**, **UNVERIFIED**.

The research report predates merged PR #10. Where it says a flow is only proposed, B-104 now overrides it: restore S via tmpfs ticket and SSH stdin is **SIGNED OFF** in B-104 §4.1. PR #11 confirmed Q-E15 and compression: transient root-only ciphertext staging on the DPU and `gzip -1` before `age` are **DECIDED**. Where the report assumes a DB size column, B-104 §7 Q-21 says there is none in v1; that conflict is recorded below.

## Decision needed

E-15 still needs decisions on controller peak sizing and size accounting before implementation. DPU ciphertext staging and compression format are **DECIDED**:

1. **Peak sizing:** how much controller storage is provisioned, guarded, capped and monitored.
2. **Size accounting:** `hdr.json` only vs an additive DB column, because B-104 Q-21 has no DB size column in v1.

## 1. DPU staging

| Claim from the report | Label | Current basis |
|---|---|---|
| Snapshots store each person's CLI history persistently in the DPU | **REFUTED (design)** | Persistent archives live on the controller under `snapshot_root`. The DPU holds only the live `/home/lab` for the current slot. |
| `.bash_history` is captured | **CONFIRMED** | It is included as user data inside the age-sealed payload. |
| The DPU will fill up as members go 10 to 30 | **PARTIAL / mostly REFUTED** | Live home is per slot, not per member. The original design's DPU tmpfs tar, 7-day unsaved stash and DPU restore copy are superseded. |
| Persistent snapshots belong on the controller | **CONFIRMED (design)** | With the caveat that the controller path must be a dedicated mount, not the OS disk. |

**SIGNED OFF / CONFIRMED from B-104:**

- Persistent archives live only under controller `snapshot_root`; the DPU holds only the live `/home/lab` during a slot.
- **DECIDED (Q-E15):** transient root-only ciphertext staging on the DPU is allowed: `/var/lib/daolab/capture/<public_id>.age` before fetch and `/var/lib/daolab/restore/<public_id>.age` during restore; dirs 0700, files 0600, deleted immediately after use, never plaintext.
- **DECIDED (compression):** capture is `tar -> count raw bytes against the 512 MiB cap -> gzip -1 -> age` inside `daolab-capture.py` using Python 3.10 stdlib zlib/gzip level 1. The cap counts the raw tar stream before compression.
- Capture uses only S's public recipient. The private S never goes to the DPU at capture.
- At restore, the CLI unwraps S locally and sends it to the API; the controller reads the tmpfs ticket and streams S over SSH stdin into `age -d -i -` on the DPU. Restore then runs `age -d -> gunzip -> tar` in both restore passes. Any `hdr.json` `compression` value other than `"gzip"` is refused.
- There is no plaintext staging, no DPU unsaved stash, and no S in env, argv, extravars, job dirs, logs or the DB.
- Capture and wipe run as root; restore preserves ownership. Root-owned files in `/home/lab` are user data, not a tripwire class.
- DPU-side scripts stay Python 3.10 stdlib only or pure shell.

**PROPOSED for E-15:**

- Keep `.bash_history` in the sealed payload as user data. It is encrypted to the owner only, adds negligible size, and restoring a session's history is expected.
- If a future setting excludes it, that must be an explicit owner choice and not a default.
- No same-filesystem bind mount under `/home/lab`; capture refuses with `home:lab` and quarantines.

**REJECTED / SUPERSEDED:**

- Persistent per-member snapshot archives on the DPU.
- A 7-day plaintext DPU stash for failed captures.
- Writing S or plaintext home data to DPU tmpfs files.
- Any DPU tmpfs staging of plaintext home data or S.

## 2. Four stores, do not conflate

| Store | Holds | Growth | Backed up? | Secrets? |
|---|---|---|---|---|
| DPU live `/home/lab` | Current user's working files only, during their slot | Bounded by one slot; wiped after sealed save or verified empty | No | Plaintext user data, inherent to a shared board |
| Controller `snapshot_root` | `u<id>/<public_id>.age`, `.key.age`, `.hdr.json` | Up to 3 x per (user, setup) x 512 MiB | **No** in v1 | Ciphertext only; no S in clear |
| SQLite DB | IDs, states, timestamps, fingerprints; **no size column in v1** under B-104 Q-21 | KBs per snapshot | Yes (`backup-db.sh`) | No S, key material or file paths |
| tmpfs restore tickets | One S per active restore, 0600, 5 min TTL | A few hundred bytes; swept if orphaned | No | S in clear, transient; swap-off/encrypted swap required by B-107 |

## 3. Peak sizing

Worst case = `512 MiB x 3 snapshots x users x setups`. The safe upper bound uses raw tar size deliberately: compression is `gzip -1` before `age`, but the 512 MiB cap is enforced on the raw stream before compression.

| Members | 1 setup | 2 setups | 3 setups |
|---|---:|---:|---:|
| 10 | 15 GiB | 30 GiB | 45 GiB |
| 20 | 30 GiB | 60 GiB | 90 GiB |
| 30 | 45 GiB | 90 GiB | 135 GiB |

For the current one-setup lab, 30 members means a **45 GiB worst case**.

**Realistic sample (reported, not proven):** one home was 17,990 bytes across 6 files. At 1,000x that is about 18 MB, so 30 members x 3 x 1 setup is about 1.6 GB; at the observed size it is about 1.6 MB. Real build trees and logs are the unknown, so the cap, not the average, must drive the design.

**Metadata overhead (estimated, UNVERIFIED):** `key.age` <= 1-2 KiB and `hdr.json` <= 4 KiB, so <= 8 KiB per snapshot. About 90 snapshots is under 0.7 MiB.

**Headroom proposal:** provision at least `1.25 x` the accepted worst case and set a global cap below it. For 30 members and one setup: **64 GiB volume**, **48 GiB global cap**, min-free reserve = one snapshot cap plus 10%. A 20-30 GiB volume is plausible at today's sizes but cannot be proven; only the guard makes it safe.

Existing timing knobs from the report: `DAOLAB_SLOT_MIN=30`, `DAOLAB_CLEANUP_MIN=10`, `DAOLAB_JOB_TIMEOUT_SEC=1800`.

## 4. Controller storage proposal

1. **Dedicated persistent mount:** own LV, partition or volume, not the dashboard OS disk. A full OS disk would take down SQLite WAL, logs, API and engine at once; a separate filesystem makes "snapshots full" a snapshot-only failure. Mount `nodev,nosuid,noexec`.
2. **Mount check:** with snapshots enabled, refuse to start unless `snapshot_root` is an actual mount point (`os.path.ismount`), so a missing mount cannot silently write to the root disk.
3. **Ownership and permissions:** service user only; `snapshot_root` 0700; `u<user_id>` 0700; files 0600. Never under a repo, web root, job/artifact dir, or the OS disk.
4. **Free-space guard before capture:** refuse capture unless `free >= DAOLAB_SNAPSHOT_MAX_MB + DAOLAB_SNAPSHOT_MIN_FREE_MB`. The reserve is one cap plus 10% of the volume.
5. **Write path:** same-filesystem `*.tmp`, enforce the byte cap on the raw tar stream, pass through `gzip -1` before `age`, `fsync`, then rename. Delete temp files on any failure; sweep stale temps at startup.
6. **Quotas:** per-snapshot cap 512 MiB raw tar; KEEP=3 per (user, setup); proposed per-user total `DAOLAB_SNAPSHOT_USER_MAX_MB` default 1536 MiB; proposed global cap `DAOLAB_SNAPSHOT_ROOT_MAX_MB`.
7. **Pruning:** only after the new snapshot is sealed and committed `saved`; prune only the owner's oldest unheld snapshots beyond KEEP or the per-user quota. "Unheld" means no deletion hold and not the source of a running restore. Never delete another user's data to make room.
8. **Fail closed:** on capacity or quota failure, skip or abort capture, keep the home, raise `home:lab`, quarantine, no wipe, no plaintext stash, row `failed` with a safe reason code (`storage_low`, `quota_user`, `quota_global`).
9. **Booking-time check:** refuse snapshot-enabled booking when free space is below the reserve (proposed safe error `snapshot_storage_low`). The slot-end guard is the backstop; the booking check keeps the lab usable because a quarantine blocks the next user.
10. **Monitoring:** one safe line per capture and per tick: `event=snapshot_capacity state=ok|warn|refuse used_mb=N free_mb=N snapshots=N users=N`. No user names, paths, file names or key material. Warn at >=70% of the global cap, refuse at >=90%. Show only counts and MiB on the admin dashboard.
11. **No snapshot backup:** `snapshot_root` is not backed up in v1; `backup-db.sh` covers SQLite only.

## 5. Metadata conflict to resolve

B-104 §7 Q-21 says **no snapshot metadata column in the DB in v1**: size, `.age` sha256, `hw_class`, use case and device profile live in `hdr.json` and `daolab-profile/1`; the `compression: "gzip"` decision is recorded in errata v1.2 E-8's 24 Nov decision. The research report recommends DB-recorded sizes for quota accounting.

E-15 cannot assume `snapshots.size_bytes` exists. Sign-off must choose:

- **Option A:** account from `hdr.json` only; no schema change, but parsing is slower and must be safe.
- **Option B:** add an additive migration for a size column; needs a new task/migration after B-104 and fresh sign-off.

Until that choice is made, quota implementation is OPEN. The config names and defaults in §4 are proposals, not decisions.

## 6. Minimal implementation mapping

The report's B-task numbers were UNVERIFIED. The signed §8 task table now gives the mapping:

- B-107: ticket directory, swap check and startup guard context.
- B-108a: snapshot-enabled booking refusal and admin-visible capacity numbers.
- B-109a/B-109c: capture mount check, raw-stream cap, in-script `gzip -1`, free-space guard, temp/rename write path, `home:lab` and quarantine.
- B-110: `age -d -> gunzip -> tar` in both restore passes, refusal of any `compression` value other than `"gzip"`, quota accounting, prune logic, restore-source exclusion and capacity failure handling.
- B-114: synthetic 30-user, capacity, mount, compression and booking acceptance tests.

No code, dependency or schema change is part of this draft.

## 7. Acceptance tests

No test should allocate 45 GiB.

1. **Synthetic 30-user test:** create 30 users x 3 snapshots with recorded or header-derived sizes and patched `disk_usage`; assert 45 GiB accounting, 91st snapshot hits global cap, and the per-user quota blocks the 4th while pruning only that user's oldest.
2. **Guard:** patched free space below reserve -> capture refused, home kept, `home:lab` raised, setup quarantined, no wipe, no stash.
3. **Stream cap:** a small raw byte cap with a larger generated stream -> `cap_exceeded`, temp removed, row `failed`, no partial `.age` visible.
4. **Prune safety:** new snapshot must be `saved` before old deletion; held rows and restore sources are never pruned; another user's snapshots are never touched.
5. **Mount check:** non-mount `snapshot_root` -> feature refuses to enable.
6. **Booking check:** low free space -> snapshot-enabled booking refused with a safe error.
7. **Logs:** capacity lines match the safe shape and contain no names or paths.
8. **Compression:** `hdr.json` with `"compression": "gzip"` restores through `age -d -> gunzip -> tar` in both passes; any other value is refused; raw cap is measured before gzip.
9. **Real board:** a 512 MiB capture fits `DAOLAB_CLEANUP_MIN` in Phase 9.

## 8. OPEN items and contradictions

- The external snapshot design draft says capacity is "sized in `plan/08`"; `plan/08` has no disk/capacity sizing.
- Original design's DPU tmpfs staging, DPU unsaved stash and DPU restore copy are superseded; mark them as such so they are not reintroduced.
- Number of setups to size for.
- Real home sizes beyond one reported sample.
- Size accounting: `hdr.json` only vs additive DB column.
- Config names and defaults.
- Restore-over-ssh is now signed off in B-104; the report's OPEN status on that point is resolved.
- Compression is decided: `gzip -1` inside `daolab-capture.py`, raw cap before compression, `hdr.json` field, `gunzip` in both restore passes.
- Production capacity baselines remain Rama's decision; synthetic baselines only for tripwire.

## 9. Future MongoDB appendix (forward compatibility only, no change now)

- Keep identifiers opaque and stable: `public_id`, `user_id`, setup/hw_class, `key_id`/fingerprint and `snapshot_id` in `hdr.json`. Never derive them from paths or file contents.
- Secrets stay file-only: S and identities live only in age-wrapped `key.age`, tmpfs tickets and the client. A document store would hold identifiers and states, never key material.
- Quota/retention mapping would be a snapshot document plus per-user and global counters; "unheld oldest" becomes an index on `(user_id, setup, state, created_at)`.
- No code, dependency or schema change now.
