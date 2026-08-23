"""Adversarial contract for canonical ControlKernel decision auditing.

Event construction is synchronous: validating an immutable value is not I/O.
Durable ``AuditRepository.append`` is the sole asynchronous audit boundary.
Audit power is a presentation projection: export/discharge positive,
import/charge negative, idle zero. Domain power remains unsigned plus direction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from energypod.domain import DecisionStatus, Direction, IntentSource, UnitLifecycle
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError

from .test_control_kernel import (
    UNITS,
    Audit,
    Authorizations,
    Clock,
    Decision,
    Intent,
    Setpoint,
    api,
    decision_for,
    intent,
    make_kernel,
    observation_pairs,
    settle,
)

__all__ = ["api"]

SCHEMA = "energypod.control-decision.v1"
PROCESS_ID = "process-20260821-01"
PROCESS_ORIGIN_MONO = 40.0
WALL_TIME = datetime(2026, 8, 21, 3, 4, 5, 6000, tzinfo=UTC)

# Sentinel distinguishes "no override" from an explicit ``None`` override so the
# factory can be forced to return ``None`` exactly as a malicious factory would.
_UNSET: object = object()


def _canonical_json(value: object) -> bytes:
    """Exact v1 encoding for JSON-safe, integer-only fingerprint projections."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _signed(direction: Direction, watts: int) -> int:
    if direction is Direction.CHARGE:
        return -watts
    if direction is Direction.DISCHARGE:
        return watts
    return 0


def _aggregate_lifecycle(intent_value: Any, decision: Any) -> UnitLifecycle:
    if intent_value.source is IntentSource.EMERGENCY_STOP:
        return UnitLifecycle.INHIBITED
    if intent_value.direction is Direction.IDLE or decision.status is DecisionStatus.REJECTED:
        return UnitLifecycle.ARMED_IDLE
    if decision.status in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED}:
        return UnitLifecycle.ACTIVE
    return UnitLifecycle.INHIBITED


@dataclass(frozen=True)
class AuditIdentity:
    event_id: str
    correlation_id: str
    occurred_at: datetime


