# Two P-0c patches: comparison and recommendation (23 Nov 2026, early)

Rama pushed `../P-0c.patch` (draft, unrun; commit `c048098`, 22 Nov 23:58) while Akshay's `P-0c-v2.patch` was being prepared.
Both close row 25 on the fake lab. They cannot be applied together: both create `tests/test_vm_names_strict.py`.
This note runs Rama's draft through its own addendum, then probes both patches with the same inputs.

| Patch | sha256 |
|---|---|
| `../P-0c.patch` (Rama) | `5ef4bc9be54adfe99b29c8c8d769a3df10cc06620ed951bdbe8b4ccb1ec68b90` |
| `P-0c-v2.patch` (Akshay) | `90bebe5c2964c7c5dba4d781259f2bb00939ce53c8238a2d6588973713e9941d` |

## 1. Rama's P-0c through `P-0C-OPERATOR-ADDENDUM.md` (scratch tree, py3.11 and py3.12)

| Step | Expected by the addendum | Result | Evidence |
|---|---|---|---|
| Applies after P-0 + F-5-1 | no output | applies | — |
| Red, tests only | the canary tests fail; list mode passes | as expected: list mode PASSED; destroy and chained FAILED; 46 failed / 1 passed in all (unit tests fail on the missing functions) | `evidence/rama-p0c/before.txt`, `before-canary.txt` |
| Green | all pass | **81 passed** on both | `evidence/rama-p0c/after.txt` |
| Stack order P-0 → F-5-1 → F-5-1b → P-0c | — | applies cleanly | — |
| Full suite, that stack | no FAILED | **1217 passed, 4 skipped, 0 failed** on both (the same 4 skips: playwright, sshd ×3) | `evidence/rama-p0c/tests/p0c-full-py*.txt`, junit in `evidence/rama-p0c/ci/` |

## 2. The same inputs through the orphan sweep (`engine._sweep_orphans`, py3.12)

A runner lists a benign orphan `daolab-r998-y` plus one name each:

| Name from XO | Today (prefix filter only) | Rama's P-0c | Akshay's P-0c v2 |
|---|---|---|---|
| `daolab-r` + 19 digits + `-x` | **sweep raises `OverflowError`** (int too large for SQLite) | `vm_name_ok=True`, **sweep raises `OverflowError`** | rejected, benign orphan destroyed |
| `daolab-r` + 5000 digits + `-x` | **sweep raises `ValueError`** (int digit limit) | `vm_name_ok=True`, **sweep raises `ValueError`** | rejected, benign orphan destroyed |
| platform name, 128-char role (allowed by `ident_ok`) | destroyed | **rejected (cap is 100)**, so that VM is never swept | destroyed |
| `daolab-r1--a` (role starts with `-`; the platform never makes it) | destroyed | accepted, destroyed (harmless) | rejected |

Evidence: `evidence/rama-p0c/probe-ids-{base,rama,akshay}.txt`; the probe script is `probe_ids.py`.

**Finding P-0e: an orphan-sweep denial of service. It exists today and is not fixed by Rama's draft.** Anyone who can name a VM `daolab-r<19+ digits>-x` makes `_sweep_orphans` raise on every tick:
- `int()` then a SQLite bind overflows, or the 4300-digit `int()` limit trips.
- The engine loop catches it as `tick failed`, so nothing crashes. But everything after it in that tick stops: the rest of `_sweep_orphans` (leases of terminal reservations), `_evaluate_health`, and the VM sweep itself.
- It happens every tick, for as long as the VM exists.

Same attacker as row 25, so it belongs in the same patch. Bounding the id to `[0-9]{1,18}` (always < 2^63) fixes it.

## 3. Other differences (design choices, both acceptable)

| | Rama | Akshay |
|---|---|---|
| Name pattern | `daolab-r[0-9]+-[A-Za-z0-9._-]{1,100}` | `daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (exactly `'daolab-r' ~ id ~ '-' ~ role` with role = `_IDENT`) |
| `tagged_vms()` | filters at the source | unchanged |
| `destroy_vms()` with a bad name | drops it, destroys the valid ones | refuses the whole call |
| `orphans.yml` | untouched (as the review asked) | list filter tightened, plus sync tests |
| Extra tests | canary promoted, unit tests | xfail(strict) for a direct extra var (orphans + reconcile); one-producer and known-readers tests |

## 4. Recommendation: one P-0c
**Take Rama's P-0c as the base** (his structure, `orphans.yml` untouched). Then:
1. Change `VM_NAME` to `daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. That fixes P-0e and accepts every name the platform makes.
2. Add regression tests:
   - a 19-digit and a 5000-digit id must not break the sweep;
   - a 128-char role passes;
   - from P-0c v2: the strict xfail, the one-producer test and the known-readers test.
3. Retire `P-0c-v2.patch`. It stays here as reference and evidence only.

Akshay can prepare that combined patch with red/green and full-suite evidence if Rama agrees.
