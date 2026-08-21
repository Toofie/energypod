"""Adversarial API tests for raw headers, cancellation, eviction, and stop races."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from energypod.api.idempotency import (
    IdempotencyConflictError,
    IdempotencyCoordinator,
    StoredResult,
)

from .conftest import FakeAuthenticator, FakeEventSource, RecordingEnergyService

API = "/api/v1"


def _app(
    service: RecordingEnergyService,
    *,
    capacity: int = 4096,
) -> Any:
    from energypod.api.rest import create_api_app

    return create_api_app(
        service=service,
        authenticator=FakeAuthenticator(),
        event_source=FakeEventSource(),
        auth_required=True,
        idempotency_capacity=capacity,
    )


async def _raw_http(
    app: Any,
    *,
    path: str,
    headers: list[tuple[bytes, bytes]],
    payload: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call ASGI directly so duplicate/control-byte headers cannot be normalized away."""
    body = b"" if payload is None else json.dumps(payload).encode()
    sent_body = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST" if payload is not None else "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 40000),
            "server": ("manager.test", 443),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body)


async def _raw_websocket_close(
    app: Any,
    *,
    headers: list[tuple[bytes, bytes]],
) -> int:
    received_connect = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received_connect
        if not received_connect:
            received_connect = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "wss",
            "path": f"{API}/events",
            "raw_path": f"{API}/events".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 40000),
            "server": ("manager.test", 443),
            "subprotocols": [],
            "state": {},
        },
        receive,
        send,
    )
    close = next(message for message in messages if message["type"] == "websocket.close")
    assert all(message["type"] != "websocket.accept" for message in messages)
    return close["code"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [(b"authorization", b"Bearer viewer-token"), (b"authorization", b"Bearer viewer-token")],
        [(b"authorization", b"Bearer viewer-token\r\nX-Injected: yes")],
        [(b"authorization", b"Bearer viewer-token\x00")],
    ],
)
async def test_raw_duplicate_or_control_character_authorization_is_rejected(
    headers: list[tuple[bytes, bytes]],
) -> None:
    service = RecordingEnergyService()
    status, body = await _raw_http(
        _app(service),
        path=f"{API}/snapshot",
        headers=[*headers, (b"host", b"manager.test")],
    )
    assert status in {400, 401}
    assert body["code"] in {"malformed_header", "authentication_required"}
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "security_headers",
    [
        [(b"origin", b"https://manager.test"), (b"origin", b"https://manager.test")],
        [(b"host", b"manager.test"), (b"host", b"manager.test")],
        [(b"origin", b"https://manager.test\r\nHost: attacker.test")],
        [(b"host", b"manager.test\x00")],
    ],
)
async def test_raw_duplicate_or_control_character_websocket_origin_and_host_are_rejected(
    security_headers: list[tuple[bytes, bytes]],
) -> None:
    baseline = [(b"authorization", b"Bearer viewer-token")]
    if not any(name == b"host" for name, _ in security_headers):
        baseline.append((b"host", b"manager.test"))
    if not any(name == b"origin" for name, _ in security_headers):
        baseline.append((b"origin", b"https://manager.test"))
    code = await _raw_websocket_close(
        _app(RecordingEnergyService()), headers=baseline + security_headers
    )
    assert code in {4401, 1008}


@pytest.mark.asyncio
async def test_cancelled_follower_does_not_cancel_leader_or_other_followers() -> None:
    coordinator = IdempotencyCoordinator(max_completed_entries=2)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> StoredResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return StoredResult(202, {"intent_id": "intent-1"})

    execute = lambda: coordinator.execute(  # noqa: E731
        principal_subject="person:operator",
        key="same",
        payload={"watts": 500},
        operation=operation,
    )
    leader = asyncio.create_task(execute())
    await entered.wait()
    cancelled_follower = asyncio.create_task(execute())
    surviving_follower = asyncio.create_task(execute())
    await asyncio.sleep(0)
    cancelled_follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_follower
    assert not leader.done()
    assert not surviving_follower.done()
    release.set()
    assert await leader == await surviving_follower == StoredResult(202, {"intent_id": "intent-1"})
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_leader_unblocks_followers_and_retry_is_deterministic() -> None:
    coordinator = IdempotencyCoordinator(max_completed_entries=2)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> StoredResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return StoredResult(202, {"attempt": calls})

    async def execute() -> StoredResult:
        return await coordinator.execute(
            principal_subject="person:operator",
            key="cancelled-leader",
            payload={"watts": 500},
            operation=operation,
        )

    leader = asyncio.create_task(execute())
    await entered.wait()
    follower = asyncio.create_task(execute())
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(follower, timeout=1)

    release.set()
    retry = await asyncio.wait_for(execute(), timeout=1)
    replay = await asyncio.wait_for(execute(), timeout=1)
    assert retry == replay == StoredResult(202, {"attempt": 2})
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_eviction_is_bounded_principal_scoped_and_preserves_inflight_work() -> None:
    coordinator = IdempotencyCoordinator(max_completed_entries=1)
    blocked = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def run(subject: str, key: str, marker: str, wait: bool = False) -> StoredResult:
        async def operation() -> StoredResult:
            calls.append(marker)
            if wait:
                blocked.set()
                await release.wait()
            return StoredResult(202, {"marker": marker})

        return await coordinator.execute(
            principal_subject=subject,
            key=key,
            payload={"marker": marker},
            operation=operation,
        )

    inflight = asyncio.create_task(run("person:a", "live", "inflight", wait=True))
    await blocked.wait()
    assert await run("person:a", "old", "old") == StoredResult(202, {"marker": "old"})
    assert await run("person:b", "old", "other-principal") == StoredResult(
        202, {"marker": "other-principal"}
    )

    follower = asyncio.create_task(run("person:a", "live", "inflight", wait=True))
    await asyncio.sleep(0)
    assert not follower.done()
    release.set()
    assert await inflight == await follower == StoredResult(202, {"marker": "inflight"})
    assert calls.count("inflight") == 1

    # Capacity is one completed result: the oldest completed entry was evicted,
    # so retrying it executes once more. Another principal never inherited it.
    assert await run("person:a", "old", "old") == StoredResult(202, {"marker": "old"})
    assert calls == ["inflight", "old", "other-principal", "old"]


