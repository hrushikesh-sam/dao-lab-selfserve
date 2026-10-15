"""The 22 scheduler cases from plan/03-scheduler.md — pure placement, no hardware."""
import sqlite3
from datetime import timedelta

import pytest

from service import models, scheduler
from service.scheduler import NoFit, NoFreeSlot, NoSetup, Placed, PlacementRequest, SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

CFG = SchedConfig(hw_class_lead_min=15, provision_lead_min=5, horizon_hours=168)
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
    assert p.start_at == T(12, 5) and p.shifted is True


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
    assert p.start_at == T(12) + timedelta(minutes=CFG.provision_lead_min)


def test_hw_class_change_pads_lead(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    p = scheduler.place(db, req(usecase="uc-b", start=T(12)), NOW, CFG)
    assert p.start_at == T(12, 15) and p.lead_min == 15 and p.shifted is True


def test_back_to_back_same_hw_class_short_lead(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(12)), NOW, CFG)
    assert p.start_at == T(12, 5) and p.lead_min == 5


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
    assert p.start_at == T(14, 20)


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
    assert p.shifted is True and p.start_at == T(12, 5)


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
    assert nfa == T(12, 5)
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
    assert isinstance(asap, Placed) and asap.start_at == T(12, 5) and asap.shifted is True


def test_at_conflict_names_the_requested_window(db):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(10, 30), hours=3, mode="at"), NOW, CFG)
    assert isinstance(p, NoFit) and (p.start_at, p.end_at) == (T(10, 30), T(13, 30))


@pytest.mark.parametrize("minute,free", [(4, False), (5, True), (6, True)])
def test_at_boundary_after_a_booking_uses_lead(db, minute, free):
    # prior booking ends 12:00, same hw_class -> 5 min lead: 12:05 is the first free start
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    p = scheduler.place(db, req(start=T(12, minute), hours=1, mode="at"), NOW, CFG)
    assert isinstance(p, Placed) is free
    if not free:
        assert p.reason == "overlap"


@pytest.mark.parametrize("minute,free", [(14, False), (15, True)])
def test_at_hw_class_change_needs_reflash_lead(db, minute, free):
    sid, uid = std(db)
    mk_res(db, sid, uid, T(10), T(12))
    mk_usecase(db, "uc-b", ["octeon-cn106"], hw_class="sdp-pf2-vf16")
    p = scheduler.place(db, req(usecase="uc-b", start=T(12, minute), hours=1, mode="at"),
                        NOW, CFG)
    assert isinstance(p, Placed) is free
    if free:
        assert p.lead_min == 15


@pytest.mark.parametrize("end_minute,free", [(55, True), (56, False)])
def test_at_window_ending_into_a_later_booking(db, end_minute, free):
    # a later booking at 12:00 holds its 5 min lead (11:55-12:00) too
    sid, uid = std(db)
    mk_res(db, sid, uid, T(12), T(14))
    start, hours = T(9, 55), 2
    p = scheduler.place(db, req(start=start + timedelta(minutes=end_minute - 55), hours=hours,
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
