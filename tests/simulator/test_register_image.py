"""Literal register-image golden program for the simulated pod (MUTATION-2).

The 2026-08-22 mutation run left ~90 survivors in ``simulator/pod.py``
because every existing pin judged the served words by RANGE or COHERENCE:
a word written one offset off still passes when the checker reads the same
wrong offset (a limit landing at +14 instead of +13, an extrema index word
landing one slot late, a low/high uint32 pair swapped).  The simulator is
now the reference model with live captures cross-checking it, so a
misplaced word must fail loudly.

This program pins the EXACT words of the FULL read plan (every IoT block
plus every common block) at two scripted moments -- an objective live under
its watchdog lease, and after the lease expired with the cell image
refreshed -- against expectations RECOMPUTED from the scenario script:

- the seeded device image is re-derived from ``random.Random(seed)`` in the
  documented fixed construction order (ADR-0003 decision D4);
- every offset, scale, sentinel and word order below is transcribed from
  PROTOCOL_EVIDENCE section 5 and field-mapping-2026-08-22 (S2/S4), never
  from production code, exactly like the wire-decode contract transcribes
  the mapping;
- the device-model arithmetic (SOC drift, watchdog split, pack counts,
  extrema tie-breaking) is restated as the documented deterministic
  function of the injected clock.

Nothing imports production pod internals: the served image and the expected
image are computed on the two sides of the assertion independently.
"""

from __future__ import annotations

import importlib
import random
import zlib
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.adapters.modbus import register_layout

# --- scenario script (fixed; identical on every run) -------------------------
#
# ADR-0003 D4: the seeded image is a deterministic function of the injected
# seed consumed in a FIXED construction order.  Order transcribed from the
# ADR: SOC, SOH, cell millivolts (BIC*10), temperature words (BIC*3), then
# the six energy bases (grid buy, grid sell, load, pv, charge, discharge).
IDENTITY = "SIM-IMAGE-0001"
SEED = 20260823
BIC_COUNT = 3
CELL_COUNT = BIC_COUNT * 10  # 30
TEMP_COUNT = BIC_COUNT * 3  # 9
WATCHDOG_S = 2.0
CELL_INTERVAL_S = 5.0
PQ_ACTIVE_W = -1234  # negative P is charging on the evidenced wire convention
PQ_REACTIVE_VAR = 567
SCRIPTED_GRID_W = 900  # positive = export (PROTOCOL_EVIDENCE 4c)
SCRIPTED_LOAD_W = -400

# Commissioning-plausible static limits the device model serves (constants of
# the scenario, not read from production).
CHARGE_CURRENT_LIMIT_COUNTS = 1500  # x0.1 A
DISCHARGE_CURRENT_LIMIT_COUNTS = 1500  # x0.1 A
CHARGE_POWER_LIMIT_W = 3000
DISCHARGE_POWER_LIMIT_W = 3000
REACTIVE_LIMIT_VAR = 1000
APPARENT_LIMIT_W = 3000

# Seeded-dynamics bounds restated for the local arithmetic.
CAPACITY_WH = 50_000.0
WATT_SECONDS_PER_COUNT = 360_000.0  # 0.1 kWh per count
CELL_DRIFT_PERIOD_S = 21
CELL_DRIFT_AMPLITUDE_MV = 10
TEMPERATURE_DRIFT_PERIOD_S = 5

# --- documented block layout (PROTOCOL_EVIDENCE 5 + field-mapping S2/S4) ----
SYSTEM_BASE = 0x0100
SYSTEM_COUNT = 61
PCS_LIVE_BASE = 0x1000
PCS_LIVE_COUNT = 21
PCS_FAULT_BASE = 0x1040
PCS_DETAIL_BASE = 0x1060
PCS_DETAIL_COUNT = 32
DCDC_LIVE_BASE = 0x2000
DCDC_LIVE_COUNT = 13
DCDC_FAULT_BASE = 0x2040
DCDC_DETAIL_BASE = 0x2060
DCDC_DETAIL_COUNT = 19
TOTALS_BASE = 0x4101
TOTALS_COUNT = 12
BMS_BASE = 0x5000
BMS_COUNT = 31
BMS_FAULT_BASE = 0x5040
CELL_VOLTAGE_BASE = 0x5200
CELL_TEMPERATURE_BASE = 0x523C
CELL_BALANCE_BASE = 0x524E
DEBUG_MODE_BASE = 0x8100
NETWORK_STATUS_BASE = 0x8139
RTU_ID_BASE = 0x8106
DEVICE_PARAMETERS_BASE = 0x8102
DEVICE_PARAMETERS_COUNT = 56


