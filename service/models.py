"""sqlite helpers — the only code touching JSON-in-TEXT columns and timestamps."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from . import migrations

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Reservation states (plan/09 §10). `cleaning` replaced `tearing_down` and `quarantined`
# (cleanup left residue) is new in B-21; the DB CHECK is kept in step by migration 6.
RESERVATION_STATES = ("queued", "provisioning", "ready", "active", "cleaning", "quarantined",
                      "released", "failed")
BUSY_STATES = ("queued", "provisioning", "ready", "active", "cleaning", "quarantined")
HELD_STATES = ("provisioning", "ready", "active", "cleaning", "quarantined")
TERMINAL_STATES = ("released", "failed")
# Per-user quota buckets (B-15): a reservation is "live" while it owns a setup (deploying or
# deployed) and "future" while it is only booked. plan/09 §4 caps these at 1 and 2.
LIVE_RESERVATION_STATES = ("provisioning", "ready", "active")
FUTURE_RESERVATION_STATES = ("queued",)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    if db_path is None:
        from . import config
        db_path = config.settings.db_path
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)  # autocommit; explicit BEGIN where needed (FastAPI deps and handlers may run on different threadpool threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> list[int]:
    """Bring the DB to the latest schema without touching existing data: apply the
    baseline (schema.sql, all IF NOT EXISTS), then pending migrations. Returns the
    migration versions applied this call — empty means nothing changed (idempotent)."""
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return migrations.apply(conn)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def from_json(text: str):
    return json.loads(text)


def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    now = utcnow()
    row = {**row, "created_at": now, "updated_at": now}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, row_id: int, changes: dict) -> None:
    changes = {**changes, "updated_at": utcnow()}
    assign = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE {table} SET {assign} WHERE id = ?", [*changes.values(), row_id])


# ---- typed row helpers -----------------------------------------------------

def user_by_token_hash(conn, token_hash: str):
    return conn.execute("SELECT * FROM users WHERE token_hash = ?", (token_hash,)).fetchone()


def user_by_id(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT u.id, u.username, u.role, u.enabled, u.created_at,
                  (SELECT COUNT(*) FROM reservations r
                   WHERE r.user_id = u.id AND r.state NOT IN ('released', 'failed'))
                  AS active_reservations
           FROM users u ORDER BY u.id"""
    ).fetchall()
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]


def usecase_by_id(conn, usecase_id: int):
    return conn.execute("SELECT * FROM usecases WHERE id = ?", (usecase_id,)).fetchone()


def set_dpu_hw_class(conn, setup_id: int, hw_class: str) -> bool:
    """Record the DPU personality after a successful deploy (invariant 6, plan/01). Targets
    the same DPU member the scheduler reads (first `dpu` resource by name); returns False if
    the setup has none."""
    row = conn.execute(
        "SELECT id FROM resources WHERE setup_id = ? AND kind = 'dpu' ORDER BY name LIMIT 1",
        (setup_id,)).fetchone()
    if row is None:
        return False
    update(conn, "resources", row["id"], {"hw_class": hw_class})
    return True


def usecase_by_name(conn, name: str):
    return conn.execute("SELECT * FROM usecases WHERE name = ?", (name,)).fetchone()


def list_usecases(conn):
    return conn.execute("SELECT * FROM usecases ORDER BY id").fetchall()


def setup_by_name(conn, name: str):
    return conn.execute("SELECT * FROM setups WHERE name = ?", (name,)).fetchone()


def list_setups(conn):
    return conn.execute("SELECT * FROM setups ORDER BY name").fetchall()


def setup_members(conn, setup_id: int):
    return conn.execute(
        "SELECT * FROM resources WHERE setup_id = ? ORDER BY name", (setup_id,)
    ).fetchall()


def setup_vm_hosts(conn, setup_id: int) -> set[str]:
    rows = conn.execute("SELECT DISTINCT host FROM resources WHERE setup_id = ?", (setup_id,))
    return {r["host"] for r in rows}


