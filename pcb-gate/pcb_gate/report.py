"""Shared report shape for every pcb-gate subcommand.

Every checker prints what it checked (RULE 1.2: arming assertions are
explicit and enumerated, not implied) and writes the same JSON shape so the
workflow can upload arm.json / canary.json / keepout.json / overlap.json
alongside erc.json / drc.json.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Violation:
    code: str
    message: str


@dataclass
class Report:
    tool: str
    project: str
    checked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Subset of `skipped` that means "could not check", not "correctly not
    # applicable" - e.g. a schematic that couldn't be loaded, not a board with
    # no rule area it never claimed to have. A blocking skip is not a pass:
    # `ok` below folds it in, same as a violation.
    skipped_blocking: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    def check(self, description: str) -> None:
        self.checked.append(description)
        print(f"[{self.tool}] checked: {description}")

    def skip(self, description: str, *, blocking: bool = False) -> None:
        self.skipped.append(description)
        if blocking:
            self.skipped_blocking.append(description)
            print(f"[{self.tool}] SKIPPED (blocking): {description}", file=sys.stderr)
        else:
            print(f"[{self.tool}] SKIPPED: {description}")

    def fail(self, code: str, message: str) -> None:
        self.violations.append(Violation(code=code, message=message))
        print(f"[{self.tool}] VIOLATION [{code}]: {message}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.skipped_blocking

    def to_json(self) -> dict:
        return {
            "tool": self.tool,
            "project": self.project,
            "checked": self.checked,
            "skipped": self.skipped,
            "skipped_blocking": self.skipped_blocking,
            "violations": [asdict(v) for v in self.violations],
            "ok": self.ok,
        }

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    def summarize(self) -> int:
        """Print a final summary line and return the process exit code.

        A report with zero checks is not a pass, no matter what `ok` says - it
        means nothing was actually verified, and printing PASS on that is the
        exact defect this method exists to close (Defect 2/3: a check that
        examined nothing must never look identical to a check that passed).
        """
        if not self.checked:
            print(
                f"[{self.tool}] INCONCLUSIVE - 0 check(s) performed "
                f"({len(self.skipped)} skip(s)) - nothing was actually verified",
                file=sys.stderr,
            )
            return 1
        if self.ok:
            print(
                f"[{self.tool}] PASS - {len(self.checked)} check(s), 0 violations, "
                f"{len(self.skipped)} skip(s)"
            )
            return 0
        print(
            f"[{self.tool}] FAIL - {len(self.checked)} check(s), "
            f"{len(self.violations)} violation(s), "
            f"{len(self.skipped_blocking)} blocking skip(s) of {len(self.skipped)} total",
            file=sys.stderr,
        )
        return 1
