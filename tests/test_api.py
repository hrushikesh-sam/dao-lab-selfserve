"""API test plan from plan/02-backend-api.md — HTTP wiring over the scheduler,
plus the admin user-management endpoints. No hardware, no engine, no ansible."""
import dataclasses
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = timezone.utc


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    from service import api, catalog, config, models
    settings = dataclasses.replace(
        config.settings,
        db_path=str(tmp_path / "test.db"),
        internal_secret="test-secret",
        dashboard_dir=os.path.join(REPO, "dashboard"),
        usecases_dir=os.path.join(REPO, "usecases"),
        setups_dir=os.path.join(REPO, "setups"),
        inventory=os.path.join(REPO, "inventory", "hosts.yml"),
    )
    monkeypatch.setattr(config, "settings", settings)
    conn = models.connect(settings.db_path)
    models.init_db(conn)
    catalog.seed_usecases(conn)
    catalog.sync(conn, settings.usecases_dir, settings.setups_dir, settings.inventory)

    def add_user(username, role="user"):
        token = f"tok-{username}"
        models.insert(conn, "users", {
            "username": username, "role": role,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "ssh_pubkey": "ssh-ed25519 AAAA test"})
        return token

    user_token = add_user("hrushi")
    admin_token = add_user("hrushi-admin", role="admin")
    client = TestClient(api.app, raise_server_exceptions=False)
    yield type("Env", (), {
        "client": client, "conn": conn, "models": models,
        "user_token": user_token, "admin_token": admin_token, "add_user": staticmethod(add_user),
        "T": datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1),
    })
    conn.close()


def H(token):
    return {"Authorization": f"Bearer {token}"}


def assert_error(resp, status, code):
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert err["code"] == code
    assert set(err) == {"code", "message", "detail"}
    return err


def reserve(env, token, **over):
    body = {"usecase": "tls-proxy-nginx", "start_at": iso(env.T), "hours": 2, **over}
    return env.client.post("/reservations", json=body, headers=H(token))


def test_1_bad_token_unauthorized(env):
    assert_error(env.client.get("/whoami"), 401, "unauthorized")
    assert_error(env.client.get("/whoami", headers=H("garbage")), 401, "unauthorized")


def test_2_role_gates(env):
    for path in ("/pools", "/setups", "/reservations?all=1", "/users"):
        assert_error(env.client.get(path, headers=H(env.user_token)), 403, "forbidden")
        assert env.client.get(path, headers=H(env.admin_token)).status_code == 200


def test_3_catalog(env):
    rows = env.client.get("/usecases", headers=H(env.user_token)).json()
    assert len(rows) == 17                     # 15 README rows + diy + smartnic
    by_name = {r["name"]: r for r in rows}
    assert by_name["tls-proxy-nginx"]["available"] is True
    assert by_name["tls-proxy-nginx"]["status"] == "active"
    assert by_name["virtio-l2fwd"] == {**by_name["virtio-l2fwd"], "status": "planned",
                                       "available": False, "reason": "no_yaml"}
    assert by_name["diy"]["status"] == "active" and by_name["diy"]["available"] is True
    tls = by_name["tls-proxy-nginx"]
    assert tls["doc_url"].endswith("tls-proxy-nginx.html") and tls["doc_summary"]
    assert by_name["virtio-l2fwd"]["doc_url"].endswith("virtio-l2fwd.html")
    assert by_name["virtio-l2fwd"]["setups"] == []          # planned rows embed no testbed
    assert [s["name"] for s in tls["setups"]] == ["tb-01"]
    assert tls["setups"][0]["diagram"].startswith("graph") and tls["setups"][0]["members"]
    assert tls["roles"]["proxy"]["on"] == "dpu" and tls["requires"] == ["octeon-cn106", "e810", "wire"]
    for uc in ("vpp", "smartnic"):
        assert by_name[uc]["status"] == "active" and by_name[uc]["available"] is True


def test_4_catalog_unmet_capability(env):
    env.conn.execute("DELETE FROM resources")
    env.conn.execute("DELETE FROM reservations")
    env.conn.execute("DELETE FROM setups")
    rows = env.client.get("/usecases", headers=H(env.user_token)).json()
    tls = next(r for r in rows if r["name"] == "tls-proxy-nginx")
    assert tls["reason"] == "unmet_capability" and tls["missing"]


