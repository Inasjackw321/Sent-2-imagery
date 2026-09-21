"""Tests for the lookout: a model asked the same questions about every square.

The thing this has to get right is not the arithmetic of a grid. It is what
counts as an answer. OpenJev returns a yes/no question as P(yes), and the
whole value of a sweep is that a marked square is worth walking over to --
so a square marked because the model said 0.51 is worse than no sweep at all,
and a square skipped because one question was unsure has to say which one.

api.codiv.ai cannot be reached from where these run, so the model is a
stand-in that answers exactly what the published API says it answers. Every
claim here is about this app's half of that contract.
"""

from __future__ import annotations

import json

import pytest

from backend import lookout


@pytest.fixture(autouse=True)
def clean():
    lookout.forget()
    lookout.reset()
    lookout.set_key("")
    yield
    lookout.forget()
    lookout.reset()
    lookout.set_key("")


class Reply:
    """What requests.post gives back, near enough."""

    def __init__(self, body=None, code=200):
        self._body = body if body is not None else {"answers": {}}
        self.status_code = code
        self.ok = 200 <= code < 300

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def yes(p=0.95):
    return {"noul": p}


def choice(name, *odds):
    return {"choice": name, "probabilities": list(odds), "confidence": 0.9}


def answers(building="yes", circle=0.95, refinery=0.02, over=0.92):
    """A reply that is a hit unless one of these is changed."""
    return {"answers": {
        "is_there_a_building": choice(building, 0.95, 0.05)
        if building == "yes" else choice(building, 0.05, 0.95),
        "is_there_a_colour_circle": yes(circle),
        "refinery": yes(refinery),
        "circle_and_building": yes(over),
    }}


class TestTheQuestionsThatShipped:
    """The four in the schema, and the pattern that makes a hit.

    They are data rather than code, so they are checked as data: a file edited
    to something plausible but wrong would otherwise change what the sweep
    marks with nothing failing.
    """

    def test_all_four_are_there_in_order(self):
        assert [q["id"] for q in lookout.questions()] == [
            "is_there_a_building", "is_there_a_colour_circle",
            "refinery", "circle_and_building"]

    def test_the_pattern_is_yes_yes_no_yes(self):
        # Asked for in as many words: 1 yes, 2 yes, 3 no, 4 yes.
        assert [q["hit"] for q in lookout.questions()] == \
            ["yes", "yes", "no", "yes"]

    def test_every_question_says_what_it_asks(self):
        for question in lookout.questions():
            assert question.get("ask"), question["id"]
            assert question.get("type") in ("noul", "choice"), question["id"]

    def test_a_choice_question_carries_its_options(self):
        first = lookout.questions()[0]
        assert first["type"] == "choice"
        assert set(first["criteria"]) == {"yes", "no"}

    def test_an_unreadable_file_is_no_questions_rather_than_a_crash(self,
                                                                   monkeypatch):
        monkeypatch.setattr(lookout, "QUESTIONS_FILE",
                            lookout.QUESTIONS_FILE.with_name("nope.json"))
        lookout.forget()
        assert lookout.questions() == []


