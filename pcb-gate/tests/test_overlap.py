from pcb_gate import overlap
from tests.conftest import footprint, pad, segment


def test_missing_capability_file_fails(project_factory):
    files = project_factory(with_capability=False, items=[])
    report = overlap.run(files)
    assert not report.ok
    assert any(v.code == "missing_capability_file" for v in report.violations)


def test_different_net_too_close_fails(project_factory):
    files = project_factory(
        items=[
            segment((0, 0), (5, 0), 0.2, "/A", "seg-a"),
            segment((0, 0.21), (5, 0.21), 0.2, "/B", "seg-b"),
        ],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    # gap between edges = 0.21 - (0.1+0.1) = 0.01mm < 0.1mm clearance
    report = overlap.run(files)
    assert not report.ok
    assert any(v.code == "overlap_clearance" for v in report.violations)


def test_different_net_with_enough_clearance_passes(project_factory):
    files = project_factory(
        items=[
            segment((0, 0), (5, 0), 0.2, "/A", "seg-a"),
            segment((0, 1), (5, 1), 0.2, "/B", "seg-b"),
        ],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert report.ok, report.violations


def test_same_net_overlap_is_not_a_violation(project_factory):
    files = project_factory(
        items=[
            segment((0, 0), (5, 0), 0.2, "/A", "seg-a"),
            segment((0, 0), (5, 0), 0.2, "/A", "seg-b"),
        ],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert report.ok, report.violations


def test_circular_pad_not_falsely_flagged_near_bbox_corner(project_factory):
    # A 2.4mm-diameter circular THT pad at the origin has radius 1.2mm, but its
    # square bounding box extends to its corners at (+-1.2, +-1.2) - a point near
    # a corner, e.g. (1.15, 1.15), sits INSIDE the bounding square (|x|,|y| < 1.2)
    # but OUTSIDE the true circle (1.15^2 + 1.15^2 ~= 2.645 > 1.2^2 = 1.44), a good
    # 0.4mm+ clear of the real pad edge. A rectangular-bbox approximation would
    # wrongly call this a direct hit; this is the exact false positive found on a
    # real board (gni-ambient-sensor-pcb, J1 pad 4, 2026-07-26) before pad shape
    # awareness was added to layers.py.
    fp = footprint("J1", (0, 0), [pad(4, "thru_hole", "circle", (0, 0), (2.4, 2.4), "/SCL", "pad-4")])
    files = project_factory(
        items=[fp, segment((1.1, 1.1), (1.2, 1.2), 0.02, "/GND", "seg-1")],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert report.ok, report.violations


def test_pad_vs_pad_different_net_too_close_fails(project_factory):
    fp1 = footprint("R1", (0, 0), [pad(1, "smd", "rect", (0, 0), (1, 1), "/A", "pad-1")])
    fp2 = footprint("R2", (1.05, 0), [pad(1, "smd", "rect", (0, 0), (1, 1), "/B", "pad-2")])
    files = project_factory(items=[fp1, fp2], capability_overrides={"min_clearance_mm": 0.1})
    report = overlap.run(files)
    assert not report.ok
    assert any(v.code == "overlap_clearance" for v in report.violations)


def test_rotated_footprint_pad_checked_at_real_position_not_phantom(project_factory):
    # SVW-0043 regression: a footprint at 90 deg has its pad offset rotated
    # y-down, so pad 3 of fp (116,110) rel (0,5.08) lives at (121.08,110).
    # The pre-fix +angle rotation evaluated it at the mirrored phantom
    # (110.92,110). A segment hugging the PHANTOM position must NOT flag...
    files = project_factory(
        items=[
            footprint(
                "U1",
                (116, 110),
                [pad("3", "thru_hole", "circle", (0, 5.08), (1.2, 1.2), "/RST", "pu3")],
                angle=90,
            ),
            segment((110.9, 105), (110.9, 115), 0.4, "/OTHER", "seg-phantom"),
        ],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert report.ok, [v.message for v in report.violations]


def test_rotated_footprint_pad_conflict_at_real_position_fails(project_factory):
    # ...and a segment crossing the REAL position (121.08,110) must flag.
    files = project_factory(
        items=[
            footprint(
                "U1",
                (116, 110),
                [pad("3", "thru_hole", "circle", (0, 5.08), (1.2, 1.2), "/RST", "pu3")],
                angle=90,
            ),
            segment((121.08, 105), (121.08, 115), 0.4, "/OTHER", "seg-real"),
        ],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert not report.ok
    assert any(v.code == "overlap_clearance" for v in report.violations)


def test_roundrect_pad_bbox_corner_not_falsely_flagged(project_factory):
    # The exact class of false positive found on mansio-pcb spin-2 (2026-07-31,
    # 24 items): a roundrect pad's bounding-box corner is NOT part of the
    # pad's real (rounded) copper. A 2x2mm pad at rratio 0.5 is a pure circle
    # of radius 1 (KiCad's own definition: radius = rratio * min(sx,sy));
    # its bbox corner at (1,1) is sqrt(2) ~= 1.414mm from center, i.e. ~0.41mm
    # clear of the true circular edge - comfortably inside the 0.1mm fab
    # floor. The pre-fix bounding-rect stand-in put pad copper AT (1,1)
    # exactly, so a different-net track anchored there read as a direct hit.
    fp = footprint("R11", (0, 0), [pad("1", "smd", "roundrect", (0, 0), (2, 2), "/A", "pad-1", roundrect_rratio=0.5)])
    files = project_factory(
        items=[fp, segment((1, 1), (2, 2), 0.02, "/B", "seg-corner")],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert report.ok, [v.message for v in report.violations]


def test_roundrect_pad_real_edge_still_flags(project_factory):
    # Companion to the above: the fix must not make roundrect pads
    # invisible to the checker. A track anchored well inside the true
    # circular edge (e.g. (0.5, 0.5), radius 1 from center) must still fail.
    fp = footprint("R11", (0, 0), [pad("1", "smd", "roundrect", (0, 0), (2, 2), "/A", "pad-1", roundrect_rratio=0.5)])
    files = project_factory(
        items=[fp, segment((0.5, 0.5), (2, 2), 0.02, "/B", "seg-inside")],
        capability_overrides={"min_clearance_mm": 0.1},
    )
    report = overlap.run(files)
    assert not report.ok
    assert any(v.code == "overlap_clearance" for v in report.violations)
