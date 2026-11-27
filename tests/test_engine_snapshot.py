"""B-109c: the end-of-slot save wired into the engine's cleanup (and reconcile) jobs.

Design v1.1 §6 save steps 1-9 + the failure list; DDL v4.1 §3.5/§4.3; interface §5.6-§5.8.
With setups.snapshot_enabled = 1 a cleanup job runs:

    revoke + capture (snapshot_capture.yml) -> `saved` committed -> cleanup.yml -> gated wipe
    (home_wipe.yml, only when the DB row is `saved` or the home was empty) -> verify_clean with
    the home check (+ the reconcile loop) -> retention prune -> the usual finish

and a reconcile job (force-clean) runs the same save first, so a failed capture is retried on the
same row (D-7-4). Every failure keeps the home: no wipe, `home:lab` residue, quarantine.
With snapshot_enabled = 0 nothing of this runs (flag-off parity).

The mock runner stands in for the playbooks; agecli is faked with an S-shaped canary (see
test_snapshots.py), except in the two requires_age tests at the end (canary through the real
age, and the real playbooks end to end through the Lab harness).
"""
import dataclasses
import gzip
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import types
from datetime import datetime, timedelta, timezone

import pytest

from service import agecli, api, config, engine, hostcheck, keys, models, runner, snapshots
from service.runner import CLEANUP_STEPS, VERIFY_CLEAN_STEPS, AnsibleRunner, MockRunner, RunResult
from service.scheduler import SchedConfig
from tests.conftest import NOW
from tests.test_snapshots import (FAKE_S, S_RECIPIENT, World, fake_age, files_under,  # noqa: F401
                                  mk_key, saved_row, snap_settings, world)

CANARY = FAKE_S.strip()


class Crash(BaseException):
    """A crash: not an Exception, so engine._call can't turn it into a failed result."""


class Spy(MockRunner):
    """The mock runner, recording the order of the procedures and the snapshot row's state
    when the apps cleanup and the wipe start. `crash_at` raises Crash at one point."""

    def __init__(self, db, rid, **kw):
        super().__init__(**kw)
        self.db, self.rid = db, rid
        self.order, self.crash_at, self.ctxs = [], None, []
        self.at_cleanup = self.at_wipe = None

    def _state(self):
        row = self.db.execute("SELECT state FROM snapshots WHERE reservation_id = ?",
                              (self.rid,)).fetchone()
        return row["state"] if row else None

    def capture(self, res, step, **kw):
        self.order.append("capture")
        if self.crash_at == "key":
            raise Crash
        out = super().capture(res, step, **kw)
        if self.crash_at == "fetch":
            raise Crash
        return out

    def cleanup(self, res, step):
        self.order.append("cleanup")
        self.at_cleanup = self._state()
        if self.crash_at == "saved":
            raise Crash
        return super().cleanup(res, step)

    def reconcile(self, res, step):
        self.order.append("reconcile")
        return super().reconcile(res, step)

    def wipe(self, res, step, *, allowed):
        self.order.append("wipe")
        self.at_wipe = self._state()
        if self.crash_at == "wipe":
            raise Crash
        return super().wipe(res, step, allowed=allowed)

    def verify_clean(self, res, step):
        self.order.append("verify")
        self.ctxs.append(dict(res))
        return super().verify_clean(res, step)


def tick(db, rn, now=NOW, **kw):
    return engine.tick(db, now, rn, SchedConfig(), **kw)


def later():
    """A clock past the job timeout of anything recorded now (updated_at is wall time)."""
    return datetime.now(timezone.utc) + timedelta(hours=2)


def audits(db, action):
    return [json.loads(a["detail"]) for a in reversed(models.list_audit_events(db, 500))
            if a["action"] == action]


def res_state(w):
    return w.res()["state"]


def availability(w):
    return w.db.execute("SELECT availability FROM setups WHERE id = ?", (w.sid,)).fetchone()[0]


def db_dump(db):
    return "\n".join(db.iterdump())


def three_files(w, pid):
    return sorted([f"u{w.uid}/{pid}.age", f"u{w.uid}/{pid}.hdr.json", f"u{w.uid}/{pid}.key.age"])


@pytest.fixture
def spy(world):
    return Spy(world.db, world.rid)


# ---- the happy path ---------------------------------------------------------------------------

