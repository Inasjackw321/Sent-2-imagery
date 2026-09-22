"""Saying why a service did not answer, once and in a few words.

Two things are tested here and they are the same bug from two ends. A
requests exception says the same thing forty times in three hundred
characters, and a host that stops answering makes every request in flight
say it separately. The log that came out of that -- a wall of identical
read timeouts around one useful sentence -- is what these are about.
"""

from __future__ import annotations

import requests

from backend import app, reasons


class TestWhy:
    def test_a_read_timeout_is_three_words(self):
        said = reasons.why(requests.ConnectionError(
            "HTTPSConnectionPool(host='view.eumetsat.int', port=443): Max "
            "retries exceeded with url: /geoserver/service?SERVICE=WMS&"
            "REQUEST=GetMap&LAYERS=mtg_fd%3Argb_truecolour (Caused by "
            "ReadTimeoutError(\"HTTPSConnectionPool(host='view.eumetsat.int', "
            "port=443): Read timed out. (read timeout=40)\"))"))
        assert said == "Read timed out"

    def test_a_proxy_refusal_is_named_rather_than_the_retry_loop(self):
        said = reasons.why(requests.ConnectionError(
            "Max retries exceeded with url: /x (Caused by ProxyError("
            "'Unable to connect to proxy', OSError('Tunnel connection "
            "failed: 403 Forbidden')))"))
        assert said == "Tunnel connection failed"

    def test_the_retry_loop_is_the_last_resort_rather_than_the_first(self):
        """It wraps nearly every other cause, so matching it early would
        report the wrapper and throw away what actually happened."""
        assert reasons.CAUSES[-1] == "Max retries exceeded"

    def test_the_retry_loop_is_still_better_than_nothing(self):
        assert reasons.why(Exception("Max retries exceeded with url: /x")) \
            == "Max retries exceeded"

    def test_a_name_that_does_not_resolve_is_named(self):
        assert reasons.why(Exception("[Errno -2] Name or service not known")) \
            == "Name or service not known"

    def test_something_nobody_has_seen_before_is_merely_trimmed(self):
        said = reasons.why(Exception("wobble " * 40))
        assert said.endswith("…")
        assert len(said) == reasons.LIMIT + 1

    def test_a_short_reason_comes_through_whole(self):
        assert reasons.why(Exception("no route to host")) == "no route to host"

    def test_newlines_are_folded_away(self):
        """It goes on one line of a log and one line of a panel."""
        assert "\n" not in reasons.why(Exception("gave\nup\nentirely"))

    def test_an_empty_reason_stays_empty(self):
        assert reasons.why(Exception("")) == ""

    def test_a_string_is_accepted_as_well_as_an_exception(self):
        assert reasons.why("Connection refused") == "Connection refused"


class TestSayingItOnce:
    """The log dedupe. Pure, so the counting can be tested without a clock."""

    FRESH = ("", 0.0, 0)

    def test_the_first_complaint_is_written(self):
        write, said, _ = app.worth_saying("EUMETSAT is quiet", 100.0, self.FRESH)
        assert write
        assert said == "EUMETSAT is quiet"

    def test_the_same_one_again_is_not(self):
        _, _, state = app.worth_saying("quiet", 100.0, self.FRESH)
        write, _, _ = app.worth_saying("quiet", 101.0, state)
        assert not write

    def test_forty_of_them_are_one_line(self):
        state = self.FRESH
        written = 0
        for n in range(40):
            write, _, state = app.worth_saying("quiet", 100.0 + n * 0.01, state)
            written += write
        assert written == 1

    def test_the_ones_held_back_are_counted_into_the_next_line(self):
        state = self.FRESH
        for n in range(5):
            _, _, state = app.worth_saying("quiet", 100.0 + n * 0.01, state)
        write, said, _ = app.worth_saying("something else", 101.0, state)
        assert write
        assert said == "something else (and 4 more like the last one)"

    def test_a_different_failure_is_never_swallowed(self):
        """The point of the dedupe is that other failures stay findable."""
        _, _, state = app.worth_saying("EUMETSAT is quiet", 100.0, self.FRESH)
        write, said, _ = app.worth_saying("the catalogue moved", 100.1, state)
        assert write
        assert said.startswith("the catalogue moved")

    def test_the_same_one_after_the_window_is_written_again(self):
        """A service still down a minute later is worth saying again, or a
        long outage looks like one blip."""
        _, _, state = app.worth_saying("quiet", 100.0, self.FRESH)
        write, _, _ = app.worth_saying(
            "quiet", 100.0 + app.SAY_AGAIN_SECONDS + 1, state)
        assert write

    def test_nothing_was_held_back_means_nothing_is_appended(self):
        _, _, state = app.worth_saying("quiet", 100.0, self.FRESH)
        _, said, _ = app.worth_saying("other", 200.0, state)
        assert said == "other"

    def test_the_count_starts_again_after_a_line_is_written(self):
        state = self.FRESH
        for n in range(3):
            _, _, state = app.worth_saying("quiet", 100.0 + n * 0.01, state)
        _, _, state = app.worth_saying("other", 101.0, state)
        _, said, _ = app.worth_saying("other again", 101.1, state)
        assert "more like" not in said

    def test_the_window_is_not_so_long_that_an_outage_goes_unreported(self):
        assert 10 <= app.SAY_AGAIN_SECONDS <= 300

    def test_what_the_browser_is_told_is_never_abridged(self):
        """The dedupe is about the log. The caller still gets the reason."""
        first = app._fail(RuntimeError("EUMETSAT View could not be reached"))
        again = app._fail(RuntimeError("EUMETSAT View could not be reached"))
        assert again.detail == first.detail
        assert "could not be reached" in again.detail
