# CLAUDE.md

This repo is **bambu3mf**: a pure-Python tool that writes Bambu Studio
compatible project 3MFs and presets from Bambu Lab's system profiles. Its main
user is Claude, preparing optimised prints for the owner's printer.

## The owner's setup
<!-- Fill in once known; used as defaults for every print. -->
- Printer: _unknown, ask_ (model + nozzle, e.g. "P1S, 0.4 mm hardened")
- AMS / filaments loaded: _unknown_
- Usual plate: _unknown_ (Textured PEI / Cool Plate / …)
- Bambu Studio version: _unknown_ (Help > About; affects `app_version` / `profiles fetch --ref`)

## Workflow for a print request
1. Make sure profiles are available: `python -m bambu3mf profiles` (if this
   fails: `python -m bambu3mf profiles fetch`).
2. Pick presets: `python -m bambu3mf presets --printer <P> --type filament --filter <material>`.
3. Find keys and current values: `python -m bambu3mf search "<words>" --printer <P>`.
   Read `docs/TUNING.md` for what to change and why.
4. Write a spec JSON (see `docs/SPEC.md`, `examples/`), then
   `python -m bambu3mf build spec.json -o out.3mf`. Use `--dry-run` first to see
   the resolved presets and the changed keys.
5. The build output includes validation; it must say `"ok": true`. Fix any
   errors and read the warnings (off-bed objects, too tall, etc.).
6. Tell the user what you changed and why (the build summary lists every
   changed key per preset and per variant).

Every build embeds the print's own named profile (`"profile": "<name>"`).
Unless the user wants a single material, add `variants` for the materials the
print could reasonably be made in (usually PLA + PETG, plus ASA/ABS for
outdoor or heat use if the printer supports them). Shared tuning goes in the
top-level `settings`, material-specific tuning in each variant (see the
"Material variants" table in `docs/TUNING.md`).

For an existing project: `inspect` → `edit --set …` (active profile;
process/printer/filament), `edit --preset "<name>" --set …` (one embedded
variant), `edit --all-variants --set …` (every variant) or
`edit --object NAME --set …` (per object) → `validate`.

## Rules
- Never hand-edit `project_settings.config` inside a 3MF; use `edit` or a spec.
  Overrides have to be listed in `different_settings_to_system` or Bambu
  Studio reverts them, and the tool does that bookkeeping.
- Change as few settings as possible relative to the system preset, and prefer
  choosing the right process/filament preset over overriding many keys.
- Don't modify printer or filament G-code keys unless the user asks (Bambu
  Studio shows the user a warning).
- Per-object, modifier and height-range overrides accept **process** settings only.
- Positions are bed coordinates of the footprint centre (0..bed size); omit
  `position` to auto-arrange.
- This tool does not slice. Bambu Studio does. Don't claim print times or
  filament usage.

## Development
- Code: `bambu3mf/` (profiles.py → settings.py → spec.py/project.py → writer.py;
  reader.py, edit.py, validate.py, presets.py, cli.py).
- Tests: `python -m unittest discover -s tests` (offline fixtures in
  `tests/fixtures/profiles`; real-profile tests run when profiles are present).
- Format facts and Bambu Studio source references: `docs/FORMAT.md`.
