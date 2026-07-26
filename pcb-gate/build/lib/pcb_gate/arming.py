"""RULE 1.2 / GATE 2 arming assertions.

Runs before any other check (SVW-0034 §1: "A check that cannot fail has not
passed; it has abstained"). Confirms the project's own design-rule
configuration is capable of catching a defect before trusting anything else
this package reports.

Key paths below were verified against a real KiCad 10 `.kicad_pro`
(gni-ambient-sensor-pcb/hardware/GniAmbientSensorPcb/GniAmbientSensorPcb.kicad_pro,
2026-07-26) rather than assumed from the brief. One correction versus the
brief's draft: the board-level connection-width floor is keyed
`design_settings.rules.min_connection`, not `min_connection_width` (that
name doesn't exist in KiCad 10's project schema).
"""
from __future__ import annotations

import datetime

from . import capability as capability_mod
from .project import ProjectFiles, load_kicad_pro
from .report import Report

REQUIRED_ERROR_SEVERITIES = (
    "clearance",
    "shorting_items",
    "courtyards_overlap",
    "unconnected_items",
)

REQUIRED_BOARD_RULE_FLOORS = ("min_clearance", "min_track_width", "min_connection")

# KiCad's per-project drc_exclusions entries serialize as
# "<violation_settings_key>|<...position/uuid fields...>" (PCB_MARKER
# serialization). No reference board carries an exclusion yet to confirm this
# byte-for-byte, so treat the parsed rule name as best-effort and always print
# the raw string too - the arming check must never silently swallow an
# exclusion it can't fully decode.
def _exclusion_rule_name(raw: str) -> str:
    return raw.split("|", 1)[0]


def check_netclasses(pro: dict, report: Report) -> None:
    classes = (pro.get("net_settings") or {}).get("classes") or []
    if not classes:
        report.fail("no_netclasses", "net_settings.classes is empty - nothing to resolve clearance from")
        return
    for nc in classes:
        name = nc.get("name", "<unnamed>")
        clearance = nc.get("clearance")
        track_width = nc.get("track_width")
        report.check(f"netclass '{name}': clearance={clearance}, track_width={track_width}")
        if not clearance or clearance <= 0:
            report.fail(
                "zeroed_netclass_clearance",
                f"netclass '{name}' has clearance={clearance!r} (must be > 0)",
            )
        if not track_width or track_width <= 0:
            report.fail(
                "zeroed_netclass_track_width",
                f"netclass '{name}' has track_width={track_width!r} (must be > 0)",
            )


def check_board_rule_floors(pro: dict, report: Report) -> None:
    rules = ((pro.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    for key in REQUIRED_BOARD_RULE_FLOORS:
        value = rules.get(key)
        report.check(f"board.design_settings.rules.{key} = {value}")
        if value is None or value <= 0:
            report.fail(
                "zeroed_board_rule_floor",
                f"board.design_settings.rules.{key}={value!r} (must be > 0)",
            )


def check_rule_severities(pro: dict, report: Report) -> None:
    severities = ((pro.get("board") or {}).get("design_settings") or {}).get(
        "rule_severities"
    ) or {}
    for key in REQUIRED_ERROR_SEVERITIES:
        value = severities.get(key)
        report.check(f"rule_severities.{key} = {value!r}")
        if value != "error":
            report.fail(
                "downgraded_severity",
                f"rule_severities.{key}={value!r}, must be 'error'",
            )


def check_drc_exclusions(
    pro: dict, cap: capability_mod.Capability | None, report: Report
) -> None:
    exclusions = ((pro.get("board") or {}).get("design_settings") or {}).get(
        "drc_exclusions"
    ) or []
    report.check(f"board.design_settings.drc_exclusions: {len(exclusions)} entr(y/ies)")
    if not exclusions:
        return

    # Per RULE 2.2, an exclusion is a legitimate documented decision, not an
    # invisible one - so a *declared* exclusion is printed (never silent) but
    # not itself a failure. An *undeclared* one is what turns an exclusion
    # from a committed decision back into an invisible one, and that fails.
    declared = cap.declared_exclusion_rules() if cap else set()
    for raw in exclusions:
        rule_name = _exclusion_rule_name(raw)
        report.check(f"DRC exclusion present: rule={rule_name!r} raw={raw!r}")
        if rule_name not in declared:
            report.fail(
                "undeclared_drc_exclusion",
                f"exclusion for rule={rule_name!r} is not listed in pcb-capability.yml "
                "declared_exclusions - an exclusion must be a committed decision, not a silent one",
            )


def check_capability_file(
    files: ProjectFiles, pro: dict, report: Report, today: datetime.date | None = None
) -> capability_mod.Capability | None:
    today = today or datetime.date.today()
    try:
        cap = capability_mod.load(files.capability_file)
    except capability_mod.CapabilityError as exc:
        report.check(f"pcb-capability.yml present at {files.capability_file}")
        report.fail("missing_capability_file", str(exc))
        return None

    report.check(
        f"pcb-capability.yml: fab={cap.fab}, stackup={cap.stackup}, retrieved={cap.retrieved}"
    )
    age = cap.age_days(today)
    report.check(f"pcb-capability.yml retrieved {age} day(s) ago (limit {capability_mod.MAX_CAPABILITY_AGE_DAYS})")
    if age > capability_mod.MAX_CAPABILITY_AGE_DAYS:
        report.fail(
            "stale_capability_sheet",
            f"pcb-capability.yml retrieved {cap.retrieved.isoformat()} is {age} days old "
            f"(limit {capability_mod.MAX_CAPABILITY_AGE_DAYS}) - re-retrieve from the fab",
        )

    classes = (pro.get("net_settings") or {}).get("classes") or []
    min_clearance = cap.constraints["min_clearance_mm"]
    min_track_width = cap.constraints["min_track_width_mm"]
    for nc in classes:
        name = nc.get("name", "<unnamed>")
        clearance = nc.get("clearance") or 0
        track_width = nc.get("track_width") or 0
        report.check(
            f"netclass '{name}' vs fab constraints: "
            f"clearance {clearance} >= {min_clearance}, track_width {track_width} >= {min_track_width}"
        )
        if clearance < min_clearance:
            report.fail(
                "netclass_below_fab_clearance",
                f"netclass '{name}' clearance={clearance}mm is below the fab's "
                f"minimum clearance {min_clearance}mm ({cap.fab} / {cap.stackup})",
            )
        if track_width < min_track_width:
            report.fail(
                "netclass_below_fab_track_width",
                f"netclass '{name}' track_width={track_width}mm is below the fab's "
                f"minimum track width {min_track_width}mm ({cap.fab} / {cap.stackup})",
            )
    return cap


def run(files: ProjectFiles, today: datetime.date | None = None) -> Report:
    report = Report(tool="pcb-gate arm", project=files.base_name)
    pro = load_kicad_pro(files.pro_file)

    check_netclasses(pro, report)
    check_board_rule_floors(pro, report)
    check_rule_severities(pro, report)

    # The capability file gates the exclusion check below (an exclusion can
    # only be "declared" against a capability file that exists), so load it
    # first even though its own failures are independent.
    cap = check_capability_file(files, pro, report, today=today)
    check_drc_exclusions(pro, cap, report)

    return report
