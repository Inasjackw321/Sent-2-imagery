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
    """Drop the cached file. For the tests."""
    global _frontiers
    _frontiers = None


# Russia's western federal subjects, and only those.
#
# Not all eighty-odd, and not a stand-in for a border: this is the ground next
# to Ukraine, drawn before any warning has been reported over it. Kamchatka is
# not next to Ukraine, and a hundred and forty lookups to draw provinces that
# can never be in frame is exactly the bulk querying this module stopped
# doing. Anything further east still arrives the old way, one province per
# warning, the moment one is reported there.
#
# Crimea is not here, for the reason in the module note.
#
# Two spellings each, native first. The gazetteer matches OpenStreetMap's own
# name tags and which of them a region carries is not something this app can
# know in advance, so each may be asked for under either and the first
# boundary back is the one used -- see tracker.want_neighbours.
RUSSIA: tuple[Region, ...] = (
    ("ru", "Брянская область", ("Брянская область", "Bryansk Oblast")),
    ("ru", "Курская область", ("Курская область", "Kursk Oblast")),
    ("ru", "Белгородская область",
     ("Белгородская область", "Belgorod Oblast")),
    ("ru", "Воронежская область",
     ("Воронежская область", "Voronezh Oblast")),
    ("ru", "Ростовская область", ("Ростовская область", "Rostov Oblast")),
    ("ru", "Краснодарский край",
     ("Краснодарский край", "Krasnodar Krai")),
    ("ru", "Смоленская область", ("Смоленская область", "Smolensk Oblast")),
    ("ru", "Орловская область", ("Орловская область", "Oryol Oblast")),
    ("ru", "Липецкая область", ("Липецкая область", "Lipetsk Oblast")),
    ("ru", "Тамбовская область", ("Тамбовская область", "Tambov Oblast")),
    ("ru", "Волгоградская область",
     ("Волгоградская область", "Volgograd Oblast")),
    ("ru", "Калужская область", ("Калужская область", "Kaluga Oblast")),
    ("ru", "Тульская область", ("Тульская область", "Tula Oblast")),
    ("ru", "Республика Адыгея", ("Республика Адыгея", "Republic of Adygea")),
)

# Everything this module asks the gazetteer for. The borders are not in it:
# they are a file, which is the whole point of the change that put them there.
REGIONS: tuple[Region, ...] = RUSSIA
EVERYTHING: tuple[Region, ...] = REGIONS
