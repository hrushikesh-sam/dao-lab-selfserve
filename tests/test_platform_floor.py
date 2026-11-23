"""G-1: the ansible-core floor. 2.20 is the design floor (F-5-3): P-0d's lookup canary
(tests/test_orphans_canary.py) was proven on 2.20.11 and 2.21.6 only, so an older
ansible-core must not install. The pin and the installed version are both checked."""
import importlib.metadata
import os
import re
import tomllib

import pytest

from tests.conftest import REPO

FLOOR = (2, 20)


def _ansible_pin():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        deps = tomllib.load(fh)["project"]["optional-dependencies"]["ansible"]
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
