Install the pinned age v1.1.1 in CI, the controller and the DPU (X-AGE)

What
- deploy/age/install-age.sh <tarball> [prefix=/usr/local]: POSIX sh,
  set -eu, no network. It refuses a tarball for the other arch, copies
  the tarball into a private temp dir, runs verify-age.sh on that copy,
  unpacks the same copy, hashes both binaries against BINARIES.sha256
  again, and requires `--version` to print exactly "v1.1.1" before and
  after installing age and age-keygen with `install -m 0755`. On any
  mismatch it prints one stderr line, exits 1 and installs nothing.
- CI: a step before the tests downloads the amd64 tarball with
  curl --fail, runs install-age.sh with prefix $HOME/.local and adds
  $HOME/.local/bin to $GITHUB_PATH; the test step sets
  DAOLAB_REQUIRE_AGE=1.
- tests/conftest.py: the requires_age marker (registered with
  addinivalue_line). A marked test runs only when age and age-keygen
  (DAOLAB_AGE_BIN / DAOLAB_AGE_KEYGEN_BIN, else PATH) print exactly
  "v1.1.1"; otherwise it is skipped, or fails when DAOLAB_REQUIRE_AGE=1.
- service/config.py: age_bin and age_keygen_bin (paths, not secrets;
  the G-3 rule stays green); .env.example lists both.
- playbooks/tasks/install_age.yml, from bringup.yml with tags
  [age, never] (runs only with --tags age; the resolve import is tagged
  always so r_dpu is still filled). On the controller it verifies and
  unpacks a private copy of the arm64 tarball (extra var
  daolab_age_arm64_tarball, refused clearly if unset or misnamed) and
  checks both binaries against the linux-arm64 pins, so neither binary
  goes to the DPU unless both match. Each copy to /usr/local/bin (root,
  0755) has a validate: sha256 check against its pin, then the DPU
  re-hashes both and requires `--version` == v1.1.1.
- deploy/age/README.md: installing on the controller, the DPU (by hand)
  and in CI; .gitignore: .venv-220/.

Why
Every later Phase 8 task (keys, challenges, capture, restore) runs the
age CLI, and no install step existed anywhere, not even in CI (errata
E-1; decision Q-AGE). The real v1.1.1 release binaries print exactly
"v1.1.1\n" for --version (checked on the downloaded release), so every
check requires exactly that.

Files
.github/workflows/ci.yml, .gitignore, .env.example,
deploy/age/README.md, deploy/age/install-age.sh (new),
playbooks/bringup.yml, playbooks/tasks/install_age.yml (new),
service/config.py, tests/conftest.py, tests/test_age_install.py (new)

Verified
Python 3.12.13, SQLite 3.53.1, ansible-core 2.21.6 and 2.20.11:
- red: 20 failed, 31 errors on the base code (no install-age.sh, marker
  not registered, no settings, no task, no CI step; test_config refuses
  the new .env.example keys)
- green: 67 passed, 1 skipped (the real-age test)
- the real amd64 release installed by install-age.sh into a scratch
  prefix (0755, pinned hashes); the requires_age tests then pass with
  DAOLAB_REQUIRE_AGE=1
- probes: tags never/always and copy validate: on both ansible-core
  versions
- full suite: 1487 passed, 5 skipped on both (base 1437 passed,
  4 skipped; the 5th skip is the requires_age test, which runs in CI)
No service code path changes: flag-off parity holds.

Series result
After this, the pinned age v1.1.1 is installed in CI, and any later
requires_age test fails CI if age is missing. Controller and DPU
installs are manual (deploy/age/README.md).

Patch
X-AGE.patch sha256
7bfb3996dc61fcde213cecbf6cedf0f76e9b3c6f3470dad8be5ae924eab38eb3
Evidence: branch akshay-p8-gate, plan/design/p8-impl/x-age-akshay/
