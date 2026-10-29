# Backend tasksheet

Part of the daolab implementation plan ΓÇö see [README.md](README.md) for the doc map.

These are the backend tasks that implement [09-backend-system-design.md](09-backend-system-design.md);
the dashboard is out of scope. The phases follow the design's prototype plan (P1ΓÇôP6). Every
task up to P5 can be built and demonstrated with `DAOLAB_RUNNER=mock` and the test users, so the
testbed is needed only in P6.

**What already exists:** `service/api.py` (auth, `/usecases`, `/reservations` CRUD,
`/internal/vm-ip`), `scheduler.py` (FCFS + slot-shift), `catalog.py`, `cli.py`, `models.py`,
`schema.sql` (7 tables), and tests for the api, catalog and scheduler.

**What was missing when this sheet was written** (all built since): `engine.py`, `runner.py`,
`playbooks/`, the jobs model, IP leasing, access control, OTel, health and cleanup verification.

Status values: `todo` ┬╖ `doing` ┬╖ `review` ┬╖ `done` ┬╖ `blocked`.

_Status reconciled after merging `origin/rama` into `rama` (2026-10-23): a task is `done` only
when its code **and** tests are on the branch; `blocked` means it needs something not done
yet; `todo` is unblocked and open._

**Status:** every task is `done` except the two that need the hardware: B-85 (testbed
bring-up ΓÇö the DPU half is done on xeon3-oct-112; the XCP-ng half needs the lab's two hosts
in the inventory) and B-86 (the recorded run ΓÇö the DPU path runs end to end from the
dashboard on the real board; the VMs need B-85). On top of the tasksheet: the DPU apps run
Akshay's runbook playbooks (`VPP_ansible/`, `dao-smartnic/`) through `playbooks/apps/`, the
dashboard deploys in one click with the user's SSH key or a generated password on the DPU's
`lab` login, DPUs are static (xeon3-oct-112 at 10.68.0.190) and VMs lease from
10.68.1.0ΓÇô10.68.2.200. What is still open is **P7** (B-92ΓÇªB-101, below); what was finished
when is in [COMPLETION_LOG.md](../COMPLETION_LOG.md).

---

## Ownership

| Owner | Focus | Tasks |
|---|---|---|
| **Akshay** | The complex core: scheduling algorithm, engine and runner, concurrency and race safety, IPAM allocator, access logic, health evaluator, cleanup/reconcile flow, `deploy.yml` + DPU app roles, end-to-end | 25 |
| **Rama** | Everything around the core: config and seeding, inventory and use-case YAMLs, schema for leases/audit/availability, API endpoints over Akshay's modules, OTel stack + SDK, mock telemetry, `cleanup.yml`, OTel agent role, testbed bring-up, docs | 36 |

**Handoffs, where one person waits on the other.** Land these early so nobody is blocked:

| Akshay delivers ΓåÆ | unblocks Rama's |
|---|---|
| B-03 schema migration (do first) | B-18, B-40, B-55 |
| B-13 `NoFit` alternatives | B-14 booking endpoint, B-16 availability |
| B-20/B-21 jobs + states | B-27, B-28, B-29 endpoints |
| B-41 `ipam.py` | B-42 lease endpoints |
| B-50 `access.py` | B-51 ΓåÆ B-52 `daolab-keys` |
| B-65 `health.py` | B-66 health endpoints |
| B-71 cleanup flow | B-74 force-clean |

| Rama delivers ΓåÆ | unblocks Akshay's |
|---|---|
| B-40 lease schema | B-41 allocator |
| B-18 availability flag | B-71 quarantine |
| B-61 OTel SDK | B-62 trace per job |
| B-64 YAML `health:` schema | B-65 evaluator |
| B-42 + B-52 lease endpoints, `daolab-keys` | B-80 `deploy.yml` |
| B-85 testbed bring-up | B-81 DPU roles, B-86 end-to-end |

**Suggested start:**
- **Akshay:** B-03 ΓåÆ B-10ΓÇªB-13 ΓåÆ B-20ΓÇªB-23.
- **Rama:** B-01, B-02, B-04ΓÇôB-06 ΓåÆ B-60/B-61 (OTel stack, no dependencies) ΓåÆ B-40 ΓåÆ endpoints
  as Akshay's modules land. Start B-85 (testbed bring-up) early, because hardware surprises are
  the biggest schedule risk.

---