class TestWhatIsSentToTheModel:
    def test_the_schema_is_the_shape_the_api_takes(self):
        got = lookout.schema(lookout.questions())
        assert got["refinery"] == {"type": "noul",
                                   "instructions": "is it a refinery or a place with oil"}
        assert got["is_there_a_building"]["type"] == "choice"
        assert got["is_there_a_building"]["criteria"] == {"yes": "", "no": ""}

    def test_what_this_app_keeps_for_itself_is_not_sent(self):
        # `hit` is this app's rule, not a question. Sending it would be
        # telling the model which answer is wanted.
        for body in lookout.schema(lookout.questions()).values():
            assert "hit" not in body
            assert set(body) <= {"type", "instructions", "criteria"}

    def test_a_question_of_a_type_the_api_has_no_name_for_is_left_out(self):
        got = lookout.schema([{"id": "x", "type": "essay", "ask": "why"}])
        assert got == {}

    def test_nothing_is_asked_without_a_key(self):
        with pytest.raises(lookout.LookoutError, match="no Codiv API key"):
            lookout.ask(["data:image/png;base64,aaa"], "Kyiv", 10)

    def test_the_key_is_sent_as_a_bearer_token(self):
        seen = {}

        def post(url, json=None, timeout=None, headers=None):
            seen.update({"url": url, "body": json, "headers": headers})
            return Reply(answers())

        lookout.set_key("sk-codiv-test")
        lookout.ask(["data:image/png;base64,aaa"], "Kyiv", 10, post=post)
        assert seen["headers"]["Authorization"] == "Bearer sk-codiv-test"
        assert seen["url"] == lookout.ENDPOINT

    def test_the_model_asked_for_is_openjev(self):
        seen = {}
        lookout.set_key("k")
        lookout.ask(["x"], "Kyiv", 10,
                    post=lambda url, json=None, **kw: (seen.update(json),
                                                       Reply(answers()))[1])
        assert seen["model"] == lookout.MODEL
        assert "openjev" in lookout.MODEL

    def test_the_picture_goes_with_the_questions(self):
        seen = {}
        lookout.set_key("k")
        lookout.ask(["data:image/png;base64,aaa"], "Kyiv", 10,
                    post=lambda url, json=None, **kw: (seen.update(json),
                                                       Reply(answers()))[1])
        assert seen["images"] == ["data:image/png;base64,aaa"]
        assert "Kyiv" in seen["state"]

    def test_a_square_is_rendered_into_the_form_the_api_takes(self):
        got = lookout.as_data_url(b"\x89PNG", "image/png")
        assert got.startswith("data:image/png;base64,")

    @pytest.mark.parametrize("code,says", [
        (401, "refused the key"), (429, "rate limiting"), (503, "503")])
    def test_a_refusal_is_said_rather_than_read_as_no(self, code, says):
        # The failure that would matter most: a sweep that turns a refused
        # request into "not a hit" marks nothing and explains nothing.
        lookout.set_key("k")
        with pytest.raises(lookout.LookoutError, match=says):
            lookout.ask(["x"], "Kyiv", 10,
                        post=lambda *a, **kw: Reply({}, code))

    def test_and_so_is_a_service_that_cannot_be_reached(self):
        def refuse(*a, **kw):
            raise lookout.requests.RequestException("no route")

        lookout.set_key("k")
        with pytest.raises(lookout.LookoutError, match="could not be reached"):
            lookout.ask(["x"], "Kyiv", 10, post=refuse)

    def test_and_a_reply_that_is_not_json(self):
        lookout.set_key("k")
        with pytest.raises(lookout.LookoutError, match="not JSON"):
            lookout.ask(["x"], "Kyiv", 10,
                        post=lambda *a, **kw: Reply(ValueError("nope")))


