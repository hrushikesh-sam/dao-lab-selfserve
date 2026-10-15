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
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from . import config, models

RUNNER_NAMES = ("ansible", "mock")
MOCK_FAIL_TARGETS = ("deploy", "cleanup")

# The steps the mock walks through (names follow plan/09 §6 and §9).
DEPLOY_STEPS = ("lease_ips", "dpu_prepare", "create_vms", "post_vm_ips", "start_app", "verify")
CLEANUP_STEPS = ("terminate_sessions", "destroy_vms", "unbind_vfs", "stop_app")
VERIFY_CLEAN_STEPS = ("check_vms", "check_vfs", "check_sessions", "check_ips")
PROBE_STEPS = ("ping_vms", "check_dpu_app")
FAIL_AT_DEPLOY_STEP = "create_vms"       # where DAOLAB_MOCK_FAIL=deploy breaks the deploy


class RunnerError(Exception):
    """A configuration problem (unknown runner, bad DAOLAB_MOCK_FAIL) or an unsupported job."""


@dataclass(frozen=True)
class RunResult:
    """Outcome of one runner call. For `verify_clean`, ok means *clean*: `residue` is then
    empty; otherwise it lists what is left (VM names, leased IPs, ...)."""
    ok: bool
    error: str | None = None
    residue: tuple[str, ...] = ()


StepSink = Callable[..., None]           # step(name, state="ok", detail=None)


class Runner(Protocol):
    def deploy(self, res: Mapping, step: StepSink) -> RunResult: ...
    def cleanup(self, res: Mapping, step: StepSink) -> RunResult: ...
    def probe(self, res: Mapping, step: StepSink) -> RunResult: ...
    def verify_clean(self, res: Mapping, step: StepSink) -> RunResult: ...


def parse_mock_fail(text: str) -> frozenset[str]:
    """``DAOLAB_MOCK_FAIL`` is empty or a comma-separated subset of deploy / cleanup."""
    names = [n.strip() for n in (text or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in MOCK_FAIL_TARGETS]
    if unknown:
        raise RunnerError(f"unknown DAOLAB_MOCK_FAIL value(s) {unknown}; "
                          f"expected a comma-separated subset of {MOCK_FAIL_TARGETS}")
    return frozenset(names)


class MockRunner:
    """Sleeps `step_sec` through every step and records it.

    ``fail={"deploy"}`` makes `deploy` fail at the `create_vms` step. ``fail={"cleanup"}``
    makes cleanup *look* fine while leaving residue behind, so `verify_clean` reports it
    every time — the way to exercise the quarantine path (plan/09 §13)."""

    def __init__(self, step_sec: float = 0.0, fail: frozenset[str] = frozenset(),
                 sleep: Callable[[float], None] = time.sleep):
        if step_sec < 0:
            raise RunnerError(f"DAOLAB_MOCK_STEP_SEC must be >= 0, got {step_sec}")
        self.step_sec = step_sec
        self.fail = frozenset(fail)
        self._sleep = sleep

    def _work(self, name: str, step: StepSink) -> None:
        self._sleep(self.step_sec)
        step(name)

    def deploy(self, res: Mapping, step: StepSink) -> RunResult:
        for name in DEPLOY_STEPS:
            if "deploy" in self.fail and name == FAIL_AT_DEPLOY_STEP:
                self._sleep(self.step_sec)
                msg = "mock failure injected (DAOLAB_MOCK_FAIL=deploy)"
                step(name, "failed", msg)
                return RunResult(ok=False, error=f"{name}: {msg}")
            self._work(name, step)
        return RunResult(ok=True)

    def cleanup(self, res: Mapping, step: StepSink) -> RunResult:
        for name in CLEANUP_STEPS:
            self._work(name, step)
        return RunResult(ok=True)

    def probe(self, res: Mapping, step: StepSink) -> RunResult:
        for name in PROBE_STEPS:
            self._work(name, step)
        return RunResult(ok=True)

    def verify_clean(self, res: Mapping, step: StepSink) -> RunResult:
        rid = res["id"]
        residue = ((f"vm daolab-r{rid}-backend", f"ip lease of reservation {rid}")
                   if "cleanup" in self.fail else ())
        for name in VERIFY_CLEAN_STEPS:
            self._sleep(self.step_sec)
            if residue and name == "check_vms":
                step(name, "failed", "; ".join(residue))
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


def get_runner(settings=None) -> Runner:
    """The backend named by DAOLAB_RUNNER (`settings` defaults to the live config)."""
    s = settings or config.settings
    if s.runner == "mock":
        return MockRunner(step_sec=s.mock_step_sec, fail=parse_mock_fail(s.mock_fail))
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
        raise RunnerError("reconcile jobs arrive with B-72")
    raise RunnerError(f"unknown job kind {kind!r}")
