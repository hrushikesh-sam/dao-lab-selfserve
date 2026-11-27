#!/bin/sh
# Usage: run-stage.sh <log-name> <venv-dir-name> <pytest args...>
# Runs pytest in ~/x-ml with the given venv and writes ~/notes/x-ml/build/<log-name>,
# starting with a header. The log is produced only by this script (never edited by hand).
set -u
LOG="$HOME/notes/x-ml/build/$1"; VENV="$2"; shift 2
cd "$HOME/x-ml" || exit 2
PY="$HOME/x-ml/$VENV/bin/python"
# Pin ansible to this venv: tests/test_playbooks.py takes DAOLAB_TEST_ANSIBLE_PLAYBOOK, then
# whatever ansible-playbook is first on PATH (the shell's PATH may start with another venv).
PATH="$HOME/x-ml/$VENV/bin:$PATH"; export PATH
DAOLAB_TEST_ANSIBLE_PLAYBOOK="$HOME/x-ml/$VENV/bin/ansible-playbook"; export DAOLAB_TEST_ANSIBLE_PLAYBOOK
{
  echo "# date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# worktree:    $HOME/x-ml (branch $(git rev-parse --abbrev-ref HEAD))"
  echo "# HEAD:        $(git rev-parse HEAD)"
  echo "# tree:        $(git write-tree)"
  echo "# status:      $(git status --short | wc -l) entries (git status --short)"
  git status --short | sed 's/^/#   /'
  echo "# venv:        $HOME/x-ml/$VENV"
  "$PY" -I -c 'import sys,sqlite3,importlib.metadata as m;print("# python:     ",sys.version.split()[0]);print("# ansible-core:",m.version("ansible-core"));print("# ansible-runner:",m.version("ansible-runner"));print("# pytest:     ",m.version("pytest"));print("# sqlite:     ",sqlite3.sqlite_version)'
  echo "# age:         $(age --version 2>/dev/null || echo absent)"
  echo "# ansible-playbook: $(command -v ansible-playbook) = $DAOLAB_TEST_ANSIBLE_PLAYBOOK"
  echo "# ansible-playbook --version: $("$DAOLAB_TEST_ANSIBLE_PLAYBOOK" --version 2>&1 | head -n 1)"
  echo "# command:     $VENV/bin/python -m pytest $*"
  echo
} > "$LOG"
"$PY" -m pytest "$@" >> "$LOG" 2>&1
rc=$?
echo "# exit: $rc" >> "$LOG"
tail -n 3 "$LOG"
exit 0
