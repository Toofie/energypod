"""Contract tests for run-mode composition against the real transports.

Run mode (``build_runtime`` with ``simulate=False``) composes the production
``WaveshareTransport`` per configured unit — lazily, on the serving loop — plus
a decode-driven telemetry strategy wired per unit.  These tests pin that
composition against the authorized live captures (golden vectors):

- ``docs/evidence/live-capture-2026-08-22.json`` — the 13-block IoT read plan
  captured once per unit (MID/RHS/LHS), and
- ``docs/evidence/live-capture-followup-2026-08-22.json`` — the system
  overview (0x0100) and parameter blocks that complete the catalog's common
  read plan,

decoded per ``docs/evidence/field-mapping-2026-08-22.md`` (high-confidence
section 6).  SAFETY: no test here may ever contact hardware.  The production
transport is faked strictly at the transport port by replaying those capture
files, and every scenario runs under a socket ban, so any network I/O —
including an accidental connection to 192.168.1.x — fails loudly.

Safety posture pinned by this suite (PROTOCOL_EVIDENCE section 4a / field
mapping section 7): control remains observe-only until scaling, direction,
freshness, watchdog timing, and the string-identity binding are validated per
unit.  Run mode therefore composes no interactive qualification path: a
replayed unit never qualifies, the facade refuses arm even for a fully
privileged interactive principal, and the only register write that can ever
reach the (replayed) hardware is the evidenced bounded stop triple
``[1, 0, 0]`` at 0x0200.  Observe-only is structural, not a mode flag.

Identity spelling: the wire RTU ID (0x8106, low-word-first uint32,
``protocol_codec.UInt32Field.RTU_ID``) is rendered as the fleet-stable string
``byd-{rtu_id:08x}`` — the same spelling the wire-decode contract
(``tests/unit/test_wire_decode.py``, field-mapping section 1) pins — so a
composed run-mode unit's ``expected_identity`` and the decoded
``device_identity`` name one identity: MID ``byd-2c225097``, RHS
``byd-2c225076``, LHS ``byd-2c225095``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from energypod.adapters.modbus.waveshare import WaveshareTransport, WaveshareTransportConfig
from energypod.application.actor import EnergyPodActor, InhibitCause
from energypod.domain import DataQuality, Direction, IntentSource, PowerIntent, UnitLifecycle
from energypod.domain.audit import AuditEvent
from energypod.domain.authorization import AuthorizationBatch, AuthorizedSetpoint
from energypod.domain.observations import Observation
from energypod.runtime.config import ControllerConfig
from energypod.simulator import SimulatorTransport

# --- the authorized live fleet, as pinned by the 2026-08-22 captures ----------
#
# docs/evidence/field-mapping-2026-08-22.md sections 1-4: per-unit gateway,
# RTU identity, commissioned BIC topology, and the decoded engineering values
# cross-validated on hardware (V*I=P at three measurement points, V*Ilim=Plim,
# pack V = cells x mean cell V).

LIVE_SITE_ID = "home"
LIVE_TIMEZONE = "Australia/Brisbane"
_STOP_TRIPLE = (0x0200, (1, 0, 0))
LIVE_CALIBRATION_WARNINGS = frozenset({"DCDC_Warning0_1", "PCS_Warning0_1"})


@dataclass(frozen=True)
class LiveUnitExpectation:
    """One unit's pinned commissioning facts and golden decoded values."""

    unit_id: str
    host: str
    port: int
    device_id: int
    identity: str
    bic_count: int
    cell_count: int
    temperature_count: int
    probe_registers: tuple[int, ...]
    system_soc_pct: float
    bms_soc_pct: float
    soh_pct: float
    pack_voltage_v: float
    pack_current_a: float
    battery_watts: float
    charge_limit_w: float
    discharge_limit_w: float
    cell_min_v: float
    cell_max_v: float
    temperature_min_c: float
    temperature_max_c: float


