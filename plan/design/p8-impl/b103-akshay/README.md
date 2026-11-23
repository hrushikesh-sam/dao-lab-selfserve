# b103-akshay: evidence for B-103 (platform floor, CI matrix, age pin) on `origin/rama` (747d10d)

| File | What |
|---|---|
| `B103-1-python-floor-ci.patch` | Change 1: `requires-python >=3.12`, CI matrix 3.12/3.13 (fail-fast off), `plan/README.md` 3.12+, 3 new tests in `tests/test_platform_floor.py` |
| `B103-2-age-pin.patch` | Change 2: `deploy/age/` (`SHA256SUMS`, `BINARIES.sha256`, `README.md`, `verify-age.sh`) and `tests/test_age_pin.py` |
| `SHA256SUMS.patches` | sha256 of the 2 patches |
| `msg/01-B103-1-python-floor-ci.txt` | Commit message for change 1 |
| `msg/02-B103-2-age-pin.txt` | Commit message for change 2 |
| `B103-1-red-220.txt`, `B103-1-red-221.txt` | Change 1 red, files unchanged: 2 failed, 3 passed (ansible-core 2.20.11 / 2.21.6) |
| `B103-1-green-220.txt`, `B103-1-green-221.txt` | Change 1 green: 5 passed |
| `B103-2-red-220.txt`, `B103-2-red-221.txt` | Change 2 red, `deploy/age/` absent: 3 failed, 7 errors |
| `B103-2-green-220.txt`, `B103-2-green-221.txt` | Change 2 green: 15 passed (`test_age_pin.py` + `test_platform_floor.py`) |
| `full-220.txt`, `full-221.txt` | Full suite, both changes: 1303 passed, 4 skipped, 0 failed on each |
| `age-hashes.txt` | The 6 age v1.1.1 hashes as computed on the laptop (2 tarballs, 4 binaries), with the method |
| `age-cross-check.txt` | Independent cross-check from a different machine and network: same 6 hashes; GitHub API shows no checksum asset and null digests |
| `age-real-verify.txt` | `verify-age.sh` on the real tarballs, both arches, under dash and busybox sh: OK |
| `apply-check.txt` | Fresh `origin/rama` worktree: both patches `--check` ok and applied; TREE MATCH `e5e99c05…`; `diff -r` exit 0; worktree removed |
| `trees.txt` | Index trees: base `76752b51…`, after change 1 `5b3b7bb2…`, after change 2 `e5e99c05…` |
| `venvs.txt` | Python 3.12.13; ansible-core 2.21.6 and 2.20.11; ansible-runner 2.4.3; no local Python 3.13 (CI covers it) |
| `README.md` | This file |

Each patch holds only its own change: they were diffed index tree to index tree (`trees.txt`), so new files are included. `SHA256SUMS` covers every file here except itself: `sha256sum -c SHA256SUMS`.
