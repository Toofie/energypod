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
    # Per-unit watt targets (2026-08-23 operator ruling): each unit's own cap.
    watts_by_unit: dict[str, int] | None = None
    # The production arbiter reads the domain spelling; the fake mirrors it.
    duration_s: float = 20.0

    @property
    def selected_unit_ids(self) -> frozenset[str]:
        return self.unit_ids


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
    def __init__(self, intent: Intent | tuple[Intent, ...], history: list[str]) -> None:
        self.intents: tuple[Intent, ...] = intent if isinstance(intent, tuple) else (intent,)
        self.history = history

    async def active(self, now: float) -> tuple[Intent, ...]:
        assert now == 100.0
        self.history.append("intents")
        return self.intents


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


def single_intent_selection(intent_value: Intent, emergency: bool = False) -> Any:
    """One intent claiming its whole selection -- the arbitration the kernel
    composes for a cycle held by exactly one intent."""
    from energypod.application.arbiter import CycleArbitration

    units = frozenset(intent_value.unit_ids)
    return CycleArbitration(
        ranked=(intent_value,),
        scopes={intent_value.id: units},
        winners={unit_id: intent_value for unit_id in units},
        emergency=intent_value if emergency else None,
    )


def empty_selection() -> Any:
    from energypod.application.arbiter import CycleArbitration

    return CycleArbitration()


class Arbiter:
    def __init__(self, intent: Intent, history: list[str]):
        self.intent, self.history = intent, history

    def arbitrate(self, intents: tuple[Intent, ...], now: float) -> Any:
        assert intents == (self.intent,) and now == 100.0
        self.history.append("select")
        source = getattr(self.intent.source, "value", self.intent.source)
        return single_intent_selection(self.intent, source == "emergency_stop")


class Allocator:
    def __init__(self, output: tuple[Proposal, ...], history: list[str]):
        self.output, self.history, self.calls = output, history, []

    def allocate(
        self,
        intent: Any,
        observations: Any,
        policy: Any,
        now_mono: Any = None,
        unit_ids: Any = None,
    ) -> Any:
        # The allocator port carries the tick's monotonic time (the
        # export bound's freshness input) and the intent's SURVIVING scope
        # (per-unit arbitration); the fake records both without asserting.
        self.history.append("allocate")
        self.calls.append((intent, observations, policy, now_mono, unit_ids))
        return self.output


class MultiIntentAllocator:
    """One allocator call per represented intent, each scoped to its survivors."""

    def __init__(self, proposals_by_intent: dict[str, tuple[Proposal, ...]], history: list[str]):
        self._by_intent = proposals_by_intent
        self.history = history
        self.calls: list[tuple[Any, Any, Any, Any, Any]] = []

    def allocate(
        self,
        intent: Any,
        observations: Any,
        policy: Any,
        now_mono: Any = None,
        unit_ids: Any = None,
    ) -> Any:
        self.history.append("allocate")
        self.calls.append((intent, observations, policy, now_mono, unit_ids))
        return self._by_intent.get(getattr(intent, "id", None), ())


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


@pytest.mark.parametrize("missing", ["current"])
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


async def test_a_boot_composition_with_one_observation_per_unit_mints(api: Any):
    """SYNC_RESILIENCE_AUDIT B3: the first post-restart tick must mint.

    Boot -> first poll -> first tick historically found no previous pair, so
    ``_eligible`` refused to mint and every active proposal was rejected for
    one cycle although the battery was readable and fresh the whole time.  A
    unit's FIRST observation is its own baseline: the pair-derived coherence
    checks are vacuous without a baseline and the batch is minted on the
    current observations alone."""
    value = intent(api)
    current, _previous = observation_pairs(value.unit_ids)
    kernel, _, auth, audit, _, _ = make_kernel(
        api, value, decision_for(api, value), current=current, previous={}
    )
    await kernel.tick()
    assert_batch(api, auth.published[0], value)
    assert audit.events


