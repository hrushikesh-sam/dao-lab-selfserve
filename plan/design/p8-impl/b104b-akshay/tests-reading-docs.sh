#!/bin/sh
# Which tests read README.md or plan/** ? Prints each exact command, its output and exit code.
# Run from ~/b104b. Output: ~/notes/b104b/build/tests-reading-docs.txt
cd "$HOME/b104b" || exit 2
run() {
  printf '$ %s\n' "$1"
  sh -c "$1"
  printf '[exit %s]\n\n' "$?"
}
echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)  HEAD: $(git rev-parse HEAD)  staged tree: $(git write-tree)"
echo "# Q: which tests read README.md or plan/** ? (git grep exit 1 = searched, nothing found)"
echo
echo "## 1. code that opens such a file"
run "git grep -n -E \"(open|read_text|join|Path)\\(.*(README|['\\\"]plan['\\\"]|plan/)|README\\.md['\\\"]|['\\\"]plan['\\\"]\" -- tests/"
echo "## 2. any '.md' reference, then the same with docstring/comment lines removed"
run "git grep -n -E '\\.md\\b' -- tests/"
run "git grep -n -E '\\.md\\b' -- tests/ | grep -v -E '\"\"\"|^\\S+:\\s*#'"
echo "## 3. every mention of README in tests/"
run "git grep -n -i -E 'readme' -- tests/"
echo "## 4. every 'plan/' mention in tests/ (all docstrings/comments citing plan sections)"
run "git grep -n -E 'plan/' -- tests/ | wc -l"
run "git grep -n -E 'plan/' -- tests/"
echo "## 5. classification of every plan/ and README mention, by file:line (tokenizer-based; columns: file:line, match, token kind, verdict)"
run "cd /tmp && $HOME/b104b/.venv/bin/python -I $HOME/notes/b104b/build/classify_doc_mentions.py $HOME/b104b"
echo "## Result: no test opens README.md or any plan/** file. The hits in sections 2 and 4 are docstrings or comments citing docs."
echo "## test_api.py:88 counts seeded catalog rows (15 README rows + diy + smartnic) in the DB, not README.md."
