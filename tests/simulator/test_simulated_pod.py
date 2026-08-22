"""Deterministic simulator device and transport contracts (ADR-0003 decision D4).

The simulated unit is pinned through the actor transport port against the production
register-layout, protocol-codec, and fault decoders.  Production simulator modules do
not exist yet: they are loaded lazily so this red-phase suite still collects, and every
pinned behavior is an ordinary test failure until the contract is implemented.

Pinned device-model surface: `SimulatedEnergyPod(clock=..., identity=..., bic_count=...,
seed=..., watchdog_timeout_s=..., cell_poll_interval_s=...)` driven exclusively by the
injected monotonic clock, with `telemetry_sequence`, `telemetry_captured_at_mono`,
`cell_sequence`, and `cell_captured_at_mono` observation metadata and
`inject_fault(prefix, bit)` / `clear_fault(prefix, bit)` scenario hooks.  One FC03 read
whose window includes the IoT BMS telemetry anchor register 0x5000 is one device poll.
"""

from __future__ import annotations

import importlib
import socket
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


async def telemetry_poll(transport: Any) -> tuple[int, ...]:
    """One anchor read of the IoT BMS live block at 0x5000: this is one device poll."""
    return await transport.read_holding(0x5000, 31)


async def measured_battery_watts(transport: Any) -> int:
    registers = await telemetry_poll(transport)
    return protocol_codec.decode_signed16(registers[8])  # IoT BMS +8: int16 W


async def applied_objectives(transport: Any) -> tuple[int, int]:
    registers = await transport.read_holding(0x1060, 32)
    return (
        protocol_codec.decode_signed16(registers[17]),  # PCS detail +17: active W
        protocol_codec.decode_signed16(registers[18]),  # PCS detail +18: reactive var
    )


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

    assert probe_registers[0] > 10
    assert probe.layout is register_layout.ProtocolLayout.IOT
    assert probe.bic_count == bic_count
    assert probe.topology_valid is True
    assert probe.enable_mask == probe_registers[4] & 0xFF
    assert probe.becu_count >= 1

    # The BMS live block shares the probe base; identity fields must not disagree.
    telemetry = await telemetry_poll(transport)
    assert register_layout.detect_layout(telemetry[:7]) == probe

    # The served cell map is exactly the vendor-formula map for this topology.
    voltages = await transport.read_holding(0x5200, bic_count * 10)
    temperatures = await transport.read_holding(0x523C, bic_count * 3)
    cell_map = register_layout.assess_iot_cell_map(
        bic_count=bic_count, observed_cell_count=len(voltages)
    )
    assert cell_map.observed_count_matches_vendor_formula is True
    assert cell_map.map_non_overlapping is True
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

    assert protocol_codec.decode_signed16(bms[8]) == 0  # idle battery power
    assert 0 < protocol_codec.decode_signed16(bms[6]) * 0.1 <= 1500  # pack voltage x0.1
    assert 0 <= protocol_codec.decode_signed16(bms[9]) <= 100  # SOC
    assert 0 <= protocol_codec.decode_signed16(bms[10]) <= 100  # SOH
    assert protocol_codec.decode_signed16(bms[11]) * 0.1 >= 0  # charge current limit
    assert protocol_codec.decode_signed16(bms[12]) * 0.1 >= 0  # discharge current limit
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

    # Low-word-first IoT energies decode through the production codec.
    charge_energy = protocol_codec.decode_uint32(
        protocol_codec.UInt32Field.IOT_BMS_ENERGY, (bms[15], bms[16])
    )
    discharge_energy = protocol_codec.decode_uint32(
        protocol_codec.UInt32Field.IOT_BMS_ENERGY, (bms[17], bms[18])
    )
    assert charge_energy >= 0 and discharge_energy >= 0
    totals = data[0x4101]
    for index in range(6):
        pair = (totals[2 * index], totals[2 * index + 1])
        field = protocol_codec.UInt32Field.IOT_TOTAL_ENERGY
        assert protocol_codec.decode_uint32(field, pair) >= 0

    # Two evidence-backed views of one battery must agree.
    dcdc = data[0x2000]
    assert protocol_codec.decode_signed16(dcdc[3]) == protocol_codec.decode_signed16(bms[6])
    assert protocol_codec.decode_signed16(dcdc[9]) == protocol_codec.decode_signed16(bms[8])


