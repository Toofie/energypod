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
import contextlib
import importlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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


@dataclass(frozen=True)
class Capability:
    unit_id: str
    direction: str
    watts: int
    not_before_mono: float = 0.0
    expires_at_mono: float = math.inf


def good_quality() -> dict[str, str]:
    return {field: "good" for field in QUALITY_FIELDS}


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
    def __init__(self, latest: Mapping[str, Telemetry] | None = None) -> None:
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

    async def append(self, event: Any) -> None:
        if self.history is not None:
            self.history.append("audit")
        if self.failing:
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
        arm_error: BaseException | None = None,
        zero_error: BaseException | None = None,
    ) -> None:
        self.unit_id = unit_id
        self.lifecycle = lifecycle
        self.disarmed_lifecycle = lifecycle if disarmed_lifecycle is None else disarmed_lifecycle
        self.armed_lifecycle = armed_lifecycle
        self.qualified = qualified
        self.inhibit_latched = inhibit_latched
        self.arm_error = arm_error
        self.zero_error = zero_error
        self.history = history

    async def arm(self) -> None:
        self.history.append(f"arm:{self.unit_id}")
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
        )
    except (ImportError, AttributeError) as error:
        pytest.fail(
            f"application service facade contract is not implemented: {error}", pytrace=False
        )


def make_rig(
    api: Any,
    *,
    units: Mapping[str, Mapping[str, Any]] | None = None,
    telemetry: Mapping[str, Telemetry] | None = None,
    capabilities: Mapping[str, Capability] | None = None,
    seeded_intents: tuple[Any, ...] = (),
    audit_events: tuple[Any, ...] = (),
    bus_sequence: int = 0,
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
            arm_error=spec.get("arm_error"),
            zero_error=spec.get("zero_error"),
        )
        for unit_id, spec in specs.items()
    }
    intents = FakeIntentRepository(seeded_intents)
    observations = FakeObservationRepository(telemetry)
    authorizations = FakeAuthorizationRepository(capabilities, now_mono=clock.monotonic())
    audit = FakeAuditRepository(audit_events, history)
    bus = FakeEventBus(bus_sequence, history)
    coordinator = RecordingCoordinator(api, history)
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


async def test_snapshot_is_read_only_and_never_triggers_control(api: Any) -> None:
    rig = make_rig(api)

    await rig.facade.snapshot(principal=OPERATOR)

    assert rig.audit.appended == []
    assert rig.bus.published == []
    assert rig.intents.added == []
    assert rig.authorizations.published == []
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
