#!/bin/sh
# Usage: run-stage.sh <log-name> <venv-dir-name> <pytest args...>
# Runs pytest in ~/x-age with the given venv (its bin/ first on PATH) and writes
# ~/notes/x-age/build/<log-name>, starting with a header. The log is produced only by this
# script (never edited by hand).
set -u
LOG="$HOME/notes/x-age/build/$1"; VENV="$2"; shift 2
cd "$HOME/x-age" || exit 2
PY="$HOME/x-age/$VENV/bin/python"
PATH="$HOME/x-age/$VENV/bin:/usr/local/bin:/usr/bin:/bin"; export PATH
{
  echo "# date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# worktree:    $HOME/x-age (branch $(git rev-parse --abbrev-ref HEAD))"
  echo "# HEAD:        $(git rev-parse HEAD)"
  echo "# tree:        $(git write-tree)   (index; git add -A before each stage)"
  echo "# status:      $(git status --short | wc -l) entries (git status --short)"
  git status --short | sed 's/^/#   /'
  echo "# venv:        $HOME/x-age/$VENV"
  echo "# PATH:        $PATH"
  echo "# age on PATH: $(command -v age || echo none)"
  "$PY" -I -c 'import sys,sqlite3,importlib.metadata as m;print("# python:     ",sys.version.split()[0]);print("# ansible-core:",m.version("ansible-core"));print("# ansible-runner:",m.version("ansible-runner"));print("# pytest:     ",m.version("pytest"));print("# sqlite:     ",sqlite3.sqlite_version)'
  echo "# command:     $VENV/bin/python -m pytest $*"
  echo
} > "$LOG"
"$PY" -m pytest "$@" >> "$LOG" 2>&1
rc=$?
echo "# exit: $rc" >> "$LOG"
grep -E '^(FAILED|ERROR|[0-9]+ (passed|failed)|=+ .*(passed|failed|error).* =+$)' "$LOG" | tail -n 3
exit 0
