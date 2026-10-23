"""Runner — executes the procedures an engine job stands for (B-22).

plan/09 §6 and §9: a job is one of `deploy`, `cleanup`, `health_probe`; the runner has one
function per procedure plus `verify_clean`, which proves a cleanup left nothing behind.
`DAOLAB_RUNNER` picks the backend:

  * ``mock``    — a fake that sleeps through each step and records it, so the whole flow
                  (booking, deploy, cleanup, quarantine) can be demonstrated with no testbed.
  * ``ansible`` — the real thing (B-84): the playbooks, through ansible-runner.

The runner never touches the database. Every function takes the reservation (anything that
supports ``res["id"]``, e.g. a sqlite Row or dict) and a *step sink*: a callable
``step(name, state="ok", detail=None)`` that is called as each step finishes, so progress is
visible while the job is still running. `job_sink` builds a sink that writes the steps onto a
job row. Each function returns a `RunResult`.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Protocol

from . import config, models

RUNNER_NAMES = ("ansible", "mock")
# `health` (B-68) drops the mock's per-reservation exporter so the health badge can be
# observed going healthy -> unknown/down.
MOCK_FAIL_TARGETS = ("deploy", "cleanup", "health")

# Where the mock exporter drops Prometheus file_sd target files (B-68); Prometheus in
# deploy/otel/ reads the same directory (mounted at /etc/prometheus/targets).
OTEL_TARGETS_DIR = os.environ.get("DAOLAB_OTEL_TARGETS_DIR", "deploy/otel/targets")

# The steps the mock walks through (names follow plan/09 §6 and §9). `terminate-user` is the
# DPU session kill (`loginctl terminate-user lab` / `pkill -u lab`, plan/09 §7): it runs first
# so access is revoked at `end_at` even if the rest of teardown fails.
DEPLOY_STEPS = ("lease_ips", "dpu_prepare", "create_vms", "post_vm_ips", "start_app", "verify")
CLEANUP_STEPS = ("terminate-user", "destroy_vms", "unbind_vfs", "stop_app")
VERIFY_CLEAN_STEPS = ("check_vms", "check_vfs", "check_sessions", "check_ips")
RECONCILE_STEPS = ("destroy_vms", "rebind_vfs", "stop_app", "kill_sessions")   # B-72
PROBE_STEPS = ("ping_vms", "check_dpu_app")
FAIL_AT_DEPLOY_STEP = "create_vms"       # where DAOLAB_MOCK_FAIL=deploy breaks the deploy


class RunnerError(Exception):
    """A configuration problem (unknown runner, bad DAOLAB_MOCK_FAIL) or an unsupported job."""


@dataclass(frozen=True)
class RunResult:
    """Outcome of one runner call. For `verify_clean`, ok means *clean*: `residue` is then
    empty; otherwise it lists what is left, one ``kind:ident`` string per item (B-70)."""
    ok: bool
    error: str | None = None
    residue: tuple[str, ...] = ()


# B-70: residue is a structured list. Each item is ``<kind>:<ident>`` so it reads well in a
# job error / audit row and can still be acted on (plan/09 §9 lists what verify checks):
#   vm:daolab-r12-backend   a VM of the reservation still exists on XCP-ng
#   vf:0000:5e:01.0         an E810/SDP VF is not back to pci-assignable
#   dpu_session:lab         processes of the shared DPU login are still running
#   dpu_app:<unit>          a use-case app unit is still active on the DPU
#   ip:10.30.0.101          a leased address still answers ping
RESIDUE_KINDS = ("vm", "vf", "dpu_session", "dpu_app", "ip")
CHECK_FOR_KIND = {"vm": "check_vms", "vf": "check_vfs", "dpu_session": "check_sessions",
                  "dpu_app": "check_sessions", "ip": "check_ips"}


def residue_item(kind: str, ident: str) -> str:
    if kind not in RESIDUE_KINDS:
        raise ValueError(f"unknown residue kind {kind!r}; expected one of {RESIDUE_KINDS}")
    return f"{kind}:{ident}"


def parse_residue(item: str) -> tuple[str, str]:
    """``"vm:daolab-r1-a"`` -> ``("vm", "daolab-r1-a")``; anything unstructured -> ("other", item)."""
    kind, sep, ident = item.partition(":")
    return (kind, ident) if sep and kind in RESIDUE_KINDS else ("other", item)


def _field(res: Mapping, key: str, default=None):
    """`res[key]` for a dict or a sqlite Row, `default` when the key is absent."""
    try:
        return res[key]
    except (KeyError, IndexError):
        return default


StepSink = Callable[..., None]           # step(name, state="ok", detail=None)


class Runner(Protocol):
    def deploy(self, res: Mapping, step: StepSink) -> RunResult: ...
    def cleanup(self, res: Mapping, step: StepSink) -> RunResult: ...
    def probe(self, res: Mapping, step: StepSink) -> RunResult: ...
    def verify_clean(self, res: Mapping, step: StepSink) -> RunResult: ...
    def reconcile(self, res: Mapping, step: StepSink) -> RunResult: ...
    # B-73, the orphan sweep (optional: the engine skips the sweep for a runner without them)
    def tagged_vms(self) -> list[str]: ...
    def destroy_vms(self, names: list[str], step: StepSink) -> RunResult: ...


def parse_mock_fail(text: str) -> frozenset[str]:
    """``DAOLAB_MOCK_FAIL`` is empty or a comma-separated subset of deploy / cleanup."""
    names = [n.strip() for n in (text or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in MOCK_FAIL_TARGETS]
    if unknown:
        raise RunnerError(f"unknown DAOLAB_MOCK_FAIL value(s) {unknown}; "
                          f"expected a comma-separated subset of {MOCK_FAIL_TARGETS}")
    return frozenset(names)


class MockExporter:
    """A fake telemetry exporter for one reservation (B-68).

    Stands in for the per-VM/DPU OTel agents so the health badge can be exercised with no
    testbed. It serves the Prometheus exposition format on ``/metrics`` and registers itself
    as a scrape target through Prometheus **file-based service discovery**: it writes
    ``<targets_dir>/res-<id>.json`` carrying the ``daolab_reservation`` label, so
    ``up{daolab_reservation="<id>"}`` (the `vms_up` rule, plan/09 §8) is generated while the
    exporter runs. `stop` removes the target file, so the series goes stale and health falls
    back to `unknown` after `DAOLAB_HEALTH_STALE_SEC` — the demoed healthy -> unknown/down.

    Binds an ephemeral port by default; `target` is the ``host:port`` Prometheus scrapes.
    """

    def __init__(self, reservation_id: int, *, labels: Mapping | None = None,
                 targets_dir: str | None = None, host: str = "127.0.0.1", port: int = 0):
        self.reservation_id = reservation_id
        self.host = host
        self.port = port
        self.targets_dir = targets_dir or OTEL_TARGETS_DIR
        self.labels = {"daolab_reservation": str(reservation_id)}
        if labels:
            self.labels.update({str(k): str(v) for k, v in labels.items()})
        self._httpd = None
        self._thread = None
        self._target_file = None
        self._started = time.time()

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    # The series the use-case health rules read (usecases/*.yml `health:`), so a mock deploy
    # reads `healthy` end to end; Prometheus adds `up` itself. App series carry the DPU's
    # host label like the rules do. Counters grow at a steady rate so rate() is > 0.
    GAUGES = (("tcpcheck_status", {"port": "22"}), ("httpcheck_status", {}),
              ("vpp_up", {"host": "dpu-01"}), ("nginx_up", {"host": "dpu-01"}),
              ("smartnic_up", {"host": "dpu-01"}))
    COUNTERS = (("vpp_interface_rx_packets", {"host": "dpu-01"}, 1000.0),
                ("smartnic_offload_pps", {"host": "dpu-01"}, 500.0))

    def _metrics_text(self) -> str:
        def series(name, extra, value):
            labels = {**self.labels, **extra}
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            return f"{name}{{{label_str}}} {value}\n"

        elapsed = max(0.0, time.time() - self._started)
        out = ["# HELP daolab_mock_up Mock agent liveness (B-68).\n",
               "# TYPE daolab_mock_up gauge\n", series("daolab_mock_up", {}, 1)]
        for name, extra in self.GAUGES:
            out += [f"# TYPE {name} gauge\n", series(name, extra, 1)]
        for name, extra, per_sec in self.COUNTERS:
            out += [f"# TYPE {name} counter\n", series(name, extra, round(per_sec * elapsed, 3))]
        return "".join(out)

    def _write_target_file(self) -> None:
        os.makedirs(self.targets_dir, exist_ok=True)
        path = os.path.join(self.targets_dir, f"res-{self.reservation_id}.json")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"targets": [self.target], "labels": self.labels}, fh)
        os.replace(tmp, path)
        self._target_file = path

    def start(self) -> "MockExporter":
        exporter = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = exporter._metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):        # keep the lab logs quiet
                pass

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name=f"mock-exporter-{self.reservation_id}", daemon=True)
        self._thread.start()
        self._write_target_file()
        return self

    def stop(self) -> None:
        if self._target_file and os.path.exists(self._target_file):
            os.remove(self._target_file)
        self._target_file = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class InternalApi:
    """The playbook-side calls of the daolab API (plan/09 §6), for the mock runner (B-44):
    `POST /internal/leases`, `/internal/leases/conflict` and `/internal/vm-ip`, authenticated
    with X-Daolab-Secret. Raises RunnerError carrying the API's error message."""

    def __init__(self, api_url: str, secret: str, timeout: float = 10.0):
        self.base = api_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Daolab-Secret": self.secret})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())["error"]
                raise RunnerError(f"{path}: {e.code} {err['code']}: {err['message']}") from None
            except (ValueError, KeyError, TypeError):
                raise RunnerError(f"{path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise RunnerError(f"{path}: api unreachable at {self.base}: "
                              f"{getattr(e, 'reason', e)}") from None

    def lease(self, reservation_id: int, role: str) -> str:
        return self.post("/internal/leases", {"reservation_id": reservation_id, "role": role})["ip"]

    def conflict(self, ip: str) -> str:
        return self.post("/internal/leases/conflict", {"ip": ip})["ip"]

    def vm_ip(self, reservation_id: int, role: str, ip: str) -> None:
        self.post("/internal/vm-ip", {"reservation_id": reservation_id, "role": role, "ip": ip})


