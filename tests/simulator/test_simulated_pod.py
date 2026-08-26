"""Deterministic simulator device and transport contracts (ADR-0003 decision D4).

The simulated unit is pinned through the actor transport port against the production
register-layout, protocol-codec, and fault decoders.  Production simulator modules do
not exist yet: they are loaded lazily so this red-phase suite still collects, and every
pinned behavior is an ordinary test failure until the contract is implemented.

Pinned device-model surface: `SimulatedEnergyPod(clock=..., identity=..., bic_count=...,
seed=..., watchdog_timeout_s=..., cell_poll_interval_s=...)` driven exclusively by the
injected monotonic clock, with `telemetry_sequence`, `telemetry_captured_at_mono`,
`cell_sequence`, and `cell_captured_at_mono` observation metadata, an explicit `poll()`
device-model step (time advances only through the injected clock), and the scenario
hooks `inject_fault(prefix, bit)` / `clear_fault(prefix, bit)`, `drop_link()` /
`restore_link()`, and `inject_malformed_register(address)`.

Measured telemetry is pinned as a deterministic, directionally correct function of the
applied setpoint and scripted time (API_CONTRACTS "Deterministic simulator"); bit-exact
equality between measured battery watts and the commanded P is deliberately not pinned.
Energy and objective expectations are computed with literal word arithmetic per the
PROTOCOL_EVIDENCE word orders, never through the production decoder round trip.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.adapters.modbus import faults, protocol_codec, register_layout


class _MissingContract:
    def __init__(self, message: str) -> None:
        self.message = message

    def __getattr__(self, name: str) -> Any:
        pytest.fail(self.message, pytrace=False)


@pytest.fixture(scope="module")
def simulator() -> Any:
    try:
        pod_module = importlib.import_module("energypod.simulator.pod")
        transport_module = importlib.import_module("energypod.simulator.transport")
        return SimpleNamespace(
            SimulatedEnergyPod=pod_module.SimulatedEnergyPod,
            SimulatorTransport=transport_module.SimulatorTransport,
        )
    except (ImportError, AttributeError) as error:
        return _MissingContract(f"Simulator contract is not implemented: {error}")


class FakeMonotonicClock:
    """Injected monotonic time; the device model may read no other clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_unit(
    simulator: Any,
    *,
    clock: FakeMonotonicClock | None = None,
    identity: str = "SIM-BEP-0042",
    bic_count: int = 6,
    seed: int = 20260822,
    watchdog_timeout_s: float = 2.0,
    cell_poll_interval_s: float = 5.0,
) -> tuple[Any, Any, FakeMonotonicClock]:
    injected_clock = clock if clock is not None else FakeMonotonicClock()
    pod = simulator.SimulatedEnergyPod(
        clock=injected_clock,
        identity=identity,
        bic_count=bic_count,
        seed=seed,
        watchdog_timeout_s=watchdog_timeout_s,
        cell_poll_interval_s=cell_poll_interval_s,
    )
    transport = simulator.SimulatorTransport(pod=pod)
    return pod, transport, injected_clock


async def read_live_block(transport: Any) -> tuple[int, ...]:
    """Read the IoT BMS live block at 0x5000 (31 registers)."""
    return await transport.read_holding(0x5000, 31)


def assert_follows_direction(measured_w: int, command_w: int) -> None:
    """Directional correctness of measured battery watts.

    The contract grants determinism and directional correctness only: measured
    watts take the sign of the commanded P with a magnitude bounded by the
    command; bit-exact equality with P is not pinned.
    """
    assert measured_w != 0, "an applied non-zero objective must move measured power"
    assert (measured_w < 0) == (command_w < 0), "measured power must take the sign of P"
    assert abs(measured_w) <= abs(command_w)


async def measured_battery_watts(pod: Any, transport: Any) -> int:
    """Advance the device model one explicit poll, then read measured battery watts."""
    pod.poll()
    registers = await read_live_block(transport)
    return protocol_codec.decode_signed16(registers[8])  # IoT BMS +8: int16 W


async def applied_objectives(transport: Any) -> tuple[int, int]:
    registers = await transport.read_holding(0x1060, 32)
    return (
        protocol_codec.decode_signed16(registers[17]),  # PCS detail +17: active W
        protocol_codec.decode_signed16(registers[18]),  # PCS detail +18: reactive var
    )


def device_state(pod: Any) -> dict[str, Any]:
    """Deep snapshot of every device-model attribute except the injected clock.

    The clock is the scripted-time source external to the device, so device
    state is every attribute the model owns; equality of two snapshots is the
    bit-for-bit "device left unchanged" pin (register bank, latched objective,
    watchdog lease, sequences, and accumulators included).
    """
    return {name: copy.deepcopy(value) for name, value in vars(pod).items() if name != "_clock"}


async def read_iot_plan(transport: Any, bic_count: int) -> dict[int, tuple[int, ...]]:
    data: dict[int, tuple[int, ...]] = {}
    for block in register_layout.RegisterCatalog().iot_reads(bic_count=bic_count):
        registers = await transport.read_holding(block.address, block.count)
        assert len(registers) == block.count
        assert all(type(value) is int and 0 <= value <= 0xFFFF for value in registers)
        data[block.address] = registers
    return data


@pytest.mark.parametrize("bic_count", [1, 3, 6])
async def test_layout_probe_selects_iot_with_consistent_topology(
    simulator: Any, bic_count: int
) -> None:
    """T-SIM-POD-001 / V-LAYOUT / S0."""
    _, transport, _ = build_unit(simulator, bic_count=bic_count)
    await transport.connect()

    probe_registers = await transport.read_holding(0x5000, 7)
    probe = register_layout.detect_layout(probe_registers)

    # Literal probe registers straight from the PROTOCOL_EVIDENCE section 4 table:
    # offset 0 > 10 selects IoT, the enable mask lives at offset 4 (byte-truncated
    # by the vendor, so bits above bit 7 must not be served), and the BIC count is
    # the raw int16 at offset 5.
    assert probe_registers[0] > 10
    assert probe_registers[4] <= 0xFF
    assert probe_registers[5] == bic_count
    assert probe.layout is register_layout.ProtocolLayout.IOT
    assert probe.bic_count == bic_count
    assert probe.topology_valid is True

    # The BMS live block shares the probe base; identity fields must not disagree.
    telemetry = await read_live_block(transport)
    assert register_layout.detect_layout(telemetry[:7]) == probe

    # The served cell map matches the vendor formula for this topology.
    voltages = await transport.read_holding(0x5200, bic_count * 10)
    temperatures = await transport.read_holding(0x523C, bic_count * 3)
    cell_map = register_layout.assess_iot_cell_map(
        bic_count=bic_count, observed_cell_count=len(voltages)
    )
    assert cell_map.observed_count_matches_vendor_formula is True
    assert len(temperatures) == bic_count * 3


