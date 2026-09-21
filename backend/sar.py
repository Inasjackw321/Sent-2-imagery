"""What a Sentinel-1 pass actually is, beyond a date.

An optical scene is nearly self-describing: a date, a cloud figure, and the
picture looks like the ground. A radar scene is not. Two Sentinel-1 passes
over the same field a day apart can be unrecognisable as the same place, and
nothing in the picture says why -- so the acquisition parameters are not
trivia here, they are most of what makes the image readable.

Four of them, and each answers a question a reader actually has.

  WHICH WAY IT WAS LOOKING. Sentinel-1 is right-looking, so an ascending pass
  lights the ground from the east and a descending one from the west. Hills
  lay over towards the sensor and cast radar shadow away from it, and both
  flip between the two. A valley bright in one is black in the other. Two
  dates are comparable when the orbit direction matches and are not when it
  does not, and this app was fetching that field and showing it nowhere.

  WHICH REPEAT TRACK. The relative orbit number. Same track means the same
  incidence angle and very nearly the same geometry, which is the condition
  under which two dates can be subtracted from one another rather than merely
  looked at side by side.

  WHICH PAIR OF POLARISATIONS. Over land the instrument transmits VV and
  receives VV and VH. Over sea ice and much open ocean it transmits HH
  instead. A scene in the other pair has no vv asset at all, and asking it for
  a picture that needs one used to fail with "Scene ... has no vv asset",
  which reads as this app being broken rather than as the satellite having
  been in a different mode.

  WHICH MODE. IW over land at 10 m, EW over ice at 40 m, and the swath and
  detail follow from it. A picture that looks soft is not a bad render.

None of this is derived or guessed. Every field here is published in the STAC
item under the SAR and satellite extensions; what this module does is read
them, say what they mean, and refuse to offer a picture the scene cannot make.
"""

from __future__ import annotations

from typing import Any

from . import config

# What each instrument mode is, in the terms that matter when looking at one.
MODES: dict[str, dict[str, str]] = {
    "IW": {"name": "Interferometric Wide",
           "about": "The land mode: a 250 km swath at about 20 m detail. "
                    "Almost everything over Europe is this."},
    "EW": {"name": "Extra Wide",
           "about": "The ice and ocean mode: 400 km across at about 40 m, so "
                    "it looks softer than a land pass and is not a bad render."},
    "SM": {"name": "Stripmap",
           "about": "A narrow 80 km strip at about 5 m, tasked rather than "
                    "routine. The sharpest Sentinel-1 gets."},
    "WV": {"name": "Wave",
           "about": "Small vignettes over open ocean for wave spectra, not a "
                    "continuous image of anywhere."},
}

# What the two directions mean on the ground.
PASSES: dict[str, dict[str, str]] = {
    "ascending": {"name": "Ascending",
                  "about": "Flying north, looking east. Evening pass. Slopes "
                           "facing east are bright; those facing west are in "
                           "radar shadow."},
    "descending": {"name": "Descending",
                   "about": "Flying south, looking west. Morning pass. The "
                            "lighting is the mirror of an ascending one, so "
                            "the two are not directly comparable."},
}

# The pairs the instrument actually transmits and receives in.
#
# It transmits one polarisation and listens on two, so these come in twos and
# never mixed: a scene is never VV and HH.
PAIRS = (("VV", "VH"), ("HH", "HV"))


def polarisations(scene: dict[str, Any]) -> list[str]:
    """Which polarisations a scene carries, upper-cased.

    From the catalogue where it says, and from the assets it actually offers
    where it does not -- a scene whose properties are thin but whose assets
    are there is perfectly usable, and refusing it on a missing property
    would be refusing a picture over a piece of paperwork.
    """
    said = scene.get("polarisations") or scene.get("sar:polarizations")
    found = []
    if isinstance(said, (list, tuple)):
        found = [str(p).upper() for p in said if str(p).strip()]
    if not found:
        assets = scene.get("assets") or {}
        found = [name.upper() for name in assets
                 if name.upper() in {"VV", "VH", "HH", "HV"}]
    # Ordered as the instrument sends them, so the label reads "VV+VH" rather
    # than whichever order the catalogue happened to list.
    order = {p: n for n, p in enumerate(["VV", "VH", "HH", "HV"])}
    return sorted(dict.fromkeys(found), key=lambda p: order.get(p, 9))


def pair_of(said: list[str]) -> str:
    """"VV+VH", "HH", or "" when there is nothing to say."""
    return "+".join(said)


def can_make(scene: dict[str, Any], composite: str) -> bool:
    """Whether a scene carries the polarisations a composite needs.

    Asked before a composite is offered rather than after it is chosen. A
    picker that lists four pictures of which two cannot be drawn is a picker
    that teaches people the app is unreliable.
    """
    spec = config.COMPOSITES.get(composite)
    if not spec:
        return False
    return not missing_for(scene, spec.get("bands") or [])


