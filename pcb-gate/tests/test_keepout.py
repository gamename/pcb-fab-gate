from pcb_gate import keepout
from tests.conftest import footprint, pad, segment, via, zone_pour

ANT_KEEPOUT_SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


def ant_keepout_zone(layer="F.Cu"):
    return zone_pour("", layer, ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="ANT_KEEPOUT")


def test_no_keepout_zone_is_skipped(project_factory):
    files = project_factory(items=[segment((0, 0), (5, 5), 0.2, "/GND", "seg-1")])
    report = keepout.run(files)
    assert report.ok
    assert report.skipped


def test_degenerate_keepout_zone_fails(project_factory):
    zone = ["zone", ["net", ""], ["layer", "F.Cu"], ["name", "ANT_KEEPOUT"], ["keepout", ["tracks", "not_allowed"]]]
    files = project_factory(items=[zone])
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "degenerate_keepout_zone" for v in report.violations)


def test_track_intrusion_detected(project_factory):
    files = project_factory(
        items=[ant_keepout_zone(), segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")]
    )
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_track_intrusion" for v in report.violations)


def test_via_intrusion_detected(project_factory):
    files = project_factory(items=[ant_keepout_zone(), via((5, 5), 0.6, 0.3, "/GND", "via-1")])
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_via_intrusion" for v in report.violations)


def test_pad_intrusion_detected(project_factory):
    fp = footprint("U1", (5, 5), [pad(1, "smd", "rect", (0, 0), (1, 1), "/GND", "pad-1")])
    files = project_factory(items=[ant_keepout_zone(), fp])
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_pad_intrusion" for v in report.violations)


def test_pour_intrusion_detected(project_factory):
    pour = zone_pour("/GND", "F.Cu", ANT_KEEPOUT_SQUARE, "pour-1")
    files = project_factory(items=[ant_keepout_zone(), pour])
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_pour_intrusion" for v in report.violations)


def test_rf_ant_netclass_is_allowed(project_factory):
    files = project_factory(
        items=[ant_keepout_zone(), segment((2, 2), (8, 8), 0.2, "/RF_FEED", "seg-1")],
        netclasses=[
            {"name": "Default", "clearance": 0.2, "track_width": 0.2},
            {"name": "RF_ANT", "clearance": 0.5, "track_width": 0.377},
        ],
    )
    # netclass_assignments lives in the .kicad_pro; project_factory doesn't expose it
    # directly, so patch it onto the already-written file.
    import json

    pro_path = files.pro_file
    data = json.loads(pro_path.read_text())
    data["net_settings"]["netclass_assignments"] = {"/RF_FEED": "RF_ANT"}
    pro_path.write_text(json.dumps(data))

    report = keepout.run(files)
    assert report.ok, report.violations


def test_track_outside_layer_scope_is_not_checked(project_factory):
    files = project_factory(
        items=[ant_keepout_zone(layer="F.Cu"), segment((2, 2), (8, 8), 0.2, "/GND", "seg-1", layer="B.Cu")]
    )
    report = keepout.run(files)
    assert report.ok, report.violations


# --- SVW-0037 Defect 2: configurable zone name + rf_board -----------------


def test_default_name_still_finds_ant_keepout(project_factory):
    """Backward compatibility: no override behaves exactly as before."""
    files = project_factory(
        items=[ant_keepout_zone(), segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")]
    )
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_track_intrusion" for v in report.violations)


def test_non_default_name_is_invisible_without_override(project_factory):
    """The exact defect this fixed: a real zone under a different name looked like 'nothing here'."""
    zone = zone_pour("", "F.Cu", ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="U1_Antenna_Keepout")
    files = project_factory(items=[zone, segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")])
    report = keepout.run(files)
    assert report.ok
    assert report.skipped
    assert not report.skipped_blocking


def test_configured_name_finds_non_default_zone(project_factory):
    zone = zone_pour("", "F.Cu", ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="U1_Antenna_Keepout")
    files = project_factory(items=[zone, segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")])
    report = keepout.run(files, keepout_zone_names=["U1_Antenna_Keepout"])
    assert not report.ok
    assert any(v.code == "keepout_track_intrusion" for v in report.violations)


def test_configured_name_accepts_a_list_of_multiple_names(project_factory):
    zone = zone_pour("", "F.Cu", ANT_KEEPOUT_SQUARE, "keepout-uuid", keepout=True, name="Analog_BCu_Keepout")
    files = project_factory(items=[zone, segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")])
    report = keepout.run(files, keepout_zone_names=["U1_Antenna_Keepout", "Analog_BCu_Keepout"])
    assert not report.ok
    assert any(v.code == "keepout_track_intrusion" for v in report.violations)


def test_rf_board_with_no_matching_zone_fails_not_skips(project_factory):
    files = project_factory(items=[segment((0, 0), (5, 5), 0.2, "/GND", "seg-1")])
    report = keepout.run(files, rf_board=True)
    assert not report.ok
    assert any(v.code == "missing_keepout_on_rf_board" for v in report.violations)


def test_non_rf_board_with_no_matching_zone_still_skips(project_factory):
    files = project_factory(items=[segment((0, 0), (5, 5), 0.2, "/GND", "seg-1")])
    report = keepout.run(files, rf_board=False)
    assert report.ok
    assert report.skipped
    assert not report.skipped_blocking


def test_keepout_zone_embedded_in_a_footprint_is_found(project_factory):
    """Real-board finding (SVW-0037 audit, mansio-pcb spin1 / helios-pcb): a library
    footprint (the Walter modem socket) ships its own keepout zone nested inside the
    footprint definition, not as a board-top-level zone. A direct-children-only search
    misses it entirely regardless of name configuration.
    """
    fp = footprint("U1", (5, 5), [ant_keepout_zone()])
    files = project_factory(items=[fp, segment((2, 2), (8, 8), 0.2, "/GND", "seg-1")])
    report = keepout.run(files)
    assert not report.ok
    assert any(v.code == "keepout_track_intrusion" for v in report.violations)


def test_rf_board_result_is_never_zero_checks(project_factory):
    """The RF-board determination itself is a check, so a skip here is never INCONCLUSIVE."""
    files = project_factory(items=[segment((0, 0), (5, 5), 0.2, "/GND", "seg-1")])
    report = keepout.run(files, rf_board=False)
    assert report.checked
    assert report.summarize() == 0
