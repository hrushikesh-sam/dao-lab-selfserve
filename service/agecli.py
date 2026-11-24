"""The only code that runs the pinned age v1.1.1 CLI. No shell, no key material in
argv, environment or temp files; identities travel through stdin/stdout pipes."""
import re
import subprocess

from . import config

RECIPIENT_RE = re.compile(r"^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$")
IDENTITY_RE = re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]{58}$")
PIN = "v1.1.1"

VERSION_FAILED = "age:version:failed"
UNSUPPORTED_VERSION = "age:version:unsupported"
KEYGEN_FAILED = "age-keygen:keygen:failed"
RECIPIENT_FAILED = "age-keygen:recipient:failed"
ENCRYPT_FAILED = "age:encrypt:failed"
DECRYPT_FAILED = "age:decrypt:failed"
INVALID_RECIPIENT = "age:recipient:invalid"
INVALID_IDENTITY = "age:identity:invalid"
INVALID_OUTPUT = "age:output:invalid"
FIXED_MESSAGES = frozenset({VERSION_FAILED, UNSUPPORTED_VERSION, KEYGEN_FAILED, RECIPIENT_FAILED,
                            ENCRYPT_FAILED, DECRYPT_FAILED, INVALID_RECIPIENT, INVALID_IDENTITY,
                            INVALID_OUTPUT})


class AgeError(Exception):
    def __init__(self, message: str, rc: int | None = None):
        super().__init__(message)
        self.message = message
        self.rc = rc


def _run(argv: list[str], message: str, *, input: bytes | None = None,
         stdin: int | None = None):
    kwargs = {"capture_output": True}
    if input is not None:
        kwargs["input"] = input
    if stdin is not None:
        kwargs["stdin"] = stdin
    try:
        return subprocess.run(argv, **kwargs)
    except OSError as exc:
        raise AgeError(message) from exc


def _check_version(binary: str) -> None:
    proc = _run([binary, "--version"], VERSION_FAILED)
    if proc.returncode != 0:
        raise AgeError(VERSION_FAILED, proc.returncode)
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("ascii", "replace")
    if PIN not in text.split():
        raise AgeError(UNSUPPORTED_VERSION, proc.returncode)


def version() -> str:
    _check_version(config.settings.age_bin)
    _check_version(config.settings.age_keygen_bin)
    return PIN


def _find_line(pattern, value: bytes, message: str, rc: int | None = None) -> str:
    for raw in value.decode("ascii", "replace").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        if pattern.fullmatch(text):
            return text
    raise AgeError(message, rc)


def _identity(value: bytes) -> bytes:
    text = _find_line(IDENTITY_RE, value, INVALID_IDENTITY)
    return (text + "\n").encode("ascii")


def _recipient(value: str) -> str:
    if not RECIPIENT_RE.fullmatch(value):
        raise AgeError(INVALID_RECIPIENT)
    return value


def _recipient_of(identity: bytes) -> str:
    proc = _run([config.settings.age_keygen_bin, "-y", "-"], RECIPIENT_FAILED, input=identity)
    if proc.returncode != 0:
        raise AgeError(RECIPIENT_FAILED, proc.returncode)
    return _find_line(RECIPIENT_RE, proc.stdout, INVALID_OUTPUT, proc.returncode)


def keygen() -> tuple[bytes, str]:
    version()
    proc = _run([config.settings.age_keygen_bin], KEYGEN_FAILED)
    if proc.returncode != 0:
        raise AgeError(KEYGEN_FAILED, proc.returncode)
    identity = (_find_line(IDENTITY_RE, proc.stdout, INVALID_OUTPUT, proc.returncode)
                + "\n").encode("ascii")
    return identity, _recipient_of(identity)


def recipient_of(identity: bytes) -> str:
    value = _identity(identity)
    version()
    return _recipient_of(value)


def encrypt_to(recipient: str, data: bytes) -> bytes:
    value = _recipient(recipient)
    version()
    proc = _run([config.settings.age_bin, "-r", value], ENCRYPT_FAILED, input=data)
    if proc.returncode != 0:
        raise AgeError(ENCRYPT_FAILED, proc.returncode)
    return proc.stdout


def encrypt_file_to(recipient: str, src_fd: int, dst_path: str) -> None:
    value = _recipient(recipient)
    version()
    proc = _run([config.settings.age_bin, "-r", value, "-o", dst_path], ENCRYPT_FAILED, stdin=src_fd)
    if proc.returncode != 0:
        raise AgeError(ENCRYPT_FAILED, proc.returncode)


def decrypt_file(identity: bytes, ciphertext_path: str) -> bytes:
    value = _identity(identity)
    version()
    proc = _run([config.settings.age_bin, "-d", "-i", "-", ciphertext_path],
                DECRYPT_FAILED, input=value)
    if proc.returncode != 0:
        raise AgeError(DECRYPT_FAILED, proc.returncode)
    return proc.stdout
