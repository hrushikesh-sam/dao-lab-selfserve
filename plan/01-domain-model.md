# Domain model and data layer

Part of the daolab implementation plan — see [README.md](README.md) for the doc map.

Defines the entities, the full sqlite schema (`service/schema.sql`), the YAML contracts, the `daolab sync` mirroring rules, and the `hw_class`/EBF semantics. The schema includes `jobs`, `ip_pools`, `ip_leases`, and supports states `cleaning`, `quarantined`, `failed` per plan/09. Everything downstream — API ([02-backend-api.md](02-backend-api.md)), scheduler ([03-scheduler.md](03-scheduler.md)), engine ([04-lifecycle-engine.md](04-lifecycle-engine.md)) — reads and writes only what is defined here.

## Entities at a glance

Tables: `users`, `pools`, `setups`, `resources`, `usecases`, `reservations`, `vms`, `jobs`, `ip_pools`, `ip_leases`, `audit_events`. (Counts as in `service/schema.sql`.)
Every table also carries `created_at` / `updated_at` (UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`, set by
`models.py`; omitted from the diagram for brevity).

```mermaid
erDiagram
    pools ||--o{ setups : "pool_id"
    setups ||--o{ resources : "setup_id (CASCADE)"
    users ||--o{ reservations : "user_id"
    usecases ||--o{ reservations : "usecase_id"
    setups ||--o{ reservations : "setup_id"
    reservations ||--o{ vms : "reservation_id (CASCADE)"
    reservations ||--o{ jobs : "reservation_id"
    ip_pools ||--o{ ip_leases : "pool_id"
    reservations ||--o{ ip_leases : "reservation_id"

    setups {
        text capabilities "JSON, sorted union"
        text links "JSON {a,b,capability}"
        text diagram "mermaid source"
    }
    resources {
        text kind "dpu|gpu|nic|compute"
        text host "XCP-ng host"
        text hw_class "live DPU state"
    }
    usecases {
        text requires "JSON capability list"
        text hw_class "declared need"
        text roles "JSON role map"
        text yaml_path "NULL=planned, builtin=diy"
        text health "JSON health rules"
    }
    reservations {
<<<<<<< HEAD
        text state "CHECK includes queued,provisioning,ready,active,cleaning,released,failed,quarantined"
=======
        text state "7-state CHECK"
>>>>>>> akshay
        text requested_start_at "user ask"
        text start_at "granted (slot-shift)"
        int diy "0|1"
    }
    vms {
        text role "from usecase roles"
        text ip "via POST /internal/vm-ip"
    }
    jobs {
        text kind "deploy|cleanup|health_probe|reconcile"
        text state "queued|running|succeeded|failed|timeout"
        text trace_id "OTel trace id"
    }
    ip_leases {
        text ip "from ip_pools"
        text state "reserved|leased|released"
        int reservation_id "FK"
    }
```

(Attribute blocks show only the semantically loaded columns; the full DDL below is normative.)

JSON-valued columns are TEXT holding canonical JSON, written and parsed **only** by
`service/models.py`, which executes `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on
every connection (WAL lets the `api` and `engine` processes share the file safely). Helper
surface:

```python
# service/models.py — sqlite helpers; the only code touching JSON columns and timestamps
def connect(db_path: str | None = None) -> sqlite3.Connection  # WAL + foreign_keys=ON + Row factory; default DAOLAB_DB_PATH
def init_db(conn: sqlite3.Connection) -> None                  # executescript(service/schema.sql)
def utcnow() -> str                                            # 'YYYY-MM-DDTHH:MM:SSZ'
def insert(conn, table: str, row: dict) -> int                 # fills created_at/updated_at, returns id
def update(conn, table: str, row_id: int, changes: dict) -> None  # refreshes updated_at
def to_json(value) -> str                                      # canonical dump for TEXT JSON columns
def from_json(text: str)                                       # parse for TEXT JSON columns
```

Ids are `INTEGER PRIMARY KEY AUTOINCREMENT`, starting at 1. API paths use the integer id; API
requests and YAML always use the natural name (`usecase: tls-proxy-nginx`, `setup: setup-01`).
`reservation_id=0` is reserved for manual playbook runs and is never issued by the DB. Orchestration uses `deploy.yml`/`cleanup.yml`.

## Entity reference

**users** — lab users and auth. Rows created only by `daolab add-user USERNAME PUBKEY_PATH
[--role {admin,user}]`. `token_hash` = SHA-256 hex of the bearer token (plaintext printed once,
never stored). `role` (`admin` | `user`) gates admin endpoints; admin ⊇ user. `ssh_pubkey` is
injected into every reservation VM via xenstore (`vm-data/user`, `vm-data/ssh_key`). `enabled`
(default 1) is flipped by admins via `PATCH /users/{id}`: a disabled user's token fails every
authenticated call with `403 forbidden` (`detail.reason: "user_disabled"`); their existing
reservations are untouched — admins cancel those explicitly. One user → many `reservations`.

**pools** — an XCP-ng resource pool, admin-level grouping of hosts (e.g. `lab-1`). Name only;
everything else lives in XCP-ng. Auto-created by `daolab sync` from the `pool:` field of setup
YAML. One pool → many `setups`.

**setups** — the unit users reserve: member resources + links (wiring) + a mermaid diagram.
`capabilities` is a **computed** sorted JSON union (next section), never hand-edited. `diagram`
is rendered verbatim by `admin.html`, so no topology lives in frontend code. `yaml_path` (e.g.
`setups/setup-01.yml`) ties the row to its source file for sync/removal. Many setups → one
pool; one setup → many `resources` (CASCADE) and many `reservations`.

**resources** — one reservable piece of hardware, member of exactly one setup
(`UNIQUE (setup_id, name)`). `kind` ∈ `dpu` | `gpu` | `nic` | `compute`. `host` names the
XCP-ng host that can spin up VM templates next to the hardware (e.g. `dpu-01-xcp`); it must
exist in `inventory/hosts.yml`. `hw_class` = current DPU personality — NULL for non-DPU and for
a never-provisioned DPU; written only by the engine, never by sync.

**usecases** — one row per DAO application variant plus the built-in `diy` pseudo-use-case.
`daolab init` seeds the 15 README catalog rows (name + description + `doc_url`/`doc_summary` —
the DAO docs page link and its verbatim intro text, rendered by the user page's info modal;
YAML never carries them) and the `diy` row (`yaml_path = 'builtin'`, `requires = '[]'`,
`roles = '{}'`).
`daolab sync` fills `variant` / `requires` / `hw_class` / `roles` / `verify` / `yaml_path` for
rows whose YAML exists; those columns are exact JSON mirrors of the YAML fields.

**reservations** — user + use case (or DIY) + matched setup + time slot. `requested_start_at` =
user ask; `start_at` = scheduler grant after slot-shift (the API's `shifted` is the derived
inequality, not a column). `state` follows the 7-state CHECK; `released`/`failed` terminal;
`failure_reason` set only in `failed`. `diy = 1` marks DIY mode and `diy_templates` (JSON
host → template) is non-NULL only then. `retries` counts playbook retries per phase (cap
`DAOLAB_MAX_RETRIES`).

**vms** — one row per provisioned role (`UNIQUE (reservation_id, role)`). `role` comes from the
use case's `roles` map (for DIY: the host name). `on: dpu` roles have NULL `template`/`name` —
the DAO app runs on the OCTEON itself, not in a VM; host roles get the XCP-ng VM name
`daolab-r<reservation_id>-<role>` (e.g. `daolab-r12-backend`). `ip` stays NULL until the
playbook posts it via `POST /internal/vm-ip`.

## Capability matching — how use cases find setups

The single coupling between use cases and setups is a set-inclusion test on kebab-case
capability strings — **never hostnames**:

```
setup.capabilities = (⋃ member.capabilities) ∪ {link.capability for every link}
match(usecase, setup)  ⇔  set(usecase.requires) ⊆ set(setup.capabilities)
```

The union is deduplicated and stored **sorted** in `setups.capabilities` at `daolab sync` time.
Worked example for `setup-01`: members contribute `octeon-cn106`, `sdp` (dpu-01) and `e810`
(tg-01); the one link contributes `wire` → stored as `["e810", "octeon-cn106", "sdp", "wire"]`.
`tls-proxy-nginx` declares `requires: [octeon-cn106, e810, wire]` ⊆ that union → match.

Hostnames appear nowhere in matching: a use case's `roles` use the placement selectors `dpu` /
`dpu_host` / `tg_host`, resolved against the *matched* setup's members only at provisioning
time ([05-provisioning.md](05-provisioning.md)) — so a new setup YAML with the right
capabilities serves every existing use case unchanged. Three consumers apply the identical
subset test: the scheduler's candidate selection ([03-scheduler.md](03-scheduler.md)), the
`available` computation in `GET /usecases`, and the `unmet_capability` 409 whose message must
name the missing capability list ([02-backend-api.md](02-backend-api.md)).

## Full DDL — `service/schema.sql`

Applied by `daolab init` (idempotent — `IF NOT EXISTS` throughout; WAL is set at connect time
by `models.py`, not in the schema).

```sql
-- service/schema.sql — daolab canonical schema (sqlite, WAL set at connect time)

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    role        TEXT    NOT NULL DEFAULT 'user'
                CHECK (role IN ('admin', 'user')),
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    token_hash  TEXT    NOT NULL UNIQUE,          -- SHA-256 hex of the bearer token
    ssh_pubkey  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,                 -- UTC ISO-8601 YYYY-MM-DDTHH:MM:SSZ
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,          -- e.g. 'lab-1'
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS setups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,         -- e.g. 'setup-01'
    pool_id      INTEGER NOT NULL REFERENCES pools(id),
    capabilities TEXT    NOT NULL DEFAULT '[]',   -- JSON array; computed union, sorted
    links        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of {a,b,capability}
    diagram      TEXT    NOT NULL DEFAULT '',     -- mermaid source, rendered verbatim
    yaml_path    TEXT    NOT NULL,                -- e.g. 'setups/setup-01.yml'
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id     INTEGER NOT NULL REFERENCES setups(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,                -- member name, e.g. 'dpu-01'
    kind         TEXT    NOT NULL
                 CHECK (kind IN ('dpu', 'gpu', 'nic', 'compute')),
    capabilities TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    host         TEXT    NOT NULL,                -- XCP-ng host, e.g. 'dpu-01-xcp'
    hw_class     TEXT,                            -- current DPU personality; NULL for non-DPU
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (setup_id, name)
);

CREATE TABLE IF NOT EXISTS usecases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,          -- e.g. 'tls-proxy-nginx', 'diy'
    description TEXT    NOT NULL DEFAULT '',      -- "what it is" column of the README table
    doc_url     TEXT,                             -- DAO documentation page for this application
    doc_summary TEXT,                             -- verbatim intro text from the DAO docs
    variant     TEXT,                             -- e.g. 'separate-backend'; NULL if planned/diy
    requires    TEXT    NOT NULL DEFAULT '[]',    -- JSON array of capability strings
    hw_class    TEXT,                             -- e.g. 'sdp-pf1-vf8'; NULL = no EBF requirement
    roles       TEXT    NOT NULL DEFAULT '{}',    -- JSON object, role name -> role spec
    verify      TEXT,                             -- JSON object {from,cmd,expect}; NULL if planned/diy
    yaml_path   TEXT,                             -- NULL = planned (no YAML yet); 'builtin' for diy
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    usecase_id         INTEGER NOT NULL REFERENCES usecases(id),
    setup_id           INTEGER NOT NULL REFERENCES setups(id),
    state              TEXT    NOT NULL DEFAULT 'queued'
                       CHECK (state IN ('queued', 'provisioning', 'ready', 'active',
                                        'tearing_down', 'released', 'failed')),
    requested_start_at TEXT    NOT NULL,          -- what the user asked for
    start_at           TEXT    NOT NULL,          -- what the scheduler granted (slot-shift)
    end_at             TEXT    NOT NULL,
    diy                INTEGER NOT NULL DEFAULT 0 CHECK (diy IN (0, 1)),
    diy_templates      TEXT,                      -- JSON object host -> template; NULL unless diy
    retries            INTEGER NOT NULL DEFAULT 0,
    failure_reason     TEXT,                      -- set only in state 'failed'
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS vms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    role           TEXT    NOT NULL,              -- role name from usecase roles; host name for diy
    host           TEXT    NOT NULL,              -- XCP-ng host (or DPU node name for on: dpu roles)
    template       TEXT,                          -- NULL for on: dpu roles (no VM template)
    name           TEXT,                          -- XCP-ng VM name: 'daolab-r<id>-<role>'; NULL for dpu roles
    ip             TEXT,                          -- posted by POST /internal/vm-ip; NULL until then
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (reservation_id, role)
);

CREATE INDEX IF NOT EXISTS idx_resources_setup       ON resources(setup_id);
CREATE INDEX IF NOT EXISTS idx_reservations_setup    ON reservations(setup_id, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_reservations_state    ON reservations(state);
CREATE INDEX IF NOT EXISTS idx_reservations_user     ON reservations(user_id);
CREATE INDEX IF NOT EXISTS idx_vms_reservation       ON vms(reservation_id);
```

Index rationale: `idx_reservations_setup` → scheduler busy-window scan; `idx_reservations_state`
→ engine tick queries; `idx_reservations_user` → `GET /reservations`; the FK indexes → joins
and cascades.

## YAML contract — `usecases/*.yml`

One file per DAO application variant: `usecases/<name>.yml` (e.g.
`usecases/tls-proxy-nginx.yml`). `diy` is built in — it has **no** YAML file. Unknown keys
anywhere in the file are a `daolab sync` validation error (exit non-zero, message
`file: field: problem`).

| Field | Type | Required | Rules |
|---|---|---|---|
| `name` | string, kebab-case | yes | Must equal the file stem. Unique across `usecases/`. |
| `variant` | string, kebab-case | yes | Which documented variant of the DAO app this file implements (e.g. `separate-backend`; docs also describe `inbuilt-server`). |
| `requires` | list of strings, kebab-case | yes, non-empty | Capability strings a setup must offer (e.g. `[octeon-cn106, e810, wire]`). Capability match only — never hostnames. |
| `hw_class` | string, kebab-case | no | DPU personality this use case needs (e.g. `sdp-pf1-vf8`). Omitted/null = runs on whatever `hw_class` the DPU currently has; never triggers `ebf_set`. |
| `roles` | map: role name → role object | yes, ≥ 1 entry | Role names match `^[a-z][a-z0-9_]*$` (e.g. `proxy`, `backend`, `client`). |
| `verify` | object | yes | `{from, cmd, expect}` — see below. |

Role object fields:

| Field | Type | Required | Rules |
|---|---|---|---|
| `on` | enum: `dpu` \| `dpu_host` \| `tg_host` | yes | Placement selector, resolved against the matched setup at provisioning time: `dpu` → the member with `kind: dpu` (runs on the OCTEON itself); `dpu_host` → the XCP-ng `host` of that member; `tg_host` → the XCP-ng `host` of the member with `kind: nic`. |
| `app` | string, kebab-case | iff `on: dpu` | DAO application id installed by `dpu_prepare.yml` (e.g. `nginx-tls`). Forbidden on host roles. |
| `vm` | string, kebab-case | iff `on` is `dpu_host`/`tg_host` | Base VM template name (e.g. `tpl-ubuntu2204-dao`). Forbidden on `on: dpu`. |
| `install` | string or list of strings | no | Software `host_vm.yml` installs inside the VM per the role (e.g. `http-server`, `[ab, h2load]`) — never baked into the template. |
| `connect` | string, kebab-case | iff `vm` is set | How the VM is wired: today `sdp` or `e810-vf`. Data, not an enum — new values need no code change. |
| `crypto` | string, kebab-case | no | Crypto attachment for `on: dpu` roles only (e.g. `cpt-vfs`). |
| `upstream` | string | no | Must name another role in the same file (e.g. proxy → `backend`). |

`verify` block: `{from: <role name>, cmd: <string>, expect: <string>}` — all three required.
`from` must exist in `roles`. `cmd` may use placeholders `{<role>_ip}` (e.g. `{proxy_ip}`),
substituted by `verify.yml` from `vms.ip` of that role. `expect` is a substring that must
appear in the command's stdout. Normative example:
`{from: client, cmd: "h2load -n1000 https://{proxy_ip}/", expect: "200: 1000"}`.

Validation rules (all enforced by `daolab sync` before any DB write):
`name` = file stem, kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`); `requires` non-empty; ≥ 1 role;
`app` xor `vm` per the `on` value; `connect` present exactly when `vm` is; `upstream` and
`verify.from` reference existing role names; every `{<role>_ip}` placeholder in `verify.cmd`
names a role in the file; unknown keys at any level rejected. The full worked example is
[07-usecase-tls-proxy.md](07-usecase-tls-proxy.md).

## YAML contract — `setups/*.yml`

One file per reservable setup: `setups/<name>.yml` (e.g. `setups/setup-01.yml`). Unknown keys
are a sync validation error.

| Field | Type | Required | Rules |
|---|---|---|---|
| `name` | string, kebab-case | yes | Must equal the file stem. Unique across `setups/`. |
| `pool` | string, kebab-case | yes | XCP-ng pool name (e.g. `lab-1`). The pool row is auto-created by `daolab sync` if absent. |
| `members` | map: member name → member object | yes, ≥ 1 entry | Member names kebab-case (e.g. `dpu-01`, `tg-01`); unique within the file. |
| `links` | list of link objects | no (default `[]`) | Physical wiring between members. |
| `diagram` | string (mermaid source) | yes, non-empty | Rendered **verbatim** by `admin.html` — the page never hardcodes topology. |

Member object `{kind, capabilities, host}`: `kind` (required) ∈ `dpu` | `gpu` | `nic` |
`compute`; `capabilities` (required) list of kebab-case strings, may be empty; `host`
(required) the XCP-ng host name, which must exist in `inventory/hosts.yml` (e.g. `dpu-01-xcp`,
`tg-01-xcp`).

Link object `{a, b, capability}`: `a` and `b` (required) are `"<member>.<port>"` where
`<member>` must be a key of `members` and `<port>` is a free-form token (e.g. `dpu-01.eth0`,
`tg-01.e810p0`); `capability` (required) is a kebab-case string (e.g. `wire`).

Note what a member does **not** carry: no `hw_class` (that is live DPU state owned by the DB,
not declared wiring) and no BDFs (those live in `inventory/host_vars/`, consumed only by
playbooks — [05-provisioning.md](05-provisioning.md)).

## `daolab sync` — mirroring YAML into the DB

`service/catalog.py` implements sync (invoked by `daolab sync`, no flags; directories from
`DAOLAB_USECASES_DIR` and `DAOLAB_SETUPS_DIR`). Signature sketch:

```python
# service/catalog.py
def load_usecases(dir_path: str) -> list[dict]   # parse + validate all files; collect errors
def load_setups(dir_path: str) -> list[dict]
def sync(conn: sqlite3.Connection) -> None       # all-or-nothing mirror in one transaction
```

Ordered algorithm — validation is all-or-nothing, then one DB transaction:

1. **Load + validate everything first.** Any error: print one `file: field: problem` line per
   error, exit 1, **write nothing**.
2. **Pools** — insert any `pool:` name not yet in `pools`.
3. **Setups** — upsert by `name`: `pool_id`, computed `capabilities` (sorted JSON union of
   member capabilities plus link capabilities), `links`, `diagram`, `yaml_path`.
4. **Resources** — per setup, upsert by `(setup_id, name)` with `kind` / `capabilities` /
   `host` from YAML; member rows no longer present in the YAML are deleted. Sync **never
   writes `resources.hw_class`**: new rows start NULL, existing values are preserved (the
   engine owns that column).
5. **Usecases** — upsert by `name`: `variant`, `requires`, `hw_class`, `roles`, `verify`,
   `yaml_path`. Rows are created for YAML names not in the seed list; `description` (seeded
   from the README table) is preserved — YAML has no description field. The seeded `diy` row
   (`yaml_path = 'builtin'`) and planned rows (`yaml_path` NULL) are never touched by upsert.
6. **Removals** — a `setups` row (or a `usecases` row with a real `yaml_path`) whose YAML file
   disappeared is deleted, **unless** it is referenced by a reservation not in
   `released`/`failed` — then sync fails (exit 1) naming the blocking reservation, and the
   transaction rolls back. On an allowed delete, `resources` rows cascade; because
   `reservations.setup_id`/`usecase_id` FKs have no cascade, terminal (`released`/`failed`)
   reservations referencing the removed row are deleted with it (their `vms` cascade) — a
   consequence of `PRAGMA foreign_keys=ON`, not a product behavior. `diy` is never deleted.
7. Commit. Re-running `daolab init` afterwards restores any deleted seed-catalog row as
   *planned* (init is idempotent and inserts only missing rows).

Update semantics: edits to a YAML apply on the next sync and affect future scheduling only —
existing reservations keep their `setup_id`/`usecase_id` and granted slots; nothing is
re-placed. Adding a use-case YAML flips its catalog row from *planned* to *active* with zero
code changes; adding a setup YAML makes it a candidate for every use case whose `requires` its
capability union satisfies.

## `hw_class` and EBF semantics

`hw_class` is the DPU personality string produced by an EBF (early-boot firmware)
configuration — e.g. `sdp-pf1-vf8` = SDP endpoint, 1 PF, 8 VFs. It appears in exactly two
places with different meanings:

- `usecases.hw_class` — **declared need**: the personality this use case requires. NULL means
  "runs on whatever the DPU currently has" and never triggers a reflash.
- `resources.hw_class` — **live state**: the personality the DPU currently has. NULL for
  non-DPU resources and for a DPU never provisioned.

Truth rule: `inventory/host_vars/dpu-01.yml` holds the Phase-1 bootstrap value; after that,
`resources.hw_class` in the DB is the single source of truth for scheduling, updated by the
engine when `dpu_prepare.yml` succeeds. Ownership of writes to `resources.hw_class`:

| Event | Effect on `resources.hw_class` |
|---|---|
| `daolab sync` | Never written (preserved on upsert, NULL on new rows). |
| `provisioning` → `ready` | Set to `usecase.hw_class` if `dpu_prepare` reflashed. |
| `tearing_down` → `released` | **Left untouched** — the DPU keeps its last personality. |
| DIY reservation | Never touched (DIY runs `host_vm.yml` only — no `dpu_prepare.yml`, no `verify.yml`). |

Consequences (both consumers detailed in [03-scheduler.md](03-scheduler.md) and
[04-lifecycle-engine.md](04-lifecycle-engine.md)):

- `playbooks/roles/ebf_set` runs inside `dpu_prepare.yml` **iff** the use case declares an
  `hw_class` different from the matched setup's DPU `resources.hw_class` at that time. A
  back-to-back rerun of the same use case therefore skips the reflash entirely.
- Lead time: `lead = DAOLAB_HW_CLASS_LEAD_MIN` (default 15) minutes when the declared
  `hw_class` differs from the DPU's current one at placement time, else
  `lead = DAOLAB_PROVISION_LEAD_MIN` (default 5). Scheduler and engine use the identical rule:
  the setup must be free for `[start_at − lead, end_at]`, and provisioning begins at
  `start_at − lead`.

## Data invariants

1. Use cases and setups are data, never code: their DB rows are pure YAML mirrors (plus seeded
   catalog metadata); no code path hardcodes a use case, setup, or topology.
2. `setups.capabilities` = sorted, deduplicated union of member + link capabilities, recomputed
   only by `daolab sync`; matching is always `requires ⊆ capabilities`, never hostnames.
3. JSON TEXT columns hold canonical JSON, touched only by `models.py`; all timestamps are UTC
   ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`; every connection runs WAL + `foreign_keys=ON`.
4. Kebab-case natural names are the external identifiers (YAML, API bodies, CLI); integer ids
   are internal (API paths/responses only); `reservation_id=0` is never issued.
5. `reservations.state` is CHECK-constrained to the seven states; `released`/`failed` are
   terminal; `failure_reason` only in `failed`; `diy_templates` non-NULL only when `diy = 1`.
6. `resources.hw_class` is written only by the engine on successful `dpu_prepare`; sync
   preserves it and teardown never resets it — only a differing next use case triggers `ebf_set`.
7. `vms` rows are unique per `(reservation_id, role)`; `ip` is set only through
   `POST /internal/vm-ip`; `template`/`name` are NULL exactly for `on: dpu` roles.
8. Sync is atomic: validate everything, then one transaction; a removal that would break a
   reservation not in `released`/`failed` aborts the whole sync naming the blocker.
9. Passthrough prep (pciback) is one-time dom0 state and BDFs live in `inventory/host_vars/` —
   neither is modeled in the DB or the setup YAML.
