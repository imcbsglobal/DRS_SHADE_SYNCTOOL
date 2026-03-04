"""
DRS Sync — app.py
GUI launcher with status window, desktop notifications, and single-instance guard.

Double-click behaviour:
  • First launch  → show status window, start background sync thread
  • Already running → bring existing window to front (via named mutex / socket)
"""

import sys
import os
import json
import socket
import threading
import time
import logging
import ctypes
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Tkinter (bundled with Python / PyInstaller) ────────────────────────────
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

# ── Optional: plyer for cross-platform desktop notifications ───────────────
try:
    from plyer import notification as _plyer_notify
    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False

HAS_TRAY = False

# ── Single-instance port ───────────────────────────────────────────────────
_LOCK_PORT   = 47921          # arbitrary local TCP port
_APP_NAME    = "DRS Sync"
_EXE_NAME    = "DRSSync"

# Internal full config (baked into exe)
_INTERNAL_CONFIG = {
    "source_db": {
        "driver":   "SQL Anywhere 17",
        "server":   "SHADEDB",
        "database": "SHADEDB",
        "uid":      "DBA",
        "pwd":      "(*$^)",
        "port":     2638
    },
    "target_db": {"type": "sqlite", "path": "db.sqlite3"},
    "sync":      {"interval_seconds": 300, "log_file": "hms_sync.log", "log_level": "INFO"}
}

# config.json sits next to the exe — only needs "database" key
CONFIG_FILE = Path(sys.executable).parent / "config.json" \
              if getattr(sys, "frozen", False) \
              else Path(__file__).parent / "config.json"


def get_db_path() -> Path:
    """
    Store db.sqlite3 in %APPDATA%\\DRSSync\\ (hidden from the exe folder).
    Falls back to exe folder if AppData is unavailable.
    """
    try:
        app_data = Path(os.environ.get("APPDATA", "")) / "DRSSync"
        app_data.mkdir(parents=True, exist_ok=True)
        return app_data / "db.sqlite3"
    except Exception:
        return CONFIG_FILE.parent / "db.sqlite3"

# ──────────────────────────────────────────────────────────────────────────
#  Sync logic (inline – mirrors sync.py but callable from a thread)
# ──────────────────────────────────────────────────────────────────────────

def load_config():
    """
    Reads a simple flat config.json next to the exe:
        { "database": "SHADEDB" }
    All other connection settings come from _INTERNAL_CONFIG defaults.
    """
    import copy
    cfg = copy.deepcopy(_INTERNAL_CONFIG)

    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(json.dumps({"database": "SHADEDB"}, indent=4))
        except Exception:
            pass
        return cfg

    try:
        with open(CONFIG_FILE) as f:
            user_cfg = json.load(f)
    except Exception:
        return cfg

    # Support both flat {"database": "X"} and nested {"source_db": {"database": "X"}}
    src = user_cfg.get("source_db", user_cfg)
    db_name = src.get("database", "").strip()
    if db_name:
        cfg["source_db"]["database"] = db_name
        cfg["source_db"]["server"]   = db_name
    return cfg


def _rows_to_dicts(cursor):
    import decimal
    from datetime import date
    columns = [col[0].lower() for col in cursor.description]
    result  = []
    for row in cursor.fetchall():
        d = {}
        for k, v in zip(columns, row):
            if isinstance(v, decimal.Decimal): v = float(v)
            elif isinstance(v, (date, datetime)): v = v.isoformat()
            elif isinstance(v, str): v = v.strip()
            d[k] = v
        result.append(d)
    return result


