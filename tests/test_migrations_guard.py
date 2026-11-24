"""B-104 PR A (B-104-DDL.md v4 §1, §2, §6.1, §6.2 T-1/T-2): versions 12 and 13 are burned
no-op placeholders (errata E-4), and `migrations.apply()` refuses a DB that carries schema
from the reverted Phase 8 attempt (4ec115e, reverted in 9c8a089) before running anything.
Updated in PR B, where migration 14 makes latest_version() 14: nothing here assumes a fixed
latest; G-7/G-8 allow only the closed list of migration-14 additions; the engine tests cover
a reverted DB at the latest version (the guard refuses) and below it (the version check does).

Synthetic DBs only (`:memory:` or `tmp_path`). The reverted v12/v13 shapes are literal DDL
copied from the fingerprint in B-104 FINDINGS §3; no reverted code is imported."""
import dataclasses
import sqlite3

import pytest

from service import cli, config, engine, migrations, models

# ---- the reverted fingerprint (FINDINGS §3), as literal DDL ------------------

# v12 `_m12_snapshots` (4ec115e:service/migrations.py:258-307)
REVERTED_USER_KEYS = """CREATE TABLE user_keys (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id),
    public_key          TEXT    NOT NULL,
    wrapped_private_key TEXT    NOT NULL,
    kdf_salt            TEXT    NOT NULL,
    key_version         INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
)"""
REVERTED_SNAPSHOTS = """CREATE TABLE snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    setup_id       INTEGER NOT NULL REFERENCES setups(id),
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    usecase_id     INTEGER NOT NULL REFERENCES usecases(id),
    hw_class       TEXT,
    state          TEXT    NOT NULL DEFAULT 'saving'
                   CHECK (state IN ('saving', 'saved', 'failed', 'deleted')),
    path           TEXT,
    size_bytes     INTEGER,
    sha256         TEXT,
    wrapped_dek    TEXT,
    header         TEXT    NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
)"""
REVERTED_V12 = [
    REVERTED_USER_KEYS,
    REVERTED_SNAPSHOTS,
    "CREATE INDEX idx_snapshots_owner ON snapshots(user_id, setup_id, created_at)",
    "CREATE INDEX idx_snapshots_reservation ON snapshots(reservation_id)",
    "ALTER TABLE reservations ADD COLUMN save_snapshot INTEGER NOT NULL DEFAULT 1"
    " CHECK (save_snapshot IN (0, 1))",
    "ALTER TABLE setups ADD COLUMN snapshot_bdfs TEXT NOT NULL DEFAULT '[]'",
]
# v13 `_m13_restore_jobs` (4ec115e:service/migrations.py:310-339): the jobs.kind CHECK
# rewritten in place (the m6 method), plus two columns.
REVERTED_KIND_OLD = "'deploy', 'cleanup', 'health_probe', 'reconcile')"
REVERTED_KIND_NEW = "'deploy', 'cleanup', 'health_probe', 'reconcile', 'restore')"
REVERTED_V13_COLUMNS = [
    "ALTER TABLE jobs ADD COLUMN snapshot_id INTEGER REFERENCES snapshots(id)",
    "ALTER TABLE jobs ADD COLUMN secret TEXT",
]

ALL_LABELS = [
    "table user_keys",
    "column jobs.secret",
    "table snapshots without public_id",
    "column snapshots.wrapped_dek",
    "column setups.snapshot_bdfs",
    "reservations.save_snapshot default 1",
]
V12_LABELS = [lbl for lbl in ALL_LABELS if lbl != "column jobs.secret"]   # M1 M3 M4 M5 M6

# A snapshots table that has public_id, so M4 can be tested without M3.
SNAPSHOTS_WITH_PUBLIC_ID_AND_DEK = """CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL, wrapped_dek TEXT)"""
SNAPSHOTS_WITHOUT_PUBLIC_ID = """CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL)"""

