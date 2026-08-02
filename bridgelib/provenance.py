"""Tracking which Zulip messages this bridge sent, and which room they came from.

Upstream recognises its own output by sender::

    if msg["sender_email"] == self.zulip_config["email"]:
        return None

That works only while every relayed message is sent by the bot. The moment we forge, a
message relayed from Telegram arrives back on the event queue attributed to *Alice*, the
check stops firing, and the message is relayed straight back out to Matrix -- where
mautrix posts it to Telegram again. The echo compounds.

So we track provenance by message id instead. ``send_message`` returns the new message's
id, we remember which Matrix room it came from, and when the event queue hands that
message back we skip *only its room of origin*.

That last detail is what makes a multi-network bridge work at all. With two portal rooms
mapped to one Zulip topic, a Telegram message must still reach the Signal room -- so
"skip the origin room" is right where "skip the message entirely" would strand it.

Entries are bounded both ways: by age, because a message id is only interesting for the
few seconds between sending it and seeing it come back, and by count, so a burst cannot
grow the map without limit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

DEFAULT_TTL_SECONDS = 900.0
DEFAULT_MAX_ENTRIES = 4096


class ProvenanceTracker:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        # message_id -> (origin_room_id, recorded_at)
        self._entries: "OrderedDict[int, tuple[str, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def record(self, message_id: int, room_id: str) -> None:
        """Note that ``message_id`` was created by relaying from ``room_id``.

        Called from the asyncio loop thread.
        """
        now = time.monotonic()
        with self._lock:
            self._entries[message_id] = (room_id, now)
            self._entries.move_to_end(message_id)
            self._evict(now)

    def origin_of(self, message_id: int) -> Optional[str]:
        """The room a message was relayed from, or None if we did not send it.

        Called from the Zulip event-queue thread, hence the lock.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return None
            room_id, recorded_at = entry
            if now - recorded_at > self.ttl_seconds:
                del self._entries[message_id]
                return None
            return room_id

    def _evict(self, now: float) -> None:
        """Drop expired entries, then oldest-first until under the cap. Caller holds the lock."""
        while self._entries:
            oldest_id = next(iter(self._entries))
            _, recorded_at = self._entries[oldest_id]
            if now - recorded_at <= self.ttl_seconds:
                break
            del self._entries[oldest_id]

        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
