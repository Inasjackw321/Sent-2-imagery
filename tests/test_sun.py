"""Tests for where the sun is.

The reason this exists: an optical satellite in the dark records nothing, so
"the next pass" and "the next pass you will get a picture from" are different
questions, and only the sun can tell them apart.

Checked against facts about the solar system rather than against a golden
file, and against the page's own copy of the same arithmetic, which is the
thing most likely to drift.
"""

from __future__ import annotations

import datetime as dt
import math

from backend import sun

UTC = dt.timezone.utc


def at(y, mo, d, h=12, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


class TestWhereTheSunIs:
    def test_the_solstices_put_it_over_the_tropics(self):
        # 23.44 degrees is the earth's axial tilt, which is what the tropics
        # are the latitude of.
        assert abs(sun.subsolar(at(2026, 6, 21))[0] - 23.44) < 0.1
        assert abs(sun.subsolar(at(2026, 12, 21))[0] + 23.44) < 0.1

    def test_and_the_equinoxes_put_it_over_the_equator(self):
        for when in (at(2026, 3, 20), at(2026, 9, 23)):
            assert abs(sun.subsolar(when)[0]) < 0.6, when

    def test_it_is_overhead_at_the_subsolar_point(self):
        when = at(2026, 5, 4, 9, 17)
        lat, lon = sun.subsolar(when)
        assert abs(sun.elevation(lat, lon, when) - 90) < 0.05

    def test_and_underfoot_on_the_far_side(self):
        when = at(2026, 5, 4, 9, 17)
        lat, lon = sun.subsolar(when)
        assert sun.elevation(-lat, lon + 180, when) < -89.9

    def test_the_subsolar_point_travels_west(self):
        # Fifteen degrees an hour, because the earth turns once a day.
        when = at(2026, 7, 2, 6)
        first = sun.subsolar(when)[1]
        later = sun.subsolar(when + dt.timedelta(hours=1))[1]
        moved = (first - later) % 360
        assert abs(moved - 15) < 0.2, moved

    def test_midday_is_light_and_midnight_is_dark(self):
        # Kyiv, in September: the sun is up in the morning and down at night.
        assert sun.elevation(50.45, 30.52, at(2026, 9, 16, 9)) > 20
        assert sun.elevation(50.45, 30.52, at(2026, 9, 16, 22)) < -20

    def test_the_arctic_sun_does_not_set_in_june(self):
        for hour in range(0, 24, 3):
            assert sun.elevation(78.0, 15.0, at(2026, 6, 21, hour)) > 0, hour

    def test_nor_rise_in_december(self):
        for hour in range(0, 24, 3):
            assert sun.elevation(78.0, 15.0, at(2026, 12, 21, hour)) < 0, hour


class TestWhetherAPassIsWorthHaving:
    def test_the_horizon_is_not_the_threshold(self):
        """At two degrees of elevation the light is red, the shadows are
        longer than the things casting them, and the air is several times
        deeper. That is technically daylight and is not a picture.
        """
        assert sun.WORTH_IT > 0

    def test_a_sun_just_above_the_horizon_is_not_enough(self, monkeypatch):
        monkeypatch.setattr(sun, "elevation", lambda *a, **k: 2.0)
        assert sun.is_daylight(0, 0, at(2026, 1, 1)) is False

    def test_and_a_sun_well_up_is(self, monkeypatch):
        monkeypatch.setattr(sun, "elevation", lambda *a, **k: 30.0)
        assert sun.is_daylight(0, 0, at(2026, 1, 1)) is True

    def test_the_threshold_can_be_asked_for(self):
        # A caller that wants civil twilight rather than a usable picture.
        # Kyiv at 04:00Z in September: the sun is 3.2 degrees up -- past
        # civil twilight, short of a usable picture.
        when = at(2026, 9, 16, 4, 0)
        assert sun.is_daylight(50.45, 30.52, when, least=-6) is True
        assert sun.is_daylight(50.45, 30.52, when, least=5) is False


class TestItAgreesWithThePage:
    """The page has the same arithmetic in JavaScript, for its day/night
    layer. Two copies of one calculation is the arrangement most likely to
    drift, so they are checked against each other rather than trusted.
    """

    def test_the_two_agree_on_the_sun(self):
        import json
        import subprocess
        import pathlib

        here = pathlib.Path(__file__).resolve().parent.parent
        moments = [
            (50.45, 30.52, "2026-09-16T09:00:00Z"),
            (-33.87, 151.21, "2026-01-15T03:30:00Z"),
            (78.00, 15.00, "2026-06-21T02:00:00Z"),
            (0.00, -60.00, "2026-03-20T17:45:00Z"),
        ]
        script = (
            "import { elevation } from './frontend/js/sun.js';"
            f"const m = {json.dumps(moments)};"
            "process.stdout.write(JSON.stringify("
            "m.map(([lat, lon, iso]) => elevation(lat, lon, new Date(iso)))));"
        )
        out = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=here, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        theirs = json.loads(out.stdout)
        for (lat, lon, iso), said in zip(moments, theirs):
            when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            mine = sun.elevation(lat, lon, when)
            assert abs(mine - said) < 0.01, (lat, lon, iso, mine, said)
