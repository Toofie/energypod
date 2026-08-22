"""The single composition root for the EnergyPod controller (ADR-0003 D1/D3/D4/D6).

``build_runtime`` is the only place that may construct the whole object graph:
volatile in-memory repositories, the durable SQLite audit/schedule stores (only
when a database path is configured and simulator mode is off), one fleet-wide
``AuthorityGenerationCoordinator``, the canonical ``AuditEventFactory`` with a
per-build process identity, one ``ControlKernel``, one sole-owner actor per
configured unit, the ``EventBus``, the ``EnergyServiceFacade``, and the guarded
API/MCP adapters.  Wiring errors are construction-time errors; nothing starts a
task, opens a socket, or connects a transport here.

Boot is observe-only and disarmed regardless of persisted history: authority,
arming, and active commands are never restored from persistence, and the
volatile stores start empty on every build.

Two bridging decisions live in this module and nowhere else:

- The shipped repositories are synchronous; every application component
  (kernel, actor, facade) awaits its ports.  The async adapters below wrap the
  same underlying store instances the runtime exposes, so "the kernel drives
  THE runtime repositories" is structural, not a convention.
- Event publication rides with the durable state change inside those adapters
  (audit append, observation append, authorization revocation), so a published
  event can never be lost to task scheduling and a slow subscriber can never
  delay the safety path.  Supervision therefore runs the kernel tick loop and
  the per-unit actor loops; there is no separate publisher task to lose.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from fastapi import FastAPI
from fastmcp import FastMCP

from energypod.adapters.modbus import (
    RegisterCatalog,
    WaveshareTransport,
    WaveshareTransportConfig,
    encode_pq_registers,
    encode_stop_registers,
)
from energypod.adapters.persistence.memory import (
    InMemoryAuthorizationRepository,
    InMemoryIntentRepository,
    InMemoryObservationRepository,
)
from energypod.adapters.persistence.sqlite import (
    SQLiteAuditRepository,
    SQLiteDatabase,
    SQLiteScheduleRepository,
)
from energypod.api.mcp import create_mcp_server
from energypod.api.rest import create_api_app
from energypod.application.actor import EnergyPodActor
from energypod.application.arbiter import IntentArbiter
from energypod.application.audit import AuditEventFactory
from energypod.application.control_kernel import ControlKernel
from energypod.application.events import EventBus
from energypod.application.generation import AuthorityGenerationCoordinator
from energypod.application.safety import SafetyKernel
from energypod.application.service import EnergyServiceFacade
from energypod.domain import (
    ControlPolicy,
    Direction,
    UnitHeadroom,
    UnitLifecycle,
    allocate_fleet_power,
)
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.schedule import SchedulePlan, ScheduleVersionConflict
from energypod.runtime.config import ControllerConfig
from energypod.simulator import SimulatedEnergyPod, SimulatorTransport

# Bounded retention for the composed event bus; publishers are never blocked
# because every subscriber queue drops to a resync marker instead of applying
# backpressure (EventBus contract).
_EVENT_BUS_RETENTION = 1024
_EVENT_BUS_QUEUE_CAPACITY = 128

# The volatile stores are bounded: nothing in-process may grow without limit.
_MAX_OBSERVATION_HISTORY_PER_UNIT = 64
_MAX_IN_MEMORY_AUDIT_EVENTS = 10_000
_MAX_IN_MEMORY_INTENTS = 4096

# Commissioning-plausible IoT cell topology for the deterministic simulator:
# ten cells per BIC, at most the evidenced six-BIC packing.
_CELLS_PER_BIC = 10
_MAX_SIMULATOR_BICS = 6

# The only evidenced writable objective is FC16 at 0x0200 with [1, P, Q].
_PQ_OBJECTIVE_ADDRESS = 0x0200

_RUNTIME_PRINCIPAL = "energypod:runtime"
_COMPOSITION_POLICY_VERSION = "composition"


class Clock(Protocol):
    """Structural time port: deterministic time is injected, never ambient."""

    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    """Ambient production clock; tests inject a scripted clock instead."""

    def wall_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_nonnegative(value: Any) -> float | None:
    """Return the value as a finite non-negative float, or None if unusable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number >= 0 else None


# ---------------------------------------------------------------------------
# Volatile durable-shaped stores for simulator mode and database-less builds.
# ---------------------------------------------------------------------------