LIVE_FLEET: tuple[LiveUnitExpectation, ...] = (
    LiveUnitExpectation(
        unit_id="mid",
        host="192.168.1.11",
        port=4196,
        device_id=4,
        identity="byd-2c225097",
        bic_count=6,
        cell_count=60,
        temperature_count=18,
        probe_registers=(536, 3, 3, 0, 1, 6, 1923),
        system_soc_pct=10.0,
        bms_soc_pct=10.0,
        soh_pct=100.0,
        pack_voltage_v=192.3,
        pack_current_a=0.2,
        battery_watts=38.0,
        charge_limit_w=7692.0,
        discharge_limit_w=0.0,
        cell_min_v=3.204,
        cell_max_v=3.208,
        temperature_min_c=23.0,
        temperature_max_c=28.0,
    ),
    LiveUnitExpectation(
        unit_id="rhs",
        host="192.168.1.12",
        port=4196,
        device_id=4,
        identity="byd-2c225076",
        bic_count=5,
        cell_count=50,
        temperature_count=15,
        probe_registers=(536, 3, 1, 2, 1, 5, 1633),
        system_soc_pct=71.0,
        bms_soc_pct=73.0,
        soh_pct=100.0,
        pack_voltage_v=163.3,
        pack_current_a=7.3,
        battery_watts=1192.0,
        charge_limit_w=6532.0,
        discharge_limit_w=6532.0,
        cell_min_v=3.265,
        cell_max_v=3.271,
        temperature_min_c=23.0,
        temperature_max_c=28.0,
    ),
    LiveUnitExpectation(
        unit_id="lhs",
        host="192.168.1.13",
        port=4196,
        device_id=4,
        identity="byd-2c225095",
        bic_count=6,
        cell_count=60,
        temperature_count=18,
        probe_registers=(536, 3, 1, 2, 1, 6, 1953),
        system_soc_pct=53.0,
        bms_soc_pct=57.0,
        soh_pct=100.0,
        pack_voltage_v=195.3,
        pack_current_a=10.1,
        battery_watts=1972.0,
        charge_limit_w=7812.0,
        discharge_limit_w=7812.0,
        cell_min_v=3.253,
        cell_max_v=3.260,
        temperature_min_c=22.0,
        temperature_max_c=27.0,
    ),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_CAPTURE_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-2026-08-22.json"
_FOLLOWUP_CAPTURE_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-followup-2026-08-22.json"


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated commissioning operator used only as adapter input.

    Full scopes and interactive: exactly the principal that CAN arm a unit in
    simulate mode, so run-mode arming refusals below are structural refusals,
    not authorization failures.
    """

    subject: str = "person:commissioning-operator"
    scopes: frozenset[str] = frozenset(
        {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
    )
    interactive: bool = True
    site_id: str = LIVE_SITE_ID


OPERATOR = OperatorPrincipal()


def _composition() -> ModuleType:
    try:
        return importlib.import_module("energypod.runtime.composition")
    except ImportError as error:
        pytest.fail(f"the runtime composition root is not implemented: {error}")
        raise  # pragma: no cover - pytest.fail never returns


def _build(config: ControllerConfig, *, clock: Any | None = None) -> Any:
    """Compose RUN mode: the simulate flag is never set by this suite."""
    factory = getattr(_composition(), "build_runtime", None)
    if not callable(factory):
        pytest.fail("energypod.runtime.composition.build_runtime is not implemented", pytrace=False)
    return factory(config, clock=clock)


def _validate(payload: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(payload)


# --- configuration payloads ---------------------------------------------------


def _live_timing_payload() -> dict[str, Any]:
    # The commissioned budget shared with the composition suite; every
    # cross-validated timing invariant holds for these numbers.
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "live-trial://direction-2026-08-22/rev-1",
        "control_period_s": 0.40,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _live_unit_payload(
    expectation: LiveUnitExpectation, *, identity: str | None = None
) -> dict[str, Any]:
    return {
        "unit_id": expectation.unit_id,
        "display_name": expectation.unit_id.upper(),
        "endpoint": {"host": expectation.host, "port": expectation.port},
        "transport_profile": "waveshare_rtu_over_tcp",
        "protocol_profile": "iot",
        "device_id": expectation.device_id,
        "expected_identity": expectation.identity if identity is None else identity,
        "expected_cell_count": expectation.cell_count,
    }


def _live_authentication_payload() -> dict[str, Any]:
    # The credential-file shape: a secret reference naming the operator
    # credential store, never an inline secret.
    return {
        "enabled": True,
        "operator_credential_ref": "secret://energypod/live-operator-credential",
        "trusted_proxy_cidrs": ["192.168.1.0/24"],
    }


def _live_policy_payload() -> dict[str, Any]:
    return {
        "version": 3,
        "threshold_provenance": "commissioning-record-2026-08",
        "max_fleet_charge_w": 6000,
        "max_fleet_discharge_w": 6000,
        "max_unit_charge_w": 2500,
        "max_unit_discharge_w": 2500,
        "minimum_soc_pct": 10.0,
        "maximum_soc_pct": 95.0,
        "minimum_cell_v": 2.80,
        "maximum_cell_v": 3.65,
        "maximum_cell_imbalance_v": 0.050,
        "minimum_temperature_c": 0.0,
        "maximum_temperature_c": 45.0,
        "maximum_soc_difference_pct": 5.0,
        "maximum_soc_jump_pct": 10.0,
        "maximum_telemetry_age_s": 1.0,
        "maximum_cell_data_age_s": 5.0,
        "authorization_lifetime_s": 0.75,
        "ramp_limit_w_per_s": 1000,
        "stable_samples_to_rearm": 5,
        "reactive_power_limit_var": 0,
        "blocking_fault_codes": [
            "PCS_EE_CALIBRATION_OUT_OF_RANGE",
            "DCDC_EE_CALIBRATION_OUT_OF_RANGE",
        ],
        "debug_modes_enabled": False,
    }


def _live_config_payload(
    *,
    mode: str = "observe_only",
    database: Path | None = None,
    identity_overrides: dict[str, str] | None = None,
    with_control_configuration: bool = False,
) -> dict[str, Any]:
    overrides = identity_overrides or {}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 7,
        "mode": mode,
        "site": {
            "site_id": LIVE_SITE_ID,
            "timezone": LIVE_TIMEZONE,
            "expected_unit_count": len(LIVE_FLEET),
        },
        "units": [
            _live_unit_payload(expectation, identity=overrides.get(expectation.unit_id))
            for expectation in LIVE_FLEET
        ],
        "timing": _live_timing_payload(),
        "authentication": _live_authentication_payload(),
    }
    if with_control_configuration:
        payload["policy"] = _live_policy_payload()
    if database is not None:
        payload["storage"] = {"database_path": str(database), "busy_timeout_ms": 250}
    return payload


# --- deterministic time -------------------------------------------------------

_SCRIPT_START = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@dataclass
class ScriptedClock:
    """Injectable deterministic clock: every sleep advances scripted time."""

    elapsed_s: float = 0.0

    def wall_now(self) -> datetime:
        return _SCRIPT_START + timedelta(seconds=self.elapsed_s)

    def monotonic(self) -> float:
        return self.elapsed_s

    async def sleep(self, seconds: float) -> None:
        self.elapsed_s += max(0.0, float(seconds))
        await asyncio.sleep(0)


# --- the replayed transport port ----------------------------------------------


def _load_captures() -> dict[str, dict[str, Any]]:
    captures: dict[str, dict[str, Any]] = {}
    for name, path in (("main", _LIVE_CAPTURE_PATH), ("followup", _FOLLOWUP_CAPTURE_PATH)):
        if not path.is_file():
            pytest.fail(f"the authorized live capture is missing: {path}", pytrace=False)
        captures[name] = json.loads(path.read_text(encoding="utf-8"))
    return captures


def _register_banks_by_host() -> dict[str, dict[int, int]]:
    """One flat register bank per gateway host, merged from both captures."""
    captures = _load_captures()
    banks: dict[str, dict[int, int]] = {}
    for unit_id, capture in captures["main"].items():
        registers: dict[int, int] = {}
        blocks = [*capture["blocks"], *captures["followup"][unit_id]["blocks"]]
        for block in blocks:
            address = int(block["address"])
            values = block["registers"]
            if len(values) != int(block["count"]):
                pytest.fail(
                    f"capture block {address:#06x} on {unit_id} declares {block['count']} "
                    f"registers but carries {len(values)}",
                    pytrace=False,
                )
            for offset, value in enumerate(values):
                registers[address + offset] = int(value)
        banks[str(capture["host"])] = registers
    return banks


@dataclass
class ReplayJournal:
    """Everything the composed runtime did at the replayed transport port."""

    constructions: list[WaveshareTransportConfig] = field(default_factory=list)
    connects: list[str] = field(default_factory=list)
    reads: list[tuple[str, int, int]] = field(default_factory=list)
    writes: list[tuple[str, int, tuple[int, ...]]] = field(default_factory=list)
    closes: list[str] = field(default_factory=list)

    def windows_for(self, host: str) -> set[tuple[int, int]]:
        return {(address, count) for served, address, count in self.reads if served == host}

    def writes_for(self, host: str) -> tuple[tuple[int, tuple[int, ...]], ...]:
        return tuple((address, values) for served, address, values in self.writes if served == host)

    def assert_only_bounded_stops(self) -> None:
        """The only register write run mode may ever issue is the stop triple."""
        for served, address, values in self.writes:
            assert (address, tuple(values)) == _STOP_TRIPLE, (
                f"run mode wrote {address:#06x} <- {tuple(values)} on {served}: the only "
                "evidenced writable objective is the bounded stop triple at 0x0200"
            )


def _install_replay_transport(monkeypatch: pytest.MonkeyPatch) -> ReplayJournal:
    """Serve the golden captures at the production transport port, per gateway.

    The composition's lazy production factory resolves the name
    ``WaveshareTransport`` from the composition module at first use, so
    replacing exactly that name fakes the real transport at its own port: the
    composed wiring (per-unit host, port, device id) is what routes each actor
    to its unit's captured register bank.  Nothing here opens a socket.
    """
    banks_by_host = _register_banks_by_host()
    journal = ReplayJournal()

    class ReplayedLiveTransport:
        """A Waveshare gateway replaying one unit's authorized capture."""

        def __init__(self, *, config: WaveshareTransportConfig) -> None:
            bank = banks_by_host.get(config.host)
            if bank is None:
                raise AssertionError(
                    f"the run-mode transport was wired to gateway {config.host!r}, which no "
                    "authorized live capture covers"
                )
            self._host = config.host
            self._bank = bank
            journal.constructions.append(config)

        async def connect(self) -> None:
            journal.connects.append(self._host)

        async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
            journal.reads.append((self._host, address, count))
            try:
                return tuple(self._bank[address + offset] for offset in range(count))
            except KeyError as error:
                raise AssertionError(
                    f"the run-mode read plan requested register {error.args[0]} (window "
                    f"{address:#06x} x {count}) that the authorized capture does not serve"
                ) from error

        async def write_registers(self, address: int, values: Sequence[int]) -> None:
            # Same write gate as the production transport: only the evidenced
            # three-register PQ objective is writable, so any composed write
            # path shows up in the journal instead of failing silently.
            registers = tuple(values)
            if address != 0x0200 or len(registers) != 3 or registers[0] != 1:
                raise ValueError("only the evidenced three-register PQ objective is writable")
            journal.writes.append((self._host, address, registers))

        async def close(self) -> None:
            journal.closes.append(self._host)

    monkeypatch.setattr(_composition(), "WaveshareTransport", ReplayedLiveTransport)
    return journal


def _forbid_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every scenario in this suite runs with network I/O refused outright."""

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live-composition tests must never perform network I/O")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "socketpair", refused)


def _forbid_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-level ban for the scenarios that drive the event loop hard.

    On Windows the proactor event loop's own self-pipe machinery isinstance-
    checks against ``socket.socket`` and builds wake-ups through the socket
    module, so replacing the class itself would break loop scheduling in the
    lifespan-driven supervision scenario.  There the safety pin is structural
    instead: the replay double installed at the composition module is the only
    transport class the runtime can construct, and it never touches a socket —
    the journal records every construction and connection it served.
    """

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("live-composition tests must never perform network I/O")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "socketpair", refused)


