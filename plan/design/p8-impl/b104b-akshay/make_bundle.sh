#!/bin/sh
# Build ~/notes/b104b-evidence/ from ~/notes/b104b/build/ (+ discovery files). Fresh folder.
set -eu
B="$HOME/notes/b104b/build"
D="$HOME/notes/b104b"
E="$HOME/notes/b104b-evidence"
[ -e "$E" ] && { echo "refusing: $E already exists"; exit 2; }
mkdir -p "$E/patches" "$E/msg" "$E/discovery"
cp "$B/B104B-m14.patch" "$B/B104B-docs.patch" "$E/patches/"
cp "$B/msg/01-B104B-m14.txt" "$B/msg/02-B104B-docs.txt" "$E/msg/"
for f in \
  red1-221.txt red1-220.txt red2-221.txt red2-220.txt green-221.txt green-220.txt \
  full-221.txt full-220.txt make-test.txt docs-tests-final-221.txt docs-tests-final-220.txt \
  diff-check.txt apply-check.txt tree1.txt tree2.txt \
  doc-vs-code.venv.txt doc-vs-code.venv-220.txt \
  ddl-verbatim-check.venv.txt ddl-verbatim-check.venv-220.txt \
  ddl-check-final.venv.txt ddl-check-final.venv-220.txt ddl-check-final-vs-v4.diff \
  diff-proof-v3-v4.txt diff-proof-v4-v4.1.txt table-count.txt \
  tests-reading-docs.txt doc-path-grep.txt plan08-word-diff.txt \
  interdiff-m14.txt V41-CHANGES.md final-files-restored.txt final-files.sha256 \
  ddl_verbatim_check.py doc_vs_code_check.py make_final_doc.py classify_doc_mentions.py \
  tests-reading-docs.sh run-stage.sh constraints.venv.txt constraints.venv-220.txt
do
  cp "$B/$f" "$E/$f"
done
cp "$D/FINDINGS.md" "$D"/probe_*.py "$D"/probe_*-out.txt "$E/discovery/"
cp "$B/README.md" "$E/BUILD-NOTES.md"
printf '* -text\n' > "$E/.gitattributes"

# Mechanical check: every file named in the msg Evidence lists is in the bundle.
missing=0
for m in "$E/msg/01-B104B-m14.txt" "$E/msg/02-B104B-docs.txt"; do
  names=$(awk '/^Evidence \(branch/{f=1; next} f&&/^$/{f=0} f' "$m" | tr '\n' ' ' \
          | sed -e 's/ and /, /g' -e 's/\. *$//' | tr ',' '\n' | sed -e 's/^ *//' -e 's/ *$//' | grep -v '^$')
  n=0
  for name in $names; do
    n=$((n + 1))
    if [ -e "$E/$name" ]; then echo "  ok      $(basename "$m"): $name"; else echo "  MISSING $(basename "$m"): $name"; missing=$((missing + 1)); fi
  done
  echo "  -> $(basename "$m"): $n named files"
done
echo "missing named files: $missing"
[ "$missing" -eq 0 ]
