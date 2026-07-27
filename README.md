# pcb-fab-gate

The reusable KiCad PCB fabrication gate for every SVW / GNI / Helios board
repo, callable from any GitHub org at a version tag. Enforces
[`SVW_PCB_DESIGN_AND_FAB_GATE.md`](https://github.com/svw-platform/svw-governance/blob/main/specs/SVW_PCB_DESIGN_AND_FAB_GATE.md)
(SVW-0034), the portfolio-wide PCB design and fab-gate policy.

**A board goes to fab only as the `fab-*` artifact of a green run of this
gate.** Never hand-exported Gerbers.

Origin: this gate's checker design (`pcb-gate/`) was authored under GNI-0283.
It lived in `gamename-infrastructure/gni-governance` until
[SVW-0036](https://github.com/svw-platform/svw-governance/blob/main/briefs/2026-07-26-SVW0036-pcb-fab-gate-all-boards.md)
moved it here — a private repo's reusable workflow cannot be called from a
different GitHub org, and four of the eight board repos live outside
`gamename-infrastructure`. This repo is public and callable from anywhere so
one gate covers every org.

## What the gate checks

`kicad-cli pcb drc` on its own only checks a fraction of what SVW-0034
requires: it doesn't evaluate rule areas / keepouts at all, and nothing
confirms its own rule configuration is actually armed (a zeroed netclass
clearance or a mis-scoped custom rule produces a clean report
indistinguishable from a genuinely clean board). This gate closes those gaps.

Steps run in this order, all before export, all failing the job on
violation:

| Step | Implements | What it does |
|---|---|---|
| **Arm** (`pcb-gate arm`) | RULE 1.2 / GATE 2 | Reads the project's `.kicad_pro` and asserts every netclass clearance/track width is non-zero, the board's rule floors and severities are set (including `items_not_allowed`, `track_dangling`, `via_dangling`, `missing_courtyard` as of SVW-0037), `pcb-capability.yml` exists and is fresh (≤180 days), and any DRC exclusion present is declared in `pcb-capability.yml` — never silent. |
| **Canary** (`pcb-gate canary`) | GATE 1 | Injects one known defect at a time into a throwaway copy of the board (a same-layer short, a below-minimum track width, a deleted connection, a via inside the configured keepout rule area, a footprint deleted so the board no longer matches the schematic, a falsified `.kicad_dru` assertion) and confirms the relevant checker reports it. If a canary comes back clean, the gate itself is broken — exits non-zero naming which one. |
| **ERC** (`kicad-cli sch erc`) | GATE 4 / RULE 8.3 | Schematic electrical rules, `--severity-all`. |
| **DRC** (`kicad-cli pcb drc`) | GATE 4 | Board design rules: `--severity-all --exit-code-violations --all-track-errors --schematic-parity --refill-zones`. All three of GATE 4's required checks (violations, unconnected items, schematic parity), at full reporting completeness, against freshly-filled zones — a script that reads only violations is reporting on a fraction of the board (SVW-0037 Defect 1). |
| **Keepout** (`pcb-gate keepout`) | GATE 6 | `kicad-cli` does not enforce rule areas from the CLI at all. This checker parses the caller-configured rule area(s) (`keepout_zone_name`, default `ANT_KEEPOUT`; comma-separated list accepted) directly and tests every track, via, pad, and **filled zone pour** on every copper layer for intersection — the filled-pour case is the actual "copper poured into the antenna clearance" failure and the reason this check exists. A board that declares `rf_board: true` and has no matching rule area **fails** (`missing_keepout_on_rf_board`) rather than skipping — SVW-0037 Defect 2: a hard-coded name match meant a real board's non-default keepout zone skipped silently, and the skip printed identically to a pass. Refills zones in a throwaway copy first (`kicad-cli pcb drc --refill-zones`) so stale committed fills don't hide a live intrusion; if `kicad-cli` is unavailable it falls back to the committed fill and says so loudly (a GATE 12 human-review item). |
| **Overlap** (`pcb-gate overlap`) | GATE 5 / RULE 8.1 | Independent same-layer clearance check. Uses this package's own hand-rolled `.kicad_pcb` parser (`sexp.py`) and `shapely` — no `pcbnew`, no `kiutils`, nothing from KiCad's DRC engine — so a regression in the project's own rule configuration can't silently re-hide a short. Disagreements with KiCad DRC are expected on a first run (net-ties, pad-shape approximations) and are root-caused in writing per RULE 8.1, never suppressed. |

**A skip is not a pass.** Every report distinguishes a benign skip (correctly
not applicable — e.g. a non-RF board with no keepout zone) from a *blocking*
skip (a check that couldn't run at all — e.g. a schematic that failed to
load for the parity canary). A report with zero checks performed prints
`INCONCLUSIVE` and exits non-zero, never `PASS` — a check that examined
nothing is not the same thing as a check that passed (SVW-0037 Defect 2/3).

Every `pcb-capability.yml` field and value is validated against the fab's
**current, dated** capability sheet (RULE 4.1) — assumed or copied numbers
are not capability.

Export (Gerbers + drill) and the GitHub Release only happen if every step
above passed (`if: success()`), on a push to the caller's default branch.
The release attaches every check's JSON report plus a DigiKey-importable BOM
CSV when the schematic has Manufacturer/MPN fields populated.

## Caller snippet

Add `.github/workflows/fab-gate.yml` to the board repo:

```yaml
name: Fab Gate

on:
  pull_request:
    paths: ["**/*.kicad_*"]
  push:
    branches: [main]
    paths: ["**/*.kicad_*"]

jobs:
  fab-gate:
    uses: gamename/pcb-fab-gate/.github/workflows/gate.yml@v1.1.0
    permissions:
      contents: write
    with:
      project_dir: hardware/YourBoardName
      id_prefix: SVW   # or GNI, HEL, BB, C2DS — matches this repo's governance tag
      keepout_zone_name: ANT_KEEPOUT   # comma-separated if your board names it something else, or has more than one
      rf_board: false                 # true fails (not skips) the gate when no matching keepout zone is found
```

**Tag policy — `v1` is a moving pointer, not a version.** It always points
at the latest `v1.x.y` release so existing callers keep getting non-breaking
fixes without a PR. That is exactly the property SVW-0037 exists to warn
about: a caller pinned to `@v1` gets a **behaviorally different** gate the
moment `v1` is retagged, with no commit in the caller's own repo to explain
why (this happened five times during the SVW-0036 rollout). **A caller that
wants reproducible gate behaviour pins a specific tag (`@v1.1.0`) or a
commit SHA, never `@v1` or `@main`.** `@main` remains categorically
forbidden regardless — cross-org callers cannot see this repo's commit
history, so a moving `main` would change every caller's gate with no tag at
all to even retag from.

`contents: write` is required even though the caller only reads its own
board files — reusable-workflow permissions are capped by the caller's
grant, and a caller stuck on `contents: read` will pass the gate and then
403 on the release job.

Each caller also needs a `pcb-capability.yml` in `project_dir`, written from
the fab's own current published capability sheet with a real retrieval date:

```yaml
fab: JLCPCB
retrieved: 2026-07-26          # date the capability sheet was read
source: https://...            # the page the numbers came from
stackup: JLC04161H-7628        # the code being ordered
constraints:
  min_track_width_mm: 0.127
  min_clearance_mm: 0.127
  min_annular_ring_mm: 0.13
  min_drill_mm: 0.2
  min_silk_line_width_mm: 0.153
  min_silk_text_height_mm: 1.0
  edge_clearance_mm: 0.3
declared_exclusions:           # optional; empty/omit if none
  - rule: unconnected_items
    reason: carrier-board DevKit GPIOs intentionally unconnected
```

## Running locally before pushing

```bash
pip install ./pcb-gate
pcb-gate arm --project-dir hardware/YourBoardName
pcb-gate canary --project-dir hardware/YourBoardName \
  --keepout-zone-name ANT_KEEPOUT     # needs kicad-cli on PATH
pcb-gate keepout --project-dir hardware/YourBoardName \
  --keepout-zone-name ANT_KEEPOUT --rf-board
pcb-gate overlap --project-dir hardware/YourBoardName
```

`--keepout-zone-name` accepts a comma-separated list and defaults to
`ANT_KEEPOUT`; pass whatever your board's rule area(s) are actually named.
`--rf-board` (accepted by `keepout` only) turns "no matching rule area
found" from a skip into a failure — set it for any board that is actually
RF.

Each subcommand prints what it checked and exits non-zero on any violation.
`canary` and the zone-refill path in `keepout` shell out to `kicad-cli`; run
them inside the `kicad/kicad:10.0` container (or with KiCad 10 installed
locally) to get the same result CI does.

## Development

```bash
pip install -e "./pcb-gate[test]"
cd pcb-gate && pytest
```

Tests use small synthetic `.kicad_pcb`/`.kicad_pro` fixtures (see
`pcb-gate/tests/conftest.py`), not real boards, and don't require
`kicad-cli` — tests that do skip gracefully when it's absent.

## What this repo is (and isn't)

This repo is a KiCad DRC harness: the `pcb-gate` Python package and the
reusable workflow. No board files, no schematics, no product information,
no credentials — publishing it leaks nothing.