async def test_a_mixed_boot_mints_for_every_selected_unit(api: Any):
    """A unit still warming up (first observation) does not hold back the
    units that already hold a previous pair, and vice versa: the eligibility
    gate no longer requires a previous observation for ANY selected unit."""
    value = intent(api)
    current, previous = observation_pairs(value.unit_ids)
    previous.pop("lhs")  # lhs holds its first observation; mid/rhs hold pairs
    kernel, _, auth, _, _, _ = make_kernel(
        api, value, decision_for(api, value), current=current, previous=previous
    )
    await kernel.tick()
    assert_batch(api, auth.published[0], value)


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


def _per_unit_intent(api: Any, targets: dict[str, int]) -> Intent:
    """One MANUAL intent whose per-unit targets sum to its fleet watts."""
    return Intent(
        "intent-per-unit",
        api.IntentSource.MANUAL,
        frozenset(targets),
        api.Direction.DISCHARGE,
        sum(targets.values()),
        watts_by_unit=dict(targets),
    )


async def test_per_unit_intent_authorizes_each_unit_within_its_own_target(api: Any) -> None:
    """A watts_by_unit intent mints per-unit authority at the unit's target.

    The matcher must accept proposals at or below each unit's own target and
    the published batch must carry each unit's target as its authorized watts
    (the 2026-08-23 operator ruling: each setting is that battery's request).
    """
    value = _per_unit_intent(api, {"lhs": 300, "mid": 100, "rhs": 200})
    at_targets = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("lhs", value.direction, 300, value.id),
            Setpoint("mid", value.direction, 100, value.id),
            Setpoint("rhs", value.direction, 200, value.id),
        ),
    )
    kernel, history, auth, audit, allocator, _safety = make_kernel(api, value, at_targets)
    allocator.output = tuple(
        Proposal(unit, value.direction, watts, value.id, value.expires_at_mono)
        for unit, watts in (("lhs", 300), ("mid", 100), ("rhs", 200))
    )
    decision = await kernel.tick()
    assert decision is at_targets
    assert history[-2:] == ["audit", "publish"]
    (batch,) = auth.published
    authorized = {cap.unit_id: cap.watts for cap in batch.authorizations}
    assert authorized == {"lhs": 300, "mid": 100, "rhs": 200}


async def test_proposal_exceeding_a_units_target_is_rejected_before_safety(api: Any) -> None:
    """The matcher enforces the per-unit cap even when the fleet total fits.

    mid's proposal of 250 W exceeds its 200 W target while the 750 W total also
    breaks the fleet bound; the 500 W-into-500 W variant below isolates the
    per-unit rule: the fleet total EQUALS the intent's watts, only mid's own
    target is exceeded, and the confused allocator output must still be
    rejected before safety evaluation mints anything from it.
    """
    value = _per_unit_intent(api, {"lhs": 150, "mid": 100, "rhs": 250})
    over_total = tuple(
        Proposal(unit, value.direction, watts, value.id, value.expires_at_mono)
        for unit, watts in (("lhs", 300), ("mid", 150), ("rhs", 300))
    )
    over_one_target = tuple(
        Proposal(unit, value.direction, watts, value.id, value.expires_at_mono)
        for unit, watts in (("lhs", 150), ("mid", 250), ("rhs", 100))
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        tuple(
            Setpoint(unit, value.direction, watts, value.id)
            for unit, watts in (("lhs", 150), ("mid", 250), ("rhs", 100))
        ),
    )
    for proposals in (over_total, over_one_target):
        kernel, _history, auth, audit, allocator, _safety = make_kernel(api, value, outcome)
        allocator.output = proposals
        with pytest.raises(ValueError, match="allocator output does not match"):
            await kernel.tick()
        assert auth.published == []
        assert audit.events == []
        assert auth.revocations


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


