"""Offline tests using the synthetic profile set in tests/fixtures/profiles."""

import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bambu3mf import cli  # noqa: E402
from bambu3mf.edit import ProjectConfigEditor  # noqa: E402
from bambu3mf.mesh import Mesh, rotation, apply  # noqa: E402
from bambu3mf.profiles import ProfileError, ProfileLibrary  # noqa: E402
from bambu3mf.project import Bed, plate_columns  # noqa: E402
from bambu3mf.reader import read_project  # noqa: E402
from bambu3mf.settings import (SettingsBuilder, SettingsError, escape_strings_cstyle, to_bs_value,  # noqa: E402
                               unescape_strings_cstyle, values_equal)
from bambu3mf.spec import SpecError, build_project  # noqa: E402
from bambu3mf.validate import validate  # noqa: E402
from bambu3mf.writer import write_3mf  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def lib():
    return ProfileLibrary(FIXTURES)


def basic_spec(**kw):
    spec = {"printer": "Test Printer", "filaments": ["Test PLA"],
            "objects": [{"name": "cube", "mesh": {"box": [20, 20, 10]}}]}
    spec.update(kw)
    return spec


class ProfileTests(unittest.TestCase):
    def test_inheritance_include_and_meta(self):
        L = lib()
        m = L.resolve("Test Printer 0.4 nozzle")
        self.assertEqual(m["retraction_length"], ["0.6", "0.7"])          # own value wins
        self.assertEqual(m["printable_height"], "150")                     # inherited
        self.assertEqual(m["machine_start_gcode"], "G28 ; template start")  # include beats parent
        self.assertNotIn("inherits", m)
        self.assertNotIn("setting_id", m)
        f = L.resolve("Test PLA @Test")
        self.assertEqual(f["filament_retraction_length"], ["nil", "0.5"])  # nil kept verbatim
        self.assertEqual(L.meta_value("Test PLA @Test", "filament_id"), "TST01")

    def test_lookup(self):
        L = lib()
        m = L.machine_preset("test printer", 0.4)
        self.assertEqual(m, "Test Printer 0.4 nozzle")
        self.assertEqual(L.machine_preset("Test Printer", "0.6"), "Test Printer 0.6 nozzle")
        self.assertEqual(L.process_preset(m), "0.20mm Standard @Test")
        self.assertEqual(L.process_preset(m, "0.12"), "0.12mm Fine @Test")
        self.assertEqual(L.filament_preset(m), "Test PLA @Test")
        self.assertEqual(L.filament_preset(m, "Test PETG"), "Test PETG @Test")
        # exact name for another nozzle falls back to the compatible variant
        self.assertEqual(L.filament_preset("Test Printer 0.6 nozzle", "Test PLA @Test"), "Test PLA @Test 0.6")
        with self.assertRaises(ProfileError):
            L.filament_preset(m, "Unobtainium")
        with self.assertRaises(ProfileError):
            L.machine_preset("Test Printer", 0.8)

    def test_user_preset_dir(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "process").mkdir()
            (Path(d) / "process" / "Mine.json").write_text(json.dumps(
                {"name": "Mine", "inherits": "0.20mm Standard @Test", "from": "User", "wall_loops": "5",
                 "print_settings_id": "Mine"}))
            L = lib()
            self.assertEqual(L.add_user_presets(Path(d)), 1)
            self.assertEqual(L.resolve("Mine")["wall_loops"], "5")
            self.assertEqual(L.system_ancestor("Mine"), "0.20mm Standard @Test")
            b = SettingsBuilder(L, "Test Printer", process="Mine")
            cfg = b.build()
            self.assertEqual(cfg["print_settings_id"], "Mine")
            self.assertEqual(cfg["inherits_group"][0], "0.20mm Standard @Test")
            self.assertEqual(cfg["different_settings_to_system"][0], "wall_loops")


