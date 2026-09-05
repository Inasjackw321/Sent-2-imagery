"""Tests for reading air-threat reports without a language model.

This exists because the model is a free tier with a daily ceiling, and when
that ceiling was reached the whole layer went dark: an empty map and a line
saying OpenRouter was rate limiting, which from the outside is exactly what a
broken feature looks like.

These reports barely need a model. They are written to be scanned during an
air raid and they are formulaic to the point of being a grammar. So the tests
here are the grammar: the shapes that actually appear in these channels, and
-- just as important -- the things that look like reports and are not.
"""

from __future__ import annotations

from backend import reports


def read(text):
    return reports.read(text)


class TestWhatKindOfThing:
    def test_the_ordinary_ones(self):
        assert reports.find_kind("БпЛА курсом на захід") == "drone"
        assert reports.find_kind("Шахед над містом") == "drone"
        assert reports.find_kind("Вибухи в Одесі") == "explosion"
        assert reports.find_kind("Повітряна тривога") == "alert"

    def test_a_jet_drone_is_not_a_propeller_one(self):
        # "реактивний БпЛА" contains "БпЛА", so the narrow reading has to be
        # tried first or every jet drone is filed as an ordinary one -- and
        # they differ by a factor of three in speed.
        assert reports.find_kind("реактивний БпЛА повз Кагарлик") == "jet_drone"
        assert reports.find_kind("Шахед-238") == "jet_drone"

    def test_reconnaissance_is_told_from_attack(self):
        # The whole difference between circling and flying across a country.
        for text in ("Розвідувальний БпЛА", "Орлан-10 над районом",
                     "ZALA у повітрі", "борт-розвідник"):
            assert reports.find_kind(text) == "recon", text

    def test_missiles_are_told_apart(self):
        assert reports.find_kind("Балістика на Дніпропетровщині") == "ballistic"
        assert reports.find_kind("балістичного озброєння") == "ballistic"
        assert reports.find_kind("Крилаті ракети") == "cruise"
        assert reports.find_kind("Калібри з моря") == "cruise"

    def test_something_being_shot_down_is_the_striking_not_the_thing(self):
        # "Збито БпЛА над Києвом" is an interception. Filing it as a drone
        # would put a marker in the air for something that is on the ground.
        assert reports.find_kind("Збито БпЛА над Києвом") == "explosion"
        assert reports.find_kind("Уражено ціль") == "explosion"

    def test_a_post_about_nothing_is_nothing(self):
        assert reports.find_kind("Підписуйтесь на наш канал") == "unknown"


class TestWhichWay:
    def test_the_compass_in_ukrainian(self):
        assert reports.find_course("на північ") == "N"
        assert reports.find_course("на південь") == "S"
        assert reports.find_course("на схід") == "E"
        assert reports.find_course("на захід") == "W"

    def test_the_diagonals_are_not_read_as_their_first_half(self):
        # "південний захід" starts with "південн", so a naive order files
        # every south-west as a south -- 45 degrees out, every time.
        assert reports.find_course("на південний захід") == "SW"
        assert reports.find_course("на північно-східному напрямку") == "NE"
        assert reports.find_course("на північний захід") == "NW"
        assert reports.find_course("на південно-східний") == "SE"

    def test_a_course_and_a_destination_are_told_apart(self):
        course, toward = reports.find_heading("БпЛА курсом на північ")
        assert (course, toward) == ("N", None)
        course, toward = reports.find_heading("БпЛА курсом на Кременчук")
        assert course is None and toward == "Кременчук"

    def test_the_other_ways_of_saying_it(self):
        assert reports.find_heading("у напрямку Полтави")[1] is not None
        assert reports.find_heading("рухаються на південь")[0] == "S"

    def test_no_direction_is_no_direction(self):
        assert reports.find_heading("Вибухи в Одесі") == (None, None)


