# age v1.1.1 pin (B-103, errata E-1)

The snapshot work uses age from the **official GitHub v1.1.1 release assets** only,
pinned by sha256 for linux-amd64 (the controller) and linux-arm64 (the DPU).
**No apt, no pyrage.** (Ubuntu 22.04's apt has no v1.1.1.)

| File | What it pins |
|---|---|
| `SHA256SUMS` | the 2 release tarballs, in `sha256sum -c` format |
| `BINARIES.sha256` | `age/age` and `age/age-keygen` inside each tarball, labelled by arch (`linux-amd64/age/age`, …) |
| `verify-age.sh` | checks a tarball against both files |

## Source

- https://github.com/FiloSottile/age/releases/download/v1.1.1/age-v1.1.1-linux-amd64.tar.gz
- https://github.com/FiloSottile/age/releases/download/v1.1.1/age-v1.1.1-linux-arm64.tar.gz

Fetched 2026-11-23T11:20:50Z. The v1.1.1 release publishes no checksum or provenance
file (its assets are the 7 archives only), so these hashes were computed locally, as
below, and are not copied from anywhere.

## How the hashes were computed

1. Each tarball was downloaded twice, with `curl -fsSL`, into two separate new
   `mktemp -d` directories. `cmp` found the two copies identical.
2. `sha256sum` of each tarball gives `SHA256SUMS`. `sha256sum -c SHA256SUMS` passes
   in both download directories.
3. `tar -tvzf` lists `age/`, `age/age`, `age/age-keygen` and `age/LICENSE` in each
   tarball. Each tarball was extracted into its own empty `mktemp -d` directory, and
   `sha256sum age/age age/age-keygen` gives `BINARIES.sha256`. `file` reports x86-64
   and ARM aarch64 static ELF binaries. The arm64 binaries were never run.

**Independent cross-check: done 2026-11-23.** A separate download of both tarballs
from a different machine and network (Claude's cloud workspace) gave the same 6
hashes. The GitHub API for the release lists 7 assets with no checksum, signature or
proof file, and every asset's digest field is empty. Both copies come from GitHub, so
this guards against a corrupted or tampered download, not against the release itself;
no second publisher of v1.1.1 hashes exists.

## Verifying a tarball

    sh deploy/age/verify-age.sh age-v1.1.1-linux-arm64.tar.gz

The script is POSIX sh (it runs under dash, and on the DPU). It needs `sha256sum`,
`tar`, `awk`, `cp` and `mktemp`. It looks up the tarball's file name in `SHA256SUMS`,
copies the tarball into a temporary directory and checks the copy's hash, then
extracts `age/age` and `age/age-keygen` from that same copy and checks them against
`BINARIES.sha256`, so the file that is checked is the file that is extracted. It prints `verify-age: OK …` and
exits 0 on a full match. On an unknown file name, any mismatch or a missing binary, it
prints one line to stderr and exits 1.

It makes no network access and installs nothing; `install-age.sh` (below) installs.
`tests/test_age_pin.py` checks the pin files' format and runs the script on synthetic
tarballs; it never uses the network.

## Installing (X-AGE)

The real v1.1.1 binaries print exactly `v1.1.1` and a newline for `age --version` and
`age-keygen --version` (rc 0, nothing on stderr). Every check below requires exactly that.

### On the controller (linux-amd64)

Download both tarballs by hand into a new empty directory. You need the arm64 one later for
the DPU. Keep the release file names: the scripts look them up in `SHA256SUMS`.

    d=$(mktemp -d)
    for a in amd64 arm64; do
      curl --fail --location --silent --show-error -o "$d/age-v1.1.1-linux-$a.tar.gz" \
        "https://github.com/FiloSottile/age/releases/download/v1.1.1/age-v1.1.1-linux-$a.tar.gz"
    done
    sh deploy/age/verify-age.sh "$d/age-v1.1.1-linux-arm64.tar.gz"    # checked now, used for the DPU
    sudo sh deploy/age/install-age.sh "$d/age-v1.1.1-linux-amd64.tar.gz"   # prefix /usr/local
    age --version; age-keygen --version                                 # both: v1.1.1

`install-age.sh <tarball> [prefix]` (default prefix `/usr/local`; POSIX sh, `set -eu`, no
network) does the following:
- It refuses a tarball for the other arch (`uname -m`).
- It copies the tarball into a private temp dir and runs `verify-age.sh` on that copy.
- It unpacks the same copy and hashes both binaries again against `BINARIES.sha256`.
- It runs each binary's `--version`, which must print exactly `v1.1.1`.
- Only then does it install both with `install -m 0755` into `<prefix>/bin`, and run
  `<prefix>/bin/age --version` (and age-keygen's) again.

On any mismatch it prints one `install-age: …` line on stderr, exits 1 and installs
nothing. A user install without sudo works the same way with a prefix such as
`"$HOME/.local"`. In that case, put `$HOME/.local/bin` on `PATH`, or point the settings at the
binaries:
`DAOLAB_AGE_BIN` and `DAOLAB_AGE_KEYGEN_BIN` (defaults `age`, `age-keygen`; paths, not secrets).

### On the DPU (linux-arm64) — Akshay, by hand

Nothing is downloaded on the DPU, and the controller can't run arm64 binaries, so
`playbooks/tasks/install_age.yml` does the following:
- The controller verifies and unpacks a private copy of the arm64 tarball.
- It copies only the two binaries to the DPU's `/usr/local/bin` (owner root, mode 0755).
  Each one replaces the old file only if its sha256 is the `linux-arm64` pin.
- The DPU then checks both installed files against the pins again and runs `--version`.

It is part of `bringup.yml`, but tagged `[age, never]`: it runs only with `--tags age`. With
that tag, nothing else in bringup runs, apart from resolving the setup.

    ansible-playbook -i inventory/hosts.yml playbooks/bringup.yml -e setup=tb-01 --tags age \
      -e daolab_age_arm64_tarball="$d/age-v1.1.1-linux-arm64.tar.gz"

Without `daolab_age_arm64_tarball` (or with another file name) it stops before doing anything.
Check by hand on the board afterwards:

    stat -c '%U %G %a %n' /usr/local/bin/age /usr/local/bin/age-keygen   # root root 755
    sha256sum /usr/local/bin/age /usr/local/bin/age-keygen               # the linux-arm64 lines of BINARIES.sha256
    age --version; age-keygen --version                                 # v1.1.1

### In CI and in tests

`.github/workflows/ci.yml` downloads the amd64 tarball with `curl --fail`, runs
`install-age.sh` with the prefix `$HOME/.local`, and adds `$HOME/.local/bin` to `$GITHUB_PATH`.
It runs the tests with `DAOLAB_REQUIRE_AGE=1`.

Tests that run the real age carry `@pytest.mark.requires_age` (`tests/conftest.py`). They run
only when `age` and `age-keygen` (`DAOLAB_AGE_BIN` / `DAOLAB_AGE_KEYGEN_BIN`, else `PATH`) both
print exactly `v1.1.1`. Otherwise they are skipped with "age v1.1.1 not installed (see
deploy/age/README.md)". With `DAOLAB_REQUIRE_AGE=1` they fail instead, so CI can't pass with a
missing age. `tests/test_age_install.py` tests the scripts, the marker and the DPU task with
synthetic tarballs and fake binaries (no network).
