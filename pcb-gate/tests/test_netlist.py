import json

import pytest

from pcb_gate import netlist, sexp
from pcb_gate.project import discover


def _export_root(components_sexp: str, nets_sexp: str) -> sexp.Node:
    text = f"(export (version \"E\") (design) (components {components_sexp}) (nets {nets_sexp}))"
    (root,) = sexp.parse(text)
    return root


# --- natural_key -------------------------------------------------------


def test_natural_key_orders_pins_numeric_aware():
    values = ["U1.10", "U1.2", "U1.1"]
    assert sorted(values, key=netlist.natural_key) == ["U1.1", "U1.2", "U1.10"]


def test_natural_key_orders_refdes_numeric_aware():
    values = ["C10", "C2", "C1"]
    assert sorted(values, key=netlist.natural_key) == ["C1", "C2", "C10"]


def test_natural_key_never_raises_on_mismatched_shapes():
    # Different chunk shapes ("ABC" has no digit run, "U1.2" does) must not
    # raise TypeError comparing int to str - the whole reason each chunk is
    # wrapped as (kind, value) rather than left as a bare int/str.
    values = ["ABC", "U1.2", "1", ""]
    assert sorted(values, key=netlist.natural_key)


# --- extracting components/nets from a parsed netlist export -----------


def test_extract_components_reads_property_form():
    root = _export_root(
        '(comp (ref "J1") (value "USB_C") (footprint "Connector:USB_C") '
        '(property (name "MPN") (value "HRO TYPE-C-31-M-12")))',
        "",
    )
    components = netlist._extract_components(root)
    assert components["J1"] == {"value": "USB_C", "footprint": "Connector:USB_C", "mpn": "HRO TYPE-C-31-M-12"}


def test_extract_components_falls_back_to_field_form():
    root = _export_root(
        '(comp (ref "J1") (value "USB_C") (footprint "Connector:USB_C") '
        '(fields (field (name "MPN") "HRO TYPE-C-31-M-12")))',
        "",
    )
    components = netlist._extract_components(root)
    assert components["J1"]["mpn"] == "HRO TYPE-C-31-M-12"


def test_extract_components_defaults_empty_mpn_when_absent():
    root = _export_root('(comp (ref "C1") (value "100nF") (footprint "Capacitor_SMD:C_0402"))', "")
    components = netlist._extract_components(root)
    assert components["C1"]["mpn"] == ""


def test_extract_nets_builds_refdes_dot_pin_nodes():
    root = _export_root(
        "",
        '(net (code "1") (name "GND") '
        '(node (ref "C1") (pin "1")) (node (ref "U1") (pin "10")) (node (ref "U1") (pin "2")))',
    )
    nets = netlist._extract_nets(root)
    assert set(nets["GND"]) == {"C1.1", "U1.10", "U1.2"}


# --- build_lock(): net ordering -----------------------------------------


