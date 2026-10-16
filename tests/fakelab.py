#!/usr/bin/env python3
"""A fake testbed for running the real playbooks in tests (tests/test_playbooks.py).

Installed under the names of the commands the playbooks call (xe, xl, pgrep, pkill, loginctl,
systemctl, ping, arping), it answers from one JSON state file ($FAKELAB_STATE) and appends
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
    if args[:1] == ["-x"]:                                 # pgrep -x <name>: is it running
        hit = args[1] in state.get("procs", [])
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
                # starting a unit the playbook just installed (any non-glob name)
                if any(c in n for c in "*?["):
                    fail(f"Failed to {verb} {n}: Unit {n} not found.", 5)
                hits = [n if n.endswith(".service") else n + ".service"]
                units[hits[0]] = "inactive"
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
    if args[:1] == ["daemon-reload"]:
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
    if "vpp" not in state.get("procs", []):
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


def ping(args):
    state = load()
    ip = args[-1]
    sys.exit(0 if ip in state["pingable"] else 1)


def arping(args):
    ping(args)


def main():
    name = os.path.basename(sys.argv[0])
    log([name, *sys.argv[1:]])
    handler = {"xe": xe, "xl": xl, "pgrep": pgrep, "pkill": pkill, "loginctl": loginctl,
               "systemctl": systemctl, "ping": ping, "arping": arping,
               "pktgen": traffic, "h2load": traffic, "which": which, "vppctl": vppctl,
               "dpdk-devbind.py": devbind, "fakeproc": fakeproc}.get(name)
    if handler is None:
        fail(f"fakelab: no fake for {name}", 2)
    handler(sys.argv[1:])


if __name__ == "__main__":
    main()
