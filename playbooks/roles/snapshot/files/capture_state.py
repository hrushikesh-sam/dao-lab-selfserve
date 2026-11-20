#!/usr/bin/env python3
"""capture_state.py --sysfs /sys --bdfs '["0002:01:00.1", ...]' - print the board state a
workspace snapshot records (B-106), as JSON on stdout. Read-only.

For each allowlisted PCI device (setups.snapshot_bdfs): whether it exists, its driver and
driver_override, its vendor:device, and for an SR-IOV PF its sriov_numvfs / sriov_totalvfs -
except the RVU admin function and the PEMs, which are marked `refused` and not recorded.
For each hugepage size: the total and per-NUMA-node counts. A restore (B-110) replays only
what differs, only on the same hw_class, and only for these devices.

Runs on the DPU under the system python3 (standard library only).
"""
import argparse
import json
import os
import re
import sys

BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

# Devices a snapshot never records, whatever the allowlist says - so a restore (B-110), which
# replays only what was recorded, can never rebind them. Checked on the live board, not by
# address, so a typo in a setup YAML cannot get past it.
#   rvu_af / 177d:a065  the RVU admin function: every RVU PF and VF depends on it; on the CN10K
#                       board it is the SDP RVU PF the OVS mailbox needs (0002:01:00.0)
#   177d:a06c           a PEM (PCIe controller), one digit from the ML device (0001:00:10.0)
NEVER_DRIVERS = {"rvu_af": "the RVU admin function (every RVU PF/VF depends on it)"}
NEVER_IDS = {"177d:a065": "the RVU admin function (every RVU PF/VF depends on it)",
             "177d:a06c": "a PEM (PCIe controller)"}


def read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def as_int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def device(sysfs, bdf):
    d = os.path.join(sysfs, "bus", "pci", "devices", bdf)
    if not os.path.isdir(d):
        return {"present": False}
    link = os.path.join(d, "driver")
    driver = os.path.basename(os.readlink(link)) if os.path.islink(link) else None
    pci_id = (f"{(read(os.path.join(d, 'vendor')) or '0x?')[2:]}:"
              f"{(read(os.path.join(d, 'device')) or '0x?')[2:]}")
    why = NEVER_DRIVERS.get(driver) or NEVER_IDS.get(pci_id)
    if why:
        return {"present": True, "id": pci_id, "refused": f"never recorded: {why}"}
    override = read(os.path.join(d, "driver_override"))
    return {
        "present": True,
        "driver": driver,
        "driver_override": None if override in (None, "", "(null)") else override,
        "id": pci_id,
        "sriov_numvfs": as_int(read(os.path.join(d, "sriov_numvfs"))),
        "sriov_totalvfs": as_int(read(os.path.join(d, "sriov_totalvfs"))),
    }


def hugepages(sysfs):
    out = {}
    top = os.path.join(sysfs, "kernel", "mm", "hugepages")
    for name in sorted(os.listdir(top)) if os.path.isdir(top) else []:
        if name.startswith("hugepages-"):
            size = name[len("hugepages-"):]
            out[size] = {"nr": as_int(read(os.path.join(top, name, "nr_hugepages"))), "nodes": {}}
    nodes = os.path.join(sysfs, "devices", "system", "node")
    for node in sorted(os.listdir(nodes)) if os.path.isdir(nodes) else []:
        if not re.match(r"^node\d+$", node):
            continue
        for size in out:
            nr = as_int(read(os.path.join(nodes, node, "hugepages", f"hugepages-{size}",
                                          "nr_hugepages")))
            if nr is not None:
                out[size]["nodes"][node] = nr
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--sysfs", default="/sys")
    p.add_argument("--bdfs", default="[]", help="JSON list of PCI addresses")
    args = p.parse_args(argv)
    bdfs = json.loads(args.bdfs)
    bad = [b for b in bdfs if not (isinstance(b, str) and BDF.match(b))]
    if bad:
        print(f"capture_state: not PCI addresses: {bad}", file=sys.stderr)
        return 2
    json.dump({"version": 1,
               "bdfs": {b: device(args.sysfs, b) for b in bdfs},
               "hugepages": hugepages(args.sysfs)}, sys.stdout, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
