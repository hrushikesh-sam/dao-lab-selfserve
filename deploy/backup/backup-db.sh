#!/usr/bin/env bash
# deploy/backup/backup-db.sh — one online backup of the daolab DB, then prune old ones (B-101).
#
# sqlite's online backup API (never `cp` the live file, plan/08 "Backup"): safe under WAL while
# the API and the engine keep writing. The copy is checked with `PRAGMA integrity_check` and only
# then renamed into place, so a half-written or corrupt file never looks like a backup.
#
#   DAOLAB_DB_PATH            the live DB (from /opt/daolab/.env; relative = from the cwd)
#   DAOLAB_BACKUP_DIR         where backups go            (default: <DB dir>/backups)
#   DAOLAB_BACKUP_KEEP_DAYS   delete backups older than N days (default 14; 0 = keep all)
#   DAOLAB_PYTHON             interpreter with sqlite3     (default: python3)
#
# Exit 0 = backup written and checked; non-zero = no new backup (the timer's unit shows failed).
# Restore: see deploy/backup/README.md.
set -euo pipefail

DB="${DAOLAB_DB_PATH:?DAOLAB_DB_PATH is not set}"
[ -f "$DB" ] || { echo "backup: no database at $DB" >&2; exit 1; }
DIR="${DAOLAB_BACKUP_DIR:-$(dirname "$DB")/backups}"
KEEP="${DAOLAB_BACKUP_KEEP_DAYS:-14}"
PY="${DAOLAB_PYTHON:-python3}"

mkdir -p "$DIR"
OUT="$DIR/daolab-$(date -u +%Y%m%dT%H%M%SZ).db"
TMP="$OUT.partial"
trap 'rm -f "$TMP"' EXIT

"$PY" - "$DB" "$TMP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
    s.backup(d)
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"backup: integrity_check on the copy says: {ok}")
PY
mv "$TMP" "$OUT"
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"

if [ "$KEEP" -gt 0 ]; then
    find "$DIR" -maxdepth 1 -name 'daolab-*.db' -mtime +"$KEEP" -print -delete \
        | sed 's/^/backup: pruned /'
fi