class FakeMonotonicClock:
    """Injected monotonic time; the device model may read no other clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def simulator() -> Any:
    try:
        pod_module = importlib.import_module("energypod.simulator.pod")
        transport_module = importlib.import_module("energypod.simulator.transport")
    except (ImportError, AttributeError) as error:  # pragma: no cover - contract guard
        pytest.fail(f"Simulator contract is not implemented: {error}", pytrace=False)
    return SimpleNamespace(
        SimulatedEnergyPod=pod_module.SimulatedEnergyPod,
        SimulatorTransport=transport_module.SimulatorTransport,
    )


def _seeded_image() -> dict[str, Any]:
    """Re-derive the constructor's seeded device image from the seed alone."""
    rng = random.Random(SEED)  # noqa: S311 - seeded device image, not a secret
    return {
        "soc_pct": rng.uniform(60.0, 90.0),
        "soh_pct": rng.randrange(98, 101),
        "cell_base_mv": [3200 + rng.randrange(0, 201) for _ in range(CELL_COUNT)],
        "temp_base_words": [64 + rng.randrange(0, 7) for _ in range(TEMP_COUNT)],
        "grid_buy_counts": rng.randrange(10_000, 200_000),
        "grid_sell_counts": rng.randrange(10_000, 200_000),
        "load_counts": rng.randrange(10_000, 200_000),
        "pv_counts": rng.randrange(10_000, 200_000),
        "charge_base_counts": rng.randrange(10_000, 200_000),
        "discharge_base_counts": rng.randrange(10_000, 200_000),
    }


def _encode_signed16(value: int) -> int:
    """Two's-complement word, the documented int16 wire convention."""
    return value & 0xFFFF


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _first_max_index(values: list[int]) -> int:
    """First index holding the maximum (documented tie-breaking)."""
    return max(range(len(values)), key=lambda index: (values[index], -index))


def _first_min_index(values: list[int]) -> int:
    return min(range(len(values)), key=lambda index: (values[index], index))


