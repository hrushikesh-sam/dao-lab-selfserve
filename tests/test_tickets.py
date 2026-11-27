"""B-107 tickets: single-use, 5-minute TTL, strict file checks and safe diagnostics."""
import dataclasses
import os
import stat
from datetime import datetime, timezone

import pytest

from service import tickets

S = b"AGE-SECRET-KEY-1" + b"Q" * 58
PUBLIC_ID = "0123456789abcdef0123456789abcdef"
USER_ID = 7
UTC = timezone.utc
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX ticket file semantics")
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ticket file semantics")


@pytest.fixture(autouse=True)
def _safe_swap(monkeypatch):
    from service import hostcheck
    monkeypatch.setattr(hostcheck, "require_safe_swap", lambda: None)


@pytest.fixture
def dir_path(tmp_path, monkeypatch):
    from service import config
    path = tmp_path / "tickets"
    settings = dataclasses.replace(config.settings, ticket_dir=str(path))
    monkeypatch.setattr(config, "settings", settings)
    return path


@pytest.fixture
def ticket_dir(dir_path):
    dir_path.mkdir(mode=0o700)
    if os.name == "posix":
        os.chmod(dir_path, 0o700)
    return dir_path


@pytest.fixture
def fake_mountinfo(tmp_path, dir_path):
    path = tmp_path / "mountinfo"
    path.write_text(f"42 1 0:42 / {dir_path.as_posix()} rw - tmpfs tmpfs rw\n")
    return str(path)


def _fix_name(monkeypatch, value):
    monkeypatch.setattr(tickets.secrets, "token_hex", lambda _n: value)


def _name(value):
    return f"{value}-{USER_ID}-{PUBLIC_ID}"