def _expected_window_plan(bic_count: int) -> set[tuple[int, int]]:
    """The evidenced register windows one run-mode telemetry cycle must read.

    ``RegisterCatalog.common_reads`` plus ``iot_reads(bic_count=BIC)`` for the
    unit's commissioned topology (PROTOCOL_EVIDENCE sections 4-7; the cell
    windows follow the per-unit BIC count the layout probe reported).
    """
    common = {(0x0100, 61), (0x8100, 1), (0x8139, 1), (0x8106, 2), (0x8102, 56)}
    iot = {
        (0x1000, 21),
        (0x1040, 22),
        (0x1060, 32),
        (0x2000, 13),
        (0x2040, 22),
        (0x2060, 19),
        (0x4101, 12),
        (0x5000, 31),
        (0x5040, 22),
        (0x5200, bic_count * 10),
        (0x523C, bic_count * 3),
        (0x524E, bic_count),
    }
    return common | iot


async def _shutdown_actors(runtime: Any) -> None:
    for actor in runtime.actors.values():
        with contextlib.suppress(Exception):
            await actor.shutdown()


def _actors_stopped(runtime: Any) -> bool:
    return all(
        actor.lifecycle in {UnitLifecycle.STOPPING, UnitLifecycle.DISCONNECTED}
        for actor in runtime.actors.values()
    )


# --- durable-history seeds for the boot contract ------------------------------


def _prior_active_audit_event(unit_id: str) -> AuditEvent:
    """Durable history claiming the previous process held armed authority."""
    return AuditEvent(
        event_id=f"live-prior-active-authority-{unit_id}",
        occurred_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
        monotonic_offset_s=0.0,
        process_instance_id="prior-live-process",
        event_type="authorization.granted",
        unit_id=unit_id,
        connection_epoch=3,
        generation=2,
        cycle_id="cycle-00000000000000000001",
        principal="person:commissioning-operator",
        source=IntentSource.MANUAL,
        correlation_id="intent:live-stale-command-1:revision:9",
        intent_id="live-stale-command-1",
        policy_version=3,
        configuration_version=7,
        observation_sequences={unit_id: 42},
        reason_codes=("safety_checks_passed",),
        requested_active_w=1500,
        authorized_active_w=1500,
        request_fingerprint="request-fingerprint-live-prior",
        response_fingerprint="response-fingerprint-live-prior",
        result="authorized",
        lifecycle=UnitLifecycle.ACTIVE,
    )


_LIVE_FLEET_UNIT_IDS = tuple(expectation.unit_id for expectation in LIVE_FLEET)


def _expectation(unit_id: str) -> LiveUnitExpectation:
    return next(item for item in LIVE_FLEET if item.unit_id == unit_id)


def _stale_command_intent(unit_ids: tuple[str, ...] | None = None) -> PowerIntent:
    return PowerIntent(
        id="live-stale-command-1",
        source=IntentSource.MANUAL,
        selected_unit_ids=frozenset(_LIVE_FLEET_UNIT_IDS if unit_ids is None else unit_ids),
        direction=Direction.DISCHARGE,
        watts=1500,
        duration_s=3600.0,
        accepted_at_mono=50.0,
        acceptance_revision=9,
        actor_identity="person:commissioning-operator",
    )


def _stale_authorization_batch(
    unit_ids: tuple[str, ...] | None = None,
    *,
    generation: int = 2,
) -> AuthorizationBatch:
    selected = _LIVE_FLEET_UNIT_IDS if unit_ids is None else unit_ids
    setpoints = tuple(
        AuthorizedSetpoint(
            unit_id=unit_id,
            connection_epoch=3,
            generation=generation,
            cycle_id="cycle-00000000000000000001",
            intent_id="live-stale-command-1",
            intent_revision=9,
            direction=Direction.DISCHARGE,
            watts=750,
            issued_at_mono=0.0,
            not_before_mono=0.0,
            expires_at_mono=3_950.0,
            observation_sequence=42,
            maximum_observation_age_s=1.0,
            policy_version="3",
            configuration_version=7,
            decision_id="decision-00000000000000000001",
        )
        for unit_id in selected
    )
    return AuthorizationBatch(
        cycle_id="cycle-00000000000000000001",
        generation=generation,
        authorizations=setpoints,
    )


# --- ASGI lifespan driver (no test server) ------------------------------------


class _LifespanSession:
    """Drives the composed app's ASGI lifespan protocol with no test server."""

    def __init__(self, app: Any) -> None:
        self.events: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.app_task: asyncio.Task[None] = asyncio.create_task(self._drive(app))

    async def _drive(self, app: Any) -> None:
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}}

        async def receive() -> dict[str, Any]:
            return await self._incoming.get()

        async def record(message: dict[str, Any]) -> None:
            self.events.append(message)

        await app(scope, receive, record)

    def send(self, message_type: str) -> None:
        self._incoming.put_nowait({"type": message_type})

    def seen(self, message_type: str) -> bool:
        return any(message["type"] == message_type for message in self.events)

    async def pump_until(
        self, predicate: Callable[[], bool], *, message: str, attempts: int = 5000
    ) -> None:
        for _ in range(attempts):
            if predicate():
                return
            if self.app_task.done():
                break
            await asyncio.sleep(0)
        if not predicate():
            pytest.fail(f"{message}: lifespan events={self.events!r}", pytrace=False)

    async def close(self) -> None:
        if not self.app_task.done():
            self.app_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.app_task


