"""B-112a: service/tripwire.py, the controller side of the tripwire (design v1.1 §3, Q-BASELINE).

The authoritative baseline lives on the controller; `load_baseline` refuses anything but the
closed `daolab-tripwire/1` format and returns the sha256 of the exact file bytes (B-112b records
it and streams those bytes to the checker). `parse_output` turns the checker's stdout into
residue items `tripwire:<class>:<count>` and refuses everything else; `TripwireError` carries a
fixed code, never the raw text. `build_baseline` makes a synthetic baseline from collect mode.

The fake board and the checker runner come from tests/dpu/test_tripwire.py (loaded by path:
that file is stdlib + pytest only, so it can't import `service` itself).
"""
import hashlib
import importlib
import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNTHETIC = os.path.join(REPO, "deploy", "tripwire", "synthetic-test.json")
DPU_TESTS = os.path.join(REPO, "tests", "dpu", "test_tripwire.py")
TOKEN = "zq7plant"
MIB16 = 16 * 1024 * 1024
# The synthetic baseline's noise rules: the four playbook noise sources of FINDINGS §7 that have
# a glob (synthetic only; production baselines are Rama's) and the tests' own noise glob.
SYNTHETIC_IGNORE = {
    "boot": ["etc/systemd/system/daolab-app-exporter.service"],
    "tmp": ["tmp/dao-smart-nic-ansible.log", "tmp/pci-after.txt", "tmp/daolab-test-noise-*"],
    "daolab": ["etc/daolab/otelcol.yaml"],
}
# Classes whose records hold no uid or file mode: their fingerprints are the same on any
# machine that builds the fake tree, so the committed synthetic file must match them exactly.
PORTABLE = ("sched", "boot", "mount", "proc")

_DPU = None


def dpu():
    global _DPU
    if _DPU is None:
        spec = importlib.util.spec_from_file_location("b112a_dpu_tripwire_helpers", DPU_TESTS)
        module = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old
        _DPU = module
    return _DPU


@pytest.fixture
def tw():
    return importlib.import_module("service.tripwire")


def collect_text(root, proc, ignore=None):
    r = dpu().run(root, proc, "collect", dpu().collect_doc(ignore))
    assert (r.returncode, r.stderr) == (0, b""), r
    return r.stdout.decode("ascii")


def synthetic_baseline(tw, base_dir):
    """How deploy/tripwire/synthetic-test.json is made (deploy/tripwire/README.md)."""
    root, proc = dpu().make_tree(str(base_dir))
    return tw.build_baseline(collect_text(root, proc, SYNTHETIC_IGNORE), "test", 1,
                             SYNTHETIC_IGNORE)


def dump(doc):
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def good(tw, tmp_path):
    root, proc = dpu().make_tree(str(tmp_path / "board"))
    return root, proc, tw.build_baseline(collect_text(root, proc), "test", 1, None)


def put(tmp_path, data, name="baseline.json"):
    path = tmp_path / name
    path.write_bytes(data if isinstance(data, bytes) else dump(data))
    return str(path)


def assert_clean_error(err, *needles):
    """A TripwireError: a fixed code only; no raw text, no path, no chained exception."""
    texts = [str(err), repr(err), repr(err.args), str(err.code)]
    for needle in (TOKEN,) + needles:
        assert all(needle not in t for t in texts), (needle, texts)
    assert err.args == (err.code,)
    assert err.__cause__ is None and err.__context__ is None


# ============================================================================================
# Shared constants
# ============================================================================================
def test_classes_and_format_match_the_checker(tw):
    assert tw.CLASSES == dpu().CLASSES == tuple(dpu().mod().CLASSES)
    assert tw.FORMAT == dpu().FORMAT == "daolab-tripwire/1"


# ============================================================================================
# load_baseline
# ============================================================================================
def test_load_baseline_returns_the_doc_and_the_sha256_of_the_file_bytes(tw, tmp_path):
    _, _, doc = good(tw, tmp_path)
    data = dump(doc)
    loaded, sha = tw.load_baseline(put(tmp_path, data), "test")
    assert loaded == doc
    assert sha == hashlib.sha256(data).hexdigest()
    compact = json.dumps(doc).encode()                       # same doc, other bytes
    assert tw.load_baseline(put(tmp_path, compact, "b2.json"), "test")[1] == \
        hashlib.sha256(compact).hexdigest() != sha


