"""B-104 discovery probe. Synthetic in-memory DBs only; never opens a file DB.
Loads migrations.py copies extracted with `git show` (origin/rama, 4ec115e)."""
import importlib.util
import sqlite3
import sys

D = sys.argv[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rama = load(f"{D}/rama/mig_rama.py", "mig_rama")
rev = load(f"{D}/rev/mig_rev.py", "mig_rev")
SCHEMA = open(f"{D}/rama/schema.sql").read()


def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    return c


def cols(c, t):
    return {r[1]: (r[2], r[3], r[4]) for r in c.execute(f"PRAGMA table_info({t})")}


print("sqlite", sqlite3.sqlite_version, "| rama latest", rama.latest_version(),
      "| reverted latest", rev.latest_version())

# 1. reverted v13 DB fingerprint
c = conn(); rev.apply(c)
print("\n[1] reverted v13 DB: user_version", rama.current_version(c))
for t in ("user_keys", "snapshots"):
    print("  table", t, sorted(cols(c, t)))
print("  reservations.save_snapshot", cols(c, "reservations").get("save_snapshot"))
print("  setups.snapshot_bdfs", cols(c, "setups").get("snapshot_bdfs"))
print("  jobs.snapshot_id", cols(c, "jobs").get("snapshot_id"), "jobs.secret", cols(c, "jobs").get("secret"))
jobs_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
print("  jobs CHECK has 'restore':", "'restore'" in jobs_sql)
snap_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='snapshots'").fetchone()[0]
print("  snapshots CHECK has 'saving':", "'saving'" in snap_sql)

# 2. the trap: today's apply() on that DB with code whose latest is 14
noop = lambda c: None
def m14(c):
    c.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE)")
    rama.add_column(c, "reservations", "save_snapshot",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (save_snapshot IN (0, 1))")
rama.MIGRATIONS = [*rama.MIGRATIONS, rama.Migration(12, "p12", noop),
                   rama.Migration(13, "p13", noop), rama.Migration(14, "new14", m14)]
applied = rama.apply(c)
print("\n[2] today's apply() + placeholders + naive 14 on reverted v13: applied", applied,
      "user_version", rama.current_version(c))
print("  snapshots columns now", sorted(cols(c, "snapshots")), "(public_id missing => old table kept)")
print("  save_snapshot default", cols(c, "reservations")["save_snapshot"][2], "(1 = reverted, not 0)")

# 3. reverted v12-only DB (stopped between reverted 12 and 13)
c = conn()
rev.MIGRATIONS = rev.MIGRATIONS[:12]
rev.apply(c)
print("\n[3] reverted v12-only DB: user_version", rama.current_version(c),
      "user_keys", rama.has_table(c, "user_keys"), "jobs.secret", rama.has_column(c, "jobs", "secret"))

# 4. FK behaviour: catalog sync deletes released reservations (catalog.py:564/578)
c = conn()
rama_fresh = load(f"{D}/rama/mig_rama.py", "mig_rama2"); rama_fresh.apply(c)
now = "2026-11-23T00:00:00Z"
c.execute("INSERT INTO users(username,token_hash,ssh_pubkey,created_at,updated_at) VALUES('u','h','k',?,?)", (now, now))
c.execute("INSERT INTO pools(name,created_at,updated_at) VALUES('p',?,?)", (now, now))
c.execute("INSERT INTO setups(name,pool_id,yaml_path,created_at,updated_at) VALUES('s',1,'y',?,?)", (now, now))
c.execute("INSERT INTO usecases(name,created_at,updated_at) VALUES('uc',?,?)", (now, now))
c.execute("""INSERT INTO reservations(user_id,usecase_id,setup_id,state,requested_start_at,start_at,
             end_at,created_at,updated_at) VALUES(1,1,1,'released',?,?,?,?,?)""", (now,) * 5)
rama_fresh.add_column(c, "reservations", "save_snapshot",
                      "INTEGER NOT NULL DEFAULT 0 CHECK (save_snapshot IN (0, 1))")
print("\n[4] legacy row save_snapshot after ADD COLUMN DEFAULT 0:",
      c.execute("SELECT save_snapshot FROM reservations").fetchone()[0])
try:
    c.execute("UPDATE reservations SET save_snapshot=2")
    print("  CHECK (0,1): NOT enforced")
except sqlite3.IntegrityError as e:
    print("  CHECK (0,1) enforced:", e)
c.execute("""CREATE TABLE snap_noaction (id INTEGER PRIMARY KEY,
             reservation_id INTEGER REFERENCES reservations(id))""")
c.execute("INSERT INTO snap_noaction(reservation_id) VALUES(1)")
c.execute("BEGIN")
try:
    c.execute("DELETE FROM reservations WHERE id=1")
    print("  FK NO ACTION: delete allowed")
except sqlite3.IntegrityError as e:
    print("  FK NO ACTION blocks the sync delete:", e)
c.execute("ROLLBACK")
c.execute("""CREATE TABLE snap_setnull (id INTEGER PRIMARY KEY,
             reservation_id INTEGER REFERENCES reservations(id) ON DELETE SET NULL)""")
c.execute("DROP TABLE snap_noaction")
c.execute("INSERT INTO snap_setnull(reservation_id) VALUES(1)")
c.execute("DELETE FROM reservations WHERE id=1")
print("  FK SET NULL: row kept, reservation_id =",
      c.execute("SELECT reservation_id FROM snap_setnull").fetchone()[0])

# 5. ADD COLUMN with REFERENCES and NULL default under foreign_keys=ON
c = conn(); load(f"{D}/rama/mig_rama.py", "mig_rama3").apply(c)
c.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
c.execute("ALTER TABLE jobs ADD COLUMN snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL")
print("\n[5] ADD COLUMN jobs.snapshot_id REFERENCES ... ON DELETE SET NULL: ok")

# 6. composite FK (key_id, user_id) -> keys(id, user_id)
c = sqlite3.connect(":memory:", isolation_level=None); c.execute("PRAGMA foreign_keys=ON")
c.execute("CREATE TABLE keys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, UNIQUE (id, user_id))")
c.execute("""CREATE TABLE ch (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, key_id INTEGER NOT NULL,
             FOREIGN KEY (key_id, user_id) REFERENCES keys(id, user_id))""")
c.execute("INSERT INTO keys VALUES (1, 10)")
c.execute("INSERT INTO ch(user_id,key_id) VALUES (10, 1)")
try:
    c.execute("INSERT INTO ch(user_id,key_id) VALUES (11, 1)")
    print("[6] composite FK: NOT enforced")
except sqlite3.IntegrityError as e:
    print("[6] composite FK rejects another user's key:", e)

# 7. partial unique index: one active key per user
c.execute("ALTER TABLE keys ADD COLUMN state TEXT")
c.execute("CREATE UNIQUE INDEX one_active ON keys(user_id) WHERE state = 'active'")
c.execute("UPDATE keys SET state='active'")
c.execute("INSERT INTO keys VALUES (2, 10, 'pending')")
try:
    c.execute("UPDATE keys SET state='active' WHERE id=2")
    print("[7] partial unique: NOT enforced")
except sqlite3.IntegrityError as e:
    print("[7] partial unique index blocks a 2nd active key:", e)
