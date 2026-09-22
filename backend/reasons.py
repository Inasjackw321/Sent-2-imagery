"""One readable line out of a network failure.

Unabridged, a requests exception is the connection pool, the full query
string, the retry count and a nested cause: three hundred characters of
which the useful part is "timed out". That is the wrong shape for both places
it ends up -- a side panel nine pixels tall, and a log line that arrives
dozens of times over when a host stops answering and every request in flight
discovers it separately.

So the cause is named and the rest is dropped. Shared rather than written
twice because the two callers were writing the same function, and the second
copy is where they drift.
"""

from __future__ import annotations

# Most specific first. "Max retries exceeded" is the wrapper around nearly
# every one of the others, so matching it early would throw away the cause and
# report the retry loop instead.
CAUSES = (
    "Read timed out",
    "connection timed out",
    "Connection timed out",
    "Tunnel connection failed",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Connection refused",
    "Connection reset by peer",
    "certificate verify failed",
    "Max retries exceeded",
)

LIMIT = 90


def why(exc: Exception | str, limit: int = LIMIT) -> str:
    """What went wrong, in a few words."""
    said = " ".join(str(exc).split())
    for cause in CAUSES:
        if cause in said:
            return cause
    return said[:limit] + ("…" if len(said) > limit else "")
