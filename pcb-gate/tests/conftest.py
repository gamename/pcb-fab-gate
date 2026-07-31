"""Shared fixture builders: small synthetic KiCad projects, not real boards.

Every helper writes just enough of a `.kicad_pcb` / `.kicad_pro` for
`pcb_gate`'s own parser and checkers to exercise - not a full valid KiCad
project a real `pcbnew` could open. Tests that need `kicad-cli` skip when it
isn't on PATH (dev machines and this repo's CI both currently lack it; the
`kicad/kicad:10.0` container in `.github/workflows/pcb-fab-gate.yml` has it).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcb_gate import sexp
from pcb_gate.project import discover

DEFAULT_LAYERS = [
    ["0", sexp.qstr("F.Cu"), "signal"],
    ["31", sexp.qstr("B.Cu"), "signal"],
    ["36", sexp.qstr("F.SilkS"), "user"],
]


def make_kicad_pro(
    tmp_path: Path,
    name: str = "TestBoard",
    netclasses=None,
    rules=None,
    severities=None,
    erc_severities=None,
    exclusions=None,
) -> Path:
    netclasses = netclasses if netclasses is not None else [
        {"name": "Default", "clearance": 0.2, "track_width": 0.2}
    ]
    rules = rules if rules is not None else {
        "min_clearance": 0.2,
        "min_track_width": 0.2,
        "min_connection": 0.2,
    }
    severities = severities if severities is not None else {
        "clearance": "error",
        "shorting_items": "error",
        "courtyards_overlap": "error",
        "unconnected_items": "error",
        "items_not_allowed": "error",
        "track_dangling": "error",
        "via_dangling": "error",
        "missing_courtyard": "error",
    }
    erc_severities = erc_severities if erc_severities is not None else {}
    exclusions = exclusions if exclusions is not None else []

    data = {
        "net_settings": {
            "classes": netclasses,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "board": {
            "design_settings": {
                "rules": rules,
                "rule_severities": severities,
                "drc_exclusions": exclusions,
            }
        },
        "erc": {
            "rule_severities": erc_severities,
        },
    }
    path = tmp_path / f"{name}.kicad_pro"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_capability_yml(tmp_path: Path, **overrides) -> Path:
    lines = [
        "fab: JLCPCB",
        f"retrieved: {overrides.get('retrieved', '2026-07-01')}",
        "source: https://example.invalid/capabilities",
        "stackup: JLC04161H-7628",
        "constraints:",
        f"  min_track_width_mm: {overrides.get('min_track_width_mm', 0.1)}",
        f"  min_clearance_mm: {overrides.get('min_clearance_mm', 0.1)}",
        "  min_annular_ring_mm: 0.13",
        "  min_drill_mm: 0.2",
        "  min_silk_line_width_mm: 0.153",
        "  min_silk_text_height_mm: 1.0",
        "  edge_clearance_mm: 0.3",
    ]
    declared = overrides.get("declared_exclusions")
    if declared:
        lines.append("declared_exclusions:")
        for entry in declared:
            lines.append(f"  - rule: {entry['rule']}")
            lines.append(f"    reason: {entry['reason']}")
    downgrades = overrides.get("declared_severity_downgrades")
    if downgrades:
        lines.append("declared_severity_downgrades:")
        for entry in downgrades:
            lines.append(f"  - check: {entry['check']}")
            lines.append(f"    severity: {entry.get('severity', 'ignore')}")
            lines.append(f"    reason: {entry['reason']}")
    path = tmp_path / "pcb-capability.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def segment(start, end, width, net, uuid, layer="F.Cu"):
    return [
        "segment",
        ["start", str(start[0]), str(start[1])],
        ["end", str(end[0]), str(end[1])],
        ["width", str(width)],
        ["layer", sexp.qstr(layer)],
        ["net", sexp.qstr(net)],
        ["uuid", sexp.qstr(uuid)],
    ]


def via(at, size, drill, net, uuid, layers=("F.Cu", "B.Cu")):
    return [
        "via",
        ["at", str(at[0]), str(at[1])],
        ["size", str(size)],
        ["drill", str(drill)],
        ["layers", *[sexp.qstr(l) for l in layers]],
        ["net", sexp.qstr(net)],
        ["uuid", sexp.qstr(uuid)],
    ]


def footprint(ref, at, pads, angle=None):
    at_node = ["at", str(at[0]), str(at[1])]
    if angle is not None:
        at_node.append(str(angle))
    return [
        "footprint",
        sexp.qstr(f"Lib:{ref}"),
        at_node,
        ["property", sexp.qstr("Reference"), sexp.qstr(ref)],
        *pads,
    ]


def pad(num, kind, shape, at, size, net, uuid, layers=("F.Cu",), angle=None, roundrect_rratio=None):
    at_node = ["at", str(at[0]), str(at[1])]
    if angle is not None:
        at_node.append(str(angle))
    node = [
        "pad",
        str(num),
        kind,
        shape,
        at_node,
        ["size", str(size[0]), str(size[1])],
        ["layers", *[sexp.qstr(l) for l in layers]],
        ["net", sexp.qstr(net)],
        ["uuid", sexp.qstr(uuid)],
    ]
    if roundrect_rratio is not None:
        node.append(["roundrect_rratio", str(roundrect_rratio)])
    return node


def zone_pour(net, layer, pts, uuid, keepout=False, name=None):
    node = ["zone", ["net", sexp.qstr(net)], ["layer", sexp.qstr(layer)], ["uuid", sexp.qstr(uuid)]]
    if name:
        node.append(["name", sexp.qstr(name)])
    if keepout:
        node.append(
            [
                "keepout",
                ["tracks", "not_allowed"],
                ["vias", "not_allowed"],
                ["pads", "not_allowed"],
                ["copperpour", "not_allowed"],
                ["footprints", "not_allowed"],
            ]
        )
        node.append(["polygon", ["pts", *[["xy", str(x), str(y)] for x, y in pts]]])
    else:
        node.append(
            ["filled_polygon", ["layer", sexp.qstr(layer)], ["pts", *[["xy", str(x), str(y)] for x, y in pts]]]
        )
    return node


def make_kicad_pcb(tmp_path: Path, name: str, items: list, layers=None) -> Path:
    root = [
        "kicad_pcb",
        ["version", "20240108"],
        ["generator", sexp.qstr("pcb-gate-tests")],
        ["layers", *(layers or DEFAULT_LAYERS)],
        *items,
    ]
    path = tmp_path / f"{name}.kicad_pcb"
    sexp.dump_file(path, root)
    return path


def make_project(
    tmp_path: Path,
    name: str = "TestBoard",
    items: list | None = None,
    layers=None,
    netclasses=None,
    rules=None,
    severities=None,
    erc_severities=None,
    exclusions=None,
    with_capability: bool = True,
    capability_overrides: dict | None = None,
):
    project_dir = tmp_path / name
    project_dir.mkdir()
    make_kicad_pro(
        project_dir,
        name=name,
        netclasses=netclasses,
        rules=rules,
        severities=severities,
        erc_severities=erc_severities,
        exclusions=exclusions,
    )
    make_kicad_pcb(project_dir, name, items or [], layers=layers)
    (project_dir / f"{name}.kicad_sch").write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")
    if with_capability:
        make_capability_yml(project_dir, **(capability_overrides or {}))
    return discover(project_dir)


@pytest.fixture
def project_factory(tmp_path):
    def _make(**kwargs):
        return make_project(tmp_path, **kwargs)

    return _make
