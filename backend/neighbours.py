"""The countries around Ukraine, and their provinces, so the map has sides.

A picture of this layer used to stop at Ukraine's edge. North and west of it
the ground was black, which is not what the ground is: a Shahed corridor up
the Polesian border runs a few kilometres from Belarus, one up the Black Sea
coast crosses Romanian airspace, and a reader looking at the empty half of
the picture cannot tell whether nothing is happening there or whether the map
simply has nothing to say about it.

Two lists, and the difference between them is the whole design.

COUNTRIES is the national boundaries -- six names, one per country, each the
country's own outline. They are drawn in red, because they are the thing a
reader needs first: a drone four kilometres from Belarus and a drone four
kilometres from the next oblast are not the same report.

Drawn from the country's own boundary rather than worked out from its
provinces, and that is not a shortcut. Deriving a national border means
taking the outside edge of the union of the provinces, which needs every
province of the country, needs them to tile, and -- as this app found out by
drawing it -- needs every ring in somebody else's file to be wound the same
way. Ukraine's set is oblasts AND raions, two nested levels, and where the
levels disagreed about winding the union came apart: every raion line in the
country came out red and the national border did not. A border asked for as
a border has none of those problems, costs one request, and is right on the
first poll instead of the hundredth.

REGIONS is the provinces, drawn as ordinary pale boundaries inside those
national borders. They are what makes the ground read as ground rather than
as a shape, and they cost one request each -- so a name here that no
gazetteer recognises loses one pale line and nothing else. That is the point
of the split: nothing important depends on this list being perfect.

Two spellings each, native first.

Not belt-and-braces. The gazetteer matches OpenStreetMap's own name tags, and
which of them a region carries is not something this app can know in advance:
`name` is always there and `name:en` usually is, but "which usually" is the
kind of thing that is true for forty of forty-two and leaves two counties as
holes. So each may be asked for under either name and the first boundary back
is the one used -- see tracker.want_neighbours for the escalation.

The names on the map are the native ones, like Ukraine's. A map that labels
Ukraine in Ukrainian and Romania in English is a map drawn from somewhere
else.
"""

from __future__ import annotations

# code, the name written on the map, the names it may be asked for under
Region = tuple[str, str, tuple[str, ...]]

# The national borders. Ukraine is in here with the rest: the red line round
# it is the one this picture most needs and the one it did not have.
#
# Russia's own boundary is deliberately NOT in this list. It is the one
# country here whose outline is a live dispute -- OpenStreetMap draws Crimea
# inside Ukraine and Russia claims otherwise -- and a red line is the most
# emphatic thing on this picture. Russia's provinces are drawn like anybody
# else's; where the two countries meet, Ukraine's own border is the line, and
# this app is not in the business of drawing a second one over it.
COUNTRIES: tuple[Region, ...] = (
    ("ua", "Україна", ("Україна", "Ukraine")),
    ("by", "Беларусь", ("Беларусь", "Belarus")),
    ("pl", "Polska", ("Polska", "Poland")),
    ("md", "Moldova", ("Moldova", "Republica Moldova")),
    ("ro", "România", ("România", "Romania")),
)

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


def _county(native: str, english: str) -> Region:
    """One Romanian county, under both the spellings it may answer to."""
    return ("ro", f"Județul {native}",
            (f"Județul {native}", f"{english} County"))