## P0 ΓÇö Foundations

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-01 | Add config vars: `DAOLAB_RUNNER`, `DAOLAB_SLOT_MIN`, `DAOLAB_CLEANUP_MIN`, `DAOLAB_DEFAULT_HOURS`, `DAOLAB_IP_POOL`, `DAOLAB_EARLY_ACCESS`, `DAOLAB_HEALTH_STALE_SEC`, `DAOLAB_OTEL_ENDPOINT`, user quota caps | `config.py`, `.env.example` | ΓÇö | settings load with defaults; listed in `.env.example` | Rama | done |
| B-02 | Seed test users `alice`, `bob`, `dev-admin` with tokens printed at start | `scripts/run/dev.sh`, `cli.py` | ΓÇö | `dev.sh --fresh` prints 3 tokens; all 3 pass `/whoami` | Rama | done |
| B-03 | Schema migration path: idempotent `daolab init` that adds the new tables/columns without wiping data | `schema.sql`, `models.py`, `cli.py` | ΓÇö | running `init` twice on an existing DB is a no-op | Akshay | done |
| B-04 | Collapse the inventory to one testbed: `tb-01-xcp` + `dpu-01`; rewrite `setups/setup-01.yml` ΓåÆ `tb-01` with the Y-cable links (p0, p1 ΓåÆ DPU) | `inventory/hosts.yml`, `setups/` | ΓÇö | `daolab sync` shows `tb-01` with capabilities `[e810, octeon-cn106, sdp, wire]` | Rama | done |
| B-05 | Use-case YAMLs for `vpp` and `smartnic` (roles, requires, hw_class, verify) | `usecases/` | B-04 | `GET /usecases` lists all three as `active` | Rama | done |
| B-06 | Shared test fixtures: frozen clock, temp DB, seeded users/setups | `tests/conftest.py` | B-02 | used by every new test module | Rama | done |

## P1 ΓÇö Booking: asap vs. `at`, conflict blocking

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-10 | Add `mode` (`asap` default \| `at`) to `PlacementRequest`; `at` = single overlap test, never shifted | `scheduler.py` | ΓÇö | unit tests: `at` on a busy window ΓåÆ conflict result; `asap` ΓåÆ earliest fit | Akshay | done |
| B-11 | Turnaround buffer: block `end_at + cleanup_min (+ reflash lead if the next hw_class differs)` | `scheduler.py` | B-10 | test: back-to-back bookings start no earlier than the buffer allows | Akshay | done |
| B-12 | Slot grid: snap starts to `DAOLAB_SLOT_MIN`; when starts tie, prefer the one next to the same hw_class | `scheduler.py` | B-10 | test: starts are grid-aligned; the same-class neighbour is chosen without breaking FCFS | Akshay | done |
| B-13 | `NoFit` result carrying the next 3 free alternative starts for the same duration | `scheduler.py` | B-10 | test: alternatives are free, sorted, the right length | Akshay | done |
| B-14 | `POST /reservations` accepts `mode`/`start_at`/`hours`; `409 slot_conflict` with `detail.alternatives` | `api.py` | B-13 | api test: alice books 09:00, bob's 10:00 `at` ΓåÆ 409 with alternatives | Rama | done |
| B-15 | Per-user quota: 1 live + 2 future reservations ΓåÆ `409 quota_exceeded` | `api.py`, `models.py` | B-14 | api test covering both caps; admins exempt | Rama | done |
| B-16 | `GET /availability?usecase=&from=&to=`: free/busy windows; owners visible to admins only | `api.py`, `scheduler.py` | B-11 | non-admin responses contain no usernames | Rama | done |
| B-17 | Concurrency test: N threads booking the same `at` window ΓåÆ exactly one 201 | `tests/test_concurrency.py` | B-14 | passes 50 runs in a row | Akshay | done |
| B-18 | Setup availability flag (`available`/`quarantined`/`maintenance`): placement still books, but the engine won't start provisioning on a non-available setup | `schema.sql`, `scheduler.py`, `models.py` | B-03 | test: queued reservation stays queued while quarantined | Rama | done |