def _with(doc, path, value):
    doc = json.loads(json.dumps(doc))
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


FP = ["classes", "kmod", "fingerprints"]
IG = ["classes", "tmp", "ignore"]
REFUSALS = [                                                   # (id, mutate, loader code)
    ("not-an-object", lambda b: [b], "bad_format"),
    ("no-format", lambda b: _with(b, ["format"], KeyError), "bad_format"),
    ("wrong-format", lambda b: _with(b, ["format"], "daolab-tripwire/2"), "bad_format"),
    ("collect-format", lambda b: _with(b, ["format"], "daolab-tripwire-collect/1"), "bad_format"),
    ("unknown-top-key", lambda b: _with(b, [f"{TOKEN}key"], 1), "unknown_key"),
    ("missing-version", lambda b: _with(b, ["version"], KeyError), "missing_key"),
    ("missing-hw-class", lambda b: _with(b, ["hw_class"], KeyError), "missing_key"),
    ("missing-classes", lambda b: _with(b, ["classes"], KeyError), "missing_key"),
    ("version-0", lambda b: _with(b, ["version"], 0), "bad_version"),
    ("version-negative", lambda b: _with(b, ["version"], -3), "bad_version"),
    ("version-str", lambda b: _with(b, ["version"], "1"), "bad_version"),
    ("version-bool", lambda b: _with(b, ["version"], True), "bad_version"),
    ("version-float", lambda b: _with(b, ["version"], 1.0), "bad_version"),
    ("hw-class-shape", lambda b: _with(b, ["hw_class"], f"Bad_{TOKEN}"), "bad_hw_class"),
    ("hw-class-double-dash", lambda b: _with(b, ["hw_class"], "a--b"), "bad_hw_class"),
    ("hw-class-long", lambda b: _with(b, ["hw_class"], "a" * 129), "bad_hw_class"),
    ("hw-class-not-str", lambda b: _with(b, ["hw_class"], 7), "bad_hw_class"),
    ("hw-class-other", lambda b: _with(b, ["hw_class"], "other"), "hw_class_mismatch"),
    ("classes-not-object", lambda b: _with(b, ["classes"], []), "bad_class"),
    ("unknown-class", lambda b: _with(b, ["classes", f"{TOKEN}cls"],
                                      {"fingerprints": [], "ignore": []}), "bad_class"),
    ("missing-class", lambda b: _with(b, ["classes", "kmod"], KeyError), "missing_key"),
    ("class-not-object", lambda b: _with(b, ["classes", "kmod"], []), "bad_class"),
    ("unknown-class-key", lambda b: _with(b, ["classes", "kmod", f"{TOKEN}k"], []),
     "unknown_key"),
    ("missing-fingerprints", lambda b: _with(b, FP, KeyError), "missing_key"),
    ("missing-ignore", lambda b: _with(b, ["classes", "kmod", "ignore"], KeyError),
     "missing_key"),
    ("fingerprints-not-list", lambda b: _with(b, FP, "x"), "bad_hex"),
    ("hex-short", lambda b: _with(b, FP, ["a" * 63]), "bad_hex"),
    ("hex-long", lambda b: _with(b, FP, ["a" * 65]), "bad_hex"),
    ("hex-upper", lambda b: _with(b, FP, ["A" * 64]), "bad_hex"),
    ("hex-not-hex", lambda b: _with(b, FP, ["g" * 64]), "bad_hex"),
    ("hex-trailing-newline", lambda b: _with(b, FP, ["a" * 64 + "\n"]), "bad_hex"),
    ("hex-not-str", lambda b: _with(b, FP, [1]), "bad_hex"),
    ("hex-duplicate", lambda b: _with(b, FP, ["a" * 64] * 2), "duplicate"),
    ("ignore-not-list", lambda b: _with(b, IG, "tmp/*"), "bad_glob"),
    ("glob-absolute", lambda b: _with(b, IG, [f"/tmp/{TOKEN}"]), "bad_glob"),
    ("glob-empty", lambda b: _with(b, IG, [""]), "bad_glob"),
    ("glob-newline", lambda b: _with(b, IG, ["a\nb"]), "bad_glob"),
    ("glob-cr", lambda b: _with(b, IG, ["a\rb"]), "bad_glob"),
    ("glob-nul", lambda b: _with(b, IG, ["a\x00b"]), "bad_glob"),
    ("glob-long", lambda b: _with(b, IG, ["a" * 257]), "bad_glob"),
    ("glob-not-str", lambda b: _with(b, IG, [3]), "bad_glob"),
    ("glob-duplicate", lambda b: _with(b, IG, ["a", "a"]), "duplicate"),
    ("glob-too-many", lambda b: _with(b, IG, [f"g{i}" for i in range(257)]), "bad_glob"),
]
BYTE_REFUSALS = [
    ("empty", b""),
    ("not-json", b"{"),
    ("garbage", f"{TOKEN} not json".encode()),
    ("not-utf8", b'{"format": "\xff"}'),
    ("utf8-bom", b'\xef\xbb\xbf{"format": "daolab-tripwire/1"}'),
    ("utf16", '{"format": "daolab-tripwire/1"}'.encode("utf-16")),
    ("duplicate-key", b'{"format": "daolab-tripwire/1", "format": "daolab-tripwire/1"}'),
    ("nan", b'{"format": "daolab-tripwire/1", "version": NaN}'),
    ("infinity", b'{"format": "daolab-tripwire/1", "version": Infinity}'),
    ("deep-nesting", b"[" * 100000 + b"]" * 100000),
    ("huge-int", b'{"format": "daolab-tripwire/1", "version": ' + b"9" * 5000 + b"}"),
    ("over-16-mib", b" " * (MIB16 + 1)),
]


