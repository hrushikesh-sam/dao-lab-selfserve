#!/usr/bin/env python3
"""A fake testbed for running the real playbooks in tests (tests/test_playbooks.py).

Installed under the names of the commands the playbooks call (daolab-xo, the B-93 Xen
Orchestra client; xe, xl; pgrep, pkill, loginctl, systemctl, ping, arping), it answers from one JSON state file ($FAKELAB_STATE) and appends
every call to $FAKELAB_LOG. Only the command forms the playbooks use are implemented; anything
else exits 2 so a playbook that drifts from them fails loudly.

State:
  vms        {name: {uuid, power, other_config: {}, xenstore: {}, vifs: [], ip}}
  templates  {name-label: uuid}
  networks   {name-label: uuid}
  assignable [bdf, ...]                 VFs in pciback and not attached to a running VM
  lab_procs  int                        processes of the DPU lab login
  units      {unit: "active" | "inactive"}
  pingable   [ip, ...]                  addresses something answers on
  files      {path: text}               files the DPU-side commands are asked about
"""
import json
import os
import re
import sys
import uuid as uuidlib


def load():
    with open(os.environ["FAKELAB_STATE"]) as fh:
        return json.load(fh)


def save(state):
    tmp = os.environ["FAKELAB_STATE"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, os.environ["FAKELAB_STATE"])


def log(argv):
    with open(os.environ["FAKELAB_LOG"], "a") as fh:
        fh.write(json.dumps(argv) + "\n")


def kv(args):
    """xe-style key=value args -> dict (flags like --minimal are kept as key: True)."""
    out = {}
    for a in args:
        if a.startswith("--"):
            out[a] = True
        elif "=" in a:
            k, v = a.split("=", 1)
            out[k] = v
    return out


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def vm_by_uuid(state, u):
    for name, vm in state["vms"].items():
        if vm["uuid"] == u:
            return name, vm
    fail(f"The uuid you supplied was invalid.\ntype: VM\nuuid: {u}")


def _release_vf(state, vm):
    pci = vm.get("other_config", {}).get("pci")
    if pci and vm["power"] == "running":
        bdf = pci.split("/", 1)[1]
        if bdf not in state["assignable"]:
            state["assignable"].append(bdf)


def xe(args):
    state = load()
    cmd, a = args[0], kv(args[1:])
    if cmd == "vm-list":
        names = [n for n in state["vms"] if a.get("name-label", n) == n]
        if a.get("params") == "name-label":
            print(",".join(names) if a.get("--minimal") else "\n".join(names))
        elif a.get("params") == "uuid":
            print(",".join(state["vms"][n]["uuid"] for n in names))
        elif a.get("--minimal"):
            print(",".join(state["vms"][n]["uuid"] for n in names))
        else:
            fail(f"fakelab: unsupported vm-list form {args}", 2)
        return
    if cmd == "template-list":
        u = state["templates"].get(a.get("name-label"), "")
        print(u)
        return
    if cmd == "network-list":
        print(state["networks"].get(a.get("name-label"), ""))
        return
    if cmd == "vm-clone":
        tpl = a["uuid"]
        if tpl not in state["templates"].values():
            fail(f"The uuid you supplied was invalid.\ntype: VM\nuuid: {tpl}")
        name = a["new-name-label"]
        if name in state["vms"]:
            fail(f"fakelab: a VM named {name} already exists", 2)
        u = str(uuidlib.uuid4())
        state["vms"][name] = {"uuid": u, "power": "halted", "other_config": {}, "xenstore": {},
                              "vifs": [], "ip": None}
        save(state)
        print(u)
        return
    if cmd == "vm-param-set":
        _, vm = vm_by_uuid(state, a["uuid"])
        for k, v in a.items():
            if k.startswith("other-config:"):
                vm["other_config"][k.split(":", 1)[1]] = v
            elif k.startswith("xenstore-data:"):
                vm["xenstore"][k.split(":", 1)[1]] = v
        save(state)
        return
    if cmd == "vm-param-get":
        _, vm = vm_by_uuid(state, a["uuid"])
        if a.get("param-name") == "networks" and a.get("param-key") == "0/ip":
            if not vm.get("ip"):
                fail("Key 0/ip not found in map")
            print(vm["ip"])
        elif a.get("param-name") == "power-state":
            print(vm["power"])
        elif a.get("param-name") == "other-config" and a.get("param-key"):
            v = vm["other_config"].get(a["param-key"])
            if v is None:
                fail(f"Key {a['param-key']} not found in map")
            print(v)
        else:
            fail(f"fakelab: unsupported vm-param-get {args}", 2)
        return
    if cmd == "vif-list":
        _, vm = vm_by_uuid(state, a["vm-uuid"])
        print(",".join(v["uuid"] for v in vm["vifs"] if v["device"] == a.get("device", v["device"])))
        return
    if cmd == "vif-create":
        _, vm = vm_by_uuid(state, a["vm-uuid"])
        u = str(uuidlib.uuid4())
        vm["vifs"].append({"uuid": u, "device": a["device"], "network": a["network-uuid"]})
        save(state)
        print(u)
        return
    if cmd == "vm-start":
        _, vm = vm_by_uuid(state, a["uuid"])
        if vm["power"] == "running":
            fail("The operation could not be performed because the VM is running.")
        pci = vm["other_config"].get("pci")
        if pci:
            bdf = pci.split("/", 1)[1]
            if bdf not in state["assignable"]:
                fail(f"PCI device {bdf} is not assignable")
            state["assignable"].remove(bdf)
        vm["power"] = "running"
        # first boot: cloud-init applies the static address it was given (B-80)
        vm["ip"] = vm["xenstore"].get("vm-data/ip") or state.get("dhcp_ip")
        save(state)
        return
    if cmd == "vm-shutdown":
        _, vm = vm_by_uuid(state, a["uuid"])
        if vm["power"] != "running":
            fail("The operation could not be performed because the VM is halted.")
        _release_vf(state, vm)
        vm["power"] = "halted"
        save(state)
        return
    if cmd == "vm-uninstall":
        name, vm = vm_by_uuid(state, a["uuid"])
        _release_vf(state, vm)
        del state["vms"][name]
        save(state)
        print("All objects destroyed")
        return
    fail(f"fakelab: unsupported xe command {args}", 2)


