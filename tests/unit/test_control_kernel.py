"""S0 orchestration contracts for one deterministic control-kernel cycle."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

UNITS = frozenset({"mid", "rhs", "lhs"})


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return datetime(2026, 8, 21, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class IntentRecord:
    id: str
    source: Any
    unit_ids: frozenset[str]
    direction: Any
    watts: int
    created_at_mono: float = 99.0
    expires_at_mono: float = 110.0
    revision: int = 1


@dataclass(frozen=True)
class ObservationRecord:
    unit_id: str
    connection_epoch: int = 1
    sequence: int = 10
    captured_at_mono: float = 99.9
    quality: str = "good"


@dataclass(frozen=True)
class AuthorizationRecord:
    unit_id: str
    generation: int
    cycle_id: int
    intent_id: str
    direction: Any
    watts: int
    connection_epoch: int = 1
    observation_sequence: int = 10
    issued_at_mono: float = 100.0
    expires_at_mono: float = 101.0


@dataclass(frozen=True)
class DecisionRecord:
    status: Any
    intent_id: str
    cycle_id: int
    authorizations: tuple[AuthorizationRecord, ...]
    reasons: tuple[str, ...] = ()


class FakeIntentRepository:
    def __init__(self, intents: tuple[IntentRecord, ...], history: list[str]) -> None:
        self.intents = intents
        self.history = history

    async def active(self, now_mono: float) -> tuple[IntentRecord, ...]:
        assert now_mono == 100.0
        self.history.append("intents")
        return self.intents


class FakeObservationRepository:
    def __init__(self, values: dict[str, ObservationRecord], history: list[str]) -> None:
        self.values = values
        self.history = history

    async def all_latest(self) -> dict[str, ObservationRecord]:
        self.history.append("observations")
        return dict(self.values)


class FakeAuthorizationRepository:
    def __init__(
        self,
        history: list[str],
        publish_error: BaseException | None = None,
        publish_gate: asyncio.Event | None = None,
    ) -> None:
        self.history = history
        self.publish_error = publish_error
        self.publish_gate = publish_gate
        self.publish_entered = asyncio.Event()
        self.published: list[tuple[AuthorizationRecord, ...]] = []
        self.revocations: list[tuple[Any, str]] = []

    async def publish(self, batch: tuple[AuthorizationRecord, ...]) -> None:
        self.history.append("publish")
        self.publish_entered.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append(tuple(batch))

    async def revoke(
        self,
        unit_ids: Any = None,
        *,
        reason: str,
        **_context: Any,
    ) -> None:
        self.history.append("revoke")
        self.revocations.append((unit_ids, reason))


class FakeAuditRepository:
    def __init__(
        self,
        history: list[str],
        *,
        error: BaseException | None = None,
        after_append: Callable[[], None] | None = None,
        append_gate: asyncio.Event | None = None,
    ) -> None:
        self.history = history
        self.error = error
        self.after_append = after_append
        self.append_gate = append_gate
        self.append_entered = asyncio.Event()
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.history.append("audit")
        self.append_entered.set()
        if self.append_gate is not None:
            await self.append_gate.wait()
        if self.error is not None:
            raise self.error
        self.events.append(event)
        if self.after_append is not None:
            self.after_append()


class FakeArbiter:
    def __init__(self, winner: IntentRecord, history: list[str]) -> None:
        self.winner = winner
        self.history = history
        self.calls: list[tuple[tuple[IntentRecord, ...], float]] = []

    def select(self, intents: tuple[IntentRecord, ...], now_mono: float) -> IntentRecord:
        self.history.append("select")
        self.calls.append((intents, now_mono))
        return self.winner


class FakeSafetyKernel:
    def __init__(
        self,
        outcome: DecisionRecord | BaseException,
        history: list[str],
    ) -> None:
        self.outcome = outcome
        self.history = history
        self.calls: list[tuple[Any, Any, Any, float]] = []

    def evaluate(
        self,
        intent: IntentRecord,
        observations: dict[str, ObservationRecord],
        policy: Any,
        now_mono: float,
    ) -> DecisionRecord:
        self.history.append("safety")
        self.calls.append((intent, observations, policy, now_mono))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def contract() -> Any:
    try:
        kernel_module = importlib.import_module("energypod.application.control_kernel")
        domain_module = importlib.import_module("energypod.domain")
        return type(
            "Contract",
            (),
            {
                "ControlKernel": kernel_module.ControlKernel,
                "DecisionStatus": domain_module.DecisionStatus,
                "Direction": domain_module.Direction,
                "IntentSource": domain_module.IntentSource,
            },
        )
    except (ImportError, AttributeError) as error:
        pytest.fail(f"Control-kernel contract is not implemented: {error}", pytrace=False)


def make_intent(
    contract: Any,
    *,
    emergency: bool = False,
    units: frozenset[str] = UNITS,
) -> IntentRecord:
    return IntentRecord(
        id="emergency-1" if emergency else "manual-1",
        source=(
            contract.IntentSource.EMERGENCY_STOP if emergency else contract.IntentSource.MANUAL
        ),
        unit_ids=units,
        direction=contract.Direction.IDLE if emergency else contract.Direction.DISCHARGE,
        watts=0 if emergency else 900,
    )


def make_authorizations(intent_id: str = "manual-1") -> tuple[AuthorizationRecord, ...]:
    return tuple(
        AuthorizationRecord(
            unit_id=unit_id,
            generation=1,
            cycle_id=1,
            intent_id=intent_id,
            direction="discharge",
            watts=300,
        )
        for unit_id in sorted(UNITS)
    )


def make_kernel(
    contract: Any,
    *,
    intent: IntentRecord,
    observations: dict[str, ObservationRecord],
    decision: DecisionRecord | BaseException,
    clock: FakeClock | None = None,
    audit_error: BaseException | None = None,
    after_audit: Callable[[], None] | None = None,
    publish_error: BaseException | None = None,
    audit_gate: asyncio.Event | None = None,
    publish_gate: asyncio.Event | None = None,
) -> tuple[Any, list[str], FakeAuthorizationRepository, FakeAuditRepository, FakeSafetyKernel]:
    history: list[str] = []
    test_clock = clock or FakeClock()
    authorizations = FakeAuthorizationRepository(history, publish_error, publish_gate)
    audit = FakeAuditRepository(
        history,
        error=audit_error,
        after_append=after_audit,
        append_gate=audit_gate,
    )
    safety = FakeSafetyKernel(decision, history)
    kernel = contract.ControlKernel(
        clock=test_clock,
        unit_ids=UNITS,
        intents=FakeIntentRepository((intent,), history),
        observations=FakeObservationRepository(observations, history),
        authorizations=authorizations,
        audit=audit,
        arbiter=FakeArbiter(intent, history),
        safety=safety,
        policy=object(),
    )
    return kernel, history, authorizations, audit, safety


def complete_observations() -> dict[str, ObservationRecord]:
    return {unit_id: ObservationRecord(unit_id) for unit_id in UNITS}


async def settle_until(predicate: Callable[[], bool], turns: int = 50) -> bool:
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


async def test_tick_selects_evaluates_audits_then_publishes(contract: Any) -> None:
    intent = make_intent(contract)
    batch = make_authorizations()
    decision = DecisionRecord(
        status=contract.DecisionStatus.AUTHORIZED,
        intent_id=intent.id,
        cycle_id=1,
        authorizations=batch,
    )
    kernel, history, authorizations, audit, safety = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
    )

    result = await kernel.tick()

    assert result is decision
    assert history == ["intents", "observations", "select", "safety", "audit", "publish"]
    assert safety.calls[0][0] is intent
    assert audit.events == [decision]
    assert authorizations.published == [batch]


async def test_audit_failure_revokes_all_and_never_publishes(contract: Any) -> None:
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, history, authorizations, _, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        audit_error=OSError("audit volume full"),
    )

    with pytest.raises(OSError, match="audit volume full"):
        await kernel.tick()

    assert authorizations.published == []
    assert authorizations.revocations
    assert history[-2:] == ["audit", "revoke"]


async def test_cancellation_while_audit_is_pending_revokes_without_publishing(
    contract: Any,
) -> None:
    audit_gate = asyncio.Event()
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, history, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        audit_gate=audit_gate,
    )

    tick = asyncio.create_task(kernel.tick())
    entered_audit = await settle_until(audit.append_entered.is_set)
    if not entered_audit:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
    assert entered_audit, "control cycle never reached durable audit acceptance"
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick

    assert authorizations.published == []
    assert authorizations.revocations
    assert history[-2:] == ["audit", "revoke"]


async def test_failed_control_evaluation_revokes_all_authority(contract: Any) -> None:
    intent = make_intent(contract)
    kernel, history, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=RuntimeError("control task died"),
    )

    with pytest.raises(RuntimeError, match="control task died"):
        await kernel.tick()

    assert audit.events == []
    assert authorizations.published == []
    assert authorizations.revocations
    assert history[-1] == "revoke"


async def test_dead_cycle_after_audit_cannot_publish_expired_authority(contract: Any) -> None:
    clock = FakeClock()
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, _, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        clock=clock,
        after_audit=lambda: clock.advance(1.01),
    )

    await kernel.tick()

    assert audit.events == [decision]
    assert authorizations.published == []
    assert any(
        reason == "control_cycle_deadline_expired" for _, reason in authorizations.revocations
    )


@pytest.mark.parametrize("elapsed", [1.0, 1.001])
async def test_authority_is_not_published_at_or_after_its_deadline(
    contract: Any,
    elapsed: float,
) -> None:
    clock = FakeClock()
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, _, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        clock=clock,
        after_audit=lambda: clock.advance(elapsed),
    )

    await kernel.tick()

    assert audit.events == [decision]
    assert authorizations.published == []
    assert any(
        reason == "control_cycle_deadline_expired" for _, reason in authorizations.revocations
    )


async def test_partial_data_for_requested_fleet_fails_closed(contract: Any) -> None:
    intent = make_intent(contract)
    unsafe_partial_batch = tuple(
        authorization for authorization in make_authorizations() if authorization.unit_id != "lhs"
    )
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        unsafe_partial_batch,
    )
    partial = complete_observations()
    partial.pop("lhs")
    kernel, _, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=partial,
        decision=decision,
    )

    await kernel.tick()

    assert authorizations.published == []
    assert authorizations.revocations
    assert audit.events


async def test_partial_fleet_is_allowed_only_when_intent_explicitly_selects_subset(
    contract: Any,
) -> None:
    selected = frozenset({"mid", "rhs"})
    intent = make_intent(contract, units=selected)
    batch = tuple(AuthorizationRecord(unit, 1, 1, intent.id, 450) for unit in sorted(selected))
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        batch,
    )
    observations = {unit: ObservationRecord(unit) for unit in selected}
    kernel, _, authorizations, _, _ = make_kernel(
        contract,
        intent=intent,
        observations=observations,
        decision=decision,
    )

    await kernel.tick()

    assert authorizations.published == [batch]


async def test_emergency_stop_revokes_before_any_publication(contract: Any) -> None:
    emergency = make_intent(contract, emergency=True)
    stale_nonzero = make_authorizations(intent_id=emergency.id)
    decision = DecisionRecord(
        contract.DecisionStatus.REVOKED,
        emergency.id,
        2,
        stale_nonzero,
        ("emergency_stop",),
    )
    kernel, history, authorizations, audit, _ = make_kernel(
        contract,
        intent=emergency,
        observations=complete_observations(),
        decision=decision,
    )

    await kernel.tick()

    assert authorizations.published == []
    assert authorizations.revocations
    assert audit.events == [decision]
    assert history.index("revoke") < history.index("audit")


async def test_emergency_stop_revokes_before_waiting_for_durable_audit(
    contract: Any,
) -> None:
    audit_gate = asyncio.Event()
    emergency = make_intent(contract, emergency=True)
    decision = DecisionRecord(
        contract.DecisionStatus.REVOKED,
        emergency.id,
        2,
        (),
        ("emergency_stop",),
    )
    kernel, _, authorizations, audit, _ = make_kernel(
        contract,
        intent=emergency,
        observations=complete_observations(),
        decision=decision,
        audit_gate=audit_gate,
    )

    tick = asyncio.create_task(kernel.tick())
    entered_audit = await settle_until(audit.append_entered.is_set)
    if not entered_audit:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
    assert entered_audit, "emergency-stop decision never reached the audit boundary"

    assert authorizations.revocations, "audit backpressure delayed emergency revocation"
    assert authorizations.published == []

    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick


async def test_emergency_stop_cannot_be_turned_into_nonzero_authority_by_safety_output(
    contract: Any,
) -> None:
    emergency = make_intent(contract, emergency=True)
    malicious_nonzero = make_authorizations(intent_id=emergency.id)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        emergency.id,
        2,
        malicious_nonzero,
    )
    kernel, _, authorizations, audit, _ = make_kernel(
        contract,
        intent=emergency,
        observations=complete_observations(),
        decision=decision,
    )

    await kernel.tick()

    assert authorizations.published == []
    assert authorizations.revocations
    assert audit.events == [decision]


async def test_emergency_stop_revocation_survives_audit_failure(contract: Any) -> None:
    emergency = make_intent(contract, emergency=True)
    decision = DecisionRecord(
        contract.DecisionStatus.REVOKED,
        emergency.id,
        2,
        (),
        ("emergency_stop",),
    )
    kernel, history, authorizations, _, _ = make_kernel(
        contract,
        intent=emergency,
        observations=complete_observations(),
        decision=decision,
        audit_error=OSError("audit unavailable"),
    )

    with pytest.raises(OSError, match="audit unavailable"):
        await kernel.tick()

    assert authorizations.published == []
    assert authorizations.revocations
    assert history.index("revoke") < history.index("audit")


async def test_publish_failure_revokes_and_propagates(contract: Any) -> None:
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, history, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        publish_error=OSError("authorization store unavailable"),
    )

    with pytest.raises(OSError, match="authorization store unavailable"):
        await kernel.tick()

    assert audit.events == [decision]
    assert authorizations.published == []
    assert history[-2:] == ["publish", "revoke"]


async def test_cancellation_during_publication_revokes_the_candidate_batch(
    contract: Any,
) -> None:
    publish_gate = asyncio.Event()
    intent = make_intent(contract)
    decision = DecisionRecord(
        contract.DecisionStatus.AUTHORIZED,
        intent.id,
        1,
        make_authorizations(),
    )
    kernel, history, authorizations, audit, _ = make_kernel(
        contract,
        intent=intent,
        observations=complete_observations(),
        decision=decision,
        publish_gate=publish_gate,
    )

    tick = asyncio.create_task(kernel.tick())
    entered_publish = await settle_until(authorizations.publish_entered.is_set)
    if not entered_publish:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
    assert entered_publish, "control cycle never reached authorization publication"
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick

    assert audit.events == [decision]
    assert authorizations.published == []
    assert authorizations.revocations
    assert history[-2:] == ["publish", "revoke"]
