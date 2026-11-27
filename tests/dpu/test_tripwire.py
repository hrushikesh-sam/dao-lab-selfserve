"""B-112a: playbooks/files/daolab-tripwire.py, the DPU-side tripwire checker (design v1.1 §3).

The checker runs on the DPU's Python 3.10, so this file is stdlib + pytest only: no conftest,
no `service` import. It runs on the controller venv (3.12) and on a 3.10 venv with
``--confcutdir=tests/dpu``. Every run works on a fake board under tmp_path (``--root`` /
``--proc``); nothing here reads this machine's real / or /proc.

The checker is used two ways: as a subprocess (the end-to-end contract: what it prints and its
exit code), and imported by path, to test each collector on its own.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "playbooks", "files", "daolab-tripwire.py")
CLASSES = ("identity", "sudo", "ssh", "sched", "boot", "bin", "kmod", "mount", "proc", "tmp",
           "daolab")
FORMAT = "daolab-tripwire/1"
COLLECT_FORMAT = "daolab-tripwire-collect/1"
TOKEN = "zq7plant"                  # every planted name contains it; no output ever may
PLANT_UID = 54321                   # the planted user's and process's uid; no output ever may
LINE_RE = re.compile(r"tripwire:(ok|(%s):[1-9][0-9]*)" % "|".join(CLASSES))
HEX_RE = re.compile(r"[0-9a-f]{64}")
TIMEOUT = 60                        # a hang (symlink loop, FIFO) fails instead of blocking
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# ---- the fake board ------------------------------------------------------------------------
def write(base, rel, text="", mode=None):
    """Create or rewrite base/rel; an existing read-only file (e.g. 0440) keeps its mode."""
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path) and not os.path.islink(path):
        old = stat.S_IMODE(os.stat(path).st_mode)
        os.chmod(path, old | stat.S_IWUSR)
        mode = old if mode is None else mode
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def link(base, rel, target):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)
    return path


def mkdir(base, rel):
    path = os.path.join(base, rel)
    os.makedirs(path, exist_ok=True)
    return path


def append(base, rel, text):
    with open(os.path.join(base, rel), "a", encoding="utf-8") as fh:
        fh.write(text)


def drop_lines(base, rel, needle):
    path = os.path.join(base, rel)
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    kept = [line for line in lines if needle not in line]
    assert len(kept) == len(lines) - 1, f"expected exactly one line with {needle!r}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(kept)


def replace_in(base, rel, old, new):
    path = os.path.join(base, rel)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count(old) == 1, f"expected {old!r} once in {rel}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


def pid(proc, n, exe, uid, name="x"):
    """A fake <proc>/<n>: `exe` a symlink (None = a kernel thread: no exe), `status` with Uid."""
    d = mkdir(proc, str(n))
    if exe is not None:
        os.symlink(exe, os.path.join(d, "exe"))
    with open(os.path.join(d, "status"), "w", encoding="utf-8") as fh:
        fh.write(f"Name:\t{name}\nState:\tS (sleeping)\nPid:\t{n}\nPPid:\t1\n"
                 f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    return d


PASSWD = ("root:x:0:0:root:/root:/bin/bash\n"
          "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
          "dao-test:x:1000:1000:dao-test:/home/dao-test:/bin/bash\n"
          "lab:x:1001:1001::/home/lab:/bin/bash\n")
GROUP = "root:x:0:\nsudo:x:27:dao-test\ndao-test:x:1000:\nlab:x:1001:\n"
SHADOW = ("root:*:19000:0:99999:7:::\n"
          "daemon:*:19000:0:99999:7:::\n"
          "dao-test:$6$saltsalt$operatorhash:19000:0:99999:7:::\n"
          "lab:!$6$labsalt$labhash:19900:0:99999:7::0:\n")
GSHADOW = "root:*::\nsudo:*::dao-test\ndao-test:!::\nlab:!::\n"
MODULES = ("vfio_pci 16384 0 - Live 0x0000000000000000\n"
           "rvu_nicpf 65536 0 - Live 0x0000000000000000\n"
           "rvu_cptpf 16384 0 - Live 0x0000000000000000\n")
MOUNTINFO = ("22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
             "23 22 0:21 / /proc rw,nosuid,nodev,noexec shared:12 - proc proc rw\n"
             "24 22 0:5 / /dev rw,nosuid shared:2 - devtmpfs udev rw\n"
             "25 24 0:23 / /dev/shm rw,nosuid,nodev shared:4 - tmpfs tmpfs rw\n"
             "26 22 0:40 / /mnt/huge rw,relatime shared:30 - hugetlbfs nodev rw,pagesize=512M\n"
             "27 22 0:41 / /mnt/with\\040space rw shared:31 - tmpfs tmpfs rw\n")


def make_tree(base):
    """A clean fake board under `base`: (root, proc). Every class has entries, and the four
    playbook noise sources of FINDINGS §7 are present. Deterministic: the same tree every time."""
    root, proc = os.path.join(base, "root"), os.path.join(base, "proc")
    # identity (+ etc/skel, etc/profile.d; etc/profile, bash.bashrc, environment stay absent so
    # a plant is a pure addition)
    write(root, "etc/passwd", PASSWD, 0o644)
    write(root, "etc/group", GROUP, 0o644)
    write(root, "etc/shadow", SHADOW, 0o640)
    write(root, "etc/gshadow", GSHADOW, 0o640)
    write(root, "etc/skel/.bashrc", "# skel bashrc\n")
    write(root, "etc/skel/.profile", "# skel profile\n")
    write(root, "etc/profile.d/01-locale-fix.sh", "# locale\n")
    # sudo
    write(root, "etc/sudoers", "root ALL=(ALL:ALL) ALL\n@includedir /etc/sudoers.d\n", 0o440)
    write(root, "etc/sudoers.d/README", "# drop-ins\n", 0o440)
    write(root, "etc/sudoers.d/90-daolab-lab", "lab ALL=(ALL) NOPASSWD:ALL\n", 0o440)
    # ssh
    write(root, "etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\n")
    write(root, "etc/ssh/sshd_config.d/10-daolab-lab.conf", "Match User lab\n")
    write(root, "etc/ssh/ssh_host_ed25519_key", "fake host key\n", 0o600)
    write(root, "etc/ssh/ssh_host_ed25519_key.pub", "ssh-ed25519 AAAAhost\n")
    write(root, "root/.ssh/authorized_keys", "ssh-ed25519 AAAAroot break-glass\n", 0o600)
    write(root, "home/dao-test/.ssh/authorized_keys", "ssh-ed25519 AAAAops operator\n", 0o600)
    write(root, "home/lab/.ssh/authorized_keys", "ssh-ed25519 AAAAlab booking-1\n", 0o600)
    # sched
    write(root, "etc/crontab", "17 * * * * root cd / && run-parts --report /etc/cron.hourly\n")
    write(root, "etc/cron.d/e2scrub_all", "30 3 * * 0 root true\n")
    write(root, "etc/cron.daily/apt-compat", "#!/bin/sh\n", 0o755)
    for d in ("hourly", "weekly", "monthly"):
        write(root, f"etc/cron.{d}/.placeholder", "")
    mkdir(root, "var/spool/cron/crontabs")
    write(root, "var/spool/cron/atjobs/.SEQ", "0\n")
    link(root, "etc/systemd/system/timers.target.wants/apt-daily.timer",
         "/lib/systemd/system/apt-daily.timer")
    # boot
    link(root, "etc/systemd/system/multi-user.target.wants/ssh.service",
         "/lib/systemd/system/ssh.service")
    link(root, "etc/systemd/system/sshd.service", "/lib/systemd/system/ssh.service")
    write(root, "etc/systemd/system/nginx-tls.service", "[Service]\nExecStart=/opt/nginx\n")
    write(root, "etc/systemd/system/daolab-otel.service", "[Service]\nExecStart=/bin/true\n")
    write(root, "etc/systemd/system/daolab-app-exporter.service",        # noise source 2
          "[Service]\nExecStart=/usr/local/bin/daolab-app-exporter --app nginx\n")
    # bin
    write(root, "usr/bin/sudo", "#!/bin/sh\n", 0o4755)
    write(root, "usr/bin/passwd", "#!/bin/sh\n", 0o4755)
    write(root, "usr/bin/crontab", "#!/bin/sh\n", 0o2755)
    write(root, "usr/bin/ls", "#!/bin/sh\n", 0o755)
    write(root, "usr/sbin/sshd", "#!/bin/sh\n", 0o755)
    write(root, "usr/local/bin/daolab-app-exporter", "#!/usr/bin/env python3\n", 0o755)
    write(root, "usr/local/bin/daolab-keys", "#!/bin/sh\n", 0o755)
    write(root, "usr/local/bin/daolab-ml-smoke", "#!/bin/sh\n", 0o755)
    mkdir(root, "usr/local/sbin")
    write(root, "usr/local/lib/daolab/otelcol.tar", "tar\n")
    write(root, "opt/async_nginx.conf", "proxy_pass http://10.0.0.1;\n")
    link(root, "bin", "usr/bin")
    link(root, "sbin", "usr/sbin")
    write(root, "etc/ld.so.conf.d/libc.conf", "/usr/local/lib\n")
    # kmod (etc/modules stays absent; etc/modules-load.d exists, empty)
    write(root, "etc/modprobe.d/blacklist.conf", "blacklist evbug\n")
    mkdir(root, "etc/modules-load.d")
    write(proc, "modules", MODULES)
    # mount
    write(proc, "self/mountinfo", MOUNTINFO)
    # proc: two sshd (root, operator), the operator's bash, nginx twice (collapses), a kernel
    # thread (no exe: skipped), systemd
    pid(proc, 9000001, "/usr/lib/systemd/systemd", 0, "systemd")
    pid(proc, 9000002, "/usr/sbin/sshd", 0, "sshd")
    pid(proc, 9000003, "/usr/sbin/sshd", 1000, "sshd")
    pid(proc, 9000004, "/usr/bin/bash", 1000, "bash")
    pid(proc, 9000005, None, 0, "kworker/0:1")
    pid(proc, 9000006, "/usr/sbin/nginx", 0, "nginx")
    pid(proc, 9000007, "/usr/sbin/nginx", 0, "nginx")
    # tmp
    write(root, "tmp/pci-after.txt", "0002:03:00.0 rvu_nicpf\n")           # noise source 3
    mkdir(root, "tmp/.X11-unix")
    write(root, "var/tmp/keep.dat", "keep\n")
    mkdir(root, "var/tmp/cloud-init")
    mkdir(root, "dev/shm")
    # daolab
    write(root, "etc/daolab/hw_class", "test\n")
    write(root, "etc/daolab/keys.conf", "DAOLAB_API=http://127.0.0.1\n", 0o600)
    write(root, "etc/daolab/authorized_keys/lab", "")
    write(root, "etc/daolab/otelcol.yaml", "daolab.reservation: 1\n")       # noise source 1
    return root, proc


# ---- running it ------------------------------------------------------------------------------
def _script():
    assert os.path.isfile(SCRIPT), "the checker playbooks/files/daolab-tripwire.py does not exist"
    return SCRIPT


def _bytes(data):
    if isinstance(data, (dict, list)):
        return json.dumps(data).encode("utf-8")
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def run_raw(args, data=b"", **kwargs):
    """The checker as the DPU runs it (python3 -I -B), with exactly `args`."""
    return subprocess.run([sys.executable, "-I", "-B", _script(), *args], input=_bytes(data),
                          capture_output=True, timeout=TIMEOUT, **kwargs)


def run(root, proc, mode, data, extra=()):
    return run_raw(["--mode", mode, "--root", root, "--proc", proc, *extra], data)


def collect_doc(ignore=None):
    return {"format": COLLECT_FORMAT, "ignore": dict(ignore or {})}


def collect(root, proc, ignore=None):
    r = run(root, proc, "collect", collect_doc(ignore))
    assert (r.returncode, r.stderr) == (0, b""), r
    out = r.stdout.decode("ascii")
    assert out.endswith("\n") and out.count("\n") == 1, "collect prints exactly one line"
    doc = json.loads(out)
    assert list(doc) == ["classes"] and list(doc["classes"]) == list(CLASSES)
    result = {}
    for c in CLASSES:
        entry = doc["classes"][c]
        assert list(entry) == ["fingerprints"]
        fps = entry["fingerprints"]
        assert fps == sorted(set(fps)) and all(HEX_RE.fullmatch(f) for f in fps)
        result[c] = fps
    return result


def mini_baseline(collected, ignore=None, hw_class="test", version=1):
    """The controller's build_baseline in ten lines (tests/dpu may not import `service`)."""
    ignore = ignore or {}
    return {"format": FORMAT, "version": version, "hw_class": hw_class,
            "classes": {c: {"fingerprints": list(collected[c]), "ignore": list(ignore.get(c, []))}
                        for c in CLASSES}}