MARKER_ALONE = {      # G-1a…f: each marker M1…M6 alone on an otherwise clean v11 DB
    "M1": ([REVERTED_USER_KEYS], "table user_keys"),
    "M2": (["ALTER TABLE jobs ADD COLUMN secret TEXT"], "column jobs.secret"),
    "M3": ([SNAPSHOTS_WITHOUT_PUBLIC_ID], "table snapshots without public_id"),
    "M4": ([SNAPSHOTS_WITH_PUBLIC_ID_AND_DEK], "column snapshots.wrapped_dek"),
    "M5": (["ALTER TABLE setups ADD COLUMN snapshot_bdfs TEXT NOT NULL DEFAULT '[]'"],
           "column setups.snapshot_bdfs"),
    "M6": (["ALTER TABLE reservations ADD COLUMN save_snapshot INTEGER NOT NULL DEFAULT 1"
            " CHECK (save_snapshot IN (0, 1))"], "reservations.save_snapshot default 1"),
}

CANARY_USER = "canary-user-7f3a9c"
CANARY_VALUES = ["canary-pubkey-b81e", "canary-wrapped-key-55d0", "canary-salt-0c2f",
                 "canary-dek-9e41", "canary-secret-a6b7", "canary-path-31fd",
                 "canary-sha-e2c8", "canary-bdf-0000:17:00.0"]


# ---- DB builders -------------------------------------------------------------

