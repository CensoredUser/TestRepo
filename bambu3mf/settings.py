"""Build Bambu Studio's ``Metadata/project_settings.config``.

This mirrors ``PresetBundle::full_fff_config(apply_extruder=false)``, the
function Bambu Studio uses when it saves a project:

* process + printer + filament presets are merged into one flat config,
* per-filament vectors are concatenated across filaments,
* ``print/printer/filament_settings_id``, ``inherits_group`` and
  ``different_settings_to_system`` record which presets were used and which
  keys were changed.

``different_settings_to_system`` is critical: when Bambu Studio opens a
project it resets every key *not* listed there back to the system preset
value (``update_non_diff_values_to_base_config``). Every override made here is
therefore tracked and written to that list.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .flush import flush_matrix, flush_volume
from .profiles import META_KEYS, ProfileError, ProfileLibrary, normalize_version
from .schema import GCODE_KEYS, PROJECT_OPTIONS, OptionDef

Value = Union[str, List[str]]

# Keys removed from the merged config before saving (full_fff_config).
_ERASE_FROM_PROJECT = {
    "compatible_prints", "compatible_prints_condition", "compatible_printers",
    "compatible_printers_condition", "inherits", "different_settings_to_system",
    "filament_map_2",
}
# Ignored when computing differences (PresetBundle.cpp ignore_settings_list).
_DIFF_IGNORE = {"inherits", "print_settings_id", "filament_settings_id", "printer_settings_id"}

DEFAULT_COLOURS = ["#F2754E", "#00AE42", "#0A2989", "#FFFFFF", "#000000", "#F7E6DE",
                   "#C12E1F", "#FEC600", "#5E43B7", "#8E9089"]

SECTIONS = ("process", "filament", "machine", "project")

# Keys of project_settings.config that are not config options.
PROJECT_META_KEYS = {
    "name", "from", "version", "inherits_group", "different_settings_to_system",
    "compatible_machine_expression_group", "compatible_process_expression_group",
    "print_compatible_printers", "filament_ids", "extruder_ams_count",
    "print_settings_id", "printer_settings_id", "filament_settings_id",
}


class SettingsError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------

def format_number(x: float) -> str:
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        return str(x)
    if math.isfinite(x) and x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return ("%.10g" % x)


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _scalar_to_str(key: str, v: Any, opt: Optional[OptionDef]) -> str:
    typ = opt.type if opt else ""
    if v is None:
        raise SettingsError(f"{key}: value must not be null")
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        s = format_number(v)
        if typ in ("coPercent", "coPercents"):
            s += "%"
        return s
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, (int, float)) for c in v):
        return f"{format_number(v[0])}x{format_number(v[1])}"   # coPoint
    s = str(v)
    if typ in ("coBool", "coBools") and s.lower() in ("true", "false", "yes", "no", "on", "off"):
        return "1" if s.lower() in ("true", "yes", "on") else "0"
    if typ in ("coPercent", "coPercents") and _is_num(s):
        s += "%"
    if opt and opt.enum_values and s not in opt.enum_values and s not in opt.enum_keys:
        # accept GUI labels / case-insensitive spellings
        for val, label in zip(opt.enum_values, opt.enum_labels or opt.enum_values):
            if s.lower() in (val.lower(), label.lower()):
                return val
    return s


def to_bs_value(key: str, value: Any, current: Optional[Value], opt: Optional[OptionDef]) -> Value:
    """Convert a Python value to Bambu Studio's JSON representation for *key*.

    Scalars given for vector options are broadcast to the current length
    (e.g. one speed for both the Standard and High Flow nozzle variants).
    """
    is_vec = isinstance(current, list) if current is not None else bool(opt and opt.is_vector)
    point = (isinstance(value, (list, tuple)) and len(value) == 2
             and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in value))
    if isinstance(value, (list, tuple)) and not (point and opt is not None and opt.type == "coPoint"):
        items = [_scalar_to_str(key, v, opt) for v in value]
        if not is_vec:
            if len(items) == 1:
                return items[0]
            raise SettingsError(f"{key} takes a single value, got a list of {len(items)}")
        return items
    s = _scalar_to_str(key, value, opt)
    if is_vec:
        n = len(current) if isinstance(current, list) and current else 1
        return [s] * n
    return s


def validate_value(key: str, value: Value, opt: Optional[OptionDef]) -> List[str]:
    """Return a list of problems (empty if the value looks valid)."""
    if opt is None:
        return []
    problems = []
    for item in (value if isinstance(value, list) else [value]):
        if item == "nil":
            if not opt.nullable:
                problems.append(f"{key}: 'nil' is only allowed for nullable options")
            continue
        accepted = opt.enum_keys or opt.enum_values
        if accepted and opt.type in ("coEnum", "coEnums"):
            if item not in accepted:
                problems.append(f"{key}: '{item}' is not one of {opt.enum_values or accepted}")
            continue
        if opt.type in ("coInt", "coInts", "coFloat", "coFloats", "coPercent", "coPercents",
                        "coFloatOrPercent", "coFloatsOrPercents"):
            num = item[:-1] if item.endswith("%") else item
            if not _is_num(num):
                problems.append(f"{key}: '{item}' is not a number")
                continue
            if opt.type in ("coInt", "coInts") and not re.fullmatch(r"-?\d+", num):
                problems.append(f"{key}: '{item}' must be an integer")
            f = float(num)
            if opt.type.startswith("coFloatOr") and item.endswith("%"):
                continue   # the limits apply to absolute values only
            if opt.min is not None and f < opt.min:
                problems.append(f"{key}: {item} is below the minimum {format_number(opt.min)}")
            if opt.max is not None and f > opt.max:
                problems.append(f"{key}: {item} is above the maximum {format_number(opt.max)}")
        if opt.type in ("coBool", "coBools") and item not in ("0", "1"):
            problems.append(f"{key}: '{item}' must be 0/1 (or true/false)")
    return problems


def values_equal(a: Optional[Value], b: Optional[Value]) -> bool:
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)) or len(a) != len(b):
            return False
        return all(values_equal(x, y) for x, y in zip(a, b))
    if a == b:
        return True
    if a is None or b is None:
        return False
    pa, pb = a.endswith("%"), b.endswith("%")
    if pa != pb:
        return False
    a2, b2 = (a[:-1], b[:-1]) if pa else (a, b)
    if _is_num(a2) and _is_num(b2):
        return float(a2) == float(b2)
    return False


def escape_strings_cstyle(items: Sequence[str]) -> str:
    """Slic3r::escape_strings_cstyle (used for different_settings_to_system)."""
    out = []
    for s in items:
        if re.search(r'[\s;"\\]', s):
            out.append('"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"')
        else:
            out.append(s)
    return ";".join(out)


def unescape_strings_cstyle(s: str) -> List[str]:
    if not s:
        return []
    out, cur, i, quoted = [], "", 0, False
    while i < len(s):
        c = s[i]
        if quoted:
            if c == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                cur += {"n": "\n", "t": "\t"}.get(nxt, nxt)
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                cur += c
        elif c == '"':
            quoted = True
        elif c == ";":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


def _strip(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in META_KEYS}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class SettingsBuilder:
    """Select presets, apply overrides and produce a project config."""

    def __init__(self, lib: ProfileLibrary, printer: str, nozzle: Union[float, str] = 0.4,
                 process: Optional[str] = None, filaments: Optional[Sequence[Optional[str]]] = None,
                 app_version: Optional[str] = None):
        self.lib = lib
        self.schema = lib.schema
        self.machine = lib.machine_preset(printer, nozzle)
        self.process = lib.process_preset(self.machine, process)
        self.filaments = [lib.filament_preset(self.machine, q) for q in (filaments or [None])]
        if not self.filaments:
            raise SettingsError("at least one filament is required")
        self.app_version = normalize_version(app_version) if app_version else self._default_app_version()

        self.cfg: Dict[str, Any] = {
            "process": _strip(lib.resolve(self.process)),
            "machine": _strip(lib.resolve(self.machine)),
            "filament": [_strip(lib.resolve(f)) for f in self.filaments],
        }
        self.base: Dict[str, Any] = {
            "process": self._base_of(self.process),
            "machine": self._base_of(self.machine),
            "filament": [self._base_of(f) for f in self.filaments],
        }
        self.project: Dict[str, Value] = {}
        self.colours: List[str] = []
        for i, f in enumerate(self.cfg["filament"]):
            dc = f.get("default_filament_colour")
            dc = dc[0] if isinstance(dc, list) and dc else dc
            self.colours.append(dc or DEFAULT_COLOURS[i % len(DEFAULT_COLOURS)])
        model = self.cfg["machine"].get("printer_model", "")
        bed = None
        if model in lib.presets:
            bed = lib.raw(model).get("default_bed_type")
        self.project["curr_bed_type"] = bed or "Textured PEI Plate"
        self.warnings: List[str] = []
        # custom names -> these presets are embedded in the project (None = use the base preset)
        self.process_name: Optional[str] = None
        self.filament_names: List[Optional[str]] = [None] * len(self.filaments)

    # -- helpers ------------------------------------------------------------
    def _default_app_version(self) -> str:
        if self.lib.app_version:
            return self.lib.app_version
        v = normalize_version(self.lib.profile_version or "2.0.0.0").split(".")
        return f"{v[0]}.{v[1]}.00.00"   # never claims to be newer than the installed minor

    def _base_of(self, name: str) -> dict:
        anc = self.lib.system_ancestor(name)
        return _strip(self.lib.resolve(anc)) if anc else {}

    @property
    def filament_count(self) -> int:
        return len(self.filaments)

    @property
    def nozzle_count(self) -> int:
        nd = self.cfg["machine"].get("nozzle_diameter", ["0.4"])
        return len(nd) if isinstance(nd, list) else 1

    def section_of(self, key: str) -> str:
        sec = self.schema.section_of(key) if self.schema else None
        if sec:
            return sec
        if key in self.cfg["process"]:
            return "process"
        if any(key in f for f in self.cfg["filament"]):
            return "filament"
        if key in self.cfg["machine"]:
            return "machine"
        if key in PROJECT_OPTIONS:
            return "project"
        pool = set(self.cfg["process"]) | set(self.cfg["machine"]) | set().union(*self.cfg["filament"])
        hint = self.schema.suggest(key, pool) if self.schema else []
        raise SettingsError(f"Unknown setting '{key}'" + (f" (did you mean: {', '.join(hint)}?)" if hint else ""))

    # -- overrides ------------------------------------------------------------
    def set(self, key: str, value: Any, filament: Optional[int] = None,
            section: Optional[str] = None) -> None:
        """Override one setting. *filament* is a 0-based index (None = all filaments)."""
        if key in _DIFF_IGNORE or key in META_KEYS:
            raise SettingsError(f"'{key}' is managed by bambu3mf and cannot be set directly")
        sec = section or self.section_of(key)
        if sec not in SECTIONS:
            raise SettingsError(f"unknown section '{sec}'")
        opt = self.schema.get(key) if self.schema else None
        if key == "filament_colour":
            cols = value if isinstance(value, list) else [value]
            if filament is not None:
                if not 0 <= filament < self.filament_count:
                    raise SettingsError(f"filament index {filament + 1} out of range (have {self.filament_count})")
                self.colours[filament] = str(cols[0])
            else:
                self.colours = [str(c) for c in cols] + self.colours[len(cols):]
            return
        if sec == "project":
            v = to_bs_value(key, value, self.project.get(key), opt)
            self._check(key, v, opt)
            self.project[key] = v
            return
        if sec == "filament":
            idxs = range(self.filament_count) if filament is None else [filament]
            for i in idxs:
                if not 0 <= i < self.filament_count:
                    raise SettingsError(f"filament index {i + 1} out of range (have {self.filament_count})")
                cur = self.cfg["filament"][i].get(key)
                v = to_bs_value(key, value, cur, opt)
                self._check(key, v, opt)
                self.cfg["filament"][i][key] = v
            return
        if filament is not None:
            raise SettingsError(f"'{key}' is a {sec} setting, not a per-filament one")
        cur = self.cfg[sec].get(key)
        v = to_bs_value(key, value, cur, opt)
        self._check(key, v, opt)
        self.cfg[sec][key] = v

    def _check(self, key: str, value: Value, opt: Optional[OptionDef]) -> None:
        problems = validate_value(key, value, opt)
        if problems:
            raise SettingsError("; ".join(problems))
        if key in GCODE_KEYS:
            self.warnings.append(f"'{key}' is G-code; Bambu Studio will ask the user to confirm "
                                 "modified G-code when the project is opened.")

    def update(self, settings: Dict[str, Any], section: Optional[str] = None,
               filament: Optional[int] = None) -> None:
        for k, v in (settings or {}).items():
            self.set(k, v, filament=filament, section=section)

    def effective(self, key: str, filament: int = 0) -> Optional[Value]:
        """Current value of a key after overrides (process > machine > filament > project)."""
        for sec in ("process", "machine"):
            if key in self.cfg[sec]:
                return self.cfg[sec][key]
        if key in self.cfg["filament"][filament]:
            return self.cfg["filament"][filament][key]
        return self.project.get(key)

    # -- embedded (project) presets --------------------------------------------
    def name_presets(self, process: Optional[str] = None,
                     filaments: Optional[Sequence[Optional[str]]] = None) -> None:
        """Give the process / filament presets their own names.

        Named presets are written into the 3MF as project-embedded presets
        (``Metadata/process_settings_N.config`` ...) that inherit from the
        system preset; Bambu Studio lists them in its preset dropdowns and
        selects them when the project is opened.
        """
        taken = set(self.lib.presets)
        if process:
            if process in taken:
                raise SettingsError(f"preset name '{process}' already exists; choose another profile name")
            self.process_name = process
        for i, name in enumerate(filaments or []):
            if name:
                if not 0 <= i < self.filament_count:
                    raise SettingsError(f"filament index {i + 1} out of range")
                if name in taken:
                    raise SettingsError(f"preset name '{name}' already exists; choose another profile name")
                self.filament_names[i] = name

    def _parent(self, name: str) -> str:
        anc = self.lib.system_ancestor(name)
        if not anc:
            raise SettingsError(f"'{name}' does not derive from a system preset and cannot be embedded")
        return anc

    def embedded_presets(self) -> List[Dict[str, Any]]:
        """Project-embedded presets as Bambu Studio saves them
        (PresetCollection::get_preset_differed_for_save): changed keys only,
        plus ``inherits``, the preset id key and the extruder variant keys."""
        out: List[Dict[str, Any]] = []
        d = self.diffs()

        def make(kind, name, parent, cfg, keys, id_key, id_val, variant_keys):
            body: Dict[str, Any] = {"from": "project", "name": name, "version": self.app_version,
                                    "inherits": parent, id_key: id_val}
            for k in list(keys) + [k for k in variant_keys if k in cfg]:
                body[k] = deepcopy(cfg[k])
            return {"type": kind, "name": name, "config": body}

        if self.process_name:
            out.append(make("process", self.process_name, self._parent(self.process), self.cfg["process"],
                            d["process"], "print_settings_id", self.process_name,
                            ("print_extruder_id", "print_extruder_variant")))
        seen = set()
        for i, name in enumerate(self.filament_names):
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(make("filament", name, self._parent(self.filaments[i]), self.cfg["filament"][i],
                            d["filament"][i], "filament_settings_id", [name], ("filament_extruder_variant",)))
        return out

    # -- diffs ----------------------------------------------------------------
    @staticmethod
    def _diff(cfg: dict, base: dict) -> List[str]:
        keys = set(cfg) | set(base)
        return sorted(k for k in keys if k not in _DIFF_IGNORE and k not in META_KEYS
                      and k in cfg and not values_equal(cfg.get(k), base.get(k)))

    def diffs(self) -> Dict[str, Any]:
        return {
            "process": self._diff(self.cfg["process"], self.base["process"]),
            "filament": [self._diff(c, b) for c, b in zip(self.cfg["filament"], self.base["filament"])],
            "machine": self._diff(self.cfg["machine"], self.base["machine"]),
        }

    def _inherits(self, name: str, embedded: bool = False) -> str:
        if embedded:
            return self._parent(name)
        info = self.lib.info(name)
        if info.source == "system":
            return ""
        return info.raw.get("inherits", "") or ""

    # -- flush ----------------------------------------------------------------
    def _min_flush_volumes(self, nozzle: int = 0) -> List[int]:
        """Port of get_min_flush_volumes() in Plater.cpp."""
        m = self.cfg["machine"]

        def at(v, i, d):
            if isinstance(v, list):
                v = v[i] if i < len(v) else (v[0] if v else d)
            return d if v in (None, "", "nil") else v

        nozzle_volume = int(float(at(m.get("nozzle_volume"), nozzle, 0)))
        level = int(float(at(m.get("enable_long_retraction_when_cut"), 0, 0)))
        machine_active = str(at(m.get("long_retractions_when_cut"), nozzle, "0")) == "1"
        printer_len = float(at(m.get("retraction_distances_when_cut"), nozzle, 18))
        out = []
        for f in self.cfg["filament"]:
            retract = printer_len if (level and machine_active) else 0
            f_active = str(at(f.get("filament_long_retractions_when_cut"), 0, "0"))
            f_len = at(f.get("filament_retraction_distances_when_cut"), 0, None)
            if f_active == "0" or f_active == "nil":
                retract = 0
            elif f_active == "1" and level == 2:
                retract = int(float(f_len)) if f_len is not None else printer_len
            diam = float(at(f.get("filament_diameter"), 0, 1.75))
            out.append(int(nozzle_volume - math.pi * diam * diam / 4 * retract))
        return out

    def flush_volumes_matrix(self) -> List[str]:
        n, mats = self.filament_count, []
        for nz in range(self.nozzle_count):
            mins = self._min_flush_volumes(nz)
            for i in range(n):
                for j in range(n):
                    mats.append("0" if i == j else str(flush_volume(self.colours[i], self.colours[j], mins[i])))
        return mats

    # -- build ----------------------------------------------------------------
    def build(self) -> Dict[str, Value]:
        n = self.filament_count
        out: Dict[str, Value] = {}
        out.update(deepcopy(self.cfg["process"]))
        out.update(deepcopy(self.cfg["machine"]))

        # project-level options
        project = {
            "filament_colour": list(self.colours[:n]),
            "flush_volumes_matrix": self.flush_volumes_matrix(),
            "flush_volumes_vector": ["140"] * (2 * n),
            "flush_multiplier": ["1"] * self.nozzle_count,
            "nozzle_volume_type": list(self.cfg["machine"].get("default_nozzle_volume_type")
                                       or ["Standard"] * self.nozzle_count),
            "filament_map_mode": "Auto For Flush",
            "filament_map": ["1"] * n,
        }
        project.update(deepcopy(self.project))
        out.update(project)

        # filaments: scalars from the first filament, vectors concatenated
        fcfgs = self.cfg["filament"]
        keys: List[str] = []
        seen = set()
        for f in fcfgs:
            for k in f:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        for k in keys:
            vals = [f.get(k) for f in fcfgs]
            first = next(v for v in vals if v is not None)
            if not isinstance(first, list):
                out[k] = first
                continue
            merged: List[str] = []
            for v in vals:
                merged += list(v if isinstance(v, list) else ([v] if v is not None else first))
            out[k] = merged

        self_index: List[str] = []
        for i, f in enumerate(fcfgs):
            variants = f.get("filament_extruder_variant")
            count = len(variants) if isinstance(variants, list) and variants else 1
            self_index += [str(i + 1)] * count
        out["filament_self_index"] = self_index

        for k in _ERASE_FROM_PROJECT:
            out.pop(k, None)
        for k in ("support_filament", "support_interface_filament"):
            if k in out and _is_num(str(out[k])):
                out[k] = str(max(0, min(int(float(out[k])), n)))

        out["print_settings_id"] = self.process_name or self.process
        out["printer_settings_id"] = self.machine
        out["filament_settings_id"] = [n or f for n, f in zip(self.filament_names, self.filaments)]
        out["filament_ids"] = [self.lib.meta_value(f, "filament_id") or "" for f in self.filaments]

        def add_if_some(key: str, values: List[str]) -> None:
            if any(values):
                out[key] = values

        p_raw, m_raw = self.cfg["process"], self.cfg["machine"]
        add_if_some("compatible_machine_expression_group",
                    [p_raw.get("compatible_printers_condition", "")]
                    + [f.get("compatible_printers_condition", "") for f in fcfgs] + [""])
        add_if_some("compatible_process_expression_group",
                    [f.get("compatible_prints_condition", "") for f in fcfgs])
        add_if_some("inherits_group", [self._inherits(self.process, bool(self.process_name))]
                    + [self._inherits(f, bool(n)) for f, n in zip(self.filaments, self.filament_names)]
                    + [self._inherits(self.machine)])
        d = self.diffs()
        add_if_some("different_settings_to_system",
                    [escape_strings_cstyle(d["process"])]
                    + [escape_strings_cstyle(x) for x in d["filament"]]
                    + [escape_strings_cstyle(d["machine"])])
        comp = p_raw.get("compatible_printers") or []
        if comp:
            out["print_compatible_printers"] = list(comp)

        if self.schema:
            # Bambu Studio's DynamicPrintConfig drops keys it does not define (obsolete or
            # newer profile keys), so they never appear in a project it saves.
            for k in [k for k in out if k not in self.schema.options and k not in PROJECT_META_KEYS]:
                del out[k]
        out["name"] = "project_settings"
        out["from"] = "project"
        out["version"] = self.app_version
        return out

    # -- reporting --------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        d = self.diffs()
        return {
            "printer": self.machine,
            "process": self.process_name or self.process,
            "filaments": [n or f for n, f in zip(self.filament_names, self.filaments)],
            "base_presets": {"process": self.process, "filaments": self.filaments},
            "colours": self.colours,
            "bed_type": self.project.get("curr_bed_type"),
            "app_version": self.app_version,
            "changed": {
                "process": {k: self.cfg["process"][k] for k in d["process"]},
                "machine": {k: self.cfg["machine"][k] for k in d["machine"]},
                "filament": [{k: c[k] for k in keys} for c, keys in zip(self.cfg["filament"], d["filament"])],
                "project": {k: v for k, v in self.project.items() if k != "curr_bed_type"},
            },
            "warnings": list(dict.fromkeys(self.warnings)),
        }
