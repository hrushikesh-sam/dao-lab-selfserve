#!/usr/bin/env python3
"""daolab-tripwire: after a cleanup, compare the board outside /home/lab with a baseline
(B-112a; design v1.1 §3; task list Q-BASELINE). Python 3.10 stdlib only: it runs on the DPU.

    daolab-tripwire.py --mode check|collect [--root DIR] [--proc DIR] [--input-fd N]

It reads ONE JSON document, from stdin or, with --input-fd, from that descriptor (for when the
script itself arrives on stdin):
  check    the baseline {"format": "daolab-tripwire/1", "version", "hw_class", "classes"}.
           For each class, count = |current - baseline| + |baseline - current| (additions and
           removals; a changed entry counts 2). Prints one line tripwire:<class>:<count> per
           class with a count, in fixed class order, or exactly "tripwire:ok". Exit 0.
  collect  {"format": "daolab-tripwire-collect/1", "ignore": {<class>: [<glob>, ...]}}.
           Prints one line {"classes": {<class>: {"fingerprints": [<sorted hex>]}}}. Exit 0.
Bad input: "tripwire:error:bad_input" or "tripwire:error:bad_baseline", exit 2. Anything
unexpected: "tripwire:error:internal", exit 2. Nothing else is ever printed, on stdout or
stderr: never a path, a file or user name, a uid, a module or unit name, or a command line.

The authoritative baseline lives on the controller (the user was root: a copy on the DPU is not
trusted). This is DETECTION ONLY: a root user can tamper with python3 or with this script's
inputs; see deploy/tripwire/README.md. --root and --proc exist for tests (a fake board).

A fingerprint is the sha256 of a normalized record: json.dumps([class, kind, field, ...]) with
ASCII escapes, so it never holds a newline and field boundaries can't be ambiguous. Walks never
follow a symlink (lstat); special files are recorded by type only; an entry that can't be read
is recorded as "unreadable" + its path, so it can't hide. "ignore" globs (fnmatch, case
sensitive; `*` also matches `/`) are matched against each record's subject: the path relative
to --root for file records, `<file>:<name>` for identity lines, the module name for loaded
modules, the mount point and the exe path without their leading `/`.
"""
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import warnings

FORMAT = "daolab-tripwire/1"
COLLECT_FORMAT = "daolab-tripwire-collect/1"
CLASSES = ("identity", "sudo", "ssh", "sched", "boot", "bin", "kmod", "mount", "proc", "tmp",
           "daolab")
LAB_LOGIN = "lab"            # its home is covered by snapshot capture/wipe, its keys change per booking
MAX_INPUT = 16 * 1024 * 1024
MAX_GLOBS = 256
MAX_GLOB_LEN = 256
MAX_HW_CLASS = 128
MAX_INT_DIGITS = 18
HEX_RE = re.compile(r"[0-9a-f]{64}")
HW_CLASS_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")          # service/catalog.py KEBAB
PID_RE = re.compile(r"[0-9]+")
FD_RE = re.compile(r"[0-9]{1,6}")
OCTAL_RE = re.compile(r"\\([0-7]{3})")
OPTIONS = ("--mode", "--root", "--proc", "--input-fd")
CHUNK = 1024 * 1024


