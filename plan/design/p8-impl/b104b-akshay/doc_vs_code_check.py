"""Doc-vs-code: the SQL in the final B-104-DDL.md vs what `_m14_snapshots_v1` sends. NO exceptions.

From the doc (as ddl-check.py extracts it): the ```sql blocks containing CREATE TABLE, split into
statements with sqlite3.complete_statement; the add_column(conn, "t", "c", "ddl") calls; and the
_OLD/_NEW_JOB_KINDS strings. From the code: every statement _m14 sends to SQLite on a v13 DB,
recorded through a wrapper. CREATEs are compared whitespace-normalised, with the statement-ending
';' (outside comments) removed. Read-only against the repo.

Usage: <venv>/bin/python -I doc_vs_code_check.py ~/b104b ~/b104b/plan/design/p8-impl/B-104-DDL.md
"""
import re
import sqlite3
import sys

repo, md = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from service import migrations, models  # noqa: E402

doc = open(md, encoding="utf-8").read()


def norm(s):
    s = s.strip()
    s = re.sub(r";(\s*(--[^\n]*)?)$", r"\1", s)        # the statement terminator only
    return re.sub(r"\s+", " ", s).strip()


def split(text):
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    assert not buf.strip(), buf
    return out


blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "CREATE TABLE" in b]
want, want_order = {}, []
for b in blocks:
    for st in split(b):
        name = re.search(r"EXISTS (\w+)", st).group(1)
        assert name not in want, name
        want[name] = norm(st)
        want_order.append(name)

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
{m.version: m for m in migrations.MIGRATIONS}[14].fn(Rec(conn))

got, got_order = {}, []
for st in sent:
    mm = re.match(r"\s*CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS (\w+)", st)
    if mm:
        got[mm.group(1)] = norm(st)
        got_order.append(mm.group(1))

ok = True
print("doc CREATEs :", len(want), want_order)
print("code CREATEs:", len(got), got_order)
print("statement set:", "IDENTICAL" if sorted(want) == sorted(got) else "DIFFERENT SET")
ok &= sorted(want) == sorted(got)
SIGNED_OFF_ORDER = ["keys", "key_challenges", "admin_approvals", "snapshots"]      # v4 §3.1
code_tables = [n for n in got_order if not n.startswith("idx_")]
print("code table order == §3.1 order (keys, key_challenges, admin_approvals, snapshots):",
      code_tables == SIGNED_OFF_ORDER, "| the doc lists §3.5 snapshots before §3.6 admin_approvals "
      "(section order); §3.1 states the run order")
ok &= code_tables == SIGNED_OFF_ORDER
for name in sorted(set(want) | set(got)):
    same = want.get(name) == got.get(name)
    ok &= same
    print(f"  {'IDENTICAL' if same else 'DIFFERS  '}  {name}")
    if not same:
        print("     doc :", want.get(name))
        print("     code:", got.get(name))
adds_doc = re.findall(r'add_column\(conn, "(\w+)", "(\w+)",\s*"([^"]*)"\)', doc)
adds_sent = [re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (.*)", st, re.S).groups()
             for st in sent if st.startswith("ALTER TABLE")]
print("add_column (table, column, ddl), in order:", "IDENTICAL" if adds_doc == adds_sent else "DIFFERS")
ok &= adds_doc == adds_sent
kinds_doc = (re.search(r'_OLD_JOB_KINDS = "(.*)"', doc).group(1),
             re.search(r'_NEW_JOB_KINDS = "(.*)"', doc).group(1))
kinds_ok = kinds_doc == (migrations._OLD_JOB_KINDS, migrations._NEW_JOB_KINDS)
print("_OLD/_NEW_JOB_KINDS:", "IDENTICAL" if kinds_ok else "DIFFERS")
ok &= kinds_ok
others = [st.split()[0:3] for st in sent if not re.match(r"\s*(CREATE|ALTER|PRAGMA|SELECT|UPDATE sqlite_master)", st)]
print("other statements sent:", others or "none", "| executescript used: no (Rec has no executescript)")
print("statements sent in total:", len(sent))
print("RESULT:", "IDENTICAL, no exceptions" if ok else "DIFFERS")
sys.exit(0 if ok else 1)
