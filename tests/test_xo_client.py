"""service/xo.py (B-93) against a fake Xen Orchestra REST API.

The fake answers the calls the client makes with the shapes XO 5.211.1 returned on the lab
(2026-11-01): cookie auth, create_vm?sync=true -> {"id"}, 204 on PATCH / actions / DELETE,
`$pool` on every listed object, guest addresses under `0/ipv4/0`.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from service import xo

TOKEN = "tok-123"
POOL, OTHER = "pool-a", "pool-b"


class FakeXo:
    def __init__(self):
        self.calls = []
        self.templates = [{"id": "tpl-1", "name_label": "tpl-ubuntu2204-dao", "$pool": POOL},
                          {"id": "tpl-x", "name_label": "tpl-ubuntu2204-dao", "$pool": OTHER}]
        self.networks = [{"id": "net-sriov", "name_label": "SRIOV-E810", "$pool": POOL}]
        self.vms = {}

    def handle(self, h, method):
        url = urlparse(h.path)
        path, q = url.path.removeprefix("/rest/v0"), parse_qs(url.query)
        body = None
        if int(h.headers.get("Content-Length") or 0):
            body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
        self.calls.append((method, path, q, body))
        if h.headers.get("Cookie") != f"authenticationToken={TOKEN}":
            return 401, {"error": "authentication required"}
        if method == "GET" and path == "/vm-templates":
            return 200, self.templates
        if method == "GET" and path == "/networks":
            return 200, self.networks
        if method == "GET" and path == "/vms":
            return 200, [{"id": k, "name_label": v["name_label"], "power_state": v["power_state"],
                          "addresses": v["addresses"], "tags": v["tags"], "$pool": POOL}
                         for k, v in self.vms.items()]
        if method == "POST" and path == f"/pools/{POOL}/actions/create_vm":
            vm_id = f"vm-{len(self.vms) + 1}"
            self.vms[vm_id] = {"name_label": body["name_label"], "power_state": "Halted",
                               "addresses": {}, "tags": [], "xenStoreData": {},
                               "memory": {"dynamic": [2147483648, 2147483648],
                                          "static": [134217728, 4294967296]},
                               "create": body}
            return 201, {"id": vm_id}
        parts = path.strip("/").split("/")
        if parts[0] == "vms" and len(parts) >= 2 and parts[1] in self.vms:
            vm = self.vms[parts[1]]
            if method == "GET" and len(parts) == 2:
                return 200, {"id": parts[1], **vm}
            if method == "PATCH":
                for k, v in body.items():
                    if k == "xenStoreData":
                        vm["xenStoreData"].update(v)
                    elif k == "memoryStaticMax":
                        vm["memory"]["static"][1] = v
                    else:
                        vm[k] = v
                return 204, None
            if method == "POST" and parts[2:] == ["actions", "start"]:
                vm["power_state"] = "Running"
                vm["addresses"] = {"0/ipv4/0": vm["xenStoreData"].get("vm-data/ip", "")}
                return 204, None
            if method == "POST" and parts[2:] == ["actions", "hard_shutdown"]:
                vm["power_state"] = "Halted"
                return 204, None
            if method == "DELETE":
                del self.vms[parts[1]]
                return 204, None
        return 404, {"error": "no such object"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = FakeXo()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self, method):
            code, payload = state.handle(self, method)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            if data:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def do_PATCH(self):
            self._do("PATCH")

        def do_DELETE(self):
            self._do("DELETE")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "xo-token"
    tok.write_text(TOKEN + "\n")
    monkeypatch.setenv("DAOLAB_XO_URL", f"http://127.0.0.1:{srv.server_port}/rest/v0")
    monkeypatch.setenv("DAOLAB_XO_TOKEN_FILE", str(tok))
    yield state
    srv.shutdown()


def run(capsys, *argv):
    rc = xo.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


def test_the_token_goes_in_the_cookie_and_a_missing_file_is_named(fake, capsys, monkeypatch):
    assert run(capsys, "template", "--pool", POOL, "--name", "tpl-ubuntu2204-dao")[:2] == (0, "tpl-1")
    monkeypatch.setenv("DAOLAB_XO_TOKEN_FILE", "/nonexistent/xo-token")
    rc, _, err = run(capsys, "vms", "--pool", POOL)
    assert rc == 1 and "cannot read DAOLAB_XO_TOKEN_FILE /nonexistent/xo-token" in err


def test_a_wrong_token_is_an_error_not_an_empty_list(fake, capsys, tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.write_text("nope")
    monkeypatch.setenv("DAOLAB_XO_TOKEN_FILE", str(bad))
    rc, out, err = run(capsys, "vms", "--pool", POOL)
    assert rc == 1 and out == "" and "HTTP 401" in err


def test_lookups_stay_in_their_pool(fake, capsys):
    assert run(capsys, "template", "--pool", OTHER, "--name", "tpl-ubuntu2204-dao")[:2] == (0, "tpl-x")
    rc, _, err = run(capsys, "template", "--pool", POOL, "--name", "nope")
    assert rc == 3 and "template 'nope' is not in pool pool-a" in err
    fake.templates.append({"id": "tpl-2", "name_label": "tpl-ubuntu2204-dao", "$pool": POOL})
    assert run(capsys, "template", "--pool", POOL, "--name", "tpl-ubuntu2204-dao")[0] == 4


def test_create_makes_a_halted_vm_with_fixed_memory_tags_and_its_vf(fake, capsys):
    rc, out, _ = run(capsys, "create", "--pool", POOL, "--template", "tpl-ubuntu2204-dao",
                     "--name", "daolab-r12-sink", "--sriov-network", "SRIOV-E810",
                     "--tag", "daolab", "--tag", "daolab-r12")
    assert rc == 0
    vm_id = json.loads(out)["id"]
    vm = fake.vms[vm_id]
    assert vm["create"] == {"name_label": "daolab-r12-sink", "template": "tpl-1", "boot": False,
                            "autoPoweron": False, "vifs": [{"network": "net-sriov"}]}
    assert vm["memory"]["static"][1] == vm["memory"]["dynamic"][1]   # no Populate-on-Demand
    assert vm["tags"] == ["daolab", "daolab-r12"] and vm["autoPoweron"] is False
    create_call = [c for c in fake.calls if c[1].endswith("/create_vm")][0]
    assert create_call[2] == {"sync": ["true"]}


def test_seed_start_ip_and_destroy(fake, capsys, tmp_path):
    vm_id = json.loads(run(capsys, "create", "--pool", POOL, "--template", "tpl-ubuntu2204-dao",
                           "--name", "daolab-r12-tgen")[1])["id"]
    assert "vifs" not in fake.vms[vm_id]["create"]               # no data plane asked
    assert run(capsys, "ip", "--vm", vm_id)[0] == 1              # not booted: no address yet
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"vm-data/ip": "10.68.2.10", "vm-data/daolab/user-data": "eA=="}))
    assert run(capsys, "seed", "--vm", vm_id, "--file", str(seed))[0] == 0
    assert fake.vms[vm_id]["xenStoreData"]["vm-data/ip"] == "10.68.2.10"
    assert run(capsys, "start", "--vm", vm_id)[0] == 0
    assert run(capsys, "power", "--vm", vm_id)[:2] == (0, "Running")
    assert run(capsys, "ip", "--vm", vm_id)[:2] == (0, "10.68.2.10")
    listed = json.loads(run(capsys, "vms", "--pool", POOL, "--prefix", "daolab-r12-")[1])
    assert listed == [{"id": vm_id, "name": "daolab-r12-tgen", "power": "Running",
                       "ip": "10.68.2.10", "tags": []}]
    assert run(capsys, "destroy", "--vm", vm_id)[0] == 0
    methods = [(m, p) for m, p, _, _ in fake.calls[-3:]]
    assert methods == [("GET", f"/vms/{vm_id}"), ("POST", f"/vms/{vm_id}/actions/hard_shutdown"),
                       ("DELETE", f"/vms/{vm_id}")]
    assert fake.vms == {}


def test_vms_filters_by_exact_name_or_prefix(fake, capsys):
    for n in ("daolab-r1-a", "daolab-r12-a", "mine"):
        run(capsys, "create", "--pool", POOL, "--template", "tpl-ubuntu2204-dao", "--name", n)
    names = lambda *a: [v["name"] for v in json.loads(run(capsys, "vms", "--pool", POOL, *a)[1])]  # noqa: E731
    assert names("--prefix", "daolab-r1-") == ["daolab-r1-a"]
    assert names("--name", "mine") == ["mine"]
    assert names() == ["daolab-r1-a", "daolab-r12-a", "mine"]


def test_first_ipv4_prefers_the_first_nic():
    assert xo.first_ipv4({"1/ipv4/0": "10.65.0.36", "0/ipv4/0": "10.68.2.10"}) == "10.68.2.10"
    assert xo.first_ipv4({"0/ipv6/0": "fe80::1", "1/ipv4/0": "10.65.0.36"}) == "10.65.0.36"
    assert xo.first_ipv4({"0/ipv6/0": "fe80::1"}) == ""


def test_the_self_signed_certificate_is_only_accepted_on_loopback(monkeypatch, tmp_path):
    tok = tmp_path / "t"
    tok.write_text("x")
    monkeypatch.setenv("DAOLAB_XO_TOKEN_FILE", str(tok))
    monkeypatch.delenv("DAOLAB_XO_INSECURE", raising=False)
    monkeypatch.setenv("DAOLAB_XO_URL", "https://127.0.0.1:8443/rest/v0")
    assert xo.Xo.from_env().ctx is not None
    monkeypatch.setenv("DAOLAB_XO_URL", "https://xo.lab.example/rest/v0")
    assert xo.Xo.from_env().ctx is None
