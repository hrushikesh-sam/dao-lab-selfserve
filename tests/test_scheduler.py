"""The 22 scheduler cases from plan/03-scheduler.md — pure placement, no hardware."""
import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from service import models, scheduler
from service.scheduler import NoFit, NoFreeSlot, NoSetup, Placed, PlacementRequest, SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

# slot_min=1: the grid is off (whole minutes), so these tests pin exact lead/cleanup arithmetic.
# The 30-minute grid has its own tests at the end (B-12).
CFG = SchedConfig(hw_class_lead_min=15, provision_lead_min=5, horizon_hours=168, cleanup_min=10,
                  slot_min=1)
GRID = dataclasses.replace(CFG, slot_min=30)
# B-11: two bookings are `cleanup + lead` apart. Same hw_class: 10 + 5 = 15 min; reflash: 10 + 15 = 25.
GAP_SAME = CFG.cleanup_min + CFG.provision_lead_min
GAP_DIFF = CFG.cleanup_min + CFG.hw_class_lead_min
CAPS = ["e810", "octeon-cn106", "sdp", "wire"]
REQ = ["octeon-cn106", "e810", "wire"]


def std(db, dpu_hw="sdp-pf1-vf8"):
    sid = mk_setup(db, "setup-01", CAPS, dpu_hw_class=dpu_hw)
    uid = mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    return sid, uid


def bare(p):
    """A NoFit without its alternatives, for tests that only care about the window and reason."""
    return dataclasses.replace(p, alternatives=()) if isinstance(p, NoFit) else p


def req(usecase="tls-proxy-nginx", start=T(10), hours=2, setup=None, mode="asap"):
    return PlacementRequest(usecase=usecase, requested_start_at=start, hours=hours,
                            setup=setup, mode=mode)


def test_simple_grant_exact_slot(db):
    std(db)
    p = scheduler.place(db, req(), NOW, CFG)
    assert isinstance(p, Placed)
    assert p.start_at == T(10) and p.end_at == T(12) and p.shifted is False


def test_overlap_shifts_second(db):          # Phase 2 checkpoint
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(), NOW, CFG)
    assert isinstance(p, Placed)
    assert p.start_at == T(12, 15) and p.shifted is True      # 12:00 end + 10 cleanup + 5 lead


def test_unmet_capability_names_missing(db):  # Phase 2 checkpoint
    std(db)
    mk_usecase(db, "uc-gpu", REQ + ["gpu"])
    p = scheduler.place(db, req(usecase="uc-gpu"), NOW, CFG)
    assert isinstance(p, NoSetup)
    assert p.missing == ["gpu"] and p.closest_setup == "setup-01"


def test_shift_lands_after_end_plus_lead(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.start_at == T(12) + timedelta(minutes=CFG.cleanup_min + CFG.provision_lead_min)


def test_hw_class_change_pads_lead(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    p = scheduler.place(db, req(usecase="uc-b", start=T(12)), NOW, CFG)
    assert p.start_at == T(12, 25) and p.lead_min == 15 and p.shifted is True   # 10 cleanup + 15 reflash


def test_back_to_back_same_hw_class_short_lead(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(12)), NOW, CFG)
    assert p.start_at == T(12, 15) and p.lead_min == 5


def test_null_hw_class_never_reflashes(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    nid = mk_usecase(db, "uc-null", ["octeon-cn106"], hw_class=None)
    p = scheduler.place(db, req(usecase="uc-null", start=T(12)), NOW, CFG)
    assert p.lead_min == 5
    mk_res(db, sid, nid, p.start_at, p.end_at)     # projection must stay sdp-pf1-vf8
    p2 = scheduler.place(db, req(start=p.end_at), NOW, CFG)
    assert p2.lead_min == 5


def test_projection_threads_through_queue(db):
    sid, uid = std(db)                                        # DB dpu class = sdp-pf1-vf8 (A)
    ub = mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    mk_res(db, sid, uid, T(10), T(12))                        # r1 class A
    mk_res(db, sid, ub, T(12, 15), T(14, 15))                 # r2 class B
    p = scheduler.place(db, req(usecase="uc-b", start=T(14, 15), hours=1), NOW, CFG)
    assert p.lead_min == 5                                    # predecessor projects B, not the DB's A
    assert p.start_at == T(14, 30)             # r2 ends 14:15 + 10 cleanup + 5 lead


def test_released_frees_slot(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12), state="released")
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.start_at == T(10) and p.shifted is False


def test_failed_excluded_from_timeline(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12), state="failed")
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.start_at == T(10) and p.shifted is False


@pytest.mark.parametrize("state", ["queued", "provisioning", "ready", "active", "cleaning",
                                   "quarantined"])
def test_all_busy_states_block(db, state):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12), state=state)
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.shifted is True and p.start_at == T(12, 15)


def test_partial_overlap_not_candidate(db):
    sid, uid = std(db)
    mk_setup(db, "setup-02", ["gpu", "octeon-cn106", "sdp"], dpu_hw_class="sdp-pf1-vf8")
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.setup_name == "setup-01" and p.shifted is True   # setup-02 never selected


def test_two_setups_earliest_start_wins(db):
    sid, uid = std(db)
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.setup_name == "setup-02" and p.start_at == T(10) and p.shifted is False


def test_tie_prefers_no_reflash_then_name(db):
    mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf2-vf16")   # needs reflash -> lead 15
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")    # match -> lead 5
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.setup_name == "setup-02" and p.lead_min == 5


def test_tie_equal_leads_alpha_name_wins(db):
    mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    p = scheduler.place(db, req(), NOW, CFG)
    assert p.setup_name == "setup-01"


