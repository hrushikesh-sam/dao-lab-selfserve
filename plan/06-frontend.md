# Frontend — user and admin dashboards

Part of the daolab implementation plan — see [README.md](README.md) for the doc map.

Scope: the two files in `dashboard/` — `user.html` and `admin.html` — served statically by
`service/api.py` from `DAOLAB_DASHBOARD_DIR` (`GET /` → `dashboard/user.html`, plus
`GET /user.html` and `GET /admin.html`; the routes themselves are auth: none — the pages ask
for a token and send it on every API call). Endpoint request/response semantics are owned by
[02-backend-api.md](02-backend-api.md); slot-shift behavior by [03-scheduler.md](03-scheduler.md);
state meanings by [04-lifecycle-engine.md](04-lifecycle-engine.md). This document specifies only
what the pages render and which endpoints they call.

## Stack: two single-file HTML pages, vanilla JS, no build step

Governing invariant (root README): **dashboards are views over the API only**. Every value on
screen — availability, granted slots, states, SSH lines, topology — is read from a JSON
response. The frontend computes nothing the backend already knows.

| Decision | Concretely | Rationale |
|---|---|---|
| Two single files | All CSS and JS inline in `dashboard/user.html` and `dashboard/admin.html` | Deploy = copy a file into `DAOLAB_DASHBOARD_DIR`; nothing to compile, version, or cache-bust. |
| Vanilla JS + `fetch` | ~300 lines of script per page against 9 small JSON endpoints | A framework earns its keep managing complex client state; dumb views over the API have none. |
| mermaid via CDN `<script>` tag | The **only** external asset, present in both files (identical `<head>` plumbing) | Setup YAML ships a `diagram` block; `admin.html` renders it verbatim, so topology never lives in frontend code. |
| No build pipeline | No npm, no bundler, no transpiler | Two files are reviewable in one sitting; the build system would outweigh the code it builds. |

Anything that looks like it needs frontend logic (filtering availability, computing shifts,
deriving `held_by`) is by definition an API responsibility — change `service/api.py`, not the page.

## Shared plumbing (identical in both files)

**Token entry.** On load, if `localStorage` has no token, show a modal gate: one password-type
input + Save. Save stores the pasted token under key `daolab_token`, then calls `GET /whoami`
and shows `username (role)` in the header. Any `401 unauthorized` from any call clears the key
and re-opens the gate (token rotated or revoked). Tokens come from `daolab add-user` — see
[08-cli-config-ops.md](08-cli-config-ops.md).

**API client.** One wrapper, one error type; nothing else touches `fetch`:

```js
const TOKEN_KEY = "daolab_token";
const getToken  = () => localStorage.getItem(TOKEN_KEY);

class ApiError extends Error {
  constructor(status, err) {            // err = {code, message, detail} per the API error model
    super(err.message);
    this.status = status; this.code = err.code; this.detail = err.detail;
  }
}

async function api(path, {method = "GET", body = null} = {}) {
  const res = await fetch(path, {
    method,
    headers: {"Authorization": "Bearer " + getToken(),
              ...(body ? {"Content-Type": "application/json"} : {})},
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401) { localStorage.removeItem(TOKEN_KEY); showTokenGate(); }
    throw new ApiError(res.status, data.error);
  }
  return data;
}

const toUtc = v =>                       // datetime-local (local tz) -> UTC ISO-8601 Z
  v ? new Date(v).toISOString().replace(/\.\d{3}Z$/, "Z") : "now";
```

**Polling.** `poll(refresh, 15000)` re-runs the page's refresh function every 15 s, skips ticks
while `document.hidden`, and fires immediately on `visibilitychange` back to visible. 15 s is
deliberate: the engine advances states once per `DAOLAB_ENGINE_INTERVAL_SEC` (default 60 s), so
15 s polling bounds staleness at ~75 s worst case for trivial load (each tick is 1–2 GETs).
Every successful POST/DELETE calls `refresh()` immediately — user actions never wait for a tick.
A manual refresh button calls the same function.

**Error and empty states.** One dismissible banner at the top of the page renders
`ApiError` as `code — message`, with `detail` pretty-printed inside a `<details>` element
(e.g. the `unmet_capability` message names the missing capability list; render it verbatim).
A `fetch` network failure (API down) shows a persistent "API unreachable — retrying" banner,
cleared by the next successful poll. Empty datasets never render blank tables:

