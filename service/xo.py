"""daolab-xo: the few Xen Orchestra REST calls the VM playbooks need (B-93).

The playbooks used to SSH into each XCP-ng dom0 and run `xe`. They now run on the control node
and call this client, which talks to Xen Orchestra (scripts/run/xo.sh) over its REST API:

    daolab-xo template --pool P --name T                 -> template id
    daolab-xo network  --pool P --name N                 -> network id
    daolab-xo vms      --pool P [--name N | --prefix S]  -> JSON [{id, name, power, ip, tags}]
    daolab-xo create   --pool P --template T --name N [--sriov-network NAME] [--tag T ...]
                                                         -> JSON {id}; halted, fixed memory
    daolab-xo seed     --vm ID --file F                  -> xenstore data from a JSON object
    daolab-xo start    --vm ID
    daolab-xo power    --vm ID                           -> Running | Halted | ...
    daolab-xo ip       --vm ID                           -> the guest's first IPv4 (exit 1: none yet)
    daolab-xo destroy  --vm ID                           -> hard shutdown if running, then delete

Settings come from the environment (the runner passes them, never on argv):
    DAOLAB_XO_URL         default https://127.0.0.1:8443/rest/v0
    DAOLAB_XO_TOKEN_FILE  a file holding only the XO API token (chmod 600)
    DAOLAB_XO_INSECURE    accept XO's self-signed certificate; default true for 127.0.0.1 /
                          localhost, false otherwise

XO facts this relies on (B-93 spike and the 2026-11-01 seed test on xcp-ng-fxbqndvl):
  - auth is the `authenticationToken` cookie; `Authorization: Bearer` is refused (401)
  - create_vm?sync=true answers {"id": ...}; `autoPoweron` must be passed (templates carry true)
  - a template's static memory max above its dynamic max means Populate-on-Demand, which
    xenopsd refuses next to PCI passthrough, so the static max is pinned to the dynamic max
  - `vifs` on create_vm adds to the template's VIFs; a VIF on an SR-IOV network IS the VF
    passthrough (xapi picks a free VF)
  - PATCH /vms/{id} takes xenStoreData (full key path -> string) and tags
Exit codes: 0 ok, 1 XO error or nothing found, 2 usage, 3 not found, 4 ambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://127.0.0.1:8443/rest/v0"
VM_FIELDS = "id,name_label,power_state,addresses,tags,$pool"


class XoError(Exception):
    pass


class Xo:
    def __init__(self, url: str, token: str, insecure: bool):
        self.url = url.rstrip("/")
        self.token = token
        self.ctx = None
        if self.url.startswith("https://") and insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    @classmethod
    def from_env(cls) -> "Xo":
        url = os.environ.get("DAOLAB_XO_URL", "").strip() or DEFAULT_URL
        token_file = os.environ.get("DAOLAB_XO_TOKEN_FILE", "").strip()
        if not token_file:
            raise XoError("DAOLAB_XO_TOKEN_FILE is not set (a file holding only the XO API token)")
        try:
            with open(os.path.expanduser(token_file)) as fh:
                token = fh.read().strip()
        except OSError as e:
            raise XoError(f"cannot read DAOLAB_XO_TOKEN_FILE {token_file}: {e.strerror}") from None
        if not token:
            raise XoError(f"DAOLAB_XO_TOKEN_FILE {token_file} is empty")
        host = urllib.parse.urlparse(url).hostname or ""
        default_insecure = "true" if host in ("127.0.0.1", "localhost", "::1") else "false"
        insecure = os.environ.get("DAOLAB_XO_INSECURE", default_insecure).strip().lower() in (
            "1", "true", "yes", "on")
        return cls(url, token, insecure)

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, safe=",$")
        data = None
        headers = {"Cookie": f"authenticationToken={self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise XoError(f"XO {method} {path}: HTTP {e.code} {detail}".strip()) from None
        except urllib.error.URLError as e:
            raise XoError(f"XO {method} {path}: cannot reach {self.url} ({e.reason})") from None
        if status == 204 or not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    # ---- lookups ------------------------------------------------------------------------
    def _in_pool(self, kind: str, pool: str, fields: str) -> list[dict]:
        rows = self.call("GET", f"/{kind}", query={"fields": fields}) or []
        return [r for r in rows if r.get("$pool") == pool]

    def template(self, pool: str, name: str) -> str:
        hits = [t for t in self._in_pool("vm-templates", pool, "id,name_label,$pool")
                if t.get("name_label") == name]
        if not hits:
            raise NotFound(f"template {name!r} is not in pool {pool}")
        if len(hits) > 1:
            raise Ambiguous(f"{len(hits)} templates are named {name!r} in pool {pool}")
        return hits[0]["id"]

    def network(self, pool: str, name: str) -> str:
        hits = [n for n in self._in_pool("networks", pool, "id,name_label,$pool")
                if n.get("name_label") == name]
        if not hits:
            raise NotFound(f"network {name!r} is not in pool {pool}")
        if len(hits) > 1:
            raise Ambiguous(f"{len(hits)} networks are named {name!r} in pool {pool}")
        return hits[0]["id"]

    def vms(self, pool: str, name: str | None = None, prefix: str | None = None) -> list[dict]:
        out = []
        for v in self._in_pool("vms", pool, VM_FIELDS):
            n = v.get("name_label", "")
            if name is not None and n != name:
                continue
            if prefix is not None and not n.startswith(prefix):
                continue
            out.append({"id": v["id"], "name": n, "power": v.get("power_state"),
                        "ip": first_ipv4(v.get("addresses") or {}), "tags": v.get("tags") or []})
        return sorted(out, key=lambda r: (r["name"], r["id"]))

    def vm(self, vm_id: str) -> dict:
        return self.call("GET", f"/vms/{vm_id}") or {}

    # ---- changes ------------------------------------------------------------------------
    def create(self, pool: str, template: str, name: str, sriov_network: str | None = None,
               tags: list[str] | None = None) -> str:
        body = {"name_label": name, "template": self.template(pool, template),
                "boot": False, "autoPoweron": False}
        if sriov_network:
            body["vifs"] = [{"network": self.network(pool, sriov_network)}]
        made = self.call("POST", f"/pools/{pool}/actions/create_vm", body=body,
                         query={"sync": "true"})
        vm_id = made.get("id") if isinstance(made, dict) else None
        if not vm_id:
            raise XoError(f"create_vm for {name} answered {made!r}, expected {{\"id\": ...}}")
        mem = (self.vm(vm_id).get("memory") or {}).get("dynamic") or []
        patch: dict = {"autoPoweron": False}
        if len(mem) == 2:
            patch["memoryStaticMax"] = mem[1]            # no Populate-on-Demand (passthrough)
        if tags:
            patch["tags"] = list(tags)
        self.call("PATCH", f"/vms/{vm_id}", body=patch)
        return vm_id

    def seed(self, vm_id: str, data: dict) -> None:
        if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
            raise XoError("seed data must be a JSON object of xenstore path -> string")
        self.call("PATCH", f"/vms/{vm_id}", body={"xenStoreData": data})

    def start(self, vm_id: str) -> None:
        self.call("POST", f"/vms/{vm_id}/actions/start", query={"sync": "true"})

    def destroy(self, vm_id: str) -> None:
        if (self.vm(vm_id).get("power_state") or "") not in ("Halted", ""):
            self.call("POST", f"/vms/{vm_id}/actions/hard_shutdown", query={"sync": "true"})
        self.call("DELETE", f"/vms/{vm_id}")


class NotFound(XoError):
    pass


class Ambiguous(XoError):
    pass


def first_ipv4(addresses: dict) -> str:
    """The guest tools' first IPv4 (`0/ipv4/0`), else any IPv4 they report, else ''."""
    if addresses.get("0/ipv4/0"):
        return addresses["0/ipv4/0"]
    for key in sorted(addresses):
        if "/ipv4/" in key and addresses[key]:
            return addresses[key]
    return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="daolab-xo", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("template", "network"):
        s = sub.add_parser(cmd)
        s.add_argument("--pool", required=True)
        s.add_argument("--name", required=True)
    s = sub.add_parser("vms")
    s.add_argument("--pool", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--name")
    g.add_argument("--prefix")
    s = sub.add_parser("create")
    s.add_argument("--pool", required=True)
    s.add_argument("--template", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--sriov-network")
    s.add_argument("--tag", action="append", default=[])
    s = sub.add_parser("seed")
    s.add_argument("--vm", required=True)
    s.add_argument("--file", required=True, help="JSON object; - for stdin")
    for cmd in ("start", "power", "ip", "destroy"):
        sub.add_parser(cmd).add_argument("--vm", required=True)
    s = sub.choices["ip"]
    s.add_argument("--wait", type=int, default=0, help="seconds to wait for an address")
    a = p.parse_args(argv)
    try:
        xo = Xo.from_env()
        if a.cmd == "template":
            print(xo.template(a.pool, a.name))
        elif a.cmd == "network":
            print(xo.network(a.pool, a.name))
        elif a.cmd == "vms":
            print(json.dumps(xo.vms(a.pool, name=a.name, prefix=a.prefix)))
        elif a.cmd == "create":
            print(json.dumps({"id": xo.create(a.pool, a.template, a.name, a.sriov_network,
                                              a.tag)}))
        elif a.cmd == "seed":
            fh = sys.stdin if a.file == "-" else open(a.file)
            with fh:
                xo.seed(a.vm, json.load(fh))
        elif a.cmd == "start":
            xo.start(a.vm)
        elif a.cmd == "power":
            print(xo.vm(a.vm).get("power_state", ""))
        elif a.cmd == "ip":
            deadline = time.monotonic() + max(a.wait, 0)
            while True:
                ip = first_ipv4(xo.vm(a.vm).get("addresses") or {})
                if ip or time.monotonic() >= deadline:
                    break
                time.sleep(5)
            if not ip:
                print(f"daolab-xo: VM {a.vm} reports no IPv4 address yet", file=sys.stderr)
                return 1
            print(ip)
        elif a.cmd == "destroy":
            xo.destroy(a.vm)
    except NotFound as e:
        print(f"daolab-xo: {e}", file=sys.stderr)
        return 3
    except Ambiguous as e:
        print(f"daolab-xo: {e}", file=sys.stderr)
        return 4
    except (XoError, OSError, ValueError) as e:
        print(f"daolab-xo: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
