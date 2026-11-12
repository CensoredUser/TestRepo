# Build spec reference

`bambu3mf build spec.json -o out.3mf` turns a JSON spec into a Bambu Studio
project. Unknown fields are rejected (typos fail loudly). Relative mesh paths
are resolved against the spec file's folder.

## Top level

| Field | Type | Meaning |
|---|---|---|
| `printer` | string, **required** | Printer model or alias (`"X1C"`, `"P1S"`, `"A1 mini"`, `"H2D"`, …) or an exact machine preset name (`"Bambu Lab X1 Carbon 0.4 nozzle"`) or one of your own printer presets. |
| `nozzle` | number | Nozzle diameter, default `0.4`. |
| `process` | string | Process preset: exact name, a prefix (`"0.20mm Standard"`), or a layer height (`"0.12"`). Default: the printer's default. |
| `filaments` | list | One entry per AMS slot / filament, in order. Each is a preset name (`"Bambu PLA Basic"`, `"Generic PETG"`, printer suffix optional) or `{"preset": …, "color": "#RRGGBB", "settings": {…}}`. Default: the printer's default filament. |
| `bed_type` | string | `"Cool Plate"`, `"Engineering Plate"`, `"High Temp Plate"`, `"Textured PEI Plate"`, `"Supertack Plate"`. Default: the printer model's default. |
| `settings` | object | Overrides routed automatically to process / printer / filament (all filaments) / project. |
| `process_settings`, `machine_settings`, `filament_settings`, `project_settings` | object | Same, forced into one section. |
| `app_version` | string | Bambu Studio version written into the file. Default: the version the profiles belong to. Never claim a newer version than the user runs. |
| `preset_files` | list | Extra user preset JSON files to load (then usable by name). |
| `metadata` | object | `title`, `designer`, `description`, `copyright`, `license`, `origin`. |
| `arrange` | object | `spacing` (mm, default 6) and `allow_new_plates` (default true). |
| `objects` | list | Objects (single plate; overflow spills onto new plates). |
| `plates` | list | Alternative to `objects`: `[{"name", "bed_type", "print_sequence": "by layer"/"by object", "spiral_mode", "locked", "objects": […]}]`. |

### Setting values

* Numbers, booleans, strings and lists are all accepted; they are converted to
  Bambu Studio's string form (`true` → `"1"`, `20` for a percent option → `"20%"`).
* A scalar for a vector option is broadcast. Printers with several nozzle
  variants (e.g. X1C: *Standard* and *High Flow*) store speeds as 2-element
  lists; `"inner_wall_speed": 200` sets both, `[200, 300]` sets each.
* Values are validated against the option schema (type, enum, min/max).
  Use `bambu3mf search <words>` to find keys and allowed values.

## Objects

| Field | Meaning |
|---|---|
| `name` | Object name (default: mesh file name). |
| `mesh` | File path (`.stl`, `.obj`, `.3mf`) or a primitive: `{"box": [x, y, z]}`, `{"cylinder": {"radius", "height", "segments", "top_radius"}}`, `{"cone": {"radius", "height"}}`, `{"sphere": {"radius"}}`. Boxes start at the origin; cylinders/cones/spheres are centred on X/Y and stand on Z=0. |
| `rotate` | `[rx, ry, rz]` degrees, applied X, then Y, then Z. |
| `scale` | number or `[sx, sy, sz]`. |
| `position` | `[x, y]` centre of the footprint on its plate (bed coordinates, e.g. 0–256 on an X1C). Omit to auto-arrange. Objects are always dropped onto the bed. |
| `copies` | Number of instances (default 1). |
| `instances` | Explicit `[[x, y], …]` positions instead of `copies`. |
| `filament` | 1-based filament (AMS slot) for the object. |
| `settings` | Per-object **process** overrides (e.g. `wall_loops`, `sparse_infill_density`). |
| `parts` | Extra parts: `{"name", "mesh", "type", "offset": [x,y,z], "rotate", "scale", "settings", "filament"}`. `type` is `normal_part` (default), `modifier_part`, `negative_part`, `support_enforcer` or `support_blocker`. `offset` is relative to the object's main mesh coordinates. |
| `modifiers` | Like `parts` but `type` defaults to `modifier_part`. |
| `height_ranges` | `[{"min_z", "max_z", "settings"}]` height-range modifiers (Z in mm from the bed). |
| `plate` | 1-based plate (only in the top-level `objects` list). |
| `printable` | `false` to keep an object in the project without printing it. |

## Example

```json
{
  "printer": "X1C",
  "process": "0.20mm Standard",
  "filaments": [{"preset": "Bambu PLA Basic", "color": "#1E88E5"}],
  "settings": {"wall_loops": 3, "sparse_infill_pattern": "gyroid"},
  "objects": [
    {"name": "bracket", "mesh": "bracket.stl", "rotate": [90, 0, 0], "copies": 2,
     "modifiers": [{"mesh": {"box": [10, 10, 10]}, "offset": [5, 5, 0],
                    "settings": {"sparse_infill_density": "80%"}}]}
  ]
}
```
