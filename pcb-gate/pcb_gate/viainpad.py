"""RULE 10.2 - no unfilled via inside a solderable land.

Neither ERC, DRC nor any other step of this gate looks at this: a same-net via
inside a pad is legal copper, so KiCad has nothing to report and `overlap`
skips it as same-net. mh2-egg-carrier-v2 (SVW-0082, commit 06d9597) passed every
check in this repo with 16 of its 18 vias inside pads, found only by eye. Solder
wicks down an unfilled via's barrel and off the joint.

Reads raw geometry only, like `overlap` - `sexp.py` and `shapely`, never
KiCad's own rule configuration. A via is flagged when its full disc (drill and
annulus) overlaps a land on any copper layer they share.

A land is any pad with copper on an outer layer and a solder mask opening on the
same side: SMD, through-hole and edge-connector pads, paste or no paste (a
test point is still exposed copper). `np_thru_hole` pads have no land, and a pad
with no mask opening is covered copper, not something solder can reach.

An intentional via (an exposed pad's thermal via, plugged or tented per RULE
10.2) is declared per pad in `pcb-capability.yml` `declared_vias_in_pads`, with a
via count and a written reason. There is no automatic exemption by pad size,
name or net. The declaration is exact: more vias than declared fail, so one
reviewed thermal via can't cover a second one added later; fewer fail as stale;
a via on a different net from the pad always fails, declared or not.
"""
from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Point

from . import capability as capability_mod
from . import sexp
from .arming import _is_placeholder_reason
from .layers import (
    board_copper_layers,
    expand_layer_names,
    footprint_reference,
    pad_land_geometry,
    pad_layers,
)
from .project import ProjectFiles
from .report import Report

VIOLATION_CODE = "via_in_pad"

# KiCad resolves pad-to-disc contact to the nanometre; a 1 nm floor keeps a via
# exactly tangent to a pad edge from reading as an overlap through float noise.
_TANGENT_EPSILON_MM = 1e-6

_ALL_MASK_MARKERS = ("*.Mask", "F&B.Mask")


@dataclass(frozen=True)
class Land:
    ref: str
    pad: str
    net: str
    layers: frozenset[str]
    geom: object

    @property
    def key(self) -> tuple[str, str]:
        return (self.ref, self.pad)

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.pad}"


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    size: float
    drill: float
    net: str
    layers: frozenset[str]


@dataclass(frozen=True)
class Hit:
    via: Via
    land: Land
    layers: frozenset[str]
    overlap: str

    def describe(self) -> str:
        return (
            f"via at ({self.via.x}, {self.via.y}) mm (net {self.via.net!r}, size {self.via.size}, "
            f"drill {self.via.drill}) {self.overlap} {self.land.label} (net {self.land.net!r}) "
            f"on {', '.join(sorted(self.layers))}"
        )


def solderable_layers(pad: sexp.Node, copper_layers: list[str]) -> frozenset[str]:
    """Outer copper layers on which `pad` is exposed copper."""
    if len(pad) > 2 and pad[2] == "np_thru_hole":
        return frozenset()
    layers_node = sexp.child(pad, "layers")
    raw = [x for x in layers_node[1:] if isinstance(x, str)] if layers_node else []
    copper = pad_layers(pad, copper_layers)
    all_mask = any(marker in raw for marker in _ALL_MASK_MARKERS)
    return frozenset(
        f"{side}.Cu"
        for side in ("F", "B")
        if f"{side}.Cu" in copper and (all_mask or f"{side}.Mask" in raw)
    )


def collect_lands(root: sexp.Node, report: Report) -> list[Land]:
    copper_layers = board_copper_layers(root)
    lands = []
    for footprint in sexp.children(root, "footprint"):
        ref = footprint_reference(footprint)
        for pad in sexp.children(footprint, "pad"):
            layers = solderable_layers(pad, copper_layers)
            if not layers:
                continue
            number = str(pad[1]) if len(pad) > 1 else "?"
            geom = pad_land_geometry(footprint, pad)
            if geom is None:
                report.skip(f"{ref}.{number}: pad geometry could not be read", blocking=True)
                continue
            net = sexp.text_of(sexp.child(pad, "net")) or ""
            lands.append(Land(ref, number, net, layers, geom))
    return lands


def _via_size(via: sexp.Node) -> float | None:
    # A padstack via can carry per-layer sizes instead of one top-level (size);
    # the largest is the worst case on any layer.
    sizes = [sexp.as_float(node[1]) for node in sexp.find_all(via, "size") if len(node) > 1]
    return max(sizes) if sizes else None


