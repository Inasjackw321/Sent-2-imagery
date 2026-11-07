"""S2DR3: Gamma Earth's Sentinel-2 Deep Resolution 3, at 1 m, through their API.

Unlike SEN2SR and SR4RS this does not run here. The model is Gamma Earth's
and closed: it is offered as a hosted service (and as a Linux-only wheel for
Colab, which a desktop app on Windows cannot use). So this button talks to the
hosted API with the official client, `gamma-earth-s2dr` (Apache-2.0, pure
Python, so it installs on any Python the app runs on), using the person's own
Gamma Earth API key and credits.

What that means, and how this module handles it:

  It costs money. A job spends credits from the person's account. So nothing
  is ever submitted without a price: quote() searches -- which is free --
  and returns the API's own estimate for this exact area and date; start()
  submits only that quoted scene, and only up to that price (the client's
  max_credits), after the page has shown the price and been told yes. A
  product the account already owns is downloaded instead, for nothing.

  The key is the person's. It is held in this process's memory only -- never
  written to disk, never logged, never sent to the page (status() says only
  whether there is one). It goes from here to api.gamma.earth and nowhere
  else; the client itself refuses to send it to any other host. GE_API_KEY in
  the environment works too.

  It takes minutes. The job runs on Gamma Earth's servers; start() follows it
  on a thread and the page polls status().

What comes back is the product's true-colour GeoTIFF (the `tci` asset), 1 m
per pixel, kept beside the app's other state and drawn onto the map's grid
by render(). The fine detail is the network's reconstruction, which the page
says beside the picture as it does for the other two.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

NEEDS = {"gamma_earth_s2dr": "gamma-earth-s2dr>=0.2,<1"}
RES_M = 1.0
NATIVE_RES_M = 10.0
SCALE = 10
ASSET = "tci"
CREDIT = "S2DR3 — Gamma Earth, Sentinel-2 Deep Resolution 3, 1 m"
SITE = "https://www.gamma.earth/try-it-free/"
WAIT_S = 60 * 60          # how long a job is followed before giving up on it
POLL_S = 20
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class S2dr3Error(RuntimeError):
    pass


class NotInstalled(S2dr3Error):
    pass


class NoKey(S2dr3Error):
    pass


# ── Installed? ─────────────────────────────────────────────────


def missing() -> list[str]:
    importlib.invalidate_caches()
    return [name for name in NEEDS if importlib.util.find_spec(name) is None]


def install_command(names: list[str] | None = None) -> list[str]:
    names = list(NEEDS) if names is None else names
    base = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix) and os.name != "nt":
        base.append("--user")
    return base + [NEEDS[n] for n in names]


_lock = threading.Lock()
_install: dict[str, Any] = {"running": False, "error": None}


def install(run=None) -> bool:
    import subprocess
    run = run or subprocess.run
    with _lock:
        if _install["running"]:
            return False
        _install.update(running=True, error=None)
    try:
        done = run(install_command(missing()), capture_output=True, text=True)
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
            raise S2dr3Error(f"pip could not install it: {tail[0][:300]}")
        if missing():
            raise S2dr3Error("pip finished but it is still missing")
        return True
    except (OSError, S2dr3Error) as exc:
        with _lock:
            _install["error"] = str(exc)
        return False
    finally:
        with _lock:
            _install["running"] = False


def install_in_background() -> bool:
    with _lock:
        if _install["running"]:
            return False
    threading.Thread(target=install, name="s2dr3-install", daemon=True).start()
    return True


# ── The key ────────────────────────────────────────────────────

_key: str | None = None


def set_key(key: str | None) -> None:
    """Hold the key in memory, or forget it with None or ''. Never saved."""
    global _key
    if key is None or key == "":
        with _lock:
            _key = None
        return
    if (not isinstance(key, str) or not 8 <= len(key) <= 256
            or not re.fullmatch(r"[\x21-\x7e]+", key)):
        raise S2dr3Error("That does not look like a Gamma Earth API key")
    with _lock:
        _key = key


def has_key() -> bool:
    return bool(_key or os.environ.get("GE_API_KEY"))


def _client():
    if missing():
        raise NotInstalled("S2DR3 needs gamma-earth-s2dr installed")
    if not has_key():
        raise NoKey("S2DR3 needs your Gamma Earth API key")
    from gamma_earth_s2dr import Client
    return Client(api_key=_key or None)


# ── Where products are kept ────────────────────────────────────


def products_dir() -> Path:
    override = os.environ.get("S2DR3_DIR")
    if override:
        return Path(override)
    from .launcher import state_dir
    return state_dir() / "s2dr3"


# ── The job, followed on a thread ──────────────────────────────

_job: dict[str, Any] = {"state": "idle"}
_quotes: dict[str, dict] = {}
MOST_QUOTES = 20


def status() -> dict[str, Any]:
    gone = missing()
    with _lock:
        job = {k: v for k, v in _job.items() if k != "item"}
        install_state = dict(_install)
    return {
        "installed": not gone,
        "installing": install_state["running"],
        "install_error": install_state["error"],
        "command": " ".join(install_command(gone or list(NEEDS))),
        "has_key": has_key(),
        "job": job,
        "credit": CREDIT,
        "resolution_m": RES_M,
        "site": SITE,
    }


def _check_bounds(bounds) -> tuple[float, float, float, float]:
    try:
        west, south, east, north = (float(v) for v in bounds)
    except (TypeError, ValueError):
        raise S2dr3Error("The area is not a bounding box") from None
    if not all(math.isfinite(v) for v in (west, south, east, north)) \
            or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise S2dr3Error("The area is not a bounding box")
    return west, south, east, north


def _check_date(date: str) -> str:
    date = str(date or "")[:10]
    if not DATE.fullmatch(date):
        raise S2dr3Error("S2DR3 needs the date of one Sentinel-2 pass")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise S2dr3Error("S2DR3 needs the date of one Sentinel-2 pass") from None
    return date


def _covers(bbox, bounds, slack=1e-6) -> bool:
    if not bbox or len(bbox) < 4:
        return False
    west, south, east, north = bbox[0], bbox[1], bbox[-2], bbox[-1]
    return (west <= bounds[0] + slack and south <= bounds[1] + slack
            and east >= bounds[2] - slack and north >= bounds[3] - slack)


def _day(item) -> str:
    when = getattr(item, "datetime", None)
    if when is None:
        when = (item.properties or {}).get("start_datetime") or ""
        return str(when)[:10]
    return when.date().isoformat()


def quote(bounds, date: str) -> dict:
    """What S2DR3 of this area on this day would cost. Spends nothing.

    Returns {"token", "owned", "credits", "date", "warnings"}: owned means the
    account already has a product covering it, which is free to fetch.
    """
    bounds = _check_bounds(bounds)
    date = _check_date(date)
    client = _client()
    from gamma_earth_s2dr import GammaEarthError, NeedsConfirmation
    after = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    try:
        found = client.search(bbox=bounds, datetime=(date, after))
    except GammaEarthError as exc:
        raise S2dr3Error(f"Gamma Earth: {exc}") from exc
    warnings = [str(w)[:200] for w in found.warnings][:5]

    owned = [p for p in found.products if _day(p) == date and _covers(p.bbox, bounds)]
    if owned:
        return _remember({"item": owned[0], "owned": True, "credits": 0,
                          "date": date, "bounds": bounds, "warnings": warnings})

    scenes = [s for s in found.scenes if _day(s) == date]
    if not scenes:
        raise S2dr3Error(
            f"Gamma Earth offers no Sentinel-2 scene of this area on {date}"
            + (f" ({warnings[0]})" if warnings else "")
            + ". Try another date.")
    scene = scenes[0]
    if scene.properties.get("s2dr3:processable") is False:
        raise S2dr3Error("That area is too large for one S2DR3 job — draw a smaller shape.")
    try:
        client.process(scene)            # never confirmed: it only says the price
    except NeedsConfirmation as exc:
        credits = exc.credits
    except GammaEarthError as exc:
        raise S2dr3Error(f"Gamma Earth: {exc}") from exc
    else:                                # cannot happen without confirm=True
        raise S2dr3Error("Gamma Earth processed a scene without asking")
    if credits is None:
        raise S2dr3Error("Gamma Earth showed no price for this scene, so it was not offered")
    return _remember({"item": scene, "owned": False, "credits": int(credits),
                      "date": date, "bounds": bounds, "warnings": warnings})


def _remember(entry: dict) -> dict:
    token = secrets.token_urlsafe(16)
    with _lock:
        while len(_quotes) >= MOST_QUOTES:
            _quotes.pop(next(iter(_quotes)))
        _quotes[token] = entry
    return {"token": token, "owned": entry["owned"], "credits": entry["credits"],
            "date": entry["date"], "warnings": entry["warnings"]}


def start(token: str, credits: int, follow=None) -> dict:
    """Make (or fetch) the quoted product, on a thread. Poll status().

    `credits` is the price the page showed and was agreed to; the job is
    refused by the client if it would cost more.
    """
    with _lock:
        if _job.get("state") == "running":
            raise S2dr3Error("An S2DR3 job is already running")
        entry = _quotes.pop(str(token), None)
        if entry is None:
            raise S2dr3Error("That price has expired — press the button again")
        if not isinstance(credits, int) or isinstance(credits, bool) \
                or credits != entry["credits"]:
            raise S2dr3Error("The price agreed to is not the price quoted")
        _job.clear()
        _job.update(state="running", said="Starting S2DR3…", started=time.time(),
                    date=entry["date"], bounds=list(entry["bounds"]),
                    owned=entry["owned"], credits=entry["credits"])
    thread = threading.Thread(target=follow or _follow, args=(entry,),
                              name="s2dr3-job", daemon=True)
    thread.start()
    return status()


def _say(**fields) -> None:
    with _lock:
        _job.update(fields)


def _follow(entry: dict, client=None) -> None:
    try:
        client = client or _client()
        from gamma_earth_s2dr import GammaEarthError
        dest = products_dir()
        try:
            if entry["owned"]:
                _say(said="Downloading your S2DR3 product…")
                collection = [entry["item"]]
            else:
                job = client.process(entry["item"], confirm=True,
                                     max_credits=entry["credits"])
                _say(job_id=job.job_id, said="S2DR3 is running on Gamma Earth's servers…",
                     credits_note=job.credits_note)
                begun = time.time()

                def pause(seconds):
                    _say(said="S2DR3 is running on Gamma Earth's servers — "
                              f"{int(time.time() - begun) // 60} min so far…")
                    time.sleep(seconds)

                collection = job.wait(timeout=WAIT_S, poll=POLL_S, sleep=pause)
                _say(said="Downloading the 1 m picture…")
            paths = client.download(collection, dest, assets=[ASSET])
        except GammaEarthError as exc:
            raise S2dr3Error(f"Gamma Earth: {exc}") from exc
        item = next(iter(collection))
        _say(state="done", said="", product={
            "id": item.id, "path": str(paths[0]), "date": _day(item) or entry["date"],
            "bbox": list(item.bbox or entry["bounds"]),
            "requested": entry["date"], "bounds": list(entry["bounds"])})
    except Exception as exc:    # whatever it was, the job is over and the page must hear
        _say(state="failed", said="", error=str(exc) or type(exc).__name__)


# ── Finding and drawing a product ──────────────────────────────


def product_for(bounds, date: str) -> dict | None:
    """The downloaded product covering this area for this date, if any.

    The one this session just made first; then anything earlier listed in the
    products folder's collection.json, which the client writes and which
    holds no key and no address.
    """
    bounds = tuple(bounds)
    with _lock:
        latest = _job.get("product") if _job.get("state") == "done" else None
    if latest and (latest["requested"] == date or latest["date"] == date) \
            and _covers(latest["bbox"], bounds) and Path(latest["path"]).is_file():
        return latest
    listing = products_dir() / "collection.json"
    try:
        features = json.loads(listing.read_text()).get("features", [])
    except (OSError, ValueError, AttributeError):
        return None
    for feature in features:
        props = feature.get("properties") or {}
        day = str(props.get("datetime") or props.get("start_datetime") or "")[:10]
        href = ((feature.get("assets") or {}).get(ASSET) or {}).get("href", "")
        path = (products_dir() / href).resolve()
        if (day == date and _covers(feature.get("bbox"), bounds)
                and path.is_file() and products_dir().resolve() in path.parents):
            return {"id": feature.get("id"), "path": str(path), "date": day,
                    "bbox": feature.get("bbox"), "requested": date}
    return None


def read_rgb(path: str, grid) -> tuple[np.ndarray, np.ndarray]:
    """The product's true colour on `grid`: (rows, cols, 3) uint8 and valid."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    with rasterio.open(path) as src:
        count = min(3, src.count)
        rgb = np.zeros((3, grid.height, grid.width), dtype="float32")
        for i in range(count):
            reproject(rasterio.band(src, i + 1), rgb[i],
                      src_nodata=src.nodata, dst_nodata=np.nan,
                      dst_transform=grid.transform, dst_crs=grid.crs,
                      resampling=Resampling.bilinear, init_dest_nodata=True)
        dtype = src.dtypes[0]
    valid = np.all(np.isfinite(rgb[:count]), axis=0)
    if count < 3:
        rgb[count:] = rgb[0]
    rgb = np.nan_to_num(rgb)
    valid &= rgb.max(axis=0) > 0                 # a black fill is not ground
    if dtype == "uint8":
        out = np.clip(rgb, 0, 255)
    else:
        # A product in reflectance, not ready-made colour: the same 2–98 %
        # stretch the app gives anything else, over what is ground.
        values = rgb[:, valid]
        low, high = (np.percentile(values, (2, 98)) if values.size else (0, 1))
        out = np.clip((rgb - low) / max(high - low, 1e-6), 0, 1) * 255
    return np.ascontiguousarray(out.transpose(1, 2, 0).astype("uint8")), valid
