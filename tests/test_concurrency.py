"""B-17: two users clicking at the same instant (plan/09 §5, race safety).

Placement and insert share one BEGIN IMMEDIATE transaction in `POST /reservations`, so
concurrent bookings of one window serialise on sqlite's write lock: exactly one wins an `at`
window, and `asap` bookings racing for the testbed get disjoint windows. Every thread has its
own TestClient, so each request gets its own DB connection, as in production."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from service import api, models

N = 12


def _tomorrow(hour):
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1, hours=hour)


def _race(env, bodies):
    """POST every body at once from its own thread / client / user; returns the responses."""
    tokens = [env.add_user(f"racer{i}") for i in range(len(bodies))]
    barrier = threading.Barrier(len(bodies))
    out = [None] * len(bodies)

    def worker(i):
        client = TestClient(api.app, raise_server_exceptions=False)
        barrier.wait()
        out[i] = client.post("/reservations", json=bodies[i],
                             headers={"Authorization": f"Bearer {tokens[i]}"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(bodies))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def _live(env):
    return env.conn.execute(
        "SELECT * FROM reservations WHERE state NOT IN ('released', 'failed') "
        "ORDER BY start_at").fetchall()


@pytest.mark.parametrize("round_", range(10))
def test_n_threads_booking_one_at_window_exactly_one_wins(api_env, round_):
    start = _tomorrow(10)
    body = {"usecase": "vpp", "mode": "at", "start_at": models.format_ts(start), "hours": 2}
    resps = _race(api_env, [body] * N)
    codes = sorted(r.status_code for r in resps)
    assert codes == [201] + [409] * (N - 1)
    losers = [r.json()["error"] for r in resps if r.status_code == 409]
    assert {e["code"] for e in losers} == {"slot_conflict"}
    assert all(e["detail"]["alternatives"] for e in losers)        # every loser gets chips
    [row] = _live(api_env)
    assert (row["start_at"], row["end_at"]) == (models.format_ts(start),
                                                models.format_ts(start + timedelta(hours=2)))


def test_n_threads_booking_asap_get_disjoint_windows(api_env):
    start = _tomorrow(10)
    body = {"usecase": "vpp", "start_at": models.format_ts(start), "hours": 1}
    resps = _race(api_env, [body] * N)
    assert [r.status_code for r in resps] == [201] * N
    rows = _live(api_env)
    assert len(rows) == N
    for a, b in zip(rows, rows[1:]):                                # same class: 10 + 5 apart
        gap = models.parse_ts(b["start_at"]) - models.parse_ts(a["end_at"])
        assert gap >= timedelta(minutes=15)
