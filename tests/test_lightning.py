"""Tests for the lightning feed.

The decompressor is the part worth testing hard. Everything else in the module
is a socket and a list; `inflate` is pure text-in text-out, it runs on every
single message before anything else looks at it, and when it is wrong the
symptom is corrupt JSON rather than a wrong position -- so it fails loudly, but
only if it is exercised.

The cases below are worked out from the algorithm rather than recorded from the
module, so they can disagree with it.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from backend import lightning


def compress(text: str) -> str:
    """The encoder matching `inflate`, for round-tripping.

    Written here rather than shipped: nothing in the app ever compresses
    anything, and an encoder in the module would be untested code that exists
    only to make a test pass. Kept deliberately simple -- the standard LZW
    encode, emitting one code unit per code.
    """
    if not text:
        return ""
    table = {chr(i): i for i in range(256)}
    next_code = 256
    current = ""
    out: list[int] = []
    for char in text:
        nxt = current + char
        if nxt in table:
            current = nxt
        else:
            out.append(table[current])
            table[nxt] = next_code
            next_code += 1
            current = char
    if current:
        out.append(table[current])
    return "".join(chr(c) for c in out)


class TestInflate:
    def test_empty(self):
        assert lightning.inflate("") == ""

    def test_text_with_no_repeats_is_its_own_encoding(self):
        # With nothing to learn, every code is a literal character and the
        # message passes through untouched.
        plain = "abcdefg"
        assert lightning.inflate(plain) == plain

    def test_a_hand_worked_expansion(self):
        # Codes: 'a', 'b', 256, 258.
        # After 'a','b' the table holds 256="ab", so the third code expands to
        # "ab" and the table learns 257="ba". The fourth code, 258, has not
        # been defined yet -- so it is the self-referential case, and can only
        # be the previous string "ab" plus its own first character: "aba".
        packed = "ab" + chr(256) + chr(258)
        assert lightning.inflate(packed) == "a" + "b" + "ab" + "aba"

    def test_the_self_referential_code(self):
        # The awkward case reached the way it is actually reached: this is
        # exactly what the encoder emits for "aaaaaa". The second code names
        # the entry being defined by that very step, and there is no way to
        # resolve it except from the string before it.
        #
        # Worth spelling out, because a plausible-looking sequence that the
        # encoder would never emit is easy to write and proves nothing.
        packed = "a" + chr(256) + chr(257)
        assert packed == compress("aaaaaa")
        assert lightning.inflate(packed) == "aaaaaa"

    @pytest.mark.parametrize("text", [
        "a",
        "aaaaaaaaaa",
        "abababababab",
        '{"time":1712345678901234567,"lat":51.5,"lon":-0.12,"sig":[]}',
        '{"lat":1,"lat":1,"lat":1,"lat":1,"lat":1,"lat":1}',
        "banana bandana bandanna",
        "".join(chr(32 + (i * 7) % 90) for i in range(500)),
    ])
    def test_round_trip(self, text):
        assert lightning.inflate(compress(text)) == text

    def test_a_real_shaped_message_survives(self):
        message = {
            "time": 1712345678901234567, "lat": 52.1234, "lon": 4.5678,
            "alt": 0, "pol": 0, "mds": 1234, "mcg": 180, "status": 0,
            "region": 1, "sig": [{"sta": 1, "time": 1}, {"sta": 2, "time": 2}],
        }
        text = json.dumps(message)
        assert json.loads(lightning.inflate(compress(text))) == message


class TestStroke:
    def test_a_normal_message(self):
        now = time.time()
        got = lightning._stroke(
            {"time": 1_712_345_678_901_234_567, "lat": 51.5, "lon": -0.12,
             "sig": [1, 2, 3]}, now)
        assert got["lat"] == 51.5
        assert got["lon"] == -0.12
        assert got["stations"] == 3
        # Nanoseconds, not seconds or milliseconds: getting this wrong puts
        # every stroke in 1970 or in the year 56000, and either way the map
        # shows nothing while the feed looks healthy.
        assert 1_712_345_678 < got["time"] < 1_712_345_680

    def test_a_message_with_no_position_is_not_a_stroke(self):
        now = time.time()
        assert lightning._stroke({"time": 1}, now) is None
        assert lightning._stroke({"lat": 51.5}, now) is None
        assert lightning._stroke({"lat": "51.5", "lon": 0}, now) is None

    def test_positions_off_the_globe_are_refused(self):
        now = time.time()
        assert lightning._stroke({"lat": 91, "lon": 0}, now) is None
        assert lightning._stroke({"lat": 0, "lon": 181}, now) is None

    def test_a_message_with_no_time_is_stamped_on_arrival(self):
        now = time.time()
        got = lightning._stroke({"lat": 0, "lon": 0}, now)
        assert got["time"] == now


class TestBuffer:
    def test_only_strokes_inside_the_box_and_the_window_come_back(self):
        listener = lightning.Listener()
        now = time.time()
        listener._strokes = [
            {"lat": 51.5, "lon": -0.1, "time": now - 60, "stations": 5},    # in
            {"lat": 51.5, "lon": -0.1, "time": now - 4000, "stations": 5},  # too old
            {"lat": 10.0, "lon": -0.1, "time": now - 60, "stations": 5},    # outside
        ]
        got = listener.recent(-1, 51, 1, 52, minutes=30)
        assert len(got) == 1
        assert got[0]["time"] == pytest.approx(now - 60)

    def test_old_strokes_are_dropped(self):
        listener = lightning.Listener()
        now = time.time()
        listener._strokes = [
            {"lat": 0, "lon": 0, "time": now - lightning.KEEP_SECONDS - 10},
            {"lat": 0, "lon": 0, "time": now - 5},
        ]
        listener._prune(now)
        assert len(listener._strokes) == 1

    def test_the_buffer_has_a_ceiling(self):
        listener = lightning.Listener()
        now = time.time()
        listener._strokes = [
            {"lat": 0, "lon": 0, "time": now} for _ in range(lightning.MAX_STROKES + 500)
        ]
        listener._prune(now)
        assert len(listener._strokes) == lightning.MAX_STROKES

    def test_a_junk_message_is_ignored_rather_than_raising(self):
        listener = lightning.Listener()
        for junk in ["", "not json", chr(300) + chr(301), "{}", "[1,2,3]"]:
            listener._take(junk)
        assert listener._strokes == []


class TestDemo:
    def test_everything_lands_inside_the_box(self):
        box = (-5.0, 50.0, 5.0, 55.0)
        got = lightning.demo(*box, minutes=30)
        assert got["count"] > 0
        for stroke in got["strokes"]:
            assert box[0] <= stroke["lon"] <= box[2]
            assert box[1] <= stroke["lat"] <= box[3]

    def test_nothing_is_older_than_the_window(self):
        now = time.time()
        got = lightning.demo(-5, 50, 5, 55, minutes=10)
        for stroke in got["strokes"]:
            assert now - 10 * 60 - 1 <= stroke["time"] <= now + 1

    def test_strokes_arrive_in_time_order(self):
        got = lightning.demo(-5, 50, 5, 55)
        times = [s["time"] for s in got["strokes"]]
        assert times == sorted(times)

    def test_they_cluster_rather_than_scatter(self):
        # Real lightning comes in storms. A uniform sprinkle is the easy thing
        # to generate and looks wrong on a map in a way that is hard to name,
        # so the demo is checked for being lumpy: the average distance between
        # neighbours should be well under what an even spread would give.
        got = lightning.demo(-5.0, 50.0, 5.0, 55.0, minutes=30)
        points = [(s["lon"], s["lat"]) for s in got["strokes"]]
        assert len(points) > 20

        def nearest(i):
            x, y = points[i]
            return min(math.hypot(x - a, y - b)
                       for j, (a, b) in enumerate(points) if j != i)

        mean_gap = sum(nearest(i) for i in range(len(points))) / len(points)
        # For n points spread evenly over a w by h box the typical nearest
        # neighbour sits around 0.5*sqrt(area/n); clustered points come in far
        # under that.
        even = 0.5 * math.sqrt((10.0 * 5.0) / len(points))
        assert mean_gap < even * 0.6, f"gap {mean_gap:.4f} vs even {even:.4f}"
