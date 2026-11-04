"""B-101 (mistakes.md J15): deploy/backup/backup-db.sh makes a checked online backup of a live
WAL database and prunes old ones."""
import os
import sqlite3
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "deploy", "backup", "backup-db.sh")


def run(db, backups, **env):
    e = {**os.environ, "DAOLAB_DB_PATH": str(db), "DAOLAB_BACKUP_DIR": str(backups),
         "DAOLAB_PYTHON": sys.executable, **env}
    return subprocess.run(["bash", SCRIPT], env=e, capture_output=True, text=True, timeout=60)


def live_db(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    return conn                                   # kept open: rows sit in the -wal file


def test_backup_of_a_live_wal_db_has_every_row(tmp_path):
    db, backups = tmp_path / "daolab.db", tmp_path / "backups"
    conn = live_db(db)
    p = run(db, backups)
    assert p.returncode == 0, p.stderr
    files = list(backups.glob("daolab-*.db"))
    assert len(files) == 1 and f"backup: {files[0]}" in p.stdout
    assert not list(backups.glob("*.partial"))
    copy = sqlite3.connect(files[0])
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 100
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_old_backups_are_pruned_and_recent_ones_kept(tmp_path):
    db, backups = tmp_path / "daolab.db", tmp_path / "backups"
    live_db(db).close()
    backups.mkdir()
    old, recent = backups / "daolab-20200101T000000Z.db", backups / "daolab-20991231T000000Z.db"
    other = backups / "notes.txt"
    for f in (old, recent, other):
        f.write_text("x")
    month_ago = time.time() - 30 * 86400
    os.utime(old, (month_ago, month_ago))
    os.utime(other, (month_ago, month_ago))
    p = run(db, backups, DAOLAB_BACKUP_KEEP_DAYS="14")
    assert p.returncode == 0, p.stderr
    assert not old.exists() and recent.exists() and other.exists()   # only daolab-*.db pruned
    assert f"backup: pruned {old}" in p.stdout


def test_a_missing_db_fails_and_writes_nothing(tmp_path):
    p = run(tmp_path / "nope.db", tmp_path / "backups")
    assert p.returncode != 0 and "no database at" in p.stderr
    assert not (tmp_path / "backups").exists()


def test_the_script_runs_as_a_program(tmp_path):
    """No BOM or CRLF in front of the shebang: systemd runs it directly (ExecStart)."""
    with open(SCRIPT, "rb") as f:
        head = f.read(64)
    assert head.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in open(SCRIPT, "rb").read()
    assert os.access(SCRIPT, os.X_OK)