class SettingsTests(unittest.TestCase):
    def test_value_conversion(self):
        L = lib()
        s = L.schema
        self.assertEqual(to_bs_value("wall_loops", 3, "2", s.get("wall_loops")), "3")
        self.assertEqual(to_bs_value("sparse_infill_density", 25, "15%", s.get("sparse_infill_density")), "25%")
        self.assertEqual(to_bs_value("inner_wall_speed", 150, ["100", "120"], s.get("inner_wall_speed")), ["150", "150"])
        self.assertEqual(to_bs_value("inner_wall_speed", [90, 110.5], ["100", "120"], None), ["90", "110.5"])
        self.assertEqual(to_bs_value("enable_support", True, "0", s.get("enable_support")), "1")
        self.assertEqual(to_bs_value("sparse_infill_pattern", "Gyroid", "grid", s.get("sparse_infill_pattern")), "gyroid")
        self.assertEqual(to_bs_value("printable_area", [[0, 0], [10, 0]], ["0x0"], s.get("printable_area")), ["0x0", "10x0"])
        with self.assertRaises(SettingsError):
            to_bs_value("wall_loops", [1, 2], "2", s.get("wall_loops"))

    def test_escape_roundtrip(self):
        items = ["a", "b c", 'q"t', "x;y"]
        self.assertEqual(unescape_strings_cstyle(escape_strings_cstyle(items)), items)
        self.assertEqual(escape_strings_cstyle(["enable_prime_tower", "wall_generator"]),
                         "enable_prime_tower;wall_generator")
        self.assertTrue(values_equal("0.20", "0.2"))
        self.assertFalse(values_equal("20%", "20"))

    def test_builder_diffs_and_layout(self):
        b = SettingsBuilder(lib(), "Test Printer", filaments=["Test PLA", "Test PETG"])
        b.update({"wall_loops": 4, "sparse_infill_pattern": "gyroid", "nozzle_temperature": 205})
        b.set("nozzle_temperature", 250, filament=1)
        b.set("retraction_length", 0.4)
        b.set("wall_loops", 2)  # back to the system value -> not a difference
        cfg = b.build()
        self.assertEqual(cfg["different_settings_to_system"],
                         ["sparse_infill_pattern", "nozzle_temperature", "nozzle_temperature", "retraction_length"])
        self.assertEqual(cfg["nozzle_temperature"], ["205", "205", "250", "250"])   # 2 variants x 2 filaments
        self.assertEqual(cfg["filament_type"], ["PLA", "PETG"])
        self.assertEqual(cfg["filament_self_index"], ["1", "1", "2", "2"])
        self.assertEqual(cfg["filament_settings_id"], ["Test PLA @Test", "Test PETG @Test"])
        self.assertEqual(cfg["filament_ids"], ["TST01", "TST02"])
        self.assertEqual(cfg["filament_colour"][1], "#00AA00")     # default_filament_colour
        self.assertEqual(len(cfg["flush_volumes_matrix"]), 4)
        self.assertEqual(cfg["curr_bed_type"], "Cool Plate")        # machine model default
        self.assertEqual(cfg["print_compatible_printers"], ["Test Printer 0.4 nozzle"])
        self.assertEqual(cfg["version"], "02.01.00.70")
        self.assertNotIn("inherits_group", cfg)                     # all system presets
        for k in ("compatible_printers", "inherits", "compatible_prints"):
            self.assertNotIn(k, cfg)

    def test_validation_errors(self):
        b = SettingsBuilder(lib(), "Test Printer")
        with self.assertRaises(SettingsError):
            b.set("wall_loops", -1)
        with self.assertRaises(SettingsError):
            b.set("sparse_infill_pattern", "hexagons")
        with self.assertRaisesRegex(SettingsError, "did you mean"):
            b.set("wall_lops", 3)
        with self.assertRaises(SettingsError):
            b.set("wall_loops", 3, filament=0)


