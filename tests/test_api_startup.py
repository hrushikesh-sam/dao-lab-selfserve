"""B-107 API startup guards: schema parity with the engine and conditional swap check."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from service import api, hostcheck, migrations, models

S = "AGE-SECRET-KEY-1" + "Q" * 58
REASON = "swap unsafe: disable swap, use zram or dm-crypt"


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    conn = models.connect(str(path))
    models.init_db(conn)
    settings = dataclasses.replace(
        getattr(api.config, "settings"),
        db_path=str(path))
    monkeypatch.setattr(api.config, "settings", settings)
    monkeypatch.setattr(hostcheck, "swap_safe", lambda *_a, **_kw: (True, "swap ok: none"))
    yield conn
    conn.close()


def _expect_schema_message(exc, have):
    want = migrations.latest_version()
    assert str(exc.value) == (
        f"error: database schema is v{have} but this checkout expects v{want}; "
        "run `daolab init` first")


def test_startup_accepts_latest_schema(api_db):
    with TestClient(api.app) as client:
        body = client.get("/whoami").text
    assert S not in body


def test_startup_refuses_v13(api_db):
    api_db.execute("PRAGMA user_version = 13")
    with pytest.raises(RuntimeError) as exc:
        api.startup_checks(api_db)
    _expect_schema_message(exc, 13)
    with pytest.raises(RuntimeError) as exc:
        with TestClient(api.app):
            pass
    _expect_schema_message(exc, 13)


def test_startup_refuses_reverted_schema(api_db):
    api_db.execute("CREATE TABLE user_keys (id INTEGER)")
    with pytest.raises(migrations.RevertedSchema) as raw:
        migrations.refuse_reverted_schema(api_db)
    expected = f"error: {raw.value}"
    with pytest.raises(RuntimeError) as exc:
        api.startup_checks(api_db)
    assert str(exc.value) == expected
    with pytest.raises(RuntimeError) as exc:
        with TestClient(api.app):
            pass
    assert str(exc.value) == expected


def test_swap_check_only_runs_when_snapshots_in_use(api_db, monkeypatch):
    from tests.conftest import mk_setup
    called = []

    def safe(*_a, **_kw):
        called.append(True)
        return True, "swap ok: none"

    monkeypatch.setattr(hostcheck, "swap_safe", safe)
    api.startup_checks(api_db)
    assert called == []

    sid = mk_setup(api_db, "tb-01", ["e810"])
    api_db.execute("UPDATE setups SET snapshot_enabled = 1 WHERE id = ?", (sid,))
    api.startup_checks(api_db)
    assert called == [True]


def test_startup_refuses_unsafe_swap_with_snapshots_in_use(api_db, monkeypatch):
    from tests.conftest import mk_setup
    sid = mk_setup(api_db, "tb-01", ["e810"])
    api_db.execute("UPDATE setups SET snapshot_enabled = 1 WHERE id = ?", (sid,))
    monkeypatch.setattr(hostcheck, "swap_safe", lambda *_a, **_kw: (False, REASON))
    with pytest.raises(hostcheck.HostCheckError) as exc:
        with TestClient(api.app):
            pass
    assert str(exc.value) == REASON