def _conn(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _at_version(conn, version):
    """Baseline + migrations 1..version run directly, user_version = version (the
    test_jobs.py / test_reservation_states.py pattern)."""
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    for m in migrations.MIGRATIONS[:version]:
        m.fn(conn)
    conn.execute(f"PRAGMA user_version = {int(version)}")
    assert migrations.current_version(conn) == version
    return conn


def _v11(conn=None):
    return _at_version(conn or _conn(), 11)


def _rewrite_jobs_kind_check(conn):
    """The reverted v13's in-place CHECK rewrite (the m6 method), done literally here."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()[0]
    assert sql.count(REVERTED_KIND_OLD) == 1
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'jobs'",
                 (sql.replace(REVERTED_KIND_OLD, REVERTED_KIND_NEW, 1),))
    conn.execute(f"PRAGMA schema_version = {int(version) + 1}")
    conn.execute("PRAGMA writable_schema = OFF")


def _reverted_v12(conn=None):
    conn = _v11(conn)
    for ddl in REVERTED_V12:
        conn.execute(ddl)
    conn.execute("PRAGMA user_version = 12")
    return conn


def _reverted_v13(conn=None):
    conn = _reverted_v12(conn)
    _rewrite_jobs_kind_check(conn)
    for ddl in REVERTED_V13_COLUMNS:
        conn.execute(ddl)
    conn.execute("PRAGMA user_version = 13")
    return conn


def _seed(conn):
    """One row per core table; returns the ids. Works on a v11 or later DB."""
    uid = models.insert(conn, "users", {"username": "u1", "token_hash": "h", "ssh_pubkey": "k"})
    pid = models.insert(conn, "pools", {"name": "lab-1"})
    sid = models.insert(conn, "setups", {
        "name": "s1", "pool_id": pid, "capabilities": "[]", "links": "[]",
        "diagram": "graph LR\n  a --> b", "yaml_path": "setups/s1.yml"})
    cid = models.insert(conn, "usecases", {"name": "uc1", "yaml_path": "usecases/uc1.yml"})
    rid = models.insert(conn, "reservations", {
        "user_id": uid, "usecase_id": cid, "setup_id": sid, "state": "released",
        "requested_start_at": "2026-09-01T10:00:00Z", "start_at": "2026-09-01T10:00:00Z",
        "end_at": "2026-09-01T12:00:00Z"})
    jid = models.insert(conn, "jobs", {"reservation_id": rid, "kind": "cleanup"})
    return {"user": uid, "setup": sid, "usecase": cid, "reservation": rid, "job": jid}


def _dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _rows(conn):
    """Every row of every table, for "data unchanged" checks across connections."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in tables}


def _state(conn):
    return _dump(conn), conn.total_changes, migrations.current_version(conn)


def _assert_refused(conn, expected_labels, call=None):
    """apply() (or `call`) raises RevertedSchema naming exactly `expected_labels`, and the
    DB is untouched: same sqlite_master, total_changes and user_version."""
    before = _state(conn)
    with pytest.raises(migrations.RevertedSchema) as exc:
        (call or migrations.apply)(conn)
    assert _state(conn) == before
    assert not conn.in_transaction
    msg = str(exc.value)
    for label in ALL_LABELS:
        assert (label in msg) == (label in expected_labels), (label, msg)
    return msg


# ---- G-1…G-5, G-9: refused ------------------------------------------------

@pytest.mark.parametrize("marker", sorted(MARKER_ALONE))
def test_g1_each_marker_alone_is_refused(marker):
    """G-1a…f: one marker on an otherwise clean v11 DB is enough."""
    ddls, label = MARKER_ALONE[marker]
    conn = _v11()
    for ddl in ddls:
        conn.execute(ddl)
    _assert_refused(conn, [label])


def test_g2_reverted_v12_is_refused():
    conn = _reverted_v12()
    assert migrations.current_version(conn) == 12
    _assert_refused(conn, V12_LABELS)
    _assert_refused(conn, V12_LABELS, call=models.init_db)


def test_g3_reverted_v13_is_refused():
    """G-3, the trap: a reverted v13 DB reports user_version 13. Without the guard, apply()
    would skip the burned 12/13 and run only 14, whose IF NOT EXISTS / add_column would
    silently keep the reverted snapshots table and save_snapshot DEFAULT 1 (and while 13
    was the latest version, it would run nothing and accept the DB)."""
    conn = _reverted_v13()
    assert migrations.current_version(conn) == 13
    _assert_refused(conn, ALL_LABELS)
    _assert_refused(conn, ALL_LABELS, call=models.init_db)


@pytest.mark.parametrize("version", [0, 11, 12, "latest"])
def test_g4_refusal_ignores_user_version(version):
    """G-4: reverted v13 tables with user_version hand-set to any value <= latest."""
    conn = _reverted_v13()
    v = migrations.latest_version() if version == "latest" else version
    conn.execute(f"PRAGMA user_version = {int(v)}")
    _assert_refused(conn, ALL_LABELS)


def test_g5_message_has_fixed_labels_only(tmp_path):
    """G-5: no usernames, row values, path, SQL text or user_version in the message."""
    path = str(tmp_path / "canary-dir-4d19" / "canary-db-8b2e.db")
    (tmp_path / "canary-dir-4d19").mkdir()
    conn = _reverted_v13(models.connect(path))
    ids = _seed(conn)
    v = CANARY_VALUES
    conn.execute("UPDATE users SET username = ?, ssh_pubkey = ? WHERE id = ?",
                 (CANARY_USER, v[0], ids["user"]))
    conn.execute("INSERT INTO user_keys VALUES (?, ?, ?, ?, 1, ?, ?)",
                 (ids["user"], v[0], v[1], v[2], "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"))
    conn.execute(
        "INSERT INTO snapshots (user_id, setup_id, reservation_id, usecase_id, state, path,"
        " sha256, wrapped_dek, created_at, updated_at) VALUES (?, ?, ?, ?, 'saved', ?, ?, ?, ?, ?)",
        (ids["user"], ids["setup"], ids["reservation"], ids["usecase"], v[5], v[6], v[3],
         "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"))
    conn.execute("UPDATE jobs SET secret = ?, kind = 'restore' WHERE id = ?", (v[4], ids["job"]))
    conn.execute("UPDATE setups SET snapshot_bdfs = ? WHERE id = ?", (f'["{v[7]}"]', ids["setup"]))
    conn.execute("PRAGMA user_version = 13")
    msg = _assert_refused(conn, ALL_LABELS)
    for canary in [CANARY_USER, *CANARY_VALUES, "canary-dir-4d19", "canary-db-8b2e", path]:
        assert canary not in msg
    for forbidden in ["CREATE", "SELECT", "PRAGMA", "user_version", "v13", "13"]:
        assert forbidden not in msg, forbidden
    assert msg.startswith("database has schema from the reverted Phase 8 snapshot attempt (")
    assert msg.endswith("refusing to migrate it. Move this DB aside and start a fresh one "
                        "with `daolab init`, or ask an admin. Do not edit it by hand.")


def _cli_settings(monkeypatch, tmp_path):
    path = str(tmp_path / "daolab.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, db_path=path))
    return path


def test_g9_cli_init_refuses_reverted_db(monkeypatch, tmp_path, capsys):
    """G-9: `daolab init` on a reverted on-disk DB exits 1 with the message, no traceback,
    and leaves the DB as it was."""
    path = _cli_settings(monkeypatch, tmp_path)
    conn = _reverted_v13(models.connect(path))
    _seed(conn)
    before = (_dump(conn), _rows(conn), migrations.current_version(conn))
    conn.close()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_init(None)
    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: database has schema from the reverted Phase 8 snapshot attempt")
    assert "Traceback" not in err and "RevertedSchema" not in err
    for label in ALL_LABELS:
        assert label in err
    conn = models.connect(path)
    assert (_dump(conn), _rows(conn), migrations.current_version(conn)) == before
    conn.close()


# ---- G-6…G-8, G-10, G-11: passes ------------------------------------------

def test_g6_guard_passes_on_our_own_latest_schema():
    """G-6: a fresh DB applies every migration (1..latest), and the guard passes on the
    result. Pins the §2.1 invariant for every later migration."""
    conn = _conn()
    assert models.init_db(conn) == list(range(1, migrations.latest_version() + 1))
    migrations.refuse_reverted_schema(conn)
    assert models.init_db(conn) == []


# The closed list of what an upgrade across migration 14 may add (B-104-DDL.md v4 §3). Literal,
# not derived from service code: migration 14 is the first non-no-op upgrade; only these
# additions are allowed. New column -> the value every pre-existing row must hold.
M14_NEW_TABLES = {"keys", "key_challenges", "admin_approvals", "snapshots"}
M14_NEW_COLUMNS = {("reservations", "save_snapshot"): 0,
                   ("jobs", "snapshot_id"): None,
                   ("setups", "snapshot_enabled"): 0}


def _snapshot(conn):
    """{table: (old-column table_info as (name, type, notnull, dflt_value, pk),
    rows as (rowid, *columns) ORDER BY rowid)} for every table."""
    out = {}
    for t in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]:
        info = [tuple(r)[1:6] for r in conn.execute(f"PRAGMA table_info({t})")]
        cols = ", ".join(f'"{c[0]}"' for c in info)
        out[t] = (info, [tuple(r) for r in conn.execute(
            f"SELECT rowid, {cols} FROM {t} ORDER BY rowid")])
    return out


def _assert_upgrade_kept_everything(conn, before):
    """Strict before/after check with a closed list of allowed changes:
    1. every old table: identical table_info for its old columns, and the same rows
       (ORDER BY rowid) with identical values in every old column;
    2. new tables: exactly M14_NEW_TABLES, each empty;
    3. new columns: exactly M14_NEW_COLUMNS, each holding its value on every row."""
    after_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert after_tables - set(before) == M14_NEW_TABLES
    for t in M14_NEW_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    for t, (info, rows) in before.items():
        assert t in after_tables, t
        old = [c[0] for c in info]
        after_info = [tuple(r)[1:6] for r in conn.execute(f"PRAGMA table_info({t})")]
        assert [c for c in after_info if c[0] in old] == info, t
        cols = ", ".join(f'"{c}"' for c in old)
        assert [tuple(r) for r in conn.execute(
            f"SELECT rowid, {cols} FROM {t} ORDER BY rowid")] == rows, t
        new_cols = {c[0] for c in after_info} - set(old)
        assert new_cols == {c for (tt, c) in M14_NEW_COLUMNS if tt == t}, t
        for c in new_cols:
            assert rows, (t, c, "no pre-existing row to check the new column on")
            assert [r[0] for r in conn.execute(f'SELECT "{c}" FROM {t} ORDER BY rowid')] \
                == [M14_NEW_COLUMNS[(t, c)]] * len(rows), (t, c)


def test_g7_v11_db_with_rows_passes_and_upgrades():
    """G-7: a v11 DB with rows upgrades to latest and keeps every row; the only changes are
    the closed list M14_NEW_TABLES / M14_NEW_COLUMNS."""
    conn = _v11()
    _seed(conn)
    before = _snapshot(conn)
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS if m.version > 11]
    assert migrations.current_version(conn) == migrations.latest_version()
    _assert_upgrade_kept_everything(conn, before)