class TestWhere:
    def test_the_oblast_nicknames(self):
        # A closed list of twenty-four, in a large fraction of reports, and
        # not something to make a model guess at.
        assert reports.find_region("БпЛА на Київщині") == "Київська область"
        assert reports.find_region("Донеччина: вибухи") == "Донецька область"
        assert reports.find_region("над Сумщиною") == "Сумська область"
        assert reports.find_region("Прикарпаття") == "Івано-Франківська область"

    def test_the_oblast_written_out(self):
        assert reports.find_region("тривога в Харківській області") == "Харківська область"

    def test_no_oblast_is_none(self):
        assert reports.find_region("Вибухи в Одесі") is None

    def test_a_named_town_beats_the_oblast_it_is_in(self):
        # "БпЛА повз Кагарлик" over Kyiv oblast is a marker on Kaharlyk, not
        # one in the middle of the region.
        got = read("Київщина: БпЛА повз Кагарлик курсом на північ")
        assert got["place"] == "Кагарлик"
        assert got["region"] == "Київська область"

    def test_the_oblast_is_the_fallback_and_a_good_one(self):
        got = read("3х БпЛА на Сумщині курсом на південь")
        assert got["place"] == "Сумська область"

    def test_an_inflected_oblast_becomes_the_name_a_gazetteer_knows(self):
        # "над Сумщиною" is the oblast in yet another ending. Sending
        # "Сумщиною" to Nominatim gets nothing back.
        assert read("Орлан над Сумщиною")["place"] == "Сумська область"

    def test_a_locative_town_is_put_back_in_the_nominative(self):
        assert read("Вибухи в Одесі")["place"] == "Одеса"

    def test_the_sea_is_not_a_place_a_marker_goes(self):
        # The gazetteer would refuse the water anyway, but "Чорним" should not
        # reach the alert list either.
        assert read("Розвідувальний БпЛА над Чорним морем") is None


class TestEnglish:
    """Several of these channels post in English, natively or translated.

    The first version read only Cyrillic, so a night of English posts gave an
    empty map with nothing to explain it -- which is the failure this whole
    module exists to stop making, arrived at from a different direction.

    The strings here are real ones, taken off the channels.
    """

    def test_the_posts_from_the_screenshot(self):
        cases = [
            ("Belgorod district, danger for UAVs/FPVs again", "alert", "Belgorod district"),
            ("UAV detection in Novy Olshanets, Belgorod District", "drone", "Novy Olshanets"),
            ("Air alert in Kharkiv Oblast", "alert", "Kharkiv oblast"),
            ("Explosions reported in Odesa", "explosion", "Odesa"),
        ]
        for text, kind, place in cases:
            got = read(text)
            assert got is not None, text
            assert got["kind"] == kind, text
            assert got["place"] == place, text

    def test_oblast_is_not_read_as_a_blast(self):
        # "blast" is inside "O-blast". Without a word boundary every report
        # naming a Ukrainian region was filed as an explosion -- which is a
        # marker of the wrong shape, the wrong colour and the wrong lifetime.
        for text in ("Drone over Poltava Oblast", "Recon UAV over Sumy Oblast",
                     "Air alert in Kyiv Oblast"):
            assert reports.find_kind(text) != "explosion", text

    def test_the_english_kinds(self):
        assert reports.find_kind("UAV detection") == "drone"
        assert reports.find_kind("FPV activity") == "drone"
        assert reports.find_kind("Jet UAV inbound") == "jet_drone"
        assert reports.find_kind("Reconnaissance drone") == "recon"
        assert reports.find_kind("Cruise missiles launched") == "cruise"
        # "Ballistic threat" is deliberately NOT here: it is a warning, and
        # the warning reading wins. This is the launch itself.
        assert reports.find_kind("Ballistic missile launch detected") == "ballistic"
        assert reports.find_kind("Shot down over the city") == "explosion"

    def test_a_warning_outranks_the_thing_it_warns_about(self):
        # "danger for UAVs" is a warning, not a drone sighting.
        assert reports.find_kind("danger for UAVs/FPVs again") == "alert"

    def test_the_english_region_forms(self):
        assert reports.find_region("Air alert in Kharkiv Oblast") == "Kharkiv oblast"
        assert reports.find_region("Belgorod district again") == "Belgorod district"
        assert reports.find_region("threat for the Emirate of Dubai") == "Dubai emirate"
        assert reports.find_region(
            "alarm in the Governorate of Baghdad") == "Baghdad governorate"

    def test_a_preposition_is_not_a_place_name(self):
        # The pattern was case-insensitive, which made [A-Z] match lowercase,
        # so "in Kharkiv Oblast" captured "in Kharkiv" as the region.
        for text in ("Air alert in Kharkiv Oblast", "Drone over Poltava Oblast"):
            region = reports.find_region(text)
            assert not region.lower().startswith(("in ", "over ", "at ")), text

    def test_english_directions(self):
        assert reports.find_heading("UAV heading north")[0] == "N"
        assert reports.find_heading("drone moving towards Poltava")[1] == "Poltava"

    def test_english_counts(self):
        assert reports.find_count("3 UAVs over the region") == 3

    def test_english_chatter_is_still_refused(self):
        for junk in ("Subscribe to our channel", "Good morning everyone",
                     "Donate to support us", "Leave a comment"):
            assert read(junk) is None, junk


