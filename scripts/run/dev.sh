#!/usr/bin/env bash
# scripts/run/dev.sh — one-shot dev runner for daolab.
#
# Boots the whole stack (the API process also serves both dashboards), seeds the
# catalog from usecases/ + setups/, (re)creates the two dev users, prints their
# tokens, and runs the server in the foreground until Ctrl-C.
#
# Usage:
#   scripts/run/dev.sh            # start on DAOLAB_API_PORT (default 8080)
#   scripts/run/dev.sh --fresh    # wipe the dev DB first, then start
#
# Respects DAOLAB_* environment variables; falls back to .env, then defaults.
# Dev users are `dev` and `dev-admin` — they are rotated (deleted + re-created)
# on every run so their tokens can always be printed. Real users are untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- settings: real env > .env > defaults (same precedence as the app) -------
_PORT="${DAOLAB_API_PORT:-}"; _HOST="${DAOLAB_API_HOST:-}"; _DB="${DAOLAB_DB_PATH:-}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export DAOLAB_API_PORT="${_PORT:-${DAOLAB_API_PORT:-8080}}"
export DAOLAB_API_HOST="${_HOST:-${DAOLAB_API_HOST:-0.0.0.0}}"
export DAOLAB_DB_PATH="${_DB:-${DAOLAB_DB_PATH:-./daolab.db}}"

if [ "${1:-}" = "--fresh" ]; then
    echo "==> --fresh: removing ${DAOLAB_DB_PATH}"
    rm -f "${DAOLAB_DB_PATH}" "${DAOLAB_DB_PATH}-wal" "${DAOLAB_DB_PATH}-shm"
fi

# --- venv + install ----------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
    echo "==> creating .venv"
    python3 -m venv .venv
fi
if [ ! -x .venv/bin/daolab ]; then
    echo "==> installing daolab (pip install -e .)"
    .venv/bin/python -m pip install -q -e .
fi

# --- .env with an internal secret -------------------------------------------
if [ ! -f .env ]; then
    echo "==> creating .env from .env.example"
    cp .env.example .env
fi
if ! grep -q '^DAOLAB_INTERNAL_SECRET=..*' .env; then
    SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s|^DAOLAB_INTERNAL_SECRET=.*|DAOLAB_INTERNAL_SECRET=${SECRET}|" .env
    echo "==> generated DAOLAB_INTERNAL_SECRET in .env"
fi

# --- DB: schema + catalog ----------------------------------------------------
.venv/bin/daolab init
.venv/bin/daolab sync

# --- dev users: rotate so the tokens can always be printed -------------------
PUBKEY=""
for f in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_ecdsa.pub" "$HOME/.ssh/id_rsa.pub"; do
    [ -f "$f" ] && PUBKEY="$f" && break
done
if [ -z "$PUBKEY" ]; then
    mkdir -p .dev
    [ -f .dev/dev_key ] || ssh-keygen -q -t ed25519 -N '' -f .dev/dev_key
    PUBKEY=".dev/dev_key.pub"
    echo "==> no ~/.ssh key found; using generated .dev/dev_key"
fi

.venv/bin/python - <<'PY'
from service import models
conn = models.connect()
for name in ("dev", "dev-admin"):
    row = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
    if row:  # reservations first (users FK has no cascade); their vms cascade
        conn.execute("DELETE FROM reservations WHERE user_id = ?", (row["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
conn.close()
PY
USER_TOKEN="$(.venv/bin/daolab add-user dev "$PUBKEY" | sed -n 's/^token: //p')"
ADMIN_TOKEN="$(.venv/bin/daolab add-user dev-admin "$PUBKEY" --role admin | sed -n 's/^token: //p')"

# --- free the port, then serve ----------------------------------------------
lsof -ti:"${DAOLAB_API_PORT}" -sTCP:LISTEN 2>/dev/null | xargs -r kill || true

URL_HOST="${DAOLAB_API_HOST}"
[ "$URL_HOST" = "0.0.0.0" ] && URL_HOST="localhost"
cat <<BANNER

==========================================================================
 daolab dev server
   user dashboard   http://${URL_HOST}:${DAOLAB_API_PORT}/
   admin dashboard  http://${URL_HOST}:${DAOLAB_API_PORT}/admin.html
   db               ${DAOLAB_DB_PATH}

 tokens (paste into the dashboard token gate):
   dev        (user)   ${USER_TOKEN}
   dev-admin  (admin)  ${ADMIN_TOKEN}

 Ctrl-C stops the server. Tokens rotate on every run of this script.
==========================================================================

BANNER

exec .venv/bin/python -m uvicorn service.api:app \
    --host "${DAOLAB_API_HOST}" --port "${DAOLAB_API_PORT}"
