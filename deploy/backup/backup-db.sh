#!/usr/bin/env bash
# DB backup timer script (B-101)
DB_PATH=\
BACKUP_DIR=\
mkdir -p \"\\"
TS=\20261028T131257Z
cp \"\\" \"\/daolab-\.db\" 2>/dev/null || sqlite3 \"\\" \".backup '\/daolab-\.db'\"
# Keep last 14 days
find \"\\" -name 'daolab-*.db' -mtime +14 -delete 2>/dev/null || true