## P2 ΓÇö Jobs, engine, runner (mock), cleanup endpoint

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-20 | Jobs entity: kind (`deploy`/`cleanup`/`health_probe`/`reconcile`), state, attempt, trace_id, steps, log path | `migrations.py` (v5; `schema.sql` is the frozen baseline), `models.py`, `tests/test_jobs.py` | B-03 | model helpers + unit tests | Akshay | done |
| B-21 | Reservation states: rename `tearing_down` ΓåÆ `cleaning`; add `quarantined`; CAS-style transition helper | `migrations.py` (v6 rewrites the state CHECK), `models.py`, `api.py`, `dashboard/*.html`, `tests/test_reservation_states.py` | B-20 | invalid transitions raise; existing tests updated | Akshay | done |
| B-22 | `runner.py` interface: `deploy(res)`, `cleanup(res)`, `probe(res)`, `verify_clean(res)`; `ansible` and `mock` backends chosen by `DAOLAB_RUNNER` | `runner.py`, `config.py`, `.env.example`, `tests/test_runner.py` | B-20 | mock runs step by step with sleeps, records steps on the job; `DAOLAB_MOCK_FAIL=deploy\|cleanup` injects failures | Akshay | done |
| B-23 | `engine.py` tick: queuedΓåÆprovisioning (start ΓêÆ lead, setup free & available), readyΓåÆactive, activeΓåÆcleaning at end_at, run pending jobs | `engine.py` | B-21, B-22 | `python -m service.engine` runs one tick; `--loop` ticks every interval | Akshay | done |
| B-24 | Single-flight per setup: at most one running deploy/cleanup/reconcile job per setup (probes exempt) | `engine.py`, `models.py` | B-23 | test: two pending mutating jobs ΓåÆ only one runs | Akshay | done |
| B-25 | Retry policy: deploy retried to `MAX_RETRIES` ΓåÆ `failed` + automatic cleanup job | `engine.py` | B-23 | test with `MOCK_FAIL=deploy` | Rama | done |
| B-26 | Crash recovery: on start, `running` jobs older than a timeout go back to `pending` (playbooks are idempotent) | `engine.py` | B-23 | test: kill mid-job ΓåÆ next tick resumes | Akshay | done |
| B-27 | `POST /reservations/{id}/cleanup` (owner; `ready`/`active` ΓåÆ `cleaning` + job, `202`) | `api.py` | B-21 | api test incl. 403 for another user | Rama | done |
| B-28 | `POST /reservations/{id}/redeploy` (owner, inside the slot only) | `api.py` | B-20 | 409 outside the slot; new deploy job inside it | Rama | done |
| B-29 | `GET /jobs/{id}` and `GET /reservations/{id}/events` (SSE of job steps + state changes) | `api.py` | B-20 | curl `-N` shows step events during a mock deploy | Rama | done |
| B-30 | `make engine` / `dev.sh` start the engine alongside the API | `Makefile`, `scripts/run/dev.sh` | B-23 | one command starts both; Ctrl-C stops both | Rama | done |

## P3 ΓÇö IP leasing (IPAM)

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-40 | IP pool + lease entities; `(pool, ip)` unique among live leases; `(reservation, role)` unique | `schema.sql`, `models.py` | B-03 | constraint test: a duplicate live IP insert fails | Rama | done |
| B-41 | `ipam.py`: `lease(res, role)` (idempotent, lowest free), `release(res)`, `quarantine(ip)`, `mark_conflict(ip)` | `ipam.py` | B-40 | unit tests: idempotency, exhaustion error, a quarantined IP is never re-issued | Akshay | done |
| B-42 | `POST /internal/leases {reservation_id, role}` (`X-Daolab-Secret`); `POST /internal/leases/conflict {ip}` ΓåÆ mark it and return a new IP | `api.py` | B-41 | api tests; rejects unknown or terminal reservations | Rama | done |
| B-43 | Pool loaded from `DAOLAB_IP_POOL` on `daolab sync` (later per-setup YAML) | `catalog.py` | B-40 | sync is idempotent; shrinking the pool never drops live leases | Rama | done |
| B-44 | Mock runner calls the real lease + vm-ip endpoints | `runner.py` | B-22, B-42 | mock deploy shows leased IPs on the reservation | Rama | done |
| B-45 | Concurrency test: parallel deploys never share an IP | `tests/test_ipam.py` | B-41 | passes under 20 threads | Akshay | done |

