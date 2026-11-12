"""Command line interface: ``python -m bambu3mf <command>`` (or ``bambu3mf``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__
from .profiles import ProfileError, ProfileLibrary, cache_dir, fetch_profiles, find_profile_root
from .settings import SettingsError


def _lib(args, required: bool = True) -> Optional[ProfileLibrary]:
    try:
        lib = ProfileLibrary.open(args.profiles, with_user_presets=not args.no_user_presets)
    except ProfileError:
        if required:
            raise
        return None
    return lib


def _parse_value(s: str) -> Any:
    """CLI values: JSON if it parses (numbers, lists, true/false), else the raw string."""
    try:
        return json.loads(s)
    except ValueError:
        return s


def _parse_sets(items: List[str]) -> List[tuple]:
    out = []
    for it in items or []:
        if "=" not in it:
            raise SettingsError(f"--set expects key=value, got '{it}'")
        k, v = it.split("=", 1)
        out.append((k.strip(), _parse_value(v)))
    return out


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            print(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}")
    else:
        print(obj)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_profiles(args) -> int:
    if args.action == "fetch":
        fetch_profiles(args.ref, Path(args.dest) if args.dest else None, log=lambda m: print(m, file=sys.stderr))
        return 0
    lib = _lib(args)
    info = {"root": str(lib.root), "profile_version": lib.profile_version, "app_version": lib.app_version,
            "schema": bool(lib.schema), "presets": len(lib.presets),
            "user_presets": sum(1 for p in lib.presets.values() if p.source == "user"),
            "cache": str(cache_dir())}
    _print(info, args.json)
    return 0


def cmd_printers(args) -> int:
    lib = _lib(args)
    rows = []
    for m in lib.printer_models():
        machines = {}
        for n in m["nozzles"]:
            try:
                machines[n] = lib.machine_preset(m["name"], n)
            except ProfileError:
                pass
        rows.append({"model": m["name"], "nozzles": sorted(machines), "machine_presets": machines})
    if args.json:
        _print(rows, True)
    else:
        for r in rows:
            print(f"{r['model']:<24} nozzles: {', '.join(r['nozzles'])}")
    return 0


def cmd_presets(args) -> int:
    lib = _lib(args)
    machine = lib.machine_preset(args.printer, args.nozzle) if args.printer else None
    out = {}
    for typ in ([args.type] if args.type else ["process", "filament"]):
        names = lib.names(typ)
        if machine:
            names = [n for n in names if lib.compatible(n, machine)]
        if args.filter:
            f = args.filter.lower()
            names = [n for n in names if f in n.lower()]
        out[typ] = names
    if machine and not args.type:
        out["machine"] = machine
    if args.json:
        _print(out, True)
    else:
        if machine:
            print(f"# compatible with {machine}")
        for typ, names in out.items():
            if isinstance(names, str):
                continue
            print(f"[{typ}] ({len(names)})")
            for n in names:
                print(f"  {n}")
    return 0


def cmd_show(args) -> int:
    lib = _lib(args)
    flat = lib.resolve(args.preset)
    if args.keys:
        keys = [k.strip() for k in args.keys.split(",")]
        flat = {k: flat.get(k) for k in keys}
    info = lib.info(args.preset)
    meta = {"name": args.preset, "type": info.type, "source": info.source,
            "inherits": info.raw.get("inherits"), "system_parent": lib.system_ancestor(args.preset)}
    if args.json:
        _print({"meta": meta, "config": flat}, True)
    else:
        for k, v in meta.items():
            print(f"# {k}: {v}")
        for k in sorted(flat):
            v = flat[k]
            s = json.dumps(v, ensure_ascii=False)
            if len(s) > 160 and not args.full:
                s = s[:157] + "..."
            print(f"{k} = {s}")
    return 0


def cmd_search(args) -> int:
    lib = _lib(args)
    if not lib.schema:
        print("No option schema available (fetch profiles with `bambu3mf profiles fetch`).", file=sys.stderr)
        return 1
    hits = lib.schema.search(args.query)[: args.limit]
    builder = None
    if args.printer:
        from .settings import SettingsBuilder
        builder = SettingsBuilder(lib, args.printer, args.nozzle, args.process, args.filament or None)
    rows = []
    for o in hits:
        row = {"key": o.key, "section": lib.schema.section_of(o.key), "type": o.type, "label": o.label,
               "category": o.category, "unit": o.sidetext, "min": o.min, "max": o.max,
               "enum": o.enum_values or None, "tooltip": o.tooltip}
        if builder:
            row["current"] = builder.effective(o.key)
        rows.append(row)
    if args.json:
        _print(rows, True)
    else:
        for r in rows:
            rng = ""
            if r["min"] is not None or r["max"] is not None:
                rng = f" [{'' if r['min'] is None else r['min']:g}..{'' if r['max'] is None else r['max']:g}]" \
                    if r["min"] is not None and r["max"] is not None else \
                    f" [min {r['min']:g}]" if r["min"] is not None else f" [max {r['max']:g}]"
            print(f"{r['key']}  ({r['section']}, {r['type']}{', ' + r['unit'] if r['unit'] else ''}{rng})")
            print(f"    {r['label']}" + (f" — {r['category']}" if r['category'] else ""))
            if r["enum"]:
                print(f"    values: {', '.join(r['enum'])}")
            if "current" in r:
                print(f"    current: {json.dumps(r['current'], ensure_ascii=False)}")
            if args.verbose and r["tooltip"]:
                print(f"    {r['tooltip']}")
    return 0


def cmd_build(args) -> int:
    from .spec import load_spec, build_project, SpecError
    from .writer import write_3mf
    from .validate import validate
    lib = _lib(args)
    spec, base = load_spec(args.spec)
    for k, v in _parse_sets(args.set):
        spec.setdefault("settings", {})[k] = v
    project, builder = build_project(spec, lib, base)
    summary = builder.summary()
    summary["objects"] = [{"name": o.name, "plate": sorted({(i.plate if i.plate is not None else o.plate) + 1
                                                             for i in o.instances}),
                           "copies": len(o.instances), "size_mm": [round(v, 2) for v in o.footprint()]}
                          for o in project.objects]
    summary["plates"] = len(project.plates)
    summary["warnings"] = summary["warnings"] + project.warnings
    if args.dry_run:
        _print(summary, True)
        return 0
    out = Path(args.output or Path(args.spec).with_suffix(".3mf"))
    write_3mf(project, out, thumbnails=not args.no_thumbnails)
    rep = validate(out, lib)
    summary["output"] = str(out)
    summary["validation"] = {"ok": rep.ok, "errors": rep.errors, "warnings": rep.warnings}
    _print(summary, True)
    return 0 if rep.ok else 2


def _project_overview(proj, cfg: dict, settings: bool) -> dict:
    from .settings import unescape_strings_cstyle
    n = len(cfg.get("filament_settings_id") or [])
    dst = cfg.get("different_settings_to_system") or [""] * (n + 2)
    names = ["process"] + [f"filament {i + 1}" for i in range(n)] + ["printer"]
    changed = {}
    for slot, label in zip(dst, names):
        keys = [k for k in unescape_strings_cstyle(slot) if k]
        if keys:
            changed[label] = {k: cfg.get(k) for k in keys} if settings else keys
    return {
        "app_version": proj.app_version,
        "printer": cfg.get("printer_settings_id"),
        "process": cfg.get("print_settings_id"),
        "filaments": [{"preset": f, "colour": c} for f, c in
                      zip(cfg.get("filament_settings_id") or [], cfg.get("filament_colour") or [])],
        "bed_type": cfg.get("curr_bed_type"),
        "changed_from_system": changed,
        "plates": [{"index": i + 1, "name": p.name, "bed_type": p.bed_type, "print_sequence": p.print_sequence}
                   for i, p in enumerate(proj.plates)],
        "objects": [{"name": o.name, "filament": o.extruder,
                     "size_mm": [round(v, 2) for v in o.footprint()],
                     "instances": [{"plate": (i.plate or 0) + 1, "x": round(i.x, 2), "y": round(i.y, 2)}
                                   for i in o.instances],
                     "settings": o.settings,
                     "parts": [{"name": p.name, "type": p.subtype, "filament": p.extruder, "settings": p.settings,
                                "triangles": len(p.mesh.triangles)} for p in o.parts],
                     "height_ranges": [{"min_z": r.min_z, "max_z": r.max_z, "settings": r.settings}
                                       for r in o.layer_ranges]}
                    for o in proj.objects],
        "extra_files": sorted(proj.extra_files),
        "warnings": proj.warnings,
    }


def cmd_inspect(args) -> int:
    from .reader import read_project
    proj = read_project(args.file)
    cfg = proj.config
    if args.keys:
        _print({k.strip(): cfg.get(k.strip()) for k in args.keys.split(",")}, True)
        return 0
    _print(_project_overview(proj, cfg, args.values), True)
    return 0


def cmd_edit(args) -> int:
    from .reader import read_project
    from .writer import write_3mf
    from .edit import ProjectConfigEditor
    from .validate import validate
    from .settings import to_bs_value, validate_value
    lib = _lib(args, required=False)
    proj = read_project(args.file)
    schema = lib.schema if lib else None
    ed = ProjectConfigEditor(proj.config, schema)
    sets = _parse_sets(args.set)
    if args.object:
        targets = [o for o in proj.objects if o.name == args.object]
        if not targets:
            raise SettingsError(f"no object named '{args.object}'; objects: {[o.name for o in proj.objects]}")
        for o in targets:
            for k, v in sets:
                if k == "extruder" or k == "filament":
                    o.extruder = int(v)
                    continue
                if schema and schema.section_of(k) not in ("process", None):
                    raise SettingsError(f"'{k}' is a {schema.section_of(k)} setting; only print settings can "
                                        "be set per object")
                opt = schema.get(k) if schema else None
                val = to_bs_value(k, v, proj.config.get(k), opt)
                problems = validate_value(k, val, opt)
                if problems:
                    raise SettingsError("; ".join(problems))
                o.settings[k] = val
    else:
        fil = args.filament - 1 if args.filament else None
        for k, v in sets:
            ed.set(k, v, filament=fil)
    for k in args.unset_object or []:
        for o in proj.objects:
            if args.object in (None, o.name):
                o.settings.pop(k, None)
    proj.config = ed.finish()
    out = Path(args.output or args.file)
    write_3mf(proj, out, thumbnails=not args.no_thumbnails)
    rep = validate(out, lib)
    _print({"output": str(out), "changed": ed.changed if not args.object else [k for k, _ in sets],
            "different_settings_to_system": proj.config.get("different_settings_to_system"),
            "validation": rep.as_dict()}, True)
    return 0 if rep.ok else 2


def cmd_validate(args) -> int:
    from .validate import validate
    lib = _lib(args, required=False)
    rep = validate(args.file, lib)
    _print(rep.as_dict(), True)
    return 0 if rep.ok else 2


def cmd_export_preset(args) -> int:
    from .presets import make_user_preset, write_user_preset
    lib = _lib(args)
    base = args.base
    if args.printer:
        machine = lib.machine_preset(args.printer, args.nozzle)
        if args.type == "process":
            base = lib.process_preset(machine, args.base)
        elif args.type == "filament":
            base = lib.filament_preset(machine, args.base)
        else:
            base = machine
    if not base:
        raise SettingsError("give --base PRESET (or --printer to pick the base by name)")
    overrides = dict(_parse_sets(args.set))
    if args.settings_json:
        overrides.update(json.loads(Path(args.settings_json).read_text(encoding="utf-8")))
    preset = make_user_preset(lib, args.type, base, args.name, overrides, args.app_version)
    out = Path(args.output or f"{args.name}.json")
    write_user_preset(preset, out)
    _print({"output": str(out), "inherits": preset["inherits"],
            "changed": [k for k in preset if k not in ("from", "inherits", "is_custom_defined", "name", "version",
                                                        "print_settings_id", "filament_settings_id",
                                                        "printer_settings_id", "filament_id")]}, True)
    return 0


def cmd_mesh(args) -> int:
    from .mesh import Mesh
    m = Mesh.load(args.file)
    lo, hi = m.bbox()
    _print({"file": args.file, "vertices": len(m.vertices), "triangles": len(m.triangles),
            "size_mm": [round(hi[i] - lo[i], 3) for i in range(3)], "min": lo, "max": hi,
            "volume_mm3": round(abs(m.volume()), 2), "inside_out": m.volume() < 0}, True)
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bambu3mf", description="Create Bambu Studio compatible 3MF projects "
                                "and presets from the official Bambu Lab system profiles.")
    p.add_argument("--version", action="version", version=f"bambu3mf {__version__}")
    p.add_argument("--profiles", help="profile directory (containing BBL.json); default: installed Bambu "
                   "Studio, then the fetch cache")
    p.add_argument("--no-user-presets", action="store_true", help="ignore the user's own Bambu Studio presets")
    p.add_argument("--json", action="store_true", help="JSON output where supported")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("profiles", help="fetch or show the Bambu Studio profile set in use")
    s.add_argument("action", choices=["info", "fetch"], nargs="?", default="info")
    s.add_argument("--ref", default="master", help="Bambu Studio git tag/branch, e.g. v02.04.00.70 (match your install)")
    s.add_argument("--dest", help="destination directory (default: cache)")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("printers", help="list printer models and nozzle sizes")
    s.set_defaults(func=cmd_printers)

    s = sub.add_parser("presets", help="list process/filament presets (optionally compatible with a printer)")
    s.add_argument("--printer")
    s.add_argument("--nozzle", default=0.4)
    s.add_argument("--type", choices=["process", "filament", "machine"])
    s.add_argument("--filter", help="substring filter")
    s.set_defaults(func=cmd_presets)

    s = sub.add_parser("show", help="show a fully resolved preset")
    s.add_argument("preset")
    s.add_argument("--keys", help="comma separated keys to show")
    s.add_argument("--full", action="store_true", help="do not truncate long values")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("search", help="search setting names/labels/tooltips")
    s.add_argument("query")
    s.add_argument("--printer", help="also show current values for this printer")
    s.add_argument("--nozzle", default=0.4)
    s.add_argument("--process")
    s.add_argument("--filament", action="append")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("-v", "--verbose", action="store_true", help="include tooltips")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("build", help="build a 3MF project from a JSON spec")
    s.add_argument("spec")
    s.add_argument("-o", "--output")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help="extra setting override (repeatable)")
    s.add_argument("--dry-run", action="store_true", help="resolve presets/settings and print, do not write")
    s.add_argument("--no-thumbnails", action="store_true")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("inspect", help="summarise a 3MF project (presets, changed settings, objects, plates)")
    s.add_argument("file")
    s.add_argument("--values", action="store_true", help="show values of changed settings")
    s.add_argument("--keys", help="print these config keys")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("edit", help="change settings in an existing 3MF project")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="output file (default: overwrite)")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", required=False)
    s.add_argument("--filament", type=int, help="1-based filament for per-filament settings")
    s.add_argument("--object", help="apply --set as per-object overrides to the object with this name")
    s.add_argument("--unset-object", action="append", metavar="KEY", help="remove a per-object override")
    s.add_argument("--no-thumbnails", action="store_true")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("validate", help="check a 3MF for problems Bambu Studio would hit")
    s.add_argument("file")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("export-preset", help="write a user preset JSON (File > Import > Import Configs)")
    s.add_argument("--type", required=True, choices=["process", "filament", "machine"])
    s.add_argument("--name", required=True, help="name of the new preset")
    s.add_argument("--base", help="preset to derive from (system or user); with --printer a short name works")
    s.add_argument("--printer")
    s.add_argument("--nozzle", default=0.4)
    s.add_argument("--set", action="append", metavar="KEY=VALUE")
    s.add_argument("--settings-json", help="JSON file with overrides")
    s.add_argument("--app-version")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_export_preset)

    s = sub.add_parser("mesh", help="show size/triangle count of a mesh file")
    s.add_argument("file")
    s.set_defaults(func=cmd_mesh)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProfileError, SettingsError, ValueError, FileNotFoundError, KeyError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        print(f"error: {msg}", file=sys.stderr)
        return 1
