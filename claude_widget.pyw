#!/usr/bin/env python3
"""
Claude Usage Widget — overlay flotante para Windows.
- Stats de tokens: JSONL (~/.claude/projects) cada 15 s
- Barras Current session / Current week: subprocess PTY de /usage cada 5 min
"""
import json, re, os, time, threading, ctypes
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk

# Ocultar la consola de python.exe (necesaria para que pywinpty pueda
# asignar el pseudo-console, pero no queremos que aparezca al usuario).
try:
    _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if _hwnd:
        ctypes.windll.user32.ShowWindow(_hwnd, 0)
except Exception:
    pass

try:
    import winpty
    _WINPTY = True
except ImportError:
    _WINPTY = False

# ── Rutas ─────────────────────────────────────────────────────────────────────
CLAUDE_DIR   = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"

PRICE = dict(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30)

REFRESH_STATS_MS = 15_000   # tokens desde JSONL
REFRESH_USAGE_MS = 60_000   # barras via /usage
REFRESH_CONTEXT_MS = 60_000 # context usage via /context

BG = "#0d0d1a"; BG2 = "#14142a"; ACCENT = "#b57bee"
TEAL = "#26c6da"; TEXT = "#d0d0e0"; DIM = "#55557a"
GREEN = "#4caf50"; FONT = "Segoe UI"; TAB_BG = "#1a1a30"


# ── Token stats (JSONL) ───────────────────────────────────────────────────────