class MockRunner:
    """Sleeps `step_sec` through every step and records it.

    ``fail={"deploy"}`` makes `deploy` fail at the `create_vms` step. ``fail={"cleanup"}``
    makes cleanup *look* fine while leaving residue behind, so `verify_clean` reports it
    every time — the way to exercise the quarantine path (plan/09 §13). ``fail={"health"}``
    keeps the per-reservation exporter (B-68) from ever starting, so the health badge stays
    `unknown`. When `exporter_factory` is set, a successful `deploy` starts an exporter and
    `cleanup` stops it.

    With an `api` (an InternalApi; get_runner passes one when DAOLAB_INTERNAL_SECRET is set)
    the deploy calls the real endpoints like deploy.yml does (B-44): `lease_ips` leases an
    address for every VM role of the reservation and `post_vm_ips` reports them through
    `/internal/vm-ip`, so the reservation shows its leased IPs. Without one it only sleeps."""

    def __init__(self, step_sec: float = 0.0, fail: frozenset[str] = frozenset(),
                 sleep: Callable[[float], None] = time.sleep,
                 exporter_factory: Callable[[int], MockExporter] | None = None,
                 api: InternalApi | None = None):
        if step_sec < 0:
            raise RunnerError(f"DAOLAB_MOCK_STEP_SEC must be >= 0, got {step_sec}")
        self.step_sec = step_sec
        self.fail = frozenset(fail)
        self._sleep = sleep
        self._exporter_factory = exporter_factory
        self._exporters: dict[int, MockExporter] = {}
        self.api = api
        self.vms: set[str] = set()       # the "testbed": VM names a deploy created (B-73)

    def _work(self, name: str, step: StepSink) -> None:
        self._sleep(self.step_sec)
        step(name)

    def _start_exporter(self, res: Mapping) -> None:
        if self._exporter_factory is None or "health" in self.fail:
            return
        rid = res["id"]
        if rid in self._exporters:
            return
        exporter = self._exporter_factory(rid)
        exporter.start()
        self._exporters[rid] = exporter

    def _stop_exporter(self, res: Mapping) -> None:
        exporter = self._exporters.pop(res["id"], None)
        if exporter is not None:
            exporter.stop()

    def close(self) -> None:
        """Stop every exporter this runner started (process teardown)."""
        for exporter in list(self._exporters.values()):
            exporter.stop()
        self._exporters.clear()

    def _api_step(self, name: str, res: Mapping, leased: dict) -> str | None:
        """The real work behind `lease_ips` / `post_vm_ips` when an api is wired (B-44).
        Returns the step detail; raises RunnerError when the API refuses."""
        if self.api is None:
            return None
        rid = res["id"]
        vm_roles = [v["role"] for v in _field(res, "vms", []) or [] if v.get("name")]
        if name == "lease_ips":
            for role in vm_roles:
                leased[role] = self.api.lease(rid, role)
        elif name == "post_vm_ips":
            for role, ip in leased.items():
                self.api.vm_ip(rid, role, ip)
        else:
            return None
        return ", ".join(f"{r}={ip}" for r, ip in leased.items()) or None

    def _vm_names(self, res: Mapping) -> set[str]:
        return {v["name"] for v in _field(res, "vms", []) or [] if v.get("name")}

    def tagged_vms(self) -> list[str]:
        """B-73: every daolab-r<id>-* VM on the fake testbed."""
        return sorted(self.vms)

    def destroy_vms(self, names: list[str], step: StepSink) -> RunResult:
        """B-73: destroy exactly these VMs (the orphan sweep)."""
        self._sleep(self.step_sec)
        gone = [n for n in names if n in self.vms]
        self.vms.difference_update(names)
        step("destroy_vms", "ok", ", ".join(gone) or None)
        return RunResult(ok=True)

    def deploy(self, res: Mapping, step: StepSink) -> RunResult:
        leased: dict[str, str] = {}
        for name in DEPLOY_STEPS:
            if "deploy" in self.fail and name == FAIL_AT_DEPLOY_STEP:
                self._sleep(self.step_sec)
                msg = "mock failure injected (DAOLAB_MOCK_FAIL=deploy)"
                step(name, "failed", msg)
                return RunResult(ok=False, error=f"{name}: {msg}")
            self._sleep(self.step_sec)
            try:
                detail = self._api_step(name, res, leased)
            except RunnerError as e:
                step(name, "failed", str(e))
                return RunResult(ok=False, error=f"{name}: {e}")
            step(name, "ok", detail)
            if name == "create_vms":
                self.vms.update(self._vm_names(res))
        self._start_exporter(res)
        return RunResult(ok=True)

    def cleanup(self, res: Mapping, step: StepSink) -> RunResult:
        for name in CLEANUP_STEPS:
            self._work(name, step)
        if "cleanup" not in self.fail:               # stubborn residue keeps its VMs
            self.vms.difference_update(self._vm_names(res))
        self._stop_exporter(res)
        return RunResult(ok=True)

    def probe(self, res: Mapping, step: StepSink) -> RunResult:
        for name in PROBE_STEPS:
            self._work(name, step)
        return RunResult(ok=True)

    def _residue(self, res: Mapping) -> tuple[str, ...]:
        """What the mock pretends is left behind with DAOLAB_MOCK_FAIL=cleanup: the
        reservation's VMs and leased addresses when the context lists them, else a stand-in
        VM and a DPU session."""
        if "cleanup" not in self.fail:
            return ()
        items = [residue_item("vm", v["name"]) for v in _field(res, "vms", []) or []
                 if v.get("name")]
        items += [residue_item("ip", ip) for ip in _field(res, "leases", []) or []]
        if not items:
            items = [residue_item("vm", models.vm_name(res["id"], "backend")),
                     residue_item("dpu_session", "lab")]
        return tuple(items)

    def reconcile(self, res: Mapping, step: StepSink) -> RunResult:
        """B-72: destroy leftovers directly. With DAOLAB_MOCK_FAIL=cleanup the residue is
        stubborn and survives it, so the quarantine path stays reachable (plan/09 §13)."""
        for name in RECONCILE_STEPS:
            self._work(name, step)
        if "cleanup" not in self.fail:
            self.vms.difference_update(self._vm_names(res))
        self._stop_exporter(res)
        return RunResult(ok=True)

    def verify_clean(self, res: Mapping, step: StepSink) -> RunResult:
        residue = self._residue(res)
        for name in VERIFY_CLEAN_STEPS:
            self._sleep(self.step_sec)
            found = [r for r in residue if CHECK_FOR_KIND.get(parse_residue(r)[0]) == name]
            if found:
                step(name, "failed", "; ".join(found))
            else:
                step(name)
        if residue:
            return RunResult(ok=False, error="residue found after cleanup", residue=residue)
        return RunResult(ok=True)


