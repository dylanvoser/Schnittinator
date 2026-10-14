# -*- coding: utf-8 -*-
"""
bohrtool_gui.py — Schnittinator: Bohrungserkennung aus STEP (GUI, v3.0)

Start:  python bohrtool_gui.py

Layout (alle Bereiche an den Trennstegen verstellbar, Seitenleisten
einklappbar):
  links  : Bohrtabelle (Typen) + Schnittführung
  Mitte  : Zeichnung mit eigener Werkzeugleiste
  rechts : Bohrungen im Schnitt — Schnitt A (automatisch) und
           Schnitt B (manuell) als Tabellen nebeneinander

Bedienung Grafik:
  Mausrad            = Zoom um den Cursor
  Rechte Maustaste   = Ansicht verschieben
  Linke Maustaste    = Bohrung anklicken -> für Schnitt B (manuell) auswählen/abwählen
  ⌂ / Taste F        = Gesamtansicht, Markierung wird entfernt
  ◀ ▶ / Alt+←/→      = zwischen letzten Ansichten wechseln
Tastenkürzel: Strg+O öffnen, Strg+S Einstellungen sichern, Strg+E PNG,
              Strg+1 / Strg+2 linke / rechte Seitenleiste ein/aus.
Bohrtabelle: 👁-Spalte blendet einzelne Bohrnummern aus/ein, Klick auf die
👁-Kopfzeile blendet alle aus/ein.
"""
import csv
import json
import math
import os
import sys
import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2Tk)

import hole_detection as hd
import hole_render as hr

APP_VERSION = "3.0"
VIEWS = ["top", "bottom", "front", "back", "left", "right"]

# ------------------------------------------------------------------ Farben
# Ruhige Grautöne, EINE Akzentfarbe (Blau aus dem Programm-Icon); Violett /
# Orange sind die Farben der Schnitte A / B (ebenfalls aus dem Icon).
C = {
    "bg":        "#e9edf2",   # Fensterhintergrund
    "panel":     "#ffffff",   # Karten / Tabellen
    "panel_alt": "#f5f7fa",   # Zebra-Zeilen, Kopfzeilen
    "border":    "#d3dae3",
    "text":      "#1c2633",
    "muted":     "#5d6b7c",
    "header":    "#152536",   # Titelleiste (dunkel, CAD-artig)
    "header_2":  "#1f3449",
    "header_3":  "#2b4661",
    "header_tx": "#e6edf5",
    "accent":    "#1565c0",
    "accent_hv": "#0f4f99",
    "accent_lt": "#dce9f9",
    "sec_a":     hr.COL_SECTION,
    "sec_b":     hr.COL_SECTION2,
    "thread":    hr.COL_THREAD,
    "cross":     hr.COL_CROSS,
    "ok":        "#1e8e5a",
    "warn":      "#c77700",
}
FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
MONO = "Consolas"


def _ui_state_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Schnittinator", "ui.json")


