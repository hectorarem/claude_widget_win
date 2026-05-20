#!/usr/bin/env bash
# Linux launcher — make executable first: chmod +x claude_widget.sh
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
    echo "Error: python3 not found. Install it with your package manager." >&2
    exit 1
fi

nohup "$PY" "$DIR/claude_widget.pyw" > /dev/null 2>&1 &
disown 2>/dev/null || true
