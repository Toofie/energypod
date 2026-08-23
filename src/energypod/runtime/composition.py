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
from os import environ
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
from energypod.adapters.modbus.waveshare import TransportConnectionError
from energypod.adapters.persistence.memory import (
    InMemoryAuthorizationRepository,
    InMemoryEnergyLedgerRepository,
    InMemoryIntentRepository,
    InMemoryObservationRepository,
    InMemoryTelemetryHistoryRepository,
)
from energypod.adapters.persistence.sqlite import (
    PersistenceBusyError,
    SQLiteAuditRepository,
    SQLiteDatabase,
    SQLiteEnergyLedgerRepository,
    SQLiteScheduleRepository,
    SQLiteTelemetryHistoryRepository,
)
from energypod.adapters.providers.http import HttpxForecastTransport
from energypod.adapters.providers.load_baseline import HistorianLoadForecast
from energypod.adapters.providers.open_meteo import OpenMeteoPvForecast, OpenMeteoWeather
from energypod.adapters.providers.ports import (
    LoadForecastProvider,
    PvForecastProvider,
    WeatherProvider,
)
from energypod.adapters.providers.registry import ForecastProviderRegistry
from energypod.adapters.providers.solcast import SolcastPvForecast
from energypod.adapters.providers.tariff_static import StaticTariffProvider, TariffRateWindow
from energypod.api.mcp import create_mcp_server
from energypod.api.rest import create_api_app
from energypod.application.actor import EnergyPodActor
from energypod.application.arbiter import STOP_ACKNOWLEDGE_SCOPE, IntentArbiter
from energypod.application.audit import AuditEventFactory
from energypod.application.control_kernel import ControlKernel
from energypod.application.energy import (
    EnergyAccountant,
    EnergyAccountingSettings,
    EnergyScorecardControl,
)
from energypod.application.events import EventBus
from energypod.application.excess_charge import (
    ExcessAdviserController,
    ExcessChargeAdviser,
    ExcessChargeSettings,
    eligible_export_charge_w,
)
from energypod.application.foreign_objective import (
    ForeignObjectiveMonitor,
    ForeignObjectiveSettings,
)
from energypod.application.generation import AuthorityGenerationCoordinator
from energypod.application.history import PlantHistoryControl, TelemetryHistorian
from energypod.application.night_charge import (
    NightChargeAdviser,
    NightChargeController,
    NightChargeSettings,
)
from energypod.application.recovery import (
    CONNECT_FAILED,
    ECHO_UNREADABLE,
    READ_FAILED,
    READ_OK,
    RecoveryMonitor,
    RecoverySettings,
)
from energypod.application.safety import SafetyKernel
from energypod.application.scheduling import (
    ScheduleEvaluator,
    SchedulePolicy,
    ScheduleRunner,
    ScheduleSurfaceControl,
    parse_hhmm,
)
from energypod.application.service import (
    EXCESS_ECONOMICS_ACK_EVENT_ID,
    SCHEDULE_NIGHT_ACK_EVENT_ID,
    EnergyServiceFacade,
)
from energypod.domain import (
    ControlPolicy,
    DataQuality,
    Direction,
    IntentSource,
    Observation,
    UnitHeadroom,
    UnitLifecycle,
    allocate_fleet_power,
)
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.energy import GRID_SOURCE_DEVICE_COUNTER, GRID_SOURCE_INTEGRATED
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
_PCS_LIVE_BLOCK_BASE = 0x1000
# The PCS detail block carries the served PQ objective at +17/+18 (the night-
# writer detector's window, PROTOCOL_EVIDENCE 4b); the one-word debug-mode
# readback rides the common reads.
_PCS_DETAIL_BLOCK_BASE = 0x1060
_BMS_BLOCK_BASE = 0x5000
_PCS_FAULT_BLOCK_BASE = 0x1040
_DCDC_FAULT_BLOCK_BASE = 0x2040
_BMS_FAULT_BLOCK_BASE = 0x5040
_CELL_VOLTAGE_BASE = 0x5200
_CELL_TEMPERATURE_BASE = 0x523C
_IDENTITY_BLOCK_BASE = 0x8106
# The one-word debug-mode readback (field-mapping S2.14): the vendor's
# PQ-dispatch precondition (MiniESapp.cs:2180 refuses when nonzero).  It rides
# the core like the identity pair so the dispatch gate (2026-08-23 incident 1)
# judges the precondition at the control rate, never a cold-ring refresh old
# enough to have missed a mode flip.
_DEBUG_MODE_BLOCK_BASE = 0x8100
# Cumulative-energy totals block (DESIGN_ENERGY_SCORECARD section 5): six
# low-word-first uint32 x 0.1 kWh pairs in vendor order, riding the cold ring.
_ENERGY_TOTALS_BLOCK_BASE = 0x4101

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

# SYNC_RESILIENCE_AUDIT B4 (2026-08-24): the cell-derived deny reasons whose
# decision promotes the 0x5200 window into the next poll of every unit's
# actor, so the deny is re-evaluated on FRESH cell data at most one cycle
# later (the window otherwise rides the every-3rd-cycle tier and may judge a
# cached window up to ~3 s old).  ``cell_count_invalid`` is included: it is
# judged on the same served window.
CELL_DENY_REASONS: frozenset[str] = frozenset(
    {"cell_voltage_low", "cell_voltage_high", "cell_imbalance", "cell_count_invalid"}
)


def decision_requests_cell_refresh(decision: Any) -> bool:
    """Whether a tick's decision carries any cell-derived deny reason (B4)."""
    if decision is None:
        return False
    codes = getattr(decision, "reason_codes", None)
    return isinstance(codes, tuple | list | set | frozenset) and bool(
        set(codes) & CELL_DENY_REASONS
    )


# API_CONTRACTS "Excess-solar accelerated charging (advisory)": the composed
# automation principal the excess-charge adviser submits under.  Audit
# attribution relies on principal plus the ``optimizer`` source tag — local
# console and agent traffic is ``operator:local`` + ``manual``/``agent``, so
# the adviser is always distinguishable.  Non-interactive, site-bound, and
# holding nothing beyond what an ordinary dispatch needs: the adviser has no
# special authority anywhere.
_EXCESS_ADVISER_PRINCIPAL_SUBJECT = "energypod:excess-adviser"
_EXCESS_ADVISER_PRINCIPAL_SCOPES = frozenset({"observe", "dispatch"})

# DESIGN_NIGHT_CHARGE §2.1: the composed automation principal the night
# strategy adviser submits under — the excess/schedule adviser pattern
# exactly.  Audit attribution separates its rows by principal plus the
# ``optimizer`` source tag; non-interactive, site-bound, and holding nothing
# beyond what an ordinary dispatch needs.
_NIGHT_ADVISER_PRINCIPAL_SUBJECT = "energypod:night-adviser"
_NIGHT_ADVISER_PRINCIPAL_SCOPES = frozenset({"observe", "dispatch"})

