# Scheduler — pure placement logic

Part of the daolab implementation plan — see [README.md](README.md) for the doc map.

Sources: root `README.md` (Phase 2, design invariants) and contract §8 (scheduler rules, lead-time rule, state machine) + §12 (env vars). Siblings: [01-domain-model.md](01-domain-model.md) for the schema read here, [02-backend-api.md](02-backend-api.md) for the HTTP mapping of results, [04-lifecycle-engine.md](04-lifecycle-engine.md) for the engine's identical lead computation, [08-cli-config-ops.md](08-cli-config-ops.md) for env-var plumbing.

## Contract: one pure function over a DB handle

`service/scheduler.py` is pure placement. Inputs: the sqlite handle it is given, the reservation request, and `now`. **No other I/O** — no clock reads, no env reads, no network, no filesystem, and no writes (SELECTs only). The caller (`api.py`, transition 1 of the state machine: *(create)* → `queued`) inserts the `reservations` row from the returned placement in the same transaction (`BEGIN IMMEDIATE` — one API worker on sqlite/WAL, so place-then-insert is race-free). This keeps design invariant 2 true verbatim: the scheduler matches capabilities, never hostnames, and has no I/O other than the DB handle it is given → always unit-testable.

```python
# service/scheduler.py — stdlib only: sqlite3, json, dataclasses, datetime, typing.

def place(db: sqlite3.Connection, req: PlacementRequest, now: datetime,
          cfg: SchedConfig = SchedConfig()) -> Placed | NoSetup | NoFreeSlot | NoFit:
    """Pure placement: FCFS, capability match, hw_class lead time. Read-only.
    mode "asap" (default): slot-shift to the earliest fit.
    mode "at": exactly the requested window, never shifted -> NoFit if it is not free."""

def next_free_at(db: sqlite3.Connection, usecase: str, now: datetime,
                 cfg: SchedConfig = SchedConfig()) -> datetime | None:
    """Earliest grantable start for a 1-hour slot (GET /usecases `next_free_at`), or None."""
```

Callers:

- `POST /reservations` (and therefore `daolab reserve`) — `place()` then INSERT. In mode `asap` (the default) slot conflicts are **never** errors: the scheduler slot-shifts and the API returns the granted slot. In mode `at` a conflict is a `NoFit` result, which the API maps to `409 slot_conflict` (B-14; alternatives in B-13).
- `GET /usecases` — `next_free_at()` per catalog row fills `next_free_at` and distinguishes `reason: "no_free_slot"`.

Configuration is passed in, never read: `api.py` builds `SchedConfig` from `service/config.py`. Defaults below mirror `.env.example` exactly.

```python
@dataclass(frozen=True)
class SchedConfig:
    hw_class_lead_min: int = 15   # DAOLAB_HW_CLASS_LEAD_MIN — EBF reflash lead
    provision_lead_min: int = 5   # DAOLAB_PROVISION_LEAD_MIN — normal lead
    horizon_hours: int = 168      # DAOLAB_SCHED_HORIZON_HOURS — search horizon
```

Precondition (enforced by `api.py`, see [02-backend-api.md](02-backend-api.md)): the use case exists and is active (`yaml_path` set, or `'builtin'` for `diy`), `hours >= 1`, and for `diy` the named setup exists. The scheduler may `assert` these, never re-validate them. The one exception is `mode`: an unknown value raises `ValueError` rather than silently behaving like `asap`.

## Input and output data shapes

```python
@dataclass(frozen=True)
class PlacementRequest:
    usecase: str                    # natural name, e.g. "tls-proxy-nginx" or "diy"
    requested_start_at: datetime    # UTC-aware; the literal "now" already resolved by api.py
    hours: int                      # >= 1
    setup: str | None = None        # diy only: the explicitly named setup, e.g. "setup-01"
    mode: Literal["asap", "at"] = "asap"   # asap: shift to the earliest fit; at: exact window only

@dataclass(frozen=True)
class Placed:
    setup_id: int                   # -> reservations.setup_id
    setup_name: str
    start_at: datetime              # granted; -> reservations.start_at
    end_at: datetime                # start_at + hours; -> reservations.end_at
    shifted: bool                   # start_at != requested_start_at
    lead_min: int                   # informational; engine re-derives identically

@dataclass(frozen=True)
class NoSetup:                      # -> 409 unmet_capability (message MUST name `missing`)
    missing: list[str]              # smallest per-setup missing capability set, sorted
    closest_setup: str | None       # tie -> alphabetically first setup

@dataclass(frozen=True)
class NoFreeSlot:                   # -> catalog reason "no_free_slot"; POST mapping in 02-backend-api.md
    horizon_hours: int

@dataclass(frozen=True)
class NoFit:                        # mode "at" only -> 409 slot_conflict (B-14); alternatives added by B-13
    start_at: datetime              # the exact window that was asked for ...
    end_at: datetime                # ... and could not be granted
    reason: Literal["overlap", "in_the_past"]
```

