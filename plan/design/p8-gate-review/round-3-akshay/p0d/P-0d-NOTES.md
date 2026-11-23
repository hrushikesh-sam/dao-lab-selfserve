# P-0d: the orphan sweep destroys by Xen Orchestra VM id (EXTRAVAR-MATRIX row 25 hardening)

- **Date:** 2026-11-23
- **Worktree:** `~/p8gate-p0d`
- **Base:** `40ebd6d` + P-0-final (`cc3b42cf`) → F-5-1 (`0c06d643`) → F-5-1b (`823a54bc`) → P-0c-combined (`58a25f53`)
- **Patch:** `P-0d.patch`, sha256 `83264b3a71666d0585983d0513de096284905681036608105d0a395ef2e6f99a`, 13 files. The same file is at `/tmp/P-0d.patch` and `~/notes/P-0d.patch`.
- **Process:** no commit, push or PR. `~/p8gate` (Part B's tree) was not touched.

## What changed

**In simple terms:**
- The sweep used to hand ansible VM *names*, which anyone with pool access can choose, and ansible evaluates `{{ … }}` inside them.
- Now only XO's own VM ids (UUIDs) travel, in a file that ansible reads as data and never templates. No playbook reads a VM name or a VM list from its caller.

**Technical:**
- **`service/models.py`:**
  - `VM_ID` (lowercase xapi UUID, fullmatch) and `vm_id_ok`;
  - `TaggedVm(id, name)`;
  - `split_vm_ids` / `split_vm_ids_detail`;
  - `classify_tagged_vms` / `split_tagged_vms`.
  - Counts are **invalid entries only**. Valid repeats are de-duplicated silently (two hosts of one pool list the same VM).
- **`service/runner.py`:**
  - `tagged_vms() -> list[TaggedVm]`; `destroy_vms(ids)`.
  - The ids go in a 0600 `daolab-vmids-*.json` file in the F-5-1 runtime dir, passed only as `daolab_vm_ids_file`. It is removed in the same `finally` as the secret file, and stale ones are swept by `_sweep_secret_files`.
  - MockRunner keeps names and derives ids with `mock_vm_id(name)` (uuid5).
- **`service/engine.py`:** `_sweep_orphans` reads the reservation id from the strict name inside Python and hands the runner the ids only. The audit row gains `vm_ids`.
- **`playbooks/orphans.yml`:**
  - list mode writes `{id, name}` pairs;
  - destroy mode reads `daolab_vm_ids_file` with `lookup('file') | from_json`, filters through `daolab_vm_id_re`, and destroys only ids listed right now;
  - `daolab_vm_names` is gone.
- **`playbooks/reconcile.yml`:** the caller-supplied VM-name override is deleted. It destroys only `daolab-r<reservation_id>-*` from XO's own listing, and only ids that match `daolab_vm_id_re`.
- **`playbooks/group_vars/all.yml`:** `daolab_vm_id_re`, kept in sync with `models.VM_ID` by a test.

## Log and message wording

- **Rama's P-0c log lines are unchanged, word for word.** They now count bad *names*:
  - `orphan sweep: dropped %d listed VM name(s) of an unexpected shape` (`tagged_vms`)
  - `orphan sweep: refused %d VM name(s) of an unexpected shape` (`destroy_vms`)
  - `orphan sweep: skipped %d VM name(s) of an unexpected shape` (engine)
- **New lines for ids, next to his:** `dropped|refused|skipped %d … VM id(s) of an unexpected shape`.
- **Deliberate wording change:** when every id is refused, `destroy_vms`'s `RunResult.error` now reads `"N VM id(s) refused, nothing to destroy"`. P-0c's said `"N VM name(s) refused, nothing to destroy"`.
  - It changed because `destroy_vms` now takes ids.
  - It is an error string, not a log line. No test asserts its wording; tests only check that it carries no payload.

## Findings while testing

- **Before P-0d, reconcile.yml split an id from its own XO listing.** It passed a listed id of `{{ 7 * 7 }}` to `daolab-xo destroy --vm` as five arguments. Ansible didn't template it, but the command module split it. The `daolab_vm_id_re` filter now skips such an id (`test_reconcile_skips_a_listed_vm_whose_id_is_not_a_uuid`).
- **The row 25 canary also fires on ansible-core 2.20.11** before P-0d, the same as on 2.21.6.
- **The `[reconcile.yml]` case of the new id canary passes before the code change.** That is expected: pre-P-0d reconcile never read ids. It is kept as a guard, not a red test.

## Evidence (`~/notes`)

| File | What | Result |
|---|---|---|
| `p0d-red-221.txt` | gate set, before the code, 2.21.6 | 2 collection errors (`mock_vm_id` missing); rerun past them: 51 failed / 36 passed; secret tests 0 failed |
| `p0d-red-canary-221.txt` / `-220.txt` | canary + playbook set, before the code | 17 failed / 20 passed on both, the same failing set |
| `p0d-green-221.txt` | gate set, after the code, 2.21.6 | 145 passed, 0 failed, 0 xfailed |
| `p0d-green-canary-221.txt` / `-220.txt` | canary + playbook set, after the code | 37 passed, 0 failed, 0 xfailed on both |
| `p0d-full-221.txt` | full suite, 2.21.6 | 1277 passed, 4 skipped (playwright, 3 × sshd), 0 failed, 0 xfailed, in 14:57. Before P-0d: 1233 passed + 2 xfailed. |
| `p0d-apply-check.txt` | the 4 patches + P-0d on a fresh `origin/rama` worktree | all `--check` ok; tree identical to `~/p8gate-p0d` |

**Environment:** Python 3.12.13. `.venv` has ansible-core 2.21.6; `.venv-220` has ansible-core 2.20.11.

### Full suite

- **Result:** **1277 passed, 4 skipped, 0 failed, 0 xfailed** in 14:57.
  - Python 3.12.13 / ansible-core 2.21.6, 2026-11-22T22:21:46Z → 22:36:44Z UTC.
  - The 4 skips are the usual ones: playwright isn't installed (1), and 3 tests need sshd, which isn't installed either.
  - Before P-0d, the same stack gave 1233 passed, 4 skipped and 2 xfailed. The 2 former xfails now pass with their marks removed.
- **Evidence:** `p0d-full-221.txt`.

### Red errors

- **Gate set (`p0d-red-221.txt`), first run:** 2 collection errors. `tests/test_orphans.py` and `tests/test_vm_names_strict.py` import `mock_vm_id` from `service.runner`, which didn't exist yet. That is a genuine red result, but pytest stops at collection errors.
- **Rerun in the same file**, with `--continue-on-collection-errors`, so every other test shows its own reason:
  - 51 failed / 36 passed, plus those 2 errors;
  - `test_runner_secret` and `test_secret_env_scrub`: 0 failed.
- **Canary and playbook set (`p0d-red-canary-221.txt` / `-220.txt`):** 17 failed / 20 passed on both 2.21.6 and 2.20.11, with the identical failing set. Each failure is for its stated reason (`AttributeError` for the new models/runner names, or the assertion on the behaviour still missing).
- **Timing note on `p0d-red-canary-221.txt`:**
  - The run started at about 03:32:58 IST and ended at 03:34:47 IST. The first P-0d code edit (`service/models.py`) was at 03:34:13 IST, after pytest had imported `service.*` at collection, and no playbook had been edited.
  - Its failing set equals the 2.20 run's. The note is recorded at the end of that file.

### Apply check rerun (`p0d-apply-check.txt`)

- **First check (22:22Z):** the only `diff` line was `Only in ~/p8gate-p0d/playbooks/files: __pycache__`. That is bytecode the test runs leave behind, not content, so the exit was 1.
- **Rerun (2026-11-22T22:37:44Z):** a new `origin/rama` worktree (HEAD `40ebd6d…`); P-0-final, F-5-1, F-5-1b, P-0c-combined and P-0d (`83264b3a…`) all passed `--check` and applied. Then `diff -r -q -x __pycache__` against `~/p8gate-p0d`:

  | Folder | Exit |
  |---|---|
  | `service` | 0 |
  | `tests` | 0 |
  | `playbooks` | 0 |
  | **Overall** | **0** |

- **Cleanup:** the check worktree held only the 7 files the patches create. It was removed afterwards (`git worktree remove --force`).

## Open (for Rama)

- `pyproject.toml` still says `ansible-core>=2.18`, but the design floor is 2.20. 2.18 and 2.19 are untested with P-0d. The earlier Py3.11 evidence used 2.19.15.
- **Before shipping, check that real XO ids have the strict shape** (read-only, Akshay, XO running). Use runbook §4.5's loop, printing `bad_ids`. Expect 0 per pool.
- **Spec text naming `daolab_vm_names`** (outside this tree; not edited):
  - `rama-p0-artifacts` / `akshay-p8-gate`, under `plan/design/p8-gate-review/`: `EXTRAVAR-MATRIX.md` (row 25), `README.md`, `p8-snapshot-design-v1.1.md`, `OPERATOR-RUNBOOK.md`, `P-0C-OPERATOR-ADDENDUM.md`, `M-VM-NAMES-HARNESS.md`, `all-phases-summary.md`, `evidence/STATUS.md`, `test_orphans_canary_DRAFT.py`.
  - `round-2-akshay/P-0c-NOTES.md`.
  - `plan/` and `docs/` in this tree: none.
