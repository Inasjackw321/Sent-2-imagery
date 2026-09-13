"""Tests for the built-in place table.

This table exists for one reason, and it is a number. Nominatim answers one
request a second by policy, and a night's worth of reports from four channels
names the same forty or so places over and over: the oblasts, the big cities,
the front-line towns. Placing thirty-six reports took fourteen and a half
seconds, nearly all of it waiting on a rate limiter for answers that do not
change. With the table it takes none -- the measured poll went from 10.9s to
0.36s, of which 0.35s is the network fetch of the posts themselves.

That makes the table a cache with no invalidation, which is only safe because
of what is in it: oblast centres and major cities, whose coordinates have not
moved and will not. So what these tests hold is that it stays that kind of
table -- every entry somewhere plausible, every alias pointing at something
real, and no name written twice with two different answers.
"""

from __future__ import annotations

from backend import places

# Ukraine, Belarus and Russia west of the Urals, generously. Anything outside
# this is a typo or a swapped pair of coordinates, which is the failure mode
# that matters: a marker in the Atlantic reads as a bug in the map, not in a
# dict.
#
# Widened east once the Russian radar channel was added: it warns for
# Bashkortostan and Kurgan, which are past the old 49E edge, and a bound that
# excludes places the app legitimately reports on is a bound that will get
# loosened carelessly the next time rather than deliberately this time.
SOUTH, NORTH = 42.0, 66.0
WEST, EAST = 21.0, 66.0


class TestEveryEntryIsSomewhereReal:
    def test_the_tables_are_not_empty(self):
        # A table that silently emptied would make every test below vacuous
        # and every lookup fall through to Nominatim, which is exactly the
        # slowness this was built to remove -- and it would look like working.
        counted = places.count()
        assert counted["cities"] >= 140
        assert counted["regions"] >= 40
        assert counted["names"] >= 300

    def test_every_coordinate_is_in_the_region_the_app_covers(self):
        for table in (places.CITIES, places.REGIONS):
            for name, (lat, lon) in table.items():
                assert SOUTH <= lat <= NORTH, f"{name} at {lat}"
                assert WEST <= lon <= EAST, f"{name} at {lon}"

    def test_no_two_places_share_a_position(self):
        """Catches the copy-paste, which is how a table like this goes wrong.

        Adding a row by duplicating the one above it and changing only the
        name leaves two cities on one spot. Nothing errors; the map just
        quietly puts Zhytomyr's drones over Vinnytsia.
        """
        seen: dict[tuple[float, float], str] = {}
        for name, where in {**places.CITIES, **places.REGIONS}.items():
            clash = seen.get(where)
            assert clash is None, f"{name} and {clash} are both at {where}"
            seen[where] = name

    def test_no_city_is_also_a_region(self):
        # They carry different radii and the region ones get an outline drawn
        # round them. "Київ" the city and "Київська область" the oblast are
        # both here on purpose and are different strings; the same string in
        # both tables would mean one shadows the other by dict order.
        assert not set(places.CITIES) & set(places.REGIONS)


class TestEveryAliasResolves:
    def test_an_alias_points_at_a_name_the_table_holds(self):
        for spelling, target in places.ALIASES.items():
            assert target in places.CITIES or target in places.REGIONS, (
                f"{spelling} -> {target}, which is in neither table")

    def test_an_alias_is_not_also_a_name(self):
        # "Суми" as an alias of something else, while also being a city, would
        # make which one wins depend on the order _index() happens to build in.
        for spelling in places.ALIASES:
            assert spelling not in places.CITIES, spelling
            assert spelling not in places.REGIONS, spelling

    def test_an_alias_answers_with_its_targets_position(self):
        got = places.lookup("Киев")
        assert got is not None
        assert got["name"] == "Київ"
        assert (got["lat"], got["lon"]) == places.CITIES["Київ"]


