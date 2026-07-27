from pathlib import Path

from pcb_gate import canary, keepout, kicad_tools, sexp
from pcb_gate.report import Report
from tests.conftest import footprint, pad, segment, via, zone_pour

ANT_KEEPOUT_SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


def _clean_drc_result() -> kicad_tools.DrcResult:
    return kicad_tools.DrcResult(
        exit_code=0, stdout="", stderr="", report={"violations": [], "unconnected_items": [], "schematic_parity": []}
    )


def _firing_drc_result() -> kicad_tools.DrcResult:
    return kicad_tools.DrcResult(
        exit_code=5,
        stdout="",
        stderr="",
        report={
            "violations": [{"type": "clearance"}, {"type": "track_width"}, {"type": "assertion_failure"}],
            "unconnected_items": [{"type": "unconnected_items"}],
            "schematic_parity": [],
        },
    )


def test_inject_short_requires_a_segment():
    root = ["kicad_pcb", ["layers"]]
    assert canary.inject_short(root) is False


def test_inject_short_adds_offset_segment_on_other_net():
    root = ["kicad_pcb", segment((0, 0), (5, 0), 0.2, "/A", "seg-1"), segment((10, 10), (15, 10), 0.2, "/B", "seg-2")]
    before = len(root) - 1
    assert canary.inject_short(root) is True
    assert len(root) - 1 == before + 1
    new_seg = root[-1]
    assert sexp.tag(new_seg) == "segment"
    assert sexp.text_of(sexp.child(new_seg, "net")) in ("/A", "/B")


def test_inject_narrow_track_sets_small_width():
    root = ["kicad_pcb", segment((0, 0), (5, 0), 0.2, "/A", "seg-1")]
    assert canary.inject_narrow_track(root) is True
    seg = next(sexp.children(root, "segment"))
    assert float(sexp.child(seg, "width")[1]) < 0.05


def test_inject_narrow_track_requires_a_segment():
    root = ["kicad_pcb"]
    assert canary.inject_narrow_track(root) is False


def test_inject_delete_connection_removes_a_segment():
    root = ["kicad_pcb", segment((0, 0), (5, 0), 0.2, "/A", "seg-1")]
    assert canary.inject_delete_connection(root) is True
    assert list(sexp.children(root, "segment")) == []


def test_inject_delete_connection_requires_a_segment():
    root = ["kicad_pcb"]
    assert canary.inject_delete_connection(root) is False


def test_inject_delete_connection_skips_a_net_with_a_zone_pour():
    # /GND has a filled zone pour - deleting its one segment wouldn't
    # actually break connectivity, so the canary must skip it and pick /A
    # instead (SVW-0036: confirmed as a false-negative on a real board).
    gnd_pour = zone_pour("/GND", "F.Cu", [(0, 0), (20, 0), (20, 20), (0, 20)], "pour-uuid")
    root = [
        "kicad_pcb",
        gnd_pour,
        segment((0, 0), (5, 0), 0.2, "/GND", "seg-gnd"),
        segment((10, 10), (15, 10), 0.2, "/A", "seg-a"),
    ]
    assert canary.inject_delete_connection(root) is True
    remaining_nets = {sexp.text_of(sexp.child(seg, "net")) for seg in sexp.children(root, "segment")}
    assert remaining_nets == {"/GND"}


def test_inject_delete_connection_removes_vias_on_the_same_net():
    root = [
        "kicad_pcb",
        segment((0, 0), (5, 0), 0.2, "/A", "seg-a"),
        via((5, 0), 0.6, 0.3, "/A", "via-a"),
        via((20, 20), 0.6, 0.3, "/B", "via-b"),
    ]
    assert canary.inject_delete_connection(root) is True
    remaining_vias = {sexp.text_of(sexp.child(v, "net")) for v in sexp.children(root, "via")}
    assert remaining_vias == {"/B"}


