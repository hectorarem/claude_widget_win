#!/usr/bin/env bash
# macOS launcher — double-clickable from Finder (.command extension).
# Runs the widget detached from any terminal window.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer uv: resolves deps from the script's inline metadata into a cached env
PY=""
for candidate in uv python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    osascript -e 'display alert "Claude Widget" message "neither uv nor python3 found. Install it via Homebrew: brew install python" as warning'
    exit 1
fi

if [ "$PY" = "uv" ]; then
    nohup uv run --script "$DIR/claude_widget.pyw" > /dev/null 2>&1 &
else
    nohup "$PY" "$DIR/claude_widget.pyw" > /dev/null 2>&1 &
fi
disown 2>/dev/null || true
