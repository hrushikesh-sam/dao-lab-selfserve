"""P8 playbooks on the fake testbed: the lab home folder at slot end (B-107) and the snapshot
step of cleanup.yml (B-106). Same harness as tests/test_playbooks.py: real ansible-playbook,
local connection, every path a temp dir.
"""
import json
import os
import stat
import tarfile

import pytest

from tests.test_playbooks import ANSIBLE, Lab, clean_state, dirty

pytestmark = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed "
                                                    "(pip install -e .[ansible])")

SKEL = {".bashrc": "# skel bashrc\n", ".profile": "# skel profile\n"}


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    skel = tmp_path / "skel"
    skel.mkdir()
    for name, text in SKEL.items():
        (skel / name).write_text(text)
    home = tmp_path / "home" / "lab"
    home.mkdir(parents=True)
    for name, text in SKEL.items():
        (home / name).write_text(text)
    lab.home, lab.skel, lab.unsaved = home, skel, tmp_path / "unsaved"
    return lab


def run(lab, playbook, policy="wipe", **extra):
    ev = {"daolab_home_policy": policy, "daolab_lab_home": str(lab.home),
          "daolab_skel": str(lab.skel), "daolab_unsaved_dir": str(lab.unsaved),
          "daolab_snapshot_workdir": str(lab.dir / "run"), **extra}
    return lab.run(playbook, **ev)