@pytest.mark.parametrize("case", REFUSALS, ids=[c[0] for c in REFUSALS])
def test_load_baseline_refuses_each_bad_baseline(tw, tmp_path, case):
    _, mutate, code = case
    _, _, doc = good(tw, tmp_path)
    path = put(tmp_path, mutate(doc))
    with pytest.raises(tw.TripwireError) as e:
        tw.load_baseline(path, "test")
    assert e.value.code == code
    assert_clean_error(e.value, path, str(tmp_path))


@pytest.mark.parametrize("case", BYTE_REFUSALS, ids=[c[0] for c in BYTE_REFUSALS])
def test_load_baseline_refuses_unparseable_bytes(tw, tmp_path, case):
    path = put(tmp_path, case[1])
    with pytest.raises(tw.TripwireError) as e:
        tw.load_baseline(path, "test")
    assert e.value.code == "bad_format"
    assert_clean_error(e.value, path)


def test_load_baseline_refuses_a_missing_or_unreadable_path(tw, tmp_path):
    for path in (str(tmp_path / f"{TOKEN}.json"), str(tmp_path)):
        with pytest.raises(tw.TripwireError) as e:
            tw.load_baseline(path, "test")
        assert e.value.code == "unreadable"
        assert_clean_error(e.value, path)


def test_load_baseline_refuses_a_bad_hw_class_argument(tw, tmp_path):
    _, _, doc = good(tw, tmp_path)
    with pytest.raises(tw.TripwireError) as e:
        tw.load_baseline(put(tmp_path, doc), f"Bad {TOKEN}")
    assert e.value.code == "bad_hw_class"
    assert_clean_error(e.value)


def test_load_baseline_hw_class_shape_is_the_catalogs(tw, tmp_path):
    from service import catalog
    _, _, doc = good(tw, tmp_path)
    for hw in ("a", "cn10k-ebf-2", "x1-y2-z3"):
        assert catalog.KEBAB.match(hw)
        loaded, _ = tw.load_baseline(put(tmp_path, _with(doc, ["hw_class"], hw)), hw)
        assert loaded["hw_class"] == hw
    for hw in ("-a", "a-", "A", "a_b", "a.b", ""):
        assert not catalog.KEBAB.match(hw)
        with pytest.raises(tw.TripwireError):
            tw.load_baseline(put(tmp_path, _with(doc, ["hw_class"], hw)), "test")


