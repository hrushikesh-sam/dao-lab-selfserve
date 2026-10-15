"""B-23: the engine tick — bookings become running reservations (plan/09 §6, §9, §10)."""
import dataclasses
import logging
from datetime import timedelta

import pytest

from service import config, engine, migrations, models, runner, scheduler
from service.models import JobError
from service.runner import (CLEANUP_STEPS, DEPLOY_STEPS, VERIFY_CLEAN_STEPS, MockRunner,
                            RunResult)
from service.scheduler import SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

CFG = SchedConfig()          # leads 5 / 15, cleanup 10


def mock(**kw):
    return MockRunner(step_sec=0.0, **kw)


@pytest.fixture
def world(db):
    """One setup whose DPU runs class cls-a, and three use cases: same class, different
    class, and no class."""
    sid = mk_setup(db, "tb", ["wire"], dpu_hw_class="cls-a")
    ids = {"a": mk_usecase(db, "ucA", ["wire"], hw_class="cls-a"),
           "b": mk_usecase(db, "ucB", ["wire"], hw_class="cls-b"),
           "n": mk_usecase(db, "ucN", ["wire"], hw_class=None)}
    return db, sid, ids


def res(world, uc="a", start=T(10), end=T(11), state="queued", sid=None):
    db, setup_id, ids = world
    return mk_res(db, sid or setup_id, ids[uc], start, end, state=state)


def state(db, rid):
    return models.reservation_by_id(db, rid)["state"]


def jobs(db, rid=None):
    q = "SELECT * FROM jobs" + (" WHERE reservation_id = ?" if rid else "") + " ORDER BY id"
    return db.execute(q, (rid,) if rid else ()).fetchall()


def dpu_class(db, sid):
    return db.execute("SELECT hw_class FROM resources WHERE setup_id = ? AND kind = 'dpu'",
                      (sid,)).fetchone()[0]


def tick(db, now=NOW, rn=None, cfg=CFG):
    return engine.tick(db, now, rn or mock(), cfg)


class Fake:
    """A scripted runner: records calls; each function returns / raises what it is told."""
    def __init__(self, deploy=None, cleanup=None, verify=None, probe=None):
        self.calls, self.script = [], dict(deploy=deploy, cleanup=cleanup,
                                           verify_clean=verify, probe=probe)

    def _do(self, name, res, step):
        self.calls.append(name)
        step(f"{name}-step")
        out = self.script[name]
        if isinstance(out, Exception):
            raise out
        return out if out is not None else RunResult(ok=True)

    def deploy(self, res, step): return self._do("deploy", res, step)
    def cleanup(self, res, step): return self._do("cleanup", res, step)
    def verify_clean(self, res, step): return self._do("verify_clean", res, step)
    def probe(self, res, step): return self._do("probe", res, step)


# ---- 1. ready -> active -----------------------------------------------------------------

def test_ready_becomes_active_when_start_has_passed(world):
    db = world[0]
    due = res(world, state="ready", start=T(9), end=T(11))
    exactly = res(world, state="ready", start=T(9), end=T(11))          # start_at == now counts
    later = res(world, state="ready", start=T(9, 30), end=T(11))
    assert engine._promote_ready_to_active(db, NOW) == 2
    assert [state(db, i) for i in (due, exactly, later)] == ["active", "active", "ready"]
    assert engine._promote_ready_to_active(db, NOW) == 0                # nothing left to do


def test_only_ready_reservations_are_promoted(world):
    db = world[0]
    ids = [res(world, state=s, start=T(8), end=T(12)) for s in ("queued", "provisioning", "active",
                                                                  "cleaning", "released")]
    assert engine._promote_ready_to_active(db, NOW) == 0
    assert [state(db, i) for i in ids] == ["queued", "provisioning", "active", "cleaning", "released"]


# ---- 2. active -> cleaning --------------------------------------------------------------

