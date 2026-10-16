"""B-23 follow-up: the engine creates a reservation's `vms` rows at queued -> provisioning
(plan/04 step (d)), and hands the runner a reservation context instead of a DB row."""
import os
from datetime import timedelta

import pytest

from service import catalog, engine, models
from service.runner import MockRunner
from service.scheduler import SchedConfig
from tests.conftest import NOW, REPO, T, mk_res, mk_user


@pytest.fixture
def real(db):
    """The real catalog: tb-01 (dpu-01 + tg-01 on tb-01-xcp) and the three use cases."""
    catalog.seed_usecases(db)
    catalog.sync(db, os.path.join(REPO, "usecases"), os.path.join(REPO, "setups"),
                 os.path.join(REPO, "inventory", "hosts.yml"))
    uid, _ = mk_user(db, "alice")
    sid = models.setup_by_name(db, "tb-01")["id"]
    return db, sid, uid


def book(real, usecase="vpp", state="queued", start=T(9, 30), end=T(11), **extra):
    db, sid, uid = real
    rid = mk_res(db, sid, models.usecase_by_name(db, usecase)["id"], start, end,
                 state=state, user_id=uid)
    if extra:
        models.update(db, "reservations", rid, extra)
    return rid


def rows(db, rid):
    return [(v["role"], v["host"], v["template"], v["name"], v["ip"])
            for v in models.vms_for_reservation(db, rid)]


def test_vpp_roles_resolve_against_tb01(real):
    db = real[0]
    rid = book(real)
    assert models.create_vms(db, rid) == 3
    assert sorted(rows(db, rid)) == [
        ("sink", "tb-01-xcp", "tpl-ubuntu2204-dao", f"daolab-r{rid}-sink", None),   # dpu_host
        ("tgen", "tb-01-xcp", "tpl-ubuntu2204-dao", f"daolab-r{rid}-tgen", None),   # tg_host
        ("vpp", "dpu-01", None, None, None),                                        # on: dpu
    ]


def test_create_vms_is_idempotent_and_keeps_posted_ips(real):
    db = real[0]
    rid = book(real, usecase="tls-proxy-nginx")
    models.create_vms(db, rid)
    models.set_vm_ip(db, models.vm_by_role(db, rid, "client")["id"], "10.30.0.100")
    assert models.create_vms(db, rid) == 0
    assert models.vm_by_role(db, rid, "client")["ip"] == "10.30.0.100"


def test_diy_gets_one_vm_per_template_host(real):
    db = real[0]
    rid = book(real, usecase="diy", diy=1,
               diy_templates=models.to_json({"tb-01-xcp": "tpl-ubuntu2204-dao"}))
    models.create_vms(db, rid)
    assert rows(db, rid) == [("tb-01-xcp", "tb-01-xcp", "tpl-ubuntu2204-dao",
                              f"daolab-r{rid}-tb-01-xcp", None)]


def test_a_role_whose_member_is_missing_is_left_out(real):
    db, sid, _ = real
    db.execute("DELETE FROM resources WHERE setup_id = ? AND kind = 'nic'", (sid,))
    rid = book(real)
    models.create_vms(db, rid)
    assert sorted(r[0] for r in rows(db, rid)) == ["sink", "vpp"]       # no tgen


def test_engine_creates_the_rows_with_the_provisioning_move(real):
    db = real[0]
    rid = book(real, start=T(9, 30))                     # first deploy reflashes: lead 15
    engine.tick(db, NOW + timedelta(minutes=15), MockRunner(),
                SchedConfig())
    assert models.reservation_by_id(db, rid)["state"] == "ready"
    assert sorted(r[0] for r in rows(db, rid)) == ["sink", "tgen", "vpp"]


def test_a_queued_reservation_has_no_rows_yet(real):
    db = real[0]
    rid = book(real, start=T(12))
    engine.tick(db, NOW, MockRunner(), SchedConfig())
    assert models.reservation_by_id(db, rid)["state"] == "queued" and rows(db, rid) == []


def test_reservation_context_carries_the_playbook_extravars(real):
    db = real[0]
    rid = book(real, usecase="tls-proxy-nginx")
    models.create_vms(db, rid)
    ctx = models.reservation_context(db, rid)
    assert (ctx["id"], ctx["reservation_id"], ctx["usecase"], ctx["setup"], ctx["user"]) == \
           (rid, rid, "tls-proxy-nginx", "tb-01", "alice")
    assert ctx["ssh_pubkey"] == "ssh-ed25519 AAAA test"
    assert (ctx["hw_class"], ctx["diy"], ctx["templates"]) == ("sdp-pf1-vf8", False, {})
    assert set(ctx["roles"]) == {"proxy", "backend", "client"}
    assert ctx["verify"]["from"] == "client"
    assert {m["name"] for m in ctx["members"]} == {"dpu-01", "tg-01"}
    assert {v["role"] for v in ctx["vms"]} == {"proxy", "backend", "client"}


def test_the_runner_receives_the_context_and_the_job(real):
    db = real[0]
    rid = book(real, state="provisioning")
    models.create_job(db, rid, "deploy")
    seen = []

    class Spy(MockRunner):
        def deploy(self, res, step):
            seen.append(dict(res))
            return super().deploy(res, step)

    engine.tick(db, NOW, Spy(), SchedConfig())
    [ctx] = seen
    assert ctx["id"] == rid and ctx["usecase"] == "vpp" and ctx["job_kind"] == "deploy"
    assert ctx["attempt"] == 1 and ctx["job_id"] == models.jobs_for_reservation(db, rid)[0]["id"]