ROMANIA: tuple[Region, ...] = (
    _county("Alba", "Alba"), _county("Arad", "Arad"),
    _county("Argeș", "Argeș"), _county("Bacău", "Bacău"),
    _county("Bihor", "Bihor"),
    _county("Bistrița-Năsăud", "Bistrița-Năsăud"),
    _county("Botoșani", "Botoșani"), _county("Brașov", "Brașov"),
    _county("Brăila", "Brăila"), _county("Buzău", "Buzău"),
    _county("Caraș-Severin", "Caraș-Severin"),
    _county("Călărași", "Călărași"), _county("Cluj", "Cluj"),
    _county("Constanța", "Constanța"), _county("Covasna", "Covasna"),
    _county("Dâmbovița", "Dâmbovița"), _county("Dolj", "Dolj"),
    _county("Galați", "Galați"), _county("Giurgiu", "Giurgiu"),
    _county("Gorj", "Gorj"), _county("Harghita", "Harghita"),
    _county("Hunedoara", "Hunedoara"), _county("Ialomița", "Ialomița"),
    _county("Iași", "Iași"), _county("Ilfov", "Ilfov"),
    _county("Maramureș", "Maramureș"), _county("Mehedinți", "Mehedinți"),
    _county("Mureș", "Mureș"), _county("Neamț", "Neamț"),
    _county("Olt", "Olt"), _county("Prahova", "Prahova"),
    _county("Satu Mare", "Satu Mare"), _county("Sălaj", "Sălaj"),
    _county("Sibiu", "Sibiu"), _county("Suceava", "Suceava"),
    _county("Teleorman", "Teleorman"), _county("Timiș", "Timiș"),
    _county("Tulcea", "Tulcea"), _county("Vaslui", "Vaslui"),
    _county("Vâlcea", "Vâlcea"), _county("Vrancea", "Vrancea"),
    # The capital is its own thing rather than a county, so it is not put
    # through _county: "Bucharest County" is not a place.
    ("ro", "Municipiul București",
     ("Municipiul București", "Bucharest")),
)


def _raion(native: str) -> Region:
    """One Moldovan raion. The English form is the bare name."""
    return ("md", f"Raionul {native}", (f"Raionul {native}", native))


MOLDOVA: tuple[Region, ...] = (
    _raion("Anenii Noi"), _raion("Basarabeasca"), _raion("Briceni"),
    _raion("Cahul"), _raion("Cantemir"), _raion("Călărași"),
    _raion("Căușeni"), _raion("Cimișlia"), _raion("Criuleni"),
    _raion("Dondușeni"), _raion("Drochia"), _raion("Dubăsari"),
    _raion("Edineț"), _raion("Fălești"), _raion("Florești"),
    _raion("Glodeni"), _raion("Hîncești"), _raion("Ialoveni"),
    _raion("Leova"), _raion("Nisporeni"), _raion("Ocnița"),
    _raion("Orhei"), _raion("Rezina"), _raion("Rîșcani"),
    _raion("Sîngerei"), _raion("Soroca"), _raion("Strășeni"),
    _raion("Șoldănești"), _raion("Ștefan Vodă"), _raion("Taraclia"),
    _raion("Telenești"), _raion("Ungheni"),
    # The two municipalities and the autonomous unit, which are regions in
    # their own right and not raions.
    ("md", "Municipiul Chișinău", ("Municipiul Chișinău", "Chișinău")),
    ("md", "Municipiul Bălți", ("Municipiul Bălți", "Bălți")),
    ("md", "Găgăuzia", ("Găgăuzia", "Gagauzia")),
    # Left out on purpose, and it is the same reason Russia has no national
    # border here: the left bank is a live dispute, and this app reports what
    # is in the air rather than settling one. It is inside Moldova's own
    # boundary either way, so the ground is on the picture.
)

# Russia's western federal subjects, and only those.
#
# Not all eighty-odd. This list exists so the ground next to Ukraine is drawn
# before any warning has been reported over it, and Kamchatka is not next to
# Ukraine -- a hundred and forty requests to draw provinces that can never be
# in frame is a cost with no reader on the other end of it. Anything further
# east still arrives the old way, one province per warning, the moment a
# warning is reported there.
#
# Crimea is not here. It is inside Ukraine's own boundary, which this app
# takes from the same source as everything else it draws.
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

REGIONS: tuple[Region, ...] = POLAND + BELARUS + ROMANIA + MOLDOVA + RUSSIA

# Every boundary this module asks for, national borders first.
#
# The order is the point. They go into the gazetteer's queue in this order at
# one request every three seconds, so asking for the five national borders
# before the hundred-odd provinces is the difference between a picture that
# has its red lines within a minute and one that has them in seven.
EVERYTHING: tuple[Region, ...] = COUNTRIES + REGIONS