class Refused(Exception):
    """Bad input: the code is printed as tripwire:error:<code>; nothing else is."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- records --------------------------------------------------------------------------------
def fingerprint(cls, record):
    text = json.dumps([cls] + list(record), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


class Ctx:
    """Where to look (--root, --proc) and the ignore globs of the class being collected."""

    def __init__(self, root, proc, ignore=()):
        self.root = root
        self.proc = proc
        self.ignore = tuple(ignore)

    def ignored(self, subject):
        return any(fnmatch.fnmatchcase(subject, g) for g in self.ignore)

    def path(self, rel):
        return os.path.join(self.root, rel)


def _open_regular(path):
    """An fd for a regular file, never through a final symlink and never blocking on a FIFO
    that replaced the file after its lstat; None when that isn't possible."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | \
        getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def file_sha256(path):
    fd = _open_regular(path)
    if fd is None:
        return None
    h = hashlib.sha256()
    try:
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_bytes(path):
    """(found, data): (False, None) missing; (True, None) there but unreadable."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError:
        return True, None
    fd = _open_regular(path)
    if fd is None:
        return True, None
    parts = []
    try:
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                return True, b"".join(parts)
            parts.append(chunk)
    except OSError:
        return True, None
    finally:
        os.close(fd)


def text_lines(data):
    text = data.decode("utf-8", "surrogateescape")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _type_char(mode):
    for test, char in ((stat.S_ISFIFO, "p"), (stat.S_ISSOCK, "s"), (stat.S_ISCHR, "c"),
                       (stat.S_ISBLK, "b")):
        if test(mode):
            return char
    return "?"


def _mode(st):
    return "%04o" % stat.S_IMODE(st.st_mode)


def walk(ctx, top, recursive=True, skip=()):
    """Yield (rel, st) for `top` (relative to --root) and, when it is a real directory, for
    everything under it. st is None for an entry that could not be read (a directory that can't
    be listed is yielded once with its lstat, then once with None). Never follows a symlink;
    a (dev, ino) set guards against bind-mount cycles; ignored or `skip` paths are left out
    with their subtree. A missing top yields nothing."""
    if top in skip or ctx.ignored(top):
        return
    try:
        st = os.lstat(ctx.path(top))
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        yield top, None
        return
    yield top, st
    if not recursive or not stat.S_ISDIR(st.st_mode):
        return
    seen = {(st.st_dev, st.st_ino)}
    stack = [top]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(ctx.path(rel_dir)) as it:
                entries = list(it)
        except OSError:
            yield rel_dir, None
            continue
        for entry in entries:
            rel = posixpath.join(rel_dir, entry.name)
            if rel in skip or ctx.ignored(rel):
                continue
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                yield rel, None
                continue
            yield rel, est
            if stat.S_ISDIR(est.st_mode):
                key = (est.st_dev, est.st_ino)
                if key not in seen:
                    seen.add(key)
                    stack.append(rel)


def entry_record(ctx, rel, st, full):
    """The record of one walked entry. full: files and dirs carry mode and owner uid too."""
    if st is None:
        return ("unreadable", rel)
    m = st.st_mode
    if stat.S_ISLNK(m):
        try:
            return ("link", rel, os.readlink(ctx.path(rel)))
        except OSError:
            return ("unreadable", rel)
    if stat.S_ISREG(m):
        digest = file_sha256(ctx.path(rel))
        if digest is None:
            return ("unreadable", rel)
        return ("file", rel, _mode(st), st.st_uid, digest) if full else ("file", rel, digest)
    if stat.S_ISDIR(m):
        return ("dir", rel, _mode(st), st.st_uid) if full else ("dir", rel)
    return ("special", rel, _type_char(m))


def tree(ctx, top, full=True, recursive=True, select=None):
    return {entry_record(ctx, rel, st, full) for rel, st in walk(ctx, top, recursive)
            if select is None or select(rel)}


def trees(ctx, tops, full=True):
    out = set()
    for top in tops:
        out |= tree(ctx, top, full)
    return out


# ---- the classes ------------------------------------------------------------------------------
def password_state(field):
    """shadow/gshadow password field -> its state class. The field itself is never recorded:
    it is secret and lab's rotates every booking."""
    if field.startswith(("!", "*")):
        return "locked"
    return "empty" if field == "" else "set"


