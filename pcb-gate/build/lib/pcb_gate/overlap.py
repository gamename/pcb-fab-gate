"""GATE 5 - independent same-layer overlap check.

Deliberately does not use KiCad's DRC engine (`kicad-cli`, `pcbnew`) or
`kiutils` for anything - only `sexp.py` (this package's own hand-rolled
parser) and `shapely`. The point (SVW-0034 GATE 5 / RULE 8.1) is that a
regression in KiCad's own rule configuration - a zeroed netclass, a
mis-scoped custom rule, a downgraded severity - cannot silently re-hide a
short, because this checker never reads KiCad's rule configuration at all.
It reads raw copper geometry and the fab's own clearance floor from
`pcb-capability.yml`, and nothing else.

Per RULE 8.1, when this disagrees with KiCad DRC, root-cause it - do not
silence either side. First-run disagreements are expected: net-ties, exact
zero-width edge cases, and this checker's rectangular pad approximation are
all plausible sources and belong in the PR description, not a suppression
list.
"""
from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from . import capability as capability_mod
from . import sexp
from .layers import (
    board_copper_layers,
    expand_layer_names,
    footprint_reference,
    pad_geometry,
    pad_layers,
    read_xy_points,
    safe_polygon,
)
from .project import ProjectFiles
from .report import Report


@dataclass
class CopperItem:
    kind: str
    ref: str
    net: str
    geom: object


def _segment_items(root: sexp.Node, copper_layers: set[str]) -> dict[str, list[CopperItem]]:
    by_layer: dict[str, list[CopperItem]] = {}
    for seg in sexp.children(root, "segment"):
        layer = sexp.text_of(sexp.child(seg, "layer"))
        if layer not in copper_layers:
            continue
        start_node, end_node = sexp.child(seg, "start"), sexp.child(seg, "end")
        width_node = sexp.child(seg, "width")
        width = sexp.as_float(width_node[1]) if width_node else 0.0
        geom = LineString(
            [
                (sexp.as_float(start_node[1]), sexp.as_float(start_node[2])),
                (sexp.as_float(end_node[1]), sexp.as_float(end_node[2])),
            ]
        ).buffer(width / 2)
        net = sexp.text_of(sexp.child(seg, "net")) or ""
        ref = sexp.text_of(sexp.child(seg, "uuid")) or "segment"
        by_layer.setdefault(layer, []).append(CopperItem("segment", ref, net, geom))
    return by_layer


def _via_items(root: sexp.Node, copper_layers: list[str], layer_filter: set[str]) -> dict[str, list[CopperItem]]:
    by_layer: dict[str, list[CopperItem]] = {}
    for via in sexp.children(root, "via"):
        layers_node = sexp.child(via, "layers")
        raw = [x for x in (layers_node[1:] if layers_node else []) if isinstance(x, str)]
        via_layers = expand_layer_names(raw, copper_layers) & layer_filter
        if not via_layers:
            continue
        at_node, size_node = sexp.child(via, "at"), sexp.child(via, "size")
        cx, cy = sexp.as_float(at_node[1]), sexp.as_float(at_node[2])
        radius = sexp.as_float(size_node[1]) / 2 if size_node else 0.0
        geom = Point(cx, cy).buffer(radius)
        net = sexp.text_of(sexp.child(via, "net")) or ""
        ref = sexp.text_of(sexp.child(via, "uuid")) or "via"
        for layer in via_layers:
            by_layer.setdefault(layer, []).append(CopperItem("via", ref, net, geom))
    return by_layer


def _pad_items(root: sexp.Node, copper_layers: list[str], layer_filter: set[str]) -> dict[str, list[CopperItem]]:
    by_layer: dict[str, list[CopperItem]] = {}
    for footprint in sexp.children(root, "footprint"):
        ref_des = footprint_reference(footprint)
        for pad in sexp.children(footprint, "pad"):
            layers_here = pad_layers(pad, copper_layers) & layer_filter
            if not layers_here:
                continue
            geom = pad_geometry(footprint, pad)
            if geom is None:
                continue
            net = sexp.text_of(sexp.child(pad, "net")) or ""
            pad_num = pad[1] if len(pad) > 1 else "?"
            ref = f"{ref_des}.{pad_num}"
            for layer in layers_here:
                by_layer.setdefault(layer, []).append(CopperItem("pad", ref, net, geom))
    return by_layer


