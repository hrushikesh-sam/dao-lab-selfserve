"""Runner — executes the procedures an engine job stands for (B-22).

plan/09 §6 and §9: a job is one of `deploy`, `cleanup`, `health_probe`; the runner has one
function per procedure plus `verify_clean`, which proves a cleanup left nothing behind.
`DAOLAB_RUNNER` picks the backend:

  * ``mock``    — a fake that sleeps through each step and records it, so the whole flow
                  (booking, deploy, cleanup, quarantine) can be demonstrated with no testbed.
  * ``ansible`` — the real thing, arriving with B-84.

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

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def _metrics_text(self) -> str:
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(self.labels.items()))
        return ("# HELP daolab_mock_up Mock agent liveness (B-68).\n"
                "# TYPE daolab_mock_up gauge\n"
                f'daolab_mock_up{{{label_str}}} 1\n')

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


class MockRunner:
    """Sleeps `step_sec` through every step and records it.

    ``fail={"deploy"}`` makes `deploy` fail at the `create_vms` step. ``fail={"cleanup"}``
    makes cleanup *look* fine while leaving residue behind, so `verify_clean` reports it
    every time — the way to exercise the quarantine path (plan/09 §13). ``fail={"health"}``
    keeps the per-reservation exporter (B-68) from ever starting, so the health badge stays
    `unknown`. When `exporter_factory` is set, a successful `deploy` starts an exporter and
    `cleanup` stops it."""

    def __init__(self, step_sec: float = 0.0, fail: frozenset[str] = frozenset(),
                 sleep: Callable[[float], None] = time.sleep,
                 exporter_factory: Callable[[int], MockExporter] | None = None):
        if step_sec < 0:
            raise RunnerError(f"DAOLAB_MOCK_STEP_SEC must be >= 0, got {step_sec}")
        self.step_sec = step_sec
        self.fail = frozenset(fail)
        self._sleep = sleep
        self._exporter_factory = exporter_factory
        self._exporters: dict[int, MockExporter] = {}

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

    def deploy(self, res: Mapping, step: StepSink) -> RunResult:
        for name in DEPLOY_STEPS:
            if "deploy" in self.fail and name == FAIL_AT_DEPLOY_STEP:
                self._sleep(self.step_sec)
                msg = "mock failure injected (DAOLAB_MOCK_FAIL=deploy)"
                step(name, "failed", msg)
                return RunResult(ok=False, error=f"{name}: {msg}")
            self._work(name, step)
        self._start_exporter(res)
        return RunResult(ok=True)

    def cleanup(self, res: Mapping, step: StepSink) -> RunResult:
        for name in CLEANUP_STEPS:
            self._work(name, step)
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
    """The real backend (ansible-runner, `deploy.yml` / `cleanup.yml`) arrives with B-84."""

    def _todo(self, what: str):
        raise NotImplementedError(
            f"the ansible runner ({what}) is not implemented yet (B-84); "
            "set DAOLAB_RUNNER=mock to run the flow without a testbed")

    def deploy(self, res, step):
        self._todo("deploy")

    def cleanup(self, res, step):
        self._todo("cleanup")

    def probe(self, res, step):
        self._todo("probe")

    def verify_clean(self, res, step):
        self._todo("verify_clean")

    def reconcile(self, res, step):
        self._todo("reconcile")


def get_runner(settings=None) -> Runner:
    """The backend named by DAOLAB_RUNNER (`settings` defaults to the live config)."""
    s = settings or config.settings
    if s.runner == "mock":
        # B-68: only run the fake exporters when the telemetry stack is switched on, so a
        # plain `DAOLAB_RUNNER=mock` (and the tests) stay side-effect free (plan/09 §13).
        factory = None
        if (s.otel_endpoint or "").strip():
            factory = lambda rid: MockExporter(rid, targets_dir=OTEL_TARGETS_DIR)
        return MockRunner(step_sec=s.mock_step_sec, fail=parse_mock_fail(s.mock_fail),
                          exporter_factory=factory)
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
