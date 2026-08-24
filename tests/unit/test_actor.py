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
# The vendor's debug-mode readback word (DESIGN_POD_PARKING sections 4/5: the
# write sits at 0x8000, the readback at 0x8100 -- the asymmetry is pinned).
DEBUG_MODE_READBACK_ADDRESS = 0x8100


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
        # The named parking write (DESIGN_POD_PARKING section 5): every
        # attempt records (value, timeout_s) so the commissioned one-shot
        # budget's journey to the transport is observable.
        self.debug_mode_attempts: list[tuple[int, float | None]] = []
        self.debug_mode_writes: list[tuple[int, float | None]] = []
        self.debug_word = 0  # the 0x8100 readback word the spy serves

    async def connect(self) -> None:
        await self._operation("connect", None)

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        async def body() -> tuple[int, ...]:
            if self.read_gate is not None:
                await self.read_gate.wait()
            if address == DEBUG_MODE_READBACK_ADDRESS and count == 1:
                return (self.debug_word,)
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

    async def write_debug_mode(self, value: int, timeout_s: float | None = None) -> None:
        self.debug_mode_attempts.append((value, timeout_s))

        async def body() -> None:
            if self.write_gates:
                await self.write_gates.popleft().wait()
            if self.write_failures:
                raise self.write_failures.popleft()
            self.debug_word = value
            self.debug_mode_writes.append((value, timeout_s))

        await self._operation("debug_write", (value, timeout_s), body)

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
                "ArmRefused": actor_module.ArmRefused,
                "DebugModeChangeError": actor_module.DebugModeChangeError,
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
    debug_mode_readback_address: int | None = None,
    mode_write_timeout_s: float | None = None,
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
        debug_mode_readback_address=debug_mode_readback_address,
        mode_write_timeout_s=mode_write_timeout_s,
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


async def test_arm_refusals_name_their_distinct_conditions(contract: Any) -> None:
    """Arm-refusal de-conflation: the one conflated "not qualified" refusal is
    three named root causes, so the operator can tell WHICH condition to clear
    — acknowledge a latch, wait out the qualification window, or disarm a unit
    that is mid-autonomy/handback."""
    # Mid-autonomy/handback: an armed unit is qualified and unlatched but is
    # not DISARMED — re-arming it is a lifecycle refusal, not a qualification
    # one.
    armed, _, _, _, _ = make_actor(contract)
    await ready_actor(armed)
    with pytest.raises(contract.ArmRefused) as armed_refusal:
        await armed.arm()
    assert armed_refusal.value.reason == "unit_not_disarmed"
    await armed.shutdown()

    # Safety qualification window: observe-only with nothing stable yet keeps
    # the long-pinned refusal message, now with its own reason word.
    fresh, _, _, _, _ = make_actor(contract)
    await fresh.start()
    with pytest.raises(contract.ArmRefused) as unqualified:
        await fresh.arm()
    assert unqualified.value.reason == "insufficient_stable_observations"
    assert "not qualified for arming" in str(unqualified.value)
    await fresh.shutdown()

    # The latch dominates: a latched unit is refused for the latch (the
    # privileged acknowledgement is the only exit), never folded into the
    # qualification or lifecycle words.
    latched, _, _, _, _ = make_actor(contract, blocking_fault_codes=frozenset({"Stack_Fault0_3"}))
    await ready_actor(latched)
    await latched.accept_observation(
        ObservationRecord(sequence=2, active_faults=("Stack_Fault0_3",))
    )
    assert latched.inhibit_latched is True
    with pytest.raises(contract.ArmRefused) as latched_refusal:
        await latched.arm()
    assert latched_refusal.value.reason == "inhibit_latched"
    assert "blocking_fault_active" in str(latched_refusal.value)
    await latched.shutdown()


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


# --- DESIGN_POD_PARKING sections 4/5: the named mode write's OWN budget ----------
#
# The 2026-08-24 live smoke: the gateway's FC16-to-0x8000 turnaround outran
# the cadence-commissioned write timeout (0.50 s), so the parking WRITE leg
# runs under its own commissioned budget (timing.mode_write_timeout_s) and
# hands that budget to the named transport write as the one FC16's per-op
# client timeout.  The prior read and the readback keep the margin.