def test_closest_setup_smallest_missing_tie_alpha(db):
    mk_usecase(db, "uc-x", ["cap-one", "cap-two"])
    mk_setup(db, "setup-b", ["cap-one"])                     # missing 1
    mk_setup(db, "setup-a", [])                              # missing 2
    p = scheduler.place(db, req(usecase="uc-x"), NOW, CFG)
    assert isinstance(p, NoSetup)
    assert p.closest_setup == "setup-b" and p.missing == ["cap-two"]
    mk_setup(db, "setup-c", ["cap-two"])                     # also missing 1 -> tie, alpha first
    p2 = scheduler.place(db, req(usecase="uc-x"), NOW, CFG)
    assert p2.closest_setup == "setup-b"


def test_diy_uses_named_setup_only(db):
    std(db)
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    db.execute("UPDATE usecases SET yaml_path='builtin' WHERE name='diy'")
    mk_usecase(db, "diy", [], hw_class=None, yaml_path="builtin")
    p = scheduler.place(db, req(usecase="diy", setup="setup-01"), NOW, CFG)
    assert p.setup_name == "setup-01" and p.lead_min == 5


def test_no_free_slot_within_horizon(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, NOW, NOW + timedelta(hours=180))
    p = scheduler.place(db, req(start=NOW, hours=1), NOW, CFG)
    assert isinstance(p, NoFreeSlot) and p.horizon_hours == 168


def test_past_start_clamped_to_now(db):
    std(db)
    p = scheduler.place(db, req(start=T(8)), NOW, CFG)        # requested before now=09:00
    assert p.start_at == NOW and p.shifted is True


def test_hours_sets_end_at(db):
    std(db)
    p = scheduler.place(db, req(hours=3), NOW, CFG)
    assert p.end_at == p.start_at + timedelta(hours=3)


def test_next_free_at_probe(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    nfa = scheduler.next_free_at(db, "tls-proxy-nginx", NOW, CFG)
    assert nfa == T(12, 15)
    mk_res(db, sid, uid, NOW, NOW + timedelta(hours=180))
    assert scheduler.next_free_at(db, "tls-proxy-nginx", NOW, CFG) is None


def test_determinism_replay(db):
    import sqlite3
    from service import models
    results = []
    for _ in range(2):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        models.init_db(conn)
        models.insert(conn, "users", {"username": "u1", "token_hash": "h", "ssh_pubkey": "k"})
        sid, uid = std(conn)
        mk_res(conn, sid, uid, T(10), T(12))
        results.append(scheduler.place(conn, req(), NOW, CFG))
        conn.close()
    assert results[0] == results[1]


def test_read_only(db):
    std(db)
    before = db.total_changes
    scheduler.place(db, req(), NOW, CFG)
    assert db.total_changes == before      # the scheduler never writes


# ---- B-18: setup availability gates provisioning, not placement ------------

def test_placement_still_books_a_quarantined_setup(db):
    sid, _ = std(db)
    models.set_setup_availability(db, sid, "quarantined")
    p = scheduler.place(db, req(), NOW, CFG)
    assert isinstance(p, Placed) and p.setup_id == sid and p.shifted is False


def test_setup_available_predicate(db):
    sid, _ = std(db)
    assert scheduler.setup_available(db, sid) is True
    for state in ("quarantined", "maintenance"):
        models.set_setup_availability(db, sid, state)
        assert scheduler.setup_available(db, sid) is False
    models.set_setup_availability(db, sid, "available")
    assert scheduler.setup_available(db, sid) is True


def test_queued_stays_queued_while_quarantined(db):
    sid, uid = std(db)
    rid = mk_res(db, sid, uid, T(10), T(12), state="queued")
    assert [r["id"] for r in scheduler.startable_queued(db)] == [rid]
    models.set_setup_availability(db, sid, "quarantined")
    assert scheduler.startable_queued(db) == []          # engine finds nothing to start
    models.set_setup_availability(db, sid, "available")
    assert [r["id"] for r in scheduler.startable_queued(db)] == [rid]


def test_availability_rejects_unknown_value(db):
    sid, _ = std(db)
    with pytest.raises(sqlite3.IntegrityError):
        models.set_setup_availability(db, sid, "bogus")


# ---- B-10: mode "at" = one overlap test, never shifted; "asap" = earliest fit ---------

def test_mode_defaults_to_asap():
    r = PlacementRequest(usecase="x", requested_start_at=T(10), hours=1)
    assert r.mode == "asap"


def test_at_free_window_granted_exactly(db):
    std(db)
    p = scheduler.place(db, req(mode="at"), NOW, CFG)
    assert isinstance(p, Placed)
    assert (p.start_at, p.end_at, p.shifted) == (T(10), T(12), False)


def test_at_busy_window_is_conflict_but_asap_shifts(db):      # B-10 done-when
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    at = scheduler.place(db, req(start=T(11), mode="at"), NOW, CFG)
    assert bare(at) == NoFit(start_at=T(11), end_at=T(13), reason="overlap")   # never shifted
    asap = scheduler.place(db, req(start=T(11), mode="asap"), NOW, CFG)
    assert isinstance(asap, Placed) and asap.start_at == T(12, 15) and asap.shifted is True


def test_at_conflict_names_the_requested_window(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(10, 30), hours=3, mode="at"), NOW, CFG)
    assert isinstance(p, NoFit) and (p.start_at, p.end_at) == (T(10, 30), T(13, 30))


