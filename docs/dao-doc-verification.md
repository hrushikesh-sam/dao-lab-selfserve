# DAO docs vs. daolab: verification report

Date: 2026-11-18. Scope: every page of Marvell's DAO documentation that constrains the lab
(`gsg/`, `howtoguides/`, `applications/`), cross-checked against this repo's use-case YAMLs,
inventory, catalog and blocker doc. Includes Akshay's `origin/akshay` `02b6b3d` (TLS proxy
without SDP, B-102) merged the same day. Live facts are from the 2026-11-14 check recorded in
`docs/sdp-blocker.md` and from `dao-smartnic/Clean reinstall of dao-smart-nic on CN10K DPU (xeon3-oct-112).md`; no new root login was taken
during this pass, so items marked **UNVERIFIED** need one command on the board.

Doc base used: <https://marvellembeddedprocessors.github.io/dao/guides/>. The catalog's
`dao-devel/applications/` URLs resolve too (checked `vpp.html` there); no dead links found.

## 1. Minimum / maximum numbers the docs actually state

| Item | Documented value | Source |
|---|---|---|
| DAO compile: C compiler | GCC **14.0+** (C11 atomics) | `gsg/system.html` |
| Python / Meson / ninja | Python **3.6+**, Meson **0.53+**, ninja | `gsg/system.html` |
| DPDK | **25.11 or later** (`dpdk-25.11-cn10k`) | `gsg/system.html`, TLS app |
| pyelftools | **0.22+** | `gsg/system.html` |
| Host kernel for virtio/vDPA | **>= 6.5**, or **6.1 with DAO patches** (`patches/kernel/v6.1/vdpa/`) | `howtoguides/virtio_host.html` |
| IOMMU | **on** (`intel_iommu=on`) when passing VFs to a guest; SDP-DPDK perf page suggests `iommu=off` for that path only | `virtio_host.html`, `sdp_host.html` |
| Host SDP drivers | `octeon_ep.ko` + `octeon_ep_vf.ko` from `pcie_ep_octeon_host` (tag e.g. `v25.05`) | `sdp_host.html` |
| Host SDP PF / VF IDs | PF `177d:b900`, VF `177d:b903` | `sdp_host.html`, `virtio_host.html`, `vpp.html` |
| Octeon SDP PF / VF IDs | PF `177d:a0f6`, VF `177d:a0f7` (our board: PF **a065**, VFs **a0f7**) | `sdp_host.html`; `inventory/host_vars/dpu-01.yml:24` |
| Host SDP VF maximum | **Octeon SDP VFs - 1** (PF consumes VF0): ours 9 -> **8** | `sdp_host.html` §6.3 |
| `octep_cp_agent` prerequisites | PEM `a06c` + **two** SDP RVU PFs `a0ef` bound to vfio-pci; hugepages **12 x 2 MB** (or 256 x 2 MB); `/usr/bin/cn106xx.cfg` | `howtoguides/octep_agent.html` |
| CP agent start order | **before** loading `octeon_ep`/`octeon_ep_vf` on the host | `sdp_host.html` §6.4.2 |
| EBF menu scope | PEM0 only: PFs, VFs/PF, SDP PF rings, per-PF SDP VF rings, device/class IDs; host stream IDs default `0x30000`, all-streams `0x3ffff`; host memory access default deny | `howtoguides/pcie_config.html` |
| OVS offload per interface | **1 SDP VF + 1 RPM VF**, plus 1 ESW VF for the exception path; SDP PF not in datapath; cores: 1 control + 1/port | `applications/ovs-offload.html` |
| OVS offload dependency | `octep_cp_agent` must be **running** for every scenario | `applications/ovs-offload.html` §1.6 |
| Packet length / pcap | `--max-pkt-len` **64-9600**; pcap default **1024** packets/graph | `ovs-offload.html` |
| SmartNIC devices | RPM port(s) + SDP VFs (`a0f7`); example 1 RPM PF + 2 SDP VFs | `applications/smart-nic.html` |
| SmartNIC CLI | telnet default `0.0.0.0:8086`; `-p` overrides; `-s` CLI file mandatory | `smart-nic.html` §7.3 |
| SmartNIC features | Done: telnet CLI, port hairpinning. **Planned:** 100G virtio, MAC learning, NFV chaining. **Pending:** port hotplugging, 200G IPsec, ... | `smart-nic.html` §7.1.1 |
| VPP release | `vpp-25.02.0-cn10k` | `applications/vpp.html` |
| VPP host-offload bandwidth | **SDP: <= 50 Gbps** (NIX bandwidth in EP NIC mode); **virtio-over-PCIe: 100 Gbps** | `vpp.html` §8.7 |
| VPP virtio DMA devices | **2 + 2 + 2 x workers** (32 VFs in the example), NPA `a0fb` on vfio, RVU SDP `a0fe` pair | `vpp.html` §8.7.1.1 |
| VPP SDP DPU-side control plane | `octep_cp_plugin.so` + `/etc/vpp/octep_cp_cn10kxx.cfg`; SDP VF on vfio; host `octeon_ep` | `vpp.html` §8.8 |
| TLS proxy packages | `nginx-1.22.0-cn10k`, `openssl-engine-1.0.0-cn10k` (pulls `cpt-firmware`, `dpdk-25.11`, `openssl-1.1.1q`) | `applications/tls-proxy-nginx.html` |
| TLS proxy CPT VFs | **one VF per logical CPU** (commonly **24**), bound to vfio; CPT PF `0002:20:00.1` | `tls-proxy-nginx.html` §9.5.1 |
| TLS proxy shape | Three models (termination, forwarding, intercepting); backend is just an IP in `proxy_pass`; **no SDP/host requirement** | `tls-proxy-nginx.html` |
| Crypto agent platform | Runs **on the Liquid Crypto Card**, not on a CN10K DPU; gRPC control; CPT + optional ZIP | `applications/crypto-agent.html` |