def fresh(tmp_path, ignore=None):
    root, proc = make_tree(str(tmp_path))
    return root, proc, mini_baseline(collect(root, proc, ignore), ignore)


def check(root, proc, baseline):
    return run(root, proc, "check", baseline)


def assert_counts(r, expected):
    """Exit 0, nothing on stderr, and exactly the expected lines in the fixed class order."""
    assert r.stderr == b"", r.stderr
    assert r.returncode == 0, r
    want = [f"tripwire:{c}:{expected[c]}" for c in CLASSES if expected.get(c)] or ["tripwire:ok"]
    assert r.stdout.decode("ascii").splitlines() == want
    assert r.stdout.endswith(b"\n")


def assert_error(r, code):
    assert (r.stdout, r.stderr, r.returncode) == (f"tripwire:error:{code}\n".encode(), b"", 2)


# ---- the checker imported by path (to test each collector on its own) ------------------------
_MOD = None


def mod():
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("daolab_tripwire", _script())
        module = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True          # no __pycache__ next to the shipped script
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old
        _MOD = module
    return _MOD


def records(cls, root, proc, ignore=()):
    m = mod()
    return m.COLLECTORS[cls](m.Ctx(root, proc, ignore))


# ============================================================================================
# The script itself
# ============================================================================================
def test_script_is_an_executable_python3_stdlib_only_file():
    path = _script()
    assert os.stat(path).st_mode & stat.S_IXUSR
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert src.startswith("#!/usr/bin/env python3\n")
    import ast
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module.split(".")[0])
    assert names and names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


