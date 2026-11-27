#!/usr/bin/python3
"""daolab-wipe.py - empty /home/lab back to /etc/skel on the DPU, or check it (B-109b).

Runs as root on the DPU (errata E-6) with Python 3.10 standard library only (E-3), in one of
two explicit modes (there is no default, so a typo can never wipe):

    python3 -I daolab-wipe.py --mode wipe  --home /home/lab --skel /etc/skel --owner-uid <uid>
    python3 -I daolab-wipe.py --mode check --home /home/lab --skel /etc/skel --owner-uid <uid>
        [--mountinfo /proc/self/mountinfo]

wipe   Removes every entry of the home without following symlinks (directories are opened
       with O_NOFOLLOW and emptied through their file descriptors; nothing outside the home is
       ever reached through a link), copies the skel tree back (permission bits kept, setuid/
       setgid dropped; files created O_EXCL|O_NOFOLLOW so a planted link is never written
       through), gives every copied entry to --owner-uid and the home directory's group, then
       proves the home equals skel. The home directory itself stays (owner already checked).
       The playbook (home_wipe.yml) runs this only when the engine allowed it from the DB.
       Prints {"status": "wiped"|"refused", "reason": <code>, "removed": n}; n = top-level
       entries removed. Exit 0 wiped, 1 refused.
check  Read-only (verify_clean.yml's home:lab check). Counts the top-level names where home
       and skel differ, with the same meaning of "equal" as daolab-capture.py's empty check
       (same names at every depth, same types, regular files byte-identical, symlink targets
       identical; FIFOs, sockets and devices never equal; anything unreadable differs).
       Prints {"status": "clean"|"dirty"|"refused", "reason": <code>, "differ": n}.
       Exit 0 clean/dirty, 1 refused.

Both modes run the guards of daolab-capture.py first (rules copied, not imported): every option
exactly once (bad_args); --home and --skel absolute, normalized, no control chars, not a system
root, at least two levels deep, neither inside the other (path_unsafe); --skel a real directory
(skel_failed, before anything is removed); no mount point at or under the home (mount_under_home;
unreadable or malformed mountinfo refuses too); the home a real directory (not a symlink) owned
by --owner-uid (home_missing, home_not_dir, home_owner).

Exactly one JSON line on stdout, nothing on stderr, never a file name or a path. Run it with
`python3 -I`, so nothing beside the script can shadow a module.
"""
import argparse
import errno
import json
import os
import re
import stat
import sys

CHUNK = 1 << 16
MOUNTINFO_MAX = 16 << 20
REASONS = ("ok", "home_differs", "bad_args", "path_unsafe", "home_missing", "home_not_dir",
           "home_owner", "mount_under_home", "skel_failed", "remove_failed", "not_clean")
MODES = ("wipe", "check")
DIGITS_RE = re.compile(r"[0-9]{1,20}")
FORBIDDEN = frozenset(("/", "/root", "/etc", "/home", "/usr", "/var"))
_ESCAPE_RE = re.compile(rb"\\([0-7]{3})")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---- arguments ----------------------------------------------------------------------------

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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = False

    def __call__(self, parser, namespace, values, option_string=None):
        if self.seen:
            raise Refused("bad_args")
        self.seen = True
        setattr(namespace, self.dest, values)


def _parse(argv):
    p = _Parser(prog="daolab-wipe", add_help=False, allow_abbrev=False)
    for name in ("--mode", "--home", "--skel", "--owner-uid"):
        p.add_argument(name, required=True, action=_Once)
    p.add_argument("--mountinfo", default="/proc/self/mountinfo", action=_Once)
    a = p.parse_args(argv)
    if a.mode not in MODES or not DIGITS_RE.fullmatch(a.owner_uid):
        raise Refused("bad_args")
    a.owner_uid = int(a.owner_uid)
    if a.owner_uid > 0xFFFFFFFE or not _clean_abs(a.mountinfo):
        raise Refused("bad_args")
    return a


def _mode_of(argv):
    """The mode asked for, only to pick the output keys of a refusal (never trusted)."""
    for i, arg in enumerate(argv):
        if arg == "--mode" and i + 1 < len(argv):
            return argv[i + 1]
    return "wipe"


