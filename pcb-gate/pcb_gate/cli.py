"""`pcb-gate <subcommand> --project-dir <dir>` entrypoint.

Every subcommand discovers the project, runs its check, prints what it
checked, writes a JSON report, and exits non-zero on any violation - the
same contract `kicad-cli pcb drc` already follows, so the workflow can treat
all seven checks (arm, canary, ERC, DRC, netlist, keepout, overlap) uniformly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import arming, canary, keepout, netlist, overlap
from .project import ProjectError, discover
from .report import Report

SUBCOMMANDS = {
    "arm": arming.run,
    "canary": canary.run,
    "keepout": keepout.run,
    "overlap": overlap.run,
    "netlist": netlist.run,
}

# Defect 2 (Task 2): both `keepout` and `canary` need to agree on which rule
# area name(s) count as the antenna keepout - `canary` because its keepout
# canary injects into whatever zone `keepout` would check, and a mismatch
# there would make the canary itself silently inert on a non-default name.
KEEPOUT_ZONE_NAME_SUBCOMMANDS = {"keepout", "canary"}
RF_BOARD_SUBCOMMANDS = {"keepout"}
# SVW-0038: `netlist --write` regenerates/refreshes the lock; without it,
# `netlist` verifies the committed lock against a fresh regeneration and
# fails on any difference (RULE 16.1 - the lock is generated, never hand-edited).
WRITE_SUBCOMMANDS = {"netlist"}


def _default_report_path(project_dir: Path, subcommand: str) -> Path:
    return project_dir / f"pcb-gate-{subcommand}.json"


def _parse_zone_names(raw: str) -> list[str]:
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or [keepout.KEEPOUT_ZONE_NAME]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcb-gate")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    for name in SUBCOMMANDS:
        sub = subparsers.add_parser(name)
        sub.add_argument("--project-dir", required=True)
        sub.add_argument("--report", default=None, help="Where to write the JSON report")
        if name in KEEPOUT_ZONE_NAME_SUBCOMMANDS:
            sub.add_argument(
                "--keepout-zone-name",
                default=keepout.KEEPOUT_ZONE_NAME,
                help="Comma-separated rule-area name(s) checked as the antenna keepout",
            )
        if name in RF_BOARD_SUBCOMMANDS:
            sub.add_argument(
                "--rf-board",
                action="store_true",
                help="Fail (instead of skip) when no matching keepout rule area is found",
            )
        if name in WRITE_SUBCOMMANDS:
            sub.add_argument(
                "--write",
                action="store_true",
                help="Generate/refresh connectivity.lock.json instead of verifying it",
            )

    args = parser.parse_args(argv)

    try:
        files = discover(args.project_dir)
    except ProjectError as exc:
        print(f"pcb-gate {args.subcommand}: {exc}", file=sys.stderr)
        return 2

    check_fn = SUBCOMMANDS[args.subcommand]
    kwargs = {}
    if args.subcommand in KEEPOUT_ZONE_NAME_SUBCOMMANDS:
        kwargs["keepout_zone_names"] = _parse_zone_names(args.keepout_zone_name)
    if args.subcommand in RF_BOARD_SUBCOMMANDS:
        kwargs["rf_board"] = args.rf_board
    if args.subcommand in WRITE_SUBCOMMANDS:
        kwargs["write"] = args.write

    report: Report = check_fn(files, **kwargs)

    report_path = Path(args.report) if args.report else _default_report_path(files.project_dir, args.subcommand)
    report.write(report_path)

    return report.summarize()


if __name__ == "__main__":
    sys.exit(main())
