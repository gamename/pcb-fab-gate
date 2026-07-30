"""GATE 15 / RULE 16.x (SVW-0038) - connectivity is an artifact, and it regresses.

`connectivity.lock.json` is generated from `kicad-cli sch export netlist --format
kicadsexpr` (confirmed empirically the richest/most-robust format under KiCad 10.0.4 -
carries per-component value/footprint/MPN and per-net REFDES.PIN nodes directly, unlike
`orcadpcb2`/`spice`/etc which drop fields this lock needs) and reduced to content that
describes the design, not the file (RULE 16.2): UUIDs, net codes, timestamps, tool
versions, and sheet/file paths are all dropped.

One field the netlist export does NOT carry, confirmed empirically against a real board
with `(dnp yes)` symbol instances (bb-pcb spin2, 2026-07-28): `dnp`. KiCad's netlist
exporter simply never emits it, in any format. It is read directly from the schematic's
own symbol instances instead - each top-level `(symbol ...)` node in `.kicad_sch` that
carries a `(dnp yes/no)` sibling (library-definition `symbol` nodes under `lib_symbols`
never do, which is how they're excluded) also carries `(property "Reference" "<ref>" ...)`
as a direct two-atom form - a different shape than the netlist's own
`(property (name "MPN") (value "..."))`, so this module has two small extractors, not one.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import kicad_tools, sexp
from .project import ProjectFiles
from .report import Report

SCHEMA_VERSION = 1
LOCK_FILENAME = "connectivity.lock.json"


class NetlistError(RuntimeError):
    pass


def natural_key(s: str) -> list[tuple[int, object]]:
    """Numeric-aware sort key (RULE 16.2): 'U1.2' before 'U1.10', 'C2' before 'C10'.

    Every chunk is wrapped as (kind, value) - kind 0 for text, 1 for a parsed int - so
    two keys of different shape (e.g. differing chunk counts) never compare an int
    against a str directly, which plain `int(part) if part.isdigit() else part` can do
    and raise TypeError on.
    """
    chunks = [c for c in re.split(r"(\d+)", s) if c != ""]
    return [(1, int(c)) if c.isdigit() else (0, c) for c in chunks]


def _run_netlist_export(sch_file: Path) -> sexp.Node:
    if not kicad_tools.available():
        raise NetlistError(
            f"'{kicad_tools.KICAD_CLI}' not found on PATH - required to export the connectivity netlist"
        )
    with tempfile.TemporaryDirectory(prefix="pcb-gate-netlist-") as tmp:
        out_path = Path(tmp) / "netlist.net"
        args = [
            kicad_tools.KICAD_CLI,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "-o",
            str(out_path),
            str(sch_file),
        ]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not out_path.is_file():
            raise NetlistError(
                f"kicad-cli sch export netlist failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        return sexp.parse_file(out_path)


def _comp_property(comp: sexp.Node, name: str) -> str | None:
    """Read a netlist `comp`'s (property (name "X") (value "Y")) form."""
    for prop in sexp.children(comp, "property"):
        if sexp.text_of(sexp.child(prop, "name")) == name:
            value_node = sexp.child(prop, "value")
            return sexp.text_of(value_node) if value_node is not None else None
    return None


def _comp_field(comp: sexp.Node, name: str) -> str | None:
    """Read a netlist `comp`'s (fields (field (name "X") "Y")) form - the field's value
    is a bare trailing atom, not wrapped in its own tag."""
    fields_node = sexp.child(comp, "fields")
    if fields_node is None:
        return None
    for field_node in sexp.children(fields_node, "field"):
        if sexp.text_of(sexp.child(field_node, "name")) == name:
            return sexp.text_of(field_node)
    return None


def _extract_components(export_root: sexp.Node) -> dict[str, dict]:
    components: dict[str, dict] = {}
    comps_node = sexp.child(export_root, "components")
    if comps_node is None:
        return components
    for comp in sexp.children(comps_node, "comp"):
        ref = sexp.text_of(sexp.child(comp, "ref"))
        if not ref:
            continue
        components[ref] = {
            "value": sexp.text_of(sexp.child(comp, "value")) or "",
            "footprint": sexp.text_of(sexp.child(comp, "footprint")) or "",
            "mpn": _comp_property(comp, "MPN") or _comp_field(comp, "MPN") or "",
        }
    return components


