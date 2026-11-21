"""B-71: the cleanup flow — teardown -> verify -> (clean: release leases + setup free) /
(residue: reconcile x N -> quarantine setup + IPs), and reconcile jobs (plan/09 §9)."""
import dataclasses
from datetime import timedelta

import pytest

from service import config, engine, ipam, models, runner
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


def book(lab, state="cleaning", start=T(7), end=T(9), roles=("client", "backend")):
    rid = mk_res(lab["db"], lab["sid"], lab["uc"], start, end, state=state)
    for role in roles:
        ipam.lease(lab["db"], rid, role)
    return rid


def tick(lab, rn, now=NOW):
    return engine.tick(lab["db"], now, rn, SchedConfig())


def st(lab, rid):
    return models.reservation_by_id(lab["db"], rid)["state"]


def availability(lab):
    return lab["db"].execute("SELECT availability FROM setups WHERE id = ?",
                             (lab["sid"],)).fetchone()[0]


def lease_states(lab, rid):
    return sorted(l["state"] for l in models.ip_leases_for_reservation(lab["db"], rid))


def jobs(lab, rid):
    return models.jobs_for_reservation(lab["db"], rid)


class Scripted:
    """cleanup / reconcile succeed; verify_clean answers from a list, one per call."""
    def __init__(self, *verdicts, with_reconcile=True):
        self.verdicts, self.calls = list(verdicts), []
        if not with_reconcile:
            self.reconcile = None

    def _step(self, name, step):
        self.calls.append(name)
        step(name)
        return RunResult(ok=True)

    def cleanup(self, res, step): return self._step("cleanup", step)
    def reconcile(self, res, step): return self._step("reconcile", step)  # noqa: E301
    def deploy(self, res, step): return self._step("deploy", step)
    def probe(self, res, step): return self._step("probe", step)

    def verify_clean(self, res, step):
        self.calls.append("verify")
        residue = self.verdicts.pop(0) if self.verdicts else ()
        step("verify", "failed" if residue else "ok")
        return RunResult(ok=not residue, residue=tuple(residue))


def queue_cleanup(lab, rid):
    return models.create_job(lab["db"], rid, "cleanup")


# ---- clean -----------------------------------------------------------------------------

def test_a_clean_cleanup_releases_the_leases_and_the_addresses_are_reused(lab):
    rid = book(lab)
    queue_cleanup(lab, rid)
    out = tick(lab, MockRunner())
    assert st(lab, rid) == "released" and out.released == 1
    assert lease_states(lab, rid) == ["released", "released"]
    nxt = book(lab, state="provisioning", start=T(10), end=T(11), roles=())
    assert ipam.lease(lab["db"], nxt, "client")["ip"] == "10.30.0.100"
    assert availability(lab) == "available"


# ---- residue: reconcile x N, then quarantine -----------------------------------------------

def test_mock_fail_cleanup_ends_quarantined_after_n_reconciles(lab):
    """The tasksheet's done-when: DAOLAB_MOCK_FAIL=cleanup -> quarantined."""
    rid = book(lab)
    jid = queue_cleanup(lab, rid)
    out = tick(lab, MockRunner(fail={"cleanup"}))
    assert st(lab, rid) == "quarantined" and out.quarantined == 1
    assert availability(lab) == "quarantined"
    assert lease_states(lab, rid) == ["quarantined", "quarantined"]
    steps = [s["name"] for s in models.job_steps(models.job_by_id(lab["db"], jid))]
    n = config.settings.reconcile_attempts
    assert steps == list(runner.CLEANUP_STEPS) + list(runner.VERIFY_CLEAN_STEPS) + \
        (list(runner.RECONCILE_STEPS) + list(runner.VERIFY_CLEAN_STEPS)) * n
    [event] = [e for e in models.list_audit_events(lab["db"]) if e["action"] == "cleanup"]
    detail = models.from_json(event["detail"])
    assert detail["reconcile_attempts"] == n and detail["residue"]


