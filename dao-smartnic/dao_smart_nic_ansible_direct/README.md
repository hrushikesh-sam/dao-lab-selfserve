# DAO SmartNIC Ansible - Direct dao-clone workflow

This version uses the existing `/home/dao-test/dao-clone` directory directly.

It does NOT:
- git clone DAO
- create `dao-clean`
- restore PCI drivers
- stop/restore VPP
- perform cleanup after the SmartNIC test

Workflow:
1. Build/configure existing `dao-clone`
2. Launch SmartNIC
3. Verify startup, VFIO, hugepages and telnet

Run the build playbook first, then the launch/verify playbook.

Restore is intentionally not included yet.
