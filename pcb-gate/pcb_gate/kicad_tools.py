"""Thin subprocess wrapper around `kicad-cli`, shared by canary.py and keepout.py.

Empirically confirmed against a real `kicad/kicad:10.0` container run against
`gni-ambient-sensor-pcb` (2026-07-26), because the DRC JSON schema and the
`--refill-zones` behaviour are load-bearing for this package and were not
worth guessing:

- `kicad-cli pcb drc --format json` writes `{"violations": [...], "unconnected_items": [...],
  "schematic_parity": [...]}` - unconnected items and schematic-parity mismatches are their
  OWN top-level arrays, not `violations` entries with a matching `type`.
- Each entry (in any of the three arrays) has a `type` key that is the DRC rule's key name
  (e.g. "clearance", "track_width", "unconnected_items", "assertion_failure" - not
  "assertion", despite the constraint being named `assertion` in the `.kicad_dru` grammar).
- `kicad-cli pcb drc --refill-zones --save-board` overwrites INPUT_FILE in place with
  freshly filled zones. This answers the brief's "fill-freshness" question: KiCad 10 CAN
  refill non-interactively, so keepout.py refills a throwaway copy before reading its
  `filled_polygon` geometry, never the real board.
- SVW-0037: re-confirmed against real `--schematic-parity` firings (mansio-pcb spin1, 4
  "Extra footprint" hits) that `schematic_parity` entries are FLAT violation dicts with
  their own `type`/`severity`/`description`/`items` keys, the same shape as `violations`
  entries - NOT sheet-wrappers with a nested `violations` list. The prior comment here
  ("each sheet has its own `violations` list") was written against a project with zero
  parity issues and was never actually confirmed against a firing case; it was wrong, and
  `all_violations()` below silently dropped every real schematic_parity entry as a result.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KICAD_CLI = "kicad-cli"


class KicadCliUnavailable(RuntimeError):
    pass


def available() -> bool:
    return shutil.which(KICAD_CLI) is not None


@dataclass
class DrcResult:
    exit_code: int
    stdout: str
    stderr: str
    report: dict = field(default_factory=dict)

    def all_violations(self) -> list[dict]:
        """Every reported item across violations, unconnected_items and schematic_parity.

        Confirmed against a real firing (SVW-0037, mansio-pcb spin1): each
        `schematic_parity` entry IS a violation dict directly (`type`,
        `severity`, `description`, `items`), the same shape as a top-level
        `violations` entry - not a per-sheet wrapper with a nested
        `violations` list. Still handled defensively (fall back to a nested
        `violations` list if an entry has one and no `type` of its own) in
        case a different kicad-cli version wraps it that way; this is now a
        fallback, not the assumed shape.
        """
        items = list(self.report.get("violations", []))
        items += list(self.report.get("unconnected_items", []))
        for entry in self.report.get("schematic_parity", []):
            if isinstance(entry, dict) and "type" not in entry and "violations" in entry:
                items += list(entry.get("violations", []))
            else:
                items.append(entry)
        return items

    def has_violation_type(self, *types: str) -> bool:
        wanted = set(types)
        return any(item.get("type") in wanted for item in self.all_violations())


def run_drc(
    pcb_file: Path,
    report_path: Path,
    refill_zones: bool = False,
    save_board: bool = False,
    exit_code_violations: bool = True,
    all_track_errors: bool = False,
    schematic_parity: bool = False,
) -> DrcResult:
    if not available():
        raise KicadCliUnavailable(f"'{KICAD_CLI}' not found on PATH")

    args = [
        KICAD_CLI,
        "pcb",
        "drc",
        "--severity-all",
        "--format",
        "json",
        "-o",
        str(report_path),
    ]
    if exit_code_violations:
        args.append("--exit-code-violations")
    if refill_zones:
        args.append("--refill-zones")
    if save_board:
        args.append("--save-board")
    if all_track_errors:
        args.append("--all-track-errors")
    if schematic_parity:
        args.append("--schematic-parity")
    args.append(str(pcb_file))

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    report: dict = {}
    if report_path.is_file():
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    return DrcResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, report=report)


def refill_zones_in_place(pcb_file: Path, scratch_report: Path) -> DrcResult:
    """Refill every zone in `pcb_file` and save it, in place. Only ever call this on a copy.

    `exit_code_violations` is deliberately off: this call's only job is the
    refill+save side effect, and its exit code should reflect whether kicad-cli
    ran successfully, not whether the (possibly unrelated) board has DRC
    violations.
    """
    return run_drc(pcb_file, scratch_report, refill_zones=True, save_board=True, exit_code_violations=False)