def test_the_class_order_is_the_contract():
    assert tuple(mod().CLASSES) == CLASSES


# ============================================================================================
# Clean tree, collect, round trip
# ============================================================================================
def test_clean_tree_is_ok(tmp_path):
    root, proc, base = fresh(tmp_path)
    assert_counts(check(root, proc, base), {})


def test_collect_is_deterministic_and_every_class_has_entries(tmp_path):
    root, proc = make_tree(str(tmp_path))
    first = collect(root, proc)
    assert first == collect(root, proc)
    assert all(first[c] for c in CLASSES), [c for c in CLASSES if not first[c]]


def test_round_trip_collect_build_check_is_ok_with_the_same_ignore(tmp_path):
    ignore = {"tmp": ["tmp/pci-after.txt"], "daolab": ["etc/daolab/otelcol.yaml"]}
    root, proc, base = fresh(tmp_path, ignore)
    assert_counts(check(root, proc, base), {})
    replace_in(root, "etc/daolab/otelcol.yaml", "1", "2")               # ignored noise
    write(root, "tmp/pci-after.txt", "something else, longer than before\n")
    assert_counts(check(root, proc, base), {})


def test_ignore_must_be_the_same_in_collect_and_baseline(tmp_path):
    """The contract build_baseline documents: a baseline whose ignore list was not used by its
    collect run holds fingerprints the checker now skips, so they count as removed."""
    root, proc = make_tree(str(tmp_path))
    base = mini_baseline(collect(root, proc), {"tmp": ["tmp/pci-after.txt"]})
    assert_counts(check(root, proc, base), {"tmp": 1})


# ============================================================================================
# Each class: one addition, one removal -> exact counts
# ============================================================================================
ADD = {
    "identity": lambda r, p: append(r, "etc/passwd", f"{TOKEN}usr:x:{PLANT_UID}:{PLANT_UID}::"
                                                     f"/home/{TOKEN}usr:/bin/sh\n"),
    "sudo": lambda r, p: write(r, f"etc/sudoers.d/{TOKEN}", f"{TOKEN} ALL=(ALL) ALL\n", 0o440),
    "ssh": lambda r, p: write(r, f"etc/ssh/sshd_config.d/{TOKEN}.conf", "PermitRootLogin yes\n"),
    "sched": lambda r, p: write(r, f"etc/cron.d/{TOKEN}", f"* * * * * root /opt/{TOKEN}\n"),
    "boot": lambda r, p: link(r, f"etc/systemd/system/multi-user.target.wants/{TOKEN}.service",
                              f"/etc/systemd/system/{TOKEN}.service"),
    "bin": lambda r, p: write(r, f"usr/bin/{TOKEN}", "#!/bin/sh\n", 0o4755),
    "kmod": lambda r, p: append(p, "modules", f"{TOKEN}mod 16384 0 - Live 0x0\n"),
    "mount": lambda r, p: append(p, "self/mountinfo",
                                 f"40 22 0:50 / /mnt/{TOKEN}\\040dir rw - tmpfs tmpfs rw\n"),
    "proc": lambda r, p: pid(p, 9000099, f"/opt/{TOKEN}exe", PLANT_UID, TOKEN),
    "tmp": lambda r, p: write(r, f"tmp/{TOKEN}", "x" * 10),
    "daolab": lambda r, p: write(r, f"etc/daolab/{TOKEN}", "x\n"),
}
REMOVE = {
    "identity": lambda r, p: drop_lines(r, "etc/group", "lab:x:1001:"),
    "sudo": lambda r, p: os.remove(os.path.join(r, "etc/sudoers.d/90-daolab-lab")),
    "ssh": lambda r, p: os.remove(os.path.join(r, "etc/ssh/ssh_host_ed25519_key.pub")),
    "sched": lambda r, p: os.remove(os.path.join(r, "etc/cron.daily/apt-compat")),
    "boot": lambda r, p: os.remove(os.path.join(r, "etc/systemd/system/nginx-tls.service")),
    "bin": lambda r, p: os.remove(os.path.join(r, "usr/local/bin/daolab-keys")),
    "kmod": lambda r, p: drop_lines(p, "modules", "rvu_cptpf"),
    "mount": lambda r, p: drop_lines(p, "self/mountinfo", " /mnt/huge "),
    "proc": lambda r, p: shutil.rmtree(os.path.join(p, "9000004")),
    "tmp": lambda r, p: os.remove(os.path.join(r, "var/tmp/keep.dat")),
    "daolab": lambda r, p: os.remove(os.path.join(r, "etc/daolab/keys.conf")),
}


@pytest.mark.parametrize("cls", CLASSES)
def test_one_addition_counts_one(tmp_path, cls):
    root, proc, base = fresh(tmp_path)
    ADD[cls](root, proc)
    assert_counts(check(root, proc, base), {cls: 1})


@pytest.mark.parametrize("cls", CLASSES)
def test_one_removal_counts_one(tmp_path, cls):
    root, proc, base = fresh(tmp_path)
    REMOVE[cls](root, proc)
    assert_counts(check(root, proc, base), {cls: 1})


@pytest.mark.parametrize("cls", CLASSES)
def test_an_addition_and_a_removal_count_two(tmp_path, cls):
    root, proc, base = fresh(tmp_path)
    ADD[cls](root, proc)
    REMOVE[cls](root, proc)
    assert_counts(check(root, proc, base), {cls: 2})


def test_every_class_at_once_prints_in_the_fixed_order(tmp_path):
    root, proc, base = fresh(tmp_path)
    for cls in reversed(CLASSES):
        ADD[cls](root, proc)
    assert_counts(check(root, proc, base), {c: 1 for c in CLASSES})


