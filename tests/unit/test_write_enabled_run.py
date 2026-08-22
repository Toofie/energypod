"""Contract tests for the write-enabled run mode (live control) over replayed captures.

These tests pin ``docs/API_CONTRACTS.md`` "Write-enabled run mode" — the
composition gate (bullet 1), the policy-owned qualification threshold and the
corroborated cadence envelope (bullet 2), and the stop/fence/shutdown
dominance over renewal (bullet 5) — together with the "Unit actor"
external-writer preflight and the "Inhibit acknowledgement" latch semantics it
reuses.  Evidence authority: PROTOCOL_EVIDENCE 4a/4b — the 2026-08-22
direction trial proved on live firmware that NEGATIVE P = CHARGE and positive
P = DISCHARGE at the ``0x0200`` ``[1, P, Q]`` objective, measured the
unrenewed watchdog expiry at ~3.5-4.0 s, and corroborated the vendor 1 s and
prior-integration 1.5 s renewal cadences as the commissioned envelope.

SAFETY: no test here may ever contact hardware, open a socket, or touch
``var/``.  The production transport is faked strictly at its own port by
replaying the authorized live captures (the
``tests/unit/test_live_composition.py`` ``ReplayedLiveTransport`` pattern),
every scenario refuses network connections outright, only replayed-capture
gateways may ever be constructed (asserted per scenario), and persistence is
entirely in-memory (no storage is configured).  A live controller process may
be running from this tree; nothing here restarts, stops, or reconfigures it.

The replay double is *mutable* exactly where the live trial proved device
behavior: a ``0x0200`` write latches the served PQ objective readback
(IoT ``0x1060+17/+18``) and is reflected in the served BMS battery-power word
(``0x5000+8``) with the proven sign, so a charge command comes back as
negative measured battery watts.  No watchdog-expiry model is replayed; that
behavior stays owned by the deterministic simulator suite.

Red-phase note: the write-enabled run-mode composition does not exist yet.
Every missing contract below must surface as an ordinary test failure, never
as a collection error, so the production modules are loaded lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import itertools
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from energypod.adapters.modbus import (
    WaveshareTransportConfig,
    encode_pq_registers,
    protocol_codec,
)
from energypod.application.actor import EnergyPodActor, InhibitCause
from energypod.domain import DecisionStatus, UnitLifecycle
from energypod.runtime.config import ControllerConfig

# --- the commissioned unit, as pinned by the 2026-08-22 captures and trial -----
#
# docs/evidence/field-mapping-2026-08-22.md: MID is the gateway the authorized
# direction trial wrote its single bounded -200 W commissioning objective to,
# so the charge path below is pinned on the very unit the sign convention was
# proven on.  Captured baseline: 192.3 V pack, +38 W battery power, SOC 10 %
# (both views), 7692 W dynamic charge limit, 60 cells over 6 BIC, identity
# byd-2c225097 decoded from the wire RTU-ID pair at 0x8106.

_UNIT_ID = "mid"
_UNIT_HOST = "192.168.1.11"
_UNIT_PORT = 4196
_UNIT_DEVICE_ID = 4
_UNIT_IDENTITY = "byd-2c225097"
_UNIT_CELL_COUNT = 60
_SITE_ID = "home"

# The only evidenced writable objective (PROTOCOL_EVIDENCE 4b/5): FC16 at
# 0x0200 with the three-register frame [1, signed P, signed Q].
_PQ_ADDRESS = 0x0200
_STOP_FRAME = (1, 0, 0)

# IoT PCS detailed-state block 0x1060: the served active/reactive power
# objective readback the arm-time preflight consumes (offsets +17/+18).
_OBJECTIVE_READBACK_ADDRESS = 0x1071
# BMS live block 0x5000: the signed, unscaled battery-power word the wire
# decoder reads as ``battery_watts`` (offset +8).  POSITIVE = DISCHARGE and
# NEGATIVE = CHARGE, live-proven 2026-08-22.
_BMS_BATTERY_POWER_ADDRESS = 0x5008

# The expiry-evidence reference a write-enabled composition must carry: it
# names the measured live watchdog trial (the 2026-08-22 direction trial whose
# unrenewed objective expiry was measured at ~3.5-4.0 s).  Anything else — the
# observe-only placeholder spelling, a TODO — must be refused at validation
# (API_CONTRACTS "Write-enabled run mode" bullet 1).
_LIVE_TRIAL_EVIDENCE = "live-trial://direction-2026-08-22/rev-1"
_PLACEHOLDER_EVIDENCE = "commissioning://watchdog-trial-2026-08/rev-1"

# The commissioned timing budget: the 0.40 s cadence and 2.35 s expiry both
# sit strictly inside the measured watchdog window with margin, and the
# cadence stays inside the corroborated 1 s / 1.5 s envelope.
_CONTROL_PERIOD_S = 0.40
_DEVICE_COMMAND_EXPIRY_S = 2.35
_AUTHORIZATION_LIFETIME_S = 0.75
_STABLE_SAMPLES_TO_REARM = 3
_CHARGE_W = 250

_STRUCTURAL_LOCK = 2**63 - 1

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_CAPTURE_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-2026-08-22.json"
_FOLLOWUP_CAPTURE_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-followup-2026-08-22.json"


@dataclass(frozen=True)
class OperatorPrincipal:
    """The fully privileged interactive principal used as facade input.

    Exactly the principal that CAN arm a unit, so every arming refusal below
    is a structural refusal of the write-enabled contract, never an
    authorization failure.
    """

    subject: str = "person:commissioning-operator"
    scopes: frozenset[str] = frozenset(
        {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
    )
    interactive: bool = True
    site_id: str = _SITE_ID


OPERATOR = OperatorPrincipal()


class ManualClock:
    """The single deterministic time source a composed runtime may read.

    Time moves only through explicit ``advance()`` calls, so renewal cadences,
    authorization lifetimes, and telemetry ages are all exactly what the test
    script says they are — never a side effect of an await.
    """

    def __init__(self, *, start: float = 1000.0) -> None:
        self.now = start
        self.wall = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
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


# --- configuration payloads ----------------------------------------------------


def _timing_payload(
    *,
    evidence: str = _LIVE_TRIAL_EVIDENCE,
    control_period_s: float = _CONTROL_PERIOD_S,
    device_command_expiry_s: float = _DEVICE_COMMAND_EXPIRY_S,
) -> dict[str, Any]:
    return {
        "device_command_expiry_s": device_command_expiry_s,
        "device_command_expiry_evidence": evidence,
        "control_period_s": control_period_s,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _policy_payload(
    *, authorization_lifetime_s: float = _AUTHORIZATION_LIFETIME_S
) -> dict[str, Any]:
    return {
        "version": 4,
        "threshold_provenance": "live-commissioning-2026-08-22",
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
        "authorization_lifetime_s": authorization_lifetime_s,
        "ramp_limit_w_per_s": 1000,
        "stable_samples_to_rearm": _STABLE_SAMPLES_TO_REARM,
        "reactive_power_limit_var": 0,
        "blocking_fault_codes": [
            "PCS_EE_CALIBRATION_OUT_OF_RANGE",
            "DCDC_EE_CALIBRATION_OUT_OF_RANGE",
        ],
        "debug_modes_enabled": False,
    }


def _authentication_payload() -> dict[str, Any]:
    return {
        "enabled": True,
        "operator_credential_ref": "secret://energypod/live-operator-credential",
        "trusted_proxy_cidrs": ["192.168.1.0/24"],
    }


def _config_payload(
    *,
    mode: str,
    evidence: str = _LIVE_TRIAL_EVIDENCE,
    control_period_s: float = _CONTROL_PERIOD_S,
    device_command_expiry_s: float = _DEVICE_COMMAND_EXPIRY_S,
    authorization_lifetime_s: float = _AUTHORIZATION_LIFETIME_S,
) -> dict[str, Any]:
    """One commissioned single-unit live config; only the knobs under test vary.

    No ``storage`` is ever configured: persistence stays entirely in-memory
    (API_CONTRACTS "Runtime composition and entry point"), so nothing here
    opens a database file or touches ``var/``.
    """
    return {
        "schema_version": 1,
        "revision": 9,
        "mode": mode,
        "site": {
            "site_id": _SITE_ID,
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [
            {
                "unit_id": _UNIT_ID,
                "display_name": "MID",
                "endpoint": {"host": _UNIT_HOST, "port": _UNIT_PORT},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": _UNIT_DEVICE_ID,
                "expected_identity": _UNIT_IDENTITY,
                "expected_cell_count": _UNIT_CELL_COUNT,
            }
        ],
        "timing": _timing_payload(
            evidence=evidence,
            control_period_s=control_period_s,
            device_command_expiry_s=device_command_expiry_s,
        ),
        "policy": _policy_payload(authorization_lifetime_s=authorization_lifetime_s),
        "authentication": _authentication_payload(),
    }


def _composition() -> ModuleType:
    try:
        return importlib.import_module("energypod.runtime.composition")
    except ImportError as error:
        pytest.fail(f"the runtime composition root is not implemented: {error}")
        raise  # pragma: no cover - pytest.fail never returns


def _build(config: ControllerConfig, clock: ManualClock) -> Any:
    """Compose RUN mode: the simulate flag is never set by this suite."""
    factory = getattr(_composition(), "build_runtime", None)
    if not callable(factory):
        pytest.fail("energypod.runtime.composition.build_runtime is not implemented", pytrace=False)
    return factory(config, clock=clock)


def _validate(payload: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(payload)


# --- the replayed transport port ------------------------------------------------


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
    writes: list[tuple[str, int, tuple[int, ...], float]] = field(default_factory=list)
    closes: list[str] = field(default_factory=list)
    # The gateway hosts the authorized captures actually cover.
    captured_hosts: frozenset[str] = field(default_factory=frozenset)
    # Fail-closed preflight probe: the served objective readback is unreadable.
    refuse_objective_readback: bool = False

    def pq_frames(self, host: str) -> tuple[tuple[int, tuple[int, ...]], ...]:
        return tuple(
            (address, values) for served, address, values, _ in self.writes if served == host
        )

    def nonzero_pq_frames(self, host: str) -> tuple[tuple[int, tuple[int, ...]], ...]:
        return tuple(
            (address, values)
            for address, values in self.pq_frames(host)
            if any(protocol_codec.decode_signed16(word) != 0 for word in values[1:])
        )

    def write_times(self, host: str) -> tuple[float, ...]:
        return tuple(at_mono for served, _, _, at_mono in self.writes if served == host)

    def assert_only_pq_frames(self) -> None:
        """The transport-gate pin: no composition, in any mode, writes anything
        other than the evidenced three-register PQ objective at 0x0200.

        (API_CONTRACTS "Write-enabled run mode": the transport write gate is
        unchanged and no other address is writable by any composition, mode,
        or tool.)
        """
        for served, address, values, _ in self.writes:
            assert address == _PQ_ADDRESS and len(values) == 3 and values[0] == 1, (
                f"run mode wrote {address:#06x} <- {values} on {served}: the only evidenced "
                "writable objective is the three-register [1, P, Q] frame at 0x0200"
            )
            assert all(
                isinstance(word, int) and not isinstance(word, bool) and 0 <= word <= 0xFFFF
                for word in values
            ), f"run mode wrote non-register words {values} on {served}"

    def assert_only_replayed_gateways(self) -> None:
        """The no-network pin: the only transports ever constructed were the
        replay doubles serving the authorized captures."""
        for config in self.constructions:
            assert config.host in self.captured_hosts, (
                f"run mode constructed a transport to gateway {config.host!r}, which no "
                "authorized live capture covers — a composition under replay must never "
                "reach for real hardware"
            )


def _install_replay_transport(
    monkeypatch: pytest.MonkeyPatch, clock: ManualClock
) -> tuple[ReplayJournal, dict[str, dict[int, int]]]:
    """Serve the golden captures at the production transport port, per gateway.

    The composition's lazy production factory resolves the name
    ``WaveshareTransport`` from the composition module at first use, so
    replacing exactly that name fakes the real transport at its own port: the
    composed per-unit wiring routes the actor to its unit's captured register
    bank, and nothing here opens a socket.  The banks are mutable, per host,
    exactly where the live direction trial proved device behavior: a ``0x0200``
    write latches the served objective readback (0x1060+17/+18) and reflects
    the applied objective in the served battery-power word (0x5000+8) with the
    proven sign, so measured power follows the commanded objective.
    """
    banks = {host: dict(bank) for host, bank in _register_banks_by_host().items()}
    journal = ReplayJournal()
    journal.captured_hosts = frozenset(banks)

    class ReplayedLiveTransport:
        """A Waveshare gateway replaying one unit's authorized capture."""

        def __init__(self, *, config: WaveshareTransportConfig) -> None:
            if config.host not in banks:
                raise AssertionError(
                    f"the run-mode transport was wired to gateway {config.host!r}, which no "
                    "authorized live capture covers"
                )
            self._host = config.host
            journal.constructions.append(config)

        async def connect(self) -> None:
            journal.connects.append(self._host)

        async def read_holding(self, address: int, count: int) -> tuple[int, ...]:
            journal.reads.append((self._host, address, count))
            if journal.refuse_objective_readback and address <= _OBJECTIVE_READBACK_ADDRESS < (
                address + count
            ):
                raise OSError("the served PQ objective readback is unreadable")
            try:
                return tuple(banks[self._host][address + offset] for offset in range(count))
            except KeyError as error:
                raise AssertionError(
                    f"the run-mode read plan requested register {error.args[0]} (window "
                    f"{address:#06x} x {count}) that the authorized capture does not serve"
                ) from error

        async def write_registers(self, address: int, values: Sequence[int]) -> None:
            # Same write gate as the production transport: only the evidenced
            # three-register PQ objective is writable, so any composed write
            # path shows up in the journal instead of failing silently.
            registers = tuple(int(word) for word in values)
            if address != _PQ_ADDRESS or len(registers) != 3 or registers[0] != 1:
                raise ValueError("only the evidenced three-register PQ objective is writable")
            # Live-proven device behavior (PROTOCOL_EVIDENCE 4b): the objective
            # readback echoes the applied word, and battery power follows the
            # applied objective — negative for a charge command.
            banks[self._host][_OBJECTIVE_READBACK_ADDRESS] = registers[1]
            banks[self._host][_OBJECTIVE_READBACK_ADDRESS + 1] = registers[2]
            banks[self._host][_BMS_BATTERY_POWER_ADDRESS] = registers[1]
            journal.writes.append((self._host, address, registers, clock.monotonic()))

        async def close(self) -> None:
            journal.closes.append(self._host)

    monkeypatch.setattr(_composition(), "WaveshareTransport", ReplayedLiveTransport)
    return journal, banks