# ---- the checker agrees with the loader ----------------------------------------------------
@pytest.mark.parametrize("case", [c for c in REFUSALS if c[2] != "hw_class_mismatch"],
                         ids=[c[0] for c in REFUSALS if c[2] != "hw_class_mismatch"])
def test_the_checker_refuses_what_the_loader_refuses(tw, tmp_path, case):
    root, proc, doc = good(tw, tmp_path)
    r = dpu().run(root, proc, "check", case[1](doc))
    assert r.returncode == 2 and r.stderr == b""
    assert r.stdout in (b"tripwire:error:bad_input\n", b"tripwire:error:bad_baseline\n")


@pytest.mark.parametrize("case", BYTE_REFUSALS, ids=[c[0] for c in BYTE_REFUSALS])
def test_the_checker_refuses_the_same_bytes(tw, tmp_path, case):
    root, proc = dpu().make_tree(str(tmp_path))
    dpu().assert_error(dpu().run(root, proc, "check", case[1]), "bad_input")


def test_the_checker_accepts_the_exact_bytes_the_loader_accepted(tw, tmp_path):
    root, proc, doc = good(tw, tmp_path)
    path = put(tmp_path, doc)
    _, sha = tw.load_baseline(path, "test")
    with open(path, "rb") as fh:
        data = fh.read()
    assert hashlib.sha256(data).hexdigest() == sha
    dpu().assert_counts(dpu().run(root, proc, "check", data), {})


# ============================================================================================
# parse_output
# ============================================================================================
ACCEPTED = [
    ("tripwire:ok\n", []),
    ("tripwire:ok", []),
    ("tripwire:sudo:2\n", ["tripwire:sudo:2"]),
    ("tripwire:identity:1\ntripwire:daolab:999999999\n",
     ["tripwire:identity:1", "tripwire:daolab:999999999"]),
]


@pytest.mark.parametrize("text,items", ACCEPTED)
def test_parse_output_accepts(tw, text, items):
    assert tw.parse_output(text) == items


def test_parse_output_accepts_every_class_in_order(tw):
    text = "".join(f"tripwire:{c}:{i + 1}\n" for i, c in enumerate(tw.CLASSES))
    assert tw.parse_output(text) == [f"tripwire:{c}:{i + 1}" for i, c in enumerate(tw.CLASSES)]


REFUSED = [
    "", "\n", "tripwire:ok\n\n", "tripwire:ok\r\n", "tripwire:OK", "TRIPWIRE:ok",
    "tripwire:sudo:0", "tripwire:sudo:-1", "tripwire:sudo:01", "tripwire:sudo:+1",
    "tripwire:sudo:1000000000", "tripwire:sudo:١", "tripwire:sudo:1.0", "tripwire:sudo: 1",
    f"tripwire:{TOKEN}cls:1", "tripwire:sudo:1\ntripwire:sudo:1",
    "tripwire:tmp:1\ntripwire:sudo:1", "tripwire:ok\ntripwire:sudo:1",
    "tripwire:sudo:1\ntripwire:ok", "tripwire:sudo", "tripwire:sudo:1:x", "trip:sudo:1",
    " tripwire:sudo:1", "tripwire:sudo:1 ", "tripwire:error:bad_input\ntripwire:sudo:1",
    "tripwire:sudo:1\ntripwire:error:internal", f"tripwire:error:{TOKEN}", "tripwire:error:",
    "tripwire:error:bad_input:x", f"tripwire:sudo:1\n/etc/sudoers.d/{TOKEN}",
    f"Traceback (most recent call last):\n  File \"/opt/{TOKEN}\"",
]


@pytest.mark.parametrize("text", REFUSED, ids=[repr(t)[:40] for t in REFUSED])
def test_parse_output_refuses_anything_else(tw, text):
    with pytest.raises(tw.TripwireError) as e:
        tw.parse_output(text)
    assert e.value.code == "bad_output"
    assert_clean_error(e.value, *(t for t in text.split("\n") if len(t) > 3))


