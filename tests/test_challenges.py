"""B-106: single-use challenges and the owner's approval cancel."""
import logging
from datetime import datetime, timezone

import pytest

from service import agecli, approvals, challenges, keys, models
from tests.conftest import mk_user

CREATED = "2026-09-01T09:00:00Z"
SOON = "2026-09-01T09:01:00Z"
EXPIRES = "2026-09-01T09:10:00Z"
LATE = "2026-09-01T09:10:01Z"
AFTER = "2026-09-02T09:00:00Z"
NONCE = b"NONCE-CANARY-" + bytes(range(19))
ARMOR = (b"-----BEGIN AGE ENCRYPTED FILE-----\nZmFrZQ==\n"
         b"-----END AGE ENCRYPTED FILE-----\n")
VECTOR = "717e764a117bc38ae7aa6769b1ade5fddc292762152f56b460b16c4e50afc6cd"


def recipient(tag="q"):
    return "age1" + "q" * 57 + tag


def user_row(conn, name):
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def user_id(conn, name):
    return user_row(conn, name)["id"]


def admin_row(conn, name="dev-admin"):
    return user_row(conn, name)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(models, "utcnow", lambda: CREATED)
    return CREATED


@pytest.fixture
def sealed(monkeypatch):
    calls = []
    issued = [0]

    def token(size):
        index = issued[0]
        issued[0] += 1
        return NONCE if index == 0 else NONCE[:31] + bytes([index])

    def encrypt(to, data):
        calls.append((to, data))
        return b"fake"

    monkeypatch.setattr(agecli, "encrypt_to", encrypt)
    monkeypatch.setattr(challenges.secrets, "token_bytes", token)
    return calls


def active_key(conn, name="alice", tag="q"):
    kid = keys.enroll(conn, user_id(conn, name), recipient(tag))
    keys.activate(conn, kid, admin_row(conn))
    return kid


def pending_key(conn, name="alice", tag="p", fingerprint=None):
    uid = user_id(conn, name)
    return models.insert(conn, "keys", {
        "user_id": uid, "recipient": recipient(tag),
        "fingerprint": fingerprint or keys.fingerprint(recipient(tag)),
        "state": "pending", "created_at": CREATED, "updated_at": CREATED})


def snapshot(conn, owner, setup, key_id, public_id, held=None, requested_by=None,
             approval_id=None, state="saved"):
    return models.insert(conn, "snapshots", {
        "public_id": public_id, "user_id": owner, "setup_id": setup, "key_id": key_id,
        "state": state, "delete_after": AFTER if held is not None else None,
        "delete_requested_by": requested_by, "hold_approval_id": approval_id,
        "created_at": CREATED, "updated_at": CREATED})


def reservation(conn, owner, setup, usecase, state):
    return models.insert(conn, "reservations", {
        "user_id": owner, "usecase_id": usecase, "setup_id": setup, "state": state,
        "start_at": CREATED, "end_at": AFTER, "requested_start_at": CREATED,
        "created_at": CREATED, "updated_at": CREATED})


def approved_reset(conn, key_id, second="second-admin"):
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (second,)).fetchone() is None:
        mk_user(conn, second, role="admin")
    aid = approvals.request(conn, "lost_key_reset", f"key:{key_id}", admin_row(conn))
    approvals.decide(conn, aid, admin_row(conn, second), True, CREATED)
    return aid


def issue(conn, name, purpose, target, now=CREATED):
    return challenges.issue(conn, user_row(conn, name), purpose, target, now)


def begin(conn):
    conn.execute("BEGIN IMMEDIATE")


def end(conn):
    conn.execute("COMMIT")


def rollback(conn):
    conn.execute("ROLLBACK")


def consume(conn, challenge_id, name, purpose, target, nonce=NONCE, now=SOON):
    begin(conn)
    try:
        challenges.consume(conn, challenge_id, user_row(conn, name), purpose, target, nonce, now)
    except BaseException:
        rollback(conn)
        raise
    end(conn)


def dump(conn):
    return "\n".join(conn.iterdump())


