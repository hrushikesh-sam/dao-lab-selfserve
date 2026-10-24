"""B-67: the engine evaluates health for active reservations every tick, keeps the latest
snapshot, and fires the webhook once per flip to `down` (plan/09 §8)."""
import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from service import config, engine, models
from service.runner import MockRunner
from service.scheduler import SchedConfig
from tests.conftest import NOW, T, mk_res, mk_setup, mk_usecase

RULES = [{"name": "up", "promql": 'min(up{daolab_reservation="$RID"})', "expect": "== 1"},
         {"name": "rate", "promql": 'rate(rx{daolab_reservation="$RID"}[1m])', "expect": "> 0",
          "severity": "degraded"}]


@pytest.fixture
def lab(db, monkeypatch):
    sid = mk_setup(db, "tb", ["wire"])
    uc = mk_usecase(db, "uc", ["wire"])
    models.update(db, "usecases", uc, {"health": models.to_json(RULES)})
    fired = []
    monkeypatch.setattr(engine, "post_webhook", fired.append)
    return {"db": db, "sid": sid, "uc": uc, "fired": fired}


class Prom:
    """A stub Prometheus whose answers the test flips between ticks."""
    def __init__(self, up=1.0, rate=1.0, fresh=True):
        self.up, self.rate, self.fresh = up, rate, fresh

    def __call__(self, promql):
        t = NOW.timestamp()
        if "timestamp" in promql:
            return [({}, t, t)] if self.fresh else []
        return [({}, t, self.up if "up{" in promql else self.rate)]


def book(lab, state="active", start=T(8), end=T(12)):
    return mk_res(lab["db"], lab["sid"], lab["uc"], start, end, state=state)


def tick(lab, prom, now=NOW):
    return engine.tick(lab["db"], now, MockRunner(), SchedConfig(), health_query=prom)


def snap(lab, rid):
    return models.health_snapshot(lab["db"], rid)


def test_an_active_reservation_gets_a_snapshot_every_tick(lab):
    rid = book(lab)
    out = tick(lab, Prom())
    s = snap(lab, rid)
    assert out.health_checked == 1 and s["verdict"] == "healthy"
    assert [c["name"] for c in models.from_json(s["checks"])] == ["up", "rate"]
    first_eval = s["evaluated_at"]
    tick(lab, Prom(rate=0.0))
    s = snap(lab, rid)
    assert s["verdict"] == "degraded" and s["reason"] == "failing: rate"
    assert s["evaluated_at"] >= first_eval
    assert lab["db"].execute("SELECT COUNT(*) FROM health_snapshots").fetchone()[0] == 1


def test_a_flip_to_down_fires_the_webhook_once_not_every_tick(lab):
    rid = book(lab)
    prom = Prom()
    tick(lab, prom)                                         # healthy
    prom.up = 0.0
    out = tick(lab, prom)                                   # healthy -> down: fire
    assert out.went_down == 1 and len(lab["fired"]) == 1
    tick(lab, prom)                                         # still down: quiet
    tick(lab, prom)
    assert len(lab["fired"]) == 1
    [payload] = lab["fired"]
    assert payload["event"] == "reservation.health_down" and payload["reservation_id"] == rid
    assert payload["previous"] == "healthy" and payload["health"]["verdict"] == "down"
    assert payload["usecase"] == "uc" and payload["setup"] == "tb"


def test_down_again_after_recovering_fires_again(lab):
    book(lab)
    prom = Prom(up=0.0)
    tick(lab, prom)                                         # first verdict is down: fire
    prom.up = 1.0
    tick(lab, prom)                                         # recovered
    prom.up = 0.0
    tick(lab, prom)                                         # down again: fire
    assert len(lab["fired"]) == 2 and lab["fired"][0]["previous"] is None


def test_changed_at_moves_only_when_the_verdict_changes(lab):
    rid = book(lab)
    tick(lab, Prom())
    changed = snap(lab, rid)["changed_at"]
    lab["db"].execute("UPDATE health_snapshots SET changed_at = '2000-01-01T00:00:00Z'")
    tick(lab, Prom())
    assert snap(lab, rid)["changed_at"] == "2000-01-01T00:00:00Z"   # same verdict: kept
    tick(lab, Prom(fresh=False))
    assert snap(lab, rid)["verdict"] == "unknown"
    assert snap(lab, rid)["changed_at"] >= changed


@pytest.mark.parametrize("st", ["queued", "provisioning", "ready", "cleaning", "released"])
def test_only_active_reservations_are_evaluated(lab, st):
    rid = book(lab, state=st, start=T(10), end=T(12))
    out = tick(lab, Prom(up=0.0))
    assert out.health_checked == 0 and snap(lab, rid) is None and lab["fired"] == []


def test_without_the_telemetry_stack_the_step_is_skipped(lab, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, otel_endpoint=""))
    rid = book(lab)
    out = engine.tick(lab["db"], NOW, MockRunner(), SchedConfig())
    assert out.health_checked == 0 and snap(lab, rid) is None


def test_one_broken_rule_set_does_not_stop_the_others(lab):
    bad_uc = mk_usecase(lab["db"], "bad", ["wire"])
    models.update(lab["db"], "usecases", bad_uc, {"health": '[{"name": "x"}]'})   # no promql
    bad = mk_res(lab["db"], lab["sid"], bad_uc, T(8), T(12), state="active")
    good = book(lab)
    out = tick(lab, Prom())
    assert out.health_checked == 1
    assert snap(lab, bad) is None and snap(lab, good)["verdict"] == "healthy"


def test_a_reservation_that_ends_this_tick_is_not_evaluated(lab):
    rid = book(lab, start=T(7), end=T(9))                   # end_at == now: goes to cleaning
    out = tick(lab, Prom(up=0.0))
    assert out.expired == 1 and out.health_checked == 0 and lab["fired"] == []
    assert snap(lab, rid) is None


# ---- post_webhook ------------------------------------------------------------------------

@pytest.fixture
def hook():
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/hook", got
    httpd.shutdown()
    httpd.server_close()


def test_post_webhook_posts_json(hook, monkeypatch):
    url, got = hook
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, webhook_url=url))
    engine.post_webhook({"event": "x", "reservation_id": 1})
    assert got == [{"event": "x", "reservation_id": 1}]


def test_post_webhook_swallows_failures_and_is_a_noop_when_unset(monkeypatch, caplog):
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, webhook_url="http://127.0.0.1:1/x"))
    engine.post_webhook({"reservation_id": 3})                      # refused: logged only
    assert "failed" in caplog.text
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, webhook_url=""))
    engine.post_webhook({"reservation_id": 3})                      # unset: nothing at all
