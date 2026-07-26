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
    violations: list[Violation] = field(default_factory=list)

    def check(self, description: str) -> None:
        self.checked.append(description)
        print(f"[{self.tool}] checked: {description}")

    def skip(self, description: str) -> None:
        self.skipped.append(description)
        print(f"[{self.tool}] SKIPPED: {description}")

    def fail(self, code: str, message: str) -> None:
        self.violations.append(Violation(code=code, message=message))
        print(f"[{self.tool}] VIOLATION [{code}]: {message}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_json(self) -> dict:
        return {
            "tool": self.tool,
            "project": self.project,
            "checked": self.checked,
            "skipped": self.skipped,
            "violations": [asdict(v) for v in self.violations],
            "ok": self.ok,
        }

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    def summarize(self) -> int:
        """Print a final summary line and return the process exit code."""
        if self.ok:
            print(f"[{self.tool}] PASS - {len(self.checked)} check(s), 0 violations")
            return 0
        print(
            f"[{self.tool}] FAIL - {len(self.checked)} check(s), "
            f"{len(self.violations)} violation(s)",
            file=sys.stderr,
        )
        return 1
