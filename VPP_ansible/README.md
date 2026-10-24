# VPP CN10K Ansible Automation

This project automates the command sequence in:

`vpp-cn10k-clean-install-runbook.md`

The runbook is the sole source for the commands and values used here.

## Important

The runbook says this is a shared board and that destructive operations require owner/sign-off.

The playbooks therefore stop before destructive operations unless:

```yaml
allow_destructive_changes: true
```

is explicitly set in `group_vars_all.yml`.

Do not enable that variable until you have the required sign-off.

## Project layout

```text
vpp_cn10k_ansible/
├── ansible.cfg
├── inventory.ini
├── group_vars_all.yml
├── site.yml
├── README.md
└── playbooks/
    ├── 01_baseline.yml
    ├── 02_purge.yml
    ├── 03_install.yml
    ├── 04_configure_02.yml
    ├── 05_test_02.yml
    ├── 06_kernel_crosscheck.yml
    ├── 07_restore_02.yml
    ├── 08_prepare_04.yml
    ├── 09_configure_04.yml
    └── 10_verify_04.yml
```

## First run

From this directory:

```bash
ansible dpu -m ping
```

Then inspect the baseline without changing the board:

```bash
ansible-playbook playbooks/01_baseline.yml
```

The complete workflow is:

```bash
ansible-playbook site.yml
```

The destructive sections will stop unless `allow_destructive_changes` is enabled.

## VPP foreground process

The runbook starts VPP in the foreground in terminal 2. The Ansible implementation starts the same command asynchronously so Ansible can continue to the documented `vppctl` checks. The VPP command itself and its configuration are unchanged.
