"""NASA FIRMS active fire detections.

The live service cannot be reached from a test run, so what is stood in here is
its output: real FIRMS CSV, byte for byte in the shape both sensors publish it.
That is where the awkwardness lives -- VIIRS and MODIS disagree about what the
columns are called and how confidence is expressed -- so that is what is
tested, along with the rules that keep the map usable: the bounding box, the
time window, and the cap on how many detections come back.

    python -m pytest tests/test_fires.py -q
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import fires  # noqa: E402


def _stamp(hours_ago: float) -> tuple[str, str]:
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%d"), when.strftime("%H%M")


# The header FIRMS actually publishes for the VIIRS C2 archive files.
VIIRS_HEADER = ("country_id,latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,"
                "satellite,instrument,confidence,version,bright_ti5,frp,daynight")
MODIS_HEADER = ("latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,"
                "instrument,confidence,version,bright_t31,frp,daynight")


def viirs_csv(rows) -> str:
    lines = [VIIRS_HEADER]
    for lat, lon, conf, frp, ago in rows:
        date, time = _stamp(ago)
        lines.append(f"AUS,{lat},{lon},345.2,0.42,0.37,{date},{time},N,VIIRS,{conf},"
                     f"2.0NRT,295.1,{frp},D")
    return "\n".join(lines) + "\n"


def modis_csv(rows) -> str:
    lines = [MODIS_HEADER]
    for lat, lon, conf, frp, ago in rows:
        date, time = _stamp(ago)
        lines.append(f"{lat},{lon},330.9,1.1,1.0,{date},{time},Terra,MODIS,{conf},"
                     f"6.1NRT,290.4,{frp},N")
    return "\n".join(lines) + "\n"


# ── Parsing what the two sensors publish ───────────────────────


def test_a_viirs_row_becomes_a_detection():
    found = fires._parse(viirs_csv([(-33.8, 150.9, "h", "42.7", 3)]), "viirs-snpp")
    assert len(found) == 1
    fire = found[0]
    assert (fire["lat"], fire["lon"]) == (-33.8, 150.9)
    assert fire["frp"] == 42.7
    assert fire["brightness_k"] == 345.2          # VIIRS calls it bright_ti4
    assert fire["confidence_label"] == "high"
    assert fire["resolution_m"] == 375
    assert fire["day"] is True


def test_a_modis_row_becomes_the_same_kind_of_detection():
    found = fires._parse(modis_csv([(-33.8, 150.9, "78", "12.0", 2)]), "modis")
    fire = found[0]
    assert fire["brightness_k"] == 330.9          # MODIS just calls it brightness
    assert fire["resolution_m"] == 1000
    assert fire["day"] is False
    # A percentage and a word have to end up on one scale.
    assert fire["confidence"] == 0.78
    assert fire["confidence_label"] == "nominal"


def test_the_two_confidence_scales_agree_about_what_is_strong():
    assert fires._confidence("l", "viirs-snpp")[1] == "low"
    assert fires._confidence("n", "viirs-snpp")[1] == "nominal"
    assert fires._confidence("h", "viirs-snpp")[1] == "high"
    assert fires._confidence("10", "modis")[1] == "low"
    assert fires._confidence("95", "modis")[1] == "high"
    # Anything unexpected is treated as ordinary rather than thrown away.
    assert fires._confidence("", "modis")[1] == "nominal"
    assert fires._confidence("banana", "modis")[1] == "nominal"


def test_the_split_timestamp_is_put_back_together():
    assert fires._acquired("2024-08-15", "0742") == "2024-08-15T07:42:00Z"
    # FIRMS drops the leading zero on times before 10:00 UTC.
    assert fires._acquired("2024-08-15", "742") == "2024-08-15T07:42:00Z"
    assert fires._acquired("2024-08-15", "") == "2024-08-15T00:00:00Z"


def test_a_row_with_no_position_is_dropped_not_crashed_on():
    broken = VIIRS_HEADER + "\nAUS,,,345.2,0.4,0.3,2024-08-15,0742,N,VIIRS,h,2,295,4,D\n"
    assert fires._parse(broken, "viirs-snpp") == []


# ── Which detections come back ─────────────────────────────────


@pytest.fixture
def served(monkeypatch):
    """Stand in for the FIRMS archive with a CSV of our own."""
    def serve(rows, sensor="viirs-snpp"):
        text = viirs_csv(rows)
        monkeypatch.setattr(fires, "MAP_KEY", "")
        monkeypatch.setattr(fires, "_fetch", lambda url, what: text)
        fires._cache.clear()
    return serve


def test_only_the_fires_inside_the_rectangle_come_back(served):
    served([(-33.8, 150.9, "h", "40", 2),      # inside
            (-33.9, 151.1, "n", "12", 4),      # inside
            (-20.0, 120.0, "h", "99", 1)])     # a long way outside
    out = fires.active_fires((150.5, -34.2, 151.4, -33.5), hours=24,
                             sensors=["viirs-snpp"], demo=False)
    assert out["count"] == 2
    assert all(150.5 <= f["lon"] <= 151.4 for f in out["fires"])


def test_a_rectangle_given_backwards_still_works(served):
    served([(-33.8, 150.9, "h", "40", 2)])
    out = fires.active_fires((151.4, -33.5, 150.5, -34.2), hours=24,
                             sensors=["viirs-snpp"], demo=False)
    assert out["count"] == 1
    assert out["bbox"] == [150.5, -34.2, 151.4, -33.5]


def test_detections_older_than_the_window_are_left_out(served):
    served([(-33.8, 150.9, "h", "40", 2), (-33.8, 150.9, "h", "40", 40)])
    box = (150.5, -34.2, 151.4, -33.5)
    assert fires.active_fires(box, hours=24, sensors=["viirs-snpp"], demo=False)["count"] == 1
    assert fires.active_fires(box, hours=48, sensors=["viirs-snpp"], demo=False)["count"] == 2


def test_the_file_fetched_always_covers_the_hours_asked_for():
    assert fires._window(24) == (24, "24h")
    assert fires._window(1) == (24, "24h")
    # 36 hours needs the 48 hour file: taking the nearer one would drop half a
    # day of fires without saying so.
    assert fires._window(36) == (48, "48h")
    assert fires._window(168) == (168, "7d")
    assert fires._window(300) == (168, "7d")      # clamped to the longest


def test_an_odd_window_is_covered_then_trimmed_exactly(served):
    """36 hours means 36 hours, not the 48 that had to be downloaded."""
    served([(-33.8, 150.9, "h", "40", 10), (-33.8, 150.91, "h", "40", 40)])
    out = fires.active_fires((150.5, -34.2, 151.4, -33.5), hours=36,
                             sensors=["viirs-snpp"], demo=False)
    assert out["hours"] == 36
    assert out["count"] == 1


def test_too_many_fires_keeps_the_fiercest_and_says_so(served, monkeypatch):
    monkeypatch.setattr(fires, "MAX_RETURNED", 5)
    served([(-33.8, 150.9 + i * 0.001, "h", str(i), 1) for i in range(40)])
    out = fires.active_fires((150.5, -34.2, 151.4, -33.5), hours=24,
                             sensors=["viirs-snpp"], demo=False)
    assert out["count"] == 5
    assert out["total"] == 40
    assert out["capped"] is True
    # The five biggest, not five at random.
    assert sorted(f["frp"] for f in out["fires"]) == [35, 36, 37, 38, 39]


def test_the_result_is_newest_first(served):
    served([(-33.8, 150.9, "h", "5", 8), (-33.8, 150.91, "h", "5", 1),
            (-33.8, 150.92, "h", "5", 4)])
    out = fires.active_fires((150.5, -34.2, 151.4, -33.5), hours=24,
                             sensors=["viirs-snpp"], demo=False)
    stamps = [f["acquired"] for f in out["fires"]]
    assert stamps == sorted(stamps, reverse=True)


def test_the_global_file_is_fetched_once_and_held(monkeypatch):
    calls = []
    monkeypatch.setattr(fires, "MAP_KEY", "")
    monkeypatch.setattr(fires, "_fetch",
                        lambda url, what: calls.append(url) or viirs_csv([]))
    fires._cache.clear()
    box = (150.5, -34.2, 151.4, -33.5)
    for _ in range(3):
        fires.active_fires(box, hours=24, sensors=["viirs-snpp"], demo=False)
    assert len(calls) == 1, "the whole planet must not be downloaded per pan"


def test_a_map_key_asks_only_for_the_rectangle(monkeypatch):
    seen = []
    monkeypatch.setattr(fires, "MAP_KEY", "abc123")
    monkeypatch.setattr(fires, "_fetch", lambda url, what: seen.append(url) or viirs_csv([]))
    fires._cache.clear()
    out = fires.active_fires((150.5, -34.2, 151.4, -33.5), hours=48,
                             sensors=["viirs-snpp"], demo=False)
    assert out["keyed"] is True
    assert "abc123" in seen[0] and "VIIRS_SNPP_NRT" in seen[0]
    assert "150.5000,-34.2000,151.4000,-33.5000" in seen[0]


def test_a_refusal_from_firms_is_reported_not_parsed(monkeypatch):
    monkeypatch.setattr(fires, "MAP_KEY", "")
    monkeypatch.setattr(fires, "_fetch", fires._fetch)
    monkeypatch.setattr(fires._session, "get", _raise)
    fires._cache.clear()
    with pytest.raises(fires.FireLookupError, match="Could not reach NASA FIRMS"):
        fires.active_fires((0, 0, 1, 1), hours=24, sensors=["viirs-snpp"], demo=False)


def _raise(*args, **kwargs):
    import requests

    raise requests.RequestException("no route to host")


def test_an_invalid_key_is_not_read_as_fire_data(monkeypatch):
    monkeypatch.setattr(fires, "MAP_KEY", "nope")
    monkeypatch.setattr(fires._session, "get",
                        lambda *a, **k: _Response("Invalid MAP_KEY."))
    fires._cache.clear()
    with pytest.raises(fires.FireLookupError, match="rejected"):
        fires.active_fires((0, 0, 1, 1), hours=24, sensors=["viirs-snpp"], demo=False)


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


# ── Offline ────────────────────────────────────────────────────


def test_the_offline_layer_puts_fires_inside_the_view():
    box = (150.5, -34.2, 151.4, -33.5)
    out = fires.active_fires(box, hours=24, demo=True)
    assert out["count"] > 0
    for fire in out["fires"]:
        assert box[0] <= fire["lon"] <= box[2]
        assert box[1] <= fire["lat"] <= box[3]
        assert fire["frp"] > 0
        assert fire["confidence_label"] in ("low", "nominal", "high")
        assert fire["demo"] is True