# DESIGN_SCHEDULES §2: the composed automation principal the schedule runner
# submits under — the adviser pattern exactly.  Audit attribution separates
# the runner's rows by principal plus the ``schedule`` source tag; the
# principal is non-interactive, site-bound, and holds nothing beyond what an
# ordinary dispatch needs.
_SCHEDULE_RUNNER_PRINCIPAL_SUBJECT = "energypod:schedule-runner"
_SCHEDULE_RUNNER_PRINCIPAL_SCOPES = frozenset({"observe", "dispatch"})
# DESIGN_ENERGY_SCORECARD section 7 (E4): the deterministic event id of the
# operator's P-A1-active pinning fact.  A ``grid_counter_roles`` value other
# than ``unpinned`` boots only when this fact exists in the durable store --
# one keyed existence check, the excess-economics precedent.
ENERGY_ROLES_PINNED_EVENT_ID = "energy-counter-roles-pinned"


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

    def contains_event(self, event_id: str) -> bool:
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise ValueError("event_id must be non-empty and normalized")
        # The retained window is bounded; an evicted fact is gone from this
        # process's memory (the durable SQLite store is the boot-load path).
        return event_id in self._seen_event_ids


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

    def contains_event(self, event_id: str) -> bool:
        """One keyed existence check over the durable rows (never a scan).

        The once-ever net-billing acknowledgement carries a deterministic
        event id, so the boot-time gate is an indexed lookup against the
        store's own UNIQUE constraint.
        """
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise ValueError("event_id must be non-empty and normalized")
        try:
            with self._database.lock:
                row = self._database.connection.execute(
                    "SELECT 1 FROM audit_events WHERE event_id = ? LIMIT 1", (event_id,)
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                raise PersistenceBusyError("audit database is busy") from exc
            raise
        return row is not None


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


def _payload_enum(raw: Any) -> Any:
    """JSON-native spelling of a domain enum for a bus payload."""
    return getattr(raw, "value", raw)


class _AsyncIntentRepository:
    """Awaitable intent port over the process-local intent store.

    Removing a latched emergency stop also completes the arbiter's own
    acknowledgement protocol: the arbiter latches a selected stop internally,
    and a latch that outlived its intent would keep fencing every future cycle
    exactly as if the stop were still live.  The runtime principal carries the
    stop-acknowledge scope here only after the facade has already admitted the
    human operator with that scope; this bridge is wiring, not authority.

    The port is also where intent TTL lapses become observable (2026-08-23
    observability root cause): the kernel's no-winner revoke publishes bus
    events only while a unit still holds a capability, which is never true at
    expiry, so an intent silently vanished from the console.  Every ``active``
    read compares the store's answer against the intents this port tracked and
    announces each newly lapsed intent exactly once as ``intent.expired`` --
    the read the kernel itself performs each tick, so the expiry the fleet
    actually acted on is the one the bus carries.  An intent that leaves by
    removal (cancel, stop acknowledgement) never announces an expiry.
    """

    def __init__(
        self, store: InMemoryIntentRepository, *, arbiter: IntentArbiter, bus: EventBus
    ) -> None:
        self._store = store
        self._arbiter = arbiter
        self._bus = bus
        self._tracked: dict[str, Any] = {}

    async def add(self, intent: Any) -> None:
        self._store.add(intent)
        intent_id = getattr(intent, "id", None)
        if isinstance(intent_id, str):
            self._tracked[intent_id] = intent

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        active = self._store.active(now_mono)
        active_ids = {getattr(intent, "id", None) for intent in active}
        lapsed = [
            (intent_id, intent)
            for intent_id, intent in self._tracked.items()
            if intent_id not in active_ids
        ]
        for intent_id, _intent in lapsed:
            del self._tracked[intent_id]
        for _intent_id, intent in lapsed:
            # Observability must never gate control: the kernel's tick reads
            # through this method, so a failed announcement is suppressed
            # rather than allowed to fail the cycle.
            with contextlib.suppress(Exception):
                await self._bus.publish(
                    {
                        "type": "intent.expired",
                        "payload": {
                            "intent_id": getattr(intent, "id", None),
                            "source": _payload_enum(getattr(intent, "source", None)),
                            "direction": _payload_enum(getattr(intent, "direction", None)),
                            "watts": getattr(intent, "watts", None),
                            "unit_ids": sorted(getattr(intent, "selected_unit_ids", ()) or ()),
                        },
                    }
                )
        return active

    async def remove(self, intent_id: str) -> None:
        # A removal is deliberate: untrack first so it can never be announced
        # as a TTL lapse by a concurrent or later ``active`` read.
        self._tracked.pop(intent_id, None)
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

    def contains_event(self, event_id: str) -> bool: ...


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
                    # Watt figures render straight off the stream (2026-08-23):
                    # the console must not re-fetch audit pages to label a
                    # request card's requested/authorized power.  The per-unit
                    # breakdowns ride the same payload (the 2026-08-23
                    # fleet-row opacity fix) so a multi-battery card can name
                    # each battery's own figures, and -- since a concurrent
                    # cycle may run different directions on different units
                    # (2026-08-24) -- each unit's own direction rides too.
                    "requested_active_w": getattr(event, "requested_active_w", None),
                    "authorized_active_w": getattr(event, "authorized_active_w", None),
                    "requested_watts_by_unit": getattr(event, "requested_watts_by_unit", None),
                    "authorized_watts_by_unit": getattr(event, "authorized_watts_by_unit", None),
                    "directions_by_unit": getattr(event, "directions_by_unit", None),
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
        # Symmetry with ``authorization.revoked`` (2026-08-23): a batch that
        # lands in the store is announced as ``authorization.granted`` with
        # the cycle, generation, and units it carries authority for -- grants
        # were store-only, so the console never saw authority arrive.  A
        # concurrent cycle may run different directions on different units
        # (2026-08-24), so each unit's own authorized watts and direction ride
        # the announcement.  The store hold precedes the announcement and a
        # failed announcement is suppressed: observability never gates the
        # grant.
        authorizations = tuple(getattr(batch, "authorizations", ()))
        with contextlib.suppress(Exception):
            await self._bus.publish(
                {
                    "type": "authorization.granted",
                    "payload": {
                        "cycle_id": getattr(batch, "cycle_id", None),
                        "generation": getattr(batch, "generation", None),
                        "unit_ids": sorted(
                            getattr(item, "unit_id", None) for item in authorizations
                        ),
                        "watts_by_unit": {
                            getattr(item, "unit_id", None): getattr(item, "watts", None)
                            for item in sorted(authorizations, key=lambda item: item.unit_id)
                        },
                        "directions_by_unit": {
                            getattr(item, "unit_id", None): _payload_enum(
                                getattr(item, "direction", None)
                            )
                            for item in sorted(authorizations, key=lambda item: item.unit_id)
                        },
                    },
                }
            )

    async def current(self, unit_id: str, now_monotonic: float) -> Any | None:
        return self._store.current(unit_id, now_monotonic)

    async def peek(self, unit_id: str) -> Any | None:
        return self._store.peek(unit_id)

    async def revoked_through(self, unit_ids: Iterable[str]) -> int:
        return self._store.revoked_through(unit_ids)

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
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": only the
    # allocator's optimizer-charge proposals carry the export-bounded flag,
    # which is what arms the kernel's fleet-wide export-evidence denial.
    export_bounded: bool = False


class _FleetAllocatorAdapter:
    """Adapt the deterministic domain allocator to the kernel's allocator port."""

    def allocate(
        self,
        intent: Any,
        observations: Mapping[str, Any],
        policy: ControlPolicy,
        now_mono: float,
        unit_ids: frozenset[str] | None = None,
    ) -> tuple[_FleetProposal, ...]:
        export_cap_w: int | None = None
        export_bounded = False
        if (
            getattr(intent, "source", None) is IntentSource.OPTIMIZER
            and intent.direction is Direction.CHARGE
            and not str(getattr(intent, "id", "")).startswith("night-")
        ):
            # API_CONTRACTS "Excess-solar accelerated charging (advisory)":
            # for an OPTIMIZER charge intent the measured-export bound is one
            # additional min() term on the allocation demand — computed from
            # the fleet observations and policy the allocator already
            # receives, fail-closed to 0 (an all-zero allocation, which stays
            # a legitimate representation) on any missing/bad/stale grid
            # evidence or an unarmed policy triple.  No other source or
            # direction is ever export-bounded.
            #
            # DESIGN_NIGHT_CHARGE §2.1/§6 — the one carve-out, pinned by the
            # night twin's own ``night-`` intent-id prefix: the night
            # strategy's OPTIMIZER charges are PAID off-peak import inside
            # the commissioned window, so the export bound (whose whole point
            # is that an advisory charge may flow only from measured FREE
            # surplus) must never apply to them — at night the bound is
            # structurally 0 and the window could never charge.  The excess
            # adviser itself is unchanged: its intents stay export-bounded,
            # and the dawn-corner precedence stays one-sided exactly as
            # pinned (§4.2).
            export_cap_w = eligible_export_charge_w(observations, policy, now_mono)
            export_bounded = True
        # Concurrent per-unit arbitration (2026-08-24): the kernel passes the
        # intent's SURVIVING scope; the domain allocator narrows the selection
        # and re-sums per-unit targets over it.
        selected = sorted(intent.selected_unit_ids if unit_ids is None else frozenset(unit_ids))
        headrooms = tuple(
            self._headroom(unit_id, observations.get(unit_id), policy) for unit_id in selected
        )
        allocation = allocate_fleet_power(
            intent, headrooms, export_cap_w=export_cap_w, unit_ids=unit_ids
        )
        return tuple(
            _FleetProposal(
                unit_id=unit_id,
                direction=intent.direction,
                watts=int(allocation.allocations[unit_id]),
                intent_id=intent.id,
                intent_expires_at_mono=float(intent.expires_at_mono),
                export_bounded=export_bounded,
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
        decode_energy_totals: bool = True,
    ) -> None:
        self._pod = pod
        self._clock = clock
        self._unit_id = unit_id
        self._expected_profile = expected_profile
        self._expected_cell_count = expected_cell_count
        # DESIGN_ENERGY_SCORECARD section 7: the six cumulative fields decode
        # only when the ``energy_scorecard`` block is present -- an absent
        # block composes no energy decode, keeping observations byte-
        # identical to the pre-scorecard shape.
        self._decode_energy_totals = bool(decode_energy_totals)
        catalog = register_layout.RegisterCatalog()
        self._plan = tuple(
            (block.address, block.count)
            for block in (*catalog.iot_reads(bic_count=pod.bic_count), *catalog.common_reads)
        )
        windows = {address: (address, count) for address, count in self._plan}
        self._system_window = windows[_SYSTEM_BLOCK_BASE]
        self._pcs_live_window = windows[_PCS_LIVE_BLOCK_BASE]
        self._pcs_detail_window = windows[_PCS_DETAIL_BLOCK_BASE]
        self._debug_mode_window = windows[_DEBUG_MODE_BLOCK_BASE]
        self._bms_window = windows[_BMS_BLOCK_BASE]
        self._fault_windows = (
            (faults.FaultBlock.IOT_PCS, windows[_PCS_FAULT_BLOCK_BASE]),
            (faults.FaultBlock.IOT_DCDC, windows[_DCDC_FAULT_BLOCK_BASE]),
            (faults.FaultBlock.IOT_BMS, windows[_BMS_FAULT_BLOCK_BASE]),
        )
        self._cell_voltage_window = windows[_CELL_VOLTAGE_BASE]
        self._cell_temperature_window = windows[_CELL_TEMPERATURE_BASE]
        self._totals_window = windows[_ENERGY_TOTALS_BLOCK_BASE]

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
        pcs_live = blocks[self._pcs_live_window]
        pcs_detail = blocks[self._pcs_detail_window]
        debug_mode = blocks[self._debug_mode_window]
        bms = blocks[self._bms_window]
        fault_codes, warning_codes = self._decode_fault_signals(blocks)
        cells = blocks[self._cell_voltage_window]
        temperatures = blocks[self._cell_temperature_window]
        served_temperatures = self._verify_served_bank(bms, cells, temperatures)

        # MUTATION-3/6 (blanket-GOOD masking): every field's quality judgment
        # is DERIVED from the served words with the production wire decoder's
        # own fail-closed helpers -- signed measurements, 0-100 percentages,
        # non-negative dynamic limits.  The simulator is the reference model,
        # so a sentinel word (a malformed-injected limit served as its
        # complement, 62,535 unsigned) must fail closed here exactly as the
        # live wire decode refuses it: value absent, quality BAD -- never a
        # giant valid limit reported GOOD.  The helpers are imported from the
        # decoder deliberately: one implementation, zero semantic drift.
        grid_power_w, grid_quality = wire_decode._measurement(pcs_live, 17, 1.0)
        load_power_w, load_quality = wire_decode._measurement(pcs_live, 20, 1.0)
        # The six cumulative-energy pairs decode through the SAME helper the
        # live wire decoder uses (one implementation, zero drift); absent
        # feature keeps every field absent and the quality map at twelve keys.
        energy_totals = (
            wire_decode.decode_totals_block(blocks[self._totals_window])
            if self._decode_energy_totals
            else ((None, DataQuality.MISSING),) * 6
        )
        (
            (energy_grid_a_kwh, energy_grid_a_quality),
            (energy_grid_b_kwh, energy_grid_b_quality),
            (energy_load_kwh, energy_load_quality),
            (energy_pv_kwh, energy_pv_quality),
            (energy_charge_kwh, energy_charge_quality),
            (energy_discharge_kwh, energy_discharge_quality),
        ) = energy_totals
        system_soc_word = wire_decode._served_word(system, 17)
        if system_soc_word is None:  # pragma: no cover - the plan serves it
            system_soc_pct, system_soc_quality = None, DataQuality.MISSING
        else:
            # Mapping S2.13: the system SOC is the word's low byte.
            system_soc_pct = float(system_soc_word & 0xFF)
            system_soc_quality = (
                DataQuality.GOOD if 0.0 <= system_soc_pct <= 100.0 else DataQuality.BAD
            )
            if system_soc_quality is DataQuality.BAD:
                system_soc_pct = None
        soh_pct, soh_quality = wire_decode._percentage(system, 22)
        battery_watts, watts_quality = wire_decode._measurement(system, 20, 1.0)
        pack_voltage_v, pack_voltage_quality = wire_decode._measurement(system, 18, 0.1)
        pack_current_a, pack_current_quality = wire_decode._measurement(system, 19, 0.1)
        bms_soc_pct, bms_soc_quality = wire_decode._percentage(bms, 9)
        dynamic_charge_limit_w, charge_quality = wire_decode._power_limit(bms, 13)
        dynamic_discharge_limit_w, discharge_quality = wire_decode._power_limit(bms, 14)

        values: dict[str, Any] = {
            "system_soc_pct": system_soc_pct,
            "bms_soc_pct": bms_soc_pct,
            "soh_pct": soh_pct,
            "battery_watts": battery_watts,
            "pack_voltage_v": pack_voltage_v,
            "pack_current_a": pack_current_a,
            "dynamic_charge_limit_w": dynamic_charge_limit_w,
            "dynamic_discharge_limit_w": dynamic_discharge_limit_w,
            "grid_power_w": grid_power_w,
            "load_power_w": load_power_w,
            # Advisory device-mode words (the production decoder's exact
            # offsets): the simulated pod is Remote/running by construction,
            # and the run-mode word flips to Remote PQ under any writer's
            # objective -- the night-writer detector's discriminator.
            "debug_mode_w": int(debug_mode[0]) & 0xFFFF,
            "ctrl_mode_w": int(system[1]) & 0xFFFF,
            "work_mode_w": int(system[2]) & 0xFFFF,
            "run_mode_w": int(pcs_live[2]) & 0xFFFF,
            # The served PQ objective (PROTOCOL_EVIDENCE 4b), fresh every
            # simulated poll: the pod's applied pair, or the scenario's
            # scripted FOREIGN pair while one holds the wire.
            "served_active_objective_w": protocol_codec.decode_signed16(pcs_detail[17]),
            "served_reactive_objective_var": protocol_codec.decode_signed16(pcs_detail[18]),
            "objective_captured_at_mono": self._pod.telemetry_captured_at_mono,
            **(
                {
                    "energy_grid_a_kwh": energy_grid_a_kwh,
                    "energy_grid_b_kwh": energy_grid_b_kwh,
                    "energy_load_kwh": energy_load_kwh,
                    "energy_pv_kwh": energy_pv_kwh,
                    "energy_charge_kwh": energy_charge_kwh,
                    "energy_discharge_kwh": energy_discharge_kwh,
                }
                if self._decode_energy_totals
                else {}
            ),
            # Cell blocks: millivolt words and raw-40-offset temperature words.
            # The evidenced window serves every cell the packing holds; the
            # unit's commissioned count takes the prefix it declares.  The
            # bank shape itself is verified above, so a served bank is GOOD.
            "cell_voltages_v": tuple(
                value / 1000.0 for value in cells[: self._expected_cell_count]
            ),
            "temperatures_c": tuple(float(value - 40) for value in temperatures),
        }
        quality: dict[str, DataQuality] = {
            "system_soc_pct": system_soc_quality,
            "bms_soc_pct": bms_soc_quality,
            "soh_pct": soh_quality,
            "battery_watts": watts_quality,
            "pack_voltage_v": pack_voltage_quality,
            "pack_current_a": pack_current_quality,
            "dynamic_charge_limit_w": charge_quality,
            "dynamic_discharge_limit_w": discharge_quality,
            "cell_voltages_v": DataQuality.GOOD,
            "temperatures_c": DataQuality.GOOD,
            "grid_power_w": grid_quality,
            "load_power_w": load_quality,
            **(
                {
                    "energy_grid_a_kwh": energy_grid_a_quality,
                    "energy_grid_b_kwh": energy_grid_b_quality,
                    "energy_load_kwh": energy_load_quality,
                    "energy_pv_kwh": energy_pv_quality,
                    "energy_charge_kwh": energy_charge_quality,
                    "energy_discharge_kwh": energy_discharge_quality,
                }
                if self._decode_energy_totals
                else {}
            ),
        }
        # Scripted scenario degradation rides ON TOP of the derived judgment
        # and never touches the served words (MUTATION-3/6): BAD/MISSING also
        # withdraw the value, SUSPECT/STALE keep it.
        for field, flag in dict(self._pod.scripted_quality()).items():
            quality[field] = flag
            if flag in (DataQuality.BAD, DataQuality.MISSING):
                values[field] = () if field in ("cell_voltages_v", "temperatures_c") else None

        return Observation(
            unit_id=self._unit_id,
            device_identity=self._pod.identity,
            connection_epoch=self._pod.connection_epoch,
            wall_timestamp=self._clock.wall_now(),
            captured_at_mono=self._pod.telemetry_captured_at_mono,
            sequence=self._pod.telemetry_sequence,
            lifecycle=lifecycle,
            protocol_profile=self._expected_profile,
            expected_cell_count=self._expected_cell_count,
            cell_captured_at_mono=self._pod.cell_captured_at_mono,
            cell_sequence=self._pod.cell_sequence,
            expected_temperature_count=served_temperatures,
            active_faults=fault_codes,
            active_warnings=warning_codes,
            **values,
            quality=quality,
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
        promote_pcs_live_block: bool = False,
        decode_energy_totals: bool = True,
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
        # API_CONTRACTS "Excess-solar accelerated charging (advisory)" +
        # DESIGN_EXCESS_ACTIVATION P6: with the excess_charging block
        # PRESENT (participating or suspended) the PCS live block (grid at
        # +17, load at +20, PROTOCOL_EVIDENCE 4c) is promoted from the cold
        # ring into the control-rate core so grid_power_w refreshes every
        # telemetry cycle inside export_telemetry_max_age_s — the live
        # per-phase figures stay on the console whether or not the adviser
        # participates.  The plan stays inside the commissioned cadence
        # budget (steady state <= 8 windows plus the probe, bootstrap <= 10).
        self._promote_pcs_live_block = bool(promote_pcs_live_block)
        # DESIGN_ENERGY_SCORECARD section 7: the six cumulative fields decode
        # only when the ``energy_scorecard`` block is PRESENT -- the absent
        # block composes no energy decode, so the served totals words never
        # reach the observation and the quality map stays at twelve keys.
        self._decode_energy_totals = bool(decode_energy_totals)
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
        # B4 (SYNC_RESILIENCE_AUDIT): one-shot deny-triggered promotion of
        # the cell window, set through ``request_cell_refresh()`` by the
        # owning actor after a cell-derived deny, consumed by the next
        # ``read_plan()``.
        self._cell_refresh_requested = False
        # DESIGN_ENERGY_SCORECARD section 5: one-shot day-rollover promotion
        # of the cumulative-energy window (the B4 precedent), set through
        # ``request_energy_refresh()`` by the owning actor.
        self._energy_refresh_requested = False

    def request_cell_refresh(self) -> None:
        """Promote the cell window into the NEXT plan regardless of phase.

        A cell-bound deny may have judged a cached window up to ~3 s old
        while the live battery already recovered; the promoted poll
        re-evaluates the deny on fresh cells (and an honest fresh capture
        clock advances with it).  Exactly one promoted cycle.
        """
        self._cell_refresh_requested = True

    def request_energy_refresh(self) -> None:
        """Promote the cumulative-energy window into the NEXT plan.

        The energy block rides the cold ring (~108 s period); at a day roll
        the accountant asks for a fresh counter baseline through the fleet
        loop, and exactly one promoted cycle makes the new day's baseline at
        most one control period old instead of one ring period.
        """
        self._energy_refresh_requested = True

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

        Every cycle: the BMS block, the three IoT fault blocks and the cell
        temperatures (everything the safety kernel consumes at the control
        rate), the two-word identity pair (a physically swapped unit must
        latch on the very next poll), and the one-word debug-mode readback
        (the vendor's dispatch precondition, judged at the control rate);
        with the excess-solar feature enabled the PCS live block joins them
        so the advisory grid word rides the control rate.  Every third cycle:
        the cell-voltage window (the domain already models cells on their own
        slower capture clock; the policy's cell-age bound covers the tier).
        Every eighth cycle: ONE cold-ring window (system overview, PCS/DCDC
        live and detail, parameters, balance, network, energy) rotating
        through the remaining blocks so the unit-detail surface stays
        populated without threatening the renewal cadence.

        SYNC_RESILIENCE_AUDIT B5 (2026-08-24): the system overview block
        (0x0100: ctrlMode +1, workMode +2, the ADVISORY system SOC +17) is
        NOT a once-per-process read any more.  The old cycle-1-only tier is
        exactly the shape that froze mid's system SOC at 67 % for 112
        sequences (the 2026-08-24 soc incident) and would pin a boot-time
        Local mode word for the process lifetime; it now rotates with the
        cold ring (~108 s period), the ring serves one window per 8th cycle
        either way, and the B5 dispatch-refusal refresh plus B1's advisory
        demotion cover the words the ring cannot serve promptly.
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
            # The one-word debug-mode readback rides the core for the same
            # reason: the vendor's dispatch precondition must be judged at
            # the control rate (2026-08-23 incident 1), and the steady-state
            # plan stays inside the commissioned window budget (7 + probe).
            _DEBUG_MODE_BLOCK_BASE,
        }
        if self._promote_pcs_live_block:
            core_bases.add(_PCS_LIVE_BLOCK_BASE)
        plan = [(base, by_base[base]) for base in sorted(core_bases) if base in by_base]
        cell_due = _CELL_VOLTAGE_BASE in by_base and self._cycle % 3 == 1
        if self._cell_refresh_requested:
            # B4: a cell-derived deny promoted this cycle; the hint is
            # consumed here so the promotion lasts exactly one cycle (a
            # persisting fresh violation re-denies and the loop re-sets it).
            self._cell_refresh_requested = False
            cell_due = _CELL_VOLTAGE_BASE in by_base
        if cell_due:
            plan.append((_CELL_VOLTAGE_BASE, by_base[_CELL_VOLTAGE_BASE]))
        if self._energy_refresh_requested and _ENERGY_TOTALS_BLOCK_BASE in by_base:
            # DESIGN_ENERGY_SCORECARD section 5: the day-roll promotion --
            # exactly one cycle includes the cumulative-energy window early;
            # the cold ring's own rotation resumes afterwards.
            self._energy_refresh_requested = False
            plan.append((_ENERGY_TOTALS_BLOCK_BASE, by_base[_ENERGY_TOTALS_BLOCK_BASE]))
        cold = sorted(base for base in by_base if base not in core_bases | {_CELL_VOLTAGE_BASE})
        if cold and self._cycle % 8 == 0:
            # The rotation starts at the system overview block (0x0100 sorts
            # first): the advisory system SOC and the display-mode words get
            # the FIRST cold-ring refresh (cycle 8, ~12 s at the 1.5 s
            # cadence) instead of a former last-place slot, and the ring's
            # one-window-per-8th-cycle shape keeps the per-cycle budget.
            chosen = cold[((self._cycle // 8) - 1) % len(cold)]
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
        # The served PQ objective rides the cold ring (the night-writer
        # detector's window): between rotations the merged decode carries the
        # CACHED words with their ORIGINAL capture clock (the cell-meta
        # pattern), so a consumer can tell a fresh serving from a stale one.
        objective_captured: float | None = None
        if _PCS_DETAIL_BLOCK_BASE in self._slow_cache:
            _detail_words, detail_at = self._slow_cache[_PCS_DETAIL_BLOCK_BASE]
            objective_captured = detail_at
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
            decode_energy_totals=self._decode_energy_totals,
            objective_captured_at_mono=objective_captured,
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

    @property
    def inhibit_cause(self) -> Any:
        """The actor's recorded inhibit cause; ``None`` while never latched."""
        return self._actor.inhibit_cause

    @property
    def inhibit_reason(self) -> str | None:
        """The actor's operator-visible inhibit reason word.

        Arm-refusal de-conflation: the facade surfaces this beside the pinned
        ``inhibit_latched`` refusal so the console can name WHICH condition to
        clear (``external_writer`` vs ``identity_mismatch`` vs
        ``blocking_fault_active``) instead of a bare latch.
        """
        return self._actor.inhibit_reason

    @property
    def last_arm_classification(self) -> str | None:
        """ADD-1: the last arm preflight's objective classification."""
        return self._actor.last_arm_classification

    async def arm(self, *, takeover_acknowledged: bool = False) -> None:
        await self._actor.arm(takeover_acknowledged=takeover_acknowledged)

    async def disarm(self) -> None:
        await self._actor.disarm()

    async def acknowledge_inhibit(self) -> None:
        await self._actor.acknowledge_inhibit()

    async def request_bounded_zero(self, reason: str) -> None:
        await self._actor.request_bounded_zero(reason)

    async def refresh_mode_words(self) -> tuple[int, int]:
        """B5: the actor-owned bounded fresh mode-word read."""
        return await self._actor.refresh_mode_words()

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


@dataclass(slots=True)
class _AdvisoryPrincipal:
    """The composed excess-charge automation principal (API_CONTRACTS).

    ``energypod:excess-adviser``: observe + dispatch only, non-interactive,
    site-bound.  It exists so the adviser's intents are attributable in the
    audit trail distinct from every human console and agent writer; it holds
    no scope the advisory path does not need and no interactive capability
    at all.
    """

    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


@dataclass(slots=True)
class _ScheduleRunnerPrincipal:
    """The composed schedule automation principal (DESIGN_SCHEDULES §2).

    ``energypod:schedule-runner``: observe + dispatch only, non-interactive,
    site-bound — the adviser principal's exact shape, so the runner's
    ``intent_accepted`` rows are attributable distinct from every console,
    agent, and adviser writer.
    """

    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


@dataclass(slots=True)
class _NightAdviserPrincipal:
    """The composed night-strategy automation principal (DESIGN_NIGHT_CHARGE §2.1).

    ``energypod:night-adviser``: observe + dispatch only, non-interactive,
    site-bound — the adviser principal's exact shape, so the night
    strategy's ``intent_accepted`` rows are attributable distinct from every
    console, agent, schedule-runner, and excess-adviser writer.
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
        audit: _AsyncAuditRepository,
        process_instance_id: str,
        process_origin_mono: float,
        adviser: ExcessChargeAdviser | None = None,
        excess_controller: ExcessAdviserController | None = None,
        night_adviser: NightChargeAdviser | None = None,
        night_controller: NightChargeController | None = None,
        intents: _AsyncIntentRepository | None = None,
        observations: _AsyncObservationRepository | None = None,
        recovery: RecoveryMonitor | None = None,
        foreign_objective: ForeignObjectiveMonitor | None = None,
        schedule_runner: ScheduleRunner | None = None,
        energy_accountant: EnergyAccountant | None = None,
        historian: TelemetryHistorian | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._clock = clock
        self._interval_s = interval_s
        self._kernel = kernel
        self._actors = actors
        self._authorizations = authorizations
        self._coordinator = coordinator
        self._audit = audit
        self._process_instance_id = process_instance_id
        self._process_origin_mono = process_origin_mono
        self._adviser = adviser
        # DESIGN_EXCESS_ACTIVATION §2: the projection controller driven
        # post-tick by this loop (its single writer).
        self._excess_controller = excess_controller
        # DESIGN_NIGHT_CHARGE §2.5/§5: the night strategy adviser, ticked
        # once per fleet cycle AFTER the excess adviser and BEFORE the
        # energy accountant (pinned below), plus its projection controller
        # driven by the same suppressed step.
        self._night_adviser = night_adviser
        self._night_controller = night_controller
        # DESIGN_SCHEDULES §2: the schedule runner, ticked once per fleet
        # cycle AFTER the polls and BEFORE the adviser step (pinned below).
        self._schedule_runner = schedule_runner
        # DESIGN_ENERGY_SCORECARD section 7: the daily energy accountant,
        # ticked once per fleet cycle AFTER the polls BESIDE the adviser
        # projection update (it consumes the fresh observations and reads --
        # never writes -- the adviser state for the attribution predicate).
        self._energy_accountant = energy_accountant
        # DESIGN_PLANT_HISTORY section 2.1: the telemetry historian, ticked
        # once per fleet cycle AFTER the polls and the energy-accountant step
        # and BEFORE the kernel tick (the pinned ordering -- observability
        # can never delay renewal or control).
        self._historian = historian
        # Self-healing awareness layer (R4): the passive detection monitor
        # driven once per fleet cycle, plus the two ports it reads through.
        self._intents_port = intents
        self._observations_port = observations
        self._recovery = recovery
        # Night-writer detector (API_CONTRACTS "Night-writer detector"): the
        # passive foreign-objective watch, driven by the same bounded pass
        # shape one step after the recovery pass.
        self._foreign_objective = foreign_objective
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
            # Self-healing awareness (R4): peek the authority each heartbeat
            # is about to consume BEFORE it is consumed, so the coherence
            # watchdog judges exactly the watts the fleet is holding.
            authorized = await self._peek_authorizations()
            for actor in self._actors:
                failure: BaseException | None = None
                try:
                    # Structural bound (2026-08-23 silent-wedge hardening):
                    # a transport write that never resolves must not wedge
                    # the whole fleet loop with a clean log. An abandoned
                    # renewal is fail-closed exactly like an unreadable
                    # poll — authority lapses and the device watchdog
                    # stops power.
                    await asyncio.wait_for(actor.heartbeat_once(), timeout=self._interval_s)
                except asyncio.CancelledError:
                    # A facade fence (emergency stop) cancels in-flight
                    # authority work; that borrowed cancellation must not end
                    # the fleet cycle. Genuine shutdown of this task carries a
                    # real cancellation request, which wins.
                    task = asyncio.current_task()
                    if task is None or task.cancelling():
                        raise
                except Exception as error:
                    # Survived per cycle, but never invisible (2026-08-23
                    # incident class): a suppressed heartbeat failure is
                    # total actuation loss for this unit until the next
                    # renewal, so it is audited and logged, every cycle.
                    failure = error
                if failure is not None:
                    await self._record_suppressed_heartbeat(actor, failure)
            # API_CONTRACTS "Unit actor": an overdue read is abandoned
            # rather than delaying a heartbeat past its safety margin. In the
            # fleet cycle the bound is structural — a poll that overruns the
            # interval is cancelled (its observation is only appended at the
            # end of the poll, so abandoned reads never become evidence) and
            # the cycle proceeds to the tick, keeping renewal cadence.  Each
            # poll's outcome feeds the recovery classifier's responsiveness
            # streaks (connect failure = gateway class, read failure = the
            # pod-silent wedge class).
            outcomes = await asyncio.gather(*(self._bounded_poll(actor) for actor in self._actors))
            # The detection pass is bounded and fully suppressed: observability
            # must never delay the renewal cadence or the kernel tick.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._observe_recovery(authorized, tuple(outcomes)),
                    timeout=self._interval_s,
                )
            # Night-writer detector: one bounded, fully suppressed observation
            # pass after the polls and the recovery pass (API_CONTRACTS
            # "Supervision driving") -- zero extra frames, a failed sample a
            # gap, never an alarm.
            if self._foreign_objective is not None:
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._observe_foreign_objectives(authorized, tuple(outcomes)),
                        timeout=self._interval_s,
                    )
            if self._schedule_runner is not None:
                # DESIGN_SCHEDULES §2 (ordering pinned): one bounded schedule
                # tick per fleet cycle, AFTER the polls and recovery pass and
                # BEFORE the adviser step and the kernel tick — the schedule's
                # claim is a published fact and the adviser is the
                # opportunist, so evaluating the schedule first means the
                # adviser's same-tick yield check already sees the schedule's
                # intent and a yield resolves within one cycle with no
                # double-claim noise.  A runner failure is survivable per
                # cycle exactly like an advisory failure; the held intent's
                # TTL lapse plus the firmware watchdog are the designed
                # hand-back, and the runner never halts the fleet.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._schedule_runner.tick(), timeout=self._interval_s)
            if self._adviser is not None:
                # API_CONTRACTS "Excess-solar accelerated charging
                # (advisory)": one bounded advisory renewal per fleet cycle,
                # after the polls (so it reasons over fresh evidence) and
                # before the kernel tick (so a renewed intent is arbitrated
                # this same cycle).  An advisory failure is survivable per
                # cycle — the intent TTL lapse plus the firmware watchdog
                # are the designed hand-back — and never halts the fleet.
                # CancelledError is a BaseException and is never swallowed.
                # DESIGN_EXCESS_ACTIVATION §2: the same suppressed step is
                # the projection's single-writer update + event publication
                # (observe_tick), so observability can never gate control.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._adviser_step(), timeout=self._interval_s)
            if self._night_adviser is not None:
                # DESIGN_NIGHT_CHARGE §2.5 (ordering pinned): one bounded,
                # suppressed night tick per fleet cycle, AFTER the excess
                # adviser and BEFORE the energy accountant and kernel tick —
                # published facts first (the schedule), then the opportunists
                # in economics order (FREE surplus before PAID import), so
                # the excess adviser's same-cycle claim is already in the
                # active set when the night adviser looks and the dawn-corner
                # exclusion resolves deterministically within one cycle.  A
                # failure is survivable per cycle exactly like an advisory
                # failure; CancelledError is never swallowed; the TTL lapse
                # plus the firmware watchdog are the designed hand-back.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._night_step(), timeout=self._interval_s)
            if self._energy_accountant is not None:
                # DESIGN_ENERGY_SCORECARD section 7: one bounded accounting
                # tick per fleet cycle, beside the adviser-projection update
                # and before the kernel tick -- it consumes observations and
                # never blocks (a failure is survivable per cycle exactly
                # like an advisory failure; the durable ledger retries).
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._energy_step(), timeout=self._interval_s)
            if self._historian is not None:
                # DESIGN_PLANT_HISTORY section 2.1: one bounded, fully
                # suppressed historian tick per fleet cycle, AFTER the polls
                # and the energy-accountant step and BEFORE the kernel tick.
                # A failure anywhere inside it is a GAP, never a delay to
                # control; the historian receives this cycle's peeked
                # authority (the same dict the heartbeats consumed).
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(self._history_step(authorized), timeout=self._interval_s)
            # A kernel tick that overruns the interval is a component failure,
            # not a survivable per-unit fault. Cancelling it is safe — the
            # kernel's BaseException path revokes authority first (shielded)
            # and the durable audit write is transactional — and letting the
            # TimeoutError end this task makes the watcher halt the fleet with
            # evidence instead of the loop wedging silently forever.
            decision = await asyncio.wait_for(self._kernel.tick(), timeout=self._interval_s)
            if decision_requests_cell_refresh(decision):
                # B4 (SYNC_RESILIENCE_AUDIT): the decision carried a
                # cell-derived deny, so each unit's NEXT telemetry cycle
                # includes the cell window regardless of the tier phase --
                # the deny is then re-judged on fresh battery data (a
                # recovered window authorizes; a persisting violation
                # re-denies and re-promotes).  Reason codes are per-decision,
                # not per-unit, so every actor is flagged; the promoted plan
                # stays inside the commissioned cadence budget (8 -> 9
                # windows ~1.0 s at the 0.1 s inter-frame gap, inside the
                # 1.5 s control period / 1.60 s renewal budget).
                for actor in self._actors:
                    actor.request_cell_refresh()

    async def _adviser_step(self) -> None:
        """One advisory tick plus the projection's single-writer update."""
        assert self._adviser is not None
        decision = await self._adviser.tick()
        if self._excess_controller is not None:
            await self._excess_controller.observe_tick(decision)

    async def _night_step(self) -> None:
        """One night tick plus the projection's single-writer update."""
        assert self._night_adviser is not None
        decision = await self._night_adviser.tick()
        if self._night_controller is not None:
            await self._night_controller.observe_tick(decision)

    async def _history_step(self, authorized: Mapping[str, Any]) -> None:
        """One suppressed historian tick with the cycle's peeked authority."""
        historian = self._historian
        assert historian is not None
        await historian.tick(authorized=authorized)

    async def _energy_step(self) -> None:
        """One accounting tick over the fleet's latest observations.

        The attribution predicate reads the adviser projection's OWN frozen
        state (active + target) -- the accountant never writes it.
        """
        accountant = self._energy_accountant
        observations_port = self._observations_port
        assert accountant is not None and observations_port is not None
        targets: frozenset[str] = frozenset()
        if self._excess_controller is not None:
            state = self._excess_controller.state()
            if state.active and state.target_unit_id is not None:
                targets = frozenset({state.target_unit_id})
        latest = await observations_port.all_latest()
        await accountant.tick(latest, adviser_active_targets=targets)

    async def _bounded_poll(self, actor: EnergyPodActor) -> str:
        """One bounded, survived poll; returns the cycle's bus-read outcome.

        The failure classification is the recovery layer's R4 discriminator:
        a ``TransportConnectionError`` is the gateway/TCP class (unreachable),
        anything else that escapes the actor's poll is a failed read attempt
        (the pod-silent-on-the-bus wedge class).  Both stay survived per
        cycle exactly as before — only the outcome is now recorded.
        """
        try:
            await asyncio.wait_for(actor.poll_once(), timeout=self._interval_s)
        except asyncio.CancelledError:
            raise
        except TransportConnectionError:
            outcome = CONNECT_FAILED
        except Exception:
            outcome = READ_FAILED
        else:
            outcome = READ_OK
        if self._recovery is not None:
            with contextlib.suppress(Exception):
                self._recovery.record_read_outcome(actor.unit_id, outcome)
        return outcome

    async def _peek_authorizations(self) -> dict[str, tuple[int, str | None] | None]:
        """The authority each unit's heartbeat is about to consume (or None).

        ``peek`` is the non-consuming projection read; an unreadable store is
        the honest unknown and judges nothing that cycle.
        """
        authorized: dict[str, tuple[int, str | None] | None] = {}
        for actor in self._actors:
            try:
                capability = await self._authorizations.peek(actor.unit_id)
            except Exception:
                capability = None
            if capability is None:
                authorized[actor.unit_id] = None
                continue
            watts = getattr(capability, "watts", None)
            direction = getattr(getattr(capability, "direction", None), "value", None)
            authorized[actor.unit_id] = (
                int(watts) if isinstance(watts, int) else 0,
                direction if isinstance(direction, str) else None,
            )
        return authorized

    async def _observe_recovery(
        self,
        authorized: Mapping[str, tuple[int, str | None] | None],
        outcomes: Sequence[str],
    ) -> None:
        """One detection pass: fresh facts in, derived health state out.

        Runs after the polls (fresh measurements) and before the kernel tick,
        mirroring the fleet cycle the actor/kernel contracts pin.  Every step
        is suppressed per unit: a failing detection path never survives into
        control.  On a coherence trigger the objective echo read-back runs
        through the OWNING actor (P1 iii — one bounded read per episode).
        """
        if self._recovery is None or self._intents_port is None or self._observations_port is None:
            return
        now_mono = float(self._clock.monotonic())
        claimed: frozenset[str] | None
        try:
            active = await self._intents_port.active(now_mono)
            claimed = frozenset(
                unit_id
                for intent in active
                for unit_id in (getattr(intent, "selected_unit_ids", ()) or ())
            )
        except Exception:
            # Unknown claim state fails safe for evidence: treat every unit
            # as claimed so nothing is recorded as "uncommanded".
            claimed = None
        for index, actor in enumerate(self._actors):
            unit_id = actor.unit_id
            held = authorized.get(unit_id)
            # Only a cycle whose poll landed contributes fresh evidence; a
            # failed poll leaves the previous observation in place and judges
            # nothing (the responsiveness streaks already carry the failure).
            observation = None
            if outcomes[index] == READ_OK:
                with contextlib.suppress(Exception):
                    observation = await self._observations_port.latest(unit_id)
            is_claimed = True if claimed is None else unit_id in claimed
            with contextlib.suppress(Exception):
                findings = await self._recovery.observe_cycle(
                    unit_id,
                    authorized_watts=0 if held is None else held[0],
                    authorized_direction=None if held is None else held[1],
                    claimed=is_claimed,
                    lifecycle=actor.lifecycle,
                    inhibit_latched=bool(actor.inhibit_latched),
                    inhibit_reason=actor.inhibit_reason,
                    observation=observation,
                    now_mono=now_mono,
                )
                if findings is not None and findings.coherence_trigger:
                    classification, served_active, served_reactive = await self._objective_echo(
                        actor
                    )
                    await self._recovery.record_incoherence_echo(
                        unit_id,
                        classification=classification,
                        served_active_w=served_active,
                        served_reactive_var=served_reactive,
                    )

    async def _objective_echo(self, actor: EnergyPodActor) -> tuple[str, int | None, int | None]:
        """One bounded objective echo read through the owning actor (P1 iii).

        The read rides the actor's serialized mailbox under the cycle bound;
        a failing or unwired read is honestly classified ``echo_unreadable``
        instead of guessed at.
        """
        try:
            async with asyncio.timeout(self._interval_s):
                classification, served = await actor.read_objective_echo()
            return classification, int(served[0]), int(served[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            return ECHO_UNREADABLE, None, None

    async def _observe_foreign_objectives(
        self,
        authorized: Mapping[str, tuple[int, str | None] | None],
        outcomes: Sequence[str],
    ) -> None:
        """The night-writer detector's one pass: fresh words in, evidence out.

        Exactly the recovery pass's shape (per-unit, fully suppressed, driven
        by the loop alone): a unit whose poll failed contributes no
        observation, and the claim set comes from the same intents port --
        a read failure treats every unit as claimed so nothing is recorded
        as uncommanded (fail-safe for evidence).
        """
        monitor = self._foreign_objective
        if monitor is None or self._intents_port is None or self._observations_port is None:
            return
        now_mono = float(self._clock.monotonic())
        try:
            active = await self._intents_port.active(now_mono)
            claimed = frozenset(
                unit_id
                for intent in active
                for unit_id in (getattr(intent, "selected_unit_ids", ()) or ())
            )
        except Exception:
            claimed = None
        for index, actor in enumerate(self._actors):
            unit_id = actor.unit_id
            held = authorized.get(unit_id)
            observation = None
            if outcomes[index] == READ_OK:
                with contextlib.suppress(Exception):
                    observation = await self._observations_port.latest(unit_id)
            is_claimed = True if claimed is None else unit_id in claimed
            with contextlib.suppress(Exception):
                await monitor.observe_cycle(
                    unit_id,
                    lifecycle=actor.lifecycle,
                    claimed=is_claimed,
                    authorized_watts=0 if held is None else held[0],
                    observation=observation,
                    now_mono=now_mono,
                )

    async def _record_suppressed_heartbeat(
        self, actor: EnergyPodActor, error: BaseException
    ) -> None:
        """Audit and log one suppressed heartbeat failure (never fatal).

        The fleet loop survives the failure per cycle, but silence here once
        hid total actuation loss: the durable heartbeat_failed row plus the
        process log line make every suppressed renewal visible to both the
        console stream and the operator reading the log.  Observability must
        never break the loop, so a failing record is itself suppressed.
        """
        print(f"SUPERVISED HEARTBEAT FAILURE ({actor.unit_id}): {error!r}", flush=True)
        try:
            await self._audit.append(
                AuditEvent(
                    event_id=f"heartbeat-failed-{uuid.uuid4().hex}",
                    occurred_at=self._clock.wall_now().astimezone(UTC),
                    monotonic_offset_s=float(self._clock.monotonic()) - self._process_origin_mono,
                    process_instance_id=self._process_instance_id,
                    event_type="heartbeat_failed",
                    unit_id=actor.unit_id,
                    connection_epoch=None,
                    generation=None,
                    cycle_id=None,
                    principal=_RUNTIME_PRINCIPAL,
                    source=None,
                    correlation_id="fleet-heartbeat",
                    intent_id=None,
                    policy_version=_COMPOSITION_POLICY_VERSION,
                    configuration_version=0,
                    observation_sequences={},
                    reason_codes=("suppressed_exception",),
                    requested_active_w=0,
                    authorized_active_w=0,
                    request_fingerprint=_fingerprint(
                        {"unit_id": actor.unit_id, "error": type(error).__name__}
                    ),
                    response_fingerprint=_fingerprint({"result": "suppressed"}),
                    result="suppressed",
                    lifecycle=getattr(actor, "lifecycle", UnitLifecycle.INHIBITED),
                )
            )
        except Exception:
            return

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


def _recovery_settings(config: ControllerConfig) -> RecoverySettings:
    """The detection layer's commissioned knobs (policy keys when a policy is
    configured; the pinned defaults otherwise, so observe-only deployments
    detect with the same eyes).

    Detection only: no control path consumes these values (the safety kernel
    and the actors never see them).
    """
    configured = config.policy
    if configured is None:
        return RecoverySettings()
    band_low, band_high = configured.expected_autonomy_band_w
    return RecoverySettings(
        actuation_coherence_cycles=configured.actuation_coherence_cycles,
        actuation_coherence_min_movement_w=configured.actuation_coherence_min_movement_w,
        expected_autonomy_band_w=(band_low, band_high),
    )


def _foreign_objective_settings(config: ControllerConfig) -> ForeignObjectiveSettings:
    """The night-writer detector's knobs, the recovery-settings pattern: the
    policy keys when a policy is configured, the pinned defaults (including
    the STRICT no-expected-writer posture) otherwise.  The autonomy band is
    the same commissioned envelope the recovery layer consumes.

    Detection only: no control path consumes these values.
    """
    configured = config.policy
    if configured is None:
        return ForeignObjectiveSettings()
    band_low, band_high = configured.expected_autonomy_band_w
    return ForeignObjectiveSettings(
        sample_interval_s=float(configured.foreign_objective_sample_interval_s),
        sustained_samples=configured.foreign_objective_sustained_samples,
        self_charge_class_w=configured.foreign_objective_self_charge_class_w,
        handback_grace_s=float(configured.foreign_objective_handback_grace_s),
        expected_charge_w=configured.foreign_objective_expected_charge_w,
        expected_min_units=configured.foreign_objective_expected_min_units,
        expected_autonomy_band_w=(band_low, band_high),
    )


def _control_policy(config: ControllerConfig) -> ControlPolicy:
    """Derive the strict domain policy; heartbeat follows ``control_period_s``.

    ``heartbeat_interval_s`` is not configurable (API_CONTRACTS): it is derived
    from ``timing.control_period_s`` so the kernel cadence, actor heartbeats,
    and the commissioned timing budget can never disagree.
    """
    heartbeat_interval_s = float(config.timing.control_period_s)
    cell_counts = {unit.unit_id: unit.expected_cell_count for unit in config.units}
    excess = config.excess_charging
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)" +
    # DESIGN_EXCESS_ACTIVATION P6: a PRESENT block arms the all-or-none
    # export triple (the allocator's optimizer-charge bound gets its cap,
    # margin, and freshness bound) — `enabled` gates PARTICIPATION, not
    # composition; an absent block arms nothing and the bound is 0.
    export_limit_w: int | None = None
    export_margin_w: int | None = None
    export_age_s: float | None = None
    if excess is not None:
        export_limit_w = excess.max_charge_from_export_w
        export_margin_w = excess.export_headroom_margin_w
        export_age_s = excess.export_telemetry_max_age_s
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
            # Operator-directed 2026-08-23 (a7297bf, live config rev 4): the
            # 0.050 V commissioning gate is demoted to an early-warning tier,
            # with the ABSOLUTE per-cell bounds above left as the hard
            # over/under-charge protection.  The observe-only default matches
            # the commissioned 0.500 V so simulated and observe-only
            # compositions judge spread like the deployed policy instead of
            # vetoing on the real top-of-charge balancing drift (LHS 54 mV at
            # 99% SOC) the operator relaxed the gate for.
            max_cell_imbalance_v=0.500,
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
        # ADD-1: the commissioned pod-autonomy band reaches the policy (and
        # from there the actors' arm preflight) exactly when configured.
        autonomous_charge_signature_max_w=configured.autonomous_charge_signature_max_w,
        blocking_fault_codes=frozenset(configured.blocking_fault_codes),
        # S1 (SYNC_RESILIENCE_AUDIT): the configured warning-tier blocking
        # set reaches the policy instead of a hard-wired empty set -- the
        # kernel's ``blocking_warning`` deny can now actually fire when a
        # commissioning chooses warning bits to block on.
        blocking_warning_codes=frozenset(configured.blocking_warning_codes),
        export_charge_limit_w=export_limit_w,
        export_headroom_margin_w=export_margin_w,
        export_telemetry_max_age_s=export_age_s,
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
    # API_CONTRACTS "Self-healing awareness layer": the composed recovery
    # monitor (detection only — audit facts, bus events, and the derived
    # per-unit health view the facade projects).
    recovery: RecoveryMonitor | None = None
    # API_CONTRACTS "Night-writer detector": the composed foreign-objective
    # monitor (detection only — always composed, evidence records, the alert
    # tier's one audit fact and bus event, and the window characterization
    # the facade serves through GET /api/v1/objectives/observed).
    foreign_objective: ForeignObjectiveMonitor | None = None
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": composed
    # only when the configuration enables the feature; None otherwise.
    # DESIGN_NIGHT_CHARGE §2/§5: composed only when the ``night_charging``
    # block is PRESENT — the night strategy adviser the fleet loop ticks
    # (after the excess adviser, before the accountant) and its projection
    # controller (participation, the PARTITION acknowledgement latch, the
    # ``night_charge_state`` view, the state_changed publication).  None
    # otherwise (block-absent doctrine).
    night_adviser: NightChargeAdviser | None = None
    night_controller: NightChargeController | None = None
    excess_adviser: ExcessChargeAdviser | None = None
    # DESIGN_EXCESS_ACTIVATION §1/§2: the projection controller (the
    # participation flag, the acknowledgement latch, the frozen state view,
    # and the state_changed publication the fleet loop drives).
    excess_controller: ExcessAdviserController | None = None
    # DESIGN_SCHEDULES §2/§5: composed only when the ``schedule`` block is
    # PRESENT — the surface control the facade projects through (policy,
    # plan store, night-acknowledgement latch, projection read) and the
    # runner the fleet loop ticks.  None otherwise (block-absent doctrine).
    schedule_surface: ScheduleSurfaceControl | None = None
    schedule_runner: ScheduleRunner | None = None
    # DESIGN_ENERGY_SCORECARD sections 5-7: composed only when the
    # ``energy_scorecard`` block is PRESENT -- the accountant the fleet loop
    # ticks (one bounded step beside the adviser projection update), the
    # durable ledger it records days and the live-day baseline through, and
    # the facade-facing surface (energy_today + the days route body).  None
    # otherwise (block-absent doctrine).
    energy_accountant: EnergyAccountant | None = None
    energy_ledger: SQLiteEnergyLedgerRepository | InMemoryEnergyLedgerRepository | None = None
    energy_surface: EnergyScorecardControl | None = None
    # DESIGN_PLANT_HISTORY sections 2.1+2.5: composed only when the
    # ``plant_history`` block is PRESENT -- the historian the fleet loop
    # ticks (post-accountant, pre-kernel, bounded, suppressed), the durable
    # (or, in simulate mode, explicitly non-durable in-memory) repository it
    # records through, and the facade-facing surface (``history_state`` +
    # the query route).  None otherwise (block-absent doctrine).
    historian: TelemetryHistorian | None = None
    history_repository: (
        SQLiteTelemetryHistoryRepository | InMemoryTelemetryHistoryRepository | None
    ) = None
    history_surface: PlantHistoryControl | None = None
    # ARCHITECTURE section 17 (the reserved advisory layer): composed only
    # when the ``forecast_providers`` block is PRESENT and ENABLED -- the
    # staged posture ships disabled.  ADVISORY-ONLY: no fleet-loop slot, no
    # facade surface, no snapshot key reads this handle; constructing the
    # adapters opens no socket, and the first forecast-consuming adviser
    # wires its own fetch loop against it.  ``notes`` records every declared
    # provider the environment could not deliver (a missing Solcast key).
    forecast_providers: ForecastProviderRegistry | None = None


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
    energy_store: SQLiteEnergyLedgerRepository | InMemoryEnergyLedgerRepository
    history_config = config.plant_history
    history_present = history_config is not None
    history_store: SQLiteTelemetryHistoryRepository | InMemoryTelemetryHistoryRepository | None
    if history_present:
        assert history_config is not None
        # DESIGN_PLANT_HISTORY section 2.5: storage rides the EXISTING
        # database path; simulate mode composes the in-memory adapter
        # instead (explicitly non-durable, for scenario tests).
        retention_rollup_s = (
            None
            if history_config.retention_rollup_days == 0
            else float(history_config.retention_rollup_days) * 86_400.0
        )
        if database is not None and not simulate:
            history_store = SQLiteTelemetryHistoryRepository(
                database,
                retention_full_resolution_s=(
                    float(history_config.retention_full_resolution_days) * 86_400.0
                ),
                retention_rollup_s=retention_rollup_s,
            )
        else:
            history_store = InMemoryTelemetryHistoryRepository(
                retention_full_resolution_s=(
                    float(history_config.retention_full_resolution_days) * 86_400.0
                ),
                retention_rollup_s=retention_rollup_s,
            )
        # DESIGN_PLANT_HISTORY section 2.4: the boot maintenance pass, once,
        # after the store opens (rollup then prune, one transaction,
        # suppressed -- a failure leaves the rows for the midnight pass).
        with contextlib.suppress(Exception):
            history_store.maintain(resolved_clock.wall_now().astimezone(UTC))
    else:
        history_store = None
    if database is not None:
        # The audit read path projects the durable row sequence so the facade
        # cursor pages the same way in every deployment mode.
        audit_store = _SequencedSQLiteAuditRepository(database)
        schedule_store = SQLiteScheduleRepository(database)
        energy_store = SQLiteEnergyLedgerRepository(database)
    else:
        audit_store = _InMemoryAuditRepository(max_events=_MAX_IN_MEMORY_AUDIT_EVENTS)
        schedule_store = _InMemoryScheduleRepository()
        energy_store = InMemoryEnergyLedgerRepository()
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
    intent_port = _AsyncIntentRepository(intent_store, arbiter=arbiter, bus=bus)
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
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)" +
    # DESIGN_EXCESS_ACTIVATION P6: a PRESENT block arms the policy's export
    # triple, promotes the PCS live block into the control-rate read plan,
    # and composes the adviser below (suspended at boot unless enabled AND
    # acknowledged); an absent block changes nothing anywhere (the default).
    excess_config = config.excess_charging
    excess_present = excess_config is not None
    # DESIGN_NIGHT_CHARGE §3/§3.3: a PRESENT night block composes the night
    # adviser surface below (suspended at boot unless enabled AND
    # acknowledged — the acknowledgement boot-loads from the durable store);
    # an absent block changes nothing anywhere.  The PCS live-block promotion
    # widens to EITHER block: the demand rule reads `load_power_w` (0x1000+20)
    # at control rate, and on the cold ring that word serves only every
    # ~96-108 s — `demand_telemetry_max_age_s` would then classify every read
    # stale and the feature would HOLD forever (the same budgeted plan, one
    # widened predicate, no new tier).
    night_config = config.night_charging
    night_present = night_config is not None
    # DESIGN_ENERGY_SCORECARD sections 5+7 (E4): a PRESENT block composes the
    # accountant, the energy decode, the snapshot key, and the route; an
    # ABSENT block composes nothing at all.  The A-1 boot gate is keyed
    # existence, exactly like the schedule's night acknowledgement: a PINNED
    # grid-counter-roles value boots only when the operator's durable
    # pinning fact is in the store.
    energy_config = config.energy_scorecard
    energy_present = energy_config is not None
    if (
        energy_config is not None
        and energy_config.grid_counter_roles != "unpinned"
        and not audit_store.contains_event(ENERGY_ROLES_PINNED_EVENT_ID)
    ):
        raise ValueError(
            "energy_scorecard.grid_counter_roles is "
            f"{energy_config.grid_counter_roles!r} but the durable pinning fact "
            f"{ENERGY_ROLES_PINNED_EVENT_ID!r} is absent from the audit store: "
            "record the operator's P-A1 pinning evidence before pinning the roles"
        )
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
                decode_energy_totals=energy_present,
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
                promote_pcs_live_block=excess_present or night_present,
                decode_energy_totals=energy_present,
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
            # ADD-1 (2026-08-24 live blocker): the commissioned pod-autonomy
            # band for the arm preflight's discrimination layer -- wired only
            # where the preflight itself is wired (write-enabled live units).
            autonomous_charge_signature_max_w=(
                policy.autonomous_charge_signature_max_w if write_enabled and not simulate else None
            ),
            blocking_fault_codes=frozenset(policy.blocking_fault_codes),
            # B5: the bounded fresh mode-word read behind the dispatch
            # refusal path -- wired wherever a register bank actually serves
            # the system overview (every live and simulated unit does).
            mode_refresh_window=(_SYSTEM_BLOCK_BASE, 3),
            telemetry=telemetry,
        )

    # --- application facade, guarded API, and MCP surface -------------------
    # The self-healing awareness monitor (R4): composed for every deployment,
    # driven by supervision once per fleet cycle, projecting the derived
    # per-unit health view through the facade.  Passive by construction.
    recovery_monitor = RecoveryMonitor(
        unit_ids=unit_ids,
        settings=_recovery_settings(config),
        clock=resolved_clock,
        audit=audit_port,
        bus=bus,
        process_instance_id=process_instance_id,
        process_origin_mono=process_origin_mono,
        configuration_version=config.revision,
    )
    # --- night-writer detector (API_CONTRACTS "Night-writer detector") ---
    # Composed ALWAYS -- observe-only, write-enabled, and simulate alike: it
    # is read-only evidence machinery over words the read plan already
    # serves, and no config block exists for it (only the defaulted policy
    # keys above).  Passive like the recovery monitor.
    foreign_objective_monitor = ForeignObjectiveMonitor(
        unit_ids=unit_ids,
        settings=_foreign_objective_settings(config),
        clock=resolved_clock,
        audit=audit_port,
        bus=bus,
        process_instance_id=process_instance_id,
        process_origin_mono=process_origin_mono,
        configuration_version=config.revision,
    )
    # --- excess-solar projection controller (P6: block PRESENT composes) ---
    # Built BEFORE the facade (the facade projects and toggles through it)
    # and BEFORE the adviser (the adviser consumes its participation verdict
    # at tick start; the controller binds the adviser for the live held
    # read).  P3: the once-ever net-billing acknowledgement boot-loads from
    # the durable store — one keyed existence check, never an audit scan.
    excess_controller: ExcessAdviserController | None = None
    if excess_config is not None:
        excess_controller = ExcessAdviserController(
            charge_cap_w=int(excess_config.max_charge_from_export_w),
            clock=resolved_clock,
            acknowledged_economics=audit_store.contains_event(EXCESS_ECONOMICS_ACK_EVENT_ID),
            config_enabled=excess_config.enabled,
            bus=bus,
        )
    # --- schedule surface control (DESIGN_SCHEDULES §3: block PRESENT composes) ---
    # Built BEFORE the facade (the facade projects and publishes through it),
    # and the runner is bound AFTER the facade exists (the runner submits
    # through the facade's internal schedule twin).  The once-ever night
    # acknowledgement boot-loads from the durable store — one keyed existence
    # check, never an audit scan (the exact NET_BILLED mechanics).
    schedule_surface: ScheduleSurfaceControl | None = None
    schedule_config = config.schedule
    if schedule_config is not None:
        schedule_surface = ScheduleSurfaceControl(
            policy=SchedulePolicy(
                allowed_windows_local=tuple(
                    (parse_hhmm(start), parse_hhmm(end))
                    for start, end in schedule_config.allowed_windows_local
                ),
                intent_ttl_s=float(schedule_config.intent_ttl_s),
                timezone=config.site.timezone,
            ),
            store=schedule_store,
            acknowledged_night_windows=audit_store.contains_event(SCHEDULE_NIGHT_ACK_EVENT_ID),
        )
    # --- night charge composition (DESIGN_NIGHT_CHARGE §3: block PRESENT) ---
    # Built BEFORE the facade (the facade projects and toggles through it)
    # and BEFORE the adviser (the adviser consumes its participation verdict
    # at tick start; the controller binds the adviser for the live held
    # read).  §3.2: the once-ever night-partition acknowledgement boot-loads
    # from the durable store — one keyed existence check under the schedule
    # surface's own historical event id (either surface's capture counts);
    # an unacknowledged site composes SUSPENDED even with `enabled: true`.
    night_controller: NightChargeController | None = None
    if night_config is not None:
        night_controller = NightChargeController(
            pacing=night_config.pacing,
            rate_cap_w=int(night_config.rate_cap_w),
            hold_rate_w=int(night_config.hold_rate_w),
            demand_scope=night_config.demand_scope,
            demand_response=night_config.demand_response,
            demand_threshold_w=int(night_config.demand_threshold_w),
            windows=tuple(
                (parse_hhmm(start), parse_hhmm(end)) for start, end in night_config.window_local
            ),
            timezone=night_config.timezone,
            posture=(
                schedule_surface.policy.posture
                if schedule_surface is not None
                else "yield"  # pragma: no cover - the grant requires the block
            ),
            clock=resolved_clock,
            acknowledged_partition=audit_store.contains_event(SCHEDULE_NIGHT_ACK_EVENT_ID),
            config_enabled=night_config.enabled,
            bus=bus,
        )
    # --- energy scorecard composition (DESIGN_ENERGY_SCORECARD sections 5-7) ---
    # Built BEFORE the facade (the facade projects through the control) and
    # composed exactly when the block is PRESENT.  The accountant consumes
    # observations only; its rollover promotion asks each actor for ONE
    # promoted cold-ring read of the energy block (the B4 precedent).
    energy_accountant: EnergyAccountant | None = None
    energy_surface: EnergyScorecardControl | None = None
    if energy_config is not None:
        grid_source = (
            GRID_SOURCE_DEVICE_COUNTER
            if energy_config.grid_source == "device_counter"
            else GRID_SOURCE_INTEGRATED
        )

        def _request_energy_refresh() -> None:
            for handle in actors.values():
                handle.request_energy_refresh()

        energy_accountant = EnergyAccountant(
            unit_ids=tuple(unit.unit_id for unit in config.units),
            timezone=config.site.timezone,
            settings=EnergyAccountingSettings(
                grid_source=grid_source,
                grid_counter_roles=energy_config.grid_counter_roles,
                integration_max_gap_s=float(energy_config.integration_max_gap_s),
                min_day_coverage_pct=float(energy_config.min_day_coverage_pct),
            ),
            clock=resolved_clock,
            ledger=energy_store,
            bus=bus,
            audit=audit_port,
            request_energy_refresh=_request_energy_refresh,
            process_instance_id=process_instance_id,
            process_origin_mono=process_origin_mono,
            configuration_version=config.revision,
        )
        tariff = energy_config.tariff
        energy_surface = EnergyScorecardControl(
            accountant=energy_accountant,
            clock=resolved_clock,
            tariff=(
                None
                if tariff is None
                else {
                    "currency": tariff.currency,
                    "import_cents_per_kwh": float(tariff.import_cents_per_kwh),
                    "export_cents_per_kwh": float(tariff.export_cents_per_kwh),
                }
            ),
        )
    # --- telemetry historian composition (DESIGN_PLANT_HISTORY sections 2.1+2.5) ---
    # Composed exactly when the ``plant_history`` block is PRESENT, reading
    # the observation port and the injected recovery view (health_state),
    # attributing OPTIMIZER winners through the advisers' own single-writer
    # projections.  Observability only: nothing in control reads it back.
    historian: TelemetryHistorian | None = None
    history_surface: PlantHistoryControl | None = None
    if history_present and history_config is not None and history_store is not None:
        configured_units = tuple(unit.unit_id for unit in config.units)

        def _adviser_claims() -> dict[str, str]:
            # The adviser attribution the commanded triple reads: each
            # single-writer fleet-loop projection names the unit it claims.
            # The excess projection composes today; the night-charge
            # projection joins this map when its surface composes (the
            # DESIGN_PLANT_HISTORY section 6 archaeology pin).
            claims: dict[str, str] = {}
            if excess_controller is not None:
                state = excess_controller.state()
                if state.active and state.target_unit_id is not None:
                    claims[state.target_unit_id] = "excess_adviser"
            return claims

        historian = TelemetryHistorian(
            unit_ids=configured_units,
            sample_interval_s=float(history_config.sample_interval_s),
            control_period_s=float(config.timing.control_period_s),
            clock=resolved_clock,
            history=history_store,
            observations=observation_port,
            timezone=config.site.timezone,
            intents=intent_port,
            health_states=recovery_monitor.unit_health_states,
            adviser_claims=_adviser_claims,
        )
        history_surface = PlantHistoryControl(
            unit_ids=configured_units,
            sample_interval_s=float(history_config.sample_interval_s),
            retention_full_resolution_days=int(history_config.retention_full_resolution_days),
            repository=history_store,
        )
    # --- advisory forecast providers (ARCHITECTURE section 17) -----------
    # Composed only when the ``forecast_providers`` block is PRESENT and
    # ENABLED (an absent or disabled block composes NOTHING -- the staged
    # posture, byte-identical).  ADVISORY-ONLY: this handle feeds advisers
    # and projections only; nothing in the fleet loop, the kernel, or any
    # safety path reads it, and constructing the adapters opens no socket
    # (the transport is inert until a consumer's first fetch).  A declared
    # provider the environment cannot deliver (the Solcast key reference
    # pointing at an unset variable) is OMITTED WITH A NOTE, never a boot
    # failure -- an advisory provider's absence degrades its own data only.
    forecast_registry: ForecastProviderRegistry | None = None
    providers_config = config.forecast_providers
    if providers_config is not None and providers_config.enabled:
        configured_units = tuple(unit.unit_id for unit in config.units)
        refresh_s = float(providers_config.refresh_interval_s)
        stale_s = float(providers_config.stale_after_s)
        wire = HttpxForecastTransport(timeout_s=float(providers_config.request_timeout_s))
        registry_weather: WeatherProvider | None = None
        registry_pv: PvForecastProvider | None = None
        registry_load: LoadForecastProvider | None = None
        registry_tariff: StaticTariffProvider | None = None
        registry_notes: list[str] = []
        open_meteo = providers_config.open_meteo
        if open_meteo is not None:
            registry_weather = OpenMeteoWeather(
                transport=wire,
                clock=resolved_clock,
                latitude=float(open_meteo.latitude),
                longitude=float(open_meteo.longitude),
                forecast_days=int(open_meteo.forecast_days),
                refresh_interval_s=refresh_s,
                stale_after_s=stale_s,
            )
            if open_meteo.pv is not None:
                plane = open_meteo.pv
                registry_pv = OpenMeteoPvForecast(
                    transport=wire,
                    clock=resolved_clock,
                    latitude=float(open_meteo.latitude),
                    longitude=float(open_meteo.longitude),
                    tilt_deg=float(plane.tilt_deg),
                    azimuth_deg=float(plane.azimuth_deg),
                    capacity_kw=float(plane.capacity_kw),
                    derate=float(plane.derate),
                    forecast_days=int(open_meteo.forecast_days),
                    refresh_interval_s=refresh_s,
                    stale_after_s=stale_s,
                )
        solcast = providers_config.solcast
        if solcast is not None:
            # The secret REFERENCE resolves here, once, at composition: the
            # key itself never lives in the configuration file.
            api_key = environ.get(solcast.api_key_env)
            if api_key:
                registry_pv = SolcastPvForecast(
                    transport=wire,
                    clock=resolved_clock,
                    api_key=api_key,
                    latitude=float(solcast.latitude),
                    longitude=float(solcast.longitude),
                    capacity_kw=float(solcast.capacity_kw),
                    hours=int(solcast.hours),
                    period=str(solcast.period),
                    refresh_interval_s=refresh_s,
                    stale_after_s=stale_s,
                )
            else:
                registry_notes.append(
                    f"solcast: the api key environment variable "
                    f"{solcast.api_key_env} is not set; the PV provider is absent "
                    f"until the reference resolves"
                )
        baseline = providers_config.load_baseline
        if baseline is not None:
            if history_store is not None:
                registry_load = HistorianLoadForecast(
                    source=history_store,
                    unit_ids=configured_units,
                    clock=resolved_clock,
                    slot_s=float(baseline.slot_s),
                    horizon_s=float(baseline.horizon_s),
                    weeks_back=int(baseline.weeks_back),
                )
            else:  # pragma: no cover - the config gate requires plant_history
                registry_notes.append(
                    "load_baseline: no history store composed; the baseline is absent"
                )
        declared_tariff = providers_config.tariff
        if declared_tariff is not None:
            registry_tariff = StaticTariffProvider(
                clock=resolved_clock,
                timezone_name=config.site.timezone,
                currency=str(declared_tariff.currency),
                default_import_cents_per_kwh=float(declared_tariff.default_import_cents_per_kwh),
                default_export_cents_per_kwh=float(declared_tariff.default_export_cents_per_kwh),
                windows=tuple(
                    TariffRateWindow(
                        window_local=(
                            str(window.window_local[0]),
                            str(window.window_local[1]),
                        ),
                        import_cents_per_kwh=float(window.import_cents_per_kwh),
                        export_cents_per_kwh=float(window.export_cents_per_kwh),
                    )
                    for window in declared_tariff.windows
                ),
                horizon_s=float(declared_tariff.horizon_s),
            )
        forecast_registry = ForecastProviderRegistry(
            weather=registry_weather,
            pv=registry_pv,
            load=registry_load,
            tariff=registry_tariff,
            notes=tuple(registry_notes),
        )
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
        recovery=recovery_monitor,
        objectives=foreign_objective_monitor,
        excess=excess_controller,
        schedules=schedule_surface,
        night=night_controller,
        energy=energy_surface,
        history=history_surface,
    )

    # --- excess-solar advisory composition ---------------------------------
    excess_adviser: ExcessChargeAdviser | None = None
    if excess_config is not None and excess_controller is not None:
        # The adviser is composed exactly when the block is PRESENT (P6) —
        # participating only while enabled AND acknowledged — under the
        # composed automation principal, driving the facade's internal
        # advisory submission (never REST/MCP).  Everything downstream is
        # the existing arbiter -> allocator -> SafetyKernel -> per-unit
        # authority path; the adviser holds no special authority anywhere.
        adviser_principal = _AdvisoryPrincipal(
            subject=_EXCESS_ADVISER_PRINCIPAL_SUBJECT,
            scopes=_EXCESS_ADVISER_PRINCIPAL_SCOPES,
            interactive=False,
            site_id=config.site.site_id,
        )

        async def _submit_advisory_intent(
            *, unit_ids: Any, direction: Any, watts: Any, ttl_s: Any
        ) -> Any:
            return await facade.submit_advisory_intent(
                unit_ids=unit_ids,
                direction=direction,
                watts=watts,
                ttl_s=ttl_s,
                principal=adviser_principal,
            )

        excess_adviser = ExcessChargeAdviser(
            settings=ExcessChargeSettings(
                assumed_autonomous_charge_w=excess_config.assumed_autonomous_charge_w,
                min_acceleration_w=excess_config.min_acceleration_w,
                exit_hysteresis_w=excess_config.exit_hysteresis_w,
                intent_ttl_s=excess_config.intent_ttl_s,
            ),
            policy=policy,
            clock=resolved_clock,
            observations=observation_port,
            intents=intent_port,
            submit=_submit_advisory_intent,
            participation=excess_controller.participation_verdict,
            # DESIGN_SCHEDULES §4: the commissioned yield choice reaches the
            # adviser's own claim check (default true).
            yield_to_schedule=excess_config.yield_to_schedule,
        )
        excess_controller.bind_adviser(excess_adviser)

    # --- schedule runner composition (DESIGN_SCHEDULES §2) ------------------
    schedule_runner: ScheduleRunner | None = None
    if schedule_config is not None and schedule_surface is not None:
        # The runner is composed exactly when the block is PRESENT, under the
        # composed automation principal, driving the facade's internal
        # schedule submission (never REST/MCP).  Everything downstream is the
        # ordinary intent path — the arbiter ranks SCHEDULE lowest, per unit,
        # and the kernel/actor/watchdog treat the runner's intent exactly like
        # any manual request.  The runner reads the plan through the surface
        # control (one repository singleton; a publish lands within one cycle)
        # and the projection is its single writer.
        runner_principal = _ScheduleRunnerPrincipal(
            subject=_SCHEDULE_RUNNER_PRINCIPAL_SUBJECT,
            scopes=_SCHEDULE_RUNNER_PRINCIPAL_SCOPES,
            interactive=False,
            site_id=config.site.site_id,
        )

        async def _submit_schedule_drive(
            *,
            unit_ids: Any,
            direction: Any,
            watts: Any,
            ttl_s: Any,
            watts_by_unit: Any = None,
        ) -> Any:
            return await facade.submit_schedule_intent(
                unit_ids=unit_ids,
                direction=direction,
                watts=watts,
                ttl_s=ttl_s,
                watts_by_unit=watts_by_unit,
                principal=runner_principal,
            )

        schedule_runner = ScheduleRunner(
            store=schedule_surface,
            evaluator=ScheduleEvaluator(intent_ttl_s=float(schedule_config.intent_ttl_s)),
            clock=resolved_clock,
            submit=_submit_schedule_drive,
            intents=intent_port,
            # The disarmed-window honesty read (the 2026-08-23 live finding):
            # the SAME observation repository the fleet loop reads, so the
            # projection can say "window open but the units are disarmed"
            # instead of leaving the arm requirement audit-only.
            observations=observation_port,
            bus=bus,
            posture=schedule_surface.policy.posture,
            initial_plan=schedule_store.get(),
        )
        schedule_surface.bind_runner(schedule_runner)

    # --- night charge advisory composition (DESIGN_NIGHT_CHARGE §2) ---------
    night_adviser: NightChargeAdviser | None = None
    if night_config is not None and night_controller is not None:
        # The adviser is composed exactly when the block is PRESENT —
        # participating only while enabled AND acknowledged — under the
        # composed automation principal, driving the facade's internal night
        # submission (never REST/MCP).  A night charge is an ordinary
        # OPTIMIZER intent: the arbiter, allocator, SafetyKernel, actor, and
        # authority path downstream are exactly the existing ones.
        night_principal = _NightAdviserPrincipal(
            subject=_NIGHT_ADVISER_PRINCIPAL_SUBJECT,
            scopes=_NIGHT_ADVISER_PRINCIPAL_SCOPES,
            interactive=False,
            site_id=config.site.site_id,
        )

        async def _submit_night_drive(
            *,
            unit_ids: Any,
            direction: Any,
            watts: Any,
            ttl_s: Any,
            watts_by_unit: Any = None,
        ) -> Any:
            return await facade.submit_night_intent(
                unit_ids=unit_ids,
                direction=direction,
                watts=watts,
                ttl_s=ttl_s,
                watts_by_unit=watts_by_unit,
                principal=night_principal,
            )

        night_adviser = NightChargeAdviser(
            settings=NightChargeSettings(
                rate_cap_w=int(night_config.rate_cap_w),
                hold_rate_w=int(night_config.hold_rate_w),
                demand_threshold_w=int(night_config.demand_threshold_w),
                demand_exit_hysteresis_w=int(night_config.demand_exit_hysteresis_w),
                demand_scope=night_config.demand_scope,
                demand_response=night_config.demand_response,
                pacing=night_config.pacing,
                assumed_capacity_wh=dict(night_config.assumed_capacity_wh or {}),
                demand_telemetry_max_age_s=float(night_config.demand_telemetry_max_age_s),
                intent_ttl_s=float(night_config.intent_ttl_s),
                windows=tuple(
                    (parse_hhmm(start), parse_hhmm(end)) for start, end in night_config.window_local
                ),
                timezone=night_config.timezone,
                unit_ids=tuple(unit.unit_id for unit in config.units),
            ),
            policy=policy,
            clock=resolved_clock,
            observations=observation_port,
            intents=intent_port,
            submit=_submit_night_drive,
            participation=night_controller.participation_verdict,
        )
        night_controller.bind_adviser(night_adviser)

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
        audit=audit_port,
        process_instance_id=process_instance_id,
        process_origin_mono=process_origin_mono,
        adviser=excess_adviser,
        excess_controller=excess_controller,
        night_adviser=night_adviser,
        night_controller=night_controller,
        intents=intent_port,
        observations=observation_port,
        schedule_runner=schedule_runner,
        recovery=recovery_monitor,
        foreign_objective=foreign_objective_monitor,
        energy_accountant=energy_accountant,
        historian=historian,
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
        recovery=recovery_monitor,
        foreign_objective=foreign_objective_monitor,
        night_adviser=night_adviser,
        night_controller=night_controller,
        excess_adviser=excess_adviser,
        excess_controller=excess_controller,
        schedule_surface=schedule_surface,
        schedule_runner=schedule_runner,
        energy_accountant=energy_accountant,
        energy_ledger=energy_store,
        energy_surface=energy_surface,
        historian=historian,
        history_repository=history_store,
        history_surface=history_surface,
        forecast_providers=forecast_registry,
    )


__all__ = ["ComposedRuntime", "build_runtime"]
