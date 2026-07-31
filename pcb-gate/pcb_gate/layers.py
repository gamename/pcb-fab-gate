"""Copper-layer helpers shared by keepout.py and overlap.py."""
from __future__ import annotations

import math

from shapely.affinity import rotate as shapely_rotate
from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import Point, Polygon, box
from shapely.validation import make_valid

from . import sexp

ALL_LAYERS_MARKERS = ("F&B.Cu", "*.Cu")


def board_copper_layers(root: sexp.Node) -> list[str]:
    layers_decl = sexp.child(root, "layers")
    names: list[str] = []
    if layers_decl is not None:
        for entry in layers_decl[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str):
                if entry[1].endswith(".Cu"):
                    names.append(str(entry[1]))
    return names


def expand_layer_names(raw_names: list[str], copper_layers: list[str]) -> set[str]:
    if any(n in ALL_LAYERS_MARKERS for n in raw_names):
        return set(copper_layers)
    return {n for n in raw_names if n in copper_layers or n.endswith(".Cu")}


def pad_layers(pad: sexp.Node, copper_layers: list[str]) -> set[str]:
    layers_node = sexp.child(pad, "layers")
    if layers_node is None:
        return set()
    raw = [x for x in layers_node[1:] if isinstance(x, str)]
    return expand_layer_names(raw, copper_layers)


def read_xy_points(pts_node: sexp.Node) -> list[tuple[float, float]]:
    points = []
    for xy in pts_node[1:]:
        if sexp.tag(xy) == "xy":
            points.append((sexp.as_float(xy[1]), sexp.as_float(xy[2])))
    return points


def safe_polygon(pts: list[tuple[float, float]]):
    """Build a polygon from raw KiCad outline points, repairing self-touching rings.

    Confirmed on a real board (gni-ol-pcb, GND pour on B.Cu, 2026-07-26):
    KiCad encodes a filled zone with holes (pads/keepouts carved out of a
    pour) as a SINGLE ring that dips in and back out through a zero-width
    slit to each hole, rather than as separate interior rings. shapely's
    `Polygon()` treats that as an invalid (self-touching) ring, and
    `distance()`/`intersects()` results on an invalid geometry are undefined
    per shapely's own documentation - confirmed empirically as a source of
    false "0.0000mm" distances near carved-out pads. `make_valid()` repairs it
    into a proper (Multi)Polygon (or GeometryCollection) with real holes.
    """
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    return poly if poly.is_valid else make_valid(poly)


def footprint_reference(footprint: sexp.Node) -> str:
    """A footprint's reference designator (e.g. "R2").

    KiCad 10 stores it as `(property "Reference" "R2" ...)`; older exports use
    `(fp_text reference "R2" ...)`. Support both rather than assuming.
    """
    for prop in sexp.children(footprint, "property"):
        if len(prop) > 1 and prop[1] == "Reference" and len(prop) > 2 and isinstance(prop[2], str):
            return prop[2]
    for fp_text in sexp.children(footprint, "fp_text"):
        if len(fp_text) > 1 and fp_text[1] == "reference" and len(fp_text) > 2 and isinstance(fp_text[2], str):
            return fp_text[2]
    return "?"


def _pad_shape(pad: sexp.Node) -> str:
    # (pad "<num>" <type: thru_hole|smd|np_thru_hole|connect> <shape> ...)
    if len(pad) > 3 and isinstance(pad[3], str):
        return pad[3]
    return "rect"


def _pad_roundrect_ratio(pad: sexp.Node) -> float:
    node = sexp.child(pad, "roundrect_rratio")
    if node is None or len(node) < 2:
        return 0.0
    try:
        return sexp.as_float(node[1])
    except (TypeError, ValueError):
        return 0.0


