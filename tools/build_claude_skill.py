#!/usr/bin/env python3
"""Package bambu3mf as a claude.ai skill (bambu-print.zip).

Upload the zip in claude.ai: Customize > Skills > "+" > Create skill > Upload a skill.
Code execution / file creation must be enabled (Settings > Capabilities).

    python tools/build_claude_skill.py --printer "P1S" --nozzle 0.4 \
        --plate "Textured PEI Plate" --filaments "1: Bambu PLA Basic white; 2: Bambu PETG HF black" \
        --studio-version 02.04.00.70

The Bambu profiles are bundled so the skill works without network access.
Use --ref to bundle the profiles of your Bambu Studio version (git tag such as
v02.04.00.70) or --profiles to bundle a folder you already have.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bambu3mf.profiles import ProfileLibrary, fetch_profiles, find_profile_root, normalize_version  # noqa: E402

SKILL_NAME = "bambu-print"


def setup_text(args) -> str:
    lines = []
    if args.printer:
        lines.append(f"- Printer: {args.printer}, {args.nozzle} mm nozzle"
                     + (f" ({args.nozzle_note})" if args.nozzle_note else ""))
    if args.filaments:
        lines.append(f"- Filaments usually loaded: {args.filaments}")
    if args.plate:
        lines.append(f"- Usual build plate: {args.plate} (spec `bed_type`)")
    if args.studio_version:
        lines.append(f"- Bambu Studio version: {args.studio_version} (use `\"app_version\": "
                     f"\"{normalize_version(args.studio_version)}\"` in specs)")
    if args.notes:
        lines.append(f"- Notes: {args.notes}")
    if not lines:
        return ("Not configured. Ask the user for printer model and nozzle, the filaments they "
                "have, and their usual build plate, then use them for the rest of the chat.")
    return "\n".join(lines + ["", "Use these as defaults; the user can override them in the chat."])


def zip_dir(src: Path, out: Path, arc_root: str = "") -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, str(Path(arc_root) / f.relative_to(src)) if arc_root else str(f.relative_to(src)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out-dir", default=str(ROOT / "dist"))
    ap.add_argument("--profiles", help="profile folder to bundle (default: installed Bambu Studio or fetch cache)")
    ap.add_argument("--ref", help="fetch and bundle the profiles of this Bambu Studio git ref (e.g. v02.04.00.70)")
    ap.add_argument("--printer", help='e.g. "P1S"')
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--nozzle-note", help='e.g. "hardened steel"')
    ap.add_argument("--filaments", help='e.g. "AMS 1: Bambu PLA Basic white, 2: Bambu PETG HF black"')
    ap.add_argument("--plate", help='e.g. "Textured PEI Plate"')
    ap.add_argument("--studio-version", help="Bambu Studio version (Help > About)")
    ap.add_argument("--notes", help="anything else Claude should always know")
    args = ap.parse_args()

    if args.ref:
        prof = fetch_profiles(args.ref, log=lambda m: print(m, file=sys.stderr))
    else:
        prof = find_profile_root(args.profiles)
    lib = ProfileLibrary(prof)
    if not lib.schema:
        print("warning: no schema.json in the profile folder; settings will not be validated. "
              "Use --ref or `bambu3mf profiles fetch` to get one.", file=sys.stderr)
    if args.printer:
        lib.machine_preset(args.printer, args.nozzle)   # fail early on a typo

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="skill-"))
    try:
        skill = work / SKILL_NAME
        skill.mkdir()
        info = f"profile set {lib.profile_version}" + (f", Bambu Studio {lib.app_version}" if lib.app_version else "")
        text = (ROOT / "skill" / "SKILL.md.in").read_text(encoding="utf-8")
        text = text.replace("{{PROFILE_INFO}}", info).replace("{{SETUP}}", setup_text(args))
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        shutil.copy2(ROOT / "skill" / "run.py", skill / "run.py")
        shutil.copytree(ROOT / "bambu3mf", skill / "bambu3mf",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (skill / "references").mkdir()
        for doc in ("SPEC.md", "TUNING.md"):
            shutil.copy2(ROOT / "docs" / doc, skill / "references" / doc)
        shutil.copytree(ROOT / "examples", skill / "examples")
        # profiles as one archive: the skill stays small in file count and works offline
        prof_stage = work / "profiles"
        prof_stage.mkdir()
        shutil.copy2(prof / "BBL.json", prof_stage / "BBL.json")
        shutil.copytree(prof / "BBL", prof_stage / "BBL")
        for extra in ("schema.json", "meta.json"):
            if (prof / extra).is_file():
                shutil.copy2(prof / extra, prof_stage / extra)
        if not (prof_stage / "meta.json").is_file() and lib.app_version:
            (prof_stage / "meta.json").write_text(json.dumps({"app_version": lib.app_version,
                                                              "profile_version": lib.profile_version}))
        zip_dir(prof_stage, skill / "profiles.zip")
        (skill / "LICENSE-NOTICE.txt").write_text(
            "profiles.zip contains Bambu Lab system profiles from Bambu Studio "
            "(https://github.com/bambulab/BambuStudio), licensed AGPL-3.0.\n", encoding="utf-8")
        out = out_dir / f"{SKILL_NAME}.zip"
        zip_dir(work, out)   # zip root contains the bambu-print/ folder
        files = sum(1 for f in skill.rglob("*") if f.is_file())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"skill_zip": str(out), "size_kb": round(out.stat().st_size / 1024),
                      "files": files, "profiles": info}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
