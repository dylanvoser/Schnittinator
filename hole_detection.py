# -*- coding: utf-8 -*-
"""
hole_detection.py
=================
Bohrungserkennung aus STEP-Dateien (OCP / OpenCascade).

Kernpunkte (entsprechen den kritischen Anforderungen):
  1. Vollständigkeit: ALLE zylindrischen/konischen Flächen werden eingesammelt;
     Segmente derselben Achse+Radius werden zu EINER Bohrung fusioniert
     (auch teilumlaufende Flächen).
  2. Gewinde erscheinen genau EINMAL (Kern+Nennzylinder werden fusioniert;
     Kernloch-Durchmessertabelle für STEP ohne Gewindegeometrie).
  3. Gleiche Formelemente bekommen dieselbe Typ-Nummer.
  4. Querbohrungen werden relativ zur gewählten Ansicht klassifiziert.
  5. Verrundungen/Radien werden über Konvexität + Winkelabdeckung gefiltert.
  6. Stufenschnitt-Planung: monotone gestufte Schnittlinie, die maximal viele
     verschiedene Bohrungen genau einmal trifft.
"""

import math
from dataclasses import dataclass, field

__version__ = "2.10"

from OCP.STEPControl import STEPControl_Reader
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import (TopAbs_FACE, TopAbs_SOLID, TopAbs_State,
                        TopAbs_EDGE)
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Cone
from OCP.BRepGProp import BRepGProp_Face
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Pnt, gp_Vec, gp_Dir

# ----------------------------------------------------------------------------
# Toleranzen / Konstanten
# ----------------------------------------------------------------------------
TOL_AXIS_ANG = 1e-3          # rad, Achsen-Parallelität
TOL_AXIS_DIST = 0.05         # mm, Achsabstand für "gleiche Achse"
TOL_RADIUS = 0.02            # mm, gleicher Radius
TOL_DEPTH = 0.1              # mm, Tiefen-Rundung für Typgleichheit
MIN_COVERAGE_DEG = 181.0     # Mindest-Winkelabdeckung, um als Bohrung zu gelten
                             # (Verrundungen liegen typ. bei <= 90..100 Grad)
THROUGH_CLEARANCE = 0.5      # mm, Prüfpunkt-Abstand hinter Bohrungsende

# Kernloch-Durchmesser -> metrisches Gewinde (Regelgewinde), editierbar/erweiterbar
DEFAULT_THREAD_CORE_MAP = {
    1.6: "M2", 2.05: "M2.5", 2.5: "M3", 3.3: "M4", 4.2: "M5",
    5.0: "M6", 6.0: "M7", 6.8: "M8", 8.5: "M10", 10.2: "M12",
    12.0: "M14", 14.0: "M16", 15.5: "M18", 17.5: "M20", 19.5: "M22",
    21.0: "M24", 26.5: "M30",
}
THREAD_CORE_TOL = 0.12       # mm Toleranz auf Kernloch-Durchmesser

# Nenndurchmesser der Gewinde (für Kern+Nenn-Zylinderpaar-Erkennung)
THREAD_NOMINAL = {
    "M2": 2.0, "M2.5": 2.5, "M3": 3.0, "M4": 4.0, "M5": 5.0, "M6": 6.0,
    "M7": 7.0, "M8": 8.0, "M10": 10.0, "M12": 12.0, "M14": 14.0,
    "M16": 16.0, "M18": 18.0, "M20": 20.0, "M22": 22.0, "M24": 24.0,
    "M30": 30.0,
}

VIEW_DIRS = {
    "front":  (0, -1, 0), "back":  (0, 1, 0),
    "top":    (0, 0, -1), "bottom": (0, 0, 1),
    "left":   (1, 0, 0),  "right": (-1, 0, 0),
}
def effective_uv(view, rotation=0):
    """Bildachsen (u,v) der Ansicht, um rotation Grad (0/90/180/270, CCW)
    gedreht — 180 = 'auf den Kopf'."""
    ud, wd = VIEW_UV[view]
    r = int(rotation) % 360
    if r == 0:
        return ud, wd
    neg = lambda x: (-x[0], -x[1], -x[2])
    if r == 90:
        return neg(wd), ud
    if r == 180:
        return neg(ud), neg(wd)
    if r == 270:
        return wd, neg(ud)
    raise ValueError("rotation muss 0/90/180/270 sein")


# Projektionsachsen (u=horizontal, v=vertikal im Bild) je Ansicht
VIEW_UV = {
    "front":  ((1, 0, 0), (0, 0, 1)),
    "back":   ((-1, 0, 0), (0, 0, 1)),
    "top":    ((1, 0, 0), (0, 1, 0)),
    "bottom": ((1, 0, 0), (0, -1, 0)),
    "left":   ((0, 1, 0), (0, 0, 1)),
    "right":  ((0, -1, 0), (0, 0, 1)),
}


# ----------------------------------------------------------------------------
# Datenstrukturen
# ----------------------------------------------------------------------------
@dataclass
class CylSeg:
    """Ein Zylinderflächen-Segment (roh, aus einer Face)."""
    axis_pt: tuple
    axis_dir: tuple          # normiert, kanonisiert (erste nicht-0-Komponente > 0)
    radius: float
    u0: float                # Winkelintervall (im Zylinder-Parameterraum)
    u1: float
    v0: float                # axiale Lage (Projektion auf axis_dir, absolut)
    v1: float
    concave: bool            # True = Innenfläche (Bohrwand)


@dataclass
class ConeSeg:
    axis_pt: tuple
    axis_dir: tuple
    concave: bool
    v0: float
    v1: float


@dataclass
class HoleCylinder:
    """Fusionierte Bohrwand: alle Segmente gleicher Achse + gleicher Radius."""
    axis_pt: tuple
    axis_dir: tuple
    radius: float
    z0: float                # axiale Ausdehnung (entlang axis_dir)
    z1: float
    coverage_deg: float
    n_segments: int
    spans: tuple = ()        # rohe (v0,v1)-Teilspannen der Einzelflaechen --
                             # interne Flaechengrenzen markieren z.B. das
                             # Gewindeende eines Teilgewindes


@dataclass
class Feature:
    """Ein Formelement (Bohrung / Gewinde / Senkung ...)."""
    kind: str                # 'Bohrung' | 'Gewinde' | 'Senkbohrung' | 'Kegelsenkung'
    diameter: float          # Nenndurchmesser (bei Gewinde: Nennmass, z.B. 8 fuer M8)
    label: str               # z.B. 'M8' oder 'D8'
    depth: float
    through: bool
    axis_pt: tuple
    axis_dir: tuple
    z0: float
    z1: float
    cylinders: list = field(default_factory=list)
    counterbore: tuple = None    # (D, tiefe) falls Senkung vorhanden
    type_no: int = 0             # gleiche Formelemente = gleiche Nummer
    orientation: str = "axial"   # 'axial' | 'quer' | 'schraeg' (bzgl. Ansicht)
    uv: tuple = (0.0, 0.0)       # Bildkoordinaten in der Ansicht


# ----------------------------------------------------------------------------
# Geometrie-Helfer
# ----------------------------------------------------------------------------
def _canon_dir(d):
    """Richtung normieren und Vorzeichen kanonisieren (Achse ist ungerichtet)."""
    n = math.sqrt(d[0]**2 + d[1]**2 + d[2]**2)
    d = (d[0]/n, d[1]/n, d[2]/n)
    for c in d:
        if abs(c) > 1e-9:
            return d if c > 0 else (-d[0], -d[1], -d[2])
    return d


def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]


def _sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def _line_dist(p, lp, ld):
    """Abstand Punkt p zur Linie (lp, ld)."""
    w = _sub(p, lp)
    t = _dot(w, ld)
    d = (w[0]-t*ld[0], w[1]-t*ld[1], w[2]-t*ld[2])
    return math.sqrt(_dot(d, d))


def _same_axis(a_pt, a_dir, b_pt, b_dir):
    cosang = abs(_dot(a_dir, b_dir))
    if cosang < math.cos(TOL_AXIS_ANG):
        return False
    return _line_dist(b_pt, a_pt, a_dir) <= TOL_AXIS_DIST


def _foot(p, ld):
    """Skalare Position von p entlang Richtung ld (Ursprung Weltnullpunkt)."""
    return _dot(p, ld)


def _merge_angular_intervals(intervals):
    """Winkelintervalle auf dem Einheitskreis vereinigen -> Gesamtabdeckung in Grad."""
    norm = []
    for (a, b) in intervals:
        span = b - a
        if span >= 2*math.pi - 1e-6:
            return 360.0
        a %= 2*math.pi
        b = a + span
        if b <= 2*math.pi:
            norm.append((a, b))
        else:
            norm.append((a, 2*math.pi))
            norm.append((0.0, b - 2*math.pi))
    norm.sort()
    total = 0.0
    cur_a, cur_b = norm[0]
    for a, b in norm[1:]:
        if a <= cur_b + 1e-6:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    total += cur_b - cur_a
    return math.degrees(min(total, 2*math.pi))


# ----------------------------------------------------------------------------
# STEP laden
# ----------------------------------------------------------------------------
def load_step(path):
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)
    if status != IFSelect_RetDone:
        raise IOError(f"STEP konnte nicht gelesen werden: {path}")
    reader.TransferRoots()
    return reader.OneShape()


# ----------------------------------------------------------------------------
# Flächen einsammeln
# ----------------------------------------------------------------------------
def collect_segments(shape):
    """Alle Zylinder- und Kegelflächen-Segmente mit Konkavitätsinfo einsammeln."""
    cyls, cones = [], []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        ad = BRepAdaptor_Surface(face)
        stype = ad.GetType()
        if stype in (GeomAbs_Cylinder, GeomAbs_Cone):
            u0, u1 = ad.FirstUParameter(), ad.LastUParameter()
            v0, v1 = ad.FirstVParameter(), ad.LastVParameter()
            um, vm = 0.5*(u0+u1), 0.5*(v0+v1)
            # Normale unter Beruecksichtigung der Face-Orientierung:
            gpf = BRepGProp_Face(face)
            pnt, nrm = gp_Pnt(), gp_Vec()
            gpf.Normal(um, vm, pnt, nrm)
            if stype == GeomAbs_Cylinder:
                cyl = ad.Cylinder()
                ax = cyl.Axis()
                lp = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
                ld = _canon_dir((ax.Direction().X(), ax.Direction().Y(),
                                 ax.Direction().Z()))
                p = (pnt.X(), pnt.Y(), pnt.Z())
                t = _dot(_sub(p, lp), ld)
                foot = (lp[0]+t*ld[0], lp[1]+t*ld[1], lp[2]+t*ld[2])
                radial = _sub(p, foot)
                concave = (nrm.X()*radial[0] + nrm.Y()*radial[1]
                           + nrm.Z()*radial[2]) < 0.0
                # axiale Ausdehnung: Eckpunkte der Parameterflaeche projizieren
                zs = []
                for uu in (u0, u1):
                    for vv in (v0, v1):
                        q = ad.Value(uu, vv)
                        zs.append(_foot((q.X(), q.Y(), q.Z()), ld))
                # Winkelintervall ggf. auf kanonisierte Achse umrechnen:
                # bei gespiegelter Achse kehrt sich der Umlaufsinn um, die
                # ABDECKUNG (Spannweite) bleibt identisch -> wir speichern die
                # Spannweite ueber eine achsunabhaengige Winkelmessung:
                cyls.append(CylSeg(lp, ld, cyl.Radius(), u0, u1,
                                   min(zs), max(zs), concave))
            else:
                cone = ad.Cone()
                ax = cone.Axis()
                lp = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
                ld = _canon_dir((ax.Direction().X(), ax.Direction().Y(),
                                 ax.Direction().Z()))
                p = (pnt.X(), pnt.Y(), pnt.Z())
                t = _dot(_sub(p, lp), ld)
                foot = (lp[0]+t*ld[0], lp[1]+t*ld[1], lp[2]+t*ld[2])
                radial = _sub(p, foot)
                concave = (nrm.X()*radial[0] + nrm.Y()*radial[1]
                           + nrm.Z()*radial[2]) < 0.0
                zs = []
                for uu in (u0, u1):
                    for vv in (v0, v1):
                        q = ad.Value(uu, vv)
                        zs.append(_foot((q.X(), q.Y(), q.Z()), ld))
                cones.append(ConeSeg(lp, ld, concave, min(zs), max(zs)))
        ex.Next()
    return cyls, cones


