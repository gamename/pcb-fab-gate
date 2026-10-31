import pytest

from pcb_gate import capability, viainpad
from tests.conftest import footprint, pad, via

MASK_F = ("F.Cu", "F.Mask")
MASK_B = ("B.Cu", "B.Mask")


def _smd(ref="U1", number="1", net="/A", at=(0, 0), size=(1, 1), layers=MASK_F, angle=None):
    node = pad(number, "smd", "rect", (0, 0), size, net, f"p-{ref}-{number}", layers=layers)
    return footprint(ref, at, [node], angle)


def _declare(ref="U2", number='"17"', vias=1, reason="exposed-pad ground via, plugged at the fab"):
    return {"declared_vias_in_pads": [{"ref": ref, "pad": number, "vias": vias, "reason": reason}]}


def _codes(report):
    return [v.code for v in report.violations]


def test_missing_capability_file_fails(project_factory):
    report = viainpad.run(project_factory(with_capability=False, items=[]))
    assert "missing_capability_file" in _codes(report)


def test_board_with_no_vias_passes_and_still_checked_something(project_factory):
    report = viainpad.run(project_factory(items=[_smd()]))
    assert report.ok
    assert report.checked


def test_same_net_via_centred_in_smd_pad_fails_with_ref_pad_net_and_coordinates(project_factory):
    files = project_factory(items=[_smd(ref="C7", number="2", net="/VBUS"), via((0.1, 0.2), 0.6, 0.3, "/VBUS", "v1")])
    report = viainpad.run(files)
    assert _codes(report) == [viainpad.VIOLATION_CODE]
    message = report.violations[0].message
    for expected in ("C7.2", "/VBUS", "(0.1, 0.2)", "F.Cu"):
        assert expected in message


def test_via_off_centre_with_drill_overlap_fails(project_factory):
    # Pad edge at x=0.5; centre at 0.6 leaves 0.1 < drill radius 0.15.
    report = viainpad.run(project_factory(items=[_smd(), via((0.6, 0), 0.6, 0.3, "/A", "v1")]))
    assert "drill" in report.violations[0].message


def test_via_with_only_annulus_overlap_fails(project_factory):
    # Centre at 0.7: 0.2 from the pad, past the drill radius, inside the 0.3 annulus radius.
    report = viainpad.run(project_factory(items=[_smd(), via((0.7, 0), 0.6, 0.3, "/A", "v1")]))
    assert "annulus" in report.violations[0].message


def test_via_exactly_tangent_to_pad_passes(project_factory):
    report = viainpad.run(project_factory(items=[_smd(), via((0.8, 0), 0.6, 0.3, "/A", "v1")]))
    assert report.ok, report.violations


def test_via_on_another_net_fails(project_factory):
    report = viainpad.run(project_factory(items=[_smd(net="/A"), via((0, 0), 0.6, 0.3, "/B", "v1")]))
    assert _codes(report) == [viainpad.VIOLATION_CODE]


def test_back_side_land_is_checked_on_its_own_layer(project_factory):
    report = viainpad.run(project_factory(items=[_smd(layers=MASK_B), via((0, 0), 0.6, 0.3, "/A", "v1")]))
    assert "B.Cu" in report.violations[0].message


def test_rotated_footprint_pad_is_checked_at_its_real_position(project_factory):
    # Same rotated position as test_overlap's SVW-0043 regression: (116,110) + rel (0,5.08) @ 90 = (121.08,110).
    fp = footprint(
        "U1", (116, 110), [pad("3", "smd", "rect", (0, 5.08), (1, 1), "/A", "p3", layers=MASK_F)], angle=90
    )
    assert not viainpad.run(project_factory(items=[fp, via((121.08, 110), 0.6, 0.3, "/A", "v1")])).ok


def test_rotated_footprint_phantom_position_is_clean(project_factory):
    fp = footprint(
        "U1", (116, 110), [pad("3", "smd", "rect", (0, 5.08), (1, 1), "/A", "p3", layers=MASK_F)], angle=90
    )
    report = viainpad.run(project_factory(items=[fp, via((110.92, 110), 0.6, 0.3, "/A", "v1")]))
    assert report.ok, report.violations


def test_through_hole_land_fails(project_factory):
    fp = footprint(
        "J1", (0, 0), [pad("1", "thru_hole", "circle", (0, 0), (2, 2), "/A", "p1", layers=("*.Cu", "*.Mask"))]
    )
    assert _codes(viainpad.run(project_factory(items=[fp, via((0.8, 0), 0.6, 0.3, "/A", "v1")]))) == ["via_in_pad"]


def test_edge_connector_land_fails(project_factory):
    fp = footprint("J1", (0, 0), [pad("1", "connect", "rect", (0, 0), (1, 1), "/A", "p1", layers=MASK_F)])
    assert not viainpad.run(project_factory(items=[fp, via((0, 0), 0.6, 0.3, "/A", "v1")])).ok


