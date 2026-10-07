"""Tests for the per-connection SSE broadcaster (issue #11).

The bug being prevented: a single asyncio.Queue shared across all /api/events
clients meant two browsers on the same server would steal events from each
other. These tests pin the fan-out contract end-to-end.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_decoder.events_bus import EventBroadcaster


async def test_two_subscribers_both_receive_published_event() -> None:
    """Fan-out: each subscriber gets its own copy of every published event."""
    bus = EventBroadcaster()
    a = bus.subscribe()
    b = bus.subscribe()

    bus.publish({"type": "refresh", "path": "/tmp/x.jsonl"})

    ev_a = await asyncio.wait_for(a.get(), timeout=1.0)
    ev_b = await asyncio.wait_for(b.get(), timeout=1.0)
    assert ev_a == {"type": "refresh", "path": "/tmp/x.jsonl"}
    assert ev_b == {"type": "refresh", "path": "/tmp/x.jsonl"}


async def test_unsubscribe_stops_delivery_and_cleans_up() -> None:
    """After unsubscribe, the queue receives no more events and count drops."""
    bus = EventBroadcaster()
    q = bus.subscribe()
    assert bus.subscriber_count == 1

    bus.unsubscribe(q)
    assert bus.subscriber_count == 0

    bus.publish({"type": "refresh"})
    # Nothing was delivered to the unsubscribed queue.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.05)


async def test_unsubscribe_is_idempotent() -> None:
    """Calling unsubscribe twice (or on an unknown queue) must not raise."""
    bus = EventBroadcaster()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.unsubscribe(q)  # no error
    bus.unsubscribe(asyncio.Queue())  # unknown queue, also no error


async def test_publish_with_no_subscribers_is_a_noop() -> None:
    """Producing events before any client connects must not raise."""
    bus = EventBroadcaster()
    bus.publish({"type": "refresh"})  # no observers
    assert bus.subscriber_count == 0


async def test_full_queue_drops_event_for_slow_consumer_only() -> None:
    """A slow consumer must not block the publisher or starve other subscribers."""
    bus = EventBroadcaster()
    slow = bus.subscribe()
    fast = bus.subscribe()
    # Fill the slow consumer's queue past capacity.
    for i in range(EventBroadcaster._QUEUE_MAXSIZE + 5):
        bus.publish({"type": "refresh", "n": i})

    # Fast consumer still received the first MAXSIZE events.
    drained = 0
    while not fast.empty():
        await fast.get()
        drained += 1
    assert drained == EventBroadcaster._QUEUE_MAXSIZE

    # Slow consumer is bounded at MAXSIZE — extras were dropped, not buffered.
    assert slow.qsize() == EventBroadcaster._QUEUE_MAXSIZE


async def test_session_meta_event_shape_round_trips() -> None:
    """Sanity: session-meta payloads come through structurally unchanged."""
    bus = EventBroadcaster()
    q = bus.subscribe()
    payload = {"type": "session-meta", "id": "abc-123", "title": "Renamed"}
    bus.publish(payload)
    got = await asyncio.wait_for(q.get(), timeout=1.0)
    assert got == payload
