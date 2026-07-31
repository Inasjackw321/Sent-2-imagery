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

from . import animate, composite, config, service, sources, stac
from .geo import geodesic_area_km2, geometry_bounds, normalise_aoi
from .raster import BandReadError

log = logging.getLogger("sent2")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Sent-2 Imagery", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _fail(exc: Exception, status: int = 502) -> HTTPException:
    log.warning("request failed: %s", exc)
    return HTTPException(status_code=status, detail=str(exc))


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _source_payload(source) -> dict:
    """What a satellite can do, so the UI only offers what it really supports."""
    composites = {k: v for k, v in config.COMPOSITES.items()
                  if sources.supports(source, _composite_needs(v))}
    indices = {k: v for k, v in config.INDICES.items()
               if sources.supports(source, v["bands"])}
    return {
        "key": source.key,
        "label": source.label,
        "platform": source.platform,
        "kind": source.kind,
        "resolution": source.resolution,
        "since": source.since,
        "revisit": source.revisit,
        "swath_hint": source.swath_hint,
        "notes": source.notes,
        "attribution": source.attribution,
        "provider": sources.PROVIDERS[source.provider]["label"],
        "has_cloud": bool(source.cloud_property),
        "has_cloud_mask": bool(source.cloud_mask),
        "pan": source.pan,
        "pan_resolution": source.pan_resolution,
        "bands": sorted(source.bands),
        "composites": sorted(composites),
        "indices": sorted(indices),
        "default_composite": (source.default_composite
                              if source.default_composite in composites
                              else (sorted(composites)[0] if composites else None)),
        "default_size": source.default_size_hint,
        "categorical": source.kind == "landcover",
    }


def _composite_needs(spec: dict) -> list[str]:
    wanted = []
    for alias in spec["bands"]:
        wanted.extend(config.DERIVED_BANDS.get(alias, (alias,)))
    return list(dict.fromkeys(wanted))


@app.get("/api/sources")
def get_sources() -> dict[str, Any]:
    return {
        "default": sources.DEFAULT_SOURCE,
        "sources": [_source_payload(s) for s in sources.SOURCES.values()],
        "providers": sources.PROVIDERS,
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "demo": config.DEMO_MODE,
        "stac_url": config.STAC_URL,
        "collection": config.STAC_COLLECTION,
        "default_source": sources.DEFAULT_SOURCE,
        "sources": [_source_payload(s) for s in sources.SOURCES.values()],
        "composites": {
            k: {"label": v["label"], "bands": v["bands"], "hint": v["hint"],
                "band_labels": [config.BANDS[b]["label"] for b in v["bands"]]}
            for k, v in config.COMPOSITES.items()
        },
        "indices": {
            k: {"label": v["label"], "bands": v["bands"], "formula": v["formula"],
                "range": v["range"], "colormap": v["colormap"], "hint": v["hint"]}
            for k, v in config.INDICES.items()
        },
        "colormaps": {
            name: [composite._hex(composite.colormap_lut(name)[int(p * 255)])
                   for p in (0, .125, .25, .375, .5, .625, .75, .875, 1)]
            for name in config.COLORMAPS
        },
        "bands": config.BANDS,
        "max_size": config.MAX_SIZE,
        "max_frames": animate.MAX_FRAMES,
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "demo": config.DEMO_MODE, "time": dt.datetime.utcnow().isoformat()}


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
            source_key=body.get("source"),
            start=body.get("start"),
            end=body.get("end"),
            max_cloud=float(body.get("max_cloud", 30)),
            limit=int(body.get("limit", 60)),
            demo=body.get("demo"),
        )
    except ValueError as exc:
        raise _fail(exc, 400)
    except stac.SceneSearchError as exc:
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
    stem = f"sentinel2_{meta['scene'].get('date', 'scene')}_{meta.get('index') or meta.get('preset')}"
    return _render_response(result, download, stem)


@app.post("/api/change")
def change(body: dict = Body(...), download: bool = Query(False)):
    try:
        result = service.change_detection(body)
    except (ValueError, service.RenderError) as exc:
        raise _fail(exc, 400)
    except (BandReadError, stac.SceneSearchError) as exc:
        raise _fail(exc)
    meta = result["meta"]
    stem = f"change_{meta['index']}_{meta['scene_a']['date']}_to_{meta['scene_b']['date']}"
    return _render_response(result, download, stem)


# ---------------------------------------------------------------------------
# Animation / export
# ---------------------------------------------------------------------------


@app.post("/api/animate")
def build_animation(body: dict = Body(...)):
    try:
        data, media, ext = animate.build(
            frames=body.get("frames") or [],
            fps=float(body.get("fps", 2)),
            loop=int(body.get("loop", 0)),
            boomerang=bool(body.get("boomerang")),
            fmt=str(body.get("format", "gif")),
            hold_last_ms=int(body.get("hold_last_ms", 0)),
            colors=int(body.get("colors", 256)),
        )
    except animate.AnimationError as exc:
        raise _fail(exc, 400)
    name = body.get("filename") or f"timelapse_{dt.date.today().isoformat()}"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'},
    )


@app.post("/api/contact-sheet")
def build_contact_sheet(body: dict = Body(...)):
    try:
        data = animate.contact_sheet(
            body.get("frames") or [], columns=int(body.get("columns", 3))
        )
    except animate.AnimationError as exc:
        raise _fail(exc, 400)
    name = body.get("filename") or "contact_sheet"
    return Response(
        content=data,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{name}.png"'},
    )


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