def reservation_by_id(conn, res_id: int):
    return conn.execute("SELECT * FROM reservations WHERE id = ?", (res_id,)).fetchone()


def count_user_reservations(conn, user_id: int, states: tuple[str, ...]) -> int:
    if not states:
        return 0
    marks = ", ".join("?" for _ in states)
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM reservations WHERE user_id = ? AND state IN ({marks})",
        (user_id, *states)).fetchone()["n"]


def list_reservations(conn, user_id: int | None = None):
    if user_id is None:
        return conn.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM reservations WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()


def held_by(conn, setup_id: int):
    marks = ", ".join("?" for _ in HELD_STATES)
    return conn.execute(
        f"""SELECT * FROM reservations WHERE setup_id = ? AND state IN ({marks})
            ORDER BY start_at, id LIMIT 1""",
        (setup_id, *HELD_STATES),
    ).fetchone()


def queued_for(conn, setup_id: int):
    return conn.execute(
        "SELECT * FROM reservations WHERE setup_id = ? AND state = 'queued' ORDER BY start_at, id",
        (setup_id,),
    ).fetchall()


def vms_for_reservation(conn, res_id: int):
    return conn.execute(
        "SELECT * FROM vms WHERE reservation_id = ? ORDER BY id", (res_id,)
    ).fetchall()


def vm_by_role(conn, res_id: int, role: str):
    return conn.execute(
        "SELECT * FROM vms WHERE reservation_id = ? AND role = ?", (res_id, role)
    ).fetchone()


def set_vm_ip(conn, vm_id: int, ip: str) -> None:
    update(conn, "vms", vm_id, {"ip": ip})


def vm_name(res_id: int, role: str) -> str:
    """The XCP-ng name (and tag prefix) of a reservation's VM: `daolab-r<id>-<role>`."""
    return f"daolab-r{res_id}-{role}"


def _member_of_kind(members, kind: str):
    return next((m for m in members if m["kind"] == kind), None)


def planned_vms(conn, res) -> list[dict]:
    """The `vms` rows a reservation will have (plan/04 step (d)), resolved against its setup:

      * `on: dpu`      -> host = the DPU member's name, no template, no VM name;
      * `on: dpu_host` -> host = the XCP-ng host of the DPU member;
      * `on: tg_host`  -> host = the XCP-ng host of the NIC member;
      * diy            -> one VM per host of `diy_templates`, the host name as the role.

    A role whose member the setup lacks is left out (capability matching should make that
    impossible; the deploy then fails on the missing VM instead of the engine crashing)."""
    members = setup_members(conn, res["setup_id"])
    out = []
    if res["diy"]:
        for host, template in sorted(from_json(res["diy_templates"] or "{}").items()):
            out.append({"role": host, "host": host, "template": template,
                        "name": vm_name(res["id"], host)})
        return out
    uc = usecase_by_id(conn, res["usecase_id"])
    roles = from_json(uc["roles"]) if uc["roles"] else {}
    dpu = _member_of_kind(members, "dpu")
    nic = _member_of_kind(members, "nic")
    for role, spec in roles.items():
        on = spec.get("on")
        if on == "dpu" and dpu is not None:
            out.append({"role": role, "host": dpu["name"], "template": None, "name": None})
        elif on in ("dpu_host", "tg_host"):
            member = dpu if on == "dpu_host" else nic
            if member is not None:
                out.append({"role": role, "host": member["host"], "template": spec.get("vm"),
                            "name": vm_name(res["id"], role)})
    return out


def create_vms(conn, res_id: int) -> int:
    """Insert the reservation's `vms` rows (ip NULL) before any playbook runs, so the
    `/internal/vm-ip` and `/internal/leases` callbacks always find their (reservation, role).
    Idempotent: existing rows are kept. Returns how many rows were added."""
    res = reservation_by_id(conn, res_id)
    added = 0
    for vm in planned_vms(conn, res):
        if vm_by_role(conn, res_id, vm["role"]) is None:
            insert(conn, "vms", {"reservation_id": res_id, **vm})
            added += 1
    return added


