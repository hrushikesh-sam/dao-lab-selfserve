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

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSIBLE = os.environ.get("DAOLAB_TEST_ANSIBLE_PLAYBOOK") or shutil.which("ansible-playbook")
pytestmark = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed "
                                                    "(pip install -e .[ansible])")

FAKES = ("xe", "xl", "pgrep", "pkill", "loginctl", "systemctl", "ping", "arping",
         "pktgen", "h2load")
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
                           "dpus": {"hosts": {"dpu-01": {}}}}}}
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


@pytest.fixture
def fake_app(tmp_path):
    """A stand-in DPU app role (the real ones are B-81): runs daolab-app-<app> and exposes a
    metrics target, idempotently."""
    root = tmp_path / "test-roles"
    tasks = root / "fake_app" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(yaml.safe_dump([
        {"name": "run the app", "ansible.builtin.command": "systemctl is-active {{ dpu_app_unit }}",
         "register": "_a", "changed_when": False, "failed_when": False},
        {"name": "start it", "ansible.builtin.command": "systemctl enable --now {{ dpu_app_unit }}",
         "when": "_a.rc != 0"},
        {"name": "remember what the app was given", "ansible.builtin.copy": {
            "content": "{{ {'upstream': dpu_app_upstream_ip, 'addresses': dpu_app_addresses} | to_json }}",
            "dest": str(tmp_path / "app-args.json")}, "delegate_to": "localhost",
         "changed_when": False},
        {"name": "metrics", "ansible.builtin.set_fact": {
            "dpu_app_scrape_targets": [{"job": "fake", "target": "127.0.0.1:9273"}]}}]))
    return root


def deploy(lab, api, fake_app, **over):
    marker = lab.dir / "hw_class"
    if not marker.exists():
        marker.write_text("sdp-pf1-vf8\n")
    ev = {"usecase": "vpp", "hw_class": "sdp-pf1-vf8", "daolab_roles": VPP_ROLES,
          "daolab_verify": VPP_VERIFY, "daolab_api_url": api["url"],
          "daolab_internal_secret": "s3", "daolab_otel_collector": "",
          "dpu_app_roles": {"dao-vpp": "fake_app"}, "ebf_marker": str(marker),
          "host_vm_control_pubkey": "ssh-ed25519 AAAAdaolab daolab@engine",
          "host_vm_ip_retries": 2, "host_vm_ip_delay": 0, "daolab_vm_connection": "local",
          "daolab_no_log": False}
    ev.update(over)
    return lab.run("deploy.yml", roles_path=str(fake_app), **ev)


def with_traffic(lab, **out):
    st = lab.state()
    st["traffic"] = out
    lab.set(st)


def seed(vm, key):
    return base64.b64decode(vm["xenstore"][f"vm-data/daolab/{key}"]).decode()


def test_deploy_vpp_end_to_end(lab, api, fake_app):
    with_traffic(lab, pktgen="tx 1000 rx 1000: rx == tx")
    assert deploy(lab, api, fake_app) == 0, lab.stdout[-3000:]
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
                                         "addresses": ["10.22.0.2/24"]}
    assert "pktgen-dpdk" in yaml.safe_load(seed(tgen, "user-data"))["packages"]
    # the DPU app ran with its data-plane addresses and the upstream's data IP
    assert st["units"]["daolab-app-dao-vpp.service"] == "active"
    args = json.loads((lab.dir / "app-args.json").read_text())
    assert args == {"upstream": "10.22.0.2",
                    "addresses": {"wire0": "10.20.0.1/24", "wire1": "10.22.0.1/24"}}
    # the callbacks: leases for the VM roles, vm-ip for all three (the DPU on its wire IP)
    leases = [b["role"] for p, _, b in api["calls"] if p == "/internal/leases"]
    assert sorted(leases) == ["sink", "tgen"]
    posted = {b["role"]: b["ip"] for p, _, b in api["calls"] if p == "/internal/vm-ip"}
    assert posted == {"vpp": "10.20.0.1", "sink": "10.30.0.100", "tgen": "10.30.0.101"}
    assert all(secret == "s3" for _, secret, _ in api["calls"])
    # verify ran pktgen against the sink's DATA address
    assert ["pktgen", "-t", "10.22.0.2", "--rate", "10mpps"] in lab.calls("pktgen")


