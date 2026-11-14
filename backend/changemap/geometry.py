"""The two questions the engine asks of a scene's footprint, without shapely.

A Sentinel-1 footprint is a GeoJSON polygon (occasionally a multipolygon
across the antimeridian) in longitude and latitude. The engine needs to know
how much of the area a set of footprints covers -- to pick the orbit that
covers it all -- and whether a footprint touches a window at all, to skip
scenes that cannot contribute. Neither needs exact polygon clipping:

  coverage   sampled on a fine grid of points across the area, which is
             exact to a fraction of a percent at 64 x 64 and is compared
             against 98 %;
  touches    any corner of the window inside the footprint, any vertex of
             the footprint inside the window, or an edge of one crossing an
             edge of the other.
"""

from __future__ import annotations

import numpy as np

SAMPLES = 64


def _rings(geom: dict) -> list[list[np.ndarray]]:
    """Polygons as lists of rings (outer first), each an (n, 2) array."""
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    out = []
    for poly in polys:
        rings = [np.asarray(r, dtype=float)[:, :2] for r in poly if len(r) >= 3]
        if rings:
            out.append(rings)
    return out


def _inside_ring(ring: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Even-odd ray casting, vectorised over the points."""
    inside = np.zeros(x.shape, bool)
    xs, ys = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    for xi, yi, xk, yk in zip(xs, ys, xj, yj):
        crosses = (yi > y) != (yk > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            at = (xk - xi) * (y - yi) / (yk - yi) + xi
        inside ^= crosses & (x < at)
    return inside


def contains(geom: dict, x, y) -> np.ndarray:
    """Which points lie inside the polygon (holes excluded)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    hit = np.zeros(x.shape, bool)
    for rings in _rings(geom):
        inner = _inside_ring(rings[0], x, y)
        for hole in rings[1:]:
            inner &= ~_inside_ring(hole, x, y)
        hit |= inner
    return hit


def coverage(geoms: list[dict], bounds) -> float:
    """The share of the box (west, south, east, north) inside any footprint."""
    west, south, east, north = bounds
    xs = west + (np.arange(SAMPLES) + 0.5) / SAMPLES * (east - west)
    ys = south + (np.arange(SAMPLES) + 0.5) / SAMPLES * (north - south)
    x, y = np.meshgrid(xs, ys)
    hit = np.zeros(x.shape, bool)
    for g in geoms:
        hit |= contains(g, x, y)
    return float(hit.mean())


def _segments_cross(a, b, c, d) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2 = side(c, d, a), side(c, d, b)
    d3, d4 = side(a, b, c), side(a, b, d)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def touches(geom: dict, bounds) -> bool:
    """Whether the footprint and the box (west, south, east, north) overlap."""
    west, south, east, north = bounds
    corners = np.array([[west, south], [east, south], [east, north], [west, north]])
    if contains(geom, corners[:, 0], corners[:, 1]).any():
        return True
    edges = list(zip(corners, np.roll(corners, -1, axis=0)))
    for rings in _rings(geom):
        ring = rings[0]
        if ((ring[:, 0] >= west) & (ring[:, 0] <= east)
                & (ring[:, 1] >= south) & (ring[:, 1] <= north)).any():
            return True
        for p, q in zip(ring, np.roll(ring, -1, axis=0)):
            if any(_segments_cross(p, q, a, b) for a, b in edges):
                return True
    return False
