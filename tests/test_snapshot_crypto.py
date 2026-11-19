"""B-103: the snapshot crypto and file format (service/snapshot.py)."""
import base64
import io
import json
import os
import stat

import pytest

from service import snapshot as snap

TOKEN = "tok-alice-" + "a" * 40
OTHER = "tok-bob-" + "b" * 40
CTX = "user:1|snapshot:7"


def _seal_bytes(data: bytes, dek: bytes, context: str = CTX, chunk: int = snap.CHUNK_SIZE) -> bytes:
    out = io.BytesIO()
    snap.encrypt_stream(io.BytesIO(data), out, dek, context, chunk_size=chunk)
    return out.getvalue()


def _open_bytes(sealed: bytes, dek: bytes, context: str = CTX) -> bytes:
    out = io.BytesIO()
    snap.decrypt_stream(io.BytesIO(sealed), out, dek, context)
    return out.getvalue()


# ---- user key ----------------------------------------------------------------------------

def test_user_key_round_trip():
    key = snap.new_user_key(TOKEN)
    private = snap.unwrap_private_key(TOKEN, key)
    assert snap._b64e(snap._raw_public(private.public_key())) == key.public_key


def test_user_key_is_not_stored_in_the_clear():
    key = snap.new_user_key(TOKEN)
    raw = snap._raw_private(snap.unwrap_private_key(TOKEN, key))
    assert raw not in base64.b64decode(key.wrapped_private_key)
    assert TOKEN not in json.dumps(key.__dict__)


@pytest.mark.parametrize("token", [OTHER, "", TOKEN + "x", TOKEN[:-1]])
def test_wrong_token_cannot_unwrap(token):
    key = snap.new_user_key(TOKEN)
    with pytest.raises(snap.SnapshotKeyError):
        snap.unwrap_private_key(token, key)


def test_two_keys_for_the_same_token_differ():
    a, b = snap.new_user_key(TOKEN), snap.new_user_key(TOKEN)
    assert a.public_key != b.public_key and a.kdf_salt != b.kdf_salt


def test_tampered_user_key_is_refused():
    key = snap.new_user_key(TOKEN)
    blob = bytearray(base64.b64decode(key.wrapped_private_key))
    blob[-1] ^= 1
    bad = snap.UserKey(key.public_key, snap._b64e(bytes(blob)), key.kdf_salt)
    with pytest.raises(snap.SnapshotKeyError):
        snap.unwrap_private_key(TOKEN, bad)


def test_swapped_public_half_is_refused():
    key, other = snap.new_user_key(TOKEN), snap.new_user_key(TOKEN)
    with pytest.raises(snap.SnapshotKeyError):
        snap.unwrap_private_key(TOKEN, snap.UserKey(other.public_key, key.wrapped_private_key,
                                                    key.kdf_salt))


def test_garbage_base64_is_a_key_error():
    key = snap.new_user_key(TOKEN)
    with pytest.raises(snap.SnapshotKeyError):
        snap.unwrap_private_key(TOKEN, snap.UserKey(key.public_key, "!!not base64!!", key.kdf_salt))


# ---- token renewal (B-105 relies on this) --------------------------------------------------

def test_rewrap_keeps_old_snapshots_readable():
    key = snap.new_user_key(TOKEN)
    dek = snap.new_dek()
    wrapped = snap.wrap_dek(key.public_key, dek, CTX)
    renewed = snap.rewrap_user_key(TOKEN, OTHER, key)
    assert renewed.public_key == key.public_key
    assert renewed.kdf_salt != key.kdf_salt
    assert snap.open_dek(OTHER, renewed, wrapped, CTX) == dek
    with pytest.raises(snap.SnapshotKeyError):
        snap.unwrap_private_key(TOKEN, renewed)          # the old token no longer works


def test_rewrap_needs_the_old_token():
    key = snap.new_user_key(TOKEN)
    with pytest.raises(snap.SnapshotKeyError):
        snap.rewrap_user_key("not-the-token", OTHER, key)


# ---- data key ----------------------------------------------------------------------------

def test_dek_wrap_round_trip_needs_only_the_public_key_to_wrap():
    key = snap.new_user_key(TOKEN)
    dek = snap.new_dek()
    wrapped = snap.wrap_dek(key.public_key, dek, CTX)
    assert snap.open_dek(TOKEN, key, wrapped, CTX) == dek


def test_dek_is_bound_to_its_context():
    key = snap.new_user_key(TOKEN)
    wrapped = snap.wrap_dek(key.public_key, snap.new_dek(), CTX)
    with pytest.raises(snap.SnapshotKeyError):
        snap.open_dek(TOKEN, key, wrapped, "user:1|snapshot:8")