# --- the 2026-08-23 publish-fence generation desync (live incident) -------------
#
# Live evidence (~23:21Z, post-emergency-stop-acknowledgement): a fresh intent
# after a prior intent's TTL expiry arbitrated `authorized` every cycle while
# its watts never dispatched.  Each cycle emitted TWO audit rows sharing one
# cycle_id -- the granted record, then a zero-authorized fenced record -- so
# the kernel minted at a generation the repository had already fenced through.
# Every kernel revocation bumps the repository's permanent fence to the last
# published generation; nothing reconciled the coordinator, so minting at the
# current epoch was fenced forever (only a restart, which resets generation
# state, escaped it).  These contracts pin the reconciliation.


class SwappableIntents:
    """The intent port with a mutable active set, for multi-phase sequences."""

    def __init__(self) -> None:
        self.active_intents: tuple[Any, ...] = ()

    async def active(self, now: float) -> tuple[Any, ...]:
        del now
        return self.active_intents


class SelectingArbiter:
    """The arbiter port that maps an empty active set to no selection."""

    def __init__(self, intent: Intent, history: list[str]) -> None:
        self.intent, self.history = intent, history

    def arbitrate(self, intents: tuple[Intent, ...], now: float) -> Any:
        del now
        self.history.append("select")
        if not intents:
            return empty_selection()
        source = getattr(self.intent.source, "value", self.intent.source)
        return single_intent_selection(self.intent, source == "emergency_stop")


class StoreBackedAuthorizations:
    """The real repository's permanent publish fence behind the awaitable port."""

    def __init__(self) -> None:
        memory = importlib.import_module("energypod.adapters.persistence.memory")
        authorization = importlib.import_module("energypod.domain.authorization")
        self._stale_error = authorization.StaleGenerationError
        self.store = memory.InMemoryAuthorizationRepository()
        self.published: list[Any] = []
        self.fenced: list[Any] = []

    async def publish(self, batch: Any) -> None:
        try:
            self.store.publish(batch)
        except self._stale_error:
            self.fenced.append(batch)
            raise
        self.published.append(batch)

    async def revoke(self, unit_ids=None, *, reason: str, **_: Any) -> None:
        self.store.revoke(unit_ids=unit_ids, reason=reason)

    async def revoked_through(self, unit_ids: Any) -> int:
        return self.store.revoked_through(unit_ids)


def make_fenced_kernel(
    api: Any,
    value: Intent,
    output: Decision | BaseException,
    *,
    clock: Clock | None = None,
    after_audit: Any = None,
    authorizations: StoreBackedAuthorizations | None = None,
    coordinator: Any = None,
):
    """One kernel over the real repository fence and a real coordinator.

    ``authorizations`` and ``coordinator`` may be injected so a multi-phase
    sequence (or several kernels) can share exactly the live pair of
    generation state and permanent fence.
    """
    history: list[str] = []
    clock = clock or Clock()
    current, previous = observation_pairs(value.unit_ids)
    authorizations = authorizations or StoreBackedAuthorizations()
    coordinator = coordinator or api.AuthorityGenerationCoordinator()
    intents = SwappableIntents()
    intents.active_intents = (value,)
    audit = Audit(history, None, after_audit)
    kernel = api.ControlKernel(
        clock=clock,
        unit_ids=UNITS,
        intents=intents,
        observations=Observations(current, previous, history),
        authorizations=authorizations,
        audit=audit,
        arbiter=SelectingArbiter(value, history),
        allocator=Allocator(proposals_for(value), history),
        safety=Safety(output, history),
        policy=SimpleNamespace(version="policy-5", max_telemetry_age_s=2.0),
        generation_coordinator=coordinator,
        configuration_version=12,
        audit_event_factory=deterministic_audit_factory(api, clock),
    )
    return kernel, intents, authorizations, audit, coordinator


