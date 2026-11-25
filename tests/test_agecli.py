"""B-105: the only wrapper that runs the pinned age CLI. Key material is confined to
stdin/stdout pipes; argv, env and exceptions carry no identity text."""
import subprocess
import types

import pytest

from service import agecli, config

CANARY = "AGE-SECRET-KEY-1" + "Q" * 58
RECIPIENT = "age1" + "q" * 58
WRONG_RECIPIENT = "age1" + "z" * 58


def done(rc=0, out=b"", err=b""):
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def fake_age(monkeypatch):
    calls = []
    state = {"identity": (CANARY + "\n").encode(), "recipient": (RECIPIENT + "\n").encode(),
             "plain": b"snapshot-bytes", "cipher": b"age-encrypted"}
    age, keygen = config.settings.age_bin, config.settings.age_keygen_bin

    def run(argv, **kw):
        args = list(argv)
        calls.append({"argv": args, "kw": kw})
        if "--version" in args:
            return done(out=b"v1.1.1\n")
        if args == [keygen]:
            return done(out=state["identity"], err=b"Public key: " + WRONG_RECIPIENT.encode() + b"\n")
        if args[:3] == [keygen, "-y", "-"]:
            assert kw.get("input") == state["identity"]
            return done(out=state["recipient"])
        if args[:2] == [age, "-r"]:
            return done(out=state["cipher"])
        if args[:4] == [age, "-d", "-i", "-"]:
            return done(out=state["plain"])
        raise AssertionError(f"unexpected age call: {args}")

    monkeypatch.setattr(agecli.subprocess, "run", run)
    return calls, state


def call_args(calls):
    return [(c["argv"], c["kw"].get("env")) for c in calls]


def test_shapes_match_the_ddl():
    assert agecli.RECIPIENT_RE.fullmatch(RECIPIENT)
    assert agecli.IDENTITY_RE.fullmatch(CANARY)
    assert not agecli.RECIPIENT_RE.fullmatch("age1" + "q" * 57)
    assert not agecli.IDENTITY_RE.fullmatch("age-secret-key-1" + "Q" * 58)


