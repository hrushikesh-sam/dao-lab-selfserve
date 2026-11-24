# X-AGE evidence — the pinned age v1.1.1 in CI, on the controller and the DPU

## What X-AGE is
Every later Phase 8 task (keys, challenges, capture, restore) runs the age CLI, and before
X-AGE it was installed nowhere, not even in CI (errata E-1; decision Q-AGE: Akshay, task X-AGE).
X-AGE adds:
- `deploy/age/install-age.sh <tarball> [prefix=/usr/local]`. POSIX sh, `set -eu`, no network. It:
  - refuses a tarball for the other arch;
  - copies the tarball into a private temp dir and runs `verify-age.sh` on that copy;
  - unpacks the same copy and re-hashes both binaries against `BINARIES.sha256`;
  - requires `--version` == exactly `v1.1.1` before and after installing with `install -m 0755`.
  On any mismatch it prints one stderr line, exits 1 and installs nothing.
- CI: a step before the tests downloads the amd64 tarball (`curl --fail`), runs install-age.sh into
  `$HOME/.local` and adds `$HOME/.local/bin` to `$GITHUB_PATH`. The test step sets `DAOLAB_REQUIRE_AGE=1`.
- `tests/conftest.py`: the `requires_age` marker, registered with `pytest_configure` + `addinivalue_line` (pyproject.toml
  unchanged). A marked test runs only if `age` and `age-keygen` (`DAOLAB_AGE_BIN` / `DAOLAB_AGE_KEYGEN_BIN`,
  else PATH) both print exactly `v1.1.1\n` with rc 0. Otherwise it is skipped with "age v1.1.1 not installed (see
  deploy/age/README.md)", or it **fails** when `DAOLAB_REQUIRE_AGE=1`.
- `service/config.py`: `age_bin` / `age_keygen_bin` (defaults `age` / `age-keygen`; paths, not secrets, so G-3 stays green).
  `.env.example` lists both; Akshay made that edit by hand.
- `playbooks/tasks/install_age.yml`, imported by `bringup.yml` with `tags: [age, never]`. The resolve import is tagged `always`.
  - Extra var `daolab_age_arm64_tarball` (refused clearly if unset, empty or misnamed).
  - On the controller: verify and unpack a private copy, then check that both binaries match the linux-arm64 pins (all or nothing).
  - On the DPU: `copy` to `/usr/local/bin` (root, 0755) with a `validate:` sha256 against each pin, then re-hash both and
    require `--version` == v1.1.1.
- `deploy/age/README.md`: how to install on the controller, the DPU (Akshay, by hand) and in CI. `.gitignore`: `.venv-220/`.

