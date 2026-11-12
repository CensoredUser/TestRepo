"""Write a :class:`Project` as a Bambu Studio project 3MF.

The archive layout, IDs, UUIDs, attribute order and number formatting follow
``_BBS_3MF_Exporter`` in Bambu Studio (src/libslic3r/Format/bbs_3mf.cpp) with
the production extension and split-model output it uses by default:

    [Content_Types].xml
    _rels/.rels
    3D/3dmodel.model                  build items + component objects
    3D/_rels/3dmodel.model.rels
    3D/Objects/object_<n>.model       one mesh object per part (volume)
    Metadata/project_settings.config  full flattened config (JSON)
    Metadata/model_settings.config    objects/parts/plates (XML)
    Metadata/layer_config_ranges.xml  height-range modifiers (optional)
    Metadata/process_settings_<n>.config, filament_settings_<n>.config,
    machine_settings_<n>.config       project-embedded presets (optional)
    Metadata/slice_info.config        client header
    Metadata/plate_<n>.png / plate_<n>_small.png
"""

from __future__ import annotations

import datetime as _dt
import json
import struct
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

from .mesh import Matrix, Mesh, translation
from .project import ModelObject, Project
from . import thumbnail as _thumb

NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')
OBJECT_UUID_SUFFIX = "-61cb-4c03-9d28-80fed5dfa1dc"
SUB_OBJECT_UUID_SUFFIX = "-81cb-4c03-9d28-80fed5dfa1dc"
COMPONENT_UUID_SUFFIX = "-b206-40ff-9872-83e8017abed1"
BUILD_UUID = "2c7c17d8-22b5-4d84-8835-1976022ea369"
BUILD_UUID_SUFFIX = "-b1ec-4553-aec9-835e5b724bb4"
MODEL_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
# bbs_3mf.hpp EMBEDDED_PRINT/FILAMENT/PRINTER_FILE_FORMAT
EMBEDDED_FILE = {"process": "process_settings", "filament": "filament_settings", "machine": "machine_settings"}


def xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _f9(x: float) -> str:
    s = "%.9g" % x
    return "0" if s == "-0" else s


def _f32(x: float) -> str:
    return "%.9g" % struct.unpack("<f", struct.pack("<f", x))[0]


def _f17(x: float) -> str:
    s = "%.17g" % x
    return "0" if s in ("-0", "0") else s


def transform_attr(m: Matrix) -> str:
    """4x4 -> 3MF 'transform' (column-major 3x4), as add_transformation()."""
    return " ".join(_f9(m[r][c]) for c in range(4) for r in range(3))


def matrix_row_major(m: Matrix) -> str:
    return " ".join(_f17(m[r][c]) for r in range(4) for c in range(4))


def config_value(v) -> str:
    """ModelConfig::opt_serialize for values stored as Bambu strings / lists."""
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


class _Volume:
    def __init__(self, part, mesh: Mesh, matrix: Matrix, vid: int):
        self.part, self.mesh, self.matrix, self.id = part, mesh, matrix, vid


class _ObjData:
    def __init__(self, obj: ModelObject, backup_id: int):
        self.obj = obj
        self.backup_id = backup_id
        self.volumes: List[_Volume] = []
        self.id = 0
        self.center = (0.0, 0.0, 0.0)
        self.z0 = 0.0

    @property
    def sub_path(self) -> str:
        return f"3D/Objects/object_{self.backup_id}.model"


def _prepare(project: Project) -> List[_ObjData]:
    datas = []
    next_id = 1
    for idx, obj in enumerate(project.objects, start=1):
        d = _ObjData(obj, idx)
        lo, hi = obj.bounds()
        d.center = tuple((lo[i] + hi[i]) / 2 for i in range(3))
        d.z0 = d.center[2] - lo[2]
        for part, mesh in obj.object_space_parts():
            if not mesh.triangles:
                raise ValueError(f"object '{obj.name}' has an empty part '{part.name}'")
            mesh.fix_orientation()
            plo, phi = mesh.bbox()
            pc = tuple((plo[i] + phi[i]) / 2 for i in range(3))
            stored = mesh.translated(-pc[0], -pc[1], -pc[2])
            mat = translation(pc[0] - d.center[0], pc[1] - d.center[1], pc[2] - d.center[2])
            d.volumes.append(_Volume(part, stored, mat, next_id))
            next_id += 1
        d.id = next_id
        next_id += 1
        datas.append(d)
    return datas


