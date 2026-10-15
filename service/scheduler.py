"""Pure placement: FCFS, capability match, slot-shift, hw_class lead time.

Two booking modes (plan/09 §4-5):
  * ``asap`` (default) — earliest free slot at or after the requested start (slot-shift).
  * ``at``             — exactly the requested window: one overlap test, never shifted.
                         If the window is not free the result is ``NoFit``, which carries the
                         next ``NUM_ALTERNATIVES`` free starts for the same duration (B-13).

Every reservation blocks ``[start_at - lead, end_at + cleanup_min)``: the lead before it
(provision, or EBF reflash when the hw_class changes) and the cleanup budget after it, run
one after the other. Back-to-back bookings are therefore ``cleanup + lead`` apart: 15 min for
the same hw_class, 25 min when a reflash is needed (plan/09 §5).

Slot grid (B-12): a granted start is always a multiple of ``slot_min`` minutes after UTC
midnight (30 by default -> :00 and :30). ``asap`` rounds each candidate start up to the next
grid point; ``at`` never shifts, so an off-grid ``at`` start is refused (``NoFit("off_grid")``).

No I/O other than the sqlite handle passed in (SELECTs only) — no clock reads,
no env reads, no writes. Same DB state + same request + same now -> identical result.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

Mode = Literal["asap", "at"]
NUM_ALTERNATIVES = 3              # plan/09 s4: "the next three free starts of the same length"


@dataclass(frozen=True)
class SchedConfig:
    hw_class_lead_min: int = 15   # DAOLAB_HW_CLASS_LEAD_MIN — EBF reflash lead
    provision_lead_min: int = 5   # DAOLAB_PROVISION_LEAD_MIN — normal lead
    horizon_hours: int = 168      # DAOLAB_SCHED_HORIZON_HOURS — search horizon
    cleanup_min: int = 10         # DAOLAB_CLEANUP_MIN — turnaround buffer after every reservation
    slot_min: int = 30            # DAOLAB_SLOT_MIN — start-time grid (minutes after UTC midnight)


@dataclass(frozen=True)
class PlacementRequest:
    usecase: str                    # natural name, e.g. "tls-proxy-nginx" or "diy"
    requested_start_at: datetime    # UTC-aware; the literal "now" already resolved by api.py
    hours: int                      # >= 1
    setup: str | None = None        # diy only: the explicitly named setup
    mode: Mode = "asap"             # "asap": shift to the earliest fit; "at": exact window only


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
class NoFit:                        # mode "at" only -> 409 slot_conflict (alternatives: B-13)
    start_at: datetime              # the exact window that was asked for ...
    end_at: datetime                # ... and could not be granted
    reason: Literal["overlap", "in_the_past", "off_grid"]
    alternatives: tuple[datetime, ...] = ()   # next free starts, same duration, ascending (B-13)


@dataclass(frozen=True)
class _Slot:
    start: datetime
    lead_min: int


@dataclass(frozen=True)
class _Row:
    start: datetime
    end: datetime
    cls: str | None                  # the use case's hw_class (None: never reflashes)
    busy: tuple[datetime, datetime]  # [start - lead, end + cleanup), half-open


class _Timeline:
    def __init__(self, rows, steps, initial):
        self.rows = rows                       # live reservations, ordered by (start_at, id)
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


def snap_up(t: datetime, slot_min: int) -> datetime:
    """Earliest grid point at or after `t`: a multiple of `slot_min` minutes after UTC midnight.
    Whole minutes only (seconds round up too), so `slot_min=1` means "whole minutes"."""
    t = t.astimezone(timezone.utc)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=slot_min)
    return midnight + -((midnight - t) // step) * step     # ceil((t - midnight) / step) steps


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
    out, steps = [], []
    for r in rows:
        start, end = _parse(r["start_at"]), _parse(r["end_at"])
        lead = lead_min(r["hw_class"], cur, cfg, has_dpu)
        out.append(_Row(start, end, r["hw_class"],
                        (start - timedelta(minutes=lead), end + timedelta(minutes=cfg.cleanup_min))))
        if has_dpu and r["hw_class"] is not None:
            cur = r["hw_class"]          # null hw_class never changes the projection
        steps.append((start, cur))
    return _Timeline(out, steps, dpu_hw_class)


def _conflict(tl: _Timeline, s: datetime, hours: int, lead: int, uc_hw_class,
              cfg: SchedConfig, has_dpu: bool):
    """The existing busy interval that stops a booking at `s` from being granted, else None.

    The booking would block ``[s - lead, s + hours + cleanup)`` (half-open). It is the single
    conflict test shared by `asap` and `at`, so the two modes cannot disagree.
      1. Bookings that start before `s` must be done (their busy end, cleanup included) by
         `s - lead`. Of several, the one ending last is returned so the sweep jumps past all.
      2. The first booking at or after `s` must not need its lead *earlier* than our cleanup
         ends. Its lead is re-derived as if we were already its predecessor: a hw_class change
         the other way round turns its 5 min lead into a 15 min reflash lead. Bookings with no
         hw_class pass the projection through, so the first classed booking after them is
         re-checked too (only when its lead got longer: pairs we did not touch are not our
         business)."""
    w0 = s - timedelta(minutes=lead)
    w1 = s + timedelta(hours=hours, minutes=cfg.cleanup_min)
    before = [r.busy for r in tl.rows if r.start < s and r.busy[1] > w0]
    if before:
        return max(before, key=lambda b: b[1])
    after = [r for r in tl.rows if r.start >= s]
    if not after:
        return None
    cur_old = tl.class_at(s)
    cur_new = uc_hw_class if (has_dpu and uc_hw_class is not None) else cur_old
    first = after[0]
    if first.start - timedelta(minutes=lead_min(first.cls, cur_new, cfg, has_dpu)) < w1:
        return first.busy
    if first.cls is not None:
        return None                      # a classed booking resets the projection: the rest is untouched
    prev = first
    for r in after[1:]:
        if r.cls is None:
            prev = r
            continue
        need_new = lead_min(r.cls, cur_new, cfg, has_dpu)
        if need_new > lead_min(r.cls, cur_old, cfg, has_dpu) and \
                r.start - timedelta(minutes=need_new) < prev.busy[1]:
            return r.busy
        break                            # first classed booking behind the null ones: done
    return None


def _sweep(tl: _Timeline, uc_hw_class, base: datetime, hours: int,
           horizon_end: datetime, cfg: SchedConfig, has_dpu: bool) -> _Slot | None:
    s = snap_up(base, cfg.slot_min)
    while s + timedelta(hours=hours) <= horizon_end:
        lead = lead_min(uc_hw_class, tl.class_at(s), cfg, has_dpu)
        hit = _conflict(tl, s, hours, lead, uc_hw_class, cfg, has_dpu)
        if hit is None:
            return _Slot(start=s, lead_min=lead)
        just_after = hit[1] + timedelta(seconds=1)      # class_at is a step function
        nxt = hit[1] + timedelta(minutes=lead_min(uc_hw_class, tl.class_at(just_after), cfg, has_dpu))
        s = snap_up(max(nxt, s + timedelta(minutes=1)), cfg.slot_min)   # nxt > s always; max() makes a hang impossible
    return None                                         # this candidate has no slot in horizon


def _candidates(db, requires: list[str], setup_name: str | None):
    rows = db.execute("SELECT id, name, capabilities FROM setups ORDER BY name").fetchall()
    if setup_name is not None:
        return [r for r in rows if r["name"] == setup_name], rows
    need = set(requires)
    return [r for r in rows if need <= set(json.loads(r["capabilities"]))], rows


def _place_at(db, req: PlacementRequest, candidates, uc_hw_class,
              now: datetime, cfg: SchedConfig) -> Placed | NoFit:
    """mode "at": test exactly [requested_start_at, +hours) on every candidate setup — one
    overlap test per setup, no sweep, never shifted. The lead window before the start is
    tested too (same rule as `asap`), so `at` is granted exactly when `asap` would grant
    the same start unshifted. Among free candidates the tie-break is (lead, name, id), the
    `asap` order for equal starts. A start before `now` is rejected, not clamped: clamping
    would shift it. The search horizon bounds `asap` only; `at` has no sweep to bound."""
    start = req.requested_start_at
    end = start + timedelta(hours=req.hours)

    def no_fit(reason):
        return NoFit(start_at=start, end_at=end, reason=reason,
                     alternatives=_alternatives(db, req, candidates, uc_hw_class, now, cfg))

    if start < now:
        return no_fit("in_the_past")
    if snap_up(start, cfg.slot_min) != start:
        return no_fit("off_grid")                                      # snapping would be shifting
    free = []
    for s in candidates:
        has_dpu, dpu_class = _setup_dpu(db, s["id"])
        tl = _timeline(db, s["id"], dpu_class, has_dpu, cfg)
        lead = lead_min(uc_hw_class, tl.class_at(start), cfg, has_dpu)
        if _conflict(tl, start, req.hours, lead, uc_hw_class, cfg, has_dpu) is None:
            free.append((lead, s["name"], s["id"]))
    if not free:
        return no_fit("overlap")
    lead, name, sid = min(free)
    return Placed(setup_id=sid, setup_name=name, start_at=start, end_at=end,
                  shifted=False, lead_min=lead)


def _earliest(db, candidates, uc_hw_class, base: datetime, hours: int,
              now: datetime, cfg: SchedConfig):
    """Earliest grid start at or after `base` over all candidate setups, as the tuple
    (start, lead_min, setup_name, setup_id) with the tie-break order, or None if the horizon
    is full. The one search behind `asap` placement and behind NoFit.alternatives."""
    horizon_end = now + timedelta(hours=cfg.horizon_hours)
    found = []
    for s in candidates:
        has_dpu, dpu_class = _setup_dpu(db, s["id"])
        tl = _timeline(db, s["id"], dpu_class, has_dpu, cfg)
        slot = _sweep(tl, uc_hw_class, base, hours, horizon_end, cfg, has_dpu)
        if slot is not None:
            found.append((slot.start, slot.lead_min, s["name"], s["id"]))
    return min(found) if found else None


def _alternatives(db, req: PlacementRequest, candidates, uc_hw_class,
                  now: datetime, cfg: SchedConfig) -> tuple[datetime, ...]:
    """The next NUM_ALTERNATIVES free starts for req.hours, ascending, searching forward from
    max(requested start, now). Each one is free on its own (they may overlap each other: they
    are choices, not a plan), on any capable setup, on the grid, inside the horizon. The first
    is exactly what `asap` would have granted. Fewer than NUM_ALTERNATIVES if the horizon runs out."""
    out, base = [], max(req.requested_start_at, now)
    for _ in range(NUM_ALTERNATIVES):
        best = _earliest(db, candidates, uc_hw_class, base, req.hours, now, cfg)
        if best is None:
            break
        out.append(best[0])
        base = best[0] + timedelta(minutes=cfg.slot_min)       # the next grid point after it
    return tuple(out)


def place(db: sqlite3.Connection, req: PlacementRequest, now: datetime,
          cfg: SchedConfig = SchedConfig()) -> Placed | NoSetup | NoFreeSlot | NoFit:
    if cfg.slot_min < 1:
        raise ValueError(f"slot_min must be >= 1 minute, got {cfg.slot_min}")
    if req.mode not in ("asap", "at"):
        raise ValueError(f"unknown placement mode: {req.mode!r} (expected 'asap' or 'at')")
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

    if req.mode == "at":
        return _place_at(db, req, candidates, uc_hw_class, now, cfg)

    best = _earliest(db, candidates, uc_hw_class, max(req.requested_start_at, now),
                     req.hours, now, cfg)
    if best is None:
        return NoFreeSlot(horizon_hours=cfg.horizon_hours)

    start, lead, name, sid = best
    return Placed(setup_id=sid, setup_name=name, start_at=start,
                  end_at=start + timedelta(hours=req.hours),
                  shifted=start != req.requested_start_at, lead_min=lead)


def next_free_at(db: sqlite3.Connection, usecase: str, now: datetime,
                 cfg: SchedConfig = SchedConfig()) -> datetime | None:
    result = place(db, PlacementRequest(usecase=usecase, requested_start_at=now, hours=1), now, cfg)
    return result.start_at if isinstance(result, Placed) else None


# ---- setup availability gating (B-18) --------------------------------------
# plan/09 §6: booking/placement ignores availability (the scheduler keeps granting
# slots), but the engine must not start provisioning while a setup is not `available`.
# These read-only helpers expose that gate; the engine (B-23) calls them in its
# queued -> provisioning step. They never write, like the rest of this module.

def setup_available(db: sqlite3.Connection, setup_id: int) -> bool:
    """True when the setup may start new provisioning (availability == 'available')."""
    row = db.execute("SELECT availability FROM setups WHERE id = ?", (setup_id,)).fetchone()
    return row is not None and row["availability"] == "available"


def startable_queued(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Queued reservations whose setup is available, in FCFS order — the candidates the
    engine weighs for queued -> provisioning. It still applies the lead time (`place`
    reports it) and single-flight; this only drops quarantined/maintenance setups, so a
    queued reservation stays queued while its setup is quarantined."""
    return db.execute(
        """SELECT r.*
           FROM reservations r JOIN setups s ON s.id = r.setup_id
           WHERE r.state = 'queued' AND s.availability = 'available'
           ORDER BY r.start_at, r.id""").fetchall()


