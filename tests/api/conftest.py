"""Boundary fakes shared by REST, event-stream, and MCP contract tests."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    interactive: bool = False
    site_id: str = "home"


PRINCIPALS = {
    "viewer-token": Principal("person:viewer", frozenset({"observe"})),
    "auditor-token": Principal("person:auditor", frozenset({"observe", "audit:read"})),
    # Holds audit:read but NOT the baseline observe scope; must be refused audit.
    "audit-only-token": Principal("person:audit-only", frozenset({"audit:read"})),
    "operator-token": Principal(
        "person:operator",
        frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
        interactive=True,
    ),
    "second-operator-token": Principal(
        "person:second-operator",
        frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
        interactive=True,
    ),
    "service-token": Principal(
        "service:optimizer",
        frozenset({"observe", "dispatch"}),
        interactive=False,
    ),
    "noninteractive-operator-token": Principal(
        "service:operator-automation",
        frozenset({"observe", "dispatch", "arm"}),
        interactive=False,
    ),
}


class FakeAuthenticator:
    def __init__(self) -> None:
        self.presented_tokens: list[str] = []

    async def authenticate(self, bearer_token: str) -> Principal | None:
        self.presented_tokens.append(bearer_token)
        return PRINCIPALS.get(bearer_token)


@dataclass
class RecordingEnergyService:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_revision: int = 40

    async def snapshot(self, *, principal: Principal) -> dict[str, Any]:
        self.calls.append(("snapshot", {"principal": principal}))
        return {
            "site_id": "home",
            "snapshot_sequence": 20,
            "captured_at": "2026-08-21T01:02:03Z",
            "units": [
                {
                    "unit_id": "pod-a",
                    "lifecycle": "disarmed",
                    "telemetry_age_s": 0.4,
                    "quality": "good",
                    "requested_power": {"direction": "idle", "watts": 0},
                    "authorized_power": {"direction": "idle", "watts": 0},
                    "measured_watts": 0,
                }
            ],
        }

    async def health(self, *, principal: Principal) -> dict[str, Any]:
        self.calls.append(("health", {"principal": principal}))
        return {
            "liveness": {"ok": True},
            "service_readiness": {"ready": True, "reasons": []},
            "control_readiness": {"ready": False, "reasons": ["units_disarmed"]},
        }

    async def recent_audit(self, *, principal: Principal, limit: int) -> dict[str, Any]:
        self.calls.append(("recent_audit", {"principal": principal, "limit": limit}))
        return {
            "events": [{"sequence": 7, "type": "intent.accepted", "request_id": "req-old"}],
            "next_cursor": None,
        }

    async def submit_intent(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("submit_intent", kwargs))
        self.next_revision += 1
        return {
            "intent_id": "intent-server-1",
            "acceptance_revision": self.next_revision,
            "accepted_at_monotonic": 123.5,
            "status": "accepted",
            "requested": {
                "direction": kwargs["direction"],
                "watts": kwargs["watts"],
            },
            "authorized": None,
            "measured": None,
            "expires_in_s": kwargs["ttl_s"],
        }

    async def arm(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("arm", kwargs))
        return {"unit_ids": kwargs["unit_ids"], "status": "armed_idle"}

    async def emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("emergency_stop", kwargs))
        return {"stop_id": "stop-server-1", "status": "latched"}

    async def acknowledge_emergency_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("acknowledge_emergency_stop", kwargs))
        return {"stop_id": kwargs["stop_id"], "status": "acknowledged"}

    async def acknowledge_inhibit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("acknowledge_inhibit", kwargs))
        if kwargs["unit_id"] == "pod-ghost":
            raise LookupError("no unit with id 'pod-ghost'")
        return {
            "unit_id": kwargs["unit_id"],
            "status": "acknowledged",
            "latch_cleared": True,
        }


class FakeEventSource:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.subscriptions: list[int | None] = []
        self.closed_subscriptions = 0

    async def subscribe(self, *, after_sequence: int | None) -> AsyncIterator[dict[str, Any]]:
        self.subscriptions.append(after_sequence)
        try:
            for event in self.events:
                yield event
        finally:
            self.closed_subscriptions += 1


def load_contract_module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"guarded boundary contract is not implemented: {name}: {exc}")


@pytest.fixture
def service() -> RecordingEnergyService:
    return RecordingEnergyService()


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()