def test_5_reserve_simple(env):
    r = reserve(env, env.user_token)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "queued" and body["shifted"] is False and body["vms"] == []
    assert body["setup"] == "tb-01" and body["user"] == "hrushi"


def test_6_overlap_shifts(env):               # Phase 2 checkpoint
    first = reserve(env, env.user_token).json()
    second = reserve(env, env.user_token).json()
    assert second["shifted"] is True
    expected = iso(env.T + timedelta(hours=2, minutes=30))   # end + 10 cleanup + 5 lead = :15, on the 30 min grid
    assert second["start_at"] == expected
    assert first["end_at"] == iso(env.T + timedelta(hours=2))


def test_6b_cleanup_min_setting_reaches_the_scheduler(env, monkeypatch):   # B-11
    from service import config
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, cleanup_min=30))
    reserve(env, env.user_token)
    second = reserve(env, env.user_token).json()
    assert second["start_at"] == iso(env.T + timedelta(hours=3))   # 30 cleanup + 5 lead = :35 -> grid 3:00


def test_6c_slot_min_setting_reaches_the_scheduler(env, monkeypatch):   # B-12
    from service import config
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, slot_min=60))
    reserve(env, env.user_token)
    second = reserve(env, env.user_token).json()
    assert second["start_at"] == iso(env.T + timedelta(hours=3))   # 2:15 rounded up to the hour
    carol = env.add_user("carol")              # fresh user: B-15 caps a user at two future books
    off_grid = reserve(env, carol, start_at=iso(env.T + timedelta(minutes=10))).json()
    assert off_grid["start_at"].endswith(":00:00Z")                # asap snaps to the hour, never :10


def test_7_unmet_capability_names_missing(env):   # Phase 2 checkpoint
    env.models.insert(env.conn, "usecases", {
        "name": "uc-gpu", "description": "x", "variant": "v",
        "requires": '["gpu","octeon-cn106"]', "roles": "{}", "verify": "{}",
        "yaml_path": "usecases/uc-gpu.yml"})
    err = assert_error(reserve(env, env.user_token, usecase="uc-gpu"), 409, "unmet_capability")
    assert "gpu" in err["message"] and err["detail"]["missing"] == ["gpu"]
    assert err["detail"]["closest_setup"] == "tb-01"


def test_8_reserve_error_matrix(env):
    assert_error(reserve(env, env.user_token, usecase="nope"), 404, "not_found")
    assert_error(reserve(env, env.user_token, usecase="ovs-offload"), 409, "usecase_unavailable")
    assert_error(reserve(env, env.user_token, hours=0), 422, "validation_error")
    assert_error(reserve(env, env.user_token, setup="tb-01"), 422, "validation_error")
    assert_error(reserve(env, env.user_token, usecase="diy", setup="tb-01"), 422, "validation_error")
    err = assert_error(reserve(env, env.user_token, usecase="diy", setup="tb-01",
                               templates={}), 422, "validation_error")
    assert "tb-01-xcp" in err["message"]


def test_9_reserve_diy(env):
    r = reserve(env, env.user_token, usecase="diy", setup="tb-01",
                templates={"tb-01-xcp": "tpl-ubuntu2204-dao"})
    assert r.status_code == 201, r.text
    assert r.json()["diy"] is True


def test_10_ownership_and_order(env):
    other_token = env.add_user("other")
    r1 = reserve(env, env.user_token).json()
    r2 = reserve(env, env.user_token).json()
    rid = r1["id"]
    assert env.client.get(f"/reservations/{rid}", headers=H(env.user_token)).status_code == 200
    assert_error(env.client.get(f"/reservations/{rid}", headers=H(other_token)), 403, "forbidden")
    assert env.client.get(f"/reservations/{rid}", headers=H(env.admin_token)).status_code == 200
    mine = env.client.get("/reservations", headers=H(env.user_token)).json()
    assert [m["id"] for m in mine] == [r2["id"], r1["id"]]           # newest first
    assert_error(env.client.get("/reservations/99999", headers=H(env.user_token)), 404, "not_found")


