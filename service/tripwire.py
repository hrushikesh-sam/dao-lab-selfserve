"""B-112a: the controller side of the tripwire (design v1.1 §3; task list Q-BASELINE).

The authoritative baseline lives on the controller: the DPU's copy is not trusted, because the
user was root during the slot. This module loads and verifies a baseline, parses the checker's
output (playbooks/files/daolab-tripwire.py) and builds synthetic baselines from its collect mode.
Nothing calls it yet; B-112b wires it into the runner/engine.

  load_baseline(path, hw_class) -> (doc, sha256)  the closed daolab-tripwire/1 format; the sha256
                                                  is of the exact file bytes (B-112b records it in
                                                  the audit trail and streams those bytes)
  parse_output(text) -> [residue items]           "tripwire:<class>:<count>", [] for "tripwire:ok"
  build_baseline(collect_json, hw_class, version, ignore) -> doc

Every refusal is a TripwireError with a fixed code and nothing else: never the raw text, a path
or a chained exception, so a log or audit row can't carry what the checker or a file held.
The validation rules are the checker's own (it is stdlib-only, so they are written twice; the
tests check that both refuse the same baselines).
"""
import hashlib
import json
import re

from .catalog import KEBAB

FORMAT = "daolab-tripwire/1"
CLASSES = ("identity", "sudo", "ssh", "sched", "boot", "bin", "kmod", "mount", "proc", "tmp",
           "daolab")
CHECKER_ERRORS = ("bad_input", "bad_baseline", "internal")
MAX_BYTES = 16 * 1024 * 1024          # the checker's input limit
MAX_GLOBS = 256
MAX_GLOB_LEN = 256
MAX_HW_CLASS = 128
MAX_INT_DIGITS = 18
HEX = re.compile(r"[0-9a-f]{64}")
COUNT = re.compile(r"[1-9][0-9]{0,8}")
BASELINE_KEYS = ("format", "version", "hw_class", "classes")
ENTRY_KEYS = ("fingerprints", "ignore")


class TripwireError(Exception):
    """A refused baseline or checker output. `code` is one of:
    loader/builder: unreadable, bad_format, unknown_key, missing_key, bad_class, bad_version,
                    bad_hw_class, hw_class_mismatch, bad_hex, duplicate, bad_glob;
    parser:         bad_output, or the checker's own bad_input, bad_baseline, internal."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_BAD = object()


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("not JSON")


def _small_int(text):
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError("too many digits")
    return int(text)


def _loads(data) -> object:
    """Strict JSON (UTF-8, no duplicate keys, no NaN/Infinity, no huge ints) or _BAD. Callers
    raise outside the except block, so no TripwireError ever chains the parser's error (which
    holds the document)."""
    if isinstance(data, str):
        text = data
    elif isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _BAD
    else:
        return _BAD
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant,
                          parse_int=_small_int)
    except (ValueError, RecursionError):
        return _BAD


def _hw_class_ok(value) -> bool:
    # fullmatch, not match: KEBAB ends in `$`, which also matches before a trailing newline
    return isinstance(value, str) and 0 < len(value) <= MAX_HW_CLASS and \
        KEBAB.fullmatch(value) is not None


def _version_ok(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _check_keys(obj: dict, keys: tuple) -> str | None:
    if not set(obj) <= set(keys):
        return "unknown_key"
    if set(obj) != set(keys):
        return "missing_key"
    return None


def _fingerprints_code(values) -> str | None:
    if not isinstance(values, list) or \
            not all(isinstance(v, str) and HEX.fullmatch(v) for v in values):
        return "bad_hex"
    return "duplicate" if len(set(values)) != len(values) else None


def _globs_code(values) -> str | None:
    if not isinstance(values, list) or len(values) > MAX_GLOBS or \
            not all(isinstance(g, str) and 0 < len(g) <= MAX_GLOB_LEN and not g.startswith("/")
                    and not any(c in g for c in "\x00\n\r") for g in values):
        return "bad_glob"
    return "duplicate" if len(set(values)) != len(values) else None


def _classes_code(classes, entry_keys: tuple) -> str | None:
    """The `classes` object: exactly the 11 names, each entry exactly `entry_keys`."""
    if not isinstance(classes, dict) or not set(classes) <= set(CLASSES):
        return "bad_class"
    if set(classes) != set(CLASSES):
        return "missing_key"
    for cls in CLASSES:
        entry = classes[cls]
        if not isinstance(entry, dict):
            return "bad_class"
        code = _check_keys(entry, entry_keys) or _fingerprints_code(entry["fingerprints"])
        if code is None and "ignore" in entry_keys:
            code = _globs_code(entry["ignore"])
        if code:
            return code
    return None


def _baseline_code(doc, hw_class: str) -> str | None:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return "bad_format"
    code = _check_keys(doc, BASELINE_KEYS)
    if code:
        return code
    if not _version_ok(doc["version"]):
        return "bad_version"
    if not _hw_class_ok(doc["hw_class"]):
        return "bad_hw_class"
    if doc["hw_class"] != hw_class:
        return "hw_class_mismatch"
    return _classes_code(doc["classes"], ENTRY_KEYS)


def load_baseline(path: str, hw_class: str) -> tuple[dict, str]:
    """The baseline at `path` for `hw_class`, and the sha256 hex of the file's exact bytes."""
    if not _hw_class_ok(hw_class):
        raise TripwireError("bad_hw_class")
    data = None
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        pass
    if data is None:
        raise TripwireError("unreadable")
    doc = _loads(data) if len(data) <= MAX_BYTES else _BAD
    if doc is _BAD:
        raise TripwireError("bad_format")
    code = _baseline_code(doc, hw_class)
    if code:
        raise TripwireError(code)
    return doc, hashlib.sha256(data).hexdigest()


