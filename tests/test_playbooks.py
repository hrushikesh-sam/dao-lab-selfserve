"""The real playbooks, run by real ansible-playbook against a fake testbed (tests/fakelab.py).

tb-01-xcp and dpu-01 are local connections; `xe`, `xl`, `pgrep`, `systemctl`, `ping`, ... are
the fakelab script, answering from a JSON state file. That exercises each playbook's logic
(which commands, in which order, what it reports, idempotency) without hardware; the commands'
real output formats are only checked on the testbed (B-85/B-86).

Skipped unless ansible-playbook is available: `pip install -e .[ansible]`, or point
DAOLAB_TEST_ANSIBLE_PLAYBOOK at one.
"""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_ANSIBLE = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
ANSIBLE = (os.environ.get("DAOLAB_TEST_ANSIBLE_PLAYBOOK") or shutil.which("ansible-playbook")
           or (_VENV_ANSIBLE if os.path.exists(_VENV_ANSIBLE) else None))
pytestmark = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed "
                                                    "(pip install -e .[ansible])")

FAKES = ("xe", "xl", "pgrep", "pkill", "loginctl", "systemctl", "ping", "arping",
         "pktgen", "h2load", "which", "vppctl", "dpdk-devbind.py", "fakeproc", "getent",
         "useradd", "usermod", "chpasswd", "chage", "visudo", "sshd")
E810_VFS = ["0000:5e:01.0", "0000:5e:01.1"]
SDP_VFS = ["0000:03:02.0", "0000:03:02.1"]
TPL_UUID = "11111111-tpl0-0000-0000-000000000000"
NET_UUID = "22222222-net0-0000-0000-000000000000"
MEMBERS = [{"name": "dpu-01", "kind": "dpu", "host": "tb-01-xcp"},
           {"name": "tg-01", "kind": "nic", "host": "tb-01-xcp"}]


def clean_state():
    return {"vms": {}, "templates": {"tpl-ubuntu2204-dao": TPL_UUID},
            "networks": {"Pool-wide network associated with eth0": NET_UUID},
            "assignable": E810_VFS + SDP_VFS, "lab_procs": 0, "units": {}, "pingable": [],
            "files": {}}


class Lab:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.state_path = tmp_path / "state.json"
        self.log_path = tmp_path / "calls.log"
        self.out_path = tmp_path / "out.json"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "fakelab"
        shutil.copy(os.path.join(REPO, "tests", "fakelab.py"), fake)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for name in FAKES:
            (bindir / name).symlink_to(fake)
        self.bindir = bindir
        self.set(clean_state())
        self.log_path.write_text("")
        inv = {"all": {"vars": {"ansible_connection": "local",
                                "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
                       "children": {
                           "xcp_hosts": {"hosts": {"tb-01-xcp": {
                               "e810_vfs": E810_VFS, "sdp_vfs": SDP_VFS,
                               "mgmt_network": "Pool-wide network associated with eth0",
                               "sr_uuid": "33333333-sr00"}}},
                           "dpus": {"hosts": {"dpu-01": {"ansible_host": "10.68.0.190"}}}}}}
        self.inventory = tmp_path / "hosts.json"
        self.inventory.write_text(json.dumps(inv))

    def set(self, state):
        self.state_path.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_path.read_text())

    def calls(self, name=None):
        rows = [json.loads(l) for l in self.log_path.read_text().splitlines() if l]
        return [r for r in rows if name is None or r[0] == name]

    def run(self, playbook, roles_path=None, **extravars):
        ev = {"reservation_id": 12, "setup": "tb-01", "usecase": "vpp", "user": "alice",
              "ssh_pubkey": "ssh-ed25519 AAAAalice alice@laptop", "diy": False,
              "daolab_members": MEMBERS, "daolab_output_file": str(self.out_path)}
        ev.update(extravars)
        evfile = self.dir / "extravars.json"
        evfile.write_text(json.dumps(ev))
        env = {**os.environ, "PATH": f"{self.bindir}:{os.environ['PATH']}",
               "FAKELAB_STATE": str(self.state_path), "FAKELAB_LOG": str(self.log_path),
               "ANSIBLE_CONFIG": os.path.join(REPO, "playbooks", "ansible.cfg"),
               "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCAL_TEMP": str(self.dir / "tmp"),
               "ANSIBLE_HOME": str(self.dir / "ansible-home")}
        if roles_path:
            env["ANSIBLE_ROLES_PATH"] = f"{roles_path}:{os.path.join(REPO, 'playbooks', 'roles')}"
        if self.out_path.exists():
            self.out_path.unlink()
        p = subprocess.run([ANSIBLE, "-i", str(self.inventory),
                            os.path.join(REPO, "playbooks", playbook), "-e", f"@{evfile}"],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=300)
        self.stdout = p.stdout + p.stderr
        return p.returncode

    def output(self):
        return json.loads(self.out_path.read_text())


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def dirty(state, rid=12):
    """What a half-torn-down reservation 12 leaves: a running VM holding a VF, a lab session,
    a running DPU app, and an address that still answers."""
    state["vms"][f"daolab-r{rid}-sink"] = {"uuid": "vm-sink", "power": "running",
                                           "other_config": {"pci": "0/0000:5e:01.1"},
                                           "xenstore": {}, "vifs": [], "ip": "10.30.0.101"}
    state["assignable"].remove("0000:5e:01.1")
    state["lab_procs"] = 3
    state["units"]["daolab-app-dao-vpp.service"] = "active"
    state["pingable"].append("10.30.0.101")
    return state


# ---- B-70: verify_clean.yml ------------------------------------------------------------

def test_verify_clean_on_a_clean_testbed_reports_nothing(lab):
    assert lab.run("verify_clean.yml", daolab_leases=["10.30.0.100"]) == 0, lab.stdout
    assert lab.output() == []
    assert lab.calls("xe") and lab.calls("xl") and lab.calls("ping")      # it did look


def test_verify_clean_reports_every_kind_of_residue(lab):
    lab.set(dirty(clean_state()))
    assert lab.run("verify_clean.yml", daolab_leases=["10.30.0.100", "10.30.0.101"]) == 0, lab.stdout
    assert sorted(lab.output()) == sorted([
        "vm:daolab-r12-sink", "vf:0000:5e:01.1", "dpu_session:lab",
        "dpu_app:daolab-app-dao-vpp.service", "ip:10.30.0.101"])


