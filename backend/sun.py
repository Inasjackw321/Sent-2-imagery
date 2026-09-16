"""Where the sun is, for deciding whether a satellite pass is any use.

An optical satellite in the dark sees nothing. Sentinel-2 and Landsat are on
sun-synchronous orbits and only record on the daylight half of each orbit, so
"the next pass" and "the next pass you will get a picture from" are different
questions -- and the second is the one anybody asking has in mind.

The arithmetic is the low-precision solar position: mean longitude, the
correction for the earth's orbit not being circular, the axial tilt, and
sidereal time to say which way the planet is facing. Good to about a hundredth
of a degree, which is a rounding error against a sun half a degree wide and a
pass predicted to the nearest minute.

This is the same calculation the page's own day/night layer makes, written
again here rather than shared, because the two are in different languages and
the alternative is a round trip to the server every time the terminator moves.
The tests check them against each other so they cannot drift.
"""

from __future__ import annotations

import datetime as dt
import math

# Days from J2000.0 to the Unix epoch. Every term below is written against
# J2000, and time arrives here as a Unix timestamp.
J2000_OFFSET = 10957.5


def _days(when: dt.datetime) -> float:
    """Days since J2000.0."""
    return when.timestamp() / 86400.0 - J2000_OFFSET


def _wrap180(deg: float) -> float:
    return ((deg + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


def subsolar(when: dt.datetime) -> tuple[float, float]:
    """Where the sun is directly overhead: (latitude, longitude)."""
    d = _days(when)
    mean_lon = 280.460 + 0.9856474 * d
    anomaly = math.radians(357.528 + 0.9856003 * d)
    # The sun runs ahead of its mean position in January and behind it in
    # July; this is that correction.
    ecliptic = math.radians(mean_lon + 1.915 * math.sin(anomaly)
                            + 0.020 * math.sin(2 * anomaly))
    tilt = math.radians(23.439 - 0.0000004 * d)

    declination = math.degrees(math.asin(math.sin(tilt) * math.sin(ecliptic)))
    right_ascension = math.degrees(math.atan2(
        math.cos(tilt) * math.sin(ecliptic), math.cos(ecliptic)))

    # Sidereal time says which way the earth is facing; the difference between
    # that and the sun's right ascension is the longitude under the sun.
    sidereal = (18.697374558 + 24.06570982441908 * d) % 24
    return declination, _wrap180(right_ascension - sidereal * 15.0)


def elevation(lat: float, lon: float, when: dt.datetime) -> float:
    """How high the sun is above the horizon at a place, in degrees."""
    declination, sub_lon = subsolar(when)
    hour_angle = math.radians(lon - sub_lon)
    a = math.radians(lat)
    d = math.radians(declination)
    return math.degrees(math.asin(
        math.sin(a) * math.sin(d)
        + math.cos(a) * math.cos(d) * math.cos(hour_angle)))


# How high the sun has to be for an optical pass to be worth having.
#
# Not zero. At the horizon the light is red, the shadows are longer than the
# things casting them, and the atmosphere is several times deeper -- a
# Sentinel-2 scene at two degrees of sun elevation is technically daylight and
# is not a picture anybody wants. Five degrees is roughly the lower limit the
# agencies themselves acquire at.
WORTH_IT = 5.0


def is_daylight(lat: float, lon: float, when: dt.datetime,
                least: float = WORTH_IT) -> bool:
    """Whether an optical sensor would see anything here, then."""
    return elevation(lat, lon, when) >= least