class TestReadingAnAnswer:
    """P(yes) = 0.51 is the model saying it does not know."""

    NOUL = {"id": "q", "type": "noul", "ask": "q"}
    CHOICE = {"id": "q", "type": "choice", "ask": "q",
              "criteria": {"yes": "", "no": ""}}

    def test_a_confident_yes_is_yes(self):
        assert lookout.read_answer(self.NOUL, {"noul": 0.9})["said"] == "yes"

    def test_a_confident_no_is_no(self):
        assert lookout.read_answer(self.NOUL, {"noul": 0.05})["said"] == "no"

    def test_a_coin_toss_is_neither(self):
        for p in (0.45, 0.5, 0.51, 0.6):
            assert lookout.read_answer(self.NOUL, {"noul": p})["said"] == "unsure", p

    def test_the_line_is_where_it_says_it_is(self):
        assert lookout.read_answer(
            self.NOUL, {"noul": lookout.SURE_AT})["said"] == "yes"
        assert lookout.read_answer(
            self.NOUL, {"noul": 1 - lookout.SURE_AT})["said"] == "no"

    def test_the_number_is_kept_whatever_the_answer(self):
        # "Unsure" with no figure beside it is a result nobody can act on,
        # and how close it was is the first thing anybody asks.
        for p in (0.9, 0.55, 0.01):
            assert lookout.read_answer(self.NOUL, {"noul": p})["p"] == p

    def test_a_chosen_option_is_the_answer(self):
        got = lookout.read_answer(self.CHOICE, choice("yes", 0.9, 0.1))
        assert got["said"] == "yes"
        assert got["p"] == 0.9

    def test_a_chosen_option_nobody_is_sure_of_is_not(self):
        assert lookout.read_answer(
            self.CHOICE, choice("yes", 0.55, 0.45))["said"] == "unsure"

    def test_the_probability_read_is_the_chosen_one(self):
        # Not the first. Reading probabilities[0] regardless would call every
        # confident "no" an unsure one, and never mark a thing.
        got = lookout.read_answer(self.CHOICE, choice("no", 0.08, 0.92))
        assert got["said"] == "no"
        assert got["p"] == 0.92

    def test_a_choice_with_no_probabilities_falls_back_on_confidence(self):
        got = lookout.read_answer(self.CHOICE,
                                  {"choice": "yes", "confidence": 0.5})
        assert got["said"] == "yes"
        assert got["confidence"] == 0.5

    def test_and_confidence_is_held_to_its_own_number(self):
        """confidence is 1 - H(p)/ln K, which is not a probability.

        For two options it is 0.278 at p = 0.8 and 0.531 at p = 0.9, so
        comparing it against the probability threshold would demand near
        certainty of every choice question and never mark anything.
        """
        assert lookout.CONFIDENCE_SURE_AT < lookout.CHOICE_SURE_AT
        assert lookout.read_answer(
            self.CHOICE, {"choice": "yes", "confidence": 0.1})["said"] == "unsure"

    def test_an_answer_that_did_not_arrive_is_unsure(self):
        for given in (None, {}, "yes", {"noul": "high"}):
            assert lookout.read_answer(self.NOUL, given)["said"] == "unsure", given

    def test_a_choice_with_no_choice_in_it_is_unsure(self):
        assert lookout.read_answer(self.CHOICE, {"probabilities": [1, 0]})["said"] \
            == "unsure"

    def test_every_question_asked_gets_a_line_back(self):
        got = lookout.read_answers({"answers": {}}, lookout.questions())
        assert set(got) == {q["id"] for q in lookout.questions()}
        assert all(a["said"] == "unsure" for a in got.values())


class TestWhatMakesAHit:
    def test_the_pattern_that_was_asked_for(self):
        got = lookout.read_answers(answers(), lookout.questions())
        assert lookout.verdict(got, lookout.questions())["hit"] is True

    @pytest.mark.parametrize("change,who", [
        ({"building": "no"}, "is_there_a_building"),
        ({"circle": 0.02}, "is_there_a_colour_circle"),
        ({"refinery": 0.95}, "refinery"),
        ({"over": 0.03}, "circle_and_building"),
    ])
    def test_any_one_answer_the_other_way_is_not(self, change, who):
        got = lookout.read_answers(answers(**change), lookout.questions())
        said = lookout.verdict(got, lookout.questions())
        assert said["hit"] is False
        assert said["missed"] == [who]

    @pytest.mark.parametrize("change,who", [
        ({"circle": 0.5}, "is_there_a_colour_circle"),
        ({"refinery": 0.5}, "refinery"),
        ({"over": 0.55}, "circle_and_building"),
    ])
    def test_and_any_one_the_model_is_unsure_of_is_not_either(self, change, who):
        got = lookout.read_answers(answers(**change), lookout.questions())
        said = lookout.verdict(got, lookout.questions())
        assert said["hit"] is False
        assert said["unsure"] == [who]
        assert said["missed"] == []

    def test_a_question_with_no_rule_is_asked_and_decides_nothing(self):
        # So a fifth question can be added for interest without silently
        # changing what the sweep marks.
        asked = lookout.questions() + [{"id": "extra", "type": "noul",
                                        "ask": "anything else"}]
        got = lookout.read_answers(answers(), asked)
        assert lookout.verdict(got, asked)["hit"] is True

    def test_nothing_answered_is_not_a_hit(self):
        got = lookout.read_answers({"answers": {}}, lookout.questions())
        assert lookout.verdict(got, lookout.questions())["hit"] is False