def test_enabled_key_and_save_capture_commit_saved_then_clean_then_wipe(world, spy, fake_age):
    jid = models.create_job(world.db, world.rid, "cleanup")
    out = tick(world.db, spy)
    assert res_state(world) == "released" and out.released == 1
    assert spy.order == ["capture", "cleanup", "wipe", "verify"]
    assert spy.at_cleanup == "saved" and spy.at_wipe == "saved"   # saved BEFORE anything else
    [row] = world.rows()
    assert row["state"] == "saved" and row["key_id"] == world.key_id
    assert models.job_by_id(world.db, jid)["snapshot_id"] == row["id"]
    pid = row["public_id"]
    assert files_under(world.root) == three_files(world, pid)
    for f in files_under(world.root):
        assert stat.S_IMODE(os.lstat(os.path.join(world.root, f)).st_mode) == 0o600
    for d in (world.root, world.udir(), os.path.join(world.udir(), ".incoming")):
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
    p = snapshots.paths(world.root, world.uid, pid)
    with open(p["hdr"]) as fh:
        hdr = json.load(fh)
    assert (hdr["format"], hdr["compression"], hdr["public_id"], hdr["s_recipient"]) == \
        ("daolab-snapshot/1", "gzip", pid, S_RECIPIENT)
    import hashlib
    with open(p["age"], "rb") as fh:
        assert hdr["age_sha256"] == hashlib.sha256(fh.read()).hexdigest()
    with open(p["key"], "rb") as fh:
        assert fh.read().startswith(b"age-wrapped:")          # S wrapped to the owner's key
    assert fake_age[:3] == ["swap", "keygen", ("encrypt_to", world.key()["recipient"])]
    steps = [s["name"] for s in models.job_steps(models.job_by_id(world.db, jid))]
    assert steps == ["capture", "fetch_snapshot", *CLEANUP_STEPS, "wipe_home",
                     *VERIFY_CLEAN_STEPS, "check_home"]
    [cap] = audits(world.db, "snapshot_capture")
    assert cap == {"job": jid, "snapshot_id": row["id"], "public_id": pid, "status": "saved",
                   "reason": "ok", "raw_bytes": cap["raw_bytes"]} and cap["raw_bytes"] > 0
    [cl] = audits(world.db, "cleanup")
    assert (cl["snapshot"], cl["wiped"]) == ("saved", True)
    assert world.root not in db_dump(world.db)                  # no paths in the DB
    assert CANARY.decode() not in db_dump(world.db)


def test_the_capture_gets_only_s_recipient_public_id_cap_and_the_fetch_path(world, fake_age):
    seen = {}

    class Rec(Spy):
        def capture(self, res, step, **kw):
            seen.update(kw)
            return super().capture(res, step, **kw)

    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, Rec(world.db, world.rid))
    pid = world.rows()[0]["public_id"]
    assert seen == {"recipient": S_RECIPIENT, "public_id": pid, "cap_bytes": 1 << 20,
                    "fetch_to": snapshots.incoming_path(os.path.abspath(world.root), world.uid, pid)}


# ---- no capture: keep the home, quarantine -----------------------------------------------------

@pytest.mark.parametrize("save,key,why", [(0, True, "save_off"), (1, False, "no_key")])
def test_without_save_or_an_active_key_nothing_is_captured_or_wiped(db, snap_settings, fake_age,
                                                                    save, key, why):
    w = World(db, snap_settings.snapshot_root, save=save, key=key)
    s = Spy(db, w.rid)
    models.create_job(db, w.rid, "cleanup")
    out = tick(db, s)
    assert "capture" not in s.order and "wipe" not in s.order
    assert res_state(w) == "quarantined" and out.quarantined == 1
    assert availability(w) == "quarantined"
    job = models.jobs_for_reservation(db, w.rid)[-1]
    assert job["state"] == "failed" and "home:lab" in job["error"]
    assert w.rows() == [] and "keygen" not in fake_age
    [cap] = audits(db, "snapshot_capture")
    assert (cap["status"], cap["reason"], cap["snapshot_id"]) == ("skipped", why, None)


# ---- a failed capture: keep the home, quarantine ------------------------------------------------

class Report(Spy):
    """The capture runs (and writes its file) but reports `self.doc` instead."""
    doc = None

    def capture(self, res, step, **kw):
        result, report = super().capture(res, step, **kw)
        return result, self.doc(report) if callable(self.doc) else self.doc


