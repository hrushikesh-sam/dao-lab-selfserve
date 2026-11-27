# B-109b + B-109c — Stage 0 findings (read-only)

Date: 2026-11-28. Worktree `~/b109bc`, branch `b109bc`, clean.
**BASE = `d97085a65f7108d5448296f10dd73a9b9f483c2f`** = `origin/rama` = HEAD, "Merge pull request #19
from hrushikesh-sam/b109a". Its second parent `dbbc5e9` adds `playbooks/files/daolab-capture.py`
(+ `tests/dpu/conftest.py`, `tests/dpu/test_capture.py`, the CI `dpu-scripts` job). Rule 1 holds.

On BASE: B-105 (`service/agecli.py`, `service/keys.py`, `service/approvals.py`), B-106
(`challenges.py`), B-107 (`tickets.py`, `hostcheck.py`) and X-ML are merged. **B-108a is not**
(`service/snapshot_holds.py` absent), as the prompt says.

Venvs present: `.venv` (Python 3.12.13, ansible-core 2.21.6), `.venv-220` (2.20.11),
`~/.venvs/b109a-310` (3.10 + pytest). age v1.1.1 is at `~/.local/bin`. `/usr/bin/python3` here
is 3.10.12 (the DPU's version). `/proc/sys/fs/protected_{hardlinks,symlinks}` = 1 on this laptop.

Nothing was built or run. The baseline (full suite, `.venv`, `/tmp` worktree at BASE) is the first
step of Stage 1, after review.

---

## 0. In one paragraph (simple first)

At the end of a slot, the engine must: lock the user out → pack `/home/lab` into a sealed
suitcase on the board → bring the suitcase to the controller and write "saved" in the DB →
clean up the apps → **only then** empty the home → check the room. The danger is emptying the
home without a saved suitcase. The design keeps that from happening: the wipe asks the DB first,
and every "check the room" on a snapshot setup also checks the home. Stage 0 found four things
that need your decision before code. They are listed first (§1). The rest are defaults I'll follow
unless you say otherwise (§7).

---

## 1. STOP items (rule 12: plan/prompt vs code disagree, or a decision looks wrong)

### S-1 Force-clean runs a `reconcile` job, not a cleanup, so a flag that only reaches `_run_cleanup` leaks the home
- **Simple:** after a quarantine, the admin presses "force-clean". That runs a different job type
  (reconcile). If that job's check-the-room doesn't look at the home, it says "clean" and hands
  the board, with the previous user's files, to the next user.
- **Facts:** `api.py:651-697` `force_clean` queues `models.create_job(conn, r["id"], "reconcile")`
  for every dirty reservation. `engine.py:363-378` `_run_reconcile` runs `rn.reconcile` then
  `rn.verify_clean`; clean → `released` + `_maybe_free_setup`. The prompt's engine flow only
  covers `_run_cleanup`.
- **DDL v4.1 §3.5/§4.3** says `failed → capturing` happens "on a **force-clean** retry (D-7-4)".
  So the signed design expects a capture retry in the reconcile path too.
- **Proposed default:**
  (a) `verify_clean` gets `daolab_snapshot_enabled: true` whenever the reservation's setup has
  `snapshot_enabled = 1`, in **both** `_run_cleanup` and `_run_reconcile` (engine puts
  `snapshot_enabled: True` into ctx only for enabled setups; see P-2).
  (b) `_run_reconcile` on an enabled setup gets the same snapshot branch, before `reconcile`:
  row `saved` → re-run the gated wipe; row `failed`/absent and save=1 and an active key → capture
  retry reusing the failed row (D-7-4), then the gated wipe; otherwise no capture and no wipe
  (home:lab stays, the setup stays quarantined).
  This edits `_run_reconcile` in `engine.py` (a listed file, not a listed function). **Need your OK.**
  If you want B-109c minimal instead: do (a) only and leave the D-7-4 retry for a later task. Then
  a failed capture can only be cleared by `skip_verify` or a two-admin `home_discard` (X-WIRE).

### S-2 The local `delete_snapshot` needs an audit action that `models.py` scope doesn't allow
- Prompt: `delete_snapshot` "deletes files then the row then audits". `models.AUDIT_ACTIONS`
  (`models.py:528-534`) is a closed tuple. The prompt allows `models.py` for "snapshot_capture
  audit action only". §5.9 gives `snapshot_delete` to B-108a.
- **Proposed default:** add `snapshot_delete` now, next to `snapshot_capture`, with detail
  `{snapshot_id, public_id, state, why: "retention"}`. Say in Interface that B-108a reuses it
  and doesn't add it again. **Need your OK** (it is a second `models.py` change).

### S-3 `delete_snapshot` arity: merged code calls it with 2 args, §5.6 and the prompt say 3
- `keys.execute_due_resets` (`keys.py:123-165`, merged B-105) calls `delete_snapshot(conn, snap)`
  (`keys.py:156`, **2 args**, matching §5.3), **inside its own `BEGIN IMMEDIATE`** (`keys.py:125`).
  §5.6 and the prompt say `delete_snapshot(conn, snap_row, root)` (**3 args**).
- **Proposed default:** keep `snapshots.delete_snapshot(conn, snap_row, root)` exactly as §5.6 says
  (B-108a can replace it). `prune_retention(conn, user_id, setup_id, delete_snapshot)` calls the
  callback as `delete_snapshot(conn, row)`, the same 2-arg shape `keys.py` uses. The engine passes
  `lambda c, r: snapshots.delete_snapshot(c, r, settings.snapshot_root)`. `delete_snapshot` **never
  opens a transaction**; the caller does (otherwise `keys.execute_due_resets` would fail with "cannot
  start a transaction within a transaction"). Put this in the Interface section for Rama.

### S-4 No file is listed for verify_clean's home count
- Prompt B-109b step 3: count entries of the home that differ from skel, "same 'empty' meaning as the
  capture script". `daolab-capture.py` has no count mode and must not change. The task breakdown
  listed `playbooks/files/daolab-home-count.py (new, 3.10) or inline shell`; **the prompt's FILES list
  drops it**.
- Inline shell (`diff -rq --no-dereference home skel`) has different semantics from `_same_tree`
  (`daolab-capture.py:290-314`): FIFOs, unreadable entries, and no `-xdev`, so it can walk a bind
  mount.
- **Proposed default:** `daolab-wipe.py` takes a **required** `--mode check|wipe` (no default, so a
  typo can't wipe). `check` is read-only. It runs the same guards, uses `_same_tree` copied from
  the capture rules, and prints `{"status": "clean"|"dirty"|"refused", "reason", "differ": n}`, where
  n counts the top-level names that differ plus the skel names missing from the home. The wipe also
  uses `_same_tree` as its own self-check after the skel copy. **Alternative:** add
  `daolab-home-count.py` to the file list. **Which one?**

---

## 2. Crash-point table (B-109c)

**How recovery works today:** a job that stops reporting for `DAOLAB_JOB_TIMEOUT_SEC` is put
back to `pending` (`engine.py:161-169`, `models.requeue_job`), and the **whole cleanup job is
replayed from the start**. So every crash below is followed by a replay of `_run_cleanup`.

**New pieces the table relies on:**
- (i) `begin_capture` sets `jobs.snapshot_id` = the row (DDL §3.8).
- (ii) A tick step `snapshots.fail_stale_captures(db)`, right after `_recover_stale_jobs`, marks
  `capturing → failed` for every row with no `running` job pointing at it (or `reservation_id`
  NULL). On a single engine that covers each crash, because the requeued job is `pending`, not
  `running`.
- (iii) `begin_capture` also treats a `capturing` row of its own reservation as failed (belt and
  braces for two engine processes).
- (iv) On reuse (D-7-4) the row keeps its id and gets a **new `public_id`**. The old public_id's
  controller files (the 3 files + `.incoming/*.part`) are deleted first.
- (v) The wipe gate re-reads the row from the DB.

| # | Crash point | DB row | Controller files | DPU | Home | What recovery does |
|---|---|---|---|---|---|---|
| C0 | before the `begin_capture` commit | none (or an older `failed` row) | none | none | intact | replay starts normally |
| C1 | after the `begin_capture` commit, before keygen | `capturing` | none | none | intact | tick → `failed`; replay reuses it (new public_id) |
| C2 | after `key.age` is written, before the playbook | `capturing` | `key.age` (S wrapped to the owner; S itself died with the process) | none | intact | → `failed`; reuse deletes the old `key.age` |
| C3 | during `snapshot_capture.yml` (tar\|age running) | `capturing` | `key.age`, maybe a partial `.part` | maybe `/var/lib/daolab/capture/<old>.age` (root 0600 ciphertext); the orphaned ansible-playbook may finish and delete it, or not | intact (capture only reads) | → `failed`; reuse deletes `key.age` + `.part`; the new public_id avoids `out_exists`; a DPU leftover is removed by the next capture's pre-clean (D-7) |
| C4 | after fetch, before the size/sha check and `os.replace` | `capturing` | `key.age` + a complete `.part` | deleted by `always` if the play ended | intact | as C3 |
| C5 | after `os.replace` → `.age` (+ maybe `hdr.json`), before the `mark_saved` commit | `capturing` | `.age`, `.key.age`, maybe `.hdr.json` | none | intact | → `failed`; reuse deletes all 3 + `.part` |
| C6 | after the `mark_saved` commit, before `cleanup.yml` | **`saved`** | 3 files | none | intact | replay: `begin_capture` sees `saved` → **skip capture** → `cleanup.yml` → wipe allowed → verify → prune |
| C7 | after `cleanup.yml`, before the wipe | `saved` | 3 | none | intact | as C6 (`cleanup.yml` is idempotent) |
| C8 | during the wipe | `saved` | 3 | — | partly wiped | replay → wipe again (idempotent) → skel |
| C9 | after the wipe, before verify / the finish txn | `saved` | 3 | — | = skel | replay → wipe again → verify clean → released |
| C10 | after prune, before the finish txn | `saved` (old ones pruned) | 3 per kept row | — | skel | replay; the prune is a no-op the second time. **Prune runs before the finish txn**, so a crash replays it |
| E1 | status `empty`, before the row delete | `capturing` | `key.age` | none (the script made no file) | intact | → `failed`; reuse → capture → `empty` again |
| E2 | after the row (+ its `key.age`) is deleted, before the wipe | none | none | none | intact | replay: no row → capture → `empty` → wipe |
| R1 | status `refused`, before `mark_failed` | `capturing` | `key.age` | the script removed its own partial out; `always` removes it too | intact | → `failed` (same end state as a refused capture) |

**Invariant the tests will check at every point:** the wipe runs only when the DB row of this
reservation is `saved` (or this run saw `empty`). Otherwise the home is byte-identical to before.

**How a crash is simulated:** a test-only `class Crash(BaseException)` raised at each point. It
passes through `engine._call`'s `except Exception`, as a real kill would; `_txn` rolls back. Then:
advance the clock, run a tick (requeue + the stale step), tick again (replay). Assert after each tick.

Ordinary errors (AgeError, OSError, a bad report, a sha mismatch) are caught in the snapshot branch:
mark `failed`, delete that attempt's files, no wipe, then verify (home:lab) → quarantine. Only
`BaseException`s escape.

---

## 3. Script delivery (B-109b): `ansible.builtin.script` with `executable: /usr/bin/python3 -I`

**Choice:** `ansible.builtin.script: daolab-capture.py --home … --owner-uid …` with
`executable: "/usr/bin/python3 -I"`, `become: true`, `failed_when` on rc ∉ {0, 1} or a bad report,
`changed_when: false`. The same for `daolab-wipe.py`.

**Why:**
- In ansible-core 2.21.6 the `script` action joins `executable` into the command string
  (`plugins/action/script.py:138-139`), so `-I` is passed. With `-I`, the script's directory and
  the cwd are not on `sys.path` and `PYTHON*` variables are ignored, so a file beside the script
  can't shadow a module (the capture script's docstring asks for exactly this).
- The temp copy lives in the connecting operator account's (dao-test) 0700 remote tmp dir, and
  `script` deletes it in `finally` (`script.py`, `_remove_tmp_path`). `lab` can't reach it. Only
  dao-test and root can, and both already have root.
- **No persistent file** on the board. A staged copy in a root-only dir would need a new DPU
  path, a prefix for tests, and its own cleanup.
- Args (the public recipient, paths, uid) are in the DPU's process list for the run. None is
  secret (S never comes here).
- `/usr/bin/python3` is fixed (it's also the scripts' shebang). In the Lab harness it is this
  laptop's 3.10.12 and CI's 3.12, so the playbook test also runs the scripts on 3.10 here.

**Known cost, see D-6:** `fetch` under become always uses `slurp` (`fetch.py:71`, `:110`).

---

## 4. Every place flag-off parity could break (and how each is held)

| # | Where | Risk | Default |
|---|---|---|---|
| P-1 | `engine._run_cleanup` (`engine.py:331-360`) | any change to the body | One early branch `if _snapshot_enabled(db, res): return _run_cleanup_snapshot(...)`. The existing body stays byte-identical. The snapshot variant reuses the verify/reconcile/finish tail through a helper extracted **verbatim** (a pure move), proven by the unchanged suite |
| P-2 | `engine._context` (`engine.py:243-247`) | new ctx keys reach every runner | Add `snapshot_enabled: True` **only** for enabled setups; never a False key |
| P-3 | `AnsibleRunner.extravars` (`runner.py:508-529`) | `test_deploy_access.py:119`, `test_pubkey_strict.py:195-262` use real extravars | Not touched. `daolab_snapshot_enabled` goes only through `_play(..., extra=)` of `verify_clean`, only when ctx says enabled |
| P-4 | `verify_clean.yml` | a skipped `step: check_home` task still emits `runner_on_skipped` → `AnsibleRunner` records a "skipped" job step (`runner.py:469-472, 570-572`) | The home check is its own play with `hosts: "{{ 'r_dpu' if (daolab_snapshot_enabled \| default(false) \| bool) else 'r_none_' }}"`: no hosts matched → no task events. The report play adds `hostvars[item].daolab_home_residue \| default([])` → `[]` when off |
| P-5 | `MockRunner.verify_clean` + `VERIFY_CLEAN_STEPS` | pinned by `test_runner.py:103-105, 174, 259`, `test_cleanup_flow.py:104-105`, `test_engine.py:538` | Constant unchanged; the mock adds a `check_home` step and `home:lab` only when ctx has `snapshot_enabled` and the home wasn't wiped |
| P-6 | `MOCK_FAIL_TARGETS` (`runner.py:37`) gains `capture` | the error text lists the tuple | Only membership is tested (`test_runner.py:315`). Docstrings updated |
| P-7 | `RESIDUE_KINDS` / `CHECK_FOR_KIND` (`runner.py:75-78`) | `parse_residue("home:lab")` changes from `("other", …)` to `("home", "lab")` | No existing test uses these kinds (`test_runner.py:420-423`, `test_ml_tvm.py:93-94, 656`); the dashboard shows residue as text (`admin.html:319`). Mapping: `home → check_home`, `tripwire → check_tripwire`, `restore_incomplete → check_home` (later tasks rename if needed) |
| P-8 | the new tick step (stale capturing) | runs every tick, flag or not | A single `UPDATE … WHERE state='capturing' AND …`: no rows → no-op. **No new `TickResult` field** (its repr is printed by `main`; tests only check the substring, `test_engine.py:1052`, `test_migrations_guard.py:509,529`), count logged only |
| P-9 | `Settings` + `.env.example` | `test_config.py:92-102` requires every field as an **uncommented** `KEY=value` line, and no strays | See D-1 |
| P-10 | `AnsibleRunner.PLAYBOOKS` gains `snapshot_capture`, `home_wipe` | `test_ansible_runner.py:110` uses `>=` | safe |
| P-11 | `AUDIT_ACTIONS` gains `snapshot_capture` (+ S-2) | no exact-tuple test | safe |
| P-12 | `cleanup` audit detail (`engine.py:357-359`) | shape change | Unchanged for flag-off; the snapshot variant adds `"snapshot": <status>` |
| P-13 | the `_run_reconcile` change (S-1) | — | Same early-branch rule; flag 0 → the existing body |
| P-14 | `tests/fakelab.py` `getent` (`fakelab.py:341-346`) | always prints uid 1001 | Use `users[name].get("uid", 1001)`: unchanged unless a test sets it |
| P-15 | custom test runners (`Scripted` in `test_cleanup_flow.py`) have no `capture`/`wipe` | — | The engine looks them up with `getattr` only on enabled setups; if missing → treat as capture failure (fail closed) |

**Proof tests:** the existing suite unchanged, plus `test_flag_off_never_calls_capture_or_wipe`:
a spy runner whose `capture`/`wipe` raise, on a setup with `snapshot_enabled = 0` and
`save_snapshot = 1` and an active key → released exactly as today, spy untouched, no
`snapshots` rows, no `snapshot_capture` audit.

---

## 5. Facts that shape the build (file:line at BASE)

**Engine / runner**
- `_run_cleanup` (`engine.py:331-360`): cleanup → verify → reconcile ×N → one finish txn (job
  result, reservation move, quarantine/free, audit). `_call` turns any `Exception` into a failed
  `RunResult` (`:234-240`).
- `EXPECTED_STATES["cleanup"] = ("cleaning", "failed")` (`:107`). A failed-deploy auto-cleanup on an
  enabled setup goes through the same branch (usually `empty` → wipe).
- `AnsibleRunner._play` (`runner.py:531-629`) writes extravars to ansible-runner's `env/extravars`
  in the job dir. The capture's extras (public recipient, public_id, cap, fetch path) are all
  public. Output JSON is read from `daolab_output_file`.
- `fetch` under become (`ansible/plugins/action/fetch.py:71, 110-129`) always slurps (see D-6).
- The Lab harness (`tests/test_playbooks.py:76-147`): local connection, fakes first on PATH
  (`getent`, `pkill`, `loginctl`, `usermod`, `chpasswd`, `chage`, … in `FAKES`), `ansible_become`
  False unless `CAN_BECOME`. The snapshot tests always pass `ansible_become: False` explicitly, like
  `test_playbooks.py:573, 1093`. That keeps root-owned files out of `tmp_path`.
- The real-runner pattern (`test_orphans_canary.py:42-55`): inject `run=` into `AnsibleRunner`.
  For the e2e test, `run=` calls `Lab.run(kw["playbook"], **kw["extravars"])` and returns
  `SimpleNamespace(status, rc)`, so the real `AnsibleRunner.capture()` → real playbook → real
  script → real age → real `snapshots.py` all run.

**Capture script** (`daolab-capture.py`, unchanged)
- `--owner-uid` is required. `--out`'s parent must be a real dir owned by the **effective uid**,
  mode 0700 (`:173-180`), not inside home/skel.
- rc 1 = refused. Nothing on stderr.
- The playbook creates the capture dir with mode 0700. With become it's root's; in the Lab it's the
  test user's, and the script's own ownership check then passes or refuses correctly in both cases.

**Data model / keys**
- `snapshots.reservation_id` is UNIQUE (DDL §3.5): one row per reservation. That is why a replay
  after `saved` must skip the capture (C6).
- `keys.active_key` (`keys.py:92-96`) returns `(id, user_id, recipient, fingerprint)`.
- `agecli.keygen`/`encrypt_to` call `version()` (needs real age). Engine unit tests monkeypatch
  them; the canary and e2e tests are `requires_age`.
- `setups.snapshot_enabled` and `reservations.save_snapshot` are only **written** by B-108a (not
  landed). Today the new path is reachable only from tests.

**E-2 (swap):** the API checks swap at startup when snapshots are in use (`api.py:103-104`);
`tickets.write` checks it too (`tickets.py:129`). The engine doesn't yet (X-WIRE). B-109c holds S in
engine memory, so see D-9.

---

## 6. Interface (what B-109c will expose; for the commit's Interface section)

```python
# service/snapshots.py
def paths(root, user_id, public_id) -> dict                # {"age", "key", "hdr"} under <root>/u<uid>/ (exactly §5.6)
def incoming_path(root, user_id, public_id) -> str          # <root>/u<uid>/.incoming/<public_id>.age.part
def begin_capture(conn, res_row, key_row) -> sqlite3.Row    # own txn; reuses this reservation's failed row (new public_id)
def mark_saved(conn, snapshot_id) / mark_failed(conn, snapshot_id)   # CAS from 'capturing'
def fail_stale_captures(conn) -> int                         # tick step; count only
def prune_retention(conn, user_id, setup_id, delete_snapshot) -> int # DDL §3.5 query; callback(conn, row)
def delete_snapshot_files(root, user_id, public_id) -> int   # the 3 files; missing = fine
def delete_snapshot(conn, snap_row, root) -> None            # files, row, audit; caller's txn (B-108a may replace)
# service/runner.py
AnsibleRunner/MockRunner.capture(res, step, *, recipient, public_id, cap_bytes, fetch_to) -> (RunResult, dict | None)
AnsibleRunner/MockRunner.wipe(res, step, *, allowed: bool) -> RunResult
RESIDUE_KINDS += ("home", "tripwire", "restore_incomplete")
MOCK_FAIL_TARGETS += ("capture",)
```

Playbook extra vars: `snapshot_capture.yml` — `daolab_snapshot_recipient`, `daolab_snapshot_public_id`,
`daolab_snapshot_cap_bytes`, `daolab_snapshot_fetch_to`; `home_wipe.yml` — `daolab_wipe_allowed`;
`verify_clean.yml` — `daolab_snapshot_enabled`. Test-only prefixes: see D-8.
Job steps: `capture`, `fetch_snapshot`, `wipe_home`, `check_home`.

---

## 7. Questions, each with the default I'll use

| # | Question | Default |
|---|---|---|
| D-1 | `.env.example` "the two new settings, commented": a `# DAOLAB_…=` line fails `test_config.py:92-96` | A `# comment` line above an **uncommented** `DAOLAB_SNAPSHOT_ROOT=./var/snapshots` and `DAOLAB_SNAPSHOT_CAP_BYTES=536870912`, the same pattern as the age lines (`.env.example:58-62`) |
| D-2 | Headroom rule: the prompt says `free ≥ 2 × cap (+ margin)`; E-15 (UNSIGNED) says `cap + one cap + 10% of the volume` | The prompt's rule: `shutil.disk_usage(snapshot_root).free ≥ 2 × cap + 64 MiB`, no new setting, reason `storage_low`. E-15's mount check and quotas → follow-ups (B-108a/B-114) |
| D-3 | Audit detail: the prompt lists `{snapshot_id, public_id, status, raw_bytes}` | Also `reason` (a fixed code: the script's reason or `storage_low`, `swap_unsafe`, `size_mismatch`, `sha_mismatch`, `bad_report`, `runner_error`, `age_error`, `no_key`, `save_off`), plus an audit with status `skipped` when save=0 or there's no active key (snapshot_id null), so every quarantine is explained. No paths |
| D-4 | Files of a failed attempt | Delete `key.age` + `.part` (+ `.age`/`.hdr.json` if present) right away; keep the row `failed` for D-7-4 and the audit |
| D-5 | Wipe gate defence in depth | `allowed = (row from the DB: state == 'saved', same reservation/user/setup, **and its 3 files exist**) or this run saw `empty`` |
| D-6 | `fetch` under become slurps the whole ciphertext (base64 in memory; up to ~0.7 GB of JSON + 0.5 GB decoded at the cap). The Lab can't exercise this path (become off) | Accept it for B-109b and document it in the playbook header and Follow-ups; measure it in B-114/Phase 9 with E-8's 512 MiB tree. The alternatives need a decision change: an operator-readable ciphertext (changes Q-E15), or engine-run scp (B-110's `dputransport`) |
| D-7 | DPU leftovers from a dead run in `/var/lib/daolab/capture/` | Before running the script, `snapshot_capture.yml` removes leftover regular `*.age` files in that dir (only ever ours: root 0700, one capture per setup at a time); count logged, no names |
| D-8 | Path prefixes for the Lab tests | New `daolab_dpu_root` (default `""`) for home, skel and the capture dir: `{{ daolab_dpu_root }}/home/<lab login>`, `/etc/skel`, `/var/lib/daolab/capture`. The existing `daolab_age_root` (install_age.yml) for `/usr/local/bin/age`. The engine never sets either |
| D-9 | B-109c holds S in engine memory; E-2's engine-start swap check isn't wired yet (X-WIRE) | Call `hostcheck.require_safe_swap()` **before** `agecli.keygen()`. If swap is unsafe → `failed`, reason `swap_unsafe`, keep home, quarantine. On this laptop an enabled setup's capture then fails closed. Tests monkeypatch it |
| D-10 | D-7-4 reuse: keep or change `public_id`? | Same row id, **new** `public_id` (avoids `out_exists` on the DPU and stale files); old files deleted first |
| D-11 | Wipe details not in the spec | gid = the home dir's current gid (no new arg). The home dir inode itself stays (owner checked, mode unchanged). `copytree(skel, home, symlinks=True, dirs_exist_ok=True)` + `os.lchown` of everything. `removed` = the number of top-level entries. Post-wipe `_same_tree` self-check, else refused `not_clean`. Reasons: `ok, bad_args, path_unsafe, home_missing, home_not_dir, home_owner, mount_under_home, remove_failed, skel_failed, not_clean`. No sysctl check for the wipe (it never reads through links). Resetting a chmod-777 home dir → follow-up (tripwire, B-112) |
| D-12 | Is a refused capture a failed playbook? | Yes: the report is written to `daolab_output_file` first (always), then a fixed-message `fail` (`capture refused: <reason>`), so the job step shows "failed". The runner returns `(RunResult(ok=False), report)`; the engine trusts only a report that passes a strict shape check |
| D-13 | Validate `daolab_snapshot_fetch_to` in the playbook too | Yes: absolute, ends with `/<public_id>.age.part`, asserted with the public_id and recipient before use |
| D-14 | The verify home check when the home is missing / unreadable / refused | Residue `home:lab` (fail-safe: only a proven-equal tree is clean) |
| D-15 | `snapshot_root` permissions | Create root, `u<uid>`, `.incoming` as 0700. Refuse (`storage_unsafe`) if one is a symlink, not a dir, or not ours; chmod 0700 if ours. Files written 0600 via temp + `os.replace`; the fetched `.part` is chmod 0600 before the rename |
| D-16 | `snapshot_cap_bytes` validation | An int in 1 … 2^40 at load; a bad value fails `Settings.from_env` like the other ints |
| D-17 | SPLIT check ("each commit passes its tests on its own") | Fresh `/tmp` worktree at BASE + the b-only file set → run `test_playbooks_snapshot.py`, `tests/dpu/test_wipe.py`, `test_playbooks.py`, `test_cleanup_flow.py` on `.venv`; then the full patch → the B-109c files. Neither is a full-suite run |

---

## 8. Cross-task notes for Rama (not blocking B-109b/c; in Follow-ups)

- **F-1** `force-clean {"skip_verify": true}` (`api.py:670-673`) releases dirty reservations without
  running anything. It bypasses `home:lab`, so the next user can get the previous home. Suggest
  refusing `skip_verify` when the residue contains `home:` (api.py, Rama).
- **F-2** Enabling `snapshot_enabled` on a setup whose home holds legacy data (tb-01, E-13): the first
  user both sees it during their slot and captures it into their own snapshot. B-108a's enable
  should require a verified-skel home or a completed `home_discard`.
- **F-3** B-108a's `due_deletions` deletes `failed` rows "at once (hold 0)". Then D-7-4 reuse only
  works between the failure and the next sweep. `begin_capture` handles both "failed row" and
  "no row".
- **F-4** E-15 (unsigned): a `snapshot_root` mount check, a stale `.part` sweep at startup, quotas and
  the capacity log line are not in B-109c.
- **F-5** `revoke` kills only processes owned by `lab` (`roles/dpu_access/tasks/revoke.yml:3-7`). A
  root process the user started with sudo survives it. During capture that fails closed (tar rc 1 →
  `tar_failed`). After the wipe, the self-check catches writes made before it ends, but not later
  ones: tripwire (B-112) territory.
- **F-6** When the only residue is `home:lab`, the reconcile loop still runs `reconcile.yml` N times
  (harmless; it's today's behaviour for any residue).
- **F-7** E-15's "not the source of a running restore" prune rule applies once B-110 lands.

---

## 9. Plan for Stage 1 (after your answers)

1. Baseline once: `/tmp` worktree at BASE, `.venv` full suite (expect ≈1818 passed, 4 skipped).
   Log header: date, HEAD, venv, python, ansible-core, command; PATH and
   `DAOLAB_TEST_ANSIBLE_PLAYBOOK` pinned to the venv.
2. B-109b tests (red, with E-lines) → playbooks + `daolab-wipe.py` (+ the fakelab uid) → green on
   `.venv` and `.venv-220`, and `tests/dpu/test_wipe.py` on b109a-310.
3. B-109c tests (red) → `snapshots.py`, runner, engine, config, models, `.env.example` → green on both
   3.12 venvs.
4. One final verification in parallel (A/B/C), patch + TREE MATCH, SPLIT check (D-17), messages,
   SHA256SUMS.
