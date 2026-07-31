"""pad_geometry rotation-convention regression tests (SVW-0043).

KiCad board coordinates are y-down; a positive stored angle is a
counter-clockwise rotation on screen, i.e. (x*cos + y*sin, -x*sin + y*cos)
in raw coordinates - the inverse of shapely's y-up `rotate`. The original
implementation rotated with shapely's sign, mirroring every rotated
footprint's pads to phantom positions (found on gni-ol-pcb spin-3, U1 at
90 deg: real pad (121.08, 110) evaluated at (110.92, 110)).

Every expected coordinate below is taken from a real board position that
KiCad's own DRC/netlist engine agreed with, not derived from this module's
own math.
"""
from pcb_gate import sexp
from pcb_gate.layers import pad_geometry

from tests.conftest import footprint, pad


def _centroid(fp_node, pad_node):
    geom = pad_geometry(fp_node, pad_node)
    assert geom is not None
    c = geom.centroid
    return (round(c.x, 6), round(c.y, 6))


def test_unrotated_footprint_pad_is_simple_translation():
    fp = footprint("R1", (10, 10), [])
    p = pad("2", "thru_hole", "circle", (2.54, 0), (1.6, 1.6), "/A", "u1")
    assert _centroid(fp, p) == (12.54, 10)


def test_rot90_footprint_pad_offset_rotates_y_down():
    # gni-ol-pcb U1 (ESP32 DevKitC carrier) at (116, 110) rot 90: pad 3 with
    # relative offset (0, 5.08) sits at world (121.08, 110) - verified by
    # KiCad DRC and the routed RST net on the real board. The pre-fix code
    # put it at (110.92, 110).
    fp = footprint("U1", (116, 110), [], angle=90)
    p = pad("3", "thru_hole", "oval", (0, 5.08), (1.2, 2), "/RST", "u3")
    assert _centroid(fp, p) == (121.08, 110)


def test_rot270_footprint_pad_offset_rotates_y_down():
    # gni-ol-pcb C10 (disc cap) at (186.5, 130.5) rot 270: pad 2 with
    # relative offset (5, 0) sits at world (186.5, 135.5) on the real board.
    fp = footprint("C10", (186.5, 130.5), [], angle=270)
    p = pad("2", "thru_hole", "circle", (5, 0), (1.6, 1.6), "GND", "u2", angle=270)
    assert _centroid(fp, p) == (186.5, 135.5)


def test_rot180_footprint_pad_offset_negates():
    # gni-ol-pcb J1 (screw terminal) at (118, 143) rot 180: pad 2 with
    # relative offset (5.08, 0) sits at world (112.92, 143).
    fp = footprint("J1", (118, 143), [], angle=180)
    p = pad("2", "thru_hole", "circle", (5.08, 0), (2.6, 2.6), "GND", "u2", angle=180)
    assert _centroid(fp, p) == (112.92, 143)


def test_pad_angle_is_absolute_shape_rotation():
    # A 1x3 rect pad whose stored angle is 90 presents a 3-wide x 1-tall
    # outline in world coordinates regardless of how it got that angle
    # (KiCad's writer stores footprint+pad-local summed, i.e. absolute).
    fp = footprint("U9", (50, 50), [], angle=90)
    p = pad("1", "thru_hole", "rect", (0, 0), (1, 3), "/A", "u9", angle=90)
    geom = pad_geometry(fp, p)
    minx, miny, maxx, maxy = geom.bounds
    assert round(maxx - minx, 6) == 3
    assert round(maxy - miny, 6) == 1


def test_offset_rotation_does_not_double_rotate_shape():
    # Shape angle comes from the pad's stored angle ALONE; the footprint
    # angle must only move the pad, not add a second shape rotation.
    # 1x3 rect, pad angle 0, footprint angle 90: outline stays 1-wide.
    fp = footprint("U8", (0, 0), [], angle=90)
    p = pad("1", "thru_hole", "rect", (10, 0), (1, 3), "/A", "u8")
    geom = pad_geometry(fp, p)
    minx, miny, maxx, maxy = geom.bounds
    assert round(maxx - minx, 6) == 1
    assert round(maxy - miny, 6) == 3
    c = geom.centroid
    # offset (10, 0) under y-down rot 90 -> (0, -10)
    assert (round(c.x, 6), round(c.y, 6)) == (0, -10)