class _InMemoryAuditRepository:
    """Bounded, newest-first audit mirror of the SQLite audit contract."""

    def __init__(self, *, max_events: int) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[Any] = deque()
        self._seen_event_ids: set[str] = set()
        self._max_events = max_events

    def append(self, event: Any) -> None:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise TypeError("audit event must carry a normalized event_id")
        if event_id in self._seen_event_ids:
            # Duplicate audit facts are refused exactly like the durable store.
            raise DuplicateAuditEventError(event_id)
        self._seen_event_ids.add(event_id)
        self._events.append(event)
        while len(self._events) > self._max_events:
            evicted = self._events.popleft()
            evicted_id = getattr(evicted, "event_id", None)
            if isinstance(evicted_id, str):
                self._seen_event_ids.discard(evicted_id)

    def recent(self, *, limit: int, unit_id: str | None = None) -> tuple[Any, ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        if unit_id is not None and (
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
        ):
            raise ValueError("unit_id must be non-empty and normalized")
        selected = [
            event
            for event in self._events
            if unit_id is None or getattr(event, "unit_id", None) == unit_id
        ]
        return tuple(reversed(selected[-limit:]))


class _InMemoryScheduleRepository:
    """Compare-and-swap singleton schedule store mirroring the SQLite contract."""

    def __init__(self) -> None:
        self._plan: SchedulePlan | None = None

    def get(self) -> SchedulePlan | None:
        return self._plan

    def replace(self, *, expected_version: int, replacement: SchedulePlan) -> None:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if type(replacement) is not SchedulePlan:
            raise TypeError("replacement must be a SchedulePlan")
        if replacement.version != expected_version + 1:
            raise ScheduleVersionConflict(expected_version, replacement.version)
        actual = 0 if self._plan is None else self._plan.version
        if actual != expected_version:
            raise ScheduleVersionConflict(expected_version, actual)
        self._plan = replacement


# ---------------------------------------------------------------------------
# Async port adapters over the exposed synchronous stores.
# ---------------------------------------------------------------------------


class _AsyncIntentRepository:
    """Awaitable intent port over the process-local intent store."""

    def __init__(self, store: InMemoryIntentRepository) -> None:
        self._store = store

    async def add(self, intent: Any) -> None:
        self._store.add(intent)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return self._store.active(now_mono)

    async def remove(self, intent_id: str) -> None:
        self._store.remove(intent_id)


class _AsyncObservationRepository:
    """Awaitable observation port that publishes every appended observation."""

    def __init__(self, *, store: InMemoryObservationRepository, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    async def append(self, observation: Any) -> None:
        self._store.append(observation)
        await self._bus.publish(
            {
                "type": "observation.published",
                "payload": {
                    "unit_id": getattr(observation, "unit_id", None),
                    "connection_epoch": getattr(observation, "connection_epoch", None),
                    "sequence": getattr(observation, "sequence", None),
                },
            }
        )

    async def latest(self, unit_id: str) -> Any | None:
        return self._store.latest(unit_id)

    async def all_latest(self) -> dict[str, Any]:
        return self._store.all_latest()

    async def history(self, unit_id: str) -> tuple[Any, ...]:
        return self._store.history(unit_id)

    async def all_previous(self) -> dict[str, Any]:
        """The observation each unit held before its latest one."""
        previous: dict[str, Any] = {}
        for unit_id in self._store.all_latest():
            history = self._store.history(unit_id)
            if len(history) >= 2:
                previous[unit_id] = history[-2]
        return previous


class _AsyncAuditRepository:
    """Awaitable audit port; every durable append is also a bus event."""

    def __init__(
        self,
        store: SQLiteAuditRepository | _InMemoryAuditRepository,
        *,
        bus: EventBus,
    ) -> None:
        self._store = store
        self._bus = bus

    async def append(self, event: Any) -> None:
        self._store.append(event)
        await self._bus.publish(
            {
                "type": "audit.appended",
                "payload": {
                    "event_id": getattr(event, "event_id", None),
                    "event_type": getattr(event, "event_type", None),
                    "unit_id": getattr(event, "unit_id", None),
                    "generation": getattr(event, "generation", None),
                    "result": getattr(event, "result", None),
                    "reason_codes": list(getattr(event, "reason_codes", ()) or ()),
                },
            }
        )

    async def recent(
        self,
        *,
        limit: int,
        unit_id: str | None = None,
        after_sequence: int | None = None,
    ) -> tuple[Any, ...]:
        # Neither shipped audit store projects a pagination cursor, so a
        # requested cursor fails loudly instead of silently repeating a page.
        if after_sequence is not None:
            raise ValueError("the composed audit stores do not support cursor pagination")
        return self._store.recent(limit=limit, unit_id=unit_id)


class _AsyncAuthorizationRepository:
    """Awaitable authorization port that durably records real revocations.

    Revocation order is fixed: the capability store is revoked first, and only
    then is the revocation audited and published.  A failing post-revocation
    record is suppressed rather than allowed to delay or undo the revocation:
    audit durability participates in *granting* authority, never in delaying an
    emergency fence.
    """

    def __init__(
        self,
        store: InMemoryAuthorizationRepository,
        *,
        fleet_unit_ids: frozenset[str],
        audit: _AsyncAuditRepository,
        bus: EventBus,
        clock: Clock,
        process_instance_id: str,
        process_origin_mono: float,
        configuration_version: int,
    ) -> None:
        self._store = store
        self._fleet_unit_ids = fleet_unit_ids
        self._audit = audit
        self._bus = bus
        self._clock = clock
        self._process_instance_id = process_instance_id
        self._process_origin_mono = process_origin_mono
        self._configuration_version = configuration_version

    async def publish(self, batch: Any) -> None:
        self._store.publish(batch)

    async def current(self, unit_id: str, now_monotonic: float) -> Any | None:
        return self._store.current(unit_id, now_monotonic)

    async def peek(self, unit_id: str) -> Any | None:
        return self._store.peek(unit_id)

    async def revoke(
        self,
        unit_ids: Iterable[str] | None = None,
        *,
        reason: str | None = None,
        **_ignored: Any,
    ) -> None:
        targets = self._fleet_unit_ids if unit_ids is None else frozenset(unit_ids)
        # peek() is the non-consuming projection read: it observes which units
        # still hold live authority without consuming a single-use capability.
        held = sorted(unit_id for unit_id in targets if self._store.peek(unit_id) is not None)
        self._store.revoke(unit_ids=unit_ids, reason=reason)
        if not held:
            return
        with contextlib.suppress(Exception):
            await self._audit.append(self._revocation_event(held, reason))
        with contextlib.suppress(Exception):
            await self._bus.publish(
                {"type": "authorization.revoked", "payload": {"reason": reason, "unit_ids": held}}
            )

    def _revocation_event(self, held: list[str], reason: str | None) -> AuditEvent:
        normalized_reason = reason if reason and reason.strip() else "unspecified"
        wall = self._clock.wall_now().astimezone(UTC)
        return AuditEvent(
            event_id=f"revocation-{uuid.uuid4().hex}",
            occurred_at=wall,
            monotonic_offset_s=float(self._clock.monotonic()) - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type="authorization_revoked",
            unit_id=None,
            connection_epoch=None,
            generation=None,
            cycle_id=None,
            principal=_RUNTIME_PRINCIPAL,
            source=None,
            correlation_id="authorization-revocation",
            intent_id=None,
            policy_version=_COMPOSITION_POLICY_VERSION,
            configuration_version=self._configuration_version,
            observation_sequences={},
            reason_codes=(normalized_reason,),
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"reason": normalized_reason, "unit_ids": held}),
            response_fingerprint=_fingerprint({"result": "revoked"}),
            result="revoked",
            # No unit holds authority after a fleet revocation; DISARMED is the
            # honest aggregate, and rearming stays an explicit operator act.
            lifecycle=UnitLifecycle.DISARMED,
        )


