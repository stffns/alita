#!/usr/bin/env bash
# Remove the macOS UF_HIDDEN flag from .pth files in the virtualenv.
#
# CPython 3.11+ skips .pth files marked hidden at the filesystem level (not
# just the dotfile convention). macOS sets UF_HIDDEN on files created inside
# directories like .venv, which silently disables editable installs.
#
# Run this after any `uv pip install` or `pip install` that creates or
# replaces .pth files. Safe to run repeatedly.
#
# Usage:
#   bash scripts/fix-pth.sh
#
# To run it automatically after every install, alias:
#   alias uvp='uv pip install $@ && bash scripts/fix-pth.sh'

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ROOT}/.venv"

if [ ! -d "$VENV" ]; then
    echo "no .venv at $VENV" >&2
    exit 1
fi

# Find all .pth files under the venv and unhide them.
count=0
while IFS= read -r -d '' f; do
    chflags nohidden "$f" 2>/dev/null || true
    count=$((count + 1))
done < <(find "$VENV" -name '*.pth' -print0)

# Also drop any "<name> 2.pth" duplicates macOS sometimes leaves behind when
# the same install runs twice.
find "$VENV" -name '* 2.pth' -delete 2>/dev/null || true

echo "unhid $count .pth file(s)"
