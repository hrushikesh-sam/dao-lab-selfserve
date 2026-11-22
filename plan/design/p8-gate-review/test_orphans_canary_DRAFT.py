"""DRAFT: M-vm-names canary for EXTRAVAR-MATRIX row 25 (`daolab_vm_names`).

NOT part of P-0-final.patch or F-5-1.patch. Never executed by its author: every assumption below is
UNVERIFIED until the operator runs it (M-VM-NAMES-HARNESS.md). Put it at tests/test_orphans_canary_DRAFT.py.

What it asks: if a Xen Orchestra VM is named `daolab-r<id>-{{ lookup('pipe', 'touch <tmp>/canary') }}`
(it passes the `^daolab-r[0-9]+-` prefix filters of `playbooks/orphans.yml` and `engine._TAGGED`), does real
ansible-playbook evaluate the name on the controller
  (a) while LISTING the tagged VMs (list mode of orphans.yml), or
  (b) when the engine hands the name back as the extra var `daolab_vm_names` to destroy it?
A FAILED test means row 25 is vulnerable (a stop condition: it would join the P-0 fix set). A PASSED test means
no evaluation was observed with that ansible-core only. The payload only `touch`es a file in pytest's tmp_path.

UNVERIFIED assumptions: (1) tests.test_playbooks exports Lab, ANSIBLE, clean_state and _vm with the signatures
used here; (2) the fake `daolab-xo vms --prefix` in tests/fakelab.py returns VM names verbatim, JSON-encoded;
(3) a VM name with braces, quotes and spaces round-trips through the fake state file; (4) orphans.yml runs to
completion on the fake lab with such a state (existing tests B-73 use the same shape); (5) the real XO lets
someone create such a name (who can is UNKNOWN: ask the XO administrator).
"""
import pytest

from tests.test_playbooks import ANSIBLE, Lab, _vm, clean_state

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


def test_list_mode_never_evaluates_a_vm_name(lab, tmp_path):
    canary = tmp_path / "canary-list"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")
    assert not canary.exists(), f"a VM name was evaluated while listing:\n{lab.stdout[-1500:]}"


def test_destroy_mode_never_evaluates_a_vm_name_handed_back_as_an_extra_var(lab, tmp_path):
    canary = tmp_path / "canary-destroy"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    # exactly what AnsibleRunner.destroy_vms passes: {"daolab_vm_names": list(names)}
    lab.run("orphans.yml", daolab_vm_names=[BENIGN, poison_name(canary)])
    assert not canary.exists(), f"a VM name was evaluated as an extra var:\n{lab.stdout[-1500:]}"


def test_the_two_halves_chained_like_the_engine_never_evaluate_a_vm_name(lab, tmp_path):
    canary = tmp_path / "canary-chain"
    lab.set(_state_with([BENIGN, poison_name(canary)]))
    lab.run("orphans.yml")                                   # half 1: list, names written to the output file
    try:
        names = lab.output()
    except (OSError, ValueError):
        pytest.skip(f"list mode wrote no output file; see the other two tests:\n{lab.stdout[-800:]}")
    assert not canary.exists(), "evaluated during the listing half"
    lab.run("orphans.yml", daolab_vm_names=list(names))      # half 2: destroy exactly what half 1 reported
    assert not canary.exists(), f"evaluated during the destroy half:\n{lab.stdout[-1500:]}"