class TestRussia:
    """The other side of the border, read exactly the same way.

    These channels report Belgorod and Bryansk as much as Sumy, and the first
    version handled almost none of it: the region pattern wanted the Ukrainian
    stem, and "угроза"/"опасность" -- how the Russian-side posts say what the
    Ukrainian ones call "тривога" -- were not alert words at all.
    """

    def test_the_shapes_these_posts_actually_take(self):
        cases = [
            ("Белгородская область: угроза БПЛА", "alert", "Белгородская область"),
            ("Курская область, опасность атаки БПЛА", "alert", "Курская область"),
            ("БПЛА над Брянской областью", "drone", "Брянская область"),
            ("Ракетная опасность для Воронежской области", "alert",
             "Воронежская область"),
            ("Белгородчина: сбит БПЛА", "explosion", "Белгородская область"),
        ]
        for text, kind, place in cases:
            got = read(text)
            assert got is not None, text
            assert got["kind"] == kind, text
            assert got["place"] == place, text

    def test_a_russian_region_does_not_get_a_ukrainian_ending(self):
        # "Воронежской области" contains no Russian-only letter, so testing
        # for one put a Ukrainian ending on a Russian region -- "Воронежська
        # область", which no gazetteer knows. The discriminator is the letters
        # Ukrainian has and Russian does not.
        assert reports.find_region("для Воронежской области") == "Воронежская область"
        assert reports.find_region("у Харківській області") == "Харківська область"

    def test_both_alphabets_reach_the_same_kinds(self):
        assert reports.find_kind("БПЛА") == reports.find_kind("БпЛА") == "drone"
        assert reports.find_kind("Взрыв в городе") == "explosion"
        assert reports.find_kind("воздушная тревога") == "alert"

    def test_a_name_in_a_case_is_tried_as_written_first(self):
        # What the report said is the best thing to ask a gazetteer for. The
        # de-inflected guesses come after it, not instead of it.
        assert reports.variants("Белгороде")[0] == "Белгороде"
        assert "Белгород" in reports.variants("Белгороде")

    def test_the_guesses_do_not_mangle_a_name_that_needs_nothing(self):
        assert reports.variants("Київ") == ["Київ"]
        assert reports.variants("") == [""]


class TestTheOtherScripts:
    """Arabic, Hebrew and Farsi, for the Lebanon and Middle East channels.

    Their posts were going entirely unread, which is why nothing from those
    countries ever reached the map. Only the kind is read: place names in a
    script with no capital letters are a different problem from the one these
    patterns solve, and guessing at them would be exactly the invention this
    module refuses to make everywhere else.
    """

    def test_the_kinds_in_arabic(self):
        assert reports.find_kind("تحذير من طائرات مسيرة") == "alert"
        assert reports.find_kind("انفجار في بيروت") == "explosion"
        assert reports.find_kind("صاروخ") == "cruise"

    def test_the_kinds_in_hebrew(self):
        assert reports.find_kind("אזעקה בצפון") == "alert"
        assert reports.find_kind("פיצוץ") == "explosion"

    def test_the_kinds_in_farsi(self):
        assert reports.find_kind("هشدار حمله پهپادی") == "alert"

    def test_they_are_listed_even_though_they_cannot_be_placed(self):
        # Listing beats the silence those channels used to get. The place is
        # left null rather than guessed at.
        for text in ("تحذير من طائرات مسيرة في الجنوب", "אזעקה בצפון",
                     "هشدار حمله پهپادی"):
            got = read(text)
            assert got is not None, text
            assert got["place"] is None, text
            assert got["summary"], text

    def test_latin_chatter_with_no_place_is_still_refused(self):
        # The listing-without-a-place rule is for the scripts this cannot
        # read, and must not become a way for every passing mention to get in.
        assert read("Subscribe to our channel") is None
        assert read("Donate for drones") is None


