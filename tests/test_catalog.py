"""daolab sync validation and mirroring — plan/01-domain-model.md rules."""
import os
import sqlite3

import pytest

from service import catalog, models
from tests.conftest import T, mk_res

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UC_DIR = os.path.join(REPO, "usecases")
ST_DIR = os.path.join(REPO, "setups")
INV = os.path.join(REPO, "inventory", "hosts.yml")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    models.init_db(c)
    catalog.seed_usecases(c)
    yield c
    c.close()


def test_sync_happy_path(conn):
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    setup = models.setup_by_name(conn, "tb-01")
    assert models.from_json(setup["capabilities"]) == ["e810", "octeon-cn106", "sdp", "wire"]
    uc = models.usecase_by_name(conn, "tls-proxy-nginx")
    assert uc["yaml_path"] and uc["variant"] == "separate-backend"
    assert uc["description"]                              # seeded description preserved
    members = models.setup_members(conn, setup["id"])
    assert {m["name"]: m["kind"] for m in members} == {"dpu-01": "dpu", "tg-01": "nic"}
    assert all(m["hw_class"] is None for m in members)    # sync never writes hw_class


def test_sync_idempotent_preserves_hw_class(conn):
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    setup = models.setup_by_name(conn, "tb-01")
    dpu = conn.execute("SELECT id FROM resources WHERE setup_id=? AND name='dpu-01'",
                       (setup["id"],)).fetchone()
    models.update(conn, "resources", dpu["id"], {"hw_class": "sdp-pf1-vf8"})
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    dpu = conn.execute("SELECT hw_class FROM resources WHERE id=?", (dpu["id"],)).fetchone()
    assert dpu["hw_class"] == "sdp-pf1-vf8"               # engine-owned column preserved


def test_validation_errors_all_or_nothing(conn, tmp_path):
    bad = tmp_path / "usecases"
    bad.mkdir()
    (bad / "broken.yml").write_text(
        "name: broken\nvariant: v1\nrequires: []\nroles: {}\n"
        "verify: {from: x, cmd: c, expect: e}\nsurprise: 1\n")
    with pytest.raises(catalog.SyncError) as e:
        catalog.sync(conn, str(bad), ST_DIR, INV)
    text = "\n".join(e.value.errors)
    assert "requires" in text and "surprise" in text and "roles" in text
    assert models.setup_by_name(conn, "tb-01") is None  # nothing written


def test_unknown_host_rejected(conn, tmp_path):
    st = tmp_path / "setups"
    st.mkdir()
    (st / "setup-02.yml").write_text(
        "name: setup-02\npool: lab-1\n"
        "members:\n  n1: {kind: compute, capabilities: [], host: ghost-xcp}\n"
        "diagram: |\n  graph LR\n    a --> b\n")
    with pytest.raises(catalog.SyncError) as e:
        catalog.sync(conn, UC_DIR, str(st), INV)
    assert "ghost-xcp" in "\n".join(e.value.errors)


def test_removal_blocked_by_active_reservation(conn, tmp_path):
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    models.insert(conn, "users", {"username": "u1", "token_hash": "h", "ssh_pubkey": "k"})
    setup = models.setup_by_name(conn, "tb-01")
    uc = models.usecase_by_name(conn, "tls-proxy-nginx")
    mk_res(conn, setup["id"], uc["id"], T(10), T(12))
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(catalog.SyncError) as e:
        catalog.sync(conn, str(empty), str(empty), INV)
    assert "removal blocked" in "\n".join(e.value.errors)
    assert models.setup_by_name(conn, "tb-01") is not None   # rolled back


def test_health_rules_loaded():
    ucs, errors = catalog.load_usecases(UC_DIR)
    assert errors == []
    vpp = next(u for u in ucs if u["name"] == "vpp")
    names = [r["name"] for r in vpp["health"]]
    assert names == ["vms_up", "ssh_reachable", "dpu_app", "datapath"]
    assert all("$RID" in r["promql"] for r in vpp["health"])
    assert [r.get("severity") for r in vpp["health"]] == [None, None, None, "degraded"]


def test_malformed_health_rule_rejected(conn, tmp_path):
    bad = tmp_path / "usecases"
    bad.mkdir()
    (bad / "hbroken.yml").write_text(
        "name: hbroken\nvariant: v1\nrequires: [wire]\n"
        "roles: {r1: {on: dpu, app: a}}\n"
        "verify: {from: r1, cmd: c, expect: e}\n"
        "health:\n"
        "  - {name: nolid, promql: 'up', expect: '== 1'}\n"
        "  - {name: badop, promql: 'up{$RID}', expect: 'maybe'}\n"
        "  - {name: badsev, promql: 'up{$RID}', expect: '> 0', severity: warn}\n")
    with pytest.raises(catalog.SyncError) as e:
        catalog.sync(conn, str(bad), ST_DIR, INV)
    text = "\n".join(e.value.errors)
    assert "hbroken.yml" in text                    # file
    assert "health.nolid.promql" in text and "$RID" in text   # rule name + problem
    assert "health.badop.expect" in text
    assert "health.badsev.severity" in text
