"""B-109a: playbooks/files/daolab-capture.py, the DPU-side capture of /home/lab.

The script runs as root on the DPU (Python 3.10, errata E-3/E-6). It streams
`tar --one-file-system --numeric-owner --acls --xattrs` through a raw-byte cap (E-8), gzip
level 1 and `age -r <S_pub>` into one 0600 file, and prints exactly one JSON line with a fixed
reason code; file names and paths are never printed (safe diagnostics only).

These tests run the script as a subprocess with the running interpreter (3.12/3.13 in the main
suite, 3.10 in the `dpu-scripts` CI job), with real tar and, where marked requires_age, the real
age v1.1.1. The few in-process tests load the file with importlib to swap in a fake tar or a
fake euid. The test age identity lives only in a tmp file: it is never printed, compared or
passed on argv (only its path is).
"""
import ast
import gzip
import hashlib
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "playbooks", "files", "daolab-capture.py")
TAR = "/usr/bin/tar"

PLANT = "PLANTED-b109a-zq7"           # in every planted name; must never reach stdout/stderr
KEYS = ["status", "reason", "raw_bytes", "age_bytes", "age_sha256"]
STATUSES = {"saved", "empty", "refused"}
REASONS = {"ok", "empty_home", "path_unsafe", "home_missing", "home_not_dir", "home_owner",
           "mount_under_home", "sysctl_unsafe", "tar_failed", "cap_exceeded", "age_failed",
           "out_exists", "bad_args"}
FAKE_RECIPIENT = "age1" + "q" * 58     # the right shape; only for runs refused before age
# An S-shaped canary (rule 8), built at run time so the full key shape is not in the source.
S_CANARY = "AGE-SECRET-KEY-1" + "Q" * 58

NON_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")


def _esc(path):
    """A path as /proc/self/mountinfo writes it (octal escapes for space, tab, newline, \\)."""
    return (path.replace("\\", "\\134").replace(" ", "\\040")
                .replace("\t", "\\011").replace("\n", "\\012"))


def _mount_line(mid, mount_point, fstype="ext4", source="/dev/sda2"):
    return f"{mid} 1 8:2 / {_esc(mount_point)} rw,relatime shared:1 - {fstype} {source} rw\n"


BASE_MOUNTS = (_mount_line(22, "/") + _mount_line(23, "/proc", "proc", "proc")
               + _mount_line(24, "/dev", "devtmpfs", "udev")
               + "25 22 0:6 / /run rw,nosuid master:3 propagate_from:1 - tmpfs tmpfs rw\n")


class Result:
    def __init__(self, rc, stdout, stderr):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.doc = None


class Lab:
    """A temp board: home, skel, a 0700 capture dir, fake /proc/sys and mountinfo."""

    def __init__(self, root, age="/nonexistent/age", recipient=FAKE_RECIPIENT, home_name="home"):
        self.root = root
        self.home = root / home_name
        self.skel = root / "skel"
        self.capdir = root / "capture"
        self.out = self.capdir / "snap-0001.age"
        self.sysroot = root / "proc-sys"
        self.mountinfo = root / "mountinfo"
        self.age = age
        self.recipient = recipient
        self.home.mkdir()
        self.skel.mkdir()
        (self.skel / ".bashrc").write_text("# skel bashrc\n")
        (self.skel / ".profile").write_text("# skel profile\n")
        self.capdir.mkdir()
        os.chmod(self.capdir, 0o700)
        (self.sysroot / "fs").mkdir(parents=True)
        self.set_sysctl("protected_hardlinks", "1\n")
        self.set_sysctl("protected_symlinks", "1\n")
        self.mountinfo.write_text(BASE_MOUNTS)

    def set_sysctl(self, name, value):
        (self.sysroot / "fs" / name).write_text(value)

    def add_mount(self, mount_point):
        with open(self.mountinfo, "a") as fh:
            fh.write(_mount_line(90, str(mount_point), "ext4", "/dev/sda2"))

    def copy_skel(self):
        for p in self.skel.iterdir():
            (self.home / p.name).write_bytes(p.read_bytes())

    def args(self, **over):
        vals = {"home": str(self.home), "skel": str(self.skel), "recipient": self.recipient,
                "out": str(self.out), "cap-bytes": "536870912", "age": str(self.age),
                "owner-uid": str(os.geteuid()), "mountinfo": str(self.mountinfo),
                "sysctl-root": str(self.sysroot)}
        vals.update({k.replace("_", "-"): v for k, v in over.items()})
        argv = []
        for k, v in vals.items():
            if v is not None:
                argv += [f"--{k}", v]
        return argv

    def run(self, argv=None, timeout=120, umask=None, **over):
        assert os.path.isfile(SCRIPT), "playbooks/files/daolab-capture.py is not written yet"
        argv = self.args(**over) if argv is None else argv
        pre = (lambda: os.umask(umask)) if umask is not None else None
        p = subprocess.run([sys.executable, SCRIPT] + argv, capture_output=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, preexec_fn=pre)
        res = Result(p.returncode, p.stdout, p.stderr)
        res.doc = check_safe(res, self)
        return res

    def leftovers(self):
        return sorted(os.listdir(self.capdir))


