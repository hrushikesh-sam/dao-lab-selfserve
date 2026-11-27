"""B-106: the challenge and owner-cancel HTTP surface and nonce canaries."""
import base64
import dataclasses
import logging
import os
import stat
import subprocess
import sys

import pytest

from service import agecli, approvals, challenges, cli, config, keys, models

CREATED = "2026-09-01T09:00:00Z"
AFTER = "2026-09-02T09:00:00Z"
NONCE = b"NONCE-CANARY-" + bytes(range(19))
ARMOR = b"-----BEGIN AGE ENCRYPTED FILE-----\nZmFrZQ==\n-----END AGE ENCRYPTED FILE-----\n"
CANARY = "NONCE-CANARY-" + "z" * 19


def recipient(tag="q"):
    return "age1" + "q" * 57 + tag


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def error_code(resp):
    return resp.json()["error"]["code"]


def user_id(conn, name):
    return conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]


def setup_id(conn, name="tb-01"):
    return conn.execute("SELECT id FROM setups WHERE name = ?", (name,)).fetchone()[0]


def admin(conn, name="dev-admin"):
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(models, "utcnow", lambda: CREATED)
    monkeypatch.setattr(agecli, "encrypt_to", lambda to, data: ARMOR)
    monkeypatch.setattr(challenges.secrets, "token_bytes", lambda size: NONCE)
    return base64.b64encode(NONCE).decode("ascii")


def active_key(env, name="alice", tag="q"):
    kid = keys.enroll(env.conn, user_id(env.conn, name), recipient(tag))
    keys.activate(env.conn, kid, admin(env.conn))
    return kid


def snapshot(env, owner, setup, key_id, public_id):
    env.conn.execute(
        """INSERT INTO snapshots (public_id, user_id, setup_id, key_id, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'saved', ?, ?)""",
        (public_id, owner, setup, key_id, CREATED, CREATED))
    return env.conn.execute("SELECT id FROM snapshots WHERE public_id = ?", (public_id,)).fetchone()[0]


def approved_reset(env, key_id):
    aid = approvals.request(env.conn, "lost_key_reset", f"key:{key_id}", admin(env.conn))
    second = env.conn.execute("SELECT id FROM users WHERE username = 'second-admin'").fetchone()
    if second is None:
        from tests.conftest import mk_user
        mk_user(env.conn, "second-admin", role="admin")
    approvals.decide(env.conn, aid, admin(env.conn, "second-admin"), True, CREATED)
    return aid


def issue(env, purpose, target, token=None):
    return env.client.post("/challenges", headers=headers(token or env.user_token),
                           json={"purpose": purpose, "target_ref": target})


def cancel(env, approval_id, challenge_id, nonce_b64, token=None):
    return env.client.post(f"/approvals/{approval_id}/cancel",
                           headers=headers(token or env.user_token),
                           json={"challenge_id": challenge_id, "nonce_b64": nonce_b64})


def dump(env):
    return "\n".join(env.conn.iterdump())


def test_post_challenges_auth_response_and_nonce_canary(api_env, frozen, caplog):
    caplog.set_level(logging.DEBUG)
    c = api_env.client
    assert c.post("/challenges", json={"purpose": "delete", "target_ref": "f" * 32}).status_code == 401
    bad = c.post("/challenges", headers=headers(api_env.user_token), json={})
    assert bad.status_code == 422 and error_code(bad) == "validation_error"
    assert c.post("/challenges", headers=headers(api_env.user_token),
                  json={"purpose": "delete", "target_ref": "f" * 32, "nonce": CANARY}).status_code == 422

    kid = active_key(api_env)
    pid = "a" * 32
    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, pid)
    result = issue(api_env, "delete", pid)
    assert result.status_code == 200
    assert set(result.json()) == {"challenge_id", "expires_at", "ciphertext"}
    assert result.json()["expires_at"] == "2026-09-01T09:10:00Z"
    assert result.json()["ciphertext"].startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    row = api_env.conn.execute("SELECT * FROM key_challenges WHERE id = ?",
                               (result.json()["challenge_id"],)).fetchone()
    assert row["nonce_sha256"] == challenges.digest(NONCE, "delete", pid, kid,
                                                    user_id(api_env.conn, "alice"),
                                                    "2026-09-01T09:10:00Z")
    for blob in (result.text, dump(api_env), caplog.text, frozen):
        assert CANARY not in blob
    assert CANARY.encode("ascii") not in caplog.text.encode("utf-8")


