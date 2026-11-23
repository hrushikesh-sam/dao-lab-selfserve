"""F-5-1b: the secrets must not stay in the process environment.

`service/config.py` calls `load_dotenv()`, which copies every `.env` value into `os.environ`, and
ansible-runner writes the WHOLE process environment to `artifacts/<ident>/command` at the start of
every run (ansible_runner/runner.py, status_callback 'starting'; there is no switch for it). So
with F-5-1 alone, DAOLAB_INTERNAL_SECRET still reached disk on every run of a deployment that keeps
it in `.env` (or in a systemd EnvironmentFile=): 1540 `command` files on a showcase laptop.

The values are read into `config.settings` once; nothing reads them from the environment again
(child processes get files: F-5-1's secret file, DAOLAB_XO_TOKEN_FILE, the become-password file).
So config drops them from `os.environ` right after reading them.
"""
import os
import subprocess
import sys

from service import config
from tests import test_runner_secret as t
from tests.conftest import REPO
from tests.test_runner_secret import rt  # noqa: F401  (fixture)

SECRETS = ("DAOLAB_INTERNAL_SECRET", "DAOLAB_AUTHZ_SECRET", "DAOLAB_TOKEN")
PROBE = ("import os; from service import config; s = config.settings; "
         "print(sorted(k for k in %r if k in os.environ), s.internal_secret, s.authz_secret, s.token)")


def _child(cwd, env):
    p = subprocess.run([sys.executable, "-c", PROBE % (SECRETS,)], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout.split()


def _base_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("DAOLAB_")}
    env["PYTHONPATH"] = REPO
    return env


def test_secrets_from_the_real_environment_are_read_then_dropped(tmp_path):
    env = {**_base_env(), "DAOLAB_INTERNAL_SECRET": "i-1", "DAOLAB_AUTHZ_SECRET": "a-1",
           "DAOLAB_TOKEN": "t-1"}
    assert _child(tmp_path, env) == ["[]", "i-1", "a-1", "t-1"]


def test_secrets_from_a_dotenv_file_are_read_then_dropped(tmp_path):
    (tmp_path / ".env").write_text("DAOLAB_INTERNAL_SECRET=i-2\nDAOLAB_AUTHZ_SECRET=a-2\nDAOLAB_TOKEN=t-2\n")
    assert _child(tmp_path, _base_env()) == ["[]", "i-2", "a-2", "t-2"]


def test_other_settings_stay_in_the_environment(tmp_path):
    env = {**_base_env(), "DAOLAB_XO_TOKEN_FILE": "/x/token", "DAOLAB_INTERNAL_SECRET": "i-3"}
    p = subprocess.run([sys.executable, "-c", "import os; from service import config; "
                        "print(os.environ.get('DAOLAB_XO_TOKEN_FILE'))"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert p.stdout.strip() == "/x/token"                  # a path the daolab-xo child needs


def test_the_real_runner_leaves_no_trace_when_the_secret_came_from_the_environment(tmp_path, rt, monkeypatch):
    monkeypatch.setenv("DAOLAB_INTERNAL_SECRET", t.CANARY)     # what load_dotenv() / systemd did
    config.scrub_secret_env()                                  # what config does at import
    assert "DAOLAB_INTERNAL_SECRET" not in os.environ
    t.test_the_real_runner_persists_no_trace_of_the_secret(tmp_path, rt)