def leave_work(home):
    """What a user leaves: a script, a nested config, an edited skel file, a symlink."""
    (home / "exp").mkdir()
    (home / "exp" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (home / "notes.txt").write_text("secret notes\n")
    (home / ".bashrc").write_text("# skel bashrc\nexport FOO=1\n")
    (home / "link").symlink_to("/etc/hostname")


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


# ---- B-107: verify_clean.yml check_home -----------------------------------------------------

def test_policy_keep_never_looks_at_the_home_folder(lab):
    leave_work(lab.home)
    assert run(lab, "verify_clean.yml", policy="keep") == 0, lab.stdout
    assert lab.output() == []
    assert "check_home" not in lab.stdout                       # no step, not even skipped


def test_a_home_with_only_skel_is_clean(lab):
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


def test_a_missing_home_is_clean(lab):
    lab.home.rename(lab.dir / "elsewhere")
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


@pytest.mark.parametrize("leftover", ["file", "dir", "edited_skel", "symlink", "dotfile"])
def test_every_kind_of_leftover_is_residue(lab, leftover):
    h = lab.home
    {"file": lambda: (h / "a.txt").write_text("x"),
     "dir": lambda: (h / "empty").mkdir(),
     "edited_skel": lambda: (h / ".profile").write_text("changed"),
     "symlink": lambda: (h / "l").symlink_to("/tmp"),
     "dotfile": lambda: (h / ".bash_history").write_text("ls\n")}[leftover]()
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == ["dpu_home:lab"]


def test_a_deleted_skel_file_is_not_residue(lab):
    (lab.home / ".profile").unlink()
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


def test_residue_names_no_file(lab):
    leave_work(lab.home)
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == ["dpu_home:lab"]
    assert "notes.txt" not in lab.stdout and "run.sh" not in lab.stdout


def test_check_home_is_read_only(lab):
    leave_work(lab.home)
    before = tree(lab.home)
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert tree(lab.home) == before and not lab.unsaved.exists()


def test_home_residue_sits_beside_the_other_kinds(lab):
    lab.set(dirty(clean_state()))
    leave_work(lab.home)
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert "dpu_home:lab" in lab.output() and "dpu_session:lab" in lab.output()


@pytest.mark.parametrize("bad", [{"daolab_lab_home": "/"}, {"daolab_lab_home": "relative/lab"},
                                 {"daolab_lab_home": "/home"},
                                 {"daolab_lab_home": "/home/lab/../.."},
                                 {"daolab_home_policy": "nuke"}])
def test_bad_paths_stop_the_play_before_anything_is_touched(lab, bad):
    leave_work(lab.home)
    before = tree(lab.home)
    assert run(lab, "reconcile.yml", **bad) != 0
    assert "refusing to touch the home folder" in lab.stdout
    assert tree(lab.home) == before


# ---- B-107: reconcile.yml stash_home --------------------------------------------------------

def test_reconcile_stashes_the_leftovers_and_verify_then_agrees(lab):
    leave_work(lab.home)
    assert run(lab, "reconcile.yml") == 0, lab.stdout
    assert tree(lab.home) == sorted(SKEL)                         # back to skel
    assert (lab.home / ".bashrc").read_text() == SKEL[".bashrc"]  # the edited copy replaced
    [stash] = list(lab.unsaved.iterdir())
    assert stash.name.startswith("r12-")
    assert (stash / "notes.txt").read_text() == "secret notes\n"
    assert (stash / "exp" / "run.sh").exists() and (stash / "link").is_symlink()
    assert (stash / ".bashrc").read_text().endswith("FOO=1\n")    # the user's version kept
    assert stat.S_IMODE(lab.unsaved.stat().st_mode) == 0o700
    assert stat.S_IMODE(stash.stat().st_mode) == 0o700
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


def test_reconcile_with_a_clean_home_stashes_nothing(lab):
    assert run(lab, "reconcile.yml") == 0, lab.stdout
    assert not lab.unsaved.exists()
    assert tree(lab.home) == sorted(SKEL)


def test_reconcile_twice_makes_two_stashes_and_loses_nothing(lab):
    (lab.home / "one.txt").write_text("1")
    assert run(lab, "reconcile.yml") == 0, lab.stdout
    (lab.home / "two.txt").write_text("2")
    assert run(lab, "reconcile.yml") == 0, lab.stdout
    stashes = sorted(lab.unsaved.iterdir())
    assert len(stashes) == 2
    found = {p.name for s in stashes for p in s.iterdir()}
    assert {"one.txt", "two.txt"} <= found


def test_reconcile_with_policy_keep_leaves_the_home_alone(lab):
    leave_work(lab.home)
    before = tree(lab.home)
    assert run(lab, "reconcile.yml", policy="keep") == 0, lab.stdout
    assert tree(lab.home) == before and not lab.unsaved.exists()


# ---- B-106: cleanup.yml end of slot ---------------------------------------------------------

from service import snapshot as snap  # noqa: E402

TOKEN = "tok-alice-" + "a" * 40
CTX = "user:1|snapshot:7"
VFIO_VF = "0002:01:00.1"          # a VF the use case bound to vfio-pci
PF = "0002:02:00.0"               # an SR-IOV PF with 8 VFs made
GONE = "0002:09:00.0"             # allowlisted but not on this board


def fake_board(sysfs):
    """A fake sysfs: VFIO_VF on vfio-pci, PF on rvu_nicpf with 8/16 VFs, two hugepage sizes."""
    drivers = sysfs / "bus" / "pci" / "drivers"
    for drv in ("vfio-pci", "rvu_nicpf"):
        (drivers / drv).mkdir(parents=True, exist_ok=True)
    for bdf, drv, extra in [(VFIO_VF, "vfio-pci", {"driver_override": "vfio-pci"}),
                            (PF, "rvu_nicpf", {"driver_override": "(null)",
                                               "sriov_numvfs": "8", "sriov_totalvfs": "16"})]:
        d = sysfs / "bus" / "pci" / "devices" / bdf
        d.mkdir(parents=True)
        (d / "driver").symlink_to(drivers / drv)
        (d / "vendor").write_text("0x177d\n")
        (d / "device").write_text("0xa0f8\n")
        for k, v in extra.items():
            (d / k).write_text(v + "\n")
    for size, nr in (("524288kB", "2"), ("2048kB", "0")):
        top = sysfs / "kernel" / "mm" / "hugepages" / f"hugepages-{size}"
        top.mkdir(parents=True)
        (top / "nr_hugepages").write_text(nr + "\n")
        node = sysfs / "devices" / "system" / "node" / "node0" / "hugepages" / f"hugepages-{size}"
        node.mkdir(parents=True)
        (node / "nr_hugepages").write_text(nr + "\n")


@pytest.fixture
def owner():
    return snap.new_user_key(TOKEN)


def snapshot_vars(lab, owner, **over):
    v = {"daolab_save_snapshot": True, "daolab_snapshot_pubkey": owner.public_key,
         "daolab_snapshot_context": CTX,
         "daolab_snapshot_path": str(lab.dir / "snapshots" / "1" / "7.snap"),
         "daolab_snapshot_stage": str(lab.dir / "stage"),
         "daolab_snapshot_file": str(lab.dir / "snapshot.json"),
         "daolab_snapshot_bdfs": [VFIO_VF, PF, GONE]}
    (lab.dir / "stage").mkdir(exist_ok=True)
    v.update(over)
    return v


def result(lab):
    return json.loads((lab.dir / "snapshot.json").read_text())


def open_snapshot(lab, owner, res, tmp_path):
    """Token -> DEK -> sealed file -> the outer tar's manifest + home.tgz members."""
    dek = snap.open_dek(TOKEN, owner, res["wrapped_dek"], CTX)
    plain = tmp_path / "opened.tar"
    snap.open_(res["path"], str(plain), dek, CTX, expect_sha256=res["sha256"])
    with tarfile.open(plain) as outer:
        assert sorted(outer.getnames()) == ["home.tgz", "manifest.json"]
        manifest = json.load(outer.extractfile("manifest.json"))
        home = tarfile.open(fileobj=outer.extractfile("home.tgz"), mode="r:gz")
        files = {m.name.removeprefix("./"): (home.extractfile(m).read() if m.isfile() else m)
                 for m in home.getmembers() if m.name != "."}
    return manifest, files


def no_plaintext_left(lab):
    run_dir = lab.dir / "run"
    assert not run_dir.exists() or not any(run_dir.iterdir())
    assert not any((lab.dir / "stage").iterdir())


def test_snapshot_saves_files_and_board_state_then_empties_the_home(lab, owner, tmp_path):
    fake_board(lab.dir / "sys")
    leave_work(lab.home)
    assert run(lab, "cleanup.yml", policy="snapshot", **snapshot_vars(lab, owner)) == 0, lab.stdout
    res = result(lab)
    assert res["saved"] is True, res
    assert res["path"].endswith("/snapshots/1/7.snap")
    assert stat.S_IMODE(os.stat(res["path"]).st_mode) == 0o600
    assert res["size_bytes"] == os.path.getsize(res["path"])
    assert res["header"] == {"files": 6, "devices": 3, "hugepage_sizes": 2}   # .bashrc .profile exp exp/run.sh notes.txt link

    manifest, files = open_snapshot(lab, owner, res, tmp_path)
    assert files["notes.txt"] == b"secret notes\n"
    assert files["exp/run.sh"].startswith(b"#!/bin/sh")
    assert files[".bashrc"].endswith(b"FOO=1\n")                 # the user's edited version
    assert files["link"].issym() and files["link"].linkname == "/etc/hostname"
    assert manifest["bdfs"][VFIO_VF] == {"present": True, "driver": "vfio-pci",
                                         "driver_override": "vfio-pci", "id": "177d:a0f8",
                                         "sriov_numvfs": None, "sriov_totalvfs": None}
    assert manifest["bdfs"][PF]["driver"] == "rvu_nicpf"
    assert manifest["bdfs"][PF]["driver_override"] is None        # "(null)" in sysfs
    assert (manifest["bdfs"][PF]["sriov_numvfs"], manifest["bdfs"][PF]["sriov_totalvfs"]) == (8, 16)
    assert manifest["bdfs"][GONE] == {"present": False}
    assert manifest["hugepages"]["524288kB"] == {"nr": 2, "nodes": {"node0": 2}}

    assert tree(lab.home) == sorted(SKEL)                         # emptied back to skel
    assert not lab.unsaved.exists()                               # saved, so nothing stashed
    no_plaintext_left(lab)
    assert run(lab, "verify_clean.yml") == 0, lab.stdout
    assert lab.output() == []


def test_the_sealed_file_holds_no_plaintext(lab, owner):
    (lab.home / "notes.txt").write_text("PLAINTEXT-MARKER " * 50)
    assert run(lab, "cleanup.yml", policy="snapshot", **snapshot_vars(lab, owner)) == 0, lab.stdout
    raw = open(result(lab)["path"], "rb").read()
    assert b"PLAINTEXT-MARKER" not in raw and b"notes.txt" not in raw


def test_another_users_token_cannot_open_it(lab, owner):
    (lab.home / "notes.txt").write_text("x")
    assert run(lab, "cleanup.yml", policy="snapshot", **snapshot_vars(lab, owner)) == 0, lab.stdout
    with pytest.raises(snap.SnapshotKeyError):
        snap.open_dek("tok-bob-" + "b" * 40, owner, result(lab)["wrapped_dek"], CTX)


def test_snapshot_runs_after_access_is_revoked_and_before_the_app_cleanup(lab, owner):
    (lab.home / "notes.txt").write_text("x")
    assert run(lab, "cleanup.yml", policy="snapshot", **snapshot_vars(lab, owner)) == 0, lab.stdout
    out = lab.stdout
    revoke = out.index("step: revoke_access")
    state = out.index("step: snapshot_state")
    seal = out.index("step: snapshot_seal")
    wipe = out.index("step: wipe_home")
    app = out.index("nothing to cleanup on the DPU")                # apps/none/cleanup.yml
    assert revoke < state < seal < wipe < app


def test_a_workspace_over_the_cap_is_stashed_not_lost(lab, owner):
    leave_work(lab.home)
    v = snapshot_vars(lab, owner, daolab_snapshot_max_bytes=100)
    assert run(lab, "cleanup.yml", policy="snapshot", **v) == 0, lab.stdout   # cleanup goes on
    res = result(lab)
    assert res["saved"] is False and "over the cap of 100 bytes" in res["error"]
    assert not os.path.exists(v["daolab_snapshot_path"])
    [stash] = list(lab.unsaved.iterdir())
    assert (stash / "notes.txt").read_text() == "secret notes\n"
    assert tree(lab.home) == sorted(SKEL)
    no_plaintext_left(lab)


def test_a_missing_key_is_a_failed_snapshot_and_a_stash(lab, owner):
    (lab.home / "notes.txt").write_text("x")
    v = snapshot_vars(lab, owner, daolab_snapshot_pubkey="")
    assert run(lab, "cleanup.yml", policy="snapshot", **v) == 0, lab.stdout
    res = result(lab)
    assert res["saved"] is False and "snapshot not configured" in res["error"]
    assert len(list(lab.unsaved.iterdir())) == 1


def test_a_seal_failure_is_reported_and_stashed(lab, owner):
    (lab.home / "notes.txt").write_text("x")
    v = snapshot_vars(lab, owner, daolab_snapshot_pubkey="bm90IGEga2V5")   # base64, not a key
    assert run(lab, "cleanup.yml", policy="snapshot", **v) == 0, lab.stdout
    res = result(lab)
    assert res["saved"] is False and "snapshot_seal" in res["error"]
    assert "X25519" in res["error"] or "public_key" in res["error"]
    assert len(list(lab.unsaved.iterdir())) == 1
    no_plaintext_left(lab)


def test_owner_said_no_means_wipe_without_saving(lab, owner):
    leave_work(lab.home)
    v = snapshot_vars(lab, owner, daolab_save_snapshot=False)
    assert run(lab, "cleanup.yml", policy="snapshot", **v) == 0, lab.stdout
    res = result(lab)
    assert res == {"saved": False, "skipped": "the owner chose not to save this workspace"}
    assert not os.path.exists(v["daolab_snapshot_path"])
    assert tree(lab.home) == sorted(SKEL) and not lab.unsaved.exists()
    assert "snapshot_state" not in lab.stdout


def test_policy_wipe_empties_without_saving(lab):
    leave_work(lab.home)
    assert run(lab, "cleanup.yml", policy="wipe") == 0, lab.stdout
    assert tree(lab.home) == sorted(SKEL) and not lab.unsaved.exists()
    assert "snapshot_state" not in lab.stdout


def test_policy_keep_leaves_the_home_as_before_p8(lab):
    leave_work(lab.home)
    before = tree(lab.home)
    assert run(lab, "cleanup.yml", policy="keep") == 0, lab.stdout
    assert tree(lab.home) == before
    assert "wipe_home" not in lab.stdout and "snapshot_state" not in lab.stdout


def test_an_empty_home_saves_an_empty_snapshot(lab, owner, tmp_path):
    assert run(lab, "cleanup.yml", policy="snapshot", **snapshot_vars(lab, owner)) == 0, lab.stdout
    res = result(lab)
    assert res["saved"] is True
    _, files = open_snapshot(lab, owner, res, tmp_path)
    assert sorted(files) == sorted(SKEL)
    assert "step: wipe_home" not in lab.stdout or "skipping" in lab.stdout   # nothing to wipe


# ---- B-106: capture_state.py on its own -----------------------------------------------------

CAPTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "playbooks", "roles", "snapshot", "files", "capture_state.py")


