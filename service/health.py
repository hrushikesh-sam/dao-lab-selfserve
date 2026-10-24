"""Health verdicts from OpenTelemetry data (B-65, plan/09 §8).

Health rules are data: each use case's YAML `health:` block (validated by `daolab sync`, B-64,
stored on the use case row) lists PromQL checks in which `$RID` stands for the reservation id.
`evaluate` runs them through the Prometheus HTTP API and folds the results into one verdict:

  * ``healthy``  every check passes;
  * ``degraded`` only checks with ``severity: degraded`` fail;
  * ``down``     a critical check (the default severity) fails. A check that returns no data
                 fails: the thing it measures is not reporting;
  * ``unknown``  there is no telemetry for the reservation at all, or none newer than
                 DAOLAB_HEALTH_STALE_SEC (the agent died or was never installed), Prometheus
                 cannot be queried, or the use case declares no rules.

"Telemetry for the reservation" is any series carrying its `daolab_reservation` label. Its
freshness is the newest sample time, read with one extra query (`FRESHNESS_QUERY`).

`evaluate` takes the query function as an argument, so it is pure apart from that call; tests
pass a stub. `prometheus(url)` builds the real one.
"""
import json
import math
import operator
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

VERDICTS = ("healthy", "degraded", "down", "unknown")
FRESHNESS_QUERY = 'max(timestamp({daolab_reservation="$RID"}))'
_EXPECT = re.compile(r"^\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
_OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le,
        ">": operator.gt, "<": operator.lt}

Sample = tuple[dict, float, float]          # (labels, sample time in unix seconds, value)
Query = Callable[[str], list[Sample]]


class PrometheusError(Exception):
    """Prometheus could not answer a query (unreachable, HTTP error, query error)."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    promql: str                 # with $RID already substituted
    expect: str
    severity: str               # critical | degraded
    state: str                  # pass | fail | no_data | error
    value: float | None = None  # the deciding value (the first failing series, else the first)
    at: str | None = None       # sample time of `value`, UTC ISO
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "pass"


@dataclass(frozen=True)
class Verdict:
    verdict: str
    checks: list[CheckResult] = field(default_factory=list)
    evaluated_at: str | None = None
    last_seen: str | None = None    # newest sample of any series of the reservation
    reason: str | None = None       # why the verdict is unknown / down / degraded


def _iso(ts: float | datetime) -> str:
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_expect(text: str) -> tuple[str, float]:
    m = _EXPECT.match(text or "")
    if m is None:
        raise ValueError(f"bad expect {text!r}; want '<op> <number>'")
    return m.group(1), float(m.group(2))


def passes(value: float, expect: str) -> bool:
    """`value` satisfies `expect` (e.g. '== 1', '> 0'); NaN never does."""
    op, number = parse_expect(expect)
    return not math.isnan(value) and _OPS[op](value, number)


def substitute(promql: str, rid: int) -> str:
    return promql.replace("$RID", str(int(rid)))


def _check(rule: dict, rid: int, query: Query) -> CheckResult:
    promql = substitute(rule["promql"], rid)
    base = dict(name=rule["name"], promql=promql, expect=rule["expect"],
                severity=rule.get("severity") or "critical")
    try:
        samples = query(promql)
    except PrometheusError as e:
        return CheckResult(**base, state="error", error=str(e))
    if not samples:
        return CheckResult(**base, state="no_data")
    failing = [s for s in samples if not passes(s[2], rule["expect"])]
    _, ts, value = failing[0] if failing else samples[0]
    return CheckResult(**base, state="fail" if failing else "pass", value=value, at=_iso(ts))


def evaluate(rules: list[dict], rid: int, query: Query, now: datetime,
             stale_sec: float) -> Verdict:
    """Run every rule for reservation `rid` and fold them into one Verdict."""
    when = _iso(now)
    if not rules:
        return Verdict("unknown", evaluated_at=when, reason="the use case declares no health rules")
    try:
        fresh = query(substitute(FRESHNESS_QUERY, rid))
    except PrometheusError as e:
        return Verdict("unknown", evaluated_at=when, reason=f"prometheus: {e}")
    newest = max((s[2] for s in fresh if not math.isnan(s[2])), default=None)
    checks = [_check(rule, rid, query) for rule in rules]
    last_seen = _iso(newest) if newest is not None else None
    if newest is None:
        return Verdict("unknown", checks, when, None,
                       f"no telemetry for reservation {rid}")
    age = now.timestamp() - newest
    if age > stale_sec:
        return Verdict("unknown", checks, when, last_seen,
                       f"no telemetry for {int(age)}s (stale after {int(stale_sec)}s)")
    if any(c.state == "error" for c in checks):
        bad = ", ".join(c.name for c in checks if c.state == "error")
        return Verdict("unknown", checks, when, last_seen, f"query failed: {bad}")
    critical = [c.name for c in checks if not c.ok and c.severity == "critical"]
    degraded = [c.name for c in checks if not c.ok and c.severity == "degraded"]
    if critical:
        return Verdict("down", checks, when, last_seen, f"failing: {', '.join(critical)}")
    if degraded:
        return Verdict("degraded", checks, when, last_seen, f"failing: {', '.join(degraded)}")
    return Verdict("healthy", checks, when, last_seen)


def verdict_object(v: Verdict) -> dict:
    """The JSON shape of a verdict (GET /reservations/{id}/health, health snapshots)."""
    return {"verdict": v.verdict, "reason": v.reason, "evaluated_at": v.evaluated_at,
            "last_seen": v.last_seen,
            "checks": [{"name": c.name, "state": c.state, "ok": c.ok, "severity": c.severity,
                        "expect": c.expect, "value": c.value, "at": c.at, "promql": c.promql,
                        "error": c.error} for c in v.checks]}


# ---- Prometheus HTTP API ---------------------------------------------------------------

def _samples(data: dict) -> list[Sample]:
    kind, result = data.get("resultType"), data.get("result")
    if kind == "vector":
        return [(r.get("metric", {}), float(r["value"][0]), float(r["value"][1])) for r in result]
    if kind == "scalar":
        return [({}, float(result[0]), float(result[1]))]
    raise PrometheusError(f"unsupported result type {kind!r} (want an instant vector)")


def prometheus(base_url: str, timeout: float = 5.0) -> Query:
    """A query function over `GET <base_url>/api/v1/query`; raises PrometheusError."""
    base = base_url.rstrip("/")

    def query(promql: str) -> list[Sample]:
        url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except ValueError:
                raise PrometheusError(f"HTTP {e.code} from {base}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PrometheusError(f"{base} unreachable: {getattr(e, 'reason', e)}") from None
        if body.get("status") != "success":
            raise PrometheusError(f"{body.get('errorType', 'error')}: {body.get('error', body)}")
        return _samples(body.get("data", {}))

    return query


def evaluate_reservation(conn, res_id: int, now: datetime, query: Query | None = None,
                         stale_sec: float | None = None) -> Verdict:
    """Evaluate one reservation's use-case rules (read from the DB, B-64 follow-up)."""
    from . import config, models
    res = models.reservation_by_id(conn, res_id)
    uc = models.usecase_by_id(conn, res["usecase_id"])
    rules = models.from_json(uc["health"]) if uc["health"] else []
    if query is None:
        query = prometheus(config.settings.prometheus_url)
    if stale_sec is None:
        stale_sec = config.settings.health_stale_sec
    return evaluate(rules, res_id, query, now, stale_sec)