def test_verify_clean_ignores_other_reservations(lab):
    state = clean_state()
    state["vms"]["daolab-r120-sink"] = {"uuid": "vm-other", "power": "halted",
                                        "other_config": {}, "xenstore": {}, "vifs": [], "ip": None}
    state["vms"]["my-own-vm"] = {"uuid": "vm-mine", "power": "halted", "other_config": {},
                                 "xenstore": {}, "vifs": [], "ip": None}
    lab.set(state)
    assert lab.run("verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


def test_verify_clean_is_read_only(lab):
    lab.set(dirty(clean_state()))
    before = lab.state()
    lab.run("verify_clean.yml", daolab_leases=["10.30.0.101"])
    assert lab.state() == before


# ---- B-72: reconcile.yml ---------------------------------------------------------------

def test_reconcile_destroys_every_kind_of_residue_and_verify_then_agrees(lab):
    lab.set(dirty(clean_state()))
    assert lab.run("reconcile.yml") == 0, lab.stdout
    st = lab.state()
    assert st["vms"] == {}                                            # VM destroyed
    assert sorted(st["assignable"]) == sorted(E810_VFS + SDP_VFS)     # its VF is back
    assert st["lab_procs"] == 0                                       # sessions killed
    assert st["units"] == {"daolab-app-dao-vpp.service": "inactive"}  # app stopped
    st["pingable"] = []                                               # the VM was what answered
    lab.set(st)
    assert lab.run("verify_clean.yml", daolab_leases=["10.30.0.101"]) == 0, lab.stdout
    assert lab.output() == []


def test_reconcile_rebinds_a_vf_that_lost_pciback(lab):
    st = clean_state()
    st["assignable"].remove("0000:03:02.0")                           # e.g. a crash rebinding it
    lab.set(st)
    assert lab.run("reconcile.yml") == 0, lab.stdout
    assert "0000:03:02.0" in lab.state()["assignable"]
    assert ["xl", "pci-assignable-add", "0000:03:02.0"] in lab.calls("xl")


def test_reconcile_is_safe_on_a_clean_testbed_and_changes_nothing(lab):
    before = lab.state()
    assert lab.run("reconcile.yml") == 0, lab.stdout
    assert lab.state() == before
    assert not [c for c in lab.calls("xe") if c[1] in ("vm-shutdown", "vm-uninstall")]
    assert not lab.calls("loginctl") and not lab.calls("pkill")


def test_reconcile_leaves_other_reservations_and_foreign_vms_alone(lab):
    st = dirty(clean_state())
    for name in ("daolab-r120-sink", "daolab-r1-tgen", "admin-scratch"):
        st["vms"][name] = {"uuid": f"u-{name}", "power": "halted", "other_config": {},
                           "xenstore": {}, "vifs": [], "ip": None}
    lab.set(st)
    assert lab.run("reconcile.yml") == 0, lab.stdout
    assert sorted(lab.state()["vms"]) == ["admin-scratch", "daolab-r1-tgen", "daolab-r120-sink"]


def test_reconcile_destroys_exactly_the_named_orphans(lab):
    st = clean_state()
    for name in ("daolab-r999-x", "daolab-r998-y", "daolab-r12-keep"):
        st["vms"][name] = {"uuid": f"u-{name}", "power": "running", "other_config": {},
                           "xenstore": {}, "vifs": [], "ip": None}
    lab.set(st)
    assert lab.run("reconcile.yml", reservation_id=0,
                   daolab_vm_names=["daolab-r999-x", "daolab-r998-y"]) == 0, lab.stdout
    assert sorted(lab.state()["vms"]) == ["daolab-r12-keep"]


def test_reconcile_twice_in_a_row(lab):
    lab.set(dirty(clean_state()))
    assert lab.run("reconcile.yml") == 0, lab.stdout
    after_first = lab.state()
    assert lab.run("reconcile.yml") == 0, lab.stdout
    assert lab.state() == after_first


# ---- B-73: orphans.yml -------------------------------------------------------------------

def _vm(name, power="running"):
    return {"uuid": f"u-{name}", "power": power, "other_config": {}, "xenstore": {},
            "vifs": [], "ip": None}


def test_orphans_lists_every_daolab_tagged_vm(lab):
    st = clean_state()
    for name in ("daolab-r999-x", "daolab-r12-sink", "admin-scratch", "daolab-notes"):
        st["vms"][name] = _vm(name)
    lab.set(st)
    assert lab.run("orphans.yml") == 0, lab.stdout
    assert sorted(lab.output()) == ["daolab-r12-sink", "daolab-r999-x"]
    assert lab.state() == st                                          # list mode is read-only


def test_orphans_destroys_exactly_the_named_vms(lab):
    st = clean_state()
    for name in ("daolab-r999-x", "daolab-r998-y", "daolab-r12-sink"):
        st["vms"][name] = _vm(name, power="halted" if name.endswith("y") else "running")
    lab.set(st)
    assert lab.run("orphans.yml", daolab_vm_names=["daolab-r999-x", "daolab-r998-y",
                                                   "daolab-r1-not-there"]) == 0, lab.stdout
    assert sorted(lab.state()["vms"]) == ["daolab-r12-sink"]


# ---- B-83: the otel_agent role ------------------------------------------------------------

def _role_play(lab, role, tasks_from="main", **role_vars):
    """A one-play wrapper that runs a role on dpu-01 (roles from playbooks/roles)."""
    import yaml
    play = [{"hosts": "dpu-01", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_role": {"name": role, "tasks_from": tasks_from},
         "vars": role_vars},
        {"ansible.builtin.copy": {"content": "{{ otel_agent_config_text | default('') }}",
                                  "dest": str(lab.dir / "rendered.yaml")},
         "delegate_to": "localhost"}]}]
    path = lab.dir / "wrapper.yml"
    path.write_text(yaml.safe_dump(play, sort_keys=False))
    return path


def _agent_vars(lab, **over):
    v = {"otel_agent_collector": "otel.lab:4317",
         "otel_agent_attributes": {"daolab.reservation": "12", "daolab.user": "alice",
                                   "daolab.role": "vpp", "daolab.setup": "tb-01",
                                   "host": "dpu-01"},
         "otel_agent_scrape_targets": [{"job": "vpp", "target": "127.0.0.1:9273"}],
         "otel_agent_bin": str(lab.dir / "otelcol-contrib"),
         "otel_agent_config": str(lab.dir / "etc" / "otelcol.yaml"),
         "otel_agent_unit_dir": str(lab.dir)}
    v.update(over)
    return v


