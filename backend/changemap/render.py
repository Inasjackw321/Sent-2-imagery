"""Turn a downloaded change result into numbers and an interactive map."""

import base64
import datetime as dt
import html
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from scipy import ndimage

from .engine import DISPLAY_FACTOR, GROUND_RES
from .stats import chi2_threshold, decode

DECREASE = "#ff3b30"
INCREASE = "#00c8ff"


@dataclass(frozen=True)
class Detection:
    """What counts as a change. A pixel must pass all three filters."""
    alpha: float = 1e-5  # statistical significance (chance of flagging an unchanged pixel)
    min_db: float = 3.0  # the radar signal must change by at least this much
    min_area_m2: float = 400  # and be part of a patch at least this big


PRESETS = {
    "Sensitive": Detection(alpha=1e-3, min_db=1.5, min_area_m2=100),
    "Balanced": Detection(),
    "Strict": Detection(alpha=1e-7, min_db=6.0, min_area_m2=2000),
}


@dataclass
class Summary:
    analysed_km2: float
    increase_km2: float
    decrease_km2: float
    spots: int
    noise_km2: float | None  # detected in the before-vs-before comparison, where nothing changed
    noise_spots: int | None


@dataclass
class Detected:
    increased: np.ndarray  # bool masks
    decreased: np.ndarray
    labels: np.ndarray  # connected patches, 0 = none
    ids: np.ndarray  # label numbers of the patches that passed the size filter

    @property
    def spots(self):
        return len(self.ids)


@dataclass
class Spot:
    lat: float
    lon: float
    area_m2: float
    change_db: float  # mean change in total radar signal
    times: int  # how many times a change was seen between consecutive passes
    when: list  # ["28 Jun → 4 Jul 2026", ...]
    row: float = 0.0  # centre in grid pixels
    col: float = 0.0


# A pass-to-pass change counts for a spot when this share of its pixels show it.
SPOT_SHARE = 0.3


def detect(signed, change_db, det, dof):
    """Apply the three filters. Returns a Detected."""
    stat, _, valid = decode(signed)
    db = change_db.astype(np.float32) / 100
    flag = valid & (stat > chi2_threshold(det.alpha, dof)) & (np.abs(db) >= det.min_db)
    labels, n = ndimage.label(flag, structure=np.ones((3, 3)))
    ids = np.zeros(0, int)
    if n:
        min_px = max(1, int(np.ceil(det.min_area_m2 / GROUND_RES ** 2)))
        keep = np.bincount(labels.ravel()) >= min_px
        keep[0] = False
        flag, ids = keep[labels], np.flatnonzero(keep)
    increased = db > 0
    return Detected(flag & increased, flag & ~increased, labels, ids)


def find_spots(result, detected):
    """Location, size, change and pass-to-pass history of each detected patch,
    largest first."""
    ids = detected.ids
    if not len(ids):
        return []
    labels, grid = detected.labels, result.grid
    pixel_m2 = np.broadcast_to((grid.row_area_km2() * 1e6)[:, None], labels.shape)
    area = ndimage.sum(pixel_m2, labels, ids)
    db = ndimage.mean(result.change_db.astype(np.float32) / 100, labels, ids)
    rows, cols = np.array(ndimage.center_of_mass(np.ones_like(labels, bool), labels, ids)).T
    passes = result.passes
    seen = np.zeros((len(passes) - 1, len(ids)), bool)
    if result.events is not None:
        for i in range(len(passes) - 1):
            bit = (result.events >> result.events.dtype.type(i)) & 1
            seen[i] = ndimage.mean(bit, labels, ids) >= SPOT_SHARE
    spots = []
    for k in range(len(ids)):
        lat, lon = grid.pixel_latlon(rows[k], cols[k])
        when = [f"{_day(passes[i])} → {_day(passes[i + 1])}" for i in np.flatnonzero(seen[:, k])]
        spots.append(Spot(lat=float(lat), lon=float(lon), area_m2=float(area[k]),
                          change_db=float(db[k]), times=len(when), when=when,
                          row=float(rows[k]), col=float(cols[k])))
    return sorted(spots, key=lambda sp: -sp.area_m2)


def _day(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


def summarize(result, det):
    dof = len(result.params.bands)
    row_area = result.grid.row_area_km2()
    km2 = lambda mask: float(mask.sum(axis=1) @ row_area)
    found = detect(result.signed, result.change_db, det, dof)
    valid = decode(result.signed)[2]
    noise_km2 = noise_spots = None
    if result.null_signed is not None:
        noise = detect(result.null_signed, result.null_db, det, dof)
        noise_km2, noise_spots = km2(noise.increased | noise.decreased), noise.spots
    return Summary(km2(valid), km2(found.increased), km2(found.decreased), found.spots,
                   noise_km2, noise_spots)


def _span(days):
    """'1 Apr – 19 May 2021' for a list of ISO dates."""
    first, last = (dt.date.fromisoformat(d) for d in (days[0], days[-1]))
    if first == last:
        return f"{first.day} {first:%b %Y}"
    head = f"{first.day} {first:%b}" if first.year == last.year else f"{first.day} {first:%b %Y}"
    return f"{head} – {last.day} {last:%b %Y}"


def _hex_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def overlay_png(found):
    """RGBA PNG (as a data URL) of the detected changes; the rest transparent."""
    inc, dec = found.increased, found.decreased
    rgba = np.zeros(inc.shape + (4,), dtype=np.uint8)
    rgba[dec] = _hex_rgb(DECREASE) + (255,)
    rgba[inc] = _hex_rgb(INCREASE) + (255,)
    return _png(Image.fromarray(rgba, "RGBA"))


def _png(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=6)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def backscatter_png(db):
    """Greyscale PNG of a stored uint8 backscatter layer; no data transparent."""
    return _png(Image.fromarray(np.dstack([db, db, db, np.where(db > 0, 255, 0).astype(np.uint8)]), "RGBA"))


def spot_tooltip(spot):
    kind = "Signal decreased" if spot.change_db < 0 else "Signal increased"
    seen = (f"Change seen {spot.times} time{'s' if spot.times != 1 else ''} between passes"
            + (":<br>" + "<br>".join(spot.when) if spot.when else ""))
    return f"<b>{kind}</b> by {abs(spot.change_db):.1f} dB over {spot.area_m2:,.0f} m²<br>{seen}"


def spot_badge(spot):
    color = DECREASE if spot.change_db < 0 else INCREASE
    return (f'<div style="font:bold 11px/16px system-ui,sans-serif;min-width:16px;height:16px;'
            f'padding:0 3px;border-radius:8px;background:#fff;color:#111;text-align:center;'
            f'border:2px solid {color};box-shadow:0 0 2px #000">{spot.times}</div>')
