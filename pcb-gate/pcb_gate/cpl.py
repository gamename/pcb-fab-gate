"""SVW-0070 - JLCPCB-ready assembly BOM + CPL, and the check that proves them.

For a fabricator-assembled board the order package is Gerber/drill zip +
assembly BOM + CPL (pick-and-place). The gate generates the last two from the
same commit as the Gerbers, so the hand-export from KiCad (unchecked,
unreproducible) goes away.

CPL  - `kicad-cli pcb export pos --format csv --units mm --side both
       --exclude-dnp [--smd-only]`, reduced to JLCPCB's columns
       `Designator,Mid X,Mid Y,Layer,Rotation`. KiCad's own "exclude from
       position files" footprint attribute is honoured by kicad-cli. Footprints
       flagged exclude-from-BOM are dropped too (a part that is not on the BOM is
       not assembled, and JLCPCB rejects a CPL row with no BOM line).
       Mid X / Mid Y are KiCad's absolute board coordinates, y-UP (so a part at
       y=110 in the .kicad_pcb is Mid Y=-110): every kicad-cli export defaults to
       the absolute page origin and ignores the board's `useauxorigin` setting
       (empirically confirmed on KiCad 10.0.6, SVW-0070), so Gerbers, drill and CPL
       share one origin as long as none of them is given an origin flag.
BOM  - `kicad-cli sch export bom` per-symbol rows, grouped by Value + Footprint +
       LCSC into `Comment,Designator,Footprint,LCSC Part #`. DNP excluded; scope
       limited to the designators in the CPL (same THT/SMD scope).
Check - `pcb-gate cpl` fails the gate unless (1) the BOM and CPL designator sets
       are identical, (2) every CPL row matches an INDEPENDENT re-derivation from
       the .kicad_pcb (pure parse of footprint position/side/rotation/attributes,
       sharing no code with `kicad-cli pos`) within 0.01 mm / 0.1 deg, and
       (3) a canary proves both comparisons can fail (SVW-0034 RULE 1.1).
"""
from __future__ import annotations

import copy
import csv
import io
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import kicad_tools, sexp
from .layers import footprint_reference
from .netlist import natural_key
from .project import ProjectFiles
from .report import Report

CPL_HEADER = ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
BOM_HEADER = ["Comment", "Designator", "Footprint", "LCSC Part #"]
LCSC_FIELD = "LCSC"
OVERRIDES_FILE = Path(__file__).with_name("jlc_rotation_overrides.csv")

POS_TOL_MM = 0.01
ROT_TOL_DEG = 0.1

CPL_OUT = "jlc_cpl.csv"
BOM_OUT = "jlc_bom.csv"
SUMMARY_OUT = "jlc_summary.json"


class CplError(RuntimeError):
    pass


# --- rotation overrides -----------------------------------------------------


@dataclass(frozen=True)
class RotationOverride:
    pattern: re.Pattern
    degrees: float
    note: str


def load_overrides(path: Path | None = None) -> list[RotationOverride]:
    """Rows of (footprint-name regex, degrees offset, note). Empty by default.

    Entries are added only when a real JLCPCB placement preview shows a wrong
    orientation - never from memory. `re.search` against the footprint name
    (library prefix stripped); anchor with ^...$ for an exact match. The first
    matching row wins.
    """
    path = path or OVERRIDES_FILE
    out: list[RotationOverride] = []
    if not path.is_file():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        pat = (row.get("footprint_regex") or "").strip()
        if not pat:
            continue
        out.append(
            RotationOverride(re.compile(pat), float(row["degrees_offset"]), (row.get("note") or "").strip())
        )
    return out


def norm_angle(deg: float) -> float:
    return deg % 360.0


def apply_override(footprint_name: str, rotation: float, overrides: list[RotationOverride]) -> float:
    """Rotation with the first matching override offset applied, wrapped to [0, 360)."""
    for ov in overrides:
        if ov.pattern.search(footprint_name):
            return norm_angle(rotation + ov.degrees)
    return norm_angle(rotation)