def run_role(lab, path):
    return lab.run(str(path))


def test_otel_agent_renders_the_daolab_attributes_and_starts_the_unit(lab):
    import yaml
    (lab.dir / "otelcol-contrib").write_text("#!/bin/sh\n")        # "installed" already
    path = _role_play(lab, "otel_agent", **_agent_vars(lab))
    assert run_role(lab, path) == 0, lab.stdout
    cfg = yaml.safe_load((lab.dir / "etc" / "otelcol.yaml").read_text())
    attrs = {a["key"]: a["value"] for a in cfg["processors"]["resource"]["attributes"]}
    assert attrs == {"daolab.reservation": "12", "daolab.user": "alice", "daolab.role": "vpp",
                     "daolab.setup": "tb-01", "host": "dpu-01"}
    assert all(a["action"] == "upsert" for a in cfg["processors"]["resource"]["attributes"])
    jobs = {j["job_name"]: j["static_configs"][0]["targets"]
            for j in cfg["receivers"]["prometheus"]["config"]["scrape_configs"]}
    assert jobs == {"daolab-agent": ["127.0.0.1:8888"], "vpp": ["127.0.0.1:9273"]}
    assert cfg["exporters"]["otlp"] == {"endpoint": "otel.lab:4317", "tls": {"insecure": True}}
    assert cfg["service"]["pipelines"]["metrics/ssh"]["processors"][0] == "attributes/ssh"
    assert "httpcheck" not in cfg["receivers"]
    assert lab.state()["units"]["daolab-otel.service"] == "active"
    unit = (lab.dir / "daolab-otel.service").read_text()
    assert f"--config {lab.dir / 'etc' / 'otelcol.yaml'}" in unit


def test_otel_agent_second_run_restarts_nothing(lab):
    (lab.dir / "otelcol-contrib").write_text("#!/bin/sh\n")
    path = _role_play(lab, "otel_agent", **_agent_vars(lab))
    run_role(lab, path)
    restarts = len([c for c in lab.calls("systemctl") if c[1] == "restart"])
    assert run_role(lab, path) == 0, lab.stdout
    assert len([c for c in lab.calls("systemctl") if c[1] == "restart"]) == restarts


def test_otel_agent_without_a_collector_does_nothing(lab):
    path = _role_play(lab, "otel_agent", **_agent_vars(lab, otel_agent_collector=""))
    assert run_role(lab, path) == 0, lab.stdout
    assert not (lab.dir / "etc").exists() and not lab.calls("systemctl")


def test_otel_agent_render_only_for_cloud_init(lab):
    import yaml
    path = _role_play(lab, "otel_agent", tasks_from="render",
                      **_agent_vars(lab, otel_agent_scrape_targets=[], otel_agent_ssh_check=True,
                                    otel_agent_httpcheck=["https://10.30.0.100/"]))
    assert run_role(lab, path) == 0, lab.stdout
    cfg = yaml.safe_load((lab.dir / "rendered.yaml").read_text())
    assert cfg["receivers"]["httpcheck"]["targets"][0]["endpoint"] == "https://10.30.0.100/"
    assert "httpcheck" in cfg["service"]["pipelines"]["metrics"]["receivers"]
    assert not lab.calls("systemctl")                               # nothing installed


def test_otel_agent_accepts_a_single_url_for_httpcheck(lab):
    import yaml
    path = _role_play(lab, "otel_agent", tasks_from="render",
                      **_agent_vars(lab, otel_agent_httpcheck="https://10.30.0.100/"))
    assert run_role(lab, path) == 0, lab.stdout
    cfg = yaml.safe_load((lab.dir / "rendered.yaml").read_text())
    assert [t["endpoint"] for t in cfg["receivers"]["httpcheck"]["targets"]] == ["https://10.30.0.100/"]


# ---- B-80: deploy.yml ----------------------------------------------------------------------

import base64                                                     # noqa: E402
import types                                                      # noqa: E402
import threading                                                  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import yaml                                                       # noqa: E402

VPP_ROLES = {"vpp": {"on": "dpu", "app": "dao-vpp", "upstream": "sink"},
             "tgen": {"on": "tg_host", "vm": "tpl-ubuntu2204-dao", "install": ["trex", "pktgen"],
                      "connect": "e810-vf"},
             "sink": {"on": "dpu_host", "vm": "tpl-ubuntu2204-dao", "install": ["sink"],
                      "connect": "e810-vf"}}
VPP_VERIFY = {"from": "tgen", "cmd": "pktgen -t {sink_ip} --rate 10mpps", "expect": "rx == tx"}