@pytest.mark.parametrize("minute,free", [(14, False), (15, True), (16, True)])
def test_at_boundary_after_a_booking_uses_lead(db, minute, free):
    # prior booking ends 12:00, same hw_class -> 10 cleanup + 5 lead: 12:15 is the first free start
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(12, minute), hours=1, mode="at"), NOW, CFG)
    assert isinstance(p, Placed) is free
    if not free:
        assert p.reason == "overlap"


@pytest.mark.parametrize("minute,free", [(24, False), (25, True)])
def test_at_hw_class_change_needs_reflash_lead(db, minute, free):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    p = scheduler.place(db, req(usecase="uc-b", start=T(12, minute), hours=1, mode="at"),
                        NOW, CFG)
    assert isinstance(p, Placed) is free
    if free:
        assert p.lead_min == 15


@pytest.mark.parametrize("end_minute,free", [(45, True), (46, False)])
def test_at_window_ending_into_a_later_booking(db, end_minute, free):
    # a later booking at 12:00 holds its 5 min lead (11:55-12:00), and the new window needs its
    # own 10 min cleanup before that: it must END by 11:45 (B-11, the mirror of the gap after)
    sid, uid = std(db)
    mk_res(db, sid, uid, T(12), T(14))
    start, hours = T(9, 45), 2
    p = scheduler.place(db, req(start=start + timedelta(minutes=end_minute - 45), hours=hours,
                                mode="at"), NOW, CFG)
    assert isinstance(p, Placed) is free


@pytest.mark.parametrize("state", ["queued", "provisioning", "ready", "active", "cleaning",
                                   "quarantined"])
def test_at_all_busy_states_block(db, state):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12), state=state)
    assert isinstance(scheduler.place(db, req(mode="at"), NOW, CFG), NoFit)


@pytest.mark.parametrize("state", ["released", "failed"])
def test_at_released_and_failed_free_the_window(db, state):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12), state=state)
    assert isinstance(scheduler.place(db, req(mode="at"), NOW, CFG), Placed)


def test_at_start_in_the_past_is_rejected_not_clamped(db):
    std(db)
    p = scheduler.place(db, req(start=T(8), hours=1, mode="at"), NOW, CFG)   # NOW is 09:00
    assert bare(p) == NoFit(start_at=T(8), end_at=T(9), reason="in_the_past")
    ok = scheduler.place(db, req(start=NOW, hours=1, mode="at"), NOW, CFG)   # start == now is fine
    assert isinstance(ok, Placed) and ok.start_at == NOW and ok.shifted is False


def test_at_picks_the_free_setup(db):
    sid, uid = std(db)
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_res(db, sid, uid, T(10), T(12))                       # setup-01 busy, setup-02 free
    p = scheduler.place(db, req(mode="at"), NOW, CFG)
    assert isinstance(p, Placed) and p.setup_name == "setup-02" and p.shifted is False


def test_at_all_capable_setups_busy_is_conflict(db):
    sid, uid = std(db)
    sid2 = mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_res(db, sid, uid, T(10), T(12))
    mk_res(db, sid2, uid, T(9), T(11))
    assert isinstance(scheduler.place(db, req(start=T(10, 30), mode="at"), NOW, CFG), NoFit)


def test_at_tie_prefers_no_reflash_then_name(db):
    mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf2-vf16")   # reflash -> lead 15
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")    # match   -> lead 5
    p = scheduler.place(db, req(mode="at"), NOW, CFG)
    assert p.setup_name == "setup-02" and p.lead_min == 5


def test_at_tie_equal_leads_alpha_name_wins(db):
    mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf1-vf8")
    assert scheduler.place(db, req(mode="at"), NOW, CFG).setup_name == "setup-01"


def test_at_unmet_capability_is_still_nosetup(db):
    std(db)
    mk_usecase(db, "uc-gpu", REQ + ["gpu"])
    p = scheduler.place(db, req(usecase="uc-gpu", mode="at"), NOW, CFG)
    assert isinstance(p, NoSetup) and p.missing == ["gpu"]


def test_at_diy_uses_named_setup_only(db):
    sid, uid = std(db)
    sid2 = mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_usecase(db, "diy", [], hw_class=None, yaml_path="builtin")
    mk_res(db, sid, uid, T(10), T(12))                       # the named setup is busy ...
    p = scheduler.place(db, req(usecase="diy", setup="setup-01", mode="at"), NOW, CFG)
    assert isinstance(p, NoFit)                              # ... setup-02 is not a fallback
    ok = scheduler.place(db, req(usecase="diy", setup="setup-02", mode="at"), NOW, CFG)
    assert isinstance(ok, Placed) and ok.setup_id == sid2