@pytest.mark.parametrize(
    ("active_w", "reactive_var"),
    [(-3000, 750), (-1, 0), (1, -1), (2500, 32767)],
)
async def test_pq_write_latches_and_drives_measured_battery_power(
    simulator: Any, active_w: int, reactive_var: int
) -> None:
    """T-SIM-POD-003 / V-WRITE / S0: [1, P, Q] latches and drives measured watts."""
    _, transport, _ = build_unit(simulator)
    await transport.connect()
    await telemetry_poll(transport)

    assert await measured_battery_watts(transport) == 0
    assert await applied_objectives(transport) == (0, 0)

    frame = protocol_codec.encode_pq_registers(active_w, reactive_var)
    await transport.write_registers(0x0200, frame)
    assert await measured_battery_watts(transport) == active_w
    assert await applied_objectives(transport) == (active_w, reactive_var)

    # A new frame replaces the latched objective; objectives never accumulate.
    replacement = protocol_codec.encode_pq_registers(active_w // 2, 0)
    await transport.write_registers(0x0200, replacement)
    assert await measured_battery_watts(transport) == active_w // 2
    assert await applied_objectives(transport) == (active_w // 2, 0)


async def test_stop_frame_returns_the_unit_to_idle_immediately(simulator: Any) -> None:
    """T-SIM-POD-004 / V-PQ-UI / S0: stop is the [1, 0, 0] frame."""
    _, transport, _ = build_unit(simulator)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(2200, 0))
    assert await measured_battery_watts(transport) == 2200

    await transport.write_registers(0x0200, protocol_codec.encode_stop_registers())

    assert await measured_battery_watts(transport) == 0
    assert await applied_objectives(transport) == (0, 0)


async def test_watchdog_expires_the_applied_setpoint_back_to_idle(simulator: Any) -> None:
    """T-SIM-POD-005 / UNC-LEASE (parameterized timing) / S0."""
    _, transport, clock = build_unit(simulator, watchdog_timeout_s=2.0)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(1800, -40))

    clock.advance(1.999)  # strictly inside the lease
    assert await measured_battery_watts(transport) == 1800
    assert await applied_objectives(transport) == (1800, -40)

    clock.advance(0.002)  # strictly past the lease
    assert await measured_battery_watts(transport) == 0
    assert await applied_objectives(transport) == (0, 0)


async def test_each_accepted_write_renews_the_watchdog_lease(simulator: Any) -> None:
    """T-SIM-POD-006 / V-PQ-UI keep-send / S0."""
    _, transport, clock = build_unit(simulator, watchdog_timeout_s=2.0)
    await transport.connect()
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-900, 0))

    clock.advance(1.5)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(-900, 0))

    clock.advance(1.0)  # past the first write's deadline, inside the renewed lease
    assert await measured_battery_watts(transport) == -900

    clock.advance(1.5)  # past the renewed deadline
    assert await measured_battery_watts(transport) == 0


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
    _, transport, _ = build_unit(simulator)
    await transport.connect()

    with pytest.raises(ValueError):
        await transport.write_registers(address, values)

    # A rejected frame neither latches an objective nor disturbs idle telemetry.
    assert await applied_objectives(transport) == (0, 0)
    assert await measured_battery_watts(transport) == 0


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


async def test_telemetry_sequence_advances_per_anchor_poll_only(simulator: Any) -> None:
    """T-SIM-POD-008 / ADR-0003 D4 / S0."""
    pod, transport, clock = build_unit(simulator)
    await transport.connect()
    await transport.read_holding(0x5000, 7)  # layout probe establishes the baseline
    baseline = pod.telemetry_sequence

    await transport.read_holding(0x1000, 21)  # PCS live block: not a poll
    await transport.read_holding(0x4101, 12)  # cumulative energy: not a poll
    await transport.write_registers(0x0200, protocol_codec.encode_stop_registers())
    assert pod.telemetry_sequence == baseline

    await telemetry_poll(transport)
    assert pod.telemetry_sequence == baseline + 1
    assert pod.telemetry_captured_at_mono == clock.monotonic()

    clock.advance(0.4)
    await telemetry_poll(transport)
    assert pod.telemetry_sequence == baseline + 2
    assert pod.telemetry_captured_at_mono == clock.monotonic()