class TestCuttingUpAnArea:
    def test_kyiv_is_an_area_this_app_knows(self):
        assert "kyiv" in lookout.AREAS
        south, north, west, east = lookout.AREAS["kyiv"]["bbox"]
        # The city itself has to be inside it, or the sweep looks at the
        # wrong ground and nothing says so.
        assert south < 50.4501 < north and west < 30.5234 < east

    def test_an_area_becomes_squares(self):
        got = lookout.squares(lookout.AREAS["kyiv"])
        assert 20 < len(got) < lookout.MOST_SQUARES

    def test_the_squares_cover_the_whole_area_with_no_gaps(self):
        # A sweep that quietly skips its own eastern edge is worse than one
        # with a narrow column in it.
        area = lookout.AREAS["kyiv"]
        south, north, west, east = area["bbox"]
        got = lookout.squares(area)
        assert min(s["bbox"][0] for s in got) == pytest.approx(south)
        assert max(s["bbox"][1] for s in got) == pytest.approx(north)
        assert min(s["bbox"][2] for s in got) == pytest.approx(west)
        assert max(s["bbox"][3] for s in got) == pytest.approx(east)

    def test_and_do_not_overlap(self):
        got = lookout.squares(lookout.AREAS["kyiv"])
        seen = set()
        for square in got:
            key = (round(square["lat"], 6), round(square["lon"], 6))
            assert key not in seen
            seen.add(key)

    def test_every_square_holds_its_own_middle(self):
        """Its MIDDLE, not a corner.

        A corner satisfies "inside the square" and is on the boundary with
        the next one: the hit would be drawn on the seam between two squares,
        and the row in the panel would name a point that is as much in its
        neighbour as in it.
        """
        for square in lookout.squares(lookout.AREAS["kyiv"]):
            south, north, west, east = square["bbox"]
            assert south < square["lat"] < north, square["id"]
            assert west < square["lon"] < east, square["id"]
            assert square["lat"] == pytest.approx((south + north) / 2)
            assert square["lon"] == pytest.approx((west + east) / 2)

    def test_a_square_is_about_the_size_it_says(self):
        got = lookout.squares(lookout.AREAS["kyiv"], km=10)
        south, north, west, east = got[0]["bbox"]
        tall = (north - south) * lookout.RAD * lookout.EARTH_KM
        assert 5 < tall <= 10.5, tall

    def test_longitude_is_narrowed_by_latitude(self):
        # At 50°N a degree of longitude is two thirds of a degree of latitude.
        # Ignoring that makes the squares half as wide again as they say.
        far = {"bbox": (50.0, 51.0, 30.0, 31.0)}
        got = lookout.squares(far, km=10)
        rows = len({s["id"].split("-")[0] for s in got})
        cols = len({s["id"].split("-")[1] for s in got})
        assert rows > cols

    def test_an_area_too_big_to_sweep_is_refused_rather_than_truncated(self):
        # Half a sweep silently presented as a whole one is the worst
        # available answer: the squares it never looked at look like misses.
        whole = {"bbox": (35.0, 60.0, 0.0, 60.0)}
        assert lookout.squares(whole, km=10) == []

    def test_a_backwards_area_is_no_squares(self):
        assert lookout.squares({"bbox": (51.0, 50.0, 31.0, 30.0)}) == []
        assert lookout.squares(lookout.AREAS["kyiv"], km=0) == []

    def test_a_square_becomes_the_polygon_the_renderer_takes(self):
        got = lookout.square_polygon((50.0, 50.1, 30.0, 30.1))
        assert got["type"] == "Polygon"
        ring = got["coordinates"][0]
        assert len(ring) == 5 and ring[0] == ring[-1]
        assert ring[0] == [30.0, 50.0]