@pytest.fixture
def api():
    """A fake daolab API for the playbook callbacks: leases (lowest free, idempotent),
    conflicts, vm-ip. Records every call."""
    st = {"calls": [], "leases": {}, "blocked": set(), "next": 100}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            st["calls"].append((self.path, self.headers.get("X-Daolab-Secret"), body))
            if self.path == "/internal/leases":
                key = (int(body["reservation_id"]), body["role"])
                if key not in st["leases"]:
                    st["leases"][key] = f"10.30.0.{st['next']}"
                    st["next"] += 1
                reply = {"ip": st["leases"][key], "role": body["role"]}
            elif self.path == "/internal/leases/conflict":
                key = next(k for k, v in st["leases"].items() if v == body["ip"])
                st["blocked"].add(body["ip"])
                st["leases"][key] = f"10.30.0.{st['next']}"
                st["next"] += 1
                reply = {"ip": st["leases"][key], "replaced": body["ip"]}
            else:
                reply = {"ok": True}
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    st["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield st
    httpd.shutdown()
    httpd.server_close()


VPP_BOOKS = ("03_install", "04_configure_02", "08_prepare_04", "09_configure_04",
             "10_verify_04", "11_cleanup_04")
SMARTNIC_BOOKS = ("01_build", "02_launch_verify", "03_cleanup")


@pytest.fixture
def books(tmp_path):
    """Stand-ins for Akshay's runbook playbooks (VPP_ansible/, dao-smartnic/): each one only
    records that it ran, so the tests check which steps a wrapper picks for a board state.
    Plus a fake sysfs whose live port (0002:04:00.0) sits on `driver`."""
    root = tmp_path / "books"
    ran = tmp_path / "books-ran.log"
    effect = {"03_install": "fakeproc install vpp", "10_verify_04": "fakeproc start vpp",
              "11_cleanup_04": "fakeproc stop vpp", "02_launch_verify": "fakeproc start dao-smart-nic",
              "03_cleanup": "fakeproc stop dao-smart-nic"}
    for sub, names in (("vpp", VPP_BOOKS), ("smartnic", SMARTNIC_BOOKS)):
        (root / sub).mkdir(parents=True)
        for name in names:
            cmd = f"echo {name} >> {ran}" + (f" && {effect[name]}" if name in effect else "")
            (root / sub / f"{name}.yml").write_text(yaml.safe_dump([{
                "hosts": "dpu", "gather_facts": False, "tasks": [
                    {"ansible.builtin.shell": cmd, "changed_when": False}]}]))
    sysfs = tmp_path / "sys"
    dev = sysfs / "bus" / "pci" / "devices" / "0002:04:00.0"
    dev.mkdir(parents=True)
    (sysfs / "bus" / "pci" / "drivers" / "rvu_nicpf").mkdir(parents=True)
    (sysfs / "bus" / "pci" / "drivers" / "vfio-pci").mkdir(parents=True)

    def port_on(driver):
        link = dev / "driver"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(sysfs / "bus" / "pci" / "drivers" / driver)

    port_on("rvu_nicpf")
    (dev / "driver_override").write_text("(null)\n")                  # after a reboot
    return types.SimpleNamespace(
        vars={"daolab_vpp_books": str(root / "vpp"), "daolab_smartnic_books": str(root / "smartnic"),
              "daolab_sysfs": str(sysfs), "smartnic_binary": str(tmp_path / "dao-smart-nic"),
              "restore_before": str(tmp_path / "smartnic-restore" / "pci-before.txt"),
              "app_exporter_bin": str(tmp_path / "daolab-app-exporter"),
              "app_exporter_unit_dir": str(tmp_path), "ansible_become": False,
              "dpu_access_root": str(tmp_path / "root")},
        ran=lambda: ran.read_text().split() if ran.exists() else [],
        port_on=port_on, binary=tmp_path / "dao-smart-nic", reset=lambda: ran.unlink(missing_ok=True),
        override=lambda: (dev / "driver_override").read_text().strip())


def deploy(lab, api, books, **over):
    marker = lab.dir / "hw_class"
    if not marker.exists():
        marker.write_text("sdp-pf1-vf8\n")
    ev = {"usecase": "vpp", "hw_class": "sdp-pf1-vf8", "daolab_roles": VPP_ROLES,
          "daolab_verify": VPP_VERIFY, "daolab_api_url": api["url"],
          "daolab_internal_secret": "s3", "daolab_otel_collector": "", "daolab_app": "dao-vpp",
          "daolab_deploy_vms": True, "allow_destructive_changes": True, "allow_launch": True,
          "allow_cleanup": True, "ebf_marker": str(marker),
          "host_vm_control_pubkey": "ssh-ed25519 AAAAdaolab daolab@engine",
          "host_vm_ip_retries": 2, "host_vm_ip_delay": 0, "daolab_vm_connection": "local",
          "daolab_no_log": False, **books.vars}
    ev.update(over)
    return lab.run("deploy.yml", **ev)


def with_traffic(lab, **out):
    st = lab.state()
    st["traffic"] = out
    lab.set(st)


def seed(vm, key):
    return base64.b64decode(vm["xenstore"][f"vm-data/daolab/{key}"]).decode()


def test_deploy_vpp_end_to_end(lab, api, books):
    with_traffic(lab, pktgen="tx 1000 rx 1000: rx == tx")
    assert deploy(lab, api, books) == 0, lab.stdout[-3000:]
    st = lab.state()
    # the VMs, on their leased addresses, sink and tgen sorted by role name
    assert sorted(st["vms"]) == ["daolab-r12-sink", "daolab-r12-tgen"]
    sink, tgen = st["vms"]["daolab-r12-sink"], st["vms"]["daolab-r12-tgen"]
    assert (sink["power"], sink["ip"], tgen["ip"]) == ("running", "10.30.0.100", "10.30.0.101")
    # deterministic VFs: k-th e810-vf role on tb-01-xcp takes e810_vfs[k]
    assert (sink["other_config"]["pci"], tgen["other_config"]["pci"]) == \
        ("0/0000:5e:01.0", "0/0000:5e:01.1")
    assert sorted(st["assignable"]) == sorted(SDP_VFS)
    assert [v["device"] for v in sink["vifs"]] == ["0"]
    # the cloud-init seed: owner key on the owner's login only, the service key elsewhere
    ud = yaml.safe_load(seed(sink, "user-data"))
    users = {u["name"]: u["ssh_authorized_keys"] for u in ud["users"]}
    assert users == {"alice": ["ssh-ed25519 AAAAalice alice@laptop"],
                     "daolab": ["ssh-ed25519 AAAAdaolab daolab@engine"]}
    assert ud["packages"] == ["iperf3"]
    net = yaml.safe_load(seed(sink, "network-config"))
    assert net["ethernets"]["mgmt0"]["addresses"] == ["10.30.0.100/24"]
    assert net["ethernets"]["data0"] == {"match": {"driver": "iavf"}, "set-name": "data0",
                                         "addresses": ["10.65.0.37/24"]}
    assert "pktgen-dpdk" in yaml.safe_load(seed(tgen, "user-data"))["packages"]
    # the DPU app: Akshay's VPP runbook steps for a board at rest, then its health exporter
    assert books.ran() == ["03_install", "04_configure_02", "08_prepare_04", "09_configure_04",
                           "10_verify_04"]
    assert st["units"]["daolab-app-exporter.service"] == "active"
    unit = (lab.dir / "daolab-app-exporter.service").read_text()
    assert "--app vpp --vpp-socket /run/vpp/cli.sock" in unit
    # the callbacks: leases for the VM roles, vm-ip for all three (the DPU: its static address)
    leases = [b["role"] for p, _, b in api["calls"] if p == "/internal/leases"]
    assert sorted(leases) == ["sink", "tgen"]
    posted = {b["role"]: b["ip"] for p, _, b in api["calls"] if p == "/internal/vm-ip"}
    assert posted == {"vpp": "10.68.0.190", "sink": "10.30.0.100", "tgen": "10.30.0.101"}
    assert all(secret == "s3" for _, secret, _ in api["calls"])
    # verify ran pktgen against the sink's DATA address
    assert ["pktgen", "-t", "10.65.0.37", "--rate", "10mpps"] in lab.calls("pktgen")


def test_deploy_twice_changes_nothing(lab, api, books):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, books) == 0, lab.stdout[-3000:]
    st = lab.state()                                   # what the first run left: VPP up on vfio
    st["installed"], st["procs"] = ["vpp"], ["vpp"]
    lab.set(st)
    books.port_on("vfio-pci")
    first = lab.state()
    assert deploy(lab, api, books) == 0, lab.stdout[-3000:]
    assert lab.state()["vms"] == first["vms"]
    recap = [l for l in lab.stdout.splitlines() if " : ok=" in l]
    assert recap and all("changed=0" in l for l in recap), "\n".join(recap)