def test_dek_wrapped_for_one_user_does_not_open_for_another():
    alice, bob = snap.new_user_key(TOKEN), snap.new_user_key(OTHER)
    wrapped = snap.wrap_dek(alice.public_key, snap.new_dek(), CTX)
    with pytest.raises(snap.SnapshotKeyError):
        snap.open_dek(OTHER, bob, wrapped, CTX)


def test_wrap_rejects_a_short_dek():
    key = snap.new_user_key(TOKEN)
    with pytest.raises(snap.SnapshotError):
        snap.wrap_dek(key.public_key, b"short", CTX)


# ---- stream format -------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 64, 65, 300])
def test_stream_round_trip_at_chunk_edges(size):
    data = os.urandom(size)
    dek = snap.new_dek()
    sealed = _seal_bytes(data, dek, chunk=16)
    assert _open_bytes(sealed, dek) == data


def test_stream_round_trip_default_chunk():
    data = os.urandom(3 * snap.CHUNK_SIZE + 123)
    dek = snap.new_dek()
    assert _open_bytes(_seal_bytes(data, dek), dek) == data


def test_ciphertext_does_not_contain_plaintext():
    data = b"SECRET-NOTES-" * 1000
    sealed = _seal_bytes(data, snap.new_dek())
    assert b"SECRET-NOTES" not in sealed


def test_wrong_dek_is_refused():
    sealed = _seal_bytes(b"hello", snap.new_dek())
    with pytest.raises(snap.SnapshotKeyError):
        _open_bytes(sealed, snap.new_dek())


def test_wrong_context_is_refused():
    dek = snap.new_dek()
    sealed = _seal_bytes(b"hello", dek)
    with pytest.raises(snap.SnapshotError):
        _open_bytes(sealed, dek, "user:2|snapshot:7")


def test_not_a_snapshot():
    with pytest.raises(snap.SnapshotFormatError):
        _open_bytes(b"PK\x03\x04 a zip file", snap.new_dek())


def test_unknown_version():
    dek = snap.new_dek()
    sealed = bytearray(_seal_bytes(b"x", dek))
    sealed[len(snap.MAGIC)] = 9
    with pytest.raises(snap.SnapshotFormatError):
        _open_bytes(bytes(sealed), dek)


@pytest.mark.parametrize("where", ["header", "first", "middle", "last"])
def test_flipped_bit_anywhere_is_refused(where):
    dek = snap.new_dek()
    sealed = bytearray(_seal_bytes(os.urandom(100), dek, chunk=16))   # 7 chunks
    pos = {"header": len(snap.MAGIC) + 3, "first": snap.HEADER_SIZE + 2,
           "middle": snap.HEADER_SIZE + 3 * 32 + 5, "last": len(sealed) - 1}[where]
    sealed[pos] ^= 0x40
    with pytest.raises(snap.SnapshotError):
        _open_bytes(bytes(sealed), dek)


@pytest.mark.parametrize("cut_chunks", [1, 2])
def test_truncation_at_a_chunk_boundary_is_refused(cut_chunks):
    dek = snap.new_dek()
    sealed = _seal_bytes(os.urandom(64), dek, chunk=16)   # exactly 4 full chunks
    with pytest.raises(snap.SnapshotError):
        _open_bytes(sealed[: len(sealed) - cut_chunks * 32], dek)


def test_truncation_inside_a_chunk_is_refused():
    dek = snap.new_dek()
    sealed = _seal_bytes(os.urandom(100), dek, chunk=16)
    with pytest.raises(snap.SnapshotError):
        _open_bytes(sealed[:-5], dek)


def test_appended_chunk_is_refused():
    dek = snap.new_dek()
    sealed = _seal_bytes(os.urandom(64), dek, chunk=16)
    with pytest.raises(snap.SnapshotError):
        _open_bytes(sealed + sealed[snap.HEADER_SIZE:snap.HEADER_SIZE + 32], dek)


def test_reordered_chunks_are_refused():
    dek = snap.new_dek()
    sealed = _seal_bytes(os.urandom(64), dek, chunk=16)
    h, c = snap.HEADER_SIZE, 32
    swapped = sealed[:h] + sealed[h + c:h + 2 * c] + sealed[h:h + c] + sealed[h + 2 * c:]
    with pytest.raises(snap.SnapshotError):
        _open_bytes(swapped, dek)


def test_one_snapshots_dek_does_not_open_another():
    key = snap.new_user_key(TOKEN)
    d1, d2 = snap.new_dek(), snap.new_dek()
    s2 = _seal_bytes(b"second", d2, "user:1|snapshot:8")
    with pytest.raises(snap.SnapshotError):
        _open_bytes(s2, d1, "user:1|snapshot:8")
    assert snap.unwrap_dek(snap.unwrap_private_key(TOKEN, key),
                           snap.wrap_dek(key.public_key, d2, "user:1|snapshot:8"),
                           "user:1|snapshot:8") == d2


