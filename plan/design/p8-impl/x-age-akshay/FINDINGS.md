# X-AGE — Stage 0 findings (read-only discovery)

- Date: 2026-11-24
- Base: `origin/rama` = **`5641830`** (Merge pull request #10, p8-b104b). Unchanged since B-104 PR B, so the
  baseline stays **1437 passed, 4 skipped**. I'll re-measure it at the red stage.
- Worktree: `~/x-age`, branch `x-age` (`--no-track` from `origin/rama`). Clean, nothing edited.
- Task source: `p8-task-breakdown.md` §2.1, §5.1, §6 Q-AGE, §8 "X-AGE" (the prompt matches that text).
- Listed files: `.github/workflows/ci.yml`, `deploy/age/install-age.sh` (new), `deploy/age/README.md`,
  `playbooks/tasks/install_age.yml` (new), `playbooks/bringup.yml`, `service/config.py`, `tests/conftest.py`,
  `tests/test_age_install.py` (new), `pyproject.toml` (marker only), `.gitignore`.

**Summary:** most of the prompt fits the code. **Two points must be decided before I write code:**
**B-1** (a test forces a change to a file outside the list, and the deny rules block that file) and
**B-2** (as written, the `age` tag either runs on every bringup or never reaches the DPU).
Q-1…Q-8 are smaller design choices; each has a proposed answer.

---

## 1. Facts at `5641830`

### 1.1 `deploy/age/`
| Fact | Where |
|---|---|
| `SHA256SUMS`: 2 lines, `age-v1.1.1-linux-amd64.tar.gz` `cf16cbb1…`, `-linux-arm64.tar.gz` `f0dbf436…` | `SHA256SUMS:1-2` |
| `BINARIES.sha256`: 4 lines labelled `linux-{amd64,arm64}/age/{age,age-keygen}`; arm64 `age` = `ed2e7ea4…`, `age-keygen` = `5376e684…` | `BINARIES.sha256:1-4` |
| `verify-age.sh` uses `set -u`, **not `-e`**. It looks up the tarball's **basename** in SHA256SUMS, so a renamed download is refused | `verify-age.sh:12,44-47` |
| It takes the arch from the name (`age-v1.1.1-<arch>.tar.gz`), not from the host. It never compares against `uname -m` | `:49-52` |
| It copies to a `mktemp -d` dir, hashes the copy, extracts `age/age` and `age/age-keygen` there, checks them, and **removes the dir on EXIT**. The verified binaries are never available to a caller | `:54-77` |
| Its comments say "no install step: installing the verified binaries is a later task" (this goes stale after X-AGE; the file isn't on the list, so I'll leave it) | `:10-11` |
| README says the same ("installing … is a later task") | `README.md:52-53` |
| `tests/test_age_pin.py` forbids `curl wget apt pip sudo install bash [[` in **verify-age.sh**'s code (comments stripped), and runs `sh -n` under dash | `test_age_pin.py:57-66` |
| The synthetic-tarball pattern: build gz tarballs with `tarfile`, write **synthetic pins** beside a **copy** of the script in `tmp_path`, run under `dash` with `PATH` + `LC_ALL=C` (+ `TMPDIR`) | `test_age_pin.py:75-119` |

### 1.2 CI — `.github/workflows/ci.yml`
- There's one job, `test`: ubuntu-latest, matrix 3.12/3.13, `fail-fast: false`, timeout 40 (`:15-24`).
- Steps: checkout, setup-python (pip cache), Install (`pip install -e ".[ansible]"`), and Test (`python -m pytest tests/ -q -rf`) (`:26-37`).
- There's no age and no `env:` block. The runner is x86_64, so the CI tarball is linux-amd64.

### 1.3 pytest config, markers, conftest
- **There's no pytest config anywhere:** no `[tool.pytest.ini_options]` in `pyproject.toml`, and no `pytest.ini`, `setup.cfg` or `tox.ini`. There are no custom markers today, and no `--strict-markers`.
- `tests/test_platform_floor.py:26-58` reads `pyproject.toml` with tomllib, but only `project.requires-python` and `optional-dependencies.ansible`. Adding a `[tool.pytest.ini_options]` table won't affect it.
- `tests/conftest.py`: there are no hooks (`pytest_configure` / `collection_modifyitems` / `runtest_setup`). It turns `dotenv.load_dotenv` into a no-op before importing `service` (`:26`). That matters: the marker must not read the developer's env file.
- Existing skip style is `pytest.mark.skipif(..., reason=...)` (e.g. `test_playbooks.py:27`).

### 1.4 `service/config.py`
- `Settings` is a frozen dataclass with 43 fields, read from `from_env` (`:11-109`). `SECRET_ENV` is at `:117`.
- **G-3** (`tests/test_secret_env_rule.py:15-16`) flags field names matching `secret|token|password|passphrase`. `age_bin` and `age_keygen_bin` don't match, so G-3 stays green.
- **`tests/test_config.py:91-95` `test_env_example_lists_every_setting`** requires every Settings field's
  `DAOLAB_*` name in the repo-root example env file (`ENV_EXAMPLE`, `test_config.py:77-78`), and `:98-101`
  refuses keys there with no field. **That file isn't on X-AGE's list. See B-1.**

### 1.5 `playbooks/bringup.yml` and its harness
- Play 1 (localhost) `import_tasks: tasks/resolve.yml`, **untagged** (`bringup.yml:9-14`). resolve.yml creates
  the transient group `r_dpu` with `add_host` (`tasks/resolve.yml:63-68`).
- Play 2 `hosts: r_dpu`, `_root: daolab_bringup_root | default('')` (`bringup.yml:16-20`). Tasks use
  `become: true` (`:22-24,41-42`).
- The final report play asserts nothing is missing (`:89-104`).
- Inventory: group `dpus` → `dpu-01` (`ansible_user: dao-test`, `ansible_become: true`) (`inventory/hosts.yml:25-30`).
  `ansible.cfg:24` has `pipelining = False`, so module files (and `copy` sources) land in the remote tmp first.
  These are public binaries, so that's fine.
- Harness: `tests/test_playbooks.py` `Lab` (`:56`). All hosts are `ansible_connection: local`. `Lab.run(playbook, roles_path=None, **extravars)` (`:96-121`) passes **no CLI arguments**, but its env is the process env plus overrides (`:106-111`). So a test can set `ANSIBLE_RUN_TAGS=age` with `monkeypatch.setenv` and needs no harness change.
- The bringup tests pass `daolab_bringup_root=<tmp>/root, ansible_become=False` (`test_playbooks.py:1240-1264`). The `ansible_become` extra var overrides the tasks' `become: true` keyword.

### 1.6 `.gitignore`, Makefile, tools
- `.gitignore:153` is `.venv`, which matches only the name `.venv`. `.venv-220/lib/` is ignored only by the
  generic `lib/` at `.gitignore:17`. Confirmed in `~/b104b`: `git status --ignored` shows `?? .venv-220/`
  and `!! .venv-220/lib/`. Adding `.venv-220/` fixes it.
- `Makefile:1-2` is `-include .env` + `export`; `make test` = `python -m pytest tests/ -q` (`:9-10`). In a fresh
  worktree the env file shouldn't exist (it's untracked), but per lesson 18.7 Akshay checks that in his own terminal
  before `make test`, and `PATH` is pinned to the venv.
