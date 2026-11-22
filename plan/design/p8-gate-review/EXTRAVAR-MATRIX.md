# Runner extravar matrix: static review (P-0 extension)

**Basis:** reading the repo at HEAD `40ebd6d` (+ the P-0 patch) only. **No runtime result is asserted in this
document; every runtime statement is UNVERIFIED** until the operator runs the tests named in the last column.
"Template lines" are the output of a text search for the variable on lines containing `{{ }}` in `playbooks/`,
`VPP_ansible/`, `dao-smartnic/`; the counts are approximate (UNVERIFIED precision).

Why it matters: ansible re-templates a string variable wherever a playbook renders it, so a value containing
`{{ lookup('pipe', ...) }}` is evaluated on the controller (the F-1 mechanism; runtime reproduction is the
operator's, see OPERATOR-RUNBOOK.md §3).

Legend: **YES / NO / UNKNOWN**. "User" = any authenticated non-admin user. "ADMIN" = only an admin can set it.
Source of the list: `AnsibleRunner.extravars()` (`service/runner.py:~492-513`) + the two values `_play` adds.

| # | extravar | source (static) | user-controlled | reaches a template | vulnerable | operator canary test |
|---|---|---|---|---|---|---|
| 1 | `reservation_id` | DB integer | NO | YES (~15 lines, `\| int`) | NO | none |
| 2 | `usecase` | must already exist in DB (`create_reservation` 404s otherwise); catalog YAML | NO (catalog/admin) | YES (`tasks/resolve.yml:29`, dataplane lookup in `plan_vms.yml`) | NO under the user threat model; UNKNOWN if catalog authors are untrusted | **C-1** (catalog name/delimiter check) |
| 3 | `setup` | same (diy `body.setup` must exist) | NO | YES (`resolve.yml:17,54`, `plan_vms.yml:84`) | NO (same caveat) | **C-1** |
| 4 | `user` | `cli.py:109` is the only user-insert path found in `service/` | ADMIN | YES (`deploy.yml:66`, `roles/host_vm/tasks/vm.yml:113`) | **YES** for a hostile admin-created name (static); new names refused by the P-0 CLI guard; existing rows need the audit | **A-1** (audit `users.username`) |
| 5 | `ssh_pubkey` | deploy body -> `reservations.ssh_pubkey`; `users.ssh_pubkey` | **YES** | YES (`roles/dpu_access/defaults/main.yml:15`, `tasks/main.yml:51`) | **YES before P-0** (static; reported reproduced); fixed by P-0 | **T-key** (`tests/test_pubkey_strict.py -k canary`) |
| 6 | `hw_class` | usecase catalog | NO | YES (`deploy.yml:45`, ~3 lines) | NO | **C-1** |
| 7 | `diy` | `bool(...)` | NO | NO (0 template lines; used via `\| bool`) | NO | none |
| 8 | `templates` (values) | `ReservationCreate.templates` -> `diy_templates` | **YES** | YES (`tasks/plan_vms.yml:36`) | **YES before P-0** (static); fixed by P-0 | **T-tpl** (`-k canary`, `-k stored_poisoned`) |
| 9 | `daolab_members` | setup members: catalog/inventory | NO | YES (`plan_vms.yml:21-22`, ~5 lines) | NO | **C-1** |
| 10 | `daolab_roles` | usecase YAML | NO | YES (~6 lines) | NO | **C-1** |
| 11 | `daolab_verify` | usecase YAML | NO | YES (~11 lines) | NO | **C-1** |
| 12 | `daolab_vms` | role/host/name from DB (P-0 ident-checks them); `template` (P-0 scrubs); `ip` validated by `ipaddress` (`api.py:1016,1114`) | template: YES before P-0; others NO | YES (~2 lines + loops) | template YES before P-0, fixed; others NO | **T-tpl** (stored row) |
| 13 | `daolab_leases` | lease table IPs | NO | YES (~2 lines) | NO | none |
| 14 | `daolab_api_url` | settings | NO (operator) | YES | NO | none |
| 15 | `daolab_internal_secret` | settings | NO | YES (4 uri headers, all `no_log`) | not injection; **confidentiality: persisted by ansible-runner** (F-5-1) | **F-5-1** (`tests/test_runner_secret.py`) + production grep (runbook §5) |
| 16 | `daolab_otel_collector` | settings | NO | YES | NO | none |
| 17 | `daolab_app` | `dpu_app(roles)` -> catalog | NO | YES (~6 lines) | NO | **C-1** |
| 18 | `daolab_access_login` | constant `"lab"` (`models.ACCESS_LOGIN`) | NO | YES (1 line) | NO | none |
| 19 | `daolab_access_password` | server-generated, `ascii_letters+digits`, 16 chars (`api.py` `_PASSWORD_CHARS`) | NO | YES (1 line) | NO | none |
| 20 | `daolab_access_expires` | ISO date derived from `end_at` | NO | YES (1 line) | NO | none |
| 21 | `daolab_access_until` | int derived from `end_at` | NO | YES (1 line) | NO | none |
| 22 | `daolab_deploy_vms` | settings bool | NO | YES (1 line) | NO | none |
| 23 | `allow_destructive_changes` / `allow_launch` / `allow_cleanup` | settings bool | NO | NO (0 template lines; used in `when:`) | NO | none |
| 24 | `daolab_output_file` (added by `_play`) | path under `artifacts_dir/job-<int>/run-<n>-<key>` | NO | YES (~4 lines) | NO | none |
| 25 | `daolab_vm_names` (added for orphan destroy) | names from `tagged_vms()` (Xen Orchestra listing) | **UNKNOWN** | YES (~2 lines) | **UNKNOWN** | **M-vm-names** (below) |

Not extravars but template inputs: `inventory/` host_vars/group_vars (admin), `daolab_no_log`, and the environment
variables read by `lookup('env', ...)` in `playbooks/group_vars/all.yml` (`DAOLAB_XO_CMD`, `DAOLAB_DOM0_SSH`):
all operator-controlled, NO.

## Operator tests referenced above

* **T-key / T-tpl / F-5-1:** exist in the patches (`tests/test_pubkey_strict.py`, `tests/test_runner_secret.py`).
* **A-1:** `audit_stored_inputs.py` (checks stored keys, template values, VM rows, usernames; ids only).
* **C-1 (catalog check, static, no ansible):** every name and string in `usecases/*.yml` and `setups/*.yml` must be free
  of `{{`, `{%`, `{#` (command in OPERATOR-RUNBOOK.md §3.4). A hit means an admin-authored template expression that
  is either intentional (then record it as such) or a vulnerability.
* **M-vm-names:** a fake Xen Orchestra listing containing a VM name with `{{ lookup('pipe','touch <tmp>/c') }}`,
  run through `orphans.yml` destroy with real ansible-playbook; assert no canary. A DRAFT harness exists (`test_orphans_canary_DRAFT.py`, `M-VM-NAMES-HARNESS.md`), never executed and not part of
  the patches; row 25 stays OPEN: it is the one row where both "user-controlled" and "vulnerable" are UNKNOWN.
* **Full matrix (optional, not provided):** for each row marked "reaches a template = YES", run each real playbook
  with the variable set to a `lookup('pipe','touch ...')` payload at every string leaf and look for the canary file.
  Rows 1, 7, 13, 14, 16, 18-24 are NO/none by the static analysis above and need not be run unless the operator wants
  the matrix complete. Any hit on a row not already fixed is a stop condition and joins the P-0 fix set.

## Residual risks of the P-0 allowlists (static)

* A template name or key that is purely numeric or `true`/`null` may be type-coerced by ansible when templated
  (UNVERIFIED); it cannot execute code.
* Non-ASCII comments (accented names) are now refused: a usability cost, by decision.
* The playbooks themselves are unchanged: anything that runs `ansible-playbook` directly with unvalidated
  extravars is still exposed. The guard is the API, the CLI and the engine context.
