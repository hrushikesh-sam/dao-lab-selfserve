"""P-0c (combined), the gate review's additions to `test_vm_names_strict.py` and `test_orphans_canary.py`.

* P-0e: an unbounded id in a VM name (`daolab-r<19+ digits>-x`) made `_sweep_orphans` raise on every tick
  (int() overflows SQLite's 64-bit INTEGER at 19 digits; Python refuses int() of 4300+ digits). The id is
  now bounded to 18 ASCII digits, so such a name is dropped like any other bad name.
* The role half follows `_IDENT` (up to 128 characters): every name the platform makes is swept.
* `orphans.yml` and `reconcile.yml` trust their caller: a bad name passed straight in as the extra var is
  still templated (strict xfail). The service has exactly one producer of that extra var
  (`AnsibleRunner.destroy_vms`, which filters through `models.split_vm_names`), and the playbooks that
  read it are known. P-0d (destroy by XO VM id) should turn the xfail into a pass; then drop it.
"""
import inspect
import io
import pathlib
import tokenize

import pytest

from service import engine, models
from service import runner as runner_mod
from service.runner import MockRunner, RunResult
from tests.conftest import REPO
from tests.test_playbooks import ANSIBLE, Lab, _vm, clean_state

BENIGN = "daolab-r998-y"                     # no reservation 998: an orphan the sweep must still destroy


def poison(canary) -> str:
    return "daolab-r999-" + "{{ lookup('pipe', 'touch " + str(canary) + "') }}"


class Recorder(MockRunner):
    def __init__(self, listed):
        super().__init__()
        self.listed, self.handed = list(listed), []

    def tagged_vms(self):
        return list(self.listed)

    def destroy_vms(self, names, step):
        self.handed.append(list(names))
        return RunResult(ok=True)


# ---- P-0e: the id is bounded ----------------------------------------------------------------

def test_an_18_digit_id_fits_sqlite_and_is_valid():
    assert int("9" * 18) < 2 ** 63
    assert models.vm_name_ok("daolab-r" + "9" * 18 + "-x")


@pytest.mark.parametrize("digits", [19, 20, 5000], ids=lambda d: f"{d}-digit-id")
def test_a_long_id_is_refused(digits):
    assert not models.vm_name_ok("daolab-r" + "9" * digits + "-x")


@pytest.mark.parametrize("digits", [18, 19, 5000], ids=lambda d: f"{d}-digit-id")
def test_a_long_id_never_breaks_the_sweep(db, digits):
    """Before P-0e the 19- and 5000-digit cases raised OverflowError / ValueError out of the sweep."""
    rn = Recorder([BENIGN, "daolab-r" + "9" * digits + "-x"])
    out = engine.TickResult()
    engine._sweep_orphans(db, rn, out)                       # must not raise
    expected = [BENIGN] if digits > 18 else [BENIGN, "daolab-r" + "9" * digits + "-x"]
    assert rn.handed == [expected] and out.orphan_vms == len(expected)


# ---- the role half: every name the platform makes ---------------------------------------------

def test_a_128_char_role_passes_and_its_vm_is_swept(db):
    name = "daolab-r12-" + "a" * 128                         # the longest role models.ident_ok allows
    assert models.ident_ok("a" * 128) and models.vm_name_ok(name)
    rn = Recorder([BENIGN, name])
    engine._sweep_orphans(db, rn, engine.TickResult())
    assert rn.handed == [[BENIGN, name]]


def test_a_129_char_role_is_refused():
    assert not models.ident_ok("a" * 129) and not models.vm_name_ok("daolab-r12-" + "a" * 129)


# ---- one producer, known readers ---------------------------------------------------------------

def _string_uses(path) -> int:
    """`daolab_vm_names` in string literals of the code (comments and docstrings don't count)."""
    toks = list(tokenize.generate_tokens(io.StringIO(path.read_text()).readline))
    return sum(1 for i, t in enumerate(toks)
               if t.type == tokenize.STRING and "daolab_vm_names" in t.string
               and not (i and toks[i - 1].type in (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL)))


def test_the_service_has_one_producer_of_daolab_vm_names_and_it_filters():
    hits = {p.name: _string_uses(p) for p in (pathlib.Path(REPO) / "service").glob("*.py")}
    assert {k: v for k, v in hits.items() if v} == {"runner.py": 1}
    src = inspect.getsource(runner_mod.AnsibleRunner.destroy_vms)
    assert "daolab_vm_names" in src and "split_vm_names" in src


def test_the_playbooks_that_read_daolab_vm_names_are_known():
    readers = sorted(str(p.relative_to(REPO)) for p in (pathlib.Path(REPO) / "playbooks").rglob("*.yml")
                     if "daolab_vm_names" in p.read_text())
    assert readers == ["playbooks/orphans.yml", "playbooks/reconcile.yml"]


# ---- the documented limit: a playbook trusts its caller ----------------------------------------

@pytest.fixture
def xo(tmp_path):
    return Lab(tmp_path)


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Known limit of P-0c: the playbook trusts its caller, so a bad name passed straight in as the "
    "daolab_vm_names extra var is still templated. The only producer, AnsibleRunner.destroy_vms, filters "
    "such names. Destroying by XO VM id (P-0d) should make this pass: then drop the xfail."))
@pytest.mark.parametrize("playbook", ["orphans.yml", "reconcile.yml"])
def test_a_playbook_never_evaluates_a_name_passed_straight_in(xo, tmp_path, playbook):
    canary = tmp_path / f"canary-{playbook}"
    st = clean_state()
    st["vms"][BENIGN] = _vm(BENIGN)
    xo.set(st)
    extra = {"reservation_id": 0} if playbook == "reconcile.yml" else {}
    xo.run(playbook, daolab_vm_names=[BENIGN, poison(canary)], **extra)
    assert not canary.exists(), f"{playbook} evaluated a name passed as an extra var"
