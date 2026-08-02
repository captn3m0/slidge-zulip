"""Echo suppression.

The failure this guards against is not subtle in effect but is subtle in cause: once we
forge, a relayed message comes back from Zulip wearing the original author's identity, so
upstream's "is the sender me?" check never fires and the message loops. See provenance.py.
"""

import time

from bridgelib.provenance import ProvenanceTracker


def test_unknown_message_has_no_origin():
    assert ProvenanceTracker().origin_of(12345) is None


def test_records_and_returns_origin_room():
    tracker = ProvenanceTracker()
    tracker.record(1, "!telegram:hs")
    assert tracker.origin_of(1) == "!telegram:hs"


def test_entries_expire():
    tracker = ProvenanceTracker(ttl_seconds=0.01)
    tracker.record(1, "!telegram:hs")
    time.sleep(0.02)
    assert tracker.origin_of(1) is None


def test_bounded_by_max_entries():
    tracker = ProvenanceTracker(max_entries=10)
    for message_id in range(100):
        tracker.record(message_id, f"!room{message_id}:hs")
    assert len(tracker) <= 10
    # Newest survive, oldest are evicted.
    assert tracker.origin_of(99) == "!room99:hs"
    assert tracker.origin_of(0) is None


def test_rerecording_refreshes_position():
    tracker = ProvenanceTracker(max_entries=2)
    tracker.record(1, "!a:hs")
    tracker.record(2, "!b:hs")
    tracker.record(1, "!a:hs")
    tracker.record(3, "!c:hs")
    # 2 was least recently touched, so it goes first.
    assert tracker.origin_of(2) is None
    assert tracker.origin_of(1) == "!a:hs"
    assert tracker.origin_of(3) == "!c:hs"
