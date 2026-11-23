"""F-5-1: DAOLAB_INTERNAL_SECRET must not be persisted by ansible-runner.

ansible-runner writes `extravars` to <private_data_dir>/env/extravars and the process environment
and command line to artifacts/, none of it removed at the end of a run. So the runner no longer
puts the secret in extravars or the environment: it writes it to a 0600 file for the duration of
the run and passes only the PATH (`daolab_internal_secret_file`); playbooks/group_vars/all.yml
defines `daolab_internal_secret` as a lazy lookup of that file.
"""
import dataclasses
import os
import shutil
import stat
import types

import pytest

from service import config, runner
from service.runner import AnsibleRunner
from tests.conftest import REPO

CANARY = "canary-F51-0123456789abcdef-secret"
RES = {"id": 1, "job_id": 5, "job_created_at": "2026-09-01T09:00:00Z", "usecase": "u", "setup": "s",
       "user": "alice", "ssh_pubkey": "", "hw_class": "", "diy": False}


def steps(name, state="ok", detail=None):
    pass


def settings(tmp_path, **over):
    base = dict(artifacts_dir=str(tmp_path / "jobs"), playbooks_dir=os.path.join(REPO, "playbooks"),
                inventory=os.path.join(REPO, "inventory", "hosts.yml"), api_url="http://127.0.0.1:1",
                internal_secret=CANARY, otel_endpoint="", job_timeout_sec=120)
    return dataclasses.replace(config.settings, **{**base, **over})


@pytest.fixture
def rt(tmp_path, monkeypatch):
    d = tmp_path / "rt"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("DAOLAB_RUNTIME_DIR", str(d))
    return d


def secret_files(d):
    return [n for n in os.listdir(d) if n.startswith(runner.SECRET_FILE_PREFIX)]


def test_extravars_never_carry_the_secret(tmp_path):
    assert "daolab_internal_secret" not in AnsibleRunner(settings(tmp_path)).extravars(RES)


def test_the_secret_is_a_0600_file_for_the_run_only(tmp_path, rt):
    seen = {}

    def fake_run(**kw):
        path = kw["extravars"]["daolab_internal_secret_file"]
        seen.update(path=path, mode=stat.S_IMODE(os.stat(path).st_mode), text=open(path).read(),
                    blob=repr([kw["extravars"], kw["envvars"], kw.get("cmdline")]))
        return types.SimpleNamespace(status="successful", rc=0)

    assert AnsibleRunner(settings(tmp_path), run=fake_run).deploy(RES, steps).ok
    assert seen["mode"] == 0o600 and seen["text"] == CANARY
    assert os.path.dirname(seen["path"]) == str(rt)
    assert CANARY not in seen["blob"]
    assert not os.path.exists(seen["path"]) and secret_files(rt) == []


def test_the_file_is_removed_when_the_run_raises(tmp_path, rt):
    def boom(**kw):
        raise RuntimeError("ansible-runner blew up")

    with pytest.raises(RuntimeError):
        AnsibleRunner(settings(tmp_path), run=boom).deploy(RES, steps)
    assert secret_files(rt) == []


def test_no_secret_configured_means_no_file_and_no_variable(tmp_path, rt):
    seen = {}

    def fake_run(**kw):
        seen["ev"] = dict(kw["extravars"])
        return types.SimpleNamespace(status="successful", rc=0)

    AnsibleRunner(settings(tmp_path, internal_secret=""), run=fake_run).deploy(RES, steps)
    assert "daolab_internal_secret_file" not in seen["ev"] and secret_files(rt) == []


def test_a_stale_secret_file_from_a_killed_run_is_swept_a_fresh_one_is_not(tmp_path, rt):
    old, fresh = rt / (runner.SECRET_FILE_PREFIX + "old.txt"), rt / (runner.SECRET_FILE_PREFIX + "new.txt")
    for f in (old, fresh):
        f.write_text("x")
    os.utime(old, (1, 1))
    AnsibleRunner(settings(tmp_path))
    assert not old.exists() and fresh.exists()


# ---- the real ansible-runner: nothing persisted contains the secret -------------------------------

def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "group_vars").mkdir(parents=True)
    (proj / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    shutil.copy(os.path.join(REPO, "playbooks", "group_vars", "all.yml"), proj / "group_vars" / "all.yml")
    (proj / "inv.yml").write_text(
        "all:\n  hosts:\n    localhost:\n      ansible_connection: local\n"
        "      ansible_python_interpreter: '{{ ansible_playbook_python }}'\n")
    (proj / "deploy.yml").write_text(
        "- hosts: localhost\n  gather_facts: false\n  tasks:\n"
        # a use of the secret like the real uri tasks: rendered, no_log
        "    - ansible.builtin.debug:\n        msg: \"{{ daolab_internal_secret }}\"\n      no_log: true\n"
        # proof that the secret reached the play (only its length is written)
        "    - ansible.builtin.copy:\n        dest: \"{{ daolab_output_file }}.seen\"\n"
        "        content: \"{{ daolab_internal_secret | length }}\"\n")
    return proj


def test_the_real_runner_persists_no_trace_of_the_secret(tmp_path, rt):
    pytest.importorskip("ansible_runner")
    if not runner.ansible_playbook_path():
        pytest.skip("ansible-playbook not installed")
    proj = _project(tmp_path)
    s = settings(tmp_path, playbooks_dir=str(proj), inventory=str(proj / "inv.yml"))
    result = AnsibleRunner(s).deploy(RES, steps)
    assert result.ok, result.error
    seen = [os.path.join(d, f) for d, _, fs in os.walk(s.artifacts_dir) for f in fs if f.endswith(".seen")]
    assert len(seen) == 1 and open(seen[0]).read() == str(len(CANARY)), "the secret did not reach the play"
    hits, files = [], 0
    for d, _, fs in os.walk(s.artifacts_dir):
        for f in fs:
            files += 1
            with open(os.path.join(d, f), "rb") as fh:
                if CANARY.encode() in fh.read():
                    hits.append(os.path.relpath(os.path.join(d, f), s.artifacts_dir))
    assert files > 3 and hits == [], f"the secret was persisted in: {hits}"
    assert secret_files(rt) == []
