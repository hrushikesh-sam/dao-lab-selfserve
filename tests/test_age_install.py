"""X-AGE: the pinned age v1.1.1 (errata E-1) installed where it's needed — the controller and CI
(deploy/age/install-age.sh), the DPU (playbooks/tasks/install_age.yml, `bringup.yml --tags age`) —
and the `requires_age` test marker (tests/conftest.py).

Everything here uses synthetic tarballs, synthetic pins and fake binaries (shell scripts that
print a version), like tests/test_age_pin.py; nothing touches the network or the real release.
The one exception is the last test, which needs the real age on PATH (requires_age; CI installs
it). The real v1.1.1 binaries print exactly "v1.1.1\\n" for --version, on stdout, with rc 0
(checked by hand on the downloaded release, plan in the X-AGE findings 8.3).
"""
import hashlib
import io
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile

import pytest
import yaml

from tests.conftest import REPO
from tests.test_playbooks import ANSIBLE, Lab

AGE_DIR = os.path.join(REPO, "deploy", "age")
VERIFY = os.path.join(AGE_DIR, "verify-age.sh")
INSTALL = os.path.join(AGE_DIR, "install-age.sh")
TASKS = os.path.join(REPO, "playbooks", "tasks", "install_age.yml")
BRINGUP = os.path.join(REPO, "playbooks", "bringup.yml")
CI = os.path.join(REPO, ".github", "workflows", "ci.yml")
README = os.path.join(AGE_DIR, "README.md")
ARCHES = ("linux-amd64", "linux-arm64")
HOST = {"x86_64": "linux-amd64", "aarch64": "linux-arm64"}.get(platform.machine())
OTHER = {"linux-amd64": "linux-arm64", "linux-arm64": "linux-amd64"}.get(HOST)
SH = shutil.which("dash") or "sh"
MISSING = "age v1.1.1 not installed (see deploy/age/README.md)"
URL = "https://github.com/FiloSottile/age/releases/download/v1.1.1/age-v1.1.1-linux-amd64.tar.gz"

needs_host_arch = pytest.mark.skipif(HOST is None, reason=f"no age release for {platform.machine()}")
needs_ansible = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed "
                                                       "(pip install -e .[ansible])")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def fake_bin(name, arch, body="echo v1.1.1"):
    """A fake age binary: a shell script. The comment makes every (name, arch) hash distinct."""
    return f"#!/bin/sh\n# fake {name} {arch}\n{body}\n".encode()


def _tarball(members, mode=0o755):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Kit:
    """Copies of verify-age.sh and install-age.sh beside synthetic pins, plus the synthetic
    tarballs they pin (one per arch)."""

    def __init__(self, tmp_path):
        assert os.path.exists(INSTALL), "deploy/age/install-age.sh does not exist"
        self.root = tmp_path
        self.pins = tmp_path / "pins"
        self.pins.mkdir()
        shutil.copy2(VERIFY, self.pins / "verify-age.sh")
        shutil.copy2(INSTALL, self.pins / "install-age.sh")
        self.inp = tmp_path / "in"
        self.inp.mkdir()
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        self.tarballs, self.members = {}, {}

    def build(self, bodies=None, mode=0o755, tamper=None):
        """bodies: {(arch, binary): shell body}; tamper: {(arch, binary): bytes} put in the
        tarball but NOT in the pins (the pins keep the untampered binary)."""
        bodies, tamper = bodies or {}, tamper or {}
        sums, bins = [], []
        for arch in ARCHES:
            members = {f"age/{b}": fake_bin(b, arch, bodies.get((arch, b), "echo v1.1.1"))
                       for b in ("age", "age-keygen")}
            members["age/LICENSE"] = b"license\n"
            for b in ("age", "age-keygen"):
                bins.append(f"{_sha(members[f'age/{b}'])}  {arch}/age/{b}\n")
            shipped = dict(members)
            for (a, b), data in tamper.items():
                if a == arch:
                    shipped[f"age/{b}"] = data
            name = f"age-v1.1.1-{arch}.tar.gz"
            data = _tarball(shipped, mode)
            (self.inp / name).write_bytes(data)
            sums.append(f"{_sha(data)}  {name}\n")
            self.tarballs[arch] = self.inp / name
            self.members[arch] = members
        (self.pins / "SHA256SUMS").write_text("".join(sums))
        (self.pins / "BINARIES.sha256").write_text("".join(bins))
        return self


