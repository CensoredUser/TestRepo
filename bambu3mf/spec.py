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
             "metadata", "arrange", "plates", "objects", "profile", "variants", "active_variant",
             "$schema", "comment", "notes"}
_VARIANT_KEYS = {"name", "filament", "color", "colour", "slot", "process", "settings", "process_settings",
                 "filament_settings", "comment"}
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


def build_settings(spec: dict, lib: ProfileLibrary, base_dir: Path = Path("."),
                   variant: Optional[dict] = None) -> SettingsBuilder:
    """Resolve presets and apply overrides. *variant* (an entry of ``variants``)
    replaces one filament slot and adds its own process/filament overrides."""
    for pf in spec.get("preset_files", []) or []:
        p = Path(pf)
        lib.add_preset_file(p if p.is_absolute() else base_dir / p)
    fil_specs = list(spec.get("filaments") or [None])
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
    slot = None
    if variant is not None:
        slot = int(variant.get("slot", 1)) - 1
        if not 0 <= slot <= len(fil_names):
            raise SpecError(f"variant '{variant.get('name')}': slot {slot + 1} does not exist")
        if slot == len(fil_names):
            fil_names.append(None)
            fil_entries.append({})
        fil_names[slot] = variant.get("filament")
        # the variant replaces the slot's material: keep only its colour
        fil_entries[slot] = {k: v for k, v in fil_entries[slot].items() if k in ("color", "colour")}
    if "printer" not in spec:
        raise SpecError("spec needs a 'printer' (e.g. \"X1C\", \"P1S\", \"A1 mini\")")
    process = (variant or {}).get("process") or spec.get("process")
    b = SettingsBuilder(lib, spec["printer"], spec.get("nozzle", 0.4), process, fil_names, spec.get("app_version"))
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
        if variant is not None:
            col = variant.get("color") or variant.get("colour")
            if col:
                b.set("filament_colour", col, filament=slot)
            for k, v in (variant.get("settings") or {}).items():
                sec = b.section_of(k)
                b.set(k, v, filament=slot if sec == "filament" else None)
            b.update(variant.get("process_settings"), section="process")
            b.update(variant.get("filament_settings"), section="filament", filament=slot)
    except SettingsError as e:
        where = f"variant '{variant.get('name')}': " if variant else ""
        raise SpecError(where + str(e)) from None
    return b


def _base_name(preset: str) -> str:
    return preset.split(" @")[0].strip()


def _profile_name(spec: dict) -> str:
    prof = spec.get("profile")
    if isinstance(prof, str):
        return prof
    if isinstance(prof, dict) and prof.get("name"):
        return str(prof["name"])
    title = (spec.get("metadata") or {}).get("title")
    if title:
        return str(title)
    objs = spec.get("objects") or [o for pl in spec.get("plates") or [] for o in pl.get("objects", [])]
    for o in objs:
        if o.get("name"):
            return str(o["name"])
        if isinstance(o.get("mesh"), str):
            return Path(o["mesh"]).stem
    return "bambu3mf"


def _embed_enabled(spec: dict) -> bool:
    prof = spec.get("profile")
    return not (isinstance(prof, dict) and prof.get("embed") is False) and prof is not False


def build_profiles(spec: dict, lib: ProfileLibrary, base_dir: Path = Path(".")):
    """Return (active builder, embedded presets, variant summary).

    Every build embeds its own named presets ("<profile> - <material>") unless
    ``"profile": {"embed": false}``. With ``variants`` one process + filament
    preset pair is embedded per variant; the active one is selected.
    """
    if isinstance(spec.get("profile"), dict):
        _check_keys(spec["profile"], {"name", "embed", "comment"}, "profile")
    variants = spec.get("variants") or []
    embed = _embed_enabled(spec)
    if variants and not embed:
        raise SpecError("'variants' are stored as embedded presets; they need profile.embed to stay enabled")
    pname = _profile_name(spec)

    def filament_names(b: SettingsBuilder, slot: Optional[int], vname: Optional[str]) -> List[str]:
        names = []
        for i, f in enumerate(b.filaments):
            n = f"{pname} - {vname}" if i == slot else f"{pname} - {_base_name(f)}"
            if n in names:
                n = f"{n} #{i + 1}"
            names.append(n)
        return names

    if not variants:
        b = build_settings(spec, lib, base_dir)
        if embed:
            b.name_presets(process=pname, filaments=filament_names(b, None, None))
        return b, (b.embedded_presets() if embed else []), []

    builders = []
    seen_names = set()
    for vi, v in enumerate(variants):
        if not isinstance(v, dict) or "filament" not in v:
            raise SpecError(f"variants[{vi}] needs at least a 'filament' (preset name or material like \"PETG\")")
        _check_keys(v, _VARIANT_KEYS, f"variants[{vi}]")
        b = build_settings(spec, lib, base_dir, v)
        slot = int(v.get("slot", 1)) - 1
        ftype = b.cfg["filament"][slot].get("filament_type")
        ftype = ftype[0] if isinstance(ftype, list) and ftype else ftype
        vname = str(v.get("name") or ftype or _base_name(b.filaments[slot]))
        if vname in seen_names:
            raise SpecError(f"variant name '{vname}' is used twice; give each variant a unique 'name'")
        seen_names.add(vname)
        b.name_presets(process=f"{pname} - {vname}", filaments=filament_names(b, slot, vname))
        builders.append((vname, slot, b))
    active = spec.get("active_variant") or builders[0][0]
    act = [x for x in builders if x[0] == active]
    if not act:
        raise SpecError(f"active_variant '{active}' is not one of {[x[0] for x in builders]}")
    active_builder = act[0][2]
    presets: List[Dict[str, Any]] = []
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(p):
        key = (p["type"], p["name"])
        if key in index:
            if index[key]["config"] != p["config"]:
                raise SpecError(f"two variants produce different {p['type']} presets named '{p['name']}'")
            return
        index[key] = p
        presets.append(p)

    for p in active_builder.embedded_presets():
        add(p)
    summary = []
    for vname, slot, b in builders:
        for p in b.embedded_presets():
            if p["type"] == "process" or p["name"] == b.filament_names[slot]:
                add(p)
        summary.append({"variant": vname, "active": b is active_builder, "process": b.process_name,
                        "process_base": b.process, "filament": b.filament_names[slot],
                        "filament_base": b.filaments[slot], "changed": b.summary()["changed"]})
    order = {"process": 0, "filament": 1, "machine": 2}
    presets.sort(key=lambda p: order[p["type"]])
    return active_builder, presets, summary


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
    builder, embedded, variants = build_profiles(spec, lib, base_dir)
    config = builder.build()
    project = Project(config, Bed.from_config(config))
    project.app_version = builder.app_version
    project.embedded_presets = embedded
    builder.variants = variants
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
