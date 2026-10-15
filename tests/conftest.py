import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from service import models

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def T(hour, minute=0, day=1):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    models.init_db(conn)
    models.insert(conn, "users", {"username": "u1", "token_hash": "h1", "ssh_pubkey": "k"})
    yield conn
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
