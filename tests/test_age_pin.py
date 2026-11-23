"""B-103 (errata E-1): age is the official GitHub v1.1.1 release, pinned by sha256 for
linux-amd64 and linux-arm64, plus the binaries inside. No apt, no pyrage.

deploy/age/SHA256SUMS pins the 2 tarballs, deploy/age/BINARIES.sha256 the 4 binaries, and
deploy/age/verify-age.sh checks a tarball against both. The script tests use synthetic
tarballs and a temporary copy of the pins and the script; nothing here touches the network
or the real release. The script runs under dash when it is installed (the DPU's /bin/sh),
else sh."""
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile

import pytest

from tests.conftest import REPO

AGE_DIR = os.path.join(REPO, "deploy", "age")
SCRIPT = os.path.join(AGE_DIR, "verify-age.sh")
ARCHES = ("linux-amd64", "linux-arm64")
TARBALLS = {f"age-v1.1.1-{a}.tar.gz" for a in ARCHES}
BINARIES = {f"{a}/age/{b}" for a in ARCHES for b in ("age", "age-keygen")}
LINE = re.compile(r"([0-9a-f]{64})  (\S+)")
SH = shutil.which("dash") or "sh"


def _entries(name):
    with open(os.path.join(AGE_DIR, name)) as fh:
        lines = fh.read().splitlines()
    out = []
    for line in lines:
        m = LINE.fullmatch(line)
        assert m, f"{name}: malformed line {line!r}"
        out.append((m.group(1), m.group(2)))
    return out


# --- the pin files ---------------------------------------------------------------------

def test_sha256sums_pins_exactly_the_two_tarballs():
    entries = _entries("SHA256SUMS")
    names = [n for _, n in entries]
    assert len(names) == 2 and set(names) == TARBALLS, names
    assert len({h for h, _ in entries}) == 2


def test_binaries_pins_exactly_the_four_binaries_by_arch():
    entries = _entries("BINARIES.sha256")
    labels = [n for _, n in entries]
    assert len(labels) == 4 and set(labels) == BINARIES, labels
    assert len({h for h, _ in entries}) == 4


def test_verify_script_is_posix_sh_with_no_network_or_install():
    with open(SCRIPT) as fh:
        text = fh.read()
    assert text.startswith("#!/bin/sh\n")
    assert os.access(SCRIPT, os.X_OK)
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for word in ("curl", "wget", "apt", "pip", "sudo", "install", "bash", "[["):
        assert word not in code, word
    p = subprocess.run([SH, "-n", SCRIPT], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


# --- verify-age.sh on synthetic tarballs ------------------------------------------------

def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _tarball(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _binaries(arch):
    return {"age/age": f"fake age {arch}\n".encode(),
            "age/age-keygen": f"fake age-keygen {arch}\n".encode(),
            "age/LICENSE": b"license\n"}


@pytest.fixture
def pinned(tmp_path):
    """A copy of verify-age.sh beside synthetic pins, and the synthetic tarballs they pin."""
    pins = tmp_path / "age"
    pins.mkdir()
    shutil.copy2(SCRIPT, pins / "verify-age.sh")
    sums, bins, tarballs = [], [], {}
    for arch in ARCHES:
        name = f"age-v1.1.1-{arch}.tar.gz"
        members = _binaries(arch)
        data = _tarball(members)
        path = tmp_path / "in" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        tarballs[arch] = path
        sums.append(f"{_sha(data)}  {name}\n")
        for b in ("age/age", "age/age-keygen"):
            bins.append(f"{_sha(members[b])}  {arch}/{b}\n")
    (pins / "SHA256SUMS").write_text("".join(sums))
    (pins / "BINARIES.sha256").write_text("".join(bins))
    return pins, tarballs


def run(pins, *args, tmp=None):
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C"}
    if tmp is not None:
        env["TMPDIR"] = str(tmp)
    return subprocess.run([SH, str(pins / "verify-age.sh"), *map(str, args)],
                          env=env, capture_output=True, text=True, timeout=60)


def _one_line_failure(p, needle):
    assert p.returncode != 0, p
    assert p.stdout == "", p.stdout
    assert p.stderr.count("\n") == 1 and needle in p.stderr, p.stderr


@pytest.mark.parametrize("arch", ARCHES)
def test_accepts_a_matching_tarball_and_cleans_up(pinned, tmp_path, arch):
    pins, tarballs = pinned
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    p = run(pins, tarballs[arch], tmp=scratch)
    assert p.returncode == 0, p.stderr
    assert "OK" in p.stdout and tarballs[arch].name in p.stdout
    assert list(scratch.iterdir()) == []


def test_rejects_a_modified_tarball(pinned):
    pins, tarballs = pinned
    path = tarballs["linux-amd64"]
    path.write_bytes(path.read_bytes() + b"\0")
    _one_line_failure(run(pins, path), "tarball sha256 mismatch")


def test_rejects_an_unknown_file_name(pinned, tmp_path):
    pins, tarballs = pinned
    other = tmp_path / "age-v1.1.1-linux-riscv64.tar.gz"
    shutil.copy2(tarballs["linux-amd64"], other)
    _one_line_failure(run(pins, other), "unknown file name")


def test_rejects_a_tarball_whose_binary_differs(pinned):
    """The tarball itself is pinned, but age/age inside it is not the pinned binary."""
    pins, tarballs = pinned
    members = _binaries("linux-arm64")
    members["age/age"] = b"tampered age\n"
    data = _tarball(members)
    path = tarballs["linux-arm64"]
    path.write_bytes(data)
    sums = (pins / "SHA256SUMS").read_text().splitlines()
    sums = [f"{_sha(data)}  {path.name}" if line.endswith(path.name) else line for line in sums]
    (pins / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    _one_line_failure(run(pins, path), "linux-arm64/age/age")


def test_rejects_a_tarball_missing_a_binary(pinned):
    pins, tarballs = pinned
    members = _binaries("linux-amd64")
    del members["age/age-keygen"]
    data = _tarball(members)
    path = tarballs["linux-amd64"]
    path.write_bytes(data)
    sums = (pins / "SHA256SUMS").read_text().splitlines()
    sums = [f"{_sha(data)}  {path.name}" if line.endswith(path.name) else line for line in sums]
    (pins / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    _one_line_failure(run(pins, path), "age-keygen")


def test_usage_without_exactly_one_argument(pinned):
    pins, tarballs = pinned
    _one_line_failure(run(pins), "usage")
    _one_line_failure(run(pins, tarballs["linux-amd64"], tarballs["linux-arm64"]), "usage")
