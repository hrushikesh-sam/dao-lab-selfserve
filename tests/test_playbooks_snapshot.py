"""B-109b: the end-of-slot save playbooks, run by real ansible-playbook on the fake lab.

  snapshot_capture.yml  revoke first -> strict input checks -> daolab-capture.py as root
                        (python3 -I) -> fetch the sealed file -> delete it on the DPU, always ->
                        the script's JSON (no paths) to daolab_output_file
  home_wipe.yml         only when the engine passes daolab_wipe_allowed: true (from the DB) ->
                        daolab-wipe.py --mode wipe
  verify_clean.yml      a `home:lab` residue item, only when daolab_snapshot_enabled is true
                        (daolab-wipe.py --mode check, read-only); off = today's checks exactly

Same harness as tests/test_playbooks.py (local connection, fakes on PATH, path prefixes, become
off). The DPU paths live under daolab_dpu_root (home, skel, capture dir) and daolab_age_root
(/usr/local/bin/age); the controller side is a temp dir. The age identity used to decrypt is
generated in tmp_path and only its path is ever passed on.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile

import pytest
import yaml

from tests.test_playbooks import ANSIBLE, REPO, Lab, clean_state

pytestmark = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed "
                                                    "(pip install -e .[ansible])")

PUBLIC_ID = "0123456789abcdef0123456789abcdef"
FAKE_RECIPIENT = "age1" + "q" * 58          # the right shape; for runs that never reach age
PLANT = "PLANTED-b109b-pb7"
MISSING = object()


def _age_tool(var, default):
    return shutil.which(os.environ.get(var) or default)


class Snap:
    """The fake DPU for one test: <root>/home/lab, <root>/etc/skel, the capture dir, an age
    under <agert>/usr/local/bin, and the controller's .incoming dir."""

    def __init__(self, lab, uid=None):
        self.lab = lab
        self.root = lab.dir / "root"
        self.home = self.root / "home" / "lab"
        self.skel = self.root / "etc" / "skel"
        self.capdir = self.root / "var" / "lib" / "daolab" / "capture"
        self.agert = lab.dir / "agert"
        self.outside = lab.dir / "outside"
        self.incoming = lab.dir / "ctrl" / "u1" / ".incoming"
        self.fetch_to = self.incoming / f"{PUBLIC_ID}.age.part"
        self.skel.mkdir(parents=True)
        (self.skel / ".bashrc").write_text("# skel bashrc\n")
        (self.skel / ".profile").write_text("# skel profile\n")
        self.home.mkdir(parents=True)
        for name in os.listdir(self.skel):
            shutil.copy2(self.skel / name, self.home / name)
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("outside data\n")
        self.incoming.mkdir(parents=True)
        os.chmod(self.incoming, 0o700)
        state = clean_state()
        state["users"] = {"lab": {"password": None, "locked": False, "expires": None,
                                  "uid": os.getuid() if uid is None else uid}}
        lab.set(state)

    def plant(self):
        (self.home / f"notes-{PLANT}.txt").write_text("my work\n")
        d = self.home / f"proj-{PLANT}" / "src"
        d.mkdir(parents=True)
        (d / "main.c").write_text("int main(){}\n")
        os.symlink(str(self.outside), self.home / f"link-{PLANT}")
        (self.home / ".bashrc").write_text("# changed\n")

    def link_age(self):
        bindir = self.agert / "usr" / "local" / "bin"
        bindir.mkdir(parents=True)
        os.symlink(_age_tool("DAOLAB_AGE_BIN", "age"), bindir / "age")

    def common(self):
        return {"daolab_dpu_root": str(self.root), "daolab_age_root": str(self.agert),
                "dpu_access_root": str(self.root), "ansible_become": False}

    def _run(self, playbook, base, over):
        ev = {**self.common(), **base, **over}
        return self.lab.run(playbook, **{k: v for k, v in ev.items() if v is not MISSING})

    def capture(self, **over):
        return self._run("snapshot_capture.yml", {
            "daolab_snapshot_recipient": FAKE_RECIPIENT, "daolab_snapshot_public_id": PUBLIC_ID,
            "daolab_snapshot_cap_bytes": 536870912,
            "daolab_snapshot_fetch_to": str(self.fetch_to)}, over)

    def wipe(self, **over):
        return self._run("home_wipe.yml", {"daolab_wipe_allowed": True}, over)

    def verify(self, **over):
        return self._run("verify_clean.yml", {}, over)


def names(path):
    out = set()
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            out.add(os.path.relpath(os.path.join(dirpath, name), path))
    return out


def snapshot_tree(path):
    out = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            body = None
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as fh:
                    body = fh.read()
            elif stat.S_ISLNK(st.st_mode):
                body = os.readlink(p)
            out.append((os.path.relpath(p, path), st.st_mode, st.st_mtime_ns, body))
    return sorted(out)


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


@pytest.fixture
def snap(lab):
    return Snap(lab)


