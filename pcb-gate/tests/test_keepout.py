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