async def test_the_named_mode_write_carries_its_own_commissioned_budget(
    contract: Any,
) -> None:
    actor, _, transport, _, _ = make_actor(
        contract,
        debug_mode_readback_address=DEBUG_MODE_READBACK_ADDRESS,
        mode_write_timeout_s=2.0,
    )
    await actor.start()

    outcome = await actor.request_debug_mode_change(1)

    assert outcome == {
        "prior_word": 0,
        "written_value": 1,
        "readback_word": 1,
        "verified": True,
        "retries": 0,
    }
    assert transport.debug_mode_writes == [(1, 2.0)], (
        "the commissioned budget must ride the named transport write"
    )
    reads = [detail for name, detail in transport.history if name == "read:start"]
    assert (DEBUG_MODE_READBACK_ADDRESS, 1) in reads, (
        "the prior read and the readback still run through the ordinary read path"
    )
    await actor.shutdown()


async def test_an_unwired_mode_write_budget_keeps_the_call_exact(contract: Any) -> None:
    """``None`` (the default) changes nothing: the write leg keeps the
    heartbeat-margin bound and the transport call carries no override."""
    actor, _, transport, _, _ = make_actor(
        contract, debug_mode_readback_address=DEBUG_MODE_READBACK_ADDRESS
    )
    await actor.start()

    outcome = await actor.request_debug_mode_change(0)

    assert outcome["verified"] is True
    assert transport.debug_mode_writes == [(0, None)]
    await actor.shutdown()


async def test_a_mode_write_outrunning_its_budget_refuses_as_write_failed(
    contract: Any,
) -> None:
    """The budget bounds the write leg itself: a write that never completes
    inside it is cancelled BY it (the live-smoke refusal class), retried once,
    and refused with the typed write_failed facts."""
    actor, _, transport, _, _ = make_actor(
        contract,
        debug_mode_readback_address=DEBUG_MODE_READBACK_ADDRESS,
        mode_write_timeout_s=0.05,
    )
    await actor.start()
    stalled_first = Gate()
    stalled_retry = Gate()
    transport.write_gates.extend((stalled_first, stalled_retry))

    with pytest.raises(contract.DebugModeChangeError) as caught:
        await actor.request_debug_mode_change(1)

    assert caught.value.reason == "write_failed"
    assert caught.value.details["error_class"] == "TimeoutError"
    assert stalled_first.cancelled.is_set() and stalled_retry.cancelled.is_set()
    assert transport.debug_mode_attempts == [(1, 0.05), (1, 0.05)], (
        "both bounded attempts carry the commissioned budget"
    )
    assert transport.debug_mode_writes == []
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


# --- the 2026-08-24 15:20:32 incident: mid-run reconnect policy ------------------
#
# One OSError on an established Waveshare session made a unit permanently
# dark: the transport's resync correctly closed and rebuilt its client (and
# set connectionless), but nothing ever called connect() again -- every later
# poll failed the instant not-connected check until process restart while the
# gateway kept accepting fresh connections, so the console's "unreachable"
# label was false.  The transport doctrine ("reconnect and retry policy
# belongs to the generation-fenced unit actor") is now implemented: a poll
# failing with the connection class schedules ONE bounded reconnect through
# the actor's own mailbox, and the next poll rides the fresh socket.


class SessionTransport:
    """A Waveshare-shaped double: one session, resync-on-failure, rebuild.

    Mirrors the production transport the policy rides on
    (adapters/modbus/waveshare.py): ``connect()`` opens the session; an
    operation failure on the connection class resyncs -- the session is
    marked connectionless and the underlying CLIENT IS REBUILT (the live
    15:20:32 shape) -- and while connectionless every operation raises the
    instant not-connected connection error the fleet classifies
    CONNECT_FAILED.  ``gateway_down`` models a gateway refusing fresh
    connections, and ``client_epoch`` identifies which client generation an
    attempt or read rode.
    """

    def __init__(self) -> None:
        self.client_epoch = 0
        self.connected = False
        self.closed = False
        self.gateway_down = False
        self.fail_next_reads = False
        self.read_gate: Gate | None = None
        self.connect_attempts: list[int] = []
        self.reads: list[tuple[int, tuple[int, int]]] = []
        self.writes: list[tuple[int, tuple[int, int]]] = []

    async def connect(self) -> None:
        self.connect_attempts.append(self.client_epoch)
        if self.closed or self.gateway_down:
            raise ConnectionError("unable to connect to Waveshare gateway")
        self.connected = True

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        if self.read_gate is not None:
            await self.read_gate.wait()
        if not self.connected:
            raise ConnectionError("Waveshare transport is not connected")
        if self.fail_next_reads:
            self._resync_after_failure()
            raise ConnectionError("connection lost during Modbus read")
        self.reads.append((self.client_epoch, (address, count)))
        return tuple(0 for _ in range(count))

    async def write_registers(self, address: int, values: tuple[int, ...]) -> None:
        if not self.connected:
            raise ConnectionError("Waveshare transport is not connected")
        self.writes.append((self.client_epoch, (address, tuple(values))))

    def _resync_after_failure(self) -> None:
        self.connected = False
        self.fail_next_reads = False
        self.client_epoch += 1

    async def close(self) -> None:
        self.closed = True
        self.connected = False