class DeterministicAuditEventFactory:
    """Strict synchronous stand-in for the production value factory."""

    def __init__(
        self,
        *,
        process_instance_id: str = PROCESS_ID,
        process_origin_mono: float = PROCESS_ORIGIN_MONO,
        first_wall_time: datetime = WALL_TIME,
        result: object = _UNSET,
        error: BaseException | None = None,
    ) -> None:
        self.process_instance_id = process_instance_id
        self.process_origin_mono = process_origin_mono
        self.first_wall_time = first_wall_time
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **facts: Any) -> AuditEvent:
        self.calls.append(facts)
        if self.error is not None:
            raise self.error
        if self.result is not _UNSET:
            return self.result  # type: ignore[return-value]
        number = len(self.calls)
        intent_value = facts["intent"]
        correlation = (
            f"emergency_stop:{intent_value.id}"
            if intent_value.source is IntentSource.EMERGENCY_STOP
            else f"intent:{intent_value.id}:revision:{intent_value.acceptance_revision}"
        )
        identity = AuditIdentity(
            event_id=f"event-{number:04d}",
            correlation_id=correlation,
            occurred_at=self.first_wall_time + timedelta(microseconds=number - 1),
        )
        return self._build(identity=identity, **facts)

    def _build(self, *, identity: AuditIdentity, **facts: Any) -> AuditEvent:
        intent_value = facts["intent"]
        decision = facts["decision"]
        batch = facts["authorization_batch"]
        observations = facts["observations"]
        decision_id = facts["decision_id"]
        authorized = (
            sum(_signed(item.direction, item.watts) for item in decision.setpoints)
            if batch is not None
            else 0
        )
        request_projection = {
            "schema": SCHEMA,
            "acceptance_revision": intent_value.acceptance_revision,
            "direction": intent_value.direction.value,
            "intent_id": intent_value.id,
            "principal": intent_value.actor_identity,
            "source": intent_value.source.value,
            "unit_ids": sorted(intent_value.unit_ids),
            "watts": intent_value.watts,
        }
        requested_watts_by_unit = getattr(intent_value, "watts_by_unit", None)
        if requested_watts_by_unit is not None:
            request_projection["watts_by_unit"] = dict(sorted(requested_watts_by_unit.items()))
        response_projection = {
            "schema": SCHEMA,
            "authorized_active_w": authorized,
            "decision_id": decision_id,
            "reason_codes": list(decision.reason_codes),
            "setpoints": [
                {
                    "direction": item.direction.value,
                    "reactive_vars": item.reactive_vars,
                    "unit_id": item.unit_id,
                    "watts": item.watts,
                }
                for item in sorted(decision.setpoints, key=lambda item: item.unit_id)
            ],
            "status": decision.status.value,
        }
        return AuditEvent(
            event_id=identity.event_id,
            occurred_at=identity.occurred_at,
            monotonic_offset_s=facts["decided_at_mono"] - self.process_origin_mono,
            process_instance_id=self.process_instance_id,
            event_type="control_decision",
            unit_id=facts.get("unit_id"),
            connection_epoch=None,
            generation=facts["generation"],
            cycle_id=facts["cycle_id"],
            principal=intent_value.actor_identity,
            source=intent_value.source,
            correlation_id=identity.correlation_id,
            intent_id=intent_value.id,
            policy_version=facts["policy_version"],
            configuration_version=facts["configuration_version"],
            observation_sequences={key: value.sequence for key, value in observations.items()},
            reason_codes=decision.reason_codes,
            requested_active_w=_signed(intent_value.direction, intent_value.watts),
            authorized_active_w=authorized,
            requested_watts_by_unit=(
                None
                if requested_watts_by_unit is None
                else dict(sorted(requested_watts_by_unit.items()))
            ),
            authorized_watts_by_unit=(
                {
                    item.unit_id: item.watts
                    for item in sorted(decision.setpoints, key=lambda item: item.unit_id)
                }
                if batch is not None
                else None
            ),
            request_fingerprint=_fingerprint(request_projection),
            response_fingerprint=_fingerprint(response_projection),
            result=decision.status.value,
            lifecycle=_aggregate_lifecycle(intent_value, decision),
        )


def _kernel_with_factory(
    api: Any,
    value: Any,
    outcome: Any,
    factory: DeterministicAuditEventFactory,
    **kwargs: Any,
) -> tuple[Any, list[str], Any, Audit]:
    kernel, history, authorizations, audit, _, _ = make_kernel(api, value, outcome, **kwargs)
    kernel = api.ControlKernel(
        clock=kwargs.get("clock") or Clock(),
        unit_ids=kernel._unit_ids,
        intents=kernel._intents,
        observations=kernel._observations,
        authorizations=authorizations,
        audit=audit,
        arbiter=kernel._arbiter,
        allocator=kernel._allocator,
        safety=kernel._safety,
        policy=kernel._policy,
        generation=8,
        configuration_version=12,
        audit_event_factory=factory,
    )
    return kernel, history, authorizations, audit


