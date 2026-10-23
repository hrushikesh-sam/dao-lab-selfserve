"""Engine — the loop that turns bookings into running reservations (B-23).

`python -m service.engine` runs ONE tick and exits (systemd-timer friendly);
`python -m service.engine --loop` ticks every DAOLAB_ENGINE_INTERVAL_SEC.

A tick walks the reservations in a fixed order:

  1. ready  -> active        when start_at has passed
  2. active -> cleaning      when end_at has passed (and queues a `cleanup` job whose first
                             step, `terminate-user`, kills the DPU's `lab` sessions so access
                             is revoked at the end of the slot — plan/09 §7, B-53)
  3. queued -> provisioning  at start_at - lead, if the setup is available and not held
                             (and, in the same transaction, creates the reservation's `vms`
                             rows and queues a `deploy` job)
  4. run the pending jobs, oldest first, through the runner (B-22)
  5. sweep orphans (B-73): destroy every daolab-r<id>-* VM whose reservation is terminal or
     gone and has no cleanup / reconcile job waiting, and release `leased` addresses still
     held by terminal reservations. Catches residue from crashes and manual runs.
  6. evaluate the health rules of every `active` reservation (B-67), store the verdict as its
     latest snapshot, and fire DAOLAB_WEBHOOK_URL when it flips to `down` (only then, not on
     every tick that stays down). Runs only with the telemetry stack switched on
     (DAOLAB_OTEL_ENDPOINT set); without it health falls back to the probe job (plan/09 §13).

A setup is freed by step 4 (cleanup verified clean -> released), after step 3 has already
looked at it, so the next booking on that setup starts on the NEXT tick. That costs at most
one DAOLAB_ENGINE_INTERVAL_SEC and is covered by the lead time the scheduler reserves before
every booking (plan/09 §6).

The engine keeps nothing in memory between ticks: every decision comes from the database and
the clock. Every state change is a compare-and-set (`models.transition_reservation`,
`models.transition_job`), so a second engine process can never double-fire a transition.
Runner calls block; ticks are serialised, so a long deploy simply delays the next tick.

Tracing (B-62): with DAOLAB_OTEL_ENDPOINT set, every job runs inside its own span. The span's
trace id is stored on the job (retries and the automatic cleanup of a failed deploy inherit it,
so one booking's whole story is one trace), every recorded step becomes a child span, and the
runner can hand `otel.traceparent()` to Ansible as TRACEPARENT. Off by default; no-op then.

Crash recovery (B-26): every tick starts by putting `running` jobs that have shown no activity
for DAOLAB_JOB_TIMEOUT_SEC (default 30 min) back to `pending`. Each step a runner records counts
as activity, so a slow but healthy job is not touched; a job whose engine died stops reporting
and is replayed (playbooks are idempotent). The timeout must be longer than the longest silent
stretch of a healthy job.

Single-flight per setup (B-24): a deploy, cleanup or reconcile job is only started if no other
such job is `running` on the same setup. The check and the pending -> running move happen in one
BEGIN IMMEDIATE transaction, so two engine processes cannot both win. A job that has to wait is
simply left `pending` and tried again next tick; it is not failed. Jobs on the same setup in one
tick run one after the other. Health probes are read-only and exempt. A job left `running` by a
crashed engine blocks its setup until crash recovery (B-26) puts it back.

What a finished job does to its reservation:
  * deploy ok      -> ready, and the DPU's hw_class is recorded (plan/01 invariant 6)
  * deploy failed  -> retried as a fresh deploy job (attempt + 1) until DAOLAB_MAX_RETRIES
                      observed failures (B-25); once exhausted -> failed, with the job error as
                      failure_reason, plus an automatic `cleanup` job to tear down whatever the
                      partial deploy left behind (plan/09 §10)
  * deploy job on a `ready`/`active` reservation is a REDEPLOY (POST .../redeploy, B-28): the
                      runner replays the deploy, the job records the outcome, and the
                      reservation keeps its state either way (no retries, no automatic cleanup):
                      a failed replay must not tear down a lab the user is still working in;
                      they can redeploy again or clean up.
  * cleanup        -> B-71: cleanup, then verify_clean regardless of the cleanup result
                      (plan/09 §9); while residue remains, reconcile + verify again, up to
                      DAOLAB_RECONCILE_ATTEMPTS times. Clean: the IP leases are released and
                      the reservation goes `released` (from `cleaning` or `failed`); if that
                      was the setup's last dirty reservation, a quarantined setup is available
                      again. Residue: the reservation's leased IPs are quarantined, the setup
                      is flagged `quarantined` (a `maintenance` flag is left alone), and the
                      reservation goes `quarantined` (from `cleaning`; a `failed` one stays
                      `failed`, there is no edge).
  * reconcile      -> reconcile then verify_clean, once (force-clean, B-74, queues these):
                      clean -> every lease of the reservation is released (quarantined ones
                      too), the reservation goes `released`, and the setup is available again
                      if nothing else on it is dirty; residue -> the job fails and the setup
                      stays (or goes back to) quarantined.
  * health_probe   -> only the job is recorded
"""
import argparse
import json
import logging
import re
import sqlite3
import signal
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config, health, ipam, migrations, models, otel, runner as runner_mod, scheduler
from .runner import RunResult

