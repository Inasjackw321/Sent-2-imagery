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
    aisstream, alerts, composite, config, fires, llm, passes, seismic, service, stac,
    telegram, version, vessels,
)
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


# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------


@app.get("/api/alerts")
def read_alerts(
    kinds: str = Query(""),
    placed: bool = Query(False),
) -> dict:
    """Everything read from the watched channels, newest first."""
    if config.DEMO_MODE:
        return {**alerts.demo(), "telegram": telegram.status()}
    wanted = [k for k in kinds.split(",") if k]
    return {**alerts.held(wanted or None, placed_only=placed),
            "telegram": telegram.status()}


@app.get("/api/alerts/status")
def alerts_status() -> dict:
    return {
        "telegram": telegram.status(),
        "model": llm.available(),
        "suggested": [{"name": n, "note": d} for n, d in llm.SUGGESTED],
        "kinds": alerts.KINDS,
    }


@app.post("/api/alerts/login")
def alerts_login(body: dict = Body(...)) -> dict:
    """Start signing in: Telegram sends a code to the account.

    The api_id and api_hash come from my.telegram.org and identify the
    application rather than the person. Nothing is written to disk.
    """
    try:
        return telegram.start_login(
            int(body.get("api_id") or 0),
            str(body.get("api_hash") or "").strip(),
            str(body.get("phone") or "").strip(),
        )
    except (ValueError, TypeError) as exc:
        raise _fail(exc, 400)
    except telegram.TelegramError as exc:
        raise _fail(exc)


@app.post("/api/alerts/code")
def alerts_code(body: dict = Body(...)) -> dict:
    """Finish signing in with the code, and a 2FA password if one is set."""
    try:
        return telegram.finish_login(
            str(body.get("code") or ""), body.get("password") or None)
    except telegram.TelegramError as exc:
        raise _fail(exc)


@app.post("/api/alerts/logout")
def alerts_logout() -> dict:
    return telegram.sign_out()


@app.get("/api/alerts/channels")
def alerts_channels() -> dict:
    """Every channel the signed-in account can read."""
    try:
        return {"channels": telegram.channels()}
    except telegram.TelegramError as exc:
        raise _fail(exc)


@app.post("/api/alerts/watch")
def alerts_watch(body: dict = Body(...)) -> dict:
    """Choose the channels, and start or stop the once-a-minute reading."""
    chosen = [str(c) for c in (body.get("channels") or [])]
    telegram.watch(chosen)
    try:
        if body.get("running") and chosen:
            return telegram.start_polling(_keep_alert)
        return telegram.stop_polling()
    except telegram.TelegramError as exc:
        raise _fail(exc)


@app.post("/api/alerts/poll")
def alerts_poll() -> dict:
    """Read the channels once, now, rather than waiting for the next minute."""
    try:
        found = telegram.poll_once(_keep_alert)
    except telegram.TelegramError as exc:
        raise _fail(exc)
    return {"read": found, **alerts.held()}


@app.post("/api/alerts/model")
def alerts_model(body: dict = Body(...)) -> dict:
    """Point the reader at a different local model."""
    return llm.set_model(str(body.get("model") or ""))


@app.post("/api/alerts/clear")
def alerts_clear() -> dict:
    alerts.forget()
    return alerts.held()


def _keep_alert(message: dict) -> None:
    """One message from a channel, read and kept."""
    if alerts.known(message["id"]):
        return
    try:
        alerts.add(message)
    except Exception as exc:  # a bad message must not stop the poll
        log.warning("alert skipped: %s", exc)


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
