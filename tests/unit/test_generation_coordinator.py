"""S0 contracts for one process-local, fleet-wide authority generation.

Generation fences invalidating events. It does not identify healthy renewal cycles:
many fresh, single-use cycle IDs are expected within one stable generation.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from dataclasses import FrozenInstanceError, dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.test_actor import (
    AuthorizationRecord,
    FakeAuditRepository,
    FakeAuthorizationRepository,
    FakeClock,
    FakeCommandEncoder,
    FakeObservationRepository,
    Gate,
    ObservationRecord,
    SpyTransport,
)
from tests.unit.test_control_kernel import (
    Allocator,
    Arbiter,
    Audit,
    Authorizations,
    Clock,
    Intents,
    Observations,
    Safety,
    decision_for,
    deterministic_audit_factory,
    intent,
    observation_pairs,
    proposals_for,
)

MAX_GENERATION = (1 << 63) - 1


@pytest.fixture(scope="module")
def contract() -> SimpleNamespace:
    """Load production lazily so absence is an ordinary red assertion."""
    try:
        generation = importlib.import_module("energypod.application.generation")
        coordinator_type = generation.AuthorityGenerationCoordinator
        snapshot_type = generation.AuthorityGenerationSnapshot
    except (ImportError, AttributeError) as error:
        message = f"Authority-generation coordinator contract is not implemented: {error}"

        class MissingContract:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError(message)

        coordinator_type = snapshot_type = MissingContract

    actor = importlib.import_module("energypod.application.actor")
    kernel = importlib.import_module("energypod.application.control_kernel")
    audit_module = importlib.import_module("energypod.application.audit")
    domain = importlib.import_module("energypod.domain")
    return SimpleNamespace(
        AuthorityGenerationCoordinator=coordinator_type,
        AuthorityGenerationSnapshot=snapshot_type,
        EnergyPodActor=actor.EnergyPodActor,
        ControlKernel=kernel.ControlKernel,
        AuditEventFactory=audit_module.AuditEventFactory,
        Direction=domain.Direction,
        DecisionStatus=domain.DecisionStatus,
        IntentSource=domain.IntentSource,
    )


def test_contract_has_only_the_two_authority_operations(contract: Any) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    assert inspect.iscoroutinefunction(coordinator.snapshot)
    assert inspect.iscoroutinefunction(coordinator.advance)
    for prohibited in ("reset", "restore", "set", "load", "initial_epoch"):
        assert not hasattr(coordinator, prohibited)


def test_constructor_cannot_restore_or_select_an_epoch(contract: Any) -> None:
    with pytest.raises(TypeError):
        contract.AuthorityGenerationCoordinator(initial_epoch=41)


async def test_instances_are_independent_and_have_no_hidden_global_epoch(contract: Any) -> None:
    first = contract.AuthorityGenerationCoordinator()
    second = contract.AuthorityGenerationCoordinator()
    assert (await first.snapshot()).epoch == (await second.snapshot()).epoch == 0
    assert (await first.advance(reason="unit-reconnect:mid")).epoch == 1
    assert (await first.snapshot()).epoch == 1
    assert (await second.snapshot()).epoch == 0


def test_snapshot_is_a_strict_frozen_bounded_value(contract: Any) -> None:
    zero = contract.AuthorityGenerationSnapshot(epoch=0)
    maximum = contract.AuthorityGenerationSnapshot(epoch=MAX_GENERATION)
    assert zero.epoch == 0
    assert maximum.epoch == MAX_GENERATION
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        zero.epoch = 1
    for invalid in (True, False, -1, MAX_GENERATION + 1, 1.0, "1", None):
        with pytest.raises((TypeError, ValueError, OverflowError)):
            contract.AuthorityGenerationSnapshot(epoch=invalid)


@pytest.mark.parametrize(
    "reason",
    [None, True, False, 1, "", " ", " leading", "trailing ", "bad\nreason", "x" * 201],
)
async def test_advance_rejects_malformed_reason_without_mutation(
    contract: Any, reason: Any
) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    with pytest.raises((TypeError, ValueError)):
        await coordinator.advance(reason=reason)
    assert (await coordinator.snapshot()).epoch == 0


async def test_snapshots_are_strictly_monotonic(contract: Any) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    values = [await coordinator.snapshot()]
    values.append(await coordinator.advance(reason="unit-reconnect:mid"))
    values.append(await coordinator.advance(reason="operator-stop"))
    assert [item.epoch for item in values] == [0, 1, 2]
    assert len({id(item) for item in values}) == 3


async def test_concurrent_advances_are_linearizable_without_lost_updates(contract: Any) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    start = asyncio.Event()

    async def advance(index: int) -> Any:
        await start.wait()
        return await coordinator.advance(reason=f"concurrent-fence:{index}")

    tasks = [asyncio.create_task(advance(index)) for index in range(64)]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sorted(item.epoch for item in results) == list(range(1, 65))
    assert (await coordinator.snapshot()).epoch == 64


async def test_cancellation_before_execution_does_not_advance(contract: Any) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    task = asyncio.create_task(coordinator.advance(reason="never-started"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await coordinator.snapshot()).epoch == 0


async def test_cancellation_after_commit_cannot_roll_back_or_reuse_epoch(contract: Any) -> None:
    coordinator = contract.AuthorityGenerationCoordinator()
    committed = await coordinator.advance(reason="committed-fence")
    cancelled_waiter = asyncio.create_task(coordinator.snapshot())
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert committed.epoch == 1
    assert (await coordinator.snapshot()).epoch == 1
    assert (await coordinator.advance(reason="next-fence")).epoch == 2


async def test_cancellation_racing_for_serialization_never_loses_or_reuses_commits(
    contract: Any,
) -> None:
    """A cancelled call may linearize or not; every committed epoch stays fenced."""
    coordinator = contract.AuthorityGenerationCoordinator()
    start = asyncio.Event()

    async def contender(index: int) -> Any:
        await start.wait()
        return await coordinator.advance(reason=f"cancellation-race:{index}")

    tasks = [asyncio.create_task(contender(index)) for index in range(40)]
    start.set()
    await asyncio.sleep(0)
    for task in tasks[::2]:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    committed = sorted(
        outcome.epoch for outcome in outcomes if not isinstance(outcome, BaseException)
    )
    final_epoch = (await coordinator.snapshot()).epoch

    assert len(committed) == len(set(committed))
    assert all(1 <= epoch <= final_epoch for epoch in committed)
    assert (await coordinator.advance(reason="post-cancellation-fence")).epoch == final_epoch + 1


@dataclass(frozen=True)
class EpochSnapshot:
    epoch: int


class ScriptedCoordinator:
    """Coordinator double with deterministic snapshot barriers and trace."""

    def __init__(self, epoch: int = 8) -> None:
        self.epoch = epoch
        self.history: list[str] = []
        self.snapshot_calls = 0
        self.advance_calls: list[str] = []
        self._snapshot_barriers: dict[int, tuple[asyncio.Event, asyncio.Event]] = {}
        self.advance_probe: Any = None
        self.advance_probe_results: list[Any] = []

    def block_snapshot(self, call: int) -> tuple[asyncio.Event, asyncio.Event]:
        entered, release = asyncio.Event(), asyncio.Event()
        self._snapshot_barriers[call] = (entered, release)
        return entered, release

    async def snapshot(self) -> EpochSnapshot:
        self.snapshot_calls += 1
        call = self.snapshot_calls
        self.history.append(f"generation:{self.epoch}")
        barrier = self._snapshot_barriers.get(call)
        if barrier is not None:
            entered, release = barrier
            entered.set()
            await release.wait()
        return EpochSnapshot(self.epoch)

    async def advance(self, *, reason: str) -> EpochSnapshot:
        if self.advance_probe is not None:
            self.advance_probe_results.append(self.advance_probe())
        self.epoch += 1
        self.advance_calls.append(reason)
        self.history.append(f"advance:{self.epoch}")
        return EpochSnapshot(self.epoch)


def _kernel(contract: Any, coordinator: Any):
    value = intent(contract)
    current, previous = observation_pairs(value.unit_ids)
    authorizations = Authorizations(coordinator.history)
    audit = Audit(coordinator.history)
    clock = Clock()
    instance = contract.ControlKernel(
        clock=clock,
        unit_ids=frozenset({"lhs", "mid", "rhs"}),
        intents=Intents(value, coordinator.history),
        observations=Observations(current, previous, coordinator.history),
        authorizations=authorizations,
        audit=audit,
        arbiter=Arbiter(value, coordinator.history),
        allocator=Allocator(proposals_for(value), coordinator.history),
        safety=Safety(decision_for(contract, value), coordinator.history),
        policy=SimpleNamespace(version="policy-5", max_telemetry_age_s=2.0),
        generation_coordinator=coordinator,
        configuration_version=12,
        audit_event_factory=deterministic_audit_factory(contract, clock),
    )
    return instance, authorizations, audit


def _actor(contract: Any, coordinator: Any, *, authorizations: Any = None):
    clock = FakeClock()
    transport = SpyTransport()
    repository = authorizations or FakeAuthorizationRepository()
    instance = contract.EnergyPodActor(
        unit_id="mid",
        transport=transport,
        clock=clock,
        observations=FakeObservationRepository(),
        authorizations=repository,
        audit=FakeAuditRepository(),
        command_encoder=FakeCommandEncoder(),
        generation_coordinator=coordinator,
        expected_identity="BEP0005KXX11B10500055",
        expected_profile="iot-v1",
        expected_cell_count=59,
        stable_observations_required=1,
        essential_read_address=0x5000,
        essential_read_count=7,
        heartbeat_interval_s=1.0,
        heartbeat_safety_margin_s=0.2,
    )
    return instance, transport, repository


async def _start_armed(actor: Any) -> None:
    await actor.start()
    await actor.accept_observation(ObservationRecord())
    await actor.arm()


async def test_shared_startup_wiring_is_proved_by_fleet_behavior(contract: Any) -> None:
    coordinator = ScriptedCoordinator(epoch=4)
    first, *_ = _actor(contract, coordinator)
    second_repo = FakeAuthorizationRepository(AuthorizationRecord(generation=4, watts=700))
    second, second_transport, _ = _actor(contract, coordinator, authorizations=second_repo)
    await _start_armed(second)
    assert await first.fence("first-reconnect") == 5
    await second.heartbeat_once()
    assert second_transport.writes == []
    assert any(reason == "generation_mismatch" for _, reason in second_repo.revocations)
    await second.shutdown()


async def test_distinct_coordinators_do_not_share_authority(contract: Any) -> None:
    first = ScriptedCoordinator(epoch=3)
    second = ScriptedCoordinator(epoch=3)
    first_actor, *_ = _actor(contract, first)
    second_repo = FakeAuthorizationRepository(AuthorizationRecord(generation=3, watts=700))
    second_actor, second_transport, _ = _actor(contract, second, authorizations=second_repo)
    await _start_armed(second_actor)
    await first_actor.fence("isolated-instance")
    await second_actor.heartbeat_once()
    assert second_transport.writes != []
    assert (first.epoch, second.epoch) == (4, 3)
    await second_actor.shutdown()


async def test_actor_fence_advances_before_cancelling_inflight_lookup(contract: Any) -> None:
    coordinator = ScriptedCoordinator(epoch=8)
    lookup_gate = Gate(ignore_cancellation=True)
    coordinator.advance_probe = lookup_gate.cancelled.is_set
    repository = FakeAuthorizationRepository(
        AuthorizationRecord(generation=8), current_gate=lookup_gate
    )
    actor, transport, _ = _actor(contract, coordinator, authorizations=repository)
    await _start_armed(actor)
    heartbeat = asyncio.create_task(actor.heartbeat_once())
    await asyncio.wait_for(lookup_gate.entered.wait(), timeout=1)
    fence = asyncio.create_task(actor.fence("replacement"))
    try:
        for _ in range(20):
            if coordinator.epoch == 9:
                break
            await asyncio.sleep(0)
        assert coordinator.epoch == 9
        assert coordinator.advance_probe_results == [False]
        for _ in range(20):
            if lookup_gate.cancelled.is_set():
                break
            await asyncio.sleep(0)
        assert lookup_gate.cancelled.is_set()
    finally:
        lookup_gate.release.set()
    await asyncio.gather(heartbeat, fence)
    assert transport.writes == []
    await actor.shutdown()


async def test_stale_repository_capability_cannot_write_after_fleet_fence(
    contract: Any,
) -> None:
    coordinator = ScriptedCoordinator(epoch=6)
    stale = FakeAuthorizationRepository(AuthorizationRecord(generation=6, watts=700))
    actor, transport, _ = _actor(contract, coordinator, authorizations=stale)
    await _start_armed(actor)
    await coordinator.advance(reason="other-unit-inhibit")
    await actor.heartbeat_once()
    assert transport.writes == []
    assert any(reason == "generation_mismatch" for _, reason in stale.revocations)
    await actor.shutdown()


async def test_kernel_checks_before_mint_before_audit_and_before_publish(contract: Any) -> None:
    coordinator = ScriptedCoordinator(epoch=8)
    kernel, authorizations, _ = _kernel(contract, coordinator)
    await kernel.tick()
    assert coordinator.snapshot_calls == 3
    assert len(authorizations.published) == 1
    batch = authorizations.published[0]
    assert batch.generation == 8
    assert {item.generation for item in batch.authorizations} == {8}
    trace = coordinator.history
    checks = [index for index, item in enumerate(trace) if item == "generation:8"]
    assert checks[0] < checks[1] < trace.index("audit") < checks[2] < trace.index("publish")


@pytest.mark.parametrize("advance_at_call", [2, 3])
async def test_epoch_change_before_audit_or_publish_revokes_without_publication(
    contract: Any, advance_at_call: int
) -> None:
    coordinator = ScriptedCoordinator(epoch=8)
    entered, release = coordinator.block_snapshot(advance_at_call)
    kernel, authorizations, _ = _kernel(contract, coordinator)
    task = asyncio.create_task(kernel.tick())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await coordinator.advance(reason="concurrent-fence")
    release.set()
    await task
    assert authorizations.published == []
    assert authorizations.revocations


async def test_epoch_fence_between_mint_and_audit_still_records_a_fenced_cycle(
    contract: Any,
) -> None:
    """A fenced cycle must leave a durable audit record, not an audit gap."""
    coordinator = ScriptedCoordinator(epoch=8)
    entered, release = coordinator.block_snapshot(2)
    kernel, authorizations, audit = _kernel(contract, coordinator)
    task = asyncio.create_task(kernel.tick())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await coordinator.advance(reason="concurrent-fence")
    release.set()
    await task
    assert authorizations.published == []
    assert authorizations.revocations
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.generation == 8
    assert event.authorized_active_w == 0


async def test_concurrent_ticks_cannot_publish_old_generation_after_fence(contract: Any) -> None:
    coordinator = ScriptedCoordinator(epoch=11)
    entered, release = coordinator.block_snapshot(3)
    kernel, authorizations, _ = _kernel(contract, coordinator)
    ticks = [asyncio.create_task(kernel.tick()) for _ in range(2)]
    await asyncio.wait_for(entered.wait(), timeout=1)
    await coordinator.advance(reason="shutdown")
    release.set()
    await asyncio.gather(*ticks)
    assert all(batch.generation >= 12 for batch in authorizations.published)
    assert not any(batch.generation == 11 for batch in authorizations.published)


async def test_healthy_cycles_keep_generation_stable_and_use_fresh_cycle_ids(
    contract: Any,
) -> None:
    coordinator = ScriptedCoordinator(epoch=5)
    kernel, authorizations, _ = _kernel(contract, coordinator)
    await kernel.tick()
    await kernel.tick()
    assert coordinator.epoch == 5
    assert [batch.generation for batch in authorizations.published] == [5, 5]
    assert len({batch.cycle_id for batch in authorizations.published}) == 2
    assert coordinator.advance_calls == []