def capture(sysfs, bdfs):
    import subprocess
    import sys
    return subprocess.run([sys.executable, CAPTURE, "--sysfs", str(sysfs),
                           "--bdfs", json.dumps(bdfs)], capture_output=True, text=True)


def test_capture_state_on_an_empty_board(tmp_path):
    p = capture(tmp_path / "nosys", [])
    assert p.returncode == 0
    assert json.loads(p.stdout) == {"version": 1, "bdfs": {}, "hugepages": {}}


def test_capture_state_refuses_a_bad_address(tmp_path):
    p = capture(tmp_path, ["0002:01:00.1; rm -rf /"])
    assert p.returncode == 2 and "not PCI addresses" in p.stderr


def test_capture_state_device_without_a_driver(tmp_path):
    d = tmp_path / "bus" / "pci" / "devices" / VFIO_VF
    d.mkdir(parents=True)
    out = json.loads(capture(tmp_path, [VFIO_VF]).stdout)
    assert out["bdfs"][VFIO_VF]["present"] is True and out["bdfs"][VFIO_VF]["driver"] is None


def _fake_device(sysfs, bdf, vendor_device, driver=None):
    d = sysfs / "bus" / "pci" / "devices" / bdf
    d.mkdir(parents=True)
    vendor, device = vendor_device.split(":")
    (d / "vendor").write_text(f"0x{vendor}\n")
    (d / "device").write_text(f"0x{device}\n")
    (d / "driver_override").write_text("(null)\n")
    if driver:
        drv = sysfs / "bus" / "pci" / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (d / "driver").symlink_to(drv)


