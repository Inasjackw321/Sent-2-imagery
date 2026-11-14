"""A change result as the page draws it: the overlay, the before and after
radar images, the numbered spots, and the summary in plain words.

This replaces the original app's folium map. EarthViewer draws on its own
Leaflet map, so what the page needs is the pieces, not a finished HTML page.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict

from . import render
from .engine import DISPLAY_FACTOR, Result

MAX_SPOTS = 400

# What the three settings mean, for the page to say beside each button.
PRESET_WORDS = {
    "Sensitive": "Finds smaller changes; more of them may be noise.",
    "Balanced": "A change is a halving or doubling of the radar signal over at least 400 m².",
    "Strict": "Only large, unmistakable changes.",
}


def detection(preset: str | None = None, alpha: float | None = None,
              min_db: float | None = None, min_area_m2: float | None = None):
    """(name, Detection) for a preset, or a custom one from three numbers."""
    if preset and preset in render.PRESETS and alpha is None:
        return preset, render.PRESETS[preset]
    base = render.PRESETS.get(preset or "Balanced", render.Detection())
    det = render.Detection(
        alpha=float(alpha) if alpha is not None else base.alpha,
        min_db=float(min_db) if min_db is not None else base.min_db,
        min_area_m2=float(min_area_m2) if min_area_m2 is not None else base.min_area_m2)
    if not 0 < det.alpha < 1 or det.min_db < 0 or det.min_area_m2 < 0:
        raise ValueError("The detection settings are out of range")
    return "Custom", det


def noise_verdict(summary, analysed_km2: float) -> dict:
    """The no-change check in words: can the map be trusted at this setting?"""
    if summary.noise_km2 is None:
        return {"level": "unknown",
                "said": "Only one image before the start date, so the noise check could not run. "
                        "Use 2 or more images on each side to check the map."}
    share = summary.noise_km2 / max(analysed_km2, 1e-9)
    found = summary.increase_km2 + summary.decrease_km2
    if summary.noise_spots == 0 or share < 0.0002:
        return {"level": "good",
                "said": "Reliable: comparing the before images with each other finds "
                        "almost nothing, so what this map shows is real change."}
    if summary.noise_km2 < 0.25 * max(found, 1e-9):
        return {"level": "fair",
                "said": f"Mostly reliable: the before images compared with each other show "
                        f"{summary.noise_km2:.2f} km² of 'change', small next to what was found. "
                        "Try Strict to be sure."}
    return {"level": "poor",
            "said": f"Noisy: the before images compared with each other already show "
                    f"{summary.noise_km2:.2f} km² of 'change'. Choose Strict, or use more images "
                    "on each side."}


def _day(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


def result_view(result: Result, name: str, det) -> dict:
    """Everything the page draws for one result at one detection setting."""
    p = result.params
    found = render.detect(result.signed, result.change_db, det, len(p.bands))
    spots = render.find_spots(result, found)
    summary = render.summarize(result, det)
    (south, west), (north, east) = result.grid.latlon_bounds()
    layers = {}
    f = DISPLAY_FACTOR
    for side, db in (("before", result.before_db), ("after", result.after_db)):
        if db is not None:
            (s2, w2), (n2, e2) = result.grid.window_latlon(0, 0, db.shape[1] * f, db.shape[0] * f)
            layers[side] = {"image": render.backscatter_png(db), "bounds": [[s2, w2], [n2, e2]]}
    return {
        "key": p.cache_key(),
        "label": p.label,
        "params": {"bounds": list(p.bounds), "start": p.start, "end": p.end,
                   "images": p.images},
        "bounds": [[south, west], [north, east]],
        "overlay": render.overlay_png(found),
        "layers": layers,
        "before_days": result.before_days,
        "after_days": result.after_days,
        "before_said": render._span(result.before_days),
        "after_said": render._span(result.after_days),
        "orbit": result.orbit,
        "enl": round(result.enl, 2),
        "enl_estimated": result.enl_estimated,
        "detection": {"name": name, **asdict(det)},
        "presets": PRESET_WORDS,
        "summary": {**asdict(summary),
                    "noise": noise_verdict(summary, summary.analysed_km2)},
        "spots": [{"lat": round(s.lat, 6), "lon": round(s.lon, 6), "area_m2": round(s.area_m2),
                   "change_db": round(s.change_db, 2), "times": s.times, "when": s.when,
                   "kind": "decrease" if s.change_db < 0 else "increase"}
                  for s in spots[:MAX_SPOTS]],
        "spots_total": len(spots),
        "colours": {"decrease": render.DECREASE, "increase": render.INCREASE},
    }
