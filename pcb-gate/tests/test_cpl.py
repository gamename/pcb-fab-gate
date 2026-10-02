"""SVW-0070: JLCPCB CPL + BOM generation and the `pcb-gate cpl` check.

kicad-cli is not available in this repo's unit-test CI, so the two exporters
are monkeypatched with canned output in KiCad's real formats (captured from
kicad-cli 10.0.6); the real-board runs live in the PR description.
"""
import csv

import pytest

from pcb_gate import cpl, sexp
from tests.conftest import make_project

Q = sexp.qstr


def fp(ref, at, layer="F.Cu", attrs=("smd",), name="Lib:R_0603", angle=None):
    at_node = ["at", str(at[0]), str(at[1])]
    if angle is not None:
        at_node.append(str(angle))
    return [
        "footprint", Q(name), ["layer", Q(layer)], at_node,
        ["property", Q("Reference"), Q(ref)],
        ["attr", *attrs],
    ]  # fmt: skip


def pos_csv(rows):
    out = ["Ref,Val,Package,PosX,PosY,Rot,Side"]
    for ref, pkg, x, y, rot, side in rows:
        out.append(f'"{ref}","v","{pkg}",{x:.6f},{y:.6f},{rot:.6f},{side}')
    return "\n".join(out) + "\n"


def raw_bom(rows):
    return [{"Reference": r, "Value": v, "Footprint": f, "LCSC": l} for r, v, f, l in rows]


@pytest.fixture
def patched(monkeypatch, tmp_path):
    state = {"pos": "", "bom": []}

    def fake_pos(pcb, out, include_tht):
        state["include_tht"] = include_tht
        return state["pos"]

    monkeypatch.setattr(cpl, "export_pos", fake_pos)
    monkeypatch.setattr(cpl, "export_bom_raw", lambda sch, out: state["bom"])
    state["tmp"] = tmp_path
    return state


def project(tmp_path, items):
    return make_project(tmp_path, items=items)


def test_override_wraparound():
    ovs = [cpl.RotationOverride(__import__("re").compile("^SOT-23"), 270.0, "x")]
    assert cpl.apply_override("SOT-23-6", 180.0, ovs) == 90.0
    assert cpl.apply_override("SOT-23-6", 0.0, ovs) == 270.0
    assert cpl.apply_override("R_0603", 45.0, ovs) == 45.0
    neg = [cpl.RotationOverride(__import__("re").compile("."), -90.0, "x")]
    assert cpl.apply_override("A", 30.0, neg) == 300.0
    assert cpl.apply_override("A", -10.0, []) == 350.0


def test_overrides_file_is_empty_by_default():
    assert cpl.load_overrides() == []