@pytest.mark.parametrize("make,why", [
    (lambda r: {**r, "age_sha256": "0" * 64}, "sha_mismatch"),
    (lambda r: {**r, "age_bytes": r["age_bytes"] + 1}, "size_mismatch"),
    (lambda r: None, "bad_report"),
    (lambda r: {**r, "status": "done"}, "bad_report"),
    (lambda r: {**r, "extra": "x"}, "bad_report"),
])
def test_a_bad_capture_result_fails_the_row_and_keeps_the_home(world, fake_age, make, why):
    s = Report(world.db, world.rid)
    s.doc = make
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, s)
    assert "wipe" not in s.order and res_state(world) == "quarantined"
    [row] = world.rows()
    assert row["state"] == "failed"
    assert files_under(world.root) == []                     # the attempt's files went
    assert audits(world.db, "snapshot_capture")[-1]["reason"] == why


def test_a_refused_capture_fails_the_row_and_keeps_the_home(world, fake_age):
    s = Spy(world.db, world.rid, fail={"capture"})
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, s)
    assert "wipe" not in s.order and res_state(world) == "quarantined"
    assert world.rows()[0]["state"] == "failed" and files_under(world.root) == []
    cap = audits(world.db, "snapshot_capture")[-1]
    assert (cap["status"], cap["reason"]) == ("refused", "tar_failed")


def test_a_runner_without_capture_fails_closed(world, fake_age):
    class Old(MockRunner):
        capture = None
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, Old())
    assert res_state(world) == "quarantined" and world.rows() == []
    assert audits(world.db, "snapshot_capture")[-1]["reason"] == "no_runner"


@pytest.mark.parametrize("break_it", ["storage", "swap", "age"])
def test_controller_side_failures_fail_closed_before_any_capture(world, fake_age, monkeypatch,
                                                                break_it):
    if break_it == "storage":
        monkeypatch.setattr(snapshots.shutil, "disk_usage",
                            lambda _p: types.SimpleNamespace(free=1))
        want = "storage_low"
    elif break_it == "swap":
        def unsafe(*a, **k):
            raise hostcheck.HostCheckError(hostcheck.UNSAFE)
        monkeypatch.setattr(hostcheck, "require_safe_swap", unsafe)
        want = "swap_unsafe"
    else:
        def broken():
            raise agecli.AgeError(agecli.KEYGEN_FAILED)
        monkeypatch.setattr(agecli, "keygen", broken)
        want = "age_error"
    s = Spy(world.db, world.rid)
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, s)
    assert "capture" not in s.order and "wipe" not in s.order
    assert world.rows()[0]["state"] == "failed" and files_under(world.root) == []
    assert res_state(world) == "quarantined"
    assert audits(world.db, "snapshot_capture")[-1]["reason"] == want
    if break_it != "age":
        assert "keygen" not in fake_age                       # S was never made


# ---- an empty home ----------------------------------------------------------------------------

def test_an_empty_home_deletes_the_row_and_allows_the_wipe(world, spy, fake_age):
    spy.empty_home = True
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, spy)
    assert world.rows() == [] and files_under(world.root) == []
    assert spy.order == ["capture", "cleanup", "wipe", "verify"]
    assert res_state(world) == "released"
    cap = audits(world.db, "snapshot_capture")[-1]
    assert (cap["status"], cap["reason"]) == ("empty", "empty_home")


# ---- crash points: the home stays unless the row is saved --------------------------------------

@pytest.mark.parametrize("point,saved_before", [
    ("begin", False), ("key", False), ("fetch", False), ("replace", False),
    ("saved", True), ("wipe", True)])
def test_a_crash_at_any_point_never_wipes_an_unsaved_home(world, spy, fake_age, monkeypatch,
                                                         point, saved_before):
    armed = {"on": True}

    def trap(real):
        def f(*a, **k):
            if armed["on"]:
                raise Crash
            return real(*a, **k)
        return f

    if point == "begin":
        monkeypatch.setattr(snapshots, "wrap_new_identity", trap(snapshots.wrap_new_identity))
    elif point == "replace":
        monkeypatch.setattr(snapshots, "mark_saved", trap(snapshots.mark_saved))
    else:
        spy.crash_at = point
    models.create_job(world.db, world.rid, "cleanup")
    with pytest.raises(Crash):
        tick(world.db, spy)
    assert spy.wiped == set()                                  # the home is intact
    [row] = world.rows()
    assert row["state"] == ("saved" if saved_before else "capturing")
    first = row["public_id"]

    armed["on"], spy.crash_at = False, None                    # the engine comes back
    tick(world.db, spy, now=later(), job_timeout_sec=60)       # requeue + stale + replay
    assert res_state(world) == "released" and spy.wiped == {world.rid}
    [row2] = world.rows()
    assert row2["id"] == row["id"] and row2["state"] == "saved"
    if saved_before:
        assert row2["public_id"] == first and spy.order.count("capture") == 1
    else:
        assert row2["public_id"] != first                     # D-7-4: same row, new public_id
    assert files_under(world.root) == three_files(world, row2["public_id"])
    assert CANARY.decode() not in db_dump(world.db)


