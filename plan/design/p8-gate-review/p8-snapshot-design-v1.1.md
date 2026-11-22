# Snapshot design v1.1 (p8)

Repo `dao-lab-selfserve`, HEAD `40ebd6dcc8877632e0270155cf5dfa797d1012dc`, branch `rama`. Source: `snapshot-design.md`.
Static document: no runtime claims. Patches are drafts; operator runbook results are UNKNOWN. Nothing is committed. B-103 and snapshot implementation are not started.
Status labels: OPEN, SIGNED OFF, RESOLVED, DECIDED, CONFIRMED, UNVERIFIED, REJECTED.

## 1. Version history

- **v1** (Phases 1–5): per-user workspace snapshots, age-sealed, challenge-gated destructive actions, tripwire residue detection.
- **v1.1** (Phase 6 corrections):
  1. F-1 defined with citations (§9).
  2. V4 = `esc()` quote bug plus inline handlers; new UI DOM-APIs only.
  3. PR #2 line review blocked (diff unavailable), not failed.
  4. Challenge binding: length-delimited hash, plaintext `target_ref`, CAS consume.
  5. Ticket carries per-snapshot identity S only (S′ removed).
  6. `save_snapshot`: migration default 0, create default 1.
  7. Tripwire accept/re-baseline needs two admins.
  8. Lost-key reset needs two admins and only starts a 24h hold.
  9. age pin source (§8).
  10. Platform floor Python ≥3.12, ansible-core ≥2.20, CI 3.12/3.13.
  11. Confirmed assumptions: admin home discard 24h/visible/owner-cancellable; a different admin activates keys; lost key = unrecoverable; V4 DOM-only.
- **Phase 7 corrections:**
  - Dependencies fixed: B-109 ← B-103, B-104, B-105; B-108 ← B-104, B-106; B-107 ← B-103, B-104.
  - Withdrawn claim: NEL/U+2028/U+2029 injecting a second authorized_keys line on the deploy path. `api.py` line splitting plus per-line matching already blocked it; only the unvalidated `users.ssh_pubkey` path was reachable.
  - **Legacy errata:** `save_snapshot=0` means PRESERVE, never opt-out wipe (§2, §6).

## 2. Scope of v1/v1.1

In scope: per-user `/home/lab` snapshot at end of slot, age-sealed; restore at next booking; key enrollment, rotation, lost-key reset; user/admin deletion with hold; admin home discard with hold; tripwire residue detection; hugepages + driver-binding replay.

**Explicitly NOT provided:**
- **Owner opt-out wipe: OUT OF SCOPE.** There is no way for an owner to choose "wipe without saving". A future opt-out would need an additive column, an active key, an explicit action and an audit event.
- **No SDP/VF replay.** VF counts are captured, replay is disabled until L2/L3 evidence exists.
- **No `snapshot_root` backup. DECIDED: no backup in v1.** `deploy/backup/backup-db.sh` covers the SQLite DB only.
- Claimed isolation is **only** `/home/lab` confidentiality plus residue detection. Not proof of cleanliness; firmware/BMC and root tampering are residual.

**Legacy `save_snapshot=0` errata (explicit):**
- `0` = preserve. Legacy rows migrate with `0`. The API stamps `1` only if the setup is snapshot-enabled **and** the user has an active key.
- Not-enabled setup → `0`, status quo, no wipe. Enabled setup, no active key → refused (`snapshot_key_required`). Explicit `save_snapshot=false` → refused.
- Engine on an enabled setup with `0` (legacy/in-flight): keep home, raise `home:lab`, quarantine. Also requires the owner's active key at capture.
- No legacy or in-flight reservation is ever wiped under snapshot mode. Drain before disabling the flag.
- Per-setup legacy home disposition: **OPEN** (§11).

## 3. Security model and isolation claim