@pytest.mark.parametrize("stopped_at", [12, 13])
def test_g8_new_code_db_stopped_at_placeholder_resumes(stopped_at):
    """G-8: a crash after the no-op 12 or 13 is a legitimate state, not a reverted DB. It
    resumes, keeps every row, and changes only the closed list M14_NEW_TABLES / M14_NEW_COLUMNS."""
    conn = _at_version(_conn(), stopped_at)
    _seed(conn)
    before = _snapshot(conn)
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS
                                    if m.version > stopped_at]
    assert migrations.current_version(conn) == migrations.latest_version()
    _assert_upgrade_kept_everything(conn, before)


def _spy_guard(monkeypatch):
    calls = []
    real = migrations.refuse_reverted_schema

    def spy(conn):
        calls.append(conn)
        return real(conn)
    monkeypatch.setattr(migrations, "refuse_reverted_schema", spy)
    return calls


def test_g10_guard_runs_on_every_call_even_when_up_to_date(monkeypatch):
    conn = _conn()
    models.init_db(conn)
    calls = _spy_guard(monkeypatch)
    before = _state(conn)
    assert models.init_db(conn) == []
    assert calls == [conn]
    assert _state(conn) == before


def test_g10b_guard_runs_before_any_migration(monkeypatch):
    """The guard runs once, before the loop: a refused DB gets no migration at all."""
    conn = _reverted_v13()
    conn.execute("PRAGMA user_version = 0")
    ran = []
    monkeypatch.setattr(migrations, "MIGRATIONS", [
        migrations.Migration(m.version, m.name, lambda c, v=m.version: ran.append(v))
        for m in migrations.MIGRATIONS])
    calls = _spy_guard(monkeypatch)
    _assert_refused(conn, ALL_LABELS)
    assert ran == [] and calls == [conn]