def _get_src_conn(cfg, log):
    import pyodbc
    src = cfg["source_db"]
    drv = src.get("driver", "SQL Anywhere 17")
    db  = src.get("database", "")
    uid = src.get("uid", "DBA")
    pwd = src.get("pwd", "")
    port= src.get("port", 2638)
    srv = src.get("server", "").strip()

    attempts = []
    if srv:
        attempts.append((f"ServerName={srv}",
            f"DRIVER={{{drv}}};ServerName={srv};DatabaseName={db};UID={uid};PWD={pwd};"))
    attempts += [
        (f"ServerName={db}",
         f"DRIVER={{{drv}}};ServerName={db};DatabaseName={db};UID={uid};PWD={pwd};"),
        (f"Host=localhost:{port}",
         f"DRIVER={{{drv}}};Host=localhost:{port};DatabaseName={db};UID={uid};PWD={pwd};"),
        (f"Host=127.0.0.1:{port}",
         f"DRIVER={{{drv}}};Host=127.0.0.1:{port};DatabaseName={db};UID={uid};PWD={pwd};"),
    ]
    last = None
    for label, cs in attempts:
        try:
            conn = pyodbc.connect(cs, autocommit=True, timeout=5)
            log(f"Connected via {label}")
            return conn
        except Exception as e:
            log(f"  ✗ {label}: {str(e)[:60]}")
            last = e
    raise ConnectionError(f"All attempts failed. Last: {last}")


