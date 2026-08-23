"""Contract tests for the EnergyServiceFacade application service.

The production module ``energypod.application.service`` does not exist yet.  It is
imported lazily through the ``api`` fixture so this red-phase suite collects
cleanly and every missing contract surfaces as an ordinary test failure.  All
collaborating ports (intent/observation/authorization/audit repositories, event
bus, fleet coordinator wrapper, actor handles) are deterministic inline fakes;
the facade is the only real module under test.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import importlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.test_actor import IDENTITY as ACTOR_IDENTITY
from tests.unit.test_actor import PROFILE as ACTOR_PROFILE
from tests.unit.test_actor import UNIT_ID as ACTOR_UNIT_ID
from tests.unit.test_actor import (
    AuthorizationRecord as ActorAuthorization,
)
from tests.unit.test_actor import (
    EncodedWrite,
    FakeCommandEncoder,
    Gate,
    ObservationRecord,
    SpyTransport,
    settle_until,
)
from tests.unit.test_actor import FakeAuthorizationRepository as ActorAuthorizationRepository
from tests.unit.test_actor import FakeClock as ActorClock
from tests.unit.test_actor import FakeObservationRepository as ActorObservationRepository

SITE_ID = "home"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
QUALITY_FIELDS = (
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


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    interactive: bool = True
    site_id: str = SITE_ID


OPERATOR = Principal(
    subject="person:operator",
    scopes=frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
)
VIEWER = Principal(
    subject="person:viewer",
    scopes=frozenset(),
    interactive=False,
)
STRANGER = Principal(
    subject="person:outsider",
    scopes=frozenset({"observe", "dispatch", "arm", "stop", "stop:acknowledge"}),
    site_id="elsewhere",
)


class FakeClock:
    """Deterministic injected clock: no wall time, no sleeping."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.wall = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall


@dataclass(frozen=True)
class Telemetry:
    unit_id: str
    captured_at_mono: float
    battery_watts: float
    quality: Mapping[str, str]
    # Advisory mode words (2026-08-23 incident 1): decoded device-mode
    # evidence; absent on telemetry a deployment never served.
    debug_mode_w: int | None = None
    ctrl_mode_w: int | None = None
    work_mode_w: int | None = None
    run_mode_w: int | None = None

    @property
    def debug_mode_active(self) -> bool | None:
        return None if self.debug_mode_w is None else self.debug_mode_w != 0

    @property
    def ctrl_mode_remote(self) -> bool | None:
        return None if self.ctrl_mode_w is None else self.ctrl_mode_w == 1


@dataclass(frozen=True)
class Capability:
    unit_id: str
    direction: str
    watts: int
    not_before_mono: float = 0.0
    expires_at_mono: float = math.inf


def good_quality() -> dict[str, str]:
    return {field: "good" for field in QUALITY_FIELDS}


TELEMETRY_SUMMARY_FIELDS = (
    "soc_pct",
    "bms_soc_pct",
    "soh_pct",
    "pack_voltage_v",
    "pack_current_a",
    "battery_watts",
    "dynamic_charge_limit_w",
    "dynamic_discharge_limit_w",
    "cell_count",
    "cell_min_v",
    "cell_max_v",
    "cell_spread_mv",
    "temperature_min_c",
    "temperature_max_c",
    "active_faults",
    "active_warnings",
    "grid_power_w",
    "load_power_w",
    "debug_mode_w",
    "ctrl_mode_w",
    "work_mode_w",
    "run_mode_w",
)

# Live-decoded reference values from the first hardware capture
# (docs/CONTINUITY.md evidence log): MID 10% / 192.4 V / 60 cells at
# 3.205-3.209 V / 23-28 C; RHS 68% / 164.5 V / 50 cells at 3.289-3.292 V;
# LHS 48% / 196.8 V / 60 cells.  All units carry both warnings and no faults.
FLEET_WARNINGS = ("DCDC_Warning0_1", "PCS_Warning0_1")
MID_IDENTITY = "BEP0005KXX11B10500055"


def decoded_observation(
    api: Any,
    *,
    unit_id: str,
    soc_pct: float,
    pack_voltage_v: float,
    cell_count: int,
    cell_low_v: float,
    cell_high_v: float,
    temperatures_c: tuple[float, ...],
    battery_watts: float,
    pack_current_a: float,
    bms_soc_pct: float | None = None,
    soh_pct: float | None = None,
    dynamic_charge_limit_w: float | None = None,
    dynamic_discharge_limit_w: float | None = None,
    sequence: int = 41,
    captured_at_mono: float = 99.5,
    cell_sequence: int = 12,
    cell_captured_at_mono: float = 98.0,
    connection_epoch: int = 3,
    grid_power_w: float | None = None,
    load_power_w: float | None = None,
) -> Any:
    """Build one real domain Observation carrying the captured fleet values."""
    steps = max(1, round((cell_high_v - cell_low_v) / 0.001))
    ladder = [cell_low_v + step * 0.001 for step in range(steps)]
    ladder.append(cell_high_v)
    cells = tuple(ladder[index % len(ladder)] for index in range(cell_count))
    return api.Observation(
        unit_id=unit_id,
        device_identity=MID_IDENTITY,
        connection_epoch=connection_epoch,
        wall_timestamp=datetime(2026, 8, 22, 0, 4, 5, tzinfo=UTC),
        captured_at_mono=captured_at_mono,
        sequence=sequence,
        lifecycle=api.UnitLifecycle.DISARMED,
        protocol_profile="iot-v1",
        system_soc_pct=soc_pct,
        bms_soc_pct=soc_pct if bms_soc_pct is None else bms_soc_pct,
        soh_pct=soh_pct,
        battery_watts=battery_watts,
        pack_voltage_v=pack_voltage_v,
        pack_current_a=pack_current_a,
        dynamic_charge_limit_w=dynamic_charge_limit_w,
        dynamic_discharge_limit_w=dynamic_discharge_limit_w,
        expected_cell_count=cell_count,
        cell_voltages_v=cells,
        cell_captured_at_mono=cell_captured_at_mono,
        cell_sequence=cell_sequence,
        expected_temperature_count=len(temperatures_c),
        temperatures_c=temperatures_c,
        active_faults=frozenset(),
        active_warnings=frozenset(FLEET_WARNINGS),
        grid_power_w=grid_power_w,
        load_power_w=load_power_w,
        quality={field: api.DataQuality.GOOD for field in QUALITY_FIELDS},
    )


def mid_observation(api: Any) -> Any:
    return decoded_observation(
        api,
        unit_id="MID",
        soc_pct=10.0,
        pack_voltage_v=192.4,
        cell_count=60,
        cell_low_v=3.205,
        cell_high_v=3.209,
        temperatures_c=(23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 24.5, 25.5),
        battery_watts=2405.0,
        pack_current_a=12.5,
        soh_pct=99.0,
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=3000.0,
        # Per-pod CT words from the same first hardware capture
        # (PROTOCOL_EVIDENCE 4c: PCS 0x1000+17/+20; importing, so negative).
        grid_power_w=-1736.0,
        load_power_w=1701.0,
    )


def rhs_observation(api: Any) -> Any:
    return decoded_observation(
        api,
        unit_id="RHS",
        soc_pct=68.0,
        pack_voltage_v=164.5,
        cell_count=50,
        cell_low_v=3.289,
        cell_high_v=3.292,
        temperatures_c=(25.0, 26.5),
        battery_watts=1192.0,
        pack_current_a=7.25,
        soh_pct=98.0,
        dynamic_charge_limit_w=2000.0,
        dynamic_discharge_limit_w=2500.0,
        sequence=12,
        cell_sequence=3,
        grid_power_w=-37.0,
        load_power_w=1063.0,
    )


def lhs_observation(api: Any) -> Any:
    return decoded_observation(
        api,
        unit_id="LHS",
        soc_pct=48.0,
        pack_voltage_v=196.8,
        cell_count=60,
        cell_low_v=3.310,
        cell_high_v=3.314,
        temperatures_c=(22.5, 23.5),
        battery_watts=0.0,
        pack_current_a=0.0,
        soh_pct=97.0,
        dynamic_charge_limit_w=2800.0,
        dynamic_discharge_limit_w=2800.0,
        sequence=27,
        cell_sequence=9,
    )


class FakeIntentRepository:
    """Async intent port; emergency-stop intents stay active until removed."""

    def __init__(self, seeded: tuple[Any, ...] = ()) -> None:
        self.seeded = list(seeded)
        self.added: list[Any] = []
        self.removed: list[str] = []
        self.active_calls: list[float] = []
        self.failing = False
        # Models a store that refused the stop's intent (capacity exhausted,
        # transient fault) while later serving removals again.
        self.add_failing = False

    async def add(self, intent: Any) -> None:
        if self.failing or self.add_failing:
            raise OSError("intent store unavailable")
        self.added.append(intent)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        self.active_calls.append(now_mono)
        if self.failing:
            raise OSError("intent store unavailable")
        removed = set(self.removed)
        live = []
        for item in [*self.seeded, *self.added]:
            expired = item.accepted_at_mono + item.duration_s <= now_mono
            latched = item.source.value == "emergency_stop"
            if (
                item.id not in removed
                and item.accepted_at_mono <= now_mono
                and (latched or not expired)
            ):
                live.append(item)
        return tuple(live)

    async def remove(self, intent_id: str) -> None:
        if self.failing:
            raise OSError("intent store unavailable")
        self.removed.append(intent_id)


class FakeObservationRepository:
    def __init__(self, latest: Mapping[str, Any] | None = None) -> None:
        self.latest_values = dict(latest or {})
        self.calls: list[str] = []
        self.failing = False

    async def latest(self, unit_id: str) -> Telemetry | None:
        self.calls.append(f"latest:{unit_id}")
        if self.failing:
            raise OSError("observation store unavailable")
        return self.latest_values.get(unit_id)

    async def all_latest(self) -> dict[str, Telemetry]:
        self.calls.append("all_latest")
        if self.failing:
            raise OSError("observation store unavailable")
        return dict(self.latest_values)


class FakeAuthorizationRepository:
    """Single-use capability slot mirroring the production port semantics."""

    def __init__(
        self, current: Mapping[str, Capability] | None = None, *, now_mono: float = 0.0
    ) -> None:
        self.slots = dict(current or {})
        self.now_mono = now_mono
        self.published: list[Any] = []
        self.current_calls: list[tuple[str, float]] = []
        self.peek_calls: list[str] = []
        self.revocations: list[str] = []
        self.failing = False

    async def publish(self, batch: Any) -> None:
        if self.failing:
            raise OSError("authorization store unavailable")
        self.published.append(batch)

    async def current(self, unit_id: str, now_mono: float) -> Capability | None:
        self.current_calls.append((unit_id, now_mono))
        if self.failing:
            raise OSError("authorization store unavailable")
        return self.slots.pop(unit_id, None)

    async def peek(self, unit_id: str) -> Capability | None:
        # API_CONTRACTS AuthorizationRepository.peek(unit_id): non-consuming
        # projection read returning the currently valid capability (not-before
        # satisfied and unexpired) or None; snapshots peek, control uses current().
        self.peek_calls.append(unit_id)
        if self.failing:
            raise OSError("authorization store unavailable")
        capability = self.slots.get(unit_id)
        if capability is None:
            return None
        if capability.not_before_mono > self.now_mono:
            return None
        if capability.expires_at_mono <= self.now_mono:
            return None
        return capability

    async def revoke(self, *args: Any, **kwargs: Any) -> None:
        self.revocations.append(str(kwargs.get("reason", "")))


class FakeAuditRepository:
    """Newest-first durable trail; appends are recorded in shared history."""

    def __init__(self, events: tuple[Any, ...] = (), history: list[str] | None = None) -> None:
        self.events: list[Any] = list(events)
        self.appended: list[Any] = []
        self.recent_calls: list[int] = []
        self.recent_cursors: list[int | None] = []
        self.history = history
        self.failing = False
        # Impl-10 compensation pin: fail exactly the first N append calls,
        # then recover -- the primary row cannot land but the compensation
        # rows must.
        self.fail_first_appends = 0
        self._append_calls = 0

    async def append(self, event: Any) -> None:
        self._append_calls += 1
        if self.history is not None:
            self.history.append("audit")
        if self.failing or self._append_calls <= self.fail_first_appends:
            raise OSError("audit store unavailable")
        self.appended.append(event)
        self.events.insert(0, event)

    async def recent(self, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]:
        self.recent_calls.append(limit)
        self.recent_cursors.append(after_sequence)
        if self.failing:
            raise OSError("audit store unavailable")
        window = self.events
        if after_sequence is not None:
            window = [event for event in window if field_of(event, "sequence") < after_sequence]
        return tuple(window[:limit])


class FakeEventBus:
    """Monotonic sequence source; publishing is the only sequence mutation."""

    def __init__(self, starting_sequence: int = 0, history: list[str] | None = None) -> None:
        self.published: list[dict[str, Any]] = []
        self.sequence_reads = 0
        self.failing = False
        self._sequence = starting_sequence
        self._history = history

    async def publish(self, body: Mapping[str, Any]) -> int:
        if self._history is not None:
            self._history.append("publish")
        if self.failing:
            raise OSError("event bus unavailable")
        self.published.append(dict(body))
        self._sequence += 1
        return self._sequence

    def snapshot_sequence(self) -> int:
        self.sequence_reads += 1
        return self._sequence


class RecordingCoordinator:
    """Delegating wrapper around the real fleet generation fence."""

    def __init__(self, api: Any, history: list[str]) -> None:
        self._inner = api.AuthorityGenerationCoordinator()
        self._history = history
        self.failing = False
        self.advance_error: BaseException | None = None

    async def snapshot(self) -> Any:
        if self.failing:
            raise OSError("authority coordinator unavailable")
        return await self._inner.snapshot()

    async def advance(self, *, reason: str) -> Any:
        if self.failing:
            raise OSError("authority coordinator unavailable")
        moved = await self._inner.advance(reason=reason)
        self._history.append("fence")
        if self.advance_error is not None:
            # The fence itself landed; only its acknowledgement is lost.  A
            # facade that abandons the remaining stop work here fails the
            # degraded-dependency matrix.
            raise self.advance_error
        return moved


class FakeRecoveryView:
    """Inline stand-in for the recovery monitor's read projection (R4).

    Serves fixed ``UnitHealthView``-shaped records so the facade's snapshot
    and health projections can be pinned without composing the monitor.
    """

    def __init__(
        self, states: Mapping[str, Mapping[str, Any]] | None = None, *, failing: bool = False
    ) -> None:
        self.states = {
            unit_id: SimpleNamespace(
                unit_id=unit_id,
                state=spec.get("state"),
                reasons=tuple(spec.get("reasons", ())),
                remediation_hint=spec.get("remediation_hint"),
            )
            for unit_id, spec in (states or {}).items()
        }
        self.failing = failing
        self.calls = 0

    async def unit_health_states(self) -> dict[str, Any]:
        self.calls += 1
        if self.failing:
            raise OSError("recovery view unavailable")
        return dict(self.states)


class FakeActorHandle:
    """Inline stand-in for the per-unit actor handle port the facade composes."""

    def __init__(
        self,
        *,
        unit_id: str,
        lifecycle: Any,
        armed_lifecycle: Any,
        history: list[str],
        disarmed_lifecycle: Any = None,
        qualified: bool | None = True,
        inhibit_latched: bool = False,
        inhibit_cause: str | None = None,
        arm_error: BaseException | None = None,
        disarm_error: BaseException | None = None,
        zero_error: BaseException | None = None,
        refresh_outcomes: tuple[tuple[int, int] | BaseException, ...] = (),
    ) -> None:
        self.unit_id = unit_id
        self.lifecycle = lifecycle
        self.disarmed_lifecycle = lifecycle if disarmed_lifecycle is None else disarmed_lifecycle
        self.armed_lifecycle = armed_lifecycle
        self.qualified = qualified
        self.inhibit_latched = inhibit_latched
        self.inhibit_cause = inhibit_cause if inhibit_latched else None
        self.arm_error = arm_error
        self.disarm_error = disarm_error
        self.zero_error = zero_error
        self.history = history
        # B5 (SYNC_RESILIENCE_AUDIT): the bounded fresh mode-word read the
        # dispatch refusal path performs through the owning actor.  Each call
        # pops one outcome; an exhausted or empty queue models an unreadable
        # refresh (which the facade must treat as a failed read).
        self.refresh_outcomes = collections.deque(refresh_outcomes)
        self.refresh_calls = 0

    async def refresh_mode_words(self) -> tuple[int, int]:
        self.refresh_calls += 1
        if self.refresh_outcomes:
            outcome = self.refresh_outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        raise OSError("mode refresh read failed")

    async def arm(self, *, takeover_acknowledged: bool = False) -> None:
        self.history.append(f"arm:{self.unit_id}")
        if takeover_acknowledged:
            self.history.append(f"arm-takeover:{self.unit_id}")
        if self.arm_error is not None:
            raise self.arm_error
        # Only an explicit "not qualified" report refuses here: a None
        # (unknown) qualification is accepted by the handle so the facade's
        # own unknown-state gate is the only thing that can refuse it.
        if (
            self.qualified is False
            or self.inhibit_latched
            or self.lifecycle is not self.disarmed_lifecycle
        ):
            raise RuntimeError("unit is not qualified for arming")
        self.lifecycle = self.armed_lifecycle

    async def disarm(self) -> None:
        self.history.append(f"disarm:{self.unit_id}")
        if self.disarm_error is not None:
            raise self.disarm_error
        self.lifecycle = self.disarmed_lifecycle

    async def acknowledge_inhibit(self) -> None:
        self.history.append(f"inhibit-ack:{self.unit_id}")
        self.inhibit_latched = False

    async def request_bounded_zero(self, reason: str) -> None:
        del reason
        self.history.append(f"zero:{self.unit_id}")
        if self.zero_error is not None:
            raise self.zero_error

    async def fence(self, reason: str) -> int:
        del reason
        self.history.append(f"fence:{self.unit_id}")
        return 0


@dataclass
class RealActorHandle:
    """Facade-facing handle over the production actor, fence included.

    The composed runtime wraps its actors the same way; this local adapter
    lets the facade be exercised against the real actor without importing the
    composition root.
    """

    actor: Any

    @property
    def unit_id(self) -> str:
        return self.actor.unit_id

    @property
    def lifecycle(self) -> Any:
        return self.actor.lifecycle

    @property
    def qualified(self) -> bool | None:
        return self.actor.qualified

    @property
    def inhibit_latched(self) -> bool:
        return bool(self.actor.inhibit_latched)

    async def arm(self) -> None:
        await self.actor.arm()

    async def disarm(self) -> None:
        await self.actor.disarm()

    async def acknowledge_inhibit(self) -> None:
        await self.actor.acknowledge_inhibit()

    async def request_bounded_zero(self, reason: str) -> None:
        await self.actor.request_bounded_zero(reason)

    async def fence(self, reason: str) -> int:
        return await self.actor.fence(reason)


@dataclass
class Rig:
    api: Any
    facade: Any
    clock: FakeClock
    intents: FakeIntentRepository
    observations: FakeObservationRepository
    authorizations: FakeAuthorizationRepository
    audit: FakeAuditRepository
    bus: FakeEventBus
    coordinator: RecordingCoordinator
    handles: dict[str, FakeActorHandle]
    history: list[str]
    recovery: FakeRecoveryView | None = None
    objectives: FakeForeignObjectiveView | None = None
    excess: Any = None
    schedules: Any = None
    night: Any = None

    def reset_recorders(self) -> None:
        self.intents.added.clear()
        self.intents.removed.clear()
        self.intents.active_calls.clear()
        self.observations.calls.clear()
        self.authorizations.published.clear()
        self.authorizations.current_calls.clear()
        self.authorizations.peek_calls.clear()
        self.authorizations.revocations.clear()
        self.audit.appended.clear()
        self.audit.recent_calls.clear()
        self.bus.published.clear()
        self.bus.sequence_reads = 0
        self.history.clear()

    def recorder_activity(self) -> list[str]:
        probes = (
            ("intents.add", self.intents.added),
            ("intents.active", self.intents.active_calls),
            ("intents.remove", self.intents.removed),
            ("observations", self.observations.calls),
            ("authorizations.publish", self.authorizations.published),
            ("authorizations.current", self.authorizations.current_calls),
            ("authorizations.peek", self.authorizations.peek_calls),
            ("authorizations.revoke", self.authorizations.revocations),
            ("audit.append", self.audit.appended),
            ("audit.recent", self.audit.recent_calls),
            ("bus.publish", self.bus.published),
            ("bus.snapshot_sequence", ["read"] * self.bus.sequence_reads),
            ("actor-operations", self.history),
        )
        return [name for name, values in probes if values]