def xl(args):
    state = load()
    if args == ["pci-assignable-list"]:
        print("\n".join(state["assignable"]))
        return
    if args[:1] == ["pci-assignable-add"]:
        if args[1] not in state["assignable"]:
            state["assignable"].append(args[1])
            save(state)
        return
    if args[:1] == ["pci-assignable-remove"]:
        state["assignable"] = [b for b in state["assignable"] if b != args[1]]
        save(state)
        return
    fail(f"fakelab: unsupported xl command {args}", 2)


def pgrep(args):
    state = load()
    if args[:1] == ["-x"]:                                 # pgrep -x <regex>: full name match
        hit = any(re.fullmatch(args[1], name) for name in state.get("procs", []))
        if hit:
            print(4242)
        sys.exit(0 if hit else 1)
    if args[:1] == ["-u"] and "-c" in args:
        print(state["lab_procs"])
        sys.exit(0 if state["lab_procs"] else 1)
    fail(f"fakelab: unsupported pgrep {args}", 2)


def pkill(args):
    state = load()
    if args[:1] == ["-u"] or args[:2] == ["-KILL", "-u"]:
        had = state["lab_procs"]
        state["lab_procs"] = 0
        save(state)
        sys.exit(0 if had else 1)
    fail(f"fakelab: unsupported pkill {args}", 2)


def loginctl(args):
    state = load()
    if args[:1] == ["terminate-user"]:
        if not state["lab_procs"]:
            fail(f"Failed to terminate user {args[1]}: User {args[1]} is not logged in.")
        state["lab_procs"] = 0
        save(state)
        return
    fail(f"fakelab: unsupported loginctl {args}", 2)


def _match(pattern, unit):
    import fnmatch
    return fnmatch.fnmatch(unit, pattern) or fnmatch.fnmatch(unit, pattern + ".service")


def systemctl(args):
    state = load()
    units = state["units"]
    if args[:1] == ["list-units"]:
        pattern = args[1]
        active = "--state=active" in args
        for u, st in sorted(units.items()):
            if _match(pattern, u) and (st == "active" or not active):
                print(f"{u} loaded {st} running fake unit")
        return
    if args[:1] in (["stop"], ["disable"], ["start"], ["enable"], ["restart"]):
        verb = args[0]
        now = "--now" in args
        names = [a for a in args[1:] if not a.startswith("-")]
        for n in names:
            hits = [u for u in units if _match(n, u)]
            if not hits and verb in ("start", "enable", "restart"):
                # a unit the playbook just installed: like systemd, only one whose file exists
                # (in $FAKELAB_UNIT_DIR, when the test says where the playbooks put them)
                unit = n if n.endswith(".service") else n + ".service"
                unit_dir = os.environ.get("FAKELAB_UNIT_DIR")
                if any(c in n for c in "*?[") or (
                        unit_dir and not os.path.exists(os.path.join(unit_dir, unit))):
                    fail(f"Failed to {verb} {unit}: Unit {unit} not found.", 5)
                hits = [unit]
                units[unit] = "inactive"
            for u in hits:
                if verb == "stop" or (verb == "disable" and now):
                    units[u] = "inactive"
                elif verb in ("start", "restart") or (verb == "enable" and now):
                    units[u] = "active"
        save(state)
        return
    if args[:1] == ["is-active"]:
        st = units.get(args[1]) or units.get(args[1] + ".service") or "inactive"
        print(st)
        sys.exit(0 if st == "active" else 3)
    if args[:1] in (["daemon-reload"], ["reload"]):
        return
    if args[:1] == ["cat"]:
        name = args[1] if args[1].endswith(".service") else args[1] + ".service"
        if name not in units:
            fail(f"No files found for {name}.", 1)
        print(f"# /etc/systemd/system/{name}")
        return
    fail(f"fakelab: unsupported systemctl {args}", 2)