def test_a_conflicting_address_is_swapped_before_use(lab, api, books):
    st = clean_state()
    st["pingable"] = ["10.30.0.100"]                      # something squats on the first lease
    st["traffic"] = {"pktgen": "rx == tx"}
    lab.set(st)
    assert deploy(lab, api, books) == 0, lab.stdout[-3000:]
    assert api["blocked"] == {"10.30.0.100"}
    assert {v["ip"] for v in lab.state()["vms"].values()} == {"10.30.0.101", "10.30.0.102"}


def test_a_failed_verify_fails_the_deploy_and_says_why(lab, api, books):
    with_traffic(lab, pktgen="tx 1000 rx 3")
    assert deploy(lab, api, books) != 0
    assert "verify failed: 'rx == tx' not in the output" in lab.stdout


def test_a_board_on_another_personality_stops_with_the_manual_flash_message(lab, api, books):
    (lab.dir / "hw_class").write_text("vf-only\n")
    assert deploy(lab, api, books) != 0
    assert "ebf_set stub: board is vf-only, use case needs sdp-pf1-vf8" in lab.stdout
    assert lab.state()["vms"] == {}                       # nothing cloned on a wrong board


def test_a_missing_template_is_named(lab, api, books):
    st = clean_state()
    st["templates"] = {}
    lab.set(st)
    assert deploy(lab, api, books) != 0
    assert "template tpl-ubuntu2204-dao is not on tb-01-xcp" in lab.stdout


def test_running_out_of_vfs_is_named(lab, api, books):
    assert deploy(lab, api, books, daolab_roles={**VPP_ROLES, "extra": {
        "on": "tg_host", "vm": "tpl-ubuntu2204-dao", "install": [], "connect": "e810-vf"}}) != 0
    assert "has no VF #2 in e810_vfs" in lab.stdout


def test_an_unknown_install_id_is_refused(lab, api, books):
    roles = {**VPP_ROLES, "sink": {**VPP_ROLES["sink"], "install": ["nope"]}}
    assert deploy(lab, api, books, daolab_roles=roles) != 0
    assert "install id 'nope' has no packages" in lab.stdout


def test_deploy_diy_makes_bare_vms_only(lab, api, books):
    assert deploy(lab, api, books, usecase="diy", diy=True, daolab_roles={}, daolab_app="none",
                  daolab_verify={}, templates={"tb-01-xcp": "tpl-ubuntu2204-dao"}) == 0, \
        lab.stdout[-3000:]
    st = lab.state()
    [(name, vm)] = st["vms"].items()
    assert name == "daolab-r12-tb-01-xcp" and vm["other_config"] == {} and vm["ip"] == "10.30.0.100"
    assert st["units"] == {} and not lab.calls("pktgen")
    assert "data0" not in yaml.safe_load(seed(vm, "network-config"))["ethernets"]


def test_a_manual_run_takes_its_addresses_and_calls_no_api(lab, api, books):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, books, reservation_id=0,
                  manual_ips={"sink": "10.30.0.190", "tgen": "10.30.0.191"}) == 0, lab.stdout[-3000:]
    assert api["calls"] == []
    assert lab.state()["vms"]["daolab-r0-sink"]["ip"] == "10.30.0.190"


# ---- B-84: probe.yml ---------------------------------------------------------------------

def test_probe_runs_verify_against_the_recorded_addresses(lab):
    with_traffic(lab, pktgen="rx == tx")
    vms = [{"role": "sink", "name": "daolab-r12-sink", "ip": "10.30.0.100"},
           {"role": "tgen", "name": "daolab-r12-tgen", "ip": "10.30.0.101"},
           {"role": "vpp", "name": None, "ip": "10.20.0.1"}]
    assert lab.run("probe.yml", daolab_roles=VPP_ROLES,
                   daolab_verify=VPP_VERIFY, daolab_vms=vms, daolab_vm_connection="local") == 0, \
        lab.stdout[-2000:]
    assert ["pktgen", "-t", "10.65.0.37", "--rate", "10mpps"] in lab.calls("pktgen")
    assert lab.state()["vms"] == {}                                    # read-only


def test_a_failing_probe_fails(lab):
    with_traffic(lab, pktgen="rx 0")
    vms = [{"role": "tgen", "name": "daolab-r12-tgen", "ip": "10.30.0.101"}]
    assert lab.run("probe.yml", daolab_roles=VPP_ROLES,
                   daolab_verify=VPP_VERIFY, daolab_vms=vms, daolab_vm_connection="local") != 0


# ---- B-81: the DPU apps run Akshay's runbook playbooks (apps/<app>/) ----------------------

def app_run(lab, books, playbook, **over):
    ev = {"usecase": "vpp", "daolab_roles": VPP_ROLES, "allow_destructive_changes": True,
          "allow_launch": True, "allow_cleanup": True, "daolab_otel_collector": "",
          **books.vars}
    ev.update(over)
    return lab.run(playbook, **ev)


def board(lab, installed=(), procs=()):
    st = lab.state()
    st["installed"], st["procs"] = list(installed), list(procs)
    lab.set(st)


def vpp_deploy(lab, books, **over):
    """deploy.yml for vpp with VMs off: play 0, ebf_set, the app, telemetry."""
    ev = {"daolab_app": "dao-vpp", "daolab_deploy_vms": False, "hw_class": "",
          "daolab_api_url": "http://127.0.0.1:1", "daolab_internal_secret": "x",
          "reservation_id": 0, **over}
    return app_run(lab, books, "deploy.yml", **ev)