def test_a_stale_capturing_row_becomes_failed_on_the_next_tick(world, fake_age):
    jid = models.create_job(world.db, world.rid, "cleanup")
    snapshots.begin_capture(world.db, world.res(), world.key(), job_id=jid)
    models.update(world.db, "jobs", jid, {"state": "failed", "finished_at": models.utcnow()})
    tick(world.db, MockRunner())
    assert world.rows()[0]["state"] == "failed"


# ---- retention --------------------------------------------------------------------------------

def test_the_fourth_save_prunes_the_oldest_unheld_one(world, spy, fake_age):
    old = [saved_row(world, f"2026-0{m}-01T00:00:00Z") for m in (1, 2, 3)]
    held = saved_row(world, "2025-01-01T00:00:00Z", held=True)
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, spy)
    left = {r["id"] for r in world.rows()}
    new = [r for r in world.rows() if r["reservation_id"] == world.rid][0]
    assert left == {old[1][0], old[2][0], held[0], new["id"]}
    assert not any(old[0][1] in f for f in files_under(world.root))
    [d] = audits(world.db, "snapshot_delete")
    assert d["snapshot_id"] == old[0][0] and d["public_id"] == old[0][1]


# ---- force-clean: the reconcile job ------------------------------------------------------------

def _force_clean(w):
    admin = models.user_by_id(w.db, w.admin)
    setup = w.db.execute("SELECT name FROM setups WHERE id = ?", (w.sid,)).fetchone()["name"]
    return api.force_clean(setup, None, admin=admin, conn=w.db)


@pytest.mark.parametrize("save,key", [(0, True), (1, False)])
def test_force_clean_of_a_kept_home_still_reports_it_and_does_not_release(db, snap_settings,
                                                                          fake_age, save, key):
    w = World(db, snap_settings.snapshot_root, save=save, key=key)
    models.create_job(db, w.rid, "cleanup")
    tick(db, Spy(db, w.rid))
    assert res_state(w) == "quarantined" and availability(w) == "quarantined"
    assert _force_clean(w)["jobs"]                              # a reconcile job is queued
    s = Spy(db, w.rid)
    tick(db, s)
    rec = models.jobs_for_reservation(db, w.rid, kind="reconcile")[-1]
    assert rec["state"] == "failed" and "home:lab" in rec["error"]
    assert res_state(w) == "quarantined" and availability(w) == "quarantined"   # NOT released
    assert "wipe" not in s.order and "capture" not in s.order
    assert s.ctxs[-1].get("snapshot_enabled") is True          # the home was checked


def test_force_clean_retries_a_failed_capture_on_the_same_row(world, fake_age):
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, Spy(world.db, world.rid, fail={"capture"}))
    [failed] = world.rows()
    assert failed["state"] == "failed" and res_state(world) == "quarantined"
    _force_clean(world)
    s = Spy(world.db, world.rid)
    tick(world.db, s)
    [row] = world.rows()
    assert (row["id"], row["state"]) == (failed["id"], "saved")
    assert row["public_id"] != failed["public_id"]
    assert s.order == ["capture", "reconcile", "wipe", "verify"]
    assert res_state(world) == "released" and availability(world) == "available"


def test_force_clean_after_a_saved_capture_wipes_again_without_recapturing(world, fake_age):
    class NoWipe(Spy):                      # the first wipe fails: home:lab, quarantine
        def wipe(self, res, step, *, allowed):
            self.order.append("wipe")
            return RunResult(ok=False, error="wipe_home: boom")
    models.create_job(world.db, world.rid, "cleanup")
    tick(world.db, NoWipe(world.db, world.rid))
    assert res_state(world) == "quarantined" and world.rows()[0]["state"] == "saved"
    _force_clean(world)
    s = Spy(world.db, world.rid)
    tick(world.db, s)
    assert s.order == ["reconcile", "wipe", "verify"] and res_state(world) == "released"


