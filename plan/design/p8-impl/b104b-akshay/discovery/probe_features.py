"""B-104 PR B discovery, probe 2: SQLite features the v4 DDL relies on.

Part A: tiny :memory: proofs, one per feature. Each prints PASS/FAIL against what the DDL needs.
Part B: a dry run of DDL v4 as one migration 14 (parsed from B-104-DDL.md, like ddl-check.py),
inside one BEGIN IMMEDIATE, on a file DB under /tmp/b104b-probe migrated by the exported
a67d4ac service/ to v13. Then the guard, integrity_check, foreign_key_check and M6.

Usage: <venv>/bin/python -I probe_features.py /tmp/b104b-probe ~/notes/b104/B-104-DDL.md
"""
import os
import re
import sqlite3
import sys

root, md = sys.argv[1], sys.argv[2]
print("python", sys.version.split()[0], "| sqlite", sqlite3.sqlite_version)
results = []


def check(label, ok, detail=""):
    results.append((label, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))


def mem():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def raises(c, sql, args=()):
    try:
        c.execute(sql, args)
        return None
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"


# ---- A1 partial unique index ---------------------------------------------------------------
c = mem()
c.execute("CREATE TABLE k (id INTEGER PRIMARY KEY, user_id INT, state TEXT)")
c.execute("CREATE UNIQUE INDEX one_active ON k(user_id) WHERE state = 'active'")
c.execute("INSERT INTO k(user_id,state) VALUES (1,'active'),(1,'retired'),(1,'retired')")
e = raises(c, "INSERT INTO k(user_id,state) VALUES (1,'active')")
check("A1 partial UNIQUE index: 2nd active refused, many retired allowed", e is not None, e or "")

# ---- A2 ADD COLUMN NOT NULL DEFAULT 0 + CHECK on a table with rows -------------------------
c = mem()
c.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, x TEXT)")
c.execute("INSERT INTO r(x) VALUES ('a'),('b')")
c.execute("ALTER TABLE r ADD COLUMN save_snapshot INTEGER NOT NULL DEFAULT 0 "
          "CHECK (save_snapshot IN (0, 1))")
vals = [r[0] for r in c.execute("SELECT save_snapshot FROM r")]
e = raises(c, "UPDATE r SET save_snapshot = 2 WHERE id = 1")
dflt = [row[4] for row in c.execute("PRAGMA table_info(r)") if row[1] == "save_snapshot"][0]
check("A2 ADD COLUMN NOT NULL DEFAULT 0 + CHECK: legacy rows get 0, 2 refused",
      vals == [0, 0] and e is not None, f"legacy={vals}; update 2 -> {e}; dflt_value={dflt!r}")
check("A2b M6 reads dflt_value '0' (not '1') for DEFAULT 0", dflt == "0", repr(dflt))

# ---- A3 ADD COLUMN CHECK tested against existing rows (SQLite >= 3.37) ---------------------
c = mem()
c.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
c.execute("INSERT INTO r DEFAULT VALUES")
e = raises(c, "ALTER TABLE r ADD COLUMN f INTEGER NOT NULL DEFAULT 2 CHECK (f IN (0, 1))")
check("A3 ADD COLUMN CHECK is tested against existing rows (bad default + rows -> refused)",
      e is not None, e or "accepted (CHECK not tested against existing rows)")
c2 = mem()
c2.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
e2 = raises(c2, "ALTER TABLE r ADD COLUMN f INTEGER NOT NULL DEFAULT 2 CHECK (f IN (0, 1))")
print("     (same ALTER on an EMPTY table:", e2 or "accepted", ")")

# ---- A4 ADD COLUMN REFERENCES ... ON DELETE SET NULL with FKs on -----------------------------
c = mem()
c.execute("CREATE TABLE s (id INTEGER PRIMARY KEY)")
c.execute("CREATE TABLE j (id INTEGER PRIMARY KEY)")
c.execute("INSERT INTO s(id) VALUES (1)")
c.execute("INSERT INTO j(id) VALUES (1)")
e_ok = raises(c, "ALTER TABLE j ADD COLUMN snapshot_id INTEGER REFERENCES s(id) ON DELETE SET NULL")
c.execute("UPDATE j SET snapshot_id = 1")
c.execute("DELETE FROM s WHERE id = 1")
after = c.execute("SELECT snapshot_id FROM j").fetchone()[0]
e_fk = raises(c, "UPDATE j SET snapshot_id = 99")
e_bad = raises(c, "ALTER TABLE j ADD COLUMN other_id INTEGER DEFAULT 0 REFERENCES s(id)")
check("A4 ADD COLUMN REFERENCES ON DELETE SET NULL (default NULL) works with FKs on",
      e_ok is None and after is None and e_fk is not None, f"after parent delete: {after}; bad FK -> {e_fk}")