def test_inject_delete_connection_returns_false_when_every_net_has_a_pour():
    gnd_pour = zone_pour("/GND", "F.Cu", [(0, 0), (20, 0), (20, 20), (0, 20)], "pour-uuid")
    root = ["kicad_pcb", gnd_pour, segment((0, 0), (5, 0), 0.2, "/GND", "seg-gnd")]
    assert canary.inject_delete_connection(root) is False


def test_inject_via_in_keepout_requires_zone_and_net():
    zone = zone_pour("", "F.Cu", ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="ANT_KEEPOUT")
    root = ["kicad_pcb", zone]
    assert canary.inject_via_in_keepout(root, zone) is False  # no nets present anywhere


def test_inject_via_in_keepout_places_via_at_centroid():
    zone = zone_pour("", "F.Cu", ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="ANT_KEEPOUT")
    root = ["kicad_pcb", zone, segment((0, 0), (1, 1), 0.2, "/GND", "seg-1", layer="B.Cu")]
    assert canary.inject_via_in_keepout(root, zone) is True
    vias = list(sexp.children(root, "via"))
    assert len(vias) == 1


def test_inject_dru_assertion_creates_file_when_absent(tmp_path):
    dru_file = tmp_path / "Board.kicad_dru"
    canary.inject_dru_assertion(dru_file)
    nodes = sexp.parse(dru_file.read_text())
    assert sexp.tag(nodes[0]) == "version"
    assert any(sexp.tag(n) == "rule" and sexp.text_of(n) == "canary" for n in nodes)


def test_inject_dru_assertion_appends_to_existing_file(tmp_path):
    dru_file = tmp_path / "Board.kicad_dru"
    dru_file.write_text('(version 1)\n\n(rule "existing" (constraint clearance (min 0.1mm)))\n', encoding="utf-8")
    canary.inject_dru_assertion(dru_file)
    nodes = sexp.parse(dru_file.read_text())
    rule_names = [sexp.text_of(n) for n in nodes if sexp.tag(n) == "rule"]
    assert "existing" in rule_names
    assert "canary" in rule_names


# --- SVW-0037 Defect 1: schematic-parity canary ----------------------------


def test_inject_parity_break_requires_a_footprint():
    root = ["kicad_pcb"]
    assert canary.inject_parity_break(root) is False


def test_inject_parity_break_removes_a_footprint():
    fp = footprint("U1", (5, 5), [pad(1, "smd", "rect", (0, 0), (1, 1), "/GND", "pad-1")])
    root = ["kicad_pcb", fp]
    assert canary.inject_parity_break(root) is True
    assert list(sexp.children(root, "footprint")) == []


def _firing_parity_drc_result() -> kicad_tools.DrcResult:
    # Real shape confirmed against kicad-cli 10.0 (SVW-0037, mansio-pcb spin1):
    # schematic_parity entries are flat violation dicts, not sheet-wrappers.
    return kicad_tools.DrcResult(
        exit_code=5,
        stdout="",
        stderr="",
        report={
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [
                {"type": "extra_footprint", "severity": "warning", "description": "Extra footprint", "items": []}
            ],
        },
    )


def test_parity_canary_fires_when_footprint_missing_from_pcb(project_factory):
    fp = footprint("U1", (5, 5), [pad(1, "smd", "rect", (0, 0), (1, 1), "/GND", "pad-1")])
    files = project_factory(items=[fp])

    def firing_parity_runner(pcb_file: Path, report_path: Path):
        return _firing_parity_drc_result()

    report = canary.run(
        files,
        drc_runner=lambda pcb, rpt: _firing_drc_result(),
        keepout_checker=lambda f: Report(tool="stub-keepout", project=f.base_name),
        parity_runner=firing_parity_runner,
    )
    assert report.ok, report.violations