def test_a_changed_file_counts_two_one_removal_and_one_addition(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, "etc/sudoers.d/90-daolab-lab", "lab ALL=(ALL) NOPASSWD: ALL\n", 0o440)
    assert_counts(check(root, proc, base), {"sudo": 2})


# ============================================================================================
# Per-class details
# ============================================================================================
# Collector additions beyond the brief's table, plus the brief's own sub-sources: each planted
# path is a pure addition to the clean tree -> exactly one record in exactly one class.
ADDITIONS = [
    ("identity", f"etc/skel/.{TOKEN}rc"),
    ("identity", "etc/profile"),
    ("identity", f"etc/profile.d/{TOKEN}.sh"),
    ("identity", "etc/bash.bashrc"),
    ("identity", "etc/environment"),
    ("bin", "etc/ld.so.preload"),
    ("bin", "etc/ld.so.conf"),
    ("bin", f"etc/ld.so.conf.d/{TOKEN}.conf"),
    ("bin", f"usr/local/bin/{TOKEN}"),
    ("bin", f"usr/local/sbin/{TOKEN}"),
    ("kmod", f"etc/modprobe.d/{TOKEN}.conf"),
    ("kmod", "etc/modules"),
    ("kmod", f"etc/modules-load.d/{TOKEN}.conf"),
    ("ssh", "root/.ssh/authorized_keys2"),
    ("ssh", "home/dao-test/.ssh/authorized_keys2"),
    ("sched", f"var/spool/cron/crontabs/{TOKEN}usr"),
    ("sched", f"var/spool/cron/atjobs/a0001{TOKEN}"),
    ("sched", f"etc/cron.hourly/{TOKEN}"),
    ("sched", f"etc/systemd/system/{TOKEN}.timer"),             # sched, not boot
    ("boot", "etc/rc.local"),
    ("boot", f"etc/systemd/system/{TOKEN}.service"),
    ("daolab", f"etc/daolab/authorized_keys/{TOKEN}"),
    ("tmp", f"dev/shm/{TOKEN}"),
    ("tmp", f"var/tmp/cloud-init/{TOKEN}"),
]


@pytest.mark.parametrize("cls,rel", ADDITIONS, ids=[f"{c}:{r}" for c, r in ADDITIONS])
def test_planted_addition_counts_one_in_its_class(tmp_path, cls, rel):
    root, proc, base = fresh(tmp_path)
    write(root, rel, f"planted {TOKEN}\n")
    assert_counts(check(root, proc, base), {cls: 1})


def test_a_timer_link_in_timers_target_wants_is_sched_only(tmp_path):
    root, proc, base = fresh(tmp_path)
    link(root, f"etc/systemd/system/timers.target.wants/{TOKEN}.timer", f"/x/{TOKEN}.timer")
    assert_counts(check(root, proc, base), {"sched": 1})


def test_a_new_override_dir_with_a_file_is_two_boot_records(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, f"etc/systemd/system/ssh.service.d/{TOKEN}.conf", "[Service]\nExecStartPre=/x\n")
    assert_counts(check(root, proc, base), {"boot": 2})