@pytest.mark.parametrize("installed,running,port,steps", [
    (False, False, "rvu_nicpf", ["03_install", "04_configure_02", "08_prepare_04",
                                 "09_configure_04", "10_verify_04"]),          # board at rest
    (True, False, "rvu_nicpf", ["04_configure_02", "08_prepare_04", "09_configure_04",
                                "10_verify_04"]),                              # installed already
    (True, False, "vfio-pci", ["04_configure_02", "10_verify_04"]),            # died after 09
    (True, True, "vfio-pci", ["04_configure_02"]),                             # redeploy: VPP up
])
def test_vpp_deploy_runs_only_the_runbook_steps_the_board_needs(lab, books, installed, running,
                                                                port, steps):
    board(lab, installed=["vpp"] if installed else [], procs=["vpp"] if running else [])
    books.port_on(port)
    assert vpp_deploy(lab, books) == 0, lab.stdout[-3000:]
    assert books.ran() == steps
    assert "vpp" in lab.state()["procs"]
    assert lab.state()["units"]["daolab-app-exporter.service"] == "active"


def test_vpp_deploy_without_sign_off_touches_nothing(lab, books):
    board(lab, installed=["vpp"])
    assert vpp_deploy(lab, books, allow_destructive_changes=False) != 0
    assert "Set DAOLAB_ALLOW_DESTRUCTIVE=true" in lab.stdout
    assert books.ran() == []


def test_a_running_vpp_needs_no_sign_off_to_redeploy(lab, books):
    board(lab, installed=["vpp"], procs=["vpp"])
    books.port_on("vfio-pci")
    assert vpp_deploy(lab, books, allow_destructive_changes=False) == 0, lab.stdout[-2000:]


def test_vpp_cleanup_gives_the_board_back(lab, books):
    board(lab, installed=["vpp"], procs=["vpp"])
    books.port_on("vfio-pci")
    assert _cleanup(lab, books, "dao-vpp") == 0, lab.stdout[-2000:]
    assert books.ran() == ["11_cleanup_04"]
    assert "vpp" not in lab.state()["procs"]
    assert ["systemctl", "start", "nginx-tls.service"] in lab.calls("systemctl")


def _cleanup(lab, books, app):
    wrapper = lab.dir / f"cleanup-{app}.yml"
    wrapper.write_text(yaml.safe_dump([
        {"hosts": "localhost", "gather_facts": False, "tasks": [
            {"ansible.builtin.import_tasks": os.path.join(REPO, "playbooks", "tasks", "resolve.yml")},
            {"ansible.builtin.set_fact": {"daolab_playbooks": os.path.join(REPO, "playbooks")}}]},
        {"ansible.builtin.import_playbook": os.path.join(REPO, "playbooks", "apps", app,
                                                          "cleanup.yml")}]))
    return lab.run(str(wrapper), allow_destructive_changes=True, allow_cleanup=True,
                   daolab_roles=VPP_ROLES, **books.vars)


def test_vpp_cleanup_of_a_board_at_rest_only_makes_sure_nginx_runs(lab, books):
    board(lab, installed=["vpp"])
    assert _cleanup(lab, books, "dao-vpp") == 0, lab.stdout[-2000:]
    assert books.ran() == []
    assert ["systemctl", "start", "nginx-tls.service"] in lab.calls("systemctl")


@pytest.mark.parametrize("built,running,steps", [
    (False, False, ["01_build", "02_launch_verify"]),
    (True, False, ["02_launch_verify"]),
    (True, True, []),
])
def test_smartnic_deploy_builds_and_launches_only_when_needed(lab, books, built, running, steps):
    if built:
        books.binary.write_text("#!/bin/sh\n")
    board(lab, procs=["dao-smart-nic"] if running else [])
    assert vpp_deploy(lab, books, daolab_app="dao-smartnic", usecase="smartnic",
                      daolab_roles={"snapp": {"on": "dpu", "app": "dao-smartnic"}}) == 0, \
        lab.stdout[-3000:]
    assert books.ran() == steps
    assert "dao-smart-nic" in lab.state()["procs"]
    restore = books.vars["restore_before"]
    assert os.path.exists(restore) == (not running)          # runbook step 10 before a launch


def test_smartnic_cleanup_runs_the_runbook_cleanup(lab, books):
    board(lab, procs=["dao-smart-nic"])
    assert _cleanup(lab, books, "dao-smartnic") == 0, lab.stdout[-2000:]
    assert books.ran() == ["03_cleanup"] and "dao-smart-nic" not in lab.state()["procs"]


def test_tls_proxy_deploy_keeps_nginx_tls_running(lab, books):
    assert vpp_deploy(lab, books, daolab_app="nginx-tls", usecase="tls-proxy-nginx",
                      daolab_roles={"proxy": {"on": "dpu", "app": "nginx-tls"}}) == 0, \
        lab.stdout[-2000:]
    assert ["systemctl", "start", "nginx-tls.service"] in lab.calls("systemctl")
    assert "--app nginx" in (lab.dir / "daolab-app-exporter.service").read_text()


def test_a_board_without_a_personality_marker_is_assumed_to_match(lab, books):
    board(lab, installed=["vpp"], procs=["vpp"])
    books.port_on("vfio-pci")
    assert vpp_deploy(lab, books, hw_class="sdp-pf1-vf8",
                      ebf_marker=str(lab.dir / "no-marker")) == 0, lab.stdout[-2000:]
    assert "personality is not tracked yet" in lab.stdout


# ---- deploy access: the `lab` login (playbooks/roles/dpu_access) ---------------------------

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice alice@laptop"