async def test_iot_plan_reads_decode_into_coherent_telemetry(simulator: Any) -> None:
    """T-SIM-POD-002 / V-IOT + PROTOCOL_EVIDENCE section 6 / S0."""
    _, transport, _ = build_unit(simulator)
    await transport.connect()
    data = await read_iot_plan(transport, bic_count=6)

    bms = data[0x5000]
    cells = data[0x5200]
    temperatures = data[0x523C]
    assert len(cells) == 60 and len(temperatures) == 18 and len(data[0x524E]) == 6

    # Literal intended engineering values (fresh idle unit): measured battery power
    # words are exactly 0 W on both evidence-backed views.
    assert bms[8] == 0  # IoT BMS +8: int16 W, idle
    assert 0 < bms[6] <= 15000  # pack voltage x0.1 V, so at most 1500.0 V
    assert 0 <= bms[9] <= 100  # SOC, raw percent
    assert 0 <= bms[10] <= 100  # SOH, raw percent
    assert protocol_codec.decode_signed16(bms[11]) >= 0  # charge current limit x0.1 A
    assert protocol_codec.decode_signed16(bms[12]) >= 0  # discharge current limit x0.1 A
    assert protocol_codec.decode_signed16(bms[13]) >= 0  # charge power limit
    assert protocol_codec.decode_signed16(bms[14]) >= 0  # discharge power limit

    # Extrema words cohere with the cell blocks they summarize (raw mV, raw-40 C).
    assert bms[20] == max(cells)  # max-voltage cell
    assert bms[23] == min(cells)  # min-voltage cell
    assert bms[26] - 40 == max(temperatures)  # max-temperature cell
    assert bms[29] - 40 == min(temperatures)  # min-temperature cell
    assert min(cells) <= bms[27] <= max(cells)
    assert min(cells) <= bms[30] <= max(cells)
    assert min(temperatures) <= bms[21] - 40 <= max(temperatures)
    assert min(temperatures) <= bms[24] - 40 <= max(temperatures)

    # PROTOCOL_EVIDENCE section 6: IoT BMS energies (0x5000+15..+18) and the whole
    # IoT cumulative block (0x4101) are low-word-first uint32 x0.1 counts. The
    # counts are composed with literal shifts here, never through the production
    # decoder, so a simulator serving swapped words cannot pass its own codec.
    charge_counts = (bms[16] << 16) | bms[15]
    discharge_counts = (bms[18] << 16) | bms[17]
    totals = data[0x4101]
    total_counts = [(totals[2 * index + 1] << 16) | totals[2 * index] for index in range(6)]
    for counts in (charge_counts, discharge_counts, *total_counts):
        assert 0 <= counts < 1_000_000, (
            f"energy words must be plausible low-word-first counts (0.1 kWh each), got {counts}"
        )
    # The BMS block and the cumulative block are two evidence-backed views of one
    # battery: BMS charge/discharge (totals words 4 and 5 of the 0x4101 order) must
    # serve exactly the same words as 0x5000+15..+18.
    assert (totals[8], totals[9]) == (bms[15], bms[16])
    assert (totals[10], totals[11]) == (bms[17], bms[18])

    # Two evidence-backed views of one battery must agree (both x0.1 V / int16 W).
    dcdc = data[0x2000]
    assert dcdc[3] == bms[6]  # battery voltage == pack voltage
    assert dcdc[9] == bms[8]  # battery power == measured battery power


@pytest.mark.parametrize(
    ("active_w", "reactive_var"),
    [(-3000, 750), (-1, 0), (1, -1), (2500, 32767)],
)
async def test_pq_write_latches_and_drives_measured_battery_power(
    simulator: Any, active_w: int, reactive_var: int
) -> None:
    """T-SIM-POD-003 / V-WRITE / S0: [1, P, Q] latches and drives measured watts."""
    pod, transport, _ = build_unit(simulator)
    await transport.connect()
    pod.poll()

    assert await measured_battery_watts(pod, transport) == 0
    assert await applied_objectives(transport) == (0, 0)

    frame = protocol_codec.encode_pq_registers(active_w, reactive_var)
    await transport.write_registers(0x0200, frame)
    assert_follows_direction(await measured_battery_watts(pod, transport), active_w)
    assert await applied_objectives(transport) == (active_w, reactive_var)

    # A new frame replaces the latched objective; objectives never accumulate.
    replacement_w = active_w // 2
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(replacement_w, 0))
    measured = await measured_battery_watts(pod, transport)
    if replacement_w == 0:
        assert measured == 0
    else:
        assert_follows_direction(measured, replacement_w)
    assert await applied_objectives(transport) == (replacement_w, 0)


async def test_stop_frame_returns_the_unit_to_idle_immediately(simulator: Any) -> None:
    """T-SIM-POD-004 / V-PQ-UI / S0: stop is the [1, 0, 0] frame."""
    pod, transport, _ = build_unit(simulator)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    assert_follows_direction(await measured_battery_watts(pod, transport), 2200)

    await transport.write_registers(0x0200, protocol_codec.encode_stop_registers())

    assert await measured_battery_watts(pod, transport) == 0
    # The stop frame itself latches [1, 0, 0], so its readback is pinned.
    assert await applied_objectives(transport) == (0, 0)


async def test_watchdog_expires_the_applied_setpoint_back_to_idle(simulator: Any) -> None:
    """T-SIM-POD-005 / UNC-LEASE (parameterized timing) / S0."""
    pod, transport, clock = build_unit(simulator, watchdog_timeout_s=2.0)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(1800, -40))

    clock.advance(1.999)  # strictly inside the lease
    assert_follows_direction(await measured_battery_watts(pod, transport), 1800)
    assert await applied_objectives(transport) == (1800, -40)

    clock.advance(0.002)  # strictly past the lease: the device is back to idle
    assert await measured_battery_watts(pod, transport) == 0

    # Idle is controllable again: a fresh frame is accepted and drives measured
    # power per its direction. Post-expiry objective-readback content is not pinned.
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-700, 0))
    assert_follows_direction(await measured_battery_watts(pod, transport), -700)


async def test_each_accepted_write_renews_the_watchdog_lease(simulator: Any) -> None:
    """T-SIM-POD-006 / V-PQ-UI keep-send / S0."""
    pod, transport, clock = build_unit(simulator, watchdog_timeout_s=2.0)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-900, 0))

    clock.advance(1.5)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-900, 0))

    clock.advance(1.0)  # past the first write's deadline, inside the renewed lease
    assert_follows_direction(await measured_battery_watts(pod, transport), -900)

    clock.advance(1.5)  # past the renewed deadline
    assert await measured_battery_watts(pod, transport) == 0


