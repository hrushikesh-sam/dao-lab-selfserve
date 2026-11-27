"""B-109c: the runner side of the end-of-slot save (service/runner.py).

  * RESIDUE_KINDS gains home, tripwire, restore_incomplete (§5.7), each with a CHECK_FOR_KIND step
  * MockRunner.capture / wipe, and DAOLAB_MOCK_FAIL=capture; verify_clean reports home:lab
    only for a snapshot-enabled reservation whose home was not wiped
  * AnsibleRunner.capture / wipe run snapshot_capture.yml / home_wipe.yml with exactly the
    snapshot extra vars; verify_clean passes daolab_snapshot_enabled only when the setup has it
  * flag off: the extra vars, the steps and the residue are exactly today's
"""
import hashlib
import json
import os
import types

import pytest

from service import runner
from service.runner import (CLEANUP_STEPS, VERIFY_CLEAN_STEPS, AnsibleRunner, MockRunner,
                            RunResult)
from tests.test_ansible_runner import Steps, settings  # noqa: F401  (fixture)

PID = "0123456789abcdef0123456789abcdef"
RCPT = "age1" + "q" * 58
RES = {"id": 4, "job_id": 9, "job_created_at": "2026-09-01T09:00:00Z", "vms": [], "leases": []}
S_CANARY = "AGE-SECRET-KEY-1" + "Q" * 58


class Sink:
    def __init__(self):
        self.calls = []

    def __call__(self, name, state="ok", detail=None):
        self.calls.append((name, state, detail))

    @property
    def names(self):
        return [c[0] for c in self.calls]


# ---- residue kinds (§5.7) ---------------------------------------------------------------------

def test_the_snapshot_residue_kinds_exist_and_parse():
    for kind in ("home", "tripwire", "restore_incomplete"):
        assert kind in runner.RESIDUE_KINDS and kind in runner.CHECK_FOR_KIND
    assert runner.residue_item("home", "lab") == "home:lab"
    assert runner.parse_residue("home:lab") == ("home", "lab")
    assert runner.parse_residue("tripwire:sudo:2") == ("tripwire", "sudo:2")
    assert runner.parse_residue("restore_incomplete:" + PID) == ("restore_incomplete", PID)
    assert runner.CHECK_FOR_KIND["home"] == "check_home"


def test_mock_fail_accepts_capture():
    assert runner.parse_mock_fail("capture") == frozenset({"capture"})
    assert runner.parse_mock_fail("cleanup,capture") == frozenset({"cleanup", "capture"})
    with pytest.raises(runner.RunnerError):
        runner.parse_mock_fail("wipe")


# ---- MockRunner -------------------------------------------------------------------------------

def test_mock_capture_writes_a_ciphertext_that_matches_its_report(tmp_path):
    m, sink = MockRunner(), Sink()
    dest = tmp_path / f"{PID}.age.part"
    result, report = m.capture(RES, sink, recipient=RCPT, public_id=PID, cap_bytes=1 << 20,
                               fetch_to=str(dest))
    data = dest.read_bytes()
    assert result.ok and report["status"] == "saved" and report["reason"] == "ok"
    assert report["age_bytes"] == len(data)
    assert report["age_sha256"] == hashlib.sha256(data).hexdigest()
    assert sink.names == ["capture", "fetch_snapshot"] and m.captured == [PID]


def test_mock_capture_fail_switch_refuses_and_fetches_nothing(tmp_path):
    m, sink = MockRunner(fail={"capture"}), Sink()
    dest = tmp_path / f"{PID}.age.part"
    result, report = m.capture(RES, sink, recipient=RCPT, public_id=PID, cap_bytes=1,
                               fetch_to=str(dest))
    assert not result.ok and report["status"] == "refused" and report["reason"] == "tar_failed"
    assert not dest.exists() and sink.calls[0][:2] == ("capture", "failed")


def test_mock_capture_of_an_empty_home(tmp_path):
    m, sink = MockRunner(), Sink()
    m.empty_home = True
    dest = tmp_path / f"{PID}.age.part"
    result, report = m.capture(RES, sink, recipient=RCPT, public_id=PID, cap_bytes=1,
                               fetch_to=str(dest))
    assert result.ok and report == {"status": "empty", "reason": "empty_home", "raw_bytes": 0,
                                    "age_bytes": 0, "age_sha256": ""}
    assert not dest.exists()


@pytest.mark.parametrize("over", [{"recipient": S_CANARY}, {"recipient": "age1x"},
                                  {"public_id": PID.upper()}, {"fetch_to": "relative.part"}])
def test_mock_capture_refuses_bad_inputs_like_the_playbook(tmp_path, over):
    kw = {"recipient": RCPT, "public_id": PID, "cap_bytes": 1,
          "fetch_to": str(tmp_path / f"{PID}.age.part"), **over}
    result, report = MockRunner().capture(RES, Sink(), **kw)
    assert not result.ok and report is None


def test_mock_wipe_only_when_allowed():
    m = MockRunner()
    for allowed in (False, None, "true", 1):
        assert not m.wipe(RES, Sink(), allowed=allowed).ok
    assert m.wiped == set()
    sink = Sink()
    assert m.wipe(RES, sink, allowed=True).ok and m.wiped == {RES["id"]}
    assert sink.names == ["wipe_home"]