def test_parity_canary_fails_the_gate_when_it_comes_back_clean(project_factory):
    fp = footprint("U1", (5, 5), [pad(1, "smd", "rect", (0, 0), (1, 1), "/GND", "pad-1")])
    files = project_factory(items=[fp])

    def clean_parity_runner(pcb_file: Path, report_path: Path):
        return _clean_drc_result()

    report = canary.run(
        files,
        drc_runner=lambda pcb, rpt: _firing_drc_result(),
        keepout_checker=lambda f: Report(tool="stub-keepout", project=f.base_name),
        parity_runner=clean_parity_runner,
    )
    assert not report.ok
    assert any(
        v.code == "canary_did_not_fire" and "parity" in v.message for v in report.violations
    )


def test_parity_canary_skips_benignly_when_no_footprint_on_board(project_factory):
    files = project_factory(items=[segment((0, 0), (5, 0), 0.2, "/A", "seg-1")])

    def unreachable_parity_runner(pcb_file: Path, report_path: Path):
        raise AssertionError("parity_runner should not be called when there's no footprint to remove")

    report = canary.run(
        files,
        drc_runner=lambda pcb, rpt: _firing_drc_result(),
        keepout_checker=lambda f: Report(tool="stub-keepout", project=f.base_name),
        parity_runner=unreachable_parity_runner,
    )
    assert report.ok, report.violations
    assert not report.skipped_blocking


def test_parity_canary_blocking_skip_when_schematic_missing(project_factory):
    fp = footprint("U1", (5, 5), [pad(1, "smd", "rect", (0, 0), (1, 1), "/GND", "pad-1")])
    files = project_factory(items=[fp])
    # `discover()` requires the .kicad_sch to exist, so an empty file is how
    # "present but unloadable" is representable here - a totally missing
    # schematic never reaches the parity canary at all (every canary's
    # _copy_project -> discover() fails first, project-wide, not a parity-
    # specific case this canary needs to handle).
    files.sch_file.write_text("", encoding="utf-8")

    def unreachable_parity_runner(pcb_file: Path, report_path: Path):
        raise AssertionError("parity_runner should not be called when the schematic can't be loaded")

    report = canary.run(
        files,
        drc_runner=lambda pcb, rpt: _firing_drc_result(),
        keepout_checker=lambda f: Report(tool="stub-keepout", project=f.base_name),
        parity_runner=unreachable_parity_runner,
    )
    assert not report.ok
    assert report.skipped_blocking
    assert any("schematic" in s.lower() for s in report.skipped_blocking)


def test_harness_fails_when_every_checker_is_stubbed_clean(project_factory):
    files = project_factory(
        items=[segment((0, 0), (5, 0), 0.2, "/A", "seg-1"), segment((10, 10), (15, 10), 0.2, "/B", "seg-2")]
    )

    def clean_drc_runner(pcb_file: Path, report_path: Path):
        return _clean_drc_result()

    def clean_keepout_checker(files):
        return Report(tool="stub-keepout", project=files.base_name)

    def unreachable_parity_runner(pcb_file: Path, report_path: Path):
        raise AssertionError("no footprint on this fixture - parity canary must skip, not call the runner")

    report = canary.run(
        files,
        drc_runner=clean_drc_runner,
        keepout_checker=clean_keepout_checker,
        parity_runner=unreachable_parity_runner,
    )

    assert not report.ok
    fire_failures = [v for v in report.violations if v.code == "canary_did_not_fire"]
    # short, track_width, unconnected, assertion - keepout canary is skipped (no ANT_KEEPOUT
    # zone) and parity canary is skipped (no footprint on this fixture to remove), neither
    # of which is a canary_did_not_fire failure - a legitimate "no eligible target" skip.
    assert len(fire_failures) == 4


def test_harness_passes_when_checkers_fire_as_expected(project_factory):
    files = project_factory(
        items=[segment((0, 0), (5, 0), 0.2, "/A", "seg-1"), segment((10, 10), (15, 10), 0.2, "/B", "seg-2")]
    )

    def working_drc_runner(pcb_file: Path, report_path: Path):
        return _firing_drc_result()

    report = canary.run(files, drc_runner=working_drc_runner, keepout_checker=keepout.run)

    assert report.ok, report.violations