def _pour_items(root: sexp.Node, layer_filter: set[str]) -> dict[str, list[CopperItem]]:
    by_layer: dict[str, list[CopperItem]] = {}
    for zone in sexp.children(root, "zone"):
        if sexp.child(zone, "keepout") is not None:
            continue  # a rule area carries no copper of its own
        net = sexp.text_of(sexp.child(zone, "net")) or ""
        for filled in sexp.children(zone, "filled_polygon"):
            layer = sexp.text_of(sexp.child(filled, "layer"))
            if layer not in layer_filter:
                continue
            pts_node = sexp.child(filled, "pts")
            if pts_node is None:
                continue
            geom = safe_polygon(read_xy_points(pts_node))
            if geom is None:
                continue
            ref = sexp.text_of(sexp.child(zone, "uuid")) or "zone"
            by_layer.setdefault(layer, []).append(CopperItem("pour", ref, net, geom))
    return by_layer


def collect_items_by_layer(root: sexp.Node) -> dict[str, list[CopperItem]]:
    copper_layers = board_copper_layers(root)
    layer_filter = set(copper_layers)
    by_layer: dict[str, list[CopperItem]] = {layer: [] for layer in copper_layers}

    for source in (
        _segment_items(root, layer_filter),
        _via_items(root, copper_layers, layer_filter),
        _pad_items(root, copper_layers, layer_filter),
        _pour_items(root, layer_filter),
    ):
        for layer, items in source.items():
            by_layer.setdefault(layer, []).extend(items)

    return by_layer


def check_layer(items: list[CopperItem], min_clearance_mm: float, layer: str, report: Report) -> None:
    geoms = [it.geom for it in items]
    tree = STRtree(geoms)
    reported_pairs: set[tuple[int, int]] = set()

    for i, item in enumerate(items):
        query_area = item.geom.buffer(min_clearance_mm)
        for j in tree.query(query_area):
            j = int(j)
            if j <= i:
                continue
            other = items[j]
            if item.net and item.net == other.net:
                continue
            pair = (i, j)
            if pair in reported_pairs:
                continue
            distance = item.geom.distance(other.geom)
            if distance < min_clearance_mm:
                reported_pairs.add(pair)
                report.fail(
                    "overlap_clearance",
                    f"{layer}: {item.kind} {item.ref!r} (net {item.net!r}) is {distance:.4f}mm from "
                    f"{other.kind} {other.ref!r} (net {other.net!r}) - below the fab's "
                    f"{min_clearance_mm}mm minimum clearance",
                )


def run(files: ProjectFiles) -> Report:
    report = Report(tool="pcb-gate overlap", project=files.base_name)

    try:
        cap = capability_mod.load(files.capability_file)
    except capability_mod.CapabilityError as exc:
        report.fail("missing_capability_file", str(exc))
        return report

    min_clearance = cap.constraints["min_clearance_mm"]
    report.check(
        f"independent same-layer overlap check at {min_clearance}mm minimum clearance "
        f"({cap.fab} / {cap.stackup}, no KiCad DRC engine involved)"
    )

    root = sexp.parse_file(files.pcb_file)
    by_layer = collect_items_by_layer(root)

    total = 0
    for layer, items in sorted(by_layer.items()):
        if len(items) < 2:
            continue
        total += len(items)
        report.check(f"{layer}: {len(items)} copper item(s) checked pairwise for different-net overlap")
        check_layer(items, min_clearance, layer, report)

    report.check(f"checked {total} copper item(s) across {len(by_layer)} copper layer(s)")
    return report
