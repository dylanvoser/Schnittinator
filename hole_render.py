# -*- coding: utf-8 -*-
"""
hole_render.py — Ansicht rendern (matplotlib, GUI-unabhaengig).

Darstellung:
  Axialbohrung : Vollkreis + Fadenkreuz-Mittellinien
  Gewinde      : Kernkreis voll + Nennkreis als 3/4-Duennlinie (normnah)
  Senkbohrung  : zwei konzentrische Kreise
  Querbohrung  : GESTRICHELTES Rechteck (projizierte Kontur) mit
                 strichpunktierter Achslinie — klar unterscheidbar
  Stufenschnitt: dicke strichpunktierte Polylinie mit Pfeilen und "A"-Marken
"""
import math
from matplotlib.patches import Circle, Arc, Rectangle, FancyArrow
from matplotlib.lines import Line2D

from hole_detection import (VIEW_UV, VIEW_DIRS, feature_uv_extent, _dot, _norm,
                            ocp_static, box_limits)

# Farben (abgestimmt auf das Programm-Icon: Blau, Violett, Orange)
COL_AXIAL = "#1c2633"      # Bohrung axial
COL_THREAD = "#1565c0"     # Gewinde
COL_CROSS = "#c62828"      # Querbohrung
COL_SECTION = "#6a3fc8"    # Schnitt A-A (automatisch)
COL_SECTION2 = "#ef7d00"   # Schnitt B-B (manuell)
COL_HILITE = "#14a05a"     # Markierung "hinspringen"
COL_OUTLINE = "#3d4a5a"    # Aussenkontur
COL_CENTER = "#9aa5b1"     # Mittellinien
COL_FRAME = "#c3ccd6"      # Blattrahmen / Achsen
COL_MUTED = "#5d6b7c"
SHEET_BG = "#f3f5f8"       # Hintergrund um das Zeichenblatt


def style_axes(ax):
    """Ruhige, CAD-artige Blattoptik: weisses Blatt, feiner Rahmen,
    gedaempfte Achsbeschriftung."""
    ax.set_facecolor("#ffffff")
    for sp in ax.spines.values():
        sp.set_color(COL_FRAME)
        sp.set_linewidth(0.8)
    ax.tick_params(colors=COL_MUTED, labelsize=7.5, length=3, width=0.6)
    ax.xaxis.label.set_color(COL_MUTED)
    ax.yaxis.label.set_color(COL_MUTED)
    ax.xaxis.label.set_size(8)
    ax.yaxis.label.set_size(8)