@pytest.fixture
def api() -> SimpleNamespace:
    try:
        service = importlib.import_module("energypod.application.service")
        domain = importlib.import_module("energypod.domain")
        generation = importlib.import_module("energypod.application.generation")
        return SimpleNamespace(
            EnergyServiceFacade=service.EnergyServiceFacade,
            AuthorityGenerationCoordinator=generation.AuthorityGenerationCoordinator,
            Direction=domain.Direction,
            IntentSource=domain.IntentSource,
            PowerIntent=domain.PowerIntent,
            UnitLifecycle=domain.UnitLifecycle,
            Observation=domain.Observation,
            DataQuality=domain.DataQuality,
        )
    except (ImportError, AttributeError) as error:
        pytest.fail(
            f"application service facade contract is not implemented: {error}", pytrace=False
        )


def make_rig(
    api: Any,
    *,
    units: Mapping[str, Mapping[str, Any]] | None = None,
    telemetry: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Capability] | None = None,
    seeded_intents: tuple[Any, ...] = (),
    audit_events: tuple[Any, ...] = (),
    bus_sequence: int = 0,
    recovery: FakeRecoveryView | None = None,
    objectives: FakeForeignObjectiveView | None = None,
    excess: Any = None,
    schedules: Any = None,
    night: Any = None,
) -> Rig:
    clock = FakeClock()
    history: list[str] = []
    specs = dict(units or {"pod-a": {}, "pod-b": {}})
    handles = {
        unit_id: FakeActorHandle(
            unit_id=unit_id,
            lifecycle=spec.get("lifecycle", api.UnitLifecycle.DISARMED),
            armed_lifecycle=api.UnitLifecycle.ARMED_IDLE,
            disarmed_lifecycle=spec.get("disarmed_lifecycle", api.UnitLifecycle.DISARMED),
            history=history,
            qualified=spec.get("qualified", True),
            inhibit_latched=spec.get("inhibit_latched", False),
            inhibit_cause=spec.get("inhibit_cause"),
            arm_error=spec.get("arm_error"),
            disarm_error=spec.get("disarm_error"),
            zero_error=spec.get("zero_error"),
            refresh_outcomes=tuple(spec.get("refresh_outcomes", ())),
        )
        for unit_id, spec in specs.items()
    }
    intents = FakeIntentRepository(seeded_intents)
    observations = FakeObservationRepository(telemetry)
    authorizations = FakeAuthorizationRepository(capabilities, now_mono=clock.monotonic())
    audit = FakeAuditRepository(audit_events, history)
    bus = FakeEventBus(bus_sequence, history)
    coordinator = RecordingCoordinator(api, history)
    # The detector's port is passed only when a rig asks for it, so the red
    # phase stays scoped to the objective-watch family (an unpassed optional
    # port is exactly the "detector not wired" case those tests pin).
    objective_kwargs = {} if objectives is None else {"objectives": objectives}
    facade = api.EnergyServiceFacade(
        site_id=SITE_ID,
        clock=clock,
        intents=intents,
        observations=observations,
        authorizations=authorizations,
        audit=audit,
        events=bus,
        coordinator=coordinator,
        actors=handles,
        recovery=recovery,
        excess=excess,
        schedules=schedules,
        night=night,
        **objective_kwargs,
    )
    return Rig(
        api=api,
        facade=facade,
        clock=clock,
        intents=intents,
        observations=observations,
        authorizations=authorizations,
        audit=audit,
        bus=bus,
        coordinator=coordinator,
        handles=handles,
        history=history,
        recovery=recovery,
        objectives=objectives,
        excess=excess,
        night=night,
    )


def canonical(value: Any) -> bool:
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def field_of(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def manual_intent(
    api: Any,
    *,
    revision: int,
    watts: int,
    direction: str = "discharge",
    unit_ids: frozenset[str] = frozenset({"pod-a"}),
    accepted_at_mono: float = 90.0,
    duration_s: float = 60.0,
) -> Any:
    return api.PowerIntent(
        id=f"intent-{revision}",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=unit_ids,
        direction=api.Direction(direction),
        watts=watts,
        duration_s=duration_s,
        accepted_at_mono=accepted_at_mono,
        acceptance_revision=revision,
        actor_identity="person:operator",
    )


def submit_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "unit_ids": ["pod-a"],
        "direction": "discharge",
        "watts": 900,
        "ttl_s": 30.0,
        "reason": "grid peak",
        "principal": OPERATOR,
        "idempotency_key": "intent-key-1",
        "request_id": "request-1",
    }
    kwargs.update(overrides)
    return kwargs


def assert_audited_and_published(rig: Rig, subject: str) -> None:
    assert rig.audit.appended, "every facade mutation must reach the audit repository"
    for event in rig.audit.appended:
        assert field_of(event, "principal") == subject
    assert rig.bus.published, "every facade mutation must be published to the event bus"
    for body in rig.bus.published:
        kind = field_of(body, "type")
        assert isinstance(kind, str) and kind, "published event bodies must carry a type"


async def _invoke(
    facade: Any,
    operation: str,
    principal: Principal,
    stop_id: str = "stop-irrelevant",
) -> Any:
    if operation == "snapshot":
        return await facade.snapshot(principal=principal)
    if operation == "health":
        return await facade.health(principal=principal)
    if operation == "unit_detail":
        return await facade.unit_detail(principal=principal, unit_id="pod-a")
    if operation == "recent_audit":
        return await facade.recent_audit(principal=principal, limit=5)
    if operation == "submit_intent":
        return await facade.submit_intent(**submit_kwargs(principal=principal))
    if operation == "arm":
        return await facade.arm(
            unit_ids=["pod-a"],
            principal=principal,
            idempotency_key="arm-key-9",
            request_id="request-r9",
        )
    if operation == "disarm":
        return await facade.disarm(
            unit_ids=["pod-a"],
            principal=principal,
            idempotency_key="disarm-key-9",
            request_id="request-q9",
        )
    if operation == "acknowledge_inhibit":
        return await facade.acknowledge_inhibit(
            unit_id="pod-a",
            principal=principal,
            idempotency_key="inhibit-ack-key-9",
            request_id="request-n9",
        )
    if operation == "emergency_stop":
        return await facade.emergency_stop(
            unit_ids=["pod-a"],
            reason="halt",
            principal=principal,
            idempotency_key="stop-key-9",
            request_id="request-s9",
        )
    return await facade.acknowledge_emergency_stop(
        stop_id=stop_id,
        principal=principal,
        idempotency_key="ack-key-9",
        request_id="request-k9",
    )


async def _stop(
    facade: Any,
    *,
    unit_ids: list[str],
    reason: str,
    idempotency_key: str,
    request_id: str,
) -> Any:
    return await facade.emergency_stop(
        unit_ids=unit_ids,
        reason=reason,
        principal=OPERATOR,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )


# --- snapshot ---------------------------------------------------------------


async def test_snapshot_assembles_site_sequence_and_per_unit_projections(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 1234.0, good_quality())},
        capabilities={"pod-a": Capability("pod-a", "discharge", 700)},
        seeded_intents=(manual_intent(api, revision=3, watts=900),),
        bus_sequence=20,
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["site_id"] == SITE_ID
    assert snapshot["snapshot_sequence"] == 20
    assert snapshot["captured_at"] in {
        rig.clock.wall_now(),
        rig.clock.wall_now().isoformat(),
    }
    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert set(units) == set(rig.handles)
    pod_a = units["pod-a"]
    assert pod_a["lifecycle"] == "disarmed"
    assert pod_a["telemetry_age_s"] == 0.5
    assert pod_a["quality"] == "good"
    assert pod_a["requested_power"] == {"direction": "discharge", "watts": 900}
    assert pod_a["authorized_power"] == {"direction": "discharge", "watts": 700}
    assert pod_a["measured_watts"] == 1234.0
    pod_b = units["pod-b"]
    assert pod_b["lifecycle"] == "disarmed"
    assert pod_b["telemetry_age_s"] is None
    assert pod_b["quality"] == "missing"
    assert pod_b["requested_power"] == {"direction": "idle", "watts": 0}
    assert pod_b["authorized_power"] is None
    assert pod_b["measured_watts"] is None


async def test_snapshot_exposes_latched_stops_and_unit_inhibit_state(api: Any) -> None:
    """2026-08-23 operator-facing defect: the emergency-stop latch was
    event-driven only, so a console opened after a latch showed nothing.

    The snapshot must tell the truth: a non-acknowledged latched stop appears
    in ``active_stops`` with the exact console-rendered shape (null unit_ids
    means fleet-wide), acknowledgement empties the list, and each unit view
    carries its actor's inhibit latch state and cause.
    """
    rig = make_rig(
        api,
        units={"pod-a": {}, "pod-b": {"inhibit_latched": True, "inhibit_cause": "latched"}},
    )
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    assert snapshot["active_stops"] == []
    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["inhibit_latched"] is False
    assert units["pod-a"]["inhibit_cause"] is None
    assert units["pod-b"]["inhibit_latched"] is True
    assert units["pod-b"]["inhibit_cause"] == "latched"

    # A fleet-wide stop latches; the snapshot lists it with the exact shape.
    stop = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="console latch visibility",
        idempotency_key="stop-latch-visible-1",
        request_id="stop-latch-visible-1-r",
    )
    assert stop["status"] == "latched", stop
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    (entry,) = snapshot["active_stops"]
    assert entry == {
        "stop_id": stop["stop_id"],
        "latched_at": rig.clock.wall_now().isoformat(),
        "principal": OPERATOR.subject,
        "reason_codes": ["latched"],
        "unit_ids": None,
    }
    # Console contract (web side, live): the stamp must be JavaScript-Date
    # parseable ISO-8601 -- an explicit UTC offset, never a naive local time.
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)",
        entry["latched_at"],
    )

    # Acknowledgement empties the list (the bus already publishes
    # emergency_stop.acknowledged for the transition itself).
    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=stop["stop_id"],
        principal=OPERATOR,
        idempotency_key="ack-latch-visible-1",
        request_id="ack-latch-visible-1-r",
    )
    assert acknowledged["status"] == "acknowledged", acknowledged
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    assert snapshot["active_stops"] == []


async def test_snapshot_lists_partial_fleet_stops_with_their_unit_ids(api: Any) -> None:
    """A stop that fenced only part of the fleet names exactly those units."""
    rig = make_rig(api, units={"pod-a": {}, "pod-b": {}})
    stop = await _stop(
        rig.facade,
        unit_ids=["pod-a"],
        reason="partial fleet latch",
        idempotency_key="stop-latch-partial-1",
        request_id="stop-latch-partial-1-r",
    )
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    (entry,) = snapshot["active_stops"]
    assert entry["stop_id"] == stop["stop_id"]
    assert entry["unit_ids"] == ["pod-a"]

    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=stop["stop_id"],
        principal=OPERATOR,
        idempotency_key="ack-latch-partial-1",
        request_id="ack-latch-partial-1-r",
    )
    assert acknowledged["status"] == "acknowledged", acknowledged
    assert (await rig.facade.snapshot(principal=OPERATOR))["active_stops"] == []


async def test_snapshot_quality_projection_never_masks_bad_telemetry(api: Any) -> None:
    degraded = {**good_quality(), "battery_watts": "bad"}
    rig = make_rig(api, telemetry={"pod-a": Telemetry("pod-a", 99.5, 0.0, degraded)})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["quality"] == "bad"


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ({**good_quality(), "battery_watts": "stale"}, "degraded"),
        ({**good_quality(), "battery_watts": "suspect"}, "degraded"),
        ({**good_quality(), "soh_pct": "stale", "temperatures_c": "suspect"}, "degraded"),
        ({**good_quality(), "battery_watts": "bad", "soh_pct": "stale"}, "bad"),
    ],
    ids=["stale_field", "suspect_field", "mixed_degraded_fields", "bad_dominates_degraded"],
)
async def test_snapshot_quality_never_reports_good_over_degraded_fields(
    api: Any, quality: Mapping[str, str], expected: str
) -> None:
    rig = make_rig(api, telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, quality)})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["quality"] == expected


async def test_snapshot_quality_never_reports_good_for_age_stale_telemetry(api: Any) -> None:
    rig = make_rig(api, telemetry={"pod-a": Telemetry("pod-a", 0.0, 100.0, good_quality())})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["telemetry_age_s"] == 100.0
    assert units["pod-a"]["quality"] != "good", "age-stale telemetry is never good"


async def test_snapshot_never_consumes_single_use_authorizations(api: Any) -> None:
    capability = Capability("pod-a", "discharge", 700)
    rig = make_rig(api, capabilities={"pod-a": capability})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["authorized_power"] == {"direction": "discharge", "watts": 700}
    still_current = await rig.authorizations.current("pod-a", rig.clock.monotonic())
    assert still_current is capability, "a snapshot read must not burn a single-use capability"


async def test_snapshot_peek_does_not_consume_while_current_does(api: Any) -> None:
    capability = Capability("pod-a", "discharge", 700)
    rig = make_rig(api, capabilities={"pod-a": capability})

    first = await rig.facade.snapshot(principal=OPERATOR)
    second = await rig.facade.snapshot(principal=OPERATOR)

    for snapshot in (first, second):
        units = {unit["unit_id"]: unit for unit in snapshot["units"]}
        assert units["pod-a"]["authorized_power"] == {"direction": "discharge", "watts": 700}
    consumed = await rig.authorizations.current("pod-a", rig.clock.monotonic())
    assert consumed is capability, "current() remains the single-use control read"
    after_consumption = await rig.facade.snapshot(principal=OPERATOR)
    units = {unit["unit_id"]: unit for unit in after_consumption["units"]}
    assert units["pod-a"]["authorized_power"] is None