- The API token is auth-only. A stolen token cannot download, delete, rotate, cancel-delete or discard; those need a challenge answered with the recovery identity, which stays client-side.
- Lost key ⇒ snapshots unrecoverable. Admin reset: two admins (`admin_approvals`), starts a 24h deletion hold only.
- Confidentiality: payload is an age file sealed to per-snapshot identity S; S is wrapped to the user's recipient (`key.age`). No custom crypto; age CLI v1.1.1 only, no pyrage.
- Residue detection (tripwire), part of the v1 claim. Classes: identity, sudo, ssh, sched, boot, bin, kmod, mount, proc, tmp, daolab. Checker streamed over stdin; per-`hw_class` root-only baseline `/etc/daolab/tripwire-<hw_class>.json`; safe output only `tripwire:<class>:<count>`; residue kind `tripwire` + quarantine via `_quarantine_setup`; accept/re-baseline needs two admins; audit holds class, count, baseline version only; quarantine lifts only after a clean re-run. Detection only. **Baseline content: OPEN.**
- Known weaknesses: F-5-2 (same-filesystem bind mounts defeat `find -xdev` / `tar --one-file-system`); V4 dashboard XSS (`esc()` misses `'`, used in single-quoted inline handlers).

## 4. Key model

- **The CLI generates the age identity on the user's machine.** The private half stays **only** on the user's machine. **Lost = unrecoverable** (admin reset only starts a 24h deletion hold, two admins).
- **The server stores only** the public recipient/fingerprint and the wrapped per-snapshot identity S (`key.age`). It never holds the user's private identity, and never persists S in the clear; S exists in the clear only transiently in the single-use ticket (§5 rule 3).
- Two layers: payload sealed to per-snapshot identity S via the age CLI; S wrapped to the user's recipient as `key.age`.
- **Enrollment/activation:** the CLI creates the identity and shows the fingerprint; a **different admin** activates the key. Snapshot-enabled booking with no active key is refused.
- **Rotation / replacement steps:**
  1. The CLI generates the new identity locally and requests a challenge of purpose `rotate` whose `target_ref` is the **new key's fingerprint**.
  2. The user answers the challenge (client-side, with the current identity); single-use CAS consume, same transaction as the action; a different admin activates the new key per enrollment rules.
  3. The CLI, locally, unwraps each `key.age` with the old identity and **re-wraps all `key.age` files to the new recipient**.
  4. The engine writes each updated `key.age` to a temp file and **renames** it into place (temp-write + rename).
  5. **The client keeps the old identity until the whole rotation is complete.**
- **REJECTED: token-derived keys and token-renewal re-wrapping.** Token theft would then yield decryption and rotation authority, contradicting "API token is auth-only".
- **age pin source: RESOLVED** — official GitHub release asset + independent lock cross-check + internal binary hashes. (Whether v1.1.1 publishes its own checksums: UNVERIFIED; the pin does not depend on it.) age CLI v1.1.1 only, no pyrage.
- **Challenge** (CLI-issued; age ciphertext of the nonce returned once, never stored; TTL 10 min; single-use CAS; ≤3 open per user):
  `nonce_sha256 = sha256(` for each of nonce, purpose, target_ref, key_id, user_id, expires_at: 8-byte big-endian length ‖ bytes `)`.
  Purposes: `rotate`, `delete`, `cancel_delete`, `discard_home`. `target_ref` stored plaintext. Each destructive action needs its own challenge.

## 5. Secret handling rules

Applies to the DEK/per-snapshot identity S, the user's recovery identity, challenge nonces and any key material:
1. **Never** in the database, backups, logs, audit rows, argv, extravars, environment variables, job rows or job directories.
2. **Only the age CLI** performs encryption/decryption; no other crypto code.
3. **Ticket carries S only** (no S′). Layout: tmpfs `<rand32>-<user_id>-<public_id>`, parent 0700, file 0600, TTL 5 min, single-use. API writes and stats it; engine/helper reads and unlinks it; an orphan sweeper is mandatory.
4. Recovery identity never leaves the client. Challenge ciphertext is returned once, not stored.
5. Safe diagnostics only (e.g. `tripwire:<class>:<count>`); no user file names, key comments or home paths in logs.
6. Related existing secret: `daolab_internal_secret` is passed as an extravar at `service/runner.py:501` (OPEN, F-5-1; §9). The snapshot rules above forbid that pattern for snapshot secrets.

## 6. Flows by machine

Tags: **[user machine]**, **[controller]** (API + engine + ansible-runner), **[DPU]** (the lab board/host holding `/home/lab`).
Timing inputs (`config.py` ~84-96): `DAOLAB_CLEANUP_MIN=10`, `DAOLAB_SLOT_MIN=30`, `DAOLAB_JOB_TIMEOUT_SEC=1800`; fit of capture+wipe in the cleanup window is UNVERIFIED.