# ----------------------------------------------------------------------------
# Segmente -> Bohrwaende fusionieren
# ----------------------------------------------------------------------------
def merge_cylinders(segs, tester=None):
    """Konkave Zylindersegmente gleicher Achse+Radius zu HoleCylinder fusionieren.

    Winkelabdeckung wird ueber die vereinigten U-Intervalle gemessen; da alle
    Segmente derselben OCC-Flaechenfamilie denselben Parameterursprung haben
    koennen ODER nicht, messen wir die Abdeckung geometrisch: wir rastern jedes
    Segmentintervall in Winkelpositionen relativ zu einer festen Referenz.
    """
    groups = []   # Liste von Listen von CylSeg
    for s in segs:
        if not s.concave:
            continue  # konvex = Aussenflaeche/Verrundung aussen -> nie Bohrung
        placed = False
        for g in groups:
            r = g[0]
            if (abs(r.radius - s.radius) <= TOL_RADIUS
                    and _same_axis(r.axis_pt, r.axis_dir, s.axis_pt, s.axis_dir)):
                g.append(s)
                placed = True
                break
        if not placed:
            groups.append([s])

    # gespiegelte/getrennte Bohrungen auf gleicher Achse auftrennen
    if tester is not None:
        split = []
        for g in groups:
            r = g[0]
            split.extend(_split_axial_components(
                g, tester, r.axis_pt, r.axis_dir,
                get_range=lambda s: (s.v0, s.v1)))
        groups = split

    holes = []
    for g in groups:
        ref = g[0]
        # geometrische Winkelmessung: Referenzrahmen senkrecht zur Achse
        d = ref.axis_dir
        # beliebiger Vektor nicht parallel zu d:
        a = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
        ex_ = _cross(d, a); ex_ = _norm(ex_)
        ey_ = _cross(d, ex_)
        intervals = []
        z0, z1 = float("inf"), float("-inf")
        for s in g:
            # Winkel der Intervallenden geometrisch bestimmen:
            ph = []
            for uu in (s.u0, 0.5*(s.u0+s.u1), s.u1):
                p = _cyl_point(s, uu)
                w = _sub(p, _foot_pt(p, ref.axis_pt, d))
                ang = math.atan2(_dot(w, ey_), _dot(w, ex_))
                ph.append(ang % (2*math.pi))
            span = abs(s.u1 - s.u0)
            if span >= 2*math.pi - 1e-6:
                intervals.append((0.0, 2*math.pi))
            else:
                # Richtung des Umlaufs aus Mittelpunktwinkel bestimmen
                a0 = ph[0]
                intervals.append((a0, a0 + span))
            z0 = min(z0, s.v0)
            z1 = max(z1, s.v1)
        cov = _merge_angular_intervals(intervals)
        spans = tuple(sorted(set((round(s.v0, 3), round(s.v1, 3))
                                 for s in g)))
        holes.append(HoleCylinder(ref.axis_pt, d, ref.radius, z0, z1,
                                  cov, len(g), spans))
    return holes


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _norm(v):
    n = math.sqrt(_dot(v, v))
    return (v[0]/n, v[1]/n, v[2]/n)


def _foot_pt(p, lp, ld):
    t = _dot(_sub(p, lp), ld)
    return (lp[0]+t*ld[0], lp[1]+t*ld[1], lp[2]+t*ld[2])


def _cyl_point(seg: CylSeg, u):
    """Punkt auf dem Original-Zylinder bei Winkel u (OCC-Parametrisierung
    approximiert ueber eigenen Rahmen; fuer Winkel-ABDECKUNG ausreichend)."""
    d = seg.axis_dir
    a = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    ex_ = _norm(_cross(d, a))
    ey_ = _cross(d, ex_)
    r = seg.radius
    lp = seg.axis_pt
    return (lp[0] + r*(math.cos(u)*ex_[0] + math.sin(u)*ey_[0]),
            lp[1] + r*(math.cos(u)*ex_[1] + math.sin(u)*ey_[1]),
            lp[2] + r*(math.cos(u)*ex_[2] + math.sin(u)*ey_[2]))


# ----------------------------------------------------------------------------
# Radien/Verrundungs-Filter
# ----------------------------------------------------------------------------
def filter_fillets(holes):
    """Nur echte Bohrungen behalten: konkav (bereits erzwungen) UND
    Winkelabdeckung >= MIN_COVERAGE_DEG. Innen-Verrundungen decken typ.
    <= ~100 Grad ab; echte Bohrungen (auch angeschnittene) > 180 Grad."""
    return [h for h in holes if h.coverage_deg >= MIN_COVERAGE_DEG]


# ----------------------------------------------------------------------------
# Durchgangs-/Sackloch-Pruefung
# ----------------------------------------------------------------------------
class SolidTester:
    """Ein einziger, wiederverwendeter Klassifikator (grosser Performancegewinn
    gegenueber Neubau pro Abfrage)."""
    def __init__(self, shape):
        self._c = BRepClass3d_SolidClassifier(shape)

    def is_outside(self, x, y, z):
        self._c.Perform(gp_Pnt(x, y, z), 1e-6)
        return self._c.State() == TopAbs_State.TopAbs_OUT


THROUGH_CONFIRM_DIST = 10.0   # mm, zweite (tiefere) Bestaetigungspruefung

def is_through(tester: "SolidTester", hole: HoleCylinder):
    """Durchgangspruefung an beiden Enden. Geprueft wird NICHT nur ein
    einzelner Punkt knapp hinter dem Ende: eine kleine, unabhaengige Tasche
    oder Kreuzungsbohrung direkt hinter dem eigentlichen Bohrungsende kann
    dort zufaellig ebenfalls 'ausserhalb' liefern und eine Sacklochbohrung
    faelschlich als durchgehend erscheinen lassen. Eine zweite, deutlich
    tiefere Pruefung (THROUGH_CONFIRM_DIST) muss ebenfalls 'ausserhalb'
    sein -- eine echte Durchgangsoeffnung bleibt dort zuverlaessig offen,
    eine begrenzte Nachbarkavitaet dagegen nicht."""
    d = hole.axis_dir
    lp = hole.axis_pt
    base = _foot(lp, d)
    res = []
    for z, sgn in ((hole.z0, -1.0), (hole.z1, +1.0)):
        ok = True
        for clearance in (THROUGH_CLEARANCE, THROUGH_CONFIRM_DIST):
            t = (z - base) + sgn * clearance
            if not tester.is_outside(lp[0]+t*d[0], lp[1]+t*d[1], lp[2]+t*d[2]):
                ok = False
                break
        res.append(ok)
    return res[0] and res[1], res


