#!/bin/sh
# Usage: run.sh <log-name> <worktree> <venv-name> <pytest args...>
# Runs pytest in <worktree> with ~/x-ml/<venv-name> and writes ~/notes/x-ml/logs/<log-name>,
# starting with a header. Logs are produced only by this script (never edited by hand).
set -u
LOG="$HOME/notes/x-ml/logs/$1"; WT="$2"; VENV="$HOME/x-ml/$3"; shift 3
mkdir -p "$HOME/notes/x-ml/logs"
cd "$WT" || exit 2
PY="$VENV/bin/python"
# Pin ansible to this venv (tests/test_playbooks.py prefers DAOLAB_TEST_ANSIBLE_PLAYBOOK, then PATH).
PATH="$VENV/bin:$PATH"; export PATH
DAOLAB_TEST_ANSIBLE_PLAYBOOK="$VENV/bin/ansible-playbook"; export DAOLAB_TEST_ANSIBLE_PLAYBOOK
{
  echo "# date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# worktree:    $WT (branch $(git rev-parse --abbrev-ref HEAD))"
  echo "# HEAD:        $(git rev-parse HEAD)"
  echo "# write-tree:  $(git write-tree)   (index; staged changes only)"
  echo "# status:      $(git status --short | grep -v -E '^\?\? \.venv(-220)?/$' | wc -l) entries (git status --short, venvs excluded)"
  git status --short | grep -v -E '^\?\? \.venv(-220)?/$' | sed 's/^/#   /'
  echo "# venv:        $VENV"
  "$PY" -c 'import sys,sqlite3,importlib.metadata as m,service;print("# python:     ",sys.version.split()[0]);print("# ansible-core:",m.version("ansible-core"));print("# ansible-runner:",m.version("ansible-runner"));print("# pytest:     ",m.version("pytest"));print("# sqlite:     ",sqlite3.sqlite_version);print("# service pkg:",service.__file__)'
  echo "# age:         $(age --version 2>/dev/null || echo absent)"
  echo "# ansible-playbook: $(command -v ansible-playbook) = $DAOLAB_TEST_ANSIBLE_PLAYBOOK"
  echo "# ansible-playbook --version: $("$DAOLAB_TEST_ANSIBLE_PLAYBOOK" --version 2>&1 | head -n 1)"
  echo "# command:     $(basename "$VENV")/bin/python -m pytest $*"
  echo
} > "$LOG"
"$PY" -m pytest "$@" >> "$LOG" 2>&1
rc=$?
echo "# exit: $rc" >> "$LOG"
tail -n 4 "$LOG"
exit 0
