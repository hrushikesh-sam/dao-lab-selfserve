# Completion log

What has been finished on `rama`, newest first, with the evidence for each. The task list is
[plan/10](plan/10-backend-tasksheet.md); what is still open is its **P7** section (B-92…B-101)
and [mistakes.md](mistakes.md).

## Where things stand (2026-10-23)

| | |
|---|---|
| Backend tasksheet | 59 of 61 tasks `done` (code + tests on the branch); B-85 and B-86 `blocked` on the XCP-ng hosts |
| Pending | P7: 10 tasks, B-92…B-101 (VMs via Xen Orchestra, four correctness fixes, use-case gates, docs, ops) |
| Tests | 861 passed (full suite, merged tree, `21961c6`) |
| Real hardware | VPP on the DPU xeon3-oct-112 (10.68.0.190) deploys and cleans up from the dashboard |
| Not yet on hardware | the use cases' VMs (`DAOLAB_DEPLOY_VMS=false`), VF/SDP passthrough, TLS proxy, SmartNIC, the telemetry stack |

## 2026-10-23

- **Merged `origin/rama`** (`21961c6`), kram2006's three commits of 2026-10-16:
  - `DELETE /reservations/{id}` now cancels a queued booking only; a live slot ends with
    `POST …/cleanup`.
  - A state-aware redeploy gate.
  - The `dev.sh` Ctrl-C fix.
  - plan/10 statuses, now reconciled with the merged tree (59 done).

  Conflicts and how they were resolved are in the merge commit message.
- **Plan audit re-checked** (`f2c633e`). All 124 findings in `mistakes.md` now carry their status
  on `rama`:

  | Status | Count |
  |---|---|
  | Fixed | 31 |
  | Partial | 45 |
  | Open | 39 |
  | Obsolete | 2 |
  | Settled | 2 |
  | Audit itself wrong | 5 |

  The audit's own errors are corrected, and six new findings (P1–P6) are recorded. P1 is a
  severity-1 race: a booking can deploy over a failed reservation whose cleanup is still pending.
- **Showcase on the real board** (`565b8ec`):
  - `scripts/run/showcase.sh` runs the real lab with VPP as the only deployable use case
    (`DAOLAB_DEPLOYABLE`), DPU only.
  - The dashboard shows the DPU credentials: host `10.68.0.190`, username `lab`, key or password,
    and the ssh command.
  - The admin page has a per-reservation detail: jobs, steps, timings and the playbook log.

  Rehearsed in a browser on xeon3-oct-112:
  - Deploy reached ready in 73 s, first attempt.
  - `ssh lab@10.68.0.190` worked with the user's key; VPP `eth0` was up with 10.65.0.192/24.
  - End now reached released in 42 s. The board went back on nginx-tls, with port `0002:04:00.0`
    back on `rvu_nicpf`, and the `lab` login refused afterwards.
- **Xen Orchestra in a container** (`21a9ba8`). `scripts/run/xo.sh` runs the community image
  5.211.1 on `https://127.0.0.1:8443/`; the pools are added in its UI. This is the start of B-93.
- **Fixes:**
  - The exporter unit started only on the second attempt (`trim_blocks`), `8ffb2ad`.
  - The engine could not be stopped after its first playbook, because ansible-runner kept the
    SIGTERM handler; a fresh DB also appended to old job logs. Both fixed in `28ebb8c`.
  - Tests read a developer's `.env`, `bf48514`.
  - `add-user --rotate` deleted the user's reservations, `7c82a3d`.
- **Docs:** plan/02 and plan/08 cover every setting (40) and the new endpoints; README has the
  launchers, status and pending work.

## 2026-10-16

- **First real deploys from the dashboard on xeon3-oct-112** (VPP, Akshay's runbook
  `VPP_ansible/` wrapped by `playbooks/apps/dao-vpp`). The board turned up four faults, each fixed
  and covered by a test:
  - The engine could not find `ansible-playbook` without the venv on PATH (`7854edb`).
  - SSH pipelining made `vppctl` exit 1 silently, and VPP runs as `vpp_main`, so `pgrep -x vpp`
    missed it and retries started extra VPPs (`b1931de`).
  - The live port's driver override is now pinned first, and a bad sshd drop-in is never left
    behind (`074e6f0`).
  - `dao-test`'s sudo password goes through a file, never the chat or a log
    (`DAOLAB_BECOME_PASSWORD_FILE`, `3a7939b`).
- **Deploy access** (`67ce842`, `fc72dcf`). One-click Deploy with the user's SSH key or a
  generated password on the DPU's `lab` login. Keys carry an sshd `expiry-time` at the slot end,
  and access is revoked first at cleanup.
- **Real lab addressing** (`678d5e8`). The DPU is static at 10.68.0.190; VMs lease from
  10.68.1.0–10.68.2.200.
- **P6 tasks done:**
  - B-73 orphan sweep (`80a6162`) and B-74 force-clean (`a0a39f8`).
  - B-63 Ansible OTel callback (`cc3d629`) and B-83 otel agent (`6151e55`).
  - B-82 cleanup (`67ce842`), B-85 bring-up playbook (`d09cb71`), B-91 ops runbook (`8e2c4ab`).
  - B-90 doc fold of plan/02–05 (`54626d0`).
  - B-80 / B-81 / B-84 deploy + ansible runner, merged from `akshay` (`9c25529`).
  - B-86 end to end on the mock runner, merged from `akshay` (`bf93497`).
- **P3–P5 tasks done:**
  - B-42 lease endpoints (`5a03b24`), B-44 mock leases (`fb8aa4b`).
  - B-51 authz keys (`eefdf10`), B-52 `daolab-keys` (`773143d`).
  - B-66 health endpoints (`e0ccf47`).
  - B-41, B-50, B-62, B-65, B-67 and B-70–72, merged from `akshay` (`a3dfa2f`).

## 2026-10-15

- **P0–P2 and the first P5 tasks** (Rama: kram2006; Akshay):
  - Foundations: B-01 config (`a57a8a7`), B-04 inventory/setup (`ff9acbf`), B-05 use cases
    (`61de526`), B-18 setup availability (`1e062b5`).
  - Booking: B-10…B-13 (`7c369f5`, `68248d5`, `cc620d9`, `5a052b6`).
  - IPAM: B-40 (`520ebaf`).
  - Jobs and engine: B-20 jobs (`8466b70`), B-21 states (`c841000`), B-22/B-23 runner + engine
    (merges `1055eb6`, `7937eb2`), B-25 retries (`0bd072c`), B-27/B-28/B-29 endpoints (`0fa4960`,
    `6e802de`, `8065f28`), B-30 one-command dev stack (`a681b32`).
  - Access: B-53 session kill (`4bd6b4c`).
  - Telemetry: B-60/B-61 OTel stack + SDK (`abf65fc`, `83b4f4c`), B-64 health rules (`24177e1`),
    B-68 mock telemetry (`8ecbc13`).