## 2. Per-use-case verdict (Marvell doc vs. repo)

| Use case | Repo claim (file:line) | Doc reality | Verdict |
|---|---|---|---|
| `vpp` | `usecases/vpp.yml:4-11`: DPU VPP on `sdp-pf1-vf8`, tgen+sink on tg host with `e810-vf` | Doc demo is **host offload over PCIe**: SDP (<=50G) or virtio (100G), host `octeon_ep`/`octep_vdpa_pci`, DPU `octep_cp_plugin`. No E810 in the datapath. | Mismatch in framing; lab demo is VPP-on-RPM with an external E810 tgen, not the documented host-offload demo. `hw_class` is unused by the roles. |
| `smartnic` | `usecases/smartnic.yml:5-9`: `hw_class: sdp-pf1-vf8`, host VM `connect: virtio` | Doc SmartNIC = DPU graph app with RPM + **SDP VFs**; host reaches it through the **SDP PF netdev** (`octeon_ep`), not virtio. | Partly wrong: host VM should get an **SDP VF** with `octeon_ep_vf`, not a virtio NIC. Verify command is a placeholder. |
| `tls-proxy-nginx` | `usecases/tls-proxy-nginx.yml:11-16`: `crypto: cpt-vfs`; backend on `tg_host` over `connect: mgmt`; client on an E810 VF | Doc: NGINX on OCTEON with **CPT VFs = nproc** (24); backend is any IP via `proxy_pass`. No SDP. | Matches after Akshay's 2026-11-17 change (`e4b859b`, B-102); CPT VFs already bound (`dpu-01.yml:25-50`). Doc-conformant. |
| `ovs-offload` (planned) | Not implemented | Requires `octep_cp_agent` running, 1 SDP VF + 1 RPM VF + ESW VF per interface | No conflict between CP agent and OVS is documented; see §3. |
| `crypto-agent` (planned) | Catalog summary says "on the Liquid Crypto Card" | Correct, but the lab has no Liquid Crypto Card | Keep planned. |
| 11 remaining planned apps | service/catalog.py:33-127, verbatim doc intros | Intros match the doc text | Catalog text is faithful. |

## 3. Errors and contradictions found

### E1. `docs/sdp-blocker.md` conflates three different PCI functions

- **a065 / a0f6** = Octeon SDP **data-path PF** (`0002:01:00.0`, 9 VFs `a0f7` on our board).
  It is *supposed* to stay on `rvu_af`/`rvu_nicpf`; only its VFs move to `vfio-pci`.
- **a0ef** = the **two SDP RVU control-plane PFs** that `octep_cp_agent` binds to vfio
  (`howtoguides/octep_agent.html`). Our inventory does not record them at all — **UNVERIFIED**
  whether they exist on this firmware.
- **b900** = the **host-visible SDP PF** (`0000:6f:00.0` on dpu-01-xcp), the only thing
  `octeon_ep` drives.

The doc's claim "this board exposes only one SDP RVU PF; binding it to vfio breaks `rvu_af`"
therefore does not describe what the docs or our own runbook do. `ovs-offload` explicitly
**requires** `octep_cp_agent`; the SDP PF stays out of the datapath, and SDP VFs are normally
bound to vfio by both OVS-offload and SmartNIC.

### E2. The demonstrated blocker is the host kernel + missing driver, not a PF ownership conflict

`howtoguides/sdp_host.html` builds `octeon_ep.ko` from `pcie_ep_octeon_host`, and
`howtoguides/virtio_host.html` requires Linux **>= 6.5** (or 6.1 + DAO patches). XCP-ng 4.19
dom0 is outside the supported matrix, `modinfo octeon_ep` fails, the PF is held by `pciback`,
and `sriov_numvfs = 0`. That is the whole host-side blocker. Our own SmartNIC run
(`dao-smartnic/Clean reinstall of dao-smart-nic on CN10K DPU (xeon3-oct-112).md:411-438`) already bound the **nine SDP VFs** to vfio and
ran the app on the DPU, with the SDP PF left on `rvu_af`; no conflict occurred.

### E3. The 8-VF number is right, but for a different reason than "1 PF, 8 VFs"