def angle_diff(a: float, b: float) -> float:
    d = abs(norm_angle(a) - norm_angle(b))
    return min(d, 360.0 - d)


# --- CPL from kicad-cli pos -------------------------------------------------


def transform_pos(pos_csv: str, overrides: list[RotationOverride], skip_refs: set[str] = frozenset()) -> list[dict]:
    """KiCad `pos --format csv` -> JLCPCB CPL rows (dicts keyed by CPL_HEADER)."""
    rows = []
    for r in csv.DictReader(io.StringIO(pos_csv)):
        ref = r["Ref"].strip()
        if ref in skip_refs:
            continue
        side = r["Side"].strip().lower()
        if side not in ("top", "bottom"):
            raise CplError(f"{ref}: unexpected Side {r['Side']!r} in kicad-cli pos output")
        rot = apply_override(r["Package"], float(r["Rot"]), overrides)
        rows.append(
            {
                "Designator": ref,
                "Mid X": f"{float(r['PosX']):.6f}",
                "Mid Y": f"{float(r['PosY']):.6f}",
                "Layer": "Top" if side == "top" else "Bottom",
                "Rotation": f"{rot:.6f}",
            }
        )
    rows.sort(key=lambda r: natural_key(r["Designator"]))
    return rows


# --- independent re-derivation from the .kicad_pcb --------------------------


def _fp_name(lib_id: str) -> str:
    return lib_id.split(":", 1)[-1]


def _fp_flags(fp: sexp.Node) -> set[str]:
    attr = sexp.child(fp, "attr")
    return {str(a) for a in attr[1:] if isinstance(a, str)} if attr else set()


def pcb_footprints(root: sexp.Node) -> list[dict]:
    out = []
    for fp in sexp.children(root, "footprint"):
        at = sexp.child(fp, "at")
        layer = sexp.text_of(sexp.child(fp, "layer"))
        if at is None or len(fp) < 2:
            continue
        out.append(
            {
                "ref": footprint_reference(fp),
                "name": _fp_name(str(fp[1])),
                "x": sexp.as_float(at[1]),
                "y": sexp.as_float(at[2]),
                "angle": sexp.as_float(at[3]) if len(at) > 3 else 0.0,
                "layer": layer,
                "flags": _fp_flags(fp),
            }
        )
    return out


def in_assembly_scope(fp: dict, include_tht: bool) -> bool:
    flags = fp["flags"]
    if flags & {"dnp", "exclude_from_pos_files", "exclude_from_bom"}:
        return False
    return include_tht or "smd" in flags


def excluded_from_bom_refs(root: sexp.Node) -> set[str]:
    return {fp["ref"] for fp in pcb_footprints(root) if "exclude_from_bom" in fp["flags"]}


def derive_cpl(root: sexp.Node, include_tht: bool, overrides: list[RotationOverride]) -> list[dict]:
    """Expected CPL, read straight from the board file. y is negated (KiCad pos is y-up)."""
    rows = []
    for fp in pcb_footprints(root):
        if not in_assembly_scope(fp, include_tht):
            continue
        rows.append(
            {
                "Designator": fp["ref"],
                "Mid X": fp["x"],
                "Mid Y": -fp["y"],
                "Layer": "Bottom" if fp["layer"] == "B.Cu" else "Top",
                "Rotation": apply_override(fp["name"], fp["angle"], overrides),
            }
        )
    return rows


# --- BOM from kicad-cli sch export bom --------------------------------------


