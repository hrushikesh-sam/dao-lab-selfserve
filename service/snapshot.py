"""Per-user workspace snapshots — the crypto and file format (B-103).

A snapshot is sealed at the end of a slot by the cleanup job, when no user is present and no
bearer token is available (the server stores only sha256(token)). It must open only for the
owner. Hence two layers:

  * a USER KEY, one per user: an X25519 keypair. The public half is stored in the clear and
    is all the cleanup job needs to seal. The private half is stored encrypted (AES-256-GCM)
    under a key derived from the user's bearer token (HKDF-SHA256 with a random salt; tokens
    are 256-bit random, so no slow password hash is needed). Only a request that carries the
    token can unwrap it.
  * a DATA KEY (DEK), one per snapshot: 32 random bytes. The archive is encrypted with it,
    and it is stored wrapped to the user's public key (ephemeral X25519 + HKDF + AES-GCM).
    A restore job is handed this one DEK, never the user's private key, so a leaked job row
    opens one snapshot at most.

Sealed file format (version 1), everything big-endian:

    header   b"DAOSNAP" | version (1 byte) | nonce prefix (7 bytes) | chunk size (4 bytes)
    chunks   AES-256-GCM(DEK, nonce = prefix | counter (4 bytes) | last (1 byte),
                         aad = header | context)          -- each chunk_size bytes of plaintext
                                                             (the last may be shorter or empty)

The `last` flag in the nonce makes truncation at a chunk boundary detectable (the STREAM
construction); the header in the AAD binds the chunk size and prefix; `context` (for example
"user:3|snapshot:17") binds the file to one snapshot row, so a sealed file or a wrapped DEK
cannot be presented as another snapshot's.

`python -m service.snapshot seal|open` is the command-line face the playbooks use. The DEK is
never passed on the command line (visible in `ps`): `open` reads it from an environment
variable or a file.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"DAOSNAP"
VERSION = 1
CHUNK_SIZE = 64 * 1024
TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + 1 + 7 + 4               # 19 bytes
DEK_SIZE = 32

_INFO_USER_KEY = b"daolab-snapshot-v1 user-key"
_INFO_DEK_WRAP = b"daolab-snapshot-v1 dek-wrap"
_AAD_USER_KEY = b"daolab-snapshot-v1 private-key"


class SnapshotError(Exception):
    """A snapshot could not be sealed or opened."""


class SnapshotKeyError(SnapshotError):
    """Wrong token, wrong key, wrong context, or tampered key material. Deliberately vague:
    the caller must not learn which."""


class SnapshotFormatError(SnapshotError):
    """The sealed file is not a snapshot, is truncated, or has been modified."""


@dataclass(frozen=True)
class UserKey:
    """What `user_keys` stores for one user (all base64)."""
    public_key: str
    wrapped_private_key: str
    kdf_salt: str


@dataclass(frozen=True)
class SealResult:
    """What a seal reports back for the `snapshots` row."""
    wrapped_dek: str
    sha256: str          # of the sealed file, hex
    size_bytes: int      # of the sealed file
    plain_bytes: int     # of the archive that went in


# ---- small helpers -----------------------------------------------------------------------

def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as e:
        raise SnapshotKeyError(f"{what} is not valid base64") from e


def _hkdf(secret: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(secret)


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_private(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _token_kek(token: str, salt: bytes) -> bytes:
    if not token:
        raise SnapshotKeyError("a token is required")
    return _hkdf(token.encode("utf-8"), salt, _INFO_USER_KEY)


def _context(context: str) -> bytes:
    return context.encode("utf-8")


# ---- the user key ------------------------------------------------------------------------

def _wrap_private(private: X25519PrivateKey, token: str, salt: bytes) -> str:
    nonce = os.urandom(12)
    sealed = AESGCM(_token_kek(token, salt)).encrypt(nonce, _raw_private(private), _AAD_USER_KEY)
    return _b64e(nonce + sealed)


def new_user_key(token: str) -> UserKey:
    """A fresh keypair whose private half only `token` can unwrap. Call it while handling a
    request that carries the token (`POST /reservations`, B-105)."""
    private = X25519PrivateKey.generate()
    salt = os.urandom(16)
    return UserKey(public_key=_b64e(_raw_public(private.public_key())),
                   wrapped_private_key=_wrap_private(private, token, salt),
                   kdf_salt=_b64e(salt))


def unwrap_private_key(token: str, key: UserKey) -> X25519PrivateKey:
    """The user's private key, or SnapshotKeyError (wrong token or tampered row)."""
    blob = _b64d(key.wrapped_private_key, "wrapped_private_key")
    salt = _b64d(key.kdf_salt, "kdf_salt")
    if len(blob) < 12 + 32 + TAG_SIZE:
        raise SnapshotKeyError("wrapped_private_key is too short")
    try:
        raw = AESGCM(_token_kek(token, salt)).decrypt(blob[:12], blob[12:], _AAD_USER_KEY)
    except InvalidTag as e:
        raise SnapshotKeyError("cannot unwrap the user key") from e
    private = X25519PrivateKey.from_private_bytes(raw)
    if _b64e(_raw_public(private.public_key())) != key.public_key:
        raise SnapshotKeyError("the user key does not match its public half")
    return private


