# -*- coding: utf-8 -*-
"""
bohrtool_gui.py — Schnittinator: Bohrungserkennung aus STEP (GUI, v2.6)

Start:  python bohrtool_gui.py

Bedienung Grafik:
  Mausrad            = Zoom um den Cursor
  Rechte Maustaste   = Ansicht verschieben
  Linke Maustaste    = Bohrung anklicken -> für Schnitt B (manuell) auswählen/abwählen
  Häuschen (Toolbar) = Gesamtansicht, Markierung wird entfernt
  Pfeile ◀ ▶         = zwischen letzten Ansichten wechseln
Seitenleiste und Teilbereiche sind an den Trennstegen frei verstellbar.
Bohrtabelle: 👁-Spalte blendet einzelne Bohrnummern aus/ein, Klick auf die
👁-Kopfzeile blendet alle aus/ein.
"""
import csv
import json
import math
import os
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

VIEWS = ["top", "bottom", "front", "back", "left", "right"]
ACCENT = "#e40033"
BG = "#f5f5f5"
PANEL = "#ffffff"


class NavToolbar(NavigationToolbar2Tk):
    """Toolbar mit eigenem 'Home': Gesamtansicht + Markierung entfernen.
    Vor/Zurueck nutzen die native Ansichts-Historie."""

    def __init__(self, canvas, parent, app):
        self.app = app
        super().__init__(canvas, parent)

    def home(self, *args):
        self.app.reset_view()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Schnittinator")
        self.geometry("1500x900")
        self.configure(bg=BG)
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
        self._hl_artist = None
        self._pan_start = None
        self._last_scroll = 0.0
        self._home_lims = None
        self._busy = False
        self._pending_import = None      # s. import_settings/_apply_pending_import

        self._build_ui()

    # --------------------------------------------------------------- Icon
    def _set_icon(self):
        base = os.path.dirname(os.path.abspath(__file__))
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
        base = ("Segoe UI", 10)
        self.option_add("*Font", base)
        st.configure(".", background=BG, font=base)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG)
        st.configure("Head.TLabel", background=PANEL, foreground="#000000",
                     font=("Segoe UI Semibold", 10))
        st.configure("Status.TLabel", background=BG, foreground="#000000",
                     font=("Segoe UI", 9))
        st.configure("TButton", padding=(10, 5), relief="flat",
                     background="#f0f0f0", foreground="#000000")
        st.map("TButton",
               background=[("active", "#e0e0e0"), ("disabled", "#f7f7f7")],
               foreground=[("disabled", "#a0a0a0")])
        st.configure("Accent.TButton", background=ACCENT,
                     foreground="#ffffff", padding=(12, 6),
                     font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton",
               background=[("active", "#b8002a"), ("disabled", "#f2a9b8")])
        st.configure("TCombobox", padding=3)
        st.configure("TCheckbutton", background=BG, foreground="#000000")
        st.configure("Treeview", rowheight=24, fieldbackground=PANEL,
                     background=PANEL, foreground="#000000", borderwidth=0)
        st.configure("Treeview.Heading", font=("Segoe UI Semibold", 9),
                     background="#eeeeee", foreground="#000000",
                     relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "#ffffff")])
        st.configure("TPanedwindow", background=BG)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(side="top", fill="x")

        ttk.Button(top, text="📂  STEP laden…", style="Accent.TButton",
                   command=self.load_step).pack(side="left")
        self.lbl_file = ttk.Label(top, text="— keine Datei —")
        self.lbl_file.pack(side="left", padx=12)

        def combo(label, var, values, width, cmd):
            ttk.Label(top, text=label).pack(side="left", padx=(16, 4))
            cb = ttk.Combobox(top, textvariable=var, values=values,
                              width=width, state="readonly")
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: cmd())
            return cb

        self.view_var = tk.StringVar(value="top")
        combo("Ansicht:", self.view_var, VIEWS, 7, self.refresh_view)
        self.rot_var = tk.StringVar(value="0°")
        combo("Drehung:", self.rot_var, ["0°", "90°", "180°", "270°"], 5,
              self.refresh_view)
        self.dir_var = tk.StringVar(value="horizontal")
        combo("Schnitt:", self.dir_var, ["horizontal", "vertical"], 10,
              self.refresh_view)

        # "Gewinde nur mit Ansenkfase": im GUI ausgeblendet (siehe
        # thread_requires_chamfer in hd.build_features) — steuert, ob ein
        # Kernloch ohne modellierten Nennzylinder nur dann als Gewinde
        # gilt, wenn am Eintritt eine Ansenkfase liegt (True, Standard hier)
        # oder ob reine Durchmesser-Tabellenzuordnung genuegt (False).
        # Funktion bleibt aktiv, nur ohne eigenen Schalter im UI.
        self.chamfer_var = tk.BooleanVar(value=True)
        ttk.Button(top, text="Kernloch-Tabelle…",
                   command=self.edit_thread_map).pack(side="left", padx=4)

        for txt, cmd in (("PNG", lambda: self.export_fig("png")),
                         ("SVG", lambda: self.export_fig("svg")),
                         ("CSV", self.export_csv),
                         ("LSP", self.export_lsp)):
            ttk.Button(top, text=f"⬇ {txt}", command=cmd)\
                .pack(side="right", padx=3)
        ttk.Button(top, text="📂 Einstellungen…",
                   command=self.import_settings).pack(side="right", padx=3)
        ttk.Button(top, text="💾 Einstellungen…",
                   command=self.export_settings).pack(side="right", padx=3)

        hint = ttk.Frame(self, padding=(10, 0))
        hint.pack(side="top", fill="x")
        ttk.Label(hint, style="Status.TLabel",
                  text="Rechtsklick: Ansicht verschieben  ·  "
                       "Linksklick auf Bohrung: für Schnitt B (manuell) "
                       "auswählen/abwählen").pack(side="left")

        # -------- Hauptbereich: horizontal verstellbar ---------------------
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # linke Spalte: vertikal verstellbare Teilbereiche
        left = ttk.PanedWindow(main, orient="vertical")
        main.add(left, weight=1)

        f_types = ttk.Frame(left, style="Panel.TFrame", padding=6)
        left.add(f_types, weight=2)
        ttk.Label(f_types, text="Bohrtabelle (Typen)  —  👁 in der Kopfzeile "
                                "blendet alle Nummern aus/ein",
                  style="Head.TLabel").pack(anchor="w")
        cols = ("Nr", "DGewinde", "Art", "Tiefe", "Gewindetiefe", "Anzahl",
                "👁")
        headings = {"Nr": "Nr", "DGewinde": "Ø / Gewinde", "Art": "Art",
                   "Tiefe": "Tiefe", "Gewindetiefe": "Gewindetiefe",
                   "Anzahl": "Anzahl", "👁": "👁"}
        self.tree = ttk.Treeview(f_types, columns=cols, show="headings")
        for c, w in zip(cols, (36, 110, 180, 55, 80, 55, 30)):
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=w, anchor="center", stretch=(c != "👁"))
        self.tree.heading("👁", command=self._toggle_all_labels)
        self._eye_col_id = f"#{len(cols)}"
        self.tree.pack(fill="both", expand=True, pady=(4, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_click)
        self.tree.bind("<Button-1>", self._on_tree_button1)
        ttk.Button(f_types, text="⇄  Typ umschalten: Gewinde / Bohrung",
                   command=self.toggle_thread).pack(fill="x")
        row = ttk.Frame(f_types, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="⚑ Erzwingen",
                   command=lambda: self._mark_type("force"))\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(row, text="∅ Nicht nötig",
                   command=lambda: self._mark_type("skip"))\
            .pack(side="left", fill="x", expand=True, padx=(2, 0))
        row2 = ttk.Frame(f_types, style="Panel.TFrame")
        row2.pack(fill="x", pady=(4, 0))
        ttk.Button(row2, text="🗑 Löschen",
                   command=self._delete_selected_type)\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(row2, text="↺ Rückgängig",
                   command=self._undo_delete)\
            .pack(side="left", fill="x", expand=True, padx=(2, 0))

        f_sec = ttk.Frame(left, style="Panel.TFrame", padding=6)
        left.add(f_sec, weight=2)
        ttk.Label(f_sec, text="Bohrungen im Schnitt  (Klick = hinspringen)",
                  style="Head.TLabel").pack(anchor="w")
        sec_cols = ttk.Frame(f_sec, style="Panel.TFrame")
        sec_cols.pack(fill="both", expand=True, pady=(4, 0))

        colA = ttk.Frame(sec_cols, style="Panel.TFrame")
        colA.pack(side="left", fill="both", expand=True, padx=(0, 3))
        ttk.Label(colA, text="Schnitt A (automatisch)",
                  style="Head.TLabel").pack(anchor="w")
        self.lst_secA = tk.Listbox(colA, font=("Consolas", 9),
                                   activestyle="none", bg=PANEL,
                                   selectbackground=ACCENT,
                                   selectforeground="white",
                                   highlightthickness=0, bd=1,
                                   relief="solid")
        self.lst_secA.pack(fill="both", expand=True, pady=(2, 0))
        self.lst_secA.bind("<<ListboxSelect>>", self._on_sectionA_click)

        colB = ttk.Frame(sec_cols, style="Panel.TFrame")
        colB.pack(side="left", fill="both", expand=True, padx=(3, 0))
        ttk.Label(colB, text="Schnitt B (manuell)",
                  style="Head.TLabel").pack(anchor="w")
        self.lst_secB = tk.Listbox(colB, font=("Consolas", 9),
                                   activestyle="none", bg=PANEL,
                                   selectbackground="#e07000",
                                   selectforeground="white",
                                   highlightthickness=0, bd=1,
                                   relief="solid")
        self.lst_secB.pack(fill="both", expand=True, pady=(2, 0))
        self.lst_secB.bind("<<ListboxSelect>>", self._on_sectionB_click)

        rowB = ttk.Frame(f_sec, style="Panel.TFrame")
        rowB.pack(fill="x", pady=(4, 0))
        ttk.Label(rowB, text="Richtung B:").pack(side="left")
        self.dir2_var = tk.StringVar(value="horizontal")
        cb2 = ttk.Combobox(rowB, textvariable=self.dir2_var,
                           values=["horizontal", "vertical"], width=10,
                           state="readonly")
        cb2.pack(side="left", padx=(4, 8))
        cb2.bind("<<ComboboxSelected>>",
                lambda e: self._quick_refresh())

        rowN = ttk.Frame(f_sec, style="Panel.TFrame")
        rowN.pack(fill="x", pady=(4, 0))
        ttk.Label(rowN, text="Nummerierung nach:").pack(side="left")
        self.renumber_var = tk.StringVar(value="Schnitt A")
        cbN = ttk.Combobox(rowN, textvariable=self.renumber_var,
                           values=["Schnitt A", "Schnitt B"], width=10,
                           state="readonly")
        cbN.pack(side="left", padx=(4, 0))
        cbN.bind("<<ComboboxSelected>>",
                lambda e: self._quick_refresh())

        rowB2 = ttk.Frame(f_sec, style="Panel.TFrame")
        rowB2.pack(fill="x", pady=(4, 0))
        ttk.Button(rowB2, text="✕ Entfernen",
                   command=self._remove_selected_manual)\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(rowB2, text="🗑 Zurücksetzen",
                   command=self._reset_manual_section)\
            .pack(side="left", fill="x", expand=True, padx=(2, 0))

        f_txt = ttk.Frame(left, style="Panel.TFrame", padding=6)
        left.add(f_txt, weight=1)
        ttk.Label(f_txt, text="Schnittführung",
                  style="Head.TLabel").pack(anchor="w")
        self.txt = tk.Text(f_txt, font=("Consolas", 9), bg=PANEL, bd=1,
                           relief="solid", highlightthickness=0, wrap="none")
        self.txt.pack(fill="both", expand=True, pady=(4, 0))

        # rechte Spalte: Grafik
        right = ttk.Frame(main, style="Panel.TFrame")
        main.add(right, weight=3)
        self.fig = Figure(figsize=(8, 6.5), dpi=110, facecolor=PANEL)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.toolbar = NavToolbar(self.canvas, right, self)

        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)

        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x")
        self.lbl_status = ttk.Label(status, text="Bereit.",
                                    style="Status.TLabel", padding=(10, 3))
        self.lbl_status.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status, mode="indeterminate",
                                        length=220)
        # (wird nur waehrend der Berechnung angezeigt)

    # ---------------------------------------------------- Hilfsfunktionen
    def _rotation(self):
        return int(self.rot_var.get().rstrip("°"))

    def _toolbar_active(self):
        return bool(str(getattr(self.toolbar, "mode", "") or ""))

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
        if self._toolbar_active() or ev.inaxes != self.ax \
                or ev.xdata is None:
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
        if self._toolbar_active():
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
        self._pan_start = None
        self._labels_visible(True)
        self.canvas.draw_idle()

    def reset_view(self):
        """Haeuschen: Gesamtansicht wiederherstellen, Markierung entfernen."""
        self._remove_highlight()
        if self._home_lims:
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
        path = filedialog.askopenfilename(
            title="STEP-Datei wählen",
            filetypes=[("STEP", "*.step *.stp *.STEP *.STP"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        self.step_path = path
        self.lbl_file.config(text=path.split("/")[-1].split("\\")[-1])
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
                    self.lbl_status.config(text="Fehler.")
                    messagebox.showerror("Analysefehler", str(payload))
                    return
        except queue.Empty:
            pass
        dt = time.perf_counter() - self._t0
        self.lbl_status.config(text=f"⏳ {self._phase}  ({dt:.0f}s)")
        self.after(200, self._poll_worker)

    def _progress_on(self):
        self.progress.pack(side="right", padx=10, pady=2)
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
                self.lbl_status.config(
                    text=f"⏳ Schnitt wird geplant… "
                         f"({time.perf_counter()-t0:.0f}s)")
                self.after(150, poll)
                return
            self._busy_view = False
            self._progress_off()
            if kind == "ok":
                self.result = payload
                self._refresh(reset_view=reset_view)
            else:
                self.lbl_status.config(text="Fehler.")
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
            self.lbl_status.config(text="Fehler.")
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

    def toggle_thread(self):
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        tno = int(self.tree.item(sel[0])["values"][0])
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
        nseg = (self.result.get("raw_cyl_segments", 0)
                + self.result.get("raw_cone_segments", 0))
        self.lbl_status.config(
            text=f"✔ {n} Formelemente ({len(rows)} Typen) — {n_ax} axial, "
                 f"{n_q} quer/schräg — {nseg} Rohsegmente — "
                 f"Erkennung v{hd.__version__}")

    def _populate_tree(self):
        """Bohrtabelle (links) neu befuellen, inkl. Markierungen und
        Sichtbarkeits-Icon."""
        feats = self.result["features"]
        rows = hd.table_rows(feats)
        self.tree.delete(*self.tree.get_children())
        by_no = {}
        for f in feats:
            by_no.setdefault(f.type_no, f)
        self._by_no = by_no
        all_keys = set()
        for r in rows:
            f = by_no.get(r["Nr"])
            mark = ""
            eye = "👁"
            if f is not None:
                k = hd.type_key(f)
                all_keys.add(k)
                if k in self.force_keys:
                    mark = "⚑ "
                elif k in self.skip_keys:
                    mark = "∅ "
                if k in self.hidden_keys:
                    eye = "🚫"
            gt = r["Gewindetiefe"]
            if gt is not None:
                gt_txt = f"{gt:g}"
            elif r["Art"] == "Durchgangsgewinde":
                gt_txt = "durch"       # Gewinde ueber die ganze Bohrung
            else:
                gt_txt = "–"
            t = r["Tiefe"]
            t_txt = f"{t:g}" if t is not None else "–"   # Durchgang: keine Tiefe
            self.tree.insert("", "end",
                             values=(r["Nr"], mark + str(r["Bezeichnung"]),
                                     r["Art"], t_txt, gt_txt,
                                     r["Anzahl"], eye))
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
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        tno = int(self.tree.item(sel[0])["values"][0])
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
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        tno = int(self.tree.item(sel[0])["values"][0])
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
            return
        key = self._delete_stack.pop()
        self.deleted_keys.discard(key)
        self.refresh_view()

    # ------------------------------------------------- Schnitt B (manuell)
    def _remove_selected_manual(self):
        sel = self.lst_secB.curselection()
        if not sel or self.result is None:
            return
        hits2 = self.result.get("section2_hits") or []
        if sel[0] < len(hits2):
            target = hits2[sel[0]]
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

    def _update_section_lists(self):
        self.lst_secA.delete(0, "end")
        self._section_feats = list(self.result.get("section_hits", []))
        for f in self._section_feats:
            ori = {"axial": "axial", "quer": "quer/längs"}.get(
                f.orientation, f.orientation)
            self.lst_secA.insert(
                "end", f" {f.type_no:>2}  {f.label:<7} "
                       f"{'durch' if f.through else 'Sack':<5} {ori}")

        self.lst_secB.delete(0, "end")
        self._section2_feats = list(self.result.get("section2_hits", []))
        for f in self._section2_feats:
            ori = {"axial": "axial", "quer": "quer/längs"}.get(
                f.orientation, f.orientation)
            self.lst_secB.insert(
                "end", f" {f.type_no:>2}  {f.label:<7} "
                       f"{'durch' if f.through else 'Sack':<5} {ori}")

    # ------------------------------------------------- Navigation/Fokus
    def _on_sectionA_click(self, _ev):
        sel = self.lst_secA.curselection()
        if sel and sel[0] < len(self._section_feats):
            self._goto_feature(self._section_feats[sel[0]])

    def _on_sectionB_click(self, _ev):
        sel = self.lst_secB.curselection()
        if sel and sel[0] < len(self._section2_feats):
            self._goto_feature(self._section2_feats[sel[0]])

    def _on_tree_click(self, _ev):
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
        self._remove_highlight()
        r = max(f.diameter * 0.9, 3.0)
        self._hl_artist = Circle((cu, cv), r, fill=False, ec="#00aa44",
                                 lw=2.4, zorder=10)
        self.ax.add_patch(self._hl_artist)
        self.canvas.draw_idle()

    # ------------------------------------------------------------ Export
    def export_csv(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
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
        messagebox.showinfo("Export", f"CSV gespeichert:\n{path}")

    def export_lsp(self):
        """Bohrnummern als Creo-Elements/Direct-Lisp-Makro (.lsp) exportieren:
        setzt beim Laden im Annotator (2D-Zeichnung) an jeder Bohrung einen
        Text mit der Bohrnummer an der Position der AKTUELLEN Ansicht
        (Texthoehe 3.5)."""
        if self.result is None:
            return
        default = "bohrnummern.lsp"
        if self.step_path:
            default = (os.path.splitext(os.path.basename(self.step_path))[0]
                       + "_bohrnummern.lsp")
        path = filedialog.asksaveasfilename(
            defaultextension=".lsp",
            filetypes=[("Creo-Elements/Direct-Makro", "*.lsp")],
            initialfile=default)
        if not path:
            return
        feats = [f for f in self.result["features"]
                 if hd.type_key(f) not in self.hidden_keys]
        text = hd.lsp_annotations(feats, size=3.5)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        messagebox.showinfo(
            "Export",
            f"LSP gespeichert:\n{path}\n\n{len(feats)} Bohrungen (Ansicht "
            f"'{self.view_var.get()}', Texthöhe 3.5).\n\nZeichnung/Annotator "
            "mit dieser Ansicht öffnen, dann die Datei laden. Maßstab/Ursprung "
            "der Ansicht ggf. oben in der Datei (*scale*, *offset-x/-y*) "
            "anpassen.")

    def export_fig(self, ext):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=f".{ext}",
            filetypes=[(ext.upper(), f"*.{ext}")])
        if not path:
            return
        self.fig.savefig(path, dpi=200, bbox_inches="tight",
                         facecolor="white")
        messagebox.showinfo("Export", f"{ext.upper()} gespeichert:\n{path}")

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
            messagebox.showinfo("Einstellungen exportieren",
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
        messagebox.showinfo("Einstellungen exportieren",
                            f"Gespeichert:\n{path}")

    def import_settings(self):
        if self.geo is None and self.step_path is None:
            messagebox.showinfo("Einstellungen importieren",
                                "Bitte zuerst eine STEP-Datei laden.")
            return
        path = filedialog.askopenfilename(
            filetypes=[("Schnittinator-Einstellungen", "*.json"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            messagebox.showerror("Einstellungen importieren",
                                 f"Datei konnte nicht gelesen werden: {e}")
            return
        cur = (os.path.basename(self.step_path) if self.step_path else None)
        saved = data.get("step_file")
        if saved and cur and saved != cur:
            if not messagebox.askyesno(
                    "Einstellungen importieren",
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
                "Einstellungen importieren",
                f"{missing} Bohrung(en) für Schnitt B konnten in der "
                "aktuell geladenen Geometrie nicht wiedergefunden werden "
                "und wurden übersprungen.")


if __name__ == "__main__":
    App().mainloop()