class TestHowMany:
    def test_a_counted_report(self):
        assert reports.find_count("3х БпЛА на Сумщині") == 3
        assert reports.find_count("12 шахедів") == 12

    def test_an_uncounted_one_is_one(self):
        assert reports.find_count("БпЛА над містом") == 1

    def test_a_silly_number_is_not_believed(self):
        assert reports.find_count("2024 ракет") == 1


class TestTheWholeReading:
    def test_the_report_from_the_screenshot(self):
        # The one that started all of this.
        got = read("Київщина: реактивний БпЛА повз Кагарлик курсом на північ.")
        assert got["kind"] == "jet_drone"
        assert got["place"] == "Кагарлик"
        assert got["course"] == "N"
        assert got["toward"] is None
        assert got["by"] == "rules"
        assert "north" in got["summary"]

    def test_a_destination_report(self):
        got = read("БпЛА на Полтавщині, курс на Кременчук")
        assert got["toward"] == "Кременчук" and got["course"] is None

    def test_a_strike_is_given_no_direction_whatever_it_mentions(self):
        got = read("Вибухи в Одесі, БпЛА курсом на північ")
        assert got["kind"] == "explosion"
        assert got["course"] is None and got["toward"] is None

    def test_a_report_with_a_destination_and_no_origin_is_still_kept(self):
        # It cannot be mapped -- nothing said where it IS -- but it is still a
        # report, and dropping it is the failure this layer was rebuilt to
        # stop making. It goes to the alert list with no place.
        got = read("Крилаті ракети курсом на Кривий Ріг")
        assert got is not None
        assert got["place"] is None
        assert got["toward"] == "Кривий Ріг"

    def test_the_things_that_are_not_reports(self):
        for junk in ("Підписатися на наш канал", "Донат на дрони для ЗСУ 🙏",
                     "", "   ", "Доброго ранку!", "Наш чат"):
            assert read(junk) is None, junk

    def test_a_bare_mention_with_no_place_or_direction_is_not_an_event(self):
        # A fundraising post that says the word "Shahed" is not a report of
        # one. Without this the alert list fills with the channel's own
        # advertising.
        assert read("Збираємо на РЕБ проти шахедів") is None

    def test_everything_it_produces_is_marked_as_its_own(self):
        # A reading from regular expressions is not the same claim as one from
        # a model that read the sentence, and the interface says which.
        assert read("Вибухи в Одесі")["by"] == "rules"

    def test_the_summary_is_english_and_short(self):
        for text in ("Київщина: БпЛА повз Кагарлик курсом на північ",
                     "3х БпЛА на Сумщині курсом на південь",
                     "Вибухи в Одесі"):
            summary = read(text)["summary"]
            assert 0 < len(summary) <= 160
            assert summary[0].isupper() or summary[0].isdigit()

    def test_it_never_raises_on_anything(self):
        # It runs on whatever a channel posts, including posts that are an
        # emoji and a link. A reader that throws would take the fallback down
        # exactly when the model is already unavailable.
        for odd in (None, "", "🔥🔥🔥", "https://t.me/x", "х" * 2000,
                    "БпЛА " * 400, "‌", "<script>", "курсом на "):
            reports.read(odd)


class TestTheOutputFitsWhatConsumesIt:
    def test_every_kind_it_can_return_is_one_the_map_knows(self):
        from backend import osint
        for kind, _ in reports.KIND_WORDS:
            assert kind in osint.KINDS, kind
        assert set(reports.SAYS) <= set(osint.KINDS) | {"unknown"}

    def test_every_course_it_can_return_is_one_the_map_can_read(self):
        from backend import osint
        for _, point in reports.COURSE_WORDS:
            assert osint.read_course(point) is not None, point

    def test_a_reading_survives_the_cleaning_the_model_path_goes_through(self):
        from backend import osint
        got = osint._clean({**read("Київщина: БпЛА повз Кагарлик курсом на північ"),
                            "id": "c/1"})
        assert got["kind"] == "drone"
        assert got["place"] == "Кагарлик"
        assert got["course"] == 0.0