async def test_snapshot_projects_only_currently_valid_capabilities(api: Any) -> None:
    rig = make_rig(
        api,
        capabilities={
            # API_CONTRACTS peek: only a capability whose not-before is
            # satisfied and which is unexpired projects as live authority.
            "pod-a": Capability("pod-a", "discharge", 700, expires_at_mono=99.0),
            "pod-b": Capability("pod-b", "charge", 500, not_before_mono=110.0),
        },
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["authorized_power"] is None, "an expired capability is not authority"
    assert units["pod-b"]["authorized_power"] is None, "a not-yet-valid capability is not authority"


async def test_snapshot_requested_power_follows_the_newest_active_intent(api: Any) -> None:
    rig = make_rig(
        api,
        seeded_intents=(
            manual_intent(api, revision=5, watts=800),
            manual_intent(api, revision=7, watts=600, direction="charge"),
            manual_intent(api, revision=9, watts=900, accepted_at_mono=10.0, duration_s=5.0),
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["requested_power"] == {"direction": "charge", "watts": 600}
    assert units["pod-b"]["requested_power"] == {"direction": "idle", "watts": 0}


# --- snapshot per-unit intent figures (2026-08-24 console cold-load fix) -----
#
# API_CONTRACTS "Application service facade": the snapshot's top-level
# ``intent`` view carries the LIVE request's per-unit figures so a cold page
# load mid-intent renders exact per-battery numbers instead of a labeled fleet
# total.  Composed across ALL active intents under per-unit arbitration: each
# unit's entry comes from THAT unit's winning intent, and the authorized map
# mirrors the freshest control-decision row.  Null when no live intent claims
# any unit.


def decision_row(*, sequence: int, authorized: Mapping[str, int] | None) -> SimpleNamespace:
    """One durable control_decision row as the audit port returns them."""
    return SimpleNamespace(
        sequence=sequence,
        event_type="control_decision",
        authorized_watts_by_unit=None if authorized is None else dict(authorized),
    )


def per_unit_intent(
    api: Any,
    *,
    revision: int,
    targets: Mapping[str, int],
    direction: str = "discharge",
    source: str = "manual",
) -> Any:
    """One live intent carrying explicit per-unit watt targets."""
    units = frozenset(targets)
    return api.PowerIntent(
        id=f"intent-{revision}",
        source=api.IntentSource(source),
        selected_unit_ids=units,
        direction=api.Direction(direction),
        watts=sum(targets.values()),
        watts_by_unit=dict(targets),
        duration_s=60.0,
        accepted_at_mono=90.0,
        acceptance_revision=revision,
        actor_identity="person:operator",
    )


async def test_snapshot_intent_view_is_null_without_any_live_intent(api: Any) -> None:
    """No live claimant: the whole intent view is null and the audit trail --
    the authorized map's only source -- is not even read."""
    rig = make_rig(
        api,
        audit_events=(decision_row(sequence=3, authorized={"pod-a": 100}),),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] is None
    assert rig.audit.recent_calls == [], "no live claimant means no decision scan"


async def test_snapshot_intent_view_expires_with_the_intent(api: Any) -> None:
    rig = make_rig(
        api,
        seeded_intents=(
            manual_intent(api, revision=3, watts=900, accepted_at_mono=90.0, duration_s=5.0),
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] is None, "an expired intent claims no unit"


async def test_snapshot_intent_view_splits_a_scalar_fleet_total_per_unit(api: Any) -> None:
    """A scalar intent's fleet total becomes the exact integer share over its
    surviving scope (largest remainder, ties by unit id) -- never the fleet
    total repeated once per covered unit, which is what forced the console's
    labeled-total fallback."""
    rig = make_rig(
        api,
        units={"pod-a": {}, "pod-b": {}, "pod-c": {}},
        seeded_intents=(
            manual_intent(
                api,
                revision=4,
                watts=1000,
                unit_ids=frozenset({"pod-a", "pod-b", "pod-c"}),
            ),
        ),
        audit_events=(
            decision_row(sequence=9, authorized={"pod-a": 334, "pod-b": 333, "pod-c": 333}),
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] == {
        "requested_watts_by_unit": {"pod-a": 334, "pod-b": 333, "pod-c": 333},
        "authorized_watts_by_unit": {"pod-a": 334, "pod-b": 333, "pod-c": 333},
        "directions_by_unit": {
            "pod-a": "discharge",
            "pod-b": "discharge",
            "pod-c": "discharge",
        },
    }
    # Compatibility: the per-unit scalar keeps projecting the intent's own
    # fleet total exactly as before; only the new top-level view is per-unit.
    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["requested_power"] == {"direction": "discharge", "watts": 1000}


async def test_snapshot_intent_view_carries_exact_per_unit_targets(api: Any) -> None:
    """A watts_by_unit intent shows each battery's own target exactly, even
    before any decision has landed (authorized stays null, never fabricated)."""
    rig = make_rig(
        api,
        seeded_intents=(per_unit_intent(api, revision=6, targets={"pod-a": 700, "pod-b": 300}),),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] == {
        "requested_watts_by_unit": {"pod-a": 700, "pod-b": 300},
        "authorized_watts_by_unit": None,
        "directions_by_unit": {"pod-a": "discharge", "pod-b": "discharge"},
    }


async def test_snapshot_intent_view_composes_each_units_own_winner(api: Any) -> None:
    """Two concurrent live intents on disjoint units (the operator's
    2026-08-24 scenario: one battery charging while another discharges): every
    unit's entry -- requested figure AND direction -- comes from ITS OWN
    winning intent, one scalar and one per-unit, composed into one view."""
    rig = make_rig(
        api,
        seeded_intents=(
            manual_intent(
                api,
                revision=4,
                watts=500,
                direction="charge",
                unit_ids=frozenset({"pod-a"}),
            ),
            per_unit_intent(
                api,
                revision=2,
                targets={"pod-b": 400},
                direction="discharge",
                source="optimizer",
            ),
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] == {
        "requested_watts_by_unit": {"pod-a": 500, "pod-b": 400},
        "authorized_watts_by_unit": None,
        "directions_by_unit": {"pod-a": "charge", "pod-b": "discharge"},
    }


async def test_snapshot_intent_view_mirrors_the_freshest_decision_row(api: Any) -> None:
    """The authorized map is the FRESHEST control-decision row's per-unit
    figures, restricted to the units a live intent still claims: newer
    non-decision rows are skipped, older decisions never override, and an
    ended request's units never linger as ghosts."""
    rig = make_rig(
        api,
        seeded_intents=(manual_intent(api, revision=3, watts=900),),
        audit_events=(
            SimpleNamespace(sequence=30, event_type="intent_accepted"),
            decision_row(sequence=29, authorized={"pod-a": 600}),
            decision_row(sequence=12, authorized={"pod-a": 111, "pod-b": 999}),
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"]["requested_watts_by_unit"] == {"pod-a": 900}
    assert snapshot["intent"]["authorized_watts_by_unit"] == {"pod-a": 600}


async def test_snapshot_intent_view_survives_an_unreadable_audit_trail(api: Any) -> None:
    """A degraded audit read leaves the authorized map null -- the requested
    targets still render; nothing is fabricated."""
    rig = make_rig(api, seeded_intents=(manual_intent(api, revision=3, watts=900),))
    rig.audit.failing = True

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["intent"] == {
        "requested_watts_by_unit": {"pod-a": 900},
        "authorized_watts_by_unit": None,
        "directions_by_unit": {"pod-a": "discharge"},
    }


async def test_snapshot_is_read_only_and_never_triggers_control(api: Any) -> None:
    rig = make_rig(api)

    await rig.facade.snapshot(principal=OPERATOR)

    assert rig.audit.appended == []
    assert rig.bus.published == []
    assert rig.intents.added == []
    assert rig.authorizations.published == []
    assert rig.history == []


# --- snapshot telemetry summary (API_CONTRACTS facade amendment) -------------


async def test_snapshot_telemetry_summary_projects_the_decoded_fleet(api: Any) -> None:
    rig = make_rig(
        api,
        units={"MID": {}, "RHS": {}, "LHS": {}},
        telemetry={
            "MID": mid_observation(api),
            "RHS": rhs_observation(api),
            "LHS": lhs_observation(api),
        },
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["MID"]["telemetry"] == {
        "soc_pct": 10.0,
        "bms_soc_pct": 10.0,
        "soh_pct": 99.0,
        "pack_voltage_v": 192.4,
        "pack_current_a": 12.5,
        "battery_watts": 2405.0,
        "dynamic_charge_limit_w": 2500.0,
        "dynamic_discharge_limit_w": 3000.0,
        "cell_count": 60,
        "cell_min_v": 3.205,
        "cell_max_v": 3.209,
        "cell_spread_mv": pytest.approx(4.0),
        "temperature_min_c": 23.0,
        "temperature_max_c": 28.0,
        "active_faults": [],
        "active_warnings": ["DCDC_Warning0_1", "PCS_Warning0_1"],
        "grid_power_w": -1736.0,
        "load_power_w": 1701.0,
        "debug_mode_w": None,
        "ctrl_mode_w": None,
        "work_mode_w": None,
        "run_mode_w": None,
    }
    assert units["RHS"]["telemetry"] == {
        "soc_pct": 68.0,
        "bms_soc_pct": 68.0,
        "soh_pct": 98.0,
        "pack_voltage_v": 164.5,
        "pack_current_a": 7.25,
        "battery_watts": 1192.0,
        "dynamic_charge_limit_w": 2000.0,
        "dynamic_discharge_limit_w": 2500.0,
        "cell_count": 50,
        "cell_min_v": 3.289,
        "cell_max_v": 3.292,
        "cell_spread_mv": pytest.approx(3.0),
        "temperature_min_c": 25.0,
        "temperature_max_c": 26.5,
        "active_faults": [],
        "active_warnings": ["DCDC_Warning0_1", "PCS_Warning0_1"],
        "grid_power_w": -37.0,
        "load_power_w": 1063.0,
        "debug_mode_w": None,
        "ctrl_mode_w": None,
        "work_mode_w": None,
        "run_mode_w": None,
    }
    assert units["LHS"]["telemetry"] == {
        "soc_pct": 48.0,
        "bms_soc_pct": 48.0,
        "soh_pct": 97.0,
        "pack_voltage_v": 196.8,
        "pack_current_a": 0.0,
        "battery_watts": 0.0,
        "dynamic_charge_limit_w": 2800.0,
        "dynamic_discharge_limit_w": 2800.0,
        "cell_count": 60,
        "cell_min_v": 3.310,
        "cell_max_v": 3.314,
        "cell_spread_mv": pytest.approx(4.0),
        "temperature_min_c": 22.5,
        "temperature_max_c": 23.5,
        "active_faults": [],
        "active_warnings": ["DCDC_Warning0_1", "PCS_Warning0_1"],
        # LHS carries no advisory CT words in this fixture: an unserved PCS
        # block projects null readthrough, never a fabricated zero.
        "grid_power_w": None,
        "load_power_w": None,
        "debug_mode_w": None,
        "ctrl_mode_w": None,
        "work_mode_w": None,
        "run_mode_w": None,
    }
    # A genuinely measured zero stays zero; it is never promoted to a value.
    assert units["LHS"]["measured_watts"] == 0.0


async def test_snapshot_telemetry_summary_is_null_without_any_observation(
    api: Any,
) -> None:
    rig = make_rig(api, units={"MID": {}, "RHS": {}}, telemetry={"MID": mid_observation(api)})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["MID"]["telemetry"] is not None
    silent = units["RHS"]
    assert silent["telemetry"] is None, "no observation means no summary, never zeros"
    assert silent["telemetry_age_s"] is None
    assert silent["measured_watts"] is None


async def test_snapshot_telemetry_summary_renders_nulls_never_zeros_for_partial_observations(
    api: Any,
) -> None:
    # A partial observation: capture time, watts, and quality only.  Every
    # datum it lacks must surface as null, never as a fabricated zero.
    partial = Telemetry("MID", 99.5, 1234.0, good_quality())
    rig = make_rig(api, units={"MID": {}}, telemetry={"MID": partial})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    telemetry = units["MID"]["telemetry"]
    assert telemetry is not None
    for field in TELEMETRY_SUMMARY_FIELDS:
        if field == "battery_watts":
            continue
        assert telemetry[field] is None, f"{field} must be null for a partial observation"
    assert telemetry["battery_watts"] == 1234.0


async def test_snapshot_telemetry_summary_nulls_derived_stats_for_empty_arrays(
    api: Any,
) -> None:
    empty_cells = api.Observation(
        unit_id="MID",
        connection_epoch=3,
        wall_timestamp=datetime(2026, 8, 22, 0, 4, 5, tzinfo=UTC),
        captured_at_mono=99.5,
        sequence=41,
        lifecycle=api.UnitLifecycle.DISARMED,
        protocol_profile="iot-v1",
        system_soc_pct=10.0,
        bms_soc_pct=None,
        soh_pct=None,
        battery_watts=None,
        pack_voltage_v=192.4,
        pack_current_a=None,
        dynamic_charge_limit_w=None,
        dynamic_discharge_limit_w=None,
        cell_voltages_v=(),
        temperatures_c=(),
        active_faults=frozenset(),
        active_warnings=frozenset(FLEET_WARNINGS),
        quality={field: api.DataQuality.GOOD for field in QUALITY_FIELDS},
    )
    rig = make_rig(api, units={"MID": {}}, telemetry={"MID": empty_cells})

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    telemetry = units["MID"]["telemetry"]
    assert telemetry is not None
    assert telemetry["soc_pct"] == 10.0
    assert telemetry["pack_voltage_v"] == 192.4
    for field in (
        "cell_count",
        "cell_min_v",
        "cell_max_v",
        "cell_spread_mv",
        "temperature_min_c",
        "temperature_max_c",
    ):
        assert telemetry[field] is None, f"{field} must be null for empty arrays, never zero"


# --- unit_detail ---------------------------------------------------------------


async def test_unit_detail_returns_the_full_latest_observation_projection(api: Any) -> None:
    observation = mid_observation(api)
    rig = make_rig(api, units={"MID": {}, "RHS": {}}, telemetry={"MID": observation})

    detail = await rig.facade.unit_detail(principal=OPERATOR, unit_id="MID")

    assert detail["unit_id"] == "MID"
    assert detail["device_identity"] == MID_IDENTITY
    assert detail["protocol_profile"] == "iot-v1"
    assert detail["connection_epoch"] == 3
    assert detail["lifecycle"] == "disarmed"
    assert detail["sequence"] == 41
    assert detail["captured_at_mono"] == 99.5
    assert detail["cell_sequence"] == 12
    assert detail["cell_captured_at_mono"] == 98.0
    assert detail["wall_timestamp"] == observation.wall_timestamp.isoformat()
    # The summary scalars are carried unchanged into the detail view.
    for field, value in (
        ("soc_pct", 10.0),
        ("bms_soc_pct", 10.0),
        ("soh_pct", 99.0),
        ("pack_voltage_v", 192.4),
        ("pack_current_a", 12.5),
        ("battery_watts", 2405.0),
        ("dynamic_charge_limit_w", 2500.0),
        ("dynamic_discharge_limit_w", 3000.0),
        ("cell_count", 60),
        ("cell_min_v", 3.205),
        ("cell_max_v", 3.209),
        ("temperature_min_c", 23.0),
        ("temperature_max_c", 28.0),
    ):
        assert detail[field] == value
    assert detail["cell_spread_mv"] == pytest.approx(4.0)
    assert detail["cell_voltages_v"] == list(observation.cell_voltages_v)
    assert len(detail["cell_voltages_v"]) == 60
    assert detail["temperatures_c"] == list(observation.temperatures_c)
    assert detail["quality"] == {field: "good" for field in QUALITY_FIELDS}
    assert detail["active_faults"] == []
    assert detail["active_warnings"] == ["DCDC_Warning0_1", "PCS_Warning0_1"]
    # The detail reads exactly one unit's latest observation.
    assert rig.observations.calls == ["latest:MID"]


async def test_unit_detail_for_a_known_unit_without_observations_projects_nulls(
    api: Any,
) -> None:
    rig = make_rig(api, units={"MID": {}, "RHS": {}}, telemetry={"MID": mid_observation(api)})

    detail = await rig.facade.unit_detail(principal=OPERATOR, unit_id="RHS")

    assert detail["unit_id"] == "RHS"
    for field in (
        "device_identity",
        "protocol_profile",
        "connection_epoch",
        "lifecycle",
        "sequence",
        "captured_at_mono",
        "cell_sequence",
        "cell_captured_at_mono",
        "wall_timestamp",
        "cell_voltages_v",
        "temperatures_c",
        "quality",
        *TELEMETRY_SUMMARY_FIELDS,
    ):
        assert detail[field] is None, f"{field} must be null before the first observation"
    assert rig.observations.calls == ["latest:RHS"]


async def test_unit_detail_refuses_unknown_units_without_reading_the_store(
    api: Any,
) -> None:
    rig = make_rig(api, units={"MID": {}}, telemetry={"MID": mid_observation(api)})

    with pytest.raises(LookupError):
        await rig.facade.unit_detail(principal=OPERATOR, unit_id="pod-ghost")

    assert rig.observations.calls == []
    with pytest.raises(ValueError):
        await rig.facade.unit_detail(principal=OPERATOR, unit_id="not canonical!")


async def test_unit_detail_requires_the_observe_scope(api: Any) -> None:
    rig = make_rig(api, telemetry={"MID": mid_observation(api)})
    sightless = replace(OPERATOR, scopes=frozenset({"dispatch"}))

    with pytest.raises(PermissionError):
        await rig.facade.unit_detail(principal=sightless, unit_id="MID")

    assert rig.recorder_activity() == []


async def test_unit_detail_is_a_pure_read_of_repository_state(api: Any) -> None:
    rig = make_rig(api, units={"MID": {}}, telemetry={"MID": mid_observation(api)})

    await rig.facade.unit_detail(principal=OPERATOR, unit_id="MID")

    assert rig.audit.appended == []
    assert rig.bus.published == []
    assert rig.intents.added == []
    assert rig.authorizations.published == []
    assert rig.authorizations.peek_calls == []
    assert rig.history == []


# --- health -----------------------------------------------------------------


async def test_health_separates_liveness_service_and_control_readiness(api: Any) -> None:
    disarmed = make_rig(api)

    report = await disarmed.facade.health(principal=OPERATOR)

    assert report["liveness"]["ok"] is True
    assert report["service_readiness"]["ready"] is True
    assert report["service_readiness"]["reasons"] == []
    control = report["control_readiness"]
    assert control["ready"] is False
    assert control["reasons"]
    assert all(isinstance(reason, str) and reason for reason in control["reasons"])

    armed = make_rig(api, units={"pod-a": {"lifecycle": api.UnitLifecycle.ARMED_IDLE}, "pod-b": {}})
    armed_report = await armed.facade.health(principal=OPERATOR)
    assert armed_report["liveness"]["ok"] is True
    assert armed_report["control_readiness"]["ready"] is True
    assert armed_report["control_readiness"]["reasons"] == []
    assert armed.audit.appended == []
    assert armed.bus.published == []
    assert armed.history == []


@pytest.mark.parametrize(
    "deformation",
    [{"qualified": None}, {"qualified": False}],
    ids=["state_unknown", "not_qualified"],
)
async def test_health_never_fabricates_control_readiness(api: Any, deformation: dict) -> None:
    rig = make_rig(
        api,
        units={"pod-a": {"lifecycle": api.UnitLifecycle.ARMED_IDLE}, "pod-b": deformation},
    )

    report = await rig.facade.health(principal=OPERATOR)

    control = report["control_readiness"]
    assert control["ready"] is False
    assert any("pod-b" in reason for reason in control["reasons"])


async def test_health_reports_unready_repositories_without_crashing(api: Any) -> None:
    rig = make_rig(api)
    rig.intents.failing = True
    rig.observations.failing = True
    rig.authorizations.failing = True
    rig.audit.failing = True

    report = await rig.facade.health(principal=OPERATOR)

    assert report["liveness"]["ok"] is True
    assert report["service_readiness"]["ready"] is False
    assert report["service_readiness"]["reasons"]
    assert report["control_readiness"]["ready"] is False


async def test_health_reports_an_unresponsive_generation_coordinator(api: Any) -> None:
    rig = make_rig(api)
    rig.coordinator.failing = True

    report = await rig.facade.health(principal=OPERATOR)

    assert report["liveness"]["ok"] is True
    assert report["service_readiness"]["ready"] is False
    assert report["service_readiness"]["reasons"]


# --- recovery health views (self-healing awareness layer, R4) --------------------


async def test_snapshot_carries_the_derived_health_state_per_unit(api: Any) -> None:
    """The recovery monitor's derived per-unit state rides the snapshot: a
    quietly self-healing unit names its reason with no remediation, and a
    not-responding wedge carries the R5 honest-terminal guidance."""
    recovery = FakeRecoveryView(
        {
            "pod-a": {
                "state": "self_healing",
                "reasons": ["autonomous_self_charge"],
                "remediation_hint": None,
            },
            "pod-b": {
                "state": "not_responding",
                "reasons": ["reads_timing_out"],
                "remediation_hint": (
                    "pod not responding — remote recovery exhausted; physical restart required"
                ),
            },
        }
    )
    rig = make_rig(api, recovery=recovery)

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["health_state"] == "self_healing"
    assert units["pod-a"]["health_reasons"] == ["autonomous_self_charge"]
    assert units["pod-a"]["remediation_hint"] is None
    assert units["pod-b"]["health_state"] == "not_responding"
    assert units["pod-b"]["health_reasons"] == ["reads_timing_out"]
    assert units["pod-b"]["remediation_hint"] is not None
    assert "physical restart" in units["pod-b"]["remediation_hint"]
    # The projection is a read: no mutation, audit, or publication happened.
    assert rig.audit.appended == []
    assert rig.bus.published == []


async def test_health_view_carries_per_unit_recovery_states(api: Any) -> None:
    """/api/v1/health serves the same derived recovery view per unit, with the
    remediation hint alongside -- the console renders it later, feature-
    detected, straight off the health read."""
    recovery = FakeRecoveryView(
        {
            "pod-a": {"state": "healthy", "reasons": [], "remediation_hint": None},
            "pod-b": {
                "state": "actuation_incoherent",
                "reasons": ["authorized_not_actuating", "echo_matches_write"],
                "remediation_hint": "pod not responding — physical restart required",
            },
        }
    )
    rig = make_rig(api, recovery=recovery)

    report = await rig.facade.health(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in report["units"]}
    assert set(units) == {"pod-a", "pod-b"}
    assert units["pod-a"] == {
        "unit_id": "pod-a",
        "health_state": "healthy",
        "reasons": [],
        "remediation_hint": None,
        # API_CONTRACTS "Night-writer detector": the per-unit last-objective
        # summary rides the health view too (null until a sample records).
        "last_objective_observed": None,
    }
    assert units["pod-b"]["health_state"] == "actuation_incoherent"
    assert units["pod-b"]["reasons"] == ["authorized_not_actuating", "echo_matches_write"]
    assert units["pod-b"]["remediation_hint"] is not None


async def test_control_readiness_names_an_actuation_incoherent_unit(api: Any) -> None:
    """The watchdog's verdict is a control-readiness reason: a unit that is
    armed and qualified but authorizing without actuating is not ready to
    act, and the health view says which unit and why."""
    recovery = FakeRecoveryView(
        {
            "pod-a": {
                "state": "actuation_incoherent",
                "reasons": ["authorized_not_actuating"],
                "remediation_hint": None,
            }
        }
    )
    rig = make_rig(
        api,
        units={"pod-a": {"lifecycle": api.UnitLifecycle.ARMED_IDLE}, "pod-b": {}},
        recovery=recovery,
    )

    report = await rig.facade.health(principal=OPERATOR)

    assert "pod-a:actuation_incoherent" in report["control_readiness"]["reasons"]
    assert report["control_readiness"]["ready"] is False


async def test_recovery_fields_are_null_when_no_monitor_is_wired(api: Any) -> None:
    """A facade driven without the recovery port (embedded tests, older rigs)
    serves explicit nulls, never fabricated states."""
    rig = make_rig(api)

    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    report = await rig.facade.health(principal=OPERATOR)

    for unit in snapshot["units"]:
        assert unit["health_state"] is None
        assert unit["health_reasons"] is None
        assert unit["remediation_hint"] is None
    assert {unit["unit_id"] for unit in report["units"]} == set(rig.handles)
    for unit in report["units"]:
        assert unit["health_state"] is None
        assert unit["reasons"] is None
        assert unit["remediation_hint"] is None


# --- night-writer detector views (API_CONTRACTS "Night-writer detector") ----------


class FakeForeignObjectiveView:
    """Inline stand-in for the composed detector's read projection.

    Serves the pinned payload shapes so the facade's snapshot/health
    projections and the observed-objectives read can be pinned without
    composing the monitor itself.
    """

    def __init__(
        self,
        last_by_unit: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        window: Mapping[str, Any] | None = None,
        failing: bool = False,
    ) -> None:
        self.last_by_unit = dict(last_by_unit or {})
        self.window = dict(
            window
            if window is not None
            else {
                "as_of": "2026-08-26T22:30:00+00:00",
                "last": "24h",
                "window_s": 86400,
                "units": [
                    {
                        "unit_id": unit_id,
                        "first_seen_at": None,
                        "last_seen_at": None,
                        "sample_count": 0,
                        "charge_sample_count": 0,
                        "discharge_sample_count": 0,
                        "min_active_w": None,
                        "typical_active_w": None,
                        "max_active_w": None,
                        "foreign_episode_count": 0,
                        "foreign_active": False,
                        "foreign_reason": None,
                        "last_objective_observed": self.last_by_unit.get(unit_id),
                    }
                    for unit_id in sorted(self.last_by_unit)
                ],
            }
        )
        self.failing = failing
        self.window_calls: list[int] = []

    def unit_last_observed(self, unit_id: str) -> Any:
        if self.failing:
            raise OSError("objective view unavailable")
        return self.last_by_unit.get(unit_id)

    def window_payload(self, *, last_hours: int) -> dict[str, Any]:
        self.window_calls.append(last_hours)
        if self.failing:
            raise OSError("objective view unavailable")
        return dict(self.window)


_MID_LAST_OBSERVED = {
    "observed_at": "2026-08-26T22:29:31+00:00",
    "active_w": -2400,
    "reactive_var": 0,
    "classification": "foreign_objective_observed",
    "reason": "sustained_charge_without_pv_evidence",
}


async def test_snapshot_carries_the_last_objective_summary_per_unit(api: Any) -> None:
    """The detector composes ALWAYS, so every snapshot unit carries the COMPACT
    ``last_objective_observed`` summary -- five pinned keys, null before any
    sample, never the full evidence record (that is the endpoint's shape)."""
    rig = make_rig(
        api,
        objectives=FakeForeignObjectiveView(
            {"pod-a": dict(_MID_LAST_OBSERVED), "pod-b": None},
        ),
    )

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["last_objective_observed"] == _MID_LAST_OBSERVED
    assert units["pod-b"]["last_objective_observed"] is None
    assert rig.audit.appended == [] and rig.bus.published == []


async def test_health_view_carries_the_same_last_objective_summary(api: Any) -> None:
    rig = make_rig(api, objectives=FakeForeignObjectiveView({"pod-a": dict(_MID_LAST_OBSERVED)}))

    report = await rig.facade.health(principal=OPERATOR)

    units = {unit["unit_id"]: unit for unit in report["units"]}
    assert units["pod-a"]["last_objective_observed"] == _MID_LAST_OBSERVED
    assert units["pod-b"]["last_objective_observed"] is None


async def test_objective_fields_are_null_when_the_detector_is_not_wired(api: Any) -> None:
    """An uncomposed detector (embedded tests, older rigs) degrades to explicit
    nulls on both views -- never fabricated evidence."""
    rig = make_rig(api)

    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    report = await rig.facade.health(principal=OPERATOR)

    for unit in snapshot["units"]:
        assert unit["last_objective_observed"] is None
    for unit in report["units"]:
        assert unit["last_objective_observed"] is None


async def test_a_failing_objective_view_never_breaks_the_views(api: Any) -> None:
    rig = make_rig(api, objectives=FakeForeignObjectiveView({"pod-a": {}}, failing=True))

    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    report = await rig.facade.health(principal=OPERATOR)

    assert snapshot["units"][0]["last_objective_observed"] is None
    assert report["units"][0]["last_objective_observed"] is None
    assert report["service_readiness"]["ready"] is True


async def test_get_observed_objectives_serves_the_window_payload(api: Any) -> None:
    """The night-window characterization read: observe scope, the ``last``
    window parsed to hours (``24h`` -> 24), the detector's payload verbatim."""
    objectives = FakeForeignObjectiveView({"pod-a": dict(_MID_LAST_OBSERVED)})
    rig = make_rig(api, objectives=objectives)

    payload = await rig.facade.get_observed_objectives(principal=OPERATOR, last="24h")

    assert objectives.window_calls == [24]
    assert payload["window_s"] == 86400
    assert payload["units"][0]["last_objective_observed"]["active_w"] == -2400

    denied = rig.facade.get_observed_objectives(principal=VIEWER, last="24h")
    with pytest.raises(Exception, match="scope"):
        await denied


async def test_get_observed_objectives_parses_and_bounds_the_window(api: Any) -> None:
    rig = make_rig(api, objectives=FakeForeignObjectiveView({"pod-a": {}}))

    assert (await rig.facade.get_observed_objectives(principal=OPERATOR, last="6h"))[
        "window_s"
    ] == 6 * 3600
    assert (await rig.facade.get_observed_objectives(principal=OPERATOR, last="7d"))[
        "window_s"
    ] == 7 * 24 * 3600

    for bad in ("0h", "169h", "24", "24m", "", "hours"):
        with pytest.raises(ValueError, match="last"):
            await rig.facade.get_observed_objectives(principal=OPERATOR, last=bad)


async def test_get_observed_objectives_serves_nulls_when_uncomposed(api: Any) -> None:
    rig = make_rig(api)

    payload = await rig.facade.get_observed_objectives(principal=OPERATOR, last="24h")

    assert payload["last"] == "24h"
    assert {unit["unit_id"] for unit in payload["units"]} == set(rig.handles)
    for unit in payload["units"]:
        assert unit["sample_count"] == 0
        assert unit["last_objective_observed"] is None


async def test_a_failing_recovery_view_never_breaks_the_views(api: Any) -> None:
    """Detection must never gate reads: an unavailable recovery projection
    degrades to explicit nulls on both views instead of failing them."""
    recovery = FakeRecoveryView({"pod-a": {"state": "healthy"}})
    recovery.failing = True
    rig = make_rig(api, recovery=recovery)

    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    report = await rig.facade.health(principal=OPERATOR)

    assert snapshot["units"][0]["health_state"] is None
    assert report["units"][0]["health_state"] is None
    assert report["service_readiness"]["ready"] is True


# --- recent_audit -----------------------------------------------------------


async def test_recent_audit_is_bounded_newest_first_and_stable(api: Any) -> None:
    events = tuple(
        SimpleNamespace(sequence=number, event_id=f"event-{number}") for number in (9, 8, 7, 6, 5)
    )
    rig = make_rig(api, audit_events=events)

    first = await rig.facade.recent_audit(principal=OPERATOR, limit=3)
    second = await rig.facade.recent_audit(principal=OPERATOR, limit=3)

    assert [field_of(event, "sequence") for event in first["events"]] == [9, 8, 7]
    assert "next_cursor" in first
    # The cursor names the oldest delivered event so pagination can resume.
    assert first["next_cursor"] == field_of(first["events"][-1], "sequence")
    assert first["next_cursor"] == second["next_cursor"]
    assert rig.audit.recent_calls == [3, 3]
    assert rig.audit.appended == []
    assert rig.bus.published == []


async def test_recent_audit_cursor_resumes_with_strictly_older_events(api: Any) -> None:
    events = tuple(
        SimpleNamespace(sequence=number, event_id=f"event-{number}") for number in (9, 8, 7, 6, 5)
    )
    rig = make_rig(api, audit_events=events)

    first = await rig.facade.recent_audit(principal=OPERATOR, limit=3)
    older = await rig.facade.recent_audit(principal=OPERATOR, limit=3, cursor=first["next_cursor"])

    assert [field_of(event, "sequence") for event in first["events"]] == [9, 8, 7]
    assert [field_of(event, "sequence") for event in older["events"]] == [6, 5]
    assert older["next_cursor"] is None, "a short terminal page leaves no further cursor"
    assert rig.audit.appended == []
    assert rig.bus.published == []


async def test_recent_audit_over_an_empty_trail_is_terminal(api: Any) -> None:
    rig = make_rig(api)

    result = await rig.facade.recent_audit(principal=OPERATOR, limit=5)

    assert result["events"] == []
    assert result["next_cursor"] is None


async def test_recent_audit_hands_the_cursor_to_the_audit_port(api: Any) -> None:
    """The cursor is the audit port's ordering key, not facade bookkeeping."""
    events = tuple(
        SimpleNamespace(sequence=number, event_id=f"event-{number}") for number in (9, 8, 7, 6, 5)
    )
    rig = make_rig(api, audit_events=events)

    first = await rig.facade.recent_audit(principal=OPERATOR, limit=3)
    older = await rig.facade.recent_audit(principal=OPERATOR, limit=3, cursor=first["next_cursor"])

    assert rig.audit.recent_cursors == [None, first["next_cursor"]], (
        "the facade must pass after_sequence through to the audit port verbatim"
    )
    # next_cursor is derived from what the store returned, never fabricated.
    assert first["next_cursor"] == field_of(first["events"][-1], "sequence")
    assert [field_of(event, "sequence") for event in older["events"]] == [6, 5]
    assert older["next_cursor"] is None


# --- submit_intent ----------------------------------------------------------


async def test_submit_intent_assigns_monotonic_server_revisions_and_stores_intents(
    api: Any,
) -> None:
    rig = make_rig(api)

    first = await rig.facade.submit_intent(**submit_kwargs())
    second = await rig.facade.submit_intent(
        **submit_kwargs(idempotency_key="intent-key-2", request_id="request-2")
    )
    third = await rig.facade.submit_intent(
        **submit_kwargs(idempotency_key="intent-key-3", request_id="request-3")
    )
    # Idempotency is the adapter's concern: the facade accepts domain input again.
    replay = await rig.facade.submit_intent(**submit_kwargs())

    revisions = [
        first["acceptance_revision"],
        second["acceptance_revision"],
        third["acceptance_revision"],
        replay["acceptance_revision"],
    ]
    assert revisions[0] < revisions[1] < revisions[2] < revisions[3]
    assert len(rig.intents.added) == 4
    for index, view in enumerate((first, second, third, replay)):
        stored = rig.intents.added[index]
        assert stored.id == view["intent_id"]
        assert stored.acceptance_revision == view["acceptance_revision"]
    stored = rig.intents.added[0]
    assert stored.source is not api.IntentSource.EMERGENCY_STOP
    assert stored.direction is api.Direction.DISCHARGE
    assert stored.selected_unit_ids == frozenset({"pod-a"})
    assert stored.watts == 900
    assert stored.duration_s == 30.0
    assert stored.accepted_at_mono == rig.clock.monotonic()
    assert stored.actor_identity == OPERATOR.subject


async def test_submit_intent_returns_the_acceptance_view_and_never_grants_authority(
    api: Any,
) -> None:
    rig = make_rig(api)

    view = await rig.facade.submit_intent(**submit_kwargs())

    assert canonical(view["intent_id"])
    assert type(view["acceptance_revision"]) is int
    assert view["acceptance_revision"] >= 0
    assert view["accepted_at_monotonic"] == rig.clock.monotonic()
    assert view["status"] == "accepted"
    assert view["requested"] == {"direction": "discharge", "watts": 900}
    assert view["authorized"] is None
    assert view["measured"] is None
    assert view["expires_in_s"] == 30.0
    assert rig.authorizations.published == [], "only the kernel tick may publish authorization"
    assert rig.authorizations.current_calls == []
    assert rig.authorizations.peek_calls == []
    # Acceptance is a facade mutation: audited and published per API_CONTRACTS,
    # while every authorization port stays untouched (never grants authority).
    assert_audited_and_published(rig, OPERATOR.subject)


async def test_submit_intent_is_audited_and_published(api: Any) -> None:
    rig = make_rig(api)

    await rig.facade.submit_intent(**submit_kwargs())

    assert_audited_and_published(rig, OPERATOR.subject)


@pytest.mark.parametrize(
    "overrides",
    [
        {"watts": 0},
        {"direction": "sideways"},
        {"unit_ids": []},
        {"ttl_s": 0.0},
        {"unit_ids": ["pod-ghost"]},
    ],
    ids=["zero_watts", "unknown_direction", "no_units", "zero_ttl", "unknown_unit"],
)
async def test_submit_intent_rejects_invalid_payloads_without_storing(
    api: Any, overrides: dict
) -> None:
    rig = make_rig(api)

    with pytest.raises(ValueError):
        await rig.facade.submit_intent(**submit_kwargs(**overrides))

    assert rig.intents.added == []


@pytest.mark.parametrize(
    "degradation",
    ["audit_append_fails", "publish_fails"],
    ids=["audit_failure", "publish_failure"],
)
async def test_submit_intent_is_atomic_with_its_audit_and_publication(
    api: Any, degradation: str
) -> None:
    """A dispatch the caller saw fail must leave nothing stored to arbitrate.

    The next kernel tick would otherwise authorize power from an intent whose
    acceptance was never reported, and the operator's retry would store a
    duplicate.
    """
    rig = make_rig(api)
    if degradation == "audit_append_fails":
        rig.audit.failing = True
    else:
        rig.bus.failing = True

    with pytest.raises(OSError):
        await rig.facade.submit_intent(**submit_kwargs())

    assert len(rig.intents.added) == 1, "the intent was committed before the failure"
    assert rig.intents.removed == [rig.intents.added[0].id], (
        "the failed acceptance must roll the stored intent back"
    )
    live = await rig.intents.active(rig.clock.now)
    assert not live, "power can never flow from a dispatch the caller saw fail"


# --- submit_intent with per-unit watt targets --------------------------------


async def test_submit_intent_with_watts_by_unit_derives_the_fleet_total(api: Any) -> None:
    """The 2026-08-23 operator ruling: each setting is that battery's request.

    One dispatch names a different watt target per battery; the facade derives
    the fleet total as the sum, stores both on the intent, and carries the
    per-unit breakdown into the audit fact set and the published event.
    """
    rig = make_rig(api)
    view = await rig.facade.submit_intent(
        **submit_kwargs(
            unit_ids=["pod-a", "pod-b"],
            watts=None,
            watts_by_unit={"pod-a": 900, "pod-b": 600},
        )
    )
    assert view["requested"] == {
        "direction": "discharge",
        "watts": 1_500,
        "watts_by_unit": {"pod-a": 900, "pod-b": 600},
    }
    (stored,) = rig.intents.added
    assert stored.watts == 1_500
    assert dict(stored.watts_by_unit) == {"pod-a": 900, "pod-b": 600}
    assert stored.selected_unit_ids == frozenset({"pod-a", "pod-b"})
    assert_audited_and_published(rig, OPERATOR.subject)
    (published,) = rig.bus.published
    assert published["payload"]["watts_by_unit"] == {"pod-a": 900, "pod-b": 600}
    assert published["payload"]["watts"] == 1_500


async def test_submit_intent_per_unit_breakdown_reaches_the_audit_fact_set(api: Any) -> None:
    """The intent_accepted audit fingerprint is computed over the breakdown.

    Two dispatches over the same unit with the same total watts -- one scalar,
    one per-unit -- must never share a request fingerprint.
    """
    per_unit = make_rig(api)
    await per_unit.facade.submit_intent(
        **submit_kwargs(unit_ids=["pod-a"], watts=None, watts_by_unit={"pod-a": 900})
    )
    scalar = make_rig(api)
    await scalar.facade.submit_intent(**submit_kwargs(unit_ids=["pod-a"], watts=900))
    (per_unit_event,) = per_unit.audit.appended
    (scalar_event,) = scalar.audit.appended
    assert per_unit_event.event_type == scalar_event.event_type == "intent_accepted"
    assert per_unit_event.request_fingerprint != scalar_event.request_fingerprint


@pytest.mark.parametrize(
    "overrides",
    [
        {"watts": 900, "watts_by_unit": {"pod-a": 900}},
        {"watts": None, "watts_by_unit": None},
        {"unit_ids": ["pod-a", "pod-b"], "watts": None, "watts_by_unit": {"pod-a": 900}},
        {"watts": None, "watts_by_unit": {"pod-a": 0}},
        {"watts": None, "watts_by_unit": {"pod-a": -5}},
        {"watts": None, "watts_by_unit": {"pod-a": 1.5}},
        {"watts": None, "watts_by_unit": {"pod-a": True}},
        {"watts": None, "watts_by_unit": "pod-a:900"},
        {"watts": None, "watts_by_unit": {}},
    ],
    ids=[
        "both_forms",
        "neither_form",
        "key_set_mismatch",
        "zero_target",
        "negative_target",
        "float_target",
        "boolean_target",
        "non_mapping",
        "empty_mapping",
    ],
)
async def test_submit_intent_rejects_malformed_per_unit_payloads_without_storing(
    api: Any, overrides: dict
) -> None:
    rig = make_rig(api)
    with pytest.raises((TypeError, ValueError)):
        await rig.facade.submit_intent(**submit_kwargs(**overrides))
    assert rig.intents.added == []
    assert rig.audit.appended == []
    assert rig.bus.published == []


# --- arm --------------------------------------------------------------------


async def test_arm_arms_exactly_the_requested_qualified_disarmed_units(api: Any) -> None:
    rig = make_rig(api)

    result = await rig.facade.arm(
        unit_ids=["pod-a"],
        principal=OPERATOR,
        idempotency_key="arm-key-1",
        request_id="request-a",
    )

    armed = {unit["unit_id"] for unit in result["units"] if unit["status"] == "armed"}
    assert armed == {"pod-a"}
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.ARMED_IDLE
    assert rig.handles["pod-b"].lifecycle is api.UnitLifecycle.DISARMED
    assert_audited_and_published(rig, OPERATOR.subject)


async def test_arm_refusals_are_visible_per_unit_and_never_silent(api: Any) -> None:
    rig = make_rig(
        api,
        units={
            "pod-a": {},
            "pod-b": {"qualified": False},
            "pod-c": {
                "lifecycle": api.UnitLifecycle.INHIBITED,
                "qualified": False,
                "inhibit_latched": True,
            },
            "pod-d": {"arm_error": RuntimeError("mailbox wedged")},
        },
    )
    requested = ["pod-a", "pod-ghost", "pod-b", "pod-c", "pod-d"]

    result = await rig.facade.arm(
        unit_ids=requested,
        principal=OPERATOR,
        idempotency_key="arm-key-2",
        request_id="request-b",
    )

    outcomes = {unit["unit_id"]: unit for unit in result["units"]}
    assert set(outcomes) == set(requested)
    assert outcomes["pod-a"]["status"] == "armed"
    reasons = {
        "unknown_unit": outcomes["pod-ghost"]["reason"],
        "not_qualified": outcomes["pod-b"]["reason"],
        "inhibit_latched": outcomes["pod-c"]["reason"],
        "actor_failure": outcomes["pod-d"]["reason"],
    }
    assert all(isinstance(reason, str) and reason for reason in reasons.values())
    assert len(set(reasons.values())) == len(reasons), (
        "every refusal category carries a distinguishable operator reason"
    )
    assert "arm:pod-b" not in rig.history and "arm:pod-c" not in rig.history, (
        "facade-level refusals never reach the actor handle"
    )
    assert "arm:pod-d" in rig.history, "an actor failure can only be discovered by attempting"
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.ARMED_IDLE
    for unit_id in ("pod-b", "pod-c", "pod-d"):
        assert rig.handles[unit_id].lifecycle is not api.UnitLifecycle.ARMED_IDLE
    assert_audited_and_published(rig, OPERATOR.subject)


async def test_arm_refuses_unknown_qualification_without_actor_contact(api: Any) -> None:
    rig = make_rig(api, units={"pod-a": {"qualified": None}, "pod-b": {}})

    result = await rig.facade.arm(
        unit_ids=["pod-a", "pod-b"],
        principal=OPERATOR,
        idempotency_key="arm-key-3",
        request_id="request-c",
    )

    outcomes = {unit["unit_id"]: unit for unit in result["units"]}
    assert outcomes["pod-a"]["status"] == "refused"
    reason = outcomes["pod-a"]["reason"]
    assert isinstance(reason, str) and reason
    assert outcomes["pod-b"]["status"] == "armed"
    assert "arm:pod-a" not in rig.history, (
        "the handle would have accepted this unit; only the facade gate refuses it"
    )
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.DISARMED
    assert_audited_and_published(rig, OPERATOR.subject)


# --- disarm ------------------------------------------------------------------


async def test_disarm_is_the_arm_path_inverted_and_always_succeeds_for_known_units(
    api: Any,
) -> None:
    rig = make_rig(
        api,
        units={
            "pod-a": {"lifecycle": api.UnitLifecycle.ARMED_IDLE},
            "pod-b": {},
            "pod-c": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True},
        },
    )

    result = await rig.facade.disarm(
        unit_ids=["pod-a", "pod-b", "pod-c", "pod-ghost"],
        principal=OPERATOR,
        idempotency_key="disarm-key-1",
        request_id="request-d1",
    )

    outcomes = {unit["unit_id"]: unit for unit in result["units"]}
    assert set(outcomes) == {"pod-a", "pod-b", "pod-c", "pod-ghost"}
    for unit_id in ("pod-a", "pod-b", "pod-c"):
        assert outcomes[unit_id]["status"] == "disarmed", (
            "disarm is the arm path inverted: every known unit succeeds"
        )
    assert outcomes["pod-ghost"]["status"] == "refused"
    ghost_reason = outcomes["pod-ghost"]["reason"]
    assert isinstance(ghost_reason, str) and ghost_reason
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.DISARMED
    assert "disarm:pod-a" in rig.history and "disarm:pod-b" in rig.history
    assert rig.handles["pod-c"].inhibit_latched is True, (
        "disarming never acknowledges an inhibit latch"
    )
    assert_audited_and_published(rig, OPERATOR.subject)


# --- facade mutation atomicity (Impl-10 / Impl-11 / Impl-15) ----------------


async def test_arm_audit_failure_disarms_every_unit_the_request_armed(api: Any) -> None:
    """Impl-10: an arm whose audit cannot land leaves no unaudited armed unit.

    Arming has an exact inverse, so the failure compensates: the request stops
    arming at the failed append, every unit it DID arm is disarmed again, each
    compensation is its own audited ``unit_armed``/``rolled_back`` row, units
    not yet visited are never touched, and the caller sees the failure.
    """
    rig = make_rig(api, units={"pod-a": {}, "pod-b": {}, "pod-c": {}})
    rig.audit.fail_first_appends = 1

    with pytest.raises(OSError):
        await rig.facade.arm(
            unit_ids=["pod-a", "pod-b", "pod-c"],
            principal=OPERATOR,
            idempotency_key="arm-key-a1",
            request_id="request-a1",
        )

    assert rig.history.index("disarm:pod-a") > rig.history.index("arm:pod-a"), (
        "the unit that armed before the audit failed must be stood back down"
    )
    assert "arm:pod-b" not in rig.history and "arm:pod-c" not in rig.history, (
        "the request stops arming at the failed append"
    )
    for handle in rig.handles.values():
        assert handle.lifecycle is not api.UnitLifecycle.ARMED_IDLE
    (compensation,) = rig.audit.appended
    assert compensation.event_type == "unit_armed"
    assert compensation.result == "rolled_back"
    assert compensation.unit_id == "pod-a"
    assert compensation.reason_codes == ("audit_unavailable", "disarmed")
    assert rig.bus.published == [], "a failed arm publishes nothing"


async def test_arm_compensation_never_masks_the_original_failure(api: Any) -> None:
    """The compensation sweep runs even when no further audit row can land,
    and a unit whose compensating disarm fails is named on its row -- one
    wedged unit never shields another from being stood down."""
    rig = make_rig(
        api,
        units={
            "pod-a": {},
            "pod-b": {"disarm_error": RuntimeError("mailbox wedged")},
            "pod-c": {},
        },
    )
    rig.audit.failing = True

    with pytest.raises(OSError):
        await rig.facade.arm(
            unit_ids=["pod-a", "pod-b", "pod-c"],
            principal=OPERATOR,
            idempotency_key="arm-key-a2",
            request_id="request-a2",
        )

    assert [step for step in rig.history if step != "audit"] == ["arm:pod-a", "disarm:pod-a"], (
        "the failure aborts the sweep at the first unit and still compensates it"
    )
    assert rig.audit.appended == [], "a fully failed audit leaves no rows, only the raise"


async def test_arm_compensation_names_a_failed_disarm(api: Any) -> None:
    rig = make_rig(api, units={"pod-a": {"disarm_error": RuntimeError("mailbox wedged")}})
    rig.audit.fail_first_appends = 1

    with pytest.raises(OSError):
        await rig.facade.arm(
            unit_ids=["pod-a"],
            principal=OPERATOR,
            idempotency_key="arm-key-a3",
            request_id="request-a3",
        )

    (compensation,) = rig.audit.appended
    assert compensation.reason_codes == ("audit_unavailable", "disarm_failed")
    assert compensation.request_fingerprint != ""


async def test_disarm_stands_and_names_the_failed_record(api: Any) -> None:
    """Impl-10 (safety-positive family): an audit failure never undoes or
    fails a completed disarm; the response names the degraded record per unit
    and the loop keeps disarming the remaining units."""
    rig = make_rig(
        api,
        units={"pod-a": {"lifecycle": api.UnitLifecycle.ARMED_IDLE}, "pod-b": {}},
    )
    rig.audit.failing = True

    result = await rig.facade.disarm(
        unit_ids=["pod-a", "pod-b"],
        principal=OPERATOR,
        idempotency_key="disarm-key-a1",
        request_id="request-b1",
    )

    assert [unit["status"] for unit in result["units"]] == ["disarmed", "disarmed"]
    assert result["degraded"] == ["audit_unavailable:pod-a", "audit_unavailable:pod-b"]
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.DISARMED
    assert rig.handles["pod-b"].lifecycle is api.UnitLifecycle.DISARMED
    assert "disarm:pod-a" in rig.history and "disarm:pod-b" in rig.history


async def test_disarm_publish_failure_degrades_without_failing(api: Any) -> None:
    rig = make_rig(api)
    rig.bus.failing = True

    result = await rig.facade.disarm(
        unit_ids=["pod-a"],
        principal=OPERATOR,
        idempotency_key="disarm-key-a2",
        request_id="request-b2",
    )

    assert result["degraded"] == ["publish_unavailable"]
    assert len(rig.audit.appended) == 1, "the audit row stands"


async def test_cancel_intent_stands_and_names_the_failed_record(api: Any) -> None:
    """Impl-10 (safety-positive family): a cancellation whose audit fails has
    still removed the intent; the response says cancelled + degraded and the
    kernel's next tick finds nothing to arbitrate."""
    rig = make_rig(api)
    accepted = await rig.facade.submit_intent(**submit_kwargs())
    rig.audit.failing = True
    appended_before = len(rig.audit.appended)

    result = await rig.facade.cancel_intent(
        intent_id=accepted["intent_id"],
        principal=OPERATOR,
        idempotency_key="cancel-key-a1",
        request_id="request-c1",
    )

    assert result["status"] == "cancelled"
    assert result["degraded"] == ["audit_unavailable"]
    assert rig.intents.removed == [accepted["intent_id"]]
    assert not await rig.intents.active(rig.clock.now), (
        "power can never flow from a dispatch the caller cancelled"
    )
    assert len(rig.audit.appended) == appended_before


async def test_acknowledge_inhibit_audit_failure_refuses_and_keeps_the_latch(
    api: Any,
) -> None:
    """Impl-10 (no inverse): the durable row must land BEFORE the latch is
    touched -- a failed append refuses with the latch intact and the
    acknowledgement retryable."""
    rig = make_rig(api, units={"pod-a": {"inhibit_latched": True}, "pod-b": {}})
    rig.audit.failing = True

    with pytest.raises(OSError):
        await rig.facade.acknowledge_inhibit(
            unit_id="pod-a",
            principal=OPERATOR,
            idempotency_key="inhibit-key-a1",
            request_id="request-d1",
        )

    assert rig.handles["pod-a"].inhibit_latched is True, "nothing is consumed by the refusal"
    assert "inhibit-ack:pod-a" not in rig.history
    assert rig.bus.published == []

    rig.audit.failing = False
    retried = await rig.facade.acknowledge_inhibit(
        unit_id="pod-a",
        principal=OPERATOR,
        idempotency_key="inhibit-key-a2",
        request_id="request-d2",
    )
    assert retried["latch_cleared"] is True
    assert retried["degraded"] == []
    assert rig.handles["pod-a"].inhibit_latched is False


async def test_acknowledge_stop_audit_failure_consumes_nothing(api: Any) -> None:
    """Impl-10 (no inverse): a stop acknowledgement whose audit fails leaves
    the latch fully consumable -- the snapshot still lists it, the stored
    intent is untouched, and the retry after recovery acknowledges exactly
    once."""
    rig = make_rig(api)
    stopped = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="halt before a degraded audit",
        idempotency_key="stop-key-a1",
        request_id="request-e1",
    )
    rig.audit.failing = True

    with pytest.raises(OSError):
        await rig.facade.acknowledge_emergency_stop(
            stop_id=stopped["stop_id"],
            principal=OPERATOR,
            idempotency_key="ack-key-a1",
            request_id="request-e2",
        )

    view = await rig.facade.snapshot(principal=OPERATOR)
    assert [stop["stop_id"] for stop in view["active_stops"]] == [stopped["stop_id"]]
    assert rig.intents.removed == []

    rig.audit.failing = False
    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=stopped["stop_id"],
        principal=OPERATOR,
        idempotency_key="ack-key-a2",
        request_id="request-e3",
    )
    assert acknowledged == {
        "stop_id": stopped["stop_id"],
        "status": "acknowledged",
        "degraded": [],
    }
    assert rig.intents.removed == [stopped["stop_id"]]


@pytest.mark.parametrize("degradation", ["store_refused", "unknown_unit"])
async def test_degraded_stop_errors_carry_the_reason_codes(api: Any, degradation: str) -> None:
    """Impl-11/Impl-15: the stop error paths carry the uniform DegradedReport
    (stop id + degraded codes) so the boundary can surface both verbatim."""
    rig = make_rig(api)
    if degradation == "store_refused":
        rig.intents.add_failing = True
        unit_ids = ["pod-a"]
    else:
        unit_ids = ["pod-a", "pod-ghost"]

    with pytest.raises((OSError, ValueError)) as excinfo:
        await _stop(
            rig.facade,
            unit_ids=unit_ids,
            reason="degraded completion",
            idempotency_key=f"stop-key-{degradation}",
            request_id=f"request-{degradation}",
        )

    report = getattr(excinfo.value, "degraded_report", None)
    assert report is not None, "the uniform degraded-report type rides the error"
    assert report.stop_id == excinfo.value.stop_id  # type: ignore[attr-defined]
    assert report.stop_id and canonical(report.stop_id)
    expected_code = "intent_store_unavailable" if degradation == "store_refused" else "unknown_unit"
    assert any(code.startswith(expected_code) for code in report.degraded), report.degraded


# --- emergency_stop ---------------------------------------------------------


async def test_emergency_stop_latches_fences_then_bounds_before_returning(api: Any) -> None:
    rig = make_rig(api)
    epoch_before = (await rig.coordinator.snapshot()).epoch

    result = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="operator initiated halt",
        idempotency_key="stop-key-1",
        request_id="request-s",
    )

    assert canonical(result["stop_id"])
    assert result["status"] == "latched"
    assert (await rig.coordinator.snapshot()).epoch > epoch_before
    stored = rig.intents.added[-1]
    assert stored.source is api.IntentSource.EMERGENCY_STOP
    assert stored.direction is api.Direction.IDLE
    assert stored.watts == 0
    assert stored.selected_unit_ids == frozenset({"pod-a", "pod-b"})
    assert stored.actor_identity == OPERATOR.subject
    # API_CONTRACTS: a latched stop carries a fixed duration of at least 24
    # hours and is removed only by acknowledgement, never by TTL expiry.
    assert stored.duration_s >= 86_400.0
    fence = rig.history.index("fence")
    assert fence < rig.history.index("zero:pod-a")
    assert fence < rig.history.index("zero:pod-b")
    assert fence < rig.history.index("audit"), "revocation must fence before audit work"
    assert fence < rig.history.index("publish"), "the stop must fence before publishing"
    assert rig.authorizations.published == []
    assert_audited_and_published(rig, OPERATOR.subject)


@pytest.mark.parametrize(
    "deformation",
    [
        "intents_add_fails",
        "audit_append_fails",
        "actor_zero_fails",
        "coordinator_advance_fails",
        "unknown_unit",
    ],
)
async def test_emergency_stop_survives_degraded_dependencies(api: Any, deformation: str) -> None:
    rig = make_rig(api, units={"pod-a": {}, "pod-b": {}})
    if deformation == "intents_add_fails":
        rig.intents.failing = True
    elif deformation == "audit_append_fails":
        rig.audit.failing = True
    elif deformation == "actor_zero_fails":
        rig.handles["pod-a"].zero_error = RuntimeError("mailbox wedged")
    elif deformation == "coordinator_advance_fails":
        rig.coordinator.advance_error = RuntimeError("fence acknowledgement lost")
    epoch_before = (await rig.coordinator.snapshot()).epoch
    known_units = ["pod-a"] if deformation == "unknown_unit" else ["pod-a", "pod-b"]
    requested_units = ["pod-a", "pod-ghost"] if deformation == "unknown_unit" else known_units

    # Whether a degraded stop raises or returns is unpinned; the safety order
    # underneath it never is.
    with contextlib.suppress(Exception):
        await _stop(
            rig.facade,
            unit_ids=requested_units,
            reason="degraded halt",
            idempotency_key="stop-key-d",
            request_id="request-d",
        )

    assert (await rig.coordinator.snapshot()).epoch > epoch_before, (
        "a degraded dependency must never prevent fencing the generation"
    )
    fence = rig.history.index("fence")
    for unit_id in known_units:
        assert f"zero:{unit_id}" in rig.history, "bounded zero is still requested"
        assert fence < rig.history.index(f"zero:{unit_id}")
    if "audit" in rig.history:
        audit = rig.history.index("audit")
        assert fence < audit, "revocation must fence before audit work"
        for unit_id in known_units:
            assert rig.history.index(f"zero:{unit_id}") < audit
    if rig.bus.published:
        assert fence < rig.history.index("publish"), "the stop must fence before publishing"


async def test_emergency_stop_fences_each_actor_before_requesting_the_bounded_zero(
    api: Any,
) -> None:
    """The bounded zero alone cannot cancel an in-flight nonzero write.

    Only the actor's fence cancels active authority work, so the stop must
    fence every affected unit before it requests the bounded zero.
    """
    rig = make_rig(api)

    result = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="halt",
        idempotency_key="stop-key-f",
        request_id="request-f",
    )

    assert result["degraded"] == [], "fencing each actor is a step of the stop"
    for unit_id in ("pod-a", "pod-b"):
        assert f"fence:{unit_id}" in rig.history, "every affected actor must be fenced"
        assert rig.history.index(f"fence:{unit_id}") < rig.history.index(f"zero:{unit_id}"), (
            "an in-flight nonzero write is cancelled before the bounded zero is requested"
        )


async def test_emergency_stop_cancels_an_inflight_nonzero_write_on_the_real_actor(
    api: Any,
) -> None:
    """P0 regression on the production facade and actor: a dispatched nonzero
    heartbeat write inside a slow gateway is cancelled by the stop, so no
    nonzero command can land on the wire after ``emergency_stop`` returns."""
    actor_module = importlib.import_module("energypod.application.actor")
    transport = SpyTransport()
    write_gate = Gate()
    transport.write_gates.append(write_gate)
    clock = ActorClock()
    authorizations = ActorAuthorizationRepository(ActorAuthorization())
    coordinator = api.AuthorityGenerationCoordinator()
    actor = actor_module.EnergyPodActor(
        unit_id=ACTOR_UNIT_ID,
        transport=transport,
        clock=clock,
        observations=ActorObservationRepository(ObservationRecord()),
        authorizations=authorizations,
        audit=FakeAuditRepository(),
        command_encoder=FakeCommandEncoder(),
        generation_coordinator=coordinator,
        expected_identity=ACTOR_IDENTITY,
        expected_profile=ACTOR_PROFILE,
        expected_cell_count=59,
        stable_observations_required=1,
        essential_read_address=0x5000,
        essential_read_count=7,
        heartbeat_interval_s=1.0,
        heartbeat_safety_margin_s=0.2,
    )
    await actor.start()
    await actor.accept_observation(ObservationRecord())
    await actor.arm()
    facade = api.EnergyServiceFacade(
        site_id=SITE_ID,
        clock=clock,
        intents=FakeIntentRepository(),
        observations=FakeObservationRepository(),
        authorizations=authorizations,
        audit=FakeAuditRepository(),
        events=FakeEventBus(),
        coordinator=coordinator,
        actors={ACTOR_UNIT_ID: RealActorHandle(actor)},
    )

    heartbeat = asyncio.create_task(actor.heartbeat_once())
    entered_write = await settle_until(write_gate.entered.is_set)
    if not entered_write:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert entered_write, "heartbeat never reached the controlled write boundary"

    result = await facade.emergency_stop(
        unit_ids=[ACTOR_UNIT_ID],
        reason="operator halt behind a slow gateway write",
        principal=OPERATOR,
        idempotency_key="stop-key-real",
        request_id="request-real",
    )

    assert result["status"] == "latched"
    assert result["degraded"] == [], "fencing the actor is part of the stop, not extra credit"
    assert any(name == "write:cancelled" for name, _ in transport.history), (
        "the in-flight nonzero write must be cancelled, not awaited"
    )
    # The slow gateway releases only after the stop returned; the cancelled
    # write can never complete on the wire.
    write_gate.release.set()
    await asyncio.gather(heartbeat, return_exceptions=True)
    for _ in range(10):
        await asyncio.sleep(0)
    nonzero = [write for write in transport.writes if write.values[1] != 0]
    assert nonzero == [], "a nonzero command landed on the wire after the stop returned"
    assert EncodedWrite(0x0200, (1, 0, 0)) in transport.writes, "the stop still bounded-zeroes"
    await actor.shutdown()


async def test_a_stop_the_store_refused_leaves_no_phantom_latch(api: Any) -> None:
    """Degraded variant 1: the safety work lands, the store refuses the intent.

    The latch is recorded only after the store holds the intent, so a degraded
    store leaves nothing half-latched: the error carries the stop id, an
    acknowledgement truthfully refuses instead of failing forever on a
    registry entry no removal could satisfy, and a re-issued stop latches and
    acknowledges exactly once.
    """
    rig = make_rig(api)
    rig.intents.add_failing = True

    with pytest.raises(OSError) as excinfo:
        await _stop(
            rig.facade,
            unit_ids=["pod-a", "pod-b"],
            reason="halt behind a degraded store",
            idempotency_key="stop-key-d1",
            request_id="request-d1",
        )
    stop_id = getattr(excinfo.value, "stop_id", None)
    assert isinstance(stop_id, str) and canonical(stop_id), (
        "the error path must carry the stop id so the operator can correlate the stop"
    )

    # The safety sequence still fenced the fleet and the actors and zeroed.
    assert "fence" in rig.history
    assert "fence:pod-a" in rig.history and "zero:pod-a" in rig.history

    # The refused intent exists nowhere, so acknowledging it must refuse too.
    rig.intents.add_failing = False
    with pytest.raises(LookupError, match="no latched emergency stop"):
        await rig.facade.acknowledge_emergency_stop(
            stop_id=stop_id,
            principal=OPERATOR,
            idempotency_key="ack-key-d1",
            request_id="request-a1",
        )
    assert rig.intents.removed == []

    # The operator's path forward: re-issue the stop once the store recovered.
    repeated = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="halt again",
        idempotency_key="stop-key-d2",
        request_id="request-d2",
    )
    assert repeated["status"] == "latched"
    assert repeated["degraded"] == []
    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=repeated["stop_id"],
        principal=OPERATOR,
        idempotency_key="ack-key-d2",
        request_id="request-a2",
    )
    assert acknowledged["status"] == "acknowledged"
    assert rig.intents.removed[-1] == repeated["stop_id"]
    with pytest.raises(LookupError):
        await rig.facade.acknowledge_emergency_stop(
            stop_id=repeated["stop_id"],
            principal=OPERATOR,
            idempotency_key="ack-key-d3",
            request_id="request-a3",
        )


async def test_unknown_unit_stop_error_still_latches_and_carries_the_stop_id(
    api: Any,
) -> None:
    """Degraded variant 2: a stop listing an unknown unit completes the safety
    sequence for the known units, then raises carrying its id, so the operator
    can still acknowledge the latched stop."""
    rig = make_rig(api)

    with pytest.raises(ValueError) as excinfo:
        await _stop(
            rig.facade,
            unit_ids=["pod-a", "pod-ghost"],
            reason="halt with a typo",
            idempotency_key="stop-key-d2",
            request_id="request-d2",
        )
    stop_id = getattr(excinfo.value, "stop_id", None)
    assert isinstance(stop_id, str) and canonical(stop_id)

    assert "fence:pod-a" in rig.history and "zero:pod-a" in rig.history, (
        "the known units were fenced and zeroed before the error surfaced"
    )
    assert any(intent.id == stop_id for intent in rig.intents.added), (
        "the store was healthy, so the stop intent is latched there too"
    )

    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=stop_id,
        principal=OPERATOR,
        idempotency_key="ack-key-d3",
        request_id="request-a3",
    )

    assert acknowledged["status"] == "acknowledged"
    assert rig.intents.removed[-1] == stop_id
    live = await rig.intents.active(rig.clock.now)
    assert not any(intent.id == stop_id for intent in live), "the latch must not relatch"


async def test_repeated_stops_receive_distinct_acknowledgeable_ids(api: Any) -> None:
    rig = make_rig(api)

    first = await _stop(
        rig.facade,
        unit_ids=["pod-a"],
        reason="first",
        idempotency_key="stop-key-1",
        request_id="request-s1",
    )
    second = await _stop(
        rig.facade,
        unit_ids=["pod-b"],
        reason="second",
        idempotency_key="stop-key-2",
        request_id="request-s2",
    )

    assert canonical(first["stop_id"])
    assert canonical(second["stop_id"])
    assert first["stop_id"] != second["stop_id"]


async def test_stop_intents_share_the_monotonic_revision_sequence(api: Any) -> None:
    rig = make_rig(api)

    await rig.facade.submit_intent(**submit_kwargs())
    await _stop(
        rig.facade,
        unit_ids=["pod-a"],
        reason="halt",
        idempotency_key="stop-key-1",
        request_id="request-s",
    )
    await rig.facade.submit_intent(
        **submit_kwargs(idempotency_key="intent-key-2", request_id="request-2")
    )

    revisions = [intent.acceptance_revision for intent in rig.intents.added]
    assert revisions[0] < revisions[1] < revisions[2]


# --- acknowledge_emergency_stop ---------------------------------------------


async def test_acknowledge_accepts_the_exact_id_and_removes_the_latch(api: Any) -> None:
    rig = make_rig(api)
    stopped = await _stop(
        rig.facade,
        unit_ids=["pod-a", "pod-b"],
        reason="halt",
        idempotency_key="stop-key-1",
        request_id="request-s",
    )
    stop_id = stopped["stop_id"]
    assert any(intent.id == stop_id for intent in await rig.intents.active(rig.clock.now))
    rig.reset_recorders()

    acknowledged = await rig.facade.acknowledge_emergency_stop(
        stop_id=stop_id,
        principal=OPERATOR,
        idempotency_key="ack-key-1",
        request_id="request-k",
    )

    assert acknowledged["stop_id"] == stop_id
    assert acknowledged["status"] == "acknowledged"
    assert rig.intents.removed == [stop_id]
    live = await rig.intents.active(rig.clock.now)
    assert not any(intent.id == stop_id for intent in live), "latch must not relatch"
    assert_audited_and_published(rig, OPERATOR.subject)


@pytest.mark.parametrize("mode", ["unknown_id", "already_acknowledged"])
async def test_acknowledge_rejects_unknown_and_consumed_ids(api: Any, mode: str) -> None:
    rig = make_rig(api)
    stopped = await _stop(
        rig.facade,
        unit_ids=["pod-a"],
        reason="halt",
        idempotency_key="stop-key-1",
        request_id="request-s",
    )
    target = stopped["stop_id"]
    if mode == "already_acknowledged":
        await rig.facade.acknowledge_emergency_stop(
            stop_id=target,
            principal=OPERATOR,
            idempotency_key="ack-key-1",
            request_id="request-k1",
        )
    rig.reset_recorders()

    with pytest.raises(LookupError):
        await rig.facade.acknowledge_emergency_stop(
            stop_id=target,
            principal=OPERATOR,
            idempotency_key="ack-key-2",
            request_id="request-k2",
        )

    assert rig.intents.removed == []


async def test_acknowledge_is_exact_and_leaves_other_latched_stops_active(api: Any) -> None:
    rig = make_rig(api)
    first = await _stop(
        rig.facade,
        unit_ids=["pod-a"],
        reason="first",
        idempotency_key="stop-key-1",
        request_id="request-s1",
    )
    second = await _stop(
        rig.facade,
        unit_ids=["pod-b"],
        reason="second",
        idempotency_key="stop-key-2",
        request_id="request-s2",
    )

    await rig.facade.acknowledge_emergency_stop(
        stop_id=first["stop_id"],
        principal=OPERATOR,
        idempotency_key="ack-key-1",
        request_id="request-k",
    )

    assert rig.intents.removed == [first["stop_id"]]
    live_ids = {intent.id for intent in await rig.intents.active(rig.clock.now)}
    assert first["stop_id"] not in live_ids
    assert second["stop_id"] in live_ids


# --- acknowledge_inhibit -------------------------------------------------------


async def test_acknowledge_inhibit_clears_exactly_the_named_latched_unit(api: Any) -> None:
    rig = make_rig(
        api,
        units={
            "pod-a": {},
            "pod-b": {},
            "pod-c": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True},
            "pod-d": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True},
        },
    )

    result = await rig.facade.acknowledge_inhibit(
        unit_id="pod-c",
        principal=OPERATOR,
        idempotency_key="inhibit-ack-key-1",
        request_id="request-i1",
    )

    assert result["unit_id"] == "pod-c"
    assert result["status"] == "acknowledged"
    assert rig.handles["pod-c"].inhibit_latched is False
    assert rig.handles["pod-d"].inhibit_latched is True, "acknowledgement is exact-unit"
    assert rig.handles["pod-c"].lifecycle is api.UnitLifecycle.INHIBITED, (
        "acknowledgement only clears the latch; recovery still needs stable samples"
    )
    assert "arm:pod-c" not in rig.history, "acknowledgement never arms the unit"
    assert_audited_and_published(rig, OPERATOR.subject)


