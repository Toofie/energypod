"""Exact-content pins for every facade happy-path audit event and publication.

The 2026-08-22 mutation run left ~86 survivors in the facade suite because it
asserted audit/publish *presence* (``assert_audited_and_published``) but never
*content*.  Each test here drives one happy-path operation on a fresh
deterministic rig and compares the appended ``AuditEvent`` and the published
bus body against one exact dictionary, computed with the FakeClock's fixed
monotonic (100.0) and wall (2026-08-21T01:02:03Z) stamps.  Fingerprints are
re-derived independently from the pinned fact dictionaries, so the fact set
that feeds them is part of the contract, not just its digest.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.test_service_facade import OPERATOR, make_rig, submit_kwargs


@pytest.fixture
def facade_api() -> Any:
    """The lazily-imported production namespace, same shape as the facade suite."""
    service = importlib.import_module("energypod.application.service")
    domain = importlib.import_module("energypod.domain")
    generation = importlib.import_module("energypod.application.generation")
    return SimpleNamespace(
        EnergyServiceFacade=service.EnergyServiceFacade,
        AuthorityGenerationCoordinator=generation.AuthorityGenerationCoordinator,
        Direction=domain.Direction,
        IntentSource=domain.IntentSource,
        PowerIntent=domain.PowerIntent,
        UnitLifecycle=domain.UnitLifecycle,
        Observation=domain.Observation,
        DataQuality=domain.DataQuality,
    )


WALL = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FACADE_ID = re.compile(r"^facade-[0-9a-f]{32}$")


def fingerprint(facts: Mapping[str, Any]) -> str:
    """Independent re-derivation of the documented fingerprint algorithm."""
    encoded = json.dumps(
        dict(facts), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_facts(event: Any) -> dict[str, Any]:
    """Project one appended audit event to a comparable plain dictionary."""
    dumped = event.model_dump()
    # Both are uuid-derived per-process values: pin their shape, not their bits.
    assert _FACADE_ID.fullmatch(dumped.pop("event_id")), "event ids are facade-uuid shaped"
    assert _FACADE_ID.fullmatch(dumped.pop("process_instance_id")), (
        "process instance ids are facade-uuid shaped"
    )
    dumped["observation_sequences"] = dict(dumped["observation_sequences"])
    return dumped


def assert_audit(rig: Any, expected: Mapping[str, Any]) -> None:
    """Exactly one appended audit event, byte-for-byte the pinned facts."""
    appended = rig.audit.appended
    assert len(appended) == 1, f"expected exactly one audit event, saw {len(appended)}"
    assert audit_facts(appended[0]) == dict(
        occurred_at=WALL,
        monotonic_offset_s=0.0,
        event_type=expected["event_type"],
        unit_id=expected.get("unit_id"),
        connection_epoch=None,
        cycle_id=None,
        generation=expected.get("generation"),
        principal=OPERATOR.subject,
        source=expected.get("source"),
        correlation_id=expected["correlation_id"],
        intent_id=expected.get("intent_id"),
        policy_version="facade",
        configuration_version=0,
        observation_sequences={},
        reason_codes=expected["reason_codes"],
        # Facade events move no power themselves: authority watts stay zero so
        # a facade event can never masquerade as a kernel grant.
        requested_active_w=0,
        authorized_active_w=0,
        requested_watts_by_unit=None,
        authorized_watts_by_unit=None,
        # Facade events describe no cycle composition (2026-08-24 concurrent
        # operations added the field to every row): no directions map either.
        directions_by_unit=None,
        request_fingerprint=fingerprint(expected["facts"]),
        response_fingerprint=fingerprint({"result": expected["result"]}),
        result=expected["result"],
        lifecycle=expected["lifecycle"],
    )


def assert_published(rig: Any, expected_body: Mapping[str, Any]) -> None:
    assert rig.bus.published == [dict(expected_body)]


async def test_submit_intent_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api)
    await rig.facade.submit_intent(**submit_kwargs())
    assert_audit(
        rig,
        {
            "event_type": "intent_accepted",
            "correlation_id": "facade:intent_accepted:request-1",
            "intent_id": "intent-0-100.000000",
            "source": facade_api.IntentSource.MANUAL,
            "reason_codes": ("accepted",),
            "result": "accepted",
            "lifecycle": facade_api.UnitLifecycle.DISARMED,
            "facts": {
                "event_type": "intent_accepted",
                "principal": OPERATOR.subject,
                "result": "accepted",
                "direction": "discharge",
                "unit_ids": ["pod-a"],
                "watts": 900,
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "intent.accepted",
            "payload": {
                "principal": OPERATOR.subject,
                "intent_id": "intent-0-100.000000",
                "direction": "discharge",
                "watts": 900,
                "unit_ids": ["pod-a"],
                "expires_in_s": 30.0,
            },
        },
    )


async def test_submit_intent_per_unit_audit_and_publication_content(
    facade_api: Any,
) -> None:
    """The per-unit dispatch carries its breakdown through audit and events."""
    rig = make_rig(facade_api)
    await rig.facade.submit_intent(
        **submit_kwargs(
            unit_ids=["pod-a", "pod-b"],
            watts=None,
            watts_by_unit={"pod-a": 900, "pod-b": 600},
        )
    )
    assert_audit(
        rig,
        {
            "event_type": "intent_accepted",
            "correlation_id": "facade:intent_accepted:request-1",
            "intent_id": "intent-0-100.000000",
            "source": facade_api.IntentSource.MANUAL,
            "reason_codes": ("accepted",),
            "result": "accepted",
            "lifecycle": facade_api.UnitLifecycle.DISARMED,
            "facts": {
                "event_type": "intent_accepted",
                "principal": OPERATOR.subject,
                "result": "accepted",
                "direction": "discharge",
                "unit_ids": ["pod-a", "pod-b"],
                "watts": 1_500,
                "watts_by_unit": {"pod-a": 900, "pod-b": 600},
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "intent.accepted",
            "payload": {
                "principal": OPERATOR.subject,
                "intent_id": "intent-0-100.000000",
                "direction": "discharge",
                "watts": 1_500,
                "watts_by_unit": {"pod-a": 900, "pod-b": 600},
                "unit_ids": ["pod-a", "pod-b"],
                "expires_in_s": 30.0,
            },
        },
    )


async def test_arm_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api)
    await rig.facade.arm(
        unit_ids=["pod-a"],
        principal=OPERATOR,
        idempotency_key="arm-key-9",
        request_id="request-r9",
    )
    assert_audit(
        rig,
        {
            "event_type": "unit_armed",
            "correlation_id": "facade:unit_armed:request-r9",
            "unit_id": "pod-a",
            "reason_codes": ("armed",),
            "result": "armed",
            "lifecycle": facade_api.UnitLifecycle.ARMED_IDLE,
            "facts": {
                "event_type": "unit_armed",
                "principal": OPERATOR.subject,
                "result": "armed",
                "unit_id": "pod-a",
                "status": "armed",
                "reason": "armed",
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "unit.armed",
            "payload": {
                "principal": OPERATOR.subject,
                "units": [{"unit_id": "pod-a", "status": "armed", "reason": "armed"}],
            },
        },
    )


async def test_disarm_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api)
    await rig.facade.disarm(
        unit_ids=["pod-a"],
        principal=OPERATOR,
        idempotency_key="disarm-key-9",
        request_id="request-d9",
    )
    assert_audit(
        rig,
        {
            "event_type": "unit_disarmed",
            "correlation_id": "facade:unit_disarmed:request-d9",
            "unit_id": "pod-a",
            "reason_codes": ("disarmed",),
            "result": "disarmed",
            "lifecycle": facade_api.UnitLifecycle.DISARMED,
            "facts": {
                "event_type": "unit_disarmed",
                "principal": OPERATOR.subject,
                "result": "disarmed",
                "unit_id": "pod-a",
                "status": "disarmed",
                "reason": "disarmed",
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "unit.disarmed",
            "payload": {
                "principal": OPERATOR.subject,
                "units": [{"unit_id": "pod-a", "status": "disarmed", "reason": "disarmed"}],
            },
        },
    )


async def test_emergency_stop_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api)
    result = await rig.facade.emergency_stop(
        unit_ids=["pod-a", "pod-b"],
        reason="operator button",
        principal=OPERATOR,
        idempotency_key="stop-key-1",
        request_id="request-s1",
    )
    assert result == {
        "stop_id": "stop-0-100.000000",
        "status": "latched",
        "unit_ids": ["pod-a", "pod-b"],
        "fenced_generation": 1,
        "degraded": [],
    }
    assert_audit(
        rig,
        {
            "event_type": "emergency_stop",
            "correlation_id": "facade:emergency_stop:request-s1",
            "intent_id": "stop-0-100.000000",
            "generation": 1,
            "source": facade_api.IntentSource.EMERGENCY_STOP,
            "reason_codes": ("latched",),
            "result": "latched",
            "lifecycle": facade_api.UnitLifecycle.INHIBITED,
            "facts": {
                "event_type": "emergency_stop",
                "principal": OPERATOR.subject,
                "result": "latched",
                "degraded": [],
                "reason": "operator button",
                "stop_id": "stop-0-100.000000",
                "unit_ids": ["pod-a", "pod-b"],
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "emergency_stop.latched",
            "payload": {
                "principal": OPERATOR.subject,
                "stop_id": "stop-0-100.000000",
                "unit_ids": ["pod-a", "pod-b"],
                "reason": "operator button",
                "generation": 1,
                "degraded": [],
            },
        },
    )


async def test_acknowledge_emergency_stop_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api)
    await rig.facade.emergency_stop(
        unit_ids=["pod-a", "pod-b"],
        reason="operator button",
        principal=OPERATOR,
        idempotency_key="stop-key-1",
        request_id="request-s1",
    )
    rig.reset_recorders()
    result = await rig.facade.acknowledge_emergency_stop(
        stop_id="stop-0-100.000000",
        principal=OPERATOR,
        idempotency_key="ack-key-1",
        request_id="request-s2",
    )
    assert result == {
        "stop_id": "stop-0-100.000000",
        "status": "acknowledged",
        "degraded": [],
    }
    assert_audit(
        rig,
        {
            "event_type": "stop_acknowledged",
            "correlation_id": "facade:stop_acknowledged:request-s2",
            "intent_id": "stop-0-100.000000",
            "source": facade_api.IntentSource.EMERGENCY_STOP,
            "reason_codes": ("acknowledged",),
            "result": "acknowledged",
            "lifecycle": facade_api.UnitLifecycle.DISARMED,
            "facts": {
                "event_type": "stop_acknowledged",
                "principal": OPERATOR.subject,
                "result": "acknowledged",
                "stop_id": "stop-0-100.000000",
                "unit_ids": ["pod-a", "pod-b"],
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "emergency_stop.acknowledged",
            "payload": {"principal": OPERATOR.subject, "stop_id": "stop-0-100.000000"},
        },
    )


async def test_acknowledge_inhibit_audit_and_publication_content(facade_api: Any) -> None:
    rig = make_rig(facade_api, units={"pod-a": {"inhibit_latched": True}, "pod-b": {}})
    result = await rig.facade.acknowledge_inhibit(
        unit_id="pod-a",
        principal=OPERATOR,
        idempotency_key="inhibit-key-1",
        request_id="request-i1",
    )
    assert result == {
        "unit_id": "pod-a",
        "status": "acknowledged",
        "latch_cleared": True,
        "degraded": [],
    }
    assert_audit(
        rig,
        {
            "event_type": "inhibit_acknowledged",
            "correlation_id": "facade:inhibit_acknowledged:request-i1",
            "unit_id": "pod-a",
            "reason_codes": ("latch_cleared",),
            "result": "acknowledged",
            "lifecycle": facade_api.UnitLifecycle.DISARMED,
            "facts": {
                "event_type": "inhibit_acknowledged",
                "principal": OPERATOR.subject,
                "result": "acknowledged",
                "latch_cleared": True,
                "unit_id": "pod-a",
            },
        },
    )
    assert_published(
        rig,
        {
            "type": "inhibit.acknowledged",
            "payload": {
                "principal": OPERATOR.subject,
                "unit_id": "pod-a",
                "latch_cleared": True,
            },
        },
    )
