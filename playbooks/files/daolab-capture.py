#!/usr/bin/python3
"""daolab-capture.py - seal /home/lab into one age file on the DPU (B-109a).

Runs as root on the DPU at the end of a slot (design v1.1 section 6 "End of slot - save",
step 3; errata E-6), with Python 3.10 standard library only (E-3):

    python3 -I daolab-capture.py --home /home/lab --skel /etc/skel --owner-uid <lab uid>
        --recipient <age1...> --out /var/lib/daolab/capture/<public_id>.age
        --cap-bytes 536870912 --age /usr/local/bin/age
        [--mountinfo /proc/self/mountinfo] [--sysctl-root /proc/sys]

It prints exactly one JSON line on stdout and nothing on stderr:

    {"status": "saved"|"empty"|"refused", "reason": <code>, "raw_bytes": n,
     "age_bytes": n, "age_sha256": "<hex>"}

and exits 0 for saved/empty, 1 for refused. The reason codes are a closed list (REASONS).
Nothing it prints names a file or a path (safe diagnostics only). A refused capture leaves
the home untouched and removes any partial --out; the caller keeps the home, raises
home:lab and quarantines.

Order of checks:
  1. arguments (bad_args): every option exactly once; --recipient is the strict age1 shape
     (public, but text from the controller); numbers are plain digits.
  2. paths (path_unsafe): --home, --skel and --out absolute, normalized, no control chars,
     not /, /root, /etc, /home, /usr, /var or any other top-level dir; home != skel and
     neither inside the other; --out not inside home or skel. --out's parent is a real
     directory owned by the effective uid (root on the board) with mode 0700.
     --out must not exist yet (out_exists).
  3. sysctls (sysctl_unsafe): fs.protected_hardlinks and fs.protected_symlinks read "1" (E-7).
  4. mounts (mount_under_home): no mount point at or under home, nor under its resolved
     path. tar --one-file-system can't see a same-filesystem bind mount (F-5-2).
     Unreadable or malformed mountinfo also refuses.
  5. home (home_missing, home_not_dir, home_owner): home is a real directory (not a symlink)
     owned by --owner-uid. Files inside it with other owners are user data (E-6).
  6. empty (status empty, reason empty_home): the whole tree equals --skel (the same names
     at every depth, same types, regular files byte-identical, symlink targets identical).
     Anything else, or a skel that can't be read, is captured.
  7. capture: tar --one-file-system --numeric-owner --acls --xattrs -C home -cf - .
     -> raw bytes counted against --cap-bytes (cap_exceeded) -> gzip level 1 in-process
     -> age -r <recipient> -o <out>. Every exit status is checked: tar != 0 (rc 1 "file
     changed as we read it" included) is tar_failed, age != 0 or a broken pipe age_failed.
     --out is created first with O_EXCL and mode 0600. No plaintext touches the disk.
     SIGTERM/SIGINT/SIGHUP kill tar and age, remove --out and report tar_failed.
  8. --out is fsynced, then its size and sha256 are reported.

Children run with a fixed environment (no TAR_OPTIONS from the caller). Run it with
`python3 -I` from a root-owned directory, so nothing beside the script can shadow a module.
"""
import argparse
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import zlib

TAR = "/usr/bin/tar"
CHILD_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
CHUNK = 1 << 16
MOUNTINFO_MAX = 16 << 20

REASONS = ("ok", "empty_home", "path_unsafe", "home_missing", "home_not_dir", "home_owner",
           "mount_under_home", "sysctl_unsafe", "tar_failed", "cap_exceeded", "age_failed",
           "out_exists", "bad_args")
RECIPIENT_RE = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")
DIGITS_RE = re.compile(r"[0-9]{1,20}")
FORBIDDEN = frozenset(("/", "/root", "/etc", "/home", "/usr", "/var"))
_ESCAPE_RE = re.compile(rb"\\([0-7]{3})")
_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Interrupted(Exception):
    pass


# ---- 1. arguments -----------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse that never prints: every parse problem is bad_args."""

    def error(self, message):
        raise Refused("bad_args")

    def exit(self, status=0, message=None):
        raise Refused("bad_args")

    def print_usage(self, file=None):
        pass

    def print_help(self, file=None):
        pass


