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

Three bridging decisions live in this module and nowhere else:

- The shipped repositories are synchronous; every application component
  (kernel, actor, facade) awaits its ports.  The async adapters below wrap the
  underlying stores, and the runtime exposes those same adapters as its
  repository handles, so "the kernel drives THE runtime repositories" is
  structural, not a convention.
- Event publication rides with the durable state change inside those adapters
  (audit append, observation append, authorization revocation), so a published
  event can never be lost to task scheduling and a slow subscriber can never
  delay the safety path.  Supervision therefore runs the kernel tick loop and
  the per-unit actor loops; there is no separate publisher task to lose.
- Run mode (``simulate=False``) wires each actor a decode-driven telemetry
  strategy over the production transport: the served layout probe decides the
  register plan, and the production wire decoder turns the served blocks into
  the domain observation.  Run mode is commissioned per configuration mode
  (API_CONTRACTS "Write-enabled run mode"): ``observe_only`` stays structural
  — the actor's stable-sample qualification threshold is wired beyond any
  reachable count, so no amount of coherent telemetry can ever carry a live
  unit into DISARMED or mint authority for it — while ``write_enabled``
  (policy, enabled authentication, and the measured live-trial expiry evidence
  all validated at configuration time) composes the policy's qualification
  threshold and pins the served PQ objective readback as the actor's arm-time
  external-writer preflight, so single-writer authority is established from
  the served registers, never assumed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import secrets
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
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
    faults,
    protocol_codec,
    register_layout,
)
from energypod.adapters.modbus import decode as wire_decode
from energypod.adapters.persistence.memory import (
    InMemoryAuthorizationRepository,
    InMemoryIntentRepository,
    InMemoryObservationRepository,
)
from energypod.adapters.persistence.sqlite import (
    PersistenceBusyError,
    SQLiteAuditRepository,
    SQLiteDatabase,
    SQLiteScheduleRepository,
)
from energypod.api.mcp import create_mcp_server
from energypod.api.rest import create_api_app
from energypod.application.actor import EnergyPodActor
from energypod.application.arbiter import STOP_ACKNOWLEDGE_SCOPE, IntentArbiter
from energypod.application.audit import AuditEventFactory
from energypod.application.control_kernel import ControlKernel
from energypod.application.events import EventBus
from energypod.application.generation import AuthorityGenerationCoordinator
from energypod.application.safety import SafetyKernel
from energypod.application.service import EnergyServiceFacade
from energypod.domain import (
    ControlPolicy,
    DataQuality,
    Direction,
    Observation,
    UnitHeadroom,
    UnitLifecycle,
    allocate_fleet_power,
)
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.schedule import SchedulePlan, ScheduleVersionConflict
from energypod.runtime.config import ControllerConfig, ControllerMode
from energypod.runtime.credentials import FileCredentialStore
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
_TEMPERATURES_PER_BIC = 3
_MAX_SIMULATOR_BICS = 6

# The only evidenced writable objective is FC16 at 0x0200 with [1, P, Q].
_PQ_OBJECTIVE_ADDRESS = 0x0200

# The served PQ objective readback (IoT PCS detailed-state block 0x1060,
# active/reactive power objectives at offsets +17/+18): PROTOCOL_EVIDENCE 4b —
# the live -200 W commissioning write read back immediately at 0x1060+17, and
# the authorized 2026-08-22 captures baseline both words at zero.  A
# write-enabled run-mode composition pins this window as the actor's arm-time
# external-writer preflight port, so sole-writer authority is proven from the
# served registers before the unit may ever transition into ARMED_IDLE.
_OBJECTIVE_READBACK_ADDRESS = 0x1060 + 17

# Observe-only run mode composes no interactive qualification path
# (PROTOCOL_EVIDENCE section 4a; field-mapping 2026-08-22 section 7):
# actuation stays gated on per-unit commissioning evidence the composition
# does not hold.  The actor's stable-sample threshold is therefore wired
# beyond any count a process could ever reach (one sample per telemetry cycle
# at the 0.40 s commissioned cadence would need ~10^12 years to reach 2^63-1),
# so a live unit can never cross into DISARMED through telemetry alone, the
# facade always sees an unqualified unit, and the only register write run mode
# can ever issue is the bounded stop triple.  Observe-only is structural, not
# a mode flag the composition re-checks anywhere else; write-enabled run mode
# (API_CONTRACTS "Write-enabled run mode") composes the policy's threshold
# instead, on the strength of the measured live-trial evidence the
# configuration validator already demanded.
_RUN_MODE_STABLE_SAMPLES_REQUIRED = 2**63 - 1

# Register windows the simulator telemetry decode consumes (PROTOCOL_EVIDENCE
# section 5): the common system block, the IoT BMS block, the three IoT
# fault/status blocks, and the cell blocks whose counts follow the BIC count.
_SYSTEM_BLOCK_BASE = 0x0100
_BMS_BLOCK_BASE = 0x5000
_PCS_FAULT_BLOCK_BASE = 0x1040
_DCDC_FAULT_BLOCK_BASE = 0x2040
_BMS_FAULT_BLOCK_BASE = 0x5040
_CELL_VOLTAGE_BASE = 0x5200
_CELL_TEMPERATURE_BASE = 0x523C
_IDENTITY_BLOCK_BASE = 0x8106

_RUNTIME_PRINCIPAL = "energypod:runtime"
_COMPOSITION_POLICY_VERSION = "composition"

