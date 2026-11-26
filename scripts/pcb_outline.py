#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Export the Edge.Cuts outline of a PolyKybd board as an SVG for the camera view.

The Camera tab's "Adjust view" draws this outline, semi-transparent, warped into
the four corners you drag, so each half can be matched to the board in the image.
Reads the .kicad_pcb (the authoritative geometry; the SVG exports in PolyKybd can
be DPI-scaled and lose per-part rotations): the board-level Edge.Cuts lines and
arcs, and the Edge.Cuts lines inside footprints (the cut-outs), turned by each
footprint's position and rotation. It also draws an 18 mm keycap square at each
key switch (Kailh_socket_MX*) and a dot at each RGB LED (*WS2812*), which is
what the camera sees.

    pip install kiutils
    python scripts/pcb_outline.py ../PolyKybd/poly_kybd/poly_kybd_split72_left.kicad_pcb \\
        station/ui/static/pcb/split72_left.svg

The output is in board millimetres, cropped to the outline's bounding box. A JSON
file with the same name (split72_left.json) has the view box size and each LED's
reference and centre in the same millimetres; station/camera.py reads it to find
the LEDs in a rectified half.
"""
import json
import math
import os
import sys

from kiutils.board import Board


def _rot(x, y, deg):
    # KiCad turns counter-clockwise on screen with y pointing down: the world
    # point of a footprint-local (x, y) uses the angle with a negated sign.
    a = math.radians(-deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _arc_points(start, mid, end, steps=16):
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))

    def norm(a):
        return a % (2 * math.pi)
    span = norm(a3 - a1)
    if norm(a2 - a1) > span:   # the mid point is on the other way round
        span -= 2 * math.pi
    return [(ux + r * math.cos(a1 + span * i / steps), uy + r * math.sin(a1 + span * i / steps))
            for i in range(steps + 1)]


def segments(board):
    """Polylines (lists of (x, y) in mm) of every Edge.Cuts line and arc."""
    out = []
    for item in board.graphicItems:
        if getattr(item, "layer", None) != "Edge.Cuts":
            continue
        kind = type(item).__name__
        if kind == "GrLine":
            out.append([(item.start.X, item.start.Y), (item.end.X, item.end.Y)])
        elif kind == "GrArc":
            out.append(_arc_points((item.start.X, item.start.Y), (item.mid.X, item.mid.Y),
                                   (item.end.X, item.end.Y)))
        elif kind == "GrRect":
            x1, y1, x2, y2 = item.start.X, item.start.Y, item.end.X, item.end.Y
            out.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)])
        elif kind == "GrPoly":
            pts = [(p.X, p.Y) for p in item.coordinates]
            out.append(pts + pts[:1])
    for fp in board.footprints:
        px, py, ang = fp.position.X, fp.position.Y, fp.position.angle or 0.0
        for g in fp.graphicItems:
            if getattr(g, "layer", None) != "Edge.Cuts":
                continue
            kind = type(g).__name__
            if kind == "FpLine":
                local = [(g.start.X, g.start.Y), (g.end.X, g.end.Y)]
            elif kind == "FpArc":
                local = _arc_points((g.start.X, g.start.Y), (g.mid.X, g.mid.Y), (g.end.X, g.end.Y))
            elif kind == "FpRect":
                x1, y1, x2, y2 = g.start.X, g.start.Y, g.end.X, g.end.Y
                local = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
            else:
                continue
            out.append([(px + dx, py + dy) for dx, dy in (_rot(x, y, ang) for x, y in local)])
    return out


KEYCAP_MM = 18.0


def _reference(fp) -> str:
    for p in fp.properties if isinstance(fp.properties, list) else []:
        if getattr(p, "key", None) == "Reference":
            return p.value
    props = fp.properties if isinstance(fp.properties, dict) else {}
    return props.get("Reference", "")


def keys_and_leds(board):
    """(keycap squares as closed polylines, LED centre points), in mm."""
    caps, leds = [], []   # leds: (reference, x, y)
    half = KEYCAP_MM / 2
    for fp in board.footprints:
        lib = fp.libId.split(":")[-1]
        px, py, ang = fp.position.X, fp.position.Y, fp.position.angle or 0.0
        if lib.startswith("Kailh_socket_MX"):
            corners = [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]
            caps.append([(px + dx, py + dy) for dx, dy in (_rot(x, y, ang) for x, y in corners)])
        elif "WS2812" in lib:
            leds.append((_reference(fp), px, py))
    return caps, leds


def view_box(polys, margin=1.0):
    """(x0, y0, w, h): the outline's bounding box plus margin, in board mm.
    Keycaps may reach past it."""
    xs = [x for p in polys for x, _ in p]
    ys = [y for p in polys for _, y in p]
    x0, y0 = min(xs) - margin, min(ys) - margin
    return x0, y0, max(xs) - x0 + margin, max(ys) - y0 + margin


def to_json(polys, leds) -> str:
    x0, y0, w, h = view_box(polys)
    return json.dumps({"width_mm": round(w, 2), "height_mm": round(h, 2),
                       "leds": [{"ref": r, "x": round(x - x0, 2), "y": round(y - y0, 2)}
                                for r, x, y in sorted(leds, key=lambda l: _ref_key(l[0]))]},
                      indent=1) + "\n"


def _ref_key(ref: str):
    digits = "".join(c for c in ref if c.isdigit())
    return (ref.rstrip("0123456789"), int(digits) if digits else 0)


def to_svg(polys, caps=(), leds=()) -> str:
    x0, y0, w, h = view_box(polys)

    def poly(p):
        return '<polyline points="' + " ".join(f"{x - x0:.2f},{y - y0:.2f}" for x, y in p) + '"/>'
    edge = "".join(poly(p) for p in polys)
    keys = "".join(poly(p) for p in caps)
    dots = "".join(f'<circle cx="{x - x0:.2f}" cy="{y - y0:.2f}" r="1.2"/>' for _, x, y in leds)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}" '
            f'width="{w:.2f}mm" height="{h:.2f}mm">'
            f'<g fill="none" stroke="#00e5ff" stroke-width="0.6" '
            f'stroke-linejoin="round" stroke-linecap="round">{edge}</g>'
            f'<g fill="none" stroke="#ffd54f" stroke-width="0.35">{keys}</g>'
            f'<g fill="#ff4081">{dots}</g></svg>\n')


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    board = Board.from_file(argv[1])
    polys = segments(board)
    if not polys:
        print(f"no Edge.Cuts shapes in {argv[1]}", file=sys.stderr)
        return 1
    caps, leds = keys_and_leds(board)
    with open(argv[2], "w") as f:
        f.write(to_svg(polys, caps, leds))
    with open(os.path.splitext(argv[2])[0] + ".json", "w") as f:
        f.write(to_json(polys, leds))
    print(f"{argv[2]}: {len(polys)} outline shapes, {len(caps)} keys, {len(leds)} LEDs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
