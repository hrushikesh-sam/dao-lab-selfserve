"""B-44: the mock runner calls the real /internal/leases and /internal/vm-ip, so a mock deploy
shows leased IPs on the reservation (plan/09 §13)."""
import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from service import config, engine, models, runner
from service.runner import InternalApi, MockRunner, RunnerError
from service.scheduler import SchedConfig


class ClientApi(InternalApi):
    """InternalApi over the FastAPI TestClient instead of a socket."""
    def __init__(self, client, secret="test-secret"):
        super().__init__("http://testserver", secret)
        self.client = client

    def post(self, path, body):
        r = self.client.post(path, json=body, headers={"X-Daolab-Secret": self.secret})
        if r.status_code >= 400:
            err = r.json()["error"]
            raise RunnerError(f"{path}: {r.status_code} {err['code']}: {err['message']}")
        return r.json()


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def booked(api_env):
    """alice books vpp now; the engine will provision it on the next tick."""
    r = api_env.client.post("/reservations", headers=H(api_env.tokens["alice"]),
                            json={"usecase": "vpp", "hours": 1})
    assert r.status_code == 201, r.text
    return api_env, r.json()


def provision(env, res, rn):
    """Tick at the reservation's start (lead long passed): queued -> provisioning -> deploy."""
    start = models.parse_ts(res["start_at"])
    return engine.tick(env.conn, start, rn, SchedConfig.from_settings(config.settings))


def test_a_mock_deploy_leases_and_posts_every_vm_ip(booked):
    env, res = booked
    out = provision(env, res, MockRunner(api=ClientApi(env.client)))
    assert out.ready == 1
    got = env.client.get(f"/reservations/{res['id']}", headers=H(env.tokens["alice"])).json()
    vms = {v["role"]: v for v in got["vms"]}
    assert (vms["sink"]["ip"], vms["tgen"]["ip"]) == ("10.68.1.0", "10.68.1.1")  # role order
    assert vms["tgen"]["ssh"] == "ssh alice@10.68.1.1"
    assert vms["vpp"]["ip"] == "10.68.0.190"           # the DPU is static: its own address, never leased
    assert sorted((l["role"], l["ip"], l["state"]) for l in got["leases"]) == [
        ("sink", "10.68.1.0", "leased"), ("tgen", "10.68.1.1", "leased")]
    [job] = models.jobs_for_reservation(env.conn, res["id"])
    steps = {s["name"]: s for s in models.job_steps(job)}
    assert steps["lease_ips"]["detail"] == "sink=10.68.1.0, tgen=10.68.1.1"
    assert steps["post_vm_ips"]["detail"] == "sink=10.68.1.0, tgen=10.68.1.1"


def test_a_replayed_deploy_keeps_its_addresses(booked):
    env, res = booked
    rn = MockRunner(api=ClientApi(env.client))
    provision(env, res, rn)
    rid = res["id"]
    models.create_job(env.conn, rid, "deploy", attempt=2)              # a redeploy
    engine.tick(env.conn, models.parse_ts(res["start_at"]), rn, SchedConfig())
    leases = models.ip_leases_for_reservation(env.conn, rid)
    assert sorted(l["ip"] for l in leases) == ["10.68.1.0", "10.68.1.1"]


def test_two_mock_deploys_never_share_an_address(api_env):
    ids = []
    for who in ("alice", "bob"):
        r = api_env.client.post("/reservations", headers=H(api_env.tokens[who]),
                                json={"usecase": "vpp", "hours": 1})
        ids.append(r.json())
    rn = MockRunner(api=ClientApi(api_env.client))
    for res in ids:                                                    # each in its own slot
        provision(api_env, res, rn)
        engine.tick(api_env.conn, models.parse_ts(res["end_at"]), rn, SchedConfig())
    all_ips = [l["ip"] for r in ids for l in models.ip_leases_for_reservation(api_env.conn, r["id"])]
    assert len(all_ips) == 4
    live_at_once = [l for r in ids for l in models.ip_leases_for_reservation(api_env.conn, r["id"])
                    if l["state"] == "leased"]
    assert len({l["ip"] for l in live_at_once}) == len(live_at_once)


def test_a_refused_lease_fails_the_deploy_at_lease_ips(booked):
    env, res = booked
    env.conn.execute("UPDATE ip_pools SET range_end = range_start")    # one address for two VMs
    out = provision(env, res, MockRunner(api=ClientApi(env.client)))
    assert out.retried == 1
    [job, retry] = models.jobs_for_reservation(env.conn, res["id"])
    assert job["state"] == "failed" and "pool_exhausted" in job["error"]
    assert [s["name"] for s in models.job_steps(job)] == ["lease_ips"]
    assert models.job_steps(job)[0]["state"] == "failed"


def test_without_an_api_the_mock_only_sleeps(booked):
    env, res = booked
    assert provision(env, res, MockRunner()).ready == 1
    assert models.ip_leases_for_reservation(env.conn, res["id"]) == []


def test_get_runner_wires_the_api_only_with_an_internal_secret():
    base = dataclasses.replace(config.settings, runner="mock", otel_endpoint="")
    assert runner.get_runner(dataclasses.replace(base, internal_secret="")).api is None
    api = runner.get_runner(dataclasses.replace(base, internal_secret="s3",
                                                api_url="http://api:8080/")).api
    assert (api.base, api.secret) == ("http://api:8080", "s3")


# ---- InternalApi over a real socket --------------------------------------------------------

@pytest.fixture
def server():
    seen, replies = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["X-Daolab-Secret"], body))
            status, reply = replies.get(self.path, (200, {"ip": "10.68.1.0"}))
            raw = json.dumps(reply).encode() if isinstance(reply, dict) else reply
            self.send_response(status)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", seen, replies
    httpd.shutdown()
    httpd.server_close()


def test_internal_api_posts_json_with_the_secret(server):
    url, seen, _ = server
    api = InternalApi(url, "s3")
    assert api.lease(5, "tgen") == "10.68.1.0"
    api.vm_ip(5, "tgen", "10.68.1.0")
    assert seen == [("/internal/leases", "s3", {"reservation_id": 5, "role": "tgen"}),
                    ("/internal/vm-ip", "s3", {"reservation_id": 5, "role": "tgen",
                                               "ip": "10.68.1.0"})]


def test_internal_api_surfaces_the_api_error(server):
    url, _, replies = server
    replies["/internal/leases"] = (409, {"error": {"code": "pool_exhausted", "message": "full"}})
    with pytest.raises(RunnerError, match="409 pool_exhausted: full"):
        InternalApi(url, "s3").lease(5, "tgen")
    replies["/internal/leases"] = (502, b"bad gateway")
    with pytest.raises(RunnerError, match="HTTP 502"):
        InternalApi(url, "s3").lease(5, "tgen")


def test_internal_api_unreachable():
    with pytest.raises(RunnerError, match="api unreachable"):
        InternalApi("http://127.0.0.1:1", "s3", timeout=1).lease(5, "tgen")
