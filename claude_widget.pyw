#!/usr/bin/env python3
"""
Claude Usage Widget — PyQt6 overlay for Windows.

Improvements over the tkinter version:
- PyQt6: per-pixel alpha, proper rounded card, no UI freeze
- Single PTY session for /usage + /context (halves startup cost)
- ReadDirectoryChangesW (ctypes, no deps) → watchdog → QTimer fallback
- QThread + pyqtSignal: thread-safe, no tkinter after() stacking
- Cached results shown instantly on tab switch while refresh runs in background
"""
from __future__ import annotations
import ctypes, json, os, re, subprocess, sys, time
if sys.platform == "win32":
    import ctypes.wintypes as wt
    import winreg
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Event

from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu,
    QSizePolicy, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt, QRect, QTimer, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap

try:
    import winpty
    _WINPTY = True
except ImportError:
    _WINPTY = False

# ── Startup (registry — Windows only) ────────────────────────────────────────
if sys.platform == "win32":
    _RUN_KEY      = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_NAME     = "ClaudeWidget"
    _VBS_LAUNCHER = Path(sys.argv[0]).resolve().parent / "claude_widget.vbs"

def _startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _RUN_NAME)
            return True
    except OSError:
        return False

def _set_startup(enable: bool) -> None:
    if sys.platform != "win32":
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                        access=winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ,
                              f'wscript.exe "{_VBS_LAUNCHER}"')
        else:
            try:
                winreg.DeleteValue(k, _RUN_NAME)
            except OSError:
                pass


# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_DIR   = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
GEOM_FILE    = CLAUDE_DIR / "claude_widget_geom.json"

RESIZE_GRIP_PX = 16   # bottom-right hit-zone for resizing
MIN_W, MIN_H   = 180, 200

