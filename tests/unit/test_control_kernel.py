"""Executable S0 contract for one control-kernel authorization cycle."""

from __future__ import annotations

import asyncio
import importlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

UNITS = frozenset({"lhs", "mid", "rhs"})


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return datetime(2026, 8, 21, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Intent:
    id: str
    source: Any
    unit_ids: frozenset[str]
    direction: Any
    watts: int
    accepted_at_mono: float = 99.0
    acceptance_revision: int = 17
    expires_at_mono: float = 110.0
    actor_identity: str = "operator-1"


@dataclass(frozen=True)
class Observation:
    unit_id: str
    connection_epoch: int = 4
    sequence: int = 10
    captured_at_mono: float = 99.9


@dataclass(frozen=True)
class Proposal:
    unit_id: str
    direction: Any
    watts: int
    intent_id: str
    intent_expires_at_mono: float


@dataclass(frozen=True)
class Setpoint:
    unit_id: str
    direction: Any
    watts: int
    intent_id: str
    authorization_expires_at_mono: float = 101.0
    reactive_vars: int = 0


@dataclass(frozen=True)
class Decision:
    status: Any
    setpoints: tuple[Setpoint, ...]
    reason_codes: tuple[str, ...] = ("safety_checks_passed",)


class Intents:
    def __init__(self, intent: Intent, history: list[str]) -> None:
        self.intent, self.history = intent, history

    async def active(self, now: float) -> tuple[Intent, ...]:
        assert now == 100.0
        self.history.append("intents")
        return (self.intent,)


class Observations:
    def __init__(self, current: dict[str, Any], previous: dict[str, Any], history: list[str]):
        self.current, self.previous, self.history = current, previous, history

    async def all_latest(self) -> dict[str, Any]:
        self.history.append("current")
        return dict(self.current)

    async def all_previous(self) -> dict[str, Any]:
        self.history.append("previous")
        return dict(self.previous)


class Authorizations:
    def __init__(self, history: list[str], error: BaseException | None = None, gate=None):
        self.history, self.error, self.gate = history, error, gate
        self.entered = asyncio.Event()
        self.published: list[Any] = []
        self.revocations: list[tuple[Any, str]] = []

    async def publish(self, batch: Any) -> None:
        self.history.append("publish")
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        self.published.append(batch)

    async def revoke(self, unit_ids=None, *, reason: str, **_: Any) -> None:
        self.history.append("revoke")
        self.revocations.append((unit_ids, reason))


class Audit:
    def __init__(self, history: list[str], error=None, after=None, gate=None):
        self.history, self.error, self.after, self.gate = history, error, after, gate
        self.entered = asyncio.Event()
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.history.append("audit")
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        self.events.append(event)
        if self.after:
            self.after()


class Arbiter:
    def __init__(self, intent: Intent, history: list[str]):
        self.intent, self.history = intent, history

    def select(self, intents: tuple[Intent, ...], now: float) -> Intent:
        assert intents == (self.intent,) and now == 100.0
        self.history.append("select")
        return self.intent


class Allocator:
    def __init__(self, output: tuple[Proposal, ...], history: list[str]):
        self.output, self.history, self.calls = output, history, []

    def allocate(self, intent: Any, observations: Any, policy: Any) -> Any:
        self.history.append("allocate")
        self.calls.append((intent, observations, policy))
        return self.output


class Safety:
    def __init__(self, output: Decision | BaseException, history: list[str]):
        self.output, self.history, self.calls = output, history, []

    def evaluate(self, proposals, current, previous, policy, now) -> Decision:
        self.history.append("safety")
        self.calls.append((proposals, current, previous, policy, now))
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


@pytest.fixture
def api() -> SimpleNamespace:
    kernel = importlib.import_module("energypod.application.control_kernel")
    generation = importlib.import_module("energypod.application.generation")
    audit = importlib.import_module("energypod.application.audit")
    domain = importlib.import_module("energypod.domain")
    auth = importlib.import_module("energypod.domain.authorization")
    return SimpleNamespace(
        ControlKernel=kernel.ControlKernel,
        AuthorityGenerationCoordinator=generation.AuthorityGenerationCoordinator,
        AuditEventFactory=audit.AuditEventFactory,
        AuthorizationBatch=auth.AuthorizationBatch,
        AuthorizedSetpoint=auth.AuthorizedSetpoint,
        DecisionStatus=domain.DecisionStatus,
        Direction=domain.Direction,
        IntentSource=domain.IntentSource,
    )


def deterministic_audit_factory(api: Any, clock: Any) -> Any:
    """Canonical factory with every nondeterministic input pinned by the test."""
    counter = itertools.count(1)
    return api.AuditEventFactory(
        process_instance_id="kernel-contract-test",
        process_origin_mono=0.0,
        wall_now=clock.wall_now,
        event_id_factory=lambda: f"kernel-event-{next(counter):04d}",
    )


def intent(api: Any, *, emergency=False, units=UNITS) -> Intent:
    return Intent(
        "stop-1" if emergency else "intent-1",
        api.IntentSource.EMERGENCY_STOP if emergency else api.IntentSource.MANUAL,
        units,
        api.Direction.IDLE if emergency else api.Direction.DISCHARGE,
        0 if emergency else 900,
    )


def observation_pairs(units=UNITS):
    return (
        {unit: Observation(unit) for unit in units},
        {unit: Observation(unit, sequence=9, captured_at_mono=99.0) for unit in units},
    )


def proposals_for(value: Intent):
    watts = 0 if value.watts == 0 else value.watts // len(value.unit_ids)
    return tuple(
        Proposal(unit, value.direction, watts, value.id, value.expires_at_mono)
        for unit in sorted(value.unit_ids)
    )


def decision_for(api: Any, value: Intent, units=None, status=None):
    selected = value.unit_ids if units is None else units
    watts = 0 if value.watts == 0 else value.watts // len(selected)
    return Decision(
        status
        or (
            api.DecisionStatus.REVOKED
            if value.source is api.IntentSource.EMERGENCY_STOP
            else api.DecisionStatus.AUTHORIZED
        ),
        tuple(Setpoint(unit, value.direction, watts, value.id) for unit in sorted(selected)),
        ("emergency_stop",) if value.watts == 0 else ("safety_checks_passed",),
    )


def make_kernel(
    api: Any,
    value: Intent,
    output: Decision | BaseException,
    *,
    current=None,
    previous=None,
    clock=None,
    audit_error=None,
    after_audit=None,
    audit_gate=None,
    publish_error=None,
    publish_gate=None,
):
    history: list[str] = []
    clock = clock or Clock()
    default_current, default_previous = observation_pairs(value.unit_ids)
    current = default_current if current is None else current
    previous = default_previous if previous is None else previous
    allocator = Allocator(proposals_for(value), history)
    safety = Safety(output, history)
    authorizations = Authorizations(history, publish_error, publish_gate)
    audit = Audit(history, audit_error, after_audit, audit_gate)
    kernel = api.ControlKernel(
        clock=clock,
        unit_ids=UNITS,
        intents=Intents(value, history),
        observations=Observations(current, previous, history),
        authorizations=authorizations,
        audit=audit,
        arbiter=Arbiter(value, history),
        allocator=allocator,
        safety=safety,
        policy=SimpleNamespace(version="policy-5", max_telemetry_age_s=2.0),
        generation_coordinator=api.AuthorityGenerationCoordinator(),
        configuration_version=12,
        audit_event_factory=deterministic_audit_factory(api, clock),
    )
    return kernel, history, authorizations, audit, allocator, safety


async def settle(predicate: Callable[[], bool]) -> bool:
    for _ in range(50):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def assert_batch(api: Any, batch: Any, value: Intent) -> None:
    assert isinstance(batch, api.AuthorizationBatch)
    assert batch.generation == 0 and batch.cycle_id
    assert {cap.unit_id for cap in batch.authorizations} == value.unit_ids
    assert len({cap.decision_id for cap in batch.authorizations}) == 1
    for cap in batch.authorizations:
        assert isinstance(cap, api.AuthorizedSetpoint)
        assert (cap.cycle_id, cap.generation) == (batch.cycle_id, batch.generation)
        assert (cap.intent_id, cap.intent_revision) == (value.id, value.acceptance_revision)
        assert cap.direction is value.direction
        assert (cap.connection_epoch, cap.observation_sequence) == (4, 10)
        assert (cap.issued_at_mono, cap.not_before_mono, cap.expires_at_mono) == (
            100.0,
            100.0,
            101.0,
        )
        assert cap.maximum_observation_age_s == 2.0
        assert (cap.policy_version, cap.configuration_version) == ("policy-5", 12)


async def test_allocates_evaluates_mints_audits_then_atomically_publishes(api: Any):
    value = intent(api)
    outcome = decision_for(api, value)
    kernel, history, auth, audit, allocator, safety = make_kernel(api, value, outcome)
    assert await kernel.tick() is outcome
    assert history == [
        "intents",
        "current",
        "previous",
        "select",
        "allocate",
        "safety",
        "audit",
        "publish",
    ]
    assert safety.calls[0][0] == allocator.output
    assert len(auth.published) == 1 and audit.events
    assert_batch(api, auth.published[0], value)


@pytest.mark.parametrize("elapsed", [1.0, 1.001])
async def test_deadline_is_rechecked_after_durable_audit(api: Any, elapsed: float):
    value, clock = intent(api), Clock()
    kernel, _, auth, audit, _, _ = make_kernel(
        api,
        value,
        decision_for(api, value),
        clock=clock,
        after_audit=lambda: clock.advance(elapsed),
    )
    await kernel.tick()
    assert audit.events and not auth.published
    assert any(reason == "control_cycle_deadline_expired" for _, reason in auth.revocations)


async def test_exact_current_epoch_and_sequence_are_minted(api: Any):
    value = intent(api)
    current, previous = observation_pairs()
    current["mid"] = Observation("mid", connection_epoch=41, sequence=73)
    kernel, _, auth, _, _, _ = make_kernel(
        api, value, decision_for(api, value), current=current, previous=previous
    )
    await kernel.tick()
    cap = next(item for item in auth.published[0].authorizations if item.unit_id == "mid")
    assert (cap.connection_epoch, cap.observation_sequence) == (41, 73)


@pytest.mark.parametrize("missing", ["current", "previous"])
async def test_partial_selected_fleet_fails_closed(api: Any, missing: str):
    value = intent(api)
    current, previous = observation_pairs()
    (current if missing == "current" else previous).pop("lhs")
    output = decision_for(api, value, units=frozenset({"mid", "rhs"}))
    kernel, _, auth, audit, _, _ = make_kernel(
        api, value, output, current=current, previous=previous
    )
    await kernel.tick()
    assert audit.events and not auth.published and auth.revocations


async def test_explicit_selected_subset_is_complete(api: Any):
    value = intent(api, units=frozenset({"mid", "rhs"}))
    kernel, _, auth, _, _, _ = make_kernel(api, value, decision_for(api, value))
    await kernel.tick()
    assert_batch(api, auth.published[0], value)


async def test_partially_eligible_fleet_mints_authority_only_for_participating_units(
    api: Any,
) -> None:
    """2026-08-23 live halt and rejection: a multi-unit intent with units at
    their headroom limits produces zero-watt proposals for those units. The
    kernel must accept them, mint authority ONLY for the participating unit —
    a zero-watt setpoint is explicit non-participation and never carries
    authority — and never raise "allocator output does not match the selected
    intent" nor blanket-reject the deliverable units."""
    from dataclasses import replace as _replace

    value = intent(api, units=UNITS)
    full_outcome = decision_for(api, value)
    proposals = proposals_for(value)
    eligible_only = (
        _replace(proposals[0], watts=0),
        _replace(proposals[1], watts=0),
        proposals[2],
    )
    # Safety echoes the allocation: zero watts for the two ineligible units.
    outcome = _replace(
        full_outcome,
        setpoints=(
            _replace(full_outcome.setpoints[0], watts=0),
            _replace(full_outcome.setpoints[1], watts=0),
            full_outcome.setpoints[2],
        ),
    )
    kernel, history, auth, audit, allocator, safety = make_kernel(api, value, outcome)
    allocator.output = eligible_only
    decision = await kernel.tick()
    assert history == [
        "intents",
        "current",
        "previous",
        "select",
        "allocate",
        "safety",
        "audit",
        "publish",
    ]
    assert decision is outcome
    assert audit.events
    # Authority is minted for the participating unit ONLY: the zero-watt
    # setpoints stay in the audited decision but carry no capability.
    (batch,) = auth.published
    participants = {cap.unit_id for cap in batch.authorizations}
    assert participants == {"rhs"}
    authorized = next(cap for cap in batch.authorizations if cap.unit_id == "rhs")
    assert authorized.watts == outcome.setpoints[2].watts
    assert authorized.direction is value.direction


async def test_single_unit_intent_with_zero_allocation_is_rejected_not_crashing(
    api: Any,
) -> None:
    """2026-08-23 live fleet halt (SUPERVISOR FAILURE, ValueError at the
    matcher): a single-unit charge into lhs while its BMS dynamic charge
    limit was 0 W produced a one-proposal ALL-zero allocation, and the
    matcher's all-zero guard raised, ending the fleet task. An allocation
    with no deliverable watts is legitimate input: the kernel must accept
    it, audit the (rejected) decision, revoke, and keep ticking."""
    from dataclasses import replace as _replace

    value = intent(api, units=frozenset({"lhs"}))
    proposals = proposals_for(value)
    all_zero = (_replace(proposals[0], watts=0),)
    outcome = Decision(
        api.DecisionStatus.REJECTED,
        (Setpoint("lhs", value.direction, 0, value.id),),
        ("zero_dynamic_capability",),
    )
    kernel, history, auth, audit, allocator, safety = make_kernel(api, value, outcome)
    allocator.output = all_zero
    decision = await kernel.tick()

    assert decision is outcome
    assert history == [
        "intents",
        "current",
        "previous",
        "select",
        "allocate",
        "safety",
        "audit",
        "revoke",
    ]
    assert auth.published == []
    assert audit.events
    # The fleet survives: the next tick also completes without raising.
    decision_two = await kernel.tick()
    assert decision_two is outcome


@pytest.mark.parametrize("shape", ["missing", "duplicate", "extra"])
async def test_missing_duplicate_or_extra_setpoint_rejects_whole_batch(api: Any, shape: str):
    value = intent(api)
    points = list(decision_for(api, value).setpoints)
    if shape == "missing":
        points = points[:-1]
    elif shape == "duplicate":
        points.append(points[0])
    else:
        points.append(Setpoint("intruder", value.direction, 1, value.id))
    kernel, _, auth, audit, _, _ = make_kernel(
        api, value, Decision(api.DecisionStatus.AUTHORIZED, tuple(points))
    )
    await kernel.tick()
    assert audit.events and not auth.published and auth.revocations


async def test_all_safety_reasons_are_audited_without_authority(api: Any):
    value = intent(api)
    output = Decision(
        api.DecisionStatus.REJECTED,
        tuple(Setpoint(unit, api.Direction.IDLE, 0, value.id) for unit in sorted(UNITS)),
        ("blocking_fault", "soc_jump", "telemetry_stale"),
    )
    kernel, _, auth, audit, _, _ = make_kernel(api, value, output)
    assert await kernel.tick() is output
    assert audit.events and not auth.published and auth.revocations


async def test_audit_failure_revokes_and_propagates(api: Any):
    value = intent(api)
    kernel, history, auth, _, _, _ = make_kernel(
        api, value, decision_for(api, value), audit_error=OSError("audit full")
    )
    with pytest.raises(OSError, match="audit full"):
        await kernel.tick()
    assert not auth.published and history[-2:] == ["audit", "revoke"]


async def test_cancellation_during_audit_revokes(api: Any):
    gate, value = asyncio.Event(), intent(api)
    kernel, _, auth, audit, _, _ = make_kernel(
        api, value, decision_for(api, value), audit_gate=gate
    )
    task = asyncio.create_task(kernel.tick())
    assert await settle(audit.entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not auth.published and auth.revocations


@pytest.mark.parametrize("mode", ["cancel", "fail"])
async def test_publication_cancellation_or_failure_revokes(api: Any, mode: str):
    value, gate = intent(api), asyncio.Event()
    kwargs = (
        {"publish_gate": gate} if mode == "cancel" else {"publish_error": OSError("store down")}
    )
    kernel, _, auth, audit, _, _ = make_kernel(api, value, decision_for(api, value), **kwargs)
    task = asyncio.create_task(kernel.tick())
    if mode == "cancel":
        assert await settle(auth.entered.is_set)
        task.cancel()
        expected = asyncio.CancelledError
    else:
        expected = OSError
    with pytest.raises(expected):
        await task
    assert audit.events and not auth.published and auth.revocations


async def test_emergency_revokes_before_blocking_audit(api: Any):
    gate, value = asyncio.Event(), intent(api, emergency=True)
    kernel, history, auth, audit, _, _ = make_kernel(
        api, value, decision_for(api, value), audit_gate=gate
    )
    task = asyncio.create_task(kernel.tick())
    assert await settle(audit.entered.is_set)
    assert auth.revocations
    assert history.index("revoke") < history.index("audit")
    assert not auth.published
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_emergency_cannot_be_promoted_by_malicious_safety(api: Any):
    value = intent(api, emergency=True)
    malicious = Decision(
        api.DecisionStatus.AUTHORIZED,
        tuple(Setpoint(unit, api.Direction.DISCHARGE, 300, value.id) for unit in sorted(UNITS)),
    )
    kernel, history, auth, audit, _, _ = make_kernel(api, value, malicious)
    await kernel.tick()
    assert history.index("revoke") < history.index("audit")
    assert audit.events and not auth.published


async def test_evaluation_failure_revokes_and_propagates(api: Any):
    value = intent(api)
    kernel, _, auth, audit, _, _ = make_kernel(api, value, RuntimeError("control died"))
    with pytest.raises(RuntimeError, match="control died"):
        await kernel.tick()
    assert not audit.events and not auth.published and auth.revocations