def outline_uv(shape, view):
    """Bauteil-Umriss (Bounding Box in Bildkoordinaten) als einfacher Rahmen."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    ocp_static(BRepBndLib, "Add")(shape, box)
    x0, y0, z0, x1, y1, z1 = box_limits(box)
    ud, wd = VIEW_UV[view]
    us, vs = [], []
    for x in (x0, x1):
        for y in (y0, y1):
            for z in (z0, z1):
                us.append(_dot((x, y, z), ud))
                vs.append(_dot((x, y, z), wd))
    return min(us), min(vs), max(us), max(vs)


AXIS_NAME = {(1,0,0): "+X", (-1,0,0): "-X", (0,1,0): "+Y",
             (0,-1,0): "-Y", (0,0,1): "+Z", (0,0,-1): "-Z"}


def draw_coord_cross(ax, view, u0, v0, rotation=0):
    """Koordinatenkreuz links unten: zeigt, welche Weltachsen den
    Bildachsen u/v in dieser Ansicht (inkl. Drehung) entsprechen."""
    from hole_detection import effective_uv
    ud, wd = effective_uv(view, rotation)
    lu = AXIS_NAME.get(tuple(int(round(c)) for c in ud), "u")
    lv = AXIS_NAME.get(tuple(int(round(c)) for c in wd), "v")
    ox, oy, L = u0 - 14, v0 - 14, 12
    ax.add_patch(FancyArrow(ox, oy, L, 0, width=0.25, head_width=2.4,
                            head_length=2.6, color=COL_OUTLINE, zorder=6))
    ax.add_patch(FancyArrow(ox, oy, 0, L, width=0.25, head_width=2.4,
                            head_length=2.6, color=COL_OUTLINE, zorder=6))
    ax.annotate(lu, (ox+L+3, oy), fontsize=9, color=COL_OUTLINE,
                fontweight="bold", va="center")
    ax.annotate(lv, (ox, oy+L+3), fontsize=9, color=COL_OUTLINE,
                fontweight="bold", ha="center")


def draw_view(ax, result, view, show_section=True, hidden_types=frozenset()):
    import math as _m
    import numpy as _np
    from matplotlib.collections import LineCollection

    shape = result["shape"]
    feats = result["features"]
    poly = result.get("section_poly", [])
    rot = result.get("rotation", 0)

    u0, v0, u1, v1 = result.get("bounds") or outline_uv(shape, view)

    buckets = {}

    def add(pts, color, lw, ls="solid"):
        buckets.setdefault((color, lw, ls), []).append(_np.asarray(pts))

    def circle(cx, cy, r, n=40):
        a = _np.linspace(0, 2*_np.pi, n)
        return _np.column_stack((cx + r*_np.cos(a), cy + r*_np.sin(a)))

    def arc(cx, cy, r, a0, a1, n=28):
        a = _np.linspace(_m.radians(a0), _m.radians(a1), n)
        return _np.column_stack((cx + r*_np.cos(a), cy + r*_np.sin(a)))

    outline = result.get("outline")
    if outline:
        for pts in outline:
            add(pts, COL_OUTLINE, 1.2)
    else:
        add([(u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0)],
            COL_OUTLINE, 1.4)

    labels = []   # (x, y, text, color)
    for f in feats:
        u, v = f.uv
        if f.orientation == "axial":
            r = f.diameter / 2.0
            if f.kind == "Gewinde":
                add(circle(u, v, r*0.85), COL_THREAD, 1.4)
                add(arc(u, v, r, 0, 270), COL_THREAD, 0.8)
                lab_col = COL_THREAD
            else:
                add(circle(u, v, r), COL_AXIAL, 1.4)
                if f.counterbore:
                    add(circle(u, v, f.counterbore[0]/2), COL_AXIAL, 0.9)
                    if getattr(f, "counterbore_both", False):
                        add(circle(u, v, f.counterbore[0]/2 + 0.7),
                            COL_AXIAL, 0.6, "dotted")
                if f.kind == "Kegelsenkung":
                    add(circle(u, v, r*1.8), COL_AXIAL, 0.7, "dotted")
                lab_col = COL_AXIAL
            m = max(r*1.4, 3.0)
            add([(u-m, v), (u+m, v)], COL_CENTER, 0.5, (0, (8, 2, 1, 2)))
            add([(u, v-m), (u, v+m)], COL_CENTER, 0.5, (0, (8, 2, 1, 2)))
            if f.type_no not in hidden_types:
                labels.append((u + m*0.75, v + m*0.75, str(f.type_no), lab_col))
        else:
            a, b = feature_uv_extent(f, view, rot)
            du, dv = b[0]-a[0], b[1]-a[1]
            ln = _m.hypot(du, dv)
            if ln < 1e-9:
                continue
            nu, nv = -dv/ln, du/ln
            r = f.diameter/2.0
            add([(a[0]+nu*r, a[1]+nv*r), (b[0]+nu*r, b[1]+nv*r),
                 (b[0]-nu*r, b[1]-nv*r), (a[0]-nu*r, a[1]-nv*r),
                 (a[0]+nu*r, a[1]+nv*r)], COL_CROSS, 1.2, "dashed")
            add([(a[0], a[1]), (b[0], b[1])], COL_CROSS, 0.7,
                (0, (10, 3, 1, 3)))
            mu, mv = (a[0]+b[0])/2, (a[1]+b[1])/2
            if f.type_no not in hidden_types:
                labels.append((mu + 3, mv + r + 2, str(f.type_no), COL_CROSS))

    for (color, lw, ls), segs in buckets.items():
        ax.add_collection(LineCollection(segs, colors=color, linewidths=lw,
                                         linestyles=ls, zorder=2))
    for (x, y, s, col) in labels:
        ax.text(x, y, s, fontsize=8.5, color=col, fontweight="bold",
                zorder=5, clip_on=True)

    if show_section and poly:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        ax.plot(xs, ys, color=COL_SECTION, lw=2.0, ls=(0, (12, 3, 2, 3)),
                alpha=0.9, zorder=4)
        for (px, py), (qx, qy), lab in ((poly[0], poly[1], "A"),
                                        (poly[-1], poly[-2], "A")):
            dx, dy = qx-px, qy-py
            n = _m.hypot(dx, dy) or 1.0
            ax.annotate(lab, (px, py), (px - 5*dy/n, py + 5*dx/n),
                        fontsize=11, color=COL_SECTION, fontweight="bold")
            ax.add_patch(FancyArrow(px, py, -4*dy/n, 4*dx/n, width=0.3,
                                    head_width=2.2, head_length=2.2,
                                    color=COL_SECTION))

    poly2 = result.get("section2_poly") or []
    hits2 = result.get("section2_hits") or []
    for f in hits2:
        u, v = f.uv
        ax.plot([u], [v], marker="o", markersize=9, mfc="none",
                mec=COL_SECTION2, mew=1.8, zorder=6)
    if show_section and len(poly2) >= 2:
        xs = [p[0] for p in poly2]
        ys = [p[1] for p in poly2]
        ax.plot(xs, ys, color=COL_SECTION2, lw=2.0,
                ls=(0, (10, 3, 2, 3, 2, 3)), alpha=0.9, zorder=4)
        for (px, py), (qx, qy), lab in ((poly2[0], poly2[1], "B"),
                                        (poly2[-1], poly2[-2], "B")):
            dx, dy = qx-px, qy-py
            n = _m.hypot(dx, dy) or 1.0
            ax.annotate(lab, (px, py), (px - 5*dy/n, py + 5*dx/n),
                        fontsize=11, color=COL_SECTION2, fontweight="bold")
            ax.add_patch(FancyArrow(px, py, -4*dy/n, 4*dx/n, width=0.3,
                                    head_width=2.2, head_length=2.2,
                                    color=COL_SECTION2))

    draw_coord_cross(ax, view, u0, v0, rot)

    style_axes(ax)
    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.margins(0.10)
    ax.set_xlabel("u [mm]")
    ax.set_ylabel("v [mm]")
    dname = result.get("direction", "horizontal")
    rtxt = f"  ·  {rot}° gedreht" if rot else ""
    ax.set_title(f"Ansicht {view}{rtxt}  ·  Stufenschnitt A-A {dname}",
                 loc="left", fontsize=9, color=COL_MUTED, pad=8)
    handles = [
        Line2D([], [], color=COL_AXIAL, lw=1.4, label="Bohrung (axial)"),
        Line2D([], [], color=COL_THREAD, lw=1.4, label="Gewinde"),
        Line2D([], [], color=COL_CROSS, lw=1.2, ls="--", label="Querbohrung"),
        Line2D([], [], color=COL_SECTION, lw=1.8, ls="-.", label="Schnitt A-A"),
    ]
    if poly2:
        handles.append(Line2D([], [], color=COL_SECTION2, lw=1.8, ls="-.",
                              label="Schnitt B-B (manuell)"))
    leg = ax.legend(handles=handles, loc="upper right", fontsize=7,
                    framealpha=0.95, fancybox=False, borderpad=0.6)
    leg.get_frame().set_edgecolor(COL_FRAME)
    leg.get_frame().set_linewidth(0.8)
    for t in leg.get_texts():
        t.set_color("#33404f")