def test_owner_cancel_endpoint_clears_holds_and_audits(api_env, frozen, caplog):
    caplog.set_level(logging.DEBUG)
    kid = active_key(api_env)
    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, "b" * 32)
    aid = approved_reset(api_env, kid)
    challenge = issue(api_env, "cancel_delete", f"approval:{aid}")
    assert challenge.status_code == 200
    cid = challenge.json()["challenge_id"]

    result = cancel(api_env, aid, cid, frozen)
    assert result.status_code == 200
    assert result.json()["state"] == "cancelled"
    row = api_env.conn.execute("SELECT * FROM admin_approvals WHERE id = ?", (aid,)).fetchone()
    assert (row["state"], row["executed_at"]) == ("cancelled", None)
    held = api_env.conn.execute("SELECT * FROM snapshots WHERE hold_approval_id IS NULL").fetchone()
    assert (held["delete_after"], held["delete_requested_by"]) == (None, None)
    assert api_env.conn.execute("SELECT consumed_at FROM key_challenges WHERE id = ?",
                                (cid,)).fetchone()[0] == CREATED
    audit = api_env.conn.execute("SELECT * FROM audit_events WHERE action = 'approval_cancel'").fetchone()
    assert audit["actor"] == "alice" and audit["object_id"] == aid
    assert models.from_json(audit["detail"])["cleared"] == 1
    for blob in (result.text, dump(api_env), caplog.text):
        assert CANARY not in blob


def test_owner_cancel_endpoint_replay_and_state_refusal(api_env, frozen):
    kid = active_key(api_env)
    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, "c" * 32)
    aid = approved_reset(api_env, kid)
    cid = issue(api_env, "cancel_delete", f"approval:{aid}").json()["challenge_id"]
    assert cancel(api_env, aid, cid, frozen).status_code == 200
    replay = cancel(api_env, aid, cid, frozen)
    assert replay.status_code == 409 and error_code(replay) == "challenge:used"

    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, "d" * 32)
    second = approved_reset(api_env, kid)
    cid = issue(api_env, "cancel_delete", f"approval:{second}").json()["challenge_id"]
    api_env.conn.execute("UPDATE admin_approvals SET state = 'rejected' WHERE id = ?", (second,))
    refused = cancel(api_env, second, cid, frozen)
    assert refused.status_code == 409 and error_code(refused) == "invalid_state"
    assert api_env.conn.execute("SELECT consumed_at FROM key_challenges WHERE id = ?",
                                (cid,)).fetchone()[0] is None


def test_validation_errors_never_echo_the_nonce(api_env, frozen):
    kid = active_key(api_env)
    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, "e" * 32)
    aid = approved_reset(api_env, kid)
    cid = issue(api_env, "cancel_delete", f"approval:{aid}").json()["challenge_id"]
    for body in ({"challenge_id": "not-an-int", "nonce_b64": {"secret": CANARY}},
                 {"challenge_id": cid, "nonce_b64": [CANARY]},
                 {"challenge_id": cid, "nonce_b64": frozen, "nonce": CANARY},
                 {"challenge_id": cid}):
        resp = api_env.client.post(f"/approvals/{aid}/cancel",
                                   headers=headers(api_env.user_token), json=body)
        assert resp.status_code == 422 and error_code(resp) == "validation_error"
        assert CANARY not in resp.text
        assert CANARY not in resp.json()["error"]["message"]
        assert frozen not in resp.json()["error"]["message"]
    assert api_env.conn.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'approval_cancel'").fetchone()[0] == 0


