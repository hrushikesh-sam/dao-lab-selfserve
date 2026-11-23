"""P-0c (EXTRAVAR-MATRIX row 25): a VM name read back from Xen Orchestra reaches ansible as the extra var
`daolab_vm_names`, and ansible re-templates extra vars. Only names that fullmatch
daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127} may travel; the rest are dropped and only their COUNT is logged.

P-0d: the name now stays inside Python (the engine reads the reservation id from it); only the XO VM id goes to
ansible, in a file. A listed pair is kept only when the name AND the id are valid (tests/test_vm_ids.py)."""
import json
import logging
import types

import pytest

from service import engine, models
from service.runner import AnsibleRunner, MockRunner, mock_vm_id
from tests.test_ansible_runner import FakeRun, Steps, settings  # noqa: F401  (fixture)
from tests.test_orphans import lab, sweeps, tick  # noqa: F401  (fixture)

PAYLOAD = "{{ lookup('pipe', 'touch /tmp/p0c-canary') }}"
POISON = "daolab-r999-" + PAYLOAD
GOOD = "daolab-r999-client"
U1, U2 = "3f0c9d2e-1b7a-4e5f-9c8d-7a6b5c4d3e2f", "8e7d6c5b-4a39-4f2e-8d1c-0b9a8f7e6d5c"

VALID = [GOOD, "daolab-r1-a", "daolab-r0-x", "daolab-r12345-A.b_c-d", "daolab-r7-" + "a" * 100, "daolab-r7-" + "a" * 128, "daolab-r" + "9" * 18 + "-x"]
INVALID = [
    POISON, "daolab-r1-{{7*7}}", "daolab-r1-{%x%}", "daolab-r1-{#x#}", "daolab-r1-$(id)", "daolab-r1-`id`",
    "daolab-r1-a b", "daolab-r1-a;b", "daolab-r1-a|b", "daolab-r1-a'b", 'daolab-r1-a"b', "daolab-r1-a\\b",
    "daolab-r1-a\n", "daolab-r1-a\nb", "daolab-r1-a\r", "daolab-r1-", "daolab-r-a", "daolab-rx-a",
    "daolab-r1-" + "a" * 129, "daolab-r" + "9" * 19 + "-x", "daolab-r1--a", "daolab-r1-.a", "Daolab-r1-a", " daolab-r1-a", "daolab-r1-a ", "xdaolab-r1-a",
    "daolab-r٣-a", "daolab-r1-é", "daolab-r1-a b", "daolab-r1-a\x00", "admin-scratch", "daolab-notes", "",
    None, 5, b"daolab-r1-a", ["daolab-r1-a"],
]


@pytest.mark.parametrize("name", VALID)
def test_valid_names_pass(name):
    assert models.vm_name_ok(name) is True


@pytest.mark.parametrize("name", INVALID, ids=lambda n: repr(n)[:40])
def test_invalid_names_are_refused(name):
    assert models.vm_name_ok(name) is False


def test_split_keeps_order_and_counts_the_dropped():
    assert models.split_vm_names([GOOD, POISON, "daolab-r1-a", None]) == ([GOOD, "daolab-r1-a"], 2)
    assert models.split_vm_names([]) == ([], 0) and models.split_vm_names(None) == ([], 0)


def test_the_runner_lists_only_valid_pairs_and_logs_a_count_only(settings, caplog):
    fake = FakeRun(output=[{"id": U1, "name": GOOD}, {"id": U2, "name": POISON},
                           {"id": "{{x}}", "name": "daolab-r1-a"}, {"id": U2, "name": "daolab-r1-a b"}])
    with caplog.at_level(logging.DEBUG):
        assert AnsibleRunner(settings, run=fake).tagged_vms() == [models.TaggedVm(U1, GOOD)]
    assert "2 listed VM name" in caplog.text
    assert "1 listed VM id" in caplog.text                    # P-0d: its own line, beside Rama's
    assert "lookup" not in caplog.text and "touch" not in caplog.text and "daolab-r1-a b" not in caplog.text
    assert "{{x}}" not in caplog.text and U2 not in caplog.text


def test_destroy_hands_ansible_ids_only_and_logs_no_payload(settings, caplog):
    seen = []

    def fake_run(**kw):
        with open(kw["extravars"]["daolab_vm_ids_file"]) as fh:
            seen.append((json.load(fh), json.dumps(kw["extravars"], default=str)))
        return types.SimpleNamespace(status="successful", rc=0)

    with caplog.at_level(logging.DEBUG):
        assert AnsibleRunner(settings, run=fake_run).destroy_vms([U1, POISON, GOOD], Steps()).ok
    [(sent, ev)] = seen
    assert sent == [U1]
    assert "daolab_vm_names" not in ev and GOOD not in ev
    assert "lookup" not in ev and "{{" not in ev and "touch" not in ev
    assert "1 VM name" in caplog.text
    assert "1 VM id" in caplog.text                           # P-0d: its own line, beside Rama's
    assert "lookup" not in caplog.text and "touch" not in caplog.text and POISON not in caplog.text


def test_destroy_with_only_invalid_ids_runs_nothing(settings):
    fake = FakeRun()
    r = AnsibleRunner(settings, run=fake).destroy_vms([POISON, "daolab-r1-$(id)", GOOD], Steps())
    assert r.ok is False and fake.calls == []
    assert "lookup" not in (r.error or "") and "$(id)" not in (r.error or "")


def test_the_sweep_skips_invalid_names_before_the_runner_and_logs_a_count_only(lab, caplog):
    seen = []

    class Hostile(MockRunner):
        def tagged_vms(self):                                 # r999: no such reservation, so an orphan
            return [models.TaggedVm(U2, POISON), models.TaggedVm(mock_vm_id(GOOD), GOOD),
                    models.TaggedVm(U1, "daolab-r999-a b")]

        def destroy_vms(self, ids, step):
            seen.append(list(ids))
            return super().destroy_vms(ids, step)

    rn = Hostile()
    rn.vms.add(GOOD)
    with caplog.at_level(logging.DEBUG):
        out = tick(lab, rn)
    assert seen == [[mock_vm_id(GOOD)]] and out.orphan_vms == 1 and rn.vms == set()
    assert sweeps(lab) == [{"vms": [GOOD], "vm_ids": [mock_vm_id(GOOD)], "ok": True, "error": None}]   # no bad name
    assert "2 VM name" in caplog.text
    assert "lookup" not in caplog.text and "touch" not in caplog.text and "a b" not in caplog.text
