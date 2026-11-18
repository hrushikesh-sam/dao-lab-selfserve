# SDP blocker: host side needs a supported kernel and driver

Status of the remaining hardware blocker for B-85 (testbed bring-up) and B-86 (recorded
end-to-end demo). Facts below come from the DAO docs (`howtoguides/sdp_host.html`,
`howtoguides/octep_agent.html`, `howtoguides/virtio_host.html`, `applications/vpp.html`), the
live board (`dpu-01`, 10.68.0.190), its host `dpu-01-xcp` (10.68.0.53) and our own SmartNIC
runbook (`dao-smartnic/Clean reinstall of dao-smart-nic on CN10K DPU (xeon3-oct-112).md`).
Full cross-check: `docs/dao-doc-verification.md`.

## What works today

- VPP runs end to end on the real CN10K board from one button: VPP on the DPU, traffic VMs
  created through Xen Orchestra on E810 SR-IOV VFs, traffic test passing (4/4, 0% loss),
  automatic cleanup back to a clean testbed.
- DAO SmartNIC deploys, passes its checks and cleans up on the DPU. Our run bound the board's
  nine SDP VFs (`0002:01:00.1` - `0002:01:01.1`, `a0f7`) to `vfio-pci` and left the SDP PF
  (`0002:01:00.0`, `a065`) on `rvu_af`; that is exactly what the docs expect.
- Access control verified on the board: per-slot login (SSH key or generated password),
  revoked at slot end.
- Hardening in place: leased-IP enforcement with an arping conflict check, verified cleanup
  with quarantine on residue, automatic retries, nightly DB backups, token expiry and
  revocation.

## The blocker

The DPU side is fine; the **host side of SDP has never been brought up**. The docs require:

- host kernel **>= 6.5**, or **6.1 with DAO's `patches/kernel/v6.1/vdpa/`** (`virtio_host.html`);
- `octeon_ep.ko` + `octeon_ep_vf.ko` built from `pcie_ep_octeon_host` (`sdp_host.html`);
- `octep_cp_agent` running on the DPU **before** the host driver is loaded, with PEM `a06c`
  and the **two** SDP RVU PFs `a0ef` bound to `vfio-pci`;
- IOMMU on for VFs passed to a guest.

Live state of `dpu-01-xcp`: SDP PF `0000:6f:00.0` (`177d:b900`) is held by `xen-pciback`,
`sriov_totalvfs = 8`, `sriov_numvfs = 0`, `modinfo octeon_ep` fails, no `octeon_ep`/`rvu`
modules, dom0 is the **4.19** line. So no host SDP VFs exist and no host VM can get one.

The 8 is correct: Octeon exposes nine SDP VFs and the host PF consumes VF0, so the maximum
host VFs is 9 - 1 = 8 (`sdp_host.html` §6.3).

### What this is not

It is not a "single SDP RVU PF" conflict and not an `rvu_af` vs `octep_cp_agent` standoff.
Those are three different PCI functions:

| Function | Where | Role |
|---|---|---|
| `a065` (doc: `a0f6`) | DPU, `0002:01:00.0` | SDP data-path PF; stays on `rvu_af`/`rvu_nicpf`; its `a0f7` VFs are what apps bind to vfio |
| `a0ef` (pair) | DPU, e.g. `0002:18:00.0`, `0002:19:00.0` | SDP RVU control-plane PFs that `octep_cp_agent` binds to vfio |
| `b900` | host, `0000:6f:00.0` | host-visible SDP PF that `octeon_ep` drives |

`applications/ovs-offload.html` explicitly **requires `octep_cp_agent` running**; SDP VFs move
to vfio for both OVS-offload and SmartNIC, and the SDP PF stays out of the datapath. Our
SmartNIC run already did this successfully, so the "mutually exclusive" theory is wrong.

## What is needed to unblock

1. Board-owner approval for one dom0 change + reboot of the shared host.
2. Build/install a host driver for XCP-ng's 4.19 dom0 (or host the SDP consumer on a supported
   kernel); unbind `0000:6f:00.0` from `xen-pciback` for the driver probe, then create and
   re-hide the 8 VFs in `pciback` (one-time `/etc/modprobe.d/pciback.conf`).
3. On the DPU: start `octep_cp_agent` with `cn106xx.cfg`, PEM `a06c` and the two `a0ef` PFs
   (hugepages 12 x 2 MB), **before** loading the host driver.
