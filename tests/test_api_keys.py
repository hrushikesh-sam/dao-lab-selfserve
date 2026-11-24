"""B-105: the keys and approvals HTTP surface, auth boundaries and secret canary."""
import logging

from service import keys, models

CANARY = "AGE-SECRET-KEY-1" + "Q" * 58
RECIPIENT = "age1" + "q" * 58
RECIPIENT_2 = "age1" + "p" * 58
RECIPIENT_3 = "age1" + "z" * 58


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def user_id(conn, name):
    return conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]


def make_active_key(env, username, recipient):
    kid = keys.enroll(env.conn, user_id(env.conn, username), recipient)
    keys.activate(env.conn, kid, env.conn.execute(
        "SELECT * FROM users WHERE username = 'dev-admin'").fetchone())
    return kid


def error_code(resp):
    return resp.json()["error"]["code"]


def test_post_keys_auth_validation_and_duplicate(api_env):
    c, t = api_env.client, api_env.user_token
    assert c.post("/keys", json={"recipient": RECIPIENT}).status_code == 401
    assert c.post("/keys", headers=headers(t), json={}).status_code == 422
    extra = c.post("/keys", headers=headers(t), json={"recipient": RECIPIENT, "identity": CANARY})
    assert extra.status_code == 422 and error_code(extra) == "validation_error"
    bad = c.post("/keys", headers=headers(t), json={"recipient": "age1short"})
    assert bad.status_code == 422 and error_code(bad) == "validation_error"

    first = c.post("/keys", headers=headers(t), json={"recipient": RECIPIENT})
    assert first.status_code == 201
    assert set(first.json()) == {"id", "fingerprint", "state"}
    assert first.json()["state"] == "pending"
    assert first.json()["fingerprint"] == keys.fingerprint(RECIPIENT)
    dup = c.post("/keys", headers=headers(t), json={"recipient": RECIPIENT})
    assert dup.status_code == 409 and error_code(dup) == "key_exists"


def test_get_keys_is_own_only(api_env):
    c = api_env.client
    c.post("/keys", headers=headers(api_env.user_token), json={"recipient": RECIPIENT})
    c.post("/keys", headers=headers(api_env.tokens["bob"]), json={"recipient": RECIPIENT_2})
    rows = c.get("/keys", headers=headers(api_env.user_token)).json()
    assert [r["fingerprint"] for r in rows] == [keys.fingerprint(RECIPIENT)]
    assert set(rows[0]) == {"id", "fingerprint", "state", "created_at", "activated_at"}
    assert rows[0]["activated_at"] is None
    assert c.get("/keys").status_code == 401


def test_admin_keys_list_and_activate(api_env):
    c = api_env.client
    assert c.get("/admin/keys", headers=headers(api_env.user_token)).status_code == 403
    invalid = c.get("/admin/keys?state=unknown", headers=headers(api_env.admin_token))
    assert invalid.status_code == 422 and error_code(invalid) == "validation_error"
    kid = make_active_key(api_env, "alice", RECIPIENT)
    pending = c.post("/keys", headers=headers(api_env.tokens["bob"]),
                     json={"recipient": RECIPIENT_2}).json()
    rows = c.get("/admin/keys?state=pending", headers=headers(api_env.admin_token)).json()
    assert [r["id"] for r in rows] == [pending["id"]]
    assert rows[0]["user_id"] == user_id(api_env.conn, "bob")
    assert [r["id"] for r in c.get("/admin/keys?state=active",
                                   headers=headers(api_env.admin_token)).json()] == [kid]

    activated = c.post(f"/admin/keys/{pending['id']}/activate", headers=headers(api_env.admin_token))
    assert activated.status_code == 200 and activated.json()["state"] == "active"
    assert activated.json()["activated_by"] == user_id(api_env.conn, "dev-admin")
    missing = c.post("/admin/keys/99999/activate", headers=headers(api_env.admin_token))
    assert missing.status_code == 404 and error_code(missing) == "not_found"
    own = c.post("/keys", headers=headers(api_env.admin_token), json={"recipient": RECIPIENT_3}).json()
    self_activate = c.post(f"/admin/keys/{own['id']}/activate", headers=headers(api_env.admin_token))
    assert self_activate.status_code == 409 and error_code(self_activate) == "invalid_state"


