"""Change settings inside an existing project config (no profiles needed).

Works directly on ``project_settings.config``: values are replaced in place and
the key is added to the right slot of ``different_settings_to_system`` so that
Bambu Studio keeps it when the project is opened.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .flush import flush_matrix
from .schema import PROJECT_OPTIONS, Schema
from .settings import (SettingsError, escape_strings_cstyle, to_bs_value, unescape_strings_cstyle,
                       validate_value)

# Used only when no schema is available.
_FILAMENT_HINTS = ("filament_", "nozzle_temperature", "hot_plate_temp", "cool_plate_temp", "eng_plate_temp",
                   "textured_plate_temp", "supertack_plate_temp", "fan_", "overhang_fan", "close_fan",
                   "slow_down", "reduce_fan", "additional_cooling", "temperature_vitrification",
                   "required_nozzle_HRC", "activate_air_filtration", "during_print_exhaust",
                   "complete_print_exhaust", "pressure_advance", "enable_pressure_advance")
_MACHINE_HINTS = ("machine_", "printable_", "nozzle_diameter", "retraction_", "retract_", "z_hop",
                  "printer_", "extruder_", "bed_exclude_area", "thumbnails", "deretraction_speed",
                  "wipe", "change_filament_gcode", "layer_change_gcode", "time_lapse_gcode")


class ProjectConfigEditor:
    def __init__(self, config: Dict[str, Any], schema: Optional[Schema] = None,
                 embedded: Optional[List[Dict[str, Any]]] = None):
        self.config = config
        self.embedded = embedded if embedded is not None else []
        self.schema = schema or Schema.empty()
        ids = config.get("filament_settings_id") or config.get("filament_colour") or [""]
        self.n = len(ids)
        si = config.get("filament_self_index") or [str(i + 1) for i in range(self.n)]
        self.self_index = [int(x) for x in si]
        raw = list(config.get("different_settings_to_system") or [])
        raw += [""] * (self.n + 2 - len(raw))
        self.diffs: List[set] = [set(unescape_strings_cstyle(s)) for s in raw[: self.n + 2]]
        self.changed: List[str] = []

    def section_of(self, key: str) -> str:
        sec = self.schema.section_of(key) if self.schema else None
        if sec:
            return sec
        if key in PROJECT_OPTIONS:
            return "project"
        if key not in self.config:
            hint = self.schema.suggest(key, self.config) if self.schema else []
            if not hint:
                from difflib import get_close_matches
                hint = get_close_matches(key, sorted(self.config), n=5, cutoff=0.6)
            raise SettingsError(f"Unknown setting '{key}' (not present in this project)"
                                + (f"; did you mean: {', '.join(hint)}?" if hint else ""))
        if key.startswith(_FILAMENT_HINTS):
            return "filament"
        if key.startswith(_MACHINE_HINTS):
            return "machine"
        return "process"

    def _filament_positions(self, value: list, filament: int) -> List[int]:
        if len(value) == len(self.self_index) and len(self.self_index) != self.n:
            return [i for i, f in enumerate(self.self_index) if f == filament + 1]
        if len(value) == self.n:
            return [filament]
        raise SettingsError(f"cannot map a per-filament value of length {len(value)} "
                            f"({self.n} filaments, {len(self.self_index)} variant slots)")

    def set(self, key: str, value: Any, filament: Optional[int] = None) -> None:
        sec = self.section_of(key)
        opt = self.schema.get(key) if self.schema else None
        cur = self.config.get(key)
        if key == "filament_colour" or (sec == "filament" and isinstance(cur, list) and filament is not None):
            if not isinstance(cur, list):
                raise SettingsError(f"'{key}' is not a per-filament list in this project")
            if filament is None:
                new = to_bs_value(key, value, cur, opt)
            else:
                if not 0 <= filament < self.n:
                    raise SettingsError(f"filament {filament + 1} does not exist (project has {self.n})")
                pos = self._filament_positions(cur, filament)
                vals = value if isinstance(value, list) else [value] * len(pos)
                if len(vals) != len(pos):
                    raise SettingsError(f"{key}: filament {filament + 1} has {len(pos)} value slot(s)")
                part = to_bs_value(key, vals, [None] * len(pos), opt)
                new = list(cur)
                for p, v in zip(pos, part):
                    new[p] = v
            self._apply(key, new, opt)
            if key != "filament_colour":
                for f in ([filament] if filament is not None else range(self.n)):
                    self.diffs[f + 1].add(key)
                self._sync_embedded(key, "filament", filament)
            else:
                self.refresh_flush_matrix()
            return
        if filament is not None and sec != "filament":
            raise SettingsError(f"'{key}' is a {sec} setting, not a per-filament one")
        new = to_bs_value(key, value, cur, opt)
        self._apply(key, new, opt)
        if sec == "process":
            self.diffs[0].add(key)
        elif sec == "machine":
            self.diffs[self.n + 1].add(key)
        elif sec == "filament":
            for f in range(self.n):
                self.diffs[f + 1].add(key)
        if sec in ("process", "filament"):
            self._sync_embedded(key, sec, None)

    def _apply(self, key: str, value, opt) -> None:
        problems = validate_value(key, value, opt)
        if problems:
            raise SettingsError("; ".join(problems))
        self.config[key] = value
        self.changed.append(key)

    # -- embedded presets -------------------------------------------------------
    def find_embedded(self, name: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for p in self.embedded:
            if p["name"] == name and (kind is None or p["type"] == kind):
                return p
        return None

    def _sync_embedded(self, key: str, sec: str, filament: Optional[int]) -> None:
        """Keep the active embedded presets equal to the project values, so Bambu
        Studio opens them unmodified."""
        if sec == "process":
            p = self.find_embedded(str(self.config.get("print_settings_id", "")), "process")
            if p:
                p["config"][key] = self.config[key]
        elif sec == "filament":
            ids = self.config.get("filament_settings_id") or []
            value = self.config[key]
            for f in ([filament] if filament is not None else range(self.n)):
                p = self.find_embedded(ids[f], "filament") if f < len(ids) else None
                if not p:
                    continue
                if isinstance(value, list):
                    p["config"][key] = [value[i] for i in self._filament_positions(value, f)]
                else:
                    p["config"][key] = value

    def set_in_preset(self, name: str, key: str, value: Any) -> str:
        """Change a key in an embedded preset (e.g. an inactive material variant).
        If the preset is currently active, the project values change too."""
        sec = self.section_of(key)
        if sec not in ("process", "filament"):
            raise SettingsError(f"'{key}' is a {sec} setting; embedded variants hold process and filament settings")
        p = self.find_embedded(name, sec)
        if p is None:
            names = [f"{e['name']} ({e['type']})" for e in self.embedded]
            raise SettingsError(f"no embedded {sec} preset named '{name}'; embedded presets: {names}")
        opt = self.schema.get(key) if self.schema else None
        cur = p["config"].get(key)
        if cur is None:
            proj = self.config.get(key)
            if sec == "filament" and isinstance(proj, list):
                ids = self.config.get("filament_settings_id") or []
                f = ids.index(name) if name in ids else 0
                cur = [proj[i] for i in self._filament_positions(proj, f)]
            else:
                cur = proj
        new = to_bs_value(key, value, cur, opt)
        problems = validate_value(key, new, opt)
        if problems:
            raise SettingsError("; ".join(problems))
        p["config"][key] = new
        active = (sec == "process" and self.config.get("print_settings_id") == name) or \
                 (sec == "filament" and name in (self.config.get("filament_settings_id") or []))
        if active:
            if sec == "process":
                self.set(key, value)
            else:
                for f, fid in enumerate(self.config.get("filament_settings_id") or []):
                    if fid == name:
                        self.set(key, value, filament=f)
        else:
            self.changed.append(f"{name}:{key}")
        return "active" if active else "inactive"

    def refresh_flush_matrix(self) -> None:
        cols = self.config.get("filament_colour") or []
        nozzles = len(self.config.get("nozzle_diameter") or ["0.4"])
        if cols:
            self.config["flush_volumes_matrix"] = flush_matrix(cols, nozzles)

    def finish(self) -> Dict[str, Any]:
        vals = [escape_strings_cstyle(sorted(d)) for d in self.diffs]
        if any(vals):
            self.config["different_settings_to_system"] = vals
        return self.config
