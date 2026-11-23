"""B-104 DDL check (v4). Synthetic in-memory DB only; never opens a file DB.

Everything under test is parsed out of B-104-DDL.md itself: the ```sql CREATE TABLE blocks,
the add_column(...) calls and the _OLD/_NEW_JOB_KINDS strings. It applies them to a fresh
origin/rama v11 DB in the §3.1 order, then exercises a sample of the §6.2 cases.

Usage: python3 -I ddl-check.py <probe-dir with rama/mig_rama.py, rama/schema.sql> <B-104-DDL.md>
(probe dir: `git show origin/rama:service/migrations.py > rama/mig_rama.py`, same for schema.sql)"""
import importlib.util
import re
import sqlite3
import sys

D, MD = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("m", f"{D}/rama/mig_rama.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
doc = open(MD).read()

c = sqlite3.connect(":memory:", isolation_level=None)
c.execute("PRAGMA foreign_keys=ON")
c.executescript(open(f"{D}/rama/schema.sql").read())
m.apply(c)
print("base: rama migrations, user_version", m.current_version(c))

# ---- 1. tables (FK targets first), 2. add_column calls, 3. jobs.kind CHECK rewrite ----------
blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "CREATE TABLE" in b]
order = {"keys": 0, "key_challenges": 1, "admin_approvals": 2, "snapshots": 3}
blocks.sort(key=lambda b: order[re.search(r"EXISTS (\w+)", b).group(1)])
assert len(blocks) == 4, len(blocks)
for b in blocks:
    c.executescript(b)
adds = re.findall(r'add_column\(conn, "(\w+)", "(\w+)",\s*"([^"]*)"\)', doc)
assert len(adds) == 3, adds
for table, col, ddl in adds:
    m.add_column(c, table, col, ddl)
old_k = re.search(r'_OLD_JOB_KINDS = "(.*)"', doc).group(1)
new_k = re.search(r'_NEW_JOB_KINDS = "(.*)"', doc).group(1)


def rewrite_job_kinds():                      # the m6 method (migrations.py:176-190), on jobs
    sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
    if "'restore'" in sql:
        return "skipped (already rewritten)"
    if old_k not in sql:
        raise RuntimeError("migration 14: cannot find the jobs.kind CHECK to rewrite")
    v = c.execute("PRAGMA schema_version").fetchone()[0]
    c.execute("PRAGMA writable_schema = ON")
    c.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='jobs'",
              (sql.replace(old_k, new_k, 1),))
    c.execute(f"PRAGMA schema_version = {int(v) + 1}")
    c.execute("PRAGMA writable_schema = OFF")
    return "rewritten"


c.execute("BEGIN IMMEDIATE")
print("jobs.kind rewrite, 1st run:", rewrite_job_kinds())
print("jobs.kind rewrite, 2nd run:", rewrite_job_kinds())
c.execute("COMMIT")
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0],
      "| foreign_key_check rows:", len(c.execute("PRAGMA foreign_key_check").fetchall()))
print("DDL parsed:", [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
    "('keys','key_challenges','snapshots','admin_approvals') ORDER BY name")],
      "| add_column:", [f"{t}.{col}" for t, col, _ in adds])

# ---- seed -----------------------------------------------------------------------------------
t0, t1, exp = "2026-11-23T00:00:00Z", "2026-11-23T00:01:00Z", "2026-11-23T00:10:00Z"
hold, later = "2026-11-24T00:00:00Z", "2026-11-24T00:00:01Z"
for name, role, th in (("u", "user", "h1"), ("a", "admin", "h2"), ("b", "admin", "h3")):
    c.execute("INSERT INTO users(username,role,token_hash,ssh_pubkey,created_at,updated_at) "
              "VALUES(?,?,?,'k',?,?)", (name, role, th, t0, t0))        # ids 1 (user), 2, 3 (admins)
