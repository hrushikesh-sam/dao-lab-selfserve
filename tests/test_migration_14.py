"""B-104 PR B (B-104-DDL.md v4.1 §3, §6.2 T-1..T-21 and the v4.1 tests; v4 signed off by
Rama 23 Nov, v4.1-1..6 signed off by Rama 24 Nov): migration 14 `snapshots_v1`.

One migration adds the Phase 8 snapshot schema: tables `keys`, `key_challenges`,
`admin_approvals`, `snapshots`; columns `reservations.save_snapshot`, `jobs.snapshot_id`,
`setups.snapshot_enabled`; and the `restore` job kind (an m6-style text edit of the jobs.kind
CHECK).

Synthetic DBs only (`:memory:` or `tmp_path`). The tests never read the DDL doc. SQL that v4
gives as text (the challenge CAS §3.4, the execution CAS §3.6, the retention prune §3.5) is
copied literally below, with its section cited. The other steps of the lost-key reset flows
(T-16/T-19/T-20) are written here from the v4 §3.3 description. There is no product code for
them yet, so those tests prove what the schema allows and refuses, not what later code does.

T-2 is `tests/test_migrations_guard.py::test_t2_placeholders_change_nothing_but_user_version`;
T-18 is the full suite (`make test`)."""
import hashlib
import itertools
import re
import sqlite3
from types import SimpleNamespace

import pytest

from service import migrations, models
from tests.conftest import T, mk_res, mk_setup, mk_usecase, mk_user

# ---- spec values (v4 text, not imported from the code under test) ---------------------

NEW_TABLES = ("keys", "key_challenges", "admin_approvals", "snapshots")
NEW_INDEXES = ("idx_keys_one_active", "idx_keys_one_pending", "idx_key_challenges_open",
               "idx_admin_approvals_one_open", "idx_admin_approvals_due",
               "idx_snapshots_user_setup", "idx_snapshots_key", "idx_snapshots_delete_due",
               "idx_snapshots_hold_approval")
NEW_COLUMNS = {"reservations": "save_snapshot", "jobs": "snapshot_id",
               "setups": "snapshot_enabled"}
OLD_KINDS_TEXT = "'deploy', 'cleanup', 'health_probe', 'reconcile')"              # v4:505
NEW_KINDS_TEXT = "'deploy', 'cleanup', 'health_probe', 'reconcile', 'restore')"   # v4:506
REWRITE_ERROR = "migration 14: cannot find the jobs.kind CHECK to rewrite"         # v4:513

# v4 §3.4: single-use consume. "rowcount must be exactly 1".
CHALLENGE_CAS = """UPDATE key_challenges SET consumed_at = :now, updated_at = :now
 WHERE id = :id AND user_id = :uid AND purpose = :purpose AND target_ref = :target
   AND consumed_at IS NULL AND expires_at > :now
   AND key_id IN (SELECT id FROM keys WHERE state = 'active')"""
# v4 §3.6: "Execution is a CAS: `UPDATE ... SET executed_at = :now WHERE ...`"; the `...` is
# the table name admin_approvals.
EXECUTION_CAS = ("UPDATE admin_approvals SET executed_at = :now WHERE id = :id AND state = "
                 "'approved' AND executed_at IS NULL AND (execute_after IS NULL OR "
                 "execute_after <= :now)")
# v4 §3.5: retention prune (Q-16): keep the newest 3 unheld saved rows per (user, setup)
RETENTION_PRUNE = """DELETE FROM snapshots WHERE id IN (
    SELECT id FROM snapshots
     WHERE user_id = :uid AND setup_id = :sid AND state = 'saved' AND delete_after IS NULL
     ORDER BY created_at DESC, id DESC
     LIMIT -1 OFFSET 3)"""

T0 = "2026-11-24T00:00:00Z"
T1 = "2026-11-24T00:01:00Z"
EXP = "2026-11-24T00:10:00Z"          # a challenge's expiry: created + 10 min (design:66)
OWN_HOLD = "2026-11-24T12:00:00Z"     # an earlier hold of the owner's own
HOLD = "2026-11-25T00:01:00Z"         # T1 + 24h: the reset's execute_after = rows' delete_after
LATER = "2026-11-25T00:01:01Z"
S_LIKE = "AGE-SECRET-KEY-1" + "Q" * 58          # S-shaped (shape only; not a real key)
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_seq = itertools.count(1)


# ---- DB builders ------------------------------------------------------------------------

def _conn(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _at_version(conn, version):
    """Baseline + migrations 1..version run directly, user_version = version (the
    test_migrations_guard.py / test_reservation_states.py pattern)."""
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    for m in migrations.MIGRATIONS[:version]:
        m.fn(conn)
    conn.execute(f"PRAGMA user_version = {int(version)}")
    assert migrations.current_version(conn) == version
    return conn


def _latest(path=":memory:"):
    conn = _conn(path)
    models.init_db(conn)
    return conn


def _m14():
    return {m.version: m for m in migrations.MIGRATIONS}[14].fn


def _dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _jobs_sql(conn):
    return conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
                        ).fetchone()[0]


def _schema_version(conn):
    return conn.execute("PRAGMA schema_version").fetchone()[0]


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _recipient(n):
    """A valid-shape age X25519 recipient: 'age1' + 58 bech32 characters, unique per n."""
    chars = []
    for _ in range(58):
        chars.append(BECH32[n % 32])
        n //= 32
    return "age1" + "".join(chars)


def ins(conn, table, **cols):
    cols.setdefault("created_at", T0)
    cols.setdefault("updated_at", cols["created_at"])
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    return conn.execute(sql, list(cols.values())).lastrowid


def key(conn, user_id, state="pending", activated_by=None, **cols):
    recipient = _recipient(next(_seq))
    if state != "pending":
        cols.setdefault("activated_by", activated_by)
        cols.setdefault("activated_at", T1)
    return ins(conn, "keys", user_id=user_id, recipient=recipient,
               fingerprint=_sha(recipient), state=state, **cols)


def challenge(conn, user_id, key_id, purpose, target_ref, created=T0, expires=EXP):
    return ins(conn, "key_challenges", user_id=user_id, key_id=key_id, purpose=purpose,
               target_ref=target_ref, nonce_sha256=_sha(f"nonce-{next(_seq)}"),
               expires_at=expires, created_at=created)


