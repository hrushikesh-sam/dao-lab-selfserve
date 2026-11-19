#!/bin/bash
# home_leftovers.sh HOME SKEL - print how many entries under HOME are not an unchanged copy of
# the same path in SKEL (B-107). A new file, directory or link counts; so does a skel file the
# user edited (.bashrc). A skel file the user deleted does not. Only a count is printed, never
# a name: residue is logged and stored, and file names are the user's data.
# A missing HOME prints 0 (the login was never created, or the folder is already gone).
set -u
home=$1
skel=$2
[ -d "$home" ] || { echo 0; exit 0; }
cd "$home" || exit 1
n=0
while IFS= read -r -d '' p; do
    s="$skel/${p#./}"
    if [ -L "$p" ]; then
        if ! { [ -L "$s" ] && [ "$(readlink -- "$p")" = "$(readlink -- "$s")" ]; }; then n=$((n + 1)); fi
    elif [ -d "$p" ]; then
        if ! { [ -d "$s" ] && [ ! -L "$s" ]; }; then n=$((n + 1)); fi
    else
        if ! { [ -f "$s" ] && [ ! -L "$s" ] && cmp -s -- "$p" "$s"; }; then n=$((n + 1)); fi
    fi
done < <(find . -mindepth 1 -print0)
echo "$n"
