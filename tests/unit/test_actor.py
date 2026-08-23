"""S0 contract tests for the sole-owner EnergyPod actor.

Production modules are loaded inside a fixture so this test-first suite collects before
implementation exists.  Missing contracts are reported as ordinary test failures.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
    active_faults: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationRecord:
    unit_id: str = UNIT_ID
    connection_epoch: int = 1
    generation: int = 0
    cycle_id: int = 1
    intent_id: str = "manual-1"
    direction: str = "discharge"
    watts: int = 500
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
    """A deterministic await boundary that can model cancellation-resistant I/O."""

    def __init__(self, *, ignore_cancellation: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.ignore_cancellation = ignore_cancellation

    async def wait(self) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if not self.ignore_cancellation:
                raise
            await self.release.wait()


class SpyTransport:
    def __init__(self) -> None:
        self.history: list[tuple[str, Any]] = []
        self.operation_tasks: list[tuple[str, asyncio.Task[Any] | None]] = []
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
        self.operation_tasks.append((name, asyncio.current_task()))
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
    def __init__(
        self,
        *responses: AuthorizationRecord | None,
        current_gate: Gate | None = None,
    ) -> None:
        self.responses: deque[AuthorizationRecord | None] = deque(responses)
        self.current_calls: list[tuple[str, float]] = []
        self.revocations: list[tuple[Any, str]] = []
        self.current_gate = current_gate

    async def current(self, unit_id: str, now_mono: float) -> AuthorizationRecord | None:
        self.current_calls.append((unit_id, now_mono))
        if self.current_gate is not None:
            await self.current_gate.wait()
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
        sign = (
            -1
            if getattr(authorization.direction, "value", authorization.direction) == "charge"
            else 1
        )
        return EncodedWrite(0x0200, (1, sign * authorization.watts, authorization.reactive_vars))

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
                "InhibitCause": actor_module.InhibitCause,
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
    blocking_fault_codes: frozenset[str] | None = None,
    mode_refresh_window: tuple[int, int] | None = None,
    telemetry: Any | None = None,
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
        blocking_fault_codes=blocking_fault_codes,
        mode_refresh_window=mode_refresh_window,
        telemetry=telemetry,
    )
    return actor, test_clock, test_transport, observation_repo, authorization_repo


async def ready_actor(actor: Any, observation: ObservationRecord | None = None) -> None:
    await actor.start()
    await actor.accept_observation(observation or ObservationRecord())
    await actor.arm()


async def settle_until(predicate: Callable[[], bool], turns: int = 50) -> bool:
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


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


async def test_mode_word_refresh_reads_one_bounded_window_through_the_transport(
    contract: Any,
) -> None:
    """SYNC_RESILIENCE_AUDIT B5: the dispatch-refusal path needs a FRESH
    ctrlMode word (the cached one rides the once-per-process system tier).
    The refresh is a mailbox operation on this actor -- the sole transport
    owner -- reading exactly the three-word system-mode window, bounded by
    the heartbeat margin, never touching the control cadence."""
    actor, _, transport, _, _ = make_actor(contract, mode_refresh_window=(0x0100, 3))
    await actor.start()

    words = await actor.refresh_mode_words()

    assert words == (0, 0), "the raw served ctrl/work words, decoded from +1/+2"
    reads = [detail for name, detail in transport.history if name == "read:start"]
    assert (0x0100, 3) in reads, "the refresh reads exactly the system-mode window"
    await actor.shutdown()


async def test_mode_word_refresh_without_a_wired_window_fails_closed(contract: Any) -> None:
    actor, _, _, _, _ = make_actor(contract)
    await actor.start()

    with pytest.raises(RuntimeError, match="mode refresh window"):
        await actor.refresh_mode_words()
    await actor.shutdown()


async def test_cell_refresh_request_reaches_the_next_polls_telemetry_plan(contract: Any) -> None:
    """SYNC_RESILIENCE_AUDIT B4: the fleet loop flags the actor after a
    cell-derived deny; the actor's NEXT poll forwards the hint to its
    telemetry strategy (sole transport ownership unchanged) and consumes it
    -- exactly one promoted cycle, then the tier phase rules again."""
    hints: list[bool] = []

    class RecordingTelemetry:
        async def advance(self) -> None:
            return None

        def request_cell_refresh(self) -> None:
            hints.append(True)

        def read_plan(self) -> tuple[tuple[int, int], ...]:
            return ()

        def decode(self, blocks: Any, lifecycle: Any) -> Any:
            return ObservationRecord()

    actor, _, _, _, _ = make_actor(contract, telemetry=RecordingTelemetry())
    await actor.start()

    await actor.poll_once()
    assert hints == [], "no hint before any request"
    actor.request_cell_refresh()
    await actor.poll_once()
    assert hints == [True], "the requested poll forwards the hint exactly once"
    await actor.poll_once()
    assert hints == [True], "the hint is consumed by one poll"
    await actor.shutdown()


async def test_advisory_system_soc_quality_does_not_block_qualification(
    contract: Any,
) -> None:
    """SYNC_RESILIENCE_AUDIT B1: a once-per-process system-SOC word must not
    permanently reset qualification.  A quality map whose ONLY non-GOOD entry
    is the advisory system SOC (the cycle-1 read decoded BAD, cached forever)
    still qualifies the unit, because every safety-critical field is served
    fresh at the control rate and reads GOOD."""
    from types import SimpleNamespace

    quality: dict[str, str] = {
        field: "good"
        for field in (
            "system_soc_pct",
            "bms_soc_pct",
            "soh_pct",
            "battery_watts",
            "pack_voltage_v",
            "pack_current_a",
            "dynamic_charge_limit_w",
            "dynamic_discharge_limit_w",
            "cell_voltages_v",
            "temperatures_c",
        )
    }
    quality["system_soc_pct"] = "bad"
    observation = SimpleNamespace(
        unit_id=UNIT_ID,
        device_identity=IDENTITY,
        protocol_profile=PROFILE,
        connection_epoch=1,
        sequence=1,
        safety_data_complete=True,
        quality=quality,
        active_faults=(),
        cells=None,
    )

    actor, _, _, _, _ = make_actor(contract)
    await actor.start()
    await actor.accept_observation(observation)

    assert actor.lifecycle is contract.UnitLifecycle.DISARMED, (
        "an advisory system-SOC quality failure must never reset qualification"
    )
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
    protocol_tasks = {
        task for operation, task in transport.operation_tasks if operation in {"read", "write"}
    }
    assert len(protocol_tasks) == 1, "one actor mailbox task must be the sole socket owner"
    await actor.shutdown()


async def test_every_heartbeat_fetches_and_consumes_fresh_authorization(contract: Any) -> None:
    first = AuthorizationRecord(cycle_id=10, watts=400)
    second = AuthorizationRecord(cycle_id=11, watts=600)
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
        authorizations=FakeAuthorizationRepository(authorization, replace(authorization)),
    )
    await ready_actor(actor)

    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert transport.writes == [EncodedWrite(0x0200, (1, 500, 0))]
    assert any(reason == "authorization_already_used" for _, reason in authorizations.revocations)
    await actor.shutdown()


async def test_authorization_is_consumed_before_entering_the_transport_await(
    contract: Any,
) -> None:
    gate = Gate()
    transport = SpyTransport()
    transport.write_gates.append(gate)
    authorization = AuthorizationRecord(cycle_id=7)
    actor, _, _, _, authorizations = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(authorization, replace(authorization)),
    )
    await ready_actor(actor)

    first_attempt = asyncio.create_task(actor.heartbeat_once())
    entered_write = await settle_until(gate.entered.is_set)
    first_attempt.cancel()
    await asyncio.gather(first_attempt, return_exceptions=True)
    assert entered_write, "heartbeat never reached the controlled write boundary"
    await actor.heartbeat_once()

    nonzero_attempts = [attempt for attempt in transport.write_attempts if attempt.values[1] != 0]
    assert nonzero_attempts == [EncodedWrite(0x0200, (1, 500, 0))]
    assert any(reason == "authorization_already_used" for _, reason in authorizations.revocations)
    await actor.shutdown()


@pytest.mark.parametrize(
    ("now_mono", "expected_reason"),
    [
        (99.999, "authorization_not_yet_valid"),
        (101.0, "authorization_expired"),
        (101.001, "authorization_expired"),
    ],
)
async def test_authorization_time_window_is_closed_at_both_unsafe_boundaries(
    contract: Any,
    now_mono: float,
    expected_reason: str,
) -> None:
    clock = FakeClock(now_mono)
    authorization = AuthorizationRecord(not_before_mono=100.0, expires_at_mono=101.0)
    actor, _, transport, _, authorizations = make_actor(
        contract,
        clock=clock,
        authorizations=FakeAuthorizationRepository(authorization),
    )
    await ready_actor(actor)

    await actor.heartbeat_once()

    assert transport.write_attempts == []
    assert any(reason == expected_reason for _, reason in authorizations.revocations)
    await actor.shutdown()


async def test_authorization_expiring_during_lookup_is_rechecked_before_write(
    contract: Any,
) -> None:
    lookup_gate = Gate()
    clock = FakeClock()
    authorization = AuthorizationRecord(expires_at_mono=100.5)
    repository = FakeAuthorizationRepository(authorization, current_gate=lookup_gate)
    actor, _, transport, _, authorizations = make_actor(
        contract,
        clock=clock,
        authorizations=repository,
    )
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    entered_lookup = await settle_until(lookup_gate.entered.is_set)
    if not entered_lookup:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_lookup, "heartbeat never requested current authorization"
    clock.advance(0.5)
    lookup_gate.release.set()
    await heartbeat

    assert repository.current_calls == [(UNIT_ID, 100.0)]
    assert transport.write_attempts == []
    assert any(reason == "authorization_expired" for _, reason in authorizations.revocations)
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
    assert any(
        reason == "observation_sequence_mismatch" for _, reason in authorizations.revocations
    )
    await actor.shutdown()


async def test_generation_fence_rejects_old_authorization_and_accepts_current_one(
    contract: Any,
) -> None:
    old = AuthorizationRecord(generation=0, cycle_id=1)
    current = AuthorizationRecord(generation=1, cycle_id=2, watts=700)
    repository = FakeAuthorizationRepository(old, current)
    actor, _, transport, _, authorizations = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    assert await actor.fence("replacement") == 1
    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert transport.writes == [EncodedWrite(0x0200, (1, 700, 0))]
    assert any(reason == "generation_mismatch" for _, reason in authorizations.revocations)
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
    entered_write = await settle_until(gate.entered.is_set)
    if not entered_write:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_write, "heartbeat never reached the controlled write boundary"
    replacement = asyncio.create_task(actor.fence("replacement"))
    heartbeat_cancelled = await settle_until(heartbeat.done)
    gate.release.set()
    await asyncio.gather(heartbeat, replacement, return_exceptions=True)

    assert heartbeat_cancelled, "replacement did not cancel old-generation work"
    assert EncodedWrite(0x0200, (1, 500, 0)) not in transport.writes
    assert actor.generation == 1
    await actor.shutdown()


async def test_fence_is_published_before_cancelling_an_authorization_lookup(
    contract: Any,
) -> None:
    lookup_gate = Gate(ignore_cancellation=True)
    repository = FakeAuthorizationRepository(
        AuthorizationRecord(generation=0),
        current_gate=lookup_gate,
    )
    actor, _, transport, _, _ = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    entered_lookup = await settle_until(lookup_gate.entered.is_set)
    if not entered_lookup:
        heartbeat.cancel()
        lookup_gate.release.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_lookup, "heartbeat never requested current authorization"
    replacement = asyncio.create_task(actor.fence("replacement"))
    generation_published = await settle_until(lambda: actor.generation == 1)

    assert not lookup_gate.release.is_set()
    assert transport.write_attempts == []

    lookup_gate.release.set()
    await asyncio.gather(heartbeat, replacement, return_exceptions=True)

    assert generation_published, "fence was not visible before cancellation completed"
    assert actor.generation == 1
    assert transport.write_attempts == []
    await actor.shutdown()


async def test_late_old_generation_acknowledgement_cannot_restore_active_state(
    contract: Any,
) -> None:
    write_gate = Gate(ignore_cancellation=True)
    transport = SpyTransport()
    transport.write_gates.append(write_gate)
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord(generation=0)),
    )
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    entered_write = await settle_until(write_gate.entered.is_set)
    if not entered_write:
        heartbeat.cancel()
        write_gate.release.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_write, "heartbeat never reached the controlled write boundary"
    replacement = asyncio.create_task(actor.fence("replacement"))
    generation_published = await settle_until(lambda: actor.generation == 1)
    write_gate.release.set()
    await asyncio.gather(heartbeat, replacement, return_exceptions=True)

    assert generation_published, "replacement did not publish its fence before cancellation"
    assert actor.generation == 1
    assert actor.lifecycle is not contract.UnitLifecycle.ACTIVE
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


async def test_inhibit_recovery_requires_qualifying_samples_and_never_bad_ones(
    contract: Any,
) -> None:
    transport = SpyTransport()
    transport.write_failures.append(OSError("gateway reset"))
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
    )
    await ready_actor(actor)
    await actor.heartbeat_once()
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED

    # The worst-quality telemetry must never be what clears a safety inhibit.
    await actor.accept_observation(replace(ObservationRecord(), quality="bad"))
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED

    # Stable qualifying samples recover to DISARMED, never directly to ACTIVE.
    await actor.accept_observation(replace(ObservationRecord(), sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED

    # Nonzero power still requires an explicit arm after recovery.
    await actor.arm()
    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    await actor.shutdown()


# --- latched inhibit causes (ARCHITECTURE 8.1) ------------------------------


async def test_blocking_fault_latches_inhibited_until_privileged_acknowledgement(
    contract: Any,
) -> None:
    """A configured blocking fault is a LATCHED cause on the real actor."""
    actor, _, transport, _, authorizations = make_actor(
        contract, blocking_fault_codes=frozenset({"Stack_Fault0_3"})
    )
    await ready_actor(actor)
    faulted = ObservationRecord(sequence=2, active_faults=("Stack_Fault0_3",))
    generation_before = actor.generation

    await actor.accept_observation(faulted)

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_cause is contract.InhibitCause.LATCHED
    assert actor.inhibit_latched is True
    assert actor.generation > generation_before, "the inhibit must fence the generation"
    assert transport.writes == [EncodedWrite(0x0200, (1, 0, 0))], "the inhibit bounded-zeros"
    assert any(reason == "blocking_fault_active" for _, reason in authorizations.revocations)

    # A persisting fault holds the latch, and stable samples alone never
    # clear it: recovery cannot proceed underneath the acknowledgement gate.
    await actor.accept_observation(faulted)
    await actor.accept_observation(ObservationRecord(sequence=3))
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True

    # Disarm on an inhibited unit is a no-op and never clears the latch.
    await actor.disarm()
    assert actor.inhibit_latched is True
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED

    # Arming is refused while the latch stands.
    with pytest.raises(RuntimeError):
        await actor.arm()

    # Acknowledgement clears only the latch; recovery still needs samples.
    await actor.acknowledge_inhibit()
    assert actor.inhibit_latched is False
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED, (
        "acknowledgement never bypasses the stable-sample recovery path"
    )
    await actor.accept_observation(ObservationRecord(sequence=4))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED
    await actor.arm()
    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE

    # A returning fault re-latches: acknowledgement is per-cause, never a
    # permanent waiver.
    await actor.accept_observation(ObservationRecord(sequence=5, active_faults=("Stack_Fault0_3",)))
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True
    await actor.shutdown()


async def test_identity_mismatch_latches_inhibited_until_privileged_acknowledgement(
    contract: Any,
) -> None:
    """ARCHITECTURE 8.1: an identity mismatch is a latched inhibit cause."""
    actor, _, transport, _, authorizations = make_actor(contract)
    await ready_actor(actor)
    generation_before = actor.generation

    await actor.accept_observation(replace(ObservationRecord(), device_identity="BEP-SOMEONE-ELSE"))

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_cause is contract.InhibitCause.LATCHED
    assert actor.inhibit_latched is True
    assert actor.generation > generation_before, "the inhibit must fence the generation"
    assert transport.writes == [EncodedWrite(0x0200, (1, 0, 0))], "the inhibit bounded-zeros"
    assert any(reason == "identity_mismatch" for _, reason in authorizations.revocations)

    # A persisting mismatch holds the standing latch without re-fencing churn.
    await actor.accept_observation(
        replace(ObservationRecord(sequence=2), device_identity="BEP-SOMEONE-ELSE")
    )
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True

    # Correct identity alone never clears the latch: the unit stays inhibited
    # and cannot re-arm without the privileged acknowledgement.
    await actor.accept_observation(ObservationRecord(sequence=3))
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True
    with pytest.raises(RuntimeError):
        await actor.arm()

    # Acknowledgement clears only the latch; stable samples then reach
    # DISARMED and an explicit arm is still required.
    await actor.acknowledge_inhibit()
    assert actor.inhibit_latched is False
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    await actor.accept_observation(ObservationRecord(sequence=4))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED
    await actor.arm()
    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE

    # A protocol-profile contradiction latches exactly like the identity one.
    await actor.accept_observation(ObservationRecord(sequence=5, protocol_profile="iot-v2"))
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True
    await actor.shutdown()


async def test_quality_failure_without_identity_mismatch_never_latches(
    contract: Any,
) -> None:
    """Ordinary quality failures stay non-latched and recover automatically."""
    actor, _, transport, _, _ = make_actor(contract)
    await ready_actor(actor)

    await actor.accept_observation(replace(ObservationRecord(), quality="bad"))

    assert actor.lifecycle is contract.UnitLifecycle.OBSERVE_ONLY
    assert actor.inhibit_cause is None
    assert actor.inhibit_latched is False
    assert transport.writes == [], "a quality failure while disarmed writes nothing"

    await actor.accept_observation(ObservationRecord(sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED, (
        "recovery from a plain quality failure is automatic"
    )
    await actor.shutdown()


async def test_absent_identity_is_the_unknown_not_a_latched_mismatch(
    contract: Any,
) -> None:
    """A not-yet-observed identity fails qualification without latching."""
    actor, _, _, _, _ = make_actor(contract)
    await actor.start()

    await actor.accept_observation(replace(ObservationRecord(), device_identity=None))

    assert actor.lifecycle is contract.UnitLifecycle.OBSERVE_ONLY
    assert actor.inhibit_cause is None
    assert actor.inhibit_latched is False
    await actor.shutdown()


async def test_a_transient_inhibit_cannot_downgrade_a_standing_latch(
    contract: Any,
) -> None:
    """Defense in depth: only acknowledgement may clear a latched cause.

    Lifecycle ordering keeps the transient inhibit paths (heartbeat renewal)
    out of a latched unit today; this pins the invariant at the one recording
    point that could forget it.
    """
    actor, _, _, _, _ = make_actor(contract, blocking_fault_codes=frozenset({"Stack_Fault0_3"}))
    await ready_actor(actor)
    await actor.accept_observation(ObservationRecord(sequence=2, active_faults=("Stack_Fault0_3",)))
    assert actor.inhibit_latched is True

    actor._record_inhibit_cause(contract.InhibitCause.TRANSIENT, "transient_probe")

    assert actor.inhibit_latched is True, "only acknowledgement may clear a latch"
    assert actor.inhibit_cause is contract.InhibitCause.LATCHED
    await actor.shutdown()


async def test_request_bounded_zero_outranks_a_queued_heartbeat(contract: Any) -> None:
    """An externally requested bounded zero jumps the mailbox queue.

    The emergency zero is the facade's actuation of last resort; it may not
    sit behind a queued renewal that would write nonzero power first.
    """
    read_gate = Gate()
    transport = SpyTransport()
    transport.read_gate = read_gate
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
    )
    await ready_actor(actor)

    poll = asyncio.create_task(actor.poll_once())
    entered_read = await settle_until(read_gate.entered.is_set)
    if not entered_read:
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
    assert entered_read, "poll never reached the controlled read boundary"
    heartbeat = asyncio.create_task(actor.heartbeat_once())
    await asyncio.sleep(0)
    zero = asyncio.create_task(actor.request_bounded_zero("emergency_stop:stop-1"))
    await asyncio.sleep(0)
    read_gate.release.set()
    await asyncio.gather(poll, heartbeat, zero)

    assert transport.writes == [
        EncodedWrite(0x0200, (1, 0, 0)),
        EncodedWrite(0x0200, (1, 500, 0)),
    ], "the bounded zero must be written before the queued heartbeat's renewal"
    await actor.shutdown()


async def test_shutdown_attempts_bounded_zero_when_owner_is_dead(contract: Any) -> None:
    class FailingConnectTransport(SpyTransport):
        async def connect(self) -> None:
            raise OSError("gateway unreachable")

    transport = FailingConnectTransport()
    actor, _, _, _, _ = make_actor(contract, transport=transport)
    with pytest.raises(OSError, match="gateway unreachable"):
        await actor.start()
    assert actor.lifecycle is contract.UnitLifecycle.DISCONNECTED

    # Even with no mailbox task alive, shutdown owes one bounded zero attempt
    # before closing the transport.
    await actor.shutdown()
    assert transport.writes == [EncodedWrite(0x0200, (1, 0, 0))]
    assert transport.closed


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
    entered_write = await settle_until(gate.entered.is_set)
    if not entered_write:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_write, "heartbeat never reached the controlled write boundary"
    shutdown = asyncio.create_task(actor.shutdown())
    heartbeat_cancelled = await settle_until(heartbeat.done)
    gate.release.set()
    await asyncio.gather(heartbeat, shutdown, return_exceptions=True)

    assert heartbeat_cancelled, "shutdown did not cancel nonzero work before zeroing"
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
    entered_read = await settle_until(read_gate.entered.is_set)
    if not entered_read:
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
    assert entered_read, "poll never reached the controlled read boundary"
    heartbeat = asyncio.create_task(actor.heartbeat_once())
    clock.advance(0.81)
    heartbeat_finished = await settle_until(heartbeat.done)

    read_gate.release.set()
    await asyncio.gather(poll, heartbeat, return_exceptions=True)

    assert heartbeat_finished, "overdue telemetry blocked the heartbeat deadline"
    assert any(name == "read:cancelled" for name, _ in transport.history)
    assert transport.writes == [EncodedWrite(0x0200, (1, 500, 0))]
    history_names = [name for name, _ in transport.history]
    assert history_names.index("read:end") < history_names.index("write:start")
    assert transport.maximum_concurrency == 1
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


async def test_concurrent_shutdown_is_idempotent_and_stop_dominates_queued_renewal(
    contract: Any,
) -> None:
    lookup_gate = Gate(ignore_cancellation=True)
    repository = FakeAuthorizationRepository(
        AuthorizationRecord(),
        AuthorizationRecord(),
        current_gate=lookup_gate,
    )
    actor, _, transport, _, _ = make_actor(contract, authorizations=repository)
    await ready_actor(actor)

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    entered_lookup = await settle_until(lookup_gate.entered.is_set)
    if not entered_lookup:
        heartbeat.cancel()
        lookup_gate.release.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_lookup, "heartbeat never requested current authorization"
    first_shutdown = asyncio.create_task(actor.shutdown())
    second_shutdown = asyncio.create_task(actor.shutdown())
    stop_published = await settle_until(lambda: actor.generation > 0)
    lookup_gate.release.set()
    await asyncio.gather(heartbeat, first_shutdown, second_shutdown, return_exceptions=True)

    assert stop_published, "shutdown did not publish its generation fence"
    assert [write for write in transport.writes if write.values[1] != 0] == []
    assert transport.writes.count(EncodedWrite(0x0200, (1, 0, 0))) == 1
    assert [name for name, _ in transport.history].count("close:start") == 1
    assert actor.lifecycle is contract.UnitLifecycle.STOPPING