class _Once(argparse.Action):
    """Store a value; a repeated option is bad_args, not 'last one wins'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = False

    def __call__(self, parser, namespace, values, option_string=None):
        if self.seen:
            raise Refused("bad_args")
        self.seen = True
        setattr(namespace, self.dest, values)


def _parse(argv):
    p = _Parser(prog="daolab-capture", add_help=False, allow_abbrev=False)
    for name in ("--home", "--skel", "--recipient", "--out", "--cap-bytes", "--age",
                 "--owner-uid"):
        p.add_argument(name, required=True, action=_Once)
    p.add_argument("--mountinfo", default="/proc/self/mountinfo", action=_Once)
    p.add_argument("--sysctl-root", default="/proc/sys", action=_Once)
    a = p.parse_args(argv)
    if not RECIPIENT_RE.fullmatch(a.recipient):
        raise Refused("bad_args")
    for name in ("cap_bytes", "owner_uid"):
        if not DIGITS_RE.fullmatch(getattr(a, name)):
            raise Refused("bad_args")
    a.cap_bytes = int(a.cap_bytes)
    a.owner_uid = int(a.owner_uid)
    if a.cap_bytes < 1 or a.owner_uid > 0xFFFFFFFE:
        raise Refused("bad_args")
    for path in (a.age, a.mountinfo, a.sysctl_root):
        if not _clean_abs(path):
            raise Refused("bad_args")
    return a


# ---- 2. paths ---------------------------------------------------------------------------------

def _clean_abs(path):
    """Absolute, normalized, no control characters, no '.' or '..' components."""
    if not path.startswith("/") or path.startswith("//"):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    if os.path.normpath(path) != path:
        return False
    return not any(part in (".", "..") for part in path.split("/"))


def _sane(path):
    """_clean_abs, and not a system root: not in FORBIDDEN and at least two levels deep."""
    if not _clean_abs(path) or path in FORBIDDEN:
        return False
    return len([part for part in path.split("/") if part]) >= 2


def _within(path, top):
    return path == top or path.startswith(top + "/")


def _check_paths(a):
    if not (_sane(a.home) and _sane(a.skel) and _sane(a.out)):
        raise Refused("path_unsafe")
    if _within(a.home, a.skel) or _within(a.skel, a.home):
        raise Refused("path_unsafe")
    if _within(a.out, a.home) or _within(a.out, a.skel):
        raise Refused("path_unsafe")
    parent = os.path.dirname(a.out)
    try:
        st = os.lstat(parent)
    except OSError:
        raise Refused("path_unsafe")
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise Refused("path_unsafe")
    real_parent = os.path.realpath(parent)
    for top in (a.home, os.path.realpath(a.home), a.skel, os.path.realpath(a.skel)):
        if _within(real_parent, top):
            raise Refused("path_unsafe")
    try:
        os.lstat(a.out)
    except FileNotFoundError:
        return
    except OSError:
        raise Refused("path_unsafe")
    raise Refused("out_exists")


# ---- 3. sysctls -------------------------------------------------------------------------------

def _check_sysctls(root):
    for name in ("protected_hardlinks", "protected_symlinks"):
        try:
            with open(os.path.join(root, "fs", name), "rb") as fh:
                value = fh.read(16)
        except OSError:
            raise Refused("sysctl_unsafe")
        if value.rstrip(b"\n") != b"1":
            raise Refused("sysctl_unsafe")


# ---- 4. mounts --------------------------------------------------------------------------------

def _unescape(field):
    def one(m):
        n = int(m.group(1), 8)
        if n > 0xFF:
            raise ValueError("bad escape")
        return bytes((n,))
    return _ESCAPE_RE.sub(one, field)


def _mount_points(path):
    """The mount point (field 5) of every /proc/<pid>/mountinfo line, decoded."""
    with open(path, "rb") as fh:
        data = fh.read(MOUNTINFO_MAX + 1)
    if len(data) > MOUNTINFO_MAX or not data.strip():
        raise ValueError("mountinfo size")
    points = []
    for line in data.split(b"\n"):
        if not line:
            continue
        fields = line.split(b" ")
        # id parent major:minor root mount-point options [optional...] - fstype source super
        if len(fields) < 10 or b"-" not in fields[6:-2]:
            raise ValueError("mountinfo line")
        points.append(os.fsdecode(_unescape(fields[4])))
    return points


def _check_mounts(a):
    try:
        points = _mount_points(a.mountinfo)
    except (OSError, ValueError):
        raise Refused("mount_under_home")
    tops = {a.home, os.path.realpath(a.home)}
    for point in points:
        if any(_within(point, top) for top in tops):
            raise Refused("mount_under_home")


# ---- 5. home ----------------------------------------------------------------------------------

def _check_home(a):
    try:
        st = os.lstat(a.home)
    except (FileNotFoundError, NotADirectoryError):
        raise Refused("home_missing")
    except OSError:
        raise Refused("home_not_dir")
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("home_not_dir")
    if st.st_uid != a.owner_uid:
        raise Refused("home_owner")


# ---- 6. empty ---------------------------------------------------------------------------------

def _open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file")
    return fd


def _same_bytes(a, b):
    fa = fb = None
    try:
        fa, fb = _open_regular(a), _open_regular(b)
        while True:
            ca, cb = os.read(fa, CHUNK), os.read(fb, CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True
    except OSError:
        return False
    finally:
        for fd in (fa, fb):
            if fd is not None:
                os.close(fd)


def _same_tree(home, skel):
    """True only if home's tree equals skel's. Bounded by skel: names must match per level."""
    try:
        names = sorted(os.listdir(home))
        if names != sorted(os.listdir(skel)):
            return False
        for name in names:
            h, s = os.path.join(home, name), os.path.join(skel, name)
            sh, ss = os.lstat(h), os.lstat(s)
            if stat.S_IFMT(sh.st_mode) != stat.S_IFMT(ss.st_mode):
                return False
            if stat.S_ISDIR(sh.st_mode):
                if not _same_tree(h, s):
                    return False
            elif stat.S_ISLNK(sh.st_mode):
                if os.readlink(h) != os.readlink(s):
                    return False
            elif stat.S_ISREG(sh.st_mode):
                if sh.st_size != ss.st_size or not _same_bytes(h, s):
                    return False
            else:
                return False              # FIFOs, sockets, devices: never "empty"
    except (OSError, RecursionError):
        return False
    return True


# ---- 7-8. capture -----------------------------------------------------------------------------

def _feed(age, data):
    if data:
        try:
            age.stdin.write(data)
        except OSError:
            raise Refused("age_failed")


def _stop(proc):
    if proc is None:
        return
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait()
    except Exception:
        pass


def _digest(fd):
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = os.pread(fd, CHUNK, size)
        if not chunk:
            return size, h.hexdigest()
        h.update(chunk)
        size += len(chunk)


def _capture(a):
    fd = None
    tar = age = None
    done = False
    try:
        try:
            fd = os.open(a.out, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
        except FileExistsError:
            raise Refused("out_exists")
        except OSError:
            raise Refused("path_unsafe")
        try:
            os.fchmod(fd, 0o600)                  # whatever the umask
            created = os.fstat(fd)
            try:
                age = subprocess.Popen([a.age, "-r", a.recipient, "-o", a.out],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=CHILD_ENV, close_fds=True)
            except (OSError, ValueError):
                raise Refused("age_failed")
            try:
                tar = subprocess.Popen([TAR, "--one-file-system", "--numeric-owner", "--acls",
                                        "--xattrs", "-C", a.home, "-cf", "-", "."],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=CHILD_ENV, close_fds=True)
            except (OSError, ValueError):
                raise Refused("tar_failed")
            gz = zlib.compressobj(1, zlib.DEFLATED, 31)   # gzip container: mtime 0, no name
            raw = 0
            src = tar.stdout.fileno()
            while True:
                try:
                    chunk = os.read(src, CHUNK)
                except OSError:
                    raise Refused("tar_failed")
                if not chunk:
                    break
                raw += len(chunk)
                if raw > a.cap_bytes:
                    raise Refused("cap_exceeded")
                _feed(age, gz.compress(chunk))
            _feed(age, gz.flush())
            try:
                age.stdin.close()
            except OSError:
                raise Refused("age_failed")
            tar_rc, age_rc = tar.wait(), age.wait()
            if tar_rc != 0:
                raise Refused("tar_failed")
            if age_rc != 0:
                raise Refused("age_failed")
            try:
                now = os.lstat(a.out)
            except OSError:
                raise Refused("age_failed")
            if ((now.st_dev, now.st_ino) != (created.st_dev, created.st_ino)
                    or not stat.S_ISREG(now.st_mode)):
                raise Refused("age_failed")       # --out was replaced under us
            os.fsync(fd)
            size, sha = _digest(fd)
            if size == 0 or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise Refused("age_failed")
        except OSError:
            raise Refused("age_failed")
        done = True
        return raw, size, sha
    finally:
        _stop(tar)
        _stop(age)
        if fd is not None:
            if not done:
                try:
                    os.unlink(a.out)              # only ever the file this run created
                except OSError:
                    pass
            os.close(fd)


# ---- main -------------------------------------------------------------------------------------

def _doc(status, reason, raw=0, size=0, sha=""):
    return {"status": status, "reason": reason, "raw_bytes": raw, "age_bytes": size,
            "age_sha256": sha}


def _run(argv, ctx):
    ctx["stage"] = "bad_args"
    a = _parse(argv)
    ctx["stage"] = "path_unsafe"
    _check_paths(a)
    ctx["stage"] = "sysctl_unsafe"
    _check_sysctls(a.sysctl_root)
    ctx["stage"] = "mount_under_home"
    _check_mounts(a)
    ctx["stage"] = "home_not_dir"
    _check_home(a)
    if _same_tree(a.home, a.skel):
        return _doc("empty", "empty_home")
    ctx["stage"] = "tar_failed"
    raw, size, sha = _capture(a)
    return _doc("saved", "ok", raw, size, sha)


def _on_signal(signum, frame):
    for s in _SIGNALS:
        signal.signal(s, signal.SIG_IGN)          # one interrupt; cleanup runs undisturbed
    raise _Interrupted()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ctx = {"stage": "bad_args"}
    old = {s: signal.signal(s, _on_signal) for s in _SIGNALS}
    try:
        try:
            doc = _run(argv, ctx)
        except Refused as r:
            doc = _doc("refused", r.reason)
        except _Interrupted:
            doc = _doc("refused", "tar_failed")
        except Exception:
            doc = _doc("refused", ctx["stage"])   # an unexpected error fails the running step
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()
    return 0 if doc["status"] in ("saved", "empty") else 1


if __name__ == "__main__":
    sys.exit(main())
