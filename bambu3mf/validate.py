"""Structural and semantic checks for Bambu Studio project 3MFs.

Checks the rules Bambu Studio's loader relies on (bbs_3mf.cpp / Plater.cpp /
PresetBundle.cpp), so problems surface before the file reaches the slicer.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .reader import CORE, PROD, _Archive, read_project
from .settings import PROJECT_META_KEYS, unescape_strings_cstyle, validate_value

_Q = lambda t: f"{{{CORE}}}{t}"


@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: Dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "info": self.info}


def validate(path, lib=None) -> Report:
    rep = Report()
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        rep.errors.append(f"not a readable zip/3MF: {e}")
        return rep
    names = set(z.namelist())
    for req in ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model",
                "Metadata/project_settings.config", "Metadata/model_settings.config"):
        if req not in names:
            (rep.errors if req.startswith(("3D", "_rels", "[")) else rep.warnings).append(f"missing {req}")
    if rep.errors:
        return rep

    ar = _Archive(path)
    try:
        objects, items, metadata = ar.model(None)
    except (ET.ParseError, ValueError) as e:
        rep.errors.append(f"3D/3dmodel.model: {e}")
        return rep
    app = metadata.get("Application", "")
    rep.info["application"] = app
    if not app.startswith("BambuStudio-"):
        rep.errors.append("Application metadata must start with 'BambuStudio-' or Bambu Studio ignores the "
                          "embedded settings and plates")
    if metadata.get("BambuStudio:3mfVersion") != "1":
        rep.warnings.append("BambuStudio:3mfVersion should be 1")

    # geometry / references
    vol_ids: Dict[int, set] = {}
    for oid, obj in objects.items():
        if not obj.components and not obj.vertices:
            rep.errors.append(f"object {oid} has neither mesh nor components")
        for cpath, cid, _ in obj.components:
            try:
                vols = ar.volumes(cpath, cid, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
            except ValueError as e:
                rep.errors.append(f"object {oid}: {e}")
                continue
            for mo, _, vid in vols:
                vol_ids.setdefault(oid, set()).add(vid)
                nv = len(mo.vertices)
                if not mo.triangles:
                    rep.errors.append(f"object {oid} volume {vid}: no triangles")
                bad = [t for t in mo.triangles if max(t) >= nv or min(t) < 0]
                if bad:
                    rep.errors.append(f"object {oid} volume {vid}: {len(bad)} triangles index missing vertices")
    for oid, _, _, _ in items:
        if oid not in objects:
            rep.errors.append(f"build item references missing object {oid}")
    if not items:
        rep.warnings.append("build section is empty (no objects on any plate)")

    # project settings
    cfg = {}
    try:
        cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    except (KeyError, ValueError) as e:
        rep.errors.append(f"project_settings.config is not valid JSON: {e}")
    if cfg:
        _check_config(cfg, rep, lib)

    # model settings
    if "Metadata/model_settings.config" in names:
        try:
            ms = ET.fromstring(z.read("Metadata/model_settings.config"))
        except ET.ParseError as e:
            rep.errors.append(f"model_settings.config: {e}")
            ms = None
        if ms is not None:
            build_counts: Dict[int, int] = {}
            for oid, *_ in items:
                build_counts[oid] = build_counts.get(oid, 0) + 1
            for o in ms.findall("object"):
                oid = int(o.get("id"))
                if oid not in objects:
                    rep.errors.append(f"model_settings object {oid} does not exist in 3dmodel.model")
                    continue
                for p in o.findall("part"):
                    pid = int(p.get("id"))
                    if pid not in vol_ids.get(oid, set()):
                        rep.errors.append(f"model_settings object {oid} part {pid} is not a component of it")
                    if p.get("subtype") not in ("normal_part", "modifier_part", "negative_part",
                                                "support_enforcer", "support_blocker"):
                        rep.errors.append(f"object {oid} part {pid}: unknown subtype {p.get('subtype')}")
                    _check_object_keys(p, f"object {oid} part {pid}", rep, lib)
                _check_object_keys(o, f"object {oid}", rep, lib)
            for pl in ms.findall("plate"):
                for mi in pl.findall("model_instance"):
                    vals = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
                    oid, iid = int(vals.get("object_id", -1)), int(vals.get("instance_id", -1))
                    if oid not in build_counts or not 0 <= iid < build_counts[oid]:
                        rep.errors.append(f"plate instance object {oid}/instance {iid} does not exist")

    # project-embedded presets
    _check_embedded(z, cfg, rep, lib)

    # higher-level checks through the reader
    try:
        proj = read_project(path)
        rep.info["objects"] = [o.name for o in proj.objects]
        rep.info["plates"] = len(proj.plates)
        rep.warnings += proj.warnings
        bed = proj.bed
        for o in proj.objects:
            w, d, h = o.footprint()
            if h > bed.height + 1e-6:
                rep.errors.append(f"'{o.name}' is {h:.1f} mm tall, printable height is {bed.height:.0f} mm")
            for inst in o.instances:
                if (inst.x - w / 2 < bed.min_x - 0.5 or inst.x + w / 2 > bed.max_x + 0.5
                        or inst.y - d / 2 < bed.min_y - 0.5 or inst.y + d / 2 > bed.max_y + 0.5):
                    rep.warnings.append(f"'{o.name}' instance at ({inst.x:.1f}, {inst.y:.1f}) on plate "
                                        f"{(inst.plate or 0) + 1} extends past the printable area")
            for i, inst in enumerate(o.instances):
                ext = [p.extruder for p in o.parts if p.extruder] + [o.extruder]
                for e in ext:
                    if e > proj.filament_count:
                        rep.errors.append(f"'{o.name}' uses filament {e} but the project has {proj.filament_count}")
                break
    except Exception as e:  # noqa: BLE001 - report anything the reader trips on
        rep.errors.append(f"reader failed: {e}")
    return rep


def _check_config(cfg: dict, rep: Report, lib) -> None:
    n = len(cfg.get("filament_settings_id") or [])
    rep.info["printer"] = cfg.get("printer_settings_id")
    rep.info["process"] = cfg.get("print_settings_id")
    rep.info["filaments"] = cfg.get("filament_settings_id")
    rep.info["version"] = cfg.get("version")
    for k in ("print_settings_id", "printer_settings_id", "filament_settings_id", "filament_colour"):
        if k not in cfg:
            rep.errors.append(f"project_settings: missing '{k}'")
    if cfg.get("name") != "project_settings" or cfg.get("from") != "project":
        rep.warnings.append("project_settings should have name=project_settings and from=project")
    cols = cfg.get("filament_colour") or []
    if n and len(cols) != n:
        rep.errors.append(f"filament_colour has {len(cols)} entries for {n} filaments")
    dst = cfg.get("different_settings_to_system")
    if dst is not None and len(dst) != n + 2:
        rep.errors.append(f"different_settings_to_system needs {n + 2} entries (process, {n} filaments, printer), "
                          f"has {len(dst)}")
    ig = cfg.get("inherits_group")
    if ig is not None and len(ig) != n + 2:
        rep.errors.append(f"inherits_group needs {n + 2} entries, has {len(ig)}")
    fm = cfg.get("flush_volumes_matrix")
    nozzles = len(cfg.get("nozzle_diameter") or ["0.4"])
    if fm is not None and len(fm) not in (n * n, n * n * nozzles):
        rep.errors.append(f"flush_volumes_matrix has {len(fm)} entries, expected {n * n * nozzles}")
    si = cfg.get("filament_self_index")
    fev = cfg.get("filament_extruder_variant")
    if si is not None and fev is not None and len(si) != len(fev):
        rep.errors.append("filament_self_index and filament_extruder_variant lengths differ")
    for k, v in cfg.items():
        if isinstance(v, list) and any(not isinstance(x, str) for x in v) or not isinstance(v, (str, list)):
            rep.errors.append(f"project_settings '{k}': values must be strings or lists of strings")
    if lib is None:
        return
    schema = lib.schema
    for slot, name, typ in ([(0, cfg.get("print_settings_id"), "process")]
                            + [(i + 1, f, "filament") for i, f in enumerate(cfg.get("filament_settings_id") or [])]
                            + [(n + 1, cfg.get("printer_settings_id"), "machine")]):
        inh = (ig or [""] * (n + 2))[slot] if ig else ""
        if name in lib.presets:
            if lib.info(name).type != typ:
                rep.errors.append(f"'{name}' is not a {typ} preset")
        elif inh and inh in lib.presets:
            pass
        else:
            rep.warnings.append(f"{typ} preset '{name}' is not a known system preset (Bambu Studio will show it "
                                "as a custom/project preset and may warn about G-code)")
    if schema:
        unknown = [k for k in cfg if k not in schema.options and k not in PROJECT_META_KEYS]
        if unknown:
            rep.warnings.append(f"{len(unknown)} keys unknown to this Bambu Studio version: {unknown[:15]}")
        changed = {key for slot in (dst or []) for key in unescape_strings_cstyle(slot)}
        for k, v in cfg.items():
            for p in validate_value(k, v, schema.get(k)):
                # values inherited from system presets are Bambu's own; only changed keys are errors
                (rep.errors if k in changed else rep.warnings).append("project_settings " + p)
        if dst:
            for slot in dst:
                for key in unescape_strings_cstyle(slot):
                    if key and key not in schema.options:
                        rep.warnings.append(f"different_settings_to_system lists unknown key '{key}'")


def _check_object_keys(el, where: str, rep: Report, lib) -> None:
    schema = lib.schema if lib is not None else None
    for md in el.findall("metadata"):
        k = md.get("key")
        if not k or k in ("name", "matrix", "module", "source_file", "source_object_id", "source_volume_id",
                          "source_offset_x", "source_offset_y", "source_offset_z", "source_in_inches",
                          "source_in_meters", "extruder", "mesh_shared"):
            continue
        if schema:
            if k not in schema.options:
                rep.errors.append(f"{where}: unknown setting '{k}'")
            elif schema.section_of(k) not in ("process", None):
                rep.warnings.append(f"{where}: '{k}' is a {schema.section_of(k)} setting; Bambu Studio only "
                                    "applies print settings per object")


_EMBED_ID = {"process": "print_settings_id", "filament": "filament_settings_id", "machine": "printer_settings_id"}


def _check_embedded(z, cfg: dict, rep: Report, lib) -> None:
    import re
    embedded = {}
    for name in z.namelist():
        m = re.match(r"^Metadata/(process|filament|machine)_settings_(\d+)\.config$", name)
        if not m:
            continue
        kind = m.group(1)
        try:
            pc = json.loads(z.read(name).decode("utf-8"))
        except ValueError as e:
            rep.errors.append(f"{name}: invalid JSON ({e})")
            continue
        pid = pc.get(_EMBED_ID[kind])
        pname = pid[0] if isinstance(pid, list) and pid else pid
        if not pname:
            rep.errors.append(f"{name}: missing {_EMBED_ID[kind]} (Bambu Studio skips the preset)")
            continue
        if (kind, pname) in embedded:
            rep.errors.append(f"{name}: duplicate embedded {kind} preset '{pname}'")
        embedded[(kind, pname)] = pc
        inh = pc.get("inherits")
        if not inh:
            rep.errors.append(f"{name}: '{pname}' has no 'inherits'; Bambu Studio only loads embedded presets "
                              "that inherit an installed preset")
        elif lib is not None:
            if inh not in lib.presets:
                rep.warnings.append(f"{name}: parent '{inh}' is not in this profile set")
            elif lib.info(inh).type != kind:
                rep.errors.append(f"{name}: parent '{inh}' is a {lib.info(inh).type} preset, not {kind}")
            if pname in lib.presets:
                rep.errors.append(f"{name}: '{pname}' clashes with an existing preset name; Bambu Studio "
                                  "would not load the embedded one")
    rep.info["embedded_presets"] = [f"{n} ({k})" for k, n in embedded]
    if not cfg:
        return
    n = len(cfg.get("filament_settings_id") or [])
    ig = cfg.get("inherits_group") or [""] * (n + 2)
    active = [("process", 0, cfg.get("print_settings_id"))] + \
             [("filament", i + 1, f) for i, f in enumerate(cfg.get("filament_settings_id") or [])]
    self_index = [int(x) for x in cfg.get("filament_self_index") or range(1, n + 1)]
    for kind, slot, pname in active:
        pc = embedded.get((kind, pname))
        if pc is None:
            continue
        if slot < len(ig) and ig[slot] != pc.get("inherits"):
            rep.errors.append(f"inherits_group[{slot}] is '{ig[slot]}' but embedded preset '{pname}' inherits "
                              f"'{pc.get('inherits')}'")
        # the project's values must equal the preset's, or it opens as "modified"
        for key, val in pc.items():
            if key in ("from", "name", "version", "inherits") or key in _EMBED_ID.values() or key not in cfg:
                continue
            cur = cfg[key]
            if kind == "filament" and isinstance(cur, list):
                if len(cur) == len(self_index) and len(self_index) != n:
                    cur = [v for v, f in zip(cur, self_index) if f == slot]
                elif len(cur) == n:
                    cur = [cur[slot - 1]]
            if cur != val and not (isinstance(val, list) and len(val) == 1 and cur == val * len(cur)):
                rep.warnings.append(f"active preset '{pname}': {key} is {cur!r} in the project but {val!r} in the "
                                    "embedded preset (Bambu Studio will show it as modified)")
