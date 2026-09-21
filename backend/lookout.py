"""Asking a model the same few questions about every square of an area.

The rest of this app answers "show me this place". This answers a different
shape of question: "somewhere in this area there is a thing that looks like
THIS -- where is it". Nobody can look at eleven thousand square kilometres of
Sentinel-2 one screen at a time, and that is what a sweep is for.

How it works. An area is cut into squares. Each square is rendered from one
satellite pass -- one pass for the whole sweep, so every square is the same
day and the same light, and two squares that look different are different
rather than a week apart. Each rendered square is sent to a model with a
fixed list of questions. A square whose answers match the pattern that was
asked for is a hit, and hits are what the map draws.

The model is OpenJev, served by Codiv at api.codiv.ai, through a Jev-shaped
API: you hand it images, a sentence of context and a schema of typed
questions, and it returns an answer per question with a probability. Nothing
is generated and nothing is parsed out of prose -- a yes/no question comes
back as P(yes) and a choice comes back as one of the names you gave it. That
is the reason this is usable at all: a sweep over a hundred squares that
answered in sentences would need its own parser, and a parser that is wrong
one time in fifty is wrong twice per sweep.

Three things are deliberate.

  AN UNSURE ANSWER IS NOT A MATCH. A yes/no answer is a probability, and
  P(yes) = 0.51 is the model saying it does not know. Counting that as "yes"
  would fill the map with squares nobody would agree with. Every question has
  to land on the right side by a margin, or the square is not a hit and the
  panel says which question was unsure.

  THE PATTERN IS DATA. Which answers make a hit is written next to the
  questions in a file, not spelled out in code, because the questions are the
  thing that gets edited. A rule reading "if 1 and 2 and not 3 and 4" in four
  places is a rule that goes stale the first time a question is added.

  NOTHING IS ASKED WITHOUT A KEY. The key lives in this process's memory for
  as long as it is running and is written nowhere, like the AIS one. It is
  the operator's own key on the operator's own machine and it should not
  outlive the run.
"""

from __future__ import annotations

import base64
import json
import math
import pathlib
import threading
import time
from typing import Any, Callable

import requests

from . import config

MODEL = "openjev-latest"
ENDPOINT = "https://api.codiv.ai/v1/systemone"

# What the model is told it is looking at, before the questions.
#
# Short on purpose. The questions carry what is being asked; this carries only
# what the picture IS, because a model told at length what to look for finds
# it whether it is there or not.
STATE = ("A satellite photograph of about {km:.0f} km of ground, taken from "
         "directly above near {place}. True colour, roughly ten metres per "
         "pixel.")

# The areas a sweep can be run over. Kyiv for now, asked for; the shape is a
# table so the next one is a row rather than a rewrite.
AREAS: dict[str, dict[str, Any]] = {
    "kyiv": {
        "name": "Kyiv",
        # South, north, west, east. The city and the ground round it, out to
        # about thirty kilometres: the reservoir in the north, Boryspil in the
        # east, Vasylkiv in the south.
        "bbox": (50.15, 50.75, 30.10, 30.95),
        "about": "Kyiv and the ground around it, out to about 30 km.",
    },
}

# How wide a square is, in kilometres.
#
# Small enough that a hit is a place rather than a district, large enough that
# a sweep is tens of squares rather than thousands. At ten kilometres the Kyiv
# area is about forty squares, which is forty renders and forty questions --
# minutes, not hours, and it does not ask a free service for a thousand
# answers in one go.
SQUARE_KM = 10.0

# And how many squares one sweep may ever cover, whatever it is asked for.
MOST_SQUARES = 120

# How far from a half a yes/no answer has to be before it counts.
#
# P(yes) = 0.51 is the model saying it does not know. A sweep that treats that
# as a yes marks squares nobody looking at them would agree with, and the
# whole value of a sweep is that its hits are worth walking over to.
SURE_AT = 0.65

# The same idea for a choice answer, which comes back with a probability per
# option rather than one number.
CHOICE_SURE_AT = 0.65

# And the fallback when a choice answer arrives without its probabilities.
#
# `confidence` is 1 - H(p)/ln K, which is not a probability: for two options
# it reaches 0.28 at p = 0.8 and 0.53 at p = 0.9. So it gets its own number,
# and the arithmetic is written down rather than a number being picked that
# looks like the other one.
CONFIDENCE_SURE_AT = 0.28