# --- tests --------------------------------------------------------------------


def test_authorized_live_captures_are_present_and_complete() -> None:
    """The golden vectors exist and cover the full run-mode read plan."""
    captures = _load_captures()
    banks = _register_banks_by_host()
    assert set(captures["main"]) == {"MID", "RHS", "LHS"}
    assert set(captures["followup"]) == {"MID", "RHS", "LHS"}
    assert set(banks) == {expectation.host for expectation in LIVE_FLEET}
    for expectation in LIVE_FLEET:
        bank = banks[expectation.host]
        for address, count in _expected_window_plan(expectation.bic_count):
            missing = [address + offset for offset in range(count) if address + offset not in bank]
            assert not missing, (
                f"window {address:#06x} x {count} for {expectation.unit_id} is not fully "
                f"covered by the authorized captures (missing {len(missing)} registers)"
            )


async def test_live_run_config_composes_lazy_production_transports_and_decode_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live run-mode config composes production transports, not simulators.

    The configuration shape is the commissioned one: per-unit gateway endpoint,
    device id, expected identity and cell count; observe-only mode; a
    credential-store authentication reference.  Composing it opens no socket,
    starts no actor I/O, leaves every actor in BOOT, and wires each actor a
    decode-driven telemetry strategy — the simulator strategy is simulator
    mode's alone.
    """
    _forbid_sockets(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())

    assert runtime.config.mode.value == "observe_only"
    authentication = runtime.config.authentication
    assert authentication is not None and authentication.enabled
    assert authentication.operator_credential_ref.startswith("secret://")
    assert runtime.simulators is None, "run mode must compose no simulator pods"

    module = _composition()
    assert module.WaveshareTransport is WaveshareTransport, (
        "the composed lazy transport must construct the production WaveshareTransport"
    )
    simulator_telemetry = getattr(module, "_SimulatorTelemetry", None)
    assert simulator_telemetry is not None

    units = {unit.unit_id: unit for unit in runtime.config.units}
    strategies: list[Any] = []
    for expectation in LIVE_FLEET:
        unit = units[expectation.unit_id]
        assert unit.endpoint.host == expectation.host
        assert unit.endpoint.port == expectation.port
        assert unit.device_id == expectation.device_id
        assert unit.expected_identity == expectation.identity
        assert unit.expected_cell_count == expectation.cell_count
        assert unit.protocol_profile.value == "iot"
        assert unit.transport_profile.value == "waveshare_rtu_over_tcp"

        actor = runtime.actors[expectation.unit_id]
        assert isinstance(actor, EnergyPodActor)
        assert actor.lifecycle is UnitLifecycle.BOOT
        assert not isinstance(actor._transport, SimulatorTransport), (
            "run mode must not compose simulator transports"
        )
        strategy = actor._telemetry
        assert strategy is not None, (
            "run mode must wire a decode-driven telemetry strategy into every actor"
        )
        assert not isinstance(strategy, simulator_telemetry), (
            "run mode must decode the real register bank, not the simulator device model"
        )
        strategies.append(strategy)

    assert len({id(strategy) for strategy in strategies}) == len(LIVE_FLEET), (
        "each unit's decode strategy is wired per unit, never shared"
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_replayed_live_capture_serves_the_full_run_mode_read_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One replayed poll reads the evidenced per-unit plan through one transport.

    Composition defers constructing the production transport (the journal is
    empty right after ``build_runtime``); the first use on the serving loop
    builds exactly one transport per unit, wired to that unit's gateway host,
    port and device id, and the telemetry cycle reads every evidenced window —
    including the per-unit cell windows that follow the commissioned BIC count.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())
    assert journal.constructions == [], "transport construction must be deferred to first use"

    try:
        for expectation in LIVE_FLEET:
            actor = runtime.actors[expectation.unit_id]
            await actor.start()
            essential = await actor.poll_once()
            assert essential == expectation.probe_registers, (
                "the poll must return the layout-probe registers the unit actually served"
            )
            assert actor.lifecycle is UnitLifecycle.OBSERVE_ONLY

        assert len(journal.constructions) == len(LIVE_FLEET)
        assert sorted(journal.connects) == sorted(e.host for e in LIVE_FLEET)
        for expectation in LIVE_FLEET:
            wired = [config for config in journal.constructions if config.host == expectation.host]
            assert len(wired) == 1
            assert wired[0].port == expectation.port
            assert wired[0].device_id == expectation.device_id
            windows = journal.windows_for(expectation.host)
            # Commissioned tiered refresh (2026-08-22; B5 2026-08-24): one
            # cycle reads the control-rate core (BMS, the three fault blocks,
            # temperatures, the identity pair, the debug readback) plus the
            # cycle-1 cell window; the system overview now rides the cold
            # ring (no longer a once-per-process read), and the remaining
            # evidenced windows rotate on slower tiers, so the full plan is
            # covered across cycles, not within one.
            core = {
                (0x5000, 31),
                (0x1040, 22),
                (0x2040, 22),
                (0x5040, 22),
                (0x523C, expectation.bic_count * 3),
                (0x8106, 2),
                (0x5200, min(expectation.bic_count * 10, 100)),
            }
            assert windows >= core, (
                f"{expectation.unit_id} read {sorted(windows)}; one cycle must read "
                "the control-rate core plus the cycle-1 cell window"
            )
            assert (0x5000, 7) in windows, "the essential layout probe stays part of the cycle"
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


async def test_replayed_live_capture_decodes_real_telemetry_and_an_honest_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """actor.poll_once() -> decode -> observation -> snapshot carries REAL values.

    The observation is the golden vector decoded per the field mapping: SOC,
    pack voltage, battery power, dynamic limits, cells, temperatures, the two
    live calibration warnings, and the per-unit wire identity.  The facade
    snapshot then shows those values with observe-only lifecycle, good quality
    and no authority anywhere.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())

    try:
        for expectation in LIVE_FLEET:
            actor = runtime.actors[expectation.unit_id]
            await actor.start()
            await actor.poll_once()
            await runtime.clock.sleep(0.05)
            # B5: the system overview rides the cold ring and is served on
            # cycle 8; until then the BMS SOC stands in for the advisory
            # system figure exactly as the decoder's stand-in doctrine pins.
            first = await runtime.observations.latest(expectation.unit_id)
            assert first is not None
            assert first.system_soc_pct == pytest.approx(expectation.bms_soc_pct), (
                "before the cold ring serves the system block, the BMS figure stands in"
            )
            for _ in range(7):
                await actor.poll_once()
            await actor.poll_once()

            history = await runtime.observations.history(expectation.unit_id)
            assert len(history) >= 2, "every run-mode poll must append one observation"
            observation = await runtime.observations.latest(expectation.unit_id)
            assert isinstance(observation, Observation)

            assert observation.unit_id == expectation.unit_id
            assert observation.device_identity == expectation.identity, (
                "identity is decoded from the wire (0x8106, low-word-first), never echoed "
                "from configuration"
            )
            assert observation.protocol_profile == "iot"
            assert observation.lifecycle is UnitLifecycle.OBSERVE_ONLY
            assert observation.sequence > history[-2].sequence, (
                "replayed polls must still mint strictly increasing capture sequences"
            )
            assert observation.wall_timestamp.tzinfo is not None

            assert observation.system_soc_pct == pytest.approx(expectation.system_soc_pct)
            assert observation.bms_soc_pct == pytest.approx(expectation.bms_soc_pct)
            assert observation.soh_pct == pytest.approx(expectation.soh_pct)
            assert observation.battery_watts == pytest.approx(expectation.battery_watts)
            assert observation.pack_voltage_v == pytest.approx(expectation.pack_voltage_v)
            assert observation.pack_current_a == pytest.approx(expectation.pack_current_a)
            assert observation.dynamic_charge_limit_w == pytest.approx(expectation.charge_limit_w)
            assert observation.dynamic_discharge_limit_w == pytest.approx(
                expectation.discharge_limit_w
            )

            assert observation.expected_cell_count == expectation.cell_count
            assert len(observation.cell_voltages_v) == expectation.cell_count
            assert observation.cell_min_voltage_v == pytest.approx(expectation.cell_min_v)
            assert observation.cell_max_voltage_v == pytest.approx(expectation.cell_max_v)
            assert len(observation.temperatures_c) == expectation.temperature_count
            assert observation.temperature_min_c == pytest.approx(expectation.temperature_min_c)
            assert observation.temperature_max_c == pytest.approx(expectation.temperature_max_c)

            assert observation.active_faults == frozenset()
            assert observation.active_warnings == LIVE_CALIBRATION_WARNINGS, (
                "the two calibration warnings every live unit reports must decode"
            )
            # The energy_scorecard block is ABSENT in this composition: the
            # decode emits the TWELVE-key shape (ten safety fields plus the
            # two advisory CT words) and no cumulative-energy keys.
            assert set(observation.quality) == set(Observation.QUALITY_FIELDS) | set(
                Observation.CT_QUALITY_FIELDS
            ), "an absent scorecard block composes no energy decode"
            assert all(
                observation.quality[field] is DataQuality.GOOD
                for field in Observation.QUALITY_FIELDS
            ), (
                "a fully served capture decodes good quality on every safety-critical field; "
                "advisory CT fields may honestly read MISSING before the cold ring serves the "
                "PCS block"
            )
            assert observation.cells_complete
            assert observation.temperatures_complete
            assert observation.safety_data_complete

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        views = {view["unit_id"]: view for view in snapshot["units"]}
        assert set(views) == {expectation.unit_id for expectation in LIVE_FLEET}
        for expectation in LIVE_FLEET:
            view = views[expectation.unit_id]
            assert view["lifecycle"] == "observe_only"
            assert view["quality"] == "good"
            assert view["measured_watts"] == pytest.approx(expectation.battery_watts)
            assert view["authorized_power"] is None
            assert view["requested_power"] == {"direction": "idle", "watts": 0}
            assert isinstance(view["telemetry_age_s"], float) and view["telemetry_age_s"] <= 30.0
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