log = logging.getLogger("daolab.engine")

# The reservation state a job of each kind needs when it starts. If the reservation moved on
# (cancelled, admin override...) the job fails without calling the runner.
EXPECTED_STATES = {
    "deploy": ("provisioning", "ready", "active"),     # ready/active = redeploy
    "cleanup": ("cleaning", "failed"),
    "health_probe": ("ready", "active"),
    "reconcile": ("quarantined", "failed"),           # failed: residue after a failed deploy
}


@dataclass
class TickResult:
    activated: int = 0       # ready -> active
    expired: int = 0         # active -> cleaning (cleanup job queued)
    started: int = 0         # queued -> provisioning (deploy job queued)
    recovered: int = 0       # stale running jobs put back to pending (B-26)
    jobs_run: int = 0
    deferred: int = 0        # pending jobs left waiting because their setup is busy (B-24)
    retried: int = 0         # deploy failure -> fresh deploy job queued (B-25)
    ready: int = 0           # deploy ok
    redeployed: int = 0      # redeploy ok (reservation unchanged)
    failed: int = 0          # deploy failed for the last time
    released: int = 0        # cleanup verified clean
    quarantined: int = 0     # residue after cleanup (and reconcile attempts)
    reconciled: int = 0      # reconcile job verified clean -> released (B-71/B-72)
    orphan_vms: int = 0      # VMs of terminal / unknown reservations destroyed (B-73)
    orphan_leases: int = 0   # leased addresses of terminal reservations released (B-73)
    health_checked: int = 0  # active reservations whose health was evaluated (B-67)
    went_down: int = 0       # ... of which flipped to `down` (webhook fired)

    @property
    def total(self) -> int:
        return (self.recovered + self.activated + self.expired + self.started + self.jobs_run
                + self.orphan_vms + self.orphan_leases)


def _ts(now: datetime) -> str:
    return models.format_ts(now)


class _Skip(Exception):
    """Abandon a transaction quietly: the situation changed under us."""


