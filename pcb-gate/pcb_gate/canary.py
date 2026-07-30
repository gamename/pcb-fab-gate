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

import json
import shutil
import tempfile
import uuid as uuid_mod
from pathlib import Path
from typing import Callable

from . import arming, keepout, kicad_tools, sexp
from . import netlist as netlist_mod
from .layers import pad_geometry
from .project import ProjectFiles, discover
from .report import Report

DrcRunner = Callable[[Path, Path], kicad_tools.DrcResult]
KeepoutChecker = Callable[[ProjectFiles], Report]
NetlistRunner = Callable[[ProjectFiles, bool], Report]
ArmRunner = Callable[[ProjectFiles], Report]


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


def _find_anchor_pad(root: sexp.Node, exclude_net: str | None) -> tuple[str, tuple[float, float]] | None:
    """A real pad's absolute position, on a net other than `exclude_net`, to anchor a
    canary short to actual circuitry (SVW-0042).

    Skips KiCad's own synthetic `unconnected-(...)` pad nets: a lone unconnected pad
    can't form a real short with anything else on that "net" and isn't a faithful
    canary of the defect this checker exists to catch.
    """
    for footprint in sexp.children(root, "footprint"):
        for pad in sexp.children(footprint, "pad"):
            net_name = sexp.text_of(sexp.child(pad, "net"))
            if not net_name or net_name == exclude_net or net_name.startswith("unconnected-("):
                continue
            geom = pad_geometry(footprint, pad)
            if geom is None:
                continue
            centroid = geom.centroid
            return net_name, (centroid.x, centroid.y)
    return None


def inject_short(root: sexp.Node) -> bool:
    """Canary 1: duplicate an existing track, overlapping the original at one end and
    anchored to a real pad on a different net at the other.

    Originally both endpoints were just offset from the original segment, leaving the
    duplicate connected to nothing at either end. Confirmed on a real board (SVW-0042,
    gni-clock-pcb PR #1, KiCad 10.0.1): `kicad-cli pcb drc` does not evaluate
    clearance/shorting for a track segment with no connection at either end, against
    *any* other net, regardless of offset or overlap - so that version's injected
    defect was silently invisible to DRC. Anchoring one endpoint to a real pad on the
    target net makes the injected copper an actual short (touching live circuitry on
    both nets), which DRC does report as `shorting_items` - verified directly against
    the same real board before this fix landed.
    """
    segments = list(sexp.children(root, "segment"))
    if not segments:
        return False
    seg = segments[0]
    orig_net = sexp.text_of(sexp.child(seg, "net"))

    anchor = _find_anchor_pad(root, exclude_net=orig_net)
    if anchor is None:
        return False
    target_net, (anchor_x, anchor_y) = anchor

    width_node = sexp.child(seg, "width")
    width = sexp.as_float(width_node[1]) if width_node else 0.2
    offset = max(width * 0.25, 0.02)

    import copy as copy_mod

    dup = copy_mod.deepcopy(seg)
    start = sexp.child(dup, "start")
    start[1] = str(sexp.as_float(start[1]) + offset)
    start[2] = str(sexp.as_float(start[2]) + offset)
    end = sexp.child(dup, "end")
    end[1] = str(anchor_x)
    end[2] = str(anchor_y)
    sexp.child(dup, "net")[1] = sexp.qstr(target_net)
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


def inject_lock_pin_move(lock: dict) -> bool:
    """Canary (RULE 16.4): move one node from one net to another, in a lock file.

    Mutates the committed `connectivity.lock.json` copy rather than the schematic
    itself. `pcb-gate netlist`'s verify path is a symmetric diff between "the committed
    lock" and "a fresh regeneration from the schematic" - it cannot tell which side
    changed, so tampering with the committed side exercises exactly the same
    comparison code a real schematic edit would, without needing to hand-edit wire
    topology/labels in a `.kicad_sch` (nets there are derived from wire connectivity,
    not a per-pin attribute like `.kicad_pcb`'s `(net ...)`, so there is no equivalently
    simple single-token edit on that side).
    """
    nets = lock.get("nets", [])
    donor = next((n for n in nets if n.get("nodes")), None)
    if donor is None:
        return False
    recipient = next((n for n in nets if n["name"] != donor["name"]), None)
    if recipient is None:
        return False
    moved_node = donor["nodes"].pop(0)
    recipient["nodes"].append(moved_node)
    recipient["nodes"].sort(key=netlist_mod.natural_key)
    return True


def inject_undeclared_ignore_severity(pro: dict) -> str | None:
    """The arm-change canary (SVW-0038): downgrade one DRC severity to 'ignore'
    without declaring it in pcb-capability.yml, and return which key was changed."""
    severities = ((pro.get("board") or {}).get("design_settings") or {}).get("rule_severities") or {}
    for key, value in severities.items():
        if value != "ignore":
            severities[key] = "ignore"
            return key
    return None


def _run_netlist_canary(report: Report, files: ProjectFiles, netlist_runner: NetlistRunner) -> None:
    key, description = "netlist", "move one node from one net to another in the committed connectivity lock"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-netlist-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        baseline = netlist_runner(copy_files, True)
        lock_path = copy_files.project_dir / netlist_mod.LOCK_FILENAME
        if not baseline.ok or not lock_path.is_file():
            report.skip(
                f"canary '{key}': could not generate a baseline {netlist_mod.LOCK_FILENAME} "
                "(kicad-cli unavailable or export failed) - the netlist canary cannot run",
                blocking=True,
            )
            return

        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        injected = inject_lock_pin_move(lock)
        if not injected:
            report.skip(f"canary '{key}' ({description}): fewer than two nets with nodes on this board")
            return
        lock_path.write_text(netlist_mod.to_lock_text(lock), encoding="utf-8")

        report.check(f"canary '{key}': {description}")
        result = netlist_runner(copy_files, False)
        if result.ok:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}) came back clean - the connectivity regression check is broken.",
            )
        else:
            report.check(f"canary '{key}' fired as expected (netlist reported a connectivity regression)")


def _run_arm_canary(report: Report, files: ProjectFiles, arm_runner: ArmRunner) -> None:
    key, description = "undeclared_severity", "downgrade one DRC severity to 'ignore' without declaring it"
    with tempfile.TemporaryDirectory(prefix="pcb-gate-canary-arm-") as tmp:
        copy_files = _copy_project(files, Path(tmp) / "project")
        pro = json.loads(copy_files.pro_file.read_text(encoding="utf-8"))
        changed_key = inject_undeclared_ignore_severity(pro)
        if changed_key is None:
            report.skip(f"canary '{key}' ({description}): no DRC severity available to downgrade")
            return
        copy_files.pro_file.write_text(json.dumps(pro), encoding="utf-8")

        report.check(f"canary '{key}': {description} (key={changed_key!r})")
        arm_report = arm_runner(copy_files)
        if arm_report.ok:
            report.fail(
                "canary_did_not_fire",
                f"canary '{key}' ({description}, key={changed_key!r}) came back clean - "
                "the closed-world severity check (RULE 1.2) is broken.",
            )
        else:
            report.check(f"canary '{key}' fired as expected (arm reported an undeclared ignored severity)")


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
    netlist_runner: NetlistRunner = netlist_mod.run,
    arm_runner: ArmRunner = arming.run,
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
    _run_netlist_canary(report, files, netlist_runner)
    _run_arm_canary(report, files, arm_runner)

    return report
