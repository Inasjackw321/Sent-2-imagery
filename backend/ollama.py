"""A local Ollama model, for reading reports written in prose.

This replaces a hosted service, and the reason is worth stating because it
changes what the code around it has to worry about.

The previous version called OpenRouter's free tier. Free models there share a
daily ceiling per account, the popular ones reach it first, and the layer spent
more code on that than on the actual work: a list of models to fall through,
`Retry-After` and `X-RateLimit-Reset` parsing, exponential backoff, a panel
that had to explain "OpenRouter is rate limiting" to somebody who just wanted
to see a map. None of that is reading Ukrainian.

Ollama runs on the machine. There is no key, no quota, no per-request cost and
no reason to back off -- so all of that is gone. What replaces it is a
different and smaller set of problems, and they are the ones this module is
about:

  Is it running?      Nothing is installed by this app. If the daemon is not
                      up, that has to be said plainly and once, with the
                      command to fix it, rather than surfacing as a network
                      error every sixty seconds.

  Is the model there? `ollama run` pulls on demand; the API does not. Asking
                      for a model that was never pulled returns a 404 with a
                      message about it, which is a fixable situation and should
                      read like one.

  Which model?        This is the interesting one. The request was for "gemma
                      4". There is no Gemma 4 -- the current family is Gemma 3,
                      whose tags are gemma3:1b, :4b, :12b, :27b, and ":4b" is
                      four billion parameters rather than a version. Hard-coding
                      any single name would be a guess that fails on a machine
                      that pulled a different size.

                      So nothing is hard-coded. The installed models are read
                      from the daemon and the best available match is chosen, by
                      a stated order of preference. Whatever is actually there
                      gets used, and the panel says which.

The model is asked for JSON and nothing else, and it is never asked where
anything is. Coordinates come from the gazetteer; see backend/gazetteer.py for
why that separation is not negotiable.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

# Where Ollama listens by default. Loopback, deliberately not configurable
# from the browser: this is a local daemon and the app should not be able to be
# pointed at an arbitrary host by anything a page can reach.
HOST = "http://127.0.0.1:11434"

TAGS = f"{HOST}/api/tags"
CHAT = f"{HOST}/api/chat"

# What to use, best first, matched against what is actually installed.
#
# Gemma is first because it was asked for. The sizes are in descending order of
# capability within each family, because this is a reading-comprehension job in
# several languages and the larger models are meaningfully better at it -- but
# a 4b model on a laptop is the realistic case and it is genuinely adequate,
# which is why it is only two places down rather than last.
#
# The fallbacks exist so that a machine with any reasonable instruct model
# works without being told to pull another one. Qwen is here because it handles
# Cyrillic well; llama and mistral because they are the most commonly present.
PREFERRED = (
    "gemma3:27b", "gemma3:12b", "gemma3:4b", "gemma3:1b", "gemma3",
    "gemma2:27b", "gemma2:9b", "gemma2", "gemma",
    "qwen2.5:32b", "qwen2.5:14b", "qwen2.5:7b", "qwen2.5", "qwen3", "qwen",
    "llama3.3", "llama3.2", "llama3.1", "llama3", "llama",
    "mistral-nemo", "mistral", "phi4", "phi3",
)

# How long to wait for an answer. A local model on a modest machine takes a few
# seconds per batch and there is no cost to waiting, but a request that never
# returns would wedge the poll loop, so there is still a ceiling.
TIMEOUT = 180

# How long the list of installed models is trusted for. Pulling a model is a
# deliberate act and not a frequent one, so this only exists so that pulling
# one does not require restarting the app.
TAGS_FOR = 120.0

_tags: list[str] = []
_tags_at = 0.0
_chosen: str | None = None
# Set when the user names a model explicitly. That overrides the preference
# order entirely -- if somebody has pulled a model for this and says to use it,
# second-guessing them would be wrong.
_asked_for: str | None = None


class OllamaError(RuntimeError):
    """Ollama could not answer. Carries something a person can act on."""


class NotRunning(OllamaError):
    """The daemon is not listening.

    Its own class because the caller treats it differently: it is not a failure
    of the model or of the reading, it is the one situation with a single
    obvious remedy, and the panel says that remedy rather than a stack of
    network language.
    """


class ModelMissing(OllamaError):
    """The daemon is up but has not been given the model.

    Also its own class, and for the same reason: `ollama pull <name>` fixes it,
    and that is worth saying instead of "404".
    """


def prefer(name: str | None) -> str | None:
    """Name a model to use, or pass None to go back to choosing one.

    Held in memory only. Nothing about the model choice is written to disk.
    """
    global _asked_for, _chosen
    _asked_for = (name or "").strip() or None
    _chosen = None
    return _asked_for


def installed(refresh: bool = False) -> list[str]:
    """The models this daemon has, newest listing cached briefly."""
    global _tags, _tags_at
    if _tags and not refresh and time.time() - _tags_at < TAGS_FOR:
        return _tags
    try:
        resp = requests.get(TAGS, timeout=5)
    except requests.RequestException as exc:
        raise NotRunning(
            "Ollama is not answering on 127.0.0.1:11434. Start it with "
            "`ollama serve`, or install it from ollama.com."
        ) from exc
    if not resp.ok:
        raise OllamaError(f"Ollama answered {resp.status_code} when asked "
                          f"what models it has.")
    try:
        found = [m["name"] for m in resp.json().get("models", []) if m.get("name")]
    except (ValueError, TypeError, AttributeError) as exc:
        raise OllamaError(f"Ollama sent a model list that would not read: "
                          f"{exc}") from exc
    _tags, _tags_at = found, time.time()
    return found


def matches(wanted: str, have: str) -> bool:
    """Whether an installed tag satisfies a wanted name.

    Ollama tags are `family:size`, and an unqualified request means "any size
    of this family". `gemma3` matches `gemma3:4b`; `gemma3:4b` matches only
    itself, and also `gemma3:4b-instruct-q4_K_M`, because quantisation suffixes
    are the same model.
    """
    have = have.lower()
    wanted = wanted.lower()
    if have == wanted:
        return True
    if ":" not in wanted:
        return have.split(":")[0] == wanted
    return have.startswith(f"{wanted}-") or have == wanted


def choose(refresh: bool = False) -> str:
    """Which model to use, from what is actually installed.

    A named preference wins outright. Otherwise the first entry of PREFERRED
    that is present, and if none of them is, the first model the daemon has --
    because a machine with one unusual instruct model should work rather than
    be told its model is not on a list.
    """
    global _chosen
    if _chosen and not refresh:
        return _chosen
    have = installed(refresh=refresh)
    if not have:
        raise ModelMissing(
            "Ollama is running but has no models. Pull one with "
            "`ollama pull gemma3:4b`.")

    if _asked_for:
        for name in have:
            if matches(_asked_for, name):
                _chosen = name
                return _chosen
        raise ModelMissing(
            f"Ollama does not have {_asked_for}. Pull it with "
            f"`ollama pull {_asked_for}`, or pick one of: "
            f"{', '.join(have[:6])}.")

    for wanted in PREFERRED:
        for name in have:
            if matches(wanted, name):
                _chosen = name
                return _chosen
    _chosen = have[0]
    return _chosen


def status() -> dict[str, Any]:
    """What the panel says about the model, including when it is not there."""
    try:
        have = installed()
        return {
            "ready": True, "host": HOST, "model": choose(),
            "installed": have[:12], "asked_for": _asked_for,
        }
    except OllamaError as exc:
        return {
            "ready": False, "host": HOST, "model": None,
            "installed": [], "asked_for": _asked_for,
            "problem": str(exc),
            "kind": type(exc).__name__,
        }


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def read_any(content: str) -> Any:
    """Whatever JSON is in a model's answer, however it chose to wrap it.

    `format: json` makes this almost always unnecessary, and almost is the
    problem: a smaller model occasionally puts a fence or a sentence around it
    anyway. Repairing that here is cheaper than losing a whole batch of reports
    to a stray backtick.

    Returns an object or a list, because a model asked for {"events": [...]}
    will sometimes answer with the bare list. Callers that need one or the
    other say so; read_json() is this plus the object check.
    """
    text = _FENCE.sub("", content.strip())
    try:
        got = json.loads(text)
    except ValueError:
        # The outermost braces, which is what is left when a model has said
        # "Here is the JSON:" first.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise OllamaError("the model's answer was not JSON") from None
        try:
            got = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise OllamaError(f"the model's answer was not JSON: {exc}") from exc
    return got


def read_json(content: str) -> dict[str, Any]:
    """read_any(), for a caller that needs an object."""
    got = read_any(content)
    if not isinstance(got, dict):
        raise OllamaError("the model answered with something other than an object")
    return got


def ask(prompt: str, payload: str, *, model: str | None = None) -> dict[str, Any]:
    """Put one batch to the model and return the object it answered with.

    Temperature zero, JSON format, one system message and one user message.
    Nothing here retries: a local daemon that fails twice will fail a third
    time, and the caller has a perfectly good answer to a failed model step --
    read the reports without one.
    """
    name = model or choose()
    body = {
        "model": name,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": payload},
        ],
        "stream": False,
        # Ollama's own structured-output switch. Asking for machine-readable
        # output is cheaper than repairing prose afterwards.
        "format": "json",
        "options": {
            "temperature": 0,
            # Long enough for a batch of reports, capped so a model that starts
            # repeating itself cannot run for minutes.
            "num_predict": 2048,
        },
    }
    try:
        resp = requests.post(CHAT, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise NotRunning(
            f"Ollama stopped answering on {HOST}: {exc}. Start it with "
            f"`ollama serve`.") from exc

    if resp.status_code == 404:
        raise ModelMissing(f"Ollama does not have {name}. Pull it with "
                           f"`ollama pull {name}`.")
    if not resp.ok:
        raise OllamaError(f"Ollama answered {resp.status_code} for {name}: "
                          f"{resp.text[:160]}")

    try:
        content = resp.json()["message"]["content"]
    except (ValueError, KeyError, TypeError) as exc:
        raise OllamaError(f"Ollama sent a reply with no message: {exc}") from exc
    # A 200 whose content is null or empty. The hosted version did this on a
    # refusal and it reached the reader and raised AttributeError, which was
    # not an error class anything caught, so it 500ed the endpoint instead of
    # falling back to reading the reports plainly. Checked here for the same
    # reason even though a local model has no reason to refuse.
    if not isinstance(content, str) or not content.strip():
        raise OllamaError(f"{name} answered with nothing")
    return read_json(content)
