# -*- coding: utf-8 -*-
"""Synthetische STEP-Testkoerper mit BEKANNTER Zusammensetzung."""
import math
from OCP.gp import gp_Pnt, gp_Dir, gp_Ax2
from hole_detection import ocp_static
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakeCone)
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopoDS import TopoDS
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Line


def cyl(x, y, z, dx, dy, dz, r, h):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(x, y, z), gp_Dir(dx, dy, dz)), r, h).Shape()


def cone(x, y, z, dx, dy, dz, r1, r2, h):
    return BRepPrimAPI_MakeCone(
        gp_Ax2(gp_Pnt(x, y, z), gp_Dir(dx, dy, dz)), r1, r2, h).Shape()


def cut(a, b):
    return BRepAlgoAPI_Cut(a, b).Shape()


def make_mixed_plate(step_path=None, with_fillets=True):
    """Platte 120x80x15 mm, Bohrachsen in Z (Ansicht 'top' = axial).

    ERWARTET (Ground Truth):
      Typ A: 4x M8-Gewinde (nur Kernloch D6.8, durchgehend)
      Typ B: 1x M6-Gewinde (Kern D5.0 + Nennzylinder D6.0 modelliert, Sackloch T10)
      Typ C: 3x Bohrung D8 Durchgang
      Typ D: 2x Bohrung D10 Sackloch T8
      Typ E: 1x Senkbohrung D6.6 Durchgang mit Senkung D11 T5
      Typ F: 1x Kegelsenkung D5.5 Durchgang (Kegel 90 Grad)
      Typ G: 2x Querbohrung D5 (Achse in X, von der Seite, Sackloch T30)
      Stoergeometrie: 4 vertikale Kantenverrundungen R4 (konvex)
                      + obere umlaufende Kante R2 (konvex)
      -> Gesamt 14 Formelemente, 7 Typen.
    """
    L, B, H = 120.0, 80.0, 15.0
    plate = BRepPrimAPI_MakeBox(L, B, H).Shape()

    if with_fillets:
        # vertikale Kanten R4 + obere Kanten R2 als Stoergeometrie
        mk = BRepFilletAPI_MakeFillet(plate)
        ex = TopExp_Explorer(plate, TopAbs_EDGE)
        while ex.More():
            e = ocp_static(TopoDS, "Edge")(ex.Current())
            ad = BRepAdaptor_Curve(e)
            if ad.GetType() == GeomAbs_Line:
                d = ad.Line().Direction()
                p = ad.Value(0.5*(ad.FirstParameter()+ad.LastParameter()))
                if abs(d.Z()) > 0.99:                       # vertikale Kante
                    mk.Add(4.0, e)
                elif abs(p.Z() - H) < 1e-6:                 # obere Kante
                    mk.Add(2.0, e)
            ex.Next()
        plate = mk.Shape()

    tools = []
    # A: 4x M8 Kernloch D6.8 durch, mit Gewindefase 90 Grad (D8.8->D6.8)
    for (x, y) in [(15, 15), (105, 15), (15, 65), (105, 65)]:
        tools.append(cyl(x, y, -1, 0, 0, 1, 3.4, H+2))
        tools.append(cone(x, y, H-1.0, 0, 0, 1, 4.4, 3.4, 1.05))
    # B: 1x M6 mit Kern D5 + Nenn D6, Sackloch T10 (von oben)
    tools.append(cyl(60, 65, H-10, 0, 0, 1, 2.5, 11))   # Kern
    tools.append(cyl(60, 65, H-10, 0, 0, 1, 3.0, 11))   # Nennzylinder
    # (der groessere schneidet den kleineren mit weg -> zwei koaxiale Waende
    #  entstehen NICHT durch simple Vereinigung; darum: Kern tiefer als Nenn)
    tools[-2] = cyl(60, 65, H-10, 0, 0, 1, 2.5, 11)
    tools[-1] = cyl(60, 65, H-6, 0, 0, 1, 3.0, 7)       # Nenn nur oben 6mm
    tools.append(cone(60, 65, H-0.8, 0, 0, 1, 3.8, 3.0, 0.85))  # Gewindefase M6
    # C: 3x D8 Durchgang
    for (x, y) in [(40, 40), (60, 40), (80, 40)]:
        tools.append(cyl(x, y, -1, 0, 0, 1, 4.0, H+2))
    # D: 2x D10 Sackloch T8 von oben
    for (x, y) in [(30, 62), (90, 62)]:
        tools.append(cyl(x, y, H-8, 0, 0, 1, 5.0, 9))
    # E: 1x Senkbohrung D6.6 durch + Senkung D11 T5 von oben
    tools.append(cyl(60, 15, -1, 0, 0, 1, 3.3, H+2))
    tools.append(cyl(60, 15, H-5, 0, 0, 1, 5.5, 6))
    # F: 1x D5.5 durch + 90-Grad-Kegelsenkung oben (D10.5 -> D5.5)
    tools.append(cyl(85, 15, -1, 0, 0, 1, 2.75, H+2))
    tools.append(cone(85, 15, H-2.5, 0, 0, 1, 5.25, 2.75, 2.6))
    # G: 2x Querbohrung D5 in X-Richtung, Sackloch T30, auf halber Hoehe
    tools.append(cyl(-1, 30, 7.5, 1, 0, 0, 2.5, 31))
    tools.append(cyl(L+1, 50, 7.5, -1, 0, 0, 2.5, 31))

    shape = plate
    for t in tools:
        shape = cut(shape, t)

    if step_path:
        w = STEPControl_Writer()
        w.Transfer(shape, STEPControl_AsIs)
        w.Write(step_path)
    return shape


GROUND_TRUTH = {
    "total_features": 14,
    "types": {
        "M8/Gewinde": 4, "M6/Gewinde": 1, "D8/Bohrung": 3,
        "D10/Bohrung": 2, "D6.6/Senkbohrung": 1, "D5.5/Kegelsenkung": 1,
        "D5/Bohrung-quer": 2,
    },
}


if __name__ == "__main__":
    make_mixed_plate("/home/claude/bohrtool/test_mixed.step")
    print("test_mixed.step geschrieben")
