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

## Opening the result

Open the `.3mf` in Bambu Studio (File > Open Project). It shows the system
presets (e.g. `0.20mm Standard @BBL X1C`) marked as modified, with your
changes highlighted in the parameter tabs, ready to slice and send. If Bambu
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
