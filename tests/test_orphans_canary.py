"""P-0c: the row 25 (`daolab_vm_names`) canary, with REAL ansible-playbook on the fake Xen Orchestra lab.

A VM named `daolab-r999-{{ lookup('pipe', 'touch <tmp>/canary') }}` passes the old prefix filters
(`^daolab-r[0-9]+-` in orphans.yml and engine._TAGGED). The canary file must never appear when the names
travel the way production sends them: AnsibleRunner.destroy_vms() builds the extra var (the P-0c chokepoint),
and the same payload is what the earlier draft passed straight to the playbook (it created the canary).
The payload only touches a file in pytest's tmp_path. Run it on every supported interpreter / ansible-core."""
import pytest

from tests.test_ansible_runner import FakeRun, Steps, settings  # noqa: F401  (fixture)
from tests.test_playbooks import ANSIBLE, Lab, _vm, clean_state
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


def _what_the_runner_sends(settings, names):
    """The `daolab_vm_names` AnsibleRunner.destroy_vms really puts in the extra vars."""
    fake = FakeRun()
    AnsibleRunner(settings, run=fake).destroy_vms(list(names), Steps())
    return fake.calls[0]["extravars"]["daolab_vm_names"] if fake.calls else []


def test_list_mode_never_evaluates_a_vm_name(lab, tmp_path):
    canary = tmp_path / "canary-list"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")
    assert not canary.exists(), f"a VM name was evaluated while listing:\n{lab.stdout[-1500:]}"


def test_destroy_mode_never_evaluates_a_vm_name_handed_back_as_an_extra_var(lab, tmp_path, settings):
    canary = tmp_path / "canary-destroy"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    sent = _what_the_runner_sends(settings, [BENIGN, poison_name(canary)])
    assert sent == [BENIGN]
    lab.run("orphans.yml", daolab_vm_names=sent)
    assert not canary.exists(), f"a VM name was evaluated as an extra var:\n{lab.stdout[-1500:]}"


def test_the_two_halves_chained_like_the_engine_never_evaluate_a_vm_name(lab, tmp_path, settings):
    canary = tmp_path / "canary-chain"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")                                   # half 1: list, names written to the output file
    try:
        names = lab.output()
    except (OSError, ValueError):
        pytest.skip(f"list mode wrote no output file; see the other two tests:\n{lab.stdout[-800:]}")
    assert not canary.exists(), "evaluated during the listing half"
    lab.run("orphans.yml", daolab_vm_names=_what_the_runner_sends(settings, names))   # half 2
    assert not canary.exists(), f"evaluated during the destroy half:\n{lab.stdout[-1500:]}"
