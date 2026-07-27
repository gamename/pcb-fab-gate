from pcb_gate import kicad_tools


def test_all_violations_includes_flat_schematic_parity_entries():
    """Real kicad-cli 10.0 shape (SVW-0037, confirmed against mansio-pcb spin1):
    schematic_parity entries are flat violation dicts, not per-sheet wrappers with a
    nested `violations` list. The original assumption (never confirmed against a real
    firing) silently dropped every one of these - this locks the fix in.
    """
    result = kicad_tools.DrcResult(
        exit_code=5,
        stdout="",
        stderr="",
        report={
            "violations": [{"type": "clearance"}],
            "unconnected_items": [],
            "schematic_parity": [
                {"type": "extra_footprint", "severity": "warning", "description": "Extra footprint MH1"},
                {"type": "missing_footprint", "severity": "warning", "description": "Missing footprint MH2"},
            ],
        },
    )
    assert result.has_violation_type("extra_footprint")
    assert result.has_violation_type("missing_footprint")
    assert len(result.all_violations()) == 3


def test_all_violations_still_handles_a_nested_sheet_wrapper():
    """Defensive fallback for a schematic_parity entry that DOES nest a `violations` list."""
    result = kicad_tools.DrcResult(
        exit_code=5,
        stdout="",
        stderr="",
        report={
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [{"violations": [{"type": "net_conflict"}]}],
        },
    )
    assert result.has_violation_type("net_conflict")


def test_all_violations_with_no_parity_issues_is_empty():
    result = kicad_tools.DrcResult(
        exit_code=0, stdout="", stderr="", report={"violations": [], "unconnected_items": [], "schematic_parity": []}
    )
    assert result.all_violations() == []
    assert not result.has_violation_type("clearance")
