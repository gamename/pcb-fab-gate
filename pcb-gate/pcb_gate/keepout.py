"""GATE 6 - antenna keepout, verified by an explicit geometric script.

`kicad-cli pcb drc` does not evaluate rule areas / keepouts at all (confirmed:
no CLI flag exposes it, and RULE 6.6/GATE 6 of SVW-0034 call this out by
name) - a clean CLI DRC means nothing about the antenna. This module is the
geometric check that closes that gap: it finds the `ANT_KEEPOUT` rule area,
and tests every copper item on every copper layer for intersection with it,
including filled zone polygons (the actual "pour leaked into the antenna
clearance" failure mode, and the whole reason this check exists).

Fill-freshness: `kicad-cli pcb drc --refill-zones --save-board` (KiCad 10.0.4,
confirmed empirically) refills a board's zones and writes the result back to
INPUT_FILE. This module uses that to refresh a *throwaway copy* of the board
before reading filled-zone geometry - never the real board. If kicad-cli is
unavailable, it falls back to the committed fill and says so loudly, per the
brief: silence here would recreate the exact problem GNI-0283 exists to fix.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import kicad_tools, netclass, sexp
from .layers import (
    board_copper_layers,
    expand_layer_names,
    footprint_reference,
    pad_geometry,
    pad_layers,
    read_xy_points,
    safe_polygon,
)
from .project import ProjectFiles, load_kicad_pro
from .report import Report

KEEPOUT_ZONE_NAME = "ANT_KEEPOUT"
ALLOWED_NETCLASS = "RF_ANT"


def find_ant_keepout_zones(root: sexp.Node) -> list[sexp.Node]:
    zones = []
    for zone in sexp.children(root, "zone"):
        if sexp.child(zone, "keepout") is None:
            continue
        name_node = sexp.child(zone, "name")
        if sexp.text_of(name_node) == KEEPOUT_ZONE_NAME:
            zones.append(zone)
    return zones


def zone_layer_names(zone: sexp.Node, copper_layers: list[str]) -> set[str]:
    layer_node = sexp.child(zone, "layer")
    if layer_node is not None:
        raw = sexp.text_of(layer_node)
        return expand_layer_names([raw] if raw else [], copper_layers)
    layers_node = sexp.child(zone, "layers")
    if layers_node is not None:
        raw = [x for x in layers_node[1:] if isinstance(x, str)]
        return expand_layer_names(raw, copper_layers)
    return set()


def zone_outline_polygons(zone: sexp.Node) -> list[Polygon]:
    polys = []
    for poly_node in sexp.children(zone, "polygon"):
        pts_node = sexp.child(poly_node, "pts")
        if pts_node is None:
            continue
        pts = read_xy_points(pts_node)
        geom = safe_polygon(pts)
        if geom is not None:
            polys.append(geom)
    return polys


def _refilled_copy_or_none(files: ProjectFiles, report: Report):
    if not kicad_tools.available():
        report.check(
            "kicad-cli not on PATH - falling back to the committed zone fill. "
            "GATE 12 human-review item: confirm zone fill freshness (press B) before fab."
        )
        return None
    with tempfile.TemporaryDirectory(prefix="pcb-gate-keepout-refill-") as tmp:
        copy_dir = Path(tmp) / "project"
        shutil.copytree(files.project_dir, copy_dir)
        copy_pcb = copy_dir / files.pcb_file.name
        result = kicad_tools.refill_zones_in_place(copy_pcb, copy_dir / "refill.json")
        if result.exit_code != 0:
            report.check(
                f"kicad-cli zone refill did not complete cleanly (exit {result.exit_code}) - "
                "falling back to the committed fill. GATE 12 human-review item: confirm zone "
                "fill freshness (press B) before fab."
            )
            return None
        report.check("zones refilled in a throwaway copy before checking (kicad-cli pcb drc --refill-zones)")
        return sexp.parse_file(copy_pcb)


def _check_segments(check_root, keepout_layers, keepout_area, is_allowed, report) -> int:
    count = 0
    for seg in sexp.children(check_root, "segment"):
        layer = sexp.text_of(sexp.child(seg, "layer"))
        if layer not in keepout_layers:
            continue
        count += 1
        start_node, end_node = sexp.child(seg, "start"), sexp.child(seg, "end")
        width_node = sexp.child(seg, "width")
        width = sexp.as_float(width_node[1]) if width_node else 0.0
        geom = LineString(
            [
                (sexp.as_float(start_node[1]), sexp.as_float(start_node[2])),
                (sexp.as_float(end_node[1]), sexp.as_float(end_node[2])),
            ]
        ).buffer(width / 2)
        net_name = sexp.text_of(sexp.child(seg, "net"))
        if geom.intersects(keepout_area) and not is_allowed(net_name):
            report.fail(
                "keepout_track_intrusion",
                f"track on {layer} (net {net_name!r}) intersects '{KEEPOUT_ZONE_NAME}'",
            )
    return count


def _check_vias(check_root, copper_layers, keepout_layers, keepout_area, is_allowed, report) -> int:
    count = 0
    for via in sexp.children(check_root, "via"):
        layers_node = sexp.child(via, "layers")
        raw_layers = [x for x in (layers_node[1:] if layers_node else []) if isinstance(x, str)]
        via_layers = expand_layer_names(raw_layers, copper_layers)
        if not (via_layers & keepout_layers):
            continue
        count += 1
        at_node = sexp.child(via, "at")
        size_node = sexp.child(via, "size")
        cx, cy = sexp.as_float(at_node[1]), sexp.as_float(at_node[2])
        radius = sexp.as_float(size_node[1]) / 2 if size_node else 0.0
        geom = Point(cx, cy).buffer(radius)
        net_name = sexp.text_of(sexp.child(via, "net"))
        if geom.intersects(keepout_area) and not is_allowed(net_name):
            report.fail(
                "keepout_via_intrusion",
                f"via at ({cx}, {cy}) on layers {sorted(via_layers)} (net {net_name!r}) "
                f"intersects '{KEEPOUT_ZONE_NAME}'",
            )
    return count


def _check_pads(check_root, copper_layers, keepout_layers, keepout_area, is_allowed, report) -> int:
    count = 0
    for footprint in sexp.children(check_root, "footprint"):
        for pad in sexp.children(footprint, "pad"):
            layers_here = pad_layers(pad, copper_layers)
            if not (layers_here & keepout_layers):
                continue
            geom = pad_geometry(footprint, pad)
            if geom is None:
                continue
            count += 1
            net_name = sexp.text_of(sexp.child(pad, "net"))
            if geom.intersects(keepout_area) and not is_allowed(net_name):
                pad_num = pad[1] if len(pad) > 1 else "?"
                ref = footprint_reference(footprint)
                report.fail(
                    "keepout_pad_intrusion",
                    f"pad {pad_num!r} of {ref} on layers {sorted(layers_here)} "
                    f"(net {net_name!r}) intersects '{KEEPOUT_ZONE_NAME}'",
                )
    return count


def _check_filled_polygons(check_root, keepout_zones, keepout_layers, keepout_area, is_allowed, report) -> int:
    count = 0
    for zone in sexp.children(check_root, "zone"):
        if zone in keepout_zones:
            continue  # the keepout zone itself has no copper to check against itself
        zone_net = sexp.text_of(sexp.child(zone, "net"))
        for filled in sexp.children(zone, "filled_polygon"):
            layer = sexp.text_of(sexp.child(filled, "layer"))
            if layer not in keepout_layers:
                continue
            pts_node = sexp.child(filled, "pts")
            if pts_node is None:
                continue
            geom = safe_polygon(read_xy_points(pts_node))
            if geom is None:
                continue
            count += 1
            if geom.intersects(keepout_area) and not is_allowed(zone_net):
                report.fail(
                    "keepout_pour_intrusion",
                    f"filled zone pour on {layer} (net {zone_net!r}) intersects '{KEEPOUT_ZONE_NAME}' - "
                    "copper poured into the antenna clearance",
                )
    return count


def run(files: ProjectFiles) -> Report:
    report = Report(tool="pcb-gate keepout", project=files.base_name)
    root = sexp.parse_file(files.pcb_file)

    zones = find_ant_keepout_zones(root)
    if not zones:
        report.skip(f"no '{KEEPOUT_ZONE_NAME}' rule area on this board - nothing to check")
        return report

    report.check(f"found {len(zones)} '{KEEPOUT_ZONE_NAME}' rule area(s)")
    copper_layers = board_copper_layers(root)

    keepout_layers: set[str] = set()
    keepout_polys: list[Polygon] = []
    for zone in zones:
        keepout_layers |= zone_layer_names(zone, copper_layers)
        keepout_polys.extend(zone_outline_polygons(zone))

    if not keepout_polys:
        report.fail(
            "degenerate_keepout_zone", f"'{KEEPOUT_ZONE_NAME}' rule area has no usable polygon outline"
        )
        return report
    report.check(f"'{KEEPOUT_ZONE_NAME}' applies to layers: {sorted(keepout_layers)}")
    keepout_area = unary_union(keepout_polys)

    pro = load_kicad_pro(files.pro_file)
    refilled_root = _refilled_copy_or_none(files, report)
    check_root = refilled_root if refilled_root is not None else root

    def is_allowed(net_name: str | None) -> bool:
        if not net_name:
            return False
        return netclass.netclass_of(net_name, pro) == ALLOWED_NETCLASS

    n_segments = _check_segments(check_root, keepout_layers, keepout_area, is_allowed, report)
    n_vias = _check_vias(check_root, copper_layers, keepout_layers, keepout_area, is_allowed, report)
    n_pads = _check_pads(check_root, copper_layers, keepout_layers, keepout_area, is_allowed, report)
    n_pours = _check_filled_polygons(check_root, zones, keepout_layers, keepout_area, is_allowed, report)

    report.check(
        f"checked {n_segments} track segment(s), {n_vias} via(s), {n_pads} pad(s), "
        f"{n_pours} filled zone pour(s) on keepout-applicable layers"
    )
    return report
