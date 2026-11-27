"""B-109c: service/snapshots.py, the controller side of the end-of-slot save (unit tests).

DDL v4.1 §3.5 (states, retention, D-7-4 row reuse), §4.1 (S on the controller), §4.3 (writers);
interface §5.6 / §5.8. The shared helpers here (a world with an owner, an admin-activated key, a
snapshot-enabled setup and a reservation; a fake age whose S is the S-shaped canary) are used by
test_engine_snapshot.py too.

Fake S: "AGE-SECRET-KEY-1" + 58 * "Q", built at run time. The fake `encrypt_to` returns a digest
of its input, never the input, so a hit of the canary anywhere means S leaked.
"""
import dataclasses
import hashlib
import json
import os
import stat
from datetime import timedelta

import pytest

from service import agecli, config, hostcheck, keys, models, snapshots
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase, mk_user

FAKE_S = ("AGE-SECRET-KEY-1" + "Q" * 58 + "\n").encode()
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def recipient(n: int) -> str:
    """A recipient of the right shape (not a real key: never handed to the real age)."""
    return "age1" + "".join(CHARSET[(n * 7 + i * 3) % 32] for i in range(58))


S_RECIPIENT = recipient(99)


def mk_key(db, user_id, admin_id, rcpt=None, state="active"):
    rcpt = rcpt or recipient(user_id)
    row = {"user_id": user_id, "recipient": rcpt, "fingerprint": keys.fingerprint(rcpt),
           "state": state}
    if state != "pending":
        row.update(activated_by=admin_id, activated_at=models.utcnow())
    return models.insert(db, "keys", row)


class World:
    def __init__(self, db, root, enabled=1, save=1, key=True, owner="alice", setup="tb",
                 state="cleaning"):
        self.db, self.root = db, str(root)
        existing = db.execute("SELECT id FROM setups WHERE name = ?", (setup,)).fetchone()
        self.sid = existing["id"] if existing else mk_setup(db, setup, ["wire"], dpu_hw_class="c")
        uc = db.execute("SELECT id FROM usecases WHERE name = 'uc'").fetchone()
        self.uc = uc["id"] if uc else mk_usecase(db, "uc", ["wire"], hw_class="c")
        u = db.execute("SELECT id FROM users WHERE username = ?", (owner,)).fetchone()
        self.uid = u["id"] if u else mk_user(db, owner)[0]
        a = db.execute("SELECT id FROM users WHERE username = 'root-admin'").fetchone()
        self.admin = a["id"] if a else mk_user(db, "root-admin", "admin")[0]
        db.execute("UPDATE setups SET snapshot_enabled = ? WHERE id = ?", (enabled, self.sid))
        self.key_id = None
        if key:
            k = keys.active_key(db, self.uid)
            self.key_id = k["id"] if k else mk_key(db, self.uid, self.admin)
        self.rid = mk_res(db, self.sid, self.uc, T(7), T(9), state=state, user_id=self.uid)
        db.execute("UPDATE reservations SET save_snapshot = ? WHERE id = ?", (save, self.rid))

    def res(self):
        return models.reservation_by_id(self.db, self.rid)

    def key(self):
        return keys.active_key(self.db, self.uid)

    def udir(self):
        return os.path.join(self.root, f"u{self.uid}")

    def rows(self):
        return self.db.execute("SELECT * FROM snapshots ORDER BY id").fetchall()


@pytest.fixture
def snap_settings(tmp_path, monkeypatch):
    s = dataclasses.replace(config.settings, snapshot_root=str(tmp_path / "snaps"),
                            snapshot_cap_bytes=1 << 20)
    monkeypatch.setattr(config, "settings", s)
    return s