def check_safe(res, lab):
    """The output contract, for every outcome: one JSON line, fixed keys and codes, exit 0 only
    for saved/empty, nothing on stderr, and no planted name or temp path anywhere."""
    for stream in (res.stdout, res.stderr):
        assert PLANT.encode() not in stream
        assert str(lab.root).encode() not in stream
        assert S_CANARY.encode() not in stream
    assert res.stderr == b""
    assert res.stdout.endswith(b"\n") and res.stdout.count(b"\n") == 1, res.stdout
    doc = json.loads(res.stdout)
    assert list(doc) == KEYS, doc
    assert doc["status"] in STATUSES and doc["reason"] in REASONS, doc
    assert res.rc == (0 if doc["status"] in ("saved", "empty") else 1), (res.rc, doc)
    assert isinstance(doc["raw_bytes"], int) and isinstance(doc["age_bytes"], int)
    if doc["status"] == "saved":
        assert doc["reason"] == "ok"
        assert len(doc["age_sha256"]) == 64 and set(doc["age_sha256"]) <= set("0123456789abcdef")
    else:
        assert doc["reason"] != "ok"
        assert (doc["raw_bytes"], doc["age_bytes"], doc["age_sha256"]) == (0, 0, ""), doc
    if doc["status"] == "empty":
        assert doc["reason"] == "empty_home"
    return doc


def refused(res, reason):
    assert (res.doc["status"], res.doc["reason"]) == ("refused", reason), res.doc
    assert res.rc == 1


# ---- fixtures ---------------------------------------------------------------------------------

@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


@pytest.fixture
def identity(tmp_path_factory, age_bins):
    """A throwaway age identity: (identity file path, its public recipient)."""
    keygen = age_bins[1]
    d = tmp_path_factory.mktemp("ident")
    os.chmod(d, 0o700)
    idfile = d / "id.txt"
    subprocess.run([keygen, "-o", str(idfile)], check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)
    pub = subprocess.run([keygen, "-y", str(idfile)], check=True, capture_output=True,
                         stdin=subprocess.DEVNULL).stdout.decode().strip()
    return idfile, pub


@pytest.fixture
def agelab(tmp_path, age_bins, identity):
    lab = Lab(tmp_path, age=age_bins[0], recipient=identity[1])
    lab.identity = identity[0]
    return lab