## P4 ΓÇö Access control (VMs + DPU)

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-50 | `access.py`: `keys_for(host, login, now)` ΓåÆ owner key iff a `ready`/`active` reservation on the setup holding `host` has `start Γëñ now < end` (`EARLY_ACCESS` flag aware) | `access.py` | B-21 | unit tests: before, during, after the slot; other setup; disabled user | Akshay | done |
| B-51 | `GET /internal/authz/keys?host=&login=` returning plain-text `authorized_keys`; empty on any error | `api.py` | B-50 | curl output usable directly by sshd | Rama | done |
| B-52 | `daolab-keys` script for sshd `AuthorizedKeysCommand` (fail-closed, 2 s timeout) + sshd drop-in config | `playbooks/files/daolab-keys`, `playbooks/files/sshd-daolab.conf` | B-51 | on a test VM acting as "DPU": alice can log in only during her slot | Rama | done ΓÇö the real "alice only during her slot" login is checked at bring-up (B-85) |
| B-53 | Session kill at slot end: engine schedules a `terminate-user` step for the DPU in the cleanup job | `engine.py`, `runner.py` | B-23 | mock records the step; real role in B-82 | Rama | done |
| B-54 | Owner-only credential exposure: ssh lines + DPU line only in owner/admin reservation views | `api.py` | ΓÇö | api test: bob cannot see alice's IPs | Rama | done |
| B-55 | Audit events: deploy, cleanup, redeploy, force-clean, admin overrides | `schema.sql`, `models.py`, `api.py` | B-03 | rows written; `GET /admin/audit` (admin) | Rama | done |

## P5 ΓÇö OpenTelemetry + health

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-60 | Telemetry stack: `otelcol-contrib` + Prometheus + Jaeger in docker compose; collector config (otlp, httpcheck, tcpcheck, prometheus receivers; resource processor; prom remote-write + otlpΓåÆjaeger) | `deploy/otel/` | ΓÇö | `docker compose up` ΓåÆ Jaeger UI + Prometheus up | Rama | done |
| B-61 | OTel SDK in the API + engine (FastAPI auto-instrumentation); no-op when `DAOLAB_OTEL_ENDPOINT` is unset | `service/otel.py`, `api.py`, `engine.py`, `pyproject.toml` | B-60 | request spans visible in Jaeger | Rama | done |
| B-62 | One trace per job: engine opens the job span, stores `trace_id`, passes `TRACEPARENT` to the runner | `engine.py`, `runner.py` | B-61, B-22 | the job's `trace_id` opens in Jaeger with child steps | Akshay | done |
| B-63 | Ansible `community.general.opentelemetry` callback enabled in runner env | `runner.py`, `playbooks/ansible.cfg` | B-62 | each Ansible task appears as a child span (verified in P6) | Rama | done |
| B-64 | `health:` block in the use-case YAML schema (name, promql with `$RID`, expect, severity); validated by `daolab sync` | `catalog.py`, `usecases/*.yml` | B-05 | sync rejects a malformed rule with file + rule name | Rama | done |
| B-65 | `health.py`: run the rules via the Prometheus HTTP API ΓåÆ `healthy`/`degraded`/`down`/`unknown` (stale > `HEALTH_STALE_SEC`) | `health.py` | B-64 | unit tests with a stubbed Prometheus | Akshay | done |
| B-66 | `GET /reservations/{id}/health` (owner, 15 s cache) + `POST ΓÇª/health/probe` ΓåÆ probe job | `api.py` | B-65, B-20 | api tests; the probe result is recorded on the job | Rama | done |
| B-67 | Engine evaluates health for `active` reservations each tick, stores the latest snapshot, fires the webhook on ΓåÆ `down` | `engine.py`, `models.py` | B-65 | test: a flip to down fires once, not every tick | Akshay | done |
| B-68 | Mock telemetry: the mock runner starts a fake exporter per reservation with `daolab_reservation` labels; `MOCK_FAIL=health` drops it | `runner.py`, `deploy/otel/` | B-22, B-60 | badge data goes healthy ΓåÆ unknown/down when the mock stops | Rama | done |