def test_residue_that_reconcile_clears_ends_released(lab):
    rid = book(lab)
    queue_cleanup(lab, rid)
    rn = Scripted(["vm:daolab-r1-backend"], [])
    out = tick(lab, rn)
    assert rn.calls == ["cleanup", "verify", "reconcile", "verify"]
    assert st(lab, rid) == "released" and out.released == 1 and availability(lab) == "available"
    assert lease_states(lab, rid) == ["released", "released"]


@pytest.mark.parametrize("attempts", [0, 1, 3])
def test_the_number_of_reconcile_attempts_is_a_setting(lab, monkeypatch, attempts):
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, reconcile_attempts=attempts))
    rid = book(lab)
    queue_cleanup(lab, rid)
    rn = Scripted(*([["vm:x"]] * 10))
    tick(lab, rn)
    assert rn.calls.count("reconcile") == attempts and st(lab, rid) == "quarantined"


def test_a_runner_without_reconcile_goes_straight_to_quarantine(lab):
    rid = book(lab)
    queue_cleanup(lab, rid)
    rn = Scripted(["vm:x"], with_reconcile=False)
    tick(lab, rn)
    assert rn.calls == ["cleanup", "verify"] and st(lab, rid) == "quarantined"


def test_quarantine_never_overrides_maintenance(lab):
    models.set_setup_availability(lab["db"], lab["sid"], "maintenance")
    rid = book(lab)
    queue_cleanup(lab, rid)
    tick(lab, MockRunner(fail={"cleanup"}))
    assert st(lab, rid) == "quarantined" and availability(lab) == "maintenance"