# ---------------------------------------------------------------------------
# Protocol-side bridges the application components expect.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EncodedCommand:
    address: int
    values: tuple[int, ...]


class _PqCommandEncoder:
    """Encode authorizations into the evidenced three-register PQ objective.

    Signing lives only here at the protocol boundary: the evidenced wire
    convention is positive discharge and negative charge, and out-of-range
    values are rejected by ``encode_signed16`` instead of being wrapped.
    """

    def encode(self, authorization: Any) -> _EncodedCommand:
        direction = getattr(authorization, "direction", None)
        watts = int(getattr(authorization, "watts", 0) or 0)
        active = -watts if direction is Direction.CHARGE else watts
        reactive = int(getattr(authorization, "reactive_vars", 0) or 0)
        return _EncodedCommand(_PQ_OBJECTIVE_ADDRESS, encode_pq_registers(active, reactive))

    def zero(self) -> _EncodedCommand:
        return _EncodedCommand(_PQ_OBJECTIVE_ADDRESS, encode_stop_registers())


@dataclass(frozen=True, slots=True)
class _FleetProposal:
    """Allocator output consumed by the safety kernel (proposal, not authority)."""

    unit_id: str
    direction: Direction
    watts: int
    intent_id: str
    intent_expires_at_mono: float
    reactive_vars: int = 0
    generation: int = 0


