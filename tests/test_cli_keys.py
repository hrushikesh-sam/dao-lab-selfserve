"""B-105: `daolab key init/status` keeps the identity on the client machine only."""
import dataclasses
import os
import stat
import subprocess
import sys

import pytest

from service import agecli, cli, config, models

CANARY = "AGE-SECRET-KEY-1" + "Q" * 58
RECIPIENT = "age1" + "q" * 58
FINGERPRINT = "a" * 64


def done(out=b""):
    return subprocess.CompletedProcess([], 0, out, b"")


@pytest.fixture
def key_cli(tmp_path, monkeypatch):
    settings = dataclasses.replace(
        config.settings, identity_file=str(tmp_path / "keys" / "identity.txt"),
        db_path=str(tmp_path / "cli.db"), api_url="http://api.invalid")
    monkeypatch.setattr(config, "settings", settings)
    conn = models.connect(settings.db_path)
    models.init_db(conn)
    conn.close()

    calls, posted = [], []

    def run(argv, **kw):
        args = list(argv)
        calls.append({"argv": args, "env": kw.get("env")})
        if "--version" in args:
            return done(b"v1.1.1\n")
        if args == [settings.age_keygen_bin]:
            return done((CANARY + "\n").encode())
        if args[:3] == [settings.age_keygen_bin, "-y", "-"]:
            return done((RECIPIENT + "\n").encode())
        raise AssertionError(f"unexpected age call: {args}")

    def api(method, path, body=None):
        posted.append((method, path, body))
        if method == "POST":
            return {"id": 1, "fingerprint": FINGERPRINT, "state": "pending"}
        return [{"id": 1, "fingerprint": FINGERPRINT, "state": "pending",
                 "created_at": "2026-09-01T09:00:00Z", "activated_at": None}]

    monkeypatch.setattr(agecli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_api", api)
    return settings, calls, posted


def argv_env_blob(calls):
    return repr([(c["argv"], c["env"]) for c in calls])


def db_dump(settings):
    conn = models.connect(settings.db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_key_init_writes_a_private_file_and_posts_only_the_recipient(key_cli, capsys, caplog):
    caplog.set_level("DEBUG")
    settings, calls, posted = key_cli
    assert cli.cmd_key_init(None) == 0
    path = settings.identity_file
    with open(path, "rb") as fh:
        assert fh.read() == (CANARY + "\n").encode()
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert posted == [("POST", "/keys", {"recipient": RECIPIENT})]
    assert CANARY not in argv_env_blob(calls)
    out = capsys.readouterr()
    assert FINGERPRINT in out.out and "next" in out.out.lower()
    assert CANARY not in out.out and CANARY not in out.err
    assert RECIPIENT not in out.out
    assert CANARY not in caplog.text and CANARY not in db_dump(settings)


def test_key_init_tightens_an_existing_parent_directory(key_cli, monkeypatch):
    settings, _calls, _posted = key_cli
    parent = os.path.dirname(settings.identity_file)
    os.makedirs(parent, exist_ok=True)
    posix = os.name == "posix"
    chmod_calls = []
    real_chmod = os.chmod

    def spy_chmod(path, mode):
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.setattr(cli.os, "chmod", spy_chmod)
    assert cli.cmd_key_init(None) == 0
    assert (parent, 0o700) in chmod_calls
    if posix:
        assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700


def test_key_init_refuses_an_existing_identity_file(key_cli, monkeypatch, capsys):
    settings, _calls, posted = key_cli
    os.makedirs(os.path.dirname(settings.identity_file), exist_ok=True)
    with open(settings.identity_file, "wb") as fh:
        fh.write(b"previous identity")
    monkeypatch.setattr(agecli, "keygen", lambda: pytest.fail("keygen must not run"))
    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_key_init(None)
    assert exit_info.value.code == 1
    assert posted == []
    assert "already exists" in capsys.readouterr().err
    with open(settings.identity_file, "rb") as fh:
        assert fh.read() == b"previous identity"


def test_key_init_expands_user(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    settings = dataclasses.replace(config.settings, identity_file="~/.config/daolab/age-identity.txt",
                                   db_path=str(tmp_path / "cli.db"), api_url="http://api.invalid")
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(agecli, "version", lambda: "v1.1.1")
    monkeypatch.setattr(agecli, "keygen", lambda: ((CANARY + "\n").encode(), RECIPIENT))
    monkeypatch.setattr(cli, "_api", lambda method, path, body=None: {"id": 1, "fingerprint": FINGERPRINT})
    assert cli.cmd_key_init(None) == 0
    assert (home / ".config" / "daolab" / "age-identity.txt").read_bytes() == (CANARY + "\n").encode()


def test_key_init_refuses_a_wrong_age_version_without_printing_key_material(key_cli, monkeypatch, capsys):
    settings, _calls, posted = key_cli
    monkeypatch.setattr(agecli, "version", lambda: (_ for _ in ()).throw(
        agecli.AgeError(agecli.UNSUPPORTED_VERSION, rc=0)))
    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_key_init(None)
    assert exit_info.value.code == 1
    assert not os.path.exists(settings.identity_file)
    assert posted == []
    err = capsys.readouterr().err
    assert agecli.UNSUPPORTED_VERSION in err and CANARY not in err


def test_key_status_prints_the_server_view_only(key_cli, capsys):
    _settings, _calls, posted = key_cli
    assert cli.cmd_key_status(None) == 0
    assert posted == [("GET", "/keys", None)]
    out = capsys.readouterr().out
    assert FINGERPRINT in out and "pending" in out
    assert CANARY not in out and RECIPIENT not in out


def test_key_subcommands_are_wired(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_key_init", lambda args: seen.append("init") or 0)
    monkeypatch.setattr(cli, "cmd_key_status", lambda args: seen.append("status") or 0)
    for command in ("init", "status"):
        monkeypatch.setattr(sys, "argv", ["daolab", "key", command])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0
    assert seen == ["init", "status"]