def test_identity_shadow_password_change_with_the_same_state_does_not_count(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(root, "etc/shadow", "$6$saltsalt$operatorhash:19000", "$6$newsalt$newhash:19950")
    replace_in(root, "etc/shadow", "!$6$labsalt$labhash", "!$6$rotated$every-booking")
    assert_counts(check(root, proc, base), {})


def test_identity_shadow_locked_to_set_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(root, "etc/shadow", "lab:!$6$labsalt$labhash", "lab:$6$labsalt$labhash")
    assert_counts(check(root, proc, base), {"identity": 2})


def test_identity_shadow_locked_to_empty_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(root, "etc/shadow", "root:*:", "root::")
    assert_counts(check(root, proc, base), {"identity": 2})


def test_identity_password_state_classes():
    state = mod().password_state
    assert [state(v) for v in ("!", "!$6$x", "*", "*LK*", "", "$6$x$y", "x")] == \
        ["locked", "locked", "locked", "locked", "empty", "set", "set"]


def test_identity_shadow_records_never_carry_the_password_field(tmp_path):
    root, proc = make_tree(str(tmp_path))
    flat = repr(sorted(records("identity", root, proc)))
    assert "operatorhash" not in flat and "labhash" not in flat and "saltsalt" not in flat


def test_identity_file_mode_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    os.chmod(os.path.join(root, "etc/shadow"), 0o644)
    assert_counts(check(root, proc, base), {"identity": 2})


def test_ssh_lab_authorized_keys_change_does_not_count(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, "home/lab/.ssh/authorized_keys", "ssh-ed25519 AAAAother booking-2\n", 0o600)
    write(root, "home/lab/.ssh/authorized_keys2", "ssh-ed25519 AAAAmore\n")
    assert_counts(check(root, proc, base), {})


def test_ssh_another_users_authorized_keys_change_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    append(root, "home/dao-test/.ssh/authorized_keys", f"ssh-ed25519 AAAA{TOKEN} extra\n")
    assert_counts(check(root, proc, base), {"ssh": 2})


def test_ssh_lab_home_moved_is_caught_by_identity(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(root, "etc/passwd", "lab:x:1001:1001::/home/lab:", "lab:x:1001:1001::/root:")
    # the lab line changed (identity 2); /root is now lab's home, so root's keys are skipped (1)
    assert_counts(check(root, proc, base), {"identity": 2, "ssh": 1})


def test_ssh_home_with_dotdot_stays_under_root(tmp_path):
    root, proc = make_tree(str(tmp_path))
    append(root, "etc/passwd", f"{TOKEN}:x:5:5::/../../../root:/bin/sh\n")
    homes = mod().homes(mod().Ctx(root, proc, ()))
    assert "root" in homes and all(not h.startswith("..") and not h.startswith("/") for h in homes)
    assert "home/lab" not in homes


def test_sched_and_boot_records_carry_no_uid_or_mode(tmp_path):
    root, proc = make_tree(str(tmp_path))
    for cls in ("sched", "boot"):
        for rec in records(cls, root, proc):
            assert not any(isinstance(f, int) for f in rec), rec


def test_bin_plain_files_outside_local_bin_do_not_count(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, f"usr/bin/{TOKEN}-plain", "#!/bin/sh\n", 0o755)
    write(root, f"opt/{TOKEN}/tool", "#!/bin/sh\n", 0o755)
    write(root, f"usr/local/lib/{TOKEN}.so", "elf\n", 0o755)
    assert_counts(check(root, proc, base), {})


def test_bin_setgid_under_opt_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, f"opt/{TOKEN}/tool", "#!/bin/sh\n", 0o2755)
    assert_counts(check(root, proc, base), {"bin": 1})


def test_bin_setuid_in_usr_local_bin_is_one_record(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, f"usr/local/bin/{TOKEN}", "#!/bin/sh\n", 0o4755)
    assert_counts(check(root, proc, base), {"bin": 1})


def test_bin_setuid_bit_removed_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    os.chmod(os.path.join(root, "usr/bin/sudo"), 0o755)
    assert_counts(check(root, proc, base), {"bin": 1})


def test_bin_local_binary_content_change_counts_two(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, "usr/local/bin/daolab-keys", "#!/bin/sh\necho evil\n", 0o755)
    assert_counts(check(root, proc, base), {"bin": 2})


def test_bin_merged_usr_links_are_recorded_not_walked(tmp_path):
    root, proc, base = fresh(tmp_path)
    os.remove(os.path.join(root, "bin"))
    mkdir(root, "bin")
    write(root, f"bin/{TOKEN}", "#!/bin/sh\n", 0o4755)
    assert_counts(check(root, proc, base), {"bin": 2})       # the link went, a setuid file came


def test_kmod_only_the_module_name_counts(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(proc, "modules", "vfio_pci 16384 0 -", "vfio_pci 16384 3 -")
    assert_counts(check(root, proc, base), {})


def test_mount_octal_escapes_are_decoded():
    decode = mod().decode_mount_field
    assert decode("/mnt/with\\040space") == "/mnt/with space"
    assert decode("/a\\011b\\012c\\134d") == "/a\tb\nc\\d"
    assert decode("/plain") == "/plain"


def test_mount_records_point_and_fstype(tmp_path):
    root, proc = make_tree(str(tmp_path))
    recs = records("mount", root, proc)
    assert ("mnt", "/mnt/with space", "tmpfs") in recs and ("mnt", "/", "ext4") in recs


def test_mount_options_do_not_count_but_fstype_does(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(proc, "self/mountinfo", "/mnt/huge rw,relatime", "/mnt/huge ro,relatime")
    assert_counts(check(root, proc, base), {})
    replace_in(proc, "self/mountinfo", "- hugetlbfs nodev", "- tmpfs nodev")
    assert_counts(check(root, proc, base), {"mount": 2})


def test_mount_malformed_line_counts_and_does_not_crash(tmp_path):
    root, proc, base = fresh(tmp_path)
    append(proc, "self/mountinfo", f"garbage {TOKEN}\n")
    assert_counts(check(root, proc, base), {"mount": 1})


def test_proc_duplicates_collapse_and_kernel_threads_are_skipped(tmp_path):
    root, proc, base = fresh(tmp_path)
    pid(proc, 9000010, "/usr/sbin/nginx", 0)             # same exe + uid as 9000006
    pid(proc, 9000011, None, 0)                          # another kernel thread
    d = pid(proc, 9000012, None, 0)                      # an exe that is not a link
    write(d, "exe", "not a link")
    assert_counts(check(root, proc, base), {})


def test_proc_uid_change_counts_two(tmp_path):
    root, proc, base = fresh(tmp_path)
    replace_in(proc, "9000004/status", "Uid:\t1000\t1000", "Uid:\t0\t1000")
    assert_counts(check(root, proc, base), {"proc": 2})


def test_proc_records_have_no_pid_and_skip_own_pid(tmp_path):
    root, proc = make_tree(str(tmp_path))
    pid(proc, os.getpid(), f"/opt/{TOKEN}self", PLANT_UID)
    recs = records("proc", root, proc)
    assert ("p", "/usr/sbin/nginx", 0) in recs and ("p", "/usr/sbin/sshd", 1000) in recs
    assert all(TOKEN not in r[1] for r in recs)
    assert all("9000" not in repr(r) for r in recs)


def test_tmp_size_counts_content_does_not(tmp_path):
    root, proc, base = fresh(tmp_path)
    write(root, "var/tmp/keep.dat", "KEEP\n")                     # same size
    assert_counts(check(root, proc, base), {})
    write(root, "var/tmp/keep.dat", "keep, longer\n")
    assert_counts(check(root, proc, base), {"tmp": 2})


def test_tmp_directories_alone_do_not_count_specials_do(tmp_path):
    root, proc, base = fresh(tmp_path)
    mkdir(root, f"tmp/{TOKEN}-dir/deeper")
    assert_counts(check(root, proc, base), {})
    os.mkfifo(os.path.join(root, f"tmp/{TOKEN}-fifo"))
    link(root, f"dev/shm/{TOKEN}-link", "/etc/shadow")
    assert_counts(check(root, proc, base), {"tmp": 2})


def test_daolab_mode_change_counts_two(tmp_path):
    root, proc, base = fresh(tmp_path)
    os.chmod(os.path.join(root, "etc/daolab/hw_class"), 0o666)
    assert_counts(check(root, proc, base), {"daolab": 2})


# ============================================================================================
# Noise rules (ignore globs)
# ============================================================================================
def test_ignore_glob_suppresses_a_planted_noise_file(tmp_path):
    ignore = {"tmp": ["tmp/noise-*"]}
    root, proc, base = fresh(tmp_path, ignore)
    write(root, "tmp/noise-1", "x")
    write(root, "tmp/noise-2/inside", "y")
    assert_counts(check(root, proc, base), {})
    write(root, f"tmp/{TOKEN}", "z")
    assert_counts(check(root, proc, base), {"tmp": 1})


def test_ignore_only_applies_to_its_own_class(tmp_path):
    ignore = {"tmp": ["etc/daolab/*"]}
    root, proc, base = fresh(tmp_path, ignore)
    write(root, f"etc/daolab/{TOKEN}", "x")
    assert_counts(check(root, proc, base), {"daolab": 1})


def test_ignored_directory_skips_its_subtree(tmp_path):
    ignore = {"tmp": ["var/tmp/systemd-private-*"]}
    root, proc, base = fresh(tmp_path, ignore)
    write(root, f"var/tmp/systemd-private-abc/tmp/{TOKEN}", "x")
    assert_counts(check(root, proc, base), {})


@pytest.mark.parametrize("cls,glob,plant", [
    ("identity", f"etc/passwd:{TOKEN}*", ADD["identity"]),
    ("kmod", f"{TOKEN}*", ADD["kmod"]),
    ("mount", f"mnt/{TOKEN}*", ADD["mount"]),
    ("proc", f"opt/{TOKEN}*", ADD["proc"]),
    ("boot", f"etc/systemd/system/multi-user.target.wants/{TOKEN}*", ADD["boot"]),
])
def test_ignore_subjects_of_the_non_file_records(tmp_path, cls, glob, plant):
    root, proc, base = fresh(tmp_path, {cls: [glob]})
    plant(root, proc)
    assert_counts(check(root, proc, base), {})


def test_the_four_playbook_noise_sources_with_their_globs(tmp_path):
    """FINDINGS §7 / the synthetic baseline's globs (synthetic only; production = Rama)."""
    ignore = {"boot": ["etc/systemd/system/daolab-app-exporter.service"],
              "tmp": ["tmp/dao-smart-nic-ansible.log", "tmp/pci-after.txt"],
              "daolab": ["etc/daolab/otelcol.yaml"]}
    root, proc, base = fresh(tmp_path, ignore)
    write(root, "etc/systemd/system/daolab-app-exporter.service", "ExecStart=x --app vpp\n")
    write(root, "tmp/dao-smart-nic-ansible.log", "launch log\n")
    write(root, "tmp/pci-after.txt", "changed, longer\n")
    write(root, "etc/daolab/otelcol.yaml", "daolab.reservation: 2\ndaolab.user: someone\n")
    assert_counts(check(root, proc, base), {})


# ============================================================================================
# Robustness: symlinks, unreadable entries, odd names
# ============================================================================================
def test_symlink_loops_do_not_hang_and_are_link_records(tmp_path):
    root, proc, base = fresh(tmp_path)
    link(root, f"etc/ssh/{TOKEN}a", f"{TOKEN}b")
    link(root, f"etc/ssh/{TOKEN}b", f"{TOKEN}a")
    link(root, f"etc/ssh/{TOKEN}up", "..")
    link(root, f"etc/daolab/{TOKEN}self", ".")
    link(root, f"tmp/{TOKEN}root", "/")
    assert_counts(check(root, proc, base), {"ssh": 3, "tmp": 1, "daolab": 1})
    base2 = mini_baseline(collect(root, proc))
    assert_counts(check(root, proc, base2), {})


def test_symlinks_are_never_followed(tmp_path):
    root, proc = make_tree(str(tmp_path))
    outside = write(str(tmp_path), "outside/secret", "one\n")
    link(root, "etc/daolab/points-outside", outside)
    base = mini_baseline(collect(root, proc))
    write(str(tmp_path), "outside/secret", "two, and longer\n")
    assert_counts(check(root, proc, base), {})


@pytest.mark.skipif(AS_ROOT, reason="root reads mode-000 files")
def test_unreadable_file_is_counted_not_hidden(tmp_path):
    root, proc, base = fresh(tmp_path)
    target = os.path.join(root, "etc/sudoers.d/90-daolab-lab")
    os.chmod(target, 0)
    try:
        assert_counts(check(root, proc, base), {"sudo": 2})     # file record -> unreadable
    finally:
        os.chmod(target, 0o440)


@pytest.mark.skipif(AS_ROOT, reason="root reads mode-000 dirs")
def test_unreadable_directory_is_counted_not_hidden(tmp_path):
    root, proc, base = fresh(tmp_path)
    d = mkdir(root, f"etc/daolab/{TOKEN}dir")
    write(d, "inside", "x")
    os.chmod(d, 0)
    try:
        assert_counts(check(root, proc, base), {"daolab": 2})   # the dir + "unreadable"
    finally:
        os.chmod(d, 0o755)


def test_a_fifo_where_a_file_was_is_never_opened(tmp_path):
    root, proc, base = fresh(tmp_path)
    os.remove(os.path.join(root, "etc/daolab/keys.conf"))
    os.mkfifo(os.path.join(root, "etc/daolab/keys.conf"))
    assert_counts(check(root, proc, base), {"daolab": 2})       # no hang (TIMEOUT)


def test_non_utf8_and_newline_names_do_not_crash(tmp_path):
    root, proc, base = fresh(tmp_path)
    with open(os.path.join(os.fsencode(root), b"etc/daolab/bad-\xff-name"), "w") as fh:
        fh.write("x")
    write(root, "etc/daolab/new\nline", "x")
    assert_counts(check(root, proc, base), {"daolab": 2})


def test_fingerprint_is_newline_free_sha256_of_a_fixed_field_order():
    fp = mod().fingerprint
    a = fp("tmp", ("f", "tmp/a\nb", 0, 1))
    assert HEX_RE.fullmatch(a)
    assert a == hashlib.sha256(json.dumps(["tmp", "f", "tmp/a\nb", 0, 1], ensure_ascii=True,
                                          separators=(",", ":")).encode("ascii")).hexdigest()
    assert a != fp("tmp", ("f", "tmp/a", "b", 0, 1))            # no field-boundary ambiguity
    assert a != fp("daolab", ("f", "tmp/a\nb", 0, 1))           # the class is in the record


def test_missing_class_sources_are_simply_empty(tmp_path):
    root, proc = os.path.join(str(tmp_path), "r"), os.path.join(str(tmp_path), "p")
    os.makedirs(root)
    os.makedirs(proc)
    assert collect(root, proc) == {c: [] for c in CLASSES}


def test_the_checker_writes_nothing(tmp_path):
    root, proc, base = fresh(tmp_path)

    def snapshot():
        out = []
        for top in (str(tmp_path), os.path.dirname(SCRIPT)):
            for d, dirs, files in os.walk(top):
                for n in dirs + files:
                    p = os.path.join(d, n)
                    st = os.lstat(p)
                    out.append((p, st.st_mode, st.st_size, st.st_mtime_ns))
        return sorted(out)

    before = snapshot()
    assert_counts(check(root, proc, base), {})
    collect(root, proc)
    assert snapshot() == before


# ============================================================================================
# Input errors: the exact error line, exit 2, nothing on stderr
# ============================================================================================
def _with(base, path, value):
    doc = json.loads(json.dumps(base))
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


BAD_BASELINES = [                                        # (id, mutate(base) -> doc, code)
    ("not-an-object", lambda b: [b], "bad_input"),
    ("no-format", lambda b: _with(b, ["format"], KeyError), "bad_input"),
    ("wrong-format", lambda b: _with(b, ["format"], "daolab-tripwire/2"), "bad_input"),
    ("collect-format", lambda b: _with(b, ["format"], COLLECT_FORMAT), "bad_input"),
    ("unknown-top-key", lambda b: _with(b, [f"{TOKEN}key"], 1), "bad_baseline"),
    ("missing-version", lambda b: _with(b, ["version"], KeyError), "bad_baseline"),
    ("version-0", lambda b: _with(b, ["version"], 0), "bad_baseline"),
    ("version-str", lambda b: _with(b, ["version"], "1"), "bad_baseline"),
    ("version-bool", lambda b: _with(b, ["version"], True), "bad_baseline"),
    ("version-float", lambda b: _with(b, ["version"], 1.0), "bad_baseline"),
    ("hw-class-shape", lambda b: _with(b, ["hw_class"], f"Bad_{TOKEN}"), "bad_baseline"),
    ("hw-class-long", lambda b: _with(b, ["hw_class"], "a" * 129), "bad_baseline"),
    ("classes-not-object", lambda b: _with(b, ["classes"], []), "bad_baseline"),
    ("unknown-class", lambda b: _with(b, ["classes", f"{TOKEN}cls"],
                                      {"fingerprints": [], "ignore": []}), "bad_baseline"),
    ("missing-class", lambda b: _with(b, ["classes", "kmod"], KeyError), "bad_baseline"),
    ("class-not-object", lambda b: _with(b, ["classes", "kmod"], []), "bad_baseline"),
    ("unknown-class-key", lambda b: _with(b, ["classes", "kmod", f"{TOKEN}k"], []),
     "bad_baseline"),
    ("missing-ignore", lambda b: _with(b, ["classes", "kmod", "ignore"], KeyError),
     "bad_baseline"),
    ("fingerprints-not-list", lambda b: _with(b, ["classes", "kmod", "fingerprints"], "x"),
     "bad_baseline"),
    ("hex-short", lambda b: _with(b, ["classes", "kmod", "fingerprints"], ["a" * 63]),
     "bad_baseline"),
    ("hex-upper", lambda b: _with(b, ["classes", "kmod", "fingerprints"], ["A" * 64]),
     "bad_baseline"),
    ("hex-not-hex", lambda b: _with(b, ["classes", "kmod", "fingerprints"], ["g" * 64]),
     "bad_baseline"),
    ("hex-not-str", lambda b: _with(b, ["classes", "kmod", "fingerprints"], [1]), "bad_baseline"),
    ("hex-duplicate", lambda b: _with(b, ["classes", "kmod", "fingerprints"], ["a" * 64] * 2),
     "bad_baseline"),
    ("glob-absolute", lambda b: _with(b, ["classes", "tmp", "ignore"], [f"/tmp/{TOKEN}"]),
     "bad_baseline"),
    ("glob-empty", lambda b: _with(b, ["classes", "tmp", "ignore"], [""]), "bad_baseline"),
    ("glob-newline", lambda b: _with(b, ["classes", "tmp", "ignore"], ["a\nb"]), "bad_baseline"),
    ("glob-nul", lambda b: _with(b, ["classes", "tmp", "ignore"], ["a\x00b"]), "bad_baseline"),
    ("glob-long", lambda b: _with(b, ["classes", "tmp", "ignore"], ["a" * 257]), "bad_baseline"),
    ("glob-not-str", lambda b: _with(b, ["classes", "tmp", "ignore"], [3]), "bad_baseline"),
    ("glob-duplicate", lambda b: _with(b, ["classes", "tmp", "ignore"], ["a", "a"]),
     "bad_baseline"),
    ("glob-too-many", lambda b: _with(b, ["classes", "tmp", "ignore"],
                                      [f"g{i}" for i in range(257)]), "bad_baseline"),
]


@pytest.mark.parametrize("case", BAD_BASELINES, ids=[c[0] for c in BAD_BASELINES])
def test_bad_baseline_is_refused_with_the_exact_error_line(tmp_path, case):
    _, mutate, code = case
    root, proc, base = fresh(tmp_path)
    assert_error(check(root, proc, mutate(base)), code)


BAD_BYTES = [
    ("empty", b""),
    ("not-json", b"{"),
    ("json-garbage", f"{TOKEN} not json".encode()),
    ("not-utf8", b'{"format": "\xff"}'),
    ("utf8-bom", b'\xef\xbb\xbf{"format": "daolab-tripwire/1"}'),
    ("utf16", '{"format": "daolab-tripwire/1"}'.encode("utf-16")),
    ("duplicate-key", b'{"format": "daolab-tripwire/1", "format": "daolab-tripwire/1"}'),
    ("nan", b'{"format": "daolab-tripwire/1", "version": NaN}'),
    ("deep-nesting", b"[" * 100000 + b"]" * 100000),
    ("huge-int", b'{"format": "daolab-tripwire/1", "version": ' + b"9" * 5000 + b"}"),
]


@pytest.mark.parametrize("case", BAD_BYTES, ids=[c[0] for c in BAD_BYTES])
def test_unparseable_input_is_bad_input(tmp_path, case):
    root, proc = make_tree(str(tmp_path))
    for mode in ("check", "collect"):
        assert_error(run(root, proc, mode, case[1]), "bad_input")


def test_input_over_16_mib_is_bad_input(tmp_path):
    root, proc = make_tree(str(tmp_path))
    assert_error(run(root, proc, "check", b" " * (16 * 1024 * 1024 + 1)), "bad_input")


BAD_COLLECT = [
    ("wrong-format", {"format": FORMAT, "ignore": {}}),
    ("no-ignore", {"format": COLLECT_FORMAT}),
    ("unknown-key", {"format": COLLECT_FORMAT, "ignore": {}, TOKEN: 1}),
    ("ignore-not-object", {"format": COLLECT_FORMAT, "ignore": []}),
    ("unknown-class", {"format": COLLECT_FORMAT, "ignore": {TOKEN: []}}),
    ("bad-glob", {"format": COLLECT_FORMAT, "ignore": {"tmp": ["/abs"]}}),
    ("globs-not-list", {"format": COLLECT_FORMAT, "ignore": {"tmp": "tmp/*"}}),
]


@pytest.mark.parametrize("case", BAD_COLLECT, ids=[c[0] for c in BAD_COLLECT])
def test_bad_collect_document_is_bad_input(tmp_path, case):
    root, proc = make_tree(str(tmp_path))
    assert_error(run(root, proc, "collect", case[1]), "bad_input")


def test_bad_options_are_bad_input_without_usage_text(tmp_path):
    root, proc = make_tree(str(tmp_path))
    fake = ["--root", root, "--proc", proc]
    doc = collect_doc()
    for args in ([*fake],                                       # no --mode
                 ["--mode", "nope", *fake],
                 ["--mode", "check", "--mode", "collect", *fake],
                 ["--mode", "collect", f"--{TOKEN}", *fake],
                 ["--mode", "collect", *fake, "positional"],
                 ["--mode", "collect", "--root=" + root, "--proc", proc],
                 ["--mode", "collect", "--proc", proc, "--root"],
                 ["--mode", "collect", "--root", os.path.join(root, TOKEN), "--proc", proc],
                 ["--mode", "collect", "--root", root, "--proc", os.path.join(proc, TOKEN)],
                 ["--mode", "collect", "--root", write(root, f"{TOKEN}file"), "--proc", proc],
                 ["--mode", "collect", "--root", "", "--proc", proc],
                 ["--mode", "collect", *fake, "--input-fd", "x"],
                 ["--mode", "collect", *fake, "--input-fd", "-1"],
                 ["--mode", "collect", *fake, "--input-fd", "987"],    # not open
                 ["--help"]):
        r = run_raw(args, doc)
        assert_error(r, "bad_input")


def test_internal_error_prints_only_the_code(tmp_path):
    """An unexpected exception: tripwire:error:internal, exit 2, no traceback anywhere."""
    root, proc = make_tree(str(tmp_path))
    boot = ("import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('t', sys.argv.pop(1))\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "def boom(ctx): raise RuntimeError('%s at /etc/%s uid %d')\n"
            "m.COLLECTORS['sudo'] = boom\n"
            "sys.exit(m.main())\n") % (TOKEN, TOKEN, PLANT_UID)
    r = subprocess.run([sys.executable, "-I", "-B", "-c", boot, _script(), "--mode", "collect",
                        "--root", root, "--proc", proc], input=_bytes(collect_doc()),
                       capture_output=True, timeout=TIMEOUT)
    assert_error(r, "internal")


def test_internal_error_in_process_returns_2(tmp_path, monkeypatch, capsys):
    root, proc = make_tree(str(tmp_path))
    m = mod()

    def boom(ctx):
        raise MemoryError(TOKEN)

    monkeypatch.setitem(m.COLLECTORS, "proc", boom)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(_bytes(collect_doc()))))
    assert m.main(["--mode", "collect", "--root", root, "--proc", proc]) == 2
    out = capsys.readouterr()
    assert (out.out, out.err) == ("tripwire:error:internal\n", "")


# ============================================================================================
# The baseline from a file descriptor (for when the script itself comes on stdin)
# ============================================================================================
def _fd_with(data):
    r, w = os.pipe()

    def feed():
        with os.fdopen(w, "wb") as fh:
            fh.write(data)

    t = threading.Thread(target=feed)
    t.start()
    return r, t


def test_input_fd_reads_the_baseline_and_ignores_stdin(tmp_path):
    root, proc, base = fresh(tmp_path)
    ADD["sudo"](root, proc)
    fd, t = _fd_with(_bytes(base))
    try:
        r = run_raw(["--mode", "check", "--root", root, "--proc", proc, "--input-fd", str(fd)],
                    b"this is not the baseline", pass_fds=(fd,))
    finally:
        os.close(fd)
        t.join()
    assert_counts(r, {"sudo": 1})


def test_script_on_stdin_with_the_baseline_on_a_descriptor(tmp_path):
    """The B-112b shape: `python3 -I -B -c <bootstrap>` reads a length-prefixed script from
    stdin; the baseline comes on another descriptor. The checker never needs its own file."""
    root, proc, base = fresh(tmp_path)
    ADD["tmp"](root, proc)
    with open(_script(), "rb") as fh:
        src = fh.read()
    boot = ("import sys\n"
            "n = int(sys.stdin.buffer.readline())\n"
            "code = compile(sys.stdin.buffer.read(n), 'daolab-tripwire', 'exec')\n"
            "exec(code, {'__name__': '__main__'})\n")
    fd, t = _fd_with(_bytes(base))
    try:
        r = subprocess.run([sys.executable, "-I", "-B", "-c", boot, "--mode", "check",
                            "--root", root, "--proc", proc, "--input-fd", str(fd)],
                           input=str(len(src)).encode() + b"\n" + src, capture_output=True,
                           timeout=TIMEOUT, pass_fds=(fd,))
    finally:
        os.close(fd)
        t.join()
    assert_counts(r, {"tmp": 1})


# ============================================================================================
# Safe output: no planted name, path, uid or module name ever reaches stdout or stderr
# ============================================================================================
def _forbidden(tmp_path):
    # ("lab" itself can't be listed: it is part of the class name "daolab")
    return [TOKEN, str(PLANT_UID), str(tmp_path), "/", "vfio", "rvu_", "sshd", "nginx", "home",
            "etc", "usr", "dao-test", "exporter", ".service", "authorized", "shadow", "1000"]


def test_output_never_contains_a_planted_name_path_uid_or_module(tmp_path):
    root, proc, base = fresh(tmp_path)
    for cls in CLASSES:
        ADD[cls](root, proc)
    for _, rel in ADDITIONS:
        write(root, rel, f"planted {TOKEN} {PLANT_UID}\n")
    link(root, f"etc/ssh/{TOKEN}loop", f"{TOKEN}loop")
    os.mkfifo(os.path.join(root, f"tmp/{TOKEN}fifo"))
    runs = [check(root, proc, base), run(root, proc, "collect", collect_doc({"tmp": [TOKEN]}))]
    runs += [check(root, proc, mutate(base)) for _, mutate, _ in BAD_BASELINES]
    runs += [run(root, proc, "check", data) for _, data in BAD_BYTES]
    runs += [run_raw(["--mode", "check", "--root", os.path.join(root, TOKEN), "--proc", proc],
                     base)]
    for r in runs:
        # fingerprints are checked as hex below; a digit run inside one is not a leak
        text = HEX_RE.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))
        for bad in _forbidden(tmp_path):
            assert bad not in text, (bad, text[:200])
        assert r.stderr == b""
    for line in runs[0].stdout.decode("ascii").splitlines():
        assert LINE_RE.fullmatch(line), line
    assert runs[0].stdout.decode().splitlines() == [f"tripwire:{c}:" + str(n) for c, n in
                                                    zip(CLASSES, _count_planted())]
    collected = json.loads(runs[1].stdout)
    for c in CLASSES:
        assert all(HEX_RE.fullmatch(f) for f in collected["classes"][c]["fingerprints"])