def _instance_matrix(project: Project, d: _ObjData, inst) -> Matrix:
    ox, oy = project.bed.plate_origin(project.plate_of(d.obj, inst), len(project.plates))
    return translation(ox + inst.x, oy + inst.y, d.z0)


# ---------------------------------------------------------------------------
# individual files
# ---------------------------------------------------------------------------

def _content_types() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            ' <Default Extension="png" ContentType="image/png"/>\n'
            ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
            '</Types>')


def _root_rels(with_thumbs: bool) -> str:
    s = ('<?xml version="1.0" encoding="UTF-8"?>\n'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
         f' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{MODEL_REL}"/>\n')
    if with_thumbs:
        s += (' <Relationship Target="/Metadata/plate_1.png" Id="rel-2" '
              'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>\n'
              ' <Relationship Target="/Metadata/plate_1.png" Id="rel-4" '
              'Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"/>\n'
              '<Relationship Target="/Metadata/plate_1_small.png" Id="rel-5" '
              'Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-small"/>\n')
    return s + "</Relationships>"


def _model_rels(datas: List[_ObjData]) -> str:
    s = ('<?xml version="1.0" encoding="UTF-8"?>\n'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n')
    for i, d in enumerate(datas, start=1):
        s += f' <Relationship Target="/{xml_escape(d.sub_path)}" Id="rel-{i}" Type="{MODEL_REL}"/>\n'
    return s + "</Relationships>"


def _root_model(project: Project, datas: List[_ObjData], with_thumbs: bool) -> str:
    md = project.metadata
    date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    items: Dict[str, str] = {
        "Application": f"BambuStudio-{project.app_version}",
        "BambuStudio:3mfVersion": "1",
        "Copyright": md.copyright, "CreationDate": date, "Description": md.description,
        "Designer": md.designer, "DesignerCover": "", "DesignerUserId": "", "License": md.license,
        "ModificationDate": date, "Origin": md.origin, "ProfileCover": "", "ProfileDescription": "",
        "ProfileTitle": "", "Title": md.title,
    }
    if with_thumbs:
        items["Thumbnail_Middle"] = "/Metadata/plate_1.png"
        items["Thumbnail_Small"] = "/Metadata/plate_1_small.png"
    out = [f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n']
    for k in sorted(items):
        out.append(f' <metadata name="{k}">{xml_escape(items[k])}</metadata>\n')
    out.append(" <resources>\n")
    for d in datas:
        out.append(f'  <object id="{d.id}" p:UUID="{d.backup_id:08x}{OBJECT_UUID_SUFFIX}" type="model">\n')
        out.append("   <components>\n")
        for idx, v in enumerate(d.volumes):
            out.append(f'    <component p:path="/{xml_escape(d.sub_path)}" objectid="{v.id}" '
                       f'p:UUID="{(d.backup_id << 16) + idx:08x}{COMPONENT_UUID_SUFFIX}" '
                       f'transform="{transform_attr(v.matrix)}"/>\n')
        out.append("   </components>\n  </object>\n")
    out.append(" </resources>\n")
    items_xml = []
    for d in datas:
        for inst in d.obj.instances:
            m = _instance_matrix(project, d, inst)
            items_xml.append(f'  <item objectid="{d.id}" p:UUID="{d.id:08x}{BUILD_UUID_SUFFIX}" '
                             f'transform="{transform_attr(m)}" printable="{1 if inst.printable else 0}"/>\n')
    if items_xml:
        out.append(f' <build p:UUID="{BUILD_UUID}">\n' + "".join(items_xml) + " </build>\n")
    else:
        out.append(" <build/>\n")
    out.append("</model>\n")
    return "".join(out)


def _object_model(d: _ObjData) -> str:
    out = [f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n',
           ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n']
    for idx, v in enumerate(d.volumes):
        typ = "model" if v.part.subtype == "normal_part" else "other"
        out.append(f'  <object id="{v.id}" p:UUID="{(d.backup_id << 16) + idx:08x}{SUB_OBJECT_UUID_SUFFIX}" '
                   f'type="{typ}">\n   <mesh>\n    <vertices>\n')
        out.extend(f'     <vertex x="{_f32(x)}" y="{_f32(y)}" z="{_f32(z)}"/>\n' for x, y, z in v.mesh.vertices)
        out.append("    </vertices>\n    <triangles>\n")
        attrs = v.mesh.triangle_attrs
        for i, (a, b, c) in enumerate(v.mesh.triangles):
            extra = ""
            if attrs and i < len(attrs) and attrs[i]:
                extra = "".join(f' {k}="{xml_escape(val)}"' for k, val in attrs[i].items())
            out.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"{extra}/>\n')
        out.append("    </triangles>\n   </mesh>\n  </object>\n")
    out.append(" </resources>\n <build/>\n</model>\n")
    return "".join(out)


def _model_settings(project: Project, datas: List[_ObjData], with_thumbs: bool) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for d in datas:
        obj = d.obj
        out.append(f'  <object id="{d.id}">\n')
        if obj.name:
            out.append(f'    <metadata key="name" value="{xml_escape(obj.name)}"/>\n')
        cfg = dict(obj.settings)
        cfg["extruder"] = str(obj.extruder)
        for k in sorted(cfg):
            out.append(f'    <metadata key="{k}" value="{xml_escape(config_value(cfg[k]))}"/>\n')
        faces = sum(len(v.mesh.triangles) for v in d.volumes)
        out.append(f'    <metadata face_count="{faces}"/>\n')
        for v in d.volumes:
            p = v.part
            out.append(f'    <part id="{v.id}" subtype="{p.subtype}">\n')
            out.append(f'      <metadata key="name" value="{xml_escape(p.name or obj.name)}"/>\n')
            out.append(f'      <metadata key="matrix" value="{matrix_row_major(v.matrix)}"/>\n')
            if p.mesh.source:
                for key, val in (("source_file", p.mesh.source), ("source_object_id", "0"),
                                 ("source_volume_id", "0"), ("source_offset_x", "0"),
                                 ("source_offset_y", "0"), ("source_offset_z", "0")):
                    out.append(f'      <metadata key="{key}" value="{xml_escape(val)}"/>\n')
            pcfg = dict(p.settings)
            if p.extruder is not None:
                pcfg["extruder"] = str(p.extruder)
            for k in sorted(pcfg):
                out.append(f'      <metadata key="{k}" value="{xml_escape(config_value(pcfg[k]))}"/>\n')
            out.append(f'      <mesh_stat face_count="{len(v.mesh.triangles)}" edges_fixed="0" '
                       'degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n')
            out.append("    </part>\n")
        out.append("  </object>\n")
    identify = 100
    for pi, plate in enumerate(project.plates):
        out.append("  <plate>\n")
        out.append(f'    <metadata key="plater_id" value="{pi + 1}"/>\n')
        out.append(f'    <metadata key="plater_name" value="{xml_escape(plate.name)}"/>\n')
        out.append(f'    <metadata key="locked" value="{"true" if plate.locked else "false"}"/>\n')
        if plate.bed_type:
            out.append(f'    <metadata key="bed_type" value="{xml_escape(plate.bed_type)}"/>\n')
        if plate.print_sequence:
            out.append(f'    <metadata key="print_sequence" value="{xml_escape(plate.print_sequence)}"/>\n')
        if plate.spiral_mode is not None:
            # Bambu Studio parses this with std::boolalpha: it must be "true"/"false"
            out.append(f'    <metadata key="spiral_mode" value="{"true" if plate.spiral_mode else "false"}"/>\n')
        if with_thumbs:
            out.append(f'    <metadata key="thumbnail_file" value="Metadata/plate_{pi + 1}.png"/>\n')
        for d in datas:
            for ii, inst in enumerate(d.obj.instances):
                if project.plate_of(d.obj, inst) != pi:
                    continue
                identify += 1
                out.append("    <model_instance>\n"
                           f'      <metadata key="object_id" value="{d.id}"/>\n'
                           f'      <metadata key="instance_id" value="{ii}"/>\n'
                           f'      <metadata key="identify_id" value="{identify}"/>\n'
                           "    </model_instance>\n")
        out.append("  </plate>\n")
    out.append("  <assemble>\n  </assemble>\n</config>\n")
    return "".join(out)


def _layer_config_ranges(project: Project) -> str:
    body = []
    for idx, obj in enumerate(project.objects, start=1):
        if not obj.layer_ranges:
            continue
        s = f'<object id="{idx}">'
        for r in sorted(obj.layer_ranges, key=lambda r: (r.min_z, r.max_z)):
            s += f'<range min_z="{_f17g(r.min_z)}" max_z="{_f17g(r.max_z)}">'
            for k in sorted(r.settings):
                s += f'<option opt_key="{k}">{xml_escape(config_value(r.settings[k]))}</option>'
            s += "</range>"
        body.append(s + "</object>")
    if not body:
        return ""
    xml = '<?xml version="1.0" encoding="utf-8"?>\n<objects>' + "".join(body) + "</objects>"
    return xml.replace("><", ">\n<") + "\n"


def _f17g(x: float) -> str:
    return "%.17g" % x if x != int(x) else str(int(x))


def _slice_info(project: Project) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n'
            '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
            f'    <header_item key="X-BBL-Client-Version" value="{project.app_version}"/>\n'
            '  </header>\n</config>\n')


def project_settings_json(config: dict) -> str:
    return json.dumps(config, indent=4, sort_keys=True, ensure_ascii=False) + "\n"


def _plate_thumbnails(project: Project, datas: List[_ObjData], plate: int) -> Tuple[bytes, bytes]:
    colours = project.config.get("filament_colour") or ["#C0C0C0"]
    tris = []
    for d in datas:
        for inst in d.obj.instances:
            if project.plate_of(d.obj, inst) != plate:
                continue
            m = _instance_matrix(project, d, inst)
            for v in d.volumes:
                if v.part.subtype != "normal_part":
                    continue
                ext = v.part.extruder or d.obj.extruder
                col = colours[(ext - 1) % len(colours)] if colours else "#C0C0C0"
                full = [[sum(m[r][k] * v.matrix[k][c] for k in range(4)) for c in range(4)] for r in range(4)]
                world = v.mesh.transformed(full)
                vs = world.vertices
                tris.extend(((vs[a], vs[b], vs[c]), col) for a, b, c in world.triangles)
    return _thumb.render(tris, 256), _thumb.render(tris, 128)


def write_3mf(project: Project, path, thumbnails: bool = True) -> Path:
    """Write *project* to *path*; returns the path."""
    path = Path(path)
    datas = _prepare(project)
    files: List[Tuple[str, bytes, int]] = []
    D, S = zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED
    files.append(("[Content_Types].xml", _content_types().encode(), D))
    if thumbnails:
        for pi in range(len(project.plates)):
            big, small = _plate_thumbnails(project, datas, pi)
            files.append((f"Metadata/plate_{pi + 1}.png", big, S))
            files.append((f"Metadata/plate_{pi + 1}_small.png", small, S))
    files.append(("3D/3dmodel.model", _root_model(project, datas, thumbnails).encode(), D))
    if datas:
        files.append(("3D/_rels/3dmodel.model.rels", _model_rels(datas).encode(), D))
        for d in datas:
            files.append((d.sub_path, _object_model(d).encode(), D))
    files.append(("Metadata/project_settings.config", project_settings_json(project.config).encode(), D))
    counters = {"process": 0, "filament": 0, "machine": 0}
    for preset in project.embedded_presets:
        kind = preset["type"]
        counters[kind] += 1
        files.append((f"Metadata/{EMBEDDED_FILE[kind]}_{counters[kind]}.config",
                      project_settings_json(preset["config"]).encode(), D))
    files.append(("Metadata/model_settings.config", _model_settings(project, datas, thumbnails).encode(), D))
    ranges = _layer_config_ranges(project)
    if ranges:
        files.append(("Metadata/layer_config_ranges.xml", ranges.encode(), D))
    files.append(("Metadata/slice_info.config", _slice_info(project).encode(), D))
    files.append(("_rels/.rels", _root_rels(thumbnails).encode(), D))
    written = {f[0] for f in files}
    for name, data in project.extra_files.items():
        if name not in written:
            files.append((name, data, S if name.endswith(".png") else D))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        for name, data, comp in files:
            zi = zipfile.ZipInfo(name, date_time=_dt.datetime.now().timetuple()[:6])
            zi.compress_type = comp
            zi.external_attr = 0o644 << 16
            z.writestr(zi, data)
    tmp.replace(path)
    return path
