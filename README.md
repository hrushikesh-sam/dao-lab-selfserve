# daolab — DAO use-case reservation system

Reserve a **setup** — a wired group of lab nodes — pre-provisioned for a Marvell
[DAO](https://marvellembeddedprocessors.github.io/dao/) use case. FCFS with time-share on conflict.

The lab is **two XCP-ng hosts**: `tg-01-xcp` (10.68.0.52, Intel E810 traffic-generator side) + `dpu-01-xcp` (10.68.0.53, OCTEON CN106 DPU side) with the DPU board `dpu-01` (10.68.0.190). Orchestration uses `playbooks/deploy.yml` and `playbooks/cleanup.yml` with roles. Everything that describes hardware or a use case is **data** (YAML), so scale-out = more files, zero code changes.

## Running it — one click on the dashboard deploys on the lab board

The lab today: **tb-01** = the CN10K DPU **xeon3-oct-112** (static, `10.68.0.190`, operated as
`dao-test`) plus an XCP-ng host for the use cases' VMs, which get addresses from
**`10.68.1.0`–`10.68.2.200`**. A user opens the dashboard, clicks **Deploy** on a use case,
gives their SSH public key (pre-filled) or asks for a generated password, and gets
`ssh lab@10.68.0.190` once the slot is live; at the end of the slot the login is revoked and the
board is given back.

```
scripts/run/dev.sh --fresh          # venv + deps (incl. ansible), .env, DB, catalog, API + engine
                                    # prints tokens for alice / bob / dev-admin
# in .env, for the real board:
#   DAOLAB_RUNNER=ansible            the real playbooks (DAOLAB_RUNNER=mock: no hardware)
#   DAOLAB_ALLOW_DESTRUCTIVE=true    after the board owner's sign-off (VPP stops nginx-tls, rebinds PCI)
#   DAOLAB_DEPLOY_VMS=false          until the XCP-ng host is in inventory/hosts.yml
ansible-playbook -i inventory/hosts.yml playbooks/bringup.yml -e setup=tb-01 -e daolab_deploy_vms=false
daolab add-user <name> ~/.ssh/<their key>.pub     # one per user; prints their dashboard token
```

For a demo on the real board, `scripts/run/showcase.sh` does all of the above in one go: the
ansible runner, DPU only (`DAOLAB_DEPLOY_VMS=false`), VPP as the only deployable use case
(`DAOLAB_DEPLOYABLE=vpp`), one-minute slots so a click deploys within seconds, its own DB in
`var/showcase.db`. It needs SSH as `dao-test` to the board and, for its sudo, a file holding only
that password (`~/.daolab-become-dao-test`, chmod 600). `scripts/run/xo.sh` runs Xen Orchestra in
a container on `https://127.0.0.1:8443/` for the XCP-ng pools (add them in its UI).

## Status and pending work

Done: the backend tasksheet (59 of 61 tasks, [plan/10](plan/10-backend-tasksheet.md)), and VPP on
the real DPU end to end from the dashboard. History: [COMPLETION_LOG.md](COMPLETION_LOG.md).
Pending (tasksheet **P7**, B-92…B-101, from the re-check in [mistakes.md](mistakes.md)):

- **The VMs.** Put the lab's two XCP-ng hosts (10.68.0.52 / .53) in the inventory (B-92), then
  create the use cases' VMs through Xen Orchestra (B-93). Until then deploys are DPU-only.
- **Correctness.**
  - A booking can start over a failed reservation whose cleanup is pending (B-94).
  - DPU-only deploys have no functional gate, and a hung playbook blocks the engine (B-96).
  - `/internal/vm-ip` trusts any IP (B-97).
- **Use cases.** The TLS proxy's verify string and health rule (B-95); SmartNIC on the board
  (B-99); a password-mode deploy recorded on the board (B-100).
- **Docs and operations.** Fold plan/09 into the rest of the docs (B-98); CI, backups and token
  expiry (B-101).

Then `http://<daolab host>:8080/` (users) and `/admin.html` (admins). What runs where:
`playbooks/deploy.yml` → `playbooks/apps/<app>/` (Akshay's runbook playbooks in `VPP_ansible/`
and `dao-smartnic/`, imported unchanged) → `roles/dpu_access` (the `lab` login) → VMs;
`playbooks/cleanup.yml` undoes it. Operations (quarantine, force-clean, telemetry, secrets):
[plan/08-cli-config-ops.md](plan/08-cli-config-ops.md#operations-runbook-b-91).

## Domain model (the decoupling that matters)

- **Pool** — an XCP-ng resource pool. Admin-level grouping of hosts.
- **Resource** — one reservable piece of hardware. Has a `kind` (dpu | gpu | nic | compute) and
  a `capabilities` list. Lives on an XCP-ng host that can spin up VM templates next to it.
- **Setup** — the unit users reserve: member resources + `links` (how they are wired) + a
  mermaid diagram. Capabilities of a setup = union of member capabilities + link capabilities.
- **Use case** — one YAML per DAO application variant. Declares `requires` (capabilities a setup
  must offer) and `roles` (what to provision where: a DAO app on the DPU, VMs from templates on
  the hosts). Scheduler matches `usecase.requires` ↔ `setup.capabilities`; nothing else couples them.
- **Reservation** — user + use case (or DIY) + matched setup + time slot.
  Lifecycle: queued→provisioning→ready→active→cleaning→released (quarantined if cleanup leaves residue).

```
daolab/
├── usecases/            one YAML per DAO use-case variant (requires, roles, verify)
├── setups/              one YAML per reservable setup (members, links, mermaid diagram)
├── inventory/           Ansible inventory; host_vars hold PCI BDFs + current hw_class
    ├── playbooks/           deploy.yml / cleanup.yml + roles/ (lease_ip, dpu_app, host_vm, verify, otel_agent, ebf_set, ...)
    ├── service/
    │   ├── config.py        env-driven settings (.env.example)
    │   ├── schema.sql       users / pools / setups / resources / usecases / reservations / vms / jobs / ip_pools / ip_leases
    │   ├── models.py        sqlite helpers
    │   ├── catalog.py       load usecases/ + setups/ YAML, mirror into DB (`daolab sync`)
    │   ├── scheduler.py     pure placement (FCFS, slot-shift, capability match, hw_class lead time)
    │   ├── runner.py        runner wrapper (mock/ansible), one fn per transition
    │   ├── engine.py        lifecycle loop: queued→provisioning→ready→active→cleaning→released/quarantined/failed
    │   ├── api.py           FastAPI: catalog / setups / reservations / jobs / internal/* + dashboard
    │   └── cli.py           `daolab init | sync | add-user | reserve | status`
├── scripts/run/dev.sh   one-shot dev runner: venv + init + sync + dev users (prints tokens) + serve
├── dashboard/
│   ├── user.html        full use-case catalog + request (auto-match or DIY) + my reservations
│   └── admin.html       pools → setups → resources drill-down, topology diagrams, holds, queue, users
└── tests/               scheduler unit tests (run without hardware)
```

## Frontend — two pages, both dumb views over the API

- **user.html** — lists the **entire** DAO use-case catalog from `GET /usecases`; availability is
  computed per row (YAML exists + some setup satisfies `requires` + a free slot). Request flow:
  pick use case → scheduler auto-matches a setup, or **DIY** mode → pick a setup + a base VM
  template per host and get bare wired VMs with no DAO app. Shows my reservations with state,
  slot, and SSH details once ready.
- **admin.html** — pools → setups → member resources drill-down; per-setup topology diagram
  rendered from the mermaid block in its YAML (the page never hardcodes topology); current holds,
  DPU hw_class, reservation queue; user management (enable/disable lab users — creation stays in
  the CLI). Requires a `role=admin` token (`users.role`: admin | user).

## Use-case catalog (from the DAO application docs)

| use case | what it is | status |
|---|---|---|
| **tls-proxy-nginx** | NGINX TLS termination proxy, RSA/ECDSA offloaded to CPT | **active — separate-backend variant** |
| virtio-l2fwd | L2 forwarding between host virtio devices and wire | planned |
| ovs-offload | OVS packet-processing offload for wire↔VM | planned |
| crypto-agent | standalone crypto service with host interaction | planned |
| virtio-crypto | crypto acceleration exposed to VMs via virtio | planned |
| virtio-blkio | block-device emulation to VMs via virtio | planned |
| secgw-graph | security gateway: IPv4 routing + IPsec | planned |
| smartnic-turnkey | universal SmartNIC turnkey app | planned |
| vpp | FD.io VPP with crypto/IO acceleration | planned |
| ml-tvm | ML model inference via TVM | planned |
| snort-vectorscan | Snort IDS with VectorScan pattern matching | planned |
| k8s-cni-offload | Kubernetes CNI networking offload | planned |
| envoy | Envoy with accelerated TLS handshakes/crypto | planned |
| rdma | RDMA endpoint / termination modes | planned |
| nvmeof-target | NVMe-oF storage target on OCTEON | planned |

`diy` appears alongside these as a pseudo-use-case (bare setup, no DAO app). user.html renders
this table live — adding a YAML flips a row to available, no frontend change.

## Current use case — tls-proxy-nginx (separate backend)

NGINX runs **on the OCTEON** as TLS termination proxy (crypto → CPT VFs via the DPDK OpenSSL
engine). The decrypted upstream is a **separate backend web-server VM** on the DPU node's XCP-ng
host. The client is a VM on the TG node with an E810 VF, requesting the payload over HTTPS.

```
client VM (ab / h2load)     ──HTTPS:443──►   NGINX on OCTEON CN106      ──HTTP:80──►  backend VM (web server)
on tg-01 XCP-ng host,          25G wire      TLS termination; RSA/ECDSA    SDP /       on dpu-01 XCP-ng host,
E810 VF passthrough                          → CPT VFs (DPDK engine)      virtio-net   serves the payload
```

```yaml
# usecases/tls-proxy-nginx.yml — the shape every use case follows
name: tls-proxy-nginx
variant: separate-backend             # docs also describe: inbuilt-server
requires: [octeon-cn106, e810, wire]  # capability match only — no hostnames
hw_class: sdp-pf1-vf8
roles:
  proxy:   {on: dpu,      app: nginx-tls, crypto: cpt-vfs, upstream: backend}
  backend: {on: dpu_host, vm: tpl-ubuntu2204-dao, install: http-server, connect: sdp}
  client:  {on: tg_host,  vm: tpl-ubuntu2204-dao, install: [ab, h2load], connect: e810-vf}
verify: {from: client, cmd: "h2load -n1000 https://{proxy_ip}/", expect: "200: 1000"}
```

```yaml
# setups/setup-01.yml — the shape every setup follows
name: setup-01
pool: lab-1
members:
  dpu-01: {kind: dpu, capabilities: [octeon-cn106, sdp], host: dpu-01-xcp}
  tg-01:  {kind: nic, capabilities: [e810],              host: tg-01-xcp}
links:
  - {a: dpu-01.eth0, b: tg-01.e810p0, capability: wire}
diagram: |                            # mermaid, rendered verbatim on admin.html
  graph LR
    C[client VM · tg-01] -- HTTPS 443 / 25G --> P[NGINX · OCTEON CN106]
    P -- HTTP 80 / SDP virtio --> B[backend VM · dpu-01 host]
```

## Implementation pipeline

Each phase is independently useful. Do not start phase N+1 until phase N's checkpoint passes.

### Phase 0 — bootstrap (30 min)
```
python -m venv .venv && . .venv/bin/activate
make setup && cp .env.example .env
daolab init && make test            # scheduler tests pass, no hardware needed
```

### Phase 1 — hardware truth (the only risky phase)
> Built since: the four playbooks below became roles of one `playbooks/deploy.yml` (+
> `cleanup.yml`), and on the real board the DPU apps are run by Akshay's runbook playbooks
> (`VPP_ansible/`, `dao-smartnic/`); `playbooks/bringup.yml` checks a board is ready. See
> [plan/05](plan/05-provisioning.md). The steps below are the original manual bring-up.

Goal: run every playbook by hand, once, successfully, for tls-proxy-nginx.
1. On the DPU: `lspci -D`, `ls /sys/bus/platform/devices` → fill `inventory/host_vars/dpu-01.yml:bdf`.
2. On each XCP-ng host: `xl pci-assignable-list`, `xe sr-list` → fill `*_xcp.yml`.
3. One-time dom0 prep: add every VF you will ever pass through (E810 VFs on tg-01-xcp, SDP VFs on
   dpu-01-xcp) to `/etc/modprobe.d/pciback.conf`, reboot dom0 once.
4. Build one base template `tpl-ubuntu2204-dao` (xe-guest-utilities + first-boot script that reads
   `vm-data/user`, `vm-data/ssh_key` from xenstore and creates the user). Role software (http
   server / ab + h2load) is installed by `host_vm.yml` per the use case's `roles`, not baked in.
5. On the OCTEON: install `cpt-firmware-cn10k`, bind CPT VFs (= nproc) to vfio-pci, build DAO
   NGINX with the DPDK OpenSSL engine — this becomes `dpu_prepare.yml`. Implement
   `playbooks/roles/ebf_set` for your board, or leave the stub and flash the hw_class manually.
6. Run, in order, with `-e reservation_id=0 -e usecase=tls-proxy-nginx -e setup=setup-01
   -e user=<you> -e ssh_pubkey="$(cat ~/.ssh/id_ed25519.pub)"`:
   `ansible-playbook -i inventory/hosts.yml playbooks/dpu_prepare.yml`, then `host_vm.yml`,
   `verify.yml`, `teardown.yml`. (host_vm.yml posts VM IPs to the API — start `make api` first,
   or comment that task out for the manual run.)
Checkpoint: from the client VM, `h2load -n1000 https://<proxy_ip>/` returns 1000×200 and the
backend VM's access log shows plain-HTTP hits.

### Phase 2 — scheduling without hardware
```
daolab sync                                       # usecases/ + setups/ → DB
daolab add-user hrushi ~/.ssh/id_ed25519.pub      # prints token; --role admin for admins
make api                                          # http://localhost:8080
DAOLAB_TOKEN=<token> daolab reserve tls-proxy-nginx 2026-09-01T10:00 --hours 2
daolab status
```
Checkpoint: two overlapping reserve calls → second one is shifted to the next slot; reserving a
use case whose `requires` no setup meets → rejected naming the missing capability; user.html
shows the catalog with correct availability; admin.html renders setup-01's diagram.

### Phase 3 — automation loop
`make engine` (or install as a systemd timer running `python -m service.engine` every minute).
Checkpoint: a reservation created for "now" goes queued→provisioning→ready on its own and
admin.html shows setup-01 as held; at end_at it goes back to free.

### Phase 4 — harden and scale out
- Add remaining use-case YAMLs (copy `tls-proxy-nginx.yml`; change `requires`, `roles`, `verify`).
- Add a second setup YAML (even with fake resources) to prove N-setup capability matching.
- `verify.yml`: per-use-case traffic smoke driven by the YAML's `verify` block.
- Webhook on `ready` (one `httpx.post` in engine) so external automation can react.
- Move sqlite → Postgres only if you get >1 API worker.

## Design invariants (keep these true)
- Use cases and setups are data, never code. New app = one YAML; new setup = one YAML + inventory.
- Scheduler matches capabilities, never hostnames, and has no I/O other than the DB handle it is
  given → always unit-testable.
- Every playbook is idempotent → engine retry = run again.
- Passthrough setup (pciback) is one-time in dom0, never per reservation.
- DPU keeps its last hw_class after teardown; only a differing next use case triggers an EBF change.
- Dashboards are views over the API only; diagrams live in setup YAML, so no topology in frontend code.