def reservation_context(conn, res_id: int) -> dict:
    """Everything a runner needs about one reservation, read once by the engine so the runner
    never touches the DB. Keys follow the playbook extravars (plan/04 runner contract):
    `reservation_id`, `usecase`, `setup`, `user`, `ssh_pubkey`, `hw_class`, `diy`,
    `templates`; plus `id` (what every runner function reads), the use case's `roles` and
    `verify`, the setup `members` and the reservation's `vms`."""
    r = reservation_by_id(conn, res_id)
    user = user_by_id(conn, r["user_id"])
    uc = usecase_by_id(conn, r["usecase_id"])
    setup = conn.execute("SELECT name FROM setups WHERE id = ?", (r["setup_id"],)).fetchone()
    return {
        "id": r["id"], "reservation_id": r["id"], "state": r["state"],
        "start_at": r["start_at"], "end_at": r["end_at"],
        "usecase": uc["name"], "setup": setup["name"],
        "user": user["username"], "ssh_pubkey": user["ssh_pubkey"],
        "hw_class": uc["hw_class"], "diy": bool(r["diy"]),
        "templates": from_json(r["diy_templates"]) if r["diy_templates"] else {},
        "roles": from_json(uc["roles"]) if uc["roles"] else {},
        "verify": from_json(uc["verify"]) if uc["verify"] else {},
        "members": [{"name": m["name"], "kind": m["kind"], "host": m["host"]}
                    for m in setup_members(conn, r["setup_id"])],
        "vms": [{"role": v["role"], "host": v["host"], "template": v["template"],
                 "name": v["name"], "ip": v["ip"]} for v in vms_for_reservation(conn, res_id)],
    }


# ---- IP pools / leases (B-40) ----------------------------------------------

def ip_pool_by_name(conn, name: str):
    return conn.execute("SELECT * FROM ip_pools WHERE name = ?", (name,)).fetchone()


def list_ip_pools(conn):
    return conn.execute("SELECT * FROM ip_pools ORDER BY name").fetchall()


def live_leases(conn, pool_id: int):
    """Leases that still occupy an address: leased or quarantined, never released."""
    return conn.execute(
        "SELECT * FROM ip_leases WHERE pool_id = ? AND state != 'released' ORDER BY ip",
        (pool_id,)).fetchall()


def ip_lease_by_role(conn, res_id: int, role: str):
    """The `leased` row of (reservation, role), or None. There is at most one (migration 7);
    quarantined rows of the same role (a swapped-out conflicting address) are not returned."""
    return conn.execute(
        """SELECT * FROM ip_leases WHERE reservation_id = ? AND role = ? AND state = 'leased'
           ORDER BY id LIMIT 1""", (res_id, role)).fetchone()


def ip_leases_for_reservation(conn, res_id: int):
    return conn.execute(
        "SELECT * FROM ip_leases WHERE reservation_id = ? ORDER BY id", (res_id,)).fetchall()


# ---- setup availability (B-18) ---------------------------------------------

SETUP_AVAILABILITY = ("available", "quarantined", "maintenance")


def set_setup_availability(conn, setup_id: int, availability: str) -> None:
    """Move a setup between available/quarantined/maintenance (CHECK-enforced).
    Booking and placement ignore this; only provisioning starts are gated (B-18)."""
    update(conn, "setups", setup_id, {"availability": availability})


# ---- audit log (B-55) ------------------------------------------------------

AUDIT_ACTIONS = (
    "cancel", "admin_override",                        # written by the API today
    "deploy", "cleanup", "redeploy", "force_clean",    # written by the engine (B-20/B-23)
)