async def test_kernel_never_mints_at_a_generation_the_repository_will_fence(api: Any):
    """The live sequence: publish, TTL-lapse revocation, then a fresh intent.

    The fresh intent's batch must actually PUBLISH -- authority lands, exactly
    one audit row carries its cycle -- and the epoch the kernel consults must
    stand strictly beyond the repository's permanent fence after the
    revocation, or every later cycle is fenced with no error and no dispatch.
    """
    value = intent(api)
    kernel, intents, authorizations, audit, coordinator = make_fenced_kernel(
        api, value, decision_for(api, value)
    )

    # 1. The prior intent publishes at the coordinator's epoch.
    await kernel.tick()
    assert [batch.generation for batch in authorizations.published] == [0]

    # 2. Its TTL lapses; the fail-closed tick revokes through the repository,
    #    which fences that published generation permanently.  The coordinator
    #    epoch the next tick mints at must reconcile past that fence.
    intents.active_intents = ()
    assert await kernel.tick() is None
    fence = await authorizations.revoked_through(UNITS)
    assert fence == 0, "the lapsed-intent revocation must fence the published generation"
    assert (await coordinator.snapshot()).epoch > fence

    # 3. A fresh intent must publish on its FIRST cycle, never at a fenced
    #    generation: the batch lands, nothing is fenced, and its cycle carries
    #    exactly one audit row (not the granted-plus-fenced pair the live
    #    incident emitted under one cycle_id).
    intents.active_intents = (value,)
    decision = await kernel.tick()
    assert decision is not None and decision.status is api.DecisionStatus.AUTHORIZED
    assert authorizations.fenced == []
    assert [batch.generation for batch in authorizations.published] == [0, fence + 1]
    assert len(audit.events) == 2
    assert len({event.cycle_id for event in audit.events}) == 2
    assert audit.events[-1].authorized_active_w != 0


@pytest.mark.parametrize(
    "scenario",
    ["no_active_intent", "decision_not_authorized", "deadline_expired", "emergency_stop"],
)
async def test_every_kernel_revocation_reconciles_the_epoch_past_the_repository_fence(
    api: Any, scenario: str
):
    """The direct invariant: after ANY kernel revocation path, the coordinator
    epoch is strictly beyond the repository's permanent revoked-through fence."""
    shared_store = StoreBackedAuthorizations()
    shared_coordinator = api.AuthorityGenerationCoordinator()
    prior = intent(api)
    first, *_ = make_fenced_kernel(
        api,
        prior,
        decision_for(api, prior),
        authorizations=shared_store,
        coordinator=shared_coordinator,
    )
    await first.tick()
    assert shared_store.published, "every scenario starts from a published generation"

    if scenario == "no_active_intent":
        second, intents, *_ = make_fenced_kernel(
            api,
            prior,
            decision_for(api, prior),
            authorizations=shared_store,
            coordinator=shared_coordinator,
        )
        intents.active_intents = ()
    elif scenario == "decision_not_authorized":
        rejected = Decision(
            api.DecisionStatus.REJECTED,
            decision_for(api, prior).setpoints,
            ("blocking_fault",),
        )
        second, *_ = make_fenced_kernel(
            api, prior, rejected, authorizations=shared_store, coordinator=shared_coordinator
        )
    elif scenario == "deadline_expired":
        clock = Clock()
        second, *_ = make_fenced_kernel(
            api,
            prior,
            decision_for(api, prior),
            clock=clock,
            after_audit=lambda: clock.advance(1.0),
            authorizations=shared_store,
            coordinator=shared_coordinator,
        )
    else:
        stop = intent(api, emergency=True)
        second, intents, *_ = make_fenced_kernel(
            api,
            stop,
            decision_for(api, stop),
            authorizations=shared_store,
            coordinator=shared_coordinator,
        )
        intents.active_intents = (stop,)

    await second.tick()
    fence = await shared_store.revoked_through(UNITS)
    assert fence == 0, f"{scenario} must fence the published generation"
    epoch = (await shared_coordinator.snapshot()).epoch
    assert epoch > fence, (
        f"{scenario} left the coordinator epoch {epoch} at or below the repository fence "
        f"{fence}: the next tick would mint at a fenced generation forever"
    )


# --- concurrent per-unit composition (2026-08-24 operator requirement) --------
#
# One tick now composes EVERY per-unit winner into ONE cycle: the allocator
# runs once per represented intent over its SURVIVING scope, the matcher binds
# each proposal to its unit's winning intent (identity AND direction), one
# cycle_id/decision_id and one audit row carry the per-unit breakdown with
# each unit's own direction, and one AuthorizationBatch carries per-unit
# capabilities whose intent/direction are their own unit's winner's.