def test_active_expires_into_cleaning_and_queues_exactly_one_cleanup_job(world):
    db = world[0]
    over = res(world, state="active", start=T(7), end=T(9))             # end_at == now counts
    running = res(world, state="active", start=T(8), end=T(10))
    assert engine._expire_active(db, NOW) == 1
    assert (state(db, over), state(db, running)) == ("cleaning", "active")
    [job] = jobs(db)
    assert (job["reservation_id"], job["kind"], job["state"], job["attempt"]) == \
           (over, "cleanup", "pending", 1)
    assert engine._expire_active(db, NOW) == 0 and len(jobs(db)) == 1   # not queued twice


def test_a_late_engine_catches_up_ready_to_cleaning_in_one_tick(world):
    """The engine was down: a ready reservation whose whole slot has passed goes
    ready -> active -> cleaning -> released in a single tick."""
    db = world[0]
    rid = res(world, state="ready", start=T(6), end=T(8))
    out = tick(db)
    assert state(db, rid) == "released"
    assert (out.activated, out.expired, out.released) == (1, 1, 1)


# ---- 3. queued -> provisioning ------------------------------------------------------------

def test_same_class_starts_five_minutes_before_start_at(world):
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))                  # lead 5 -> due at 09:00
    assert engine._start_due(db, NOW - timedelta(seconds=1), CFG) == 0  # one second early
    assert state(db, rid) == "queued" and jobs(db) == []
    assert engine._start_due(db, NOW, CFG) == 1                         # exactly on time
    assert state(db, rid) == "provisioning"
    [job] = jobs(db)
    assert (job["reservation_id"], job["kind"], job["state"]) == (rid, "deploy", "pending")


def test_different_class_needs_the_fifteen_minute_reflash_lead(world):
    db = world[0]
    rid = res(world, uc="b", start=T(9, 20), end=T(10))                 # lead 15 -> due at 09:05
    assert engine._start_due(db, NOW, CFG) == 0                         # 09:00: five minutes early
    assert engine._start_due(db, NOW + timedelta(minutes=5), CFG) == 1  # 09:05: exactly on time
    assert state(db, rid) == "provisioning"


def test_lead_is_derived_from_the_dpus_actual_class_at_tick_time(world):
    db, sid, _ = world
    rid = res(world, uc="b", start=T(9, 12), end=T(10))                 # needs cls-b
    # DPU runs cls-a: reflash lead 15 -> due 08:57, so it is already due at 09:00 ...
    # ... but if the DPU already runs cls-b the lead is 5 -> due 09:07, not yet.
    models.set_dpu_hw_class(db, sid, "cls-b")
    assert engine._start_due(db, NOW, CFG) == 0 and state(db, rid) == "queued"
    models.set_dpu_hw_class(db, sid, "cls-a")                           # the DPU changed under us
    assert engine._start_due(db, NOW, CFG) == 1 and state(db, rid) == "provisioning"


def test_a_use_case_without_a_class_never_needs_the_long_lead(world):
    db = world[0]
    rid = res(world, uc="n", start=T(9, 5), end=T(10))
    assert engine._start_due(db, NOW, CFG) == 1 and state(db, rid) == "provisioning"


def test_a_setup_without_a_dpu_always_uses_the_short_lead(db):
    sid = mk_setup(db, "nodpu", ["wire"], has_dpu=False)
    cid = mk_usecase(db, "uc", ["wire"], hw_class="cls-z")
    rid = mk_res(db, sid, cid, T(9, 5), T(10))
    assert engine._start_due(db, NOW, CFG) == 1 and state(db, rid) == "provisioning"


def test_not_started_while_the_setup_is_still_held(world):
    db = world[0]
    held = res(world, state="cleaning", start=T(7), end=T(8, 50))
    nxt = res(world, uc="a", start=T(9, 5), end=T(10))
    assert engine._start_due(db, NOW, CFG) == 0 and state(db, nxt) == "queued"
    db.execute("UPDATE reservations SET state = 'released' WHERE id = ?", (held,))
    assert engine._start_due(db, NOW, CFG) == 1 and state(db, nxt) == "provisioning"