class _FleetAllocatorAdapter:
    """Adapt the deterministic domain allocator to the kernel's allocator port."""

    def allocate(
        self, intent: Any, observations: Mapping[str, Any], policy: ControlPolicy
    ) -> tuple[_FleetProposal, ...]:
        selected = sorted(intent.selected_unit_ids)
        headrooms = tuple(
            self._headroom(unit_id, observations.get(unit_id), policy) for unit_id in selected
        )
        allocation = allocate_fleet_power(intent, headrooms)
        return tuple(
            _FleetProposal(
                unit_id=unit_id,
                direction=intent.direction,
                watts=int(allocation.allocations[unit_id]),
                intent_id=intent.id,
                intent_expires_at_mono=float(intent.expires_at_mono),
            )
            for unit_id in selected
        )

    @staticmethod
    def _headroom(unit_id: str, observation: Any | None, policy: ControlPolicy) -> UnitHeadroom:
        static_charge = policy.static_charge_limit_w_by_unit.get(unit_id, 0)
        static_discharge = policy.static_discharge_limit_w_by_unit.get(unit_id, 0)
        dynamic_charge = _finite_nonnegative(getattr(observation, "dynamic_charge_limit_w", None))
        dynamic_discharge = _finite_nonnegative(
            getattr(observation, "dynamic_discharge_limit_w", None)
        )
        if dynamic_charge is None or dynamic_discharge is None:
            # A unit without usable telemetry contributes no headroom; the
            # safety kernel independently rejects non-zero power for it.
            return UnitHeadroom(unit_id=unit_id, charge_watts=0, discharge_watts=0, eligible=False)
        return UnitHeadroom(
            unit_id=unit_id,
            charge_watts=max(0, int(min(static_charge, dynamic_charge))),
            discharge_watts=max(0, int(min(static_discharge, dynamic_discharge))),
        )


class _ActorCommandHandle:
    """Facade-facing per-unit handle; the actor keeps sole ownership of I/O.

    ``qualified`` is deliberately not exposed: the composed actor surface
    cannot yet report qualification, and the facade is required to treat an
    unknown state as a refusal, never as permission.
    """

    def __init__(self, actor: EnergyPodActor) -> None:
        self._actor = actor

    @property
    def unit_id(self) -> str:
        return self._actor.unit_id

    @property
    def lifecycle(self) -> UnitLifecycle:
        return self._actor.lifecycle

    @property
    def inhibit_latched(self) -> bool:
        return bool(self._actor.inhibit_latched)

    async def arm(self) -> None:
        await self._actor.arm()

    async def disarm(self) -> None:
        # The actor has no disarm mailbox operation; fencing is the safe
        # equivalent available today: it revokes the unit's outstanding
        # authority and cancels any in-flight heartbeat write.
        await self._actor.fence("facade_disarm")

    async def acknowledge_inhibit(self) -> None:
        await self._actor.acknowledge_inhibit()

    async def request_bounded_zero(self, reason: str) -> None:
        await self._actor.request_bounded_zero(reason)


class _UnresolvedCredentialAuthenticator:
    """Fail-closed bearer authentication until a credential store is composed.

    The configuration carries a secret *reference*, never a secret, and no
    credential store exists in this milestone.  Every bearer token is refused
    so no principal is ever fabricated; the REST boundary answers with its
    structured 401 envelope.
    """

    async def authenticate(self, bearer_token: str) -> None:
        return None