@pytest.mark.parametrize(
    ("address", "values"),
    [
        (0x0201, (1, 0, 0)),  # prior active-only write; not the vendor transaction
        (0x01FF, (1, 0, 0)),
        (0x5000, (1, 0, 0)),  # read-only telemetry area
        (0x8000, (0,)),  # debug/maintenance mode
        (0x8001, (0xFF00,)),  # clear historical energy
        (0x8002, (0, 0)),  # RS485 parameters
        (0x8037, (0xFF00,)),  # clear protection latch
        (0x0200, (2, 0, 0)),  # header word must be 1
        (0x0200, (0, 0, 0)),
        (0x0200, (1, 0)),  # short frame
        (0x0200, (1, 0, 0, 0)),  # long frame
        (0x0200, ()),  # empty frame
        (0x0200, (1, -1, 0)),
        (0x0200, (1, 0x10000, 0)),
        (0x0200, (1, True, 0)),
        (0x0200, (1, 1.5, 0)),
        (0x0200, "100"),  # not a register sequence
        (-1, (1, 0, 0)),
        (0x10000, (1, 0, 0)),
    ],
)
async def test_write_gate_accepts_only_the_full_pq_frame(
    simulator: Any, address: int, values: Any
) -> None:
    """T-SIM-POD-007 / V-WRITE + INV-EVIDENCE / S0: every other write is refused."""
    pod, transport, _ = build_unit(simulator)
    await transport.connect()

    with pytest.raises(ValueError):
        await transport.write_registers(address, values)

    # A rejected frame neither latches an objective nor disturbs idle telemetry.
    assert await applied_objectives(transport) == (0, 0)
    assert await measured_battery_watts(pod, transport) == 0


@pytest.mark.parametrize(
    ("address", "values"),
    [
        (0x0201, (1, 0, 0)),  # prior active-only write; not the vendor transaction
        (0x8000, (0,)),  # debug/maintenance mode
        (0x0200, (2, 0, 0)),  # header word must be 1
        (0x0200, (1, 0)),  # short frame
        (0x0200, (1, True, 0)),  # non-integer payload
    ],
)
async def test_refused_writes_never_disturb_a_latched_objective(
    simulator: Any, address: int, values: Any
) -> None:
    """T-SIM-POD-013 / V-WRITE + INV-EVIDENCE / S0.

    A garbage write during an active dispatch must not alter, clear, or half-latch
    the objective in force; rejection leaves the unit exactly as it was.
    """
    pod, transport, _ = build_unit(simulator)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    assert_follows_direction(await measured_battery_watts(pod, transport), 2200)

    with pytest.raises(ValueError):
        await transport.write_registers(address, values)

    # Raw literal check: 0x1060+17/+18 still serve the latched [2200 W, 0 var]
    # objectives exactly as two's-complement words.
    registers = await transport.read_holding(0x1060, 32)
    assert (registers[17], registers[18]) == (2200 & 0xFFFF, 0 & 0xFFFF)
    assert_follows_direction(await measured_battery_watts(pod, transport), 2200)


@pytest.mark.parametrize(
    "frame",
    [
        (1, 32767, 70000),  # second payload word out of range: must not latch 32767 W
        (1, 70000, 0),  # first payload word out of range
        (1, -200, 5),  # negative payload word
        (1, True, 0),  # bool is not a register word
        (True, 0, 0),  # the header must be the integer 1, not a bool that equals it
        (2, 0, 0),  # wrong header word
        (1, 100),  # short frame
        (1, 100, 100, 100),  # long frame
        (),  # empty frame
    ],
)
async def test_pod_refuses_malformed_frames_without_half_latching(
    simulator: Any, frame: tuple[int, ...]
) -> None:
    """T-SIM-POD-017 / V-WRITE + INV-EVIDENCE / S0: the device gate is all-or-nothing.

    The pod is exposed directly as the scenario handle (bypassing the transport
    gate), so the device model must refuse a malformed ``[1, P, Q]`` frame on
    its own: every frame word is validated before any state changes, so a frame
    that fails on a later word leaves the latched objective, the watchdog
    lease, and the whole served register bank bit-for-bit unchanged.
    """
    pod, transport, clock = build_unit(simulator, watchdog_timeout_s=2.0)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    pod.poll()
    assert await applied_objectives(transport) == (2200, 0)

    clock.advance(0.5)  # a lease renewed now would land at a distinguishable deadline
    before_state = device_state(pod)
    before_image = await read_iot_plan(transport, bic_count=6)

    with pytest.raises(ValueError):
        pod.apply_pq_frame(frame)

    assert device_state(pod) == before_state, "a refused frame must not touch any device state"
    assert await read_iot_plan(transport, bic_count=6) == before_image
    assert await applied_objectives(transport) == (2200, 0)

    # The refused frame renews nothing: the unit still expires at the original
    # 2.0 s lease deadline (a renewal at the refused-frame instant would hold
    # the objective for another 2.0 s from t+0.5).
    clock.advance(1.6)  # 2.1 s after the latch, so past the original deadline
    assert await measured_battery_watts(pod, transport) == 0


@pytest.mark.parametrize(
    ("address", "count"),
    [
        (0x5000, 0),
        (0x5000, -3),
        (-1, 1),
        (0x10000, 1),
        (0xFFFF, 2),  # window runs past the address space
        (0x5000, 126),  # beyond the FC03 protocol limit
        (0x0900, 4),  # unmapped window
        (0x1020, 4),  # gap between mapped IoT blocks
    ],
)
async def test_reads_enforce_device_like_boundaries(
    simulator: Any, address: int, count: int
) -> None:
    """T-SIM-TRANSPORT-001 / INV-DECODE / S1."""
    _, transport, _ = build_unit(simulator)
    await transport.connect()

    with pytest.raises(ValueError):
        await transport.read_holding(address, count)


@pytest.mark.parametrize("count", [0, -3])
async def test_pod_rejects_non_positive_read_windows_like_a_device(
    simulator: Any, count: int
) -> None:
    """T-SIM-POD-018 / INV-DECODE / S1.

    The pod is exposed directly as the scenario handle (bypassing the transport
    gate), so the device model itself must refuse a non-positive register
    count like a device refuses an FC03 quantity of 0: never an empty tuple,
    never a silently truncated window.
    """
    pod, _, _ = build_unit(simulator)

    with pytest.raises(ValueError):
        pod.read(0x5000, count)


async def test_connection_boundaries_are_device_like_and_idempotent(simulator: Any) -> None:
    """T-SIM-TRANSPORT-002 / S1."""
    _, transport, _ = build_unit(simulator)

    with pytest.raises(ConnectionError):
        await transport.read_holding(0x5000, 7)
    with pytest.raises(ConnectionError):
        await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(100, 0))

    await transport.connect()
    await transport.connect()  # idempotent
    await transport.read_holding(0x5000, 7)

    await transport.close()
    await transport.close()  # idempotent

    with pytest.raises(ConnectionError):
        await transport.read_holding(0x5000, 7)
    with pytest.raises(ConnectionError):
        await transport.write_registers(0x0200, protocol_codec.encode_stop_registers())
    with pytest.raises(ConnectionError):
        await transport.connect()