def test_capture_state_never_records_the_rvu_af_or_a_pem(tmp_path):
    """Listed by mistake, the SDP RVU PF and the PEM are refused, so a restore (B-110) can
    never rebind them. Caught by driver and by device id: an address typo cannot get past it."""
    _fake_device(tmp_path, "0002:01:00.0", "177d:a065", "rvu_af")          # the SDP RVU PF
    _fake_device(tmp_path, "0001:00:10.0", "177d:a06c")                    # the PEM
    _fake_device(tmp_path, "0002:09:00.0", "177d:a0f7", "rvu_af")          # any device on rvu_af
    _fake_device(tmp_path, "0002:0a:00.0", "177d:a065", "vfio-pci")        # the AF id, rebound
    _fake_device(tmp_path, "0000:00:10.0", "177d:a092")                    # ML: recorded
    out = json.loads(capture(tmp_path, ["0002:01:00.0", "0001:00:10.0", "0002:09:00.0",
                                        "0002:0a:00.0", "0000:00:10.0"]).stdout)["bdfs"]
    for bdf in ("0002:01:00.0", "0001:00:10.0", "0002:09:00.0", "0002:0a:00.0"):
        assert out[bdf]["refused"].startswith("never recorded"), bdf
        assert "driver" not in out[bdf] and "sriov_numvfs" not in out[bdf]
    assert "refused" not in out["0000:00:10.0"]
    assert out["0000:00:10.0"]["driver"] is None and out["0000:00:10.0"]["id"] == "177d:a092"