def access_play(lab, tasks_from="main", **over):
    play = [{"hosts": "dpu-01", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_role": {"name": "dpu_access", "tasks_from": tasks_from}}]}]
    path = lab.dir / f"access-{tasks_from}.yml"
    path.write_text(yaml.safe_dump(play))
    ev = {"dpu_access_root": str(lab.dir / "root"), "ansible_become": False,
          "daolab_access_expires": "2026-10-17", **over}
    return lab.run(str(path), **ev)


def lab_user(lab):
    return lab.state()["users"]["lab"]


def root(lab, path):
    return lab.dir / "root" / path.lstrip("/")


def test_deploy_access_with_a_key(lab):
    assert access_play(lab, ssh_pubkey=KEY) == 0, lab.stdout[-2000:]
    u = lab_user(lab)
    assert (u["locked"], u["password"], u["expires"]) == (True, None, "2026-10-17")
    assert root(lab, "/etc/daolab/authorized_keys/lab").read_text() == KEY + "\n"
    assert root(lab, "/etc/sudoers.d/90-daolab-lab").read_text() == "lab ALL=(ALL) NOPASSWD:ALL\n"
    conf = root(lab, "/etc/ssh/sshd_config.d/10-daolab-lab.conf").read_text()
    assert "Match User lab" in conf and "PasswordAuthentication yes" in conf
    assert f"AuthorizedKeysFile {root(lab, '/etc/daolab/authorized_keys')}/%u" in conf


def test_deploy_access_with_a_generated_password(lab):
    assert access_play(lab, ssh_pubkey="", daolab_access_password="s3cretPassw0rd16") == 0, \
        lab.stdout[-2000:]
    u = lab_user(lab)
    assert (u["locked"], u["password"]) == (False, "s3cretPassw0rd16")
    assert root(lab, "/etc/daolab/authorized_keys/lab").read_text() == ""
    assert "s3cretPassw0rd16" not in lab.stdout                         # no_log


def test_deploy_access_twice_reports_no_change(lab):
    access_play(lab, ssh_pubkey=KEY)
    assert access_play(lab, ssh_pubkey=KEY) == 0
    recap = [l for l in lab.stdout.splitlines() if " : ok=" in l]
    assert all("changed=0" in l for l in recap), recap


def test_revoke_kills_sessions_and_shuts_every_door(lab):
    access_play(lab, ssh_pubkey="", daolab_access_password="s3cretPassw0rd16")
    st = lab.state()
    st["lab_procs"] = 4
    lab.set(st)
    assert access_play(lab, tasks_from="revoke") == 0, lab.stdout[-2000:]
    u = lab_user(lab)
    assert u["locked"] is True and u["expires"] == "0"
    assert u["password"] not in (None, "s3cretPassw0rd16") and len(u["password"]) == 32
    assert lab.state()["lab_procs"] == 0
    assert root(lab, "/etc/daolab/authorized_keys/lab").read_text() == ""


def test_revoke_without_the_login_is_harmless(lab):
    assert access_play(lab, tasks_from="revoke") == 0, lab.stdout[-2000:]
    assert "lab" not in lab.state().get("users", {})


SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)


@pytest.mark.skipif(SSHD is None, reason="sshd not installed")
def test_the_access_drop_in_opens_lab_only(lab, tmp_path):
    access_play(lab, ssh_pubkey=KEY)
    dropin = root(lab, "/etc/ssh/sshd_config.d/10-daolab-lab.conf")
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    main = tmp_path / "sshd_config"
    main.write_text(f"Include {dropin}\nHostKey {key}\nPasswordAuthentication no\n")

    def eff(user):
        p = subprocess.run([SSHD, "-T", "-f", str(main), "-C", f"user={user},host=x,addr=10.0.0.9"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            pytest.skip(p.stderr[:200])
        return dict(line.split(" ", 1) for line in p.stdout.splitlines() if " " in line)

    lab_cfg, root_cfg = eff("lab"), eff("root")
    assert lab_cfg["passwordauthentication"] == "yes"
    assert lab_cfg["authorizedkeysfile"].endswith("/etc/daolab/authorized_keys/%u")
    assert root_cfg["passwordauthentication"] == "no"                  # the board's own policy
    assert root_cfg["authorizedkeysfile"] != lab_cfg["authorizedkeysfile"]


# ---- B-82: cleanup.yml -------------------------------------------------------------------

def cleanup(lab, books, **over):
    ev = {"daolab_app": "none", "daolab_roles": VPP_ROLES, "allow_destructive_changes": True,
          "allow_cleanup": True, "dpu_access_root": str(lab.dir / "root"), **books.vars}
    ev.update(over)
    return lab.run("cleanup.yml", **ev)


def test_cleanup_revokes_access_first_and_destroys_the_vms(lab, books):
    access_play(lab, ssh_pubkey=KEY)
    st = dirty(lab.state())
    st["units"].pop("daolab-app-dao-vpp.service")
    lab.set(st)
    assert cleanup(lab, books) == 0, lab.stdout[-2000:]
    st = lab.state()
    assert st["vms"] == {} and sorted(st["assignable"]) == sorted(E810_VFS + SDP_VFS)
    assert st["lab_procs"] == 0 and lab_user(lab)["expires"] == "0"
    names = [l for l in lab.stdout.splitlines() if l.startswith("TASK [")]
    assert names.index(next(n for n in names if "terminate-user" in n)) < \
        names.index(next(n for n in names if "destroy_vms" in n))     # access ends first
    st["pingable"] = []
    lab.set(st)
    assert lab.run("verify_clean.yml") == 0 and lab.output() == []


def test_cleanup_runs_the_apps_runbook_cleanup(lab, books):
    board(lab, installed=["vpp"], procs=["vpp"])
    books.port_on("vfio-pci")
    assert cleanup(lab, books, daolab_app="dao-vpp") == 0, lab.stdout[-2000:]
    assert books.ran() == ["11_cleanup_04"] and "vpp" not in lab.state()["procs"]


def test_cleanup_is_safe_on_a_clean_testbed(lab, books):
    before = lab.state()
    assert cleanup(lab, books) == 0, lab.stdout[-2000:]
    assert lab.state()["vms"] == before["vms"]
    assert not [c for c in lab.calls("xe") if c[1] in ("vm-shutdown", "vm-uninstall")]


def test_verify_clean_sees_an_app_process_left_running(lab):
    board(lab, procs=["vpp"])
    assert lab.run("verify_clean.yml") == 0
    assert lab.output() == ["dpu_app:vpp"]


def test_deploy_gives_the_owner_the_lab_login(lab, api, books):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, books, ssh_pubkey=KEY,
                  dpu_access_root=str(lab.dir / "root")) == 0, lab.stdout[-3000:]
    assert root(lab, "/etc/daolab/authorized_keys/lab").read_text() == KEY + "\n"
    assert lab_user(lab)["locked"] is True


def test_a_password_deploy_reaches_the_vms_too(lab, api, books):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, books, ssh_pubkey="", daolab_access_password="Pw0rdPw0rdPw0rd1",
                  dpu_access_root=str(lab.dir / "root")) == 0, lab.stdout[-3000:]
    assert lab_user(lab)["password"] == "Pw0rdPw0rdPw0rd1"
    ud = yaml.safe_load(seed(lab.state()["vms"]["daolab-r12-sink"], "user-data"))
    owner = next(u for u in ud["users"] if u["name"] == "alice")
    assert owner["plain_text_passwd"] == "Pw0rdPw0rdPw0rd1" and owner["lock_passwd"] is False
    assert owner["ssh_authorized_keys"] == [] and ud["ssh_pwauth"] is True


# ---- DAOLAB_DEPLOY_VMS off: nothing reaches the XCP-ng host -------------------------------

def test_without_vms_verify_cleanup_and_reconcile_never_touch_the_dom0(lab, books):
    st = dirty(clean_state())                         # a VM exists, but VMs are off
    st["units"].pop("daolab-app-dao-vpp.service")
    st["lab_procs"] = 0
    lab.set(st)
    assert lab.run("verify_clean.yml", daolab_deploy_vms=False) == 0, lab.stdout[-2000:]
    assert lab.output() == []                         # only the DPU was checked
    assert cleanup(lab, books, daolab_deploy_vms=False) == 0, lab.stdout[-2000:]
    assert lab.run("reconcile.yml", daolab_deploy_vms=False, daolab_app="none",
                   **books.vars) == 0, lab.stdout[-2000:]
    assert not lab.calls("xe") and not lab.calls("xl")
    assert "daolab-r12-sink" in lab.state()["vms"]    # untouched


# ---- B-85: bringup.yml -------------------------------------------------------------------

def test_bringup_lists_everything_missing(lab):
    assert lab.run("bringup.yml", daolab_bringup_root=str(lab.dir / "root"),
                   ansible_become=False, hw_class="sdp-pf1-vf8") != 0
    assert "not ready yet" in lab.stdout
    assert "dpu-01: missing: nginx-tls.service" in lab.stdout
    assert "tb-01-xcp: host_vars/tb-01-xcp.yml: sr_uuid" not in lab.stdout   # inventory says ''
    assert "tb-01-xcp: missing template" not in lab.stdout             # the fake has it
    # the marker is written even while other things are missing
    assert (lab.dir / "root" / "etc" / "daolab" / "hw_class").read_text() == "sdp-pf1-vf8\n"


def test_bringup_of_a_ready_board_passes_and_keeps_an_existing_marker(lab):
    st = lab.state()
    st["installed"] = ["dpdk-devbind.py"]
    st["units"]["nginx-tls.service"] = "active"
    lab.set(st)
    marker = lab.dir / "root" / "etc" / "daolab" / "hw_class"
    marker.parent.mkdir(parents=True)
    marker.write_text("vf-only\n")
    rc = lab.run("bringup.yml", daolab_bringup_root=str(lab.dir / "root"), ansible_become=False,
                 hw_class="sdp-pf1-vf8", daolab_deploy_vms=False)
    missing = [l for l in lab.stdout.splitlines() if l.strip().startswith("- dpu-01")]
    # this machine has no /home/dao-test or Marvell repo: only those may be reported
    assert all("dao-test" in l or "Marvell" in l for l in missing), missing
    assert marker.read_text() == "vf-only\n"                     # never overwritten
    assert rc == (0 if not missing else 2)


def test_deployed_keys_stop_working_at_the_end_of_the_slot(lab):
    """Every key line carries sshd's expiry-time = end_at, in the DPU's local time, so key
    logins end at the end of the slot even if no cleanup runs (checked by hand against a real
    sshd: an expired expiry-time is "Refused by key options")."""
    import time as _t
    end = 1792148400                                                    # 2026-10-15T15:00:00Z
    assert access_play(lab, ssh_pubkey=KEY, daolab_access_until=end) == 0, lab.stdout[-2000:]
    local = _t.strftime("%Y%m%d%H%M", _t.localtime(end))
    assert root(lab, "/etc/daolab/authorized_keys/lab").read_text() == \
        f'expiry-time="{local}" {KEY}\n'


def _fake_exporter(lab, up):
    exp = lab.dir / "fake-exporter"
    exp.write_text(f"#!/bin/sh\necho '# TYPE vpp_up gauge'\necho 'vpp_up {up}'\n")
    exp.chmod(0o755)
    return str(exp)


@pytest.mark.parametrize("up,rc_ok", [(1, True), (0, False)])
def test_probe_without_vms_asks_the_dpu_app(lab, up, rc_ok):
    rc = lab.run("probe.yml", daolab_roles=VPP_ROLES, daolab_verify=VPP_VERIFY,
                 daolab_deploy_vms=False, daolab_app="dao-vpp", ansible_become=False,
                 app_exporter_bin=_fake_exporter(lab, up))
    assert (rc == 0) is rc_ok, lab.stdout[-2000:]
    assert not lab.calls("pktgen")                                      # no VM to verify from


def test_taking_the_port_first_pins_its_override_so_the_runbook_cleanup_can_restore_it(lab, books):
    """xeon3-oct-112 after a reboot: the live port is on rvu_nicpf with no override. 08 saves
    the override as found, and 11 only restores "override=rvu_nicpf" - so it is pinned first."""
    board(lab, installed=["vpp"])
    assert books.override() == "(null)"
    assert vpp_deploy(lab, books) == 0, lab.stdout[-3000:]
    assert books.override() == "rvu_nicpf"
    assert books.ran() == ["04_configure_02", "08_prepare_04", "09_configure_04", "10_verify_04"]


def test_a_running_vpp_leaves_the_override_alone(lab, books):
    board(lab, installed=["vpp"], procs=["vpp"])
    books.port_on("vfio-pci")
    assert vpp_deploy(lab, books) == 0, lab.stdout[-2000:]
    assert books.override() == "(null)"


def test_a_drop_in_sshd_rejects_is_removed_again(lab):
    st = lab.state()
    st["sshd_broken"] = True
    lab.set(st)
    assert access_play(lab, ssh_pubkey=KEY) != 0
    assert "sshd -t rejected the lab drop-in; it was removed" in lab.stdout
    assert not root(lab, "/etc/ssh/sshd_config.d/10-daolab-lab.conf").exists()