def test_mock_verify_reports_the_home_only_for_an_enabled_setup():
    m = MockRunner()
    off, on = Sink(), Sink()
    assert m.verify_clean(RES, off).ok and off.names == list(VERIFY_CLEAN_STEPS)
    r = m.verify_clean({**RES, "snapshot_enabled": True}, on)
    assert r.residue == ("home:lab",) and on.names == list(VERIFY_CLEAN_STEPS) + ["check_home"]
    assert on.calls[-1][:2] == ("check_home", "failed")
    m.wipe(RES, Sink(), allowed=True)
    assert m.verify_clean({**RES, "snapshot_enabled": True}, Sink()).ok


def test_mock_flag_off_steps_are_todays():
    m, sink = MockRunner(), Sink()
    m.cleanup(RES, sink)
    m.verify_clean(RES, sink)
    assert sink.names == list(CLEANUP_STEPS) + list(VERIFY_CLEAN_STEPS)


# ---- AnsibleRunner ----------------------------------------------------------------------------

class FakeRun:
    def __init__(self, output=None, status="successful", rc=0, write=None):
        self.output, self.status, self.rc, self.write = output, status, rc, write
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.write:
            self.write(kw)
        if self.output is not None:
            with open(kw["extravars"]["daolab_output_file"], "w") as fh:
                json.dump(self.output, fh)
        return types.SimpleNamespace(status=self.status, rc=self.rc)


def test_capture_runs_snapshot_capture_yml_with_only_the_snapshot_extras(settings, tmp_path):
    report = {"status": "saved", "reason": "ok", "raw_bytes": 1, "age_bytes": 1, "age_sha256": "a" * 64}
    fake = FakeRun(output=report)
    dest = str(tmp_path / f"{PID}.age.part")
    result, out = AnsibleRunner(settings, run=fake).capture(
        RES, Steps(), recipient=RCPT, public_id=PID, cap_bytes=4096, fetch_to=dest)
    assert result.ok and out == report
    kw = fake.calls[0]
    assert kw["playbook"] == "snapshot_capture.yml" and kw["ident"] == "snapshot_capture"
    ev = kw["extravars"]
    assert {k: ev[k] for k in ev if k.startswith("daolab_snapshot_")} == {
        "daolab_snapshot_recipient": RCPT, "daolab_snapshot_public_id": PID,
        "daolab_snapshot_cap_bytes": 4096, "daolab_snapshot_fetch_to": dest}
    assert "daolab_wipe_allowed" not in ev and "daolab_snapshot_enabled" not in ev


@pytest.mark.parametrize("over", [{"recipient": S_CANARY}, {"public_id": "x" * 32},
                                  {"fetch_to": "relative.part"}, {"cap_bytes": 0}])
def test_capture_never_runs_with_bad_inputs(settings, tmp_path, over):
    fake = FakeRun()
    kw = {"recipient": RCPT, "public_id": PID, "cap_bytes": 1,
          "fetch_to": str(tmp_path / f"{PID}.age.part"), **over}
    result, out = AnsibleRunner(settings, run=fake).capture(RES, Steps(), **kw)
    assert not result.ok and out is None and fake.calls == []


def test_a_refused_capture_comes_back_with_its_report(settings, tmp_path):
    report = {"status": "refused", "reason": "home_owner", "raw_bytes": 0, "age_bytes": 0,
              "age_sha256": ""}
    fake = FakeRun(output=report, status="failed", rc=2)
    result, out = AnsibleRunner(settings, run=fake).capture(
        RES, Steps(), recipient=RCPT, public_id=PID, cap_bytes=1,
        fetch_to=str(tmp_path / f"{PID}.age.part"))
    assert not result.ok and out == report


@pytest.mark.parametrize("allowed,sent", [(True, True), (False, False), ("true", False), (1, False)])
def test_wipe_passes_the_gate_as_a_strict_bool(settings, allowed, sent):
    fake = FakeRun(output={"status": "wiped", "reason": "ok", "removed": 0})
    AnsibleRunner(settings, run=fake).wipe(RES, Steps(), allowed=allowed)
    kw = fake.calls[0]
    assert kw["playbook"] == "home_wipe.yml" and kw["extravars"]["daolab_wipe_allowed"] is sent


def test_verify_clean_passes_the_flag_only_for_an_enabled_setup(settings):
    off, on = FakeRun(output=[]), FakeRun(output=["home:lab"])
    assert AnsibleRunner(settings, run=off).verify_clean(RES, Steps()).ok
    assert "daolab_snapshot_enabled" not in off.calls[0]["extravars"]
    r = AnsibleRunner(settings, run=on).verify_clean({**RES, "snapshot_enabled": True}, Steps())
    assert on.calls[0]["extravars"]["daolab_snapshot_enabled"] is True
    assert r.residue == ("home:lab",)


def test_the_extravars_of_a_flag_off_reservation_are_unchanged(settings):
    rn = AnsibleRunner(settings, run=FakeRun())
    assert rn.extravars(RES) == rn.extravars({**RES, "snapshot_enabled": True})
    assert not any(k.startswith(("daolab_snapshot", "daolab_wipe")) for k in rn.extravars(RES))


def test_the_new_playbooks_exist(settings):
    for key in ("snapshot_capture", "home_wipe"):
        path = os.path.join(settings.playbooks_dir, AnsibleRunner.PLAYBOOKS[key])
        assert os.path.isfile(path), path
