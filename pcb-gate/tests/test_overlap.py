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
