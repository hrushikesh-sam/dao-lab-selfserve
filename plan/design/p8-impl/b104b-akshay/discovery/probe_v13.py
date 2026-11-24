"""B-104 PR B discovery, probe 1. Read-only against the repo.

Imports origin/rama a67d4ac's service/ from an exported copy (git archive) and migrates
a throwaway DB under /tmp/b104b-probe/. Prints the stored CREATE TABLE text for jobs and
the reservations/setups columns, the guard result, and SQLite facts.

Usage: <venv>/bin/python -I probe_v13.py /tmp/b104b-probe
"""
import os
import sqlite3
import sys

root = sys.argv[1]
sys.path.insert(0, root)                       # the exported service/ package only
from service import migrations, models          # noqa: E402

print("python", sys.version.split()[0], "| sqlite", sqlite3.sqlite_version)
print("service from:", os.path.dirname(models.__file__))
print("latest_version():", migrations.latest_version(), "| migrations:",
      [(m.version, m.name) for m in migrations.MIGRATIONS][-3:])

db = os.path.join(root, "v13.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(db + suffix):
        os.remove(db + suffix)
c = models.connect(db)
print("foreign_keys during init:", c.execute("PRAGMA foreign_keys").fetchone()[0],
      "| isolation_level:", repr(c.isolation_level))
applied = models.init_db(c)
print("applied:", applied, "| user_version:", migrations.current_version(c))
jobs_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
print("\n--- sqlite_master.sql for jobs (repr) ---")
print(repr(jobs_sql))
print("--- sqlite_master.sql for jobs (raw) ---")
print(jobs_sql)
OLD = "'deploy', 'cleanup', 'health_probe', 'reconcile')"
print("\n_OLD_JOB_KINDS (DDL §3.8) present:", OLD in jobs_sql, "| count:", jobs_sql.count(OLD),
      "| 'restore' present:", "'restore'" in jobs_sql)
print("kind CHECK line:", [ln.strip() for ln in jobs_sql.splitlines() if "kind IN" in ln])
print("\nreservations cols:", [r[1] for r in c.execute("PRAGMA table_info(reservations)")])
print("setups cols:", [r[1] for r in c.execute("PRAGMA table_info(setups)")])
print("jobs cols:", [r[1] for r in c.execute("PRAGMA table_info(jobs)")])
print("tables:", [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
migrations.refuse_reverted_schema(c)
print("guard on our own v13: passes")
print("schema_version:", c.execute("PRAGMA schema_version").fetchone()[0],
      "| integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
opts = [r[0] for r in c.execute("PRAGMA compile_options")]
print("compile_options of interest:",
      [o for o in opts if any(k in o for k in ("DEFENSIVE", "FOREIGN", "DQS", "THREADSAFE", "ENABLE_FTS5"))])
c.close()