def test_hash_vector_is_fixed_and_length_delimited():
    digest = challenges.digest(bytes(32), "rotate", "a" * 64, 7, 11, EXPIRES)
    assert digest == VECTOR
    assert challenges.digest(NONCE, "rotate", "a" * 64, 7, 11, EXPIRES) != digest
    assert challenges.digest(bytes(32), "delete", "a" * 64, 7, 11, EXPIRES) != digest
    assert challenges.digest(bytes(32), "rotate", "b" * 64, 7, 11, EXPIRES) != digest
    assert challenges.digest(bytes(32), "rotate", "a" * 64, 8, 11, EXPIRES) != digest
    assert challenges.digest(bytes(32), "rotate", "a" * 64, 7, 12, EXPIRES) != digest
    assert challenges.digest(bytes(32), "rotate", "a" * 64, 7, 11, AFTER) != digest


def test_issue_seals_a_hash_and_returns_the_ciphertext_once(db, users, clock, sealed, caplog):
    caplog.set_level(logging.DEBUG)
    kid = active_key(db)
    target = pending_key(db, fingerprint="a" * 64)
    result = issue(db, "alice", "rotate", "a" * 64)
    assert set(result) == {"challenge_id", "expires_at", "ciphertext"}
    assert result["expires_at"] == EXPIRES and result["ciphertext"] == ARMOR
    assert sealed == [(recipient(), NONCE)]
    row = db.execute("SELECT * FROM key_challenges WHERE id = ?", (result["challenge_id"],)).fetchone()
    assert (row["user_id"], row["key_id"], row["purpose"], row["target_ref"]) == (
        user_id(db, "alice"), kid, "rotate", "a" * 64)
    assert row["nonce_sha256"] == challenges.digest(NONCE, "rotate", "a" * 64, kid,
                                                    user_id(db, "alice"), EXPIRES)
    assert (row["created_at"], row["updated_at"], row["consumed_at"]) == (CREATED, CREATED, None)
    assert target != kid
    assert NONCE.decode("ascii") not in dump(db)
    assert ARMOR.decode("ascii") not in dump(db)
    assert NONCE.decode("ascii") not in caplog.text
    assert ARMOR.decode("ascii") not in caplog.text


def test_issue_checks_age_version_before_opening_the_write_transaction(db, users, clock, sealed, monkeypatch):
    active_key(db)
    pending_key(db, fingerprint="a" * 64)
    seen = []

    def version():
        seen.append(db.in_transaction)
        return "v1.1.1"

    monkeypatch.setattr(agecli, "version", version)
    issue(db, "alice", "rotate", "a" * 64)
    assert seen == [False]


def test_issue_rejects_bad_purpose_target_and_missing_or_inactive_key(db, users, clock, sealed):
    pending_key(db)
    user = user_row(db, "alice")
    for purpose, target in (("unknown", "a" * 64), ("rotate", "ABC"),
                            ("rotate", "age-secret-key-" + "Q" * 58)):
        with pytest.raises(challenges.ChallengeError):
            challenges.issue(db, user, purpose, target, CREATED)
    with pytest.raises(challenges.ChallengeError):
        challenges.issue(db, user, "rotate", "a" * 64, CREATED)
    kid = active_key(db)
    db.execute("UPDATE keys SET state = 'retired', updated_at = ? WHERE id = ?", (CREATED, kid))
    with pytest.raises(challenges.ChallengeError):
        issue(db, "alice", "rotate", "a" * 64)
    assert db.execute("SELECT COUNT(*) FROM key_challenges").fetchone()[0] == 0


def test_q10_rotate_and_delete_targets(db, users, clock, sealed, tb01):
    kid = active_key(db)
    new_key = pending_key(db, fingerprint="b" * 64)
    pid = "c" * 32
    snapshot(db, user_id(db, "alice"), tb01[0], kid, pid)
    assert issue(db, "alice", "rotate", "b" * 64)["challenge_id"]
    consume(db, 1, "alice", "rotate", "b" * 64)
    assert issue(db, "alice", "delete", pid)["challenge_id"]
    assert new_key != kid


@pytest.mark.parametrize("purpose,target", [
    ("rotate", "A" * 64), ("rotate", "a" * 63), ("delete", "A" * 32),
    ("delete", "f" * 32), ("cancel_delete", "approval:abc"),
    ("cancel_delete", "f" * 32), ("discard_home", "tb-01"),
    ("discard_home", "setup:missing"), ("discard_home", "setup:TB-01")])