| Dataset empty | Message shown |
|---|---|
| `GET /usecases` returns `[]` | "catalog empty — run `daolab init && daolab sync`" |
| `GET /reservations` returns `[]` | "no reservations yet — pick a use case above" |
| `GET /pools` returns `[]` | "no pools — run `daolab sync` to load `setups/*.yml`" |
| `queue` array empty on a setup | "queue empty" |

## user.html — catalog, request flow, my reservations

```
+------------------------------------------------------------------------------+
| daolab                                 hrushi (user)  [change token] [refresh]|
+------------------------------------------------------------------------------+
| USE-CASE CATALOG                                                              |
| +-----------------+------------------------+---------+---------------+------+ |
| | use case        | what it is             | status  | availability  |      | |
| +-----------------+------------------------+---------+---------------+------+ |
| | tls-proxy-nginx | NGINX TLS termination..| active  | available     |[Rsv] | |
| | virtio-l2fwd    | L2 forwarding between..| planned | no YAML yet   |      | |
| | ...13 more planned rows...                                                | |
| | diy             | bare setup, no DAO app | active  | available     |[Rsv] | |
| +-----------------+------------------------+---------+---------------+------+ |
|                                                                               |
| MY RESERVATIONS                                                               |
| +----+-----------------+----------+---------+------------------------+------+ |
| | id | usecase         | setup    | state   | slot                   |      | |
| | 12 | tls-proxy-nginx | setup-01 | ready   | 09-01 10:00Z -> 12:00Z |[Can] | |
| |    |   backend  ssh hrushi@10.20.0.21     client  ssh hrushi@10.20.0.31  | |
| | 13 | tls-proxy-nginx | setup-01 | queued  | 09-01 12:00Z -> 14:00Z |[Can] | |
| |    |   (shifted from 10:00Z)                                             | |
| +----+-----------------+----------+---------+------------------------+------+ |
+------------------------------------------------------------------------------+
```

**Catalog table** — one row per element of `GET /usecases`, in API order; the `diy` row arrives
in the array like any other and is rendered identically. Adding a use-case YAML and running
`daolab sync` flips a row to available with zero frontend change (root README invariant).

| Column | Source field | Rendering |
|---|---|---|
| use case | `name` | code style; `variant` appended small when non-null |
| info | `doc_summary`, `doc_url`, `requires`, `hw_class`, `roles`, `setups` | ⓘ button on every row (planned included) opens a modal: verbatim DAO doc quote + link, requires/hw_class chips, roles table, and per matching setup a generated testbed sentence + the mermaid diagram from its `setups` entry |
| what it is | `description` | plain text |
| status | `status` | badge: `active` (green) / `planned` (grey) |
| availability | `available`, `reason`, `missing`, `next_free_at` | rules below |
| action | — | **Reserve** button iff `available === true` |

Availability cell rules (all server-computed; the page only maps to text):
`available: true` → "available" plus "next free `next_free_at`" when `next_free_at` is later
than now; `reason: "no_yaml"` → "planned — no YAML yet"; `reason: "unmet_capability"` →
"missing: " + `missing` joined with ", "; `reason: "no_free_slot"` → "no free slot within
horizon".

**Request flow — use-case mode.**
1. Reserve opens a modal: *start* (`datetime-local` input, blank = now) and *hours*
   (`<input type=number min=1 value=1>`).
2. Submit → `POST /reservations` with `{"usecase": row.name, "start_at": toUtc(start),
   "hours": hours}`. The scheduler auto-matches a setup by capabilities — the page never picks
   or names one in this mode.
3. `201` returns the reservation object. Confirmation card: `#id` on `setup`,
   `start_at → end_at`, state `queued`. If `shifted` is true, add a highlighted line
   "slot shifted from `requested_start_at`" — slot conflicts are **never** errors; the granted
   slot is the answer ([03-scheduler.md](03-scheduler.md)).
4. Errors surface in the banner verbatim: `usecase_unavailable` (409, planned row raced),
   `unmet_capability` (409, message names the missing capabilities), `validation_error` (422).
5. `refresh()` immediately — the new reservation appears in *My reservations*.

