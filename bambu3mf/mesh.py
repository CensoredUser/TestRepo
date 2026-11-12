"""Triangle meshes: loading (STL/OBJ/3MF), primitives and transforms.

Pure Python, no dependencies. Matrices are 4x4 row-major nested lists.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]
Tri = Tuple[int, int, int]
Matrix = List[List[float]]


# ---------------------------------------------------------------------------
# Matrices
# ---------------------------------------------------------------------------

def identity() -> Matrix:
    return [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def matmul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def translation(x: float, y: float, z: float) -> Matrix:
    m = identity()
    m[0][3], m[1][3], m[2][3] = float(x), float(y), float(z)
    return m


def scaling(sx: float, sy: Optional[float] = None, sz: Optional[float] = None) -> Matrix:
    sy = sx if sy is None else sy
    sz = sx if sz is None else sz
    m = identity()
    m[0][0], m[1][1], m[2][2] = float(sx), float(sy), float(sz)
    return m


def rotation(rx: float = 0.0, ry: float = 0.0, rz: float = 0.0) -> Matrix:
    """Rotation in degrees, applied X then Y then Z (R = Rz * Ry * Rx)."""
    ax, ay, az = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    rxm = [[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]]
    rym = [[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]]
    rzm = [[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    m = matmul(rzm, matmul(rym, rxm))
    return [[_clean(v) for v in row] for row in m]


def _clean(v: float) -> float:
    r = round(v)
    return float(r) if abs(v - r) < 1e-12 else v


def apply(m: Matrix, v: Sequence[float]) -> Vec3:
    x, y, z = v
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
            m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
            m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])


def determinant3(m: Matrix) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def parse_3mf_transform(s: str) -> Matrix:
    """3MF/Bambu 'transform' attribute (12 numbers, column-major 3x4) -> 4x4."""
    v = [float(x) for x in s.split()]
    m = identity()
    for c in range(4):
        for r in range(3):
            m[r][c] = v[c * 3 + r]
    return m


def parse_row_major(s: str) -> Matrix:
    v = [float(x) for x in s.split()]
    return [v[r * 4:(r + 1) * 4] for r in range(4)]


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------

class Mesh:
    def __init__(self, vertices: Optional[List[Vec3]] = None, triangles: Optional[List[Tri]] = None,
                 source: str = "", triangle_attrs: Optional[List[dict]] = None):
        self.vertices: List[Vec3] = list(vertices or [])
        self.triangles: List[Tri] = list(triangles or [])
        self.source = source           # file the mesh was loaded from (basename used in 3MF)
        # per-triangle extra 3MF attributes (Bambu painting: paint_color, paint_supports, ...)
        self.triangle_attrs: Optional[List[dict]] = triangle_attrs

    def __repr__(self) -> str:
        return f"Mesh({len(self.vertices)} vertices, {len(self.triangles)} triangles)"

    def copy(self) -> "Mesh":
        return Mesh(list(self.vertices), list(self.triangles), self.source,
                    [dict(a) for a in self.triangle_attrs] if self.triangle_attrs else None)

    # -- geometry -------------------------------------------------------------
    def bbox(self) -> Tuple[Vec3, Vec3]:
        if not self.vertices:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs, ys, zs = zip(*self.vertices)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def size(self) -> Vec3:
        lo, hi = self.bbox()
        return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

    def volume(self) -> float:
        vol = 0.0
        vs = self.vertices
        for a, b, c in self.triangles:
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = vs[a], vs[b], vs[c]
            vol += (x1 * (y2 * z3 - y3 * z2) - x2 * (y1 * z3 - y3 * z1) + x3 * (y1 * z2 - y2 * z1))
        return vol / 6.0

    def transformed(self, m: Matrix) -> "Mesh":
        out = self.copy()
        out.vertices = [apply(m, v) for v in self.vertices]
        if determinant3(m) < 0:   # mirroring flips orientation; keep normals outward
            out.triangles = [(a, c, b) for a, b, c in self.triangles]
        return out

    def translated(self, x: float, y: float, z: float) -> "Mesh":
        return self.transformed(translation(x, y, z))

    def fix_orientation(self) -> "Mesh":
        """Flip all triangles if the mesh is inside-out (negative volume)."""
        if self.volume() < 0:
            self.triangles = [(a, c, b) for a, b, c in self.triangles]
        return self

    def merged(self, other: "Mesh") -> "Mesh":
        off = len(self.vertices)
        out = self.copy()
        out.vertices += other.vertices
        out.triangles += [(a + off, b + off, c + off) for a, b, c in other.triangles]
        out.triangle_attrs = None
        return out

    # -- I/O ------------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "Mesh":
        p = Path(path)
        ext = p.suffix.lower()
        if ext == ".stl":
            return cls.from_stl(p)
        if ext == ".obj":
            return cls.from_obj(p)
        if ext == ".3mf":
            from .reader import read_meshes
            meshes = read_meshes(p)
            if not meshes:
                raise ValueError(f"{p}: no meshes found")
            m = meshes[0]
            for other in meshes[1:]:
                m = m.merged(other)
            m.source = p.name
            return m
        raise ValueError(f"Unsupported mesh format '{ext}' (use .stl, .obj or .3mf)")

    @classmethod
    def from_stl(cls, path) -> "Mesh":
        data = Path(path).read_bytes()
        tris: List[Tuple[Vec3, Vec3, Vec3]] = []
        is_binary = len(data) >= 84 and 84 + 50 * struct.unpack_from("<I", data, 80)[0] == len(data)
        if not is_binary and data[:5].lower() == b"solid":
            text = data.decode("utf-8", errors="replace")
            verts = []
            for line in text.splitlines():
                parts = line.split()
                if parts[:1] == ["vertex"]:
                    verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    if len(verts) == 3:
                        tris.append(tuple(verts))  # type: ignore[arg-type]
                        verts = []
        else:
            n = struct.unpack_from("<I", data, 80)[0]
            for i in range(n):
                vals = struct.unpack_from("<12f", data, 84 + 50 * i)
                tris.append((vals[3:6], vals[6:9], vals[9:12]))
        mesh = cls.from_triangle_soup(tris)
        mesh.source = Path(path).name
        return mesh

    @classmethod
    def from_obj(cls, path) -> "Mesh":
        verts: List[Vec3] = []
        tris: List[Tri] = []
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v":
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f":
                idx = []
                for tok in parts[1:]:
                    i = int(tok.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):   # fan-triangulate polygons
                    tris.append((idx[0], idx[k], idx[k + 1]))
        mesh = cls(verts, tris, Path(path).name)
        return mesh

    @classmethod
    def from_triangle_soup(cls, tris: Iterable[Sequence[Sequence[float]]]) -> "Mesh":
        """Build an indexed mesh, merging coincident vertices (float32 precision)."""
        index = {}
        verts: List[Vec3] = []
        out: List[Tri] = []
        for tri in tris:
            ids = []
            for v in tri:
                key = tuple(struct.unpack("<3f", struct.pack("<3f", *v)))
                i = index.get(key)
                if i is None:
                    i = index[key] = len(verts)
                    verts.append(key)  # type: ignore[arg-type]
                ids.append(i)
            if ids[0] != ids[1] and ids[1] != ids[2] and ids[0] != ids[2]:
                out.append((ids[0], ids[1], ids[2]))
        return cls(verts, out)

    def save_stl(self, path) -> None:
        with open(path, "wb") as f:
            f.write(b"bambu3mf".ljust(80, b" "))
            f.write(struct.pack("<I", len(self.triangles)))
            for a, b, c in self.triangles:
                p1, p2, p3 = self.vertices[a], self.vertices[b], self.vertices[c]
                u = [p2[i] - p1[i] for i in range(3)]
                w = [p3[i] - p1[i] for i in range(3)]
                n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
                ln = math.sqrt(sum(x * x for x in n)) or 1.0
                f.write(struct.pack("<12fH", *(x / ln for x in n), *p1, *p2, *p3, 0))

    # -- primitives -----------------------------------------------------------
    @classmethod
    def box(cls, x: float, y: float, z: float) -> "Mesh":
        """Axis-aligned box with its min corner at the origin."""
        v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
        t = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        return cls([tuple(map(float, p)) for p in v], t, "")

    @classmethod
    def cylinder(cls, radius: float, height: float, segments: int = 64,
                 top_radius: Optional[float] = None) -> "Mesh":
        """Cylinder (or cone/frustum with top_radius) standing on z=0, centred on the origin."""
        rt = radius if top_radius is None else top_radius
        v: List[Vec3] = [(0.0, 0.0, 0.0), (0.0, 0.0, float(height))]
        for i in range(segments):
            a = 2 * math.pi * i / segments
            v.append((radius * math.cos(a), radius * math.sin(a), 0.0))
        for i in range(segments):
            a = 2 * math.pi * i / segments
            v.append((rt * math.cos(a), rt * math.sin(a), float(height)))
        t: List[Tri] = []
        for i in range(segments):
            j = (i + 1) % segments
            b0, b1, t0, t1 = 2 + i, 2 + j, 2 + segments + i, 2 + segments + j
            t.append((0, b1, b0))
            if rt > 0:
                t.append((1, t0, t1))
                t += [(b0, b1, t1), (b0, t1, t0)]
            else:
                t.append((b0, b1, 1))
        if rt == 0:
            v = v[:2 + segments]
        return cls(v, t, "")

    @classmethod
    def sphere(cls, radius: float, segments: int = 48, rings: int = 24) -> "Mesh":
        """UV sphere centred on (0, 0, radius) so it rests on z=0."""
        v: List[Vec3] = [(0.0, 0.0, 0.0)]
        for r in range(1, rings):
            phi = math.pi * r / rings
            z = radius - radius * math.cos(phi)
            rr = radius * math.sin(phi)
            for s in range(segments):
                th = 2 * math.pi * s / segments
                v.append((rr * math.cos(th), rr * math.sin(th), z))
        v.append((0.0, 0.0, 2.0 * radius))
        top = len(v) - 1
        t: List[Tri] = []
        for s in range(segments):
            t.append((0, 1 + (s + 1) % segments, 1 + s))
        for r in range(rings - 2):
            a0 = 1 + r * segments
            b0 = a0 + segments
            for s in range(segments):
                s1 = (s + 1) % segments
                t += [(a0 + s, a0 + s1, b0 + s1), (a0 + s, b0 + s1, b0 + s)]
        last = 1 + (rings - 2) * segments
        for s in range(segments):
            t.append((last + s, last + (s + 1) % segments, top))
        return cls(v, t, "")