def test_11_cancel_matrix(env):
    rid = reserve(env, env.user_token).json()["id"]
    r = env.client.delete(f"/reservations/{rid}", headers=H(env.user_token))
    assert r.json() == {"id": rid, "state": "released"}
    rid2 = reserve(env, env.user_token).json()["id"]
    env.models.update(env.conn, "reservations", rid2, {"state": "ready"})
    assert env.client.delete(f"/reservations/{rid2}", headers=H(env.user_token)).json()["state"] == "cleaning"
    rid3 = reserve(env, env.user_token).json()["id"]
    env.models.update(env.conn, "reservations", rid3, {"state": "provisioning"})
    assert_error(env.client.delete(f"/reservations/{rid3}", headers=H(env.user_token)),
                 409, "invalid_state")


def _mk_vm(env, rid, role="backend"):
    return env.models.insert(env.conn, "vms", {
        "reservation_id": rid, "role": role, "host": "tb-01-xcp",
        "template": "tpl-ubuntu2204-dao", "name": f"daolab-r{rid}-{role}"})


def test_12_vm_ip_callback(env, monkeypatch):
    rid = reserve(env, env.user_token).json()["id"]
    _mk_vm(env, rid)
    body = {"reservation_id": rid, "role": "backend", "ip": "10.20.0.21"}
    assert_error(env.client.post("/internal/vm-ip", json=body,
                                 headers={"X-Daolab-Secret": "wrong"}), 401, "unauthorized")
    from service import config
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, internal_secret=""))
    assert_error(env.client.post("/internal/vm-ip", json=body,
                                 headers={"X-Daolab-Secret": "test-secret"}), 401, "unauthorized")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, internal_secret="test-secret"))
    assert_error(env.client.post("/internal/vm-ip",
                                 json={**body, "role": "nope"},
                                 headers={"X-Daolab-Secret": "test-secret"}), 404, "not_found")
    ok = env.client.post("/internal/vm-ip", json=body, headers={"X-Daolab-Secret": "test-secret"})
    assert ok.json() == {"ok": True}
    obj = env.client.get(f"/reservations/{rid}", headers=H(env.user_token)).json()
    vm = obj["vms"][0]
    assert vm["ip"] == "10.20.0.21" and vm["ssh"] == "ssh hrushi@10.20.0.21"


def test_13_callback_idempotent(env):
    rid = reserve(env, env.user_token).json()["id"]
    _mk_vm(env, rid)
    body = {"reservation_id": rid, "role": "backend", "ip": "10.20.0.21"}
    for _ in range(2):
        assert env.client.post("/internal/vm-ip", json=body,
                               headers={"X-Daolab-Secret": "test-secret"}).status_code == 200


def test_14_dashboards_served(env):
    for path in ("/", "/user.html", "/admin.html"):
        r = env.client.get(path)
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text


# ---- admin user management (this iteration's addition) ----------------------

def test_15_users_list(env):
    rows = env.client.get("/users", headers=H(env.admin_token)).json()
    by_name = {r["username"]: r for r in rows}
    assert by_name["hrushi"]["enabled"] is True
    assert by_name["hrushi-admin"]["role"] == "admin"
    reserve(env, env.user_token)
    rows = env.client.get("/users", headers=H(env.admin_token)).json()
    assert next(r for r in rows if r["username"] == "hrushi")["active_reservations"] == 1


def test_16_disable_user(env):
    uid = env.conn.execute("SELECT id FROM users WHERE username='hrushi'").fetchone()["id"]
    r = env.client.patch(f"/users/{uid}", json={"enabled": False}, headers=H(env.admin_token))
    assert r.json()["enabled"] is False
    err = assert_error(env.client.get("/whoami", headers=H(env.user_token)), 403, "forbidden")
    assert err["detail"]["reason"] == "user_disabled"
    r = env.client.patch(f"/users/{uid}", json={"enabled": True}, headers=H(env.admin_token))
    assert r.json()["enabled"] is True
    assert env.client.get("/whoami", headers=H(env.user_token)).status_code == 200


def test_17_disable_guards(env):
    admin_id = env.conn.execute("SELECT id FROM users WHERE username='hrushi-admin'").fetchone()["id"]
    assert_error(env.client.patch(f"/users/{admin_id}", json={"enabled": False},
                                  headers=H(env.admin_token)), 409, "invalid_state")
    assert_error(env.client.patch("/users/9999", json={"enabled": False},
                                  headers=H(env.admin_token)), 404, "not_found")
    uid = env.conn.execute("SELECT id FROM users WHERE username='hrushi'").fetchone()["id"]
    assert_error(env.client.patch(f"/users/{uid}", json={"enabled": False},
                                  headers=H(env.user_token)), 403, "forbidden")