**Request flow — DIY mode.** The `diy` row's Reserve opens the DIY variant of the modal, adding:
- *setup* — text input for the setup name. When `GET /whoami` said the token is admin, upgrade
  to a dropdown populated from `GET /setups` (that endpoint is admin-only, so plain users type
  the name — matching the CLI's `daolab reserve diy ... --setup NAME`).
- *templates* — an editable list of `host = template` rows (add/remove row; placeholder
  template `tpl-ubuntu2204-dao`). With an admin token, host keys are prefilled from the chosen
  setup's distinct member `host` values.

Submit → `POST /reservations` with `{"usecase": "diy", "start_at": ..., "hours": ...,
"setup": "<name>", "templates": {"<host>": "<template>", ...}}`. Server-side validation is
authoritative: unknown setup → `404 not_found`; a template map not covering every VM host of
the setup → `422 validation_error` whose message names the problem — display it verbatim so a
non-admin user can correct the host list. Confirmation is identical to use-case mode; the
returned object has `"diy": true`.

**My reservations** — `GET /reservations` (own reservations, newest first; each element is the
full reservation object, `vms` included). Row: `id`, `usecase`, `setup`, state badge,
`start_at → end_at` with a "shifted" tag when `shifted`, action. Expanded detail per row:
`failure_reason` (state `failed` only) and a `vms` table — `role`, `host`, `template`, `name`,
`ip`, and the `ssh` string rendered as copyable code (e.g. `ssh hrushi@10.20.0.21`). `ip` null
→ "waiting for IP"; `template`/`name`/`ssh` null on `on: dpu` roles → "runs on the DPU (no VM)".
The `vms` table doubles as the credentials view — per role: where it runs, VM name, IP, the
copyable `ssh` login, and `username` + "your registered SSH key" from the `auth` field. Below
it the reservation's `diagram` (server-generated mermaid of the provisioned topology with IPs)
is rendered via the same mermaid plumbing as admin.html.

| state | badge | SSH shown | Cancel button |
|---|---|---|---|
| `queued` | grey | no (`vms` is `[]`) | yes → `released` |
| `provisioning` | amber | no (ips filling in) | no (API would 409 `invalid_state`) |
| `ready` | green | yes | yes → `cleaning` |
| `active` | green | yes | yes → `cleaning` |
| `cleaning` | amber | no | no |
| `quarantined` | red | no | no (API would 409 `invalid_state`) |
| `released` | muted | no | no |
| `failed` | red + `failure_reason` | no | no |

Cancel = confirm dialog → `DELETE /reservations/{id}` → response `{"id", "state"}` updates the
row instantly, then `refresh()`.

Build order: (1) token gate + `api()` + banner; (2) catalog table; (3) use-case reserve modal;
(4) DIY modal; (5) my-reservations list + cancel; (6) polling wiring; (7) empty/error pass.

## admin.html — drill-down, topology, holds, queue

```
+------------------------------------------------------------------------------+
| daolab admin                            admin (admin)  [change token] [refresh]|
+----------------+--------------------------------------------------------------+
| POOLS          | SETUP setup-01                                pool lab-1     |
|  lab-1         |  capabilities: e810  octeon-cn106  sdp  wire                 |
|   > setup-01 * |  members:  name    kind  host        hw_class     caps       |
|                |            dpu-01  dpu   dpu-01-xcp  sdp-pf1-vf8  octeon..   |
| (* = held)     |            tg-01   nic   tg-01-xcp   -            e810       |
|                |  links:    dpu-01.eth0  <-- wire -->  tg-01.e810p0           |
|                |  +--------------------------------------------------------+  |
|                |  | topology (mermaid SVG rendered from setup YAML diagram)|  |
|                |  +--------------------------------------------------------+  |
|                |  held_by: #12 hrushi tls-proxy-nginx active 10:00Z->12:00Z   |
|                |  queue:   #13 hrushi tls-proxy-nginx queued 12:00Z->14:00Z   |
+----------------+--------------------------------------------------------------+
| ALL RESERVATIONS (GET /reservations?all=1)                                    |
|  id  user    usecase          setup     state    start_at -> end_at   [Cancel]|
+------------------------------------------------------------------------------+
```

**Data flow.** After the role gate (next section): `GET /pools` fills the left column (each
pool row lists its `setups` names); `GET /setups` returns everything the detail panel needs —
`members`, `links`, `diagram`, `held_by`, `queue`; `GET /reservations?all=1` fills the global
table. All three are polled together by one `refresh()`.

**Drill-down.** Click a setup name → detail panel from the matching `/setups` element:
- Header: `name`, `pool`, `capabilities` (the sorted union computed by `daolab sync` — shown,
  never recomputed client-side).
- Members table: `name`, `kind`, `host`, `hw_class`, `capabilities`. `hw_class` is "—" for
  non-DPU resources. For the DPU it is the personality **currently on the card**, which
  survives teardown (invariant: DPU keeps its last `hw_class`) — showing it explains why the
  next same-`hw_class` reservation gets the short lead and a differing one triggers an EBF
  reflash ([04-lifecycle-engine.md](04-lifecycle-engine.md)).
- Links: one line per `{a, b, capability}` as `a <-- capability --> b`.
- Topology diagram, rendered from the `diagram` field **verbatim** — the mermaid source ships
  inside the setup YAML and reaches the page through the API; the frontend contains zero
  topology knowledge (invariant). Editing the YAML + `daolab sync` changes the drawing with no
  frontend change.

```js
mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: "neutral"});
async function renderDiagram(setup) {                  // setup = one GET /setups element
  const {svg} = await mermaid.render("diagram-" + setup.id, setup.diagram);
  document.getElementById("topology").innerHTML = svg;
}
```

**Holds and queue.** `held_by` is a reservation-summary
`{"id", "user", "usecase", "state", "start_at", "end_at"}` or `null` ("free"); render its state
with the same badges as user.html — a teardown-failure reservation surfaces here in `failed`
until an admin intervenes manually. `queue` lists the `queued` summaries ordered by `start_at`.
The pools column marks held setups. The global **ALL RESERVATIONS** table shows every user's
reservations with an admin Cancel (`DELETE /reservations/{id}`, allowed in
`queued`/`ready`/`active`).

**Users panel.** `GET /users` fills a table — `username`, `role` badge, `enabled` badge,
`active_reservations` count. *Disable* (confirm dialog) and *Enable* call `PATCH /users/{id}`
with `{"enabled": bool}`; the signed-in admin's own row has its Disable button inert (the API
answers `409 invalid_state` regardless). Disabling kills the user's token immediately
(`403 forbidden`, `detail.reason: "user_disabled"`) but leaves their reservations untouched —
cancel those in ALL RESERVATIONS if the hold must end.