def traffic(args):
    """pktgen / h2load: print what the test says the traffic run answers (state "traffic")."""
    state = load()
    name = os.path.basename(sys.argv[0])
    print(state.get("traffic", {}).get(name, ""))


def which(args):
    """`which <cmd>`: only the commands the state says are installed exist."""
    state = load()
    found = [a for a in args if a in state.get("installed", [])]
    for a in found:
        print(f"/usr/bin/{a}")
    sys.exit(0 if found and len(found) == len(args) else 1)


def vppctl(args):
    state = load()
    if not any(re.fullmatch("vpp|vpp_main", n) for n in state.get("procs", [])):
        fail("clib_socket_init: connect: No such file or directory")
    print("vpp v24.09.0-cn10k built by fakelab")


def devbind(args):
    state = load()
    if args[:1] == ["-s"]:
        for bdf, drv in sorted(state.get("bindings", {}).items()):
            print(f"{bdf} 'fake device' drv={drv} unused=")
        return
    if args[:1] == ["-b"]:
        for bdf in args[2:]:
            state.setdefault("bindings", {})[bdf] = args[1]
        save(state)
        return
    fail(f"fakelab: unsupported dpdk-devbind.py {args}", 2)


def fakeproc(args):
    """Test helper for stand-in playbooks: `fakeproc start|stop|install <name>`."""
    state = load()
    verb, name = args[0], args[1]
    procs, installed = state.setdefault("procs", []), state.setdefault("installed", [])
    if verb == "start" and name not in procs:
        procs.append(name)
    elif verb == "stop" and name in procs:
        procs.remove(name)
    elif verb == "install" and name not in installed:
        installed.append(name)
    save(state)


def _users(state):
    return state.setdefault("users", {})


def getent(args):
    state = load()
    if args[:1] == ["passwd"] and args[1] in _users(state):
        print(f"{args[1]}:x:1001:1001::/home/{args[1]}:/bin/bash")
        return
    sys.exit(2)


def useradd(args):
    state = load()
    name = args[-1]
    if name in _users(state):
        fail(f"useradd: user '{name}' already exists", 9)
    _users(state)[name] = {"password": None, "locked": True, "expires": None}
    save(state)


def usermod(args):
    state = load()
    name = args[-1]
    if name not in _users(state):
        fail(f"usermod: user '{name}' does not exist", 6)
    if "-L" in args:
        _users(state)[name]["locked"] = True
    if "-U" in args:
        if _users(state)[name]["password"] is None:
            fail("usermod: unlocking the user's password would result in a passwordless account.")
        _users(state)[name]["locked"] = False
    save(state)


def chpasswd(args):
    state = load()
    for line in sys.stdin.read().splitlines():
        name, _, pw = line.partition(":")
        if name not in _users(state):
            fail(f"chpasswd: line 1: user '{name}' does not exist")
        _users(state)[name]["password"] = pw
        _users(state)[name]["locked"] = False
    save(state)


def chage(args):
    state = load()
    name = args[-1]
    if name not in _users(state):
        fail(f"chage: user '{name}' does not exist in /etc/passwd")
    _users(state)[name]["expires"] = args[args.index("-E") + 1]
    save(state)


def visudo(args):
    path = args[-1]
    text = open(path).read()
    sys.exit(0 if re.search(r"^\S+ ALL=\(ALL\) NOPASSWD:ALL$", text, re.M) else 1)


def sshd(args):
    if args == ["-t"]:
        sys.exit(1 if load().get("sshd_broken") else 0)
    fail(f"fakelab: unsupported sshd {args}", 2)


def ping(args):
    state = load()
    ip = args[-1]
    sys.exit(0 if ip in state["pingable"] else 1)


def arping(args):
    ping(args)


def _opts(args):
    """argparse-style --key value pairs (repeatable --tag kept as a list)."""
    out, i = {"tag": []}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and i + 1 < len(args):
            k = a[2:]
            if k == "tag":
                out["tag"].append(args[i + 1])
            else:
                out[k] = args[i + 1]
            i += 2
        else:
            i += 1
    return out


_POWER = {"running": "Running", "halted": "Halted"}