c.execute("INSERT INTO pools(name,created_at,updated_at) VALUES('p',?,?)", (t0, t0))
c.execute("INSERT INTO setups(name,pool_id,yaml_path,created_at,updated_at) VALUES('tb-01',1,'y',?,?)", (t0, t0))
c.execute("INSERT INTO usecases(name,created_at,updated_at) VALUES('uc',?,?)", (t0, t0))
c.execute("INSERT INTO reservations(user_id,usecase_id,setup_id,state,requested_start_at,start_at,end_at,"
          "created_at,updated_at) VALUES(1,1,1,'released',?,?,?,?,?)", (t0,) * 5)
S_LIKE = "AGE-SECRET-KEY-1" + "Q" * 58          # shape only; not a real key
REC = lambda ch: "age1" + ch * 58               # valid-shape recipients


def attempt(label, sql, args=()):
    c.execute("SAVEPOINT s")
    try:
        n = c.execute(sql, args).rowcount
        print(f"  {label}: accepted" + ("" if n == 1 else f" (rowcount {n})"))
        c.execute("RELEASE s")
        return True
    except sqlite3.IntegrityError as e:
        print(f"  {label}: refused ({str(e).splitlines()[0].strip()})")
        c.execute("ROLLBACK TO s")
        c.execute("RELEASE s")
        return False


print("\n[keys]")
K = "INSERT INTO keys(user_id,recipient,fingerprint,created_at,updated_at) VALUES(1,?,?,?,?)"
attempt("recipient S-shaped", K, (S_LIKE, "a" * 64, t0, t0))
attempt("recipient bad bech32 char 'b'", K, (REC("b"), "a" * 64, t0, t0))
attempt("recipient valid shape (key 1)", K, (REC("q"), "a" * 64, t0, t0))
attempt("second pending key for the user", K, (REC("p"), "b" * 64, t0, t0))
attempt("self-activation", "UPDATE keys SET state='active',activated_by=1,activated_at=? WHERE id=1", (t1,))
attempt("active without activator", "UPDATE keys SET state='active' WHERE id=1")
attempt("activation by another admin", "UPDATE keys SET state='active',activated_by=2,activated_at=? WHERE id=1", (t1,))
attempt("pending key 2", K, (REC("z"), "c" * 64, t0, t0))
attempt("pending -> revoked (key 2)", "UPDATE keys SET state='revoked' WHERE id=2")
attempt("second active key (key 2)", "UPDATE keys SET state='active',activated_by=2,activated_at=? WHERE id=2", (t1,))

print("\n[key_challenges]")
CH = ("INSERT INTO key_challenges(user_id,key_id,purpose,target_ref,nonce_sha256,expires_at,"
      "created_at,updated_at) VALUES(1,1,?,?,?,?,?,?)")
attempt("valid rotate challenge (ch 1)", CH, ("rotate", "f" * 64, "1" * 64, exp, t0, t0))
attempt("nonce_sha256 uppercase", CH, ("rotate", "x", "B" * 64, exp, t0, t0))
attempt("target_ref with braces", CH, ("rotate", "{{x}}", "2" * 64, exp, t0, t0))
attempt("target_ref uppercase", CH, ("rotate", "Setup:TB", "3" * 64, exp, t0, t0))
attempt("target_ref S-shaped", CH, ("rotate", S_LIKE, "4" * 64, exp, t0, t0))
attempt("target_ref S-shaped lowercase", CH, ("rotate", S_LIKE.lower(), "5" * 64, exp, t0, t0))
attempt("valid cancel_delete challenge (ch 2)", CH, ("cancel_delete", "approval:1", "6" * 64, exp, t0, t0))
CAS = ("UPDATE key_challenges SET consumed_at=?, updated_at=? WHERE id=? AND consumed_at IS NULL "
       "AND expires_at > ? AND key_id IN (SELECT id FROM keys WHERE state='active')")
print("  CAS consume ch 1, first / replay rowcount:",
      c.execute(CAS, (t1, t1, 1, t1)).rowcount, c.execute(CAS, (t1, t1, 1, t1)).rowcount)

print("\n[snapshots]")
SN = ("INSERT INTO snapshots(public_id,user_id,setup_id,reservation_id,key_id,created_at,updated_at) "
      "VALUES(?,1,1,?,1,?,?)")