# ---- availability windows (B-16) -------------------------------------------
# plan/09 §4: GET /availability feeds the date picker with the busy windows a new booking
# would collide with. The blocked interval is the same one `place` tests — [start - lead,
# end + cleanup) — so the picker greys exactly the slots the scheduler would refuse.

def busy_windows(db: sqlite3.Connection, setup_id: int, from_dt: datetime, to_dt: datetime,
                 cfg: SchedConfig = SchedConfig()) -> list[tuple[datetime, datetime, int]]:
    """The busy intervals a new booking on `setup_id` would collide with, as
    ``(blocked_start, blocked_end, reservation_id)`` for every live reservation whose
    ``[start - lead, end + cleanup)`` overlaps ``[from_dt, to_dt)``. Read-only and pure,
    like the rest of this module: same DB state + same args -> identical result. The lead
    is projected over the full ordered timeline (as in `_timeline`), so reflashes are
    accounted for even when the preceding booking falls outside the window."""
    has_dpu, dpu_class = _setup_dpu(db, setup_id)
    cur, out = dpu_class, []
    for r in db.execute(TIMELINE_SQL, (setup_id,)).fetchall():
        start, end = _parse(r["start_at"]), _parse(r["end_at"])
        lead = lead_min(r["hw_class"], cur, cfg, has_dpu)
        if has_dpu and r["hw_class"] is not None:
            cur = r["hw_class"]
        b0 = start - timedelta(minutes=lead)
        b1 = end + timedelta(minutes=cfg.cleanup_min)
        if b1 > from_dt and b0 < to_dt:
            out.append((b0, b1, r["id"]))
    return out