PRICES = {
    "opus":   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "haiku":  {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_read": 0.10},
}
PRICE = PRICES["sonnet"]  # default fallback / preserves existing flat-rate behavior


def _price_for(model_id: str) -> dict:
    mid = (model_id or "").lower()
    for k in ("opus", "sonnet", "haiku"):
        if k in mid:
            return PRICES[k]
    return PRICE


def _model_display(model_id: str) -> str:
    if not model_id:
        return "Unknown"
    m = re.match(r"claude-(opus|sonnet|haiku)-(\d+)-?(\d+)?", model_id, re.I)
    if m:
        ver = f"{m.group(2)}.{m.group(3)}" if m.group(3) else m.group(2)
        return f"{m.group(1).title()} {ver}"
    return model_id

REFRESH_STATS_MS  = 15_000   # JSONL re-scan interval (also fired by file watcher)
REFRESH_CLAUDE_MS = 180_000  # /usage + /context PTY interval

FONT = "Segoe UI" if sys.platform == "win32" else ".AppleSystemUIFont" if sys.platform == "darwin" else "Noto Sans"

C = {
    "bg":      "#0d0d1a",
    "bg2":     "#14142a",
    "accent":  "#b57bee",
    "teal":    "#26c6da",
    "text":    "#d0d0e0",
    "dim":     "#55557a",
    "green":   "#4caf50",
    "bar_bg":  "#1a1a30",
    "sep":     "#1e1e3a",
}


# ── File watching ─────────────────────────────────────────────────────────────
# Priority: ReadDirectoryChangesW (native, no deps) → watchdog → polling via QTimer

class _FileWatcher:
    """
    Calls `on_change()` whenever a .jsonl file in `path` is created or written.
    Uses ReadDirectoryChangesW on Windows (kernel notification, not polling).
    Falls back to watchdog, then sets method="polling" for QTimer-based fallback.
    """

    def __init__(self, path: Path, on_change):
        self._path      = path
        self._on_change = on_change
        self._stop      = Event()
        self.method     = "none"
        self._handle    = None

    def start(self):
        if sys.platform == "win32":
            try:
                self._start_rdcw()
                return
            except Exception:
                pass
        try:
            self._start_watchdog()
            return
        except ImportError:
            pass
        self.method = "polling"

    def stop(self):
        self._stop.set()
        # Unblock any pending ReadDirectoryChangesW
        if self._handle is not None:
            try:
                ctypes.windll.kernel32.CancelIoEx(self._handle, None)
            except Exception:
                pass
        if hasattr(self, "_wdog_obs"):
            try:
                self._wdog_obs.stop()
            except Exception:
                pass

    def _start_rdcw(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype         = wt.HANDLE
        k.ReadDirectoryChangesW.restype = wt.BOOL

        FILE_LIST_DIRECTORY        = 0x0001
        FILE_SHARE_ALL             = 0x0007
        OPEN_EXISTING              = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_NOTIFY_CHANGE_WRITE   = 0x00000010
        FILE_NOTIFY_CHANGE_NAME    = 0x00000001

        handle = k.CreateFileW(
            str(self._path), FILE_LIST_DIRECTORY, FILE_SHARE_ALL,
            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        # INVALID_HANDLE_VALUE = all bits set (pointer-width)
        _invalid = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        if handle is None or handle == _invalid:
            raise ctypes.WinError(ctypes.get_last_error())

        self._handle = handle
        self.method  = "ReadDirectoryChangesW"

        buf      = ctypes.create_string_buffer(65536)
        returned = wt.DWORD(0)

        def _watch():
            try:
                while not self._stop.is_set():
                    ok = k.ReadDirectoryChangesW(
                        handle, buf, len(buf), True,
                        FILE_NOTIFY_CHANGE_WRITE | FILE_NOTIFY_CHANGE_NAME,
                        ctypes.byref(returned), None, None,
                    )
                    if ok and not self._stop.is_set():
                        self._on_change()
            finally:
                k.CloseHandle(handle)

        Thread(target=_watch, daemon=True).start()

    def _start_watchdog(self):
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        cb = self._on_change

        class _H(FileSystemEventHandler):
            def on_modified(self, e):
                if not e.is_directory and e.src_path.endswith(".jsonl"):
                    cb()
            def on_created(self, e):
                if not e.is_directory and e.src_path.endswith(".jsonl"):
                    cb()

        obs = Observer()
        obs.schedule(_H(), str(self._path), recursive=True)
        obs.start()
        self._wdog_obs = obs
        self.method    = "watchdog"


# ── Token stats ───────────────────────────────────────────────────────────────

def fetch_token_stats() -> dict:
    today = datetime.now(timezone.utc).date()
    inp = out = cw = cr = 0
    sessions_today: set = set()
    active_sessions: set = set()
    by_model: dict[str, dict] = {}

    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d   = json.loads(f.read_text(encoding="utf-8"))
            sid = d.get("sessionId", "")
            if sid:
                active_sessions.add(sid)
        except Exception:
            pass

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(jsonl, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    try:
                        dt = datetime.fromisoformat(
                            entry.get("timestamp", "").replace("Z", "+00:00")
                        )
                        if dt.date() != today:
                            continue
                    except Exception:
                        continue
                    sessions_today.add(entry.get("sessionId", ""))
                    msg  = entry.get("message", {}) or {}
                    u    = msg.get("usage", {}) or {}
                    e_in = u.get("input_tokens", 0)
                    e_ou = u.get("output_tokens", 0)
                    e_cw = u.get("cache_creation_input_tokens", 0)
                    e_cr = u.get("cache_read_input_tokens", 0)
                    inp += e_in; out += e_ou; cw += e_cw; cr += e_cr

                    mid = msg.get("model", "") or "unknown"
                    bm  = by_model.setdefault(
                        mid, {"inp": 0, "out": 0, "cw": 0, "cr": 0}
                    )
                    bm["inp"] += e_in; bm["out"] += e_ou
                    bm["cw"]  += e_cw; bm["cr"]  += e_cr
        except Exception:
            pass

    # Cost per model (uses model-specific pricing); global cost = sum of those.
    for mid, bm in by_model.items():
        pr = _price_for(mid)
        bm["cost"] = (
            bm["inp"] * pr["input"] + bm["out"] * pr["output"]
            + bm["cw"] * pr["cache_write"] + bm["cr"] * pr["cache_read"]
        ) / 1_000_000
        bm["display"] = _model_display(mid)

    cost = sum(bm["cost"] for bm in by_model.values()) if by_model else (
        inp * PRICE["input"] + out * PRICE["output"]
        + cw * PRICE["cache_write"] + cr * PRICE["cache_read"]
    ) / 1_000_000

    return dict(
        inp=inp, out=out, cw=cw, cr=cr, cost=cost,
        sessions=len(sessions_today),
        active=len(sessions_today & active_sessions),
        by_model=by_model,
    )


# ── Claude PTY ────────────────────────────────────────────────────────────────

def _clean_ansi(text: str) -> str:
    text = re.sub(r"\x1b\[\d+;\d*H", " ", text)
    text = re.sub(r"\x1b\[[0-9;?]*[mGKHFJA-Za-z]", "", text)
    text = re.sub(r"\x1b[()][A-Z0-9]|\r|\x1b=|\x1b>", "", text)
    return text


def _pty_read_until(pty, keyword: str, timeout: float) -> tuple[str, bool]:
    buf = ""; deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = pty.read(blocking=False)
        if chunk:
            buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
            if keyword in buf:
                return buf, True
        time.sleep(0.1)
    return buf, False


def _pty_read_until_any(pty, keywords: list[str], timeout: float) -> tuple[str, bool]:
    buf = ""; deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = pty.read(blocking=False)
        if chunk:
            buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
            if any(kw in buf for kw in keywords):
                return buf, True
        time.sleep(0.1)
    return buf, False


def _pty_read_for(pty, secs: float) -> str:
    buf = ""; deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        chunk = pty.read(blocking=False)
        if chunk:
            buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
        time.sleep(0.1)
    return buf


def _parse_usage(text: str) -> dict:
    r = {"session_pct": None, "session_reset": "", "week_pct": None, "week_reset": ""}
    pcts = re.findall(r"(\d+)%", text)
    if len(pcts) >= 1: r["session_pct"] = int(pcts[0])
    if len(pcts) >= 2: r["week_pct"]    = int(pcts[1])
    reset_re = re.compile(
        r"Re[set]+s?\s*((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\w*\s*\d+\s*,?\s*)?\d+(?::\d+)?\s*(?:am|pm))(?:\s*\(([^)]+)\))?",
        re.IGNORECASE,
    )
    resets = reset_re.findall(text)
    if len(resets) >= 1:
        t, tz = resets[0]; r["session_reset"] = t.strip() + (f" ({tz})" if tz else "")
    if len(resets) >= 2:
        t, tz = resets[1]; r["week_reset"]    = t.strip() + (f" ({tz})" if tz else "")
    return r


def _parse_context(text: str) -> dict:
    r = {"model": "", "model_id": "", "total_tokens": "",
         "total_max": "", "total_pct": None, "categories": {}}
    m = re.search(r"(Sonnet|Opus|Haiku)\s*([\d.]+)", text, re.IGNORECASE)
    if m: r["model"] = f"{m.group(1).title()} {m.group(2)}"
    m = re.search(r"(claude-[a-z0-9-]+)", text, re.IGNORECASE)
    if m: r["model_id"] = m.group(1)
    m = re.search(
        r"([\d.]+\s*[kKmM]?)\s*/\s*([\d.]+\s*[kKmM]?)\s*tokens?\s*\((\d+(?:\.\d+)?)%\)",
        text,
    )
    if m:
        r["total_tokens"] = m.group(1).strip()
        r["total_max"]    = m.group(2).strip()
        r["total_pct"]    = float(m.group(3))
    for key, pat in [
        ("system_prompt", r"System\s*prompt"),
        ("system_tools",  r"System\s*tools"),
        ("mcp_tools",     r"MCP\s*tools"),
        ("memory_files",  r"Memory\s*files"),
        ("skills",        r"Skills"),
        ("messages",      r"Messages"),
        ("free_space",    r"Free\s*space"),
        ("autocompact",   r"Autocompact\s*buffer"),
    ]:
        m = re.search(
            pat + r"[:\s]+([\d.]+\s*[kKmM]?)\s*(?:tokens?)?\s*\((\d+(?:\.\d+)?)%\)",
            text, re.IGNORECASE,
        )
        if m:
            r["categories"][key] = {"tokens": m.group(1).strip(), "pct": float(m.group(2))}
    return r


# ── PTY abstraction (Windows: winpty  |  macOS/Linux: pty + subprocess) ───────

class _WinPty:
    def __init__(self):
        self._p = winpty.PTY(220, 50)
        self._p.spawn("cmd.exe /c claude")

    def read(self, blocking=False) -> str:
        chunk = self._p.read(blocking=blocking)
        if not chunk:
            return ""
        return chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")

    def write(self, text: str) -> None:
        self._p.write(text)

    def close(self) -> None:
        try:
            self._p.write("/exit\r"); time.sleep(0.3)
        except Exception:
            pass


class _UnixPty:
    def __init__(self):
        import pty, select
        master, slave = pty.openpty()
        self._master = master
        self._select = select
        self._proc   = subprocess.Popen(
            ["claude"],
            stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, cwd=str(Path.home()),
        )
        os.close(slave)

    def read(self, blocking=False) -> str:
        timeout = None if blocking else 0
        r, _, _ = self._select.select([self._master], [], [], timeout)
        if r:
            try:
                return os.read(self._master, 4096).decode("utf-8", errors="replace")
            except OSError:
                return ""
        return ""

    def write(self, text: str) -> None:
        os.write(self._master, text.encode())

    def close(self) -> None:
        try:
            self.write("/exit\r"); time.sleep(0.3)
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass
        try:
            os.close(self._master)
        except Exception:
            pass


def _make_pty():
    if sys.platform == "win32":
        return _WinPty() if _WINPTY else None
    try:
        return _UnixPty()
    except Exception:
        return None


def _run_claude_command(cmd: str, finish_keyword: str,
                        finish_timeout: float) -> str | None:
    """
    Spawn a fresh claude PTY, send `cmd`, wait until `finish_keyword` appears
    (or `finish_timeout` elapses) and return the cleaned output.

    A new PTY is used per command because Claude Code does not reliably accept
    a second slash command in the same interactive session.
    """
    if sys.platform == "win32":
        old_cwd = os.getcwd()
        os.chdir(str(Path.home()))

    pty = _make_pty()

    if sys.platform == "win32":
        try:
            os.chdir(old_cwd)
        except Exception:
            pass

    if pty is None:
        return None

    try:
        _, has_trust = _pty_read_until(pty, "trust", timeout=6)
        if has_trust:
            pty.write("\r")

        _, ready = _pty_read_until_any(
            pty,
            ["for shortcuts", "bypass permissions",
             "shift+tab", "for agents", "auto mode"],
            timeout=20,
        )
        if not ready:
            return None

        pty.write(f"{cmd}\r")
        out, done = _pty_read_until(pty, finish_keyword, timeout=finish_timeout)
        if done:
            out += _pty_read_for(pty, 1.5)

        return _clean_ansi(out)
    except Exception:
        return None
    finally:
        pty.close()


def fetch_claude_data() -> tuple[dict | None, dict | None]:
    """
    Fetch /usage and /context using two independent PTY sessions.

    Two sessions are needed because Claude Code does not reliably handle a
    second slash command sent in the same interactive session.
    """
    usage_text = _run_claude_command("/usage",   "Resets",     finish_timeout=12)
    ctx_text   = _run_claude_command("/context", "Autocompact", finish_timeout=25)

    usage = _parse_usage(usage_text)     if usage_text else None
    ctx   = _parse_context(ctx_text)     if ctx_text   else None
    return usage, ctx


# ── QThread workers ───────────────────────────────────────────────────────────

class _StatsWorker(QThread):
    done = pyqtSignal(dict)

    def run(self):
        try:
            self.done.emit(fetch_token_stats())
        except Exception:
            pass


class _ClaudeWorker(QThread):
    usage_done   = pyqtSignal(object)
    context_done = pyqtSignal(object)

    def run(self):
        usage, ctx = fetch_claude_data()
        self.usage_done.emit(usage)
        self.context_done.emit(ctx)


# ── Reusable helpers ──────────────────────────────────────────────────────────

def _lbl(text: str = "", color: str = "text", size: int = 8,
         bold: bool = False, parent=None) -> QLabel:
    l = QLabel(text, parent)
    l.setProperty("_kind",  "lbl")
    l.setProperty("_color", color)
    l.setProperty("_pt",    size)
    l.setProperty("_bold",  bold)
    _restyle_lbl(l, 1.0)
    return l


def _restyle_lbl(l: QLabel, scale: float) -> None:
    pt   = max(5, int(round(l.property("_pt") * scale)))
    bold = l.property("_bold")
    color = l.property("_color")
    l.setStyleSheet(
        f"color:{C[color]}; font:{'bold ' if bold else ''}{pt}pt '{FONT}';"
        " background:transparent; border:none;"
    )


def _restyle_tab(btn: QLabel, scale: float) -> None:
    active = bool(btn.property("_active"))
    pt   = max(5, int(round(btn.property("_pt") * scale)))
    pad  = max(2, int(round(2  * scale)))
    rad  = max(8, int(round(10 * scale)))
    if active:
        btn.setStyleSheet(
            f"color:{C['accent']}; background:rgba(181,123,238,0.18);"
            f" font:bold {pt}pt '{FONT}'; padding:{pad}px; border-radius:{rad}px;"
        )
    else:
        btn.setStyleSheet(
            f"color:{C['dim']}; background:transparent;"
            f" font:{pt}pt '{FONT}'; padding:{pad}px; border-radius:{rad}px;"
        )


def _restyle_bar_pct(l: QLabel, scale: float) -> None:
    pt    = max(5, int(round(l.property("_pt") * scale)))
    color = l.property("_color_hex")
    l.setStyleSheet(
        f"color:{color}; font:bold {pt}pt '{FONT}'; background:transparent; border:none;"
    )


class _Bar(QWidget):
    """Thin progress bar drawn with QPainter — no stylesheet flickering."""

    BASE_H = 8

    def __init__(self, hex_color: str, parent=None):
        super().__init__(parent)
        self._fill = QColor(hex_color)
        self._pct  = 0.0
        self.setFixedHeight(self.BASE_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_pct(self, pct: float):
        self._pct = max(0.0, min(100.0, float(pct)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C["bar_bg"]))
        w = int(self.width() * self._pct / 100)
        if w > 0:
            p.fillRect(QRect(0, 0, w, self.height()), self._fill)


# ── Main widget ───────────────────────────────────────────────────────────────

class ClaudeWidget(QWidget):
    W, H = 220, 240

    # Cross-thread signal from file watcher → debounced stats refresh
    _file_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._drag_pos       = None
        self._resize_start   = None   # (global QPoint, start_w, start_h)
        self._cache_usage    = None
        self._cache_context  = None
        self._cache_stats    = None
        self._stats_worker   = None
        self._claude_worker  = None
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_W, MIN_H)

        self._init_window()
        self._build_ui()
        self._apply_scale()
        self._init_timers()
        self._init_watcher()
        self._init_tray()

        self._file_changed.connect(self._on_file_change)

        # Initial fetches
        self._do_stats()
        self._do_claude()

    # ── Window ────────────────────────────────────────────────────────────────

    def _init_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool           # skip taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.93)

        saved = self._load_geometry()
        if saved is not None:
            x, y, w, h = saved
            self.resize(max(MIN_W, w), max(MIN_H, h))
            self.move(x, y)
        else:
            self.resize(self.W, self.H)
            geo = QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - self.W - 20, geo.bottom() - self.H - 50)

    @staticmethod
    def _load_geometry():
        try:
            d = json.loads(GEOM_FILE.read_text(encoding="utf-8"))
            return int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"])
        except Exception:
            return None

    def _save_geometry(self):
        try:
            GEOM_FILE.parent.mkdir(parents=True, exist_ok=True)
            GEOM_FILE.write_text(json.dumps({
                "x": self.x(), "y": self.y(),
                "w": self.width(), "h": self.height(),
            }), encoding="utf-8")
        except Exception:
            pass

    def _in_resize_zone(self, pos) -> bool:
        return (self.width()  - pos.x() <= RESIZE_GRIP_PX and
                self.height() - pos.y() <= RESIZE_GRIP_PX)

    # ── Proportional scaling of text + bar/header heights ────────────────────

    def _scale_factor(self) -> float:
        # Anchor to default size so bigger window → bigger fonts (and slightly
        # smaller text if user shrinks the widget, clamped at 0.75).
        s = min(self.width() / self.W, self.height() / self.H)
        return max(0.75, s)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_scale()

    def _apply_scale(self):
        # Skip until the UI is built (resize fires once during __init__ before _build_ui)
        if not hasattr(self, "_titlebar"):
            return
        s = self._scale_factor()
        for l in self.findChildren(QLabel):
            k = l.property("_kind")
            if k == "lbl":
                _restyle_lbl(l, s)
            elif k == "tab":
                _restyle_tab(l, s)
            elif k == "bar_pct":
                _restyle_bar_pct(l, s)
        for bar in self.findChildren(_Bar):
            bar.setFixedHeight(max(_Bar.BASE_H, int(round(_Bar.BASE_H * s))))
        self._titlebar.setFixedHeight(max(32, int(round(32 * s))))
        self._footer_frame.setFixedHeight(max(22, int(round(22 * s))))

    def paintEvent(self, _):
        pass  # required for WA_TranslucentBackground

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Rounded card — the only opaque element
        card = QFrame(self)
        card.setStyleSheet(
            f"QFrame {{ background:{C['bg']}; border-radius:8px;"
            f" border:1px solid {C['sep']}; }}"
        )
        root.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._titlebar = self._build_titlebar()
        self._footer_frame = self._build_footer()
        cl.addWidget(self._titlebar)
        cl.addWidget(self._build_content(), 1)
        cl.addWidget(self._footer_frame)

    def _build_titlebar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(32)
        bar.setStyleSheet(
            f"background:{C['bg2']}; border-radius:8px 8px 0 0; border:none;"
        )
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(4)

        hl.addWidget(_lbl("⚡ Claude", "accent", 9, bold=True))
        hl.addStretch()

        self._dot = _lbl("●", "dim", 8)
        hl.addWidget(self._dot)

        self._btn_usage  = self._make_tab_btn("Usage",   active=True)
        self._btn_ctx    = self._make_tab_btn("Context", active=False)
        self._btn_models = self._make_tab_btn("Models",  active=False)
        self._btn_usage.mousePressEvent  = lambda _: self._switch_tab(0)
        self._btn_ctx.mousePressEvent    = lambda _: self._switch_tab(1)
        self._btn_models.mousePressEvent = lambda _: self._switch_tab(2)
        hl.addWidget(self._btn_usage)
        hl.addWidget(self._btn_ctx)
        hl.addWidget(self._btn_models)

        x = _lbl("×", "dim", 14, bold=True)
        x.setContentsMargins(4, 0, 0, 0)
        x.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        x.mousePressEvent = lambda _: self.close()
        x.enterEvent = lambda _: x.setStyleSheet(x.styleSheet().replace(C["dim"], "#f44336"))
        x.leaveEvent = lambda _: x.setStyleSheet(x.styleSheet().replace("#f44336", C["dim"]))
        hl.addWidget(x)
        return bar

    def _make_tab_btn(self, text: str, active: bool) -> QLabel:
        btn = QLabel(text)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.setProperty("_kind",   "tab")
        btn.setProperty("_pt",     8)
        btn.setProperty("_active", active)
        _restyle_tab(btn, self._scale_factor())
        return btn

    def _style_tab(self, btn: QLabel, active: bool):
        btn.setProperty("_active", active)
        _restyle_tab(btn, self._scale_factor())

    def _build_content(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background:{C['bg']}; border:none;")
        hl = QHBoxLayout(frame)
        hl.setContentsMargins(10, 8, 10, 4)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent; border:none;")
        self._stack.addWidget(self._build_usage_tab())    # index 0
        self._stack.addWidget(self._build_context_tab())  # index 1
        self._stack.addWidget(self._build_models_tab())   # index 2
        hl.addWidget(self._stack)
        return frame

    def _build_usage_tab(self) -> QWidget:
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        self._stat: dict[str, QLabel] = {}
        for key, label, color, bold in [
            ("inp",  "↑  Input tokens",  "text",   False),
            ("out",  "↓  Output tokens", "text",   False),
            ("cw",   "✎  Cache write",  "dim",    False),
            ("cr",   "≋  Cache read",   "dim",    False),
            ("cost", "$  Est. cost",    "accent", True),
        ]:
            row = QFrame(); row.setStyleSheet("background:transparent; border:none;")
            rl  = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0)
            ico, _, txt = label.partition(" ")
            rl.addWidget(_lbl(ico, "accent", 8))
            rl.addWidget(_lbl(txt.strip(), "dim", 8))
            rl.addStretch()
            val = _lbl("…", color, 8, bold)
            self._stat[key] = val
            rl.addWidget(val)
            vl.addWidget(row)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{C['sep']}; border:none;")
        vl.addWidget(sep)
        vl.addSpacing(4)

        self._bar_s = self._make_bar(vl, "Current session",           C["accent"])
        self._bar_w = self._make_bar(vl, "Current week (all models)", C["teal"])
        vl.addStretch()
        return w

    def _make_bar(self, layout: QVBoxLayout, label: str, hex_color: str) -> dict:
        c  = QWidget(); c.setStyleSheet("background:transparent; border:none;")
        cl = QVBoxLayout(c); cl.setContentsMargins(0, 2, 0, 0); cl.setSpacing(2)

        hdr = QWidget(); hdr.setStyleSheet("background:transparent; border:none;")
        hl  = QHBoxLayout(hdr); hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(_lbl(label, "dim", 7)); hl.addStretch()
        pct = QLabel("—")
        pct.setProperty("_kind",      "bar_pct")
        pct.setProperty("_color_hex", hex_color)
        pct.setProperty("_pt",        7)
        _restyle_bar_pct(pct, self._scale_factor())
        hl.addWidget(pct)
        cl.addWidget(hdr)

        bar = _Bar(hex_color); cl.addWidget(bar)

        rst = _lbl("", "dim", 6)
        rst.setAlignment(Qt.AlignmentFlag.AlignRight)
        cl.addWidget(rst)

        layout.addWidget(c)
        return {"bar": bar, "pct": pct, "reset": rst}

    def _build_context_tab(self) -> QWidget:
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(1)

        self._ctx_model   = _lbl("…", "accent", 9, bold=True)
        self._ctx_modelid = _lbl("",  "dim", 7)
        vl.addWidget(self._ctx_model)
        vl.addWidget(self._ctx_modelid)

        tot_row = QWidget(); tot_row.setStyleSheet("background:transparent; border:none;")
        tl = QHBoxLayout(tot_row); tl.setContentsMargins(0, 6, 0, 2)
        self._ctx_total   = _lbl("—", "text", 8, bold=True)
        self._ctx_tot_pct = _lbl("",  "accent", 8, bold=True)
        tl.addWidget(self._ctx_total); tl.addStretch(); tl.addWidget(self._ctx_tot_pct)
        vl.addWidget(tot_row)

        self._ctx_bar = _Bar(C["accent"]); vl.addWidget(self._ctx_bar)
        vl.addSpacing(4)
        vl.addWidget(_lbl("Usage by category", "dim", 7))

        self._ctx_cat: dict[str, QLabel] = {}
        for key, label, color in [
            ("system_prompt", "⚙︎  System prompt", "text"),
            ("system_tools",  "⊞  System tools",  "text"),
            ("mcp_tools",     "⊕  MCP tools",     "text"),
            ("memory_files",  "▤  Memory files",  "text"),
            ("skills",        "◎  Skills",        "text"),
            ("messages",      "◆  Messages",      "text"),
            ("free_space",    "◇  Free space",    "green"),
            ("autocompact",   "⊠  Autocompact",   "dim"),
        ]:
            row = QWidget(); row.setStyleSheet("background:transparent; border:none;")
            rl  = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0)
            ico, _, txt = label.partition(" ")
            rl.addWidget(_lbl(ico, "accent", 7))
            rl.addWidget(_lbl(txt.strip(), "dim", 7))
            rl.addStretch()
            val = _lbl("—", color, 7)
            self._ctx_cat[key] = val
            rl.addWidget(val)
            vl.addWidget(row)

        vl.addStretch()
        return w

    def _build_models_tab(self) -> QWidget:
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        self._models_layout = vl
        self._models_empty  = _lbl("No model usage today", "dim", 8)
        vl.addWidget(self._models_empty)
        vl.addStretch()
        return w

    def _apply_models(self, by_model: dict):
        # Drop previously-rendered model rows; keep the empty-state label + final stretch.
        lay = self._models_layout
        while lay.count() > 2:
            item = lay.takeAt(0)
            if item is None:
                break
            wdg = item.widget()
            if wdg is not None:
                wdg.setParent(None)
                wdg.deleteLater()

        items = sorted(by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        items = [(mid, bm) for mid, bm in items
                 if bm["inp"] + bm["out"] + bm["cw"] + bm["cr"] > 0]

        self._models_empty.setVisible(not items)

        for i, (mid, bm) in enumerate(items):
            # Header: model name + cost
            hdr_row = QFrame()
            hdr_row.setStyleSheet("background:transparent; border:none;")
            hr = QHBoxLayout(hdr_row); hr.setContentsMargins(0, 4 if i else 0, 0, 0)
            hr.addWidget(_lbl(bm["display"], "accent", 9, bold=True))
            hr.addStretch()
            hr.addWidget(_lbl(f"${bm['cost']:.4f}", "accent", 8, bold=True))
            lay.insertWidget(lay.count() - 2, hdr_row)

            # Token rows: input/output/cache w/cache r
            for key, label, color in [
                ("inp", "↑  Input",       "text"),
                ("out", "↓  Output",      "text"),
                ("cw",  "✎  Cache write", "dim"),
                ("cr",  "≋  Cache read",  "dim"),
            ]:
                row = QFrame(); row.setStyleSheet("background:transparent; border:none;")
                rl  = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0)
                ico, _, txt = label.partition(" ")
                rl.addWidget(_lbl(ico, "accent", 7))
                rl.addWidget(_lbl(txt.strip(), "dim", 7))
                rl.addStretch()
                rl.addWidget(_lbl(f"{bm[key]:,}", color, 7))
                lay.insertWidget(lay.count() - 2, row)

        self._apply_scale()   # scale the freshly-created rows to current widget size

    def _build_footer(self) -> QFrame:
        foot = QFrame()
        foot.setFixedHeight(22)
        foot.setStyleSheet(
            f"background:{C['bg2']}; border-radius:0 0 8px 8px; border:none;"
        )
        hl = QHBoxLayout(foot); hl.setContentsMargins(8, 0, 8, 0)
        hl.addStretch(1)
        self._footer = _lbl("Loading…", "dim", 7)
        self._footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hl.addWidget(self._footer)
        hl.addStretch(1)
        grip = _lbl("⇲", "dim", 10)
        grip.setToolTip("Drag to resize")
        grip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        hl.addWidget(grip)
        return foot

    # ── Tab switch ────────────────────────────────────────────────────────────

    def _switch_tab(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._style_tab(self._btn_usage,  idx == 0)
        self._style_tab(self._btn_ctx,    idx == 1)
        self._style_tab(self._btn_models, idx == 2)
        # Show cached data immediately — no waiting
        if idx == 0 and self._cache_usage is not None:
            self._apply_usage(self._cache_usage)
        if idx == 1 and self._cache_context is not None:
            self._apply_context(self._cache_context)
        if idx == 2 and self._cache_stats is not None:
            self._apply_models(self._cache_stats.get("by_model", {}))

    # ── Drag & menu ───────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        if self._in_resize_zone(pos):
            self._resize_start = (e.globalPosition().toPoint(),
                                  self.width(), self.height())
        elif pos.y() <= 32:
            self._drag_pos = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._resize_start and e.buttons() & Qt.MouseButton.LeftButton:
            g0, w0, h0 = self._resize_start
            d = e.globalPosition().toPoint() - g0
            self.resize(max(MIN_W, w0 + d.x()), max(MIN_H, h0 + d.y()))
            return
        if self._drag_pos and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            return
        # No button held → update cursor based on hover zone
        if self._in_resize_zone(e.position().toPoint()):
            self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, _):
        if self._drag_pos is not None or self._resize_start is not None:
            self._save_geometry()
        self._drag_pos     = None
        self._resize_start = None

    # ── System tray ───────────────────────────────────────────────────────────

    def _init_tray(self):
        px = QPixmap(22, 22)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(C["accent"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 20, 20)
        p.setPen(QColor(C["bg"]))
        f = p.font(); f.setPointSize(10); f.setBold(True); p.setFont(f)
        p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "C")
        p.end()

        self._tray = QSystemTrayIcon(QIcon(px), self)
        self._tray.setToolTip("Claude Widget")

        menu = QMenu()
        menu.setStyleSheet(
            f"QMenu{{background:{C['bg2']};color:{C['text']};"
            f"border:1px solid {C['dim']};}}"
            f"QMenu::item:selected{{background:{C['accent']};color:{C['bg']};}}"
        )
        menu.addAction("Bring to front", self._bring_to_front)
        menu.addAction("Refresh",        self._refresh_all)
        self._startup_action = None
        if sys.platform == "win32":
            menu.addSeparator()
            self._startup_action = menu.addAction("", self._toggle_startup)
            menu.aboutToShow.connect(self._update_startup_action)
        menu.addSeparator()
        menu.addAction("Quit",           self._quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _bring_to_front(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _refresh_all(self):
        self._do_stats()
        self._do_claude()

    def _update_startup_action(self):
        self._startup_action.setText(
            "Remove from startup" if _startup_enabled() else "Add to startup"
        )

    def _toggle_startup(self):
        _set_startup(not _startup_enabled())

    def _quit(self):
        if hasattr(self, "_watcher"):
            self._watcher.stop()
        QApplication.quit()

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self._bring_to_front()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        m.setStyleSheet(
            f"QMenu{{background:{C['bg2']};color:{C['text']};"
            f"border:1px solid {C['dim']};}}"
            f"QMenu::item:selected{{background:{C['accent']};color:{C['bg']};}}"
        )
        m.addAction("Refresh stats",  self._do_stats)
        m.addAction("Refresh Claude", self._do_claude)
        m.addSeparator()
        m.addAction("Close", self.close)
        m.exec(e.globalPos())

    def closeEvent(self, e):
        if hasattr(self, "_tray") and self._tray.isVisible():
            self.hide()
            e.ignore()
        else:
            if hasattr(self, "_watcher"):
                self._watcher.stop()
            e.accept()

    # ── Timers ────────────────────────────────────────────────────────────────

    def _init_timers(self):
        self._t_stats = QTimer(self)
        self._t_stats.setInterval(REFRESH_STATS_MS)
        self._t_stats.timeout.connect(self._do_stats)
        self._t_stats.start()

        self._t_claude = QTimer(self)
        self._t_claude.setInterval(REFRESH_CLAUDE_MS)
        self._t_claude.timeout.connect(self._do_claude)
        self._t_claude.start()

        # Debounce: coalesce rapid file-change bursts into one stats refresh
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(800)
        self._debounce.timeout.connect(self._do_stats)

    # ── File watcher ──────────────────────────────────────────────────────────

    def _init_watcher(self):
        if not PROJECTS_DIR.exists():
            return
        self._watcher = _FileWatcher(PROJECTS_DIR, self._file_changed.emit)
        self._watcher.start()
        # If both native and watchdog unavailable, QTimer polling is already running
        if self._watcher.method == "none":
            del self._watcher  # let QTimer handle it

    @pyqtSlot()
    def _on_file_change(self):
        self._debounce.start()  # resets the 800 ms window on each event

    # ── Workers: skip if already running ─────────────────────────────────────

    def _do_stats(self):
        if self._stats_worker and self._stats_worker.isRunning():
            return
        self._stats_worker = _StatsWorker(self)
        self._stats_worker.done.connect(self._on_stats)
        self._stats_worker.start()

    def _do_claude(self):
        if self._claude_worker and self._claude_worker.isRunning():
            return
        # Show loading indicator only on first load (no cached data yet)
        if self._cache_usage is None and self._stack.currentIndex() == 0:
            self._bar_s["pct"].setText("loading…")
            self._bar_w["pct"].setText("loading…")
        if self._cache_context is None and self._stack.currentIndex() == 1:
            self._ctx_total.setText("loading…")
        self._claude_worker = _ClaudeWorker(self)
        self._claude_worker.usage_done.connect(self._on_usage)
        self._claude_worker.context_done.connect(self._on_context)
        self._claude_worker.start()

    # ── Apply results (always on main thread via pyqtSignal) ──────────────────

    @pyqtSlot(dict)
    def _on_stats(self, d: dict):
        self._cache_stats = d
        self._stat["inp"].setText(f"{d['inp']:,}")
        self._stat["out"].setText(f"{d['out']:,}")
        self._stat["cw"].setText(f"{d['cw']:,}")
        self._stat["cr"].setText(f"{d['cr']:,}")
        self._stat["cost"].setText(f"${d['cost']:.4f}")
        self._dot.setProperty("_color", "green" if d["active"] > 0 else "dim")
        _restyle_lbl(self._dot, self._scale_factor())
        ts  = datetime.now().strftime("%H:%M:%S")
        act = f"{d['active']} active" if d["active"] > 0 else "inactive"
        self._footer.setText(f"{ts}  ·  {d['sessions']} session(s)  ·  {act}")
        if self._stack.currentIndex() == 2:
            self._apply_models(d.get("by_model", {}))

    @pyqtSlot(object)
    def _on_usage(self, data):
        self._cache_usage = data
        if self._stack.currentIndex() == 0:
            self._apply_usage(data)

    @pyqtSlot(object)
    def _on_context(self, data):
        self._cache_context = data
        if self._stack.currentIndex() == 1:
            self._apply_context(data)

    def _apply_usage(self, data):
        if not data:
            self._bar_s["pct"].setText("no data")
            self._bar_w["pct"].setText("no data")
            return
        for bar, pk, rk in [
            (self._bar_s, "session_pct", "session_reset"),
            (self._bar_w, "week_pct",    "week_reset"),
        ]:
            pct = data.get(pk) or 0
            bar["bar"].set_pct(pct)
            bar["pct"].setText(f"{pct}% used")
            rst = data.get(rk, "")
            bar["reset"].setText(f"Resets {rst}" if rst else "")

    def _apply_context(self, data):
        if not data:
            self._ctx_model.setText("no data")
            self._ctx_modelid.setText("")
            self._ctx_total.setText("—")
            self._ctx_tot_pct.setText("")
            self._ctx_bar.set_pct(0)
            for v in self._ctx_cat.values():
                v.setText("—")
            return
        self._ctx_model.setText(data.get("model") or "Claude")
        self._ctx_modelid.setText(data.get("model_id") or "")
        tot = data.get("total_tokens", ""); mx = data.get("total_max", "")
        pct = data.get("total_pct")
        self._ctx_total.setText(f"{tot}/{mx} tokens" if tot and mx else "—")
        self._ctx_tot_pct.setText(f"{pct:g}%" if pct is not None else "")
        self._ctx_bar.set_pct(pct or 0)
        for key, lbl in self._ctx_cat.items():
            c = data.get("categories", {}).get(key)
            lbl.setText(f"{c['tokens']} ({c['pct']:g}%)" if c else "—")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    widget = ClaudeWidget()
    widget.show()
    sys.exit(app.exec())
