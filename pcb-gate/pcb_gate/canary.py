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


def _net_has_zone_pour(root: sexp.Node, net_name: str) -> bool:
    for zone in sexp.children(root, "zone"):
        if sexp.child(zone, "keepout") is not None:
            continue  # a rule area carries no copper of its own
        if sexp.text_of(sexp.child(zone, "net")) == net_name:
            return True
    return False


def inject_delete_connection(root: sexp.Node) -> bool:
    """Canary 3: fully disconnect one net's routing, breaking its connectivity.

    Removing a single arbitrary segment doesn't guarantee `unconnected_items`
    fires: if that segment's net also carries a filled zone pour (e.g. GND)
    or a redundant parallel track, the net stays physically connected and the
    canary comes back clean even though nothing about the checker was
    exercised - a false-negative canary, confirmed on a real board with GND
    pours (SVW-0036 bb-pcb spin2). Instead, pick a net that is routed with
    track segments and carries no zone pour of its own, and remove every
    segment and via on that net - the resulting two-terminal break has no
    alternate copper path.
    """
    segments_by_net: dict[str, list[sexp.Node]] = {}
    for seg in sexp.children(root, "segment"):
        name = sexp.text_of(sexp.child(seg, "net"))
        if name:
            segments_by_net.setdefault(name, []).append(seg)

    target_net = next((name for name in segments_by_net if not _net_has_zone_pour(root, name)), None)
    if target_net is None:
        return False

    for seg in segments_by_net[target_net]:
        root.remove(seg)
    for via in list(sexp.children(root, "via")):
        if sexp.text_of(sexp.child(via, "net")) == target_net:
            root.remove(via)
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


def inject_parity_break(root: sexp.Node) -> bool:
    """Canary 6: delete a footprint from the board copy so it no longer matches the schematic.

    Defect 1: `--schematic-parity` was added to gate.yml's real DRC step
    without a canary to prove any board's checker can actually fire it -
    RULE 1.1 verbatim, "a check that cannot fail has not passed, it has
    abstained." A footprint present in the schematic but missing from the
    board is the simplest, most reliable parity break to inject: KiCad
    reports it as `missing_footprint` (confirmed against a real board's
    `rule_severities`, bb-pcb spin2) regardless of net wiring, so it doesn't
    share the false-negative failure mode `inject_delete_connection` had to
    work around (a net with a zone pour masking a deleted connection).
    """
    footprints = list(sexp.children(root, "footprint"))
    if not footprints:
        return False
    root.remove(footprints[0])
    return True


def default_parity_drc_runner(pcb_file: Path, report_path: Path) -> kicad_tools.DrcResult:
    """The parity canary's own DRC invocation - always forces `--schematic-parity` on.

    Kept separate from the `drc_runner` parameter (used by the short /
    track-width / unconnected canaries above) rather than adding a flag to
    that shared 2-arg callable: `drc_runner` is stubbed directly in tests
    (see test_canary.py's `clean_drc_runner`), and widening its signature
    would break every existing stub for a flag only this one canary needs.
    Same pattern as `keepout_checker` below - a distinct callable per
    distinct external-tool invocation shape.
    """
    return kicad_tools.run_drc(pcb_file, report_path, schematic_parity=True)


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


def _run_keepout_canary(
    report: Report, files: ProjectFiles, keepout_checker: KeepoutChecker, keepout_zone_names: list[str]
) -> None:
    key, description = "keepout", f"place a via inside a rule area named {', '.join(keepout_zone_names)}"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-keepout-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        root = sexp.parse_file(copy_files.pcb_file)
        zones = keepout.find_ant_keepout_zones(root, keepout_zone_names)
        if not zones:
            report.skip(f"canary '{key}': no rule area named any of {keepout_zone_names} on this board")
            return

        injected = inject_via_in_keepout(root, zones[0])
        if not injected:
            report.skip(
                f"canary '{key}': could not place a via inside the keepout area (degenerate polygon or no nets)"
            )
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


def _run_parity_canary(report: Report, files: ProjectFiles, parity_runner: DrcRunner) -> None:
    """Defect 1: prove `--schematic-parity` can actually fail before it's trusted.

    "No footprint to remove" is a legitimate, non-blocking skip - same as
    every other canary's "no eligible target on this board." A schematic
    that can't be loaded is different: the canary itself couldn't run, which
    per the brief must "skip loudly and fail the gate," not skip quietly
    (Report.skip(..., blocking=True) - Defect 2/3's skip-vs-pass fix).
    """
    key, description = "parity", "delete a footprint from the board copy so it no longer matches the schematic"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-parity-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        if not copy_files.sch_file.is_file() or not copy_files.sch_file.read_text(encoding="utf-8").strip():
            report.skip(
                f"canary '{key}': schematic file {copy_files.sch_file.name} could not be loaded - "
                "the parity canary cannot run",
                blocking=True,
            )
            return

        root = sexp.parse_file(copy_files.pcb_file)
        injected = inject_parity_break(root)
        if not injected:
            report.skip(f"canary '{key}' ({description}): no footprint on this board to remove")
            return
        sexp.dump_file(copy_files.pcb_file, root)

        report.check(f"canary '{key}': {description}")
        result = parity_runner(copy_files.pcb_file, copy_files.project_dir / f"canary_{key}.json")
        expected = {"missing_footprint", "extra_footprint", "net_conflict"}
        if result.has_violation_type(*expected):
            report.check(f"canary '{key}' fired as expected (one of {sorted(expected)})")
        else:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}) came back clean - expected one of "
                f"{sorted(expected)} in the DRC report. The gate is broken.",
            )


def run(
    files: ProjectFiles,
    drc_runner: DrcRunner = kicad_tools.run_drc,
    keepout_checker: KeepoutChecker | None = None,
    parity_runner: DrcRunner = default_parity_drc_runner,
    keepout_zone_names: list[str] | None = None,
) -> Report:
    report = Report(tool="pcb-gate canary", project=files.base_name)
    zone_names = list(keepout_zone_names) if keepout_zone_names else [keepout.KEEPOUT_ZONE_NAME]
    checker = keepout_checker or (lambda f: keepout.run(f, keepout_zone_names=zone_names))

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
    _run_keepout_canary(report, files, checker, zone_names)
    _run_dru_canary(report, files, drc_runner)
    _run_parity_canary(report, files, parity_runner)

    return report
