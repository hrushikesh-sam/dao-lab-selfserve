"""B-84: runner.AnsibleRunner — the playbooks through ansible-runner (extravars, TRACEPARENT,
a per-job artifact dir, real job logs linked from GET /jobs/{id})."""
import dataclasses
import json
import os
import types

import pytest

from service import catalog, config, engine, models, otel, runner
from service.runner import AnsibleRunner, RunnerError
from service.scheduler import SchedConfig
from tests.conftest import NOW, REPO, T, mk_res, mk_user


class Steps:
    def __init__(self):
        self.calls = []

    def __call__(self, name, state="ok", detail=None):
        self.calls.append((name, state, detail))


def ev(event, task, host="dpu-01", stdout="", **data):
    return {"event": event, "stdout": stdout,
            "event_data": {"task": task, "host": host, **data}}


class FakeRun:
    """Stands in for ansible_runner.run: plays back `events`, writes `output` to the
    daolab_output_file, returns status / rc."""
    def __init__(self, events=(), output=None, status="successful", rc=0):
        self.events, self.output, self.status, self.rc = list(events), output, status, rc
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        for e in self.events:
            kw["event_handler"](e)
        if self.output is not None:
            with open(kw["extravars"]["daolab_output_file"], "w") as fh:
                json.dump(self.output, fh)
        return types.SimpleNamespace(status=self.status, rc=self.rc)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = dataclasses.replace(config.settings, artifacts_dir=str(tmp_path / "jobs"),
                            playbooks_dir=os.path.join(REPO, "playbooks"),
                            inventory=os.path.join(REPO, "inventory", "hosts.yml"),
                            api_url="http://10.30.0.2:8080/", internal_secret="s3",
                            otel_endpoint="", otel_agent_endpoint="10.30.0.2:4317")
    monkeypatch.setattr(config, "settings", s)
    return s


@pytest.fixture
def ctx(db):
    catalog.seed_usecases(db)
    catalog.sync(db, os.path.join(REPO, "usecases"), os.path.join(REPO, "setups"),
                 os.path.join(REPO, "inventory", "hosts.yml"))
    uid, _ = mk_user(db, "alice")
    rid = mk_res(db, models.setup_by_name(db, "tb-01")["id"],
                 models.usecase_by_name(db, "vpp")["id"], T(9, 30), T(11), "provisioning",
                 user_id=uid)
    models.create_vms(db, rid)
    return {**models.reservation_context(db, rid), "job_id": 7, "job_kind": "deploy",
            "attempt": 1}


def test_deploy_runs_deploy_yml_with_the_playbook_contract(settings, ctx):
    fake = FakeRun()
    assert AnsibleRunner(settings, run=fake).deploy(ctx, Steps()).ok
    [kw] = fake.calls
    assert kw["playbook"] == "deploy.yml" and kw["project_dir"] == settings.playbooks_dir
    assert kw["inventory"] == settings.inventory and kw["ident"] == "deploy"
    assert kw["private_data_dir"].startswith(os.path.join(settings.artifacts_dir, "job-7", "run-1-"))
    e = kw["extravars"]
    assert (e["reservation_id"], e["usecase"], e["setup"], e["user"]) == \
        (ctx["id"], "vpp", "tb-01", "alice")
    assert e["hw_class"] == "sdp-pf1-vf8" and e["diy"] is False and e["templates"] == {}
    assert set(e["daolab_roles"]) == {"vpp", "tgen", "sink"} and e["daolab_verify"]["from"] == "tgen"
    assert {m["name"] for m in e["daolab_members"]} == {"dpu-01", "tg-01"}
    assert {v["role"] for v in e["daolab_vms"]} == {"vpp", "tgen", "sink"}
    assert (e["daolab_api_url"], e["daolab_internal_secret"]) == ("http://10.30.0.2:8080", "s3")
    assert e["daolab_otel_collector"] == "10.30.0.2:4317"
    assert e["daolab_output_file"].endswith("output.json")
    assert e["daolab_app"] == "dao-vpp"                        # selects playbooks/apps/dao-vpp
    assert (e["allow_destructive_changes"], e["allow_launch"], e["allow_cleanup"]) == \
        (False, False, False)                                  # DAOLAB_ALLOW_DESTRUCTIVE off
    assert e["daolab_deploy_vms"] is settings.deploy_vms
    assert kw["envvars"]["ANSIBLE_CONFIG"] == os.path.join(settings.playbooks_dir, "ansible.cfg")
    assert "TRACEPARENT" not in kw["envvars"]                       # tracing off


