# Tuning guide (Bambu Studio setting keys)

Start from the Bambu system preset for the printer, nozzle and filament, then
change as little as needed. Every key below exists in Bambu Studio 2.9. Check
types and allowed values with `bambu3mf search <key>`, and current values with
`bambu3mf search <key> --printer X1C`.

Sections: **P** = process (can also be set per object / modifier / height
range), **F** = filament (per filament), **M** = printer, **J** = project.

## Quality and layers
| Key | Sec | Notes |
|---|---|---|
| `layer_height` | P | Choose the process preset with the right height (`"process": "0.12"`) rather than overriding it: the presets also retune speeds. |
| `initial_layer_print_height` | P | 0.2 typical. |
| `line_width`, `outer_wall_line_width`, `inner_wall_line_width`, `top_surface_line_width` | P | mm or % of nozzle. |
| `wall_generator` | P | `classic` / `arachne` (arachne for thin or variable walls). |
| `seam_position` | P | `nearest`, `aligned`, `back`, `random`. |
| `seam_slope_type` | P | Scarf seam: `none`, `external`, `all`. |
| `top_surface_pattern` / `bottom_surface_pattern` | P | `monotonicline` gives clean tops. |
| `ironing_type` | P | `no ironing`, `top`, `topmost`, `solid`; plus `ironing_flow`, `ironing_spacing`. |
| `precise_outer_wall`, `xy_hole_compensation`, `xy_contour_compensation`, `elefant_foot_compensation` | P | Dimensional accuracy. |

## Strength
| Key | Sec | Notes |
|---|---|---|
| `wall_loops` | P | Walls add the most strength per gram; 3–4 for functional parts. |
| `top_shell_layers`, `bottom_shell_layers`, `top_shell_thickness` | P | |
| `sparse_infill_density` | P | `"15%"`. 20–40 % for functional parts. |
| `sparse_infill_pattern` | P | `grid`, `gyroid`, `cubic`, `adaptivecubic`, `lightning` (decorative), `crosshatch`, `zig-zag` (rectilinear), … |
| `wall_sequence` | P | `inner wall/outer wall`, `outer wall/inner wall` (more accurate), `inner-outer-inner wall`. |
| `ensure_vertical_shell_thickness` | P | |

## Speed and acceleration (vectors on multi-variant printers)
`outer_wall_speed`, `inner_wall_speed`, `sparse_infill_speed`,
`internal_solid_infill_speed`, `top_surface_speed`, `gap_infill_speed`,
`bridge_speed`, `initial_layer_speed`, `initial_layer_infill_speed`,
`travel_speed`, `default_acceleration`, `outer_wall_acceleration`,
`top_surface_acceleration`, `initial_layer_acceleration`,
`small_perimeter_speed`, `enable_overhang_speed` (all **P**).
The real limit is usually the filament's `filament_max_volumetric_speed`
(**F**, mm³/s). Slow the outer wall down for appearance, not the infill.

## Support and adhesion
| Key | Sec | Notes |
|---|---|---|
| `enable_support` | P | |
| `support_type` | P | `normal(auto)`, `tree(auto)`, `normal(manual)`, `tree(manual)`. Manual means enforcers only. |
| `support_style` | P | `default`, `grid`, `snug`, `tree_slim`, `tree_strong`, `tree_hybrid`, `tree_organic`. |
| `support_threshold_angle` | P | Overhang angle that gets support. |
| `support_on_build_plate_only` | P | |
| `support_top_z_distance`, `support_bottom_z_distance`, `support_interface_top_layers`, `support_interface_pattern` | P | Ease of removal versus underside quality. |
| `tree_support_branch_angle`, `tree_support_wall_count` | P | |
| `brim_type` | P | `no_brim`, `outer_only`, `inner_only`, `outer_and_inner`, `auto_brim`, `brim_ears`; plus `brim_width`, `brim_object_gap`. |
| `raft_layers`, `skirt_loops` | P | |
| `curr_bed_type` | J | Selects which plate temperature (below) is used. Set it with `bed_type` in the spec. |

## Temperatures, cooling, flow (filament)
| Key | Sec | Notes |
|---|---|---|
| `nozzle_temperature`, `nozzle_temperature_initial_layer` | F | |
| `cool_plate_temp`, `eng_plate_temp`, `hot_plate_temp`, `textured_plate_temp`, `supertack_plate_temp` (+ `_initial_layer`) | F | Bed temperature for each plate type. Change the one matching `curr_bed_type`. |
| `fan_min_speed`, `fan_max_speed`, `slow_down_layer_time`, `slow_down_min_speed`, `close_fan_the_first_x_layers`, `overhang_fan_speed`, `overhang_fan_threshold`, `additional_cooling_fan_speed` | F | PETG, ABS and ASA want less cooling than PLA. |
| `filament_flow_ratio` | F | From flow calibration (Bambu default about 0.98). |
| `enable_pressure_advance`, `pressure_advance` | F | Only when the user calibrated it; Bambu printers otherwise auto-calibrate. |
| `filament_max_volumetric_speed` | F | mm³/s cap on print speed. |
| `filament_retraction_length`, `filament_z_hop`, `filament_retraction_speed` | F | `nil` means "use the printer value". |

## Multi-material and special modes
`enable_prime_tower`, `prime_tower_width`, `wipe_tower_x`/`wipe_tower_y` (J,
per plate), `flush_multiplier` (J); `print_sequence` (`by layer` / `by
object`, per plate in the spec), `spiral_mode` (vase), `fuzzy_skin`
(`none`, `external`, `all`, `allwalls`) with `fuzzy_skin_thickness` and
`fuzzy_skin_point_distance`, `adaptive_layer_height`, `timelapse_type`
(`0` = instant, `1` = smooth).

## Rules of thumb
* Strength: more walls before more infill. Orient so layer lines don't carry
  the main load.
* Flat tops: `top_shell_layers` ≥ 5 at 0.12–0.16 mm layers, `monotonicline`,
  optionally `ironing_type: top`.
* Overhangs: tree supports (`tree(auto)`, `tree_organic`) remove more cleanly on
  organic shapes; normal supports suit flat undersides.
* Small or thin parts: arachne, slower outer walls, enough minimum layer time.
* Never edit the printer's G-code keys unless asked. Bambu Studio warns the
  user whenever a project changes printer or filament G-code.