@contextmanager
def _txn(db: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, rolled back if the body raises (the error propagates)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


# ---- 0. crash recovery -------------------------------------------------------------

def _recover_stale_jobs(db: sqlite3.Connection, now: datetime, timeout_sec: float) -> int:
    """Requeue `running` jobs with no activity for `timeout_sec` (B-26)."""
    if timeout_sec <= 0:
        raise ValueError(f"DAOLAB_JOB_TIMEOUT_SEC must be > 0, got {timeout_sec}")
    idle_before = models.format_ts(now - timedelta(seconds=timeout_sec))
    ids = models.requeue_stale_jobs(db, idle_before)   # each requeue is one atomic UPDATE
    for jid in ids:
        log.warning("job %s had no activity for %ss; back to pending", jid, timeout_sec)
    return len(ids)


# ---- 1. ready -> active -----------------------------------------------------------

def _promote_ready_to_active(db: sqlite3.Connection, now: datetime) -> int:
    n = 0
    rows = db.execute("SELECT id FROM reservations WHERE state = 'ready' AND start_at <= ? "
                      "ORDER BY start_at, id", (_ts(now),)).fetchall()
    for r in rows:
        try:
            models.transition_reservation(db, r["id"], "active", from_state="ready")
            n += 1
        except models.ReservationStateError:
            pass                                    # someone else moved it first
    return n


# ---- 2. active -> cleaning + cleanup job -----------------------------------------------

def _expire_active(db: sqlite3.Connection, now: datetime) -> int:
    n = 0
    rows = db.execute("SELECT id FROM reservations WHERE state = 'active' AND end_at <= ? "
                      "ORDER BY end_at, id", (_ts(now),)).fetchall()
    for r in rows:
        try:
            with _txn(db):
                models.transition_reservation(db, r["id"], "cleaning", from_state="active")
                models.create_job(db, r["id"], "cleanup")
            n += 1
        except models.ReservationStateError:
            pass                                    # someone else moved it first
    return n


# ---- 3. queued -> provisioning + deploy job -------------------------------------------

def _start_due(db: sqlite3.Connection, now: datetime, cfg: scheduler.SchedConfig) -> int:
    n = 0
    for r in scheduler.startable_queued(db):
        if models.held_by(db, r["setup_id"]) is not None:
            continue                                # the setup is still in use
        uc = models.usecase_by_id(db, r["usecase_id"])
        lead = scheduler.start_lead_min(db, r["setup_id"], uc["hw_class"], cfg)
        if models.parse_ts(r["start_at"]).timestamp() - lead * 60 > now.timestamp():
            continue                                # not due yet
        try:
            with _txn(db):
                if models.held_by(db, r["setup_id"]) is not None:   # re-check under the lock
                    raise _Skip
                models.transition_reservation(db, r["id"], "provisioning", from_state="queued")
                models.create_vms(db, r["id"])      # before any playbook: callbacks need them
                models.create_job(db, r["id"], "deploy")
            n += 1
        except (_Skip, models.ReservationStateError):
            pass
    return n


# ---- 4. run the pending jobs ----------------------------------------------------------

def _call(fn, ctx, sink, what: str) -> RunResult:
    """Run one runner function; a crash becomes a failed result instead of killing the tick."""
    try:
        return fn(ctx, sink)
    except Exception as e:                           # noqa: BLE001 — the engine must survive
        log.exception("runner %s crashed for reservation %s", what, ctx["id"])
        return RunResult(ok=False, error=f"{type(e).__name__}: {e}")


def _context(db, job, res) -> dict:
    """What the runner gets instead of a DB row: the reservation context (models) plus the job."""
    return {**models.reservation_context(db, res["id"]),
            "job_id": job["id"], "job_kind": job["kind"], "attempt": job["attempt"],
            "job_created_at": job["created_at"]}


def _audit(db, job, action: str, detail: dict) -> None:
    models.audit(db, "engine", action, "reservation", job["reservation_id"],
                 {"job": job["id"], "attempt": job["attempt"], **detail},
                 trace_id=job["trace_id"])


def _move(db, res_id: int, new_state: str, **kw) -> bool:
    try:
        models.transition_reservation(db, res_id, new_state, **kw)
        return True
    except models.ReservationStateError as e:
        log.warning("not moving reservation %s to %s: %s", res_id, new_state, e)
        return False


def _finish(db, job_id: int, result: RunResult) -> None:
    if result.ok:
        models.transition_job(db, job_id, "succeeded")
    else:
        models.transition_job(db, job_id, "failed", error=result.error or "failed")


def _run_deploy(db, rn, job, res, ctx, sink, out: TickResult) -> RunResult:
    redeploy = res["state"] != "provisioning"          # ready/active: replay inside a live slot
    result = _call(rn.deploy, ctx, sink, "deploy")
    with _txn(db):          # job result + reservation move + audit commit together or not at all
        _finish(db, job["id"], result)
        if redeploy:
            if result.ok:
                out.redeployed += 1
            else:
                log.warning("redeploy of reservation %s failed: %s", res["id"], result.error)
        elif result.ok:
            uc = models.usecase_by_id(db, res["usecase_id"])
            if uc["hw_class"] is not None:
                models.set_dpu_hw_class(db, res["setup_id"], uc["hw_class"])
            if _move(db, res["id"], "ready", from_state="provisioning"):
                out.ready += 1
        elif job["attempt"] < config.settings.max_retries:
            # B-25: replay the deploy as a brand-new job; the reservation stays provisioning.
            # A later tick picks it up (pending_jobs was snapshotted before this row existed).
            models.create_job(db, res["id"], "deploy", attempt=job["attempt"] + 1,
                              trace_id=job["trace_id"])
            out.retried += 1
        else:
            if _move(db, res["id"], "failed", from_state="provisioning",
                     failure_reason=result.error or "deploy failed"):
                out.failed += 1
            models.create_job(db, res["id"], "cleanup", trace_id=job["trace_id"])
        _audit(db, job, "deploy", {"ok": result.ok, "error": result.error, "redeploy": redeploy})
    return result


def _is_clean(verified: RunResult) -> bool:
    return verified.ok and not verified.residue       # `ok` alone is not trusted


def _residue_result(verified: RunResult, after: str) -> RunResult:
    if _is_clean(verified):
        return RunResult(ok=True)
    what = ", ".join(verified.residue) if verified.residue else (verified.error or "unknown")
    return RunResult(ok=False, error=f"residue after {after}: {what}", residue=verified.residue)


def _quarantine_setup(db, res) -> None:
    """Residue survived: block the reservation's addresses and stop new deploys on the setup
    (B-18 gate). An admin's `maintenance` flag is stronger and is left alone."""
    ipam.quarantine_reservation(db, res["id"])
    setup = db.execute("SELECT availability FROM setups WHERE id = ?",
                       (res["setup_id"],)).fetchone()
    if setup["availability"] == "available":
        models.set_setup_availability(db, res["setup_id"], "quarantined")


def _maybe_free_setup(db, setup_id: int) -> None:
    """A quarantined setup is available again once none of its reservations is dirty."""
    setup = db.execute("SELECT availability FROM setups WHERE id = ?", (setup_id,)).fetchone()
    if setup["availability"] == "quarantined" and not models.dirty_reservations(db, setup_id):
        models.set_setup_availability(db, setup_id, "available")


def _run_cleanup(db, rn, job, res, ctx, sink, out: TickResult) -> RunResult:
    cleaned = _call(rn.cleanup, ctx, sink, "cleanup")
    verified = _call(rn.verify_clean, ctx, sink, "verify_clean")   # whatever the cleanup said
    reconcile, attempts = getattr(rn, "reconcile", None), 0
    while not _is_clean(verified) and reconcile is not None \
            and attempts < config.settings.reconcile_attempts:
        attempts += 1                                # B-71: reconcile x N, verifying each time
        _call(reconcile, ctx, sink, "reconcile")
        verified = _call(rn.verify_clean, ctx, sink, "verify_clean")
    result = _residue_result(verified, "cleanup")
    with _txn(db):          # job result + reservation move + audit commit together or not at all
        _finish(db, job["id"], result)
        if result.ok:
            ipam.release(db, res["id"])
            # from `cleaning` or from `failed` (the cleanup queued when retries ran out):
            # both have exactly one legal exit, this one.
            if _move(db, res["id"], "released", from_state=res["state"]):
                out.released += 1
            _maybe_free_setup(db, res["setup_id"])
        else:
            _quarantine_setup(db, res)
            # residue after an already-failed deploy has no reservation state to go to (no
            # failed -> quarantined edge); it stays `failed` and the setup carries the flag.
            if res["state"] == "cleaning" and _move(db, res["id"], "quarantined",
                                                    from_state="cleaning"):
                out.quarantined += 1
        _audit(db, job, "cleanup", {"ok": result.ok, "cleanup_ok": cleaned.ok,
                                    "reconcile_attempts": attempts,
                                    "residue": list(verified.residue), "error": result.error})
    return result


def _run_reconcile(db, rn, job, res, ctx, sink, out: TickResult) -> RunResult:
    reconciled = _call(rn.reconcile, ctx, sink, "reconcile")
    verified = _call(rn.verify_clean, ctx, sink, "verify_clean")
    result = _residue_result(verified, "reconcile")
    with _txn(db):
        _finish(db, job["id"], result)
        if result.ok:
            ipam.clear(db, res["id"])                 # quarantined addresses proven free too
            if _move(db, res["id"], "released", from_state=res["state"]):
                out.reconciled += 1
            _maybe_free_setup(db, res["setup_id"])
        else:
            _quarantine_setup(db, res)
        _audit(db, job, "reconcile", {"ok": result.ok, "reconcile_ok": reconciled.ok,
                                      "residue": list(verified.residue), "error": result.error})
    return result


class _Busy(Exception):
    """The setup already has a running mutating job."""


def _claim(db: sqlite3.Connection, job) -> str:
    """Try to move `job` pending -> running. Returns "claimed", "busy" (single-flight: the
    setup has a running deploy/cleanup/reconcile job; the job stays pending) or "taken"
    (another engine got this job first). Check and move share one write transaction."""
    try:
        with _txn(db):
            fresh = models.job_by_id(db, job["id"])      # our snapshot may be stale
            if fresh is None or fresh["state"] != "pending":
                raise models.JobError(f"job {job['id']} is no longer pending")
            if job["kind"] in models.MUTATING_JOB_KINDS:
                res = models.reservation_by_id(db, job["reservation_id"])
                if models.running_mutating_job(db, res["setup_id"]) is not None:
                    raise _Busy
            models.transition_job(db, job["id"], "running")
    except _Busy:
        return "busy"
    except models.JobError:
        return "taken"
    return "claimed"


def _run_job(db, rn, job, out: TickResult) -> None:
    claim = _claim(db, job)
    if claim == "busy":
        out.deferred += 1
        log.info("job %s (%s) waits: its setup has a running job", job["id"], job["kind"])
        return
    if claim == "taken":
        return                                       # another engine took it
    out.jobs_run += 1
    res = models.reservation_by_id(db, job["reservation_id"])
    sink = runner_mod.job_sink(db, job["id"])
    expected = EXPECTED_STATES[job["kind"]]
    if res["state"] not in expected:
        models.transition_job(db, job["id"], "failed",
                              error=f"reservation is {res['state']}, expected {' or '.join(expected)}")
        return
    try:
        fn = runner_mod.method_for(rn, job["kind"])
    except runner_mod.RunnerError as e:
        models.transition_job(db, job["id"], "failed", error=str(e))
        return
    ctx = _context(db, job, res)
    log_for = getattr(rn, "log_path_for", None)     # B-84: the ansible runner's job log
    if callable(log_for) and not job["log_path"]:
        models.set_job_log_path(db, job["id"], log_for(job["id"]))
    with otel.job_span(job, ctx) as span:            # B-62: one trace per job (no-op if off)
        if span is not None:
            if models.set_job_trace_id(db, job["id"], span.trace_id):
                job = models.job_by_id(db, job["id"])     # retries / auto-cleanup inherit it
            sink = _traced(sink, span)
        if job["kind"] == "deploy":
            result = _run_deploy(db, rn, job, res, ctx, sink, out)
        elif job["kind"] == "cleanup":
            result = _run_cleanup(db, rn, job, res, ctx, sink, out)
        elif job["kind"] == "reconcile":
            result = _run_reconcile(db, rn, job, res, ctx, sink, out)
        else:                                        # health_probe
            result = _call(fn, ctx, sink, job["kind"])
            _finish(db, job["id"], result)
        if span is not None:
            span.finish(result.ok, result.error)


def _traced(sink, span):
    """Record each step on the job row and as a child span of the job's span."""
    def step(name: str, state: str = "ok", detail: str | None = None) -> None:
        sink(name, state, detail)
        span.step(name, state, detail)
    return step


# ---- 5. orphan sweep (B-73) ----------------------------------------------------------------

_TAGGED = re.compile(r"^daolab-r(\d+)-")


def _job_waiting(db, res_id: int) -> bool:
    """A pending or running job will deal with this reservation's leftovers itself."""
    return db.execute("SELECT 1 FROM jobs WHERE reservation_id = ? AND state IN "
                      "('pending', 'running') LIMIT 1", (res_id,)).fetchone() is not None


def _orphan(db, res_id: int) -> bool:
    res = models.reservation_by_id(db, res_id)
    if res is None:
        return True                                  # e.g. a VM made by hand or a dropped row
    return res["state"] in models.TERMINAL_STATES and not _job_waiting(db, res_id)


def _sweep_log_sink(name: str, state: str = "ok", detail: str | None = None) -> None:
    log.info("event=orphan_sweep step=%s state=%s detail=%s", name, state, detail)


def _sweep_orphans(db, rn, out: TickResult) -> None:
    models.wipe_access_passwords(db)                 # a finished slot's password is not kept
    lister, destroyer = getattr(rn, "tagged_vms", None), getattr(rn, "destroy_vms", None)
    left_behind: set[int] = set()                    # reservations whose VMs survived
    if lister is not None and destroyer is not None:
        try:
            names = list(lister())
        except Exception:                            # noqa: BLE001 — never stop the tick
            log.exception("orphan sweep: could not list the testbed's VMs")
            names = []
        doomed = [n for n in names
                  if (m := _TAGGED.match(n)) is not None and _orphan(db, int(m.group(1)))]
        if doomed:
            result = _call(lambda _ctx, sink: destroyer(doomed, sink), {"id": 0},
                           _sweep_log_sink, "destroy_vms")
            if result.ok:
                out.orphan_vms += len(doomed)
            else:
                left_behind = {int(_TAGGED.match(n).group(1)) for n in doomed}
            models.audit(db, "engine", "orphan_sweep", None, None,
                         {"vms": doomed, "ok": result.ok, "error": result.error})
    rows = db.execute(
        f"""SELECT DISTINCT l.reservation_id FROM ip_leases l
            JOIN reservations r ON r.id = l.reservation_id
            WHERE l.state = 'leased' AND r.state IN ({", ".join("?" for _ in models.TERMINAL_STATES)})""",
        models.TERMINAL_STATES).fetchall()
    for r in rows:
        rid = r["reservation_id"]
        if rid in left_behind or _job_waiting(db, rid):
            continue
        n = ipam.release(db, rid)
        if n:
            out.orphan_leases += n
            models.audit(db, "engine", "orphan_sweep", "reservation", rid, {"leases_released": n})


# ---- 6. health of active reservations (B-67) ---------------------------------------------

def post_webhook(payload: dict) -> None:
    """POST `payload` to DAOLAB_WEBHOOK_URL, at most once: a failure is logged, never retried
    and never blocks the engine (plan/04 "Webhook on ready" semantics). No-op when unset."""
    url = (config.settings.webhook_url or "").strip()
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("event=webhook r=%s status=%s", payload.get("reservation_id"), resp.status)
    except Exception as e:                           # noqa: BLE001 — at-most-once by design
        log.warning("event=webhook r=%s failed: %s", payload.get("reservation_id"), e)


def _health_query():
    """The Prometheus query function, or None when the telemetry stack is off."""
    if not (config.settings.otel_endpoint or "").strip():
        return None
    return health.prometheus(config.settings.prometheus_url)


def _evaluate_health(db, now: datetime, query, out: TickResult) -> None:
    rows = db.execute("SELECT * FROM reservations WHERE state = 'active' ORDER BY id").fetchall()
    for r in rows:
        try:
            verdict = health.verdict_object(health.evaluate_reservation(db, r["id"], now, query))
        except Exception:                            # noqa: BLE001 — one bad rule set must not stop the tick
            log.exception("health evaluation failed for reservation %s", r["id"])
            continue
        with _txn(db):
            prev = models.save_health_snapshot(db, r["id"], verdict)
        out.health_checked += 1
        if verdict["verdict"] == "down" and prev != "down":
            out.went_down += 1
            ctx = models.reservation_context(db, r["id"])
            post_webhook({"event": "reservation.health_down", "reservation_id": r["id"],
                          "usecase": ctx["usecase"], "setup": ctx["setup"], "user": ctx["user"],
                          "previous": prev, "health": verdict})


def _run_pending(db, rn, out: TickResult) -> None:
    for job in models.pending_jobs(db):
        _run_job(db, rn, job, out)


# ---- the tick + entry point ----------------------------------------------------------

def tick(db: sqlite3.Connection, now: datetime, rn=None,
         cfg: scheduler.SchedConfig | None = None,
         job_timeout_sec: float | None = None, health_query=None) -> TickResult:
    """One pass. `now` is injected (tests freeze it); `rn` defaults to the configured runner;
    `health_query` (a health.Query) defaults to Prometheus when the telemetry stack is on."""
    rn = rn if rn is not None else runner_mod.get_runner()
    cfg = cfg if cfg is not None else scheduler.SchedConfig.from_settings(config.settings)
    if job_timeout_sec is None:
        job_timeout_sec = config.settings.job_timeout_sec
    out = TickResult()
    out.recovered = _recover_stale_jobs(db, now, job_timeout_sec)
    out.activated = _promote_ready_to_active(db, now)
    out.expired = _expire_active(db, now)
    out.started = _start_due(db, now, cfg)
    _run_pending(db, rn, out)
    _sweep_orphans(db, rn, out)
    query = health_query if health_query is not None else _health_query()
    if query is not None:
        _evaluate_health(db, now, query, out)
    return out


def run_loop(db, interval_sec: float, rn=None, sleep=time.sleep, max_ticks: int | None = None,
             clock=lambda: datetime.now(timezone.utc)) -> int:
    """Tick forever (or `max_ticks` times). A crash inside one tick is logged and the loop
    carries on — the engine is stateless, so the next tick starts from the database."""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        try:
            result = tick(db, clock(), rn)
            if result.total:
                log.info("tick: %s", result)
        except Exception:                            # noqa: BLE001
            log.exception("tick failed")
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(interval_sec)
    return ticks


def stop_on_sigterm() -> None:
    """SIGTERM (systemctl stop) ends the loop like ^C does. A job cut off mid-playbook stays
    `running` and crash recovery takes it on the next start, as after a kill."""
    signal.signal(signal.SIGTERM, signal.default_int_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m service.engine",
                                     description="Run one engine tick, or tick forever with --loop.")
    parser.add_argument("--loop", action="store_true",
                        help="tick every DAOLAB_ENGINE_INTERVAL_SEC seconds until interrupted")
    opts = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, config.settings.log_level.upper(), logging.INFO))
    otel.setup_tracing(None, "daolab-engine")       # B-61/B-62: no-op without an endpoint
    db = models.connect(config.settings.db_path)
    have, want = migrations.current_version(db), migrations.latest_version()
    if have != want:
        print(f"error: database schema is v{have} but this checkout expects v{want}; "
              "run `daolab init` first", file=sys.stderr)
        return 1
    if config.settings.job_timeout_sec <= 0:
        print(f"error: DAOLAB_JOB_TIMEOUT_SEC must be > 0, got {config.settings.job_timeout_sec}",
              file=sys.stderr)
        return 1
    try:
        rn = runner_mod.get_runner()
    except runner_mod.RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        if not opts.loop:
            result = tick(db, datetime.now(timezone.utc), rn)
            print(f"tick done: {result}")
            return 0
        stop_on_sigterm()
        try:
            run_loop(db, config.settings.engine_interval_sec, rn)
        except KeyboardInterrupt:
            print("engine stopped")
        return 0
    finally:
        _close_runner(rn)


def _close_runner(rn) -> None:
    """Give the runner a chance to tidy up when the engine process ends. The mock runner (B-68)
    keeps a fake telemetry exporter per reservation and removes its Prometheus target file on
    `close()`; without this a stopped engine would leave targets pointing at dead ports."""
    close = getattr(rn, "close", None)
    if callable(close):
        try:
            close()
        except Exception:                            # noqa: BLE001 — never hide the real exit
            log.exception("runner close failed")


if __name__ == "__main__":
    sys.exit(main())