def _plays(name):
    with open(os.path.join(REPO, "playbooks", name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tasks(obj):
    """Every task dict in a play list, blocks included."""
    if isinstance(obj, list):
        for item in obj:
            yield from _tasks(item)
    elif isinstance(obj, dict):
        if any(k.startswith("ansible.builtin.") for k in obj):
            yield obj
        for key in ("tasks", "block", "rescue", "always"):
            if key in obj:
                yield from _tasks(obj[key])


# ---- capture ----------------------------------------------------------------------------------

@pytest.mark.requires_age
def test_capture_seals_the_home_fetches_it_and_leaves_nothing_on_the_dpu(lab, snap, tmp_path):
    snap.plant()
    snap.link_age()
    ident = tmp_path / "owner-of-s.txt"                  # stands in for S's private half
    subprocess.run([_age_tool("DAOLAB_AGE_KEYGEN_BIN", "age-keygen"), "-o", str(ident)],
                   check=True, capture_output=True)
    recipient = subprocess.run([_age_tool("DAOLAB_AGE_KEYGEN_BIN", "age-keygen"), "-y", str(ident)],
                               check=True, capture_output=True, text=True).stdout.strip()
    assert snap.capture(daolab_snapshot_recipient=recipient) == 0, lab.stdout[-3000:]
    report = lab.output()
    assert list(report) == ["status", "reason", "raw_bytes", "age_bytes", "age_sha256"]
    assert (report["status"], report["reason"]) == ("saved", "ok")
    data = snap.fetch_to.read_bytes()
    assert len(data) == report["age_bytes"] and hashlib.sha256(data).hexdigest() == report["age_sha256"]
    assert os.listdir(snap.capdir) == []                 # the ciphertext left the DPU
    plain = subprocess.run([_age_tool("DAOLAB_AGE_BIN", "age"), "-d", "-i", str(ident),
                            str(snap.fetch_to)], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(plain))) as tf:
        listed = {os.path.normpath(m.name) for m in tf.getmembers()} - {"."}
    assert listed == names(snap.home)
    assert report["raw_bytes"] > 0
    assert "step: capture" in lab.stdout and "step: fetch_snapshot" in lab.stdout
    assert PLANT not in json.dumps(report)


def test_capture_revokes_access_first(lab, snap):
    assert snap.capture() == 0, lab.stdout[-3000:]       # a home equal to skel: status empty
    user = lab.state()["users"]["lab"]
    assert user["locked"] is True and user["expires"] == "0"
    assert lab.stdout.index("step: terminate-user") < lab.stdout.index("step: capture")


def test_a_home_equal_to_skel_is_empty_and_nothing_is_fetched(lab, snap):
    assert snap.capture() == 0, lab.stdout[-3000:]
    assert lab.output() == {"status": "empty", "reason": "empty_home", "raw_bytes": 0,
                            "age_bytes": 0, "age_sha256": ""}
    assert not snap.fetch_to.exists() and os.listdir(snap.capdir) == []


def test_a_refused_capture_propagates_and_keeps_the_home(lab):
    snap = Snap(lab, uid=os.getuid() + 1)                # the DPU says lab is someone else
    snap.plant()
    before = snapshot_tree(snap.home)
    assert snap.capture() != 0
    assert lab.output()["status"] == "refused" and lab.output()["reason"] == "home_owner"
    assert "capture refused: home_owner" in lab.stdout
    assert not snap.fetch_to.exists() and os.listdir(snap.capdir) == []
    assert snapshot_tree(snap.home) == before


def test_a_missing_home_is_refused(lab, snap):
    shutil.rmtree(snap.home)
    assert snap.capture() != 0
    assert lab.output()["reason"] == "home_missing"
    assert not snap.fetch_to.exists()


@pytest.mark.parametrize("over", [
    {"daolab_snapshot_public_id": PUBLIC_ID.upper()},
    {"daolab_snapshot_public_id": PUBLIC_ID[:31]},
    {"daolab_snapshot_public_id": "../" + PUBLIC_ID[3:]},
    {"daolab_snapshot_public_id": MISSING},
    {"daolab_snapshot_recipient": "age1" + "b" * 58},           # 'b' is not bech32
    {"daolab_snapshot_recipient": "AGE-SECRET-KEY-1" + "Q" * 58},
    {"daolab_snapshot_recipient": MISSING},
    {"daolab_snapshot_cap_bytes": -1},
    {"daolab_snapshot_cap_bytes": "1e9"},
    {"daolab_snapshot_fetch_to": "relative/x.age.part"},
    {"daolab_snapshot_fetch_to": "/tmp/other.age.part"},
    {"daolab_snapshot_fetch_to": MISSING},
], ids=lambda o: next(iter(o)) + "=" + ("missing" if next(iter(o.values())) is MISSING
                                        else str(next(iter(o.values())))[:12]))
def test_bad_inputs_never_reach_the_dpu_script(lab, snap, over):
    assert snap.capture(**over) != 0
    assert not snap.capdir.exists()                      # stopped before any DPU write
    assert not lab.out_path.exists() and not snap.fetch_to.exists()
    assert "snapshot capture: bad input" in lab.stdout


def test_leftovers_of_a_dead_run_are_removed_before_the_capture(lab, snap):
    snap.capdir.mkdir(parents=True)
    os.chmod(snap.capdir, 0o700)
    (snap.capdir / ("f" * 32 + ".age")).write_bytes(b"old ciphertext")
    assert snap.capture() == 0, lab.stdout[-3000:]
    assert os.listdir(snap.capdir) == []
    assert "capture dir leftovers removed: 1" in lab.stdout


def test_the_profile_hook_is_guarded_and_off_by_default():
    hooks = [t for t in _tasks(_plays("snapshot_capture.yml"))
             if t.get("ansible.builtin.include_tasks") == "tasks/profile_capture.yml"]
    assert len(hooks) == 1
    assert "daolab_profile_enabled | default(false) | bool" in str(hooks[0]["when"])


@pytest.mark.parametrize("playbook,script", [("snapshot_capture.yml", "daolab-capture.py"),
                                             ("home_wipe.yml", "daolab-wipe.py"),
                                             ("verify_clean.yml", "daolab-wipe.py")])
def test_the_dpu_scripts_run_as_root_with_python3_isolated(playbook, script):
    runs = [t for t in _tasks(_plays(playbook)) if "ansible.builtin.script" in t]
    assert runs, playbook
    for t in runs:
        assert t["ansible.builtin.script"]["cmd"].split()[0] == script
        assert t["ansible.builtin.script"]["executable"] == "/usr/bin/python3 -I"
        assert t["become"] is True


# ---- wipe -------------------------------------------------------------------------------------

@pytest.mark.parametrize("allowed", [MISSING, False, "true", "yes", 1, None],
                         ids=["missing", "false", "str-true", "yes", "one", "null"])
def test_the_wipe_refuses_unless_the_engine_allowed_it(lab, snap, allowed):
    snap.plant()
    before = snapshot_tree(snap.home)
    assert snap.wipe(daolab_wipe_allowed=allowed) != 0
    assert "home wipe refused: not allowed by the engine" in lab.stdout
    assert snapshot_tree(snap.home) == before
    assert not lab.calls("getent")                      # stopped before touching the DPU


def test_the_wipe_restores_skel_and_never_follows_a_link_out(lab, snap):
    snap.plant()
    outside = snapshot_tree(snap.outside)
    top = len(os.listdir(snap.home))
    assert snap.wipe() == 0, lab.stdout[-3000:]
    assert lab.output() == {"status": "wiped", "reason": "ok", "removed": top}
    assert names(snap.home) == names(snap.skel)
    for name in os.listdir(snap.skel):
        assert (snap.home / name).read_bytes() == (snap.skel / name).read_bytes()
    assert snapshot_tree(snap.outside) == outside
    assert "step: wipe_home" in lab.stdout


def test_a_wipe_the_script_refuses_fails_and_keeps_the_home(lab):
    snap = Snap(lab, uid=os.getuid() + 1)
    snap.plant()
    before = snapshot_tree(snap.home)
    assert snap.wipe() != 0
    assert lab.output()["reason"] == "home_owner"
    assert "home wipe refused: home_owner" in lab.stdout
    assert snapshot_tree(snap.home) == before


# ---- verify_clean: home:lab only when enabled --------------------------------------------------

def test_verify_ignores_the_home_when_snapshots_are_off(lab, snap):
    snap.plant()
    assert snap.verify() == 0, lab.stdout[-3000:]
    assert lab.output() == []
    assert "step: check_home" not in lab.stdout and not lab.calls("getent")
    assert snap.verify(daolab_snapshot_enabled=False) == 0 and lab.output() == []


def test_verify_reports_a_dirty_home_when_snapshots_are_on(lab, snap):
    snap.plant()
    before = snapshot_tree(snap.home)
    assert snap.verify(daolab_snapshot_enabled=True) == 0, lab.stdout[-3000:]
    assert lab.output() == ["home:lab"]
    assert "step: check_home" in lab.stdout
    assert snapshot_tree(snap.home) == before            # read-only


def test_verify_passes_a_home_equal_to_skel(lab, snap):
    assert snap.verify(daolab_snapshot_enabled=True) == 0, lab.stdout[-3000:]
    assert lab.output() == []


@pytest.mark.parametrize("broken", ["missing", "owner"])
def test_verify_counts_a_home_it_cannot_prove_clean_as_residue(lab, broken):
    snap = Snap(lab, uid=os.getuid() + (1 if broken == "owner" else 0))
    if broken == "missing":
        shutil.rmtree(snap.home)
    assert snap.verify(daolab_snapshot_enabled=True) == 0, lab.stdout[-3000:]
    assert lab.output() == ["home:lab"]


def test_verify_adds_home_to_the_other_residue(lab, snap):
    from tests.test_playbooks import dirty
    state = dirty(lab.state())
    lab.set(state)
    snap.plant()
    assert snap.verify(daolab_snapshot_enabled=True, daolab_leases=["10.30.0.101"]) == 0
    assert sorted(lab.output()) == sorted(["vm:daolab-r12-sink", "dpu_session:lab",
                                           "dpu_app:daolab-app-dao-vpp.service",
                                           "ip:10.30.0.101", "home:lab"])
