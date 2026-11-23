# P-0c combined: Rama's base + the bounded pattern + the review tests (23 Nov 2026)

This is built the way Rama asked (23 Nov, 01:13):
- one P-0c, with Rama's v1 as the base;
- `orphans.yml` untouched;
- source filter in `tagged_vms`, drop-not-refuse in `destroy_vms`;
- regex `daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}`;
- the 19- and 5000-digit sweep regressions, the 128-char role case, the strict xfail, and the one-producer and known-readers tests.

`P-0c-v2.patch` is retired and kept as reference only. **Nothing is committed, pushed or opened as a PR.**

| File | sha256 |
|---|---|
| `P-0c-combined.patch` | `58a25f53353bbeabaafed9dff61786220cadb86c51f8302d52df612947079376` |
| `P-0c-combined-vs-rama-v1.diff` (review aid: only the delta from Rama's v1) | see `SHA256SUMS` |
| Rama's `../P-0c.patch` (v1, the base) | `5ef4bc9be54adfe99b29c8c8d769a3df10cc06620ed951bdbe8b4ccb1ec68b90` |
| `F-5-1b.patch` (unchanged) | `823a54bcdefd8ae52a6c3226144b93978d25583e5d37adfbc079dd78471c0f5d` |

## Stack (apply on `40ebd6d`, `git apply --check` first)
`P-0-final.patch` → `F-5-1.patch` → `F-5-1b.patch` → `P-0c-combined.patch`

`P-0c-combined.patch` is built against that exact stack. Rebuilding from the patch file gives a tree identical to the tested one (`diff -rq` empty).

## What changed from Rama's v1 (`P-0c-combined-vs-rama-v1.diff`: 3 files, +132 / −5)
| File | Change |
|---|---|
| `service/models.py` | `VM_NAME = daolab-r[0-9]{1,18}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, plus a comment on the platform's name shape and P-0e. `vm_name_ok`, `split_vm_names` and all call sites are unchanged. |
| `tests/test_vm_names_strict.py` | The boundary moved with the pattern. VALID gains a 128-char role and an 18-digit id. INVALID's `"a" * 101` becomes `* 129`, and it gains a 19-digit id, `daolab-r1--a` and `daolab-r1-.a`. The docstring regex is updated. Everything else is untouched. |
| `tests/test_vm_names_review.py` (new) | The review tests listed in the next section. |
| `service/engine.py`, `service/runner.py`, `tests/test_orphans_canary.py`, `playbooks/orphans.yml` | Identical to Rama's v1. `orphans.yml` is not in the patch. |

**New tests in `tests/test_vm_names_review.py`:**

| Test | Checks |
|---|---|
| `test_an_18_digit_id_fits_sqlite_and_is_valid` | 18 digits are accepted and fit a 64-bit INTEGER |
| `test_a_long_id_is_refused[19, 20, 5000]` | longer ids are rejected |
| `test_a_long_id_never_breaks_the_sweep[18, 19, 5000]` | P-0e: the sweep doesn't raise, and the benign orphan is still destroyed |
| `test_a_128_char_role_passes_and_its_vm_is_swept`, `test_a_129_char_role_is_refused` | the role boundary matches `ident_ok` |
| `test_the_service_has_one_producer_of_daolab_vm_names_and_it_filters` | string literals only; `AnsibleRunner.destroy_vms` uses `split_vm_names` |
| `test_the_playbooks_that_read_daolab_vm_names_are_known` | the readers are `orphans.yml` and `reconcile.yml` |
| `test_a_playbook_never_evaluates_a_name_passed_straight_in[orphans.yml, reconcile.yml]` | `xfail(strict=True, raises=AssertionError)`: the documented limit until P-0d |

## Evidence (scratch checkout; Python 3.11 + ansible-core 2.19.15, Python 3.12 + ansible-core 2.21.6)
| Run | Expected | Result | File |
|---|---|---|---|
| **Red 1**: the patch's tests only, on P-0 → F-5-1 → F-5-1b | canary destroy and chained fail, list passes; unit tests fail (missing functions) | as expected on both: 60 failed, 3 passed, 2 xfailed; canary list PASSED, destroy FAILED, chained FAILED | `evidence/combined/red1-tests-only.txt` |
| **Red 2**: the new review tests against **Rama's v1** code | only the P-0e and 128-role tests fail | as expected on both: 6 failed (3 × long id accepted, 2 × sweep raised, 1 × 128 role rejected), 5 passed, 2 xfailed. The reasons recorded are `OverflowError` (SQLite bind, `models.py:150`), `ValueError` (4300-digit limit, `engine.py:493`), and the 128-role assertion | `evidence/combined/red2-review-tests-vs-rama-v1.txt`, `red2-reasons.txt` |
| **Green**: the full stack from the patch file | all pass | **101 passed, 2 xfailed** on both (review + strict + canary + orphans + ansible_runner + runner_secret + secret_env_scrub) | `evidence/combined/green.txt` |
| **Full suite**: the full stack | no FAILED | **1233 passed, 4 skipped, 2 xfailed, 0 failed** on both (skips: playwright, sshd ×3; xfails: the documented playbook limit) | `evidence/combined/tests/full-combined-py*.txt`, junit in `evidence/combined/ci/` |

## Still open (unchanged)
- **P-0d**: destroy by XO VM id, covering `reconcile.yml`. Required before Phase 9; it will turn the 2 strict xfails into passes.
- **Part B steps 2–5**: on hold until this evidence is accepted. Rotation stays the three secrets.
- No commit, PR or push of code.
