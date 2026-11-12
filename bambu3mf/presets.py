"""Export user presets (process / filament / printer) for Bambu Studio.

The JSON files can be imported with *File > Import > Import Configs...*.
They contain only the changed keys plus ``inherits`` (the system preset they
derive from), exactly like the user presets Bambu Studio saves itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .profiles import META_KEYS, ProfileLibrary, normalize_version
from .settings import SettingsError, to_bs_value, validate_value, values_equal

_ID_KEY = {"process": "print_settings_id", "filament": "filament_settings_id", "machine": "printer_settings_id"}


def make_user_preset(lib: ProfileLibrary, preset_type: str, base: str, name: str,
                     overrides: Dict[str, Any], app_version: Optional[str] = None) -> Dict[str, Any]:
    if preset_type not in _ID_KEY:
        raise SettingsError(f"preset type must be one of {list(_ID_KEY)}")
    info = lib.info(base)
    if info.type != preset_type:
        raise SettingsError(f"'{base}' is a {info.type} preset, not {preset_type}")
    parent = lib.system_ancestor(base)
    if not parent:
        raise SettingsError(f"'{base}' does not derive from a system preset")
    flat = lib.resolve(base)
    parent_flat = lib.resolve(parent)
    schema = lib.schema
    changed: Dict[str, Any] = {}
    # keep the differences a user base preset already had
    for k, v in flat.items():
        if k not in META_KEYS and not values_equal(v, parent_flat.get(k)):
            changed[k] = v
    for key, value in overrides.items():
        if key in META_KEYS or key in _ID_KEY.values():
            raise SettingsError(f"'{key}' cannot be set in a preset")
        if schema:
            sec = schema.section_of(key)
            if sec and sec != preset_type:
                raise SettingsError(f"'{key}' is a {sec} setting and cannot go into a {preset_type} preset")
        elif key not in flat:
            raise SettingsError(f"Unknown {preset_type} setting '{key}'")
        opt = schema.get(key) if schema else None
        v = to_bs_value(key, value, flat.get(key), opt)
        problems = validate_value(key, v, opt)
        if problems:
            raise SettingsError("; ".join(problems))
        changed[key] = v
    if name == parent or name in lib.presets and lib.info(name).source == "system":
        raise SettingsError(f"'{name}' is a system preset name; choose a different name")
    version = normalize_version(app_version) if app_version else (lib.app_version or "02.00.00.00")
    out: Dict[str, Any] = {
        "from": "User",
        "inherits": parent,
        "is_custom_defined": "0",
        "name": name,
        "version": version,
        _ID_KEY[preset_type]: [name] if preset_type == "filament" else name,
    }
    if preset_type == "filament":
        fid = lib.meta_value(parent, "filament_id")
        if fid:
            out["filament_id"] = fid
    for k in sorted(changed):
        out[k] = changed[k]
    return out


def write_user_preset(preset: Dict[str, Any], path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(preset, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return p