async def test_a_midrun_connection_failure_reconnects_without_restart(
    contract: Any,
) -> None:
    """The live incident, end to end: a single transient TCP failure darkens
    the unit for exactly the cycles the gateway is unreachable -- never until
    process restart.  The failed poll still reports the connection-failure
    class (the fleet's honest CONNECT_FAILED classification is unchanged);
    the actor then schedules one bounded reconnect through its own mailbox on
    the REBUILT client, and the next poll reads OK."""
    transport = SessionTransport()
    actor, _, _, _, _ = make_actor(contract, transport=transport)

    await actor.start()
    assert transport.connect_attempts == [0], "boot connects exactly once, unchanged"
    await actor.poll_once()
    assert transport.reads == [(0, (0x5000, 7))]

    # The gateway drops the established session mid-run (the 15:20:32 shape).
    transport.fail_next_reads = True
    with pytest.raises(ConnectionError, match="connection lost"):
        await actor.poll_once()
    assert transport.client_epoch == 1, "the transport resynced and rebuilt its client"

    # Recovery without restart: exactly one reconnect lands on the rebuilt
    # client generation.
    assert await settle_until(lambda: len(transport.connect_attempts) >= 2)
    assert transport.connect_attempts == [0, 1]

    essential = await actor.poll_once()
    assert essential == (0, 0, 0, 0, 0, 0, 0)
    assert transport.reads[-1] == (1, (0x5000, 7)), "the poll rode the fresh client"
    await actor.shutdown()


async def test_a_persisting_outage_stays_honest_and_bounded(contract: Any) -> None:
    """While the gateway truly refuses fresh connections, every poll keeps
    reporting the instant not-connected class (the honest gateway-unreachable
    classification stands), each poll cycle schedules EXACTLY ONE connect
    attempt, and nothing is scheduled between polls -- no busy-loop.  The
    moment the path is restored, the next cycle's attempt reconnects and the
    following poll reads OK."""
    transport = SessionTransport()
    actor, _, _, _, _ = make_actor(contract, transport=transport)
    await actor.start()

    # The gateway drops the session AND refuses fresh connections: the
    # reconnect attempt the failing poll schedules must fail too.
    transport.fail_next_reads = True
    transport.gateway_down = True
    with pytest.raises(ConnectionError, match="connection lost"):
        await actor.poll_once()
    assert await settle_until(lambda: len(transport.connect_attempts) >= 2), (
        "the failed poll must still have scheduled its one reconnect attempt"
    )

    # The attempt failed; with no new poll arriving, nothing more schedules.
    for _ in range(20):
        await asyncio.sleep(0)
    assert transport.connect_attempts == [0, 1]

    # Two more fleet cycles against the down gateway: one attempt per cycle,
    # every poll reporting the connect-failure class.
    for expected_attempts in (3, 4):
        with pytest.raises(ConnectionError, match="not connected"):
            await actor.poll_once()
        assert await settle_until(
            lambda expected=expected_attempts: len(transport.connect_attempts) >= expected
        )
    assert transport.connect_attempts == [0, 1, 1, 1], "exactly one attempt per cycle"

    # The path is restored: the next cycle's single attempt reconnects and
    # the poll after it reads OK -- recovery without restart.
    transport.gateway_down = False
    with pytest.raises(ConnectionError, match="not connected"):
        await actor.poll_once()
    assert await settle_until(lambda: len(transport.connect_attempts) >= 5)
    await actor.poll_once()
    assert transport.reads[-1] == (1, (0x5000, 7))
    await actor.shutdown()


