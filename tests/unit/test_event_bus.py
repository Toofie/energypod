"""Red-phase contract tests for the in-process event bus.

``energypod.application.events.EventBus`` is the only ``EventSource`` behind the
WebSocket adapter (``energypod.api.rest._stream_events``), so the subscription
shape is pinned to that consumption contract: ``subscribe(after_sequence=...)``
returns an async iterator of JSON-serializable dict events carrying an int
``sequence``. The production module is loaded inside a fixture so this
test-first suite collects before the implementation exists; a missing contract
is reported as an ordinary test failure.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

VOCABULARY = (
    "observation.updated",
    "decision.updated",
    "unit.lifecycle",
    "unit.armed",
    "intent.accepted",
    "emergency_stop.latched",
)

_MARKER_TYPE = re.compile(r"resync|gap|discontinuity", re.IGNORECASE)
_MARKER_FLAGS = ("discontinuity", "gap", "resync", "resync_required")
_CREDENTIAL_KEY = re.compile(
    r"token|secret|password|passphrase|credential|authorization|authorisation"
    r"|cookie|api[_-]?key|bearer",
    re.IGNORECASE,
)

# Sentinel for an idle live stream: the subscription is not exhausted, it is
# simply waiting for the next publication, and the test must never hang on it.
_EXHAUSTED: Any = object()


class FakeClock:
    """Deterministic injected clock; the bus must never read ambient time."""

    def __init__(self, wall: datetime | None = None) -> None:
        self.wall = wall if wall is not None else datetime(2026, 8, 21, tzinfo=UTC)

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def bus_module() -> Any:
    try:
        return importlib.import_module("energypod.application.events")
    except ImportError as error:
        pytest.fail(f"event bus contract is not implemented: {error}", pytrace=False)


def make_bus(
    module: Any,
    *,
    retention: int = 64,
    queue_capacity: int = 64,
    clock: FakeClock | None = None,
) -> Any:
    factory = getattr(module, "EventBus", None)
    if factory is None:
        pytest.fail("energypod.application.events.EventBus is not implemented", pytrace=False)
    return factory(
        retention=retention,
        queue_capacity=queue_capacity,
        clock=clock if clock is not None else FakeClock(),
    )


async def publish_event(bus: Any, event_type: str, payload: Mapping[str, Any] | None = None) -> int:
    body: dict[str, Any] = {"type": event_type, "payload": dict(payload or {})}
    return await bus.publish(body)


async def publish_events(bus: Any, *, start: int, count: int) -> None:
    for index in range(start, start + count):
        await publish_event(bus, "observation.updated", {"index": index})


def is_resync_marker(event: Any) -> bool:
    """A discontinuity marker must be recognizable without vocabulary guesses."""
    if not isinstance(event, dict):
        return False
    event_type = event.get("type")
    if isinstance(event_type, str) and _MARKER_TYPE.search(event_type):
        return True
    return any(event.get(flag) is True for flag in _MARKER_FLAGS)


def has_int_sequence(event: Any) -> bool:
    sequence = event.get("sequence") if isinstance(event, dict) else None
    return isinstance(sequence, int) and not isinstance(sequence, bool)


def credential_shaped_keys(value: Any, path: str = "event") -> list[str]:
    if isinstance(value, dict):
        found = [f"{path}.{key}" for key in value if _CREDENTIAL_KEY.search(str(key))]
        for key, nested in value.items():
            found.extend(credential_shaped_keys(nested, f"{path}.{key}"))
        return found
    if isinstance(value, list | tuple):
        found: list[str] = []
        for index, nested in enumerate(value):
            found.extend(credential_shaped_keys(nested, f"{path}[{index}]"))
        return found
    return []


def occurred_at_instant(event: Mapping[str, Any]) -> datetime:
    """Parse ``occurred_at`` leniently: any ISO spelling of the injected stamp."""
    parsed = datetime.fromisoformat(str(event["occurred_at"]))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def settle_until(predicate: Callable[[], bool], turns: int = 200) -> bool:
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


async def next_event(iterator: Any, *, turns: int = 80) -> Any:
    """Advance the subscription once without ever hanging on an idle stream."""
    advance = asyncio.create_task(anext(iterator))
    if not await settle_until(advance.done, turns):
        advance.cancel()
        with suppress(asyncio.CancelledError):
            await advance
        return _EXHAUSTED
    try:
        return advance.result()
    except StopAsyncIteration:
        return _EXHAUSTED


async def drain(iterator: Any, limit: int) -> list[Any]:
    events: list[Any] = []
    for _ in range(limit):
        event = await next_event(iterator)
        if event is _EXHAUSTED:
            break
        events.append(event)
    return events


async def close_subscription(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        with suppress(Exception):
            await close()


@pytest.mark.parametrize(
    "bound",
    [
        {"retention": 0},
        {"retention": -1},
        {"queue_capacity": 0},
        {"queue_capacity": -4},
    ],
)
def test_constructor_rejects_non_positive_bounds(bus_module: Any, bound: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {"retention": 8, "queue_capacity": 2, "clock": FakeClock()}
    with pytest.raises(ValueError):
        make_bus(bus_module, **{**kwargs, **bound})


async def test_many_publications_stay_monotonic_gapless_and_ordered(bus_module: Any) -> None:
    bus = make_bus(bus_module, retention=16, queue_capacity=512)
    iterator = bus.subscribe(after_sequence=0)
    total = 250
    for index in range(total):
        event_type = VOCABULARY[index % len(VOCABULARY)]
        await publish_event(bus, event_type, {"index": index})
        assert bus.snapshot_sequence() == index + 1
    events = await drain(iterator, total)
    assert [event["sequence"] for event in events] == list(range(1, total + 1))
    expected_types = [VOCABULARY[index % len(VOCABULARY)] for index in range(total)]
    assert [event["type"] for event in events] == expected_types
    await close_subscription(iterator)


async def test_snapshot_sequence_reflects_latest_state_despite_eviction(
    bus_module: Any,
) -> None:
    bus = make_bus(bus_module, retention=8, queue_capacity=64)
    assert bus.snapshot_sequence() == 0
    await publish_events(bus, start=0, count=30)
    assert bus.snapshot_sequence() == 30
    iterator = bus.subscribe(after_sequence=29)
    events = await drain(iterator, 2)
    assert [event["sequence"] for event in events] == [30]
    await close_subscription(iterator)


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (0, list(range(1, 31))),
        (26, list(range(27, 31))),
        (29, [30]),
        (30, []),
    ],
)
async def test_subscribe_yields_exactly_the_events_after_the_cursor(
    bus_module: Any, after: int, expected: list[int]
) -> None:
    bus = make_bus(bus_module, retention=64, queue_capacity=64)
    await publish_events(bus, start=0, count=30)
    iterator = bus.subscribe(after_sequence=after)
    events = await drain(iterator, len(expected) + 2)
    assert [event["sequence"] for event in events] == expected
    assert not any(is_resync_marker(event) for event in events)
    await close_subscription(iterator)


@pytest.mark.parametrize(("after", "expect_marker"), [(22, False), (21, True), (0, True)])
async def test_retention_window_boundary_is_exact(
    bus_module: Any, after: int, expect_marker: bool
) -> None:
    # Retention 8 keeps only sequences 23..30 of the thirty published events.
    bus = make_bus(bus_module, retention=8, queue_capacity=64)
    await publish_events(bus, start=0, count=30)
    iterator = bus.subscribe(after_sequence=after)
    events = await drain(iterator, 10)
    if expect_marker:
        assert len(events) == 1
        assert is_resync_marker(events[0]), f"expected a discontinuity marker, got {events!r}"
    else:
        assert [event["sequence"] for event in events] == list(range(23, 31))
        assert not any(is_resync_marker(event) for event in events)
    await close_subscription(iterator)


async def test_replayed_window_transitions_into_live_events_without_a_gap(
    bus_module: Any,
) -> None:
    # API_CONTRACTS event bus: "yields events with sequence > after_sequence in
    # order" must also hold across the replay-to-live seam. The WebSocket adapter
    # terminates the socket on a sequence gap, so an implementation that replays
    # the retained window and then starts live delivery at the snapshot sequence
    # (skipping the first live event) is a contract violation.
    bus = make_bus(bus_module, retention=8, queue_capacity=64)
    await publish_events(bus, start=0, count=30)
    iterator = bus.subscribe(after_sequence=26)
    replayed = await drain(iterator, 4)
    assert [event["sequence"] for event in replayed] == [27, 28, 29, 30]

    await publish_events(bus, start=30, count=2)
    live = await drain(iterator, 4)
    assert [event["sequence"] for event in live] == [31, 32]
    assert not any(is_resync_marker(event) for event in replayed + live), (
        "an in-window cursor must see neither a marker nor a skipped live head"
    )
    await close_subscription(iterator)


async def test_stale_cursor_resyncs_from_live_edge_instead_of_replaying_history(
    bus_module: Any,
) -> None:
    bus = make_bus(bus_module, retention=8, queue_capacity=64)
    await publish_events(bus, start=0, count=30)
    latest = bus.snapshot_sequence()
    assert latest == 30
    iterator = bus.subscribe(after_sequence=10)
    marker = await next_event(iterator)
    assert is_resync_marker(marker), f"expected an explicit discontinuity marker, got {marker!r}"
    await publish_event(bus, "unit.armed", {"unit_id": "pod-a"})
    await publish_event(bus, "unit.armed", {"unit_id": "pod-b"})
    events = await drain(iterator, 4)
    sequences = [event["sequence"] for event in events]
    assert sequences == [31, 32]
    assert all(sequence > latest for sequence in sequences)
    await close_subscription(iterator)


async def test_gated_slow_consumer_never_stalls_publishes_and_gets_a_resync_marker(
    bus_module: Any,
) -> None:
    # Retention churn and queue overflow press on the publisher at once while
    # the only subscriber is gated shut: publishing must still run to completion.
    bus = make_bus(bus_module, retention=4, queue_capacity=2)
    await publish_event(bus, "observation.updated", {"index": 0})
    gate = asyncio.Event()
    iterator = bus.subscribe(after_sequence=1)

    async def gated_reads() -> list[Any]:
        await gate.wait()
        # Drain everything the queue still holds once the gate opens, so the
        # bounded-delivery assertions below can actually fail.
        return await drain(iterator, 16)

    consumer = asyncio.create_task(gated_reads())
    publishes = asyncio.create_task(publish_events(bus, start=1, count=12))
    if not await settle_until(publishes.done):
        publishes.cancel()
        consumer.cancel()
        await asyncio.gather(publishes, consumer, return_exceptions=True)
        pytest.fail(
            "a slow consumer stalled publishing: the safety path must never block",
            pytrace=False,
        )
    assert bus.snapshot_sequence() == 13

    gate.set()
    items = await consumer
    assert any(is_resync_marker(item) for item in items), (
        f"a subscriber with dropped events must be told to resync, saw {items!r}"
    )
    delivered = [item["sequence"] for item in items if has_int_sequence(item)]
    assert delivered, "the live edge must still reach the subscriber after the marker"
    assert delivered == sorted(set(delivered))
    assert delivered[-1] == 13, "the newest published event must survive the drop"
    assert 2 not in delivered, "history dropped from the bounded queue must not be replayed"
    assert len(delivered) <= 2, (
        "the per-subscriber queue is bounded at queue_capacity=2, not an unbounded buffer"
    )
    await close_subscription(iterator)


async def test_concurrent_publishers_produce_one_gapless_sequence_each_time(
    bus_module: Any,
) -> None:
    bus = make_bus(bus_module, retention=8, queue_capacity=256)
    iterator = bus.subscribe(after_sequence=0)

    async def publish_batch(worker: int) -> None:
        for index in range(10):
            await publish_event(bus, "decision.updated", {"worker": worker, "index": index})

    await asyncio.gather(*(publish_batch(worker) for worker in range(4)))
    events = await drain(iterator, 40)
    assert bus.snapshot_sequence() == 40
    assert len(events) == 40
    sequences = [event["sequence"] for event in events]
    assert sorted(sequences) == list(range(1, 41))
    assert sequences == sorted(sequences)
    await close_subscription(iterator)


async def test_event_bodies_are_json_serializable_with_server_assigned_metadata_only(
    bus_module: Any,
) -> None:
    clock = FakeClock()
    base = clock.wall_now()
    bus = make_bus(bus_module, retention=8, queue_capacity=8, clock=clock)
    iterator = bus.subscribe(after_sequence=0)
    first_payload = {"status": "authorized", "watts": 900}
    second_payload = {"stop_id": "stop-1", "unit_ids": ["pod-a"]}
    first_sequence = await publish_event(bus, "decision.updated", first_payload)
    clock.advance(5.0)
    second_sequence = await publish_event(bus, "emergency_stop.latched", second_payload)
    assert isinstance(first_sequence, int) and not isinstance(first_sequence, bool)
    assert (first_sequence, second_sequence) == (1, 2)

    first, second = await drain(iterator, 2)
    for event, event_type, payload in (
        (first, "decision.updated", first_payload),
        (second, "emergency_stop.latched", second_payload),
    ):
        assert isinstance(event, dict)
        # API_CONTRACTS: bodies carry AT LEAST type/sequence/occurred_at/payload;
        # additional non-secret metadata (for example a unique event id) is allowed.
        assert {"type", "sequence", "occurred_at", "payload"} <= set(event)
        assert event["type"] == event_type
        assert event["payload"] == payload
        sequence = event["sequence"]
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        occurred_at = event["occurred_at"]
        assert isinstance(occurred_at, str) and occurred_at
        json.dumps(event, allow_nan=False, sort_keys=True)
    assert first["sequence"] == first_sequence
    assert second["sequence"] == second_sequence
    # occurred_at is stamped from the injected clock: the first publication at the
    # base instant, the second exactly 5 seconds later. A counter-derived or
    # ambient-clock stamp cannot match both.
    assert occurred_at_instant(first) == base
    assert occurred_at_instant(second) == base + timedelta(seconds=5.0)
    await close_subscription(iterator)


async def test_caller_supplied_sequence_and_occurred_at_are_never_trusted(
    bus_module: Any,
) -> None:
    bus = make_bus(bus_module, retention=8, queue_capacity=8)
    iterator = bus.subscribe(after_sequence=0)
    forged_timestamp = "1970-01-01T00:00:00+00:00"
    body = {
        "type": "observation.updated",
        "payload": {"unit_id": "pod-a"},
        "sequence": 999,
        "occurred_at": forged_timestamp,
    }
    assigned = await bus.publish(body)
    assert assigned == 1
    events = await drain(iterator, 1)
    assert len(events) == 1
    event = events[0]
    assert event["sequence"] == 1
    assert event["occurred_at"] != forged_timestamp
    assert bus.snapshot_sequence() == 1
    await close_subscription(iterator)


async def test_bus_added_envelope_keys_never_look_like_credentials(bus_module: Any) -> None:
    # API_CONTRACTS: "Event bodies are JSON-serializable, credential-free". The
    # bus's share of that duty is structural: whatever envelope keys it adds on
    # top of the payload (sequence, occurred_at, metadata) must never be
    # credential-shaped. This ban is independent of the published payloads: the
    # bus is explicitly not a payload filter, and publishers own their payload
    # hygiene.
    bus = make_bus(bus_module, retention=4, queue_capacity=8, clock=FakeClock())
    iterator = bus.subscribe(after_sequence=0)
    payload = {
        "unit_ids": ["pod-a", "pod-b"],
        "requested_power": {"direction": "discharge", "watts": 900},
        "reason": "operator dispatch",
    }
    await publish_event(bus, "intent.accepted", payload)
    await publish_event(bus, "emergency_stop.latched", {"stop_id": "stop-1"})
    events = await drain(iterator, 2)
    assert len(events) == 2
    for event in events:
        assert isinstance(event, dict)
        bus_added = {key: value for key, value in event.items() if key != "payload"}
        assert credential_shaped_keys(bus_added) == []
        json.dumps(event, allow_nan=False, sort_keys=True)
    assert events[0]["payload"] == payload
    assert events[1]["payload"] == {"stop_id": "stop-1"}
    await close_subscription(iterator)


async def test_identical_publication_scripts_produce_identical_event_streams(
    bus_module: Any,
) -> None:
    script = [
        (VOCABULARY[index % len(VOCABULARY)], {"watts": 25 * index % 900}) for index in range(12)
    ]
    left_clock, right_clock = FakeClock(), FakeClock()
    left = make_bus(bus_module, retention=6, queue_capacity=32, clock=left_clock)
    right = make_bus(bus_module, retention=6, queue_capacity=32, clock=right_clock)
    left_iterator = left.subscribe(after_sequence=0)
    right_iterator = right.subscribe(after_sequence=0)
    for event_type, payload in script:
        await publish_event(left, event_type, payload)
        await publish_event(right, event_type, payload)
        left_clock.advance(1.0)
        right_clock.advance(1.0)

    def projection(event: dict[str, Any]) -> tuple[Any, ...]:
        return (
            event.get("type"),
            event.get("sequence"),
            event.get("occurred_at"),
            event.get("payload"),
        )

    left_events = await drain(left_iterator, len(script))
    right_events = await drain(right_iterator, len(script))
    assert [projection(event) for event in left_events] == [
        projection(event) for event in right_events
    ]
    assert [event["sequence"] for event in left_events] == list(range(1, len(script) + 1))
    await close_subscription(left_iterator)
    await close_subscription(right_iterator)


async def test_subscribers_with_different_cursors_are_isolated(bus_module: Any) -> None:
    bus = make_bus(bus_module, retention=64, queue_capacity=64)
    await publish_events(bus, start=0, count=12)
    from_middle = bus.subscribe(after_sequence=2)
    from_late = bus.subscribe(after_sequence=9)
    middle_events = await drain(from_middle, 12)
    late_events = await drain(from_late, 12)
    assert [event["sequence"] for event in middle_events] == list(range(3, 13))
    assert [event["sequence"] for event in late_events] == list(range(10, 13))
    assert not any(is_resync_marker(event) for event in middle_events + late_events)
    assert bus.snapshot_sequence() == 12
    await close_subscription(from_middle)
    await close_subscription(from_late)


async def test_subscribe_without_a_cursor_yields_only_events_published_after_it(
    bus_module: Any,
) -> None:
    # The adapter's EventSource protocol types after_sequence as `int | None`; a
    # cursor-less subscription is live-only: nothing retained before the
    # subscription is replayed, and everything published afterwards arrives.
    bus = make_bus(bus_module, retention=8, queue_capacity=64)
    await publish_events(bus, start=0, count=30)
    iterator = bus.subscribe(after_sequence=None)
    await publish_events(bus, start=30, count=2)
    events = await drain(iterator, 6)
    sequences = [event["sequence"] for event in events if has_int_sequence(event)]
    assert sequences == [31, 32]
    await close_subscription(iterator)


async def test_subscription_matches_the_adapter_eventsource_shape(bus_module: Any) -> None:
    bus = make_bus(bus_module, retention=8, queue_capacity=8)
    with pytest.raises(TypeError):
        bus.subscribe(0)
    iterator = bus.subscribe(after_sequence=0)
    assert hasattr(iterator, "__aiter__")
    assert hasattr(iterator, "__anext__")
    assert hasattr(iterator, "aclose")
    await publish_event(bus, "unit.lifecycle", {"unit_id": "pod-a", "lifecycle": "disarmed"})
    event = await next_event(iterator)
    assert isinstance(event, dict)
    assert isinstance(event.get("sequence"), int) and not isinstance(event.get("sequence"), bool)
    await close_subscription(iterator)