async def test_telemetry_sequence_advances_per_explicit_poll(simulator: Any) -> None:
    """T-SIM-POD-008 / ADR-0003 D4 / S0: one `poll()` step advances one sample."""
    pod, transport, clock = build_unit(simulator)
    await transport.connect()
    await transport.read_holding(0x5000, 7)  # layout probe establishes the baseline
    baseline = pod.telemetry_sequence

    pod.poll()
    assert pod.telemetry_sequence == baseline + 1
    assert pod.telemetry_captured_at_mono == clock.monotonic()

    clock.advance(0.4)
    pod.poll()
    assert pod.telemetry_sequence == baseline + 2
    assert pod.telemetry_captured_at_mono == clock.monotonic()


async def test_cell_data_refreshes_on_its_own_slower_cadence(simulator: Any) -> None:
    """T-SIM-POD-009 / ADR-0003 D4 / S0: unchanged cells, fresh telemetry, growing age."""
    pod, transport, clock = build_unit(simulator, cell_poll_interval_s=5.0)
    await transport.connect()
    pod.poll()
    first_cells = await transport.read_holding(0x5200, 60)
    first_temperatures = await transport.read_holding(0x523C, 18)
    first_sequence = pod.cell_sequence
    first_capture = pod.cell_captured_at_mono

    clock.advance(4.9)  # inside the cell cadence
    pod.poll()
    assert pod.cell_sequence == first_sequence
    assert pod.cell_captured_at_mono == first_capture
    assert await transport.read_holding(0x5200, 60) == first_cells
    assert await transport.read_holding(0x523C, 18) == first_temperatures

    clock.advance(0.2)  # 5.1 s since capture: past the cadence
    pod.poll()
    assert pod.cell_sequence == first_sequence + 1
    assert pod.cell_captured_at_mono == clock.monotonic()
    assert pod.cell_captured_at_mono <= pod.telemetry_captured_at_mono


async def run_deterministic_scenario(simulator: Any) -> dict[str, Any]:
    """One fixed script; every number is a plain comparable value."""
    pod, transport, clock = build_unit(simulator, clock=FakeMonotonicClock(1000.0))
    captured: dict[str, Any] = {"identity": pod.identity}
    await transport.connect()
    captured["probe"] = await transport.read_holding(0x5000, 7)
    pod.poll()
    captured["telemetry_before_write"] = await read_live_block(transport)
    frame = protocol_codec.encode_pq_registers(-1500, 320)
    await transport.write_registers(0x0200, frame)
    clock.advance(0.25)
    pod.poll()
    captured["telemetry_latched"] = await read_live_block(transport)
    captured["objectives_latched"] = await transport.read_holding(0x1060, 32)
    clock.advance(5.0)  # the first lease expires; the cell cadence elapses
    await transport.write_registers(0x0200, frame)
    pod.poll()
    captured["telemetry_renewed"] = await read_live_block(transport)
    captured["cells_renewed"] = await transport.read_holding(0x5200, 60)
    pod.inject_fault("Stack_Fault0", 3)
    pod.inject_fault("Stack_Warning0", 11)
    captured["bms_status"] = await transport.read_holding(0x5040, 22)
    clock.advance(2.5)  # past the renewed lease
    pod.poll()
    captured["telemetry_expired"] = await read_live_block(transport)
    captured["objectives_expired"] = await transport.read_holding(0x1060, 32)
    captured["sequences"] = (
        pod.telemetry_sequence,
        pod.cell_sequence,
        pod.telemetry_captured_at_mono,
        pod.cell_captured_at_mono,
    )
    await transport.close()
    return captured


async def test_identical_scenario_scripts_produce_identical_results(simulator: Any) -> None:
    """T-SIM-POD-010 / ADR-0003 D4 determinism / S0."""
    first = await run_deterministic_scenario(simulator)
    second = await run_deterministic_scenario(simulator)

    assert first == second
    # The equality is meaningful: while latched the measured power follows the
    # commanded direction, and after the lease expires the device measures idle.
    assert protocol_codec.decode_signed16(first["telemetry_latched"][8]) < 0
    assert protocol_codec.decode_signed16(first["telemetry_renewed"][8]) < 0
    assert protocol_codec.decode_signed16(first["telemetry_expired"][8]) == 0


async def test_fault_and_warning_injection_surfaces_in_decodable_status_words(
    simulator: Any,
) -> None:
    """T-SIM-POD-011 / V-FAULTS / S0."""
    pod, transport, _ = build_unit(simulator)
    await transport.connect()

    # A clean simulated unit boots with quiet status words.
    for block, address in (
        (faults.FaultBlock.IOT_PCS, 0x1040),
        (faults.FaultBlock.IOT_DCDC, 0x2040),
        (faults.FaultBlock.IOT_BMS, 0x5040),
    ):
        words = faults.extract_fault_block(block, await transport.read_holding(address, 22))
        assert set(words.values()) == {0}

    pod.inject_fault("Stack_Fault0", 3)  # PCS CAN disconnected
    pod.inject_fault("Stack_Warning0", 11)  # No Remote Dispatch
    pod.inject_fault("PCS_Fault1", 2)  # abnormal ground voltage
    pod.inject_fault("DCDC_Warning0", 1)  # EEPROM calibration out of range

    bms_words = faults.extract_fault_block(
        faults.FaultBlock.IOT_BMS, await transport.read_holding(0x5040, 22)
    )
    assert bms_words["Stack_Fault0"] == 1 << 3
    assert bms_words["Stack_Warning0"] == 1 << 11
    assert bms_words["Stack_Fault1"] == 0
    signals = faults.decode_fault_word("Stack_Fault0", bms_words["Stack_Fault0"])
    assert [signal.code for signal in signals] == ["Stack_Fault0_3"]
    assert signals[0].description == "PCS CAN disconnected"

    pcs_words = faults.extract_fault_block(
        faults.FaultBlock.IOT_PCS, await transport.read_holding(0x1040, 22)
    )
    assert pcs_words["PCS_Fault1"] == 1 << 2

    dcdc_words = faults.extract_fault_block(
        faults.FaultBlock.IOT_DCDC, await transport.read_holding(0x2040, 22)
    )
    assert dcdc_words["DCDC_Warning0"] == 1 << 1

    pod.clear_fault("Stack_Fault0", 3)
    refreshed = faults.extract_fault_block(
        faults.FaultBlock.IOT_BMS, await transport.read_holding(0x5040, 22)
    )
    assert refreshed["Stack_Fault0"] == 0
    assert refreshed["Stack_Warning0"] == 1 << 11  # other injections are untouched


async def test_identity_is_stable_across_polls_and_time(simulator: Any) -> None:
    """T-SIM-POD-012 / V-LAYOUT RTU ID / S1.

    Identity is pinned only through the evidenced registers: PROTOCOL_EVIDENCE
    section 5 lists the common `0x8106` two-register RTU-ID read (Confirmed by
    vendor code, V-LAYOUT); no unevidenced identity mapping is invented here.
    """
    pod, transport, clock = build_unit(simulator, identity="SIM-BEP-0042")
    assert pod.identity == "SIM-BEP-0042"
    await transport.connect()

    first = await transport.read_holding(0x8106, 2)
    assert len(first) == 2
    assert all(type(word) is int and 0 <= word <= 0xFFFF for word in first)
    clock.advance(9.7)
    pod.poll()
    assert await transport.read_holding(0x8106, 2) == first


