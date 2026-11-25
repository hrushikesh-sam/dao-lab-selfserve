"""B-105: key enrollment, activation and the end-of-hold reset execution."""
import logging

import pytest

from service import keys, models
from tests.conftest import mk_user

CANARY = "AGE-SECRET-KEY-1" + "Q" * 58
CREATED = "2026-09-01T09:00:00Z"
DUE = "2026-09-02T09:00:00Z"
LATER = "2026-09-03T09:00:00Z"


def recipient(tag="q"):
    return "age1" + "q" * 57 + tag


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(models, "utcnow", lambda: CREATED)
    return CREATED


def admin_row(conn, name):
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def active_key(conn, username, tag="q", admins=("dev-admin",)):
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    kid = keys.enroll(conn, uid, recipient(tag))
    keys.activate(conn, kid, admin_row(conn, admins[0]))
    return kid


def make_snapshot(conn, user_id, setup_id, key_id, public_id, state="saved",
                  delete_after=None, requested_by=None, hold_approval_id=None):
    conn.execute(
        """INSERT INTO snapshots (public_id, user_id, setup_id, key_id, state, delete_after,
                                  delete_requested_by, hold_approval_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (public_id, user_id, setup_id, key_id, state, delete_after, requested_by,
         hold_approval_id, CREATED, CREATED))


def make_approval(conn, key_id, state="approved", execute_after=DUE, executed_at=None,
                  requested_by=1, decided_by=2, created=CREATED):
    return models.insert(conn, "admin_approvals", {
        "action": "lost_key_reset", "target_ref": f"key:{key_id}", "requested_by": requested_by,
        "decided_by": decided_by, "state": state, "decided_at": created if decided_by else None,
        "execute_after": execute_after, "executed_at": executed_at,
        "created_at": created, "updated_at": created})


def test_fingerprint_is_the_lowercase_sha256_of_the_recipient():
    import hashlib
    value = recipient()
    assert keys.fingerprint(value) == hashlib.sha256(value.encode("ascii")).hexdigest()
    assert keys.fingerprint(value) == keys.fingerprint(value).lower()


def test_enroll_validates_the_recipient_and_replaces_only_the_pending_key(db, clock):
    with pytest.raises(keys.KeyServiceError) as bad:
        keys.enroll(db, 1, "age1short")
    assert (bad.value.code, bad.value.http_status) == ("validation_error", 422)
    assert db.execute("SELECT COUNT(*) FROM keys").fetchone()[0] == 0

    first = keys.enroll(db, 1, recipient("q"))
    assert db.execute("SELECT state FROM keys WHERE id = ?", (first,)).fetchone()[0] == "pending"
    second = keys.enroll(db, 1, recipient("p"))
    assert [r["id"] for r in db.execute("SELECT id FROM keys")] == [second]
    rows = db.execute("SELECT action, detail FROM audit_events ORDER BY id").fetchall()
    assert [r["action"] for r in rows] == ["key_enroll", "key_enroll"]
    assert models.from_json(rows[-1]["detail"])["state"] == "pending"
    assert models.from_json(rows[-1]["detail"])["key_id"] == second


def test_enroll_refuses_a_duplicate_recipient_without_leaving_a_pending_key(db, clock, users):
    keys.enroll(db, 2, recipient("q"))
    before = [tuple(r) for r in db.execute("SELECT * FROM keys")]
    with pytest.raises(keys.KeyServiceError) as dup:
        keys.enroll(db, 1, recipient("q"))
    assert (dup.value.code, dup.value.http_status) == ("key_exists", 409)
    assert [tuple(r) for r in db.execute("SELECT * FROM keys")] == before


def test_activation_checks_role_ownership_state_and_the_existing_active_key(db, clock, users):
    mk_user(db, "own-admin", role="admin")
    alice = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
    kid = keys.enroll(db, alice, recipient("q"))

    with pytest.raises(keys.KeyServiceError) as user_activation:
        keys.activate(db, kid, admin_row(db, "alice"))
    assert (user_activation.value.code, user_activation.value.http_status) == ("forbidden", 403)

    own = keys.enroll(db, db.execute("SELECT id FROM users WHERE username='own-admin'").fetchone()[0],
                      recipient("z"))
    with pytest.raises(keys.KeyServiceError) as self_activation:
        keys.activate(db, own, admin_row(db, "own-admin"))
    assert self_activation.value.code == "invalid_state"

    keys.activate(db, kid, admin_row(db, "dev-admin"))
    row = db.execute("SELECT state, activated_by, activated_at FROM keys WHERE id = ?", (kid,)).fetchone()
    assert tuple(row) == ("active", admin_row(db, "dev-admin")["id"], CREATED)

    second = keys.enroll(db, alice, recipient("p"))
    with pytest.raises(keys.KeyServiceError) as second_active:
        keys.activate(db, second, admin_row(db, "dev-admin"))
    assert second_active.value.code == "active_key_exists"
    with pytest.raises(keys.KeyServiceError) as missing:
        keys.activate(db, 99999, admin_row(db, "dev-admin"))
    assert missing.value.code == "not_found"
    with pytest.raises(keys.KeyServiceError) as not_pending:
        keys.activate(db, kid, admin_row(db, "dev-admin"))
    assert not_pending.value.code == "invalid_state"
    assert keys.active_key(db, alice)["id"] == kid
    assert tuple(keys.active_key(db, alice)) == (kid, alice, recipient("q"), keys.fingerprint(recipient("q")))

    audit = db.execute("SELECT action, detail FROM audit_events WHERE action = 'key_activate'").fetchone()
    assert models.from_json(audit["detail"])["state"] == "active"


def test_reset_open_for_user_follows_approval_lifecycle(db, clock, users, tb01):
    alice = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
    kid = active_key(db, "alice")
    bob = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    active_key(db, "bob", tag="p")
    assert keys.reset_open_for_user(db, alice) is False
    pending = make_approval(db, kid, state="pending", decided_by=None, execute_after=None)
    assert keys.reset_open_for_user(db, alice) is True
    assert keys.reset_open_for_user(db, bob) is False
    db.execute("UPDATE admin_approvals SET state = 'cancelled' WHERE id = ?", (pending,))
    assert keys.reset_open_for_user(db, alice) is False
    approved = make_approval(db, kid, executed_at=None)
    assert keys.reset_open_for_user(db, alice) is True
    db.execute("UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (DUE, approved))
    assert keys.reset_open_for_user(db, alice) is False


def test_execute_due_resets_revokes_and_deletes_every_snapshot_once(db, clock, users, tb01, caplog):
    caplog.set_level(logging.DEBUG)
    kid = active_key(db, "alice")
    owner = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    setup = tb01[0]
    for i, state in enumerate(("saved", "capturing", "failed", "saved")):
        make_snapshot(db, owner, setup, kid, f"{i + 1:032x}", state)
    make_snapshot(db, owner, setup, kid, "a" * 32, "saved", DUE, 2, None)
    make_approval(db, kid)
    seen = []

    def delete_snapshot(conn, row):
        seen.append(dict(row))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (row["id"],))

    assert keys.execute_due_resets(db, LATER, delete_snapshot) == 1
    assert len(seen) == 5
    assert db.execute("SELECT state FROM keys WHERE id = ?", (kid,)).fetchone()[0] == "revoked"
    assert db.execute("SELECT COUNT(*) FROM snapshots WHERE key_id = ?", (kid,)).fetchone()[0] == 0
    assert db.execute("SELECT executed_at FROM admin_approvals").fetchone()[0] == LATER
    audits = db.execute("SELECT action, detail FROM audit_events ORDER BY id").fetchall()
    assert audits[-1][0] == "key_reset_execute"
    assert models.from_json(audits[-1][1])["snapshots"] == 5
    assert keys.execute_due_resets(db, LATER, delete_snapshot) == 0
    assert len(seen) == 5
    dump = "\n".join(db.iterdump())
    assert CANARY not in dump and CANARY not in caplog.text and CANARY not in repr(seen)


def test_execute_due_resets_is_idempotent_and_skips_not_yet_due(db, clock, users, tb01):
    kid = active_key(db, "alice")
    owner = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    make_snapshot(db, owner, tb01[0], kid, f"{1:032x}")
    make_approval(db, kid, execute_after=LATER)
    seen = []

    def delete_snapshot(conn, row):
        seen.append(row["id"])
        conn.execute("DELETE FROM snapshots WHERE id = ?", (row["id"],))

    assert keys.execute_due_resets(db, DUE, delete_snapshot) == 0
    assert seen == []
    assert keys.execute_due_resets(db, LATER, delete_snapshot) == 1
    assert len(seen) == 1
    assert keys.execute_due_resets(db, LATER, delete_snapshot) == 0
    assert len(seen) == 1


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_execute_due_resets_keeps_snapshots_when_the_key_is_no_longer_active(db, clock, users, tb01, state):
    kid = active_key(db, "alice")
    owner = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    make_snapshot(db, owner, tb01[0], kid, f"{2:032x}")
    make_approval(db, kid)
    db.execute("UPDATE keys SET state = ?, updated_at = ? WHERE id = ?", (state, CREATED, kid))

    def delete_snapshot(conn, row):
        raise AssertionError("a retired/revoked key must not delete snapshots")

    assert keys.execute_due_resets(db, LATER, delete_snapshot) == 1
    assert db.execute("SELECT COUNT(*) FROM snapshots WHERE key_id = ?", (kid,)).fetchone()[0] == 1
    audit = db.execute("SELECT action, detail FROM audit_events WHERE action = 'key_reset_noop'").fetchone()
    assert models.from_json(audit["detail"])["key_state"] == state


def test_execute_due_resets_rolls_back_everything_if_a_callback_fails(db, clock, users, tb01):
    kid = active_key(db, "alice")
    owner = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
    make_snapshot(db, owner, tb01[0], kid, f"{3:032x}")
    aid = make_approval(db, kid)

    def boom(conn, row):
        raise RuntimeError("filesystem failure")

    with pytest.raises(RuntimeError):
        keys.execute_due_resets(db, LATER, boom)
    assert db.execute("SELECT state FROM keys WHERE id = ?", (kid,)).fetchone()[0] == "active"
    assert db.execute("SELECT executed_at FROM admin_approvals WHERE id = ?", (aid,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
