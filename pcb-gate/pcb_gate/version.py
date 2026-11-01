"""Hardware semver - the version file, the silkscreen and the release name agree.

Optional: a caller opts in with the gate's `version_file` input. The file holds
one `MAJOR.MINOR.PATCH`. A bare board in the hand has only its silkscreen to say
what it is, so the same version must be printed there as `vMAJOR.MINOR.PATCH`,
in a `gr_text` on F.SilkS or B.SilkS. A second, different version on the
silkscreen fails too: that is a stale marking, not a spare one.

Reads raw geometry only, like `overlap` - `sexp.py`, never KiCad.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from . import sexp
from .project import ProjectFiles
from .report import Report

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_SILK_LAYERS = ("F.SilkS", "B.SilkS")
_SILK_VERSION = re.compile(r"(?<![\w.])v(\d+\.\d+\.\d+)(?![\w.])")


def read_version(version_file: Path) -> str | None:
    """The version in `version_file`, or None if it is missing or isn't plain semver."""
    if not version_file.is_file():
        return None
    text = version_file.read_text(encoding="utf-8").strip()
    return text if SEMVER.fullmatch(text) else None


def silk_versions(root: sexp.Node) -> list[str]:
    """Every `vX.Y.Z` printed in a board-level text on a silkscreen layer."""
    found = []
    for node in sexp.children(root, "gr_text"):
        if sexp.text_of(sexp.child(node, "layer")) not in _SILK_LAYERS or len(node) < 2:
            continue
        found.extend(_SILK_VERSION.findall(str(node[1])))
    return found


def _mismatches(version: str, printed: list[str]) -> list[str]:
    problems = []
    if version not in printed:
        problems.append(f"silkscreen does not carry v{version} (found: {printed or 'none'})")
    problems.extend(f"silkscreen carries stale v{other}" for other in sorted(set(printed)) if other != version)
    return problems


def _run_canary(root: sexp.Node, version: str, report: Report) -> None:
    """RULE 1.1: blank the silkscreen in a throwaway copy and prove the check notices."""
    blanked = copy.deepcopy(root)
    for node in sexp.children(blanked, "gr_text"):
        if len(node) > 1:
            node[1] = _SILK_VERSION.sub("", str(node[1]))
    if _mismatches(version, silk_versions(blanked)):
        report.check("canary: removing the version from the silkscreen is detected")
    else:
        report.fail("version_canary_inert", "removing the version from the silkscreen went unnoticed")


def run(files: ProjectFiles, version_file: str | Path = "VERSION") -> Report:
    report = Report(tool="pcb-gate version", project=files.base_name)
    path = Path(version_file)
    version = read_version(path)
    if version is None:
        report.fail("bad_version_file", f"{path} must exist and hold one MAJOR.MINOR.PATCH")
        return report
    report.check(f"{path} holds {version}")

    root = sexp.parse_file(files.pcb_file)
    problems = _mismatches(version, silk_versions(root))
    for problem in problems:
        report.fail("silkscreen_version_mismatch", problem)
    if not problems:
        report.check(f"silkscreen carries v{version}")
    _run_canary(root, version, report)
    return report