**Owner opt-out wipe: none; `save_snapshot=false` refused.** (OUT OF SCOPE.)

**End of slot — save:**
1. [controller] Revoke the user's access.
2. [controller] Require the owner's active key and `save_snapshot=1`; otherwise go to failure handling (no wipe).
3. [DPU] Capture as a **streaming `tar | age`** pipeline: tar of `/home/lab` streams directly into the age CLI sealed to S, with the 512 MiB raw-tar cap enforced on the stream. **No two-pass capture and no plaintext stash on disk.** Abort on tar rc≠0, a mount under home, or the empty-home guard.
4. [controller] Wrap S to the owner's recipient (`key.age`, age CLI); write `<snapshot_root>/u<user_id>/<public_id>.{age,key.age,hdr.json}`.
5. [controller] Commit DB `capturing→saved` **before** exit 0.
6. [DPU] Apps cleanup.
7. [DPU] Wipe, gated: only if sealed (`saved`) or the home is verified empty.
8. [DPU] Verify: `verify_clean` plus tripwire (checker streamed over stdin).
9. [controller] Enforce 3 snapshots per (user, setup); system prune holds 0.

**Failure / quarantine:**
- [controller/DPU] Capture failure (tar rc≠0, mount under home, empty-home guard): **keep the home**, raise `home:lab`, quarantine. **No plaintext stash**: nothing unsealed is written anywhere as a fallback.
- `save_snapshot=0` on an enabled setup (legacy/in-flight): keep home, raise `home:lab`, quarantine; never wipe.
- Tripwire hit: residue kind `tripwire`, `_quarantine_setup`; lifts only after a clean re-run; baseline accept needs two admins.
- Capture retry reuses the failed snapshot row via force-clean (D-7-4).

**Restore (next booking):**
1. [user machine] Answer the challenge/unwrap S with the recovery identity (private half never leaves the machine).
2. [controller] API writes the ticket (S only; tmpfs, 0700/0600, TTL 5 min, single-use); engine/helper reads and unlinks it. Orphan sweeper mandatory.
3. [DPU] Decrypt via age CLI and extract in two passes; any non-zero tar rc fails; partial extraction sets `restore_incomplete:<public_id>` and quarantines.
4. [DPU] Board replay: hugepages + driver bindings only; read-back; marker last; closed-schema `daolab-profile/1`, roles by name; unsafe roles excluded (e.g. sdp_pf BDF 0002:01:00.0, driver rvu_af). VF replay disabled.

**Deletion / discard:**
- [user machine] Challenge answer (`delete`, `cancel_delete`, `discard_home`).
- [controller] 24h hold for user/admin deletes; unsaved purge holds 0; engine physically deletes files. Admin home discard: 24h hold, visible to the owner, cancellable by the owner's `cancel_delete` challenge.

## 7. Data model

Exact DDL is **OPEN** for this document: it is not in the attachments, so no column list is reproduced here. Known, from decisions only:
- Migrations today: `PRAGMA user_version` 1..11 (`service/migrations.py`). Planned: 12 snapshots/keys/challenges; 13 jobs rewrite/restore; 14 `admin_approvals`.
- Tables (names known): snapshots, keys, `key_challenges`, `admin_approvals`.
- Known columns/fields: `reservations.save_snapshot` (`CHECK IN (0,1)`; migration default 0, create default 1); `snapshots.public_id`; `key_challenges`: nonce hash (`nonce_sha256`), purpose, `target_ref`, key_id, user_id, `expires_at`.
- Known states: `capturing`, `saved`; `restore_incomplete:<public_id>`; residue/quarantine `home:lab`, `tripwire`.
- Limits: 3 snapshots per (user, setup); 512 MiB raw tar; paths `<snapshot_root>/u<user_id>/<public_id>.{age,key.age,hdr.json}`.
- **DDL deltas `key_challenges.target_ref` (plaintext) and migration-14 `admin_approvals`: SIGNED OFF.**
- Existing schema citations: `models.py:226` vms, `:265-277` access_keys/context; `migrations.py:246` `reservations.ssh_pubkey`.

## 8. Preconditions and gates

