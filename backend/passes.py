"""When either Sentinel next flies over a point.

There is no public "next overpass" service, but there is something better: the
catalogue of every pass already flown. Both satellites are held on a repeating
ground track -- Sentinel-2 retraces its own every 10 days, Sentinel-1 every 12
-- and each track is numbered, so a place is visited by the same numbered orbit
at the same local time, over and over.

So the prediction is measurement, not modelling. Ask the catalogue what has
flown over this point recently, group those passes by the orbit that took them,
work out the interval each orbit actually repeats on, and step the most recent
one forward until it lands in the future. The result inherits the real time of
day, and it degrades honestly: with only one pass on record it falls back to
the nominal repeat cycle and says so.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import requests

from . import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

# How far back to look for passes. Long enough to see a couple of repeats of
# every orbit that touches the point, short enough to stay one quick request.
LOOKBACK_DAYS = 45

# Two products from the same pass are minutes apart; that is not an interval.
MIN_INTERVAL = dt.timedelta(hours=12)


class PassLookupError(RuntimeError):
    pass


def _parse(when: str) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def _recent_passes(sat: dict, lon: float, lat: float, now: dt.datetime) -> list[dict]:
    """Every pass the catalogue holds over this point in the lookback window."""
    payload: dict[str, Any] = {
        "collections": [sat["collection"]],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{(now - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()}/"
                    f"{now.isoformat()}",
        "limit": 100,
        # Cloud is irrelevant here: a cloudy pass is still a pass, and
        # filtering them out would make the satellite look slower than it is.
        "fields": {"include": ["properties.datetime", "properties.platform",
                               "properties.sat:relative_orbit",
                               "properties.sat:orbit_state"]},
    }
    try:
        resp = _session.post(f"{config.STAC_URL}/search", json=payload, timeout=45)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except requests.RequestException as exc:
        raise PassLookupError(f"Could not reach the {sat['short']} catalogue: {exc}") from exc

    out = []
    for item in features:
        props = item.get("properties", {})
        when = _parse(props.get("datetime") or "")
        if when:
            out.append({
                "when": when,
                "orbit": props.get("sat:relative_orbit"),
                "platform": props.get("platform"),
                "orbit_state": props.get("sat:orbit_state"),
            })
    out.sort(key=lambda p: p["when"])
    return out


def _distinct(times: list[dt.datetime]) -> list[dt.datetime]:
    """One entry per pass.

    A single overflight is often filed as several products -- consecutive
    slices of the same strip, or a reprocessing -- minutes apart. Left in, they
    would drag the measured interval a few minutes short of the truth every
    cycle, so each cluster is collapsed to the moment it began.
    """
    out: list[dt.datetime] = []
    for when in times:
        if not out or when - out[-1] >= MIN_INTERVAL:
            out.append(when)
    return out


def _interval(times: list[dt.datetime], fallback: dt.timedelta) -> tuple[dt.timedelta, bool]:
    """How often this orbit comes round, from the gaps between its passes.

    The smallest gap is the period. A missed or unarchived acquisition can only
    ever make a gap bigger, never smaller, so taking the minimum is what
    survives a patchy record.
    """
    unique = _distinct(times)
    gaps = [b - a for a, b in zip(unique, unique[1:])]
    return (min(gaps), True) if gaps else (fallback, False)


def _project(last: dt.datetime, period: dt.timedelta, now: dt.datetime) -> dt.datetime:
    """Step forward by whole periods until the pass is in the future."""
    if period <= dt.timedelta(0):
        return last
    steps = int((now - last) // period) + 1
    return last + steps * period


def satellite_passes(sat: dict, lon: float, lat: float,
                     now: dt.datetime | None = None) -> dict[str, Any]:
    """The last and next pass of one satellite over one point."""
    now = now or dt.datetime.now(dt.timezone.utc)
    nominal = dt.timedelta(days=sat["repeat_days"])
    recent = _recent_passes(sat, lon, lat, now)

    if not recent:
        return {
            "satellite": sat["key"], "short": sat["short"], "kind": sat["kind"],
            "last": None, "next": None, "measured": False, "passes_seen": 0,
            "note": f"No {sat['short']} pass in the last {LOOKBACK_DAYS} days here.",
        }

    # Group by the ground track that took each pass: one track, one interval.
    tracks: dict[Any, list[dt.datetime]] = {}
    for item in recent:
        tracks.setdefault(item["orbit"], []).append(item["when"])

    candidates = []
    for orbit, times in tracks.items():
        period, measured = _interval(times, nominal)
        # Project from when the last pass began, not from a trailing slice of
        # the same strip, or the prediction drifts early.
        anchor = _distinct(times)[-1]
        candidates.append({
            "orbit": orbit,
            "when": _project(anchor, period, now),
            "period_days": round(period.total_seconds() / 86400, 2),
            "measured": measured,
            "last_seen": times[-1],
        })
    soonest = min(candidates, key=lambda c: c["when"])
    latest = recent[-1]

    return {
        "satellite": sat["key"],
        "short": sat["short"],
        "kind": sat["kind"],
        "passes_seen": len(recent),
        "orbits_seen": len(tracks),
        "measured": soonest["measured"],
        "last": {
            "datetime": latest["when"].isoformat().replace("+00:00", "Z"),
            "hours_ago": round((now - latest["when"]).total_seconds() / 3600, 1),
            "orbit": latest["orbit"],
            "platform": latest["platform"],
            "orbit_state": latest["orbit_state"],
        },
        "next": {
            "datetime": soonest["when"].isoformat().replace("+00:00", "Z"),
            "hours_away": round((soonest["when"] - now).total_seconds() / 3600, 1),
            "orbit": soonest["orbit"],
            "period_days": soonest["period_days"],
        },
    }


def next_passes(lon: float, lat: float, satellites=None,
                demo: bool | None = None) -> dict[str, Any]:
    """Both satellites' last and next pass over a point, soonest first."""
    now = dt.datetime.now(dt.timezone.utc)
    keys = [k for k in (satellites or config.SATELLITES) if k in config.SATELLITES]

    if demo if demo is not None else config.DEMO_MODE:
        found = [_demo_passes(config.satellite(k), lon, lat, now) for k in keys]
    else:
        found = [satellite_passes(config.satellite(k), lon, lat, now) for k in keys]

    found.sort(key=lambda p: p["next"]["hours_away"] if p.get("next") else 1e9)
    return {
        "point": [round(lon, 5), round(lat, 5)],
        "asked_at": now.isoformat().replace("+00:00", "Z"),
        "satellites": found,
        "lookback_days": LOOKBACK_DAYS,
    }


