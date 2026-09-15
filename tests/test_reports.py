"""Tests for reading air-threat reports without a language model.

This exists because the model can be absent. It was a free tier with a daily
ceiling, and when that ceiling was reached the whole layer went dark -- an
empty map and a line about rate limiting, which from the outside is exactly
what a broken feature looks like. The model is local now and has no ceiling,
but it can equally be a daemon that is not running or one with nothing pulled,
and the answer to all three is the same: read the reports by rule instead.

These reports barely need a model. They are written to be scanned during an
air raid and they are formulaic to the point of being a grammar. So the tests
here are the grammar: the shapes that actually appear in these channels, and
-- just as important -- the things that look like reports and are not.
"""

from __future__ import annotations

from backend import places, reports


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

    def test_reconnaissance_is_still_recognised_here(self):
        # The reader keeps the distinction even though the map no longer draws
        # it: tracker.FOLD collapses "recon" into "drone", because telling a
        # reconnaissance drone from an attack one needs the airframe and these
        # reports usually just say "БпЛА".
        #
        # Kept because the phrase is real and unambiguous when it does appear,
        # and because keeping it is what makes the decision reversible -- the
        # information is still extracted, and putting the kind back is a line
        # in a table rather than a re-derivation.
        for text in ("Розвідувальний БпЛА", "Орлан-10 над районом",
                     "ZALA у повітрі", "борт-розвідник"):
            assert reports.find_kind(text) == "recon", text

    def test_missile_types_are_still_told_apart_here(self):
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


class TestNamesAsTheMapHoldsThem:
    """The lookup misses that the live run showed.

    The model was being told to transliterate place names to Latin, and it
    was: "Любешів" came back as "Lubeshiv". OpenStreetMap holds the Cyrillic,
    so a Latin spelling the model invented is one the map has never heard of,
    and three of eight reports in a live run were "named nowhere a map knows".

    It is asked for the original script now, which means the names arrive in
    whatever case the sentence used -- so the de-inflection here has to earn
    its keep. Every variant costs a Nominatim request and Nominatim is asked
    at most once a second, so a wrong guess is a second a report spends
    unplaced.
    """

    def test_an_oblast_in_the_genitive_becomes_the_nominative(self):
        # The commonest names in these reports by a distance.
        for asked, wanted in (
            ("Волинської області", "Волинська область"),
            ("Київської області", "Київська область"),
            ("Сумській області", "Сумська область"),
            ("Белгородской области", "Белгородская область"),
        ):
            assert wanted in reports.variants(asked), asked

    def test_the_one_word_oblast_form_too(self):
        # "Харківщина" and "Харківщини" both mean Kharkiv oblast, and neither
        # is what the map calls it.
        for asked, wanted in (("Харківщини", "Харківська область"),
                              ("Харківщина", "Харківська область"),
                              ("Сумщини", "Сумська область")):
            assert wanted in reports.variants(asked), asked

    def test_the_instrumental_a_report_produces_with_nad(self):
        # "над Нікополем" is how these posts say it, and the nominative is
        # Нікополь. This was missing entirely.
        assert "Нікополь" in reports.variants("Нікополем")
        assert "Харков" in reports.variants("Харковом")

    def test_the_ukrainian_vowel_alternation(self):
        # Stripping the case ending alone gives "Харков", which is not a
        # place; the name is "Харків".
        assert "Харків" in reports.variants("Харкові")
        assert "Львів" in reports.variants("Львові")
        assert "Тернопіль" in reports.variants("Тернополі")

    def test_the_alternation_does_not_fire_where_it_does_not_apply(self):
        # A rule that fires on names it does not apply to is worse than no
        # rule: it costs a second each. Ужгород and Белгород keep their о, and
        # a wider version of this produced "Ужгорід" and "Белгорід".
        for asked, junk in (("Ужгороді", "Ужгорід"), ("Белгороде", "Белгорід")):
            assert junk not in reports.variants(asked), asked
        assert "Ужгород" in reports.variants("Ужгороді")
        assert "Белгород" in reports.variants("Белгороде")

    def test_a_leading_preposition_is_dropped(self):
        assert "Харків" in reports.variants("у Харкові")
        assert "Нікополь" in reports.variants("над Нікополем")
        for asked in ("у Харкові", "в Белгороде", "над Нікополем"):
            assert not reports.variants(asked)[0].startswith(("у ", "в ", "над ")), asked

    def test_a_name_already_nominative_is_asked_for_once_and_no_more(self):
        # Nothing to de-inflect, so nothing should be guessed at: a lookup
        # that succeeds first time must not pay for three more.
        for plain in ("Харків", "Волинська область", "Бахів", "Nikopol"):
            assert reports.variants(plain) == [plain], plain

    def test_a_recognised_oblast_stops_the_letter_stripping(self):
        # "Волинської області" used to also produce "Волинської област" -- a
        # form no map has ever held, charged at a second.
        got = reports.variants("Волинської області")
        assert got == ["Волинської області", "Волинська область"], got

    def test_no_name_costs_more_than_four_lookups(self):
        """Swept rather than sampled, because the first version was vacuous.

        It checked four hand-picked names against a cap of four, all of which
        produced two or three -- so it passed with the cap deleted and proved
        nothing. The property worth holding is about every name, not four of
        them: whatever endings combine, the rules above must not produce a
        fifth lookup, because each one is a second the report spends unplaced.

        The cap in variants() does not currently fire -- the maximum is
        exactly four -- so this is what guards the cost, and it fails the
        moment a fifth rule is added.
        """
        import itertools
        stems = ("Харк", "Льв", "Тернопол", "Белгород", "Одесс", "Нікопол",
                 "Сум", "Київ", "Абракадабр", "Миколаїв")
        endings = ("", "е", "і", "и", "а", "ем", "ом", "ове", "ові", "щини",
                   "щина", "щину", " області", "ої області", "ой области",
                   " области", "ій області")
        worst = 0
        for stem, ending in itertools.product(stems, endings):
            got = reports.variants(stem + ending)
            worst = max(worst, len(got))
            assert len(got) <= 4, f"{stem}{ending} -> {got}"
            # And no duplicates, which would waste a lookup on a repeat.
            assert len(got) == len(set(got)), f"{stem}{ending} -> {got}"
        # The sweep has to actually exercise the rules, or it is the same
        # vacuous test with more inputs.
        assert worst == 4, f"the sweep only ever reached {worst} variants"

    def test_the_name_as_written_is_always_tried_first(self):
        # What the report said is the best thing to ask for. A guess at its
        # nominative is only worth trying once that has failed.
        for asked in ("Харкові", "Волинської області", "Нікополем", "Харківщини"):
            assert reports.variants(asked)[0] == asked, asked

    def test_rubbish_does_not_produce_rubbish_lookups(self):
        for junk in ("", "   ", None, "a"):
            got = reports.variants(junk)
            assert got == [] or all(len(v.strip()) >= 1 for v in got), junk


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

    def test_a_locative_town_a_table_knows_is_left_alone_and_read_aloud(self):
        """The guess does not overrule the answer.

        "Одесі" used to be rewritten to "Одеса" by a blanket feminine rule
        that turns a trailing "і" into "а". That rule is a guess, and on
        "Кременчуці" -- masculine -- it produced "Кременчуца", a string no
        gazetteer has, costing the report its place and a second of
        Nominatim's rate limit to find that out.

        Now a name the built-in table recognises survives as written, because
        the table is keyed by these forms and resolves them directly. The
        nominative still appears where a person reads it.
        """
        got = read("Вибухи в Одесі")
        assert got["place"] == "Одесі"
        assert places.lookup(got["place"])["name"] == "Одеса"
        assert "Одеса" in got["summary"]

    def test_a_locative_town_no_table_knows_is_still_guessed_at(self):
        # The feminine rule is not gone, only outranked. An unknown town still
        # gets its ending undone, because an inflected name reaches nothing.
        assert read("Вибухи в Кобеляці")["place"] == "Кобеляца"

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

    def test_nothing_to_look_up_is_no_lookups(self):
        # This asserted [""] until the cost of a variant was noticed. A caller
        # loops over these and asks the gazetteer for each at one request a
        # second, so an empty string is a request that can only fail.
        assert reports.variants("") == []
        assert reports.variants("   ") == []
        assert reports.variants(None) == []


