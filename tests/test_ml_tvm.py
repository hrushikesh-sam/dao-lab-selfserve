"""X-ML: the ml-tvm use case (ML inference on the OCTEON 10 MLIP, DPU only).

The catalog row, the ML device's one inventory address (Q-ROLES: bdf.ml), the host paths the
ml-tvm playbooks and verify_clean touch (each a variable whose default is the board's value,
so no test ever reads or writes the test machine's /proc, /dev/vfio, /var/run/dpdk or hugepage
mounts), and the deploy -> verify_clean -> cleanup round trip through the fake lab.
"""
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from service import catalog, config, models, runner
from tests.test_app_exporter import exp, series
from tests.test_catalog import INV, ST_DIR, UC_DIR, conn  # noqa: F401 (fixture)
from tests.test_playbooks import ANSIBLE, REPO, app_run, board, books, lab  # noqa: F401 (fixtures)

needs_ansible = pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not installed")

PCI = "0000:00:10.0"
GROUP = "98"
MODEL = "resnet50_fp16_t08_b01"
ML_ROLES = {"infer": {"on": "dpu", "app": "ml-tvm"}}
PLAYBOOKS = os.path.join(REPO, "playbooks")
SMOKE = os.path.join(PLAYBOOKS, "files", "daolab-ml-smoke")
HOST_VARS = os.path.join(REPO, "inventory", "host_vars", "dpu-01.yml")
ML_PLAYBOOKS = ("apps/ml-tvm/deploy.yml", "apps/ml-tvm/cleanup.yml", "verify_clean.yml",
                "probe.yml")


def _read(rel):
    with open(os.path.join(PLAYBOOKS, rel), encoding="utf-8") as fh:
        return fh.read()


# ---- the catalog ------------------------------------------------------------------------

def test_the_catalog_loads_ml_tvm():
    ucs, errors = catalog.load_usecases(UC_DIR)
    assert errors == []
    ml = {u["name"]: u for u in ucs}["ml-tvm"]
    assert ml["requires"] == ["octeon-cn106", "mlip"] and ml["hw_class"] is None
    assert ml["roles"] == ML_ROLES                                    # DPU only: no VM role
    assert ml["verify"]["from"] == "infer"
    assert not any(d in ml["verify"]["cmd"] for d in ("{{", "{%", "{#"))     # C-1 / G-2
    assert {r["name"] for r in ml["health"]} == {"ml_device", "ml_smoke_ok"}


def test_sync_turns_the_seeded_ml_tvm_row_into_the_yaml_backed_row(conn):
    seeded = models.usecase_by_name(conn, "ml-tvm")
    assert seeded["yaml_path"] is None                                # "coming soon" before
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    rows = conn.execute("SELECT * FROM usecases WHERE name = 'ml-tvm'").fetchall()
    assert len(rows) == 1                                             # updated, not duplicated
    row = rows[0]
    assert row["id"] == seeded["id"]
    assert row["yaml_path"].endswith(os.path.join("usecases", "ml-tvm.yml"))
    assert row["description"] == seeded["description"]                # seeded text preserved
    assert models.from_json(row["roles"]) == ML_ROLES
    tb01 = models.setup_by_name(conn, "tb-01")
    assert set(models.from_json(row["requires"])) <= set(models.from_json(tb01["capabilities"]))


@pytest.mark.parametrize("deployable,expected", [
    (("vpp", "smartnic", "tls-proxy-nginx", "ml-tvm"), True),        # the showcase default now
    (("vpp", "smartnic", "tls-proxy-nginx"), False),                  # not listed: coming soon
    ((), True),                                                       # DAOLAB_DEPLOYABLE unset
])
def test_ml_tvm_is_deployable_when_daolab_deployable_lists_it(conn, monkeypatch, deployable,
                                                              expected):
    from service import api
    catalog.sync(conn, UC_DIR, ST_DIR, INV)
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, deployable=deployable))
    assert api._deployable(models.usecase_by_name(conn, "ml-tvm")) is expected


def test_the_mock_exporter_serves_the_series_the_ml_health_rules_read():
    ucs, _ = catalog.load_usecases(UC_DIR)
    ml = {u["name"]: u for u in ucs}["ml-tvm"]
    wanted = {r["promql"].split("{", 1)[0] for r in ml["health"]}
    assert wanted == {"ml_up", "ml_last_smoke_ok"}
    assert wanted <= {name for name, _ in runner.MockExporter.GAUGES}


def test_ml_tvm_residue_is_an_existing_kind():
    assert runner.parse_residue("dpu_app:ml-tvm") == ("dpu_app", "ml-tvm")
    assert "dpu_app" in runner.RESIDUE_KINDS