def test_approval_endpoints_and_owner_visibility(api_env):
    c = api_env.client
    kid = make_active_key(api_env, "alice", RECIPIENT)
    other = make_active_key(api_env, "bob", RECIPIENT_2)
    second_token = api_env.add_user("second-admin", role="admin")
    body = {"action": "lost_key_reset", "target_ref": f"key:{kid}"}

    assert c.post("/admin/approvals", json=body, headers=headers(api_env.user_token)).status_code == 403
    created = c.post("/admin/approvals", json=body, headers=headers(api_env.admin_token))
    assert created.status_code == 201
    aid = created.json()["id"]
    assert created.json()["state"] == "pending"
    duplicate = c.post("/admin/approvals", json=body, headers=headers(second_token))
    assert duplicate.status_code == 409 and error_code(duplicate) == "approval_open"
    other_reset = c.post("/admin/approvals",
                         json={"action": "lost_key_reset", "target_ref": f"key:{other}"},
                         headers=headers(api_env.admin_token))
    assert other_reset.status_code == 201

    listed = c.get("/admin/approvals?state=pending", headers=headers(api_env.admin_token)).json()
    assert [r["id"] for r in listed] == [aid, other_reset.json()["id"]]
    assert c.get("/admin/approvals", headers=headers(api_env.user_token)).status_code == 403
    same_admin = c.post(f"/admin/approvals/{aid}/approve", headers=headers(api_env.admin_token))
    assert same_admin.status_code == 409 and error_code(same_admin) == "invalid_state"
    approved = c.post(f"/admin/approvals/{aid}/approve", headers=headers(second_token))
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert approved.json()["execute_after"] is not None
    rejected = c.post(f"/admin/approvals/{other_reset.json()['id']}/reject", headers=headers(second_token))
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"

    own = c.get("/approvals", headers=headers(api_env.user_token)).json()
    assert [r["id"] for r in own] == [aid]
    bob_own = c.get("/approvals", headers=headers(api_env.tokens["bob"])).json()
    assert [r["id"] for r in bob_own] == [other_reset.json()["id"]]
    assert c.get("/approvals").status_code == 401


def test_get_approvals_excludes_discards_and_other_users(api_env):
    c = api_env.client
    kid = make_active_key(api_env, "alice", RECIPIENT)
    reset = c.post("/admin/approvals",
                   json={"action": "lost_key_reset", "target_ref": f"key:{kid}"},
                   headers=headers(api_env.admin_token)).json()["id"]
    discard = c.post("/admin/approvals",
                     json={"action": "home_discard", "target_ref": "setup:tb-01"},
                     headers=headers(api_env.admin_token)).json()["id"]
    visible = c.get("/approvals", headers=headers(api_env.user_token)).json()
    assert [r["id"] for r in visible] == [reset]
    assert discard not in [r["id"] for r in visible]
    assert c.get("/approvals", headers=headers(api_env.tokens["bob"])).json() == []


def test_canary_identity_never_reaches_db_response_or_logs(api_env, caplog):
    caplog.set_level(logging.DEBUG)
    c = api_env.client
    resp = c.post("/keys", headers=headers(api_env.user_token),
                  json={"recipient": RECIPIENT, "identity": CANARY})
    assert resp.status_code == 422
    good = c.post("/keys", headers=headers(api_env.user_token), json={"recipient": RECIPIENT})
    assert good.status_code == 201
    dump = "\n".join(api_env.conn.iterdump())
    for blob in (resp.text, good.text, dump, caplog.text):
        assert CANARY not in blob
    assert "identity" not in good.json()