class MeshTests(unittest.TestCase):
    def test_primitives_volume(self):
        self.assertAlmostEqual(Mesh.box(10, 20, 30).volume(), 6000.0)
        cyl = Mesh.cylinder(10, 10, 256)
        self.assertAlmostEqual(cyl.volume(), 3.14159265 * 100 * 10, delta=10)
        self.assertGreater(Mesh.sphere(10).volume(), 0)
        self.assertGreater(Mesh.cylinder(5, 10, top_radius=0).volume(), 0)

    def test_stl_obj_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.stl"
            Mesh.box(5, 6, 7).save_stl(p)
            m = Mesh.load(p)
            self.assertEqual(len(m.vertices), 8)
            self.assertEqual(len(m.triangles), 12)
            self.assertAlmostEqual(m.volume(), 210.0, places=3)
            o = Path(d) / "q.obj"
            o.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n")
            self.assertEqual(len(Mesh.load(o).triangles), 2)
            a = Path(d) / "a.stl"
            a.write_text("solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
                         "endloop\nendfacet\nendsolid t\n")
            self.assertEqual(len(Mesh.load(a).triangles), 1)

    def test_rotation(self):
        x, y, z = apply(rotation(0, 0, 90), (1, 0, 0))
        self.assertAlmostEqual(x, 0)
        self.assertAlmostEqual(y, 1)

    def test_plate_grid(self):
        self.assertEqual([plate_columns(n) for n in (1, 2, 3, 4, 5, 9, 10)], [1, 2, 2, 2, 3, 3, 4])
        bed = Bed(["0x0", "256x0", "256x256", "0x256"])
        self.assertEqual(bed.plate_origin(1, 2), (256 * 1.2, 0))
        self.assertEqual(bed.plate_origin(2, 4), (0, -256 * 1.2))


