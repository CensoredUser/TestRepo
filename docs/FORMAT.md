# How Bambu Studio reads a project 3MF

Notes from reading Bambu Studio 02.09 (`src/libslic3r/Format/bbs_3mf.cpp`,
`src/slic3r/GUI/Plater.cpp`, `src/libslic3r/PresetBundle.cpp`,
`src/libslic3r/Preset.cpp`). `bambu3mf` follows them exactly.

## Archive layout
```
[Content_Types].xml                 rels, model, png, gcode defaults
_rels/.rels                         -> /3D/3dmodel.model + plate_1.png thumbnails
3D/3dmodel.model                    metadata, component objects, build items
3D/_rels/3dmodel.model.rels         one relationship per object file
3D/Objects/object_<n>.model         meshes; one <object> per part (volume)
Metadata/project_settings.config    JSON: the complete flattened config
Metadata/model_settings.config      XML: object/part names, per-object settings, plates
Metadata/layer_config_ranges.xml    height-range modifiers (1-based object index)
Metadata/process_settings_<n>.config  project-embedded presets (JSON diff + inherits)
Metadata/filament_settings_<n>.config
Metadata/machine_settings_<n>.config
Metadata/slice_info.config          client type/version header
Metadata/plate_<n>.png, plate_<n>_small.png
```

## Things that must be right
* **`<metadata name="Application">BambuStudio-<version></metadata>`**. Without
  the `BambuStudio-` prefix the file is treated as a foreign 3MF: settings and
  plates are ignored and only geometry is loaded.
* **Version**: if the version is newer than the running Bambu Studio (a
  different minor version), a "newer version" dialog appears. Write the
  user's version or an older one.
* **IDs**: for each object the part (volume) IDs come first, then the object
  ID: object 1 with two parts gets volumes 1, 2 and object id 3; the next
  object starts at 4. `p:UUID`s are `%08x` of the 1-based object index (or of
  `(index << 16) + part`) followed by fixed suffixes.
* **Transforms**: component and item `transform` attributes are 3×4,
  column-major (`m00 m10 m20 m01 … m23`). Meshes are stored centred on their
  bounding box; Bambu Studio recentres anyway (`modify_to_center_geometry`).
* **Plates** are not explicit containers for geometry. Plate *n* occupies
  `printable_area` shifted by `(col·W·1.2, −row·D·1.2)` with
  `cols = ceil(sqrt(plates))`, and instances are assigned to plates by
  position. `<plate>` entries in `model_settings.config` carry names, lock
  state, per-plate bed type and print sequence.
* **`project_settings.config`**: JSON, sorted keys, 4-space indent, every value
  a string or a list of strings. Missing keys are filled from Bambu Studio's
  built-in defaults.
* **Preset names**: `print_settings_id`, `printer_settings_id` and
  `filament_settings_id` must name presets that exist (system presets, or the
  user's own presets on their machine). Otherwise Bambu Studio creates "project"
  presets and warns about custom G-code.
* **`different_settings_to_system`** (n + 2 entries: process, each filament,
  printer; keys joined with `;`). On load, Bambu Studio *resets every key not
  listed here to the system preset's value*
  (`update_non_diff_values_to_base_config`). An override that is missing from
  this list is silently lost.
* **Filament vectors**: per-filament keys are concatenated across filaments.
  Keys with nozzle variants (e.g. `nozzle_temperature` on an X1C:
  `[Standard, High Flow]`) contribute one entry per variant, and
  `filament_self_index` maps each entry to its filament (`[1,1,2,2]`).
* **Per-object/part settings** in `model_settings.config` are print (process)
  options only, serialised like Bambu Studio's `opt_serialize` (vectors joined
  with commas).
* **Flush volumes**: `flush_volumes_matrix` is n×n per nozzle; Bambu Studio
  recomputes it when colours change. The colour formula is a port of
  `FlushVolCalc.cpp`; the minimum is the nozzle volume minus the volume
  pulled back by the long retraction on filament cut.
* **Project-embedded presets** (`Metadata/process_settings_<n>.config`, …) are
  JSON like `get_preset_differed_for_save()` writes them: `from: "project"`,
  `name`, `version`, `inherits` (an installed preset; embedded presets whose
  parent is missing are skipped), the id key (`print_settings_id` /
  `filament_settings_id: [name]`), the extruder-variant keys and the changed
  keys only. They are loaded *before* the project config, so pointing
  `print_settings_id` / `filament_settings_id` at an embedded name (with
  `inherits_group` = its parent and the diff keys listed) makes Bambu Studio
  select it unmodified. An embedded name that matches an existing preset is not
  loaded ("Preset already present").
