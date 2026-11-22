# Phase 8 gate-review artifacts

This directory is a review-only artifact bundle for the operator runbook. Do not merge this branch.

## Status

- Phase 8 and Phase 9 are stopped.
- `P-0-final.patch` and `F-5-1.patch` are draft patches, not production-approved changes.
- Row 25 (`daolab_vm_names`) failed the harness: the canary executed in destroy mode on the fake lab. `P-0c` is still required.
- Local red/green and full-suite evidence is included under `evidence/`.
- Production DB audit, production F-5-1 grep, and production fact collection were not run.
- No runtime claims are made beyond the attached evidence.

## Core files

- `p8-snapshot-design-v1.1.md` — consolidated snapshot/restore design.
- `all-phases-summary.md` — all-phase audit and design summary.
- `OPERATOR-RUNBOOK.md` — operator steps for local gates and production facts.
- `EXTRAVAR-MATRIX.md` — static review of Ansible extravars.
- `audit_stored_inputs.py` — read-only stored-data audit script.
- `M-VM-NAMES-HARNESS.md` — row 25 harness notes.
- `test_orphans_canary_DRAFT.py` — row 25 canary test draft.
- `P-0-final.patch` — draft patch for the controller templating RCE class.
- `F-5-1.patch` — draft patch removing Ansible secret persistence.
- `diffstat-final-corrected.txt` — corrected combined diffstat.

## Evidence

`evidence/STATUS.md` summarises the local run. The `evidence/` tree contains local red/green outputs, full-suite logs, JUnit XML, skip lists, the row 25 harness result, and NOT RUN stubs for production-only gates.

## Applying patches

Use `git apply` from the repository root. Do not copy-paste patch bodies; `.gitattributes` in this directory preserves patch bytes.
