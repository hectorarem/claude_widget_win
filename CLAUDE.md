# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the widget

```powershell
# Preferred: uv reads the PEP 723 inline metadata at the top of the script
uv run --script claude_widget.pyw

# Alternative: install dependencies with pip
pip install -r requirements.txt

# Run directly (shows a console window briefly)
python claude_widget.pyw

# Run silently (no console window) — preferred for normal use
wscript claude_widget.vbs
```

`pywinpty` is required for the Claude PTY integration. `watchdog` is optional — it is only used if `ReadDirectoryChangesW` is unavailable.

## Architecture

The entire application lives in a single file: `claude_widget.pyw`. It is a frameless, always-on-top PyQt6 overlay widget pinned to the bottom-right corner of the primary screen.

### Data sources and refresh cadence

| Data | Source | Interval |
|---|---|---|
| Token counts and cost | `~/.claude/projects/**/*.jsonl` parsed in-process | 15 s + file-change event |
| Session/week usage % | PTY → `claude /usage` | 3 min |
| Context window breakdown | PTY → `claude /context` | 3 min (same worker) |

### Key components

**`_FileWatcher`** — watches `~/.claude/projects/` for `.jsonl` changes. Falls back through three mechanisms in priority order: `ReadDirectoryChangesW` (ctypes, no extra deps) → `watchdog` → QTimer polling. Emits `ClaudeWidget._file_changed` signal (cross-thread safe). Change bursts are debounced to a single stats refresh via an 800 ms `QTimer`.

**`fetch_token_stats()`** — incrementally scans `.jsonl` files under `~/.claude/projects/`: files not modified today are skipped, and for the rest only bytes appended since the last scan are parsed (per-file offsets and totals in `_stats_cache`, reset when the UTC date changes). Filters to today's UTC date, sums `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` from assistant messages, and computes a cost estimate using the `PRICE` dict at the top of the file.

**`_run_claude_command()` / `fetch_usage()` / `fetch_context()`** — spawns a `winpty.PTY` running `cmd.exe /c claude`, waits for the interactive prompt, sends a slash command (`/usage` or `/context`), and reads back the ANSI output. Two separate PTY sessions are used because Claude Code does not reliably accept a second slash command in the same session; `_ClaudeWorker` runs them concurrently. The PTY cwd is passed to `spawn()` rather than set via `os.chdir` so concurrent spawns are thread-safe.

**`_parse_usage()` / `_parse_context()`** — regex parsers that extract structured data from the ANSI-cleaned PTY output.

**`_StatsWorker` / `_ClaudeWorker`** — `QThread` subclasses. Results are emitted via `pyqtSignal` back to the main thread. Workers are skipped (not queued) if a previous run is still in progress.

**`ClaudeWidget`** — main widget. Tab 0 (Usage) shows token stats and session/week progress bars. Tab 1 (Context) shows model info and per-category context window breakdown. Tab switching immediately shows cached data while the background worker runs. The widget hides (rather than closes) when the × button is pressed if the system tray icon is active.

**`_Bar`** — thin custom `QPainter` progress bar, avoiding stylesheet-based flickering.

### Color and style constants

All colors are in the `C` dict; the font is set by `FONT = "Segoe UI"`. Modify these at the top of the file to restyle the widget.

### PTY ready-keyword

`_run_claude_command()` waits for `"for shortcuts"` in the PTY output before sending a slash command — this is the text Claude Code v2.1+ shows in its status bar once the interactive prompt is ready. If a future Claude Code update breaks PTY communication, check this string first.

### Python version compatibility

The file requires `from __future__ import annotations` (first import) to support the `X | Y` union type syntax on Python 3.9. Without it, annotations are evaluated at import time and raise `TypeError` on 3.9.

### Windows-specific notes

- The VBS launcher prefers `uv run --script` when `uv` is on `PATH`, falling back to python. Keep the PEP 723 block in sync with `requirements.txt`.
- The VBS launcher uses `cmd /c start /b python` instead of calling `python` directly. `wscript.exe` cannot resolve Windows Store Python app execution aliases; routing through `cmd.exe` fixes this.
- `ReadDirectoryChangesW` is called via `ctypes` directly to avoid adding `pywin32` as a dependency.
- The entry point hides the console window via `GetConsoleWindow` / `ShowWindow` when run as `.pyw`.
