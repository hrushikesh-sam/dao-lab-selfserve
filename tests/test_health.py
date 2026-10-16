"""B-65: health.py — YAML rules through PromQL into one verdict (plan/09 §8)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from service import catalog, health, models
from service.health import PrometheusError
from tests.conftest import NOW, REPO, T, mk_res, mk_user

RID = 7
RULES = [
    {"name": "vms_up", "promql": 'min(up{daolab_reservation="$RID"})', "expect": "== 1"},
    {"name": "dpu_app", "promql": 'vpp_up{daolab_reservation="$RID"}', "expect": "== 1"},
    {"name": "datapath", "promql": 'rate(rx{daolab_reservation="$RID"}[1m])', "expect": "> 0",
     "severity": "degraded"},
]
FRESH = 'max(timestamp({daolab_reservation="7"}))'
T0 = NOW.timestamp()


def stub(answers):
    """A query function: promql -> list of samples, or an exception to raise. Unknown -> []."""
    def query(promql):
        out = answers.get(promql, [])
        if isinstance(out, Exception):
            raise out
        return out
    return query


def world(fresh_age=10, vms=1.0, app=1.0, rate=5.0, **over):
    answers = {FRESH: [({}, T0, T0 - fresh_age)],
               'min(up{daolab_reservation="7"})': [({}, T0, vms)],
               'vpp_up{daolab_reservation="7"}': [({"host": "dpu-01"}, T0, app)],
               'rate(rx{daolab_reservation="7"}[1m])': [({}, T0, rate)]}
    answers.update(over)
    return stub(answers)


def ev(query, rules=RULES, stale=120):
    return health.evaluate(rules, RID, query, NOW, stale)


def states(v):
    return {c.name: c.state for c in v.checks}


def test_all_checks_pass_is_healthy():
    v = ev(world())
    assert v.verdict == "healthy" and v.reason is None
    assert states(v) == {"vms_up": "pass", "dpu_app": "pass", "datapath": "pass"}
    assert v.last_seen == "2026-09-01T08:59:50Z" and v.evaluated_at == "2026-09-01T09:00:00Z"
    assert v.checks[0].promql == 'min(up{daolab_reservation="7"})'     # $RID substituted


def test_only_a_degraded_check_failing_is_degraded():
    v = ev(world(rate=0.0))
    assert v.verdict == "degraded" and v.reason == "failing: datapath"


def test_a_critical_check_failing_is_down_even_if_degraded_ones_fail_too():
    v = ev(world(app=0.0, rate=0.0))
    assert v.verdict == "down" and v.reason == "failing: dpu_app"


def test_a_check_without_data_fails():
    v = ev(world(**{'vpp_up{daolab_reservation="7"}': []}))
    assert v.verdict == "down" and states(v)["dpu_app"] == "no_data"


def test_one_failing_series_out_of_many_fails_the_check_and_is_reported():
    v = ev(world(**{'vpp_up{daolab_reservation="7"}': [({"i": "a"}, T0, 1.0),
                                                        ({"i": "b"}, T0, 0.0)]}))
    [app] = [c for c in v.checks if c.name == "dpu_app"]
    assert app.state == "fail" and app.value == 0.0


def test_nan_never_passes():
    assert ev(world(vms=float("nan"))).verdict == "down"


def test_stale_telemetry_is_unknown_but_still_lists_the_checks():
    v = ev(world(fresh_age=121))
    assert v.verdict == "unknown" and "stale after 120s" in v.reason
    assert len(v.checks) == 3 and v.last_seen is not None


def test_telemetry_exactly_at_the_stale_limit_still_counts():
    assert ev(world(fresh_age=120)).verdict == "healthy"


def test_no_telemetry_at_all_is_unknown():
    v = ev(world(**{FRESH: []}))
    assert v.verdict == "unknown" and v.last_seen is None and "no telemetry" in v.reason


def test_prometheus_down_is_unknown():
    v = ev(stub({FRESH: PrometheusError("http://prom:9090 unreachable")}))
    assert v.verdict == "unknown" and v.reason.startswith("prometheus:") and v.checks == []


def test_a_failing_rule_query_is_unknown_not_down():
    v = ev(world(**{'vpp_up{daolab_reservation="7"}': PrometheusError("bad_data: parse error")}))
    assert v.verdict == "unknown" and states(v)["dpu_app"] == "error"


def test_no_rules_is_unknown():
    assert ev(world(), rules=[]).verdict == "unknown"


@pytest.mark.parametrize("value,expect,ok", [
    (1, "== 1", True), (0, "== 1", False), (2, "!= 1", True), (0, "> 0", False),
    (0.5, "> 0", True), (3, ">= 3", True), (3, "< 3", False), (-1, "<= -1", True),
    (1.5, "== 1.5", True)])
def test_expect_operators(value, expect, ok):
    assert health.passes(value, expect) is ok


def test_bad_expect_is_rejected():
    with pytest.raises(ValueError):
        health.parse_expect("~ 1")


def test_verdict_object_is_json_ready():
    obj = health.verdict_object(ev(world(rate=0.0)))
    json.dumps(obj)
    assert obj["verdict"] == "degraded"
    assert obj["checks"][2] == {"name": "datapath", "state": "fail", "ok": False,
                                "severity": "degraded", "expect": "> 0", "value": 0.0,
                                "at": "2026-09-01T09:00:00Z",
                                "promql": 'rate(rx{daolab_reservation="7"}[1m])', "error": None}


def test_evaluate_reservation_reads_the_rules_from_the_db(db):
    catalog.seed_usecases(db)
    catalog.sync(db, os.path.join(REPO, "usecases"), os.path.join(REPO, "setups"),
                 os.path.join(REPO, "inventory", "hosts.yml"))
    uid, _ = mk_user(db, "alice")
    rid = mk_res(db, models.setup_by_name(db, "tb-01")["id"],
                 models.usecase_by_name(db, "vpp")["id"], T(8), T(10), "active", user_id=uid)
    asked = []

    def query(promql):
        asked.append(promql)
        return [({}, T0, T0)] if "timestamp" in promql else [({}, T0, 1.0)]

    v = health.evaluate_reservation(db, rid, NOW, query=query, stale_sec=60)
    assert v.verdict == "healthy"
    assert [c.name for c in v.checks] == ["vms_up", "ssh_reachable", "dpu_app", "datapath"]
    assert all(f'daolab_reservation="{rid}"' in q for q in asked)


# ---- the Prometheus HTTP client ---------------------------------------------------------

@pytest.fixture
def prom():
    """A fake Prometheus: answers /api/v1/query from `routes[query]` -> (status, body)."""
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse
            u = urlparse(self.path)
            q = parse_qs(u.query).get("query", [""])[0]
            status, body = routes.get(q, (200, {"status": "success",
                                                "data": {"resultType": "vector", "result": []}}))
            raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", routes
    httpd.shutdown()
    httpd.server_close()


def test_client_parses_an_instant_vector(prom):
    url, routes = prom
    routes["up"] = (200, {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"job": "x"}, "value": [1756717200.5, "1"]}]}})
    assert health.prometheus(url)("up") == [({"job": "x"}, 1756717200.5, 1.0)]


def test_client_parses_a_scalar(prom):
    url, routes = prom
    routes["1+1"] = (200, {"status": "success", "data": {"resultType": "scalar",
                                                          "result": [1756717200, "2"]}})
    assert health.prometheus(url)("1+1") == [({}, 1756717200.0, 2.0)]


def test_client_reports_a_query_error(prom):
    url, routes = prom
    routes["bad("] = (400, {"status": "error", "errorType": "bad_data", "error": "parse error"})
    with pytest.raises(PrometheusError, match="bad_data: parse error"):
        health.prometheus(url)("bad(")


def test_client_reports_a_non_json_error(prom):
    url, routes = prom
    routes["x"] = (502, b"<html>bad gateway</html>")
    with pytest.raises(PrometheusError, match="HTTP 502"):
        health.prometheus(url)("x")


def test_client_reports_an_unreachable_server():
    with pytest.raises(PrometheusError, match="unreachable"):
        health.prometheus("http://127.0.0.1:1", timeout=1)("up")


def test_end_to_end_through_the_client(prom):
    url, routes = prom
    vec = lambda v, ts=T0: (200, {"status": "success", "data": {"resultType": "vector",  # noqa: E731
                                  "result": [{"metric": {}, "value": [ts, str(v)]}]}})
    routes[FRESH] = vec(T0 - 5)
    routes['min(up{daolab_reservation="7"})'] = vec(1)
    routes['vpp_up{daolab_reservation="7"}'] = vec(0)
    routes['rate(rx{daolab_reservation="7"}[1m])'] = vec(3)
    v = health.evaluate(RULES, RID, health.prometheus(url), NOW, 120)
    assert v.verdict == "down" and v.reason == "failing: dpu_app"