def test_every_kind_runs_its_playbook(settings, ctx):
    fake = FakeRun(output=[])
    rn = AnsibleRunner(settings, run=fake)
    for fn in ("cleanup", "probe", "reconcile", "verify_clean"):
        getattr(rn, fn)(ctx, Steps())
    assert [c["playbook"] for c in fake.calls] == ["cleanup.yml", "probe.yml", "reconcile.yml",
                                                   "verify_clean.yml"]
    assert [os.path.basename(c["private_data_dir"]) for c in fake.calls] == [
        "run-1-cleanup", "run-2-probe", "run-3-reconcile", "run-4-verify_clean"]
    for name in fake.calls:
        assert set(AnsibleRunner.PLAYBOOKS.values()) >= {name["playbook"]}
    for pb in ("deploy.yml", "cleanup.yml", "probe.yml", "reconcile.yml", "verify_clean.yml",
               "orphans.yml"):
        assert os.path.exists(os.path.join(REPO, "playbooks", pb)), pb


def test_step_tasks_become_job_steps_and_failures_say_where(settings, ctx):
    steps = Steps()
    fake = FakeRun(events=[
        ev("playbook_on_task_start", "step: lease_ips"),
        ev("runner_on_ok", "step: lease_ips", host="localhost"),
        ev("runner_on_ok", "gather something"),                       # not a step: not recorded
        ev("runner_on_skipped", "step: ebf_set"),
        ev("runner_on_failed", "probe something", ignore_errors=True, res={"msg": "meh"}),
        ev("runner_on_failed", "step: create_vm tgen", host="tb-01-xcp",
           res={"msg": "The uuid you supplied was invalid."})],
        status="failed", rc=2)
    result = AnsibleRunner(settings, run=fake).deploy(ctx, steps)
    assert steps.calls == [("lease_ips", "ok", None), ("ebf_set", "skipped", None),
                           ("create_vm tgen", "failed", "The uuid you supplied was invalid.")]
    assert result.ok is False
    assert result.error == "step: create_vm tgen on tb-01-xcp: The uuid you supplied was invalid."


def test_a_failed_task_that_is_not_a_step_is_still_recorded(settings, ctx):
    steps = Steps()
    fake = FakeRun(events=[ev("runner_on_unreachable", "find the template", host="tb-01-xcp",
                              res={"msg": "ssh: connect to host 10.0.0.10 port 22: timed out"})],
                   status="failed", rc=4)
    result = AnsibleRunner(settings, run=fake).deploy(ctx, steps)
    assert steps.calls == [("find the template", "failed",
                            "ssh: connect to host 10.0.0.10 port 22: timed out")]
    assert "find the template on tb-01-xcp" in result.error


def test_a_run_that_fails_without_events_still_has_an_error(settings, ctx):
    result = AnsibleRunner(settings, run=FakeRun(status="timeout", rc=254)).cleanup(ctx, Steps())
    assert result.ok is False and result.error == "cleanup.yml timeout (rc=254)"


def test_verify_clean_reads_the_residue_the_playbook_wrote(settings, ctx):
    rn = AnsibleRunner(settings, run=FakeRun(output=["vm:daolab-r1-sink", "dpu_session:lab"]))
    r = rn.verify_clean(ctx, Steps())
    assert (r.ok, r.residue) == (False, ("vm:daolab-r1-sink", "dpu_session:lab"))
    assert AnsibleRunner(settings, run=FakeRun(output=[])).verify_clean(ctx, Steps()).ok
    unverified = AnsibleRunner(settings, run=FakeRun(status="failed", rc=4)).verify_clean(ctx, Steps())
    assert unverified.ok is False and unverified.residue == ()      # could not verify != clean