## P6 ΓÇö Cleanup verification, reconciler, real playbooks

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-70 | `verify_clean(res)`: no `daolab-r<id>-*` VMs, VFs back to assignable, no DPU sessions + app stopped, leased IPs silent | `runner.py`, `playbooks/verify_clean.yml` | B-22 | returns a structured residue list (empty = clean) | Akshay | done |
| B-71 | Cleanup flow: teardown ΓåÆ verify ΓåÆ (clean: release leases + setup free) / (residue: reconcile ├ùN ΓåÆ quarantine setup + IPs) | `engine.py` | B-70, B-41, B-18 | mock test with `MOCK_FAIL=cleanup` ends in `quarantined` | Akshay | done |
| B-72 | Reconcile actions: `xe vm-uninstall` tagged VMs, stop DPU app, unbind VFs, kill sessions | `playbooks/reconcile.yml` | B-70 | removes residue left by an interrupted deploy | Akshay | done |
| B-73 | Orphan sweep every tick: tagged VMs / live leases whose reservation is terminal | `engine.py` | B-72 | test: a manual VM tagged `daolab-r999` gets cleaned | Rama | done |
| B-74 | `POST /admin/setups/{name}/force-clean` (reconcile + clear quarantine, audit-logged) | `api.py` | B-71, B-55 | admin-only; 403 for users | Rama | done |
| B-80 | `playbooks/deploy.yml` entry + roles: `lease_ip`, `ebf_set` (stub), `dpu_app`, `host_vm` (clone, VF passthrough, cloud-init static IP + owner key + otel agent), `verify` | `playbooks/` | B-42, B-52 | idempotent: a second run is all `ok`/`skipped` | Akshay | done |
| B-81 | Per-use-case DPU app roles: `vpp`, `tls_proxy_nginx`, `smartnic` | `playbooks/roles/` | B-80 | each passes its YAML `verify` on tb-01 | Akshay | done |
| B-82 | `playbooks/cleanup.yml`: best-effort teardown (VMs, VFs, DPU app stop, session kill; hw_class kept) | `playbooks/` | B-80 | safe to run on an already-clean testbed | Rama | done |
| B-83 | OTel agent install (VMs via cloud-init, DPU via role) with `daolab.*` resource attributes | `playbooks/roles/otel_agent` | B-60 | metrics labelled by reservation in Prometheus | Rama | done |
| B-84 | `runner.py` ansible backend via `ansible-runner` (extravars, TRACEPARENT, per-job artifact dir) | `runner.py` | B-22, B-80 | real job logs linked from `GET /jobs/{id}` | Akshay | done |
| B-85 | Testbed bring-up: dom0 pciback for E810 VFs, template `tpl-ubuntu2204-dao`, DPU sshd drop-in, host_vars BDFs | `inventory/host_vars/` | B-04 | manual checklist in [05-provisioning.md](05-provisioning.md) ticked | Rama | blocked ΓÇö the DPU half is done on xeon3-oct-112 (`playbooks/bringup.yml`, host_vars); the XCP-ng half needs the lab's two hosts (10.68.0.52 / .53) in the inventory, see `mistakes.md` A1 / F1 |
| B-86 | End-to-end on tb-01: alice one-click VPP ΓåÆ ready ΓåÆ healthy ΓåÆ slot end ΓåÆ released; bob blocked from the DPU throughout | ΓÇö | all P6 | demo recorded; trace in Jaeger | Akshay | blocked ΓÇö the DPU path runs end to end from the dashboard on xeon3-oct-112 (VPP, 2026-10-16) and the whole flow on the mock runner (`tests/test_e2e_tb01.py`, `tests/test_dashboard_e2e.py`); the VMs need B-85 |

## Docs follow-up

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-90 | Fold the design changes back into 02ΓÇô05 (mode/409, cleaning/quarantined, single deploy.yml, leased IPs, one-host inventory) | `plan/02ΓÇô05` | P1ΓÇôP3 | no doc contradicts 09 | Rama | done |
| B-91 | Ops runbook: quarantine handling, force-clean, OTel stack ops, rotating the internal secret | `plan/08-cli-config-ops.md` | P6 | reviewed by whoever is on lab duty | Rama | done |

## P7 ΓÇö Pending (from the 2026-10-23 re-check, `mistakes.md`)

Open work found by re-checking the plan audit against the code (`mistakes.md`, sections
AΓÇôP). Owner `ΓÇö` = not assigned yet.

