"""B-50: access.keys_for — the DPU key window (plan/09 §7)."""
from datetime import timedelta

import pytest

from service import access, models
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase, mk_user

ALICE_KEY = "ssh-ed25519 AAAAalice alice@laptop"
BOB_KEY = "ssh-ed25519 AAAAbob bob@laptop"


@pytest.fixture
def lab(db):
    """Two setups (tb, other) and alice + bob with their own keys. Members are named
    ``<setup>-dpu`` / ``<setup>-tg`` (conftest.mk_setup)."""
    tb = mk_setup(db, "tb", ["wire"])
    other = mk_setup(db, "other", ["wire"])
    uc = mk_usecase(db, "uc", ["wire"])
    alice, _ = mk_user(db, "alice")
    bob, _ = mk_user(db, "bob")
    models.update(db, "users", alice, {"ssh_pubkey": ALICE_KEY})
    models.update(db, "users", bob, {"ssh_pubkey": BOB_KEY})
    return {"db": db, "tb": tb, "other": other, "uc": uc, "alice": alice, "bob": bob}


def book(lab, who="alice", setup="tb", start=T(9), end=T(11), state="active"):
    return mk_res(lab["db"], lab[setup], lab["uc"], start, end, state=state, user_id=lab[who])


def keys(lab, now, host="tb-dpu", login="lab", early=False):
    return access.keys_for(lab["db"], host, login, now, early_access=early)


def test_before_during_and_after_the_slot(lab):
    book(lab, start=T(10), end=T(12), state="ready")
    assert keys(lab, T(9, 59)) == []                                   # before
    assert keys(lab, T(10)) == [ALICE_KEY]                             # start_at is inside
    assert keys(lab, T(11, 59)) == [ALICE_KEY]
    assert keys(lab, T(12)) == []                                      # end_at is outside
    assert keys(lab, T(13)) == []                                      # after


def test_only_the_slot_owner_gets_a_key(lab):
    book(lab, "alice", start=T(9), end=T(10))
    book(lab, "bob", start=T(10, 30), end=T(12), state="ready")
    assert keys(lab, T(9, 30)) == [ALICE_KEY]
    assert keys(lab, T(10, 15)) == []                                  # the turnaround gap
    assert keys(lab, T(11)) == [BOB_KEY]


def test_a_reservation_on_another_setup_grants_nothing_here(lab):
    book(lab, setup="other")
    assert keys(lab, NOW + timedelta(minutes=30)) == []
    assert keys(lab, NOW + timedelta(minutes=30), host="other-dpu") == [ALICE_KEY]


def test_every_member_of_the_setup_is_gated_the_same_way(lab):
    book(lab)
    assert keys(lab, NOW, host="tb-tg") == [ALICE_KEY]


def test_unknown_host_or_other_login_gets_nothing(lab):
    book(lab)
    assert keys(lab, NOW, host="nope") == []
    assert keys(lab, NOW, login="root") == []
    assert keys(lab, NOW, login="alice") == []


@pytest.mark.parametrize("st", ["queued", "provisioning", "cleaning", "quarantined",
                                "released", "failed"])
def test_only_ready_or_active_grants(lab, st):
    book(lab, state=st)
    assert keys(lab, NOW) == []


def test_a_disabled_owner_loses_access_immediately(lab):
    book(lab)
    assert keys(lab, NOW) == [ALICE_KEY]
    models.update(lab["db"], "users", lab["alice"], {"enabled": 0})
    assert keys(lab, NOW) == []


def test_early_access_opens_a_ready_slot_before_start(lab):
    book(lab, start=T(10), end=T(12), state="ready")
    assert keys(lab, T(9, 50)) == []
    assert keys(lab, T(9, 50), early=True) == [ALICE_KEY]
    assert keys(lab, T(12), early=True) == []                         # still closes at end_at


def test_early_access_never_opens_a_slot_that_is_not_deployed(lab):
    book(lab, start=T(10), end=T(12), state="provisioning")
    assert keys(lab, T(9, 50), early=True) == []


def test_early_access_defaults_to_the_setting(lab, monkeypatch):
    import dataclasses
    from service import config
    book(lab, start=T(10), end=T(12), state="ready")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, early_access=True))
    assert access.keys_for(lab["db"], "tb-dpu", "lab", T(9, 50)) == [ALICE_KEY]


def test_multi_line_key_files_are_split_and_comments_dropped(lab):
    models.update(lab["db"], "users", lab["alice"],
                  {"ssh_pubkey": f"# laptop\n{ALICE_KEY}\n\nssh-rsa AAAAwork alice@work\n"})
    book(lab)
    assert keys(lab, NOW) == [ALICE_KEY, "ssh-rsa AAAAwork alice@work"]