def build_bom(raw_rows: list[dict], scope_refs: set[str]) -> list[dict]:
    """Group per-symbol rows by Value + Footprint + LCSC (JLCPCB headers)."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in raw_rows:
        ref = (r.get("Reference") or "").strip()
        if not ref or ref not in scope_refs:
            continue
        key = (
            (r.get("Value") or "").strip(),
            _fp_name((r.get("Footprint") or "").strip()),
            (r.get(LCSC_FIELD) or "").strip(),
        )
        groups.setdefault(key, []).append(ref)
    rows = []
    for (value, footprint, lcsc), refs in groups.items():
        refs = sorted(set(refs), key=natural_key)
        rows.append(
            {"Comment": value, "Designator": ",".join(refs), "Footprint": footprint, "LCSC Part #": lcsc}
        )
    rows.sort(key=lambda r: natural_key(r["Designator"].split(",")[0]))
    return rows


def bom_designators(bom_rows: list[dict]) -> list[str]:
    return [d.strip() for r in bom_rows for d in r["Designator"].split(",") if d.strip()]


# --- the check --------------------------------------------------------------


def compare(cpl_rows: list[dict], bom_rows: list[dict], derived: list[dict]) -> list[tuple[str, str]]:
    """Every disagreement between CPL, BOM and the independent derivation, as (code, message)."""
    problems: list[tuple[str, str]] = []
    cpl_refs = [r["Designator"] for r in cpl_rows]
    bom_refs = bom_designators(bom_rows)
    for name, refs in (("CPL", cpl_refs), ("BOM", bom_refs)):
        dups = sorted({r for r in refs if refs.count(r) > 1}, key=natural_key)
        if dups:
            problems.append(("cpl_duplicate_designator", f"{name} lists designator(s) more than once: {', '.join(dups)}"))
    cpl_set, bom_set = set(cpl_refs), set(bom_refs)
    for ref in sorted(cpl_set - bom_set, key=natural_key):
        problems.append(("cpl_part_missing_from_bom", f"{ref} is placed in the CPL but is not in the BOM"))
    for ref in sorted(bom_set - cpl_set, key=natural_key):
        problems.append(("bom_part_missing_from_cpl", f"{ref} is in the BOM but is not placed in the CPL"))

    exp = {r["Designator"]: r for r in derived}
    got = {r["Designator"]: r for r in cpl_rows}
    for ref in sorted(set(exp) - set(got), key=natural_key):
        problems.append(("cpl_missing_placement", f"{ref} is placed on the board (per the .kicad_pcb) but absent from the CPL"))
    for ref in sorted(set(got) - set(exp), key=natural_key):
        problems.append(("cpl_extra_placement", f"{ref} is in the CPL but not an assembly-scope footprint in the .kicad_pcb"))
    for ref in sorted(set(exp) & set(got), key=natural_key):
        e, g = exp[ref], got[ref]
        if e["Layer"] != g["Layer"]:
            problems.append(("cpl_layer_mismatch", f"{ref}: CPL layer {g['Layer']}, board says {e['Layer']}"))
        for axis in ("Mid X", "Mid Y"):
            if abs(float(g[axis]) - float(e[axis])) > POS_TOL_MM:
                problems.append(
                    ("cpl_position_mismatch", f"{ref}: CPL {axis}={float(g[axis]):.4f}, board says {float(e[axis]):.4f} (tol {POS_TOL_MM} mm)")
                )
        if angle_diff(float(g["Rotation"]), float(e["Rotation"])) > ROT_TOL_DEG:
            problems.append(
                ("cpl_rotation_mismatch", f"{ref}: CPL rotation={float(g['Rotation']):.3f}, board says {float(e['Rotation']):.3f} (tol {ROT_TOL_DEG} deg)")
            )
    return problems


def _moved_copy(root: sexp.Node, ref: str) -> sexp.Node:
    """Throwaway copy of the board with footprint `ref` moved 1 mm in x."""
    clone = copy.deepcopy(root)
    for fp in sexp.children(clone, "footprint"):
        if footprint_reference(fp) == ref:
            at = sexp.child(fp, "at")
            at[1] = str(sexp.as_float(at[1]) + 1.0)
            return clone
    raise CplError(f"canary: footprint {ref} not found")


def run_canary(
    root: sexp.Node,
    cpl_rows: list[dict],
    bom_rows: list[dict],
    include_tht: bool,
    overrides: list[RotationOverride],
    report: Report,
) -> None:
    """SVW-0034 RULE 1.1: both comparisons must be able to fail, or the check has not passed."""
    if not cpl_rows:
        report.skip("canary: no assembly parts in scope - nothing to move or drop")
        return
    first = cpl_rows[0]["Designator"]

    moved = derive_cpl(_moved_copy(root, first), include_tht, overrides)
    if any(c == "cpl_position_mismatch" for c, _ in compare(cpl_rows, bom_rows, moved)):
        report.check(f"canary: moving {first} 1 mm in a throwaway board copy is detected")
    else:
        report.fail("cpl_canary_inert", f"canary moved {first} by 1 mm and the CPL-vs-board comparison did not fail")

    shrunk = []
    for r in bom_rows:
        keep = [d.strip() for d in r["Designator"].split(",") if d.strip() and d.strip() != first]
        if keep:
            shrunk.append({**r, "Designator": ",".join(keep)})
    if any(c == "cpl_part_missing_from_bom" for c, _ in compare(cpl_rows, shrunk, derive_cpl(root, include_tht, overrides))):
        report.check(f"canary: dropping {first} from the BOM is detected")
    else:
        report.fail("cpl_canary_inert", f"canary dropped {first} from the BOM and the BOM-vs-CPL comparison did not fail")


# --- kicad-cli wrappers -----------------------------------------------------


def _run(args: list[str]) -> None:
    if not kicad_tools.available():
        raise kicad_tools.KicadCliUnavailable(f"'{kicad_tools.KICAD_CLI}' not found on PATH")
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CplError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")


def export_pos(pcb_file: Path, out: Path, include_tht: bool) -> str:
    # Deliberately NO --use-drill-file-origin: Gerbers and drill are exported at
    # the absolute origin too, so all three share one origin (SVW-0070).
    args = [kicad_tools.KICAD_CLI, "pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp"]
    if not include_tht:
        args.append("--smd-only")
    _run(args + ["-o", str(out), str(pcb_file)])
    return out.read_text(encoding="utf-8")


def export_bom_raw(sch_file: Path, out: Path) -> list[dict]:
    _run(
        [
            kicad_tools.KICAD_CLI, "sch", "export", "bom",
            "--fields", f"Reference,Value,Footprint,{LCSC_FIELD}",
            "--labels", f"Reference,Value,Footprint,{LCSC_FIELD}",
            "--exclude-dnp",
            "-o", str(out), str(sch_file),
        ]
    )  # fmt: skip
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def run(files: ProjectFiles, out_dir: str | Path = "jlc", include_tht: bool = False) -> Report:
    report = Report(tool="pcb-gate cpl", project=files.base_name)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides()
    root = sexp.parse_file(files.pcb_file)

    pos_text = export_pos(files.pcb_file, out / "kicad_pos.csv", include_tht)
    cpl_rows = transform_pos(pos_text, overrides, excluded_from_bom_refs(root))
    bom_rows = build_bom(export_bom_raw(files.sch_file, out / "kicad_bom_raw.csv"), {r["Designator"] for r in cpl_rows})
    write_csv(out / CPL_OUT, CPL_HEADER, cpl_rows)
    write_csv(out / BOM_OUT, BOM_HEADER, bom_rows)
    report.check(f"generated CPL ({len(cpl_rows)} placement(s), {'SMD+THT' if include_tht else 'SMD only'}, {len(overrides)} rotation override(s)) and BOM ({len(bom_rows)} line(s))")

    derived = derive_cpl(root, include_tht, overrides)
    for code, msg in compare(cpl_rows, bom_rows, derived):
        report.fail(code, msg)
    report.check(
        f"BOM designator set == CPL designator set; CPL matches independent .kicad_pcb re-derivation "
        f"({len(derived)} part(s), {POS_TOL_MM} mm / {ROT_TOL_DEG} deg)"
    )
    run_canary(root, cpl_rows, bom_rows, include_tht, overrides, report)

    no_lcsc = sum(1 for r in bom_rows if not r["LCSC Part #"])
    summary = {
        "cpl_rows": len(cpl_rows),
        "bom_lines": len(bom_rows),
        "bom_lines_without_lcsc": no_lcsc,
        "note": f"{no_lcsc} of {len(bom_rows)} assembly lines have no LCSC number",
    }
    (out / SUMMARY_OUT).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"[pcb-gate cpl] {summary['note']}")
    return report