# API_CONTRACTS "Operations surface": `energypod simulate` mints one
# deterministic development principal — full scopes, interactive — when no
# credential store is configured.  "Full scopes" is exactly the vocabulary the
# guarded boundary can check (nothing invented, nothing missing); only the
# token is per-process.
_DEV_PRINCIPAL_SUBJECT = "dev:simulator"
_DEV_PRINCIPAL_SCOPES = frozenset(
    {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
)


class Clock(Protocol):
    """Structural time port: deterministic time is injected, never ambient."""

    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    """Ambient production clock; tests inject a scripted clock instead.

    The monotonic timeline is process-relative with a fixed origin: every
    window the controller reasons about (authorization lifetime, telemetry age,
    watchdog leases) is a duration between two reads of one clock, so the
    epoch is arbitrary — but it must be process-local.  Raw host monotonic
    time would leak the machine's boot uptime into every controller
    timestamp, coupling process-internal validity windows to an accident of
    the host.  The origin matches the deterministic test clocks' convention
    (the golden scenarios' manual clock starts at 1000.0), so ambient and
    injected timelines live on one familiar scale.
    """

    _origin = 1000.0
    _anchor = time.monotonic()

    def wall_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return self._origin + (time.monotonic() - self._anchor)

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


class _SequencedAuditEvent(AuditEvent):
    """The audit read model: one durable fact plus its store sequence.

    ``AuditEvent`` is the write model (a fact, immutable, exactly what was
    appended); the pagination cursor needs the store's own ordering key next
    to it.  The projection is a subclass so every consumer of ``AuditEvent``
    — facade snapshots, REST/MCP serialization, audit assertions — keeps
    working unchanged while ``recent_audit`` can name the oldest delivered
    fact and resume strictly below it.
    """

    sequence: int


def _with_sequence(event: AuditEvent, sequence: int) -> _SequencedAuditEvent:
    """Project one stored fact into the sequenced read model."""
    if type(sequence) is not int or sequence < 0:  # pragma: no cover - store invariant
        raise ValueError("audit sequence must be a non-negative integer")
    values = {name: value for name, value in event.__dict__.items() if name != "sequence"}
    return _SequencedAuditEvent.model_construct(sequence=sequence, **values)


def _validate_after_sequence(after_sequence: int | None) -> None:
    if after_sequence is not None and (
        type(after_sequence) is not int or isinstance(after_sequence, bool) or after_sequence < 0
    ):
        raise ValueError("after_sequence must be a non-negative integer")


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    # Mirrors the durable store's own busy classification so the read bridge
    # reports contention identically instead of leaking a raw driver error.
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class _InMemoryAuditRepository:
    """Bounded, newest-first audit mirror of the SQLite audit contract.

    Every append takes the next monotone store sequence, so the read model
    carries the same stable cursor the durable rows provide and the facade can
    page past one page in database-less deployments too.
    """

    def __init__(self, *, max_events: int) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[tuple[int, Any]] = deque()
        self._seen_event_ids: set[str] = set()
        self._max_events = max_events
        self._sequence = 0

    def append(self, event: Any) -> None:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise TypeError("audit event must carry a normalized event_id")
        if event_id in self._seen_event_ids:
            # Duplicate audit facts are refused exactly like the durable store.
            raise DuplicateAuditEventError(event_id)
        self._seen_event_ids.add(event_id)
        self._sequence += 1
        self._events.append((self._sequence, event))
        while len(self._events) > self._max_events:
            _, evicted = self._events.popleft()
            evicted_id = getattr(evicted, "event_id", None)
            if isinstance(evicted_id, str):
                self._seen_event_ids.discard(evicted_id)

    def recent(
        self, *, limit: int, unit_id: str | None = None, after_sequence: int | None = None
    ) -> tuple[Any, ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        if unit_id is not None and (
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
        ):
            raise ValueError("unit_id must be non-empty and normalized")
        _validate_after_sequence(after_sequence)
        selected = [
            (sequence, event)
            for sequence, event in self._events
            if (unit_id is None or getattr(event, "unit_id", None) == unit_id)
            and (after_sequence is None or sequence < after_sequence)
        ]
        return tuple(
            _with_sequence(event, sequence) for sequence, event in reversed(selected[-limit:])
        )


class _SequencedSQLiteAuditRepository(SQLiteAuditRepository):
    """Composition-owned audit read path over the durable audit rows.

    The durable store owns the ``AUTOINCREMENT`` row sequence; this bridge
    projects it onto the audit read model and honours the facade cursor, so
    ``recent_audit`` pages identically whether the deployment persists to
    SQLite or keeps its audit trail in memory.  Sequences therefore survive a
    restart with the database that minted them.
    """

    def recent(
        self, *, limit: int, unit_id: str | None = None, after_sequence: int | None = None
    ) -> tuple[Any, ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        if unit_id is not None and (
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
        ):
            raise ValueError("unit_id must be non-empty and normalized")
        _validate_after_sequence(after_sequence)
        query = "SELECT sequence, payload FROM audit_events"
        parameters: list[Any] = []
        clauses: list[str] = []
        if unit_id is not None:
            clauses.append("unit_id = ?")
            parameters.append(unit_id)
        if after_sequence is not None:
            clauses.append("sequence < ?")
            parameters.append(after_sequence)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence DESC LIMIT ?"
        parameters.append(limit)
        try:
            with self._database.lock:
                rows = self._database.connection.execute(query, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                raise PersistenceBusyError("audit database is busy") from exc
            raise
        return tuple(_with_sequence(self._decode(row[1]), row[0]) for row in rows)


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
    """Awaitable intent port over the process-local intent store.

    Removing a latched emergency stop also completes the arbiter's own
    acknowledgement protocol: the arbiter latches a selected stop internally,
    and a latch that outlived its intent would keep fencing every future cycle
    exactly as if the stop were still live.  The runtime principal carries the
    stop-acknowledge scope here only after the facade has already admitted the
    human operator with that scope; this bridge is wiring, not authority.
    """

    def __init__(self, store: InMemoryIntentRepository, *, arbiter: IntentArbiter) -> None:
        self._store = store
        self._arbiter = arbiter

    async def add(self, intent: Any) -> None:
        self._store.add(intent)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return self._store.active(now_mono)

    async def remove(self, intent_id: str) -> None:
        try:
            self._arbiter.acknowledge_emergency_stop(
                intent_id=intent_id,
                actor_identity=_RUNTIME_PRINCIPAL,
                operator_scopes=frozenset({STOP_ACKNOWLEDGE_SCOPE}),
            )
        except KeyError:
            # Not the arbiter's latched stop: an ordinary intent removal.
            self._store.remove(intent_id)
            return
        # The arbiter removed the acknowledged stop from the shared store and
        # released its latch; removing again would raise a spurious LookupError.
        return


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


class _AuditStore(Protocol):
    """The audit store shape every composed deployment exposes.

    Both shipped shapes project the store's own ordering key onto the audit
    read model and honour the facade cursor, so ``recent_audit`` pages the
    same way in memory-backed and SQLite-backed builds.
    """

    def append(self, event: Any) -> None: ...

    def recent(
        self,
        *,
        limit: int,
        unit_id: str | None = None,
        after_sequence: int | None = None,
    ) -> tuple[Any, ...]: ...


class _AsyncAuditRepository:
    """Awaitable audit port; every durable append is also a bus event."""

    def __init__(self, store: _AuditStore, *, bus: EventBus) -> None:
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
        # The cursor names the oldest fact already delivered; both composed
        # stores resume strictly below it, never repeating a page.
        return self._store.recent(limit=limit, unit_id=unit_id, after_sequence=after_sequence)


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


class _LazyWaveshareTransport:
    """Loop-deferred production transport: construct on first use, never here.

    pymodbus 3.15's ``AsyncModbusTcpClient`` captures the running event loop at
    construction, while the entry-point contract composes synchronously with no
    loop and no socket events at all.  The proxy therefore defers building the
    real ``WaveshareTransport`` until the first await — always inside the
    owning actor's serialized mailbox dispatch on the serving loop — so the
    client is always bound to the loop that actually drives it.  The transport
    configuration itself is still validated eagerly at composition time.
    """

    __slots__ = ("_factory", "_transport")

    def __init__(self, factory: Callable[[], WaveshareTransport]) -> None:
        self._factory = factory
        self._transport: WaveshareTransport | None = None

    def _resolve(self) -> WaveshareTransport:
        if self._transport is None:
            self._transport = self._factory()
        return self._transport

    async def connect(self) -> None:
        await self._resolve().connect()

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        return await self._resolve().read_holding(address, count)

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        await self._resolve().write_registers(address, values)

    async def close(self) -> None:
        # A transport that was never constructed never opened anything, and
        # building a client merely to close it would bind a loop for nothing.
        if self._transport is not None:
            await self._transport.close()


def _production_transport_factory(
    unit: Any, timeout_s: float, inter_request_delay_s: float = 0.1
) -> Callable[[], WaveshareTransport]:
    """Build the per-unit production transport factory (eagerly validated)."""
    config = WaveshareTransportConfig(
        host=unit.endpoint.host,
        port=unit.endpoint.port,
        device_id=unit.device_id,
        timeout_s=timeout_s,
        inter_request_delay_s=inter_request_delay_s,
    )
    return lambda: WaveshareTransport(config=config)


class _ServedBankMismatchError(RuntimeError):
    """The served register bank contradicts what the unit is configured for.

    A decode that cannot reconcile the bank it actually read with the unit's
    configured profile or topology fails the poll: no observation is
    delivered, qualification can never pass, and control fails closed through
    telemetry staleness.  Copying the configured expectation into the
    observation instead would fabricate self-consistent evidence.  Both
    composed telemetry strategies — the simulator device model and the live
    wire decode — share this one fail-closed doctrine.
    """


class _SimulatorTelemetry:
    """Composition-owned poll -> decode -> deliver strategy over one pod.

    Every register window is decoded with the production stack — the shipped
    register catalog (which also supplies the read plan), the evidenced codec
    scaling, and the fault catalog — into one domain ``Observation``.  Identity,
    connection epoch, and the telemetry/cell sequences come from the pod's
    device-model surface (the evidenced register bank carries no string
    identity), and the lifecycle comes from the owning actor at decode time so
    the control path sees controllability exactly as the actor holds it.  A
    decode failure propagates: no observation is delivered and control fails
    closed through telemetry staleness.

    The served bank — not the configuration — is the evidence for the decoded
    profile and the temperature topology: the layout probe word read from the
    BMS block decides which profile this decode may claim, and the BIC count
    it reports fixes how many temperature sensors the bank must serve.  The
    cell window keeps its evidenced ``BIC * 10`` width and the decoded cells
    are reconciled with the configured ``expected_cell_count``, so a unit
    whose commissioned topology is not a multiple of ten (the corroborated
    59-cell packing) can still be qualified against its own expectation.
    """

    def __init__(
        self,
        *,
        pod: SimulatedEnergyPod,
        clock: Clock,
        unit_id: str,
        expected_profile: str,
        expected_cell_count: int,
    ) -> None:
        self._pod = pod
        self._clock = clock
        self._unit_id = unit_id
        self._expected_profile = expected_profile
        self._expected_cell_count = expected_cell_count
        catalog = register_layout.RegisterCatalog()
        self._plan = tuple(
            (block.address, block.count)
            for block in (*catalog.iot_reads(bic_count=pod.bic_count), *catalog.common_reads)
        )
        windows = {address: (address, count) for address, count in self._plan}
        self._system_window = windows[_SYSTEM_BLOCK_BASE]
        self._bms_window = windows[_BMS_BLOCK_BASE]
        self._fault_windows = (
            (faults.FaultBlock.IOT_PCS, windows[_PCS_FAULT_BLOCK_BASE]),
            (faults.FaultBlock.IOT_DCDC, windows[_DCDC_FAULT_BLOCK_BASE]),
            (faults.FaultBlock.IOT_BMS, windows[_BMS_FAULT_BLOCK_BASE]),
        )
        self._cell_voltage_window = windows[_CELL_VOLTAGE_BASE]
        self._cell_temperature_window = windows[_CELL_TEMPERATURE_BASE]

    async def advance(self) -> None:
        """Advance the device model exactly once per telemetry cycle."""
        self._pod.poll()

    def read_plan(self) -> tuple[tuple[int, int], ...]:
        """The evidenced IoT register windows one telemetry cycle reads."""
        return self._plan

    def decode(
        self, blocks: Mapping[tuple[int, int], tuple[int, ...]], lifecycle: UnitLifecycle
    ) -> Observation:
        system = blocks[self._system_window]
        bms = blocks[self._bms_window]
        fault_codes, warning_codes = self._decode_fault_signals(blocks)
        cells = blocks[self._cell_voltage_window]
        temperatures = blocks[self._cell_temperature_window]
        served_temperatures = self._verify_served_bank(bms, cells, temperatures)
        return Observation(
            unit_id=self._unit_id,
            device_identity=self._pod.identity,
            connection_epoch=self._pod.connection_epoch,
            wall_timestamp=self._clock.wall_now(),
            captured_at_mono=self._pod.telemetry_captured_at_mono,
            sequence=self._pod.telemetry_sequence,
            lifecycle=lifecycle,
            protocol_profile=self._expected_profile,
            # System block: SOC at +17, pack voltage x0.1 V at +18, pack
            # current x0.1 A at +19, signed battery watts at +20, SOH at +22.
            system_soc_pct=float(system[17]),
            soh_pct=float(system[22]),
            battery_watts=float(protocol_codec.decode_signed16(system[20])),
            pack_voltage_v=system[18] * 0.1,
            pack_current_a=protocol_codec.decode_signed16(system[19]) * 0.1,
            # BMS block: SOC at +9, dynamic charge/discharge power limits at
            # +13/+14 (raw watts, the device's own headroom report).
            bms_soc_pct=float(bms[9]),
            dynamic_charge_limit_w=float(bms[13]),
            dynamic_discharge_limit_w=float(bms[14]),
            expected_cell_count=self._expected_cell_count,
            # Cell blocks: millivolt words and raw-40-offset temperature words.
            # The evidenced window serves every cell the packing holds; the
            # unit's commissioned count takes the prefix it declares.
            cell_voltages_v=tuple(value / 1000.0 for value in cells[: self._expected_cell_count]),
            cell_captured_at_mono=self._pod.cell_captured_at_mono,
            cell_sequence=self._pod.cell_sequence,
            expected_temperature_count=served_temperatures,
            temperatures_c=tuple(float(value - 40) for value in temperatures),
            active_faults=fault_codes,
            active_warnings=warning_codes,
            quality={field: DataQuality.GOOD for field in Observation.QUALITY_FIELDS},
        )

    def _verify_served_bank(
        self, bms: Sequence[int], cells: Sequence[int], temperatures: Sequence[int]
    ) -> int:
        """Reconcile the served bank with the configured unit, or fail closed.

        Returns the number of temperature sensors the served topology declares.
        Every expectation is derived from the words the bank actually served,
        never echoed from configuration.
        """
        probe = register_layout.detect_layout(bms[:7])
        if probe.layout.value != self._expected_profile:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} is configured for protocol profile "
                f"{self._expected_profile!r} but the served register bank reports "
                f"{probe.layout.value!r}"
            )
        if not probe.topology_valid:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} is served an invalid {probe.layout.value} topology: "
                f"{probe.bic_count} BICs"
            )
        served_cells = probe.bic_count * _CELLS_PER_BIC
        served_temperatures = probe.bic_count * _TEMPERATURES_PER_BIC
        if len(cells) != served_cells:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} served {len(cells)} cell words where its own "
                f"topology reports {served_cells}"
            )
        if len(temperatures) != served_temperatures:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} served {len(temperatures)} temperature words where "
                f"its own topology reports {served_temperatures}"
            )
        if served_cells < self._expected_cell_count:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} expects {self._expected_cell_count} cells but the "
                f"served IoT packing holds {served_cells}"
            )
        return served_temperatures

    def _decode_fault_signals(
        self, blocks: Mapping[tuple[int, int], tuple[int, ...]]
    ) -> tuple[frozenset[str], frozenset[str]]:
        fault_codes: set[str] = set()
        warning_codes: set[str] = set()
        for block, window in self._fault_windows:
            layout = faults.FAULT_BLOCK_LAYOUTS[block]
            words = faults.extract_fault_block(block, blocks[window])
            for prefix in layout.warning_offsets:
                warning_codes.update(
                    signal.code for signal in faults.decode_fault_word(prefix, words[prefix])
                )
            for prefix in layout.fault_offsets:
                fault_codes.update(
                    signal.code for signal in faults.decode_fault_word(prefix, words[prefix])
                )
        return frozenset(fault_codes), frozenset(warning_codes)


