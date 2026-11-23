# Rama PR stack: 8 patches on `origin/rama` (40ebd6d)

- **Prepared:** 2026-11-23, in a detached worktree of `origin/rama` (the "tested tree" below). Nothing committed, pushed or opened as a PR.
- **Base:** `origin/rama` = `40ebd6dcc8877632e0270155cf5dfa797d1012dc`, re-checked after `git fetch origin`.
- **Apply:** in the order below, each with `git apply --check` first.
- **Each patch holds only its own change:** G-1..G-3 were diffed index tree to index tree (`trees.txt`), so new files are included.
- **One commit per patch:** each patch has its message file, `msg/NN-<name>.txt`.

## Evidence in this folder

Branch `akshay-p8-gate`, `plan/design/p8-gate-review/round-3-akshay/rama-pr/`:

| File | What |
|---|---|
| `stack-apply.txt` | the 5 gate patches on the tested tree: sha256, `--check` and apply |
| `venvs.txt` | `.venv`: Python 3.12.13, ansible-core 2.21.6. `.venv-220`: ansible-core 2.20.11. ansible-runner 2.4.3 in both |
| `G-1-red.txt` / `G-1-green.txt` | G-1: 1 failed / 1 passed, then 2 passed, on both venvs |
| `G-2-red.txt` / `G-2-green.txt` | G-2: 4 failed / 4 passed, then 8 passed (31 with `test_catalog.py`), on both |
| `G-3-red.txt` / `G-3-green.txt` | G-3: the rule-bites probe (no code change was needed), then 3 + 4 passed, on both |
| `full-221.txt` / `full-220.txt` | full suite, all 8 patches: **1290 passed, 4 skipped, 0 failed** on each. `full-220.txt` ends with a note about its header line. |
| `apply-check.txt` | fresh `origin/rama` worktree: all 8 `--check` ok; `diff -r -q -x __pycache__` against the tested tree exit 0; worktree removed |
| `trees.txt` | the index trees: base (5 patches), after G-1, after G-2, after G-3 |

Evidence for patches 1–5:
- P-0-final and F-5-1: branch `rama-p0-artifacts`, `plan/design/p8-gate-review/evidence/`.
- F-5-1b: branch `akshay-p8-gate`, `plan/design/p8-gate-review/round-2-akshay/evidence/`.
- P-0c-combined: branch `akshay-p8-gate`, `plan/design/p8-gate-review/round-3-akshay/p0c-combined/` (from `p0c-combined.zip`, sha256 `c0875b8f…`).
- P-0d: branch `akshay-p8-gate`, `plan/design/p8-gate-review/round-3-akshay/p0d/`.

Each message names its files.

## Series

### 1. `P-0-final.patch`

- **sha256:** `cc3b42cf7c601bd6c414e46847f3ef78c84bf1d3e65a895ce582dd8cf51c6409`
- **Subject:** Keep user text out of ansible templating: keys, names, DIY (P-0)
- **Message:** `msg/01-P-0-final.txt`
- **Files (5, 5 files changed, 375 insertions(+), 18 deletions(-)):**
  - `service/api.py` (+11/-8)
  - `service/cli.py` (+21/-3)
  - `service/models.py` (+71/-6)
  - `tests/test_deploy_access.py` (+6/-1)
  - `tests/test_pubkey_strict.py` (+266/-0)

### 2. `F-5-1.patch`

- **sha256:** `0c06d643c4d371655eac4280a00b8f440106f378d729cb4625d8efee75d69f0a`
- **Subject:** Pass the internal secret to playbooks in a 0600 file (F-5-1)
- **Message:** `msg/02-F-5-1.txt`
- **Files (5, 5 files changed, 208 insertions(+), 3 deletions(-)):**
  - `playbooks/deploy.yml` (+2/-1)
  - `playbooks/group_vars/all.yml` (+6/-0)
  - `service/runner.py` (+60/-1)
  - `tests/test_ansible_runner.py` (+4/-1)
  - `tests/test_runner_secret.py` (+136/-0)