@pytest.mark.parametrize("held_state", ["provisioning", "ready", "active", "cleaning", "quarantined"])
def test_every_held_state_blocks_the_next_start(world, held_state):
    db = world[0]
    res(world, state=held_state, start=T(7), end=T(8))
    nxt = res(world, uc="a", start=T(9, 5), end=T(10))
    assert engine._start_due(db, NOW, CFG) == 0 and state(db, nxt) == "queued"


@pytest.mark.parametrize("availability", ["quarantined", "maintenance"])
def test_an_unavailable_setup_keeps_its_queue_waiting(world, availability):
    db, sid, _ = world
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    models.set_setup_availability(db, sid, availability)
    assert engine._start_due(db, NOW, CFG) == 0 and state(db, rid) == "queued"
    models.set_setup_availability(db, sid, "available")
    assert engine._start_due(db, NOW, CFG) == 1


def test_only_the_first_queued_reservation_per_setup_starts(world):
    db = world[0]
    first = res(world, uc="a", start=T(9, 5), end=T(10))
    second = res(world, uc="a", start=T(9, 5), end=T(10))               # same moment (artificial)
    assert engine._start_due(db, NOW, CFG) == 1
    assert (state(db, first), state(db, second)) == ("provisioning", "queued")


def test_each_setup_starts_independently(world):
    db, _, ids = world
    other = mk_setup(db, "tb2", ["wire"], dpu_hw_class="cls-a")
    a = res(world, uc="a", start=T(9, 5), end=T(10))
    b = res(world, uc="a", start=T(9, 5), end=T(10), sid=other)
    assert engine._start_due(db, NOW, CFG) == 2
    assert (state(db, a), state(db, b)) == ("provisioning", "provisioning")


def test_a_second_engine_with_a_stale_view_cannot_start_it_twice(world, monkeypatch):
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    stale = scheduler.startable_queued(db)                              # both engines saw it queued
    assert engine._start_due(db, NOW, CFG) == 1
    monkeypatch.setattr(scheduler, "startable_queued", lambda _db: stale)
    assert engine._start_due(db, NOW, CFG) == 0                         # held now, and CAS would refuse
    assert len([j for j in jobs(db) if j["kind"] == "deploy"]) == 1 and state(db, rid) == "provisioning"


def test_the_stale_view_is_refused_even_if_the_hold_check_passes(world, monkeypatch):
    """Belt and braces: with the held-by guard blinded, the state compare-and-set still refuses."""
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    stale = scheduler.startable_queued(db)
    engine._start_due(db, NOW, CFG)
    monkeypatch.setattr(scheduler, "startable_queued", lambda _db: stale)
    monkeypatch.setattr(models, "held_by", lambda *_a: None)
    assert engine._start_due(db, NOW, CFG) == 0
    assert len([j for j in jobs(db) if j["kind"] == "deploy"]) == 1
    assert not db.in_transaction


# ---- 4. running jobs: deploy --------------------------------------------------------------

def test_successful_deploy_makes_the_reservation_ready_and_records_the_dpu_class(world):
    db, sid, _ = world
    rid = res(world, uc="b", start=T(9, 10), end=T(10))
    out = tick(db, NOW + timedelta(minutes=0), cfg=SchedConfig(hw_class_lead_min=15))
    assert state(db, rid) == "ready" and out.ready == 1
    [job] = jobs(db)
    assert job["state"] == "succeeded" and job["started_at"] and job["finished_at"]
    assert [s["name"] for s in models.job_steps(job)] == list(DEPLOY_STEPS)
    assert dpu_class(db, sid) == "cls-b"                                # reflashed -> recorded


def test_a_use_case_without_a_class_leaves_the_dpu_class_alone(world):
    db, sid, _ = world
    res(world, uc="n", start=T(9, 5), end=T(10))
    tick(db)
    assert dpu_class(db, sid) == "cls-a"


