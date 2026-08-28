"""FastAPI application: JSON API plus the static front end."""

from __future__ import annotations

import base64
import datetime as dt
import logging
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import composite, config, fires, passes, service, stac, version
from .geo import geodesic_area_km2, geometry_bounds, normalise_aoi
from .raster import BandReadError

log = logging.getLogger("sent2")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="EarthViewer", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _fail(exc: Exception, status: int = 502) -> HTTPException:
    log.warning("request failed: %s", exc)
    return HTTPException(status_code=status, detail=str(exc))


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Everything the front end needs to build itself, in one call."""
    return {
        "demo": config.DEMO_MODE,
        "build": version.described(),
        "stac_url": config.STAC_URL,
        "collection": config.STAC_COLLECTION,
        "satellite": config.SATELLITE,
        "satellites": config.SATELLITES,
        "default_satellite": config.DEFAULT_SATELLITE,
        "composites": {
            k: {"label": v["label"], "bands": v["bands"], "hint": v["hint"],
                "sat": v["sat"],
                "band_labels": [config.BANDS[b]["label"] for b in v["bands"]]}
            for k, v in config.COMPOSITES.items()
        },
        "indices": {
            k: {"label": v["label"], "bands": v["bands"], "formula": v["formula"],
                "range": v["range"], "colormap": v["colormap"], "hint": v["hint"],
                "sat": v["sat"]}
            for k, v in config.INDICES.items()
        },
        "colormaps": {
            name: [composite._hex(composite.colormap_lut(name)[int(p * 255)])
                   for p in (0, .125, .25, .375, .5, .625, .75, .875, 1)]
            for name in config.COLORMAPS
        },
        "bands": config.BANDS,
        "fires": {
            "windows": sorted(fires.WINDOWS),
            "sensors": {k: v["label"] for k, v in fires.SENSORS.items()},
            "attribution": "NASA FIRMS",
            "keyed": bool(fires.MAP_KEY),
        },
        "max_size": config.MAX_SIZE,
        "max_superres": config.MAX_SUPERRES,
        "superres_steps": [list(step) for step in config.SUPERRES_STEPS],
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "demo": config.DEMO_MODE, "build": version.described(),
            "time": dt.datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@app.post("/api/aoi")
def describe_aoi(body: dict = Body(...)) -> dict:
    try:
        geom = normalise_aoi(body.get("aoi") or body.get("geometry"))
    except ValueError as exc:
        raise _fail(exc, 400)
    west, south, east, north = geometry_bounds(geom)
    return {
        "geometry": geom,
        "bounds": [west, south, east, north],
        "area_km2": round(geodesic_area_km2(geom), 4),
        "center": [(west + east) / 2, (south + north) / 2],
    }


@app.post("/api/search")
def search(body: dict = Body(...)) -> dict:
    try:
        geom = normalise_aoi(body.get("aoi") or body.get("geometry"))
        return stac.search_scenes(
            geom,
            start=body.get("start"),
            end=body.get("end"),
            max_cloud=float(body.get("max_cloud", 30)),
            limit=int(body.get("limit", 60)),
            demo=body.get("demo"),
            satellites=body.get("satellites") or body.get("satellite"),
        )
    except ValueError as exc:
        raise _fail(exc, 400)
    except stac.SceneSearchError as exc:
        raise _fail(exc)


@app.get("/api/passes")
def overpasses(
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-85, le=85),
    satellite: str | None = Query(None),
) -> dict:
    """When either Sentinel last flew over this point, and when it next will."""
    try:
        return passes.next_passes(
            lon, lat, satellites=[satellite] if satellite else None)
    except passes.PassLookupError as exc:
        raise _fail(exc)


@app.post("/api/probe")
def probe(body: dict = Body(...)) -> dict:
    """What the satellite measured at one point, in its own units."""
    try:
        return service.probe(body)
    except (KeyError, ValueError, service.RenderError) as exc:
        raise _fail(exc, 400)
    except (BandReadError, stac.SceneSearchError) as exc:
        raise _fail(exc)


@app.get("/api/fires")
def active_fires(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    hours: int = Query(24, ge=1, le=168),
) -> dict:
    """Every NASA FIRMS thermal detection in a rectangle, newest first."""
    try:
        return fires.active_fires((west, south, east, north), hours=hours)
    except fires.FireLookupError as exc:
        raise _fail(exc)


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=2)) -> dict:
    try:
        resp = requests.get(
            config.NOMINATIM_URL,
            params={"q": q, "format": "jsonv2", "limit": 8, "polygon_geojson": 0},
            headers={"User-Agent": config.USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise _fail(exc)
    return {
        "results": [
            {
                "name": r.get("display_name"),
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "bbox": [float(v) for v in r["boundingbox"]] if r.get("boundingbox") else None,
                "type": r.get("type"),
            }
            for r in resp.json()
        ]
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_response(result: dict, download: bool, stem: str):
    if download:
        ext = {"image/tiff": "tif", "image/jpeg": "jpg",
               "image/png": "png"}.get(result["media_type"], "bin")
        return Response(
            content=result["bytes"],
            media_type=result["media_type"],
            headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'},
        )
    return JSONResponse({
        "image": f"data:{result['media_type']};base64,"
                 + base64.b64encode(result["bytes"]).decode(),
        "meta": result["meta"],
    })


@app.post("/api/render")
def render(body: dict = Body(...), download: bool = Query(False)):
    try:
        result = service.render(body)
    except (ValueError, service.RenderError) as exc:
        raise _fail(exc, 400)
    except (BandReadError, stac.SceneSearchError) as exc:
        raise _fail(exc)
    meta = result["meta"]
    stem = (f"{meta.get('satellite', 'sentinel-2').replace('-', '')}"
            f"_{meta['scene'].get('date', 'scene')}"
            f"_{meta.get('index') or meta.get('preset')}")
    return _render_response(result, download, stem)


# ---------------------------------------------------------------------------
# Static front end
# ---------------------------------------------------------------------------

if FRONTEND.exists():
    app.mount("/js", StaticFiles(directory=FRONTEND / "js"), name="js")
    app.mount("/css", StaticFiles(directory=FRONTEND / "css"), name="css")
    app.mount("/vendor", StaticFiles(directory=FRONTEND / "vendor"), name="vendor")
    app.mount("/icons", StaticFiles(directory=FRONTEND / "icons"), name="icons")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(FRONTEND / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        # Must be served from the root for the worker to control the whole app.
        return FileResponse(
            FRONTEND / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(FRONTEND / "icons" / "favicon.ico")
