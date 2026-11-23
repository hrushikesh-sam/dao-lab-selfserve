# round-3-akshay: evidence for the 8-patch PR stack on `origin/rama` (40ebd6d)

| Folder | What |
|---|---|
| `rama-pr/` | The PR stack: the 8 patches, their commit messages (`msg/NN-*.txt`), `SERIES.md`, the G-1..G-3 red/green runs, the full suite on ansible-core 2.21.6 and 2.20.11 (`full-221.txt`, `full-220.txt`: 1290 passed, 4 skipped, 0 failed) and the fresh-tree `apply-check.txt` |
| `p0c-combined/` | The combined P-0c patch, notes and evidence (`evidence/combined/`), from `p0c-combined.zip` (sha256 `c0875b8f…`) |
| `p0d/` | P-0d: patch, notes, red/green and canary runs on 2.21.6 and 2.20.11, full suite, apply check |
| `akshay-gate/` | The `akshay` branch with the 4 gate patches + P-0d: the full-suite run, the base round-trip failure (sudo needs a password) and `P-0d-on-akshay.patch` |
| `akshay-fix/` | A-1: the `real_runner()` become fix (tests only), with its sha256, red/green on `akshay-base` and `akshay-gate`, and the before/after state of `akshay-gate` |

Each folder that has a `SHA256SUMS` can be checked on its own. `SHA256SUMS` at the top covers every file here: `sha256sum -c SHA256SUMS`.