NEW_VARS = ("ml_pci", "ml_fw", "ml_lcores", "ml_file_prefix", "ml_hugepage_kb",
            "ml_hugepages_extra", "ml_smoke_dir", "ml_smoke_model", "ml_state_dir", "ml_smoke_bin",
            "ml_env_file", "ml_test_mldev", "daolab_procfs", "daolab_vfio_dir",
            "daolab_dpdk_run_dir", "bdf")


def test_no_new_ml_var_is_a_runner_extravar():
    """Extravar matrix: every ml-tvm input is inventory (admin); the runner passes none."""
    import inspect
    src = inspect.getsource(runner.AnsibleRunner)
    assert [v for v in NEW_VARS if re.search(rf"[\"']{v}[\"']", src)] == []


@pytest.mark.parametrize("rel", ["apps/ml-tvm/deploy.yml", "apps/ml-tvm/cleanup.yml",
                                 "files/daolab-ml-smoke", "verify_clean.yml", "probe.yml"])
def test_no_secret_reaches_the_ml_tvm_files(rel):
    """F-5-1: the ml-tvm playbooks and the smoke test reference no secret."""
    assert not re.search(r"secret|token|password", _read(rel), re.I)


# ---- Q-ROLES: the ML device has one address, bdf.ml -------------------------------------

def test_the_inventory_names_the_ml_device_role():
    with open(HOST_VARS, encoding="utf-8") as fh:
        hv = yaml.safe_load(fh)
    assert hv["bdf"]["ml"] == PCI
    assert {"sdp_pf", "cpt_pf", "cpt_vfs", "ml"} <= set(hv["bdf"])
    assert hv["ml_pci"] == "{{ bdf.ml }}"                             # the playbooks' name


ML_FALLBACK = "{{ ml_pci | default(bdf.ml | default('')) }}"      # verify_clean.yml, probe.yml