async def test_acknowledge_inhibit_is_idempotent(api: Any) -> None:
    rig = make_rig(
        api,
        units={"pod-a": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True}},
    )

    first = await rig.facade.acknowledge_inhibit(
        unit_id="pod-a",
        principal=OPERATOR,
        idempotency_key="inhibit-ack-key-1",
        request_id="request-i1",
    )
    second = await rig.facade.acknowledge_inhibit(
        unit_id="pod-a",
        principal=OPERATOR,
        idempotency_key="inhibit-ack-key-2",
        request_id="request-i2",
    )

    assert first["status"] == "acknowledged"
    assert second["status"] == "acknowledged", "a repeated acknowledgement never raises"
    assert rig.handles["pod-a"].inhibit_latched is False
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.INHIBITED
    assert_audited_and_published(rig, OPERATOR.subject)


async def test_acknowledge_inhibit_without_a_latched_inhibit_is_a_no_op_success(
    api: Any,
) -> None:
    # API_CONTRACTS "Inhibit acknowledgement" makes the endpoint idempotent,
    # audited, and published; only LATCHED inhibits need the acknowledgement,
    # so a non-latched inhibit is pinned here as a no-op success.
    rig = make_rig(
        api,
        units={
            "pod-a": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": False},
            "pod-b": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True},
        },
    )

    result = await rig.facade.acknowledge_inhibit(
        unit_id="pod-a",
        principal=OPERATOR,
        idempotency_key="inhibit-ack-key-1",
        request_id="request-i1",
    )

    assert result["unit_id"] == "pod-a"
    assert rig.handles["pod-a"].lifecycle is api.UnitLifecycle.INHIBITED, (
        "a transient inhibit recovers through stable samples only"
    )
    assert rig.handles["pod-b"].inhibit_latched is True
    assert "arm:pod-a" not in rig.history, "a no-op acknowledgement must not bypass recovery"
    assert_audited_and_published(rig, OPERATOR.subject)


