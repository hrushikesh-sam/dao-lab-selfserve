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

It makes no network access and installs nothing: installing the verified binaries is a
later task. `tests/test_age_pin.py` checks the pin files' format and runs the script
on synthetic tarballs; it never uses the network.
