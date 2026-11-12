"""Option schema: names, types, enums, limits, labels and preset routing.

The schema is extracted from Bambu Studio's own source files
(``src/libslic3r/PrintConfig.cpp`` and ``src/libslic3r/Preset.cpp``) at the
same git ref as the profiles, so it always matches the profile set in use.
It is optional: without it the tool still works, but it can only route and
validate keys that appear in the resolved profiles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from difflib import get_close_matches
from pathlib import Path
from typing import Dict, Iterable, List, Optional

VECTOR_TYPES = {
    "coFloats", "coInts", "coStrings", "coPercents", "coFloatsOrPercents",
    "coPoints", "coBools", "coEnums", "coPointsGroups", "coIntsGroups",
}

# Options that live in the project (not in any preset); see
# PresetBundle.cpp s_project_options.
PROJECT_OPTIONS = [
    "flush_volumes_vector", "flush_volumes_matrix", "filament_colour",
    "filament_colour_type", "filament_multi_colour", "wipe_tower_x",
    "wipe_tower_y", "wipe_tower_rotation_angle", "curr_bed_type",
    "flush_multiplier", "flush_multiplier_fast", "nozzle_volume_type",
    "filament_map_mode", "filament_map", "filament_volume_map",
    "filament_nozzle_map", "filament_diameter_map", "multi_nozzle_diameter_mode",
    "extruder_nozzle_stats", "extruder_nozzle_stats_new", "prime_volume_mode",
    "extruder_nozzle_count", "extruder_nozzle_volume_type",
    "extruder_nozzle_diameter", "extruder_nozzle_slot",
    "enable_filament_dynamic_map", "filament_is_mixed",
    "filament_mixed_components", "filament_mixed_sublayer_ratios",
    "filament_mixed_gradient", "filament_mixed_gradient_range",
    "filament_mixed_gradient_curve", "filament_mixed_gradient_per_part",
    "has_filament_switcher",
]

# Keys that Bambu Studio treats as G-code (it warns when a project modifies
# them; see PresetBundle.cpp gcodes_key_set).
GCODE_KEYS = {
    "filament_end_gcode", "filament_start_gcode", "change_filament_gcode",
    "layer_change_gcode", "machine_end_gcode", "machine_pause_gcode",
    "machine_start_gcode", "template_custom_gcode", "printing_by_object_gcode",
    "before_layer_change_gcode", "time_lapse_gcode", "wrapping_detection_gcode",
}


@dataclass
class OptionDef:
    key: str
    type: str = ""
    nullable: bool = False
    label: str = ""
    category: str = ""
    tooltip: str = ""
    sidetext: str = ""
    mode: str = ""
    enum_values: List[str] = field(default_factory=list)
    enum_labels: List[str] = field(default_factory=list)
    enum_keys: List[str] = field(default_factory=list)   # every accepted value (enum key map)
    min: Optional[float] = None
    max: Optional[float] = None
    default: str = ""

    @property
    def is_vector(self) -> bool:
        return self.type in VECTOR_TYPES


class Schema:
    def __init__(self, options: Dict[str, OptionDef], print_keys: Iterable[str] = (),
                 filament_keys: Iterable[str] = (), printer_keys: Iterable[str] = ()):
        self.options = options
        self.print_keys = set(print_keys)
        self.filament_keys = set(filament_keys)
        self.printer_keys = set(printer_keys)
        self.project_keys = set(PROJECT_OPTIONS)

    # -- persistence -----------------------------------------------------
    @classmethod
    def empty(cls) -> "Schema":
        return cls({})

    @classmethod
    def load(cls, path: Path) -> "Schema":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        opts = {k: OptionDef(**v) for k, v in data.get("options", {}).items()}
        return cls(opts, data.get("print_keys", []), data.get("filament_keys", []),
                   data.get("printer_keys", []))

    def save(self, path: Path) -> None:
        data = {
            "options": {k: asdict(v) for k, v in sorted(self.options.items())},
            "print_keys": sorted(self.print_keys),
            "filament_keys": sorted(self.filament_keys),
            "printer_keys": sorted(self.printer_keys),
        }
        Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    # -- queries ---------------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self.options)

    def get(self, key: str) -> Optional[OptionDef]:
        return self.options.get(key)

    def section_of(self, key: str) -> Optional[str]:
        if key in self.print_keys:
            return "process"
        if key in self.filament_keys:
            return "filament"
        if key in self.printer_keys:
            return "machine"
        if key in self.project_keys:
            return "project"
        return None

    def suggest(self, key: str, extra: Iterable[str] = ()) -> List[str]:
        pool = set(self.options) | set(extra)
        return get_close_matches(key, sorted(pool), n=5, cutoff=0.6)

    def search(self, query: str) -> List[OptionDef]:
        q = query.lower()
        hits = []
        for opt in self.options.values():
            hay = " ".join([opt.key, opt.label, opt.category, opt.tooltip]).lower()
            if all(word in hay for word in q.split()):
                hits.append(opt)
        # rank: key match first, then label match
        hits.sort(key=lambda o: (q not in o.key.lower(), q not in o.label.lower(), o.key))
        return hits


# ---------------------------------------------------------------------------
# Extraction from Bambu Studio C++ sources
# ---------------------------------------------------------------------------

_ADD_RE = re.compile(r'def\s*=\s*this->add(_nullable)?\(\s*"([^"]+)"\s*,\s*(co\w+)\s*\)')
_STR_LIT = r'"((?:[^"\\]|\\.)*)"'


def _unescape(s: str) -> str:
    return (s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
             .replace("\\\\", "\\"))


def _string_expr(block: str, attr: str) -> str:
    """Return the concatenated string literal assigned to def->attr."""
    m = re.search(r"def->" + attr + r"\s*=\s*(.*?);\s*\n", block, re.S)
    if not m:
        return ""
    return _unescape("".join(re.findall(_STR_LIT, m.group(1))))


def _number(block: str, attr: str) -> Optional[float]:
    m = re.search(r"def->" + attr + r"\s*=\s*(-?[0-9.]+)\s*[fF]?\s*;", block)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _enum_key_maps(src: str) -> Dict[str, List[str]]:
    maps = {}
    for m in re.finditer(r"t_config_enum_values\s+s_keys_map_(\w+)\s*=?\s*\{(.*?)\};", src, re.S):
        body = re.sub(r"//[^\n]*", "", m.group(2))
        maps[m.group(1)] = re.findall(r'\{\s*"([^"]*)"\s*,', body)
    return maps


def extract_options(print_config_cpp: str) -> Dict[str, OptionDef]:
    options: Dict[str, OptionDef] = {}
    key_maps = _enum_key_maps(print_config_cpp)
    matches = list(_ADD_RE.finditer(print_config_cpp))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(print_config_cpp)
        block = print_config_cpp[m.end():end]
        # stop at the end of the defining function to avoid swallowing code
        key = m.group(2)
        if key in options:
            continue
        opt = OptionDef(key=key, type=m.group(3), nullable=bool(m.group(1)))
        opt.label = _string_expr(block, "label")
        opt.category = _string_expr(block, "category")
        opt.tooltip = _string_expr(block, "tooltip")
        opt.sidetext = _string_expr(block, "sidetext")
        mm = re.search(r"def->mode\s*=\s*(com\w+)", block)
        opt.mode = mm.group(1)[3:].lower() if mm else ""
        opt.enum_values = [_unescape(v) for v in
                           re.findall(r"def->enum_values\.(?:push_back|emplace_back)\(\s*" + _STR_LIT, block)]
        opt.enum_labels = [_unescape(v) for v in
                           re.findall(r"def->enum_labels\.(?:push_back|emplace_back)\(\s*(?:L\()?\s*" + _STR_LIT, block)]
        km = re.search(r"enum_keys_map\s*=\s*&(?:ConfigOptionEnum<(\w+)>::get_enum_values\(\)|s_keys_map_(\w+))", block)
        if km:
            opt.enum_keys = key_maps.get(km.group(1) or km.group(2), [])
        opt.min = _number(block, "min")
        opt.max = _number(block, "max")
        dm = re.search(r"set_default_value\((.*?)\);\s*\n", block, re.S)
        opt.default = " ".join(dm.group(1).split()) if dm else ""
        if "Nullable" in opt.default:
            opt.nullable = True
        options[key] = opt
    # Machine limits are generated per axis in a loop.
    for prefix, label in (("machine_max_speed_", "Maximum speed"), ("machine_max_acceleration_", "Maximum acceleration"),
                          ("machine_max_jerk_", "Maximum jerk")):
        for axis in "xyze":
            key = prefix + axis
            options.setdefault(key, OptionDef(key=key, type="coFloats", label=f"{label} {axis.upper()}",
                                              category="Machine limits"))
    # Filament-level overrides of extruder options are generated in a loop
    # ("filament_retraction_length" mirrors "retraction_length", ...).
    for list_name in ("filament_extruder_override_keys", "filament_overhang_override_keys"):
        for fkey in _string_list(print_config_cpp, list_name):
            raw = options.get(fkey[len("filament_"):]) if fkey.startswith("filament_") else None
            if fkey in options or raw is None:
                continue
            clone = OptionDef(**asdict(raw))
            clone.key, clone.nullable, clone.default = fkey, True, "nil"
            options[fkey] = clone
    return options


def _string_list(source: str, var_name: str) -> List[str]:
    m = re.search(re.escape(var_name) + r"\s*(?:=\s*)?\{(.*?)\};", source, re.S)
    if not m:
        return []
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return re.findall(_STR_LIT, body)


def extract_schema(print_config_cpp: str, preset_cpp: str) -> Schema:
    options = extract_options(print_config_cpp)
    print_keys = _string_list(preset_cpp, "s_Preset_print_options")
    filament_keys = _string_list(preset_cpp, "s_Preset_filament_options")
    printer_keys = (_string_list(preset_cpp, "s_Preset_printer_options")
                    + _string_list(preset_cpp, "s_Preset_machine_limits_options")
                    + _string_list(print_config_cpp, "m_extruder_option_keys"))
    return Schema(options, print_keys, filament_keys, printer_keys)