check("A4b ...and a non-NULL default is refused while FKs are on", e_bad is not None, e_bad or "")

# ---- A5 composite FK needs UNIQUE(id, user_id) on the parent ----------------------------------
c = mem()
c.execute("CREATE TABLE keys (id INTEGER PRIMARY KEY, user_id INT NOT NULL, UNIQUE (id, user_id))")
c.execute("CREATE TABLE ch (id INTEGER PRIMARY KEY, user_id INT, key_id INT, "
          "FOREIGN KEY (key_id, user_id) REFERENCES keys(id, user_id))")
c.execute("INSERT INTO keys VALUES (1, 10), (2, 20)")
ok = raises(c, "INSERT INTO ch(user_id,key_id) VALUES (10, 1)")
cross = raises(c, "INSERT INTO ch(user_id,key_id) VALUES (10, 2)")
c3 = mem()
c3.execute("CREATE TABLE keys (id INTEGER PRIMARY KEY, user_id INT NOT NULL)")      # no UNIQUE pair
c3.execute("CREATE TABLE ch (id INTEGER PRIMARY KEY, user_id INT, key_id INT, "
           "FOREIGN KEY (key_id, user_id) REFERENCES keys(id, user_id))")
c3.execute("INSERT INTO keys VALUES (1, 10)")
mismatch = raises(c3, "INSERT INTO ch(user_id,key_id) VALUES (10, 1)")
check("A5 composite FK: own key ok, another user's key refused", ok is None and cross is not None, cross or "")
print("     (without UNIQUE(id,user_id) on the parent:", mismatch, ")")

# ---- A6 CHECK with GLOB / LIKE / length() --------------------------------------------------
c = mem()
TS = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'"
c.execute(f"""CREATE TABLE t (
  ref TEXT CHECK (length(ref) BETWEEN 1 AND 128 AND ref NOT GLOB '*[^a-z0-9._:-]*'
                  AND ref NOT LIKE 'age-secret-key-%'),
  rec TEXT CHECK (length(rec) = 62 AND rec GLOB 'age1*'
                  AND substr(rec, 5) NOT GLOB '*[^qpzry9x8gf2tvdw0s3jn54khce6mua7l]*'),
  hex TEXT CHECK (length(hex) = 64 AND hex NOT GLOB '*[^0-9a-f]*'),
  ts  TEXT CHECK (ts GLOB {TS}))""")
cases = [
    ("ref", "approval:12", True), ("ref", "setup:tb-01", True), ("ref", "a-b", True),
    ("ref", "Setup:TB", False), ("ref", "a b", False), ("ref", "a/b", False), ("ref", "{{x}}", False),
    ("ref", "x" * 129, False), ("ref", "", False), ("ref", "age-secret-key-1" + "q" * 58, False),
    ("ref", "AGE-SECRET-KEY-1" + "Q" * 58, False), ("ref", "a]b", False), ("ref", "a^b", False),
    ("ref", "é", False),
    ("rec", "age1" + "q" * 58, True), ("rec", "age1" + "b" * 58, False), ("rec", "age1" + "q" * 57, False),
    ("rec", "AGE1" + "q" * 58, False), ("rec", "age1" + "Q" * 58, False),
    ("hex", "a" * 64, True), ("hex", "A" * 64, False), ("hex", "g" * 64, False), ("hex", "a" * 63, False),
    ("ts", "2026-11-24T00:00:00Z", True), ("ts", "2026-11-24 00:00:00", False),
    ("ts", "2026-11-24T00:00:00+00:00", False), ("ts", "2026-11-24T00:00:00z", False),
    ("ts", "2026-11-24T00:00:00.5Z", False), ("ts", "2026-13-99T99:99:99Z", True),
]
bad = []
for col, val, want in cases:
    got = raises(c, f"INSERT INTO t({col}) VALUES (?)", (val,)) is None
    if got != want:
        bad.append((col, val[:24], "accepted" if got else "refused"))
check("A6 CHECKs with GLOB '[^...]' / LIKE / length() behave as the DDL intends", not bad,
      f"{len(cases)} cases" + (f"; unexpected: {bad}" if bad else ""))