class _BlockingStopService(RecordingEnergyService):
    def __init__(self) -> None:
        super().__init__()
        self.ack_entered = asyncio.Event()
        self.release_ack = asyncio.Event()

    async def acknowledge_emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("acknowledge_emergency_stop", kwargs))
        self.ack_entered.set()
        await self.release_ack.wait()
        return {"stop_id": kwargs["stop_id"], "status": "acknowledged"}


def _stop_headers(key: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"host", b"manager.test"),
        (b"authorization", b"Bearer operator-token"),
        (b"idempotency-key", key.encode()),
        (b"content-type", b"application/json"),
    ]


@pytest.mark.asyncio
async def test_concurrent_stop_acknowledgements_mutate_once_and_replay() -> None:
    service = _BlockingStopService()
    app = _app(service)
    stopped_status, stopped = await _raw_http(
        app,
        path=f"{API}/emergency-stop",
        headers=_stop_headers("stop-create"),
        payload={"unit_ids": ["pod-a"], "reason": "hazard"},
    )
    assert stopped_status == 202
    path = f"{API}/emergency-stop/{stopped['stop_id']}/acknowledge"

    first = asyncio.create_task(
        _raw_http(
            app,
            path=path,
            headers=_stop_headers("ack-same"),
            payload={"confirmation": "ACKNOWLEDGE"},
        )
    )
    await service.ack_entered.wait()
    duplicate = asyncio.create_task(
        _raw_http(
            app,
            path=path,
            headers=_stop_headers("ack-same"),
            payload={"confirmation": "ACKNOWLEDGE"},
        )
    )
    conflict = asyncio.create_task(
        _raw_http(
            app,
            path=path,
            headers=_stop_headers("ack-other"),
            payload={"confirmation": "ACKNOWLEDGE"},
        )
    )
    await asyncio.sleep(0)
    service.release_ack.set()

    first_result, duplicate_result, conflict_result = await asyncio.gather(
        first, duplicate, conflict
    )
    assert (
        first_result
        == duplicate_result
        == (
            200,
            {"stop_id": stopped["stop_id"], "status": "acknowledged"},
        )
    )
    assert conflict_result[0] == 409
    assert conflict_result[1]["code"] == "stop_id_mismatch"
    assert len([name for name, _ in service.calls if name == "acknowledge_emergency_stop"]) == 1

    replay = await _raw_http(
        app,
        path=path,
        headers=_stop_headers("ack-same"),
        payload={"confirmation": "ACKNOWLEDGE"},
    )
    assert replay == first_result
    assert len([name for name, _ in service.calls if name == "acknowledge_emergency_stop"]) == 1


@pytest.mark.asyncio
async def test_idempotency_conflict_during_inflight_work_does_not_cancel_leader() -> None:
    coordinator = IdempotencyCoordinator(max_completed_entries=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> StoredResult:
        entered.set()
        await release.wait()
        return StoredResult(202, {"ok": True})

    async def execute(payload: Mapping[str, Any]) -> StoredResult:
        return await coordinator.execute(
            principal_subject="person:operator",
            key="same",
            payload=payload,
            operation=operation,
        )

    leader = asyncio.create_task(execute({"watts": 1}))
    await entered.wait()
    with pytest.raises(IdempotencyConflictError):
        await execute({"watts": 2})
    assert not leader.done()
    release.set()
    assert await leader == StoredResult(202, {"ok": True})