attempt("valid public_id (snap 1, reservation 1)", SN, ("0" * 32, 1, t0, t0))
attempt("public_id with '-'", SN, ("0" * 31 + "-", None, t0, t0))
attempt("second snapshot for reservation 1", SN, ("1" * 32, 1, t0, t0))
attempt("state restore_incomplete", "UPDATE snapshots SET state='restore_incomplete' WHERE id=1")
attempt("hold on a capturing row", "UPDATE snapshots SET delete_after=?,delete_requested_by=1 WHERE id=1", (hold,))
c.execute("UPDATE snapshots SET state='saved' WHERE id=1")
attempt("delete_after without delete_requested_by", "UPDATE snapshots SET delete_after=? WHERE id=1", (hold,))
attempt("delete_requested_by without delete_after", "UPDATE snapshots SET delete_requested_by=1 WHERE id=1")
attempt("delete_after <= created_at", "UPDATE snapshots SET delete_after=?,delete_requested_by=1 WHERE id=1", (t0,))
attempt("delete_requested_by unknown user", "UPDATE snapshots SET delete_after=?,delete_requested_by=99 WHERE id=1", (hold,))
attempt("hold on a saved row (user delete)", "UPDATE snapshots SET delete_after=?,delete_requested_by=1 WHERE id=1", (hold,))
attempt("cancel_delete clears the hold", "UPDATE snapshots SET delete_after=NULL,delete_requested_by=NULL WHERE id=1")
attempt("hold_approval_id without delete_after", "UPDATE snapshots SET hold_approval_id=1 WHERE id=1")
attempt("hold_approval_id of a non-existent approval", "UPDATE snapshots SET delete_after=?,delete_requested_by=2,hold_approval_id=99 WHERE id=1", (hold,))

print("\n[admin_approvals]")
AA = ("INSERT INTO admin_approvals(action,target_ref,requested_by,created_at,updated_at) VALUES(?,?,2,?,?)")
attempt("target_ref S-shaped", AA, ("home_discard", S_LIKE, t0, t0))
attempt("unknown action", AA, ("legacy_home", "tb-01", t0, t0))
attempt("valid home_discard (appr 1)", AA, ("home_discard", "tb-01", t0, t0))
attempt("second pending for same target", AA, ("home_discard", "tb-01", t0, t0))
attempt("pending with decided_by", "UPDATE admin_approvals SET decided_by=3,decided_at=? WHERE id=1", (t1,))
attempt("decided by the requester", "UPDATE admin_approvals SET state='approved',decided_by=2,decided_at=? WHERE id=1", (t1,))
attempt("approved without decided_by", "UPDATE admin_approvals SET state='approved' WHERE id=1")
attempt("decided_by without decided_at", "UPDATE admin_approvals SET state='approved',decided_by=3 WHERE id=1")
attempt("execute_after <= created_at", "UPDATE admin_approvals SET state='approved',decided_by=3,decided_at=?,"
        "execute_after=? WHERE id=1", (t1, t0))
attempt("approved by a second admin, 24h hold", "UPDATE admin_approvals SET state='approved',decided_by=3,"
        "decided_at=?,execute_after=? WHERE id=1", (t1, hold))
attempt("executed before execute_after", "UPDATE admin_approvals SET executed_at=? WHERE id=1", (t1,))
attempt("owner cancel after approval (cancelled, decided_by kept)", "UPDATE admin_approvals SET state='cancelled' WHERE id=1")
attempt("executed_at on a cancelled row", "UPDATE admin_approvals SET executed_at=? WHERE id=1", (later,))
attempt("tripwire_accept, no hold (appr 2)", AA, ("tripwire_accept", "sdp-pf1-vf8", t0, t0))
attempt("approve + execute at once (execute_after NULL)", "UPDATE admin_approvals SET state='approved',decided_by=3,"
        "decided_at=?,executed_at=? WHERE id=2", (t1, t1))

