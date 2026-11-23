"""P-0d (EXTRAVAR-MATRIX row 25 hardening): the orphan sweep destroys by Xen Orchestra VM id.

A VM name never reaches ansible again. `tagged_vms()` gives (id, name) pairs, both strictly checked; the engine
reads the reservation id from the name inside Python and hands `destroy_vms()` the ids only. The ids travel in a
0600 JSON file in the runtime dir (the F-5-1 pattern), read by orphans.yml with lookup('file'): lookup results
are data, never templated. The file is removed when the run ends, whatever happens.
"""
import dataclasses
import json
import os
import pathlib
import stat
import time
import types
import uuid

import pytest
import yaml

from service import config, models, runner
from service.runner import AnsibleRunner
from tests.conftest import REPO

U1 = "c2a73b7e-be6b-493b-8ce4-bb0b9af95001"                # a fixed uuid4-shaped id: stable test ids
U2 = str(uuid.uuid5(uuid.NAMESPACE_URL, "daolab-r999-x"))
GOOD = "daolab-r999-client"
PAYLOAD = "{{ lookup('pipe', 'touch /tmp/p0d-canary') }}"

VALID_IDS = [U1, U2, "00000000-0000-0000-0000-000000000000", "6769f0a6-7f4e-88ae-801e-648d0f5d0650"]
INVALID_IDS = [
    U1.upper(), "{{7*7}}", PAYLOAD, U1 + "\n", U1 + " ", " " + U1, U1.replace("-", ""), U1[:-1], U1 + "a",
    "u-daolab-r999-x", "vm-sink", "u1", "", "6769f0a6-7f4e-88ae-801e-648d0f5d065g", "6769f0a6_7f4e_88ae_801e_648d0f5d0650",
    None, 5, U1.encode(), [U1], {"id": U1},
]


# ---- the pattern --------------------------------------------------------------------------------

@pytest.mark.parametrize("vm_id", VALID_IDS)
def test_valid_ids_pass(vm_id):
    assert models.vm_id_ok(vm_id) is True


@pytest.mark.parametrize("vm_id", INVALID_IDS, ids=lambda v: repr(v)[:40])
def test_invalid_ids_are_refused(vm_id):
    assert models.vm_id_ok(vm_id) is False


def test_split_ids_keeps_order_drops_duplicates_and_counts_the_invalid():
    assert models.split_vm_ids([U2, PAYLOAD, U1, U2, None]) == ([U2, U1], 2)
    assert models.split_vm_ids([]) == ([], 0) and models.split_vm_ids(None) == ([], 0)


def test_split_tagged_vms_needs_both_halves_valid():
    rows = [{"id": U1, "name": GOOD},                               # kept
            {"id": U2, "name": "daolab-r999-" + PAYLOAD},          # bad name
            {"id": PAYLOAD, "name": "daolab-r1-a"},                # bad id
            {"id": "vm-sink", "name": "daolab-r12-sink"},          # not an XO id
            models.TaggedVm(U2, "daolab-r1-a"),                    # already a pair: kept
            {"id": U1, "name": "daolab-r5-dup"},                   # same id again: kept once, not counted
            GOOD,                                                  # the old contract (a bare name)
            {"name": GOOD}, {"id": U1}, None, 5]
    kept, dropped = models.split_tagged_vms(rows)
    # exactly the two valid VMs, in listing order; the repeated id U1 appears once (its first row)
    assert kept == [models.TaggedVm(U1, GOOD), models.TaggedVm(U2, "daolab-r1-a")]
    assert [v.id for v in kept].count(U1) == 1
    assert models.TaggedVm(U1, "daolab-r5-dup") not in kept
    # the rows with a bad name or a bad id, listed one by one (the repeat is not among them)
    invalid = [{"id": U2, "name": "daolab-r999-" + PAYLOAD}, {"id": PAYLOAD, "name": "daolab-r1-a"},
               {"id": "vm-sink", "name": "daolab-r12-sink"}, GOOD, {"name": GOOD}, {"id": U1}, None, 5]
    assert all(r in rows for r in invalid)
    assert dropped == len(invalid)
    assert dropped == len(rows) - 3                                 # the repeat is not "invalid"
    assert models.split_tagged_vms(None) == ([], 0)


def test_the_playbook_pattern_is_the_python_pattern():
    gv = yaml.safe_load((pathlib.Path(REPO) / "playbooks" / "group_vars" / "all.yml").read_text())
    assert gv["daolab_vm_id_re"] == "^" + models.VM_ID.pattern + "$"


