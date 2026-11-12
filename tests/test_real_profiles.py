"""Integration tests against the real Bambu Lab profiles.

Skipped unless profiles are available (run `python -m bambu3mf profiles fetch`
first, or set BAMBU3MF_PROFILES). Set BAMBU3MF_REF_DIR to a folder containing
Bambu Studio's resources/calib/pressure_advance/auto_pa_line_single.3mf to
also compare against a project saved by Bambu Studio itself.
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bambu3mf.profiles import ProfileError, ProfileLibrary  # noqa: E402
from bambu3mf.reader import read_project  # noqa: E402
from bambu3mf.settings import SettingsBuilder  # noqa: E402
from bambu3mf.spec import build_project, load_spec  # noqa: E402
from bambu3mf.validate import validate  # noqa: E402
from bambu3mf.writer import write_3mf  # noqa: E402

try:
    LIB = ProfileLibrary.open(with_user_presets=False)
except ProfileError:
    LIB = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(LIB is None, "Bambu profiles not available (run: python -m bambu3mf profiles fetch)")
class RealProfileTests(unittest.TestCase):
    def test_examples_build_and_validate(self):
        for ex in sorted((ROOT / "examples").glob("*.json")):
            with self.subTest(example=ex.name):
                spec, base = load_spec(ex)
                project, _ = build_project(spec, LIB, base)
                with tempfile.TemporaryDirectory() as d:
                    out = write_3mf(project, Path(d) / "x.3mf", thumbnails=False)
                    rep = validate(out, LIB)
                    self.assertTrue(rep.ok, rep.errors)
                    self.assertEqual(read_project(out).config, project.config)

    def test_every_printer_model(self):
        self.checked = 0
        for model in LIB.printer_models():
            for nozzle in model["nozzles"]:
                with self.subTest(printer=model["name"], nozzle=nozzle):
                    try:
                        machine = LIB.machine_preset(model["name"], nozzle)
                    except ProfileError:
                        continue
                    b = SettingsBuilder(LIB, machine, filaments=[None, None])
                    system_walls = LIB.resolve(b.process).get("wall_loops")
                    walls = 5 if system_walls != "5" else 4
                    b.update({"wall_loops": walls})
                    cfg = b.build()
                    self.assertEqual(cfg["wall_loops"], str(walls))
                    self.assertIn("wall_loops", cfg["different_settings_to_system"][0].split(";"))
                    self.assertEqual(len(cfg["filament_colour"]), 2)
                    self.assertEqual(len(cfg["different_settings_to_system"]), 4)
                    self.checked += 1
        self.assertGreater(self.checked, 40)

    @unittest.skipUnless(os.environ.get("BAMBU3MF_REF_DIR"), "BAMBU3MF_REF_DIR not set")
    def test_reproduces_bambu_reference_project(self):
        ref_path = Path(os.environ["BAMBU3MF_REF_DIR"]) / "auto_pa_line_single.3mf"
        ref = json.loads(zipfile.ZipFile(ref_path).read("Metadata/project_settings.config"))
        b = SettingsBuilder(LIB, ref["printer_settings_id"], process=ref["print_settings_id"],
                            filaments=ref["filament_settings_id"])
        b.set("filament_colour", ref["filament_colour"])
        for key in ref["different_settings_to_system"][0].split(";"):
            b.set(key, ref[key])
        cfg = b.build()
        self.assertEqual(cfg["different_settings_to_system"], ref["different_settings_to_system"])
        self.assertEqual(cfg["filament_self_index"], ref["filament_self_index"])
        self.assertEqual(cfg["print_compatible_printers"], ref["print_compatible_printers"])
        same = sum(a == r for a, r in zip(cfg["flush_volumes_matrix"], ref["flush_volumes_matrix"]))
        self.assertGreaterEqual(same, 60)   # 3 entries in the reference are stale user edits
        # read -> write -> read keeps geometry positions exactly
        project = read_project(ref_path)
        with tempfile.TemporaryDirectory() as d:
            out = write_3mf(project, Path(d) / "rt.3mf")
            again = read_project(out)
        for a, c in zip(project.objects, again.objects):
            self.assertEqual([(round(i.x, 4), round(i.y, 4)) for i in a.instances],
                             [(round(i.x, 4), round(i.y, 4)) for i in c.instances])


if __name__ == "__main__":
    unittest.main()