- Local: `dash` = `/usr/bin/dash`; `install` = `/usr/bin/install` (GNU coreutils); `/usr/bin/python3.10` exists;
  **age isn't installed** (expected); the machine is x86_64.
- Venvs to copy: `~/b104b` has Python 3.12.13 / SQLite 3.53.1, `.venv` ansible-core 2.21.6, `.venv-220` 2.20.11. The pins are in
  `~/notes/b104b/build/constraints.venv{,-220}.txt` (51 lines; they differ only in ansible-core), and the log header script is
  `~/notes/b104b/build/run-stage.sh` (date, worktree, HEAD, `git write-tree`, status, venv, python, ansible-core,
  runner, pytest, sqlite, command, `# exit:`). I'll reuse both, retargeted to `~/x-age`.
- `git grep` finds no `install-age`, `requires_age`, `AGE_BIN` or `DAOLAB_REQUIRE_AGE` anywhere yet.

---

## 2. Blocking points (need a decision before code)

### B-1. Adding the two Settings fields breaks `test_config.py` unless the example env file changes
`test_env_example_lists_every_setting` (`tests/test_config.py:91-95`) will fail with
`missing settings: ['DAOLAB_AGE_BIN', 'DAOLAB_AGE_KEYGEN_BIN']`. Changing the test expectation is not allowed (rule 6). So the
repo-root example env file must gain two lines, but **it isn't on X-AGE's file list** (AGENTS.md: stop and ask).
Also, **the Claude Code deny rule `*.env*` matches its name**, so I can't edit, stage-check or even name it in a
command. I didn't touch it; the second denial below was this same rule, as a false positive on `os.environ`.