class TestTheFormsThatActuallyArrive:
    """The case endings these channels write, not the dictionary forms.

    Posts say "у Кременчуці", "над Ужгородом", "в Харкові" -- never the
    nominative. Each of these missing from the table was a Nominatim request
    and, for the ones the de-inflection rules then mangled, a request that
    came back with nothing.
    """

    def test_the_locative_and_instrumental_of_the_big_cities(self):
        for written, wanted in (
            ("Кременчуці", "Кременчук"), ("Ужгородом", "Ужгород"),
            ("Харкові", "Харків"), ("Києві", "Київ"), ("Одесі", "Одеса"),
            ("Дніпрі", "Дніпро"), ("Львові", "Львів"),
        ):
            got = places.lookup(written)
            assert got is not None, written
            assert got["name"] == wanted, written

    def test_the_one_word_oblast_forms(self):
        for written, wanted in (("Сумщина", "Сумська область"),
                                ("Харківщина", "Харківська область"),
                                ("Полтавщина", "Полтавська область")):
            got = places.lookup(written)
            assert got is not None, written
            assert got["name"] == wanted, written

    def test_the_russian_spelling_of_a_ukrainian_city(self):
        # Two of the four channels write Russian. "Киев" and "Харьков" are what
        # they say, and neither is what OpenStreetMap holds.
        for written, wanted in (("Киев", "Київ"), ("Харьков", "Харків"),
                                ("Одесса", "Одеса"), ("Львов", "Львів")):
            got = places.lookup(written)
            assert got is not None, written
            assert got["name"] == wanted, written

    def test_an_apostrophe_matches_however_it_is_typed(self):
        # These posts use three different apostrophes and OpenStreetMap a
        # fourth, so the table folds all of them to one.
        first = places.lookup("Слов'янськ")
        if first is None:
            return  # not in the table; nothing to hold
        for odd in ("’", "ʼ", "`", "´"):
            assert places.lookup(f"Слов{odd}янськ") == first, odd


class TestWhatALookupHandsBack:
    def test_a_city_comes_back_as_a_place_and_a_region_as_a_boundary(self):
        # tracker._look() branches on this: a boundary gets its real outline
        # fetched in the background, a place does not, because a strike in a
        # town is a point in it and not the whole municipal border.
        assert places.lookup("Київ")["category"] == "place"
        assert places.lookup("Сумська область")["category"] == "boundary"

    def test_a_region_covers_far_more_ground_than_a_city(self):
        wide = places.lookup("Сумська область")["bbox"]
        small = places.lookup("Суми")["bbox"]
        assert (wide[1] - wide[0]) > (small[1] - small[0]) * 5

    def test_an_answer_is_marked_as_built_in(self):
        # The panel says how many places were found without a request, and
        # gazetteer.find() uses this to know a learned outline should win.
        assert places.lookup("Київ")["built_in"] is True
        assert places.lookup("Київ")["shape"] is None

    def test_the_caller_cannot_edit_the_table_through_its_answer(self):
        got = places.lookup("Київ")
        got["lat"] = 0.0
        assert places.lookup("Київ")["lat"] != 0.0

    def test_a_name_nobody_has_heard_of_is_a_miss_not_a_guess(self):
        for absurd in ("", None, "   ", "Springfield", "Обоян", "42"):
            assert places.lookup(absurd) is None, absurd


class TestRegionsAreDrawnTheSizeTheyAre:
    """A republic is not an oblast, and drawing one as one understates it.

    Bashkortostan is about the area of Britain. At OBLAST_HALF a warning for
    it covers roughly a tenth of the ground it actually covers, which reads as
    "somewhere around here" when what was said is "everywhere in this".
    """

    def test_the_big_ones_are_drawn_bigger(self):
        for name in ("Республика Башкортостан", "Республика Коми",
                     "Свердловская область", "Оренбургская область"):
            got = places.lookup(name)
            assert got is not None, name
            tall = got["bbox"][1] - got["bbox"][0]
            assert tall > places.OBLAST_HALF * 2, (name, tall)

    def test_an_ordinary_oblast_is_still_an_oblast(self):
        got = places.lookup("Сумська область")
        tall = got["bbox"][1] - got["bbox"][0]
        assert abs(tall - places.OBLAST_HALF * 2) < 0.01

    def test_every_wide_entry_names_a_region_that_exists(self):
        # A typo here is silent: the override simply never applies and the
        # region goes on being drawn at an oblast's size.
        for name in places.WIDE:
            assert name in places.REGIONS, name

    def test_a_wide_region_reached_through_its_english_name_too(self):
        # Which is how it arrives, since that channel posts in English.
        got = places.lookup("Bashkortostan republic")
        assert got["bbox"][1] - got["bbox"][0] > places.OBLAST_HALF * 2
