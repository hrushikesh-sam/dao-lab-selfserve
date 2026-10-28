"""`daolab init | sync | add-user | reserve | status` — init/sync/add-user open the DB
directly; reserve/status are thin HTTP clients over DAOLAB_API_URL using DAOLAB_TOKEN."""
import argparse
import hashlib
import json
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from typing import NoReturn, cast

from . import catalog, config, migrations, models


def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _api(method: str, path: str, body: dict | None = None) -> dict | list:
    settings = config.settings
    req = urllib.request.Request(
        settings.api_url.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {settings.token}",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
            _die(f"{err['code']}: {err['message']}")
        except (ValueError, KeyError):
            _die(f"http {e.code}")
    except urllib.error.URLError as e:
        _die(f"api unreachable at {settings.api_url}: {e.reason}")


def cmd_init(args) -> int:
    conn = models.connect(config.settings.db_path)
    try:
        applied = models.init_db(conn)
    except migrations.SchemaTooNew as e:
        _die(str(e))
    catalog.seed_usecases(conn)
    planned = conn.execute("SELECT COUNT(*) FROM usecases WHERE yaml_path IS NULL").fetchone()[0]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"db: {config.settings.db_path} (journal_mode={mode})")
    print(f"schema: v{migrations.current_version(conn)}"
          + (f" (applied: {', '.join(f'v{v}' for v in applied)})" if applied else " (up to date)"))
    print(f"usecases seeded: {planned} planned + diy")
    return 0


def cmd_sync(args) -> int:
    conn = models.connect(config.settings.db_path)
    try:
        result = catalog.sync(conn)
    except catalog.SyncError as e:
        for line in e.errors:
            print(line, file=sys.stderr)
        return 1
    for s in result["setups"]:
        print(f"setups: {s['name']} (pool {s['pool']}, capabilities: {', '.join(s['capabilities'])})")
    for u in result["usecases"]:
        print(f"usecases: {u['name']} (variant {u['variant']}) -> active")
    print("ok")
    return 0


def cmd_add_user(args) -> int:
    try:
        pubkey = open(args.pubkey_path).read().strip()
    except OSError as e:
        _die(f"cannot read {args.pubkey_path}: {e}")
    token = secrets.token_urlsafe(32)
    conn = models.connect(config.settings.db_path)
    try:
        fields = {"role": args.role, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                  "ssh_pubkey": pubkey}
        row = conn.execute("SELECT id FROM users WHERE username = ?", (args.username,)).fetchone()
        if args.rotate and row:
            # same user, new token (and key, role): the old token stops working, but the user's
            # reservations stay - deleting them would orphan a live one, its app still on the board
            models.update(conn, "users", row["id"], fields)
        else:
            models.insert(conn, "users", {"username": args.username, **fields})
    except sqlite3.IntegrityError:
        _die(f"duplicate user: {args.username} (pass --rotate to replace it)")
    print(f"token: {token}")
    return 0


def cmd_reserve(args) -> int:
    body = {"usecase": args.usecase, "start_at": args.start, "hours": args.hours}
    if args.password:
        body["ssh_pubkey"] = ""                       # no key: the API generates a password
    elif args.ssh_key:
        try:
            body["ssh_pubkey"] = open(args.ssh_key).read()
        except OSError as e:
            _die(f"cannot read {args.ssh_key}: {e}")
    if args.setup:
        body["setup"] = args.setup
    if args.template:
        templates = {}
        for item in args.template:
            if "=" not in item:
                _die(f"--template must be HOST=TEMPLATE, got: {item}")
            host, template = item.split("=", 1)
            templates[host] = template
        body["templates"] = templates
    r = cast("dict", _api("POST", "/reservations", body))
    print(f"id: {r['id']}  setup: {r['setup']}  state: {r['state']}")
    print(f"slot: {r['start_at']} -> {r['end_at']}")
    if r.get("access"):
        how = "a generated password (shown once the slot is live)" \
            if r["access"]["auth"] == "password" else "your SSH key"
        print(f"access: {r['access']['ssh']}  with {how}")
    if r["shifted"]:
        print(f"shifted from {r['requested_start_at']}")
    return 0


def cmd_status(args) -> int:
    rows = cast("list", _api("GET", "/reservations" + ("?all=1" if args.all else "")))
    if not rows:
        print("no reservations")
        return 0
    print(f"{'id':<4}{'usecase':<18}{'setup':<10}{'state':<12}{'start_at':<22}end_at")
    for r in rows:
        print(f"{r['id']:<4}{r['usecase']:<18}{r['setup']:<10}{r['state']:<12}"
              f"{r['start_at']:<22}{r['end_at']}")
        if r["state"] in ("ready", "active"):
            acc = r.get("access")
            if acc:
                print(f"      {acc['ssh']}   (dpu)" +
                      (f"   password: {acc['password']}" if acc.get("password") else ""))
            for vm in r["vms"]:
                if vm["ssh"]:
                    print(f"      {vm['ssh']}   ({vm['role']})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="daolab", description="DAO use-case reservation system")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the DB and seed the use-case catalog")
    sub.add_parser("sync", help="mirror usecases/ + setups/ YAML into the DB")

    p = sub.add_parser("add-user", help="create a user and print their token once")
    p.add_argument("username")
    p.add_argument("pubkey_path")
    p.add_argument("--role", choices=("admin", "user"), default="user")
    p.add_argument("--rotate", action="store_true",
                   help="if the user exists, give them a new token (and this key and role); "
                        "their reservations are kept (dev seeding)")

    p = sub.add_parser("reserve", help="reserve a use case (or diy) via the API")
    p.add_argument("usecase")
    p.add_argument("start", help="YYYY-MM-DDTHH:MM[:SS] (UTC) or 'now'")
    p.add_argument("--hours", type=int, default=1)
    p.add_argument("--setup", help="diy only: setup name")
    p.add_argument("--template", action="append", metavar="HOST=TEMPLATE",
                   help="diy only: repeatable, one per VM host of the setup")
    p.add_argument("--ssh-key", metavar="PUBKEY_PATH",
                   help="log in with this public key instead of your registered one")
    p.add_argument("--password", action="store_true",
                   help="no key: get a generated password for this reservation")

    p = sub.add_parser("status", help="list reservations via the API")
    p.add_argument("--all", action="store_true", help="every user's (admin token required)")

    args = parser.parse_args()
    handler = {"init": cmd_init, "sync": cmd_sync, "add-user": cmd_add_user,
               "reserve": cmd_reserve, "status": cmd_status}[args.command]
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
