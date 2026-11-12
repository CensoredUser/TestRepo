"""In-memory Bambu Studio project: objects, parts, instances and plates.

Coordinates follow Bambu Studio: millimetres, Z up, plate 1 occupies the
printer's ``printable_area``; further plates are laid out on a grid with a
gap of 1/5 of the bed size (PartPlate.cpp ``LOGICAL_PART_PLATE_GAP``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .mesh import Matrix, Mesh, identity, matmul, rotation, scaling, translation

PART_TYPES = ("normal_part", "modifier_part", "negative_part", "support_enforcer", "support_blocker")
PLATE_GAP = 1.0 / 5.0


@dataclass
class Part:
    mesh: Mesh
    name: str = ""
    subtype: str = "normal_part"
    # placement of this part relative to the object's main geometry (applied before the object transform)
    transform: Matrix = field(default_factory=identity)
    settings: Dict[str, str] = field(default_factory=dict)   # per-part overrides (Bambu Studio strings)
    extruder: Optional[int] = None                           # 1-based filament for this part

    def __post_init__(self):
        if self.subtype not in PART_TYPES:
            raise ValueError(f"part type must be one of {PART_TYPES}, got '{self.subtype}'")


@dataclass
class LayerRange:
    min_z: float
    max_z: float
    settings: Dict[str, str] = field(default_factory=dict)


@dataclass
class Instance:
    x: float = 0.0          # position of the object's footprint centre on its plate
    y: float = 0.0
    printable: bool = True
    placed: bool = False    # True once x/y are final (user-given or arranged)
    plate: Optional[int] = None   # 0-based plate; None = the object's plate


@dataclass
class ModelObject:
    name: str
    parts: List[Part]
    # object-level rotation/scale (applied to all parts, about the object origin)
    transform: Matrix = field(default_factory=identity)
    settings: Dict[str, str] = field(default_factory=dict)  # per-object overrides
    extruder: int = 1
    instances: List[Instance] = field(default_factory=lambda: [Instance()])
    layer_ranges: List[LayerRange] = field(default_factory=list)
    plate: int = 0          # 0-based plate index

    def object_space_parts(self) -> List[Tuple[Part, Mesh]]:
        """Each part's mesh transformed into object space."""
        out = []
        for p in self.parts:
            out.append((p, p.mesh.transformed(matmul(self.transform, p.transform))))
        return out

    def footprint(self) -> Tuple[float, float, float]:
        """(size_x, size_y, size_z) of the printable geometry."""
        lo, hi = self.bounds()
        return hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]

    def bounds(self):
        meshes = [m for p, m in self.object_space_parts() if p.subtype == "normal_part"] or \
                 [m for _, m in self.object_space_parts()]
        los, his = zip(*(m.bbox() for m in meshes))
        lo = tuple(min(v[i] for v in los) for i in range(3))
        hi = tuple(max(v[i] for v in his) for i in range(3))
        return lo, hi


@dataclass
class Plate:
    name: str = ""
    locked: bool = False
    # optional per-plate overrides written to model_settings.config
    bed_type: Optional[str] = None          # e.g. "Textured PEI Plate"
    print_sequence: Optional[str] = None    # "by layer" | "by object"
    spiral_mode: Optional[bool] = None


@dataclass
class Metadata:
    title: str = ""
    designer: str = ""
    description: str = ""
    copyright: str = ""
    license: str = ""
    origin: str = ""