class AnsibleRunner:
    """The real backend (B-84): every procedure is a playbook under DAOLAB_PLAYBOOKS_DIR, run
    with ansible-runner against DAOLAB_INVENTORY.

      deploy        deploy.yml        (B-80)          cleanup     cleanup.yml   (B-82)
      probe         probe.yml         (verify only)   reconcile   reconcile.yml (B-72)
      verify_clean  verify_clean.yml  (B-70)          orphans     orphans.yml   (B-73)

    Extra vars are the playbook contract (plan/04): the reservation context's scalars plus the
    structured `daolab_*` facts, the API URL + internal secret for the callbacks, and the
    lab-reachable collector for the agents. The environment comes from `ansible_env` (B-63),
    with the current job span as TRACEPARENT (B-62).

    Each job gets `<DAOLAB_ARTIFACTS_DIR>/job-<id>/`: ansible-runner's artifacts per playbook,
    and `job.log`, every playbook's output of the job in order - the engine records it as the
    job's log_path, so GET /jobs/{id} shows its tail. A task named `step: <name>` becomes a job
    step when it finishes; any failed task does too, and its message is the job error. Playbooks
    that report data (verify_clean, orphans) write JSON to `daolab_output_file`.

    `run` is ansible_runner.run, injectable for tests; ansible-runner is imported only when a
    playbook actually runs (`pip install -e .[ansible]`)."""

    PLAYBOOKS = {"deploy": "deploy.yml", "cleanup": "cleanup.yml", "probe": "probe.yml",
                 "verify_clean": "verify_clean.yml", "reconcile": "reconcile.yml",
                 "orphans": "orphans.yml"}
    STEP_PREFIX = "step: "
    _DONE = {"runner_on_ok": "ok", "runner_on_skipped": "skipped",
             "runner_on_failed": "failed", "runner_on_unreachable": "failed"}

    def __init__(self, settings=None, run: Callable | None = None):
        self.s = settings or config.settings
        self._run = run

    # ---- paths -----------------------------------------------------------------------
    def job_dir(self, job_id) -> str:
        return os.path.abspath(os.path.join(self.s.artifacts_dir, f"job-{job_id}"))

    def log_path_for(self, job_id) -> str:
        """Where the job's combined playbook output goes (the engine stores it on the job)."""
        return os.path.join(self.job_dir(job_id), "job.log")

    def _claim(self, root: str, res: Mapping) -> None:
        """job-<id>/ belongs to one job. A DB started afresh numbers its jobs from 1 again, so a
        directory another job of the same id left behind is moved aside (job-<id>.stale-<n>),
        never appended to; the job's retries find their own stamp and keep appending."""
        stamp = {"job_id": _field(res, "job_id"), "created_at": _field(res, "job_created_at")}
        marker = os.path.join(root, "job.json")
        if os.path.isdir(root):
            try:
                with open(marker, encoding="utf-8") as fh:
                    if json.load(fh) == stamp:
                        return
            except (OSError, ValueError):
                pass
            if os.listdir(root):
                n = 1
                while os.path.exists(f"{root}.stale-{n}"):
                    n += 1
                os.rename(root, f"{root}.stale-{n}")
        os.makedirs(root, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh)

    # ---- the contract ------------------------------------------------------------------
    def extravars(self, res: Mapping) -> dict:
        get = lambda key, default=None: _field(res, key, default)            # noqa: E731
        return {
            "reservation_id": res["id"], "usecase": get("usecase"), "setup": get("setup"),
            "user": get("user"), "ssh_pubkey": get("ssh_pubkey"), "hw_class": get("hw_class") or "",
            "diy": bool(get("diy", False)), "templates": get("templates") or {},
            "daolab_members": get("members") or [], "daolab_roles": get("roles") or {},
            "daolab_verify": get("verify") or {}, "daolab_vms": get("vms") or [],
            "daolab_leases": get("leases") or [],
            "daolab_api_url": self.s.api_url.rstrip("/"),
            "daolab_internal_secret": self.s.internal_secret,
            "daolab_otel_collector": self.s.otel_agent_endpoint,
            "daolab_app": dpu_app(get("roles") or {}) if not get("diy", False) else "none",
            "daolab_access_login": get("access_login") or "lab",
            "daolab_access_password": get("access_password") or "",
            "daolab_access_expires": get("access_expires") or "",
            "daolab_access_until": get("access_until") or 0,
            "daolab_deploy_vms": bool(self.s.deploy_vms),
            # the runbook playbooks' owner sign-off (VPP_ansible, dao-smartnic): one switch
            "allow_destructive_changes": bool(self.s.allow_destructive),
            "allow_launch": bool(self.s.allow_destructive),
            "allow_cleanup": bool(self.s.allow_destructive),
        }

    def _play(self, kind: str, playbook_key: str, res: Mapping | None, step: StepSink,
              extra: dict | None = None) -> tuple[RunResult, object]:
        """Run one playbook; returns (result, the JSON it wrote to daolab_output_file)."""
        from . import otel
        job_id = _field(res, "job_id") if res is not None else None
        root = self.job_dir(job_id if job_id is not None else "sweep")
        if job_id is not None:
            self._claim(root, res)
        os.makedirs(root, exist_ok=True)
        n = len([d for d in os.listdir(root) if d.startswith("run-")]) + 1
        pdd = os.path.join(root, f"run-{n}-{playbook_key}")
        out_file = os.path.join(pdd, "output.json")
        os.makedirs(pdd, exist_ok=True)
        ev = {**(self.extravars(res) if res is not None else {}),
              "daolab_output_file": out_file, **(extra or {})}
        failure: dict = {}
        log = open(os.path.join(root, "job.log"), "a", encoding="utf-8")

        def on_event(event: dict) -> bool:
            if event.get("stdout"):
                log.write(event["stdout"] + "\n")
                log.flush()
            state = self._DONE.get(event.get("event"))
            if state is None:
                return True
            data = event.get("event_data", {})
            task = (data.get("task") or "").strip()
            if state == "failed" and data.get("ignore_errors"):
                return True
            detail = None
            if state == "failed":
                r = data.get("res") or {}
                detail = str(r.get("msg") or r.get("stderr") or r.get("stdout") or "failed")[-500:]
                failure.update(task=task, msg=detail, host=data.get("host"))
            if task.startswith(self.STEP_PREFIX) or state == "failed":
                name = task[len(self.STEP_PREFIX):] if task.startswith(self.STEP_PREFIX) else task
                step(name, state, detail)
            return True

        run = self._run
        if run is None:
            import ansible_runner
            run = ansible_runner.run
        envvars = ansible_env(self.s, otel.traceparent())
        playbook_bin = ansible_playbook_path()
        if playbook_bin:
            # the engine runs as .venv/bin/python without the venv on PATH (systemd, dev.sh):
            # put the directory of the ansible-playbook installed next to it first on the PATH
            # ansible-runner resolves the command with (not `binary=`: that is raw mode)
            envvars["PATH"] = os.path.dirname(playbook_bin) + os.pathsep + os.environ.get("PATH", "")
        try:
            r = run(private_data_dir=pdd, project_dir=os.path.abspath(self.s.playbooks_dir),
                    playbook=self.PLAYBOOKS[playbook_key],
                    inventory=os.path.abspath(self.s.inventory), extravars=ev,
                    envvars=envvars, event_handler=on_event, quiet=True, ident=kind,
                    # without a callback ansible-runner installs its own SIGTERM/SIGINT handlers
                    # for good, and the engine could no longer be stopped (systemctl stop, ^C)
                    cancel_callback=lambda: False)
        finally:
            log.close()
        ok = getattr(r, "status", None) == "successful" and getattr(r, "rc", 1) == 0
        error = None
        if not ok:
            where = f"{failure['task']} on {failure['host']}: " if failure.get("task") else ""
            error = where + (failure.get("msg") or
                             f"{self.PLAYBOOKS[playbook_key]} {getattr(r, 'status', 'failed')} "
                             f"(rc={getattr(r, 'rc', None)})")
        output = None
        if os.path.exists(out_file):
            with open(out_file, encoding="utf-8") as fh:
                output = json.load(fh)
        return RunResult(ok=ok, error=error), output

    # ---- the runner interface ----------------------------------------------------------
    def deploy(self, res, step):
        return self._play("deploy", "deploy", res, step)[0]

    def cleanup(self, res, step):
        return self._play("cleanup", "cleanup", res, step)[0]

    def probe(self, res, step):
        return self._play("probe", "probe", res, step)[0]

    def reconcile(self, res, step):
        return self._play("reconcile", "reconcile", res, step)[0]

    def verify_clean(self, res, step):
        result, residue = self._play("verify_clean", "verify_clean", res, step)
        residue = tuple(residue or ())
        if residue:
            return RunResult(ok=False, error=result.error or "residue found after cleanup",
                             residue=residue)
        return result                     # a run that could not verify is not clean either

    def tagged_vms(self):
        if not self.s.deploy_vms:                    # no VMs are made: no XCP-ng host to ask
            return []
        result, names = self._play("orphans", "orphans", None, lambda *a, **k: None)
        if not result.ok:
            raise RunnerError(f"could not list the testbed's VMs: {result.error}")
        return list(names or [])

    def destroy_vms(self, names, step):
        return self._play("orphans", "orphans", None, step, {"daolab_vm_names": list(names)})[0]