def test_the_orphan_sweep_lists_and_destroys_through_orphans_yml(settings):
    fake = FakeRun(output=["daolab-r999-x"])
    rn = AnsibleRunner(settings, run=fake)
    assert rn.tagged_vms() == ["daolab-r999-x"]
    assert rn.destroy_vms(["daolab-r999-x"], Steps()).ok
    assert [c["playbook"] for c in fake.calls] == ["orphans.yml", "orphans.yml"]
    assert "daolab_vm_names" not in fake.calls[0]["extravars"]
    assert fake.calls[1]["extravars"]["daolab_vm_names"] == ["daolab-r999-x"]
    assert "sweep" in fake.calls[0]["private_data_dir"]
    with pytest.raises(RunnerError, match="could not list"):
        AnsibleRunner(settings, run=FakeRun(status="failed", rc=2)).tagged_vms()


def test_all_playbook_output_of_a_job_goes_to_one_log(settings, ctx):
    rn = AnsibleRunner(settings, run=FakeRun(events=[ev("runner_on_ok", "x", stdout="TASK [x] ok")],
                                             output=[]))
    rn.cleanup(ctx, Steps())
    rn.verify_clean(ctx, Steps())
    path = rn.log_path_for(7)
    assert path == os.path.join(settings.artifacts_dir, "job-7", "job.log")
    assert open(path).read() == "TASK [x] ok\nTASK [x] ok\n"


def test_the_job_span_is_handed_to_ansible_as_traceparent(settings, ctx):
    from opentelemetry.sdk.trace import TracerProvider
    otel.use_provider(TracerProvider())
    try:
        fake = FakeRun()
        rn = AnsibleRunner(dataclasses.replace(settings, otel_endpoint="otel.lab:4317"), run=fake)
        job = {"id": 7, "kind": "deploy", "attempt": 1, "trace_id": None}
        with otel.job_span(job, ctx) as span:
            rn.deploy(ctx, Steps())
        env = fake.calls[0]["envvars"]
        assert env["TRACEPARENT"].split("-")[1] == span.trace_id
        assert env["DAOLAB_ANSIBLE_OTEL"] == "true"
    finally:
        otel.use_provider(None)


def test_the_engine_links_the_real_job_log(settings, db, ctx):
    rid = ctx["id"]
    jid = models.create_job(db, rid, "deploy")
    rn = AnsibleRunner(settings, run=FakeRun(events=[ev("runner_on_ok", "step: lease_ips",
                                                        stdout="TASK [step: lease_ips] ok")]))
    engine.tick(db, NOW, rn, SchedConfig())
    job = models.job_by_id(db, jid)
    assert job["state"] == "succeeded"
    assert job["log_path"] == rn.log_path_for(jid) and os.path.exists(job["log_path"])
    assert [s["name"] for s in models.job_steps(job)] == ["lease_ips"]
    from service.api import job_object
    assert "TASK [step: lease_ips] ok" in job_object(job)["log_tail"]


# ---- with the real ansible-runner, against the fake testbed -----------------------------

ansible_runner = pytest.importorskip("ansible_runner")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from tests.test_playbooks import ANSIBLE, Lab
    if not ANSIBLE:
        pytest.skip("ansible-playbook not installed")
    lab = Lab(tmp_path)
    monkeypatch.setenv("PATH", f"{lab.bindir}:{os.path.dirname(ANSIBLE)}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKELAB_STATE", str(lab.state_path))
    monkeypatch.setenv("FAKELAB_LOG", str(lab.log_path))
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("ANSIBLE_HOME", str(tmp_path / "ansible-home"))
    return lab