def audit(conn, actor: str, action: str, object_type: str | None = None,
          object_id: int | None = None, detail=None, trace_id: str | None = None) -> int:
    """Append one audit event. This is the *only* writer for audit_events and there is no
    update or delete helper, so the table is append-only by construction (plan/09 §11).
    `detail` is JSON-serialised; `actor` is a username or a system name like 'engine'."""
    cur = conn.execute(
        """INSERT INTO audit_events (actor, action, object_type, object_id, detail,
                                     trace_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (actor, action, object_type, object_id,
         to_json(detail) if detail is not None else None, trace_id, utcnow()))
    return cur.lastrowid


def list_audit_events(conn, limit: int = 100) -> list:
    """Newest-first slice of the audit log (raw rows; `detail` is JSON text)."""
    return conn.execute(
        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---- jobs (B-20) -------------------------------------------------------------

JOB_KINDS = ("deploy", "cleanup", "health_probe", "reconcile")
MUTATING_JOB_KINDS = ("deploy", "cleanup", "reconcile")   # health probes are read-only (B-24)
JOB_STATES = ("pending", "running", "succeeded", "failed")
JOB_TERMINAL_STATES = ("succeeded", "failed")
STEP_STATES = ("ok", "failed", "skipped")

# plan/09 §6: pending -> running -> succeeded | failed, nothing else. A failed job is final;
# a retry is a *new* job row with attempt + 1 (create_job). The one explicit exception is crash
# recovery (B-26): requeue_job puts a stale `running` job back to `pending`; it is not a
# transition_job move.
JOB_TRANSITIONS = {
    "pending": ("running",),
    "running": ("succeeded", "failed"),
    "succeeded": (),
    "failed": (),
}


class JobError(Exception):
    """An illegal job operation: bad kind, bad transition, or the row changed under us."""


def create_job(conn, reservation_id: int, kind: str, attempt: int = 1,
               trace_id: str | None = None, log_path: str | None = None) -> int:
    """Insert a `pending` job. `attempt` is 1 for the first try; a retry creates a new job
    for the same reservation and kind with attempt + 1 (a failed job is never revived)."""
    if kind not in JOB_KINDS:
        raise JobError(f"unknown job kind {kind!r}; expected one of {JOB_KINDS}")
    if attempt < 1:
        raise JobError(f"attempt must be >= 1, got {attempt}")
    return insert(conn, "jobs", {
        "reservation_id": reservation_id, "kind": kind, "attempt": attempt,
        "trace_id": trace_id, "log_path": log_path})


def job_by_id(conn, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def jobs_for_reservation(conn, res_id: int, kind: str | None = None):
    """A reservation's jobs, oldest first, optionally only one kind."""
    if kind is None:
        return conn.execute(
            "SELECT * FROM jobs WHERE reservation_id = ? ORDER BY id", (res_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE reservation_id = ? AND kind = ? ORDER BY id",
        (res_id, kind)).fetchall()


def pending_jobs(conn, limit: int | None = None):
    """Jobs waiting to run, oldest first (FIFO) — what an engine tick picks from."""
    sql = "SELECT * FROM jobs WHERE state = 'pending' ORDER BY id"
    if limit is None:
        return conn.execute(sql).fetchall()
    return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()


def running_mutating_job(conn, setup_id: int):
    """The `running` deploy / cleanup / reconcile job on `setup_id`, or None (B-24).

    A job belongs to a setup through its reservation. Health probes are read-only and never
    count; neither do pending or finished jobs. If several are running (they should never be)
    the oldest is returned."""
    marks = ", ".join("?" for _ in MUTATING_JOB_KINDS)
    return conn.execute(
        f"""SELECT j.* FROM jobs j JOIN reservations r ON r.id = j.reservation_id
            WHERE r.setup_id = ? AND j.state = 'running' AND j.kind IN ({marks})
            ORDER BY j.id LIMIT 1""",
        (setup_id, *MUTATING_JOB_KINDS)).fetchone()


def stale_running_jobs(conn, idle_before: str):
    """`running` jobs with no sign of life since before `idle_before` (a timestamp string), oldest
    first (B-26). Every step a runner records bumps `updated_at`, so a deploy that is slow but
    still reporting is never stale; one whose engine died stops reporting."""
    return conn.execute(
        "SELECT * FROM jobs WHERE state = 'running' AND updated_at < ? ORDER BY id",
        (idle_before,)).fetchall()


def requeue_job(conn, job) -> bool:
    """Put a stale `running` job back to `pending` so an engine picks it up again (B-26).

    This is the one allowed way out of `running` that is not `succeeded` or `failed`
    (plan/09 §10): playbooks are idempotent, so replaying the job is safe. The attempt number
    does not change. The steps recorded so far are kept, and a `requeued` marker step is added,
    so a live viewer keeps its place in the list and the history shows what happened. The update
    only applies if the job is still running AND has not recorded anything since `job` was read,
    so a job that came back to life is left alone. Returns True if it was requeued."""
    now = utcnow()
    marker = {"name": "requeued", "state": "skipped", "at": now,
              "detail": f"no activity since {job['updated_at']}; engine presumed dead"}
    steps = to_json([*from_json(job["steps"]), marker])
    cur = conn.execute(
        "UPDATE jobs SET state = 'pending', started_at = NULL, steps = ?, updated_at = ? "
        "WHERE id = ? AND state = 'running' AND updated_at = ? AND steps = ?",
        (steps, now, job["id"], job["updated_at"], job["steps"]))
    return cur.rowcount == 1


def requeue_stale_jobs(conn, idle_before: str) -> list[int]:
    """Requeue every stale running job (see `stale_running_jobs`); returns the ids requeued."""
    return [j["id"] for j in stale_running_jobs(conn, idle_before) if requeue_job(conn, j)]


def set_job_trace_id(conn, job_id: int, trace_id: str) -> bool:
    """Record the OpenTelemetry trace id of a job (B-62); only fills an empty one, so a retry
    that continues an earlier trace keeps it. Returns True if it was written."""
    cur = conn.execute("UPDATE jobs SET trace_id = ?, updated_at = ? "
                       "WHERE id = ? AND trace_id IS NULL", (trace_id, utcnow(), job_id))
    return cur.rowcount == 1


def job_steps(job) -> list[dict]:
    """The parsed `steps` list of a job row."""
    return from_json(job["steps"])


def transition_job(conn, job_id: int, new_state: str, error: str | None = None) -> None:
    """Move a job along pending -> running -> succeeded | failed (compare-and-set).

    Raises JobError for an unknown job, an illegal move, or if another writer changed the
    state between our read and our write (the UPDATE only matches the state we validated).
    `started_at` is stamped on running, `finished_at` on succeeded/failed; `error` is
    recorded only for a failed job."""
    job = job_by_id(conn, job_id)
    if job is None:
        raise JobError(f"no job {job_id}")
    if new_state not in JOB_STATES:
        raise JobError(f"unknown job state {new_state!r}; expected one of {JOB_STATES}")
    if new_state not in JOB_TRANSITIONS[job["state"]]:
        raise JobError(f"job {job_id}: cannot go {job['state']} -> {new_state}")
    if error is not None and new_state != "failed":
        raise JobError(f"job {job_id}: error is only recorded for a failed job")
    now = utcnow()
    changes = {"state": new_state, "updated_at": now}
    if new_state == "running":
        changes["started_at"] = now
    else:
        changes["finished_at"] = now
    if new_state == "failed":
        changes["error"] = error
    assign = ", ".join(f"{k} = ?" for k in changes)
    cur = conn.execute(f"UPDATE jobs SET {assign} WHERE id = ? AND state = ?",
                       [*changes.values(), job_id, job["state"]])
    if cur.rowcount != 1:
        raise JobError(f"job {job_id}: state changed concurrently (expected {job['state']})")


def append_job_step(conn, job_id: int, name: str, state: str = "ok",
                    detail: str | None = None) -> dict:
    """Record one finished step on a *running* job; returns the stored step record.

    Steps are appended with a compare-and-set on the current JSON text, so two writers
    never overwrite each other's step, and a job that is no longer running (finished or
    never started) refuses new steps with JobError."""
    if state not in STEP_STATES:
        raise JobError(f"unknown step state {state!r}; expected one of {STEP_STATES}")
    step = {"name": name, "state": state, "at": utcnow()}
    if detail is not None:
        step["detail"] = detail
    for _ in range(50):
        job = job_by_id(conn, job_id)
        if job is None:
            raise JobError(f"no job {job_id}")
        if job["state"] != "running":
            raise JobError(f"job {job_id}: cannot add a step while {job['state']}")
        new_steps = to_json([*from_json(job["steps"]), step])
        cur = conn.execute(
            "UPDATE jobs SET steps = ?, updated_at = ? "
            "WHERE id = ? AND state = 'running' AND steps = ?",
            (new_steps, utcnow(), job_id, job["steps"]))
        if cur.rowcount == 1:
            return step
    raise JobError(f"job {job_id}: could not append step {name!r} (too much contention)")


# ---- reservation state machine (B-21) ----------------------------------------

# plan/09 §10, edge for edge. `failed -> released` happens once the cleanup of a failed
# deploy is verified; `quarantined -> released` after reconcile / force-clean.
RESERVATION_TRANSITIONS = {
    "queued": ("provisioning", "released"),
    "provisioning": ("ready", "failed"),
    "ready": ("active", "cleaning"),
    "active": ("cleaning",),
    "cleaning": ("released", "quarantined"),
    "quarantined": ("released",),
    "failed": ("released",),
    "released": (),
}


class ReservationStateError(Exception):
    """An illegal reservation state change, or the row changed under us."""


def transition_reservation(conn, res_id: int, new_state: str, from_state: str | None = None,
                           failure_reason: str | None = None) -> None:
    """Move a reservation along the plan/09 §10 state machine (compare-and-set).

    Reads the current state, checks the move against RESERVATION_TRANSITIONS and writes with
    `UPDATE ... WHERE id = ? AND state = <the state we validated>`, so a concurrent change
    makes this raise instead of silently overwriting it. Pass `from_state` when the caller
    decided based on an earlier read: if the row is no longer in that state this raises.
    `failure_reason` may be given only when moving to `failed`."""
    row = reservation_by_id(conn, res_id)
    if row is None:
        raise ReservationStateError(f"no reservation {res_id}")
    if new_state not in RESERVATION_STATES:
        raise ReservationStateError(
            f"unknown reservation state {new_state!r}; expected one of {RESERVATION_STATES}")
    current = row["state"]
    if from_state is not None and current != from_state:
        raise ReservationStateError(
            f"reservation {res_id}: expected state {from_state}, found {current}")
    if new_state not in RESERVATION_TRANSITIONS[current]:
        raise ReservationStateError(f"reservation {res_id}: cannot go {current} -> {new_state}")
    if failure_reason is not None and new_state != "failed":
        raise ReservationStateError(
            f"reservation {res_id}: failure_reason is only recorded when moving to failed")
    changes = {"state": new_state, "updated_at": utcnow()}
    if new_state == "failed":
        changes["failure_reason"] = failure_reason
    assign = ", ".join(f"{k} = ?" for k in changes)
    cur = conn.execute(f"UPDATE reservations SET {assign} WHERE id = ? AND state = ?",
                       [*changes.values(), res_id, current])
    if cur.rowcount != 1:
        raise ReservationStateError(
            f"reservation {res_id}: state changed concurrently (expected {current})")
