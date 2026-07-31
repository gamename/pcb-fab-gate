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

# SVW-0038 RULE 1.2 (replaces the SVW-0037 enumerated roster in full): an
# enumerated allowlist of severities that must equal "error" is precisely
# the shape SVW-0037 Defect 3 exploited - `items_not_allowed` was simply
# absent from the list, so it was never asserted, and a report that checked
# nothing printed identically to one that passed. RULE 1.2's closed-world
# replacement asserts the negative instead: read every DRC severity
# (board.design_settings.rule_severities) and every ERC severity
# (erc.rule_severities) actually present in the project, and fail on any
# that is 'ignore' unless explicitly declared in pcb-capability.yml's
# declared_severity_downgrades with a real reason. A tool that adds a new
# check next release is covered the day it ships, not the day someone
# notices it missing from a list.
REQUIRED_BOARD_RULE_FLOORS = ("min_clearance", "min_track_width", "min_connection")

# SVW-0038: reasons this short or matching one of these tokens (case-folded)
# are placeholders, not reviews - "the declaration is the review, and an
# unreviewed declaration is the hole this replaces." Length floor is
# deliberately generous (real reasons in the brief/README run 40+ chars);
# this only catches the "reason: TBD" class of non-answer.
_PLACEHOLDER_REASON_TOKENS = frozenset({"todo", "tbd", "n/a", "na", "reason", "...", "-", "fixme", "xxx", "tk"})
_MIN_REASON_LENGTH = 8


def _is_placeholder_reason(reason: str) -> bool:
    stripped = reason.strip()
    return len(stripped) < _MIN_REASON_LENGTH or stripped.casefold() in _PLACEHOLDER_REASON_TOKENS

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


def check_rule_severities(
    pro: dict, cap: capability_mod.Capability | None, report: Report
) -> None:
    """RULE 1.2 (SVW-0038): closed-world - nothing may be 'ignore' unless declared.

    Checks both severity dicts KiCad actually maintains: DRC's
    board.design_settings.rule_severities and ERC's top-level erc.rule_severities
    (confirmed against a real board, bb-pcb spin2, 2026-07-28 - these are separate
    dicts with disjoint key namespaces, not one shared table).
    """
    drc_severities = ((pro.get("board") or {}).get("design_settings") or {}).get("rule_severities") or {}
    erc_severities = (pro.get("erc") or {}).get("rule_severities") or {}
    declared = cap.declared_severity_downgrades_by_check() if cap else {}

    report.check(
        f"rule_severities closed-world check: {len(drc_severities)} DRC + {len(erc_severities)} ERC "
        "severit(y/ies) present - none may be 'ignore' unless declared in pcb-capability.yml"
    )

    for domain, severities in (("DRC", drc_severities), ("ERC", erc_severities)):
        for check_name in sorted(severities):
            value = severities[check_name]
            if value != "ignore":
                continue
            downgrade = declared.get(check_name)
            if downgrade is None:
                report.fail(
                    "undeclared_ignored_severity",
                    f"{domain} check '{check_name}' has severity 'ignore' and is not declared in "
                    "pcb-capability.yml declared_severity_downgrades - turn it on or declare it with a reason",
                )
                continue
            report.check(
                f"declared severity downgrade: {domain} '{check_name}' -> ignore ({downgrade.reason!r})"
            )
            if _is_placeholder_reason(downgrade.reason):
                report.fail(
                    "placeholder_severity_downgrade_reason",
                    f"declared_severity_downgrades entry for '{check_name}' has an empty or placeholder "
                    f"reason ({downgrade.reason!r}) - the declaration is the review",
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


def check_erc_exclusions(
    pro: dict, cap: capability_mod.Capability | None, report: Report
) -> None:
    """SVW-0043: same closed-world contract as check_drc_exclusions, for ERC.

    KiCad serializes erc.erc_exclusions as [marker_string, comment] pairs
    (observed on a real board, KiCad 10: the marker is
    "<check_name>|<x>|<y>|<uuid...>" and the comment is the reviewer's free
    text). Older/hand-edited projects may carry bare strings; both forms are
    handled, and an entry this parser can't decode is printed raw and FAILED,
    never swallowed.
    """
    exclusions = (pro.get("erc") or {}).get("erc_exclusions") or []
    report.check(f"erc.erc_exclusions: {len(exclusions)} entr(y/ies)")
    if not exclusions:
        return

    declared = cap.declared_exclusion_rules() if cap else set()
    for entry in exclusions:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            raw, comment = entry[0], (entry[1] if len(entry) > 1 and isinstance(entry[1], str) else "")
        elif isinstance(entry, str):
            raw, comment = entry, ""
        else:
            report.fail(
                "undecodable_erc_exclusion",
                f"erc_exclusions entry has an unrecognized shape: {entry!r}",
            )
            continue
        rule_name = _exclusion_rule_name(raw)
        report.check(f"ERC exclusion present: rule={rule_name!r} comment={comment!r} raw={raw!r}")
        if rule_name not in declared:
            report.fail(
                "undeclared_erc_exclusion",
                f"ERC exclusion for rule={rule_name!r} is not listed in pcb-capability.yml "
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

    # SVW-0038: the closed-world severity check and the exclusion check both
    # need the capability file's declarations, so load it before either -
    # a missing/invalid capability file still fails its own way
    # (missing_capability_file), and both downstream checks then see cap=None
    # and fail closed (nothing can be "declared" without it).
    cap = check_capability_file(files, pro, report, today=today)
    check_rule_severities(pro, cap, report)
    check_drc_exclusions(pro, cap, report)
    check_erc_exclusions(pro, cap, report)

    return report
