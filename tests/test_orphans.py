"""B-73: the orphan sweep - every tick, tagged VMs / live leases whose reservation is terminal
(or gone) are cleaned up (plan/09 §9)."""
import pytest

from service import engine, ipam, models
from service.runner import MockRunner, RunResult
from service.scheduler import SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase


@pytest.fixture
def lab(db):
    sid = mk_setup(db, "tb", ["wire"], dpu_hw_class="c")
    uc = mk_usecase(db, "uc", ["wire"], hw_class="c")
    models.insert(db, "ip_pools", {"name": ipam.DEFAULT_POOL,
                                   "range_start": "10.30.0.100", "range_end": "10.30.0.109"})
    return {"db": db, "sid": sid, "uc": uc}


def book(lab, state, start=T(8), end=T(12)):
    return mk_res(lab["db"], lab["sid"], lab["uc"], start, end, state=state)


def tick(lab, rn):
    return engine.tick(lab["db"], NOW, rn, SchedConfig())


def sweeps(lab):
    return [models.from_json(e["detail"]) for e in models.list_audit_events(lab["db"])
            if e["action"] == "orphan_sweep"]


def test_a_manual_vm_tagged_daolab_r999_gets_cleaned(lab):
    """The tasksheet's done-when: no reservation 999 exists, so its VM is an orphan."""
    rn = MockRunner()
    rn.vms.add("daolab-r999-manual")
    out = tick(lab, rn)
    assert rn.vms == set() and out.orphan_vms == 1
    assert sweeps(lab) == [{"vms": ["daolab-r999-manual"], "ok": True, "error": None}]


def test_vms_of_terminal_reservations_go_and_live_ones_stay(lab):
    released, failed = book(lab, "released"), book(lab, "failed")
    active, quarantined = book(lab, "active"), book(lab, "quarantined")
    rn = MockRunner()
    rn.vms.update({f"daolab-r{released}-a", f"daolab-r{failed}-a", f"daolab-r{active}-a",
                   f"daolab-r{quarantined}-a", "admin-scratch", "daolab-notes"})
    tick(lab, rn)
    assert rn.vms == {f"daolab-r{active}-a", f"daolab-r{quarantined}-a",
                      "admin-scratch", "daolab-notes"}          # live, quarantined, untagged


def test_a_terminal_reservation_with_a_waiting_cleanup_is_left_to_it(lab):
    failed = book(lab, "failed")
    jid = models.create_job(lab["db"], failed, "cleanup")
    models.transition_job(lab["db"], jid, "running")             # another engine has it
    rn = MockRunner()
    rn.vms.add(f"daolab-r{failed}-a")
    tick(lab, rn)
    assert rn.vms == {f"daolab-r{failed}-a"}


def test_leased_addresses_of_terminal_reservations_are_released(lab):
    gone, live = book(lab, "released"), book(lab, "active")
    ipam.lease(lab["db"], gone, "client")
    ipam.lease(lab["db"], live, "client")
    out = tick(lab, MockRunner())
    assert out.orphan_leases == 1
    assert [l["state"] for l in models.ip_leases_for_reservation(lab["db"], gone)] == ["released"]
    assert [l["state"] for l in models.ip_leases_for_reservation(lab["db"], live)] == ["leased"]


def test_quarantined_addresses_are_never_swept(lab):
    failed = book(lab, "failed")
    ipam.lease(lab["db"], failed, "client")
    ipam.quarantine_reservation(lab["db"], failed)
    tick(lab, MockRunner())
    assert [l["state"] for l in models.ip_leases_for_reservation(lab["db"], failed)] == \
        ["quarantined"]


def test_when_destroying_fails_the_leases_stay_and_it_is_retried(lab):
    gone = book(lab, "released")
    ipam.lease(lab["db"], gone, "client")

    class Stuck(MockRunner):
        def destroy_vms(self, names, step):
            return RunResult(ok=False, error="xapi timeout")

    rn = Stuck()
    rn.vms.add(f"daolab-r{gone}-client")
    out = tick(lab, rn)
    assert out.orphan_vms == 0 and out.orphan_leases == 0
    assert sweeps(lab)[0]["ok"] is False
    rn2 = MockRunner()
    rn2.vms = rn.vms
    tick(lab, rn2)                                               # next tick: it goes
    assert rn2.vms == set()
    assert [l["state"] for l in models.ip_leases_for_reservation(lab["db"], gone)] == ["released"]


def test_a_runner_that_cannot_list_vms_does_not_stop_the_tick(lab):
    class Blind(MockRunner):
        def tagged_vms(self):
            raise RuntimeError("dom0 unreachable")

    gone = book(lab, "released")
    ipam.lease(lab["db"], gone, "client")
    out = tick(lab, Blind())
    assert out.orphan_leases == 1                                # the DB half still runs


def test_a_runner_without_the_sweep_methods_skips_the_vm_half(lab):
    class Old:
        def deploy(self, r, s): return RunResult(ok=True)        # noqa: E704
    out = engine.tick(lab["db"], NOW, Old(), SchedConfig())
    assert out.orphan_vms == 0


def test_a_mock_deploy_and_cleanup_leave_no_orphan(lab):
    """End to end: deploy creates the VMs, cleanup removes them, nothing is left to sweep."""
    rid = book(lab, "provisioning", start=T(9, 5), end=T(10))
    models.insert(lab["db"], "vms", {"reservation_id": rid, "role": "a", "host": "tb-xcp",
                                     "template": "tpl", "name": f"daolab-r{rid}-a"})
    models.create_job(lab["db"], rid, "deploy")
    rn = MockRunner()
    tick(lab, rn)
    assert rn.vms == {f"daolab-r{rid}-a"}
    engine.tick(lab["db"], T(10), rn, SchedConfig())             # slot over -> cleanup
    assert rn.vms == set() and sweeps(lab) == []