def _charge_intent(api: Any, intent_id: str, units: frozenset[str], watts: int) -> Intent:
    return Intent(intent_id, api.IntentSource.MANUAL, units, api.Direction.CHARGE, watts)


def _discharge_intent(api: Any, intent_id: str, units: frozenset[str], watts: int) -> Intent:
    return Intent(
        intent_id,
        api.IntentSource.AGENT,
        units,
        api.Direction.DISCHARGE,
        watts,
        actor_identity="agent:automation",
    )


def _make_composed_kernel(
    api: Any,
    *,
    intents_in_cycle: tuple[Intent, ...],
    arbiter_value: Any,
    allocator: Any,
    output: Decision | BaseException,
    **kwargs: Any,
) -> tuple[Any, list[str], Authorizations, Audit]:
    history: list[str] = []
    clock = kwargs.pop("clock", None) or Clock()
    selected = frozenset(arbiter_value.winners)
    current, previous = observation_pairs(selected)
    for key in ("current", "previous"):
        if key in kwargs:
            current = kwargs.pop("current")
        if key in kwargs:
            previous = kwargs.pop("previous")
    allocator.history = history
    authorizations = Authorizations(history)
    audit = Audit(history)
    kernel = api.ControlKernel(
        clock=clock,
        unit_ids=UNITS,
        intents=Intents(intents_in_cycle, history),
        observations=Observations(current, previous, history),
        authorizations=authorizations,
        audit=audit,
        arbiter=_StaticArbiter(arbiter_value, history),
        allocator=allocator,
        safety=Safety(output, history),
        policy=SimpleNamespace(version="policy-5", max_telemetry_age_s=2.0),
        generation_coordinator=api.AuthorityGenerationCoordinator(),
        configuration_version=12,
        audit_event_factory=deterministic_audit_factory(api, clock),
    )
    return kernel, history, authorizations, audit


class _StaticArbiter:
    """The arbiter port returning one pre-composed selection."""

    def __init__(self, selection: Any, history: list[str]) -> None:
        self.selection, self.history = selection, history

    def arbitrate(self, intents: tuple[Intent, ...], now: float) -> Any:
        del intents, now
        self.history.append("select")
        return self.selection


def _real_selection(intents_in_cycle: tuple[Intent, ...]) -> Any:
    """The production arbiter's composition over the scripted intents."""
    from energypod.application.arbiter import IntentArbiter

    return IntentArbiter().arbitrate(intents_in_cycle, 100.0)


