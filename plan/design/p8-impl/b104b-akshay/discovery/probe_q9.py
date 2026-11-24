"""B-104 PR B discovery, probe 3: edge cases behind FINDINGS §9. :memory: only.
Applies the v4 DDL (parsed from B-104-DDL.md) on the exported a67d4ac v13 schema.
Usage: <venv>/bin/python -I probe_q9.py /tmp/b104b-probe ~/notes/b104/B-104-DDL.md"""
import re
import sqlite3
import sys

root, md = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from service import migrations, models  # noqa: E402

doc = open(md).read()
blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "CREATE TABLE" in b]
order = {"keys": 0, "key_challenges": 1, "admin_approvals": 2, "snapshots": 3}
blocks.sort(key=lambda b: order[re.search(r"EXISTS (\w+)", b).group(1)])
c = models.connect(":memory:")
models.init_db(c)
for b in blocks:
    buf = ""
    for line in b.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            c.execute(buf)
            buf = ""
t0, t1, hold = "2026-11-24T00:00:00Z", "2026-11-24T00:01:00Z", "2026-11-25T00:00:00Z"
for n, role in (("u", "user"), ("a", "admin"), ("b", "admin")):
    c.execute("INSERT INTO users(username,role,token_hash,ssh_pubkey,created_at,updated_at) "
              "VALUES(?,?,?,'k',?,?)", (n, role, "h" + n, t0, t0))


def try_(label, sql, args=()):
    c.execute("SAVEPOINT s")
    try:
        c.execute(sql, args)
        print(f"  {label}: ACCEPTED")
        c.execute("RELEASE s")
    except sqlite3.IntegrityError as e:
        print(f"  {label}: refused ({e})")
        c.execute("ROLLBACK TO s")
        c.execute("RELEASE s")


K = "INSERT INTO keys(user_id,recipient,fingerprint,created_at,updated_at) VALUES(1,?,?,?,?)"
c.execute(K, ("age1" + "q" * 58, "a" * 64, t0, t0))                      # key 1 pending
print("[keys: transitions are not constraints]")
try_("pending -> revoked, state only", "UPDATE keys SET state='revoked' WHERE id=1")
try_("pending -> revoked, with activated_by+activated_at set in the same UPDATE",
     "UPDATE keys SET state='revoked', activated_by=2, activated_at=? WHERE id=1", (t1,))
c.execute("UPDATE keys SET state='pending', activated_by=NULL, activated_at=NULL WHERE id=1")
try_("active with activated_by but activated_at NULL",
     "UPDATE keys SET state='active', activated_by=2 WHERE id=1")

print("[BLOB values vs TEXT CHECKs]")
CH = ("INSERT INTO key_challenges(user_id,key_id,purpose,target_ref,nonce_sha256,expires_at,"
      "created_at,updated_at) VALUES(1,1,'delete',?,?,?,?,?)")
try_("target_ref = BLOB lowercase S-shaped", CH,
     (("age-secret-key-1" + "q" * 58).encode(), "1" * 64, hold, t0, t0))
try_("nonce_sha256 = 64-byte BLOB of hex chars", CH, ("x", b"2" * 64, hold, t0, t0))

print("[admin_approvals: one-open index covers only 'pending']")
AA = "INSERT INTO admin_approvals(action,target_ref,requested_by,created_at,updated_at) VALUES(?,?,2,?,?)"
c.execute(AA, ("lost_key_reset", "user:1", t0, t0))
c.execute("UPDATE admin_approvals SET state='approved',decided_by=3,decided_at=?,execute_after=? WHERE id=1",
          (t1, hold))
try_("2nd lost_key_reset for user:1 while the 1st is approved, not executed", AA,
     ("lost_key_reset", "user:1", t1, t1))
try_("...and approving it too", "UPDATE admin_approvals SET state='approved',decided_by=3,decided_at=?,"
     "execute_after=? WHERE id=2", (t1, hold))
print("  approvals pending-or-unexecuted for user:1:", c.execute(
    "SELECT count(*) FROM admin_approvals WHERE target_ref='user:1' AND executed_at IS NULL "
    "AND state IN ('pending','approved')").fetchone()[0])
print("[admin_approvals: no key column; the key under reset is implied by target_ref]")
print("  columns:", [r[1] for r in c.execute("PRAGMA table_info(admin_approvals)")])
print("[index coverage of FK child columns used by ON DELETE / parent deletes]")
for t in ("jobs", "key_challenges", "snapshots"):
    idx = [(r[1], [x[2] for x in c.execute(f"PRAGMA index_info({r[1]})")])
           for r in c.execute(f"PRAGMA index_list({t})")]
    print(f"  {t}: {idx}")
