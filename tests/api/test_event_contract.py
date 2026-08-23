"""Red-phase WebSocket contracts for ordered, recoverable status delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from tests.unit.test_event_bus import close_subscription, drain
from tests.unit.test_excess_charge import (
    NOW,
    FakeClock,
    FakeIntents,
    FakeObservations,
    FakeSubmit,
    make_fleet,
    make_policy,
    make_settings,
)

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


# --- excess_adviser.state_changed (DESIGN_EXCESS_ACTIVATION §2) -----------------
#
# One new bus event, same vocabulary as the §1 projection: published ONLY
# when the semantic state tuple changes (watt figures ride but never
# trigger), with a 30 s heartbeat republish while enabled and none while
# disabled.  The REAL EventBus and the REAL adviser/controller drive these
# contracts -- the adapter-family fakes above cannot express them.


def _excess_modules() -> Any:
    try:
        import energypod.application.events as events
        import energypod.application.excess_charge as excess
    except ImportError as error:  # pragma: no cover - contract modules exist
        raise AssertionError(f"excess event contract dependency missing: {error}") from error
    return SimpleNamespace(events=events, excess=excess)


@pytest.fixture
def api_domain() -> Any:
    import energypod.domain as domain

    return domain


def _fresh_fleet(api_domain: Any, grids: Any, rig: Any) -> None:
    """Re-script the fleet with capture times fresh at the CURRENT clock."""
    rig.observations.latest = make_fleet(
        api_domain,
        grids,
        **{f"{unit}__captured_at_mono": rig.clock.now for unit in grids},
    )


def _adviser_rig(api_domain: Any, grids: Any) -> Any:
    """A real adviser + controller over the real bus (deterministic clock)."""
    modules = _excess_modules()
    clock = FakeClock()
    bus = modules.events.EventBus(retention=64, queue_capacity=64, clock=clock)
    controller = modules.excess.ExcessAdviserController(
        charge_cap_w=2_000,
        clock=clock,
        acknowledged_economics=True,
        config_enabled=True,
        bus=bus,
    )
    observations = FakeObservations(latest=make_fleet(api_domain, grids))
    adviser = modules.excess.ExcessChargeAdviser(
        settings=make_settings(modules.excess),
        policy=make_policy(api_domain),
        clock=clock,
        observations=observations,
        intents=FakeIntents(),
        submit=FakeSubmit(),
        participation=controller.participation_verdict,
    )
    controller.bind_adviser(adviser)
    return SimpleNamespace(
        controller=controller,
        adviser=adviser,
        observations=observations,
        clock=clock,
        bus=bus,
    )


async def test_first_tick_publishes_the_contract_payload(api_domain: Any) -> None:
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    events = await drain(subscription, 4)

    assert [event["type"] for event in events] == ["excess_adviser.state_changed"]
    payload = events[0]["payload"]
    assert set(payload) == {
        "enabled",
        "enabled_origin",
        "acknowledged_economics",
        "active",
        "hysteresis_state",
        "target_unit_id",
        "commanded_charge_w",
        "eligible_export_charge_w",
        "fleet_export_w",
        "export_evidence",
        "reason_codes",
        "held_intent_id",
        "heartbeat",
    }
    assert payload["enabled"] is True
    assert payload["enabled_origin"] == "config"
    assert payload["acknowledged_economics"] is True
    assert payload["active"] is True
    assert payload["hysteresis_state"] == "holding"
    assert payload["target_unit_id"] == "mid"
    assert payload["commanded_charge_w"] == 1_100
    assert payload["eligible_export_charge_w"] == 1_100
    assert payload["fleet_export_w"] == 1_200
    assert payload["export_evidence"] == "good"
    assert payload["reason_codes"] == ["export_headroom_available"]
    assert payload["held_intent_id"] == "excess-1"
    assert payload["heartbeat"] is False
    await close_subscription(subscription)


async def test_watt_wander_rides_but_never_triggers_a_publication(api_domain: Any) -> None:
    """The pinned throttle: while holding, the commanded watts re-price with
    export every tick (~1.5 s), and publishing that would put one event per
    cycle on the bus for figure wander the console already gets elsewhere."""
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    first = await drain(subscription, 2)
    assert len(first) == 1

    for cycle, export in enumerate((1_600.0, 1_700.0, 1_800.0), start=1):
        rig.clock.now = NOW + 1.5 * cycle
        _fresh_fleet(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": export}, rig)
        await rig.controller.observe_tick(await rig.adviser.tick())

    assert rig.controller.state().commanded_charge_w == 1_400
    quiet = await drain(subscription, 2)
    assert quiet == [], "watt wander must never publish"
    await close_subscription(subscription)


async def test_a_semantic_change_publishes_and_carries_the_new_figures(api_domain: Any) -> None:
    """Export collapse to below the exit threshold is a semantic change
    (holding -> exiting, the reason vocabulary changes): exactly one new
    event, carrying the collapsed figures."""
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    assert len(await drain(subscription, 2)) == 1

    rig.observations.latest = make_fleet(api_domain, {"lhs": 0.0, "mid": 0.0, "rhs": 650.0})
    rig.clock.now = NOW + 1.5
    await rig.controller.observe_tick(await rig.adviser.tick())
    events = await drain(subscription, 2)

    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["active"] is False
    assert payload["hysteresis_state"] == "exiting"
    assert payload["reason_codes"] == ["below_exit_hysteresis"]
    assert payload["commanded_charge_w"] == 0
    assert payload["held_intent_id"] is None
    await close_subscription(subscription)


async def test_the_heartbeat_republishes_every_30_seconds_while_enabled(api_domain: Any) -> None:
    """Bounded (<= 2/min) liveness proof: a repeat publication that does NOT
    change the semantic tuple, every 30 s while enabled -- and never sooner."""
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    assert len(await drain(subscription, 2)) == 1

    rig.clock.now = NOW + 29.0
    _fresh_fleet(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0}, rig)
    await rig.controller.observe_tick(await rig.adviser.tick())
    assert (await drain(subscription, 2)) == [], "29 s is not the heartbeat cadence"

    rig.clock.now = NOW + 30.0
    _fresh_fleet(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0}, rig)
    await rig.controller.observe_tick(await rig.adviser.tick())
    events = await drain(subscription, 2)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["heartbeat"] is True
    assert payload["active"] is True, "the heartbeat carries the full live payload"

    rig.clock.now = NOW + 45.0
    _fresh_fleet(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0}, rig)
    await rig.controller.observe_tick(await rig.adviser.tick())
    assert (await drain(subscription, 2)) == [], "one heartbeat per 30 s window, no more"
    await close_subscription(subscription)


async def test_disabled_publishes_its_state_change_and_then_nothing(api_domain: Any) -> None:
    """The state_changed to disabled is the LAST event: while disabled there
    is no heartbeat, however long the loop runs."""
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    assert len(await drain(subscription, 2)) == 1

    rig.controller.set_participation(enabled=False)
    rig.clock.now = NOW + 1.5
    await rig.controller.observe_tick(await rig.adviser.tick())
    events = await drain(subscription, 2)
    assert [event["payload"]["enabled"] for event in events] == [False]
    assert events[0]["payload"]["reason_codes"] == ["disabled_by_runtime"]
    assert events[0]["payload"]["heartbeat"] is False

    for cycle in range(1, 5):
        rig.clock.now = NOW + 30.0 * cycle
        _fresh_fleet(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0}, rig)
        await rig.controller.observe_tick(await rig.adviser.tick())
    assert (await drain(subscription, 4)) == [], "no heartbeat while disabled"
    await close_subscription(subscription)


async def test_missing_evidence_replaces_the_headroom_code_in_the_vocabulary(
    api_domain: Any,
) -> None:
    """The evidence words are projection codes: a collapsed rollup prepends
    its own code and drops ``no_export_headroom``, whose definition ('evidence
    good but export at or below the margin') the collapsed state cannot
    satisfy -- one vocabulary, never two codes for the same fact."""
    rig = _adviser_rig(api_domain, {"lhs": -300.0, "mid": 0.0, "rhs": 1_500.0})
    subscription = rig.bus.subscribe(after_sequence=None)

    await rig.controller.observe_tick(await rig.adviser.tick())
    assert len(await drain(subscription, 2)) == 1

    rig.clock.now = NOW + 1.5
    _fresh_fleet(api_domain, {"lhs": None, "mid": 0.0, "rhs": 1_500.0}, rig)  # one phase unserved
    await rig.controller.observe_tick(await rig.adviser.tick())
    events = await drain(subscription, 2)

    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["export_evidence"] == "missing"
    assert payload["fleet_export_w"] is None
    assert payload["reason_codes"] == ["export_evidence_missing"]
    assert payload["hysteresis_state"] == "exiting"
    await close_subscription(subscription)


# --- schedule.replaced / schedule_window events (DESIGN_SCHEDULES §5) -----------
#
# The REAL EventBus carries the publications: the facade's schedule.replaced
# (one per publish) and the runner's window transitions (opened on the first
# submit for a key, closing on the removal tick — transitions only, never a
# per-tick heartbeat).


def _generation_module() -> Any:
    import energypod.application.generation as generation

    return generation


def _schedule_modules() -> Any:
    try:
        import energypod.application.events as events
        import energypod.application.scheduling as scheduling
        import energypod.application.service as service
    except ImportError as error:  # pragma: no cover - contract modules exist
        raise AssertionError(f"schedule event contract dependency missing: {error}") from error
    return SimpleNamespace(events=events, scheduling=scheduling, service=service)


def _schedule_surface(modules: Any, *, night: bool = False) -> Any:
    windows = (("00:00", "06:00"), ("06:00", "20:00")) if night else (("06:00", "20:00"),)
    return modules.scheduling.ScheduleSurfaceControl(
        policy=modules.scheduling.SchedulePolicy(
            allowed_windows_local=tuple(
                (modules.scheduling.parse_hhmm(a), modules.scheduling.parse_hhmm(b))
                for a, b in windows
            ),
            intent_ttl_s=10.0,
            timezone="Australia/Brisbane",
        ),
        store=_PlanStore(),
        acknowledged_night_windows=night,
    )


class _PlanStore:
    def __init__(self) -> None:
        self.plan: Any = None

    def get(self) -> Any:
        return self.plan

    def replace(self, *, expected_version: int, replacement: Any) -> None:
        actual = 0 if self.plan is None else self.plan.version
        assert actual == expected_version and replacement.version == expected_version + 1
        self.plan = replacement


def _schedule_facade_rig(modules: Any, *, night: bool = False) -> Any:
    from tests.unit.test_service_facade import (
        OPERATOR,
        FakeActorHandle,
        FakeAuditRepository,
        FakeAuthorizationRepository,
        FakeClock,
        FakeIntentRepository,
        FakeObservationRepository,
        RecordingCoordinator,
    )

    clock = FakeClock()
    history: list[str] = []
    handles = {
        unit: FakeActorHandle(
            unit_id=unit,
            lifecycle=SimpleNamespace(value="disarmed"),
            armed_lifecycle=SimpleNamespace(value="armed_idle"),
            history=history,
        )
        for unit in ("pod-a", "pod-b")
    }
    surface = _schedule_surface(modules, night=night)
    bus = modules.events.EventBus(retention=64, queue_capacity=64, clock=clock)
    facade = modules.service.EnergyServiceFacade(
        site_id="home",
        clock=clock,
        intents=FakeIntentRepository(),
        observations=FakeObservationRepository(),
        authorizations=FakeAuthorizationRepository({}, now_mono=clock.monotonic()),
        audit=FakeAuditRepository(),
        events=bus,
        coordinator=RecordingCoordinator(
            SimpleNamespace(
                AuthorityGenerationCoordinator=_generation_module().AuthorityGenerationCoordinator
            ),
            history,
        ),
        actors=handles,
        schedules=surface,
    )
    return SimpleNamespace(facade=facade, surface=surface, bus=bus, clock=clock, operator=OPERATOR)


def _publish_body() -> dict[str, Any]:
    return {
        "principal": None,
        "expected_version": None,
        "timezone": "Australia/Brisbane",
        "entries": [
            {
                "entry_id": "day-charge",
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start_local": "09:00",
                "end_local": "17:00",
                "action": "charge",
                "watts": 1200,
                "unit_ids": ["pod-a"],
                "effective_from": "2020-01-01",
                "effective_until": "2035-12-31",
                "priority": 0,
                "enabled": True,
            }
        ],
        "idempotency_key": "publish-key-1",
        "request_id": "request-p1",
    }


async def test_schedule_replaced_publishes_the_contract_payload() -> None:
    modules = _schedule_modules()
    rig = _schedule_facade_rig(modules)
    subscription = rig.bus.subscribe(after_sequence=None)
    body = _publish_body()
    body["principal"] = rig.operator

    await rig.facade.replace_schedule(**body)
    events = await drain(subscription, 1)

    assert [event["type"] for event in events] == ["schedule.replaced"]
    payload = events[0]["payload"]
    assert payload["principal"] == rig.operator.subject
    assert payload["version"] == 1
    assert payload["diff"] == {
        "added": ["day-charge"],
        "removed": [],
        "changed": [],
        "timezone_changed": False,
    }
    await close_subscription(subscription)


async def test_window_events_are_transitions_never_heartbeats() -> None:
    """opened publishes on the first submit for a window key only; renewal
    ticks publish nothing; the removal tick publishes closing exactly once."""
    from datetime import date, time

    from energypod.domain.intents import Direction
    from energypod.domain.schedule import ScheduleEntry, SchedulePlan, Weekday

    modules = _schedule_modules()

    class _Clock:
        def __init__(self) -> None:
            self.now = 100.0
            self.wall = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)

        def monotonic(self) -> float:
            return self.now

        def wall_now(self) -> datetime:
            return self.wall

    clock = _Clock()
    bus = modules.events.EventBus(retention=64, queue_capacity=64, clock=clock)
    entry = ScheduleEntry(
        entry_id="day-charge",
        days=frozenset({Weekday.FRIDAY}),
        start_local=time(9, 0),
        end_local=time(17, 0),
        action=Direction.CHARGE,
        watts=1500,
        unit_ids=frozenset({"pod-a", "pod-b"}),
        effective_from=date(2020, 1, 1),
        effective_until=date(2035, 12, 31),
        priority=0,
        enabled=True,
        watts_by_unit={"pod-a": 700, "pod-b": 800},
    )
    store = _PlanStore()
    store.plan = SchedulePlan(version=1, timezone="Australia/Brisbane", entries=(entry,))

    async def get_plan() -> Any:
        return store.get()

    submissions: list[dict[str, Any]] = []

    async def submit(**kwargs: Any) -> dict[str, Any]:
        submissions.append(kwargs)
        return {"intent_id": f"schedule-{len(submissions)}"}

    intents = FakeIntents()
    runner = modules.scheduling.ScheduleRunner(
        store=SimpleNamespace(get_plan=get_plan),
        evaluator=modules.scheduling.ScheduleEvaluator(intent_ttl_s=10.0),
        clock=clock,
        submit=submit,
        intents=intents,
        bus=bus,
    )
    subscription = bus.subscribe(after_sequence=None)

    await runner.tick()
    opened = await drain(subscription, 1)
    await runner.tick()
    await runner.tick()
    clock.wall = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)  # 17:00 Friday: the window ends
    await runner.tick()
    closing = await drain(subscription, 1)

    assert [event["type"] for event in opened] == ["schedule_window.opened"]
    payload = opened[0]["payload"]
    assert payload["entry_id"] == "day-charge"
    assert payload["version"] == 1
    assert payload["action"] == "charge"
    assert payload["watts"] == 1500
    assert payload["watts_by_unit"] == {"pod-a": 700, "pod-b": 800}
    assert payload["unit_ids"] == ["pod-a", "pod-b"]
    assert payload["ends_at"].startswith("2026-08-21T17:00")
    assert len(submissions) == 3, "open + two renewals"
    assert [event["type"] for event in closing] == ["schedule_window.closing"]
    assert closing[0]["payload"]["reason"] == "window_ended"
    await close_subscription(subscription)


# --- DESIGN_ENERGY_SCORECARD section 6: energy.day_rolled (E4) ------------------


def _energy_observation(unit_id: str, *, wall: datetime, mono: float, sequence: int) -> Any:
    from energypod.domain.observations import DataQuality, Observation, UnitLifecycle

    quality = {name: DataQuality.GOOD for name in Observation.QUALITY_FIELDS} | {
        name: DataQuality.GOOD for name in Observation.ADVISORY_QUALITY_FIELDS
    }
    return Observation(
        unit_id=unit_id,
        wall_timestamp=wall,
        captured_at_mono=mono,
        sequence=sequence,
        lifecycle=UnitLifecycle.ARMED_IDLE,
        protocol_profile="iot",
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
        soh_pct=100.0,
        battery_watts=-1500.0,
        pack_voltage_v=200.0,
        pack_current_a=-7.5,
        dynamic_charge_limit_w=3000.0,
        dynamic_discharge_limit_w=3000.0,
        expected_cell_count=2,
        cell_voltages_v=(3.3, 3.3),
        expected_temperature_count=1,
        temperatures_c=(24.0,),
        grid_power_w=-1000.0,
        load_power_w=None,
        energy_grid_a_kwh=100.0,
        energy_grid_b_kwh=50.0,
        energy_load_kwh=200.0,
        energy_pv_kwh=None,
        energy_charge_kwh=100.0,
        energy_discharge_kwh=50.0,
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality=quality,
    )


async def test_energy_day_rolled_publishes_the_completed_record_once() -> None:
    """The rollover is a TRANSITION: exactly one publication per completed
    day, payload the record itself; renewal ticks within the new day publish
    nothing (the no-heartbeat rule)."""
    energy = load_contract_module("energypod.application.energy")
    events = load_contract_module("energypod.application.events")
    from zoneinfo import ZoneInfo

    brisbane = ZoneInfo("Australia/Brisbane")
    day = datetime(2026, 8, 26, 20, 0, tzinfo=brisbane)  # before midnight
    midnight = datetime(2026, 8, 27, 0, 0, 30, tzinfo=brisbane)

    class _Clock:
        def __init__(self) -> None:
            self.wall = day.astimezone(UTC)
            self.now = 1000.0

        def monotonic(self) -> float:
            return self.now

        def wall_now(self) -> datetime:
            return self.wall

    clock = _Clock()
    bus = events.EventBus(retention=64, queue_capacity=64, clock=clock)

    class _Ledger:
        def __init__(self) -> None:
            self.days: dict[Any, Any] = {}
            self._baseline: dict[str, Any] = {}

        def record_day(self, record: Any) -> None:
            self.days[record.date] = record

        def get_day(self, day: Any) -> Any | None:
            return self.days.get(day)

        def latest_days(self, limit: int) -> tuple[Any, ...]:
            return tuple(self.days[day] for day in sorted(self.days, reverse=True)[:limit])

        def load_baseline(self) -> dict[str, Any]:
            return dict(self._baseline)

        def save_baseline(self, baselines: dict[str, Any]) -> None:
            self._baseline = dict(baselines)

    accountant = energy.EnergyAccountant(
        unit_ids=("mid",),
        timezone="Australia/Brisbane",
        settings=energy.EnergyAccountingSettings(integration_max_gap_s=3600.0),
        clock=clock,
        ledger=_Ledger(),
        bus=bus,
    )
    subscription = bus.subscribe(after_sequence=None)

    # Two ticks in the day, then the first observation of the next day.
    accountant.observe(
        _energy_observation("mid", wall=day.astimezone(UTC), mono=1000.0, sequence=1),
        adviser_active_targets=frozenset({"mid"}),
        now_mono=1000.0,
    )
    await accountant.flush()
    accountant.observe(
        _energy_observation(
            "mid", wall=day.astimezone(UTC) + timedelta(seconds=60), mono=1060.0, sequence=2
        ),
        adviser_active_targets=frozenset({"mid"}),
        now_mono=1060.0,
    )
    await accountant.flush()
    accountant.observe(
        _energy_observation("mid", wall=midnight.astimezone(UTC), mono=1120.0, sequence=3),
        adviser_active_targets=frozenset(),
        now_mono=1120.0,
    )
    await accountant.flush()
    rolled = await drain(subscription, 1)
    # Two more ticks inside the new day: no further publication.
    accountant.observe(
        _energy_observation(
            "mid",
            wall=midnight.astimezone(UTC) + timedelta(seconds=60),
            mono=1180.0,
            sequence=4,
        ),
        adviser_active_targets=frozenset(),
        now_mono=1180.0,
    )
    await accountant.flush()
    quiet = await drain(subscription, 0)

    assert [event["type"] for event in rolled] == ["energy.day_rolled"]
    payload = rolled[0]["payload"]
    assert payload["date"] == "2026-08-26"
    assert payload["kind"] in {"complete", "partial"}
    assert payload["timezone"] == "Australia/Brisbane"
    assert payload["fleet"]["charged_from_surplus_kwh"] == pytest.approx(
        1500.0 * 60.0 / 3_600_000.0
    )
    assert quiet == [], "renewal ticks within a day publish nothing"
    await close_subscription(subscription)


# --- foreign_objective.observed (API_CONTRACTS "Night-writer detector") ----------
#
# The alert tier's ONE bus event, driven over the REAL EventBus by the REAL
# monitor: one publication per foreign episode (or per reason change inside
# one), never per sample, and nothing at all on the quiet tiers.


def _objective_modules() -> Any:
    try:
        import energypod.application.events as events
        import energypod.application.foreign_objective as foreign_objective
    except ImportError as error:  # pragma: no cover - contract modules exist
        raise AssertionError(f"objective event contract dependency missing: {error}") from error
    return SimpleNamespace(events=events, foreign_objective=foreign_objective)


def _objective_rig(units: tuple[str, ...] = ("mid",)) -> Any:
    modules = _objective_modules()
    clock = FakeClock()
    clock.now = 1_000.0
    bus = modules.events.EventBus(retention=64, queue_capacity=64, clock=clock)

    class _NoopAudit:
        async def append(self, event: Any) -> None:
            return None

    monitor = modules.foreign_objective.ForeignObjectiveMonitor(
        unit_ids=frozenset(units),
        settings=modules.foreign_objective.ForeignObjectiveSettings(
            sample_interval_s=1.0,
        ),
        clock=clock,
        audit=_NoopAudit(),
        bus=bus,
        process_instance_id="objective-event-test",
        process_origin_mono=clock.now,
        configuration_version=1,
    )
    return SimpleNamespace(monitor=monitor, bus=bus, clock=clock)


async def test_a_foreign_episode_publishes_exactly_one_alert_event() -> None:
    rig = _objective_rig()
    subscription = rig.bus.subscribe(after_sequence=None)

    for now in (1_000.0, 1_040.0, 1_080.0, 1_120.0):
        rig.clock.now = now
        await rig.monitor.observe_cycle(
            "mid",
            lifecycle="disarmed",
            claimed=False,
            authorized_watts=0,
            observation=SimpleNamespace(
                served_active_objective_w=-3000,
                served_reactive_objective_var=0,
                objective_captured_at_mono=now,
                grid_power_w=-1500.0,
                run_mode_w=None,
                ctrl_mode_w=1,
                work_mode_w=6,
                debug_mode_w=0,
                sequence=int(now),
                wall_timestamp=datetime(2026, 8, 26, 22, 30, tzinfo=UTC),
            ),
            now_mono=now,
        )

    events = await drain(subscription, 1)
    assert [event["type"] for event in events] == ["foreign_objective.observed"]
    payload = events[0]["payload"]
    assert set(payload) == {
        "unit_id",
        "observed_at",
        "active_w",
        "reactive_var",
        "classification",
        "reason",
        "lifecycle",
        "claimed",
        "run_mode_w",
        "ctrl_mode_w",
        "work_mode_w",
        "debug_mode_w",
        "grid_power_w",
        "pv_evidence",
    }
    assert payload["unit_id"] == "mid"
    assert payload["classification"] == "foreign_objective_observed"
    assert payload["reason"] == "outside_autonomy_band"
    assert payload["pv_evidence"] is False


async def test_quiet_tiers_publish_nothing_at_all() -> None:
    """In-band autonomy and handback-grace samples are EVIDENCE, not alarms:
    the quiet tiers never reach the bus."""
    rig = _objective_rig()
    subscription = rig.bus.subscribe(after_sequence=None)

    # Our own command, then its lapse inside the grace window.
    rig.clock.now = 1_000.0
    await rig.monitor.observe_cycle(
        "mid",
        lifecycle="active",
        claimed=True,
        authorized_watts=1500,
        observation=None,
        now_mono=1_000.0,
    )
    rig.clock.now = 1_002.0
    await rig.monitor.observe_cycle(
        "mid",
        lifecycle="armed_idle",
        claimed=False,
        authorized_watts=0,
        observation=SimpleNamespace(
            served_active_objective_w=-1500,
            served_reactive_objective_var=0,
            objective_captured_at_mono=1_002.0,
            grid_power_w=None,
            run_mode_w=1,
            ctrl_mode_w=None,
            work_mode_w=None,
            debug_mode_w=None,
            sequence=2,
            wall_timestamp=datetime(2026, 8, 26, 22, 30, tzinfo=UTC),
        ),
        now_mono=1_002.0,
    )
    # Then the pod's own quiet self-charge, twice.
    for now in (1_040.0, 1_080.0):
        rig.clock.now = now
        await rig.monitor.observe_cycle(
            "mid",
            lifecycle="disarmed",
            claimed=False,
            authorized_watts=0,
            observation=SimpleNamespace(
                served_active_objective_w=-620,
                served_reactive_objective_var=0,
                objective_captured_at_mono=now,
                grid_power_w=900.0,
                run_mode_w=0,
                ctrl_mode_w=1,
                work_mode_w=6,
                debug_mode_w=0,
                sequence=int(now),
                wall_timestamp=datetime(2026, 8, 26, 22, 31, tzinfo=UTC),
            ),
            now_mono=now,
        )

    # The drain helper settles without hanging on an idle stream: an empty
    # list IS the "nothing was published" verdict.
    assert await drain(subscription, 1) == []