def test_18_reservation_diagram_and_credentials(env):
    rid = reserve(env, env.user_token).json()["id"]
    for role, host, tpl, name, ip in [
            ("proxy", "dpu-01", None, None, "10.20.0.10"),
            ("backend", "tb-01-xcp", "tpl-ubuntu2204-dao", f"daolab-r{rid}-backend", "10.20.0.21"),
            ("client", "tb-01-xcp", "tpl-ubuntu2204-dao", f"daolab-r{rid}-client", "10.20.0.31")]:
        env.models.insert(env.conn, "vms", {"reservation_id": rid, "role": role, "host": host,
                                            "template": tpl, "name": name, "ip": ip})
    obj = env.client.get(f"/reservations/{rid}", headers=H(env.user_token)).json()
    vms = {v["role"]: v for v in obj["vms"]}
    assert vms["backend"]["username"] == "hrushi" and vms["backend"]["auth"] == "ssh-key"
    assert vms["backend"]["ssh"] == "ssh hrushi@10.20.0.21"
    assert vms["proxy"]["username"] is None and vms["proxy"]["ssh"] is None
    d = obj["diagram"]
    assert d.startswith("graph LR")
    assert "ssh hrushi@10.20.0.31" in d              # client VM credentials in the diagram
    assert "upstream · sdp" in d                     # proxy -> backend edge
    assert "e810-vf" in d                            # client -> proxy edge
    queued = reserve(env, env.user_token).json()
    assert queued["diagram"] is None                 # no vms yet -> no diagram


def test_19_owner_only_credentials(env):
    """B-54: ssh lines + VM/DPU IPs are returned only to the owner or an admin."""
    alice = env.add_user("alice")
    bob = env.add_user("bob")
    rid = reserve(env, alice).json()["id"]
    for role, host, tpl, name, ip in [
            ("proxy", "dpu-01", None, None, "10.20.0.10"),
            ("backend", "tb-01-xcp", "tpl-ubuntu2204-dao", f"daolab-r{rid}-backend", "10.20.0.21")]:
        env.models.insert(env.conn, "vms", {"reservation_id": rid, "role": role, "host": host,
                                            "template": tpl, "name": name, "ip": ip})
    # the owner sees the credentials
    obj = env.client.get(f"/reservations/{rid}", headers=H(alice)).json()
    vms = {v["role"]: v for v in obj["vms"]}
    assert vms["backend"]["ssh"] == "ssh alice@10.20.0.21"
    assert "10.20.0.10" in obj["diagram"]
    # a non-owner is refused the object outright, with no IPs in the error body
    err = assert_error(env.client.get(f"/reservations/{rid}", headers=H(bob)), 403, "forbidden")
    assert "10.20.0." not in str(err)
    # a non-owner's own listing never contains someone else's rows (or IPs)
    assert env.client.get("/reservations", headers=H(bob)).json() == []
    # an admin sees them in the ?all=1 view
    admin_rows = env.client.get("/reservations?all=1", headers=H(env.admin_token)).json()
    assert [r["id"] for r in admin_rows] == [rid]
    assert "ssh alice@10.20.0.21" in str(admin_rows)


def test_20_admin_audit(env):
    """B-55: mutating actions append audit rows; GET /admin/audit is admin-only."""
    assert_error(env.client.get("/admin/audit", headers=H(env.user_token)), 403, "forbidden")
    uid = env.conn.execute("SELECT id FROM users WHERE username='hrushi'").fetchone()["id"]
    env.client.patch(f"/users/{uid}", json={"enabled": False}, headers=H(env.admin_token))
    env.client.patch(f"/users/{uid}", json={"enabled": True}, headers=H(env.admin_token))
    rid = reserve(env, env.user_token).json()["id"]
    env.client.delete(f"/reservations/{rid}", headers=H(env.user_token))
    rows = env.client.get("/admin/audit", headers=H(env.admin_token)).json()
    assert [r["action"] for r in rows] == ["cancel", "admin_override", "admin_override"]
    newest, oldest = rows[1], rows[2]
    assert newest["actor"] == "hrushi-admin" and newest["object_type"] == "user"
    assert newest["object_id"] == uid
    assert [newest["detail"]["enabled"], oldest["detail"]["enabled"]] == [True, False]
    cancel = rows[0]
    assert cancel["actor"] == "hrushi" and cancel["object_type"] == "reservation"
    assert cancel["object_id"] == rid
    assert cancel["detail"] == {"previous_state": "queued", "state": "released"}
    assert all(r["created_at"] for r in rows)