def collect_identity(ctx):
    recs = set()
    for rel, kind in (("etc/passwd", "line"), ("etc/group", "line"), ("etc/shadow", "state"),
                      ("etc/gshadow", "state")):
        if ctx.ignored(rel):
            continue
        found, data = read_bytes(ctx.path(rel))
        if not found:
            continue
        for _, st in walk(ctx, rel, recursive=False):                 # its mode and owner
            recs.add(("meta", rel, _mode(st), st.st_uid) if st is not None else
                     ("unreadable", rel))
        if data is None:
            recs.add(("unreadable", rel))
            continue
        for line in text_lines(data):
            fields = line.split(":")
            if ctx.ignored(f"{rel}:{fields[0]}"):
                continue
            if kind == "line":
                recs.add(("line", rel, line))
            else:
                recs.add(("state", rel, fields[0],
                          password_state(fields[1] if len(fields) > 1 else "")))
    # what every new home and every login shell gets
    recs |= trees(ctx, ("etc/skel", "etc/profile", "etc/profile.d", "etc/bash.bashrc",
                        "etc/environment"))
    return recs


def collect_sudo(ctx):
    return trees(ctx, ("etc/sudoers", "etc/sudoers.d"))


def homes(ctx):
    """Every home in etc/passwd as a path relative to --root, except lab's."""
    _, data = read_bytes(ctx.path("etc/passwd"))
    if data is None:
        return set()
    out, lab = set(), set()
    for line in text_lines(data):
        fields = line.split(":")
        if len(fields) < 7 or not fields[5].startswith("/"):
            continue
        rel = posixpath.normpath(fields[5]).lstrip("/")      # an absolute normpath has no ".."
        (lab if fields[0] == LAB_LOGIN else out).add(rel)
    return out - lab


def collect_ssh(ctx):
    recs = tree(ctx, "etc/ssh")
    for home in homes(ctx):
        for name in ("authorized_keys", "authorized_keys2"):
            recs |= tree(ctx, posixpath.join(home, ".ssh", name), recursive=False)
    return recs


SCHED_TOPS = ("etc/crontab", "etc/cron.d", "etc/cron.hourly", "etc/cron.daily",
              "etc/cron.weekly", "etc/cron.monthly", "var/spool/cron/crontabs",
              "var/spool/cron/atjobs")
UNITS = "etc/systemd/system"


def _is_timer(rel):
    return rel.endswith(".timer")


def collect_sched(ctx):
    recs = trees(ctx, SCHED_TOPS, full=False)
    return recs | tree(ctx, UNITS, full=False, select=_is_timer)


def collect_boot(ctx):
    """Files only, never systemctl: unit files and overrides, *.wants/*.requires links, aliases
    and masks under etc/systemd/system (timers belong to sched), and etc/rc.local."""
    recs = tree(ctx, UNITS, full=False, select=lambda rel: not _is_timer(rel))
    return recs | tree(ctx, "etc/rc.local", full=False)


SETUID_TOPS = ("usr/bin", "usr/sbin", "usr/local", "bin", "sbin", "opt")
LOCAL_BIN = ("usr/local/bin", "usr/local/sbin")


def collect_bin(ctx):
    recs = set()
    for top in SETUID_TOPS:
        for rel, st in walk(ctx, top, skip=LOCAL_BIN):
            if st is None:
                recs.add(("unreadable", rel))
            elif rel == top and stat.S_ISLNK(st.st_mode):           # bin -> usr/bin
                recs.add(entry_record(ctx, rel, st, True))
            elif stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                recs.add(("setid", rel, _mode(st), st.st_uid))
    # local binaries by content (not all of usr/bin: too slow), and the dynamic loader's config
    return recs | trees(ctx, LOCAL_BIN + ("etc/ld.so.preload", "etc/ld.so.conf",
                                          "etc/ld.so.conf.d"))


def _proc_file(ctx, rel):
    return read_bytes(os.path.join(ctx.proc, rel))


def collect_kmod(ctx):
    recs = set()
    found, data = _proc_file(ctx, "modules")
    if found and data is None:
        recs.add(("unreadable", "proc:modules"))
    for line in text_lines(data or b""):
        name = line.split(" ", 1)[0]
        if name and not ctx.ignored(name):
            recs.add(("module", name))
    return recs | trees(ctx, ("etc/modprobe.d", "etc/modules", "etc/modules-load.d"))