class WriterReaderTests(unittest.TestCase):
    def build(self, spec):
        project, builder = build_project(spec, lib())
        tmp = tempfile.mkdtemp()
        out = Path(tmp) / "t.3mf"
        write_3mf(project, out)
        return project, builder, out

    def test_archive_structure_matches_bambu_layout(self):
        spec = basic_spec(objects=[
            {"name": "a", "mesh": {"box": [20, 20, 10]}, "copies": 2,
             "modifiers": [{"mesh": {"box": [5, 5, 5]}, "offset": [2, 2, 2], "settings": {"wall_loops": 5}}]},
            {"name": "b", "mesh": {"cylinder": {"radius": 5, "height": 15}},
             "height_ranges": [{"min_z": 5, "max_z": 10, "settings": {"layer_height": 0.1}}]}])
        _, _, out = self.build(spec)
        z = zipfile.ZipFile(out)
        names = set(z.namelist())
        for n in ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels",
                  "3D/Objects/object_1.model", "3D/Objects/object_2.model", "Metadata/project_settings.config",
                  "Metadata/model_settings.config", "Metadata/slice_info.config", "Metadata/layer_config_ranges.xml",
                  "Metadata/plate_1.png", "Metadata/plate_1_small.png"):
            self.assertIn(n, names)
        model = z.read("3D/3dmodel.model").decode()
        self.assertIn('<metadata name="Application">BambuStudio-02.01.00.70</metadata>', model)
        # object a: volumes 1,2 -> object id 3; object b: volume 4 -> object id 5
        self.assertIn('<object id="3" p:UUID="00000001-61cb-4c03-9d28-80fed5dfa1dc" type="model">', model)
        self.assertIn('objectid="2" p:UUID="00010001-b206-40ff-9872-83e8017abed1"', model)
        self.assertIn('<object id="5" p:UUID="00000002-61cb-4c03-9d28-80fed5dfa1dc"', model)
        self.assertEqual(model.count('<item objectid="3"'), 2)
        sub = z.read("3D/Objects/object_1.model").decode()
        self.assertIn('<object id="2" p:UUID="00010001-81cb-4c03-9d28-80fed5dfa1dc" type="other">', sub)
        ms = ET.fromstring(z.read("Metadata/model_settings.config"))
        parts = ms.find("object").findall("part")
        self.assertEqual([p.get("subtype") for p in parts], ["normal_part", "modifier_part"])
        self.assertEqual(len(ms.find("plate").findall("model_instance")), 3)
        ranges = z.read("Metadata/layer_config_ranges.xml").decode()
        self.assertIn('<object id="2">', ranges)
        self.assertIn('<option opt_key="layer_height">0.1</option>', ranges)
        cfg = json.loads(z.read("Metadata/project_settings.config"))
        self.assertEqual(list(cfg), sorted(cfg))
        self.assertTrue(validate(out, lib()).ok, validate(out, lib()).errors)

    def test_placement_on_bed_and_roundtrip(self):
        spec = basic_spec(objects=[{"name": "c", "mesh": {"box": [20, 20, 10]}, "position": [50, 60],
                                    "rotate": [0, 0, 45], "settings": {"wall_loops": 4}, "filament": 1}])
        project, _, out = self.build(spec)
        q = read_project(out)
        self.assertEqual(len(q.objects), 1)
        o = q.objects[0]
        self.assertAlmostEqual(o.instances[0].x, 50, places=4)
        self.assertAlmostEqual(o.instances[0].y, 60, places=4)
        lo, hi = o.bounds()
        self.assertAlmostEqual(hi[0] - lo[0], 20 * 2 ** 0.5, places=3)
        self.assertEqual(o.settings, {"wall_loops": "4"})
        self.assertEqual(q.config, project.config)

    def test_overflow_creates_plates(self):
        spec = basic_spec(objects=[{"name": "big", "mesh": {"box": [90, 80, 5]}, "copies": 6}])
        project, _, out = self.build(spec)
        self.assertGreaterEqual(len(project.plates), 2)
        q = read_project(out)
        self.assertEqual(sorted({i.plate for o in q.objects for i in o.instances}), list(range(len(project.plates))))
        self.assertTrue(validate(out, lib()).ok)

    def test_spec_errors(self):
        with self.assertRaises(SpecError):
            build_project(basic_spec(objects=[{"mesh": {"box": [1, 1, 1]}, "settings": {"nozzle_temperature": 200}}]),
                          lib())
        with self.assertRaises(SpecError):
            build_project(basic_spec(objects=[{"mesh": {"box": [1, 1, 1]}, "filament": 3}]), lib())
        with self.assertRaises(SpecError):
            build_project(basic_spec(objects=[{"mesh": {"box": [1, 1, 1]}, "colour": "red"}]), lib())
        with self.assertRaises(SpecError):
            build_project(basic_spec(settings={"wall_loops": "many"}), lib())

    def test_edit_keeps_diffs(self):
        project, _, out = self.build(basic_spec(filaments=["Test PLA", "Test PETG"], settings={"wall_loops": 3}))
        q = read_project(out)
        ed = ProjectConfigEditor(q.config, lib().schema)
        ed.set("sparse_infill_density", "30%")
        ed.set("nozzle_temperature", 222, filament=1)
        cfg = ed.finish()
        self.assertEqual(cfg["different_settings_to_system"][0], "sparse_infill_density;wall_loops")
        self.assertEqual(cfg["different_settings_to_system"][2], "nozzle_temperature")
        self.assertEqual(cfg["nozzle_temperature"][2:], ["222", "222"])
        self.assertEqual(cfg["nozzle_temperature"][:2], ["210", "215"])