@pytest.fixture
def fake_age(monkeypatch):
    """agecli without the binary: keygen hands out the S-shaped canary; the swap check passes
    (this laptop's swap is unsafe, E-2). Records the calls (never their data)."""
    calls = []

    def keygen():
        calls.append("keygen")
        return FAKE_S, S_RECIPIENT

    def encrypt_to(rcpt, data):
        calls.append(("encrypt_to", rcpt))
        return b"age-wrapped:" + hashlib.sha256(data).hexdigest().encode()

    monkeypatch.setattr(agecli, "keygen", keygen)
    monkeypatch.setattr(agecli, "encrypt_to", encrypt_to)
    monkeypatch.setattr(hostcheck, "require_safe_swap", lambda *a, **k: calls.append("swap"))
    return calls


@pytest.fixture
def world(db, snap_settings):
    return World(db, snap_settings.snapshot_root)


def files_under(path):
    out = []
    for dirpath, _dirs, names in os.walk(path):
        out += [os.path.relpath(os.path.join(dirpath, n), path) for n in names]
    return sorted(out)


def saved_row(w, created_at, held=False, user_id=None, setup_id=None, state="saved"):
    """A snapshot row with its three files (as an earlier save left it)."""
    uid = user_id or w.uid
    pid = hashlib.md5(f"{created_at}{uid}{state}{held}".encode()).hexdigest()
    key_id = keys.active_key(w.db, uid)["id"]
    row = {"public_id": pid, "user_id": uid, "setup_id": setup_id or w.sid, "key_id": key_id,
           "state": state}
    if held:
        row.update(delete_after="2099-01-01T00:00:00Z", delete_requested_by=uid)
    sid = models.insert(w.db, "snapshots", row)
    w.db.execute("UPDATE snapshots SET created_at = ? WHERE id = ?", (created_at, sid))
    snapshots.ensure_dirs(w.root, uid)
    for p in snapshots.paths(w.root, uid, pid).values():
        with open(p, "wb") as fh:
            fh.write(b"x")
    return sid, pid


# ---- paths and files --------------------------------------------------------------------------

def test_paths_follow_the_design(tmp_path):
    pid = "a" * 32
    p = snapshots.paths(str(tmp_path), 7, pid)
    assert p == {"age": str(tmp_path / "u7" / f"{pid}.age"),
                 "key": str(tmp_path / "u7" / f"{pid}.key.age"),
                 "hdr": str(tmp_path / "u7" / f"{pid}.hdr.json")}
    assert snapshots.incoming_path(str(tmp_path), 7, pid) == str(tmp_path / "u7" / ".incoming"
                                                                 / f"{pid}.age.part")


@pytest.mark.parametrize("bad", ["A" * 32, "a" * 31, "../" + "a" * 29, "a" * 31 + "/"])
def test_paths_refuse_a_public_id_of_another_shape(tmp_path, bad):
    with pytest.raises(ValueError):
        snapshots.paths(str(tmp_path), 7, bad)


def test_ensure_dirs_makes_owner_only_dirs(tmp_path):
    root = tmp_path / "snaps"
    snapshots.ensure_dirs(str(root), 3)
    for d in (root, root / "u3", root / "u3" / ".incoming"):
        st = os.lstat(d)
        assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700
    os.chmod(root / "u3", 0o755)
    snapshots.ensure_dirs(str(root), 3)
    assert stat.S_IMODE(os.lstat(root / "u3").st_mode) == 0o700


def test_ensure_dirs_refuses_a_symlinked_user_dir(tmp_path):
    root = tmp_path / "snaps"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "u3")
    with pytest.raises(snapshots.SnapshotError) as e:
        snapshots.ensure_dirs(str(root), 3)
    assert str(e.value) == "storage_unsafe"


def test_write_private_is_0600_and_atomic(tmp_path):
    p = tmp_path / "f.key.age"
    snapshots.write_private(str(p), b"one")
    snapshots.write_private(str(p), b"two")
    assert p.read_bytes() == b"two" and stat.S_IMODE(os.lstat(p).st_mode) == 0o600
    assert os.listdir(tmp_path) == ["f.key.age"]                 # no temp left behind


