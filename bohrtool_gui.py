# -*- coding: utf-8 -*-
"""
bohrtool_gui.py — Bohrungserkennung aus STEP (GUI, v2.6)

Start:  python bohrtool_gui.py

Bedienung Grafik:
  Mausrad            = Zoom um den Cursor
  Linke Maustaste    = Ansicht verschieben
  Häuschen (Toolbar) = Gesamtansicht, Markierung wird entfernt
  Pfeile ◀ ▶         = zwischen letzten Ansichten wechseln
Seitenleiste und Teilbereiche sind an den Trennstegen frei verstellbar.
"""
import csv
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
        self.title("Bohrungserkennung aus STEP")
        self.geometry("1500x900")
        self.configure(bg=BG)
        self._setup_style()

        self.shape = None
        self.geo = None
        self.result = None
        self.step_path = None
        self.thread_map = dict(hd.DEFAULT_THREAD_CORE_MAP)
        self.thread_overrides = {}
        self.force_keys = set()
        self.skip_keys = set()
        self._section_feats = []
        self._hl_artist = None
        self._pan_start = None
        self._last_scroll = 0.0
        self._home_lims = None
        self._busy = False

        self._build_ui()

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

        # -------- Hauptbereich: horizontal verstellbar ---------------------
        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # linke Spalte: vertikal verstellbare Teilbereiche
        left = ttk.PanedWindow(main, orient="vertical")
        main.add(left, weight=1)

        f_types = ttk.Frame(left, style="Panel.TFrame", padding=6)
        left.add(f_types, weight=2)
        ttk.Label(f_types, text="Bohrtabelle (Typen)",
                  style="Head.TLabel").pack(anchor="w")
        cols = ("Nr", "Bezeichnung", "Durchmesser", "Art", "Tiefe", "Anzahl")
        self.tree = ttk.Treeview(f_types, columns=cols, show="headings")
        for c, w in zip(cols, (40, 90, 90, 200, 55, 55)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center", stretch=True)
        self.tree.pack(fill="both", expand=True, pady=(4, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_click)
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

        f_sec = ttk.Frame(left, style="Panel.TFrame", padding=6)
        left.add(f_sec, weight=2)
        ttk.Label(f_sec, text="Bohrungen im Schnitt  (Klick = hinspringen)",
                  style="Head.TLabel").pack(anchor="w")
        self.lst_sec = tk.Listbox(f_sec, font=("Consolas", 10),
                                  activestyle="none", bg=PANEL,
                                  selectbackground=ACCENT,
                                  selectforeground="white",
                                  highlightthickness=0, bd=1,
                                  relief="solid")
        self.lst_sec.pack(fill="both", expand=True, pady=(4, 0))
        self.lst_sec.bind("<<ListboxSelect>>", self._on_section_click)

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
        if ev.inaxes == self.ax and ev.button == 1 and ev.xdata is not None:
            self.toolbar.push_current()        # fuer Vor/Zurueck
            self._pan_start = (ev.xdata, ev.ydata)
            self._labels_visible(False)        # fluessig verschieben
            self._last_motion = 0.0

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
        self.shape = None
        self.reanalyze()

    def reanalyze(self):
        """Schwere Phase (Laden + Geometrie) im Hintergrund-Thread."""
        if self.step_path is None and self.shape is None:
            return
        if self._busy:
            return
        self._busy = True
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

    def refresh_view(self):
        """Ansichtsphase (Schnittplanung) im Hintergrund mit Ladebalken."""
        if self.geo is None or getattr(self, "_busy_view", False):
            return
        self._busy_view = True
        self._apply_overrides()
        q = queue.Queue()
        geo, view = self.geo, self.view_var.get()
        d, rot = self.dir_var.get(), self._rotation()

        fk = frozenset(self.force_keys)
        sk = frozenset(self.skip_keys)

        def worker():
            try:
                q.put(("ok", hd.analyze_view(geo, view, d, rotation=rot,
                                             force_keys=fk, skip_keys=sk)))
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
                self._refresh()
            else:
                self.lbl_status.config(text="Fehler.")
                messagebox.showerror("Analysefehler", str(payload))

        poll()

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
    def _refresh(self):
        self._progress_off()
        feats = self.result["features"]
        rows = hd.table_rows(feats)

        self.tree.delete(*self.tree.get_children())
        by_no = {}
        for f in feats:
            by_no.setdefault(f.type_no, f)
        for r in rows:
            f = by_no.get(r["Nr"])
            mark = ""
            if f is not None:
                k = hd.type_key(f)
                if k in self.force_keys:
                    mark = "⚑ "
                elif k in self.skip_keys:
                    mark = "∅ "
            self.tree.insert("", "end",
                             values=(r["Nr"], mark + str(r["Bezeichnung"]),
                                     r["Durchmesser"], r["Art"],
                                     r["Tiefe"], r["Anzahl"]))

        self.lst_sec.delete(0, "end")
        self._section_feats = list(self.result["section_hits"])
        for f in self._section_feats:
            ori = {"axial": "axial", "quer": "quer/längs"}.get(
                f.orientation, f.orientation)
            self.lst_sec.insert(
                "end", f" {f.type_no:>2}  {f.label:<7} "
                       f"{'durch' if f.through else 'Sack':<5} {ori}")

        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", self.result["section_text"])

        self._hl_artist = None       # Achse wird geleert
        self.ax.clear()
        hr.draw_view(self.ax, self.result, self.view_var.get())
        self.fig.tight_layout()
        self.canvas.draw()
        self._home_lims = (self.ax.get_xlim(), self.ax.get_ylim())
        if hasattr(self.toolbar, "update"):
            self.toolbar.update()    # Ansichts-Historie zuruecksetzen
        self.toolbar.push_current()

        n = len(feats)
        n_ax = sum(1 for f in feats if f.orientation == "axial")
        n_q = n - n_ax
        nseg = (self.result.get("raw_cyl_segments", 0)
                + self.result.get("raw_cone_segments", 0))
        self.lbl_status.config(
            text=f"✔ {n} Formelemente ({len(rows)} Typen) — {n_ax} axial, "
                 f"{n_q} quer/schräg — {nseg} Rohsegmente — "
                 f"Erkennung v{hd.__version__}")

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

    # ------------------------------------------------- Navigation/Fokus
    def _on_section_click(self, _ev):
        sel = self.lst_sec.curselection()
        if sel and sel[0] < len(self._section_feats):
            self._goto_feature(self._section_feats[sel[0]])

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
