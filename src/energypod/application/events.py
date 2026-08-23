"""In-process event bus: the sole ``EventSource`` behind the API adapters.

The bus owns one strictly monotonic publication sequence, a bounded
most-recent retention window, and one bounded queue per subscriber. A
publisher is never blocked by a slow consumer: a full subscriber queue
drops its backlog to a resync marker and keeps only the newest event, so a
lagging client resynchronizes from a snapshot instead of stalling the
safety path. Sequence numbers and ``occurred_at`` stamps are assigned by
the bus from the injected clock; caller-supplied values are never trusted.
The event vocabulary (for example ``intent.accepted``) belongs to
publishers; the bus is not a vocabulary filter.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol

_RESYNC_TYPE = "resync"
_RESYNC_STALE_CURSOR = "retention_window_exceeded"
_RESYNC_SLOW_CONSUMER = "slow_subscriber"
_RESYNC_FUTURE_CURSOR = "future_cursor"
# The bus overwrites these envelope fields on every publication; a caller
# supplying them is untrusted input, exactly like a forged audit sequence.
_SERVER_ASSIGNED_KEYS = frozenset({"sequence", "occurred_at"})


class Clock(Protocol):
    """Structural wall-clock port: deterministic time is injected, never ambient."""

    def wall_now(self) -> datetime: ...


def _require_positive_bound(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _build_envelope(body: Mapping[str, Any], *, sequence: int, occurred_at: str) -> dict[str, Any]:
    event_type = body.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("a published event body requires a non-empty string type")
    envelope = {key: value for key, value in body.items() if key not in _SERVER_ASSIGNED_KEYS}
    envelope.setdefault("payload", {})
    envelope["sequence"] = sequence
    envelope["occurred_at"] = occurred_at
    # Fail closed at the publisher AND detach from caller-owned objects: the
    # envelope is re-materialized from its own JSON encoding, so an event that
    # cannot be serialized surfaces here (never silently breaking a downstream
    # subscriber), and a caller mutating or aliasing its payload after publish
    # can never reach the retention window or another subscriber's queue.  The
    # retained copy is exactly the wire shape.
    detached: dict[str, Any] = json.loads(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    return detached


def _resync_marker(reason: str, snapshot_sequence: int) -> dict[str, Any]:
    """An explicit discontinuity: resynchronize from a snapshot, not stale replay."""
    return {
        "type": _RESYNC_TYPE,
        "resync": True,
        "reason": reason,
        "snapshot_sequence": snapshot_sequence,
    }


class _Subscription:
    """One subscriber's bounded, strictly ordered view of the stream."""

    def __init__(
        self,
        bus: EventBus,
        *,
        queue_capacity: int,
        cursor: int,
        replay: tuple[dict[str, Any], ...] = (),
        resync: tuple[str, int] | None = None,
    ) -> None:
        self._bus = bus
        self._cursor = cursor
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=queue_capacity)
        # (reason, snapshot_sequence): the snapshot point is fixed when the
        # discontinuity is detected, never restamped from the live sequence at
        # delivery time, so nothing older than the announced snapshot can be
        # delivered after the marker.
        self._pending_resync = resync
        self._closed = False
        # The bus keeps only this weak reference, so a subscription abandoned
        # without ``aclose`` (a disconnected client whose adapter never ran
        # its finally block) is garbage-collected instead of being drained on
        # every publish forever.  The reference lives on the subscription so
        # ``_detach`` removes exactly its own entry: distinct ``weakref.ref``
        # objects to the same subscription never compare equal.
        self._self_reference = weakref.ref(self)
        for event in replay:
            self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._closed:
            raise StopAsyncIteration
        if self._pending_resync is None:
            event = await self._queue.get()
            if event is None or self._closed:
                raise StopAsyncIteration
            if self._pending_resync is None:
                return event
            # A drop-to-resync fired while this reader was parked on the
            # queue: the discontinuity precedes the event the queue just
            # handed over, so the marker still goes first. Requeueing is
            # safe because the slot this event occupied is now free.
            self._queue.put_nowait(event)
        # The marker precedes any queued event so the client learns about
        # the discontinuity before consuming newer state.
        reason, snapshot_sequence = self._pending_resync
        self._pending_resync = None
        self._drop_superseded(snapshot_sequence)
        return _resync_marker(reason, snapshot_sequence)

    async def aclose(self) -> None:
        self._bus._detach(self)
        self._closed = True
        # Wake a reader blocked on an empty queue so it observes the close.
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    def offer(self, event: dict[str, Any]) -> None:
        """Enqueue one published event; this must never block the publisher."""
        if self._closed or event["sequence"] <= self._cursor:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop-to-resync: discard the backlog, flag the discontinuity, and
            # keep only the newest event so the live edge always survives. The
            # marker's snapshot point is that surviving event's sequence: every
            # event queued after the drop is strictly newer, so a client that
            # resynchronizes from the announced snapshot is never handed
            # history the snapshot already supersedes.
            while not self._queue.empty():
                self._queue.get_nowait()
            if self._pending_resync is None:
                # A later drop while a marker is already pending keeps the
                # first (oldest) snapshot point: it stays a lower bound for
                # everything still queued behind the pending marker.
                self._pending_resync = (_RESYNC_SLOW_CONSUMER, event["sequence"])
            self._queue.put_nowait(event)

    def _drop_superseded(self, snapshot_sequence: int) -> None:
        """Discard queued events older than the marker's snapshot point.

        By construction the queue only holds events at least as new as the
        pending marker's snapshot point; draining here keeps that guarantee
        local to marker delivery instead of trusting every producer of a
        pending marker. The drain and re-queue are one synchronous section,
        so no publication can interleave and overflow the queue.
        """
        retained: list[dict[str, Any] | None] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, dict):
                sequence = item.get("sequence")
                if (
                    isinstance(sequence, int)
                    and not isinstance(sequence, bool)
                    and sequence < snapshot_sequence
                ):
                    continue
            retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)


