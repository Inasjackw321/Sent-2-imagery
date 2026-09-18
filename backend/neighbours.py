"""The provinces of the countries Ukraine borders, so the map has two sides.

A picture of this layer used to stop at Ukraine's edge. North and west of it
the ground was black, which is not what the ground is: a Shahed corridor up
the Polesian border runs a few kilometres from Belarus, the NATO frontier is
one province away from Lviv, and a reader looking at the empty half of the
picture cannot tell whether nothing is happening there or whether the map
simply has nothing to say about it.

Ukraine's provinces arrive as a file -- NEPTUN publish one, and everything in
it is Ukrainian by definition. Nobody publishes one for Poland or Belarus, so
these are named here and their boundaries fetched one at a time from the
gazetteer, the same way Russia's arrive. That makes this module a list of
names and nothing else, which is the right shape for it: the sixteen
voivodeships and the six oblasts are closed lists that change about once a
generation, so listing them is more reliable than any amount of inference.

Two spellings each, native first.

Not belt-and-braces. The gazetteer matches OpenStreetMap's own name tags, and
which of them a region carries is not something this app can know in advance:
`name` is always there and `name:en` usually is, but "which usually" is the
kind of thing that is true for fifteen of sixteen and leaves one province as
a hole in the map. So each region may be asked for under either name and the
first boundary that comes back is the one that is used -- and a region that
answers to neither is simply absent, rather than being a gap in a country
that claims to be whole. See `whole()`.

The names on the map are the native ones, like Ukraine's and Russia's. A map
that labels Ukraine in Ukrainian and Poland in English is a map drawn from
somewhere else.
"""

from __future__ import annotations

# code, the name written on the map, the names it may be asked for under
Region = tuple[str, str, tuple[str, ...]]

POLAND: tuple[Region, ...] = (
    ("pl", "województwo dolnośląskie",
     ("województwo dolnośląskie", "Lower Silesian Voivodeship")),
    ("pl", "województwo kujawsko-pomorskie",
     ("województwo kujawsko-pomorskie", "Kuyavian-Pomeranian Voivodeship")),
    ("pl", "województwo lubelskie",
     ("województwo lubelskie", "Lublin Voivodeship")),
    ("pl", "województwo lubuskie",
     ("województwo lubuskie", "Lubusz Voivodeship")),
    ("pl", "województwo łódzkie",
     ("województwo łódzkie", "Łódź Voivodeship")),
    ("pl", "województwo małopolskie",
     ("województwo małopolskie", "Lesser Poland Voivodeship")),
    ("pl", "województwo mazowieckie",
     ("województwo mazowieckie", "Masovian Voivodeship")),
    ("pl", "województwo opolskie",
     ("województwo opolskie", "Opole Voivodeship")),
    ("pl", "województwo podkarpackie",
     ("województwo podkarpackie", "Subcarpathian Voivodeship")),
    ("pl", "województwo podlaskie",
     ("województwo podlaskie", "Podlaskie Voivodeship")),
    ("pl", "województwo pomorskie",
     ("województwo pomorskie", "Pomeranian Voivodeship")),
    ("pl", "województwo śląskie",
     ("województwo śląskie", "Silesian Voivodeship")),
    ("pl", "województwo świętokrzyskie",
     ("województwo świętokrzyskie", "Holy Cross Voivodeship")),
    ("pl", "województwo warmińsko-mazurskie",
     ("województwo warmińsko-mazurskie", "Warmian-Masurian Voivodeship")),
    ("pl", "województwo wielkopolskie",
     ("województwo wielkopolskie", "Greater Poland Voivodeship")),
    ("pl", "województwo zachodniopomorskie",
     ("województwo zachodniopomorskie", "West Pomeranian Voivodeship")),
)

BELARUS: tuple[Region, ...] = (
    ("by", "Брэсцкая вобласць", ("Брэсцкая вобласць", "Brest Region")),
    ("by", "Віцебская вобласць", ("Віцебская вобласць", "Vitebsk Region")),
    ("by", "Гомельская вобласць", ("Гомельская вобласць", "Gomel Region")),
    ("by", "Гродзенская вобласць", ("Гродзенская вобласць", "Grodno Region")),
    ("by", "Мінская вобласць", ("Мінская вобласць", "Minsk Region")),
    ("by", "Магілёўская вобласць", ("Магілёўская вобласць", "Mogilev Region")),
    # A city that is its own region, the way Kyiv is. Left out, the country
    # has a hole in the middle of it.
    ("by", "Мінск", ("Мінск", "Minsk")),
)

REGIONS: tuple[Region, ...] = POLAND + BELARUS

# Which countries this module can draw whole, and how many regions each takes.
# Read by whole(): a national border may only be drawn round a country whose
# every province is present, and this is the count that decides it.
EXPECTED = {
    "pl": len(POLAND),
    "by": len(BELARUS),
}
