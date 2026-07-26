"""GATE 1 - canary discipline.

"A check that cannot fail has not passed; it has abstained" (SVW-0034 RULE
1.1). Every checker this package (or the existing ERC/DRC steps) relies on
gets a known defect injected into a throwaway copy of the board, and is
trusted only after it reports that defect. A canary that comes back clean
means the gate itself is broken - exit non-zero and say which one.

Never mutates the real project: every injection happens in a fresh
`tempfile.TemporaryDirectory()` copy, discarded when the check completes.
"""
from __future__ import annotations

import shutil
import tempfile
import uuid as uuid_mod
from pathlib import Path
from typing import Callable

from . import keepout, kicad_tools, sexp
from .project import ProjectFiles, discover
from .report import Report

DrcRunner = Callable[[Path, Path], kicad_tools.DrcResult]
KeepoutChecker = Callable[[ProjectFiles], Report]


def _copy_project(files: ProjectFiles, dest: Path) -> ProjectFiles:
    shutil.copytree(files.project_dir, dest)
    return discover(dest)


def _new_uuid() -> sexp.QStr:
    return sexp.qstr(str(uuid_mod.uuid4()))


def _all_net_names(root: sexp.Node) -> set[str]:
    names: set[str] = set()
    for tag in ("segment", "via"):
        for node in sexp.children(root, tag):
            name = sexp.text_of(sexp.child(node, "net"))
            if name:
                names.add(name)
    for footprint in sexp.children(root, "footprint"):
        for pad in sexp.children(footprint, "pad"):
            name = sexp.text_of(sexp.child(pad, "net"))
            if name:
                names.add(name)
    return names


def inject_short(root: sexp.Node) -> bool:
    """Canary 1: duplicate an existing track onto a different net, offset so it overlaps."""
    segments = list(sexp.children(root, "segment"))
    if not segments:
        return False
    seg = segments[0]
    orig_net = sexp.text_of(sexp.child(seg, "net"))
    other_nets = [n for n in _all_net_names(root) if n != orig_net]
    if not other_nets:
        return False

    width_node = sexp.child(seg, "width")
    width = sexp.as_float(width_node[1]) if width_node else 0.2
    offset = max(width * 0.25, 0.02)

    import copy as copy_mod

    dup = copy_mod.deepcopy(seg)
    for point_tag in ("start", "end"):
        point = sexp.child(dup, point_tag)
        point[1] = str(sexp.as_float(point[1]) + offset)
        point[2] = str(sexp.as_float(point[2]) + offset)
    sexp.child(dup, "net")[1] = sexp.qstr(other_nets[0])
    uuid_node = sexp.child(dup, "uuid")
    if uuid_node is not None:
        uuid_node[1] = _new_uuid()

    root.append(dup)
    return True


def inject_narrow_track(root: sexp.Node) -> bool:
    """Canary 2: narrow a track well below any plausible netclass minimum."""
    seg = next(iter(sexp.children(root, "segment")), None)
    if seg is None:
        return False
    width_node = sexp.child(seg, "width")
    if width_node is None:
        return False
    width_node[1] = "0.01"
    return True


def inject_delete_connection(root: sexp.Node) -> bool:
    """Canary 3: delete a track segment, breaking a net's connectivity."""
    segments = list(sexp.children(root, "segment"))
    if not segments:
        return False
    root.remove(segments[0])
    return True


def inject_via_in_keepout(root: sexp.Node, zone: sexp.Node) -> bool:
    """Canary 4: place a via at the centroid of the ANT_KEEPOUT polygon."""
    polys = keepout.zone_outline_polygons(zone)
    if not polys:
        return False
    centroid = polys[0].centroid
    net_names = _all_net_names(root)
    non_rf_net = next((n for n in net_names if n), None)
    if non_rf_net is None:
        return False

    via = [
        "via",
        ["at", str(centroid.x), str(centroid.y)],
        ["size", "0.6"],
        ["drill", "0.3"],
        ["layers", sexp.qstr("F.Cu"), sexp.qstr("B.Cu")],
        ["net", sexp.qstr(non_rf_net)],
        ["uuid", _new_uuid()],
    ]
    root.append(via)
    return True