# ---- paths (the rules of daolab-capture.py) -----------------------------------------------

def _clean_abs(path):
    if not path.startswith("/") or path.startswith("//"):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    if os.path.normpath(path) != path:
        return False
    return not any(part in (".", "..") for part in path.split("/"))


def _sane(path):
    if not _clean_abs(path) or path in FORBIDDEN:
        return False
    return len([part for part in path.split("/") if part]) >= 2


def _within(path, top):
    return path == top or path.startswith(top + "/")


def _check_paths(a):
    if not (_sane(a.home) and _sane(a.skel)):
        raise Refused("path_unsafe")
    if _within(a.home, a.skel) or _within(a.skel, a.home):
        raise Refused("path_unsafe")
    real_home, real_skel = os.path.realpath(a.home), os.path.realpath(a.skel)
    if _within(real_home, real_skel) or _within(real_skel, real_home):
        raise Refused("path_unsafe")


def _check_skel(a):
    try:
        st = os.lstat(a.skel)
    except OSError:
        raise Refused("skel_failed")
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("skel_failed")
    try:                                    # the whole tree must be readable before any removal
        for dirpath, dirnames, filenames in os.walk(a.skel, followlinks=False,
                                                    onerror=_raise):
            for name in filenames:
                p = os.path.join(dirpath, name)
                s = os.lstat(p)
                if stat.S_ISREG(s.st_mode):
                    os.close(_open_regular(p))
                elif not stat.S_ISLNK(s.st_mode):
                    raise Refused("skel_failed")      # skel holds files, dirs, links only
    except (OSError, RecursionError):
        raise Refused("skel_failed")


def _raise(err):
    raise err


# ---- mounts -------------------------------------------------------------------------------

def _unescape(field):
    def one(m):
        n = int(m.group(1), 8)
        if n > 0xFF:
            raise ValueError("bad escape")
        return bytes((n,))
    return _ESCAPE_RE.sub(one, field)


def _mount_points(path):
    with open(path, "rb") as fh:
        data = fh.read(MOUNTINFO_MAX + 1)
    if len(data) > MOUNTINFO_MAX or not data.strip():
        raise ValueError("mountinfo size")
    points = []
    for line in data.split(b"\n"):
        if not line:
            continue
        fields = line.split(b" ")
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


# ---- home ---------------------------------------------------------------------------------

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
    return st


# ---- equality (the meaning of daolab-capture.py's empty check) ----------------------------

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


def _same_entry(h, s):
    """True only if the entry h equals the skel entry s (types first: a FIFO is never opened)."""
    try:
        sh, ss = os.lstat(h), os.lstat(s)
        if stat.S_IFMT(sh.st_mode) != stat.S_IFMT(ss.st_mode):
            return False
        if stat.S_ISDIR(sh.st_mode):
            return _same_tree(h, s)
        if stat.S_ISLNK(sh.st_mode):
            return os.readlink(h) == os.readlink(s)
        if stat.S_ISREG(sh.st_mode):
            return sh.st_size == ss.st_size and _same_bytes(h, s)
        return False                        # FIFOs, sockets, devices: never equal
    except (OSError, RecursionError):
        return False


def _same_tree(home, skel):
    try:
        names = sorted(os.listdir(home))
        if names != sorted(os.listdir(skel)):
            return False
    except OSError:
        return False
    return all(_same_entry(os.path.join(home, n), os.path.join(skel, n)) for n in names)


def _differ(home, skel):
    """How many top-level names differ between home and skel (count only)."""
    try:
        h, s = set(os.listdir(home)), set(os.listdir(skel))
    except OSError:
        raise Refused("home_not_dir")
    n = len(h ^ s)
    for name in h & s:
        if not _same_entry(os.path.join(home, name), os.path.join(skel, name)):
            n += 1
    return n


# ---- wipe ---------------------------------------------------------------------------------