def _demo_passes(sat: dict, lon: float, lat: float, now: dt.datetime) -> dict:
    """A plausible pass schedule offline, on the satellite's real cadence."""
    period = dt.timedelta(days=sat["repeat_days"] / 2)
    # Anchor on the point and on the satellite, so the answer is stable, varies
    # from place to place, and does not have both satellites arriving together.
    offset = dt.timedelta(hours=(abs(lon * 7 + lat * 13 + sat["repeat_days"] * 17.3)
                                 % (period.total_seconds() / 3600)))
    last = now - offset
    nxt = last + period
    return {
        "satellite": sat["key"], "short": sat["short"], "kind": sat["kind"],
        "passes_seen": 6, "orbits_seen": 2, "measured": False, "demo": True,
        "last": {
            "datetime": last.isoformat().replace("+00:00", "Z"),
            "hours_ago": round(offset.total_seconds() / 3600, 1),
            "orbit": 59 if sat["kind"] == "radar" else 108,
            "platform": f"{sat['platform']} (synthetic)",
            "orbit_state": "ascending",
        },
        "next": {
            "datetime": nxt.isoformat().replace("+00:00", "Z"),
            "hours_away": round((nxt - now).total_seconds() / 3600, 1),
            "orbit": 59 if sat["kind"] == "radar" else 108,
            "period_days": round(period.total_seconds() / 86400, 2),
        },
    }