Build order: (1) role gate; (2) pools column; (3) setup detail panel; (4) mermaid render;
(5) held_by + queue; (6) global table + admin cancel; (7) users panel + enable/disable;
(8) polling + empty/error pass.

## Component-to-endpoint map

| Page | Component | Endpoint(s) | Notes |
|---|---|---|---|
| both | token gate / header identity | `GET /whoami` | on token save and page load |
| both | API status indicator | `GET /health` | auth none; drives the "API unreachable" banner |
| user.html | use-case catalog table | `GET /usecases` | polled |
| user.html | reserve modal (use-case mode) | `POST /reservations` | on submit |
| user.html | DIY modal | `POST /reservations`; `GET /setups` | `/setups` only for admin tokens (dropdown assist); others type the name |
| user.html | my-reservations list | `GET /reservations` | polled; full objects incl. `vms`/`ssh` |
| user.html | post-action row refresh | `GET /reservations/{id}` | after reserve/cancel |
| user.html | cancel button | `DELETE /reservations/{id}` | `queued`/`ready`/`active` only |
| admin.html | role gate | `GET /whoami` | refuses render unless `role == "admin"` |
| admin.html | pools column | `GET /pools` | polled |
| admin.html | setup detail (members, links, diagram, held_by, queue) | `GET /setups` | polled; one call drives the whole panel |
| admin.html | global reservation table | `GET /reservations?all=1` | polled; admin token required |
| admin.html | admin cancel | `DELETE /reservations/{id}` | any user's reservation |
| admin.html | users panel | `GET /users` | polled; enabled badge + active count |
| admin.html | enable/disable toggle | `PATCH /users/{id}` | confirm on disable; self-row inert |
| — | never called by the frontend | `POST /internal/vm-ip` | internal auth (`X-Daolab-Secret`); playbook callback only |

## Admin gating

`admin.html` gates on role, not on a separate credential: after the token gate it calls
`GET /whoami` and **refuses to render unless `role == "admin"`**. A `role = 'user'` token gets
a single message — "admin token required (this token has role `user`)" — with a link to
`/user.html`, and the page issues no further API calls. This gate is UX only; enforcement is
server-side: `GET /pools`, `GET /setups`, and `?all=1` return `403 forbidden` to user tokens,
and the page treats an unexpected 403 exactly like a failed gate. The converse holds too —
admin ⊇ user, so an admin token drives `user.html` fully (and unlocks the DIY dropdown assist).

## Deliberate non-goals

- **No framework** (React/Vue/htmx) and **no build pipeline** (npm, bundlers, transpilers):
  two static files against 9 endpoints; the toolchain would exceed the code it ships.