def _cell_temperature_word(temp_words: list[int], cell_index: int) -> int:
    """Cell N reports the first sensor of the BIC that owns it (3 per BIC)."""
    sensor = (cell_index // 10) * 3
    return temp_words[min(sensor, len(temp_words) - 1)]


def _low_first(counts: int) -> tuple[int, int]:
    """Low-address word is the LOW word (mapping S1 / PROTOCOL_EVIDENCE 6)."""
    return (counts & 0xFFFF, (counts >> 16) & 0xFFFF)


class _ScriptedDevice:
    """The scenario's device state, restated with local arithmetic.

    Steps mirror the script below: idle poll, objective latched, poll under
    load, poll past the watchdog deadline with the cell cadence crossed.
    """

    def __init__(self) -> None:
        seeded = _seeded_image()
        self.soc_pct = seeded["soc_pct"]
        self.soh_pct = seeded["soh_pct"]
        self.cell_base_mv = seeded["cell_base_mv"]
        self.temp_base_words = seeded["temp_base_words"]
        self.energy = seeded
        self.debug_mode = 0  # the device boots in Normal (never parked here)
        self.charge_watt_seconds = 0.0
        self.discharge_watt_seconds = 0.0
        self.applied_active_w = 0
        self.applied_reactive_var = 0
        self.lease_deadline: float | None = None
        self.cell_mv = list(self.cell_base_mv)
        self.temp_words = list(self.temp_base_words)
        self.origin_mono = 100.0
        self.last_poll_mono = 100.0

    def apply_pq(self, now: float) -> None:
        self.applied_active_w = PQ_ACTIVE_W
        self.applied_reactive_var = PQ_REACTIVE_VAR
        self.lease_deadline = now + WATCHDOG_S

    def poll(self, now: float) -> None:
        if self.lease_deadline is not None and now >= self.lease_deadline:
            live_seconds = max(0.0, min(now, self.lease_deadline) - self.last_poll_mono)
            self._accumulate(self.applied_active_w, live_seconds)
            self.applied_active_w = 0
            self.applied_reactive_var = 0
            self.lease_deadline = None
            self._accumulate(0, now - self.last_poll_mono - live_seconds)
        else:
            self._accumulate(self.applied_active_w, now - self.last_poll_mono)
        self.last_poll_mono = now
        # The cell image refreshes only on its own slower cadence.
        if now - 100.0 >= CELL_INTERVAL_S:
            elapsed = int(now - self.origin_mono)
            drift_mv = elapsed % CELL_DRIFT_PERIOD_S - CELL_DRIFT_AMPLITUDE_MV
            drift_counts = elapsed % TEMPERATURE_DRIFT_PERIOD_S
            self.cell_mv = [_clamp(base + drift_mv, 2800, 3650) for base in self.cell_base_mv]
            self.temp_words = [max(0, base + drift_counts) for base in self.temp_base_words]

    def _accumulate(self, power_w: int, seconds: float) -> None:
        if seconds <= 0.0:
            return
        if power_w < 0:
            self.charge_watt_seconds += -power_w * seconds
        else:
            self.discharge_watt_seconds += power_w * seconds
        drift_pct = -power_w * seconds / (36.0 * CAPACITY_WH)
        self.soc_pct = _clamp(self.soc_pct + drift_pct, 5.5, 94.5)

    # --- expected served words, per block ----------------------------------

    def pack_voltage_counts(self) -> int:
        return max(1, round(sum(self.cell_mv) / 100))

    def pack_current_word(self) -> int:
        volts = self.pack_voltage_counts() / 10.0
        if volts <= 0.0:
            return 0
        return _encode_signed16(round(self.applied_active_w / volts * 10.0))

    def measured_word(self) -> int:
        return _encode_signed16(self.applied_active_w)

    def charge_counts(self) -> int:
        return min(
            999_999,
            self.energy["charge_base_counts"]
            + int(self.charge_watt_seconds / WATT_SECONDS_PER_COUNT),
        )

    def discharge_counts(self) -> int:
        return min(
            999_999,
            self.energy["discharge_base_counts"]
            + int(self.discharge_watt_seconds / WATT_SECONDS_PER_COUNT),
        )

    def expected_system_block(self) -> list[int]:
        words = [0] * SYSTEM_COUNT
        words[0] = 3  # system status: on grid
        words[1] = 1  # control mode: remote
        words[2] = 6  # work mode: remote dispatch
        words[16] = 3  # battery status: running
        words[17] = int(self.soc_pct)  # system SOC, raw percent
        words[18] = self.pack_voltage_counts()  # battery voltage x0.1 V
        words[19] = self.pack_current_word()  # battery current x0.1 A, int16
        words[20] = self.measured_word()  # battery power, int16 W
        words[22] = self.soh_pct
        return words

    def expected_pcs_live_block(self) -> list[int]:
        words = [0] * PCS_LIVE_COUNT
        words[0] = 0x0101  # packed PCS software version
        words[1] = 3  # PCS status: on grid
        words[2] = 1 if self.lease_deadline is not None else 0  # run mode
        words[3] = self.pack_voltage_counts()  # DC voltage x0.1 V
        words[13] = self.measured_word()  # PCS active power, int16 W
        words[17] = SCRIPTED_GRID_W & 0xFFFF  # per-pod CT grid word
        words[20] = SCRIPTED_LOAD_W & 0xFFFF  # per-pod CT load word
        return words

    def expected_pcs_detail_block(self) -> list[int]:
        words = [0] * PCS_DETAIL_COUNT
        words[0] = 0  # debug status readback: normal mode
        words[17] = _encode_signed16(self.applied_active_w)  # active objective
        words[18] = _encode_signed16(self.applied_reactive_var)  # reactive objective
        words[24] = APPARENT_LIMIT_W
        words[25] = DISCHARGE_POWER_LIMIT_W
        words[26] = CHARGE_POWER_LIMIT_W
        words[27] = REACTIVE_LIMIT_VAR
        return words

    def expected_dcdc_live_block(self) -> list[int]:
        words = [0] * DCDC_LIVE_COUNT
        words[0] = 0x0101
        words[3] = self.pack_voltage_counts()
        words[9] = self.measured_word()
        return words

    def expected_totals_block(self) -> list[int]:
        words: list[int] = []
        for counts in (
            self.energy["grid_buy_counts"],
            self.energy["grid_sell_counts"],
            self.energy["load_counts"],
            self.energy["pv_counts"],
            self.charge_counts(),
            self.discharge_counts(),
        ):
            words.extend(_low_first(counts))
        return words

    def expected_bms_block(self) -> list[int]:
        words = [0] * BMS_COUNT
        words[0] = 0x0101  # packed BMS version; > 10 selects the IoT layout
        words[1] = 3  # BMS status: running
        words[4] = 0x01  # string enable mask (one stack)
        words[5] = BIC_COUNT  # int16 BIC count
        words[6] = self.pack_voltage_counts()
        words[7] = self.pack_current_word()
        words[8] = self.measured_word()
        words[9] = int(self.soc_pct)
        words[10] = self.soh_pct
        words[11] = CHARGE_CURRENT_LIMIT_COUNTS
        words[12] = DISCHARGE_CURRENT_LIMIT_COUNTS
        words[13] = CHARGE_POWER_LIMIT_W
        words[14] = DISCHARGE_POWER_LIMIT_W
        words[15], words[16] = _low_first(self.charge_counts())
        words[17], words[18] = _low_first(self.discharge_counts())
        highest = _first_max_index(self.cell_mv)
        lowest = _first_min_index(self.cell_mv)
        hottest = _first_max_index(self.temp_words)
        coldest = _first_min_index(self.temp_words)
        # Extrema triples: (cell index, cell mV, cell temp+40) for the voltage
        # extrema; (sensor index, sensor temp+40, that cell's mV) for the
        # temperature extrema -- the pinned +40 offsets included.
        words[19], words[20], words[21] = (
            highest,
            self.cell_mv[highest],
            _cell_temperature_word(self.temp_words, highest) + 40,
        )
        words[22], words[23], words[24] = (
            lowest,
            self.cell_mv[lowest],
            _cell_temperature_word(self.temp_words, lowest) + 40,
        )
        words[25], words[26], words[27] = (
            hottest,
            self.temp_words[hottest] + 40,
            self.cell_mv[hottest],
        )
        words[28], words[29], words[30] = (
            coldest,
            self.temp_words[coldest] + 40,
            self.cell_mv[coldest],
        )
        return words

    def expected_identity_words(self) -> tuple[list[int], list[int], list[int]]:
        """Debug, network, RTU-ID pair, and the device-parameters mirror.

        The debug block serves the device's debug-mode word (DESIGN_POD_PARKING
        section 6; the hard-pinned [0] became the field in the same round).
        This scenario never parks, so the word is the boot default 0.
        """
        rtu_id = zlib.crc32(IDENTITY.encode("utf-8"))
        id_low, id_high = _low_first(rtu_id)
        parameters = [0] * DEVICE_PARAMETERS_COUNT
        parameters[4], parameters[5] = id_low, id_high  # mirror at 0x8102+4/+5
        return [self.debug_mode], [id_low, id_high], parameters


async def _read_full_plan(transport: Any) -> dict[int, tuple[int, ...]]:
    catalog = register_layout.RegisterCatalog()
    blocks = (*catalog.iot_reads(bic_count=BIC_COUNT), *catalog.common_reads)
    served: dict[int, tuple[int, ...]] = {}
    for block in blocks:
        served[block.address] = await transport.read_holding(block.address, block.count)
    return served


def _assert_full_plan_image(served: dict[int, tuple[int, ...]], device: _ScriptedDevice) -> None:
    """Every word of every served block, compared literally per block."""
    assert served[SYSTEM_BASE] == tuple(device.expected_system_block())
    assert served[PCS_LIVE_BASE] == tuple(device.expected_pcs_live_block())
    assert served[PCS_FAULT_BASE] == _pcs_fault_words()
    assert served[PCS_DETAIL_BASE] == tuple(device.expected_pcs_detail_block())
    assert served[DCDC_LIVE_BASE] == tuple(device.expected_dcdc_live_block())
    assert served[DCDC_FAULT_BASE] == _dcdc_fault_words()
    assert served[DCDC_DETAIL_BASE] == tuple([0] * DCDC_DETAIL_COUNT)
    assert served[TOTALS_BASE] == tuple(device.expected_totals_block())
    assert served[BMS_BASE] == tuple(device.expected_bms_block())
    assert served[BMS_FAULT_BASE] == _bms_fault_words()
    assert served[CELL_VOLTAGE_BASE] == tuple(device.cell_mv)
    assert served[CELL_TEMPERATURE_BASE] == tuple(device.temp_words)
    assert served[CELL_BALANCE_BASE] == tuple([0] * BIC_COUNT)
    debug, rtu_id_words, parameters = device.expected_identity_words()
    assert served[DEBUG_MODE_BASE] == tuple(debug)
    assert served[NETWORK_STATUS_BASE] == tuple([0])
    assert served[RTU_ID_BASE] == tuple(rtu_id_words)
    assert served[DEVICE_PARAMETERS_BASE] == tuple(parameters)


def _pcs_fault_words() -> tuple[int, ...]:
    """PCS fault block: PCS_Warning0..3 at 0..3, PCS_Fault0..5 at 16..21.

    The scenario raises the EE-calibration WARNING bit (PCS_Warning0 bit 1)
    -- the standing live-fleet signal -- and no fault bits.
    """
    words = [0] * 22
    words[0] = 1 << 1
    return tuple(words)


def _dcdc_fault_words() -> tuple[int, ...]:
    """DCDC fault block: DCDC_Fault2 bit 5 raised at offset 18."""
    words = [0] * 22
    words[18] = 1 << 5
    return tuple(words)


def _bms_fault_words() -> tuple[int, ...]:
    """BMS fault block: Stack/BECU warning and fault words, all clear."""
    return tuple([0] * 22)


async def _scripted_pod(simulator: Any) -> tuple[Any, Any, FakeMonotonicClock, _ScriptedDevice]:
    clock = FakeMonotonicClock(start=100.0)
    pod = simulator.SimulatedEnergyPod(
        clock=clock,
        identity=IDENTITY,
        bic_count=BIC_COUNT,
        seed=SEED,
        watchdog_timeout_s=WATCHDOG_S,
        cell_poll_interval_s=CELL_INTERVAL_S,
    )
    transport = simulator.SimulatorTransport(pod=pod)
    await transport.connect()

    device = _ScriptedDevice()
    # The scenario hooks (scripted CT words, two fault bits) before any poll.
    pod.script_grid_power_w(SCRIPTED_GRID_W)
    pod.script_load_power_w(SCRIPTED_LOAD_W)
    pod.inject_fault("PCS_Warning0", 1)
    pod.inject_fault("DCDC_Fault2", 5)

    pod.poll()  # t=100.0: idle sample, no load yet
    device.poll(100.0)

    clock.now = 100.5
    pod.apply_pq_frame((1, _encode_signed16(PQ_ACTIVE_W), _encode_signed16(PQ_REACTIVE_VAR)))
    device.apply_pq(100.5)
    return pod, transport, clock, device


async def test_full_plan_image_while_an_objective_is_live(simulator: Any) -> None:
    """Snapshot A: the latched objective shows in every view of it.

    The run-mode word is Remote-PQ, the measured-power words carry the
    objective's int16 image on all four evidence-backed views, the pack
    current follows P/V, and the energy counters still sit on their seeded
    bases (the 1.5 s load is far below the 0.1 kWh count resolution).
    """
    pod, transport, clock, device = await _scripted_pod(simulator)
    clock.now = 101.5
    pod.poll()
    device.poll(101.5)
    assert device.lease_deadline is not None, "scenario guard: the objective is live"

    served = await _read_full_plan(transport)
    _assert_full_plan_image(served, device)

    # Spot guards, independent of the block builders: a misplaced word cannot
    # pass because both sides of the equality were computed at documented
    # offsets -- these restate the load-bearing ones in plain sight.
    assert served[PCS_LIVE_BASE][2] == 1, "run mode: remote PQ while the lease holds"
    assert served[SYSTEM_BASE][20] == _encode_signed16(PQ_ACTIVE_W)
    assert served[BMS_BASE][13] == CHARGE_POWER_LIMIT_W
    assert served[BMS_BASE][14] == DISCHARGE_POWER_LIMIT_W
    await transport.close()


async def test_full_plan_image_after_the_watchdog_and_a_cell_refresh(simulator: Any) -> None:
    """Snapshot B: expiry returns the objective words to idle, cells refresh.

    Past the 2.0 s lease the applied/measured words all read zero, the run
    mode returns to matching-load, and the 6 s mark crosses the 5 s cell
    cadence: the cell and temperature banks move by the documented drift
    (-4 mV, +1 count at elapsed 6) and the BMS extrema follow the moved
    bank, +40 offsets included.
    """
    pod, transport, clock, device = await _scripted_pod(simulator)
    clock.now = 101.5
    pod.poll()
    device.poll(101.5)
    clock.now = 106.0
    pod.poll()
    device.poll(106.0)
    assert device.lease_deadline is None, "scenario guard: the watchdog expired"
    assert device.cell_mv != device.cell_base_mv, "scenario guard: cells refreshed"

    served = await _read_full_plan(transport)
    _assert_full_plan_image(served, device)

    assert served[PCS_LIVE_BASE][2] == 0, "run mode: back to matching load"
    assert served[PCS_DETAIL_BASE][17] == 0 and served[PCS_DETAIL_BASE][18] == 0
    assert served[SYSTEM_BASE][20] == 0 and served[BMS_BASE][8] == 0
    # The drift is the documented function of elapsed time, in plain sight.
    assert served[CELL_VOLTAGE_BASE] == tuple(
        _clamp(base + (6 % CELL_DRIFT_PERIOD_S - CELL_DRIFT_AMPLITUDE_MV), 2800, 3650)
        for base in device.cell_base_mv
    )
    assert served[CELL_TEMPERATURE_BASE] == tuple(
        base + 6 % TEMPERATURE_DRIFT_PERIOD_S for base in device.temp_base_words
    )
    await transport.close()


async def test_block_boundaries_are_device_like_in_the_full_plan(simulator: Any) -> None:
    """The last word of each block serves; one past the block refuses.

    A misplaced word is not the only survivor class: a block that quietly
    GROWS one word would shift every later base.  The one-past reads pin the
    plan's edges (the next served base is far away for each of these).
    """
    _pod, transport, _clock, _device = await _scripted_pod(simulator)
    served = await _read_full_plan(transport)

    for base in (SYSTEM_BASE, PCS_DETAIL_BASE, BMS_BASE, CELL_BALANCE_BASE):
        words = served[base]
        assert await transport.read_holding(base + len(words) - 1, 1) == (words[-1],)

    for address in (
        SYSTEM_BASE + SYSTEM_COUNT,
        PCS_LIVE_BASE + PCS_LIVE_COUNT,
        CELL_BALANCE_BASE + BIC_COUNT,
        DEBUG_MODE_BASE + 1,
        NETWORK_STATUS_BASE + 1,
    ):
        with pytest.raises(ValueError):
            await transport.read_holding(address, 1)
    await transport.close()