async def test_link_drop_fails_reads_and_writes_until_restore(simulator: Any) -> None:
    """T-SIM-POD-014 / API_CONTRACTS 'Deterministic simulator' link drop/restore / S0."""
    pod, transport, _ = build_unit(simulator)
    await transport.connect()
    clean_probe = register_layout.detect_layout(await transport.read_holding(0x5000, 7))

    pod.drop_link()
    with pytest.raises(ConnectionError):
        await transport.read_holding(0x5000, 7)
    with pytest.raises(ConnectionError):
        await transport.write_registers(0x0200, protocol_codec.encode_stop_registers())

    pod.restore_link()
    restored = await transport.read_holding(0x5000, 7)
    assert register_layout.detect_layout(restored) == clean_probe


async def test_malformed_register_injection_corrupts_only_the_targeted_word(
    simulator: Any,
) -> None:
    """T-SIM-POD-015 / API_CONTRACTS malformed-register injection / S0.

    The hook targets one register address: the served word at that address must
    differ from the clean image while every neighbor in the same read is intact.
    """
    pod, transport, _ = build_unit(simulator)
    await transport.connect()
    clean = await transport.read_holding(0x5000, 31)

    pod.inject_malformed_register(0x5008)  # IoT BMS +8: measured battery power
    corrupted = await transport.read_holding(0x5000, 31)

    assert corrupted[8] != clean[8]
    assert corrupted[:8] == clean[:8]
    assert corrupted[9:] == clean[9:]


async def test_telemetry_follows_the_seeded_schedule_across_scripted_time(
    simulator: Any,
) -> None:
    """T-SIM-POD-016 / ADR-0003 D4 seeded schedule / S0.

    Measured telemetry is a function of the applied setpoint AND scripted time:
    the served register image must move as scripted time advances (a static bank
    with power passthrough is not a seeded schedule), and identically so for a
    same-seed twin running the same script.
    """
    pod, transport, clock = build_unit(simulator)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    pod.poll()
    before = await read_iot_plan(transport, bic_count=6)

    clock.advance(30.0)  # six cell-cadence intervals elapse under load
    pod.poll()
    after = await read_iot_plan(transport, bic_count=6)

    assert after != before, "scripted time must move derived telemetry, not just power"

    twin_pod, twin_transport, twin_clock = build_unit(simulator)
    await twin_transport.connect()
    await twin_transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    twin_pod.poll()
    twin_before = await read_iot_plan(twin_transport, bic_count=6)
    twin_clock.advance(30.0)
    twin_pod.poll()
    twin_after = await read_iot_plan(twin_transport, bic_count=6)

    assert (twin_before, twin_after) == (before, after)


async def test_no_socket_is_ever_opened(simulator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-SIM-TRANSPORT-003 / ADR-0003 D4 / S0: on-wire framing stays a commissioning item."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the simulator must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    pod, transport, clock = build_unit(simulator)
    await transport.connect()
    pod.poll()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(400, 0))
    clock.advance(0.1)
    assert_follows_direction(await measured_battery_watts(pod, transport), 400)
    await transport.close()


# --- energy scorecard (E6): the totals block accumulates from the scripted CT --
#
# DESIGN_ENERGY_SCORECARD section 9 family 8: the simulator's grid and load
# counter pairs ACCUMULATE from the scripted CT words exactly the way the
# charge/discharge pair accumulates from the applied objective
# (`_accumulate` precedent, DEFERRED_FINDINGS item 3's reference model) --
# deterministic, seeded statics as the base, watt-seconds over the injected
# clock, low-word-first uint32 x 0.1 served words.

_TOTALS_BASE = 0x4101


def _totals_counts(registers: tuple[int, ...]) -> tuple[int, ...]:
    """The six uint32 counts of the served totals block, low word first."""
    return tuple(
        (int(registers[pair * 2 + 1]) << 16) | int(registers[pair * 2]) for pair in range(6)
    )


async def test_scripted_grid_ct_accumulates_into_the_grid_counter_pairs(
    simulator: Any,
) -> None:
    """36 s at -10,000 W is exactly 0.1 kWh: one counter quantum per interval,
    buy pair for imports (negative grid), sell pair for exports."""
    pod, _transport, clock = build_unit(simulator)
    pod.script_grid_power_w(-10_000)
    before = _totals_counts(pod.read(_TOTALS_BASE, 12))

    for _ in range(10):
        clock.advance(36.0)
        pod.poll()
    after = _totals_counts(pod.read(_TOTALS_BASE, 12))

    deltas = [later - earlier for earlier, later in zip(before, after, strict=True)]
    assert deltas[0] == 10, "pair 0 (vendor grid pair A) accrued 10 x 0.1 kWh"
    assert deltas[1] == 0, "the export pair does not move while importing"
    assert deltas[4] == 0 and deltas[5] == 0, "no battery objective was applied"

    pod.script_grid_power_w(10_000)
    export_before = _totals_counts(pod.read(_TOTALS_BASE, 12))
    clock.advance(36.0)
    pod.poll()
    export_after = _totals_counts(pod.read(_TOTALS_BASE, 12))
    export_deltas = [
        later - earlier for earlier, later in zip(export_before, export_after, strict=True)
    ]
    assert export_deltas[0] == 0
    assert export_deltas[1] == 1, "pair 1 (vendor grid pair B) accrued the export"


async def test_scripted_load_ct_accumulates_into_the_load_counter(simulator: Any) -> None:
    pod, _transport, clock = build_unit(simulator)
    pod.script_load_power_w(5_000)
    before = _totals_counts(pod.read(_TOTALS_BASE, 12))

    clock.advance(72.0)  # 5000 W x 72 s = 360000 Ws = exactly 0.1 kWh
    pod.poll()
    after = _totals_counts(pod.read(_TOTALS_BASE, 12))

    deltas = [later - earlier for earlier, later in zip(before, after, strict=True)]
    assert deltas[2] == 1, "the load counter accrued exactly one 0.1 kWh quantum"
    assert deltas[0] == 0 and deltas[1] == 0


