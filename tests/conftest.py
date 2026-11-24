"""Shared test fixtures — temp DB, frozen clock, seeded users/setups (B-06).

New test modules should build on these instead of rolling their own DB, clock or users:

  * ``db``       — an in-memory DB at the latest schema, one placeholder user (``u1``).
  * ``now``      — the frozen clock (:data:`NOW`); schedule against it for determinism.
  * ``users``    — the shared test users ``alice``, ``bob`` (user) and ``dev-admin``
                   (admin) from B-02; maps username -> bearer token.
  * ``tb01``     — the canonical ``tb-01`` setup + ``tls-proxy-nginx`` use case.
  * ``api_env``  — a FastAPI ``TestClient`` over a temp DB synced from ``usecases/`` +
                   ``setups/``, with the shared users seeded.

The ``mk_setup`` / ``mk_usecase`` / ``mk_res`` / ``mk_user`` helpers insert rows directly.
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

import dotenv
import pytest

# The tests run on the settings' defaults, never on a developer's .env (scripts/run/dev.sh writes
# one; with DAOLAB_DEPLOY_VMS=false in it the VM tests saw no VMs). service.config calls
# load_dotenv at import, so this has to come before the first `from service import ...`.
dotenv.load_dotenv = lambda *args, **kwargs: False

from service import models                          # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)      # frozen clock
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TB01_CAPS = ["e810", "octeon-cn106", "sdp", "wire"]
TLS_REQ = ["octeon-cn106", "e810", "wire"]
TEST_USERS = {"alice": "user", "bob": "user", "dev-admin": "admin"}


def T(hour, minute=0, day=1):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


# ---- the `requires_age` marker (X-AGE) ----------------------------------------------------
# A test marked @pytest.mark.requires_age runs the real age CLI. It runs only when both age
# and age-keygen (DAOLAB_AGE_BIN / DAOLAB_AGE_KEYGEN_BIN, else the names on PATH) are the
# pinned v1.1.1: `--version` exits 0 and prints exactly "v1.1.1\n", as the release binaries
# do. Otherwise it is skipped, or fails when DAOLAB_REQUIRE_AGE=1 (CI installs age with
# deploy/age/install-age.sh and sets it, so a broken install can't hide behind skips).
AGE_MISSING = "age v1.1.1 not installed (see deploy/age/README.md)"
_AGE_TOOLS = (("DAOLAB_AGE_BIN", "age"), ("DAOLAB_AGE_KEYGEN_BIN", "age-keygen"))
_age_ok = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_age: needs the pinned age v1.1.1 CLI, age and age-keygen "
                   "(skipped without it; fails instead when DAOLAB_REQUIRE_AGE=1)")


def _is_age_v111(name):
    path = shutil.which(name)
    if path is None:
        return False
    try:
        p = subprocess.run([path, "--version"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout == b"v1.1.1\n"


def age_installed():
    """True when the pinned age v1.1.1 is usable (checked once per test session)."""
    global _age_ok
    if _age_ok is None:
        _age_ok = all(_is_age_v111(os.environ.get(var) or default) for var, default in _AGE_TOOLS)
    return _age_ok


def pytest_runtest_setup(item):
    if item.get_closest_marker("requires_age") is None or age_installed():
        return
    if os.environ.get("DAOLAB_REQUIRE_AGE") != "1":
        pytest.skip(AGE_MISSING)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    # The required case fails in the call phase, so it is reported as a failed test, not as
    # a setup error.
    if item.get_closest_marker("requires_age") is not None and not age_installed():
        pytest.fail(f"{AGE_MISSING}, and DAOLAB_REQUIRE_AGE=1", pytrace=False)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    models.init_db(conn)
    models.insert(conn, "users", {"username": "u1", "token_hash": "h1", "ssh_pubkey": "k"})
    yield conn
    conn.close()


@pytest.fixture
def now():
    """The frozen clock — every new test should schedule against this, not wall time."""
    return NOW


def mk_user(conn, username, role="user", token=None):
    """Insert a user with a known token (default ``tok-<username>``); returns (id, token)."""
    token = token or f"tok-{username}"
    uid = models.insert(conn, "users", {
        "username": username, "role": role,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "ssh_pubkey": "ssh-ed25519 AAAA test"})
    return uid, token


@pytest.fixture
def users(db):
    """The shared seeded users (B-02): alice, bob (user), dev-admin (admin).
    Returns ``{username: bearer_token}``."""
    for name, role in TEST_USERS.items():
        mk_user(db, name, role)
    return {name: f"tok-{name}" for name in TEST_USERS}


@pytest.fixture
def tb01(db):
    """The canonical testbed: the ``tb-01`` setup + the ``tls-proxy-nginx`` use case.
    Returns ``(setup_id, usecase_id)``."""
    sid = mk_setup(db, "tb-01", TB01_CAPS, dpu_hw_class="sdp-pf1-vf8")
    uid = mk_usecase(db, "tls-proxy-nginx", TLS_REQ, hw_class="sdp-pf1-vf8")
    return sid, uid


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """A FastAPI TestClient over a temp DB synced from usecases/ + setups/, with the
    shared users seeded. New API test modules should use this instead of a bespoke client."""
    import dataclasses
    from fastapi.testclient import TestClient
    from service import api, catalog, config

    settings = dataclasses.replace(
        config.settings,
        db_path=str(tmp_path / "api.db"),
        internal_secret="test-secret",
        dashboard_dir=os.path.join(REPO, "dashboard"),
        usecases_dir=os.path.join(REPO, "usecases"),
        setups_dir=os.path.join(REPO, "setups"),
        inventory=os.path.join(REPO, "inventory", "hosts.yml"))
    monkeypatch.setattr(config, "settings", settings)
    conn = models.connect(settings.db_path)
    models.init_db(conn)
    catalog.seed_usecases(conn)
    catalog.sync(conn, settings.usecases_dir, settings.setups_dir, settings.inventory)
    tokens = {name: mk_user(conn, name, role)[1] for name, role in TEST_USERS.items()}

    def add_user(username, role="user"):
        return mk_user(conn, username, role)[1]

    client = TestClient(api.app, raise_server_exceptions=False)
    yield type("ApiEnv", (), {
        "client": client, "conn": conn, "models": models, "tokens": tokens,
        "user_token": tokens["alice"], "admin_token": tokens["dev-admin"],
        "add_user": staticmethod(add_user)})
    conn.close()


def mk_setup(conn, name, caps, dpu_hw_class=None, has_dpu=True, pool="lab-1"):
    row = conn.execute("SELECT id FROM pools WHERE name = ?", (pool,)).fetchone()
    pool_id = row["id"] if row else models.insert(conn, "pools", {"name": pool})
    sid = models.insert(conn, "setups", {
        "name": name, "pool_id": pool_id, "capabilities": models.to_json(sorted(caps)),
        "links": "[]", "diagram": "graph LR\n  a --> b", "yaml_path": f"setups/{name}.yml"})
    if has_dpu:
        models.insert(conn, "resources", {
            "setup_id": sid, "name": f"{name}-dpu", "kind": "dpu",
            "capabilities": "[]", "host": f"{name}-xcp", "hw_class": dpu_hw_class})
    models.insert(conn, "resources", {
        "setup_id": sid, "name": f"{name}-tg", "kind": "nic",
        "capabilities": "[]", "host": f"{name}-tg-xcp"})
    return sid


def mk_usecase(conn, name, requires, hw_class=None, yaml_path=None):
    return models.insert(conn, "usecases", {
        "name": name, "description": name, "variant": "v",
        "requires": models.to_json(requires), "hw_class": hw_class,
        "roles": "{}", "verify": "{}",
        "yaml_path": yaml_path or f"usecases/{name}.yml"})


def mk_res(conn, setup_id, usecase_id, start, end, state="queued", user_id=1):
    return models.insert(conn, "reservations", {
        "user_id": user_id, "usecase_id": usecase_id, "setup_id": setup_id,
        "state": state, "requested_start_at": models.format_ts(start),
        "start_at": models.format_ts(start), "end_at": models.format_ts(end)})
