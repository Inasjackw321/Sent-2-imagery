"""Geometry helpers: AOI normalisation, areas, and output grid definition."""

from __future__ import annotations

import math
from typing import Any

from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform_bounds

from .config import DEFAULT_SIZE, MAX_SIZE, MIN_SIZE

WGS84 = CRS.from_epsg(4326)
WEB_MERCATOR = CRS.from_epsg(3857)
EARTH_RADIUS = 6378137.0


def circle_to_polygon(lon: float, lat: float, radius_m: float, steps: int = 96) -> dict:
    """Approximate a geodesic circle as a GeoJSON polygon."""
    coords = []
    lat_r = math.radians(lat)
    d_lat = radius_m / EARTH_RADIUS
    for i in range(steps + 1):
        theta = 2 * math.pi * i / steps
        dy = d_lat * math.cos(theta)
        dx = d_lat * math.sin(theta) / max(math.cos(lat_r), 1e-9)
        coords.append([lon + math.degrees(dx), lat + math.degrees(dy)])
    coords[-1] = coords[0]
    return {"type": "Polygon", "coordinates": [coords]}


def normalise_aoi(aoi: dict[str, Any]) -> dict:
    """Accept a GeoJSON geometry, a {lon,lat,radius} circle or a bbox; return a Polygon."""
    if not aoi:
        raise ValueError("No area of interest supplied")

    if aoi.get("type") == "circle" or {"lon", "lat", "radius"} <= set(aoi):
        return circle_to_polygon(float(aoi["lon"]), float(aoi["lat"]), float(aoi["radius"]))

    if aoi.get("type") == "bbox" or "bbox" in aoi:
        w, s, e, n = [float(v) for v in aoi.get("bbox", aoi.get("coordinates"))]
        return {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        }

    if aoi.get("type") in ("Polygon", "MultiPolygon"):
        return aoi

    if aoi.get("type") == "Feature":
        return normalise_aoi(aoi["geometry"])

    raise ValueError(f"Unsupported area of interest: {aoi.get('type')!r}")


def _rings(geom: dict) -> list[list[list[float]]]:
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    rings: list[list[list[float]]] = []
    for poly in geom["coordinates"]:
        rings.extend(poly)
    return rings


def geometry_bounds(geom: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for ring in _rings(geom):
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def geodesic_area_km2(geom: dict) -> float:
    """Spherical-excess area of a lon/lat polygon, in square kilometres."""
    total = 0.0
    for i, ring in enumerate(_rings(geom)):
        area = 0.0
        n = len(ring)
        for j in range(n):
            lon1, lat1 = ring[j]
            lon2, lat2 = ring[(j + 1) % n]
            area += math.radians(lon2 - lon1) * (
                2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
            )
        area = abs(area * EARTH_RADIUS * EARTH_RADIUS / 2.0)
        # Outer ring adds, holes subtract. Ring order is not guaranteed in the
        # wild, so only treat later rings of a single Polygon as holes.
        total = area if i == 0 else total - area
    return abs(total) / 1e6


class Grid:
    """The output raster grid: Web Mercator, north-up, covering the AOI bounds."""

    def __init__(self, bounds4326: tuple[float, float, float, float], max_dim: int):
        self.bounds4326 = bounds4326
        west, south, east, north = bounds4326
        self.bounds3857 = transform_bounds(WGS84, WEB_MERCATOR, west, south, east, north)
        x0, y0, x1, y1 = self.bounds3857
        span_x, span_y = x1 - x0, y1 - y0

        max_dim = int(max(MIN_SIZE, min(MAX_SIZE, max_dim or DEFAULT_SIZE)))
        if span_x >= span_y:
            self.width = max_dim
            self.height = max(MIN_SIZE // 4, round(max_dim * span_y / span_x))
        else:
            self.height = max_dim
            self.width = max(MIN_SIZE // 4, round(max_dim * span_x / span_y))

        self.transform = Affine(
            span_x / self.width, 0, x0, 0, -span_y / self.height, y1
        )
        self.crs = WEB_MERCATOR
        self.center_lat = (south + north) / 2.0
        self.center_lon = (west + east) / 2.0

    def refined(self, factor: int) -> "Grid":
        """The same ground, sampled `factor` times more finely.

        Multi-frame super-resolution reads every date straight onto this finer
        grid: each one then arrives with its own sub-pixel sampling phase,
        which is the raw material the fusion works from.
        """
        factor = max(1, int(factor))
        if factor == 1:
            return self
        clone = object.__new__(Grid)
        clone.__dict__.update(self.__dict__)
        clone.width = self.width * factor
        clone.height = self.height * factor
        x0, y0, x1, y1 = self.bounds3857
        clone.transform = Affine(
            (x1 - x0) / clone.width, 0, x0, 0, -(y1 - y0) / clone.height, y1
        )
        return clone

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def ground_res_m(self) -> float:
        """True ground metres per pixel, de-scaled from Mercator at the AOI centre."""
        mercator_res = (self.bounds3857[2] - self.bounds3857[0]) / self.width
        return mercator_res * math.cos(math.radians(self.center_lat))

    def as_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "bounds": list(self.bounds4326),
            "bounds3857": list(self.bounds3857),
            "ground_res_m": round(self.ground_res_m, 3),
            "center": [self.center_lon, self.center_lat],
        }
