"""FastAPI service: catalog / pools / setups / users / reservations + internal
VM-IP callback; serves dashboard/. Views over the DB only — YAML is read by
`daolab sync` (catalog.py), never by request handlers."""
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from . import access, config, ipam, models, otel, scheduler
from . import health as health_eval     # `health` is the liveness route below

logging.basicConfig(level=getattr(logging, config.settings.log_level.upper(), logging.INFO))
log = logging.getLogger("daolab.api")

app = FastAPI(title="daolab", docs_url=None, redoc_url=None)

otel.setup_tracing(app, "daolab-api")


# ---- error layer -----------------------------------------------------------

class DaolabError(Exception):
    def __init__(self, code: str, http_status: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.http_status, self.message = code, http_status, message
        self.detail = detail or {}


@app.exception_handler(DaolabError)
async def daolab_error_handler(request: Request, exc: DaolabError):
    return JSONResponse(status_code=exc.http_status, content={
        "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    msg = f"{loc}: {first.get('msg', 'invalid request')}" if loc else first.get("msg", "invalid request")
    return JSONResponse(status_code=422, content={
        "error": {"code": "validation_error", "message": msg, "detail": {}}})


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={
        "error": {"code": "internal_error", "message": "internal error", "detail": {}}})


# ---- auth dependencies -----------------------------------------------------

def db_conn():
    conn = models.connect(config.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_user(authorization: str | None = Header(None),
                 conn: sqlite3.Connection = Depends(db_conn)) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise DaolabError("unauthorized", 401, "missing bearer token")
    token_hash = hashlib.sha256(authorization[len("Bearer "):].encode()).hexdigest()
    user = models.user_by_token_hash(conn, token_hash)
    if user is None:
        raise DaolabError("unauthorized", 401, "invalid bearer token")
    if not user["enabled"]:
        raise DaolabError("forbidden", 403, f"user {user['username']} is disabled",
                          {"reason": "user_disabled"})
    return user


def require_admin(request: Request, user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if user["role"] != "admin":
        raise DaolabError("forbidden", 403,
                          f"admin role required for {request.method} {request.url.path}",
                          {"required_role": "admin"})
    return user


def require_internal(x_daolab_secret: str | None = Header(None)) -> None:
    secret = config.settings.internal_secret
    if not secret or not x_daolab_secret or not hmac.compare_digest(x_daolab_secret, secret):
        raise DaolabError("unauthorized", 401, "invalid or missing X-Daolab-Secret")


# ---- request models --------------------------------------------------------

class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usecase: str
    mode: Literal["asap", "at"] = "asap"
    start_at: str | None = None
    hours: int | None = None
    setup: str | None = None
    templates: dict[str, str] | None = None
    # How the owner gets in (the dashboard's Deploy dialog): absent -> their registered key;
    # a public key (one per line) -> that key; "" -> no key, a generated password instead.
    ssh_pubkey: str | None = None


class VmIpCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reservation_id: int
    role: str
    ip: str


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reservation_id: int
    role: str


class LeaseConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str


class ForceClean(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip_verify: bool = False


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


# ---- access: SSH key or generated password ----------------------------------

_PUBKEY = re.compile(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|"
                     r"sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) [A-Za-z0-9+/]+={0,3}"
                     r"( [^\x00-\x1f]*)?$")
_PASSWORD_CHARS = string.ascii_letters + string.digits
_LIVE = ("ready", "active")


def _deploy_access(given: str | None) -> tuple[str | None, str | None]:
    """(ssh_pubkey, access_password) to store for a new reservation. Keys are checked line by
    line; a blank answer means "give me a password" (16 random letters and digits)."""
    if given is None:
        return None, None                             # the owner's registered key
    lines = [ln.strip() for ln in given.strip().splitlines() if ln.strip()]
    if not lines:
        return None, "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(16))
    if len(lines) > 5 or len(given) > 8192:
        raise DaolabError("validation_error", 422, "ssh_pubkey: at most 5 keys")
    bad = [ln for ln in lines if not _PUBKEY.match(ln)]
    if bad:
        raise DaolabError("validation_error", 422,
                          f"ssh_pubkey: not an SSH public key: {bad[0][:40]}...",
                          {"hint": "paste the contents of ~/.ssh/id_ed25519.pub, or leave it "
                                   "empty to get a password"})
    return "\n".join(lines), None


def _key_label(keys: str) -> str | None:
    """`ssh-ed25519 …alice@laptop` for display (type + comment, never the whole key)."""
    first = (keys or "").strip().splitlines()[:1]
    if not first:
        return None
    parts = first[0].split()
    return f"{parts[0]} …{parts[2] if len(parts) > 2 else parts[1][-8:]}"


def _access_object(conn, row, user) -> dict | None:
    """How the owner logs into the DPU: `ssh lab@<static address>` plus their key or the
    generated password. The password is only shown while the slot is live; this object is
    only ever served to the owner or an admin (B-54)."""
    dpu = conn.execute("SELECT * FROM resources WHERE setup_id = ? AND kind = 'dpu' "
                       "ORDER BY name LIMIT 1", (row["setup_id"],)).fetchone()
    if dpu is None or row["diy"]:
        return None
    host = dpu["address"] or dpu["name"]
    password_mode = row["access_password"] is not None
    live = row["state"] in _LIVE
    return {"login": models.ACCESS_LOGIN, "host": host,
            "ssh": f"ssh {models.ACCESS_LOGIN}@{host}", "live": live,
            "auth": "password" if password_mode else "ssh-key",
            "password": row["access_password"] if (password_mode and live) else None,
            "key": None if password_mode else _key_label(models.access_keys(row, user))}


# ---- serializers -----------------------------------------------------------

def _sched_config() -> scheduler.SchedConfig:
    return scheduler.SchedConfig.from_settings(config.settings)


def _node_id(name: str) -> str:
    return "n_" + re.sub(r"[^a-z0-9_]", "_", str(name).lower())


def _label(*lines) -> str:
    return "<br/>".join(str(l) for l in lines if l).replace('"', "'")


def _reservation_diagram(conn, row, user, uc) -> str | None:
    """Mermaid topology of the provisioned reservation, generated from data only
    (use-case roles + vms rows + setup links) so the frontend stays topology-free."""
    vms = models.vms_for_reservation(conn, row["id"])
    if not vms:
        return None
    by_role = {v["role"]: v for v in vms}
    roles = models.from_json(uc["roles"]) if uc["roles"] else {}
    lines = ["graph LR"]
    if roles:
        dpu_role = next((r for r, s in roles.items() if s.get("on") == "dpu"), None)
        for rname, spec in roles.items():
            vm = by_role.get(rname)
            ip = vm["ip"] if vm else None
            if spec.get("on") == "dpu":
                label = _label(f"{rname} · {spec.get('app', 'app')}", "on the OCTEON DPU",
                               ip or "no IP yet")
            else:
                label = _label(f"{rname} VM · {(vm['host'] if vm else spec.get('on'))}",
                               vm["name"] if vm else spec.get("vm"),
                               f"ssh {user['username']}@{ip}" if ip else "no IP yet")
            lines.append(f'  {_node_id(rname)}["{label}"]')
        upstream = roles.get(dpu_role, {}).get("upstream") if dpu_role else None
        for rname, spec in roles.items():
            if spec.get("on") == "dpu" or not dpu_role:
                continue
            if rname == upstream:
                edge = f"upstream · {spec.get('connect', '')}".strip(" ·")
                lines.append(f'  {_node_id(dpu_role)} -->|"{edge}"| {_node_id(rname)}')
            else:
                lines.append(f'  {_node_id(rname)} -->|"{spec.get("connect", "link")}"| {_node_id(dpu_role)}')
    else:                                                 # diy: nodes per VM, edges from setup links
        setup = conn.execute("SELECT * FROM setups WHERE id = ?", (row["setup_id"],)).fetchone()
        host_of = {m["name"]: m["host"] for m in models.setup_members(conn, row["setup_id"])}
        vm_on_host = {}
        for v in vms:
            vm_on_host.setdefault(v["host"], v)
        for v in vms:
            label = _label(f"{v['role']} · {v['host']}", v["name"],
                           f"ssh {user['username']}@{v['ip']}" if v["ip"] else "no IP yet")
            lines.append(f'  {_node_id(v["role"])}["{label}"]')
        for link in models.from_json(setup["links"]):
            va = vm_on_host.get(host_of.get(link["a"].split(".", 1)[0]))
            vb = vm_on_host.get(host_of.get(link["b"].split(".", 1)[0]))
            if va and vb and va["role"] != vb["role"]:
                lines.append(f'  {_node_id(va["role"])} ---|"{link["capability"]}"| {_node_id(vb["role"])}')
    return "\n".join(lines)


def reservation_object(conn, row) -> dict:
    user = models.user_by_id(conn, row["user_id"])
    uc = conn.execute("SELECT * FROM usecases WHERE id = ?", (row["usecase_id"],)).fetchone()
    setup = conn.execute("SELECT name FROM setups WHERE id = ?", (row["setup_id"],)).fetchone()
    vms = []
    password_mode = row["access_password"] is not None
    for vm in models.vms_for_reservation(conn, row["id"]):
        ssh = None
        if vm["ip"] and vm["name"]:                       # on: dpu roles have no VM, no ssh
            ssh = f"ssh {user['username']}@{vm['ip']}"
        vms.append({"role": vm["role"], "host": vm["host"], "template": vm["template"],
                    "name": vm["name"], "ip": vm["ip"], "ssh": ssh,
                    "username": user["username"] if vm["name"] else None,
                    "auth": (("password" if password_mode else "ssh-key") if vm["name"] else None),
                    "password": (row["access_password"] if (vm["name"] and password_mode
                                                             and row["state"] in _LIVE) else None)})
    return {
        "id": row["id"], "user": user["username"], "usecase": uc["name"],
        "setup": setup["name"], "state": row["state"], "diy": bool(row["diy"]),
        "requested_start_at": row["requested_start_at"], "start_at": row["start_at"],
        "end_at": row["end_at"], "shifted": row["start_at"] != row["requested_start_at"],
        "failure_reason": row["failure_reason"],
        "vms": vms, "diagram": _reservation_diagram(conn, row, user, uc),
        "leases": [{"role": l["role"], "ip": l["ip"], "state": l["state"]}
                   for l in models.ip_leases_for_reservation(conn, row["id"])
                   if l["state"] != "released"],
        "health": _health_summary(conn, row["id"]),
        "access": _access_object(conn, row, user),
        "job": _latest_job_summary(conn, row["id"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _latest_job_summary(conn, res_id: int) -> dict | None:
    """The reservation's most recent job, for the card's progress line."""
    jobs = models.jobs_for_reservation(conn, res_id)
    if not jobs:
        return None
    j = jobs[-1]
    steps = models.job_steps(j)
    return {"id": j["id"], "kind": j["kind"], "state": j["state"], "attempt": j["attempt"],
            "error": j["error"], "steps": len(steps),
            "last_step": steps[-1] if steps else None}


def _health_summary(conn, res_id: int) -> dict | None:
    """The engine's latest verdict (B-67) for the card's badge; None until it has one."""
    snap = models.health_snapshot(conn, res_id)
    if snap is None:
        return None
    return {"verdict": snap["verdict"], "reason": snap["reason"],
            "evaluated_at": snap["evaluated_at"], "changed_at": snap["changed_at"]}


def reservation_summary(conn, row) -> dict:
    user = models.user_by_id(conn, row["user_id"])
    uc = conn.execute("SELECT name FROM usecases WHERE id = ?", (row["usecase_id"],)).fetchone()
    return {"id": row["id"], "user": user["username"], "usecase": uc["name"],
            "state": row["state"], "start_at": row["start_at"], "end_at": row["end_at"]}


def user_object(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "enabled": bool(row["enabled"]), "created_at": row["created_at"]}


_LOG_TAIL_BYTES = 8192


def _log_tail(path: str | None) -> str | None:
    """The last few KB of a job's playbook log (plan/09 §4); None when there is no log yet."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def job_object(job) -> dict:
    """The canonical job shape (plan/09 §4), returned by the cleanup/redeploy responses and
    GET /jobs/{id}: state, parsed steps, trace_id and the playbook log tail."""
    return {
        "id": job["id"], "reservation_id": job["reservation_id"], "kind": job["kind"],
        "state": job["state"], "attempt": job["attempt"], "trace_id": job["trace_id"],
        "log_path": job["log_path"], "log_tail": _log_tail(job["log_path"]),
        "error": job["error"], "steps": models.job_steps(job),
        "started_at": job["started_at"], "finished_at": job["finished_at"],
        "created_at": job["created_at"], "updated_at": job["updated_at"],
    }


_TERMINAL_RESERVATION_STATES = ("released", "failed")
_EVENTS_POLL_SEC = 1.0
_EVENTS_MAX_SEC = 900.0


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _reservation_events(conn: sqlite3.Connection, res_id: int):
    """Yield SSE frames for a reservation: its state, then every job step as it lands
    (plan/09 §4). Ends when the reservation reaches a terminal state, or after
    `_EVENTS_MAX_SEC` so an abandoned stream does not pin a connection forever."""
    seen_steps: set[tuple[int, int]] = set()
    last_state = None
    deadline = time.monotonic() + _EVENTS_MAX_SEC
    while True:
        row = models.reservation_by_id(conn, res_id)
        if row is None:                                   # deleted under us
            return
        if row["state"] != last_state:
            last_state = row["state"]
            yield _sse("state", {"id": res_id, "state": row["state"],
                                 "start_at": row["start_at"], "end_at": row["end_at"]})
        for job in models.jobs_for_reservation(conn, res_id):
            for idx, step in enumerate(models.job_steps(job)):
                key = (job["id"], idx)
                if key in seen_steps:
                    continue
                seen_steps.add(key)
                yield _sse("step", {"job_id": job["id"], "kind": job["kind"], **step})
        if last_state in _TERMINAL_RESERVATION_STATES:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(_EVENTS_POLL_SEC)


# ---- routes ----------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/whoami")
def whoami(user: sqlite3.Row = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "created_at": user["created_at"], "ssh_pubkey": user["ssh_pubkey"]}


@app.get("/usecases")
def usecases(user: sqlite3.Row = Depends(current_user),
             conn: sqlite3.Connection = Depends(db_conn)):
    now = datetime.now(timezone.utc)
    setups = [{"id": s["id"], "name": s["name"],
               "capabilities": models.from_json(s["capabilities"]),
               "members": [{"name": m["name"], "kind": m["kind"], "host": m["host"]}
                           for m in models.setup_members(conn, s["id"])],
               "links": models.from_json(s["links"]), "diagram": s["diagram"]}
              for s in models.list_setups(conn)]
    out = []
    for uc in models.list_usecases(conn):
        row = {"name": uc["name"], "description": uc["description"], "variant": uc["variant"],
               "status": "active" if uc["yaml_path"] else "planned",
               "doc_url": uc["doc_url"], "doc_summary": uc["doc_summary"],
               "requires": models.from_json(uc["requires"]), "hw_class": uc["hw_class"],
               "roles": models.from_json(uc["roles"]) if uc["roles"] else {},
               "setups": [],
               "available": False, "reason": None, "missing": [], "next_free_at": None}
        if uc["yaml_path"] is None:
            row["reason"] = "no_yaml"
        else:
            requires = set(models.from_json(uc["requires"]))
            candidates = [s for s in setups if requires <= set(s["capabilities"])]
            row["setups"] = [{k: s[k] for k in ("name", "capabilities", "members", "links", "diagram")}
                             for s in candidates]
            if not candidates:
                best = min(setups, key=lambda s: (len(requires - set(s["capabilities"])), s["name"]),
                           default=None)
                row["reason"] = "unmet_capability"
                row["missing"] = sorted(requires - set(best["capabilities"])) if best else sorted(requires)
            else:
                nfa = scheduler.next_free_at(conn, uc["name"], now, _sched_config())
                if nfa is None:
                    row["reason"] = "no_free_slot"
                else:
                    row["available"] = True
                    row["next_free_at"] = models.format_ts(nfa)
        out.append(row)
    return out


@app.get("/pools")
def pools(user: sqlite3.Row = Depends(require_admin),
          conn: sqlite3.Connection = Depends(db_conn)):
    out = []
    for pool in conn.execute("SELECT * FROM pools ORDER BY name").fetchall():
        setups = [s["name"] for s in conn.execute(
            "SELECT name FROM setups WHERE pool_id = ? ORDER BY name", (pool["id"],))]
        out.append({"id": pool["id"], "name": pool["name"], "setups": setups})
    return out


@app.get("/setups")
def setups(user: sqlite3.Row = Depends(require_admin),
           conn: sqlite3.Connection = Depends(db_conn)):
    out = []
    for s in models.list_setups(conn):
        pool = conn.execute("SELECT name FROM pools WHERE id = ?", (s["pool_id"],)).fetchone()
        members = [{"name": m["name"], "kind": m["kind"],
                    "capabilities": models.from_json(m["capabilities"]),
                    "host": m["host"], "hw_class": m["hw_class"]}
                   for m in models.setup_members(conn, s["id"])]
        hold = models.held_by(conn, s["id"])
        out.append({
            "id": s["id"], "name": s["name"], "pool": pool["name"],
            "capabilities": models.from_json(s["capabilities"]),
            "members": members, "links": models.from_json(s["links"]),
            "diagram": s["diagram"],
            "availability": s["availability"],
            "held_by": reservation_summary(conn, hold) if hold else None,
            "queue": [reservation_summary(conn, r) for r in models.queued_for(conn, s["id"])],
            "dirty": [_dirty_object(conn, r) for r in models.dirty_reservations(conn, s["id"])],
        })
    return out


def _dirty_object(conn, row) -> dict:
    """A reservation whose residue is not cleared, with what its last cleanup / reconcile
    found: the admin.html quarantine banner (plan/09 §9)."""
    last = [j for j in models.jobs_for_reservation(conn, row["id"])
            if j["kind"] in ("cleanup", "reconcile")]
    job = last[-1] if last else None
    return {**reservation_summary(conn, row),
            "residue_job": job["id"] if job else None,
            "residue": job["error"] if job else None}


@app.get("/availability")
def availability(usecase: str,
                 from_: str | None = Query(None, alias="from"),
                 to: str | None = Query(None, alias="to"),
                 user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db_conn)):
    """Busy windows for the date picker (plan/09 §4). Each window is a live reservation's
    blocked interval ``[start - lead, end + cleanup)`` on a capable setup overlapping the
    ``[from, to)`` range — the same interval `place` tests, so the picker greys exactly the
    slots the scheduler would refuse; the gaps are the free windows. The owner is exposed
    only to admins, everyone else sees a boolean `mine` instead."""
    now = datetime.now(timezone.utc)
    uc = models.usecase_by_name(conn, usecase)
    if uc is None:
        raise DaolabError("not_found", 404, f"unknown use case: {usecase}", {"usecase": usecase})
    start = _parse_start(from_, now)
    end = _parse_start(to, start + timedelta(hours=config.settings.sched_horizon_hours))
    if end <= start:
        raise DaolabError("validation_error", 422, "to: must be after from")
    requires = set(models.from_json(uc["requires"]))
    admin = user["role"] == "admin"
    windows = []
    for setup in models.list_setups(conn):
        if not requires <= set(models.from_json(setup["capabilities"])):
            continue
        for b0, b1, rid in scheduler.busy_windows(conn, setup["id"], start, end, _sched_config()):
            r = models.reservation_by_id(conn, rid)
            window = {"setup": setup["name"], "start_at": r["start_at"], "end_at": r["end_at"],
                      "blocked_start_at": models.format_ts(b0),
                      "blocked_end_at": models.format_ts(b1),
                      "state": r["state"], "mine": r["user_id"] == user["id"]}
            if admin:
                window["owner"] = models.user_by_id(conn, r["user_id"])["username"]
            windows.append(window)
    windows.sort(key=lambda w: (w["blocked_start_at"], w["setup"]))
    return {"usecase": usecase, "from": models.format_ts(start), "to": models.format_ts(end),
            "slot_min": config.settings.slot_min, "windows": windows}


@app.get("/users")
def users(user: sqlite3.Row = Depends(require_admin),
          conn: sqlite3.Connection = Depends(db_conn)):
    return models.list_users(conn)


@app.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch,
               admin: sqlite3.Row = Depends(require_admin),
               conn: sqlite3.Connection = Depends(db_conn)):
    target = models.user_by_id(conn, user_id)
    if target is None:
        raise DaolabError("not_found", 404, f"unknown user id: {user_id}", {"user_id": user_id})
    if not body.enabled and target["id"] == admin["id"]:
        raise DaolabError("invalid_state", 409, "cannot disable your own account",
                          {"user_id": user_id})
    models.update(conn, "users", user_id, {"enabled": int(body.enabled)})
    models.audit(conn, admin["username"], "admin_override", "user", user_id,
                 {"username": target["username"], "enabled": bool(body.enabled)})
    return user_object(models.user_by_id(conn, user_id))


@app.post("/admin/setups/{name}/force-clean", status_code=202)
def force_clean(name: str, body: ForceClean | None = None,
                admin: sqlite3.Row = Depends(require_admin),
                conn: sqlite3.Connection = Depends(db_conn)):
    """Clear a setup's quarantine (plan/09 §9, B-74), audit-logged. Admin only.

    Default: queue a `reconcile` job for every dirty reservation on the setup (one already
    waiting is reused). The engine destroys the leftovers, verifies, and releases the
    reservations; the setup goes back to `available` once the last one verifies clean
    (B-71), so a queued booking never starts on a testbed that is still dirty. With nothing
    dirty, a quarantined setup is cleared at once.

    `{"skip_verify": true}`: the admin vouches that the testbed is clean (e.g. verify keeps
    flagging something they checked by hand). Every dirty reservation is released and its
    addresses freed now, without running anything. A `maintenance` flag is never touched."""
    skip = bool(body and body.skip_verify)
    setup = models.setup_by_name(conn, name)
    if setup is None:
        raise DaolabError("not_found", 404, f"unknown setup: {name}", {"setup": name})
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = setup["availability"]
        dirty = models.dirty_reservations(conn, setup["id"])
        job_ids = []
        if skip:
            for r in dirty:
                models.transition_reservation(conn, r["id"], "released", from_state=r["state"])
                ipam.clear(conn, r["id"])
        else:
            for r in dirty:
                waiting = [j for j in models.jobs_for_reservation(conn, r["id"], kind="reconcile")
                           if j["state"] in ("pending", "running")]
                job_ids.append(waiting[-1]["id"] if waiting
                               else models.create_job(conn, r["id"], "reconcile"))
        if previous == "quarantined" and (skip or not dirty):
            models.set_setup_availability(conn, setup["id"], "available")
        availability = conn.execute("SELECT availability FROM setups WHERE id = ?",
                                    (setup["id"],)).fetchone()[0]
        models.audit(conn, admin["username"], "force_clean", "setup", setup["id"], {
            "setup": name, "skip_verify": skip, "previous_availability": previous,
            "availability": availability, "reservations": [r["id"] for r in dirty],
            "jobs": job_ids})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"setup": name, "availability": availability, "previous_availability": previous,
            "skip_verify": skip, "reservations": [r["id"] for r in dirty],
            "jobs": [job_object(models.job_by_id(conn, j)) for j in job_ids]}


@app.get("/admin/audit")
def admin_audit(limit: int = 100, admin: sqlite3.Row = Depends(require_admin),
                conn: sqlite3.Connection = Depends(db_conn)):
    """Append-only log of who did what (B-55). Admin-only; newest first."""
    rows = models.list_audit_events(conn, max(1, min(limit, 1000)))
    return [{"id": r["id"], "actor": r["actor"], "action": r["action"],
             "object_type": r["object_type"], "object_id": r["object_id"],
             "detail": models.from_json(r["detail"]) if r["detail"] else None,
             "trace_id": r["trace_id"], "created_at": r["created_at"]} for r in rows]


def _parse_start(value: str | None, now: datetime) -> datetime:
    if value is None or value == "now":
        return now
    try:
        return models.parse_ts(value)
    except ValueError:
        raise DaolabError("validation_error", 422, f"start_at: invalid timestamp: {value}")


@app.post("/reservations", status_code=201)
def create_reservation(body: ReservationCreate,
                       user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    now = datetime.now(timezone.utc)
    hours = body.hours if body.hours is not None else config.settings.default_hours
    if hours < 1:
        raise DaolabError("validation_error", 422, "hours: must be >= 1")
    start = _parse_start(body.start_at, now)
    is_diy = body.usecase == "diy"
    if is_diy and (body.setup is None or body.templates is None):
        raise DaolabError("validation_error", 422,
                          "setup and templates are required when usecase is diy")
    if not is_diy and (body.setup is not None or body.templates is not None):
        raise DaolabError("validation_error", 422,
                          "setup and templates are only valid when usecase is diy")

    uc = models.usecase_by_name(conn, body.usecase)
    if uc is None:
        raise DaolabError("not_found", 404, f"unknown use case: {body.usecase}",
                          {"usecase": body.usecase})
    if uc["yaml_path"] is None:
        raise DaolabError("usecase_unavailable", 409, f"{body.usecase} is planned (no YAML yet)",
                          {"reason": "no_yaml"})
    if is_diy:
        setup = models.setup_by_name(conn, body.setup)
        if setup is None:
            raise DaolabError("not_found", 404, f"unknown setup: {body.setup}",
                              {"setup": body.setup})
        hosts = models.setup_vm_hosts(conn, setup["id"])
        if set(body.templates.keys()) != hosts:
            missing = sorted(hosts - set(body.templates.keys()))
            extra = sorted(set(body.templates.keys()) - hosts)
            parts = []
            if missing:
                parts.append(f"templates missing hosts: {', '.join(missing)}")
            if extra:
                parts.append(f"unknown hosts: {', '.join(extra)}")
            raise DaolabError("validation_error", 422, "; ".join(parts))

    ssh_pubkey, access_password = _deploy_access(body.ssh_pubkey)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if user["role"] != "admin":
            for reason, states, limit in (
                    ("live", models.LIVE_RESERVATION_STATES,
                     config.settings.max_live_reservations),
                    ("future", models.FUTURE_RESERVATION_STATES,
                     config.settings.max_future_reservations)):
                current = models.count_user_reservations(conn, user["id"], states)
                if current >= limit:
                    raise DaolabError(
                        "quota_exceeded", 409,
                        f"at most {limit} {reason} reservation(s) per user; "
                        f"you already have {current}",
                        {"reason": reason, "limit": limit, "current": current})
        placement = scheduler.place(
            conn,
            scheduler.PlacementRequest(usecase=body.usecase, requested_start_at=start,
                                       hours=hours, setup=body.setup, mode=body.mode),
            now, _sched_config())
        if isinstance(placement, scheduler.NoSetup):
            raise DaolabError(
                "unmet_capability", 409,
                f"no setup satisfies requires for {body.usecase}; "
                f"missing: {', '.join(placement.missing)}",
                {"usecase": body.usecase, "missing": placement.missing,
                 "closest_setup": placement.closest_setup})
        if isinstance(placement, scheduler.NoFreeSlot):
            raise DaolabError("usecase_unavailable", 409,
                              f"no free slot for {body.usecase} within "
                              f"{placement.horizon_hours}h",
                              {"reason": "no_free_slot",
                               "horizon_hours": placement.horizon_hours})
        if isinstance(placement, scheduler.NoFit):
            raise DaolabError(
                "slot_conflict", 409,
                f"{body.usecase}: requested window "
                f"{models.format_ts(placement.start_at)}.."
                f"{models.format_ts(placement.end_at)} is not available "
                f"({placement.reason})",
                {"usecase": body.usecase,
                 "start_at": models.format_ts(placement.start_at),
                 "end_at": models.format_ts(placement.end_at),
                 "reason": placement.reason,
                 "alternatives": [models.format_ts(a) for a in placement.alternatives]})
        rid = models.insert(conn, "reservations", {
            "user_id": user["id"], "usecase_id": uc["id"], "setup_id": placement.setup_id,
            "state": "queued", "requested_start_at": models.format_ts(start),
            "start_at": models.format_ts(placement.start_at),
            "end_at": models.format_ts(placement.end_at),
            "diy": int(is_diy),
            "diy_templates": models.to_json(body.templates) if is_diy else None,
            "ssh_pubkey": ssh_pubkey, "access_password": access_password,
        })
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return reservation_object(conn, models.reservation_by_id(conn, rid))


@app.get("/reservations")
def list_reservations(all: int = 0,
                      user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db_conn)):
    if all:
        if user["role"] != "admin":
            raise DaolabError("forbidden", 403, "admin role required for GET /reservations?all=1",
                              {"required_role": "admin"})
        rows = models.list_reservations(conn)
    else:
        rows = models.list_reservations(conn, user["id"])
    return [reservation_object(conn, r) for r in rows]


def _owned_reservation(conn, res_id: int, user) -> sqlite3.Row:
    row = models.reservation_by_id(conn, res_id)
    if row is None:
        raise DaolabError("not_found", 404, f"unknown reservation: {res_id}", {"id": res_id})
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise DaolabError("forbidden", 403, "not your reservation", {"id": res_id})
    return row


@app.get("/reservations/{res_id}")
def get_reservation(res_id: int, user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db_conn)):
    return reservation_object(conn, _owned_reservation(conn, res_id, user))


@app.delete("/reservations/{res_id}")
def cancel_reservation(res_id: int, user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    row = _owned_reservation(conn, res_id, user)
    if row["state"] == "queued":
        new_state = "released"        # nothing ran, nothing to clean
    elif row["state"] in ("ready", "active"):
        new_state = "cleaning"        # the engine runs the cleanup
    else:
        raise DaolabError("invalid_state", 409,
                          f"cannot cancel a reservation in state {row['state']}",
                          {"state": row["state"]})
    try:
        models.transition_reservation(conn, res_id, new_state, from_state=row["state"])
    except models.ReservationStateError:   # the engine moved it between our read and write
        current = models.reservation_by_id(conn, res_id)["state"]
        raise DaolabError("invalid_state", 409,
                          f"cannot cancel a reservation in state {current}",
                          {"state": current})
    models.audit(conn, user["username"], "cancel", "reservation", res_id,
                 {"previous_state": row["state"], "state": new_state})
    return {"id": res_id, "state": new_state}


@app.post("/reservations/{res_id}/cleanup", status_code=202)
def cleanup_reservation(res_id: int, user: sqlite3.Row = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db_conn)):
    """Owner ends a running slot early (plan/09 §4): `ready`/`active` -> `cleaning` plus a
    pending `cleanup` job the engine will run, `202`. Any other state -> `409 invalid_state`."""
    row = _owned_reservation(conn, res_id, user)
    if row["state"] not in ("ready", "active"):
        raise DaolabError("invalid_state", 409,
                          f"cannot clean up a reservation in state {row['state']}",
                          {"state": row["state"]})
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            models.transition_reservation(conn, res_id, "cleaning", from_state=row["state"])
        except models.ReservationStateError:   # the engine moved it under us
            current = models.reservation_by_id(conn, res_id)["state"]
            raise DaolabError("invalid_state", 409,
                              f"cannot clean up a reservation in state {current}",
                              {"state": current})
        job_id = models.create_job(conn, res_id, "cleanup")
        models.audit(conn, user["username"], "cleanup", "reservation", res_id,
                     {"previous_state": row["state"], "state": "cleaning"})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": res_id, "state": "cleaning",
            "job": job_object(models.job_by_id(conn, job_id))}


@app.post("/reservations/{res_id}/redeploy", status_code=202)
def redeploy_reservation(res_id: int, user: sqlite3.Row = Depends(current_user),
                         conn: sqlite3.Connection = Depends(db_conn)):
    """Owner re-runs `deploy.yml` for a live reservation (plan/09 §4) — idempotent replay,
    allowed only inside its slot. Returns `202` + the new `deploy` job (attempt + 1); a
    request outside `[start_at, end_at)` or in any non-live state is `409 invalid_state`."""
    row = _owned_reservation(conn, res_id, user)
    now = datetime.now(timezone.utc)
    if not (models.parse_ts(row["start_at"]) <= now < models.parse_ts(row["end_at"])):
        raise DaolabError("invalid_state", 409, f"reservation {res_id} is outside its slot",
                          {"state": row["state"], "start_at": row["start_at"],
                           "end_at": row["end_at"]})
    if row["state"] not in ("ready", "active"):
        raise DaolabError("invalid_state", 409,
                          f"cannot redeploy a reservation in state {row['state']}",
                          {"state": row["state"]})
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = models.jobs_for_reservation(conn, res_id, kind="deploy")
        attempt = max((j["attempt"] for j in prev), default=0) + 1
        job_id = models.create_job(conn, res_id, "deploy", attempt=attempt)
        models.audit(conn, user["username"], "redeploy", "reservation", res_id,
                     {"attempt": attempt})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": res_id, "state": row["state"],
            "job": job_object(models.job_by_id(conn, job_id))}


# ---- health (B-66, plan/09 §8) ----------------------------------------------

_HEALTH_CACHE_SEC = 15.0
_health_cache: dict[int, tuple[float, dict]] = {}      # reservation id -> (monotonic, verdict)
_LIVE_STATES = ("ready", "active")


def _latest_job(conn, res_id: int, kind: str):
    jobs = models.jobs_for_reservation(conn, res_id, kind=kind)
    return jobs[-1] if jobs else None


def _probe_summary(job) -> dict | None:
    if job is None:
        return None
    return {"job_id": job["id"], "state": job["state"], "error": job["error"],
            "steps": models.job_steps(job), "finished_at": job["finished_at"]}


def _evaluate_cached(conn, res_id: int) -> tuple[dict, bool]:
    """The verdict for a live reservation, at most _HEALTH_CACHE_SEC old. Returns (verdict,
    served_from_cache). A dashboard polling every 30 s per card costs one evaluation per 15 s
    per reservation, however many tabs are open."""
    hit = _health_cache.get(res_id)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _HEALTH_CACHE_SEC:
        return hit[1], True
    verdict = health_eval.verdict_object(
        health_eval.evaluate_reservation(conn, res_id, datetime.now(timezone.utc)))
    _health_cache[res_id] = (now, verdict)
    return verdict, False


@app.get("/reservations/{res_id}/health")
def reservation_health(res_id: int, user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    """The latest health verdict with every check (owner). Evaluated from Prometheus with a
    15 s cache when the telemetry stack is on; otherwise `unknown`, and health is whatever
    the last probe job (POST .../health/probe) found. Always carries the deploy trace id."""
    row = _owned_reservation(conn, res_id, user)
    deploy = _latest_job(conn, res_id, "deploy")
    probe = _probe_summary(_latest_job(conn, res_id, "health_probe"))
    base = {"reservation_id": res_id, "state": row["state"],
            "trace_id": deploy["trace_id"] if deploy else None, "probe": probe,
            "cached": False}
    if row["state"] not in _LIVE_STATES:
        return {**base, "verdict": "unknown", "checks": [], "evaluated_at": None,
                "last_seen": None, "reason": f"reservation is {row['state']}"}
    if not (config.settings.otel_endpoint or "").strip():
        return {**base, "verdict": "unknown", "checks": [], "evaluated_at": None,
                "last_seen": None,
                "reason": "telemetry stack is off (DAOLAB_OTEL_ENDPOINT unset); "
                          "see the probe result"}
    verdict, cached = _evaluate_cached(conn, res_id)
    return {**base, **verdict, "cached": cached}


@app.post("/reservations/{res_id}/health/probe", status_code=202)
def probe_reservation(res_id: int, user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db_conn)):
    """On-demand active check (owner): queues a `health_probe` job, which runs the use case's
    verify command; the result is recorded on the job. A probe already waiting or running is
    returned instead of queueing another. 409 unless the reservation is ready or active."""
    row = _owned_reservation(conn, res_id, user)
    if row["state"] not in _LIVE_STATES:
        raise DaolabError("invalid_state", 409,
                          f"cannot probe a reservation in state {row['state']}",
                          {"state": row["state"]})
    conn.execute("BEGIN IMMEDIATE")
    try:
        waiting = [j for j in models.jobs_for_reservation(conn, res_id, kind="health_probe")
                   if j["state"] in ("pending", "running")]
        job_id = waiting[-1]["id"] if waiting else models.create_job(conn, res_id, "health_probe")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": res_id, "state": row["state"], "job": job_object(models.job_by_id(conn, job_id))}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(db_conn)):
    """A job's state, steps, `trace_id` and log tail (plan/09 §4); owner-gated."""
    job = models.job_by_id(conn, job_id)
    if job is None:
        raise DaolabError("not_found", 404, f"unknown job: {job_id}", {"id": job_id})
    _owned_reservation(conn, job["reservation_id"], user)
    return job_object(job)


@app.get("/reservations/{res_id}/events")
def reservation_events(res_id: int, user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db_conn)):
    """SSE of a reservation's state changes and job steps (owner only); the dashboard
    progress bar uses this and falls back to polling (plan/09 §4)."""
    _owned_reservation(conn, res_id, user)
    return StreamingResponse(_reservation_events(conn, res_id),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/internal/vm-ip")
def vm_ip_callback(body: VmIpCallback, _: None = Depends(require_internal),
                   conn: sqlite3.Connection = Depends(db_conn)):
    vm = models.vm_by_role(conn, body.reservation_id, body.role)
    if vm is None:
        raise DaolabError("not_found", 404,
                          f"no vm for reservation {body.reservation_id} role {body.role}",
                          {"reservation_id": body.reservation_id, "role": body.role})
    models.set_vm_ip(conn, vm["id"], body.ip)     # idempotent: re-posting is a harmless UPDATE
    return {"ok": True}


# ---- IP leases for the deploy playbook (B-42, plan/09 §6) ------------------

_LEASE_STATES = ("provisioning", "ready", "active")     # deploying, or a redeploy in the slot


def _lease_target(conn, reservation_id: int, role: str):
    """The reservation + VM a playbook may lease for; raises the API error otherwise."""
    res = models.reservation_by_id(conn, reservation_id)
    if res is None:
        raise DaolabError("not_found", 404, f"unknown reservation: {reservation_id}",
                          {"reservation_id": reservation_id})
    if res["state"] not in _LEASE_STATES:
        raise DaolabError("invalid_state", 409,
                          f"reservation {reservation_id} is {res['state']}; addresses are "
                          f"leased only while {', '.join(_LEASE_STATES)}",
                          {"reservation_id": reservation_id, "state": res["state"]})
    vm = models.vm_by_role(conn, reservation_id, role)
    if vm is None:
        raise DaolabError("not_found", 404,
                          f"no vm for reservation {reservation_id} role {role}",
                          {"reservation_id": reservation_id, "role": role})
    if vm["name"] is None:
        raise DaolabError("validation_error", 422,
                          f"role {role} runs on the DPU and has no VM to lease an address for",
                          {"reservation_id": reservation_id, "role": role})
    return res


def _lease(conn, reservation_id: int, role: str):
    try:
        return ipam.lease(conn, reservation_id, role)
    except ipam.PoolExhausted as e:
        raise DaolabError("pool_exhausted", 409, str(e), {"reservation_id": reservation_id})
    except ipam.IpamError as e:
        raise DaolabError("no_ip_pool", 409, str(e), {})


@app.post("/internal/leases")
def lease_ip(body: LeaseRequest, _: None = Depends(require_internal),
             conn: sqlite3.Connection = Depends(db_conn)):
    """The deploy playbook asks for a VM's address (plan/09 §6). Idempotent per
    (reservation, role): a replayed playbook gets the same address back. Only while the
    reservation is deploying or live; only for VM roles (the DPU has its own address)."""
    conn.execute("BEGIN IMMEDIATE")              # state check and lease see the same row
    try:
        _lease_target(conn, body.reservation_id, body.role)
        lease = _lease(conn, body.reservation_id, body.role)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ipam.lease_object(lease)


@app.post("/internal/leases/conflict")
def lease_conflict(body: LeaseConflict, _: None = Depends(require_internal),
                   conn: sqlite3.Connection = Depends(db_conn)):
    """Something outside daolab answers on a leased address (the playbook pings / arpings it
    before use). The address is quarantined so it is never issued again, and the same
    (reservation, role) gets a new one, returned here."""
    try:
        ip = str(ipaddress.ip_address(body.ip))
    except ValueError:
        raise DaolabError("validation_error", 422, f"ip: not an IP address: {body.ip}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            bad = ipam.mark_conflict(conn, ip)
        except ipam.IpamError as e:
            raise DaolabError("not_found", 404, str(e), {"ip": ip})
        _lease_target(conn, bad["reservation_id"], bad["role"])
        lease = _lease(conn, bad["reservation_id"], bad["role"])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    log.warning("ip %s answers outside daolab: quarantined; reservation %s role %s now has %s",
                ip, bad["reservation_id"], bad["role"], lease["ip"])
    return {**ipam.lease_object(lease), "replaced": ip}


# ---- DPU access: sshd AuthorizedKeysCommand (B-51, plan/09 §7) -------------

def _authz_secret_ok(given: str | None) -> bool:
    """The DPU's `daolab-keys` holds DAOLAB_AUTHZ_SECRET, a read-only secret that cannot lease
    addresses or post VM IPs. DAOLAB_INTERNAL_SECRET is accepted too (it is the superset)."""
    if not given:
        return False
    for secret in (config.settings.authz_secret, config.settings.internal_secret):
        if secret and hmac.compare_digest(given, secret):
            return True
    return False


@app.get("/internal/authz/keys", response_class=PlainTextResponse)
def authz_keys(host: str = "", login: str = "",
               x_daolab_secret: str | None = Header(None)):
    """The authorized_keys lines valid for `login`@`host` right now, as plain text sshd reads
    directly (one key per line). Empty means no access, and so does every error: the caller
    is sshd, so failing closed is the only safe answer."""
    if not _authz_secret_ok(x_daolab_secret):
        return PlainTextResponse("", status_code=401)
    conn = None
    try:
        conn = models.connect(config.settings.db_path)
        keys = access.keys_for(conn, host, login, datetime.now(timezone.utc))
    except Exception:                                     # noqa: BLE001 — fail closed
        log.exception("authz keys for %s@%s failed; answering no keys", login, host)
        return PlainTextResponse("")
    finally:
        if conn is not None:
            conn.close()
    return PlainTextResponse("".join(f"{k}\n" for k in keys))


# ---- static dashboards (registered after all API routes) -------------------

def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(config.settings.dashboard_dir, name), media_type="text/html")


@app.get("/")
def index():
    return _page("user.html")


@app.get("/user.html")
def user_page():
    return _page("user.html")


@app.get("/admin.html")
def admin_page():
    return _page("admin.html")