def test_capture_state_on_the_tb01_board_as_seen_on_2026_11_20(tmp_path):
    """The four tb-01 devices as dpdk-devbind.py -s and sysfs showed them with nginx-tls up."""
    _fake_device(tmp_path, "0000:00:10.0", "177d:a092")
    _fake_device(tmp_path, "0002:03:00.0", "177d:a063", "rvu_nicpf")
    _fake_device(tmp_path, "0002:04:00.0", "177d:a063", "rvu_nicpf")
    (tmp_path / "bus/pci/devices/0002:04:00.0/driver_override").write_text("rvu_nicpf\n")
    _fake_device(tmp_path, "0002:20:00.0", "177d:a0f2", "rvu_cptpf")
    for bdf, n, total in (("0002:03:00.0", 0, 3), ("0002:04:00.0", 0, 3), ("0002:20:00.0", 24, 64)):
        (tmp_path / "bus/pci/devices" / bdf / "sriov_numvfs").write_text(f"{n}\n")
        (tmp_path / "bus/pci/devices" / bdf / "sriov_totalvfs").write_text(f"{total}\n")
    out = json.loads(capture(tmp_path, ["0000:00:10.0", "0002:03:00.0", "0002:04:00.0",
                                        "0002:20:00.0"]).stdout)["bdfs"]
    assert out["0002:04:00.0"]["driver"] == "rvu_nicpf"
    assert out["0002:04:00.0"]["driver_override"] == "rvu_nicpf"
    assert (out["0002:20:00.0"]["sriov_numvfs"], out["0002:20:00.0"]["sriov_totalvfs"]) == (24, 64)
    assert out["0000:00:10.0"]["driver"] is None
    assert not any("refused" in v for v in out.values())