class _LiveDecodeTelemetry:
    """Composition-owned probe -> plan -> decode strategy over one live gateway.

    Run mode (``simulate=False``) wires exactly one of these per unit, over the
    same lazy production transport the owning actor dispatches through (so sole
    socket ownership is unchanged — every read below still runs inside the
    actor's serialized mailbox dispatch).  One telemetry cycle is:

    1. ``advance()`` reads the seven-word layout probe from the BMS block and
       decodes it with the production ``detect_layout``.  The served probe —
       not the configuration — is the evidence: the wire's own BIC count sizes
       this cycle's cell, temperature, and balance windows, exactly as the
       2026-08-22 captures show a mixed 6/5/6-BIC fleet behind one read plan.
       A probe that contradicts the configured profile, or reports a topology
       the evidenced IoT packing cannot serve, fails the poll: no observation,
       no qualification, control fails closed through telemetry staleness.
    2. ``read_plan()`` is the shipped catalog's IoT read plan for the probed
       topology plus the common blocks (system overview, debug-mode readback,
       network status, the identity pair, and the device parameters whose
       RTU-ID mirror cross-checks identity).
    3. ``decode()`` hands the served blocks — re-keyed from the actor's
       ``(address, count)`` windows to the base-address mapping the wire
       decoder speaks — to the production decoder, together with the probe and
       this unit's commissioned expectations.  Identity comes from the wire
       (``0x8106``, low word first, rendered ``byd-{rtu_id:08x}``), never from
       configuration; the deployed plan carries no poll-sequence or
       capture-time registers, so the strategy mints the per-unit capture
       sequence and stamps the injected clock's capture times; the lifecycle
       comes from the owning actor so the control path sees controllability
       exactly as the actor holds it.
    """

    def __init__(
        self,
        *,
        transport: _LazyWaveshareTransport,
        clock: Clock,
        unit_id: str,
        expected_identity: str,
        expected_profile: str,
        expected_cell_count: int,
        probe_address: int,
        probe_count: int,
    ) -> None:
        if expected_profile != register_layout.ProtocolLayout.IOT.value:
            # The evidenced live decode covers the deployed IoT register plan
            # only.  Composing a legacy-profile live unit would boot a pod that
            # can never be served a decodable bank: a wiring error, not a
            # runtime discovery.
            raise ValueError(
                f"unit {unit_id!r} is configured for protocol profile "
                f"{expected_profile!r}; the evidenced live decode covers the "
                f"deployed {register_layout.ProtocolLayout.IOT.value} register plan only"
            )
        self._transport = transport
        self._clock = clock
        self._unit_id = unit_id
        self._expected_identity = expected_identity
        self._expected_profile = expected_profile
        self._expected_cell_count = expected_cell_count
        self._probe_window = (probe_address, probe_count)
        self._catalog = register_layout.RegisterCatalog()
        self._sequence = itertools.count(1)
        self._probe: register_layout.LayoutProbe | None = None
        # Tiered refresh (the 2026-08-22 commissioning constraint): the
        # gateway's 0.1 s inter-frame gap makes a full 17-window plan take
        # ~3.4 s — longer than the commissioned renewal cadence inside the
        # measured watchdog.  The domain already models cells polling slower
        # than the control rate (separate cell capture time and sequence), so
        # the live plan splits the same way: a fast core every cycle (BMS,
        # fault/status blocks), a hot ring alternating cells/temperatures,
        # and a cold ring rotating one slow window per cycle, with decoded
        # observations merging the cached slow blocks and their capture times.
        self._cycle = 0
        self._slow_cache: dict[int, tuple[tuple[int, ...], float]] = {}
        self._cell_meta: tuple[float, int] | None = None

    async def advance(self) -> None:
        """Probe the served layout so this cycle's plan follows the wire."""
        words = await self._transport.read_holding(*self._probe_window)
        probe = register_layout.detect_layout(words)
        if probe.layout.value != self._expected_profile:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} is configured for protocol profile "
                f"{self._expected_profile!r} but the served register bank reports "
                f"{probe.layout.value!r}"
            )
        if not probe.topology_valid:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} is served an invalid {probe.layout.value} topology: "
                f"{probe.bic_count} BICs"
            )
        self._probe = probe
        self._cycle += 1

    def read_plan(self) -> tuple[tuple[int, int], ...]:
        """This cycle's windows under the commissioned tiered refresh.

        Every cycle: the BMS block and the three IoT fault blocks (everything
        the safety kernel consumes at the control rate) plus temperatures.
        Every third cycle: the cell-voltage window (the domain already models
        cells on their own slower capture clock; the policy's cell-age bound
        covers the tier).  First cycle: the stable identity pair, cached for
        the process lifetime.  Every eighth cycle: one cold-ring window
        (PCS/DCDC detail, system overview, parameters, balance, energy) so
        the unit-detail surface stays populated without threatening the
        renewal cadence.
        """
        probe = self._probe_require()
        full = [
            (block.address, block.count)
            for block in (
                *self._catalog.iot_reads(bic_count=probe.bic_count),
                *self._catalog.common_reads,
            )
        ]
        by_base = {address: count for address, count in full}
        core_bases = {
            _BMS_BLOCK_BASE,
            _PCS_FAULT_BLOCK_BASE,
            _DCDC_FAULT_BLOCK_BASE,
            _BMS_FAULT_BLOCK_BASE,
            _CELL_TEMPERATURE_BASE,
            # The two-word identity pair rides the core: a physically swapped
            # or re-addressed unit must latch on the very next poll, not on a
            # slow ring refresh.
            _IDENTITY_BLOCK_BASE,
        }
        plan = [(base, by_base[base]) for base in sorted(core_bases) if base in by_base]
        if _CELL_VOLTAGE_BASE in by_base and self._cycle % 3 == 1:
            plan.append((_CELL_VOLTAGE_BASE, by_base[_CELL_VOLTAGE_BASE]))
        if self._cycle == 1 and _SYSTEM_BLOCK_BASE in by_base:
            plan.append((_SYSTEM_BLOCK_BASE, by_base[_SYSTEM_BLOCK_BASE]))
        cold = sorted(
            base
            for base in by_base
            if base not in core_bases | {_CELL_VOLTAGE_BASE, _SYSTEM_BLOCK_BASE}
        )
        if cold and self._cycle % 8 == 0:
            chosen = cold[(self._cycle // 8) % len(cold)]
            plan.append((chosen, by_base[chosen]))
        return tuple(plan)

    def decode(
        self, blocks: Mapping[tuple[int, int], tuple[int, ...]], lifecycle: UnitLifecycle
    ) -> Observation:
        probe = self._probe_require()
        now = float(self._clock.monotonic())
        # Refresh the slow-block cache with this cycle's reads, keyed by base
        # address alongside the capture time each cache entry was actually read.
        for (address, _count), words in blocks.items():
            self._slow_cache[address] = (words, now)
        cell_base = _CELL_VOLTAGE_BASE
        if cell_base in self._slow_cache:
            captured, at = self._slow_cache[cell_base]
            if self._cell_meta is None or self._cell_meta[0] != at:
                self._cell_meta = (at, next(self._sequence))
        cell_captured: float | None = None
        cell_sequence: int | None = None
        if self._cell_meta is not None:
            cell_captured, cell_sequence = self._cell_meta
        # The actor keys blocks by read window; the wire decoder keys them by
        # base address.  Where two windows share a base (the seven-word
        # essential probe inside the 31-word BMS block), the fuller block is
        # the one the decoder consumes; cached slow blocks ride along with
        # their original registers so the decode stays a projection of what
        # the wire actually served.
        served: dict[int, tuple[int, ...]] = {}
        candidates: list[tuple[int, tuple[int, ...]]] = [
            (address, words) for (address, _count), words in blocks.items()
        ]
        candidates.extend((address, words) for address, (words, _at) in self._slow_cache.items())
        for address, words in candidates:
            current = served.get(address)
            if current is None or len(words) > len(current):
                served[address] = words
        return wire_decode.decode_observation(
            probe,
            served,
            unit_id=self._unit_id,
            expected_identity=self._expected_identity,
            expected_profile=self._expected_profile,
            expected_cell_count=self._expected_cell_count,
            wall_timestamp=self._clock.wall_now().astimezone(UTC),
            captured_at_mono=now,
            sequence=next(self._sequence),
            cell_captured_at_mono=cell_captured,
            cell_sequence=cell_sequence,
            lifecycle=lifecycle,
        )

    def _probe_require(self) -> register_layout.LayoutProbe:
        """The probe ``advance()`` read this cycle; the actor always awaits it first."""
        if self._probe is None:
            raise _ServedBankMismatchError(
                f"unit {self._unit_id!r} has no served layout probe for this telemetry cycle"
            )
        return self._probe


class _ActorCommandHandle:
    """Facade-facing per-unit handle; the actor keeps sole ownership of I/O."""

    def __init__(self, actor: EnergyPodActor) -> None:
        self._actor = actor

    @property
    def unit_id(self) -> str:
        return self._actor.unit_id

    @property
    def lifecycle(self) -> UnitLifecycle:
        return self._actor.lifecycle

    @property
    def qualified(self) -> bool | None:
        """The actor's own qualification report; ``None`` stays unknown."""
        return self._actor.qualified

    @property
    def inhibit_latched(self) -> bool:
        return bool(self._actor.inhibit_latched)

    async def arm(self) -> None:
        await self._actor.arm()

    async def disarm(self) -> None:
        await self._actor.disarm()

    async def acknowledge_inhibit(self) -> None:
        await self._actor.acknowledge_inhibit()

    async def request_bounded_zero(self, reason: str) -> None:
        await self._actor.request_bounded_zero(reason)

    async def fence(self, reason: str) -> int:
        # The facade's emergency stop fences the actor so an in-flight
        # nonzero heartbeat write is cancelled before the stop returns.
        return await self._actor.fence(reason)


class _UnresolvedCredentialAuthenticator:
    """Fail-closed bearer authentication until a credential store is composed.

    The configuration carries a secret *reference*, never a secret, so a
    reference alone never fabricates a principal: every bearer token is
    refused and the REST boundary answers with its structured 401 envelope.
    This stays the composed authenticator whenever no credential store is
    injected — every run-mode deployment without a store, and any
    configuration that references credentials without one.  Only the
    simulator grant below and an injected, configuration-referenced
    ``FileCredentialStore`` ever replace it.
    """

    async def authenticate(self, bearer_token: str) -> None:
        return None


@dataclass(slots=True)
class _DevelopmentPrincipal:
    """The one deterministic simulator-only principal (API_CONTRACTS).

    Identity, scopes, and interactivity are fixed constants of the simulator
    deployment; only ``site_id`` follows the configuration so the facade's
    cross-site refusal still applies.  The REST boundary's ``Principal``
    protocol declares settable attributes, and mypy models frozen-dataclass
    fields as read-only, so immutability is enforced by ownership instead:
    the authenticator mints this once and nothing else ever holds it.
    """

    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


def _announce_dev_credential_to_stdout(token: str) -> None:
    """The default startup sink: print the token once (API_CONTRACTS)."""
    print(f"energypod simulate: development principal bearer token: {token}")


class _DevelopmentPrincipalAuthenticator:
    """Simulator-only bearer authentication for the development principal.

    ``build_runtime`` composes this exactly when simulate mode is on and no
    credential store is configured: it mints one per-process token for the
    deterministic interactive development principal with full scopes and
    announces that token exactly once — through an injectable sink so tests
    can capture it, stdout by default.  Run mode, and any configuration that
    names a credential store (enabled or not), keeps the fail-closed
    authenticator instead: a secret reference names a store that does not
    exist in this milestone, and no principal is ever fabricated from one.
    """

    def __init__(self, *, site_id: str, announce: Callable[[str], None]) -> None:
        if not callable(announce):
            raise ValueError("announce must be callable")
        self._principal = _DevelopmentPrincipal(
            subject=_DEV_PRINCIPAL_SUBJECT,
            scopes=_DEV_PRINCIPAL_SCOPES,
            interactive=True,
            site_id=site_id,
        )
        self._token = f"dev-{secrets.token_urlsafe(32)}"
        self._announce = announce
        self._announced = False

    def announce_once(self) -> None:
        """Announce the token exactly once; re-entry stays silent."""
        if self._announced:
            return
        self._announced = True
        self._announce(self._token)

    async def authenticate(self, bearer_token: str) -> _DevelopmentPrincipal | None:
        # compare_digest requires ASCII; any non-ASCII offer is simply no
        # credential rather than an error the boundary would have to absorb.
        if not bearer_token.isascii():
            return None
        if secrets.compare_digest(bearer_token, self._token):
            return self._principal
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
        # Per-actor start reports: an actor loop resolves its own future only
        # once ``actor.start()`` has actually succeeded, so startup can never
        # read a lifecycle flip (an actor unwinding a failed connect sets
        # DISCONNECTED long before its task ends) as a started fleet.
        self._start_reports: dict[str, asyncio.Future[None]] = {}

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("supervision cannot restart after shutdown")
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._start_reports = {actor.unit_id: loop.create_future() for actor in self._actors}
        self._tasks = [
            asyncio.create_task(
                self._run_fleet(self._start_reports),
                name="energypod-supervision:fleet-cycle",
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
        """Startup completes only once every actor started, or fails loudly.

        A lifecycle that left BOOT is not evidence of a start: an actor whose
        connect failed flips its own lifecycle while its loop is still
        unwinding, and a fleet that serves with a dead actor owner has no
        polls, no observations, and no heartbeats.  Each actor loop therefore
        reports its own successful start, and any component failure — reported
        task or start report — fails startup.
        """
        while True:
            failure = self._failure()
            if failure is not None:
                raise failure
            if self._all_actors_started():
                return
            await asyncio.sleep(0)

    def _all_actors_started(self) -> bool:
        for report in self._start_reports.values():
            if not report.done() or report.cancelled():
                return False
            if report.exception() is not None:
                return False
        return True

    async def _run_fleet(self, start_reports: dict[str, asyncio.Future[None]]) -> None:
        """One fleet cycle: start, then heartbeat -> poll -> tick forever.

        The ordering is the load-bearing part (2026-08-22 live commissioning):
        with independent kernel/actor/poll timers, a fresh poll lands between
        the kernel's mint and the actor's heartbeat with near certainty, so the
        sequence-bound single-use authorization is stale by consumption time
        and no write ever happens. In one cycle: heartbeats run first and
        consume the authority minted by the PREVIOUS cycle's tick against the
        observation that has not changed since; polls then advance the
        observations concurrently (independent gateways, bounded by the read
        timeout); the kernel tick mints against those fresh observations,
        which the next cycle's heartbeats consume before any poll can
        invalidate them. Renewal spacing is therefore bounded by the cycle:
        sleep(interval) + at most one read timeout + the kernel timeout, all
        inside the commissioned device-command expiry.

        Start failures are component failures and propagate (the watcher
        halts the fleet). Heartbeat and poll failures are survived per cycle:
        the actor's own state machine fences/inhibits on write failures, and
        unreadable telemetry fails closed through authorization expiry and
        kernel staleness checks. A kernel tick failure ends the task and
        fences the fleet.
        """
        for actor in self._actors:
            try:
                await actor.start()
            except BaseException as error:
                report = start_reports.get(actor.unit_id)
                if report is not None and not report.done():
                    report.set_exception(error)
                raise
            report = start_reports.get(actor.unit_id)
            if report is not None and not report.done():
                report.set_result(None)
        while True:
            await self._clock.sleep(self._interval_s)
            for actor in self._actors:
                try:
                    with contextlib.suppress(Exception):
                        await actor.heartbeat_once()
                except asyncio.CancelledError:
                    # A facade fence (emergency stop) cancels in-flight
                    # authority work; that borrowed cancellation must not end
                    # the fleet cycle. Genuine shutdown of this task carries a
                    # real cancellation request, which wins.
                    task = asyncio.current_task()
                    if task is None or task.cancelling():
                        raise
            # API_CONTRACTS "Unit actor": an overdue read is abandoned
            # rather than delaying a heartbeat past its safety margin. In the
            # fleet cycle the bound is structural — a poll that overruns the
            # interval is cancelled (its observation is only appended at the
            # end of the poll, so abandoned reads never become evidence) and
            # the cycle proceeds to the tick, keeping renewal cadence.
            await asyncio.gather(
                *(self._bounded_poll(actor) for actor in self._actors),
                return_exceptions=True,
            )
            await self._kernel.tick()

    async def _bounded_poll(self, actor: EnergyPodActor) -> None:
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(_poll_once(actor), timeout=self._interval_s)

    async def _watch_for_failure(self) -> None:
        if not self._tasks:
            return
        await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
        failure = self._failure()
        if failure is not None and not self._stopped:
            # Record the triggering exception for diagnostics before the
            # tasks are cancelled and the evidence disappears.
            import traceback

            self.halt_evidence = "".join(
                traceback.format_exception(type(failure), failure, failure.__traceback__)
            )
            # Observability (ledger follow-up): a supervisor failure must name
            # itself in the process log, not only in inspectable state.
            print("SUPERVISOR FAILURE:", self.halt_evidence, flush=True)
            await self._halt("supervisor_failure")

    def _failure(self) -> BaseException | None:
        for task in self._tasks:
            if task.done() and not task.cancelled():
                exception = task.exception()
                if exception is not None:
                    return exception
        for report in self._start_reports.values():
            if report.done() and not report.cancelled():
                exception = report.exception()
                if exception is not None:
                    return exception
        return None

    async def _cancel_tasks(self) -> None:
        """Cancel every supervision task except the one doing the cancelling.

        The failure watcher runs `_halt`, which runs this method; cancelling
        the watcher from inside its own halt would abort the halt before the
        actors' bounded-zero shutdown, and gathering over a set that contains
        the caller would recurse through the gather's own cancellation.
        """
        current = asyncio.current_task()
        watcher, self._watcher = self._watcher, None
        pending = [
            task for task in (*self._tasks, watcher) if task is not None and task is not current
        ]
        self._tasks = []
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _halt(self, reason: str) -> None:
        """Fence every generation, revoke, stop loops, and shut actors down.

        The generation fence is published before any potentially blocking
        repository or audit work, matching the emergency-revocation order, and
        both the fence and the revocation land before any actor lifecycle
        change an observer could read as "shutdown started" — cancelled actor
        loops flip their actor to DISCONNECTED as they unwind, so authority
        must already be gone by then.

        Actor shutdown is unconditional: it runs in a ``finally`` so no
        cancellation or repository failure can leave an actor holding
        authority, an unbounded objective, or an open transport.
        """
        self._stopped = True
        try:
            with contextlib.suppress(Exception):
                await self._coordinator.advance(reason=reason)
            with contextlib.suppress(Exception):
                await self._authorizations.revoke(reason=reason)
            # Loops stop before actor shutdown so no start/poll races it.
            await self._cancel_tasks()
        finally:
            for actor in self._actors:
                with contextlib.suppress(Exception):
                    await actor.shutdown()


async def _poll_once(actor: EnergyPodActor) -> None:
    """One supervised telemetry cycle; a poll failure is survived, not fatal."""
    with contextlib.suppress(Exception):
        await actor.poll_once()


_LAST_SUPERVISION: _Supervision | None = None


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

    The intent, observation, and authorization handles are the same async port
    adapters the kernel, actors, and facade drive — awaiting a handle and
    driving the control loop touch exactly the same state.  The audit and
    schedule handles stay the shipped durable stores those ports wrap.
    ``simulators`` is populated only in simulate mode.
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
    intents: _AsyncIntentRepository
    observations: _AsyncObservationRepository
    authorizations: _AsyncAuthorizationRepository
    audit: _AuditStore
    schedule: SQLiteScheduleRepository | _InMemoryScheduleRepository
    app: FastAPI
    mcp_server_factory: Callable[..., FastMCP]
    simulators: Mapping[str, SimulatedEnergyPod] | None


def _simulator_pod(
    unit: Any, index: int, clock: Clock, *, command_expiry_s: float
) -> SimulatedEnergyPod:
    bic_count = min(_MAX_SIMULATOR_BICS, max(1, -(-unit.expected_cell_count // _CELLS_PER_BIC)))
    served_cells = bic_count * _CELLS_PER_BIC
    if served_cells < unit.expected_cell_count:
        # A cell count the evidenced packing cannot serve is a wiring error,
        # not a runtime discovery: composing it would boot a unit that can
        # never present a complete cell window and therefore never qualify.
        raise ValueError(
            f"unit {unit.unit_id!r} expects {unit.expected_cell_count} cells but the evidenced "
            f"IoT packing serves at most {served_cells} cells for a simulated unit; compose a "
            "count the register plan can serve"
        )
    # The seed is the unit's configuration position, so identical
    # configurations produce identical register banks across builds.  The
    # device's command lease follows the commissioned expiry evidence, so the
    # simulated watchdog fences exactly when the configured budget says so.
    return SimulatedEnergyPod(
        clock=clock,
        identity=unit.expected_identity,
        bic_count=bic_count,
        seed=index,
        watchdog_timeout_s=command_expiry_s,
    )


def _validate_actor_timing_wiring(config: ControllerConfig) -> None:
    """Eager wiring check mirrored from the actor's own constructor.

    ``timing.write_timeout_s`` is wired as the actor's heartbeat safety margin
    and ``timing.control_period_s`` as its heartbeat interval, so a write
    timeout that cannot fit strictly inside the control period can never
    compose.  It is rejected here — before any durable store is opened — so
    the configuration validator and the composition root agree on exactly the
    same set of commissionable timing budgets.
    """
    margin = float(config.timing.write_timeout_s)
    interval = float(config.timing.control_period_s)
    if not 0 <= margin < interval:
        raise ValueError(
            "timing.write_timeout_s is wired as the heartbeat safety margin and must fit "
            "strictly inside timing.control_period_s (the heartbeat interval)"
        )


def build_runtime(
    config: ControllerConfig,
    *,
    simulate: bool = False,
    clock: Clock | None = None,
    dev_credential_announce: Callable[[str], None] | None = None,
    credential_store: FileCredentialStore | None = None,
) -> ComposedRuntime:
    """Construct the whole controller graph; the only composition point.

    Construction is eager and side-effect free apart from opening the durable
    SQLite store when one is configured — and, in simulate mode with no
    credential store configured, printing the minted development credential's
    token once (API_CONTRACTS "Operations surface"; ``dev_credential_announce``
    replaces the stdout sink so tests can capture it).  No task starts, no
    socket opens, no transport connects, and nothing is restored from
    persistence.  Wiring is validated before that store opens, and a
    composition that cannot complete closes it again, so a rejected
    configuration leaves nothing on disk.

    ``credential_store`` injects the run-mode credential store; it is used
    only when ``config.authentication`` references credentials (present and
    enabled), and is otherwise left unused — the configuration, not the
    injection, is the authority.
    """
    _validate_actor_timing_wiring(config)
    resolved_clock = clock if clock is not None else _SystemClock()
    storage = config.storage
    if storage is None or simulate:
        return _build_runtime(
            config,
            simulate=simulate,
            resolved_clock=resolved_clock,
            database=None,
            dev_credential_announce=dev_credential_announce,
            credential_store=credential_store,
        )
    database = SQLiteDatabase(
        storage.database_path,
        busy_timeout_ms=storage.busy_timeout_ms,
    )
    try:
        database.open()
    except BaseException:
        with contextlib.suppress(Exception):
            database.close()
        raise
    try:
        return _build_runtime(
            config,
            simulate=simulate,
            resolved_clock=resolved_clock,
            database=database,
            dev_credential_announce=dev_credential_announce,
            credential_store=credential_store,
        )
    except BaseException:
        # A composition that cannot complete must not leave an open durable
        # store — and its live WAL siblings — behind on disk.
        with contextlib.suppress(Exception):
            database.close()
        raise


def _credential_store_referenced(config: ControllerConfig) -> bool:
    """True when the configuration's authentication block references credentials.

    The configuration, not the injection, is the authority: an injected
    ``FileCredentialStore`` composes only for a deployment whose
    authentication block is present and enabled, and is otherwise ignored.
    """
    authentication = config.authentication
    return authentication is not None and authentication.enabled


def _build_runtime(
    config: ControllerConfig,
    *,
    simulate: bool,
    resolved_clock: Clock,
    database: SQLiteDatabase | None,
    dev_credential_announce: Callable[[str], None] | None = None,
    credential_store: FileCredentialStore | None = None,
) -> ComposedRuntime:
    unit_ids = frozenset(unit.unit_id for unit in config.units)
    process_instance_id = f"energypod-{uuid.uuid4().hex}"
    process_origin_mono = float(resolved_clock.monotonic())

    # --- persistence -----------------------------------------------------
    # SQLite backs exactly the durable audit and schedule stores, and only
    # when a database path is configured and simulator mode is off.  Intents,
    # observations, and authorizations are always process-local so no restored
    # authority can survive a restart (observe-only boot).
    audit_store: _AuditStore
    schedule_store: SQLiteScheduleRepository | _InMemoryScheduleRepository
    if database is not None:
        # The audit read path projects the durable row sequence so the facade
        # cursor pages the same way in every deployment mode.
        audit_store = _SequencedSQLiteAuditRepository(database)
        schedule_store = SQLiteScheduleRepository(database)
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
    # The arbiter owns the live intent store so its exact-id acknowledgement
    # removes a latched stop durably, not just from its own latch.
    arbiter = IntentArbiter(intent_repository=intent_store)

    # --- async ports over the exposed stores ------------------------------
    audit_port = _AsyncAuditRepository(audit_store, bus=bus)
    observation_port = _AsyncObservationRepository(store=observation_store, bus=bus)
    intent_port = _AsyncIntentRepository(intent_store, arbiter=arbiter)
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
        arbiter=arbiter,
        allocator=_FleetAllocatorAdapter(),
        safety=SafetyKernel(),
        policy=policy,
        generation_coordinator=coordinator,
        configuration_version=config.revision,
        audit_event_factory=audit_event_factory,
    )

    # --- one sole-owner actor per configured unit ---------------------------
    probe = RegisterCatalog().layout_probe
    # Run mode's qualification path is decided here, once, from the validated
    # configuration mode (API_CONTRACTS "Write-enabled run mode"): a
    # write-enabled deployment — which the configuration validator already
    # forced to carry a policy, enabled authentication, the measured
    # live-trial expiry evidence, and a cadence inside the corroborated
    # envelope — composes the policy's stable-sample threshold and the
    # arm-time external-writer preflight over the served objective readback;
    # an observe-only deployment keeps the structural never-qualify wiring
    # and today's arm path, exactly as before.  Simulate mode keeps the
    # commissioning threshold it always had; its deterministic device models
    # own the watchdog and writer behaviors the live preflight probes for.
    write_enabled = config.mode is ControllerMode.WRITE_ENABLED
    actors: dict[str, EnergyPodActor] = {}
    simulators: dict[str, SimulatedEnergyPod] | None = {} if simulate else None
    for index, unit in enumerate(config.units):
        telemetry: _SimulatorTelemetry | _LiveDecodeTelemetry | None = None
        if simulate:
            pod = _simulator_pod(
                unit,
                index,
                resolved_clock,
                command_expiry_s=config.timing.device_command_expiry_s,
            )
            transport: SimulatorTransport | _LazyWaveshareTransport = SimulatorTransport(pod=pod)
            telemetry = _SimulatorTelemetry(
                pod=pod,
                clock=resolved_clock,
                unit_id=unit.unit_id,
                expected_profile=unit.protocol_profile.value,
                expected_cell_count=unit.expected_cell_count,
            )
            if simulators is not None:
                simulators[unit.unit_id] = pod
        else:
            # The production transport is configured (and validated) here but
            # constructed lazily on the serving loop: pymodbus's client binds
            # the running loop at construction, and composition itself runs
            # with no loop and must open no sockets.
            transport = _LazyWaveshareTransport(
                _production_transport_factory(
                    unit,
                    config.timing.essential_read_timeout_s,
                    config.timing.inter_request_delay_s,
                )
            )
            # Run mode decodes the served register bank with the production
            # wire decoder over the same lazy transport the actor owns, so one
            # telemetry cycle probes the served layout, reads the plan the
            # probe justifies, and delivers the decoded observation through
            # the actor's accept-observation path.
            telemetry = _LiveDecodeTelemetry(
                transport=transport,
                clock=resolved_clock,
                unit_id=unit.unit_id,
                expected_identity=unit.expected_identity,
                expected_profile=unit.protocol_profile.value,
                expected_cell_count=unit.expected_cell_count,
                probe_address=probe.address,
                probe_count=probe.count,
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
            # Structural observe-only for observe-only run mode: the
            # qualification threshold is wired beyond any reachable count
            # because the per-unit commissioning evidence that would justify
            # qualification is not composed.  Write-enabled run mode and
            # simulate mode compose the policy's commissioning count.
            stable_observations_required=(
                policy.stable_samples_needed_to_rearm
                if simulate or write_enabled
                else _RUN_MODE_STABLE_SAMPLES_REQUIRED
            ),
            essential_read_address=probe.address,
            essential_read_count=probe.count,
            heartbeat_interval_s=policy.heartbeat_interval_s,
            # The write timeout is the heartbeat safety margin: the margin a
            # renewal may eat into before its deadline, and the bound on the
            # bounded-zero attempt.
            heartbeat_safety_margin_s=config.timing.write_timeout_s,
            # The arm-time external-writer preflight port (API_CONTRACTS
            # "Write-enabled run mode", bullet 3): pinned only for a
            # write-enabled run-mode composition, so the actor proves sole-
            # writer authority by reading the served PQ objective readback
            # before ARMED_IDLE.  Observe-only run mode and simulate mode pass
            # the default None and keep today's arm path exactly.
            objective_readback_address=(
                _OBJECTIVE_READBACK_ADDRESS if write_enabled and not simulate else None
            ),
            blocking_fault_codes=frozenset(policy.blocking_fault_codes),
            telemetry=telemetry,
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

    # API_CONTRACTS "Operations surface": simulate mode with no configured
    # credential store mints exactly one deterministic development principal
    # (full scopes, interactive) and announces its per-process token once —
    # composition is the controller's startup.  A credential store injected
    # for a configuration whose authentication block references credentials is
    # the composed authenticator instead (and the simulator grant is then
    # never minted).  Everything else — run mode without a store, a
    # configuration that references credentials without one, a store injected
    # without a reference — stays fail-closed: every bearer is refused, so no
    # principal is fabricated outside the simulator grant and the store.
    authenticator: (
        _DevelopmentPrincipalAuthenticator
        | _UnresolvedCredentialAuthenticator
        | FileCredentialStore
    )
    if credential_store is not None and _credential_store_referenced(config):
        authenticator = credential_store
    elif simulate and config.authentication is None:
        dev_authenticator = _DevelopmentPrincipalAuthenticator(
            site_id=config.site.site_id,
            announce=(
                _announce_dev_credential_to_stdout
                if dev_credential_announce is None
                else dev_credential_announce
            ),
        )
        dev_authenticator.announce_once()
        authenticator = dev_authenticator
    else:
        authenticator = _UnresolvedCredentialAuthenticator()
    app = create_api_app(
        service=adapter_service,
        authenticator=authenticator,
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
    global _LAST_SUPERVISION
    _LAST_SUPERVISION = supervision
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
        intents=intent_port,
        observations=observation_port,
        authorizations=authorization_port,
        audit=audit_store,
        schedule=schedule_store,
        app=app,
        mcp_server_factory=mcp_server_factory,
        simulators=simulators,
    )


__all__ = ["ComposedRuntime", "build_runtime"]
