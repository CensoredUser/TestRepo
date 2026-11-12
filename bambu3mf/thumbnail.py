"""Tiny software renderer for plate thumbnails (PNG, RGBA, transparent background).

Bambu Studio re-renders thumbnails when it saves; these only need to look
right in the file browser, the printer screen and Bambu Handy.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import List, Sequence, Tuple

Tri3 = Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _hex(c: str) -> Tuple[int, int, int]:
    c = (c or "#C0C0C0").lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return 192, 192, 192


def render(triangles: Sequence[Tuple[Tri3, str]], size: int = 256, max_tris: int = 150_000) -> bytes:
    """Render coloured triangles from an isometric-ish view; returns PNG bytes."""
    if len(triangles) > max_tris:
        step = math.ceil(len(triangles) / max_tris)
        triangles = triangles[::step]
    # view: rotate 45deg about Z, then tilt 35deg about X
    az, el = math.radians(-45), math.radians(55)
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)

    def view(p):
        x, y, z = p
        x1, y1 = x * ca - y * sa, x * sa + y * ca
        return x1, y1 * ce + z * se, -y1 * se + z * ce   # screen x, screen y, depth (towards viewer)

    proj = []
    for tri, col in triangles:
        proj.append(([view(p) for p in tri], tri, col))
    if not proj:
        return encode_png(size, size, bytes(size * size * 4))
    xs = [p[0] for t, _, _ in proj for p in t]
    ys = [p[1] for t, _, _ in proj for p in t]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1e-6)
    margin = 0.06 * size
    scale = (size - 2 * margin) / span
    ox = (size - (maxx - minx) * scale) / 2
    oy = (size - (maxy - miny) * scale) / 2
    zbuf = [-1e30] * (size * size)
    img = bytearray(size * size * 4)
    light = (0.35, -0.45, 0.82)
    ln = math.sqrt(sum(c * c for c in light))
    light = tuple(c / ln for c in light)
    for pts, world, col in proj:
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = [((p[0] - minx) * scale + ox, size - ((p[1] - miny) * scale + oy), p[2])
                                                     for p in pts]
        # shading from the world-space normal
        a, b, c = world
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        nl = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
        shade = 0.45 + 0.55 * abs((n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / nl)
        r, g, bb = _hex(col)
        pr, pg, pb = int(r * shade), int(g * shade), int(bb * shade)
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-12:
            continue
        bx0, bx1 = max(int(min(x0, x1, x2)), 0), min(int(max(x0, x1, x2)) + 1, size - 1)
        by0, by1 = max(int(min(y0, y1, y2)), 0), min(int(max(y0, y1, y2)) + 1, size - 1)
        inv = 1.0 / area
        for py in range(by0, by1 + 1):
            cy = py + 0.5
            for px in range(bx0, bx1 + 1):
                cx = px + 0.5
                w0 = ((x1 - cx) * (y2 - cy) - (x2 - cx) * (y1 - cy)) * inv
                w1 = ((x2 - cx) * (y0 - cy) - (x0 - cx) * (y2 - cy)) * inv
                w2 = 1.0 - w0 - w1
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                z = w0 * z0 + w1 * z1 + w2 * z2
                i = py * size + px
                if z > zbuf[i]:
                    zbuf[i] = z
                    j = i * 4
                    img[j], img[j + 1], img[j + 2], img[j + 3] = pr, pg, pb, 255
    return encode_png(size, size, bytes(img))
