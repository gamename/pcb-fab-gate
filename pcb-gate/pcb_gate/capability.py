"""Schema and loader for `pcb-capability.yml` (SVW-0034 GATE 2 / RULE 4.1).

One of these lives beside every board's .kicad_pro, written from the fab's
*current* published capability sheet with a real retrieval date. See the
GNI-0283 brief for the canonical example.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MAX_CAPABILITY_AGE_DAYS = 180

REQUIRED_TOP_FIELDS = ("fab", "retrieved", "source", "stackup", "constraints")

REQUIRED_CONSTRAINT_FIELDS = (
    "min_track_width_mm",
    "min_clearance_mm",
    "min_annular_ring_mm",
    "min_drill_mm",
    "min_silk_line_width_mm",
    "min_silk_text_height_mm",
    "edge_clearance_mm",
)


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class DeclaredExclusion:
    rule: str
    reason: str


@dataclass(frozen=True)
class DeclaredSeverityDowngrade:
    """SVW-0038 RULE 1.2: a reviewed, written exception to the closed-world 'no ignore' assertion.

    `check` is the DRC or ERC rule-severity key (e.g. "footprint_type_mismatch",
    "footprint_filter") - the two severity dicts (board.design_settings.rule_severities
    for DRC, erc.rule_severities for ERC) are keyed independently, but in practice their
    key names don't collide, so a single flat `declared_severity_downgrades` list (no
    drc/erc split) matches the brief's YAML example and keeps the capability file simple.
    """

    check: str
    severity: str
    reason: str


@dataclass(frozen=True)
class Capability:
    fab: str
    retrieved: datetime.date
    source: str
    stackup: str
    constraints: dict[str, float]
    declared_exclusions: tuple[DeclaredExclusion, ...] = field(default_factory=tuple)
    declared_severity_downgrades: tuple[DeclaredSeverityDowngrade, ...] = field(default_factory=tuple)

    def age_days(self, today: datetime.date) -> int:
        return (today - self.retrieved).days

    def declared_exclusion_rules(self) -> set[str]:
        return {e.rule for e in self.declared_exclusions}

    def declared_severity_downgrades_by_check(self) -> dict[str, DeclaredSeverityDowngrade]:
        return {d.check: d for d in self.declared_severity_downgrades}


def load(capability_file: Path) -> Capability:
    if not capability_file.is_file():
        raise CapabilityError(
            f"required file not found: {capability_file} "
            "(SVW-0034 GATE 2 / RULE 4.1 - every board needs a dated capability sheet)"
        )

    with open(capability_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    missing_top = [k for k in REQUIRED_TOP_FIELDS if k not in raw]
    if missing_top:
        raise CapabilityError(
            f"{capability_file}: missing required field(s): {', '.join(missing_top)}"
        )

    constraints = raw["constraints"] or {}
    missing_constraints = [k for k in REQUIRED_CONSTRAINT_FIELDS if k not in constraints]
    if missing_constraints:
        raise CapabilityError(
            f"{capability_file}: constraints missing required field(s): "
            f"{', '.join(missing_constraints)}"
        )

    retrieved_raw = raw["retrieved"]
    if isinstance(retrieved_raw, datetime.date):
        retrieved = retrieved_raw
    else:
        try:
            retrieved = datetime.date.fromisoformat(str(retrieved_raw))
        except ValueError as exc:
            raise CapabilityError(
                f"{capability_file}: 'retrieved' must be an ISO date (YYYY-MM-DD), got {retrieved_raw!r}"
            ) from exc

    exclusions_raw = raw.get("declared_exclusions") or []
    exclusions = tuple(
        DeclaredExclusion(rule=e["rule"], reason=e["reason"]) for e in exclusions_raw
    )

    downgrades_raw = raw.get("declared_severity_downgrades") or []
    downgrades = tuple(
        DeclaredSeverityDowngrade(check=d["check"], severity=d.get("severity", "ignore"), reason=d["reason"])
        for d in downgrades_raw
    )

    return Capability(
        fab=str(raw["fab"]),
        retrieved=retrieved,
        source=str(raw["source"]),
        stackup=str(raw["stackup"]),
        constraints={k: float(v) for k, v in constraints.items()},
        declared_exclusions=exclusions,
        declared_severity_downgrades=downgrades,
    )