def _extract_nets(export_root: sexp.Node) -> dict[str, list[str]]:
    nets: dict[str, list[str]] = {}
    nets_node = sexp.child(export_root, "nets")
    if nets_node is None:
        return nets
    for net in sexp.children(nets_node, "net"):
        name = sexp.text_of(sexp.child(net, "name")) or ""
        nodes = []
        for node in sexp.children(net, "node"):
            ref = sexp.text_of(sexp.child(node, "ref"))
            pin = sexp.text_of(sexp.child(node, "pin"))
            if ref and pin:
                nodes.append(f"{ref}.{pin}")
        nets.setdefault(name, []).extend(nodes)
    return nets


def _property_value(symbol_node: sexp.Node, name: str) -> str | None:
    """Read a `.kicad_sch` symbol instance's (property "Name" "Value" ...) form -
    positional atoms, not the netlist's nested (name)/(value) sub-nodes."""
    for prop in sexp.children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name and isinstance(prop[2], str):
            return prop[2]
    return None


def extract_dnp_by_ref(sch_file: Path) -> dict[str, bool]:
    root = sexp.parse_file(sch_file)
    dnp_by_ref: dict[str, bool] = {}
    for symbol_node in sexp.find_all(root, "symbol"):
        dnp_node = sexp.child(symbol_node, "dnp")
        if dnp_node is None:
            continue  # a lib_symbols library definition, not a placed instance
        ref = _property_value(symbol_node, "Reference")
        if not ref:
            continue
        dnp_by_ref[ref] = sexp.text_of(dnp_node) == "yes"
    return dnp_by_ref


def build_lock(files: ProjectFiles) -> dict:
    export_root = _run_netlist_export(files.sch_file)
    components_raw = _extract_components(export_root)
    nets_raw = _extract_nets(export_root)
    dnp_by_ref = extract_dnp_by_ref(files.sch_file)

    nets = [
        {"name": name, "nodes": sorted(set(nodes), key=natural_key)}
        for name, nodes in sorted(nets_raw.items(), key=lambda item: natural_key(item[0]))
    ]
    components = [
        {
            "ref": ref,
            "value": components_raw[ref]["value"],
            "footprint": components_raw[ref]["footprint"],
            "mpn": components_raw[ref]["mpn"],
            "dnp": dnp_by_ref.get(ref, False),
        }
        for ref in sorted(components_raw, key=natural_key)
    ]

    return {"schema": SCHEMA_VERSION, "nets": nets, "components": components}


def to_lock_text(lock: dict) -> str:
    """Deterministic serialization (RULE 16.2): fixed field order as built (not
    alphabetical - matches the brief's own worked example, schema/nets/components and
    ref/value/footprint/mpn/dnp), fixed indent, trailing newline, UTF-8, LF. Determinism
    here comes from every array already being sorted and every dict built in a fixed
    field order, not from `sort_keys=True` (which would reorder fields away from the
    brief's example)."""
    return json.dumps(lock, indent=2, ensure_ascii=False) + "\n"