@pytest.mark.parametrize(
    "deformation",
    [
        {"scopes": frozenset({"observe", "dispatch", "stop", "stop:acknowledge"})},
        {"interactive": False},
    ],
    ids=["missing_arm_scope", "non_interactive"],
)
async def test_acknowledge_inhibit_requires_arm_scope_and_an_interactive_principal(
    api: Any, deformation: dict
) -> None:
    rig = make_rig(
        api,
        units={"pod-a": {"lifecycle": api.UnitLifecycle.INHIBITED, "inhibit_latched": True}},
    )
    principal = replace(OPERATOR, **deformation)

    with pytest.raises(PermissionError):
        await rig.facade.acknowledge_inhibit(
            unit_id="pod-a",
            principal=principal,
            idempotency_key="inhibit-ack-key-1",
            request_id="request-i1",
        )

    assert rig.recorder_activity() == []
    assert rig.handles["pod-a"].inhibit_latched is True


# --- principal enforcement --------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        "snapshot",
        "health",
        "unit_detail",
        "recent_audit",
        "submit_intent",
        "arm",
        "disarm",
        "acknowledge_inhibit",
        "emergency_stop",
        "acknowledge_emergency_stop",
    ],
)
async def test_cross_site_principals_never_reach_the_ports(api: Any, operation: str) -> None:
    rig = make_rig(api)
    stop_id = "stop-irrelevant"
    if operation == "acknowledge_emergency_stop":
        stopped = await _stop(
            rig.facade,
            unit_ids=["pod-a"],
            reason="halt",
            idempotency_key="stop-key-0",
            request_id="request-s0",
        )
        stop_id = stopped["stop_id"]
    rig.reset_recorders()

    with pytest.raises(PermissionError):
        await _invoke(rig.facade, operation, STRANGER, stop_id)

    assert rig.recorder_activity() == []


