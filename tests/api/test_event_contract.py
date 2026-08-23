"""Red-phase WebSocket contracts for ordered, recoverable status delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from .conftest import (
    FakeAuthenticator,
    FakeEventSource,
    MutableMonotonicClock,
    RecordingEnergyService,
    load_contract_module,
)


def _app(
    service: RecordingEnergyService,
    source: Any,
    *,
    trusted_websocket_origins: frozenset[str] | None = None,
    event_ticket_ttl_s: float | None = None,
    event_ticket_clock: Any = None,
) -> object:
    module = load_contract_module("energypod.api.rest")
    assert hasattr(module, "create_api_app")
    app_kwargs: dict[str, Any] = {
        "service": service,
        "authenticator": FakeAuthenticator(),
        "event_source": source,
        "auth_required": True,
        "websocket_queue_capacity": 2,
        "trusted_websocket_origins": trusted_websocket_origins,
    }
    if event_ticket_ttl_s is not None:
        app_kwargs["event_ticket_ttl_s"] = event_ticket_ttl_s
    if event_ticket_clock is not None:
        app_kwargs["event_ticket_clock"] = event_ticket_clock
    return module.create_api_app(**app_kwargs)


def _event_ticket(client: TestClient, credential: str = "viewer-token") -> str:
    response = client.post(
        "/api/v1/events/session", headers={"Authorization": f"Bearer {credential}"}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["ticket"])


def test_websocket_requires_viewer_authentication() -> None:
    with TestClient(_app(RecordingEnergyService(), FakeEventSource())) as client:
        try:
            with client.websocket_connect("/api/v1/events"):
                raise AssertionError("unauthenticated WebSocket unexpectedly connected")
        except (WebSocketDisconnect, WebSocketDenialResponse) as exc:
            assert getattr(exc, "code", None) in {1008, 4401} or "403" in str(exc)


def test_websocket_rejects_credentials_in_query_string_without_leaking_them() -> None:
    credential = "viewer" + "-token"
    with (
        TestClient(_app(RecordingEnergyService(), FakeEventSource())) as client,
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)) as caught,
        client.websocket_connect(f"/api/v1/events?access_token={credential}"),
    ):
        raise AssertionError("query-string credential unexpectedly authenticated")
    assert credential not in str(caught.value)


def test_websocket_rejects_untrusted_browser_origin_before_subscription() -> None:
    source = FakeEventSource()
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        client.websocket_connect(
            "/api/v1/events",
            headers={
                "Authorization": "Bearer viewer-token",
                "Origin": "https://attacker.invalid",
            },
        ),
    ):
        raise AssertionError("cross-origin WebSocket unexpectedly connected")
    assert source.subscriptions == []


def test_websocket_accepts_configured_same_origin_browser_client() -> None:
    source = FakeEventSource()
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                trusted_websocket_origins=frozenset({"https://manager.test"}),
            ),
            base_url="https://manager.test",
        ) as client,
        client.websocket_connect(
            "/api/v1/events",
            headers={
                "Authorization": "Bearer viewer-token",
                "Origin": "https://manager.test",
            },
        ) as websocket,
    ):
        assert websocket.receive_json()["type"] == "snapshot"
    assert source.subscriptions == [20]


def test_websocket_rejects_origin_absent_from_configured_trusted_set() -> None:
    source = FakeEventSource()
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                trusted_websocket_origins=frozenset({"https://manager.test"}),
            )
        ) as client,
        pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)),
        client.websocket_connect(
            "/api/v1/events",
            headers={
                "Authorization": "Bearer viewer-token",
                "Origin": "https://attacker.invalid",
            },
        ),
    ):
        raise AssertionError("origin outside the configured trusted set unexpectedly connected")
    assert source.subscriptions == []


def test_websocket_sends_authoritative_snapshot_before_ordered_events() -> None:
    source = FakeEventSource(
        [
            {"sequence": 21, "type": "observation.updated", "data": {"unit_id": "pod-a"}},
            {"sequence": 22, "type": "decision.updated", "data": {"unit_id": "pod-a"}},
        ]
    )
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        snapshot = websocket.receive_json()
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["sequence"] == 20
    assert first["sequence"] == 21
    assert second["sequence"] == 22
    assert source.subscriptions == [20]


def test_reconnect_cursor_recovers_gap_with_snapshot_not_stale_replay() -> None:
    source = FakeEventSource(
        [{"sequence": 51, "type": "observation.updated", "data": {"unit_id": "pod-a"}}]
    )
    service = RecordingEnergyService()
    with (
        TestClient(_app(service, source)) as client,
        client.websocket_connect(
            "/api/v1/events?after=10", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        recovery = websocket.receive_json()

    assert recovery["type"] == "snapshot"
    assert recovery["sequence"] == 20
    assert recovery["recovery"] == "cursor_gap"
    assert recovery["requested_after"] == 10
    assert source.subscriptions == [20]


class _SnapshotFailingService(RecordingEnergyService):
    async def snapshot(self, *, principal: object) -> dict[str, object]:
        raise RuntimeError("telemetry backend unavailable")


def test_snapshot_failure_after_accept_sends_error_envelope_and_clean_close() -> None:
    source = FakeEventSource()
    with (
        TestClient(_app(_SnapshotFailingService(), source)) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "internal_error"
        assert error["request_id"]
        websocket.receive_json()
    assert caught.value.code == 1011
    assert source.subscriptions == []


def test_non_monotonic_source_event_closes_stream_instead_of_forwarding_it() -> None:
    source = FakeEventSource(
        [
            {"sequence": 21, "type": "observation.updated", "data": {}},
            {"sequence": 21, "type": "decision.updated", "data": {}},
        ]
    )
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        assert websocket.receive_json()["type"] == "snapshot"
        assert websocket.receive_json()["sequence"] == 21
        terminal = websocket.receive_json()

    assert terminal["type"] == "resync_required"
    assert terminal["reason"] == "non_monotonic_event"


def test_publisher_event_typed_resync_required_is_delivered_not_terminal() -> None:
    # Deferred P2 (implementation review 2026-08-22): the consumer treated ANY
    # frame typed ``resync_required`` as its own terminate marker, so one
    # publisher event using that vocabulary string closed every client stream.
    # The adapter's own control frames never carry a sequence; a published
    # event always carries the bus-assigned one, so the stream must deliver it
    # and keep flowing.
    source = FakeEventSource(
        [
            {"sequence": 21, "type": "resync_required", "payload": {"note": "publisher event"}},
            {"sequence": 22, "type": "observation.updated", "payload": {"index": 1}},
        ]
    )
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        assert websocket.receive_json()["type"] == "snapshot"
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "resync_required"
    assert first["sequence"] == 21
    assert second["type"] == "observation.updated"
    assert second["sequence"] == 22
    assert source.subscriptions == [20]


@pytest.mark.parametrize(
    "events, reason",
    [
        (
            [
                {"sequence": 21, "type": "observation.updated", "data": {}},
                {"sequence": 23, "type": "decision.updated", "data": {}},
            ],
            "sequence_gap",
        ),
        (
            [{"sequence": 19, "type": "observation.updated", "data": {}}],
            "non_monotonic_event",
        ),
    ],
)
def test_stream_never_silently_forwards_gaps_or_events_older_than_snapshot(
    events: list[dict[str, object]], reason: str
) -> None:
    source = FakeEventSource(events)
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        assert websocket.receive_json()["sequence"] == 20
        if events[0]["sequence"] == 21:
            assert websocket.receive_json()["sequence"] == 21
        terminal = websocket.receive_json()
    assert terminal["type"] == "resync_required"
    assert terminal["reason"] == reason


def test_backpressure_is_bounded_and_forces_resync() -> None:
    source = FakeEventSource(
        [
            {"sequence": sequence, "type": "observation.updated", "data": {}}
            for sequence in range(21, 121)
        ]
    )
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        assert websocket.receive_json()["type"] == "snapshot"
        messages = [websocket.receive_json() for _ in range(3)]

    assert any(
        message.get("type") == "resync_required" and message.get("reason") == "backpressure"
        for message in messages
    )
    assert len(messages) <= 3


def test_client_disconnect_cancels_and_closes_event_subscription() -> None:
    source = FakeEventSource([{"sequence": 21, "type": "observation.updated", "data": {}}])
    with (
        TestClient(_app(RecordingEnergyService(), source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        assert websocket.receive_json()["type"] == "snapshot"
        assert websocket.receive_json()["sequence"] == 21
    assert source.subscriptions == [20]
    assert source.closed_subscriptions == 1


class _UtcClock:
    """Deterministic clock for the real EventBus; ambient time is never read."""

    def wall_now(self) -> datetime:
        return datetime(2026, 8, 21, tzinfo=UTC)


class _RealBusSource:
    """The REAL EventBus behind the adapter's ``EventSource`` seam.

    ``burst_on_subscribe`` publishes that many events in one scheduling turn
    as soon as the adapter subscribes, while its producer is gated awaiting
    the next event: the only way a composed runtime produces a bus marker.
    """

    def __init__(self, bus: Any, *, burst_on_subscribe: int = 0) -> None:
        self._bus = bus
        self._burst = burst_on_subscribe
        self.subscriptions: list[int | None] = []
        self._pump: Any = None

    def subscribe(self, *, after_sequence: int | None) -> Any:
        self.subscriptions.append(after_sequence)
        if self._burst:

            async def pump() -> None:
                for index in range(self._burst):
                    await self._bus.publish(
                        {"type": "observation.updated", "payload": {"index": index}}
                    )

            # Keep the reference: the loop holds only weak references to tasks.
            self._pump = asyncio.create_task(pump())
        return self._bus.subscribe(after_sequence=after_sequence)


class _BusSequencedService(RecordingEnergyService):
    """Snapshot sequence tracks the real bus, optionally behind it."""

    def __init__(
        self,
        bus: Any,
        *,
        publish_first: int = 0,
        stale_sequence: int | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._publish_first = publish_first
        self._stale_sequence = stale_sequence

    async def snapshot(self, *, principal: object) -> dict[str, Any]:
        snapshot = await super().snapshot(principal=principal)
        for index in range(self._publish_first):
            await self._bus.publish({"type": "observation.updated", "payload": {"index": index}})
        live = self._bus.snapshot_sequence()
        if self._stale_sequence is not None:
            snapshot["snapshot_sequence"] = self._stale_sequence
        else:
            snapshot["snapshot_sequence"] = live
        return snapshot


def test_bus_slow_subscriber_marker_reaches_client_with_true_reason_and_snapshot() -> None:
    # The bus's drop-to-resync marker is a first-class discontinuity: the
    # adapter may map it onto its resync_required envelope, but it must carry
    # the marker's own reason and snapshot point. Relabeling it as
    # "non_monotonic_event" misdiagnoses an ordinary slow consumer (here a
    # producer burst the subscriber never got to read) as source corruption.
    events_module = load_contract_module("energypod.application.events")
    bus = events_module.EventBus(retention=64, queue_capacity=4, clock=_UtcClock())
    source = _RealBusSource(bus, burst_on_subscribe=5)  # one past the bus queue bound
    service = _BusSequencedService(bus)
    with (
        TestClient(_app(service, source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        snapshot = websocket.receive_json()
        terminal = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["sequence"] == 0
    assert terminal["type"] == "resync_required"
    assert terminal["reason"] == "slow_subscriber"
    assert terminal["snapshot_sequence"] == 5
    assert source.subscriptions == [0]


def test_bus_stale_cursor_marker_reaches_client_with_true_reason_and_snapshot() -> None:
    # Same seam, retention path: a cursor older than the retained window must
    # arrive as the bus's own reason and snapshot point, not as a fabricated
    # non_monotonic_event, so the operator and the client see the truth.
    events_module = load_contract_module("energypod.application.events")
    bus = events_module.EventBus(retention=8, queue_capacity=64, clock=_UtcClock())
    source = _RealBusSource(bus)
    # 30 events publish inside snapshot(); the snapshot then reports a lagging
    # sequence 0 cursor, which predates the retained window 23..30.
    service = _BusSequencedService(bus, publish_first=30, stale_sequence=0)
    with (
        TestClient(_app(service, source)) as client,
        client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket,
    ):
        snapshot = websocket.receive_json()
        terminal = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["sequence"] == 0
    assert terminal["type"] == "resync_required"
    assert terminal["reason"] == "retention_window_exceeded"
    assert terminal["snapshot_sequence"] == 30
    assert source.subscriptions == [0]


def test_subprotocol_ticket_handshake_streams_without_authorization_header() -> None:
    # Browsers cannot set an Authorization header on a WebSocket: the handshake
    # offers Sec-WebSocket-Protocol: energypod-events, <ticket> instead, the
    # server consumes the ticket, negotiates the subprotocol, and streams.
    source = FakeEventSource(
        [{"sequence": 21, "type": "observation.updated", "data": {"unit_id": "pod-a"}}]
    )
    clock = MutableMonotonicClock(2000.0)
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                event_ticket_ttl_s=15.0,
                event_ticket_clock=clock,
            )
        ) as client,
    ):
        ticket = _event_ticket(client)
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["energypod-events", ticket]
        ) as websocket:
            assert websocket.accepted_subprotocol == "energypod-events"
            assert websocket.receive_json()["type"] == "snapshot"
            assert websocket.receive_json()["sequence"] == 21
    assert source.subscriptions == [20]


def test_event_ticket_is_consumed_at_the_handshake() -> None:
    source = FakeEventSource()
    clock = MutableMonotonicClock()
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                event_ticket_ttl_s=15.0,
                event_ticket_clock=clock,
            )
        ) as client,
    ):
        ticket = _event_ticket(client)
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["energypod-events", ticket]
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/api/v1/events", subprotocols=["energypod-events", ticket]),
        ):
            raise AssertionError("a consumed ticket unexpectedly re-authenticated")
        assert refused.value.code == 4401


def test_expired_event_ticket_refuses_the_handshake() -> None:
    source = FakeEventSource()
    clock = MutableMonotonicClock(100.0)
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                event_ticket_ttl_s=5.0,
                event_ticket_clock=clock,
            )
        ) as client,
    ):
        ticket = _event_ticket(client)
        clock.now = 105.0  # exactly the TTL later: no longer valid
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/api/v1/events", subprotocols=["energypod-events", ticket]),
        ):
            raise AssertionError("an expired ticket unexpectedly authenticated")
        assert refused.value.code == 4401
        assert ticket not in str(refused.value)
    assert source.subscriptions == []


def test_handshake_without_a_ticket_still_accepts_the_authorization_header() -> None:
    source = FakeEventSource()
    with TestClient(_app(RecordingEnergyService(), source)) as client:
        with client.websocket_connect(
            "/api/v1/events", headers={"Authorization": "Bearer viewer-token"}
        ) as websocket:
            assert websocket.accepted_subprotocol is None
            assert websocket.receive_json()["type"] == "snapshot"
        # A subprotocol offer without a ticket still rides the header path and
        # negotiates the offered energypod-events subprotocol.
        with client.websocket_connect(
            "/api/v1/events",
            headers={"Authorization": "Bearer viewer-token"},
            subprotocols=["energypod-events"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "energypod-events"
            assert websocket.receive_json()["type"] == "snapshot"
    assert source.subscriptions == [20, 20]


@pytest.mark.parametrize(
    "offered",
    [
        ["energypod-events", "not-a-real-ticket"],
        ["energypod-events", "candidate-one", "candidate-two"],
    ],
)
def test_unknown_or_malformed_tickets_refuse_cleanly(offered: list[str]) -> None:
    source = FakeEventSource()
    with TestClient(_app(RecordingEnergyService(), source)) as client:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/api/v1/events", subprotocols=offered),
        ):
            raise AssertionError("an invalid ticket offer unexpectedly authenticated")
        assert refused.value.code in {1008, 4401}
        assert not str(refused.value)
    assert source.subscriptions == []


def test_ticket_channel_still_prohibits_query_string_credentials() -> None:
    source = FakeEventSource()
    clock = MutableMonotonicClock()
    with (
        TestClient(
            _app(
                RecordingEnergyService(),
                source,
                event_ticket_ttl_s=15.0,
                event_ticket_clock=clock,
            )
        ) as client,
    ):
        ticket = _event_ticket(client)
        with (
            pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)) as refused,
            client.websocket_connect(
                f"/api/v1/events?access_token={ticket}",
                subprotocols=["energypod-events", ticket],
            ),
        ):
            raise AssertionError("a query-string credential unexpectedly authenticated")
        assert getattr(refused.value, "code", None) in {1008, 4401}
        assert ticket not in str(refused.value)
        # The refused handshake never consumed the ticket nor leaked it: the
        # same ticket still authenticates a clean subprotocol-only handshake.
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["energypod-events", ticket]
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
    assert source.subscriptions == [20]