def fetch_token_stats():
    today = datetime.now(timezone.utc).date()
    inp = out = cw = cr = 0
    sessions_today, active_sessions = set(), set()

    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            sid = d.get("sessionId", "")
            if sid: active_sessions.add(sid)
        except Exception: pass

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(jsonl, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw: continue
                    try: entry = json.loads(raw)
                    except: continue
                    if entry.get("type") != "assistant": continue
                    ts = entry.get("timestamp", "")
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt.date() != today: continue
                    except: continue
                    sessions_today.add(entry.get("sessionId", ""))
                    u = entry.get("message", {}).get("usage", {})
                    inp += u.get("input_tokens", 0)
                    out += u.get("output_tokens", 0)
                    cw  += u.get("cache_creation_input_tokens", 0)
                    cr  += u.get("cache_read_input_tokens", 0)
        except Exception: pass

    cost = (inp*PRICE["input"] + out*PRICE["output"] +
            cw*PRICE["cache_write"] + cr*PRICE["cache_read"]) / 1_000_000

    return dict(inp=inp, out=out, cw=cw, cr=cr, cost=cost,
                sessions=len(sessions_today),
                active=len(sessions_today & active_sessions))


# ── Usage bars via /usage PTY ─────────────────────────────────────────────────

def _clean_ansi(text):
    # Cursor-position → espacio (preserva fronteras de palabras)
    text = re.sub(r'\x1b\[\d+;\d*H', ' ', text)
    # Resto de secuencias ANSI
    text = re.sub(r'\x1b\[[0-9;?]*[mGKHFJA-Za-z]', '', text)
    text = re.sub(r'\x1b[()][A-Z0-9]|\r|\x1b=|\x1b>', '', text)
    return text

def _parse_usage_text(text):
    """Extrae (session_pct, session_reset, week_pct, week_reset) del output limpio."""
    result = dict(session_pct=None, session_reset="",
                  week_pct=None,   week_reset="")

    # Porcentajes en orden de aparicion: 1ro = session, 2do = week
    pcts = re.findall(r'(\d+)%', text)
    if len(pcts) >= 1: result["session_pct"] = int(pcts[0])
    if len(pcts) >= 2: result["week_pct"]    = int(pcts[1])

    # Tiempos de reset — patron flexible para texto pegado por ANSI stripping
    reset_re = re.compile(
        r'Re[set]+s?\s*'
        r'((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s*\d+\s*,?\s*)?'
        r'\d+(?::\d+)?\s*(?:am|pm))'
        r'(?:\s*\(([^)]+)\))?',
        re.IGNORECASE)

    resets = reset_re.findall(text)
    if len(resets) >= 1:
        t, tz = resets[0]
        result["session_reset"] = t.strip() + (f" ({tz})" if tz else "")
    if len(resets) >= 2:
        t, tz = resets[1]
        result["week_reset"] = t.strip() + (f" ({tz})" if tz else "")

    return result

def fetch_claude_usage():
    """Lanza claude en PTY oculto, ejecuta /usage, devuelve dict con porcentajes."""
    if not _WINPTY:
        return None
    try:
        old_cwd = os.getcwd()
        os.chdir(str(Path.home()))
        pty = winpty.PTY(220, 50)
        pty.spawn("cmd.exe /c claude")
        os.chdir(old_cwd)

        def _read_until(kw, timeout=15):
            buf = ""; dl = time.time() + timeout
            while time.time() < dl:
                c = pty.read(blocking=False)
                if c:
                    buf += c if isinstance(c, str) else c.decode("utf-8", errors="replace")
                    if kw in buf: return buf, True
                time.sleep(0.15)
            return buf, False

        def _read_for(secs):
            buf = ""; dl = time.time() + secs
            while time.time() < dl:
                c = pty.read(blocking=False)
                if c:
                    buf += c if isinstance(c, str) else c.decode("utf-8", errors="replace")
                time.sleep(0.15)
            return buf

        out = ""
        chunk, has_trust = _read_until("trust", timeout=6)
        out += chunk
        if has_trust:
            pty.write("\r")

        chunk2, _ = _read_until("shortcuts", timeout=18)
        out += chunk2

        pty.write("/usage\r")
        out += _read_for(8)

        try: pty.write("/exit\r"); time.sleep(0.5)
        except Exception: pass

        return _parse_usage_text(_clean_ansi(out))

    except Exception:
        return None


def _parse_context_text(text):
    """Extrae datos del output de /context:
    - model, model_id
    - total_tokens, total_max, total_pct
    - categorías: system_prompt, system_tools, mcp_tools, memory_files, skills,
                  messages, free_space, autocompact
    Cada categoría es dict(tokens=str, pct=float|None)
    """
    result = dict(model="", model_id="", total_tokens="", total_max="",
                  total_pct=None, categories={})

    # Modelo (ej: "Sonnet 4.6" y "claude-sonnet-4-6")
    m = re.search(r'(Sonnet|Opus|Haiku)\s+[\d.]+', text, re.IGNORECASE)
    if m: result["model"] = m.group(0)
    m = re.search(r'(claude-[a-z0-9-]+)', text, re.IGNORECASE)
    if m: result["model_id"] = m.group(1)

    # Total: "20.5k/200k tokens (10%)"
    m = re.search(r'([\d.]+\s*[kKmM]?)\s*/\s*([\d.]+\s*[kKmM]?)\s*tokens?\s*\((\d+(?:\.\d+)?)%\)', text)
    if m:
        result["total_tokens"] = m.group(1).strip()
        result["total_max"] = m.group(2).strip()
        result["total_pct"] = float(m.group(3))

    # Categorías: "Label: 6.3k tokens (3.2%)"
    cat_patterns = [
        ("system_prompt",  r'System\s*prompt'),
        ("system_tools",   r'System\s*tools'),
        ("mcp_tools",      r'MCP\s*tools'),
        ("memory_files",   r'Memory\s*files'),
        ("skills",         r'Skills'),
        ("messages",       r'Messages'),
        ("free_space",     r'Free\s*space'),
        ("autocompact",    r'Autocompact\s*buffer'),
    ]
    for key, label_re in cat_patterns:
        m = re.search(
            label_re + r'[:\s]+([\d.]+\s*[kKmM]?)\s*(?:tokens?)?\s*\((\d+(?:\.\d+)?)%\)',
            text, re.IGNORECASE)
        if m:
            result["categories"][key] = dict(
                tokens=m.group(1).strip(),
                pct=float(m.group(2)),
            )

    return result


def fetch_claude_context():
    """Lanza claude en PTY oculto, ejecuta /context, devuelve dict con contexto."""
    if not _WINPTY:
        return None
    try:
        old_cwd = os.getcwd()
        os.chdir(str(Path.home()))
        pty = winpty.PTY(220, 50)
        pty.spawn("cmd.exe /c claude")
        os.chdir(old_cwd)

        def _read_until(kw, timeout=15):
            buf = ""; dl = time.time() + timeout
            while time.time() < dl:
                c = pty.read(blocking=False)
                if c:
                    buf += c if isinstance(c, str) else c.decode("utf-8", errors="replace")
                    if kw in buf: return buf, True
                time.sleep(0.15)
            return buf, False

        def _read_for(secs):
            buf = ""; dl = time.time() + secs
            while time.time() < dl:
                c = pty.read(blocking=False)
                if c:
                    buf += c if isinstance(c, str) else c.decode("utf-8", errors="replace")
                time.sleep(0.15)
            return buf

        out = ""
        chunk, has_trust = _read_until("trust", timeout=6)
        out += chunk
        if has_trust:
            pty.write("\r")

        chunk2, _ = _read_until("shortcuts", timeout=18)
        out += chunk2

        pty.write("/context\r")
        out += _read_for(8)

        try: pty.write("/exit\r"); time.sleep(0.5)
        except Exception: pass

        return _parse_context_text(_clean_ansi(out))

    except Exception:
        return None


# ── Widget ────────────────────────────────────────────────────────────────────

class ClaudeWidget:
    W, H = 280, 340

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claude Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.93)
        self.root.configure(bg=BG)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{sw-self.W-20}+{sh-self.H-50}")

        self.current_tab = "usage"  # Tab activo: "usage" o "context"
        self.tab_frames = {}  # Almacenar frames de cada tab
        
        self._build_ui()
        self._bind_drag()
        self._bind_menu()
        self.root.bind("<Escape>", lambda _: self.root.destroy())

        self._usage_cache = None
        self._context_cache = None
        self._enqueue_stats()
        # Solo enqueuar el tab activo inicial (usage)
        self._enqueue_usage()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Título y tabs
        self.title_bar = tk.Frame(self.root, bg=BG2, height=30)
        self.title_bar.pack(fill=tk.X); self.title_bar.pack_propagate(False)

        self.lbl_title = tk.Label(self.title_bar, text=" ⚡ Claude",
            bg=BG2, fg=ACCENT, font=(FONT, 9, "bold"))
        self.lbl_title.pack(side=tk.LEFT, padx=4, pady=6)

        # Botones de tab
        self.tab_usage_btn = tk.Label(self.title_bar, text="Usage",
            bg=ACCENT, fg=BG, font=(FONT, 8, "bold"), cursor="hand2", padx=8, pady=2)
        self.tab_usage_btn.pack(side=tk.LEFT, padx=2)
        self.tab_usage_btn.bind("<Button-1>", lambda e: self._switch_tab("usage"))

        self.tab_context_btn = tk.Label(self.title_bar, text="Context",
            bg=DIM, fg=TEXT, font=(FONT, 8), cursor="hand2", padx=8, pady=2)
        self.tab_context_btn.pack(side=tk.LEFT, padx=2)
        self.tab_context_btn.bind("<Button-1>", lambda e: self._switch_tab("context"))

        self.btn_close = tk.Label(self.title_bar, text=" × ",
            bg=BG2, fg=DIM, font=(FONT, 12, "bold"), cursor="hand2")
        self.btn_close.pack(side=tk.RIGHT, padx=4)
        self.btn_close.bind("<Button-1>", lambda e: self.root.destroy())
        self.btn_close.bind("<Enter>",    lambda e: self.btn_close.config(fg="#f44336"))
        self.btn_close.bind("<Leave>",    lambda e: self.btn_close.config(fg=DIM))

        self.dot = tk.Label(self.title_bar, text="●", bg=BG2, fg=DIM, font=(FONT, 8))
        self.dot.pack(side=tk.RIGHT, pady=6)

        # Contenedor principal para los tabs
        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill=tk.BOTH, expand=True, padx=10, pady=(7, 4))

        # ── TAB: Usage ────────────────────────────────────────────────────────────
        usage_frame = tk.Frame(self.content, bg=BG)
        self.tab_frames["usage"] = usage_frame

        self.vars = {}
        for key, label, color, bold in [
            ("inp",  "↑  Input tokens",  TEXT,   False),
            ("out",  "↓  Output tokens", TEXT,   False),
            ("cw",   "✎  Cache escrito", DIM,    False),
            ("cr",   "⚡  Cache leido",   DIM,    False),
            ("cost", "💲  Costo est.",    ACCENT, True),
        ]:
            row = tk.Frame(usage_frame, bg=BG); row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, bg=BG, fg=DIM,
                     font=(FONT, 8), anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar(value="…"); self.vars[key] = v
            tk.Label(row, textvariable=v, bg=BG, fg=color,
                     font=(FONT, 8, "bold" if bold else "normal"),
                     anchor="e").pack(side=tk.RIGHT)

        tk.Frame(usage_frame, bg="#1e1e3a", height=1).pack(fill=tk.X, pady=(6, 4))

        # Barras /usage
        (self.bar_s_cv, self.bar_s_id, self.bar_s_pct,
         self.bar_s_reset) = self._make_bar(usage_frame, "Current session", ACCENT)
        (self.bar_w_cv, self.bar_w_id, self.bar_w_pct,
         self.bar_w_reset) = self._make_bar(usage_frame, "Current week (all models)", TEAL)

        # ── TAB: Context ──────────────────────────────────────────────────────────
        context_frame = tk.Frame(self.content, bg=BG)
        self.tab_frames["context"] = context_frame

        # Modelo
        self.ctx_model_var = tk.StringVar(value="…")
        tk.Label(context_frame, textvariable=self.ctx_model_var, bg=BG, fg=ACCENT,
                 font=(FONT, 9, "bold"), anchor="w").pack(fill=tk.X)

        self.ctx_model_id_var = tk.StringVar(value="")
        tk.Label(context_frame, textvariable=self.ctx_model_id_var, bg=BG, fg=DIM,
                 font=(FONT, 7), anchor="w").pack(fill=tk.X)

        # Total tokens + barra
        total_row = tk.Frame(context_frame, bg=BG); total_row.pack(fill=tk.X, pady=(6, 2))
        self.ctx_total_var = tk.StringVar(value="—")
        tk.Label(total_row, textvariable=self.ctx_total_var, bg=BG, fg=TEXT,
                 font=(FONT, 8, "bold"), anchor="w").pack(side=tk.LEFT)
        self.ctx_total_pct_var = tk.StringVar(value="")
        tk.Label(total_row, textvariable=self.ctx_total_pct_var, bg=BG, fg=ACCENT,
                 font=(FONT, 8, "bold"), anchor="e").pack(side=tk.RIGHT)

        self.ctx_total_cv = tk.Canvas(context_frame, bg="#1a1a30", height=8,
                                       bd=0, highlightthickness=0)
        self.ctx_total_cv.pack(fill=tk.X, pady=(0, 4))
        self.ctx_total_rect = self.ctx_total_cv.create_rectangle(0, 0, 0, 8,
                                                                  fill=ACCENT, outline="")

        # Separador
        tk.Label(context_frame, text="Usage by category", bg=BG, fg=DIM,
                 font=(FONT, 7, "italic"), anchor="w").pack(fill=tk.X, pady=(4, 2))

        # Categorías
        self.ctx_cat_vars = {}
        cat_labels = [
            ("system_prompt", "⚙ System prompt", TEXT),
            ("system_tools",  "🔧 System tools", TEXT),
            ("mcp_tools",     "🔌 MCP tools",    TEXT),
            ("memory_files",  "📝 Memory files", TEXT),
            ("skills",        "🎯 Skills",       TEXT),
            ("messages",      "💬 Messages",     TEXT),
            ("free_space",    "✨ Free space",   GREEN),
            ("autocompact",   "⊠ Autocompact",   DIM),
        ]
        for key, label, color in cat_labels:
            row = tk.Frame(context_frame, bg=BG); row.pack(fill=tk.X, pady=0)
            tk.Label(row, text=label, bg=BG, fg=DIM,
                     font=(FONT, 7), anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar(value="—"); self.ctx_cat_vars[key] = v
            tk.Label(row, textvariable=v, bg=BG, fg=color,
                     font=(FONT, 7), anchor="e").pack(side=tk.RIGHT)

        # Pie
        foot = tk.Frame(self.root, bg=BG2, height=22)
        foot.pack(fill=tk.X); foot.pack_propagate(False)
        self.footer_var = tk.StringVar(value="Cargando…")
        tk.Label(foot, textvariable=self.footer_var, bg=BG2, fg=DIM,
                 font=(FONT, 7)).pack(pady=3)

        # Mostrar tab inicial (Usage)
        self._switch_tab("usage")

    def _make_bar(self, parent, label, color):
        c = tk.Frame(parent, bg=BG); c.pack(fill=tk.X, pady=2)

        h = tk.Frame(c, bg=BG); h.pack(fill=tk.X)
        tk.Label(h, text=label, bg=BG, fg=DIM,
                 font=(FONT, 7), anchor="w").pack(side=tk.LEFT)
        pct_var = tk.StringVar(value="—")
        tk.Label(h, textvariable=pct_var, bg=BG, fg=color,
                 font=(FONT, 7, "bold")).pack(side=tk.RIGHT)

        cv = tk.Canvas(c, bg="#1a1a30", height=8, bd=0, highlightthickness=0)
        cv.pack(fill=tk.X, pady=(2, 0))
        rect_id = cv.create_rectangle(0, 0, 0, 8, fill=color, outline="")

        reset_var = tk.StringVar(value="")
        tk.Label(c, textvariable=reset_var, bg=BG, fg=DIM,
                 font=(FONT, 6)).pack(anchor="e")

        return cv, rect_id, pct_var, reset_var

    def _switch_tab(self, tab_name):
        """Cambia entre tabs: 'usage' o 'context'"""
        self.current_tab = tab_name

        # Ocultar todos los frames
        for frame in self.tab_frames.values():
            frame.pack_forget()

        # Mostrar el frame del tab activo
        if tab_name in self.tab_frames:
            self.tab_frames[tab_name].pack(fill=tk.BOTH, expand=True)

        # Actualizar estilos de botones
        if tab_name == "usage":
            self.tab_usage_btn.config(bg=ACCENT, fg=BG, font=(FONT, 8, "bold"))
            self.tab_context_btn.config(bg=DIM, fg=TEXT, font=(FONT, 8))
            # Iniciar actualización de usage
            self._enqueue_usage()
        else:  # context
            self.tab_usage_btn.config(bg=DIM, fg=TEXT, font=(FONT, 8))
            self.tab_context_btn.config(bg=ACCENT, fg=BG, font=(FONT, 8, "bold"))
            # Iniciar actualización de context
            self._enqueue_context()

    def _set_bar(self, cv, rect_id, pct_var, reset_var, pct, reset_txt):
        cv.update_idletasks()
        w = cv.winfo_width()
        cv.coords(rect_id, 0, 0, max(0, int(w * pct / 100)), 8)
        pct_var.set(f"{pct}% used")
        reset_var.set(f"Resets {reset_txt}" if reset_txt else "")

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _bind_drag(self):
        self._dx = self._dy = 0
        for w in (self.title_bar, self.lbl_title):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _bind_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground=BG,
                       font=(FONT, 9))
        menu.add_command(label="Actualizar stats",    command=self._enqueue_stats)
        menu.add_command(label="Actualizar barras",   command=self._enqueue_usage)
        menu.add_command(label="Actualizar contexto", command=self._enqueue_context)
        menu.add_separator()
        menu.add_command(label="Cerrar widget",       command=self.root.destroy)
        self.root.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # ── Refresh: stats (15 s) ─────────────────────────────────────────────────

    def _enqueue_stats(self):
        threading.Thread(target=self._bg_stats, daemon=True).start()
        self.root.after(REFRESH_STATS_MS, self._enqueue_stats)

    def _bg_stats(self):
        try:
            d = fetch_token_stats()
            self.root.after(0, lambda: self._apply_stats(d))
        except Exception as ex:
            self.root.after(0, lambda: self.footer_var.set(f"Error: {ex}"))

    def _apply_stats(self, d):
        self.vars["inp"].set(f"{d['inp']:,}")
        self.vars["out"].set(f"{d['out']:,}")
        self.vars["cw"].set(f"{d['cw']:,}")
        self.vars["cr"].set(f"{d['cr']:,}")
        self.vars["cost"].set(f"${d['cost']:.4f}")
        self.dot.config(fg=GREEN if d["active"] > 0 else DIM)
        ts  = datetime.now().strftime("%H:%M:%S")
        act = f"{d['active']} activa(s)" if d["active"] > 0 else "inactivo"
        self.footer_var.set(f"{ts}  ·  {d['sessions']} sesion(es)  ·  {act}")

    # ── Refresh: /usage bars (5 min) ──────────────────────────────────────────

    def _enqueue_usage(self):
        # Solo actualizar si estamos en el tab de usage
        if self.current_tab != "usage":
            return
            
        # Muestra "cargando" en las barras mientras espera
        self.bar_s_pct.set("cargando…")
        self.bar_w_pct.set("cargando…")
        threading.Thread(target=self._bg_usage, daemon=True).start()
        self.root.after(REFRESH_USAGE_MS, self._enqueue_usage)

    def _bg_usage(self):
        data = fetch_claude_usage()
        self.root.after(0, lambda: self._apply_usage(data))

    def _apply_usage(self, data):
        if not data:
            self.bar_s_pct.set("sin datos")
            self.bar_w_pct.set("sin datos")
            return

        sp = data["session_pct"] or 0
        wp = data["week_pct"]    or 0
        self._set_bar(self.bar_s_cv, self.bar_s_id, self.bar_s_pct,
                      self.bar_s_reset, sp, data["session_reset"])
        self._set_bar(self.bar_w_cv, self.bar_w_id, self.bar_w_pct,
                      self.bar_w_reset, wp, data["week_reset"])

    # ── Refresh: /context (5 min) ─────────────────────────────────────────────

    def _enqueue_context(self):
        # Solo actualizar si estamos en el tab de context
        if self.current_tab != "context":
            return

        # Muestra "cargando" mientras espera
        self.ctx_total_var.set("cargando…")
        self.ctx_total_pct_var.set("")
        threading.Thread(target=self._bg_context, daemon=True).start()
        self.root.after(REFRESH_CONTEXT_MS, self._enqueue_context)

    def _bg_context(self):
        data = fetch_claude_context()
        self.root.after(0, lambda: self._apply_context(data))

    def _apply_context(self, data):
        if not data:
            self.ctx_model_var.set("sin datos")
            self.ctx_model_id_var.set("")
            self.ctx_total_var.set("—")
            self.ctx_total_pct_var.set("")
            for v in self.ctx_cat_vars.values():
                v.set("—")
            return

        # Modelo
        self.ctx_model_var.set(data.get("model") or "Claude")
        self.ctx_model_id_var.set(data.get("model_id") or "")

        # Total + barra
        tot = data.get("total_tokens", "")
        mx = data.get("total_max", "")
        pct = data.get("total_pct")
        if tot and mx:
            self.ctx_total_var.set(f"{tot}/{mx} tokens")
        else:
            self.ctx_total_var.set("—")
        self.ctx_total_pct_var.set(f"{pct:g}%" if pct is not None else "")

        self.ctx_total_cv.update_idletasks()
        w = self.ctx_total_cv.winfo_width()
        p = pct if pct is not None else 0
        self.ctx_total_cv.coords(self.ctx_total_rect, 0, 0,
                                 max(0, int(w * p / 100)), 8)

        # Categorías
        cats = data.get("categories", {})
        for key, v in self.ctx_cat_vars.items():
            c = cats.get(key)
            if c:
                v.set(f"{c['tokens']} ({c['pct']:g}%)")
            else:
                v.set("—")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ClaudeWidget().run()