def test_residue_after_a_failed_deploy_flags_the_setup_and_blocks_the_next_start(lab, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    rid = book(lab, state="provisioning", start=T(9, 5), end=T(10))
    models.create_job(lab["db"], rid, "deploy")
    rn = MockRunner(fail={"deploy", "cleanup"})
    tick(lab, rn)                                    # deploy fails for good -> failed + cleanup
    tick(lab, rn)                                    # cleanup leaves residue
    assert st(lab, rid) == "failed" and availability(lab) == "quarantined"
    assert [r["id"] for r in models.dirty_reservations(lab["db"], lab["sid"])] == [rid]
    nxt = mk_res(lab["db"], lab["sid"], lab["uc"], T(10), T(11))
    tick(lab, MockRunner(), now=T(10))               # due, setup not held - but quarantined
    assert st(lab, nxt) == "queued"


def test_a_due_booking_waits_while_a_failed_deploys_cleanup_is_pending(lab, monkeypatch):
    """B-94 (mistakes.md P1): between a deploy's last failed attempt and the end of its cleanup
    chain the reservation is `failed` — terminal, so `held_by` ignores it — and the setup is
    still `available`, because quarantine only happens once cleanup has found residue. A
    booking that comes due in that window must wait, not start on a possibly dirty setup."""
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    rid = book(lab, state="provisioning", start=T(9, 5), end=T(10))
    models.create_job(lab["db"], rid, "deploy")
    tick(lab, MockRunner(fail={"deploy"}))           # fails for good -> failed + cleanup queued
    assert st(lab, rid) == "failed" and availability(lab) == "available"
    assert models.held_by(lab["db"], lab["sid"]) is None          # terminal: nothing holds it
    assert [(j["kind"], j["state"]) for j in jobs(lab, rid)] == [
        ("deploy", "failed"), ("cleanup", "pending")]

    nxt = mk_res(lab["db"], lab["sid"], lab["uc"], T(10), T(11))
    tick(lab, MockRunner(), now=T(10))               # due, setup free - but cleanup is pending
    assert st(lab, nxt) == "queued"
    assert [j["kind"] for j in jobs(lab, nxt)] == []               # no deploy job was created
    assert st(lab, rid) == "released"                # that same tick ran the cleanup

    tick(lab, MockRunner(), now=T(10))               # cleanup done -> the booking may start
    assert st(lab, nxt) != "queued"
    assert [j["kind"] for j in jobs(lab, nxt)] == ["deploy"]


# ---- reconcile jobs ------------------------------------------------------------------------

def quarantined(lab):
    rid = book(lab)
    queue_cleanup(lab, rid)
    tick(lab, MockRunner(fail={"cleanup"}))
    assert st(lab, rid) == "quarantined"
    return rid


def test_a_clean_reconcile_releases_the_reservation_frees_the_ips_and_the_setup(lab):
    rid = quarantined(lab)
    jid = models.create_job(lab["db"], rid, "reconcile")
    out = tick(lab, MockRunner())
    assert models.job_by_id(lab["db"], jid)["state"] == "succeeded"
    assert st(lab, rid) == "released" and out.reconciled == 1
    assert lease_states(lab, rid) == ["released", "released"]       # quarantined ones too
    assert availability(lab) == "available"
    [event] = [e for e in models.list_audit_events(lab["db"]) if e["action"] == "reconcile"]
    assert models.from_json(event["detail"])["ok"] is True


def test_the_next_booking_starts_once_reconcile_frees_the_setup(lab):
    rid = quarantined(lab)
    nxt = mk_res(lab["db"], lab["sid"], lab["uc"], T(9, 30), T(11))
    tick(lab, MockRunner(), now=T(9, 25))
    assert st(lab, nxt) == "queued"                                  # still blocked
    models.create_job(lab["db"], rid, "reconcile")
    tick(lab, MockRunner(), now=T(9, 25))                            # reconciled this tick
    tick(lab, MockRunner(), now=T(9, 26))                            # started the next one
    assert st(lab, rid) == "released" and st(lab, nxt) == "ready"


def test_a_dirty_reconcile_fails_the_job_and_keeps_everything_blocked(lab):
    rid = quarantined(lab)
    jid = models.create_job(lab["db"], rid, "reconcile")
    tick(lab, MockRunner(fail={"cleanup"}))
    job = models.job_by_id(lab["db"], jid)
    assert job["state"] == "failed" and job["error"].startswith("residue after reconcile:")
    assert st(lab, rid) == "quarantined" and availability(lab) == "quarantined"
    assert lease_states(lab, rid) == ["quarantined", "quarantined"]


def test_the_setup_stays_quarantined_while_another_reservation_is_dirty(lab, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    failed = book(lab, state="provisioning", start=T(6), end=T(7), roles=("a",))
    models.create_job(lab["db"], failed, "deploy")
    rn = MockRunner(fail={"deploy", "cleanup"})
    tick(lab, rn)
    tick(lab, rn)                                                     # failed + residue
    q = quarantined(lab)
    models.create_job(lab["db"], q, "reconcile")
    tick(lab, MockRunner())
    assert st(lab, q) == "released" and availability(lab) == "quarantined"   # `failed` still dirty
    models.create_job(lab["db"], failed, "reconcile")
    tick(lab, MockRunner())
    assert st(lab, failed) == "released" and availability(lab) == "available"
    assert models.dirty_reservations(lab["db"], lab["sid"]) == []


def test_reconcile_on_a_reservation_that_is_not_dirty_is_refused(lab):
    rid = book(lab, state="active", start=T(8), end=T(12))
    jid = models.create_job(lab["db"], rid, "reconcile")
    tick(lab, MockRunner())
    job = models.job_by_id(lab["db"], jid)
    assert job["state"] == "failed" and "expected quarantined or failed" in job["error"]
    assert st(lab, rid) == "active"


def test_end_to_end_slot_end_to_quarantine_to_reconcile(lab):
    """An active slot ends, cleanup leaves residue, an admin's reconcile clears it."""
    rid = book(lab, state="active", start=T(7), end=T(8, 59))
    tick(lab, MockRunner(fail={"cleanup"}))
    assert st(lab, rid) == "quarantined"
    models.create_job(lab["db"], rid, "reconcile")
    tick(lab, MockRunner(), now=NOW + timedelta(minutes=1))
    assert st(lab, rid) == "released" and availability(lab) == "available"