def rewrap_user_key(old_token: str, new_token: str, key: UserKey) -> UserKey:
    """Token renewal (B-105): the same keypair, re-wrapped under the new token with a new
    salt. Every existing snapshot stays readable. Needs the old token: an admin who issues a
    token without it cannot do this, and the user's snapshots are lost (by design)."""
    private = unwrap_private_key(old_token, key)
    salt = os.urandom(16)
    return UserKey(public_key=key.public_key,
                   wrapped_private_key=_wrap_private(private, new_token, salt),
                   kdf_salt=_b64e(salt))


# ---- the data key ------------------------------------------------------------------------

def new_dek() -> bytes:
    return os.urandom(DEK_SIZE)


def wrap_dek(public_key: str, dek: bytes, context: str) -> str:
    """Wrap `dek` to the user's public key. Needs no token: the cleanup job calls this."""
    if len(dek) != DEK_SIZE:
        raise SnapshotError(f"a DEK is {DEK_SIZE} bytes, got {len(dek)}")
    recipient_raw = _b64d(public_key, "public_key")
    try:
        recipient = X25519PublicKey.from_public_bytes(recipient_raw)
    except ValueError as e:
        raise SnapshotKeyError("public_key is not an X25519 key") from e
    ephemeral = X25519PrivateKey.generate()
    eph_raw = _raw_public(ephemeral.public_key())
    kek = _hkdf(ephemeral.exchange(recipient), eph_raw + recipient_raw, _INFO_DEK_WRAP)
    nonce = os.urandom(12)
    return _b64e(eph_raw + nonce + AESGCM(kek).encrypt(nonce, dek, _context(context)))


def unwrap_dek(private: X25519PrivateKey, wrapped: str, context: str) -> bytes:
    """The snapshot's DEK, or SnapshotKeyError (wrong user, wrong context, tampered)."""
    blob = _b64d(wrapped, "wrapped_dek")
    if len(blob) != 32 + 12 + DEK_SIZE + TAG_SIZE:
        raise SnapshotKeyError("wrapped_dek has the wrong length")
    eph_raw, nonce, sealed = blob[:32], blob[32:44], blob[44:]
    recipient_raw = _raw_public(private.public_key())
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(eph_raw))
        kek = _hkdf(shared, eph_raw + recipient_raw, _INFO_DEK_WRAP)
        return AESGCM(kek).decrypt(nonce, sealed, _context(context))
    except (InvalidTag, ValueError) as e:
        raise SnapshotKeyError("cannot unwrap the snapshot key") from e


def open_dek(token: str, key: UserKey, wrapped_dek: str, context: str) -> bytes:
    """Token → private key → this snapshot's DEK, in one call (the restore endpoint, B-109)."""
    return unwrap_dek(unwrap_private_key(token, key), wrapped_dek, context)


# ---- the sealed file -----------------------------------------------------------------------

