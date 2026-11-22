# F-5-1b: the secret still leaks through the process environment (gap in F-5-1)

**Status:** found on Akshay's laptop, 22 Nov 2026. Reproduced against `F-5-1.patch`, fixed, and tested locally. No code is committed: this is a patch file for review.
**Applies on:** `40ebd6d` + `P-0-final.patch` + `F-5-1.patch` + `P-0c-v2.patch`, in that order (it also applies on P-0c v1; it touches only `service/config.py` and a new test).
**`F-5-1b.patch` sha256:** `823a54bcdefd8ae52a6c3226144b93978d25583e5d37adfbc079dd78471c0f5d`

## What was found
Akshay's laptop has run `scripts/run/showcase.sh`: the real ansible runner against the real DPU, with `DAOLAB_DEPLOY_VMS=false`.
Its job directory `var/showcase-jobs/` (17 job dirs, gitignored, never committed) has **1563 files** containing `DAOLAB_INTERNAL_SECRET`. They were counted with `grep -rlF -f <(…)`, so the value was never printed.

| Count | File kind |
|---|---|
| 1540 | `artifacts/<run>/command` |
| 23 | `env/extravars` |

`env/extravars` is what F-5-1 fixes. **`artifacts/<run>/command` is not fixed by F-5-1:**

1. `service/config.py` calls `load_dotenv()`, which copies every `.env` value into `os.environ`. A systemd `EnvironmentFile=/opt/daolab/.env` (plan/08) does the same.
2. ansible-runner 2.4.3 always starts a run's environment from `os.environ.copy()`. In `status_callback('starting')` it writes `{'command', 'env', 'cwd'}` to `artifacts/<ident>/command`. There is no option to turn this off; `suppress_env_files` only covers `env/`.
3. So every playbook run writes the secret to disk, even with F-5-1 applied. Rama's `test_the_real_runner_persists_no_trace_of_the_secret` passes because the test puts the secret into `settings` and never into `os.environ`.

**Reproduction:** with F-5-1 applied, the same real-runner test with `monkeypatch.setenv("DAOLAB_INTERNAL_SECRET", CANARY)` fails: `the secret was persisted in: ['job-5/run-1-deploy/artifacts/deploy/command']`.

The same path also leaks `DAOLAB_AUTHZ_SECRET` and `DAOLAB_TOKEN` (both in `.env`).

## The fix (`service/config.py`, 14 lines)
Right after `settings = Settings.from_env()`, `scrub_secret_env()` pops `DAOLAB_INTERNAL_SECRET`, `DAOLAB_AUTHZ_SECRET` and `DAOLAB_TOKEN` from `os.environ`.

This is safe because:
- **Nothing else reads them.** `grep -rn environ service/`: only `config.py` reads these values.
- **No child process needs them from the environment.** Playbooks read the secret from F-5-1's file. `daolab-xo` gets `DAOLAB_XO_TOKEN_FILE`, a path, which is kept. Sudo uses the become-password file.
- **No restart loses them.** The API runs without `--reload`, with a single worker (plan/08 ExecStart, `dev.sh`), so no re-spawned child is left without them.

## Tests (`tests/test_secret_env_scrub.py`, 4)

| Test | Before | After |
|---|---|---|
| Secrets from the real environment are read into `settings`, then dropped from `os.environ` (fresh interpreter) | FAIL | PASS |
| Same, from a `.env` file through `load_dotenv()` | FAIL | PASS |
| Non-secret settings (`DAOLAB_XO_TOKEN_FILE`) stay in the environment | PASS | PASS |
| Real runner, secret in the environment, scrub applied: no trace anywhere in the job dir | FAIL (`AttributeError`) | PASS |

Together with `test_runner_secret.py`, `test_ansible_runner.py` and `test_config.py`: **33 passed on Python 3.11 and 3.12.**

**Full suite (P-0 + F-5-1 + P-0c v1 + F-5-1b): 1205 passed, 4 skipped, 0 failed on both Pythons.**
**Full suite (P-0 + F-5-1 + P-0c v2 + F-5-1b): 1235 passed, 4 skipped, 2 xfailed, 0 failed on both Pythons** (`evidence/tests/full-v2-py*.txt`).

## Consequences for the gate
- **Production §5.3 step 3 (grep the new job dir) must include `artifacts/*/command`.** It already does: it greps the whole job dir. It would have FAILED with F-5-1 alone, so deploy F-5-1 and F-5-1b together.
- **Rotation (§5.3 step 4):** rotate `DAOLAB_AUTHZ_SECRET` and `DAOLAB_TOKEN` as well as `DAOLAB_INTERNAL_SECRET`. All three were in every old `command` file. `DAOLAB_AUTHZ_SECRET` is also on the DPU (`daolab-keys`), so rotating it means re-deploying it there; plan that.
- **Decision for Rama:** this list is the three secrets in today's `Settings`. Should any new secret setting be added to `SECRET_ENV` by rule? A `*_SECRET`/`*_TOKEN` name-pattern check in CI would enforce that.

## Answers to the review (22 Nov, late)

- **"The CLI could lose a token it reads from the environment."** It can't. `cli.py:27` uses `settings.token`, which is read before the scrub, and the CLI starts no child process that would need it. The CLI tests are in the full suite.
- **`/proc/<pid>/environ`.** Agreed for values systemd put in at exec (`EnvironmentFile=`); the scrub can't change that memory. It is readable only by the same uid or root, and not persisted. A `.env` loaded by `load_dotenv()` was never in the exec environment, so `/proc` is clean in that case. The durable fix is secrets from files (`*_FILE` or systemd `LoadCredential=`). That's a follow-up, not in this patch.
- **"Other values in `command`."** Checked on the laptop, names only plus scheme/host/userinfo/query for URLs. 125 variables. The only secrets are `DAOLAB_INTERNAL_SECRET`, `DAOLAB_AUTHZ_SECRET` and `DAOLAB_TOKEN`. Everything else is a path or a setting:
  - password-file paths: `*_PASSWORD_FILE`;
  - socket or helper paths: `SSH_AUTH_SOCK`, `GIT_ASKPASS`, `VSCODE_GIT_ASKPASS_*`, `XAUTHORITY`;
  - URLs, all local and with no userinfo or query: `DAOLAB_WEBHOOK_URL` (empty), `DAOLAB_PROMETHEUS_URL` and `DAOLAB_API_URL` (localhost), `DAOLAB_XO_URL` (127.0.0.1).

  **Rotation list: those three secrets.**
- **The `user_version` 13 trap.** The guard proposed in the review already exists (`SchemaTooNew`, `migrations.py:24`) and does **not** catch it. v1.1's B-104 goes to 14, so a DB at 13 is not "too new", and the new 12 and 13 would be skipped silently. Proposal:
  - **(a)** burn 12 and 13, so the new B-104 starts at 14 (simplest); or
  - **(b)** record each applied migration's `name` (the `Migration` dataclass has one) and refuse a mismatch at startup.
