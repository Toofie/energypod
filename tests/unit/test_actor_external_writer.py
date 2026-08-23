"""Contract tests for the arm-time external-writer preflight.

API_CONTRACTS "Write-enabled run mode (live control)", bullet 3: at arm time
(transition into ARMED_IDLE) the actor reads the served PQ objective readback
(IoT 0x1060+17/+18) through its own transport.  Any nonzero objective it did
not itself write latches INHIBITED with cause ``external_writer`` (privileged
acknowledgement required, re-latching while the foreign objective persists);
an unreadable readback refuses the arm fail-closed.  Single-writer authority
is structural, not assumed.

Pinned read shape: the actor performs the read itself, inside the arm
mailbox dispatch, through the one transport it owns (the contract's "Unit
actor: one EnergyPodActor owns one transport").  The preflight is enabled by
one optional constructor port carrying the readback window's base address
(``objective_readback_address``); ``None`` (the default) keeps the arm path
exactly as it is today, so observe-only wiring is unchanged.

Production modules are loaded inside a fixture so this test-first suite
collects before implementation exists.  Missing contracts are reported as
ordinary test failures.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

UNIT_ID = "mid"
IDENTITY = "BEP0005KXX11B10500055"
PROFILE = "iot-v1"
SITE_ID = "site-1"

# The served PQ objective readback: IoT PCS block base 0x1060 plus the
# active/reactive power objectives at offsets +17/+18 (PROTOCOL_EVIDENCE 4b:
# the live -200 W commissioning write read back immediately at 0x1060+17).
OBJECTIVE_READBACK_ADDRESS = 0x1060 + 17
OBJECTIVE_READBACK_COUNT = 2
PQ_WRITE_ADDRESS = 0x0200
# -200 W as the firmware serves it: two's-complement int16 (live trial).
CHARGE_OBJECTIVE_RAW = 0xFF38  # 65336 == int16 -200


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


class SpyTransport:
    """The actor-suite transport fake, serving the objective readback window.

    Reads at ``OBJECTIVE_READBACK_ADDRESS`` serve the configured PQ pair
    (raw int16 words, exactly as the firmware would); any other address
    serves zeros.  ``objective_read_error`` models an unreadable readback.
    """

    def __init__(self) -> None:
        self.history: list[tuple[str, Any]] = []
        self.operation_tasks: list[tuple[str, asyncio.Task[Any] | None]] = []
        self.write_attempts: list[EncodedWrite] = []
        self.writes: list[EncodedWrite] = []
        self.active_operations = 0
        self.maximum_concurrency = 0
        self.closed = False
        self.objective: tuple[int, int] = (0, 0)
        self.objective_read_error: BaseException | None = None

    async def connect(self) -> None:
        await self._operation("connect", None, None)

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        async def body() -> tuple[int, ...]:
            if address == OBJECTIVE_READBACK_ADDRESS:
                if self.objective_read_error is not None:
                    raise self.objective_read_error
                return (*self.objective, 0, 0)[:count]
            return tuple(0 for _ in range(count))

        return await self._operation("read", (address, count), body)

    async def write_registers(self, address: int, values: tuple[int, ...]) -> None:
        request = EncodedWrite(address, tuple(values))
        self.write_attempts.append(request)

        async def body() -> None:
            self.writes.append(request)

        await self._operation("write", request, body)

    async def close(self) -> None:
        async def body() -> None:
            self.closed = True

        await self._operation("close", None, body)

    async def _operation(
        self,
        name: str,
        detail: Any,
        body: Callable[[], Any] | None,
    ) -> Any:
        self.active_operations += 1
        self.maximum_concurrency = max(self.maximum_concurrency, self.active_operations)
        self.history.append((f"{name}:start", detail))
        self.operation_tasks.append((name, asyncio.current_task()))
        try:
            if body is None:
                return None
            return await body()
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

    async def all_latest(self) -> dict[str, ObservationRecord | None]:
        return {UNIT_ID: self.value}

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

    async def peek(self, unit_id: str) -> AuthorizationRecord | None:
        return None

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

    async def recent(self, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]:
        return ()


class FakeIntentRepository:
    def __init__(self) -> None:
        self.active_calls: list[float] = []

    async def add(self, intent: Any) -> None:
        return None

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        self.active_calls.append(now_mono)
        return ()

    async def remove(self, intent_id: str) -> None:
        return None


class FakeEventBus:
    def __init__(self) -> None:
        self.sequence = 0
        self.published: list[dict[str, Any]] = []

    async def publish(self, body: Any) -> int:
        self.sequence += 1
        self.published.append(dict(body))
        return self.sequence

    def snapshot_sequence(self) -> int:
        return self.sequence


class FakeCommandEncoder:
    def encode(self, authorization: AuthorizationRecord) -> EncodedWrite:
        sign = (
            -1
            if getattr(authorization.direction, "value", authorization.direction) == "charge"
            else 1
        )
        return EncodedWrite(
            PQ_WRITE_ADDRESS, (1, sign * authorization.watts, authorization.reactive_vars)
        )

    def zero(self) -> EncodedWrite:
        return EncodedWrite(PQ_WRITE_ADDRESS, (1, 0, 0))


@dataclass(frozen=True)
class OperatorPrincipal:
    subject: str = "operator"
    scopes: frozenset[str] = frozenset({"observe", "arm"})
    interactive: bool = True
    site_id: str = SITE_ID


@pytest.fixture
def contract() -> Any:
    try:
        actor_module = importlib.import_module("energypod.application.actor")
        domain_module = importlib.import_module("energypod.domain")
        service_module = importlib.import_module("energypod.application.service")
        generation_module = importlib.import_module("energypod.application.generation")
        return type(
            "Contract",
            (),
            {
                "EnergyPodActor": actor_module.EnergyPodActor,
                "InhibitCause": actor_module.InhibitCause,
                "UnitLifecycle": domain_module.UnitLifecycle,
                "EnergyServiceFacade": service_module.EnergyServiceFacade,
                "AuthorityGenerationCoordinator": (
                    generation_module.AuthorityGenerationCoordinator
                ),
            },
        )
    except (ImportError, AttributeError) as error:
        pytest.fail(
            f"external-writer preflight contract is not implemented: {error}", pytrace=False
        )


def make_actor(
    contract: Any,
    *,
    transport: SpyTransport | None = None,
    authorizations: FakeAuthorizationRepository | None = None,
    objective_readback_address: int | None = OBJECTIVE_READBACK_ADDRESS,
    autonomous_charge_signature_max_w: int | None = None,
) -> tuple[Any, SpyTransport, FakeAuthorizationRepository]:
    """One real actor over fakes; the preflight port defaults to the IoT window."""
    test_transport = transport or SpyTransport()
    authorization_repo = authorizations or FakeAuthorizationRepository()
    actor = contract.EnergyPodActor(
        unit_id=UNIT_ID,
        transport=test_transport,
        clock=FakeClock(),
        observations=FakeObservationRepository(),
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
        objective_readback_address=objective_readback_address,
        autonomous_charge_signature_max_w=autonomous_charge_signature_max_w,
    )
    return actor, test_transport, authorization_repo


async def qualify_disarmed(contract: Any, actor: Any) -> None:
    """Start the actor and drive it to a qualified DISARMED state."""
    await actor.start()
    await actor.accept_observation(ObservationRecord())
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED


def objective_reads(transport: SpyTransport) -> list[tuple[int, int]]:
    """The arm-preflight readback windows the actor actually requested."""
    return [
        detail
        for name, detail in transport.history
        if name == "read:start" and detail[0] == OBJECTIVE_READBACK_ADDRESS
    ]


def nonzero_pq_writes(transport: SpyTransport) -> list[EncodedWrite]:
    return [write for write in transport.writes if write.values != (1, 0, 0)]


def make_facade(contract: Any, actor: Any, audit: FakeAuditRepository | None = None) -> Any:
    """The real facade over the real actor; repositories stay fakes."""
    return contract.EnergyServiceFacade(
        site_id=SITE_ID,
        clock=FakeClock(),
        intents=FakeIntentRepository(),
        observations=FakeObservationRepository(),
        authorizations=FakeAuthorizationRepository(),
        audit=audit or FakeAuditRepository(),
        events=FakeEventBus(),
        coordinator=contract.AuthorityGenerationCoordinator(),
        actors={UNIT_ID: actor},
    )


# --- foreign objective: refused arm, latched cause, no PQ write --------------


@pytest.mark.parametrize(
    ("objective", "label"),
    [
        ((500, 0), "foreign_discharge_objective"),
        ((CHARGE_OBJECTIVE_RAW, 0), "foreign_charge_objective_minus_200_w"),
        ((0, 300), "foreign_reactive_objective"),
    ],
    ids=["discharge_p", "charge_p_twos_complement", "reactive_q"],
)
async def test_foreign_objective_refuses_arm_and_latches_external_writer(
    contract: Any, objective: tuple[int, int], label: str
) -> None:
    transport = SpyTransport()
    transport.objective = objective
    actor, transport, authorizations = make_actor(contract, transport=transport)
    await qualify_disarmed(contract, actor)
    generation_before = actor.generation

    with pytest.raises(RuntimeError):
        await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_cause is contract.InhibitCause.LATCHED
    assert actor.inhibit_latched is True, f"{label}: the external writer must be latched"
    assert actor.qualified is False
    assert actor.generation > generation_before, "the inhibit must fence the generation"
    assert any(reason == "external_writer" for _, reason in authorizations.revocations), (
        "the latched cause string 'external_writer' must be observable to operators"
    )
    assert nonzero_pq_writes(transport) == [], "a foreign objective must never be over-written"
    assert all(write.address == PQ_WRITE_ADDRESS for write in transport.writes), (
        "no register outside the commissioned PQ window may be written"
    )
    reads = objective_reads(transport)
    assert len(reads) == 1, "arm must preflight the served objective exactly once"
    assert reads[0][0] == OBJECTIVE_READBACK_ADDRESS
    assert reads[0][1] >= OBJECTIVE_READBACK_COUNT, "the preflight must cover both P and Q"

    # Acknowledgement clears only the latch; recovery then reaches DISARMED,
    # but the persisting foreign objective re-latches on the next arm attempt.
    await actor.acknowledge_inhibit()
    assert actor.inhibit_latched is False
    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    await actor.accept_observation(ObservationRecord(sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED

    with pytest.raises(RuntimeError):
        await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    assert actor.inhibit_latched is True, "a persisting foreign objective re-latches"
    assert len(objective_reads(transport)) == 2
    assert nonzero_pq_writes(transport) == []
    await actor.shutdown()


# --- ADD-1 (2026-08-24): pod-autonomy discrimination at the arm preflight -----
#
# The live blocker: after any restart while a pod AUTONOMOUSLY self-charges,
# its own firmware holds a nonzero PQ objective (~-2.2 kW measured; the fresh
# process has no provenance), so the preflight latched external_writer and
# the watchdog reverted every bounded zero in ~1.34 s vs ~5-6 s
# re-qualification -- ~40 failed arms over 25 min while the pod charged
# itself at -2.2 kW throughout.  Two remedy layers:
#   (a) AUTONOMY-SIGNATURE DISCRIMINATION -- a nonzero readback whose
#       sign/magnitude matches the pod's evidenced self-consumption signature
#       (negative P, within the commissioned band, Q zero) that this process
#       did not write is classified POD AUTONOMY, not foreign: the arm
#       proceeds and our renewed objective replaces autonomy (beat-autonomy).
#   (b) OPERATOR-ACKNOWLEDGED TAKEOVER -- arm accepts an explicit takeover
#       acknowledgement for objectives OUTSIDE the signature band; audited.
# Anything else still latches external_writer exactly as before, and boot
# stays observe-only with NO provenance persistence across restarts.


def raw_int16(value: int) -> int:
    """One signed objective word exactly as the firmware serves it."""
    return value & 0xFFFF


async def test_a_pod_autonomy_signature_arms_instead_of_latching(contract: Any) -> None:
    """The exact live shape: mid self-charging at ~-2.2 kW across a restart."""
    transport = SpyTransport()
    transport.objective = (raw_int16(-2275), 0)
    actor, transport, authorizations = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
        autonomous_charge_signature_max_w=2500,
    )
    await qualify_disarmed(contract, actor)

    await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE, (
        "the pod's own self-charge objective is POD AUTONOMY, not a foreign writer"
    )
    assert actor.inhibit_latched is False
    assert actor.last_arm_classification == "pod_autonomy"
    assert all(reason != "external_writer" for _, reason in authorizations.revocations)
    # Beat-autonomy: our first renewed objective replaces the pod's own.
    await actor.heartbeat_once()
    assert transport.writes == [EncodedWrite(PQ_WRITE_ADDRESS, (1, 500, 0))]
    await actor.shutdown()


@pytest.mark.parametrize(
    ("objective", "label"),
    [
        ((500, 0), "discharge_objective_is_not_the_self_charge_signature"),
        ((raw_int16(-3000), 0), "charge_objective_beyond_the_commissioned_band"),
        ((0, 300), "reactive_objective_is_not_the_signature"),
        ((raw_int16(-200), 300), "signature_p_with_foreign_q"),
    ],
    ids=["discharge_p", "beyond_band_p", "reactive_q", "p_in_band_q_nonzero"],
)
async def test_objectives_outside_the_signature_still_latch_external_writer(
    contract: Any, objective: tuple[int, int], label: str
) -> None:
    transport = SpyTransport()
    transport.objective = objective
    actor, transport, authorizations = make_actor(
        contract,
        transport=transport,
        autonomous_charge_signature_max_w=2500,
    )
    await qualify_disarmed(contract, actor)

    with pytest.raises(RuntimeError):
        await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED, label
    assert actor.inhibit_latched is True
    assert any(reason == "external_writer" for _, reason in authorizations.revocations)
    assert nonzero_pq_writes(transport) == []
    await actor.shutdown()


async def test_signature_discrimination_needs_the_commissioned_band(contract: Any) -> None:
    """Without the configured band the preflight keeps today's exact
    behavior: every nonzero objective this process did not write is foreign."""
    transport = SpyTransport()
    transport.objective = (raw_int16(-520), 0)
    actor, transport, _ = make_actor(contract, transport=transport)
    await qualify_disarmed(contract, actor)

    with pytest.raises(RuntimeError):
        await actor.arm()

    assert actor.inhibit_latched is True
    await actor.shutdown()


async def test_an_acknowledged_takeover_arms_and_is_audited(contract: Any) -> None:
    """Layer (b): a beyond-band objective arms under an explicit operator
    takeover acknowledgement, and the classification is observable for the
    audit trail.  Unacknowledged, the same objective still latches."""
    transport = SpyTransport()
    transport.objective = (500, 0)  # a foreign discharge objective
    actor, transport, authorizations = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord(observation_sequence=2)),
        autonomous_charge_signature_max_w=2500,
    )
    del authorizations
    await qualify_disarmed(contract, actor)

    with pytest.raises(RuntimeError):
        await actor.arm(takeover_acknowledged=False)
    assert actor.inhibit_latched is True

    await actor.acknowledge_inhibit()
    await actor.accept_observation(ObservationRecord(sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED

    await actor.arm(takeover_acknowledged=True)

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    assert actor.last_arm_classification == "takeover_acknowledged"
    # The takeover replaced nothing on its own: only our own renewed
    # authority ever writes, exactly as any armed unit (the pod-autonomy test
    # above pins the renewal write itself).
    assert nonzero_pq_writes(transport) == []
    await actor.shutdown()


async def test_the_facade_requires_the_takeover_acknowledgement_and_audits_it(
    contract: Any,
) -> None:
    """The facade threads the REST acknowledgement and audits the takeover."""
    transport = SpyTransport()
    transport.objective = (raw_int16(-3000), 0)  # beyond the band
    actor, transport, _ = make_actor(
        contract,
        transport=transport,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
        autonomous_charge_signature_max_w=2500,
    )
    audit = FakeAuditRepository()
    bus = FakeEventBus()
    facade = contract.EnergyServiceFacade(
        site_id=SITE_ID,
        clock=FakeClock(),
        intents=FakeIntentRepository(),
        observations=FakeObservationRepository(),
        authorizations=FakeAuthorizationRepository(),
        audit=audit,
        events=bus,
        coordinator=contract.AuthorityGenerationCoordinator(),
        actors={UNIT_ID: actor},
    )
    await actor.start()
    await actor.accept_observation(ObservationRecord())

    refused = await facade.arm(
        unit_ids=[UNIT_ID],
        principal=OperatorPrincipal(),
        idempotency_key="takeover-key-1",
        request_id="takeover-request-1",
    )
    assert refused["units"][0]["status"] == "refused"
    assert refused["units"][0]["reason"] == "inhibit_latched"

    await actor.acknowledge_inhibit()
    await actor.accept_observation(ObservationRecord(sequence=2))
    armed = await facade.arm(
        unit_ids=[UNIT_ID],
        principal=OperatorPrincipal(),
        idempotency_key="takeover-key-2",
        request_id="takeover-request-2",
        takeover="ACKNOWLEDGE",
    )
    assert armed["units"][0]["status"] == "armed", armed
    assert armed["units"][0]["objective_classification"] == "takeover_acknowledged"

    # The takeover reaches BOTH durable surfaces: the audit row's reason
    # codes name it, and the published event carries the classification.
    events = [event for event in audit.events if event.event_type == "unit_armed"]
    assert events, "the takeover must reach the audit trail"
    assert any(
        "arm_takeover_acknowledged" in event.reason_codes
        for event in events
        if event.result == "armed"
    ), [event.reason_codes for event in events]
    published = [body for body in bus.published if body.get("type") == "unit.armed"]
    assert any(
        unit.get("objective_classification") == "takeover_acknowledged"
        for body in published
        for unit in body.get("payload", {}).get("units", [])
    ), published
    await actor.shutdown()


async def test_an_invalid_takeover_spelling_is_refused_before_any_arm(contract: Any) -> None:
    actor, _, _ = make_actor(contract)
    await actor.start()
    await actor.accept_observation(ObservationRecord())
    facade = make_facade(contract, actor)
    with pytest.raises(ValueError, match="takeover"):
        await facade.arm(
            unit_ids=[UNIT_ID],
            principal=OperatorPrincipal(),
            idempotency_key="takeover-key-3",
            request_id="takeover-request-3",
            takeover="YES",
        )
    await actor.shutdown()


# --- clean objective: arm proceeds, preflight is arm-gated -------------------


async def test_zero_objective_arms_and_the_preflight_is_arm_gated(
    contract: Any,
) -> None:
    first = AuthorizationRecord(cycle_id=10, watts=400)
    second = AuthorizationRecord(cycle_id=11, watts=600)
    repository = FakeAuthorizationRepository(first, second)
    actor, transport, _ = make_actor(contract, authorizations=repository)
    await qualify_disarmed(contract, actor)

    await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    await actor.heartbeat_once()
    await actor.heartbeat_once()

    assert transport.writes == [
        EncodedWrite(PQ_WRITE_ADDRESS, (1, 400, 0)),
        EncodedWrite(PQ_WRITE_ADDRESS, (1, 600, 0)),
    ], "a clean readback must not change the renewal write path"
    assert transport.maximum_concurrency == 1, "the preflight read stays inside the mailbox"

    # The preflight is arm-gated, not per-heartbeat: one arm performed exactly
    # one objective read while two heartbeats performed none.
    reads = objective_reads(transport)
    assert len(reads) == 1
    assert reads[0][1] >= OBJECTIVE_READBACK_COUNT
    history_names = [name for name, _ in transport.history]
    first_read = history_names.index("read:start")
    first_write = history_names.index("write:start")
    assert first_read < first_write, "the preflight read precedes any PQ write"

    # A second arm performs a second preflight; heartbeats between them none.
    await actor.disarm()
    await actor.arm()
    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    assert len(objective_reads(transport)) == 2
    await actor.shutdown()


async def test_absent_preflight_wiring_arms_without_an_objective_read(
    contract: Any,
) -> None:
    """The port is optional: observe-only wiring must be unchanged."""
    actor, transport, _ = make_actor(
        contract,
        objective_readback_address=None,
        authorizations=FakeAuthorizationRepository(AuthorizationRecord()),
    )
    await qualify_disarmed(contract, actor)

    await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    assert objective_reads(transport) == []
    await actor.heartbeat_once()
    assert transport.writes == [EncodedWrite(PQ_WRITE_ADDRESS, (1, 500, 0))]
    await actor.shutdown()


# --- unreadable readback: fail-closed refusal --------------------------------


async def test_unreadable_objective_readback_refuses_arm_fail_closed(
    contract: Any,
) -> None:
    transport = SpyTransport()
    transport.objective_read_error = OSError("objective readback unreadable")
    repository = FakeAuthorizationRepository()
    actor, transport, authorizations = make_actor(
        contract, transport=transport, authorizations=repository
    )
    await qualify_disarmed(contract, actor)
    generation_before = actor.generation

    with pytest.raises(RuntimeError):
        await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.INHIBITED
    # Pinned: the unreadable-readback refusal is non-latched (TRANSIENT), so a
    # readable-again readback recovers through stable samples without a
    # privileged acknowledgement.
    assert actor.inhibit_cause is contract.InhibitCause.TRANSIENT
    assert actor.inhibit_latched is False
    assert actor.generation > generation_before
    assert authorizations.revocations, "the refusal must revoke outstanding authority"
    assert nonzero_pq_writes(transport) == []
    assert len(objective_reads(transport)) == 1

    # Recovery: the readback becomes readable and zero, telemetry stays good.
    transport.objective_read_error = None
    await actor.accept_observation(ObservationRecord(sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED
    repository.responses.append(
        AuthorizationRecord(generation=actor.generation, observation_sequence=2, cycle_id=31)
    )

    await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    await actor.heartbeat_once()
    assert transport.writes[-1] == EncodedWrite(PQ_WRITE_ADDRESS, (1, 500, 0))
    await actor.shutdown()


# --- acknowledged latch with the objective now clear --------------------------


async def test_acknowledged_external_writer_latch_recovers_and_arms(
    contract: Any,
) -> None:
    transport = SpyTransport()
    transport.objective = (500, 0)
    repository = FakeAuthorizationRepository()
    actor, transport, authorizations = make_actor(
        contract, transport=transport, authorizations=repository
    )
    await qualify_disarmed(contract, actor)

    with pytest.raises(RuntimeError):
        await actor.arm()
    assert actor.inhibit_latched is True

    # The foreign writer goes away; acknowledgement then clears the latch and
    # the ordinary stable-sample recovery plus explicit arm succeed.
    transport.objective = (0, 0)
    await actor.acknowledge_inhibit()
    assert actor.inhibit_latched is False
    await actor.accept_observation(ObservationRecord(sequence=2))
    assert actor.lifecycle is contract.UnitLifecycle.DISARMED
    repository.responses.append(
        AuthorizationRecord(generation=actor.generation, observation_sequence=2, cycle_id=21)
    )

    await actor.arm()

    assert actor.lifecycle is contract.UnitLifecycle.ARMED_IDLE
    await actor.heartbeat_once()
    assert transport.writes[-1] == EncodedWrite(PQ_WRITE_ADDRESS, (1, 500, 0)), (
        "a recovered unit renews its own objective normally"
    )
    assert any(reason == "external_writer" for _, reason in authorizations.revocations)
    await actor.shutdown()


# --- facade visibility --------------------------------------------------------


async def test_external_writer_latch_is_visible_through_the_facade_surface(
    contract: Any,
) -> None:
    """The preflight result reaches operators through the existing surface.

    The snapshot projects the inhibited lifecycle exactly as the other
    latched causes do, the facade refuses further arming with the standing
    latch reason, and the privileged acknowledgement endpoint clears it.
    """
    transport = SpyTransport()
    transport.objective = (500, 0)
    actor, transport, _ = make_actor(contract, transport=transport)
    operator = OperatorPrincipal()
    facade = make_facade(contract, actor)
    await qualify_disarmed(contract, actor)

    clean = await facade.snapshot(principal=operator)
    assert clean["units"][0]["lifecycle"] == "disarmed"

    first_arm = await facade.arm(
        unit_ids=[UNIT_ID],
        principal=operator,
        idempotency_key="external-writer-arm-1",
        request_id="external-writer-arm-1",
    )
    assert first_arm["units"][0]["status"] == "refused"

    snapshot = await facade.snapshot(principal=operator)
    assert snapshot["units"][0]["lifecycle"] == "inhibited"
    health = await facade.health(principal=operator)
    assert f"{UNIT_ID}:inhibit_latched" in health["control_readiness"]["reasons"]

    second_arm = await facade.arm(
        unit_ids=[UNIT_ID],
        principal=operator,
        idempotency_key="external-writer-arm-2",
        request_id="external-writer-arm-2",
    )
    assert second_arm["units"][0]["status"] == "refused"
    assert second_arm["units"][0]["reason"] == "inhibit_latched"

    acknowledged = await facade.acknowledge_inhibit(
        unit_id=UNIT_ID,
        principal=operator,
        idempotency_key="external-writer-ack-1",
        request_id="external-writer-ack-1",
    )
    assert acknowledged["latch_cleared"] is True
    assert actor.inhibit_latched is False

    # Acknowledgement never bypasses recovery: the unit stays inhibited until
    # stable qualifying samples return it to DISARMED.
    after_ack = await facade.snapshot(principal=operator)
    assert after_ack["units"][0]["lifecycle"] == "inhibited"
    await actor.accept_observation(ObservationRecord(sequence=2))
    recovered = await facade.snapshot(principal=operator)
    assert recovered["units"][0]["lifecycle"] == "disarmed"
    assert nonzero_pq_writes(transport) == []
    await actor.shutdown()
