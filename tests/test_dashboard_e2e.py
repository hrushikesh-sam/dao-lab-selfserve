"""The dashboard in a real browser against the real stack (API + engine, DAOLAB_RUNNER=mock):
a user clicks Deploy, picks an SSH key or a generated password, watches the reservation come up,
reads `ssh lab@<DPU>` and the password, and ends it; an admin sees the quarantine banner and
force-cleans. Skipped unless Playwright and a Chromium are installed
(pip install playwright; DAOLAB_TEST_CHROMIUM=/path/to/chromium if it is not on PATH)."""
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pw = pytest.importorskip("playwright.sync_api")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMIUM = (os.environ.get("DAOLAB_TEST_CHROMIUM") or shutil.which("chromium-browser")
            or shutil.which("chromium") or shutil.which("google-chrome"))
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="no chromium for the browser test")
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBrowserTest0 alice@laptop"


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(tmp_path):
    port = _port()
    env = {**os.environ, "DAOLAB_DB_PATH": str(tmp_path / "daolab.db"),
           "DAOLAB_API_URL": f"http://127.0.0.1:{port}", "DAOLAB_INTERNAL_SECRET": "e2e",
           "DAOLAB_RUNNER": "mock", "DAOLAB_MOCK_STEP_SEC": "0.2",
           "DAOLAB_ENGINE_INTERVAL_SEC": "1", "DAOLAB_SLOT_MIN": "1",
           "DAOLAB_ARTIFACTS_DIR": str(tmp_path / "jobs"), "DAOLAB_LOG_LEVEL": "WARNING",
           "DAOLAB_OTEL_ENDPOINT": "", "DAOLAB_MOCK_FAIL": ""}
    py = sys.executable
    cli = [py, "-c", "from service.cli import main; main()"]
    subprocess.run(cli + ["init"], env=env, cwd=REPO, check=True, capture_output=True)
    subprocess.run(cli + ["sync"], env=env, cwd=REPO, check=True, capture_output=True)
    (tmp_path / "k.pub").write_text(KEY + "\n")
    tokens = {}
    for name, role in (("alice", "user"), ("boss", "admin")):
        out = subprocess.run(cli + ["add-user", name, str(tmp_path / "k.pub"), "--role", role],
                             env=env, cwd=REPO, check=True, capture_output=True, text=True).stdout
        tokens[name] = out.split("token: ")[1].strip()
    procs = [subprocess.Popen([py, "-m", "uvicorn", "service.api:app", "--host", "127.0.0.1",
                               "--port", str(port)], env=env, cwd=REPO,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]

    def engine(**extra):
        p = subprocess.Popen([py, "-m", "service.engine", "--loop"], env={**env, **extra},
                             cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return p

    eng = engine()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    yield type("Stack", (), {"base": f"http://127.0.0.1:{port}", "tokens": tokens,
                             "engine": staticmethod(engine), "first_engine": eng})
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


def _login(page, base, token, path="/"):
    page.goto(base + path)
    page.fill("#gate-token", token)
    page.click("#gate button:has-text('Save')")


def _wait_state(page, wanted, timeout=60):
    t0, badge = time.time(), ""
    while time.time() - t0 < timeout:
        page.click("header button:has-text('refresh')")
        page.wait_for_timeout(700)
        badge = page.locator("#reservations tbody tr").first.locator("td").nth(3).inner_text()
        if any(w in badge for w in wanted):
            return badge
    raise AssertionError(f"timed out waiting for {wanted}, last {badge!r}")


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM, headless=True)
        yield b
        b.close()


def test_a_user_deploys_with_a_password_logs_in_details_and_ends_it(stack, browser):
    errors = []
    page = browser.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    _login(page, stack.base, stack.tokens["alice"])
    row = page.locator("#catalog tbody tr", has=page.locator("code", has_text=re.compile("^vpp$")))
    row.locator("button:has-text('Deploy')").click()
    assert page.inner_text("#rm-title") == "Deploy vpp"
    assert page.input_value("#rm-key") == KEY                      # pre-filled from the profile
    page.check('input[name="rm-auth"][value="password"]')
    assert page.is_hidden("#rm-keybox") and page.is_visible("#rm-pwbox")
    page.click("#rm-submit")
    page.wait_for_selector("#banner.ok")
    assert "ssh lab@10.68.0.190" in page.inner_text("#banner-msg")
    _wait_state(page, ["ready", "active"])
    access = " ".join(page.locator(".access").first.inner_text().split())
    assert "ssh lab@10.68.0.190" in access and "••••" in access
    page.locator(".access button:has-text('show')").click()
    pw_text = page.locator("[id^=pw-]").first.inner_text()
    assert len(pw_text) == 16 and pw_text.isalnum()
    vms = " ".join(page.locator("table.creds tbody").first.inner_text().split())
    assert "10.68.1." in vms and "the password above" in vms
    page.locator("button:has-text('End now')").first.click()
    _wait_state(page, ["released"])
    assert errors == []


def test_a_user_deploys_with_their_key(stack, browser):
    page = browser.new_page()
    _login(page, stack.base, stack.tokens["alice"])
    page.locator("#catalog tbody tr", has=page.locator("code", has_text=re.compile("^vpp$"))) \
        .locator("button:has-text('Deploy')").click()
    page.click("#rm-submit")
    _wait_state(page, ["ready", "active"])
    access = " ".join(page.locator(".access").first.inner_text().split())
    assert "your SSH key ssh-ed25519 …alice@laptop" in access


def test_an_admin_sees_the_quarantine_and_clears_it(stack, browser):
    stack.first_engine.terminate()
    stack.first_engine.wait(timeout=10)
    stack.engine(DAOLAB_MOCK_FAIL="cleanup")                   # cleanup leaves residue
    user = browser.new_page()
    user.on("dialog", lambda d: d.accept())
    _login(user, stack.base, stack.tokens["alice"])
    user.locator("#catalog tbody tr", has=user.locator("code", has_text=re.compile("^vpp$"))) \
        .locator("button:has-text('Deploy')").click()
    user.click("#rm-submit")
    _wait_state(user, ["ready", "active"])
    user.locator("button:has-text('End now')").first.click()
    admin = browser.new_page()
    admin.on("dialog", lambda d: d.accept())
    _login(admin, stack.base, stack.tokens["boss"], "/admin.html")
    for _ in range(60):
        admin.click("header button:has-text('refresh')")
        admin.wait_for_timeout(700)
        if admin.locator(".quarantine").count():
            break
    banner = " ".join(admin.locator(".quarantine").inner_text().split())
    assert "residue after cleanup" in banner and "vm:daolab-r1-" in banner
    admin.locator("button:has-text('Mark clean without verifying')").click()
    admin.wait_for_timeout(1000)
    admin.click("header button:has-text('refresh')")
    admin.wait_for_timeout(1000)
    assert "available" in admin.locator("#detail-body .kv").first.inner_text()
    assert admin.locator(".quarantine").count() == 0
