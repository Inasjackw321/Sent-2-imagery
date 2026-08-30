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

from . import (
    aisstream, composite, config, fires, passes, seismic, service, stac, version,
    vessels, weather,
)
from .geo import geodesic_area_km2, geometry_bounds, normalise_aoi
from .raster import BandReadError

log = logging.getLogger("sent2")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="EarthViewer", version="1.0.0")

# What the page in the browser is allowed to load, and from where.
#
# Most of this app's traffic goes out from Python, where a policy like this
# does nothing. What it constrains is the browser: map tiles, the webcam
# embeds, and the one library fetched from a CDN at runtime. Those are the
# parts that run somebody else's code in your session, and the reason to
# enumerate them is that anything not on this list simply does not load --
# including anything a compromised camera host or CDN might try to pull in.
#
# The frames are the point of it. A webcam page is another site's JavaScript
# running in your browser; the sandbox attribute on the iframe limits what it
# may do, and frame-src here limits which sites may be framed at all.
CSP = "; ".join([
    "default-src 'self'",
    # Leaflet and this app's own modules are served from here. jsDelivr carries
    # hls.js, fetched only when an HLS camera is opened.
    "script-src 'self' https://cdn.jsdelivr.net",
    # Inline styles are unavoidable: element styles are set from JavaScript all
    # over the interface, and every one of them is a string this code wrote.
    "style-src 'self' 'unsafe-inline'",
    # Map tiles come from several providers, and a rendered scene arrives as a
    # data: URL. blob: is the decoded seismogram.
    "img-src 'self' data: blob: "
    "https://*.tile.openstreetmap.org https://tile.openstreetmap.org "
    "https://*.tile.openstreetmap.fr https://*.tile.opentopomap.org "
    "https://server.arcgisonline.com https://*.rainviewer.com "
    "https://gibs.earthdata.nasa.gov "
    "https://imgproxy.windy.com https://www.ndbc.noaa.gov "
    "https://airtw.moenv.gov.tw https://pildid.teeilm.ee "
    "https://pics.starvisor.net",
    # Everything the browser fetches by script: this backend, the radar index,
    # and the HLS playlists and segments.
    # EarthCam serves its playlists from numbered video hosts that rotate, and
    # a playlist names its own segment host, so this one is a wildcard where
    # the others are exact. It widens the policy only across a domain already
    # trusted enough to be framed below.
    "connect-src 'self' https://api.rainviewer.com "
    "https://*.streamlock.net https://*.vdotcameras.com "
    "https://*.earthcam.com https://cdn.jsdelivr.net",
    "media-src 'self' blob: https://*.streamlock.net https://*.vdotcameras.com "
    "https://*.earthcam.com",
    # The camera embeds, named one host at a time.
    "frame-src https://ipcamlive.com https://rtsp.me https://vkvideo.ru "
    "https://www.earthcam.com",
    # Nothing here submits a form, embeds a plugin, or should ever be framed
    # by anybody else.
    "form-action 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
])


