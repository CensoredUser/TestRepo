"""Bambu Studio system profile library.

Loads the vendor profile tree (``BBL.json`` + ``BBL/{machine,process,filament}``)
from one of:

* an explicit directory (``--profiles DIR`` / ``BAMBU3MF_PROFILES``),
* the cache filled by ``bambu3mf profiles fetch`` (exact Bambu Studio git ref),
* the user's own Bambu Studio installation (``<data dir>/system``), which is
  guaranteed to match the installed Bambu Studio version.

and flattens presets the way ``PresetBundle::load_vendor_configs_from_json``
does: the parent (``inherits``) is resolved first, then every ``include``
template is applied, then the preset's own keys.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .schema import Schema, extract_schema

VENDOR = "BBL"
REPO_URL = "https://github.com/bambulab/BambuStudio.git"
RAW_URL = "https://raw.githubusercontent.com/bambulab/BambuStudio/{ref}/{path}"

# Keys stored in profile JSON that are preset metadata, not config options.
META_KEYS = {
    "type", "name", "inherits", "from", "setting_id", "filament_id",
    "instantiation", "include", "description", "url", "is_custom_defined",
    "version", "renamed_from", "base_id", "user_id", "updated_time",
}

PRESET_TYPES = ("machine", "process", "filament")

# Short printer names people use -> printer_model (Bambu Studio name).
PRINTER_ALIASES = {
    "x1c": "Bambu Lab X1 Carbon",
    "x1carbon": "Bambu Lab X1 Carbon",
    "x1e": "Bambu Lab X1E",
    "x1": "Bambu Lab X1",
}


class ProfileError(Exception):
    pass


@dataclass
class PresetInfo:
    name: str
    type: str                  # machine | process | filament | machine_model
    path: Path
    source: str = "system"     # system | user
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def instantiated(self) -> bool:
        return str(self.raw.get("instantiation", "true")).lower() == "true"


def cache_dir() -> Path:
    base = os.environ.get("BAMBU3MF_CACHE")
    if base:
        return Path(base)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "bambu3mf"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bambu3mf"


def bambu_data_dirs() -> List[Path]:
    """Candidate Bambu Studio data directories for this OS."""
    home = Path.home()
    cands = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            cands.append(Path(appdata) / "BambuStudio")
    elif sys.platform == "darwin":
        cands.append(home / "Library/Application Support/BambuStudio")
    else:
        cands.append(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "BambuStudio")
        cands.append(home / ".var/app/com.bambulab.BambuStudio/config/BambuStudio")  # flatpak
    return [c for c in cands if c.is_dir()]


def installed_app_version(data_dir: Path) -> Optional[str]:
    conf = data_dir / "BambuStudio.conf"
    if not conf.is_file():
        return None
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
        # The file is JSON with an optional trailing checksum comment line.
        text = text[: text.rfind("}") + 1]
        data = json.loads(text)
        ver = data.get("app", {}).get("version") or data.get("version")
        return normalize_version(ver) if ver else None
    except (ValueError, AttributeError):
        m = re.search(r'"version"\s*:\s*"([0-9.]+)"', conf.read_text(errors="replace"))
        return normalize_version(m.group(1)) if m else None


def normalize_version(ver: str) -> str:
    """'2.3.0.70' / 'v02.03.00.70' -> '02.03.00.70'."""
    parts = re.findall(r"\d+", str(ver))[:4]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(p.zfill(2) for p in parts)


# ---------------------------------------------------------------------------
# Locating / fetching profile sets
# ---------------------------------------------------------------------------

def find_profile_root(explicit: Optional[str] = None) -> Path:
    """Return a directory containing BBL.json and BBL/."""
    cands: List[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    env = os.environ.get("BAMBU3MF_PROFILES")
    if env:
        cands.append(Path(env).expanduser())
    if not explicit and not env:
        # Prefer the installed Bambu Studio (matches the user's version), then the cache.
        cands += [d / "system" for d in bambu_data_dirs()]
        cdir = cache_dir() / "profiles"
        if cdir.is_dir():
            subs = sorted((p for p in cdir.iterdir() if (p / f"{VENDOR}.json").is_file()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            cands += subs
    for c in cands:
        if (c / f"{VENDOR}.json").is_file() and (c / VENDOR).is_dir():
            return c
        # allow pointing at a BambuStudio checkout or resources dir
        for sub in ("resources/profiles", "profiles"):
            if (c / sub / f"{VENDOR}.json").is_file():
                return c / sub
    raise ProfileError(
        "No Bambu Studio profiles found. Run `bambu3mf profiles fetch` (downloads the "
        "official BBL profiles), install Bambu Studio, or pass --profiles DIR.")


def fetch_profiles(ref: str = "master", dest: Optional[Path] = None, log=print) -> Path:
    """Download BBL profiles + schema sources for a Bambu Studio git ref.

    Uses a sparse, blob-filtered git clone (fast, a few MB); falls back to
    raw.githubusercontent.com downloads when git is unavailable.
    """
    dest = Path(dest) if dest else cache_dir() / "profiles" / re.sub(r"[^\w.-]", "_", ref)
    tmp = Path(tempfile.mkdtemp(prefix="bambu3mf-fetch-"))
    sparse = ["/version.inc", f"/resources/profiles/{VENDOR}.json", f"/resources/profiles/{VENDOR}/",
              "/src/libslic3r/PrintConfig.cpp", "/src/libslic3r/Preset.cpp"]
    try:
        if shutil.which("git"):
            log(f"Cloning Bambu Studio profiles at {ref} (sparse git clone)...")
            repo = tmp / "repo"
            run = lambda *a: subprocess.run(a, check=True, capture_output=True, text=True)
            try:
                run("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                    "--branch", ref, REPO_URL, str(repo))
            except subprocess.CalledProcessError:
                # ref may be a commit sha
                run("git", "clone", "--filter=blob:none", "--sparse", "--no-checkout", REPO_URL, str(repo))
                run("git", "-C", str(repo), "checkout", ref)
            run("git", "-C", str(repo), "sparse-checkout", "set", "--no-cone", *sparse)
            src = repo
        else:
            log(f"git not found; downloading files for {ref} from raw.githubusercontent.com ...")
            src = tmp / "raw"
            _raw_download(ref, src, log)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        shutil.copy2(src / "resources/profiles" / f"{VENDOR}.json", dest / f"{VENDOR}.json")
        shutil.copytree(src / "resources/profiles" / VENDOR, dest / VENDOR)
        app_version = None
        vinc = src / "version.inc"
        if vinc.is_file():
            m = re.search(r'SLIC3R_VERSION\s+"([0-9.]+)"', vinc.read_text())
            app_version = m.group(1) if m else None
        pc, pr = src / "src/libslic3r/PrintConfig.cpp", src / "src/libslic3r/Preset.cpp"
        if pc.is_file() and pr.is_file():
            extract_schema(pc.read_text(encoding="utf-8", errors="replace"),
                           pr.read_text(encoding="utf-8", errors="replace")).save(dest / "schema.json")
        vendor_ver = json.loads((dest / f"{VENDOR}.json").read_text(encoding="utf-8")).get("version")
        (dest / "meta.json").write_text(json.dumps(
            {"ref": ref, "app_version": app_version, "profile_version": vendor_ver}, indent=2))
        log(f"Profiles {vendor_ver} (Bambu Studio {app_version or ref}) saved to {dest}")
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _raw_download(ref: str, dest: Path, log) -> None:
    def get(path: str) -> bytes:
        with urllib.request.urlopen(RAW_URL.format(ref=ref, path=path), timeout=60) as r:
            return r.read()

    def save(path: str) -> None:
        out = dest / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(get(path))

    for p in ("version.inc", "src/libslic3r/PrintConfig.cpp", "src/libslic3r/Preset.cpp",
              f"resources/profiles/{VENDOR}.json"):
        save(p)
    index = json.loads((dest / f"resources/profiles/{VENDOR}.json").read_text(encoding="utf-8"))
    paths = sorted({f"resources/profiles/{VENDOR}/{e['sub_path']}"
                    for lst in ("machine_model_list", "machine_list", "process_list", "filament_list")
                    for e in index.get(lst, [])})
    log(f"  {len(paths)} profile files...")
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(save, paths))


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class ProfileLibrary:
    """Index of system (and optional user) presets with flattening."""

    def __init__(self, root: Path, user_dirs: Iterable[Path] = ()):
        self.root = Path(root)
        index_path = self.root / f"{VENDOR}.json"
        self.index = json.loads(index_path.read_text(encoding="utf-8"))
        self.profile_version = self.index.get("version", "")
        meta_path = self.root / "meta.json"
        self.meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        schema_path = self.root / "schema.json"
        self.schema = Schema.load(schema_path) if schema_path.is_file() else Schema.empty()
        self.presets: Dict[str, PresetInfo] = {}
        lists = {"machine_model_list": "machine_model", "machine_list": "machine",
                 "process_list": "process", "filament_list": "filament"}
        for lst, typ in lists.items():
            for entry in self.index.get(lst, []):
                path = self.root / VENDOR / entry["sub_path"]
                self.presets[entry["name"]] = PresetInfo(entry["name"], typ, path)
        for d in user_dirs:
            self.add_user_presets(d)
        self._resolved: Dict[str, dict] = {}

    # -- construction helpers -------------------------------------------
    @classmethod
    def open(cls, explicit: Optional[str] = None, with_user_presets: bool = True) -> "ProfileLibrary":
        root = find_profile_root(explicit)
        user_dirs = []
        if with_user_presets:
            for data in bambu_data_dirs():
                user = data / "user"
                if user.is_dir():
                    user_dirs += [p for p in user.iterdir() if p.is_dir()]
        return cls(root, user_dirs)

    def add_user_presets(self, directory: Path) -> int:
        """Add user presets from a Bambu Studio ``user/<id>`` folder or a flat folder of JSON files."""
        directory = Path(directory)
        count = 0
        files: List[Tuple[Path, Optional[str]]] = []
        for typ in PRESET_TYPES:
            sub = directory / typ
            if sub.is_dir():
                files += [(f, typ) for f in sub.glob("*.json")]
        if not files:
            files = [(f, None) for f in directory.glob("*.json")]
        for f, typ in files:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            name = raw.get("name") or f.stem
            typ = typ or raw.get("type") or _guess_type(raw)
            if typ not in PRESET_TYPES:
                continue
            self.presets[name] = PresetInfo(name, typ, f, source="user", raw=raw)
            count += 1
        self._resolved = {}
        return count

    def add_preset_file(self, path: Path) -> PresetInfo:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        typ = raw.get("type") or _guess_type(raw)
        info = PresetInfo(raw["name"], typ, Path(path), source="user", raw=raw)
        self.presets[info.name] = info
        self._resolved = {}
        return info

    # -- raw access -------------------------------------------------------
    @property
    def app_version(self) -> Optional[str]:
        """Bambu Studio version these profiles belong to, if known."""
        if self.meta.get("app_version"):
            return normalize_version(self.meta["app_version"])
        # installed system dir: <data>/system -> <data>/BambuStudio.conf
        return installed_app_version(self.root.parent)

    def info(self, name: str) -> PresetInfo:
        try:
            info = self.presets[name]
        except KeyError:
            raise ProfileError(f"Unknown preset '{name}'") from None
        if not info.raw:
            info.raw = json.loads(info.path.read_text(encoding="utf-8"))
        return info

    def raw(self, name: str) -> dict:
        return self.info(name).raw

    # -- flattening -------------------------------------------------------
    def resolve(self, name: str) -> dict:
        """Fully flattened config (option keys only) of a preset."""
        if name not in self._resolved:
            self._resolved[name] = self._resolve(name, set())
        return dict(self._resolved[name])

    def _resolve(self, name: str, stack: set) -> dict:
        if name in stack:
            raise ProfileError(f"Inheritance loop at '{name}'")
        stack = stack | {name}
        raw = self.raw(name)
        parent = raw.get("inherits")
        cfg = self._resolve(parent, stack) if parent else {}
        includes = raw.get("include") or []
        if isinstance(includes, str):
            includes = [includes]
        for inc in includes:
            if inc in self.presets:
                inc_cfg = self._resolve(inc, stack)
                cfg.update(inc_cfg)
        for k, v in raw.items():
            if k in META_KEYS:
                continue
            cfg[k] = v
        return cfg

    def meta_value(self, name: str, key: str) -> Optional[str]:
        """Metadata (setting_id, filament_id, ...) walking up the inheritance chain."""
        seen = set()
        while name and name not in seen:
            seen.add(name)
            raw = self.raw(name)
            if raw.get(key):
                return raw[key]
            name = raw.get("inherits")
        return None

    def system_ancestor(self, name: str) -> Optional[str]:
        """Nearest instantiated *system* preset in the chain (the preset Bambu Studio compares against)."""
        seen = set()
        while name and name not in seen:
            seen.add(name)
            info = self.info(name)
            if info.source == "system" and info.instantiated:
                return name
            name = info.raw.get("inherits")
        return None

    # -- listing / lookup ----------------------------------------------------
    def names(self, typ: str, instantiated_only: bool = True) -> List[str]:
        out = []
        for name, info in self.presets.items():
            if info.type != typ:
                continue
            if instantiated_only and info.source == "system" and not self.info(name).instantiated:
                continue
            out.append(name)
        return sorted(out)

    def printer_models(self) -> List[dict]:
        models = []
        for name in self.names("machine_model", instantiated_only=False):
            raw = self.raw(name)
            nozzles = [n for n in str(raw.get("nozzle_diameter", "")).split(";") if n]
            models.append({"name": name, "nozzles": nozzles, "family": raw.get("family", "")})
        return models

    def resolve_printer_model(self, query: str) -> str:
        models = [m["name"] for m in self.printer_models()]
        if query in models:
            return query
        norm = _norm(query)
        if norm in PRINTER_ALIASES and PRINTER_ALIASES[norm] in models:
            return PRINTER_ALIASES[norm]
        for m in models:
            if _norm(m) == norm or _norm(m.replace("Bambu Lab", "")) == norm:
                return m
        raise ProfileError(f"Unknown printer '{query}'. Known: " + ", ".join(models))

    def machine_preset(self, printer: str, nozzle: float | str = 0.4) -> str:
        """Name of the system machine preset for a printer + nozzle size."""
        if printer in self.presets and self.info(printer).type == "machine":
            return printer
        model = self.resolve_printer_model(printer)
        nozzle_s = _fmt_nozzle(nozzle)
        cands = []
        for name in self.names("machine"):
            if self.info(name).source != "system":
                continue
            flat = self.resolve(name)
            if flat.get("printer_model") == model:
                cands.append((name, str(flat.get("printer_variant", ""))))
        for name, variant in cands:
            if variant == nozzle_s:
                return name
        raise ProfileError(f"No machine preset for {model} with {nozzle_s} mm nozzle. "
                           f"Available: {', '.join(n for n, _ in cands)}")

    def compatible(self, preset: str, machine: str) -> bool:
        flat = self.resolve(preset)
        comp = flat.get("compatible_printers") or []
        if comp:
            return machine in comp
        cond = flat.get("compatible_printers_condition", "")
        if not cond:
            return True
        return _eval_condition(cond, self.resolve(machine))

    def process_preset(self, machine: str, query: Optional[str] = None) -> str:
        if not query:
            default = self.resolve(machine).get("default_print_profile")
            if default and default in self.presets:
                return default
            query = "0.20mm Standard"
        return self._lookup("process", machine, query)

    def filament_preset(self, machine: str, query: Optional[str] = None) -> str:
        if not query:
            defaults = self.resolve(machine).get("default_filament_profile") or []
            for d in defaults if isinstance(defaults, list) else [defaults]:
                if d in self.presets:
                    return d
            query = "Bambu PLA Basic"
        return self._lookup("filament", machine, query)

    def _lookup(self, typ: str, machine: str, query: str) -> str:
        if query in self.presets and self.info(query).type == typ and self.compatible(query, machine):
            return query
        cands = [n for n in self.names(typ) if self.compatible(n, machine)]
        q = query.strip().lower()
        if query in self.presets and " @" in query:
            # an exact name for another printer, e.g. "Bambu PLA Basic @BBL X1C" on an A1
            q = query.split(" @")[0].strip().lower()
        # e.g. "0.16" -> "0.16mm ..."; "Bambu PLA Basic" -> "Bambu PLA Basic @BBL X1C"
        exact_base = [n for n in cands if n.split(" @")[0].lower() == q]
        if exact_base:
            return sorted(exact_base, key=len)[0]
        if typ == "filament" and " " not in q:
            # a bare material ("PETG", "ABS"): prefer Bambu's own, then generic
            for pattern in (f"bambu {q} basic", f"bambu {q}", f"bambu {q} hf", f"generic {q}"):
                hit = [n for n in cands if n.split(" @")[0].lower() == pattern]
                if hit:
                    return sorted(hit, key=len)[0]
        starts = [n for n in cands if n.lower().startswith(q)]
        if typ == "process" and re.fullmatch(r"\d+(\.\d+)?(mm)?", q):
            pref = [n for n in starts if "Standard" in n] or starts
            starts = pref
        if starts:
            return sorted(starts, key=len)[0]
        contains = [n for n in cands if q in n.lower()]
        if len(contains) == 1:
            return contains[0]
        hint = contains or cands
        raise ProfileError(f"No {typ} preset matching '{query}' for {machine}. Candidates: "
                           + ", ".join(sorted(hint)[:40]) + (" ..." if len(hint) > 40 else ""))


def _guess_type(raw: dict) -> str:
    if "printer_settings_id" in raw or "printer_model" in raw:
        return "machine"
    if "print_settings_id" in raw or "layer_height" in raw:
        return "process"
    if "filament_settings_id" in raw or "filament_type" in raw:
        return "filament"
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("bambu lab", "").replace("bambulab", ""))


def _fmt_nozzle(n) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    return ("%g" % f) if f != int(f) else "%.1f" % f


def _eval_condition(cond: str, machine_cfg: dict) -> bool:
    """Evaluate the simple ``key == "value"`` / ``key != "value"`` conditions
    (joined with and/or) that vendor profiles use. Anything more complex is
    treated as compatible rather than evaluated."""
    def val(key):
        v = machine_cfg.get(key, "")
        return str(v[0] if isinstance(v, list) and v else v)

    def atom(text):
        m = re.fullmatch(r'\s*!?\s*([A-Za-z_]\w*)(?:\[\d+\])?\s*(==|!=|=~)\s*["/]?(.*?)["/]?\s*', text)
        if not m:
            raise ValueError(text)
        key, op, rhs = m.groups()
        if op == "=~":
            return re.search(rhs.strip(".*"), val(key)) is not None
        return (val(key) == rhs) == (op == "==")

    try:
        return any(all(atom(a) for a in re.split(r"\band\b", part))
                   for part in re.split(r"\bor\b", cond.replace("(", "").replace(")", "")))
    except (ValueError, re.error):
        return True