def test_deploy_is_audited(world):
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    tick(db)
    [event] = [e for e in models.list_audit_events(db) if e["action"] == "deploy"]
    assert (event["actor"], event["object_type"], event["object_id"]) == ("engine", "reservation", rid)
    detail = models.from_json(event["detail"])
    assert detail["ok"] is True and detail["job"] == jobs(db)[0]["id"] and detail["attempt"] == 1


def test_a_failed_deploy_is_retried_as_a_fresh_job_before_giving_up(world, monkeypatch):
    """B-25: an observed deploy failure queues a fresh deploy job (attempt + 1) and keeps the
    reservation `provisioning`; the replay waits for the next tick (the pending list is a
    snapshot taken before the retry is created)."""
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=3))
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    out = tick(db, rn=mock(fail={"deploy"}))
    assert state(db, rid) == "provisioning" and out.failed == 0 and out.retried == 1
    assert [(j["kind"], j["attempt"], j["state"]) for j in jobs(db, rid)] == \
        [("deploy", 1, "failed"), ("deploy", 2, "pending")]
    out = tick(db, rn=mock(fail={"deploy"}))            # only the queued retry runs now
    assert out.jobs_run == 1 and out.retried == 1 and state(db, rid) == "provisioning"
    assert [j["attempt"] for j in jobs(db, rid) if j["kind"] == "deploy"] == [1, 2, 3]


def test_retries_are_exhausted_by_counting_attempts(world, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=2))
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    rn = mock(fail={"deploy"})
    assert tick(db, rn=rn).failed == 0 and state(db, rid) == "provisioning"   # attempt 1 fails
    out = tick(db, rn=rn)                                                     # attempt 2 fails
    assert out.failed == 1 and out.retried == 0 and state(db, rid) == "failed"
    deploy_jobs = [j for j in jobs(db, rid) if j["kind"] == "deploy"]
    assert [j["attempt"] for j in deploy_jobs] == [1, 2]
    assert all(j["state"] == "failed" for j in deploy_jobs)