def inject_dru_assertion(dru_file: Path) -> None:
    """Canary 5: append a deliberately-false (constraint assertion) rule to .kicad_dru."""
    canary_rule = [
        "rule",
        sexp.qstr("canary"),
        ["constraint", "assertion", sexp.qstr("false")],
        ["condition", sexp.qstr("A.Type == 'Pad'")],
    ]
    if dru_file.is_file():
        nodes = sexp.parse(dru_file.read_text(encoding="utf-8"))
    else:
        nodes = [["version", "1"]]
    nodes.append(canary_rule)
    dru_file.write_text("\n\n".join(sexp.dumps(n) for n in nodes) + "\n", encoding="utf-8")


def _run_drc_canary(
    report: Report,
    files: ProjectFiles,
    key: str,
    description: str,
    inject,
    expected_types: set[str],
    drc_runner: DrcRunner,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"pcb-gate-canary-{key}-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        root = sexp.parse_file(copy_files.pcb_file)
        injected = inject(root)
        if not injected:
            report.skip(f"canary '{key}' ({description}): no eligible target on this board")
            return
        sexp.dump_file(copy_files.pcb_file, root)

        report.check(f"canary '{key}': {description}")
        result = drc_runner(copy_files.pcb_file, copy_files.project_dir / f"canary_{key}.json")
        if result.has_violation_type(*expected_types):
            report.check(f"canary '{key}' fired as expected (one of {sorted(expected_types)})")
        else:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}) came back clean - expected one of "
                f"{sorted(expected_types)} in the DRC report. The gate is broken.",
            )


def _run_dru_canary(report: Report, files: ProjectFiles, drc_runner: DrcRunner) -> None:
    key, description = "assertion", "falsify a (constraint assertion) rule in .kicad_dru"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-assertion-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        inject_dru_assertion(copy_files.dru_file)

        report.check(f"canary '{key}': {description}")
        result = drc_runner(copy_files.pcb_file, copy_files.project_dir / f"canary_{key}.json")
        expected = {"assertion_failure"}
        if result.has_violation_type(*expected):
            report.check(f"canary '{key}' fired as expected ({sorted(expected)})")
        else:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}) came back clean - expected 'assertion_failure' "
                "in the DRC report. The gate is broken.",
            )


def _run_keepout_canary(report: Report, files: ProjectFiles, keepout_checker: KeepoutChecker) -> None:
    key, description = "keepout", "place a via inside the ANT_KEEPOUT rule area"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-keepout-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        root = sexp.parse_file(copy_files.pcb_file)
        zones = keepout.find_ant_keepout_zones(root)
        if not zones:
            report.skip(f"canary '{key}': no ANT_KEEPOUT rule area on this board")
            return

        injected = inject_via_in_keepout(root, zones[0])
        if not injected:
            report.skip(f"canary '{key}': could not place a via inside ANT_KEEPOUT (degenerate polygon or no nets)")
            return
        sexp.dump_file(copy_files.pcb_file, root)

        report.check(f"canary '{key}': {description}")
        keepout_report = keepout_checker(copy_files)
        if keepout_report.ok:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}) came back clean - the keepout checker is broken.",
            )
        else:
            report.check(f"canary '{key}' fired as expected (keepout checker reported a violation)")


def run(
    files: ProjectFiles,
    drc_runner: DrcRunner = kicad_tools.run_drc,
    keepout_checker: KeepoutChecker = keepout.run,
) -> Report:
    report = Report(tool="pcb-gate canary", project=files.base_name)

    _run_drc_canary(
        report, files, "short", "duplicate a track onto a different net, offset to overlap",
        inject_short, {"clearance", "shorting_items"}, drc_runner,
    )
    _run_drc_canary(
        report, files, "track_width", "narrow a track below the netclass minimum",
        inject_narrow_track, {"track_width"}, drc_runner,
    )
    _run_drc_canary(
        report, files, "unconnected", "delete a track segment",
        inject_delete_connection, {"unconnected_items"}, drc_runner,
    )
    _run_keepout_canary(report, files, keepout_checker)
    _run_dru_canary(report, files, drc_runner)

    return report
