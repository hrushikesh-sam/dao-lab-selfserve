"""B-105: the generic two-admin approval flow and the lost-key reset effect."""
import pytest

from service import approvals, challenges, keys, models
from tests.conftest import mk_user

CANARY = "AGE-SECRET-KEY-1" + "Q" * 58
CREATED = "2026-09-01T09:00:00Z"
AFTER = "2026-09-02T09:00:00Z"


def recipient(tag="q"):
    return "age1" + "q" * 57 + tag


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(models, "utcnow", lambda: CREATED)
    return CREATED


def user_id(conn, name):
    return conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]


def admin(conn, name="dev-admin"):
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def key_for(conn, username, tag="q"):
    uid = user_id(conn, username)
    kid = keys.enroll(conn, uid, recipient(tag))
    keys.activate(conn, kid, admin(conn))
    return kid


def snapshot(conn, user, setup, key_id, public_id, state="saved",
             delete_after=None, requested_by=None, hold_approval_id=None):
    conn.execute(
        """INSERT INTO snapshots (public_id, user_id, setup_id, key_id, state, delete_after,
                                  delete_requested_by, hold_approval_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (public_id, user, setup, key_id, state, delete_after, requested_by,
         hold_approval_id, CREATED, CREATED))


def approval_row(conn, aid):
    return conn.execute("SELECT * FROM admin_approvals WHERE id = ?", (aid,)).fetchone()


def test_request_checks_admin_action_and_target_charset(db, clock, users):
    kid = key_for(db, "alice")

    with pytest.raises(approvals.ApprovalServiceError) as not_admin:
        approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db, "alice"))
    assert (not_admin.value.code, not_admin.value.http_status) == ("forbidden", 403)

    for action, target in (("unknown_action", f"key:{kid}"),
                           ("lost_key_reset", f"user:{kid}"),
                           ("lost_key_reset", "AGE-SECRET-KEY-1" + "Q" * 58),
                           ("home_discard", "Setup:tb-01"),
                           ("home_discard", "x" * 129)):
        with pytest.raises(approvals.ApprovalServiceError) as bad:
            approvals.request(db, action, target, admin(db))
        assert bad.value.code == "validation_error"
    assert db.execute("SELECT COUNT(*) FROM admin_approvals").fetchone()[0] == 0

    with pytest.raises(approvals.ApprovalServiceError) as missing:
        approvals.request(db, "lost_key_reset", "key:99999", admin(db))
    assert missing.value.code == "not_found"

    pending = keys.enroll(db, user_id(db, "bob"), recipient("p"))
    with pytest.raises(approvals.ApprovalServiceError) as not_active:
        approvals.request(db, "lost_key_reset", f"key:{pending}", admin(db))
    assert not_active.value.code == "invalid_state"


def test_request_enforces_one_open_and_no_second_reset_for_a_user(db, clock, users):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    other = key_for(db, "bob", tag="p")
    first = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    assert approval_row(db, first)["state"] == "pending"

    with pytest.raises(approvals.ApprovalServiceError) as duplicate:
        approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db, "second-admin"))
    assert duplicate.value.code == "approval_open"
    assert approvals.request(db, "lost_key_reset", f"key:{other}", admin(db)) != first

    db.execute("UPDATE keys SET state = 'retired', updated_at = ? WHERE id = ?", (CREATED, kid))
    second_key = key_for(db, "alice", tag="z")
    with pytest.raises(approvals.ApprovalServiceError) as second_reset:
        approvals.request(db, "lost_key_reset", f"key:{second_key}", admin(db))
    assert second_reset.value.code == "approval_open"
    audit = db.execute("SELECT detail FROM audit_events WHERE action = 'approval_request' LIMIT 1").fetchone()
    detail = models.from_json(audit["detail"])
    assert detail["state"] == "pending" and detail["approval_id"] == first
    assert CANARY not in str(detail)


def test_lost_key_reset_owner_cannot_request_or_decide(db, clock, users):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    db.execute("UPDATE users SET role = 'admin' WHERE username = 'alice'")

    with pytest.raises(approvals.ApprovalServiceError) as owner_request:
        approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db, "alice"))
    assert owner_request.value.code == "forbidden"

    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    with pytest.raises(approvals.ApprovalServiceError) as owner_decision:
        approvals.decide(db, aid, admin(db, "alice"), True, CREATED)
    assert owner_decision.value.code == "forbidden"
    assert approval_row(db, aid)["state"] == "pending"


@pytest.mark.parametrize("finish", ["executed", "cancelled"])
def test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted(db, clock, users, finish):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    first = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    approvals.decide(db, first, admin(db, "second-admin"), True, CREATED)

    with pytest.raises(approvals.ApprovalServiceError) as second_open:
        approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db, "second-admin"))
    assert second_open.value.code == "approval_open"

    if finish == "executed":
        db.execute("UPDATE admin_approvals SET executed_at = ?, updated_at = ? WHERE id = ?",
                   (AFTER, AFTER, first))
    else:
        db.execute("UPDATE admin_approvals SET state = 'cancelled', updated_at = ? WHERE id = ?",
                   (AFTER, first))
    assert approvals.request(db, "lost_key_reset", f"key:{kid}",
                             admin(db, "second-admin")) != first


def test_request_accepts_the_other_three_actions(db, clock, users, tb01):
    aid = approvals.request(db, "home_discard", "setup:tb-01", admin(db))
    assert approval_row(db, aid)["state"] == "pending"
    aid = approvals.request(db, "tripwire_accept", "sdp-pf1-vf8", admin(db))
    assert approval_row(db, aid)["action"] == "tripwire_accept"
    aid = approvals.request(db, "tripwire_rebaseline", "sdp-pf1-vf8", admin(db))
    assert approval_row(db, aid)["action"] == "tripwire_rebaseline"


def test_decide_needs_a_different_admin_and_an_open_active_target(db, clock, users):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))

    with pytest.raises(approvals.ApprovalServiceError) as user_decision:
        approvals.decide(db, aid, admin(db, "alice"), True, CREATED)
    assert user_decision.value.code == "forbidden"
    with pytest.raises(approvals.ApprovalServiceError) as same_admin:
        approvals.decide(db, aid, admin(db), True, CREATED)
    assert same_admin.value.code == "invalid_state"

    db.execute("UPDATE keys SET state = 'revoked', updated_at = ? WHERE id = ?", (CREATED, kid))
    with pytest.raises(approvals.ApprovalServiceError) as inactive:
        approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)
    assert inactive.value.code == "invalid_state"
    assert approval_row(db, aid)["state"] == "pending"

    db.execute("UPDATE keys SET state = 'active', updated_at = ? WHERE id = ?", (CREATED, kid))
    approvals.decide(db, aid, admin(db, "second-admin"), False, CREATED)
    assert approval_row(db, aid)["state"] == "rejected"
    assert approval_row(db, aid)["execute_after"] is None
    with pytest.raises(approvals.ApprovalServiceError) as decided_twice:
        approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)
    assert decided_twice.value.code == "invalid_state"


def test_reject_reset_when_target_key_is_no_longer_active(db, clock, users):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    db.execute("UPDATE keys SET state = 'retired', updated_at = ? WHERE id = ?", (CREATED, kid))

    with pytest.raises(approvals.ApprovalServiceError) as inactive:
        approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)
    assert inactive.value.code == "invalid_state"

    approvals.decide(db, aid, admin(db, "second-admin"), False, CREATED)
    assert approval_row(db, aid)["state"] == "rejected"
    assert approval_row(db, aid)["execute_after"] is None


def test_reject_reset_when_requester_is_no_longer_an_admin(db, clock, users):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    db.execute("UPDATE users SET role = 'user' WHERE username = 'dev-admin'")

    with pytest.raises(approvals.ApprovalServiceError) as not_admin:
        approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)
    assert not_admin.value.code == "forbidden"

    approvals.decide(db, aid, admin(db, "second-admin"), False, CREATED)
    assert approval_row(db, aid)["state"] == "rejected"
    assert approval_row(db, aid)["execute_after"] is None


def test_approve_reset_holds_only_unheld_saved_snapshots(db, clock, users, tb01):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    owner = user_id(db, "alice")
    bob_key = key_for(db, "bob", tag="p")
    bob = user_id(db, "bob")
    snapshot(db, owner, tb01[0], kid, f"{1:032x}")
    snapshot(db, owner, tb01[0], kid, f"{2:032x}", "saved", AFTER, owner)
    snapshot(db, owner, tb01[0], kid, f"{3:032x}", "capturing")
    snapshot(db, owner, tb01[0], kid, f"{4:032x}", "failed")
    snapshot(db, bob, tb01[0], bob_key, f"{5:032x}")
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))

    approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)
    row = approval_row(db, aid)
    assert (row["state"], row["decided_by"], row["decided_at"]) == ("approved", user_id(db, "second-admin"), CREATED)
    assert row["execute_after"] == AFTER and row["executed_at"] is None
    held = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (f"{1:032x}",)).fetchone()
    assert (held["delete_after"], held["delete_requested_by"], held["hold_approval_id"]) == (
        AFTER, user_id(db, "dev-admin"), aid)
    own = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (f"{2:032x}",)).fetchone()
    assert (own["delete_after"], own["delete_requested_by"], own["hold_approval_id"]) == (
        AFTER, owner, None)
    for public_id in (f"{3:032x}", f"{4:032x}", f"{5:032x}"):
        other = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (public_id,)).fetchone()
        assert (other["delete_after"], other["hold_approval_id"]) == (None, None)
    assert db.execute("SELECT state FROM keys WHERE id = ?", (kid,)).fetchone()[0] == "active"
    audit = db.execute("SELECT detail FROM audit_events WHERE action = 'approval_decide'").fetchone()
    assert models.from_json(audit["detail"])["state"] == "approved"
    assert CANARY not in db_dump(db) and CANARY not in str(models.from_json(audit["detail"]))


def test_execute_after_and_hooks_follow_the_action(db, clock, users, tb01, monkeypatch):
    mk_user(db, "second-admin", role="admin")
    second = admin(db, "second-admin")
    seen = []

    def hook(conn, row, now):
        seen.append(dict(row))

    monkeypatch.setitem(approvals.EFFECTS, "home_discard", hook)
    discard = approvals.request(db, "home_discard", "setup:tb-01", admin(db))
    approvals.decide(db, discard, second, True, CREATED)
    assert approval_row(db, discard)["execute_after"] == AFTER
    assert seen[0]["execute_after"] == AFTER and seen[0]["action"] == "home_discard"

    tripwire = approvals.request(db, "tripwire_accept", "sdp-pf1-vf8", admin(db))
    approvals.decide(db, tripwire, second, True, CREATED)
    assert approval_row(db, tripwire)["execute_after"] is None

    rejected = approvals.request(db, "tripwire_rebaseline", "sdp-pf1-vf8", admin(db))
    approvals.decide(db, rejected, second, False, CREATED)
    assert approval_row(db, rejected)["execute_after"] is None
    assert len(seen) == 1


def test_reject_does_not_place_holds(db, clock, users, tb01):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    snapshot(db, user_id(db, "alice"), tb01[0], kid, f"{6:032x}")
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    approvals.decide(db, aid, admin(db, "second-admin"), False, CREATED)
    row = db.execute("SELECT * FROM snapshots").fetchone()
    assert (row["delete_after"], row["hold_approval_id"]) == (None, None)


def test_cancel_by_owner_clears_only_the_approvals_holds(db, clock, users, tb01):
    mk_user(db, "second-admin", role="admin")
    kid = key_for(db, "alice")
    owner = user_id(db, "alice")
    other = approvals.request(db, "home_discard", "setup:tb-01", admin(db))
    approvals.decide(db, other, admin(db, "second-admin"), True, CREATED)

    snapshot(db, owner, tb01[0], kid, f"{1:032x}")
    snapshot(db, owner, tb01[0], kid, f"{2:032x}")
    snapshot(db, owner, tb01[0], kid, f"{3:032x}", "saved", AFTER, owner)
    snapshot(db, owner, tb01[0], kid, f"{4:032x}", "saved", AFTER,
             user_id(db, "dev-admin"), other)
    aid = approvals.request(db, "lost_key_reset", f"key:{kid}", admin(db))
    approvals.decide(db, aid, admin(db, "second-admin"), True, CREATED)

    nonce = b"cancel-nonce-canary"
    target = f"approval:{aid}"
    cid = models.insert(db, "key_challenges", {
        "user_id": owner, "key_id": kid, "purpose": "cancel_delete", "target_ref": target,
        "nonce_sha256": challenges.digest(nonce, "cancel_delete", target, kid, owner, AFTER),
        "expires_at": AFTER, "created_at": CREATED, "updated_at": CREATED})

    approvals.cancel_by_owner(db, aid, admin(db, "alice"), CREATED, nonce, cid)

    row = approval_row(db, aid)
    assert row["state"] == "cancelled" and row["executed_at"] is None
    challenge = db.execute("SELECT * FROM key_challenges WHERE id = ?", (cid,)).fetchone()
    assert challenge["consumed_at"] == CREATED
    for public_id in (f"{1:032x}", f"{2:032x}"):
        held = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (public_id,)).fetchone()
        assert (held["delete_after"], held["delete_requested_by"], held["hold_approval_id"]) == (
            None, None, None)
    own = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (f"{3:032x}",)).fetchone()
    assert (own["delete_after"], own["delete_requested_by"], own["hold_approval_id"]) == (
        AFTER, owner, None)
    other_row = db.execute("SELECT * FROM snapshots WHERE public_id = ?", (f"{4:032x}",)).fetchone()
    assert (other_row["delete_after"], other_row["delete_requested_by"],
            other_row["hold_approval_id"]) == (AFTER, user_id(db, "dev-admin"), other)
    audit = db.execute("SELECT actor, detail FROM audit_events WHERE action = 'approval_cancel'").fetchone()
    assert audit["actor"] == "alice"
    detail = models.from_json(audit["detail"])
    assert (detail["approval_id"], detail["action"], detail["state"], detail["cleared"]) == (
        aid, "lost_key_reset", "cancelled", 2)
    assert nonce.decode("ascii") not in db_dump(db)


def db_dump(conn):
    return "\n".join(conn.iterdump())
