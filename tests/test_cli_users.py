"""`daolab add-user --rotate`, which scripts/run/dev.sh runs for its test users on every start: a
new token for the same user. Their reservations stay - deleting them on a restart orphaned a live
one, its app still running on the board with nothing left to clean it up."""
import argparse
import dataclasses
import hashlib

import pytest

from service import cli, config, models
from tests.conftest import T, TB01_CAPS, TLS_REQ, mk_res, mk_setup, mk_usecase


@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    path = str(tmp_path / "daolab.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, db_path=path))
    conn = models.connect(path)
    models.init_db(conn)
    return conn


def add_user(capsys, key_path, rotate, role="user"):
    args = argparse.Namespace(username="alice", pubkey_path=str(key_path), role=role, rotate=rotate)
    assert cli.cmd_add_user(args) == 0
    return capsys.readouterr().out.split("token: ", 1)[1].splitlines()[0].strip()


def user_for(conn, token):
    return models.user_by_token_hash(conn, hashlib.sha256(token.encode()).hexdigest())


def test_rotate_gives_a_new_token_and_keeps_the_users_reservations(cli_db, capsys, tmp_path):
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAold alice@old\n")
    old = add_user(capsys, key, rotate=False)
    uid = user_for(cli_db, old)["id"]
    rid = mk_res(cli_db, mk_setup(cli_db, "tb-01", TB01_CAPS), mk_usecase(cli_db, "vpp", TLS_REQ),
                 T(10), T(11), state="active", user_id=uid)
    models.create_job(cli_db, rid, "deploy")

    key.write_text("ssh-ed25519 AAAAnew alice@new\n")
    new = add_user(capsys, key, rotate=True, role="admin")

    assert user_for(cli_db, old) is None                       # the old token no longer works
    user = user_for(cli_db, new)
    assert (user["id"], user["role"], user["ssh_pubkey"]) == (uid, "admin", "ssh-ed25519 AAAAnew alice@new")
    res = models.reservation_by_id(cli_db, rid)
    assert res["user_id"] == uid and res["state"] == "active"  # still hers, still cleaned at its end
    assert [j["kind"] for j in models.jobs_for_reservation(cli_db, rid)] == ["deploy"]


def test_rotate_creates_a_missing_user_and_a_repeat_without_it_is_refused(cli_db, capsys, tmp_path):
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAkey alice@laptop\n")
    assert user_for(cli_db, add_user(capsys, key, rotate=True))["username"] == "alice"
    with pytest.raises(SystemExit):
        cli.cmd_add_user(argparse.Namespace(username="alice", pubkey_path=str(key), role="user",
                                            rotate=False))
    assert "duplicate user: alice" in capsys.readouterr().err


# ---- B-101 (mistakes.md J13): tokens expire and can be revoked ---------------------------

def test_a_new_token_expires_after_the_ttl(cli_db, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, token_ttl_days=90))
    monkeypatch.setattr(models, "utcnow", lambda: "2026-11-04T10:00:00Z")
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAkey alice@laptop\n")
    args = argparse.Namespace(username="alice", pubkey_path=str(key), role="user", rotate=False)
    assert cli.cmd_add_user(args) == 0
    out = capsys.readouterr().out
    token = out.split("token: ", 1)[1].splitlines()[0]
    assert "expires: 2027-02-02T10:00:00Z" in out
    assert user_for(cli_db, token)["token_expires_at"] == "2027-02-02T10:00:00Z"


def test_ttl_zero_means_the_token_never_expires(cli_db, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, token_ttl_days=0))
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAkey alice@laptop\n")
    token = add_user(capsys, key, rotate=False)
    assert user_for(cli_db, token)["token_expires_at"] is None


def test_revoke_token_ends_the_token_and_keeps_the_user(cli_db, capsys, tmp_path):
    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAkey alice@laptop\n")
    token = add_user(capsys, key, rotate=False)
    uid = user_for(cli_db, token)["id"]
    assert cli.cmd_revoke_token(argparse.Namespace(username="alice")) == 0
    assert "revoked: alice" in capsys.readouterr().out
    assert user_for(cli_db, token) is None                         # the old token is unknown now
    row = cli_db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["username"] == "alice" and row["token_expires_at"] is not None
    assert user_for(cli_db, add_user(capsys, key, rotate=True))["id"] == uid   # re-issue works
    with pytest.raises(SystemExit):
        cli.cmd_revoke_token(argparse.Namespace(username="nobody"))
    assert "unknown user: nobody" in capsys.readouterr().err