def _get_sqlite(cfg):
    import sqlite3
    p = get_db_path()
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sync_doctors (
            code TEXT PRIMARY KEY, name TEXT, rate REAL,
            department TEXT, avgcontime INTEGER,
            qualification TEXT, synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_doctorstiming (
            slno INTEGER PRIMARY KEY, code TEXT,
            t1 REAL, t2 REAL, synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_misel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firm_name TEXT, address1 TEXT, synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_department (
            code TEXT PRIMARY KEY, name TEXT, synced_at TEXT
        );
    """)
    conn.commit()


def do_sync(cfg, log_fn):
    """Run one full sync cycle. log_fn(str) receives status lines."""
    now_iso = datetime.now(timezone.utc).isoformat()
    import sqlite3

    log_fn("Connecting to source database …")
    try:
        src = _get_src_conn(cfg, log_fn)
    except Exception as e:
        log_fn(f"ERROR: {e}")
        return False, str(e)

    try:
        cur = src.cursor()

        cur.execute("SELECT code,name,rate,department,avgcontime,qualification FROM DBA.hms_doctors")
        doctors = _rows_to_dicts(cur)
        log_fn(f"  Doctors fetched: {len(doctors)}")

        cur.execute("SELECT slno,code,t1,t2 FROM DBA.hms_doctorstiming")
        timings = _rows_to_dicts(cur)
        log_fn(f"  Timings fetched: {len(timings)}")

        cur.execute("SELECT TOP 1 firm_name,address1 FROM DBA.misel")
        misel_rows = _rows_to_dicts(cur)
        misel = misel_rows[0] if misel_rows else {}
        log_fn(f"  Hospital: {misel.get('firm_name','?')}")

        cur.execute("SELECT code,name FROM DBA.hms_department")
        depts = _rows_to_dicts(cur)
        log_fn(f"  Departments fetched: {len(depts)}")
    except Exception as e:
        log_fn(f"ERROR fetching: {e}")
        src.close()
        return False, str(e)
    finally:
        try: src.close()
        except: pass

    log_fn("Writing to local database \u2026")
    try:
        lite = _get_sqlite(cfg)
        _ensure_tables(lite)
        c = lite.cursor()

        # \u2500\u2500 Truncate all tables first, then insert fresh data \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        log_fn("  Clearing old data \u2026")
        c.execute("DELETE FROM sync_doctors")
        c.execute("DELETE FROM sync_doctorstiming")
        c.execute("DELETE FROM sync_misel")
        c.execute("DELETE FROM sync_department")

        # \u2500\u2500 Insert doctors \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        c.executemany(
            "INSERT INTO sync_doctors (code,name,rate,department,avgcontime,qualification,synced_at) VALUES (?,?,?,?,?,?,?)",
            [(d.get("code"),d.get("name"),d.get("rate"),d.get("department"),
              d.get("avgcontime"),d.get("qualification"),now_iso) for d in doctors])

        # \u2500\u2500 Insert timings \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        c.executemany(
            "INSERT INTO sync_doctorstiming (slno,code,t1,t2,synced_at) VALUES (?,?,?,?,?)",
            [(t.get("slno"),t.get("code"),t.get("t1"),t.get("t2"),now_iso) for t in timings])

        # \u2500\u2500 Insert hospital info \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if misel:
            c.execute("INSERT INTO sync_misel (firm_name,address1,synced_at) VALUES (?,?,?)",
                      (misel.get("firm_name"),misel.get("address1"),now_iso))

        # \u2500\u2500 Insert departments \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        c.executemany(
            "INSERT INTO sync_department (code,name,synced_at) VALUES (?,?,?)",
            [(d.get("code"),d.get("name"),now_iso) for d in depts])

        lite.commit()
        lite.close()
    except Exception as e:
        log_fn(f"ERROR writing SQLite: {e}")
        return False, str(e)

    summary = (f"✓  Sync complete — "
               f"{len(doctors)} doctors, {len(timings)} timings, "
               f"{len(depts)} departments")
    log_fn(summary)
    return True, summary


# ──────────────────────────────────────────────────────────────────────────
#  Single-instance guard (TCP socket approach — works in .exe)
# ──────────────────────────────────────────────────────────────────────────

class SingleInstance:
    """Try to bind a local socket. If already bound → another instance runs."""

    def __init__(self, port=_LOCK_PORT):
        self.port = port
        self._sock = None

    def try_acquire(self):
        """Returns True if this is the first instance."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", self.port))
            s.listen(5)
            self._sock = s
            threading.Thread(target=self._listen, daemon=True).start()
            return True
        except OSError:
            return False

    def _listen(self):
        """Listen for bring-to-front signals from subsequent instances."""
        while True:
            try:
                conn, _ = self._sock.accept()
                data = conn.recv(64).decode("utf-8", errors="ignore").strip()
                conn.close()
                if data == "SHOW" and _app_window:
                    _app_window.after(0, _app_window.bring_to_front)
            except Exception:
                break

    def signal_existing(self):
        """Send SHOW to the already-running instance."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", self.port))
            s.sendall(b"SHOW")
            s.close()
        except Exception:
            pass


_app_window = None   # global reference so the socket thread can reach it


# ──────────────────────────────────────────────────────────────────────────
#  Toast notification helper
# ──────────────────────────────────────────────────────────────────────────

def notify(title, msg, icon_path=None):
    if HAS_TOAST:
        try:
            _plyer_notify.notify(
                title=title,
                message=msg,
                app_name="DRS Sync",
                app_icon=icon_path or "",
                timeout=4,
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
#  Main window
# ──────────────────────────────────────────────────────────────────────────

DARK_BG   = "#1a1f2e"
PANEL_BG  = "#242938"
ACCENT    = "#4f8ef7"
SUCCESS   = "#3dd68c"
ERROR     = "#f76f6f"
WARNING   = "#f5a623"
TEXT      = "#e8ecf4"
MUTED     = "#8b92a5"
BORDER    = "#2e3446"
# ── Embedded DRS icon (base64 PNG) — always available even inside .exe ──
_DRS_ICON_B64 = (
    "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"
    "AN1y47VWo8bb2RnGLbTveM9z8NiPayfUFfz/AMPUTzfAdOKpfWRhWzbAwgpyxdCMW0t6VRJK75nlb28z"
    "r44zD7zP1mFfNs1r05Uq+Z42rCSs4zrykn8zY9rLJXkdY82/h39nm0jT2W5lUwazKjiNxtOdKMpLg+uM"
    "TY1tq2nXTe7i5X6EqM/zidBAr1y368s4ev8Ai+v1NtC1DnDpwo43E4ChBd7oV3Hed3xbVus4elqfUVO+"
    "7neYO/8Aaryl6WcQCjcjHSsaiG4x2Ox2OlGWNxmIxUo9y61WU2vJdm3AC4AAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAP//Z"
)

def _load_drs_icon(size):
    """Load DRS icon from embedded base64. Returns ImageTk.PhotoImage or None."""
    try:
        import base64, io
        from PIL import Image, ImageTk
        data = base64.b64decode(_DRS_ICON_B64)
        img  = Image.open(io.BytesIO(data)).convert("RGBA")
        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))
    except Exception:
        return None

# ── Embedded IMC Business Solutions logo (base64 PNG) ──────────────────────
_IMC_LOGO_B64 = (
    "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"
)

def _load_imc_logo(height):
    """Load IMC logo (transparent PNG) composited onto dark panel background."""
    try:
        import base64, io
        from PIL import Image, ImageTk
        data = base64.b64decode(_IMC_LOGO_B64)
        img  = Image.open(io.BytesIO(data)).convert("RGBA")
        ratio = height / img.height
        w = int(img.width * ratio)
        img = img.resize((w, height), Image.LANCZOS)
        # Composite onto PANEL_BG (#242938)
        bg = Image.new("RGBA", img.size, (36, 41, 56, 255))
        bg.paste(img, mask=img.split()[3])
        return ImageTk.PhotoImage(bg.convert("RGB"))
    except Exception:
        return None




class StatusWindow(tk.Tk):
    def __init__(self, cfg):
        super().__init__()
        global _app_window
        _app_window = self

        self.cfg      = cfg
        self._sync_th = None
        self._running = True
        self._log_lines = []

        # ── Kill the default Tk feather icon immediately ──────────────────
        # Load real icon right away so feather never shows
        try:
            self._early_icon_ref = _load_drs_icon(64)
            if self._early_icon_ref:
                self.wm_iconphoto(True, self._early_icon_ref)
        except Exception:
            pass

        self._setup_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.title(_APP_NAME)
        self.resizable(False, False)
        self.configure(bg=DARK_BG)

        # ── Set window + taskbar icon (Win32 direct method) ──────────────
        self._icon_ref_small = None
        self._icon_ref_large = None

        def _apply_icon():
            base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
                   else Path(__file__).parent

            # Search for icon files with common naming variants
            ico = None
            for name in ["DRS_icon.ico"]:
                p = base / name
                if p.exists():
                    ico = p
                    break

            png = None
            for name in ["DRS_icon.png"]:
                p = base / name
                if p.exists():
                    png = p
                    break

            # ── Step 1: wm_iconphoto via PIL (title bar) ────────────────────
            pil_ok = False
            if png or ico:
                try:
                    from PIL import Image, ImageTk
                    src = png or ico
                    img = Image.open(str(src)).convert("RGBA")
                    self._icon_ref_large = ImageTk.PhotoImage(
                        img.resize((64, 64), Image.LANCZOS))
                    self._icon_ref_small = ImageTk.PhotoImage(
                        img.resize((32, 32), Image.LANCZOS))
                    self.wm_iconphoto(True,
                                      self._icon_ref_large,
                                      self._icon_ref_small)
                    pil_ok = True
                except Exception:
                    pass

            # ── Step 2: Win32 SendMessage with CORRECT top-level HWND ───────
            # winfo_id() gives the child/embedded HWND; GetParent() gets
            # the real top-level window that appears in the taskbar.
            if ico and ico.exists():
                try:
                    import ctypes
                    LR_LOADFROMFILE = 0x00000010
                    IMAGE_ICON      = 1
                    ICON_SMALL      = 0
                    ICON_BIG        = 1
                    WM_SETICON      = 0x0080

                    user32 = ctypes.windll.user32

                    # Get the REAL top-level HWND (not the embedded child HWND)
                    child_hwnd = self.winfo_id()
                    hwnd = user32.GetParent(child_hwnd)
                    if not hwnd:
                        hwnd = child_hwnd   # fallback if no parent

                    # Load icons at multiple sizes for best quality
                    hicon_16 = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                    hicon_32 = user32.LoadImageW(None, str(ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                    hicon_48 = user32.LoadImageW(None, str(ico), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)

                    # Send to BOTH parent (taskbar) and child (title bar)
                    for h in [hwnd, child_hwnd]:
                        if hicon_32:
                            user32.SendMessageW(h, WM_SETICON, ICON_BIG,   hicon_32)
                        if hicon_16:
                            user32.SendMessageW(h, WM_SETICON, ICON_SMALL, hicon_16)

                    # Also call iconbitmap — belt and suspenders
                    try:
                        self.iconbitmap(default=str(ico))
                    except Exception:
                        pass

                    # Force taskbar to refresh this window's icon
                    try:
                        WM_SETREDRAW = 0x000B
                        user32.SendMessageW(hwnd, WM_SETREDRAW, 0, 0)
                        user32.SendMessageW(hwnd, WM_SETREDRAW, 1, 0)
                        user32.RedrawWindow(hwnd, None, None, 0x0081)  # RDW_INVALIDATE|RDW_UPDATENOW
                    except Exception:
                        pass

                except Exception:
                    if not pil_ok:
                        try:
                            self.iconbitmap(default=str(ico))
                        except Exception:
                            pass

        # Must run after window handle (HWND) exists — use after(0) + update
        self.update_idletasks()
        self.after(0, _apply_icon)

        # ── Title bar area ────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=ACCENT, height=4)
        hdr.pack(fill="x")

        title_frame = tk.Frame(self, bg=DARK_BG, padx=24, pady=18)
        title_frame.pack(fill="x")

        # Load DRS icon from embedded base64 — works inside .exe too
        self._title_icon_ref = _load_drs_icon(40)

        title_inner = tk.Frame(title_frame, bg=DARK_BG)
        title_inner.pack(side="left")
        if self._title_icon_ref:
            lbl_icon = tk.Label(title_inner, image=self._title_icon_ref, bg=DARK_BG)
            lbl_icon.image = self._title_icon_ref   # prevent GC
            lbl_icon.pack(side="left", padx=(0, 10))
        tk.Label(title_inner, text="DRS Sync", font=("Segoe UI", 22, "bold"),
                 bg=DARK_BG, fg=TEXT).pack(side="left")

        self._status_dot = tk.Label(title_frame, text="●", font=("Segoe UI", 14),
                                    bg=DARK_BG, fg=MUTED)
        self._status_dot.pack(side="right", padx=(0, 4))

        # ── DB name banner ────────────────────────────────────────────────
        db_name = self.cfg.get('source_db', {}).get('database', '—')
        db_banner = tk.Frame(self, bg=PANEL_BG, padx=20, pady=7)
        db_banner.pack(fill='x')
        tk.Label(db_banner, text='Source DB:',
                 font=('Segoe UI', 9), bg=PANEL_BG, fg=MUTED).pack(side='left')
        self._db_banner_lbl = tk.Label(db_banner, text=f'  {db_name}',
                 font=('Segoe UI', 10, 'bold'), bg=PANEL_BG, fg=ACCENT)
        self._db_banner_lbl.pack(side='left')

        # ── Stats row ─────────────────────────────────────────────────────
        stats_frame = tk.Frame(self, bg=PANEL_BG, padx=20, pady=14)
        stats_frame.pack(fill="x", padx=16, pady=(0, 8))
        stats_frame.grid_columnconfigure((0,1,2,3), weight=1)

        self._stat_vars = {}
        for col, (label, key) in enumerate([
            ("Last Sync",    "last_sync"),
            ("Doctors",      "doctors"),
            ("Departments",  "depts"),
        ]):
            cell = tk.Frame(stats_frame, bg=PANEL_BG)
            cell.grid(row=0, column=col, padx=8, sticky="ew")
            tk.Label(cell, text=label, font=("Segoe UI", 9),
                     bg=PANEL_BG, fg=MUTED).pack(anchor="w")
            var = tk.StringVar(value="—")
            self._stat_vars[key] = var
            tk.Label(cell, textvariable=var, font=("Segoe UI", 16, "bold"),
                     bg=PANEL_BG, fg=TEXT).pack(anchor="w")

        # ── Log area ──────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=DARK_BG, padx=16, pady=4)
        log_frame.pack(fill="both", expand=True)

        tk.Label(log_frame, text="Sync Log", font=("Segoe UI", 9, "bold"),
                 bg=DARK_BG, fg=MUTED).pack(anchor="w", pady=(0, 4))

        txt_frame = tk.Frame(log_frame, bg=BORDER, bd=1)
        txt_frame.pack(fill="both", expand=True)

        self._log_txt = tk.Text(
            txt_frame,
            height=18, width=100,
            font=("Consolas", 10),
            bg="#0e1117", fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            state="disabled",
            padx=8, pady=8,
        )
        sb = tk.Scrollbar(txt_frame, command=self._log_txt.yview,
                          bg=PANEL_BG, troughcolor=DARK_BG)
        self._log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_txt.pack(fill="both", expand=True)

        # Color tags
        self._log_txt.tag_config("ok",   foreground=SUCCESS)
        self._log_txt.tag_config("err",  foreground=ERROR)
        self._log_txt.tag_config("warn", foreground=WARNING)
        self._log_txt.tag_config("info", foreground=MUTED)
        self._log_txt.tag_config("head", foreground=ACCENT)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=DARK_BG, padx=16, pady=12)
        btn_frame.pack(fill="x")

        self._sync_btn = tk.Button(
            btn_frame, text="  ⟳  Sync Now",
            font=("Segoe UI", 10, "bold"),
            bg=ACCENT, fg="white", activebackground="#3a7de8",
            relief="flat", cursor="hand2", padx=16, pady=8,
            command=self._manual_sync,
        )
        self._sync_btn.pack(side="left")

        tk.Button(
            btn_frame, text="Clear Log",
            font=("Segoe UI", 9),
            bg=PANEL_BG, fg=MUTED, activebackground=BORDER,
            relief="flat", cursor="hand2", padx=12, pady=8,
            command=self._clear_log,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btn_frame, text="⚙  DB Settings",
            font=("Segoe UI", 9),
            bg=PANEL_BG, fg=WARNING, activebackground=BORDER,
            relief="flat", cursor="hand2", padx=12, pady=8,
            command=self._open_db_settings,
        ).pack(side="left", padx=(8, 0))


        # ── Powered by IMC footer ─────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")   # thin separator
        footer = tk.Frame(self, bg=PANEL_BG, padx=20, pady=10)
        footer.pack(fill="x", side="bottom")

        tk.Label(footer, text="Powered by",
                 font=("Segoe UI", 9), bg=PANEL_BG, fg=MUTED).pack(side="left", padx=(0, 10))

        self._imc_logo_ref = _load_imc_logo(32)
        if self._imc_logo_ref:
            lbl_imc = tk.Label(footer, image=self._imc_logo_ref, bg=PANEL_BG,
                               cursor="hand2")
            lbl_imc.image = self._imc_logo_ref
            lbl_imc.pack(side="left")
        else:
            tk.Label(footer, text="IMC Business Solutions",
                     font=("Segoe UI", 9, "bold"), bg=PANEL_BG, fg=TEXT).pack(side="left")

        # ── Center window ─────────────────────────────────────────────────
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = max(w, 860)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._log("DRS Sync started", "head")
        self._log(f"Config: {CONFIG_FILE}", "info")
        self._log(f"Config: {CONFIG_FILE}", "info")
        db_name = self.cfg.get("source_db", {}).get("database", "?")
        self._log(f"Source DB: {db_name}", "ok")

    # ── Logging ───────────────────────────────────────────────────────────

    def _log(self, msg, tag=""):
        ts  = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}\n"

        # Auto-detect colour from message content if no tag given
        if not tag:
            m = msg.strip().lower()
            if any(k in m for k in ("error", "failed", "fail", "fatal", "denied", "exception", "\u2717")):
                tag = "err"
            elif any(k in m for k in ("\u2713", "sync complete", "complete", "connected via", "saved", "ready")):
                tag = "ok"
            elif any(k in m for k in ("\u2500\u2500", "sync started", "drs sync started", "source db:")):
                tag = "head"
            elif any(k in m for k in ("warning", "warn", "\u26a0")):
                tag = "warn"
            elif any(k in m for k in ("config:", "writing", "clearing", "connecting",
                                       "fetched", "hospital:", "doctors fetched",
                                       "timings fetched", "departments fetched")):
                tag = "ok"
            else:
                tag = "info"

        self._log_txt.configure(state="normal")
        self._log_txt.insert("end", line, tag)
        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")

    def _clear_log(self):
        self._log_txt.configure(state="normal")
        self._log_txt.delete("1.0", "end")
        self._log_txt.configure(state="disabled")

    # ── Sync orchestration ────────────────────────────────────────────────

    def _do_sync_thread(self, manual=False):
        self.after(0, lambda: self._set_syncing(True))
        self.after(0, lambda: self._log("── Sync started ──", "head"))

        try:
            cfg    = load_config()
            ok, msg = do_sync(cfg, lambda m: self.after(0, lambda m=m: self._log(m)))
        except Exception as e:
            ok, msg = False, str(e)
            self.after(0, lambda: self._log(f"FATAL: {e}", "err"))

        def _finish():
            self._set_syncing(False)
            if ok:
                self._status_dot.configure(fg=SUCCESS)
                self._stat_vars["last_sync"].set(datetime.now().strftime("%H:%M"))
                self._refresh_counts()
                notify(_APP_NAME, msg,
                       icon_path=str(Path(sys.executable).parent / "DRS_icon.ico")
                       if getattr(sys, "frozen", False) else None)
            else:
                self._status_dot.configure(fg=ERROR)
                notify(_APP_NAME + " — Error", msg)

        self.after(0, _finish)

    def _set_syncing(self, syncing: bool):
        if syncing:
            self._sync_btn.configure(state="disabled", text="  ⟳  Syncing …")
            self._status_dot.configure(fg=WARNING)
        else:
            self._sync_btn.configure(state="normal", text="  ⟳  Sync Now")

    def _manual_sync(self):
        if self._sync_th and self._sync_th.is_alive():
            return
        self._sync_th = threading.Thread(
            target=self._do_sync_thread, args=(True,), daemon=True)
        self._sync_th.start()


    def _refresh_counts(self):
        try:
            import sqlite3
            cfg  = load_config()
            path = get_db_path()
            conn = sqlite3.connect(path)
            c    = conn.cursor()
            c.execute("SELECT COUNT(*) FROM sync_doctors")
            self._stat_vars["doctors"].set(str(c.fetchone()[0]))
            c.execute("SELECT COUNT(*) FROM sync_department")
            self._stat_vars["depts"].set(str(c.fetchone()[0]))
            conn.close()
        except Exception:
            pass


    # ── Bring to front ────────────────────────────────────────────────────

    def bring_to_front(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        # Flash taskbar
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:
            pass

    # ── DB Settings dialog ────────────────────────────────────────────────

    def _open_db_settings(self):
        """Simple dialog — change the database name written to config.json."""
        dlg = tk.Toplevel(self)
        dlg.title("Database Settings")
        dlg.configure(bg=DARK_BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 360) // 2
        y = self.winfo_y() + (self.winfo_height() - 210) // 2
        dlg.geometry(f"360x210+{x}+{y}")

        tk.Frame(dlg, bg=ACCENT, height=4).pack(fill="x")
        tk.Label(dlg, text="⚙  Change Database",
                 font=("Segoe UI", 13, "bold"),
                 bg=DARK_BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")

        form = tk.Frame(dlg, bg=DARK_BG, padx=20)
        form.pack(fill="x")

        tk.Label(form, text="Database Name",
                 font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, anchor="w"
                 ).pack(fill="x", pady=(0, 4))

        cur_db = self.cfg.get("source_db", {}).get("database", "SHADEDB")
        e_db = tk.Entry(form, font=("Segoe UI", 11),
                        bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
                        relief="flat", bd=8)
        e_db.insert(0, cur_db)
        e_db.pack(fill="x", ipady=5)
        e_db.focus_set()
        e_db.select_range(0, "end")

        tk.Label(form, text="This value is saved to config.json next to the exe.",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED).pack(anchor="w", pady=(6, 0))

        status_var = tk.StringVar()
        status_lbl = tk.Label(dlg, textvariable=status_var,
                              font=("Segoe UI", 9), bg=DARK_BG, fg=SUCCESS)
        status_lbl.pack(pady=(6, 0))

        def save():
            db = e_db.get().strip()
            if not db:
                status_var.set("✗  Database name cannot be empty")
                status_lbl.configure(fg=ERROR)
                return
            # Update in-memory config
            self.cfg["source_db"]["database"] = db
            self.cfg["source_db"]["server"]   = db
            # Write simple flat config.json
            try:
                with open(CONFIG_FILE, "w") as f:
                    json.dump({"database": db}, f, indent=4)
                status_var.set(f"✓  Saved!  DB = {db}")
                status_lbl.configure(fg=SUCCESS)
                self._refresh_db_banner(db)
                self.after(1200, dlg.destroy)
            except Exception as ex:
                status_var.set(f"✗  Could not save: {ex}")
                status_lbl.configure(fg=ERROR)

        btn_row = tk.Frame(dlg, bg=DARK_BG, padx=20, pady=12)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Save & Close",
                  font=("Segoe UI", 10, "bold"),
                  bg=ACCENT, fg="white", activebackground="#3a7de8",
                  relief="flat", cursor="hand2", padx=16, pady=7,
                  command=save).pack(side="left")
        tk.Button(btn_row, text="Cancel",
                  font=("Segoe UI", 9),
                  bg=PANEL_BG, fg=MUTED, activebackground=BORDER,
                  relief="flat", cursor="hand2", padx=12, pady=7,
                  command=dlg.destroy).pack(side="left", padx=(8, 0))
        dlg.bind("<Return>", lambda e: save())

    def _refresh_db_banner(self, db_name):
        """Update the Source DB label shown in the header banner."""
        try:
            self._db_banner_lbl.configure(text=f"  {db_name}")
        except Exception:
            pass


    # ── Close ─────────────────────────────────────────────────────────────

    def _on_close(self):
        if messagebox.askokcancel(
                "Quit", "Stop DRS Sync?"):
            self._running = False
            self.destroy()


# ──────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────

def _set_app_icon_early():
    """
    Must be called BEFORE the Tk window is created.
    Sets the Windows AppUserModelID so the taskbar groups/icons correctly,
    and pre-loads the icon into the process so Windows picks it up immediately.
    """
    try:
        import ctypes
        # ── 1. Set AppUserModelID (Windows 7+) ────────────────────────────
        # This is the PRIMARY control for the taskbar icon on Win 10/11.
        # It must match a registered app or just be a unique string.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DRSSyncTool.DRSSync.2"
        )
    except Exception:
        pass

    try:
        import ctypes
        # ── 2. Set process default icon via ExtractIcon ────────────────────
        # When the exe has an embedded icon (--icon in PyInstaller),
        # this makes Windows use it before the window even appears.
        exe = sys.executable if getattr(sys, "frozen", False) else ""
        if exe:
            hicon = ctypes.windll.shell32.ExtractIconW(0, exe, 0)
            if hicon and hicon != 1:
                # Store it as process icon
                pass  # ExtractIcon is enough to register with shell
    except Exception:
        pass


def main():
    # Set AppUserModelID BEFORE creating any window — this is critical
    _set_app_icon_early()

    si = SingleInstance()
    if not si.try_acquire():
        # Another instance is running — bring its window to front
        si.signal_existing()
        sys.exit(0)

    try:
        cfg = load_config()
    except Exception as e:
        messagebox.showerror("DRS Sync — Config Error",
                             f"Cannot load config.json:\n\n{e}")
        sys.exit(1)

    app = StatusWindow(cfg)
    app.mainloop()


if __name__ == "__main__":
    main()