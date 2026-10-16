"""B-80: playbooks/files/daolab-seed — the template-side half of host_vm's cloud-init seed:
xenstore vm-data/daolab/* (base64) -> /var/lib/cloud/seed/nocloud/ + the otel agent config."""
import base64
import os
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "playbooks", "files", "daolab-seed")


@pytest.fixture
def guest(tmp_path):
    """A fake guest: xenstore-read answers from a dict written to a file."""
    store = tmp_path / "xenstore"
    store.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "xenstore-read"
    fake.write_text(f'#!/bin/sh\nf="{store}/$(echo "$1" | tr / _)"\n[ -f "$f" ] || exit 1\ncat "$f"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def put(key, text):
        (store / key.replace("/", "_")).write_text(base64.b64encode(text.encode()).decode())

    def run():
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
               "DAOLAB_SEED_DIR": str(tmp_path / "seed"), "DAOLAB_ETC_DIR": str(tmp_path / "etc")}
        return subprocess.run([SCRIPT], env=env, capture_output=True, text=True)

    return tmp_path, put, run


def test_the_seed_becomes_a_nocloud_datasource_and_the_agent_config(guest):
    root, put, run = guest
    put("vm-data/daolab/user-data", "#cloud-config\nhostname: daolab-r12-sink\n")
    put("vm-data/daolab/network-config", "version: 2\n")
    put("vm-data/daolab/meta-data", "instance-id: daolab-r12-sink-10.30.0.100\n")
    put("vm-data/daolab/otel", "receivers: {}\n")
    assert run().returncode == 0
    seed = root / "seed"
    assert (seed / "user-data").read_text().startswith("#cloud-config")
    assert (seed / "network-config").read_text() == "version: 2\n"
    assert "instance-id" in (seed / "meta-data").read_text()
    assert oct((seed / "user-data").stat().st_mode & 0o777) == "0o600"
    assert (root / "etc" / "otelcol.yaml").read_text() == "receivers: {}\n"


def test_no_seed_means_not_a_daolab_vm_and_nothing_happens(guest):
    root, _, run = guest
    assert run().returncode == 0
    assert not (root / "seed").exists() and not (root / "etc").exists()


def test_without_a_collector_no_agent_config_is_written(guest):
    root, put, run = guest
    for key in ("user-data", "network-config", "meta-data"):
        put(f"vm-data/daolab/{key}", "x: 1\n")
    put("vm-data/daolab/otel", "")
    assert run().returncode == 0
    assert (root / "seed" / "user-data").exists() and not (root / "etc" / "otelcol.yaml").exists()
