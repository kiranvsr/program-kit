#!/bin/sh
# install.sh: copy the program-kit kit/ directory into a git repository.
#
#   install.sh <target-repo> [--preset go|node|python] [--upgrade]
#
# Fresh install copies kit/ and, when program/ is absent, runs `pk init`.
# --upgrade replaces kit/ wholesale after checking it has no local changes;
# program/ is never read, written or listed.
set -eu

usage='usage: install.sh <target-repo> [--preset go|node|python] [--upgrade]'

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

target_arg=''
preset=''
upgrade=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --preset)
            [ "$#" -ge 2 ] || fail "--preset needs a value (go, node or python); $usage"
            preset=$2
            shift 2
            ;;
        --upgrade)
            upgrade=1
            shift
            ;;
        -*)
            fail "unknown option: $1; $usage"
            ;;
        *)
            [ -z "$target_arg" ] || fail "exactly one target is allowed; $usage"
            target_arg=$1
            shift
            ;;
    esac
done

[ -n "$target_arg" ] || fail "a target repository is required; $usage"
case "$preset" in
    '' | go | node | python) ;;
    *) fail "unknown preset: $preset (expected go, node or python)" ;;
esac

# Where the kit comes from: the directory this script lives in, whatever the working directory.
source_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || fail 'cannot resolve the location of install.sh'
source_kit="$source_root/kit"
[ -d "$source_kit" ] || fail "kit directory not found at $source_kit"
[ -f "$source_kit/VERSION" ] || fail "$source_kit/VERSION not found"
version=$(cat "$source_kit/VERSION")
[ -n "$version" ] || fail "$source_kit/VERSION is empty"

# The target: an existing directory that is the top level of a git repository.
[ -d "$target_arg" ] || fail "$target_arg is not a directory"
target=$(CDPATH= cd -- "$target_arg" && pwd -P) || fail "cannot enter $target_arg"
command -v git >/dev/null 2>&1 || fail 'git was not found on PATH'
top=$(git -C "$target" rev-parse --show-toplevel 2>/dev/null) || fail "$target is not inside a git repository"
[ "$top" = "$target" ] || fail "$target is not the top level of a git repository (top level is $top)"
[ "$target" != "$source_root" ] || fail "refusing to install into the kit's own repository ($target)"

# Python 3.11 or newer is what the kit scripts need.
command -v python3 >/dev/null 2>&1 || fail 'python3 3.11 or newer is required on PATH'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 ||
    fail 'python3 3.11 or newer is required on PATH'
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

# The suffix of the two work directories is this process id. PK_INSTALL_PID overrides it (digits only) so the
# tests can plant a clashing directory; a clash is always an error, never reused.
case "${PK_INSTALL_PID:-}" in
    '' | *[!0-9]*) suffix=$$ ;;
    *) suffix=$PK_INSTALL_PID ;;
esac
kit_dir="$target/kit"
tmp_dir="$target/.kit.tmp.$suffix"
old_dir="$target/.kit.old.$suffix"
tmp_made=0
old_made=0

# Removes only the two directories this run created (each flag is set right after its mkdir succeeded). The old
# kit lives at "$old_dir/kit"; if an interrupt lands between the two renames of an upgrade it is put back first.
cleanup() {
    trap - EXIT INT TERM HUP
    if [ "$old_made" = 1 ] && [ -d "$old_dir/kit" ] && [ ! -e "$kit_dir" ] && [ ! -L "$kit_dir" ]; then
        mv "$old_dir/kit" "$kit_dir" || printf 'error: could not restore the previous kit; it is at %s\n' "$old_dir/kit" >&2
    fi
    if [ "$tmp_made" = 1 ] && [ -d "$tmp_dir" ]; then
        rm -rf "$tmp_dir"
    fi
    if [ "$old_made" = 1 ] && [ -d "$old_dir" ] && { [ -e "$kit_dir" ] || [ -L "$kit_dir" ]; }; then
        rm -rf "$old_dir"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ "$upgrade" = 0 ]; then
    if [ -e "$kit_dir" ] || [ -L "$kit_dir" ]; then
        fail "$kit_dir already exists; use --upgrade"
    fi
