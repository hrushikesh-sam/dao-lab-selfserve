# Completion log

What has been finished, newest first, with the evidence for each. The task list is
[plan/10](plan/10-backend-tasksheet.md); open findings are in [mistakes.md](mistakes.md).

## Where things stand (2026-11-14)

| | |
|---|---|
| Backend tasksheet | 70 of 71 tasks `done` (code + tests on `b93-xo`); B-85 `blocked` on SDP VFs |
| Pending | B-85: SDP VFs on `dpu-01-xcp` ([docs/sdp-blocker.md](docs/sdp-blocker.md)) |
| Tests | CI runs the whole suite on every push (B-101) |
| Real hardware | VPP with its VMs end to end (DPU + tgen/sink through XO), healthy, released, traced in Jaeger; SmartNIC on the DPU; password-mode access |
| Not yet on hardware | SmartNIC's host VM and the TLS proxy's backend VM (SDP VFs) |

## 2026-11-17 — the TLS proxy use case, without SDP

- Wired as DAO_Setups TLS_Proxy_ansible "Setup 2" (verified live 2026-09-05): client VM on an E810
  VF -> HTTPS to nginx on the DPU's enP2p4s0 (10.65.0.190, CPT offload) -> `/test/` over the
  management network to the backend VM's HTTP :80 (`mistakes.md` Q10). Code and tests done; the
  run on the board is next.

## 2026-11-17 — nginx-tls back at rest; resting hugepage count is 2

- nginx-tls had failed at rest since the 2026-10-17 reboot (`mistakes.md` Q9): no CPT VFs. Rebuilt
  on the board with the vendor script (24 VFs on vfio-pci): `CPT HW Offload Configured!!!`, one
  worker, `https 200` from the DPU.
- Its CPT engine maps two 512 MB pages and dies with one (`rte_errno=12`); it never reserves them
  itself. `ea4e733` set the count to 0 and `a6aa0e8` to 1 before restarting it; cleanup and
  reconcile now set it to 2 (`daolab_hugepages_baseline`) and clear engine files on both hugetlbfs
  mounts (`/dev/hugepages`, `/mnt/huge`). Board after: 512 MB 2 reserved / 0 free, 2 MB 0.

## 2026-11-15 — DPU hugepages given back

- The board had six 512 MB hugepages (3 GB) reserved since SmartNIC's last run, one of them held
  by a file a failed nginx-tls engine start left (`e_cpt_engine82558map_0`, no process mapping it;
  first blamed on VPP); nothing reserves any at boot. `ea4e733`: cleanup (VPP and SmartNIC) and force-clean remove unmapped hugepage files and
  set the 512 MB count back to the baseline (0); `verify_clean` reports leftovers as
  `dpu_hugepages:<n>`.
- On the board: VPP reserves 2 itself and runs healthy from 0 (it never depended on the leak),
  End now -> 0; SmartNIC reserves 6 -> its cleanup (`release_hugepages`) -> 0, no files, clean.

## 2026-11-14 — B-86 on the real lab

- **The run** (reservation 5, alice): one click on `vpp` → lease → VPP on xeon3-oct-112 → sink
  and tgen through XO → the OTel agent on the DPU and both VMs → `verify ok: received = 4 (== 4)`
  → ready → **healthy** (`vms_up`, `ssh_reachable`, `dpu_app`, `datapath`) → End now → cleanup
  clean → released. Jaeger trace `898db8a9` (daolab-engine + daolab-ansible, 287 spans); demo
  recorded. Bob being refused was not shown live; the mock-runner test covers it.
- **Fixed on the way**, each found only once telemetry ran on hardware:
  - `156c8ab` — the VM template has no OTel agent; the deploy now installs and starts it on every
    VM after boot. The central collector is pinned to 0.160.0 (0.115.0 does not exist).
  - `df16daa` — the agent config (2228 bytes as base64) was over xenstore's `quota-maxsize` (2048),
    so `VM.start` failed with `Xs_protocol.Error("E2BIG")` and left the VM paused; it is written
    over SSH instead. A failed or paused VM now stops the deploy with its own error.
  - `ee82a6e` — the 0.160 collector's remote-write stores `tcpcheck_status_ratio` and
    `vpp_interface_rx_packets_total`; the health rules read those names.
  - `1e09ae9` — the freshness query `timestamp({…})` failed on series that differ only by name;
    it reads `timestamp(up{…})`.
- Then: the two dashboards keep their tokens under their own keys (the shared key had the user
  page deploy as dev-admin), and `daolab-xo` exits 3 on a 404.

## 2026-11-01 … 2026-11-04 — P7 (Akshay)

- B-93 VMs through Xen Orchestra (`85377e3`); B-92 the real lab in the inventory (`d1335ef`);
  B-96 a DPU-only functional gate and a playbook timeout (`ec768e5`); B-97 `/internal/vm-ip`
  takes only the leased address, arping before use (`072451b`); B-99 SmartNIC on the real board
  (`3cc37fb`, launch fix `3a5de69`); B-100 password-mode login verified on the board (2026-11-03).
- B-94 `failed` is terminal, not held (`4190966`); B-95 `verify` checks exit code and parsed
  numbers, TLS gates (`953ffda`); B-98 docs folded into plan/09 (`4082745`); B-101 CI, nightly DB
  backup, token expiry (`f0cbc1a`).

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
