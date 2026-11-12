"""Colour-based purge ("flush") volume estimate.

Port of the colour-distance fallback in Bambu Studio's
``FlushVolCalculator::calc_flush_vol_rgb`` (src/libslic3r/FlushVolCalc.cpp).
Bambu Studio recomputes the matrix whenever filament colours are edited, so
this only needs to give sensible starting values.
"""

from __future__ import annotations

import colorsys
import math
from typing import List, Sequence

MAX_FLUSH = 900


def parse_colour(c: str):
    c = (c or "#FFFFFF").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    a = int(c[6:8], 16) if len(c) >= 8 else 255
    return r, g, b, a


def _lum(r, g, b):
    return r * 0.3 + g * 0.59 + b * 0.11


def flush_volume(src: str, dst: str, min_flush: int = 0) -> int:
    sr, sg, sb, sa = parse_colour(src)
    dr, dg, db, da = parse_colour(dst)
    if sa == 0:
        sr = sg = sb = 255
    if da == 0:
        dr = dg = db = 255
    s = (sr / 255.0, sg / 255.0, sb / 255.0)
    d = (dr / 255.0, dg / 255.0, db / 255.0)
    h1, s1, v1 = colorsys.rgb_to_hsv(*s)
    h2, s2, v2 = colorsys.rgb_to_hsv(*d)
    h1, h2 = math.radians(h1 * 360), math.radians(h2 * 360)
    dx = math.cos(h1) * s1 * v1 - math.cos(h2) * s2 * v2
    dy = math.sin(h1) * s1 * v1 - math.sin(h2) * s2 * v2
    hs_dist = min(1.2, math.hypot(dx, dy))
    from_l, to_l = _lum(*s), _lum(*d)
    if to_l >= from_l:
        lumi_flush = (to_l - from_l) ** 0.7 * 560.0
    else:
        lumi_flush = (from_l - to_l) * 80.0
        hs_dist = min(0.67 * v2 + 0.33 * v1, hs_dist)
    hs_flush = 230.0 * hs_dist
    vol = math.sqrt(hs_flush ** 2 + lumi_flush ** 2 - 2 * hs_flush * lumi_flush * math.cos(math.radians(120)))
    vol = max(vol, 60.0)
    return min(int(int(vol) + min_flush), MAX_FLUSH)


def flush_matrix(colours: Sequence[str], nozzles: int = 1, min_flush: int = 0) -> List[str]:
    """Row-major n*n matrix (repeated per nozzle) as Bambu Studio stores it."""
    n = len(colours)
    one = []
    for i in range(n):
        for j in range(n):
            one.append("0" if i == j else str(flush_volume(colours[i], colours[j], min_flush)))
    return one * max(1, nozzles)
