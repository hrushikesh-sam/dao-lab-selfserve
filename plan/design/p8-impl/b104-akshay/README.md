# B-104 review bundle: migrations 12–14, the reverted-schema guard, and the DDL

**This is a draft for Rama's sign-off. Once approved, it closes E-14 (errata v1.2: "the exact DDL is written with B-104, starting at migration 14").**

It contains documents and synthetic-DB checks only. No repo file is changed, and nothing here touches a real database.

- Base: `origin/rama` `a03f858` (PR #8 merged). Every file:line citation also holds at `747d10d`; the cited files are unchanged between the two.
- The reverted attempt (`4ec115e`) is read only for its schema fingerprint.

## Files

| File | What it is |
|---|---|
| `B-104-DDL.md` | **The draft for sign-off (v3).** §0 Rama's answers; §1 placeholders 12/13; §2 the guard; §3 the DDL for migration 14; §4 interfaces for later tasks; §5 rules check (no secret in any column); §6 test plan; §7 decisions for sign-off (OPEN items marked); §8–§9 B-105/B-107 notes; §10 outside B-104 |
| `FINDINGS.md` | Read-only discovery: how migrations work today, the current DDL, the reverted v12/v13 fingerprint, and the name collisions with the new design |
| `probe.py` | Synthetic in-memory checks behind FINDINGS: the reverted-v13 fingerprint, the silent-skip trap, and the SQLite FK/CHECK behaviour relied on |
| `probe-out.txt` | Output of `probe.py` |
| `effective-ddl-v11.txt` | Effective DDL of `users`, `setups`, `reservations` and `jobs` on a fresh `origin/rama` DB (`user_version` 11) |
| `ddl-check.py` | Parses the `sql` blocks, `add_column` calls and job-kind strings **out of `B-104-DDL.md`**, applies them to a synthetic v11 DB, and exercises the §6.2 cases (CHECKs, the S-shaped canary, holds, two-admin rules, the lost-key reset) |
| `ddl-check-out.txt` | Output of `ddl-check.py` against this `B-104-DDL.md` |
| `B-104-DDL.v1-v2.s3.diff` | v1 → v2 diff of §3 (DDL) |
| `B-104-DDL.v1-v2.s7.diff` | v1 → v2 diff of §7 (open questions → decisions for sign-off) |
| `B-104-DDL.v2-v3.diff` | v2 → v3 diff of §3.3, §3.6 and §7 Q-6 (a lost-key reset only starts a 24h hold; the key stays active and the owner can cancel) |
| `SHA256SUMS` | sha256 of every file above and this README |

## How to verify

Run from the root of a clone of `dao-lab-selfserve` with `origin` fetched. Pick any empty scratch directory as `$PROBE`, and set `$BUNDLE` to this folder.

**1. Check the bundle's integrity.**

```sh
(cd "$BUNDLE" && sha256sum -c SHA256SUMS)       # expect every line OK
```

**2. Rebuild the probe directory from git objects.** This needs no checkout.

```sh
mkdir -p "$PROBE/rama" "$PROBE/rev"
git show origin/rama:service/migrations.py > "$PROBE/rama/mig_rama.py"
git show origin/rama:service/schema.sql    > "$PROBE/rama/schema.sql"
git show 4ec115e:service/migrations.py     > "$PROBE/rev/mig_rev.py"     # only probe.py needs this
```

**3. Run the checks and compare.** Both use in-memory SQLite only, and `-I` keeps Python from importing anything from the current directory.

```sh
python3 -I "$BUNDLE/ddl-check.py" "$PROBE" "$BUNDLE/B-104-DDL.md" > "$PROBE/ddl-check-out.txt" 2>&1
echo "exit=$?"                                                        # expect exit=0
diff "$BUNDLE/ddl-check-out.txt" "$PROBE/ddl-check-out.txt" && echo "ddl-check output identical"

python3 -I "$BUNDLE/probe.py" "$PROBE" > "$PROBE/probe-out.txt" 2>&1
diff "$BUNDLE/probe-out.txt" "$PROBE/probe-out.txt" && echo "probe output identical"
```

**What to expect:**
- Identical output on SQLite 3.53.1, the version used here; `probe.py`'s first line prints it.
- On another SQLite version, the accepted/refused verdicts should match. The wording of the constraint messages may differ.
- If `origin/rama`'s `service/migrations.py` or `service/schema.sql` changes after `a03f858`, re-check the citations before relying on the results.