def test_when_retries_are_exhausted_a_cleanup_job_is_queued_and_releases_the_reservation(
        world, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    db, sid, _ = world
    rid = res(world, uc="b", start=T(9, 10), end=T(10))
    out = tick(db, rn=mock(fail={"deploy"}), cfg=SchedConfig(hw_class_lead_min=15))
    row = models.reservation_by_id(db, rid)
    assert row["state"] == "failed" and out.failed == 1 and out.ready == 0
    assert "create_vms" in row["failure_reason"]
    assert dpu_class(db, sid) == "cls-a"                                # nothing was flashed
    assert models.held_by(db, sid) is None                              # next booking can start
    assert [(j["kind"], j["state"]) for j in jobs(db, rid)] == \
        [("deploy", "failed"), ("cleanup", "pending")]
    [deploy_job] = [j for j in jobs(db, rid) if j["kind"] == "deploy"]
    assert [s["name"] for s in models.job_steps(deploy_job)] == ["lease_ips", "dpu_prepare", "create_vms"]
    [event] = [e for e in models.list_audit_events(db) if e["action"] == "deploy"]
    assert models.from_json(event["detail"])["ok"] is False
    out = tick(db, rn=mock())                    # the automatic cleanup now tears it down
    assert state(db, rid) == "released" and out.released == 1


def test_a_crashing_runner_fails_the_job_but_not_the_tick(world, caplog, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    with caplog.at_level(logging.ERROR, logger="daolab.engine"):
        out = tick(db, rn=Fake(deploy=RuntimeError("boom")))
    assert state(db, rid) == "failed" and out.failed == 1
    assert jobs(db)[0]["error"] == "RuntimeError: boom"
    assert "reservation %s" % rid in caplog.text or "boom" in caplog.text


def test_the_unfinished_ansible_backend_fails_jobs_with_a_clear_message(world, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_retries=1))
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    tick(db, rn=runner.AnsibleRunner())
    assert state(db, rid) == "failed"
    assert "B-84" in jobs(db)[0]["error"]


# ---- 4. running jobs: cleanup --------------------------------------------------------------

def test_cleanup_that_verifies_clean_releases_the_reservation(world):
    db, sid, _ = world
    rid = res(world, state="active", start=T(7), end=T(9))
    out = tick(db)
    assert state(db, rid) == "released" and out.released == 1 and out.expired == 1
    [job] = jobs(db)
    assert job["kind"] == "cleanup" and job["state"] == "succeeded"
    assert [s["name"] for s in models.job_steps(job)] == list(CLEANUP_STEPS + VERIFY_CLEAN_STEPS)
    assert models.held_by(db, sid) is None
    [event] = [e for e in models.list_audit_events(db) if e["action"] == "cleanup"]
    assert models.from_json(event["detail"])["residue"] == []


def test_end_of_slot_cleanup_kills_the_dpu_user_session_first(world):
    """B-53: the cleanup job queued at `end_at` starts by terminating the DPU's `lab` user
    sessions (plan/09 §7/§9), so access is revoked at the end of the slot whatever the rest of
    teardown does. The mock records the step."""
    db, sid, _ = world
    rid = res(world, state="active", start=T(7), end=T(9))
    out = tick(db)                                    # end_at passed -> cleaning + cleanup job
    assert out.expired == 1
    [job] = jobs(db)
    assert job["kind"] == "cleanup"
    steps = [s["name"] for s in models.job_steps(job)]
    assert steps == list(CLEANUP_STEPS + VERIFY_CLEAN_STEPS)
    assert steps[0] == "terminate-user"
    assert state(db, rid) == "released"


def test_residue_after_cleanup_quarantines_the_reservation(world):
    db, sid, _ = world
    rid = res(world, state="active", start=T(7), end=T(9))
    out = tick(db, rn=mock(fail={"cleanup"}))
    assert state(db, rid) == "quarantined" and out.quarantined == 1 and out.released == 0
    [job] = jobs(db)
    assert job["state"] == "failed"
    assert job["error"].startswith("residue after cleanup:") and f"daolab-r{rid}-backend" in job["error"]
    assert models.held_by(db, sid)["id"] == rid                         # still holds the setup
    [event] = [e for e in models.list_audit_events(db) if e["action"] == "cleanup"]
    detail = models.from_json(event["detail"])
    assert detail["ok"] is False and len(detail["residue"]) == 2


def test_a_quarantined_reservation_blocks_the_next_start(world):
    db = world[0]
    res(world, state="active", start=T(7), end=T(9))
    nxt = res(world, uc="a", start=T(9, 5), end=T(10))
    tick(db, rn=mock(fail={"cleanup"}))
    tick(db, rn=mock(fail={"cleanup"}))
    assert state(db, nxt) == "queued"


def test_verify_runs_even_if_the_cleanup_itself_reports_failure(world):
    """plan/09 §9: Ansible teardown is best effort; the direct verify decides."""
    db = world[0]
    rid = res(world, state="cleaning")
    models.create_job(db, rid, "cleanup")
    rn = Fake(cleanup=RunResult(ok=False, error="playbook rc=2"))
    tick(db, rn=rn)
    assert rn.calls == ["cleanup", "verify_clean"]
    assert state(db, rid) == "released" and jobs(db)[0]["state"] == "succeeded"


def test_a_crash_in_cleanup_is_still_verified(world):
    db = world[0]
    rid = res(world, state="cleaning")
    models.create_job(db, rid, "cleanup")
    rn = Fake(cleanup=RuntimeError("xe vm-uninstall hung"))
    tick(db, rn=rn)
    assert rn.calls == ["cleanup", "verify_clean"] and state(db, rid) == "released"


def test_if_cleanliness_cannot_be_proven_the_reservation_is_quarantined(world):
    db = world[0]
    rid = res(world, state="cleaning")
    models.create_job(db, rid, "cleanup")
    tick(db, rn=Fake(verify=RuntimeError("ssh to dom0 failed")))
    assert state(db, rid) == "quarantined"
    assert "ssh to dom0 failed" in jobs(db)[0]["error"]


def test_a_failed_verify_without_a_residue_list_still_quarantines(world):
    db = world[0]
    rid = res(world, state="cleaning")
    models.create_job(db, rid, "cleanup")
    tick(db, rn=Fake(verify=RunResult(ok=False, error="could not reach xapi")))
    assert state(db, rid) == "quarantined" and "could not reach xapi" in jobs(db)[0]["error"]


def test_residue_listed_by_a_verify_that_claims_ok_still_quarantines(world):
    """`ok` alone is not trusted: any residue at all means the setup is not clean."""
    db = world[0]
    rid = res(world, state="cleaning")
    models.create_job(db, rid, "cleanup")
    tick(db, rn=Fake(verify=RunResult(ok=True, residue=["vf 0000:03:00.2 still bound"])))
    assert state(db, rid) == "quarantined"
    assert "vf 0000:03:00.2 still bound" in jobs(db)[0]["error"]


def test_a_freed_setup_is_started_on_the_next_tick_not_the_same_one(world):
    db = world[0]
    res(world, state="active", start=T(7), end=T(8, 55))
    nxt = res(world, uc="a", start=T(9, 5), end=T(10))
    out = tick(db)                                                        # 09:00: cleanup runs, setup freed
    assert out.released == 1 and out.started == 0 and state(db, nxt) == "queued"
    out = tick(db, NOW + timedelta(seconds=60))
    assert out.started == 1 and state(db, nxt) == "ready"


# ---- 4. running jobs: the rest --------------------------------------------------------------

def test_a_job_whose_reservation_moved_on_fails_without_calling_the_runner(world):
    db = world[0]
    rid = res(world, state="released")
    models.create_job(db, rid, "deploy")
    rn = Fake()
    out = tick(db, rn=rn)
    assert rn.calls == [] and out.jobs_run == 1
    job = jobs(db)[0]
    assert job["state"] == "failed" and "reservation is released, expected provisioning" in job["error"]


def test_health_probe_records_the_job_and_leaves_the_reservation_alone(world):
    db = world[0]
    rid = res(world, state="active", start=T(8), end=T(12))
    models.create_job(db, rid, "health_probe")
    tick(db)
    assert state(db, rid) == "active"
    job = jobs(db)[0]
    assert job["state"] == "succeeded" and [s["name"] for s in models.job_steps(job)] == ["ping_vms", "check_dpu_app"]


def test_health_probe_on_a_queued_reservation_is_refused(world):
    db = world[0]
    rid = res(world)
    models.create_job(db, rid, "health_probe")
    tick(db)
    assert jobs(db)[0]["state"] == "failed" and "expected ready or active" in jobs(db)[0]["error"]


def test_reconcile_jobs_are_deferred_to_b72(world):
    db = world[0]
    rid = res(world, state="quarantined")
    models.create_job(db, rid, "reconcile")
    tick(db)
    assert jobs(db)[0]["state"] == "failed" and "B-72" in jobs(db)[0]["error"]
    assert state(db, rid) == "quarantined"


def test_pending_jobs_run_oldest_first(world):
    db = world[0]
    a = res(world, state="active", start=T(8), end=T(12))
    b = res(world, state="active", start=T(8), end=T(12))
    order = []

    class Spy(Fake):
        def probe(self, r, step):
            order.append(r["id"])
            return RunResult(ok=True)
    models.create_job(db, b, "health_probe")
    models.create_job(db, a, "health_probe")
    tick(db, rn=Spy())
    assert order == [b, a]                                              # by job id, not reservation id


def test_a_job_another_engine_already_took_is_not_run_twice(world):
    db = world[0]
    rid = res(world, state="active", start=T(8), end=T(12))
    jid = models.create_job(db, rid, "health_probe")
    models.transition_job(db, jid, "running")                           # the other engine
    rn = Fake()
    out = tick(db, rn=rn)
    assert rn.calls == [] and out.jobs_run == 0


def test_losing_the_pending_to_running_race_skips_the_job(world, monkeypatch):
    db = world[0]
    rid = res(world, state="active", start=T(8), end=T(12))
    models.create_job(db, rid, "health_probe")
    monkeypatch.setattr(models, "transition_job",
                        lambda *a, **k: (_ for _ in ()).throw(JobError("lost the race")))
    rn = Fake()
    assert tick(db, rn=rn).jobs_run == 0 and rn.calls == []


# ---- crash safety --------------------------------------------------------------------------

def test_finishing_a_job_is_all_or_nothing(world, monkeypatch):
    """If the engine dies while recording a result (here: the audit write blows up), the job is
    still `running` and the reservation still `provisioning` — nothing half-done is committed."""
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    monkeypatch.setattr(models, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError, match="disk full"):
        tick(db)
    assert state(db, rid) == "provisioning"
    assert jobs(db)[0]["state"] == "running"
    assert not db.in_transaction


def test_a_failure_while_starting_leaves_no_half_started_reservation(world, monkeypatch):
    db = world[0]
    rid = res(world, uc="a", start=T(9, 5), end=T(10))
    monkeypatch.setattr(models, "create_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        engine._start_due(db, NOW, CFG)
    assert state(db, rid) == "queued" and jobs(db) == [] and not db.in_transaction


# ---- the whole lifecycle across ticks ----------------------------------------------------------

def test_full_lifecycle_across_ticks_and_back_to_back_bookings(world):
    db, sid, _ = world
    first = res(world, uc="a", start=T(10), end=T(11))
    second = res(world, uc="a", start=T(11, 15), end=T(12))             # gap = cleanup 10 + lead 5
    rn = mock()
    at = lambda h, m=0: engine.tick(db, T(h, m), rn, CFG)

    at(9, 50)
    assert (state(db, first), state(db, second)) == ("queued", "queued")      # 09:55 is the first due moment
    at(9, 55)
    assert state(db, first) == "ready"                                         # deployed ahead of time
    at(10, 0)
    assert state(db, first) == "active"
    at(10, 59)
    assert state(db, first) == "active"
    out = at(11, 0)
    assert state(db, first) == "released" and out.expired == 1 and out.released == 1
    at(11, 5)
    assert state(db, second) == "queued"                                       # 11:10 is its due moment
    at(11, 10)
    assert state(db, second) == "ready"
    at(11, 15)
    assert state(db, second) == "active"
    at(12, 0)
    assert state(db, second) == "released"
    kinds = [(j["reservation_id"], j["kind"], j["state"]) for j in jobs(db)]
    assert kinds == [(first, "deploy", "succeeded"), (first, "cleanup", "succeeded"),
                     (second, "deploy", "succeeded"), (second, "cleanup", "succeeded")]


def test_a_quiet_tick_does_nothing_and_leaves_no_transaction_open(world):
    db = world[0]
    res(world, uc="a", start=T(20), end=T(21))
    out = tick(db)
    assert out.total == 0 and not db.in_transaction


def test_tick_result_total_counts_the_work(world):
    db = world[0]
    res(world, state="ready", start=T(8), end=T(12))
    res(world, uc="a", start=T(9, 5), end=T(10), sid=mk_setup(db, "tb2", ["wire"], dpu_hw_class="cls-a"))
    out = tick(db)
    assert out.activated == 1 and out.started == 1 and out.jobs_run == 1 and out.ready == 1
    assert out.total == 3


# ---- config + lead helper ----------------------------------------------------------------------

def test_sched_config_from_settings_copies_every_field():
    s = dataclasses.replace(config.settings, hw_class_lead_min=21, provision_lead_min=7,
                            sched_horizon_hours=48, cleanup_min=13, slot_min=20)
    assert SchedConfig.from_settings(s) == SchedConfig(21, 7, 48, 13, 20)


def test_start_lead_min_reads_the_live_dpu_class(world):
    db, sid, _ = world
    assert scheduler.start_lead_min(db, sid, "cls-a", CFG) == 5
    assert scheduler.start_lead_min(db, sid, "cls-b", CFG) == 15
    assert scheduler.start_lead_min(db, sid, None, CFG) == 5
    models.set_dpu_hw_class(db, sid, "cls-b")
    assert scheduler.start_lead_min(db, sid, "cls-b", CFG) == 5


def test_set_dpu_hw_class_is_a_no_op_without_a_dpu(db):
    sid = mk_setup(db, "nodpu", ["wire"], has_dpu=False)
    assert models.set_dpu_hw_class(db, sid, "x") is False


# ---- loop + command line -----------------------------------------------------------------------

def test_run_loop_ticks_and_sleeps_between_ticks(world):
    db = world[0]
    sleeps = []
    assert engine.run_loop(db, 42, mock(), sleep=sleeps.append, max_ticks=3, clock=lambda: NOW) == 3
    assert sleeps == [42, 42]                                           # no sleep after the last tick


def test_run_loop_survives_a_crashing_tick(world, monkeypatch, caplog):
    db = world[0]
    calls = []

    def flaky(_db, _now, _rn=None, _cfg=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return engine.TickResult()
    monkeypatch.setattr(engine, "tick", flaky)
    with caplog.at_level(logging.ERROR, logger="daolab.engine"):
        assert engine.run_loop(db, 0, mock(), sleep=lambda s: None, max_ticks=3, clock=lambda: NOW) == 3
    assert len(calls) == 3 and "tick failed" in caplog.text


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    path = str(tmp_path / "e.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, db_path=path, runner="mock", mock_step_sec=0.0, mock_fail=""))
    conn = models.connect(path)
    models.init_db(conn)
    return conn


def test_main_runs_one_tick_and_exits(cli_env, capsys):
    assert engine.main([]) == 0
    assert "tick done" in capsys.readouterr().out


def test_main_does_real_work_from_the_command_line(cli_env, capsys):
    conn = cli_env
    models.insert(conn, "users", {"username": "u", "token_hash": "h", "ssh_pubkey": "k"})
    sid = mk_setup(conn, "tb", ["wire"], dpu_hw_class="cls-a")
    cid = mk_usecase(conn, "ucA", ["wire"], hw_class="cls-a")
    rid = mk_res(conn, sid, cid, T(10), T(11), state="active")
    conn.execute("UPDATE reservations SET end_at = '2000-01-01T00:00:00Z' WHERE id = ?", (rid,))
    assert engine.main([]) == 0
    assert state(conn, rid) == "released"


def test_main_refuses_an_out_of_date_database(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, db_path=path, runner="mock"))
    conn = models.connect(path)                                          # never initialised: v0
    assert engine.main([]) == 1
    err = capsys.readouterr().err
    assert "daolab init" in err and "v0" in err
    conn.close()


def test_main_rejects_a_bad_runner_setting(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, runner="docker"))
    assert engine.main([]) == 1 and "DAOLAB_RUNNER" in capsys.readouterr().err


def test_main_loop_stops_cleanly_on_ctrl_c(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(engine, "run_loop", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert engine.main(["--loop"]) == 0 and "engine stopped" in capsys.readouterr().out


def test_main_loop_passes_the_configured_interval(cli_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, engine_interval_sec=17))
    monkeypatch.setattr(engine, "run_loop", lambda db, interval, rn: seen.update(interval=interval))
    assert engine.main(["--loop"]) == 0 and seen["interval"] == 17