async def test_real_canonical_event_is_durable_before_atomic_publication(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory()
    kernel, history, auth, audit = _kernel_with_factory(
        api, value, decision_for(api, value), factory
    )
    await kernel.tick()
    assert history.index("audit") < history.index("publish")
    assert len(audit.events) == 1 and type(audit.events[0]) is AuditEvent
    assert len(auth.published) == 1


async def test_control_decision_row_carries_the_per_unit_watt_breakdowns(api: Any):
    """The 2026-08-23 fleet-row opacity fix: one control_decision row names
    the per-unit requested targets AND the per-unit authorized watts, so a
    fleet-level decision no longer needs fingerprint inference to explain
    which battery got what.  Unsigned magnitudes, like the domain."""
    from .test_control_kernel import Proposal

    value = Intent(
        "intent-per-unit",
        api.IntentSource.MANUAL,
        UNITS,
        api.Direction.DISCHARGE,
        600,
        watts_by_unit={"lhs": 300, "mid": 100, "rhs": 200},
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("lhs", value.direction, 300, value.id),
            Setpoint("mid", value.direction, 100, value.id),
            Setpoint("rhs", value.direction, 200, value.id),
        ),
    )
    factory = DeterministicAuditEventFactory()
    kernel, _history, auth, audit = _kernel_with_factory(api, value, outcome, factory)
    kernel._allocator.output = tuple(
        Proposal(unit, value.direction, watts, value.id, value.expires_at_mono)
        for unit, watts in (("lhs", 300), ("mid", 100), ("rhs", 200))
    )
    await kernel.tick()
    (event,) = audit.events
    assert dict(event.requested_watts_by_unit) == {"lhs": 300, "mid": 100, "rhs": 200}
    assert dict(event.authorized_watts_by_unit) == {"lhs": 300, "mid": 100, "rhs": 200}
    assert auth.published


async def test_scalar_decisions_still_carry_their_per_unit_authorized_breakdown(api: Any):
    """A scalar (fleet-total) intent has no per-unit request to record, but
    its authorized breakdown is still per unit -- the row explains the split
    the allocator chose without any fingerprint inference."""
    value = intent(api)  # scalar 900 W over lhs/mid/rhs -> 300 W each
    factory = DeterministicAuditEventFactory()
    kernel, _history, _auth, audit = _kernel_with_factory(
        api, value, decision_for(api, value), factory
    )
    await kernel.tick()
    (event,) = audit.events
    assert event.requested_watts_by_unit is None
    assert dict(event.authorized_watts_by_unit) == {"lhs": 300, "mid": 300, "rhs": 300}


