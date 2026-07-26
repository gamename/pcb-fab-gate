import datetime

import pytest

from pcb_gate import capability
from tests.conftest import make_capability_yml


def test_missing_file_raises(tmp_path):
    with pytest.raises(capability.CapabilityError):
        capability.load(tmp_path / "pcb-capability.yml")


def test_missing_required_field_raises(tmp_path):
    path = tmp_path / "pcb-capability.yml"
    path.write_text("fab: JLCPCB\nretrieved: 2026-07-01\n", encoding="utf-8")
    with pytest.raises(capability.CapabilityError):
        capability.load(path)


def test_valid_file_loads(tmp_path):
    path = make_capability_yml(tmp_path)
    cap = capability.load(path)
    assert cap.fab == "JLCPCB"
    assert cap.retrieved == datetime.date(2026, 7, 1)
    assert cap.constraints["min_clearance_mm"] == 0.1
    assert cap.declared_exclusion_rules() == set()


def test_declared_exclusions_parsed(tmp_path):
    path = make_capability_yml(
        tmp_path,
        declared_exclusions=[{"rule": "unconnected_items", "reason": "DevKit GPIOs intentionally unconnected"}],
    )
    cap = capability.load(path)
    assert cap.declared_exclusion_rules() == {"unconnected_items"}


def test_age_days():
    cap = capability.Capability(
        fab="JLCPCB",
        retrieved=datetime.date(2026, 1, 1),
        source="https://example.invalid",
        stackup="X",
        constraints={},
    )
    assert cap.age_days(datetime.date(2026, 1, 31)) == 30
