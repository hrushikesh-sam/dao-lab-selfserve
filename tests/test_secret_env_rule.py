"""G-3: the SECRET_ENV rule in CI (F-5-1b follow-up). Every secret-looking setting must be in
config.SECRET_ENV, so `import service.config` scrubs it from os.environ before ansible-runner
can write the environment to artifacts/<run>/command. A new secret setting that someone
forgets to add fails here instead of leaking at the next deploy."""
import dataclasses
import inspect
import os
import re
import subprocess
import sys

from service import config
from tests.conftest import REPO

SECRET_LIKE = re.compile(r"secret|token|password|passphrase")
NOT_A_VALUE = ("_file", "_path", "_dir", "_days")       # a path to the secret, or a duration


def env_name(field: str) -> str:
    return "DAOLAB_" + field.upper()


def secret_fields(cls) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)
            if SECRET_LIKE.search(f.name) and not f.name.endswith(NOT_A_VALUE)]


def missing_from_secret_env(cls, secret_env) -> list[str]:
    return [env_name(n) for n in secret_fields(cls) if env_name(n) not in secret_env]


def test_every_secret_setting_is_in_secret_env():
    fields = secret_fields(config.Settings)
    assert fields, "the rule matched no field: the pattern or the Settings names changed"
    src = inspect.getsource(config.Settings.from_env)
    for n in fields:                  # the env name the rule checks is the one from_env reads
        assert f'{n}=env("{env_name(n)}"' in src, n
    assert missing_from_secret_env(config.Settings, config.SECRET_ENV) == []


def test_secret_env_is_scrubbed_at_import_in_a_fresh_interpreter():
    names = [env_name(n) for n in secret_fields(config.Settings)]
    env = {**os.environ, **{n: f"g3-canary-{i}" for i, n in enumerate(names)}}
    code = ("import os, sys; sys.path.insert(0, os.getcwd()); import service.config; "
            f"print(','.join(n for n in {names!r} if n in os.environ))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == ""            # the names left in os.environ: none


def test_canary_the_rule_flags_a_secret_setting_missing_from_secret_env():
    @dataclasses.dataclass
    class Canary:
        db_path: str = ""
        token: str = ""
        smtp_password: str = ""              # secret-like, not in the list: must be flagged
        signing_passphrase: str = ""         # likewise
        xo_token_file: str = ""              # a path: not a secret value
        token_ttl_days: int = 0              # a duration: not a secret value

    assert missing_from_secret_env(Canary, ("DAOLAB_TOKEN",)) == [
        "DAOLAB_SMTP_PASSWORD", "DAOLAB_SIGNING_PASSPHRASE"]
    assert missing_from_secret_env(
        Canary, ("DAOLAB_TOKEN", "DAOLAB_SMTP_PASSWORD", "DAOLAB_SIGNING_PASSPHRASE")) == []