print("     note: the timestamp GLOB checks shape only ('2026-13-99T99:99:99Z' is accepted, by design)")
blob = raises(c, "INSERT INTO t(hex) VALUES (?)", (b"a" * 64,))
print("     note: a 64-byte BLOB into the hex column:", blob or "accepted (BLOB passes length/GLOB)")

# ---- A7 FK target need not exist at CREATE time (table order) ----------------------------------
c = mem()
e = raises(c, "CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INT REFERENCES b(id))")
check("A7 CREATE TABLE with an FK to a not-yet-created table is accepted (order is a convention)",
      e is None, e or "")

# ---- A8 writable_schema CHECK rewrite after ADD COLUMN, in one transaction --------------------
c = mem()
c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL "
          "CHECK (kind IN ('deploy', 'cleanup', 'health_probe', 'reconcile')))")
c.execute("INSERT INTO jobs(kind) VALUES ('cleanup')")
OLD = "'deploy', 'cleanup', 'health_probe', 'reconcile')"
NEW = "'deploy', 'cleanup', 'health_probe', 'reconcile', 'restore')"
c.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
try:
    defensive = c.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
except AttributeError:
    defensive = "n/a"
c.execute("BEGIN IMMEDIATE")
c.execute("ALTER TABLE jobs ADD COLUMN snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL")
sql = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
v = c.execute("PRAGMA schema_version").fetchone()[0]
c.execute("PRAGMA writable_schema = ON")
c.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='jobs'", (sql.replace(OLD, NEW, 1),))
c.execute(f"PRAGMA schema_version = {int(v) + 1}")
c.execute("PRAGMA writable_schema = OFF")
in_txn = raises(c, "INSERT INTO jobs(kind) VALUES ('restore')")
c.execute("ROLLBACK")
after_rb = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
rb_ok = "'restore'" not in after_rb and "snapshot_id" not in after_rb
check("A8 ADD COLUMN then writable_schema rewrite in one txn; 'restore' insertable inside it; "
      "ROLLBACK undoes both", in_txn is None and rb_ok and OLD in sql,
      f"DBCONFIG_DEFENSIVE={defensive}; insert in txn: {in_txn or 'ok'}")
print("     stored jobs sql after ADD COLUMN ends with:", repr(sql[-90:]))

# ---- A9 ALTER/CREATE inside BEGIN IMMEDIATE with foreign_keys ON (pragma can't change in txn) --
c = mem()
c.execute("BEGIN IMMEDIATE")
c.execute("PRAGMA foreign_keys=OFF")          # silently a no-op inside a transaction
fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
c.execute("ROLLBACK")
check("A9 PRAGMA foreign_keys cannot be switched off inside a migration's transaction", fk == 1, f"fk={fk}")

# ================= Part B: DDL v4 as one migration 14 on a real v13 file DB =====================
print("\n--- Part B: v4 DDL dry run on a v13 file DB (exported a67d4ac service/) ---")
sys.path.insert(0, root)
from service import migrations, models  # noqa: E402

doc = open(md).read()
blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "CREATE TABLE" in b]
order = {"keys": 0, "key_challenges": 1, "admin_approvals": 2, "snapshots": 3}
blocks.sort(key=lambda b: order[re.search(r"EXISTS (\w+)", b).group(1)])
adds = re.findall(r'add_column\(conn, "(\w+)", "(\w+)",\s*"([^"]*)"\)', doc)
old_k = re.search(r'_OLD_JOB_KINDS = "(.*)"', doc).group(1)
new_k = re.search(r'_NEW_JOB_KINDS = "(.*)"', doc).group(1)
naive = [s.strip() for b in blocks for s in b.split(";") if s.strip()]


def split_sql(text):                           # comments hold ';' (e.g. "-- [design:57]; Q-6")
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    assert not buf.strip(), buf
    return out


stmts = [s for b in blocks for s in split_sql(b)]
print("naive ';' split gives", len(naive), "pieces; complete_statement split gives", len(stmts),
      "statements:", sum(s.upper().startswith("CREATE TABLE") for s in stmts), "tables,",
      sum("INDEX" in s.split("(")[0].upper() for s in stmts), "indexes")


