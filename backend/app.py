"""FastAPI application: JSON API plus the static front end."""

from __future__ import annotations

import base64
import datetime as dt
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import (
    aisstream, animate as animation, composite, config, copernicus,
    fires, gazetteer, mtg,
    ollama, passes, seismic, service, shake, stac, tracker, version, vessels,
    weather,
)
from .geo import geodesic_area_km2, geometry_bounds, normalise_aoi
from .raster import BandReadError, scenes_are_safe

log = logging.getLogger("sent2")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="EarthViewer", version="1.0.0")


@app.exception_handler(Exception)
def anything_else(request, exc: Exception):
    """Turn an unexpected failure into an answer rather than a crash.

    Every one of these endpoints leans on somebody else's service, and a
    service can change its reply at any time. Each handler catches its own
    error class, which covers the failures that were thought of; this covers
    the ones that were not.

    It exists because of a real one. A free model returned a 200 whose content
    field was null -- legitimate, it does that for a refusal -- and the reader
    called .strip() on None. AttributeError is not any of the error classes
    the endpoints catch, so it went past all of them, and the layer answered
    an unreadable 500 instead of falling back to reading the reports without
    a model, which it was perfectly able to do.

    The traceback goes to the log, where it is useful. What the browser gets
    is one sentence it can put in a panel.
    """
    log.exception("unhandled error in %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}".strip()[:300]},
    )

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
    "https://server.arcgisonline.com https://*.rainviewer.com "
    "https://gibs.earthdata.nasa.gov "
    "https://imgproxy.windy.com https://www.ndbc.noaa.gov "
    "https://airtw.moenv.gov.tw https://ristmikud.tallinn.ee "
    "https://pics.starvisor.net https://www.customs.gov.by "
    "https://customs.gov.md "
    "https://eismoinfo.lt https://view.eumetsat.int "
    "https://imageserver.webcamera.pl",
    # Everything the browser fetches by script: this backend, the radar index,
    # and the HLS playlists and segments.
    # EarthCam serves its playlists from numbered video hosts that rotate, and
    # a playlist names its own segment host, so this one is a wildcard where
    # the others are exact. It widens the policy only across a domain already
    # trusted enough to be framed below.
    # The tile hosts appear here as well as in img-src because the map fetches
    # one tile from each service to read its status code. A refusal can arrive
    # as a perfectly valid picture, and an <img> never reveals that it came
    # with a 403 on it. This allows a GET of a URL already permitted as an
    # image, which widens nothing.
    # galata.ai numbers its CDN hosts (cdn-101, and a playlist may name
    # another for its segments), so it is a wildcard for the same reason
    # EarthCam is.
    "connect-src 'self' https://api.rainviewer.com "
    "https://server.arcgisonline.com "
    "https://*.streamlock.net https://*.vdotcameras.com "
    "https://*.earthcam.com https://*.galata.ai https://cdn.jsdelivr.net",
    "media-src 'self' blob: https://*.streamlock.net https://*.vdotcameras.com "
    "https://*.earthcam.com https://*.galata.ai",
    # The camera embeds, named one host at a time.
    "frame-src https://ipcamlive.com https://rtsp.me https://vkvideo.ru "
    "https://www.earthcam.com https://balticlivecam.com",
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


# Who may talk to this API from a browser other than by being this app.
#
# Nobody, which is the point. This used to allow every origin, and that was a
# real hole rather than an untidy default: this server listens on loopback,
# and a page on any website the operator happened to have open could reach it
# — read every answer, drive a render, clear the AIS key — because the
# browser will happily send a cross-origin request to 127.0.0.1 and, with a
# wildcard, hand back the reply. The same-origin policy is the thing that
# normally stops a web page rummaging through what is running on your own
# machine, and the wildcard was switching it off.
#
# Nothing needed it. The page is served by this app, so every call it makes is
# same-origin and no CORS header is involved at all. The list below is empty
# rather than absent so that a deployment which really does serve the front
# end from somewhere else has one obvious place to name it.
CORS_ORIGINS: list[str] = []

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )


# The last complaint written to the log, when it was written, and how many
# identical ones have arrived since. See _fail.
_said: tuple[str, float, int] = ("", 0.0, 0)
_said_lock = threading.Lock()
SAY_AGAIN_SECONDS = 60