def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 2 ** 32:
        raise SnapshotError("archive too large for one snapshot")
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def _read_exact(fh, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        piece = fh.read(n - len(out))
        if not piece:
            break
        out += piece
    return bytes(out)


def encrypt_stream(src, dst, dek: bytes, context: str, chunk_size: int = CHUNK_SIZE) -> int:
    """Encrypt everything readable from `src` into `dst`; returns the plaintext byte count."""
    if len(dek) != DEK_SIZE:
        raise SnapshotError(f"a DEK is {DEK_SIZE} bytes, got {len(dek)}")
    if not 1 <= chunk_size < 2 ** 32:
        raise SnapshotError("bad chunk size")
    prefix = os.urandom(7)
    header = MAGIC + bytes([VERSION]) + prefix + chunk_size.to_bytes(4, "big")
    aad = header + _context(context)
    aead = AESGCM(dek)
    dst.write(header)
    total, counter = 0, 0
    current = _read_exact(src, chunk_size)
    while True:
        following = _read_exact(src, chunk_size) if len(current) == chunk_size else b""
        last = not following
        dst.write(aead.encrypt(_nonce(prefix, counter, last), current, aad))
        total += len(current)
        if last:
            return total
        counter += 1
        current = following


def decrypt_stream(src, dst, dek: bytes, context: str) -> int:
    """Decrypt a sealed stream into `dst`; returns the plaintext byte count. Raises
    SnapshotFormatError on a foreign, truncated, extended or modified file and SnapshotKeyError
    when the DEK or context is wrong (indistinguishable from tampering by design). The caller
    must discard `dst` on any error: earlier chunks may already have been written."""
    header = _read_exact(src, HEADER_SIZE)
    if len(header) != HEADER_SIZE or not header.startswith(MAGIC):
        raise SnapshotFormatError("not a daolab snapshot")
    if header[len(MAGIC)] != VERSION:
        raise SnapshotFormatError(f"unsupported snapshot version {header[len(MAGIC)]}")
    prefix = header[len(MAGIC) + 1:len(MAGIC) + 8]
    chunk_size = int.from_bytes(header[-4:], "big")
    if chunk_size < 1:
        raise SnapshotFormatError("bad chunk size")
    aad = header + _context(context)
    aead = AESGCM(dek)
    sealed_size = chunk_size + TAG_SIZE
    total, counter = 0, 0
    current = _read_exact(src, sealed_size)
    if len(current) < TAG_SIZE:
        raise SnapshotFormatError("snapshot is truncated")
    while True:
        following = _read_exact(src, sealed_size) if len(current) == sealed_size else b""
        last = not following
        try:
            plain = aead.decrypt(_nonce(prefix, counter, last), current, aad)
        except InvalidTag as e:
            if counter == 0:
                raise SnapshotKeyError("wrong key, wrong context, or a modified snapshot") from e
            raise SnapshotFormatError("snapshot is truncated or modified") from e
        dst.write(plain)
        total += len(plain)
        if last:
            return total
        if len(following) < TAG_SIZE:
            raise SnapshotFormatError("snapshot is truncated")
        counter += 1
        current = following


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _private_open(path: str):
    """Open `path` for writing, mode 0600 from the first byte (no umask window)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "wb")


def seal(src_path: str, dst_path: str, public_key: str, context: str,
         max_bytes: int | None = None) -> SealResult:
    """Encrypt the archive at `src_path` to `dst_path` (0600, written to a temp name and renamed,
    so a crash never leaves a half file under the final name) for the owner of `public_key`.
    `max_bytes` caps the plaintext (DAOLAB_SNAPSHOT_MAX_MB): over it, nothing is written."""
    size = os.path.getsize(src_path)
    if max_bytes is not None and size > max_bytes:
        raise SnapshotError(f"workspace is {size} bytes, over the {max_bytes}-byte cap")
    dek = new_dek()
    wrapped = wrap_dek(public_key, dek, context)
    tmp = dst_path + ".part"
    try:
        with open(src_path, "rb") as src, _private_open(tmp) as dst:
            plain = encrypt_stream(src, dst, dek, context)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, dst_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return SealResult(wrapped_dek=wrapped, sha256=_sha256_file(dst_path),
                      size_bytes=os.path.getsize(dst_path), plain_bytes=plain)


def open_(src_path: str, dst_path: str, dek: bytes, context: str,
          expect_sha256: str | None = None) -> int:
    """Decrypt `src_path` to `dst_path` (0600). With `expect_sha256`, the sealed file is checked
    first (the `snapshots.sha256` recorded at seal time). On any error `dst_path` is removed."""
    if expect_sha256 is not None and _sha256_file(src_path) != expect_sha256:
        raise SnapshotFormatError("sealed file does not match its recorded sha256")
    try:
        with open(src_path, "rb") as src, _private_open(dst_path) as dst:
            return decrypt_stream(src, dst, dek, context)
    except BaseException:
        if os.path.exists(dst_path):
            os.unlink(dst_path)
        raise


# ---- command line (the playbooks' interface) ----------------------------------------------

DEK_ENV = "DAOLAB_SNAPSHOT_DEK"


def _read_dek(args) -> bytes:
    if args.dek_file:
        with open(args.dek_file, encoding="ascii") as fh:
            text = fh.read().strip()
    else:
        text = os.environ.get(DEK_ENV, "").strip()
    if not text:
        raise SnapshotKeyError(f"no DEK: set {DEK_ENV} or pass --dek-file")
    dek = _b64d(text, "DEK")
    if len(dek) != DEK_SIZE:
        raise SnapshotKeyError("DEK has the wrong length")
    return dek


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m service.snapshot",
                                description="Seal or open a daolab workspace snapshot.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal", help="encrypt an archive for one user (no token needed)")
    s.add_argument("--in", dest="src", required=True, help="the plaintext archive (.tgz)")
    s.add_argument("--out", dest="dst", required=True, help="the sealed file to write")
    s.add_argument("--public-key", required=True, help="user_keys.public_key (base64)")
    s.add_argument("--context", required=True, help='e.g. "user:3|snapshot:17"')
    s.add_argument("--max-bytes", type=int, default=None)
    s.add_argument("--result-json", help="write the SealResult here (else stdout)")
    o = sub.add_parser("open", help=f"decrypt a sealed file (DEK from ${DEK_ENV} or --dek-file)")
    o.add_argument("--in", dest="src", required=True)
    o.add_argument("--out", dest="dst", required=True)
    o.add_argument("--context", required=True)
    o.add_argument("--dek-file")
    o.add_argument("--sha256", help="refuse unless the sealed file has this sha256")
    args = p.parse_args(argv)
    try:
        if args.cmd == "seal":
            result = json.dumps(asdict(seal(args.src, args.dst, args.public_key, args.context,
                                            args.max_bytes)))
            if args.result_json:
                with _private_open(args.result_json) as fh:
                    fh.write(result.encode())
            else:
                print(result)
        else:
            n = open_(args.src, args.dst, _read_dek(args), args.context, args.sha256)
            print(json.dumps({"plain_bytes": n}))
    except (SnapshotError, OSError) as e:
        print(f"snapshot {args.cmd} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