class EmbeddedPresetTests(unittest.TestCase):
    def variant_spec(self):
        return basic_spec(profile="Widget", settings={"wall_loops": 4}, variants=[
            {"name": "PLA", "filament": "Test PLA", "color": "#FF0000"},
            {"name": "PETG", "filament": "Test PETG", "process_settings": {"seam_position": "back"},
             "filament_settings": {"nozzle_temperature": 250}}])

    def test_single_build_embeds_named_presets(self):
        project, builder = build_project(basic_spec(profile="Cube", settings={"wall_loops": 3}), lib())
        cfg = project.config
        self.assertEqual(cfg["print_settings_id"], "Cube")
        self.assertEqual(cfg["filament_settings_id"], ["Cube - Test PLA"])
        self.assertEqual(cfg["inherits_group"], ["0.20mm Standard @Test", "Test PLA @Test", ""])
        proc = [p for p in project.embedded_presets if p["type"] == "process"][0]["config"]
        self.assertEqual(proc["inherits"], "0.20mm Standard @Test")
        self.assertEqual(proc["print_settings_id"], "Cube")
        self.assertEqual(proc["wall_loops"], "3")
        self.assertEqual(proc["print_extruder_variant"], ["Direct Drive Standard", "Direct Drive High Flow"])
        self.assertEqual(proc["from"], "project")
        fil = [p for p in project.embedded_presets if p["type"] == "filament"][0]["config"]
        self.assertEqual(fil["filament_settings_id"], ["Cube - Test PLA"])
        self.assertNotIn("nozzle_temperature", fil)       # unchanged keys are not stored

    def test_embed_can_be_disabled(self):
        project, _ = build_project(basic_spec(profile={"embed": False}), lib())
        self.assertEqual(project.embedded_presets, [])
        self.assertEqual(project.config["print_settings_id"], "0.20mm Standard @Test")

    def test_variants(self):
        project, builder = build_project(self.variant_spec(), lib())
        names = [(p["type"], p["name"]) for p in project.embedded_presets]
        self.assertEqual(names, [("process", "Widget - PLA"), ("process", "Widget - PETG"),
                                 ("filament", "Widget - PLA"), ("filament", "Widget - PETG")])
        cfg = project.config
        self.assertEqual(cfg["print_settings_id"], "Widget - PLA")       # first variant is active
        self.assertEqual(cfg["filament_colour"], ["#FF0000"])
        petg_proc = dict(project.embedded_presets[1]["config"])
        self.assertEqual(petg_proc["wall_loops"], "4")                  # common settings in every variant
        self.assertEqual(petg_proc["seam_position"], "back")
        petg_fil = project.embedded_presets[3]["config"]
        self.assertEqual(petg_fil["inherits"], "Test PETG @Test")
        self.assertEqual(petg_fil["nozzle_temperature"], ["250", "250"])
        spec = self.variant_spec()
        spec["active_variant"] = "PETG"
        project2, _ = build_project(spec, lib())
        self.assertEqual(project2.config["print_settings_id"], "Widget - PETG")
        self.assertEqual(project2.config["nozzle_temperature"], ["250", "250"])
        with tempfile.TemporaryDirectory() as d:
            out = write_3mf(project, Path(d) / "v.3mf")
            names_in_zip = zipfile.ZipFile(out).namelist()
            for n in ("Metadata/process_settings_1.config", "Metadata/process_settings_2.config",
                      "Metadata/filament_settings_1.config", "Metadata/filament_settings_2.config"):
                self.assertIn(n, names_in_zip)
            rep = validate(out, lib())
            self.assertTrue(rep.ok, rep.errors)
            self.assertEqual(rep.warnings, [])
            q = read_project(out)
            self.assertEqual(q.embedded_presets, project.embedded_presets)

    def test_variant_errors(self):
        spec = self.variant_spec()
        spec["variants"][1]["name"] = "PLA"
        with self.assertRaises(SpecError):
            build_project(spec, lib())
        spec = self.variant_spec()
        spec["active_variant"] = "ABS"
        with self.assertRaises(SpecError):
            build_project(spec, lib())
        with self.assertRaises(SettingsError):
            b = SettingsBuilder(lib(), "Test Printer")
            b.name_presets(process="0.12mm Fine @Test")              # clashes with a real preset

    def test_edit_variants(self):
        project, _ = build_project(self.variant_spec(), lib())
        ed = ProjectConfigEditor(project.config, lib().schema, project.embedded_presets)
        ed.set("wall_loops", 5)                                       # active variant follows
        self.assertEqual(ed.find_embedded("Widget - PLA", "process")["config"]["wall_loops"], "5")
        self.assertEqual(ed.find_embedded("Widget - PETG", "process")["config"]["wall_loops"], "4")
        self.assertEqual(ed.set_in_preset("Widget - PETG", "nozzle_temperature", 245), "inactive")
        self.assertEqual(ed.find_embedded("Widget - PETG", "filament")["config"]["nozzle_temperature"],
                         ["245", "245"])
        self.assertEqual(project.config["nozzle_temperature"], ["210", "215"])   # active PLA untouched
        ed.set("nozzle_temperature", 205, filament=0)
        self.assertEqual(ed.find_embedded("Widget - PLA", "filament")["config"]["nozzle_temperature"],
                         ["205", "205"])
        with self.assertRaises(SettingsError):
            ed.set_in_preset("Nope", "wall_loops", 3)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--profiles", str(FIXTURES), "--no-user-presets", *args])
        return code, buf.getvalue()

    def test_build_inspect_edit_validate(self):
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / "s.json"
            spec.write_text(json.dumps(basic_spec(settings={"seam_position": "back"})))
            out = Path(d) / "s.3mf"
            code, text = self.run_cli("build", str(spec), "-o", str(out), "--set", "wall_loops=3")
            self.assertEqual(code, 0, text)
            self.assertTrue(json.loads(text)["validation"]["ok"])
            code, text = self.run_cli("inspect", str(out))
            info = json.loads(text)
            self.assertEqual(info["changed_from_system"]["process"], ["seam_position", "wall_loops"])
            code, text = self.run_cli("edit", str(out), "--set", "layer_height=0.16", "-o", str(out))
            self.assertEqual(code, 0, text)
            self.assertIn("layer_height", json.loads(text)["different_settings_to_system"][0])
            code, text = self.run_cli("validate", str(out))
            self.assertEqual(code, 0, text)

    def test_variants_extract_presets(self):
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / "v.json"
            spec.write_text(json.dumps(basic_spec(profile="W", variants=[
                {"name": "PLA", "filament": "Test PLA"},
                {"name": "PETG", "filament": "Test PETG", "filament_settings": {"nozzle_temperature": 250}}])))
            out = Path(d) / "v.3mf"
            code, text = self.run_cli("build", str(spec), "-o", str(out))
            self.assertEqual(code, 0, text)
            self.assertEqual([v["variant"] for v in json.loads(text)["variants"]], ["PLA", "PETG"])
            code, text = self.run_cli("edit", str(out), "--preset", "W - PETG", "--set", "nozzle_temperature=245")
            self.assertEqual(code, 0, text)
            code, text = self.run_cli("extract-presets", str(out), "-o", str(Path(d) / "presets"))
            self.assertEqual(code, 0, text)
            files = json.loads(text)["written"]
            self.assertEqual(len(files), 4)
            petg = json.loads((Path(d) / "presets" / "W - PETG (filament).json").read_text())
            self.assertEqual(petg["from"], "User")
            self.assertEqual(petg["inherits"], "Test PETG @Test")
            self.assertEqual(petg["nozzle_temperature"], ["245", "245"])

    def test_export_preset(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            code, text = self.run_cli("export-preset", "--type", "filament", "--printer", "Test Printer",
                                      "--base", "Test PLA", "--name", "My PLA", "--set", "nozzle_temperature=205",
                                      "-o", str(out))
            self.assertEqual(code, 0, text)
            p = json.loads(out.read_text())
            self.assertEqual(p["inherits"], "Test PLA @Test")
            self.assertEqual(p["filament_settings_id"], ["My PLA"])
            self.assertEqual(p["nozzle_temperature"], ["205", "205"])
            self.assertEqual(p["filament_id"], "TST01")


if __name__ == "__main__":
    unittest.main()