def worth_saying(message: str, now: float,
                 last: tuple[str, float, int]) -> tuple[bool, str, tuple[str, float, int]]:
    """Whether to write this complaint to the log, and what to write.

    Returns (write it, what to write, the new state).

    Pulled out and pure because of what it is fixing. One playable loop of a
    ten-minute satellite is tens of tiles, all in flight together, all
    against the same host -- so when EUMETSAT stops answering, every one of
    them times out separately and writes the same three-line requests
    exception to the log. The reader gets a wall of identical text, in which
    the one fact ("EUMETSAT is not answering") is no easier to find than it
    would have been in silence, and any *other* failure that happens in the
    same minute is buried in it.

    So an identical message inside the window is counted rather than
    written, and the count is attached to the next thing that is.
    """
    was, at, missed = last
    if message == was and now - at < SAY_AGAIN_SECONDS:
        return False, "", (was, at, missed + 1)
    extra = f" (and {missed} more like the last one)" if missed else ""
    return True, message + extra, (message, now, 0)


def _fail(exc: Exception, status: int = 502) -> HTTPException:
    global _said
    message = str(exc)
    with _said_lock:
        write, said, _said = worth_saying(message, time.time(), _said)
    if write:
        log.warning("request failed: %s", said)
    return HTTPException(status_code=status, detail=message)


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
        # The one picture that is about a pair of dates rather than one.
        "change": {
            "label": config.CHANGE["label"],
            "hint": config.CHANGE["hint"],
            "range": config.CHANGE["range"],
            "colormap": config.CHANGE["colormap"],
            "sat": ["sentinel-1"],
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
            "shake": shake.ATTRIBUTION,
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
    # The scene comes back from the page, so its asset addresses are whatever
    # the caller put in them. See raster.scene_is_safe. Refused with 400
    # rather than 502: the request is the thing at fault, not an upstream.
    try:
        scenes_are_safe(body)
    except BandReadError as exc:
        raise _fail(exc, 400)
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


@app.get("/api/tracker")
def tracker_events() -> dict:
    """Air-threat reports from public Telegram channels, as map events.

    Every part of this stays on the server: Telegram is read here and the
    model runs here, so the browser never talks to either. What comes back is
    positions, areas and masses, already placed and already checked.
    """
    if config.DEMO_MODE:
        return tracker.demo()
    try:
        return tracker.refresh()
    except tracker.TrackerError as exc:
        # Not a failure of the endpoint: Ollama not running, a model not
        # pulled, a channel that would not answer. The map wants to keep
        # drawing what it already has and say why nothing new arrived, rather
        # than go blank on a 502.
        answer = tracker.current()
        answer["state"] = str(exc)
        return answer


@app.get("/api/tracker/outlines")
def tracker_outlines() -> dict[str, Any]:
    """The region boundaries, for the page to draw a map of its own.

    Not in the tracker feed, and that is the point of a separate endpoint:
    this is a few hundred kilobytes of borders that change about never, and
    the feed is fetched every thirty seconds. Asked for once, when somebody
    exports a picture.
    """
    return {"outlines": tracker.outlines()}


@app.get("/api/tracker/photo")
def tracker_photo(u: str = Query(..., max_length=600)):
    """One picture from a Telegram post, fetched here rather than by the page.

    The browser never talks to Telegram anywhere else in this layer, and it
    should not start for pictures: an <img> pointed straight at their CDN
    hands them the viewer's address every time a popup opens, which is a lot
    to give away for a thumbnail.

    Everything that makes this safe is in tracker.fetch_photo -- an allowlist
    of Telegram's own CDN, no redirects, an image content type and a size
    ceiling. An endpoint that fetches a URL from its caller is otherwise a way
    into whatever this process can reach and the caller cannot.
    """
    try:
        body, kind = tracker.fetch_photo(u)
    except tracker.TrackerError as exc:
        raise _fail(exc, status=400)
    return Response(content=body, media_type=kind, headers={
        # The files are immutable at their URL, so let the browser keep them:
        # a popup reopened should not refetch.
        "Cache-Control": "private, max-age=3600",
    })


@app.get("/api/ollama")
def ollama_status() -> dict:
    """Whether the local model is there, which one, and how to fix it.

    Its own endpoint, and a GET, because the page asks this on its own -- the
    daemon can be started after the page is open, and a panel that said "not
    connected" until a reload would be wrong more often than right.
    """
    return ollama.status()


@app.post("/api/tracker/dismiss")
def tracker_dismiss(body: dict = Body(...)) -> dict:
    """Take one mark off the map, or put it back.

    A POST rather than a DELETE because it is reversible and because the
    restore is the same operation with a flag -- two verbs for one toggle
    would be tidier REST and a worse thing to use.

    It hides a mark; it does not edit the record. The report stays in the
    stream, marked, so what a channel actually said is not something a browser
    can change.
    """
    ident = str(body.get("id") or "")[:160]
    if not ident:
        raise HTTPException(status_code=400, detail="no id given")
    if body.get("restore"):
        return {"restored": tracker.restore(ident),
                "dismissed": tracker.dismissed_now()}
    return {"removed": tracker.dismiss(ident),
            "dismissed": tracker.dismissed_now()}


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


@app.get("/api/mtg")
def mtg_layers(refresh: bool = Query(False)) -> dict:
    """Which MTG lightning layers EUMETSAT is serving, and how fresh they are.

    The tiles go straight from the browser to EUMETSAT; this only asks what
    exists and when it is from. A layer reaches the live list by declaring a
    recent frame -- never by being named the right thing.
    """
    if config.DEMO_MODE:
        return mtg.demo()
    try:
        return mtg.layers(refresh=refresh)
    except mtg.MTGError as exc:
        raise _fail(exc)


@app.get("/api/copernicus")
def copernicus_layers(refresh: bool = Query(False)) -> dict:
    """Sentinel-3 and Sentinel-5P, as live layers rather than as imagery.

    They are published as NetCDF granules, which the render pipeline -- built
    on windowed reads of cloud-optimised GeoTIFFs -- cannot use. EUMETSAT
    serves them as ordinary WMS, so that is how they arrive.
    """
    if config.DEMO_MODE:
        return copernicus.demo()
    try:
        return copernicus.layers(refresh=refresh)
    except copernicus.CopernicusError as exc:
        raise _fail(exc)


@app.get("/api/copernicus/wms")
def copernicus_tile(request: Request) -> Response:
    """One WMS tile, fetched through this app rather than from the browser.

    Two reasons, and the second is the load-bearing one.

    Held here, a frame fetched once serves every later loop and every other
    person watching -- which is what makes eight hours of a ten-minute
    satellite playable rather than a download per pass.

    And served from this origin, a tile is an ordinary same-origin image. A
    cross-origin one taints any canvas it is drawn into and the browser then
    refuses to hand the pixels back, so the exported picture and the recorded
    video would both be impossible. Not a convenience: the feature cannot
    exist without it.
    """
    try:
        body, kind, _hit = copernicus.tile(dict(request.query_params))
    except copernicus.CopernicusError as exc:
        raise _fail(exc)
    return Response(content=body, media_type=kind, headers={
        # A published frame of a fixed past moment never changes, so the
        # browser may keep it as long as it likes.
        "Cache-Control": "public, max-age=86400, immutable",
    })


@app.get("/api/copernicus/held")
def copernicus_held() -> dict:
    """What the tile cache is holding. For the panel's "ready to play"."""
    return copernicus.tiles_held()


@app.post("/api/animate")
def animate(body: dict = Body(...)):
    """Several passes over one place, as one animated GIF.

    Each frame goes through the same render path a single picture does, so a
    frame and a saved picture are the same pixels rather than two code paths
    that drift apart.
    """
    scenes = body.get("scenes") or []
    if not scenes:
        raise _fail(ValueError("pick some dates to animate"), 400)
    try:
        scenes_are_safe(body)
    except BandReadError as exc:
        raise _fail(exc, 400)

    def one(scene: dict) -> bytes:
        made = service.render({**body, "scenes": [scene], "scene": scene,
                               "format": "png"})
        return made["bytes"]

    try:
        gif, dates = animation.animate(
            scenes, one,
            ms=int(body.get("ms") or animation.FRAME_MS),
            bounce=bool(body.get("bounce")))
    except animation.AnimateError as exc:
        raise _fail(exc, 400)
    except (ValueError, service.RenderError) as exc:
        raise _fail(exc, 400)
    except (BandReadError, stac.SceneSearchError) as exc:
        raise _fail(exc)
    stem = f"{scenes[0].get('satellite', 'imagery')}_{dates[0]}-{dates[-1]}"
    return Response(content=gif, media_type="image/gif", headers={
        "Content-Disposition": f'attachment; filename="{stem}_kaldockhi.gif"',
    })


@app.get("/api/selftest")
def selftest() -> dict:
    """Try every outside service this app depends on, and say what happened.

    Written because "fires, lightning and the rest don't work" is a report
    nobody can act on, from either end. Each layer talks to a different
    service, each fails quietly into its own panel, and none of them says
    whether the problem is the network here, a provider that has changed, or
    the code. This asks all of them at once and answers in one page.

    Only reachability -- one small request each, no keys sent, nothing
    interpreted. A service answering here and the layer still being empty is
    itself a useful result: it separates "cannot get there from this machine"
    from "got there and did not like the answer".
    """
    checks = [
        ("fires", "NASA FIRMS",
         f"{fires.FIRMS_ROOT}/api/area/csv/x/VIIRS_SNPP_NRT/-1,-1,1,1/1"),
        ("radar", "RainViewer", "https://api.rainviewer.com/public/weather-maps.json"),
        ("lightning", "EUMETSAT View", mtg.WMS + "?service=WMS&request=GetCapabilities"),
        ("places", "Nominatim", config.NOMINATIM_URL + "?q=Kyiv&format=jsonv2&limit=1"),
        ("imagery", "Copernicus STAC", config.STAC_URL),
        ("reports", "Telegram preview",
         tracker.PREVIEW.format(channel=tracker.CHANNELS[0]["name"])),
        ("basemap", "Esri basemap tiles",
         "https://server.arcgisonline.com/ArcGIS/rest/services"
         "/World_Street_Map/MapServer/tile/3/2/4"),
    ]

    out = []
    for layer, name, url in checks:
        started = dt.datetime.now(dt.timezone.utc)
        row: dict[str, Any] = {"layer": layer, "service": name}
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": config.USER_AGENT},
                                stream=True)
            row["status"] = resp.status_code
            # 4xx from a probe URL is still proof the host is reachable, which
            # is the question being asked. Only a refusal to connect is a no.
            row["reached"] = True
            row["ok"] = resp.ok
            resp.close()
        except requests.RequestException as exc:
            row["reached"] = False
            row["ok"] = False
            row["why"] = f"{type(exc).__name__}: {exc}"[:200]
        row["ms"] = round(
            (dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
        out.append(row)

    return {
        "checked": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "demo": config.DEMO_MODE,
        "build": version.described(),
        "services": out,
        "unreachable": [r["service"] for r in out if not r["reached"]],
        "keys": {
            # Whether one is set, never what it is.
            "firms_map_key": bool(fires.MAP_KEY),
            "aisstream": aisstream.has_key(),
        },
    }


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


@app.get("/api/shake")
def raspberry_shakes(
    refresh: bool = Query(False),
    west: float | None = Query(None, ge=-180, le=180),
    south: float | None = Query(None, ge=-90, le=90),
    east: float | None = Query(None, ge=-180, le=180),
    north: float | None = Query(None, ge=-90, le=90),
) -> dict:
    """The Raspberry Shake seismographs, kept apart from the professional ones.

    Two halves, deliberately. The named stations are a decision in the source:
    somebody is watching those places and they are in the answer wherever the
    map is pointed. The rectangle is a question: what else of this network is
    inside the view -- which is the only way to find out that a coast has four
    amateur instruments on it when no research station is within six hundred
    kilometres.

    The rectangle is optional. Without it this answers exactly as it did
    before, which is what the panel asks for before the map has settled.
    """
    corners = (west, south, east, north)
    box = tuple(corners) if all(c is not None for c in corners) else None
    if config.DEMO_MODE:
        return shake.demo_stations(box)
    return shake.stations(refresh=refresh, box=box)


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
    # The search box wants a list to choose from, so it asks Nominatim
    # directly rather than through the gazetteer, which answers with the one
    # best match. They share the rate limit, because Nominatim's policy is
    # about this process and not about which of its functions is calling.
    gazetteer.wait_turn()
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


# What a download's filename may be made of.
#
# The stem is built from the scene, and a scene arrives in the request body,
# so a quote or a newline in it lands in a response header. A newline there is
# header injection; a quote is a second filename in the same Content-
# Disposition. Neither is a plausible date, so the answer is to keep the
# characters a filename actually needs and drop the rest.
FILENAME_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def filename_stem(stem: str, fallback: str = "imagery") -> str:
    """A download filename with nothing in it that could be a header."""
    kept = "".join(c if c in FILENAME_OK else "_" for c in str(stem))
    kept = kept.strip("._")[:120]
    return kept or fallback


def _render_response(result: dict, download: bool, stem: str):
    if download:
        ext = {"image/tiff": "tif", "image/jpeg": "jpg",
               "image/png": "png"}.get(result["media_type"], "bin")
        return Response(
            content=result["bytes"],
            media_type=result["media_type"],
            headers={"Content-Disposition":
                     f'attachment; filename="{filename_stem(stem)}.{ext}"'},
        )
    return JSONResponse({
        "image": f"data:{result['media_type']};base64,"
                 + base64.b64encode(result["bytes"]).decode(),
        "meta": result["meta"],
    })


@app.post("/api/render")
def render(body: dict = Body(...), download: bool = Query(False)):
    # See the note in probe(): the addresses in a scene are the caller's.
    try:
        scenes_are_safe(body)
    except BandReadError as exc:
        raise _fail(exc, 400)
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