def test_cancel_is_owner_only_and_needs_auth(api_env, frozen):
    kid = active_key(api_env)
    snapshot(api_env, user_id(api_env.conn, "alice"), setup_id(api_env.conn), kid, "f" * 32)
    aid = approved_reset(api_env, kid)
    cid = issue(api_env, "cancel_delete", f"approval:{aid}").json()["challenge_id"]
    assert api_env.client.post(f"/approvals/{aid}/cancel",
                               json={"challenge_id": cid, "nonce_b64": frozen}).status_code == 401
    foreign = cancel(api_env, aid, cid, frozen, token=api_env.tokens["bob"])
    assert foreign.status_code in (403, 404, 409)
    assert api_env.conn.execute("SELECT state FROM admin_approvals WHERE id = ?", (aid,)).fetchone()[0] == "approved"


def test_cli_answer_decrypts_in_a_0600_temp_file_without_leaking_the_nonce(
        tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    identity = b"AGE-SECRET-KEY-1" + b"Q" * 58 + b"\n"
    path = tmp_path / "identity.txt"
    path.write_bytes(identity)
    settings = dataclasses.replace(config.settings, identity_file=str(path),
                                   api_url="http://api.invalid")
    monkeypatch.setattr(config, "settings", settings)
    calls, modes, posted, chmod_calls = [], [], [], []
    real_chmod = os.chmod

    def spy_chmod(target, mode):
        chmod_calls.append((str(target), mode))
        return real_chmod(target, mode)

    monkeypatch.setattr(cli.os, "chmod", spy_chmod)

    def run(argv, **kw):
        args = list(argv)
        calls.append({"argv": args, "kw": kw})
        if "--version" in args:
            return subprocess.CompletedProcess([], 0, b"v1.1.1\n", b"")
        if args[:4] == [settings.age_bin, "-d", "-i", "-"]:
            modes.append(stat.S_IMODE(os.stat(args[4]).st_mode))
            assert kw.get("input", b"").strip() == identity.strip()
            return subprocess.CompletedProcess([], 0, NONCE, b"")
        raise AssertionError(f"unexpected age call: {args}")

    def api(method, route, body=None):
        posted.append((method, route, body))
        if route == "/challenges":
            return {"challenge_id": 7, "expires_at": "2026-09-01T09:10:00Z",
                    "ciphertext": ARMOR.decode("ascii")}
        return {"state": "cancelled"}

    monkeypatch.setattr(agecli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_api", api)
    challenge_id, nonce_b64 = cli.answer("cancel_delete", "approval:3")
    assert (challenge_id, base64.b64decode(nonce_b64)) == (7, NONCE)
    assert posted[0] == ("POST", "/challenges", {"purpose": "cancel_delete",
                                                 "target_ref": "approval:3"})
    assert len(posted) == 1
    assert any(mode == 0o600 for _, mode in chmod_calls)
    if os.name == "posix":
        assert modes == [0o600]
    decrypt_path = next(c["argv"][4] for c in calls if "-d" in c["argv"])
    assert not os.path.exists(decrypt_path)
    call_blob = repr([(c["argv"], c["kw"].get("env")) for c in calls])
    out = capsys.readouterr()
    assert CANARY not in call_blob and CANARY not in caplog.text
    assert CANARY not in out.out and CANARY not in out.err
    assert NONCE.decode("ascii") not in call_blob


def test_cli_approvals_cancel_command_wired(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "cmd_approvals_cancel",
                        lambda args: seen.append(args.approval_id) or 0)
    monkeypatch.setattr(sys, "argv", ["daolab", "approvals", "cancel", "12"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0 and seen == [12]
    assert capsys.readouterr().out == ""


def test_cli_approvals_cancel_posts_the_challenge_answer(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(cli, "answer", lambda purpose, target: (7, base64.b64encode(NONCE).decode()))
    monkeypatch.setattr(cli, "_api", lambda method, route, body=None: posted.append(
        (method, route, body)) or {"state": "cancelled"})
    assert cli.cmd_approvals_cancel(type("Args", (), {"approval_id": 3})()) == 0
    assert posted == [("POST", "/approvals/3/cancel", {"challenge_id": 7,
                                                       "nonce_b64": base64.b64encode(NONCE).decode()})]
    assert capsys.readouterr().out.strip() == "cancelled: 3"
