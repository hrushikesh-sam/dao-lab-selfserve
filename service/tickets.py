"""B-107 restore tickets: S lives only in one 0600 tmpfs file for at most five minutes."""
import logging
import os
import re
import secrets
import stat
from datetime import datetime

from . import config, hostcheck

log = logging.getLogger("daolab.tickets")

IDENTITY_RE = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{58}")
PUBLIC_ID_RE = re.compile(r"[0-9a-f]{32}")
TTL_SECONDS = 300
CLOCK_SLACK_SECONDS = 5
DEFAULT_MOUNTINFO = "/proc/self/mountinfo"
INVALID = "ticket:invalid"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CHUNK = 65536


class TicketError(Exception):
    pass


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _on_tmpfs(path: str, mountinfo: str) -> bool:
    target = os.path.normpath(os.path.realpath(path))
    best_len = -1
    fstype = ""
    try:
        with open(mountinfo) as fh:
            for line in fh:
                parts = line.split()
                try:
                    sep = parts.index("-")
                except ValueError:
                    continue
                if sep <= 4 or sep + 1 >= len(parts):
                    continue
                mount = os.path.normpath(_unescape(parts[4]))
                if (target == mount or target.startswith(mount.rstrip(os.sep) + os.sep)) \
                        and len(mount) > best_len:
                    best_len, fstype = len(mount), parts[sep + 1]
    except OSError:
        return False
    return best_len >= 0 and fstype == "tmpfs"


def _check_dir(path: str, mountinfo: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        raise TicketError(INVALID) from None
    if not stat.S_ISDIR(st.st_mode):
        raise TicketError(INVALID)
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise TicketError(INVALID)
    if st.st_uid != os.geteuid():
        raise TicketError(INVALID)
    if not _on_tmpfs(path, mountinfo):
        raise TicketError(INVALID)


def ensure_dir(mountinfo: str = DEFAULT_MOUNTINFO) -> None:
    path = config.settings.ticket_dir
    created = not os.path.lexists(path)
    if created:
        try:
            os.makedirs(path, mode=0o700)
            os.chmod(path, 0o700)
        except OSError:
            raise TicketError(INVALID) from None
    _check_dir(path, mountinfo)


def _valid_ids(user_id: int, public_id: str) -> bool:
    return (isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0
            and isinstance(public_id, str) and PUBLIC_ID_RE.fullmatch(public_id) is not None)


def _normalize_secret(s: bytes) -> bytes:
    if not isinstance(s, bytes):
        raise TicketError(INVALID)
    if s.endswith(b"\r\n"):
        value = s[:-2]
    elif s.endswith(b"\n"):
        value = s[:-1]
    else:
        value = s
    try:
        ok = IDENTITY_RE.fullmatch(value.decode("ascii")) is not None
    except UnicodeDecodeError:
        ok = False
    if not ok:
        raise TicketError(INVALID)
    return value


def _file_ok(st, now_ts: float | None = None) -> bool:
    if not stat.S_ISREG(st.st_mode):
        return False
    if stat.S_IMODE(st.st_mode) != 0o600:
        return False
    if st.st_uid != os.geteuid():
        return False
    if st.st_nlink != 1:
        return False
    if now_ts is not None:
        age = now_ts - st.st_mtime
        if age < -CLOCK_SLACK_SECONDS or age > TTL_SECONDS:
            return False
    return True


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def write(user_id: int, public_id: str, s: bytes, *, mountinfo: str = DEFAULT_MOUNTINFO) -> None:
    hostcheck.require_safe_swap()
    if not _valid_ids(user_id, public_id):
        raise TicketError(INVALID)
    value = _normalize_secret(s)
    ensure_dir(mountinfo=mountinfo)
    name = f"{secrets.token_hex(16)}-{user_id}-{public_id}"
    path = os.path.join(config.settings.ticket_dir, name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    except OSError:
        raise TicketError(INVALID) from None
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(value)
            while view:
                view = view[os.write(fd, view[:_CHUNK]):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not _file_ok(os.lstat(path)):
            raise TicketError(INVALID)
    except (OSError, TicketError):
        _unlink_quiet(path)
        raise TicketError(INVALID) from None
    log.info("ticket written: user_id=%s public_id=%s", user_id, public_id)


def take(user_id: int, public_id: str, now: datetime) -> bytes:
    if not _valid_ids(user_id, public_id):
        raise TicketError(INVALID)
    try:
        matches = [os.path.join(config.settings.ticket_dir, name)
                   for name in os.listdir(config.settings.ticket_dir)
                   if re.fullmatch(rf"[0-9a-f]{{32}}-{user_id}-{public_id}", name)]
    except OSError:
        raise TicketError(INVALID) from None
    if not matches:
        raise TicketError("ticket:missing")
    if len(matches) > 1:
        for path in matches:
            _unlink_quiet(path)
        raise TicketError("ticket:ambiguous")
    path = matches[0]
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        try:
            if not _file_ok(os.fstat(fd), now.timestamp()):
                raise TicketError(INVALID)
            chunks = []
            while chunk := os.read(fd, _CHUNK):
                chunks.append(chunk)
        finally:
            os.close(fd)
        os.unlink(path)
    except (OSError, TicketError):
        _unlink_quiet(path)
        raise TicketError(INVALID) from None
    return b"".join(chunks)


def sweep(now: datetime) -> int:
    try:
        entries = list(os.scandir(config.settings.ticket_dir))
    except FileNotFoundError:
        log.info("ticket sweep: removed 0")
        return 0
    except OSError:
        raise TicketError(INVALID) from None
    removed = 0
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        age = now.timestamp() - st.st_mtime
        if stat.S_ISREG(st.st_mode) and -CLOCK_SLACK_SECONDS <= age <= TTL_SECONDS:
            continue
        try:
            os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    log.info("ticket sweep: removed %d", removed)
    return removed