def _empty_dir(dfd, dev):
    """Remove everything inside the directory open as `dfd`, never following a link and never
    leaving the home's filesystem."""
    for name in os.listdir(dfd):
        st = os.lstat(name, dir_fd=dfd)
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != dev:
                raise Refused("remove_failed")
            sub = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            try:
                got = os.fstat(sub)
                if (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
                    raise Refused("remove_failed")   # swapped under us
                _empty_dir(sub, dev)
            finally:
                os.close(sub)
            os.rmdir(name, dir_fd=dfd)
        else:
            os.unlink(name, dir_fd=dfd)


def _remove_all(a, home_st):
    fd = os.open(a.home, _DIR_FLAGS)
    try:
        got = os.fstat(fd)
        if (got.st_dev, got.st_ino) != (home_st.st_dev, home_st.st_ino):
            raise Refused("remove_failed")
        names = os.listdir(fd)
        _empty_dir(fd, got.st_dev)
        return len(names)
    finally:
        os.close(fd)


def _copy_into(src_dir, dst_fd, uid, gid):
    for name in sorted(os.listdir(src_dir)):
        src = os.path.join(src_dir, name)
        st = os.lstat(src)
        mode = stat.S_IMODE(st.st_mode) & 0o1777          # no setuid / setgid
        if stat.S_ISDIR(st.st_mode):
            os.mkdir(name, 0o700, dir_fd=dst_fd)
            sub = os.open(name, _DIR_FLAGS, dir_fd=dst_fd)
            try:
                _copy_into(src, sub, uid, gid)
                os.fchown(sub, uid, gid)
                os.fchmod(sub, mode)
            finally:
                os.close(sub)
        elif stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), name, dir_fd=dst_fd)
            os.chown(name, uid, gid, dir_fd=dst_fd, follow_symlinks=False)
        elif stat.S_ISREG(st.st_mode):
            sfd = _open_regular(src)
            try:
                dfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                              | os.O_CLOEXEC, 0o600, dir_fd=dst_fd)
                try:
                    while True:
                        chunk = os.read(sfd, CHUNK)
                        if not chunk:
                            break
                        os.write(dfd, chunk)
                    os.fchown(dfd, uid, gid)
                    os.fchmod(dfd, mode)
                finally:
                    os.close(dfd)
            finally:
                os.close(sfd)
        else:
            raise Refused("skel_failed")


def _copy_skel(a, home_st):
    fd = os.open(a.home, _DIR_FLAGS)
    try:
        got = os.fstat(fd)
        if (got.st_dev, got.st_ino) != (home_st.st_dev, home_st.st_ino):
            raise Refused("skel_failed")
        _copy_into(a.skel, fd, a.owner_uid, home_st.st_gid)
    finally:
        os.close(fd)


# ---- main ---------------------------------------------------------------------------------

def _doc(mode, status, reason, n=0):
    key = "differ" if mode == "check" else "removed"
    return {"status": status, "reason": reason, key: n}


def _run(argv, ctx):
    ctx["stage"] = "bad_args"
    a = _parse(argv)
    ctx["mode"] = a.mode
    ctx["stage"] = "path_unsafe"
    _check_paths(a)
    ctx["stage"] = "skel_failed"
    _check_skel(a)
    ctx["stage"] = "mount_under_home"
    _check_mounts(a)
    ctx["stage"] = "home_not_dir"
    home_st = _check_home(a)
    if a.mode == "check":
        n = _differ(a.home, a.skel)
        return _doc("check", "clean" if n == 0 else "dirty", "ok" if n == 0 else "home_differs", n)
    ctx["stage"] = "remove_failed"
    try:
        removed = _remove_all(a, home_st)
    except (OSError, RecursionError):
        raise Refused("remove_failed")
    ctx["stage"] = "skel_failed"
    try:
        _copy_skel(a, home_st)
    except (OSError, RecursionError):
        raise Refused("skel_failed")
    ctx["stage"] = "not_clean"
    if not _same_tree(a.home, a.skel):
        raise Refused("not_clean")
    return _doc("wipe", "wiped", "ok", removed)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ctx = {"stage": "bad_args", "mode": None}
    try:
        doc = _run(argv, ctx)
    except Refused as r:
        doc = _doc(ctx["mode"] or _mode_of(argv), "refused", r.reason)
    except Exception:
        doc = _doc(ctx["mode"] or _mode_of(argv), "refused", ctx["stage"])
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()
    return 0 if doc["status"] in ("wiped", "clean", "dirty") else 1


if __name__ == "__main__":
    sys.exit(main())
