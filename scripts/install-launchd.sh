#!/usr/bin/env bash
# Install the Pelops launchd agents so the Telegram bot and the
# (optional) standalone scheduler run automatically at login and
# restart on crash.
#
# Usage:
#   bash scripts/install-launchd.sh           # install + load
#   bash scripts/install-launchd.sh --uninstall
#
# The plists ship with placeholder absolute paths; this script
# rewrites them to point at THIS clone before copying to
# ~/Library/LaunchAgents.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
AGENTS=(
    "com.pelops.telegram"
    "com.pelops.scheduler"
)

mkdir -p "$LAUNCH_DIR"

uninstall() {
    for label in "${AGENTS[@]}"; do
        plist="$LAUNCH_DIR/${label}.plist"
        if [ -f "$plist" ]; then
            echo "unloading $label"
            launchctl unload "$plist" 2>/dev/null || true
            rm -f "$plist"
        fi
    done
    echo "done"
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
    exit 0
fi

# Sanity checks before installing.
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "error: $ROOT/.venv/bin/python is missing." >&2
    echo "       Run 'uv venv && uv pip install -e .' first." >&2
    exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
    echo "warning: $ROOT/.env is missing -- the bot will fail to start." >&2
fi

for label in "${AGENTS[@]}"; do
    src="$ROOT/scripts/${label}.plist"
    dst="$LAUNCH_DIR/${label}.plist"

    if [ ! -f "$src" ]; then
        echo "skipping $label (no plist at $src)"
        continue
    fi

    # Rewrite hardcoded paths to point at THIS checkout.
    python3 - "$src" "$dst" "$ROOT" <<'PY'
import sys, re
src, dst, root = sys.argv[1:4]
text = open(src).read()
# Replace the original hardcoded prefix with the current root.
text = re.sub(
    r"/Users/[^/]+/Desktop/Personal/Projects/stilt",
    root,
    text,
)
open(dst, "w").write(text)
PY

    # Reload (unload first to be safe).
    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
    echo "loaded $label"
done

echo
echo "running agents:"
launchctl list | grep "com.pelops" || true
echo
echo "logs: tail -f $ROOT/data/logs/*.out"