def test_21_at_conflict_returns_alternatives(env):
    """B-14: mode 'at' refuses an overlapping window with 409 slot_conflict + alternatives."""
    alice = env.add_user("alice")
    bob = env.add_user("bob")
    first = reserve(env, alice, mode="at", start_at=iso(env.T), hours=2)
    assert first.status_code == 201, first.text
    assert first.json()["start_at"] == iso(env.T)
    err = assert_error(reserve(env, bob, mode="at", start_at=iso(env.T + timedelta(hours=1)),
                               hours=1), 409, "slot_conflict")
    assert err["detail"]["reason"] == "overlap"
    assert err["detail"]["start_at"] == iso(env.T + timedelta(hours=1))
    # freed only after alice's window + 10 min cleanup + 5 min lead = :15, rounded up to grid
    assert err["detail"]["alternatives"] == [
        iso(env.T + timedelta(hours=2, minutes=30)),
        iso(env.T + timedelta(hours=3)),
        iso(env.T + timedelta(hours=3, minutes=30))]
    # the same window in asap mode shifts instead of failing
    shifted = reserve(env, bob, mode="asap", start_at=iso(env.T), hours=1).json()
    assert shifted["start_at"] == iso(env.T + timedelta(hours=2, minutes=30))


def test_22_at_mode_validation(env):
    """B-14: unknown mode -> 422; past/off-grid 'at' -> 409 slot_conflict with the reason."""
    alice = env.add_user("alice")
    assert reserve(env, alice, mode="later").status_code == 422
    past = reserve(env, alice, mode="at", start_at=iso(env.T - timedelta(days=30)), hours=1)
    assert assert_error(past, 409, "slot_conflict")["detail"]["reason"] == "in_the_past"
    off = reserve(env, alice, mode="at", start_at=iso(env.T + timedelta(minutes=10)), hours=1)
    assert assert_error(off, 409, "slot_conflict")["detail"]["reason"] == "off_grid"


def test_23_quota_exceeded(env):
    """B-15: at most two future reservations per user; a live one blocks the next book too."""
    alice = env.add_user("alice")
    first = reserve(env, alice).json()
    second = reserve(env, alice).json()
    err = assert_error(reserve(env, alice), 409, "quota_exceeded")
    assert err["detail"] == {"reason": "future", "limit": 2, "current": 2}
    # promote one to a live state and drop a future slot: the live cap is now the blocker
    env.models.update(env.conn, "reservations", first["id"], {"state": "ready"})
    env.client.delete(f"/reservations/{second['id']}", headers=H(alice))
    err = assert_error(reserve(env, alice), 409, "quota_exceeded")
    assert err["detail"] == {"reason": "live", "limit": 1, "current": 1}


def test_24_quota_admin_exempt(env):
    """B-15: admins are not subject to the per-user caps."""
    for _ in range(4):
        assert reserve(env, env.admin_token).status_code == 201


def test_25_availability_busy_windows(env):
    """B-16: /availability lists busy windows; the owner is shown only to admins."""
    alice = env.add_user("alice")
    bob = env.add_user("bob")
    reserve(env, bob, mode="at", start_at=iso(env.T), hours=2)
    reserve(env, alice, mode="at", start_at=iso(env.T + timedelta(hours=3)), hours=1)
    q = ("/availability?usecase=tls-proxy-nginx"
         f"&from={iso(env.T - timedelta(hours=1))}&to={iso(env.T + timedelta(hours=6))}")
    view = env.client.get(q, headers=H(alice)).json()
    assert view["usecase"] == "tls-proxy-nginx" and view["slot_min"] == 30
    mine = {w["mine"]: w for w in view["windows"]}
    assert set(mine) == {True, False}                       # one is alice's, one is bob's
    assert mine[False]["state"] == "queued" and mine[False]["setup"] == "tb-01"
    assert mine[True]["start_at"] == iso(env.T + timedelta(hours=3))
    # a non-admin response never leaks a username
    assert all("owner" not in w for w in view["windows"])
    assert "alice" not in json.dumps(view) and "bob" not in json.dumps(view)
    # an admin sees who holds each window
    admin_view = env.client.get(q, headers=H(env.admin_token)).json()
    assert sorted(w["owner"] for w in admin_view["windows"]) == ["alice", "bob"]
    assert all(w["mine"] is False for w in admin_view["windows"])


