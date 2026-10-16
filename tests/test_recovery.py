"""B-26: crash recovery - a `running` job whose engine died goes back to `pending` (plan/09 §10)."""
import dataclasses
import os
from datetime import timedelta

import pytest

from service import config, engine, models
from service.models import JobError
from service.runner import CLEANUP_STEPS, DEPLOY_STEPS, VERIFY_CLEAN_STEPS, MockRunner
from service.scheduler import SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

CFG = SchedConfig()
TIMEOUT = 1800


def ts(delta_sec):
    """A timestamp `delta_sec` before the frozen NOW."""
    return models.format_ts(NOW - timedelta(seconds=delta_sec))


@pytest.fixture
def world(db):
    sid = mk_setup(db, "tb", ["wire"], dpu_hw_class="cls-a")
    cid = mk_usecase(db, "ucA", ["wire"], hw_class="cls-a")
    return db, sid, cid


def crashed_job(db, rid, kind="deploy", idle_sec=TIMEOUT + 60, steps=("lease_ips", "dpu_prepare"),
                attempt=1):
    """A job some engine started and then died on: running, a few steps done, silent for idle_sec."""
    jid = models.create_job(db, rid, kind, attempt=attempt)
    models.transition_job(db, jid, "running")
    for name in steps:
        models.append_job_step(db, jid, name)
    db.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts(idle_sec), jid))
    return jid


def tick(db, **kw):
    return engine.tick(db, NOW, MockRunner(step_sec=0.0), CFG, **kw)


def job(db, jid):
    return models.job_by_id(db, jid)


# ---- the model helpers ---------------------------------------------------------------------

def test_a_silent_running_job_is_stale_and_goes_back_to_pending(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid, attempt=3)
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == [jid]
    row = job(db, jid)
    assert row["state"] == "pending" and row["started_at"] is None
    assert row["attempt"] == 3 and row["error"] is None and row["finished_at"] is None


def test_the_steps_done_so_far_are_kept_and_a_marker_is_added(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid, steps=("lease_ips", "dpu_prepare"))
    models.requeue_stale_jobs(db, ts(TIMEOUT))
    steps = models.job_steps(job(db, jid))
    assert [s["name"] for s in steps] == ["lease_ips", "dpu_prepare", "requeued"]
    assert steps[-1]["state"] == "skipped" and "no activity since" in steps[-1]["detail"]


def test_recent_activity_means_the_job_is_alive(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid, idle_sec=TIMEOUT - 60)
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == []
    assert job(db, jid)["state"] == "running"


def test_the_boundary_is_strict(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    exactly = crashed_job(db, rid, idle_sec=TIMEOUT)
    one_over = crashed_job(db, rid, idle_sec=TIMEOUT + 1, attempt=2)
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == [one_over]
    assert job(db, exactly)["state"] == "running"


@pytest.mark.parametrize("kind", models.JOB_KINDS)
def test_every_kind_of_job_can_be_recovered(world, kind):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="active")
    jid = crashed_job(db, rid, kind=kind)
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == [jid]


def test_only_running_jobs_are_ever_touched(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="active")
    pending = models.create_job(db, rid, "deploy")
    done = models.create_job(db, rid, "cleanup")
    models.transition_job(db, done, "running")
    models.transition_job(db, done, "succeeded")
    bad = models.create_job(db, rid, "deploy", attempt=2)
    models.transition_job(db, bad, "running")
    models.transition_job(db, bad, "failed", error="x")
    db.execute("UPDATE jobs SET updated_at = ?", (ts(10 * TIMEOUT),))
    before = [tuple(r) for r in db.execute("SELECT * FROM jobs ORDER BY id")]
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == []
    assert [tuple(r) for r in db.execute("SELECT * FROM jobs ORDER BY id")] == before
    assert job(db, pending)["state"] == "pending"


def test_a_job_that_came_back_to_life_is_left_alone(world):
    """We read the job as stale, but before we requeue it the engine records another step."""
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid)
    [seen] = models.stale_running_jobs(db, ts(TIMEOUT))
    models.append_job_step(db, jid, "create_vms")                        # alive after all
    assert models.requeue_job(db, seen) is False
    assert job(db, jid)["state"] == "running"
    assert [s["name"] for s in models.job_steps(job(db, jid))][-1] == "create_vms"


def test_requeue_stale_jobs_reports_only_the_jobs_it_really_requeued(world, monkeypatch):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    dead = crashed_job(db, rid)
    revived = crashed_job(db, rid, attempt=2)
    snapshot = models.stale_running_jobs(db, ts(TIMEOUT))                 # both look dead ...
    models.append_job_step(db, revived, "create_vms")                     # ... but one is alive
    monkeypatch.setattr(models, "stale_running_jobs", lambda *_a: snapshot)
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == [dead]
    assert job(db, revived)["state"] == "running"


def test_a_job_that_finished_meanwhile_is_left_alone(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid)
    [seen] = models.stale_running_jobs(db, ts(TIMEOUT))
    models.transition_job(db, jid, "succeeded")
    assert models.requeue_job(db, seen) is False and job(db, jid)["state"] == "succeeded"


