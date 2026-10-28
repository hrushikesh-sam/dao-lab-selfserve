"""The lab-facing switches the dashboards show: DAOLAB_DEPLOYABLE (only the named use cases can
be deployed; the rest stay in the catalog as "coming soon"), the DPU-only flag on reservations
(DAOLAB_DEPLOY_VMS off: the use case's VMs are listed but not provisioned), and
GET /reservations/{id}/jobs behind the admin dashboard's reservation detail."""
import dataclasses

import pytest

from service import config, models


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def vpp_only(api_env, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, deployable=("vpp",), deploy_vms=False))
    return api_env


def catalog(env):
    return {r["name"]: r for r in env.client.get("/usecases", headers=H(env.user_token)).json()}


def test_only_the_deployable_use_cases_can_be_deployed(vpp_only):
    rows = catalog(vpp_only)
    assert rows["vpp"]["deployable"] and rows["vpp"]["available"]
    assert not rows["smartnic"]["deployable"] and not rows["smartnic"]["available"]
    assert rows["smartnic"]["reason"] == "not_enabled"
    planned = next(r for r in rows.values() if r["status"] == "planned")
    assert planned["reason"] == "no_yaml" and not planned["deployable"]


def test_the_catalog_names_the_dpu_login_a_deploy_hands_out(vpp_only):
    rows = catalog(vpp_only)
    assert rows["vpp"]["dpu_login"] == "lab" and rows["diy"]["dpu_login"] is None
    [dpu] = [m for st in rows["vpp"]["setups"] for m in st["members"] if m["kind"] == "dpu"]
    assert dpu["address"] == "10.68.0.190"


def test_the_api_refuses_a_use_case_that_is_not_enabled_even_for_admins(vpp_only):
    env = vpp_only
    r = env.client.post("/reservations", headers=H(env.admin_token), json={"usecase": "smartnic"})
    assert r.status_code == 409
    assert r.json()["error"]["detail"] == {"reason": "not_enabled", "deployable": ["vpp"]}
    assert env.client.post("/reservations", headers=H(env.user_token),
                           json={"usecase": "vpp"}).status_code == 201


def test_every_use_case_with_a_yaml_is_deployable_when_the_switch_is_empty(api_env):
    assert config.settings.deployable == ()
    for r in catalog(api_env).values():
        assert r["deployable"] == (r["status"] == "active")


def test_a_dpu_only_reservation_says_its_vms_are_not_provisioned(vpp_only):
    env = vpp_only
    rid = env.client.post("/reservations", headers=H(env.user_token),
                          json={"usecase": "vpp"}).json()["id"]
    models.create_vms(env.conn, rid)
    r = env.client.get(f"/reservations/{rid}", headers=H(env.user_token)).json()
    assert r["vms_provisioned"] is False
    assert {v["role"] for v in r["vms"] if v["name"]} == {"sink", "tgen"}
    assert "not provisioned" in r["diagram"] and "no IP yet" not in r["diagram"]


def test_with_vms_on_they_are_waiting_for_their_ip(api_env, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, deploy_vms=True))
    rid = api_env.client.post("/reservations", headers=H(api_env.user_token),
                              json={"usecase": "vpp"}).json()["id"]
    models.create_vms(api_env.conn, rid)
    r = api_env.client.get(f"/reservations/{rid}", headers=H(api_env.user_token)).json()
    assert r["vms_provisioned"] is True and "no IP yet" in r["diagram"]


def test_a_reservations_jobs_are_shown_to_its_owner_and_admins_only(vpp_only):
    env = vpp_only
    rid = env.client.post("/reservations", headers=H(env.user_token),
                          json={"usecase": "vpp"}).json()["id"]
    first = models.create_job(env.conn, rid, "deploy")
    second = models.create_job(env.conn, rid, "health_probe")
    for token in (env.user_token, env.admin_token):
        jobs = env.client.get(f"/reservations/{rid}/jobs", headers=H(token)).json()
        assert [(j["id"], j["kind"]) for j in jobs] == [(first, "deploy"), (second, "health_probe")]
        assert {"steps", "log_tail", "trace_id", "error"} <= set(jobs[0])
    other = env.client.get(f"/reservations/{rid}/jobs", headers=H(env.tokens["bob"]))
    assert other.status_code == 403