async def test_idle_ct_words_never_accumulate(simulator: Any) -> None:
    """The default scripted CT words are 0 W: the seeded statics stand still,
    so the pinned register images of a quiet pod are unchanged."""
    pod, _transport, clock = build_unit(simulator)
    before = _totals_counts(pod.read(_TOTALS_BASE, 12))
    for _ in range(5):
        clock.advance(36.0)
        pod.poll()
    after = _totals_counts(pod.read(_TOTALS_BASE, 12))
    assert before == after


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"identity": ""}, "identity"),
        ({"identity": "  "}, "identity"),
        ({"identity": 42}, "identity"),
        ({"bic_count": 0}, "bic_count"),
        ({"bic_count": 7}, "bic_count"),
        ({"bic_count": True}, "bic_count"),
        ({"seed": "7"}, "seed"),
        ({"seed": 1.5}, "seed"),
        ({"watchdog_timeout_s": 0}, "watchdog"),
        ({"watchdog_timeout_s": -1.0}, "watchdog"),
        ({"watchdog_timeout_s": float("nan")}, "watchdog"),
        ({"watchdog_timeout_s": True}, "watchdog"),
        ({"cell_poll_interval_s": 0}, "cell_poll"),
        ({"cell_poll_interval_s": float("inf")}, "cell_poll"),
        ({"cell_poll_interval_s": "5"}, "cell_poll"),
    ],
)
def test_constructor_validates_every_commissioning_shape(
    simulator: Any, kwargs: dict[str, Any], fragment: str
) -> None:
    """DEFERRED_FINDINGS item 4's open half: the constructor-validation matrix.
    Every malformed shape is refused at construction -- a pod built from it
    could never present a coherent register bank."""
    clock = FakeMonotonicClock()
    with pytest.raises((TypeError, ValueError), match=fragment):
        simulator.SimulatedEnergyPod(clock=clock, **kwargs)


# --- night-writer detector scenario hook: a foreign served objective --------------


async def test_script_objective_serves_a_foreign_pq_objective(simulator: Any) -> None:
    """API_CONTRACTS "Night-writer detector": the scenario hook places ANOTHER
    writer's objective on the served wire -- the detail block's +17/+18 words
    read back exactly the scripted pair while the pod's own applied state
    stays untouched (the foreign writer's words, not ours)."""
    pod, transport, clock = build_unit(simulator)
    await transport.connect()

    pod.script_objective(-2400, 0)
    pod.poll()

    assert await applied_objectives(transport) == (-2400, 0)
    # The pod's own applied objective is untouched: this is a FOREIGN word.
    assert pod._applied_active_w == 0 and pod._lease_deadline_mono is None


async def test_a_scripted_objective_puts_the_pcs_into_remote_pq_mode(simulator: Any) -> None:
    """The discriminator the pattern tier consumes: a written objective puts
    the PCS into run mode 1 ("Remote PQ Power" -- the vendor's written-objective
    state); the pod's own CT-following autonomy reads 0 ("Matching Load")."""
    pod, transport, clock = build_unit(simulator)
    await transport.connect()

    pod.poll()
    idle = await transport.read_holding(0x1000, 21)
    assert idle[2] == 0, "an autonomous pod matches load"

    pod.script_objective(-2400, 0)
    pod.poll()
    written = await transport.read_holding(0x1000, 21)
    assert written[2] == 1, "a scripted (foreign) objective holds the remote-PQ mode"


async def test_clearing_the_scripted_objective_restores_the_served_words(simulator: Any) -> None:
    pod, transport, clock = build_unit(simulator)
    await transport.connect()

    pod.script_objective(-2400, 300)
    pod.clear_scripted_objective()
    pod.poll()

    assert await applied_objectives(transport) == (0, 0)


async def test_the_scenario_hook_validates_its_words_like_the_wire(simulator: Any) -> None:
    pod, transport, clock = build_unit(simulator)
    for bad in (32768, -32769, 1.5, "2400"):
        with pytest.raises((TypeError, ValueError), match="int16|integer"):
            pod.script_objective(bad, 0)  # type: ignore[arg-type]


async def test_the_scenario_hook_may_serve_the_matching_load_mode(simulator: Any) -> None:
    """The pod's own CT-following autonomy reads run mode 0 ("Matching
    Load") while it still holds a nonzero objective -- the state the 2026-08
    23 lhs evening hold was observed in.  The hook's ``remote_mode=False``
    serves exactly that, so a scenario can stage pod autonomy as distinct
    from a writer's Remote-PQ state."""
    pod, transport, clock = build_unit(simulator)
    await transport.connect()

    pod.script_objective(-620, 0, remote_mode=False)
    pod.poll()

    assert await applied_objectives(transport) == (-620, 0)
    live = await transport.read_holding(0x1000, 21)
    assert live[2] == 0, "an autonomous pod matches load"

    pod.script_objective(-620, 0)
    pod.poll()
    live = await transport.read_holding(0x1000, 21)
    assert live[2] == 1, "a scripted writer holds the remote-PQ mode by default"


# --- off-peak night charge (DESIGN_NIGHT_CHARGE B6): the scripted night ----------
#
# The full composed scenario over the SIMULATED fleet: the demand rule driven
# by the simulator's own `script_load_power_w` hook (the LOAD CT words the
# rule reads), pacing from the pods' real SOCs, one battery already full
# sitting out, the EV spike engaging the hold, the hysteresis band holding,
# demand falling releasing it, a manual request outranking per battery, the
# dawn-corner optimizer claim excluding its unit, completion before the
# window ends, the units_disarmed boot state, and window-end non-renewal.

_NIGHT_WINDOW_UTC_START = datetime(2026, 8, 21, 18, 30, tzinfo=UTC)  # 04:30 Brisbane
_NIGHT_WINDOW_UTC_END = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)  # 06:00 Brisbane


@dataclass
class _NightScriptedClock:
    """A deterministic clock starting inside the commissioned window."""

    elapsed_s: float = 0.0

    def wall_now(self) -> datetime:
        return _NIGHT_WINDOW_UTC_START + timedelta(seconds=self.elapsed_s)

    def monotonic(self) -> float:
        return self.elapsed_s

    async def sleep(self, seconds: float) -> None:
        self.elapsed_s += max(0.0, float(seconds))
        await asyncio.sleep(0)


def _night_operator() -> Any:
    from tests.unit.test_composition import OPERATOR

    return OPERATOR


def _compose_night_fleet(database: Any, clock: Any, *, standby_posture: bool = False) -> Any:
    """A three-unit simulate composition with the partition granted and the
    night block present-but-suspended (the operator enables it at runtime).
    ``standby_posture`` commissions the TRUE STANDBY posture instead —
    per_phase independence, the park_standby selector, and the parking lease
    budget sized past the window span +120 s."""
    from tests.unit.test_composition import (
        _authentication_payload,
        _compose_with,
        _policy_payload,
        _timing_payload,
        _unit_payload,
        _validate,
    )

    night_block: dict[str, Any] = {"timezone": "Australia/Brisbane"}
    if standby_posture:
        night_block["demand_scope"] = "per_phase"
        night_block["demand_response"] = "park_standby"
    payload = {
        "schema_version": 1,
        "revision": 7,
        "mode": "write_enabled",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 3,
        },
        "units": [
            _unit_payload("mid", "BEP-MID", "192.168.1.11"),
            _unit_payload("rhs", "BEP-RHS", "192.168.1.12"),
            _unit_payload("lhs", "BEP-LHS", "192.168.1.13"),
        ],
        "timing": _timing_payload(),
        # The simulator's SOC model clamps at 94.5%, so the scenario's
        # ceiling sits at 94.0: a battery the model can actually fill.
        "policy": {**_policy_payload(), "maximum_soc_pct": 94.0},
        "authentication": _authentication_payload(),
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
        "schedule": {"allowed_windows_local": [["00:00", "20:00"]]},
        "night_charging": night_block,
    }
    if standby_posture:
        payload["parking"] = {"max_lease_s": 25_200}
    return _compose_with(_validate(payload), simulate=True, clock=clock)


