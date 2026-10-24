"""B-81: playbooks/files/daolab-app-exporter — the series the use-case health rules read."""
import importlib.machinery
import importlib.util
import os
import re
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "playbooks", "files", "daolab-app-exporter")


def _load():
    loader = importlib.machinery.SourceFileLoader("daolab_app_exporter", PATH)
    spec = importlib.util.spec_from_loader("daolab_app_exporter", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


exp = _load()

SHOW_INT = """\
              Name               Idx    State  MTU (L3/IP4/IP6/MPLS)     Counter          Count
eth0                              1      up          9000/0/0/0     rx packets                 12345
                                                                    rx bytes                 7654321
                                                                    tx packets                 12340
                                                                    tx bytes                 7650000
                                                                    drops                          5
eth1                              2      up          9000/0/0/0     rx packets                    10
local0                            0     down          0/0/0/0
"""


def series(text):
    return {line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
            for line in text.splitlines() if line and not line.startswith("#")}


def args(**kw):
    import argparse
    base = dict(app="vpp", vppctl="vppctl", vpp_socket="", nginx_addr="127.0.0.1:1",
                stub_status="", process="no-such-process-x", pci="", netdev="",
                sysfs="/nonexistent")
    base.update(kw)
    return argparse.Namespace(**base)


def test_vpp_show_interface_is_parsed_per_interface():
    got = exp.parse_vpp_interfaces(SHOW_INT)
    assert got["eth0"] == {"rx_packets": 12345, "rx_bytes": 7654321, "tx_packets": 12340,
                           "tx_bytes": 7650000}
    assert got["eth1"] == {"rx_packets": 10} and got["local0"] == {}


@pytest.fixture
def fake_vppctl(tmp_path):
    def make(version_rc=0):
        f = tmp_path / "vppctl"
        f.write_text("#!/bin/sh\n"
                     f'[ "$2" = version ] && {{ echo "vpp v24.02"; exit {version_rc}; }}\n'
                     f"cat <<'X'\n{SHOW_INT}X\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
        return str(f)
    return make


def test_vpp_metrics_match_the_vpp_rules(fake_vppctl):
    out = series(exp.render(args(app="vpp", vppctl=fake_vppctl())))
    assert out["vpp_up"] == 1
    assert out['vpp_interface_rx_packets{interface="eth0"}'] == 12345
    assert not any("local0" in k for k in out)
    # the use-case rules: vpp_up{...} and rate(vpp_interface_rx_packets{...}[1m])
    text = open(os.path.join(REPO, "usecases", "vpp.yml")).read()
    for metric in re.findall(r"(vpp_[a-z_]+)\{", text):
        assert any(k.startswith(metric) for k in out), metric


def test_vpp_down_is_zero_and_no_counters(fake_vppctl):
    out = series(exp.render(args(app="vpp", vppctl=fake_vppctl(version_rc=1))))
    assert out == {"vpp_up": 0}


def test_vppctl_missing_is_down():
    assert series(exp.render(args(app="vpp", vppctl="/nonexistent/vppctl"))) == {"vpp_up": 0}


@pytest.fixture
def stub_status():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"Active connections: 3 \nserver accepts handled requests\n"
                    b" 120 120 4567 \nReading: 0 Writing: 1 Waiting: 2 \n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/stub_status"
    httpd.shutdown()
    httpd.server_close()


def _addr(url):
    return url.split("//", 1)[1].split("/", 1)[0]


def test_nginx_is_up_when_its_port_accepts(stub_status):
    out = series(exp.render(args(app="nginx", nginx_addr=_addr(stub_status))))
    assert out == {"nginx_up": 1}


def test_nginx_with_stub_status_adds_the_counters(stub_status):
    out = series(exp.render(args(app="nginx", nginx_addr=_addr(stub_status),
                                 stub_status=stub_status)))
    assert out == {"nginx_up": 1, "nginx_connections_active": 3, "nginx_http_requests_total": 4567}


def test_nginx_down_when_nothing_listens():
    assert series(exp.render(args(app="nginx"))) == {"nginx_up": 0}


def test_smartnic_up_follows_the_process(tmp_path):
    import shutil
    import subprocess
    assert series(exp.render(args(app="smartnic", process="daolabfakeapp"))) == {"smartnic_up": 0}
    fake = tmp_path / "daolabfakeapp"
    shutil.copy(shutil.which("sleep"), fake)
    proc = subprocess.Popen([str(fake), "30"])
    try:
        assert series(exp.render(args(app="smartnic", process="daolabfakeapp"))) == {"smartnic_up": 1}
    finally:
        proc.kill()
        proc.wait()


def test_smartnic_offload_counter_from_a_netdev(tmp_path):
    stats = tmp_path / "sdp0" / "statistics"
    stats.mkdir(parents=True)
    (stats / "rx_packets").write_text("987\n")
    out = series(exp.render(args(app="smartnic", sysfs=str(tmp_path), netdev="sdp0")))
    assert out == {"smartnic_up": 0, 'smartnic_offload_pps{interface="sdp0"}': 987}


def test_the_vpp_socket_is_passed_to_vppctl(tmp_path):
    f = tmp_path / "vppctl"
    f.write_text('#!/bin/sh\n[ "$1" = -s ] && [ "$2" = /run/vpp/cli.sock ] || exit 9\necho ok\n')
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    out = series(exp.render(args(app="vpp", vppctl=str(f), vpp_socket="/run/vpp/cli.sock")))
    assert out["vpp_up"] == 1


def test_the_once_flag_prints_the_exposition(capsys, fake_vppctl):
    exp.main(["--app", "vpp", "--vppctl", fake_vppctl(), "--once"])
    assert "vpp_up 1" in capsys.readouterr().out