def test_at_is_pure_and_read_only(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    before = db.total_changes
    first = scheduler.place(db, req(start=T(11), mode="at"), NOW, CFG)
    second = scheduler.place(db, req(start=T(11), mode="at"), NOW, CFG)
    assert first == second and db.total_changes == before


def test_unknown_mode_is_an_error(db):
    std(db)
    with pytest.raises(ValueError, match="unknown placement mode"):
        scheduler.place(db, req(mode="whenever"), NOW, CFG)


@pytest.mark.parametrize("cfg", [CFG, GRID], ids=["grid-off", "grid-30"])
def test_at_and_asap_agree_on_random_timelines(db, cfg):
    """Differential test: `at` is granted exactly when `asap` would grant the same start
    unshifted (identical Placed), and is a conflict exactly when `asap` had to shift."""
    import random
    rng = random.Random(20261015)
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-a")
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="class-b")
    ucs = [mk_usecase(db, "uc-a", REQ, hw_class="class-a"),
           mk_usecase(db, "uc-b", REQ, hw_class="class-b"),
           mk_usecase(db, "uc-n", REQ, hw_class=None)]
    names = ["uc-a", "uc-b", "uc-n"]
    setups = [r["id"] for r in db.execute("SELECT id FROM setups ORDER BY name")]
    states = ["queued", "provisioning", "ready", "active", "cleaning", "quarantined",
              "released", "failed"]
    granted = conflicts = 0
    for _ in range(400):
        db.execute("DELETE FROM reservations")
        for _ in range(rng.randint(3, 10)):                     # dense: ~half the requests collide
            start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 12))
            end = start + timedelta(hours=rng.randint(1, 3))
            mk_res(db, rng.choice(setups), rng.choice(ucs), start, end, state=rng.choice(states))
        step = 5 if cfg.slot_min == 1 else cfg.slot_min          # `at` asks for on-grid starts
        r_start = NOW + timedelta(minutes=step * rng.randint(0, 12 * 12 * 5 // step))
        r = dict(usecase=rng.choice(names), start=r_start, hours=rng.randint(1, 3))
        asap = scheduler.place(db, req(mode="asap", **r), NOW, cfg)
        at = scheduler.place(db, req(mode="at", **r), NOW, cfg)
        assert isinstance(asap, Placed)                         # horizon is a week; never full
        if asap.shifted:
            assert bare(at) == NoFit(start_at=r_start, end_at=r_start + timedelta(hours=r["hours"]),
                               reason="overlap")
            conflicts += 1
        else:
            assert at == asap
            granted += 1
    assert granted > 50 and conflicts > 50                      # the test exercises both outcomes


# ---- B-11: turnaround buffer ------------------------------------------------
# Every reservation blocks [start - lead, end + cleanup_min). Back-to-back bookings are
# `cleanup + lead` apart (plan/09 §5): cleanup first, then provision / reflash.

def test_gap_constants_match_the_documented_numbers():
    assert (GAP_SAME, GAP_DIFF) == (15, 25)


def test_cleanup_min_is_configurable(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    for cleanup, first_start in [(0, T(12, 5)), (10, T(12, 15)), (30, T(12, 35))]:
        cfg = dataclasses.replace(CFG, cleanup_min=cleanup)
        assert scheduler.place(db, req(), NOW, cfg).start_at == first_start


def test_cleanup_zero_restores_the_pre_b11_gap(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    cfg = dataclasses.replace(CFG, cleanup_min=0)
    assert scheduler.place(db, req(start=T(12, 4), hours=1, mode="at"), NOW, cfg).reason == "overlap"
    assert isinstance(scheduler.place(db, req(start=T(12, 5), hours=1, mode="at"), NOW, cfg), Placed)


def test_setup_without_dpu_still_gets_cleanup(db):
    sid = mk_setup(db, "plain", ["nic"], has_dpu=False)
    uid = mk_usecase(db, "uc-plain", ["nic"], hw_class="whatever")
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(usecase="uc-plain", start=T(12)), NOW, CFG)
    assert p.start_at == T(12, 15) and p.lead_min == 5          # no reflash lead, cleanup still applies


def test_cleanup_blocks_every_live_state_until_released(db):
    sid, uid = std(db)
    rid = mk_res(db, sid, uid, T(10), T(12), state="cleaning")
    assert scheduler.place(db, req(start=T(12)), NOW, CFG).start_at == T(12, 15)
    db.execute("UPDATE reservations SET state = 'released' WHERE id = ?", (rid,))
    assert scheduler.place(db, req(start=T(12)), NOW, CFG).start_at == T(12)   # buffer freed too


def test_new_booking_leaves_room_for_its_own_cleanup(db):
    # the mirror case: a later booking at 12:00 (lead 5 -> busy from 11:55). A 2h booking
    # asked for 09:50 would end 11:50, but its own cleanup runs to 12:00 -> not free.
    sid, uid = std(db)
    mk_res(db, sid, uid, T(12), T(14))
    at = scheduler.place(db, req(start=T(9, 50), mode="at"), NOW, CFG)
    assert bare(at) == NoFit(start_at=T(9, 50), end_at=T(11, 50), reason="overlap")
    asap = scheduler.place(db, req(start=T(9, 50), mode="asap"), NOW, CFG)
    assert asap.start_at == T(14, 15)                            # ... and 2h can't fit before it
    short = scheduler.place(db, req(start=T(9, 50), hours=1, mode="at"), NOW, CFG)
    assert isinstance(short, Placed) and short.end_at == T(10, 50)


def test_later_reflash_booking_needs_the_longer_gap_before_it(db):
    # a later booking of another hw_class at 12:00 holds a 15 min reflash lead (busy from 11:45);
    # our window must end + 10 cleanup by then -> end <= 11:35
    sid, uid = std(db)
    ub = mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    mk_res(db, sid, ub, T(12), T(14))
    ok = scheduler.place(db, req(start=T(9, 35), mode="at"), NOW, CFG)      # ends 11:35
    assert isinstance(ok, Placed)
    no = scheduler.place(db, req(start=T(9, 36), mode="at"), NOW, CFG)      # ends 11:36
    assert isinstance(no, NoFit)


def test_turnaround_only_adds_time_after_never_before_the_start(db):
    # a free setup is still granted at exactly the requested start (the cleanup buffer is not a lead)
    sid, uid = std(db)
    p = scheduler.place(db, req(start=T(10)), NOW, CFG)
    assert p.start_at == T(10) and p.shifted is False


def _gap_property(db, cfg):
    """Property test, independent of scheduler.py's own arithmetic: place many random
    requests (both modes), insert each grant, and after every insert re-check the documented
    rule on the whole timeline: no overlap, and consecutive bookings on a setup are at least
    `cleanup + lead` apart, with the lead derived from hw_class by the rule in plan/03. With
    a grid, every grant must also start on a multiple of `slot_min` after UTC midnight."""
    import random
    rng = random.Random(20261016)
    classes = {"uc-a": "class-a", "uc-b": "class-b", "uc-n": None}
    ids = {name: mk_usecase(db, name, REQ, hw_class=cls) for name, cls in classes.items()}
    dpu_class = {"setup-01": "class-a", "setup-02": "class-b"}
    sids = {n: mk_setup(db, n, CAPS, dpu_hw_class=c) for n, c in dpu_class.items()}
    shifted = conflicts = placed = 0
    for _ in range(40):
        db.execute("DELETE FROM reservations")
        for _ in range(25):
            name = rng.choice(list(classes))
            mode = rng.choice(["asap", "at"])
            step = 5 if mode == "asap" else cfg.slot_min           # `at` asks for on-grid starts
            start = NOW + timedelta(minutes=step * rng.randint(0, 12 * 8 * 5 // step))
            r = req(usecase=name, start=start, hours=rng.randint(1, 3), mode=mode)
            p = scheduler.place(db, r, NOW, cfg)
            if isinstance(p, NoFit):
                assert p.reason == "overlap"                       # starts were on-grid and in the future
                conflicts += 1
                continue
            if isinstance(p, NoFreeSlot):
                continue
            placed += 1
            shifted += p.shifted
            assert ((p.start_at - T(0)).total_seconds() / 60) % cfg.slot_min == 0   # grid-aligned
            mk_res(db, p.setup_id, ids[name], p.start_at, p.end_at)
            for sname, sid in sids.items():
                rows = db.execute(
                    """SELECT r.start_at, r.end_at, u.hw_class FROM reservations r
                       JOIN usecases u ON u.id = r.usecase_id
                       WHERE r.setup_id = ? ORDER BY r.start_at""", (sid,)).fetchall()
                cur, prev_end = dpu_class[sname], None
                for row in rows:
                    s0, e0 = scheduler._parse(row["start_at"]), scheduler._parse(row["end_at"])
                    reflash = row["hw_class"] is not None and row["hw_class"] != cur
                    lead = cfg.hw_class_lead_min if reflash else cfg.provision_lead_min
                    if prev_end is not None:
                        assert s0 - prev_end >= timedelta(minutes=cfg.cleanup_min + lead), \
                            (sname, row["start_at"], prev_end, lead)
                    prev_end = e0
                    if row["hw_class"] is not None:
                        cur = row["hw_class"]
    assert placed > 300 and shifted > 50 and conflicts > 20     # all three outcomes are exercised


def test_placements_always_keep_the_turnaround_gap(db):
    _gap_property(db, CFG)


def test_placements_keep_the_gap_and_the_grid(db):               # B-12: same property, 30 min grid
    _gap_property(db, GRID)


def _two_class_setup(db):
    """DPU currently flashed class-b; use cases uc-a (class-a), uc-b (class-b), uc-n (null)."""
    sid = mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-b")
    ids = {n: mk_usecase(db, n, REQ, hw_class=c)
           for n, c in [("uc-a", "class-a"), ("uc-b", "class-b"), ("uc-n", None)]}
    return sid, ids


def test_inserting_before_a_booking_can_lengthen_its_lead(db):
    # X (class-b, 12:00-13:00) follows an idle class-b DPU -> 5 min lead, busy from 11:55.
    # A class-a booking placed before it makes X a reflash: its lead becomes 15 (busy from
    # 11:45), so our window + 10 cleanup must end by 11:45, not 11:55.
    sid, ids = _two_class_setup(db)
    mk_res(db, sid, ids["uc-b"], T(12), T(13))
    late = scheduler.place(db, req(usecase="uc-a", start=T(9, 45), mode="at"), NOW, CFG)  # ends 11:45
    assert bare(late) == NoFit(start_at=T(9, 45), end_at=T(11, 45), reason="overlap")
    ok = scheduler.place(db, req(usecase="uc-a", start=T(9, 35), mode="at"), NOW, CFG)    # ends 11:35
    assert isinstance(ok, Placed)
    same_class = scheduler.place(db, req(usecase="uc-b", start=T(9, 45), mode="at"), NOW, CFG)
    assert isinstance(same_class, Placed)                    # class-b before class-b: no reflash
    asap = scheduler.place(db, req(usecase="uc-a", start=T(9, 45)), NOW, CFG)
    assert asap.start_at == T(13, 25) and asap.lead_min == 15   # after X: 13:00 + 10 + 15 reflash


def test_lead_change_propagates_through_bookings_without_hw_class(db):
    # X (null class, 12:00-13:00) passes the projection through, so a class-a booking before
    # it also changes the lead of Z (class-b, 13:20): 5 -> 15. Z needs busy-from 13:05, but
    # X's cleanup runs to 13:10 -> conflict, even though our window clears X itself.
    sid, ids = _two_class_setup(db)
    mk_res(db, sid, ids["uc-n"], T(12), T(13))
    mk_res(db, sid, ids["uc-b"], T(13, 20), T(14, 20))
    p = scheduler.place(db, req(usecase="uc-a", start=T(9, 45), mode="at"), NOW, CFG)
    assert bare(p) == NoFit(start_at=T(9, 45), end_at=T(11, 45), reason="overlap")
    no_change = scheduler.place(db, req(usecase="uc-n", start=T(9, 45), mode="at"), NOW, CFG)
    assert isinstance(no_change, Placed)                     # a null class never changes the projection


# ---- B-12: slot grid ----------------------------------------------------------
# A granted start is a multiple of `slot_min` minutes after UTC midnight (30 -> :00 / :30).
# `asap` rounds candidate starts up; `at` never shifts, so an off-grid `at` start is refused.

def test_slot_min_defaults_to_30():
    assert SchedConfig().slot_min == 30


@pytest.mark.parametrize("slot,t,expected", [
    (30, T(10, 0), T(10, 0)), (30, T(10, 1), T(10, 30)), (30, T(10, 30), T(10, 30)),
    (30, T(10, 31), T(11, 0)), (30, T(23, 45), T(0, 0, day=2)),
    (45, T(9, 0), T(9, 0)), (45, T(9, 10), T(9, 45)), (45, T(23, 20), T(0, 0, day=2)),
    (60, T(10, 1), T(11, 0)), (1, T(10, 7), T(10, 7)),
])
def test_snap_up_rounds_to_the_next_grid_point(slot, t, expected):
    assert scheduler.snap_up(t, slot) == expected


def test_snap_up_rounds_seconds_up_and_converts_to_utc():
    from datetime import datetime, timezone
    assert scheduler.snap_up(datetime(2026, 9, 1, 10, 30, 1, tzinfo=timezone.utc), 30) == T(11, 0)
    ist = timezone(timedelta(hours=5, minutes=30))
    assert scheduler.snap_up(datetime(2026, 9, 1, 15, 31, tzinfo=ist), 30) == T(10, 30)   # 10:01 UTC


def test_asap_start_snaps_up_to_the_grid(db):
    std(db)
    for asked, granted, shifted in [(T(10, 0), T(10, 0), False), (T(10, 10), T(10, 30), True),
                                    (T(10, 30), T(10, 30), False), (T(10, 31), T(11, 0), True)]:
        p = scheduler.place(db, req(start=asked), NOW, GRID)
        assert (p.start_at, p.shifted) == (granted, shifted), asked


def test_deploy_now_waits_for_the_next_grid_point(db):
    std(db)
    now = NOW + timedelta(minutes=7)                                   # 09:07
    p = scheduler.place(db, req(start=now), now, GRID)
    assert p.start_at == T(9, 30) and p.shifted is True
    assert scheduler.place(db, req(start=now), now, CFG).start_at == now      # grid off: immediate


def test_shift_after_a_booking_lands_on_the_grid(db):
    sid, uid = std(db)
    mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    mk_res(db, sid, uid, T(10), T(12))
    assert scheduler.place(db, req(start=T(12)), NOW, GRID).start_at == T(12, 30)   # raw 12:15
    assert scheduler.place(db, req(usecase="uc-b", start=T(12)), NOW, GRID).start_at == T(12, 30)  # raw 12:25
    bigger = dataclasses.replace(GRID, cleanup_min=30)
    assert scheduler.place(db, req(start=T(12)), NOW, bigger).start_at == T(13, 0)  # raw 12:35


def test_next_free_at_is_on_the_grid(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    assert scheduler.next_free_at(db, "tls-proxy-nginx", NOW, GRID) == T(12, 30)


def test_at_off_grid_start_is_refused_even_when_free(db):
    std(db)
    p = scheduler.place(db, req(start=T(10, 10), mode="at"), NOW, GRID)
    assert bare(p) == NoFit(start_at=T(10, 10), end_at=T(12, 10), reason="off_grid")
    ok = scheduler.place(db, req(start=T(10, 30), mode="at"), NOW, GRID)
    assert isinstance(ok, Placed) and ok.start_at == T(10, 30) and ok.shifted is False


def test_at_reason_precedence_past_then_grid_then_overlap(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    assert scheduler.place(db, req(start=T(8, 10), mode="at"), NOW, GRID).reason == "in_the_past"
    assert scheduler.place(db, req(start=T(10, 10), mode="at"), NOW, GRID).reason == "off_grid"
    assert scheduler.place(db, req(start=T(11, 0), mode="at"), NOW, GRID).reason == "overlap"


def test_at_on_the_grid_after_a_booking_respects_the_gap(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    assert scheduler.place(db, req(start=T(12, 0), hours=1, mode="at"), NOW, GRID).reason == "overlap"
    assert isinstance(scheduler.place(db, req(start=T(12, 30), hours=1, mode="at"), NOW, GRID), Placed)


def test_slot_min_below_one_is_an_error(db):
    std(db)
    with pytest.raises(ValueError, match="slot_min"):
        scheduler.place(db, req(), NOW, dataclasses.replace(CFG, slot_min=0))


@pytest.mark.parametrize("slot", [5, 30])
def test_sweep_finds_the_earliest_grid_start(db, slot):
    """Oracle: walk every grid point from the base and take the first one `_conflict` clears.
    The sweep's jumps must never skip a free grid start (nor return a busy one)."""
    import random
    rng = random.Random(20261017 + slot)
    cfg = dataclasses.replace(CFG, slot_min=slot)
    sid = mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-a")
    names = {"uc-a": "class-a", "uc-b": "class-b", "uc-n": None}
    ids = {n: mk_usecase(db, n, REQ, hw_class=c) for n, c in names.items()}
    has_dpu, dpu_class = scheduler._setup_dpu(db, sid)
    horizon_end = NOW + timedelta(hours=cfg.horizon_hours)
    for _ in range(150):
        db.execute("DELETE FROM reservations")
        for _ in range(rng.randint(2, 9)):
            start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 10))
            mk_res(db, sid, rng.choice(list(ids.values())), start,
                   start + timedelta(hours=rng.randint(1, 3)), state=rng.choice(["queued", "active"]))
        name, hours = rng.choice(list(names)), rng.randint(1, 3)
        asked = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 10))
        got = scheduler.place(db, req(usecase=name, start=asked, hours=hours), NOW, cfg)
        tl = scheduler._timeline(db, sid, dpu_class, has_dpu, cfg)
        s, expected = scheduler.snap_up(max(asked, NOW), slot), None
        while s + timedelta(hours=hours) <= horizon_end:
            lead = scheduler.lead_min(names[name], tl.class_at(s), cfg, has_dpu)
            if scheduler._conflict(tl, s, hours, lead, names[name], cfg, has_dpu) is None:
                expected = s
                break
            s += timedelta(minutes=slot)
        assert isinstance(got, Placed) and got.start_at == expected, (asked, name, hours)


def test_same_class_neighbour_wins_a_tie_the_grid_creates(db):             # B-12 done-when
    # request: uc-a, 2h, asked for 10:00 (busy on both setups).
    #   setup-01: previous booking is class-b -> reflash: raw earliest 11:55 + 10 + 15 = 12:20
    #   setup-02: previous booking is class-a -> same class: raw earliest 12:10 + 10 + 5 = 12:25
    # Grid off: setup-01 starts 5 min earlier and wins. Grid on: both snap to 12:30 -> a tie,
    # and the same-class neighbour (setup-02, no reflash) wins although setup-01 sorts first.
    s1 = mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-a")
    s2 = mk_setup(db, "setup-02", CAPS, dpu_hw_class="class-a")
    uc_a = mk_usecase(db, "uc-a", REQ, hw_class="class-a")
    uc_b = mk_usecase(db, "uc-b", REQ, hw_class="class-b")
    mk_res(db, s1, uc_b, T(9, 55), T(11, 55))
    mk_res(db, s2, uc_a, T(10, 10), T(12, 10))
    off = scheduler.place(db, req(usecase="uc-a", start=T(10)), NOW, CFG)
    assert (off.setup_name, off.start_at, off.lead_min) == ("setup-01", T(12, 20), 15)
    on = scheduler.place(db, req(usecase="uc-a", start=T(10)), NOW, GRID)
    assert (on.setup_name, on.start_at, on.lead_min) == ("setup-02", T(12, 30), 5)


def test_tie_preference_never_beats_an_earlier_start(db):                  # FCFS is not broken
    # same layout, but setup-02's previous booking ends later: its snapped start is 13:00,
    # so the reflash setup-01 (12:30) still wins - the same-class preference only breaks ties.
    s1 = mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-a")
    s2 = mk_setup(db, "setup-02", CAPS, dpu_hw_class="class-a")
    uc_a = mk_usecase(db, "uc-a", REQ, hw_class="class-a")
    uc_b = mk_usecase(db, "uc-b", REQ, hw_class="class-b")
    mk_res(db, s1, uc_b, T(9, 55), T(11, 55))
    mk_res(db, s2, uc_a, T(10, 40), T(12, 40))                             # raw 12:55 -> grid 13:00
    p = scheduler.place(db, req(usecase="uc-a", start=T(10)), NOW, GRID)
    assert (p.setup_name, p.start_at, p.lead_min) == ("setup-01", T(12, 30), 15)


# ---- B-13: NoFit alternatives ---------------------------------------------------
# A NoFit carries the next NUM_ALTERNATIVES (3) free starts for the same duration, ascending,
# searched forward from max(requested start, now). Each is free on its own (they may overlap
# each other); the first is what `asap` would have granted.

def test_nofit_alternatives_default_to_empty():
    assert NoFit(start_at=T(10), end_at=T(12), reason="overlap").alternatives == ()
    assert scheduler.NUM_ALTERNATIVES == 3


def test_alternatives_match_the_design_doc_example(db):          # plan/09 s4 sequence diagram
    sid, uid = std(db)
    mk_res(db, sid, uid, T(9), T(11))                           # alice 09:00, 2 h
    p = scheduler.place(db, req(start=T(10), mode="at"), NOW, GRID)    # bob asks for 10:00
    assert isinstance(p, NoFit) and p.reason == "overlap"
    assert p.alternatives == (T(11, 30), T(12, 0), T(12, 30))   # 11:00 + 10 cleanup + 5 lead -> 11:30 grid


def test_alternatives_skip_gaps_that_are_too_small(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    mk_res(db, sid, uid, T(14), T(15))
    # 1 h: 12:30 fits before the 14:00 booking (its lead starts 13:55); 13:00 would not
    one = scheduler.place(db, req(start=T(11), hours=1, mode="at"), NOW, GRID)
    assert one.alternatives == (T(12, 30), T(15, 30), T(16, 0))
    # 2 h: nothing fits before 14:00, so all three come after the second booking
    two = scheduler.place(db, req(start=T(11), hours=2, mode="at"), NOW, GRID)
    assert two.alternatives == (T(15, 30), T(16, 0), T(16, 30))


@pytest.mark.parametrize("cfg", [dataclasses.replace(CFG, slot_min=5), GRID], ids=["grid-5", "grid-30"])
def test_alternatives_on_random_timelines(db, cfg):             # B-13 done-when
    """For every NoFit: exactly 3 alternatives, strictly ascending, on the grid, at or after
    max(requested, now), each one really bookable (`at` there is Placed, window of the same
    length), the first equal to the asap grant, and no free grid start skipped in between."""
    import random
    rng = random.Random(20261018)
    mk_setup(db, "setup-01", CAPS, dpu_hw_class="class-a")
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="class-b")
    ucs = [mk_usecase(db, n, REQ, hw_class=c)
           for n, c in [("uc-a", "class-a"), ("uc-b", "class-b"), ("uc-n", None)]]
    names = ["uc-a", "uc-b", "uc-n"]
    setups = [r["id"] for r in db.execute("SELECT id FROM setups ORDER BY name")]
    step = cfg.slot_min
    nofits = 0
    for _ in range(200):
        db.execute("DELETE FROM reservations")
        for _ in range(rng.randint(5, 14)):
            start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 12))
            mk_res(db, rng.choice(setups), rng.choice(ucs), start,
                   start + timedelta(hours=rng.randint(1, 3)),
                   state=rng.choice(["queued", "active", "released"]))
        name, hours = rng.choice(names), rng.randint(1, 3)
        asked = NOW + timedelta(minutes=step * rng.randint(0, 12 * 12 * 5 // step))
        at = scheduler.place(db, req(usecase=name, start=asked, hours=hours, mode="at"), NOW, cfg)
        if isinstance(at, Placed):
            continue
        nofits += 1
        alts = at.alternatives
        assert len(alts) == 3 and list(alts) == sorted(set(alts)), alts
        assert alts[0] == scheduler.place(db, req(usecase=name, start=asked, hours=hours), NOW, cfg).start_at
        for a in alts:
            assert a >= max(asked, NOW) and scheduler.snap_up(a, cfg.slot_min) == a
            ok = scheduler.place(db, req(usecase=name, start=a, hours=hours, mode="at"), NOW, cfg)
            assert isinstance(ok, Placed) and ok.end_at == a + timedelta(hours=hours)
        t, free_between = max(asked, NOW), []                    # brute force: every free grid start
        t = scheduler.snap_up(t, cfg.slot_min)
        while t <= alts[-1]:
            if isinstance(scheduler.place(db, req(usecase=name, start=t, hours=hours, mode="at"),
                                          NOW, cfg), Placed):
                free_between.append(t)
            t += timedelta(minutes=cfg.slot_min)
        assert tuple(free_between) == alts, (asked, name, hours)
    assert nofits > 60                                           # the test exercises real conflicts


def test_alternatives_for_a_past_start_begin_at_now(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(9), T(10))
    p = scheduler.place(db, req(start=T(8), hours=1, mode="at"), NOW, GRID)
    assert p.reason == "in_the_past" and p.alternatives == (T(10, 30), T(11, 0), T(11, 30))


def test_alternatives_for_a_past_start_are_never_in_the_past(db):
    std(db)                                                      # empty timeline: 08:00 would be "free"
    p = scheduler.place(db, req(start=T(8), hours=1, mode="at"), NOW, GRID)
    assert p.reason == "in_the_past"
    assert p.alternatives == (T(9, 0), T(9, 30), T(10, 0)) and all(a >= NOW for a in p.alternatives)


def test_alternatives_for_an_off_grid_start_begin_at_the_next_grid_point(db):
    std(db)
    p = scheduler.place(db, req(start=T(10, 10), hours=1, mode="at"), NOW, GRID)
    assert p.reason == "off_grid" and p.alternatives == (T(10, 30), T(11, 0), T(11, 30))


def test_alternatives_never_go_before_the_requested_start(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(14), T(16))                           # free all morning
    p = scheduler.place(db, req(start=T(14, 30), hours=1, mode="at"), NOW, GRID)
    assert p.alternatives[0] > T(14, 30) and all(a >= T(14, 30) for a in p.alternatives)
    assert T(10) not in p.alternatives


def test_alternatives_are_cut_short_by_the_horizon(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(9), T(10))
    short = dataclasses.replace(GRID, horizon_hours=3)           # now .. 12:00
    p = scheduler.place(db, req(start=T(9, 30), hours=1, mode="at"), NOW, short)
    assert p.reason == "overlap"
    assert p.alternatives == (T(10, 30), T(11, 0))               # 11:30 + 1 h would end after 12:00
    mk_res(db, sid, uid, T(10), T(12))                           # now nothing is free inside the horizon
    assert scheduler.place(db, req(start=T(10), hours=1, mode="at"), NOW, short).alternatives == ()


def test_alternatives_use_every_capable_setup_without_duplicates(db):
    s1 = mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf1-vf8")
    s2 = mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")
    uid = mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    mk_res(db, s1, uid, T(10), T(14))
    mk_res(db, s2, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(11), hours=1, mode="at"), NOW, GRID)
    assert p.reason == "overlap"
    assert p.alternatives == (T(12, 30), T(13, 0), T(13, 30))    # setup-02 frees first; one entry per start


def test_diy_alternatives_come_from_the_named_setup_only(db):
    s1 = mk_setup(db, "setup-01", CAPS, dpu_hw_class="sdp-pf1-vf8")
    mk_setup(db, "setup-02", CAPS, dpu_hw_class="sdp-pf1-vf8")      # free, but not the named one
    mk_usecase(db, "diy", [], hw_class=None)
    uid = db.execute("SELECT id FROM usecases WHERE name = 'diy'").fetchone()["id"]
    mk_res(db, s1, uid, T(10), T(12))
    p = scheduler.place(db, req(usecase="diy", setup="setup-01", start=T(11), hours=1, mode="at"),
                        NOW, GRID)
    assert p.reason == "overlap" and p.alternatives == (T(12, 30), T(13, 0), T(13, 30))


def test_alternatives_are_deterministic_and_read_only(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    before = db.total_changes
    a = scheduler.place(db, req(start=T(11), mode="at"), NOW, GRID)
    b = scheduler.place(db, req(start=T(11), mode="at"), NOW, GRID)
    assert a == b and a.alternatives and db.total_changes == before