print("\n[jobs]")
J = "INSERT INTO jobs(reservation_id,kind,created_at,updated_at) VALUES(1,?,?,?)"
attempt("kind restore", J, ("restore", t0, t0))
attempt("kind cleanup (legacy kind)", J, ("cleanup", t0, t0))
attempt("kind bogus", J, ("bogus", t0, t0))
c.execute("UPDATE jobs SET snapshot_id=1")
print("  jobs has a 'secret' column:", m.has_column(c, "jobs", "secret"))

print("\n[timestamp shape]")
attempt("expires_at with a space", CH, ("rotate", "a" * 64, "7" * 64, "2026-11-23 00:10:00", t0, t0))
attempt("expires_at with +00:00", CH, ("rotate", "a" * 64, "8" * 64, "2026-11-23T00:10:00+00:00", t0, t0))
attempt("created_at not a time", CH, ("rotate", "a" * 64, "9" * 64, exp, "yesterday", t0))
attempt("expires_at well-formed", CH, ("rotate", "a" * 64, "a" * 64, exp, t0, t0))

print("\n[S-shaped canary over every new TEXT column]")
new_cols = [("keys", 2), ("key_challenges", 1), ("snapshots", 1), ("admin_approvals", 1)]
for table, rowid in new_cols:
    for cid, name, typ, *_ in c.execute(f"PRAGMA table_info({table})").fetchall():
        if typ == "TEXT":
            attempt(f"{table}.{name}", f"UPDATE {table} SET {name}=? WHERE id=?", (S_LIKE, rowid))

print("\n[FK actions vs daolab sync]")
attempt("delete released reservation 1 (as catalog.py:564)", "DELETE FROM reservations WHERE id=1")
print("  snapshot 1 kept, reservation_id:", c.execute("SELECT reservation_id FROM snapshots WHERE id=1").fetchone())
attempt("delete setup tb-01 with snapshots (as catalog.py:565)", "DELETE FROM setups WHERE id=1")
c.execute("INSERT INTO reservations(user_id,usecase_id,setup_id,state,requested_start_at,start_at,end_at,"
          "created_at,updated_at) VALUES(1,1,1,'released',?,?,?,?,?)", (t0,) * 5)
rid2 = c.execute("SELECT max(id) FROM reservations").fetchone()[0]     # AUTOINCREMENT: not 1
c.execute("INSERT INTO jobs(reservation_id,kind,created_at,updated_at) VALUES(?,?,?,?)",
          (rid2, "restore", t0, t0))
jid = c.execute("SELECT max(id) FROM jobs").fetchone()[0]
c.execute("UPDATE jobs SET snapshot_id=1 WHERE id=?", (jid,))
c.execute("DELETE FROM snapshots WHERE id=1")
print("  jobs.snapshot_id after the snapshot row is deleted:",
      c.execute("SELECT snapshot_id FROM jobs WHERE id=?", (jid,)).fetchone()[0])
# ---- T-16 / T-19 / T-20: lost-key reset = a 24h hold; rows matched by hold_approval_id ------------
# State here: key 1 active (user 1), key 2 pending (user 1); admins 2 and 3.
print("\n[T-16/T-19/T-20 lost-key reset: hold, owner cancel, then a second reset to execute_after]")
CAS_FULL = ("UPDATE key_challenges SET consumed_at=?, updated_at=? WHERE id=? AND user_id=1 "
            "AND purpose=? AND target_ref=? AND consumed_at IS NULL AND expires_at > ? "
            "AND key_id IN (SELECT id FROM keys WHERE state='active')")
EXEC = ("UPDATE admin_approvals SET executed_at=?, updated_at=? WHERE id=? AND state='approved' "
        "AND executed_at IS NULL AND (execute_after IS NULL OR execute_after <= ?)")
own_hold = "2026-11-23T12:00:00Z"                       # an earlier hold of the user's own
key_state = lambda: c.execute("SELECT state FROM keys WHERE id=1").fetchone()[0]
hold_of = lambda pid: c.execute("SELECT delete_after, delete_requested_by, hold_approval_id FROM snapshots "
                                "WHERE public_id=?", (pid,)).fetchone()