Docs: the host PF consumes Octeon SDP **VF0**; host VF[n] <-> Octeon VF[n+1]; max host VFs =
9 - 1 = **8** (`sdp_host.html` §6.3). `sriov_totalvfs = 8` on `0000:6f:00.0` matches. The
comment "SDP, 1 PF, 8 VFs" is fine; the assumption that the board only exposes one SDP function
is not.

### E4. `plan/07-usecase-tls-proxy.md:152` and `README.md:49` overstate host readiness

The plan says dom0 "sees the SDP PF plus 8 VFs ... already claimed by pciback". Live state:
PF hidden by pciback, **zero VFs** (`sriov_numvfs = 0`). VFs can only be created after a
supported `octeon_ep` is loaded in dom0 (or pre-created on the DPU, which the docs do not
describe for the host side).

### E5. TLS proxy does not need SDP — fixed in `e4b859b`

The original `usecases/tls-proxy-nginx.yml` sent decrypted HTTP to a backend VM over SDP. The
doc only requires an HTTP(S) upstream reachable by IP. Akshay's 2026-11-17 change moves the
backend to a `tg_host` VM on the management network (`connect: mgmt`), so the CPT-offload demo
no longer needs host SDP VFs. `plan/07` still carries the old SDP path in its body with a top
note marking it as the original plan; only the note is current.

### E6. SmartNIC's `connect: virtio` is the wrong guest model

SDP VFs on a host are `octeon_ep_vf` netdevs (`sdp_host.html` §6.4.3). Virtio is a separate
vDPA path (`octep_vdpa_pci.ko`, `vdpa`/`vhost-vdpa`/`virtio_vdpa`, `virtio_host.html`) and is
not what `dao-smart-nic` uses. The host VM should attach an SDP VF and load `octeon_ep_vf`.

### E7. VPP demo is valid but mislabeled relative to the doc

VPP on the DPU with RPM ports and a separate E810-driven tgen is a working DPDK forwarding
test, but the doc's VPP sections are about **host offload to the DPU** over SDP (<=50G) or
virtio (100G). Also, `hw_class: sdp-pf1-vf8` in `vpp.yml` is unused by any role, yet it forces
the EBF lead time; and `requires` omits `sdp`, which is what the doc path would need.

### E8. Catalog duplicates SmartNIC

`service/catalog.py:78` seeds `smartnic-turnkey` (planned) while `usecases/smartnic.yml` adds
`smartnic` (active). `tests/test_api.py:88` locks this in as 17 rows. The same Marvell app
("Universal SmartNIC Turnkey App") therefore appears twice, once as planned, which is
misleading now that it deploys.

### E9. Smaller items

- `sdp_host.html` says all CNXK SDP PFs are `a0f6`; our board reports `a065`. Scripts must not
  match on `a0f6` alone (ours use BDFs, so we are fine).
- `octep_cp_agent` config sample shows **64 VFs/PF**; our board has 9 — VF count is per-firmware
  (EBF), not fixed.
- SmartNIC: port hotplugging is **Pending** in Marvell's own list; do not advertise it.
- OVS: `--max-pkt-len` max is 9600; our planned MTU/verify values should stay <= 9600.
- `crypto-agent` is a Liquid Crypto Card app; the DPU board cannot run it.

## 4. Commands to close the UNVERIFIED items (one root session on dpu-01)

```sh
lspci -nn | grep -E '177d:(a0ef|a0f6|a0f7|a065|a06c)'
ls -l /usr/bin/octep_cp_agent /usr/bin/cn106xx.cfg
dpdk-devbind.py -s | grep -E 'a0ef|a065|a0f7|a06c'
cat /etc/daolab/hw_class
```

- Two `a0ef` entries + `a06c` -> CP agent path can be tried exactly as documented.
- No `a0ef` -> per `octep_agent.html` that means incompatible firmware; that is a Marvell
  question, not a scripting bug.
- On dpu-01-xcp: `lspci -nn | grep 177d`, `cat /sys/bus/pci/devices/0000:6f:00.0/sriov_numvfs`,
  `uname -r`.

## 5. Recommended changes (in order)

1. Rewrite `docs/sdp-blocker.md` (done in the same commit as this report): kernel/driver cause,
   correct PCI-function roles, answerable questions only.
2. `usecases/smartnic.yml`: `connect: sdp` for `hostvm`; verify via the SDP interface; keep the
   placeholder clearly marked until VFs exist.
3. `usecases/vpp.yml`: decide whether the demo is "VPP on DPU" (drop `hw_class`, keep E810
   tgen) or "documented host offload" (add `sdp`, host VM with SDP VF, `octep_cp_plugin`).
4. Resolve the `smartnic-turnkey` / `smartnic` duplicate (rename the seed row or drop it and
   update `tests/test_api.py`).
5. `plan/07` body still describes the SDP backend; trim it or keep the historical note.
6. Proposed device-allocation schema task is **B-104** (B-103 is the Python/core/age floor and CI; B-102 is Akshay's TLS end-to-end).
7. B-60's otel issues are resolved on this branch: `otel-collector-contrib:0.160.0` exists
   (verified with `docker manifest inspect`), and `tcpcheck` is a contrib receiver in that line.
