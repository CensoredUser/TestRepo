---
name: bambu-print
description: Create or tune Bambu Studio print projects (.3mf) and print profiles for a Bambu Lab printer. Use when the user wants something printed, wants a 3MF/project file, asks to optimise print settings (strength, speed, quality, supports, materials), or wants to change settings in an existing Bambu Studio project or export a preset.
---

# Bambu print projects with bambu3mf

Everything goes through `python -m bambu3mf` in this repository. Read
`CLAUDE.md` for the owner's printer, `docs/SPEC.md` for the spec format and
`docs/TUNING.md` before choosing settings.

## Steps
1. `python -m bambu3mf profiles`: profiles available? If not, run `profiles fetch`.
2. Clarify only what changes the result: printer/nozzle (if not in CLAUDE.md),
   material, and the goal (strength, looks, speed, fit). Assume sensible
   defaults for the rest and say so.
3. Choose presets. Process: the layer height for the goal (0.12–0.16 for
   detail, 0.20 standard, 0.24–0.28 drafts). Filament: list with
   `presets --printer <P> --type filament --filter <material>`.
4. Look up any key you aren't sure about: `search "<words>" --printer <P>`
   (shows type, allowed values, range and the current value).
5. Write the spec (JSON) next to the model files and run
   `build spec.json -o <name>.3mf --dry-run`, then without `--dry-run`.
6. Require `"validation": {"ok": true}`. Fix errors and address warnings.
7. Report: presets used, each changed setting with a one-line reason, and
   anything the user should check in Bambu Studio (supports, orientation).

## Modifying an existing project
`inspect file.3mf --values` → `edit file.3mf -o new.3mf --set key=value`
(add `--filament N` for one filament, `--object NAME` for per-object) →
`validate new.3mf`.

## Saving a reusable profile
`export-preset --type process|filament --printer <P> --base "<preset>" --name "<name>" --set k=v …`
→ the user imports the JSON via *File > Import > Import Configs…*.
