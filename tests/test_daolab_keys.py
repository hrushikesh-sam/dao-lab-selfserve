"""B-52: playbooks/files/daolab-keys (sshd AuthorizedKeysCommand) and the sshd drop-in.

The script runs for real against a fake API; the drop-in is checked with `sshd -T`, which prints
the effective config for a given user. "alice can log in only during her slot" end to end needs
a DPU (or a VM standing in for one) and is part of the testbed bring-up (B-85)."""
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "playbooks", "files", "daolab-keys")
DROPIN = os.path.join(REPO, "playbooks", "files", "sshd-daolab.conf")
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice+key/0= alice@laptop"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")


@pytest.fixture
def api():
    """A fake daolab API; `reply` decides (status, body, delay) per request."""
    state = {"reply": (200, KEY + "\n", 0.0), "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            state["seen"].append((u.path, parse_qs(u.query), self.headers.get("X-Daolab-Secret")))
            status, body, delay = state["reply"]
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def conf(tmp_path, api):
    header = tmp_path / "authz-header"
    header.write_text("X-Daolab-Secret: dpu-secret\n")
    path = tmp_path / "keys.conf"
    path.write_text(f"DAOLAB_API_URL={api['url']}/\nDAOLAB_HOST=dpu-01\n"
                    f"DAOLAB_HEADER_FILE={header}\n")
    return path


def run(conf_path, login="lab", timeout=10):
    t0 = time.monotonic()
    p = subprocess.run([SCRIPT, login], env={**os.environ, "DAOLAB_KEYS_CONF": str(conf_path)},
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, time.monotonic() - t0


def test_it_prints_the_keys_the_backend_allows(api, conf):
    rc, out, _ = run(conf)
    assert (rc, out) == (0, KEY + "\n")
    [(path, query, secret)] = api["seen"]
    assert path == "/internal/authz/keys" and secret == "dpu-secret"
    assert query == {"host": ["dpu-01"], "login": ["lab"]}


def test_no_keys_means_no_output(api, conf):
    api["reply"] = (200, "", 0.0)
    assert run(conf)[:2] == (0, "")


@pytest.mark.parametrize("status", [401, 404, 500])
def test_an_http_error_prints_nothing(api, conf, status):
    api["reply"] = (status, KEY + "\n", 0.0)
    assert run(conf)[:2] == (0, "")


def test_an_unreachable_backend_prints_nothing(tmp_path, conf):
    conf.write_text("DAOLAB_API_URL=http://127.0.0.1:1\nDAOLAB_HOST=dpu-01\n"
                    f"DAOLAB_HEADER_FILE={tmp_path / 'authz-header'}\n")
    assert run(conf)[:2] == (0, "")


def test_a_slow_backend_is_cut_off_after_two_seconds(api, conf):
    api["reply"] = (200, KEY + "\n", 5.0)
    rc, out, took = run(conf)
    assert (rc, out) == (0, "") and took < 4


def test_anything_but_plain_public_keys_is_dropped(api, conf):
    api["reply"] = (200, "\n".join([
        KEY,
        'command="/bin/sh" ' + KEY,                      # options are not passed through
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ== bob",
        "<html>proxy error</html>",
        "ssh-ed25519 not-base64!! x"]) + "\n", 0.0)
    rc, out, _ = run(conf)
    assert out.splitlines() == [KEY, "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ== bob"]


@pytest.mark.parametrize("login", ["", "lab;rm -rf /", "../x", "la b"])
def test_odd_logins_are_refused_without_asking(api, conf, login):
    assert run(conf, login=login)[:2] == (0, "")
    assert api["seen"] == []


def test_a_missing_config_or_header_file_prints_nothing(api, tmp_path, conf):
    assert run(tmp_path / "nope.conf")[:2] == (0, "")
    (tmp_path / "authz-header").unlink()
    assert run(conf)[:2] == (0, "")
    assert api["seen"] == []


def test_the_secret_is_not_on_the_command_line():
    text = open(SCRIPT).read()
    assert '-H "@$header_file"' in text and "X-Daolab-Secret:" not in text.split("CONF=")[1]


# ---- the sshd drop-in -------------------------------------------------------------------

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)


def effective(tmp_path, user):
    key = tmp_path / "hostkey"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    main = tmp_path / "sshd_config"
    main.write_text(f"Include {DROPIN}\nHostKey {key}\nPasswordAuthentication yes\n")
    p = subprocess.run([SSHD, "-T", "-f", str(main), "-C", f"user={user},host=x,addr=10.0.0.9"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"sshd -T unavailable here: {p.stderr.strip()[:200]}")
    return dict(line.split(" ", 1) for line in p.stdout.splitlines() if " " in line)


@pytest.mark.skipif(SSHD is None, reason="sshd not installed")
def test_the_lab_login_is_gated_by_daolab_keys(tmp_path):
    cfg = effective(tmp_path, "lab")
    assert cfg["authorizedkeyscommand"] == "/usr/local/bin/daolab-keys %u"
    assert cfg["authorizedkeyscommanduser"] == "daolab-keys"
    assert cfg["authorizedkeysfile"] == "none"
    assert cfg["passwordauthentication"] == "no"


@pytest.mark.skipif(SSHD is None, reason="sshd not installed")
def test_root_keeps_its_static_keys_and_the_match_does_not_leak(tmp_path):
    cfg = effective(tmp_path, "root")
    assert cfg["authorizedkeyscommand"] == "none"
    assert cfg["authorizedkeysfile"] != "none"
    assert cfg["passwordauthentication"] == "yes"           # the main config, after Match all