- **No websockets, no SSE — polling only**: the engine ticks once per
  `DAOLAB_ENGINE_INTERVAL_SEC` (60 s), so 15 s polling already over-samples reality.
- **No SPA/router**: two audiences, two files, two URLs.
- **No client-side scheduling or availability math**: `available`/`reason`/`missing`/
  `next_free_at` and granted slots come exclusively from the API (dumb-views invariant).
- **No topology drawing code**: diagrams arrive as mermaid source through `GET /setups`.
- **User creation stays in the CLI**: `daolab add-user` issues users and tokens
  ([08-cli-config-ops.md](08-cli-config-ops.md)); `admin.html` manages only the `enabled`
  flag (enable/disable) via `PATCH /users/{id}`.

Revisit trigger — the same one that moves sqlite → Postgres (root README Phase 4, >1 API
worker): only when the API outgrows a single process does push-based updating or a build step
deserve reconsideration.

## Test plan — manual checklist against a seeded DB

Seed once (no hardware needed — Phase 2 scope):

```
daolab init && daolab sync
daolab add-user hrushi ~/.ssh/id_ed25519.pub                 # note user token
daolab add-user hrushi-admin ~/.ssh/id_ed25519.pub --role admin   # note admin token
make api                                                     # http://localhost:8080
DAOLAB_TOKEN=<user-token> daolab reserve tls-proxy-nginx 2026-09-01T10:00 --hours 2
DAOLAB_TOKEN=<user-token> daolab reserve tls-proxy-nginx 2026-09-01T10:00 --hours 2   # -> shifted
```

**user.html** (open `http://localhost:8080/`):
1. No token → gate shown. Garbage token → first call 401s, gate returns. Valid token → header
   shows `hrushi (user)`.
2. Catalog renders 16 rows (15 README use cases + `diy`); `tls-proxy-nginx` is `active` with a
   Reserve button; all planned rows read "planned — no YAML yet" with no button; `diy` is
   `active`.
3. Reserve `tls-proxy-nginx` at `2026-09-01T10:00` again → confirmation shows a later slot and
   "slot shifted from 2026-09-01T10:00:00Z".
4. My reservations lists the CLI-created reservations, state `queued`, second one tagged
   shifted; Cancel one → row flips to `released` immediately.
5. Simulate `ready` to check SSH rendering:
   `sqlite3 daolab.db "UPDATE reservations SET state='ready' WHERE id=1; INSERT INTO vms
   (reservation_id, role, host, template, name, ip, created_at, updated_at) VALUES
   (1,'client','tg-01-xcp','tpl-ubuntu2204-dao','daolab-r1-client','10.20.0.31',
   strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'));"` — within one
   poll the row shows `ssh hrushi@10.20.0.31`; Cancel is enabled again.
6. DIY with an unknown setup name → banner shows `not_found`; DIY on `setup-01` with only one
   `host = template` row → banner shows the `validation_error` message naming the uncovered
   host; with both `dpu-01-xcp` and `tg-01-xcp` mapped to `tpl-ubuntu2204-dao` → `201`, row
   shows usecase `diy`.
7. Kill the API → "API unreachable — retrying" appears; restart → clears on next poll. Switch
   to another tab for 1 min → no polls fired (check server log); switch back → immediate
   refresh.

**admin.html** (open `http://localhost:8080/admin.html`):
1. User token → "admin token required" message, and the server log shows no `/pools` or
   `/setups` calls. Admin token → full render, header `hrushi-admin (admin)`.
2. Pools column shows `lab-1` → `setup-01`. Detail: capabilities exactly
   `e810 octeon-cn106 sdp wire`; members `dpu-01` (`dpu`, `dpu-01-xcp`) and `tg-01` (`nic`,
   `tg-01-xcp`); link `dpu-01.eth0 <-- wire --> tg-01.e810p0`.
3. Topology renders as an SVG matching the `diagram` block of `setups/setup-01.yml`. Edit that
   block, run `daolab sync`, refresh → the drawing changes with no frontend edit (proves no
   hardcoded topology).
4. With the simulated `ready` reservation from user-test 5, `held_by` shows its summary and the
   pools column marks `setup-01` held; the remaining `queued` reservation appears under
   `queue` ordered by `start_at`.
5. ALL RESERVATIONS lists both users' reservations; admin Cancel on a `queued` one flips it to
   `released`. `curl -H "Authorization: Bearer <user-token>"
   "http://localhost:8080/reservations?all=1"` → `403 forbidden` (server-side gate holds).