def ansible_playbook_path() -> str | None:
    """The ansible-playbook of this interpreter's environment (.venv/bin), else the one on PATH."""
    import shutil
    import sys
    here = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    if os.access(here, os.X_OK):
        return here
    return shutil.which("ansible-playbook")


def dpu_app(roles: Mapping) -> str:
    """The DAO app of a use case's `on: dpu` role (selects playbooks/apps/<app>/), or "none"."""
    for spec in (roles or {}).values():
        if isinstance(spec, Mapping) and spec.get("on") == "dpu" and spec.get("app"):
            return str(spec["app"])
    return "none"


ANSIBLE_SERVICE_NAME = "daolab-ansible"


def ansible_env(settings=None, traceparent: str | None = None) -> dict[str, str]:
    """The environment of an ansible-playbook run (B-63, used by the ansible runner, B-84).

    Always points ANSIBLE_CONFIG at playbooks/ansible.cfg. With DAOLAB_OTEL_ENDPOINT set it
    also switches on the community.general.opentelemetry callback (DAOLAB_ANSIBLE_OTEL=true),
    tells it where the collector is, and passes the job span's TRACEPARENT (B-62), so each
    Ansible task becomes a child span of the job. With the endpoint unset nothing OTel is set
    and the callback stays off."""
    from . import otel
    s = settings or config.settings
    env = {"ANSIBLE_CONFIG": os.path.join(s.playbooks_dir, "ansible.cfg"),
           "ANSIBLE_NOCOLOR": "1", "ANSIBLE_RETRY_FILES_ENABLED": "0"}
    become_file = (getattr(s, "become_password_file", "") or "").strip()
    if become_file:                 # sudo password for the runbooks' account, never on argv / in logs
        env["ANSIBLE_BECOME_PASSWORD_FILE"] = os.path.abspath(os.path.expanduser(become_file))
    endpoint = (s.otel_endpoint or "").strip()
    if not endpoint:
        return env
    target, insecure = otel._grpc_target(endpoint)
    env.update({
        "DAOLAB_ANSIBLE_OTEL": "true",
        "OTEL_SERVICE_NAME": ANSIBLE_SERVICE_NAME,
        "OTEL_EXPORTER_OTLP_ENDPOINT": ("http://" if insecure else "https://") + target,
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_INSECURE": "true" if insecure else "false",
    })
    if traceparent:
        env["TRACEPARENT"] = traceparent
    return env