def collect_vias(root: sexp.Node, report: Report) -> list[Via]:
    copper_layers = board_copper_layers(root)
    vias = []
    for node in sexp.children(root, "via"):
        at_node, drill_node = sexp.child(node, "at"), sexp.child(node, "drill")
        size = _via_size(node)
        if at_node is None or drill_node is None or size is None:
            report.skip(f"via {sexp.text_of(sexp.child(node, 'uuid'))!r}: geometry could not be read", blocking=True)
            continue
        layers_node = sexp.child(node, "layers")
        raw = [x for x in layers_node[1:] if isinstance(x, str)] if layers_node else []
        vias.append(
            Via(
                x=sexp.as_float(at_node[1]),
                y=sexp.as_float(at_node[2]),
                size=size,
                drill=sexp.as_float(drill_node[1]),
                net=sexp.text_of(sexp.child(node, "net")) or "",
                layers=frozenset(expand_layer_names(raw, copper_layers)),
            )
        )
    return vias


def _overlap_kind(via: Via, land: Land) -> str | None:
    """How `via` overlaps `land`, or None. Disc-to-polygon distance, so exact, not a buffered circle."""
    distance = Point(via.x, via.y).distance(land.geom)
    if distance >= via.size / 2 - _TANGENT_EPSILON_MM:
        return None
    if distance == 0:
        return "is centred inside"
    if distance < via.drill / 2:
        return "has its drill overlapping"
    return "has its annulus overlapping"


def find_hits(vias: list[Via], lands: list[Land]) -> list[Hit]:
    """One hit per via per pad, so a pad split into same-numbered parts counts each via once."""
    hits: dict[tuple[int, tuple[str, str]], Hit] = {}
    for index, via in enumerate(vias):  # by index: two identical stacked vias are two vias
        for land in lands:
            shared = via.layers & land.layers
            if not shared:
                continue
            overlap = _overlap_kind(via, land)
            if overlap is None:
                continue
            key = (index, land.key)
            previous = hits.get(key)
            layers = shared | previous.layers if previous else shared
            hits[key] = Hit(via, land, layers, previous.overlap if previous else overlap)
    return list(hits.values())


def _check_declarations(
    declared: tuple[capability_mod.DeclaredViaInPad, ...], report: Report
) -> dict[tuple[str, str], capability_mod.DeclaredViaInPad]:
    by_key: dict[tuple[str, str], capability_mod.DeclaredViaInPad] = {}
    for entry in declared:
        if (entry.ref, entry.pad) in by_key:
            report.fail(
                "duplicate_via_in_pad_declaration",
                f"declared_vias_in_pads lists {entry.ref}.{entry.pad} more than once",
            )
        by_key[(entry.ref, entry.pad)] = entry
        if _is_placeholder_reason(entry.reason):
            report.fail(
                "placeholder_via_in_pad_reason",
                f"declared_vias_in_pads entry for {entry.ref}.{entry.pad} has an empty or placeholder "
                f"reason ({entry.reason!r}) - the declaration is the review",
            )
    return by_key


def _adjudicate(hits: list[Hit], declared: dict, report: Report) -> None:
    by_pad: dict[tuple[str, str], list[Hit]] = {}
    for hit in hits:
        by_pad.setdefault(hit.land.key, []).append(hit)

    for key, entry in sorted(declared.items()):
        pad_hits = by_pad.get(key, [])
        if len(pad_hits) < entry.vias:
            report.fail(
                "stale_via_in_pad_declaration",
                f"declared_vias_in_pads says {entry.ref}.{entry.pad} carries {entry.vias} via(s), "
                f"found {len(pad_hits)} - update or remove the declaration",
            )

    for hit in hits:
        entry = declared.get(hit.land.key)
        if entry is None:
            report.fail(
                VIOLATION_CODE,
                f"{hit.describe()} (RULE 10.2) - move it out of the pad, or plug/tent it and declare it "
                "in pcb-capability.yml declared_vias_in_pads",
            )
        elif hit.via.net != hit.land.net:
            report.fail(VIOLATION_CODE, f"{hit.describe()} - a declaration never covers a via on another net")
        elif len(by_pad[hit.land.key]) > entry.vias:
            report.fail(
                VIOLATION_CODE,
                f"{hit.describe()} - {hit.land.label} carries {len(by_pad[hit.land.key])} via(s), "
                f"only {entry.vias} declared",
            )
        else:
            report.check(f"declared via in pad: {hit.describe()} ({entry.reason!r})")


def run(files: ProjectFiles) -> Report:
    report = Report(tool="pcb-gate via-in-pad", project=files.base_name)

    try:
        cap = capability_mod.load(files.capability_file)
    except capability_mod.CapabilityError as exc:
        report.fail("missing_capability_file", str(exc))
        return report

    root = sexp.parse_file(files.pcb_file)
    lands = collect_lands(root, report)
    vias = collect_vias(root, report)
    report.check(
        f"{len(vias)} via(s) tested against {len(lands)} solderable land(s), "
        f"{len(cap.declared_vias_in_pads)} pad(s) declared"
    )
    _adjudicate(find_hits(vias, lands), _check_declarations(cap.declared_vias_in_pads, report), report)
    return report
