#!/bin/sh
# Usage: run-stage.sh <log-name> <venv-dir-name> <pytest args...>
# Runs pytest in ~/b104b with the given venv and writes ~/notes/b104b/build/<log-name>,
# starting with a header. The log is produced only by this script (never edited by hand).
set -u
LOG="$HOME/notes/b104b/build/$1"; VENV="$2"; shift 2
cd "$HOME/b104b" || exit 2
PY="$HOME/b104b/$VENV/bin/python"
{
  echo "# date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# worktree:    $HOME/b104b (branch $(git rev-parse --abbrev-ref HEAD))"
  echo "# HEAD:        $(git rev-parse HEAD)"
  echo "# tree:        $(git write-tree)"
  echo "# status:      $(git status --short | wc -l) entries (git status --short)"
  git status --short | sed 's/^/#   /'
  echo "# venv:        $HOME/b104b/$VENV"
  "$PY" -I -c 'import sys,sqlite3,importlib.metadata as m;print("# python:     ",sys.version.split()[0]);print("# ansible-core:",m.version("ansible-core"));print("# ansible-runner:",m.version("ansible-runner"));print("# pytest:     ",m.version("pytest"));print("# sqlite:     ",sqlite3.sqlite_version)'
  echo "# command:     $VENV/bin/python -m pytest $*"
  echo
} > "$LOG"
"$PY" -m pytest "$@" >> "$LOG" 2>&1
rc=$?
echo "# exit: $rc" >> "$LOG"
tail -n 3 "$LOG"
exit 0