| ID | Task | Files | Depends | Done when | Owner | Status |
|---|---|---|---|---|---|---|
| B-92 | The real lab in the inventory: two XCP-ng hosts (`tg-01-xcp` 10.68.0.52 with the E810 VFs on PF `0000:01:00.1`, `dpu-01-xcp` 10.68.0.53) instead of the placeholder `tb-01-xcp`; host_vars (SR, networks, templates, `e810_vfs`); the setup YAML wired to them; VM prefix, gateway, DNS in `group_vars` | `inventory/`, `setups/`, `plan/05`, `plan/09` | ΓÇö | `bringup.yml` passes against both hosts; `mistakes.md` A1, A7, F1, F3, D8 closed | ΓÇö | todo |
| B-93 | VMs through Xen Orchestra: the two pools added in XO (`scripts/run/xo.sh`), an XO API token in a file (`DAOLAB_XO_TOKEN_FILE`); the VM roles create, start and destroy VMs through XO's API instead of `xe` over SSH | `playbooks/roles/host_vm`, `cleanup.yml`, `orphans.yml`, `service/config.py` | B-92 | a `vpp` deploy with `DAOLAB_DEPLOY_VMS=true` gets `tgen` + `sink` with leased IPs; End now removes them (seen in XO) | ΓÇö | todo |
| B-94 | A booking must not start on a setup whose failed reservation still has its cleanup pending (`mistakes.md` P1) | `service/engine.py`, `service/scheduler.py`, tests | ΓÇö | test: a deploy fails out ΓåÆ a due booking on the same setup waits until that cleanup is verified | ΓÇö | todo |
| B-95 | TLS use case gates: `expect` that `h2load` really prints (`"1000 succeeded"`); `verify` checks the rc and a parsed field, not a substring; an httpcheck target for the proxy VM and a rule filtered on `http_status_class="2xx"` | `usecases/tls-proxy-nginx.yml`, `roles/verify`, `roles/host_vm`, `roles/otel_agent` | B-93 | `mistakes.md` B1, B2, I9, P2 closed | ΓÇö | todo |
| B-96 | A functional gate for DPU-only deploys (VPP: `eth0` up with its address, not only `show version`); a wall-clock limit per playbook (`DAOLAB_JOB_TIMEOUT_SEC` handed to ansible-runner) | `playbooks/apps/dao-vpp`, `service/runner.py` | ΓÇö | `mistakes.md` I10, P3, P4 closed; a hung fake playbook fails its job within the timeout | ΓÇö | todo |
| B-97 | `POST /internal/vm-ip` accepts only the reservation's own leased address while it is deploying or live; the IP conflict check uses arping, not ICMP ping | `service/api.py`, `roles/lease_ip` | B-93 | `mistakes.md` J10, K4, P5, P6 closed | ΓÇö | todo |
| B-98 | Fold plan/09 into the rest of the doc set: `README.md`, `plan/README.md`, `plan/01`, `plan/06`, `plan/07`, `plan/08` (topology, playbooks, IP model, counts, the endpoint map, dangling "contract ┬º" references) | docs | B-92 | `mistakes.md` A2, A8, B8, B10, B13, C9, C14, I19, M4 closed | ΓÇö | todo |
| B-99 | SmartNIC on the real board: in Akshay's runbook `smartnic_build` / `smartnic_launch` check `pgrep -x vpp` (misses `vpp_main`), and the build prefix (`dao-clone/install`) does not match `smartnic_binary` (`dao-clean/install`) | `dao-smartnic/` | ΓÇö | SmartNIC deploys and cleans up from the dashboard on xeon3-oct-112; then add it to `DAOLAB_DEPLOYABLE` | Akshay | todo |
| B-100 | Password-mode deploy verified on the real board: log in as `lab` with the generated password, refused after End now | ΓÇö | ΓÇö | recorded run | ΓÇö | todo |
| B-101 | Operations: CI running the tests on every push; a DB backup timer; token expiry | `.github/workflows/`, `deploy/`, `service/models.py` | ΓÇö | `mistakes.md` K6, J15, J13 closed | ΓÇö | todo |

---

## Critical path and totals

```mermaid
graph LR
    P0["P0 foundations"] --> P1["P1 booking"]
    P0 --> P2["P2 jobs/engine"]
    P2 --> P3["P3 IPAM"]
    P2 --> P4["P4 access"]
    P0 --> P5a["B-60/61 OTel stack"]
    P2 --> P5["P5 health"]
    P5a --> P5
    P3 & P4 & P5 --> P6["P6 cleanup + real playbooks"]
    P6 --> E2E["B-86 end-to-end on tb-01"]
```

- **Critical path:** P0 ΓåÆ P2 ΓåÆ (P3, P4, P5) ΓåÆ P6.
- **Can run in parallel:** P1 alongside P2; B-60/B-61 (OTel stack) from the start; B-85
  (testbed bring-up) any time.
- **Mock-demo milestone (end of P5):** the whole flow works on a laptop with test users.
- **After P6 (P7):** B-92 ΓåÆ B-93 ΓåÆ (B-95, B-97, B-98) is the path to VMs on hardware; B-94,
  B-96, B-99, B-100 and B-101 can start now.