# ---- files -------------------------------------------------------------------------------

def test_seal_and_open_files(tmp_path):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "home.tgz"
    src.write_bytes(os.urandom(200_000))
    dst = tmp_path / "7.snap"
    result = snap.seal(str(src), str(dst), key.public_key, CTX)
    assert result.plain_bytes == 200_000
    assert result.size_bytes == dst.stat().st_size
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert not (tmp_path / "7.snap.part").exists()

    out = tmp_path / "back.tgz"
    dek = snap.open_dek(TOKEN, key, result.wrapped_dek, CTX)
    assert snap.open_(str(dst), str(out), dek, CTX, expect_sha256=result.sha256) == 200_000
    assert out.read_bytes() == src.read_bytes()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_seal_refuses_over_the_cap_and_writes_nothing(tmp_path):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "home.tgz"
    src.write_bytes(b"x" * 1001)
    dst = tmp_path / "7.snap"
    with pytest.raises(snap.SnapshotError, match="cap"):
        snap.seal(str(src), str(dst), key.public_key, CTX, max_bytes=1000)
    assert not dst.exists() and not (tmp_path / "7.snap.part").exists()


def test_open_checks_the_recorded_sha256(tmp_path):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "a.tgz"
    src.write_bytes(b"abc")
    result = snap.seal(str(src), str(tmp_path / "a.snap"), key.public_key, CTX)
    dek = snap.open_dek(TOKEN, key, result.wrapped_dek, CTX)
    with pytest.raises(snap.SnapshotFormatError, match="sha256"):
        snap.open_(str(tmp_path / "a.snap"), str(tmp_path / "out"), dek, CTX, expect_sha256="0" * 64)
    assert not (tmp_path / "out").exists()


def test_failed_open_leaves_no_partial_plaintext(tmp_path):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "a.tgz"
    src.write_bytes(os.urandom(3 * snap.CHUNK_SIZE))
    result = snap.seal(str(src), str(tmp_path / "a.snap"), key.public_key, CTX)
    data = bytearray((tmp_path / "a.snap").read_bytes())
    data[-1] ^= 1                                     # break the LAST chunk: earlier ones decrypt
    (tmp_path / "a.snap").write_bytes(bytes(data))
    dek = snap.open_dek(TOKEN, key, result.wrapped_dek, CTX)
    with pytest.raises(snap.SnapshotError):
        snap.open_(str(tmp_path / "a.snap"), str(tmp_path / "out"), dek, CTX)
    assert not (tmp_path / "out").exists()


# ---- command line --------------------------------------------------------------------------

def test_cli_seal_then_open(tmp_path, monkeypatch, capsys):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "home.tgz"
    src.write_bytes(b"workspace bytes")
    res_json = tmp_path / "seal.json"
    assert snap.main(["seal", "--in", str(src), "--out", str(tmp_path / "s.snap"),
                      "--public-key", key.public_key, "--context", CTX,
                      "--result-json", str(res_json)]) == 0
    result = json.loads(res_json.read_text())
    assert stat.S_IMODE(res_json.stat().st_mode) == 0o600

    dek = snap.open_dek(TOKEN, key, result["wrapped_dek"], CTX)
    monkeypatch.setenv(snap.DEK_ENV, base64.b64encode(dek).decode())
    assert snap.main(["open", "--in", str(tmp_path / "s.snap"), "--out", str(tmp_path / "o.tgz"),
                      "--context", CTX, "--sha256", result["sha256"]]) == 0
    assert (tmp_path / "o.tgz").read_bytes() == b"workspace bytes"


def test_cli_open_without_a_dek_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(snap.DEK_ENV, raising=False)
    (tmp_path / "s.snap").write_bytes(b"x")
    assert snap.main(["open", "--in", str(tmp_path / "s.snap"), "--out", str(tmp_path / "o"),
                      "--context", CTX]) == 1
    assert "no DEK" in capsys.readouterr().err


def test_cli_open_with_wrong_dek_fails_cleanly(tmp_path, monkeypatch, capsys):
    key = snap.new_user_key(TOKEN)
    src = tmp_path / "h.tgz"
    src.write_bytes(b"data")
    snap.main(["seal", "--in", str(src), "--out", str(tmp_path / "s.snap"),
               "--public-key", key.public_key, "--context", CTX])
    capsys.readouterr()
    monkeypatch.setenv(snap.DEK_ENV, base64.b64encode(snap.new_dek()).decode())
    assert snap.main(["open", "--in", str(tmp_path / "s.snap"), "--out", str(tmp_path / "o"),
                      "--context", CTX]) == 1
    assert not (tmp_path / "o").exists()
    assert "failed" in capsys.readouterr().err