def _gap_has_material(tester, axis_pt, axis_dir, base, lo, hi):
    """Prueft MEHRERE Punkte in der Luecke [lo,hi] (axiale Position,
    absolut) auf Material. Ein einzelner Punkt (z.B. genau der Mittelpunkt)
    kann bei langen Luecken zufaellig in einer voellig unabhaengigen
    Kavitaet (Tasche, Kreuzungsbohrung) liegen und so faelschlich 'kein
    Material' anzeigen, obwohl entlang der Luecke echtes Material liegt --
    das fusioniert zwei tatsaechlich getrennte Sacklochbohrungen (z.B. von
    gegenueberliegenden Seiten eines langen Blocks) faelschlich zu einer
    scheinbaren Durchgangsbohrung."""
    span = hi - lo
    if span <= 0:
        return False
    n = min(max(3, int(span // 15) + 1), 15)
    for i in range(1, n + 1):
        tm = lo + span * i / (n + 1) - base
        p = (axis_pt[0]+tm*axis_dir[0], axis_pt[1]+tm*axis_dir[1],
             axis_pt[2]+tm*axis_dir[2])
        if not tester.is_outside(*p):
            return True
    return False


def _split_axial_components(items, tester, axis_pt, axis_dir,
                            get_range, touch_tol=0.3):
    """Objekte auf derselben Achse in zusammenhaengende Bohrungen trennen.
    Getrennt wird bei einer axialen Luecke, in der IRGENDWO Material liegt
    (gespiegelte Bohrungen von zwei Seiten). Ist die gesamte Luecke ein
    Hohlraum (z.B. kreuzende Bohrung), bleibt es EINE Bohrung."""
    items = sorted(items, key=lambda it: get_range(it)[0])
    base = _foot(axis_pt, axis_dir)
    comps = [[items[0]]]
    cur_end = get_range(items[0])[1]
    for it in items[1:]:
        z0, z1 = get_range(it)
        gap = z0 - cur_end
        if gap > touch_tol:
            if _gap_has_material(tester, axis_pt, axis_dir, base,
                                 cur_end, z0):      # Material dazwischen -> trennen
                comps.append([])
        comps[-1].append(it)
        cur_end = max(cur_end, z1)
    return comps


# ----------------------------------------------------------------------------
# Achsgruppen -> Features (Gewinde, Senkungen, einfache Bohrungen)
# ----------------------------------------------------------------------------
def build_features(shape, holes, cones, thread_map=None,
                   thread_requires_chamfer=True, tester=None):
    """thread_requires_chamfer=True (empfohlen): Nur-Kernloch wird nur dann
    als Gewinde klassifiziert, wenn eine Ansenkfase (konkaver Kegel) am
    Eintritt liegt. False: reine Durchmessertabelle."""
    thread_map = dict(DEFAULT_THREAD_CORE_MAP) if thread_map is None else thread_map
    tester = tester or SolidTester(shape)

    # 1) Bohrwaende nach Achse gruppieren (radienuebergreifend) ...
    axis_groups = []
    for h in holes:
        placed = False
        for g in axis_groups:
            r = g[0]
            if _same_axis(r.axis_pt, r.axis_dir, h.axis_pt, h.axis_dir):
                g.append(h)
                placed = True
                break
        if not placed:
            axis_groups.append([h])
    # ... und in zusammenhaengende Bohrungen trennen (gespiegelte Bohrungen!)
    split_groups = []
    for g in axis_groups:
        r = g[0]
        split_groups.extend(_split_axial_components(
            g, tester, r.axis_pt, r.axis_dir,
            get_range=lambda h: (h.z0, h.z1)))
    axis_groups = split_groups

    features = []
    for g in axis_groups:
        g.sort(key=lambda h: h.radius)
        radii = [h.radius for h in g]
        cyl_z0 = min(h.z0 for h in g)
        cyl_z1 = max(h.z1 for h in g)
        main = g[0]
        # konkave Kegel NUR, wenn sie axial an DIESE Bohrung anschliessen
        ax_cones = [c for c in cones
                    if c.concave
                    and _same_axis(main.axis_pt, main.axis_dir,
                                   c.axis_pt, c.axis_dir)
                    and c.v0 <= cyl_z1 + 0.5 and c.v1 >= cyl_z0 - 0.5]

        # Durchgangspruefung MIT voll erweiterten (Kegel-inklusive) Enden --
        # ein Testpunkt knapp hinter der reinen Zylinderkante koennte noch
        # innerhalb einer angrenzenden Bohrerspitze liegen und faelschlich
        # als "Material dahinter" durchgehen, statt am wirklichen Ende
        # (ggf. weit hinter dem Kegel) zu pruefen.
        full_z0, full_z1 = cyl_z0, cyl_z1
        for c in ax_cones:
            full_z0 = min(full_z0, c.v0)
            full_z1 = max(full_z1, c.v1)
        through, res = is_through(tester, HoleCylinder(
            main.axis_pt, main.axis_dir, main.radius, full_z0, full_z1,
            360, 1))

        # Kegel am EINTRITT (offenes Ende) vs. an der BOHRSPITZE (Sackloch-
        # Ende) unterscheiden: nur der Eintritts-Kegel ist eine echte
        # Ansenkfase/Senkung; ein Kegel an der Bohrspitze ist nur die
        # Bohrerform und zaehlt weder als "Kegelsenkung"-Merkmal noch zur
        # Gewinde-/Bohrtiefe. Ein Kegel zaehlt nur dann als Eintritts- bzw.
        # Bohrspitzen-Kegel, wenn er UNMITTELBAR an die aeussere Kante der
        # Bohrung anschliesst (nicht nur "naeher an der einen Seite") --
        # ein interner Uebergangskegel (z.B. Kern- auf Senkungsdurchmesser
        # bei einer Senkbohrung) beruehrt keine der beiden Aussenkanten und
        # zaehlt zu keinem der beiden Faelle.
        if through or not ax_cones:
            entry_cones, blind_cones = list(ax_cones), []
        else:
            tol = 0.5
            entry_at_lo = res[0]
            entry_z = cyl_z0 if entry_at_lo else cyl_z1
            blind_z = cyl_z1 if entry_at_lo else cyl_z0
            entry_cones, blind_cones = [], []
            for c in ax_cones:
                touches_entry = (abs(c.v0 - entry_z) <= tol
                                 or abs(c.v1 - entry_z) <= tol)
                touches_blind = (abs(c.v0 - blind_z) <= tol
                                 or abs(c.v1 - blind_z) <= tol)
                if touches_entry and not touches_blind:
                    entry_cones.append(c)
                elif touches_blind and not touches_entry:
                    blind_cones.append(c)
                # sonst: interner Uebergangskegel -- weder noch, ignorieren
        has_cone = bool(entry_cones)
        # Spanne (nicht Summe!) der Eintritts-Kegel-Segmente -- ein einzelner
        # physischer Kegel wird wie Zylinderflaechen oft an einer Naht in
        # mehrere Winkelsegmente mit IDENTISCHEM v0/v1 aufgeteilt.
        entry_ext = (0.0 if through or not entry_cones
                    else max(c.v1 for c in entry_cones)
                         - min(c.v0 for c in entry_cones))

        # Eintrittsseite + Fasenlaenge fuer die Gewindelaengen-Ermittlung:
        # bei Sackloechern liefert die Durchgangspruefung die offene Seite;
        # bei Durchgangsbohrungen verraet sie die Fase, sofern sie nur an
        # einem Ende sitzt.
        if through:
            tol_e = 0.5
            lo_c = [c for c in ax_cones
                    if abs(c.v1 - cyl_z0) <= tol_e or abs(c.v0 - cyl_z0) <= tol_e]
            hi_c = [c for c in ax_cones
                    if abs(c.v0 - cyl_z1) <= tol_e or abs(c.v1 - cyl_z1) <= tol_e]
            entry_known = bool(lo_c) != bool(hi_c)
            entry_lo = bool(lo_c)
            ec = lo_c or hi_c
            chamfer_ext = (max(c.v1 for c in ec) - min(c.v0 for c in ec)
                           if entry_known and ec else 0.0)
        else:
            entry_known = res[0] != res[1]
            entry_lo = res[0]
            chamfer_ext = entry_ext

        # --- Gewinde-Fall A: Kern- + Nennzylinder modelliert -----------------
        thread_label = None
        if len(g) >= 2:
            for name, dn in THREAD_NOMINAL.items():
                core = _core_for(name, thread_map)
                if core is None:
                    continue
                if (any(abs(2*r - dn) <= 0.1 for r in radii)
                        and any(abs(2*r - core) <= THREAD_CORE_TOL for r in radii)):
                    thread_label = name
                    break
        # --- Gewinde-Fall B: nur Kernloch, Zuordnung ueber Tabelle -----------
        thread_from_table = False
        if thread_label is None and len(g) == 1:
            dia = 2*g[0].radius
            for core, name in thread_map.items():
                if abs(dia - core) <= THREAD_CORE_TOL:
                    if (not thread_requires_chamfer) or has_cone:
                        thread_label = name
                        thread_from_table = True
                    break

        z0, z1 = full_z0, full_z1      # volle Ausdehnung inkl. ALLER Kegel
                                        # (Eintritt + Bohrspitze) -- fuer
                                        # Zeichnung/Feature-Position
        depth = z1 - z0                # volle Ausdehnung inkl. Kegel (Senkung!)
        depth_plain = cyl_z1 - cyl_z0  # ohne jeden Kegel (Bohrerspitze/Fase)

        if thread_label:
            # Gewindelaenge = Laenge des Nennzylinders (Teilgewinde!) plus
            # Eintritts-Ansenkfase, falls vorhanden -- die Fase gehoert zur
            # nutzbaren Gewindelaenge, die Bohrerspitze am Sackloch-Ende
            # dagegen nicht. Bei Nur-Kernloch-Gewinden (Fall B) endet ein
            # Teilgewinde ggf. mitten in der Bohrung: das CAD-Modell
            # markiert die Grenze als saubere Flaechenkante im radius-
            # gleichen Kernloch-Zylinder (_thread_len_from_spans).
            dn = THREAD_NOMINAL[thread_label]
            nom = [h for h in g if abs(2*h.radius - dn) <= 0.1]
            # Fase an der GEWINDE-Eintrittsseite (Uebergangs-/Fremdkegel am
            # anderen Ende gehoeren nicht zu diesem Gewinde): bei Fall A
            # liegt der Eintritt am aeusseren Ende des Nennzylinders, bei
            # Fall B an der ueber Fase/offene Seite bestimmten Seite.
            if nom:
                ent_lo = (abs(nom[0].z0 - cyl_z0)
                          <= abs(cyl_z1 - nom[0].z1))
                edge = cyl_z0 if ent_lo else cyl_z1
                ec = [c for c in ax_cones
                      if abs(c.v1 - edge) <= 0.5 or abs(c.v0 - edge) <= 0.5]
                fase = (max(c.v1 for c in ec) - min(c.v0 for c in ec)
                        if ec else 0.0)
            else:
                fase = chamfer_ext if entry_known else 0.0
            # Bohrtiefe: vom Gewindebeginn (inkl. Eintrittsfase) bis zum
            # Bohrungsende; beim Sackloch endet sie am Beginn der
            # Bohrerspitze (depth_plain schliesst diese bereits aus).
            bohr_tiefe = depth_plain + fase
            feat = Feature("Gewinde", THREAD_NOMINAL[thread_label], thread_label,
                           bohr_tiefe, through, main.axis_pt,
                           main.axis_dir, z0, z1, cylinders=g)
            feat.thread_from_table = thread_from_table
            if nom:
                feat.thread_len = (nom[0].z1 - nom[0].z0) + fase
            else:
                tl = (_thread_len_from_spans(getattr(g[0], "spans", ()),
                                             cyl_z0, cyl_z1, entry_lo)
                      if entry_known else None)
                feat.thread_len = ((tl + fase) if tl is not None
                                   else bohr_tiefe)
            # Durchgehendes Gewinde (Gewinde ueber die ganze Bohrung bei
            # einer Durchgangsbohrung): Laenge ist nur die Materialdicke,
            # keine echte Gewindeangabe -> None (Anzeige "durch", ein Typ
            # unabhaengig von der Dicke).
            if through and feat.thread_len >= depth_plain - 0.5:
                feat.thread_len = None
        elif len(g) >= 2:
            bigs = [h for h in g if h.radius > main.radius + TOL_RADIUS]
            big = max(bigs, key=lambda h: h.radius)
            cb_depth = big.z1 - big.z0
            both = len(bigs) >= 2   # Senkung an beiden Enden
            # Sackloch: Bohrerspitze am blinden Ende bleibt aussen vor (wie
            # bei Bohrung/Gewinde); Durchgang: beide Enden sind offen,
            # volle Ausdehnung bleibt wie gehabt.
            report_depth = depth if through else (depth_plain + entry_ext)
            feat = Feature("Senkbohrung", 2*main.radius,
                           f"D{_fmt(2*main.radius)}", report_depth, through,
                           main.axis_pt, main.axis_dir, z0, z1, cylinders=g,
                           counterbore=(2*big.radius, cb_depth))
            feat.counterbore_both = both
        elif has_cone:
            report_depth = depth if through else (depth_plain + entry_ext)
            feat = Feature("Kegelsenkung", 2*main.radius,
                           f"D{_fmt(2*main.radius)}", report_depth, through,
                           main.axis_pt, main.axis_dir, z0, z1, cylinders=g)
        else:
            feat = Feature("Bohrung", 2*main.radius, f"D{_fmt(2*main.radius)}",
                           depth_plain, through, main.axis_pt, main.axis_dir,
                           z0, z1, cylinders=g)
        features.append(feat)
    return features


def _core_for(name, thread_map):
    for core, n in thread_map.items():
        if n == name:
            return core
    return None


def _thread_len_from_spans(spans, cyl_z0, cyl_z1, entry_at_lo, tol=0.05):
    """Gewindeende eines Teilgewindes aus den Teilflaechen des Kernloch-
    Zylinders ableiten: endet das Gewinde vor dem Bohrungsende, modelliert
    das CAD die Grenze als saubere Kante -- zwei axial EXAKT aneinander-
    stossende Teilflaechen gleichen Radius. Liefert die Zylinderlaenge vom
    Eintritt bis zur ersten solchen Grenze, sonst None (= Gewinde ueber die
    ganze Bohrung). Unterbrechungen durch kreuzende Bohrungen erzeugen
    dagegen Ueberlappungen oder Luecken der Teilspannen und zaehlen nicht
    als Gewindegrenze."""
    uniq = sorted(set((round(a, 3), round(b, 3)) for a, b in spans))
    if len(uniq) < 2:
        return None
    bounds = []
    for (a1, b1), (a2, b2) in zip(uniq, uniq[1:]):
        if abs(a2 - b1) <= tol and b1 - cyl_z0 > tol and cyl_z1 - b1 > tol:
            bounds.append(b1)
    if not bounds:
        return None
    return (bounds[0] - cyl_z0) if entry_at_lo else (cyl_z1 - bounds[-1])


def _fmt(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------------------
# Typnummern: gleiche Formelemente = gleiche Nummer
# ----------------------------------------------------------------------------
def type_key(f):
    """Stabiler, ansichts-unabhaengiger Typschluessel (fuer Erzwingen /
    Nicht-noetig-Markierungen im GUI). Bei Gewinden zaehlt NUR die
    Gewindelaenge, nie die gesamte Bohrtiefe -- eine Teilgewindebohrung mit
    gleicher Gewindelaenge, aber anderer (tieferer) Kernbohrung bekommt so
    dieselbe Nummer wie eine kuerzere Bohrung mit demselben Gewinde.
    Durchgehende Gewinde (thread_len None) gelten unabhaengig von der
    Materialdicke als derselbe Typ."""
    depth_part = (None if f.through or f.kind == "Gewinde"
                 else round(f.depth/TOL_DEPTH))
    tl = getattr(f, "thread_len", None)
    thr_part = (round(tl/TOL_DEPTH)
                if f.kind == "Gewinde" and tl is not None else None)
    return (f.kind, f.label, depth_part, thr_part,
            f.through, f.counterbore and (round(f.counterbore[0], 2),
                                          round(f.counterbore[1], 1)))


def assign_type_numbers(features):
    def key(f):
        # Durchgangsbohrungen: Tiefe ist KEIN Typmerkmal (Bohrungen, die auf
        # verschiedenen Ebenen beginnen und daher verschieden lang sind,
        # bekommen dieselbe Nummer). Bei Gewinden IMMER (Durchgang wie
        # Sackloch) zaehlt stattdessen die GEWINDELAENGE (Teilgewinde-
        # Bohrungen mit gleichem Gewinde, aber anderer Bohrtiefe =
        # gleiche Nummer); durchgehende Gewinde (thread_len None) sind
        # unabhaengig von der Materialdicke derselbe Typ.
        depth_part = (None if f.through or f.kind == "Gewinde"
                     else round(f.depth/TOL_DEPTH))
        tl = getattr(f, "thread_len", None)
        thr_part = (round(tl/TOL_DEPTH)
                    if f.kind == "Gewinde" and tl is not None else None)
        return (f.kind, f.label, depth_part, thr_part,
                f.through, f.counterbore and (round(f.counterbore[0], 2),
                                              round(f.counterbore[1], 1)))
    types = {}
    order = []
    for f in features:
        k = key(f)
        if k not in types:
            order.append(k)
            types[k] = 0
    # Sortierung: Gewinde zuerst, dann nach Durchmesser
    def sort_key(k):
        kind, label, *_ = k
        f = next(f for f in features if key(f) == k)
        return (0 if kind == "Gewinde" else 1, f.diameter)
    order.sort(key=sort_key)
    for i, k in enumerate(order, start=1):
        types[k] = i
    for f in features:
        f.type_no = types[key(f)]
    return features


# ----------------------------------------------------------------------------
# Ansicht: Orientierung + Projektion
# ----------------------------------------------------------------------------
def classify_view(features, view="top", rotation=0):
    vd = _norm(VIEW_DIRS[view])
    ud, wd = effective_uv(view, rotation)
    for f in features:
        c = abs(_dot(f.axis_dir, vd))
        if c > 0.999:
            f.orientation = "axial"
        elif c < 0.001:
            f.orientation = "quer"
        else:
            f.orientation = "schraeg"
        f.uv = (_dot(f.axis_pt_mid(), ud) if hasattr(f, 'axis_pt_mid')
                else _dot(_mid(f), ud), _dot(_mid(f), wd))
    return features


def _mid(f: Feature):
    d = f.axis_dir
    base = _foot(f.axis_pt, d)
    tm = 0.5*(f.z0 + f.z1) - base
    return (f.axis_pt[0]+tm*d[0], f.axis_pt[1]+tm*d[1], f.axis_pt[2]+tm*d[2])


def feature_uv_extent(f: Feature, view, rotation=0):
    """Fuer Querbohrungen: projizierte Rechteck-Kontur (Laenge x Durchmesser)."""
    ud, wd = effective_uv(view, rotation)
    d = f.axis_dir
    base = _foot(f.axis_pt, d)
    p0 = tuple(f.axis_pt[i] + (f.z0-base)*d[i] for i in range(3))
    p1 = tuple(f.axis_pt[i] + (f.z1-base)*d[i] for i in range(3))
    a = (_dot(p0, ud), _dot(p0, wd))
    b = (_dot(p1, ud), _dot(p1, wd))
    return a, b


# ----------------------------------------------------------------------------
# Stufenschnitt
# ----------------------------------------------------------------------------
def plan_step_section(features, view="top", direction="horizontal",
                      bounds=None, cluster_tol=1.0, margin=2.0, rotation=0,
                      _mirror=False, force_keys=frozenset(),
                      skip_keys=frozenset()):
    """MONOTONER Stufenschnitt.

    Knoten:
      * Axialbohrungen  -> Punktknoten (Segment durch die Bohrmitte)
      * Querbohrungen, deren Achse PARALLEL zur Schnittrichtung liegt
        -> Intervallknoten: das Segment liegt auf der Bohrachse und
           ueberstreicht die GANZE Bohrungslaenge; Knicke nur ausserhalb
    Sperrflaechen:
      * Querbohrungen SENKRECHT zur Schnittrichtung (und schraege) werden
        weder aufgenommen noch von Segmenten/Spruengen gekreuzt.
    Garantien: streng monoton, Knicke ausserhalb aller Bohrungen, jede
    getroffene Bohrung voll geschnitten, keine Dopplung, keine Streifschnitte
    (Verletzungen werden durch Neuplanung eliminiert).
    """
    horiz = (direction == "horizontal")

    def to_st(p):
        return (p[0], p[1]) if horiz else (p[1], p[0])

    nodes = []       # dict(s0,s1,t,r,se0,se1,f,quer)
    obstacles = []   # (s0,s1,t0,t1) Sperr-Rechtecke
    for f in features:
        if f.orientation == "axial":
            s, t = to_st(f.uv)
            if _mirror:
                s = -s
            r = f.diameter/2.0
            k_ = type_key(f)
            nodes.append(dict(s0=s, s1=s, t=t, r=r,
                              se0=s-r, se1=s+r, f=f, quer=False,
                              forced=k_ in force_keys,
                              skip=k_ in skip_keys))
        elif f.orientation in ("quer", "schraeg"):
            a, b = feature_uv_extent(f, view, rotation)
            (sa, ta), (sb, tb) = to_st(a), to_st(b)
            if _mirror:
                sa, sb = -sa, -sb
            r = f.diameter/2.0
            if f.orientation == "quer" and abs(ta - tb) <= 0.1:
                # Achse parallel zur Schnittrichtung -> laengs schneidbar
                s0, s1 = min(sa, sb), max(sa, sb)
                k_ = type_key(f)
                nodes.append(dict(s0=s0, s1=s1, t=0.5*(ta+tb), r=r,
                                  se0=s0, se1=s1, f=f, quer=True,
                                  forced=k_ in force_keys,
                                  skip=k_ in skip_keys))
            else:
                # Radius nur SENKRECHT zur Achse aufblasen (laengs endet
                # der Bohrkoerper am Achsenende)
                ds, dt = sb - sa, tb - ta
                ln = math.hypot(ds, dt) or 1.0
                infl_s = r * abs(dt) / ln
                infl_t = r * abs(ds) / ln
                obstacles.append((min(sa, sb) - infl_s, max(sa, sb) + infl_s,
                                  min(ta, tb) - infl_t, max(ta, tb) + infl_t))

    if not nodes:
        return [], [], "Keine schnittbaren Bohrungen in dieser Ansicht."

    nodes.sort(key=lambda nd: (nd["s0"], nd["s1"]))
    n = len(nodes)
    n_ax = sum(1 for nd in nodes if not nd["quer"])
    n_qp = n - n_ax

    if bounds:
        (smin, smax) = (bounds[0], bounds[2]) if horiz else (bounds[1], bounds[3])
        if _mirror:
            smin, smax = -smax, -smin
    else:
        smin = min(nd["se0"] for nd in nodes) - 8
        smax = max(nd["se1"] for nd in nodes) + 8
    lead = 5.0

    import numpy as _np
    A_se0 = _np.array([m["se0"] for m in nodes])
    A_se1 = _np.array([m["se1"] for m in nodes])
    A_s0 = _np.array([m["s0"] for m in nodes])
    A_s1 = _np.array([m["s1"] for m in nodes])
    A_t = _np.array([m["t"] for m in nodes])
    A_r = _np.array([m["r"] for m in nodes])
    A_q = _np.array([m["quer"] for m in nodes])
    if obstacles:
        O0, O1, O2, O3 = (_np.array(x) for x in zip(*obstacles))
    else:
        O0 = O1 = O2 = O3 = _np.empty(0)

    _jb_cache = {}

    def jog_blocked(s, tlo, thi):
        key = (round(s, 4), round(tlo, 4), round(thi, 4))
        v = _jb_cache.get(key)
        if v is not None:
            return v
        v = _jog_blocked_calc(s, tlo, thi)
        _jb_cache[key] = v
        return v

    def _jog_blocked_calc(s, tlo, thi):
        skip = A_q & ((_np.abs(A_t - tlo) <= cluster_tol)
                      | (_np.abs(A_t - thi) <= cluster_tol))
        hit = (~skip & (A_se0 - 0.5 < s) & (s < A_se1 + 0.5)
               & (tlo - A_r - 0.5 < A_t) & (A_t < thi + A_r + 0.5))
        if hit.any():
            return True
        if len(O0) and ((O0 - 0.5 < s) & (s < O1 + 0.5)
                        & (tlo < O3) & (thi > O2)).any():
            return True
        return False

    _jog_cache = {}

    def _free_jog(lo, hi, tlo, thi, prefer=None, nodes_block=True):
        """EXAKTE freie Sprungposition in [lo,hi] fuer einen Quersprung
        zwischen den Ebenen tlo/thi: alle Blocker-Intervalle (Bohrungen,
        Sperrflaechen) werden verschmolzen; gewaehlt wird der freie Punkt,
        der 'prefer' am naechsten liegt. None wenn keine Luecke existiert."""
        if hi < lo:
            return None
        skip = A_q & ((_np.abs(A_t - tlo) <= cluster_tol)
                      | (_np.abs(A_t - thi) <= cluster_tol))
        ivals = []
        if nodes_block:
            rel = (~skip & (tlo - A_r - 0.5 < A_t) & (A_t < thi + A_r + 0.5))
            st = _np.maximum(A_se0[rel] - 0.5, lo)
            en = _np.minimum(A_se1[rel] + 0.5, hi)
            ivals = [(float(a), float(b)) for a, b in zip(st, en) if a < b]
        if len(O0):
            orel = (tlo < O3) & (thi > O2)
            ost = _np.maximum(O0[orel] - 0.5, lo)
            oen = _np.minimum(O1[orel] + 0.5, hi)
            ivals += [(float(a), float(b)) for a, b in zip(ost, oen) if a < b]
        ivals.sort()
        gaps = []
        cur = lo
        for a, b in ivals:
            if a - cur >= 0.2:
                gaps.append((cur, a))
            cur = max(cur, b)
        if hi - cur >= 0.2:
            gaps.append((cur, hi))
        if not gaps:
            return None
        if prefer is None:
            prefer = 0.5 * (lo + hi)
        best, bd = None, None
        for a, b in gaps:
            p = min(max(prefer, a), b)
            d = abs(p - prefer)
            if bd is None or d < bd:
                best, bd = p, d
        return best

    MIN_LEN = 1.0

    def cov_req(m):
        """Erforderliche Laengsabdeckung einer schnittparallelen Querbohrung:
        die GANZE Laenge; nur bei sehr langen Kanaelen (> 3x Durchmesser)
        genuegt eine Mindeststrecke von 3 Durchmessern, weil volle Laenge
        dort jede Stufe unmoeglich machen wuerde."""
        return min((m["s1"] - m["s0"]) - 0.5, 6.0 * m["r"])

    def jog_pos(i, j, relaxed=False):
        key = (i, j, relaxed)
        if key in _jog_cache:
            return _jog_cache[key]
        s = _jog_pos_calc(i, j, relaxed)
        _jog_cache[key] = s
        return s

    def _jog_pos_calc(i, j, relaxed=False):
        a, b = nodes[i], nodes[j]
        lo = (a["s0"] + max(margin, MIN_LEN)) if a["quer"] else (a["se1"] + margin)
        hi = (b["s1"] - max(margin, MIN_LEN)) if b["quer"] else (b["se0"] - margin)
        if hi < lo:
            return None
        tlo, thi = min(a["t"], b["t"]), max(a["t"], b["t"])
        prefer = min(max(a["s1"] + margin, lo), hi)
        s = _free_jog(lo, hi, tlo, thi, prefer)
        if s is None and relaxed:
            # Teilschnitt-Modus: nur Sperrflaechen bleiben harte Blocker;
            # gekreuzte Bohrungen werden zu tolerierten Teilschnitten,
            # sofern ihr Typ anderweitig voll geschnitten ist (validate).
            s = _free_jog(lo, hi, tlo, thi, prefer, nodes_block=False)
        return s

    def run_dp(forbidden, forbid_start, forbid_end):
        best = [1]*n
        par = [-1]*n
        jogs = {}
        for j in range(n):
            tj = nodes[j]["t"]
            s0j = nodes[j]["s0"]
            # Vorgaenger absteigend nach Kettenlaenge: sobald keine
            # Verbesserung mehr moeglich ist, abbrechen
            for i in sorted(range(j), key=lambda k: best[k], reverse=True):
                if best[i] + 1 <= best[j]:
                    break
                if (i, j) in forbidden:
                    continue
                if abs(nodes[i]["t"] - tj) <= cluster_tol:
                    ok = s0j >= nodes[i]["s0"] - 1e-9
                else:
                    s = jog_pos(i, j)
                    ok = s is not None
                    if ok:
                        jogs[(i, j)] = s
                if ok:
                    best[j] = best[i] + 1
                    par[j] = i
        return best, par, jogs

    def reconstruct(end_i, par):
        chain = []
        k = end_i
        while k >= 0:
            chain.append(k)
            k = par[k]
        chain.reverse()
        return chain

    def build_poly(chain, jogs):
        """Sprungpositionen mit Kenntnis der GANZEN Kette setzen:
        - jede Ketten-Bohrung der Quellebene vollstaendig abdecken
          (Laengsbohrungen: ganze Laenge, lange Kanaele >= 3x Durchmesser)
        - so SPAET wie moeglich springen, wenn die Quellebene einen laengeren
          Kanal traegt (maximiert dessen Laengsschnitt), sonst so frueh, dass
          die Zielebene maximal abgedeckt wird."""
        groups = []
        for idx in chain:
            if groups and abs(nodes[idx]["t"] - groups[-1][0]) <= cluster_tol:
                groups[-1][1].append(idx)
            else:
                groups.append((nodes[idx]["t"], [idx]))
        pts = [(smin - lead, groups[0][0])]
        prev = smin - lead
        if not hasattr(build_poly, "_memo"):
            build_poly._memo = {}
        for gi in range(len(groups) - 1):
            t_a, mem_a = groups[gi]
            t_b, mem_b = groups[gi + 1]
            tlo, thi = min(t_a, t_b), max(t_a, t_b)

            def src_lo(relax):
                v = prev + margin
                for mi in mem_a:
                    m = nodes[mi]
                    if m["quer"]:
                        if relax:
                            v = max(v, max(prev, m["s0"]) + cov_req(m))
                        else:
                            v = max(v, m["s1"] + margin)
                    else:
                        v = max(v, m["se1"] + margin)
                return v

            def dst_hi(relax):
                v = smax
                for mi in mem_b:
                    m = nodes[mi]
                    if m["quer"]:
                        if relax:
                            v = min(v, m["s1"] - cov_req(m))
                        else:
                            v = min(v, m["s0"] - margin)
                    else:
                        v = min(v, m["se0"] - margin)
                return v

            mkey = (tuple(mem_a), tuple(mem_b), round(prev, 3))
            s = build_poly._memo.get(mkey)
            if s is not None:
                pts.append((s, t_a))
                pts.append((s, t_b))
                prev = s
                continue
            for rl_src, rl_dst in ((False, False), (True, False),
                                   (False, True), (True, True)):
                lo = max(src_lo(rl_src), prev + margin)
                hi = dst_hi(rl_dst)
                if hi < lo:
                    continue
                src_ch = any(nodes[mi]["quer"] and nodes[mi]["s1"] > hi + 1e-9
                             for mi in mem_a)
                dst_ch = any(nodes[mi]["quer"] and nodes[mi]["s0"] < lo - 1e-9
                             for mi in mem_b)
                if src_ch and not dst_ch:
                    prefer = hi
                elif dst_ch and not src_ch:
                    prefer = lo
                else:
                    prefer = 0.5*(lo+hi)
                s = _free_jog(lo, hi, tlo, thi, prefer)
                if s is not None:
                    break
            if s is not None:
                build_poly._memo[mkey] = s
            else:
                s = jogs.get((mem_a[-1], mem_b[0]))
                if s is None or s <= prev:
                    s = max(prev + margin,
                            0.5*(nodes[mem_a[-1]]["se1"]
                                 + nodes[mem_b[0]]["se0"]))
            pts.append((s, t_a))
            pts.append((s, t_b))
            prev = s
        pts.append((smax + lead, groups[-1][0]))
        return pts

    def validate(pts):
        import numpy as _np
        hits, viol = [], []
        P = _np.asarray(pts)
        if len(P) < 2:
            return [], []
        # Monotonie
        bad_mono = _np.nonzero(P[1:, 0] < P[:-1, 0] - 1e-9)[0]
        for k in bad_mono:
            viol.append((None, int(k)))
        A_creq = _np.minimum((A_s1 - A_s0) - 0.5, 6.0 * A_r)
        d_t = _np.abs(P[1:, 1] - P[:-1, 1])
        is_level = d_t < 1e-9
        seg_idx = _np.arange(len(P) - 1)

        # ---- Hauptsegmente (Ebenen) als (K x n)-Matrix ----
        if is_level.any():
            ks = seg_idx[is_level]
            lo = _np.minimum(P[ks, 0], P[ks+1, 0])[:, None]
            hi = _np.maximum(P[ks, 0], P[ks+1, 0])[:, None]
            t0 = P[ks, 1][:, None]
            d = _np.abs(A_t[None, :] - t0)
            on_lvl = d <= cluster_tol
            ov = _np.minimum(hi, A_s1[None, :]) - _np.maximum(lo, A_s0[None, :])
            q_hit = on_lvl & A_q[None, :] & (ov >= A_creq[None, :] - 1e-6)
            q_bad = (on_lvl & A_q[None, :] & (ov > 1e-6)
                     & (ov < A_creq[None, :] - 1e-6))
            a_full = (on_lvl & ~A_q[None, :]
                      & (lo - 1e-6 <= A_s0[None, :])
                      & (A_s1[None, :] <= hi + 1e-6))
            a_bad = (on_lvl & ~A_q[None, :] & ~a_full
                     & (A_se1[None, :] > lo) & (A_se0[None, :] < hi))
            graze = (~on_lvl & (d < A_r[None, :] - 1e-6)
                     & (A_se1[None, :] > lo) & (A_se0[None, :] < hi))
            kk, mm = _np.nonzero(q_hit | a_full)
            order = _np.argsort(kk, kind="stable")
            for ki, mi in zip(kk[order], mm[order]):
                hits.append((int(mi), int(ks[ki])))
            kk, mm = _np.nonzero(q_bad | a_bad | graze)
            for ki, mi in zip(kk, mm):
                viol.append((int(mi), int(ks[ki])))
            if len(O0):
                ob = ((O2[None, :] < t0) & (t0 < O3[None, :])
                      & (O0[None, :] < hi) & (O1[None, :] > lo))
                for ki in _np.nonzero(ob.any(axis=1))[0]:
                    viol.append((-1, int(ks[ki])))

        # ---- Sprungsegmente als (K x n)-Matrix ----
        if (~is_level).any():
            ks = seg_idx[~is_level]
            s0 = P[ks, 0][:, None]
            tlo = _np.minimum(P[ks, 1], P[ks+1, 1])[:, None]
            thi = _np.maximum(P[ks, 1], P[ks+1, 1])[:, None]
            skip = A_q[None, :] & ((_np.abs(A_t[None, :] - tlo) <= cluster_tol)
                                   | (_np.abs(A_t[None, :] - thi) <= cluster_tol))
            bad = (~skip & (A_se0[None, :] < s0) & (s0 < A_se1[None, :])
                   & (tlo < A_t[None, :] + A_r[None, :])
                   & (thi > A_t[None, :] - A_r[None, :]))
            kk, mm = _np.nonzero(bad)
            for ki, mi in zip(kk, mm):
                viol.append((int(mi), int(ks[ki])))
            if len(O0):
                ob = ((O0[None, :] < s0) & (s0 < O1[None, :])
                      & (tlo < O3[None, :]) & (thi > O2[None, :]))
                for ki in _np.nonzero(ob.any(axis=1))[0]:
                    viol.append((-1, int(ks[ki])))

        seen_once = set()
        clean = []
        for mi, k in hits:
            if mi in seen_once:
                viol.append((mi, k))
            else:
                seen_once.add(mi)
                clean.append((mi, k))
        # Teilschnitt-Regel: aussermittiges Anschneiden einer Bohrung ist
        # TOLERIERT, wenn (a) ihr Typ an anderer Stelle komplett geschnitten
        # wird und (b) nicht dieselbe Instanz zusaetzlich voll geschnitten
        # ist (sonst Doppelschnitt). Monotonie-, Sperrflaechen- und
        # Doppel-Verstoesse bleiben hart.
        hit_inst = set(mi for mi, _ in clean)
        hit_types = set(nodes[mi]["f"].type_no for mi in hit_inst)
        hard, soft = [], []
        for mi, k in viol:
            if (mi is not None and mi >= 0
                    and mi not in hit_inst
                    and (nodes[mi]["skip"]
                         or nodes[mi]["f"].type_no in hit_types)):
                soft.append((mi, k))
            else:
                hard.append((mi, k))
        return clean, hard, soft

    def sweep_seed():
        """Startkette mit minimalen Leerwegen: links -> rechts, Ebene wird
        nur gewechselt, wenn dort ein NEUER Typ wartet."""
        chain, jogs, seen_t = [], {}, set()
        cur = None
        for i in sorted(range(n), key=lambda k: nodes[k]["s0"]):
            if nodes[i]["skip"]:
                continue
            tno = nodes[i]["f"].type_no
            if cur is None:
                chain.append(i)
                cur = i
                seen_t.add(tno)
                continue
            if abs(nodes[i]["t"] - nodes[cur]["t"]) <= cluster_tol:
                if nodes[i]["s0"] >= nodes[cur]["s0"] - 1e-9:
                    chain.append(i)
                    cur = i
                    seen_t.add(tno)
                continue
            if tno in seen_t and not nodes[i]["forced"]:
                continue
            s = jog_pos(cur, i)
            if s is None:
                continue
            jogs[(cur, i)] = s
            chain.append(i)
            cur = i
            seen_t.add(tno)
        return chain, jogs

    def choose_best(forbidden, forbid_start, forbid_end, topk=8):
        """EIN DP-Lauf; Ketten fuer die besten K Endknoten werden aus den
        Vorgaenger-Zeigern rekonstruiert; zusaetzlich eine Sweep-Startkette
        mit minimalen Leerwegen. Der Score entscheidet."""
        best, par, jogs = run_dp(forbidden, forbid_start, forbid_end)
        cand_ends = [k for k in sorted(range(n), key=lambda k: best[k],
                                       reverse=True)
                     if k not in forbid_end][:max(topk, 1)]
        result = None
        cands = []
        for end_i in cand_ends:
            c = reconstruct(end_i, par)
            if c[0] in forbid_start:
                continue
            cands.append((c, jogs))
        if not forbidden and not forbid_start and not forbid_end:
            sc_, sj_ = sweep_seed()
            if sc_:
                cands.append((sc_, sj_))
        for c, j0 in cands:
            c, j, p, score = prune_chain(c, j0)
            if result is None or score > result[0]:
                result = (score, c, j, p)
        if result is None:
            c = reconstruct(max(range(n), key=lambda k: best[k]), par)
            c, j, p, _ = prune_chain(c, jogs)
            return c, j, p
        return result[1], result[2], result[3]

    def score_of(hits, viol, soft=(), pts=None):
        """Rangfolge (lexikografisch): keine harten Verstoesse > ERZWUNGENE
        Typen > Typenzahl (ohne 'nicht noetig') > wenige Zusatz-Segmente je
        Typ > wenige tolerierte Teilschnitte > Axialtreffer > Gesamttreffer >
        WEGE-OEKONOMIE (wenig Sprunghoehe, kurze Gesamtlinie): bei gleicher
        Ausbeute gewinnt der kompakteste Verlauf ohne Leerwege."""
        segs_per_type = {}
        n_forced = set()
        for mi, k in hits:
            nd = nodes[mi]
            if nd["forced"]:
                n_forced.add(nd["f"].type_no)
            if not nd["skip"]:
                segs_per_type.setdefault(nd["f"].type_no, set()).add(k)
        n_types = len(segs_per_type)
        extra = sum(len(s) - 1 for s in segs_per_type.values())
        uniq_ = [mi for mi, _ in hits]
        t_travel = length = 0.0
        if pts:
            for (a0, a1), (b0, b1) in zip(pts, pts[1:]):
                length += abs(b0 - a0) + abs(b1 - a1)
                t_travel += abs(b1 - a1)
        return (-len(viol), len(n_forced), n_types, -extra,
                -len(set(mi for mi, _ in soft)),
                sum(1 for m in uniq_ if not nodes[m]["quer"]), len(uniq_),
                -round(t_travel, 1), -round(length, 1))

    def _try_insert(c, j, p, sc, h):
        """Fuer noch NICHT geschnittene Typen gezielt einen Umweg einbauen,
        wenn das die Typenzahl erhoeht (Gegenspieler zum Pruning)."""
        covered = set(nodes[mi]["f"].type_no for mi, _ in h)
        cand = sorted((i for i in range(n)
                       if nodes[i]["f"].type_no not in covered
                       and not nodes[i]["skip"]),
                      key=lambda k: (not nodes[k]["forced"], nodes[k]["s0"]))
        attempts = 0
        changed = False
        chain_set = set(c)
        for i in cand:
            tno = nodes[i]["f"].type_no
            if tno in covered or i in chain_set or attempts >= 200:
                continue
            # Einfuegeposition nach s-Reihenfolge
            pos = 0
            while pos < len(c) and nodes[c[pos]]["s0"] <= nodes[i]["s0"]:
                pos += 1
            attempts += 1
            import hole_detection as _hd_self
            # Einfuegen mit GEOMETRISCHER Verdraengung: alle Kettenknoten
            # raeumen, deren Bereich das noetige Sprungfenster der neuen
            # Bohrung ueberlappt (skaliert mit dem Durchmesser; Knoten auf
            # derselben Ebene brauchen keinen Sprung und bleiben).
            accepted = False
            need_l = nodes[i]["se0"] - 2*margin
            need_r = nodes[i]["se1"] + 2*margin
            lo_geo = pos
            while (lo_geo > 0
                   and abs(nodes[c[lo_geo-1]]["t"] - nodes[i]["t"]) > cluster_tol
                   and nodes[c[lo_geo-1]]["se1"] > need_l):
                lo_geo -= 1
            hi_geo = pos
            while (hi_geo < len(c)
                   and abs(nodes[c[hi_geo]]["t"] - nodes[i]["t"]) > cluster_tol
                   and nodes[c[hi_geo]]["se0"] < need_r):
                hi_geo += 1
            variants = [(pos, pos), (lo_geo, hi_geo),
                        (max(0, lo_geo-1), hi_geo),
                        (lo_geo, min(len(c), hi_geo+1)),
                        (max(0, lo_geo-1), min(len(c), hi_geo+1))]
            seen_var = set()
            reasons = []
            for lo_cut, hi_cut in variants:
                if (lo_cut, hi_cut) in seen_var:
                    continue
                seen_var.add((lo_cut, hi_cut))
                if lo_cut < 0 or hi_cut > len(c):
                    continue
                nj = dict(j)
                ok = True
                pairs = []
                if lo_cut > 0:
                    pairs.append((c[lo_cut-1], i))
                if hi_cut < len(c):
                    pairs.append((i, c[hi_cut]))
                for a, b in pairs:
                    if abs(nodes[a]["t"] - nodes[b]["t"]) <= cluster_tol:
                        if nodes[b]["s0"] < nodes[a]["s0"] - 1e-9:
                            ok = False
                            break
                    else:
                        s = jog_pos(a, b, relaxed=True)
                        if s is None:
                            ok = False
                            break
                        nj[(a, b)] = s
                if not ok:
                    reasons.append("Sprung blockiert")
                    continue
                nc = c[:lo_cut] + [i] + c[hi_cut:]
                pp = build_poly(nc, nj)
                hh, vv, ss_ = validate(pp)
                nsc = score_of(hh, vv, ss_, pp)
                got_target = any(nodes[mi]["f"].type_no == tno
                                 for mi, _ in hh)
                # Annahme, wenn besser ODER typneutraler Tausch, der den
                # Zieltyp hereinholt (verdraengter Typ wird in einer
                # Folgerunde an anderer Instanz nachgeholt)
                lateral = False  # Tauschzuege: an realen Teilen ohne
                # Gewinn, aber teuer — deaktiviert (Doppelzug waere noetig)
                if nsc <= sc and not lateral:
                    dv = len(vv) - (-sc[0])
                    reasons.append(f"Score(dViol={dv},dTyp={nsc[1]-sc[1]})")
                if nsc > sc or lateral:
                    c, j, p, sc, h = nc, nj, pp, nsc, hh
                    covered = set(nodes[mi]["f"].type_no for mi, _ in hh)
                    chain_set = set(c)
                    changed = True
                    accepted = True
                    break
            if not accepted and getattr(_hd_self, "INSERT_DEBUG", None) is not None:
                _hd_self.INSERT_DEBUG.append(
                    (nodes[i]["f"].type_no, nodes[i]["f"].label,
                     round(nodes[i]["s0"]), round(nodes[i]["t"]),
                     "|".join(sorted(set(reasons))) or "keine Variante"))
        return c, j, p, sc, h, changed

    def _try_swap(c, j, p, sc, h):
        """Kettenknoten durch NAEHER liegende Instanzen desselben Typs
        ersetzen — reduziert Leerwege/Sprunghoehen bei gleicher Ausbeute
        (Wege-Oekonomie entscheidet im Score)."""
        by_type = {}
        for i2 in range(n):
            if not nodes[i2]["skip"]:
                by_type.setdefault(nodes[i2]["f"].type_no, []).append(i2)
        in_chain = set(c)
        attempts = 0
        changed = False
        pos = -1
        while pos + 1 < len(c):
            pos += 1
            if attempts >= 120:
                break
            i = c[pos]
            tno = nodes[i]["f"].type_no
            alts = [a for a in by_type.get(tno, []) if a not in in_chain]
            if not alts:
                continue
            t_nb = [nodes[c[q]]["t"] for q in (pos-1, pos+1)
                    if 0 <= q < len(c)]
            ref_t = sum(t_nb)/len(t_nb) if t_nb else nodes[i]["t"]
            alts.sort(key=lambda a: abs(nodes[a]["t"] - ref_t))
            for a in alts[:4]:
                attempts += 1
                rest = [x for x in c if x != i]
                ip = 0
                while ip < len(rest) and nodes[rest[ip]]["s0"] <= nodes[a]["s0"]:
                    ip += 1
                nc = rest[:ip] + [a] + rest[ip:]
                nj = dict(j)
                ok = True
                touched = {max(0, ip-1), ip, min(len(nc)-1, ip+1)}
                for q in sorted(touched):
                    if q + 1 >= len(nc):
                        continue
                    x, y = nc[q], nc[q+1]
                    if abs(nodes[x]["t"] - nodes[y]["t"]) <= cluster_tol:
                        if nodes[y]["s0"] < nodes[x]["s0"] - 1e-9:
                            ok = False
                            break
                    else:
                        s = jog_pos(x, y)
                        if s is None:
                            ok = False
                            break
                        nj[(x, y)] = s
                if not ok:
                    continue
                pp = build_poly(nc, nj)
                hh, vv, ss_ = validate(pp)
                nsc = score_of(hh, vv, ss_, pp)
                if nsc > sc:
                    c, j, p, sc, h = nc, nj, pp, nsc, hh
                    in_chain = set(c)
                    changed = True
                    break
        return c, j, p, sc, h, changed

    def prune_chain(c, j):
        """Kette verbessern: Knoten mit doppelt geschnittenem Typ entfernen
        UND fuer fehlende Typen Umwege einfuegen (inkl. typneutraler
        Tauschzuege) — bester Zwischenstand gewinnt."""
        p = build_poly(c, j)
        h, v, so = validate(p)
        sc = score_of(h, v, so, p)
        best = (sc, c, j, p)
        for _round in range(6):
            c, j, p, sc, h, ins = _try_insert(c, j, p, sc, h)
            c, j, p, sc, h, swp = _try_swap(c, j, p, sc, h)
            c, j, p, sc2, h2 = _prune_only(c, j, p, sc, h)
            if sc2 > best[0]:
                best = (sc2, c, j, p)
            if not ins and not swp and sc2 == sc:
                sc, h = sc2, h2
                break
            sc, h = sc2, h2
        if best[0] > sc:
            sc, c, j, p = best
        return c, j, p, sc

    def _prune_only(c, j, p, sc, h):
        v = []
        attempts = 0
        improved = True
        while improved and len(c) > 1 and attempts < 150:
            improved = False
            hitset = set(mi for mi, _ in h)
            tcount = {}
            for m in hitset:
                tno = nodes[m]["f"].type_no
                tcount[tno] = tcount.get(tno, 0) + 1
            cand_pos = [pos for pos, idx in enumerate(c)
                        if idx not in hitset
                        or tcount.get(nodes[idx]["f"].type_no, 0) > 1]
            # zuerst: ganze Ebenen-Gruppen, deren Typen anderweitig
            # abgedeckt sind, komplett entfernen (verhindert absichtliches
            # Anfahren bereits geschnittener Bohrnummern)
            groups = []
            for pos, idx in enumerate(c):
                if groups and abs(nodes[idx]["t"]
                                  - nodes[c[groups[-1][0]]]["t"]) <= cluster_tol:
                    groups[-1].append(pos)
                else:
                    groups.append([pos])
            if len(groups) > 1:
                seg_of = {}
                for mi, k in h:
                    seg_of.setdefault(mi, k)
                for gpos in groups:
                    gset = set(c[p] for p in gpos)
                    g_types = set(nodes[i]["f"].type_no for i in gset)
                    other_types = set(nodes[mi]["f"].type_no
                                      for mi, _ in h if mi not in gset)
                    if not (g_types <= other_types):
                        continue
                    attempts += 1
                    nc = [x for p, x in enumerate(c) if p not in gpos]
                    if not nc:
                        continue
                    nj = dict(j)
                    lo_p, hi_p = gpos[0], gpos[-1]
                    if 0 < lo_p and hi_p < len(c) - 1:
                        a, b = c[lo_p-1], c[hi_p+1]
                        if abs(nodes[a]["t"] - nodes[b]["t"]) <= cluster_tol:
                            if nodes[b]["s0"] < nodes[a]["s0"] - 1e-9:
                                continue
                        else:
                            s = jog_pos(a, b)
                            if s is None:
                                continue
                            nj[(a, b)] = s
                    pp = build_poly(nc, nj)
                    hh, vv, ss_ = validate(pp)
                    nsc = score_of(hh, vv, ss_, pp)
                    if nsc > sc or (nsc == sc and len(nc) < len(c)):
                        c, j, p, sc, h, v = nc, nj, pp, nsc, hh, vv
                        improved = True
                        break
                if improved:
                    continue
            for pos in cand_pos:
                attempts += 1
                nc = c[:pos] + c[pos+1:]
                nj = dict(j)
                if 0 < pos < len(c) - 1:
                    a, b = c[pos-1], c[pos+1]
                    if abs(nodes[a]["t"] - nodes[b]["t"]) <= cluster_tol:
                        if nodes[b]["s0"] < nodes[a]["s0"] - 1e-9:
                            continue
                    else:
                        s = jog_pos(a, b)
                        if s is None:
                            continue
                        nj[(a, b)] = s
                pp = build_poly(nc, nj)
                hh, vv, ss_ = validate(pp)
                nsc = score_of(hh, vv, ss_, pp)
                if nsc > sc or (nsc == sc and len(nc) < len(c)):
                    c, j, p, sc, h, v = nc, nj, pp, nsc, hh, vv
                    improved = True
                    break
                if attempts >= 150:
                    break
        return c, j, p, sc, h

    forbidden, forbid_start, forbid_end = set(), set(), set()
    chain, jogs, pts = choose_best(forbidden, forbid_start, forbid_end)
    for _ in range(30):
        hits, viol, _soft = validate(pts)
        if not viol:
            break
        _, segk = viol[0]
        level_idx = (segk + 1) // 2
        transitions = [(a, b) for a, b in zip(chain, chain[1:])
                       if abs(nodes[a]["t"] - nodes[b]["t"]) > cluster_tol]
        if level_idx == 0:
            forbid_start.add(chain[0])
        elif level_idx - 1 < len(transitions):
            forbidden.add(transitions[level_idx - 1])
        else:
            forbid_end.add(chain[-1])
        chain, jogs, pts = choose_best(forbidden, forbid_start, forbid_end)
    hits, viol, soft = validate(pts)
    final_score = score_of(hits, viol, soft, pts)
    soft_summary = {}
    for mi, _k in set(soft):
        tno = nodes[mi]["f"].type_no
        soft_summary[tno] = soft_summary.get(tno, 0) + 1

    seen, uniq = set(), []
    for m, _k in hits:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    uniq.sort(key=lambda m: nodes[m]["s0"])
    if _mirror:
        pts = [(-s, t) for (s, t) in pts][::-1]
        uniq = uniq[::-1]
    hit_feats = [nodes[m]["f"] for m in uniq]
    n_qhit = sum(1 for m in uniq if nodes[m]["quer"])

    poly = pts if horiz else [(t, s) for (s, t) in pts]
    dname = ("horizontal (links -> rechts)" if horiz
             else "vertikal (unten -> oben)")
    txt = [f"Stufenschnitt A-A, Richtung: {dname}",
           "Verlauf (u=Bild-x, v=Bild-y), monoton, Knicke ausserhalb der "
           "Bohrungen:"]
    for i, (u, v) in enumerate(poly):
        txt.append(f"  P{i+1}: ({u:.1f}, {v:.1f})")
    def _seg_cover(m):
        cov = 0.0
        for k in range(len(pts)-1):
            (a0, b0), (a1, b1) = pts[k], pts[k+1]
            if abs(b0-b1) < 1e-9 and abs(nodes[m]["t"] - b0) <= cluster_tol:
                cov += max(0.0, min(max(a0, a1), nodes[m]["s1"])
                           - max(min(a0, a1), nodes[m]["s0"]))
        return cov

    def _tag(m):
        if not nodes[m]["quer"]:
            return f"#{nodes[m]['f'].type_no}"
        full = _seg_cover(m) >= (nodes[m]["s1"] - nodes[m]["s0"]) - 0.1
        return (f"#{nodes[m]['f'].type_no} (quer, laengs"
                + ("" if full else " teilw.") + ")")
    txt.append("Geschnittene Bohrungen (voller Durchmesser bzw. volle "
               "Laenge): " + ", ".join(_tag(m) for m in uniq)
               + f"  ({len(uniq)} von {n_ax} axialen + {n_qp} "
                 f"schnittparallelen Querbohrungen, davon {n_qhit} quer; "
                 f"keine doppelt)")
    txt.append("Senkrecht zur Schnittrichtung liegende Querbohrungen werden "
               "nicht einbezogen und nicht gekreuzt; je Bohrungstyp genuegt "
               "EIN Schnitt.")
    if viol:
        txt.append("WARNUNG: Konflikt nicht vollstaendig aufloesbar.")
    return poly, hit_feats, "\n".join(txt), final_score, soft_summary


# ----------------------------------------------------------------------------
# Manueller Zweitschnitt (Schnitt B-B): Versatzschnitt durch eine vom
# Anwender per Klick gewaehlte Menge von Bohrungen, in der Reihenfolge ihrer
# Lage entlang der Schnittrichtung. Im Gegensatz zum automatischen
# Stufenschnitt wird hier keine Optimalitaet gesucht, sondern nur versucht,
# die Knicke (Spruenge zwischen den Ebenen) so zu legen, dass sie andere,
# NICHT ausgewaehlte axiale Bohrungen nach Moeglichkeit nicht kreuzen.
# ----------------------------------------------------------------------------
def plan_manual_section(selected, all_features, view="top",
                        direction="horizontal", rotation=0,
                        margin=2.0, cluster_tol=1.0):
    """Versatzschnitt B-B durch die angeklickten Bohrungen (axial ODER quer).

    selected      : Liste von Feature-Objekten in Klickreihenfolge (die
                     tatsaechliche Ausgabereihenfolge richtet sich nach der
                     Lage entlang der Schnittrichtung, nicht nach der
                     Klickreihenfolge).
    all_features  : alle Features der aktuellen Ansicht (fuer die
                     Kollisionspruefung der Sprungsegmente).
    Axialbohrungen liefern einen Punktknoten. Querbohrungen, deren Achse
    PARALLEL zur Schnittrichtung liegt, liefern einen Intervallknoten (volle
    Laenge, wie beim automatischen Stufenschnitt); alle anderen Quer-/
    Schraegbohrungen liefern einen Punktknoten an ihrem Bildmittelpunkt (der
    Versatz wird so gelegt, dass er dort hindurchlaeuft).
    Rueckgabe: (poly, hits) — poly = Liste (u,v)-Punkte, hits = geschnittene
    Features in Schnittreihenfolge. ([], []) wenn keine schnittbare Bohrung
    ausgewaehlt ist.
    """
    horiz = (direction == "horizontal")

    def to_st(p):
        return (p[0], p[1]) if horiz else (p[1], p[0])

    def node_of(f):
        """(s0, s1, t, r) — Axialbohrungen und quer zur Schnittrichtung
        liegende Querbohrungen ergeben einen Punktknoten (s0==s1); zur
        Schnittrichtung PARALLELE Querbohrungen ergeben ein Intervall."""
        r = f.diameter / 2.0
        if f.orientation == "axial":
            s, t = to_st(f.uv)
            return s, s, t, r
        a, b = feature_uv_extent(f, view, rotation)
        (sa, ta), (sb, tb) = to_st(a), to_st(b)
        if f.orientation == "quer" and abs(ta - tb) <= cluster_tol:
            return min(sa, sb), max(sa, sb), 0.5 * (ta + tb), r
        s, t = to_st(f.uv)
        return s, s, t, r

    pts = []
    for f in selected:
        if f.orientation not in ("axial", "quer", "schraeg"):
            continue
        s0, s1, t, r = node_of(f)
        pts.append((s0, s1, t, f))
    if not pts:
        return [], []
    pts.sort(key=lambda x: x[0])

    # in Ebenen-Gruppen (annaehernd gleiches t) zusammenfassen
    groups = []
    for s0, s1, t, f in pts:
        if groups and abs(t - groups[-1][0]) <= cluster_tol:
            groups[-1][1].append((s0, s1, f))
        else:
            groups.append((t, [(s0, s1, f)]))

    sel_ids = set(id(f) for f in selected)
    blockers = []
    for f in all_features:
        if id(f) in sel_ids or f.orientation not in ("axial", "quer", "schraeg"):
            continue
        s0, s1, t, r = node_of(f)
        blockers.append((s0, s1, t, r))

    def jog_free(s, tlo, thi):
        for bs0, bs1, bt, br in blockers:
            if (tlo - br - margin * 0.4 < bt < thi + br + margin * 0.4
                    and bs0 - br - margin * 0.4 < s < bs1 + br + margin * 0.4):
                return False
        return True

    def find_jog(lo, hi, tlo, thi, prefer):
        if hi < lo:
            lo, hi = hi, lo
        cand = [prefer, lo, hi, 0.5 * (lo + hi)]
        if hi > lo:
            step = max((hi - lo) / 10.0, 0.5)
            c = lo
            while c <= hi:
                cand.append(c)
                c += step
        for c in cand:
            c = min(max(c, lo), hi)
            if jog_free(c, tlo, thi):
                return c
        return prefer   # kein freier Punkt gefunden -> bestmoegliche Naeherung

    lead = 5.0
    all_lo = [s0 for _, mem in groups for s0, _, _ in mem]
    all_hi = [s1 for _, mem in groups for _, s1, _ in mem]
    smin_all, smax_all = min(all_lo) - lead, max(all_hi) + lead

    poly = [(smin_all, groups[0][0])]
    prev_s = smin_all
    for gi in range(len(groups) - 1):
        t_a, mem_a = groups[gi]
        t_b, mem_b = groups[gi + 1]
        s_a_max = max(s1 for _, s1, _ in mem_a)
        s_b_min = min(s0 for s0, _, _ in mem_b)
        lo = max(s_a_max + margin, prev_s + margin)
        hi = max(s_b_min - margin, lo)
        prefer = 0.5 * (lo + hi)
        s_jog = find_jog(lo, hi, min(t_a, t_b), max(t_a, t_b), prefer)
        poly.append((s_jog, t_a))
        poly.append((s_jog, t_b))
        prev_s = s_jog
    poly.append((smax_all, groups[-1][0]))

    ordered = [f for _, mem in groups for _, _, f in mem]
    poly_uv = poly if horiz else [(t, s) for (s, t) in poly]
    return poly_uv, ordered


# ----------------------------------------------------------------------------
# Komplettpipeline — getrennt in schwere Geometriephase (einmal je Datei /
# Gewinde-Einstellung, wird gecacht) und leichte Ansichtsphase (bei jedem
# Ansichts-/Richtungswechsel).
# ----------------------------------------------------------------------------
def bounds_uv(shape, view, rotation=0):
    """Bounding Box des Bauteils in Bildkoordinaten (u0, v0, u1, v1)."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    ud, wd = effective_uv(view, rotation)
    us, vs = [], []
    for x in (x0, x1):
        for y in (y0, y1):
            for z in (z0, z1):
                us.append(_dot((x, y, z), ud))
                vs.append(_dot((x, y, z), wd))
    return min(us), min(vs), max(us), max(vs)


def analyze_geometry(shape_or_path, thread_map=None,
                     thread_requires_chamfer=True):
    """SCHWERE Phase: STEP -> Flaechen -> fusionierte Bohrwaende -> Features.
    Ansichts-UNABHAENGIG; Ergebnis fuer alle Ansichten wiederverwendbar."""
    shape = (load_step(shape_or_path) if isinstance(shape_or_path, str)
             else shape_or_path)
    tester = SolidTester(shape)
    cyls, cones = collect_segments(shape)
    holes = merge_cylinders(cyls, tester)
    holes = filter_fillets(holes)
    feats = build_features(shape, holes, cones, thread_map,
                           thread_requires_chamfer, tester=tester)
    feats = assign_type_numbers(feats)
    return {
        "shape": shape,
        "features": feats,
        "raw_cyl_segments": len(cyls),
        "raw_cone_segments": len(cones),
    }


def analyze_view(geo, view="top", direction="horizontal", rotation=0,
                 force_keys=frozenset(), skip_keys=frozenset(),
                 manual_selection=(), manual_direction="horizontal",
                 renumber_source="A", section_cache=None):
    """LEICHTE Phase: Orientierung/Projektion (inkl. Bilddrehung),
    Stufenschnitt, manueller Zweitschnitt B-B, Nummerierung entlang des
    gewaehlten Schnitts (renumber_source 'A' oder 'B'), Text, Aussenkontur.
    manual_selection: Feature-Objekte aus geo['features'] fuer Schnitt B.
    section_cache: optionales, vom Aufrufer gehaltenes dict — der teure
    Stufenschnitt A-A (doppelte Planung mit/ohne Spiegelung) wird darin
    zwischengespeichert und bei unveraenderten Eingaben (Ansicht, Richtung,
    Drehung, Erzwingen-/Nicht-noetig-Markierungen, Bohrungsmenge)
    wiederverwendet — wichtig, damit Schnitt-B-Aenderungen (Klick fuer
    manuelle Bohrungsauswahl) nicht jedes Mal die komplette, aufwendige
    Stufenschnitt-Optimierung neu durchlaufen."""
    feats = assign_type_numbers(geo["features"])
    feats = classify_view(feats, view, rotation)
    bnds = bounds_uv(geo["shape"], view, rotation)

    cache_key = (view, direction, int(rotation) % 360,
                frozenset(force_keys), frozenset(skip_keys),
                tuple(id(f) for f in feats))
    cached = section_cache.get(cache_key) if section_cache is not None \
        else None
    if cached is not None:
        poly, hits, raw_txt, soft_sum = cached
    else:
        best = None
        for mir in (False, True):
            poly_, hits_, raw_, sc_, soft_ = plan_step_section(
                feats, view, direction, bounds=bnds, rotation=rotation,
                _mirror=mir, force_keys=force_keys, skip_keys=skip_keys)
            if best is None or sc_ > best[3]:
                best = (poly_, hits_, raw_, sc_, soft_)
        poly, hits, raw_txt, _, soft_sum = best
        if section_cache is not None:
            section_cache.clear()   # nur die aktuelle Ansicht vorhalten
            section_cache[cache_key] = (poly, hits, raw_txt, soft_sum)

    poly2, hits2 = plan_manual_section(
        list(manual_selection), feats, view=view,
        direction=manual_direction, rotation=rotation) \
        if manual_selection else ([], [])

    mapping = renumber_by_section(feats, hits2 if renumber_source == "B"
                                  else hits)
    soft_sum = {mapping.get(t, t): n for t, n in soft_sum.items()}
    section_txt = build_section_text(feats, poly, hits, view, direction,
                                     warn=("WARNUNG" in raw_txt),
                                     rotation=rotation, soft=soft_sum,
                                     force_keys=force_keys,
                                     skip_keys=skip_keys)
    if "outline_cache" not in geo:
        geo["outline_cache"] = {}
    ck = (view, int(rotation) % 360)
    if ck not in geo["outline_cache"]:
        geo["outline_cache"][ck] = outline_curves(geo["shape"], view,
                                                  rotation)
    res = dict(geo)
    res.update({
        "view": view,
        "direction": direction,
        "rotation": int(rotation) % 360,
        "bounds": bnds,
        "outline": geo["outline_cache"][ck],
        "section_poly": poly,
        "section_hits": hits,
        "section_text": section_txt,
        "section2_poly": poly2,
        "section2_hits": hits2,
        "renumber_source": renumber_source,
    })
    return res


def analyze(shape_or_path, view="top", thread_map=None,
            thread_requires_chamfer=True, direction="horizontal"):
    """Komfortfunktion: beide Phasen in einem Aufruf."""
    geo = analyze_geometry(shape_or_path, thread_map, thread_requires_chamfer)
    return analyze_view(geo, view, direction)


def table_rows(features):
    """Bohrtabelle: je Typ eine Zeile mit Anzahl. Gewindetiefe ist nur bei
    Gewinden gesetzt (sonst None) — die tatsaechlich tragende Gewindelaenge,
    die von der Bohrtiefe abweichen kann (Teilgewinde). Durchgangsbohrungen
    (inkl. Durchgangs-/Teilgewinde) haben KEINE Tiefenangabe — sie gelten
    unabhaengig von der Materialdicke als dieselbe Bohrung; bei einem
    durchgehenden Gewinde ist auch die Gewindetiefe None."""
    by_type = {}
    for f in features:
        by_type.setdefault(f.type_no, []).append(f)
    rows = []
    for tno in sorted(by_type):
        fs = by_type[tno]
        f = fs[0]
        art = ("Durchgang" if f.through else "Sackloch")
        thread_depth = None
        if f.kind == "Gewinde":
            tl = getattr(f, "thread_len", None)
            thread_depth = round(tl, 2) if tl is not None else None
            if not f.through:
                art = "Gewinde, Sackloch"
            elif tl is None:
                art = "Durchgangsgewinde"      # Gewinde ueber die ganze Bohrung
            else:
                art = "Teilgewinde, Durchgang"  # Gewinde nur teilweise, Rest Kernloch
        elif f.kind == "Senkbohrung":
            art = f"Senkung D{_fmt(f.counterbore[0])}x{_fmt(f.counterbore[1])}, {art}"
        elif f.kind == "Kegelsenkung":
            art = f"Kegelsenkung, {art}"
        rows.append({
            "Nr": tno, "Bezeichnung": f.label,
            "Durchmesser": round(f.diameter, 2),
            "Art": art,
            "Tiefe": None if f.through else round(f.depth, 2),
            "Gewindetiefe": thread_depth,
            "Anzahl": len(fs),
        })
    return rows


# ----------------------------------------------------------------------------
# Nummerierung entlang des Schnitts + Textaufbau + Aussenkontur
# ----------------------------------------------------------------------------
def renumber_by_section(features, hits):
    """Bohrnummern: Typen, die im Schnitt liegen, bekommen 1..k in der
    Reihenfolge, in der der Schnitt sie trifft (links beginnend); alle
    uebrigen Typen zaehlen danach weiter (Gewinde zuerst, dann Durchmesser)."""
    order = []
    for f in hits:
        if f.type_no not in order:
            order.append(f.type_no)
    rest = sorted(set(f.type_no for f in features) - set(order),
                  key=lambda t: next((0 if f.kind == "Gewinde" else 1,
                                      f.diameter)
                                     for f in features if f.type_no == t))
    mapping = {old: i+1 for i, old in enumerate(order + rest)}
    for f in features:
        f.type_no = mapping[f.type_no]
    return mapping


def build_section_text(features, poly, hits, view, direction, warn=False,
                       cluster_tol=1.0, rotation=0, soft=None,
                       force_keys=frozenset(), skip_keys=frozenset()):
    if not poly:
        return "Keine schnittbaren Bohrungen in dieser Ansicht."
    horiz = (direction == "horizontal")
    def to_st(p):
        return (p[0], p[1]) if horiz else (p[1], p[0])
    segs = []
    for a, b in zip(poly, poly[1:]):
        (sa, ta), (sb, tb) = to_st(a), to_st(b)
        if abs(ta - tb) < 1e-9:
            segs.append((min(sa, sb), max(sa, sb), ta))
    def cover(f):
        a, b = feature_uv_extent(f, view, rotation)
        (s0, t0), (s1, t1) = to_st(a), to_st(b)
        s0, s1 = min(s0, s1), max(s0, s1)
        cov = sum(max(0.0, min(hi, s1) - max(lo, s0))
                  for lo, hi, t in segs if abs(t - 0.5*(t0+t1)) <= cluster_tol)
        return cov, (s1 - s0)
    dname = ("horizontal (links -> rechts)" if horiz
             else "vertikal (unten -> oben)")
    txt = [f"Stufenschnitt A-A, Richtung: {dname}",
           "Verlauf (u=Bild-x, v=Bild-y), monoton, Knicke ausserhalb der "
           "Bohrungen:"]
    for i, (u, v) in enumerate(poly):
        txt.append(f"  P{i+1}: ({u:.1f}, {v:.1f})")
    tags = []
    for f in hits:
        if f.orientation == "quer":
            cov, ln = cover(f)
            tags.append(f"#{f.type_no} (quer, laengs"
                        + ("" if cov >= ln - 0.2 else " teilw.") + ")")
        else:
            tags.append(f"#{f.type_no}")
    n_ax = sum(1 for f in features if f.orientation == "axial")
    txt.append("Geschnittene Bohrungen in Schnittreihenfolge: "
               + ", ".join(tags) + f"  ({len(hits)} Bohrungen, "
               f"{len(set(f.type_no for f in hits))} Typen; je Typ genuegt "
               f"EIN Schnitt, Mehrfachtreffer nur auf derselben Geraden)")
    txt.append(f"Axialbohrungen in der Ansicht: {n_ax}; senkrechte "
               "Querbohrungen werden nicht einbezogen und nicht gekreuzt.")
    hit_t = set(f.type_no for f in hits)
    reach_t = set()
    for f in features:
        if f.orientation == "axial":
            reach_t.add(f.type_no)
        elif f.orientation == "quer":
            a, b = feature_uv_extent(f, view, rotation)
            (s0_, t0_), (s1_, t1_) = to_st(a), to_st(b)
            if abs(t0_ - t1_) <= 0.1:
                reach_t.add(f.type_no)
    if soft:
        txt.append("Tolerierte Teilschnitte (Typ anderweitig komplett "
                   "geschnitten): "
                   + ", ".join(f"Typ {t} ({n}x)" for t, n
                               in sorted(soft.items())))
    skip_t = set(f.type_no for f in features if type_key(f) in skip_keys)
    force_t = set(f.type_no for f in features if type_key(f) in force_keys)
    if force_t:
        ok_f = sorted(force_t & hit_t)
        bad_f = sorted(force_t - hit_t)
        if ok_f:
            txt.append("Erzwungen und im Schnitt: Typ "
                       + ", ".join(map(str, ok_f)))
        if bad_f:
            txt.append("ERZWUNGEN, ABER NICHT ERREICHBAR: Typ "
                       + ", ".join(map(str, bad_f)))
    if skip_t:
        txt.append("Als 'nicht noetig' markiert: Typ "
                   + ", ".join(map(str, sorted(skip_t))))
    miss = sorted(reach_t - hit_t - skip_t)
    if miss:
        txt.append("Nicht schneidbar ohne Regelverletzung (Umgebung zu "
                   "dicht / jede Zufahrt wuerde Nachbarbohrungen "
                   "aussermittig anschneiden): Typ "
                   + ", ".join(str(m) for m in miss))
    if warn:
        txt.append("WARNUNG: Konflikt nicht vollstaendig aufloesbar.")
    return "\n".join(txt)


def outline_curves(shape, view, rotation=0, max_pts=60000):
    """Echte Aussenkontur: Aussen-Wires aller ebenen Flaechen, deren Normale
    parallel zur Blickrichtung liegt, in Bildkoordinaten abgetastet.
    Faellt bei zu vielen Punkten auf None (Bounding Box) zurueck."""
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.BRepTools import BRepTools
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    vd = _norm(VIEW_DIRS[view])
    ud, wd = effective_uv(view, rotation)
    curves = []
    total = 0
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        ad = BRepAdaptor_Surface(face)
        if ad.GetType() == GeomAbs_Plane:
            nrm = ad.Plane().Axis().Direction()
            if abs(nrm.X()*vd[0] + nrm.Y()*vd[1] + nrm.Z()*vd[2]) > 0.999:
                try:
                    wire = BRepTools.OuterWire_s(face)
                except Exception:
                    ex.Next()
                    continue
                wex = TopExp_Explorer(wire, TopAbs_EDGE)
                while wex.More():
                    e = TopoDS.Edge_s(wex.Current())
                    try:
                        c = BRepAdaptor_Curve(e)
                        p0, p1 = c.FirstParameter(), c.LastParameter()
                        npts = 2 if c.GetType() == 0 else 24  # Linie vs Kurve
                        pts = []
                        for k in range(npts + 1):
                            q = c.Value(p0 + (p1 - p0)*k/npts)
                            pt = (q.X(), q.Y(), q.Z())
                            pts.append((_dot(pt, ud), _dot(pt, wd)))
                        curves.append(pts)
                        total += len(pts)
                        if total > max_pts:
                            return None
                    except Exception:
                        pass
                    wex.Next()
        ex.Next()
    return curves or None