def test_delete_snapshot_files_tolerates_missing_files(tmp_path):
    pid = "b" * 32
    snapshots.ensure_dirs(str(tmp_path), 1)
    p = snapshots.paths(str(tmp_path), 1, pid)
    open(p["key"], "wb").close()
    assert snapshots.delete_snapshot_files(str(tmp_path), 1, pid) == 1
    assert snapshots.delete_snapshot_files(str(tmp_path), 1, pid) == 0


def test_headroom_needs_twice_the_cap_plus_the_margin(tmp_path, monkeypatch):
    cap = 1 << 20
    need = 2 * cap + snapshots.HEADROOM_MARGIN
    usage = type("U", (), {})
    for free, ok in ((need, True), (need - 1, False)):
        usage.free = free
        monkeypatch.setattr(snapshots.shutil, "disk_usage", lambda _p, u=usage: u)
        assert snapshots.headroom_ok(str(tmp_path), cap) is ok


# ---- the report -------------------------------------------------------------------------------

GOOD = {"status": "saved", "reason": "ok", "raw_bytes": 10, "age_bytes": 5, "age_sha256": "a" * 64}


@pytest.mark.parametrize("doc", [
    GOOD,
    {"status": "empty", "reason": "empty_home", "raw_bytes": 0, "age_bytes": 0, "age_sha256": ""},
    {"status": "refused", "reason": "home_owner", "raw_bytes": 0, "age_bytes": 0, "age_sha256": ""}])
def test_check_report_accepts_the_contract(doc):
    assert snapshots.check_report(doc) == doc


@pytest.mark.parametrize("doc", [
    None, [], "saved",
    {**GOOD, "extra": 1},
    {k: v for k, v in GOOD.items() if k != "reason"},
    {**GOOD, "status": "done"},
    {**GOOD, "reason": "whatever"},
    {**GOOD, "reason": "empty_home"},
    {**GOOD, "raw_bytes": -1}, {**GOOD, "age_bytes": True}, {**GOOD, "age_bytes": 0},
    {**GOOD, "age_sha256": "A" * 64}, {**GOOD, "age_sha256": ""},
    {"status": "empty", "reason": "ok", "raw_bytes": 0, "age_bytes": 0, "age_sha256": ""},
    {"status": "refused", "reason": "ok", "raw_bytes": 0, "age_bytes": 0, "age_sha256": ""},
])
def test_check_report_refuses_anything_else(doc):
    with pytest.raises(snapshots.SnapshotError) as e:
        snapshots.check_report(doc)
    assert str(e.value) == "bad_report"


def test_verify_fetched_checks_size_and_sha(tmp_path):
    p = tmp_path / "x.part"
    p.write_bytes(b"cipher")
    doc = {**GOOD, "age_bytes": 6, "age_sha256": hashlib.sha256(b"cipher").hexdigest()}
    snapshots.verify_fetched(str(p), doc)
    for over, why in (({"age_bytes": 7}, "size_mismatch"), ({"age_sha256": "0" * 64}, "sha_mismatch")):
        with pytest.raises(snapshots.SnapshotError) as e:
            snapshots.verify_fetched(str(p), {**doc, **over})
        assert str(e.value) == why
    with pytest.raises(snapshots.SnapshotError) as e:
        snapshots.verify_fetched(str(tmp_path / "missing"), doc)
    assert str(e.value) == "size_mismatch"


# ---- S: generated, wrapped, dropped -----------------------------------------------------------

def test_wrap_new_identity_checks_swap_first_and_returns_only_the_wrapped_key(fake_age):
    key_age, s_recipient = snapshots.wrap_new_identity(recipient(1))
    assert fake_age == ["swap", "keygen", ("encrypt_to", recipient(1))]
    assert s_recipient == S_RECIPIENT
    assert FAKE_S not in key_age and key_age.startswith(b"age-wrapped:")