def test_paste_free_test_point_is_still_a_land(project_factory):
    report = viainpad.run(project_factory(items=[_smd(ref="TP1"), via((0, 0), 0.6, 0.3, "/A", "v1")]))
    assert not report.ok


def test_masked_copper_pad_is_not_a_land(project_factory):
    report = viainpad.run(project_factory(items=[_smd(layers=("F.Cu",)), via((0, 0), 0.6, 0.3, "/A", "v1")]))
    assert report.ok, report.violations


def test_np_thru_hole_has_no_land(project_factory):
    fp = footprint(
        "H1", (0, 0), [pad("MH", "np_thru_hole", "circle", (0, 0), (2, 2), "", "p1", layers=("F&B.Cu", "*.Mask"))]
    )
    report = viainpad.run(project_factory(items=[fp, via((0, 0), 0.6, 0.3, "/A", "v1")]))
    assert report.ok, report.violations


def test_via_missing_a_layer_is_not_compared_on_it(project_factory):
    report = viainpad.run(
        project_factory(items=[_smd(layers=MASK_B), via((0, 0), 0.3, 0.1, "/A", "v1", layers=("F.Cu", "In1.Cu"))])
    )
    assert report.ok, report.violations


def test_padstack_via_uses_its_largest_size(project_factory):
    padstack = via((0.7, 0), 0.2, 0.1, "/A", "v1")
    padstack.append(["padstack", ["layer", '"F.Cu"', ["size", "0.6"]]])
    assert not viainpad.run(project_factory(items=[_smd(), padstack])).ok


def test_via_with_no_size_is_a_blocking_skip_not_a_pass(project_factory):
    broken = [n for n in via((0, 0), 0.6, 0.3, "/A", "v1") if not (isinstance(n, list) and n[0] == "size")]
    report = viainpad.run(project_factory(items=[_smd(), broken]))
    assert not report.ok
    assert report.skipped_blocking


def _custom_pad(primitive, at=(0, 0), angle=None):
    # Real shape from mh2-pcb IC1: a small anchor plus a gr_rect arm.
    node = pad("1", "smd", "custom", at, (0.2, 0.2), "/A", "p1", layers=MASK_F, angle=angle)
    node.append(["primitives", primitive])
    return node


GR_RECT_ARM = ["gr_rect", ["start", "0", "-0.1"], ["end", "2", "0.1"], ["width", "0"], ["fill", "yes"]]


def test_custom_pad_primitive_is_part_of_the_land(project_factory):
    fp = footprint("IC1", (0, 0), [_custom_pad(GR_RECT_ARM)])
    report = viainpad.run(project_factory(items=[fp, via((1.8, 0), 0.3, 0.1, "/A", "v1")]))
    assert _codes(report) == ["via_in_pad"]


def test_custom_pad_primitive_rotates_with_the_pad(project_factory):
    # At a stored 90 deg the +x arm points up the screen, to -y in KiCad's y-down frame.
    fp = footprint("IC1", (0, 0), [_custom_pad(GR_RECT_ARM, angle=90)])
    assert not viainpad.run(project_factory(items=[fp, via((0, -1.8), 0.3, 0.1, "/A", "v1")])).ok


def test_custom_pad_primitive_rotated_away_is_clean(project_factory):
    fp = footprint("IC1", (0, 0), [_custom_pad(GR_RECT_ARM, angle=90)])
    report = viainpad.run(project_factory(items=[fp, via((1.8, 0), 0.3, 0.1, "/A", "v1")]))
    assert report.ok, report.violations


def test_custom_pad_with_an_unsupported_primitive_is_a_blocking_skip(project_factory):
    fp = footprint("IC1", (0, 0), [_custom_pad(["gr_curve", ["pts"]])])
    report = viainpad.run(project_factory(items=[fp]))
    assert not report.ok
    assert report.skipped_blocking


def test_through_hole_shape_offset_moves_the_land(project_factory):
    node = pad("1", "thru_hole", "rect", (0, 0), (1, 1), "/A", "p1", layers=("*.Cu", "*.Mask"))
    node.append(["drill", "0.3", ["offset", "3", "0"]])
    fp = footprint("J1", (0, 0), [node])
    assert not viainpad.run(project_factory(items=[fp, via((3, 0), 0.3, 0.1, "/A", "v1")])).ok


def test_split_pad_counts_each_via_once(project_factory):
    fp = footprint(
        "U2",
        (0, 0),
        [
            pad("17", "smd", "rect", (-0.25, 0), (0.5, 1), "/GND", "p1", layers=MASK_F),
            pad("17", "smd", "rect", (0.25, 0), (0.5, 1), "/GND", "p2", layers=MASK_F),
        ],
    )
    files = project_factory(items=[fp, via((0, 0), 0.6, 0.3, "/GND", "v1")], capability_overrides=_declare())
    report = viainpad.run(files)
    assert report.ok, report.violations