@pytest.mark.parametrize(
    "deformation",
    [
        {"subject": "subject with spaces"},
        {"site_id": ""},
        {"scopes": {"observe"}},
        {"interactive": "yes"},
    ],
    ids=["non_canonical_subject", "empty_site", "scopes_not_frozen", "interactive_not_bool"],
)
@pytest.mark.parametrize("operation", ["snapshot", "submit_intent"])
async def test_malformed_principals_are_rejected_before_any_port_work(
    api: Any, deformation: dict, operation: str
) -> None:
    rig = make_rig(api)
    broken = replace(OPERATOR, **deformation)

    with pytest.raises((PermissionError, TypeError, ValueError)):
        await _invoke(rig.facade, operation, broken)

    assert rig.recorder_activity() == []


# --- cancel intent (2026-08-23 operator feature) -------------------------------
#
# API_CONTRACTS: cancelling the active intent is safety-positive -- repository
# removal, the kernel's next no-winner tick revokes, and the device watchdog
# hands power back.  Scope is dispatch with NO interactive requirement, the
# mutation is audited as intent_cancelled, and the bus carries intent.cancelled
# so the console's request card clears the same way it does on expiry.


async def test_cancel_intent_by_id_and_by_current_removes_audits_and_publishes(
    api: Any,
) -> None:
    rig = make_rig(api, seeded_intents=(manual_intent(api, revision=3, watts=900),))
    cancelled = await rig.facade.cancel_intent(
        intent_id="intent-3",
        principal=OPERATOR,
        idempotency_key="cancel-key-1",
        request_id="cancel-request-1",
    )
    assert cancelled == {
        "intent_id": "intent-3",
        "status": "cancelled",
        "unit_ids": ["pod-a"],
        "degraded": [],
    }
    assert rig.intents.removed == ["intent-3"]
    (event,) = [event for event in rig.audit.appended if event.event_type == "intent_cancelled"]
    assert event.intent_id == "intent-3"
    assert event.result == "cancelled"
    assert event.principal == OPERATOR.subject
    (published,) = [body for body in rig.bus.published if body["type"] == "intent.cancelled"]
    assert published["payload"] == {
        "principal": OPERATOR.subject,
        "intent_id": "intent-3",
        "unit_ids": ["pod-a"],
    }

    # "current" resolves the newest active intent.
    rig.intents.added.append(manual_intent(api, revision=7, watts=400))
    rig.intents.removed.clear()
    current = await rig.facade.cancel_intent(
        intent_id="current",
        principal=OPERATOR,
        idempotency_key="cancel-key-2",
        request_id="cancel-request-2",
    )
    assert current["intent_id"] == "intent-7"
    assert rig.intents.removed == ["intent-7"]


async def test_cancel_intent_never_touches_a_latched_emergency_stop(api: Any) -> None:
    """A latched stop leaves only through its privileged acknowledgement."""
    latched_stop = api.PowerIntent(
        id="stop-1",
        source=api.IntentSource.EMERGENCY_STOP,
        selected_unit_ids=frozenset({"pod-a"}),
        direction=api.Direction.IDLE,
        watts=0,
        duration_s=86_400.0,
        accepted_at_mono=90.0,
        acceptance_revision=9,
        actor_identity="person:operator",
    )
    rig = make_rig(api, seeded_intents=(latched_stop,))
    with pytest.raises(ValueError, match="emergency stop"):
        await rig.facade.cancel_intent(
            intent_id="stop-1",
            principal=OPERATOR,
            idempotency_key="cancel-stop-key",
            request_id="cancel-stop-request",
        )
    assert rig.intents.removed == []
    assert not [event for event in rig.audit.appended if event.event_type == "intent_cancelled"]


async def test_cancel_intent_refusals_are_loud_and_precise(api: Any) -> None:
    rig = make_rig(api, seeded_intents=(manual_intent(api, revision=3, watts=900),))
    with pytest.raises(LookupError):
        await rig.facade.cancel_intent(
            intent_id="intent-404",
            principal=OPERATOR,
            idempotency_key="cancel-unknown-key",
            request_id="cancel-unknown-request",
        )
    empty = make_rig(api)
    with pytest.raises(ValueError, match="no active intent"):
        await empty.facade.cancel_intent(
            intent_id="current",
            principal=OPERATOR,
            idempotency_key="cancel-empty-key",
            request_id="cancel-empty-request",
        )
    without_dispatch = replace(OPERATOR, scopes=frozenset({"observe"}))
    with pytest.raises(PermissionError):
        await rig.facade.cancel_intent(
            intent_id="intent-3",
            principal=without_dispatch,
            idempotency_key="cancel-scope-key",
            request_id="cancel-scope-request",
        )


async def test_cancel_intent_is_available_to_automation(api: Any) -> None:
    """Cancelling stops power: a non-interactive dispatch principal may drive it."""
    automation = Principal(
        subject="service:automation",
        scopes=frozenset({"observe", "dispatch"}),
        interactive=False,
    )
    rig = make_rig(api, seeded_intents=(manual_intent(api, revision=3, watts=900),))
    cancelled = await rig.facade.cancel_intent(
        intent_id="current",
        principal=automation,
        idempotency_key="cancel-automation-key",
        request_id="cancel-automation-request",
    )
    assert cancelled["status"] == "cancelled"


# --- device-mode dispatch gating (2026-08-23 incident 1) -----------------------
#
# The vendor app refuses PQ sends unless debugMode == 0 and the fleet is in
# Remote control (MiniESapp.cs:2180; ctrlMode enum 1 Remote / 2 Local).  When
# the decoded mode words say the pod will ignore external objectives, intent
# submission is refused with the explicit reason -- absent evidence (a read
# plan without the mode blocks) changes nothing.


async def test_submit_intent_is_refused_while_a_unit_reports_debug_mode(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), debug_mode_w=3)},
    )
    with pytest.raises(ValueError, match="device_debug_mode_active"):
        await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert rig.intents.added == []


async def test_submit_intent_is_refused_while_a_unit_is_not_remote(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), ctrl_mode_w=2)},
    )
    with pytest.raises(ValueError, match="device_mode_not_remote"):
        await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert rig.intents.added == []


# --- B5: the cached mode word alone never refuses -----------------------------
#
# SYNC_RESILIENCE_AUDIT B5 (2026-08-24): the ctrlMode word rides the
# once-per-process system tier, so a pod that was in Local at controller boot
# refused EVERY dispatch for the process lifetime -- even after the operator
# flipped it to Remote -- exactly the soc incident's shape one register over.
# The refusal path now performs ONE bounded fresh read of the mode words
# through the owning actor before denying: the cached word alone never
# refuses; a fresh-confirmed non-Remote (or two failed refresh reads) still
# does (class D).


async def test_a_cached_local_word_refreshed_to_remote_accepts_the_intent(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), ctrl_mode_w=2)},
        units={
            "pod-a": {"refresh_outcomes": ((1, 6),)},
            "pod-b": {},
        },
    )
    accepted = await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert accepted["status"] == "accepted"
    handles = rig.handles
    assert handles["pod-a"].refresh_calls == 1, (
        "the refusal path must refresh the mode words exactly once before denying"
    )


async def test_refresh_recovering_after_one_failed_read_still_accepts(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), ctrl_mode_w=2)},
        units={"pod-a": {"refresh_outcomes": (OSError("gateway hiccup"), (1, 6))}},
    )
    accepted = await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert accepted["status"] == "accepted"
    assert rig.handles["pod-a"].refresh_calls == 2


async def test_a_fresh_confirmed_local_word_still_refuses(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), ctrl_mode_w=2)},
        units={"pod-a": {"refresh_outcomes": ((2, 6),)}},
    )
    with pytest.raises(ValueError, match="device_mode_not_remote"):
        await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert rig.intents.added == []
    assert rig.handles["pod-a"].refresh_calls == 1


async def test_two_failed_mode_refresh_reads_refuse_dispatch(api: Any) -> None:
    """An unreadable mode word is genuinely-unreadable evidence (class D):
    one bounded retry, then the refusal stands -- the cached word alone never
    refuses, but neither does a mode we cannot read."""
    rig = make_rig(
        api,
        telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), ctrl_mode_w=2)},
        units={"pod-a": {"refresh_outcomes": (OSError("gateway hiccup"),)}},
    )
    with pytest.raises(ValueError, match="device_mode_not_remote"):
        await _invoke(rig.facade, "submit_intent", OPERATOR)
    assert rig.intents.added == []
    assert rig.handles["pod-a"].refresh_calls == 2, "exactly one bounded retry"


async def test_submit_intent_dispatchable_modes_and_absent_evidence_both_accept(
    api: Any,
) -> None:
    remote = make_rig(
        api,
        telemetry={
            "pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality(), debug_mode_w=0, ctrl_mode_w=1)
        },
    )
    accepted = await _invoke(remote.facade, "submit_intent", OPERATOR)
    assert accepted["status"] == "accepted"

    unaware = make_rig(api, telemetry={"pod-a": Telemetry("pod-a", 99.5, 100.0, good_quality())})
    accepted_without_words = await _invoke(unaware.facade, "submit_intent", OPERATOR)
    assert accepted_without_words["status"] == "accepted"


async def test_snapshot_telemetry_summary_exposes_the_mode_words(api: Any) -> None:
    rig = make_rig(
        api,
        telemetry={
            "pod-a": Telemetry(
                "pod-a", 99.5, 100.0, good_quality(), debug_mode_w=0, ctrl_mode_w=1, work_mode_w=7
            )
        },
    )
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert units["pod-a"]["telemetry"]["debug_mode_w"] == 0
    assert units["pod-a"]["telemetry"]["ctrl_mode_w"] == 1
    assert units["pod-a"]["telemetry"]["work_mode_w"] == 7
    assert units["pod-a"]["telemetry"]["run_mode_w"] is None
    assert units["pod-b"]["telemetry"] is None

    detail = await rig.facade.unit_detail(principal=OPERATOR, unit_id="pod-a")
    assert detail["debug_mode_w"] == 0
    assert detail["ctrl_mode_w"] == 1


# --- the guarded excess-charging toggle (DESIGN_EXCESS_ACTIVATION §3) ------------
#
# POST /api/v1/excess-charging's facade half: the inhibit-acknowledgement
# guarded-confirmation pattern applied to a feature gate.  P1 non-persistence
# (runtime toggles never survive restart), P2 enable refusals (units ACTIVE
# under another intent, latched stops -- never latched inhibits), P3 the
# once-ever durable net-billing acknowledgement (durable-append-first, latch
# flip second, an append failure refuses), P4 scopes (arm + interactive to
# enable, arm alone to disable), P5 participation-only (the toggle flips no
# commissioned envelope), P6 composition rescope (the facade port is wired
# exactly when the block is present).


EXCESS_ECONOMICS_ACK_EVENT_ID = "excess-charging-economics-acknowledged"


class FakeExcessControl:
    """Inline stand-in for the composed ExcessAdviserController port."""

    def __init__(
        self,
        *,
        acknowledged: bool = False,
        enabled: bool = False,
        origin: str = "config",
    ) -> None:
        self.acknowledged_economics = acknowledged
        self.enabled = enabled
        self.enabled_origin = origin
        self.flips: list[bool] = []
        self.acknowledged_at = 0

    def set_participation(self, *, enabled: bool) -> None:
        self.flips.append(bool(enabled))
        self.enabled = bool(enabled)
        self.enabled_origin = "runtime"

    def mark_acknowledged(self) -> None:
        self.acknowledged_at += 1
        self.acknowledged_economics = True

    def state_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "enabled_origin": self.enabled_origin,
            "acknowledged_economics": self.acknowledged_economics,
            "active": False,
            "hysteresis_state": "inactive",
            "target_unit_id": None,
            "commanded_charge_w": 0,
            "eligible_export_charge_w": 0,
            "fleet_export_w": None,
            "export_evidence": "missing",
            "charge_cap_w": 2_500,
            "held_intent_id": None,
            "last_action": "idle",
            "last_tick_at": "2026-08-25T11:04:31+00:00",
            "reason_codes": ["disabled_by_config"],
        }


def toggle_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "action": "disable",
        "confirmation": "EXCESS",
        "principal": OPERATOR,
        "idempotency_key": "excess-key-1",
        "request_id": "excess-request-1",
    }
    kwargs.update(overrides)
    return kwargs


def make_excess_rig(
    api: Any,
    *,
    acknowledged: bool = False,
    config_enabled: bool = False,
    **rig_kwargs: Any,
) -> Rig:
    control = FakeExcessControl(
        acknowledged=acknowledged,
        enabled=config_enabled,
        origin="config",
    )
    rig_kwargs.setdefault("units", {"pod-a": {}, "pod-b": {}})
    return make_rig(api, excess=control, **rig_kwargs)


def _refusal_code(error: BaseException) -> str:
    return str(getattr(error, "code", ""))


async def test_toggle_without_a_composed_block_is_not_commissioned(api: Any) -> None:
    rig = make_rig(api)

    with pytest.raises(Exception) as caught:
        await rig.facade.set_excess_charging(**toggle_kwargs())

    assert _refusal_code(caught.value) == "excess_charging_not_commissioned"
    assert getattr(caught.value, "details", {}) == {}


async def test_disable_flips_participation_audits_and_answers_the_contract_body(
    api: Any,
) -> None:
    rig = make_excess_rig(api, acknowledged=True, config_enabled=True)

    result = await rig.facade.set_excess_charging(**toggle_kwargs(action="disable"))

    assert result["feature"] == "excess_charging"
    assert result["enabled"] is False
    assert result["enabled_origin"] == "runtime"
    assert result["persisted"] is False, "P1: the non-persistence policy rides every response"
    assert result["acknowledged_economics"] is True
    assert result["adviser_state"]["enabled"] is False
    assert result["adviser_state"]["enabled_origin"] == "runtime"
    assert rig.excess.flips == [False] if rig.excess else []
    toggled = [
        event for event in rig.audit.appended if event.event_type == "excess_charging_toggled"
    ]
    assert len(toggled) == 1
    assert toggled[0].result == "disabled"
    assert toggled[0].principal == OPERATOR.subject
    assert toggled[0].reason_codes == ("disabled",)


async def test_enable_requires_the_arm_scope_and_an_interactive_principal(api: Any) -> None:
    rig = make_excess_rig(api, acknowledged=True, config_enabled=False)
    automation = Principal(
        subject="service:automation",
        scopes=frozenset({"observe", "dispatch", "arm"}),
        interactive=False,
    )

    with pytest.raises(PermissionError, match="interactive"):
        await rig.facade.set_excess_charging(
            **toggle_kwargs(action="enable", principal=automation, economics="NET_BILLED")
        )
    viewer = Principal(subject="person:viewer", scopes=frozenset({"observe"}))
    with pytest.raises(PermissionError, match="arm"):
        await rig.facade.set_excess_charging(**toggle_kwargs(action="enable", principal=viewer))
    # P4: disable is safety-positive -- arm scope alone, no interactivity.
    await rig.facade.set_excess_charging(**toggle_kwargs(action="disable", principal=automation))


