# claude_widget

A lightweight, always-on-top overlay that monitors your Claude Code usage in real time. It reads your local Claude session files and queries the Claude CLI directly to show token consumption, cost estimates, and context window breakdown — all without leaving your workflow.

![Widget tabs: Usage and Context](https://github.com/hectorarem/claude_widget_win/blob/main/image.png)

---

## Requirements

| Requirement | Notes |
|---|---|
| **Python 3.9+** | Must be on your `PATH` |
| **Claude Code CLI** | `claude` command must be accessible from any terminal |
| **PyQt6 ≥ 6.7** | Main UI framework |
| **pywinpty 3.x** | PTY sessions for `/usage` and `/context` (Windows only) |
| **watchdog ≥ 6.0** | File watcher fallback (required on macOS/Linux; optional on Windows) |

### Platform notes

| Platform | File watcher | PTY |
|---|---|---|
| **Windows 10/11** | `ReadDirectoryChangesW` (native, no extra deps) → watchdog fallback | `pywinpty` |
| **macOS** | watchdog | Not yet supported — Usage/Context bars show "no data" |
| **Linux** | watchdog | Not yet supported — Usage/Context bars show "no data" |

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/hectorarem/claude_widget_win.git
cd claude_widget_win
```

**2. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**3. Verify Claude Code is installed and authenticated**

```bash
claude --version
```

The widget spawns `claude` in a PTY session. It must be reachable from any working directory without additional setup.

---

## Running

### Windows

**Silent (no console window) — recommended**

Double-click `claude_widget.vbs`, or run from PowerShell:

```powershell
wscript claude_widget.vbs
```

**Direct (console visible briefly)**

```powershell
python claude_widget.pyw
```

### macOS

Make the launcher executable once, then double-click it from Finder:

```bash
chmod +x claude_widget.command
```

Or run from a terminal:

```bash
./claude_widget.command
```

### Linux

Make the launcher executable once, then run it:

```bash
chmod +x claude_widget.sh
./claude_widget.sh
```

---

The widget appears in the **bottom-right corner** of your primary monitor. It stays on top of other windows and does not appear in the taskbar.

---

## Usage

### Moving the widget

Click and drag the **title bar** to reposition the widget anywhere on screen.

### Tabs

**Usage tab** — updated every 15 seconds (also on file change):

- Input and output token counts for today (UTC)
- Cache write and cache read token counts
- Estimated cost in USD
- Session and weekly usage percentage bars with reset times

**Context tab** — updated every 3 minutes via Claude CLI:

- Active model name and ID
- Total tokens used vs. context window maximum
- Per-category breakdown: system prompt, system tools, MCP tools, memory files, skills, messages, free space, autocompact buffer

### System tray icon

A purple **C** icon appears in the system tray. Left-clicking it toggles the widget's visibility. Right-clicking opens a menu with:

- **Bring to front** — shows and focuses the widget
- **Refresh** — triggers an immediate update of all data
- **Add to startup / Remove from startup** — toggles whether the widget launches automatically at login (Windows only, see below)
- **Quit** — exits the application

### Right-click menu on the widget

- **Refresh stats** — re-scans JSONL files immediately
- **Refresh Claude** — re-runs `/usage` and `/context` via PTY immediately

### Closing

The **×** button hides the widget (it keeps running in the tray). To fully exit, use **Quit** from the tray icon menu.

---

## How it works

The widget has two independent data pipelines:

1. **JSONL scanner** — reads `~/.claude/projects/**/*.jsonl` directly in-process. Filters to today's UTC date and aggregates token usage from assistant messages. Triggered every 15 s and immediately on any file change detected via `ReadDirectoryChangesW` (Windows) or watchdog (macOS/Linux).

2. **PTY sessions** — spawns two `winpty` PTY processes running `cmd.exe /c claude`, sends `/usage` and `/context`, parses the ANSI output, and closes the session. Runs every 3 minutes. Currently Windows-only; on macOS/Linux the Usage and Context bars remain at their last known value.

All background work runs in `QThread` workers and communicates to the UI via `pyqtSignal` — the UI never blocks. Cached results are shown instantly on tab switch while a background refresh runs.

---

## Auto-start on login

### Windows

The easiest way is via the tray icon menu: right-click the **C** icon → **Add to startup**. This writes an entry to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` pointing to `claude_widget.vbs`. To undo it, right-click → **Remove from startup**.

Alternatively, manually place a shortcut to `claude_widget.vbs` in your Startup folder (`Win + R` → `shell:startup`).

### macOS

Add `claude_widget.command` to **System Settings → General → Login Items**.

### Linux

Add the following line to your `~/.config/autostart/claude_widget.desktop` file (create it if it does not exist):

```ini
[Desktop Entry]
Type=Application
Name=Claude Widget
Exec=/path/to/claude_widget.sh
Hidden=false
X-GNOME-Autostart-enabled=true
```

Or add `bash /path/to/claude_widget.sh &` to your shell profile (`~/.bashrc`, `~/.profile`, etc.).

---

## Troubleshooting

**Usage / Context tabs show "no data"**
- Confirm `claude` is on your `PATH`: open a new terminal and run `claude --version`.
- Confirm `pywinpty` is installed: `pip show pywinpty`.
- The PTY session takes up to ~30 seconds on first load. Data appears after the first 3-minute cycle or immediately after a manual **Refresh** from the tray icon.
- On macOS and Linux, PTY sessions are not supported — these tabs will always show "no data".

**VBS launcher does nothing (Windows Store Python)**
- Windows Store Python stubs are not resolved by `wscript.exe`. The included VBS uses `cmd /c start /b python` to work around this. If it still fails, run `python claude_widget.pyw` directly from a terminal to see any error output.

**Token counts are zero**
- Confirm Claude Code has been used today. Data is read from `~/.claude/projects/`.
- On Windows, confirm the path exists: `ls $env:USERPROFILE\.claude\projects`.

**Widget does not appear**
- It launches in the bottom-right corner of the primary monitor. If you have multiple monitors, check the primary one.
- Use the system tray icon → **Bring to front**.

**macOS: `.command` file won't open**
- macOS may block unsigned scripts. Right-click the file → **Open** → confirm in the dialog, or run `chmod +x claude_widget.command` in a terminal first.