def _enable_windows_dpi_awareness():
    """Scharfe Darstellung bei Windows-Skalierung 125–150 %."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class Tooltip:
    """Kleiner Hinweistext beim Überfahren mit der Maus."""

    def __init__(self, widget, text, delay=450):
        self.widget, self.text, self.delay = widget, text, delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(tw, text=self.text, bg=C["header"], fg=C["header_tx"],
                 font=(FONT, 9), padx=8, pady=4, justify="left").pack()
        tw.wm_geometry(f"+{x}+{y}")
        self._tip = tw

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class NavToolbar(NavigationToolbar2Tk):
    """Matplotlib-Toolbar, unsichtbar: liefert Ansichts-Historie (Vor/
    Zurück), Zoom-Rechteck und Verschieben für die eigenen Knöpfe. 'Home' =
    Gesamtansicht + Markierung entfernen. Mauskoordinaten gehen in die
    Statusleiste."""

    def __init__(self, canvas, parent, app):
        self.app = app
        super().__init__(canvas, parent, pack_toolbar=False)

    def home(self, *args):
        self.app.reset_view()

    def set_message(self, s):
        try:
            self.app._set_coords(s)
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Schnittinator {APP_VERSION}")
        self.configure(bg=C["bg"])
        # Pixel-Faktor fuer hohe DPI (1.0 bei 100 % Windows-Skalierung)
        try:
            self._k = max(1.0, float(self.tk.call("tk", "scaling")) / (96/72))
        except Exception:
            self._k = 1.0
        self._ui = self._load_ui_state()
        self._init_geometry()
        self._set_icon()
        self._setup_style()

        self.shape = None
        self.geo = None
        self.result = None
        self.step_path = None
        self.thread_map = dict(hd.DEFAULT_THREAD_CORE_MAP)
        self.thread_overrides = {}
        self.force_keys = set()
        self.skip_keys = set()
        self.hidden_keys = set()
        self.deleted_keys = set()
        self._delete_stack = []
        self.manual_selection = []       # Feature-Objekte fuer Schnitt B
        self._section_cache = {}         # teurer Stufenschnitt A-A, s. hd.analyze_view
        self._section_feats = []
        self._section2_feats = []
        self._hl_artist = None
        self._pan_start = None
        self._last_scroll = 0.0
        self._home_lims = None
        self._busy = False
        self._pending_import = None      # s. import_settings/_apply_pending_import

        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._restore_panes)

    def px(self, n):
        """Pixelmass DPI-gerecht skalieren."""
        return int(round(n * self._k))

    # ------------------------------------------------- Fensterzustand
    def _load_ui_state(self):
        try:
            with open(_ui_state_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _init_geometry(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.minsize(min(self.px(1100), sw), min(self.px(660), sh))
        geo = self._ui.get("geometry")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                geo = None
        if not geo:
            w = min(self.px(1720), sw - 40)
            h = min(self.px(980), sh - 80)
            self.geometry(f"{w}x{h}+{max(0, (sw-w)//2)}+{max(0, (sh-h)//3)}")
        if self._ui.get("zoomed") and sys.platform == "win32":
            try:
                self.state("zoomed")
            except Exception:
                pass

    def _on_close(self):
        try:
            self._save_ui_state()
        except Exception:
            pass
        self.destroy()

    def _save_ui_state(self):
        zoomed = False
        try:
            zoomed = self.state() == "zoomed"
        except Exception:
            pass
        if zoomed:
            geometry = self._ui.get("geometry")
        else:
            geometry = self.geometry()
        data = {
            "geometry": geometry,
            "zoomed": zoomed,
            "left_visible": self._left_visible,
            "right_visible": self._right_visible,
            "left_width": self._pane_width("left"),
            "right_width": self._pane_width("right"),
            "left_split": self._safe_sash(self.left_pw, 0),
            "view": self.view_var.get(),
            "rotation": self.rot_var.get(),
            "direction": self.dir_var.get(),
            "manual_direction": self.dir2_var.get(),
            "last_dir": self._ui.get("last_dir"),
        }
        path = _ui_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    @staticmethod
    def _safe_sash(pw, i):
        try:
            return int(pw.sashpos(i))
        except Exception:
            return None

    def _pane_width(self, which):
        pane = self.left_pane if which == "left" else self.right_pane
        try:
            w = pane.winfo_width()
            return int(w) if w > 50 else None
        except Exception:
            return None

    # --------------------------------------------------------------- Icon
    def _set_icon(self):
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(__file__)))
        self._icon_imgs = []
        try:
            icons = [tk.PhotoImage(file=os.path.join(base, "assets", n))
                    for n in ("icon_128.png", "icon_64.png", "icon_32.png",
                              "icon_16.png")]
            self._icon_imgs = icons     # Referenz halten (sonst GC'd)
            self.iconphoto(True, *icons)
        except Exception:
            pass                        # z.B. Bilder fehlen -> Standardicon

    # ---------------------------------------------------------------- Stil
    def _setup_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        base = (FONT, 9)
        self.option_add("*Font", base)
        self.option_add("*TCombobox*Listbox.font", base)
        self.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        p = self.px

        st.configure(".", background=C["bg"], foreground=C["text"],
                     font=base, bordercolor=C["border"],
                     lightcolor=C["border"], darkcolor=C["border"],
                     troughcolor=C["panel_alt"], focuscolor=C["accent"])
        st.configure("TFrame", background=C["bg"])
        st.configure("Panel.TFrame", background=C["panel"])
        st.configure("Bar.TFrame", background=C["panel"])
        st.configure("Header.TFrame", background=C["header"])
        st.configure("Status.TFrame", background=C["header"])

        st.configure("TLabel", background=C["bg"], foreground=C["text"])
        st.configure("Panel.TLabel", background=C["panel"])
        st.configure("Muted.TLabel", background=C["panel"],
                     foreground=C["muted"], font=(FONT, 8))
        st.configure("Group.TLabel", background=C["panel"],
                     foreground=C["muted"], font=(FONT, 8))
        st.configure("Card.TLabel", background=C["panel"],
                     foreground=C["text"], font=(FONT_SEMI, 10))
        st.configure("Badge.TLabel", background=C["accent_lt"],
                     foreground=C["accent"], font=(FONT_SEMI, 8),
                     padding=(p(7), p(1)))
        st.configure("Title.TLabel", background=C["header"],
                     foreground="#ffffff", font=(FONT_SEMI, 12))
        st.configure("HeaderMuted.TLabel", background=C["header"],
                     foreground="#93a7bd", font=(FONT, 9))
        st.configure("File.TLabel", background=C["header_2"],
                     foreground=C["header_tx"], font=(FONT, 9),
                     padding=(p(10), p(3)))
        st.configure("Status.TLabel", background=C["header"],
                     foreground=C["header_tx"], font=(FONT, 9))
        st.configure("StatusMuted.TLabel", background=C["header"],
                     foreground="#93a7bd", font=(MONO, 9))
        st.configure("Version.TLabel", background=C["header_3"],
                     foreground="#ffffff", font=(FONT_SEMI, 8),
                     padding=(p(8), p(1)))

        # Knöpfe ---------------------------------------------------------
        btn_pad = (p(10), p(5))
        st.configure("TButton", padding=btn_pad, relief="flat",
                     background=C["panel_alt"], foreground=C["text"],
                     bordercolor=C["border"], lightcolor=C["panel_alt"],
                     darkcolor=C["panel_alt"], focusthickness=0,
                     anchor="center")
        st.map("TButton",
               background=[("pressed", "#d5dde7"), ("active", "#e7ecf2"),
                           ("disabled", C["panel_alt"])],
               lightcolor=[("active", "#e7ecf2")],
               darkcolor=[("active", "#e7ecf2")],
               foreground=[("disabled", "#a7b1bd")])
        st.configure("Small.TButton", padding=(p(4), p(5)))
        st.configure("Accent.TButton", background=C["accent"],
                     foreground="#ffffff", bordercolor=C["accent"],
                     lightcolor=C["accent"], darkcolor=C["accent"],
                     font=(FONT_SEMI, 9), padding=(p(14), p(5)))
        st.map("Accent.TButton",
               background=[("pressed", "#0c3f7a"), ("active", C["accent_hv"])],
               lightcolor=[("active", C["accent_hv"])],
               darkcolor=[("active", C["accent_hv"])],
               bordercolor=[("active", C["accent_hv"])])
        st.configure("Head.TButton", background=C["header_2"],
                     foreground=C["header_tx"], bordercolor=C["header_2"],
                     lightcolor=C["header_2"], darkcolor=C["header_2"],
                     padding=(p(10), p(4)))
        st.map("Head.TButton",
               background=[("pressed", C["header"]),
                           ("active", C["header_3"])],
               lightcolor=[("active", C["header_3"])],
               darkcolor=[("active", C["header_3"])],
               bordercolor=[("active", C["header_3"])])
        st.configure("Icon.TButton", padding=(p(8), p(3)),
                     background=C["panel"], bordercolor=C["panel"],
                     lightcolor=C["panel"], darkcolor=C["panel"],
                     font=(FONT, 10))
        st.map("Icon.TButton",
               background=[("pressed", "#d5dde7"), ("active", "#e7ecf2")],
               lightcolor=[("active", "#e7ecf2")],
               darkcolor=[("active", "#e7ecf2")],
               bordercolor=[("active", "#e7ecf2")])
        st.configure("IconOn.TButton", padding=(p(8), p(3)),
                     background=C["accent_lt"], foreground=C["accent"],
                     bordercolor=C["accent"], lightcolor=C["accent_lt"],
                     darkcolor=C["accent_lt"], font=(FONT, 10))
        st.map("IconOn.TButton",
               background=[("active", "#c9dcf4")])

        # Eingabefelder ---------------------------------------------------
        st.configure("TCombobox", padding=(p(6), p(3)),
                     fieldbackground=C["panel"], background=C["panel"],
                     foreground=C["text"], arrowcolor=C["muted"],
                     bordercolor=C["border"], lightcolor=C["panel"],
                     darkcolor=C["panel"], selectbackground=C["panel"],
                     selectforeground=C["text"])
        st.map("TCombobox",
               fieldbackground=[("readonly", C["panel"])],
               bordercolor=[("focus", C["accent"]), ("active", "#aab6c4")],
               arrowcolor=[("active", C["accent"])])

        # Tabellen ---------------------------------------------------------
        st.configure("Treeview", rowheight=p(25), fieldbackground=C["panel"],
                     background=C["panel"], foreground=C["text"],
                     borderwidth=0, font=(FONT, 9))
        st.configure("Treeview.Heading", font=(FONT_SEMI, 8),
                     background=C["panel_alt"], foreground=C["muted"],
                     relief="flat", bordercolor=C["border"],
                     lightcolor=C["panel_alt"], darkcolor=C["border"],
                     padding=(p(4), p(4)))
        st.map("Treeview.Heading", background=[("active", "#e7ecf2")])
        st.map("Treeview", background=[("selected", C["accent"])],
               foreground=[("selected", "#ffffff")])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        st.configure("TPanedwindow", background=C["bg"])
        st.configure("Sash", sashthickness=p(7), gripcount=0,
                     background=C["bg"], lightcolor=C["bg"],
                     bordercolor=C["bg"])
        st.configure("Vertical.TScrollbar", background=C["panel_alt"],
                     troughcolor=C["panel"], bordercolor=C["panel"],
                     arrowcolor=C["muted"], lightcolor=C["panel_alt"],
                     darkcolor=C["panel_alt"], gripcount=0)
        st.configure("Horizontal.TProgressbar", background=C["accent"],
                     troughcolor=C["header_2"], bordercolor=C["header_2"],
                     lightcolor=C["accent"], darkcolor=C["accent"])

    # ------------------------------------------------------- UI-Bausteine
    def _card(self, parent, title, color=None, subtitle=None):
        """Weisse Karte mit Kopfzeile (farbiger Streifen + Titel).
        Rueckgabe: (aeusserer Rahmen, Kopfzeile, Inhalt)."""
        p = self.px
        outer = tk.Frame(parent, bg=C["border"], bd=0, highlightthickness=0)
        inner = tk.Frame(outer, bg=C["panel"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=C["panel"])
        head.pack(fill="x", padx=p(10), pady=(p(8), p(4)))
        tk.Frame(head, bg=color or C["accent"], width=p(4),
                 height=p(16)).pack(side="left", padx=(0, p(8)))
        ttk.Label(head, text=title, style="Card.TLabel").pack(side="left")
        if subtitle:
            ttk.Label(head, text=subtitle, style="Muted.TLabel")\
                .pack(side="left", padx=(p(10), 0))
        body = tk.Frame(inner, bg=C["panel"])
        body.pack(fill="both", expand=True, padx=p(10), pady=(0, p(10)))
        return outer, head, body

    def _tree(self, parent, columns, headings, widths, anchors=None,
              height=8):
        """Treeview mit eigener Scrollleiste und Zebra-Zeilen."""
        wrap = tk.Frame(parent, bg=C["border"])
        inner = tk.Frame(wrap, bg=C["panel"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tv = ttk.Treeview(inner, columns=columns, show="headings",
                          height=height, selectmode="browse")
        sb = ttk.Scrollbar(inner, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        anchors = anchors or {}
        for c in columns:
            tv.heading(c, text=headings.get(c, c),
                       anchor=anchors.get(c, "center"))
            tv.column(c, width=self.px(widths[c]),
                      minwidth=self.px(min(widths[c], 28)),
                      anchor=anchors.get(c, "center"),
                      stretch=(c in anchors and anchors[c] == "w"))
        tv.tag_configure("odd", background=C["panel_alt"])
        tv.tag_configure("thread", foreground=C["thread"])
        tv.tag_configure("cross", foreground=C["cross"])
        tv.tag_configure("dim", foreground="#9aa6b4")
        return wrap, tv

    def _sep(self, parent, bg=None):
        tk.Frame(parent, bg=bg or C["border"], width=1)\
            .pack(side="left", fill="y", padx=self.px(10), pady=self.px(6))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        p = self.px

        # ================= Titelleiste (dunkel) ===========================
        header = ttk.Frame(self, style="Header.TFrame",
                           padding=(p(12), p(8), p(12), p(8)))
        header.pack(side="top", fill="x")
        if self._icon_imgs:
            try:
                self._logo = self._icon_imgs[2]          # 32 px
                tk.Label(header, image=self._logo, bg=C["header"], bd=0)\
                    .pack(side="left", padx=(0, p(8)))
            except Exception:
                pass
        self._lbl_title = ttk.Label(header, text="Schnittinator",
                                    style="Title.TLabel")
        self._lbl_title.pack(side="left")
        self._lbl_tagline = ttk.Label(header,
                                      text="Bohrungen & Stufenschnitt aus STEP",
                                      style="HeaderMuted.TLabel")
        self._lbl_tagline.pack(side="left", padx=(p(10), 0))

        def _responsive(ev):
            # Untertitel nur zeigen, wenn genug Platz ist
            wide = ev.width >= self.px(1560)
            shown = bool(self._lbl_tagline.winfo_manager())
            if wide and not shown:
                self._lbl_tagline.pack(side="left", padx=(p(10), 0),
                                       after=self._lbl_title)
            elif not wide and shown:
                self._lbl_tagline.pack_forget()
        header.bind("<Configure>", _responsive)

        b = ttk.Button(header, text="📂  STEP laden…", style="Accent.TButton",
                       command=self.load_step)
        b.pack(side="left", padx=(p(24), p(8)))
        Tooltip(b, "STEP-Datei öffnen  (Strg+O)")
        self.lbl_file = ttk.Label(header, text="keine Datei geladen",
                                  style="File.TLabel")
        self.lbl_file.pack(side="left")

        # rechts: Export + Einstellungen
        for txt, cmd, tip in (
                ("⬇ PNG", lambda: self.export_fig("png"),
                 "Zeichnung als PNG speichern  (Strg+E)"),
                ("⬇ SVG", lambda: self.export_fig("svg"),
                 "Zeichnung als SVG (Vektor) speichern"),
                ("⬇ CSV", self.export_csv,
                 "Bohrtabelle + Schnittführung als CSV (Excel)")):
            bb = ttk.Button(header, text=txt, style="Head.TButton",
                            command=cmd)
            bb.pack(side="right", padx=(p(4), 0))
            Tooltip(bb, tip)
        tk.Frame(header, bg=C["header_3"], width=1)\
            .pack(side="right", fill="y", padx=p(10))
        bb = ttk.Button(header, text="Laden…", style="Head.TButton",
                        command=self.import_settings)
        bb.pack(side="right", padx=(p(4), 0))
        Tooltip(bb, "Gespeicherte Einstellungen zu einer Platte laden\n"
                    "(Erzwingen, Ausblenden, Schnitt B, Ansicht …)")
        bb = ttk.Button(header, text="Sichern…", style="Head.TButton",
                        command=self.export_settings)
        bb.pack(side="right", padx=(p(4), 0))
        Tooltip(bb, "Alle Einstellungen zu dieser Platte als Datei sichern"
                    "  (Strg+S)")
        ttk.Label(header, text="Einstellungen", style="HeaderMuted.TLabel")\
            .pack(side="right", padx=(0, p(4)))

        # ================= Werkzeugleiste (hell, gruppiert) ===============
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(p(12), p(6)))
        bar.pack(side="top", fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(side="top", fill="x")

        def combo(label, var, values, width, cmd, tip):
            ttk.Label(bar, text=label, style="Group.TLabel")\
                .pack(side="left", padx=(0, p(4)))
            cb = ttk.Combobox(bar, textvariable=var, values=values,
                              width=width, state="readonly")
            cb.pack(side="left", padx=(0, p(12)))
            cb.bind("<<ComboboxSelected>>", lambda e: cmd())
            Tooltip(cb, tip)
            return cb

        self.view_var = tk.StringVar(value=self._ui.get("view", "top"))
        combo("ANSICHT", self.view_var, VIEWS, 8, self.refresh_view,
              "Blickrichtung auf das Bauteil")
        self.rot_var = tk.StringVar(value=self._ui.get("rotation", "0°"))
        combo("DREHUNG", self.rot_var, ["0°", "90°", "180°", "270°"], 5,
              self.refresh_view, "Ansicht in der Bildebene drehen")
        self.dir_var = tk.StringVar(value=self._ui.get("direction",
                                                       "horizontal"))
        combo("SCHNITT A", self.dir_var, ["horizontal", "vertical"], 10,
              self.refresh_view,
              "Hauptrichtung des automatischen Stufenschnitts A-A")
        self._sep(bar)

        # "Gewinde nur mit Ansenkfase": im GUI ausgeblendet (siehe
        # thread_requires_chamfer in hd.build_features) — steuert, ob ein
        # Kernloch ohne modellierten Nennzylinder nur dann als Gewinde
        # gilt, wenn am Eintritt eine Ansenkfase liegt (True, Standard hier)
        # oder ob reine Durchmesser-Tabellenzuordnung genuegt (False).
        # Funktion bleibt aktiv, nur ohne eigenen Schalter im UI.
        self.chamfer_var = tk.BooleanVar(value=True)
        bb = ttk.Button(bar, text="Kernloch-Tabelle…",
                        command=self.edit_thread_map)
        bb.pack(side="left")
        Tooltip(bb, "Zuordnung Kernloch-Ø → Gewinde bearbeiten\n"
                    "(z. B. 6.8 → M8)")

        # Seitenleisten ein/aus (rechts in der Leiste)
        self._left_visible = bool(self._ui.get("left_visible", True))
        self._right_visible = bool(self._ui.get("right_visible", True))
        self.btn_right = ttk.Button(bar, text="Bohrungen im Schnitt ▸",
                                    command=self.toggle_right)
        self.btn_right.pack(side="right")
        Tooltip(self.btn_right, "Rechte Seitenleiste ein/aus  (Strg+2)")
        self.btn_left = ttk.Button(bar, text="◂ Bohrtabelle",
                                   command=self.toggle_left)
        self.btn_left.pack(side="right", padx=(0, p(6)))
        Tooltip(self.btn_left, "Linke Seitenleiste ein/aus  (Strg+1)")
        ttk.Label(bar, text="BEREICHE", style="Group.TLabel")\
            .pack(side="right", padx=(0, p(6)))

        # ================= Statusleiste (zuerst packen -> nie abgeschnitten)
        status = ttk.Frame(self, style="Status.TFrame",
                           padding=(p(12), p(4)))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, text=f"v{APP_VERSION}", style="Version.TLabel")\
            .pack(side="left", padx=(0, p(10)))
        self.lbl_status = ttk.Label(status, text="Bereit — STEP-Datei laden "
                                    "(Strg+O).", style="Status.TLabel")
        self.lbl_status.pack(side="left")
        self.lbl_coords = ttk.Label(status, text="", style="StatusMuted.TLabel")
        self.lbl_coords.pack(side="right")
        self.progress = ttk.Progressbar(status, mode="indeterminate",
                                        length=p(200))
        # (wird nur waehrend der Berechnung angezeigt)

        # ================= Hauptbereich: 3 Spalten ========================
        self.main = ttk.PanedWindow(self, orient="horizontal")
        self.main.pack(fill="both", expand=True, padx=p(8), pady=p(8))

        self._build_left()
        self._build_center()
        self._build_right()

        self.main.add(self.left_pane, weight=0)
        self.main.add(self.center_pane, weight=1)
        self.main.add(self.right_pane, weight=0)
        if not self._left_visible:
            self.main.forget(self.left_pane)
        if not self._right_visible:
            self.main.forget(self.right_pane)
        self._update_panel_buttons()

    # ------------------------------------------- linke Spalte: Bohrtabelle
    def _build_left(self):
        p = self.px
        self.left_pane = ttk.Frame(self.main)
        self.left_pw = ttk.PanedWindow(self.left_pane, orient="vertical")
        self.left_pw.pack(fill="both", expand=True)

        card, head, body = self._card(self.left_pw, "Bohrtabelle")
        self.lbl_types_badge = ttk.Label(head, text="—", style="Badge.TLabel")
        self.lbl_types_badge.pack(side="right")
        self.left_pw.add(card, weight=3)

        cols = ("Nr", "DGewinde", "Art", "Tiefe", "Gewindetiefe", "Anzahl",
                "👁")
        headings = {"Nr": "Nr", "DGewinde": "Ø / Gew.", "Art": "Art",
                    "Tiefe": "Tiefe", "Gewindetiefe": "Gew.-T.",
                    "Anzahl": "Anz.", "👁": "👁"}
        widths = {"Nr": 30, "DGewinde": 64, "Art": 128, "Tiefe": 42,
                  "Gewindetiefe": 50, "Anzahl": 38, "👁": 28}
        # Knoepfe zuerst (unten) packen -> werden bei wenig Hoehe nie
        # abgeschnitten, die Tabelle schrumpft stattdessen
        acts = tk.Frame(body, bg=C["panel"])
        acts.pack(side="bottom", fill="x", pady=(p(8), 0))
        tk.Label(body, text="Zeile anklicken = zur Bohrung springen  ·  "
                            "👁 = Nummer ein/aus", bg=C["panel"],
                 fg=C["muted"], font=(FONT, 8), anchor="w")\
            .pack(side="bottom", fill="x", pady=(p(4), 0))
        wrap, self.tree = self._tree(body, cols, headings, widths,
                                     anchors={"Art": "w"}, height=6)
        wrap.pack(side="top", fill="both", expand=True)
        self.tree.heading("👁", command=self._toggle_all_labels)
        self._eye_col_id = f"#{len(cols)}"
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_click)
        self.tree.bind("<Button-1>", self._on_tree_button1)

        acts.columnconfigure((0, 1), weight=1, uniform="a")
        for i, (txt, cmd, tip) in enumerate((
                ("⇄ Gewinde / Bohrung", self.toggle_thread,
                 "Ausgewählten Typ zwischen Gewinde und Bohrung umschalten"),
                ("⚑ Erzwingen", lambda: self._mark_type("force"),
                 "Typ muss im Schnitt A vorkommen (nochmals = aufheben)"),
                ("∅ Nicht nötig", lambda: self._mark_type("skip"),
                 "Typ muss nicht geschnitten werden (nochmals = aufheben)"),
                ("🗑 Löschen", self._delete_selected_type,
                 "Typ aus der Bohrtabelle entfernen"),
                ("↺ Rückgängig", self._undo_delete,
                 "Letztes Löschen rückgängig machen"))):
            bb = ttk.Button(acts, text=txt, command=cmd)
            if i == 0:
                bb.grid(row=0, column=0, columnspan=2, sticky="ew",
                        pady=(0, p(4)))
            else:
                r, c = divmod(i - 1, 2)
                bb.grid(row=1 + r, column=c, sticky="ew",
                        padx=(0 if c == 0 else p(2), p(2) if c == 0 else 0),
                        pady=(0, p(4)))
            Tooltip(bb, tip)

        card, head, body = self._card(self.left_pw, "Schnittführung")
        self.left_pw.add(card, weight=2)
        wrapt = tk.Frame(body, bg=C["border"])
        wrapt.pack(fill="both", expand=True)
        self.txt = tk.Text(wrapt, font=(MONO, 9), bg=C["panel"],
                           fg=C["text"], bd=0, relief="flat",
                           highlightthickness=0, wrap="none",
                           padx=p(6), pady=p(4))
        sb = ttk.Scrollbar(wrapt, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.txt.pack(side="left", fill="both", expand=True, padx=(1, 0),
                      pady=1)

    # ---------------------------------------------- Mitte: Zeichnung
    def _build_center(self):
        p = self.px
        self.center_pane = ttk.Frame(self.main)
        card, head, body = self._card(self.center_pane, "Zeichnung")
        card.pack(fill="both", expand=True, padx=p(4))
        foot = tk.Frame(body, bg=C["panel"])
        foot.pack(side="bottom", fill="x", pady=(p(6), 0))
        tk.Label(foot, text="Mausrad: Zoom  ·  Rechte Maustaste: verschieben"
                            "  ·  Linksklick auf Bohrung: für Schnitt B "
                            "wählen / abwählen", bg=C["panel"],
                 fg=C["muted"], font=(FONT, 8), anchor="w")\
            .pack(side="left")

        # eigene Werkzeugknöpfe (rechts im Kartenkopf)
        self._nav_btns = {}
        for key, txt, tip in (
                ("pan", "✥", "Verschieben mit linker Maustaste (Umschalter)"),
                ("zoom", "⬚", "Zoom-Rechteck aufziehen (Umschalter)"),
                ("fwd", "▶", "Nächste Ansicht  (Alt+→)"),
                ("back", "◀", "Vorherige Ansicht  (Alt+←)"),
                ("home", "⌂", "Gesamtansicht, Markierung entfernen  (F)")):
            bb = ttk.Button(head, text=txt, style="Icon.TButton", width=3,
                            command=lambda k=key: self._nav(k))
            bb.pack(side="right", padx=(p(2), 0))
            Tooltip(bb, tip)
            self._nav_btns[key] = bb

        k = self._k
        self.fig = Figure(figsize=(8, 6.5), dpi=96 * k,
                          facecolor=hr.SHEET_BG)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        w = self.canvas.get_tk_widget()
        w.configure(highlightthickness=0, bd=0, bg=hr.SHEET_BG)
        w.pack(side="top", fill="both", expand=True)
        self.toolbar = NavToolbar(self.canvas, body, self)
        self._draw_placeholder()

        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)

    def _draw_placeholder(self):
        self.ax.clear()
        hr.style_axes(self.ax)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.ax.text(0.5, 0.54, "Keine Datei geladen",
                     transform=self.ax.transAxes, ha="center", va="center",
                     fontsize=14, color="#5d6b7c")
        self.ax.text(0.5, 0.46, "STEP laden…  oder  Strg+O",
                     transform=self.ax.transAxes, ha="center", va="center",
                     fontsize=10, color="#8a97a6")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ----------------------------- rechte Spalte: Bohrungen im Schnitt
    def _build_right(self):
        p = self.px
        self.right_pane = ttk.Frame(self.main)
        card, head, body = self._card(self.right_pane, "Bohrungen im Schnitt",
                                      subtitle="A = automatisch · "
                                               "B = manuell")
        card.pack(fill="both", expand=True)

        cols = ("Nr", "Bez", "Art", "Lage")
        headings = {"Nr": "Nr", "Bez": "Ø / Gew.", "Art": "Art",
                    "Lage": "Lage"}
        widths = {"Nr": 28, "Bez": 54, "Art": 42, "Lage": 46}

        tables = tk.Frame(body, bg=C["panel"])
        tables.pack(fill="both", expand=True)
        tables.columnconfigure((0, 1), weight=1, uniform="t")
        tables.rowconfigure(1, weight=1)

        def col_head(col, title, color, badge_attr):
            h = tk.Frame(tables, bg=C["panel"])
            h.grid(row=0, column=col, sticky="ew",
                   padx=(0 if col == 0 else p(4), p(4) if col == 0 else 0),
                   pady=(0, p(4)))
            tk.Frame(h, bg=color, height=p(3)).pack(fill="x", pady=(0, p(4)))
            tk.Label(h, text=title, bg=C["panel"], fg=color,
                     font=(FONT_SEMI, 9)).pack(side="left")
            lb = tk.Label(h, text="0", bg=C["panel_alt"], fg=C["muted"],
                          font=(FONT_SEMI, 8), padx=p(6))
            lb.pack(side="right")
            setattr(self, badge_attr, lb)

        col_head(0, "Schnitt A", C["sec_a"], "lbl_badge_a")
        col_head(1, "Schnitt B", C["sec_b"], "lbl_badge_b")

        wa, self.tv_secA = self._tree(tables, cols, headings, widths,
                                      anchors={"Lage": "w"}, height=12)
        wa.grid(row=1, column=0, sticky="nsew", padx=(0, p(4)))
        wb, self.tv_secB = self._tree(tables, cols, headings, widths,
                                      anchors={"Lage": "w"}, height=12)
        wb.grid(row=1, column=1, sticky="nsew", padx=(p(4), 0))
        self.tv_secA.bind("<<TreeviewSelect>>", self._on_sectionA_click)
        self.tv_secB.bind("<<TreeviewSelect>>", self._on_sectionB_click)

        # Steuerung Schnitt A (Hinweis) / Schnitt B (Richtung, Entfernen …)
        ctrl_a = tk.Frame(tables, bg=C["panel"])
        ctrl_a.grid(row=2, column=0, sticky="new", padx=(0, p(4)),
                    pady=(p(8), 0))
        tk.Label(ctrl_a, text="Klick = hinspringen.\nRichtung: „Schnitt A“ "
                              "oben.", bg=C["panel"],
                 fg=C["muted"], font=(FONT, 8), justify="left")\
            .pack(anchor="w")

        ctrl_b = tk.Frame(tables, bg=C["panel"])
        ctrl_b.grid(row=2, column=1, sticky="new", padx=(p(4), 0),
                    pady=(p(8), 0))
        rowd = tk.Frame(ctrl_b, bg=C["panel"])
        rowd.pack(fill="x")
        tk.Label(rowd, text="Richtung", bg=C["panel"], fg=C["muted"],
                 font=(FONT, 8)).pack(side="left")
        self.dir2_var = tk.StringVar(value=self._ui.get("manual_direction",
                                                        "horizontal"))
        cb2 = ttk.Combobox(rowd, textvariable=self.dir2_var,
                           values=["horizontal", "vertical"], width=10,
                           state="readonly")
        cb2.pack(side="right")
        cb2.bind("<<ComboboxSelected>>", lambda e: self._quick_refresh())
        Tooltip(cb2, "Hauptrichtung des manuellen Schnitts B-B")
        rowbtn = tk.Frame(ctrl_b, bg=C["panel"])
        rowbtn.pack(fill="x", pady=(p(6), 0))
        rowbtn.columnconfigure((0, 1), weight=1, uniform="b")
        bb = ttk.Button(rowbtn, text="✕ Entfernen", style="Small.TButton",
                        command=self._remove_selected_manual)
        bb.grid(row=0, column=0, sticky="ew", padx=(0, p(2)))
        Tooltip(bb, "Markierte Bohrung aus Schnitt B entfernen")
        bb = ttk.Button(rowbtn, text="↺ Leeren", style="Small.TButton",
                        command=self._reset_manual_section)
        bb.grid(row=0, column=1, sticky="ew", padx=(p(2), 0))
        Tooltip(bb, "Schnitt B komplett zurücksetzen")

        # gemeinsame Option: Nummerierung
        tk.Frame(body, bg=C["border"], height=1).pack(fill="x",
                                                       pady=(p(10), p(8)))
        rown = tk.Frame(body, bg=C["panel"])
        rown.pack(fill="x")
        tk.Label(rown, text="Bohrnummern fortlaufend entlang", bg=C["panel"],
                 fg=C["muted"], font=(FONT, 8)).pack(side="left")
        self.renumber_var = tk.StringVar(value="Schnitt A")
        cbN = ttk.Combobox(rown, textvariable=self.renumber_var,
                           values=["Schnitt A", "Schnitt B"], width=10,
                           state="readonly")
        cbN.pack(side="right")
        cbN.bind("<<ComboboxSelected>>", lambda e: self._quick_refresh())
        Tooltip(cbN, "Nr. 1 = erste Bohrung im gewählten Schnitt,\n"
                     "danach aufsteigend in Schnittreihenfolge")

    # ------------------------------------------- Seitenleisten / Panes
    def _restore_panes(self):
        """Gespeicherte Breiten der Seitenleisten wiederherstellen (oder
        Standard: Zeichnung bekommt den groessten Teil)."""
        self.update_idletasks()
        total = self.main.winfo_width()
        if total < 200:
            self.after(80, self._restore_panes)
            return
        lw = self._ui.get("left_width") or self.px(390)
        rw = self._ui.get("right_width") or self.px(410)
        # Zeichnung behaelt mindestens ~40 % der Breite
        maxside = int(total * 0.60)
        if self._left_visible and self._right_visible and lw + rw > maxside:
            f = maxside / float(lw + rw)
            lw, rw = int(lw * f), int(rw * f)
        try:
            idx = 0
            if self._left_visible:
                self.main.sashpos(0, int(lw))
                idx = 1
            if self._right_visible:
                self.main.sashpos(idx, total - int(rw))
        except Exception:
            pass
        ls = self._ui.get("left_split")
        if not ls:      # Standard: Bohrtabelle ~60 %, Schnittfuehrung ~40 %
            ls = int(self.left_pw.winfo_height() * 0.6)
        try:
            self.left_pw.sashpos(0, int(ls))
        except Exception:
            pass

    def toggle_left(self):
        if self._left_visible:
            self.main.forget(self.left_pane)
        else:
            self.main.insert(0, self.left_pane, weight=0)
            self.after(30, lambda: self._set_side_width("left"))
        self._left_visible = not self._left_visible
        self._update_panel_buttons()

    def toggle_right(self):
        if self._right_visible:
            self.main.forget(self.right_pane)
        else:
            self.main.add(self.right_pane, weight=0)
            self.after(30, lambda: self._set_side_width("right"))
        self._right_visible = not self._right_visible
        self._update_panel_buttons()

    def _set_side_width(self, which):
        self.update_idletasks()
        total = self.main.winfo_width()
        w = self._ui.get(f"{which}_width") or self.px(440)
        w = min(int(w), int(total * 0.4))
        try:
            if which == "left":
                self.main.sashpos(0, w)
            else:
                n = len(self.main.panes())
                self.main.sashpos(n - 2, total - w)
        except Exception:
            pass

    def _update_panel_buttons(self):
        self.btn_left.configure(
            text=("◂ Bohrtabelle" if self._left_visible else "▸ Bohrtabelle"))
        self.btn_right.configure(
            text=("Bohrungen im Schnitt ▸" if self._right_visible
                  else "Bohrungen im Schnitt ◂"))

    # ---------------------------------------------------- Tastenkürzel
    def _bind_shortcuts(self):
        def guard(fn, plain=False):
            def h(ev):
                # Tasten ohne Strg/Alt nicht abfangen, solange in einem
                # Textfeld geschrieben wird
                if plain and isinstance(ev.widget, (tk.Text, tk.Entry,
                                                    ttk.Entry)):
                    return None
                fn()
                return "break"
            return h
        self.bind_all("<Control-o>", guard(self.load_step))
        self.bind_all("<Control-O>", guard(self.load_step))
        self.bind_all("<Control-s>", guard(self.export_settings))
        self.bind_all("<Control-S>", guard(self.export_settings))
        self.bind_all("<Control-e>", guard(lambda: self.export_fig("png")))
        self.bind_all("<Control-E>", guard(lambda: self.export_fig("png")))
        self.bind_all("<Control-Key-1>", guard(self.toggle_left))
        self.bind_all("<Control-Key-2>", guard(self.toggle_right))
        self.bind_all("<Alt-Left>", guard(lambda: self._nav("back")))
        self.bind_all("<Alt-Right>", guard(lambda: self._nav("fwd")))
        self.bind_all("<Home>", guard(self.reset_view, plain=True))
        self.bind_all("<Key-f>", guard(self.reset_view, plain=True))
        self.bind_all("<Escape>", guard(self._nav_off, plain=True))

    # ---------------------------------------------------- Hilfsfunktionen
    def _rotation(self):
        return int(self.rot_var.get().rstrip("°"))

    def _toolbar_active(self):
        return bool(str(getattr(self.toolbar, "mode", "") or ""))

    def _set_coords(self, s):
        s = (s or "").replace("\n", "  ").strip()
        if s.startswith("x=") or "x=" in s:
            s = s.replace("x=", "u = ").replace("y=", "v = ")
        self.lbl_coords.configure(text=s)

    def _nav(self, key):
        tb = self.toolbar
        if key == "home":
            self.reset_view()
        elif key == "back":
            tb.back()
        elif key == "fwd":
            tb.forward()
        elif key == "zoom":
            tb.zoom()
        elif key == "pan":
            tb.pan()
        self._update_nav_buttons()

    def _nav_mode(self):
        """'zoom', 'pan' oder '' (matplotlib: _Mode.ZOOM / _Mode.PAN)."""
        m = getattr(self.toolbar, "mode", None)
        name = str(getattr(m, "name", "") or "").upper()
        if not name:                       # aeltere matplotlib-Versionen
            txt = str(m or "").lower()
            name = ("ZOOM" if txt.startswith("zoom") else
                    "PAN" if txt.startswith("pan") else "")
        return {"ZOOM": "zoom", "PAN": "pan"}.get(name, "")

    def _nav_off(self):
        mode = self._nav_mode()
        if mode == "zoom":
            self.toolbar.zoom()
        elif mode == "pan":
            self.toolbar.pan()
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        mode = self._nav_mode()
        for key in ("zoom", "pan"):
            self._nav_btns[key].configure(
                style="IconOn.TButton" if mode == key else "Icon.TButton")

    # ------------------------------------------------ Zoom & Verschieben
    def _labels_visible(self, vis):
        for t in self.ax.texts:
            t.set_visible(vis)

    def _labels_back(self):
        if time.monotonic() - self._last_scroll >= 0.35 \
                and self._pan_start is None:
            self._labels_visible(True)
            self.canvas.draw_idle()

    def _on_scroll(self, ev):
        if self.result is None or self._toolbar_active() \
                or ev.inaxes != self.ax or ev.xdata is None:
            return
        now = time.monotonic()
        if now - self._last_scroll > 1.0:      # neue Zoom-Geste -> Historie
            self.toolbar.push_current()
        self._last_scroll = now
        self._labels_visible(False)            # fluessig zoomen
        self.after(400, self._labels_back)
        f = (1/1.25) if ev.button == "up" else 1.25
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self.ax.set_xlim(ev.xdata - (ev.xdata - x0)*f,
                         ev.xdata + (x1 - ev.xdata)*f)
        self.ax.set_ylim(ev.ydata - (ev.ydata - y0)*f,
                         ev.ydata + (y1 - ev.ydata)*f)
        self.canvas.draw_idle()

    def _on_press(self, ev):
        if self.result is None or self._toolbar_active():
            return
        if ev.inaxes != self.ax or ev.xdata is None:
            return
        if ev.button == 3:                     # rechte Maustaste = Pan
            self.toolbar.push_current()        # fuer Vor/Zurueck
            self._pan_start = (ev.xdata, ev.ydata)
            self._labels_visible(False)        # fluessig verschieben
            self._last_motion = 0.0
        elif ev.button == 1:                   # linke Maustaste = Bohrung waehlen
            self._toggle_hole_at(ev.xdata, ev.ydata)

    @staticmethod
    def _point_seg_dist(px, py, ax_, ay_, bx, by):
        dx, dy = bx - ax_, by - ay_
        ln2 = dx*dx + dy*dy
        if ln2 < 1e-9:
            return math.hypot(px - ax_, py - ay_)
        t = max(0.0, min(1.0, ((px-ax_)*dx + (py-ay_)*dy) / ln2))
        return math.hypot(px - (ax_ + t*dx), py - (ay_ + t*dy))

    def _toggle_hole_at(self, x, y):
        """Naechstgelegene Bohrung (axial ODER quer) unter dem Klick fuer
        den manuellen Schnitt B an-/abwaehlen."""
        if self.result is None:
            return
        view, rot = self.view_var.get(), self._rotation()
        best, bestd = None, None
        for f in self.result["features"]:
            if f.orientation == "axial":
                u, v = f.uv
                d = math.hypot(u - x, v - y)
            elif f.orientation in ("quer", "schraeg"):
                a, b = hd.feature_uv_extent(f, view, rot)
                d = self._point_seg_dist(x, y, a[0], a[1], b[0], b[1])
            else:
                continue
            tol = max(f.diameter * 0.9, 3.0)
            if d <= tol and (bestd is None or d < bestd):
                best, bestd = f, d
        if best is None:
            return
        idx = next((i for i, m in enumerate(self.manual_selection)
                    if m is best), None)
        if idx is None:
            self.manual_selection.append(best)
        else:
            del self.manual_selection[idx]
        self._quick_refresh()

    def _on_motion(self, ev):
        if (self._pan_start is None or ev.inaxes != self.ax
                or ev.xdata is None):
            return
        now = time.monotonic()
        if now - getattr(self, "_last_motion", 0.0) < 0.03:
            return                              # ~30 FPS Drossel
        self._last_motion = now
        dx = ev.xdata - self._pan_start[0]
        dy = ev.ydata - self._pan_start[1]
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self.ax.set_xlim(x0 - dx, x1 - dx)
        self.ax.set_ylim(y0 - dy, y1 - dy)
        self.canvas.draw_idle()

    def _on_release(self, ev):
        if self._pan_start is None:
            self._update_nav_buttons()
            return
        self._pan_start = None
        self._labels_visible(True)
        self.canvas.draw_idle()

    def reset_view(self):
        """Gesamtansicht wiederherstellen, Markierung entfernen."""
        self._remove_highlight()
        if self._home_lims:
            self.toolbar.push_current()
            self.ax.set_xlim(self._home_lims[0])
            self.ax.set_ylim(self._home_lims[1])
        self.canvas.draw_idle()

    def _remove_highlight(self):
        if self._hl_artist is not None:
            try:
                self._hl_artist.remove()
            except Exception:
                pass
            self._hl_artist = None

    # -------------------------------------------------------------- Laden
    def load_step(self):
        init_dir = self._ui.get("last_dir") if isinstance(self._ui, dict) \
            else None
        path = filedialog.askopenfilename(
            title="STEP-Datei wählen",
            initialdir=init_dir if init_dir and os.path.isdir(init_dir)
            else None,
            filetypes=[("STEP", "*.step *.stp *.STEP *.STP"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        self._ui["last_dir"] = self._last_dir
        self.open_path(path)

    def open_path(self, path):
        self.step_path = path
        self.lbl_file.config(text="📄  " + os.path.basename(path))
        self.thread_overrides.clear()
        self.force_keys.clear()
        self.skip_keys.clear()
        self.hidden_keys.clear()
        self.deleted_keys.clear()
        self._delete_stack.clear()
        self.shape = None
        self.reanalyze()

    def reanalyze(self):
        """Schwere Phase (Laden + Geometrie) im Hintergrund-Thread."""
        if self.step_path is None and self.shape is None:
            return
        if self._busy:
            return
        self._busy = True
        self.manual_selection = []      # alte Feature-Objekte werden ungueltig
        self._section_cache = {}        # bezog sich auf die alten Objekte
        self._q = queue.Queue()
        self._t0 = time.perf_counter()
        tm = dict(self.thread_map)
        chamfer = self.chamfer_var.get()
        path, shape = self.step_path, self.shape

        def worker():
            try:
                sh = shape if shape is not None else hd.load_step(path)
                self._q.put(("loaded", sh))
                geo = hd.analyze_geometry(sh, thread_map=tm,
                                          thread_requires_chamfer=chamfer)
                self._q.put(("done", geo))
            except Exception as e:
                self._q.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()
        self._phase = ("STEP wird geladen…" if shape is None
                       else "Geometrie wird analysiert…")
        self._progress_on()
        self._poll_worker()

    def _poll_worker(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "loaded":
                    self.shape = payload
                    self._phase = "Geometrie wird analysiert…"
                elif kind == "done":
                    self._busy = False
                    self.geo = payload
                    self.geo.setdefault("outline_cache", {})
                    if self._pending_import is not None:
                        self._apply_pending_import()
                    self.refresh_view()
                    return
                elif kind == "error":
                    self._busy = False
                    self._progress_off()
                    self._status("Fehler beim Laden.", kind="warn")
                    messagebox.showerror("Analysefehler", str(payload))
                    return
        except queue.Empty:
            pass
        dt = time.perf_counter() - self._t0
        self._status(f"⏳ {self._phase}  ({dt:.0f} s)")
        self.after(200, self._poll_worker)

    def _status(self, text, kind=None):
        col = {"warn": "#ffcf8a", "ok": "#a8e6c4"}.get(kind, C["header_tx"])
        self.lbl_status.configure(text=text, foreground=col)

    def _progress_on(self):
        self.progress.pack(side="right", padx=(0, self.px(12)))
        self.progress.start(12)

    def _progress_off(self):
        self.progress.stop()
        self.progress.pack_forget()

    def _analyze_kwargs(self):
        """Gemeinsame Eingaben fuer hd.analyze_view (Hintergrund- und
        Schnellpfad)."""
        self._apply_overrides()
        geo, view = self.geo, self.view_var.get()
        d, rot = self.dir_var.get(), self._rotation()
        if self.deleted_keys:
            geo = dict(self.geo)
            geo["features"] = [f for f in self.geo["features"]
                               if hd.type_key(f) not in self.deleted_keys]
        live_ids = {id(f) for f in geo["features"]}
        self.manual_selection = [f for f in self.manual_selection
                                 if id(f) in live_ids]
        renum = "B" if self.renumber_var.get() == "Schnitt B" else "A"
        return dict(geo=geo, view=view, direction=d, rotation=rot,
                   force_keys=frozenset(self.force_keys),
                   skip_keys=frozenset(self.skip_keys),
                   manual_selection=list(self.manual_selection),
                   manual_direction=self.dir2_var.get(),
                   renumber_source=renum, section_cache=self._section_cache)

    def refresh_view(self, reset_view=True):
        """Ansichtsphase (Schnittplanung) im Hintergrund mit Ladebalken.
        reset_view=False erhaelt Zoom/Pan (z.B. bei Schnitt-B-Klicks)."""
        if self.geo is None or getattr(self, "_busy_view", False):
            return
        self._busy_view = True
        kwargs = self._analyze_kwargs()
        q = queue.Queue()

        def worker():
            try:
                q.put(("ok", hd.analyze_view(**kwargs)))
            except Exception as e:
                q.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()
        self._progress_on()
        t0 = time.perf_counter()

        def poll():
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                self._status(f"⏳ Schnitt wird geplant…  "
                             f"({time.perf_counter()-t0:.0f} s)")
                self.after(150, poll)
                return
            self._busy_view = False
            self._progress_off()
            if kind == "ok":
                self.result = payload
                self._refresh(reset_view=reset_view)
            else:
                self._status("Fehler bei der Schnittplanung.", kind="warn")
                messagebox.showerror("Analysefehler", str(payload))

        poll()

    def _quick_refresh(self, reset_view=False):
        """Synchrone Aktualisierung ohne Hintergrund-Thread, fuer Schnitt-B-
        Aenderungen (Klick, Entfernen, Zuruecksetzen, Richtung/Nummerierung):
        dank Schnitt-A-Cache in hd.analyze_view ist das i.d.R. sofort fertig.
        Faellt auf den Hintergrundpfad zurueck, wenn dieser gerade laeuft
        (verhindert gleichzeitigen Zugriff auf Cache/Feature-Objekte)."""
        if self.geo is None:
            return
        if getattr(self, "_busy_view", False):
            self.refresh_view(reset_view=reset_view)
            return
        kwargs = self._analyze_kwargs()
        try:
            self.result = hd.analyze_view(**kwargs)
        except Exception as e:
            self._status("Fehler bei der Schnittplanung.", kind="warn")
            messagebox.showerror("Analysefehler", str(e))
            return
        self._refresh(reset_view=reset_view)

    # ----------------------------------------------- Gewinde-Umschaltung
    def _apply_overrides(self):
        for f in self.geo["features"]:
            core_d = round(2 * min(c.radius for c in f.cylinders), 2)
            ov = self.thread_overrides.get(core_d)
            if ov is None:
                continue
            if ov == "Bohrung" and f.kind == "Gewinde":
                f.kind = "Bohrung"
                f.diameter = core_d
                f.label = f"D{hd._fmt(core_d)}"
            elif ov.startswith("Gewinde:") and f.kind != "Gewinde":
                lab = ov.split(":", 1)[1]
                f.kind = "Gewinde"
                f.label = lab
                f.diameter = hd.THREAD_NOMINAL.get(lab, f.diameter)

    def _selected_type_no(self):
        sel = self.tree.selection()
        if not sel or self.result is None:
            if self.result is not None:
                self._status("Bitte zuerst eine Zeile in der Bohrtabelle "
                             "auswählen.", kind="warn")
            return None
        return int(self.tree.item(sel[0])["values"][0])

    def toggle_thread(self):
        tno = self._selected_type_no()
        if tno is None:
            return
        feats = [f for f in self.result["features"] if f.type_no == tno]
        if not feats:
            return
        f = feats[0]
        core_d = round(2 * min(c.radius for c in f.cylinders), 2)
        if f.kind == "Gewinde":
            self.thread_overrides[core_d] = "Bohrung"
        else:
            lab = None
            for core, name in self.thread_map.items():
                if abs(core_d - core) <= hd.THREAD_CORE_TOL:
                    lab = name
                    break
            if lab is None:
                lab = simpledialog.askstring(
                    "Gewinde", f"Gewindebezeichnung für Kernloch "
                               f"D{core_d} (z.B. M8):", parent=self)
                if not lab:
                    return
            self.thread_overrides[core_d] = f"Gewinde:{lab}"
        self.refresh_view()

    def edit_thread_map(self):
        cur = ", ".join(f"{k}->{v}" for k, v in
                        sorted(self.thread_map.items()))
        s = simpledialog.askstring(
            "Kernloch-Zuordnung",
            "Format:  kernloch->Gewinde, ...   (z.B. 6.8->M8, 5.0->M6)",
            initialvalue=cur, parent=self)
        if s is None:
            return
        try:
            new = {}
            for part in s.split(","):
                if not part.strip():
                    continue
                k, v = part.split("->")
                new[float(k)] = v.strip()
            self.thread_map = new
            self.reanalyze()
        except Exception as e:
            messagebox.showerror("Fehler", f"Ungültiges Format: {e}")

    # ----------------------------------------------------------- Anzeige
    def _refresh(self, reset_view=True):
        self._progress_off()
        self._populate_tree()
        self._update_section_lists()

        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", self.result["section_text"])
        if self.manual_selection:
            self.txt.insert("end", "\n\n" + self._manual_section_text())

        self._draw(reset_view=reset_view)

        feats = self.result["features"]
        rows = hd.table_rows(feats)
        n = len(feats)
        n_ax = sum(1 for f in feats if f.orientation == "axial")
        n_q = n - n_ax
        hits = self.result.get("section_hits", [])
        n_ht = len({f.type_no for f in hits})
        self.lbl_types_badge.configure(text=f"{len(rows)} Typen · {n} Bohr.")
        stxt = self.result.get("section_text") or ""
        warn = ("WARNUNG" in stxt or "NICHT ERREICHBAR" in stxt)
        self._status(
            f"✔  {n} Bohrungen · {len(rows)} Typen ({n_ax} axial, {n_q} "
            f"quer/schräg)  ·  Schnitt A: {n_ht} Typen  ·  "
            f"Erkennung v{hd.__version__}"
            + ("   ⚠ Hinweise in der Schnittführung" if warn else ""),
            kind="warn" if warn else None)

    def _populate_tree(self):
        """Bohrtabelle neu befuellen, inkl. Markierungen und
        Sichtbarkeits-Icon."""
        feats = self.result["features"]
        rows = hd.table_rows(feats)
        sel_no = None
        sel = self.tree.selection()
        if sel:
            try:
                sel_no = self.tree.item(sel[0])["values"][0]
            except Exception:
                sel_no = None
        self.tree.delete(*self.tree.get_children())
        by_no = {}
        for f in feats:
            by_no.setdefault(f.type_no, f)
        self._by_no = by_no
        all_keys = set()
        for i, r in enumerate(rows):
            f = by_no.get(r["Nr"])
            mark = ""
            eye = "👁"
            tags = ["odd"] if i % 2 else []
            if f is not None:
                k = hd.type_key(f)
                all_keys.add(k)
                if k in self.force_keys:
                    mark = "⚑ "
                elif k in self.skip_keys:
                    mark = "∅ "
                if k in self.hidden_keys:
                    eye = "🚫"
                    tags.append("dim")
                elif f.kind == "Gewinde":
                    tags.append("thread")
                elif f.orientation != "axial":
                    tags.append("cross")
            gt = r["Gewindetiefe"]
            if gt is not None:
                gt_txt = f"{gt:g}"
            elif r["Art"] == "Durchgangsgewinde":
                gt_txt = "durch"       # Gewinde ueber die ganze Bohrung
            else:
                gt_txt = "–"
            t = r["Tiefe"]
            t_txt = f"{t:g}" if t is not None else "–"   # Durchgang: keine Tiefe
            iid = self.tree.insert("", "end", tags=tags,
                                   values=(r["Nr"], mark + str(r["Bezeichnung"]),
                                           r["Art"], t_txt, gt_txt,
                                           r["Anzahl"], eye))
            if sel_no is not None and r["Nr"] == sel_no:
                self._suppress_tree_jump = True
                self.tree.selection_set(iid)
        all_hidden = bool(all_keys) and all_keys <= self.hidden_keys
        self.tree.heading("👁", text="🚫" if all_hidden else "👁")

    def _draw(self, reset_view=True):
        """Grafik (neu) zeichnen. Bei reset_view=False bleiben Zoom/Pan
        erhalten (fuer leichte Aktualisierungen wie Sichtbarkeits-Umschalten
        oder Schnitt-B-Aenderungen)."""
        if self.result is None:
            return
        xlim = ylim = None
        if not reset_view:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._hl_artist = None       # Achse wird geleert
        self.ax.clear()
        hidden_now = {f.type_no for f in self.result["features"]
                      if hd.type_key(f) in self.hidden_keys}
        hr.draw_view(self.ax, self.result, self.view_var.get(),
                    hidden_types=hidden_now)
        self.fig.tight_layout()
        if xlim is not None:
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
        self.canvas.draw()
        if reset_view:
            self._home_lims = (self.ax.get_xlim(), self.ax.get_ylim())
            if hasattr(self.toolbar, "update"):
                self.toolbar.update()    # Ansichts-Historie zuruecksetzen
            self.toolbar.push_current()

    def _mark_type(self, kind):
        """Ausgewaehlten Typ erzwingen / als nicht noetig markieren
        (erneuter Klick hebt die Markierung wieder auf)."""
        tno = self._selected_type_no()
        if tno is None:
            return
        f = next((x for x in self.result["features"]
                  if x.type_no == tno), None)
        if f is None:
            return
        key = hd.type_key(f)
        if kind == "force":
            if key in self.force_keys:
                self.force_keys.discard(key)
            else:
                self.force_keys.add(key)
                self.skip_keys.discard(key)
        else:
            if key in self.skip_keys:
                self.skip_keys.discard(key)
            else:
                self.skip_keys.add(key)
                self.force_keys.discard(key)
        self.refresh_view()

    # ------------------------------------------------ Sichtbarkeit (Augen)
    def _on_tree_button1(self, ev):
        region = self.tree.identify("region", ev.x, ev.y)
        if region != "cell":
            return
        col = self.tree.identify_column(ev.x)
        row = self.tree.identify_row(ev.y)
        if not row or col != self._eye_col_id:
            return
        tno = int(self.tree.item(row)["values"][0])
        self._toggle_row_visibility(tno)
        return "break"

    def _toggle_row_visibility(self, tno):
        f = self._by_no.get(tno) if hasattr(self, "_by_no") else None
        if f is None or self.result is None:
            return
        key = hd.type_key(f)
        if key in self.hidden_keys:
            self.hidden_keys.discard(key)
        else:
            self.hidden_keys.add(key)
        self._populate_tree()
        self._draw(reset_view=False)

    def _toggle_all_labels(self):
        if self.result is None:
            return
        keys = {hd.type_key(f) for f in self.result["features"]}
        if self.hidden_keys & keys:
            self.hidden_keys -= keys
        else:
            self.hidden_keys |= keys
        self._populate_tree()
        self._draw(reset_view=False)

    # ------------------------------------------------- Bohrung loeschen
    def _delete_selected_type(self):
        tno = self._selected_type_no()
        if tno is None:
            return
        f = next((x for x in self.result["features"]
                  if x.type_no == tno), None)
        if f is None:
            return
        if not messagebox.askyesno(
                "Bohrung löschen",
                f"Typ {tno} ({f.label}, {hd._fmt(f.diameter)} mm) wirklich "
                "aus der Bohrtabelle löschen?\n\nMit '↺ Rückgängig' "
                "wiederherstellbar."):
            return
        key = hd.type_key(f)
        self.deleted_keys.add(key)
        self._delete_stack.append(key)
        self.manual_selection = [m for m in self.manual_selection
                                 if hd.type_key(m) != key]
        self.refresh_view()

    def _undo_delete(self):
        if not self._delete_stack:
            self._status("Nichts rückgängig zu machen.")
            return
        key = self._delete_stack.pop()
        self.deleted_keys.discard(key)
        self.refresh_view()

    # ------------------------------------------------- Schnitt B (manuell)
    def _remove_selected_manual(self):
        sel = self.tv_secB.selection()
        if not sel or self.result is None:
            if self.result is not None:
                self._status("Bitte zuerst eine Bohrung in der Tabelle "
                             "„Schnitt B“ auswählen.", kind="warn")
            return
        hits2 = self.result.get("section2_hits") or []
        i = int(sel[0])
        if i < len(hits2):
            target = hits2[i]
            self.manual_selection = [m for m in self.manual_selection
                                     if m is not target]
            self._quick_refresh()

    def _reset_manual_section(self):
        self.manual_selection = []
        self._quick_refresh()

    def _manual_section_text(self):
        poly2 = self.result.get("section2_poly") or []
        hits2 = self.result.get("section2_hits") or []
        if not poly2:
            return "Schnitt B-B (manuell): keine Bohrung ausgewählt."
        lines = [f"Stufenschnitt B-B (manuell), Richtung: "
                f"{self.dir2_var.get()}"]
        for i, (u, v) in enumerate(poly2):
            lines.append(f"  P{i+1}: ({u:.1f}, {v:.1f})")
        tags = ", ".join(f"#{f.type_no}" for f in hits2)
        lines.append(f"Geschnittene Bohrungen: {tags or '—'}  "
                     f"({len(hits2)} Bohrungen)")
        return "\n".join(lines)

    @staticmethod
    def _sec_row(f):
        ori = {"axial": "axial", "quer": "quer", "schraeg": "schräg"}.get(
            f.orientation, f.orientation)
        return (f.type_no, f.label, "durch" if f.through else "Sack", ori)

    def _fill_section_table(self, tv, feats):
        tv.delete(*tv.get_children())
        for i, f in enumerate(feats):
            tags = ["odd"] if i % 2 else []
            if f.kind == "Gewinde":
                tags.append("thread")
            elif f.orientation != "axial":
                tags.append("cross")
            tv.insert("", "end", iid=str(i), values=self._sec_row(f),
                      tags=tags)

    def _update_section_lists(self):
        self._section_feats = list(self.result.get("section_hits", []))
        self._fill_section_table(self.tv_secA, self._section_feats)
        n_types = len({f.type_no for f in self._section_feats})
        self.lbl_badge_a.configure(
            text=f"{len(self._section_feats)} Bohr. · {n_types} Typen")

        self._section2_feats = list(self.result.get("section2_hits", []))
        self._fill_section_table(self.tv_secB, self._section2_feats)
        n_types2 = len({f.type_no for f in self._section2_feats})
        self.lbl_badge_b.configure(
            text=(f"{len(self._section2_feats)} Bohr. · {n_types2} Typen"
                  if self._section2_feats else "leer"))

    # ------------------------------------------------- Navigation/Fokus
    def _on_sectionA_click(self, _ev):
        sel = self.tv_secA.selection()
        if sel and int(sel[0]) < len(self._section_feats):
            self._goto_feature(self._section_feats[int(sel[0])])

    def _on_sectionB_click(self, _ev):
        sel = self.tv_secB.selection()
        if sel and int(sel[0]) < len(self._section2_feats):
            self._goto_feature(self._section2_feats[int(sel[0])])

    def _on_tree_click(self, _ev):
        if getattr(self, "_suppress_tree_jump", False):
            self._suppress_tree_jump = False
            return
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        tno = int(self.tree.item(sel[0])["values"][0])
        f = next((x for x in self._section_feats if x.type_no == tno),
                 next((x for x in self.result["features"]
                       if x.type_no == tno), None))
        if f:
            self._goto_feature(f)

    def _goto_feature(self, f):
        from matplotlib.patches import Circle
        self.toolbar.push_current()            # fuer Vor/Zurueck
        if f.orientation == "axial":
            cu, cv = f.uv
            half = max(4.0 * f.diameter, 12.0)
        else:
            a, b = hd.feature_uv_extent(f, self.view_var.get(),
                                        self._rotation())
            cu, cv = 0.5*(a[0]+b[0]), 0.5*(a[1]+b[1])
            half = max(0.6*((abs(b[0]-a[0])**2
                             + abs(b[1]-a[1])**2) ** 0.5), 12.0)
        self.ax.set_xlim(cu - half, cu + half)
        self.ax.set_ylim(cv - half, cv + half)
        self.toolbar.push_current()            # Ziel ebenfalls merken (▶)
        self._remove_highlight()
        r = max(f.diameter * 0.9, 3.0)
        self._hl_artist = Circle((cu, cv), r, fill=False, ec=hr.COL_HILITE,
                                 lw=2.6, zorder=10)
        self.ax.add_patch(self._hl_artist)
        self.canvas.draw_idle()

    # ------------------------------------------------------------ Export
    def _need_result(self):
        if self.result is None:
            messagebox.showinfo("Schnittinator",
                                "Bitte zuerst eine STEP-Datei laden.")
            return False
        return True

    def _default_name(self, ext):
        base = os.path.splitext(os.path.basename(self.step_path or
                                                 "schnittinator"))[0]
        return f"{base}.{ext}"

    def export_csv(self):
        if not self._need_result():
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=self._default_name("csv"))
        if not path:
            return
        rows = hd.table_rows(self.result["features"])
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["Nr", "Bezeichnung",
                                               "Durchmesser", "Art", "Tiefe",
                                               "Gewindetiefe", "Anzahl"],
                               delimiter=";")
            w.writeheader()
            w.writerows(rows)
            fh.write("\n")
            for line in self.result["section_text"].splitlines():
                fh.write(f"{line}\n")
        self._status(f"✔  CSV gespeichert: {path}", kind="ok")

    def export_fig(self, ext):
        if not self._need_result():
            return
        path = filedialog.asksaveasfilename(
            defaultextension=f".{ext}",
            filetypes=[(ext.upper(), f"*.{ext}")],
            initialfile=self._default_name(ext))
        if not path:
            return
        self.fig.savefig(path, dpi=200, bbox_inches="tight",
                         facecolor="white")
        self._status(f"✔  {ext.upper()} gespeichert: {path}", kind="ok")

    # ------------------------------------------------- Einstellungen (Platte)
    @staticmethod
    def _key_to_json(k):
        cb = list(k[5]) if k[5] is not None else None
        return [k[0], k[1], k[2], k[3], k[4], cb]

    @staticmethod
    def _json_to_key(lst):
        cb = tuple(lst[5]) if lst[5] is not None else None
        return (lst[0], lst[1], lst[2], lst[3], lst[4], cb)

    def export_settings(self):
        """Alle manuellen Einstellungen zu dieser Platte (Ansicht, Schnitt B,
        Erzwingen/Nicht-nötig/Ausblenden/Löschen, Gewinde-Zuordnung) als JSON
        sichern — z.B. um spaeter an derselben STEP-Datei weiterzuarbeiten."""
        if self.geo is None:
            messagebox.showinfo("Einstellungen sichern",
                                "Bitte zuerst eine STEP-Datei laden.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Schnittinator-Einstellungen", "*.json")],
            initialfile=(os.path.splitext(
                os.path.basename(self.step_path or "einstellungen"))[0]
                + ".schnittinator.json"))
        if not path:
            return
        data = {
            "schnittinator_settings": 1,
            "step_file": os.path.basename(self.step_path) if self.step_path
                        else None,
            "view": self.view_var.get(),
            "rotation": self._rotation(),
            "direction": self.dir_var.get(),
            "manual_direction": self.dir2_var.get(),
            "renumber_source": ("B" if self.renumber_var.get() == "Schnitt B"
                                else "A"),
            "thread_requires_chamfer": self.chamfer_var.get(),
            "thread_map": {str(k): v for k, v in self.thread_map.items()},
            "thread_overrides": {str(k): v for k, v
                                 in self.thread_overrides.items()},
            "force_keys": [self._key_to_json(k) for k in self.force_keys],
            "skip_keys": [self._key_to_json(k) for k in self.skip_keys],
            "hidden_keys": [self._key_to_json(k) for k in self.hidden_keys],
            "deleted_keys": [self._key_to_json(k) for k in self.deleted_keys],
            "manual_selection": [
                {"axis_pt": list(f.axis_pt), "axis_dir": list(f.axis_dir),
                 "diameter": f.diameter, "z0": f.z0, "z1": f.z1}
                for f in self.manual_selection],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        self._status(f"✔  Einstellungen gesichert: {path}", kind="ok")

    def import_settings(self):
        if self.geo is None and self.step_path is None:
            messagebox.showinfo("Einstellungen laden",
                                "Bitte zuerst eine STEP-Datei laden.")
            return
        path = filedialog.askopenfilename(
            filetypes=[("Schnittinator-Einstellungen", "*.json"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        self.apply_settings_file(path)

    def apply_settings_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            messagebox.showerror("Einstellungen laden",
                                 f"Datei konnte nicht gelesen werden: {e}")
            return
        cur = (os.path.basename(self.step_path) if self.step_path else None)
        saved = data.get("step_file")
        if saved and cur and saved != cur:
            if not messagebox.askyesno(
                    "Einstellungen laden",
                    f"Die Einstellungen wurden für '{saved}' gespeichert, "
                    f"aktuell ist aber '{cur}' geladen. Trotzdem "
                    "anwenden? (Bohrungsauswahl für Schnitt B wird nur "
                    "übernommen, wenn passende Bohrungen gefunden werden)"):
                return

        self._pending_import = data
        need_reanalyze = (
            data.get("thread_requires_chamfer", self.chamfer_var.get())
            != self.chamfer_var.get()
            or {float(k): v for k, v in data.get("thread_map", {}).items()}
            != self.thread_map)
        if need_reanalyze:
            self.thread_map = {float(k): v for k, v
                               in data.get("thread_map", {}).items()} \
                or self.thread_map
            self.chamfer_var.set(
                data.get("thread_requires_chamfer", self.chamfer_var.get()))
            self.reanalyze()
        else:
            self._apply_pending_import()
            self.refresh_view()

    def _apply_pending_import(self):
        """Wendet die restlichen (nicht geometriephasen-relevanten)
        Einstellungen an, nachdem geo (ggf. nach reanalyze) bereit ist."""
        data = self._pending_import
        self._pending_import = None
        if data is None or self.geo is None:
            return
        self.thread_overrides = {float(k): v for k, v
                                 in data.get("thread_overrides", {}).items()}
        self.view_var.set(data.get("view", self.view_var.get()))
        self.rot_var.set(f"{int(data.get('rotation', 0)) % 360}°")
        self.dir_var.set(data.get("direction", self.dir_var.get()))
        self.dir2_var.set(data.get("manual_direction", self.dir2_var.get()))
        self.renumber_var.set(
            "Schnitt B" if data.get("renumber_source", "A") == "B"
            else "Schnitt A")
        self.force_keys = {self._json_to_key(k)
                           for k in data.get("force_keys", [])}
        self.skip_keys = {self._json_to_key(k)
                          for k in data.get("skip_keys", [])}
        self.hidden_keys = {self._json_to_key(k)
                            for k in data.get("hidden_keys", [])}
        self.deleted_keys = {self._json_to_key(k)
                             for k in data.get("deleted_keys", [])}

        feats = self.geo["features"]
        selection = []
        missing = 0
        for desc in data.get("manual_selection", []):
            best, bestd = None, None
            for f in feats:
                dd = sum(abs(f.axis_pt[i] - desc["axis_pt"][i])
                        for i in range(3))
                if dd > 1.0 or abs(f.diameter - desc["diameter"]) > 0.5:
                    continue
                if (abs(f.z0 - desc["z0"]) > 1.0
                        or abs(f.z1 - desc["z1"]) > 1.0):
                    continue
                if best is None or dd < bestd:
                    best, bestd = f, dd
            if best is not None:
                selection.append(best)
            else:
                missing += 1
        self.manual_selection = selection
        self._section_cache = {}
        if missing:
            messagebox.showwarning(
                "Einstellungen laden",
                f"{missing} Bohrung(en) für Schnitt B konnten in der "
                "aktuell geladenen Geometrie nicht wiedergefunden werden "
                "und wurden übersprungen.")


def main():
    _enable_windows_dpi_awareness()
    app = App()
    # Optional: STEP-Datei als Argument (z. B. Datei auf die EXE ziehen)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.after(200, lambda: app.open_path(sys.argv[1]))
    app.mainloop()


if __name__ == "__main__":
    main()