def _diff(old: dict, new: dict) -> list[str]:
    """A readable diff: nets added/removed, nodes added/removed per net, components
    added/removed/changed - and a moved pin (same node, different net in old vs new)
    reported as a single "moved from X to Y" line rather than as an add in one net plus
    a remove in another, per the acceptance criterion that a moved pin names the pin,
    old net, and new net on one line."""
    lines: list[str] = []

    old_nets = {n["name"]: n["nodes"] for n in old.get("nets", [])}
    new_nets = {n["name"]: n["nodes"] for n in new.get("nets", [])}
    old_node_net = {node: name for name, nodes in old_nets.items() for node in nodes}
    new_node_net = {node: name for name, nodes in new_nets.items() for node in nodes}

    moved = sorted(
        (node for node in (set(old_node_net) & set(new_node_net)) if old_node_net[node] != new_node_net[node]),
        key=natural_key,
    )
    moved_set = set(moved)
    for node in moved:
        lines.append(f"{node} moved from net '{old_node_net[node]}' to net '{new_node_net[node]}'")

    for name in sorted(set(new_nets) - set(old_nets)):
        lines.append(f"net '{name}' added with node(s): {', '.join(sorted(new_nets[name], key=natural_key))}")
    for name in sorted(set(old_nets) - set(new_nets)):
        lines.append(f"net '{name}' removed (had node(s): {', '.join(sorted(old_nets[name], key=natural_key))})")

    for name in sorted(set(old_nets) & set(new_nets)):
        old_set, new_set = set(old_nets[name]), set(new_nets[name])
        added = sorted((new_set - old_set) - moved_set, key=natural_key)
        removed = sorted((old_set - new_set) - moved_set, key=natural_key)
        if added:
            lines.append(f"net '{name}': node(s) added: {', '.join(added)}")
        if removed:
            lines.append(f"net '{name}': node(s) removed: {', '.join(removed)}")

    old_comps = {c["ref"]: c for c in old.get("components", [])}
    new_comps = {c["ref"]: c for c in new.get("components", [])}

    for ref in sorted(set(new_comps) - set(old_comps), key=natural_key):
        c = new_comps[ref]
        lines.append(f"component '{ref}' added ({c['value']}, {c['footprint']})")
    for ref in sorted(set(old_comps) - set(new_comps), key=natural_key):
        c = old_comps[ref]
        lines.append(f"component '{ref}' removed (was {c['value']}, {c['footprint']})")

    for ref in sorted(set(old_comps) & set(new_comps), key=natural_key):
        old_c, new_c = old_comps[ref], new_comps[ref]
        for field_name in ("value", "footprint", "mpn", "dnp"):
            if old_c.get(field_name) != new_c.get(field_name):
                lines.append(
                    f"component '{ref}': {field_name} changed from {old_c.get(field_name)!r} "
                    f"to {new_c.get(field_name)!r}"
                )

    return lines


def run(files: ProjectFiles, write: bool = False) -> Report:
    report = Report(tool="pcb-gate netlist", project=files.base_name)
    lock_path = files.project_dir / LOCK_FILENAME

    try:
        new_lock = build_lock(files)
    except NetlistError as exc:
        report.fail("netlist_export_failed", str(exc))
        return report

    new_text = to_lock_text(new_lock)
    report.check(
        f"regenerated {LOCK_FILENAME} from {files.sch_file.name} "
        "(kicad-cli sch export netlist --format kicadsexpr)"
    )

    if write:
        lock_path.write_text(new_text, encoding="utf-8")
        report.check(f"wrote {lock_path}")
        return report

    if not lock_path.is_file():
        report.fail(
            "missing_connectivity_lock",
            f"{lock_path} does not exist - run "
            f"'pcb-gate netlist --project-dir {files.project_dir} --write', review the result, and commit it "
            "(RULE 16.1)",
        )
        return report

    old_text = lock_path.read_text(encoding="utf-8")
    report.check(f"comparing committed {LOCK_FILENAME} against the schematic's current connectivity")
    if old_text == new_text:
        report.check(f"{lock_path} matches - no connectivity change")
        return report

    old_lock = json.loads(old_text)
    for line in _diff(old_lock, new_lock):
        report.fail("connectivity_regression", line)
    if not report.violations:
        # The lock differs byte-for-byte (e.g. reordered-but-equal content, which
        # shouldn't happen given the sort above, but _diff finding nothing is the
        # real signal something in normalization itself is non-deterministic) -
        # never let a byte diff with no reported cause look like a pass.
        report.fail(
            "connectivity_lock_churn",
            f"{lock_path} differs from the freshly regenerated lock but no semantic difference was found - "
            "normalization is non-deterministic; this is a bug in pcb-gate netlist, not a design change",
        )

    return report