def test_q10_refuses_targets_that_do_not_resolve(db, users, clock, sealed, tb01, purpose, target):
    active_key(db)
    with pytest.raises(challenges.ChallengeError):
        issue(db, "alice", purpose, target)
    assert db.execute("SELECT COUNT(*) FROM key_challenges").fetchone()[0] == 0


def test_q10_cross_user_and_held_targets_are_refused(db, users, clock, sealed, tb01):
    alice_key = active_key(db)
    bob_key = active_key(db, "bob", "p")
    pending_key(db, "bob", "z")
    snapshot(db, user_id(db, "bob"), tb01[0], bob_key, "1" * 32)
    snapshot(db, user_id(db, "alice"), tb01[0], alice_key, "2" * 32,
             held=user_id(db, "alice"), requested_by=user_id(db, "alice"))
    for purpose, target in (("rotate", keys.fingerprint(recipient("z"))),
                            ("delete", "1" * 32), ("delete", "2" * 32)):
        with pytest.raises(challenges.ChallengeError):
            issue(db, "alice", purpose, target)


def test_q10_cancel_delete_accepts_a_held_snapshot_or_the_users_approval(db, users, clock, sealed, tb01):
    kid = active_key(db)
    owner = user_id(db, "alice")
    held = snapshot(db, owner, tb01[0], kid, "3" * 32, held=owner, requested_by=owner)
    assert issue(db, "alice", "cancel_delete", "3" * 32)["challenge_id"]
    aid = approved_reset(db, kid)
    db.execute("UPDATE snapshots SET hold_approval_id = ?, delete_requested_by = ? WHERE id = ?",
               (aid, user_id(db, "dev-admin"), held))
    result = issue(db, "alice", "cancel_delete", f"approval:{aid}")
    assert result["challenge_id"] != 1
    with pytest.raises(challenges.ChallengeError):
        issue(db, "bob", "cancel_delete", f"approval:{aid}")


def test_q10_cancel_delete_public_id_requires_an_own_delete_hold(db, users, clock, sealed, tb01):
    kid = active_key(db)
    owner = user_id(db, "alice")
    own_hold = "5" * 32
    approval_hold = "6" * 32
    snapshot(db, owner, tb01[0], kid, own_hold, held=owner, requested_by=owner)
    snapshot(db, owner, tb01[0], kid, approval_hold)
    assert issue(db, "alice", "cancel_delete", own_hold)["challenge_id"]
    aid = approved_reset(db, kid)
    held = db.execute("SELECT hold_approval_id FROM snapshots WHERE public_id = ?",
                      (approval_hold,)).fetchone()
    assert held["hold_approval_id"] == aid
    with pytest.raises(challenges.ChallengeError):
        issue(db, "alice", "cancel_delete", approval_hold)
    assert db.execute("SELECT COUNT(*) FROM key_challenges").fetchone()[0] == 1


def test_q10_discard_home_needs_the_users_kept_home(db, users, clock, sealed, tb01):
    active_key(db)
    reservation(db, user_id(db, "bob"), tb01[0], tb01[1], "quarantined")
    with pytest.raises(challenges.ChallengeError):
        issue(db, "alice", "discard_home", "setup:tb-01")
    reservation(db, user_id(db, "alice"), tb01[0], tb01[1], "quarantined")
    assert issue(db, "alice", "discard_home", "setup:tb-01")["challenge_id"]


def test_at_most_three_open_challenges(db, users, clock, sealed, tb01):
    kid = active_key(db)
    pid = "4" * 32
    snapshot(db, user_id(db, "alice"), tb01[0], kid, pid)
    for _ in range(3):
        issue(db, "alice", "delete", pid)
    with pytest.raises(challenges.ChallengeError):
        issue(db, "alice", "delete", pid)
    consume(db, 1, "alice", "delete", pid)
    assert issue(db, "alice", "delete", pid)["challenge_id"] == 4
    assert db.execute("SELECT COUNT(*) FROM key_challenges WHERE consumed_at IS NULL "
                      "AND expires_at > ?", (CREATED,)).fetchone()[0] == 3