def _forbid_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every scenario in this suite runs with network connections refused.

    On Windows the proactor event loop's own self-pipe machinery isinstance-
    checks against ``socket.socket``, so replacing the class itself would
    break loop scheduling in these mailbox-driven scenarios.  The safety pin
    is structural instead, exactly as in ``test_live_composition.py``: the
    replay double installed at the composition module is the only transport
    class the runtime can construct, it never touches a socket, and the
    journal records every construction (asserted against the captured
    gateways) and every connection it served.
    """

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("write-enabled run tests must never perform network I/O")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "socketpair", refused)


# --- drive helpers -------------------------------------------------------------


def _unit_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    units = {view["unit_id"]: view for view in snapshot["units"]}
    assert _UNIT_ID in units, f"the snapshot must include {_UNIT_ID}: {list(units)}"
    return units[_UNIT_ID]


async def _shutdown_actors(runtime: Any) -> None:
    for actor in runtime.actors.values():
        with contextlib.suppress(Exception):
            await actor.shutdown()


def _assert_replay_safety(journal: ReplayJournal) -> None:
    """The pins every replay scenario ends with: only PQ frames were written,
    and only replayed-capture gateways were ever constructed."""
    journal.assert_only_pq_frames()
    journal.assert_only_replayed_gateways()


async def _qualify(runtime: Any, clock: ManualClock) -> None:
    """Stable identity-pinned live decodes carry the unit into DISARMED."""
    actor = runtime.actors[_UNIT_ID]
    for _ in range(runtime.policy.stable_samples_needed_to_rearm):
        clock.advance(0.05)
        await actor.poll_once()
    assert actor.qualified is True, "live-decoded stable telemetry must qualify the unit"
    assert actor.lifecycle is UnitLifecycle.DISARMED


async def _arm(runtime: Any) -> None:
    result = await runtime.facade.arm(
        unit_ids=[_UNIT_ID],
        principal=OPERATOR,
        idempotency_key="write-enabled-arm",
        request_id="write-enabled-arm-request",
    )
    outcomes = {unit["unit_id"]: unit["status"] for unit in result["units"]}
    assert outcomes == {_UNIT_ID: "armed"}, result


async def _drive_to_active(runtime: Any, clock: ManualClock, *, cycles: int = 1) -> None:
    """Qualify, arm, dispatch the charge intent, and renew ``cycles`` times."""
    await _qualify(runtime, clock)
    await _arm(runtime)
    clock.advance(_CONTROL_PERIOD_S)
    await runtime.actors[_UNIT_ID].poll_once()
    view = await runtime.facade.submit_intent(
        unit_ids=[_UNIT_ID],
        direction="charge",
        watts=_CHARGE_W,
        ttl_s=30.0,
        reason="write-enabled replay scenario",
        principal=OPERATOR,
        idempotency_key="write-enabled-dispatch",
        request_id="write-enabled-dispatch-request",
    )
    assert view["status"] == "accepted", view
    for _ in range(cycles):
        clock.advance(_CONTROL_PERIOD_S)
        await runtime.kernel.tick()
        await runtime.actors[_UNIT_ID].heartbeat_once()
        await runtime.actors[_UNIT_ID].poll_once()
    assert runtime.actors[_UNIT_ID].lifecycle is UnitLifecycle.ACTIVE


# --- tests ----------------------------------------------------------------------


async def test_write_enabled_run_mode_composes_the_policy_qualification_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 2, first half: the actor's threshold IS the policy's count.

    ``mode: write_enabled`` with the policy, enabled authentication, and the
    live-trial expiry evidence composes actors whose stable-observation
    qualification threshold is exactly the policy's
    ``stable_samples_needed_to_rearm`` — never the structural 2**63-1 lock —
    and identity-pinned live decode qualifies a unit exactly as the simulator
    does: one sample short it stays unqualified, at the policy count it
    reaches DISARMED.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, _banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        assert isinstance(actor, EnergyPodActor)
        required = runtime.policy.stable_samples_needed_to_rearm
        assert required == _STABLE_SAMPLES_TO_REARM, (
            "the composed policy must carry the configured commissioning count"
        )
        structural_lock = getattr(_composition(), "_RUN_MODE_STABLE_SAMPLES_REQUIRED", None)
        assert structural_lock == _STRUCTURAL_LOCK, (
            "the structural never-qualify constant itself must stay beyond any reachable count"
        )
        assert actor._stable_required == required, (
            "write-enabled run mode must wire the policy's stable_samples_needed_to_rearm as "
            f"the qualification threshold, saw {actor._stable_required!r}"
        )
        assert required < _STRUCTURAL_LOCK, "the wired threshold must not be the structural lock"

        await actor.start()
        for _ in range(required - 1):
            clock.advance(0.05)
            await actor.poll_once()
        assert actor.qualified is False, (
            "one sample short of the policy count the unit must stay unqualified"
        )
        clock.advance(0.05)
        await actor.poll_once()
        observation = await runtime.observations.latest(_UNIT_ID)
        assert observation is not None
        assert observation.device_identity == _UNIT_IDENTITY, (
            "qualification evidence is the identity-pinned live decode, never configuration"
        )
        assert actor.qualified is True
        assert actor.lifecycle is UnitLifecycle.DISARMED
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


async def test_observe_only_run_mode_keeps_the_structural_qualification_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 2, second half: observe-only keeps the structural lock.

    The very same commissioned configuration with ``mode: observe_only``
    (policy, authentication, and live-trial evidence unchanged — the mode
    alone flips the wiring) keeps the never-qualify threshold: no telemetry
    count can ever carry a live observe-only unit into DISARMED, the facade
    refuses arm for the fully privileged interactive principal, and no write
    of any kind reaches the unit before shutdown.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, _banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="observe_only")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        structural_lock = getattr(_composition(), "_RUN_MODE_STABLE_SAMPLES_REQUIRED", None)
        assert structural_lock == _STRUCTURAL_LOCK
        assert actor._stable_required == _STRUCTURAL_LOCK, (
            "observe-only run mode must keep the structural never-qualify wiring unchanged"
        )

        await actor.start()
        for _ in range(runtime.policy.stable_samples_needed_to_rearm + 2):
            clock.advance(0.05)
            await actor.poll_once()
        assert actor.qualified is not True, (
            "no amount of coherent replayed telemetry may qualify an observe-only live unit"
        )
        assert actor.lifecycle is UnitLifecycle.OBSERVE_ONLY

        result = await runtime.facade.arm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="observe-only-arm-refusal",
            request_id="observe-only-arm-refusal-request",
        )
        assert result["units"][0]["status"] == "refused"
        assert journal.pq_frames(_UNIT_HOST) == (), (
            "an unqualified observe-only unit must never be written to"
        )
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


def test_write_enabled_mode_requires_the_live_trial_expiry_evidence() -> None:
    """Bullet 1: the evidence reference must name the measured live trial.

    The commissioned spelling references the 2026-08-22 direction trial that
    measured the ~3.5-4.0 s unrenewed objective expiry.  A write-enabled
    configuration whose ``timing.device_command_expiry_evidence`` does not
    reference that trial — the observe-only placeholder spelling, or any TODO
    string — is refused at validation.  The gate is mode-scoped: observe-only
    deployments keep accepting the placeholder because they never write.
    """
    commissioned = _validate(_config_payload(mode="write_enabled"))
    assert commissioned.timing.device_command_expiry_evidence == _LIVE_TRIAL_EVIDENCE

    for placeholder in (_PLACEHOLDER_EVIDENCE, "evidence://tbd", "placeholder"):
        payload = _config_payload(mode="write_enabled", evidence=placeholder)
        with pytest.raises(ValidationError, match="device_command_expiry_evidence"):
            _validate(payload)

    observe_only = _validate(_config_payload(mode="observe_only", evidence=_PLACEHOLDER_EVIDENCE))
    assert observe_only.timing.device_command_expiry_evidence == _PLACEHOLDER_EVIDENCE


def test_write_enabled_renewal_cadence_cannot_exceed_the_corroborated_envelope() -> None:
    """Bullet 1, budget pin: the commissioned cadence fits the proven envelope.

    The vendor 1 s and prior-integration 1.5 s renewal cadences are the
    corroborated envelope, so a write-enabled cadence above 1.5 s is refused
    even when the arithmetic budget still fits inside the commissioned
    expiry; exactly 1.5 s commissions.  The existing complete-budget refusal
    — a cadence budget that no longer fits strictly inside the expiry at all
    — stays green, and observe-only keeps the slower cadence because it never
    writes (the 9.0 s expiry placeholder doctrine, PROTOCOL_EVIDENCE 4b).
    """
    boundary = _validate(
        _config_payload(
            mode="write_enabled",
            control_period_s=1.5,
            device_command_expiry_s=3.0,
            authorization_lifetime_s=2.0,
        )
    )
    assert boundary.timing.control_period_s == 1.5

    one_notch_slower = _config_payload(
        mode="write_enabled",
        control_period_s=1.6,
        device_command_expiry_s=3.0,
        authorization_lifetime_s=2.0,
    )
    with pytest.raises(ValidationError, match="control_period_s"):
        _validate(one_notch_slower)

    overruns_expiry = _config_payload(
        mode="write_enabled",
        control_period_s=2.4,
        device_command_expiry_s=3.0,
        authorization_lifetime_s=2.45,
    )
    with pytest.raises(
        ValidationError, match="control renewal budget must fit inside device command expiry"
    ):
        _validate(overruns_expiry)

    slow_observe_only = _validate(
        _config_payload(
            mode="observe_only",
            control_period_s=2.4,
            device_command_expiry_s=3.0,
            authorization_lifetime_s=2.45,
        )
    )
    assert slow_observe_only.timing.control_period_s == 2.4


async def test_write_enabled_control_loop_drives_replayed_live_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 2 end to end: qualification, arm, dispatch, renew, measure, stop.

    Over the replayed live capture of the unit the direction trial was proven
    on: identity-pinned live decode qualifies the unit, the facade arms it, a
    bounded charge intent is dispatched, the kernel tick mints authority, and
    the actor heartbeat writes ``[1, negative-P, 0]`` (negative P = CHARGE,
    live-proven) through the production transport fake at the control-period
    cadence.  Renewal repeats while authorized, the applied objective comes
    back as negative measured battery power in the snapshot, and the
    emergency stop fences, delivers the bounded zero ``[1,0,0]``, and stops
    renewal.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        await actor.start()

        # Qualification from live-decoded, identity-pinned observations.
        for _ in range(runtime.policy.stable_samples_needed_to_rearm - 1):
            clock.advance(0.05)
            await actor.poll_once()
        assert actor.qualified is False
        clock.advance(0.05)
        await actor.poll_once()
        observation = await runtime.observations.latest(_UNIT_ID)
        assert observation is not None
        assert observation.device_identity == _UNIT_IDENTITY
        assert actor.lifecycle is UnitLifecycle.DISARMED

        # The facade arm succeeds: the single-writer preflight found the
        # captured zero objective baseline.
        await _arm(runtime)
        assert actor.lifecycle is UnitLifecycle.ARMED_IDLE
        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        armed_evidence = await runtime.observations.latest(_UNIT_ID)
        assert armed_evidence is not None
        assert armed_evidence.lifecycle is UnitLifecycle.ARMED_IDLE

        # Dispatch a bounded charge intent; acceptance grants nothing.
        view = await runtime.facade.submit_intent(
            unit_ids=[_UNIT_ID],
            direction="charge",
            watts=_CHARGE_W,
            ttl_s=30.0,
            reason="write-enabled replay scenario",
            principal=OPERATOR,
            idempotency_key="write-enabled-e2e-dispatch",
            request_id="write-enabled-e2e-dispatch-request",
        )
        assert view["status"] == "accepted", view
        assert view["requested"] == {"direction": "charge", "watts": _CHARGE_W}
        assert view["authorized"] is None
        assert view["measured"] is None

        # The kernel tick mints authority against the armed evidence.
        decision = await runtime.kernel.tick()
        assert decision is not None
        assert decision.status is DecisionStatus.AUTHORIZED
        assert decision.reason_codes == ("safety_checks_passed",)
        assert await runtime.authorizations.peek(_UNIT_ID) is not None
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = _unit_view(snapshot)
        assert unit["authorized_power"] == {"direction": "charge", "watts": _CHARGE_W}

        # The heartbeat writes [1, negative-P, 0] for the charge intent.
        await actor.heartbeat_once()
        frames = journal.pq_frames(_UNIT_HOST)
        assert len(frames) == 1, frames
        address, values = frames[0]
        assert (address, values) == (_PQ_ADDRESS, encode_pq_registers(-_CHARGE_W, 0)), (
            "a charge intent must be written as [1, negative-P, 0]: negative P = CHARGE, "
            f"live-proven 2026-08-22, saw {values!r}"
        )
        assert protocol_codec.decode_signed16(values[1]) == -_CHARGE_W
        assert protocol_codec.decode_signed16(values[2]) == 0
        assert actor.lifecycle is UnitLifecycle.ACTIVE

        # The single-use capability was consumed by the write.
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = _unit_view(snapshot)
        assert unit["authorized_power"] is None
        assert unit["lifecycle"] == "active"

        # Renewal repeats while authorized, at the control-period cadence.
        for _ in range(2):
            clock.advance(_CONTROL_PERIOD_S)
            await runtime.kernel.tick()
            await actor.heartbeat_once()
            await actor.poll_once()
        frames = journal.pq_frames(_UNIT_HOST)
        assert len(frames) == 3, frames
        assert all(values == encode_pq_registers(-_CHARGE_W, 0) for _address, values in frames)
        times = journal.write_times(_UNIT_HOST)
        gaps = [second - first for first, second in itertools.pairwise(times)]
        assert gaps == [pytest.approx(_CONTROL_PERIOD_S)] * len(gaps), (
            f"renewal writes must follow the {_CONTROL_PERIOD_S} s control period, saw {gaps}"
        )
        decisions = [
            event
            for event in runtime.audit.recent(limit=16)
            if getattr(event, "event_type", None) == "control_decision"
        ]
        cycle_ids = {event.cycle_id for event in decisions}
        assert len(cycle_ids) >= 3, "every renewal cycle must mint a fresh cycle id"

        # Measured-power reflection: the applied objective comes back as
        # negative battery watts — negative means charging, live-proven.
        measured = await runtime.observations.latest(_UNIT_ID)
        assert measured is not None
        assert measured.battery_watts == -float(_CHARGE_W)
        assert measured.lifecycle is UnitLifecycle.ACTIVE
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = _unit_view(snapshot)
        assert unit["measured_watts"] == -float(_CHARGE_W)
        assert unit["requested_power"] == {"direction": "charge", "watts": _CHARGE_W}
        assert (
            protocol_codec.decode_signed16(banks[_UNIT_HOST][_OBJECTIVE_READBACK_ADDRESS])
            == -_CHARGE_W
        ), "the served objective readback must echo our own applied objective"

        # Emergency stop: fence, bounded zero, renewal stops.
        nonzero_before = len(journal.nonzero_pq_frames(_UNIT_HOST))
        assert nonzero_before == 3
        stop = await runtime.facade.emergency_stop(
            unit_ids=[_UNIT_ID],
            reason="write-enabled replay halt",
            principal=OPERATOR,
        )
        assert stop["status"] == "latched", stop
        assert await runtime.authorizations.peek(_UNIT_ID) is None, (
            "the stop must fence and revoke outstanding authority before returning"
        )
        assert (_PQ_ADDRESS, _STOP_FRAME) in journal.pq_frames(_UNIT_HOST), (
            "the emergency stop must deliver the bounded zero [1,0,0]"
        )
        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = _unit_view(snapshot)
        assert unit["measured_watts"] == 0, "the bounded zero must reach the device"
        assert unit["authorized_power"] is None

        for _ in range(2):
            clock.advance(_CONTROL_PERIOD_S)
            await runtime.kernel.tick()
            await actor.heartbeat_once()
            await actor.poll_once()
        assert len(journal.nonzero_pq_frames(_UNIT_HOST)) == nonzero_before, (
            "a latched stop during ACTIVE must stop renewal writes immediately"
        )
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        unit = _unit_view(snapshot)
        assert unit["lifecycle"] != "active"
        assert unit["authorized_power"] is None
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


async def test_external_writer_objective_latches_the_arm_until_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 3 wiring: a nonzero foreign objective at arm time latches.

    Single-writer authority is structural, not assumed: at arm time the actor
    reads the served PQ objective readback (IoT 0x1060+17).  An objective the
    controller did not itself write — the positive discharge objective an
    external writer left behind — refuses the arm and latches INHIBITED with
    the ``external_writer`` cause (privileged acknowledgement required).
    The controller's own applied objective is not foreign, and while the
    foreign objective persists a later arm re-latches.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        await actor.start()

        # Positive control: drive the unit ACTIVE so the readback holds the
        # controller's own applied objective, then disarm and re-arm.
        await _drive_to_active(runtime, clock)
        assert (
            protocol_codec.decode_signed16(banks[_UNIT_HOST][_OBJECTIVE_READBACK_ADDRESS])
            == -_CHARGE_W
        )
        disarmed = await runtime.facade.disarm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="external-writer-disarm",
            request_id="external-writer-disarm-request",
        )
        assert {unit["unit_id"]: unit["status"] for unit in disarmed["units"]} == {
            _UNIT_ID: "disarmed"
        }
        await _arm(runtime)
        assert actor.lifecycle is UnitLifecycle.ARMED_IDLE, (
            "an objective the controller itself wrote is not a foreign writer"
        )

        # A foreign writer takes over the objective between our disarm and arm.
        await runtime.facade.disarm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="external-writer-disarm-2",
            request_id="external-writer-disarm-2-request",
        )
        banks[_UNIT_HOST][_OBJECTIVE_READBACK_ADDRESS] = 300  # foreign +300 W discharge
        refused = await runtime.facade.arm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="external-writer-arm",
            request_id="external-writer-arm-request",
        )
        assert refused["units"][0]["status"] == "refused", refused
        assert actor.lifecycle is UnitLifecycle.INHIBITED
        assert actor.inhibit_latched is True
        assert actor.inhibit_cause is InhibitCause.LATCHED
        assert getattr(actor, "inhibit_reason", None) == "external_writer", (
            "the latch must record the external-writer cause for the privileged acknowledgement"
        )
        assert len(journal.nonzero_pq_frames(_UNIT_HOST)) == 1, (
            "the refused arm path must never write a nonzero objective"
        )

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        assert _unit_view(snapshot)["lifecycle"] == "inhibited"

        # Acknowledgement clears only the latch; the unit still needs the
        # configured count of stable qualifying observations to reach DISARMED.
        await actor.acknowledge_inhibit()
        assert actor.inhibit_latched is False
        for _ in range(runtime.policy.stable_samples_needed_to_rearm):
            clock.advance(0.05)
            await actor.poll_once()
        assert actor.lifecycle is UnitLifecycle.DISARMED

        # The foreign objective persists (the external writer re-asserts it),
        # so the next arm re-latches.
        banks[_UNIT_HOST][_OBJECTIVE_READBACK_ADDRESS] = 300
        relatched = await runtime.facade.arm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="external-writer-rearm",
            request_id="external-writer-rearm-request",
        )
        assert relatched["units"][0]["status"] == "refused"
        assert relatched["units"][0]["reason"] == "inhibit_latched"
        assert actor.inhibit_latched is True
        assert actor.lifecycle is UnitLifecycle.INHIBITED
        assert len(journal.nonzero_pq_frames(_UNIT_HOST)) == 1
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


async def test_unreadable_objective_readback_refuses_the_arm_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 3 wiring: an unreadable readback refuses the arm fail-closed.

    The preflight cannot establish single-writer authority when the served
    objective readback cannot be read, so the arm is refused without ever
    transitioning the unit into ARMED_IDLE.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, _banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        await actor.start()
        await _qualify(runtime, clock)

        journal.refuse_objective_readback = True
        result = await runtime.facade.arm(
            unit_ids=[_UNIT_ID],
            principal=OPERATOR,
            idempotency_key="unreadable-readback-arm",
            request_id="unreadable-readback-arm-request",
        )
        assert result["units"][0]["status"] == "refused", result
        assert actor.lifecycle not in {UnitLifecycle.ARMED_IDLE, UnitLifecycle.ACTIVE}, (
            "an unreadable objective readback must never leave the unit armed"
        )
        assert journal.nonzero_pq_frames(_UNIT_HOST) == ()
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


async def test_latched_inhibit_mid_active_stops_renewal_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 5: a latched inhibit during ACTIVE is dominated by bounded zero.

    With renewal actively driving the unit, the wire starts serving another
    device's identity: the next live decode contradicts the commissioned
    identity, the actor latches INHIBITED (identity mismatch is a LATCHED
    cause), the bounded zero ``[1,0,0]`` is issued immediately, and renewal
    writes stop even though the charge intent is still live and the kernel
    keeps ticking.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        await actor.start()
        await _drive_to_active(runtime, clock)
        nonzero_before = len(journal.nonzero_pq_frames(_UNIT_HOST))
        assert nonzero_before >= 1

        # The served identity changes underneath a live, driving unit.
        banks[_UNIT_HOST][0x8106] = 0x000B
        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        assert actor.lifecycle is UnitLifecycle.INHIBITED
        assert actor.inhibit_latched is True
        assert actor.inhibit_cause is InhibitCause.LATCHED
        assert (_PQ_ADDRESS, _STOP_FRAME) in journal.pq_frames(_UNIT_HOST), (
            "the latched inhibit must issue the bounded zero immediately"
        )

        clock.advance(_CONTROL_PERIOD_S)
        await actor.poll_once()
        measured = await runtime.observations.latest(_UNIT_ID)
        assert measured is not None
        assert measured.battery_watts == 0, "the bounded zero must dominate the applied objective"

        # Renewal stops despite the live intent and continued kernel ticks.
        for _ in range(2):
            clock.advance(_CONTROL_PERIOD_S)
            await runtime.kernel.tick()
            await actor.heartbeat_once()
            await actor.poll_once()
        assert len(journal.nonzero_pq_frames(_UNIT_HOST)) == nonzero_before, (
            "an inhibited unit must never renew a nonzero objective"
        )
        assert await runtime.authorizations.peek(_UNIT_ID) is None
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)


async def test_shutdown_stops_renewal_and_closes_the_transport_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullet 5: shutdown stops renewal, zeroes, and closes once, cleanly.

    Cancelling the actor mid-ACTIVE revokes and fences, attempts the bounded
    zero before closing the transport, closes it exactly once, and later
    control cycles write nothing.  A second shutdown is a clean no-op.
    """
    _forbid_network_connections(monkeypatch)
    clock = ManualClock()
    journal, _banks = _install_replay_transport(monkeypatch, clock)
    runtime = _build(_validate(_config_payload(mode="write_enabled")), clock)

    try:
        actor = runtime.actors[_UNIT_ID]
        await actor.start()
        await _drive_to_active(runtime, clock, cycles=2)
        nonzero_before = len(journal.nonzero_pq_frames(_UNIT_HOST))
        assert nonzero_before == 2

        await actor.shutdown()
        assert actor.lifecycle is UnitLifecycle.STOPPING
        assert journal.closes == [_UNIT_HOST], (
            "shutdown must close the unit's transport exactly once"
        )
        assert (_PQ_ADDRESS, _STOP_FRAME) in journal.pq_frames(_UNIT_HOST), (
            "shutdown owes one bounded zero attempt before closing the transport"
        )

        # Renewal is dead: further control cycles write nothing at all.
        for _ in range(2):
            clock.advance(_CONTROL_PERIOD_S)
            await runtime.kernel.tick()
            await actor.heartbeat_once()
            await actor.poll_once()
        assert journal.pq_frames(_UNIT_HOST)[-1] == (_PQ_ADDRESS, _STOP_FRAME)
        assert len(journal.nonzero_pq_frames(_UNIT_HOST)) == nonzero_before
        assert journal.closes == [_UNIT_HOST]

        # Shutdown is idempotent and never raises on the second call.
        await actor.shutdown()
        assert journal.closes == [_UNIT_HOST]
    finally:
        await _shutdown_actors(runtime)

    _assert_replay_safety(journal)
