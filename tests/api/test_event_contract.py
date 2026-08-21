"""Red-phase WebSocket contracts for ordered, recoverable status delivery."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from .conftest import (
    FakeAuthenticator,
    FakeEventSource,
    RecordingEnergyService,
    load_contract_module,
)


def _app(
    service: RecordingEnergyService,
    source: FakeEventSource,
    *,
    trusted_websocket_origins: frozenset[str] | None = None,
) -> object:
    module = load_contract_module("energypod.api.rest")
    assert hasattr(module, "create_api_app")
    return module.create_api_app(
        service=service,
        authenticator=FakeAuthenticator(),
        event_source=source,
        auth_required=True,
        websocket_queue_capacity=2,
        trusted_websocket_origins=trusted_websocket_origins,
    )


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