QUESTIONS_FILE = pathlib.Path(__file__).with_name("data") / "lookout_questions.json"

_questions: list[dict[str, Any]] | None = None


class LookoutError(RuntimeError):
    pass


def questions() -> list[dict[str, Any]]:
    """The questions asked of every square, in order, read once and kept."""
    global _questions
    if _questions is None:
        try:
            with QUESTIONS_FILE.open(encoding="utf-8") as fh:
                got = json.load(fh)
            _questions = [q for q in got if isinstance(q, dict) and q.get("id")]
        except (OSError, ValueError):
            _questions = []
    return _questions


def forget() -> None:
    """Drop the cached questions. For the tests."""
    global _questions
    _questions = None


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

_key = ""
_lock = threading.Lock()


def set_key(key: Any) -> bool:
    """Hold a key for this run, or drop it. Written nowhere."""
    global _key
    with _lock:
        _key = str(key or "").strip()
        return bool(_key)


def have_key() -> bool:
    with _lock:
        return bool(_key)


# ---------------------------------------------------------------------------
# Cutting an area into squares
# ---------------------------------------------------------------------------

EARTH_KM = 6371.0
RAD = math.pi / 180


def squares(area: dict[str, Any], km: float = SQUARE_KM,
            most: int = MOST_SQUARES) -> list[dict[str, Any]]:
    """The area, cut into squares of about `km` across, in reading order.

    About, not exactly: an area is not a whole number of squares across, so
    the last column and row are as wide as what is left. Squares of a fixed
    size with the remainder dropped would leave a strip of the area never
    looked at, and a sweep that quietly skips its own eastern edge is worse
    than one that has a narrow column in it.
    """
    south, north, west, east = area["bbox"]
    if not (north > south and east > west) or km <= 0:
        return []
    tall = (north - south) * RAD * EARTH_KM
    middle = ((north + south) / 2) * RAD
    wide = (east - west) * RAD * EARTH_KM * math.cos(middle)
    rows = max(1, math.ceil(tall / km))
    cols = max(1, math.ceil(wide / km))
    if rows * cols > most:
        return []
    out = []
    for row in range(rows):
        # North to south, so the list reads down the map the way a person does.
        top = north - (north - south) * row / rows
        bottom = north - (north - south) * (row + 1) / rows
        for col in range(cols):
            left = west + (east - west) * col / cols
            right = west + (east - west) * (col + 1) / cols
            out.append({
                "id": f"{row}-{col}",
                "bbox": (bottom, top, left, right),
                "lat": (top + bottom) / 2,
                "lon": (left + right) / 2,
                "km": km,
            })
    return out