### 3. `F-5-1b.patch`

- **sha256:** `823a54bcdefd8ae52a6c3226144b93978d25583e5d37adfbc079dd78471c0f5d`
- **Subject:** Scrub the secrets from os.environ after loading settings (F-5-1b)
- **Message:** `msg/03-F-5-1b.txt`
- **Files (2, 2 files changed, 77 insertions(+)):**
  - `service/config.py` (+14/-0)
  - `tests/test_secret_env_scrub.py` (+63/-0)

### 4. `P-0c-combined.patch`

- **sha256:** `58a25f53353bbeabaafed9dff61786220cadb86c51f8302d52df612947079376`
- **Subject:** Accept only strictly shaped VM names in the orphan sweep (P-0c)
- **Message:** `msg/04-P-0c-combined.txt`
- **Files (6, 6 files changed, 320 insertions(+), 2 deletions(-)):**
  - `service/engine.py` (+3/-0)
  - `service/models.py` (+19/-0)
  - `service/runner.py` (+14/-2)
  - `tests/test_orphans_canary.py` (+68/-0)
  - `tests/test_vm_names_review.py` (+124/-0)
  - `tests/test_vm_names_strict.py` (+92/-0)

### 5. `P-0d.patch`

- **sha256:** `83264b3a71666d0585983d0513de096284905681036608105d0a395ef2e6f99a`
- **Subject:** Destroy orphan and reconcile VMs by XO id, not by name (P-0d)
- **Message:** `msg/05-P-0d.txt`
- **Files (13, 13 files changed, 600 insertions(+), 154 deletions(-)):**
  - `playbooks/group_vars/all.yml` (+5/-0)
  - `playbooks/orphans.yml` (+28/-12)
  - `playbooks/reconcile.yml` (+11/-8)
  - `service/engine.py` (+15/-10)
  - `service/models.py` (+72/-3)
  - `service/runner.py` (+71/-31)
  - `tests/test_ansible_runner.py` (+22/-9)
  - `tests/test_orphans.py` (+3/-2)
  - `tests/test_orphans_canary.py` (+39/-13)
  - `tests/test_playbooks.py` (+53/-15)
  - `tests/test_vm_ids.py` (+178/-0)
  - `tests/test_vm_names_review.py` (+66/-27)
  - `tests/test_vm_names_strict.py` (+37/-24)

### 6. `G-1-ansible-floor.patch`

- **sha256:** `040e403eebbdb1e64bd67716791dae4fc666d4b783f07ef3947ac27ab4593508`
- **Subject:** Raise the ansible-core floor to 2.20 (G-1)
- **Message:** `msg/06-G-1-ansible-floor.txt`
- **Files (2, 2 files changed, 45 insertions(+), 3 deletions(-)):**
  - `pyproject.toml` (+5/-3)
  - `tests/test_platform_floor.py` (+40/-0)

### 7. `G-2-catalog-template-guard.patch`

- **sha256:** `d5de53e6aeb235ece12a2d0a691350a8f04e3876abfda09378239b6f6112160b`
- **Subject:** Reject template delimiters in usecase verify.cmd (G-2, C-1)
- **Message:** `msg/07-G-2-catalog-template-guard.txt`
- **Files (2, 2 files changed, 93 insertions(+)):**
  - `service/catalog.py` (+10/-0)
  - `tests/test_catalog_template_guard.py` (+83/-0)

### 8. `G-3-secret-env-rule.patch`

- **sha256:** `3b9cf9f52c655cedfaa300d07272fb2ffbb8f495338377b25417fe0d63bcec7a`
- **Subject:** Require every secret-like setting to be in SECRET_ENV (G-3)
- **Message:** `msg/08-G-3-secret-env-rule.txt`
- **Files (1, 1 file changed, 65 insertions(+)):**
  - `tests/test_secret_env_rule.py` (+65/-0)