@needs_ansible
@pytest.mark.parametrize("expr,drop_ml_pci", [
    ("{{ ml_pci }}", False),                                         # the app playbooks
    (ML_FALLBACK, False),                                            # verify_clean / probe
    (ML_FALLBACK, True),                                             # ... falling back to bdf.ml
])
def test_ml_pci_resolves_to_the_ml_role(tmp_path, expr, drop_ml_pci):
    """Through ansible itself, with dpu-01's real host_vars beside a local-only inventory (no
    address, nothing is contacted)."""
    inv = tmp_path / "inv"
    (inv / "host_vars").mkdir(parents=True)
    with open(HOST_VARS, encoding="utf-8") as fh:
        hv = fh.read()
    if drop_ml_pci:
        hv = re.sub(r"(?m)^ml_pci:.*$", "", hv)
        assert "ml_pci:" not in hv
    (inv / "host_vars" / "dpu-01.yml").write_text(hv)
    (inv / "hosts.json").write_text(json.dumps({"all": {"hosts": {"dpu-01": {
        "ansible_connection": "local",
        "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}))
    out = tmp_path / "ml_pci.txt"
    play = tmp_path / "resolve.yml"
    play.write_text(yaml.safe_dump([{"hosts": "dpu-01", "gather_facts": False, "tasks": [
        {"ansible.builtin.copy": {"content": expr, "dest": str(out)}}]}]))
    env = {**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"),
           "ANSIBLE_HOME": str(tmp_path / "ansible-home"), "ANSIBLE_NOCOLOR": "1"}
    p = subprocess.run([ANSIBLE, "-i", str(inv / "hosts.json"), str(play)], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (p.stdout + p.stderr)[-2000:]
    assert out.read_text() == PCI


def test_every_ml_address_fallback_is_the_inventory_role():
    """Q-C: the exporter's and the smoke test's own defaults equal bdf.ml, and the playbooks
    fall back to bdf.ml itself, never to a second copy of the address."""
    with open(HOST_VARS, encoding="utf-8") as fh:
        ml = yaml.safe_load(fh)["bdf"]["ml"]
    with open(os.path.join(PLAYBOOKS, "files", "daolab-app-exporter"), encoding="utf-8") as fh:
        exporter = re.findall(r'"--ml-pci", default="([^"]+)"', fh.read())
    with open(SMOKE, encoding="utf-8") as fh:
        smoke = re.findall(r"PCI=\$\{ML_PCI:-([^}]+)\}", fh.read())
    assert exporter == [ml] and smoke == [ml]
    for rel in ML_PLAYBOOKS:
        assert ml not in _read(rel), rel
    for rel in ("verify_clean.yml", "probe.yml"):
        assert ML_FALLBACK.strip("{} ") in _read(rel), rel


# ---- host paths: variables whose defaults are today's values -----------------------------

PATH_DEFAULTS = {
    "daolab_sysfs": "/sys",
    "daolab_hugetlbfs": "/dev/hugepages /mnt/huge",
    "daolab_dpdk_run_dir": "/var/run/dpdk",
    "daolab_procfs": "/proc",
    "daolab_vfio_dir": "/dev/vfio",
    "ml_smoke_bin": "/usr/local/bin/daolab-ml-smoke",
    "ml_env_file": "/etc/default/daolab-ml",
    "ml_test_mldev": "/usr/bin/dpdk-test-mldev",
}
LITERALS = ("/dev/hugepages", "/mnt/huge", "/var/run/dpdk", "/proc", "/dev/vfio",
            "/usr/local/bin/daolab-ml-smoke", "/etc/default/daolab-ml",
            "/usr/bin/dpdk-test-mldev")


def _code(text):
    """The text without comment lines (YAML and shell)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("rel", ML_PLAYBOOKS[:3])
def test_no_ml_host_path_is_a_literal_outside_a_default(rel):
    code = re.sub(r"""default\((['"])[^'"]*\1\)""", "default()", _code(_read(rel)))
    assert [lit for lit in LITERALS if lit in code] == []


def test_the_ml_host_path_defaults_are_todays_values():
    found = {}
    for rel in ML_PLAYBOOKS[:3]:
        for var, val in re.findall(r"""\b(\w+) \| default\((?:'([^']*)')\)""", _code(_read(rel))):
            found.setdefault(var, set()).add(val)
    for var, val in PATH_DEFAULTS.items():
        assert found.get(var) == {val}, var


SMOKE_DEFAULTS = {"DAOLAB_SYSFS": "/sys", "DAOLAB_PROCFS": "/proc", "DAOLAB_VFIO_DIR": "/dev/vfio",
                  "DAOLAB_DPDK_RUN_DIR": "/var/run/dpdk",
                  "DAOLAB_HUGETLBFS": "/dev/hugepages /mnt/huge"}


def test_the_smoke_test_host_paths_are_overridable_with_todays_defaults():
    """Q-B: like DAOLAB_SYSFS, every host path the smoke test reads or deletes comes from the
    environment, defaulting to the board's value."""
    with open(SMOKE, encoding="utf-8") as fh:
        code = _code(fh.read())
    found = dict(re.findall(r"\$\{(DAOLAB_[A-Z_]+):-([^}]*)\}", code))
    assert found == SMOKE_DEFAULTS
    rest = re.sub(r"\$\{DAOLAB_[A-Z_]+:-[^}]*\}", "", code)
    assert [lit for lit in LITERALS[:5] if lit in rest] == []


ML_ENV_KEYS = {"ML_PCI", "ML_FW", "ML_LCORES", "ML_PREFIX", "ML_SMOKE_DIR", "ML_SMOKE_MODEL",
               "ML_STATE_DIR"}
SECRET_LIKE = re.compile(r"SECRET|TOKEN|PASSW|KEY|AUTH|CRED", re.I)


def _env_file_template():
    """The `content:` of deploy.yml's /etc/default/daolab-ml task: {NAME: template}."""
    plays = yaml.safe_load(_read("apps/ml-tvm/deploy.yml"))
    tasks = [t for play in plays for t in play.get("tasks", [])
             if t.get("name") == "its board values"]
    assert len(tasks) == 1
    lines = tasks[0]["ansible.builtin.copy"]["content"].splitlines()
    return dict(line.split("=", 1) for line in lines if line.strip())


def test_the_env_file_path_lines_are_the_smoke_tests_own_defaults():
    """Q-B / point 3: with no override, every DAOLAB_* line deploy writes equals the smoke
    script's built-in default, so the board runs exactly as before."""
    env = _env_file_template()
    assert set(env) == ML_ENV_KEYS | set(SMOKE_DEFAULTS)
    for name, default in SMOKE_DEFAULTS.items():
        m = re.fullmatch(r"""(["']?)\{\{ (\w+) \| default\('([^']*)'\) \}\}\1""", env[name])
        assert m, (name, env[name])
        assert m.group(3) == default and PATH_DEFAULTS[m.group(2)] == default, name


def test_the_env_file_holds_nothing_secret_like():
    """F-5-1: no secret-like name or value, and no secret variable, in /etc/default/daolab-ml."""
    env = _env_file_template()
    assert [k for k in env if SECRET_LIKE.search(k)] == []
    assert [v for v in env.values() if SECRET_LIKE.search(v) or "secret" in v] == []


# ---- the fake board ------------------------------------------------------------------------

class MlBoard:
    """The ML device and the paths ml-tvm touches, all under the lab's temp dir."""

    def __init__(self, lab):
        d = lab.dir
        self.lab = lab
        self.sys = d / "sys"                          # Lab.run's daolab_sysfs
        self.dev = self.sys / "bus" / "pci" / "devices" / PCI
        self.drivers = self.sys / "bus" / "pci" / "drivers"
        self.hp = self.sys / "kernel" / "mm" / "hugepages" / "hugepages-524288kB"
        self.proc, self.vfio, self.run_dir = d / "proc", d / "vfio", d / "dpdk"
        self.huge = [d / "hugepages", d / "mnt-huge"]
        self.state = d / "ml-state"
        self.models = d / "ml-models"
        self.fw = d / "firmware" / "mlip-fw.bin"
        self.smoke_bin = d / "usr-local-bin" / "daolab-ml-smoke"
        self.env_file = d / "etc-default-daolab-ml"
        self.mldev_log = d / "mldev-calls.log"
        self.verdict = d / "mldev-verdict"
        self.mldev = lab.bindir / "dpdk-test-mldev"
        for p in (self.dev, self.drivers / "vfio-pci", self.hp, self.proc, self.vfio,
                  self.run_dir, *self.huge, self.models, self.fw.parent, self.smoke_bin.parent,
                  self.sys / "kernel" / "iommu_groups" / GROUP):
            p.mkdir(parents=True, exist_ok=True)
        (self.dev / "iommu_group").symlink_to(self.sys / "kernel" / "iommu_groups" / GROUP)
        (self.dev / "driver_override").write_text("(null)\n")
        for f in ("bind", "unbind"):
            (self.drivers / "vfio-pci" / f).write_text("")
        self.pages(2, 2)                                          # nginx-tls's two resting pages
        self.fw.write_text("fw")
        for suffix in (".bin", "-input.bin", "-output.bin"):
            (self.models / f"{MODEL}{suffix}").write_text("model")
        self.verdict.write_text("Success")
        # dpdk-test-mldev: leaves its runtime dir and a hugepage file like a real run, and
        # colours its verdict (c736d4b)
        self.mldev.write_text(f"""#!/bin/sh
echo "$*" >> {self.mldev_log}
prefix=""
while [ $# -gt 0 ]; do [ "$1" = --file-prefix ] && prefix=$2; shift; done
mkdir -p {self.run_dir}/$prefix
: > {self.huge[0]}/${{prefix}}map_0
printf 'ML: inference done\\nResult: \\033[32m%s\\033[0m\\n' "$(cat {self.verdict})"
[ "$(cat {self.verdict})" = Success ]
""")
        self.mldev.chmod(0o755)
        self.holders = []

    def on(self, driver):
        link = self.dev / "driver"
        if link.is_symlink():
            link.unlink()
        if driver:
            (self.drivers / driver).mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.drivers / driver)

    def pages(self, nr, free):
        (self.hp / "nr_hugepages").write_text(f"{nr}\n")
        (self.hp / "free_hugepages").write_text(f"{free}\n")

    def holder(self):
        """A real process of this test (never anyone else's pid), shown in the fake /proc as
        holding the ML device's VFIO group."""
        p = subprocess.Popen(["sleep", "300"])
        self.holders.append(p)
        fd = self.proc / str(p.pid) / "fd"
        fd.mkdir(parents=True)
        (fd / "7").symlink_to(self.vfio / GROUP)
        return p

    def leftovers(self):
        """What a user's own runs leave: ML DPDK files, beside nginx-tls's (never touched)."""
        for name in ("ml", "ml-smoke", "e_cpt_engine82558"):
            (self.run_dir / name).mkdir(exist_ok=True)
        for f in (self.huge[0] / "mlmap_0", self.huge[1] / "ml-smokemap_3",
                  self.huge[0] / "e_cpt_engine82558map_0"):
            f.write_text("x")

    def reap(self):
        """The kernel drops /proc/<pid> when a process dies; the fake /proc does it here."""
        for p in self.holders:
            if p.poll() is not None:
                shutil.rmtree(self.proc / str(p.pid), ignore_errors=True)

    def files(self):
        return (sorted(p.name for p in self.run_dir.iterdir()),
                sorted(p.name for h in self.huge for p in h.iterdir()))

    def save_state(self, **over):
        self.state.mkdir(exist_ok=True)
        st = {"saved_driver": "", "saved_override": "", "saved_nr_hugepages": 2,
              "added_hugepages": 2, **over}
        (self.state / "state.json").write_text(json.dumps(st))

    @property
    def vars(self):
        return {"ml_pci": PCI, "ml_fw": str(self.fw), "ml_lcores": "14-17",
                "ml_file_prefix": "ml", "ml_hugepage_kb": 524288, "ml_hugepages_extra": 2,
                "ml_smoke_dir": str(self.models), "ml_smoke_model": MODEL,
                "ml_state_dir": str(self.state), "daolab_procfs": str(self.proc),
                "daolab_vfio_dir": str(self.vfio), "daolab_dpdk_run_dir": str(self.run_dir),
                "daolab_hugetlbfs": " ".join(str(h) for h in self.huge),
                "ml_smoke_bin": str(self.smoke_bin), "ml_env_file": str(self.env_file),
                "ml_test_mldev": str(self.mldev), "ansible_become": False,
                # ansible's own module temp dirs (and its `rm -f -r` of them) stay in the test dir
                "ansible_remote_tmp": str(self.lab.dir / "ansible-remote-tmp")}

    def smoke_env(self):
        return {**os.environ, "PATH": f"{self.lab.bindir}:{os.environ['PATH']}",
                "ML_PCI": PCI, "ML_FW": str(self.fw), "ML_LCORES": "14-17", "ML_PREFIX": "ml",
                "ML_SMOKE_DIR": str(self.models), "ML_SMOKE_MODEL": MODEL,
                "ML_STATE_DIR": str(self.state), "DAOLAB_SYSFS": str(self.sys),
                "DAOLAB_PROCFS": str(self.proc), "DAOLAB_VFIO_DIR": str(self.vfio),
                "DAOLAB_DPDK_RUN_DIR": str(self.run_dir),
                "DAOLAB_HUGETLBFS": " ".join(str(h) for h in self.huge)}

    def stop(self):
        for p in self.holders:
            if p.poll() is None:
                p.kill()
            p.wait()


@pytest.fixture
def ml(lab):
    b = MlBoard(lab)
    yield b
    b.stop()


def _verify_cmd(ml):
    """The use case's check as deploy and probe run it on the DPU, with the fake board's
    environment (the catalog's own cmd is `sudo -n <smoke>`)."""
    return f"set -a; . {ml.env_file}; set +a; {ml.smoke_bin}"


ML_VERIFY_MATCH = {"match": r"daolab-ml-smoke: success=(?P<success>\d)", "expect": {"success": "== 1"}}


def ml_deploy(lab, books, ml, **over):
    """deploy.yml for ml-tvm (no VMs): play 0, the app, its exporter, verify from the DPU."""
    ev = {"daolab_app": "ml-tvm", "usecase": "ml-tvm", "daolab_roles": ML_ROLES,
          "daolab_verify": {"from": "infer", "cmd": _verify_cmd(ml), **ML_VERIFY_MATCH},
          "daolab_deploy_vms": False, "hw_class": "", "daolab_api_url": "http://127.0.0.1:1",
          "daolab_internal_secret": "x", "reservation_id": 0,
          "daolab_vms": [],                                   # the runner always passes it
          **ml.vars, **over}
    return app_run(lab, books, "deploy.yml", **ev)


def ml_cleanup(lab, books, ml, **over):
    wrapper = lab.dir / "cleanup-ml-tvm.yml"
    wrapper.write_text(yaml.safe_dump([
        {"hosts": "localhost", "gather_facts": False, "tasks": [
            {"ansible.builtin.import_tasks": os.path.join(PLAYBOOKS, "tasks", "resolve.yml")},
            {"ansible.builtin.set_fact": {"daolab_playbooks": PLAYBOOKS}}]},
        {"ansible.builtin.import_playbook": os.path.join(PLAYBOOKS, "apps", "ml-tvm",
                                                          "cleanup.yml")}]))
    return lab.run(str(wrapper), daolab_roles=ML_ROLES, usecase="ml-tvm",
                   **{**books.vars, **ml.vars, **over})


def ml_verify_clean(lab, ml, **over):
    assert lab.run("verify_clean.yml", usecase="ml-tvm", **{**ml.vars, **over}) == 0, \
        lab.stdout[-2000:]
    return lab.output()


# ---- the smoke test (DPU side) -------------------------------------------------------------

def _smoke(ml):
    return subprocess.run(["bash", SMOKE], env=ml.smoke_env(), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def test_the_smoke_test_runs_one_inference_and_leaves_no_dpdk_files(ml):
    ml.on("vfio-pci")
    ml.leftovers()
    before = ml.files()
    p = _smoke(ml)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.splitlines()[-1] == "daolab-ml-smoke: success=1"
    log = (ml.state / "last-smoke.log").read_text()
    assert "Result: Success" in log and "\x1b" not in log             # plain text (c736d4b)
    assert "--file-prefix ml-smoke" in ml.mldev_log.read_text()
    # only its own prefix (ml-smoke) is removed; the user's `ml` files and nginx-tls's stay
    assert ml.files() == (sorted(set(before[0]) - {"ml-smoke"}),
                          sorted(set(before[1]) - {"ml-smokemap_3"}))


def test_the_smoke_test_fails_when_the_inference_fails(ml):
    ml.on("vfio-pci")
    ml.verdict.write_text("Failed")
    p = _smoke(ml)
    assert p.returncode == 1 and p.stdout.splitlines()[-1] == "daolab-ml-smoke: success=0"


def test_the_smoke_test_reports_the_last_result_while_the_owner_holds_the_device(ml):
    ml.on("vfio-pci")
    ml.state.mkdir()
    (ml.state / "last-smoke.log").write_text("Result: Success\n")
    ml.holder()
    p = _smoke(ml)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "busy" in p.stdout and p.stdout.splitlines()[-1] == "daolab-ml-smoke: success=1"
    assert not ml.mldev_log.exists()                                  # it did not contend


def test_the_smoke_test_refuses_a_device_not_on_vfio_pci(ml):
    ml.on("")
    p = _smoke(ml)
    assert p.returncode == 1 and "not vfio-pci" in p.stdout
    assert not ml.mldev_log.exists()


# ---- the exporter (--app ml) ---------------------------------------------------------------

@pytest.mark.parametrize("driver,result,want", [
    ("vfio-pci", "ML: done\nResult: Success\n", {"ml_up": 1.0, "ml_last_smoke_ok": 1.0}),
    ("", "Result: Failed\n", {"ml_up": 0.0, "ml_last_smoke_ok": 0.0}),
    ("vfio-pci", None, {"ml_up": 1.0, "ml_last_smoke_ok": 0.0}),
])
def test_the_exporter_reports_the_ml_device_and_the_last_smoke(tmp_path, capsys, driver,
                                                               result, want):
    devs = tmp_path / "devices"
    (devs / PCI).mkdir(parents=True)
    if driver:
        (tmp_path / "drivers" / driver).mkdir(parents=True)
        (devs / PCI / "driver").symlink_to(tmp_path / "drivers" / driver)
    res = tmp_path / "last-smoke.log"
    if result is not None:
        res.write_text(result)
    exp.main(["--app", "ml", "--ml-pci", PCI, "--pci-sysfs", str(devs), "--ml-result", str(res),
              "--once"])
    assert series(capsys.readouterr().out) == want


# ---- the playbooks, through the fake lab --------------------------------------------------

@needs_ansible
def test_ml_deploy_without_sign_off_touches_nothing(lab, books, ml):
    ml.on("")                                                         # the board at rest
    board(lab)
    assert ml_deploy(lab, books, ml, allow_destructive_changes=False) != 0
    assert "Set DAOLAB_ALLOW_DESTRUCTIVE=true" in lab.stdout
    assert not (ml.state / "state.json").exists()
    assert (ml.dev / "driver_override").read_text() == "(null)\n"
    assert (ml.drivers / "vfio-pci" / "bind").read_text() == ""


@needs_ansible
def test_ml_deploy_refuses_a_board_that_is_not_ready(lab, books, ml):
    ml.on("vfio-pci")
    board(lab)
    (ml.models / f"{MODEL}-output.bin").unlink()
    assert ml_deploy(lab, books, ml) != 0
    assert "the board is not ready for ml-tvm" in lab.stdout


@needs_ansible
def test_ml_deploy_proves_the_mlip_and_verifies_from_the_dpu(lab, books, ml):
    ml.on("vfio-pci")
    board(lab)
    assert ml_deploy(lab, books, ml) == 0, lab.stdout[-3000:]
    saved = json.loads((ml.state / "state.json").read_text())
    assert saved == {"saved_driver": "vfio-pci", "saved_override": "", "saved_nr_hugepages": 2,
                     "added_hugepages": 0}
    env = dict(line.split("=", 1) for line in ml.env_file.read_text().splitlines() if line)
    assert set(env) == ML_ENV_KEYS | set(SMOKE_DEFAULTS)
    assert env["ML_PCI"] == PCI and env["DAOLAB_PROCFS"] == str(ml.proc)
    assert env["DAOLAB_HUGETLBFS"].strip('"') == ml.vars["daolab_hugetlbfs"]
    assert os.access(ml.smoke_bin, os.X_OK)
    assert "step: dpu_app" in lab.stdout
    assert "step: verify" in lab.stdout and "localhost -> dpu-01" in lab.stdout   # on the DPU
    assert "verify ok: success = 1" in lab.stdout
    assert lab.state()["units"]["daolab-app-exporter.service"] == "active"
    unit = (books.binary.parent / "daolab-app-exporter.service").read_text()
    assert f"--app ml --ml-pci {PCI}" in unit
    assert ml.files() == ([], [])                                      # the smoke left nothing


@needs_ansible
def test_a_second_ml_deploy_keeps_the_first_saved_state(lab, books, ml):
    ml.on("vfio-pci")
    board(lab)
    assert ml_deploy(lab, books, ml) == 0, lab.stdout[-3000:]
    first = (ml.state / "state.json").read_text()
    ml.pages(4, 2)
    assert ml_deploy(lab, books, ml) == 0, lab.stdout[-3000:]
    assert (ml.state / "state.json").read_text() == first


@needs_ansible
def test_ml_cleanup_gives_the_board_back(lab, books, ml):
    ml.on("vfio-pci")
    ml.pages(4, 2)
    ml.save_state()                                                   # deploy bound it, +2 pages
    ml.leftovers()
    holder = ml.holder()
    board(lab)
    assert ml_cleanup(lab, books, ml) == 0, lab.stdout[-3000:]
    assert holder.wait(timeout=10) is not None                        # the device holder stopped
    assert re.search(r"TASK \[step: stop_app\][^\n]*\n+changed: \[dpu-01\]", lab.stdout)
    assert ml.files() == (["e_cpt_engine82558"], ["e_cpt_engine82558map_0"])   # nginx-tls's kept
    assert (ml.hp / "nr_hugepages").read_text().strip() == "2"
    assert (ml.drivers / "vfio-pci" / "unbind").read_text().strip() == PCI
    assert (ml.dev / "driver_override").read_text().strip() == ""
    assert not (ml.state / "state.json").exists()


GUARD = """#!{python}
# Test stand-in for {name}: logs its targets and refuses to act on any outside {root}
# (never the test machine's own files), else runs the real {real}.
import json, os, sys
args = sys.argv[1:]
if "{name}" == "rm":
    targets = [a for a in args if not a.startswith("-")]
else:
    targets = []
    for a in args:
        if a.startswith("-") or a in ("(", "!"):
            break
        targets.append(a)
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"cmd": "{name}", "targets": targets, "delete": "-delete" in args}}) + "\\n")
if "{name}" == "find" and "-delete" not in args:
    os.execv({real!r}, [{real!r}] + args)
if any(not os.path.realpath(t).startswith({root!r} + os.sep) for t in targets):
    sys.exit(1)
os.execv({real!r}, [{real!r}] + args)
"""


def _guard_rm_and_find(lab):
    log = lab.dir / "rm-find.log"
    root = os.path.realpath(lab.dir)
    plain = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                            if os.path.realpath(p) != os.path.realpath(lab.bindir))
    for name in ("rm", "find"):
        real = shutil.which(name, path=plain)
        f = lab.bindir / name
        f.write_text(GUARD.format(python=sys.executable, name=name, root=root, real=real,
                                  log=str(log)))
        f.chmod(0o755)
    return log, root


@needs_ansible
def test_every_ml_cleanup_delete_target_is_under_the_test_dir(lab, books, ml):
    """Decision 5: in a test run, cleanup's rm and find -delete only ever reach the fake
    board under tmp_path. Off-tree targets are refused by the stand-ins, so a red run cannot
    delete anything of the test machine's."""
    ml.on("vfio-pci")
    ml.pages(4, 2)
    ml.save_state()
    ml.leftovers()
    board(lab)
    log, root = _guard_rm_and_find(lab)
    ml_cleanup(lab, books, ml)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    deletes = [c for c in calls if c["cmd"] == "rm" or c["delete"]]
    assert {c["cmd"] for c in deletes} == {"rm", "find"}               # both steps did run
    outside = sorted({t for c in deletes for t in c["targets"]
                      if not os.path.realpath(t).startswith(root + os.sep)})
    assert outside == []


@needs_ansible
def test_ml_cleanup_of_a_clean_board_changes_nothing(lab, books, ml):
    ml.on("")
    board(lab)
    assert ml_cleanup(lab, books, ml) == 0, lab.stdout[-3000:]
    assert (ml.hp / "nr_hugepages").read_text() == "2\n"
    assert (ml.dev / "driver_override").read_text() == "(null)\n"
    assert (ml.drivers / "vfio-pci" / "unbind").read_text() == ""


@needs_ansible
@pytest.mark.parametrize("left", ["state", "holder", "runtime", "hugepages"])
def test_verify_clean_reports_what_ml_tvm_left(lab, ml, left):
    ml.on("vfio-pci")
    if left == "state":
        ml.save_state()
    elif left == "holder":
        ml.holder()
    elif left == "runtime":
        (ml.run_dir / "ml-smoke").mkdir()
    else:
        (ml.huge[1] / "mlmap_0").write_text("x")
    residue = ml_verify_clean(lab, ml)
    assert residue == ["dpu_app:ml-tvm"]
    assert [runner.parse_residue(r)[0] for r in residue] == ["dpu_app"]   # an existing kind


def _without_ml_address(ml):
    return {k: v for k, v in ml.vars.items() if k != "ml_pci"}      # and no bdf: resolves to ''


@needs_ansible
def test_verify_clean_skips_the_ml_device_check_without_an_ml_address(lab, ml):
    """Another use case on a DPU whose inventory names no ML device: the holder check is
    skipped, and the job log says so."""
    ml.on("vfio-pci")
    ml.holder()                                         # would be found if the check ran
    assert lab.run("verify_clean.yml", usecase="vpp", **_without_ml_address(ml)) == 0, \
        lab.stdout[-2000:]
    assert lab.output() == []
    assert "ML device check skipped: no ML device address (ml_pci / bdf.ml)" in lab.stdout


@needs_ansible
@pytest.mark.parametrize("left", ["state", "runtime", "hugepages"])
def test_without_an_ml_address_only_the_holder_check_is_skipped(lab, ml, left):
    """The skip drops the device-holder check only: what ml-tvm leaves in its state dir and its
    DPDK files is still reported."""
    ml.on("vfio-pci")
    if left == "state":
        ml.save_state()
    elif left == "runtime":
        (ml.run_dir / "ml").mkdir()
    else:
        (ml.huge[0] / "mlmap_0").write_text("x")
    assert lab.run("verify_clean.yml", usecase="ml-tvm", **_without_ml_address(ml)) == 0, \
        lab.stdout[-2000:]
    assert lab.output() == ["dpu_app:ml-tvm"]
    assert "ML device check skipped: no ML device address (ml_pci / bdf.ml)" in lab.stdout


@needs_ansible
@pytest.mark.parametrize("address", [{"ml_pci": ""}, {}])      # set to '', or no ml_pci and no bdf
def test_ml_deploy_fails_closed_without_an_ml_address(lab, books, ml, address):
    """No ml-tvm reservation can exist on a DPU whose inventory names no ML device, so the
    verify_clean skip above cannot hide one's residue."""
    ml.on("")
    board(lab)
    v = {**_without_ml_address(ml), **address}
    ev = {"daolab_app": "ml-tvm", "usecase": "ml-tvm", "daolab_roles": ML_ROLES,
          "daolab_verify": {"from": "infer", "cmd": "true", **ML_VERIFY_MATCH},
          "daolab_deploy_vms": False, "hw_class": "", "daolab_api_url": "http://127.0.0.1:1",
          "daolab_internal_secret": "x", "reservation_id": 0, "daolab_vms": [], **v}
    assert app_run(lab, books, "deploy.yml", **ev) != 0
    assert "ml-tvm needs the DPU's ML device address" in lab.stdout
    assert not ml.state.exists()                                      # no partial state
    assert (ml.dev / "driver_override").read_text() == "(null)\n"
    assert (ml.drivers / "vfio-pci" / "bind").read_text() == ""
    assert (ml.hp / "nr_hugepages").read_text() == "2\n"
    assert not ml.smoke_bin.exists() and not ml.env_file.exists()


@needs_ansible
def test_verify_clean_ignores_other_apps_dpdk_files(lab, ml):
    ml.on("vfio-pci")
    (ml.run_dir / "e_cpt_engine82558").mkdir()
    (ml.huge[0] / "e_cpt_engine82558map_0").write_text("x")
    assert ml_verify_clean(lab, ml) == []


@needs_ansible
def test_ml_tvm_residue_is_reported_and_then_cleared(lab, books, ml):
    ml.on("vfio-pci")
    board(lab)
    assert ml_deploy(lab, books, ml) == 0, lab.stdout[-3000:]
    during = ["dpu_app:daolab-app-exporter.service", "dpu_app:ml-tvm"]   # its exporter, its state
    assert sorted(ml_verify_clean(lab, ml)) == during
    ml.holder()                                                        # the owner's own run
    ml.leftovers()
    assert sorted(ml_verify_clean(lab, ml)) == during
    assert ml_cleanup(lab, books, ml) == 0, lab.stdout[-3000:]
    ml.reap()
    assert ml_verify_clean(lab, ml) == []


@needs_ansible
def test_probe_without_vms_verifies_ml_tvm_from_the_dpu(lab, ml):
    """The probe falls back to bdf.ml when ml_pci is not set (Q-C)."""
    args = lab.dir / "exporter-args"
    exporter = lab.dir / "fake-exporter"
    exporter.write_text(f"#!/bin/sh\necho \"$*\" > {args}\necho '# TYPE ml_up gauge'\n"
                        "echo 'ml_up 1'\n")
    exporter.chmod(0o755)
    v = {k: v for k, v in ml.vars.items() if k != "ml_pci"}
    rc = lab.run("probe.yml", usecase="ml-tvm", daolab_roles=ML_ROLES, daolab_app="ml-tvm",
                 daolab_verify={"from": "infer", "cmd": "echo 'daolab-ml-smoke: success=1'",
                                **ML_VERIFY_MATCH},
                 daolab_deploy_vms=False, daolab_vms=[], app_exporter_bin=str(exporter),
                 bdf={"ml": PCI}, **v)
    assert rc == 0, lab.stdout[-3000:]
    assert "localhost -> dpu-01" in lab.stdout and "verify ok: success = 1" in lab.stdout
    assert args.read_text().split() == ["--app", "ml", "--ml-pci", PCI, "--once"]