def real_runner(lab, tmp_path):
    s = dataclasses.replace(config.settings, artifacts_dir=str(tmp_path / "jobs"),
                            playbooks_dir=os.path.join(REPO, "playbooks"),
                            inventory=str(lab.inventory), otel_endpoint="")
    return AnsibleRunner(s)


def test_real_verify_clean_and_reconcile_round_trip(lab, tmp_path):
    from tests.test_playbooks import MEMBERS, clean_state, dirty
    lab.set(dirty(clean_state()))
    rn = real_runner(lab, tmp_path)
    ctx = {"id": 12, "job_id": 1, "setup": "tb-01", "usecase": "vpp", "members": MEMBERS,
           "leases": ["10.30.0.101"]}
    steps = Steps()
    dirty_result = rn.verify_clean(ctx, steps)
    assert dirty_result.ok is False
    assert sorted(dirty_result.residue) == sorted([
        "vm:daolab-r12-sink", "vf:0000:5e:01.1", "dpu_session:lab",
        "dpu_app:daolab-app-dao-vpp.service", "ip:10.30.0.101"])
    assert [n for n, _, _ in steps.calls] == ["check_vms", "check_vfs", "check_sessions", "check_ips"]
    assert rn.reconcile(ctx, Steps()).ok
    st = lab.state()
    st["pingable"] = []
    lab.set(st)
    assert rn.verify_clean(ctx, Steps()).ok
    log = open(rn.log_path_for(1)).read()
    assert "PLAY RECAP" in log and log.count("PLAY RECAP") == 3


def test_real_orphan_sweep(lab, tmp_path):
    from tests.test_playbooks import clean_state
    st = clean_state()
    st["vms"]["daolab-r999-x"] = {"uuid": "u1", "power": "running", "other_config": {},
                                 "xenstore": {}, "vifs": [], "ip": None}
    lab.set(st)
    rn = real_runner(lab, tmp_path)
    assert rn.tagged_vms() == ["daolab-r999-x"]
    assert rn.destroy_vms(["daolab-r999-x"], Steps()).ok
    assert lab.state()["vms"] == {}


def test_the_sign_off_switch_and_the_app_reach_the_playbooks(settings, ctx):
    fake = FakeRun()
    on = dataclasses.replace(settings, allow_destructive=True, deploy_vms=False)
    AnsibleRunner(on, run=fake).deploy(ctx, Steps())
    e = fake.calls[0]["extravars"]
    assert e["allow_destructive_changes"] and e["allow_launch"] and e["allow_cleanup"]
    assert e["daolab_deploy_vms"] is False
    assert runner.dpu_app({"x": {"on": "dpu", "app": "dao-smartnic"}, "y": {"on": "tg_host"}}) \
        == "dao-smartnic"
    assert runner.dpu_app({}) == "none"
    diy = {**ctx, "diy": True}
    AnsibleRunner(on, run=fake).deploy(diy, Steps())
    assert fake.calls[1]["extravars"]["daolab_app"] == "none"


def test_with_vms_off_the_orphan_sweep_asks_no_xcp_host(settings):
    fake = FakeRun(output=["daolab-r999-x"])
    rn = AnsibleRunner(dataclasses.replace(settings, deploy_vms=False), run=fake)
    assert rn.tagged_vms() == [] and fake.calls == []


def test_the_runner_uses_the_ansible_playbook_of_its_own_environment(settings, ctx, monkeypatch):
    """The engine runs as .venv/bin/python without the venv on PATH (systemd, dev.sh): the
    first real run on xeon3-oct-112 failed with rc=127, ansible-playbook not found."""
    import sys
    fake = FakeRun()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    AnsibleRunner(settings, run=fake).deploy(ctx, Steps())
    kw = fake.calls[0]
    expected = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    if os.access(expected, os.X_OK):
        assert "binary" not in kw                     # raw mode would drop the playbook
        assert kw["envvars"]["PATH"].split(os.pathsep)[0] == os.path.dirname(expected)
