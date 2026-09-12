"""The basemap list and the security policy have to agree.

These live in different files and different languages -- the basemaps in
frontend/js/tiles.js, the Content-Security-Policy in backend/app.py -- and a
basemap whose host is missing from the policy does not fail in any way that
looks like a missing policy line. The browser refuses the tiles, so the map is
blank or falls back, and the only clue is a console message nobody has open.

That has already happened once during this work: a basemap was changed, the
policy was not, and the tiles were blocked by the page itself rather than by
the service. It was found by watching the console in a browser test, which is
not a thing that happens reliably.

So the two are checked against each other here. This reads tiles.js as text
rather than executing it, which is crude and is the point: no JavaScript
runtime, no build step, nothing to keep in sync. It just has to notice when a
host appears on one side and not the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app import CSP

TILES_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "tiles.js"


def basemap_hosts() -> set[str]:
    """The hosts the basemap list would actually contact."""
    source = TILES_JS.read_text(encoding="utf-8")

    # Most of these URLs are template literals built from a shared prefix
    # constant, so the constants have to be resolved before any host can be
    # read out. Collected from the whole file, applied inside the array.
    prefixes = dict(re.findall(
        r"const\s+([A-Z_][A-Z_0-9]*)\s*=\s*['\"](https://[^'\"]+)['\"]", source))

    # Only the BASEMAPS array, so the vetted-but-unused entries in
    # KEYLESS_HOSTS are not demanded of the policy.
    start = source.index("export const BASEMAPS")
    end = source.index("export const DEFAULT_BASEMAP")
    body = source[start:end]

    hosts = set()
    for match in re.finditer(r"url:\s*[`'\"]([^`'\"]+)", body):
        url = match.group(1)
        for name, value in prefixes.items():
            url = url.replace("${" + name + "}", value)
        assert url.startswith("https://"), f"unresolved basemap url: {url}"
        host = url.split("://", 1)[1].split("/", 1)[0]
        # Leaflet's subdomain placeholder stands for a real label.
        hosts.add(host.replace("{s}.", ""))
    return hosts


def policy(directive: str) -> str:
    for part in CSP.split(";"):
        if part.strip().startswith(directive):
            return part.strip()
    raise AssertionError(f"no {directive} in the policy at all")


def allows(directive: str, host: str) -> bool:
    sources = policy(directive).split()[1:]
    for source in sources:
        if source == f"https://{host}":
            return True
        # A wildcard covers one or more leading labels: *.example.com matches
        # a.example.com but, per the spec, not example.com itself.
        if source.startswith("https://*."):
            if host.endswith(source.removeprefix("https://*.")) and host.count(".") > 1:
                return True
    return False


def test_the_basemap_list_was_found_and_read() -> None:
    # If the parsing ever stops working, every test below would pass by
    # checking nothing at all.
    hosts = basemap_hosts()
    assert hosts, "no basemap hosts parsed out of tiles.js — the parsing broke"
    assert all("{" not in host for host in hosts), hosts


@pytest.mark.parametrize("directive", ["img-src", "connect-src"])
def test_every_basemap_host_is_allowed_by_the_policy(directive: str) -> None:
    # img-src so the tiles can be drawn, connect-src so the status probe can
    # fetch one and read the code it came with. A host missing from either is a
    # basemap that quietly does not work.
    for host in sorted(basemap_hosts()):
        assert allows(directive, host), (
            f"{host} is used by a basemap but not allowed by {directive}. "
            f"Add it to the CSP in backend/app.py, or the browser will block "
            f"the tiles and the map will look like the service is down."
        )


def test_the_policy_does_not_allow_tile_hosts_it_no_longer_needs() -> None:
    # The other direction, which is hygiene rather than breakage: a policy that
    # accumulates hosts nobody uses stops being a statement of what the page
    # talks to. Only the known non-basemap image sources are exempt.
    others = {
        "api.rainviewer.com", "gibs.earthdata.nasa.gov", "imgproxy.windy.com",
        "www.ndbc.noaa.gov", "airtw.moenv.gov.tw", "ristmikud.tallinn.ee",
        "pics.starvisor.net", "www.customs.gov.by", "eismoinfo.lt",
        "view.eumetsat.int", "cdn.jsdelivr.net",
    }
    # A wildcard source stands for any host under it, so it is accounted for
    # by anything it would cover rather than by an exact string match.
    def accounted_for(source: str) -> bool:
        bare = source.removeprefix("https://")
        known = others | basemap_hosts()
        if bare.startswith("*."):
            suffix = bare.removeprefix("*.")
            return any(host.endswith(suffix) for host in known)
        return bare in known

    stale = {
        source for source in policy("img-src").split()[1:]
        if source.startswith("https://") and not accounted_for(source)
    }
    assert not stale, (
        f"img-src allows {sorted(stale)}, which no basemap uses and which is "
        f"not a known non-basemap source. Remove it, or add it to the exempt "
        f"list here if it is one."
    )