class TestTheOtherScripts:
    """Arabic, Hebrew and Farsi.

    No channel in the current four posts in these, so nothing here is on the
    live path -- it is kept because the cost is a few regexes and the reader
    should not silently fail to recognise a report it could read. Only the
    kind is read: place names in a script with no capital letters are a
    different problem from the one these
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
    def test_every_kind_it_can_return_is_one_the_map_can_draw(self):
        """Folds to one, rather than being one.

        This asserted direct membership until the kinds were collapsed. The
        reader still makes the finer distinctions -- "розвідувальний БпЛА" is
        a real and unambiguous phrase, and балістика is genuinely not a cruise
        missile -- and tracker.FOLD collapses them at the boundary, so what
        has to hold is that everything the reader produces lands somewhere the
        map knows.

        Stronger than the old version, which would have passed with the fold
        table empty and every missile drawn grey.
        """
        from backend import tracker
        for kind, _ in reports.KIND_WORDS:
            folded = tracker.fold_kind(kind)
            # Two kinds are read and not drawn, both on purpose, and both
            # still have to survive the fold as THEMSELVES.
            #
            # LIFTED: "відбій тривоги" is a report that a warning has ENDED,
            # and the reader has to tell it apart from "тривога" -- which it
            # contains -- or the map raises an alert at the moment one is
            # lifted. It goes to tracker.lift_alerts(), not to a marker.
            #
            # NOT_DRAWN: an explosion is recognised and then dropped. It has
            # to keep its own name through the fold precisely BECAUSE it is
            # not in KINDS -- anything unrecognised falls through to
            # "unknown", which would put a grey ring in the sky over a
            # report that explosions had been heard.
            assert (folded in tracker.KINDS or folded == tracker.LIFTED
                    or folded in tracker.NOT_DRAWN), f"{kind} -> {folded}"
            # And not silently thrown away: a kind the reader identified must
            # not come out the other side as "unknown".
            assert folded != "unknown" or kind == "unknown", \
                f"{kind} was read and then discarded"
        for kind in reports.SAYS:
            # LIFTED is the deliberate exception, the same one as above: an
            # all-clear is drawn by REMOVING a warning rather than by adding
            # a mark, so it has wording and no drawing.
            assert (tracker.fold_kind(kind) in tracker.KINDS
                    or tracker.fold_kind(kind) == tracker.LIFTED
                    or tracker.fold_kind(kind) in tracker.NOT_DRAWN), kind

    def test_every_course_it_can_return_is_one_the_map_can_read(self):
        from backend import tracker
        for _, point in reports.COURSE_WORDS:
            assert tracker.read_course(point) is not None, point

    def test_a_reading_survives_the_cleaning_the_model_path_goes_through(self):
        from backend import tracker
        got = tracker._clean({**read("Київщина: БпЛА повз Кагарлик курсом на північ"),
                            "id": "c/1"})
        assert got["kind"] == "drone"
        assert got["place"] == "Кагарлик"
        assert got["course"] == 0.0


DIGEST = (
    "⚠️ Щодо руху ударних БпЛА: "
    "🛸 Сумщина: 🛩 БпЛА в р—ні н.п. Путивль, Глухів, Кролевець, Буринь та "
    "Лебедин рухаються західним курсом; "
    "🛸 Чернігівщина: 🛩 БпЛА в р—ні н.п. Батурин, Сосниця, Макошине, Ніжин, "
    "Козелець та Гончарівське рухаються західним курсом; "
    "🛸 Київщина: 🛩 БпЛА в р—ні Київського водосховища рухаються західним "
    "курсом; "
    "🛸 Житомирщина: 🛩 БпЛА в р—ні н.п. Малин, Коростень та Нова Борова "
    "рухаються західним курсом; "
    "🛸 Рівненщина: 🛩 БпЛА в р—ні н.п. Корець та Здолбунів рухаються "
    "західним курсом."
)


class TestTheMovementDigest:
    """The busiest post of the night, and the one that was read worst.

    Fifteen settlements across five oblasts, each section stating its own
    course. read() returned ONE reading for it -- on whichever oblast matched
    last, with no course -- so a post naming fifteen towns heading west drew a
    single courseless ring in the middle of a province, and the fifteen towns
    were simply not on the map.

    Both halves of that had a cause. The place came from a pattern that finds
    the first plausible name and stops; the course was missing because these
    posts write it in the instrumental -- "рухаються західним курсом" -- and
    find_heading() only looks for "курсом на <point>".
    """

    def test_every_named_town_gets_its_own_reading(self):
        got = reports.read_all(DIGEST)
        named = [g["place"] for g in got]
        for town in ("Путивль", "Глухів", "Кролевець", "Буринь", "Лебедин",
                     "Батурин", "Сосниця", "Макошине", "Ніжин", "Козелець",
                     "Гончарівське", "Малин", "Коростень", "Нова Борова",
                     "Корець", "Здолбунів"):
            assert town in named, f"{town} missing from {named}"

    def test_the_last_town_in_a_section_is_not_eaten_by_the_course_phrase(self):
        # "Лебедин рухаються західним курсом" has no comma before the verb, so
        # the last name of every section arrived with the phrase stuck to it.
        # Judged before it was trimmed, it failed and the list ended there --
        # which quietly dropped the last town of all five sections.
        got = [g["place"] for g in reports.read_all(DIGEST)]
        assert "Лебедин" in got
        assert "Нова Борова" in got
        assert "Здолбунів" in got
        assert not any("рухаються" in name for name in got), got

    def test_each_section_carries_its_own_course(self):
        got = reports.read_all(DIGEST)
        assert all(g["course"] == "W" for g in got), \
            sorted({g["course"] for g in got})

    def test_a_section_naming_no_town_falls_back_to_its_oblast(self):
        # "в р-ні Київського водосховища" is the Kyiv Reservoir: a hundred
        # kilometres of water, in the genitive, that no gazetteer answers for.
        # The oblast the section named is the honest place for that mark.
        got = reports.read_all(DIGEST)
        kyiv = [g for g in got if "Київськ" in g["place"]]
        assert kyiv, [g["place"] for g in got]
        assert kyiv[0]["place"] == "Київська область"
        assert not any("водосховищ" in g["place"] for g in got)

    def test_every_place_it_produces_is_one_the_app_can_put_down(self):
        # The point of reading fifteen names is fifteen marks. A name that
        # reaches neither the built-in table nor a plausible gazetteer lookup
        # is a row in "unplaced", which is worse than the ring it replaced.
        for got in reports.read_all(DIGEST):
            assert places.lookup(got["place"]), got["place"]

    def test_the_kind_comes_from_the_section(self):
        assert all(g["kind"] == "drone" for g in reports.read_all(DIGEST))

    def test_an_ordinary_post_is_still_one_reading(self):
        # One heading is a report that happens to name its oblast. It takes
        # two to be a digest, or every "Сумщина: вибухи" becomes a section.
        for text in ("Вибухи у Харкові", "Сумщина: вибухи в Охтирці",
                     "Шахед над Нікополем курсом на північ"):
            got = reports.read_all(text)
            assert len(got) == 1, (text, got)

    def test_a_post_with_nothing_in_it_is_still_nothing(self):
        assert reports.read_all("Підписуйтесь на наш канал") == []
        assert reports.read_all("") == []

    def test_a_malformed_digest_cannot_become_a_hundred_marks(self):
        # A comma-separated wall of capitalised words is bounded, so a page
        # that changes shape degrades to too few marks rather than to a
        # thousand.
        wall = "; ".join(
            "Сумщина: БпЛА в р-ні н.п. " + ", ".join(f"Місто{i}{j}"
                                                     for j in range(40))
            for i in range(3))
        got = reports.read_all(wall)
        # A literal, not `3 * reports.MOST_PER_SECTION`. Written that way this
        # compared the cap against itself: raising the cap raised the bound
        # too, so deleting it entirely still passed.
        assert reports.MOST_PER_SECTION <= 20
        assert len(got) <= 60, len(got)


class TestTheRussianRadarChannel:
    """Warnings written in English, and read as drones.

    @radarrussiia posts "Lipetsk Oblast Drone Alert" and "Republic of
    Tatarstan, Republic of Bashkortostan — UAV alert cleared." Every one of
    those was read as a DRONE, because the drone pattern matched the word
    "Drone" and the English alert pattern only knew "air raid", "threat" and
    "warning" -- a bare "Alert" was not in it.

    So a warning FOR a province was drawn as an aircraft flying over it, at a
    point, in the middle of a region where nothing had been reported; and a
    stand-down put a fresh drone in the air at the moment one was lifted.
    """

    def test_a_warning_is_a_warning_and_not_the_thing_it_warns_about(self):
        assert reports.find_kind("Lipetsk Oblast Drone Alert") == "alert"
        assert reports.find_kind("Voronezh Oblast Missile Alert") == "alert"
        assert reports.find_kind("Kursk Oblast UAV Alarm") == "alert"

    def test_a_stand_down_is_not_a_warning_and_not_a_drone(self):
        for text in ("Republic of Tatarstan — UAV alert cleared.",
                     "Chuvash Republic — UAV alert cleared.",
                     "Bryansk Oblast — missile threat lifted",
                     "Belgorod Oblast — drone alert is over"):
            assert reports.find_kind(text) == "all_clear", text

    def test_the_weapon_word_says_what_the_warning_is_about(self):
        # Which is drawn as colour: yellow for a drone warning, red for a
        # missile one. That difference is fifteen minutes against ninety
        # seconds, and drawing both amber said neither.
        assert reports.find_cause("Lipetsk Oblast Drone Alert") == "drone"
        assert reports.find_cause("Voronezh Oblast Missile Alert") == "missile"
        assert reports.find_cause("Ракетна небезпека для Харківщини") == "missile"

    def test_a_warning_naming_both_is_about_the_faster_one(self):
        # Being wrong towards "missile" costs a reader caution they did not
        # need. Being wrong towards "drone" costs them the time to take cover.
        assert reports.find_cause("drone and missile threat") == "missile"

    def test_a_warning_that_does_not_say_keeps_no_cause(self):
        # A real third state. Inventing one would be worse than the neutral
        # amber it falls back to.
        assert reports.find_cause("Повітряна тривога у Києві") is None
        assert reports.read("Повітряна тривога у Києві")["cause"] is None

    def test_only_warnings_carry_a_cause(self):
        # A drone in the air IS a drone; "cause" would be saying the same
        # thing twice and would colour a strike by whatever hit it.
        for text in ("Шахед над Нікополем", "Вибухи у Харкові"):
            assert reports.read(text)["cause"] is None, text

    def test_several_regions_in_one_post_all_get_read(self):
        """The shape most of that channel's stand-downs come in.

        read() took the first region and dropped the rest, so a warning lifted
        across four provinces was lifted on the map across one -- and the
        other three kept a warning that had ended.
        """
        got = reports.read_all(
            "Republic of Tatarstan, Republic of Bashkortostan – UAV alert cleared.")
        assert [g["place"] for g in got] == [
            "Tatarstan republic", "Bashkortostan republic"]
        assert all(g["kind"] == "all_clear" for g in got)
        assert all(g["cause"] == "drone" for g in got)

    def test_and_the_republics_without_the_word_republic_first(self):
        got = reports.read_all("Chuvash Republic, Mari El Republic – UAV alert cleared.")
        assert [g["place"] for g in got] == ["Chuvash republic", "Mari El republic"]

    def test_every_region_it_names_is_one_the_table_can_place(self):
        # These posts arrive in English and OpenStreetMap holds these regions
        # in Russian, so without the aliases each one was a failed lookup that
        # cost a second of Nominatim's rate limit to discover.
        for text in ("Lipetsk Oblast Drone Alert",
                     "Voronezh Oblast Missile Alert",
                     "Republic of Tatarstan, Republic of Bashkortostan – UAV alert cleared.",
                     "Chuvash Republic, Mari El Republic – UAV alert cleared."):
            for got in reports.read_all(text):
                assert places.lookup(got["place"]), (text, got["place"])

    def test_a_strike_report_is_still_a_strike(self):
        # The alert pattern is tried before the weapon words now, so a post
        # that is genuinely about something happening must not be swept up.
        got = reports.read(
            "Nizhnekamsk, Republic of Tatarstan, was the target of a massive drone strike.")
        assert got["kind"] == "explosion"

    def test_an_ordinary_movement_report_is_not_a_warning(self):
        # The risk of putting a bare "alert" in the pattern. These have to go
        # on being read as things in the air.
        for text in ("Шахед над Нікополем курсом на північ",
                     "БпЛА курсом на Київ",
                     "UAV heading west past Kaharlyk"):
            assert reports.find_kind(text) in ("drone", "jet_drone"), text


class TestHeadlinesAreNotSentences:
    """These channels write headlines, and the words in them are capitalised.

    Every name pattern here takes up to two capitalised words, which is right
    for "Nova Borova" and wrong for "Tatarstan Drone" -- and the second is what
    "Republic of Tatarstan Drone Alert" produced. That is a place no gazetteer
    has, so a warning written in that word order was listed as unplaceable
    while the same warning written "Lipetsk Oblast Drone Alert" worked.
    """

    def test_a_weapon_word_after_the_name_is_not_part_of_it(self):
        got = reports.read("Republic of Tatarstan Drone Alert")
        assert got["place"] == "Tatarstan republic"
        assert places.lookup(got["place"])

    def test_a_headline_word_before_the_name_is_not_either(self):
        for text in ("BREAKING Kursk Oblast Drone Alert",
                     "URGENT Voronezh Oblast Missile Alert"):
            got = reports.read(text)
            assert places.lookup(got["place"]), (text, got["place"])

    def test_a_two_word_place_name_survives_the_trimming(self):
        # The trim must not eat real names. "Mari El" and "Nova Borova" are
        # two capitalised words and both of them are places.
        assert reports._trim_en("Mari El") == "Mari El"
        assert reports._trim_en("Nova Borova") == "Nova Borova"
        assert reports._trim_en("Nizhny Novgorod") == "Nizhny Novgorod"

    def test_a_name_that_is_only_headline_words_does_not_become_empty(self):
        # The loops stop at one word rather than emptying the list, so this
        # returns something rather than None and fails the gazetteer honestly.
        assert reports._trim_en("Alert") == "Alert"


class TestTheFeminineInstrumental:
    """"над Охтиркою" had no rule at all, in either place it needed one.

    "над X" is how half these reports name a place, and the masculine forms --
    "над Нікополем", "над Харковом" -- were handled while the feminine one was
    not. So every "над Охтиркою", "над Шосткою", "над Вінницею" produced a
    name no gazetteer holds, and it failed as a silent unplaced row rather
    than as anything anybody would notice.
    """

    def test_the_reader_puts_it_back_in_the_nominative(self):
        for written, wanted in (("Сумщина: Шахед над Охтиркою", "Охтирка"),
                                ("Шахед над Шосткою", "Шостка")):
            assert reports.read(written)["place"] == wanted, written

    def test_and_the_lookup_forms_carry_it_too(self):
        # Two separate rule sets -- _nominative() at read time and variants()
        # at lookup time -- and a name can arrive at either. Adding the rule
        # to one and not the other leaves half the cases failing.
        assert "Охтирка" in reports.variants("Охтиркою")
        assert "Шостка" in reports.variants("Шосткою")
        assert "Вінниця" in reports.variants("Вінницею")

    def test_what_it_produces_is_a_place_the_table_holds(self):
        for written in ("Охтиркою", "Шосткою", "Вінницею", "Полтавою"):
            assert any(places.lookup(v) for v in reports.variants(written)), written

    def test_it_does_not_fire_on_a_name_the_table_already_knows(self):
        # The guard that stops a guess overruling an answer. A rule this
        # broad would otherwise mangle anything ending in those two letters.
        assert reports.read("Вибухи в Одесі")["place"] == "Одесі"

    def test_it_still_costs_no_more_than_the_cap(self):
        # Every variant is a second against Nominatim's rate limit, and this
        # is a fifth rule added to a set the cap was written to bound.
        for written in ("Охтиркою", "Шосткою", "Вінницею", "Харкові",
                        "Белгороде", "Нікополем"):
            assert len(reports.variants(written)) <= 4, written


class TestTheCasesThatWereLeavingReportsUnplaced:
    """Measured, not guessed at: eleven of fifteen realistic forms missed.

    The panel said "5 on the map · 7 unplaced" and the rows it could not place
    were ordinary posts -- "над Броварами", "над Фастовом", "над Кривим
    Рогом". Each was a different hole in the de-inflection, and each failed
    silently as an unplaced row rather than as anything that looked wrong.
    """

    def placeable(self, text):
        got = reports.read(text)
        if not got:
            return False
        name = got.get("place") or got.get("toward")
        return bool(name) and any(places.lookup(v)
                                  for v in reports.variants(name))

    def test_the_plural_instrumental(self):
        # A large share of these towns have plural names, and "над X" is the
        # commonest preposition in the feed -- so this one hole covered
        # Бровари, Прилуки, Лубни, Ромни, Черкаси and Суми at once.
        for text in ("Шахед над Броварами", "БпЛА над Прилуками",
                     "Шахед над Лубнами", "БпЛА над Ромнами",
                     "Шахед над Черкасами", "БпЛА над Сумами"):
            assert self.placeable(text), text

    def test_the_vowel_alternation_after_an_instrumental(self):
        # The rule existed and was applied only after the locative strip, so
        # "над Фастовом" gave "Фастов" and the place is "Фастів".
        assert "Фастів" in reports.variants("Фастовом")
        assert "Обухів" in reports.variants("Обуховом")

    def test_the_feminine_accusative_a_course_produces(self):
        # "курсом на Одесу" -- the destination, which is looked up the same
        # way a place is.
        for written, wanted in (("Одесу", "Одеса"), ("Полтаву", "Полтава"),
                                ("Вінницю", "Вінниця")):
            assert wanted in reports.variants(written), written

    def test_two_word_names_in_oblique_cases(self):
        # The letter rules work a word at a time and these inflect both words
        # at once, so no rule was ever going to reach them. Listed instead.
        for text in ("БпЛА над Білою Церквою", "Шахед над Кривим Рогом",
                     "БпЛА над Новгородом-Сіверським"):
            assert self.placeable(text), text

    def test_the_whole_sweep_places(self):
        """The measurement itself, as a test.

        Eleven of these fifteen were unplaceable. Kept as a sweep rather than
        as separate cases because the number is the point: a rule added for
        one of them that broke another would still pass every test above.
        """
        forms = [
            "Шахед над Броварами", "БпЛА над Прилуками", "Шахед над Лубнами",
            "БпЛА над Ромнами", "Шахед над Фастовом", "Шахед над Обуховом",
            "Шахед курсом на Полтаву", "БпЛА курсом на Вінницю",
            "БпЛА над Білою Церквою", "Шахед над Кривим Рогом",
            "БпЛА над Сумами", "Шахед над Черкасами", "БпЛА над Конотопом",
            "Шахед над Ніжином", "БпЛА над Воронежской областью",
        ]
        missed = [text for text in forms if not self.placeable(text)]
        assert not missed, missed

    def test_a_junk_form_is_not_tried_ahead_of_the_answer(self):
        # "Броварами" also ends in "и", so the locative strip produced
        # "Броварам" -- a form no map holds -- and appended it FIRST. Every
        # one of those is a second of Nominatim's rate limit spent ahead of
        # the right answer.
        got = reports.variants("Броварами")
        assert "Броварам" not in got, got
        assert got.index("Бровари") == 1, got

    def test_none_of_this_costs_more_than_the_cap(self):
        # Five rules were added to a set the cap was written to bound.
        for written in ("Броварами", "Фастовом", "Одесу", "Охтиркою",
                        "Харкові", "Белгороде", "Нікополем", "Сумами",
                        "Львові", "Тернополі", "Волинської області"):
            assert len(reports.variants(written)) <= 4, (
                written, reports.variants(written))

    def test_a_report_that_says_only_where_it_is_going_stays_unplaced(self):
        """And that is correct, however much it looks like a miss.

        "БпЛА курсом на Одесу" says a drone is heading for Odesa and does not
        say where it is. Drawing it on Odesa would claim it had arrived, which
        on a map somebody uses to decide whether to take cover is a materially
        wrong thing to say. It is listed with its reason instead.
        """
        got = reports.read("БпЛА курсом на Одесу")
        assert got["place"] is None
        assert got["toward"] == "Одесу"
        # Named properly in the line a person reads, even so.
        assert "Одеса" in got["summary"]


# Every shape these five channels actually post, in both languages and both
# scripts. Kept as one list because the property worth holding is about the
# WHOLE set: a rule added for one form that breaks another would still pass
# every individual case written separately.
EVERY_SHAPE = (
    "Шахед над Нікополем курсом на північ", "БпЛА повз Кагарлик",
    "Вибухи в Одесі", "Тривога у Харківській області", "БпЛА на Полтавщині",
    "Група БпЛА над Сумщиною", "Балістика на Дніпропетровщину",
    "БпЛА в районі Кременчука", "Шахед над Житомиром", "Вибух у Запоріжжі",
    "БпЛА біля Ізмаїла", "Шахед над Чернігівщиною", "Ракети на Львівщину",
    "БпЛА над Вінниччиною", "Тривога на Рівненщині", "Вибухи у Миколаєві",
    "Шахед над Хмельниччиною", "БпЛА над Тернопільщиною", "Вибухи на Одещині",
    "Шахед над Кропивницьким", "БпЛА над Ужгородом", "Вибух у Чернівцях",
    "Тривога у Закарпатській області", "Шахед над Луцьком", "БпЛА над Рівним",
    "Вибухи у Дніпрі", "Шахед над Херсонщиною", "БпЛА над Донеччиною",
    "Тривога на Луганщині", "Шахед над Кривим Рогом", "БпЛА над Білою Церквою",
    "Вибухи у Броварах", "БпЛА над Бориспілем", "Шахед над Фастовом",
    "Вибух у Павлограді", "Повітряна тривога у Запорізькій області",
    "Тривога у Донецькій області", "Тривога у Вінницькій області",
    "БпЛА над Белгородской областью", "Взрыв в Курске",
    "БпЛА над Воронежской областью", "Взрывы в Брянске",
    "БпЛА над Ростовской областью", "Отбой угрозы БпЛА в Липецкой области",
    "Lipetsk Oblast Drone Alert", "Voronezh Oblast Missile Alert",
    "Republic of Tatarstan, Republic of Bashkortostan – UAV alert cleared.",
    "Explosions reported in Kharkiv", "UAV heading west past Kaharlyk",
    "Drone over Sumy Oblast", "Drone over Kyiv Oblast",
    "Nizhnekamsk, Republic of Tatarstan, was the target of a massive drone strike.",
    "Explosions in Zaporizhzhia", "Air alert in Lviv Oblast",
)


class TestNothingInTheSweepGoesUnplaced:
    """The whole sweep, as one number, because the number is the point.

    It started at eleven of fifteen unplaceable and every fix since has been
    found by widening this rather than by guessing. What it holds now is that
    every shape these channels post is both READ and PLACEABLE -- the second
    being the one that failed silently, as a row in the panel nobody could act
    on.
    """

    def test_every_shape_is_read(self):
        unread = [t for t in EVERY_SHAPE if not reports.read_all(t)]
        assert not unread, unread

    def test_every_shape_names_a_place(self):
        nameless = [t for t in EVERY_SHAPE
                    if any(not g.get("place") for g in reports.read_all(t))]
        assert not nameless, nameless

    def test_every_place_it_names_can_be_found(self):
        lost = []
        for text in EVERY_SHAPE:
            for got in reports.read_all(text):
                name = got["place"]
                if not any(places.lookup(v) for v in reports.variants(name)):
                    lost.append((text, name))
        assert not lost, lost

    def test_and_found_without_a_request(self):
        # Not just placeable but placeable for nothing. Every one of these is
        # a name these channels write nightly; a lookup for one is a second of
        # Nominatim's rate limit spent on an answer that has not changed.
        wired = []
        for text in EVERY_SHAPE:
            for got in reports.read_all(text):
                if not any(places.lookup(v)
                           for v in [got["place"], *reports.variants(got["place"])]):
                    wired.append((text, got["place"]))
        assert not wired, wired

    def test_the_oblast_adjectives_that_are_not_spelled_with_s(self):
        """Four oblasts, and they were all silently failing.

        The pattern hardcoded an "с" before "-ька", and Запорізька, Донецька,
        Вінницька and Хмельницька do not have one. So "Повітряна тривога у
        Запорізькій області" came back as the place "Запорізькій" -- the
        adjective alone, with "області" lost -- which no gazetteer holds.

        That is a warning for a whole province going unplaced, and because the
        declared warning never landed, a DERIVED one was raised beside it from
        the drones underneath: two triangles over one oblast.
        """
        for written, wanted in (
            ("Повітряна тривога у Запорізькій області", "Запорізька область"),
            ("Тривога у Донецькій області", "Донецька область"),
            ("Тривога у Вінницькій області", "Вінницька область"),
            ("Тривога у Хмельницькій області", "Хмельницька область"),
        ):
            assert reports.read(written)["place"] == wanted, written

    def test_a_masculine_region_word_keeps_its_masculine_ending(self):
        # "Краснодарская край" is not a thing anybody writes and not a thing
        # any gazetteer holds. The rebuild always produced the feminine.
        assert reports.read("БпЛА над Краснодарским краем")["place"] \
            == "Краснодарский край"

    def test_a_bare_plural_of_missile_is_a_report_of_missiles(self):
        # "Ракети на Львівщину" was in no pattern at all and was read as
        # nothing, so it produced no mark of any kind.
        assert reports.find_kind("Ракети на Львівщину") == "cruise"
        assert reports.read("Ракети на Львівщину")["place"] == "Львівська область"

    def test_a_missile_warning_is_still_a_warning_not_a_missile(self):
        # The risk of widening the missile pattern: "alert" is tried first, so
        # a warning ABOUT missiles stays a warning.
        assert reports.find_kind("Ракетна небезпека для Харківщини") == "alert"
        assert reports.find_kind("Ракетная опасность") == "alert"


class TestTheLineAPersonActuallyReads:
    """The summary is the row in the panel, and it was saying nothing.

    "Report over Республика Башкортостан" is what a lifted warning read as --
    the default wording, because all_clear had none. It is the commonest post
    on the Russian radar channel, so the commonest row in the panel said
    nothing about the one fact in it: that the warning ENDED.
    """

    def test_a_lifted_warning_says_it_is_over(self):
        for text in ("Republic of Tatarstan – UAV alert cleared.",
                     "Відбій тривоги у Сумській області",
                     "Bryansk Oblast — missile threat lifted"):
            said = reports.read(text)["summary"]
            assert said.lower().startswith("all clear"), (text, said)
            assert "report over" not in said.lower(), said

    def test_it_names_the_region_without_pretending_to_be_an_event(self):
        # "All clear — Sumy oblast", not "All clear in Sumy oblast". A
        # stand-down is a statement ABOUT a region, not a thing happening
        # inside one.
        said = reports.read("Відбій тривоги у Сумській області")["summary"]
        assert "All clear — Сумська область" == said

    def test_a_warning_is_never_counted(self):
        """"3 × air alert in Sumy oblast" is not a thing that can happen.

        A province is under a warning or it is not. The count comes from a
        phrase like "група БпЛА" elsewhere in the post and multiplying the
        warning by it says something impossible.
        """
        # A number in the post, so the guard is actually reached. Written
        # with "група БпЛА" first, which has no digit in it -- so find_count
        # returned 1, the multiplication never happened, and the test passed
        # with the guard deleted.
        for text in ("Повітряна тривога у Сумській області — 12 БпЛА",
                     "Відбій тривоги у Сумській області — 12 БпЛА"):
            got = reports.read(text)
            assert got["kind"] in ("alert", "all_clear"), (text, got["kind"])
            assert got["count"] > 1, "the count never reached the summary"
            assert "×" not in got["summary"], (text, got["summary"])

    def test_things_in_the_air_are_still_counted(self):
        # The other half: the count is worth saying where it means something.
        assert "×" in reports.read("12 шахедів над Одещиною")["summary"]

    def test_every_kind_it_can_produce_has_wording(self):
        # A kind with no entry falls through to "Report", which is the bug
        # this class is about -- and it fails silently, as a dull row.
        for kind, _ in reports.KIND_WORDS:
            assert kind in reports.SAYS, kind


class TestWhereItIsVersusWhereItIsGoing:
    """A report gives both, and the position was being read as the destination.

    "Реактивний БпЛА через зону відчуження Чорнобильської АЕС курсом на
    Житомирщину" says a jet drone is crossing the Chornobyl exclusion zone on
    its way to Zhytomyr oblast. The place-finding read the whole sentence, so
    it took Zhytomyr -- the destination -- as the position, and the popup said
    "over Житомирська область, heading for Житомирська область": a thing drawn
    where it has not got to yet, claiming to be heading for where it already
    is.
    """

    CHORNOBYL = ("Реактивний БпЛА через зону відчуження Чорнобильської АЕС "
                 "курсом на Житомирщину.")

    def test_the_position_is_where_it_is_not_where_it_is_going(self):
        got = reports.read(self.CHORNOBYL)
        assert places.lookup(got["place"])["name"] == "Чорнобиль"
        assert got["toward"] == "Житомирщину"
        assert "Чорнобиль" in got["summary"]

    def test_nothing_is_ever_heading_for_where_it_already_is(self):
        # Compared by what the names RESOLVE to, not by the strings.
        # "Житомирщину" and "Житомирська область" are one province written two
        # ways, so a string comparison said they were different.
        got = reports.read("БпЛА над Житомирщиною курсом на Житомирську область")
        assert got["toward"] is None

    def test_the_english_word_order_still_works(self):
        """The first attempt at this cut the sentence at the course phrase.

        That is right for Ukrainian, where the destination comes last, and
        wrong for English: "UAV heading west past Kaharlyk" puts the course
        BEFORE the position, so cutting there threw the position away. Only
        the destination NAME is removed now -- a compass word can never be
        mistaken for a place, so there is nothing to protect against.
        """
        got = reports.read("UAV heading west past Kaharlyk")
        assert places.lookup(got["place"])["name"] == "Кагарлик"
        assert got["course"] == "W"

    def test_a_report_with_only_a_destination_is_still_unplaced(self):
        # It says where something is going and not where it is. Putting it on
        # the destination claims it has arrived.
        got = reports.read("БпЛА курсом на Одесу")
        assert got["place"] is None
        assert got["toward"] == "Одесу"

    def test_a_name_after_a_lowercase_word_or_two_is_still_found(self):
        """"через зону відчуження Чорнобильської АЕС" -- the name is third.

        The preposition patterns wanted a capitalised word immediately after
        the preposition, so every "в районі міста Суми" and "над селищем
        Козелець" read as no place at all.
        """
        for text, wanted in (("БпЛА в районі міста Суми", "Суми"),
                             ("Шахед над селищем Козелець", "Козелець"),
                             ("БпЛА через зону відчуження Чорнобильської АЕС",
                              "Чорнобиль")):
            got = reports.read(text)
            assert places.lookup(got["place"])["name"] == wanted, text

    def test_the_reach_is_bounded(self):
        # The further that reaches, the more likely it is to walk past the
        # phrase and pick up an unrelated name later in the sentence. Four
        # lowercase words is past the limit, so nothing is found -- and a
        # report with no place, no destination and no course is not a report.
        assert reports.read("БпЛА над одним двома трьома чотирма Харковом") is None


class TestTheKindsThatHadNoName:
    """Reports the reader dropped whole, because it could not name them.

    find_kind returning "unknown" makes read() give up, so a post naming a
    weapon this had no pattern for produced nothing at all -- not a grey
    mark, not a row in the panel, nothing. Two of those were common enough
    to matter, and both showed up the same way on the map: everything in the
    air was a drone, because drones were what could be read.
    """

    def test_a_guided_bomb_is_read(self):
        # The map has drawn a "bomb" since NEPTUN started sending them; the
        # reader had no word for one at all.
        for text in ("КАБ у напрямку Вовчанська", "КАБи по Харківщині",
                     "КАБів на Сумщині", "керовані авіабомби по Куп'янську",
                     "Guided bombs over Vovchansk", "glide bomb on Kherson"):
            assert reports.find_kind(text) == "kab", text

    def test_and_folds_to_the_kind_the_map_draws(self):
        from backend import tracker
        assert tracker.fold_kind("kab") == "bomb"

    def test_a_cable_is_not_a_guided_bomb(self):
        # "КАБ" needs its endings to be matched -- КАБи, КАБів, КАБами -- and
        # the lazy way to get those is \\w*, which swallows кабель and кабінет.
        for text in ("кабель у Харкові", "кабінет міністрів",
                     "кабіна пілота"):
            assert reports.find_kind(text) != "kab", text

    def test_a_bare_english_missile_is_read(self):
        # The Russian-side channel posts in English. "Cruise missile" was
        # covered and "missile" was not, so every plain missile report from
        # it was dropped.
        assert reports.find_kind("Missile heading north over Belgorod") == "cruise"
        assert reports.find_kind("Two missiles inbound") == "cruise"

    def test_a_missile_alert_is_still_a_warning_and_not_a_missile(self):
        # "alert" is tried before the weapon words on purpose: there the
        # weapon says what the warning is ABOUT, and find_cause reads it.
        assert reports.find_kind("Voronezh Oblast Missile Alert") == "alert"
        assert reports.find_cause("Voronezh Oblast Missile Alert") == "missile"

    def test_a_high_speed_target_is_a_missile(self):
        # What both sides call one before anybody has identified which.
        assert reports.find_kind("Швидкісна ціль курсом на Кривий Ріг") == "cruise"
        assert reports.find_kind("высокоскоростная цель") == "cruise"

    def test_these_now_reach_the_map_with_a_course(self):
        # The point of the whole change: something other than a drone, with
        # an arrow on it.
        got = reports.read("Missile heading north over Belgorod Oblast")
        assert got["course"] == "N"
        assert got["place"]
