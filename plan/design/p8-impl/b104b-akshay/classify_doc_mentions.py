"""Classify every 'plan/' and 'README' mention in tests/ as "opens a file" or
"comment/string only", by file:line, using Python's tokenizer (not a regex guess).

A mention is "opens a file" if it sits in code (not a comment or a string token), or if it is a
string literal on a line that also calls open(, read_text(, read_bytes(, Path( or os.path.join(.
Usage: python3 -I classify_doc_mentions.py <repo>   (read-only)"""
import io
import re
import subprocess
import sys
import tokenize

repo = sys.argv[1]
OPENERS = re.compile(r"\b(open|read_text|read_bytes|Path|join)\s*\(")
PAT = re.compile(r"plan/|README")
hits = subprocess.run(["git", "-C", repo, "grep", "-n", "-E", "plan/|README", "--", "tests/"],
                      capture_output=True, text=True, check=True).stdout.splitlines()
tok_cache = {}


def tokens(path):
    if path not in tok_cache:
        src = open(f"{repo}/{path}", encoding="utf-8").read()
        tok_cache[path] = (list(tokenize.generate_tokens(io.StringIO(src).readline)),
                           src.splitlines())
    return tok_cache[path]


rows, opens = [], []
for h in hits:
    path, lineno, _ = h.split(":", 2)
    lineno = int(lineno)
    toks, lines = tokens(path)
    line = lines[lineno - 1]
    for m in PAT.finditer(line):
        col = m.start()
        kind = "code"
        for t in toks:
            (sr, sc), (er, ec) = t.start, t.end
            inside = (sr, sc) <= (lineno, col) < (er, ec) or (sr < lineno < er)
            if inside and t.type in (tokenize.COMMENT, tokenize.STRING):
                kind = "comment" if t.type == tokenize.COMMENT else "string"
                break
            if inside and t.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                kind = "string"
                break
        opener = kind == "code" or (kind == "string" and OPENERS.search(line))
        verdict = "opens a file" if opener else "comment/string only"
        rows.append((path, lineno, m.group(0), kind, verdict))
        if opener:
            opens.append(f"{path}:{lineno}")

for path, lineno, what, kind, verdict in rows:
    print(f"{path}:{lineno}\t{what}\t{kind}\t{verdict}")
n_plan = sum(1 for r in rows if r[2] == "plan/")
n_readme = sum(1 for r in rows if r[2] == "README")
print(f"\nSUMMARY: {len(rows)} mentions ({n_plan} 'plan/', {n_readme} 'README') in "
      f"{len({r[0] for r in rows})} files; opens a file: {len(opens)}"
      + (f" ({', '.join(opens)})" if opens else "")
      + f"; comment/string only: {len(rows) - len(opens)}")
