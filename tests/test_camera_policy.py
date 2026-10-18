"""The camera list and the security policy have to agree.

Same reason as the basemap check next door, and the same failure shape: a
camera whose host is missing from the Content-Security-Policy does not fail
like a policy problem. The browser refuses the playlist, the player shows an
error, and the camera looks broken at the far end -- which is exactly what a
camera that really is off looks like. The only clue is a console message
nobody has open.

The list is JavaScript and the policy is Python, so this reads the list as
text rather than running it. Crude on purpose: no runtime, no build step,
nothing to keep in sync. It only has to notice when a host appears on one
side and not the other.

Which directive a camera needs depends on how it is drawn, and that is the
part worth getting right:

  a still is an <img>, so img-src;
  an HLS stream is fetched by hls.js and fed to a <video> through a blob, so
    connect-src for the playlist and segments and media-src for the video;
  an embed is an <iframe>, so frame-src.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app import CSP

CAMS_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "cams.js"

# Which policy directives each kind of camera needs to be allowed by.
NEEDS = {
    "still": ("img-src",),
    "dated": ("img-src",),
    "hls": ("connect-src", "media-src"),
    "embed": ("frame-src",),
}


def cameras() -> list[tuple[str, str, str]]:
    """Every camera as (id, kind, host), read out of the source.

    Two things the list does that this has to follow. A camera with no `kind`
    is an embed -- that is the fall-through in cams.js -- and a dated camera
    has a `template` where the others have a `src`, because each of its
    frames lives at its own address.

    A camera marked `offsite` needs no directive at all: its host refuses to
    be framed, so the panel offers a link out rather than fetching anything.
    """
    source = CAMS_JS.read_text(encoding="utf-8")
    body = source[source.index("export const CAMS"):]
    out = []
    # Split on the entries rather than matching each one as a balanced block:
    # a comment inside an entry can hold a brace, and a non-greedy match then
    # swallows the entry after it -- which silently drops a camera from a
    # check whose whole job is noticing dropped cameras.
    for chunk in body.split("\n  {")[1:]:
        rest = chunk.split("\n  }")[0]
        found = re.search(r"id: '([^']+)'", rest)
        if not found:
            continue
        cam_id = found.group(1)
        if re.search(r"offsite: true", rest):
            continue
        kind = re.search(r"kind: '([^']+)'", rest)
        address = (re.search(r"src: '(https://[^']+)'", rest)
                   or re.search(r"template: '(https://[^']+)'", rest))
        if not address:
            continue
        host = address.group(1).split("://", 1)[1].split("/", 1)[0]
        out.append((cam_id, kind.group(1) if kind else "embed", host))
    return out


def policy(directive: str) -> str:
    for part in CSP.split(";"):
        if part.strip().startswith(directive):
            return part.strip()
    raise AssertionError(f"no {directive} in the policy at all")


def allows(directive: str, host: str) -> bool:
    for source in policy(directive).split()[1:]:
        if source == f"https://{host}":
            return True
        # A wildcard covers one or more leading labels: *.example.com matches
        # a.example.com but, per the spec, not example.com itself.
        if source.startswith("https://*."):
            suffix = source.removeprefix("https://*.")
            if host.endswith(suffix) and host.count(".") > 1:
                return True
    return False


def test_the_camera_list_was_found_and_read() -> None:
    # Without this, a parsing change would make every test below pass by
    # checking nothing at all.
    found = cameras()
    assert len(found) > 20, f"only {len(found)} cameras parsed — the parsing broke"
    assert all(host and "'" not in host for _, _, host in found), found


def test_no_camera_is_quietly_skipped() -> None:
    """Every entry in the list is either checked or an offsite link.

    The count, because a parsing slip here does not fail -- it drops a camera
    from a check whose whole purpose is to notice a dropped camera. One did:
    a brace inside a comment ended an entry early and took the next one with
    it.
    """
    body = CAMS_JS.read_text(encoding="utf-8")
    body = body[body.index("export const CAMS"):]
    declared = re.findall(r"^    id: '([^']+)'", body, re.M)
    offsite = body.count("offsite: true")
    assert len(cameras()) == len(declared) - offsite, (
        f"{len(declared)} cameras declared, {offsite} offsite, but "
        f"{len(cameras())} parsed: "
        f"{sorted(set(declared) - {c[0] for c in cameras()})}"
    )


def test_every_camera_kind_is_one_this_check_knows_about() -> None:
    """A new kind of camera has to arrive with a decision about which
    directive it needs, rather than silently skipping the check."""
    kinds = {kind for _, kind, _ in cameras()}
    assert kinds <= set(NEEDS), f"no policy rule for {kinds - set(NEEDS)}"


@pytest.mark.parametrize("cam_id,kind,host", cameras(),
                         ids=[c[0] for c in cameras()])
def test_every_camera_host_is_allowed_by_the_policy(
        cam_id: str, kind: str, host: str) -> None:
    for directive in NEEDS[kind]:
        assert allows(directive, host), (
            f"{cam_id} is a {kind} camera on {host}, which {directive} does "
            f"not allow. Add it to the CSP in backend/app.py, or the browser "
            f"will block it and the camera will look like it is off."
        )


# ── The other direction: nothing allowed that nothing uses ─────────────


def test_every_frame_grant_is_used_by_a_camera() -> None:
    """frame-src is there for the cameras and nothing else, so a host in it
    that no camera embeds is a site allowed to load inside this page for no
    reason at all. www.earthcam.com had been left in it that way, though its
    one camera opens on its own site and is never framed."""
    framed = {host for _id, kind, host in cameras() if kind == "embed"}
    granted = {source.removeprefix("https://")
               for source in policy("frame-src").split()[1:]}
    assert granted - framed == set(), f"framed by nothing: {sorted(granted - framed)}"


def test_every_camera_address_is_encrypted() -> None:
    """A camera over plain http could be swapped in transit for anything at
    all, and would be shown in this page as if it were the camera."""
    source = CAMS_JS.read_text(encoding="utf-8")
    body = source[source.index("export const CAMS"):]
    plain = re.findall(r"(?:src|template): '(http://[^']+)'", body)
    assert plain == []


def test_a_camera_shown_off_site_opens_safely() -> None:
    """Off-site cameras are a link, so the link is the whole attack surface:
    a new tab with no handle back to this page and no referrer sent."""
    source = CAMS_JS.read_text(encoding="utf-8")
    at = source.index("if (cam.offsite) {")
    block = source[at:source.index("}", source.index("`Watch on", at))]
    assert "target: '_blank'" in block
    assert "rel: 'noopener noreferrer'" in block


def test_embedded_cameras_are_sandboxed() -> None:
    """A framed camera page runs a player and nothing else: no pop-ups, no
    forms, and no navigating this tab away. Pinned, because loosening the
    sandbox is a one-word change that would not show up anywhere else."""
    source = CAMS_JS.read_text(encoding="utf-8")
    at = source.index("const player = el('iframe', {")
    block = source[at:source.index("});", at)]
    sandbox = re.search(r"sandbox: '([^']+)'", block).group(1).split()
    assert set(sandbox) == {"allow-scripts", "allow-same-origin", "allow-presentation"}
    assert "referrerpolicy: 'no-referrer'" in block