def test_g11_too_new_db_still_raises_schema_too_new(monkeypatch):
    """G-11: SchemaTooNew stays first, even on a DB that also has every reverted marker,
    and the two exceptions are unrelated (Q-4: SchemaTooNew is not reparented)."""
    conn = _reverted_v13()
    conn.execute(f"PRAGMA user_version = {migrations.latest_version() + 1}")
    calls = _spy_guard(monkeypatch)
    with pytest.raises(migrations.SchemaTooNew) as exc:
        migrations.apply(conn)
    assert not isinstance(exc.value, migrations.RevertedSchema)
    assert calls == []
    assert not issubclass(migrations.RevertedSchema, migrations.SchemaTooNew)
    assert not issubclass(migrations.SchemaTooNew, migrations.RevertedSchema)
    assert migrations.SchemaTooNew.__bases__ == (Exception,)
    assert migrations.RevertedSchema.__bases__ == (Exception,)


def test_markers_are_a_closed_list_of_fixed_labels():
    """§2.1/§2.3: exactly the six markers, in order, each a (fixed label, check) pair."""
    assert [label for label, _ in migrations._REVERTED_MARKERS] == ALL_LABELS
    assert all(isinstance(label, str) and callable(check)
               for label, check in migrations._REVERTED_MARKERS)


def test_m6_marker_is_only_default_1():
    """M6 is about the inverted PRESERVE default: a save_snapshot column with default 0 is
    not a marker (migration 14 adds exactly that)."""
    conn = _v11()
    conn.execute("ALTER TABLE reservations ADD COLUMN save_snapshot INTEGER NOT NULL DEFAULT 0"
                 " CHECK (save_snapshot IN (0, 1))")
    migrations.refuse_reverted_schema(conn)
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS if m.version > 11]


# ---- T-1, T-2: the placeholders -------------------------------------------

def test_t1_fresh_db_applies_placeholders_12_and_13():
    conn = _conn()
    applied = models.init_db(conn)
    assert 12 in applied and 13 in applied
    assert migrations.current_version(conn) == migrations.latest_version() >= 13
    by_version = {m.version: m for m in migrations.MIGRATIONS}
    assert by_version[12].name == "burned_reverted_v12"
    assert by_version[13].name == "burned_reverted_v13"
    assert [m.version for m in migrations.MIGRATIONS] == list(range(1, len(migrations.MIGRATIONS) + 1))