async def test_factory_is_synchronous_and_receives_complete_explicit_facts(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory()
    kernel, _, auth, _ = _kernel_with_factory(api, value, decision_for(api, value), factory)
    await kernel.tick()
    assert not asyncio.iscoroutinefunction(factory.create)
    assert set(factory.calls[0]) == {
        "authorization_batch",
        "configuration_version",
        "cycle_id",
        "decided_at_mono",
        "decision",
        "decision_id",
        "generation",
        "intent",
        "observations",
        "policy_version",
        "unit_id",
    }
    assert factory.calls[0]["authorization_batch"] is auth.published[0]


async def test_utc_and_process_relative_monotonic_time_have_distinct_semantics(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory(process_origin_mono=40.0)
    kernel, _, _, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    await kernel.tick()
    event = audit.events[0]
    assert event.occurred_at == WALL_TIME and event.occurred_at.tzinfo is UTC
    assert event.monotonic_offset_s == 60.0
    assert event.process_instance_id == PROCESS_ID


async def test_cycle_and_event_ids_are_unique_but_intent_correlation_is_stable(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory()
    kernel, _, _, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    await kernel.tick()
    await kernel.tick()
    first, second = audit.events
    assert first.event_id != second.event_id
    assert first.cycle_id != second.cycle_id
    assert factory.calls[0]["decision_id"] != factory.calls[1]["decision_id"]
    assert first.correlation_id == second.correlation_id
    assert first.correlation_id == "intent:intent-1:revision:17"


async def test_fleet_event_is_complete_without_fabricating_singular_unit_facts(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory()
    kernel, _, auth, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    await kernel.tick()
    event, batch = audit.events[0], auth.published[0]
    assert (event.unit_id, event.connection_epoch) == (None, None)
    assert event.generation == batch.generation == 8
    assert event.cycle_id == batch.cycle_id
    assert dict(event.observation_sequences) == {"lhs": 10, "mid": 10, "rhs": 10}
    assert {item.decision_id for item in batch.authorizations} == {factory.calls[0]["decision_id"]}
    assert (event.policy_version, event.configuration_version) == ("policy-5", 12)


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(Direction.DISCHARGE, 900), (Direction.CHARGE, -900)],
)
async def test_signed_audit_projection_does_not_change_unsigned_domain_power(
    api: Any, direction: Direction, expected: int
):
    value = replace(intent(api), direction=direction)
    outcome = decision_for(api, value)
    factory = DeterministicAuditEventFactory()
    kernel, _, _, audit = _kernel_with_factory(api, value, outcome, factory)
    await kernel.tick()
    assert value.watts == 900 and all(item.watts == 300 for item in outcome.setpoints)
    assert audit.events[0].requested_active_w == expected
    assert audit.events[0].authorized_active_w == expected


@pytest.mark.parametrize(
    ("status", "authorized_watts", "lifecycle"),
    [
        (DecisionStatus.AUTHORIZED, 900, UnitLifecycle.ACTIVE),
        (DecisionStatus.CLAMPED, 600, UnitLifecycle.ACTIVE),
        (DecisionStatus.REJECTED, 0, UnitLifecycle.ARMED_IDLE),
    ],
)
async def test_authorized_clamped_and_rejected_results_are_not_conflated(
    api: Any,
    status: DecisionStatus,
    authorized_watts: int,
    lifecycle: UnitLifecycle,
):
    value = intent(api)
    per_unit = authorized_watts // len(UNITS)
    outcome = Decision(
        status,
        tuple(Setpoint(unit, value.direction, per_unit, value.id) for unit in sorted(UNITS)),
        ("static_limit",) if status is DecisionStatus.CLAMPED else ("blocking_fault",),
    )
    factory = DeterministicAuditEventFactory()
    kernel, _, auth, audit = _kernel_with_factory(api, value, outcome, factory)
    await kernel.tick()
    event = audit.events[0]
    assert (event.requested_active_w, event.authorized_active_w) == (900, authorized_watts)
    assert (event.result, event.reason_codes, event.lifecycle) == (
        status.value,
        outcome.reason_codes,
        lifecycle,
    )
    assert bool(auth.published) is (status in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED})


@pytest.mark.parametrize("emergency", [False, True], ids=["idle", "emergency-stop"])
async def test_idle_and_emergency_are_zero_but_have_distinct_lifecycle(api: Any, emergency: bool):
    value = intent(api, emergency=emergency)
    if not emergency:
        value = replace(value, direction=Direction.IDLE, watts=0)
    outcome = decision_for(
        api,
        value,
        status=DecisionStatus.REVOKED if emergency else DecisionStatus.REJECTED,
    )
    factory = DeterministicAuditEventFactory()
    kernel, history, auth, audit = _kernel_with_factory(api, value, outcome, factory)
    await kernel.tick()
    event = audit.events[0]
    assert (event.requested_active_w, event.authorized_active_w) == (0, 0)
    assert event.lifecycle is (UnitLifecycle.INHIBITED if emergency else UnitLifecycle.ARMED_IDLE)
    assert not auth.published
    if emergency:
        assert history.index("revoke") < history.index("audit")


async def test_fingerprints_have_explicit_schema_and_normalized_setpoint_order(api: Any):
    value = intent(api)
    outcome = decision_for(api, value)
    first, second = DeterministicAuditEventFactory(), DeterministicAuditEventFactory()
    kernel1, _, _, audit1 = _kernel_with_factory(api, value, outcome, first)
    reversed_outcome = replace(outcome, setpoints=tuple(reversed(outcome.setpoints)))
    kernel2, _, _, audit2 = _kernel_with_factory(api, value, reversed_outcome, second)
    await kernel1.tick()
    await kernel2.tick()
    assert audit1.events[0].request_fingerprint == audit2.events[0].request_fingerprint
    assert audit1.events[0].response_fingerprint == audit2.events[0].response_fingerprint
    assert len(bytes.fromhex(audit1.events[0].request_fingerprint)) == 32
    assert len(bytes.fromhex(audit1.events[0].response_fingerprint)) == 32


async def test_factory_and_event_never_receive_ambient_secrets(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory()
    kernel, _, _, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    await kernel.tick()
    forbidden = {"authorization", "cookie", "credential", "password", "secret", "token"}
    assert forbidden.isdisjoint(factory.calls[0])
    serialized = audit.events[0].model_dump_json().casefold()
    assert all(word not in serialized for word in forbidden)


@pytest.mark.parametrize(
    "malicious",
    [{"event_id": "mapping"}, SimpleNamespace(event_id="duck"), None],
    ids=["mapping", "duck-type", "none"],
)
async def test_factory_must_return_exact_validated_audit_event(api: Any, malicious: object):
    value = intent(api)
    factory = DeterministicAuditEventFactory(result=malicious)
    kernel, _, auth, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    with pytest.raises(TypeError, match="AuditEvent"):
        await kernel.tick()
    assert not audit.events and not auth.published and auth.revocations


async def test_valid_but_mismatched_factory_event_is_rejected_before_append(api: Any):
    value = intent(api)
    seed = DeterministicAuditEventFactory()
    kernel, _, _, audit = _kernel_with_factory(api, value, decision_for(api, value), seed)
    await kernel.tick()
    forged = audit.events[0].model_copy(update={"cycle_id": "cycle-forged"})
    factory = DeterministicAuditEventFactory(result=forged)
    kernel, _, auth, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    with pytest.raises(ValueError, match="audit.*cycle|cycle.*audit"):
        await kernel.tick()
    assert not audit.events and not auth.published and auth.revocations


def test_real_audit_event_rejects_non_utc_and_nonfinite_offset():
    base = dict(
        event_id="event-1",
        occurred_at=WALL_TIME,
        monotonic_offset_s=1.0,
        process_instance_id=PROCESS_ID,
        event_type="control_decision",
        generation=1,
        cycle_id="cycle-1",
        principal="operator-1",
        source=IntentSource.MANUAL,
        correlation_id="correlation-1",
        intent_id="intent-1",
        policy_version="policy-1",
        configuration_version=1,
        observation_sequences={"lhs": 1},
        reason_codes=("ok",),
        requested_active_w=1,
        authorized_active_w=1,
        request_fingerprint="0" * 64,
        response_fingerprint="1" * 64,
        result="authorized",
        lifecycle=UnitLifecycle.ACTIVE,
    )
    with pytest.raises(ValidationError, match="UTC"):
        AuditEvent(**{**base, "occurred_at": WALL_TIME.replace(tzinfo=None)})
    with pytest.raises(ValidationError, match="finite"):
        AuditEvent(**{**base, "monotonic_offset_s": float("nan")})


async def test_duplicate_or_backpressured_audit_never_exposes_authority(api: Any):
    value, gate = intent(api), asyncio.Event()
    factory = DeterministicAuditEventFactory()
    kernel, _, auth, audit = _kernel_with_factory(
        api, value, decision_for(api, value), factory, audit_gate=gate
    )
    task = asyncio.create_task(kernel.tick())
    assert await settle(audit.entered.is_set)
    assert not auth.published
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert auth.revocations and not auth.published

    kernel, _, auth, audit = _kernel_with_factory(
        api, value, decision_for(api, value), DeterministicAuditEventFactory()
    )
    audit.error = DuplicateAuditEventError("event-0001 already exists")
    with pytest.raises(DuplicateAuditEventError, match="already exists"):
        await kernel.tick()
    assert auth.revocations and not auth.published


async def test_factory_failure_revokes_without_append_or_publication(api: Any):
    value = intent(api)
    factory = DeterministicAuditEventFactory(error=RuntimeError("factory failed"))
    kernel, _, auth, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    with pytest.raises(RuntimeError, match="factory failed"):
        await kernel.tick()
    assert not audit.events and not auth.published and auth.revocations


async def test_construction_requires_a_canonical_audit_event_factory(api: Any):
    value = intent(api)
    kernel, _, _, _, _, _ = make_kernel(api, value, decision_for(api, value))
    with pytest.raises(TypeError, match="audit_event_factory"):
        api.ControlKernel(
            clock=Clock(),
            unit_ids=kernel._unit_ids,
            intents=kernel._intents,
            observations=kernel._observations,
            authorizations=Authorizations([]),
            audit=Audit([]),
            arbiter=kernel._arbiter,
            allocator=kernel._allocator,
            safety=kernel._safety,
            policy=kernel._policy,
            generation=8,
            configuration_version=12,
            audit_event_factory=None,
        )


async def test_missing_selected_unit_observation_audits_rejection_without_crashing(api: Any):
    value = intent(api)
    current, previous = observation_pairs(value.unit_ids)
    current.pop("lhs")
    kernel, _, auth, audit = _kernel_with_factory(
        api,
        value,
        decision_for(api, value),
        DeterministicAuditEventFactory(),
        current=current,
        previous=previous,
    )
    await kernel.tick()
    assert len(audit.events) == 1
    assert audit.events[0].authorized_active_w == 0
    assert not auth.published and auth.revocations


# --- per-unit attribution and stop linkage (2026-08-23 console Activity) -------
#
# The console's Activity view filters audit rows by unit_id, so a fleet-wide
# control_decision with unit_id None never appears under a unit filter.  A
# decision that selects exactly ONE unit now carries that unit's id on its
# row (fleet-level fields unchanged); a genuinely multi-unit decision stays
# fleet-level.  And a decision held by a latched emergency stop correlates
# to the stop id explicitly, so the Activity view names the stop on the row
# itself instead of guessing from the newest latch event.


async def test_single_unit_decisions_carry_unit_attribution(api: Any):
    value = intent(api, units=frozenset({"mid"}))
    kernel, _, auth, audit = _kernel_with_factory(
        api, value, decision_for(api, value), DeterministicAuditEventFactory()
    )
    await kernel.tick()
    assert auth.published
    (event,) = audit.events
    assert event.unit_id == "mid"
    # Fleet-level evidence is unchanged: the full observation map, the cycle,
    # and both watt figures still name the whole decision.
    assert dict(event.observation_sequences) == {"mid": 10}
    assert event.requested_active_w == 900
    assert event.authorized_active_w == 900


async def test_multi_unit_decisions_stay_fleet_level(api: Any):
    """A row that names three units cannot carry one unit id: it stays
    fleet-level and appears under All units, exactly as before."""
    value = intent(api)
    kernel, _, _, audit = _kernel_with_factory(
        api, value, decision_for(api, value), DeterministicAuditEventFactory()
    )
    await kernel.tick()
    (event,) = audit.events
    assert event.unit_id is None
    assert set(event.observation_sequences) == set(value.unit_ids)


async def test_stop_held_decisions_correlate_to_the_stop_id(api: Any):
    value = intent(api, emergency=True)
    kernel, _, _, audit = _kernel_with_factory(
        api, value, decision_for(api, value), DeterministicAuditEventFactory()
    )
    await kernel.tick()
    (event,) = audit.events
    assert event.source is IntentSource.EMERGENCY_STOP
    assert event.intent_id == value.id
    assert event.correlation_id == f"emergency_stop:{value.id}"


async def test_a_factory_attributing_the_wrong_unit_is_rejected(api: Any):
    value = intent(api, units=frozenset({"mid"}))
    honest = DeterministicAuditEventFactory()
    kernel, _, _, audit = _kernel_with_factory(api, value, decision_for(api, value), honest)
    await kernel.tick()
    assert audit.events[0].unit_id == "mid"
    forged = audit.events[0].model_copy(update={"unit_id": "rhs"})
    factory = DeterministicAuditEventFactory(result=forged)
    kernel, _, auth, audit = _kernel_with_factory(api, value, decision_for(api, value), factory)
    with pytest.raises(ValueError, match="audit event does not match control cycle facts"):
        await kernel.tick()
    assert not audit.events and not auth.published and auth.revocations
