"""Pure-Python XSS audit of the dashboard data paths (B-113a).

The pages are plain HTML/JS with no build step, so the audit reads their source,
extracts the JS esc() table and checks the sinks that markup could reach. The
hostile values below are the ones from the task prompt plus quote/backtick cases.
"""
import html
import json
import re
from html.parser import HTMLParser

from tests.test_dashboard_lint import (
    PAGES, _read_string, inline_script, interpolations, is_esc,
    page_source, template_literals,
)

HOSTILE = {
    "username": "a'\"><img src=x onerror=alert(1)>",
    "usecase": "vpp' onmouseover='alert(1)",
    "residue": "</script><img src=x onerror=alert(1)>",
    "setup": "tb-`1`><svg onload=alert(1)>",
}
EXPECTED_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;", "`": "&#96;"}
EXPECTED_CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                "object-src 'none'; base-uri 'none'")
RENDERERS = {
    "user.html": ("loadCatalog", "loadReservations", "accessBlock", "progressLine",
                  "healthBadge", "actionsCell", "openInfo", "addTemplateRow",
                  "openReserve", "submitReserve"),
    "admin.html": ("resSummaryLine", "renderPools", "renderDetail",
                   "loadAllReservations", "jobBlock", "loadResDetail", "loadUsers",
                   "setEnabled"),
}


def esc_mapping(path):
    match = re.search(r"\bconst ESCAPES\s*=\s*(\{[^}]*\});", page_source(path), re.S)
    assert match, f"{path.name}: no ESCAPES table"
    return json.loads(match.group(1))


def py_esc(value, mapping):
    return "".join(mapping.get(ch, ch) for ch in str(value))


def function_body(js, name):
    match = re.search(r"\b(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    i = start
    while i < len(js):
        c = js[i]
        if c in "\"'`":
            _, i = _read_string(js, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    return ""


class _StartTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)


def test_esc_table_escapes_all_six_dangerous_characters():
    for path in PAGES:
        assert esc_mapping(path) == EXPECTED_ESC, f"{path.name}: wrong esc() table"
        esc_line = next(line for line in page_source(path).splitlines() if "const esc" in line)
        assert "\\x60" in esc_line and "ESCAPES[c]" in esc_line, \
            f"{path.name}: esc() must use the full character class"


def test_hostile_values_are_inert_after_esc():
    for name, value in HOSTILE.items():
        escaped = py_esc(value, EXPECTED_ESC)
        assert not any(ch in escaped for ch in "<>\"'`"), f"{name}: raw markup survived"
        parser = _StartTags()
        parser.feed("<code>" + escaped + "</code>")
        assert parser.tags == ["code"], f"{name}: injected tag {parser.tags}"
        assert html.unescape(escaped) == value, f"{name}: escaping is not reversible"


def test_markup_interpolations_use_esc_on_both_pages():
    for path in PAGES:
        checked = 0
        for tmpl in template_literals(inline_script(page_source(path))):
            if "<" not in tmpl:
                continue
            checked += 1
            for expr in interpolations(tmpl):
                assert is_esc(expr), f"{path.name}: raw ${expr} in {tmpl[:100]!r}"
        assert checked >= 1, f"{path.name}: no markup template audited"


def test_render_paths_never_assign_html():
    for path in PAGES:
        js = inline_script(page_source(path))
        for name in RENDERERS[path.name]:
            body = function_body(js, name)
            assert body, f"{path.name}: renderer {name} not found"
            assert "innerHTML" not in body, f"{path.name}: {name} assigns innerHTML"
        text_helper = function_body(js, "text")
        assert text_helper and ".textContent" in text_helper, \
            f"{path.name}: text helper must write textContent"


def test_csp_report_only_header_on_pages_only(api_env):
    client = api_env.client
    for path in ("/", "/user.html", "/admin.html"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers.get("content-security-policy-report-only") == EXPECTED_CSP
        assert "content-security-policy" not in {k.lower() for k in response.headers}
    for path in ("/whoami", "/health", "/usecases"):
        response = client.get(path)
        assert response.status_code in (200, 401), response.text
        assert "content-security-policy-report-only" not in {k.lower() for k in response.headers}