async def test_replayed_run_mode_unit_never_qualifies_and_arm_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run mode composes no interactive qualification path: arm is refused.

    Actuation is gated on open evidence items (watchdog timing, verified sign
    conventions, string-identity binding — field mapping section 7), so a
    run-mode unit must never reach a qualified, armable state no matter how
    many good telemetry cycles it serves.  Even the fully privileged
    interactive principal that CAN arm in simulate mode is refused here:
    observe-only is structural, not a flag the composition checks.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())
    required = runtime.policy.stable_samples_needed_to_rearm

    try:
        actor = runtime.actors["rhs"]
        await actor.start()
        for _ in range(required * 3 + 2):
            await runtime.clock.sleep(0.05)
            await actor.poll_once()

        assert actor.qualified is not True, (
            f"{required * 3 + 2} stable, good, identity-matching replayed cycles must never "
            "qualify a run-mode unit for control"
        )
        assert actor.lifecycle is UnitLifecycle.OBSERVE_ONLY, (
            "a run-mode unit never leaves observe-only through telemetry alone"
        )

        result = await runtime.facade.arm(
            unit_ids=["rhs", "lhs"],
            principal=OPERATOR,
            idempotency_key="live-arm-refusal",
            request_id="live-arm-request",
        )
        outcomes = {unit["unit_id"]: unit for unit in result["units"]}
        assert set(outcomes) == {"rhs", "lhs"}
        for outcome in outcomes.values():
            assert outcome["status"] == "refused", outcome
            assert outcome["reason"], "refusals must carry a machine-readable reason"

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        views = {view["unit_id"]: view for view in snapshot["units"]}
        assert views["rhs"]["lifecycle"] == "observe_only"
        assert journal.writes_for(_expectation("rhs").host) == (), (
            "an unqualified, unarmed run-mode unit must never be written to"
        )
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


async def test_wrong_expected_identity_latches_a_permanent_non_qualifying_inhibit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong expected identity inhibits permanently; it never error-loops.

    The decode stays honest to the wire, so the observation carries the
    served identity while the actor latches INHIBITED (LATCHED cause): polls
    keep succeeding and appending observations — a permanent non-qualifying
    state, not an exception loop.  Acknowledging the latch clears only the
    latch; the still-mismatched next poll re-latches immediately.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    payload = _live_config_payload(identity_overrides={"mid": "byd-00000000"})
    runtime = _build(_validate(payload), clock=ScriptedClock())

    try:
        actor = runtime.actors["mid"]
        await actor.start()
        for _ in range(3):
            await actor.poll_once()

        history = await runtime.observations.history("mid")
        assert len(history) == 3, "every mismatching poll still delivers its observation"
        observation = await runtime.observations.latest("mid")
        assert observation is not None
        assert observation.device_identity == "byd-2c225097", (
            "the decode reports the identity the hardware served, not the configured one"
        )
        assert actor.lifecycle is UnitLifecycle.INHIBITED
        assert actor.inhibit_latched is True
        assert actor.inhibit_cause is InhibitCause.LATCHED
        assert actor.qualified is False

        await actor.acknowledge_inhibit()
        assert actor.inhibit_latched is False, "acknowledgement clears only the latch"
        await runtime.clock.sleep(0.05)
        await actor.poll_once()
        assert actor.inhibit_latched is True, "a still-mismatched identity re-latches"
        assert actor.lifecycle is UnitLifecycle.INHIBITED
        later = await runtime.observations.latest("mid")
        assert later is not None
        assert later.sequence > observation.sequence
        assert later.lifecycle is UnitLifecycle.INHIBITED, (
            "the observation reports the inhibited lifecycle the actor holds"
        )

        result = await runtime.facade.arm(
            unit_ids=["mid"],
            principal=OPERATOR,
            idempotency_key="live-identity-arm-refusal",
            request_id="live-identity-arm-request",
        )
        assert result["units"][0]["status"] == "refused"
        assert result["units"][0]["reason"] == "inhibit_latched"

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        views = {view["unit_id"]: view for view in snapshot["units"]}
        assert views["mid"]["lifecycle"] == "inhibited"
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


