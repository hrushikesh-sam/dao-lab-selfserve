"""The 22 scheduler cases from plan/03-scheduler.md — pure placement, no hardware."""
import sqlite3
from datetime import timedelta

import pytest

from service import models, scheduler
from service.scheduler import NoFit, NoFreeSlot, NoSetup, Placed, PlacementRequest, SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

CFG = SchedConfig(hw_class_lead_min=15, provision_lead_min=5, horizon_hours=168, cleanup_min=10)
# B-11: two bookings are `cleanup + lead` apart. Same hw_class: 10 + 5 = 15 min; reflash: 10 + 15 = 25.
GAP_SAME = CFG.cleanup_min + CFG.provision_lead_min
GAP_DIFF = CFG.cleanup_min + CFG.hw_class_lead_min
CAPS = ["e810", "octeon-cn106", "sdp", "wire"]
REQ = ["octeon-cn106", "e810", "wire"]


def std(db, dpu_hw="sdp-pf1-vf8"):
    sid = mk_setup(db, "setup-01", CAPS, dpu_hw_class=dpu_hw)
    uid = mk_usecase(db, "tls-proxy-nginx", REQ, hw_class="sdp-pf1-vf8")
    return sid, uid


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


@pytest.mark.parametrize("state", ["queued", "provisioning", "ready", "active", "tearing_down"])
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
    assert at == NoFit(start_at=T(11), end_at=T(13), reason="overlap")   # never shifted
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


@pytest.mark.parametrize("state", ["queued", "provisioning", "ready", "active", "tearing_down"])
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
    assert p == NoFit(start_at=T(8), end_at=T(9), reason="in_the_past")
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


def test_at_and_asap_agree_on_random_timelines(db):
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
    states = ["queued", "provisioning", "ready", "active", "tearing_down", "released", "failed"]
    granted = conflicts = 0
    for _ in range(400):
        db.execute("DELETE FROM reservations")
        for _ in range(rng.randint(3, 10)):                     # dense: ~half the requests collide
            start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 12))
            end = start + timedelta(hours=rng.randint(1, 3))
            mk_res(db, rng.choice(setups), rng.choice(ucs), start, end, state=rng.choice(states))
        r_start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 12))
        r = dict(usecase=rng.choice(names), start=r_start, hours=rng.randint(1, 3))
        asap = scheduler.place(db, req(mode="asap", **r), NOW, CFG)
        at = scheduler.place(db, req(mode="at", **r), NOW, CFG)
        assert isinstance(asap, Placed)                         # horizon is a week; never full
        if asap.shifted:
            assert at == NoFit(start_at=r_start, end_at=r_start + timedelta(hours=r["hours"]),
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
        cfg = SchedConfig(15, 5, 168, cleanup_min=cleanup)
        assert scheduler.place(db, req(), NOW, cfg).start_at == first_start


def test_cleanup_zero_restores_the_pre_b11_gap(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    cfg = SchedConfig(15, 5, 168, cleanup_min=0)
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
    rid = mk_res(db, sid, uid, T(10), T(12), state="tearing_down")
    assert scheduler.place(db, req(start=T(12)), NOW, CFG).start_at == T(12, 15)
    db.execute("UPDATE reservations SET state = 'released' WHERE id = ?", (rid,))
    assert scheduler.place(db, req(start=T(12)), NOW, CFG).start_at == T(12)   # buffer freed too


def test_new_booking_leaves_room_for_its_own_cleanup(db):
    # the mirror case: a later booking at 12:00 (lead 5 -> busy from 11:55). A 2h booking
    # asked for 09:50 would end 11:50, but its own cleanup runs to 12:00 -> not free.
    sid, uid = std(db)
    mk_res(db, sid, uid, T(12), T(14))
    at = scheduler.place(db, req(start=T(9, 50), mode="at"), NOW, CFG)
    assert at == NoFit(start_at=T(9, 50), end_at=T(11, 50), reason="overlap")
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


def test_placements_always_keep_the_turnaround_gap(db):
    """Property test, independent of scheduler.py's own arithmetic: place many random
    requests (both modes), insert each grant, and after every insert re-check the documented
    rule on the whole timeline: no overlap, and consecutive bookings on a setup are at least
    `cleanup + lead` apart, with the lead derived from hw_class by the rule in plan/03."""
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
            start = NOW + timedelta(minutes=5 * rng.randint(0, 12 * 8))
            r = req(usecase=name, start=start, hours=rng.randint(1, 3),
                    mode=rng.choice(["asap", "at"]))
            p = scheduler.place(db, r, NOW, CFG)
            if isinstance(p, NoFit):
                conflicts += 1
                continue
            if isinstance(p, NoFreeSlot):
                continue
            placed += 1
            shifted += p.shifted
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
                    lead = CFG.hw_class_lead_min if reflash else CFG.provision_lead_min
                    if prev_end is not None:
                        assert s0 - prev_end >= timedelta(minutes=CFG.cleanup_min + lead), \
                            (sname, row["start_at"], prev_end, lead)
                    prev_end = e0
                    if row["hw_class"] is not None:
                        cur = row["hw_class"]
    assert placed > 300 and shifted > 50 and conflicts > 20     # all three outcomes are exercised


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
    assert late == NoFit(start_at=T(9, 45), end_at=T(11, 45), reason="overlap")
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
    assert p == NoFit(start_at=T(9, 45), end_at=T(11, 45), reason="overlap")
    no_change = scheduler.place(db, req(usecase="uc-n", start=T(9, 45), mode="at"), NOW, CFG)
    assert isinstance(no_change, Placed)                     # a null class never changes the projection