async def test_a_stale_generation_s_late_connect_is_a_noop(contract: Any) -> None:
    """The reconnect reuses the authority generation fence.  A higher-priority
    operation that fences the generation between the failed poll and the
    attempt's dispatch (the live shape: the fleet cycle's heartbeat write
    fails on the same dead socket and inhibits with a generation advance)
    makes the scheduled attempt a NO-OP -- no connect rides a stale epoch --
    and the live generation's own next failed poll schedules the fresh
    attempt that reconnects."""
    transport = SessionTransport()
    repository = FakeAuthorizationRepository(AuthorizationRecord())
    actor, _, _, _, _ = make_actor(
        contract,
        transport=transport,
        observations=FakeObservationRepository(ObservationRecord()),
        authorizations=repository,
    )
    await ready_actor(actor)

    # Hold the poll's read in flight, then queue the heartbeat behind it: the
    # mailbox will dispatch the heartbeat (priority above the reconnect)
    # between the failed poll and the reconnect it schedules.
    gate = Gate()
    transport.read_gate = gate
    poll = asyncio.create_task(actor.poll_once())
    assert await settle_until(gate.entered.is_set)
    heartbeat = asyncio.create_task(actor.heartbeat_once())
    await asyncio.sleep(0)  # let the heartbeat message land in the mailbox

    transport.fail_next_reads = True
    gate.release.set()
    await asyncio.gather(poll, heartbeat, return_exceptions=True)
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_reason == "write_failed"
    assert actor.generation == 1, "the heartbeat's write failure fenced the generation"

    # The scheduled reconnect carried generation 0: it must dispatch as a
    # no-op and never touch the rebuilt client.
    for _ in range(10):
        await asyncio.sleep(0)
    assert transport.connect_attempts == [0], "a stale generation's late connect"

    # The live generation recovers on its own failed poll's fresh schedule.
    with pytest.raises(ConnectionError, match="not connected"):
        await actor.poll_once()
    assert await settle_until(lambda: len(transport.connect_attempts) >= 2)
    assert transport.connect_attempts == [0, 1]
    await actor.poll_once()
    assert transport.reads[-1] == (1, (0x5000, 7))
    await actor.shutdown()


class _SessionRegisters:
    """The pymodbus response surface ``WaveshareTransport`` validates."""

    def __init__(self, *, function_code: int, count: int, dev_id: int) -> None:
        self.function_code = function_code
        self.registers = [0] * count
        self.dev_id = dev_id

    def isError(self) -> bool:
        return False


class _SessionClient:
    """The pymodbus client surface the real transport drives, scripted."""

    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host = host
        self.constructor_kwargs = kwargs
        self.connected = False
        self.closed = False
        self.read_error: BaseException | None = None
        self.read_calls = 0

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _SessionRegisters:
        self.read_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return _SessionRegisters(function_code=3, count=count, dev_id=device_id)

    def close(self) -> None:
        self.closed = True
        self.connected = False


class _SessionClientFactory:
    """Records every client the transport constructs, including resyncs."""

    def __init__(self) -> None:
        self.clients: list[_SessionClient] = []

    def __call__(self, host: str, **kwargs: Any) -> _SessionClient:
        client = _SessionClient(host, **kwargs)
        self.clients.append(client)
        return client


async def test_the_real_transport_reconnects_through_the_actor_after_a_midrun_failure(
    contract: Any,
) -> None:
    """The wire-true variant: the REAL WaveshareTransport (with its resync
    client rebuild) under the real actor.  One OSError on an established
    session resyncs and leaves the transport connectionless; the actor's
    bounded reconnect connects the rebuilt client and the next poll reads
    through it -- the exact sequence that stayed dark until restart before
    the policy existed."""
    waveshare = importlib.import_module("energypod.adapters.modbus.waveshare")
    factory = _SessionClientFactory()
    transport = waveshare.WaveshareTransport(
        config=waveshare.WaveshareTransportConfig(host="192.168.1.11"),
        client_factory=factory,
    )
    actor, _, _, _, _ = make_actor(contract, transport=transport)

    await actor.start()
    await actor.poll_once()
    assert len(factory.clients) == 1
    assert factory.clients[0].read_calls == 1

    # The established session dies mid-run.
    factory.clients[0].read_error = ConnectionResetError("synthetic reset")
    with pytest.raises(waveshare.TransportConnectionError, match="connection lost"):
        await actor.poll_once()
    assert factory.clients[0].closed is True
    assert len(factory.clients) == 2, "the resync rebuilt the client from the factory"

    assert await settle_until(lambda: factory.clients[1].connected), (
        "the actor must reconnect the rebuilt client without any restart"
    )
    await actor.poll_once()
    assert factory.clients[1].read_calls == 1, "the poll rode the fresh socket"
    await actor.shutdown()
