"""A small local model, used to read alert messages.

The job is narrow: take a line of Telegram, in Russian or Ukrainian or
English, and say what kind of event it reports and where. That is extraction,
not reasoning, and extraction is what small models are good at -- so this asks
for the smallest one that can do it rather than the best one available.

The default is qwen2.5:1.5b-instruct. It is about a gigabyte, answers in well
under a second on a laptop CPU, and unlike the 1B Llama models it was trained
on enough Russian and Ukrainian to read the messages these channels are
actually written in, which is the whole requirement. Anything larger is paying
for reasoning this task does not need; anything smaller starts inventing place
names, which is worse than useless when the output is a pin on a map.

Nothing here is required. If Ollama is not running, the caller falls back to
matching words, and says which of the two produced each answer.
"""

from __future__ import annotations

import json
import os
import threading
import time

import requests

# Where Ollama listens by default. Overridable for a model running elsewhere.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b-instruct")

# Alternatives worth knowing about, offered in the interface so the choice is
# visible rather than buried in an environment variable.
SUGGESTED = [
    ("qwen2.5:1.5b-instruct", "1 GB · reads Russian and Ukrainian · the default"),
    ("qwen2.5:3b-instruct", "2 GB · slower, better on awkward phrasing"),
    ("llama3.2:1b", "1.3 GB · fastest, weakest outside English"),
    ("gemma2:2b", "1.6 GB · a middle option"),
]

# A model that has to be pulled into memory first can take a while on the very
# first call; after that it is fast. Short enough that a stalled model does not
# hold up a minute's worth of messages.
WARMUP_TIMEOUT = 120
READ_TIMEOUT = 20

# Availability is re-checked at most this often. Ollama being absent is the
# common case and should not cost a connection attempt per message.
PROBE_SECONDS = 60

_session = requests.Session()
_lock = threading.Lock()
_probe: dict = {"at": 0.0, "ok": False, "detail": "not checked yet", "models": []}

# The shape every answer has to take. Ollama enforces this itself, which is
# the difference between parsing a model's prose and reading a field.
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "explosion", "missile", "drone", "aircraft", "artillery",
                "air_defence", "alert", "other",
            ],
        },
        "place": {"type": "string"},
        "confident": {"type": "boolean"},
    },
    "required": ["kind", "place", "confident"],
}

PROMPT = (
    "You read short military and emergency alerts from Telegram channels. "
    "They are usually in Russian or Ukrainian.\n\n"
    "Reply with the single event the message reports and the single most "
    "specific place name in it.\n\n"
    "kind must be one of: explosion, missile, drone, aircraft, artillery, "
    "air_defence, alert, other.\n"
    "place must be a bare place name as written -- a city, town, district or "
    "oblast. No prepositions, no directions, no distances. Use an empty "
    "string if the message names nowhere.\n"
    "confident must be false if you are guessing at either field.\n\n"
    "Invent nothing. A place that is not in the message must not appear in "
    "your answer.\n\n"
    "Message:\n"
)


def available(force: bool = False) -> dict:
    """Whether a model is reachable, and which ones are installed."""
    now = time.time()
    with _lock:
        fresh = now - _probe["at"] < PROBE_SECONDS
        if fresh and not force:
            return dict(_probe)

    out = {"at": now, "ok": False, "detail": "", "models": []}
    try:
        resp = _session.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        resp.raise_for_status()
        out["models"] = sorted(m.get("name", "") for m in resp.json().get("models", []))
        out["ok"] = True
        if MODEL not in out["models"] and MODEL.split(":")[0] not in {
            m.split(":")[0] for m in out["models"]
        }:
            out["ok"] = False
            out["detail"] = (
                f"Ollama is running but {MODEL} is not installed. "
                f"Run: ollama pull {MODEL}"
            )
        else:
            out["detail"] = f"{MODEL} ready"
    except requests.RequestException as exc:
        out["detail"] = (
            f"No model at {OLLAMA_URL} ({exc.__class__.__name__}). "
            "Alerts fall back to matching words, which is cruder."
        )

    with _lock:
        _probe.update(out)
    return dict(out)


def last_known() -> dict:
    """What the last probe found, without making another.

    Serving the alert list must not depend on a network call. A refused
    connection is instant, but a host that silently drops packets is not, and
    a four-second stall on every poll for a model that is not there would be
    the model making the app slower by being absent.
    """
    with _lock:
        return dict(_probe)


def read(message: str, timeout: int = READ_TIMEOUT) -> dict | None:
    """Ask the model what one message reports. None if it cannot answer."""
    text = (message or "").strip()
    if not text:
        return None
    try:
        resp = _session.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL,
                "prompt": PROMPT + text[:1200],
                # Constrained decoding: the reply cannot be anything but this
                # shape, so there is no prose to parse and no retry loop.
                "format": SCHEMA,
                "stream": False,
                "options": {
                    # Nothing here benefits from invention.
                    "temperature": 0.0,
                    # The answer is three short fields. Anything longer is the
                    # model having gone wrong, and cutting it off is cheaper
                    # than reading it.
                    "num_predict": 120,
                    "num_ctx": 1024,
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        answer = json.loads(resp.json().get("response", "{}"))
    except (requests.RequestException, ValueError, KeyError):
        return None

    kind = str(answer.get("kind") or "other").strip().lower()
    if kind not in set(SCHEMA["properties"]["kind"]["enum"]):
        kind = "other"
    return {
        "kind": kind,
        "place": str(answer.get("place") or "").strip(),
        "confident": bool(answer.get("confident")),
        "by": "model",
    }


def set_model(name: str) -> dict:
    """Point at a different local model."""
    global MODEL
    MODEL = (name or "").strip() or MODEL
    return available(force=True)
