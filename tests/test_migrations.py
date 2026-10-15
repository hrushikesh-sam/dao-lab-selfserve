"""B-03: `daolab init` is idempotent and upgrades an existing DB without losing data."""
import dataclasses
import sqlite3

import pytest

from service import cli, config, migrations, models


def _conn(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _legacy_db():
    """A DB as `daolab init` left it before B-03: baseline only, user_version 0."""
    conn = _conn()
    with open(models.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    assert migrations.current_version(conn) == 0
    return conn


def _dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _seed_reservation(conn):
    uid = models.insert(conn, "users", {"username": "u1", "token_hash": "h", "ssh_pubkey": "k"})
    pid = models.insert(conn, "pools", {"name": "lab-1"})
    sid = models.insert(conn, "setups", {
        "name": "s1", "pool_id": pid, "capabilities": "[]", "links": "[]",
        "diagram": "graph LR\n  a --> b", "yaml_path": "setups/s1.yml"})
    cid = models.insert(conn, "usecases", {"name": "uc1", "yaml_path": "usecases/uc1.yml"})
    return models.insert(conn, "reservations", {
        "user_id": uid, "usecase_id": cid, "setup_id": sid, "state": "queued",
        "requested_start_at": "2026-09-01T10:00:00Z", "start_at": "2026-09-01T10:00:00Z",
        "end_at": "2026-09-01T12:00:00Z"})


def test_fresh_db_reaches_latest():
    conn = _conn()
    applied = models.init_db(conn)
    assert applied == [m.version for m in migrations.MIGRATIONS]
    assert migrations.current_version(conn) == migrations.latest_version()
    assert migrations.has_column(conn, "reservations", "mode")
    assert migrations.has_column(conn, "reservations", "turnaround_min")


def test_init_twice_is_noop():
    conn = _conn()
    models.init_db(conn)
    before, changes = _dump(conn), conn.total_changes
    assert models.init_db(conn) == []
    assert _dump(conn) == before
    assert conn.total_changes == changes


def test_legacy_db_upgraded_without_data_loss():
    conn = _legacy_db()
    rid = _seed_reservation(conn)
    assert not migrations.has_column(conn, "reservations", "mode")
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS]
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert row["state"] == "queued" and row["end_at"] == "2026-09-01T12:00:00Z"
    assert row["mode"] == "asap" and row["turnaround_min"] == 0
    assert models.init_db(conn) == []


def test_new_mode_column_enforces_check():
    conn = _conn()
    models.init_db(conn)
    rid = _seed_reservation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE reservations SET mode = 'bogus' WHERE id = ?", (rid,))
    conn.execute("UPDATE reservations SET mode = 'at' WHERE id = ?", (rid,))


def test_column_already_present_is_tolerated():
    conn = _legacy_db()
    conn.execute("ALTER TABLE reservations ADD COLUMN mode TEXT NOT NULL DEFAULT 'asap'")
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS]
    assert migrations.has_column(conn, "reservations", "turnaround_min")
    assert migrations.current_version(conn) == migrations.latest_version()


def test_failed_migration_rolls_back_change_and_version(monkeypatch):
    def boom(c):
        migrations.add_column(c, "reservations", "half_done", "TEXT")
        raise RuntimeError("boom")
    conn = _conn()
    models.init_db(conn)
    base, nxt = migrations.latest_version(), migrations.latest_version() + 1
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [*migrations.MIGRATIONS, migrations.Migration(nxt, "bad", boom)])
    with pytest.raises(RuntimeError, match="boom"):
        migrations.apply(conn)
    assert migrations.current_version(conn) == base
    assert not migrations.has_column(conn, "reservations", "half_done")
    assert not conn.in_transaction


def test_later_migration_applies_after_earlier(monkeypatch):
    conn = _conn()
    models.init_db(conn)
    nxt = migrations.latest_version() + 1
    monkeypatch.setattr(migrations, "MIGRATIONS", [
        *migrations.MIGRATIONS,
        migrations.Migration(nxt, "extra", lambda c: migrations.add_column(c, "users", "note", "TEXT")),
    ])
    assert migrations.apply(conn) == [nxt]
    assert migrations.has_column(conn, "users", "note")
    assert migrations.current_version(conn) == nxt
    assert migrations.apply(conn) == []


def test_db_newer_than_code_is_refused():
    conn = _conn()
    models.init_db(conn)
    conn.execute(f"PRAGMA user_version = {migrations.latest_version() + 1}")
    with pytest.raises(migrations.SchemaTooNew):
        models.init_db(conn)


def test_versions_must_be_contiguous():
    noop = lambda c: None
    with pytest.raises(RuntimeError, match="1..N"):
        migrations._check_contiguous([migrations.Migration(1, "a", noop),
                                      migrations.Migration(3, "c", noop)])


def test_on_disk_wal_db_init_twice(tmp_path):
    path = str(tmp_path / "daolab.db")
    conn = models.connect(path)
    assert models.init_db(conn) == [m.version for m in migrations.MIGRATIONS]
    conn.close()
    conn = models.connect(path)
    assert models.init_db(conn) == []
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def _cli_settings(monkeypatch, tmp_path):
    path = str(tmp_path / "daolab.db")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, db_path=path))
    return path


def test_cli_init_twice(monkeypatch, tmp_path, capsys):
    _cli_settings(monkeypatch, tmp_path)
    latest = migrations.latest_version()
    assert cli.cmd_init(None) == 0
    out = capsys.readouterr().out
    assert f"schema: v{latest}" in out and "applied: v1" in out
    assert cli.cmd_init(None) == 0
    assert f"schema: v{latest} (up to date)" in capsys.readouterr().out


def test_cli_init_refuses_newer_db(monkeypatch, tmp_path, capsys):
    path = _cli_settings(monkeypatch, tmp_path)
    conn = models.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_init(None)
    assert exc.value.code == 1
    assert "v99" in capsys.readouterr().err