def test_declared_via_passes_and_is_printed(project_factory):
    files = project_factory(
        items=[_smd(ref="U2", number="17", net="/GND"), via((0, 0), 0.6, 0.3, "/GND", "v1")],
        capability_overrides=_declare(),
    )
    report = viainpad.run(files)
    assert report.ok, report.violations
    assert any("declared via in pad" in c and "U2.17" in c for c in report.checked)


def test_unquoted_yaml_pad_number_still_matches(project_factory):
    files = project_factory(
        items=[_smd(ref="U2", number="17", net="/GND"), via((0, 0), 0.6, 0.3, "/GND", "v1")],
        capability_overrides=_declare(number="17"),
    )
    assert viainpad.run(files).ok


def test_declaring_one_pad_does_not_cover_another(project_factory):
    files = project_factory(
        items=[
            _smd(ref="U2", number="17", net="/GND"),
            via((0, 0), 0.6, 0.3, "/GND", "v1"),
            _smd(ref="U3", number="1", net="/GND", at=(10, 10)),
            via((10, 10), 0.6, 0.3, "/GND", "v2"),
        ],
        capability_overrides=_declare(),
    )
    report = viainpad.run(files)
    assert _codes(report) == ["via_in_pad"]
    assert "U3.1" in report.violations[0].message


def test_more_vias_than_declared_fails(project_factory):
    files = project_factory(
        items=[
            _smd(ref="U2", number="17", net="/GND", size=(3, 3)),
            via((-1, 0), 0.6, 0.3, "/GND", "v1"),
            via((1, 0), 0.6, 0.3, "/GND", "v2"),
        ],
        capability_overrides=_declare(),
    )
    assert _codes(viainpad.run(files)) == ["via_in_pad", "via_in_pad"]


def test_declared_via_on_another_net_fails(project_factory):
    files = project_factory(
        items=[_smd(ref="U2", number="17", net="/GND"), via((0, 0), 0.6, 0.3, "/SIG", "v1")],
        capability_overrides=_declare(),
    )
    assert _codes(viainpad.run(files)) == ["via_in_pad"]


def test_stale_declaration_fails(project_factory):
    files = project_factory(items=[_smd(ref="U2", number="17")], capability_overrides=_declare())
    assert _codes(viainpad.run(files)) == ["stale_via_in_pad_declaration"]


def test_declaration_for_a_pad_not_on_the_board_fails(project_factory):
    assert _codes(viainpad.run(project_factory(items=[], capability_overrides=_declare()))) == [
        "stale_via_in_pad_declaration"
    ]


@pytest.mark.parametrize("reason", ["TBD", "n/a", "short"])
def test_placeholder_reason_fails(project_factory, reason):
    files = project_factory(
        items=[_smd(ref="U2", number="17", net="/GND"), via((0, 0), 0.6, 0.3, "/GND", "v1")],
        capability_overrides=_declare(reason=reason),
    )
    assert "placeholder_via_in_pad_reason" in _codes(viainpad.run(files))


def test_duplicate_declaration_fails(project_factory):
    entry = {"ref": "U2", "pad": 17, "vias": 1, "reason": "exposed-pad ground via, plugged"}
    files = project_factory(
        items=[_smd(ref="U2", number="17", net="/GND"), via((0, 0), 0.6, 0.3, "/GND", "v1")],
        capability_overrides={"declared_vias_in_pads": [entry, entry]},
    )
    assert "duplicate_via_in_pad_declaration" in _codes(viainpad.run(files))


@pytest.mark.parametrize("field,value", [("vias", 0), ("vias", "one"), ("vias", True)])
def test_declaration_count_must_be_a_positive_integer(tmp_path, field, value):
    path = tmp_path / "pcb-capability.yml"
    path.write_text(
        "fab: F\nretrieved: 2026-07-01\nsource: s\nstackup: x\nconstraints:\n"
        "  min_track_width_mm: 0.1\n  min_clearance_mm: 0.1\n  min_annular_ring_mm: 0.1\n  min_drill_mm: 0.2\n"
        "  min_silk_line_width_mm: 0.1\n  min_silk_text_height_mm: 1\n  edge_clearance_mm: 0.3\n"
        f"declared_vias_in_pads:\n  - ref: U2\n    pad: 17\n    {field}: {value}\n    reason: long enough reason\n",
        encoding="utf-8",
    )
    with pytest.raises(capability.CapabilityError):
        capability.load(path)


def test_invalid_declaration_is_reported_as_a_failure(project_factory):
    files = project_factory(items=[], capability_overrides=_declare())
    text = files.capability_file.read_text(encoding="utf-8").replace("    vias: 1\n", "")
    files.capability_file.write_text(text, encoding="utf-8")
    assert not viainpad.run(files).ok