# ---- flag-off parity ---------------------------------------------------------------------------

class Forbidden(MockRunner):
    def capture(self, *a, **k):
        raise AssertionError("capture called with snapshots off")

    def wipe(self, *a, **k):
        raise AssertionError("wipe called with snapshots off")

    def verify_clean(self, res, step):
        assert "snapshot_enabled" not in res
        return super().verify_clean(res, step)


def test_flag_off_never_calls_capture_or_wipe(db, snap_settings, fake_age):
    w = World(db, snap_settings.snapshot_root, enabled=0, save=1, key=True)
    jid = models.create_job(db, w.rid, "cleanup")
    out = tick(db, Forbidden())
    assert res_state(w) == "released" and out.released == 1
    steps = [s["name"] for s in models.job_steps(models.job_by_id(db, jid))]
    assert steps == list(CLEANUP_STEPS) + list(VERIFY_CLEAN_STEPS)
    assert w.rows() == [] and audits(db, "snapshot_capture") == [] and fake_age == []
    [cl] = audits(db, "cleanup")
    assert set(cl) == {"job", "attempt", "ok", "cleanup_ok", "reconcile_attempts", "residue", "error"}
    assert not os.path.exists(snap_settings.snapshot_root)


def test_flag_off_reconcile_is_todays(db, snap_settings, fake_age):
    w = World(db, snap_settings.snapshot_root, enabled=0, state="quarantined")
    models.create_job(db, w.rid, "reconcile")
    tick(db, Forbidden())
    assert res_state(w) == "released" and w.rows() == [] and fake_age == []


# ---- rule 8: the canary through the real age ---------------------------------------------------

def _age_tool(var, default):
    return shutil.which(os.environ.get(var) or default)


