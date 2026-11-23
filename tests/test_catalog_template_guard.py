"""G-2: the C-1 rule in CI. A usecase's verify.cmd reaches ansible as a string the verify role
runs, so an ansible template delimiter in it would be evaluated on the controller (the P-0
class: `{{ lookup('pipe', ...) }}` runs a shell command there). `daolab sync` rejects
"{{", "{%" and "{#" in verify.cmd; the single-brace {role_ip} placeholders stay valid.
The repo scan replaces the manual C-1 grep over usecases/ and setups/."""
import os
import sqlite3

import pytest

from service import catalog, models
from tests.conftest import REPO

ST_DIR = os.path.join(REPO, "setups")
INV = os.path.join(REPO, "inventory", "hosts.yml")
DELIMITERS = ("{{", "{%", "{#")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    models.init_db(c)
    catalog.seed_usecases(c)
    yield c
    c.close()


def _usecases_dir(tmp_path, cmd):
    d = tmp_path / "usecases"
    d.mkdir(exist_ok=True)
    quoted = cmd.replace("'", "''")             # YAML single-quoted scalar
    (d / "canary.yml").write_text(
        "name: canary\nvariant: v1\nrequires: [wire]\n"
        "roles: {r1: {on: dpu, app: a}}\n"
        f"verify: {{from: r1, cmd: '{quoted}', expect: ok}}\n")
    return str(d)


def test_canary_lookup_in_verify_cmd_fails_sync_and_never_runs(conn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # a relative `touch CANARY` would land here
    cmd = "{{ lookup('pipe','touch CANARY') }}"
    uc_dir = _usecases_dir(tmp_path, cmd)
    ucs, errors = catalog.load_usecases(uc_dir)
    assert [e for e in errors if "verify.cmd" in e and "template delimiter" in e], errors
    with pytest.raises(catalog.SyncError) as exc:
        catalog.sync(conn, uc_dir, ST_DIR, INV)
    assert any("verify.cmd" in e and "template delimiter" in e for e in exc.value.errors)
    assert models.usecase_by_name(conn, "canary") is None      # all-or-nothing: nothing written
    assert not (tmp_path / "CANARY").exists()


@pytest.mark.parametrize("delim", DELIMITERS)
def test_each_template_delimiter_is_rejected(tmp_path, delim):
    _, errors = catalog.load_usecases(_usecases_dir(tmp_path, f"echo {delim} x"))
    hits = [e for e in errors if "verify.cmd" in e and "template delimiter" in e]
    assert hits and repr(delim) in hits[0], errors


@pytest.mark.parametrize("cmd", [
    "ping -c 4 -W 2 {r1_ip}",
    "curl -sk https://{r1_ip}/ | grep -c ok",
    "awk '{print $1}' /proc/loadavg",            # a single brace is shell, not a template
])
def test_single_brace_commands_are_still_accepted(tmp_path, cmd):
    ucs, errors = catalog.load_usecases(_usecases_dir(tmp_path, cmd))
    assert errors == []
    assert ucs[0]["verify"]["cmd"] == cmd


def test_no_template_delimiter_anywhere_in_usecases_or_setups():
    """The manual C-1 check (`grep -rnE '\\{\\{|\\{%|\\{#' usecases setups` -> 0 lines), in CI."""
    hits = []
    for top in ("usecases", "setups"):
        for root, _, files in os.walk(os.path.join(REPO, top)):
            for f in files:
                path = os.path.join(root, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if any(d in line for d in DELIMITERS):
                            hits.append(f"{os.path.relpath(path, REPO)}:{n}")
    assert hits == []
