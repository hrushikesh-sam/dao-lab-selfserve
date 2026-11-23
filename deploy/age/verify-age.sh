#!/bin/sh
# verify-age.sh <tarball> - check an age v1.1.1 release tarball (B-103, errata E-1).
#
# The tarball must be named in SHA256SUMS. It is copied into a temporary directory,
# and that copy must match its hash there; then age/age and age/age-keygen are
# extracted from the same copy and checked against
# BINARIES.sha256 for the tarball's arch. Both pin files are read from this script's
# directory. Exit 0 on a full match; on anything else, one line on stderr and exit 1.
#
# POSIX sh (runs under dash, and on the DPU). No network access and no install step:
# installing the verified binaries is a later task.
set -u

die() {
    printf 'verify-age: %s\n' "$*" >&2
    exit 1
}

[ "$#" -eq 1 ] || die "usage: verify-age.sh <tarball>"
tarball=$1

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || die "cannot find the script directory"
sums=$here/SHA256SUMS
bins=$here/BINARIES.sha256
[ -f "$sums" ] || die "missing pin file: SHA256SUMS"
[ -f "$bins" ] || die "missing pin file: BINARIES.sha256"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found"
[ -f "$tarball" ] || die "not a file: $tarball"

# pin FILE NAME: the one 64-hex hash pinned for NAME in FILE, or nothing.
pin() {
    awk -v n="$2" '$2 == n { c++; h = $1 } END { if (c == 1) print h }' "$1"
}

hash_of() {
    sha256sum < "$1" | awk '{ print $1 }'
}

is_hash() {
    case $1 in *[!0-9a-f]*) return 1 ;; esac
    [ "${#1}" -eq 64 ]
}

name=$(basename -- "$tarball")
want=$(pin "$sums" "$name")
[ -n "$want" ] || die "unknown file name: $name (not pinned exactly once in SHA256SUMS)"
is_hash "$want" || die "malformed SHA256SUMS entry for $name"

# age-v1.1.1-linux-amd64.tar.gz -> linux-amd64
arch=${name#age-v1.1.1-}
arch=${arch%.tar.gz}
[ "$arch" != "$name" ] || die "cannot read the arch from: $name"

tmp=$(mktemp -d) || die "mktemp failed"
trap 'rm -rf "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

# Check and extract one private copy, so the file that is checked is the file that is
# extracted: a change to the original after this point cannot reach the binaries.
copy=$tmp/tarball
cp -- "$tarball" "$copy" 2>/dev/null || die "cannot copy: $name"
got=$(hash_of "$copy")
[ "$got" = "$want" ] || die "tarball sha256 mismatch: $name"

tar -xzf "$copy" -C "$tmp" age/age age/age-keygen 2>/dev/null \
    || die "cannot extract age/age and age/age-keygen from: $name"

for b in age age-keygen; do
    label=$arch/age/$b
    f=$tmp/age/$b
    want=$(pin "$bins" "$label")
    [ -n "$want" ] || die "no pin for $label in BINARIES.sha256"
    is_hash "$want" || die "malformed BINARIES.sha256 entry for $label"
    [ -f "$f" ] && [ ! -h "$f" ] || die "not a regular file in $name: age/$b"
    got=$(hash_of "$f")
    [ "$got" = "$want" ] || die "binary sha256 mismatch: $label"
done

printf 'verify-age: OK %s (tarball, age/age and age/age-keygen match the pins)\n' "$name"