pA, pB, pC = "a" * 32, "b" * 32, "c" * 32
for pid in (pA, pB):
    c.execute(SN, (pid, None, t0, t0))
c.execute("UPDATE snapshots SET state='saved' WHERE public_id IN (?,?)", (pA, pB))
c.execute("UPDATE snapshots SET delete_after=?, delete_requested_by=1 WHERE public_id=?", (own_hold, pB))


def approve_reset():                                    # one transaction (T-16)
    c.execute("BEGIN IMMEDIATE")
    c.execute(AA, ("lost_key_reset", "user:1", t0, t0))
    rid = c.execute("SELECT max(id) FROM admin_approvals").fetchone()[0]
    c.execute("UPDATE admin_approvals SET state='approved',decided_by=3,decided_at=?,execute_after=? "
              "WHERE id=?", (t1, hold, rid))
    c.execute("UPDATE snapshots SET delete_after=?, delete_requested_by=2, hold_approval_id=? "
              "WHERE key_id=1 AND state='saved' AND delete_after IS NULL", (hold, rid))
    c.execute("COMMIT")
    return rid


def challenge(purpose, target, nonce_hex, created, expires):
    c.execute(CH, (purpose, target, nonce_hex, expires, created, created))   # user 1, key 1
    return c.execute("SELECT max(id) FROM key_challenges").fetchone()[0]


# reset 1: approve, owner cancels during the hold (T-19)
r1 = approve_reset()
print(f"  reset 1 approved (appr {r1}): key 1 state = {key_state()}")
print("  pA held by the approval:", hold_of(pA) == (hold, 2, r1),
      "| pB keeps its own hold (hold_approval_id NULL):", hold_of(pB) == (own_hold, 1, None))
attempt("clear pA's delete_after while hold_approval_id is still set",
        "UPDATE snapshots SET delete_after=NULL, delete_requested_by=NULL WHERE public_id=?", (pA,))
attempt("activate pending key 2 during the hold", "UPDATE keys SET state='active',activated_by=3,"
        "activated_at=? WHERE id=2", (t1,))
ch_a = challenge("delete", pA, "c" * 63 + "1", t0, exp)
print("  CAS on the key's open challenge during the hold, rowcount:",
      c.execute(CAS_FULL, (t1, t1, ch_a, "delete", pA, t1)).rowcount)
ch_cancel = challenge("cancel_delete", f"approval:{r1}", "c" * 63 + "2", t0, exp)
c.execute("BEGIN IMMEDIATE")
n_cas = c.execute(CAS_FULL, (t1, t1, ch_cancel, "cancel_delete", f"approval:{r1}", t1)).rowcount
n_appr = c.execute("UPDATE admin_approvals SET state='cancelled', updated_at=? WHERE id=? AND state='approved' "
                   "AND executed_at IS NULL", (t1, r1)).rowcount
n_snap = c.execute("UPDATE snapshots SET delete_after=NULL, delete_requested_by=NULL, hold_approval_id=NULL, "
                   "updated_at=? WHERE hold_approval_id=?", (t1, r1)).rowcount
c.execute("COMMIT")
print(f"  owner cancel (one transaction, rows WHERE hold_approval_id={r1}): challenge CAS {n_cas}, "
      f"approval -> cancelled {n_appr}, holds cleared {n_snap}")
print("  after cancel: approval state =", c.execute("SELECT state FROM admin_approvals WHERE id=?", (r1,)).fetchone()[0],
      "| pA hold:", hold_of(pA), "| pB own hold kept:", hold_of(pB) == (own_hold, 1, None), "| key 1 =", key_state())
print("  execution CAS on the cancelled approval at execute_after, rowcount:",
      c.execute(EXEC, (later, later, r1, later)).rowcount)

# reset 2: approve, a capture during the hold (unheld), run to execute_after (T-16, T-20)
r2 = approve_reset()
print(f"  reset 2 approved (appr {r2}): key 1 state = {key_state()} | pA held by appr {r2}:",
      hold_of(pA) == (hold, 2, r2))
