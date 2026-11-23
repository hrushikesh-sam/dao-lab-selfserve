"""P-0c (EXTRAVAR-MATRIX row 25): a VM name read back from Xen Orchestra reaches ansible as the extra var
`daolab_vm_names`, and ansible re-templates extra vars. Only names that fullmatch
daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127} may travel; the rest are dropped and only their COUNT is logged."""
import json
import logging

import pytest

from service import engine, models
from service.runner import AnsibleRunner, MockRunner
from tests.test_ansible_runner import FakeRun, Steps, settings  # noqa: F401  (fixture)
from tests.test_orphans import lab, sweeps, tick  # noqa: F401  (fixture)

PAYLOAD = "{{ lookup('pipe', 'touch /tmp/p0c-canary') }}"
POISON = "daolab-r999-" + PAYLOAD
GOOD = "daolab-r999-client"

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


def _extravars(fake):
    return [json.dumps(c["extravars"], default=str) for c in fake.calls]


def test_the_runner_lists_only_valid_names_and_logs_a_count_only(settings, caplog):
    fake = FakeRun(output=[GOOD, POISON, "daolab-r1-a b"])
    with caplog.at_level(logging.DEBUG):
        assert AnsibleRunner(settings, run=fake).tagged_vms() == [GOOD]
    assert "2 listed VM name" in caplog.text
    assert "lookup" not in caplog.text and "touch" not in caplog.text and "daolab-r1-a b" not in caplog.text


def test_destroy_hands_ansible_no_payload_and_logs_no_name(settings, caplog):
    fake = FakeRun()
    with caplog.at_level(logging.DEBUG):
        assert AnsibleRunner(settings, run=fake).destroy_vms([GOOD, POISON], Steps()).ok
    [kw] = fake.calls
    assert kw["extravars"]["daolab_vm_names"] == [GOOD]
    assert all("lookup" not in e and "{{" not in e and "touch" not in e for e in _extravars(fake))
    assert "1 VM name" in caplog.text
    assert "lookup" not in caplog.text and "touch" not in caplog.text and POISON not in caplog.text


def test_destroy_with_only_invalid_names_runs_nothing(settings):
    fake = FakeRun()
    r = AnsibleRunner(settings, run=fake).destroy_vms([POISON, "daolab-r1-$(id)"], Steps())
    assert r.ok is False and fake.calls == []
    assert "lookup" not in (r.error or "") and "$(id)" not in (r.error or "")


def test_the_sweep_skips_invalid_names_before_the_runner_and_logs_a_count_only(lab, caplog):
    seen = []

    class Hostile(MockRunner):
        def tagged_vms(self):
            return [POISON, GOOD, "daolab-r999-a b"]          # r999: no such reservation, so an orphan

        def destroy_vms(self, names, step):
            seen.append(list(names))
            return super().destroy_vms(names, step)

    rn = Hostile()
    rn.vms.add(GOOD)
    with caplog.at_level(logging.DEBUG):
        out = tick(lab, rn)
    assert seen == [[GOOD]] and out.orphan_vms == 1 and rn.vms == set()
    assert sweeps(lab) == [{"vms": [GOOD], "ok": True, "error": None}]          # the audit row has no bad name either
    assert "2 VM name" in caplog.text
    assert "lookup" not in caplog.text and "touch" not in caplog.text and "a b" not in caplog.text
