import sqlite3, logging, sys
from service import engine, models
from service.runner import MockRunner, RunResult
logging.disable(logging.CRITICAL)

def db():
    c = sqlite3.connect(":memory:", isolation_level=None); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); models.init_db(c); return c

class R(MockRunner):
    def __init__(s, names): super().__init__(); s.names, s.handed = names, []
    def tagged_vms(s): return list(s.names)
    def destroy_vms(s, names, step): s.handed.append(list(names)); return RunResult(ok=True)

cases = {
  "platform name, 128-char role": "daolab-r12-" + "a" * 128,
  "role starts with '-'":         "daolab-r1--a",
  "19-digit id (> int64)":        "daolab-r" + "9" * 19 + "-x",
  "5000-digit id":                "daolab-r" + "9" * 5000 + "-x",
}
for label, n in cases.items():
    ok = models.vm_name_ok(n)
    try:
        rn = R(["daolab-r998-y", n]); engine._sweep_orphans(db(), rn, engine.TickResult())
        sweep = f"sweep ok, destroyed {rn.handed}" .replace("a"*128, "a*128").replace("9"*19, "9*19")
    except Exception as e:
        sweep = f"SWEEP RAISED {type(e).__name__}: {str(e)[:70]}"
    print(f"{label:32} vm_name_ok={ok!s:5} | {sweep}")