def daolab_xo(args):
    """The B-93 Xen Orchestra client (service/xo.py), faked on the same state as xe: every
    subcommand the playbooks use, with the real one's output shapes and exit codes."""
    state = load()
    cmd, o = args[0], _opts(args[1:])
    if cmd == "template":
        u = state["templates"].get(o.get("name"))
        if not u:
            fail(f"daolab-xo: template {o.get('name')!r} is not in pool {o.get('pool')}", 3)
        print(u)
        return
    if cmd == "network":
        u = state["networks"].get(o.get("name"))
        if not u:
            fail(f"daolab-xo: network {o.get('name')!r} is not in pool {o.get('pool')}", 3)
        print(u)
        return
    if cmd == "vms":
        rows = []
        for n, vm in sorted(state["vms"].items()):
            if "name" in o and n != o["name"]:
                continue
            if "prefix" in o and not n.startswith(o["prefix"]):
                continue
            rows.append({"id": vm["uuid"], "name": n, "power": _POWER.get(vm["power"], vm["power"]),
                         "ip": vm.get("ip") or "", "tags": vm.get("tags", [])})
        print(json.dumps(rows))
        return
    if cmd == "create":
        tpl = state["templates"].get(o["template"])
        if not tpl:
            fail(f"daolab-xo: template {o['template']!r} is not in pool {o.get('pool')}", 3)
        name = o["name"]
        if name in state["vms"]:
            fail(f"fakelab: a VM named {name} already exists", 2)
        vifs = [{"device": "0", "network": "template"}]
        if o.get("sriov-network"):
            net = state["networks"].get(o["sriov-network"])
            if not net:
                fail(f"daolab-xo: network {o['sriov-network']!r} is not in pool {o.get('pool')}", 3)
            vifs.append({"device": "1", "network": net})
        u = str(uuidlib.uuid4())
        state["vms"][name] = {"uuid": u, "power": "halted", "other_config": {}, "xenstore": {},
                              "vifs": vifs, "ip": None, "tags": o["tag"], "memory_fixed": True,
                              "template": tpl}
        save(state)
        print(json.dumps({"id": u}))
        return
    if cmd == "seed":
        _, vm = vm_by_uuid(state, o["vm"])
        with open(o["file"]) as fh:
            vm["xenstore"].update(json.load(fh))
        save(state)
        return
    if cmd == "start":
        _, vm = vm_by_uuid(state, o["vm"])
        if vm["power"] == "running":
            fail("XO POST /vms/x/actions/start: HTTP 500 VM_BAD_POWER_STATE")
        if any(v["network"] != "template" for v in vm["vifs"]):     # an SR-IOV VIF takes a VF
            free = [b for b in state["assignable"] if b in state.get("sriov_vfs", state["assignable"])]
            if not free:
                fail("XO POST /vms/x/actions/start: HTTP 500 NETWORK_SRIOV_INSUFFICIENT_CAPACITY")
            state["assignable"].remove(free[0])
            vm["other_config"]["pci"] = "0/" + free[0]
        vm["power"] = "running"
        # first boot: a seed-aware template applies the static address it was given (B-80)
        vm["ip"] = (vm["xenstore"].get("vm-data/ip") if not state.get("template_ignores_seed")
                    else None) or state.get("dhcp_ip")
        save(state)
        return
    if cmd == "power":
        _, vm = vm_by_uuid(state, o["vm"])
        print(_POWER.get(vm["power"], vm["power"]))
        return
    if cmd == "ip":
        _, vm = vm_by_uuid(state, o["vm"])
        if not vm.get("ip"):
            fail(f"daolab-xo: VM {o['vm']} reports no IPv4 address yet")
        print(vm["ip"])
        return
    if cmd == "destroy":
        name, vm = vm_by_uuid(state, o["vm"])
        _release_vf(state, vm)
        del state["vms"][name]
        save(state)
        return
    fail(f"fakelab: unsupported daolab-xo command {args}", 2)


def main():
    name = os.path.basename(sys.argv[0])
    log([name, *sys.argv[1:]])
    handler = {"xe": xe, "xl": xl, "pgrep": pgrep, "pkill": pkill, "loginctl": loginctl,
               "systemctl": systemctl, "ping": ping, "arping": arping,
               "pktgen": traffic, "h2load": traffic, "which": which, "vppctl": vppctl,
               "dpdk-devbind.py": devbind, "fakeproc": fakeproc, "getent": getent,
               "useradd": useradd, "usermod": usermod, "chpasswd": chpasswd, "chage": chage,
               "visudo": visudo, "sshd": sshd, "daolab-xo": daolab_xo}.get(name)
    if handler is None:
        fail(f"fakelab: no fake for {name}", 2)
    handler(sys.argv[1:])


if __name__ == "__main__":
    main()