def test_write_enabled_live_config_keeps_the_existing_validation_gate() -> None:
    """write_enabled keeps the configuration contract's own gate.

    Existing semantics: write-enabled mode is refused without a control
    policy and without enabled authentication, and accepted once both are
    present.  The run-mode composition never needs to re-decide this — what
    it must guarantee (pinned by the replay tests) is that even a fully
    commissioned write-enabled run-mode composition executes no control write
    path against unqualified hardware.
    """
    incomplete = _live_config_payload(mode="write_enabled")
    with pytest.raises(ValidationError, match="write-enabled mode requires policy"):
        _validate(incomplete)

    missing_authentication = _live_config_payload(mode="write_enabled")
    missing_authentication["policy"] = _live_policy_payload()
    missing_authentication["authentication"] = {
        **_live_authentication_payload(),
        "enabled": False,
    }
    with pytest.raises(ValidationError, match="write-enabled mode requires enabled authentication"):
        _validate(missing_authentication)

    complete = _live_config_payload(mode="write_enabled", with_control_configuration=True)
    config = _validate(complete)
    assert config.mode.value == "write_enabled"
    assert config.policy is not None
    assert config.authentication is not None and config.authentication.enabled


async def test_no_control_write_path_executes_against_replayed_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write-enabled run-mode composition still writes nothing but stops.

    With a commissioned policy, an accepted dispatch intent and live-quality
    telemetry on every unit, the kernel must not mint authority for the
    observe-only fleet and the facade must refuse to arm — so no PQ objective
    ever reaches the replayed hardware, and actor shutdown owes only the
    bounded stop triple.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    payload = _live_config_payload(mode="write_enabled", with_control_configuration=True)
    runtime = _build(_validate(payload), clock=ScriptedClock())

    try:
        for expectation in LIVE_FLEET:
            actor = runtime.actors[expectation.unit_id]
            await actor.start()
            await actor.poll_once()
            await runtime.clock.sleep(0.05)

        await runtime.intents.add(_stale_command_intent(("rhs", "lhs")))
        # The intent is accepted at monotonic 50.0 with a one-hour duration.
        assert await runtime.intents.active(100.0)

        for _ in range(3):
            await runtime.kernel.tick()
            await runtime.clock.sleep(0.05)

        for expectation in LIVE_FLEET:
            assert await runtime.authorizations.peek(expectation.unit_id) is None, (
                "the kernel must not authorize power for an unqualified observe-only fleet"
            )

        result = await runtime.facade.arm(
            unit_ids=["rhs"],
            principal=OPERATOR,
            idempotency_key="live-write-mode-arm-refusal",
            request_id="live-write-mode-arm-request",
        )
        assert result["units"][0]["status"] == "refused"

        for expectation in LIVE_FLEET:
            assert journal.writes_for(expectation.host) == (), (
                f"no control write may reach {expectation.unit_id} in run mode"
            )
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


async def test_run_mode_supervision_failure_fences_and_shuts_down_the_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor failure in run mode fences everything before it exits.

    The failure fence is mode-independent: the fleet generation advances,
    outstanding authority is revoked, every actor runs its shutdown (bounded
    zero before transport close — the only writes the replayed hardware ever
    sees), and no supervision task survives.
    """
    _forbid_network_connections(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())
    fleet = tuple(expectation.unit_id for expectation in LIVE_FLEET)
    replayed_hosts = {expectation.host for expectation in LIVE_FLEET}
    epoch = (await runtime.generation_coordinator.snapshot()).epoch
    await runtime.authorizations.publish(_stale_authorization_batch(fleet, generation=epoch))
    for unit_id in fleet:
        assert await runtime.authorizations.peek(unit_id) is not None

    async def exploding_tick() -> None:
        raise RuntimeError("run-mode supervisor component failed")

    monkeypatch.setattr(runtime.kernel, "tick", exploding_tick)

    baseline = set(asyncio.all_tasks())
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        # The fleet cycle stages the first kernel tick after startup, so a
        # component failure surfaces as a halt (fence, revoke, actor shutdown)
        # rather than a startup refusal. In production the serving bridge
        # stops the server on that halt; in this raw lifespan session the
        # halt is observed directly, then shutdown is driven explicitly.
        await session.pump_until(
            lambda: _actors_stopped(runtime),
            message="a failed run-mode supervisor component must run actor shutdown",
        )
        assert (await runtime.generation_coordinator.snapshot()).epoch > epoch
        for unit_id in fleet:
            assert await runtime.authorizations.peek(unit_id) is None, (
                "the failure fence must revoke every outstanding authorization"
            )
        await session.pump_until(
            lambda: not asyncio.all_tasks() - baseline - {session.app_task},
            message="a failed run-mode supervisor component must not leave tasks running",
        )
        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the halted fleet must still shut the lifespan down cleanly",
        )
        assert not session.seen("lifespan.shutdown.failed")
    finally:
        await session.close()

    assert {config.host for config in journal.constructions} <= replayed_hosts, (
        "only the replayed capture transports may ever be constructed in run mode"
    )
    journal.assert_only_bounded_stops()


async def test_run_mode_boot_restores_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run-mode restart boots observe-only regardless of durable history."""
    _forbid_sockets(monkeypatch)
    database = tmp_path / "live-fleet.sqlite3"
    prior = _build(_validate(_live_config_payload(database=database)), clock=ScriptedClock())
    fleet = tuple(expectation.unit_id for expectation in LIVE_FLEET)
    for unit_id in fleet:
        prior.audit.append(_prior_active_audit_event(unit_id))
    await prior.intents.add(_stale_command_intent())
    await prior.authorizations.publish(_stale_authorization_batch())
    for unit_id in fleet:
        # The observation port is structural; the seed need only carry the
        # attributes the repository contract reads.
        await prior.observations.append(
            SimpleNamespace(unit_id=unit_id, connection_epoch=1, sequence=1)
        )

    # The seeds are genuinely live in the previous process.
    assert await prior.intents.active(60.0)
    for unit_id in fleet:
        assert await prior.authorizations.peek(unit_id) is not None
        assert await prior.observations.latest(unit_id) is not None

    restarted = _build(_validate(_live_config_payload(database=database)), clock=ScriptedClock())

    recent = restarted.audit.recent(limit=10)
    assert {f"live-prior-active-authority-{unit_id}" for unit_id in fleet} <= {
        event.event_id for event in recent
    }, "the durable audit trail really was reused"
    for now in (0.0, 60.0, 1_000_000_000.0):
        assert not await restarted.intents.active(now)
        for unit_id in fleet:
            assert await restarted.authorizations.peek(unit_id) is None
            assert await restarted.observations.latest(unit_id) is None
    for unit_id in fleet:
        actor = restarted.actors[unit_id]
        assert actor is not prior.actors[unit_id]
        assert actor.lifecycle is UnitLifecycle.BOOT
        assert actor.qualified is None