def test_version_checks_both_binaries_and_refuses_a_wrong_age_keygen(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(list(argv))
        if argv[0] == config.settings.age_keygen_bin:
            return done(out=b"age-keygen v1.1.1\n")
        return done(out=b"age v1.1.1\n")

    monkeypatch.setattr(agecli.subprocess, "run", run)
    assert agecli.version() == "v1.1.1"
    assert seen == [[config.settings.age_bin, "--version"],
                    [config.settings.age_keygen_bin, "--version"]]

    def wrong_keygen(argv, **kw):
        if argv[0] == config.settings.age_keygen_bin:
            return done(out=b"age-keygen v1.2.0\n")
        return done(out=b"age v1.1.1\n")

    monkeypatch.setattr(agecli.subprocess, "run", wrong_keygen)
    with pytest.raises(agecli.AgeError) as bad:
        agecli.version()
    assert bad.value.message == agecli.UNSUPPORTED_VERSION
    assert bad.value.rc == 0
    assert "1.2.0" not in str(bad.value)


def test_keygen_parses_the_recipient_from_stdin_not_stderr(fake_age):
    calls, state = fake_age
    identity, recipient = agecli.keygen()
    assert identity == state["identity"]
    assert recipient == RECIPIENT
    assert "Public key" not in recipient and WRONG_RECIPIENT not in recipient
    assert CANARY not in repr(call_args(calls))
    assert [c["argv"][:3] for c in calls if "-y" in c["argv"]] == [
        [config.settings.age_keygen_bin, "-y", "-"]]
    assert all(c["kw"].get("env") is None for c in calls)
    assert all(isinstance(c["argv"], list) and not c["kw"].get("shell") for c in calls)


def test_keygen_finds_the_identity_in_real_multiline_output(monkeypatch):
    output = (b"# created: 2026-09-01T09:00:00Z\n"
              b"# public key: " + RECIPIENT.encode() + b"\n" +
              (CANARY + "\n").encode())
    calls = []

    def run(argv, **kw):
        args = list(argv)
        calls.append({"argv": args, "kw": kw})
        if "--version" in args:
            return done(out=b"age v1.1.1\n")
        if args == [config.settings.age_keygen_bin]:
            return done(out=output, err=b"Public key: " + WRONG_RECIPIENT.encode() + b"\n")
        if args[:3] == [config.settings.age_keygen_bin, "-y", "-"]:
            assert kw.get("input") == (CANARY + "\n").encode()
            return done(out=(RECIPIENT + "\n").encode())
        raise AssertionError(f"unexpected age call: {args}")

    monkeypatch.setattr(agecli.subprocess, "run", run)
    identity, recipient = agecli.keygen()
    assert identity == (CANARY + "\n").encode()
    assert recipient == RECIPIENT
    assert b"#" not in identity and WRONG_RECIPIENT.encode() not in identity
    assert CANARY not in repr([(c["argv"], c["kw"].get("env")) for c in calls])


def test_parsers_accept_comments_around_key_material(fake_age):
    _calls, state = fake_age
    commented = (b"# created: 2026-09-01T09:00:00Z\n"
                 b"# public key: " + RECIPIENT.encode() + b"\n" +
                 (CANARY + "\n").encode())
    assert agecli._identity(commented) == (CANARY + "\n").encode()
    state["recipient"] = b"# public key:\n" + (RECIPIENT + "\n").encode()
    assert agecli.recipient_of(commented) == RECIPIENT


def test_encrypt_decrypt_round_trip_uses_pipes_only(fake_age, tmp_path):
    calls, state = fake_age
    cipher = agecli.encrypt_to(RECIPIENT, state["plain"])
    assert cipher == state["cipher"]
    path = tmp_path / "snapshot.age"
    path.write_bytes(cipher)
    assert agecli.decrypt_file((CANARY + "\n").encode(), str(path)) == state["plain"]
    assert CANARY not in repr(call_args(calls))
    decrypt = [c for c in calls if "-d" in c["argv"]][0]
    assert decrypt["argv"] == [config.settings.age_bin, "-d", "-i", "-", str(path)]
    assert decrypt["kw"]["input"].strip() == CANARY.encode()
    assert decrypt["kw"].get("env") is None


def test_encrypt_file_to_passes_the_fd_not_the_plaintext(fake_age, tmp_path):
    calls, state = fake_age
    src = tmp_path / "plain.tar"
    src.write_bytes(state["plain"])
    dst = tmp_path / "out.age"
    with open(src, "rb") as fh:
        src_fd = fh.fileno()
        agecli.encrypt_file_to(RECIPIENT, src_fd, str(dst))
    encrypt = [c for c in calls if "-o" in c["argv"]][0]
    assert encrypt["argv"] == [config.settings.age_bin, "-r", RECIPIENT, "-o", str(dst)]
    assert encrypt["kw"]["stdin"] == src_fd
    assert "input" not in encrypt["kw"]
    assert CANARY not in repr(call_args(calls))


def test_bad_shapes_are_refused_before_any_second_call(fake_age):
    calls, state = fake_age
    with pytest.raises(agecli.AgeError) as bad_recipient:
        agecli.encrypt_to("age1short", b"x")
    assert bad_recipient.value.message == agecli.INVALID_RECIPIENT
    with pytest.raises(agecli.AgeError) as bad_identity:
        agecli.recipient_of(b"not-an-identity")
    assert bad_identity.value.message == agecli.INVALID_IDENTITY
    assert calls == []

    state["recipient"] = b"age1notavalidrecipient\n"
    with pytest.raises(agecli.AgeError) as bad_output:
        agecli.recipient_of((CANARY + "\n").encode())
    assert bad_output.value.message == agecli.INVALID_OUTPUT


def test_age_error_carries_rc_but_never_stderr(monkeypatch):
    def run(argv, **kw):
        if "--version" in argv:
            return done(out=b"v1.1.1\n")
        return done(1, b"", ("failed: " + CANARY + "\n").encode())

    monkeypatch.setattr(agecli.subprocess, "run", run)
    for call in (lambda: agecli.keygen(), lambda: agecli.encrypt_to(RECIPIENT, b"x"),
                 lambda: agecli.decrypt_file((CANARY + "\n").encode(), "x.age")):
        with pytest.raises(agecli.AgeError) as exc:
            call()
        assert exc.value.rc == 1
        assert CANARY not in str(exc.value)
        assert exc.value.message in agecli.FIXED_MESSAGES


def test_run_times_out_with_the_calls_fixed_message(monkeypatch):
    seen = []

    def hang(argv, **kw):
        seen.append(kw)
        raise subprocess.TimeoutExpired(argv, 60, output=CANARY.encode(),
                                        stderr=b"leaked stderr " + CANARY.encode())

    monkeypatch.setattr(agecli.subprocess, "run", hang)
    with pytest.raises(agecli.AgeError) as timed_out:
        agecli._run([config.settings.age_bin, "-r", RECIPIENT], agecli.ENCRYPT_FAILED,
                    input=CANARY.encode())
    assert timed_out.value.message == agecli.ENCRYPT_FAILED
    assert timed_out.value.rc is None
    assert CANARY not in str(timed_out.value) and "leaked stderr" not in str(timed_out.value)
    assert seen[0]["timeout"] == 60


@pytest.mark.requires_age
def test_real_age_round_trip(tmp_path):
    identity, recipient = agecli.keygen()
    assert agecli.RECIPIENT_RE.fullmatch(recipient)
    assert identity.count(b"\n") == 1 and b"#" not in identity
    assert identity.strip().decode().startswith("AGE-SECRET-KEY-1")
    plain = b"three lines\nof real payload\n"
    age_path = tmp_path / "sealed.age"
    src_path = tmp_path / "plain"
    src_path.write_bytes(plain)
    with open(src_path, "rb") as fh:
        agecli.encrypt_file_to(recipient, fh.fileno(), str(age_path))
    assert agecli.decrypt_file(identity, str(age_path)) == plain
    assert agecli.decrypt_file(identity + b"\n", str(age_path)) == plain


@pytest.mark.requires_age
def test_real_keygen_matches_the_keys_table_check():
    _identity, recipient = agecli.keygen()
    assert len(recipient) == 62 and recipient.startswith("age1")
    assert agecli.RECIPIENT_RE.fullmatch(recipient)