def _identity(path):
    keygen = _age_tool("DAOLAB_AGE_KEYGEN_BIN", "age-keygen")
    subprocess.run([keygen, "-o", str(path)], check=True, capture_output=True)
    return subprocess.run([keygen, "-y", str(path)], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.requires_age
def test_the_fake_s_reaches_only_ages_stdin(db, snap_settings, tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    owner_rcpt = _identity(tmp_path / "owner.txt")
    s_rcpt = _identity(tmp_path / "s-stand-in.txt")
    w = World(db, snap_settings.snapshot_root, key=False)
    w.key_id = mk_key(db, w.uid, w.admin, rcpt=owner_rcpt)
    monkeypatch.setattr(hostcheck, "require_safe_swap", lambda *a, **k: None)
    monkeypatch.setattr(agecli, "keygen", lambda: (FAKE_S, s_rcpt))
    procs = []
    real_run = subprocess.run

    def recording_run(argv, **kw):
        procs.append({"argv": list(argv), "env": kw.get("env"), "input": kw.get("input")})
        return real_run(argv, **kw)

    monkeypatch.setattr(agecli.subprocess, "run", recording_run)
    jobs_dir = tmp_path / "jobs"
    s = dataclasses.replace(config.settings, artifacts_dir=str(jobs_dir), internal_secret="",
                            deploy_vms=False)
    monkeypatch.setattr(config, "settings", s)
    sent = []

    def fake_run(**kw):
        sent.append(kw)
        ev = kw["extravars"]
        out = None
        if kw["playbook"] == "snapshot_capture.yml":
            data = b"sealed-payload-for-the-test"
            with open(ev["daolab_snapshot_fetch_to"], "wb") as fh:
                fh.write(data)
            import hashlib
            out = {"status": "saved", "reason": "ok", "raw_bytes": 99, "age_bytes": len(data),
                   "age_sha256": hashlib.sha256(data).hexdigest()}
        elif kw["playbook"] == "home_wipe.yml":
            out = {"status": "wiped", "reason": "ok", "removed": 1}
        elif kw["playbook"] == "verify_clean.yml":
            out = []
        if out is not None:
            with open(ev["daolab_output_file"], "w") as fh:
                json.dump(out, fh)
        return types.SimpleNamespace(status="successful", rc=0)

    models.create_job(db, w.rid, "cleanup")
    tick(db, AnsibleRunner(s, run=fake_run))
    assert res_state(w) == "released" and w.rows()[0]["state"] == "saved"
    hits = []
    for dirpath, _d, names in os.walk(jobs_dir):
        for n in names:
            with open(os.path.join(dirpath, n), "rb") as fh:
                if CANARY in fh.read():
                    hits.append("job dir")
    for root, _d, names in os.walk(w.root):
        for n in names:
            with open(os.path.join(root, n), "rb") as fh:
                if CANARY in fh.read():
                    hits.append("snapshot_root plaintext")
    if CANARY.decode() in json.dumps([{"extravars": k["extravars"], "envvars": k["envvars"]}
                                      for k in sent], default=str):
        hits.append("extravars/envvars")
    if CANARY.decode() in caplog.text:
        hits.append("log")
    if CANARY.decode() in db_dump(db):
        hits.append("db")
    if any(CANARY.decode() in v for v in os.environ.values()):
        hits.append("environ")
    for p in procs:
        if any(CANARY.decode() in str(a) for a in p["argv"]) or CANARY.decode() in str(p["env"]):
            hits.append("argv/env")
    assert hits == []
    fed = [p["argv"][1:] for p in procs if p["input"] and CANARY in p["input"]]
    assert fed == [["-r", owner_rcpt]]                         # only into `age -r <owner>`
    # and key.age is S wrapped to the owner: the owner's identity opens it
    with open(tmp_path / "owner.txt", "rb") as fh:
        owner = fh.read()
    pid = w.rows()[0]["public_id"]
    opened = agecli.decrypt_file(owner, snapshots.paths(w.root, w.uid, pid)["key"])
    assert opened == FAKE_S, "key.age does not hold the generated S"


# ---- end to end through the real playbooks -----------------------------------------------------

@pytest.mark.requires_age
def test_end_to_end_with_the_real_playbooks_and_age(db, snap_settings, tmp_path, monkeypatch):
    from tests.test_playbooks import ANSIBLE, REPO, Lab
    from tests.test_playbooks_snapshot import Snap, names
    if not ANSIBLE:
        pytest.skip("ansible-playbook not installed")
    (tmp_path / "lab").mkdir()
    lab = Lab(tmp_path / "lab")
    snap = Snap(lab)
    snap.plant()
    snap.link_age()
    expected = names(snap.home)
    owner_rcpt = _identity(tmp_path / "owner.txt")
    w = World(db, snap_settings.snapshot_root, key=False)
    w.key_id = mk_key(db, w.uid, w.admin, rcpt=owner_rcpt)
    monkeypatch.setattr(hostcheck, "require_safe_swap", lambda *a, **k: None)
    s = dataclasses.replace(config.settings, artifacts_dir=str(tmp_path / "jobs"), internal_secret="",
                            playbooks_dir=os.path.join(REPO, "playbooks"))
    monkeypatch.setattr(config, "settings", s)

    def through_the_lab(**kw):
        ev = {k: v for k, v in kw["extravars"].items()
              if k not in ("daolab_members", "daolab_vms", "daolab_leases")}
        rc = lab.run(kw["playbook"], **{**ev, **snap.common()})
        return types.SimpleNamespace(status="successful" if rc == 0 else "failed", rc=rc)

    class Hybrid(AnsibleRunner):
        """The real capture, wipe and verify playbooks; apps cleanup is covered elsewhere."""
        reconcile = None

        def cleanup(self, res, step):
            return RunResult(ok=True)

    models.create_job(db, w.rid, "cleanup")
    tick(db, Hybrid(s, run=through_the_lab))
    assert res_state(w) == "released", lab.stdout[-3000:]
    [row] = w.rows()
    p = snapshots.paths(w.root, w.uid, row["public_id"])
    assert row["state"] == "saved" and all(os.path.isfile(x) for x in p.values())
    with open(tmp_path / "owner.txt", "rb") as fh:
        owner = fh.read()
    s_identity = agecli.decrypt_file(owner, p["key"])                       # owner -> S
    ok = agecli.IDENTITY_RE.fullmatch(s_identity.decode().strip()) is not None
    assert ok, "key.age did not hold an age identity"
    plain = agecli.decrypt_file(s_identity, p["age"])                        # S -> payload
    del s_identity
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(plain))) as tf:    # gunzip -> tar
        listed = {os.path.normpath(m.name) for m in tf.getmembers()} - {"."}
    assert listed == expected
    assert names(snap.home) == names(snap.skel)                              # wiped after saved
    with open(p["hdr"]) as fh:
        hdr = json.load(fh)
    assert (hdr["setup"], hdr["usecase"], hdr["compression"]) == ("tb", "uc", "gzip")