def m14(conn):
    for s in stmts:                            # no executescript: it would COMMIT the txn
        conn.execute(s)
    for table, col, ddl in adds:
        migrations.add_column(conn, table, col, ddl)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
    if "'restore'" in sql:
        return
    if old_k not in sql:
        raise RuntimeError("migration 14: cannot find the jobs.kind CHECK to rewrite")
    v = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='jobs'",
                 (sql.replace(old_k, new_k, 1),))
    conn.execute(f"PRAGMA schema_version = {int(v) + 1}")
    conn.execute("PRAGMA writable_schema = OFF")


db = os.path.join(root, "v14-dry.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(db + suffix):
        os.remove(db + suffix)
c = models.connect(db)
models.init_db(c)
t0 = "2026-11-24T00:00:00Z"
c.execute("INSERT INTO users(username,role,token_hash,ssh_pubkey,created_at,updated_at) "
          "VALUES('u','user','h','k',?,?)", (t0, t0))
c.execute("INSERT INTO pools(name,created_at,updated_at) VALUES('p',?,?)", (t0, t0))
c.execute("INSERT INTO setups(name,pool_id,yaml_path,created_at,updated_at) VALUES('tb-01',1,'y',?,?)", (t0, t0))
c.execute("INSERT INTO usecases(name,created_at,updated_at) VALUES('uc',?,?)", (t0, t0))
c.execute("INSERT INTO reservations(user_id,usecase_id,setup_id,state,requested_start_at,start_at,end_at,"
          "created_at,updated_at) VALUES(1,1,1,'released',?,?,?,?,?)", (t0,) * 5)
c.execute("INSERT INTO jobs(reservation_id,kind,created_at,updated_at) VALUES(1,'cleanup',?,?)", (t0, t0))
print("statements parsed:", len(stmts), "| add_column:", [f"{t}.{col}" for t, col, _ in adds],
      "| user_version before:", migrations.current_version(c))
c.execute("BEGIN IMMEDIATE")
try:
    m14(c)
    m14(c)                                     # T-4: second run inside the same txn is a no-op
    c.execute("PRAGMA user_version = 14")
    c.execute("COMMIT")
except BaseException:
    c.execute("ROLLBACK")
    raise
c.close()
c = models.connect(db)                          # fresh connection: re-reads the rewritten schema
legacy = c.execute("SELECT r.save_snapshot, s.snapshot_enabled, j.snapshot_id, j.kind FROM reservations r "
                   "JOIN setups s ON s.id=r.setup_id JOIN jobs j ON j.reservation_id=r.id").fetchone()
check("B1 legacy rows after 14: save_snapshot 0, snapshot_enabled 0, snapshot_id NULL, kind kept",
      tuple(legacy) == (0, 0, None, "cleanup"), str(tuple(legacy)))
e_r = raises(c, "INSERT INTO jobs(reservation_id,kind,created_at,updated_at) VALUES(1,'restore',?,?)", (t0, t0))
e_b = raises(c, "INSERT INTO jobs(reservation_id,kind,created_at,updated_at) VALUES(1,'bogus',?,?)", (t0, t0))
check("B2 jobs.kind 'restore' accepted, 'bogus' refused (new connection)", e_r is None and e_b is not None, e_b or "")
try:
    migrations.refuse_reverted_schema(c)
    check("B3 guard M1..M6 passes on the v4 schema", True)
except migrations.RevertedSchema as ex:
    check("B3 guard M1..M6 passes on the v4 schema", False, str(ex))
hits = [label for label, fn in migrations._REVERTED_MARKERS if fn(c)]
check("B3b no marker fires individually", not hits, str(hits))
ss = [r for r in c.execute("PRAGMA table_info(reservations)") if r[1] == "save_snapshot"][0]
check("B4 M6 input: reservations.save_snapshot dflt_value", ss[4] == "0", f"{ss[4]!r} (M6 needs '1')")
check("B5 integrity_check ok, foreign_key_check empty",
      c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
      and not c.execute("PRAGMA foreign_key_check").fetchall())
names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' "
                                 "AND tbl_name IN ('keys','key_challenges','snapshots','admin_approvals') "
                                 "ORDER BY name")]
print("new indexes:", names)
check("B6 no reuse of the reverted index names",
      not {"idx_snapshots_owner", "idx_snapshots_reservation"} & set(names))
jobs_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
print("jobs kind line now:", [ln.strip() for ln in jobs_sql.splitlines() if "kind IN" in ln])
print("jobs sql tail:", repr(jobs_sql[-80:]))
c.close()

print("\nSUMMARY:", sum(ok for _, ok in results), "PASS /", sum(not ok for _, ok in results), "FAIL")
