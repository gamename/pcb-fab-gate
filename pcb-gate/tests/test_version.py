from __future__ import annotations

import pytest

from pcb_gate import sexp, version


def silk(text, layer="F.SilkS"):
    return ["gr_text", sexp.qstr(text), ["at", "100", "100"], ["layer", sexp.qstr(layer)]]


def run(project_factory, tmp_path, items, value="2.0.0"):
    files = project_factory(items=items)
    path = tmp_path / "VERSION"
    path.write_text(f"{value}\n", encoding="utf-8")
    return version.run(files, path)


def codes(report):
    return [v.code for v in report.violations]


def test_matching_silkscreen_passes(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("MH2 Egg v2.0.0")])) == []


def test_back_silkscreen_counts(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.0", "B.SilkS")])) == []


def test_missing_marking_fails(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("MH2 Egg")])) == ["silkscreen_version_mismatch"]


def test_copper_text_does_not_count(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.0", "F.Cu")])) == ["silkscreen_version_mismatch"]


def test_different_version_fails_both_ways(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.1")])) == ["silkscreen_version_mismatch"] * 2


def test_stale_second_version_fails(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.0"), silk("v1.9.0")])) == ["silkscreen_version_mismatch"]


def test_longer_version_is_not_a_match(project_factory, tmp_path):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.0.1")])) == ["silkscreen_version_mismatch"]


@pytest.mark.parametrize("value", ["2.0", "v2.0.0", "02.0.0", "2.0.0-rc1", ""])
def test_bad_version_file_fails(project_factory, tmp_path, value):
    assert codes(run(project_factory, tmp_path, [silk("v2.0.0")], value)) == ["bad_version_file"]


def test_missing_version_file_fails(project_factory, tmp_path):
    files = project_factory(items=[])
    assert codes(version.run(files, tmp_path / "nope")) == ["bad_version_file"]
