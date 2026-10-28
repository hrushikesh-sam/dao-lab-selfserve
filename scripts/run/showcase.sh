#!/usr/bin/env bash
# scripts/run/showcase.sh — the dashboards against the real lab, with VPP as the use case to deploy.
#
# Deploy on the user dashboard runs Akshay's VPP runbook (VPP_ansible/) on the real DPU,
# xeon3-oct-112 (inventory/hosts.yml), and hands the user `ssh lab@<DPU>` with their SSH key or a
# generated password; End now (or the end of the slot) revokes that login and gives the board back
# to nginx-tls. The admin dashboard shows every reservation's jobs, steps and playbook log.
#
# DPU only for now: the use case's VMs (tgen, sink) wait for the Xen Orchestra pools, so they are
# listed as "not provisioned". Every other use case is in the catalog as "coming soon".
#
# Usage:
#   scripts/run/showcase.sh            # API + engine on :8080, keeps the showcase DB
#   scripts/run/showcase.sh --fresh    # start from an empty showcase DB (only with the board at rest)
#
# Needs, on this machine: SSH as dao-test to the DPU (key in ~/.ssh or the agent) and, since
# dao-test's sudo asks for a password, ~/.daolab-become-dao-test holding only that password
# (chmod 600) - or DAOLAB_BECOME_PASSWORD_FILE pointing at such a file.
# Any DAOLAB_* set in the environment wins over the values below; dev.sh does the rest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export DAOLAB_DB_PATH="${DAOLAB_DB_PATH:-./var/showcase.db}"
export DAOLAB_ARTIFACTS_DIR="${DAOLAB_ARTIFACTS_DIR:-./var/showcase-jobs}"
export DAOLAB_RUNNER="${DAOLAB_RUNNER:-ansible}"
export DAOLAB_DEPLOYABLE="${DAOLAB_DEPLOYABLE:-vpp}"
export DAOLAB_DEPLOY_VMS="${DAOLAB_DEPLOY_VMS:-false}"
# the board owner's sign-off: the runbooks stop nginx-tls and rebind the port to vfio-pci
export DAOLAB_ALLOW_DESTRUCTIVE="${DAOLAB_ALLOW_DESTRUCTIVE:-true}"
# a click deploys within seconds: one-minute slots, a one-minute lead, a 5 s engine tick
export DAOLAB_SLOT_MIN="${DAOLAB_SLOT_MIN:-1}"
export DAOLAB_PROVISION_LEAD_MIN="${DAOLAB_PROVISION_LEAD_MIN:-1}"
export DAOLAB_HW_CLASS_LEAD_MIN="${DAOLAB_HW_CLASS_LEAD_MIN:-1}"
export DAOLAB_CLEANUP_MIN="${DAOLAB_CLEANUP_MIN:-1}"
export DAOLAB_ENGINE_INTERVAL_SEC="${DAOLAB_ENGINE_INTERVAL_SEC:-5}"
export DAOLAB_DEFAULT_HOURS="${DAOLAB_DEFAULT_HOURS:-1}"

BECOME="${DAOLAB_BECOME_PASSWORD_FILE:-$HOME/.daolab-become-dao-test}"
if [ -f "${BECOME}" ]; then
    export DAOLAB_BECOME_PASSWORD_FILE="${BECOME}"
else
    echo "==> note: no ${BECOME}; the runbooks' sudo steps fail unless dao-test has passwordless sudo"
fi

DPU="$(sed -n 's/^ *ansible_host: *\([0-9.]*\).*xeon3-oct-112.*/\1/p' inventory/hosts.yml | head -1)"
echo "==> checking the DPU ${DPU:-?} as dao-test"
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "dao-test@${DPU}" true 2>/dev/null; then
    echo "error: cannot ssh dao-test@${DPU} with a key; deploys would fail" >&2
    exit 1
fi

mkdir -p "$(dirname "${DAOLAB_DB_PATH}")" "${DAOLAB_ARTIFACTS_DIR}"
exec scripts/run/dev.sh "$@"