Rows read (all times are UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ` TEXT, parsed to aware datetimes by `models.py` helpers):

| Table | Columns read | Used for |
|---|---|---|
| `usecases` | `id`, `name`, `requires`, `hw_class` | `requires` (JSON array) drives the candidate filter; `hw_class` drives lead time. |
| `setups` | `id`, `name`, `capabilities` | Candidate filter: `requires ⊆ capabilities` (JSON array, the sync-computed union). |
| `resources` | `setup_id`, `kind`, `hw_class` | The `kind = 'dpu'` member's current `hw_class` — single source of truth at placement time. |
| `reservations` | `id`, `setup_id`, `state`, `start_at`, `end_at`, `usecase_id` | The setup timeline. |

The one timeline query per candidate setup:

```sql
SELECT r.id, r.start_at, r.end_at, u.hw_class
FROM reservations r JOIN usecases u ON u.id = r.usecase_id
WHERE r.setup_id = ? AND r.state NOT IN ('released', 'failed')
ORDER BY r.start_at, r.id;
```

## Algorithm walkthrough

```mermaid
flowchart TD
    RQ["PlacementRequest + now"] --> DIY{"diy with named setup?"}
    DIY -- yes --> ONE["candidates = the named setup only"]
    DIY -- no --> FIL["candidates = setups with capabilities superset of usecase.requires"]
    FIL --> ANY{"any candidate?"}
    ANY -- no --> NS["NoSetup: smallest missing set, closest_setup"]
    ANY -- yes --> TL["per candidate: busy intervals + projected hw_class timeline"]
    ONE --> TL
    TL --> MD{"mode?"}
    MD -- at --> AT["one overlap test of exactly (start - lead, start + hours) per candidate;<br/>start < now -> NoFit in_the_past; none free -> NoFit overlap"]
    AT --> PICKAT["Placed (shifted=False): min by (lead_min, setup name)"]
    MD -- asap --> SW["sweep for earliest s with (s - lead, s + hours) free"]
    SW --> OK{"feasible within DAOLAB_SCHED_HORIZON_HOURS?"}
    OK -- "no, all candidates" --> NF["NoFreeSlot"]
    OK -- yes --> PICK["min by (start_at, lead_min, setup name)"]
    PICK --> PL["Placed: setup_id, start_at, end_at, shifted"]
```

### Step 1 — candidate setups by capability match

Candidates = setups where `set(usecase.requires) ⊆ set(setup.capabilities)`, iterated `ORDER BY name`. Capability strings only — never hostnames, never `resources.host`. For `diy`, the explicitly named setup is the sole candidate (its `requires` is `[]`, so the filter is moot). If no candidate exists, compute per setup `missing = sorted(set(requires) - set(capabilities))`, pick the setup minimizing `(len(missing), name)`, and return `NoSetup(missing, closest_setup)` — exactly the `unmet_capability` payload of the contract's error model.

### Step 2 — FCFS ordering

FCFS by `created_at` is structural: placement happens once, at creation, and every existing reservation not in `released`/`failed` is immutable input. A new request never displaces or re-packs earlier grants; it takes the earliest slot that remains. Ties in `start_at` order by `id` (creation order). Requests arriving later are placed later — the second of two overlapping requests is the one that shifts (Phase 2 checkpoint).

### Step 3 — busy intervals on the setup timeline

A setup is busy for `[start_at − lead, end_at)` of every reservation not in `released`/`failed` (states `queued`, `provisioning`, `ready`, `active`, `tearing_down`). Intervals are **half-open**: a window may begin exactly at a busy interval's `end_at`. Each existing reservation's `lead` is re-derived deterministically by threading hw_class through the timeline (leads are not stored — scheduler and engine re-derive with the same function).

### Step 4 — hw_class projection and lead time

The DPU keeps its last `hw_class` after teardown (invariant 5), so the class the DPU *will* have before a future slot is the last one any earlier reservation provisions:

```python
def lead_min(uc_hw_class: str | None, projected: str | None, cfg: SchedConfig, has_dpu: bool) -> int:
    if has_dpu and uc_hw_class is not None and uc_hw_class != projected:
        return cfg.hw_class_lead_min     # EBF reflash takes real time
    return cfg.provision_lead_min        # same class, null class, or no DPU member

def timeline(db, setup, cfg) -> Timeline:
    rows = db.execute(TIMELINE_SQL, (setup.id,)).fetchall()       # ORDER BY start_at, id
    cur = setup.dpu_hw_class          # resources.hw_class of the kind='dpu' member; None if no DPU
    busy, steps = [], []
    for r in rows:
        lead = lead_min(r.hw_class, cur, cfg, setup.has_dpu)
        busy.append((r.start_at - timedelta(minutes=lead), r.end_at))
        if setup.has_dpu and r.hw_class is not None:
            cur = r.hw_class          # null hw_class never changes the projection
        steps.append((r.start_at, cur))
    return Timeline(busy=busy, steps=steps, initial=setup.dpu_hw_class)
    # Timeline.class_at(t): class of the last step with start_at < t, else initial.
```

The lead is used twice, identically: the scheduler requires the setup free for `[start_at − lead, end_at]`, and the engine begins provisioning at `start_at − lead` (transition 2; `dpu_prepare` includes `ebf_set` iff the classes differ — see [04-lifecycle-engine.md](04-lifecycle-engine.md)). Slot-shift therefore automatically inserts the reflash gap between back-to-back reservations with differing `hw_class`. `diy` never declares an `hw_class` → always the short lead, never a reflash.

### Step 5 — slot-shift sweep

```python
def sweep(tl, uc_hw_class, base, hours, horizon_end, cfg, has_dpu) -> Slot | None:
    s = base                                            # max(requested_start_at, now)
    while s + timedelta(hours=hours) <= horizon_end:
        lead = lead_min(uc_hw_class, tl.class_at(s), cfg, has_dpu)
        w0, w1 = s - timedelta(minutes=lead), s + timedelta(hours=hours)
        hit = next((b for b in tl.busy if b[0] < w1 and b[1] > w0), None)   # busy sorted by start
        if hit is None:
            return Slot(start=s, lead_min=lead)
        just_after = hit[1] + timedelta(seconds=1)      # class_at is a step function
        s = hit[1] + timedelta(minutes=lead_min(uc_hw_class, tl.class_at(just_after), cfg, has_dpu))
    return None                                          # this candidate has no slot in horizon
```

Each iteration returns or advances `s` past at least one busy interval → guaranteed termination. `place()` runs the sweep per candidate, collects `(start, lead_min, name, id)` tuples, and returns `Placed` from the minimum (tie-break table below) or `NoFreeSlot` if every candidate fails within `now + DAOLAB_SCHED_HORIZON_HOURS`.

### Step 5b — exact window (mode `at`, B-10)

```python
def place_at(db, req, candidates, uc_hw_class, now, cfg) -> Placed | NoFit:
    start, end = req.requested_start_at, req.requested_start_at + timedelta(hours=req.hours)
    if start < now:
        return NoFit(start, end, "in_the_past")          # rejected, never clamped (clamping = shifting)
    free = []
    for s in candidates:
        tl = timeline(db, s.id, ...)
        lead = lead_min(uc_hw_class, tl.class_at(start), cfg, has_dpu)
        if first_overlap(tl, start - timedelta(minutes=lead), end) is None:   # the SAME test the sweep uses
            free.append((lead, s.name, s.id))
    if not free:
        return NoFit(start, end, "overlap")
    lead, name, sid = min(free)
    return Placed(sid, name, start, end, shifted=False, lead_min=lead)
```

`first_overlap` is the single half-open test (`b.start < w1 and b.end > w0`) shared with the sweep, so the two modes cannot disagree about what a conflict is. Consequence, pinned by a differential test: for any start at or after `now`, `at` is granted exactly when `asap` would grant that same start unshifted (identical `Placed`), and is a `NoFit` exactly when `asap` would have shifted. The lead window before the start counts, and so does a later booking's lead before *its* start, so `at` 12:00–14:00 right after a booking that ends 12:00 conflicts (first free start is 12:05, as in `asap`). `DAOLAB_SCHED_HORIZON_HOURS` bounds the `asap` search only; `at` has no sweep to bound, so it does not enforce the horizon (open policy question for B-14/quotas).

Build order for the implementer: 1. dataclasses + `SchedConfig`; 2. loader SQL (`usecases`, `setups` + `resources`, timeline query); 3. `lead_min`; 4. `timeline` / `class_at`; 5. `sweep`; 6. `place` + `next_free_at`; 7. `tests/test_scheduler.py`.

## Tie-breaking and determinism rules

| Rule | Statement |
|---|---|
| Candidate tie-break | Among candidates, pick min `(start_at, lead_min, setup_name)`. Earliest granted start wins; equal starts prefer the setup needing no EBF reflash (invariant 5: fewer reflashes); still equal → alphabetically first setup name. |
| `closest_setup` tie-break | Smallest per-setup missing set; tie → alphabetically first setup (contract error model, verbatim). |
| Iteration order | Setups `ORDER BY name`; reservations `ORDER BY start_at, id`. |
| Interval convention | Half-open `[start − lead, end)`: a slot may begin exactly at a predecessor's `end_at` (plus its own lead). |
| Candidate tie-break, mode `at` | All free candidates share the requested start, so pick min `(lead_min, setup_name)` — the `asap` order for equal starts. |
| Past starts | Mode `asap`: `requested_start_at < now` (including the literal `now`) clamps the sweep base to `now`; `shifted` still compares against the stored `requested_start_at`. Mode `at`: a start before `now` is rejected as `NoFit(reason="in_the_past")`, never clamped; `start == now` is allowed. |
| Time handling | UTC-aware datetimes only; ISO-8601 UTC strings sort lexicographically = chronologically, so SQL `ORDER BY start_at` is correct. |
| Purity | `now` and `cfg` are parameters; no randomness; no re-placement of existing rows. Same DB state + same request + same `now` → byte-identical result, replayable in tests. |

## Worked examples

All examples: `setup-01` (capabilities `[e810, octeon-cn106, sdp, wire]`, DPU member `dpu-01` with `resources.hw_class = sdp-pf1-vf8`), defaults `DAOLAB_PROVISION_LEAD_MIN=5`, `DAOLAB_HW_CLASS_LEAD_MIN=15`. `tls-proxy-nginx` requires `[octeon-cn106, e810, wire]`, `hw_class: sdp-pf1-vf8`.

**1 — simple grant.** Empty timeline; reserve `tls-proxy-nginx` at `2026-09-01T10:00:00Z` for 2 h. Use-case `hw_class` equals the DPU's → lead 5. Window `[09:55, 12:00)` is free.

```
setup-01 ────────────────────────────────────────────► t (UTC)
             09:55       10:00                12:00
             |· lead 5m ·|██████ r1 granted ██████|
hw_class: sdp-pf1-vf8 throughout (match → no reflash)
result: start_at=10:00:00Z end_at=12:00:00Z shifted=false
```

**2 — overlap shift (Phase 2 checkpoint).** r1 as above is `queued`. r2 requests the same use case, same start `10:00`, 2 h. r1's busy interval is `[09:55, 12:00)`. Sweep: `s=10:00` conflicts; class after `12:00` is `sdp-pf1-vf8` = r2's class → lead 5 → `s = 12:00 + 5m = 12:05`; window `[12:00, 14:05)` is free.

```
             09:55 10:00               12:00 12:05              14:05
setup-01     |·5m·|██████ r1 ████████████|
r2 request         ▲ 10:00 (requested)
r2 granted                               |·5m·|██████ r2 █████████|
                                         └ back-to-back, same hw_class → 5-min lead only
result: start_at=12:05:00Z end_at=14:05:00Z shifted=true
```

**3 — hw_class lead time.** r1 as in example 1. r2 is test-fixture use case `uc-b` (requires `[octeon-cn106]`, `hw_class: sdp-pf2-vf16`), requested `12:00`, 2 h. Projected class at `12:00` is `sdp-pf1-vf8` (left by r1) → differs → lead 15. Window `[11:45, 14:00)` overlaps r1 → shift to `s = 12:00 + 15m = 12:15`; window `[12:00, 14:15)` is free.

```
             09:55 10:00                12:00      12:15               14:15
setup-01     |·5m·|██ r1 (sdp-pf1-vf8) ██|         |██ r2 (sdp-pf2-vf16) ██|
r2 request                              ▲ 12:00
r2 granted                              |··15m····|
                                        └ EBF reflash gap = DAOLAB_HW_CLASS_LEAD_MIN
engine starts dpu_prepare (incl. ebf_set) at 12:00 = start_at − 15m
result: start_at=12:15:00Z end_at=14:15:00Z shifted=true
```

## Edge cases

- **No satisfying setup** → `NoSetup`; `api.py` returns `409 unmet_capability` whose message **names the missing capability list** (Phase 2 checkpoint), e.g. `requires [octeon-cn106, e810, wire, gpu]` against setup-01 → `missing: ["gpu"]`, `closest_setup: "setup-01"`.
- **Cancellation frees a slot.** `DELETE` of a `queued` reservation → `released` immediately; `released` is excluded from busy, so the slot is free for the *next* request at once. Cancelling `ready`/`active` goes through `tearing_down` (still busy) until `released`. Freed slots are never back-filled retroactively: earlier shifted grants stay where they were placed (FCFS grants are final).
- **Back-to-back same `hw_class`** → no reflash, only `DAOLAB_PROVISION_LEAD_MIN` (example 2). Likewise a use case with `hw_class` null/omitted runs on whatever class the DPU has — never triggers `ebf_set`, never takes the long lead, and never changes the projection.
- **N setups with partial capability overlap.** `requires [octeon-cn106, e810, wire]` with setup-01 `[e810, octeon-cn106, sdp, wire]` and a fixture `setup-02` `[gpu, octeon-cn106, sdp]`: only setup-01 is a candidate — partial overlap is not a match. If *both* satisfy `requires`, each is swept independently and the tie-break table decides; a request that would shift on setup-01 is granted unshifted on a free setup-02.
- **Setup without a DPU member** (pure `nic`/`compute`/`gpu`): `has_dpu` is false, hw_class logic is inert, lead is always `DAOLAB_PROVISION_LEAD_MIN`.
- **`failed` reservations** are excluded from the timeline (contract busy rule, verbatim). A failed *teardown* leaving the setup physically held is an operational condition surfaced via `held_by` and resolved by an admin — engine/ops concern ([04-lifecycle-engine.md](04-lifecycle-engine.md)), not a scheduler input.
- **Mode `at` on a busy window** → `NoFit(reason="overlap")` carrying the requested `start_at`/`end_at`; nothing is shifted and nothing is written. With several capable setups the window is tested on each, and the request is granted on a free one (tie-break above); `NoFit` only when every candidate conflicts. `diy` tests only its named setup.
- **Mode `at` with a start in the past** → `NoFit(reason="in_the_past")`; the API may reject such input earlier with a validation error.
- **Horizon exhaustion** → `NoFreeSlot(horizon_hours=168)`; surfaces as `reason: "no_free_slot"` / `next_free_at: null` in the catalog.
- **`reservation_id=0`** (manual Phase-1 runs) never exists as a DB row, so it never appears on any timeline.

## Complexity and practical limits

Let S = candidate setups, R = timeline rows per setup (reservations not in `released`/`failed` within the horizon).

- Timeline build: one indexed query per candidate (`idx_reservations_setup` on `(setup_id, start_at, end_at)`), O(R) rows, already ordered.
- Sweep: each shift consumes ≥ 1 busy interval → ≤ R shifts; linear conflict scan → O(R²) worst case per setup, O(S·R²) total, O(R) memory.
- Practical bound: `DAOLAB_SCHED_HORIZON_HOURS=168` and ≥ 1-hour slots cap R at ~168 per setup; with tens of setups the worst case is well under a millisecond in-process on sqlite — no caching, no precomputation, nothing shared between calls. Revisit only alongside the Phase-4 sqlite→Postgres move (>1 API worker), which is when place-then-insert would need `SELECT ... FOR UPDATE` semantics instead of sqlite's single-writer guarantee.

## Unit test plan

`tests/test_scheduler.py`, run by `make test` (`python -m pytest tests/ -q`) — no hardware, no API, no engine. Harness: `sqlite3.connect(":memory:")`, execute `service/schema.sql`, tiny fixture helpers (`mk_setup(name, caps, dpu_hw_class)`, `mk_usecase(name, requires, hw_class)`, `mk_res(setup, usecase, start, end, state)`) inserting rows directly; `now` is a frozen parameter — no clock mocking. Every test calls `place()`/`next_free_at()` with an explicit `SchedConfig(15, 5, 168)`-style config.

| # | Test | Fixture / action | Expected |
|---|---|---|---|
| 1 | `test_simple_grant_exact_slot` | Empty timeline; reserve `tls-proxy-nginx` 10:00 +2h | `Placed`, `start_at=10:00`, `end_at=12:00`, `shifted=False` |
| 2 | `test_overlap_shifts_second` **(Phase 2 checkpoint)** | r1 10:00–12:00 queued; identical second request | `start_at=12:05`, `shifted=True` — second call shifted to the next slot |
| 3 | `test_unmet_capability_names_missing` **(Phase 2 checkpoint)** | `requires` includes `gpu`; no setup offers it | `NoSetup`, `missing == ["gpu"]`, `closest_setup == "setup-01"` |
| 4 | `test_shift_lands_after_end_plus_lead` | As #2 | Granted window starts exactly at `end_at + provision_lead_min` |
| 5 | `test_hw_class_change_pads_lead` | r1 `sdp-pf1-vf8`; r2 use case `sdp-pf2-vf16` at r1's `end_at` | `start_at = end_at + 15m` (worked example 3) |
| 6 | `test_back_to_back_same_hw_class_short_lead` | r2 same `hw_class` as r1, requested at r1's `end_at` | `start_at = end_at + 5m`; `lead_min == 5` |
| 7 | `test_null_hw_class_never_reflashes` | Use case `hw_class=None`, DPU class arbitrary | `lead_min == 5`; projection unchanged for later requests |
| 8 | `test_projection_threads_through_queue` | r1 class A, r2 class B queued; r3 class B after r2 | r3 gets short lead (predecessor projects B, not the DB's A) |
| 9 | `test_released_frees_slot` | r1 10:00–12:00 `released`; request 10:00 | Granted 10:00 unshifted — cancellation freed the slot |
| 10 | `test_failed_excluded_from_timeline` | r1 in `failed` | Timeline ignores it; grant lands at the requested start |
| 11 | `test_all_busy_states_block` | r1 in each of `queued/provisioning/ready/active/tearing_down` | Every one conflicts and shifts the new request |
| 12 | `test_partial_overlap_not_candidate` | setup-02 `[gpu, octeon-cn106, sdp]`; `requires [octeon-cn106, e810, wire]` | Only setup-01 candidate; setup-02 never selected |
| 13 | `test_two_setups_earliest_start_wins` | setup-01 busy, satisfying setup-02 free | Granted on setup-02 at the requested start, `shifted=False` |
| 14 | `test_tie_prefers_no_reflash_then_name` | Two free setups, equal starts, one needs reflash | No-reflash setup wins; with equal leads the alphabetically first name wins |
| 15 | `test_closest_setup_smallest_missing_tie_alpha` | Two setups missing 2 vs 1 caps; then two missing 1 each | Smallest set wins; tie → alphabetically first `closest_setup` |
| 16 | `test_diy_uses_named_setup_only` | `PlacementRequest(usecase="diy", setup="setup-01")`, other setups free | Placed on `setup-01`; short lead (diy has no `hw_class`) |
| 17 | `test_no_free_slot_within_horizon` | Timeline saturated for 168 h | `NoFreeSlot(horizon_hours=168)` |
| 18 | `test_past_start_clamped_to_now` | `requested_start_at` = literal `now` resolved / in the past | Sweep base is `now`; `shifted` computed vs stored request |
| 19 | `test_hours_sets_end_at` | `hours=3` | `end_at == start_at + 3h` |
| 20 | `test_next_free_at_probe` | Busy until 12:00 (same class) | `next_free_at == 12:05`; `None` once the horizon is saturated |
| 21 | `test_determinism_replay` | Run #2 and #5 twice on cloned DBs | Byte-identical `Placed` results both times |
| 22 | `test_read_only` | Any `place()` call | `db.total_changes == 0` — the scheduler never writes |

B-10 adds the mode-`at` cases below (same file, same fixtures; `req(..., mode="at")`):

| # | Test | Fixture / action | Expected |
|---|---|---|---|
| B-10.1 | `test_mode_defaults_to_asap` | `PlacementRequest` without `mode` | `mode == "asap"` |
| B-10.2 | `test_at_free_window_granted_exactly` | Empty timeline; `at` 10:00 +2h | `Placed` 10:00–12:00, `shifted=False` |
| B-10.3 | `test_at_busy_window_is_conflict_but_asap_shifts` **(B-10 done-when)** | r1 10:00–12:00; `at` 11:00 +2h, then the same as `asap` | `NoFit(11:00, 13:00, "overlap")`; `asap` → 12:05, `shifted=True` |
| B-10.4 | `test_at_conflict_names_the_requested_window` | r1 10:00–12:00; `at` 10:30 +3h | `NoFit` with `start_at=10:30`, `end_at=13:30` |
| B-10.5 | `test_at_boundary_after_a_booking_uses_lead` | r1 ends 12:00; `at` 12:04 / 12:05 / 12:06 | 12:04 `NoFit`; 12:05 and 12:06 `Placed` |
| B-10.6 | `test_at_hw_class_change_needs_reflash_lead` | r1 ends 12:00; other `hw_class`; `at` 12:14 / 12:15 | 12:14 `NoFit`; 12:15 `Placed`, `lead_min=15` |
| B-10.7 | `test_at_window_ending_into_a_later_booking` | r1 12:00–14:00; `at` window ending 11:55 / 11:56 | ending 11:55 `Placed`; 11:56 `NoFit` (r1's 5 min lead) |
| B-10.8 | `test_at_all_busy_states_block` | r1 in each busy state | `NoFit` every time |
| B-10.9 | `test_at_released_and_failed_free_the_window` | r1 `released` / `failed` | `Placed` |
| B-10.10 | `test_at_start_in_the_past_is_rejected_not_clamped` | `at` 08:00 with `now` 09:00; then `at` == `now` | `NoFit("in_the_past")`; `start == now` → `Placed` |
| B-10.11 | `test_at_picks_the_free_setup` / `test_at_all_capable_setups_busy_is_conflict` | setup-01 busy, setup-02 free; then both busy | granted on setup-02 unshifted; then `NoFit` |
| B-10.12 | `test_at_tie_prefers_no_reflash_then_name` / `test_at_tie_equal_leads_alpha_name_wins` | Two free setups | no-reflash setup wins; equal leads → alphabetical |
| B-10.13 | `test_at_unmet_capability_is_still_nosetup` | `requires` includes `gpu` | `NoSetup`, `missing == ["gpu"]` |
| B-10.14 | `test_at_diy_uses_named_setup_only` | diy, named setup busy, other free | `NoFit` (no fallback); named free → `Placed` |
| B-10.15 | `test_at_is_pure_and_read_only` | Same `at` request twice | equal results; `db.total_changes` unchanged |
| B-10.16 | `test_unknown_mode_is_an_error` | `mode="whenever"` | `ValueError` |
| B-10.17 | `test_at_and_asap_agree_on_random_timelines` | 400 seeded random timelines, 2 setups, 3 use cases | `at == asap` whenever `asap` is unshifted; `NoFit("overlap")` whenever `asap` shifted |

Tests 2 and 3 are the two Phase 2 checkpoint scenarios from the root README, verbatim: two overlapping reserve calls → the second is shifted to the next slot; reserving a use case whose `requires` no setup meets → rejected naming the missing capability.
