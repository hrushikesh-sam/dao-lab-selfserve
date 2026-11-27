"""B-109b: playbooks/files/daolab-wipe.py, the DPU-side wipe (and read-only check) of /home/lab.

The script runs as root on the DPU (Python 3.10, errata E-3/E-6) in one of two explicit modes:

  --mode wipe   remove every entry of the home without following symlinks, copy /etc/skel back
                (modes kept, owned by the lab uid and the home's group), then prove home == skel
  --mode check  read-only: count the top-level names where home and skel differ (verify_clean's
                `home:lab` check, the same "empty" meaning as daolab-capture.py)

Both run the capture script's guards first (arguments, path sanity, mount guard, home owner)
and print exactly one JSON line with a fixed reason code; never a file name or a path.

These tests run the script as a subprocess with the running interpreter (3.12/3.13 in the main
suite, 3.10 in the `dpu-scripts` CI job / `--confcutdir=tests/dpu`). Standard library + pytest
only: nothing here may import `service`.
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "playbooks", "files", "daolab-wipe.py")

PLANT = "PLANTED-b109b-wq3"           # in every planted name; must never reach stdout/stderr
WIPE_KEYS = ["status", "reason", "removed"]
CHECK_KEYS = ["status", "reason", "differ"]
REASONS = {"ok", "home_differs", "bad_args", "path_unsafe", "home_missing", "home_not_dir",
           "home_owner", "mount_under_home", "skel_failed", "remove_failed", "not_clean"}
S_CANARY = "AGE-SECRET-KEY-1" + "Q" * 58


def _esc(path):
    return (path.replace("\\", "\\134").replace(" ", "\\040")
                .replace("\t", "\\011").replace("\n", "\\012"))


def _mount_line(mid, mount_point, fstype="ext4", source="/dev/sda2"):
    return f"{mid} 1 8:2 / {_esc(mount_point)} rw,relatime shared:1 - {fstype} {source} rw\n"


BASE_MOUNTS = (_mount_line(22, "/") + _mount_line(23, "/proc", "proc", "proc")
               + _mount_line(24, "/dev", "devtmpfs", "udev"))


class Result:
    def __init__(self, rc, stdout, stderr, doc):
        self.rc, self.stdout, self.stderr, self.doc = rc, stdout, stderr, doc


class Board:
    """A temp board: <root>/home/lab, <root>/etc/skel, an outside dir, a fake mountinfo."""

    def __init__(self, root):
        self.root = root
        self.home = root / "home" / "lab"
        self.skel = root / "etc" / "skel"
        self.outside = root / "outside"
        self.mountinfo = root / "mountinfo"
        self.home.mkdir(parents=True)
        self.skel.mkdir(parents=True)
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("outside data\n")
        (self.skel / ".bashrc").write_text("# skel bashrc\n")
        (self.skel / ".profile").write_text("# skel profile\n")
        os.chmod(self.skel / ".profile", 0o600)
        (self.skel / ".config").mkdir()
        os.chmod(self.skel / ".config", 0o700)
        (self.skel / ".config" / "app.conf").write_text("x=1\n")
        os.symlink(".bashrc", self.skel / ".bashrc-link")
        self.mountinfo.write_text(BASE_MOUNTS)

    def copy_skel(self):
        """home := an exact copy of skel (what a fresh account looks like)."""
        for name in os.listdir(self.skel):
            _copy(self.skel / name, self.home / name)

    def plant(self):
        """User data of every kind, including links that point out of the home."""
        (self.home / f"notes-{PLANT}.txt").write_text("my work\n")
        (self.home / f".hidden-{PLANT}").write_text("hidden\n")
        d = self.home / f"proj-{PLANT}" / "deep" / "deeper"
        d.mkdir(parents=True)
        (d / f"f-{PLANT}.c").write_text("int main(){}\n")
        os.symlink(str(self.outside), self.home / f"link-dir-{PLANT}")
        os.symlink(str(self.outside / "keep.txt"), self.home / f"link-file-{PLANT}")
        os.mkfifo(self.home / f"fifo-{PLANT}")
        os.link(self.outside / "keep.txt", self.home / f"hard-{PLANT}")
        (self.home / ".bashrc").write_text("# changed by the user\n")

    def argv(self, mode="wipe", **over):
        vals = {"mode": mode, "home": str(self.home), "skel": str(self.skel),
                "owner-uid": str(os.geteuid()), "mountinfo": str(self.mountinfo)}
        vals.update({k.replace("_", "-"): v for k, v in over.items()})
        out = []
        for k, v in vals.items():
            if v is not None:
                out += [f"--{k}", v]
        return out

    def run(self, mode="wipe", argv=None, **over):
        assert os.path.isfile(SCRIPT), "playbooks/files/daolab-wipe.py is not written yet"
        argv = self.argv(mode, **over) if argv is None else argv
        p = subprocess.run([sys.executable, SCRIPT] + argv, capture_output=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        doc = check_safe(p.stdout, p.stderr, self, mode if mode in ("wipe", "check") else "wipe")
        return Result(p.returncode, p.stdout, p.stderr, doc)

    def add_mount(self, mount_point):
        with open(self.mountinfo, "a") as fh:
            fh.write(_mount_line(90, str(mount_point)))


def _copy(src, dst):
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISDIR(st.st_mode):
        os.mkdir(dst)
        for name in os.listdir(src):
            _copy(os.path.join(src, name), os.path.join(dst, name))
        os.chmod(dst, stat.S_IMODE(st.st_mode))
    else:
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
        os.chmod(dst, stat.S_IMODE(st.st_mode))


def check_safe(stdout, stderr, board, mode):
    """The output contract: one JSON line, fixed keys and codes, nothing on stderr, and no
    planted name, temp path or S-shaped string anywhere."""
    for stream in (stdout, stderr):
        assert PLANT.encode() not in stream
        assert str(board.root).encode() not in stream
        assert S_CANARY.encode() not in stream
    assert stderr == b""
    assert stdout.endswith(b"\n") and stdout.count(b"\n") == 1, stdout
    doc = json.loads(stdout)
    if doc["status"] in ("wiped", "clean", "dirty") or "removed" in doc or "differ" in doc:
        assert list(doc) == (WIPE_KEYS if "removed" in doc else CHECK_KEYS), doc
    assert doc["reason"] in REASONS, doc
    assert isinstance(doc.get("removed", doc.get("differ")), int)
    return doc


def tree(path):
    """Every entry under `path`: (relative path, type, mode, owner, content or link target,
    mtime). Never follows a symlink."""
    out = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as fh:
                    body = hashlib.sha256(fh.read()).hexdigest()
            elif stat.S_ISLNK(st.st_mode):
                body = os.readlink(p)
            else:
                body = None
            out.append((os.path.relpath(p, path), stat.S_IFMT(st.st_mode),
                        stat.S_IMODE(st.st_mode), st.st_uid, body, st.st_mtime_ns))
    return sorted(out)


def same_shape(a, b):
    """tree() without the mtimes (a fresh copy has new ones)."""
    return [t[:5] for t in a] == [t[:5] for t in b]


@pytest.fixture
def board(tmp_path):
    return Board(tmp_path)


def _source():
    assert os.path.isfile(SCRIPT), "playbooks/files/daolab-wipe.py is not written yet"
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def _load():
    spec = importlib.util.spec_from_file_location("daolab_wipe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- E-3: Python 3.10, standard library only ---------------------------------------------------

def test_script_parses_as_python_3_10():
    ast.parse(_source(), filename=SCRIPT, feature_version=(3, 10))


def test_script_imports_only_the_standard_library():
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            names = [node.module]
        else:
            continue
        for name in names:
            assert stdlib is None or name.split(".")[0] in stdlib, name


def test_script_has_no_3_11_stdlib_names():
    src = _source()
    for name in ("tomllib", "typing.Self", "ExceptionGroup", "except*", "datetime.UTC"):
        assert name not in src, name


def test_script_is_executable_and_runs_on_python3():
    assert os.access(SCRIPT, os.X_OK)
    assert _source().splitlines()[0] == "#!/usr/bin/python3"


def test_reason_codes_are_the_closed_list():
    assert set(_load().REASONS) == REASONS


# ---- wipe -------------------------------------------------------------------------------------

def test_wipe_removes_everything_and_restores_skel(board):
    board.copy_skel()
    board.plant()
    top = len(os.listdir(board.home))
    outside_before = tree(board.outside)
    r = board.run("wipe")
    assert (r.rc, r.doc) == (0, {"status": "wiped", "reason": "ok", "removed": top})
    assert same_shape(tree(board.home), tree(board.skel))
    assert tree(board.outside) == outside_before       # links were removed, never followed


def test_wipe_keeps_skel_modes_and_gives_everything_to_the_owner(board):
    board.plant()
    gid = os.lstat(board.home).st_gid
    assert board.run("wipe").doc["status"] == "wiped"
    assert stat.S_IMODE(os.lstat(board.home / ".profile").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(board.home / ".config").st_mode) == 0o700
    assert os.readlink(board.home / ".bashrc-link") == ".bashrc"
    for dirpath, dirnames, filenames in os.walk(board.home):
        for name in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, name))
            assert (st.st_uid, st.st_gid) == (os.geteuid(), gid), name


def test_wipe_keeps_the_home_directory_itself(board):
    board.plant()
    before = os.lstat(board.home)
    assert board.run("wipe").doc["status"] == "wiped"
    after = os.lstat(board.home)
    assert (after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode)) == \
        (before.st_ino, before.st_uid, stat.S_IMODE(before.st_mode))


def test_wipe_never_writes_through_a_planted_symlink(board):
    # .bashrc in the home is a link to a file outside: the skel copy must not overwrite it.
    os.symlink(str(board.outside / "keep.txt"), board.home / ".bashrc")
    assert board.run("wipe").doc["status"] == "wiped"
    assert (board.outside / "keep.txt").read_text() == "outside data\n"
    assert not os.path.islink(board.home / ".bashrc")
    assert (board.home / ".bashrc").read_text() == "# skel bashrc\n"


def test_wipe_twice_is_harmless(board):
    board.plant()
    assert board.run("wipe").doc["status"] == "wiped"
    r = board.run("wipe")
    assert (r.rc, r.doc["status"], r.doc["removed"]) == (0, "wiped", len(os.listdir(board.skel)))
    assert same_shape(tree(board.home), tree(board.skel))


def test_wipe_of_an_empty_home_only_copies_skel(board):
    r = board.run("wipe")
    assert r.doc == {"status": "wiped", "reason": "ok", "removed": 0}
    assert same_shape(tree(board.home), tree(board.skel))


def test_wipe_refuses_when_the_result_is_not_skel(board):
    # the self-check: if the copy left the home different from skel, say so (fail closed)
    board.plant()
    mod = _load()
    mod._copy_skel = lambda *a, **k: None
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(board.argv("wipe"))
    doc = json.loads(buf.getvalue())
    assert (rc, doc["status"], doc["reason"]) == (1, "refused", "not_clean")


def test_wipe_refuses_before_touching_the_home_when_skel_is_missing(board):
    board.plant()
    before = tree(board.home)
    r = board.run("wipe", skel=str(board.root / "etc" / "no-skel"))
    assert (r.rc, r.doc["status"], r.doc["reason"]) == (1, "refused", "skel_failed")
    assert tree(board.home) == before


# ---- guards (both modes) -----------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_a_home_of_another_owner_is_refused_and_left_alone(board, mode):
    board.plant()
    before = tree(board.home)
    r = board.run(mode, owner_uid=str(os.geteuid() + 1))
    assert (r.rc, r.doc["status"], r.doc["reason"]) == (1, "refused", "home_owner")
    assert tree(board.home) == before


@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_a_missing_home_is_refused(board, mode):
    r = board.run(mode, home=str(board.root / "home" / "nobody"))
    assert (r.rc, r.doc["reason"]) == (1, "home_missing")


@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_a_home_that_is_a_file_or_a_symlink_is_refused(board, mode):
    (board.root / "home" / "afile").write_text("x")
    os.symlink(str(board.outside), board.root / "home" / "alink")
    for name in ("afile", "alink"):
        r = board.run(mode, home=str(board.root / "home" / name))
        assert (r.rc, r.doc["reason"]) == (1, "home_not_dir"), name
    assert tree(board.outside)[0][0] == "keep.txt"


@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_a_mount_at_or_under_the_home_is_refused(board, mode):
    board.plant()
    before = tree(board.home)
    for point in (board.home, board.home / "sub"):
        board.mountinfo.write_text(BASE_MOUNTS)
        board.add_mount(point)
        r = board.run(mode)
        assert (r.rc, r.doc["reason"]) == (1, "mount_under_home"), point
    assert tree(board.home) == before


@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_unreadable_or_malformed_mountinfo_is_refused(board, mode):
    r = board.run(mode, mountinfo=str(board.root / "no-mountinfo"))
    assert r.doc["reason"] == "mount_under_home"
    board.mountinfo.write_text("garbage\n")
    assert board.run(mode).doc["reason"] == "mount_under_home"


@pytest.mark.parametrize("mode", ["wipe", "check"])
def test_unsafe_paths_are_refused(board, mode):
    for over in ({"home": "home/lab"}, {"home": "/"}, {"home": "/home"}, {"home": "/etc"},
                 {"home": "/x"}, {"home": str(board.home) + "/"},
                 {"home": str(board.home) + "/../lab"}, {"home": str(board.skel)},
                 {"home": str(board.skel / ".config")}, {"skel": str(board.home / "s")},
                 {"skel": "/"}):
        r = board.run(mode, **over)
        assert (r.rc, r.doc["reason"]) == (1, "path_unsafe"), over


def test_bad_arguments_are_refused(board):
    base = board.argv("wipe")
    cases = [
        [a for a in base if a not in ("--mode", "wipe")],                    # no mode
        board.argv("bogus"),
        base + ["--home", str(board.home)],                                  # repeated
        board.argv("wipe", owner_uid="12a"),
        board.argv("wipe", owner_uid="-1"),
        board.argv("wipe", mountinfo="relative/mountinfo"),
        base + ["--extra", "x"],
        [],
    ]
    for argv in cases:
        r = board.run("wipe", argv=argv)
        assert (r.rc, r.doc["status"], r.doc["reason"]) == (1, "refused", "bad_args"), argv


# ---- check (read-only) -------------------------------------------------------------------------

def test_check_says_clean_for_a_home_equal_to_skel(board):
    board.copy_skel()
    r = board.run("check")
    assert (r.rc, r.doc) == (0, {"status": "clean", "reason": "ok", "differ": 0})


@pytest.mark.parametrize("change,n", [
    ("extra", 1), ("missing", 1), ("modified", 1), ("extra+modified", 2),
    ("type", 1), ("deep", 1), ("link", 1)])
def test_check_counts_the_top_level_names_that_differ(board, change, n):
    board.copy_skel()
    if "extra" in change:
        (board.home / f"new-{PLANT}").write_text("x")
    if change == "missing":
        os.unlink(board.home / ".profile")
    if "modified" in change:
        (board.home / ".bashrc").write_text("# other\n")
    if change == "type":
        os.unlink(board.home / ".bashrc")
        os.mkfifo(board.home / ".bashrc")
    if change == "deep":
        (board.home / ".config" / f"more-{PLANT}").write_text("x")
    if change == "link":
        os.unlink(board.home / ".bashrc-link")
        os.symlink(str(board.outside), board.home / ".bashrc-link")
    r = board.run("check")
    assert (r.rc, r.doc) == (0, {"status": "dirty", "reason": "home_differs", "differ": n})


def test_check_changes_nothing(board):
    board.copy_skel()
    board.plant()
    home, outside, skel = tree(board.home), tree(board.outside), tree(board.skel)
    r = board.run("check")
    assert r.doc["status"] == "dirty"
    assert (tree(board.home), tree(board.outside), tree(board.skel)) == (home, outside, skel)


def test_check_never_opens_a_fifo(board):
    # a FIFO named like a skel file must not hang the check (types are compared first)
    board.copy_skel()
    os.unlink(board.home / ".profile")
    os.mkfifo(board.home / ".profile")
    r = board.run("check")                          # subprocess timeout = 120 s
    assert r.doc["status"] == "dirty"
