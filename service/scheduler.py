"""Pure placement: FCFS, capability match, slot-shift, hw_class lead time.

No I/O other than the sqlite handle passed in (SELECTs only) — no clock reads,
no env reads, no writes. Same DB state + same request + same now -> identical result.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SchedConfig:
    hw_class_lead_min: int = 15   # DAOLAB_HW_CLASS_LEAD_MIN — EBF reflash lead
    provision_lead_min: int = 5   # DAOLAB_PROVISION_LEAD_MIN — normal lead
    horizon_hours: int = 168      # DAOLAB_SCHED_HORIZON_HOURS — search horizon


@dataclass(frozen=True)
class PlacementRequest:
    usecase: str                    # natural name, e.g. "tls-proxy-nginx" or "diy"
    requested_start_at: datetime    # UTC-aware; the literal "now" already resolved by api.py
    hours: int                      # >= 1
    setup: str | None = None        # diy only: the explicitly named setup


@dataclass(frozen=True)
class Placed:
    setup_id: int
    setup_name: str
    start_at: datetime
    end_at: datetime
    shifted: bool                   # start_at != requested_start_at
    lead_min: int                   # informational; engine re-derives identically


@dataclass(frozen=True)
class NoSetup:                      # -> 409 unmet_capability (message MUST name `missing`)
    missing: list[str]
    closest_setup: str | None


@dataclass(frozen=True)
class NoFreeSlot:                   # -> catalog reason "no_free_slot"
    horizon_hours: int


@dataclass(frozen=True)
class _Slot:
    start: datetime
    lead_min: int


class _Timeline:
    def __init__(self, busy, steps, initial):
        self.busy = sorted(busy)               # [(w0, end)] half-open
        self.steps = steps                     # [(start_at, class after this reservation)]
        self.initial = initial

    def class_at(self, t: datetime):
        cur = self.initial
        for start_at, cls in self.steps:
            if start_at < t:
                cur = cls
            else:
                break
        return cur


TIMELINE_SQL = """
SELECT r.id, r.start_at, r.end_at, u.hw_class
FROM reservations r JOIN usecases u ON u.id = r.usecase_id
WHERE r.setup_id = ? AND r.state NOT IN ('released', 'failed')
ORDER BY r.start_at, r.id
"""


def _parse(text: str) -> datetime:
    from datetime import timezone
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lead_min(uc_hw_class: str | None, projected: str | None,
             cfg: SchedConfig, has_dpu: bool) -> int:
    if has_dpu and uc_hw_class is not None and uc_hw_class != projected:
        return cfg.hw_class_lead_min     # EBF reflash takes real time
    return cfg.provision_lead_min        # same class, null class, or no DPU member


def _setup_dpu(db, setup_id: int):
    row = db.execute(
        "SELECT hw_class FROM resources WHERE setup_id = ? AND kind = 'dpu' ORDER BY name LIMIT 1",
        (setup_id,),
    ).fetchone()
    return (row is not None, row["hw_class"] if row else None)


def _timeline(db, setup_id: int, dpu_hw_class, has_dpu: bool, cfg: SchedConfig) -> _Timeline:
    rows = db.execute(TIMELINE_SQL, (setup_id,)).fetchall()
    cur = dpu_hw_class
    busy, steps = [], []
    for r in rows:
        start, end = _parse(r["start_at"]), _parse(r["end_at"])
        lead = lead_min(r["hw_class"], cur, cfg, has_dpu)
        busy.append((start - timedelta(minutes=lead), end))
        if has_dpu and r["hw_class"] is not None:
            cur = r["hw_class"]          # null hw_class never changes the projection
        steps.append((start, cur))
    return _Timeline(busy, steps, dpu_hw_class)


def _sweep(tl: _Timeline, uc_hw_class, base: datetime, hours: int,
           horizon_end: datetime, cfg: SchedConfig, has_dpu: bool) -> _Slot | None:
    s = base
    while s + timedelta(hours=hours) <= horizon_end:
        lead = lead_min(uc_hw_class, tl.class_at(s), cfg, has_dpu)
        w0, w1 = s - timedelta(minutes=lead), s + timedelta(hours=hours)
        hit = next((b for b in tl.busy if b[0] < w1 and b[1] > w0), None)
        if hit is None:
            return _Slot(start=s, lead_min=lead)
        just_after = hit[1] + timedelta(seconds=1)      # class_at is a step function
        s = hit[1] + timedelta(minutes=lead_min(uc_hw_class, tl.class_at(just_after), cfg, has_dpu))
    return None


def _candidates(db, requires: list[str], setup_name: str | None):
    rows = db.execute("SELECT id, name, capabilities FROM setups ORDER BY name").fetchall()
    if setup_name is not None:
        return [r for r in rows if r["name"] == setup_name], rows
    need = set(requires)
    return [r for r in rows if need <= set(json.loads(r["capabilities"]))], rows


def place(db: sqlite3.Connection, req: PlacementRequest, now: datetime,
          cfg: SchedConfig = SchedConfig()) -> Placed | NoSetup | NoFreeSlot:
    uc = db.execute(
        "SELECT id, name, requires, hw_class FROM usecases WHERE name = ?", (req.usecase,)
    ).fetchone()
    assert uc is not None, "api.py validates the use case exists"
    requires = json.loads(uc["requires"])
    uc_hw_class = uc["hw_class"]

    candidates, all_setups = _candidates(db, requires, req.setup)
    if not candidates:
        best = None
        for s in all_setups:
            missing = sorted(set(requires) - set(json.loads(s["capabilities"])))
            if best is None or (len(missing), s["name"]) < (len(best[0]), best[1]):
                best = (missing, s["name"])
        if best is None:
            return NoSetup(missing=sorted(set(requires)), closest_setup=None)
        return NoSetup(missing=best[0], closest_setup=best[1])

    base = max(req.requested_start_at, now)
    horizon_end = now + timedelta(hours=cfg.horizon_hours)
    found = []
    for s in candidates:
        has_dpu, dpu_class = _setup_dpu(db, s["id"])
        tl = _timeline(db, s["id"], dpu_class, has_dpu, cfg)
        slot = _sweep(tl, uc_hw_class, base, req.hours, horizon_end, cfg, has_dpu)
        if slot is not None:
            found.append((slot.start, slot.lead_min, s["name"], s["id"]))
    if not found:
        return NoFreeSlot(horizon_hours=cfg.horizon_hours)

    start, lead, name, sid = min(found)
    return Placed(setup_id=sid, setup_name=name, start_at=start,
                  end_at=start + timedelta(hours=req.hours),
                  shifted=start != req.requested_start_at, lead_min=lead)


def next_free_at(db: sqlite3.Connection, usecase: str, now: datetime,
                 cfg: SchedConfig = SchedConfig()) -> datetime | None:
    result = place(db, PlacementRequest(usecase=usecase, requested_start_at=now, hours=1), now, cfg)
    return result.start_at if isinstance(result, Placed) else None