def test_build_lock_sorts_nets_with_natural_key_not_lexicographic(tmp_path, monkeypatch):
    """Regression (SVW-0042): `build_lock()`'s own nets list must sort the same
    numeric-aware way its nodes already do. A plain `sorted(nets_raw.items())` sorts
    by the tuple's first element - i.e. lexicographically - putting
    'unconnected-(U1-Pad10)' before 'unconnected-(U1-Pad9)'. On a re-run, a freshly
    regenerated lock reorders these back to natural_key order and reports a byte-diff
    with no semantic difference: `connectivity_lock_churn`, on any board with more
    than 9 numbered unconnected pins on the same component - not a design change.
    """
    export_root = _export_root(
        components_sexp='(comp (ref "U1") (value "X") (footprint "F"))',
        nets_sexp=(
            '(net (code "1") (name "unconnected-(U1-Pad9)") (node (ref "U1") (pin "9"))) '
            '(net (code "2") (name "unconnected-(U1-Pad10)") (node (ref "U1") (pin "10")))'
        ),
    )
    monkeypatch.setattr(netlist, "_run_netlist_export", lambda sch_file: export_root)
    monkeypatch.setattr(netlist, "extract_dnp_by_ref", lambda sch_file: {})

    project = tmp_path / "TestBoard"
    project.mkdir()
    (project / "TestBoard.kicad_sch").write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")
    (project / "TestBoard.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    (project / "TestBoard.kicad_pro").write_text("{}", encoding="utf-8")
    files = discover(project)

    lock = netlist.build_lock(files)

    assert [n["name"] for n in lock["nets"]] == [
        "unconnected-(U1-Pad9)",
        "unconnected-(U1-Pad10)",
    ]


# --- dnp, sourced from the schematic, not the netlist -------------------


def test_extract_dnp_by_ref_reads_placed_instances_only(tmp_path):
    sch = tmp_path / "Board.kicad_sch"
    sch.write_text(
        "(kicad_sch (version 20231120)"
        '(lib_symbols (symbol "Device:R" (property "Reference" "R" (at 0 0 0))))'
        '(symbol (lib_id "Device:R") (at 0 0 0) (unit 1) (dnp yes) '
        '(property "Reference" "R1" (at 0 0 0)))'
        '(symbol (lib_id "Device:R") (at 0 0 0) (unit 1) (dnp no) '
        '(property "Reference" "R2" (at 0 0 0)))'
        ")",
        encoding="utf-8",
    )
    dnp_by_ref = netlist.extract_dnp_by_ref(sch)
    assert dnp_by_ref == {"R1": True, "R2": False}


# --- readable diff -------------------------------------------------------


def _lock(nets, components=()):
    return {
        "schema": 1,
        "nets": [{"name": n, "nodes": sorted(nodes, key=netlist.natural_key)} for n, nodes in nets.items()],
        "components": list(components),
    }


def test_diff_reports_a_moved_pin_on_one_line():
    old = _lock({"SDA": ["U3.7"], "SCL": []})
    new = _lock({"SDA": [], "SCL": ["U3.7"]})
    lines = netlist._diff(old, new)
    assert lines == ["U3.7 moved from net 'SDA' to net 'SCL'"]


def test_diff_reports_added_and_removed_nets():
    old = _lock({"GND": ["C1.1"]})
    new = _lock({"GND": ["C1.1"], "+3V3": ["C2.1"]})
    lines = netlist._diff(old, new)
    assert lines == ["net '+3V3' added with node(s): C2.1"]


def test_diff_reports_added_and_removed_nodes_within_a_net():
    old = _lock({"GND": ["C1.1", "C2.1"]})
    new = _lock({"GND": ["C1.1", "C3.1"]})
    lines = netlist._diff(old, new)
    assert "net 'GND': node(s) added: C3.1" in lines
    assert "net 'GND': node(s) removed: C2.1" in lines


def test_diff_reports_component_added_removed_changed():
    old = _lock(
        {},
        [
            {"ref": "C1", "value": "100nF", "footprint": "F1", "mpn": "", "dnp": False},
            {"ref": "C2", "value": "100nF", "footprint": "F1", "mpn": "", "dnp": False},
        ],
    )
    new = _lock(
        {},
        [
            {"ref": "C1", "value": "220nF", "footprint": "F1", "mpn": "", "dnp": False},
            {"ref": "C3", "value": "100nF", "footprint": "F1", "mpn": "", "dnp": False},
        ],
    )
    lines = netlist._diff(old, new)
    assert "component 'C3' added (100nF, F1)" in lines
    assert "component 'C2' removed (was 100nF, F1)" in lines
    assert any("C1" in line and "value" in line and "100nF" in line and "220nF" in line for line in lines)


def test_diff_is_empty_for_identical_locks():
    lock = _lock({"GND": ["C1.1"]})
    assert netlist._diff(lock, json.loads(json.dumps(lock))) == []


# --- serialization determinism ------------------------------------------


def test_to_lock_text_field_order_matches_schema_example():
    lock = {
        "schema": 1,
        "nets": [{"name": "GND", "nodes": ["C1.1"]}],
        "components": [{"ref": "C1", "value": "100nF", "footprint": "F1", "mpn": "X", "dnp": False}],
    }
    text = netlist.to_lock_text(lock)
    assert text.endswith("\n")
    assert list(json.loads(text).keys()) == ["schema", "nets", "components"]
    # Same dict serialized twice is byte-identical (RULE 16.2 idempotence).
    assert netlist.to_lock_text(lock) == text


# --- run(): write / verify ------------------------------------------------


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "TestBoard"
    project.mkdir()
    (project / "TestBoard.kicad_pro").write_text("{}", encoding="utf-8")
    (project / "TestBoard.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    (project / "TestBoard.kicad_sch").write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")
    return discover(project)


def test_run_write_mode_writes_the_lock(project_dir, monkeypatch):
    lock = _lock({"GND": ["C1.1"]})
    monkeypatch.setattr(netlist, "build_lock", lambda files: lock)

    report = netlist.run(project_dir, write=True)

    assert report.ok, report.violations
    lock_path = project_dir.project_dir / netlist.LOCK_FILENAME
    assert json.loads(lock_path.read_text(encoding="utf-8")) == lock


def test_run_write_mode_is_idempotent(project_dir, monkeypatch):
    lock = _lock({"GND": ["C1.1", "C2.1"], "+3V3": ["U1.2"]})
    monkeypatch.setattr(netlist, "build_lock", lambda files: lock)

    netlist.run(project_dir, write=True)
    first = (project_dir.project_dir / netlist.LOCK_FILENAME).read_bytes()
    netlist.run(project_dir, write=True)
    second = (project_dir.project_dir / netlist.LOCK_FILENAME).read_bytes()

    assert first == second


def test_run_verify_mode_fails_when_lock_missing(project_dir, monkeypatch):
    monkeypatch.setattr(netlist, "build_lock", lambda files: _lock({}))
    report = netlist.run(project_dir, write=False)
    assert not report.ok
    assert any(v.code == "missing_connectivity_lock" for v in report.violations)


def test_run_verify_mode_passes_when_lock_matches(project_dir, monkeypatch):
    lock = _lock({"GND": ["C1.1"]})
    monkeypatch.setattr(netlist, "build_lock", lambda files: lock)
    netlist.run(project_dir, write=True)

    report = netlist.run(project_dir, write=False)
    assert report.ok, report.violations


def test_run_verify_mode_fails_with_readable_diff_on_regression(project_dir, monkeypatch):
    committed = _lock({"SDA": ["U3.7"], "SCL": []})
    monkeypatch.setattr(netlist, "build_lock", lambda files: committed)
    netlist.run(project_dir, write=True)

    regressed = _lock({"SDA": [], "SCL": ["U3.7"]})
    monkeypatch.setattr(netlist, "build_lock", lambda files: regressed)
    report = netlist.run(project_dir, write=False)

    assert not report.ok
    assert any(v.code == "connectivity_regression" for v in report.violations)
    assert any("U3.7 moved from net 'SDA' to net 'SCL'" in v.message for v in report.violations)


def test_run_reports_violation_when_export_fails(project_dir, monkeypatch):
    def _raise(files):
        raise netlist.NetlistError("kicad-cli not found")

    monkeypatch.setattr(netlist, "build_lock", _raise)
    report = netlist.run(project_dir, write=False)
    assert not report.ok
    assert any(v.code == "netlist_export_failed" for v in report.violations)