def _pad_local_outline(shape: str, sx: float, sy: float, rratio: float = 0.0) -> Polygon:
    """A pad's own outline before pad-angle/footprint-angle/position are applied.

    circle/oval/rect/roundrect are exact; trapezoid/custom fall back to the
    full bounding rectangle - a conservative (never-smaller) stand-in, not a
    true outline. That fallback is the known, expected source of first-run
    false positives against KiCad's real DRC engine near non-rectangular
    corners (RULE 8.1: root-cause, don't suppress) - circle/oval were fixed
    here because GNI-0283's first real-board run hit exactly that case on a
    THT circular pad; roundrect was fixed after mansio-pcb's spin-2 board
    (2026-07-31) showed 24 false overlap_clearance hits against 0805/SOD-323
    SMD pads (KiCad's default roundrect shape for both footprint families) -
    `kicad-cli pcb drc`, which knows the real rounded-corner geometry,
    reported 0 violations on the same board.

    roundrect's corner radius is `roundrect_rratio * min(sx, sy)` (KiCad's own
    definition - the ratio is always against the SHORTER side, even for a
    non-square pad). Built as the Minkowski sum of the inset rectangle with a
    disk of that radius (`box(...).buffer(radius, join_style="round")`) -
    shapely's standard technique for a rounded rectangle, not an
    approximation itself. `rratio` is clamped to 0.5 (KiCad's own UI maximum;
    a larger value would invert the inset rectangle) and a non-positive
    result (rratio <= 0) falls back to the plain rectangle, since that's
    exactly what a 0-ratio roundrect is.
    """
    if shape in ("circle", "oval"):
        unit_circle = Point(0, 0).buffer(1.0, quad_segs=16)
        return shapely_scale(unit_circle, sx / 2, sy / 2, origin=(0, 0))
    if shape == "roundrect" and rratio > 0:
        radius = min(rratio, 0.5) * min(sx, sy)
        inset = box(-sx / 2 + radius, -sy / 2 + radius, sx / 2 - radius, sy / 2 - radius)
        return inset.buffer(radius, quad_segs=16, join_style="round")
    return box(-sx / 2, -sy / 2, sx / 2, sy / 2)


def pad_geometry(footprint: sexp.Node, pad: sexp.Node) -> Polygon | None:
    """A pad's absolute-position outline, footprint placement and rotation applied.

    Shape-aware for circle/oval/rect/roundrect (exact); trapezoid/custom use a
    conservative bounding-rectangle stand-in - see `_pad_local_outline`.

    Rotation convention (SVW-0043): KiCad board coordinates are y-down, and a
    positive stored angle rotates counter-clockwise ON SCREEN - in y-down axes
    that is (x*cos + y*sin, -x*sin + y*cos), the INVERSE of shapely's y-up
    counter-clockwise `rotate`. The pad's own stored angle is absolute in
    board files (KiCad's writer stores footprint + pad-local summed), so the
    shape is rotated by the pad angle alone and only the pad's OFFSET rotates
    with the footprint angle. The previous +angle/+angle form evaluated every
    pad of every rotated footprint at a mirrored phantom position - found on
    gni-ol-pcb spin-3, where U1 (a 44-pin DevKitC at 90 deg) had its real pad
    at (121.08, 110) checked at (110.92, 110), producing 29 false
    overlap_clearance hits against copper that was >4 mm clear in reality,
    while leaving the pads' REAL neighbourhoods unchecked.
    """
    fp_at = sexp.child(footprint, "at")
    if fp_at is None:
        return None
    fx, fy = sexp.as_float(fp_at[1]), sexp.as_float(fp_at[2])
    f_angle = sexp.as_float(fp_at[3]) if len(fp_at) > 3 else 0.0

    pad_at = sexp.child(pad, "at")
    size_node = sexp.child(pad, "size")
    if pad_at is None or size_node is None:
        return None
    px, py = sexp.as_float(pad_at[1]), sexp.as_float(pad_at[2])
    p_angle = sexp.as_float(pad_at[3]) if len(pad_at) > 3 else 0.0
    sx, sy = sexp.as_float(size_node[1]), sexp.as_float(size_node[2])

    local = _pad_local_outline(_pad_shape(pad), sx, sy, _pad_roundrect_ratio(pad))
    # Absolute shape angle, mapped into shapely's y-up frame.
    local = shapely_rotate(local, -p_angle, origin=(0, 0))
    # Pad offset rotates with the footprint, y-down convention.
    th = math.radians(f_angle)
    wx = px * math.cos(th) + py * math.sin(th)
    wy = -px * math.sin(th) + py * math.cos(th)
    return shapely_translate(local, fx + wx, fy + wy)
