"""`pcb-gate <subcommand> --project-dir <dir>` entrypoint.

Every subcommand discovers the project, runs its check, prints what it
checked, writes a JSON report, and exits non-zero on any violation - the
same contract `kicad-cli pcb drc` already follows, so the workflow can treat
all six checks (arm, canary, ERC, DRC, keepout, overlap) uniformly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import arming, canary, keepout, overlap
from .project import ProjectError, discover
from .report import Report

SUBCOMMANDS = {
    "arm": arming.run,
    "canary": canary.run,
    "keepout": keepout.run,
    "overlap": overlap.run,
}


def _default_report_path(project_dir: Path, subcommand: str) -> Path:
    return project_dir / f"pcb-gate-{subcommand}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcb-gate")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    for name in SUBCOMMANDS:
        sub = subparsers.add_parser(name)
        sub.add_argument("--project-dir", required=True)
        sub.add_argument("--report", default=None, help="Where to write the JSON report")

    args = parser.parse_args(argv)

    try:
        files = discover(args.project_dir)
    except ProjectError as exc:
        print(f"pcb-gate {args.subcommand}: {exc}", file=sys.stderr)
        return 2

    check_fn = SUBCOMMANDS[args.subcommand]
    report: Report = check_fn(files)

    report_path = Path(args.report) if args.report else _default_report_path(files.project_dir, args.subcommand)
    report.write(report_path)

    return report.summarize()


if __name__ == "__main__":
    sys.exit(main())