async def test_two_disjoint_intents_compose_into_one_cycle_and_batch(api: Any) -> None:
    """The operator's exact scenario: MID charge 2,000 W (manual) and RHS
    discharge 1,000 W (agent) run in ONE cycle -- one batch, one cycle id, one
    decision id, per-unit directions and per-intent attribution."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    selection = _real_selection((mid, rhs))
    assert selection.emergency is None and len(selection.ranked) == 2
    allocator = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "mid-charge", 110.0),),
            "rhs-discharge": (
                Proposal("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("mid", api.Direction.CHARGE, 2_000, "mid-charge"),
            Setpoint("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge"),
        ),
    )
    kernel, history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(mid, rhs),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )

    decision = await kernel.tick()

    assert decision is outcome
    assert history == [
        "intents",
        "current",
        "previous",
        "select",
        "allocate",
        "allocate",
        "safety",
        "audit",
        "publish",
    ]
    # The allocator ran ONCE PER REPRESENTED INTENT, each scoped to its own
    # surviving units.
    assert [call[0].id for call in allocator.calls] == ["mid-charge", "rhs-discharge"]
    assert [call[4] for call in allocator.calls] == [frozenset({"mid"}), frozenset({"rhs"})]
    (batch,) = auth.published
    by_unit = {cap.unit_id: cap for cap in batch.authorizations}
    assert set(by_unit) == {"mid", "rhs"}
    assert by_unit["mid"].direction is api.Direction.CHARGE
    assert by_unit["mid"].watts == 2_000
    assert (by_unit["mid"].intent_id, by_unit["mid"].intent_revision) == (
        "mid-charge",
        17,
    )
    assert by_unit["rhs"].direction is api.Direction.DISCHARGE
    assert by_unit["rhs"].watts == 1_000
    assert by_unit["rhs"].intent_id == "rhs-discharge"
    assert {cap.cycle_id for cap in batch.authorizations} == {batch.cycle_id}
    assert len({cap.decision_id for cap in batch.authorizations}) == 1
    # One audit row carries BOTH units with their own directions and watts.
    (event,) = audit.events
    assert dict(event.directions_by_unit) == {"mid": "charge", "rhs": "discharge"}
    assert dict(event.authorized_watts_by_unit) == {"mid": 2_000, "rhs": 1_000}
    assert event.requested_active_w == -2_000 + 1_000
    assert event.authorized_active_w == -2_000 + 1_000
    assert event.event_type == "control_decision"


async def test_composed_row_stays_cycle_level_and_joins_its_principals(api: Any) -> None:
    """A row composed from several intents cannot honestly name one intent:
    it correlates to its cycle and joins the represented principals, while a
    single-intent row keeps today's exact attribution."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    selection = _real_selection((mid, rhs))
    allocator = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "mid-charge", 110.0),),
            "rhs-discharge": (
                Proposal("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("mid", api.Direction.CHARGE, 2_000, "mid-charge"),
            Setpoint("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge"),
        ),
    )
    kernel, _history, _auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(mid, rhs),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    await kernel.tick()
    (event,) = audit.events
    assert event.intent_id is None
    assert event.unit_id is None
    assert event.correlation_id == f"cycle:{event.cycle_id}"
    assert event.principal == ",".join(sorted({mid.actor_identity, rhs.actor_identity}))
    assert event.source is mid.source

    # The single-intent twin keeps today's attribution byte-for-byte.
    solo = _charge_intent(api, "solo", frozenset({"mid"}), 2_000)
    solo_selection = _real_selection((solo,))
    solo_allocator = MultiIntentAllocator(
        {"solo": (Proposal("mid", api.Direction.CHARGE, 2_000, "solo", 110.0),)}, []
    )
    solo_outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (Setpoint("mid", api.Direction.CHARGE, 2_000, "solo"),),
    )
    kernel2, _h2, _a2, audit2 = _make_composed_kernel(
        api,
        intents_in_cycle=(solo,),
        arbiter_value=solo_selection,
        allocator=solo_allocator,
        output=solo_outcome,
    )
    await kernel2.tick()
    (solo_event,) = audit2.events
    assert solo_event.intent_id == "solo"
    assert solo_event.unit_id == "mid"
    assert solo_event.correlation_id == "intent:solo:revision:17"
    assert solo_event.principal == solo.actor_identity
    assert solo_event.directions_by_unit is None


