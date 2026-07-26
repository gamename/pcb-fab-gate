"""Resolve which netclass governs a given net name.

Best-effort: KiCad resolves a net's netclass from (in decreasing priority) an
explicit per-net assignment, then netclass patterns matched against the net
name in list order, falling back to "Default". The exact JSON shape of
`net_settings.netclass_assignments` / `net_settings.netclass_patterns` in a
`.kicad_pro` with more than one netclass in play is **unverified** - every
current GNI reference board uses a single (Default) netclass, so there is
nothing to check the shape against yet. Flagged here rather than hidden, in
the spirit of SVW-0034 RULE 6.3 ("record that fact, choose a defensible
number, state the basis"): verify this against a real multi-netclass project
(the first RF board with an `RF_ANT` netclass will be the first real test)
before trusting it as ground truth.
"""
from __future__ import annotations

import fnmatch


def netclass_of(net_name: str, pro: dict) -> str:
    net_settings = pro.get("net_settings") or {}

    assignments = net_settings.get("netclass_assignments") or {}
    if isinstance(assignments, dict) and net_name in assignments:
        return assignments[net_name]

    for entry in net_settings.get("netclass_patterns") or []:
        pattern = entry.get("pattern") or entry.get("matchPattern") or entry.get("net")
        netclass = entry.get("netclass") or entry.get("class") or entry.get("netClassName")
        if pattern and netclass and fnmatch.fnmatchcase(net_name, pattern):
            return netclass

    return "Default"
