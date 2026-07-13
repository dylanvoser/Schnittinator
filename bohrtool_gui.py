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
Bohrtabelle: 👁-Spalte blendet einzelne Bohrnummern aus/ein, Kopf-Knopf alle.
"""
import csv
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
ACCENT = "#0057b8"
BG = "#f4f5f7"
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
        self.split_keys = set()          # Durchgangs-Typen, manuell in 2 Sacklöcher geteilt
        self._split_stack = []
        self._filt_cache = None          # s. _filtered_features()
        self.manual_selection = []       # Feature-Objekte fuer Schnitt B
        self._section_cache = {}         # teurer Stufenschnitt A-A, s. hd.analyze_view
        self._section_feats = []
        self._hl_artist = None
        self._pan_start = None
        self._last_scroll = 0.0
        self._home_lims = None
        self._busy = False

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
        st.configure("Head.TLabel", background=PANEL, foreground="#333",
                     font=("Segoe UI Semibold", 10))
        st.configure("Status.TLabel", background=BG,
                     font=("Segoe UI", 9))
        st.configure("TButton", padding=(10, 5), relief="flat",
                     background="#e8eaee")
        st.map("TButton", background=[("active", "#dbe4f5")])
        st.configure("Accent.TButton", background=ACCENT,
                     foreground="white", padding=(12, 6),
                     font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton",
               background=[("active", "#0a6ae0"), ("disabled", "#9bb8dd")])
        st.configure("TCombobox", padding=3)
        st.configure("TCheckbutton", background=BG)
        st.configure("Treeview", rowheight=24, fieldbackground=PANEL,
                     background=PANEL, borderwidth=0)
        st.configure("Treeview.Heading", font=("Segoe UI Semibold", 9),
                     background="#e3e6ea", relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "white")])
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

        self.chamfer_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Gewinde nur mit Ansenkfase",
                        variable=self.chamfer_var,
                        command=self.reanalyze).pack(side="left", padx=14)
        ttk.Button(top, text="Kernloch-Tabelle…",
                   command=self.edit_thread_map).pack(side="left", padx=4)

        for txt, cmd in (("PNG", lambda: self.export_fig("png")),
                         ("SVG", lambda: self.export_fig("svg")),
                         ("CSV", self.export_csv)):
            ttk.Button(top, text=f"⬇ {txt}", command=cmd)\
                .pack(side="right", padx=3)

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
        hdr = ttk.Frame(f_types, style="Panel.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Bohrtabelle (Typen)",
                  style="Head.TLabel").pack(side="left")
        ttk.Button(hdr, text="👁 Alle Nummern ein-/ausblenden",
                   command=self._toggle_all_labels).pack(side="right")
        cols = ("Nr", "Bezeichnung", "Durchmesser", "Art", "Tiefe", "Anzahl",
                "👁")
        self.tree = ttk.Treeview(f_types, columns=cols, show="headings")
        for c, w in zip(cols, (40, 90, 90, 190, 55, 55, 30)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center", stretch=(c != "👁"))
        self._eye_col_id = f"#{len(cols)}"
        self.tree.pack(fill="both", expand=True, pady=(4, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_click)
        self.tree.bind("<Button-1>", self._on_tree_button1)
        ttk.Button(f_types, text="⇄  Typ umschalten: Gewinde / Bohrung",
                   command=self.toggle_thread).pack(fill="x")
        row = ttk.Frame(f_types, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="⚑ Im Schnitt erzwingen",
                   command=lambda: self._mark_type("force"))\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(row, text="∅ Nicht nötig",
                   command=lambda: self._mark_type("skip"))\
            .pack(side="left", fill="x", expand=True, padx=(2, 0))
        row2 = ttk.Frame(f_types, style="Panel.TFrame")
        row2.pack(fill="x", pady=(4, 0))
        ttk.Button(row2, text="🗑 Bohrung löschen (falsch erkannt)",
                   command=self._delete_selected_type)\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(row2, text="↺ Löschung rückgängig",
                   command=self._undo_delete)\
            .pack(side="left", fill="x", expand=True, padx=(2, 0))
        row3 = ttk.Frame(f_types, style="Panel.TFrame")
        row3.pack(fill="x", pady=(4, 0))
        ttk.Button(row3, text="✂ In 2 Sacklöcher teilen (Mitte)",
                   command=self._toggle_split)\
            .pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(row3, text="↺ Teilung rückgängig",
                   command=self._undo_split)\
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
        ttk.Button(rowB, text="✕ Entfernen",
                   command=self._remove_selected_manual)\
            .pack(side="left", padx=2)
        ttk.Button(rowB, text="🗑 Schnitt B zurücksetzen",
                   command=self._reset_manual_section)\
            .pack(side="left", padx=2)

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
        self.split_keys.clear()
        self._split_stack.clear()
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

    def _filtered_features(self):
        """geo['features'] mit angewandten Löschungen/Sackloch-Teilungen —
        gecacht, damit gleiche Eingaben dieselben Feature-Objekte liefern
        (sonst würde der Schnitt-A-Cache in hd.analyze_view bei jedem Aufruf
        unnötig verfallen, da Teilung neue Objekte erzeugt)."""
        sig = (frozenset(self.deleted_keys), frozenset(self.split_keys))
        c = self._filt_cache
        if c is not None and c[0] is self.geo["features"] and c[1] == sig:
            return c[2]
        feats = [f for f in self.geo["features"]
                if hd.type_key(f) not in self.deleted_keys]
        if self.split_keys:
            feats = hd.split_hole_types(feats, self.split_keys)
        self._filt_cache = (self.geo["features"], sig, feats)
        return feats

    def _analyze_kwargs(self):
        """Gemeinsame Eingaben fuer hd.analyze_view (Hintergrund- und
        Schnellpfad)."""
        self._apply_overrides()
        geo, view = self.geo, self.view_var.get()
        d, rot = self.dir_var.get(), self._rotation()
        if self.deleted_keys or self.split_keys:
            geo = dict(self.geo)
            geo["features"] = self._filtered_features()
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
        for r in rows:
            f = by_no.get(r["Nr"])
            mark = ""
            eye = "👁"
            if f is not None:
                k = hd.type_key(f)
                if k in self.force_keys:
                    mark = "⚑ "
                elif k in self.skip_keys:
                    mark = "∅ "
                if k in self.hidden_keys:
                    eye = "🚫"
            self.tree.insert("", "end",
                             values=(r["Nr"], mark + str(r["Bezeichnung"]),
                                     r["Durchmesser"], r["Art"],
                                     r["Tiefe"], r["Anzahl"], eye))

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
                "aus der Bohrtabelle löschen?\n\nMit '↺ Löschung "
                "rückgängig' wiederherstellbar."):
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

    # ------------------------------------------- Sackloch-Teilung (manuell)
    def _toggle_split(self):
        """Zwei von gegenueberliegenden Seiten gebohrte Sacklochbohrungen,
        die sich genau treffen, ergeben oft eine nahtlose Zylinderflaeche
        ohne erkennbaren Absatz — von der automatischen Erkennung nicht von
        einer echten Durchgangsbohrung zu unterscheiden. Korrektur von Hand:
        ausgewaehlten Durchgangs-Typ in zwei Sacklöcher (je halbe Tiefe)
        aufteilen."""
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        tno = int(self.tree.item(sel[0])["values"][0])
        f = next((x for x in self.result["features"]
                  if x.type_no == tno), None)
        if f is None:
            return
        if not f.through:
            messagebox.showinfo(
                "Sackloch-Teilung",
                "Nur Durchgangsbohrungen können in zwei Sacklochbohrungen "
                "aufgeteilt werden.")
            return
        key = hd.type_key(f)
        if not messagebox.askyesno(
                "Sackloch-Teilung",
                f"Typ {tno} ({f.label}, Durchgang) wirklich in zwei "
                "Sacklochbohrungen aufteilen, die sich in der Mitte "
                "treffen?\n\nMit '↺ Teilung rückgängig' wiederherstellbar."):
            return
        self.split_keys.add(key)
        self._split_stack.append(key)
        self.manual_selection = [m for m in self.manual_selection
                                 if hd.type_key(m) != key]
        self.refresh_view()

    def _undo_split(self):
        if not self._split_stack:
            return
        key = self._split_stack.pop()
        self.split_keys.discard(key)
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
                                               "Durchmesser", "Art",
                                               "Tiefe", "Anzahl"],
                               delimiter=";")
            w.writeheader()
            w.writerows(rows)
            fh.write("\n")
            for line in self.result["section_text"].splitlines():
                fh.write(f"{line}\n")
        messagebox.showinfo("Export", f"CSV gespeichert:\n{path}")

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


if __name__ == "__main__":
    App().mainloop()
