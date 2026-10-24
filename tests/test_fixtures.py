"""Sanity checks for the shared fixtures in conftest.py (B-06)."""
from tests.conftest import NOW, TEST_USERS


def test_db_is_temp_and_migrated(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 1
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1     # placeholder u1


def test_frozen_clock(db, now):
    assert now == NOW


def test_users_fixture(db, users):
    assert set(users) == set(TEST_USERS)
    assert all(users[name] == f"tok-{name}" for name in TEST_USERS)
    roles = {r["username"]: r["role"] for r in db.execute("SELECT username, role FROM users")}
    assert roles["alice"] == "user" and roles["bob"] == "user"
    assert roles["dev-admin"] == "admin"


def test_tb01_fixture(db, tb01):
    sid, uid = tb01
    setup = db.execute("SELECT * FROM setups WHERE id = ?", (sid,)).fetchone()
    uc = db.execute("SELECT * FROM usecases WHERE id = ?", (uid,)).fetchone()
    assert setup["name"] == "tb-01" and uc["name"] == "tls-proxy-nginx"


def test_api_env_seeds_users_and_serves(api_env):
    r = api_env.client.get("/whoami", headers={"Authorization": f"Bearer {api_env.user_token}"})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    admin = api_env.client.get("/whoami", headers={"Authorization": f"Bearer {api_env.admin_token}"})
    assert admin.json()["role"] == "admin"
    assert set(api_env.tokens) == set(TEST_USERS)
