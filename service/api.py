"""FastAPI service: catalog / pools / setups / users / reservations + internal
VM-IP callback; serves dashboard/. Views over the DB only — YAML is read by
`daolab sync` (catalog.py), never by request handlers."""
import hashlib
import hmac
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from . import config, models, otel, scheduler

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


class VmIpCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reservation_id: int
    role: str
    ip: str


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


# ---- serializers -----------------------------------------------------------

def _sched_config() -> scheduler.SchedConfig:
    s = config.settings
    return scheduler.SchedConfig(hw_class_lead_min=s.hw_class_lead_min,
                                 provision_lead_min=s.provision_lead_min,
                                 horizon_hours=s.sched_horizon_hours,
                                 cleanup_min=s.cleanup_min,
                                 slot_min=s.slot_min)


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
    for vm in models.vms_for_reservation(conn, row["id"]):
        ssh = None
        if vm["ip"] and vm["name"]:                       # on: dpu roles have no VM, no ssh
            ssh = f"ssh {user['username']}@{vm['ip']}"
        vms.append({"role": vm["role"], "host": vm["host"], "template": vm["template"],
                    "name": vm["name"], "ip": vm["ip"], "ssh": ssh,
                    "username": user["username"] if vm["name"] else None,
                    "auth": "ssh-key" if vm["name"] else None})
    return {
        "id": row["id"], "user": user["username"], "usecase": uc["name"],
        "setup": setup["name"], "state": row["state"], "diy": bool(row["diy"]),
        "requested_start_at": row["requested_start_at"], "start_at": row["start_at"],
        "end_at": row["end_at"], "shifted": row["start_at"] != row["requested_start_at"],
        "failure_reason": row["failure_reason"],
        "vms": vms, "diagram": _reservation_diagram(conn, row, user, uc),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def reservation_summary(conn, row) -> dict:
    user = models.user_by_id(conn, row["user_id"])
    uc = conn.execute("SELECT name FROM usecases WHERE id = ?", (row["usecase_id"],)).fetchone()
    return {"id": row["id"], "user": user["username"], "usecase": uc["name"],
            "state": row["state"], "start_at": row["start_at"], "end_at": row["end_at"]}


def user_object(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "enabled": bool(row["enabled"]), "created_at": row["created_at"]}


# ---- routes ----------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/whoami")
def whoami(user: sqlite3.Row = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "created_at": user["created_at"]}


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
            "held_by": reservation_summary(conn, hold) if hold else None,
            "queue": [reservation_summary(conn, r) for r in models.queued_for(conn, s["id"])],
        })
    return out


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
        new_state = "released"        # nothing ran, nothing to tear down
    elif row["state"] in ("ready", "active"):
        new_state = "tearing_down"    # the engine runs the teardown
    else:
        raise DaolabError("invalid_state", 409,
                          f"cannot cancel a reservation in state {row['state']}",
                          {"state": row["state"]})
    models.update(conn, "reservations", res_id, {"state": new_state})
    models.audit(conn, user["username"], "cancel", "reservation", res_id,
                 {"previous_state": row["state"], "state": new_state})
    return {"id": res_id, "state": new_state}


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