@POSIX
def test_ensure_dir_creates_0700_on_tmpfs(dir_path, fake_mountinfo):
    tickets.ensure_dir(mountinfo=fake_mountinfo)
    st = os.lstat(dir_path)
    assert stat.S_ISDIR(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.geteuid()


@POSIX
def test_ensure_dir_refuses_non_tmpfs(dir_path, tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"42 1 0:42 / {dir_path.as_posix()} rw - ext4 /dev/sda1 rw\n")
    with pytest.raises(tickets.TicketError) as exc:
        tickets.ensure_dir(mountinfo=str(mountinfo))
    assert str(exc.value) == "ticket:invalid"


@POSIX
def test_ensure_dir_accepts_realistic_mountinfo_with_optional_fields(dir_path, tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "123 45 0:42 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        f"124 45 0:43 / {dir_path.as_posix()} rw,nosuid,nodev shared:7 - tmpfs tmpfs "
        "rw,size=64m,inode64\n")
    tickets.ensure_dir(mountinfo=str(mountinfo))
    assert dir_path.is_dir()


@POSIX
def test_ensure_dir_refuses_symlinked_directory(tmp_path, dir_path, fake_mountinfo):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    os.chmod(real, 0o700)
    os.symlink(real, dir_path)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.ensure_dir(mountinfo=fake_mountinfo)


@POSIX
def test_ensure_dir_refuses_wrong_mode_and_owner(ticket_dir, fake_mountinfo, monkeypatch):
    os.chmod(ticket_dir, 0o755)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.ensure_dir(mountinfo=fake_mountinfo)
    os.chmod(ticket_dir, 0o700)
    real_geteuid = os.geteuid
    monkeypatch.setattr(tickets.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.ensure_dir(mountinfo=fake_mountinfo)


@POSIX
def test_write_creates_0600_owner_file_and_logs_only_ids(ticket_dir, fake_mountinfo, caplog):
    caplog.set_level("INFO")
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    names = list(ticket_dir.iterdir())
    assert len(names) == 1
    assert names[0].name.endswith(f"-{USER_ID}-{PUBLIC_ID}")
    st = os.lstat(names[0])
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert st.st_uid == os.geteuid()
    assert st.st_nlink == 1
    assert names[0].read_bytes() == S
    assert "ticket written" in caplog.text
    assert str(USER_ID) in caplog.text and PUBLIC_ID in caplog.text
    assert S.decode() not in caplog.text
    assert names[0].name not in caplog.text
    assert str(names[0]) not in caplog.text


def test_write_strips_one_newline(ticket_dir, fake_mountinfo):
    tickets.write(USER_ID, PUBLIC_ID, S + b"\n", mountinfo=fake_mountinfo)
    assert next(ticket_dir.iterdir()).read_bytes() == S


@pytest.mark.parametrize("secret", [b"", b"short", S.lower(), S + b"Q", S + b"\n\n", S.decode().encode() + b"x"])
def test_write_refuses_bad_secret(ticket_dir, fake_mountinfo, secret):
    with pytest.raises(tickets.TicketError) as exc:
        tickets.write(USER_ID, PUBLIC_ID, secret, mountinfo=fake_mountinfo)
    assert str(exc.value) == "ticket:invalid"
    assert not list(ticket_dir.iterdir())


@pytest.mark.parametrize("public_id", [PUBLIC_ID.upper(), PUBLIC_ID[:-1], PUBLIC_ID + "0", "a" * 31 + "g"])
def test_write_refuses_bad_public_id(ticket_dir, fake_mountinfo, public_id):
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.write(USER_ID, public_id, S, mountinfo=fake_mountinfo)
    assert not list(ticket_dir.iterdir())


def test_write_requires_safe_swap_every_time(ticket_dir, fake_mountinfo, monkeypatch):
    from service import hostcheck
    calls = []
    monkeypatch.setattr(hostcheck, "require_safe_swap", lambda: calls.append(True))
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    assert calls == [True]


def test_write_o_excl_collision_keeps_existing_file(ticket_dir, fake_mountinfo, monkeypatch):
    _fix_name(monkeypatch, "a" * 32)
    path = ticket_dir / _name("a" * 32)
    path.write_bytes(b"keep me")
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    assert path.read_bytes() == b"keep me"


@POSIX
def test_write_refuses_preplanted_symlink(ticket_dir, fake_mountinfo, monkeypatch, tmp_path):
    _fix_name(monkeypatch, "b" * 32)
    target = tmp_path / "target"
    target.write_bytes(S)
    path = ticket_dir / _name("b" * 32)
    os.symlink(target, path)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    assert path.is_symlink()


@POSIX
def test_write_refuses_wrong_owner(ticket_dir, fake_mountinfo, monkeypatch):
    monkeypatch.setattr(tickets, "ensure_dir", lambda **_kw: None)
    real_geteuid = os.geteuid
    monkeypatch.setattr(tickets.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    assert list(ticket_dir.iterdir()) == []


def test_take_reads_then_unlinks(ticket_dir, fake_mountinfo, monkeypatch):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    events = []
    real_read, real_unlink = tickets.os.read, tickets.os.unlink

    def read(fd, size):
        events.append("read")
        return real_read(fd, size)

    def unlink(target):
        events.append("unlink")
        return real_unlink(target)

    monkeypatch.setattr(tickets.os, "read", read)
    monkeypatch.setattr(tickets.os, "unlink", unlink)
    now = datetime.now(UTC)
    assert tickets.take(USER_ID, PUBLIC_ID, now, mountinfo=fake_mountinfo) == S
    assert "read" in events and "unlink" in events
    assert events.index("read") < events.index("unlink")
    assert events[-1] == "unlink"
    assert not path.exists()


def test_take_rechecks_the_ticket_directory(ticket_dir, fake_mountinfo):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    os.chmod(ticket_dir, 0o755)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)


def test_take_missing(ticket_dir, fake_mountinfo):
    with pytest.raises(tickets.TicketError) as exc:
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert str(exc.value) == "ticket:missing"


def test_take_ambiguous_unlinks_everything(ticket_dir, fake_mountinfo, monkeypatch):
    monkeypatch.setattr(tickets.secrets, "token_hex", lambda _n: "a" * 32)
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    monkeypatch.setattr(tickets.secrets, "token_hex", lambda _n: "b" * 32)
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    with pytest.raises(tickets.TicketError) as exc:
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert str(exc.value) == "ticket:ambiguous"
    assert list(ticket_dir.iterdir()) == []


@pytest.mark.parametrize("age_seconds,accepted", [(299, True), (300, True), (301, False), (-1, True)])
def test_take_ttl_boundary(ticket_dir, fake_mountinfo, age_seconds, accepted):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    now = datetime.now(UTC)
    ts = now.timestamp() - age_seconds
    os.utime(path, (ts, ts))
    if accepted:
        assert tickets.take(USER_ID, PUBLIC_ID, now, mountinfo=fake_mountinfo) == S
    else:
        with pytest.raises(tickets.TicketError) as exc:
            tickets.take(USER_ID, PUBLIC_ID, now, mountinfo=fake_mountinfo)
        assert str(exc.value) == "ticket:invalid"
    assert not path.exists()


@pytest.mark.parametrize("age_seconds,accepted", [
    (-5, True), (-6, False), (300, True), (301, False)])
def test_take_ttl_allows_five_seconds_of_clock_slack(ticket_dir, fake_mountinfo, age_seconds, accepted):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    now = datetime.now(UTC)
    ts = now.timestamp() - age_seconds
    os.utime(path, (ts, ts))
    if accepted:
        assert tickets.take(USER_ID, PUBLIC_ID, now, mountinfo=fake_mountinfo) == S
    else:
        with pytest.raises(tickets.TicketError) as exc:
            tickets.take(USER_ID, PUBLIC_ID, now, mountinfo=fake_mountinfo)
        assert str(exc.value) == "ticket:invalid"
    assert not path.exists()


def test_sweep_keeps_files_within_clock_slack(ticket_dir, fake_mountinfo):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    future = next(ticket_dir.iterdir())
    now = datetime.now(UTC)
    ts = now.timestamp() + 5
    os.utime(future, (ts, ts))
    assert tickets.sweep(now, mountinfo=fake_mountinfo) == 0
    assert future.exists()


@POSIX
def test_take_refuses_symlink(ticket_dir, fake_mountinfo, monkeypatch, tmp_path):
    _fix_name(monkeypatch, "c" * 32)
    target = tmp_path / "target"
    target.write_bytes(S)
    path = ticket_dir / _name("c" * 32)
    os.symlink(target, path)
    with pytest.raises(tickets.TicketError) as exc:
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert str(exc.value) == "ticket:invalid"
    assert not path.exists()


@POSIX
def test_take_refuses_hardlink(ticket_dir, fake_mountinfo, tmp_path):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    os.link(path, tmp_path / "other-link")
    with pytest.raises(tickets.TicketError) as exc:
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert str(exc.value) == "ticket:invalid"
    assert not path.exists()


@POSIX
def test_take_refuses_wrong_owner_and_mode(ticket_dir, fake_mountinfo, monkeypatch):
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    real_geteuid = os.geteuid
    monkeypatch.setattr(tickets.os, "geteuid", lambda: real_geteuid() + 1)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert path.exists()
    path.unlink()

    monkeypatch.setattr(tickets.os, "geteuid", real_geteuid)
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    path = next(ticket_dir.iterdir())
    os.chmod(path, 0o644)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    assert not path.exists()


def test_sweep_removes_old_and_nonregular_only(ticket_dir, fake_mountinfo, caplog):
    caplog.set_level("INFO")
    tickets.write(USER_ID, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    fresh = next(ticket_dir.iterdir())
    tickets.write(USER_ID + 1, PUBLIC_ID, S, mountinfo=fake_mountinfo)
    old = next(p for p in ticket_dir.iterdir() if p != fresh)
    now = datetime.now(UTC)
    ts = now.timestamp() - 301
    os.utime(old, (ts, ts))
    if os.name == "posix":
        link = ticket_dir / "random-symlink"
        os.symlink(fresh, link)
        expected = 2
    else:
        expected = 1
    assert tickets.sweep(now, mountinfo=fake_mountinfo) == expected
    assert fresh.exists()
    assert not old.exists()
    assert S.decode() not in caplog.text
    assert fresh.name not in caplog.text
    assert str(expected) in caplog.text


def test_sweep_rechecks_the_ticket_directory(ticket_dir, fake_mountinfo):
    os.chmod(ticket_dir, 0o755)
    with pytest.raises(tickets.TicketError, match="ticket:invalid"):
        tickets.sweep(datetime.now(UTC), mountinfo=fake_mountinfo)


def test_canary_secret_absent_from_errors_and_logs(ticket_dir, fake_mountinfo, caplog):
    caplog.set_level("INFO")
    errors = []
    try:
        tickets.write(USER_ID, "bad", S, mountinfo=fake_mountinfo)
    except tickets.TicketError as exc:
        errors.append(str(exc))
    try:
        tickets.take(USER_ID, PUBLIC_ID, datetime.now(UTC), mountinfo=fake_mountinfo)
    except tickets.TicketError as exc:
        errors.append(str(exc))
    assert errors == ["ticket:invalid", "ticket:missing"]
    assert all(S.decode() not in error for error in errors)
    assert S.decode() not in caplog.text