else
    if [ -L "$kit_dir" ] || [ ! -d "$kit_dir" ]; then
        fail "$kit_dir does not exist (or is not a directory); nothing to upgrade"
    fi
    # The installed kit is verified with the SOURCE kit's manifest module against the installed MANIFEST.sha256, in
    # isolated mode with no bytecode, so nothing under the target is imported or run.
    problems=$(python3 -I -B -c '
import sys
sys.path.insert(0, sys.argv[1])
import manifest
from common import KitError
try:
    found = manifest.check(sys.argv[2])
except KitError as exc:
    print("error: " + " ".join(str(exc).split()))
    sys.exit(1)
for line in found:
    print("error: " + line)
sys.exit(1 if found else 0)
' "$source_kit/scripts" "$kit_dir" 2>&1) || {
        printf '%s\n' "$problems" >&2
        fail "$kit_dir does not match its manifest; refusing to upgrade"
    }
    # Claim the directory for the old kit now: mkdir fails on any existing path, including a symlink.
    mkdir "$old_dir" || fail "cannot create $old_dir (it may already exist)"
    old_made=1
fi

# Copy the kit to a temporary sibling, without caches.
if [ -n "$(find "$source_kit" -type l)" ]; then
    fail "$source_kit contains a symbolic link; the kit ships none"
fi
dirs=$(CDPATH= cd -- "$source_kit" && find . -name __pycache__ -prune -o -type d -print)
files=$(CDPATH= cd -- "$source_kit" && find . -name __pycache__ -prune -o -type f ! -name '*.pyc' ! -name .DS_Store -print)
mkdir "$tmp_dir" || fail "cannot create $tmp_dir"
tmp_made=1
newline='
'
IFS=$newline
set -f
for entry in $dirs; do
    mkdir -p "$tmp_dir/$entry" || fail "cannot create $tmp_dir/$entry"
done
for entry in $files; do
    cp -p "$source_kit/$entry" "$tmp_dir/$entry" || fail "cannot copy $entry"
done
set +f
unset IFS

if [ "$upgrade" = 0 ]; then
    # mv would move the directory INTO an existing kit_dir, so test again right before the rename. A kit created
    # by someone else in the instant between this test and the mv is a race this script cannot close.
    if [ -e "$kit_dir" ] || [ -L "$kit_dir" ]; then
        fail "$kit_dir appeared during the install; use --upgrade"
    fi
    mv "$tmp_dir" "$kit_dir" || fail "cannot move $tmp_dir to $kit_dir"
    tmp_made=0
else
    mv "$kit_dir" "$old_dir/kit" || fail "cannot move $kit_dir aside"
    if ! mv "$tmp_dir" "$kit_dir"; then
        mv "$old_dir/kit" "$kit_dir" || fail "cannot install the new kit and cannot restore the old one (at $old_dir/kit)"
        fail "cannot move $tmp_dir to $kit_dir; the previous kit was restored"
    fi
    tmp_made=0
    rm -rf "$old_dir"
    old_made=0
fi

# program/ is the project's: the only thing asked of it is whether it exists.
if [ -e "$target/program" ] || [ -L "$target/program" ]; then
    printf 'program/ exists; left untouched\n'
else
    if [ -n "$preset" ]; then
        (CDPATH= cd -- "$target" && python3 "$kit_dir/scripts/pk.py" init --preset "$preset") ||
            fail "kit installed, but pk init failed"
    else
        (CDPATH= cd -- "$target" && python3 "$kit_dir/scripts/pk.py" init) || fail "kit installed, but pk init failed"
    fi
fi

printf 'installed program-kit %s into %s\n' "$version" "$target"
