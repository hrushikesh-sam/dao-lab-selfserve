#!/bin/sh
# install-age.sh <tarball> [prefix] - install the pinned age v1.1.1 (X-AGE, errata E-1).
#
# Installs age and age-keygen from an official v1.1.1 release tarball into <prefix>/bin
# (default /usr/local; run it as root for that). The tarball must be for this host's arch
# (linux-amd64 on x86_64, linux-arm64 on aarch64).
#
# The tarball is copied into a private temporary directory. verify-age.sh checks that copy
# against SHA256SUMS and BINARIES.sha256 (both beside this script), and the binaries are
# extracted from the same copy and hashed again here. So the file that is checked is the file
# that is installed. Each binary's `--version` must print exactly "v1.1.1" (one line, rc 0)
# before it is installed with `install -m 0755`, and again from <prefix>/bin afterwards.
# On any mismatch, one line on stderr and exit 1; a refused binary is never installed.
#
# POSIX sh (runs under dash). No network access: download the tarball yourself
# (deploy/age/README.md).
set -eu

die() {
    printf 'install-age: %s\n' "$*" >&2
    exit 1
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "usage: install-age.sh <tarball> [prefix, default /usr/local]"
tarball=$1
prefix=${2:-/usr/local}
case $prefix in
    /*) ;;
    *) die "the prefix must be an absolute path: $prefix" ;;
esac

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || die "cannot find the script directory"
verify=$here/verify-age.sh
bins=$here/BINARIES.sha256
[ -f "$verify" ] || die "missing: verify-age.sh"
[ -f "$bins" ] || die "missing pin file: BINARIES.sha256"
[ -f "$tarball" ] || die "not a file: $tarball"

machine=$(uname -m)
case $machine in
    x86_64) host=linux-amd64 ;;
    aarch64) host=linux-arm64 ;;
    *) die "no pinned age release for this machine: $machine" ;;
esac

name=$(basename -- "$tarball")
case $name in
    age-v1.1.1-linux-amd64.tar.gz) arch=linux-amd64 ;;
    age-v1.1.1-linux-arm64.tar.gz) arch=linux-arm64 ;;
    *) die "unknown file name: $name" ;;
esac
[ "$arch" = "$host" ] || die "wrong arch: the tarball is $arch, this host is $host"

# pin FILE NAME: the one 64-hex hash pinned for NAME in FILE, or nothing (as verify-age.sh).
pin() {
    awk -v n="$2" '$2 == n { c++; h = $1 } END { if (c == 1) print h }' "$1"
}

# is_v111 BINARY: BINARY --version exits 0 and prints exactly "v1.1.1" and one newline.
# (The trailing x keeps the command substitution from dropping extra newlines.)
is_v111() {
    out=$("$1" --version 2>/dev/null && echo x) || return 1
    [ "$out" = "v1.1.1
x" ]
}

tmp=$(mktemp -d) || die "mktemp failed"
trap 'rm -rf "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

copy=$tmp/$name
cp -- "$tarball" "$copy" 2>/dev/null || die "cannot copy: $name"
sh "$verify" "$copy" >/dev/null || die "verify-age.sh refused $name"
mkdir "$tmp/x" || die "mkdir failed"
tar -xzf "$copy" -C "$tmp/x" age/age age/age-keygen 2>/dev/null \
    || die "cannot extract age/age and age/age-keygen from: $name"

for b in age age-keygen; do
    f=$tmp/x/age/$b
    [ -f "$f" ] && [ ! -h "$f" ] || die "not a regular file in $name: age/$b"
    want=$(pin "$bins" "$arch/age/$b")
    got=$(sha256sum < "$f" | awk '{ print $1 }')
    [ -n "$want" ] && [ "$got" = "$want" ] || die "binary sha256 mismatch: $arch/age/$b"
    chmod 0755 "$f" || die "chmod failed: age/$b"
    is_v111 "$f" || die "age/$b --version is not exactly v1.1.1"
done

# Create <prefix>/bin only when missing: `install -d -m` would also change an existing dir's mode.
[ -d "$prefix/bin" ] || install -d -m 0755 "$prefix/bin" || die "cannot create $prefix/bin"
for b in age age-keygen; do
    install -m 0755 "$tmp/x/age/$b" "$prefix/bin/$b" || die "cannot install $prefix/bin/$b"
done
for b in age age-keygen; do
    is_v111 "$prefix/bin/$b" || die "installed $prefix/bin/$b --version is not exactly v1.1.1"
done

printf 'install-age: OK age v1.1.1 (%s): %s/bin/age, %s/bin/age-keygen (mode 0755)\n' \
    "$arch" "$prefix" "$prefix"
