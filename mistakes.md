# daolab plan set — mistakes, contradictions and unverified claims

Audit of `main` (`origin/main` = `82b32bd`) covering the root `README.md`, all 11 documents in
`plan/`, `inventory/hosts.yml`, `setups/setup-01.yml`, and the plan's reference to `plan/09` /
`plan/10`.

- **Audited snapshot:** `C:\Users\kalar\Downloads\self_serve_DAO\dao-lab-selfserve-main` (byte-identical to `origin/main`).
- **Verdict scale:** `WRONG` (objectively false / cannot work), `CONFLICT` (two docs cannot both be true), `UNVERIFIED` (could not be confirmed from public sources — not the same as wrong), `OK` (checked and correct).
- **Rule applied:** every claim is cited as `file:line`. External claims carry a source link. Nothing is asserted from memory alone.
- **Status:** re-checked against branch `rama` on 2026-10-23 (at `565b8ec`, plus the `plan/02` /
  `plan/08` updates committed with this file) and committed there. Every item now carries its
  status on `rama` after its section. Objective errors in the audit itself (counts, citations,
  cross-references) are corrected in place; judgement-level corrections are in
  [O](#o-corrections-to-the-original-audit), new problems the re-check found in
  [P](#p-new-findings-from-the-re-check).
- **Hardware facts** marked `F` come from the live lab (tg-01-xcp `10.68.0.52`, dpu-01-xcp `10.68.0.53`, board dpu-01 `10.68.0.190`; the management network is `10.68.0.0/22` — the board's `eth5` is `10.68.0.190/22`).
- **Status legend (re-check):** `FIXED` (the defect is gone on `rama`), `PARTIAL` (what remains is
  named), `OPEN` (still true), `OBSOLETE` (the premise no longer applies), `AUDIT-WRONG` (the
  audit's claim was incorrect — see O), `SETTLED` (an unverified claim is now confirmed or
  refuted); for E: `OK` / `CHANGED`. Evidence is `file:line` on `rama`.

## Status on `rama` — re-check of 2026-10-23

The branch took the `plan/09` design (Option B): `deploy.yml` + `cleanup.yml` with roles,
`cleaning` / `quarantined`, jobs with compare-and-set, IPAM leases + cloud-init, numbered
migrations (v1–v10), reconcile + orphan sweep, quarantine + force-clean, OTel / health rules,
quotas, the `lab` DPU login. The DPU path (VPP on xeon3-oct-112) runs end to end from the
dashboard on the real board; the use cases' VMs have not run on hardware (`DAOLAB_DEPLOY_VMS=false`).

| Section | Items | Fixed | Partial | Open | Obsolete | Audit wrong | Settled |
|---|---|---|---|---|---|---|---|
| A. Blockers | 8 | 1 | 3 | 3 | 1 | – | – |
| B. Factual errors | 15 | 4 | 2 | 7 | – | 2 | – |
| C. Contradictions | 15 | 8 | 4 | 3 | – | – | – |
| D. Unverified claims | 10 | – | 3 | 5 | – | – | 2 |
| F. Hardware reality | 4 | – | 1 | 3 | – | – | – |
| H. Overlaps | 12 | 4 | 5 | 1 | – | 2 | – |
| I. Clashes | 20 | 7 | 6 | 7 | – | – | – |
| J. Reliability holes | 18 | 3 | 10 | 4 | – | 1 | – |
| K. Structural gaps | 10 | 3 | 4 | 3 | – | – | – |
| L. Sequencing | 5 | – | 4 | – | 1 | – | – |
| M. Invariants | 7 | 1 | 3 | 3 | – | – | – |
| **Total** | **124** | **31** | **45** | **39** | **2** | **5** | **2** |

E (verified correct, 20 items): 19 still `OK`, 1 `CHANGED` (E19, refined, still sound).

**Still open and most important, ranked:**

1. **Topology (A1, A7, F1, F3).** The inventory has one placeholder XCP-ng host (`tb-01-xcp`,
   `10.0.0.10`); the lab has two (`tg-01-xcp` 10.68.0.52 with the E810 VFs, `dpu-01-xcp`
   10.68.0.53). Nothing VM-side can run on hardware until the inventory, `setups/` and the docs
   describe the two hosts — this is the Xen Orchestra / VM work.
2. **P1** — a deploy can start over a failed reservation whose cleanup is still pending.
3. **The TLS use case cannot pass** — its verify string is wrong (B1) and its health rule has no
   data (B2, P2).
4. **No functional gate on a DPU-only deploy** (I10, P3); `verify` is a substring match (I9).
5. **A hung playbook blocks the single engine** (J5, P4).
6. **The doc set still describes the old design** outside `plan/02–05` and `plan/09` (A2, A8,
   C9, C14, I19, M4).
7. **No CI (K6), no shipped backups (J15), tokens never expire (J13).**

## Summary (original audit, `82b32bd`)

| Class | Count | Nature |
|---|---|---|
| A. Blockers | 8 | The plan set targets **two different systems**; nothing can be built until one is picked. |
| B. Factual errors | 15 | Wrong regardless of which design wins (h2load output, OTel metric names, SQLite FK semantics, stale counts, broken references). |
| C. Internal contradictions | 15 | Two documents give incompatible contracts for the same thing. |
| D. Unverified claims | 10 | Plausible but not confirmable from public sources — must be checked on the board before Phase 1. |
| E. Verified correct | 20 | Checked and right — do not "fix" these. |
| F. Hardware reality | 4 | What the lab actually is; constrains which design is implementable. |
| **H. Overlaps** | 12 | The same rule implemented in two or more places → they will drift. |
| **I. Clashes** | 20 | Two mechanisms that cannot both be in the same flow. |
| **J. Reliability holes** | 18 | State that can silently drift, or failures with no recovery path. |
| **K. Structural gaps** | 10 | Components/behaviours the design never provides. |
| **L. Sequencing flaws** | 5 | The plan's ordering puts risk in the wrong place. |
| **M. Invariant violations** | 7 | The design breaks its own stated rules. |

**Part I (A–F)** is the line-cited audit of *what the documents say*.
**Part II (H–N)** is the design review of *whether the system works* — overlaps, clashes,
unreliable mechanisms and missing pieces across the whole daolab system, independent of which of
the two designs wins. Part II is judgement, not citation: each item states the reasoning and the
consequence, with the lines it rests on.

**The single most important finding (at `82b32bd`):** `plan/09` (and `plan/10` derived from it) describes a
**different product** from `README.md` + `plan/01–08` — a different topology, a different playbook
set, different states, a different booking policy, a different cancel API and a different IP model.
`plan/README.md:15-16` states the precedence rule ("root `README.md` > these plan docs. A plan doc
never contradicts the spec; it refines it.") — `plan/09` breaks that rule on six axes at once, and
`plan/10` (its tasksheet) is built on it. `plan/09:509-510` does say its changes "should be folded
back into those docs when P1–P3 land" — i.e. `plan/09` is an unmerged proposal, not a replacement.

---

## A. Blockers — the plan set describes two different systems

| ID | Subject | Version 1 (README + plan/01–08) | Version 2 (plan/09 + plan/10) | Impact |
|---|---|---|---|---|
| **A1** | Topology | Two XCP-ng hosts: `dpu-01` on `dpu-01-xcp`, `tg-01` on `tg-01-xcp` — `README.md:6-7`, `README.md:112-120`, `plan/README.md:32-40`, `plan/05:305-307`, `inventory/hosts.yml`, `setups/setup-01.yml` | One XCP-ng host `tb-01-xcp` + DPU — `plan/09:43-63`, `plan/09:113-118`, `plan/09:503`, `plan/09:521-524`, `plan/10:64` (B-04), `plan/10:144` (B-81), `plan/10:149` (B-86) | Every inventory fact, BDF, `on:` selector, capability set and play in the set is host-specific. The lab has the two-host topology (F1). |
| **A2** | Playbook set | Four: `dpu_prepare.yml`, `host_vm.yml`, `verify.yml`, `teardown.yml` — `README.md:29`, `README.md:147-149`, `README.md:173`, `plan/README.md:51`, `plan/README.md:78`, `plan/README.md:107`, `plan/04:161-164`, `plan/05:7`, `plan/05:141`, `plan/05:166-276`, `plan/07:213-222`, `plan/08:143` | Two: `deploy.yml` + `cleanup.yml` — `plan/09:22`, `plan/09:114`, `plan/09:161-162`, `plan/09:261`, `plan/09:275-278`, `plan/09:503`, `plan/09:521-522`, `plan/10:143` (B-80), `plan/10:145` (B-82) | `plan/09:275-278` says the four become *roles* of `deploy.yml`, but no doc specifies that mapping, the role boundaries, or how `verify`/`teardown` idempotency and the `reservation_id=0` manual path survive. |
| **A3** | Reservation states | `queued → provisioning → ready → active → tearing_down → released` (+`failed`) — `README.md:22`, `README.md:37`, `plan/README.md:29`, `plan/01:221-222`, `plan/02:132-133`, `plan/04:13-16`, `plan/04:27-30`, `plan/05:271-274`, `plan/06:175-183` | `… → cleaning → released`, plus `quarantined` — `plan/09:29`, `plan/09:289-291`, `plan/09:398-399`, `plan/09:409`, `plan/09:429-438`, `plan/09:476`, `plan/10:87` (B-21), `plan/10:89` (B-23), `plan/10:93` (B-27), `plan/10:139` (B-71) | Also breaks the DDL: `plan/01:221-222` has a `CHECK (state IN (...))` list without `cleaning`/`quarantined`, and B-21 touches only `models.py`, `api.py` — `schema.sql` must change too (C7). |
| **A4** | Booking conflict policy | Never reject, always slot-shift — `README.md:4`, `README.md:161-162`, `plan/README.md:27-28`, `plan/README.md:181`, `plan/02:263-266`, `plan/03:25`, `plan/03:214-215`, `plan/06:142-144`, `plan/08:85`, `plan/08:92-95` | `mode: at` overlaps → `409 slot_conflict` with alternatives — `plan/09:23-24`, `plan/09:186-188`, `plan/09:221`, `plan/09:231-239`, `plan/09:472`, `plan/09:498`, `plan/09:512-514`, `plan/10:76` (B-14) | Different HTTP semantics for the same request; the frontend error paths differ (`plan/06:145-146` has no `slot_conflict` case). |
| **A5** | Cancel / end-early API | `DELETE /reservations/{id}` (`queued`→`released`, `ready`/`active`→`tearing_down`, else `409 invalid_state`) — `plan/02:178-182`, `plan/03:215`, `plan/06:185-186`, `plan/06:271`, `plan/06:276` | `POST /reservations/{id}/cleanup` (202 + job) + `POST …/redeploy` — `plan/09:161-163`, `plan/09:476`, `plan/10:93-94` | The dashboard, the CLI and the state machine all key off the old contract. |
| **A6** | VM IP model | IP discovered from guest tools over DHCP, then data IPs set statically in-guest via netplan — `plan/05:151-153`, `plan/05:337`, `plan/05:347`, `plan/07:52-54`, `plan/07:151-153`, `plan/07:172-173`, `plan/02:275-283` | IP **leased by the backend** (`POST /internal/leases`) and set statically **through cloud-init** — `plan/09:280-293`, `plan/09:262-268`, `plan/09:521-522`, `plan/10:143` | Removes the whole "post your IP back to us" callback for the VM roles; changes the API surface, the schema (new `ip_leases` tables, `plan/09:458`) and `host_vars`. |
| **A7** | Inventory | `inventory/hosts.yml` + `setups/setup-01.yml` = `dpu-01`/`dpu-01-xcp` + `tg-01`/`tg-01-xcp` — `README.md:112-120`, `plan/README.md:34-40` | Collapse to `tb-01`/`tb-01-xcp` with Y-cable links — `plan/09:44-53`, `plan/09:521-524`, `plan/10:64` (B-04) | B-04 rewrites files the rest of the plan set depends on by name. |
| **A8** | The fix-up task cannot fix the fix-up | — | `plan/10:155` (B-90) says "Fold the design changes back into **02–05**", done-when "no doc contradicts 09" | Unreachable: `plan/01` (CHECK constraint), `plan/06` (dashboard), `plan/07` (use case), `plan/08` (env/ops), `plan/README.md` (counts, glossary) and the root `README.md` all also contradict `plan/09`. B-90's scope must be widened. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| A1 | **OPEN** | `inventory/hosts.yml:13-21`, `setups/tb-01.yml:4-9`, `plan/05:21-51`, `plan/09:43-62` (one host, `tb-01-xcp` = placeholder `10.0.0.10`); still two hosts in `README.md:6-7,141-153`, `plan/README.md:32-40`, `plan/01`, `plan/06`, `plan/07` | The branch picked the single-host topology F1 says does not exist; 10.68.0.52/.53 appear nowhere in the repo. Blocks every VM on hardware. |
| A2 | PARTIAL | `playbooks/{deploy,cleanup,probe,verify_clean,reconcile,orphans,bringup}.yml`; role mapping `plan/05:159-181`; manual path `plan/05:132-135,577-583` | Decided and specified in `plan/05`; `README.md:62,179-186`, `plan/README.md:51,78,107`, `plan/01:304-313`, `plan/07:56-229` still say four playbooks. |
| A3 | FIXED | `service/models.py:13`; `service/migrations.py:167-195` (v6 rewrites the CHECK, renames rows); `README.md:55,70`; `plan/04:18` | `schema.sql` stays the v0 baseline the migrations upgrade. |
| A4 | PARTIAL | `plan/03:17-18,35,47`; `service/api.py:741` (`at` → 409, `asap` shifts) | Option B as a superset; `plan/README.md:26-28,181` and `plan/06:135-147` still say "never rejected"; no client sends `mode: at`. |
| A5 | OBSOLETE | `service/api.py:797-826` (DELETE), `:829` (POST …/cleanup), `:859` (…/redeploy) | Both kept with one meaning; only `plan/06`'s map lacks `/cleanup` (C5). |
| A6 | PARTIAL | `service/ipam.py`; `service/api.py:1050,1067`; `roles/lease_ip`; `plan/05:88-106` | Lease + cloud-init in code; `plan/07:48-54,149-153,170-173` still DHCP / netplan. Not run on hardware. |
| A7 | OPEN | `inventory/hosts.yml:13-21`; `setups/tb-01.yml` (setup-01.yml deleted) | B-04 landed, but `README.md:141-153`, `plan/README.md:32-40`, `plan/01`, `plan/06` still name `setup-01`; the inventory does not match the lab (F1). |
| A8 | OPEN | `plan/10:163` (B-90 still scoped to `plan/02–05`) | As predicted: 02–05 folded; 01, 06, 07, `plan/README.md`, `README.md` still contradict 09. |

---

## B. Factual errors (wrong no matter which design wins)

| ID | Location | Error | Verdict | Correct form / evidence |
|---|---|---|---|---|
| **B1** | `README.md:104`, `plan/01:290`, `plan/04:329-330`, `plan/05:400`, `plan/07:191`, `plan/07:231`, `plan/07:245`, `plan/07:323` | `verify: { …, expect: "200: 1000"}` — `h2load` never prints `200: 1000` | **WRONG** | `h2load` prints `requests: 1000, … 1000 succeeded` and `status codes: 1000 2xx, 0 3xx, 0 4xx, 0 5xx`. Use `expect: "1000 succeeded"` (or `"1000 2xx"`). This breaks the Phase-1 checkpoint in `README.md:150` and the engine's success signal in `plan/04:248`. Source: https://manpages.ubuntu.com/manpages/stonking/man1/h2load.1.html |
| **B2** | `plan/09:364-365` | Health rule `httpcheck_status == 1` for the TLS proxy | **WRONG** | The collector's `httpcheck` receiver exposes `httpcheck.status` with `http.status_class` / `http.status_code` labels (Prometheus: `httpcheck_status`, `http_status_class`, `http_status_code`). A bare `== 1` cannot distinguish a 2xx from a 5xx — NGINX answering 500 would pass the check. Also the receiver was renamed **`http_check`** (`httpcheck` is the deprecated alias). Rule should filter on `http_status_class="2xx"`. Source: https://pkg.go.dev/github.com/open-telemetry/opentelemetry-collector-contrib/receiver/httpcheckreceiver |
| **B3** | `plan/09:359` | `tcpcheck_status{daolab_reservation="$RID", port="22"}` | **WRONG** | The `tcpcheck` receiver's attributes are `tcpcheck.target` and `tcpcheck.port`; in Prometheus the label is `tcpcheck_port`, not `port`. The selector matches nothing → the check always evaluates to "no data" → verdict `unknown`. |
| **B4** | `plan/09:360-361` | `vpp_up{host="dpu-01"}` and `rate(vpp_interface_rx_packets{host="dpu-01"}[1m])` | **WRONG / UNVERIFIED** | There is no VPP metric `vpp_up` — Prometheus liveness is `up{job="vpp"}`. `vpp_interface_rx_packets` only exists if the DAO VPP build enables the `vpp_prom` exporter, which no document states. Both rules silently yield "no data" → `unknown`, so the health badge never goes green in P5/P6. |
| **B5** | `plan/01:355-358` | "because `reservations.setup_id`/`usecase_id` FKs have no cascade, terminal reservations referencing the removed row **are deleted with it** (their `vms` cascade)" | **WRONG** | `plan/01:217-219` declares `REFERENCES users(id)` / `usecases(id)` / `setups(id)` with **no `ON DELETE` clause** → SQLite default `NO ACTION`. With `PRAGMA foreign_keys=ON` (`plan/08:241`) deleting the parent raises `FOREIGN KEY constraint failed`; it does **not** cascade-delete the child reservation. Only `vms.reservation_id … ON DELETE CASCADE` (`plan/01:236`) cascades. `plan/08:275-276` states the correct behaviour ("historical `reservations` rows referencing the user block the DELETE"). The `daolab sync` removal path in `plan/01:352-358` would abort instead of deleting. |
| **B6** | `plan/02:37-39` vs `plan/08:132-151` | `Settings` dataclass has 10 fields; `plan/08` documents 18 variables read by cli/api/engine | **WRONG** | Missing at least `DAOLAB_USECASES_DIR`, `DAOLAB_SETUPS_DIR`, `DAOLAB_INVENTORY`, `DAOLAB_PLAYBOOKS_DIR`, `DAOLAB_ENGINE_INTERVAL_SEC`, `DAOLAB_MAX_RETRIES`, `DAOLAB_WEBHOOK_URL`, `DAOLAB_TOKEN`. `plan/02:30-31` claims "each field maps 1:1 to its env var". |
| **B7** | `plan/04:129-131` vs `plan/04:148` | Comment says `ok` = "status == 'successful' **and** rc == 0"; the code computes `ok=(r.status == "successful")` | **WRONG** | A playbook that ends with a non-zero rc is treated as success → the engine advances the reservation instead of retrying. |
| **B8** | `plan/06:22`, `plan/06:294` | "~300 lines of script per page against **9** small JSON endpoints" / "two static files against **9** endpoints" | **WRONG** | The endpoint map at `plan/06:262-279` lists **11** distinct method+route pairs (12 counting `?all=1` separately) (`/whoami`, `/health`, `/usecases`, `POST /reservations`, `GET /reservations`, `GET /reservations/{id}`, `DELETE /reservations/{id}`, `/pools`, `/setups`, `/reservations?all=1`, `/users`, `PATCH /users/{id}`). |
| **B9** | `plan/02:13-15`, `plan/08:162` | "served by `make api` … (default `http://localhost:8080`)" | **WRONG** | The bind address default is `DAOLAB_API_HOST = 0.0.0.0` (`plan/08:135`); `http://localhost:8080` is the `DAOLAB_API_URL` default (`plan/08:137`), a different variable. The bind default is a security decision that `plan/08:288-291` then discusses — conflating the two hides it. |
| **B10** | `plan/README.md:9`, `plan/README.md:112` | "The **nine** documents in `plan/`" / "**Nine** files." | **WRONG** | `plan/` holds **11** Markdown files (README + 01…10) and the document map at `plan/README.md:116-126` lists 11 rows. |
| **B11** | `plan/README.md:126` | "Task IDs B-01…B-91" | **WRONG** | `plan/10` defines **61** tasks (IDs up to B-91 with 30 unused: 7-9, 19, 31-39, 46-49, 56-59, 69, 75-79, 87-89). The IDs are sparse; the range notation is misleading. (The ownership split 25 + 36 = 61 at `plan/10:25-26` does match the real count.) |
| **B12** | `plan/08:33-34`, `plan/08:39` | "seeds the **15 planned** catalog rows" / "usecases seeded: 15 planned + diy" | **WRONG** | The README catalog (`README.md:61-77`) lists 15 use cases of which **1 is active** (`tls-proxy-nginx`) and 14 are planned. `plan/06:325` is correct ("16 rows (15 README use cases + `diy`)"). |
| **B13** | `plan/02:31`, `plan/03:5`, `plan/04:5`, `plan/05:8`, `plan/05:18`, `plan/05:75`, `plan/05:141`, `plan/05:177`, `plan/05:188`, `plan/05:194`, `plan/05:202`, `plan/05:238`, `plan/05:258`, `plan/05:274`, `plan/05:301`, `plan/05:361` | References to "contract §4/§5/§8/§9/§10/§12" | **WRONG (dangling)** | No numbered "contract" document exists in the repo; the root `README.md` has no §-sections. These point at a spec that is not here — the reader is sent to a nonexistent authority for the most load-bearing rules (extravar set, state machine, lead time). |
| **B14** | `plan/09:405` | Cleanup verify step 1: "No XCP-ng VM whose name **or tag** is `daolab-r<id>-*` (`xe vm-list tags:contains=daolab-r<id>`)" | **WRONG (vacuous)** | No plan step ever sets a VM `tags` field: VMs are created with `xe vm-clone … new-name-label=daolab-r<id>-<role>` (`plan/05:341`, `plan/07:141`) and teardown greps `name-label` (`plan/05:262-266`). The residue check therefore matches nothing and always passes — the cleanup safety net of `plan/09:9` (section 9) does not work. |
| **B15** | `plan/09:46`, `plan/09:50`, `plan/09:52-53`, `plan/09:61-63` | "OCTEON DPU **single QSFP port**", "its two QSFP28 ports go through the **Y cable** into the DPU's single port" | **WRONG / UNVERIFIED** | Marvell's public OCTEON 10 CN106XX spec lists Ethernet `4 × 50G + 2 × 10G`, so "single QSFP port" contradicts the published hardware. A passive Y-split between two QSFP28 legs and one port is not a standard interconnect. Also: an E810 exposes up to **256 VFs total, split evenly across ports** (128/port on a 2-port card) — plan/09's "one E810 VF per role VM" works, but its p0/p1 pairing assumption needs checking against how the VFs are actually enumerated. Sources: https://www.marvell.com/content/dam/marvell/en/company/media-kit/octeon-10/marvell-octeon-10-media-deck.pdf , https://cdrdv2-public.intel.com/630682/630682_E810%20SR-IOV%20SLES15%20ConfigGuide_Rev1.2.pdf |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| B1 | **OPEN** | `usecases/tls-proxy-nginx.yml:10` (`expect: "200: 1000"`), checked as a substring by `playbooks/roles/verify/tasks/run.yml:45-53` | The TLS verify would always fail on hardware. |
| B2 | **OPEN** | `plan/09:378-379`; `usecases/tls-proxy-nginx.yml:14`; `roles/otel_agent/templates/otelcol.yaml.j2:33` (`httpcheck`) | Worse than stated: `vm_httpcheck` is never set (only defaulted, `roles/host_vm/tasks/vm.yml:72`), so the rule has no data (P2). |
| B3 | FIXED | `roles/otel_agent/templates/otelcol.yaml.j2:26-31,52-56` (the pipeline adds `port="22"`) | The selector now matches. |
| B4 | FIXED | `playbooks/files/daolab-app-exporter:61-72` (`vpp_up`, `vpp_interface_*`); `playbooks/tasks/app_exporter.yml`; `usecases/vpp.yml:14-15` | daolab exports these itself; it served `vpp_up 1` on xeon3-oct-112 (2026-10-16). |
| B5 | OPEN (prose) | `plan/01:380-383` | The FK prose is still wrong; the behaviour never was (O). |
| B6 | FIXED | `plan/02` Settings (`:47-63`) | All 40 settings are in `plan/02`'s Settings and `plan/08`'s table (this re-check added `become_password_file`, `deployable`). |
| B7 | FIXED | `service/runner.py:564` (`status == "successful" and rc == 0`); `plan/04:345` | |
| B8 | OPEN | `plan/06:22,295` ("9"); map `plan/06:264-280` | The dashboards also call `/cleanup`, `/health/probe`, `/reservations/{id}/jobs`, force-clean — none in the map. |
| B9 | AUDIT-WRONG | `plan/02:18-20`; `plan/08:149` | See O. |
| B10 | OPEN | `plan/README.md:9,112` | Still "nine". |
| B11 | OPEN (overstated) | `plan/README.md:126` | The range is literally right; only "sparse" is missing (O). |
| B12 | AUDIT-WRONG | `plan/08:34,43,48`; `service/catalog.py:28-158` | See O. |
| B13 | PARTIAL | `plan/03:5,144,266,330`; `plan/04:5` | Gone from `plan/02` and `plan/05`. |
| B14 | PARTIAL | `verify_clean.yml:30-33`, `cleanup.yml:45-47`, `reconcile.yml:29`, `orphans.yml:20` (name-label prefix) | Code fixed; `plan/09:419` (`tags:contains=`) and `plan/10:148-149` ("tagged VMs") still say tags. |
| B15 | OPEN | `plan/09:50,52-53,61-62` | Now also contradicted inside the repo: `setups/tb-01.yml:8-9`, `plan/05:50-51` wire two DPU ports. |

---

## C. Internal contradictions (two docs, incompatible contracts)

| ID | Subject | Side A | Side B | Why it matters |
|---|---|---|---|---|
| **C1** | `http-server` package | `plan/05:225` — `install_map: {http-server: [nginx], ab: [apache2-utils], h2load: [nghttp2-client]}` | `plan/07:154-157` — "`install: http-server` maps … to `apt-get install -y apache2`"; `plan/07:237` then reads `/var/log/apache2/access.log` | Different web server on the backend VM; the payload path `/var/www/html/` is nginx's default, apache2's is `/var/www/html` too but its access log path and vhost differ. The role token `http-server` is ambiguous by name. |
| **C2** | `held_by` definition | `plan/02:132-133` — `held_by` = the reservation in `provisioning`/`ready`/`active`/`tearing_down` (excludes `failed`) | `plan/04:44`, `plan/05:271-274`, `plan/08:360` — a failed teardown keeps the setup held and "surfaces under `held_by` on `admin.html` until an admin intervenes" | Under `plan/02`'s definition the hold vanishes from the dashboard the moment it fails, which is exactly when an admin must see it. `plan/08:360`'s manual release step then cannot find the reservation it refers to. |
| **C3** | Busy-window boundary | `plan/02:212` — "a setup is busy for `[start_at − lead, end_at]`" (closed interval) | `plan/03:115` — "Intervals are **half-open** … a window may begin exactly at a busy interval's `end_at`" | Two different reservation rules at the boundary → the API and the scheduler can both believe a slot is free (double-booking). `plan/02:266` uses a third form (`[start − lead, start + hours]`). |
| **C4** | Scheduler signatures | `plan/03:14-20` — `place(db, req: PlacementRequest, now, cfg=SchedConfig())`, `next_free_at(db, usecase: str, now, cfg=SchedConfig())` | `plan/02:201-202` — `scheduler.next_free_at(db, candidate_setup_ids=[…], hours=1, hw_class=…, now=now)`; `plan/02:253-254` — `scheduler.place(db, usecase_row=uc, requested_start_at=…, hours=…, setup_name=…, now=now)` | Neither call matches the declared signature, and the `cfg` that carries the lead times and horizon is passed nowhere in `plan/02` — yet `plan/03:28` says `api.py` builds `SchedConfig` from `service/config.py`. Two implementers will write incompatible modules. |
| **C5** | Dashboard contract | `plan/06` — catalog table + "Reserve" button → modal with `datetime-local` + hours; slot conflicts shown as "slot shifted from …" (`plan/06:135-147`); Cancel via `DELETE` (`plan/06:185-186`); 12-endpoint map | `plan/09:471-476`, `plan/09:498` — "**Deploy now**" button on each card, a date-picker grid with busy slots greyed out, alternative-slot chips on a 409, "Cleanup" action while the card shows "cleaning" | Two different UIs. `plan/06` has no availability endpoint, no `mode` field, no cleanup button, no SSE/progress. |
| **C6** | Env-var surface | `plan/08:127-128` — "`.env.example` lists **every** setting with its default"; table at `plan/08:132-151` (18 vars) | `plan/09:282` (`DAOLAB_IP_POOL`), `plan/09:322` (`DAOLAB_EARLY_ACCESS`), `plan/09:369` (`DAOLAB_HEALTH_STALE_SEC`), `plan/10:61` (also `DAOLAB_RUNNER`, `DAOLAB_SLOT_MIN`, `DAOLAB_CLEANUP_MIN`, `DAOLAB_DEFAULT_HOURS`, `DAOLAB_OTEL_ENDPOINT`, quota caps) | `plan/08` is the owned doc for the config surface; a doc-consistency rule (`plan/README.md:185-188`) requires updating it in the same change. |
| **C7** | DDL `CHECK` vs new states | `plan/01:221-222` — `CHECK (state IN ('queued','provisioning','ready','active','tearing_down','released','failed'))` | `plan/09:434-438`, `plan/10:87` (B-21, files `models.py`, `api.py`) | With `schema.sql` unchanged, `INSERT`/`UPDATE` into `cleaning` or `quarantined` fails with a CHECK violation (SQLite enforces CHECK constraints regardless of `foreign_keys`). B-21 does not list `schema.sql`; `plan/08:253-259` also says there is no migration framework, so a CHECK change needs a hand-applied table rebuild. |
| **C8** | Route table | `plan/02:22-23` — registered routes are `/health /whoami /usecases /pools /setups /reservations (+/{id}) /internal/vm-ip` | `plan/02:118-130` defines `GET /users` and `PATCH /users/{id}`; `plan/06:249-250`, `plan/06:277-278` call them | The structure comment is the file-layout spec an implementer copies; two documented routes are missing from it. |
| **C9** | Schema size | `plan/README.md:117` — "`service/schema.sql` DDL (**7 tables**)"; `plan/10:12` — "schema.sql (7 tables)" | `plan/09:456-458` adds `jobs`, `ip_pools`, `ip_leases` (+ audit/availability); `plan/10:86` (B-20) says files `schema.sql`, `models.py` | The doc-map summary of the schema goes stale the moment B-20 lands; `plan/08:256` ("only `users`, `reservations`, `vms` carry state") is also no longer true. |
| **C10** | Manual-run path | `plan/05:144-157` — manual runs use `reservation_id=0` and the callback tasks are skipped by `when: reservation_id | int != 0` | `plan/09:262`, `plan/09:268` — `deploy.yml` leases an IP from the API **and** posts `POST /internal/vm-ip` for every role | Under `plan/09` the manual path needs a live API and a lease, but no `reservation_id=0` bypass is specified — the Phase-1 "run every playbook by hand" workflow (`README.md:145-149`) breaks. |
| **C11** | IP address model | `plan/07:52-54` — management IP over DHCP, data IPs static 10.20.0.x / 10.21.0.x on two private segments | `plan/09:54` — one management pool `10.30.0.100–199`; `plan/09:292` — IPs leased and set via cloud-init | Three address plans; the `vms.ip` semantics in `plan/02:275-283` ("posted by `POST /internal/vm-ip`") only covers one of them. |
| **C12** | `resources.hw_class` ownership | `plan/01:345-347` — "Sync **never** writes `resources.hw_class`"; `plan/04:206`, `plan/05:295`, `plan/07:301-303` — the engine owns it | `plan/09:266` — "set hw_class if different (EBF)" as a playbook step; `plan/08:356` — manual `UPDATE resources SET hw_class=…` | If the playbook writes it, the "engine owns it" rule and the `plan/03:115` lead-time re-derivation (which compares against the current value) race with the playbook. |
| **C13** | Teardown "never touches" the DPU | `plan/05:254-258`, `plan/07:294-299` — teardown stops the app, removes certs, keeps EBF/`hw_class`/firmware | `plan/09:390-401` — cleanup = teardown → verify → reconcile → quarantine, and reconcile "destroy leftovers directly (xe vm-uninstall, stop DPU app, unbind VFs)" | Teardown in `plan/09` also **unbinds VFs** — an action `plan/05` explicitly forbids ("**Never** touches EBF, `hw_class`, or pciback"). Two different teardown contracts. |
| **C14** | "One click" vs the phase model | `README.md:153-168` — Phase 2 = scheduling, Phase 3 = automation loop, Phase 4 = harden; `plan/10:7-8` — "Every task up to P5 can be built and demonstrated with `DAOLAB_RUNNER=mock`" | `plan/09:496-503` — P1…P6 are a *different* phase list (booking, jobs, IPAM, access, health, real playbooks) | Two incompatible phase ladders with the same labels; `plan/10`'s P0–P6 map onto `plan/09`'s P1–P6 but not onto the README's Phase 0–4. Nobody can tell which phase gate a task belongs to. |
| **C15** | `plan/10` ownership vs task IDs | `plan/10:25-26` — Akshay 25, Rama 36 | `plan/10` — 61 task rows (IDs sparse, see B11) | The counts add up (25 + 36 = 61), but the IDs run to B-91 with 30 gaps, so "B-01…B-91" and per-owner totals cannot both be used for tracking. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| C1 | FIXED | `plan/05:298-299`; `plan/07:154-155`; `roles/host_vm/defaults/main.yml:22-25` | `http-server: [apache2]` everywhere. |
| C2 | PARTIAL | `plan/02:195` (held_by incl. `cleaning`, `quarantined`, plus `dirty`); `service/models.py:16`; `dashboard/admin.html:268-271` | Contract and code agree; `plan/06:244-245` and `plan/08:487` still stale. |
| C3 | FIXED | `plan/03:152,269`; `service/scheduler.py:9,183` | One half-open rule `[start−lead, end+cleanup)`. |
| C4 | FIXED | `plan/03:14-22`; `service/scheduler.py:308,346`; `service/api.py:473,721-724` | |
| C5 | PARTIAL | `plan/06:127,136,186,272,294-297` vs `plan/09:90,157-158,194-195`; `dashboard/user.html` | The built UI is `plan/06`'s modal labelled "Deploy" plus `/cleanup`; no availability picker, `mode`, chips or SSE. Two docs, two UIs. |
| C6 | FIXED | `plan/08:148-186` | Every one of the 40 settings is in the table. |
| C7 | FIXED | `service/migrations.py:167-195`; `plan/01:112,170`; `plan/08:288-297` | |
| C8 | FIXED | `plan/02:30-33,126-127` | |
| C9 | OPEN | `plan/README.md:117`; `plan/01:13` ("Seven tables"); `plan/08:294` | Migrations add `ip_pools`, `ip_leases`, `audit_events`, `jobs`, `health_snapshots`; only `jobs` is documented. |
| C10 | FIXED | `plan/05:132-135,577-583`; `playbooks/deploy.yml:22,84`; `roles/lease_ip/tasks/main.yml:5-14` | `reservation_id=0` + `manual_ips` skip every API call. |
| C11 | PARTIAL | `plan/05:92-93,106`; `plan/09:54,295-297` | `plan/07:48-54,153` still DHCP; `plan/09:277` still 10.30. |
| C12 | PARTIAL | `service/engine.py:274-275` (sole writer of `resources.hw_class`); `roles/ebf_set` (reads only) | Race gone; `plan/08:483` still says `UPDATE resources SET hw_class`; the board marker is a second copy (H10). |
| C13 | FIXED | `plan/05:349-351,373-381`; `reconcile.yml:7-8,60-61` | One contract; `plan/09:411` still words it "unbind VFs". |
| C14 | OPEN | `README.md:157-208`; `plan/10:65-142`; `plan/09:530-535` | Two ladders, no mapping. |
| C15 | OPEN | `plan/10:33-34`; `plan/README.md:126` | Unchanged. |

---

## D. Unverified claims ("don't know" — not the same as wrong)

Nothing in this section is asserted to be false. Each item is a claim the plan depends on that could
not be confirmed from public sources and must be checked on the board before Phase 1.

| ID | Location | Claim | Why it is unverified / how to settle it |
|---|---|---|---|
| **D1** | `plan/07:93-95`, `plan/07:113`, `plan/05:183-186` | OpenSSL engine id `dpdk_engine`, `ssl_engine dpdk_engine;`, install prefix `/usr/local/dao/nginx` | The Marvell DAO docs describe an OpenSSL engine and NGINX TLS offload through CPT, but the exact engine name and install path are build-specific. Check the DAO repo/build guide for `ssl_engine` and the engine .so name. |
| **D2** | `plan/05:276-290`, `plan/07:70-75`, `plan/08:356` | "EBF" = OCTEON early-boot firmware; `playbooks/roles/ebf_set` flashes a personality and reboots the board | The word "EBF" and a flash-and-reboot procedure for it could not be found in the public Marvell/DAO material. Confirm the real board-configuration mechanism before writing the role. |
| **D3** | `plan/05:279`, `plan/07:70`, `plan/09:170` | `sdp-pf1-vf8` = "SDP, 1 PF, 8 VFs" | SDP is real (see E3) and "1 PF + N VFs" is the right shape, but the exact personality string and the VF count are Marvell-internal. |
| **D4** | `plan/05:180`, `plan/07:78-89`, `plan/05:358` | "CPT VFs = `nproc`", one CPT VF per NGINX worker (`worker_processes auto`) | The CN10k CPT VF limit is not stated anywhere in the plan and `nproc` on the board is unknown. If the board has 16 cores, the plan asks for 16 CPT VFs. Verify the CPT PF's max `sriov_numvfs` before running `dpu_prepare.yml` — this is a Phase-1 blocker on the board. |
| **D5** | `plan/09:364` | `httpcheck` receiver probing `https://<proxy_ip>/` | The receiver needs TLS config (CA/insecure) for a self-signed certificate (`plan/07:96-104`). No TLS options appear in the collector design (`plan/09:342`). |
| **D6** | `plan/07:171` | The guest sees an `iavf` netdev for the E810 VF | Plausible (`iavf`, the successor of `i40evf`, is the E810's VF driver) but not verified on XCP-ng. |
| **D7** | `plan/05:181`, `plan/07:87` | `dpdk-devbind.py -b vfio-pci <bdf>` available on the OCTEON | The script ships with DPDK; its presence on the board image is assumed. |
| **D8** | `plan/09:54`, `plan/09:282` | Management pool `10.30.0.100–199`; `DAOLAB_IP_POOL` | Arbitrary; must be reconciled with the actual lab addressing (see F1: the lab runs 10.68.0.0/24). |
| **D9** | `plan/09:497-503` | P6 demo: "real VPP datapath, health green" | Depends on D4, B4, D5 and on VPP being built for the board. |
| **D10** | `plan/05:361`, `plan/05:360-369` | "the scheduler holds a setup exclusively for a reservation's whole window, so at most one reservation ever owns a setup's VFs" | True only if no two reservations can be in the same window — see C3 (busy-window boundary) and `plan/03:215` (slot-shift can move a grant to a window that overlaps another's *lead*). Verify the VF-ownership invariant against the scheduler's real busy rule. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| D1 | OPEN | `plan/07:93,113`; `playbooks/apps/nginx-tls/deploy.yml:10` | No NGINX build runs; the engine id is unconfirmed (the board has `openssl-engine-1.0.0-cn10k`). |
| D2 | OPEN | `roles/ebf_set/tasks/main.yml:1-18` | Flash / reboot still unknown; a missing marker is assumed to match. |
| D3 | OPEN | `inventory/host_vars/dpu-01.yml:10`; `playbooks/bringup.yml:40-46` | Only a label now; the board log in `dao-smartnic/` lists nine SDP VFs, so "vf8" is unconfirmed. |
| D4 | OPEN | `plan/07:78-89`; `README.md:180` | No playbook creates CPT VFs. |
| D5 | PARTIAL | `roles/otel_agent/templates/otelcol.yaml.j2:33-40` (`insecure_skip_verify`) | The TLS option exists; no httpcheck target is ever configured (B2). |
| D6 | OPEN | `roles/host_vm/defaults/main.yml:33` (`e810-vf: iavf`) | Relied on, never run on hardware. |
| D7 | SETTLED | `dao-smartnic/` board log (`dpdk-devbind.py -b vfio-pci …`); `playbooks/bringup.yml:31` | Present and used on xeon3-oct-112. |
| D8 | PARTIAL | `service/config.py:84`; `.env.example:37`; `inventory/group_vars/all.yml:6-8` | Pool 10.68.1.0–10.68.2.200, inside the lab's /22; the VMs' prefix, gateway and DNS are still TODO; `plan/09:277` still 10.30. |
| D9 | PARTIAL | `playbooks/apps/dao-vpp`; `playbooks/files/daolab-app-exporter` | The VPP datapath on the DPU runs from the dashboard (ready in 55–73 s, 2026-10-16); "health green" needs the telemetry stack and the VMs. |
| D10 | SETTLED (true) | `service/scheduler.py:179-215,364-373`; `service/engine.py:203,211` | VF ownership holds by construction. |

---

## E. Verified correct — do not "fix" these

| ID | Location | Claim | Verification |
|---|---|---|---|
| **E1** | `plan/09:270`, `plan/10:126-127` (B-62 / B-63) | `community.general.opentelemetry` Ansible callback emits one span per task; honours `TRACEPARENT` and `OTEL_EXPORTER_OTLP_ENDPOINT` | Real callback in `community.general`; docs at https://docs.ansible.com/projects/ansible/12/collections/community/general/opentelemetry_callback.html |
| **E2** | `plan/09:405` | `xe vm-list tags:contains=<value>` | Valid XCP-ng CLI filter syntax (the *use* of it is the bug — see B14). |
| **E3** | `plan/README.md:167`, `plan/05:354-358` | SDP = the OCTEON's PCIe packet endpoint, 1 PF + multiple VFs presented to the XCP-ng host | Confirmed (Marvell OCTEON TX2 / DAO material). |
| **E4** | `plan/07:76`, `plan/07:241-242` | `cpt-firmware-cn10k` package; CPT counters readable under `/sys/kernel/debug/cn10k/cpt/` | Both real (CN10k SDK kernel tree; Marvell firmware releases). |
| **E5** | `plan/07:28`, `plan/07:92`, `README.md:84-86` | Marvell DAO ships an OpenSSL engine + NGINX build with RSA/ECDSA offload to CPT | Real (DAO accelerated-NGINX guide): https://marvellembeddedprocessors.github.io/dao/guides/dao-devel/white_papers/accelerated-nginx.html |
| **E6** | `plan/05:356`, `plan/09:46-48` | Intel E810 supports SR-IOV with up to 256 VFs total, split evenly across ports | Confirmed by the Intel E810 SR-IOV config guide. |
| **E7** | `plan/07:174-175` | `ab` → `apache2-utils`, `h2load` → `nghttp2-client` | Correct Debian/Ubuntu package names. |
| **E8** | `plan/01:236` | `reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE` | Correct SQLite syntax (only the prose around it is wrong — B5). |
| **E9** | `plan/05:345`, `plan/05:347` | `xe vm-param-set uuid=<vm> other-config:pci=0/<bdf>`; `xe vm-param-get … param-name=networks param-key=0/ip` | Correct XCP-ng PCI-passthrough and guest-IP commands. |
| **E10** | `plan/05:336-348` | `xl pci-assignable-list`, `xe sr-list`, `xe network-list`, `xe vif-create`, `xe vm-clone`, `xe vm-uninstall force=true` | All real XCP-ng/Xen commands with the right semantics. |
| **E11** | `plan/05:181-182`, `plan/07:86-88` | `dpdk-devbind.py -b vfio-pci <bdf>` | Correct DPDK bind syntax (see D7 for presence on the board). |
| **E12** | `plan/08:245-252` | Online sqlite backup via `sqlite3 db ".backup 'file'"` while WAL + two processes are live; restore by stopping units and removing `-wal`/`-shm` | Correct sqlite/WAL procedure. |
| **E13** | `plan/08:198-223` | `OnCalendar=*-*-* *:*:00` timer driving a `Type=oneshot` service | Correct systemd pattern for a per-minute tick without overlap. |
| **E14** | `plan/08:241-244` | `PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON` per connection; WAL needs a local filesystem | Correct sqlite behaviour. |
| **E15** | `plan/08:126-127`, `plan/08:281-287` | `python-dotenv` — real env vars win over `.env`; `hmac.compare_digest` for the internal secret | Correct library behaviour and correct constant-time compare. |
| **E16** | `plan/08:66-68` | `secrets.token_urlsafe(32)`, store only `sha256(token).hexdigest()` | Correct one-way token handling. |
| **E17** | `plan/10:25-26` | Ownership split 25 + 36 = 61 tasks | Matches the 61 task rows in `plan/10`. |
| **E18** | `plan/07:239-240` | `h2load -n1000 -c100` opens 100 connections → 100 TLS handshakes | Correct h2load semantics. |
| **E19** | `plan/09:283-287` | Lease idempotency by `(reservation_id, role)`; uniqueness on `(pool, ip)` among live leases | Sound IPAM rule; matches `plan/09:458`. |
| **E20** | `plan/03:9`, `plan/02:250` | `BEGIN IMMEDIATE` around place-then-insert makes placement race-free on sqlite/WAL with a single API worker | Correct sqlite transaction pattern. |

**Status on `rama`:** E1–E18 and E20 still `OK` (E1 is implemented: `service/runner.py:631-658`; E4/E11 are confirmed on xeon3-oct-112). E6 is background, not a doc claim (O). **E19 `CHANGED`** — still sound, refined: `(reservation, role)` is unique only among `leased` rows (`service/migrations.py:198-208`, m7) so a quarantined address can be replaced; `plan/09` is not updated.

---

## F. Hardware reality (what the lab actually is)

| ID | Fact | Consequence |
|---|---|---|
| **F1** | The lab **is** the two-host topology: `tg-01-xcp` = `xcp-ng-fxbqndvl` @10.68.0.52 (E810 PF `0000:01:00.1`, 7 VFs); `dpu-01-xcp` @10.68.0.53 (SDP PF `0000:6f:00.0`); board `dpu-01` @10.68.0.190. | `README.md` + `plan/01–08` + `inventory/hosts.yml` match the lab. `plan/09`'s `tb-01`/`tb-01-xcp` (A1) does not exist. A7/B-85/B-86 target hardware that is not there. |
| **F2** | **SDP VFs cannot be created on `dpu-01-xcp`**: no SR-IOV PF `177d:b900`, `sriov_numvfs` is not writable, `sdp_vfs` stays empty. | `plan/05:382-383` ("one-time dom0 prep … SDP VFs on dpu-01-xcp … reboot dom0 once") assumes a PF with VF support that is not there. The `connect: sdp` role (`plan/07:145-150`) cannot be provisioned on today's board. Plan/10's B-85/B-86 are not closable without a board/SDP fix. |
| **F3** | `tg-01-xcp` has 7 E810 VFs, created on the PF. | Enough for the current two-VM client+backend pair (`plan/07:168-171`), so the TLS-proxy use case is implementable on the TG side; the blocker is the SDP side (F2). |
| **F4** | Lab addressing is `10.68.0.0/22` (the board's management interface is `10.68.0.190/22`; this audit first said /24); `plan/09` assumes `10.30.0.100–199` for the lease pool and `plan/07:52-54` uses `10.20.0.x`/`10.21.0.x` for data segments. | Any IP plan has to be reconciled with the real lab network (D8). |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| F1 | **OPEN** | `inventory/hosts.yml:15-16`; `setups/tb-01.yml:6-7` | The repo committed to one placeholder host; the two real hosts are what Xen Orchestra (`scripts/run/xo.sh`) will manage. Inventory and setups must describe them before VMs run. |
| F2 | OPEN | `plan/05:446-449`; `inventory/host_vars/tb-01-xcp.yml:11` | Avoided, not solved: deploys are DPU-only. |
| F3 | OPEN | `inventory/host_vars/tb-01-xcp.yml:10` (`e810_vfs: []`) | The 7 VFs on PF `0000:01:00.1` are not recorded. |
| F4 | PARTIAL | `plan/09:54,295`; `inventory/hosts.yml:20` | 10.30 gone except `plan/09:277`; the pool sits inside the lab's 10.68.0.0/22; `group_vars` prefix still a /16 TODO. |

---

## G. Recommended order of work

**Step 1 — fix what is wrong regardless of the design decision** (no product call needed):
B1 (`200: 1000` → `1000 succeeded`), B2/B3/B4 (OTel rule names), B5 (FK prose), B6 (`Settings` fields),
B7 (`RunResult.ok`), B8 (endpoint count), B9 (bind default), B10/B11/B12 (counts), B13 (contract refs),
B14 (VM tags), B15 (QSFP / Y cable) — 15 edits, each one line plus its ripple in `plan/01`, `plan/04`, `plan/06`, `plan/07`, `plan/08`, `plan/09`.

**Step 2 — take the five design decisions** (a human call; the audit cannot make it):

| # | Decision | Option A (README + plan/01–08) | Option B (plan/09 + plan/10) | Note |
|---|---|---|---|---|
| D-1 | Topology | two XCP-ng hosts (`setup-01`) | one host (`tb-01`) | **The lab is Option A** (F1). Option B needs hardware that does not exist (F1, F2). |
| D-2 | Booking | always slot-shift | `mode: at` → 409 | Option B is a superset of A; adopting it means adding `mode` + `slot_conflict` without removing shift for `asap` (`plan/09:512-514`). |
| D-3 | States | `tearing_down` | `cleaning` + `quarantined` | Option B needs the DDL `CHECK` updated (C7) and a migration story (`plan/08:253-259`). |
| D-4 | Cancel | `DELETE /reservations/{id}` | `POST …/cleanup` (+`redeploy`) | Option B also implies the job model (C5, C14). |
| D-5 | Playbooks | four | `deploy.yml` + `cleanup.yml` | Option B must specify the role mapping, the `reservation_id=0` manual path (C10) and the teardown scope (C13). |

**Step 3 — fix the tasksheet and the doc map:**
- `plan/10`: B-90's scope must widen from `plan/02–05` to `plan/01`, `plan/06`, `plan/07`, `plan/08`, `plan/09`, `plan/10`, `plan/README.md` and the root `README.md` (A8); add tasks for the DDL `CHECK` change (C7), the `tags:` on VMs (B14), the `verify.expect` string (B1) and the health-rule names (B2-B4); fix the "B-01…B-91" claim (B11) and the phase ladder (C14).
- `plan/README.md`: document count (B10), task-ID range (B11), "four playbooks" (`plan/README.md:51`, `78`, `107`) once D-5 lands, the 7-table schema line (C9), the glossary entries for the new states/jobs/leases.

**Step 4 — only then start Phase 1 hardware truth**, and settle D4 (`CPT VF count = nproc`),
D2 (`ebf_set` mechanism) and F2 (SDP VFs) first — they are the three items most likely to stop the
bring-up on the board.

**Status on `rama`:** Step 1 — B3, B4, B6, B7 fixed; B13, B14 partly; B1, B2, B5, B8, B10, B11,
B15 open; B9 and B12 were not errors. Step 2 — decided as built: D-1 one host `tb-01` (against
F1; revisit for the VMs), D-2 Option B as a superset of A, D-3 Option B (migration 6), D-4 both
endpoints kept, D-5 Option B (`plan/05` maps the roles). Step 3 — B-90 still has the narrow scope
(A8). Step 4 — the DPU half is done on xeon3-oct-112; D2, D4 and F2 are still open.

---

---

# Part II — Design-level defects (the whole system)

Part I says *which lines are wrong*. Part II says *whether the design works* — where the same
rule is implemented twice, where two mechanisms fight each other, where state can silently drift,
what is missing, and in what order the plan intends to build it.

Scoring used below:

| Severity | Meaning |
|---|---|
| **S1** | The system cannot be correct as designed (a real failure, a lost lab, or a wrong answer to a user). |
| **S2** | The system works but produces wrong or surprising behaviour under normal use. |
| **S3** | Design debt / maintainability; will cost time later but is survivable. |

---

## H. Overlaps — one rule implemented in two or more places

Each of these is a place where the *same fact* has two owners. Two owners means one of them will
be updated and the other forgotten.

| ID | Overlap | Where | Severity | Consequence |
|---|---|---|---|---|
| **H1** | The free-slot / busy-window rule | The catalog path `plan/02:195-205`, the reserve path `plan/02:250-266`, and the definition `plan/03:115` | S2 | `plan/02:210-212` explicitly promises "delegated entirely to `scheduler.next_free_at` so catalog and `POST /reservations` can never disagree" — and then re-implements the window and the shift in prose on the same page. The invariant is stated and broken 50 lines later. |
| **H2** | The hw_class lead-time rule | Scheduler `plan/03:115`, engine `plan/04` (lead re-derived per tick), playbook re-check `plan/05:174-178` | S2 | Three implementations of "how much lead does this booking need". The playbook only *checks*, the other two *decide*. |
| **H3** | The capability union | Computed at sync time and stored (`plan/01:345-347`, `plan/README.md:117`) and re-checked at booking time (`plan/03`) | S3 | Two owners of the same derived fact; `daolab sync` must have run for the scheduler to see a new setup, which `plan/08:355` already treats as a support issue. |
| **H4** | "Is this use case available?" | API `available`/`reason`/`next_free_at` (`plan/02:195-205`), the frontend badge (`plan/06:129-133`), and `plan/09:158`'s new `/availability` endpoint | S2 | 09 adds a third answer to the same question; the dashboard must then merge two sources of truth for the same cell. |
| **H5** | IP assignment | DHCP (`plan/07:52`), guest-tools discovery + callback (`plan/02:275-283`, `plan/05:347`), in-guest netplan static IPs (`plan/07:151-153`), and 09's backend lease via cloud-init (`plan/09:280-293`) | S1 | Four mechanisms for one job. In the merged design all four are live at once: DHCP gives the management address, netplan pins the data address, the callback reports it, and the lease is supposed to own it. There is no stated authority — which one wins on a conflict? |
| **H6** | "What does this reservation own?" | The `vms` DB table (`plan/01:234-244`), teardown's name-label grep (`plan/05:262-266`), 09's `tags:` query (`plan/09:405`), and the physical XCP-ng VM list | S1 | The API can only see the DB copy; the lab can only see the real VMs. Nothing reconciles them (see J1). 09's residue check queries a tag nobody sets (B14), so its "verify" step is decorative. |
| **H7** | Teardown / cleanup | Playbook `teardown.yml` (`plan/05:254-274`), engine transition (`plan/04:164`), 09's cleanup job + reconciler (`plan/09:390-401`) | S1 | Three teardown paths. `plan/05` forbids touching EBF/hw_class/pciback; 09's reconcile unbinds VFs (C13/I16). If both exist, an engine tick and a cleanup job can tear down the same reservation concurrently. |
| **H8** | The reservation state machine | API cancel (`plan/02:178-182`), engine tick (`plan/04:60-70`), model helpers, plus the two written copies (`plan/04:20-30`, `plan/06:175-183`, `plan/09:428-438`) | S1 | Three writers of `reservations.state` and three written copies of the transition table. The three copies already disagree (A3). A CAS-style transition helper is only added by 09's B-21 — i.e. 01–08 has no guard against a lost update. |
| **H9** | Observability | journald + ansible-runner event stream (`plan/08:234-237`) vs OTel + Prometheus + Jaeger + SSE (`plan/09:327-383`) | S2 | 09 adds a parallel telemetry stack for the same events; two ways to answer "what happened to my reservation". |
| **H10** | The DPU's current personality | Written by the engine (`plan/04:206`), read by the scheduler (`plan/03:115`), re-checked by the playbook (`plan/05:174-178`), written by the playbook in 09 (`plan/09:266`) | S1 | One mutable global with four parties and no lock or version. The scheduler's answer to "is this slot busy" depends on it (see J7). |
| **H11** | User lifecycle | Creation in the CLI only (`plan/06:301-303`), enable/disable in the dashboard (`plan/06:249-254`), deletion as raw SQL (`plan/08:270-274`) | S3 | One entity, two tools, and a destructive path with no product surface. |
| **H12** | The doc-ownership map itself | `plan/README.md:114-126` vs the folder rule at `plan/README.md:9`, `plan/README.md:112` | S3 | The table everything routes through says "nine documents" and lists eleven. The map is stale on arrival, so "update the owning doc" has no reliable target. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| H1 | FIXED | `service/api.py:473` → `scheduler.py:346-349`; `api.py:721-725` | Catalog and booking both call `place()`. |
| H2 | AUDIT-WRONG | `service/scheduler.py:141-153`; `engine.py:206` | One lead helper, two callers (O). |
| H3 | AUDIT-WRONG | `service/catalog.py:403-411`; `scheduler.py:235-240` | The union has one owner (O); what is duplicated is the `requires ⊆ caps` / `missing` logic (`api.py:463-471,548-553` vs `scheduler.py:235-240,322-330`). |
| H4 | PARTIAL | `service/api.py:435-480,529-565` | No client merges two sources; capability logic is copied in `api.py`, and `busy_windows` re-implements the timeline projection. |
| H5 | PARTIAL | `service/ipam.py:78`; `api.py:1050-1064`; `roles/lease_ip`; `network-config.j2` | One authority in code (lease → cloud-init); `plan/07` still DHCP / netplan; `vms.ip` duplicates `ip_leases`. |
| H6 | PARTIAL | `cleanup.yml:45-46`, `verify_clean.yml:33-34`, `reconcile.yml:40-41`, `orphans.yml:20-21`; orphan sweep `engine.py:469-502` | One name rule + orphan sweep; the VM plan is derived twice (`models.py:212-243` vs `tasks/plan_vms.yml:41-58`). |
| H7 | FIXED | `service/engine.py:321-350,375-393`; `models.py:639-671` | One teardown path; `plan/07:291-297`, `plan/08:487` still name `teardown.yml`. |
| H8 | FIXED | `service/models.py:623-671` (transition table + CAS, used by every writer) | `plan/08:487` still prescribes a raw `UPDATE reservations` (bypasses the CAS). |
| H9 | FIXED | `service/models.py:591`; `api.py:981-998`; `engine.py:421-444` | Layered: job steps in the DB are the record; OTel mirrors them. |
| H10 | PARTIAL | `service/engine.py:273-275`; `playbooks/bringup.yml:40-49`; `roles/ebf_set` | Two unreconciled copies: `resources.hw_class` and `/etc/daolab/hw_class`; `plan/08:478` vs `:483` disagree. |
| H11 | PARTIAL | `service/cli.py:72-92` (`--rotate` in place); `api.py:574-587` | Rotation needs no delete and is documented (`plan/08:307-310`); still no delete surface. |
| H12 | OPEN | `plan/README.md:9,112` vs `:114-126` | Unchanged. |

---

## I. Clashes — two mechanisms that cannot both be in the flow

| ID | Clash | Side A | Side B | Severity | Consequence |
|---|---|---|---|---|---|
| **I1** | Booking conflict | Always slot-shift (`README.md:4`, `plan/03:25`) | `mode: at` → 409 (`plan/09:186-188`) | S1 | A4. Also forces two frontend error paths. |
| **I2** | Cancel | `DELETE /reservations/{id}` (`plan/02:178-182`) | `POST …/cleanup` (`plan/09:162`) | S1 | A5. |
| **I3** | State names | `tearing_down` (`plan/04:13-16`) | `cleaning` + `quarantined` (`plan/09:434-438`) | S1 | A3/C7 — and the DDL CHECK rejects the new names. |
| **I4** | Playbook granularity | Four phase-scoped playbooks (`plan/05:166-276`) | One `deploy.yml` with roles (`plan/09:275-278`) | S1 | A2. The mapping from phase to role is never specified, and idempotency is per-phase in one design and per-role in the other. |
| **I5** | IP model | Discovered + static-in-guest (`plan/07:151-153`) | Leased + cloud-init (`plan/09:292-293`) | S1 | A6/H5. |
| **I6** | `held_by` | `plan/02:132-133` (excludes `failed`) | `plan/04:44`, `plan/05:273`, `plan/08:360` (failed teardown holds) | S2 | C2. The dashboard loses the hold exactly when it matters. |
| **I7** | Busy-window boundary | Closed `[start−lead, end_at]` (`plan/02:212`) | Half-open `[start−lead, end_at)` (`plan/03:115`) | S1 | C3. One of the two allows a double-book at the boundary. |
| **I8** | Scheduler interface | `place(db, req, now, cfg)` (`plan/03:14-20`) | `place(db, usecase_row=…, hours=…, setup_name=…)` (`plan/02:253-254`) | S1 | C4. Two implementers, two modules. |
| **I9** | The functional gate is a substring match on stdout | `plan/01:288-290` defines `expect` as "a substring that must appear in stdout" | `plan/05:248` implements `assert: expect_string in out.stdout` | S1 | The contract cannot express "1000 requests, all 2xx". Any numeric expectation can be satisfied by the wrong token — a duration, a byte count, an IP fragment, a `1000` inside `10001`. The plan's only quality gate is structurally too weak, not just mis-spelled (B1). |
| **I10** | `verify` is asked to be two different things | Phase-1 manual checkpoint with a human reading counters (`plan/07:234-245`) | The engine's pass/fail gate (`plan/05:250-252`) | S1 | One h2load invocation cannot establish "the datapath works". The manual path adds criteria the automated path does not check (CPT counters moved, plain-HTTP in the backend log) — so the automated gate is strictly weaker than the manual one. |
| **I11** | Who writes `resources.hw_class` | The engine (`plan/04:206`, `plan/01:345-347`) | The playbook (`plan/09:266`) | S2 | C12. The scheduler re-derives leads from this value; if a playbook writes it mid-flight, previously granted slots change shape. |
| **I12** | The Postgres trigger | "move to Postgres only if >1 API worker" (`plan/08:260-262`) | Race-freedom assumes **one** API worker: "one API worker on sqlite/WAL, so place-then-insert is race-free" (`plan/03:9`, `plan/02:356`) | S1 | The scale trigger invalidates the concurrency assumption. The rule should be "before you permit a second API worker, add a different locking story" — as written, hitting the trigger silently breaks placement. |
| **I13** | Availability duration | `next_free_at` is computed for a **1-hour** slot (`plan/02:210`, `plan/08:83`) | Bookings may be `--hours 4` (`plan/08:80`) | S2 | The catalog can say "available" for a 4-hour request that will actually be slot-shifted. Availability and booking are computed for different durations. |
| **I14** | Lead time vs near-term booking | The engine provisions at `start_at − lead` (5 or 15 min, `plan/08:145-146`) | The API accepts any `start_at` (`plan/02:236`) | S1 | There is no "too late to book" rule. A slot starting in 2 minutes with a 5-minute lead is created, accepted, and then either provisions instantly with no margin or is skipped, depending on the tick's `>=` semantics. The user is told it succeeded. |
| **I15** | Manual-run path vs leasing | `reservation_id=0` sentinel + `when: reservation_id | int != 0` skips the callback (`plan/05:144-157`) | `deploy.yml` leases an IP from the API and posts an IP per role (`plan/09:262-268`) | S2 | C10. The "run every playbook by hand once" workflow that Phase 1 depends on (`README.md:145-149`) needs a live API under 09. |
| **I16** | Teardown scope | "**Never** touches EBF, `hw_class`, or pciback" (`plan/05:254-258`) | Reconcile "destroy leftovers directly (xe vm-uninstall, stop DPU app, **unbind VFs**)" (`plan/09:397`) | S2 | C13. Unbinding the CPT VFs contradicts `plan/07:297` ("re-asserted by the next `dpu_prepare.yml`") — the two designs disagree about who re-binds what. |
| **I17** | Scope of the plan folder | "No invented scope: nothing here adds product capabilities, services, or technologies beyond the spec" (`plan/README.md:21`) | 09 adds OTel SDK, Collector, Prometheus, Jaeger, SSE, jobs, IPAM, quotas, `AuthorizedKeysCommand` (`plan/09:302-323`, `plan/09:327-383`, `plan/09:442-458`) | S2 | `plan/09:525-527` says "This is the only new technology" — while adding five. The rule as written forbids exactly what 09 does. |
| **I18** | Who owns the slot after a failed deploy | "the slot stays the user's for a manual `redeploy`" (`plan/09:296-298`) | The same failure quarantines the setup (`plan/09:398`) | S2 | The slot is simultaneously "the user's to redeploy" and "blocked for everybody". The user's `redeploy` would be refused by its own quarantine. |
| **I19** | Authority for the rules | "contract §8/§9/§12" (`plan/03:5`, `plan/05:141`) | No such document exists (B13) | S2 | The most load-bearing rules (extravars, state machine, lead time) cite a spec that is not in the repo, so they cannot be checked. |
| **I20** | Phase ladders | README Phase 0–4 (`README.md:126-175`) | `plan/10` P0–P6 / `plan/09` P1–P6 (`plan/10:82`, `plan/09:496-503`) | S3 | C14. Same labels, different gates; "don't start phase N+1 until phase N passes" (`plan/README.md:148`) is uncheckable. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| I1 | PARTIAL | `service/scheduler.py:3-7,332-343`; `api.py:739-750` | `asap` default, `at` opt-in; no client sends `at`; `plan/README.md:26-28,181` stale. |
| I2 | FIXED | `service/api.py:797-826,829-856` | Both kept, same live-slot effect. |
| I3 | FIXED | `service/models.py:13-17`; `migrations.py:167-196` | |
| I4 | PARTIAL | `playbooks/deploy.yml:23-103`; `plan/05:161-172` | Four playbooks remain in `plan/07`, `plan/01`, `plan/README.md`, `README.md`, `plan/08`. |
| I5 | PARTIAL | `network-config.j2:4-7`; `plan/05:100-106` | `plan/07` keeps the old model. |
| I6 | PARTIAL | `service/models.py:16,171-177,359-371`; `api.py:504-514` | A teardown failure is `quarantined`, which holds. New gap P1. |
| I7 | FIXED | `service/scheduler.py:9,99,183`; `plan/03:152` | |
| I8 | FIXED | `service/scheduler.py:308-309`; `api.py:721-725` | |
| I9 | **OPEN** | `roles/verify/tasks/run.yml:49-53` (rc ignored, substring) | Still a substring gate. |
| I10 | **OPEN** | `run.yml:51-53`; `playbooks/deploy.yml:96-103` | Worse on the real board: with VMs off, verify never runs (P3). |
| I11 | FIXED | `service/engine.py:273-275` (sole writer) | |
| I12 | PARTIAL | `service/api.py:706` and the other `BEGIN IMMEDIATE` sites | See O: sqlite already serialises writers across processes; the Postgres story exists only on paper and `plan/03:9` still ties race-freedom to one worker. |
| I13 | OPEN | `service/scheduler.py:346-349` (`hours=1`); `dashboard/user.html:154-155` | Right for one-click; other durations can still shift silently. |
| I14 | OPEN | `service/scheduler.py:223,258-261`; `engine.py:206-208` | No "too late to book" margin (the deploy just starts at once). |
| I15 | FIXED | `roles/lease_ip/tasks/main.yml:5-26` | `README.md:186-187` stale. |
| I16 | PARTIAL | `playbooks/reconcile.yml:55-67` | `plan/09:411`, `plan/07:291-297` stale. |
| I17 | OPEN | `plan/README.md:20-21`; `plan/09:557-559` | The scope rule still forbids what is built (OTel, IPAM, SSE, quotas, authz, now a Xen Orchestra container). |
| I18 | FIXED | `plan/09:309-312`; `service/api.py:871-874`; `engine.py:284-288,340-346` | |
| I19 | OPEN | `plan/03:5,144,266,330`; `plan/04:5-6` | Dangling "contract §" citations. |
| I20 | OPEN | `README.md:155-213`; `plan/README.md:146-157`; `plan/10:65-142` | Two ladders. |

---

## J. Reliability holes — state that drifts, and failures with no way back

| ID | Hole | Evidence | Severity | Consequence |
|---|---|---|---|---|
| **J1** | **No reconciliation between the database and the lab.** | The DB records intent; the lab records reality. `plan/01–08` never list XCP-ng VMs and compare them to the `vms` table. 09 adds a reconciler (`plan/09:102`, `plan/09:397`) — so this is a *missing component* in 01–08, not a doc inconsistency. | S1 | A crash between "VM cloned" and "IP posted" leaves a VM holding a VF with no DB row; the reservation is `failed`, and `failed` is **excluded from the busy rule** (`plan/03:115`, `plan/03:219`) → the setup looks free to the next user while the hardware is still occupied. This is the single most likely production failure in the design. |
| **J2** | **A failed teardown has no product-level release path.** | "an admin intervenes manually" (`plan/04:44`), and the documented fix is a hand-written `UPDATE reservations SET state='released'` (`plan/08:360`). | S1 | The hold is not modelled (no hold column, no quarantine state in 01–08) → the dashboard cannot act on it, the scheduler cannot reason about it, and the runbook depends on someone knowing SQL. |
| **J3** | **"Hardware truth" is collected once, by hand, and never re-verified.** | `README.md:133-144`, `plan/05:371-408` — BDFs typed into `inventory/host_vars/` by hand. | S1 | A dom0 reboot, a BIOS change or a firmware update silently invalidates every BDF. The failure mode is a retry storm (`DAOLAB_MAX_RETRIES=3`) that looks like a hardware fault. There is no "re-collect and compare" step anywhere. |
| **J4** | **Retries are whole-phase; there is no step checkpoint.** | `plan/04:164`, `plan/05:191-195`, `plan/08:148` — retry = run the whole phase again. | S1 | Safety rests entirely on every step being idempotent, which is *asserted* (`README.md:181`) and delegated to per-step prose ("check-then-act", `plan/05:191`) with no framework, no test, and no idempotency test in the test layout (`plan/08:314-321`). A crash mid-playbook loses the position and re-runs from the top. |
| **J5** | **The engine tick is the only clock, and a missed tick has no rule.** | `plan/08:163`, `plan/04:60-70` — one tick per minute; states advance only on a tick. | S2 | A suspended host, a crashed unit or a laptop that slept delays every reservation with no catch-up semantics ("tick missed by 10 minutes → what?"). The only timeout in the system is the retry counter (`DAOLAB_MAX_RETRIES`), which turns a *stall* into a *failure*. |
| **J6** | **Nothing watches a reservation after `ready`.** | The only functional check is the one-shot `verify` at provisioning time (`plan/05:236-252`). | S1 | After `ready`, a crashed app, a rebooted guest or a broken datapath is invisible until a user complains. The dashboard shows a green state for a dead setup. 09's health rules are the fix (`plan/09:327-383`). |
| **J7** | **The card's real personality can drift from the DB.** | The board's personality is the truth; `resources.hw_class` is a DB copy updated by the engine (`plan/04:206`) — and the plan tells humans to flash manually (`plan/07:266-267`, `plan/08:356`). | S1 | Nothing detects the drift. Since the scheduler derives the busy window from this value (`plan/03:115`), a stale `hw_class` makes every subsequent booking use the wrong lead — and, per A3, the wrong availability. |
| **J8** | **The board reflash has no rollback and an unverified duration.** | `plan/07:70-75` (flash + reboot), `plan/05:191` (no failure mode listed for a failed flash), `plan/08:145` (15-minute lead). | S1 | A failed flash leaves the only card in the lab unusable; the setup is down for everyone and the plan has no recovery procedure. The 15-minute lead is an assumption (D2/D4). |
| **J9** | **One setup is a single point of failure.** | "Today: exactly one setup" (`plan/README.md:32`); 09's quarantine has no automatic expiry (`plan/09:399` — release only by admin force-clean or a later successful reconcile). | S1 | Every reservation serialises onto one setup. A quarantined or wedged setup blocks the entire lab until a human acts. |
| **J10** | **The internal callback trusts the playbook.** | `POST /internal/vm-ip` (`plan/02:100`) upserts `vms.ip` for `(reservation_id, role)` behind one shared secret (`plan/08:281-287`). | S2 | Nothing checks that the IP belongs to the reservation's host, is inside the pool, or is not already used by another VM. The DB's IP uniqueness is therefore not enforced on this path (contrast 09's lease rule, `plan/09:287-288`). A wrong IP in the callback silently redirects the user's SSH line and the proxy's upstream. |
| **J11** | **The only quality gate produces no artifact.** | `plan/04:329-330` logs the assertion message; nothing stores the command's output, exit code or response. | S2 | When a reservation fails, the operator has a message quoting a substring, not the evidence. |
| **J12** | **No idempotency key on `POST /reservations`.** | The dashboard POSTs once (`plan/06:138`), the CLI POSTs once (`plan/08:83`). | S1 | A network timeout after the server commits leaves the client retrying → a second reservation for the same slot. Because of slot-shift the second one slides, so the failure is *silent*, but the user holds two bookings and one of them will provision on hardware the other is using. |
| **J13** | **Tokens never expire and cannot be cleanly revoked.** | "Tokens do not expire" (`plan/08:266-268`); revocation requires a `PATCH /users/{id}` (`plan/02:127`) or deleting the row — which FK references block (`plan/08:275-276`). | S2 | A leaked token stays valid until someone manually disables the account. Tokens also live in browser `localStorage` (`plan/06:31-33`), so an XSS on the dashboard page exfiltrates a long-lived credential. |
| **J14** | **Telemetry is on the critical path of deploys, with an undefined failure mode.** | The Ansible OTel callback emits spans during the deploy (`plan/09:270`). | S2 | If the collector is unreachable and the callback errors the task, deploys fail on telemetry loss; if it swallows errors, spans are silently lost. Neither behaviour is specified, and neither is testable in the mock-runner phases (`plan/10:7-8`). |
| **J15** | **Backups are "nightly (cron or another systemd timer)" — which nobody writes.** | `plan/08:250`; no cron file and no timer unit is specified. | S2 | In practice there is no backup until someone adds one; restore is also manual and takes the API and engine down (`plan/08:250-252`). |
| **J16** | **No migration framework, and sqlite cannot `ALTER` a CHECK constraint.** | `plan/08:253-259`: "every statement is `CREATE ... IF NOT EXISTS`, so `daolab init` is always safe to re-run"; schema changes are "a hand-applied `ALTER TABLE`". | S1 | Two consequences: (a) a changed `CHECK` needs a table rebuild (create-copy-drop-rename) — `ALTER TABLE` cannot do it; (b) `CREATE TABLE IF NOT EXISTS` means **every existing database keeps the old constraint forever**. So the state rename in 09 (A3/C7) will silently fail on any DB that was already created, and `daolab init` will report success while changing nothing. This is a mechanical bug, not a style issue. |
| **J17** | **`daolab sync` can abort mid-migration.** | `plan/01:352-358` deletes catalog rows; the FK semantics are wrong (B5). | S2 | A sync after a YAML rename raises `FOREIGN KEY constraint failed` and rolls the whole transaction back — leaving the DB and the YAML out of sync, with the sync command as the only way to notice. |
| **J18** | **The documented production install has no way in.** | The systemd units (`plan/08:198-223`) create no users; the only user creation path is the CLI (`plan/08:63-76`), and the bootstrap is a manual sequence (`plan/08:306-312`). | S2 | Install the system exactly as documented and you get an API with no admin token, no way to add one from the dashboard, and a runbook whose first step is a command nobody is told to run on the server. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| J1 | PARTIAL | `service/engine.py:469-502` (orphan sweep each tick), `:321-350` (cleanup → verify → reconcile ×N → quarantine), `:288` | Reconciliation exists; race P1 remains; the VM sweep is skipped with `DEPLOY_VMS=false` and never ran on hardware. |
| J2 | FIXED | `service/models.py:13-16`; `migrations.py:103-110`; `api.py:590-638`; `dashboard/admin.html` | `plan/08:487` still prescribes the SQL. |
| J3 | PARTIAL | `playbooks/bringup.yml:1-7,68-82`; `roles/host_vm/tasks/vm.yml:141-147` | A stale BDF fails by name or shows as `vf:` residue; nothing re-collects BDFs. |
| J4 | PARTIAL | `service/models.py:516-533`; `tests/test_playbooks.py` (a second run reports `changed=0`) | Idempotency is tested on the simulated lab; replay from the top by design. |
| J5 | PARTIAL | `service/engine.py:155-163,170-219`; `runner.py:555-561` | A stall no longer fails a job. Remaining: P4, and a booking whose whole window passed while the engine was down is still deployed, then expired. |
| J6 | PARTIAL | `service/health.py`; `engine.py:522-545` | Continuous health needs `DAOLAB_OTEL_ENDPOINT`; off by default, so only the owner's Re-check probe watches. |
| J7 | PARTIAL | `roles/ebf_set/tasks/main.yml:7-28`; `engine.py:273-275` | A deploy stops on a marker mismatch; the marker is not read from the card and is not reconciled with the DB. |
| J8 | OPEN | `roles/ebf_set/tasks/main.yml:1-6` (stub); `service/config.py:73` | Reflash still manual, no rollback. |
| J9 | OPEN | `setups/tb-01.yml`; `engine.py:304-318` | One setup; quarantine has no expiry, auto-reconcile or alert. |
| J10 | PARTIAL | `service/api.py:1001-1010` | IPAM is the authority, but `/internal/vm-ip` still accepts any IP (P5). |
| J11 | FIXED | `service/runner.py:429-433,504-575`; `api.py:351-374` | Per-job artifacts, `job.log`, log tail, failing task as the error. |
| J12 | OPEN | `service/api.py:105-115,661-765` | No idempotency key; quotas cap the damage (consequence corrected in O). |
| J13 | PARTIAL | `service/cli.py` (`--rotate`); `api.py:574-587` | Rotation in place; tokens still never expire and live in `localStorage`. |
| J14 | PARTIAL | `playbooks/ansible.cfg:8-17`; `runner.py:646-659`; `roles/otel_agent` | Off by default; with it on, a failed agent download fails the deploy; collector-down behaviour untested. |
| J15 | OPEN | `plan/08:280-287`; `deploy/` | No backup timer is shipped. |
| J16 | FIXED | `service/migrations.py:1-18,167-195,251-312`; `tests/test_migrations.py` | Numbered migrations on `user_version`. |
| J17 | AUDIT-WRONG | `service/catalog.py:500-522` | No FK abort (O); the real issue: a rename silently deletes the old row's terminal history. |
| J18 | PARTIAL | `plan/08:402-411` | A user step exists; no admin bootstrap in the production sequence; no units shipped. |

---

## K. Structural gaps — things the design never provides

| ID | Gap | Why it matters | Severity |
|---|---|---|---|
| **K1** | **No per-reservation log/artifact surface in the API.** The runner event stream goes to journald (`plan/08:236-237`); the API exposes nothing. | When a reservation fails, the operator must ssh to the host and read journald. 09's `GET /jobs/{id}` with a log tail (`plan/09:167`) fixes it. | S2 |
| **K2** | **No way to release a failed hold through the product** (only raw SQL, `plan/08:360`). | J2. The admin dashboard shows a stuck setup with no button. | S1 |
| **K3** | **No per-user limits in 01–08.** 09 adds "1 live + 2 future" quotas (`plan/09:189`, `plan/10:77`). | One user can fill the 168-hour horizon (`plan/08:149`) with bookings; FCFS by `created_at` (`plan/README.md:26-27`) gives no fairness guarantee. | S2 |
| **K4** | **No IP conflict detection.** Static data IPs are written blind (`plan/07:153`, `plan/07:173`); 09 adds an arping check (`plan/09:265`). | Two reservations or a stray host on the same segment produce a silent black hole; the `verify` gate may still pass on the proxy side. | S1 |
| **K5** | **The second setup is fiction.** `README.md:172` wants "a second setup YAML (even with fake resources) to prove N-setup matching" — but no test, fixture or acceptance criterion exists for it (`plan/08:314-321` scopes tests to the scheduler). | Scale-out is the headline claim ("more files, zero code changes", `README.md:8-9`) and it is untested. | S2 |
| **K6** | **No CI and no test plan beyond the scheduler.** `plan/08:314-321` defines `tests/test_scheduler.py` and *suggests* `test_catalog.py` / `test_api.py`; nothing runs them on change. | The phase gates are all manual (`README.md:124`), so "make test" is a habit, not a gate. | S2 |
| **K7** | **No schema/version field for the YAML contract.** `plan/01` owns the use-case and setup YAML schema; neither carries a version. | An old `usecases/*.yml`, or a user's DIY template map, can silently mismatch the current code — and `daolab sync` validates field *names*, not semantics. | S2 |
| **K8** | **No capacity model for pools.** `pool` is only a grouping label (`plan/README.md:13`). | Nothing answers "how many reservations can this pool take?", so the dashboard cannot show capacity, and the scheduler cannot prefer an emptier setup. | S3 |
| **K9** | **The platform itself is unobservable in 01–08.** Journald only (`plan/08:234-235`). | 09 adds OTel for the *application*, but not for the API/engine themselves — a wedged engine is discovered by a user. | S2 |
| **K10** | **The role model hardcodes exactly one DPU and one NIC host.** `on: dpu`/`dpu_host`/`tg_host` resolve to "the member with `kind: dpu`" and "the `host` field of the `kind: nic` member" (`plan/05:305-307`). | The schema has a `kind` enum (`plan/01`), and the README promises "many, mixing DPUs, GPUs, NICs and pure compute" (`README.md:7-8`) — but a second DPU or a GPU member has no role selector. Scale-out stops at one of each. | S2 |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| K1 | FIXED | `service/api.py:970-988` (`GET /jobs/{id}`, `GET /reservations/{id}/jobs`), `:990-998` (SSE) | Logs on the API host's disk, pruned by hand. |
| K2 | FIXED | `service/api.py:590-638`; `dashboard/admin.html`; `tests/test_force_clean.py` | |
| K3 | FIXED | `service/api.py:708-720`; `config.py:88-89` | 1 live + 2 future per non-admin. |
| K4 | PARTIAL | `roles/lease_ip/tasks/lease_one.yml:24-55`; `api.py:1067-1091` | Management IPs checked with ICMP `ping`, not arping (P6); data-plane IPs unchecked. |
| K5 | PARTIAL | `tests/test_scheduler.py` (setup-02 fixtures) | Tested at unit level (also at `82b32bd`, see O); no second setup YAML, no sync→placement test. |
| K6 | PARTIAL | 859 tests incl. a simulated lab running the real playbooks and browser tests; `Makefile` | Still no CI (`.github/` holds only templates). |
| K7 | OPEN | `service/catalog.py:221-241,353` | Stronger validation; no schema version. |
| K8 | OPEN | `service/schema.sql:17-22`; `scheduler.py:271,289` | No capacity model. |
| K9 | PARTIAL | `service/api.py:31`; `engine.py:609` | Traced when OTel is on; no engine heartbeat. |
| K10 | OPEN | `service/models.py:208-242`; `scheduler.py:156-161`; `tasks/resolve.yml:48` | One DPU + one NIC host per setup. |

---

## L. Sequencing and risk placement — the order the plan intends to build in

| ID | Flaw | Evidence | Severity | Consequence |
|---|---|---|---|---|
| **L1** | **The riskiest integration is validated last.** | `plan/10:7-8`: "Every task up to P5 can be built and demonstrated with `DAOLAB_RUNNER=mock`". Ansible, XCP-ng, VF passthrough, CPT and the OTel callback are therefore untested until P6 (`plan/10:149`). | S1 | Six phases of mock work build confidence in a system that has never touched the hardware. The first real run is also the first integration test — with the whole schedule riding on it. |
| **L2** | **The hardware bring-up that gates P6 has no protected slot.** | B-85 is "any time" (`plan/10:176-177`) yet gates B-81 and B-86 (`plan/10:47`); the plan itself calls hardware surprises "the biggest schedule risk" (`plan/10:52-53`). | S1 | The known-blocked item (F2: SDP VFs cannot be created on `dpu-01-xcp`) sits on the critical path with no owner-time reserved for it. |
| **L3** | **Doc-folding is scheduled after the drift has already accumulated.** | B-90 (fold 09's changes back into 02–05) is placed in P1–P3 (`plan/10:155`), while the code that follows is written against 09. | S2 | Every phase from P0 onward reads a doc set that is known-wrong, and the correction lands at the end (A8). |
| **L4** | **The migration is a single big-bang cutover.** | 09 changes the state machine (A3), the API surface (A5), the schema (C7, C9) and the playbooks (A2) together, with no additive-then-migrate step. | S1 | With no compatibility window, no migration framework (J16) and no reconciliation (J1), a half-landed migration leaves the DB, the lab and the docs three different systems. |
| **L5** | **No rollback plan for the plan itself.** | Nothing in `plan/08`'s schema stance (`plan/08:253-259`) or `plan/10` describes how to back out of a partially landed phase. | S2 | The documented recovery vocabulary ("re-run the playbook", `plan/08:354`) does not cover a partially migrated DB. |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| L1 | PARTIAL | commits `074e6f0`, `b1931de`, `8ffb2ad` | The DPU/VPP path ran on the board and surfaced four real faults (venv PATH, SSH pipelining vs `vppctl`, `vpp_main`, the exporter's `trim_blocks`). VMs, VF/SDP passthrough, CPT/TLS, SmartNIC and the OTel callback have never run on hardware. |
| L2 | PARTIAL | `plan/10:156,185` | DPU bring-up done; the XCP-ng / SDP half has no owner time. |
| L3 | PARTIAL | `plan/10:163` | B-90's narrow scope left 01, 07, 08 and both READMEs contradicting the code. |
| L4 | OBSOLETE | `service/migrations.py:251-262` | Landed incrementally through additive migrations. |
| L5 | PARTIAL | `service/migrations.py:292-295` | Forward-only migrations with a newer-DB guard; no down migrations. |

---

## M. Invariant and governance violations — the design breaks its own rules

| ID | Rule | Violation | Severity |
|---|---|---|---|
| **M1** | "A plan doc never contradicts the spec; it refines it" (`plan/README.md:15-16`) | `plan/09` contradicts the spec on six axes at once (A1–A6). | S1 |
| **M2** | "No invented scope … nothing here adds product capabilities, services, or technologies beyond the spec" (`plan/README.md:21`) | 09 adds OTel/Prometheus/Jaeger/SSE/IPAM/quotas/`AuthorizedKeysCommand` — and `plan/09:525-527` asserts "This is the only new technology". | S2 |
| **M3** | "A code change that contradicts a plan doc must update that doc in the same PR" (`plan/README.md:185-188`) | The reverse direction is unmanaged: a *doc* that contradicts the code (09, 10) is how nine documents ended up disagreeing. | S2 |
| **M4** | The root README is "the product spec and source of truth" (`plan/README.md:7-9`) | But the root README itself describes one of the two designs (two hosts, four playbooks, `tearing_down` — `README.md:22`, `29`, `37`, `112-120`), so the arbiter is itself one of the disputants. | S1 |
| **M5** | Invariant 1: "Use cases and setups are data, never code. New app = one YAML" (`README.md:178`) | The `install_map` is hardcoded in Ansible (`plan/05:225`), the `on:` selectors are hardcoded (`plan/05:305-307`), and `ebf_set` has a hardcoded var contract (`plan/05:276-285`). A new application still needs a code change — a new role and a new install-map entry. | S2 |
| **M6** | Invariant 3: "Every playbook is idempotent → engine retry = run again" (`README.md:181`) | Asserted, never designed: no idempotency framework, no idempotency test, no checkpointing (J4), and one prose claim already provably wrong (`plan/05:262` invents a non-existent `xe` filter and corrects it inline — a sign the command set was not verified). | S1 |
| **M7** | Invariant 6: "Dashboards are views over the API only" (`README.md:184`) | 09's dashboard consumes an `/availability` endpoint and a `mode`/`slot_conflict` vocabulary that `plan/02` does not define (`plan/09:158`, `plan/09:472` vs `plan/02`), and 09's `admin.html` shows quarantine/force-clean actions with no counterpart in `plan/02`'s error and endpoint tables. The "view over the API" invariant is currently unimplementable for the 09 design. | S3 |

**Status on `rama`:**

| ID | Status on `rama` | Evidence (current `file:line`) | Note |
|---|---|---|---|
| M1 | PARTIAL | `README.md:55,70` | A3 reconciled; A1, A2, A4 still contradict the spec; the precedence rule is unchanged. |
| M2 | OPEN | `plan/README.md:20-21` | The added scope is built while the rule forbids it. |
| M3 | OPEN | `plan/README.md:185-188`; `plan/10:156-157` | Docs that contradict code are not managed — e.g. B-85/B-86 still "blocked" although the board runs. |
| M4 | PARTIAL | `README.md:11-42` vs `README.md:6-7,62,104,141-153,212` | A current section was added; the README now contradicts itself (vpp "planned", "webhook on ready"). |
| M5 | OPEN | `roles/host_vm/defaults/main.yml:19-28`; `playbooks/apps/<app>/`; `playbooks/vars/dataplane.yml` | A new app still needs an `apps/` directory, install-map ids and a data-plane entry. |
| M6 | PARTIAL | `tests/test_playbooks.py` | Idempotency tested on the simulated lab; no checkpointing; no VM hardware proof. |
| M7 | FIXED | `plan/02` endpoint table and job / force-clean sections | The dashboards use only documented routes. |

---

## N. If you only fix eight things (ranked by blast radius)

| Rank | Fix | Why it is first | Defects closed |
|---|---|---|---|
| 1 | **Decide the topology and the design** (D-1 … D-5) | Everything else is written against one of two systems; no work is safe until it is chosen. | A1–A8, C1–C15, I1–I5, M1, M4 |
| 2 | **Add a reconciliation loop** (DB ↔ XCP-ng ↔ card) | Without it the database is a fiction: orphaned VMs, stale `hw_class`, silent drift. 09 has it; 01–08 does not. | J1, J3, J7, H6, H10 |
| 3 | **Make the functional gate typed** — exit status + parsed field, artifacts stored, and a continuous health check | The current gate is a substring match on tool output (I9) and exists only at provisioning time (J6). | B1, I9, I10, J6, J11 |
| 4 | **Single-source the state machine and the schema** — one transition table, one owner, a real migration path | Three writers and three copies of the table (H8); the DDL CHECK rejects the new states and `CREATE TABLE IF NOT EXISTS` can never change it (C7, J16). | A3, C2, C7, H8, I3, J16 |
| 5 | **One IP mechanism and one IP authority** | Four mechanisms are live at once (H5) and the callback trusts whatever the playbook posts (J10). | A6, C11, H5, J10, K4 |
| 6 | **Model the hold as state, not as a SQL UPDATE** | A stuck setup currently has no product-level release path, and the dashboard loses it. | J2, K2, C2 |
| 7 | **Re-verify hardware truth automatically + give the board reflash a rollback** | A stale BDF or a bricked card takes the only setup down, with no detection and no recovery. | J3, J8, D2, D4, F2 |
| 8 | **Repair the doc set itself** — counts, references, and one authoritative design | You are navigating by a map that says "nine documents" for eleven, cites a contract that does not exist, and hands B-90 a scope that cannot reach its own done-when. | B10–B13, A8, H12, I19, M3 |

**Status on `rama`:** 1 — design decided (Option B, as built), topology **still open** (one host
chosen, against F1). 2 — mostly fixed (orphan sweep, verify_clean, reconcile, quarantine; P1
remains; VM side never ran on hardware). 3 — partial (artifacts stored, continuous health built
but opt-in; `verify` still a substring match, B1 still wrong). 4 — fixed (H8, J16). 5 — partial
(one authority in code; `/internal/vm-ip` unchecked; docs stale). 6 — fixed. 7 — open. 8 — open.

---

### What is *sound* in the design (so it is not thrown away with the above)

- **The core idea is right:** a setup is a data file; the scheduler matches capabilities, not hostnames, and is a pure function over a DB handle (`plan/03:9`) — that is genuinely the load-bearing part of the system and it is designed well (E20).
- **Data-driven use cases with a single YAML per application** is the right scalability story — it just needs the `install_map`/selector exceptions fixed (M5).
- **Idempotent playbooks + retry = run again** is the right model for Ansible — it just needs a framework, not a promise (M6, J4).
- **SQLite + WAL + a single API worker** is a sound prototype substrate (`plan/08:241-244`, E14) — the mistake is the *trigger* for leaving it (I12).
- **Dumb views over the API, topology in YAML, mermaid rendered verbatim** (`README.md:184`, `plan/06:228-239`) is exactly right for a two-page dashboard.
- **Best-effort teardown followed by verification** (`plan/09:386-401`) is a better model than "Ansible must succeed" — 09's instinct here is right even though its implementation contradicts 05 (C13).
- **Capability matching instead of hostname matching** is what makes N setups possible at all.

---

## O. Corrections to the original audit

Re-checked against `82b32bd` (what the audit read) and `rama`. Objective slips are already fixed
in place above (A3 → C7, B6 / C6 → 18 variables, B8 → 11 routes, C7 CHECK vs `foreign_keys`, D6
`i40evf`, E1 → `plan/10:126-127`, F1 / F2 → B-86, F4 → /22 and `plan/07`, G steps 1 and 4, M1 →
six axes). The judgement-level corrections:

| ID | The audit said | Correct statement | Evidence |
|---|---|---|---|
| B2 | NGINX answering 500 would pass `httpcheck_status == 1` | Only under a PromQL-filter reading; under the evaluator that was built, every series must pass, so the rule fails instead. The `http_check` rename could not be confirmed offline. | `service/health.py:101-103` |
| B5, J17 | `daolab sync` would abort with `FOREIGN KEY constraint failed` | It never did: the code blocked removal for a live reservation and deleted terminal reservations before the parent row. Only the prose's reasoning is wrong. | `service/catalog.py:416-433` at `82b32bd` (now `:500-528`) |
| B9 | `http://localhost:8080` as the default is wrong | A 0.0.0.0 bind is reachable at `localhost:8080`; the bind default is documented and discussed separately. Overstated. | `plan/08:149` |
| B11 | "B-01…B-91" is wrong | The range is literally accurate; only "sparse IDs" is missing. Misleading at most. | `plan/10` |
| B12 | "15 planned" is wrong | `init` seeds 15 rows without a YAML, all `planned` until `sync`; the "16 rows" in `plan/06` is after sync. Not an error. | `service/catalog.py:28-158`; `service/cli.py:47,52` |
| C3 | `plan/02:266` is a third interval form | It is the same closed interval as `plan/02:212`. | `82b32bd` |
| C13 | Teardown in 09 unbinds VFs, which 05 forbids | The "unbind VFs" step was in 09's *reconcile*, and 05 forbade editing pciback, not returning VFs to the assignable pool. Overstated. | `plan/05:256-257` at `82b32bd` |
| D10 | `plan/03:215` lets slot-shift overlap another lead | That line is the "cancellation frees a slot" edge case. | `82b32bd` |
| E6 | E810: 256 VFs, as stated by the plan | No plan doc states the 256 figure; `plan/05:356` is the VF-family row. Background, not a verified doc claim. | grep of `plan/` at `82b32bd` |
| H2 | Three implementations of the lead rule | One pure helper with two callers; the inputs differ (projected vs current class); the playbook only checks the board. | `plan/04:104-106`, `plan/03:57` at `82b32bd` |
| H3 | Two owners of the capability union | The union is computed only at sync; the scheduler only reads it. | `plan/01:345-347`, `plan/03` Step 1 |
| H6, B14 | The residue check is decorative | 09's check was "name **or** tag"; the name half matched the `daolab-r<id>-*` labels. Overstated. | `plan/09:405` at `82b32bd` |
| H8 | 01–08 has no guard against a lost update | `plan/04` already mandated compare-and-set (`UPDATE … WHERE state=…`, rowcount 0 ⇒ skip); B-21 centralised it. | `plan/04:91-98` at `82b32bd` |
| H10, I11 | The engine writes hw_class at `plan/04:206`; 09 has the playbook write it | `plan/04:206` is "never resets hw_class" (the write rule is `:166-168`); `plan/09:266` is an EBF flash on the board, not a DB write. The real second writer was the manual SQL in `plan/08:356`. | `82b32bd` |
| I1 | README.md:4 says "always slot-shift" | It says "FCFS with time-share on conflict"; the "never rejected" statements are `plan/README.md:27-28,181` and `plan/03:25`. | `82b32bd` |
| I2 | An S1 clash | 09 kept `DELETE` for queued bookings, so the designs overlapped only on ending a live slot. Overstated. | `plan/09:163` at `82b32bd` |
| I7 | Closed vs half-open between plan/02 and plan/03 | Omission: old `plan/03` also contradicted itself (`:115` half-open, `:141` closed). | `82b32bd` |
| I12 | The Postgres trigger silently breaks placement | `BEGIN IMMEDIATE` locks the sqlite file across processes, so a second API worker on sqlite stays race-free; and a Postgres locking story (exclusion constraint) already existed. | `plan/09:218-221,455` at `82b32bd` |
| I13 | `plan/08:83` (next_free_at), `plan/08:80` (`--hours 4`) | `plan/08` never mentions `next_free_at`; the example is `plan/08:97`. Finding stands. | `82b32bd` |
| I18 | The same failure quarantines the setup | Only when residue survives N reconcile attempts after the failed deploy's cleanup. | `plan/09:393-398` at `82b32bd` |
| J12 | A duplicate POST provisions on hardware the other is using | A duplicate `asap` POST slides past the first booking's window and a duplicate `at` POST gets 409; the harm is a silent second booking that uses a slot and quota. | `service/scheduler.py:179-218` |
| J13 | Tokens cannot be cleanly revoked | Revocation was clean (`PATCH /users/{id}` → 403, history kept); rotation was the unclean part (fixed: `add-user --rotate`). | `plan/08` "Disabling vs deleting" |
| J16 | The state rename would silently fail | Writing a new state against the old CHECK raises "CHECK constraint failed" (as C7 says); only "init reports success while changing nothing" was right. | C7 |
| K5 | No test or fixture for a second setup | `tests/test_scheduler.py:115-123` already built `setup-02` fixtures at `82b32bd`. | `82b32bd` |
| M6 | `plan/05:262` invents a non-existent `xe` filter | It says `name-label-startswith` is not a real filter and gives the working command. Not "provably wrong". | `plan/05:262-263` at `82b32bd` |

## P. New findings from the re-check

Problems in the implementation on `rama` that the original audit could not see. Each is from
reading the code; none is reproduced by a test yet.

| ID | Finding | Evidence | Severity |
|---|---|---|---|
| P1 | After a deploy's last attempt fails, its reservation is `failed` (not held) while its cleanup is only queued, and the setup stays `available` until cleanup → verify → reconcile ×N has found residue. A booking that comes due in that window starts on the same setup; single-flight serialises the jobs, but the new deploy can run before the old reservation's reconcile jobs (created later) — on a dirty setup, with a reconcile that may then stop the shared DPU app under it, or a quarantine that lands while it is live. Neither `_start_due` nor `_claim` waits for a failed reservation's cleanup. | `service/engine.py:284-288` (`failed` + cleanup job), `:202-216` (`_start_due` checks only `held_by`), `:375-393` (`_claim`); `service/models.py:16` (`HELD_STATES`); `service/scheduler.py:364-373` (drops only already-quarantined setups); `tests/test_cleanup_flow.py:148-160` books only after the cleanup | S1 |
| P2 | The TLS proxy's health can never be green: no httpcheck target is configured (`vm_httpcheck` is never set), and the evaluator requires every series to pass. | `roles/host_vm/tasks/vm.yml:72`; `service/health.py:101-103`; `usecases/tls-proxy-nginx.yml:14` | S2 |
| P3 | A DPU-only deploy has no functional gate: `verify` runs only with VMs; the `dpu_app` step only asks `vppctl show version`. | `playbooks/deploy.yml:96-103`; `playbooks/apps/dao-vpp/deploy.yml` | S2 |
| P4 | A hung playbook blocks the single engine indefinitely: `ansible_runner.run` gets no timeout, and `DAOLAB_JOB_TIMEOUT_SEC` only drives crash recovery of stale jobs, which cannot run while the engine is blocked. | `service/runner.py:555-561`; `service/engine.py:565` | S2 |
| P5 | `POST /internal/vm-ip` accepts any IP for any reservation in any state; nothing checks it against the reservation's lease. | `service/api.py:1001-1010` | S2 |
| P6 | The IP conflict check is ICMP `ping`, not arping as the docstrings say; a host that drops ICMP passes. | `roles/lease_ip/tasks/lease_one.yml:25,51`; `service/api.py:1070`; `service/ipam.py:16` | S3 |

---

### Verification sources

| Claim | Source |
|---|---|
| `h2load` output format | https://manpages.ubuntu.com/manpages/stonking/man1/h2load.1.html |
| OTel `httpcheck` / `tcpcheck` receivers, metric + label names, `http_check` rename | https://pkg.go.dev/github.com/open-telemetry/opentelemetry-collector-contrib/receiver/httpcheckreceiver |
| `community.general.opentelemetry` callback (`TRACEPARENT`, `OTEL_EXPORTER_OTLP_ENDPOINT`) | https://github.com/ansible-collections/community.general/blob/main/plugins/callback/opentelemetry.py and https://docs.ansible.com/projects/ansible/12/collections/community/general/opentelemetry_callback.html |
| Marvell OpenSSL engine + DAO NGINX TLS offload via CPT | https://marvellembeddedprocessors.github.io/dao/guides/dao-devel/white_papers/accelerated-nginx.html |
| OCTEON 10 CN106XX Ethernet: 4×50G + 2×10G | https://www.marvell.com/content/dam/marvell/en/company/media-kit/octeon-10/marvell-octeon-10-media-deck.pdf |
| SDP / CPT on CN10k (`cpt-firmware-cn10k`, `/sys/kernel/debug/cn10k/cpt/`) | https://www.marvell.com/content/dam/marvell/en/public-collateral/embedded-processors/marvell-octeon-tx2-dpdk-whitepaper.pdf and https://github.com/MarvellEmbeddedProcessors/marvell-firmware/releases |
| Intel E810 SR-IOV: 256 VFs total, split evenly across ports | https://cdrdv2-public.intel.com/630682/630682_E810%20SR-IOV%20SLES15%20ConfigGuide_Rev1.2.pdf |
| XCP-ng CLI (`tags:contains=`, `xe`/`xl` command set) | https://docs.xcp-ng.org/appendix/cli_reference |
| SQLite FK semantics (`NO ACTION` default, `ON DELETE CASCADE`) | SQLite foreign-keys documentation (behaviour verified against `plan/01:217-219`, `plan/01:236`, `plan/08:241`) |
| Lab hardware facts (F1–F4) | Live inspection of `tg-01-xcp` (10.68.0.52), `dpu-01-xcp` (10.68.0.53), board `dpu-01` (10.68.0.190) |
| Re-check on `rama` (status tables, O, P) | Read-only review of the `rama` working tree at `565b8ec` (2026-10-23), `git show 82b32bd:<file>` for the original text, and the real-board runs on xeon3-oct-112 of 2026-10-16 |
