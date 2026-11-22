# Phase 8 gate, round 2 (Akshay, 22-23 Nov 2026)

Continues the gate run in `../` (base `40ebd6d`). Patch files and evidence for review only: no code is committed.

## Read first
`P-0c-COMPARISON.md`. There are two P-0c patches: Rama's `../P-0c.patch` and Akshay's `P-0c-v2.patch`. Both close row 25.
- Rama's draft passes its own addendum: red reproduced, green 81 passed, full suite 1217 passed / 0 failed.
- Both patches also probed with the same inputs. Rama's `[0-9]+` id leaves an orphan-sweep DoS (**P-0e**): a VM named with a 19+ digit id makes the sweep raise on every tick.
- **Recommendation:** one P-0c. Rama's as the base, with the bounded pattern and the extra regression tests. `P-0c-v2.patch` is reference only.

## Files
| File | What |
|---|---|
| `F-5-1b.patch` + `F-5-1b-NOTES.md` | The secret also leaks through the process environment into `artifacts/<run>/command`; scrub after reading settings. Goes after F-5-1, in either P-0c order. |
| `P-0c-COMPARISON.md` | Rama's P-0c through its addendum, the P-0e finding, and the recommendation |
| `probe_ids.py` | The probe behind the comparison table |
| `P-0c-v2.patch` + `P-0c-NOTES.md` | Akshay's P-0c (reference) |
| `evidence/rama-p0c/` | Red/green, full suite and junit for Rama's P-0c (stack P-0 > F-5-1 > F-5-1b > P-0c); probe outputs |
| `evidence/p0`, `f51`, `m-vm-names`, `p0c`, `f51b`, `tests`, `ci` | The re-run of the round-1 gates, P-0c v2 and F-5-1b runs |
| `evidence/part-a/PART-A-RESULTS.md` | Laptop controller: DB audit exit 0 x2, XO bad names 0/0, F-5-1 exposure (1563 files), facts |

## Status
| Item | State |
|---|---|
| P-0, F-5-1 red/green, C-1 | PASS locally (py3.11, py3.12) |
| Row 25 | fixed on the fake lab by either P-0c; P-0e open in Rama's draft |
| F-5-1b | red 3 failed -> green 4 passed; full suite green with both P-0c variants |
| Production controller | OPEN: none identified |
| Part B 2-5 (laptop remediation) | approved, pending |
| Commit/PR of the code | ON HOLD (review) |

All results are local, scratch or from the laptop controller; none is production evidence.