async def test_the_first_enable_needs_the_net_billing_acknowledgement(api: Any) -> None:
    """P3: an unacknowledged site is refused with exactly what to send."""
    rig = make_excess_rig(api, acknowledged=False, config_enabled=False)

    with pytest.raises(Exception) as caught:
        await rig.facade.set_excess_charging(**toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "economics_acknowledgement_required"
    assert getattr(caught.value, "details", {}) == {"acknowledgement": "NET_BILLED"}
    assert not rig.audit.appended, "a refusal appends nothing"
    if rig.excess:
        assert rig.excess.flips == []


async def test_the_first_enable_captures_the_acknowledgement_durably_first(api: Any) -> None:
    """Durable-append FIRST, latch flip second: the ack row is the
    once-ever fact (deterministic event id), and only after it lands does
    the participation flip and its own audit row."""
    rig = make_excess_rig(api, acknowledged=False, config_enabled=False)

    result = await rig.facade.set_excess_charging(
        **toggle_kwargs(action="enable", economics="NET_BILLED")
    )

    assert [event.event_type for event in rig.audit.appended] == [
        "excess_charging_economics_acknowledged",
        "excess_charging_toggled",
    ]
    ack = rig.audit.appended[0]
    assert ack.event_id == EXCESS_ECONOMICS_ACK_EVENT_ID
    assert ack.principal == OPERATOR.subject
    assert ack.result == "acknowledged"
    assert rig.audit.appended[1].result == "enabled"
    assert rig.excess is not None and rig.excess.flips == [True]
    assert rig.excess.acknowledged_economics is True
    assert result["enabled"] is True
    assert result["enabled_origin"] == "runtime"
    assert result["acknowledged_economics"] is True

    # Once ever: a later enable needs no economics field and appends no ack row.
    rig.audit.appended.clear()
    await rig.facade.set_excess_charging(
        **toggle_kwargs(action="disable", idempotency_key="excess-key-2")
    )
    rig.excess.flips.clear()
    result = await rig.facade.set_excess_charging(
        **toggle_kwargs(action="enable", idempotency_key="excess-key-3")
    )
    assert [event.event_type for event in rig.audit.appended] == [
        "excess_charging_toggled",
        "excess_charging_toggled",
    ]
    assert result["acknowledged_economics"] is True


async def test_an_acknowledgement_append_failure_refuses_the_enable(api: Any) -> None:
    """P3's fail-closed gate: no durable audit append means no latch and no
    enable -- never a silent pass."""
    rig = make_excess_rig(api, acknowledged=False, config_enabled=False)
    rig.audit.failing = True

    with pytest.raises(OSError, match="audit store unavailable"):
        await rig.facade.set_excess_charging(
            **toggle_kwargs(action="enable", economics="NET_BILLED")
        )

    assert rig.excess is not None
    assert rig.excess.flips == [], "the participation flag never moved"
    assert rig.excess.acknowledged_economics is False, "the latch never flipped"


async def test_enable_is_refused_while_a_unit_runs_under_another_intent(api: Any) -> None:
    """P2: enabling under an active manual/agent request would be 'on doing
    nothing' -- the exact invisibility this package removes.  The refusal
    names the units so the operator finishes or cancels first."""
    rig = make_excess_rig(
        api,
        acknowledged=True,
        config_enabled=False,
        seeded_intents=(manual_intent(api, revision=5, watts=900, unit_ids=frozenset({"pod-a"})),),
    )

    with pytest.raises(Exception) as caught:
        await rig.facade.set_excess_charging(**toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "excess_enable_refused"
    details = getattr(caught.value, "details", {})
    assert details["reasons"] == ["unit_active_under_intent"]
    assert details["unit_ids"] == ["pod-a"]
    assert details["stop_ids"] == []
    assert rig.excess is not None and rig.excess.flips == []


async def test_enable_is_refused_while_a_latched_stop_holds(api: Any) -> None:
    rig = make_excess_rig(api, acknowledged=True, config_enabled=False)
    stop = await rig.facade.emergency_stop(
        unit_ids=["pod-a", "pod-b"],
        reason="latched for the refusal scenario",
        principal=OPERATOR,
        idempotency_key="stop-key-1",
        request_id="stop-request-1",
    )

    with pytest.raises(Exception) as caught:
        await rig.facade.set_excess_charging(**toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "excess_enable_refused"
    details = getattr(caught.value, "details", {})
    assert details["reasons"] == ["latched_stop_holds"]
    assert details["stop_ids"] == [stop["stop_id"]]
    assert details["unit_ids"] == []

    # P2: disable is never refused -- stopping is the safety-positive direction.
    rig.audit.appended.clear()
    result = await rig.facade.set_excess_charging(**toggle_kwargs(action="disable"))
    assert result["enabled"] is False


async def test_a_latched_inhibit_does_not_refuse_the_enable(api: Any) -> None:
    """P2's carve-out: the selector skips inhibited units honestly
    (no_eligible_target) and the projection says so -- an inhibit is not a
    fence on the whole feature."""
    rig = make_excess_rig(
        api,
        acknowledged=True,
        config_enabled=False,
        units={"pod-a": {"inhibit_latched": True, "inhibit_cause": "external_writer"}, "pod-b": {}},
    )

    result = await rig.facade.set_excess_charging(**toggle_kwargs(action="enable"))

    assert result["enabled"] is True


async def test_a_repeated_toggle_is_an_audited_noop(api: Any) -> None:
    rig = make_excess_rig(api, acknowledged=True, config_enabled=False)

    await rig.facade.set_excess_charging(
        **toggle_kwargs(action="enable", economics="NET_BILLED", idempotency_key="k1")
    )
    rig.audit.appended.clear()
    result = await rig.facade.set_excess_charging(
        **toggle_kwargs(action="enable", idempotency_key="k2")
    )

    assert result["enabled"] is True
    assert result["enabled_origin"] == "runtime"
    noop = [event for event in rig.audit.appended if event.event_type == "excess_charging_toggled"]
    assert len(noop) == 1
    assert noop[0].result == "noop"

    rig.audit.appended.clear()
    again = await rig.facade.set_excess_charging(
        **toggle_kwargs(action="disable", idempotency_key="k3")
    )
    again = await rig.facade.set_excess_charging(
        **toggle_kwargs(action="disable", idempotency_key="k4")
    )
    assert again["enabled"] is False
    noop2 = [event for event in rig.audit.appended if event.event_type == "excess_charging_toggled"]
    assert [event.result for event in noop2][-1] == "noop"


async def test_the_toggle_validates_its_literals_at_the_facade_too(api: Any) -> None:
    """Defense in depth behind the guarded boundary's 422s: a direct drive
    with a wrong literal is a plain validation error, never a mutation."""
    rig = make_excess_rig(api, acknowledged=True, config_enabled=True)
    with pytest.raises(ValueError, match="action"):
        await rig.facade.set_excess_charging(**toggle_kwargs(action="pause"))
    with pytest.raises(ValueError, match="confirmation"):
        await rig.facade.set_excess_charging(**toggle_kwargs(confirmation="ARM"))
    with pytest.raises(ValueError, match="economics"):
        await rig.facade.set_excess_charging(**toggle_kwargs(action="disable", economics="GROSS"))
    assert rig.excess is not None and rig.excess.flips == []


async def test_the_snapshot_carries_the_projection_exactly_when_composed(api: Any) -> None:
    """§1: top level beside intent, present while composed (even suspended),
    ABSENT when the config block is absent -- feature detection, byte-identical
    absent-block behavior."""
    rig = make_excess_rig(api, acknowledged=False, config_enabled=False)
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    assert "adviser_state" in snapshot
    assert snapshot["adviser_state"]["acknowledged_economics"] is False

    bare = make_rig(api)
    absent = await bare.facade.snapshot(principal=OPERATOR)
    assert "adviser_state" not in absent


async def test_intent_acceptance_events_carry_the_remaining_lifetime(api: Any) -> None:
    """The UI audit's filed backend request: `intent.accepted` bus payloads
    carry `expires_in_s` so every operator's console card can state its
    remaining time, not only the 202's caller."""
    rig = make_rig(api)

    await rig.facade.submit_intent(**submit_kwargs(ttl_s=42.0))

    accepted = [body for body in rig.bus.published if body["type"] == "intent.accepted"]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["expires_in_s"] == 42.0


# --- DESIGN_NIGHT_CHARGE §2.1/§3.2 B3: the night facade surface ------------------
#
# POST /api/v1/night-charging's facade half: the excess toggle's pattern with
# the night feature's own codes — the shared durable-once PARTITION
# acknowledgement (either surface's capture counts; durable-append-FIRST, an
# audit failure refuses), the P2 refusal set on enable, the snapshot's
# feature-detected `night_charge_state`, and the composition-internal
# `submit_night_intent` twin (source pinned OPTIMIZER, `night-` prefix,
# per-battery watts native, never routed on REST or MCP).


NIGHT_SHARED_ACK_EVENT_ID = "schedule-night-windows-acknowledged"
NIGHT_PARTITION_ASSERTION = (
    "the external writer applications stand down for the granted window; the controller owns it"
)


class FakeNightControl:
    """Inline stand-in for the composed NightChargeController port."""

    def __init__(
        self,
        *,
        acknowledged: bool = False,
        enabled: bool = False,
        origin: str = "config",
    ) -> None:
        self.acknowledged_partition = acknowledged
        self.enabled = enabled
        self.enabled_origin = origin
        self.flips: list[bool] = []
        self.acknowledged_at = 0

    def set_participation(self, *, enabled: bool) -> None:
        self.flips.append(bool(enabled))
        self.enabled = bool(enabled)
        self.enabled_origin = "runtime"

    def mark_acknowledged(self) -> None:
        self.acknowledged_at += 1
        self.acknowledged_partition = True

    def state_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "enabled_origin": self.enabled_origin,
            "acknowledged_partition": self.acknowledged_partition,
            "posture": "partition",
            "active": False,
            "phase": "idle",
            "window": {
                "start_local": "00:00",
                "end_local": "06:00",
                "timezone": "Australia/Brisbane",
            },
            "window_ends_at": None,
            "window_ends_in_s": None,
            "next_window_at": None,
            "pacing": "cap_first",
            "rate_cap_w": 2_500,
            "hold_rate_w": 100,
            "demand_scope": "fleet",
            "demand_threshold_w": 1_000,
            "demand_w": None,
            "demand_evidence": "missing",
            "held_intent_id": None,
            "units": [],
            "last_action": "idle",
            "last_tick_at": "2026-08-27T01:02:03+00:00",
            "reason_codes": ["disabled_by_config"],
        }


def night_toggle_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "action": "disable",
        "confirmation": "NIGHT",
        "principal": OPERATOR,
        "idempotency_key": "night-key-1",
        "request_id": "night-request-1",
    }
    kwargs.update(overrides)
    return kwargs


def make_night_rig(
    api: Any,
    *,
    acknowledged: bool = False,
    config_enabled: bool = False,
    **rig_kwargs: Any,
) -> Rig:
    control = FakeNightControl(acknowledged=acknowledged, enabled=config_enabled, origin="config")
    rig_kwargs.setdefault("units", {"pod-a": {}, "pod-b": {}})
    return make_rig(api, night=control, **rig_kwargs)


async def test_night_toggle_without_a_composed_block_is_not_commissioned(api: Any) -> None:
    rig = make_rig(api)

    with pytest.raises(Exception) as caught:
        await rig.facade.set_night_charging(**night_toggle_kwargs())

    assert _refusal_code(caught.value) == "night_charging_not_commissioned"
    assert getattr(caught.value, "details", {}) == {}


async def test_night_disable_flips_participation_audits_and_answers_the_contract_body(
    api: Any,
) -> None:
    rig = make_night_rig(api, acknowledged=True, config_enabled=True)

    result = await rig.facade.set_night_charging(**night_toggle_kwargs(action="disable"))

    assert result["feature"] == "night_charging"
    assert result["enabled"] is False
    assert result["enabled_origin"] == "runtime"
    assert result["persisted"] is False, "the non-persistence policy rides every response"
    assert result["acknowledged_partition"] is True
    assert result["night_charge_state"]["enabled"] is False
    assert result["night_charge_state"]["enabled_origin"] == "runtime"
    if rig.night:
        assert rig.night.flips == [False]
    toggled = [
        event for event in rig.audit.appended if event.event_type == "night_charging_toggled"
    ]
    assert len(toggled) == 1
    assert toggled[0].result == "disabled"
    assert toggled[0].principal == OPERATOR.subject
    assert toggled[0].reason_codes == ("disabled",)


async def test_night_enable_requires_the_arm_scope_and_an_interactive_principal(api: Any) -> None:
    rig = make_night_rig(api, acknowledged=True, config_enabled=False)
    automation = Principal(
        subject="service:automation",
        scopes=frozenset({"observe", "dispatch", "arm"}),
        interactive=False,
    )

    with pytest.raises(PermissionError, match="interactive"):
        await rig.facade.set_night_charging(
            **night_toggle_kwargs(
                action="enable", principal=automation, night_posture="PARTITION_ACKNOWLEDGED"
            )
        )
    viewer = Principal(subject="person:viewer", scopes=frozenset({"observe"}))
    with pytest.raises(PermissionError, match="arm"):
        await rig.facade.set_night_charging(
            **night_toggle_kwargs(action="enable", principal=viewer)
        )
    # The arm/disarm asymmetry: disable is safety-positive, arm scope alone.
    await rig.facade.set_night_charging(
        **night_toggle_kwargs(action="disable", principal=automation)
    )


async def test_the_first_night_enable_needs_the_partition_acknowledgement(api: Any) -> None:
    rig = make_night_rig(api, acknowledged=False, config_enabled=False)

    with pytest.raises(Exception) as caught:
        await rig.facade.set_night_charging(**night_toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "night_acknowledgement_required"
    assert getattr(caught.value, "details", {}) == {"acknowledgement": "PARTITION_ACKNOWLEDGED"}
    assert not rig.audit.appended, "a refusal appends nothing"
    if rig.night:
        assert rig.night.flips == []


async def test_the_first_night_enable_captures_the_shared_acknowledgement_durably_first(
    api: Any,
) -> None:
    """§3.2: the SECOND capture path — the first enable carries
    PARTITION_ACKNOWLEDGED, the durable fact lands FIRST under the schedule
    surface's own historical event id (one site fact, either surface's
    capture counts), then the latch flips and the toggle's own row follows."""
    rig = make_night_rig(api, acknowledged=False, config_enabled=False)

    result = await rig.facade.set_night_charging(
        **night_toggle_kwargs(action="enable", night_posture="PARTITION_ACKNOWLEDGED")
    )

    assert [event.event_type for event in rig.audit.appended] == [
        "schedule_night_windows_acknowledged",
        "night_charging_toggled",
    ]
    ack = rig.audit.appended[0]
    assert ack.event_id == NIGHT_SHARED_ACK_EVENT_ID
    assert ack.principal == OPERATOR.subject
    assert ack.result == "acknowledged"
    assert rig.audit.appended[1].result == "enabled"
    assert rig.night is not None and rig.night.flips == [True]
    assert rig.night.acknowledged_partition is True
    assert result["enabled"] is True
    assert result["enabled_origin"] == "runtime"
    assert result["acknowledged_partition"] is True

    # Once ever: a later enable needs no posture field and appends no ack row.
    rig.audit.appended.clear()
    await rig.facade.set_night_charging(
        **night_toggle_kwargs(action="disable", idempotency_key="night-key-2")
    )
    if rig.night:
        rig.night.flips.clear()
    result = await rig.facade.set_night_charging(
        **night_toggle_kwargs(action="enable", idempotency_key="night-key-3")
    )
    assert [event.event_type for event in rig.audit.appended] == [
        "night_charging_toggled",
        "night_charging_toggled",
    ]
    assert result["acknowledged_partition"] is True


async def test_a_night_acknowledgement_append_failure_refuses_the_enable(api: Any) -> None:
    rig = make_night_rig(api, acknowledged=False, config_enabled=False)
    rig.audit.failing = True

    with pytest.raises(OSError, match="audit store unavailable"):
        await rig.facade.set_night_charging(
            **night_toggle_kwargs(action="enable", night_posture="PARTITION_ACKNOWLEDGED")
        )

    assert rig.night is not None
    assert rig.night.flips == [], "the participation flag never moved"
    assert rig.night.acknowledged_partition is False, "the latch never flipped"


async def test_the_schedules_surface_capture_counts_as_the_night_acknowledgement(
    api: Any,
) -> None:
    """The one-site-fact invariant, live in-process: a night capture marks
    the schedule surface's latch too, and a schedule capture (an already-
    acknowledged surface) means the night toggle never re-prompts."""
    schedule = FakeScheduleSurface(acknowledged=False, windows=(("00:00", "20:00"),))
    rig = make_night_rig(api, acknowledged=False, config_enabled=False, schedules=schedule)

    await rig.facade.set_night_charging(
        **night_toggle_kwargs(action="enable", night_posture="PARTITION_ACKNOWLEDGED")
    )

    assert schedule.acknowledged_night_windows is True, "one site fact, both latches"

    # The inverse direction: a pre-acknowledged schedule surface means the
    # night controller's own boot-loaded latch is the same fact.
    acknowledged_schedule = FakeScheduleSurface(acknowledged=True, windows=(("00:00", "20:00"),))
    other = make_night_rig(
        api, acknowledged=True, config_enabled=False, schedules=acknowledged_schedule
    )
    result = await other.facade.set_night_charging(
        **night_toggle_kwargs(action="enable", idempotency_key="night-key-4")
    )
    assert result["enabled"] is True, "no posture field needed — the fact already exists"


async def test_night_enable_is_refused_while_a_unit_runs_under_another_intent(api: Any) -> None:
    rig = make_night_rig(
        api,
        acknowledged=True,
        config_enabled=False,
        seeded_intents=(manual_intent(api, revision=5, watts=900, unit_ids=frozenset({"pod-a"})),),
    )

    with pytest.raises(Exception) as caught:
        await rig.facade.set_night_charging(**night_toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "night_enable_refused"
    details = getattr(caught.value, "details", {})
    assert details["reasons"] == ["unit_active_under_intent"]
    assert details["unit_ids"] == ["pod-a"]
    assert details["stop_ids"] == []
    assert rig.night is not None and rig.night.flips == []


async def test_night_enable_is_refused_while_a_latched_stop_holds_and_disable_never_is(
    api: Any,
) -> None:
    rig = make_night_rig(api, acknowledged=True, config_enabled=False)
    stop = await rig.facade.emergency_stop(
        unit_ids=["pod-a", "pod-b"],
        reason="latched for the night refusal scenario",
        principal=OPERATOR,
        idempotency_key="night-stop-key-1",
        request_id="night-stop-request-1",
    )

    with pytest.raises(Exception) as caught:
        await rig.facade.set_night_charging(**night_toggle_kwargs(action="enable"))

    assert _refusal_code(caught.value) == "night_enable_refused"
    details = getattr(caught.value, "details", {})
    assert details["reasons"] == ["latched_stop_holds"]
    assert details["stop_ids"] == [stop["stop_id"]]

    # Disable is never refused — stopping is the safety-positive direction.
    rig.audit.appended.clear()
    result = await rig.facade.set_night_charging(**night_toggle_kwargs(action="disable"))
    assert result["enabled"] is False


async def test_the_night_toggle_validates_its_literals_at_the_facade_too(api: Any) -> None:
    rig = make_night_rig(api, acknowledged=True, config_enabled=True)
    with pytest.raises(ValueError, match="action"):
        await rig.facade.set_night_charging(**night_toggle_kwargs(action="pause"))
    with pytest.raises(ValueError, match="confirmation"):
        await rig.facade.set_night_charging(**night_toggle_kwargs(confirmation="EXCESS"))
    with pytest.raises(ValueError, match="night_posture"):
        await rig.facade.set_night_charging(
            **night_toggle_kwargs(action="disable", night_posture="YIELDED")
        )
    assert rig.night is not None and rig.night.flips == []


async def test_the_snapshot_carries_night_charge_state_exactly_when_composed(api: Any) -> None:
    rig = make_night_rig(api, acknowledged=False, config_enabled=False)
    snapshot = await rig.facade.snapshot(principal=OPERATOR)
    assert "night_charge_state" in snapshot
    assert snapshot["night_charge_state"]["enabled"] is False
    assert snapshot["night_charge_state"]["reason_codes"] == ["disabled_by_config"]

    bare = make_rig(api)
    absent = await bare.facade.snapshot(principal=OPERATOR)
    assert "night_charge_state" not in absent, "absent block = byte-identical snapshot"


async def test_submit_night_intent_pins_source_prefix_and_per_unit_watts(api: Any) -> None:
    """The `submit_advisory_intent` twin (§2.1): source pinned OPTIMIZER,
    intent-id prefix `night-`, the per-battery watt form native, and the same
    audit/publication contract — a night charge is an ordinary intent."""
    from types import SimpleNamespace

    rig = make_rig(api)

    principal = SimpleNamespace(
        subject="energypod:night-adviser",
        scopes=frozenset({"observe", "dispatch"}),
        interactive=False,
        site_id=SITE_ID,
    )
    result = await rig.facade.submit_night_intent(
        unit_ids=["pod-a", "pod-b"],
        direction="charge",
        watts=None,
        watts_by_unit={"pod-a": 2_500, "pod-b": 100},
        ttl_s=10.0,
        principal=principal,
    )

    assert result["intent_id"].startswith("night-")
    stored = rig.intents.added[0]
    assert stored.source.value == "optimizer"
    assert stored.watts_by_unit == {"pod-a": 2_500, "pod-b": 100}
    assert stored.watts == 2_600
    assert stored.actor_identity == "energypod:night-adviser"
    accepted = [event for event in rig.audit.appended if event.event_type == "intent_accepted"]
    assert accepted[0].source.value == "optimizer"
    published = [body for body in rig.bus.published if body["type"] == "intent.accepted"]
    assert published[0]["payload"]["watts_by_unit"] == {"pod-a": 2_500, "pod-b": 100}


# --- DESIGN_SCHEDULES §5 B3: the schedule facade surface ------------------------


def _scheduling() -> Any:
    import energypod.application.scheduling as scheduling

    return scheduling


class FakeScheduleStore:
    """In-memory singleton plan store mirroring the SQLite CAS contract."""

    def __init__(self, plan: Any = None) -> None:
        self.plan = plan

    def get(self) -> Any:
        return self.plan

    def replace(self, *, expected_version: int, replacement: Any) -> None:
        from energypod.domain.schedule import ScheduleVersionConflict

        actual = 0 if self.plan is None else self.plan.version
        if actual != expected_version or replacement.version != expected_version + 1:
            raise ScheduleVersionConflict(expected_version, actual)
        self.plan = replacement


class FakeScheduleSurface:
    """Inline stand-in for the composed ScheduleSurfaceControl port."""

    def __init__(
        self,
        *,
        windows: tuple[tuple[str, str], ...] = (("06:00", "20:00"),),
        acknowledged: bool = False,
        plan: Any = None,
        ttl_s: float = 10.0,
    ) -> None:
        scheduling = _scheduling()
        self.policy = scheduling.SchedulePolicy(
            allowed_windows_local=tuple(
                (scheduling.parse_hhmm(a), scheduling.parse_hhmm(b)) for a, b in windows
            ),
            intent_ttl_s=ttl_s,
            timezone="Australia/Brisbane",
        )
        self.store = FakeScheduleStore(plan)
        self._acknowledged = acknowledged
        self.ack_flips = 0

    @property
    def acknowledged_night_windows(self) -> bool:
        return self._acknowledged

    def mark_night_acknowledged(self) -> None:
        self.ack_flips += 1
        self._acknowledged = True

    async def get_plan(self) -> Any:
        return self.store.get()

    async def replace_plan(self, *, expected_version: int, replacement: Any) -> None:
        self.store.replace(expected_version=expected_version, replacement=replacement)

    def state_payload(self) -> dict[str, Any]:
        return {
            "version": None if self.store.plan is None else self.store.plan.version,
            "active": False,
            "entry_id": None,
            "held_intent_id": None,
            "ends_at": None,
            "ends_in_s": None,
            "next": None,
            "posture": self.policy.posture,
            "last_action": "idle",
            "last_tick_at": "2026-08-21T01:02:03+00:00",
            "reason_codes": ["no_window_open"],
        }


def make_schedule_rig(
    api: Any,
    surface: FakeScheduleSurface | None = None,
    **rig_kwargs: Any,
) -> tuple[Rig, FakeScheduleSurface]:
    control = surface or FakeScheduleSurface()
    rig_kwargs.setdefault("units", {"pod-a": {}, "pod-b": {}})
    return make_rig(api, schedules=control, **rig_kwargs), control


def wire_entry(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "entry_id": "day-charge",
        "days": ["fri"],
        "start_local": "09:00",
        "end_local": "17:00",
        "action": "charge",
        "watts": 1200,
        "unit_ids": ["pod-a"],
        "effective_from": "2026-01-01",
        "effective_until": "2026-12-31",
        "priority": 0,
        "enabled": True,
    }
    values.update(overrides)
    return values


def publish_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "principal": OPERATOR,
        "expected_version": None,
        "timezone": "Australia/Brisbane",
        "entries": [wire_entry()],
        "night_posture": None,
        "idempotency_key": "publish-key-1",
        "request_id": "request-p1",
    }
    values.update(overrides)
    return values


def _refusal_code_of(error: BaseException) -> tuple[str, dict[str, Any]]:
    return str(getattr(error, "code", "")), dict(getattr(error, "details", {}) or {})


async def test_schedule_surface_without_a_composed_block_is_not_commissioned(api: Any) -> None:
    rig = make_rig(api)

    with pytest.raises(Exception) as caught_get:
        await rig.facade.get_schedule(principal=OPERATOR)
    with pytest.raises(Exception) as caught_put:
        await rig.facade.replace_schedule(**publish_kwargs())

    assert _refusal_code_of(caught_get.value)[0] == "schedule_not_commissioned"
    assert _refusal_code_of(caught_put.value)[0] == "schedule_not_commissioned"


async def test_get_schedule_serves_the_contract_view_before_the_first_publish(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)

    view = await rig.facade.get_schedule(principal=OPERATOR)

    assert view["plan"] is None
    assert view["policy"] == {
        "posture": "yield",
        "allowed_windows_local": [["06:00", "20:00"]],
        "intent_ttl_s": 10.0,
    }
    assert view["acknowledged_night_windows"] is False
    assert view["next_action"] is None
    assert rig.intents.added == [], "a read view never triggers control"


async def test_get_schedule_answers_the_stored_plan_and_next_action(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)
    await rig.facade.replace_schedule(**publish_kwargs())

    view = await rig.facade.get_schedule(principal=OPERATOR)

    assert view["plan"] is not None and view["plan"]["version"] == 1
    assert view["plan"]["timezone"] == "Australia/Brisbane"
    entry = view["plan"]["entries"][0]
    assert entry["entry_id"] == "day-charge"
    assert entry["days"] == ["fri"]
    assert entry["watts"] == 1200
    assert "watts_by_unit" not in entry
    assert view["next_action"]["entry_id"] == "day-charge"
    assert "starts_at" in view["next_action"] and "starts_in_s" in view["next_action"]


async def test_replace_schedule_requires_dispatch_and_an_interactive_principal(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)
    viewer = Principal(subject="person:viewer", scopes=frozenset({"observe"}))
    automation = Principal(
        subject="service:automation", scopes=frozenset({"observe", "dispatch"}), interactive=False
    )

    # GET is observe-only; PUT needs dispatch AND an interactive principal.
    await rig.facade.get_schedule(principal=viewer)
    with pytest.raises(PermissionError):
        await rig.facade.replace_schedule(**publish_kwargs(principal=viewer))
    with pytest.raises(PermissionError):
        await rig.facade.replace_schedule(**publish_kwargs(principal=automation))


async def test_first_publish_stores_the_plan_audits_and_publishes_the_replacement(
    api: Any,
) -> None:
    rig, surface = make_schedule_rig(api)

    result = await rig.facade.replace_schedule(**publish_kwargs())

    assert result["version"] == 1
    assert result["diff"] == {
        "added": ["day-charge"],
        "removed": [],
        "changed": [],
        "timezone_changed": False,
    }
    assert result["acknowledged_night_windows"] is False
    assert surface.store.plan is not None and surface.store.plan.version == 1
    replaced = [event for event in rig.audit.appended if event.event_type == "schedule_replaced"]
    assert len(replaced) == 1
    assert replaced[0].result == "replaced"
    assert replaced[0].principal == OPERATOR.subject
    assert replaced[0].correlation_id == "facade:schedule_replaced:request-p1"
    announced = [body for body in rig.bus.published if body["type"] == "schedule.replaced"]
    assert len(announced) == 1
    assert announced[0]["payload"]["principal"] == OPERATOR.subject
    assert announced[0]["payload"]["version"] == 1
    assert announced[0]["payload"]["diff"]["added"] == ["day-charge"]


async def test_publish_carries_per_unit_watts_as_the_native_form(api: Any) -> None:
    rig, surface = make_schedule_rig(api)

    result = await rig.facade.replace_schedule(
        **publish_kwargs(
            entries=[
                wire_entry(
                    watts=None,
                    watts_by_unit={"pod-a": 800, "pod-b": 700},
                    unit_ids=["pod-a", "pod-b"],
                )
            ],
        )
    )

    stored = surface.store.plan.entries[0]
    assert dict(stored.watts_by_unit or {}) == {"pod-a": 800, "pod-b": 700}
    assert stored.watts == 1500
    assert result["plan"]["entries"][0]["watts_by_unit"] == {"pod-a": 800, "pod-b": 700}
    assert "watts" not in result["plan"]["entries"][0]


@pytest.mark.parametrize(
    "dates",
    [
        None,  # both bounds absent — the editor's normal case
        {"effective_from": None, "effective_until": None},  # explicit nulls
        {"effective_from": None},  # half-open: no start bound
        {"effective_until": None},  # half-open: no end bound
    ],
)
async def test_publish_without_effective_dates_is_open_bounded_and_echoes_null(
    api: Any, dates: dict[str, Any] | None
) -> None:
    # DESIGN_SCHEDULES §1: the effective date range is OPTIONAL. An entry
    # published without a bound (the editor's normal case — the fields sit
    # empty) resolves onto the domain's open bounds and echoes null on the
    # wire, so the editor's optional date fields stay empty on reload and the
    # entry never silently expires.
    from energypod.domain.schedule import OPEN_EFFECTIVE_FROM, OPEN_EFFECTIVE_UNTIL

    rig, surface = make_schedule_rig(api)

    entry = {
        key: value
        for key, value in wire_entry().items()
        if key not in ("effective_from", "effective_until")
    }
    entry.update(dates or {})
    result = await rig.facade.replace_schedule(**publish_kwargs(entries=[entry]))

    expected_from = OPEN_EFFECTIVE_FROM if entry.get("effective_from") is None else date(2026, 1, 1)
    expected_until = (
        OPEN_EFFECTIVE_UNTIL if entry.get("effective_until") is None else date(2026, 12, 31)
    )
    stored = surface.store.plan.entries[0]
    assert stored.effective_from == expected_from
    assert stored.effective_until == expected_until
    wire = result["plan"]["entries"][0]
    assert wire["effective_from"] == (
        None if expected_from == OPEN_EFFECTIVE_FROM else "2026-01-01"
    )
    assert wire["effective_until"] == (
        None if expected_until == OPEN_EFFECTIVE_UNTIL else "2026-12-31"
    )
    assert result["next_action"] is not None, "an open-bounded entry always occurs again"
    view = await rig.facade.get_schedule(principal=OPERATOR)
    assert view["plan"]["entries"][0]["effective_from"] == wire["effective_from"]
    assert view["plan"]["entries"][0]["effective_until"] == wire["effective_until"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"watts": 1500, "watts_by_unit": {"pod-a": 1500}},
        {"watts": None},
        {"action": "idle", "watts": 100},
        {"action": "idle", "watts": 0, "watts_by_unit": {"pod-a": 0}},
        {"unit_ids": ["pod-ghost"]},
        {"unit_ids": []},
        {"days": ["monday"]},
        {"days": []},
        {"start_local": "9:00"},
        {"start_local": "25:00"},
        {"end_local": "09:00:00+10:00"},
        {"effective_from": "2026-13-01"},
        {"priority": "high"},
        {"enabled": "yes"},
        {"watts_by_unit": {"pod-a": 0}, "watts": None},
        {"watts_by_unit": {"pod-b": 900}, "watts": None},
    ],
)
async def test_per_entry_validation_errors_name_the_offending_entry(
    api: Any, mutation: dict[str, Any]
) -> None:
    rig, surface = make_schedule_rig(api)

    with pytest.raises(ValueError) as caught:
        await rig.facade.replace_schedule(**publish_kwargs(entries=[wire_entry(**mutation)]))

    errors = getattr(caught.value, "entry_errors", None)
    assert errors, "the wire names the offending entry on every 422"
    assert errors[0]["entry_id"] == "day-charge"
    assert surface.store.plan is None
    assert rig.audit.appended == []


