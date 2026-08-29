"""Reading chosen Telegram channels, once a minute.

This needs a real account rather than a bot. A bot can only read a channel it
has been made an administrator of, which is not something you can do to
somebody else's channel -- so reading the public OSINT channels people
actually follow means signing in as yourself, over Telegram's own MTProto
protocol.

That has consequences worth being plain about:

  * **The session is held in memory and written nowhere.** Restarting the app
    means signing in again. A Telegram session file is equivalent to a logged-in
    copy of your account; leaving one on disk beside a hobby project is not a
    trade this makes on your behalf.
  * **Your credentials go to Telegram and nowhere else.** There is no server in
    the middle here; this is your machine talking to Telegram directly.
  * **Reading is all it does.** Nothing here can send, join, leave or post.

The api_id and api_hash come from my.telegram.org. They identify the
application, not you.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time

# Telethon is optional. The rest of the app must import cleanly without it, so
# the failure is reported when the feature is used rather than at start-up.
try:
    from telethon import TelegramClient
    from telethon.errors import (
        PhoneCodeInvalidError, SessionPasswordNeededError,
    )
    from telethon.sessions import StringSession
    HAVE_TELETHON = True
except ImportError:  # pragma: no cover - depends on the install
    TelegramClient = None
    StringSession = None
    SessionPasswordNeededError = PhoneCodeInvalidError = Exception
    HAVE_TELETHON = False


class TelegramError(RuntimeError):
    pass


# The floor the user asked for, and a sensible one: these channels post in
# bursts and Telegram does not thank you for polling harder than this.
POLL_SECONDS = 60

# How far back the first poll of a channel looks. Enough to put something on
# the map immediately without replaying the whole day.
BACKFILL = 12

# Per poll, per channel. A channel in the middle of a mass attack can post
# faster than this; the rest are dropped rather than queued, because the map is
# a live picture and the newest messages are the ones that matter.
PER_CHANNEL = 25

_lock = threading.Lock()
_state: dict = {
    "signed_in": False,
    "phone": None,
    "channels": [],       # the ones being watched
    "polling": False,
    "last_poll": None,
    "last_error": None,
    "counted": 0,
}

# The asyncio loop Telethon lives on. Telethon is async and the rest of this
# app is not, so it gets one background thread of its own and everything
# crosses over through run_coroutine_threadsafe.
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_client = None
_pending = {}     # a sign-in half-finished: the sent code, waiting for a reply
_poller: threading.Thread | None = None
_stop = threading.Event()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(target=_loop.run_forever, name="telegram", daemon=True)
        _thread.start()
    # Give the thread a moment to actually start running the loop, or the
    # first call is scheduled onto a loop that is not turning yet.
    for _ in range(50):
        if _loop.is_running():
            break
        time.sleep(0.01)
    return _loop


def _run(coro, timeout: float = 60):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError as exc:
        future.cancel()
        raise TelegramError("Telegram did not answer in time") from exc


def _need_telethon() -> None:
    if not HAVE_TELETHON:
        raise TelegramError(
            "Telegram support needs the telethon package. "
            "Install it with: pip install telethon"
        )


# ── Signing in ─────────────────────────────────────────────────


def start_login(api_id: int, api_hash: str, phone: str) -> dict:
    """Ask Telegram to send a login code to this account."""
    _need_telethon()
    global _client

    async def go():
        global _client
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        return client, sent.phone_code_hash

    try:
        client, code_hash = _run(go())
    except TelegramError:
        raise
    except Exception as exc:
        raise TelegramError(f"Telegram refused the sign-in: {exc}") from exc

    _client = client
    _pending.update(phone=phone, hash=code_hash)
    with _lock:
        _state.update(phone=phone, last_error=None)
    return {"sent": True, "phone": phone, "needs": "code"}


def finish_login(code: str, password: str | None = None) -> dict:
    """Hand back the code Telegram sent, and a 2FA password if there is one."""
    _need_telethon()
    if _client is None or "hash" not in _pending:
        raise TelegramError("No sign-in is in progress. Start again.")

    async def go():
        try:
            await _client.sign_in(
                phone=_pending["phone"], code=code.strip(),
                phone_code_hash=_pending["hash"],
            )
        except SessionPasswordNeededError:
            if not password:
                return "password"
            await _client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise TelegramError("That code was not accepted.") from exc
        return "in"

    try:
        outcome = _run(go())
    except TelegramError:
        raise
    except Exception as exc:
        raise TelegramError(f"Sign-in failed: {exc}") from exc

    if outcome == "password":
        return {"signed_in": False, "needs": "password"}

    with _lock:
        _state.update(signed_in=True, last_error=None)
    return {"signed_in": True, "needs": None}


def sign_out() -> dict:
    """Drop the session. Nothing was written, so nothing is left behind."""
    global _client
    stop_polling()
    client = _client
    _client = None
    _pending.clear()
    if client is not None:
        try:
            _run(client.disconnect(), timeout=10)
        except Exception:  # pragma: no cover - disconnect is best effort
            pass
    with _lock:
        _state.update(signed_in=False, phone=None, channels=[], counted=0)
    return status()


# ── Channels ───────────────────────────────────────────────────


def channels() -> list[dict]:
    """Every channel this account can read."""
    _need_telethon()
    if not _state["signed_in"]:
        raise TelegramError("Not signed in.")

    async def go():
        out = []
        async for dialog in _client.iter_dialogs():
            if not (dialog.is_channel or dialog.is_group):
                continue
            out.append({
                "id": str(dialog.id),
                "title": dialog.name or str(dialog.id),
                "username": getattr(dialog.entity, "username", None),
            })
        return out

    try:
        return sorted(_run(go(), timeout=90), key=lambda c: c["title"].lower())
    except TelegramError:
        raise
    except Exception as exc:
        raise TelegramError(f"Could not list channels: {exc}") from exc


def watch(ids: list[str]) -> dict:
    """Choose which channels are read."""
    with _lock:
        _state["channels"] = [str(i) for i in ids]
    return status()


# ── Polling ────────────────────────────────────────────────────

# The id of the newest message already read, per channel, so a poll asks only
# for what has arrived since.
_marks: dict[str, int] = {}


def poll_once(on_message) -> int:
    """Read whatever is new in the watched channels. Returns how many."""
    _need_telethon()
    if not _state["signed_in"]:
        raise TelegramError("Not signed in.")
    watched = list(_state["channels"])
    if not watched:
        return 0

    async def go():
        found = []
        for chan in watched:
            entity = int(chan) if chan.lstrip("-").isdigit() else chan
            mark = _marks.get(chan)
            try:
                messages = await _client.get_messages(
                    entity,
                    limit=PER_CHANNEL if mark else BACKFILL,
                    min_id=mark or 0,
                )
            except Exception as exc:  # one bad channel must not stop the rest
                with _lock:
                    _state["last_error"] = f"{chan}: {exc}"
                continue
            title = ""
            for msg in reversed(list(messages)):
                if not getattr(msg, "message", None):
                    continue
                _marks[chan] = max(_marks.get(chan, 0), msg.id)
                found.append({
                    "id": f"{chan}:{msg.id}",
                    "channel": chan,
                    "channel_title": title,
                    "text": msg.message,
                    "at": (msg.date or dt.datetime.now(dt.timezone.utc))
                        .isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "url": None,
                })
        return found

    try:
        messages = _run(go(), timeout=120)
    except TelegramError:
        raise
    except Exception as exc:
        raise TelegramError(f"Reading channels failed: {exc}") from exc

    for message in messages:
        on_message(message)
    with _lock:
        _state["last_poll"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        _state["counted"] += len(messages)
    return len(messages)


def start_polling(on_message) -> dict:
    """Read the watched channels every minute until told to stop."""
    global _poller
    _need_telethon()
    if not _state["signed_in"]:
        raise TelegramError("Not signed in.")

    stop_polling()
    _stop.clear()

    def loop():
        while not _stop.is_set():
            try:
                poll_once(on_message)
            except Exception as exc:
                with _lock:
                    _state["last_error"] = str(exc)
            # wait() rather than sleep(), so stopping is immediate instead of
            # taking up to a minute to notice.
            _stop.wait(POLL_SECONDS)

    _poller = threading.Thread(target=loop, name="telegram-poll", daemon=True)
    _poller.start()
    with _lock:
        _state["polling"] = True
    return status()


def stop_polling() -> dict:
    _stop.set()
    with _lock:
        _state["polling"] = False
    return status()


def status() -> dict:
    with _lock:
        out = dict(_state)
    out["available"] = HAVE_TELETHON
    out["interval"] = POLL_SECONDS
    return out