@pytest.fixture
def kit(tmp_path):
    return Kit(tmp_path)


def install(kit, *args, umask="022", script="install-age.sh"):
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C", "TMPDIR": str(kit.scratch)}
    return subprocess.run(["sh", "-c", f'umask {umask} && exec "$0" "$@"', SH,
                           str(kit.pins / script), *map(str, args)],
                          env=env, capture_output=True, text=True, timeout=60)


def _refused(p, needle, prefix):
    assert p.returncode != 0, (p.stdout, p.stderr)
    assert "install-age: " in p.stderr and needle in p.stderr, p.stderr
    assert not (prefix / "bin").exists() or list((prefix / "bin").iterdir()) == []


# --- install-age.sh ----------------------------------------------------------------------

def test_install_script_is_posix_sh_with_set_eu_and_no_network():
    assert os.path.exists(INSTALL), "deploy/age/install-age.sh does not exist"
    with open(INSTALL) as fh:
        text = fh.read()
    assert text.startswith("#!/bin/sh\n")
    assert os.access(INSTALL, os.X_OK)
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "\nset -eu\n" in code
    for word in ("curl", "wget", "apt", "pip", "sudo", "bash", "[["):
        assert word not in code, word
    assert "verify-age.sh" in code and "install -m 0755" in code
    assert "${2:-/usr/local}" in code                     # the default prefix
    p = subprocess.run([SH, "-n", INSTALL], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@needs_host_arch
def test_installs_both_binaries_0755_from_the_verified_copy(kit):
    kit.build(mode=0o600)                       # the archive's own mode doesn't matter
    prefix = kit.root / "prefix"                # prefix/bin doesn't exist yet: it's created
    p = install(kit, kit.tarballs[HOST], prefix, umask="077")
    assert p.returncode == 0, p.stderr
    assert "install-age: OK" in p.stdout and p.stderr == ""
    for b in ("age", "age-keygen"):
        f = prefix / "bin" / b
        assert stat.S_IMODE(f.stat().st_mode) == 0o755, oct(f.stat().st_mode)
        assert f.read_bytes() == kit.members[HOST][f"age/{b}"]
    assert sorted(os.listdir(prefix / "bin")) == ["age", "age-keygen"]
    assert list(kit.scratch.iterdir()) == []            # its temp dirs are gone


@needs_host_arch
def test_an_existing_bin_dir_keeps_its_mode_and_a_rerun_is_fine(kit):
    kit.build()
    prefix = kit.root / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin").chmod(0o700)
    assert install(kit, kit.tarballs[HOST], prefix).returncode == 0
    p = install(kit, kit.tarballs[HOST], prefix)
    assert p.returncode == 0, p.stderr
    assert stat.S_IMODE((prefix / "bin").stat().st_mode) == 0o700


@needs_host_arch
def test_rejects_a_tampered_tarball(kit):
    kit.build()
    path = kit.tarballs[HOST]
    path.write_bytes(path.read_bytes() + b"\0")
    p = install(kit, path, kit.root / "prefix")
    _refused(p, "verify-age.sh refused", kit.root / "prefix")
    assert "tarball sha256 mismatch" in p.stderr                  # verify-age.sh's own line
    assert list(kit.scratch.iterdir()) == []


@needs_host_arch
def test_rejects_a_tarball_for_the_other_arch_before_extracting_anything(kit):
    kit.build()
    p = install(kit, kit.tarballs[OTHER], kit.root / "prefix")
    _refused(p, "wrong arch", kit.root / "prefix")
    assert OTHER in p.stderr and HOST in p.stderr
    assert list(kit.scratch.iterdir()) == []


@needs_host_arch
def test_rejects_a_binary_that_differs_from_its_pin_even_if_the_tarball_is_pinned(kit):
    kit.build(tamper={(HOST, "age-keygen"): b"#!/bin/sh\necho v1.1.1\n"})
    p = install(kit, kit.tarballs[HOST], kit.root / "prefix")
    _refused(p, "verify-age.sh refused", kit.root / "prefix")
    assert f"{HOST}/age/age-keygen" in p.stderr


@needs_host_arch
@pytest.mark.parametrize("binary", ["age", "age-keygen"])
@pytest.mark.parametrize("body", [
    "echo v1.2.0",                       # another version
    "echo v1.1.1-extra",                 # not exact
    "echo 'v1.1.1 '",                    # trailing text
    "echo v1.1.1; echo more",            # a second line
    "echo; echo v1.1.1",                 # a leading line
    "echo v1.1.1; exit 3",               # right text, failing rc
])
def test_rejects_a_pinned_binary_whose_version_is_not_exactly_v1_1_1(kit, binary, body):
    kit.build(bodies={(HOST, binary): body})
    p = install(kit, kit.tarballs[HOST], kit.root / "prefix")
    _refused(p, "not exactly v1.1.1", kit.root / "prefix")
    assert f"age/{binary}" in p.stderr
    assert list(kit.scratch.iterdir()) == []


@needs_host_arch
def test_installs_the_file_it_verified_not_the_file_named_on_the_command_line(kit):
    """A stand-in verify-age.sh swaps the ORIGINAL tarball for a tampered (but re-pinned-looking)
    one while the real check runs on install-age.sh's private copy: the installed binaries must
    still be the pinned ones."""
    kit.build()
    real = kit.pins / "real-verify.sh"
    shutil.move(kit.pins / "verify-age.sh", real)
    evil = kit.root / "evil.tar.gz"
    evil.write_bytes(_tarball({"age/age": b"#!/bin/sh\necho v1.1.1\n# evil\n",
                               "age/age-keygen": b"#!/bin/sh\necho v1.1.1\n# evil\n"}))
    seen = kit.root / "verify-arg"
    original = kit.tarballs[HOST]
    (kit.pins / "verify-age.sh").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$1\" > '{seen}'\n"
        f"cp '{evil}' '{original}'\n"
        f"exec sh '{real}' \"$1\"\n")
    prefix = kit.root / "prefix"
    p = install(kit, original, prefix)
    assert p.returncode == 0, p.stderr
    assert seen.read_text().strip() != str(original)            # it checked a private copy
    assert original.read_bytes() == evil.read_bytes()           # the swap really happened
    for b in ("age", "age-keygen"):
        assert (prefix / "bin" / b).read_bytes() == kit.members[HOST][f"age/{b}"]


