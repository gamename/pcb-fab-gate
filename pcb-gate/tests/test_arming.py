import datetime

from pcb_gate import arming

TODAY = datetime.date(2026, 7, 26)


def test_clean_project_is_armed(project_factory):
    files = project_factory()
    report = arming.run(files, today=TODAY)
    assert report.ok, report.violations


def test_zeroed_netclass_clearance_fails(project_factory):
    files = project_factory(netclasses=[{"name": "Default", "clearance": 0, "track_width": 0.2}])
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "zeroed_netclass_clearance" for v in report.violations)


def test_zeroed_board_rule_floor_fails(project_factory):
    files = project_factory(rules={"min_clearance": 0.0, "min_track_width": 0.2, "min_connection": 0.2})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "zeroed_board_rule_floor" for v in report.violations)


def test_warning_severity_does_not_fail(project_factory):
    """RULE 1.2 (SVW-0038) only bans 'ignore', not 'warning' - a stricter exact-'error'
    requirement was the enumerated allowlist this amendment replaced."""
    files = project_factory(
        severities={
            "clearance": "warning",
            "shorting_items": "error",
            "courtyards_overlap": "error",
            "unconnected_items": "error",
        }
    )
    report = arming.run(files, today=TODAY)
    assert report.ok, report.violations


def test_undeclared_ignored_drc_severity_fails(project_factory):
    files = project_factory(severities={"clearance": "error", "missing_courtyard": "ignore"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(
        v.code == "undeclared_ignored_severity" and "missing_courtyard" in v.message for v in report.violations
    )


def test_undeclared_ignored_erc_severity_fails(project_factory):
    files = project_factory(erc_severities={"footprint_filter": "ignore"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(
        v.code == "undeclared_ignored_severity" and "footprint_filter" in v.message for v in report.violations
    )


def test_declared_severity_downgrade_does_not_fail(project_factory):
    files = project_factory(
        severities={"clearance": "error", "missing_courtyard": "ignore"},
        capability_overrides={
            "declared_severity_downgrades": [
                {
                    "check": "missing_courtyard",
                    "severity": "ignore",
                    "reason": "carrier board has no courtyard artwork; see docs/board-notes.md",
                }
            ]
        },
    )
    report = arming.run(files, today=TODAY)
    assert report.ok, report.violations


def test_declared_severity_downgrade_with_placeholder_reason_fails(project_factory):
    files = project_factory(
        severities={"clearance": "error", "missing_courtyard": "ignore"},
        capability_overrides={
            "declared_severity_downgrades": [
                {"check": "missing_courtyard", "severity": "ignore", "reason": "TBD"}
            ]
        },
    )
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "placeholder_severity_downgrade_reason" for v in report.violations)


def test_missing_capability_file_fails(project_factory):
    files = project_factory(with_capability=False)
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "missing_capability_file" for v in report.violations)


def test_stale_capability_file_fails(project_factory):
    files = project_factory(capability_overrides={"retrieved": "2025-01-01"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "stale_capability_sheet" for v in report.violations)


def test_netclass_below_fab_constraint_fails(project_factory):
    files = project_factory(
        netclasses=[{"name": "Default", "clearance": 0.05, "track_width": 0.2}],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "netclass_below_fab_clearance" for v in report.violations)


def test_undeclared_exclusion_fails(project_factory):
    files = project_factory(exclusions=["unconnected_items|1|2|uuid-a|uuid-b"])
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(v.code == "undeclared_drc_exclusion" for v in report.violations)


def test_items_not_allowed_ignored_fails(project_factory):
    """SVW-0037 Defect 3: items_not_allowed (rule-area violations) must be armed too -
    now covered by the closed-world 'no ignore' check rather than a specific
    enumeration entry."""
    files = project_factory(severities={"clearance": "error", "items_not_allowed": "ignore"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(
        v.code == "undeclared_ignored_severity" and "items_not_allowed" in v.message for v in report.violations
    )


def test_dangling_track_or_via_ignored_fails(project_factory):
    """RULE 2.1: dangling tracks are a hard stop, same as unconnected_items - covered
    here as an undeclared 'ignore', same as any other DRC check."""
    files = project_factory(severities={"clearance": "error", "track_dangling": "ignore", "via_dangling": "ignore"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    codes_with_messages = [(v.code, v.message) for v in report.violations]
    assert any(
        code == "undeclared_ignored_severity" and "track_dangling" in msg for code, msg in codes_with_messages
    )
    assert any(
        code == "undeclared_ignored_severity" and "via_dangling" in msg for code, msg in codes_with_messages
    )


def test_missing_courtyard_ignored_fails(project_factory):
    """GATE 4: one of KiCad 10's five Ignore-by-default DRC checks - must be turned
    on or declared, per RULE 1.2's closed-world assertion."""
    files = project_factory(severities={"clearance": "error", "missing_courtyard": "ignore"})
    report = arming.run(files, today=TODAY)
    assert not report.ok
    assert any(
        v.code == "undeclared_ignored_severity" and "missing_courtyard" in v.message for v in report.violations
    )


def test_declared_exclusion_does_not_fail(project_factory):
    files = project_factory(
        exclusions=["unconnected_items|1|2|uuid-a|uuid-b"],
        capability_overrides={
            "declared_exclusions": [
                {"rule": "unconnected_items", "reason": "carrier-board DevKit GPIOs intentionally unconnected"}
            ]
        },
    )
    report = arming.run(files, today=TODAY)
    assert report.ok, report.violations


def _pro_with_erc_exclusion(comment="GNI-0288: documented safe-by-design pin_to_pin exclusion"):
    return {
        "erc": {
            "erc_exclusions": [
                ["pin_to_pin|431800|990600|uuid-a|uuid-b|/sheet|/sheet|/sheet", comment]
            ]
        }
    }


def test_undeclared_erc_exclusion_fails(project_factory, monkeypatch):
    from pcb_gate import arming, capability
    from pcb_gate.report import Report

    report = Report(tool="pcb-gate arm", project="t")
    cap = capability.Capability(
        fab="F", retrieved=__import__("datetime").date.today(), source="s", stackup="s",
        constraints={}, declared_exclusions=(), declared_severity_downgrades=(),
    )
    arming.check_erc_exclusions(_pro_with_erc_exclusion(), cap, report)
    assert any(v.code == "undeclared_erc_exclusion" for v in report.violations)


def test_declared_erc_exclusion_passes(project_factory):
    from pcb_gate import arming, capability
    from pcb_gate.report import Report

    report = Report(tool="pcb-gate arm", project="t")
    cap = capability.Capability(
        fab="F", retrieved=__import__("datetime").date.today(), source="s", stackup="s",
        constraints={},
        declared_exclusions=(capability.DeclaredExclusion(rule="pin_to_pin", reason="documented safe-by-design, see board notes"),),
        declared_severity_downgrades=(),
    )
    arming.check_erc_exclusions(_pro_with_erc_exclusion(), cap, report)
    assert not any(v.code.startswith("undeclared") for v in report.violations)


def test_bare_string_erc_exclusion_is_handled(project_factory):
    from pcb_gate import arming
    from pcb_gate.report import Report

    report = Report(tool="pcb-gate arm", project="t")
    pro = {"erc": {"erc_exclusions": ["pin_to_pin|1|2|u1|u2"]}}
    arming.check_erc_exclusions(pro, None, report)
    assert any(v.code == "undeclared_erc_exclusion" for v in report.violations)