async def test_cell_data_refreshes_on_its_own_slower_cadence(simulator: Any) -> None:
    """T-SIM-POD-009 / ADR-0003 D4 / S0: unchanged cells, fresh telemetry, growing age."""
    pod, transport, clock = build_unit(simulator, cell_poll_interval_s=5.0)
    await transport.connect()
    await telemetry_poll(transport)
    first_cells = await transport.read_holding(0x5200, 60)
    first_temperatures = await transport.read_holding(0x523C, 18)
    first_sequence = pod.cell_sequence
    first_capture = pod.cell_captured_at_mono

    clock.advance(4.9)  # inside the cell cadence
    await telemetry_poll(transport)
    assert pod.cell_sequence == first_sequence
    assert pod.cell_captured_at_mono == first_capture
    assert await transport.read_holding(0x5200, 60) == first_cells
    assert await transport.read_holding(0x523C, 18) == first_temperatures

    clock.advance(0.2)  # 5.1 s since capture: past the cadence
    await telemetry_poll(transport)
    assert pod.cell_sequence == first_sequence + 1
    assert pod.cell_captured_at_mono == clock.monotonic()
    assert pod.cell_captured_at_mono <= pod.telemetry_captured_at_mono


async def run_deterministic_scenario(simulator: Any) -> dict[str, Any]:
    """One fixed script; every number is a plain comparable value."""
    pod, transport, clock = build_unit(simulator, clock=FakeMonotonicClock(1000.0))
    captured: dict[str, Any] = {"identity": pod.identity}
    await transport.connect()
    captured["probe"] = await transport.read_holding(0x5000, 7)
    captured["telemetry_before_write"] = await telemetry_poll(transport)
    frame = protocol_codec.encode_pq_registers(-1500, 320)
    await transport.write_registers(0x0200, frame)
    clock.advance(0.25)
    captured["telemetry_latched"] = await telemetry_poll(transport)
    captured["objectives_latched"] = await transport.read_holding(0x1060, 32)
    clock.advance(5.0)  # the first lease expires; the cell cadence elapses
    await transport.write_registers(0x0200, frame)
    captured["telemetry_renewed"] = await telemetry_poll(transport)
    captured["cells_renewed"] = await transport.read_holding(0x5200, 60)
    pod.inject_fault("Stack_Fault0", 3)
    pod.inject_fault("Stack_Warning0", 11)
    captured["bms_status"] = await transport.read_holding(0x5040, 22)
    clock.advance(2.5)  # past the renewed lease
    captured["telemetry_expired"] = await telemetry_poll(transport)
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
    # The equality is meaningful: measured power follows the frame, then the expiry.
    assert protocol_codec.decode_signed16(first["telemetry_latched"][8]) == -1500
    assert protocol_codec.decode_signed16(first["telemetry_renewed"][8]) == -1500
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
    """T-SIM-POD-012 / V-LAYOUT RTU ID / S1."""
    pod, transport, clock = build_unit(simulator, identity="SIM-BEP-0042")
    assert pod.identity == "SIM-BEP-0042"
    await transport.connect()

    first = await transport.read_holding(0x8106, 2)
    clock.advance(9.7)
    await telemetry_poll(transport)
    assert await transport.read_holding(0x8106, 2) == first
    rtu_id = protocol_codec.decode_uint32(protocol_codec.UInt32Field.RTU_ID, first)
    assert rtu_id >= 0


async def test_no_socket_is_ever_opened(simulator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-SIM-TRANSPORT-003 / ADR-0003 D4 / S0: on-wire framing stays a commissioning item."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the simulator must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    _, transport, clock = build_unit(simulator)
    await transport.connect()
    await telemetry_poll(transport)
    await transport.write_registers(0x0200, protocol_codec.encode_pq_registers(400, 0))
    clock.advance(0.1)
    assert await measured_battery_watts(transport) == 400
    await transport.close()