## Identity
| | |
|---|---|
| Base | `origin/rama` **`5641830`** (`564183062842a5f6426355701582c13f2f7afa92`, merge of PR #10) |
| Tested tree (index) | **`8639e1b40703b7277f34810964fdaeb1ee063976`** |
| Patch | `build/X-AGE.patch` = `git diff --cached --binary origin/rama`, sha256 **`7bfb3996dc61fcde213cecbf6cedf0f76e9b3c6f3470dad8be5ae924eab38eb3`** |
| Apply check | fresh `origin/rama` worktree: `git apply --index` rc 0, applied tree = tested tree (**TREE MATCH**), `git diff --cached --check` rc 0, 10 files, +1031 −3 (`build/apply-check.txt`) |
| Commit message draft | `msg/X-AGE.msg` (subject 71 chars, body ≤ 72, no local paths, no AI attribution) |
| Files changed | `.github/workflows/ci.yml`, `.gitignore`, `.env.example`, `deploy/age/README.md`, `deploy/age/install-age.sh` (new, 0755), `playbooks/bringup.yml`, `playbooks/tasks/install_age.yml` (new), `service/config.py`, `tests/conftest.py`, `tests/test_age_install.py` (new) |

## Stages
All stages ran with Python 3.12.13 and SQLite 3.53.1. Every log was made by `build/run-stage.sh`, except `real-age-install.txt` and `apply-check.txt`, whose commands are in their headers. Header fields: date, worktree, HEAD, `git write-tree`, status, venv, PATH (pinned to the venv), age on PATH, python, ansible-core, ansible-runner, pytest, sqlite, command, `# exit:`. Venvs: `build/constraints.venv{,-220}.txt`.

| Stage | `.venv` — ansible-core 2.21.6 | `.venv-220` — ansible-core 2.20.11 | Logs |
|---|---|---|---|
| **red**: new tests + `test_config.py` on base code (+ Akshay's `.env.example` lines) | 20 failed, 3 passed, 31 errors | 20 failed, 3 passed, 31 errors | `build/red-.venv.txt`, `build/red-.venv-220.txt` |
| **green**: `test_age_install`, `test_config`, `test_age_pin`, `test_secret_env_rule` | 67 passed, 1 skipped | 67 passed, 1 skipped | `build/green-.venv.txt`, `build/green-.venv-220.txt` |
| **real age**: the real amd64 release installed by install-age.sh into a scratch prefix (`build/real-age-install.txt`: rc 0, 755, pinned hashes), then `-k 'pinned_v1_1_1 or marker'` with `DAOLAB_REQUIRE_AGE=1` and `DAOLAB_AGE_BIN`/`DAOLAB_AGE_KEYGEN_BIN` pointing at it | 11 passed | 11 passed | `build/green-real-age-.venv.txt`, `build/green-real-age-.venv-220.txt` |
| **full suite** `tests/ -q -rsf -p no:cacheprovider` | **1487 passed, 5 skipped**, 1 warning, exit 0 | **1487 passed, 5 skipped**, 1 warning, exit 0 | `build/full-.venv.txt`, `build/full-.venv-220.txt` |

- **Baseline:** 1437 passed, 4 skipped at `5641830`. X-AGE adds 51 tests: 50 pass, and 1 skips locally.
  The skip is `test_the_age_on_path_is_the_pinned_v1_1_1` (`requires_age`; no age on this laptop). In CI it runs and must
  pass. The 4 standard skips are unchanged: playwright, and 3× sshd.
- **The 1 warning** is pre-existing: `StarletteDeprecationWarning` from `fastapi/testclient.py`. It isn't from X-AGE.
- **Every red is for the right reason** (the `E` lines in the red logs):
  - 32× `deploy/age/install-age.sh does not exist`.
  - 9× `'requires_age' not found in markers` (the inner runs use `--strict-markers`).
  - 2× `'Settings' object has no attribute 'age_bin'`.
  - `FileNotFoundError …/playbooks/tasks/install_age.yml`.
  - The resolve import has `tags` None.
  - No CI install step.
  - The README doesn't mention install-age.sh.
  - `.env.example lists keys with no Settings field: ['DAOLAB_AGE_BIN', 'DAOLAB_AGE_KEYGEN_BIN']`.
  - `.venv-220/bin/python is ignored only by .venv-220/.gitignore`.
  - The real-age test isn't skipped, because the marker doesn't exist yet.
  - The 3 passes are existing `test_config.py` tests.
- **Flag-off parity:** no service code path changes. The only service change is two new Settings fields with defaults, which nothing reads yet.
  The existing suites pass unchanged.
- **Canary rule:** not applicable. X-AGE handles no S, user identity or nonce, and the real-age test generates no key.

## Probes (`probe/`)
**Tags and `copy validate:`.** `probe/probe_tags.py`, output in `probe/probe_tags-out.txt`. It ran in a fresh `mktemp -d` with no deletion, on a copy of `playbooks/` with the draft bringup edits and a stub `install_age.yml`, using the playbook test harness `Lab` and its inventory (`dpus: dpu-01`). Results were identical on 2.21.6 and 2.20.11:
- **(a)** `--list-tasks` with no tags: the age play lists **no tasks**. A real run with no tags runs no age task. Its rc=2 is only because this
  laptop lacks /home/dao-test and the Marvell repo, which the existing bringup test allows.
- **(b)** `--list-tasks --tags age`: resolve's 9 tasks (`TAGS: [always]`) **and** both age tasks (`TAGS: [age, never]`) are listed.
  A real run with `--tags age` gives `add the DPU to r_dpu` ok, `ok: [dpu-01 -> localhost]` (controller step) and `ok: [dpu-01]` (DPU step),
  so **the DPU group is non-empty**; rc 0. Running with `ANSIBLE_RUN_TAGS=age`, as the tests do, gives the same result.
- **(c)** `copy` + `validate: sh -c 'echo "<pin>  $1" | sha256sum -c --status' sh %s`:
  - a bad source over an existing target: rc 2, **target sha256 unchanged**;
  - the good source: rc 0, the target is the source, mode 0755;
  - a bad source to a new path: rc 2, **not created**.

**The real age v1.1.1 output** (`probe/age-version-real.txt`):
- The tarball was fetched with `curl --fail --location` from the release URL. Its sha256 `cf16cbb1…` matches `SHA256SUMS`, and `verify-age.sh` reports OK.
- A `cmp`-identical copy was unpacked. `age/age` `0c6ddc31…` and `age/age-keygen` `e279f64c…` match `BINARIES.sha256`.
- `age --version`: rc 0, stdout **7 bytes** `v 1 . 1 . 1 \n` (`od -c`), stderr 0 bytes.
- `age-keygen --version`: rc 0, stdout **7 bytes** `v1.1.1\n`, stderr 0 bytes. `age-keygen --bogus` gives rc 2, so `--version` really is parsed.

The arm64 binaries were never run. The DPU task's `--version` check covers them on the board.

## Changes to tests during the build (stated openly)
1. **`test_gitignore_covers_both_venvs` (red stage, before any code):** it first passed vacuously, because `uv venv` writes a
   private `*` `.gitignore` inside each venv. It now requires the matching rule to come from the repo's own `.gitignore` (stricter).
2. **`test_marker_fails_without_age_when_required` (first green):** `pytest.fail` in `pytest_runtest_setup` is reported as a
   setup *error*, not a failure. The **code** was fixed: the required case now fails in `pytest_runtest_call`. The test is unchanged.
3. **`test_dpu_side_pin_check_refuses_a_bad_binary_even_if_verify_were_bypassed` (first green):** the copy loop went on
   to install `age-keygen` after `age` was refused, leaving a partial install. The **code** was fixed: the controller now hashes both unpacked
   binaries before any copy (all or nothing). The test's file assertions are unchanged (the old `age` is kept, and no `age-keygen` is installed). Its message
   needle changed from `failed validation` (a wrong guess at Ansible's text, which is `failed to validate`) to the new check's
   message. The `validate:` gate itself is tested by a new test, `test_the_dpu_copy_validate_refuses_a_binary_that_is_not_its_pin`, which runs
   the real copy task from `install_age.yml`.

**Overwritten logs:** the first red run (with the vacuous gitignore test) and the first green run (with the 2 failures above)
were overwritten when the stages were re-run. The logs here are the re-runs on the final code. No log was edited by hand.
`build/real-age-install.txt` replaces the scratch prefix path with `<prefix>`, done by its own generating command. Raw logs
contain local `/home/akshay/...` paths (accepted for raw logs).

## Follow-ups
- `deploy/age/verify-age.sh:10-11` still says "installing the verified binaries is a later task". The file wasn't on X-AGE's list,
  so this is a one-line comment fix for a later change.
- Controller install (`sudo sh deploy/age/install-age.sh …`) and DPU install (`bringup.yml --tags age -e
  daolab_age_arm64_tarball=…`): Akshay does both by hand (`deploy/age/README.md`). Nothing here touched the DPU.
- CI's first run proves the real install path on GitHub's runners, on both Pythons.

## File index
| File | What |
|---|---|
| `README.md` | this file |
| `.gitattributes` | `* -text` (byte-exact evidence) |
| `SHA256SUMS` | sha256 of every other file (`sha256sum -c SHA256SUMS`) |
| `FINDINGS.md` | Stage 0 discovery (§1–7) and Akshay's decisions + the probe results (§8) |
| `msg/X-AGE.msg` | commit message draft |
| `build/X-AGE.patch` | the patch (sha256 above) |
| `build/apply-check.txt` | fresh-worktree apply, TREE MATCH, `--check` |
| `build/red-.venv.txt`, `build/red-.venv-220.txt` | red stage |
| `build/green-.venv.txt`, `build/green-.venv-220.txt` | green stage |
| `build/real-age-install.txt` | install-age.sh on the real amd64 release |
| `build/green-real-age-.venv.txt`, `build/green-real-age-.venv-220.txt` | requires_age tests against the real age, `DAOLAB_REQUIRE_AGE=1` |
| `build/full-.venv.txt`, `build/full-.venv-220.txt` | full suite |
| `build/run-stage.sh` | the stage runner that wrote the stage logs |
| `build/constraints.venv.txt`, `build/constraints.venv-220.txt` | the two venvs' pinned packages |
| `probe/probe_tags.py`, `probe/probe_tags-out.txt` | tags (a)/(b) and `copy validate:` (c) probe |
| `probe/age-version-real.txt` | the real release: hashes, verify-age.sh, `--version` bytes |