def test_26_availability_validation(env):
    """B-16: unknown use case -> 404; bad range / bad timestamp -> 422; token required."""
    assert_error(env.client.get("/availability?usecase=nope"), 401, "unauthorized")
    assert_error(env.client.get("/availability", headers=H(env.admin_token)), 422, "validation_error")
    assert_error(env.client.get("/availability?usecase=nope", headers=H(env.admin_token)),
                 404, "not_found")
    base = "/availability?usecase=tls-proxy-nginx"
    assert_error(env.client.get(f"{base}&from={iso(env.T)}&to={iso(env.T)}",
                                headers=H(env.admin_token)), 422, "validation_error")
    assert_error(env.client.get(f"{base}&from=not-a-time", headers=H(env.admin_token)),
                 422, "validation_error")


def test_27_cleanup_endpoint(env):
    """B-27: POST /reservations/{id}/cleanup ends a ready/active slot early (202 + job)."""
    alice = env.add_user("alice")
    bob = env.add_user("bob")
    rid = reserve(env, alice).json()["id"]
    queued = reserve(env, alice).json()["id"]      # book before rid goes live (B-15 quota)
    env.models.update(env.conn, "reservations", rid, {"state": "ready"})
    # a non-owner cannot end someone else's slot
    assert_error(env.client.post(f"/reservations/{rid}/cleanup", headers=H(bob)), 403, "forbidden")
    # a queued reservation has nothing to tear down
    assert_error(env.client.post(f"/reservations/{queued}/cleanup", headers=H(alice)),
                 409, "invalid_state")
    resp = env.client.post(f"/reservations/{rid}/cleanup", headers=H(alice))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"] == rid and body["state"] == "cleaning"
    assert body["job"]["kind"] == "cleanup" and body["job"]["state"] == "pending"
    assert body["job"]["reservation_id"] == rid and body["job"]["steps"] == []
    # the state change and the job are persisted
    assert env.models.reservation_by_id(env.conn, rid)["state"] == "cleaning"
    assert [j["kind"] for j in env.models.jobs_for_reservation(env.conn, rid)] == ["cleanup"]
    # a second cleanup is refused and the action is audit-logged
    assert_error(env.client.post(f"/reservations/{rid}/cleanup", headers=H(alice)),
                 409, "invalid_state")
    rows = env.client.get("/admin/audit", headers=H(env.admin_token)).json()
    assert rows[0]["action"] == "cleanup" and rows[0]["object_id"] == rid


def test_28_redeploy_endpoint(env):
    """B-28: POST /reservations/{id}/redeploy re-runs deploy, inside the slot only."""
    alice = env.add_user("alice")
    bob = env.add_user("bob")
    rid = reserve(env, alice).json()["id"]
    env.models.update(env.conn, "reservations", rid, {"state": "active"})
    # env.T is a day ahead, so right now is outside the slot
    assert_error(env.client.post(f"/reservations/{rid}/redeploy", headers=H(alice)),
                 409, "invalid_state")
    # open the slot around now, then redeploy
    now = datetime.now(UTC)
    env.models.update(env.conn, "reservations", rid, {
        "start_at": iso(now - timedelta(hours=1)), "end_at": iso(now + timedelta(hours=1))})
    assert_error(env.client.post(f"/reservations/{rid}/redeploy", headers=H(bob)), 403, "forbidden")
    first = env.client.post(f"/reservations/{rid}/redeploy", headers=H(alice))
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "active"
    assert first.json()["job"]["kind"] == "deploy" and first.json()["job"]["attempt"] == 1
    # a second redeploy is a new job with attempt + 1
    second = env.client.post(f"/reservations/{rid}/redeploy", headers=H(alice)).json()
    assert second["job"]["attempt"] == 2 and second["job"]["id"] != first.json()["job"]["id"]
    # a queued booking cannot redeploy even inside its window
    q = reserve(env, bob).json()["id"]
    env.models.update(env.conn, "reservations", q, {
        "start_at": iso(now - timedelta(hours=1)), "end_at": iso(now + timedelta(hours=1))})
    assert_error(env.client.post(f"/reservations/{q}/redeploy", headers=H(bob)),
                 409, "invalid_state")