def _pod_full(pod: Any) -> None:
    """Place one simulated battery above the scenario's 94% ceiling (rhs
    tonight): the model's own clamp (94.5%) holds it there through polls."""
    pod._soc_pct = 96.0
    pod._rebuild()


def _script_load(runtime: Any, watts_by_unit: dict[str, int]) -> None:
    for unit_id, watts in watts_by_unit.items():
        runtime.simulators[unit_id].script_load_power_w(watts)


def _state(runtime: Any) -> dict[str, Any]:
    controller = runtime.night_controller
    assert controller is not None
    return controller.state_payload()


def _targets(state: dict[str, Any]) -> dict[str, int]:
    return {
        unit["unit_id"]: unit["target_w"]
        for unit in state["units"]
        if unit["phase"] in ("pacing", "holding_on_demand", "standing_by_on_demand")
    }


async def test_the_scripted_night_runs_the_full_strategy(tmp_path: Any) -> None:
    """The night trajectory, end to end over the simulated fleet: disarmed
    boot -> pacing from real SOCs with the full battery sitting out -> the EV
    hold -> the hysteresis band -> the resume -> a manual claim excluding one
    battery -> the dawn-corner optimizer claim -> completion -> window-end
    non-renewal."""
    from tests.unit.test_composition import _LifespanSession

    clock = _NightScriptedClock()
    runtime = _compose_night_fleet(tmp_path / "night-scenario.sqlite3", clock)
    operator = _night_operator()
    controller = runtime.night_controller
    assert controller is not None

    enabled = await runtime.facade.set_night_charging(
        action="enable",
        confirmation="NIGHT",
        night_posture="PARTITION_ACKNOWLEDGED",
        principal=operator,
        idempotency_key="night-scenario-enable",
        request_id="night-scenario-enable-request",
    )
    assert enabled["enabled"] is True
    assert enabled["acknowledged_partition"] is True

    # rhs sits above the ceiling from the first night: a zero-watt
    # non-participant, exactly like the commissioning fleet.
    _pod_full(runtime.simulators["rhs"])
    _script_load(runtime, {"mid": 100, "rhs": 100, "lhs": 100})

    session = _LifespanSession(runtime.app)
    session.send("lifespan.startup")
    try:
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete"),
            message="the lifespan never reported startup",
        )

        # The boot state: the runner can never self-arm, and the projection
        # says so honestly (the S3 lesson, designed in from day one).
        await session.pump_until(
            lambda: "units_disarmed" in _state(runtime)["reason_codes"],
            message="the disarmed boot state never surfaced",
        )
        disarmed = _state(runtime)
        assert disarmed["phase"] == "idle"
        assert _targets(disarmed) == {}

        # The operator's standing ritual: arm the fleet once (the actor's
        # own stable-sample qualification must land first).
        await session.pump_until(
            lambda: all(getattr(actor, "qualified", False) for actor in runtime.actors.values()),
            message="the simulated fleet never qualified",
            attempts=12000,
        )
        await runtime.facade.arm(
            unit_ids=["mid", "rhs", "lhs"],
            principal=operator,
            idempotency_key="night-scenario-arm",
            request_id="night-scenario-arm-request",
        )
        await session.pump_until(
            lambda: _state(runtime)["phase"] == "pacing",
            message="the fleet never started pacing after arming",
            attempts=12000,
        )
        pacing = _state(runtime)
        assert pacing["active"] is True
        assert (pacing["held_intent_id"] or "").startswith("night-")
        assert pacing["demand_w"] == 300
        assert pacing["demand_evidence"] == "good"
        by_unit = {unit["unit_id"]: unit for unit in pacing["units"]}
        assert by_unit["rhs"]["phase"] == "skipped_full"
        assert by_unit["rhs"]["target_w"] == 0
        assert by_unit["rhs"]["reason"] == "at_ceiling"
        assert _targets(pacing) == {"mid": 2_500, "lhs": 2_500}, "cap_first Docker parity"

        # The EV arrives: 500 + 500 + 300 = 1300 W of house load, and every
        # participating battery STANDS DOWN INTO ITS HOLD — the operator's
        # directive: the grid serves the heavy load while the hold pins each
        # battery at hold_rate_w (zero discharge, autonomy overridden).
        _script_load(runtime, {"mid": 500, "rhs": 300, "lhs": 500})
        await session.pump_until(
            lambda: _state(runtime)["phase"] == "standing_by_on_demand",
            message="the demand stand-down never engaged",
        )
        stood_down = _state(runtime)
        assert stood_down["demand_w"] == 1_300
        assert "demand_above_threshold" in stood_down["reason_codes"]
        assert _targets(stood_down) == {"mid": 100, "lhs": 100}, (
            "stood down at the hold rate: zero discharge, not zero charge"
        )

        # Into the hysteresis band (900 W: above the 800 W exit bound): the
        # stand-down must NOT release.
        _script_load(runtime, {"mid": 400, "rhs": 200, "lhs": 300})
        for _ in range(24):
            await asyncio.sleep(0)
        band = _state(runtime)
        assert band["phase"] == "standing_by_on_demand", "the band never flaps"
        assert band["demand_w"] == 900
        assert _targets(band) == {"mid": 100, "lhs": 100}, "the band keeps the holds"

        # Demand falls below the exit bound: pacing resumes.
        _script_load(runtime, {"mid": 300, "rhs": 100, "lhs": 300})
        await session.pump_until(
            lambda: _state(runtime)["phase"] == "pacing",
            message="pacing never resumed below the exit bound",
        )
        resumed = _state(runtime)
        assert _targets(resumed) == {"mid": 2_500, "lhs": 2_500}

        # A manual request outranks per battery: mid is excluded from the
        # submission while lhs keeps charging.
        manual = await runtime.facade.submit_intent(
            unit_ids=["mid"],
            direction="charge",
            watts=700,
            ttl_s=30.0,
            principal=operator,
            idempotency_key="night-scenario-manual",
            request_id="night-scenario-manual-request",
        )
        await session.pump_until(
            lambda: _targets(_state(runtime)) == {"lhs": 2_500},
            message="the manual claim never excluded its battery",
        )
        yielding_state = _state(runtime)
        by_unit = {unit["unit_id"]: unit for unit in yielding_state["units"]}
        assert by_unit["mid"]["phase"] == "sitting_out"
        assert by_unit["mid"]["reason"] == "yielding_to_higher_priority"
        await runtime.facade.cancel_intent(
            intent_id=manual["intent_id"],
            principal=operator,
            idempotency_key="night-scenario-cancel",
            request_id="night-scenario-cancel-request",
        )
        await session.pump_until(
            lambda: _targets(_state(runtime)) == {"mid": 2_500, "lhs": 2_500},
            message="the released battery never rejoined",
        )

        # The dawn corner: a live not-own OPTIMIZER intent (the excess
        # adviser's class, identified by claim at tick time) excludes its
        # unit too -- free surplus outranks paid import.
        from energypod.domain import Direction, IntentSource, PowerIntent

        await runtime.intents.add(
            PowerIntent(
                id="opt-dawn-1",
                source=IntentSource.OPTIMIZER,
                selected_unit_ids=frozenset({"mid"}),
                direction=Direction.CHARGE,
                watts=1_800,
                duration_s=60.0,
                accepted_at_mono=clock.monotonic(),
                acceptance_revision=999,
                actor_identity="energypod:excess-adviser",
            )
        )
        await session.pump_until(
            lambda: _targets(_state(runtime)) == {"lhs": 2_500},
            message="the dawn-corner claim never excluded its battery",
        )
        dawn = _state(runtime)
        by_unit = {unit["unit_id"]: unit for unit in dawn["units"]}
        assert by_unit["mid"]["reason"] == "yielding_to_higher_priority"
        await runtime.intents.remove("opt-dawn-1")

        # Completion before the window ends: both needy batteries reach the
        # ceiling and the projection says complete, nothing charging.
        _pod_full(runtime.simulators["mid"])
        _pod_full(runtime.simulators["lhs"])
        await session.pump_until(
            lambda: _state(runtime)["phase"] == "complete",
            message="the window never completed",
        )
        complete = _state(runtime)
        assert "target_reached" in complete["reason_codes"]
        assert complete["active"] is False
        assert _targets(complete) == {}

        # Window end is NON-RENEWAL: past 06:00 the projection idles with
        # outside_window and holds nothing.
        clock.elapsed_s += (_NIGHT_WINDOW_UTC_END - _NIGHT_WINDOW_UTC_START).total_seconds() + 60.0
        await session.pump_until(
            lambda: _state(runtime)["reason_codes"] == ["outside_window"],
            message="the window never ended by non-renewal",
        )
        ended = _state(runtime)
        assert ended["phase"] == "idle"
        assert ended["active"] is False
        assert ended["next_window_at"] is not None, "the next night is named"
    finally:
        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the lifespan never reported shutdown",
            attempts=20000,
        )