def get_runner(settings=None) -> Runner:
    """The backend named by DAOLAB_RUNNER (`settings` defaults to the live config)."""
    s = settings or config.settings
    if s.runner == "mock":
        # B-68: only run the fake exporters when the telemetry stack is switched on, so a
        # plain `DAOLAB_RUNNER=mock` (and the tests) stay side-effect free (plan/09 §13).
        factory = None
        if (s.otel_endpoint or "").strip():
            factory = lambda rid: MockExporter(rid, targets_dir=OTEL_TARGETS_DIR)
        # B-44: with the internal secret set (dev.sh writes one) the mock deploy leases its
        # addresses and posts its VM IPs through the real API, like deploy.yml.
        api = InternalApi(s.api_url, s.internal_secret) if s.internal_secret else None
        return MockRunner(step_sec=s.mock_step_sec, fail=parse_mock_fail(s.mock_fail),
                          exporter_factory=factory, api=api)
    if s.runner == "ansible":
        return AnsibleRunner()
    raise RunnerError(f"unknown DAOLAB_RUNNER {s.runner!r}; expected one of {RUNNER_NAMES}")


def job_sink(conn, job_id: int) -> StepSink:
    """A step sink that records every step on the job row (it must be `running`)."""
    def step(name: str, state: str = "ok", detail: str | None = None) -> None:
        models.append_job_step(conn, job_id, name, state, detail)
    return step


def method_for(runner: Runner, kind: str):
    """The runner function that executes a job of this kind."""
    if kind == "deploy":
        return runner.deploy
    if kind == "cleanup":
        return runner.cleanup
    if kind == "health_probe":
        return runner.probe
    if kind == "reconcile":
        return runner.reconcile
    raise RunnerError(f"unknown job kind {kind!r}")