def square_polygon(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """A square as the GeoJSON the render pipeline takes."""
    south, north, west, east = bbox
    return {"type": "Polygon", "coordinates": [[
        [west, south], [east, south], [east, north], [west, north],
        [west, south]]]}


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def schema(asked: list[dict[str, Any]]) -> dict[str, Any]:
    """The questions, in the shape the API takes them.

    Only the fields it defines. A yes/no question is `noul` and may carry what
    true and false mean; a choice question carries its options. Anything this
    app keeps for itself -- which answer counts as a hit, what to call the
    question in the panel -- stays out of the request.
    """
    out: dict[str, Any] = {}
    for question in asked:
        kind = question.get("type")
        if kind not in ("noul", "choice"):
            continue
        body: dict[str, Any] = {"type": kind,
                                "instructions": question.get("ask", "")}
        criteria = question.get("criteria")
        if isinstance(criteria, dict) and criteria:
            body["criteria"] = criteria
        out[question["id"]] = body
    return out


def read_answer(question: dict[str, Any], given: Any) -> dict[str, Any]:
    """One answer, as yes, no, or unsure -- with the number it came from.

    The number is kept and shown. "Unsure" with no figure beside it is a
    result nobody can act on or argue with, and the first thing anybody asks
    of a square the sweep skipped is how close it was.
    """
    if not isinstance(given, dict):
        return {"said": "unsure", "why": "no answer", "p": None}
    if question.get("type") == "noul":
        p = given.get("noul")
        if not isinstance(p, (int, float)):
            return {"said": "unsure", "why": "no probability", "p": None}
        p = float(p)
        if p >= SURE_AT:
            return {"said": "yes", "p": p}
        if p <= 1 - SURE_AT:
            return {"said": "no", "p": p}
        return {"said": "unsure", "why": "between", "p": p}

    name = given.get("choice")
    if not isinstance(name, str) or not name:
        return {"said": "unsure", "why": "no choice", "p": None}
    names = list((question.get("criteria") or {}).keys())
    odds = given.get("probabilities")
    if (isinstance(odds, list) and len(odds) == len(names) and name in names
            and all(isinstance(n, (int, float)) for n in odds)):
        p = float(odds[names.index(name)])
        if p >= CHOICE_SURE_AT:
            return {"said": name, "p": p}
        return {"said": "unsure", "why": "between", "p": p}
    # No usable probabilities. `confidence` is an entropy measure rather than
    # a probability, so it is held to its own number -- see the constant.
    sure = given.get("confidence")
    if isinstance(sure, (int, float)) and float(sure) >= CONFIDENCE_SURE_AT:
        return {"said": name, "p": None, "confidence": float(sure)}
    return {"said": "unsure", "why": "not confident", "p": None,
            "confidence": float(sure) if isinstance(sure, (int, float)) else None}


def read_answers(body: Any, asked: list[dict[str, Any]]) -> dict[str, Any]:
    """Every answer in a reply, read against the questions that were asked."""
    given = body.get("answers") if isinstance(body, dict) else None
    given = given if isinstance(given, dict) else {}
    return {q["id"]: read_answer(q, given.get(q["id"])) for q in asked}


def verdict(answers: dict[str, Any],
            asked: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether a square is a hit, and if not, which question stopped it.

    Every question has to land on the answer it says makes a hit. A question
    with nothing to say about it -- no `hit` in the file -- is asked and shown
    and does not decide anything, so a fifth question can be added for
    interest without silently changing what the sweep marks.
    """
    misses = []
    unsure = []
    for question in asked:
        wanted = question.get("hit")
        if wanted is None:
            continue
        said = (answers.get(question["id"]) or {}).get("said")
        if said == "unsure":
            unsure.append(question["id"])
        elif said != wanted:
            misses.append(question["id"])
    return {"hit": not misses and not unsure,
            "missed": misses, "unsure": unsure}


def ask(images: list[str], place: str, km: float,
        asked: list[dict[str, Any]] | None = None,
        post: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Put the questions to the model about one square. Returns its reply.

    Raises LookoutError with what happened, rather than returning something
    that looks like an answer -- a sweep that silently turns a refusal into
    "no" marks nothing and says nothing about why.
    """
    asked = questions() if asked is None else asked
    if not asked:
        raise LookoutError("there are no questions to ask")
    with _lock:
        key = _key
    if not key:
        raise LookoutError("no Codiv API key has been given to this app")
    body = {
        "model": MODEL,
        "state": STATE.format(km=km, place=place),
        "questions": schema(asked),
        "images": images,
    }
    send = post or requests.post
    try:
        resp = send(ENDPOINT, json=body, timeout=60, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": config.USER_AGENT,
        })
    except requests.RequestException as exc:
        raise LookoutError(f"Codiv could not be reached: {exc}") from exc
    if getattr(resp, "status_code", 0) == 401:
        raise LookoutError("Codiv refused the key")
    if getattr(resp, "status_code", 0) == 429:
        raise LookoutError("Codiv is rate limiting; slow down or try later")
    if not getattr(resp, "ok", False):
        raise LookoutError(f"Codiv answered {getattr(resp, 'status_code', '?')}")
    try:
        return resp.json()
    except ValueError as exc:
        raise LookoutError(f"Codiv sent something that is not JSON: {exc}") from exc


def as_data_url(body: bytes, kind: str = "image/png") -> str:
    """One rendered square, in the form the API takes an image in."""
    return f"data:{kind};base64," + base64.b64encode(body).decode()


# ---------------------------------------------------------------------------
# A sweep
# ---------------------------------------------------------------------------
#
# Run on a thread, because a sweep is tens of renders and tens of requests and
# nothing about that belongs in a web request. The panel polls what is below.

_state: dict[str, Any] = {
    "running": False, "area": None, "of": 0, "done": 0,
    "hits": [], "looked": [], "trouble": "", "scene": None, "at": 0.0,
}
_stop = False


def state() -> dict[str, Any]:
    """What the sweep is doing. Copied, so a poll cannot see it half-written."""
    with _lock:
        return {
            **{k: v for k, v in _state.items() if k not in ("hits", "looked")},
            "hits": list(_state["hits"]),
            "looked": list(_state["looked"]),
            "areas": [{"key": k, "name": v["name"], "bbox": list(v["bbox"]),
                       "about": v["about"]} for k, v in AREAS.items()],
            "questions": [{"id": q["id"], "ask": q.get("ask", ""),
                           "type": q.get("type"), "hit": q.get("hit")}
                          for q in questions()],
            "have_key": bool(_key),
            "square_km": SQUARE_KM,
        }


def note_trouble(said: str) -> None:
    """Record why a sweep could not run, for the panel to show.

    A sweep that fails before its first square has nothing in `looked` to
    explain itself, and a panel reading "0 of 0" with no reason is the same
    screen as one that is simply slow.
    """
    with _lock:
        _state["trouble"] = said
        _state["running"] = False
        _state["at"] = time.time()


def stop() -> None:
    """Ask a running sweep to give up at the next square."""
    global _stop
    with _lock:
        _stop = True


def reset() -> None:
    """Back to nothing. For the tests, and for starting a fresh sweep."""
    global _stop
    with _lock:
        _stop = False
        _state.update({"running": False, "area": None, "of": 0, "done": 0,
                       "hits": [], "looked": [], "trouble": "", "scene": None,
                       "at": 0.0})


def sweep(area_key: str, picture: Callable[[dict[str, Any]], str],
          post: Callable[..., Any] | None = None,
          km: float = SQUARE_KM) -> dict[str, Any]:
    """Look at every square of an area and record which ones are hits.

    `picture` is handed a square and returns its rendered image as a data
    URL, or raises. Passed in rather than reached for, because what a square
    looks like and what the model says about it are two separable problems --
    and because a sweep has to be testable without a satellite.
    """
    global _stop
    area = AREAS.get(area_key)
    if area is None:
        raise LookoutError(f"{area_key} is not an area this app knows")
    asked = questions()
    if not asked:
        raise LookoutError("there are no questions to ask")
    grid = squares(area, km)
    if not grid:
        raise LookoutError(
            f"that area cuts into more than {MOST_SQUARES} squares at "
            f"{km:.0f} km; ask for fewer")

    with _lock:
        _stop = False
        _state.update({"running": True, "area": area_key, "of": len(grid),
                       "done": 0, "hits": [], "looked": [], "trouble": "",
                       "at": time.time()})
    try:
        for square in grid:
            with _lock:
                if _stop:
                    _state["trouble"] = "stopped"
                    break
            _one(square, area, asked, picture, post)
            with _lock:
                _state["done"] += 1
    finally:
        with _lock:
            _state["running"] = False
            _state["at"] = time.time()
    return state()


def _one(square: dict[str, Any], area: dict[str, Any],
         asked: list[dict[str, Any]], picture: Callable[..., str],
         post: Callable[..., Any] | None) -> None:
    """One square: render it, ask about it, record what came back.

    One square that fails does not end the sweep. A cloud over a square, a
    scene with a hole in it, one refused request -- each is a square this
    sweep cannot answer for, and forty-one squares with one gap is a result
    where nothing at all is not.
    """
    row: dict[str, Any] = {"id": square["id"], "lat": square["lat"],
                           "lon": square["lon"],
                           "bbox": list(square["bbox"])}
    try:
        image = picture(square)
        reply = ask([image], area["name"], square["km"], asked, post)
        answers = read_answers(reply, asked)
        said = verdict(answers, asked)
        row.update({"answers": answers, **said})
    except (LookoutError, ValueError, OSError, RuntimeError) as exc:
        row.update({"hit": False, "trouble": str(exc), "answers": {},
                    "missed": [], "unsure": []})
    with _lock:
        _state["looked"].append(row)
        if row.get("hit"):
            _state["hits"].append(row)
