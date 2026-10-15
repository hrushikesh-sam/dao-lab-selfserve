"""Load usecases/ + setups/ YAML, validate, and mirror into the DB (`daolab sync`).

Validation is all-or-nothing: any error -> SyncError listing one
'file: field: problem' line per issue, nothing written.
"""
import json
import os
import re
import sqlite3

import yaml

from . import models

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)_ip\}")
KINDS = ("dpu", "gpu", "nic", "compute")
ON_VALUES = ("dpu", "dpu_host", "tg_host")

# Seeded by `daolab init` from the root README use-case table; doc_url/doc_summary
# carry the verbatim intro text and page link from the DAO application docs.
DOC_BASE = "https://marvellembeddedprocessors.github.io/dao/guides/dao-devel/applications/"
SEED_USECASES = [
    {"name": "tls-proxy-nginx",
     "description": "NGINX TLS termination proxy, RSA/ECDSA offloaded to CPT",
     "doc_url": DOC_BASE + "tls-proxy-nginx.html",
     "doc_summary": "TLS proxy is an intermediate entity/application which sits between a server "
                    "and client where at least one of them uses TLS for their communication. The DAO "
                    "guide documents three deployment models: TLS termination/Reverse Proxy (server "
                    "side), TLS forwarding Proxy (client side), and Intercepting Proxy. daolab "
                    "implements the TLS termination proxy with a separate backend web server."},
    {"name": "virtio-l2fwd",
     "description": "L2 forwarding between host virtio devices and wire",
     "doc_url": DOC_BASE + "virtio-l2fwd.html",
     "doc_summary": "The dao-virtio-l2fwd EP application is a Dataplane development kit (DPDK) "
                    "application that allows to exercise virtio usecase of forwarding traffic "
                    "between VirtIO net device and DPDK ethdev device."},
    {"name": "ovs-offload",
     "description": "OVS packet-processing offload for wire\u2194VM",
     "doc_url": DOC_BASE + "ovs-offload.html",
     "doc_summary": "Open vSwitch (OVS) is often used in conjunction with companion applications to "
                    "enhance and extend its capabilities. OVS offload is a companion application "
                    "which enhance OVS functionality between host ports and mac ports."},
    {"name": "crypto-agent",
     "description": "standalone crypto service with host interaction",
     "doc_url": DOC_BASE + "crypto-agent.html",
     "doc_summary": "The DAO Crypto Agent acts as a daemon service on the Liquid Crypto Card. It "
                    "receives cryptographic requests from the host, submits them to the CPT "
                    "hardware, collects completions, and returns results to the host."},
    {"name": "virtio-crypto",
     "description": "crypto acceleration exposed to VMs via virtio",
     "doc_url": DOC_BASE + "virtio-crypto.html",
     "doc_summary": "The dao-virtio-crypto application is a sample application that shows how to "
                    "use the DAO virtio_crypto library. The application is designed to work with "
                    "the VirtIO crypto device, which is a virtualized crypto device that can be "
                    "used to offload crypto operations to a hardware crypto engine."},
    {"name": "virtio-blkio",
     "description": "block-device emulation to VMs via virtio",
     "doc_url": DOC_BASE + "virtio-blkio.html",
     "doc_summary": "The dao-virtio-blkio application is Dataplane development kit (DPDK) "
                    "application that allows to exercise usecase of presenting the Virtio block "
                    "devices and integrate them with various backend storage devices."},
    {"name": "secgw-graph",
     "description": "security gateway: IPv4 routing + IPsec",
     "doc_url": DOC_BASE + "secgw-graph.html",
     "doc_summary": "dao-secgw-graph or simply secgw-graph is a DPDK based security gateway "
                    "application providing IPv4 and IPsec dataplane functionalities which is "
                    "integrated with LINUX for control plane configuration, via netlink protocol."},
    {"name": "smartnic-turnkey",
     "description": "universal SmartNIC turnkey app",
     "doc_url": DOC_BASE + "smart-nic.html",
     "doc_summary": "Universal Smart NIC app is a powerful tool that harnesses the performance and "
                    "efficiency of hardware accelerators in DPUs."},
    {"name": "vpp",
     "description": "FD.io VPP with crypto/IO acceleration",
     "doc_url": DOC_BASE + "vpp.html",
     "doc_summary": "Vector Packet Processing (VPP) is an open-source framework developed under the "
                    "FD.io (Fast Data project) umbrella. It is designed to offer high-performance "
                    "packet processing capabilities."},
    {"name": "ml-tvm",
     "description": "ML model inference via TVM",
     "doc_url": DOC_BASE + "machine-learning.html",
     "doc_summary": "The dpdk-test-mldev tool is a DPDK application designed to test various "
                    "machine learning (mldev) use cases."},
    {"name": "snort-vectorscan",
     "description": "Snort IDS with VectorScan pattern matching",
     "doc_url": DOC_BASE + "snort.html",
     "doc_summary": "Snort is a open-source Intrusion Prevention System (IPS). It utilizes a series "
                    "of rules to define malicious network activity, identifying packets that match "
                    "these rules and generating alerts for users. VectorScan is a high-performance "
                    "regular expression matching library, a portable fork of Intel's Hyperscan."},
    {"name": "k8s-cni-offload",
     "description": "Kubernetes CNI networking offload",
     "doc_url": DOC_BASE + "k8s-cni-offload.html",
     "doc_summary": "Kubernetes networking is handled outside of Kubernetes core, this is based on "
                    "standardized framework called Container network interface (CNI). The solution "
                    "offloads the Cilium data path to the OCTEON DPU while keeping kubelet and Pods "
                    "unmodified on the host."},
    {"name": "envoy",
     "description": "Envoy with accelerated TLS handshakes/crypto",
     "doc_url": DOC_BASE + "envoy.html",
     "doc_summary": "Envoy is a high-performance, open-source edge and service proxy tailored for "
                    "cloud-native environments, especially microservices architectures."},
    {"name": "rdma",
     "description": "RDMA endpoint / termination modes",
     "doc_url": DOC_BASE + "rdma.html",
     "doc_summary": "Remote Direct Memory Access (RDMA) enables zero-copy data transfer between "
                    "memory regions of two systems without CPU involvement in the data path. This "
                    "significantly reduces latency and CPU overhead, making RDMA ideal for "
                    "high-performance networking."},
    {"name": "nvmeof-target",
     "description": "NVMe-oF storage target on OCTEON",
     "doc_url": DOC_BASE + "nvme-of-rdma.html",
     "doc_summary": "Data centers increasingly disaggregate storage from compute: NVMe SSDs are "
                    "pooled behind storage nodes and served to remote machines over the network, "
                    "rather than sitting inside each server. NVMe over Fabrics (NVMe-oF) is the "
                    "protocol that makes this practical."},
]
DIY_DESCRIPTION = "Bare setup, no DAO app"


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader minus the YAML 1.1 bool words on/off/yes/no — role maps use a bare
    `on:` key, which stock PyYAML would parse as boolean True. true/false still work."""


for _ch in "OoYyNn":
    _YamlLoader.yaml_implicit_resolvers[_ch] = [
        (tag, regexp) for tag, regexp in _YamlLoader.yaml_implicit_resolvers.get(_ch, [])
        if tag != "tag:yaml.org,2002:bool"
    ]


def _load_yaml(stream):
    return yaml.load(stream, Loader=_YamlLoader)


class SyncError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def seed_usecases(conn: sqlite3.Connection) -> None:
    """Idempotent: inserts only missing rows; backfills NULL doc fields, never
    overwrites anything already set."""
    for entry in SEED_USECASES:
        row = models.usecase_by_name(conn, entry["name"])
        if row is None:
            models.insert(conn, "usecases", dict(entry))
        elif row["doc_url"] is None:
            models.update(conn, "usecases", row["id"],
                          {"doc_url": entry["doc_url"], "doc_summary": entry["doc_summary"]})
    if models.usecase_by_name(conn, "diy") is None:
        models.insert(conn, "usecases", {
            "name": "diy", "description": DIY_DESCRIPTION,
            "doc_summary": "Bare wired VMs on a setup of your choice — no DAO application is "
                           "installed. You pick the setup and one base template per VM host.",
            "requires": "[]", "roles": "{}", "yaml_path": "builtin",
        })


def inventory_hosts(inventory_path: str) -> set[str]:
    """Collect every host name from the Ansible inventory YAML (any nesting)."""
    if not os.path.exists(inventory_path):
        return set()
    with open(inventory_path) as f:
        data = _load_yaml(f) or {}
    hosts: set[str] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "hosts" and isinstance(value, dict):
                hosts.update(value.keys())
            else:
                walk(value)

    walk(data)
    return hosts


def _yaml_files(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    return sorted(
        os.path.join(dir_path, f) for f in os.listdir(dir_path)
        if f.endswith((".yml", ".yaml"))
    )


def _check_keys(doc: dict, allowed: set[str], fname: str, ctx: str, errors: list[str]):
    for key in doc:
        if key not in allowed:
            errors.append(f"{fname}: {ctx}{key}: unknown key")


def load_usecases(dir_path: str) -> tuple[list[dict], list[str]]:
    out, errors = [], []
    for path in _yaml_files(dir_path):
        fname = os.path.relpath(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            doc = _load_yaml(open(path)) or {}
        except yaml.YAMLError as e:
            errors.append(f"{fname}: yaml: {e}")
            continue
        if not isinstance(doc, dict):
            errors.append(f"{fname}: document: must be a mapping")
            continue
        _check_keys(doc, {"name", "variant", "requires", "hw_class", "roles", "verify"},
                    fname, "", errors)
        name = doc.get("name")
        if name != stem or not (isinstance(name, str) and KEBAB.match(name)):
            errors.append(f"{fname}: name: must equal the file stem and be kebab-case")
        if not (isinstance(doc.get("variant"), str) and KEBAB.match(doc["variant"])):
            errors.append(f"{fname}: variant: required kebab-case string")
        requires = doc.get("requires")
        if not (isinstance(requires, list) and requires
                and all(isinstance(c, str) and KEBAB.match(c) for c in requires)):
            errors.append(f"{fname}: requires: must be a non-empty list of kebab-case strings")
        hw_class = doc.get("hw_class")
        if hw_class is not None and not (isinstance(hw_class, str) and KEBAB.match(hw_class)):
            errors.append(f"{fname}: hw_class: must be a kebab-case string")
        roles = doc.get("roles")
        if not (isinstance(roles, dict) and roles):
            errors.append(f"{fname}: roles: must be a non-empty map")
            roles = {}
        for rname, spec in roles.items():
            ctx = f"roles.{rname}."
            if not ROLE_NAME.match(str(rname)):
                errors.append(f"{fname}: roles.{rname}: invalid role name")
            if not isinstance(spec, dict):
                errors.append(f"{fname}: roles.{rname}: must be a map")
                continue
            _check_keys(spec, {"on", "app", "vm", "install", "connect", "crypto", "upstream"},
                        fname, ctx, errors)
            on = spec.get("on")
            if on not in ON_VALUES:
                errors.append(f"{fname}: {ctx}on: must be one of {'|'.join(ON_VALUES)}")
            if on == "dpu":
                if not spec.get("app"):
                    errors.append(f"{fname}: {ctx}app: required for on: dpu roles")
                if spec.get("vm"):
                    errors.append(f"{fname}: {ctx}vm: forbidden for on: dpu roles")
            elif on in ("dpu_host", "tg_host"):
                if not spec.get("vm"):
                    errors.append(f"{fname}: {ctx}vm: required for host roles")
                if spec.get("app"):
                    errors.append(f"{fname}: {ctx}app: forbidden for host roles")
                if spec.get("crypto"):
                    errors.append(f"{fname}: {ctx}crypto: only valid on on: dpu roles")
            if (spec.get("connect") is not None) != (spec.get("vm") is not None):
                errors.append(f"{fname}: {ctx}connect: present exactly when vm is")
            up = spec.get("upstream")
            if up is not None and up not in roles:
                errors.append(f"{fname}: {ctx}upstream: must name another role in this file")
        verify = doc.get("verify")
        if not (isinstance(verify, dict) and all(k in verify for k in ("from", "cmd", "expect"))):
            errors.append(f"{fname}: verify: must be a map with from, cmd, expect")
        else:
            _check_keys(verify, {"from", "cmd", "expect"}, fname, "verify.", errors)
            if verify["from"] not in roles:
                errors.append(f"{fname}: verify.from: must name a role in this file")
            for ref in PLACEHOLDER.findall(str(verify.get("cmd", ""))):
                if ref not in roles:
                    errors.append(f"{fname}: verify.cmd: placeholder {{{ref}_ip}} names no role")
        out.append({
            "name": stem, "variant": doc.get("variant"), "requires": requires or [],
            "hw_class": hw_class, "roles": roles, "verify": verify if isinstance(verify, dict) else {},
            "yaml_path": fname,
        })
    return out, errors


def load_setups(dir_path: str, known_hosts: set[str]) -> tuple[list[dict], list[str]]:
    out, errors = [], []
    for path in _yaml_files(dir_path):
        fname = os.path.relpath(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            doc = _load_yaml(open(path)) or {}
        except yaml.YAMLError as e:
            errors.append(f"{fname}: yaml: {e}")
            continue
        if not isinstance(doc, dict):
            errors.append(f"{fname}: document: must be a mapping")
            continue
        _check_keys(doc, {"name", "pool", "members", "links", "diagram"}, fname, "", errors)
        name = doc.get("name")
        if name != stem or not (isinstance(name, str) and KEBAB.match(name)):
            errors.append(f"{fname}: name: must equal the file stem and be kebab-case")
        if not (isinstance(doc.get("pool"), str) and KEBAB.match(doc["pool"])):
            errors.append(f"{fname}: pool: required kebab-case string")
        members = doc.get("members")
        if not (isinstance(members, dict) and members):
            errors.append(f"{fname}: members: must be a non-empty map")
            members = {}
        for mname, spec in members.items():
            ctx = f"members.{mname}."
            if not (isinstance(mname, str) and KEBAB.match(mname)):
                errors.append(f"{fname}: members.{mname}: member names must be kebab-case")
            if not isinstance(spec, dict):
                errors.append(f"{fname}: members.{mname}: must be a map")
                continue
            _check_keys(spec, {"kind", "capabilities", "host"}, fname, ctx, errors)
            if spec.get("kind") not in KINDS:
                errors.append(f"{fname}: {ctx}kind: must be one of {'|'.join(KINDS)}")
            caps = spec.get("capabilities")
            if not (isinstance(caps, list)
                    and all(isinstance(c, str) and KEBAB.match(c) for c in caps)):
                errors.append(f"{fname}: {ctx}capabilities: must be a list of kebab-case strings")
            host = spec.get("host")
            if not isinstance(host, str) or not host:
                errors.append(f"{fname}: {ctx}host: required string")
            elif known_hosts and host not in known_hosts:
                errors.append(f"{fname}: {ctx}host: '{host}' not in inventory hosts")
        links = doc.get("links", [])
        if not isinstance(links, list):
            errors.append(f"{fname}: links: must be a list")
            links = []
        for i, link in enumerate(links):
            ctx = f"links[{i}]."
            if not isinstance(link, dict):
                errors.append(f"{fname}: links[{i}]: must be a map")
                continue
            _check_keys(link, {"a", "b", "capability"}, fname, ctx, errors)
            for end in ("a", "b"):
                val = link.get(end)
                member = str(val).split(".", 1)[0] if val else ""
                if not val or "." not in str(val) or member not in members:
                    errors.append(f"{fname}: {ctx}{end}: must be '<member>.<port>' of a member")
            cap = link.get("capability")
            if not (isinstance(cap, str) and KEBAB.match(cap)):
                errors.append(f"{fname}: {ctx}capability: required kebab-case string")
        diagram = doc.get("diagram")
        if not (isinstance(diagram, str) and diagram.strip()):
            errors.append(f"{fname}: diagram: required non-empty mermaid source")
        union = set()
        for spec in members.values():
            if isinstance(spec, dict) and isinstance(spec.get("capabilities"), list):
                union.update(c for c in spec["capabilities"] if isinstance(c, str))
        union.update(l.get("capability") for l in links
                     if isinstance(l, dict) and isinstance(l.get("capability"), str))
        out.append({
            "name": stem, "pool": doc.get("pool"), "members": members, "links": links,
            "capabilities": sorted(union), "diagram": diagram or "", "yaml_path": fname,
        })
    return out, errors


def sync(conn: sqlite3.Connection, usecases_dir: str | None = None,
         setups_dir: str | None = None, inventory_path: str | None = None) -> dict:
    from . import config
    usecases_dir = usecases_dir or config.settings.usecases_dir
    setups_dir = setups_dir or config.settings.setups_dir
    inventory_path = inventory_path or config.settings.inventory

    ucs, uc_errors = load_usecases(usecases_dir)
    sts, st_errors = load_setups(setups_dir, inventory_hosts(inventory_path))
    errors = uc_errors + st_errors
    if errors:
        raise SyncError(errors)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # pools
        for s in sts:
            if conn.execute("SELECT id FROM pools WHERE name = ?", (s["pool"],)).fetchone() is None:
                models.insert(conn, "pools", {"name": s["pool"]})
        # setups + resources
        for s in sts:
            pool_id = conn.execute("SELECT id FROM pools WHERE name = ?", (s["pool"],)).fetchone()["id"]
            row = models.setup_by_name(conn, s["name"])
            fields = {"pool_id": pool_id, "capabilities": models.to_json(s["capabilities"]),
                      "links": models.to_json(s["links"]), "diagram": s["diagram"],
                      "yaml_path": s["yaml_path"]}
            setup_id = row["id"] if row else models.insert(conn, "setups", {"name": s["name"], **fields})
            if row:
                models.update(conn, "setups", setup_id, fields)
            for mname, spec in s["members"].items():
                existing = conn.execute(
                    "SELECT id FROM resources WHERE setup_id = ? AND name = ?",
                    (setup_id, mname)).fetchone()
                fields = {"kind": spec["kind"], "capabilities": models.to_json(sorted(spec["capabilities"])),
                          "host": spec["host"]}      # hw_class never written by sync
                if existing:
                    models.update(conn, "resources", existing["id"], fields)
                else:
                    models.insert(conn, "resources", {"setup_id": setup_id, "name": mname, **fields})
            conn.execute(
                f"""DELETE FROM resources WHERE setup_id = ? AND name NOT IN
                    ({', '.join('?' for _ in s['members']) or "''"})""",
                (setup_id, *s["members"].keys()))
        # usecases
        for u in ucs:
            row = models.usecase_by_name(conn, u["name"])
            fields = {"variant": u["variant"], "requires": models.to_json(u["requires"]),
                      "hw_class": u["hw_class"], "roles": models.to_json(u["roles"]),
                      "verify": models.to_json(u["verify"]), "yaml_path": u["yaml_path"]}
            if row:
                models.update(conn, "usecases", row["id"], fields)   # description preserved
            else:
                models.insert(conn, "usecases", {"name": u["name"], **fields})
        # removals — blocked by reservations not in released/failed
        synced_setups = {s["name"] for s in sts}
        for row in conn.execute("SELECT * FROM setups").fetchall():
            if row["name"] in synced_setups:
                continue
            blocker = conn.execute(
                """SELECT id FROM reservations WHERE setup_id = ?
                   AND state NOT IN ('released', 'failed') LIMIT 1""", (row["id"],)).fetchone()
            if blocker:
                raise SyncError([f"{row['yaml_path']}: setup {row['name']}: removal blocked by "
                                 f"reservation {blocker['id']} (not released/failed)"])
            conn.execute("DELETE FROM reservations WHERE setup_id = ?", (row["id"],))
            conn.execute("DELETE FROM setups WHERE id = ?", (row["id"],))
        synced_ucs = {u["name"] for u in ucs}
        for row in conn.execute(
                "SELECT * FROM usecases WHERE yaml_path IS NOT NULL AND yaml_path != 'builtin'"
        ).fetchall():
            if row["name"] in synced_ucs:
                continue
            blocker = conn.execute(
                """SELECT id FROM reservations WHERE usecase_id = ?
                   AND state NOT IN ('released', 'failed') LIMIT 1""", (row["id"],)).fetchone()
            if blocker:
                raise SyncError([f"{row['yaml_path']}: usecase {row['name']}: removal blocked by "
                                 f"reservation {blocker['id']} (not released/failed)"])
            conn.execute("DELETE FROM reservations WHERE usecase_id = ?", (row["id"],))
            conn.execute("DELETE FROM usecases WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"setups": sts, "usecases": ucs}
