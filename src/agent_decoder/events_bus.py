"""Per-connection SSE broadcaster.

The /api/events endpoint historically drained a *single* asyncio.Queue, which
meant two clients pointed at the same server would steal events from each
other (only one would receive any given refresh / rename / etc.). This module
provides a small SRP fan-out so every connected client gets every event.

Contract:
    bus = EventBroadcaster()
    q = bus.subscribe()       # returns an asyncio.Queue
    await bus.publish({...})  # delivers a SHALLOW-COPY of the event to every queue
    bus.unsubscribe(q)        # idempotent cleanup; safe to call from finally:

Notes:
  - publish() never awaits on a slow consumer: if a queue fills up we drop
    the event for that subscriber rather than block the publisher. Refresh
    events are idempotent and renames have a server-authoritative cache, so
    drop-on-overflow is the right tradeoff for keeping the bus non-blocking.
  - The same broadcaster instance is shared by every emitter (file watcher,
    /api/sessions/{id}/title endpoint, future producers). Keep it tiny.
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBroadcaster:
    """Fan-out publisher with one bounded asyncio.Queue per subscriber."""

    # Queue depth large enough to absorb a burst of file-watcher events while
    # a slow client catches up, small enough that an unresponsive client
    # doesn't pin a lot of memory.
    _QUEUE_MAXSIZE = 64

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Any]] = set()

    def subscribe(self) -> asyncio.Queue[Any]:
        """Register a new subscriber and return its dedicated queue."""
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        """Remove a subscriber. Idempotent — safe in a finally block."""
        self._subscribers.discard(queue)

    def publish(self, event: Any) -> None:
        """Deliver ``event`` to every subscriber, dropping on full queues.

        Synchronous (not async) so producers from anywhere — even sync
        callbacks — can publish without awaiting. asyncio.Queue.put_nowait
        is safe from the running loop's thread.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — skip this event rather than block the bus.
                # Refresh events are idempotent; rename events are reconciled
                # against the server-authoritative response on next fetch.
                continue

    @property
    def subscriber_count(self) -> int:
        """Test/observability helper."""
        return len(self._subscribers)
