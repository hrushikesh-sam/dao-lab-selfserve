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

FAKES = ("xe", "xl", "pgrep", "pkill", "loginctl", "systemctl", "ping", "arping")
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

    def run(self, playbook, **extravars):
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