async def test_matcher_binds_each_proposal_to_its_units_winning_intent(api: Any) -> None:
    """A proposal whose direction or intent id contradicts its unit's winner
    is rejected before safety evaluation -- per-unit coherence is enforced at
    the kernel boundary, whatever the allocator produced."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    selection = _real_selection((mid, rhs))
    wrong_direction = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "mid-charge", 110.0),),
            "rhs-discharge": (
                # rhs's winner is DISCHARGE; a charge proposal for it is
                # incoherent with the arbitration.
                Proposal("rhs", api.Direction.CHARGE, 1_000, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    wrong_intent = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "rhs-discharge", 110.0),),
            "rhs-discharge": (
                Proposal("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(api.DecisionStatus.AUTHORIZED, ())
    for allocator in (wrong_direction, wrong_intent):
        kernel, _history, auth, audit = _make_composed_kernel(
            api,
            intents_in_cycle=(mid, rhs),
            arbiter_value=selection,
            allocator=allocator,
            output=outcome,
        )
        with pytest.raises(ValueError, match="allocator output does not match"):
            await kernel.tick()
        assert auth.published == []
        assert audit.events == []
        assert auth.revocations


async def test_scope_erosion_allocates_each_intent_over_its_survivors(api: Any) -> None:
    """A manual intent claiming lhs+mid erodes an agent intent claiming mid+rhs
    to just rhs: the kernel allocates each over its OWN surviving scope and
    composes both into one cycle."""
    manual = Intent(
        "manual-wide",
        api.IntentSource.MANUAL,
        frozenset({"lhs", "mid"}),
        api.Direction.CHARGE,
        900,
        watts_by_unit={"lhs": 600, "mid": 300},
    )
    agent = Intent(
        "agent-eroded",
        api.IntentSource.AGENT,
        frozenset({"mid", "rhs"}),
        api.Direction.DISCHARGE,
        500,
    )
    selection = _real_selection((agent, manual))
    assert dict(selection.scopes) == {
        "manual-wide": frozenset({"lhs", "mid"}),
        "agent-eroded": frozenset({"rhs"}),
    }
    allocator = MultiIntentAllocator(
        {
            "manual-wide": (
                Proposal("lhs", api.Direction.CHARGE, 600, "manual-wide", 110.0),
                Proposal("mid", api.Direction.CHARGE, 300, "manual-wide", 110.0),
            ),
            "agent-eroded": (Proposal("rhs", api.Direction.DISCHARGE, 500, "agent-eroded", 110.0),),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("lhs", api.Direction.CHARGE, 600, "manual-wide"),
            Setpoint("mid", api.Direction.CHARGE, 300, "manual-wide"),
            Setpoint("rhs", api.Direction.DISCHARGE, 500, "agent-eroded"),
        ),
    )
    kernel, _history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(manual, agent),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    await kernel.tick()
    assert [call[4] for call in allocator.calls] == [
        frozenset({"lhs", "mid"}),
        frozenset({"rhs"}),
    ]
    (batch,) = auth.published
    by_unit = {cap.unit_id: cap for cap in batch.authorizations}
    assert set(by_unit) == {"lhs", "mid", "rhs"}
    assert by_unit["rhs"].intent_id == "agent-eroded"
    assert by_unit["rhs"].direction is api.Direction.DISCHARGE
    (event,) = audit.events
    assert dict(event.requested_watts_by_unit) == {"lhs": 600, "mid": 300}
    assert dict(event.authorized_watts_by_unit) == {"lhs": 600, "mid": 300, "rhs": 500}
    assert event.requested_active_w == -900 + 500


async def test_idle_winner_rides_along_an_active_composed_cycle(api: Any) -> None:
    """A manual idle intent holding mid to zero while an agent discharges rhs:
    one authorized cycle, mid at zero watts with no capability minted for it."""
    idle = Intent(
        "manual-idle",
        api.IntentSource.MANUAL,
        frozenset({"mid"}),
        api.Direction.IDLE,
        0,
    )
    agent = Intent(
        "agent-discharge",
        api.IntentSource.AGENT,
        frozenset({"mid", "rhs"}),
        api.Direction.DISCHARGE,
        400,
    )
    selection = _real_selection((agent, idle))
    allocator = MultiIntentAllocator(
        {
            "manual-idle": (Proposal("mid", api.Direction.IDLE, 0, "manual-idle", 110.0),),
            "agent-discharge": (
                Proposal("rhs", api.Direction.DISCHARGE, 400, "agent-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("mid", api.Direction.IDLE, 0, "manual-idle"),
            Setpoint("rhs", api.Direction.DISCHARGE, 400, "agent-discharge"),
        ),
        ("safety_checks_passed",),
    )
    kernel, _history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(idle, agent),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    decision = await kernel.tick()
    assert decision is outcome
    (batch,) = auth.published
    assert {cap.unit_id: cap.watts for cap in batch.authorizations} == {"rhs": 400}
    (event,) = audit.events
    assert dict(event.directions_by_unit) == {"mid": "idle", "rhs": "discharge"}


async def test_a_units_denial_mints_authority_only_for_the_running_units(api: Any) -> None:
    """One unit's safety denial in a composed cycle zeroes that unit only:
    the capability batch carries the other unit's authority."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    selection = _real_selection((mid, rhs))
    allocator = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "mid-charge", 110.0),),
            "rhs-discharge": (
                Proposal("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("mid", api.Direction.CHARGE, 0, "mid-charge"),
            Setpoint("rhs", api.Direction.DISCHARGE, 1_000, "rhs-discharge"),
        ),
        ("soc_above_charge_ceiling",),
    )
    kernel, _history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(mid, rhs),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    await kernel.tick()
    (batch,) = auth.published
    assert {cap.unit_id: cap.watts for cap in batch.authorizations} == {"rhs": 1_000}
    (event,) = audit.events
    assert dict(event.authorized_watts_by_unit) == {"mid": 0, "rhs": 1_000}
    assert event.authorized_active_w == 1_000
    assert event.reason_codes == ("soc_above_charge_ceiling",)