def test_unsafe_swap_stops_before_s_exists(fake_age, monkeypatch):
    def unsafe(*a, **k):
        raise hostcheck.HostCheckError(hostcheck.UNSAFE)
    monkeypatch.setattr(hostcheck, "require_safe_swap", unsafe)
    with pytest.raises(hostcheck.HostCheckError):
        snapshots.wrap_new_identity(recipient(1))
    assert "keygen" not in fake_age


# ---- rows: begin, reuse, saved, failed, stale -------------------------------------------------

def test_begin_capture_creates_a_capturing_row_and_links_the_job(world):
    jid = models.create_job(world.db, world.rid, "cleanup")
    row = snapshots.begin_capture(world.db, world.res(), world.key(), job_id=jid)
    assert row["state"] == "capturing" and row["reservation_id"] == world.rid
    assert row["key_id"] == world.key_id and row["user_id"] == world.uid
    assert len(row["public_id"]) == 32 and all(c in "0123456789abcdef" for c in row["public_id"])
    assert models.job_by_id(world.db, jid)["snapshot_id"] == row["id"]


def test_begin_capture_reuses_the_failed_row_with_a_new_public_id(world):
    first = snapshots.begin_capture(world.db, world.res(), world.key())
    snapshots.mark_failed(world.db, first["id"])
    world.db.execute("UPDATE snapshots SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                     (first["id"],))
    again = snapshots.begin_capture(world.db, world.res(), world.key())
    assert again["id"] == first["id"] and again["state"] == "capturing"
    assert again["public_id"] != first["public_id"]
    assert again["created_at"] > "2026-01-01T00:00:00Z"          # retention sees the new capture
    assert len(world.rows()) == 1


def test_begin_capture_never_reuses_a_saved_row(world):
    row = snapshots.begin_capture(world.db, world.res(), world.key())
    snapshots.mark_saved(world.db, row["id"])
    with pytest.raises(snapshots.SnapshotError) as e:
        snapshots.begin_capture(world.db, world.res(), world.key())
    assert str(e.value) == "already_saved"


def test_mark_saved_and_failed_are_compare_and_set(world):
    row = snapshots.begin_capture(world.db, world.res(), world.key())
    snapshots.mark_saved(world.db, row["id"])
    for fn in (snapshots.mark_saved, snapshots.mark_failed):
        with pytest.raises(snapshots.SnapshotError) as e:
            fn(world.db, row["id"])
        assert str(e.value) == "state_changed"


def test_a_capturing_row_whose_job_is_not_running_becomes_failed(world):
    jid = models.create_job(world.db, world.rid, "cleanup")          # pending: its engine died
    row = snapshots.begin_capture(world.db, world.res(), world.key(), job_id=jid)
    snapshots.ensure_dirs(world.root, world.uid)
    open(snapshots.paths(world.root, world.uid, row["public_id"])["key"], "wb").close()
    assert snapshots.fail_stale_captures(world.db, world.root) == 1
    assert world.rows()[0]["state"] == "failed"
    assert files_under(world.udir()) == []                           # the attempt's files went
    audit = [json.loads(a["detail"]) for a in models.list_audit_events(world.db)
             if a["action"] == "snapshot_capture"]
    assert audit[0]["status"] == "failed" and audit[0]["reason"] == "stale"


def test_a_capturing_row_whose_job_runs_is_left_alone(world):
    jid = models.create_job(world.db, world.rid, "cleanup")
    models.transition_job(world.db, jid, "running")
    snapshots.begin_capture(world.db, world.res(), world.key(), job_id=jid)
    assert snapshots.fail_stale_captures(world.db, world.root) == 0
    assert world.rows()[0]["state"] == "capturing"


def test_stale_with_no_rows_is_a_no_op(db, snap_settings):
    assert snapshots.fail_stale_captures(db, snap_settings.snapshot_root) == 0


# ---- retention and delete ---------------------------------------------------------------------

def test_retention_keeps_the_newest_three_unheld_saved_rows(world):
    other = World(world.db, world.root, owner="bob")
    ids = [saved_row(world, f"2026-0{m}-01T00:00:00Z")[0] for m in (1, 2, 3, 4)]
    held_id, _ = saved_row(world, "2025-12-01T00:00:00Z", held=True)
    failed_id, _ = saved_row(world, "2025-11-01T00:00:00Z", state="failed")
    bob_ids = [saved_row(other, f"2026-0{m}-01T00:00:00Z")[0] for m in (1, 2, 3, 4)]
    seen = []

    def delete(conn, row):                     # the 2-argument callback, like keys.py's
        seen.append(row["id"])
        snapshots.delete_snapshot(conn, row, world.root)

    assert snapshots.prune_retention(world.db, world.uid, world.sid, delete) == 1
    assert seen == [ids[0]]
    left = {r["id"] for r in world.rows()}
    assert left == set(ids[1:]) | {held_id, failed_id} | set(bob_ids)
    assert snapshots.prune_retention(world.db, world.uid, world.sid, delete) == 0


def test_delete_snapshot_removes_files_then_row_then_audits_inside_the_callers_txn(world):
    sid, pid = saved_row(world, "2026-01-01T00:00:00Z")
    row = world.db.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone()
    world.db.execute("BEGIN IMMEDIATE")                # keys.execute_due_resets holds one too
    snapshots.delete_snapshot(world.db, row, world.root)
    world.db.execute("COMMIT")
    assert world.rows() == [] and files_under(world.udir()) == []
    ev = [a for a in models.list_audit_events(world.db) if a["action"] == "snapshot_delete"]
    detail = json.loads(ev[0]["detail"])
    assert detail == {"snapshot_id": sid, "public_id": pid, "user_id": world.uid,
                      "setup_id": world.sid, "state": "saved"}
    assert world.root not in ev[0]["detail"]


def test_the_new_audit_actions_are_registered():
    assert {"snapshot_capture", "snapshot_delete"} <= set(models.AUDIT_ACTIONS)


# ---- the header (§5.8) ------------------------------------------------------------------------

def test_header_has_exactly_the_design_fields(world):
    row = snapshots.begin_capture(world.db, world.res(), world.key())
    ctx = {"setup": "tb", "usecase": "uc", "hw_class": "c"}
    doc = {**GOOD, "raw_bytes": 123, "age_sha256": "c" * 64}
    hdr = snapshots.header(ctx, row, doc, S_RECIPIENT)
    assert list(hdr) == ["format", "compression", "public_id", "user_id", "setup", "hw_class",
                         "usecase", "created_at", "raw_bytes", "age_sha256", "s_recipient",
                         "profile"]
    assert (hdr["format"], hdr["compression"], hdr["profile"]) == ("daolab-snapshot/1", "gzip", None)
    assert (hdr["public_id"], hdr["user_id"], hdr["raw_bytes"]) == (row["public_id"], world.uid, 123)
    assert hdr["s_recipient"] == S_RECIPIENT


# ---- settings ---------------------------------------------------------------------------------

def test_snapshot_settings_defaults(monkeypatch):
    monkeypatch.delenv("DAOLAB_SNAPSHOT_ROOT", raising=False)
    monkeypatch.delenv("DAOLAB_SNAPSHOT_CAP_BYTES", raising=False)
    s = config.Settings.from_env()
    assert (s.snapshot_root, s.snapshot_cap_bytes) == ("./var/snapshots", 536870912)


@pytest.mark.parametrize("bad", ["0", "-5", str(2 ** 40 + 1), "lots"])
def test_a_bad_cap_fails_loading_the_settings(monkeypatch, bad):
    monkeypatch.setenv("DAOLAB_SNAPSHOT_CAP_BYTES", bad)
    with pytest.raises(ValueError):
        config.Settings.from_env()
