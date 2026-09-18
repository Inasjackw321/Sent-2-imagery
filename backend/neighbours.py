"""The countries around Ukraine, so the map has two sides.

A picture of this layer stopped at Ukraine's edge. North and west of it the
ground was black, which is not what the ground is: a Shahed corridor up the
Polesian border runs a few kilometres from Belarus, one up the Black Sea coast
crosses Romanian airspace, and a reader looking at the empty half of the
picture cannot tell whether nothing is happening there or whether the map
simply has nothing to say about it.

The national borders are SHIPPED WITH THIS APP rather than fetched, and that
is the second attempt. The first asked the gazetteer for them by name, along
with a hundred and sixteen provinces -- and a hundred and twenty-one
systematic boundary lookups is precisely the bulk querying Nominatim's usage
policy forbids. Whatever the immediate cause of the empty map was, that design
could not be the right one: a free service that asks for one request a second
and no systematic queries is not a place to go for a hundred and twenty pieces
of static reference data that change about once a generation.

So they are a file. Four countries, seventy-six kilobytes, no network, no rate
limit, right on the first poll instead of the hundredth -- and it cannot get
this app blocked from the gazetteer it genuinely needs for the one-at-a-time
lookups that place actual reports.

Ukraine and Russia are deliberately NOT in that file.

Every ready-made country dataset that could be had here -- Natural Earth by
way of world-atlas, and sane-topojson -- draws Crimea inside Russia. Checked
rather than assumed: a point at Simferopol falls inside Russia and outside
Ukraine in both. That is a live territorial dispute and the most emphatic
thing on this picture is a red line, so this app ships neither country's
outline rather than shipping that claim in either direction. Ukraine's shape
on the picture is its own oblasts, which come from NEPTUN; Russia's near side
is the provinces below.

What is left out with them is the four countries' own provinces. They were the
bulk half of the queries, they were never asked for -- the request was to see
the countries and their borders -- and with each country drawn as a named,
filled area the subdivisions inside it earn very little. Russia's western
oblasts stay, because nothing else draws that ground until a warning is
reported over it, and fourteen occasional lookups is ordinary use rather than
bulk.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

# code, the name written on the map, the names it may be asked for under
Region = tuple[str, str, tuple[str, ...]]

# Natural Earth 1:10m country boundaries, by way of the world-atlas package.
# Natural Earth is public domain. Trimmed to the four countries wanted, with
# coordinates rounded to four decimal places -- about eleven metres, against a
# border drawn at a scale where a pixel is most of a kilometre.
FRONTIERS_FILE = pathlib.Path(__file__).with_name("data") / "frontiers.json"

_frontiers: dict[str, dict[str, Any]] | None = None


def frontiers() -> dict[str, dict[str, Any]]:
    """The shipped national borders, as {code: {"name", "shape"}}.

    Read once and kept. A missing or unreadable file is an empty answer
    rather than an exception: the picture without its red lines is the
    picture this app drew for months, and a border that cannot be read is not
    a reason to fail to draw the marks.
    """
    global _frontiers
    if _frontiers is None:
        try:
            with FRONTIERS_FILE.open(encoding="utf-8") as fh:
                got = json.load(fh)
            _frontiers = {code: row for code, row in got.items()
                          if isinstance(row, dict) and row.get("shape")}
        except (OSError, ValueError):
            _frontiers = {}
    return _frontiers


def forget() -> None:
    """Drop the cached files. For the tests."""
    global _frontiers, _provinces, _by_name
    _frontiers = None
    _provinces = None
    _by_name = None


# The provinces themselves, with their boundaries.
#
# This is what makes a Russian warning look like a Ukrainian one. Ukraine's
# warnings shade their oblast because NEPTUN publish the boundaries; Russia's
# had none, so the same warning came out as a triangle on a province's
# arithmetic centre -- the difference between the two halves of the map was
# never about the warnings, it was about whether an outline existed.
#
# They used to be fetched, one name at a time, which is the bulk querying the
# note above describes and which never arrived. Natural Earth publishes them
# and Natural Earth is public domain: fifty regions, the ones the built-in
# table can name, thinned to about two hundred and sixty points each and
# rounded to four decimal places. 230 KB, no network, right on the first poll.
#
# Checked rather than trusted: every one of them contains its own capital.
PROVINCES_FILE = pathlib.Path(__file__).with_name("data") / "provinces.json"

_provinces: dict[str, dict[str, Any]] | None = None


def provinces() -> dict[str, dict[str, Any]]:
    """The shipped province boundaries, as {name: {"in", "shape"}}."""
    global _provinces
    if _provinces is None:
        try:
            with PROVINCES_FILE.open(encoding="utf-8") as fh:
                got = json.load(fh)
            _provinces = {name: row for name, row in got.items()
                          if isinstance(row, dict) and row.get("shape")}
        except (OSError, ValueError):
            _provinces = {}
    return _provinces


def _fold(name: Any) -> str:
    return " ".join(str(name or "").lower().split())


_by_name: dict[str, str] | None = None


def shape_for(name: Any) -> Any | None:
    """A shipped province boundary by name, or None.

    The same question neptun.shape_for answers for Ukraine, answered for the
    other side of the border from a file instead of a published one.
    """
    global _by_name
    got = provinces()
    if _by_name is None:
        _by_name = {_fold(real): real for real in got}
    real = _by_name.get(_fold(name))
    return got[real]["shape"] if real else None


# Nothing is fetched by name any more.
#
# There was a list of Russia's western federal subjects here, asked for one
# at a time so the ground next to Ukraine was drawn before a warning reached
# it. The file above does that for fifty regions at once, with real
# boundaries rather than centres, so the list had nothing left to do.
#
# What still goes to the gazetteer is a region this app cannot name at all --
# somewhere east of the file, mentioned in a report for the first time. One
# lookup, when a warning actually arrives, is ordinary use of a free service
# rather than the systematic querying its policy forbids.