class EventBus:
    """Bounded monotonic fan-out publisher; the only ``EventSource``."""

    def __init__(self, *, retention: int, queue_capacity: int, clock: Clock) -> None:
        _require_positive_bound("retention", retention)
        _require_positive_bound("queue_capacity", queue_capacity)
        self._clock = clock
        self._queue_capacity = queue_capacity
        self._window: deque[dict[str, Any]] = deque(maxlen=retention)
        self._subscribers: list[weakref.ReferenceType[_Subscription]] = []
        self._sequence = 0

    async def publish(self, body: Mapping[str, Any]) -> int:
        """Assign the next sequence, retain the event, and fan it out."""
        if not isinstance(body, Mapping):
            raise TypeError("a published event body must be a mapping")
        sequence = self._sequence + 1
        envelope = _build_envelope(body, sequence=sequence, occurred_at=self._occurred_at())
        # Sequence commit, retention, and fan-out share one synchronous
        # section: a publication is never interleaved with another publish
        # or with consumer progress, so every queue observes strict sequence
        # order and a failed publication consumes no sequence at all.
        self._sequence = sequence
        self._window.append(envelope)
        for reference in tuple(self._subscribers):
            subscriber = reference()
            if subscriber is None:
                # The subscription was abandoned without aclose: reclaim its
                # entry instead of draining a dead queue on every publish.
                with suppress(ValueError):
                    self._subscribers.remove(reference)
                continue
            subscriber.offer(envelope)
        return sequence

    def snapshot_sequence(self) -> int:
        """The sequence of the latest published snapshot state."""
        return self._sequence

    def subscribe(self, *, after_sequence: int | None) -> AsyncIterator[dict[str, Any]]:
        """Yield events with ``sequence > after_sequence`` in order.

        A cursor older than the retained window cannot be served history it
        still needs, so delivery restarts at the live edge behind one
        explicit resync marker. An absent cursor is live-only and replays
        nothing. Envelopes are shared between subscribers and the retention
        window and must be treated as immutable.
        """
        cursor, replay, resync = self._subscription_start(after_sequence)
        subscription = _Subscription(
            self,
            queue_capacity=self._queue_capacity,
            cursor=cursor,
            replay=replay,
            resync=resync,
        )
        self._subscribers.append(subscription._self_reference)
        return subscription

    def _subscription_start(
        self, after_sequence: int | None
    ) -> tuple[int, tuple[dict[str, Any], ...], tuple[str, int] | None]:
        if after_sequence is None:
            # A cursor-less subscription is live-only; it never replays.
            return self._sequence, (), None
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise TypeError("after_sequence must be an integer or None")
        if after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if after_sequence > self._sequence:
            # A cursor ahead of the live edge names history that does not
            # exist: the client's view is desynchronized (for example carried
            # over from another process instance).  Saying nothing would
            # silently suppress every event up to the fabricated sequence, so
            # the subscriber is told to resynchronize from the live snapshot
            # instead and then continues from the live edge.
            return self._sequence, (), (_RESYNC_FUTURE_CURSOR, self._sequence)
        if self._window:
            oldest = self._window[0]["sequence"]
            if after_sequence < oldest - 1:
                # The cursor predates the retained window: events it still
                # needs were evicted, so resynchronize instead of replaying a
                # partial history that would look contiguous to the client.
                return self._sequence, (), (_RESYNC_STALE_CURSOR, self._sequence)
        replay = tuple(event for event in self._window if event["sequence"] > after_sequence)
        if len(replay) >= self._queue_capacity:
            # The bounded queue must hold the replay AND leave a slot for the
            # live edge: a replay that exactly fills it would leave the very
            # first live publish no room, which would annihilate the whole
            # replay as a spurious slow-consumer drop. Such a subscriber takes
            # the same resynchronization path as a stale cursor — told up
            # front, before any replayed event is delivered — rather than a
            # silently truncated replay.
            return self._sequence, (), (_RESYNC_STALE_CURSOR, self._sequence)
        return after_sequence, replay, None

    def _occurred_at(self) -> str:
        now = self._clock.wall_now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock.wall_now must return a timezone-aware datetime")
        return now.isoformat()

    def _detach(self, subscription: _Subscription) -> None:
        reference = subscription._self_reference
        with suppress(ValueError):
            self._subscribers.remove(reference)


__all__ = ["Clock", "EventBus"]