def test_t2_placeholders_change_nothing_but_user_version(monkeypatch):
    """T-2: the dump before and after 12 and 13 is equal except user_version."""
    conn = _v11()
    _seed(conn)
    before = (_dump(conn), _rows(conn), conn.total_changes)
    for version in (12, 13):
        m = migrations.MIGRATIONS[version - 1]
        assert m.version == version
        assert m.fn(conn) is None
        assert (_dump(conn), _rows(conn), conn.total_changes) == before
    assert migrations.current_version(conn) == 11
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:13])
    assert migrations.apply(conn) == [12, 13]
    assert migrations.current_version(conn) == 13
    assert (_dump(conn), _rows(conn), conn.total_changes) == before


# ---- engine startup runs the same guard (Q-3 (b)) -------------------------
# A reverted DB whose user_version equals latest_version() passes the engine's
# `have == want` check (engine.py:629-633), so the engine must also run the guard. A reverted
# DB below latest (e.g. a reverted v13 DB at 13, with latest 14) is refused by the version
# check first, before the guard runs.

def _engine_settings(monkeypatch, tmp_path):
    path = str(tmp_path / "engine.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, db_path=path, runner="mock", mock_step_sec=0.0, mock_fail=""))
    return path


def _spy_tick(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "tick", lambda *a, **k: calls.append(a) or "spied")
    return calls


def test_engine_refuses_reverted_schema_at_latest_version(monkeypatch, tmp_path, capsys):
    """Reverted v13 tables with user_version hand-set to latest: the version check passes,
    so only the engine's guard call refuses it."""
    path = _engine_settings(monkeypatch, tmp_path)
    conn = _reverted_v13(models.connect(path))
    _seed(conn)
    conn.execute(f"PRAGMA user_version = {migrations.latest_version()}")
    assert migrations.current_version(conn) == migrations.latest_version()
    before = (_dump(conn), _rows(conn), migrations.current_version(conn))
    conn.close()
    ticks = _spy_tick(monkeypatch)
    assert engine.main([]) == 1
    out, err = capsys.readouterr()
    assert ticks == [] and "tick done" not in out
    assert err.startswith("error: database has schema from the reverted Phase 8 snapshot attempt (")
    assert err.rstrip("\n").endswith("Do not edit it by hand.")
    assert "Traceback" not in err and "RevertedSchema" not in err and path not in err
    for label in ALL_LABELS:
        assert label in err
    conn = models.connect(path)
    assert (_dump(conn), _rows(conn), migrations.current_version(conn)) == before
    conn.close()


def test_engine_starts_on_clean_fully_migrated_db_and_runs_the_guard(monkeypatch, tmp_path,
                                                                     capsys):
    path = _engine_settings(monkeypatch, tmp_path)
    conn = models.connect(path)
    models.init_db(conn)
    conn.close()
    calls = _spy_guard(monkeypatch)
    assert engine.main([]) == 0
    out, err = capsys.readouterr()
    assert "tick done" in out and err == ""
    assert len(calls) == 1


@pytest.mark.parametrize("build, have", [(_v11, 11), (_reverted_v12, 12), (_reverted_v13, 13)])
def test_engine_still_refuses_a_version_mismatch_first(monkeypatch, tmp_path, capsys,
                                                       build, have):
    """A DB behind latest gets the version message, before the guard runs, and is left
    unchanged. Includes a reverted v13 DB at user_version 13 (< latest since migration 14)."""
    path = _engine_settings(monkeypatch, tmp_path)
    conn = build(models.connect(path))
    assert migrations.current_version(conn) == have
    before = (_dump(conn), _rows(conn), migrations.current_version(conn))
    conn.close()
    calls, ticks = _spy_guard(monkeypatch), _spy_tick(monkeypatch)
    assert engine.main([]) == 1
    err = capsys.readouterr().err
    assert err == (f"error: database schema is v{have} but this checkout expects "
                   f"v{migrations.latest_version()}; run `daolab init` first\n")
    assert calls == [] and ticks == []
    conn = models.connect(path)
    assert (_dump(conn), _rows(conn), migrations.current_version(conn)) == before
    conn.close()