async def test_the_scripted_standby_parks_the_heavy_phase_releases_and_stands_down(
    tmp_path: Any,
) -> None:
    """The TRUE STANDBY directive end to end over the simulated fleet
    (operator directive 2026-08-26, "Only the heavy one"): ONE battery's own
    circuit crosses 1,000 W inside the window and THAT battery alone goes
    into TRUE STANDBY — an ACTIVE PARK under an automation-origin lease, its
    sibling pacing on untouched; below 800 W it resumes AND re-arms back to
    the full capped rate; at window close every exit path releases."""
    from energypod.domain import UnitLifecycle
    from tests.unit.test_composition import _LifespanSession

    clock = _NightScriptedClock()
    runtime = _compose_night_fleet(
        tmp_path / "standby-scenario.sqlite3", clock, standby_posture=True
    )
    operator = _night_operator()
    assert runtime.night_controller is not None

    enabled = await runtime.facade.set_night_charging(
        action="enable",
        confirmation="NIGHT",
        night_posture="PARTITION_ACKNOWLEDGED",
        principal=operator,
        idempotency_key="standby-scenario-enable",
        request_id="standby-scenario-enable-request",
    )
    assert enabled["enabled"] is True
    assert enabled["acknowledged_partition"] is True

    # lhs's OWN circuit carries tonight's heavy load; the others are quiet.
    _pod_full(runtime.simulators["rhs"])
    _script_load(runtime, {"mid": 100, "rhs": 100, "lhs": 1_500})

    session = _LifespanSession(runtime.app)
    session.send("lifespan.startup")
    try:
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete"),
            message="the lifespan never reported startup",
        )
        await session.pump_until(
            lambda: all(getattr(actor, "qualified", False) for actor in runtime.actors.values()),
            message="the simulated fleet never qualified",
            attempts=12000,
        )
        await runtime.facade.arm(
            unit_ids=["mid", "rhs", "lhs"],
            principal=operator,
            idempotency_key="standby-scenario-arm",
            request_id="standby-scenario-arm-request",
        )

        # THE DIRECTIVE'S CORE: only the heavy battery parks.
        await session.pump_until(
            lambda: any(unit["phase"] == "standing_by_parked" for unit in _state(runtime)["units"]),
            message="the heavy phase never went to TRUE STANDBY",
            attempts=12000,
        )
        parked = _state(runtime)
        by_unit = {unit["unit_id"]: unit for unit in parked["units"]}
        assert by_unit["lhs"]["phase"] == "standing_by_parked"
        assert by_unit["lhs"]["target_w"] == 0
        assert by_unit["mid"]["phase"] == "pacing", "the clean phase is untouched"
        assert _targets(parked) == {"mid": 2_500}, (
            "only the heavy battery left the submission — never passive exclusion of the rest"
        )

        # And it REALLY parked: the vendor Standby register under a lease
        # whose origin reads automation (the adviser's composed principal).
        states = await runtime.parking.park_states()
        assert states["lhs"]["parked"] is True
        assert states["lhs"]["origin"] == "automation"
        assert states["lhs"]["reason"] == "night_demand_standby"

        # Below 800 W: resumed AND re-armed, back at the full capped rate —
        # release is a choreography, not an omission.
        _script_load(runtime, {"lhs": 600})
        await session.pump_until(
            lambda: _targets(_state(runtime)) == {"mid": 2_500, "lhs": 2_500},
            message="the released battery never returned to full rate",
            attempts=12000,
        )
        assert runtime.actors["lhs"].lifecycle is UnitLifecycle.ARMED_IDLE, (
            "release means RE-ARMED, not merely resumed"
        )
        states = await runtime.parking.park_states()
        assert states.get("lhs", {}).get("parked") is False

        # Window end: EVERY exit path releases — nothing stays parked past
        # the window on an alarm-only expiry doctrine.
        clock.elapsed_s += (_NIGHT_WINDOW_UTC_END - _NIGHT_WINDOW_UTC_START).total_seconds() + 60.0
        await session.pump_until(
            lambda: _state(runtime)["reason_codes"] == ["outside_window"],
            message="the window never ended by non-renewal",
            attempts=12000,
        )
        await session.pump_until(
            lambda: runtime.parking.parked_unit_ids() == frozenset(),
            message="window end left a pod parked",
            attempts=12000,
        )
        assert runtime.actors["lhs"].lifecycle is UnitLifecycle.ARMED_IDLE
    finally:
        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the lifespan never reported shutdown",
            attempts=20000,
        )
