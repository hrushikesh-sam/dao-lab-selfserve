"""The controller platform floor (F-5-3).

G-1: the ansible-core floor. 2.20 is the design floor (F-5-3): P-0d's lookup canary
(tests/test_orphans_canary.py) was proven on 2.20.11 and 2.21.6 only, so an older
ansible-core must not install. The pin and the installed version are both checked.

B-103: the Python floor. ansible-core 2.20 needs Python 3.12, so the controller floor is
>=3.12 (F-5-3, errata E-3) and CI runs the suite on 3.12 and 3.13. DPU-side scripts stay
on Python 3.10 and are not covered here."""
import importlib.metadata
import os
import re
import sys
import tomllib

import pytest
import yaml

from tests.conftest import REPO

FLOOR = (2, 20)
PYTHON_FLOOR = (3, 12)
CI_PYTHONS = ["3.12", "3.13"]


def _pyproject():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)


def _ansible_pin():
    deps = _pyproject()["project"]["optional-dependencies"]["ansible"]
    pins = [d for d in deps if re.match(r"ansible-core\b", d)]
    assert len(pins) == 1, deps
    return pins[0]


def _version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:2])


def test_the_pyproject_pin_is_the_design_floor():
    pin = _ansible_pin()
    m = re.fullmatch(r"ansible-core\s*>=\s*([0-9.]+)", pin)
    assert m, f"expected a plain floor, got {pin!r}"
    assert _version(m.group(1)) == FLOOR, pin


def test_the_installed_ansible_core_meets_the_floor():
    try:
        installed = importlib.metadata.version("ansible-core")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("ansible-core not installed")
    assert _version(installed) >= FLOOR, installed


def test_requires_python_is_the_controller_floor():
    spec = _pyproject()["project"]["requires-python"]
    m = re.fullmatch(r">=\s*([0-9.]+)", spec)
    assert m, f"expected a plain floor, got {spec!r}"
    assert _version(m.group(1)) == PYTHON_FLOOR, spec


def test_ci_tests_on_exactly_python_3_12_and_3_13():
    with open(os.path.join(REPO, ".github", "workflows", "ci.yml")) as fh:
        job = yaml.safe_load(fh)["jobs"]["test"]
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") or {}
    # Strings, not floats: an unquoted 3.10 in YAML is the number 3.1.
    assert matrix.get("python-version") == CI_PYTHONS, matrix
    assert strategy.get("fail-fast") is False, strategy
    setup = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1, job["steps"]
    assert setup[0]["with"]["python-version"] == "${{ matrix.python-version }}", setup[0]


def test_the_running_interpreter_meets_the_floor():
    assert sys.version_info[:2] >= PYTHON_FLOOR, sys.version