- **P-0 gate** (all required before commit/deploy and before B-103): F-1 fixed, stored-row audit clean, patch review, remaining extravar sweep clean, F-5-1 fixed. All currently **OPEN**; patches (`P-0-final.patch`, `F-5-1.patch`) are drafts, runbook results UNKNOWN.
- Platform floor (F-5-3): Python ≥3.12, ansible-core ≥2.20, CI 3.12/3.13; 3.11 REJECTED for ansible-controlled paths. Current `pyproject.toml:9,26-28` (`>=3.11`, `>=2.18`); `.github/workflows/ci.yml:22` runs 3.12.
- **age pin source: RESOLVED** (official GitHub asset + independent lock cross-check + internal binary hashes).
- **Decisions D-7-1..D-7-5: SIGNED OFF.** Per-setup legacy home disposition remains **OPEN**.
  - D-7-1: no opt-out wipe.
  - D-7-2: blob-level `\n` / `\r\n` separators are tolerated.
  - D-7-3: drain legacy reservations, or admin acknowledgement + keep/quarantine.
  - D-7-4: capture retry reuses the failed snapshot row via force-clean.
  - D-7-5: unowned legacy home content is kept/quarantined until a two-admin decision.
- **`snapshot_root` backup policy: DECIDED — no backup in v1.**
- **PR #2 diff: OPEN** (unavailable; line review blocked). No PR #2 code reuse.
- **Production facts: OPEN** (no access to DPU, controller or production DB).
- Tasks **B-103..B-114** per `snapshot-design.md` §8; corrected dependencies in §1. B-103 not started.

## 9. Review findings list

Known IDs only. Severity grades are not recorded in the attachments; "not recorded" means no grade was assigned.

| ID | Severity | Description | File:line evidence | Required fix | Current status |
|---|---|---|---|---|---|
| F-1 | not recorded | Extravar strings are re-templated by Ansible, so `{{ lookup('pipe', …) }}` in a user-supplied key comment, diy template value or username executes on the controller | `service/api.py:152-154` (`_PUBKEY`, comment class `[^\x00-\x1f]*`), `api.py:159-174`, `service/runner.py:495`, `playbooks/roles/dpu_access/defaults/main.yml:15`, `tasks/main.yml:51`; related `api.py:116,701`, `runner.py:493-495`, `playbooks/tasks/plan_vms.yml:36`, `cli.py:75-84,109,124-127` | Strict key-line parser; template-name/username guards; stored-row chokepoint; regression test failing before / passing after | **OPEN** (draft `P-0-final.patch`; results UNKNOWN) |
| F-5-1 | not recorded | `daolab_internal_secret` passed as an extravar, so it persists in job artifacts | `service/runner.py:501`; uses `playbooks/deploy.yml:78` (no_log `:86`), `lease_one.yml:7,44` (no_log `:11,:48`), `vm.yml:202` (no_log `:207`) | Pass via 0600 file + lazy `group_vars` lookup; rotate secret; scrub old job dirs | **OPEN** (draft `F-5-1.patch`; results UNKNOWN) |
| F-5-2 | not recorded | Same-filesystem bind mounts defeat `find -xdev` / `tar --one-file-system` | not cited in attachments | Mount guard in capture/wipe | **OPEN** (not implemented) |
| F-5-3 | not recorded | ansible-core ≥2.20 needs Python ≥3.12; 3.11 REJECTED for ansible-controlled paths | `pyproject.toml:9,26-28`; `.github/workflows/ci.yml:22` | Raise floor to Python ≥3.12 / ansible-core ≥2.20; CI 3.12 and 3.13 (B-103) | **OPEN** (not started) |
| V4 | not recorded | Dashboard XSS: `esc()` misses `'` and is used in single-quoted inline handlers | `dashboard/user.html` ~223, 262-266, 294-302, 315-323, 367-400, line 8 (mermaid CDN); `admin.html:194` | New UI DOM-APIs only (no innerHTML/outerHTML/insertAdjacentHTML/document.write/inline `on*`/string-built handlers); lint test + XSS suite; CSP Report-Only first; mermaid SRI or self-host | **OPEN** (existing pages not hardened) |
| I13–I17 | n/a | Invariants as defined in your earlier message (text not restated here) | n/a | n/a | as defined by you |
| Row 25 `daolab_vm_names` | not recorded | EXTRAVAR-MATRIX row 25: user-controllability and vulnerability both UNKNOWN | matrix row 25; `playbooks/orphans.yml`; `engine.py:479-499` | Run the M-vm-names harness (draft) | **OPEN** |