def _count_planted():
    """Per class: one ADD plant + its ADDITIONS (+ the ssh loop link, + the tmp FIFO)."""
    extra = {"ssh": 1, "tmp": 1}
    return [1 + sum(1 for c, _ in ADDITIONS if c == cls) + extra.get(cls, 0) for cls in CLASSES]


# ============================================================================================
# Timing (informational, not asserted)
# ============================================================================================
def test_timing_on_about_5000_files(tmp_path):
    root, proc = make_tree(str(tmp_path))
    for i in range(2000):
        write(root, f"usr/bin/tool{i:04d}", "#!/bin/sh\n", 0o755)
    for i in range(1000):
        write(root, f"usr/local/bin/bin{i:04d}", "x" * 4096, 0o755)
    for i in range(1000):
        write(root, f"tmp/t{i // 100}/f{i:04d}", "x")
    for i in range(1000):
        write(root, f"etc/daolab/d{i // 100}/f{i:04d}", "y" * 1024)
    t0 = time.monotonic()
    base = mini_baseline(collect(root, proc))
    t1 = time.monotonic()
    assert_counts(check(root, proc, base), {})
    t2 = time.monotonic()
    print(f"\nTIMING tripwire ~5000 files: collect {t1 - t0:.2f}s, check {t2 - t1:.2f}s "
          f"(python {sys.version.split()[0]})")
