#!/usr/bin/env bash
# macOS launcher — double-clickable from Finder (.command extension).
# Runs the widget detached from any terminal window.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    osascript -e 'display alert "Claude Widget" message "python3 not found. Install it via Homebrew: brew install python" as warning'
    exit 1
fi

nohup "$PY" "$DIR/claude_widget.pyw" > /dev/null 2>&1 &
disown 2>/dev/null || true