def decrypt(lab):
    """age -d -> the gzip stream, in memory (what B-110 feeds to gunzip)."""
    p = subprocess.run([lab.age, "-d", "-i", str(lab.identity), str(lab.out)],
                       capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, "age -d failed"
    return p.stdout


def gunzip(data):
    p = subprocess.run(["gzip", "-dc"], input=data, capture_output=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def listing(tar_bytes):
    p = subprocess.run([TAR, "--numeric-owner", "-tvf", "-"], input=tar_bytes,
                       capture_output=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.decode("utf-8", "surrogateescape").splitlines()


def reference_tar(home):
    p = subprocess.run([TAR, "--one-file-system", "--numeric-owner", "--acls", "--xattrs",
                        "-C", str(home), "-cf", "-", "."], capture_output=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def plant_tree(home):
    """Regular files (one with a space), a nested dir, a symlink in and one out of the home,
    a hard link and a FIFO. All names carry PLANT."""
    (home / f"notes-{PLANT}.txt").write_text(f"CONTENT-{PLANT}\n" * 200)
    (home / f"with space {PLANT}").write_bytes(os.urandom(5000))
    sub = home / f"proj-{PLANT}" / "src"
    sub.mkdir(parents=True)
    (sub / f"main-{PLANT}.c").write_text("int main(void) { return 0; }\n")
    os.chmod(sub / f"main-{PLANT}.c", 0o600)
    (home / f"empty-dir-{PLANT}").mkdir()
    os.symlink(f"notes-{PLANT}.txt", home / f"rel-link-{PLANT}")
    os.symlink(f"/nonexistent/target-{PLANT}", home / f"abs-link-{PLANT}")
    os.link(home / f"notes-{PLANT}.txt", home / f"hard-{PLANT}")
    os.mkfifo(home / f"fifo-{PLANT}")


def load_module():
    assert os.path.isfile(SCRIPT), "playbooks/files/daolab-capture.py is not written yet"
    spec = importlib.util.spec_from_file_location("daolab_capture_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_inproc(mod, lab, capsys, **over):
    rc = mod.main(lab.args(**over))
    out, err = capsys.readouterr()
    res = Result(rc, out.encode(), err.encode())
    res.doc = check_safe(res, lab)
    return res


def fake_tool(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    os.chmod(p, 0o755)
    return str(p)


# ---- the script itself ------------------------------------------------------------------------

def _source():
    assert os.path.isfile(SCRIPT), "playbooks/files/daolab-capture.py is not written yet"
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_script_parses_as_python_3_10():
    # E-3: no 3.11+ syntax (except*, PEP 695 ...). The 3.10 CI job also runs it for real.
    ast.parse(_source(), filename=SCRIPT, feature_version=(3, 10))


def test_script_imports_only_the_standard_library():
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            names = [node.module]
        else:
            continue
        for name in names:
            assert name.split(".")[0] in stdlib, name


def test_script_has_no_3_11_stdlib_names():
    src = _source()
    for name in ("tomllib", "typing.Self", "ExceptionGroup", "except*", "datetime.UTC"):
        assert name not in src, name


def test_script_is_executable_and_runs_on_python3():
    assert os.access(SCRIPT, os.X_OK)
    assert _source().splitlines()[0] == "#!/usr/bin/python3"


# ---- save: the round trip ---------------------------------------------------------------------

@pytest.mark.requires_age
def test_saved_round_trip_matches_a_direct_tar(agelab):
    lab = agelab
    plant_tree(lab.home)
    if os.geteuid() == 0:                     # E-6: other owners' files are user data
        os.chown(lab.home / f"notes-{PLANT}.txt", 0, 0)
        os.chown(lab.home / f"with space {PLANT}", 4242, 4242)
    res = lab.run(timeout=60)                 # a FIFO read would hang: the timeout proves not
    assert (res.doc["status"], res.doc["reason"]) == ("saved", "ok"), res.doc
    assert lab.leftovers() == [lab.out.name]                    # no temp files
    st = os.stat(lab.out)
    assert stat.S_IMODE(st.st_mode) == 0o600
    data = lab.out.read_bytes()
    assert res.doc["age_bytes"] == len(data) == st.st_size
    assert res.doc["age_sha256"] == hashlib.sha256(data).hexdigest()
    assert data.startswith(b"age-encryption.org/v1\n")
    assert f"CONTENT-{PLANT}".encode() not in data and PLANT.encode() not in data

    gz = decrypt(lab)
    assert gz[:3] == b"\x1f\x8b\x08"                            # gzip, deflate
    assert gz[3] & 0x08 == 0                                    # no file name in the header
    assert gz[4:8] == b"\0\0\0\0"                               # mtime 0
    assert gz[8] == 4                                           # XFL 4: fastest (level 1)
    raw = gunzip(gz)
    assert raw == gzip.decompress(gz)
    assert res.doc["raw_bytes"] == len(raw)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        members = tf.getmembers()
    # --numeric-owner: ids only, no user/group names stored (restore maps nothing by name)
    assert members and all(m.uname == "" and m.gname == "" for m in members)

    got = listing(raw)
    assert got == listing(reference_tar(lab.home))
    assert any(line.startswith("p") and f"fifo-{PLANT}" in line for line in got)
    assert any(line.startswith("h") and f"hard-{PLANT}" in line for line in got)
    assert any(line.startswith("l") and f"-> /nonexistent/target-{PLANT}" in line for line in got)
    assert any(line.startswith("l") and f"rel-link-{PLANT} -> notes-{PLANT}.txt" in line
               for line in got)
    owners = {line.split()[1] for line in got}
    assert all(o.replace("/", "").isdigit() for o in owners), owners   # --numeric-owner
    if os.geteuid() == 0:
        assert "0/0" in owners and "4242/4242" in owners
    else:
        assert owners == {f"{os.geteuid()}/{os.getegid()}"}


@pytest.mark.requires_age
def test_out_is_0600_even_with_a_permissive_umask(agelab):
    (agelab.home / f"f-{PLANT}").write_text("x")
    res = agelab.run(umask=0o002)             # the board's umask gave 0664 on 24 Nov
    assert res.doc["status"] == "saved"
    assert stat.S_IMODE(os.stat(agelab.out).st_mode) == 0o600


@pytest.mark.requires_age
def test_default_mountinfo_is_the_running_kernels(agelab):
    # The defaults (--mountinfo /proc/self/mountinfo) work on a host with no mount under home.
    (agelab.home / f"f-{PLANT}").write_text("x")
    res = agelab.run(mountinfo=None)
    assert res.doc["status"] == "saved", res.doc


# ---- the raw-stream cap (E-8) -----------------------------------------------------------------

@pytest.mark.requires_age
def test_cap_exceeded_kills_the_pipeline_and_removes_out(agelab):
    (agelab.home / f"big-{PLANT}").write_bytes(os.urandom(256 * 1024))
    res = agelab.run(cap_bytes="1024")
    refused(res, "cap_exceeded")
    assert agelab.leftovers() == []


@pytest.mark.requires_age
def test_cap_counts_the_raw_tar_stream_exactly(agelab):
    # Highly compressible data: the gzip stream is far below the cap, the raw stream is not.
    (agelab.home / f"zeros-{PLANT}").write_bytes(b"\0" * (1024 * 1024))
    first = agelab.run()
    assert first.doc["status"] == "saved"
    n = first.doc["raw_bytes"]
    assert n > 1024 * 1024 and first.doc["age_bytes"] < n // 50
    os.unlink(agelab.out)
    at_cap = agelab.run(cap_bytes=str(n))
    assert (at_cap.doc["status"], at_cap.doc["raw_bytes"]) == ("saved", n)
    os.unlink(agelab.out)
    refused(agelab.run(cap_bytes=str(n - 1)), "cap_exceeded")
    assert agelab.leftovers() == []


# ---- tar and age failures ---------------------------------------------------------------------

@NON_ROOT
@pytest.mark.requires_age
def test_unreadable_file_is_tar_failed_and_out_removed(agelab):
    (agelab.home / f"ok-{PLANT}").write_text("x")
    secret = agelab.home / f"unreadable-{PLANT}"
    secret.write_text("y")
    os.chmod(secret, 0)
    try:
        res = agelab.run()
    finally:
        os.chmod(secret, 0o600)
    refused(res, "tar_failed")
    assert agelab.leftovers() == []


def test_tar_exit_1_is_a_failure_too(lab, tmp_path, capsys, monkeypatch):
    # GNU tar exits 1 for "file changed as we read it": still a failed capture.
    mod = load_module()
    monkeypatch.setattr(mod, "TAR", fake_tool(tmp_path, "tar1", "head -c 20480 /dev/zero; exit 1"))
    age = fake_tool(tmp_path, "age-ok", "cat >/dev/null; exit 0")
    (lab.home / f"f-{PLANT}").write_text("x")
    res = run_inproc(mod, lab, capsys, age=age)
    refused(res, "tar_failed")
    assert lab.leftovers() == []


def test_age_nonzero_exit_is_age_failed(lab, tmp_path):
    (lab.home / f"f-{PLANT}").write_text("x")
    res = lab.run(age=fake_tool(tmp_path, "age-bad", "cat >/dev/null; exit 1"))
    refused(res, "age_failed")
    assert lab.leftovers() == []


def test_age_missing_is_age_failed(lab):
    (lab.home / f"f-{PLANT}").write_text("x")
    refused(lab.run(age=str(lab.root / "no-such-age")), "age_failed")
    assert lab.leftovers() == []


def test_age_that_stops_reading_is_age_failed(lab, tmp_path):
    # Exits 0 without reading its stdin: the broken pipe is a failure, whatever its exit code.
    (lab.home / f"rand-{PLANT}").write_bytes(os.urandom(2 * 1024 * 1024))
    res = lab.run(age=fake_tool(tmp_path, "age-quits", "exit 0"))
    refused(res, "age_failed")
    assert lab.leftovers() == []


def test_sigterm_mid_capture_cleans_up(lab, tmp_path):
    # An Ansible timeout sends SIGTERM: children are killed and the partial file removed.
    assert os.path.isfile(SCRIPT), "playbooks/files/daolab-capture.py is not written yet"
    (lab.home / f"rand-{PLANT}").write_bytes(os.urandom(4 * 1024 * 1024))
    age = fake_tool(tmp_path, "age-stuck", "exec sleep 60")   # never reads: the writer blocks
    p = subprocess.Popen([sys.executable, SCRIPT] + lab.args(age=age), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while not lab.out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lab.out.exists()
    time.sleep(0.5)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=30)
    res = Result(p.returncode, out, err)
    res.doc = check_safe(res, lab)
    refused(res, "tar_failed")
    assert lab.leftovers() == []


# ---- the empty-home guard (Q-EMPTY, decided 24 Nov: nothing beyond /etc/skel) ------------------

def _skel_tree(lab):
    (lab.skel / ".config").mkdir()
    (lab.skel / ".config" / "app.conf").write_text("k=v\n")
    os.symlink(".profile", lab.skel / ".prof-link")


def _copy_tree(src, dst):
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.islink(s):
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            os.mkdir(d)
            _copy_tree(s, d)
        else:
            with open(s, "rb") as a, open(d, "wb") as b:
                b.write(a.read())


def test_identical_copy_of_skel_is_empty(lab):
    _skel_tree(lab)
    _copy_tree(lab.skel, lab.home)
    res = lab.run()
    assert (res.doc["status"], res.doc["reason"]) == ("empty", "empty_home")
    assert res.rc == 0
    assert lab.leftovers() == []                                # no capture, no out


@pytest.mark.requires_age
@pytest.mark.parametrize("change", [
    "same_size_edit", "extra_in_subdir", "extra_top_level", "symlink_target", "type_change",
    "fifo_for_file", "missing_entry"])
def test_anything_beyond_skel_is_captured(agelab, change):
    lab = agelab
    _skel_tree(lab)
    _copy_tree(lab.skel, lab.home)
    if change == "same_size_edit":            # same type and size, different bytes
        old = (lab.home / ".bashrc").read_bytes()
        (lab.home / ".bashrc").write_bytes(old[:-2] + b"X\n")
        assert len((lab.home / ".bashrc").read_bytes()) == len(old)
    elif change == "extra_in_subdir":
        (lab.home / ".config" / f"extra-{PLANT}").write_text("mine\n")
    elif change == "extra_top_level":
        (lab.home / f"work-{PLANT}").write_text("mine\n")
    elif change == "symlink_target":
        os.unlink(lab.home / ".prof-link")
        os.symlink(".bashrc", lab.home / ".prof-link")
    elif change == "type_change":
        os.unlink(lab.home / ".profile")
        (lab.home / ".profile").mkdir()
    elif change == "fifo_for_file":           # compared without opening the FIFO
        os.unlink(lab.home / ".profile")
        os.mkfifo(lab.home / ".profile")
    elif change == "missing_entry":           # the user deleted a skel file: still their state
        os.unlink(lab.home / ".profile")
    res = lab.run(timeout=60)
    assert (res.doc["status"], res.doc["reason"]) == ("saved", "ok"), res.doc


@pytest.mark.requires_age
def test_missing_skel_means_capture(agelab):
    # Emptiness can't be proven without skel, so the home is captured, never skipped.
    agelab.copy_skel()
    res = agelab.run(skel=str(agelab.root / "no-skel"))
    assert res.doc["status"] == "saved", res.doc


# ---- home checks ------------------------------------------------------------------------------

def test_home_missing(lab):
    os.rmdir(lab.home)
    refused(lab.run(), "home_missing")


def test_home_not_a_dir(lab):
    os.rmdir(lab.home)
    lab.home.write_text("x")
    refused(lab.run(), "home_not_dir")


def test_home_that_is_a_symlink_is_not_a_dir(lab):
    os.rmdir(lab.home)
    real = lab.root / "real-home"
    real.mkdir()
    os.symlink(real, lab.home)
    refused(lab.run(), "home_not_dir")


def test_home_owner_mismatch(lab):
    lab.copy_skel()
    refused(lab.run(owner_uid=str(os.geteuid() + 1)), "home_owner")
    assert lab.leftovers() == []


# ---- mount guard (F-5-2) ----------------------------------------------------------------------

def test_bind_mount_under_home_is_refused(lab):
    lab.copy_skel()
    lab.add_mount(lab.home / "sub")
    refused(lab.run(), "mount_under_home")


def test_mount_on_home_itself_is_refused(lab):
    lab.copy_skel()
    lab.add_mount(lab.home)
    refused(lab.run(), "mount_under_home")


def test_mountinfo_octal_escapes_are_decoded(tmp_path):
    lab = Lab(tmp_path, home_name="my home")
    lab.copy_skel()
    lab.add_mount(f"{lab.home}/sub\\dir")
    text = lab.mountinfo.read_text()
    assert "my\\040home/sub\\134dir" in text
    refused(lab.run(), "mount_under_home")


def test_mount_beside_home_is_not_under_it(lab):
    lab.copy_skel()
    lab.add_mount(f"{lab.home}2")
    lab.add_mount(f"{lab.home}-x/sub")
    res = lab.run()
    assert res.doc["status"] == "empty", res.doc


def test_mount_under_the_real_path_of_home_is_refused(tmp_path):
    # home reached through a symlinked parent: the guard also checks the resolved path.
    lab = Lab(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "home").mkdir()
    os.symlink(real, tmp_path / "via")
    for p in lab.skel.iterdir():
        (real / "home" / p.name).write_bytes(p.read_bytes())
    lab.add_mount(real / "home" / "sub")
    refused(lab.run(home=str(tmp_path / "via" / "home")), "mount_under_home")


@pytest.mark.parametrize("content", [None, "garbage\n", "22 1 8:2 / / rw shared:1\n"])
def test_unreadable_or_malformed_mountinfo_is_refused(lab, content):
    lab.copy_skel()
    if content is None:
        os.unlink(lab.mountinfo)
    else:
        lab.mountinfo.write_text(BASE_MOUNTS + content)
    refused(lab.run(), "mount_under_home")


# ---- sysctls (E-7) ----------------------------------------------------------------------------

@pytest.mark.parametrize("name,value", [
    ("protected_hardlinks", "0\n"), ("protected_symlinks", "0\n"),
    ("protected_hardlinks", "2\n"), ("protected_symlinks", "11\n"),
    ("protected_hardlinks", None)])
def test_unsafe_sysctl_is_refused(lab, name, value):
    lab.copy_skel()
    if value is None:
        os.unlink(lab.sysroot / "fs" / name)
    else:
        lab.set_sysctl(name, value)
    refused(lab.run(), "sysctl_unsafe")


# ---- path sanity and --out --------------------------------------------------------------------

@pytest.mark.parametrize("which,value", [
    ("home", "relative/home"), ("home", "/"), ("home", "/root"), ("home", "/etc"),
    ("home", "/home"), ("home", "/usr"), ("home", "/var"), ("home", "/tmp"),
    ("home", "{home}/"), ("home", "{home}/../home"), ("home", "{home}/."), ("home", "//x/y"),
    ("home", "{home}\nx"), ("home", "{skel}"), ("skel", "skel"), ("skel", "{home}/skel"),
    ("out", "x.age"), ("out", "/var"), ("out", "{home}/in-home.age"),
    ("out", "{capdir}/../capture/x.age"), ("out", "{capdir}/"), ("out", "{skel}/x.age")])
def test_unsafe_paths_are_refused(lab, which, value):
    lab.copy_skel()
    value = value.format(home=lab.home, skel=lab.skel, capdir=lab.capdir)
    refused(lab.run(**{which: value}), "path_unsafe")
    assert lab.leftovers() == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o711, 0o1700])
def test_out_parent_must_be_mode_0700(lab, mode):
    lab.copy_skel()
    os.chmod(lab.capdir, mode)
    refused(lab.run(), "path_unsafe")


def test_out_parent_must_be_owned_by_the_euid(lab, capsys, monkeypatch):
    lab.copy_skel()
    mod = load_module()
    real = os.geteuid()
    monkeypatch.setattr(mod.os, "geteuid", lambda: real + 1)
    res = run_inproc(mod, lab, capsys, owner_uid=str(real))
    refused(res, "path_unsafe")


def test_out_parent_missing_or_symlink_or_file(lab):
    lab.copy_skel()
    refused(lab.run(out=str(lab.root / "nope" / "x.age")), "path_unsafe")
    os.symlink(lab.capdir, lab.root / "cap-link")
    refused(lab.run(out=str(lab.root / "cap-link" / "x.age")), "path_unsafe")
    (lab.root / "afile").write_text("x")
    refused(lab.run(out=str(lab.root / "afile" / "x.age")), "path_unsafe")


def test_out_exists_is_refused_and_left_alone(lab):
    lab.copy_skel()
    lab.out.write_bytes(b"someone else's file")
    os.chmod(lab.out, 0o640)
    refused(lab.run(), "out_exists")
    assert lab.out.read_bytes() == b"someone else's file"
    assert stat.S_IMODE(os.stat(lab.out).st_mode) == 0o640


def test_out_dangling_symlink_is_out_exists(lab):
    lab.copy_skel()
    os.symlink(lab.root / "elsewhere", lab.out)
    refused(lab.run(), "out_exists")
    assert os.path.islink(lab.out) and not (lab.root / "elsewhere").exists()


# ---- arguments --------------------------------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"recipient": "age1" + "q" * 57}, {"recipient": "age1" + "q" * 59},
    {"recipient": "AGE1" + "Q" * 58}, {"recipient": "age1" + "b" * 58},
    {"recipient": "age1" + "q" * 57 + "1"}, {"recipient": " age1" + "q" * 58},
    {"recipient": "age1" + "q" * 58 + "\n"}, {"recipient": ""},
    {"cap_bytes": "abc"}, {"cap_bytes": "0"}, {"cap_bytes": "-5"}, {"cap_bytes": "1e3"},
    {"cap_bytes": " 10"}, {"owner_uid": "-1"}, {"owner_uid": "x"}, {"owner_uid": "4294967296"},
    {"age": "age"}, {"age": "./age"}, {"mountinfo": "proc/self/mountinfo"},
    {"sysctl_root": "proc/sys"},
    {"home": None}, {"skel": None}, {"recipient": None}, {"out": None}, {"cap_bytes": None},
    {"age": None}, {"owner_uid": None}])
def test_bad_args(lab, over):
    lab.copy_skel()
    res = lab.run(**over)
    refused(res, "bad_args")
    assert b"usage" not in res.stdout.lower()
    assert lab.leftovers() == []


@pytest.mark.parametrize("extra", [["-h"], ["--help"], ["--bogus", "1"], ["stray"],
                                   ["--hom", "/x/y"], ["--home", "/x/y"]])
def test_bad_args_unknown_repeated_or_help(lab, extra):
    lab.copy_skel()
    res = lab.run(argv=lab.args() + extra)
    refused(res, "bad_args")
    assert b"usage" not in res.stdout.lower()


def test_no_args_at_all(lab):
    refused(lab.run(argv=[]), "bad_args")


def test_s_shaped_recipient_is_refused_and_never_echoed(lab):
    # Rule 8 canary: an identity passed by mistake is refused and printed nowhere
    # (check_safe asserts the canary is absent from stdout and stderr).
    lab.copy_skel()
    refused(lab.run(recipient=S_CANARY), "bad_args")
    refused(lab.run(recipient=S_CANARY.lower()), "bad_args")


# ---- timing (informational) -------------------------------------------------------------------

@pytest.mark.requires_age
def test_timing_64_mib(agelab, capsys):
    for i in range(64):
        (agelab.home / f"blob-{i:02d}-{PLANT}").write_bytes(os.urandom(1024 * 1024))
    t0 = time.monotonic()
    res = agelab.run(timeout=300)
    dt = time.monotonic() - t0
    assert res.doc["status"] == "saved"
    assert res.doc["raw_bytes"] >= 64 * 1024 * 1024
    with capsys.disabled():
        print(f"\n[B-109a timing] 64 MiB random tree: {dt:.2f} s, "
              f"{res.doc['raw_bytes'] / dt / 1e6:.1f} MB/s raw, "
              f"python {sys.version.split()[0]}")
