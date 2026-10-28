#!/usr/bin/env bash
# scripts/run/xo.sh — Xen Orchestra for the lab's XCP-ng pools, in a container (podman).
#
# XO built from sources (the community image ronivay/xen-orchestra), pinned to one release. The
# pools are added in its web UI (Settings -> Servers) with their root credentials, so those never
# pass through daolab; daolab talks to XO with an API token from a file (DAOLAB_XO_TOKEN_FILE).
#
# Listens on 127.0.0.1 only: a fresh XO takes admin@admin.net / admin until that is changed, and
# once the pools are added it can do anything to them.
#
# Usage:
#   scripts/run/xo.sh [start]   # create or start the container; prints the URL
#   scripts/run/xo.sh stop | status | logs
# Settings: DAOLAB_XO_VERSION (5.211.1), DAOLAB_XO_PORT (8443).
# State (users, pools, tokens; logs) is in the podman volumes daolab-xo-redis / daolab-xo-data.
set -euo pipefail

IMAGE="docker.io/ronivay/xen-orchestra:${DAOLAB_XO_VERSION:-5.211.1}"
NAME=daolab-xo
PORT="${DAOLAB_XO_PORT:-8443}"

case "${1:-start}" in
    start)
        if podman container exists "${NAME}"; then
            podman start "${NAME}" >/dev/null
        else
            podman run -d --name "${NAME}" --stop-timeout 60 \
                -p "127.0.0.1:${PORT}:443" \
                -e HTTP_PORT=80 -e HTTPS_PORT=443 \
                -v daolab-xo-redis:/var/lib/redis \
                -v daolab-xo-data:/var/lib/xo-server \
                "${IMAGE}" >/dev/null
        fi
        echo "Xen Orchestra ${IMAGE##*:}: https://127.0.0.1:${PORT}/  (self-signed certificate)"
        ;;
    stop)   podman stop -t 60 "${NAME}" ;;
    status) podman ps -a --filter "name=^${NAME}\$" --format '{{.Names}}  {{.Status}}  {{.Ports}}  {{.Image}}' ;;
    logs)   podman logs --tail 50 "${NAME}" ;;
    *) echo "usage: $0 [start|stop|status|logs]" >&2; exit 2 ;;
esac
