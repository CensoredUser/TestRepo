"""JSON build spec -> Bambu Studio project.

Minimal example::

    {
      "printer": "X1C", "nozzle": 0.4,
      "process": "0.20mm Standard",
      "filaments": ["Bambu PLA Basic"],
      "settings": {"wall_loops": 3, "sparse_infill_density": "25%"},
      "objects": [{"name": "cube", "mesh": {"box": [20, 20, 20]}}]
    }

See docs/SPEC.md for every field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .mesh import Mesh
from .profiles import ProfileLibrary
from .project import (Bed, Instance, LayerRange, Metadata, ModelObject, Part, Plate, Project,
                      object_transform, part_transform)
from .settings import SettingsBuilder, SettingsError, to_bs_value, validate_value

_TOP_KEYS = {"printer", "nozzle", "process", "filaments", "bed_type", "settings", "process_settings",
             "machine_settings", "filament_settings", "project_settings", "app_version", "preset_files",
             "metadata", "arrange", "plates", "objects", "$schema", "comment", "notes"}
_OBJ_KEYS = {"name", "mesh", "rotate", "scale", "position", "copies", "instances", "filament", "settings",
             "parts", "modifiers", "height_ranges", "plate", "printable", "comment"}
_PART_KEYS = {"name", "mesh", "type", "offset", "rotate", "scale", "settings", "filament", "comment"}


class SpecError(ValueError):
    pass


def _check_keys(d: dict, allowed: set, where: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {sorted(unknown)}; allowed: {sorted(allowed)}")


def load_mesh(spec: Any, base_dir: Path) -> Mesh:
    if isinstance(spec, str):
        p = Path(spec)
        if not p.is_absolute():
            p = base_dir / p
        return Mesh.load(p)
    if isinstance(spec, dict):
        if "file" in spec:
            return load_mesh(spec["file"], base_dir)
        if "box" in spec:
            x, y, z = spec["box"]
            return Mesh.box(x, y, z)
        if "cylinder" in spec:
            c = spec["cylinder"]
            return Mesh.cylinder(c["radius"], c["height"], int(c.get("segments", 64)), c.get("top_radius"))
        if "cone" in spec:
            c = spec["cone"]
            return Mesh.cylinder(c["radius"], c["height"], int(c.get("segments", 64)), c.get("top_radius", 0.0))
        if "sphere" in spec:
            c = spec["sphere"]
            r = c["radius"] if isinstance(c, dict) else c
            return Mesh.sphere(r)
    raise SpecError(f"invalid mesh spec {spec!r}; use a file path or "
                    "{'box': [x,y,z]} / {'cylinder': {...}} / {'cone': {...}} / {'sphere': {...}}")


def _object_settings(builder: SettingsBuilder, settings: Dict[str, Any], where: str,
                     allow_extruder: bool = True) -> Dict[str, Any]:
    """Validate per-object / per-part / per-range overrides (print options only)."""
    out = {}
    for key, value in (settings or {}).items():
        if key == "extruder" and allow_extruder:
            out[key] = str(int(value))
            continue
        try:
            sec = builder.section_of(key)
        except SettingsError as e:
            raise SpecError(f"{where}: {e}") from None
        if sec != "process":
            raise SpecError(f"{where}: '{key}' is a {sec} setting; only process (print) settings can be "
                            "overridden per object, part or height range")
        opt = builder.schema.get(key) if builder.schema else None
        v = to_bs_value(key, value, builder.cfg["process"].get(key), opt)
        problems = validate_value(key, v, opt)
        if problems:
            raise SpecError(f"{where}: " + "; ".join(problems))
        out[key] = v
    return out


def _filament_index(value: Any, n: int, where: str) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise SpecError(f"{where}: filament must be a 1-based number") from None
    if not 1 <= i <= n:
        raise SpecError(f"{where}: filament {i} does not exist (project has {n})")
    return i


def build_settings(spec: dict, lib: ProfileLibrary, base_dir: Path = Path(".")) -> SettingsBuilder:
    for pf in spec.get("preset_files", []) or []:
        p = Path(pf)
        lib.add_preset_file(p if p.is_absolute() else base_dir / p)
    fil_specs = spec.get("filaments") or [None]
    fil_names, fil_entries = [], []
    for i, f in enumerate(fil_specs):
        if f is None or isinstance(f, str):
            fil_names.append(f)
            fil_entries.append({})
        elif isinstance(f, dict):
            _check_keys(f, {"preset", "color", "colour", "settings", "comment"}, f"filaments[{i}]")
            fil_names.append(f.get("preset"))
            fil_entries.append(f)
        else:
            raise SpecError(f"filaments[{i}] must be a preset name or an object")
    if "printer" not in spec:
        raise SpecError("spec needs a 'printer' (e.g. \"X1C\", \"P1S\", \"A1 mini\")")
    b = SettingsBuilder(lib, spec["printer"], spec.get("nozzle", 0.4), spec.get("process"),
                        fil_names, spec.get("app_version"))
    try:
        for i, f in enumerate(fil_entries):
            col = f.get("color") or f.get("colour")
            if col:
                b.set("filament_colour", col, filament=i)
        if spec.get("bed_type"):
            b.set("curr_bed_type", spec["bed_type"], section="project")
        b.update(spec.get("settings"))
        b.update(spec.get("process_settings"), section="process")
        b.update(spec.get("machine_settings"), section="machine")
        b.update(spec.get("filament_settings"), section="filament")
        b.update(spec.get("project_settings"), section="project")
        for i, f in enumerate(fil_entries):
            b.update(f.get("settings"), section="filament", filament=i)
    except SettingsError as e:
        raise SpecError(str(e)) from None
    return b


def _build_object(o: dict, builder: SettingsBuilder, base_dir: Path, where: str, default_plate: int) -> ModelObject:
    _check_keys(o, _OBJ_KEYS, where)
    n = builder.filament_count
    parts: List[Part] = []
    name = o.get("name")
    if "mesh" in o:
        mesh = load_mesh(o["mesh"], base_dir)
        name = name or (Path(mesh.source).stem if mesh.source else "Object")
        parts.append(Part(mesh=mesh, name=name))
    for key, default_type in (("parts", "normal_part"), ("modifiers", "modifier_part")):
        for j, ps in enumerate(o.get(key, []) or []):
            pw = f"{where}.{key}[{j}]"
            _check_keys(ps, _PART_KEYS, pw)
            if "mesh" not in ps:
                raise SpecError(f"{pw}: needs a 'mesh'")
            pm = load_mesh(ps["mesh"], base_dir)
            ptype = ps.get("type", default_type)
            ext = _filament_index(ps["filament"], n, pw) if "filament" in ps else None
            try:
                part = Part(mesh=pm, name=ps.get("name") or (Path(pm.source).stem if pm.source else f"{ptype} {j + 1}"),
                            subtype=ptype,
                            transform=part_transform(ps.get("offset", (0, 0, 0)), ps.get("rotate", (0, 0, 0)),
                                                     ps.get("scale", 1.0)),
                            settings=_object_settings(builder, ps.get("settings"), pw, allow_extruder=False),
                            extruder=ext)
            except ValueError as e:
                raise SpecError(f"{pw}: {e}") from None
            parts.append(part)
    if not parts or not any(p.subtype == "normal_part" for p in parts):
        raise SpecError(f"{where}: an object needs a 'mesh' or at least one normal part")
    name = name or parts[0].name or "Object"
    extruder = _filament_index(o.get("filament", 1), n, where)
    plate = int(o["plate"]) - 1 if "plate" in o else default_plate
    if plate < 0:
        raise SpecError(f"{where}: plate numbers start at 1")
    instances: List[Instance] = []
    printable = bool(o.get("printable", True))
    if "instances" in o:
        for pos in o["instances"]:
            instances.append(Instance(float(pos[0]), float(pos[1]), printable, True))
    else:
        copies = int(o.get("copies", 1))
        if copies < 1:
            raise SpecError(f"{where}: copies must be >= 1")
        if "position" in o:
            x, y = o["position"][:2]
            instances.append(Instance(float(x), float(y), printable, True))
            copies -= 1
        instances += [Instance(printable=printable) for _ in range(copies)]
    ranges = []
    for k, r in enumerate(o.get("height_ranges", []) or []):
        rw = f"{where}.height_ranges[{k}]"
        if float(r["max_z"]) <= float(r["min_z"]):
            raise SpecError(f"{rw}: max_z must be greater than min_z")
        settings = _object_settings(builder, r.get("settings"), rw)
        ranges.append(LayerRange(float(r["min_z"]), float(r["max_z"]), settings))
    return ModelObject(name=name, parts=parts,
                       transform=object_transform(o.get("rotate", (0, 0, 0)), o.get("scale", 1.0)),
                       settings=_object_settings(builder, o.get("settings"), where, allow_extruder=False),
                       extruder=extruder, instances=instances, layer_ranges=ranges, plate=plate)


def build_project(spec: dict, lib: ProfileLibrary, base_dir: Path = Path(".")) -> Tuple[Project, SettingsBuilder]:
    _check_keys(spec, _TOP_KEYS, "spec")
    builder = build_settings(spec, lib, base_dir)
    config = builder.build()
    project = Project(config, Bed.from_config(config))
    project.app_version = builder.app_version
    md = spec.get("metadata") or {}
    project.metadata = Metadata(**{k: str(v) for k, v in md.items() if k in Metadata.__dataclass_fields__})
    plates = spec.get("plates")
    if plates and spec.get("objects"):
        raise SpecError("use either 'objects' (single plate / auto) or 'plates', not both")
    if plates:
        project.plates = []
        for pi, pl in enumerate(plates):
            _check_keys(pl, {"name", "bed_type", "print_sequence", "spiral_mode", "locked", "objects", "comment"},
                        f"plates[{pi}]")
            project.plates.append(Plate(name=pl.get("name", ""), locked=bool(pl.get("locked", False)),
                                        bed_type=pl.get("bed_type"), print_sequence=pl.get("print_sequence"),
                                        spiral_mode=pl.get("spiral_mode")))
            for oi, o in enumerate(pl.get("objects", []) or []):
                if "plate" in o:
                    raise SpecError(f"plates[{pi}].objects[{oi}]: 'plate' is implied by the plate it is listed in")
                project.add_object(_build_object(o, builder, base_dir, f"plates[{pi}].objects[{oi}]", pi))
    else:
        for oi, o in enumerate(spec.get("objects", []) or []):
            project.add_object(_build_object(o, builder, base_dir, f"objects[{oi}]", 0))
    arr = spec.get("arrange") or {}
    project.arrange(spacing=float(arr.get("spacing", 6.0)), allow_new_plates=bool(arr.get("allow_new_plates", True)))
    _check_positions(project)
    return project, builder


def _check_positions(project: Project) -> None:
    bed = project.bed
    for obj in project.objects:
        w, d, _ = obj.footprint()
        for inst in obj.instances:
            if (inst.x - w / 2 < bed.min_x - 1e-6 or inst.x + w / 2 > bed.max_x + 1e-6
                    or inst.y - d / 2 < bed.min_y - 1e-6 or inst.y + d / 2 > bed.max_y + 1e-6):
                project.warnings.append(f"'{obj.name}' at ({inst.x:.1f}, {inst.y:.1f}) extends beyond the bed")
            for ex in bed.exclude:
                if not (inst.x + w / 2 <= ex[0] or inst.x - w / 2 >= ex[2]
                        or inst.y + d / 2 <= ex[1] or inst.y - d / 2 >= ex[3]):
                    project.warnings.append(f"'{obj.name}' overlaps the bed's excluded area {ex}")


def load_spec(path) -> Tuple[dict, Path]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")), p.resolve().parent