async def test_emergency_stop_dominates_the_composed_cycle(api: Any) -> None:
    """A live stop is the whole cycle even while two active intents are live:
    revocation precedes audit, nothing is minted, and the row is the stop's."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    stop = Intent(
        "stop-1",
        api.IntentSource.EMERGENCY_STOP,
        frozenset({"mid", "rhs"}),
        api.Direction.IDLE,
        0,
    )
    selection = _real_selection((mid, rhs, stop))
    assert selection.emergency is stop
    assert selection.ranked == (stop,)
    allocator = MultiIntentAllocator(
        {
            "stop-1": (
                Proposal("mid", api.Direction.IDLE, 0, "stop-1", 110.0),
                Proposal("rhs", api.Direction.IDLE, 0, "stop-1", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (
            Setpoint("mid", api.Direction.IDLE, 0, "stop-1"),
            Setpoint("rhs", api.Direction.IDLE, 0, "stop-1"),
        ),
        ("stop_authorized",),
    )
    kernel, history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(mid, rhs, stop),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    await kernel.tick()
    assert history.index("revoke") < history.index("audit")
    assert auth.published == []
    (event,) = audit.events
    assert event.intent_id == "stop-1"
    assert event.source is api.IntentSource.EMERGENCY_STOP
    assert event.correlation_id == "emergency_stop:stop-1"
    assert event.directions_by_unit is None


async def test_per_intent_total_bound_is_enforced_by_the_matcher(api: Any) -> None:
    """Each intent's proposals may never exceed ITS OWN fleet watts: the
    matcher groups proposals per winning intent and bounds each group."""
    mid = _charge_intent(api, "mid-charge", frozenset({"mid"}), 2_000)
    rhs = _discharge_intent(api, "rhs-discharge", frozenset({"rhs"}), 1_000)
    selection = _real_selection((mid, rhs))
    allocator = MultiIntentAllocator(
        {
            "mid-charge": (Proposal("mid", api.Direction.CHARGE, 2_000, "mid-charge", 110.0),),
            "rhs-discharge": (
                # rhs's intent asked 1,000 W; 1,500 W for rhs alone breaks the
                # per-intent bound even though the fleet total is plausible.
                Proposal("rhs", api.Direction.DISCHARGE, 1_500, "rhs-discharge", 110.0),
            ),
        },
        [],
    )
    outcome = Decision(
        api.DecisionStatus.AUTHORIZED,
        (Setpoint("rhs", api.Direction.DISCHARGE, 1_500, "rhs-discharge"),),
    )
    kernel, _history, auth, audit = _make_composed_kernel(
        api,
        intents_in_cycle=(mid, rhs),
        arbiter_value=selection,
        allocator=allocator,
        output=outcome,
    )
    with pytest.raises(ValueError, match="allocator output does not match"):
        await kernel.tick()
    assert auth.published == [] and audit.events == [] and auth.revocations
