"""READ-ONLY audit of a DB COPY for stored user text the P-0 rules would refuse. Prints ids only,
never a value. Usage: python -I audit_stored_inputs.py <db-copy> (run in the venv where the FIXED repo is installed; -I ignores PYTHONPATH).
Exit 0 = nothing found, 1 = poisoned rows found (STOP CONDITION), 2 = could not audit."""
import json, sqlite3, sys
from service import models

def blob_bad(text, comments_ok):
    if text is None:
        return False
    for ln in models.ssh_key_lines(text):
        if comments_ok and ln.startswith("#"):
            continue
        if not models.ssh_key_line_ok(ln):
            return True
    return False

def main(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    found = {}
    def hit(label, row_id):
        found.setdefault(label, []).append(row_id)
    for r in db.execute("SELECT id, ssh_pubkey FROM reservations WHERE ssh_pubkey IS NOT NULL"):
        if blob_bad(r["ssh_pubkey"], False): hit("reservations.ssh_pubkey", r["id"])
    for r in db.execute("SELECT id, ssh_pubkey FROM users WHERE ssh_pubkey IS NOT NULL"):
        if blob_bad(r["ssh_pubkey"], True): hit("users.ssh_pubkey", r["id"])
    for r in db.execute("SELECT id, username FROM users"):
        n = r["username"] or ""
        if (not n.strip() or "{{" in n or "{%" in n or "{#" in n
                or any(ord(c) < 0x20 or c in "\x7f\x85\u2028\u2029" for c in n)):
            hit("users.username", r["id"])
    for r in db.execute("SELECT id, diy_templates FROM reservations WHERE diy_templates IS NOT NULL"):
        try:
            d = json.loads(r["diy_templates"])
            bad = (not isinstance(d, dict)) or any(not models.ident_ok(k) or not models.template_name_ok(v) for k, v in d.items())
        except ValueError:
            bad = True
        if bad: hit("reservations.diy_templates", r["id"])
    for r in db.execute("SELECT id, role, host, name, template FROM vms"):
        if (r["template"] is not None and not models.template_name_ok(r["template"])) or \
           any(x is not None and not models.ident_ok(x) for x in (r["role"], r["host"], r["name"])):
            hit("vms", r["id"])
    for label in ("reservations.ssh_pubkey", "users.ssh_pubkey", "users.username", "reservations.diy_templates", "vms"):
        print(f"{label}: {len(found.get(label, []))} row(s) ids={found.get(label, [])}")
    return 1 if found else 0

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as e:                      # noqa: BLE001
        print(f"audit failed: {type(e).__name__}", file=sys.stderr); sys.exit(2)