def parse_output(text: str) -> list[str]:
    """The checker's stdout -> residue items "tripwire:<class>:<count>" ([] when clean).
    Anything but exactly that contract is refused; an error line raises its own code."""
    if not isinstance(text, str):
        raise TripwireError("bad_output")
    body = text[:-1] if text.endswith("\n") else text
    lines = body.split("\n")
    if lines == ["tripwire:ok"]:
        return []
    if len(lines) == 1 and body.startswith("tripwire:error:"):
        code = body[len("tripwire:error:"):]
        raise TripwireError(code if code in CHECKER_ERRORS else "bad_output")
    items, last = [], -1
    for line in lines:
        parts = line.split(":")
        ok = len(parts) == 3 and parts[0] == "tripwire" and parts[1] in CLASSES and \
            COUNT.fullmatch(parts[2]) is not None
        if not ok or CLASSES.index(parts[1]) <= last:
            raise TripwireError("bad_output")
        last = CLASSES.index(parts[1])
        items.append(line)
    return items


def build_baseline(collect_json: str | bytes, hw_class: str, version: int,
                   ignore: dict | None) -> dict:
    """A baseline from the checker's collect-mode line. The collect run must have used the same
    `ignore` globs: an entry the checker skips must not be in the baseline either, or it counts
    as removed on every check."""
    doc = _loads(collect_json)
    if not isinstance(doc, dict):
        raise TripwireError("bad_format")
    code = _check_keys(doc, ("classes",)) or _classes_code(doc["classes"], ("fingerprints",))
    if code:
        raise TripwireError(code)
    if not _hw_class_ok(hw_class):
        raise TripwireError("bad_hw_class")
    if not _version_ok(version):
        raise TripwireError("bad_version")
    ignore = {} if ignore is None else ignore
    if not isinstance(ignore, dict) or not set(ignore) <= set(CLASSES):
        raise TripwireError("bad_class")
    for globs in ignore.values():
        code = _globs_code(globs)
        if code:
            raise TripwireError(code)
    out = {"format": FORMAT, "version": version, "hw_class": hw_class,
           "classes": {cls: {"fingerprints": sorted(doc["classes"][cls]["fingerprints"]),
                             "ignore": list(ignore.get(cls, []))} for cls in CLASSES}}
    code = _baseline_code(out, hw_class)            # the same rules load_baseline applies
    if code:
        raise TripwireError(code)
    return out
