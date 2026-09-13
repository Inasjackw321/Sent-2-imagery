"""Test-wide defaults.

One thing only: no test reaches neptun.in.ua.

The tracker reads that feed on every poll, and dozens of tests call poll().
Left alone they would each make a real request -- slow, dependent on somebody
else's uptime, and rude to a free service that asks for one call every five
seconds. So the fetch is stubbed out for the whole suite and the tests that
are ABOUT the feed put their own answer in.

Stubbing `_get` rather than `threats`/`alerts` on purpose: it is the single
point where this app touches the network, so a new endpoint added later is
caught by the same stub instead of quietly escaping it.
"""

from __future__ import annotations

import pytest

from backend import neptun


@pytest.fixture(autouse=True)
def no_neptun_network(monkeypatch):
    def refuse(url, **kw):
        raise neptun.NeptunError(
            f"the test suite does not reach the network ({url})")

    # The real one is stashed rather than lost. One test is about the pacing
    # that lives INSIDE _get -- skip rather than wait -- and it cannot check
    # that through a stub that replaced the thing being checked.
    monkeypatch.setattr(neptun, "unstubbed_get", neptun._get, raising=False)
    monkeypatch.setattr(neptun, "_get", refuse)
    neptun.forget()
    yield
    neptun.forget()