c.execute(SN, (pC, None, t1, t1))
c.execute("UPDATE snapshots SET state='saved' WHERE public_id=?", (pC,))
print("  pC captured during the hold (saved, unheld):", hold_of(pC) == (None, None, None))
ch_late = challenge("delete", pA, "c" * 63 + "3", "2026-11-23T23:55:00Z", "2026-11-24T00:05:00Z")
print("  execution CAS before execute_after, rowcount:", c.execute(EXEC, (t1, t1, r2, t1)).rowcount)
c.execute("BEGIN IMMEDIATE")
n_exec = c.execute(EXEC, (later, later, r2, later)).rowcount
n_rev = c.execute("UPDATE keys SET state='revoked', updated_at=? WHERE id=1 AND state='active'", (later,)).rowcount
n_del = c.execute("DELETE FROM snapshots WHERE key_id=1").rowcount            # every snapshot of the key
c.execute("COMMIT")
print(f"  execution at {later} (one transaction): approval executed {n_exec}, key revoked {n_rev}, "
      f"snapshots of the key deleted {n_del} (approval-held pA, own-held pB, unheld pC)")
print("  key 1 state:", key_state(), "| pA, pB, pC exist:",
      [hold_of(p) is not None for p in (pA, pB, pC)],
      "| snapshots still on key 1:", c.execute("SELECT count(*) FROM snapshots WHERE key_id=1").fetchone()[0])
print("  open challenge of the revoked key, unexpired at", later, ":",
      c.execute("SELECT expires_at > ? FROM key_challenges WHERE id=?", (later, ch_late)).fetchone()[0] == 1,
      "| CAS rowcount:", c.execute(CAS_FULL, (later, later, ch_late, "delete", pA, later)).rowcount)
attempt("activate pending key 2 after the revoke", "UPDATE keys SET state='active',activated_by=3,"
        "activated_at=? WHERE id=2", (later,))

# ---- T-21: retention counts and prunes only unheld saved rows (query taken from the document) ---------
print("\n[T-21 retention: a held row is neither counted nor pruned]")
prune = re.search(r"```sql\n(-- retention prune.*?)```", doc, re.S).group(1)
SN2 = ("INSERT INTO snapshots(public_id,user_id,setup_id,key_id,state,created_at,updated_at) "
       "VALUES(?,1,1,2,'saved',?,?)")
rows = [("d" * 32, "2026-11-24T01:00:00Z"),            # oldest: will carry an own hold
        ("e" * 32, "2026-11-24T02:00:00Z"), ("f" * 32, "2026-11-24T03:00:00Z"),
        ("0" * 31 + "1", "2026-11-24T04:00:00Z"), ("0" * 31 + "2", "2026-11-24T05:00:00Z")]
for pid, ts in rows:
    c.execute(SN2, (pid, ts, ts))
c.execute("UPDATE snapshots SET delete_after=?, delete_requested_by=1 WHERE public_id=?",
          ("2026-11-25T01:00:00Z", "d" * 32))
unheld = lambda: c.execute("SELECT count(*) FROM snapshots WHERE user_id=1 AND setup_id=1 AND state='saved' "
                           "AND delete_after IS NULL").fetchone()[0]
print("  before prune: unheld saved =", unheld(), "| held =",
      c.execute("SELECT count(*) FROM snapshots WHERE delete_after IS NOT NULL").fetchone()[0])
n_pruned = c.execute(prune, {"uid": 1, "sid": 1}).rowcount
left = [r[0] for r in c.execute("SELECT public_id FROM snapshots WHERE user_id=1 AND setup_id=1 ORDER BY created_at")]
print("  prune query from §3.5 deleted:", n_pruned, "| unheld saved left:", unheld(),
      "| held oldest row kept:", "d" * 32 in left, "| oldest unheld pruned:", "e" * 32 not in left)

print("\nfinal integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0],
      "| foreign_key_check rows:", len(c.execute("PRAGMA foreign_key_check").fetchall()))