def test_requeueing_twice_does_nothing_the_second_time(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    crashed_job(db, rid)
    assert len(models.requeue_stale_jobs(db, ts(TIMEOUT))) == 1
    assert models.requeue_stale_jobs(db, ts(TIMEOUT)) == []


def test_the_ordinary_state_machine_still_forbids_running_to_pending(world):
    """Recovery is its own explicit exception, not a general transition."""
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid)
    with pytest.raises(JobError):
        models.transition_job(db, jid, "pending")
    assert models.JOB_TRANSITIONS["running"] == ("succeeded", "failed")


def test_a_requeued_job_can_be_claimed_and_run_again(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid)
    models.requeue_stale_jobs(db, ts(TIMEOUT))
    models.transition_job(db, jid, "running")
    assert job(db, jid)["state"] == "running" and job(db, jid)["started_at"] is not None
    models.append_job_step(db, jid, "lease_ips")                          # steps work again


# ---- the engine: "kill mid-job -> next tick resumes" ------------------------------------------

def test_a_deploy_interrupted_by_a_crash_is_resumed_by_the_next_tick(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    jid = crashed_job(db, rid, steps=("lease_ips", "dpu_prepare"))
    out = tick(db)
    assert out.recovered == 1 and out.ready == 1
    assert models.reservation_by_id(db, rid)["state"] == "ready"
    row = job(db, jid)
    assert row["state"] == "succeeded" and row["attempt"] == 1
    names = [s["name"] for s in models.job_steps(row)]
    assert names == ["lease_ips", "dpu_prepare", "requeued", *DEPLOY_STEPS]


def test_a_cleanup_interrupted_by_a_crash_is_resumed_by_the_next_tick(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(8), T(9), state="cleaning")
    jid = crashed_job(db, rid, kind="cleanup", steps=("terminate_sessions",))
    out = tick(db)
    assert out.recovered == 1 and out.released == 1
    assert models.reservation_by_id(db, rid)["state"] == "released"
    names = [s["name"] for s in models.job_steps(job(db, jid))]
    assert names == ["terminate_sessions", "requeued", *CLEANUP_STEPS, *VERIFY_CLEAN_STEPS]


def test_recovery_unblocks_the_setup_in_the_same_tick(world):
    """B-24: a dead engine's running job blocked the setup; once recovered, the queue moves."""
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(8), T(9), state="cleaning")
    dead = crashed_job(db, rid, kind="cleanup", steps=())
    waiting = models.create_job(db, rid, "cleanup", attempt=2)
    out = tick(db)
    assert out.recovered == 1 and out.deferred == 0
    assert job(db, dead)["state"] == "succeeded"                          # oldest id runs first
    assert job(db, waiting)["state"] == "failed"                          # reservation already released
    assert models.reservation_by_id(db, rid)["state"] == "released"


def test_a_live_job_is_not_recovered_and_keeps_blocking_its_setup(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(8), T(9), state="cleaning")
    live = crashed_job(db, rid, kind="cleanup", idle_sec=60, steps=("terminate_sessions",))
    waiting = models.create_job(db, rid, "cleanup", attempt=2)
    out = tick(db)
    assert (out.recovered, out.deferred) == (0, 1)
    assert job(db, live)["state"] == "running" and job(db, waiting)["state"] == "pending"


def test_a_recovered_job_whose_reservation_moved_on_is_failed_not_run(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(8), T(9), state="released")
    jid = crashed_job(db, rid)
    out = tick(db)
    assert out.recovered == 1
    row = job(db, jid)
    assert row["state"] == "failed" and "reservation is released" in row["error"]


def test_a_second_tick_has_nothing_left_to_recover(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    crashed_job(db, rid)
    assert tick(db).recovered == 1 and tick(db).recovered == 0


def test_the_timeout_is_a_parameter(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    crashed_job(db, rid, idle_sec=120)
    assert tick(db, job_timeout_sec=300).recovered == 0
    assert tick(db, job_timeout_sec=60).recovered == 1


def test_the_default_timeout_comes_from_the_settings(world, monkeypatch):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    crashed_job(db, rid, idle_sec=600)
    assert config.settings.job_timeout_sec == 1800
    assert tick(db).recovered == 0                                         # 10 min < 30 min
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, job_timeout_sec=300))
    assert tick(db).recovered == 1


@pytest.mark.parametrize("bad", [0, -5])
def test_a_non_positive_timeout_is_refused(world, bad):
    """0 would requeue every running job on every tick, including healthy ones."""
    db = world[0]
    with pytest.raises(ValueError, match="DAOLAB_JOB_TIMEOUT_SEC"):
        tick(db, job_timeout_sec=bad)
    assert not db.in_transaction


def test_recovery_counts_as_work(world):
    db, sid, cid = world
    rid = mk_res(db, sid, cid, T(10), T(11), state="provisioning")
    crashed_job(db, rid)
    out = engine.TickResult(recovered=1)
    assert out.total == 1


def test_main_refuses_a_non_positive_timeout(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, db_path=path, runner="mock", mock_step_sec=0.0, job_timeout_sec=0))
    conn = models.connect(path)
    models.init_db(conn)
    assert engine.main([]) == 1 and "DAOLAB_JOB_TIMEOUT_SEC" in capsys.readouterr().err


def test_env_example_lists_the_timeout():
    root = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))
    text = open(os.path.join(root, ".env.example")).read()
    assert "DAOLAB_JOB_TIMEOUT_SEC=1800" in text