@pytest.mark.parametrize("value", [b"tripwire:ok\n", None, ["tripwire:ok"]])
def test_parse_output_refuses_non_text(tw, value):
    with pytest.raises(tw.TripwireError) as e:
        tw.parse_output(value)
    assert e.value.code == "bad_output"


@pytest.mark.parametrize("code", ["bad_input", "bad_baseline", "internal"])
def test_parse_output_raises_the_checkers_error_code(tw, code):
    for text in (f"tripwire:error:{code}\n", f"tripwire:error:{code}"):
        with pytest.raises(tw.TripwireError) as e:
            tw.parse_output(text)
        assert e.value.code == code
        assert_clean_error(e.value)


def test_parse_output_items_are_residue_items_of_kind_tripwire(tw):
    """B-112b's contract (task list §5.7): parse_residue splits on the first ':'."""
    for item in tw.parse_output("tripwire:sudo:2\ntripwire:tmp:11\n"):
        kind, _, ident = item.partition(":")
        assert kind == "tripwire" and ident.split(":")[0] in tw.CLASSES
        assert ident.split(":")[1].isdigit()


def test_parse_output_of_a_real_checker_run(tw, tmp_path):
    root, proc, doc = good(tw, tmp_path)
    dpu().ADD["sudo"](root, proc)
    dpu().ADD["mount"](root, proc)
    r = dpu().run(root, proc, "check", doc)
    assert tw.parse_output(r.stdout.decode("ascii")) == ["tripwire:sudo:1", "tripwire:mount:1"]


# ============================================================================================
# build_baseline
# ============================================================================================
def test_build_baseline_round_trip_is_ok(tw, tmp_path):
    root, proc = dpu().make_tree(str(tmp_path / "board"))
    doc = tw.build_baseline(collect_text(root, proc, SYNTHETIC_IGNORE), "test", 3,
                            SYNTHETIC_IGNORE)
    assert list(doc) == ["format", "version", "hw_class", "classes"]
    assert (doc["format"], doc["version"], doc["hw_class"]) == ("daolab-tripwire/1", 3, "test")
    assert list(doc["classes"]) == list(tw.CLASSES)
    for c in tw.CLASSES:
        entry = doc["classes"][c]
        assert list(entry) == ["fingerprints", "ignore"]
        assert entry["fingerprints"] == sorted(entry["fingerprints"])
        assert entry["ignore"] == SYNTHETIC_IGNORE.get(c, [])
    path = put(tmp_path, doc)
    loaded, _ = tw.load_baseline(path, "test")
    assert loaded == doc
    with open(path, "rb") as fh:
        dpu().assert_counts(dpu().run(root, proc, "check", fh.read()), {})
    dpu().ADD["daolab"](root, proc)
    dpu().assert_counts(dpu().run(root, proc, "check", doc), {"daolab": 1})


def test_build_baseline_accepts_bytes_and_no_ignore(tw, tmp_path):
    root, proc = dpu().make_tree(str(tmp_path))
    doc = tw.build_baseline(collect_text(root, proc).encode("ascii"), "test", 1, None)
    assert all(doc["classes"][c]["ignore"] == [] for c in tw.CLASSES)


def _collected(tw, tmp_path):
    root, proc = dpu().make_tree(str(tmp_path))
    return json.loads(collect_text(root, proc))