class TestASweep:
    """End to end, with a stand-in satellite and a stand-in model."""

    def picture(self, square):
        return f"data:image/png;base64,{square['id']}"

    def run(self, reply=None, picture=None):
        lookout.set_key("k")
        got = reply if callable(reply) else (lambda *a, **kw: Reply(
            reply if reply is not None else answers()))
        return lookout.sweep("kyiv", picture or self.picture, post=got)

    def test_every_square_is_looked_at(self):
        said = self.run()
        assert said["done"] == said["of"] == len(
            lookout.squares(lookout.AREAS["kyiv"]))
        assert len(said["looked"]) == said["of"]

    def test_a_matching_square_is_a_hit(self):
        said = self.run()
        assert len(said["hits"]) == said["of"]
        assert all(h["lat"] and h["lon"] for h in said["hits"])

    def test_a_square_that_does_not_match_is_not(self):
        said = self.run(reply=answers(refinery=0.99))
        assert said["hits"] == []
        assert all(s["missed"] == ["refinery"] for s in said["looked"])

    def test_one_square_that_fails_does_not_end_the_sweep(self):
        # Cloud over one square, or one refused request, is a square this
        # sweep cannot answer for -- and forty-eight squares with one gap is
        # a result where nothing at all is not.
        state = {"n": 0}

        def sometimes(*a, **kw):
            state["n"] += 1
            if state["n"] == 3:
                raise lookout.requests.RequestException("dropped")
            return Reply(answers())

        said = self.run(reply=sometimes)
        blank = [s for s in said["looked"] if s.get("trouble")]
        assert len(blank) == 1
        assert said["done"] == said["of"]
        assert len(said["hits"]) == said["of"] - 1

    def test_and_a_square_that_will_not_render_is_the_same(self):
        def broken(square):
            if square["id"] == "0-1":
                raise ValueError("no pixels there")
            return self.picture(square)

        said = self.run(picture=broken)
        assert [s["id"] for s in said["looked"] if s.get("trouble")] == ["0-1"]

    def test_a_square_that_could_not_be_answered_for_is_not_a_hit(self):
        # The distinction the whole panel rests on: "no" and "we could not
        # tell" must never be drawn as the same thing.
        said = self.run(reply=lambda *a, **kw: Reply({}, 503))
        assert said["hits"] == []
        assert all(s["trouble"] for s in said["looked"])

    def test_an_area_nobody_has_heard_of_is_refused(self):
        lookout.set_key("k")
        with pytest.raises(lookout.LookoutError, match="not an area"):
            lookout.sweep("atlantis", self.picture)

    def test_the_sweep_says_when_it_is_finished(self):
        said = self.run()
        assert said["running"] is False

    def test_it_can_be_stopped_partway(self):
        state = {"n": 0}

        def counting(*a, **kw):
            state["n"] += 1
            if state["n"] == 2:
                lookout.stop()
            return Reply(answers())

        said = self.run(reply=counting)
        assert said["done"] < said["of"]
        assert said["trouble"] == "stopped"

    def test_the_state_a_poll_sees_carries_what_the_panel_needs(self):
        self.run()
        said = lookout.state()
        assert set(said) >= {"running", "area", "of", "done", "hits", "looked",
                             "areas", "questions", "have_key", "square_km"}
        assert said["areas"][0]["key"] == "kyiv"
        assert [q["hit"] for q in said["questions"]] == ["yes", "yes", "no", "yes"]

    def test_the_key_never_leaves_this_process(self):
        # Not in the state a browser polls, and not in the areas or the
        # questions beside it.
        lookout.set_key("sk-codiv-secret")
        said = json.dumps(lookout.state())
        assert "sk-codiv-secret" not in said
        assert lookout.state()["have_key"] is True

    def test_and_it_can_be_taken_away(self):
        lookout.set_key("sk-codiv-secret")
        assert lookout.set_key("") is False
        assert lookout.have_key() is False