def test_consume_single_use_expiry_and_wrong_answer(db, users, clock, sealed, tb01):
    kid = active_key(db)
    pid = "5" * 32
    snapshot(db, user_id(db, "alice"), tb01[0], kid, pid)
    cid = issue(db, "alice", "delete", pid)["challenge_id"]

    with pytest.raises(AssertionError):
        challenges.consume(db, cid, user_row(db, "alice"), "delete", pid, NONCE, SOON)

    cases = [
        ("mismatch", "delete", pid, NONCE[::-1], SOON),
        ("mismatch", "cancel_delete", pid, NONCE, SOON),
        ("mismatch", "delete", "f" * 32, NONCE, SOON),
        ("expired", "delete", pid, NONCE, EXPIRES),
        ("expired", "delete", pid, NONCE, LATE),
    ]
    for expected, purpose, target, nonce, now in cases:
        with pytest.raises(challenges.ChallengeError) as err:
            consume(db, cid, "alice", purpose, target, nonce, now)
        assert str(err.value) == expected
        assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] is None

    consume(db, cid, "alice", "delete", pid)
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] == SOON
    with pytest.raises(challenges.ChallengeError) as replay:
        consume(db, cid, "alice", "delete", pid)
    assert str(replay.value) == "used"


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_revoked_or_retired_key_kills_an_open_challenge(db, users, clock, sealed, tb01, state):
    kid = active_key(db)
    pid = "6" * 32
    snapshot(db, user_id(db, "alice"), tb01[0], kid, pid)
    cid = issue(db, "alice", "delete", pid)["challenge_id"]
    db.execute("UPDATE keys SET state = ?, updated_at = ? WHERE id = ?", (state, CREATED, kid))
    with pytest.raises(challenges.ChallengeError) as err:
        consume(db, cid, "alice", "delete", pid)
    assert str(err.value) == "mismatch"
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] is None


def test_consume_refuses_another_users_challenge(db, users, clock, sealed, tb01):
    kid = active_key(db)
    pid = "7" * 32
    snapshot(db, user_id(db, "alice"), tb01[0], kid, pid)
    cid = issue(db, "alice", "delete", pid)["challenge_id"]
    with pytest.raises(challenges.ChallengeError) as err:
        consume(db, cid, "bob", "delete", pid)
    assert str(err.value) == "mismatch"
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] is None


def test_owner_cancel_clears_only_that_approvals_holds(db, users, clock, sealed, tb01, monkeypatch):
    monkeypatch.setattr(models, "utcnow", lambda: SOON)
    kid = active_key(db)
    owner = user_id(db, "alice")
    own = snapshot(db, owner, tb01[0], kid, "8" * 32, held=owner, requested_by=owner)
    held = snapshot(db, owner, tb01[0], kid, "9" * 32)
    other_key = active_key(db, "bob", "p")
    foreign = snapshot(db, user_id(db, "bob"), tb01[0], other_key, "a" * 32,
                       held=AFTER, requested_by=user_id(db, "dev-admin"))
    aid = approved_reset(db, kid)
    second = models.insert(db, "admin_approvals", {
        "action": "home_discard", "target_ref": "setup:tb-01",
        "requested_by": user_id(db, "dev-admin"),
        "decided_by": user_id(db, "second-admin"), "state": "approved",
        "decided_at": CREATED, "execute_after": AFTER, "created_at": CREATED,
        "updated_at": CREATED})
    other = snapshot(db, owner, tb01[0], kid, "b" * 32, held=user_id(db, "dev-admin"),
                     requested_by=user_id(db, "dev-admin"), approval_id=second)
    db.execute("UPDATE snapshots SET hold_approval_id = ? WHERE id = ?", (aid, foreign))
    db.execute("UPDATE snapshots SET hold_approval_id = ? WHERE id = ?", (aid, held))
    cid = issue(db, "alice", "cancel_delete", f"approval:{aid}", CREATED)["challenge_id"]
    approvals.cancel_by_owner(db, aid, user_row(db, "alice"), SOON, NONCE, cid)

    row = db.execute("SELECT * FROM admin_approvals WHERE id = ?", (aid,)).fetchone()
    assert (row["state"], row["executed_at"]) == ("cancelled", None)
    for snap_id in (foreign, held):
        snap = db.execute("SELECT * FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
        assert (snap["delete_after"], snap["delete_requested_by"], snap["hold_approval_id"]) == (
            None, None, None)
    own_after = db.execute("SELECT * FROM snapshots WHERE id = ?", (own,)).fetchone()
    assert (own_after["delete_after"], own_after["hold_approval_id"]) == (AFTER, None)
    other_after = db.execute("SELECT * FROM snapshots WHERE id = ?", (other,)).fetchone()
    assert (other_after["delete_after"], other_after["hold_approval_id"]) == (AFTER, second)
    assert db.execute("SELECT state FROM keys WHERE id = ?", (kid,)).fetchone()[0] == "active"
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] == SOON
    audit = db.execute("SELECT * FROM audit_events WHERE action = 'approval_cancel'").fetchone()
    assert audit["actor"] == "alice" and audit["object_type"] == "approval"
    detail = models.from_json(audit["detail"])
    assert detail["approval_id"] == aid and detail["state"] == "cancelled" and detail["cleared"] == 2
    assert keys.execute_due_resets(db, AFTER, lambda conn, row: None) == 0
    assert NONCE.decode("ascii") not in dump(db)