def decode_mount_field(field):
    """mountinfo escapes space, tab, newline and backslash as \\NNN octal."""
    return OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


def collect_mount(ctx):
    recs = set()
    found, data = _proc_file(ctx, "self/mountinfo")
    if found and data is None:
        recs.add(("unreadable", "proc:self/mountinfo"))
    for line in text_lines(data or b""):
        left, sep, right = line.partition(" - ")
        fields, fstype = left.split(" "), right.split(" ")[0]
        if not sep or len(fields) < 5 or not fstype:
            recs.add(("malformed", line))
            continue
        point = decode_mount_field(fields[4])
        if not ctx.ignored(point.lstrip("/")):
            recs.add(("mnt", point, fstype))
    return recs


def _status_uid(path):
    _, data = read_bytes(path)
    for line in text_lines(data or b""):
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) > 1 and PID_RE.fullmatch(parts[1]):
                return int(parts[1])
    return None


def collect_proc(ctx):
    """Executable path + real uid of every process, never the pid. A process whose exe or
    status can't be read (kernel threads, a process that just exited) is skipped."""
    try:
        names = os.listdir(ctx.proc)
    except OSError:
        return {("unreadable", "proc:")}
    own = str(os.getpid())
    recs = set()
    for name in names:
        if not PID_RE.fullmatch(name) or name == own:
            continue
        try:
            exe = os.readlink(os.path.join(ctx.proc, name, "exe"))
        except OSError:
            continue
        uid = _status_uid(os.path.join(ctx.proc, name, "status"))
        if uid is None or ctx.ignored(exe.lstrip("/")):
            continue
        recs.add(("p", exe, uid))
    return recs


def collect_tmp(ctx):
    """Regular files (path, owner uid, size; never the content), links and special files under
    tmp, var/tmp and dev/shm. Directories alone are not recorded."""
    recs = set()
    for top in ("tmp", "var/tmp", "dev/shm"):
        for rel, st in walk(ctx, top):
            if st is None:
                recs.add(("unreadable", rel))
            elif stat.S_ISREG(st.st_mode):
                recs.add(("file", rel, st.st_uid, st.st_size))
            elif not stat.S_ISDIR(st.st_mode):
                recs.add(entry_record(ctx, rel, st, False))
    return recs


def collect_daolab(ctx):
    return tree(ctx, "etc/daolab")


COLLECTORS = {"identity": collect_identity, "sudo": collect_sudo, "ssh": collect_ssh,
              "sched": collect_sched, "boot": collect_boot, "bin": collect_bin,
              "kmod": collect_kmod, "mount": collect_mount, "proc": collect_proc,
              "tmp": collect_tmp, "daolab": collect_daolab}


def collect_all(root, proc, ignore):
    """{class: set of fingerprints}, each class collected with its own ignore globs."""
    return {cls: {fingerprint(cls, r) for r in COLLECTORS[cls](Ctx(root, proc, ignore[cls]))}
            for cls in CLASSES}


# ---- input -------------------------------------------------------------------------------------
def parse_args(argv):
    opts = {}
    i = 0
    while i < len(argv):
        name = argv[i]
        if name not in OPTIONS or name in opts or i + 1 >= len(argv):
            raise Refused("bad_input")
        opts[name] = argv[i + 1]
        i += 2
    mode = opts.get("--mode")
    root, proc = opts.get("--root", "/"), opts.get("--proc", "/proc")
    fd = opts.get("--input-fd")
    if mode not in ("check", "collect") or (fd is not None and not FD_RE.fullmatch(fd)):
        raise Refused("bad_input")
    for d in (root, proc):
        if not d or not os.path.isdir(d):
            raise Refused("bad_input")
    return mode, root, proc, (None if fd is None else int(fd))