class _ComposedFacade(EnergyServiceFacade):
    """The composed facade, drivable outside the guarded HTTP/MCP boundary.

    Every facade mutation is correlated; the guarded adapters always supply
    the idempotency and request identifiers.  A caller driving the facade
    handle directly (deterministic tests, embedded automation) still travels
    the same audited path, so the composition supplies deterministic
    composition-owned identifiers instead of letting the mutation bypass the
    correlation contract.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._direct_correlations = itertools.count(1)

    def _direct_key(self, prefix: str) -> str:
        return f"composition-{prefix}-{next(self._direct_correlations):08d}"

    async def emergency_stop(
        self,
        *,
        unit_ids: Any,
        reason: Any,
        principal: Any,
        idempotency_key: Any = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        return await super().emergency_stop(
            unit_ids=unit_ids,
            reason=reason,
            principal=principal,
            idempotency_key=(
                self._direct_key("stop") if idempotency_key is None else idempotency_key
            ),
            request_id=(self._direct_key("request") if request_id is None else request_id),
        )


# ---------------------------------------------------------------------------
# Supervision: the structured tasks behind the application lifespan.
# ---------------------------------------------------------------------------


class _Supervision:
    """Kernel tick loop plus per-unit actor loops, started/stopped by lifespan.

    Any supervision task failure fences every generation, revokes outstanding
    authority, and runs actor shutdown (bounded zero before transport close)
    before the lifespan reports failure or the process exits.  Supervision can
    never restart after shutdown: a restarted supervisor would have to restore
    authority state, and observe-only boot forbids that.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        interval_s: float,
        kernel: ControlKernel,
        actors: tuple[EnergyPodActor, ...],
        authorizations: _AsyncAuthorizationRepository,
        coordinator: AuthorityGenerationCoordinator,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._clock = clock
        self._interval_s = interval_s
        self._kernel = kernel
        self._actors = actors
        self._authorizations = authorizations
        self._coordinator = coordinator
        self._tasks: list[asyncio.Task[None]] = []
        self._watcher: asyncio.Task[None] | None = None
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("supervision cannot restart after shutdown")
        if self._started:
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._run_kernel(), name="energypod-supervision:kernel"),
            *(
                asyncio.create_task(
                    self._run_actor(actor), name=f"energypod-supervision:actor:{actor.unit_id}"
                )
                for actor in self._actors
            ),
        ]
        try:
            await self._await_startup()
        except BaseException:
            await self._halt("supervisor_failure")
            raise
        self._watcher = asyncio.create_task(
            self._watch_for_failure(), name="energypod-supervision:watcher"
        )

    async def stop(self) -> None:
        """Normal shutdown: revoke, stop the loops, and shut every actor down.

        Revocation lands before any observable actor lifecycle change so an
        observer that sees actors stopping can never still see live authority.
        The generation fence itself comes from each actor's shutdown, which
        must make its bounded zero attempt before closing the transport.
        """
        self._stopped = True
        with contextlib.suppress(Exception):
            await self._authorizations.revoke(reason="supervision_shutdown")
        await self._cancel_tasks()
        for actor in self._actors:
            with contextlib.suppress(Exception):
                await actor.shutdown()

    async def _await_startup(self) -> None:
        """Startup completes only once every actor left BOOT, or fails loudly."""
        while True:
            failure = self._failure()
            if failure is not None:
                raise failure
            if all(actor.lifecycle is not UnitLifecycle.BOOT for actor in self._actors):
                return
            await asyncio.sleep(0)

    async def _run_kernel(self) -> None:
        # Tick immediately at startup, then hold the commissioned heartbeat
        # cadence; any tick failure ends this task and fences the fleet.
        while True:
            await self._kernel.tick()
            await self._clock.sleep(self._interval_s)

    async def _run_actor(self, actor: EnergyPodActor) -> None:
        # A start failure is a component failure and propagates.  Poll and
        # heartbeat failures are survived: the actor's own state machine
        # fences/inhibits on write failures, and unreadable telemetry fails
        # closed through authorization expiry and kernel staleness checks.
        await actor.start()
        while True:
            await self._clock.sleep(self._interval_s)
            # Heartbeat first: it consumes authority minted against the
            # previous observation before a fresh poll invalidates it.
            with contextlib.suppress(Exception):
                await actor.heartbeat_once()
            with contextlib.suppress(Exception):
                await actor.poll_once()

    async def _watch_for_failure(self) -> None:
        if not self._tasks:
            return
        await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
        if self._failure() is not None and not self._stopped:
            await self._halt("supervisor_failure")

    def _failure(self) -> BaseException | None:
        for task in self._tasks:
            if task.done() and not task.cancelled():
                exception = task.exception()
                if exception is not None:
                    return exception
        return None

    async def _cancel_tasks(self) -> None:
        watcher, self._watcher = self._watcher, None
        pending = [*self._tasks]
        if watcher is not None:
            pending.append(watcher)
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []

    async def _halt(self, reason: str) -> None:
        """Fence every generation, revoke, stop loops, and shut actors down.

        The generation fence is published before any potentially blocking
        repository or audit work, matching the emergency-revocation order, and
        both the fence and the revocation land before any actor lifecycle
        change an observer could read as "shutdown started" — cancelled actor
        loops flip their actor to DISCONNECTED as they unwind, so authority
        must already be gone by then.
        """
        self._stopped = True
        with contextlib.suppress(Exception):
            await self._coordinator.advance(reason=reason)
        with contextlib.suppress(Exception):
            await self._authorizations.revoke(reason=reason)
        # Loops stop before actor shutdown so no start/poll races it.
        await self._cancel_tasks()
        for actor in self._actors:
            with contextlib.suppress(Exception):
                await actor.shutdown()


def _attach_lifespan(app: FastAPI, supervision: _Supervision) -> None:
    """Run supervision inside the application lifespan (API_CONTRACTS entry point)."""

    @contextlib.asynccontextmanager
    async def supervised_lifespan(app: FastAPI) -> AsyncIterator[None]:
        await supervision.start()
        try:
            yield
        finally:
            await supervision.stop()

    app.router.lifespan_context = supervised_lifespan