def test_deploy_twice_changes_nothing(lab, api, fake_app):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, fake_app) == 0, lab.stdout[-3000:]
    first = lab.state()
    assert deploy(lab, api, fake_app) == 0, lab.stdout[-3000:]
    assert lab.state()["vms"] == first["vms"]
    recap = [l for l in lab.stdout.splitlines() if " : ok=" in l]
    assert recap and all("changed=0" in l for l in recap), "\n".join(recap)


def test_a_conflicting_address_is_swapped_before_use(lab, api, fake_app):
    st = clean_state()
    st["pingable"] = ["10.30.0.100"]                      # something squats on the first lease
    st["traffic"] = {"pktgen": "rx == tx"}
    lab.set(st)
    assert deploy(lab, api, fake_app) == 0, lab.stdout[-3000:]
    assert api["blocked"] == {"10.30.0.100"}
    assert {v["ip"] for v in lab.state()["vms"].values()} == {"10.30.0.101", "10.30.0.102"}


def test_a_failed_verify_fails_the_deploy_and_says_why(lab, api, fake_app):
    with_traffic(lab, pktgen="tx 1000 rx 3")
    assert deploy(lab, api, fake_app) != 0
    assert "verify failed: 'rx == tx' not in the output" in lab.stdout


def test_a_board_on_another_personality_stops_with_the_manual_flash_message(lab, api, fake_app):
    (lab.dir / "hw_class").write_text("vf-only\n")
    assert deploy(lab, api, fake_app) != 0
    assert "ebf_set stub: board is vf-only, use case needs sdp-pf1-vf8" in lab.stdout
    assert lab.state()["vms"] == {}                       # nothing cloned on a wrong board


def test_a_missing_template_is_named(lab, api, fake_app):
    st = clean_state()
    st["templates"] = {}
    lab.set(st)
    assert deploy(lab, api, fake_app) != 0
    assert "template tpl-ubuntu2204-dao is not on tb-01-xcp" in lab.stdout


def test_running_out_of_vfs_is_named(lab, api, fake_app):
    assert deploy(lab, api, fake_app, daolab_roles={**VPP_ROLES, "extra": {
        "on": "tg_host", "vm": "tpl-ubuntu2204-dao", "install": [], "connect": "e810-vf"}}) != 0
    assert "has no VF #2 in e810_vfs" in lab.stdout


def test_an_unknown_install_id_is_refused(lab, api, fake_app):
    roles = {**VPP_ROLES, "sink": {**VPP_ROLES["sink"], "install": ["nope"]}}
    assert deploy(lab, api, fake_app, daolab_roles=roles) != 0
    assert "install id 'nope' has no packages" in lab.stdout


def test_deploy_diy_makes_bare_vms_only(lab, api, fake_app):
    assert deploy(lab, api, fake_app, usecase="diy", diy=True, daolab_roles={},
                  daolab_verify={}, templates={"tb-01-xcp": "tpl-ubuntu2204-dao"}) == 0, \
        lab.stdout[-3000:]
    st = lab.state()
    [(name, vm)] = st["vms"].items()
    assert name == "daolab-r12-tb-01-xcp" and vm["other_config"] == {} and vm["ip"] == "10.30.0.100"
    assert st["units"] == {} and not lab.calls("pktgen")
    assert "data0" not in yaml.safe_load(seed(vm, "network-config"))["ethernets"]


def test_a_manual_run_takes_its_addresses_and_calls_no_api(lab, api, fake_app):
    with_traffic(lab, pktgen="rx == tx")
    assert deploy(lab, api, fake_app, reservation_id=0,
                  manual_ips={"sink": "10.30.0.190", "tgen": "10.30.0.191"}) == 0, lab.stdout[-3000:]
    assert api["calls"] == []
    assert lab.state()["vms"]["daolab-r0-sink"]["ip"] == "10.30.0.190"