Options:
- **(a) Recommended:** add the file to the list. Akshay adds the two lines by hand in his terminal (non-secret
  defaults, no values from the real env file):
  ```
  DAOLAB_AGE_BIN=age
  DAOLAB_AGE_KEYGEN_BIN=age-keygen
  ```
  (placed next to the existing XO entries, with a one-line comment). Then I stage it with the rest using `git add -A`,
  which doesn't name the file. The patch and tree checks cover it like any other file.
- (b) Leave the Settings fields out of X-AGE and let B-105 (Rama, first user of `age_bin`) add them. This is cleaner for
  the file list, but it changes contract §5.1 ("Added by X-AGE"), so both owners must agree.

### B-2. "Behind a tag `age` so it runs only when asked" needs `never`, and resolve needs `always`
- A plain `tags: [age]` task **still runs on every normal bringup** (no `--tags` means run everything). "Only when asked" needs
  Ansible's special tag: `tags: [age, never]`.
- With `--tags age`, every untagged task is skipped, **including play 1's `import_tasks: tasks/resolve.yml`**.
  Then `r_dpu` stays empty and the age task silently runs on no host. So the resolve import in bringup.yml
  needs `tags: [always]` (in-file change; bringup.yml is listed). The extra cost is that `--tags <anything>` always
  runs resolve, which is localhost-only and read-only.
- With `--tags age`, the report play's assert is skipped too, so the run's exit code comes from the age tasks alone.
- **Not verified yet:** my sandbox probe of exactly these three behaviours (never/always/`ANSIBLE_RUN_TAGS`, plus `copy validate`) on both
  ansible-core versions was **denied** (the command contained `rm -rf`). I didn't retry it in another form. The build stage
  tests will prove it anyway. If you want proof before then, approve a re-run of the probe in the scratchpad (a fresh
  `mktemp -d`, with no deletion).

---

## 3. Design questions (proposed answers; I'll build with these unless told otherwise)

**Q-1. "Install from the SAME verified extracted copy" vs verify-age.sh deleting its extraction.**
verify-age.sh can't hand its extracted binaries to a caller (`:54-56`).
**Proposed:** don't change verify-age.sh (not listed, and its no-`install` test stays as is). In install-age.sh:
1. `mktemp -d` (0700, own trap).
2. `cp` the tarball into it, keeping the **basename** (verify-age.sh looks the name up).
3. `sh verify-age.sh <that copy>`.
4. `tar -xzf` the **same copy** into the same private dir (only `age/age`, `age/age-keygen`).
5. Re-hash both extracted binaries against `BINARIES.sha256` itself (they must be regular files, not symlinks).
6. Install.

So the bytes installed are the bytes verified. A change to the original after step 2 can't reach them.