# ---- the ids file: 0600, in the runtime dir, gone after the run -------------------------------------

def _settings(tmp_path, **over):
    base = dict(artifacts_dir=str(tmp_path / "jobs"), playbooks_dir=os.path.join(REPO, "playbooks"),
                inventory=os.path.join(REPO, "inventory", "hosts.yml"), api_url="http://127.0.0.1:1",
                internal_secret="s3", otel_endpoint="", job_timeout_sec=120)
    return dataclasses.replace(config.settings, **{**base, **over})


@pytest.fixture
def rt(tmp_path, monkeypatch):
    d = tmp_path / "rt"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("DAOLAB_RUNTIME_DIR", str(d))
    return d


def ids_files(d):
    return [n for n in os.listdir(d) if n.startswith(runner.VM_IDS_FILE_PREFIX)]


def _noop(*a, **k):
    pass


def test_the_ids_travel_in_a_0600_file_in_the_runtime_dir(tmp_path, rt):
    seen = {}

    def fake_run(**kw):
        path = kw["extravars"]["daolab_vm_ids_file"]
        seen.update(path=path, mode=stat.S_IMODE(os.stat(path).st_mode), ids=json.load(open(path)),
                    ev=json.dumps(kw["extravars"]))
        return types.SimpleNamespace(status="successful", rc=0)

    assert AnsibleRunner(_settings(tmp_path), run=fake_run).destroy_vms([U1, PAYLOAD, U2], _noop).ok
    assert seen["mode"] == 0o600 and seen["ids"] == [U1, U2]
    assert os.path.dirname(seen["path"]) == str(rt)
    assert "daolab_vm_names" not in seen["ev"] and "{{" not in seen["ev"] and "lookup" not in seen["ev"]
    assert not os.path.exists(seen["path"]) and ids_files(rt) == []


@pytest.mark.parametrize("outcome", ["successful", "failed", "timeout"])
def test_no_ids_file_is_left_behind_whatever_the_run_did(tmp_path, rt, outcome):
    def fake_run(**kw):
        assert ids_files(rt)
        return types.SimpleNamespace(status=outcome, rc=0 if outcome == "successful" else 2)

    AnsibleRunner(_settings(tmp_path), run=fake_run).destroy_vms([U1], _noop)
    assert ids_files(rt) == []


def test_no_ids_file_is_left_behind_when_the_run_raises(tmp_path, rt):
    def boom(**kw):
        raise RuntimeError("ansible-runner blew up")

    with pytest.raises(RuntimeError):
        AnsibleRunner(_settings(tmp_path), run=boom).destroy_vms([U1], _noop)
    assert ids_files(rt) == []


def test_only_invalid_ids_write_no_file_and_run_nothing(tmp_path, rt):
    calls = []
    r = AnsibleRunner(_settings(tmp_path), run=lambda **kw: calls.append(kw)).destroy_vms(
        [PAYLOAD, "vm-sink"], _noop)
    assert r.ok is False and calls == [] and ids_files(rt) == []
    assert "lookup" not in (r.error or "") and "vm-sink" not in (r.error or "")


def test_listing_writes_no_ids_file(tmp_path, rt):
    def fake_run(**kw):
        assert "daolab_vm_ids_file" not in kw["extravars"]
        with open(kw["extravars"]["daolab_output_file"], "w") as fh:
            json.dump([{"id": U1, "name": GOOD}], fh)
        return types.SimpleNamespace(status="successful", rc=0)

    assert AnsibleRunner(_settings(tmp_path), run=fake_run).tagged_vms() == [models.TaggedVm(U1, GOOD)]
    assert ids_files(rt) == []


def test_a_stale_ids_file_is_swept_like_a_stale_secret_file(rt):
    stale = rt / (runner.VM_IDS_FILE_PREFIX + "x.json")
    stale.write_text("[]")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    runner._sweep_secret_files(60)
    assert not stale.exists()


def test_the_mock_runner_destroys_by_id_and_keeps_its_names():
    rn = runner.MockRunner()
    rn.vms.update({GOOD, "daolab-r1-a"})
    listed = rn.tagged_vms()
    assert listed == [models.TaggedVm(runner.mock_vm_id(n), n) for n in sorted(rn.vms)]
    assert all(models.vm_id_ok(v.id) for v in listed)
    assert rn.destroy_vms([runner.mock_vm_id(GOOD)], _noop).ok
    assert rn.vms == {"daolab-r1-a"}
    assert rn.destroy_vms([GOOD], _noop).ok and rn.vms == {"daolab-r1-a"}     # a name is not an id