# ---------------------------------------------------------------------------
# Policy construction.
# ---------------------------------------------------------------------------


def _control_policy(config: ControllerConfig) -> ControlPolicy:
    """Derive the strict domain policy; heartbeat follows ``control_period_s``.

    ``heartbeat_interval_s`` is not configurable (API_CONTRACTS): it is derived
    from ``timing.control_period_s`` so the kernel cadence, actor heartbeats,
    and the commissioned timing budget can never disagree.
    """
    heartbeat_interval_s = float(config.timing.control_period_s)
    cell_counts = {unit.unit_id: unit.expected_cell_count for unit in config.units}
    configured = config.policy
    if configured is None:
        # Observe-only deployments configure no control policy.  The kernel
        # still needs one, so it gets the most conservative legal policy: one
        # watt of unit and fleet headroom.  Arming remains a separate, explicit
        # operator action that observe-only boot never performs by itself.
        minimal = 1
        unit_ids = sorted(cell_counts)
        return ControlPolicy(
            version="observe-only-default",
            static_charge_limit_w_by_unit={unit_id: minimal for unit_id in unit_ids},
            static_discharge_limit_w_by_unit={unit_id: minimal for unit_id in unit_ids},
            fleet_charge_limit_w=minimal,
            fleet_discharge_limit_w=minimal,
            min_soc_pct=0.0,
            max_soc_pct=100.0,
            max_soc_jump_pct=10.0,
            max_soc_disagreement_pct=5.0,
            min_cell_voltage_v=2.80,
            max_cell_voltage_v=3.65,
            max_cell_imbalance_v=0.050,
            expected_cell_count_by_unit=cell_counts,
            min_temperature_c=0.0,
            max_temperature_c=45.0,
            max_temperature_spread_c=45.0,
            max_telemetry_age_s=1.0,
            max_cell_age_s=5.0,
            # The default lifetime only has to exceed the heartbeat interval;
            # no authority is ever minted in observe-only deployments.
            authorization_lifetime_s=heartbeat_interval_s * 1.5,
            heartbeat_interval_s=heartbeat_interval_s,
            ramp_limit_w_per_s_by_unit={unit_id: minimal for unit_id in unit_ids},
            apparent_power_limit_va_by_unit={unit_id: minimal for unit_id in unit_ids},
            reactive_limit_var=0,
            stable_samples_needed_to_rearm=5,
            blocking_fault_codes=frozenset(),
            blocking_warning_codes=frozenset(),
        )
    unit_ids = sorted(cell_counts)
    # The configuration model does not yet express an apparent-power limit or a
    # temperature-spread bound, so both are derived as non-binding envelopes of
    # the configured static limits and temperature window; commissioning can
    # tighten them by extending the configuration contract.
    apparent_limit = max(
        configured.max_unit_charge_w,
        configured.max_unit_discharge_w,
        configured.reactive_power_limit_var,
    )
    return ControlPolicy(
        version=str(configured.version),
        static_charge_limit_w_by_unit={
            unit_id: configured.max_unit_charge_w for unit_id in unit_ids
        },
        static_discharge_limit_w_by_unit={
            unit_id: configured.max_unit_discharge_w for unit_id in unit_ids
        },
        fleet_charge_limit_w=configured.max_fleet_charge_w,
        fleet_discharge_limit_w=configured.max_fleet_discharge_w,
        min_soc_pct=configured.minimum_soc_pct,
        max_soc_pct=configured.maximum_soc_pct,
        max_soc_jump_pct=configured.maximum_soc_jump_pct,
        max_soc_disagreement_pct=configured.maximum_soc_difference_pct,
        min_cell_voltage_v=configured.minimum_cell_v,
        max_cell_voltage_v=configured.maximum_cell_v,
        max_cell_imbalance_v=configured.maximum_cell_imbalance_v,
        expected_cell_count_by_unit=cell_counts,
        min_temperature_c=configured.minimum_temperature_c,
        max_temperature_c=configured.maximum_temperature_c,
        max_temperature_spread_c=(
            configured.maximum_temperature_c - configured.minimum_temperature_c
        ),
        max_telemetry_age_s=configured.maximum_telemetry_age_s,
        max_cell_age_s=configured.maximum_cell_data_age_s,
        authorization_lifetime_s=configured.authorization_lifetime_s,
        heartbeat_interval_s=heartbeat_interval_s,
        ramp_limit_w_per_s_by_unit={unit_id: configured.ramp_limit_w_per_s for unit_id in unit_ids},
        apparent_power_limit_va_by_unit={unit_id: apparent_limit for unit_id in unit_ids},
        reactive_limit_var=configured.reactive_power_limit_var,
        stable_samples_needed_to_rearm=configured.stable_samples_to_rearm,
        blocking_fault_codes=frozenset(configured.blocking_fault_codes),
        blocking_warning_codes=frozenset(),
    )


