"""Finding Sentinel scenes in the catalogue.

Earth Search is a free, anonymous STAC API in front of the Sentinel archives on
AWS Open Data: no account, no key, no token. A search returns one flat record
per pass over the area, carrying the URLs of the band files it is made of, so
the rest of the app never has to know how the catalogue is shaped.

Sentinel-1 and Sentinel-2 live in separate collections and are searched
separately -- a cloud-cover filter would throw away every radar scene, because
radar has no such property -- then interleaved into one list of passes over the
area, each one saying which satellite took it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import threading
from typing import Any

import requests

from . import config
from .geo import geometry_bounds

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


class SceneSearchError(RuntimeError):
    pass


# How many items to ask for per page. Catalogues cap this themselves; 100 is
# what both of the ones used here accept.
PAGE_SIZE = 100

# How many pages to follow before stopping. Enough to reach back over the
# whole Sentinel-2 archive for one place -- about 900 passes since 2015 --
# without letting a search over a whole country run away with itself.
MAX_PAGES = 12

# What a search returns when nobody says otherwise. A year over one place is
# roughly seventy Sentinel-2 passes, so this comfortably clears it.
DEFAULT_LIMIT = 250

# And the ceiling, however much is asked for.
MAX_LIMIT = 1200


def _best_href(asset: dict) -> str | None:
    """Prefer an https URL; fall back to whatever the item offers."""
    alternates = asset.get("alternate") or {}
    for key in ("https", "http"):
        if key in alternates and alternates[key].get("href"):
            return alternates[key]["href"]
    href = asset.get("href")
    if href and href.startswith("s3://"):
        # Public buckets serve the same object over https.
        bucket, _, path = href[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{path}"
    return href


# ── Search ─────────────────────────────────────────────────────


def _iso_day(value: str | None, fallback: dt.date) -> str:
    return str(value)[:10] if value else fallback.isoformat()


def normalise_satellites(value) -> list[str]:
    """Whatever the caller asked for, as a list of known satellite keys."""
    if value in (None, "", "all", "both"):
        return list(config.SATELLITES)
    keys = [value] if isinstance(value, str) else list(value)
    chosen = [k for k in keys if k in config.SATELLITES]
    if not chosen:
        raise SceneSearchError(f"Unknown satellite: {keys}")
    return chosen


def search_scenes(
    geometry: dict,
    start: str | None = None,
    end: str | None = None,
    max_cloud: float = 30.0,
    limit: int = DEFAULT_LIMIT,
    demo: bool | None = None,
    satellites=None,
) -> dict[str, Any]:
    """Every pass over the area from the satellites asked for, newest first."""
    today = dt.date.today()
    start = _iso_day(start, today - dt.timedelta(days=365))
    end = _iso_day(end, today)
    wanted = normalise_satellites(satellites)

    if demo if demo is not None else config.DEMO_MODE:
        return {"scenes": _demo_scenes(geometry, start, end, max_cloud, limit, wanted),
                "demo": True, "satellites": wanted}

    scenes: list[dict] = []
    matched = 0
    for key in wanted:
        found, count = _search_one(config.SATELLITES[key], geometry, start, end,
                                   max_cloud, limit)
        scenes.extend(found)
        matched += count or 0

    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    pieces = len(scenes)
    scenes = fold_passes(scenes, geometry_bounds(geometry))
    # `matched` is what the catalogue holds; `scenes` is what was fetched. The
    # two differ once a search runs past the page budget, and the front end
    # says so rather than implying the archive stops here.
    return {"scenes": scenes, "demo": False, "matched": matched,
            "fetched": pieces, "passes": len(scenes), "satellites": wanted}


# ── One pass, not six pieces of one ────────────────────────────
#
# A satellite does not take a picture of your area; it flies over, recording
# continuously, and the catalogue cuts what it recorded into squares. For
# Sentinel-2 those are the hundred-kilometre MGRS tiles, and an ordinary area
# of interest sits across several of them -- so one pass over one town comes
# back as six entries, same date, same minute, different squares.
#
# Listed as six, that is wrong twice over. It reads as six chances to see the
# ground when it is one, and it offers a picture per tile: pick any single one
# and the area is rendered with the parts that fell in the other tiles simply
# missing. The six cloud figures make it worse, because they are six readings
# of six different hundred-kilometre squares and none of them is the cloud
# over the area asked for.
#
# So the pieces of a pass are folded into one entry that carries them all, and
# rendering it lays them side by side. Which pieces belong together is decided
# by the satellite, the instant and the track -- not by the date alone, since
# two passes on the same day from adjacent tracks are two passes.

# How far apart two acquisitions can be and still be the same flight over.
# A Sentinel-2 orbit crosses a continent in minutes and the tiles of one pass
# carry timestamps seconds apart; two passes over the same ground are days
# apart, or at the very least a whole orbit -- ninety minutes.
SAME_PASS_MINUTES = 20


def pass_key(scene: dict[str, Any]) -> tuple:
    """What makes two scenes pieces of the same flight over.

    The track as well as the day: a place near the edge of two adjacent
    tracks is photographed by both, sometimes within a day of each other, and
    those are genuinely two passes with two views of the ground.
    """
    when = _parse_time(scene.get("datetime"))
    slot = int(when.timestamp() // (SAME_PASS_MINUTES * 60)) if when else scene.get("date")
    return (scene.get("satellite"), scene.get("orbit"), slot)


def _overlap(box: Any, area: tuple[float, float, float, float] | None) -> float:
    """How much of the area asked for this piece covers, as a fraction.

    Rectangles rather than footprints: a Sentinel-2 tile is very nearly a
    rectangle in its own projection, the area asked for is usually a box the
    reader drew, and the answer is only used to weigh one cloud figure
    against another. A polygon intersection here would be more arithmetic for
    the same decision.
    """
    if not box or len(box) < 4 or not area:
        return 1.0
    west, south, east, north = area
    wide = max(0.0, min(box[2], east) - max(box[0], west))
    tall = max(0.0, min(box[3], north) - max(box[1], south))
    whole = max(1e-12, (east - west) * (north - south))
    return max(0.0, min(1.0, (wide * tall) / whole))


def fold_passes(scenes: list[dict[str, Any]],
                area: tuple[float, float, float, float] | None = None
                ) -> list[dict[str, Any]]:
    """The tiles of each pass, folded into one entry per pass.

    The entry is the first piece with the rest attached: same shape as any
    other scene, so everything downstream that reads a date or a satellite off
    it carries on working, and `pieces` is there for the renderer to lay side
    by side.
    """
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for scene in scenes:
        groups.setdefault(pass_key(scene), []).append(scene)

    out = []
    for pieces in groups.values():
        if len(pieces) == 1:
            out.append(pieces[0])
            continue
        # The piece covering most of the area leads, so the entry's id, tile
        # and assets are the ones a reader would have picked by hand.
        pieces = sorted(pieces, key=lambda s: -_overlap(s.get("bbox"), area))
        folded = dict(pieces[0])
        folded["pieces"] = [dict(p) for p in pieces]
        folded["tile"] = f"{len(pieces)} tiles of one pass"
        folded["tiles"] = [p.get("tile") for p in pieces if p.get("tile")]
        cloud = _pass_cloud(pieces, area)
        if cloud is not None:
            folded["cloud"] = cloud
        out.append(folded)
    out.sort(key=lambda s: s.get("datetime") or "", reverse=True)
    return out


def _pass_cloud(pieces: list[dict[str, Any]],
                area: tuple[float, float, float, float] | None) -> float | None:
    """One cloud figure for a folded pass, weighted by what each piece covers.

    Averaging the pieces flat would let a tile clipping one corner of the area
    count as much as the one holding the rest of it -- and on the pass that
    prompted this, the six figures ran from 59% to 99%.
    """
    weighed = [(s, _overlap(s.get("bbox"), area)) for s in pieces
               if s.get("cloud") is not None]
    if not weighed:
        return None
    total = sum(weight for _, weight in weighed)
    if total <= 0:
        return round(sum(float(s["cloud"]) for s, _ in weighed) / len(weighed), 1)
    return round(sum(float(s["cloud"]) * weight for s, weight in weighed) / total, 1)


def sources_for(sat: dict) -> list[dict]:
    """The catalogues to try for a satellite, best first."""
    keys = list(sat.get("sources") or ("earth-search",))
    if sat["kind"] == "radar" and config.S1_SOURCE in config.SOURCES:
        keys = [config.S1_SOURCE]          # pinned by the environment
    return [config.SOURCES[k] for k in keys if k in config.SOURCES]


def _search_one(sat: dict, geometry: dict, start: str, end: str,
                max_cloud: float, limit: int) -> tuple[list[dict], int]:
    """Ask each catalogue in turn until one answers.

    Falling through matters for radar: the catalogues carry the same
    acquisitions but not in the same state, and one being unreachable or
    holding nothing for this area should not be the end of it. Whichever
    answered is written onto every scene, because reading its pixels later
    depends on knowing where they came from.
    """
    problems = []
    for source in sources_for(sat):
        try:
            found, count = _search_source(source, sat, geometry, start, end,
                                          max_cloud, limit)
        except SceneSearchError as exc:
            problems.append(str(exc))
            continue
        if found:
            return found, count
        problems.append(f"{source['label']} has no {sat['short']} pass here")

    if problems and len(problems) == len(sources_for(sat)):
        # Every catalogue was asked and none of them produced anything. An
        # empty result is not an error, but being unable to reach any of them
        # is -- and saying which were tried is what makes it fixable.
        if all("has no" in p for p in problems):
            return [], 0
        raise SceneSearchError("; ".join(problems))
    return [], 0


def _search_source(source: dict, sat: dict, geometry: dict, start: str, end: str,
                   max_cloud: float, limit: int) -> tuple[list[dict], int]:
    """Every matching pass, following the catalogue's pages until there are
    enough of them.

    A STAC search answers one page at a time and says where the next one is.
    Asking once and stopping -- which is what this did -- caps the archive at
    whatever fits in a single response however wide a date range was asked
    for, so a search over ten years came back with the most recent handful and
    looked like the satellite had only just launched.

    Sorting is asked for explicitly as well. Without it the order across pages
    is whatever the catalogue felt like, and taking the first page of an
    unordered result is not the same as taking the newest scenes.
    """
    payload: dict[str, Any] = {
        "collections": [sat["collection"]],
        "intersects": geometry,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": min(int(limit), PAGE_SIZE),
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    if sat["cloud_filter"]:
        payload["query"] = {"eo:cloud_cover": {"lt": float(max_cloud)}}

    found: list[dict] = []
    matched = 0
    url = f"{source['stac']}/search"
    body: dict | None = payload

    for _ in range(MAX_PAGES):
        try:
            resp = (_session.post(url, json=body, timeout=60) if body is not None
                    else _session.get(url, timeout=60))
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            if found:
                # Some of the archive is better than none of it: a page that
                # fails after several have worked should not lose those.
                break
            raise SceneSearchError(
                f"Could not reach {source['label']} for {sat['short']}: {exc}") from exc

        matched = matched or data.get("numberMatched") or 0
        found.extend(s for s in (scene_summary(item, sat["key"], source["key"])
                                 for item in data.get("features", [])) if s)
        if len(found) >= limit:
            break

        step = _next_page(data)
        if not step:
            break
        url, body = step

    return found[:limit], matched or len(found)


def _next_page(data: dict) -> tuple[str, dict | None] | None:
    """Where the catalogue says the next page is, if there is one.

    Two conventions are in the wild and both are met here: a POST link that
    carries its own body, and a plain GET link with the paging token already
    in the query string.
    """
    for link in data.get("links") or []:
        if link.get("rel") != "next" or not link.get("href"):
            continue
        if str(link.get("method", "GET")).upper() == "POST":
            return link["href"], link.get("body") or {}
        return link["href"], None
    return None


def scene_summary(item: dict, satellite: str | None = None,
                  source: str | None = None) -> dict[str, Any] | None:
    """Flatten a STAC item into what the app needs."""
    props = item.get("properties", {})
    assets = item.get("assets", {})

    hrefs = {name: href for name, asset in assets.items()
             if (href := _best_href(asset))}
    if not hrefs:
        return None

    key = satellite or config.satellite_for_collection(item.get("collection"))
    sat = config.satellite(key)
    when = props.get("datetime") or props.get("start_datetime") or ""
    cloud = props.get("eo:cloud_cover")

    return {
        "id": item.get("id"),
        "satellite": key,
        # Which catalogue served it: reading its pixels needs to know, because
        # some of them hand out URLs that must be signed before they open.
        "source": source or "earth-search",
        "datetime": when,
        "date": when[:10],
        "cloud": round(float(cloud), 1) if cloud is not None else None,
        "platform": props.get("platform") or sat["platform"],
        "tile": _tile_label(props) or _radar_label(props),
        "epsg": props.get("proj:epsg"),
        # Where this piece of the pass actually is. Kept because a pass is
        # delivered as a row of tiles and, folded back together, the tiles
        # have to be weighed by how much of the area asked for each one
        # covers -- a tile clipping one corner should not decide the cloud
        # figure for the whole picture.
        "bbox": [float(v) for v in (item.get("bbox") or [])[:4]] or None,
        "orbit": props.get("sat:relative_orbit"),
        "orbit_state": props.get("sat:orbit_state"),
        # The SAR extension. Fetched for every scene and meaningless for an
        # optical one, which is why sar.describe() answers nothing for those
        # rather than this being filtered here -- a scene is one shape.
        #
        # These are what make a radar pass readable: which pair of
        # polarisations it carries decides which pictures it can make at all,
        # and the mode decides how sharp it is.
        "polarisations": [str(p).upper() for p in
                          (props.get("sar:polarizations") or [])
                          if str(p).strip()],
        "mode": props.get("sar:instrument_mode"),
        "product": props.get("sar:product_type"),
        "boa_offset_applied": bool(props.get("earthsearch:boa_offset_applied", False)),
        "sun_elevation": props.get("view:sun_elevation"),
        "thumbnail": (assets.get("thumbnail") or {}).get("href"),
        "assets": hrefs,
        "demo": False,
    }


# ── Signing ────────────────────────────────────────────────────

# Signatures are handed out with an expiry, so they are held until shortly
# before they run out rather than fetched per band of every render.
_tokens: dict[str, tuple[dt.datetime, str]] = {}
_token_lock = threading.Lock()
TOKEN_MARGIN = dt.timedelta(minutes=5)


def sign_href(scene: dict, href: str) -> str:
    """Make one asset URL openable.

    Some catalogues publish their pixels in storage that will not serve an
    unsigned request. The signature is free and anonymous -- ask the token
    endpoint, put what it returns on the end of the URL -- but it has to be
    asked for, and it expires, so it is fetched once and reused.
    """
    source = config.SOURCES.get(scene.get("source") or "")
    if not source or not source.get("sign") or not href.startswith("http"):
        return href
    token = _token(source, config.satellite(scene.get("satellite"))["collection"])
    if not token:
        return href
    return f"{href}{'&' if '?' in href else '?'}{token}"


def _token(source: dict, collection: str) -> str:
    key = f"{source['key']}/{collection}"
    now = dt.datetime.now(dt.timezone.utc)
    with _token_lock:
        held = _tokens.get(key)
        if held and held[0] - TOKEN_MARGIN > now:
            return held[1]

    url = source["sign"].format(collection=collection)
    try:
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SceneSearchError(
            f"{source['label']} would not sign the imagery: {exc}") from exc

    token = data.get("token") or ""
    expiry = _parse_time(data.get("msft:expiry")) or (now + dt.timedelta(minutes=30))
    with _token_lock:
        _tokens[key] = (expiry, token)
    return token


def _parse_time(value) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def _tile_label(props: dict) -> str:
    """The MGRS square the scene comes from, e.g. 30UXC."""
    return "".join(str(props.get(k, "")) for k in
                   ("mgrs:utm_zone", "mgrs:latitude_band", "mgrs:grid_square"))


def _radar_label(props: dict) -> str:
    """A radar pass has no tile: which way it was flying is what identifies it."""
    state = str(props.get("sat:orbit_state") or "")[:3].upper()
    orbit = props.get("sat:relative_orbit")
    mode = props.get("sar:instrument_mode") or ""
    return " ".join(str(p) for p in (mode, state, orbit and f"#{orbit}") if p)


def get_scene(scene_id: str, satellite: str | None = None) -> dict[str, Any]:
    """Re-fetch one scene, so the client never has to hold asset URLs."""
    if scene_id.startswith("demo-"):
        return _demo_scene_from_id(scene_id)

    # Sentinel-1 ids start with S1, Sentinel-2's with S2, so a caller who did
    # not say which collection to look in usually need not.
    key = satellite or ("sentinel-1" if scene_id.upper().startswith("S1")
                        else config.DEFAULT_SATELLITE)
    sat = config.satellite(key)

    problems = []
    for source in sources_for(sat):
        url = f"{source['stac']}/collections/{sat['collection']}/items/{scene_id}"
        try:
            resp = _session.get(url, timeout=45)
            resp.raise_for_status()
        except requests.RequestException as exc:
            problems.append(f"{source['label']}: {exc}")
            continue
        summary = scene_summary(resp.json(), sat["key"], source["key"])
        if summary:
            return summary
        problems.append(f"{source['label']} holds no readable assets for it")

    raise SceneSearchError(f"Scene {scene_id} could not be fetched — "
                           + "; ".join(problems))


def boa_offset(scene: dict) -> int:
    """Sentinel-2's baseline 04.00 DN offset, where it applies.

    From January 2022 the ground segment shifted the stored numbers by -1000 so
    that a few genuinely dark pixels would stop clipping at zero. Scenes older
    than that, and scenes the catalogue has already corrected, need nothing.
    """
    if scene.get("boa_offset_applied"):
        return 0
    # The offset is a Sentinel-2 processing-baseline quirk. Nothing else has it.
    if scene.get("satellite") not in (None, "sentinel-2"):
        return 0
    return config.BOA_OFFSET if scene.get("date", "") >= config.BOA_OFFSET_DATE else 0


# ── Synthetic scenes (DEMO_MODE) ───────────────────────────────


_DEMO_CADENCE = {"sentinel-2": 5, "sentinel-1": 6, "landsat": 8}
# A demo scene has to say which satellite it came from inside its own id, since
# that id is all the client hands back when it asks for the scene again.
# Sentinel-2 carries no tag so ids made before there was a third satellite
# still read correctly.
_DEMO_TAG = {"sentinel-2": "", "sentinel-1": "s1", "landsat": "ls"}
_DEMO_HOUR = {"sentinel-2": "10:30:00", "sentinel-1": "17:52:00",
              "landsat": "10:05:00"}


def _demo_scenes(geometry, start, end, max_cloud, limit, satellites=None) -> list[dict]:
    """A believable list of passes for exploring offline.

    Both satellites fly over, on their own cadences and at their own times of
    day, so the offline app behaves like the real one right down to the radar
    pass landing on a different date from the optical one.
    """
    west, south, _, _ = geometry_bounds(geometry)
    seed = int(hashlib.sha1(f"{west:.3f},{south:.3f}".encode()).hexdigest()[:8], 16)
    end_d = dt.date.fromisoformat(end)
    # The offline catalogue used to stop at sixty however far back it was
    # asked to go, which made the demo look like the archive began two months
    # ago -- exactly the thing the real search was also doing.
    per_satellite = max(1, min(int(limit), MAX_LIMIT) // max(len(satellites or [1]), 1))

    scenes: list[dict] = []
    for key in satellites or [config.DEFAULT_SATELLITE]:
        sat = config.satellite(key)
        start_d = dt.date.fromisoformat(max(start, sat["since"]))
        day = end_d
        i = 0
        taken = 0
        while day >= start_d and taken < per_satellite:
            cloud = round(abs(math.sin(seed * 0.017 + i * 1.7)) * 55, 1)
            # Radar does not care about cloud, so it is never filtered by it.
            if sat["cloud_filter"] and cloud > max_cloud:
                pass
            else:
                scenes.append(_demo_scene(day, cloud, seed, key))
                taken += 1
            day -= dt.timedelta(days=_DEMO_CADENCE[key])
            i += 1
    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    return scenes


def _demo_scene(day: dt.date, cloud: float, seed: int, satellite: str | None = None) -> dict:
    key = satellite or config.DEFAULT_SATELLITE
    sat = config.satellite(key)
    radar = sat["kind"] == "radar"
    # Which way this pass was going. Off the DAY rather than off the seed,
    # because the seed is one value per area and every pass then came out the
    # same way round -- which never exercises the warning about averaging two
    # lightings of the same hill. Off the day it also survives the round trip
    # through a scene id, which is how a chosen date is re-fetched.
    up = (day.toordinal() // 6) % 2 == 0
    tag = _DEMO_TAG.get(key, "")
    suffix = f"-{tag}" if tag else ""
    return {
        "id": f"demo-{day.isoformat()}-{seed % 9973}-{int(round(cloud * 10))}{suffix}",
        "satellite": key,
        "datetime": f"{day.isoformat()}T{_DEMO_HOUR[key]}Z",
        "date": day.isoformat(),
        # Radar records no cloud cover, and pretending otherwise would make the
        # date list lie about what the pass can see through.
        "cloud": None if radar else cloud,
        "platform": f"{sat['platform']} (synthetic)",
        "tile": (f"IW {'ASC' if up else 'DSC'} #{59 if up else 36}"
                 if radar else ("199/024" if key == "landsat" else "DEMO")),
        "epsg": 3857,
        "orbit": (59 if up else 36) if radar else 108,
        "orbit_state": ("ascending" if up else "descending")
                       if radar else None,
        "boa_offset_applied": True,
        "sun_elevation": None if radar else 45.0,
        "thumbnail": None,
        # The same SAR fields a live radar pass carries, so the offline build
        # shows the same line under a scene that the real one does -- and a
        # screenshot from the demo explains the real thing rather than a
        # different app.
        #
        # Alternating passes, because the whole point of showing the direction
        # is that it changes: a demo where every pass is ascending never
        # exercises the warning about averaging two lightings together.
        "polarisations": ["VV", "VH"] if radar else [],
        "mode": "IW" if radar else None,
        "product": "GRD" if radar else None,
        "assets": {},
        "demo": True,
    }


def _demo_scene_from_id(scene_id: str) -> dict:
    parts = scene_id.split("-")
    if len(parts) < 6:
        raise SceneSearchError(f"Malformed demo scene id: {scene_id}")
    day = dt.date.fromisoformat("-".join(parts[1:4]))
    tagged = {tag: key for key, tag in _DEMO_TAG.items() if tag}
    satellite = tagged.get(parts[-1], config.DEFAULT_SATELLITE)
    return _demo_scene(day, int(parts[5]) / 10.0, int(parts[4]), satellite)