def snapshot(conn, user_id, setup_id, key_id, state="saved", created=T0, **cols):
    return ins(conn, "snapshots", public_id=f"{next(_seq):032x}", user_id=user_id,
               setup_id=setup_id, key_id=key_id, state=state, created_at=created, **cols)


def approval(conn, action, target_ref, requested_by, created=T0):
    return ins(conn, "admin_approvals", action=action, target_ref=target_ref,
               requested_by=requested_by, created_at=created)


def public_id(conn, snap_id):
    return conn.execute("SELECT public_id FROM snapshots WHERE id = ?", (snap_id,)).fetchone()[0]


def hold_of(conn, snap_id):
    row = conn.execute("SELECT delete_after, delete_requested_by, hold_approval_id "
                       "FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
    return None if row is None else tuple(row)


def key_state(conn, key_id):
    return conn.execute("SELECT state FROM keys WHERE id = ?", (key_id,)).fetchone()[0]


def _world(conn=None):
    """A latest-schema DB with an owner (active key), another user, two admins, a setup, a
    released reservation, one saved snapshot, one open challenge, one pending approval."""
    conn = conn or _latest()
    w = SimpleNamespace(conn=conn)
    w.owner = mk_user(conn, "owner")[0]
    w.other = mk_user(conn, "other")[0]
    w.adm1 = mk_user(conn, "adm1", "admin")[0]
    w.adm2 = mk_user(conn, "adm2", "admin")[0]
    w.setup = mk_setup(conn, "tb-01", ["wire"])
    w.usecase = mk_usecase(conn, "uc1", ["wire"])
    w.res = mk_res(conn, w.setup, w.usecase, T(10), T(11), state="released", user_id=w.owner)
    w.key = key(conn, w.owner, "active", activated_by=w.adm1)
    w.snap = snapshot(conn, w.owner, w.setup, w.key, reservation_id=w.res)
    w.chal = challenge(conn, w.owner, w.key, "delete", public_id(conn, w.snap))
    w.appr = approval(conn, "tripwire_accept", "sdp-pf1-vf8", w.adm1)
    return w


@pytest.fixture
def w():
    world = _world()
    yield world
    world.conn.close()


def refused(conn, sql, args=(), match=None):
    """`sql` raises IntegrityError (optionally naming `match`); the DB is left as it was."""
    conn.execute("SAVEPOINT t")
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute(sql, args)
    finally:
        conn.execute("ROLLBACK TO t")
        conn.execute("RELEASE t")
    if match is not None:
        assert match in str(exc.value), str(exc.value)
    return str(exc.value)


def accepted(conn, sql, args=(), keep=False):
    """`sql` succeeds on exactly one row; rolled back unless `keep`."""
    conn.execute("SAVEPOINT t")
    try:
        assert conn.execute(sql, args).rowcount == 1
    except BaseException:
        conn.execute("ROLLBACK TO t")
        conn.execute("RELEASE t")
        raise
    if not keep:
        conn.execute("ROLLBACK TO t")
    conn.execute("RELEASE t")


# ---- T-1: a fresh DB reaches 14 -------------------------------------------------------

def test_t1_fresh_db_reaches_14_with_new_tables_and_columns():
    conn = _conn()
    assert models.init_db(conn) == list(range(1, 15))
    assert migrations.latest_version() == 14 == migrations.current_version(conn)
    last = migrations.MIGRATIONS[-1]
    assert (last.version, last.name) == (14, "snapshots_v1")
    for table in NEW_TABLES:
        assert migrations.has_table(conn, table), table
    for table, column in NEW_COLUMNS.items():
        assert migrations.has_column(conn, table, column), (table, column)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(NEW_INDEXES) <= indexes
    sql = _jobs_sql(conn)
    assert sql.count(NEW_KINDS_TEXT) == 1 and OLD_KINDS_TEXT not in sql
    assert models.init_db(conn) == []


# ---- T-3: legacy rows across 14 --------------------------------------------------------

@pytest.mark.parametrize("from_version", [11, 13])
def test_t3_upgrade_keeps_legacy_rows_with_preserve_defaults(from_version):
    conn = _at_version(_conn(), from_version)
    owner = mk_user(conn, "owner")[0]
    sid = mk_setup(conn, "tb-01", ["wire"])
    cid = mk_usecase(conn, "uc1", ["wire"])
    rid = mk_res(conn, sid, cid, T(10), T(11), state="released", user_id=owner)
    jid = models.create_job(conn, rid, "cleanup")
    tables = {"reservations": rid, "setups": sid, "jobs": jid}
    before = {t: dict(conn.execute(f"SELECT * FROM {t} WHERE id = ?", (i,)).fetchone())
              for t, i in tables.items()}
    assert models.init_db(conn) == list(range(from_version + 1, 15))
    after = {t: dict(conn.execute(f"SELECT * FROM {t} WHERE id = ?", (i,)).fetchone())
             for t, i in tables.items()}
    for table in tables:
        new_col = NEW_COLUMNS[table]
        assert set(after[table]) == set(before[table]) | {new_col}
        assert {k: after[table][k] for k in before[table]} == before[table]   # unchanged
    assert after["reservations"]["save_snapshot"] == 0          # PRESERVE (design:25; E-13)
    assert after["setups"]["snapshot_enabled"] == 0
    assert after["jobs"]["snapshot_id"] is None
    conn.execute("UPDATE jobs SET kind = kind WHERE id = ?", (jid,))   # CHECK re-evaluated
    assert models.create_job(conn, rid, "cleanup")
    conn.close()


# ---- T-4: idempotence -----------------------------------------------------------------

def test_t4_m14_twice_in_one_transaction_is_a_noop():
    conn = _at_version(_conn(), 13)
    fn = _m14()
    conn.execute("BEGIN IMMEDIATE")
    fn(conn)
    dump, version = _dump(conn), _schema_version(conn)
    fn(conn)                                         # including the jobs.kind rewrite
    assert (_dump(conn), _schema_version(conn)) == (dump, version)
    conn.execute("COMMIT")
    assert _jobs_sql(conn).count(NEW_KINDS_TEXT) == 1
    conn.close()


def test_t4_m14_on_an_up_to_date_db_changes_nothing():
    conn = _latest()
    dump, version, changes = _dump(conn), _schema_version(conn), conn.total_changes
    _m14()(conn)
    assert (_dump(conn), _schema_version(conn), conn.total_changes) == (dump, version, changes)
    assert models.init_db(conn) == []
    conn.close()


# ---- T-5: CHECK violations --------------------------------------------------------------

def _t5_cases():
    rec = _recipient(10 ** 6)
    good_hex = "a" * 64
    cases = {
        "save_snapshot 2": ("UPDATE reservations SET save_snapshot = 2 WHERE id = {res}", ()),
        "save_snapshot NULL": ("UPDATE reservations SET save_snapshot = NULL WHERE id = {res}", ()),
        "snapshot_enabled 2": ("UPDATE setups SET snapshot_enabled = 2 WHERE id = {setup}", ()),
        "snapshot_enabled NULL": ("UPDATE setups SET snapshot_enabled = NULL WHERE id = {setup}", ()),
        "keys.state bogus": ("UPDATE keys SET state = 'bogus' WHERE id = {key}", ()),
        "purpose restore": ("UPDATE key_challenges SET purpose = 'restore' WHERE id = {chal}", ()),
        "action legacy_home": ("UPDATE admin_approvals SET action = 'legacy_home' WHERE id = {appr}", ()),
        "approval state executed": ("UPDATE admin_approvals SET state = 'executed' WHERE id = {appr}", ()),
        "expires_at <= created_at": ("UPDATE key_challenges SET expires_at = ? WHERE id = {chal}", (T0,)),
        "consumed_at > expires_at": ("UPDATE key_challenges SET consumed_at = ? WHERE id = {chal}",
                                     ("2026-11-24T00:11:00Z",)),
    }
    for state in ("restore_incomplete", "deleted", "saving", "bogus"):
        cases[f"snapshots.state {state}"] = (
            "UPDATE snapshots SET state = ? WHERE id = {snap}", (state,))
    for label, value in {"63 chars": "a" * 63, "65 chars": "a" * 65, "uppercase": "A" * 64,
                         "non-hex": "g" * 64}.items():
        cases[f"nonce_sha256 {label}"] = (
            "UPDATE key_challenges SET nonce_sha256 = ? WHERE id = {chal}", (value,))
    for label, value in {"prefix age2": "age2" + rec[4:], "prefix AGE1": "AGE1" + rec[4:],
                         "61 chars": rec[:-1], "63 chars": rec + "q",
                         "bech32 b": rec[:-1] + "b", "bech32 i": rec[:-1] + "i",
                         "bech32 o": rec[:-1] + "o", "bech32 1": rec[:-1] + "1",
                         "uppercase body": "age1" + rec[4:].upper()}.items():
        cases[f"recipient {label}"] = ("UPDATE keys SET recipient = ? WHERE id = {key}", (value,))
    for label, value in {"uppercase": good_hex.upper(), "non-hex": "g" * 64,
                         "63 chars": "a" * 63}.items():
        cases[f"fingerprint {label}"] = ("UPDATE keys SET fingerprint = ? WHERE id = {key}", (value,))
    for label, value in {"with -": "0" * 31 + "-", "uppercase": "A" * 32, "31 chars": "0" * 31,
                         "33 chars": "0" * 33,
                         "uuid with dashes": "12345678-1234-1234-1234-123456789abc"}.items():
        cases[f"public_id {label}"] = ("UPDATE snapshots SET public_id = ? WHERE id = {snap}", (value,))
    bad_refs = {"space": "a b", "slash": "a/b", "braces": "{{x}}", "uppercase": "Setup:TB",
                "129 chars": "x" * 129, "empty": "", "non-ascii": "é"}
    for table, row in (("key_challenges", "chal"), ("admin_approvals", "appr")):
        for label, value in bad_refs.items():
            cases[f"{table}.target_ref {label}"] = (
                f"UPDATE {table} SET target_ref = ? WHERE id = {{{row}}}", (value,))
    return cases


T5_CASES = _t5_cases()


@pytest.mark.parametrize("case", sorted(T5_CASES))
def test_t5_check_violations_raise(w, case):
    sql, args = T5_CASES[case]
    refused(w.conn, sql.format(**vars(w)), args)


def test_t5_s_shaped_canary_refused_by_every_new_text_column(w):
    rows = {"keys": w.key, "key_challenges": w.chal, "snapshots": w.snap,
            "admin_approvals": w.appr}
    text_columns = 0
    for table, row_id in rows.items():
        for col in w.conn.execute(f"PRAGMA table_info({table})").fetchall():
            if col["type"] != "TEXT":
                continue
            text_columns += 1
            for canary in (S_LIKE, S_LIKE.lower()):
                refused(w.conn, f"UPDATE {table} SET {col['name']} = ? WHERE id = ?",
                        (canary, row_id))
    assert text_columns == 26          # keys 6, key_challenges 7, snapshots 5, admin_approvals 8


# Every timestamp column of the new tables, with the assignments that make an otherwise
# valid row around it, and its valid value. v4 §3.2: the shape CHECK is
# `GLOB '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'`.
TS_COLUMNS = [
    ("keys", "key", "created_at", "", T0),
    ("keys", "key", "updated_at", "", T0),
    ("keys", "key", "activated_at", "", T1),
    ("key_challenges", "chal", "created_at", "", T0),
    ("key_challenges", "chal", "updated_at", "", T0),
    ("key_challenges", "chal", "expires_at", "", EXP),
    ("key_challenges", "chal", "consumed_at", "", T1),
    ("snapshots", "snap", "created_at", "", T0),
    ("snapshots", "snap", "updated_at", "", T0),
    ("snapshots", "snap", "delete_after", ", delete_requested_by = {owner}", HOLD),
    ("admin_approvals", "appr", "created_at", "", T0),
    ("admin_approvals", "appr", "updated_at", "", T0),
    ("admin_approvals", "appr", "decided_at", ", state = 'approved', decided_by = {adm2}", T1),
    ("admin_approvals", "appr", "execute_after",
     ", state = 'approved', decided_by = {adm2}, decided_at = '" + T1 + "'", HOLD),
    ("admin_approvals", "appr", "executed_at",
     ", state = 'approved', decided_by = {adm2}, decided_at = '" + T1 + "'", LATER),
]


@pytest.mark.parametrize("table, row, column, extra, valid", TS_COLUMNS,
                         ids=[f"{t}.{c}" for t, _, c, _, _ in TS_COLUMNS])
def test_t5_timestamp_shape_refused(w, table, row, column, extra, valid):
    sql = f"UPDATE {table} SET {column} = ?{extra.format(**vars(w))} WHERE id = {getattr(w, row)}"
    accepted(w.conn, sql, (valid,))
    # A last-character change keeps every ordering CHECK true, so the shape CHECK alone refuses.
    msg = refused(w.conn, sql, (valid[:-1] + "z",))
    assert "GLOB" in msg, msg
    for bad in (valid.replace("T", " ")[:-1], valid[:-1] + "+00:00", valid[:-1] + ".000Z",
                "yesterday", S_LIKE):
        refused(w.conn, sql, (bad,))


# ---- T-6: two-admin and different-admin rules -------------------------------------------

def test_t6_two_admin_and_different_admin_rules(w):
    c = w.conn
    pending = key(c, w.owner)
    refused(c, "UPDATE keys SET state = 'active', activated_by = ?, activated_at = ? WHERE id = ?",
            (w.owner, T1, pending), match="activated_by <> user_id")
    refused(c, "UPDATE keys SET state = 'active' WHERE id = ?", (pending,),
            match="(state = 'pending') = (activated_by IS NULL)")
    a = w.appr
    refused(c, "UPDATE admin_approvals SET state = 'approved', decided_by = ?, decided_at = ? "
               "WHERE id = ?", (w.adm1, T1, a), match="decided_by <> requested_by")
    for state in ("approved", "rejected"):
        refused(c, "UPDATE admin_approvals SET state = ? WHERE id = ?", (state, a),
                match="decided_by IS NOT NULL")
    refused(c, "UPDATE admin_approvals SET decided_by = ?, decided_at = ? WHERE id = ?",
            (w.adm2, T1, a), match="state <> 'pending' OR decided_by IS NULL")
    refused(c, "UPDATE admin_approvals SET state = 'approved', decided_by = ? WHERE id = ?",
            (w.adm2, a), match="(decided_by IS NULL) = (decided_at IS NULL)")
    refused(c, "UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (T1, a),
            match="executed_at IS NULL OR state = 'approved'")
    c.execute("UPDATE admin_approvals SET state = 'rejected', decided_by = ?, decided_at = ? "
              "WHERE id = ?", (w.adm2, T1, a))
    refused(c, "UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (LATER, a),
            match="executed_at IS NULL OR state = 'approved'")
    b = approval(c, "home_discard", "setup:tb-01", w.adm1)
    c.execute("UPDATE admin_approvals SET state = 'approved', decided_by = ?, decided_at = ?, "
              "execute_after = ? WHERE id = ?", (w.adm2, T1, HOLD, b))
    accepted(c, "UPDATE admin_approvals SET state = 'cancelled' WHERE id = ?", (b,))   # owner cancel


# ---- T-7: uniqueness ------------------------------------------------------------------------

def test_t7_uniqueness(w):
    c = w.conn
    second_active = key(c, w.owner)              # pending is fine: one pending per user
    refused(c, "UPDATE keys SET state = 'active', activated_by = ?, activated_at = ? WHERE id = ?",
            (w.adm2, T1, second_active), match="UNIQUE constraint failed: keys.user_id")
    refused(c, "INSERT INTO keys (user_id, recipient, fingerprint, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?)", (w.owner, _recipient(10 ** 7 + 1), _sha("x"), T0, T0),
            match="UNIQUE constraint failed: keys.user_id")          # a second pending
    dup = c.execute("SELECT recipient, fingerprint FROM keys WHERE id = ?", (w.key,)).fetchone()
    refused(c, "INSERT INTO keys (user_id, recipient, fingerprint, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?)", (w.other, dup["recipient"], _sha("y"), T0, T0),
            match="keys.recipient")
    refused(c, "INSERT INTO keys (user_id, recipient, fingerprint, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?)", (w.other, _recipient(10 ** 7 + 2), dup["fingerprint"], T0, T0),
            match="keys.fingerprint")
    nonce = c.execute("SELECT nonce_sha256 FROM key_challenges WHERE id = ?", (w.chal,)).fetchone()[0]
    refused(c, "INSERT INTO key_challenges (user_id, key_id, purpose, target_ref, nonce_sha256, "
               "expires_at, created_at, updated_at) VALUES (?, ?, 'rotate', 'x', ?, ?, ?, ?)",
            (w.owner, w.key, nonce, EXP, T0, T0), match="key_challenges.nonce_sha256")
    refused(c, "INSERT INTO snapshots (public_id, user_id, setup_id, key_id, created_at, "
               "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (public_id(c, w.snap), w.owner, w.setup, w.key, T0, T0), match="snapshots.public_id")
    refused(c, "INSERT INTO snapshots (public_id, user_id, setup_id, reservation_id, key_id, "
               "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("f" * 32, w.owner, w.setup, w.res, w.key, T0, T0), match="snapshots.reservation_id")
    refused(c, "INSERT INTO admin_approvals (action, target_ref, requested_by, created_at, "
               "updated_at) VALUES ('tripwire_accept', 'sdp-pf1-vf8', ?, ?, ?)", (w.adm2, T0, T0),
            match="admin_approvals.action, admin_approvals.target_ref")
    approval(c, "tripwire_accept", "other-class", w.adm2)           # another target: fine


# ---- T-8: cross-user integrity (composite FK) ----------------------------------------------

def test_t8_composite_fk_refuses_another_users_key(w):
    c = w.conn
    others_key = key(c, w.other, "active", activated_by=w.adm1)
    refused(c, "INSERT INTO key_challenges (user_id, key_id, purpose, target_ref, nonce_sha256, "
               "expires_at, created_at, updated_at) VALUES (?, ?, 'rotate', 'x', ?, ?, ?, ?)",
            (w.owner, others_key, _sha("cross"), EXP, T0, T0), match="FOREIGN KEY")
    refused(c, "INSERT INTO snapshots (public_id, user_id, setup_id, key_id, created_at, "
               "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("e" * 32, w.owner, w.setup, others_key, T0, T0), match="FOREIGN KEY")
    challenge(c, w.other, others_key, "rotate", "x")             # the owner's own pair: fine
    snapshot(c, w.other, w.setup, others_key)


# ---- T-9: single-use challenge CAS (v4 §3.4) ------------------------------------------------

def _cas(c, chal, uid, purpose, target, now):
    return c.execute(CHALLENGE_CAS, {"now": now, "id": chal, "uid": uid, "purpose": purpose,
                                     "target": target}).rowcount


def test_t9_first_consume_wins_and_a_replay_gets_nothing(w):
    target = public_id(w.conn, w.snap)
    assert _cas(w.conn, w.chal, w.owner, "delete", target, T1) == 1
    assert _cas(w.conn, w.chal, w.owner, "delete", target, T1) == 0
    assert w.conn.execute("SELECT consumed_at FROM key_challenges WHERE id = ?",
                          (w.chal,)).fetchone()[0] == T1


@pytest.mark.parametrize("case", ["expired", "at expiry", "wrong purpose", "wrong target",
                                  "wrong user", "key revoked", "key retired"])
def test_t9_challenge_cas_refusals(w, case):
    c, target, now, uid, purpose = w.conn, public_id(w.conn, w.snap), T1, w.owner, "delete"
    if case == "expired":
        now = "2026-11-24T00:10:01Z"
    elif case == "at expiry":
        now = EXP
    elif case == "wrong purpose":
        purpose = "cancel_delete"
    elif case == "wrong target":
        target = "f" * 32
    elif case == "wrong user":
        uid = w.other
    elif case == "key revoked":
        c.execute("UPDATE keys SET state = 'revoked' WHERE id = ?", (w.key,))
    elif case == "key retired":
        c.execute("UPDATE keys SET state = 'retired' WHERE id = ?", (w.key,))
    assert _cas(c, w.chal, uid, purpose, target, now) == 0
    assert c.execute("SELECT consumed_at FROM key_challenges WHERE id = ?",
                     (w.chal,)).fetchone()[0] is None


# ---- T-10, T-11: FK actions ---------------------------------------------------------------

def test_t10_sync_style_deletes_keep_snapshot_and_block_setup_removal(w):
    c = w.conn
    c.execute("DELETE FROM reservations WHERE setup_id = ?", (w.setup,))     # as catalog.py:564
    row = c.execute("SELECT id, reservation_id FROM snapshots WHERE id = ?", (w.snap,)).fetchone()
    assert tuple(row) == (w.snap, None)                                     # kept, SET NULL
    refused(c, "DELETE FROM setups WHERE id = ?", (w.setup,), match="FOREIGN KEY")   # Q-20
    # Control: a setup with a released reservation but no snapshot is removable (catalog.py:564-565).
    s2 = mk_setup(c, "tb-02", ["wire"])
    mk_res(c, s2, w.usecase, T(12), T(13), state="released", user_id=w.owner)
    c.execute("DELETE FROM reservations WHERE setup_id = ?", (s2,))
    c.execute("DELETE FROM setups WHERE id = ?", (s2,))


def test_t11_jobs_snapshot_id_set_null_on_snapshot_delete(w):
    c = w.conn
    jid = models.create_job(c, w.res, "cleanup")
    c.execute("UPDATE jobs SET snapshot_id = ? WHERE id = ?", (w.snap, jid))
    refused(c, "UPDATE jobs SET snapshot_id = 99999 WHERE id = ?", (jid,), match="FOREIGN KEY")
    c.execute("DELETE FROM snapshots WHERE id = ?", (w.snap,))
    assert c.execute("SELECT snapshot_id FROM jobs WHERE id = ?", (jid,)).fetchone()[0] is None


# ---- T-12: the restore kind and the jobs.kind rewrite ----------------------------------------

def test_t12_restore_kind_accepted_bogus_refused_old_kinds_kept(w):
    c = w.conn
    for kind in ("deploy", "cleanup", "health_probe", "reconcile", "restore"):
        models.insert(c, "jobs", {"reservation_id": w.res, "kind": kind})
    refused(c, "INSERT INTO jobs (reservation_id, kind, created_at, updated_at) "
               "VALUES (?, 'bogus', ?, ?)", (w.res, T0, T0), match="'restore'")


# A jobs table shaped like m5's, with a chosen kind CHECK, created before migration 5 runs
# (m5's CREATE TABLE IF NOT EXISTS then keeps it). For the v4.1-3 cases.
JOBS_TEMPLATE = """CREATE TABLE jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL
                   {kind_check},{extra}
    state          TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt        INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    trace_id       TEXT,
    log_path       TEXT,
    steps          TEXT    NOT NULL DEFAULT '[]',
    error          TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
)"""
KIND_CHECKS = {
    "old text 0 times": ("CHECK (kind IN ('deploy','cleanup','health_probe','reconcile'))", ""),
    "old text 2 times": ("CHECK (kind IN (" + OLD_KINDS_TEXT + ")",
                         "\n    kind2 TEXT CHECK (kind2 IN (" + OLD_KINDS_TEXT + "),"),
    "new text present": ("CHECK (kind IN (" + NEW_KINDS_TEXT + ")", ""),
}


def _v13_with_jobs(case, path=":memory:"):
    conn = _conn(path)
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    kind_check, extra = KIND_CHECKS[case]
    conn.execute(JOBS_TEMPLATE.format(kind_check=kind_check, extra=extra))
    for m in migrations.MIGRATIONS[:13]:
        m.fn(conn)
    conn.execute("PRAGMA user_version = 13")
    return conn


@pytest.mark.parametrize("case", ["old text 0 times", "old text 2 times"])
def test_t12_rewrite_refuses_unless_old_kinds_occur_exactly_once(case):
    """v4 §3.8 + v4.1-3: no guessing. The migration fails loudly and rolls back."""
    conn = _v13_with_jobs(case)
    assert _jobs_sql(conn).count(OLD_KINDS_TEXT) == {"old text 0 times": 0,
                                                     "old text 2 times": 2}[case]
    dump = _dump(conn)
    with pytest.raises(RuntimeError, match=REWRITE_ERROR):
        models.init_db(conn)
    assert migrations.current_version(conn) == 13
    assert _dump(conn) == dump                       # no new table, index or column
    assert not conn.in_transaction
    conn.close()


def test_t12_rewrite_skips_when_new_kinds_already_present():
    """v4.1-3: the exact new text present means already rewritten: skip, don't raise."""
    conn = _v13_with_jobs("new text present")
    assert models.init_db(conn) == [14]
    sql = _jobs_sql(conn)
    assert sql.count(NEW_KINDS_TEXT) == 1 and OLD_KINDS_TEXT not in sql
    conn.close()


def test_t12_other_connections_see_restore(tmp_path):
    path = str(tmp_path / "m14.db")
    conn = models.connect(path)
    models.init_db(conn)
    uid = mk_user(conn, "owner")[0]
    sid = mk_setup(conn, "tb-01", ["wire"])
    rid = mk_res(conn, sid, mk_usecase(conn, "uc1", ["wire"]), T(10), T(11), user_id=uid)
    conn.close()
    other = sqlite3.connect(path)                    # a brand-new connection
    other.execute("INSERT INTO jobs (reservation_id, kind, created_at, updated_at) "
                  "VALUES (?, 'restore', ?, ?)", (rid, T0, T0))
    with pytest.raises(sqlite3.IntegrityError):
        other.execute("INSERT INTO jobs (reservation_id, kind, created_at, updated_at) "
                      "VALUES (?, 'bogus', ?, ?)", (rid, T0, T0))
    other.commit()
    other.close()


def test_m14_is_one_transaction(tmp_path, monkeypatch):
    """Q-5: a failure after the rewrite (the last step) rolls back all of 14: no new table,
    index or column, the old jobs.kind CHECK, user_version 13."""
    conn = _at_version(models.connect(str(tmp_path / "v13.db")), 13)
    dump = _dump(conn)
    real = _m14()

    def rewrite_then_explode(c):
        real(c)
        assert _jobs_sql(c).count(NEW_KINDS_TEXT) == 1 and migrations.has_table(c, "snapshots")
        raise RuntimeError("boom")
    monkeypatch.setattr(migrations, "MIGRATIONS", [
        *migrations.MIGRATIONS[:13], migrations.Migration(14, "snapshots_v1", rewrite_then_explode)])
    with pytest.raises(RuntimeError, match="boom"):
        models.init_db(conn)
    assert migrations.current_version(conn) == 13
    assert _dump(conn) == dump
    assert OLD_KINDS_TEXT in _jobs_sql(conn)
    conn.close()


# ---- T-13, T-14: what must never exist -------------------------------------------------------

def test_t13_no_reverted_shapes_after_14():
    conn = _latest()
    assert not migrations.has_column(conn, "jobs", "secret")
    assert not migrations.has_table(conn, "user_keys")
    assert not migrations.has_column(conn, "snapshots", "wrapped_dek")
    assert not migrations.has_column(conn, "snapshots", "path")
    assert not migrations.has_column(conn, "setups", "snapshot_bdfs")
    assert migrations.has_column(conn, "snapshots", "public_id")
    dflt = [r[4] for r in conn.execute("PRAGMA table_info(reservations)")
            if r[1] == "save_snapshot"]
    assert dflt == ["0"]
    migrations.refuse_reverted_schema(conn)                  # no marker M1..M6
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert not {"idx_snapshots_owner", "idx_snapshots_reservation"} & names
    conn.close()


def test_t14_no_key_material_column_names():
    conn = _latest()
    rule = re.compile(r"secret|dek|private|identity|nonce$|password|wrapped")
    for table in NEW_TABLES:
        assert migrations.has_table(conn, table), table          # not vacuous
        for column in _cols(conn, table):
            assert not rule.search(column), (table, column)
    conn.close()


# ---- T-15: delete holds --------------------------------------------------------------------

def test_t15_delete_hold_constraints(w):
    c, s = w.conn, w.snap
    hold = "UPDATE snapshots SET delete_after = ?, delete_requested_by = ? WHERE id = ?"
    accepted(c, hold, (HOLD, w.owner, s))
    refused(c, "UPDATE snapshots SET delete_after = ? WHERE id = ?", (HOLD, s),
            match="(delete_after IS NULL) = (delete_requested_by IS NULL)")
    refused(c, "UPDATE snapshots SET delete_requested_by = ? WHERE id = ?", (w.owner, s),
            match="(delete_after IS NULL) = (delete_requested_by IS NULL)")
    for state in ("capturing", "failed"):
        other = snapshot(c, w.owner, w.setup, w.key, state=state)
        refused(c, hold, (HOLD, w.owner, other), match="delete_after IS NULL OR state = 'saved'")
    refused(c, hold, (T0, w.owner, s), match="delete_after > created_at")
    refused(c, hold, (HOLD, 99999, s), match="FOREIGN KEY")
    c.execute(hold, (HOLD, w.owner, s))
    accepted(c, "UPDATE snapshots SET delete_after = NULL, delete_requested_by = NULL "
                "WHERE id = ?", (s,))                                      # cancel_delete
    refused(c, "UPDATE snapshots SET hold_approval_id = ?, delete_after = NULL, "
               "delete_requested_by = NULL WHERE id = ?", (w.appr, s),
            match="hold_approval_id IS NULL OR delete_after IS NOT NULL")
    refused(c, "UPDATE snapshots SET hold_approval_id = 99999 WHERE id = ?", (s,),
            match="FOREIGN KEY")
    c.execute("UPDATE snapshots SET hold_approval_id = ? WHERE id = ?", (w.appr, s))
    # clearing both hold columns while hold_approval_id is still set: only that CHECK can fail
    refused(c, "UPDATE snapshots SET delete_after = NULL, delete_requested_by = NULL "
               "WHERE id = ?", (s,), match="hold_approval_id IS NULL OR delete_after IS NOT NULL")


# ---- T-16, T-19, T-20: the lost-key reset (v4 §3.3; target_ref key:<id>, v4.1-4) ------------

def _approve_reset(w, key_id):
    """The second admin's approval, one transaction (v4 §3.3 "On approval")."""
    c = w.conn
    c.execute("BEGIN IMMEDIATE")
    aid = approval(c, "lost_key_reset", f"key:{key_id}", w.adm1)
    c.execute("UPDATE admin_approvals SET state = 'approved', decided_by = ?, decided_at = ?, "
              "execute_after = ?, updated_at = ? WHERE id = ?", (w.adm2, T1, HOLD, T1, aid))
    held = c.execute("UPDATE snapshots SET delete_after = ?, delete_requested_by = ?, "
                     "hold_approval_id = ?, updated_at = ? WHERE key_id = ? AND state = 'saved' "
                     "AND delete_after IS NULL", (HOLD, w.adm1, aid, T1, key_id)).rowcount
    c.execute("COMMIT")
    return aid, held


def _execute_reset(w, aid, key_id, now):
    """The end of the hold, one transaction (v4 §3.3 "When the hold ends"). Rama, 24 Nov:
    act only if the key is still active; the snapshots are deleted only if the revoke
    changed a row, otherwise nothing is deleted."""
    c = w.conn
    c.execute("BEGIN IMMEDIATE")
    executed = c.execute(EXECUTION_CAS, {"now": now, "id": aid}).rowcount
    revoked = deleted = 0
    if executed:
        revoked = c.execute("UPDATE keys SET state = 'revoked', updated_at = ? WHERE id = ? "
                            "AND state = 'active'", (now, key_id)).rowcount
    if revoked:
        deleted = c.execute("DELETE FROM snapshots WHERE key_id = ?", (key_id,)).rowcount
    c.execute("COMMIT")
    return executed, revoked, deleted


def _own_held(w):
    sid = snapshot(w.conn, w.owner, w.setup, w.key)
    w.conn.execute("UPDATE snapshots SET delete_after = ?, delete_requested_by = ? WHERE id = ?",
                   (OWN_HOLD, w.owner, sid))
    return sid


def test_t16_lost_key_reset_hold_then_execute(w):
    c, k, a_snap = w.conn, w.key, w.snap
    a_pid = public_id(c, a_snap)
    b_snap = _own_held(w)
    aid, held = _approve_reset(w, k)
    assert held == 1 and key_state(c, k) == "active"                # the key stays active
    assert hold_of(c, a_snap) == (HOLD, w.adm1, aid)
    assert hold_of(c, b_snap) == (OWN_HOLD, w.owner, None)            # own hold kept
    assert c.execute("SELECT execute_after FROM admin_approvals WHERE id = ?",
                     (aid,)).fetchone()[0] == hold_of(c, a_snap)[0]   # the same instant
    # during the hold
    assert _cas(c, w.chal, w.owner, "delete", a_pid, T1) == 1
    new_key = key(c, w.owner)                                         # enrol as pending: fine
    refused(c, "UPDATE keys SET state = 'active', activated_by = ?, activated_at = ? WHERE id = ?",
            (w.adm2, T1, new_key), match="UNIQUE constraint failed: keys.user_id")
    late = challenge(c, w.owner, k, "delete", a_pid,
                     created="2026-11-25T00:00:00Z", expires="2026-11-25T00:10:00Z")
    assert _execute_reset(w, aid, k, T1) == (0, 0, 0)                 # before execute_after
    assert key_state(c, k) == "active"
    # the hold ends
    assert _execute_reset(w, aid, k, LATER) == (1, 1, 2)
    assert key_state(c, k) == "revoked"
    assert c.execute("SELECT executed_at FROM admin_approvals WHERE id = ?",
                     (aid,)).fetchone()[0] == LATER
    assert c.execute("SELECT count(*) FROM snapshots WHERE key_id = ?", (k,)).fetchone()[0] == 0
    assert _cas(c, late, w.owner, "delete", a_pid, LATER) == 0       # dies with the key
    assert _execute_reset(w, aid, k, LATER) == (0, 0, 0)              # single use
    accepted(c, "UPDATE keys SET state = 'active', activated_by = ?, activated_at = ? "
                "WHERE id = ?", (w.adm2, LATER, new_key), keep=True)
    third = key(c, w.owner)
    refused(c, "UPDATE keys SET state = 'revoked' WHERE id = ?", (third,),
            match="(state = 'pending') = (activated_by IS NULL)")     # pending -> revoked


def test_t19_owner_cancels_reset_during_hold(w):
    c, k, a_snap = w.conn, w.key, w.snap
    b_snap = _own_held(w)
    aid, _ = _approve_reset(w, k)
    cancel = challenge(c, w.owner, k, "cancel_delete", f"approval:{aid}")
    c.execute("BEGIN IMMEDIATE")
    consumed = _cas(c, cancel, w.owner, "cancel_delete", f"approval:{aid}", T1)
    cancelled = c.execute("UPDATE admin_approvals SET state = 'cancelled', updated_at = ? "
                          "WHERE id = ? AND state = 'approved' AND executed_at IS NULL",
                          (T1, aid)).rowcount
    cleared = c.execute("UPDATE snapshots SET delete_after = NULL, delete_requested_by = NULL, "
                        "hold_approval_id = NULL, updated_at = ? WHERE hold_approval_id = ?",
                        (T1, aid)).rowcount
    c.execute("COMMIT")
    assert (consumed, cancelled, cleared) == (1, 1, 1)
    assert hold_of(c, a_snap) == (None, None, None)
    assert hold_of(c, b_snap) == (OWN_HOLD, w.owner, None)
    assert key_state(c, k) == "active"
    assert _execute_reset(w, aid, k, LATER) == (0, 0, 0)              # cancelled: never runs
    assert key_state(c, k) == "active" and hold_of(c, a_snap) is not None


def test_t20_execution_deletes_every_snapshot_of_the_revoked_key(w):
    c, k = w.conn, w.key
    b_snap = _own_held(w)
    aid, _ = _approve_reset(w, k)
    during = snapshot(c, w.owner, w.setup, k, created=T1)             # captured during the hold
    assert hold_of(c, during) == (None, None, None)
    unrelated = snapshot(c, w.other, w.setup, key(c, w.other, "active", activated_by=w.adm1))
    assert _execute_reset(w, aid, k, LATER) == (1, 1, 3)
    for sid in (w.snap, b_snap, during):
        assert hold_of(c, sid) is None
    assert c.execute("SELECT count(*) FROM snapshots WHERE key_id = ?", (k,)).fetchone()[0] == 0
    assert hold_of(c, unrelated) == (None, None, None)


# ---- T-17: execute_after -------------------------------------------------------------------

def test_t17_execute_after_rules(w):
    c = w.conn
    b = approval(c, "home_discard", "setup:tb-01", w.adm1)                # v4.1-5 target
    approve = ("UPDATE admin_approvals SET state = 'approved', decided_by = ?, decided_at = ?, "
               "execute_after = ? WHERE id = ?")
    refused(c, approve, (w.adm2, T1, T0, b), match="execute_after > created_at")
    c.execute(approve, (w.adm2, T1, HOLD, b))
    refused(c, "UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (T1, b),
            match="executed_at >= execute_after")
    accepted(c, "UPDATE admin_approvals SET executed_at = ? WHERE id = ?", (HOLD, b))
    assert c.execute(EXECUTION_CAS, {"now": T1, "id": b}).rowcount == 0
    assert c.execute(EXECUTION_CAS, {"now": HOLD, "id": b}).rowcount == 1
    # tripwire: no hold, so it executes at once
    c.execute("UPDATE admin_approvals SET state = 'approved', decided_by = ?, decided_at = ? "
              "WHERE id = ?", (w.adm2, T1, w.appr))
    assert c.execute(EXECUTION_CAS, {"now": T1, "id": w.appr}).rowcount == 1


# ---- T-21: retention (v4 §3.5) ---------------------------------------------------------------

def test_t21_retention_prune_ignores_held_rows(w):
    c, k = w.conn, w.key
    c.execute("DELETE FROM snapshots")
    held = snapshot(c, w.owner, w.setup, k, created="2026-11-24T01:00:00Z")
    c.execute("UPDATE snapshots SET delete_after = ?, delete_requested_by = ? WHERE id = ?",
              ("2026-11-26T00:00:00Z", w.owner, held))
    unheld = [snapshot(c, w.owner, w.setup, k, created=f"2026-11-24T0{h}:00:00Z")
              for h in (2, 3, 4, 5)]
    failed = snapshot(c, w.owner, w.setup, k, state="failed", created="2026-11-23T00:00:00Z")
    other_setup = snapshot(c, w.owner, mk_setup(c, "tb-02", ["wire"]), k,
                           created="2026-11-23T00:00:00Z")
    assert c.execute(RETENTION_PRUNE, {"uid": w.owner, "sid": w.setup}).rowcount == 1
    left = {r[0] for r in c.execute("SELECT id FROM snapshots")}
    assert left == {held, *unheld[1:], failed, other_setup}
    assert c.execute(RETENTION_PRUNE, {"uid": w.owner, "sid": w.setup}).rowcount == 0


# ---- extras: integrity --------------------------------------------------------------------------

def test_m14_integrity_and_foreign_key_check_clean():
    conn = _at_version(_conn(), 11)
    uid = mk_user(conn, "legacy")[0]
    sid = mk_setup(conn, "legacy-tb", ["wire"])
    rid = mk_res(conn, sid, mk_usecase(conn, "legacy-uc", ["wire"]), T(10), T(11), user_id=uid)
    models.create_job(conn, rid, "deploy")
    assert models.init_db(conn) == [12, 13, 14]
    _world(conn)
    assert conn.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


# ---- v4.1-1: one open approval per (action, target), pending or approved-unexecuted ---------

@pytest.mark.parametrize("first_ends", ["executed", "cancelled"])
def test_v41_1_second_reset_refused_while_first_is_approved_and_unexecuted(w, first_ends):
    c, k = w.conn, w.key
    aid, _ = _approve_reset(w, k)
    second = ("INSERT INTO admin_approvals (action, target_ref, requested_by, created_at, "
              "updated_at) VALUES ('lost_key_reset', ?, ?, ?, ?)")
    refused(c, second, (f"key:{k}", w.adm2, T1, T1),
            match="UNIQUE constraint failed: admin_approvals.action, admin_approvals.target_ref")
    if first_ends == "executed":
        assert _execute_reset(w, aid, k, LATER)[0] == 1
    else:
        c.execute("UPDATE admin_approvals SET state = 'cancelled' WHERE id = ?", (aid,))
    accepted(c, second, (f"key:{k}", w.adm2, T1, T1))


# ---- v4.1-4: execution acts only if the key under reset is still active (Rama, 24 Nov) ------

@pytest.mark.parametrize("key_now", ["retired", "revoked"])
def test_v41_4_execution_when_key_no_longer_active_deletes_nothing(w, key_now):
    c, k = w.conn, w.key
    _own_held(w)
    aid, held = _approve_reset(w, k)
    assert held == 1
    c.execute("UPDATE keys SET state = ?, updated_at = ? WHERE id = ?", (key_now, T1, k))
    snaps = lambda: [tuple(r) for r in c.execute(                       # noqa: E731
        "SELECT id, state, delete_after, delete_requested_by, hold_approval_id FROM snapshots "
        "WHERE key_id = ? ORDER BY id", (k,))]
    before = snaps()
    assert len(before) == 2
    executed, revoked, deleted = _execute_reset(w, aid, k, LATER)
    assert executed in (0, 1)                        # the CAS may succeed
    assert (revoked, deleted) == (0, 0)
    assert snaps() == before                         # every snapshot of K is still there
    assert key_state(c, k) == key_now


# ---- v4.1-2: activated_by and activated_at are set together ----------------------------------

@pytest.mark.parametrize("case", ["activated_by without activated_at",
                                  "activated_at without activated_by"])
def test_v41_2_keys_activation_pair(w, case):
    pending = key(w.conn, w.owner)
    sql = {"activated_by without activated_at":
           "UPDATE keys SET state = 'active', activated_by = ? WHERE id = ?",
           "activated_at without activated_by":
           "UPDATE keys SET activated_at = ? WHERE id = ?"}[case]
    arg = w.adm1 if case.startswith("activated_by") else T1
    refused(w.conn, sql, (arg, pending), match="(activated_by IS NULL) = (activated_at IS NULL)")
