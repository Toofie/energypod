"""S0 contract tests for the sole-owner EnergyPod actor.

Production modules are loaded inside a fixture so this test-first suite collects before
implementation exists.  Missing contracts are reported as ordinary test failures.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import pytest


UNIT_ID = "mid"
IDENTITY = "BEP0005KXX11B10500055"
PROFILE = "iot-v1"


@dataclass(frozen=True)
class ObservationRecord:
    unit_id: str = UNIT_ID
    device_identity: str = IDENTITY
    protocol_profile: str = PROFILE
    connection_epoch: int = 1
    sequence: int = 1
    complete: bool = True
    quality: str = "good"


@dataclass(frozen=True)
class AuthorizationRecord:
    unit_id: str = UNIT_ID
    connection_epoch: int = 1
    generation: int = 0
    cycle_id: int = 1
    intent_id: str = "manual-1"
    active_watts: int = 500
    reactive_vars: int = 0
    issued_at_mono: float = 100.0
    not_before_mono: float = 100.0
    expires_at_mono: float = 101.0
    observation_sequence: int = 1
    policy_version: str = "policy-1"


@dataclass(frozen=True)
class EncodedWrite:
    address: int
    values: tuple[int, ...]


class FakeClock:
    def __init__(self, monotonic: float = 100.0) -> None:
        self.now = monotonic
        self.wall = datetime(2026, 8, 21, tzinfo=UTC)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        deadline = self.now + seconds
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((deadline, future))
        self._release_due_waiters()
        await future

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.now += seconds
        self.wall += timedelta(seconds=seconds)
        self._release_due_waiters()

    def _release_due_waiters(self) -> None:
        pending: list[tuple[float, asyncio.Future[None]]] = []
        for deadline, future in self._waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)
            elif not future.done():
                pending.append((deadline, future))
        self._waiters = pending


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


class SpyTransport:
    def __init__(self) -> None:
        self.history: list[tuple[str, Any]] = []
        self.write_attempts: list[EncodedWrite] = []
        self.writes: list[EncodedWrite] = []
        self.active_operations = 0
        self.maximum_concurrency = 0
        self.closed = False
        self.read_gate: Gate | None = None
        self.write_gates: deque[Gate] = deque()
        self.write_failures: deque[BaseException] = deque()

    async def connect(self) -> None:
        await self._operation("connect", None)

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        async def body() -> tuple[int, ...]:
            if self.read_gate is not None:
                await self.read_gate.wait()
            return tuple(0 for _ in range(count))

        return await self._operation("read", (address, count), body)

    async def write_registers(self, address: int, values: tuple[int, ...]) -> None:
        request = EncodedWrite(address, tuple(values))
        self.write_attempts.append(request)

        async def body() -> None:
            if self.write_gates:
                await self.write_gates.popleft().wait()
            if self.write_failures:
                raise self.write_failures.popleft()
            self.writes.append(request)

        await self._operation("write", request, body)

    async def close(self) -> None:
        if self.closed:
            return

        async def body() -> None:
            self.closed = True

        await self._operation("close", None, body)

    async def _operation(
        self,
        name: str,
        detail: Any,
        body: Callable[[], Any] | None = None,
    ) -> Any:
        self.active_operations += 1
        self.maximum_concurrency = max(self.maximum_concurrency, self.active_operations)
        self.history.append((f"{name}:start", detail))
        try:
            if body is None:
                return None
            return await body()
        except asyncio.CancelledError:
            self.history.append((f"{name}:cancelled", detail))
            raise
        finally:
            self.history.append((f"{name}:end", detail))
            self.active_operations -= 1


class FakeObservationRepository:
    def __init__(self, observation: ObservationRecord | None = None) -> None:
        self.value = observation
        self.appended: list[ObservationRecord] = []

    async def latest(self, unit_id: str) -> ObservationRecord | None:
        assert unit_id == UNIT_ID
        return self.value

    async def append(self, observation: ObservationRecord) -> None:
        self.value = observation
        self.appended.append(observation)


class FakeAuthorizationRepository:
    def __init__(self, *responses: AuthorizationRecord | None) -> None:
        self.responses: deque[AuthorizationRecord | None] = deque(responses)
        self.current_calls: list[tuple[str, float]] = []
        self.revocations: list[tuple[Any, str]] = []

    async def current(self, unit_id: str, now_mono: float) -> AuthorizationRecord | None:
        self.current_calls.append((unit_id, now_mono))
        return self.responses.popleft() if self.responses else None

    async def revoke(
        self,
        unit_ids: Any = None,
        *,
        reason: str,
        **_context: Any,
    ) -> None:
        self.revocations.append((unit_ids, reason))


class FakeAuditRepository:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


class FakeCommandEncoder:
    def encode(self, authorization: AuthorizationRecord) -> EncodedWrite:
        return EncodedWrite(0x0200, (1, authorization.active_watts, authorization.reactive_vars))

    def zero(self) -> EncodedWrite:
        return EncodedWrite(0x0200, (1, 0, 0))


@pytest.fixture
def contract() -> Any:
    try:
        actor_module = importlib.import_module("energypod.application.actor")
        domain_module = importlib.import_module("energypod.domain")
        return type(
            "Contract",
            (),
            {
                "EnergyPodActor": actor_module.EnergyPodActor,
                "UnitLifecycle": domain_module.UnitLifecycle,
            },
        )
    except (ImportError, AttributeError) as error:
        pytest.fail(f"EnergyPod actor contract is not implemented: {error}", pytrace=False)


def make_actor(
    contract: Any,
    *,
    clock: FakeClock | None = None,
    transport: SpyTransport | None = None,
    observations: FakeObservationRepository | None = None,
    authorizations: FakeAuthorizationRepository | None = None,
) -> tuple[Any, FakeClock, SpyTransport, FakeObservationRepository, FakeAuthorizationRepository]:
    test_clock = clock or FakeClock()
    test_transport = transport or SpyTransport()
    observation_repo = observations or FakeObservationRepository()
    authorization_repo = authorizations or FakeAuthorizationRepository()
    actor = contract.EnergyPodActor(
        unit_id=UNIT_ID,
        transport=test_transport,
        clock=test_clock,
        observations=observation_repo,
        authorizations=authorization_repo,
        audit=FakeAuditRepository(),
        command_encoder=FakeCommandEncoder(),
        expected_identity=IDENTITY,
        expected_profile=PROFILE,
        expected_cell_count=59,
        stable_observations_required=1,
        essential_read_address=0x5000,
        essential_read_count=7,
        heartbeat_interval_s=1.0,
        heartbeat_safety_margin_s=0.2,
    )
    return actor, test_clock, test_transport, observation_repo, authorization_repo


async def ready_actor(actor: Any, observation: ObservationRecord | None = None) -> None:
    await actor.start()
    await actor.accept_observation(observation or ObservationRecord())
    await actor.arm()


async def settle_until(predicate: Callable[[], bool], turns: int = 50) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("deterministic condition was not reached")


async def test_boot_is_observe_only_and_requires_qualification_then_explicit_arm(
    contract: Any,
) -> None:
    actor, _, transport, _, _ = make_actor(contract)

    await actor.start()

    assert actor.lifecycle is contract.UnitLifecycle.OBSERVE_ONLY
    assert transport.writes == []

    await actor.accept_observation(ObservationRecord())
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED

    await actor.arm()
    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    assert transport.writes == []

    await actor.shutdown()


async def test_transport_operations_are_serialized_by_the_actor(contract: Any) -> None:
    auth = AuthorizationRecord()
    actor, _, transport, _, _ = make_actor(
        contract,
        authorizations=FakeAuthorizationRepository(auth),
    )
    await ready_actor(actor)

    await asyncio.gather(actor.poll_once(), actor.heartbeat_once())

    assert transport.maximum_concurrency == 1
    assert transport.writes == [EncodedWrite(0x0200, (1, 500, 0))]
    await actor.shutdown()


async def test_every_heartbeat_fetches_and_consumes_fresh_authorization(contract: Any) -> None:
    first = AuthorizationRecord(cycle_id=10, active_watts=400)
    second = AuthorizationRecord(cycle_id=11, active_watts=600)
    repository = FakeAuthorizationRepository(first, second)
    actor, _, transport, _, _ = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert repository.current_calls == [(UNIT_ID, 100.0), (UNIT_ID, 100.0)]
    assert transport.writes == [
        EncodedWrite(0x0200, (1, 400, 0)),
        EncodedWrite(0x0200, (1, 600, 0)),
    ]
    await actor.shutdown()


async def test_single_cycle_authorization_cannot_be_reused(contract: Any) -> None:
    authorization = AuthorizationRecord(cycle_id=7)
    actor, _, transport, _, authorizations = make_actor(
        contract,
        authorizations=FakeAuthorizationRepository(authorization, authorization),
    )
    await ready_actor(actor)

    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert transport.writes == [EncodedWrite(0x0200, (1, 500, 0))]
    assert any(reason == "authorization_already_used" for _, reason in authorizations.revocations)
    await actor.shutdown()


@pytest.mark.parametrize("evidence_sequence", [0, 2])
async def test_observation_sequence_mismatch_rejects_authorization(
    contract: Any,
    evidence_sequence: int,
) -> None:
    authorization = AuthorizationRecord(observation_sequence=evidence_sequence)
    actor, _, transport, _, authorizations = make_actor(
        contract,
        authorizations=FakeAuthorizationRepository(authorization),
    )
    await ready_actor(actor, ObservationRecord(sequence=1))

    await actor.heartbeat_once()

    assert transport.writes == []
    assert any(reason == "observation_sequence_mismatch" for _, reason in authorizations.revocations)
    await actor.shutdown()


async def test_generation_fence_rejects_old_authorization_and_accepts_current_one(
    contract: Any,
) -> None:
    old = AuthorizationRecord(generation=0, cycle_id=1)
    current = AuthorizationRecord(generation=1, cycle_id=2, active_watts=700)
    repository = FakeAuthorizationRepository(old, current)
    actor, _, transport, _, _ = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    assert await actor.fence("replacement") == 1
    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert transport.writes == [EncodedWrite(0x0200, (1, 700, 0))]
    await actor.shutdown()


async def test_replacement_cancels_inflight_old_generation_before_it_can_write(
    contract: Any,
) -> None:
    gate = Gate()
    transport = SpyTransport()
    transport.write_gates.append(gate)
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord(generation=0)),
    )
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    await gate.entered.wait()
    replacement = asyncio.create_task(actor.fence("replacement"))
    await settle_until(lambda: heartbeat.done())
    gate.release.set()
    await replacement

    assert EncodedWrite(0x0200, (1, 500, 0)) not in transport.writes
    assert actor.generation == 1
    await actor.shutdown()


async def test_write_failure_revokes_attempts_zero_and_inhibits(contract: Any) -> None:
    transport = SpyTransport()
    transport.write_failures.append(OSError("gateway reset"))
    authorizations = FakeAuthorizationRepository(AuthorizationRecord())
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=authorizations,
    )
    await ready_actor(actor)

    await actor.heartbeat_once()

    assert transport.write_attempts == [
        EncodedWrite(0x0200, (1, 500, 0)),
        EncodedWrite(0x0200, (1, 0, 0)),
    ]
    assert transport.writes == [EncodedWrite(0x0200, (1, 0, 0))]
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert authorizations.revocations
    await actor.shutdown()


async def test_shutdown_fences_cancels_nonzero_attempt_then_zeroes_and_closes(
    contract: Any,
) -> None:
    gate = Gate()
    transport = SpyTransport()
    transport.write_gates.append(gate)
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
    )
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    await gate.entered.wait()
    shutdown = asyncio.create_task(actor.shutdown())
    await settle_until(lambda: heartbeat.done())
    gate.release.set()
    await shutdown

    assert EncodedWrite(0x0200, (1, 500, 0)) not in transport.writes
    assert EncodedWrite(0x0200, (1, 0, 0)) in transport.writes
    assert actor.lifecycle is contract.UnitLifecycle.STOPPING
    assert transport.closed is True


async def test_heartbeat_preempts_an_overdue_telemetry_read(contract: Any) -> None:
    read_gate = Gate()
    transport = SpyTransport()
    transport.read_gate = read_gate
    actor, clock, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
    )
    await ready_actor(actor)

    poll = asyncio.create_task(actor.poll_once())
    await read_gate.entered.wait()
    heartbeat = asyncio.create_task(actor.heartbeat_once())
    clock.advance(0.81)
    await settle_until(lambda: heartbeat.done())

    assert any(name == "read:cancelled" for name, _ in transport.history)
    assert transport.writes == [EncodedWrite(0x0200, (1, 500, 0))]
    read_gate.release.set()
    await asyncio.gather(poll, return_exceptions=True)
    await actor.shutdown()


async def test_no_nonzero_write_is_possible_after_stop(contract: Any) -> None:
    repository = FakeAuthorizationRepository(AuthorizationRecord(), AuthorizationRecord())
    actor, _, transport, _, _ = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    await actor.heartbeat_once()
    await actor.shutdown()
    writes_at_stop = tuple(transport.writes)
    await actor.heartbeat_once()

    assert tuple(transport.writes) == writes_at_stop
    assert transport.writes[-1] == EncodedWrite(0x0200, (1, 0, 0))
