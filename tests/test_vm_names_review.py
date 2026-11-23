"""P-0c (combined), the gate review's additions to `test_vm_names_strict.py` and `test_orphans_canary.py`.

* P-0e: an unbounded id in a VM name (`daolab-r<19+ digits>-x`) made `_sweep_orphans` raise on every tick
  (int() overflows SQLite's 64-bit INTEGER at 19 digits; Python refuses int() of 4300+ digits). The id is
  now bounded to 18 ASCII digits, so such a name is dropped like any other bad name.
* The role half follows `_IDENT` (up to 128 characters): every name the platform makes is swept.
* P-0d: `orphans.yml` and `reconcile.yml` no longer read a VM name from their caller (the P-0c strict xfail,
  "a bad name passed straight in is still templated", now passes and its mark is gone). The orphan sweep hands
  orphans.yml XO VM ids in a file (`daolab_vm_ids_file`, read with lookup: data, never templated); reconcile.yml
  takes no VM list at all. `AnsibleRunner.destroy_vms` is the one producer and filters through
  `models.split_vm_ids`.
"""
import inspect
import io
import json
import pathlib
import tokenize

import pytest

from service import engine, models
from service import runner as runner_mod
from service.runner import MockRunner, RunResult
from tests.conftest import REPO
from tests.test_playbooks import ANSIBLE, Lab, _vm, clean_state, vm_uuid

BENIGN = "daolab-r998-y"                     # no reservation 998: an orphan the sweep must still destroy


def poison(canary) -> str:
    return "daolab-r999-" + "{{ lookup('pipe', 'touch " + str(canary) + "') }}"


class Recorder(MockRunner):
    """Lists the given names (each with a real-shaped XO id); records the ids it is asked to destroy."""
    def __init__(self, listed):
        super().__init__()
        self.listed, self.handed = list(listed), []

    def tagged_vms(self):
        return [models.TaggedVm(vm_uuid(n), n) for n in self.listed]

    def destroy_vms(self, ids, step):
        self.handed.append(list(ids))
        return RunResult(ok=True)


def ids(*names):
    return [vm_uuid(n) for n in names]


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
    assert rn.handed == [ids(*expected)] and out.orphan_vms == len(expected)


# ---- the role half: every name the platform makes ---------------------------------------------

def test_a_128_char_role_passes_and_its_vm_is_swept(db):
    name = "daolab-r12-" + "a" * 128                         # the longest role models.ident_ok allows
    assert models.ident_ok("a" * 128) and models.vm_name_ok(name)
    rn = Recorder([BENIGN, name])
    engine._sweep_orphans(db, rn, engine.TickResult())
    assert rn.handed == [ids(BENIGN, name)]


def test_a_129_char_role_is_refused():
    assert not models.ident_ok("a" * 129) and not models.vm_name_ok("daolab-r12-" + "a" * 129)


# ---- P-0d: no VM name travels; one producer and one reader of the ids file ---------------------

def _string_uses(path, word) -> int:
    """`word` in string literals of the code (comments and docstrings don't count)."""
    toks = list(tokenize.generate_tokens(io.StringIO(path.read_text()).readline))
    return sum(1 for i, t in enumerate(toks)
               if t.type == tokenize.STRING and word in t.string
               and not (i and toks[i - 1].type in (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL)))


def _service_uses(word):
    hits = {p.name: _string_uses(p, word) for p in (pathlib.Path(REPO) / "service").glob("*.py")}
    return {k: v for k, v in hits.items() if v}


def _playbook_readers(word):
    return sorted(p.relative_to(REPO).as_posix() for p in (pathlib.Path(REPO) / "playbooks").rglob("*.yml")
                  if word in p.read_text())


def test_the_service_never_produces_daolab_vm_names():
    assert _service_uses("daolab_vm_names") == {}


def test_the_service_has_one_producer_of_the_ids_file_and_it_filters():
    assert _service_uses("daolab_vm_ids_file") == {"runner.py": 1}
    src = inspect.getsource(runner_mod.AnsibleRunner.destroy_vms)
    assert "daolab_vm_ids_file" in src and "split_vm_ids" in src


def test_no_playbook_reads_daolab_vm_names():
    assert _playbook_readers("daolab_vm_names") == []


def test_only_orphans_yml_reads_the_ids_file():
    assert _playbook_readers("daolab_vm_ids_file") == ["playbooks/orphans.yml"]


# ---- P-0d: a playbook never evaluates what its caller passes ------------------------------------

@pytest.fixture
def xo(tmp_path):
    return Lab(tmp_path)


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")
@pytest.mark.parametrize("playbook", ["orphans.yml", "reconcile.yml"])
def test_a_playbook_never_evaluates_a_name_passed_straight_in(xo, tmp_path, playbook):
    canary = tmp_path / f"canary-{playbook}"
    st = clean_state()
    st["vms"][BENIGN] = _vm(BENIGN)
    xo.set(st)
    extra = {"reservation_id": 0} if playbook == "reconcile.yml" else {}
    xo.run(playbook, daolab_vm_names=[BENIGN, poison(canary)], **extra)
    assert not canary.exists(), f"{playbook} evaluated a name passed as an extra var"


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")
@pytest.mark.parametrize("playbook", ["orphans.yml", "reconcile.yml"])
def test_a_playbook_never_evaluates_an_id_passed_straight_in(xo, tmp_path, playbook):
    """A poisoned id in the ids file (and, for reconcile, as a list extra var) is data: never templated.
    orphans.yml still destroys the good id beside it; reconcile.yml takes no VM list, so BENIGN stays."""
    canary = tmp_path / f"canary-id-{playbook}"
    st = clean_state()
    st["vms"][BENIGN] = _vm(BENIGN)
    xo.set(st)
    payload = [vm_uuid(BENIGN), "{{ lookup('pipe', 'touch " + str(canary) + "') }}"]
    ids_file = tmp_path / "vm-ids.json"
    ids_file.write_text(json.dumps(payload))
    extra = {"daolab_vm_ids_file": str(ids_file)}
    if playbook == "reconcile.yml":
        extra.update(reservation_id=0, daolab_vm_ids=payload)
    assert xo.run(playbook, **extra) == 0, xo.stdout[-1500:]
    assert not canary.exists(), f"{playbook} evaluated an id passed by its caller"
    left = sorted(xo.state()["vms"])
    assert left == ([] if playbook == "orphans.yml" else [BENIGN]), xo.stdout[-1500:]