**OPEN: complete F-ID registry unavailable.** No other IDs are asserted.

## 10. Acceptance tests

All are pass criteria; none has been run. Statuses elsewhere are unchanged.

### 10.1 Mock / harness tests
- **P-0:** strict key parser; regression test fails before and passes after; diy `templates`, usernames and stored rows cannot reach `lookup('pipe')`; no echo of user text.
- **F-5-1:** secret absent from extravars, env and persisted job artifacts (real ansible-runner canary).
- **Row 25:** orphans destroy path with a canary VM name produces no canary (draft harness).
- **Challenge:** wrong purpose/target/key/user; replay; expiry; ≤3 open; CAS single use.
- **Rotation:** challenge bound to new fingerprint; all `key.age` re-wrapped; temp-write + rename; interrupted rotation leaves old identity valid; token-derived key paths absent.
- **Ticket/secret rules:** perms 0700/0600, TTL 5 min, single use, unlinked after read, sweeper removes orphans; S/identity absent from DB, backups, logs, argv, env, extravars, job rows and job dirs.
- **Save:** streaming `tar | age` (no plaintext on disk); DB commit `capturing→saved` precedes exit 0; wipe only when sealed or verified empty; tar rc≠0, mount-under-home and empty-home guard each keep the home, raise `home:lab`, quarantine.
- **Legacy/opt-out:** `save_snapshot=0` on an enabled setup never wipes; no active key refused; `save_snapshot=false` refused; capture retry reuses the failed row via force-clean.
- **Restore:** non-zero tar rc fails; partial extraction ⇒ `restore_incomplete:<public_id>` + quarantine.
- **Tripwire (mock):** per-class counts only; quarantine lifts only on a clean re-run; baseline accept needs two admins.
- **Lifecycle:** 24h holds, two-admin lost-key reset, 3 snapshots per (user, setup), 512 MiB cap.
- **Dashboard:** lint forbids banned DOM APIs and inline handlers; XSS suite.
- **Platform:** CI on 3.12 and 3.13 with ansible-core ≥2.20.

### 10.2 Real-board tests
- **A→B→A:** user A saves, user B books and is isolated from A's data, then A restores and gets A's snapshot intact.
- **512 MiB capture timing vs cleanup window:** time a 512 MiB capture + seal + wipe + verify against `DAOLAB_CLEANUP_MIN=10` / `DAOLAB_JOB_TIMEOUT_SEC=1800`; pass only if it fits.
- **Board readback:** `dpdk-devbind` output, hugepage counts and driver bindings read back match the `daolab-profile/1` profile after replay; unsafe roles excluded; VF replay stays disabled.
- **Tripwire plant matrix:** plant one marker per class (identity, sudo, ssh, sched, boot, bin, kmod, mount, proc, tmp, daolab); each must raise `tripwire:<class>:<count>`, quarantine, and clear only after a clean re-run; run per `hw_class` baseline (baseline itself OPEN).
- **Human signoff:** operator and a second admin sign off the real-board results before any enablement.

## 11. Open questions

- **OPEN:** F-1, F-5-1 (deployment, secret rotation, job-dir scrub).
- **OPEN:** row 25 `daolab_vm_names`.
- **OPEN:** PR #2 diff review.
- **OPEN:** production facts (DPU, controller, production DB).
- **OPEN:** tripwire baseline per `hw_class`.
- **OPEN:** per-setup legacy home disposition.
- **OPEN:** exact DDL listing for this document (the deltas themselves are SIGNED OFF).
- **OPEN:** F-5-2 mount guard, F-5-3 floor, V4 existing-page hardening (implementation).
- **OPEN:** complete F-ID registry unavailable.
- **UNKNOWN:** all operator runbook results. **UNVERIFIED:** age v1.1.1 checksum publication; capture+wipe fit within the cleanup window.
- **Closed:** age pin source RESOLVED; D-7-1..D-7-5 SIGNED OFF; DDL deltas SIGNED OFF; `snapshot_root` backup DECIDED (none in v1); owner opt-out wipe OUT OF SCOPE.