async def test_snapshot_and_health_stay_honest_for_an_unqualified_replayed_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot and health never fabricate optimism about an unqualified fleet.

    A unit with telemetry shows its real measured power and good quality; a
    unit without telemetry stays honestly missing; control readiness names
    every unit's qualification state plus the absence of any armed unit
    instead of assuming controllability.
    """
    _forbid_sockets(monkeypatch)
    journal = _install_replay_transport(monkeypatch)
    runtime = _build(_validate(_live_config_payload()), clock=ScriptedClock())

    try:
        for expectation in LIVE_FLEET:
            await runtime.actors[expectation.unit_id].start()
        polled = {"mid", "rhs"}
        for expectation in LIVE_FLEET:
            if expectation.unit_id in polled:
                await runtime.actors[expectation.unit_id].poll_once()
        await runtime.clock.sleep(0.05)

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        views = {view["unit_id"]: view for view in snapshot["units"]}
        for expectation in LIVE_FLEET:
            view = views[expectation.unit_id]
            assert view["lifecycle"] == "observe_only"
            if expectation.unit_id in polled:
                assert view["quality"] == "good"
                assert view["measured_watts"] == pytest.approx(expectation.battery_watts)
                assert isinstance(view["telemetry_age_s"], float)
            else:
                assert view["quality"] == "missing", (
                    f"{expectation.unit_id} has no telemetry; that stays visible, not assumed"
                )
                assert view["measured_watts"] is None
                assert view["telemetry_age_s"] is None

        health = await runtime.facade.health(principal=OPERATOR)
        # Liveness carries this process's identity so a console can tell a
        # DEPLOYMENT (new instance id) from a stall (same id, ages climbing).
        assert health["liveness"]["ok"] is True
        assert isinstance(health["liveness"]["process_instance_id"], str)
        assert health["liveness"]["process_instance_id"]
        assert isinstance(health["liveness"]["uptime_s"], float)
        assert health["liveness"]["uptime_s"] >= 0.0
        assert health["service_readiness"] == {"ready": True, "reasons": []}
        control = health["control_readiness"]
        assert control["ready"] is False
        assert "no_unit_armed" in control["reasons"]
        for expectation in LIVE_FLEET:
            assert any(
                reason.startswith(f"{expectation.unit_id}:") for reason in control["reasons"]
            ), (
                f"{expectation.unit_id}'s own qualification state must be a blocking reason, "
                f"not an assumption (reasons={control['reasons']!r})"
            )
    finally:
        await _shutdown_actors(runtime)

    journal.assert_only_bounded_stops()


# --- excess-solar tier promotion (API_CONTRACTS "Excess-solar accelerated
# --- charging (advisory)") -----------------------------------------------------
#
# The PCS live block 0x1000 (grid at +17, load at +20, PROTOCOL_EVIDENCE 4c)
# rides the cold ring today: one window every 8th cycle, ~108 s per refresh at
# the 1.5 s commissioned cadence — useless as a control-rate export signal.
# With the feature enabled it must join the control-rate core EVERY cycle while
# the per-cycle plan stays inside the cadence budget (windows x 0.1 s gateway
# inter-frame gap << control period).


class _StaticCaptureTransport:
    """Serves one captured register bank at the transport port; no socket."""

    def __init__(self, bank: dict[int, int]) -> None:
        self._bank = bank

    async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
        return tuple(self._bank[address + offset] for offset in range(count))

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        raise AssertionError("the telemetry strategy never writes")

    async def close(self) -> None:
        return None


def _live_decode_strategy(bank: dict[int, int], *, promote_pcs_live_block: bool) -> Any:
    """Compose the live decode telemetry strategy directly over one bank."""
    telemetry_class = getattr(_composition(), "_LiveDecodeTelemetry", None)
    if telemetry_class is None:  # pragma: no cover - pinned by the suite import
        pytest.fail("energypod.runtime.composition._LiveDecodeTelemetry is not implemented")
    try:
        return telemetry_class(
            transport=_StaticCaptureTransport(bank),
            clock=ScriptedClock(),
            unit_id="mid",
            expected_identity="byd-2c225097",
            expected_profile="iot",
            expected_cell_count=60,
            probe_address=0x5000,
            probe_count=7,
            promote_pcs_live_block=promote_pcs_live_block,
        )
    except TypeError as error:
        pytest.fail(
            "the live decode strategy does not yet accept promote_pcs_live_block (the "
            f"excess-solar tier-promotion contract): {error}",
            pytrace=False,
        )
        raise  # pragma: no cover - pytest.fail never returns


async def test_pcs_live_block_is_promoted_to_the_control_rate_core_for_excess_charging() -> None:
    """With the feature enabled, grid evidence refreshes EVERY telemetry cycle.

    Default (disabled): 0x1000 stays on the cold ring — present on its rotating
    cycle only.  Enabled: it rides the core on every cycle, and the plan stays
    within the cadence budget (<= 10 windows even on the bootstrap cycle).
    """
    bank = _register_banks_by_host()["192.168.1.11"]
    window = (0x1000, 21)

    default_plans: list[tuple[tuple[int, int], ...]] = []
    strategy = _live_decode_strategy(bank, promote_pcs_live_block=False)
    # B5 added the system overview to the cold ring (9 rotating windows), so
    # the PCS live block serves at cycle 16 of a 17-cycle sample.
    for _ in range(17):
        await strategy.advance()
        default_plans.append(strategy.read_plan())
    coldring_cycles = [plan for plan in default_plans if window in plan]
    assert 0 < len(coldring_cycles) < len(default_plans), (
        "today's cold ring serves the PCS block on a rotating minority of cycles"
    )

    promoted_plans: list[tuple[tuple[int, int], ...]] = []
    strategy = _live_decode_strategy(bank, promote_pcs_live_block=True)
    for _ in range(9):
        await strategy.advance()
        promoted_plans.append(strategy.read_plan())

    for cycle, plan in enumerate(promoted_plans, start=1):
        assert window in plan, (
            f"cycle {cycle} must read the PCS live block at the control rate: {sorted(plan)}"
        )
        assert len(plan) <= 10, (
            f"cycle {cycle} reads {len(plan)} windows; the promoted plan must stay inside "
            "the commissioned cadence budget"
        )


async def test_a_cell_deny_promotes_the_cell_window_into_the_next_plan() -> None:
    """SYNC_RESILIENCE_AUDIT B4: deny-triggered cell tier promotion.

    The cell window normally rides the every-3rd-cycle tier, so a cell-bound
    deny judges a window up to ~3 s old while the live battery may already
    have recovered.  A deny therefore schedules the 0x5200 window into the
    VERY NEXT poll regardless of the ``cycle % 3`` phase -- one promoted
    cycle (the hint is consumed by the plan), the steady plan stays inside
    the commissioned budget (<= 9 windows plus the probe), and the deny is
    then re-evaluated on FRESH battery data: a recovered window authorizes,
    a persisting violation keeps denying.
    """
    bank = _register_banks_by_host()["192.168.1.11"]
    cell_window = (0x5200, 60)

    strategy = _live_decode_strategy(bank, promote_pcs_live_block=False)
    await strategy.advance()  # cycle 1: cells by phase
    assert cell_window in strategy.read_plan()
    await strategy.advance()  # cycle 2: no cells by phase
    assert cell_window not in strategy.read_plan()

    strategy.request_cell_refresh()
    await strategy.advance()  # cycle 3: no cells by phase -- promoted
    promoted = strategy.read_plan()
    assert cell_window in promoted, "a cell deny must promote the window past the phase"
    assert len(promoted) <= 9, (
        "the promoted plan must stay inside the commissioned cadence budget "
        "(8 -> 9 windows, ~1.0 s at the 0.1 s inter-frame gap, inside the 1.5 s "
        "control period / 1.60 s renewal budget)"
    )

    await strategy.advance()  # cycle 4: cells by phase (4 % 3 == 1)
    assert cell_window in strategy.read_plan()
    await strategy.advance()  # cycle 5: the hint was consumed -- phase rules again
    assert cell_window not in strategy.read_plan(), "the promotion lasts exactly one cycle"
    await strategy.advance()  # cycle 6: still phase-ruled
    assert cell_window not in strategy.read_plan()
    await strategy.advance()  # cycle 7: cells by phase
    strategy.request_cell_refresh()  # a duplicate hint must not double the window
    plan = strategy.read_plan()
    assert plan.count(cell_window) == 1


def test_the_fleet_loop_promotes_cells_for_every_cell_derived_deny() -> None:
    """The supervisor promotes the cell window from the tick's decision
    reasons: every cell-derived deny code schedules the fresh re-read."""
    module = _composition()
    reasons = getattr(module, "CELL_DENY_REASONS", None)
    promote = getattr(module, "decision_requests_cell_refresh", None)
    if reasons is None or promote is None:  # pragma: no cover - red-phase pin
        pytest.fail("the composition does not yet expose the cell-deny promotion contract")
    assert reasons == frozenset(
        {"cell_voltage_low", "cell_voltage_high", "cell_imbalance", "cell_count_invalid"}
    )
    assert promote(SimpleNamespace(reason_codes=("safety_checks_passed",))) is False
    assert promote(SimpleNamespace(reason_codes=("cell_voltage_low",))) is True
    assert promote(SimpleNamespace(reason_codes=("power_clamped", "cell_imbalance"))) is True
    assert promote(None) is False


def test_configured_blocking_warning_codes_reach_the_composed_policy() -> None:
    """SYNC_RESILIENCE_AUDIT S1: the wiring defect, fixed.

    The composition hard-wired ``blocking_warning_codes`` to the empty set,
    so the policy's ``blocking_warning`` deny could never fire no matter what
    the configuration said -- the operator's intended EE-calibration block
    was silently inert.  The configured set now reaches the composed policy
    verbatim (the kernel's existing intersection check does the rest)."""
    payload = _live_config_payload(with_control_configuration=True)
    payload["policy"]["blocking_warning_codes"] = ["PCS_Warning0_1", "DCDC_Warning0_1"]
    runtime = _build(_validate(payload))
    assert runtime.policy.blocking_warning_codes == frozenset(
        {"PCS_Warning0_1", "DCDC_Warning0_1"}
    ), "the configured warning codes must reach the composed policy"

    default = _build(_validate(_live_config_payload(with_control_configuration=True)))
    assert default.policy.blocking_warning_codes == frozenset()


async def test_the_system_overview_block_rides_the_cold_ring_not_a_once_per_process_read() -> None:
    """SYNC_RESILIENCE_AUDIT B5 + the SOC-incident read-plan follow-up.

    The system overview (0x0100: ctrlMode +1, workMode +2, the advisory
    system SOC +17) was read on CYCLE 1 ONLY and cached for the process
    lifetime -- the exact tier shape that froze mid's system SOC at 67 % for
    112 sequences and that would pin a boot-time Local mode word forever.
    It now rides the cold ring: served on a rotating minority of cycles after
    cycle 1 (so the mode words and the advisory SOC semi-refresh ~every ring
    period instead of never), the steady per-cycle window count stays inside
    the commissioned budget, and the BMS SOC stands in as authoritative
    everywhere while the block is unserved.
    """
    bank = _register_banks_by_host()["192.168.1.11"]
    system_window = (0x0100, 61)

    plans: list[tuple[tuple[int, int], ...]] = []
    strategy = _live_decode_strategy(bank, promote_pcs_live_block=False)
    for _ in range(17):
        await strategy.advance()
        plans.append(strategy.read_plan())

    assert system_window not in plans[0], "cycle 1 must not pin the system block anymore"
    serving_cycles = [index + 1 for index, plan in enumerate(plans) if system_window in plan]
    assert serving_cycles and all(cycle > 1 for cycle in serving_cycles), (
        "the system overview must rotate in on the cold ring after cycle 1"
    )
    # The rotation is one cold window per 8th cycle, and 0x0100 sorts first:
    # cycle 8 serves it, then once every full ring period.
    assert 8 in serving_cycles
    assert all(len(plan) <= 9 for plan in plans), (
        "the steady plan must stay inside the commissioned cadence budget "
        "(<= 9 windows: 7 core + one tier window + one cold window)"
    )


async def test_the_live_tiered_decode_serves_the_objective_words_with_an_honest_clock() -> None:
    """API_CONTRACTS "Night-writer detector" on the LIVE decode path: the
    detail block rides the cold ring, and between rotations the merged decode
    carries the CACHED words with their ORIGINAL capture clock -- so the
    detector can tell a fresh serving from a stale ride-along."""
    bank = dict(_register_banks_by_host()["192.168.1.11"])
    detail_window = (0x1060, 32)
    # The night writer's words: -2400 W active (word 0xF650), +300 var.
    bank[0x1060 + 17] = -2400 & 0xFFFF
    bank[0x1060 + 18] = 300 & 0xFFFF

    strategy = _live_decode_strategy(bank, promote_pcs_live_block=False)
    await strategy.advance()  # cycle 1: bootstrap plan
    served_cycle: int | None = None
    observation = None
    for cycle in range(2, 90):
        await strategy.advance()
        plan = strategy.read_plan()
        blocks = {window: tuple(bank[window[0] + i] for i in range(window[1])) for window in plan}
        decoded = strategy.decode(blocks, UnitLifecycle.DISARMED)
        if detail_window in plan:
            served_cycle = cycle
            observation = decoded
            break

    assert served_cycle is not None, "the cold ring must eventually serve the detail block"
    assert observation is not None
    assert observation.served_active_objective_w == -2400
    assert observation.served_reactive_objective_var == 300
    captured = observation.objective_captured_at_mono

    # Between rotations the words ride from cache: same values, SAME capture
    # clock -- never a fabricated fresh serving.
    for _ in range(3):
        await strategy.advance()
        plan = strategy.read_plan()
        assert detail_window not in plan
        blocks = {window: tuple(bank[window[0] + i] for i in range(window[1])) for window in plan}
        cached = strategy.decode(blocks, UnitLifecycle.DISARMED)
        assert cached.served_active_objective_w == -2400
        assert cached.objective_captured_at_mono == captured