async def test_plan_level_validation_names_the_plan_not_an_entry(api: Any) -> None:
    rig, surface = make_schedule_rig(api)

    with pytest.raises(ValueError) as caught:
        await rig.facade.replace_schedule(
            **publish_kwargs(
                entries=[
                    wire_entry(entry_id="same", start_local="09:00", end_local="11:00"),
                    wire_entry(entry_id="same", start_local="12:00", end_local="13:00"),
                ]
            )
        )

    assert caught.value.entry_errors[0]["entry_id"] is None
    assert surface.store.plan is None

    with pytest.raises(ValueError) as overlap:
        await rig.facade.replace_schedule(
            **publish_kwargs(
                entries=[
                    wire_entry(entry_id="a", start_local="09:00", end_local="11:00"),
                    wire_entry(entry_id="b", start_local="10:00", end_local="12:00"),
                ]
            )
        )
    message = overlap.value.entry_errors[0]["message"]
    assert "overlap" in message


async def test_an_unknown_timezone_is_a_validation_error(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)

    with pytest.raises(ValueError):
        await rig.facade.replace_schedule(**publish_kwargs(timezone="Australia/NotAZone"))


async def test_enabled_entries_outside_the_allowed_windows_are_refused(api: Any) -> None:
    rig, surface = make_schedule_rig(api)

    with pytest.raises(Exception) as caught:
        await rig.facade.replace_schedule(
            **publish_kwargs(entries=[wire_entry(start_local="22:30", end_local="06:00")])
        )

    code, details = _refusal_code_of(caught.value)
    assert code == "schedule_window_not_allowed"
    assert details["posture"] == "yield"
    assert details["allowed_windows_local"] == [["06:00", "20:00"]]
    assert details["offending"] == [
        {"entry_id": "day-charge", "start_local": "22:30", "end_local": "06:00"}
    ]
    assert surface.store.plan is None
    assert rig.audit.appended == []


async def test_disabled_entries_are_exempt_from_the_window_gate(api: Any) -> None:
    rig, surface = make_schedule_rig(api)

    await rig.facade.replace_schedule(
        **publish_kwargs(
            entries=[wire_entry(start_local="22:30", end_local="06:00", enabled=False)]
        )
    )

    assert surface.store.plan is not None, "a disabled entry commands nothing"


def _night_surface() -> FakeScheduleSurface:
    return FakeScheduleSurface(windows=(("00:00", "06:00"), ("06:00", "20:00")), acknowledged=False)


async def test_first_night_publish_requires_the_one_time_acknowledgement(api: Any) -> None:
    surface = _night_surface()
    rig, _control = make_schedule_rig(api, surface)
    night = wire_entry(start_local="00:01", end_local="05:59")

    with pytest.raises(Exception) as caught:
        await rig.facade.replace_schedule(**publish_kwargs(entries=[night]))

    code, details = _refusal_code_of(caught.value)
    assert code == "night_posture_acknowledgement_required"
    assert details == {"acknowledgement": "PARTITION_ACKNOWLEDGED"}
    assert surface.store.plan is None


async def test_the_acknowledgement_lands_durably_first_and_only_once(api: Any) -> None:
    surface = _night_surface()
    rig, _control = make_schedule_rig(api, surface)
    night = wire_entry(start_local="00:01", end_local="05:59")

    first = await rig.facade.replace_schedule(
        **publish_kwargs(entries=[night], night_posture="PARTITION_ACKNOWLEDGED")
    )

    from energypod.application.service import SCHEDULE_NIGHT_ACK_EVENT_ID

    ack_rows = [
        event
        for event in rig.audit.appended
        if event.event_type == "schedule_night_windows_acknowledged"
    ]
    assert len(ack_rows) == 1
    assert ack_rows[0].event_id == SCHEDULE_NIGHT_ACK_EVENT_ID
    assert ack_rows[0].result == "acknowledged"
    assert first["acknowledged_night_windows"] is True

    second = await rig.facade.replace_schedule(
        **publish_kwargs(expected_version=1, entries=[night], night_posture=None)
    )
    assert second["acknowledged_night_windows"] is True
    assert surface.ack_flips == 1


async def test_an_audit_failure_refuses_the_night_publish_with_nothing_consumed(
    api: Any,
) -> None:
    surface = _night_surface()
    rig, _control = make_schedule_rig(api, surface)
    rig.audit.failing = True
    night = wire_entry(start_local="00:01", end_local="05:59")

    with pytest.raises(OSError):
        await rig.facade.replace_schedule(
            **publish_kwargs(entries=[night], night_posture="PARTITION_ACKNOWLEDGED")
        )

    assert surface.store.plan is None
    assert surface.acknowledged_night_windows is False


async def test_a_stale_expected_version_is_an_honest_conflict(api: Any) -> None:
    rig, surface = make_schedule_rig(api)
    await rig.facade.replace_schedule(**publish_kwargs())

    with pytest.raises(Exception) as caught:
        await rig.facade.replace_schedule(**publish_kwargs(expected_version=None))

    code, details = _refusal_code_of(caught.value)
    assert code == "schedule_version_conflict"
    assert details == {"current_version": 1}
    assert surface.store.plan.version == 1


async def test_a_non_null_expected_against_no_plan_is_a_conflict(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)

    with pytest.raises(Exception) as caught:
        await rig.facade.replace_schedule(**publish_kwargs(expected_version=4))

    code, details = _refusal_code_of(caught.value)
    assert code == "schedule_version_conflict"
    assert details == {"current_version": None}


async def test_the_diff_summary_names_added_removed_and_changed(api: Any) -> None:
    rig, _surface = make_schedule_rig(api)
    await rig.facade.replace_schedule(
        **publish_kwargs(
            entries=[
                wire_entry(entry_id="keep", start_local="09:00", end_local="11:00"),
                wire_entry(entry_id="old", start_local="12:00", end_local="13:00"),
            ]
        )
    )

    result = await rig.facade.replace_schedule(
        **publish_kwargs(
            expected_version=1,
            entries=[
                wire_entry(entry_id="keep", start_local="09:00", end_local="11:00", watts=900),
                wire_entry(entry_id="new", start_local="12:00", end_local="13:00"),
            ],
        )
    )

    assert result["version"] == 2
    assert result["diff"] == {
        "added": ["new"],
        "removed": ["old"],
        "changed": ["keep"],
        "timezone_changed": False,
    }
    timezone_moved = await rig.facade.replace_schedule(
        **publish_kwargs(
            expected_version=2,
            timezone="Australia/Perth",
            entries=[
                wire_entry(entry_id="keep", start_local="09:00", end_local="11:00", watts=900)
            ],
        )
    )
    assert timezone_moved["diff"]["timezone_changed"] is True


async def test_a_failed_record_after_commit_restores_the_prior_plan(api: Any) -> None:
    rig, surface = make_schedule_rig(api)
    await rig.facade.replace_schedule(**publish_kwargs())
    original = surface.store.plan
    rig.audit.failing = True

    with pytest.raises(OSError):
        await rig.facade.replace_schedule(
            **publish_kwargs(expected_version=1, entries=[wire_entry(watts=900)])
        )

    assert surface.store.plan.version == 3, "compensated to a fresh version of the prior plan"
    assert surface.store.plan.entries == original.entries
    assert surface.store.plan.timezone == original.timezone


async def test_a_failed_record_after_the_first_publish_restores_an_empty_plan(api: Any) -> None:
    rig, surface = make_schedule_rig(api)
    rig.audit.fail_first_appends = 1

    with pytest.raises(OSError):
        await rig.facade.replace_schedule(**publish_kwargs())

    assert surface.store.plan is not None
    assert surface.store.plan.entries == (), "the nearest inverse the CAS port allows: off"


async def test_the_snapshot_carries_schedule_state_exactly_when_composed(api: Any) -> None:
    rig, surface = make_schedule_rig(api)

    snapshot = await rig.facade.snapshot(principal=OPERATOR)

    assert snapshot["schedule_state"] == surface.state_payload()
    bare = await make_rig(api).facade.snapshot(principal=OPERATOR)
    assert "schedule_state" not in bare


def _runner_principal() -> Principal:
    return Principal(
        subject="energypod:schedule-runner",
        scopes=frozenset({"observe", "dispatch"}),
        interactive=False,
    )


async def test_submit_schedule_intent_is_the_advisory_twin_with_schedule_mintage(
    api: Any,
) -> None:
    rig = make_rig(api)

    result = await rig.facade.submit_schedule_intent(
        unit_ids=["pod-a", "pod-b"],
        direction="charge",
        watts=2000,
        ttl_s=10.0,
        principal=_runner_principal(),
    )

    assert result["intent_id"].startswith("schedule-")
    stored = rig.intents.added[-1]
    assert stored.source.value == "schedule"
    assert stored.watts == 2000
    assert rig.audit.appended[-1].event_type == "intent_accepted"
    assert rig.audit.appended[-1].source.value == "schedule"
    announced = [body for body in rig.bus.published if body["type"] == "intent.accepted"]
    assert announced[-1]["payload"]["principal"] == "energypod:schedule-runner"


async def test_submit_schedule_intent_carries_per_unit_watts_and_compensates(api: Any) -> None:
    rig = make_rig(api)

    result = await rig.facade.submit_schedule_intent(
        unit_ids=["pod-a", "pod-b"],
        direction="charge",
        watts=None,
        watts_by_unit={"pod-a": 600, "pod-b": 700},
        ttl_s=10.0,
        principal=_runner_principal(),
    )

    stored = rig.intents.added[-1]
    assert dict(stored.watts_by_unit or {}) == {"pod-a": 600, "pod-b": 700}
    assert stored.watts == 1300
    assert result["requested"]["watts_by_unit"] == {"pod-a": 600, "pod-b": 700}

    failing = await rig.facade.submit_schedule_intent(
        unit_ids=["pod-a"],
        direction="charge",
        watts=500,
        ttl_s=10.0,
        principal=_runner_principal(),
    )
    assert failing["intent_id"] != stored.id
    live_before = {intent.id for intent in rig.intents.added} - set(rig.intents.removed)
    rig.audit.failing = True
    with pytest.raises(OSError):
        await rig.facade.submit_schedule_intent(
            unit_ids=["pod-b"],
            direction="charge",
            watts=500,
            ttl_s=10.0,
            principal=_runner_principal(),
        )
    live_after = {intent.id for intent in rig.intents.added} - set(rig.intents.removed)
    assert live_after == live_before, "a submission whose record failed leaves nothing stored"
