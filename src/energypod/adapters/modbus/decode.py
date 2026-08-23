"""Wire decoder for one polled set of Modbus holding-register blocks.

Turns the registers a unit served from its deployed IoT read plan into a
domain :class:`~energypod.domain.observations.Observation`, translating every
field exactly per the validated live mapping
``docs/evidence/field-mapping-2026-08-22.md`` (vendor decode authority cited
per field family below).  The decoder is a pure function: no I/O, no sockets,
no mutation of the served blocks, and it never raises on wire-shaped input —
every failure mode fails closed into a returned observation with honest
quality.

Fail-closed contract (pinned by ``tests/unit/test_wire_decode.py``):

- Identity comes only from the ``0x8106`` RTU-ID pair, low-address word first
  (mapping S1, ``MiniESapp.cs:1325-1329``); the ``0x8102+4/+5`` mirror only
  cross-checks it (S2.15) and never supplies identity on its own.
- The deployed IoT plan carries no poll-sequence or capture-time registers,
  so the caller-supplied capture metadata passes through verbatim.
- A unit-grade mismatch — identity absent, mismatched or contradicted, a
  profile the unit did not serve, a topology that contradicts the configured
  cell count, or an unread fault block — still returns the decoded values
  but downgrades every GOOD quality to SUSPECT: nothing unverified is
  trusted and the observation cannot qualify.
- A source block the poll did not serve is MISSING per field; a served word
  that decodes outside its domain (percentages outside 0-100, a negative
  dynamic power limit) is BAD with the value absent — never clamped, never
  fabricated.
- The two advisory CT power words (PCS live block +17/+20, PROTOCOL_EVIDENCE
  4c) decode as signed unscaled watts and ALWAYS appear in the quality map,
  MISSING when the block was not served; they stay outside the
  safety-critical completeness set.
- The six cumulative-energy pairs (totals block 0x4101, field-mapping S2.7)
  decode as low-word-first uint32 x 0.1 kWh ADVISORY fields
  (DESIGN_ENERGY_SCORECARD section 5): the GRID PAIR under NEUTRAL A/B names
  (the pair order is vendor-confirmed; the buy/sell ROLE labels are
  evidence-open, S5 A-1, and are never applied here), the load/PV and the
  capture-confirmed charge/discharge pair under their vendor names.  All six
  quality keys are always emitted, MISSING for unserved pairs, and stay
  outside the safety-critical completeness set.
- The balance words (S5 A-15) stay ambiguous with no channel in the
  observation and are never read here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from energypod.adapters.modbus import faults, protocol_codec, register_layout
from energypod.domain.observations import DataQuality, Observation, UnitLifecycle

# Block base addresses of the deployed IoT plan (field-mapping S2 per block).
_SYSTEM_BLOCK_BASE = 0x0100
_PCS_LIVE_BLOCK_BASE = 0x1000
_PCS_FAULT_BLOCK_BASE = 0x1040
_DCDC_FAULT_BLOCK_BASE = 0x2040
_BMS_BLOCK_BASE = 0x5000
_BMS_FAULT_BLOCK_BASE = 0x5040
_CELL_VOLTAGE_BASE = 0x5200
_CELL_TEMPERATURE_BASE = 0x523C
_RTU_ID_BASE = 0x8106
_DEVICE_PARAMETERS_BASE = 0x8102
_DEBUG_MODE_BLOCK_BASE = 0x8100
# Cumulative energy totals, field-mapping S2.7 (SysControl.cs:773 read,
# :779-784 decode): six low-word-first uint32 x 0.1 kWh pairs in vendor order
# -- grid pair (NEUTRAL A/B: the pair ORDER is confirmed, the buy/sell ROLE
# labels are evidence-open, S5 A-1), load, PV, BMS charge, BMS discharge.
_ENERGY_TOTALS_BLOCK_BASE = 0x4101
_ENERGY_PAIR_COUNT = 6
_KWH_PER_COUNT = 0.1

# PCS live block advisory offsets, PROTOCOL_EVIDENCE 4c (SysControl.cs:500
# and :503): the per-pod CT external-grid power at +17 and the load power at
# +20, both int16 unscaled watts.  Sign live-proven: negative = import,
# positive = export.  The system-overview grid word at 0x0100+55 is a
# separately-filtered cross-check and is deliberately NOT merged into this
# value.
_PCS_GRID_POWER_OFFSET = 17
_PCS_LOAD_POWER_OFFSET = 20

# BMS live block offsets, field-mapping S2.8 (SysControl.cs:731-739).
_BMS_VOLTAGE_OFFSET = 6
_BMS_CURRENT_OFFSET = 7
_BMS_POWER_OFFSET = 8
_BMS_SOC_OFFSET = 9
_BMS_SOH_OFFSET = 10
_BMS_CHARGE_LIMIT_OFFSET = 13
_BMS_DISCHARGE_LIMIT_OFFSET = 14

# System overview block, field-mapping S2.13 (SysControl.cs:416): SOC is the
# low byte of word 17.  Device parameters, S2.15 (SysControl.cs:1544): the
# RTU-ID mirror sits at offsets 4-5, low word first.
_SYSTEM_SOC_OFFSET = 17
_RTU_ID_MIRROR_OFFSET = 4

# Advisory device-mode words (2026-08-23 incident 1, field-mapping S2.13/
# S2.14/S2): the system overview carries ctrlMode at +1 (enum 1 Remote /
# 2 Local, GlobalFun.cs:204-211) and workMode at +2 (kept RAW: MID's
# captured 7 is deliberately unmapped, field-mapping A-17); the PCS live
# block carries runMode at +2 (enum 0 Matching Load / 1 Remote PQ Power,
# GlobalFun.cs:178-188); the debug-mode readback 0x8100+0 is the vendor
# PQ-dispatch precondition (MiniESapp.cs:2180-2184 refuses when nonzero).
# They decode as raw advisory words, never as quality-map keys.
_CTRL_MODE_OFFSET = 1
_WORK_MODE_OFFSET = 2
_PCS_RUN_MODE_OFFSET = 2

# Topology formulae, field-mapping S1/S4 (SysControl.cs:839-844, 869-890).
_CELLS_PER_BIC = 10
_MAX_CELLS = 100
_TEMPERATURES_PER_BIC = 3

# Scaling families, field-mapping S3: 0.1 V pack voltage, 0.1 A pack current,
# unscaled-W power and dynamic limits, raw-mV cells, raw-minus-40 Celsius.
_VOLTAGE_SCALE_V = 0.1
_CURRENT_SCALE_A = 0.1
_WATT_SCALE = 1.0
_MILLIVOLTS_PER_VOLT = 1000.0
_TEMPERATURE_OFFSET_C = 40.0

_FAULT_BLOCK_SOURCES: tuple[tuple[int, faults.FaultBlock], ...] = (
    (_PCS_FAULT_BLOCK_BASE, faults.FaultBlock.IOT_PCS),
    (_DCDC_FAULT_BLOCK_BASE, faults.FaultBlock.IOT_DCDC),
    (_BMS_FAULT_BLOCK_BASE, faults.FaultBlock.IOT_BMS),
)


def _served_word(block: Sequence[int] | None, offset: int) -> int | None:
    """One masked register word, or ``None`` when the block did not serve it."""
    if block is None or offset >= len(block):
        return None
    return int(block[offset]) & 0xFFFF


def _rtu_id(words: Sequence[int] | None) -> int | None:
    """32-bit RTU ID with the lower-address word as the low word (mapping S1)."""
    if words is None or len(words) < 2:
        return None
    return ((int(words[1]) & 0xFFFF) << 16) | (int(words[0]) & 0xFFFF)


def _measurement(
    block: Sequence[int] | None, offset: int, scale: float
) -> tuple[float | None, DataQuality]:
    """Signed measurement at ``scale``; MISSING when the word was not served."""
    word = _served_word(block, offset)
    if word is None:
        return None, DataQuality.MISSING
    return protocol_codec.decode_signed16(word) * scale, DataQuality.GOOD


def _percentage(block: Sequence[int] | None, offset: int) -> tuple[float | None, DataQuality]:
    """Percentage domain 0-100 (mapping S2.8/S2.13); outside the domain is BAD."""
    word = _served_word(block, offset)
    if word is None:
        return None, DataQuality.MISSING
    value = float(protocol_codec.decode_signed16(word))
    if not 0.0 <= value <= 100.0:
        return None, DataQuality.BAD
    return value, DataQuality.GOOD


def _power_limit(block: Sequence[int] | None, offset: int) -> tuple[float | None, DataQuality]:
    """Dynamic power limit in unscaled watts; the domain forbids negatives."""
    word = _served_word(block, offset)
    if word is None:
        return None, DataQuality.MISSING
    value = float(protocol_codec.decode_signed16(word))
    if value < 0.0:
        return None, DataQuality.BAD
    return value, DataQuality.GOOD


def _decode_cell_voltages(
    block: Sequence[int] | None, expected_count: int
) -> tuple[tuple[float, ...], DataQuality]:
    """Raw millivolt words to volts (mapping S4, SysControl.cs:852)."""
    if block is None:
        return (), DataQuality.MISSING
    voltages = tuple(float(int(word) & 0xFFFF) / _MILLIVOLTS_PER_VOLT for word in block)
    if len(voltages) != expected_count:
        # A served bank that contradicts the commissioned topology must not
        # fabricate a complete cell picture.
        return voltages, DataQuality.BAD
    return voltages, DataQuality.GOOD


def _decode_temperatures(
    block: Sequence[int] | None, expected_count: int | None
) -> tuple[tuple[float, ...], DataQuality]:
    """Raw-minus-40 Celsius per served sensor (mapping S4, SysControl.cs:881-890)."""
    if block is None:
        return (), DataQuality.MISSING
    temperatures = tuple(float(int(word) & 0xFFFF) - _TEMPERATURE_OFFSET_C for word in block)
    if expected_count is None or len(temperatures) != expected_count:
        return temperatures, DataQuality.BAD
    return temperatures, DataQuality.GOOD


def decode_energy_totals(
    block: Sequence[int] | None,
) -> tuple[tuple[float | None, DataQuality], ...]:
    """Decode the six cumulative-energy pairs of the totals block 0x4101.

    Field-mapping S2.7 (SysControl.cs:779-784): every pair is a low-word-first
    ``uint32`` count scaled x 0.1 kWh, in vendor order -- grid pair (NEUTRAL
    A/B naming; the buy/sell role labels are evidence-open, S5 A-1), load, PV,
    BMS charge, BMS discharge.  A pair whose block the poll did not serve (or
    served only partially) is ``(None, MISSING)`` -- never zero-filled.  This
    is the one implementation of the pair arithmetic: the live wire decode and
    the simulator's composed decode both call it, so the reference model
    cannot drift from production.
    """
    decoded: list[tuple[float | None, DataQuality]] = []
    for pair in range(_ENERGY_PAIR_COUNT):
        low = _served_word(block, pair * 2)
        high = _served_word(block, pair * 2 + 1)
        if low is None or high is None:
            decoded.append((None, DataQuality.MISSING))
            continue
        counts = (high << 16) | low
        decoded.append((float(counts) * _KWH_PER_COUNT, DataQuality.GOOD))
    return tuple(decoded)


def _decode_fault_blocks(
    blocks: Mapping[int, Sequence[int]],
) -> tuple[frozenset[str], frozenset[str], bool]:
    """Decode the PCS/DCDC/BMS warning and fault words (mapping S2.2/S2.5/S2.9).

    Returns the active warning codes, the active fault codes, and whether the
    poll served every fault block: unread fault words must never be reported
    as an absence of faults, so an unserved block fails the qualification.
    """
    warning_codes: set[str] = set()
    fault_codes: set[str] = set()
    complete = True
    for base, block_id in _FAULT_BLOCK_SOURCES:
        served = blocks.get(base)
        if served is None:
            complete = False
            continue
        try:
            words = faults.extract_fault_block(block_id, tuple(served))
        except faults.FaultDecodeError:
            complete = False
            continue
        layout = faults.FAULT_BLOCK_LAYOUTS[block_id]
        for prefix, raw_word in words.items():
            codes = {signal.code for signal in faults.decode_fault_word(prefix, raw_word)}
            if prefix in layout.fault_offsets:
                fault_codes |= codes
            else:
                warning_codes |= codes
    return frozenset(warning_codes), frozenset(fault_codes), complete


def decode_observation(
    layout_probe: register_layout.LayoutProbe,
    blocks: Mapping[int, Sequence[int]],
    *,
    unit_id: str,
    expected_identity: str,
    expected_profile: str,
    expected_cell_count: int,
    wall_timestamp: datetime,
    captured_at_mono: float,
    sequence: int,
    cell_captured_at_mono: float | None = None,
    cell_sequence: int | None = None,
    lifecycle: UnitLifecycle = UnitLifecycle.OBSERVE_ONLY,
) -> Observation:
    """Decode one polled set of holding-register blocks into an observation.

    The blocks are keyed by zero-based base address with the register words
    verbatim as captured.  The probe, expectations and capture metadata come
    from the calling poll actor; the wire plan itself carries no sequence or
    timestamp, so the metadata passes through unchanged.  Never raises on
    wire-shaped input: every failure mode is reported honestly in the
    returned observation.
    """
    # Identity, field-mapping S1: the 0x8106 pair is the only identity source
    # and the 0x8102 mirror (S2.15) only contradicts it when both are polled.
    rtu_id = _rtu_id(blocks.get(_RTU_ID_BASE))
    device_identity = None if rtu_id is None else f"byd-{rtu_id:08x}"
    identity_verified = device_identity is not None and device_identity == expected_identity
    parameters = blocks.get(_DEVICE_PARAMETERS_BASE)
    mirror_words = (
        parameters[_RTU_ID_MIRROR_OFFSET : _RTU_ID_MIRROR_OFFSET + 2]
        if parameters is not None
        else None
    )
    mirror_id = _rtu_id(mirror_words)
    if rtu_id is not None and mirror_id is not None and mirror_id != rtu_id:
        identity_verified = False

    # Profile and topology from the layout probe, field-mapping S1: the
    # vendor cell-count formula min(BIC x 10, 100) reconciles the probe with
    # the configured expectation.
    served_profile = layout_probe.layout.value
    probe_cell_count = min(layout_probe.bic_count * _CELLS_PER_BIC, _MAX_CELLS)
    topology_verified = layout_probe.topology_valid and probe_cell_count == expected_cell_count
    expected_temperature_count = (
        layout_probe.bic_count * _TEMPERATURES_PER_BIC if layout_probe.topology_valid else None
    )

    warning_codes, fault_codes, fault_blocks_served = _decode_fault_blocks(blocks)

    # BMS live block, field-mapping S2.8 (SysControl.cs:731-739): 0.1 V pack
    # voltage, 0.1 A pack current, unscaled-W power and dynamic limits.
    bms = blocks.get(_BMS_BLOCK_BASE)
    pack_voltage_v, pack_voltage_quality = _measurement(bms, _BMS_VOLTAGE_OFFSET, _VOLTAGE_SCALE_V)
    pack_current_a, pack_current_quality = _measurement(bms, _BMS_CURRENT_OFFSET, _CURRENT_SCALE_A)
    battery_watts, watts_quality = _measurement(bms, _BMS_POWER_OFFSET, _WATT_SCALE)
    bms_soc_pct, bms_soc_quality = _percentage(bms, _BMS_SOC_OFFSET)
    soh_pct, soh_quality = _percentage(bms, _BMS_SOH_OFFSET)
    charge_limit_w, charge_limit_quality = _power_limit(bms, _BMS_CHARGE_LIMIT_OFFSET)
    discharge_limit_w, discharge_limit_quality = _power_limit(bms, _BMS_DISCHARGE_LIMIT_OFFSET)

    # System SOC, field-mapping S2.13 (SysControl.cs:416): the system
    # overview block supplies the system controller's own SOC when polled.
    # The deployed IoT capture set carries no system block, so the BMS SOC —
    # the only mapping-stated SOC source in it — stands in for both views
    # with the same honest quality.
    system = blocks.get(_SYSTEM_BLOCK_BASE)
    system_soc_word = _served_word(system, _SYSTEM_SOC_OFFSET)
    if system_soc_word is None:
        system_soc_pct, system_soc_quality = bms_soc_pct, bms_soc_quality
    else:
        system_soc_pct = float(system_soc_word & 0xFF)
        if not 0.0 <= system_soc_pct <= 100.0:
            system_soc_pct, system_soc_quality = None, DataQuality.BAD
        else:
            system_soc_quality = DataQuality.GOOD

    # Cell and temperature blocks, field-mapping S4.
    cell_voltages_v, cell_quality = _decode_cell_voltages(
        blocks.get(_CELL_VOLTAGE_BASE), expected_cell_count
    )
    temperatures_c, temperature_quality = _decode_temperatures(
        blocks.get(_CELL_TEMPERATURE_BASE), expected_temperature_count
    )

    # Advisory per-pod CT power, PROTOCOL_EVIDENCE 4c: the PCS live block is
    # the control-grade source (tier-promotable to the control-rate core).
    # Both keys are ALWAYS emitted — MISSING when the poll did not serve the
    # block — because the observation's quality map is the honest inventory
    # of what this poll saw, and the export bound fails closed on MISSING
    # without touching the safety-critical completeness set.
    pcs_live = blocks.get(_PCS_LIVE_BLOCK_BASE)
    grid_power_w, grid_power_quality = _measurement(pcs_live, _PCS_GRID_POWER_OFFSET, _WATT_SCALE)
    load_power_w, load_power_quality = _measurement(pcs_live, _PCS_LOAD_POWER_OFFSET, _WATT_SCALE)

    # Advisory device-mode words: raw registers, present only when their block
    # was served.  They carry the vendor's dispatch preconditions but never
    # enter the quality map or the safety-critical completeness set.
    debug_mode_w = _served_word(blocks.get(_DEBUG_MODE_BLOCK_BASE), 0)
    ctrl_mode_w = _served_word(system, _CTRL_MODE_OFFSET)
    work_mode_w = _served_word(system, _WORK_MODE_OFFSET)
    run_mode_w = _served_word(pcs_live, _PCS_RUN_MODE_OFFSET)

    # Advisory cumulative energy (DESIGN_ENERGY_SCORECARD section 5): the
    # cold-ring totals block.  All six quality keys are ALWAYS emitted --
    # MISSING when the poll did not serve the block or served a partial pair
    # -- exactly like the CT pair: the quality map is the honest inventory of
    # what this poll saw, and an unserved energy block never refuses power.
    energy_totals = decode_energy_totals(blocks.get(_ENERGY_TOTALS_BLOCK_BASE))
    (
        (energy_grid_a_kwh, energy_grid_a_quality),
        (energy_grid_b_kwh, energy_grid_b_quality),
        (energy_load_kwh, energy_load_quality),
        (energy_pv_kwh, energy_pv_quality),
        (energy_charge_kwh, energy_charge_quality),
        (energy_discharge_kwh, energy_discharge_quality),
    ) = energy_totals

    quality: dict[str, DataQuality] = {
        "system_soc_pct": system_soc_quality,
        "bms_soc_pct": bms_soc_quality,
        "soh_pct": soh_quality,
        "battery_watts": watts_quality,
        "pack_voltage_v": pack_voltage_quality,
        "pack_current_a": pack_current_quality,
        "dynamic_charge_limit_w": charge_limit_quality,
        "dynamic_discharge_limit_w": discharge_limit_quality,
        "cell_voltages_v": cell_quality,
        "temperatures_c": temperature_quality,
        "grid_power_w": grid_power_quality,
        "load_power_w": load_power_quality,
        "energy_grid_a_kwh": energy_grid_a_quality,
        "energy_grid_b_kwh": energy_grid_b_quality,
        "energy_load_kwh": energy_load_quality,
        "energy_pv_kwh": energy_pv_quality,
        "energy_charge_kwh": energy_charge_quality,
        "energy_discharge_kwh": energy_discharge_quality,
    }

    # Fail-closed downgrade: with identity, profile, topology or fault-block
    # verification failed the values may still be reported, but nothing in
    # the observation may be trusted.
    unit_verified = (
        identity_verified
        and served_profile == expected_profile
        and topology_verified
        and fault_blocks_served
    )
    if not unit_verified:
        quality = {
            field: DataQuality.SUSPECT if current is DataQuality.GOOD else current
            for field, current in quality.items()
        }

    return Observation(
        unit_id=unit_id,
        device_identity=device_identity,
        wall_timestamp=wall_timestamp,
        captured_at_mono=captured_at_mono,
        sequence=sequence,
        lifecycle=lifecycle,
        protocol_profile=served_profile,
        system_soc_pct=system_soc_pct,
        bms_soc_pct=bms_soc_pct,
        soh_pct=soh_pct,
        battery_watts=battery_watts,
        pack_voltage_v=pack_voltage_v,
        pack_current_a=pack_current_a,
        dynamic_charge_limit_w=charge_limit_w,
        dynamic_discharge_limit_w=discharge_limit_w,
        expected_cell_count=expected_cell_count,
        cell_voltages_v=cell_voltages_v,
        cell_captured_at_mono=cell_captured_at_mono,
        cell_sequence=cell_sequence,
        expected_temperature_count=expected_temperature_count,
        temperatures_c=temperatures_c,
        grid_power_w=grid_power_w,
        load_power_w=load_power_w,
        debug_mode_w=debug_mode_w,
        ctrl_mode_w=ctrl_mode_w,
        work_mode_w=work_mode_w,
        run_mode_w=run_mode_w,
        energy_grid_a_kwh=energy_grid_a_kwh,
        energy_grid_b_kwh=energy_grid_b_kwh,
        energy_load_kwh=energy_load_kwh,
        energy_pv_kwh=energy_pv_kwh,
        energy_charge_kwh=energy_charge_kwh,
        energy_discharge_kwh=energy_discharge_kwh,
        active_faults=fault_codes,
        active_warnings=warning_codes,
        quality=quality,
    )
