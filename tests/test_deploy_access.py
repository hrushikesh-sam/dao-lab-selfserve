"""Deploy access: the dashboard's Deploy dialog asks for an SSH key; left empty, a password is
generated. The owner then gets `ssh lab@<the DPU's static address>` with that key or password,
shown only to them and only while the slot is live."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from service import access, config, engine, models, runner
from service.runner import MockRunner
from service.scheduler import SchedConfig

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeploy+Key/0 alice@work"


def H(token):
    return {"Authorization": f"Bearer {token}"}


def deploy(env, who="alice", **body):
    r = env.client.post("/reservations", headers=H(env.tokens[who]),
                        json={"usecase": "vpp", "hours": 1, **body})
    return r


def get(env, rid, who="alice"):
    return env.client.get(f"/reservations/{rid}", headers=H(env.tokens[who]))


def go_live(env, rid):
    env.conn.execute("UPDATE reservations SET state = 'active' WHERE id = ?", (rid,))


def test_whoami_offers_the_registered_key_for_the_dialog(api_env):
    me = api_env.client.get("/whoami", headers=H(api_env.tokens["alice"])).json()
    assert me["ssh_pubkey"] == "ssh-ed25519 AAAA test"


def test_deploy_with_a_key_gives_ssh_lab_at_the_static_dpu(api_env):
    r = deploy(api_env, ssh_pubkey=KEY + "\n")
    assert r.status_code == 201, r.text
    acc = r.json()["access"]
    assert acc == {"login": "lab", "host": "10.68.0.190", "ssh": "ssh lab@10.68.0.190",
                   "live": False, "auth": "ssh-key", "password": None,
                   "key": "ssh-ed25519 …alice@work"}
    row = models.reservation_by_id(api_env.conn, r.json()["id"])
    assert (row["ssh_pubkey"], row["access_password"]) == (KEY, None)


def test_deploy_without_a_key_generates_a_password_shown_once_live(api_env):
    r = deploy(api_env, ssh_pubkey="   ")
    rid = r.json()["id"]
    assert r.json()["access"]["auth"] == "password" and r.json()["access"]["password"] is None
    row = models.reservation_by_id(api_env.conn, rid)
    pw = row["access_password"]
    assert len(pw) == 16 and pw.isalnum() and row["ssh_pubkey"] is None
    go_live(api_env, rid)
    acc = get(api_env, rid).json()["access"]
    assert (acc["live"], acc["password"], acc["key"]) == (True, pw, None)


def test_two_password_deploys_get_different_passwords(api_env):
    a = deploy(api_env, "alice", ssh_pubkey="").json()["id"]
    b = deploy(api_env, "bob", ssh_pubkey="").json()["id"]
    pws = {models.reservation_by_id(api_env.conn, i)["access_password"] for i in (a, b)}
    assert len(pws) == 2


def test_no_key_field_means_the_registered_key(api_env):
    r = deploy(api_env)
    acc = r.json()["access"]
    assert acc["auth"] == "ssh-key" and acc["key"] == "ssh-ed25519 …test"


@pytest.mark.parametrize("bad", ["not a key", "ssh-ed25519", "ssh-ed25519 AAA$$ x",
                                 'command="sh" ' + KEY, "\n".join([KEY] * 6)])
def test_a_malformed_key_is_refused(api_env, bad):
    r = deploy(api_env, ssh_pubkey=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert models.list_reservations(api_env.conn) == []


def test_only_the_owner_or_an_admin_sees_the_password(api_env):
    rid = deploy(api_env, ssh_pubkey="").json()["id"]
    go_live(api_env, rid)
    assert get(api_env, rid, "bob").status_code == 403
    assert get(api_env, rid, "dev-admin").json()["access"]["password"]
    listing = api_env.client.get("/reservations", headers=H(api_env.tokens["bob"])).json()
    assert listing == []
    avail = api_env.client.get("/availability?usecase=vpp", headers=H(api_env.tokens["bob"])).json()
    assert "password" not in str(avail) and "access" not in str(avail)


def test_the_dpu_key_window_uses_the_deployed_key(api_env):
    rid = deploy(api_env, ssh_pubkey=KEY).json()["id"]
    go_live(api_env, rid)
    row = models.reservation_by_id(api_env.conn, rid)
    now = models.parse_ts(row["start_at"]) + timedelta(minutes=1)
    assert access.keys_for(api_env.conn, "dpu-01", "lab", now) == [KEY]


def test_a_password_deploy_opens_no_key_window(api_env):
    rid = deploy(api_env, ssh_pubkey="").json()["id"]
    go_live(api_env, rid)
    row = models.reservation_by_id(api_env.conn, rid)
    now = models.parse_ts(row["start_at"]) + timedelta(minutes=1)
    assert access.keys_for(api_env.conn, "dpu-01", "lab", now) == []


def test_the_runner_gets_the_key_or_the_password(api_env):
    k = deploy(api_env, "alice", ssh_pubkey=KEY).json()["id"]
    p = deploy(api_env, "bob", ssh_pubkey="").json()["id"]
    rn = runner.AnsibleRunner(config.settings, run=lambda **kw: None)
    ek = rn.extravars(models.reservation_context(api_env.conn, k))
    ep = rn.extravars(models.reservation_context(api_env.conn, p))
    assert (ek["ssh_pubkey"], ek["daolab_access_password"]) == (KEY, "")
    assert ep["ssh_pubkey"] == "" and len(ep["daolab_access_password"]) == 16
    assert ek["daolab_access_login"] == "lab"
    end = models.parse_ts(models.reservation_by_id(api_env.conn, k)["end_at"])
    assert ek["daolab_access_expires"] == (end + timedelta(days=1)).date().isoformat()


def test_the_password_is_forgotten_once_the_slot_is_over(api_env):
    rid = deploy(api_env, ssh_pubkey="").json()["id"]
    api_env.conn.execute("UPDATE reservations SET state = 'released' WHERE id = ?", (rid,))
    engine.tick(api_env.conn, datetime.now(timezone.utc), MockRunner(), SchedConfig())
    assert models.reservation_by_id(api_env.conn, rid)["access_password"] is None
    assert get(api_env, rid).json()["access"]["password"] is None


def test_the_card_shows_the_latest_job(api_env):
    rid = deploy(api_env).json()["id"]
    assert get(api_env, rid).json()["job"] is None
    models.transition_reservation(api_env.conn, rid, "provisioning", from_state="queued")
    jid = models.create_job(api_env.conn, rid, "deploy")
    models.transition_job(api_env.conn, jid, "running")
    models.append_job_step(api_env.conn, jid, "lease_ips")
    job = get(api_env, rid).json()["job"]
    assert (job["id"], job["kind"], job["state"], job["steps"]) == (jid, "deploy", "running", 1)
    assert job["last_step"]["name"] == "lease_ips"


def test_diy_has_no_dpu_access_block(api_env):
    r = api_env.client.post("/reservations", headers=H(api_env.tokens["alice"]), json={
        "usecase": "diy", "hours": 1, "setup": "tb-01",
        "templates": {"tb-01-xcp": "tpl-ubuntu2204-dao"}})
    assert r.status_code == 201, r.text
    assert r.json()["access"] is None


def test_cli_reserve_password_flag(api_env, monkeypatch, capsys):
    from service import cli
    sent = {}
    monkeypatch.setattr(cli, "_api", lambda m, path, body=None: sent.update(body=body) or {
        "id": 1, "setup": "tb-01", "state": "queued", "start_at": "x", "end_at": "y",
        "shifted": False, "access": {"auth": "password", "ssh": "ssh lab@10.68.0.190"}})
    args = type("A", (), {"usecase": "vpp", "start": "now", "hours": 1, "setup": None,
                          "template": None, "password": True, "ssh_key": None})
    assert cli.cmd_reserve(args) == 0
    assert sent["body"]["ssh_pubkey"] == ""
    assert "ssh lab@10.68.0.190  with a generated password" in capsys.readouterr().out


def test_ending_a_live_slot_queues_its_cleanup_and_the_engine_releases_it(api_env):
    """A live slot is ended with POST …/cleanup: `cleaning` plus a cleanup job the engine runs
    to `released` (a live slot left `cleaning` with no job would keep the testbed held for
    ever). DELETE only cancels a queued booking: on a live slot it is a 409 and queues nothing."""
    rid = deploy(api_env).json()["id"]
    go_live(api_env, rid)
    r = api_env.client.delete(f"/reservations/{rid}", headers=H(api_env.tokens["alice"]))
    assert r.status_code == 409 and models.jobs_for_reservation(api_env.conn, rid) == []
    r = api_env.client.post(f"/reservations/{rid}/cleanup", headers=H(api_env.tokens["alice"]))
    assert r.status_code == 202 and r.json()["state"] == "cleaning"
    [job] = models.jobs_for_reservation(api_env.conn, rid)
    assert (job["kind"], job["state"]) == ("cleanup", "pending")
    engine.tick(api_env.conn, datetime.now(timezone.utc), MockRunner(), SchedConfig())
    assert models.reservation_by_id(api_env.conn, rid)["state"] == "released"


def test_cancelling_a_queued_slot_queues_nothing(api_env):
    rid = deploy(api_env).json()["id"]
    api_env.client.delete(f"/reservations/{rid}", headers=H(api_env.tokens["alice"]))
    assert models.jobs_for_reservation(api_env.conn, rid) == []


def test_the_dpu_role_shows_the_dpus_static_address_before_anything_is_posted(api_env):
    rid = deploy(api_env).json()["id"]
    models.transition_reservation(api_env.conn, rid, "provisioning", from_state="queued")
    models.create_vms(api_env.conn, rid)
    body = get(api_env, rid).json()
    vms = {v["role"]: v for v in body["vms"]}
    assert vms["vpp"]["ip"] == "10.68.0.190" and vms["vpp"]["ssh"] is None
    assert vms["tgen"]["ip"] is None                               # VMs wait for their lease
    assert "10.68.0.190" in body["diagram"]


def test_the_booking_mode_is_recorded(api_env):
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=10, minute=0, second=0,
                                                                       microsecond=0)
    at = deploy(api_env, "alice", mode="at", start_at=models.format_ts(start)).json()["id"]
    asap = deploy(api_env, "bob").json()["id"]
    assert models.reservation_by_id(api_env.conn, at)["mode"] == "at"
    assert models.reservation_by_id(api_env.conn, asap)["mode"] == "asap"


def test_the_runner_gets_the_end_of_the_slot_for_key_expiry(api_env):
    rid = deploy(api_env, ssh_pubkey=KEY).json()["id"]
    e = runner.AnsibleRunner(config.settings, run=lambda **kw: None).extravars(
        models.reservation_context(api_env.conn, rid))
    end = models.parse_ts(models.reservation_by_id(api_env.conn, rid)["end_at"])
    assert e["daolab_access_until"] == int(end.timestamp())
