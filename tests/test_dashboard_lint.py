"""Static lint guards for the hardened dashboards (B-113a).

No browser, no JS runtime: these checks keep the XSS fixes from regressing by
rejecting inline handlers, markup sinks and unescaped template interpolation.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGES = (REPO / "dashboard" / "user.html", REPO / "dashboard" / "admin.html")

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
MERMAID_SRI = ("sha384-WmdflGW9aGfoBdHc4rRyWzYuAjEmDwMdGdiPNacbwfGKxBW/"
               "SO6guzuQ76qjnSlr")
STATIC_INNERHTML_LITERALS = frozenset({'""', "''"})


def page_source(path):
    return path.read_text(encoding="utf-8")


def inline_script(text):
    return "\n".join(m.group(1) for m in
                     re.finditer(r"(?is)<script\b[^>]*>(.*?)</script>", text))


def _read_string(text, i):
    quote = text[i]
    out = [quote]
    j = i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            out.append(text[j:j + 2])
            j += 2
            continue
        if quote == "`" and c == "$" and j + 1 < len(text) and text[j + 1] == "{":
            expr, j = _read_braced(text, j + 2)
            out.append("${" + expr + "}")
            continue
        out.append(c)
        j += 1
        if c == quote:
            break
    return "".join(out), j


def _read_braced(text, i):
    depth, start, j = 1, i, i
    while j < len(text):
        c = text[j]
        if c in "\"'`":
            _, j = _read_string(text, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:j], j + 1
        j += 1
    return text[start:], j


def _read_expression(text, i):
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text) and text[i] in "\"'`":
        return _read_string(text, i)
    start = i
    j = i
    while j < len(text):
        c = text[j]
        if c in "\"'`":
            _, j = _read_string(text, j)
            continue
        if c == ";" or c == "\n":
            break
        j += 1
    return text[start:j].strip(), j


def interpolations(tmpl):
    found = []
    i = 0
    while True:
        start = tmpl.find("${", i)
        if start < 0:
            return found
        expr, i = _read_braced(tmpl, start + 2)
        found.append(expr.strip())


def is_esc(expr):
    return re.fullmatch(r"esc\(.*\)", expr, re.S) is not None


def template_literals(js):
    i = 0
    while i < len(js):
        if js[i] == "`":
            tmpl, i = _read_string(js, i)
            yield tmpl
            continue
        if js[i:i + 2] == "//":
            end = js.find("\n", i)
            i = len(js) if end < 0 else end + 1
            continue
        if js[i:i + 2] == "/*":
            end = js.find("*/", i + 2)
            i = len(js) if end < 0 else end + 2
            continue
        i += 1


def innerhtml_violations(script):
    problems = []
    for m in re.finditer(r"\.innerHTML\s*\+?=\s*", script):
        expr, _ = _read_expression(script, m.end())
        line = script.count("\n", 0, m.start()) + 1
        if expr.startswith("`"):
            for value in interpolations(expr):
                if not is_esc(value):
                    problems.append((line, f"unescaped interpolation {value!r}"))
            continue
        if expr in STATIC_INNERHTML_LITERALS or is_esc(expr):
            continue
        problems.append((line, f"non-allowlisted innerHTML literal {expr[:120]!r}"))
    return problems


def test_no_inline_event_handlers():
    for path in PAGES:
        src = page_source(path)
        tags = [m.group(0)[:100] for m in
                re.finditer(r"(?is)<[a-z][^>]*\bon[a-z]+\s*=", src)]
        assert not tags, f"{path.name}: inline handlers {tags}"
        script = inline_script(src)
        code = [line.strip() for line in script.splitlines()
                if re.search(r"(?i)\bon[a-z]+\s*=", line)]
        assert not code, f"{path.name}: handler assignments {code}"


def test_actions_are_wired_with_delegated_listeners():
    for path in PAGES:
        script = inline_script(page_source(path))
        assert 'document.addEventListener("click"' in script, f"{path.name}: no delegated click listener"
        assert '[data-action]' in script, f"{path.name}: no data-action wiring"
        assert script.count("addEventListener(") >= 2, f"{path.name}: no element listeners"


def test_no_forbidden_markup_or_code_sinks():
    sinks = {
        "javascript URL": r"(?i)javascript\s*:",
        "document.write": r"(?i)document\s*\.\s*write\s*\(",
        "outerHTML": r"(?i)\.outerHTML\b",
        "insertAdjacentHTML": r"(?i)insertAdjacentHTML\s*\(",
        "eval": r"(?i)(?<![.\w])eval\s*\(",
        "new Function": r"(?i)\bnew\s+Function\s*\(",
        "setAttribute handler": r"(?i)setAttribute\(\s*['\"]on[a-z]+",
        "string handler": r"addEventListener\(\s*['\"][^'\"]+['\"]\s*,\s*['\"`]",
    }
    for path in PAGES:
        src = page_source(path)
        for label, pattern in sinks.items():
            found = [m.group(0)[:100] for m in re.finditer(pattern, src)]
            assert not found, f"{path.name}: {label} {found}"


def test_innerhtml_only_literal_or_escaped_template():
    for path in PAGES:
        violations = innerhtml_violations(inline_script(page_source(path)))
        assert not violations, f"{path.name}: {violations}"


def test_innerhtml_rule_accepts_reviewed_forms():
    assert innerhtml_violations('el.innerHTML = "";') == []
    assert innerhtml_violations("el.innerHTML = '';") == []
    assert innerhtml_violations("el.innerHTML = esc(value);") == []
    assert innerhtml_violations("el.innerHTML = `<pre>${esc(source)}</pre>`;") == []
    assert innerhtml_violations('el.innerHTML = "<img src=x>";')


def test_innerhtml_rule_rejects_unsafe_template_assignment():
    script = "el.innerHTML = `<img src=x>${hostile}</img>`;"
    violations = innerhtml_violations(script)
    assert any("unescaped interpolation" in reason for _, reason in violations), violations


def test_markup_templates_escape_every_interpolation():
    checked = 0
    for path in PAGES:
        for tmpl in template_literals(inline_script(page_source(path))):
            if "<" not in tmpl:
                continue
            checked += 1
            for expr in interpolations(tmpl):
                assert is_esc(expr), \
                    f"{path.name}: unescaped markup interpolation {expr!r} in {tmpl[:100]!r}"
    assert checked >= 2, "expected at least one escaped markup template per page"


def test_mermaid_disables_html_labels():
    for path in PAGES:
        script = inline_script(page_source(path))
        assert re.search(r"mermaid\.initialize\(\{[^}]*flowchart:\s*\{\s*htmlLabels:\s*false",
                         script, re.S), f"{path.name}: htmlLabels not disabled"


def test_mermaid_is_pinned_with_sri():
    tag = (f'<script src="{MERMAID_URL}" integrity="{MERMAID_SRI}" '
           'crossorigin="anonymous"></script>')
    for path in PAGES:
        assert tag in page_source(path), f"{path.name}: missing pinned mermaid tag"
