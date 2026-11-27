"""tests/dpu: the tests for the DPU-side scripts (B-109a; errata E-3).

The DPU runs Python 3.10.12, so these tests run twice:

  * in the main suite (`pytest tests/`, Python 3.12/3.13), where tests/conftest.py is loaded too;
  * on Python 3.10 with pytest alone, in the CI job `dpu-scripts`:
        python -m pytest --confcutdir=tests/dpu tests/dpu -q
    `--confcutdir` stops pytest from loading tests/conftest.py, which imports the controller
    (`service`, Python >= 3.12) and python-dotenv. Nothing here may import either.

So this file is standard library + pytest only, and it carries its own copy of the
`requires_age` marker (the same rule as tests/conftest.py, X-AGE): a marked test runs only when
age and age-keygen (DAOLAB_AGE_BIN / DAOLAB_AGE_KEYGEN_BIN, else the names on PATH) are the pinned
v1.1.1. Otherwise it is skipped, or fails when DAOLAB_REQUIRE_AGE=1. In the main suite both
copies run; they agree, so a test is skipped or failed once either way.
"""
import os
import shutil
import subprocess

import pytest

AGE_MISSING = "age v1.1.1 not installed (see deploy/age/README.md)"
_AGE_TOOLS = (("DAOLAB_AGE_BIN", "age"), ("DAOLAB_AGE_KEYGEN_BIN", "age-keygen"))
_age_ok = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_age: needs the pinned age v1.1.1 CLI, age and age-keygen "
                   "(skipped without it; fails instead when DAOLAB_REQUIRE_AGE=1)")


def age_tool(var, default):
    """The path of one age tool (the env override, else PATH), or None."""
    return shutil.which(os.environ.get(var) or default)


def _is_age_v111(path):
    if path is None:
        return False
    try:
        p = subprocess.run([path, "--version"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout == b"v1.1.1\n"


def age_installed():
    """True when the pinned age v1.1.1 is usable (checked once per test session)."""
    global _age_ok
    if _age_ok is None:
        _age_ok = all(_is_age_v111(age_tool(var, default)) for var, default in _AGE_TOOLS)
    return _age_ok


def pytest_runtest_setup(item):
    if item.get_closest_marker("requires_age") is None or age_installed():
        return
    if os.environ.get("DAOLAB_REQUIRE_AGE") != "1":
        pytest.skip(AGE_MISSING)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    # Fails in the call phase, so it is reported as a failed test, not a setup error.
    if item.get_closest_marker("requires_age") is not None and not age_installed():
        pytest.fail(f"{AGE_MISSING}, and DAOLAB_REQUIRE_AGE=1", pytrace=False)


@pytest.fixture(scope="session")
def age_bins():
    """(age, age-keygen) absolute paths; only meaningful in requires_age tests."""
    return age_tool("DAOLAB_AGE_BIN", "age"), age_tool("DAOLAB_AGE_KEYGEN_BIN", "age-keygen")
