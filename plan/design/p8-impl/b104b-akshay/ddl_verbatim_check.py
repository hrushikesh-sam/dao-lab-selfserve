"""Check that migration 14's DDL is DDL v4 §3 verbatim except the declared v4.1 edits
(signed off by Rama 24 Nov; v4.1-1 includes dropping the one-open index's "(inferred)" tag).

Records every statement `_m14_snapshots_v1` sends to SQLite (on a v13 DB) and compares each
CREATE with the v4 doc's statement, after applying the v4.1 edits to the doc text. The
comparison is whitespace-normalised, with ';' removed on both sides (v4 ends statements with
';', and its comments contain ';'). It also compares the add_column DDL and the
_OLD/_NEW_JOB_KINDS strings. Read-only against the repo.

Usage: <venv>/bin/python -I ddl_verbatim_check.py ~/b104b ~/notes/b104/B-104-DDL.md
"""
import re
import sqlite3
import sys

repo, md = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from service import migrations, models  # noqa: E402

doc = open(md).read()
# The v4.1 edits, applied to the v4 text (old -> new). Each must match exactly once.
V41 = [
    ("v4.1-1", "ON admin_approvals(action, target_ref) WHERE state = 'pending';      -- (inferred)",
     "ON admin_approvals(action, target_ref) WHERE state = 'pending' OR "
     "(state = 'approved' AND executed_at IS NULL);"),      # v4.1-6: its "(inferred)" tag dropped
    ("v4.1-2", "-- retired/revoked keys were once active\n",
     "-- retired/revoked keys were once active\n"
     "    CHECK ((activated_by IS NULL) = (activated_at IS NULL)),\n"),
    ("v4.1-4", "-- Q-11: user:<id>, hw_class,", "-- Q-11: key:<id>, hw_class,"),
    ("v4.1-5", "hw_class, setup name\n", "hw_class, setup:<name>\n"),
]
for tag, old, new in V41:
    assert doc.count(old) == 1, (tag, doc.count(old))
    doc = doc.replace(old, new)


def norm(s):
    return re.sub(r"\s+", " ", s.replace(";", "")).strip()


def split(text):
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "CREATE TABLE" in b]
want = {}
for b in blocks:
    for s in split(b):
        name = re.search(r"EXISTS (\w+)", s).group(1)
        want[name] = norm(s)

sent = []


class Rec:
    def __init__(self, c):
        self.c = c

    def execute(self, sql, *a):
        sent.append(sql)
        return self.c.execute(sql, *a)


conn = sqlite3.connect(":memory:", isolation_level=None)
conn.execute("PRAGMA foreign_keys=ON")
with open(models.SCHEMA_PATH) as f:
    conn.executescript(f.read())
for m in migrations.MIGRATIONS[:13]:
    m.fn(conn)
m14 = {m.version: m for m in migrations.MIGRATIONS}[14].fn
m14(Rec(conn))

got = {}
order = []
for s in sent:
    mm = re.match(r"\s*CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS (\w+)", s)
    if mm:
        got[mm.group(1)] = norm(s)
        order.append(mm.group(1))
print("CREATE statements sent, in order:", order)
ok = True
for name in sorted(set(want) | set(got)):
    same = want.get(name) == got.get(name)
    ok &= same
    print(f"  {'SAME' if same else 'DIFF'}  {name}")
    if not same:
        print("    v4+v4.1:", want.get(name))
        print("    code   :", got.get(name))
adds_doc = re.findall(r'add_column\(conn, "(\w+)", "(\w+)",\s*"([^"]*)"\)', doc)
adds_sent = [re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (.*)", s, re.S).groups()
             for s in sent if s.startswith("ALTER TABLE")]
print("add_column DDL identical to v4 §3.7-§3.9 (order too):", adds_doc == adds_sent)
ok &= adds_doc == adds_sent
kinds = (re.search(r'_OLD_JOB_KINDS = "(.*)"', doc).group(1),
         re.search(r'_NEW_JOB_KINDS = "(.*)"', doc).group(1))
print("_OLD/_NEW_JOB_KINDS identical to v4:",
      kinds == (migrations._OLD_JOB_KINDS, migrations._NEW_JOB_KINDS))
ok &= kinds == (migrations._OLD_JOB_KINDS, migrations._NEW_JOB_KINDS)
print("executescript used:", any("executescript" in s for s in sent),
      "| statements sent:", len(sent))
print("RESULT:", "VERBATIM (modulo the declared v4.1 edits)" if ok else "DIFFERS")