def missing_for(scene: dict[str, Any], bands: list[str]) -> list[str]:
    """Which polarisations these bands need that the scene has not got."""
    have = set(polarisations(scene))
    if not have:
        # Nothing known either way. Not the same as "it has none": a scene
        # whose properties and assets are both absent has not been fetched
        # yet, and refusing it here would refuse every scene in a list.
        return []
    wanted = []
    for band in bands:
        spec = config.BANDS.get(band) or {}
        for part in spec.get("derive") or (band,):
            need = config.BAND_POLARISATION.get(part)
            if need and need not in have and need not in wanted:
                wanted.append(need)
    return wanted


def composites_for(scene: dict[str, Any]) -> list[str]:
    """Which radar composites this particular pass can make, in order."""
    return [name for name, spec in config.COMPOSITES.items()
            if "sentinel-1" in _sats(spec) and can_make(scene, name)]


def _sats(spec: dict[str, Any]) -> tuple[str, ...]:
    said = spec.get("sat")
    if isinstance(said, str):
        return (said,)
    return tuple(said or ())


def default_composite(scene: dict[str, Any]) -> str:
    """The composite to open a radar scene with.

    The satellite's own default where the scene can make it, and otherwise
    the first one it can -- which is how an HH/HV pass over the ice opens on
    a picture rather than on an error.
    """
    usual = config.satellite("sentinel-1")["default_composite"]
    if can_make(scene, usual):
        return usual
    got = composites_for(scene)
    return got[0] if got else usual


def comparable(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two passes may honestly be averaged or subtracted.

    Same direction and same repeat track. Merging dates over radar
    speckle-averages several passes, and averaging an ascending pass with a
    descending one averages two different lightings of the same hill: the
    result is not a cleaner picture of the ground, it is a blur of two.

    Unknown is not the same as different. A scene whose track this app was not
    told is not evidence of a mismatch, and refusing on it would refuse every
    merge against a catalogue with thin properties.
    """
    for field in ("orbit_state", "orbit"):
        one, two = a.get(field), b.get(field)
        if one is not None and two is not None and one != two:
            return False
    return True


def merge_trouble(scenes: list[dict[str, Any]]) -> str:
    """What is wrong with averaging these passes together, or "".

    Said rather than refused. Somebody may well want the average anyway --
    and being told what they are getting is the difference between a choice
    and a surprise.
    """
    radar = [s for s in scenes if s.get("satellite") == "sentinel-1"]
    if len(radar) < 2:
        return ""
    first = radar[0]
    ways = {s.get("orbit_state") for s in radar if s.get("orbit_state")}
    tracks = {s.get("orbit") for s in radar if s.get("orbit") is not None}
    pairs = {pair_of(polarisations(s)) for s in radar if polarisations(s)}
    said = []
    if len(ways) > 1:
        said.append("ascending and descending passes, which light the ground "
                    "from opposite sides")
    elif len(tracks) > 1:
        said.append(f"{len(tracks)} different repeat tracks, so the incidence "
                    "angle differs between them")
    if len(pairs) > 1:
        said.append(f"two polarisation pairs ({', '.join(sorted(pairs))})")
    if not said:
        return ""
    _ = first
    return "Averaging " + " and ".join(said) + "."


def describe(scene: dict[str, Any]) -> dict[str, Any]:
    """Everything worth saying about one radar pass, for the panel.

    Empty for anything that is not radar, so a caller can hand it any scene.
    """
    if scene.get("satellite") != "sentinel-1":
        return {}
    said = polarisations(scene)
    mode = str(scene.get("mode") or "").upper()
    way = str(scene.get("orbit_state") or "").lower()
    return {
        "mode": mode,
        "mode_name": MODES.get(mode, {}).get("name", ""),
        "mode_about": MODES.get(mode, {}).get("about", ""),
        "pass": way,
        "pass_name": PASSES.get(way, {}).get("name", ""),
        "pass_about": PASSES.get(way, {}).get("about", ""),
        "track": scene.get("orbit"),
        "polarisations": said,
        "pair": pair_of(said),
        "product": scene.get("product") or "",
        "composites": composites_for(scene),
        "default_composite": default_composite(scene),
    }


def label(scene: dict[str, Any]) -> str:
    """The one line that goes under a radar scene in a list.

    "Descending · track 36 · IW · VV+VH". Four facts, in the order somebody
    reading a list of passes wants them: which way it looked, whether it is
    the same track as the one above, and what it can be asked for.
    """
    said = describe(scene)
    if not said:
        return ""
    bits = [
        said["pass_name"],
        f"track {said['track']}" if said["track"] is not None else "",
        said["mode"],
        said["pair"],
    ]
    return " · ".join(bit for bit in bits if bit)
