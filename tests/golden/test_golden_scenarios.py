"""Golden end-to-end scenarios over the real composed simulate-mode runtime.

These scenarios are the integration keystone: every collaborating module is
real (``energypod.runtime.composition.build_runtime`` in simulate mode, the
control kernel, the sole-owner actors, ``EnergyServiceFacade``, ``EventBus``,
and ``energypod.simulator.*``), and the only injected test double is one
deterministic manual clock.  The composition, facade, bus, and simulator
modules do not exist yet: they are loaded lazily so this red-phase suite
collects cleanly, and every pinned scenario is an ordinary test failure until
the contract is implemented.

Scenarios read as operator/device action scripts with contracted outcomes.
Operators act only through the facade (arm, dispatch, stop, acknowledge); the
control loop advances exactly one kernel tick at a time; devices act only
through their actor (poll, heartbeat) and the per-unit simulator scenario
handle.  Golden numbers derive from the configured commissioning policy, the
configured timing budget, and the device's own reported telemetry -- never
from mirrored implementation output.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from energypod.domain import DataQuality, DecisionStatus, IntentSource, UnitLifecycle
from energypod.runtime.config import ControllerConfig

SITE_ID = "home"
UNIT_ID = "pod-a"
UNIT_IDENTITY = "SIM-POD-A-0042"
EXPECTED_CELL_COUNT = 60

# Commissioning numbers shared by every golden scenario.  The timing block
# satisfies the cross-validated control budget; the ramp limit makes one
# heartbeat's ramp allowance exactly 300 W, which is the bound the safety
# kernel alone owns (the allocator distributes capacity, not ramp rate).
# API_CONTRACTS (runtime composition) pins the mapping
# ControlPolicy.heartbeat_interval_s <- timing.control_period_s, so the
# allowance is RAMP_LIMIT_W_PER_S * CONTROL_PERIOD_S; the over-headroom
# scenario asserts that mapping explicitly before relying on the number.
CONTROL_PERIOD_S = 0.40
DEVICE_COMMAND_EXPIRY_S = 2.35
AUTHORIZATION_LIFETIME_S = 1.2
MAXIMUM_TELEMETRY_AGE_S = 2.0
STABLE_SAMPLES_TO_REARM = 2
STATIC_UNIT_LIMIT_W = 3000
FLEET_LIMIT_W = 3000
RAMP_LIMIT_W_PER_S = 750
RAMP_ALLOWANCE_W = int(RAMP_LIMIT_W_PER_S * CONTROL_PERIOD_S)

# 250 W sits inside every safety bound (static, dynamic, ramp) so a healthy
# dispatch is authorized exactly as requested; 12 kW exceeds every bound at
# once -- the one-heartbeat ramp allowance is the binding term -- so the
# kernel must bound it and audit the clamp reason.
HEALTHY_DISPATCH_W = 250
OVER_HEADROOM_REQUEST_W = 12_000
MIN_DEVICE_HEADROOM_W = 400

# Audit fields that carry per-build opaque process identity.  The determinism
# scenario excludes exactly these; every other audit fact must be identical.
PROCESS_IDENTITY_FIELDS = (
    "event_id",
    "process_instance_id",
    "intent_id",
    "correlation_id",
    "request_fingerprint",
    "response_fingerprint",
    "occurred_at",
    "monotonic_offset_s",
)

_IDLE: Any = object()
_ABSENT: Any = object()


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated operator identity used only as facade input."""

    subject: str = "person:operator"
    scopes: frozenset[str] = frozenset(
        {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
    )
    interactive: bool = True
    site_id: str = SITE_ID


OPERATOR = OperatorPrincipal()


class ManualClock:
    """The single deterministic time source the composed runtime may read."""

    def __init__(self, *, start: float = 1000.0) -> None:
        self.now = start
        self.wall = datetime(2026, 8, 22, 0, 0, 0, tzinfo=UTC)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        deadline = self.now + seconds
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((deadline, future))
        self._release_due_waiters()
        await future

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("the manual clock cannot move backwards")
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


async def _next_or_idle(iterator: Any, *, turns: int = 60) -> Any:
    """Advance one bus event without ever hanging on an idle live stream."""
    advance = asyncio.create_task(anext(iterator))
    for _ in range(turns):
        if advance.done():
            break
        await asyncio.sleep(0)
    if not advance.done():
        advance.cancel()
        with suppress(asyncio.CancelledError):
            await advance
        return _IDLE
    try:
        return advance.result()
    except StopAsyncIteration:
        return _IDLE


class BusRecorder:
    """Drains the real event bus after every scripted action."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self.events: list[dict[str, Any]] = []

    async def pump(self) -> list[dict[str, Any]]:
        after = self.events[-1]["sequence"] if self.events else 0
        iterator = self._bus.subscribe(after_sequence=after)
        fresh: list[dict[str, Any]] = []
        while True:
            event = await _next_or_idle(iterator)
            if event is _IDLE:
                break
            fresh.append(event)
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()
        for event in fresh:
            expected = self.events[-1]["sequence"] + 1 if self.events else 1
            assert isinstance(event, dict) and event.get("sequence") == expected, (
                f"the event-bus sequence must be gapless, expected {expected}: {event!r}"
            )
            self.events.append(event)
        return fresh


def fleet_config(database: Path) -> ControllerConfig:
    """One write-enabled single-unit site with the commissioning numbers."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 3,
        "mode": "write_enabled",
        "site": {
            "site_id": SITE_ID,
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [
            {
                "unit_id": UNIT_ID,
                "display_name": "Golden Pod",
                "endpoint": {"host": "192.168.1.11", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": UNIT_IDENTITY,
                "expected_cell_count": EXPECTED_CELL_COUNT,
            }
        ],
        "timing": {
            "device_command_expiry_s": DEVICE_COMMAND_EXPIRY_S,
            "device_command_expiry_evidence": ("live-trial://direction-2026-08-22/rev-1"),
            "control_period_s": CONTROL_PERIOD_S,
            "essential_read_timeout_s": 0.10,
            "kernel_timeout_s": 0.05,
            "audit_timeout_s": 0.05,
            "write_timeout_s": 0.10,
            "acknowledgement_timeout_s": 0.10,
            "maximum_jitter_s": 0.10,
            "renewal_margin_s": 0.50,
        },
        "policy": {
            "version": 3,
            "threshold_provenance": "golden://commissioning-baseline-2026-08",
            "max_fleet_charge_w": FLEET_LIMIT_W,
            "max_fleet_discharge_w": FLEET_LIMIT_W,
            "max_unit_charge_w": STATIC_UNIT_LIMIT_W,
            "max_unit_discharge_w": STATIC_UNIT_LIMIT_W,
            "minimum_soc_pct": 5.0,
            "maximum_soc_pct": 95.0,
            "minimum_cell_v": 2.80,
            "maximum_cell_v": 3.65,
            "maximum_cell_imbalance_v": 0.50,
            "minimum_temperature_c": -20.0,
            "maximum_temperature_c": 60.0,
            "maximum_soc_difference_pct": 5.0,
            "maximum_soc_jump_pct": 5.0,
            "maximum_telemetry_age_s": MAXIMUM_TELEMETRY_AGE_S,
            "maximum_cell_data_age_s": 10.0,
            "authorization_lifetime_s": AUTHORIZATION_LIFETIME_S,
            "ramp_limit_w_per_s": RAMP_LIMIT_W_PER_S,
            "stable_samples_to_rearm": STABLE_SAMPLES_TO_REARM,
            "reactive_power_limit_var": 0,
            "blocking_fault_codes": ["Stack_Fault0_3"],
            "debug_modes_enabled": False,
        },
        "authentication": {
            "enabled": True,
            "operator_credential_ref": "secret://golden/operator-credential",
            "trusted_proxy_cidrs": ["127.0.0.1/32"],
        },
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
    }
    return ControllerConfig.model_validate(payload)


def _composition_factory() -> Any:
    try:
        module = importlib.import_module("energypod.runtime.composition")
    except ImportError as error:
        pytest.fail(f"runtime composition contract is not implemented: {error}", pytrace=False)
    factory = getattr(module, "build_runtime", None)
    if not callable(factory):
        pytest.fail("energypod.runtime.composition.build_runtime is not implemented", pytrace=False)
    return factory


def compose_runtime(config: ControllerConfig, clock: ManualClock) -> Any:
    factory = _composition_factory()
    # Probe the signature without executing the factory, so a missing
    # parameter is reported as a contract gap while genuine implementation
    # errors inside a correctly shaped build propagate as ordinary failures.
    signature = inspect.signature(factory)
    takes_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    for name in ("simulate", "clock"):
        if name not in signature.parameters and not takes_keywords:
            pytest.fail(
                "build_runtime must accept simulate= plus an injected deterministic "
                f"clock= so golden scenarios step manually; no {name!r} parameter",
                pytrace=False,
            )
    runtime = factory(config, simulate=True, clock=clock)
    assert runtime.clock is clock, "the injected manual clock must be the composed clock"
    return runtime


async def compose_harness(database: Path) -> Harness:
    clock = ManualClock()
    runtime = compose_runtime(fleet_config(database), clock)
    harness = Harness(
        runtime=runtime,
        clock=clock,
        recorder=BusRecorder(runtime.event_bus),
        keys=itertools.count(1),
    )
    await harness.start_unit()
    return harness


@dataclass
class Harness:
    """Operator/device action verbs over one composed simulate runtime."""

    runtime: Any
    clock: ManualClock
    recorder: BusRecorder
    keys: Iterator[int]

    def _key(self, prefix: str) -> str:
        return f"{prefix}-{next(self.keys):04d}"

    async def start_unit(self) -> None:
        actor = self.runtime.actors[UNIT_ID]
        await actor.start()
        assert actor.lifecycle is UnitLifecycle.OBSERVE_ONLY, "boot must be observe-only"
        await self.recorder.pump()

    async def device_poll(self, *, advance_s: float = CONTROL_PERIOD_S) -> Any:
        """One device action: the unit serves a fresh telemetry poll."""
        self.clock.advance(advance_s)
        actor = self.runtime.actors[UNIT_ID]
        before = await self.runtime.observations.latest(UNIT_ID)
        await actor.poll_once()
        fresh = await self.recorder.pump()
        after = await self.runtime.observations.latest(UNIT_ID)
        assert after is not None, "a device poll must publish an observation"
        assert after.unit_id == UNIT_ID
        assert after.captured_at_mono == self.clock.monotonic(), (
            "observation capture time must follow the injected clock"
        )
        if before is not None and before.connection_epoch == after.connection_epoch:
            assert after.sequence > before.sequence, "sequences must advance within an epoch"
        assert any("observation" in event["type"] for event in fresh), (
            f"each poll must publish an observation event, saw {[e['type'] for e in fresh]}"
        )
        return after

    async def operator_arm(self) -> None:
        result = await self.runtime.facade.arm(
            unit_ids=[UNIT_ID],
            principal=OPERATOR,
            idempotency_key=self._key("arm"),
            request_id=self._key("req"),
        )
        fresh = await self.recorder.pump()
        outcomes = {unit["unit_id"]: unit["status"] for unit in result["units"]}
        assert outcomes == {UNIT_ID: "armed"}, outcomes
        assert self.runtime.actors[UNIT_ID].lifecycle is UnitLifecycle.ARMED_IDLE
        assert any("armed" in event["type"] for event in fresh), (
            f"arming must be published, saw {[e['type'] for e in fresh]}"
        )

    async def operator_dispatch(
        self, watts: int, *, direction: str = "discharge", ttl_s: float = 30.0
    ) -> dict[str, Any]:
        view = await self.runtime.facade.submit_intent(
            unit_ids=[UNIT_ID],
            direction=direction,
            watts=watts,
            ttl_s=ttl_s,
            reason="golden scenario dispatch",
            principal=OPERATOR,
            idempotency_key=self._key("intent"),
            request_id=self._key("req"),
        )
        fresh = await self.recorder.pump()
        assert view["status"] == "accepted", view
        assert view["requested"] == {"direction": direction, "watts": watts}
        assert view["authorized"] is None, "acceptance never grants authority"
        assert view["measured"] is None
        assert type(view["acceptance_revision"]) is int
        # The event-bus vocabulary pins `intent.accepted` for facade intent
        # acceptance (API_CONTRACTS, event bus).
        assert "intent.accepted" in [event["type"] for event in fresh], (
            "intent acceptance must publish the contracted intent.accepted event, "
            f"saw {[event['type'] for event in fresh]}"
        )
        return view

    async def control_tick(self) -> Any:
        """One control-loop step: a single kernel authority cycle."""
        decision = await self.runtime.kernel.tick()
        fresh = await self.recorder.pump()
        assert decision is not None, "an active intent must produce a decision"
        types = [event["type"] for event in fresh]
        assert any("decision" in kind or "audit" in kind or "control" in kind for kind in types), (
            f"each kernel decision must be published, saw {types}"
        )
        return decision

    async def device_heartbeat(self) -> None:
        await self.runtime.actors[UNIT_ID].heartbeat_once()
        await self.recorder.pump()

    async def snapshot(self) -> dict[str, Any]:
        snap = await self.runtime.facade.snapshot(principal=OPERATOR)
        assert snap["site_id"] == SITE_ID
        assert snap["snapshot_sequence"] == len(self.recorder.events), (
            "the fleet snapshot must be sequenced from the event bus"
        )
        return snap

    async def audit(self, limit: int = 32) -> list[Any]:
        result = await self.runtime.facade.recent_audit(principal=OPERATOR, limit=limit)
        return list(result["events"])

    async def control_decisions(self) -> list[Any]:
        """The audited control_decision records, newest-first."""
        return [
            event
            for event in await self.audit()
            if getattr(event, "event_type", "") == "control_decision"
        ]

    async def operator_emergency_stop(self, reason: str) -> dict[str, Any]:
        epoch_before = (await self.runtime.generation_coordinator.snapshot()).epoch
        result = await self.runtime.facade.emergency_stop(
            unit_ids=[UNIT_ID],
            reason=reason,
            principal=OPERATOR,
            idempotency_key=self._key("stop"),
            request_id=self._key("req"),
        )
        fresh = await self.recorder.pump()
        assert result["status"] == "latched", result
        assert isinstance(result["stop_id"], str) and result["stop_id"]
        assert (await self.runtime.generation_coordinator.snapshot()).epoch > epoch_before, (
            "an emergency stop must fence the fleet generation before returning"
        )
        assert any("emergency_stop" in event["type"] for event in fresh), (
            f"the latched stop must be published, saw {[e['type'] for e in fresh]}"
        )
        events = await self.audit(limit=8)
        assert any(
            "stop" in getattr(event, "event_type", "")
            or getattr(event, "source", None) is IntentSource.EMERGENCY_STOP
            for event in events
        ), "the latched stop must be audited"
        return result

    async def operator_acknowledge_stop(self, stop_id: str) -> None:
        epoch_before = (await self.runtime.generation_coordinator.snapshot()).epoch
        result = await self.runtime.facade.acknowledge_emergency_stop(
            stop_id=stop_id,
            principal=OPERATOR,
            idempotency_key=self._key("ack"),
            request_id=self._key("req"),
        )
        await self.recorder.pump()
        assert result["stop_id"] == stop_id
        assert result["status"] == "acknowledged"
        assert (await self.runtime.generation_coordinator.snapshot()).epoch == epoch_before, (
            "acknowledgement removes the latch without fencing again"
        )
        active = await self.runtime.intents.active(self.clock.monotonic())
        assert stop_id not in {getattr(intent, "id", None) for intent in active}, (
            "an acknowledged stop must be removed and unable to relatch"
        )

    def simulator(self) -> Any:
        """The per-unit simulated device, for scripted device-level actions."""
        return simulator_handle(self.runtime)

    async def generation(self) -> int:
        return (await self.runtime.generation_coordinator.snapshot()).epoch

    async def shutdown(self) -> None:
        for actor in self.runtime.actors.values():
            await actor.shutdown()
        with suppress(Exception):
            await self.recorder.pump()


def simulator_handle(runtime: Any) -> Any:
    """Resolve the composed per-unit simulator scenario hook surface.

    Only absence is a contract failure: attribute presence is probed without
    executing descriptors, and a raising getter or a broken handle propagates
    as an ordinary implementation failure.
    """
    if inspect.getattr_static(runtime, "simulators", _ABSENT) is _ABSENT:
        pytest.fail(
            "the simulate runtime must expose per-unit scenario handles at runtime.simulators",
            pytrace=False,
        )
    try:
        handle = runtime.simulators[UNIT_ID]
    except KeyError:
        pytest.fail(
            f"the simulate runtime must expose per-unit scenario handles at "
            f"runtime.simulators[{UNIT_ID!r}]",
            pytrace=False,
        )
    for hook in ("drop_link", "restore_link"):
        if not callable(inspect.getattr_static(handle, hook, None)):
            pytest.fail(f"the simulator scenario handle must provide {hook}()", pytrace=False)
    if inspect.getattr_static(handle, "connection_epoch", _ABSENT) is _ABSENT:
        pytest.fail(
            "the simulator scenario handle must expose connection_epoch",
            pytrace=False,
        )
    epoch = handle.connection_epoch
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        pytest.fail(
            "the simulator scenario handle must expose connection_epoch",
            pytrace=False,
        )
    return handle


def composed_policy(runtime: Any) -> Any:
    """Resolve the composed ControlPolicy so its timing mapping is checkable.

    API_CONTRACTS (runtime composition) grants the derivation
    ``ControlPolicy.heartbeat_interval_s`` <- ``timing.control_period_s``;
    whichever handle carries the policy, the over-headroom scenario must be
    able to verify that derivation before relying on its golden ramp number.
    """
    kernel = getattr(runtime, "kernel", None)
    for candidate in (
        getattr(runtime, "policy", None),
        getattr(kernel, "policy", None),
        getattr(kernel, "_policy", None),
    ):
        if candidate is not None:
            return candidate
    pytest.fail(
        "the composed runtime must expose its ControlPolicy (runtime.policy or "
        "the kernel policy) so heartbeat_interval_s is verifiable",
        pytrace=False,
    )


def assert_healthy_observation(observation: Any) -> None:
    """A healthy simulated unit must expose complete, qualifying telemetry."""
    assert observation.device_identity == UNIT_IDENTITY, (
        "the simulated device identity must match the configured unit binding"
    )
    assert observation.battery_watts == 0, "an idle pod must measure zero power"
    assert len(observation.cell_voltages_v) == EXPECTED_CELL_COUNT, (
        f"expected {EXPECTED_CELL_COUNT} cells, saw {len(observation.cell_voltages_v)}"
    )
    assert observation.temperatures_c, "temperatures must be present"
    assert observation.dynamic_discharge_limit_w >= MIN_DEVICE_HEADROOM_W, (
        f"a healthy simulated pod must report at least {MIN_DEVICE_HEADROOM_W} W "
        f"of discharge headroom, saw {observation.dynamic_discharge_limit_w}"
    )
    assert observation.system_soc_pct is not None and 5.0 < observation.system_soc_pct < 95.0, (
        f"dischargeable SOC required, saw {observation.system_soc_pct}"
    )
    degraded = {
        field: quality
        for field, quality in dict(observation.quality).items()
        if quality is not DataQuality.GOOD
    }
    assert not degraded, f"a healthy pod reports good quality everywhere: {degraded}"


def unit_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    units = {unit["unit_id"]: unit for unit in snapshot["units"]}
    assert UNIT_ID in units, f"the snapshot must include {UNIT_ID}: {list(units)}"
    return units[UNIT_ID]


def decision_event(events: list[Any], intent_id: str) -> Any:
    """The one canonical control_decision audit record for one intent."""
    matches = [
        event
        for event in events
        if getattr(event, "event_type", "") == "control_decision"
        and getattr(event, "intent_id", None) == intent_id
    ]
    assert len(matches) == 1, (
        f"expected exactly one control_decision for {intent_id}, "
        f"saw {len(matches)} with {[getattr(e, 'intent_id', None) for e in matches]}"
    )
    return matches[0]


def bus_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if isinstance(event, dict)]


def _first_index(types: list[str], needles: tuple[str, ...], *, after: int = 0) -> int:
    for index in range(after, len(types)):
        if any(needle in types[index] for needle in needles):
            return index
    pytest.fail(f"expected one of {needles} events on the bus, saw {types}", pytrace=False)


def audit_projection(events: list[Any]) -> list[dict[str, Any]]:
    """Audit facts minus the per-build process identity the contract allows."""
    fields = (
        "event_type",
        "unit_id",
        "connection_epoch",
        "generation",
        "cycle_id",
        "principal",
        "source",
        "policy_version",
        "configuration_version",
        "observation_sequences",
        "reason_codes",
        "requested_active_w",
        "authorized_active_w",
        "result",
        "lifecycle",
    )
    assert not set(fields) & set(PROCESS_IDENTITY_FIELDS)
    projected: list[dict[str, Any]] = []
    for event in events:
        record = {field: getattr(event, field, None) for field in fields}
        record["observation_sequences"] = dict(record["observation_sequences"] or {})
        for key in ("source", "lifecycle"):
            value = record[key]
            record[key] = getattr(value, "value", value)
        projected.append(record)
    return projected


def assert_audit_projection_is_fully_populated(projection: list[dict[str, Any]]) -> None:
    """The determinism comparison must not pass vacuously on absent facts.

    Both runs project missing attributes as ``None``, so presence and
    canonical types are asserted before the two runs are ever compared.
    """
    assert projection, "the golden script must leave an audit trail"
    decisions = [record for record in projection if record["event_type"] == "control_decision"]
    assert decisions, "the audit projection must contain control_decision records"
    for record in decisions:
        reason_codes = record["reason_codes"]
        assert isinstance(reason_codes, tuple) and all(
            isinstance(code, str) for code in reason_codes
        ), f"reason_codes must be a canonical tuple of strings: {record}"
        for field in ("cycle_id", "generation", "policy_version"):
            assert record[field] is not None, (
                f"control_decision audit events must carry {field}: {record}"
            )


async def bring_up_armed(harness: Harness) -> Any:
    """Qualify the unit with stable polls, arm it, and refresh its evidence."""
    baseline = await harness.device_poll()
    assert_healthy_observation(baseline)
    qualifying = await harness.device_poll()
    assert qualifying.sequence > baseline.sequence
    await harness.operator_arm()
    evidence = await harness.device_poll()
    assert evidence.lifecycle is UnitLifecycle.ARMED_IDLE, (
        "observations must carry the actor lifecycle so the kernel sees controllable units"
    )
    assert_healthy_observation(evidence)
    return evidence


async def run_healthy_dispatch(harness: Harness) -> dict[str, Any]:
    """Scenario 1 script: qualify, arm, dispatch, authorize, apply, measure."""
    evidence = await bring_up_armed(harness)
    view = await harness.operator_dispatch(HEALTHY_DISPATCH_W)
    await harness.control_tick()
    before_write = await harness.snapshot()
    await harness.device_heartbeat()
    measured = await harness.device_poll()
    after_write = await harness.snapshot()
    events = await harness.audit()
    return {
        "view": view,
        "evidence": evidence,
        "measured": measured,
        "before_write": before_write,
        "after_write": after_write,
        "audit": events,
        "bus": list(harness.recorder.events),
        "generation": await harness.generation(),
    }


async def test_composed_simulate_runtime_exposes_manually_drivable_handles(
    tmp_path: Path,
) -> None:
    """The runtime must be steppable by hand, without the supervisor loop."""
    clock = ManualClock()
    config = fleet_config(tmp_path / "fleet.sqlite3")
    runtime = compose_runtime(config, clock)
    try:
        assert runtime.config == config
        assert callable(runtime.kernel.tick)
        assert (await runtime.generation_coordinator.snapshot()).epoch == 0

        assert set(runtime.actors) == {UNIT_ID}
        actor = runtime.actors[UNIT_ID]
        for operation in (
            "start",
            "accept_observation",
            "arm",
            "poll_once",
            "heartbeat_once",
            "fence",
            "shutdown",
        ):
            assert callable(getattr(actor, operation, None)), operation
        assert actor.lifecycle is UnitLifecycle.BOOT, "composed boot is disarmed"

        for operation in (
            "snapshot",
            "health",
            "recent_audit",
            "submit_intent",
            "arm",
            "emergency_stop",
            "acknowledge_emergency_stop",
        ):
            assert callable(getattr(runtime.facade, operation, None)), operation

        assert callable(runtime.event_bus.subscribe)
        assert runtime.event_bus.snapshot_sequence() == 0
        for repository in ("intents", "observations", "authorizations", "audit", "schedule"):
            assert getattr(runtime, repository, None) is not None, repository

        handle = simulator_handle(runtime)
        assert isinstance(handle.connection_epoch, int)
    finally:
        for actor in runtime.actors.values():
            await actor.shutdown()


async def test_healthy_dispatch_applies_exactly_the_authorized_setpoint(
    tmp_path: Path,
) -> None:
    """Scenario 1: authorized setpoint, measured power, audit, gapless bus."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        capture = await run_healthy_dispatch(harness)
    finally:
        await harness.shutdown()

    view = capture["view"]
    assert view["expires_in_s"] == 30.0

    event = decision_event(capture["audit"], view["intent_id"])
    assert event.requested_active_w == HEALTHY_DISPATCH_W
    assert event.authorized_active_w == HEALTHY_DISPATCH_W
    assert event.reason_codes == ("safety_checks_passed",)
    assert event.result == "authorized"
    assert event.lifecycle is UnitLifecycle.ACTIVE
    # 2026-08-23 attribution wave (API_CONTRACTS, control-decision audit
    # attribution): a cycle that selected exactly one unit attributes that row
    # to the unit; only genuinely multi-unit decisions stay fleet-level null.
    assert event.unit_id == UNIT_ID, "a single-unit dispatch decision carries that unit's id"
    assert event.cycle_id and event.generation == capture["generation"]
    assert dict(event.observation_sequences) == {UNIT_ID: capture["evidence"].sequence}
    assert event.principal == OPERATOR.subject
    assert event.source is IntentSource.MANUAL

    before = unit_view(capture["before_write"])
    assert before["lifecycle"] == "armed_idle"
    assert before["requested_power"] == {"direction": "discharge", "watts": HEALTHY_DISPATCH_W}
    assert before["authorized_power"] == {"direction": "discharge", "watts": HEALTHY_DISPATCH_W}
    assert before["measured_watts"] == 0, "nothing is written before the heartbeat"
    assert before["telemetry_age_s"] is not None and before["telemetry_age_s"] <= CONTROL_PERIOD_S

    measured = capture["measured"]
    assert measured.battery_watts == float(HEALTHY_DISPATCH_W), (
        "the pod must apply exactly the authorized setpoint"
    )
    assert measured.lifecycle is UnitLifecycle.ACTIVE

    after = unit_view(capture["after_write"])
    assert after["lifecycle"] == "active"
    assert after["requested_power"] == {"direction": "discharge", "watts": HEALTHY_DISPATCH_W}
    assert after["measured_watts"] == HEALTHY_DISPATCH_W
    assert after["authorized_power"] is None, "each heartbeat consumes its single-use authority"

    events = capture["bus"]
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1)), "the bus sequence is gapless"
    types = bus_types(events)
    poll_at = _first_index(types, ("observation",))
    armed_at = _first_index(types, ("armed",), after=poll_at)
    intent_at = _first_index(types, ("intent.accepted",), after=armed_at)
    decision_at = _first_index(types, ("decision", "audit", "control"), after=intent_at)
    assert poll_at < armed_at < intent_at < decision_at, types
    assert sum(1 for kind in types if "observation" in kind) >= 4, types


async def test_over_headroom_dispatch_is_clamped_to_the_safety_bound(
    tmp_path: Path,
) -> None:
    """Scenario 2: the kernel bounds the request and audits the clamp reason."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        # The ramp allowance below is ramp_limit_w_per_s * heartbeat_interval_s,
        # and API_CONTRACTS (runtime composition) pins heartbeat_interval_s <-
        # timing.control_period_s; assert the mapping so a mis-mapped policy
        # fails here with its cause instead of as a confusing clamp mismatch.
        policy = composed_policy(harness.runtime)
        assert policy.heartbeat_interval_s == CONTROL_PERIOD_S, (
            "build_runtime must derive ControlPolicy.heartbeat_interval_s from "
            f"timing.control_period_s (API_CONTRACTS), saw {policy.heartbeat_interval_s!r}"
        )
        evidence = await bring_up_armed(harness)
        view = await harness.operator_dispatch(OVER_HEADROOM_REQUEST_W)
        await harness.control_tick()
        before_write = await harness.snapshot()
        await harness.device_heartbeat()
        measured = await harness.device_poll()
        after_write = await harness.snapshot()
        events = await harness.audit()
    finally:
        await harness.shutdown()

    expected = int(
        min(
            STATIC_UNIT_LIMIT_W,
            evidence.dynamic_discharge_limit_w,
            RAMP_ALLOWANCE_W,
        )
    )
    assert 0 < expected < OVER_HEADROOM_REQUEST_W

    event = decision_event(events, view["intent_id"])
    assert event.requested_active_w == OVER_HEADROOM_REQUEST_W
    assert event.authorized_active_w == expected
    assert event.result == "clamped"
    assert event.reason_codes == ("power_clamped",), event.reason_codes

    before = unit_view(before_write)
    assert before["requested_power"] == {"direction": "discharge", "watts": OVER_HEADROOM_REQUEST_W}
    assert before["authorized_power"] == {"direction": "discharge", "watts": expected}

    assert measured.battery_watts == float(expected), "the actor may write only the clamped value"
    after = unit_view(after_write)
    assert after["measured_watts"] == expected
    assert len({OVER_HEADROOM_REQUEST_W, expected, after["measured_watts"]}) == 2, (
        "requested, authorized, and measured power must be separate projections"
    )


async def test_emergency_stop_latches_zeroes_and_requires_exact_acknowledgement(
    tmp_path: Path,
) -> None:
    """Scenario 3: a latched stop fences, zeroes, and holds until acknowledged."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        await bring_up_armed(harness)
        await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        await harness.device_heartbeat()
        driving = await harness.device_poll()
        assert driving.battery_watts == float(HEALTHY_DISPATCH_W)

        stop = await harness.operator_emergency_stop("operator halt")
        halted = await harness.device_poll()
        assert halted.battery_watts == 0, "the bounded zero must reach the pod"

        pending = await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        fenced_evidence = await harness.device_poll()
        assert fenced_evidence.battery_watts == 0
        await harness.control_tick()
        await harness.device_heartbeat()
        fenced = await harness.device_poll()
        fenced_snapshot = await harness.snapshot()
        latched_events = await harness.audit()

        await harness.operator_acknowledge_stop(stop["stop_id"])
        await harness.device_poll()
        await harness.control_tick()
        await harness.device_heartbeat()
        resumed = await harness.device_poll()
        resumed_events = await harness.audit()
    finally:
        await harness.shutdown()

    assert fenced.battery_watts == 0, "no nonzero write is possible while latched"
    assert unit_view(fenced_snapshot)["authorized_power"] is None, (
        "the latched stop must leave no grantable authority"
    )
    stop_decisions = [
        event for event in latched_events if getattr(event, "event_type", "") == "control_decision"
    ]
    assert stop_decisions, "the stop cycle must still be audited"
    newest = stop_decisions[0]  # recent_audit is newest-first
    assert newest.source is IntentSource.EMERGENCY_STOP
    assert newest.authorized_active_w == 0

    assert resumed.battery_watts == float(HEALTHY_DISPATCH_W), (
        "acknowledgement must remove the latch and let control resume"
    )
    resumed_decision = decision_event(resumed_events, pending["intent_id"])
    assert resumed_decision.authorized_active_w == HEALTHY_DISPATCH_W
    assert resumed_decision.source is IntentSource.MANUAL


async def test_watchdog_expiry_returns_the_unit_to_idle_without_heartbeats(
    tmp_path: Path,
) -> None:
    """Scenario 4: the device's command lease expires on its own."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        await bring_up_armed(harness)
        await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        await harness.device_heartbeat()

        inside_lease = await harness.device_poll(advance_s=2.0)
        assert inside_lease.battery_watts == float(HEALTHY_DISPATCH_W), (
            "2.0 s is inside the configured 2.35 s command lease"
        )
        expired = await harness.device_poll(advance_s=2.5)
        final = await harness.snapshot()
    finally:
        await harness.shutdown()

    assert expired.battery_watts == 0, (
        "after the configured device command expiry the pod must idle itself"
    )
    assert unit_view(final)["measured_watts"] == 0


async def test_stale_safety_inputs_fail_closed_to_zero_authority(tmp_path: Path) -> None:
    """Stale telemetry must reject nonzero power and leave nothing grantable."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        await bring_up_armed(harness)
        view = await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        live = await harness.snapshot()
        assert unit_view(live)["authorized_power"] == {
            "direction": "discharge",
            "watts": HEALTHY_DISPATCH_W,
        }, "the healthy cycle must hold live authority before the evidence goes stale"

        # Let the only observation age past maximum_telemetry_age_s with no poll.
        harness.clock.advance(MAXIMUM_TELEMETRY_AGE_S + 0.5)
        decision = await harness.control_tick()
        stale_snapshot = await harness.snapshot()
        await harness.device_heartbeat()
        stale = await harness.device_poll()
        decisions = [
            event
            for event in await harness.control_decisions()
            if getattr(event, "intent_id", None) == view["intent_id"]
        ]
    finally:
        await harness.shutdown()

    assert decision.status is DecisionStatus.REJECTED, decision.status
    assert "telemetry_stale" in decision.reason_codes, decision.reason_codes
    assert all(setpoint.watts == 0 for setpoint in decision.setpoints), (
        "a rejected cycle authorizes no nonzero setpoint"
    )
    assert unit_view(stale_snapshot)["authorized_power"] is None, (
        "stale evidence must leave no grantable authority"
    )
    assert stale.battery_watts == 0, "no write may reach the pod on stale evidence"

    assert len(decisions) == 2, "the healthy and the stale cycle must both be audited"
    stale_event, healthy_event = decisions  # recent_audit is newest-first
    assert healthy_event.result == "authorized"
    assert stale_event.result == "rejected"
    assert stale_event.authorized_active_w == 0
    assert "telemetry_stale" in stale_event.reason_codes, stale_event.reason_codes


async def test_consumed_authorization_cannot_be_replayed_by_a_second_heartbeat(
    tmp_path: Path,
) -> None:
    """A heartbeat consumes the capability once; only a fresh tick re-grants."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        await bring_up_armed(harness)
        await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        await harness.device_heartbeat()
        applied = await harness.device_poll()
        assert applied.battery_watts == float(HEALTHY_DISPATCH_W)
        consumed = await harness.snapshot()
        assert unit_view(consumed)["authorized_power"] is None, (
            "the first heartbeat consumed the single-use capability"
        )

        idled = await harness.device_poll(advance_s=2.5)
        assert idled.battery_watts == 0, "the device lease lapses without a renewal write"
        ticks_before = await harness.control_decisions()

        await harness.device_heartbeat()
        replayed = await harness.device_poll()
        replay_snapshot = await harness.snapshot()
        ticks_after = await harness.control_decisions()
    finally:
        await harness.shutdown()

    assert replayed.battery_watts == 0, (
        "a second heartbeat without a fresh kernel cycle must not write again"
    )
    assert unit_view(replay_snapshot)["authorized_power"] is None, (
        "a consumed capability can never be replayed into a new grant"
    )
    assert len(ticks_after) == len(ticks_before) == 1, (
        "only a kernel tick mints control cycles; the replayed heartbeat mints none"
    )


async def test_renewal_in_one_generation_reauthorizes_with_a_fresh_cycle_id(
    tmp_path: Path,
) -> None:
    """Repeated cycles in one healthy generation are the loop's normal mode."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        await bring_up_armed(harness)
        view = await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        await harness.device_heartbeat()
        driving = await harness.device_poll()
        assert driving.battery_watts == float(HEALTHY_DISPATCH_W)
        assert driving.lifecycle is UnitLifecycle.ACTIVE
        generation = await harness.generation()
        consumed = await harness.snapshot()
        assert unit_view(consumed)["authorized_power"] is None, (
            "the first heartbeat consumed the single-use capability"
        )

        await harness.control_tick()
        generation_after = await harness.generation()
        renewed_authorization = await harness.snapshot()
        await harness.device_heartbeat()
        renewed = await harness.device_poll()
        decisions = [
            event
            for event in await harness.control_decisions()
            if getattr(event, "intent_id", None) == view["intent_id"]
        ]
    finally:
        await harness.shutdown()

    assert generation_after == generation, "a healthy renewal must not fence the generation"
    assert unit_view(renewed_authorization)["authorized_power"] == {
        "direction": "discharge",
        "watts": HEALTHY_DISPATCH_W,
    }, "the renewal cycle must publish fresh single-use authority"
    assert renewed.battery_watts == float(HEALTHY_DISPATCH_W), (
        "the renewal write must keep driving the pod at the authorized setpoint"
    )

    assert len(decisions) == 2, "each cycle audits exactly one correlated record"
    renewal, first = decisions  # recent_audit is newest-first
    assert first.result == "authorized" and renewal.result == "authorized"
    assert renewal.cycle_id != first.cycle_id, "every renewal receives a fresh cycle_id"
    assert renewal.generation == first.generation == generation
    assert renewal.authorized_active_w == HEALTHY_DISPATCH_W
    assert renewal.reason_codes == ("safety_checks_passed",)


async def test_reconnect_bumps_the_epoch_and_requires_requalification(
    tmp_path: Path,
) -> None:
    """Scenario 5: a reconnect fences stale authority and forces re-arming."""
    harness = await compose_harness(tmp_path / "fleet.sqlite3")
    try:
        evidence = await bring_up_armed(harness)
        await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        armed_snapshot = await harness.snapshot()
        tick_generation = await harness.generation()

        device = harness.simulator()
        epoch_before = device.connection_epoch
        assert evidence.connection_epoch == epoch_before
        device.drop_link()
        device.restore_link()
        assert device.connection_epoch == epoch_before + 1, (
            "a simulated transport reconnect must bump the connection epoch"
        )

        reconnected = await harness.device_poll()
        assert reconnected.connection_epoch == epoch_before + 1
        # Exact intermediate states are the actor's own state machine; the
        # granted invariants are that the fenced epoch holds no control
        # authority and that re-qualification needs the configured
        # stable-sample count of new-epoch polls.
        fenced_lifecycle = harness.runtime.actors[UNIT_ID].lifecycle
        assert fenced_lifecycle not in {UnitLifecycle.ARMED_IDLE, UnitLifecycle.ACTIVE}, (
            "the first new-epoch poll must leave the unit without control authority"
        )
        assert fenced_lifecycle is not UnitLifecycle.DISARMED, (
            "one new-epoch poll is fewer stable samples than the configured "
            f"stable_samples_to_rearm={STABLE_SAMPLES_TO_REARM}"
        )
        assert await harness.generation() > tick_generation, (
            "the reconnect must fence the generation"
        )
        refused_snapshot = await harness.snapshot()
        await harness.device_heartbeat()
        refused = await harness.device_poll()
        assert harness.runtime.actors[UNIT_ID].lifecycle is UnitLifecycle.DISARMED, (
            f"{STABLE_SAMPLES_TO_REARM} stable new-epoch polls must re-qualify the unit to disarmed"
        )
        await harness.operator_arm()
        requalified = await harness.device_poll()
        assert requalified.connection_epoch == epoch_before + 1
        assert requalified.lifecycle is UnitLifecycle.ARMED_IDLE
        await harness.operator_dispatch(HEALTHY_DISPATCH_W)
        await harness.control_tick()
        await harness.device_heartbeat()
        resumed = await harness.device_poll()
    finally:
        await harness.shutdown()

    assert unit_view(armed_snapshot)["authorized_power"] == {
        "direction": "discharge",
        "watts": HEALTHY_DISPATCH_W,
    }, "a live authorization existed before the reconnect"
    # API_CONTRACTS (unit actor): a unit fence -- including reconnect fences --
    # revokes that unit's outstanding authorizations, and the snapshot
    # projection must reflect the revocation.
    assert unit_view(refused_snapshot)["authorized_power"] is None, (
        "the reconnected epoch must refuse the stale authorization"
    )
    assert refused.battery_watts == 0, "the stale authorization must not reach the pod"
    assert resumed.battery_watts == float(HEALTHY_DISPATCH_W), (
        "re-qualification plus re-arm must restore dispatch"
    )


async def test_identical_scripts_produce_identical_audit_and_bus_streams(
    tmp_path: Path,
) -> None:
    """Scenario 6: the same script over fresh runtimes is bit-for-bit golden."""
    runs: list[dict[str, Any]] = []
    for name in ("left", "right"):
        harness = await compose_harness(tmp_path / f"{name}.sqlite3")
        try:
            capture = await run_healthy_dispatch(harness)
        finally:
            await harness.shutdown()
        runs.append(
            {
                "audit": audit_projection(capture["audit"]),
                "bus_sequences": [event["sequence"] for event in capture["bus"]],
                "bus_types": bus_types(capture["bus"]),
                "measured": capture["measured"].battery_watts,
            }
        )
        assert_audit_projection_is_fully_populated(runs[-1]["audit"])

    left, right = runs
    assert left["audit"] == right["audit"], (
        "identical scripts must produce identical audit facts "
        "(excluding per-build process identity)"
    )
    assert left["bus_types"] == right["bus_types"], f"{left['bus_types']} != {right['bus_types']}"
    assert left["bus_sequences"] == right["bus_sequences"]
    assert left["bus_sequences"] == list(range(1, len(left["bus_sequences"]) + 1))
    assert left["measured"] == right["measured"] == float(HEALTHY_DISPATCH_W)


# --- energy scorecard golden reference (DESIGN_ENERGY_SCORECARD section 9, E6) --
#
# The deferred golden energy/SOC scenario (DEFERRED_FINDINGS item 3): a
# scripted import-day / export-day / mixed-day-with-mid-day-restart against
# EXACT expected kWh.  Every figure derives from the scripted trace alone:
# 36 s at 10,000 W is exactly 360,000 Ws = 0.1 kWh -- one device-counter
# quantum per interval for the pod's own accumulation, and one exact step of
# the controller's zero-order-hold integration between consecutive polls.
# The polls run through the composed actor (the real decode publishes the
# observations); the accounting runs through the composed accountant's own
# tick -- the same call the fleet loop makes.

_ENERGY_INTERVAL_S = 36.0
_ENERGY_STEP_W = 10_000
_ENERGY_STEP_KWH = _ENERGY_STEP_W * _ENERGY_INTERVAL_S / 3_600_000.0  # 0.1 exactly
_BRISBANE_MIDNIGHT_UTC = datetime(2026, 8, 21, 14, 0, 0, tzinfo=UTC)  # Aug 22 00:00 AEST


def _energy_fleet_config(database: Path, *, timezone: str = "Australia/Brisbane") -> Any:
    """The golden fleet config with the energy_scorecard block composed.

    The gap (60 s) exceeds the control period and every scripted interval
    (36 s); the coverage threshold is the commissioned default.
    """
    base = fleet_config(database).model_dump(mode="json")
    base["site"]["timezone"] = timezone
    base["energy_scorecard"] = {
        "integration_max_gap_s": 60.0,
        "min_day_coverage_pct": 95.0,
    }
    return ControllerConfig.model_validate(base)


async def _energy_poll_and_tick(runtime: Any, clock: ManualClock, advance_s: float) -> Any:
    clock.advance(advance_s)
    actor = runtime.actors[UNIT_ID]
    await actor.poll_once()
    latest = await runtime.observations.all_latest()
    await runtime.energy_accountant.tick(latest, adviser_active_targets=frozenset())
    return await runtime.observations.latest(UNIT_ID)


async def test_energy_scorecard_golden_days(tmp_path: Path) -> None:
    """Import day -> export day -> mixed day with a mid-day restart, every
    kWh figure exact against the scripted trace."""
    clock = ManualClock()
    clock.wall = _BRISBANE_MIDNIGHT_UTC  # local midnight, Aug 22, Brisbane
    runtime = compose_runtime(_energy_fleet_config(tmp_path / "energy-golden.sqlite3"), clock)
    await runtime.actors[UNIT_ID].start()
    pod = runtime.simulators[UNIT_ID]
    accountant = runtime.energy_accountant
    assert accountant is not None

    async def script_and_sample(watts: int, intervals: int) -> None:
        pod.script_grid_power_w(watts)
        for _ in range(intervals):
            await _energy_poll_and_tick(runtime, clock, _ENERGY_INTERVAL_S)
        pod.script_grid_power_w(0)

    # --- day 1 (Aug 22): a pure import day, fully sampled from midnight ---
    # The CT word is scripted BEFORE the midnight anchor poll so every one of
    # the ten intervals integrates -10,000 W.
    pod.script_grid_power_w(-_ENERGY_STEP_W)
    await _energy_poll_and_tick(runtime, clock, 0.0)
    for _ in range(10):
        await _energy_poll_and_tick(runtime, clock, _ENERGY_INTERVAL_S)
    pod.script_grid_power_w(0)

    day1 = accountant.today_summary()
    unit = day1.units[UNIT_ID]
    assert unit.grid_import_kwh == pytest.approx(10 * _ENERGY_STEP_KWH), "1.0 kWh"
    assert unit.grid_export_kwh == pytest.approx(0.0)
    # The device counters accrued the same ten quanta over the same window.
    assert day1.counter_cross_check is not None
    assert day1.counter_cross_check.grid_a_delta_kwh == pytest.approx(1.0)
    assert day1.counter_cross_check.grid_b_delta_kwh == pytest.approx(0.0)
    # No export side -> the day cannot discriminate (A-1, section 3).
    assert day1.counter_cross_check.discriminating is False
    assert unit.coverage_pct == pytest.approx(100.0, abs=0.01)

    # --- day 2 (Aug 23): a pure export day; the roll lands at local midnight.
    # The export word is scripted BEFORE the midnight anchor poll (the
    # inter-day jump lands in the new day's counter BASELINE only -- a
    # baseline is never a delta).
    pod.script_grid_power_w(_ENERGY_STEP_W)
    await _energy_poll_and_tick(
        runtime, clock, 86_400.0 - 10 * _ENERGY_INTERVAL_S
    )  # exactly local midnight Aug 23
    rolled = runtime.energy_accountant.live_day
    assert rolled is not None and rolled.isoformat() == "2026-08-23"
    # Day 1 is durable and complete.
    days = await runtime.facade.get_energy_days(principal=OPERATOR, limit=8)
    assert [day["date"] for day in days["days"]] == ["2026-08-22"]
    assert days["days"][0]["kind"] == "complete"
    assert days["grid_counter_roles"] == "unpinned"

    for _ in range(10):
        await _energy_poll_and_tick(runtime, clock, _ENERGY_INTERVAL_S)
    pod.script_grid_power_w(-_ENERGY_STEP_W)
    day2_live = accountant.today_summary()
    assert day2_live.units[UNIT_ID].grid_export_kwh == pytest.approx(1.0)
    assert day2_live.units[UNIT_ID].grid_import_kwh == pytest.approx(0.0)

    # --- day 3 (Aug 24): import morning, controller restart, export evening ---
    await _energy_poll_and_tick(
        runtime, clock, 86_400.0 - 10 * _ENERGY_INTERVAL_S
    )  # exactly local midnight Aug 24 (import already scripted)
    assert accountant.live_day is not None and accountant.live_day.isoformat() == "2026-08-24"
    for _ in range(5):
        await _energy_poll_and_tick(runtime, clock, _ENERGY_INTERVAL_S)
    pod.script_grid_power_w(0)

    # The restart: a FRESH accountant over the SAME durable ledger and the
    # same fleet -- the exact seam the fleet loop drives -- composed 360 s
    # later (an outage above the commissioned 60 s gap: excluded from the
    # integration, never interpolated; the device itself kept counting, but
    # the CT word was 0 through the outage so nothing diverged).
    from energypod.application.energy import EnergyAccountant, EnergyAccountingSettings

    restarted_accountant = EnergyAccountant(
        unit_ids=(UNIT_ID,),
        timezone="Australia/Brisbane",
        settings=EnergyAccountingSettings(integration_max_gap_s=60.0, min_day_coverage_pct=95.0),
        clock=runtime.clock,
        ledger=runtime.energy_ledger,
        bus=runtime.event_bus,
    )
    assert restarted_accountant.live_day is not None
    assert restarted_accountant.live_day.isoformat() == "2026-08-24", (
        "the durable baseline restored the live day"
    )
    restored = restarted_accountant.today_summary()
    assert restored.units[UNIT_ID].grid_import_kwh == pytest.approx(0.5), (
        "the morning's integration survived the restart"
    )

    async def restarted_poll_and_tick(advance_s: float) -> None:
        clock.advance(advance_s)
        await runtime.actors[UNIT_ID].poll_once()
        latest = await runtime.observations.all_latest()
        await restarted_accountant.tick(latest, adviser_active_targets=frozenset())

    # A 0 W anchor poll closes the 360 s outage window; then the export evening.
    await restarted_poll_and_tick(360.0)
    pod.script_grid_power_w(_ENERGY_STEP_W)
    for _ in range(6):
        await restarted_poll_and_tick(_ENERGY_INTERVAL_S)
    pod.script_grid_power_w(0)

    # Roll day 3 at the next local midnight (the export word cleared).
    await restarted_poll_and_tick(86_400.0 - (540.0 + 6 * _ENERGY_INTERVAL_S))
    assert restarted_accountant.live_day is not None
    assert restarted_accountant.live_day.isoformat() == "2026-08-25"

    days = await runtime.facade.get_energy_days(principal=OPERATOR, limit=8)
    by_date = {day["date"]: day for day in days["days"]}
    assert set(by_date) == {"2026-08-22", "2026-08-23", "2026-08-24"}, sorted(by_date)
    day3 = by_date["2026-08-24"]
    day3_unit = day3["units"][UNIT_ID]
    # 0.5 kWh imported and 0.5 kWh exported across the restart; the outage
    # contributed nothing (never interpolated).
    assert day3_unit["grid_import_kwh"] == pytest.approx(0.5)
    assert day3_unit["grid_export_kwh"] == pytest.approx(0.5)
    # The outage left the day below the coverage threshold -> partial.
    assert day3_unit["coverage_pct"] == pytest.approx(100.0 * 396.0 / 756.0, abs=0.01)
    assert day3["kind"] == "partial"
    assert day3_unit["metric_flags"] == []
    assert day3["counter_cross_check"]["discriminating"] is False, (
        "a sub-threshold-coverage day never discriminates"
    )
    # Days are immutable and newest-last.
    assert [day["date"] for day in days["days"]] == [
        "2026-08-22",
        "2026-08-23",
        "2026-08-24",
    ]
    assert days["solar_production_measured"] is False


async def test_energy_scorecard_golden_dst_day_is_honest(tmp_path: Path) -> None:
    """Sydney 2026-10-04: DST starts at 02:00 local -- a 23-hour day.  The
    day rolls at LOCAL midnight however many UTC hours the day held, and
    each record stores its own midnight's utc_offset_minutes."""
    from zoneinfo import ZoneInfo

    sydney = ZoneInfo("Australia/Sydney")
    dst_day_midnight = datetime(2026, 10, 4, 0, 0, tzinfo=sydney).astimezone(UTC)
    next_midnight = datetime(2026, 10, 5, 0, 0, tzinfo=sydney).astimezone(UTC)
    assert (next_midnight - dst_day_midnight) == timedelta(hours=23), (
        "2026-10-04 is Sydney's 23-hour spring-forward day"
    )

    clock = ManualClock()
    clock.wall = dst_day_midnight
    runtime = compose_runtime(
        _energy_fleet_config(tmp_path / "energy-dst.sqlite3", timezone="Australia/Sydney"),
        clock,
    )
    await runtime.actors[UNIT_ID].start()
    pod = runtime.simulators[UNIT_ID]

    pod.script_grid_power_w(-_ENERGY_STEP_W)
    await _energy_poll_and_tick(runtime, clock, 0.0)
    for _ in range(10):
        await _energy_poll_and_tick(runtime, clock, _ENERGY_INTERVAL_S)
    pod.script_grid_power_w(0)

    live = runtime.energy_accountant
    assert live is not None
    today = live.today_summary()
    assert today.utc_offset_minutes == 600, "Oct 4 midnight is still AEST (+10)"
    assert today.units[UNIT_ID].grid_import_kwh == pytest.approx(1.0)

    # Jump across the local midnight that arrives only 23 wall hours later.
    await _energy_poll_and_tick(
        runtime, clock, (next_midnight - dst_day_midnight).total_seconds() - 10 * _ENERGY_INTERVAL_S
    )
    assert live.live_day is not None and live.live_day.isoformat() == "2026-10-05"
    rolled = live.today_summary()
    assert rolled.utc_offset_minutes == 660, "Oct 5 midnight is AEDT (+11)"
    days = await runtime.facade.get_energy_days(principal=OPERATOR, limit=8)
    assert days["days"][0]["date"] == "2026-10-04"
    assert days["days"][0]["utc_offset_minutes"] == 600