def read_input(fd):
    if fd is None:
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            raise Refused("bad_input")
        try:
            data = stream.read(MAX_INPUT + 1)
        except (OSError, ValueError):
            raise Refused("bad_input") from None
    else:
        parts, size = [], 0
        try:
            while size <= MAX_INPUT:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
        except OSError:
            raise Refused("bad_input") from None
        data = b"".join(parts)
    if data is None or len(data) > MAX_INPUT:
        raise Refused("bad_input")
    return data


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("not JSON")


def _small_int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError("too many digits")
    return int(text)


def load_json(data):
    """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity, no huge ints, no deep nesting."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant, parse_int=_small_int)
    except (ValueError, RecursionError):
        raise Refused("bad_input") from None


def valid_globs(value):
    return (isinstance(value, list) and len(value) <= MAX_GLOBS
            and len(set(g for g in value if isinstance(g, str))) == len(value)
            and all(isinstance(g, str) and 0 < len(g) <= MAX_GLOB_LEN and not g.startswith("/")
                    and not any(c in g for c in "\x00\n\r") for g in value))


def validate_baseline(doc):
    """-> ({class: set of fingerprints}, {class: globs}); Refused otherwise."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise Refused("bad_input")
    bad = Refused("bad_baseline")
    version, hw_class, classes = doc.get("version"), doc.get("hw_class"), doc.get("classes")
    if set(doc) != {"format", "version", "hw_class", "classes"}:
        raise bad
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise bad
    if not isinstance(hw_class, str) or len(hw_class) > MAX_HW_CLASS or \
            not HW_CLASS_RE.fullmatch(hw_class):
        raise bad
    if not isinstance(classes, dict) or set(classes) != set(CLASSES):
        raise bad
    fps, ignore = {}, {}
    for cls in CLASSES:
        entry = classes[cls]
        if not isinstance(entry, dict) or set(entry) != {"fingerprints", "ignore"}:
            raise bad
        values = entry["fingerprints"]
        if not isinstance(values, list) or \
                not all(isinstance(f, str) and HEX_RE.fullmatch(f) for f in values) or \
                len(set(values)) != len(values) or not valid_globs(entry["ignore"]):
            raise bad
        fps[cls], ignore[cls] = set(values), entry["ignore"]
    return fps, ignore


def validate_collect(doc):
    if not isinstance(doc, dict) or doc.get("format") != COLLECT_FORMAT or \
            set(doc) != {"format", "ignore"}:
        raise Refused("bad_input")
    ignore = doc["ignore"]
    if not isinstance(ignore, dict) or not set(ignore) <= set(CLASSES) or \
            not all(valid_globs(v) for v in ignore.values()):
        raise Refused("bad_input")
    return {cls: ignore.get(cls, []) for cls in CLASSES}


# ---- main ----------------------------------------------------------------------------------------
def run(argv):
    mode, root, proc, fd = parse_args(argv)
    doc = load_json(read_input(fd))
    if mode == "collect":
        found = collect_all(root, proc, validate_collect(doc))
        out = {"classes": {cls: {"fingerprints": sorted(found[cls])} for cls in CLASSES}}
        return [json.dumps(out, ensure_ascii=True, separators=(",", ":"))]
    baseline, ignore = validate_baseline(doc)
    current = collect_all(root, proc, ignore)
    lines = [f"tripwire:{cls}:{len(current[cls] ^ baseline[cls])}" for cls in CLASSES
             if current[cls] != baseline[cls]]
    return lines or ["tripwire:ok"]


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        lines, code = run(argv), 0
    except Refused as e:
        lines, code = [f"tripwire:error:{e.code}"], 2
    except BaseException as e:                      # noqa: B036 - nothing may reach stderr
        if isinstance(e, SystemExit):
            raise
        lines, code = ["tripwire:error:internal"], 2
    try:
        sys.stdout.write("".join(line + "\n" for line in lines))
        sys.stdout.flush()
    except BaseException:
        try:                                        # no "Exception ignored" at exit either
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except BaseException:
            pass
        return 2
    return code


if __name__ == "__main__":
    warnings.simplefilter("ignore")
    sys.exit(main())