@app.middleware("http")
async def security_headers(request, call_next):
    """Headers that only matter for the page, applied to everything.

    Cheaper and harder to forget than remembering to attach them to each of
    the handful of routes that return HTML.
    """
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    # A browser guessing at content types is how an image becomes a script.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


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
        "vessels": {
            "source": vessels.SOURCE["label"],
            "bounds": list(vessels.SOURCE["bounds"]),
            "attribution": vessels.SOURCE["attribution"],
            "global_source": "aisstream.io",
            "global_key_set": aisstream.has_key(),
            "min_interval": aisstream.MIN_INTERVAL_SECONDS,
        },
        "fires": {
            "windows": sorted(fires.WINDOWS),
            "sensors": {k: v["label"] for k, v in fires.SENSORS.items()},
            "attribution": "NASA FIRMS",
            "keyed": bool(fires.MAP_KEY),
        },
        "seismic": {
            "windows": {str(k): v for k, v in seismic.WINDOWS.items()},
            "trace_minutes": {str(k): v for k, v in seismic.TRACE_MINUTES.items()},
            "events": seismic.ATTRIBUTION["events"],
            "stations": seismic.ATTRIBUTION["stations"],
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
            limit=max(1, min(int(body.get("limit", stac.DEFAULT_LIMIT)),
                             stac.MAX_LIMIT)),
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


@app.get("/api/vessels")
def ships(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    source: str = Query("digitraffic"),
) -> dict:
    """Every ship broadcasting AIS inside a rectangle."""
    box = (west, south, east, north)
    if config.DEMO_MODE:
        return {**vessels.demo_vessels(box), "next_in": aisstream.MIN_INTERVAL_SECONDS}
    if source == "aisstream":
        try:
            return aisstream.vessels_in(box)
        except aisstream.StreamError as exc:
            raise _fail(exc)
    try:
        return vessels.vessels_in(box)
    except vessels.VesselLookupError as exc:
        raise _fail(exc)


@app.get("/api/quakes")
def earthquakes(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    hours: int = Query(168, ge=1, le=720),
    min_magnitude: float = Query(2.5, ge=-1.0, le=10.0),
) -> dict:
    """Every earthquake the USGS has located inside a rectangle."""
    box = (west, south, east, north)
    if config.DEMO_MODE:
        return seismic.demo_quakes(box, hours=hours, min_magnitude=min_magnitude)
    try:
        return seismic.quakes(box, hours=hours, min_magnitude=min_magnitude)
    except seismic.SeismicLookupError as exc:
        raise _fail(exc)


@app.get("/api/seismographs")
def seismographs(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
) -> dict:
    """Open seismograph stations in a rectangle, still recording today."""
    box = (west, south, east, north)
    if config.DEMO_MODE:
        return seismic.demo_stations(box)
    try:
        return seismic.stations(box)
    except seismic.SeismicLookupError as exc:
        raise _fail(exc)


@app.get("/api/seismographs/trace.png")
def seismograph_trace(
    network: str = Query(..., min_length=1, max_length=8),
    station: str = Query(..., min_length=1, max_length=8),
    channel: str = Query("BHZ", min_length=2, max_length=4),
    loc: str = Query("", max_length=2),
    minutes: int = Query(60, ge=1, le=1440),
) -> Response:
    """The last few minutes of ground motion at one station, plotted.

    Proxied rather than linked so a station with nothing to give says so in
    words, instead of the browser showing a broken image and leaving the
    reader to guess whether the instrument or the app is at fault.
    """
    if config.DEMO_MODE:
        png = seismic.demo_trace(network, station, channel, minutes=minutes)
    else:
        try:
            png = seismic.trace(network, station, channel, loc=loc, minutes=minutes)
        except seismic.SeismicLookupError as exc:
            raise _fail(exc)
    return Response(
        content=png, media_type="image/png",
        # The window ends a couple of minutes ago and moves on, so a cached
        # copy would quietly stop being live.
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/vessels/key")
def ais_key(body: dict = Body(...)) -> dict:
    """Hand the app an aisstream API key, or take it away again.

    Kept in memory for as long as the process lives and written nowhere. It
    is the operator's own key on the operator's own machine, and it should not
    outlive the run.
    """
    ok = aisstream.set_key(body.get("key"))
    return {"set": ok, "min_interval": aisstream.MIN_INTERVAL_SECONDS}


@app.post("/api/vessels/test")
def ais_test() -> dict:
    """Try the key once, over busy water, and say exactly what happened.

    Outside the five-minute floor on purpose: it is the button someone
    presses when the map is empty and guessing has stopped being useful.
    """
    return aisstream.test_key()


@app.get("/api/weather")
def weather_at(
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
) -> dict:
    """Weather now and for the next few days, at one point.

    Sits beside the pass prediction because the two answer one question
    between them: when there will be a picture, and whether there will be
    anything visible in it.
    """
    if config.DEMO_MODE:
        return weather.demo(lon, lat)
    try:
        return weather.at(lon, lat)
    except weather.WeatherError as exc:
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
