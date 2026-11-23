"""P-0c: the row 25 (`daolab_vm_names`) canary, with REAL ansible-playbook on the fake Xen Orchestra lab.

A VM named `daolab-r999-{{ lookup('pipe', 'touch <tmp>/canary') }}` passes the old prefix filters
(`^daolab-r[0-9]+-` in orphans.yml and engine._TAGGED). The canary file must never appear when the names
travel the way production sends them: AnsibleRunner.destroy_vms() builds the extra var (the P-0c chokepoint),
and the same payload is what the earlier draft passed straight to the playbook (it created the canary).
The payload only touches a file in pytest's tmp_path. Run it on every supported interpreter / ansible-core.

P-0d: what travels back is no longer a name but the XO VM ids, in a file (`daolab_vm_ids_file`); list mode
writes {id, name} pairs and the runner keeps a pair only when both halves are valid."""
import json
import types

import pytest

from tests.test_ansible_runner import Steps, settings  # noqa: F401  (fixture)
from tests.test_playbooks import ANSIBLE, Lab, _vm, clean_state, vm_uuid
from service import models
from service.runner import AnsibleRunner

pytestmark = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")

BENIGN = "daolab-r998-y"


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def poison_name(canary) -> str:
    return "daolab-r999-" + "{{ lookup('pipe', 'touch " + str(canary) + "') }}"


def _state_with(names):
    st = clean_state()
    for n in names:
        st["vms"][n] = _vm(n)
    return st


def _what_the_runner_sends(settings, ids):
    """The ids AnsibleRunner.destroy_vms really writes to its `daolab_vm_ids_file` (read during the run:
    the file is removed when the run ends). None when it runs nothing."""
    sent = []

    def fake_run(**kw):
        assert "daolab_vm_names" not in kw["extravars"]
        with open(kw["extravars"]["daolab_vm_ids_file"]) as fh:
            sent.append(json.load(fh))
        return types.SimpleNamespace(status="successful", rc=0)

    AnsibleRunner(settings, run=fake_run).destroy_vms(list(ids), Steps())
    return sent[0] if sent else None


def _send(lab, ids):
    path = lab.dir / "sent-ids.json"
    path.write_text(json.dumps(ids))
    return str(path)


def test_list_mode_never_evaluates_a_vm_name(lab, tmp_path):
    canary = tmp_path / "canary-list"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")
    assert not canary.exists(), f"a VM name was evaluated while listing:\n{lab.stdout[-1500:]}"


def test_destroy_mode_never_evaluates_a_vm_name_handed_back_as_an_extra_var(lab, tmp_path, settings):
    canary = tmp_path / "canary-destroy"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    sent = _what_the_runner_sends(settings, [vm_uuid(BENIGN), poison_name(canary)])
    assert sent == [vm_uuid(BENIGN)]
    assert lab.run("orphans.yml", daolab_vm_ids_file=_send(lab, sent)) == 0, lab.stdout[-1500:]
    assert not canary.exists(), f"a VM name was evaluated in destroy mode:\n{lab.stdout[-1500:]}"
    assert sorted(lab.state()["vms"]) == [poison_name(canary)]          # BENIGN went, by id


def test_the_two_halves_chained_like_the_engine_never_evaluate_a_vm_name(lab, tmp_path, settings):
    canary = tmp_path / "canary-chain"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")                                   # half 1: list, names written to the output file
    try:
        names = lab.output()
    except (OSError, ValueError):
        pytest.skip(f"list mode wrote no output file; see the other two tests:\n{lab.stdout[-800:]}")
    assert not canary.exists(), "evaluated during the listing half"
    pairs, dropped = models.split_tagged_vms(names)                  # what tagged_vms() keeps
    assert [v.name for v in pairs] == [BENIGN] and dropped == 1
    sent = _what_the_runner_sends(settings, [v.id for v in pairs])
    assert lab.run("orphans.yml", daolab_vm_ids_file=_send(lab, sent)) == 0     # half 2
    assert not canary.exists(), f"evaluated during the destroy half:\n{lab.stdout[-1500:]}"
    assert sorted(lab.state()["vms"]) == [poison_name(canary)]
