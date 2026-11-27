# Snapshot design §8 — task table

## 8. Task table

This is the Phase 8 task table called for by B-104 §0 R-1. It records the Phase 8 split in §8 that was missing from `plan/10-backend-tasksheet.md`. Statuses are as of 2026-11-24 after X-AGE merged as `7a2dbad`.

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| X-AGE | Install pinned age v1.1.1 in CI, controller script, DPU task; `requires_age` marker | `deploy/age/`, CI, playbooks, `service/config.py`, tests | — | CI installs and verifies age; marker fails in CI if age missing | Akshay | done (PR #11) |
| X-DOCS | Errata v1.2, E-15, §8 task table, stale doc ID, production ansible note | docs only | — | Docs land on `rama`; cross-references resolve | Rama | in review |
| B-103 | Python/core/age floor and CI | `pyproject.toml`, CI, `deploy/age/` | — | Controller Python >= 3.12, ansible-core >= 2.20, CI 3.12/3.13, pinned age assets | Akshay | done |
| B-104 | Snapshot/challenge/approval schema; reverted-schema guard | `service/migrations.py`, migration tests, DDL doc | B-103 | One migration 14, schema v14, guard refuses reverted v12/v13 shapes | Akshay | done (PR #9/#10) |
| B-113a | Dashboard V4 hardening on existing pages | `dashboard/`, `service/api.py`, tests | — | No inline handlers, escaped interpolation, mermaid pinned/self-hosted, CSP Report-Only | Rama | todo |
| B-105 | `agecli`, key enrollment/activation, approvals, lost-key reset | `service/agecli.py`, `service/keys.py`, `service/approvals.py`, API/CLI/config/models, tests | X-AGE, B-104 | Different admin activates; reset has 24h hold and CAS execution; CLI identity stays client-side | Rama | todo |
| B-107 | Tickets, sweeper, swap check, API startup schema check | `service/tickets.py`, `service/hostcheck.py`, config/API, tests | X-AGE, B-104 | 0600 tmpfs ticket, unlink-before-use, 5 min TTL, count-only sweeper; unsafe swap refused | Rama | todo |
| B-106 | Challenges, owner cancel | `service/challenges.py`, approvals/API/CLI, tests | B-105 | Single-use CAS in caller transaction; TTL 10 min; <=3 open; nonce never stored | Rama | todo |
| B-109a | DPU capture script and Python 3.10 CI job | DPU script, `tests/dpu/`, CI | X-AGE | Streaming `tar -> raw cap -> gzip -1 -> age`; cap counts raw stream; 3.10 stdlib | Akshay | todo |
| B-109b | Capture/wipe playbooks and gated `home:lab` check | playbooks, DPU helper scripts, tests | B-109a | Save before gated wipe; root capture/wipe; refused capture keeps home and quarantines | Akshay | todo |
| B-109c | Controller capture lifecycle | `service/snapshots.py`, DB states, retention, cleanup wiring | B-105, B-109b | `saved` only after sealed file; failed rows and retention follow DDL v4.1 | Akshay | todo |
| B-108a | Snapshot API, holds, enable flag, booking rules, delete sweep | `service/snapshot_holds.py`, API, catalog, tests | B-106, B-109c | Own/admin holds, 24h holds, admin-controlled enable excluded from sync | Rama | blocked by B-109c |
| X-WIRE | Engine tick wiring for sweeps, resets, discard, swap check | `service/engine.py`, tests | B-105, B-107, B-108a | Pure Rama functions wired without cross-owner edits | Akshay | todo |
| B-110 | Restore: SSH stdin identity flow, DPU restore script, restore job | `service/dputransport.py`, engine/runner/playbooks, tests | B-107, B-109c | `age -d -> gunzip -> tar`; any `compression` value other than `gzip` refused | Akshay | todo |
| B-108b | Restore endpoint and CLI | API/CLI, tests | B-107, B-110 | Ticket write plus restore job; S never in DB/logs/argv | Rama | blocked by B-110 |
| B-106b | Key rotation | key modules, API/CLI, tests | B-106, B-108a | Old identity kept until all `key.age` rewrapped; temp-file + rename | Rama | blocked by B-108a |
| X-ML | Port ml-tvm use case to `rama`, including A-1 | inventory/playbooks/use-case files | — | ML device role and replay data land without unsafe roles | Akshay | todo |
| B-111 | Snapshot profile and device replay | profile files, restore playbook, tests | B-109b, B-110, X-ML | Recorded BDF/driver bindings replay with readback; no invented hugepages | Akshay | todo |
| B-112 | Tripwire checker and synthetic baselines | checker, playbooks, tests | B-109b, approvals | Closed classes; safe `tripwire:<class>:<count>`; two-admin accept/rebaseline | Akshay (Rama: production baselines) | todo |
| B-113b | Dashboard snapshot/key/approval/tripwire views | dashboard templates/tests | B-108a, B-112 | DOM APIs only; owner/admin views show only allowed data | Rama | blocked by B-108a/B-112 |
| B-114 | Acceptance tooling | acceptance scripts/docs, tests | B-109, B-110, B-112 | Synthetic 512 MiB timing, held-row retention, restore/quarantine and safe-output gates pass | Akshay builds, Rama reviews | todo |

Corrected dependency notes from `origin/akshay-p8-gate:plan/design/p8-gate-review/p8-snapshot-design-v1.1.md` §1: X-AGE precedes all age-using tasks. B-109 <- B-103, B-104, B-105; B-108 <- B-104, B-106 and, for snapshot files, B-109c; B-107 <- B-103, B-104. Critical path: X-AGE -> B-105 -> B-109c -> B-110 -> B-111/B-112 -> B-114.

## 9. Operational notes

- Every existing DB needs `daolab init` once after B-104 lands; a DB that ran earlier B-104 must be recreated, not migrated.
- ansible-core 2.20 security support ended 2 Nov 2026. The tested floor stays 2.20 for compatibility; production runs 2.21.
