#!/usr/bin/env bash
# Install the Pelops launchd agent so the Telegram bot starts at login
# and restarts on crash. The bot embeds the scheduler (ADR-0002), so
# the standalone scheduler plist is intentionally NOT installed --
# running both against the same persistent jobstore corrupts the
# executor pool.
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

    # Rewrite hardcoded paths to point at THIS checkout AND its sibling
    # vstash-local (which the bot needs on PYTHONPATH because the editable
    # .pth file gets re-hidden by macOS inside .venv).
    python3 - "$src" "$dst" "$ROOT" <<'PY'
import os, re, sys
src, dst, root = sys.argv[1:4]
sibling = os.path.normpath(os.path.join(root, "..", "vstash-local"))
text = open(src).read()
text = re.sub(r"/Users/[^/]+/Desktop/Personal/Projects/stilt", root, text)
text = re.sub(r"/Users/[^/]+/Desktop/Personal/Projects/vstash-local", sibling, text)
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