@pytest.mark.parametrize("args,needle", [
    ((), "usage"),
    (("a", "b", "c"), "usage"),
    (("{missing}", "{prefix}"), "not a file"),
    (("{tarball}", "relative/prefix"), "absolute"),
    (("{renamed}", "{prefix}"), "unknown file name"),
])
def test_refuses_bad_arguments(kit, args, needle):
    kit.build()
    renamed = kit.root / "age.tar.gz"
    shutil.copy2(kit.tarballs["linux-amd64"], renamed)
    subs = {"{missing}": str(kit.root / "nope.tar.gz"), "{prefix}": str(kit.root / "prefix"),
            "{tarball}": str(kit.tarballs[HOST or "linux-amd64"]), "{renamed}": str(renamed)}
    p = install(kit, *[subs.get(a, a) for a in args])
    _refused(p, needle, kit.root / "prefix")


# --- the requires_age marker (tests/conftest.py) -----------------------------------------

INNER = """import pytest

@pytest.mark.requires_age
def test_needs_age():
    pass

def test_plain():
    pass
"""


def _fake_tools(d, age="echo v1.1.1", keygen="echo v1.1.1"):
    d.mkdir(parents=True, exist_ok=True)
    for name, body in (("age", age), ("age-keygen", keygen)):
        if body is not None:
            (d / name).write_text(f"#!/bin/sh\n{body}\n")
            (d / name).chmod(0o755)
    return d