class Bed:
    """Printable area of the selected printer (from printable_area/printable_height)."""

    def __init__(self, printable_area: Sequence[str] = ("0x0", "256x0", "256x256", "0x256"),
                 height: float = 250.0, exclude: Sequence[str] = ()):
        pts = [tuple(float(c) for c in p.split("x")) for p in printable_area]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.min_x, self.max_x, self.min_y, self.max_y = min(xs), max(xs), min(ys), max(ys)
        self.height = float(height)
        self.exclude = []
        if exclude:
            ex = [tuple(float(c) for c in p.split("x")) for p in exclude]
            self.exclude.append((min(p[0] for p in ex), min(p[1] for p in ex),
                                 max(p[0] for p in ex), max(p[1] for p in ex)))

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Bed":
        area = cfg.get("printable_area") or ["0x0", "256x0", "256x256", "0x256"]
        h = cfg.get("printable_height", "250")
        ex = cfg.get("bed_exclude_area") or []
        return cls(area, float(h if not isinstance(h, list) else h[0]), ex)

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def depth(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Tuple[float, float]:
        return (self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2

    def plate_origin(self, index: int, plate_count: int) -> Tuple[float, float]:
        """World offset of plate *index* (PartPlateList::compute_shape_position)."""
        cols = plate_columns(plate_count)
        row, col = divmod(index, cols)
        return col * self.width * (1 + PLATE_GAP), -row * self.depth * (1 + PLATE_GAP)


def plate_columns(count: int) -> int:
    value = math.sqrt(count)
    r = round(value)
    return int(r + 1 if value > r else r)


class Project:
    def __init__(self, config: Dict[str, Any], bed: Optional[Bed] = None):
        self.config = config                      # flat project_settings dict
        self.bed = bed or Bed.from_config(config)
        self.objects: List[ModelObject] = []
        self.plates: List[Plate] = [Plate()]
        self.metadata = Metadata()
        self.extra_files: Dict[str, bytes] = {}  # passthrough files when editing an existing 3MF
        self.app_version: str = str(config.get("version", "02.00.00.00"))
        self.warnings: List[str] = []

    @property
    def filament_count(self) -> int:
        cols = self.config.get("filament_colour") or self.config.get("filament_settings_id") or ["#FFFFFF"]
        return len(cols)

    def add_object(self, obj: ModelObject) -> ModelObject:
        self.objects.append(obj)
        top = max([obj.plate] + [i.plate for i in obj.instances if i.plate is not None])
        while top >= len(self.plates):
            self.plates.append(Plate())
        return obj

    # -- arrangement ------------------------------------------------------------
    def plate_of(self, obj: ModelObject, inst: Instance) -> int:
        return obj.plate if inst.plate is None else inst.plate

    def arrange(self, spacing: float = 6.0, allow_new_plates: bool = True) -> None:
        """Place every instance that has no explicit position.

        Simple shelf packing per plate, centred on the bed. Instances that do
        not fit spill onto new plates when *allow_new_plates* is set.
        """
        pending: Dict[int, List[Tuple[ModelObject, Instance]]] = {}
        for obj in self.objects:
            for inst in obj.instances:
                if not inst.placed:
                    pending.setdefault(self.plate_of(obj, inst), []).append((obj, inst))
        queue = sorted(pending)
        while queue:
            p = queue.pop(0)
            items = sorted(pending.pop(p), key=lambda it: -it[0].footprint()[1])
            overflow = self._pack(items, spacing)
            if not overflow:
                continue
            if not allow_new_plates or len(overflow) == len(items):
                names = ", ".join(sorted({o.name for o, _ in overflow}))
                raise ValueError(f"Objects do not fit on plate {p + 1}: {names}")
            newp = len(self.plates)
            self.plates.append(Plate())
            for _, inst in overflow:
                inst.plate = newp
            pending[newp] = overflow
            queue.append(newp)
        for obj in self.objects:
            lo, hi = obj.bounds()
            if hi[2] - lo[2] > self.bed.height + 1e-6:
                self.warnings.append(f"'{obj.name}' is {hi[2] - lo[2]:.1f} mm tall; the printer allows "
                                     f"{self.bed.height:.0f} mm")

    def _pack(self, items, spacing):
        bed = self.bed
        rows: List[List[Tuple[ModelObject, Instance, float, float]]] = []
        row: List = []
        row_w = row_h = 0.0
        total_h = 0.0
        overflow = []
        for obj, inst in items:
            w, d, _ = obj.footprint()
            if w > bed.width or d > bed.depth:
                self.warnings.append(f"'{obj.name}' ({w:.0f}x{d:.0f} mm) is larger than the bed")
            need_w = w if not row else row_w + spacing + w
            if row and need_w > bed.width:
                rows.append(row)
                total_h += row_h + spacing
                row, row_w, row_h = [], 0.0, 0.0
                need_w = w
            if total_h + max(row_h, d) > bed.depth + 1e-9 and (rows or row):
                overflow.append((obj, inst))
                continue
            row.append((obj, inst, w, d))
            row_w, row_h = need_w, max(row_h, d)
        if row:
            rows.append(row)
            total_h += row_h
        else:
            total_h = max(0.0, total_h - spacing)
        cx, cy = bed.center
        y = cy + total_h / 2
        for r in rows:
            rh = max(it[3] for it in r)
            rw = sum(it[2] for it in r) + spacing * (len(r) - 1)
            x = cx - rw / 2
            for obj, inst, w, d in r:
                inst.x, inst.y, inst.placed = x + w / 2, y - rh / 2, True
                x += w + spacing
            y -= rh + spacing
        return overflow


def object_transform(rotate: Sequence[float] = (0, 0, 0), scale: Any = 1.0) -> Matrix:
    if isinstance(scale, (int, float)):
        s = scaling(scale)
    else:
        s = scaling(*scale)
    return matmul(rotation(*rotate), s)


def part_transform(offset: Sequence[float] = (0, 0, 0), rotate: Sequence[float] = (0, 0, 0),
                   scale: Any = 1.0) -> Matrix:
    return matmul(translation(*offset), object_transform(rotate, scale))
