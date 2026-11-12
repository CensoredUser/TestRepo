# bambu3mf

Generate **Bambu Studio project files (.3mf)** and **print presets** that open in
Bambu Studio exactly as if Bambu Studio had saved them: same archive layout,
same IDs, same preset references, and every tuned setting preserved. Built so
that Claude (or any script) can turn "print this, strong, in PETG on my P1S"
into a ready-to-slice project.

* Uses Bambu Lab's **official system profiles** (every printer, nozzle and
  filament Bambu Studio ships), with inheritance resolved the way Bambu Studio
  does it.
* Writes the complete `project_settings.config`, plus the
  `different_settings_to_system` bookkeeping Bambu Studio uses to decide which
  values to keep. Leave a key off that list and Bambu Studio silently reverts it.
* **Embeds the print's own named profile** in the 3MF (e.g. `Shelf bracket - PLA`):
  a process preset and filament presets stored as project presets that inherit
  from Bambu's system presets. They show up in Bambu Studio's dropdowns and are
  selected when the project opens.
* **Material variants**: one file can carry a tuned process + filament pair per
  material (`- PLA`, `- PETG`, `- ASA`, …). Switch both dropdowns to the other
  variant in Bambu Studio and the print is retuned for that material.
* Supports per-object settings, modifiers (incl. negative parts and support
  enforcers/blockers), height-range modifiers, multiple filaments/AMS slots,
  multiple plates, per-plate print sequence and bed type, copies, auto-arrange,
  STL/OBJ/3MF input and primitives.
* Reads existing Bambu Studio projects (including MakerWorld downloads), changes
  settings, and writes them back with painting, custom G-code and variable
  layer heights preserved.
* Exports user presets (`File > Import > Import Configs…`).
* Validates every file against the rules Bambu Studio's loader relies on.
* Pure Python 3.9+, no dependencies.

**What it is not:** a slicer. Bambu Studio still turns the project into
G-code. Re-implementing its slicing engine adds nothing here: the project file
fully determines what Bambu Studio slices.

## Install

```bash
pip install -e .            # or just run: python -m bambu3mf ...
bambu3mf profiles fetch     # official BBL profiles + option schema (~3 s, git sparse clone)
```

If Bambu Studio is installed on the same machine, its own system profiles
(which match your installed version exactly) are used automatically, as are
your own saved presets. To pin profiles to your Bambu Studio version
explicitly: `bambu3mf profiles fetch --ref v02.04.00.70` (use your version's tag).

## Quick start

```bash
bambu3mf printers                                   # models and nozzle sizes
bambu3mf presets --printer P1S --type filament --filter PETG
bambu3mf search "infill density" --printer P1S      # find setting keys + current values
bambu3mf build examples/calibration_cube.json -o cube.3mf
bambu3mf inspect cube.3mf --values                  # what changed vs. the system presets
bambu3mf edit cube.3mf --set wall_loops=4 --set sparse_infill_density=30%
bambu3mf edit cube.3mf --filament 1 --set nozzle_temperature=225
bambu3mf edit cube.3mf --object cube --set top_shell_layers=6
bambu3mf export-preset --type process --printer P1S --base "0.20mm Standard" \
        --name "P1S strong 0.20" --set wall_loops=4 --set sparse_infill_density=35%
bambu3mf validate cube.3mf
```

Material variants (see `examples/bracket_material_variants.json`):

```json
{
  "printer": "X1C",
  "profile": "Shelf bracket",
  "settings": {"wall_loops": 4, "sparse_infill_density": "35%"},
  "variants": [
    {"name": "PLA",  "filament": "Bambu PLA Basic"},
    {"name": "PETG", "filament": "Bambu PETG HF", "filament_settings": {"fan_max_speed": 50}},
    {"name": "ASA",  "filament": "ASA", "settings": {"brim_type": "outer_only"}}
  ],
  "objects": [{"name": "bracket", "mesh": "bracket.stl"}]
}
```

```bash
bambu3mf edit bracket.3mf --preset "Shelf bracket - PETG" --set nozzle_temperature=245   # tune one variant
bambu3mf edit bracket.3mf --all-variants --set wall_loops=5                              # tune every variant
bambu3mf extract-presets bracket.3mf -o presets/                                         # keep the variants as user presets
```

A spec looks like this (full reference: [docs/SPEC.md](docs/SPEC.md)):

```json
{
  "printer": "X1C",
  "process": "0.20mm Standard",
  "filaments": [{"preset": "Bambu PLA Basic", "color": "#1E88E5"}],
  "settings": {"wall_loops": 3, "sparse_infill_pattern": "gyroid"},
  "objects": [{"name": "bracket", "mesh": "bracket.stl", "copies": 2}]
}
```

Python API:

```python
from bambu3mf import ProfileLibrary, build_project, write_3mf, read_project
lib = ProfileLibrary.open()
project, builder = build_project(spec, lib)
write_3mf(project, "out.3mf")
```

## Using it in claude.ai chats

The tool can be packaged as a claude.ai **skill**. Claude then runs it in the
chat's code sandbox whenever you ask for a print; no network access is needed.

1. Build the skill (bundles the tool, Bambu's profiles and your printer setup):
   ```bash
   python tools/build_claude_skill.py --ref v02.04.00.70 \
       --printer P1S --nozzle 0.4 --plate "Textured PEI Plate" \
       --filaments "AMS 1: Bambu PLA Basic white, 2: Bambu PETG HF black" --studio-version 02.04.00.70
   ```
   This writes `dist/bambu-print.zip` (~4–5 MB). `--ref` should be your Bambu
   Studio version's git tag; without it, the profiles already on this machine are bundled.
2. In claude.ai, turn on code execution ("Create and edit files" under
   Settings > Capabilities; on Team/Enterprise plans an owner enables it).
3. Customize > Skills > **+** > Create skill > **Upload a skill**, choose
   `bambu-print.zip`, and make sure the skill is switched on.
4. In any chat, attach your STL/3MF and ask, e.g. *"Print this bracket strong,
   PLA and PETG versions."* Claude builds, validates and hands you the `.3mf`.

Rebuild and re-upload the zip after updating the tool, your setup or your
Bambu Studio version.

## Opening the result

Open the `.3mf` in Bambu Studio (File > Open Project). The process and
filament dropdowns show the project's own presets (e.g. `Shelf bracket - PLA`),
derived from the Bambu system presets; the parameter tabs show what differs
from the system preset. To print another variant, select `Shelf bracket - PETG`
in the filament slot **and** in the process dropdown. Bambu Studio doesn't
switch the process automatically. To keep a variant for other prints, save it
as a user preset in Bambu Studio, or use `bambu3mf export-preset`. If Bambu
Studio shows a "newer version" dialog, build with
`"app_version": "<your version>"` or fetch the profiles for your version.

## Docs

* [docs/SPEC.md](docs/SPEC.md): build spec reference
* [docs/TUNING.md](docs/TUNING.md): the setting keys that matter, with rules of thumb
* [docs/FORMAT.md](docs/FORMAT.md): how Bambu Studio reads a project 3MF and the pitfalls

## Tests

```bash
python -m unittest discover -s tests       # offline tests use synthetic fixtures
```
Integration tests run automatically when profiles are available.

## License note

Bambu Lab's profiles and Bambu Studio sources are AGPL-3.0 and are **not**
included in this repository. They are downloaded (or read from your Bambu Studio
installation) at run time.