**Q-2. Wrong arch, and when to check the version (install-age.sh).**
**Proposed order:** map `uname -m` (`x86_64`→linux-amd64, `aarch64`→linux-arm64, else refuse). A tarball whose name has the
other arch is refused **before** anything is extracted or installed ("wrong arch: tarball is linux-arm64, host is
linux-amd64"). Then run `<extracted>/age --version`, which must equal `v1.1.1` **before** installing, so a wrong-version binary never lands in
`<prefix>/bin`. Then `install -d -m 0755 <prefix>/bin` (CI's `$HOME/.local/bin` may not exist),
`install -m 0755` both binaries, and finally `<prefix>/bin/age --version` must equal `v1.1.1` (as the prompt asks).
Every error is one stderr line `install-age: …` with exit 1. `set -eu`. No network; no `sudo` (the caller runs it as root
for `/usr/local`).
Not atomic: if `install` is interrupted, one binary can be new and the other old. That's acceptable here; a re-run repairs it.

**Q-3. `age --version` printing exactly `v1.1.1` is UNVERIFIED for the real release binaries.**
Nobody here has run them (the README says the arm64 binaries were never run, and amd64 isn't installed). If the real output differs
(e.g. `v1.1.1` plus extra text), install-age.sh fails closed, which is safe but blocks CI. **Proposed:** require exactly
`v1.1.1` as specified. The first CI run (and Akshay's controller install) proves it. If it fails, we change the
expectation openly with evidence and don't loosen it silently.

**Q-4. The DPU task's shape (`playbooks/tasks/install_age.yml`).** The controller is amd64, so it can't run the
arm64 `age --version`; the version can only be checked on the DPU.
**Proposed (A, as the prompt describes):**
- On localhost, `run_once`:
  - assert `daolab_age_arm64_tarball` is set and non-empty: `fail_msg: "set -e daolab_age_arm64_tarball=<path to age-v1.1.1-linux-arm64.tar.gz>"`.
  - assert the basename is the arm64 name.
  - copy it into a private temp dir, run `verify-age.sh` on the copy, extract both binaries from that copy, and remove the
    temp dir in an `always:` block.
- On the DPU (become):
  - `ansible.builtin.copy` each binary to `{{ _age_root }}/usr/local/bin/` with `mode: "0755"`, owner/group root, and
    **`validate:`** = a `sha256sum -c` of the temp file against the BINARIES.sha256 linux-arm64 pin. A wrong
    binary then never replaces `/usr/local/bin/age`.
  - after install: `sha256sum` both installed files again against the pins (`changed_when: false`).
  - `age --version` must equal `v1.1.1`.

  The pins are read on the controller with `lookup('file', …)` and parsed with a regex. That's repo content, not user text, so
  P-0 re-templating doesn't apply.

Alternative (B): copy the tarball, the pins and both scripts to a DPU temp dir and run `install-age.sh … /usr/local` there.
One code path, but it puts the tarball and scripts on the DPU and makes the DPU's own tools the verifier.
I recommend A.

**Q-5. Owner root in tests.** With `ansible_become=False` as a normal user, `owner: root` makes `copy`'s chown
fail. **Proposed:** a prefix var `daolab_age_root` (default `''`, like `daolab_bringup_root`). Owner and group are `root` when
it's empty and `omit` otherwise. The tests then can't prove the ownership, so a static test asserts that the task file sets
`owner: root` on the real path, and Akshay checks `stat -c '%U %a' /usr/local/bin/age*` on the board.

**Q-6. What the `requires_age` marker checks.**
- The prompt says skip "when age is not on PATH (or DAOLAB_AGE_BIN)". **Proposed:** resolve `DAOLAB_AGE_BIN` (default
  `age`) and `DAOLAB_AGE_KEYGEN_BIN` (default `age-keygen`) with `shutil.which`, and require **both** to exist and
  `age --version` == `v1.1.1` (one subprocess per session, cached). Otherwise skip with exactly `age v1.1.1 not installed (see deploy/age/README.md)`, or call `pytest.fail` with the same text when `DAOLAB_REQUIRE_AGE=1`.
  Why: an apt or other age would otherwise run the later tests against the wrong version.
- It reads the process environment directly, not `config.settings`. The marker is test infrastructure, and `DAOLAB_REQUIRE_AGE` is **not** a Settings field (so it stays out of the example env file and `test_config` stays happy).
- It's implemented with `pytest_configure` (fallback registration) plus `pytest_runtest_setup`.

**Q-7. Where to register the marker.** No pytest config exists, so adding `[tool.pytest.ini_options]` to `pyproject.toml`
makes it pytest's configfile. The `rootdir` stays the repo, and the header line `configfile: pyproject.toml` appears.
`testpaths`, `addopts` and strict markers stay unset. **Proposed:** do that (the prompt says so), with only `markers = [...]`.
Alternative: `config.addinivalue_line` in conftest only, which needs no pyproject change. Tell me if you'd rather keep
pytest config-less.

**Q-8. A real `requires_age` test in this task.** The acceptance says "a later task's requires_age test proves it". That would leave
the CI step unproven until B-105.
- **Proposed:** add one `@pytest.mark.requires_age` test to `tests/test_age_install.py`: `age --version` == `v1.1.1` and
  `age-keygen` runs (`age-keygen --version` only if v1.1.1 has it; I'll check in CI, else just check that it's executable). It
  generates **no** identity.
- With `DAOLAB_REQUIRE_AGE=1` in CI, it fails the build if the install step didn't work.
- Locally it skips, so the **local baseline becomes N passed, 5 skipped** (4 + this one) unless age is installed.

**Q-9. Python 3.10.** Nothing in X-AGE runs Python on the DPU. install-age.sh and verify-age.sh are POSIX sh, tested under dash. The
test module imports `tests.conftest` → `service`, and `requires-python >=3.12` blocks a 3.10 install.
**Proposed:** no 3.10 run for X-AGE. Instead, a dash `-n` syntax check plus all script tests run under dash (the DPU's `/bin/sh`).
B-109a adds the `dpu-scripts` 3.10 job.

---

## 4. Planned changes (after sign-off)

| File | Change |
|---|---|
| `deploy/age/install-age.sh` (new, 0755) | Q-1/Q-2. Usage: `install-age.sh <tarball> [prefix=/usr/local]` |
| `.github/workflows/ci.yml` | Step "Install age v1.1.1" after Install, before Test: `d=$(mktemp -d "$RUNNER_TEMP/age.XXXXXX")`; `curl --fail --location --silent --show-error -o "$d/age-v1.1.1-linux-amd64.tar.gz" <URL>` (`-L` is needed: GitHub redirects release assets); `sh deploy/age/install-age.sh "$d/…" "$HOME/.local"`; `echo "$HOME/.local/bin" >> "$GITHUB_PATH"`. Test step gets `env: DAOLAB_REQUIRE_AGE: "1"` |
| `tests/conftest.py` | `requires_age` marker logic (Q-6) |
| `pyproject.toml` | `[tool.pytest.ini_options] markers = ["requires_age: needs the pinned age v1.1.1 CLI (deploy/age)"]` (Q-7) |
| `service/config.py` | `age_bin=env("DAOLAB_AGE_BIN", "age")`, `age_keygen_bin=env("DAOLAB_AGE_KEYGEN_BIN", "age-keygen")` (B-1) |
| example env file | 2 lines (only if B-1 (a)) |
| `playbooks/tasks/install_age.yml` (new) | Q-4/Q-5 |
| `playbooks/bringup.yml` | resolve import `tags: [always]`; a new play `hosts: r_dpu` importing install_age.yml with `tags: [age, never]`; header usage line `--tags age -e daolab_age_arm64_tarball=…` (B-2) |
| `deploy/age/README.md` | "Installing": the controller (download both tarballs by hand with `curl -fL`, `sudo sh install-age.sh … /usr/local`, check `age --version`); the DPU (Akshay: `ansible-playbook … bringup.yml -e setup=tb-01 --tags age -e daolab_age_arm64_tarball=…`, then the `stat`/`sha256sum`/`--version` checks); CI; the `requires_age` marker. Replace the "later task" sentence at `:52-53` |
| `.gitignore` | `.venv-220/` |
| `tests/test_age_install.py` (new) | below |

**Tests (red first).** On unchanged code they fail for the right reasons: no script, no marker, no task file, no fields.
- install-age.sh (synthetic tarballs and pins, copied scripts, under dash), on both arches relative to `uname -m`:
  - accepts a matching tarball: both files present, mode exactly 0755 even under `umask 077`, `<prefix>/bin/age --version` output checked, temp dir empty afterwards
  - rejects a tampered tarball (nothing installed)
  - rejects the wrong arch (nothing installed, message names both arches)
  - rejects a pinned binary that prints `v1.2.0` (nothing installed)
  - rejects a pinned binary that prints `v1.1.1-extra` (exactness)
  - wrong usage
  - creates a missing `<prefix>/bin`
  - static checks: `#!/bin/sh`, `set -eu`, no curl/wget/apt/pip/sudo/bash/`[[`, `dash -n` clean
- Marker (via `pytester`, or a subprocess pytest on a tmp test file with a monkeypatched PATH/env):
  - no age → skipped with the exact reason
  - no age plus `DAOLAB_REQUIRE_AGE=1` → failed with the reason
  - a fake `v1.1.1` age plus age-keygen on PATH → runs
  - a fake `v1.0.0` → skipped
  - `DAOLAB_AGE_BIN` pointing at the fake → runs
- Config: defaults `age` / `age-keygen`, and the env overrides work.
- DPU task (Lab harness, `ANSIBLE_RUN_TAGS=age`, `ansible_become=False`, `daolab_age_root=<tmp>`, synthetic arm64 tarball, and pins injected through a pin-dir var such as `daolab_age_pin_dir` so tests never edit `deploy/age/`):
  - refuses a missing or empty tarball var (fixed message, nothing installed)
  - refuses a tarball with a bad hash (verify-age.sh fails, nothing on the DPU)
  - refuses a binary whose hash isn't the arm64 pin (validate fails, nothing installed)
  - installs both at 0755 and the version check passes (the fake binaries are sh scripts, so they run on x86_64)
  - a plain bringup run (no tags) doesn't run the age tasks (the `never` proof)
  - static: owner root on the real path
- Requires real age: Q-8.

**Not applicable / unchanged:**
- Rule 8 (canaries): X-AGE handles no S, identity or nonce, and the Q-8 test makes no keys.
- Rule 7 (flag-off parity): no service code path changes. The only change is two new Settings fields with defaults, which nothing reads yet. The full suite on both venvs, at the baseline count plus the new tests, is the proof.

---

## 5. Risks
- R-1: `age --version` format (Q-3).
- R-2: tags (B-2), unprobed until the build or an approved probe.
- R-3: CI network. GitHub release downloads can fail transiently. `curl --fail` makes that a clear red. I won't add retries
  unless you want `--retry 3`.
- R-4: if ubuntu-latest ever ships an `age` in `/usr/bin`, `$GITHUB_PATH` prepends `$HOME/.local/bin`, so ours wins, and the
  marker's version check (Q-6) catches the case anyway.
- R-5: `copy validate:` runs its command on the DPU as root, on a temp file. The pin hash is interpolated into it from repo
  content and checked against `^[0-9a-f]{64}$` first.
- R-6: the deny rule `*.env*` also matches `os.environ` in commands. My Bash commands will avoid that text, and test
  code is written with the Write/Edit tools.

## 6. Denials during Stage 0 (reported, not worked around)
1. The scratchpad probe of the tags and `copy validate` (it contained `rm -rf` on the scratchpad dir). Not retried.
2. A `grep` for the `Lab.run` env line in `tests/test_playbooks.py`: the pattern contained `os.environ` (deny rule `*.env*`).
   I read the same non-secret test file with the Read tool instead (`:96-121`). Nothing env-file-related was read.

## 7. What I need from you
1. **B-1:** (a) add the example env file to the list, and Akshay adds the 2 lines by hand; or (b) move the Settings fields to B-105.
2. **B-2:** OK to tag resolve `always` and the age play `[age, never]`? And should I re-run the probe now (no `rm -rf`)?
3. Q-1…Q-9: OK as proposed? In particular Q-4 (A vs B), Q-7 (pyproject vs conftest only) and Q-8 (a real requires_age test now, giving 5 local skips).

---

## 8. Decisions (Akshay, 2026-11-24) and probe results

### 8.1 Decisions
- **B-1:** `.env.example` is **added to the file list**. Akshay added `DAOLAB_AGE_BIN=age` and
  `DAOLAB_AGE_KEYGEN_BIN=age-keygen` himself. Claude Code doesn't touch it (`git status` in `~/x-age` shows
  ` M .env.example` before any build edit).
- **B-2:** approved as proposed: the age play `tags: [age, never]`, the resolve import `tags: [always]`, both in bringup.yml.
  The probe (8.2) confirms it.
- **Q-7 changed:** register `requires_age` in `tests/conftest.py` only (`pytest_configure` +
  `config.addinivalue_line`). **`pyproject.toml` is off the file list.**
- **Q-3 resolved by the real output (8.3):** the check is that stdout is exactly `v1.1.1\n` with rc 0.
- Everything else in §3 is approved as proposed (Q-1, Q-2, Q-4 option A with `copy validate:`, Q-5, Q-6, Q-8, Q-9).

Final file list: `.github/workflows/ci.yml`, `deploy/age/install-age.sh` (new), `deploy/age/README.md`,
`playbooks/tasks/install_age.yml` (new), `playbooks/bringup.yml`, `service/config.py`, `tests/conftest.py`,
`tests/test_age_install.py` (new), `.gitignore`, `.env.example` (Akshay's edit only).

### 8.2 Probe: tags and `copy validate:` (`probe/probe_tags.py`, `probe/probe_tags-out.txt`)
Fresh `mktemp -d` scratch dir, no deletion. It used a copy of `playbooks/` with the draft B-2 edits, a stub
`install_age.yml` (one `delegate_to: localhost, run_once` task, one DPU task), and the harness `Lab`
(`tests/test_playbooks.py:56`) for the fake lab and its inventory (`dpus: dpu-01`). Results were **identical on ansible-core 2.21.6 and 2.20.11:**

| Check | Result |
|---|---|
| (a) `--list-tasks`, no tags | play #4 "age … (only with --tags age)" lists **no tasks**; resolve's tasks are listed `TAGS: [always]` |
| (a2) a real run, no tags | the age stub tasks **don't appear**. rc=2 because this machine lacks /home/dao-test and the Marvell repo, which the existing bringup test allows (`test_playbooks.py:1260-1264`) |
| (b1) `--list-tasks --tags age` | resolve's 9 tasks (`always`) **and** both age stub tasks `TAGS: [age, never]`; the other plays list nothing |
| (b2) a real run, `--tags age` | resolve runs, `add the DPU to r_dpu` → `ok`; the age play runs `ok: [dpu-01 -> localhost]` (controller step) and `ok: [dpu-01]` (DPU step), so **r_dpu is non-empty**; rc=0 |
| (b3) `ANSIBLE_RUN_TAGS=age` (how the tests will drive `Lab.run`) | same as (b2): the DPU step ran on dpu-01, rc=0 |
| (c1) `copy` with `validate: sh -c 'echo "<pin>  $1" \| sha256sum -c --status' sh %s`, a bad source over an existing target | rc=2 (`failed validation`); target sha256 **unchanged** |
| (c2) the same with the good source | rc=0; target == source; mode 0755 |
| (c3) a bad source to a new path | rc=2; **target not created** |

Side note: under `-c local` the discovered interpreter came from the main clone's `.venv` on PATH. The build pins
`PATH` to `~/x-age/<venv>/bin` first, so each run uses its own venv.

### 8.3 The real age v1.1.1 output (`probe/age-version-real.txt`)
- `curl --fail --location` from the release URL gave a 4465769-byte tarball. Its sha256 `cf16cbb1…` matches `SHA256SUMS:1`.
- `deploy/age/verify-age.sh` → `verify-age: OK …`, rc=0.
- A `cmp`-identical copy was extracted. `age/age` `0c6ddc31…` and `age/age-keygen` `e279f64c…` match `BINARIES.sha256:1-2`.
- `age --version`: rc=0, stdout **7 bytes `v 1 . 1 . 1 \n`**, stderr 0 bytes.
- `age-keygen --version`: rc=0, stdout **7 bytes `v1.1.1\n`**, stderr 0 bytes. (An unknown flag gives rc=2, so `--version` really
  is parsed, not ignored.)
- So: install-age.sh and the marker require **rc 0 and stdout exactly `v1.1.1\n`** for both binaries.
  install-age.sh compares `$(bin --version; echo x)` against `v1.1.1` + newline + `x`, so trailing text or extra lines fail.
  The DPU task checks the same with `stdout == 'v1.1.1'` (Ansible strips the one trailing newline) **and** `stdout_lines | length == 1`.
- Still not run: the arm64 binaries (they only run on the DPU; the DPU task's version check covers them).