def test_load_overrides_parses_rows(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("footprint_regex,degrees_offset,note\n^QFN-.*,90,preview showed 90 off\n# comment\n")
    (ov,) = cpl.load_overrides(p)
    assert ov.degrees == 90.0 and ov.pattern.search("QFN-16") and ov.note.startswith("preview")


def test_transform_pos_columns_layers_and_override():
    ovs = [cpl.RotationOverride(__import__("re").compile("^SOT"), 180.0, "")]
    rows = cpl.transform_pos(
        pos_csv([("U1", "SOT-23", 10, -20, 270, "top"), ("R10", "R_0603", 1, -2, 0, "bottom"), ("R2", "R_0603", 3, -4, 0, "top")]),
        ovs,
    )
    assert list(rows[0]) == cpl.CPL_HEADER
    assert [r["Designator"] for r in rows] == ["R2", "R10", "U1"]  # natural order
    assert rows[1]["Layer"] == "Bottom" and rows[2]["Layer"] == "Top"
    assert float(rows[2]["Rotation"]) == 90.0  # (270 + 180) % 360


def test_transform_pos_skips_excluded_refs():
    rows = cpl.transform_pos(pos_csv([("R1", "R", 0, 0, 0, "top"), ("TP1", "TP", 0, 0, 0, "top")]), [], {"TP1"})
    assert [r["Designator"] for r in rows] == ["R1"]


def test_build_bom_groups_and_scopes():
    raw = raw_bom(
        [
            ("R1", "10k", "Lib:R_0603", "C25804"),
            ("R2", "10k", "Lib:R_0603", "C25804"),
            ("R3", "10k", "Lib:R_0603", ""),  # same value, no LCSC -> its own line
            ("R4", "10k", "Lib:R_0603", "C25804"),  # out of scope (THT / not in CPL)
        ]
    )
    rows = cpl.build_bom(raw, {"R1", "R2", "R3"})
    assert rows == [
        {"Comment": "10k", "Designator": "R1,R2", "Footprint": "R_0603", "LCSC Part #": "C25804"},
        {"Comment": "10k", "Designator": "R3", "Footprint": "R_0603", "LCSC Part #": ""},
    ]


def test_independent_derivation_scope_and_conventions():
    root = ["kicad_pcb"] + [
        fp("R1", (10, 20), angle=90),
        fp("R2", (1, 2), layer="B.Cu", angle=-90),
        fp("R3", (0, 0), attrs=("smd", "dnp")),
        fp("R4", (0, 0), attrs=("smd", "exclude_from_pos_files")),
        fp("R5", (0, 0), attrs=("smd", "exclude_from_bom")),
        fp("J1", (5, 5), attrs=("through_hole",)),
    ]
    smd = {r["Designator"]: r for r in cpl.derive_cpl(root, False, [])}
    assert set(smd) == {"R1", "R2"}
    assert smd["R1"]["Mid Y"] == -20 and smd["R1"]["Rotation"] == 90
    assert smd["R2"]["Layer"] == "Bottom" and smd["R2"]["Rotation"] == 270
    assert set(r["Designator"] for r in cpl.derive_cpl(root, True, [])) == {"R1", "R2", "J1"}


def good_run(patched, tmp_path, extra_items=()):
    items = [fp("R1", (10, 20), angle=90), fp("R2", (30, 40)), *extra_items]
    files = project(tmp_path, items)
    patched["pos"] = pos_csv([("R1", "R_0603", 10, -20, 90, "top"), ("R2", "R_0603", 30, -40, 0, "top")])
    patched["bom"] = raw_bom([("R1", "10k", "L:R_0603", "C1"), ("R2", "10k", "L:R_0603", "")])
    return files


def test_run_passes_and_writes_outputs(patched, tmp_path):
    files = good_run(patched, tmp_path)
    report = cpl.run(files, out_dir=tmp_path / "out")
    assert report.ok, report.violations
    cpl_rows = list(csv.reader(open(tmp_path / "out" / cpl.CPL_OUT)))
    assert cpl_rows[0] == cpl.CPL_HEADER and len(cpl_rows) == 3
    bom = list(csv.DictReader(open(tmp_path / "out" / cpl.BOM_OUT)))
    assert list(bom[0]) == cpl.BOM_HEADER
    summary = __import__("json").load(open(tmp_path / "out" / cpl.SUMMARY_OUT))
    assert summary["note"] == "1 of 2 assembly lines have no LCSC number"


def test_run_fails_when_cpl_disagrees_with_board(patched, tmp_path):
    files = good_run(patched, tmp_path)
    patched["pos"] = pos_csv([("R1", "R_0603", 10.5, -20, 90, "top"), ("R2", "R_0603", 30, -40, 0, "top")])
    report = cpl.run(files, out_dir=tmp_path / "out")
    assert [v.code for v in report.violations] == ["cpl_position_mismatch"]


def test_rotation_and_layer_mismatch(patched, tmp_path):
    files = good_run(patched, tmp_path)
    patched["pos"] = pos_csv([("R1", "R_0603", 10, -20, 91, "bottom"), ("R2", "R_0603", 30, -40, 0, "top")])
    codes = {v.code for v in cpl.run(files, out_dir=tmp_path / "out").violations}
    assert codes == {"cpl_rotation_mismatch", "cpl_layer_mismatch"}


def test_rotation_compare_wraps_around_360():
    row = {"Designator": "R1", "Mid X": "0", "Mid Y": "0", "Layer": "Top", "Rotation": "359.95"}
    exp = {**row, "Rotation": 0.0, "Mid X": 0.0, "Mid Y": 0.0}
    bom = [{"Comment": "", "Designator": "R1", "Footprint": "", "LCSC Part #": ""}]
    assert cpl.compare([row], bom, [exp]) == []


def test_run_fails_when_placed_part_missing_from_cpl(patched, tmp_path):
    files = good_run(patched, tmp_path)
    patched["pos"] = pos_csv([("R1", "R_0603", 10, -20, 90, "top")])
    codes = {v.code for v in cpl.run(files, out_dir=tmp_path / "out").violations}
    assert "cpl_missing_placement" in codes


def test_run_passes_with_dnp_and_exclude_from_pos_parts_absent(patched, tmp_path):
    files = good_run(
        patched,
        tmp_path,
        extra_items=[fp("R9", (1, 1), attrs=("smd", "dnp")), fp("R8", (2, 2), attrs=("smd", "exclude_from_pos_files"))],
    )
    assert cpl.run(files, out_dir=tmp_path / "out").ok


def test_exclude_from_bom_footprint_dropped_from_cpl(patched, tmp_path):
    files = good_run(patched, tmp_path, extra_items=[fp("R7", (3, 3), attrs=("smd", "exclude_from_bom"))])
    patched["pos"] += pos_csv([("R7", "R_0603", 3, -3, 0, "top")]).splitlines()[1] + "\n"
    report = cpl.run(files, out_dir=tmp_path / "out")
    assert report.ok, report.violations
    assert "R7" not in (tmp_path / "out" / cpl.CPL_OUT).read_text()


def test_dropped_bom_row_fails_check():
    cpl_rows = [
        {"Designator": "R1", "Mid X": "0", "Mid Y": "0", "Layer": "Top", "Rotation": "0"},
        {"Designator": "R2", "Mid X": "1", "Mid Y": "0", "Layer": "Top", "Rotation": "0"},
    ]
    derived = [{**r, "Mid X": float(r["Mid X"]), "Mid Y": 0.0, "Rotation": 0.0} for r in cpl_rows]
    bom = [{"Comment": "", "Designator": "R1", "Footprint": "", "LCSC Part #": ""}]
    assert [c for c, _ in cpl.compare(cpl_rows, bom, derived)] == ["cpl_part_missing_from_bom"]


def test_canary_fires_both_failures(patched, tmp_path):
    files = good_run(patched, tmp_path)
    report = cpl.run(files, out_dir=tmp_path / "out")
    checked = " | ".join(report.checked)
    assert "moving R1" in checked and "dropping R1" in checked


def test_canary_reports_inert_comparison(monkeypatch, patched, tmp_path):
    files = good_run(patched, tmp_path)
    monkeypatch.setattr(cpl, "compare", lambda *a, **k: [])
    codes = [v.code for v in cpl.run(files, out_dir=tmp_path / "out").violations]
    assert codes == ["cpl_canary_inert", "cpl_canary_inert"]


def test_tht_only_board_defaults_to_empty_scope(patched, tmp_path):
    files = project(tmp_path, [fp("J1", (5, 5), attrs=("through_hole",))])
    patched["pos"] = pos_csv([])
    report = cpl.run(files, out_dir=tmp_path / "out")
    assert report.ok and report.checked and report.skipped
    assert patched["include_tht"] is False


def test_include_tht_flag_reaches_exporter(patched, tmp_path):
    files = project(tmp_path, [fp("J1", (5, 5), attrs=("through_hole",))])
    patched["pos"] = pos_csv([("J1", "Conn", 5, -5, 0, "top")])
    patched["bom"] = raw_bom([("J1", "Conn", "L:Conn", "")])
    assert cpl.run(files, out_dir=tmp_path / "out", include_tht=True).ok
    assert patched["include_tht"] is True