# ---------------------------------------------------------------------------
# The composition root.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposedRuntime:
    """Drivable handles over one composed controller process.

    The repository handles are the shipped synchronous stores the async port
    adapters wrap, so driving a handle and driving the kernel touch exactly the
    same state.  ``simulators`` is populated only in simulate mode.
    """

    config: ControllerConfig
    clock: Clock
    kernel: ControlKernel
    generation_coordinator: AuthorityGenerationCoordinator
    audit_event_factory: AuditEventFactory
    policy: ControlPolicy
    actors: Mapping[str, EnergyPodActor]
    facade: EnergyServiceFacade
    event_bus: EventBus
    intents: InMemoryIntentRepository
    observations: InMemoryObservationRepository
    authorizations: InMemoryAuthorizationRepository
    audit: SQLiteAuditRepository | _InMemoryAuditRepository
    schedule: SQLiteScheduleRepository | _InMemoryScheduleRepository
    app: FastAPI
    mcp_server_factory: Callable[..., FastMCP]
    simulators: Mapping[str, SimulatedEnergyPod] | None


def _simulator_pod(unit: Any, index: int, clock: Clock) -> SimulatedEnergyPod:
    bic_count = min(_MAX_SIMULATOR_BICS, max(1, -(-unit.expected_cell_count // _CELLS_PER_BIC)))
    # The seed is the unit's configuration position, so identical
    # configurations produce identical register banks across builds.
    return SimulatedEnergyPod(
        clock=clock,
        identity=unit.expected_identity,
        bic_count=bic_count,
        seed=index,
    )


def build_runtime(
    config: ControllerConfig, *, simulate: bool = False, clock: Clock | None = None
) -> ComposedRuntime:
    """Construct the whole controller graph; the only composition point.

    Construction is eager and side-effect free apart from opening the durable
    SQLite store when one is configured: no task starts, no socket opens, no
    transport connects, and nothing is restored from persistence.
    """
    resolved_clock = clock if clock is not None else _SystemClock()
    unit_ids = frozenset(unit.unit_id for unit in config.units)
    process_instance_id = f"energypod-{uuid.uuid4().hex}"
    process_origin_mono = float(resolved_clock.monotonic())

    # --- persistence -----------------------------------------------------
    # SQLite backs exactly the durable audit and schedule stores, and only
    # when a database path is configured and simulator mode is off.  Intents,
    # observations, and authorizations are always process-local so no restored
    # authority can survive a restart (observe-only boot).
    storage = config.storage
    if storage is not None and not simulate:
        database = SQLiteDatabase(
            storage.database_path,
            busy_timeout_ms=storage.busy_timeout_ms,
        )
        database.open()
        audit_store: SQLiteAuditRepository | _InMemoryAuditRepository = SQLiteAuditRepository(
            database
        )
        schedule_store: SQLiteScheduleRepository | _InMemoryScheduleRepository = (
            SQLiteScheduleRepository(database)
        )
    else:
        audit_store = _InMemoryAuditRepository(max_events=_MAX_IN_MEMORY_AUDIT_EVENTS)
        schedule_store = _InMemoryScheduleRepository()
    intent_store = InMemoryIntentRepository(max_stored_intents=_MAX_IN_MEMORY_INTENTS)
    observation_store = InMemoryObservationRepository(
        max_history_per_unit=_MAX_OBSERVATION_HISTORY_PER_UNIT
    )
    # The injected clock lets the non-consuming projection read (peek) judge
    # capability validity without ever consuming one.
    authorization_store = InMemoryAuthorizationRepository(clock=resolved_clock)

    # --- shared fleet authority -------------------------------------------
    coordinator = AuthorityGenerationCoordinator()
    bus = EventBus(
        retention=_EVENT_BUS_RETENTION,
        queue_capacity=_EVENT_BUS_QUEUE_CAPACITY,
        clock=resolved_clock,
    )

    # --- async ports over the exposed stores ------------------------------
    audit_port = _AsyncAuditRepository(audit_store, bus=bus)
    observation_port = _AsyncObservationRepository(store=observation_store, bus=bus)
    intent_port = _AsyncIntentRepository(intent_store)
    authorization_port = _AsyncAuthorizationRepository(
        authorization_store,
        fleet_unit_ids=unit_ids,
        audit=audit_port,
        bus=bus,
        clock=resolved_clock,
        process_instance_id=process_instance_id,
        process_origin_mono=process_origin_mono,
        configuration_version=config.revision,
    )

    # --- control authority -------------------------------------------------
    policy = _control_policy(config)
    audit_event_factory = AuditEventFactory(
        process_instance_id=process_instance_id,
        process_origin_mono=process_origin_mono,
        wall_now=resolved_clock.wall_now,
    )
    kernel = ControlKernel(
        clock=resolved_clock,
        unit_ids=unit_ids,
        intents=intent_port,
        observations=observation_port,
        authorizations=authorization_port,
        audit=audit_port,
        arbiter=IntentArbiter(),
        allocator=_FleetAllocatorAdapter(),
        safety=SafetyKernel(),
        policy=policy,
        generation_coordinator=coordinator,
        configuration_version=config.revision,
        audit_event_factory=audit_event_factory,
    )

    # --- one sole-owner actor per configured unit ---------------------------
    probe = RegisterCatalog().layout_probe
    actors: dict[str, EnergyPodActor] = {}
    simulators: dict[str, SimulatedEnergyPod] | None = {} if simulate else None
    for index, unit in enumerate(config.units):
        if simulate:
            pod = _simulator_pod(unit, index, resolved_clock)
            transport: SimulatorTransport | WaveshareTransport = SimulatorTransport(pod=pod)
            if simulators is not None:
                simulators[unit.unit_id] = pod
        else:
            # The production transport is constructed but never connected:
            # connection belongs to the actor's serialized start, which only
            # supervision or an explicit start() performs.
            transport = WaveshareTransport(
                config=WaveshareTransportConfig(
                    host=unit.endpoint.host,
                    port=unit.endpoint.port,
                    device_id=unit.device_id,
                    timeout_s=config.timing.essential_read_timeout_s,
                )
            )
        actors[unit.unit_id] = EnergyPodActor(
            unit_id=unit.unit_id,
            transport=transport,
            clock=resolved_clock,
            observations=observation_port,
            authorizations=authorization_port,
            audit=audit_port,
            command_encoder=_PqCommandEncoder(),
            generation_coordinator=coordinator,
            expected_identity=unit.expected_identity,
            expected_profile=unit.protocol_profile.value,
            expected_cell_count=unit.expected_cell_count,
            stable_observations_required=policy.stable_samples_needed_to_rearm,
            essential_read_address=probe.address,
            essential_read_count=probe.count,
            heartbeat_interval_s=policy.heartbeat_interval_s,
            # The write timeout is the heartbeat safety margin: the margin a
            # renewal may eat into before its deadline, and the bound on the
            # bounded-zero attempt.
            heartbeat_safety_margin_s=config.timing.write_timeout_s,
            blocking_fault_codes=frozenset(policy.blocking_fault_codes),
        )

    # --- application facade, guarded API, and MCP surface -------------------
    facade = _ComposedFacade(
        site_id=config.site.site_id,
        clock=resolved_clock,
        intents=intent_port,
        observations=observation_port,
        authorizations=authorization_port,
        audit=audit_port,
        events=bus,
        coordinator=coordinator,
        actors={unit_id: _ActorCommandHandle(actor) for unit_id, actor in actors.items()},
    )

    def mcp_server_factory(*, principal: Any) -> FastMCP:
        # MCP is read-only by default: dispatch needs explicit configuration
        # plus a separately issued automation credential (API_CONTRACTS).
        return create_mcp_server(service=adapter_service, principal=principal)

    # The adapters declare their service dependency with deliberately loose
    # ``**kwargs`` mutation signatures; the concrete facade is the strict,
    # named-argument implementation behind them, so this cast only narrows the
    # structural boundary both sides already tested independently.
    adapter_service = cast("Any", facade)
    app = create_api_app(
        service=adapter_service,
        authenticator=_UnresolvedCredentialAuthenticator(),
        event_source=bus,
    )
    supervision = _Supervision(
        clock=resolved_clock,
        interval_s=policy.heartbeat_interval_s,
        kernel=kernel,
        actors=tuple(actors.values()),
        authorizations=authorization_port,
        coordinator=coordinator,
    )
    _attach_lifespan(app, supervision)

    return ComposedRuntime(
        config=config,
        clock=resolved_clock,
        kernel=kernel,
        generation_coordinator=coordinator,
        audit_event_factory=audit_event_factory,
        policy=policy,
        actors=actors,
        facade=facade,
        event_bus=bus,
        intents=intent_store,
        observations=observation_store,
        authorizations=authorization_store,
        audit=audit_store,
        schedule=schedule_store,
        app=app,
        mcp_server_factory=mcp_server_factory,
        simulators=simulators,
    )


__all__ = ["ComposedRuntime", "build_runtime"]