def run_inner(tmp_path, path_dirs, extra=None, args=()):
    """Run pytest on a two-test file with tests/conftest.py loaded as a plugin, in a clean
    environment (CI's DAOLAB_REQUIRE_AGE=1 must not leak in) with the given PATH."""
    inner = tmp_path / "inner"
    inner.mkdir(exist_ok=True)
    (inner / "test_inner.py").write_text(INNER)
    env = {"PATH": os.pathsep.join(str(d) for d in path_dirs), "PYTHONPATH": REPO,
           "LC_ALL": "C", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})}
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.conftest",
                           "-p", "no:cacheprovider", "-q", "-rsfE", "--strict-markers",
                           "--rootdir", str(inner), *args, str(inner / "test_inner.py")],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def _summary(p):
    return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-500:]


def test_marker_skips_without_age(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    p = run_inner(tmp_path, [empty])
    assert "1 passed, 1 skipped" in _summary(p), p.stdout + p.stderr
    assert MISSING in p.stdout


def test_marker_fails_without_age_when_required(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    p = run_inner(tmp_path, [empty], {"DAOLAB_REQUIRE_AGE": "1"})
    assert p.returncode == 1 and "1 failed, 1 passed" in _summary(p), p.stdout + p.stderr
    assert MISSING in p.stdout and "test_needs_age" in p.stdout


def test_marker_runs_the_test_with_a_v1_1_1_age_on_path(tmp_path):
    p = run_inner(tmp_path, [_fake_tools(tmp_path / "bin")], {"DAOLAB_REQUIRE_AGE": "1"})
    assert p.returncode == 0 and "2 passed" in _summary(p), p.stdout + p.stderr


@pytest.mark.parametrize("age,keygen", [
    ("echo v1.0.0", "echo v1.1.1"),          # an apt-style older age
    ("echo v1.1.1", "echo v1.2.0"),
    ("echo v1.1.1", None),                   # no age-keygen
    (None, "echo v1.1.1"),                   # no age
    ("echo v1.1.1; exit 1", "echo v1.1.1"),
])
def test_marker_skips_a_wrong_or_partial_install(tmp_path, age, keygen):
    p = run_inner(tmp_path, [_fake_tools(tmp_path / "bin", age, keygen)])
    assert "1 passed, 1 skipped" in _summary(p), p.stdout + p.stderr
    assert MISSING in p.stdout


def test_marker_uses_daolab_age_bin_and_keygen_bin(tmp_path):
    tools = _fake_tools(tmp_path / "elsewhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    p = run_inner(tmp_path, [empty], {"DAOLAB_AGE_BIN": str(tools / "age"),
                                      "DAOLAB_AGE_KEYGEN_BIN": str(tools / "age-keygen"),
                                      "DAOLAB_REQUIRE_AGE": "1"})
    assert p.returncode == 0 and "2 passed" in _summary(p), p.stdout + p.stderr
    # ... and a DAOLAB_AGE_BIN pointing at a wrong version wins over a good age on PATH
    bad = _fake_tools(tmp_path / "bad", age="echo v1.0.0")
    p = run_inner(tmp_path, [_fake_tools(tmp_path / "bin")], {"DAOLAB_AGE_BIN": str(bad / "age")})
    assert "1 passed, 1 skipped" in _summary(p), p.stdout + p.stderr


def test_marker_is_registered(tmp_path):
    p = run_inner(tmp_path, [tmp_path], args=("--markers",))
    assert "@pytest.mark.requires_age" in p.stdout, p.stdout[-1500:]


# --- service/config.py -------------------------------------------------------------------

def test_age_settings_default_to_the_names_on_path(monkeypatch):
    from service import config
    monkeypatch.delenv("DAOLAB_AGE_BIN", raising=False)
    monkeypatch.delenv("DAOLAB_AGE_KEYGEN_BIN", raising=False)
    s = config.Settings.from_env()
    assert (s.age_bin, s.age_keygen_bin) == ("age", "age-keygen")


def test_age_settings_come_from_the_environment(monkeypatch):
    from service import config
    monkeypatch.setenv("DAOLAB_AGE_BIN", "/opt/age/bin/age")
    monkeypatch.setenv("DAOLAB_AGE_KEYGEN_BIN", "/opt/age/bin/age-keygen")
    s = config.Settings.from_env()
    assert (s.age_bin, s.age_keygen_bin) == ("/opt/age/bin/age", "/opt/age/bin/age-keygen")
    assert "DAOLAB_AGE_BIN" not in config.SECRET_ENV            # paths, not secrets


# --- CI, .gitignore, README --------------------------------------------------------------

def _ci_steps():
    with open(CI) as fh:
        return yaml.safe_load(fh)["jobs"]["test"]["steps"]


def test_ci_installs_the_pinned_age_before_the_tests_and_requires_it():
    steps = _ci_steps()
    names = [s.get("name") for s in steps]
    idx = [i for i, s in enumerate(steps) if "install-age.sh" in s.get("run", "")]
    assert len(idx) == 1, names
    step = steps[idx[0]]
    run = step["run"]
    assert "curl --fail" in run and URL in run
    assert 'install-age.sh "$d/age-v1.1.1-linux-amd64.tar.gz" "$HOME/.local"' in run
    assert 'echo "$HOME/.local/bin" >> "$GITHUB_PATH"' in run
    test = names.index("Test")
    assert names.index("Install") < idx[0] < test
    assert steps[test].get("env", {}).get("DAOLAB_REQUIRE_AGE") == "1"


def test_gitignore_covers_both_venvs():
    if not os.path.isdir(os.path.join(REPO, ".git")) and not os.path.isfile(os.path.join(REPO, ".git")):
        pytest.skip("not a git checkout")
    # The rule must come from the repo's own .gitignore: a venv made by `uv venv` carries a
    # private `*` .gitignore, which would make this pass on one machine and not another.
    for path in (".venv/bin/python", ".venv-220/bin/python", ".venv-220/pyvenv.cfg"):
        p = subprocess.run(["git", "-C", REPO, "check-ignore", "-v", "--no-index", path],
                           capture_output=True, text=True)
        assert p.returncode == 0, f"{path} is not ignored"
        source = p.stdout.split(":", 1)[0]
        assert source == ".gitignore", f"{path} is ignored only by {source}, not the repo's .gitignore"


def test_readme_says_how_to_install_on_the_controller_and_the_dpu():
    with open(README) as fh:
        text = fh.read()
    for needle in ("install-age.sh", "/usr/local", "--tags age", "daolab_age_arm64_tarball",
                   "requires_age", "DAOLAB_REQUIRE_AGE", "age-v1.1.1-linux-arm64.tar.gz",
                   "age-v1.1.1-linux-amd64.tar.gz"):
        assert needle in text, needle
    assert "installing the verified binaries is a\nlater task" not in text
    assert "is a later task" not in text


# --- the DPU task: playbooks/tasks/install_age.yml via `bringup.yml --tags age` -----------

def test_bringup_runs_the_age_task_only_when_asked_and_always_resolves():
    with open(BRINGUP) as fh:
        plays = yaml.safe_load(fh)
    imports = [(p, t) for p in plays for t in p.get("tasks", [])
               if "ansible.builtin.import_tasks" in t]
    resolve = [t for _, t in imports if t["ansible.builtin.import_tasks"] == "tasks/resolve.yml"]
    age = [(p, t) for p, t in imports if t["ansible.builtin.import_tasks"] == "tasks/install_age.yml"]
    assert len(resolve) == 1 and resolve[0].get("tags") == ["always"]
    assert len(age) == 1
    play, task = age[0]
    assert play["hosts"] == "r_dpu" and task.get("tags") == ["age", "never"]


def test_the_dpu_binaries_are_owned_by_root_outside_tests():
    with open(TASKS) as fh:
        text = fh.read()
    assert text.count("owner: \"{{ 'root' if _age_root == '' else omit }}\"") == 1
    assert text.count("group: \"{{ 'root' if _age_root == '' else omit }}\"") == 1
    assert "/usr/local/bin/" in text and 'mode: "0755"' in text


class DpuKit(Kit):
    """A Kit whose arm64 tarball goes to the fake DPU (the fake binaries are shell scripts, so
    they run on any controller)."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.lab = Lab(_labdir(tmp_path))
        self.bin = self.lab.dir / "root" / "usr" / "local" / "bin"
        self.bin.mkdir(parents=True)          # the DPU's /usr/local/bin exists
        self.ctl_tmp = tmp_path / "ctl-tmp"
        self.ctl_tmp.mkdir()


def _labdir(tmp_path):
    d = tmp_path / "lab"
    d.mkdir()
    return d


@pytest.fixture
def dpu(tmp_path):
    return DpuKit(tmp_path)


def dpu_run(dpu, monkeypatch, tags="age", **ev):
    if tags:
        monkeypatch.setenv("ANSIBLE_RUN_TAGS", tags)
    else:
        monkeypatch.delenv("ANSIBLE_RUN_TAGS", raising=False)
    monkeypatch.setenv("TMPDIR", str(dpu.ctl_tmp))
    base = {"ansible_become": False, "daolab_bringup_root": str(dpu.lab.dir / "root"),
            "daolab_age_root": str(dpu.lab.dir / "root"), "daolab_age_pin_dir": str(dpu.pins),
            "hw_class": "sdp-pf1-vf8", "daolab_deploy_vms": False}
    base.update(ev)
    return dpu.lab.run("bringup.yml", **{k: v for k, v in base.items() if v is not None})


@needs_ansible
def test_dpu_task_installs_both_binaries_0755_and_checks_them(dpu, monkeypatch):
    dpu.build(mode=0o600)
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=str(dpu.tarballs["linux-arm64"]))
    assert rc == 0, dpu.lab.stdout[-3000:]
    for b in ("age", "age-keygen"):
        f = dpu.bin / b
        assert f.read_bytes() == dpu.members["linux-arm64"][f"age/{b}"]
        assert stat.S_IMODE(f.stat().st_mode) == 0o755
    assert "age: installed binaries match the linux-arm64 pins" in dpu.lab.stdout
    assert "age: --version is exactly v1.1.1" in dpu.lab.stdout
    assert list(dpu.ctl_tmp.iterdir()) == []                # the controller work dir is gone


@needs_ansible
@pytest.mark.parametrize("value", [None, ""])
def test_dpu_task_refuses_a_missing_tarball_var(dpu, monkeypatch, value):
    dpu.build()
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=value)
    assert rc != 0
    assert "set -e daolab_age_arm64_tarball=" in dpu.lab.stdout, dpu.lab.stdout[-3000:]
    assert list(dpu.bin.iterdir()) == []


@needs_ansible
def test_dpu_task_refuses_the_amd64_tarball(dpu, monkeypatch):
    dpu.build()
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=str(dpu.tarballs["linux-amd64"]))
    assert rc != 0 and "set -e daolab_age_arm64_tarball=" in dpu.lab.stdout
    assert list(dpu.bin.iterdir()) == []


@needs_ansible
def test_dpu_task_refuses_a_tarball_with_a_bad_hash(dpu, monkeypatch):
    dpu.build()
    path = dpu.tarballs["linux-arm64"]
    path.write_bytes(path.read_bytes() + b"\0")
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=str(path))
    assert rc != 0
    assert "tarball sha256 mismatch" in dpu.lab.stdout, dpu.lab.stdout[-3000:]
    assert list(dpu.bin.iterdir()) == []
    assert list(dpu.ctl_tmp.iterdir()) == []


@needs_ansible
def test_dpu_side_pin_check_refuses_a_bad_binary_even_if_verify_were_bypassed(dpu, monkeypatch):
    """Defence in depth: with verify-age.sh replaced by a stub that says OK, a binary that isn't
    the arm64 pin must never replace the DPU's existing /usr/local/bin/age (copy's validate:)."""
    dpu.build(tamper={("linux-arm64", "age"): b"#!/bin/sh\necho v1.1.1\n# not the pin\n"})
    (dpu.pins / "verify-age.sh").write_text("#!/bin/sh\necho 'verify-age: OK (stub)'\n")
    old = b"#!/bin/sh\necho previous age\n"
    (dpu.bin / "age").write_bytes(old)
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=str(dpu.tarballs["linux-arm64"]))
    assert rc != 0, dpu.lab.stdout[-3000:]
    assert "do not match the linux-arm64 pins; nothing was copied to the DPU" in dpu.lab.stdout, \
        dpu.lab.stdout[-3000:]
    assert (dpu.bin / "age").read_bytes() == old
    assert not (dpu.bin / "age-keygen").exists()
    assert list(dpu.ctl_tmp.iterdir()) == []


@needs_ansible
def test_the_dpu_copy_validate_refuses_a_binary_that_is_not_its_pin(tmp_path):
    """The second gate on its own: install_age.yml's real copy task (its validate: line), run
    with a source file that isn't the pin, must fail and leave the existing file untouched; a
    file that is its pin is installed 0755."""
    with open(TASKS) as fh:
        tasks = yaml.safe_load(fh)
    outer = next(t for t in tasks if "block" in t and "always" in t)
    copy = next(t for t in outer["block"] if "validate" in t.get("ansible.builtin.copy", {}))
    src = tmp_path / "src"
    (src / "age").mkdir(parents=True)
    good_age, good_kg = b"#!/bin/sh\necho v1.1.1\n# a\n", b"#!/bin/sh\necho v1.1.1\n# k\n"
    (src / "age" / "age").write_bytes(b"#!/bin/sh\necho v1.1.1\n# not the pin\n")
    (src / "age" / "age-keygen").write_bytes(good_kg)
    root = tmp_path / "root"
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    old = b"#!/bin/sh\necho previous age\n"
    (root / "usr" / "local" / "bin" / "age").write_bytes(old)
    play = [{"hosts": "localhost", "gather_facts": False,
             "vars": {"_age_root": str(root), "_age_tmp": {"path": str(src)},
                      "_age_pins": {"age": [_sha(good_age)], "age-keygen": [_sha(good_kg)]}},
             "tasks": [copy]}]
    pb = tmp_path / "copy.yml"
    pb.write_text(yaml.safe_dump(play))
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8", "HOME": str(tmp_path),
           "ANSIBLE_LOCAL_TEMP": str(tmp_path / "t"), "ANSIBLE_HOME": str(tmp_path / "ah"),
           "ANSIBLE_NOCOLOR": "1"}
    p = subprocess.run([ANSIBLE, "-i", "localhost,", "-c", "local", str(pb),
                        "-e", f"ansible_python_interpreter={sys.executable}"],
                       env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "failed to validate" in out, out[-3000:]
    assert (root / "usr" / "local" / "bin" / "age").read_bytes() == old
    kg = root / "usr" / "local" / "bin" / "age-keygen"
    assert kg.read_bytes() == good_kg and stat.S_IMODE(kg.stat().st_mode) == 0o755


@needs_ansible
def test_dpu_task_refuses_a_pinned_binary_with_the_wrong_version(dpu, monkeypatch):
    dpu.build(bodies={("linux-arm64", "age-keygen"): "echo v1.2.0"})
    rc = dpu_run(dpu, monkeypatch, daolab_age_arm64_tarball=str(dpu.tarballs["linux-arm64"]))
    assert rc != 0
    assert "--version on the DPU is not exactly v1.1.1" in dpu.lab.stdout, dpu.lab.stdout[-3000:]


@needs_ansible
def test_a_plain_bringup_does_not_install_age(dpu, monkeypatch):
    dpu.build()
    dpu_run(dpu, monkeypatch, tags=None,
            daolab_age_arm64_tarball=str(dpu.tarballs["linux-arm64"]))
    assert "TASK [age: " not in dpu.lab.stdout, dpu.lab.stdout[-3000:]
    assert list(dpu.bin.iterdir()) == []
    assert "PLAY [the DPU]" in dpu.lab.stdout          # the normal bringup did run


# --- the real age (CI installs it; skipped locally unless installed) -----------------------

@pytest.mark.requires_age
def test_the_age_on_path_is_the_pinned_v1_1_1():
    pins = {}
    with open(os.path.join(AGE_DIR, "BINARIES.sha256")) as fh:
        for line in fh:
            h, label = line.split()
            pins[label] = h
    for var, name in (("DAOLAB_AGE_BIN", "age"), ("DAOLAB_AGE_KEYGEN_BIN", "age-keygen")):
        path = shutil.which(os.environ.get(var) or name)
        p = subprocess.run([path, "--version"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        assert (p.returncode, p.stdout) == (0, b"v1.1.1\n"), (name, p)
        if HOST:
            with open(path, "rb") as fh:
                assert _sha(fh.read()) == pins[f"{HOST}/age/{name}"], name