@pytest.mark.parametrize("change", ["executed", "rejected", "pending", "foreign"])
def test_owner_cancel_refuses_a_non_cancellable_approval(db, users, clock, sealed, tb01, change):
    kid = active_key(db)
    aid = approved_reset(db, kid)
    cid = issue(db, "alice", "cancel_delete", f"approval:{aid}")["challenge_id"]
    if change == "executed":
        db.execute("UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (AFTER, aid))
    elif change == "rejected":
        db.execute("UPDATE admin_approvals SET state = 'rejected' WHERE id = ?", (aid,))
    elif change == "pending":
        db.execute("UPDATE admin_approvals SET state = 'pending', decided_by = NULL, "
                   "decided_at = NULL, execute_after = NULL WHERE id = ?", (aid,))
    user = "bob" if change == "foreign" else "alice"
    with pytest.raises((approvals.ApprovalServiceError, challenges.ChallengeError)):
        approvals.cancel_by_owner(db, aid, user_row(db, user), SOON, NONCE, cid)
    assert db.execute("SELECT state FROM admin_approvals WHERE id = ?", (aid,)).fetchone()[0] != "cancelled"
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] is None


def test_owner_cancel_rolls_back_on_a_challenge_failure(db, users, clock, sealed, tb01):
    kid = active_key(db)
    aid = approved_reset(db, kid)
    snapshot(db, user_id(db, "alice"), tb01[0], kid, "c" * 32,
             held=user_id(db, "dev-admin"), requested_by=user_id(db, "dev-admin"), approval_id=aid)
    cid = issue(db, "alice", "cancel_delete", f"approval:{aid}")["challenge_id"]
    with pytest.raises(challenges.ChallengeError):
        approvals.cancel_by_owner(db, aid, user_row(db, "alice"), SOON, NONCE[::-1], cid)
    assert db.execute("SELECT state FROM admin_approvals WHERE id = ?", (aid,)).fetchone()[0] == "approved"
    assert db.execute("SELECT COUNT(*) FROM snapshots WHERE hold_approval_id = ?", (aid,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'approval_cancel'").fetchone()[0] == 0


def test_owner_cancel_requires_an_explicit_challenge_id(db, users, clock, sealed, tb01):
    kid = active_key(db)
    snapshot(db, user_id(db, "alice"), tb01[0], kid, "d" * 32)
    aid = approved_reset(db, kid)
    cid = issue(db, "alice", "cancel_delete", f"approval:{aid}")["challenge_id"]

    with pytest.raises(TypeError):
        approvals.cancel_by_owner(db, aid, user_row(db, "alice"), SOON, NONCE)
    with pytest.raises(challenges.ChallengeError) as err:
        approvals.cancel_by_owner(db, aid, user_row(db, "alice"), SOON, NONCE, None)
    assert str(err.value) == "mismatch"
    assert db.execute("SELECT state FROM admin_approvals WHERE id = ?", (aid,)).fetchone()[0] == "approved"
    assert db.execute("SELECT consumed_at FROM key_challenges WHERE id = ?", (cid,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM snapshots WHERE hold_approval_id = ?", (aid,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'approval_cancel'").fetchone()[0] == 0
