"""Project file discovery, shared by every pcb-gate subcommand."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class ProjectError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectFiles:
    project_dir: Path
    base_name: str
    pro_file: Path
    sch_file: Path
    pcb_file: Path
    dru_file: Path
    capability_file: Path


def discover(project_dir: str | Path) -> ProjectFiles:
    """Locate the single .kicad_pro in `project_dir` and its sibling files.

    Mirrors the "Discover project files" step in pcb-fab-gate.yml so both the
    workflow and this CLI agree on what a project is.
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise ProjectError(f"project_dir '{project_dir}' does not exist")

    pro_files = sorted(project_dir.glob("*.kicad_pro"))
    if len(pro_files) != 1:
        raise ProjectError(
            f"expected exactly one .kicad_pro in '{project_dir}', found {len(pro_files)}"
        )
    pro_file = pro_files[0]
    base_name = pro_file.stem

    sch_file = project_dir / f"{base_name}.kicad_sch"
    pcb_file = project_dir / f"{base_name}.kicad_pcb"
    for f in (sch_file, pcb_file):
        if not f.is_file():
            raise ProjectError(f"missing expected project file: {f}")

    return ProjectFiles(
        project_dir=project_dir,
        base_name=base_name,
        pro_file=pro_file,
        sch_file=sch_file,
        pcb_file=pcb_file,
        dru_file=project_dir / f"{base_name}.kicad_dru",
        capability_file=project_dir / "pcb-capability.yml",
    )


def load_kicad_pro(pro_file: Path) -> dict:
    with open(pro_file, encoding="utf-8") as f:
        return json.load(f)