4. Confirm with Marvell that this firmware actually exposes the `a0ef` pair and a host-side
   `octeon_ep` build for 4.19 is supported (see questions).

## Questions for Marvell

1. `lspci -nn | grep a0ef` on this board: our firmware is a CN106 variant whose SDP PF shows
   `a065`, while the docs say `a0f6`. What are the `a0ef` control-plane PFs called on this
   firmware, and is `octep_cp_agent` supported on it (vs. the VPP `octep_cp_plugin`)?
2. Is `pcie_ep_octeon_host` (e.g. tag `v25.05`) expected to build and run on an **XCP-ng 4.19
   dom0**, or is the >= 6.5 / 6.1-patched requirement mandatory for the SDP netdev path too
   (not just vDPA)? If 4.19 is out, is a pre-created-VF / host-passthrough mode supported?
3. With nine Octeon SDP VFs, is 8 host VFs the hard maximum, and is anything else required on
   the DPU (EBF `sdp-pf1-vf8`, SDP VF rings) for host VFs to appear?
4. For a SmartNIC host VM on an SDP VF, does the guest use `octeon_ep_vf` (netdev) or a virtio
   function? The virtio path in the VPP docs uses `octep_vdpa_pci`/`vdpa` instead — which is
   recommended under Xen?

## Minor items still open

- Clearer error for the SmartNIC host VM when SDP is unavailable.

## Live check, 2026-11-14

All three hosts reply to ping.

**tg-01-xcp 10.68.0.52 (root):**
- `xl pci-assignable-list` shows 8 E810 VFs: `0000:01:11.0` - `0000:01:11.7`. `host_vars`
  listed only .0 - .6; `0000:01:11.7` was added.
- PF `0000:01:00.1` is on the `ice` driver with `sriov_numvfs = 128`.
- Pool `6769f0a6-7f4e-88ae-801e-648d0f5d0650`, SR `73946000-676f-3f7d-a117-50e7aecc7009`,
  template `tpl-ubuntu2204-dao` = `d9acd7a1-44f9-5747-b7e1-23e7f2a0560e`, network
  `SRIOV-E810` = `5886a435-4a7b-a75d-e1dd-8246a6e456aa`, `Pool-wide network 0` =
  `61f86fd2-53a6-6ef2-28e8-036b2eebcc14` - all match `inventory/host_vars/tg-01-xcp.yml`.
- `modinfo octeon_ep` fails (expected: this host has no SDP device).

**dpu-01-xcp 10.68.0.53 (root):**
- `0000:6f:00.0` is `Cavium 177d:b900`, bound to `pciback`, `sriov_totalvfs = 8`,
  `sriov_numvfs = 0` - the host-side SDP PF is hidden but no VFs exist.
- Template `eb8ecdeb-f97a-fbb0-1d4f-c896ac3a17d0`, SR
  `df51fda3-fee4-e466-b70f-b6886d357ea9`, network
  `Pool-wide network associated with eth0` = `644da301-fe91-ba5f-f6d5-b98b1696e051` - all
  match `inventory/host_vars/dpu-01-xcp.yml`.
- `modinfo octeon_ep` fails and `lsmod` shows no `octeon_ep` / `rvu` - there is no
  SR-IOV-capable PF driver in the dom0 kernel, so `sriov_numvfs` cannot be written there.
  This confirms the host-side half of the blocker.

**dpu-01 10.68.0.190 (`dao-test`):**
- Ubuntu 22.04.5, `lab` user (uid 1001), `/etc/daolab/{authorized_keys,hw_class}` present,
  `octeontx2-sdp` / `rvu_af` / `rvu_nicpf` / `rvu_cptpf` / `vfio-pci` drivers present,
  `octep_cp_agent` inactive, no `vpp_main`, hugepages 0 - the board is idle and clean.
- Sudo regression fixed: the only rule was `(ALL : ALL) ALL` (password required), so
  `become: true` would have failed. Added `/etc/sudoers.d/90-daolab` with
  `dao-test ALL=(ALL) NOPASSWD: ALL`, `visudo -c` passes, `sudo -n true` OK.
- The sshd drop-in on the board is `10-daolab-lab.conf` (generated by the `dpu_access`
  role: `AuthorizedKeysFile /etc/daolab/authorized_keys/%u`), not the unused
  `playbooks/files/sshd-daolab.conf` / `daolab-keys` pair.
