# SDP blocker: one SDP RVU PF on the DPU

Status of the remaining hardware blocker for B-85 (testbed bring-up) and B-86 (recorded
end-to-end demo). Facts below come from the live board (`dpu-01`, 10.68.0.190) and its host
`dpu-01-xcp` (10.68.0.53).

## What works today

- VPP runs end to end on the real CN10K board from one button: VPP on the DPU, traffic VMs
  created through Xen Orchestra on E810 SR-IOV VFs, traffic test passing (4/4, 0% loss),
  automatic cleanup back to a clean testbed.
- DAO SmartNIC deploys, passes its checks and cleans up on the DPU.
- Access control verified on the board: per-slot login (SSH key or generated password),
  revoked at slot end.
- Hardening in place: leased-IP enforcement with an arping conflict check, verified cleanup
  with quarantine on residue, automatic retries, nightly DB backups, token expiry and
  revocation.

## The blocker

Host-side SDP needs `octep_cp_agent` running on the DPU. The host's `octeon_ep` driver
handshakes with it, and `octep_cp_agent` requires the DPU's SDP RVU PF to be bound to
`vfio-pci`.

- This board exposes **only one SDP RVU PF**.
- Binding it to `vfio-pci` (for `octep_cp_agent`) breaks the mailbox path through `rvu_af`
  that OVS offload depends on.
- Leaving it with `rvu_af` keeps OVS working but `octep_cp_agent` cannot use it, so the
  host-side SDP endpoint never comes up and no SDP VFs are available for VMs.

Net effect: host-side SDP (SmartNIC host VM, TLS backend VM) and `rvu_af`-dependent apps
(OVS offload) look mutually exclusive on this hardware. This appears to be a resource
limitation of the board configuration, not a scripting issue.

## Secondary (host side, XCP-ng)

- SDP PF on the host: `0000:6f:00.0` (`177d:b900`), bound to `xen-pciback`,
  `sriov_totalvfs = 8`, `sriov_numvfs = 0`. Writing `sriov_numvfs` failed earlier.
- Likely cause: no SR-IOV-capable PF driver (`octeon_ep`) in XCP-ng's dom0 kernel (4.19
  line). Being verified.
- Instantiating and hiding the VFs needs one dom0 reboot of a shared host, so it needs
  owner approval.

## Questions for Marvell

1. With a single SDP RVU PF, how should `octep_cp_agent` (needs `vfio-pci`) coexist with
   apps that rely on `rvu_af` ownership (OVS offload)? Is a second SDP PF, a different
   board personality / EBF setting, or a firmware config expected?
2. Is `sdp-pf1-vf8` the right configuration for passing 8 SDP VFs to host VMs, and what
   else must be enabled on the DPU for them to come up?
3. Can SDP VFs be instantiated on a host whose kernel has no `octeon_ep` (XCP-ng dom0, 4.19
   line)? For example an out-of-tree build, or VFs pre-created from the DPU side.
4. For the DAO SmartNIC virtio path, what does the guest see on an SDP VF
   (`octeon_ep_vf` or a virtio-net function / `octep_vdpa`), and which guest driver is
   recommended?

## Minor items still open

- SmartNIC cleanup leaves hugepages reserved.
- Clearer error for the SmartNIC host VM when SDP is unavailable.
