#!/usr/bin/env bash
# scripts/run/dev.sh — one-shot dev runner for daolab.
#
# Boots the whole stack: the API process (which also serves both dashboards) *and* the
# lifecycle engine (`python -m service.engine --loop`). It seeds the catalog from usecases/ +
# setups/, (re)creates the three test users, prints their tokens, and runs both in the
# foreground until Ctrl-C, which stops both (B-30).
#
# Usage:
#   scripts/run/dev.sh            # API + engine on DAOLAB_API_PORT (default 8080)
#   scripts/run/dev.sh --fresh    # wipe the dev DB first, then start
#   scripts/run/dev.sh --api-only # serve the API only, without the engine
#
# Respects DAOLAB_* environment variables; falls back to .env, then defaults.
# Test users are `alice`, `bob` (role user) and `dev-admin` (admin) — they are rotated
# (deleted + re-created) on every run so their tokens can always be printed. Real users
# are untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- settings: real env > .env > defaults (same precedence as the app) -------
_PORT="${DAOLAB_API_PORT:-}"; _HOST="${DAOLAB_API_HOST:-}"; _DB="${DAOLAB_DB_PATH:-}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export DAOLAB_API_PORT="${_PORT:-${DAOLAB_API_PORT:-8080}}"
export DAOLAB_API_HOST="${_HOST:-${DAOLAB_API_HOST:-0.0.0.0}}"
export DAOLAB_DB_PATH="${_DB:-${DAOLAB_DB_PATH:-./daolab.db}}"

FRESH=0
API_ONLY=0
for arg in "$@"; do
    case "${arg}" in
        --fresh)    FRESH=1 ;;
        --api-only) API_ONLY=1 ;;
        *) echo "unknown option: ${arg}" >&2; exit 2 ;;
    esac
done

if [ "${FRESH}" = 1 ]; then
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

# --- test users: rotate so the tokens can always be printed ------------------
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

ALICE_TOKEN="$(.venv/bin/daolab add-user alice "$PUBKEY" --rotate | sed -n 's/^token: //p')"
BOB_TOKEN="$(.venv/bin/daolab add-user bob "$PUBKEY" --rotate | sed -n 's/^token: //p')"
ADMIN_TOKEN="$(.venv/bin/daolab add-user dev-admin "$PUBKEY" --role admin --rotate | sed -n 's/^token: //p')"

# --- free the port, then serve ----------------------------------------------
lsof -ti:"${DAOLAB_API_PORT}" -sTCP:LISTEN 2>/dev/null | xargs -r kill || true

URL_HOST="${DAOLAB_API_HOST}"
[ "$URL_HOST" = "0.0.0.0" ] && URL_HOST="localhost"
if [ "${API_ONLY}" = 1 ]; then
    ENGINE_LINE="engine           disabled (--api-only)"
else
    ENGINE_LINE="engine           running (python -m service.engine --loop)"
fi
cat <<BANNER

==========================================================================
 daolab dev stack
   user dashboard   http://${URL_HOST}:${DAOLAB_API_PORT}/
   admin dashboard  http://${URL_HOST}:${DAOLAB_API_PORT}/admin.html
   db               ${DAOLAB_DB_PATH}
   ${ENGINE_LINE}

 tokens (paste into the dashboard token gate):
   alice      (user)   ${ALICE_TOKEN}
   bob        (user)   ${BOB_TOKEN}
   dev-admin  (admin)  ${ADMIN_TOKEN}

 Ctrl-C stops the whole stack (API + engine). Tokens rotate on every run.
==========================================================================

BANNER

# --- run the stack: API, plus the engine unless --api-only -------------------
# Both run in the foreground under one trap, so Ctrl-C (or either process
# exiting) tears the whole stack down (B-30).
PIDS=()
cleanup() {
    trap - INT TERM EXIT
    echo
    echo "==> shutting down daolab (stopping pids: ${PIDS[*]:-none})..."
    for pid in "${PIDS[@]:-}"; do
        kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

.venv/bin/python -m uvicorn service.api:app \
    --host "${DAOLAB_API_HOST}" --port "${DAOLAB_API_PORT}" &
PIDS+=("$!")

if [ "${API_ONLY}" = 0 ]; then
    .venv/bin/python -m service.engine --loop &
    PIDS+=("$!")
fi

# Block until the first process exits; the EXIT trap then stops the rest.
wait -n 2>/dev/null || wait 2>/dev/null || true
