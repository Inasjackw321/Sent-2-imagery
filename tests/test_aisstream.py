"""The stream, against a stand-in that misbehaves the way the real one does.

These run a WebSocket server on localhost rather than mocking, because the
bugs worth catching here were all about how the connection *ends* -- and a
mock that returns a tidy list cannot reproduce a server that hangs up without
a close handshake.
"""

from __future__ import annotations

import asyncio
import json
import threading

import websockets

from backend import aisstream

BOX = (20.0, 59.0, 25.0, 61.0)


def _serve(handler, listen=2.0, asks=1):
    """Run one client request against a handler, and give back the result.

    vessels_in runs its own event loop, so the client goes on a thread and the
    server keeps this one.
    """
    outcome: dict = {}

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            aisstream.ENDPOINT = f"ws://127.0.0.1:{port}"
            aisstream.LISTEN_SECONDS = listen
            aisstream.set_key("pretend-key")
            aisstream._cache.update(at=0.0, box=None, data=None)

            def client():
                try:
                    outcome["ok"] = aisstream.vessels_in(BOX)
                    if asks > 1:
                        outcome["second"] = aisstream.vessels_in(BOX)
                except aisstream.StreamError as exc:
                    outcome["err"] = str(exc)

            thread = threading.Thread(target=client)
            thread.start()
            while thread.is_alive():
                await asyncio.sleep(0.05)

    try:
        asyncio.run(run())
    finally:
        aisstream.set_key(None)
        aisstream._cache.update(at=0.0, box=None, data=None)
    return outcome


def _position(index):
    return json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 1000 + index, "ShipName": f"SHIP{index}",
                     "time_utc": "2026-08-28 10:00:00 +0000 UTC"},
        "Message": {"PositionReport": {
            "Latitude": 60.0 + index * 0.1, "Longitude": 22.0,
            "Sog": 8.0, "Cog": 90.0, "TrueHeading": 90}},
    })


def test_ships_survive_a_server_that_hangs_up_rudely():
    """aisstream drops the socket instead of closing it politely.

    Leaving the connection through a context manager then raises "no close
    frame received or sent", and an earlier version let that escape -- which
    threw away every ship already collected and reported a working feed as
    unreachable. This is that exact sequence.
    """
    async def handler(socket):
        await socket.recv()
        for i in range(5):
            await socket.send(_position(i))
        await asyncio.sleep(0.2)
        socket.transport.abort()      # no close handshake, just gone

    outcome = _serve(handler)
    assert "err" not in outcome, outcome.get("err")
    assert len(outcome["ok"]["vessels"]) == 5


def test_a_server_that_says_nothing_and_leaves_is_explained():
    """Accepted, then dropped, with nothing sent.

    "No vessels here" would be the wrong thing to say about it, and so would
    "the key was rejected" on its own -- an abrupt drop with no close frame is
    equally what a proxy does to a connection it let through the handshake.
    Both possibilities get named.
    """
    async def handler(socket):
        await socket.recv()
        socket.transport.abort()

    outcome = _serve(handler)
    assert "ok" not in outcome
    said = outcome["err"].lower()
    assert "key" in said
    assert any(word in said for word in ("proxy", "vpn", "firewall"))


def test_a_server_that_closes_politely_with_a_reason_quotes_it():
    """When there is a close frame with a reason, that is the whole answer."""
    async def handler(socket):
        await socket.recv()
        await socket.close(code=1008, reason="Invalid API key")

    outcome = _serve(handler)
    assert "ok" not in outcome
    assert "Invalid API key" in outcome["err"]


def test_an_error_is_not_wrapped_twice():
    """StreamError is a RuntimeError, and vessels_in used to catch its own.

    The result was "Could not run the stream: aisstream could not be
    reached: ..." -- two explanations glued together, neither of which was
    added by anything the reader could act on.
    """
    async def handler(socket):
        await socket.recv()
        await socket.send("Invalid API key")
        await asyncio.sleep(0.1)
        socket.transport.abort()

    outcome = _serve(handler)
    assert "ok" not in outcome
    assert "Could not run the stream" not in outcome["err"]
    assert "Invalid API key" in outcome["err"]


def test_a_quiet_stretch_is_not_an_error():
    """A box with little traffic sends slowly, and that is not a failure."""
    async def handler(socket):
        await socket.recv()
        await socket.send(_position(0))
        await asyncio.sleep(5.0)

    outcome = _serve(handler, listen=1.0)
    assert "err" not in outcome, outcome.get("err")
    assert len(outcome["ok"]["vessels"]) == 1


def test_the_five_minute_floor_holds_across_calls():
    """A second ask inside the window must not open a second connection.

    Both asks happen while the key is still set, since clearing it between
    them would be testing the teardown rather than the floor.
    """
    opened = {"count": 0}

    async def handler(socket):
        opened["count"] += 1
        await socket.recv()
        await socket.send(_position(0))
        await asyncio.sleep(0.2)
        socket.transport.abort()

    outcome = _serve(handler, listen=1.0, asks=2)
    assert "err" not in outcome, outcome.get("err")
    assert opened["count"] == 1, "the second ask opened another connection"
    assert outcome["second"]["cached"] is True


def test_a_close_frame_with_a_reason_is_repeated_verbatim():
    """When the server says why, nothing here can improve on it.

    The previous message asserted "the key was not accepted" for every way a
    connection could end with nothing in it, which is a guess -- and the wrong
    one when a proxy is cutting the connection after letting the handshake
    through.
    """
    from backend.aisstream import _why_nothing

    said = _why_nothing(0.3, {"code": 1008, "reason": "Invalid API key", "clean": True}, "x")
    assert "Invalid API key" in said
    assert "usually means" not in said


def test_a_clean_close_with_no_reason_still_points_at_the_key():
    from backend.aisstream import _why_nothing

    said = _why_nothing(0.4, {"code": 1000, "reason": None, "clean": True}, "x")
    assert "1000" in said
    assert "key" in said.lower()


def test_an_abrupt_drop_names_both_possibilities():
    """No close frame at all is not how a server refuses a key.

    It is how a connection gets cut by something in the middle, and saying so
    is the difference between someone checking their key forever and someone
    checking their proxy.
    """
    from backend.aisstream import _why_nothing

    said = _why_nothing(0.2, None, "no close frame received or sent")
    assert "key" in said.lower()
    assert any(word in said.lower() for word in ("proxy", "vpn", "firewall"))


def test_the_subscription_carries_no_optional_filters():
    """An optional field is a suspect that costs nothing to drop.

    A subscription the server dislikes is answered by hanging up rather than
    by saying so, and everything unwanted is ignored on arrival anyway.
    """
    import inspect

    from backend import aisstream

    source = inspect.getsource(aisstream._collect)
    assert '"BoundingBoxes"' in source
    assert '"FilterMessageTypes"' not in source.split("# No FilterMessageTypes")[0]


def test_the_connection_test_reports_a_failure_rather_than_raising():
    """The diagnostic must always answer, including when nothing works."""
    from backend import aisstream

    aisstream.set_key(None)
    out = aisstream.test_key()
    assert out["ok"] is False
    assert out["stage"] == "key"
