# Extravar matrix: X-ML addendum (ml-tvm on rama `8bfcf4b`)

Same legend as `plan/design/p8-gate-review/EXTRAVAR-MATRIX.md` (branch `akshay-p8-gate`).

**Runner extravars: no new rows.** `AnsibleRunner.extravars()` is unchanged by the port; pinned by
`tests/test_ml_tvm.py::test_no_new_ml_var_is_a_runner_extravar` (no ml-tvm input name appears in
`AnsibleRunner`'s source). Existing rows that ml-tvm uses: 2 `usecase`, 10 `daolab_roles`,
11 `daolab_verify`, 17 `daolab_app` (catalog YAML, C-1/G-2), 22 `daolab_deploy_vms`,
23 `allow_destructive_changes` (`| bool` in an `assert`), 24 `daolab_output_file`.

**New template inputs (not extravars):**

| input | source | user-controlled | reaches a template | vulnerable | test |
|---|---|---|---|---|---|
| `bdf.ml`, `ml_pci` (= `{{ bdf.ml }}`) | `inventory/host_vars/dpu-01.yml` | NO (admin) | YES (deploy, cleanup, verify_clean, probe) | NO | `test_the_inventory_names_the_ml_device_role`, `test_ml_pci_resolves_to_the_ml_role` |
| `ml_fw`, `ml_lcores`, `ml_file_prefix`, `ml_hugepage_kb`, `ml_hugepages_extra`, `ml_smoke_dir`, `ml_smoke_model`, `ml_state_dir` | host_vars | NO (admin) | YES | NO | — |
| `daolab_procfs`, `daolab_vfio_dir`, `daolab_dpdk_run_dir`, `ml_smoke_bin`, `ml_env_file`, `ml_test_mldev` (new); `daolab_sysfs`, `daolab_hugetlbfs` (existing) | unset in the inventory: inline `default()` = the board's path; tests only | NO | YES | NO | `test_the_ml_host_path_defaults_are_todays_values`, `test_no_ml_host_path_is_a_literal_outside_a_default` |
| `_ml_dev` (verify_clean fact) | derived from `ml_pci` / `bdf.ml` | NO | YES | NO | `test_verify_clean_skips_…`, `test_without_an_ml_address_only_the_holder_check_is_skipped` |
| `ml-tvm` `verify.cmd` | `usecases/ml-tvm.yml` | NO (catalog) | YES (`shell: "{{ _cmd }}"`) | NO: no `{{ {% {#` | G-2 `test_no_template_delimiter_anywhere_in_usecases_or_setups` (scans `ml-tvm.yml`), `test_the_catalog_loads_ml_tvm` |

**P-0:** no user-supplied value reaches a new template; ml-tvm adds no user-facing field (the API
request models are unchanged). **F-5-1:** no ml-tvm file references a secret
(`test_no_secret_reaches_the_ml_tvm_files`); `/etc/default/daolab-ml` holds only board values and
paths (`test_the_env_file_holds_nothing_secret_like`). **No canary added:** no user value reaches
a new template (Akshay's decision 4, 24 Nov).