BUILD_REFUSALS = [                          # (id, mutate collect doc, hw, version, ignore, code)
    ("not-json", lambda c: "{", "test", 1, None, "bad_format"),
    ("not-object", lambda c: [c], "test", 1, None, "bad_format"),
    ("extra-top-key", lambda c: dict(c, format="x"), "test", 1, None, "unknown_key"),
    ("no-classes", lambda c: {}, "test", 1, None, "missing_key"),
    ("missing-class", lambda c: _with(c, ["classes", "kmod"], KeyError), "test", 1, None,
     "missing_key"),
    ("unknown-class", lambda c: _with(c, ["classes", TOKEN], {"fingerprints": []}), "test", 1,
     None, "bad_class"),
    ("extra-class-key", lambda c: _with(c, ["classes", "kmod", "ignore"], []), "test", 1, None,
     "unknown_key"),
    ("bad-hex", lambda c: _with(c, ["classes", "kmod", "fingerprints"], ["x"]), "test", 1, None,
     "bad_hex"),
    ("dup-hex", lambda c: _with(c, ["classes", "kmod", "fingerprints"], ["a" * 64] * 2), "test",
     1, None, "duplicate"),
    ("bad-hw", lambda c: c, f"Bad_{TOKEN}", 1, None, "bad_hw_class"),
    ("version-0", lambda c: c, "test", 0, None, "bad_version"),
    ("version-bool", lambda c: c, "test", True, None, "bad_version"),
    ("ignore-not-dict", lambda c: c, "test", 1, ["tmp/*"], "bad_class"),
    ("ignore-unknown-class", lambda c: c, "test", 1, {TOKEN: []}, "bad_class"),
    ("ignore-bad-glob", lambda c: c, "test", 1, {"tmp": [f"/{TOKEN}"]}, "bad_glob"),
    ("ignore-dup-glob", lambda c: c, "test", 1, {"tmp": ["a", "a"]}, "duplicate"),
]


@pytest.mark.parametrize("case", BUILD_REFUSALS, ids=[c[0] for c in BUILD_REFUSALS])
def test_build_baseline_refuses(tw, tmp_path, case):
    _, mutate, hw, version, ignore, code = case
    raw = mutate(_collected(tw, tmp_path))
    text = raw if isinstance(raw, str) else json.dumps(raw)
    with pytest.raises(tw.TripwireError) as e:
        tw.build_baseline(text, hw, version, ignore)
    assert e.value.code == code
    assert_clean_error(e.value)


# ============================================================================================
# deploy/tripwire/synthetic-test.json
# ============================================================================================
def test_synthetic_baseline_loads_and_matches_the_fake_tree(tw, tmp_path):
    loaded, sha = tw.load_baseline(SYNTHETIC, "test")
    with open(SYNTHETIC, "rb") as fh:
        data = fh.read()
    assert sha == hashlib.sha256(data).hexdigest()
    assert data == dump(loaded), "keep the file in json.dumps(doc, indent=2) + newline form"
    assert loaded["version"] == 1
    built = synthetic_baseline(tw, tmp_path)
    for c in tw.CLASSES:
        mine, ref = loaded["classes"][c], built["classes"][c]
        assert mine["ignore"] == SYNTHETIC_IGNORE.get(c, [])
        assert len(mine["fingerprints"]) == len(ref["fingerprints"]), c
        if c in PORTABLE:
            assert mine["fingerprints"] == ref["fingerprints"], c


def test_synthetic_baseline_on_a_fresh_fake_tree_differs_only_in_uid_or_mode_classes(tw,
                                                                                    tmp_path):
    root, proc = dpu().make_tree(str(tmp_path))
    with open(SYNTHETIC, "rb") as fh:
        r = dpu().run(root, proc, "check", fh.read())
    items = tw.parse_output(r.stdout.decode("ascii"))
    assert {item.split(":")[1] for item in items}.isdisjoint(PORTABLE), items


def test_synthetic_noise_globs_suppress_the_playbook_noise_sources(tw, tmp_path):
    loaded, _ = tw.load_baseline(SYNTHETIC, "test")
    ignore = {c: loaded["classes"][c]["ignore"] for c in tw.CLASSES}
    root, proc = dpu().make_tree(str(tmp_path))
    doc = tw.build_baseline(collect_text(root, proc, ignore), "test", 1, ignore)
    dpu().write(root, "etc/systemd/system/daolab-app-exporter.service", "--app vpp\n")
    dpu().write(root, "tmp/dao-smart-nic-ansible.log", "launch\n")
    dpu().write(root, "tmp/pci-after.txt", "after, longer than before\n")
    dpu().write(root, "tmp/daolab-test-noise-7", "n")
    dpu().write(root, "etc/daolab/otelcol.yaml", "daolab.user: someone-else\n")
    dpu().assert_counts(dpu().run(root, proc, "check", doc), {})
    dpu().write(root, "usr/local/bin/daolab-ml-smoke", "#!/bin/sh\nchanged\n", 0o755)
    dpu().assert_counts(dpu().run(root, proc, "check", doc), {"bin": 2})   # not ignored
