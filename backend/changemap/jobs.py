"""The change map as the page uses it: check what images there are, run the
comparison on a thread while the page watches, and open saved results.

One comparison at a time. Each downloads tens to hundreds of megabytes of
radar and keeps several cores busy, and two at once would only make both
slower.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from collections import OrderedDict
from typing import Any

import numpy as np

from .. import config
from . import engine, inputs, render, stats

KEY = re.compile(r"[0-9a-f]{16}")
FIRST_DAY = dt.date(2014, 10, 3)   # the start of the Sentinel-1 RTC archive
GAP_WARN_DAYS = 14


class ChangeError(ValueError):
    """Something about the request, worded for the page."""


def params_from(body: dict) -> tuple[engine.Params, str | None]:
    """Parameters from what the page sent, the area fitted to what can be
    analysed at 10 m. Returns (params, note about any change to the area)."""
    try:
        bounds = tuple(float(v) for v in body["bounds"])
        start = dt.date.fromisoformat(str(body["start"]))
        end = dt.date.fromisoformat(str(body["end"]))
    except (KeyError, TypeError, ValueError):
        raise ChangeError("Draw an area and pick a start and an end date") from None
    if len(bounds) != 4 or not (-180 <= bounds[0] < bounds[2] <= 180
                                and -85 <= bounds[1] < bounds[3] <= 85):
        raise ChangeError("Draw an area on the map first")
    if not start < end:
        raise ChangeError("The end date has to be after the start date")
    if end > dt.date.today():
        raise ChangeError("The end date can't be in the future")
    if start < FIRST_DAY:
        raise ChangeError("Sentinel-1 radar images start in October 2014")
    try:
        images = int(body.get("images", 3))
    except (TypeError, ValueError):
        images = 3
    images = max(1, min(engine.MAX_IMAGES, images))
    fitted, note = inputs.fit_area(bounds)
    label = re.sub(r"[\x00-\x1f]", "", str(body.get("label") or "")).strip()[:80]
    if not label:
        cy, cx = (fitted[1] + fitted[3]) / 2, (fitted[0] + fitted[2]) / 2
        label = f"Area at {cy:.3f}, {cx:.3f}"
    return engine.Params(label=label, bounds=fitted, start=str(start), end=str(end),
                         images=images), note


def _demo_days(params: engine.Params) -> tuple[list[str], list[str]]:
    start, end = dt.date.fromisoformat(params.start), dt.date.fromisoformat(params.end)
    before = [str(start - dt.timedelta(days=12 * k + 2)) for k in range(params.images)][::-1]
    after = [str(end - dt.timedelta(days=12 * k + 1)) for k in range(params.images)
             if end - dt.timedelta(days=12 * k + 1) > start][::-1] or [str(end)]
    return before, after


def plan(body: dict) -> dict:
    """Which images a comparison would use, before anything is downloaded."""
    params, note = params_from(body)
    grid = engine.Grid.for_bounds(params.bounds)
    if config.DEMO_MODE:
        before, after = _demo_days(params)
        orbit, state, mb = 36, "descending", grid.pixels * 2 * 2 * params.images * engine.BYTES_PER_PIXEL / 1e6
    else:
        try:
            found = engine.make_plan(params)
        except ValueError as exc:
            raise ChangeError(str(exc)) from exc
        except engine.source.SourceError as exc:
            raise ChangeError(str(exc)) from exc
        before, after = [str(d) for d in found.before], [str(d) for d in found.after]
        orbit, state, mb = found.orbit, found.orbit_state, found.download_mb
    warnings = []
    if len(before) < params.images:
        warnings.append(f"Only {len(before)} image{'s' if len(before) != 1 else ''} before the "
                        f"start date from this orbit, not {params.images}.")
    if len(after) < params.images:
        warnings.append(f"Only {len(after)} image{'s' if len(after) != 1 else ''} between the "
                        f"dates from this orbit, not {params.images}.")
    gap_b = (dt.date.fromisoformat(params.start) - dt.date.fromisoformat(before[-1])).days
    gap_a = (dt.date.fromisoformat(params.end) - dt.date.fromisoformat(after[-1])).days
    if gap_b > GAP_WARN_DAYS:
        warnings.append(f"The latest image before the start date is {gap_b} days earlier.")
    if gap_a > GAP_WARN_DAYS:
        warnings.append(f"The latest image before the end date is {gap_a} days earlier.")
    return {"params": {"bounds": list(params.bounds), "start": params.start, "end": params.end,
                       "images": params.images, "label": params.label},
            "note": note, "orbit": orbit, "orbit_state": state,
            "before": before, "after": after, "download_mb": round(mb),
            "area_km2": round(inputs.area_km2(params.bounds), 1),
            "warnings": warnings, "key": params.cache_key(),
            "saved": (engine.CACHE_DIR / f"{params.cache_key()}.npz").exists()}


# ── The running comparison ─────────────────────────────────────

_lock = threading.Lock()
_job: dict[str, Any] = {"state": "idle"}


def status() -> dict:
    with _lock:
        return dict(_job)


def start(body: dict, run=None) -> dict:
    """Start a comparison on a thread. Poll status()."""
    params, _ = params_from(body)
    with _lock:
        if _job.get("state") == "running":
            raise ChangeError("A change map is already being made")
        _job.clear()
        _job.update(state="running", fraction=0.0, message="Starting…", key=params.cache_key(),
                    label=params.label, started=time.time())
    threading.Thread(target=_work, args=(params, run), name="changemap", daemon=True).start()
    return status()


def _say(fraction: float, message: str) -> None:
    with _lock:
        _job.update(fraction=round(float(fraction), 3), message=message)


def _work(params: engine.Params, run=None) -> None:
    try:
        if run is not None:
            result = run(params, progress=_say)
        elif config.DEMO_MODE:
            result = demo_result(params, progress=_say)
        else:
            result = engine.run(params, progress=_say)
        _remember(result)
        with _lock:
            _job.update(state="done", fraction=1.0, message="Done", key=params.cache_key())
    except Exception as exc:  # the page must hear, whatever it was
        message = str(exc) or type(exc).__name__
        with _lock:
            _job.update(state="failed", message=message, error=message)


# ── Results ────────────────────────────────────────────────────

_held: "OrderedDict[str, engine.Result]" = OrderedDict()


def _remember(result: engine.Result) -> None:
    with _lock:
        _held[result.params.cache_key()] = result
        _held.move_to_end(result.params.cache_key())
        while len(_held) > 2:
            _held.popitem(last=False)


def load(key: str) -> engine.Result:
    """A saved result by its key."""
    if not KEY.fullmatch(str(key)):
        raise ChangeError("Not a change map")
    with _lock:
        if key in _held:
            return _held[key]
    path = engine.CACHE_DIR / f"{key}.npz"
    if not path.is_file():
        raise ChangeError("That change map isn't saved on this computer any more")
    try:
        result = engine.Result.load(path)
    except (OSError, ValueError, KeyError) as exc:
        raise ChangeError("That change map was saved by an older version and can't be opened") from exc
    _remember(result)
    return result


def saved() -> list[dict]:
    """Saved results, newest first."""
    out = []
    for path, said in engine.saved_results():
        out.append({"key": path.stem, "said": said,
                    "when": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def forget(key: str) -> None:
    if not KEY.fullmatch(str(key)):
        raise ChangeError("Not a change map")
    with _lock:
        _held.pop(key, None)
    (engine.CACHE_DIR / f"{key}.npz").unlink(missing_ok=True)


# ── Demo ───────────────────────────────────────────────────────

def demo_result(params: engine.Params, progress=lambda f, m: None) -> engine.Result:
    """A synthetic result for the offline build: speckled ground, a block that
    lost 7 dB, one that gained 6, and traffic that came and went."""
    grid = engine.Grid.for_bounds(params.bounds)
    scale = min(1.0, (1600 * 1600 / grid.pixels) ** 0.5)
    if scale < 1:   # keep the stand-in quick: a coarser grid over the same ground
        grid = engine.Grid(grid.x0, grid.y0, grid.scale / scale,
                           max(1, int(grid.width * scale)), max(1, int(grid.height * scale)))
    rng = np.random.default_rng(abs(hash(params.cache_key())) % 2**32)
    h, w = grid.height, grid.width
    for k in range(5):
        progress(0.1 + 0.18 * k, f"Downloading and comparing images: part {k + 1} of 5 (demo)")
        time.sleep(0.15)
    stat = rng.chisquare(2, (h, w))
    db = rng.normal(0, 0.6, (h, w))
    events = np.zeros((h, w), np.uint16)
    blocks = [(0.35, 0.40, -7.0, 0b0010), (0.62, 0.30, 6.0, 0b0100), (0.50, 0.70, 9.0, 0b1010)]
    for fy, fx, change, bits in blocks:
        r0, c0 = int(fy * h), int(fx * w)
        rr, cc = max(3, h // 25), max(3, w // 25)
        stat[r0:r0 + rr, c0:c0 + cc] = 80
        db[r0:r0 + rr, c0:c0 + cc] = change
        events[r0:r0 + rr, c0:c0 + cc] = bits
    encode = lambda st, d: (np.round(np.minimum(st, stats.STAT_MAX) * stats.STAT_SCALE
                                     * np.sign(d)).astype(np.int16),
                            np.round(np.clip(d, -300, 300) * 100).astype(np.int16))
    signed, change_db = encode(stat, db)
    null_signed, null_db = encode(rng.chisquare(2, (h, w)), rng.normal(0, 0.6, (h, w)))
    dh, dw = -(-h // engine.DISPLAY_FACTOR), -(-w // engine.DISPLAY_FACTOR)
    ground = rng.uniform(60, 200, (dh, dw))
    before_db = np.clip(ground + rng.normal(0, 15, (dh, dw)), 1, 255).astype(np.uint8)
    after_db = np.clip(ground + rng.normal(0, 15, (dh, dw)), 1, 255).astype(np.uint8)
    before, after = _demo_days(params)
    result = engine.Result(
        params=params, grid=grid, signed=signed, change_db=change_db, orbit=36,
        before_days=before, after_days=after, enl=float(4.4 * params.smooth ** 2),
        enl_estimated=False, null_signed=null_signed, null_db=null_db,
        events=events if len(before) + len(after) >= 4 else None,
        before_db=before_db, after_db=after_db)
    result.save(engine.CACHE_DIR / f"{params.cache_key()}.npz")
    progress(1.0, "Done")
    return result


def detection_from(query: dict) -> tuple[str, render.Detection]:
    from . import view
    def num(name):
        v = query.get(name)
        return float(v) if v not in (None, "") else None
    try:
        return view.detection(query.get("preset") or "Balanced", num("alpha"), num("min_db"),
                              num("min_area_m2"))
    except (TypeError, ValueError) as exc:
        raise ChangeError(str(exc) or "The detection settings are out of range") from exc
