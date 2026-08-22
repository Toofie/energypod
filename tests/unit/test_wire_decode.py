"""Wire-decode contract for production telemetry over the deployed IoT layout.

The module under test is ``energypod.adapters.modbus.decode``.  It turns one
polled set of Modbus holding-register blocks into a domain ``Observation``
using the real register plan.  Every vector in this file is one of the two
authorized live captures — ``docs/evidence/live-capture-2026-08-22.json``
(the 13-block IoT read plan per unit) and
``docs/evidence/live-capture-followup-2026-08-22.json`` (the common system,
debug, network, and device-parameter blocks) — decoded per the authoritative
mapping ``docs/evidence/field-mapping-2026-08-22.md``.  No test opens a
socket, connects to, or otherwise contacts hardware; the capture files are
immutable golden input and the expectations are recomputed from their
registers (mapping offsets and scales are transcribed from the mapping
document, never from production code).

Pinned contract (the red phase fails cleanly while ``decode`` is absent)::

    decode_observation(
        layout_probe,            # register_layout.detect_layout result for this poll
        blocks,                  # Mapping[base address -> register words], as captured
        *,
        unit_id: str,
        expected_identity: str,  # pinned per-unit stable identity, e.g. "byd-2c225097"
        expected_profile: str,   # ProtocolLayout value, e.g. "iot"
        expected_cell_count: int,
        wall_timestamp: datetime,        # UTC, caller-supplied capture metadata
        captured_at_mono: float,
        sequence: int,
        cell_captured_at_mono: float | None = None,   # defaults to captured_at_mono
        cell_sequence: int | None = None,             # defaults to sequence
        lifecycle: UnitLifecycle = UnitLifecycle.OBSERVE_ONLY,
    ) -> Observation

Rules the tests enforce:

- Identity comes only from the ``0x8106`` RTU-ID pair, low word first
  (field-mapping section 1): ``device_identity == f"byd-{rtu_id:08x}"``.
  The deployed IoT register plan carries no poll sequence or capture-time
  registers, so sequence and capture times are caller-supplied capture
  metadata and must pass through verbatim.
- Field sources (field-mapping sections 2.8 and 4): BMS block ``0x5000`` —
  pack voltage ``s16@6 * 0.1 V``, pack current ``s16@7 * 0.1 A``, signed
  battery power ``s16@8`` (W, unscaled), BMS SOC ``@9``, SOH ``@10``,
  dynamic charge/discharge power limits ``@13/@14`` (W, unscaled); cells
  ``0x5200`` raw mV -> V; temperatures ``0x523C`` raw - 40.  The system
  overview block ``0x0100`` contributes ``system_soc_pct`` from ``@17``
  (low byte) when it is part of the poll; the deployed IoT capture set does
  not include that block, and the only mapping-stated SOC source in it is
  the BMS block, so with the system block absent the BMS SOC stands in for
  both SOC views with honest quality.  Pack measurements never come from the
  system block while the BMS block is the mapping's stated source.
- Qualification is fail-closed: a mismatched ``expected_identity`` or
  ``expected_profile``, a topology that contradicts the expected cell count,
  an invalid layout probe, an absent identity block, contradicted identity
  evidence (the ``0x8102`` mirror disagrees with ``0x8106``), any missing
  required plan block, or a register word that decodes outside the domain
  (SOC/SOH outside 0-100, a negative dynamic limit) each yield a returned,
  non-qualifying observation with honest quality (absent sources MISSING,
  undecodable values BAD) — never a raise and never a fabricated value.
- Ambiguous fields stay out of the observation: energy-counter role labels
  and balance words (mapping section 5, A-1/A-15) exert no influence on the
  decoded observation.
- Required blocks for a qualifying IoT decode are the blocks the observation
  consumes — BMS/cell/temperature telemetry, the three fault blocks (unread
  fault words must not be reported as an absence of faults), and the identity
  pair: ``0x1040, 0x2040, 0x5000, 0x5040, 0x5200, 0x523C, 0x8106``.  The
  common blocks ``0x0100, 0x8100, 0x8102, 0x8139`` are optional for the
  observation (``0x0100`` supplies system SOC, ``0x8102`` only cross-checks
  identity), and the remaining deployed-plan blocks ``0x1000, 0x1060,
  0x2000, 0x2060, 0x4101, 0x524E`` are not consumed: their absence or
  content must not influence the decode (poll integrity beyond the decode is
  the owning actor's responsibility).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from energypod.adapters.modbus import protocol_codec, register_layout
from energypod.domain.observations import DataQuality, Observation, UnitLifecycle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CAPTURE_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-2026-08-22.json"
_FOLLOWUP_PATH = _REPO_ROOT / "docs" / "evidence" / "live-capture-followup-2026-08-22.json"

_UNIT_KEYS = ("MID", "RHS", "LHS")
_IOT_PROFILE = register_layout.ProtocolLayout.IOT.value
_LEGACY_PROFILE = register_layout.ProtocolLayout.LEGACY.value

_BMS_BASE = 0x5000
_CELL_VOLTAGE_BASE = 0x5200
_CELL_TEMPERATURE_BASE = 0x523C
_BALANCE_BASE = 0x524E
_SYSTEM_BASE = 0x0100
_TOTALS_BASE = 0x4101
_RTU_ID_BASE = 0x8106
_DEVICE_PARAMETERS_BASE = 0x8102

_REQUIRED_BLOCKS: tuple[int, ...] = (
    0x1040,
    0x2040,
    _BMS_BASE,
    0x5040,
    _CELL_VOLTAGE_BASE,
    _CELL_TEMPERATURE_BASE,
    _RTU_ID_BASE,
)

# The PCS live block 0x1000 became advisory-CONSUMED with the excess-solar
# contract (grid_power_w at +17, load_power_w at +20 — PROTOCOL_EVIDENCE 4c);
# it no longer belongs in the unconsumed set below.
_UNCONSUMED_BLOCKS: tuple[int, ...] = (
    0x1060,
    0x2000,
    0x2060,
    _TOTALS_BASE,
    _BALANCE_BASE,
)

# Caller-supplied capture metadata (the wire plan carries none of it).
_WALL_TIMESTAMP = datetime(2026, 8, 22, 6, 30, 15, tzinfo=UTC)
_CAPTURED_AT_MONO = 1_000.0
_SEQUENCE = 41
_CELL_CAPTURED_AT_MONO = 1_002.5
_CELL_SEQUENCE = 9

# Spot engineering values transcribed from field-mapping-2026-08-22 sections
# 1, 2.8, and 4 (and the follow-up capture's system block).  They guard the
# recomputed expectations against an indexing error in either direction.
_SPOT: dict[str, dict[str, Any]] = {
    "MID": {
        "identity": "byd-2c225097",
        "soc": 10.0,
        "followup_system_soc": 10.0,
        "soh": 100.0,
        "pack_v": 192.3,
        "pack_i": 0.2,
        "watts": 38.0,
        "charge_limit_w": 7692.0,
        "discharge_limit_w": 0.0,
        "cells": 60,
        "cell_min_v": 3.204,
        "cell_max_v": 3.208,
        "temps": 18,
        "temp_min_c": 23.0,
        "temp_max_c": 28.0,
    },
    "RHS": {
        "identity": "byd-2c225076",
        "soc": 73.0,
        "followup_system_soc": 71.0,
        "soh": 100.0,
        "pack_v": 163.3,
        "pack_i": 7.3,
        "watts": 1192.0,
        "charge_limit_w": 6532.0,
        "discharge_limit_w": 6532.0,
        "cells": 50,
        "cell_min_v": 3.265,
        "cell_max_v": 3.271,
        "temps": 15,
        "temp_min_c": 23.0,
        "temp_max_c": 28.0,
    },
    "LHS": {
        "identity": "byd-2c225095",
        "soc": 57.0,
        "followup_system_soc": 53.0,
        "soh": 100.0,
        "pack_v": 195.3,
        "pack_i": 10.1,
        "watts": 1972.0,
        "charge_limit_w": 7812.0,
        "discharge_limit_w": 7812.0,
        "cells": 60,
        "cell_min_v": 3.253,
        "cell_max_v": 3.260,
        "temps": 18,
        "temp_min_c": 22.0,
        "temp_max_c": 27.0,
    },
}


class _MissingContract:
    def __init__(self, message: str) -> None:
        self.message = message

    def __getattr__(self, name: str) -> Any:
        pytest.fail(self.message, pytrace=False)


@pytest.fixture(scope="module")
def wire_decode() -> Any:
    try:
        return importlib.import_module("energypod.adapters.modbus.decode")
    except (ImportError, AttributeError) as error:
        return _MissingContract(f"Wire-decode contract is not implemented: {error}")


@pytest.fixture(scope="module")
def capture() -> Any:
    return json.loads(_CAPTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def followup() -> Any:
    return json.loads(_FOLLOWUP_PATH.read_text(encoding="utf-8"))


def _blocks_of(capture: Any, unit_key: str) -> dict[int, tuple[int, ...]]:
    """Served blocks keyed by zero-based base address, words verbatim."""
    return {
        int(block["address"]): tuple(int(word) for word in block["registers"])
        for block in capture[unit_key]["blocks"]
    }


def _merged_blocks(capture: Any, followup: Any, unit_key: str) -> dict[int, tuple[int, ...]]:
    """Primary IoT blocks plus the follow-up capture's common blocks."""
    merged = _blocks_of(capture, unit_key)
    for block in followup[unit_key]["blocks"]:
        merged[int(block["address"])] = tuple(int(word) for word in block["registers"])
    return merged


def _probe_of(capture: Any, unit_key: str) -> register_layout.LayoutProbe:
    return register_layout.detect_layout(_blocks_of(capture, unit_key)[_BMS_BASE][:7])


def _identity_of(blocks: Mapping[int, Sequence[int]]) -> str:
    """Stable identity per field-mapping section 1: low word first."""
    words = blocks[_RTU_ID_BASE]
    rtu_id = (int(words[1]) << 16) | int(words[0])
    return f"byd-{rtu_id:08x}"


def _decode_unit(
    wire_decode: Any,
    capture: Any,
    unit_key: str,
    *,
    blocks: Mapping[int, Sequence[int]] | None = None,
    probe: register_layout.LayoutProbe | None = None,
    **overrides: Any,
) -> Any:
    """Decode one captured unit with its own pinned expectations."""
    served: dict[int, tuple[int, ...]] = (
        _blocks_of(capture, unit_key)
        if blocks is None
        else {address: tuple(words) for address, words in blocks.items()}
    )
    kwargs: dict[str, Any] = {
        "unit_id": unit_key,
        "expected_identity": _identity_of(_blocks_of(capture, unit_key)),
        "expected_profile": _IOT_PROFILE,
        "expected_cell_count": int(capture[unit_key]["bic_count"]) * 10,
        "wall_timestamp": _WALL_TIMESTAMP,
        "captured_at_mono": _CAPTURED_AT_MONO,
        "sequence": _SEQUENCE,
    }
    kwargs.update(overrides)
    if probe is None:
        probe = _probe_of(capture, unit_key)
    return wire_decode.decode_observation(probe, served, **kwargs)


def _assert_fully_unqualified(observation: Any) -> None:
    """Identity-grade failure: nothing in the observation may be trusted."""
    assert observation.safety_data_complete is False
    trusted = [
        field for field, quality in observation.quality.items() if quality is DataQuality.GOOD
    ]
    assert trusted == [], f"unverified unit must not report trusted fields: {trusted}"


def test_identity_comes_from_the_rtu_id_pair_low_word_first(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-001 / field-mapping S1, live-capture (E1) / S1."""
    for key in _UNIT_KEYS:
        blocks = _blocks_of(capture, key)
        observation = _decode_unit(wire_decode, capture, key)
        words = blocks[_RTU_ID_BASE]
        high_first = (int(words[0]) << 16) | int(words[1])
        assert observation.device_identity == _identity_of(blocks)
        assert observation.device_identity == _SPOT[key]["identity"]
        assert f"byd-{high_first:08x}" != _SPOT[key]["identity"], "word order must be low-first"


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_all_high_confidence_fields_decode_on_every_captured_unit(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-002 / field-mapping S2.8,S4 / S0."""
    blocks = _blocks_of(capture, unit_key)
    bms = blocks[_BMS_BASE]
    cells = blocks[_CELL_VOLTAGE_BASE]
    temperatures = blocks[_CELL_TEMPERATURE_BASE]
    observation = _decode_unit(wire_decode, capture, unit_key)

    assert observation.unit_id == unit_key
    assert observation.protocol_profile == _IOT_PROFILE
    assert observation.lifecycle is UnitLifecycle.OBSERVE_ONLY

    # BMS core, field-mapping section 2.8 offsets 6..14.
    signed = protocol_codec.decode_signed16
    assert observation.pack_voltage_v == pytest.approx(signed(bms[6]) * 0.1)
    assert observation.pack_current_a == pytest.approx(signed(bms[7]) * 0.1)
    assert observation.battery_watts == pytest.approx(float(signed(bms[8])))
    assert observation.bms_soc_pct == pytest.approx(float(bms[9]))
    assert observation.soh_pct == pytest.approx(float(bms[10]))
    assert observation.dynamic_charge_limit_w == pytest.approx(float(bms[13]))
    assert observation.dynamic_discharge_limit_w == pytest.approx(float(bms[14]))

    # The deployed IoT capture set carries no system block: the BMS SOC is
    # the only mapping-stated SOC source and stands in for both views.
    assert observation.system_soc_pct == pytest.approx(float(bms[9]))

    # Cell and temperature blocks, field-mapping section 4.
    cell_count = int(capture[unit_key]["bic_count"]) * 10
    assert observation.expected_cell_count == cell_count
    assert observation.cell_voltages_v == pytest.approx(tuple(word / 1000.0 for word in cells))
    assert len(observation.cell_voltages_v) == cell_count
    assert observation.cells_complete is True
    assert observation.expected_temperature_count == _probe_of(capture, unit_key).bic_count * 3
    assert observation.temperatures_c == pytest.approx(tuple(word - 40 for word in temperatures))
    assert observation.temperatures_complete is True

    # Coherent captures qualify across the whole declared quality surface.
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the wire
    # decoder always emits the twelve-key quality shape — the ten safety
    # fields plus the two advisory CT fields (MISSING when the PCS block is
    # unserved by the read plan).
    assert set(observation.quality) == set(Observation.QUALITY_FIELDS) | set(
        Observation.ADVISORY_QUALITY_FIELDS
    )
    assert all(
        observation.quality[field] is DataQuality.GOOD
        for field in Observation.QUALITY_FIELDS
    )
    assert observation.safety_data_complete is True


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_spot_engineering_values_match_the_field_mapping_tables(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-003 / field-mapping S2.8,S4 tables / S0."""
    spot = _SPOT[unit_key]
    observation = _decode_unit(wire_decode, capture, unit_key)

    assert observation.device_identity == spot["identity"]
    assert observation.system_soc_pct == pytest.approx(spot["soc"])
    assert observation.bms_soc_pct == pytest.approx(spot["soc"])
    assert observation.soh_pct == pytest.approx(spot["soh"])
    assert observation.pack_voltage_v == pytest.approx(spot["pack_v"])
    assert observation.pack_current_a == pytest.approx(spot["pack_i"])
    assert observation.battery_watts == pytest.approx(spot["watts"])
    assert observation.dynamic_charge_limit_w == pytest.approx(spot["charge_limit_w"])
    assert observation.dynamic_discharge_limit_w == pytest.approx(spot["discharge_limit_w"])
    assert len(observation.cell_voltages_v) == spot["cells"]
    assert observation.cell_min_voltage_v == pytest.approx(spot["cell_min_v"])
    assert observation.cell_max_voltage_v == pytest.approx(spot["cell_max_v"])
    assert len(observation.temperatures_c) == spot["temps"]
    assert observation.temperature_min_c == pytest.approx(spot["temp_min_c"])
    assert observation.temperature_max_c == pytest.approx(spot["temp_max_c"])


def test_capture_metadata_passes_through_verbatim(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-004 / no wire source for sequence or capture time / S1.

    The 13 captured IoT blocks encode no poll sequence or timestamp, so the
    observation must carry the caller's capture metadata unchanged and default
    the cell metadata to the telemetry metadata when not supplied separately.
    """
    observation = _decode_unit(wire_decode, capture, "MID")
    assert observation.wall_timestamp == _WALL_TIMESTAMP
    assert observation.captured_at_mono == _CAPTURED_AT_MONO
    assert observation.sequence == _SEQUENCE
    assert observation.cell_captured_at_mono == _CAPTURED_AT_MONO
    assert observation.cell_sequence == _SEQUENCE

    explicit = _decode_unit(
        wire_decode,
        capture,
        "MID",
        cell_captured_at_mono=_CELL_CAPTURED_AT_MONO,
        cell_sequence=_CELL_SEQUENCE,
        lifecycle=UnitLifecycle.INHIBITED,
    )
    assert explicit.cell_captured_at_mono == _CELL_CAPTURED_AT_MONO
    assert explicit.cell_sequence == _CELL_SEQUENCE
    assert explicit.lifecycle is UnitLifecycle.INHIBITED


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_scaling_families_hold_through_the_mapping_cross_checks(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-005 / field-mapping S3.1,S3.2 / S0.

    The mapping validated BMS current at 0.1 A/count, power registers in
    unscaled watts, and the dynamic-limit identity chargePowerLimit ==
    packV x chargeCurrentLimit.  A 10x scale error in any family breaks at
    least one of these identities on the decoded values.
    """
    bms = _blocks_of(capture, unit_key)[_BMS_BASE]
    observation = _decode_unit(wire_decode, capture, unit_key)
    signed = protocol_codec.decode_signed16

    # V x I = P within quantization tolerance (mapping section 3.1).
    product = observation.pack_voltage_v * observation.pack_current_a
    tolerance = max(1.0, 0.01 * abs(observation.battery_watts))
    assert abs(product - observation.battery_watts) <= tolerance

    # Dynamic-limit identity (mapping section 3.2) pins the 0.1 A current
    # limit family and the unscaled-W power limit family simultaneously.
    charge_current_limit = signed(bms[11]) * 0.1
    discharge_current_limit = signed(bms[12]) * 0.1
    assert observation.dynamic_charge_limit_w == pytest.approx(
        observation.pack_voltage_v * charge_current_limit, abs=0.5
    )
    assert observation.dynamic_discharge_limit_w == pytest.approx(
        observation.pack_voltage_v * discharge_current_limit, abs=0.5
    )

    # 0.1 A family spot check: raw word 2 on MID must decode to 0.2 A, not
    # 0.02 A; the same register as raw watts must stay 38 W, not 3.8 W.
    if unit_key == "MID":
        assert signed(bms[7]) == 2
        assert observation.pack_current_a == pytest.approx(0.2)
        assert observation.battery_watts == pytest.approx(38.0)


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_calibration_warnings_decode_without_blocking_quality(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-006 / field-mapping S2.2,S2.5,S2.9 / S1.

    All three units carry PCS and DCDC warning word 0 = 2 (bit 1: the two
    documented calibration-parameter warnings) and no fault words anywhere.
    The shipped fault catalog's code vocabulary is the contract.
    """
    observation = _decode_unit(wire_decode, capture, unit_key)

    assert observation.active_faults == frozenset()
    assert observation.active_warnings == frozenset({"PCS_Warning0_1", "DCDC_Warning0_1"})
    # Known benign warnings do not taint coherent telemetry.
    assert observation.safety_data_complete is True


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_system_overview_block_supplies_system_soc_when_polled(
    wire_decode: Any, capture: Any, followup: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-007 / PROTOCOL_EVIDENCE S6 (0x0100+17) / S1.

    Merged vector: the deployed IoT blocks plus the follow-up capture's
    system block.  The system controller's own SOC must come from the system
    block when it is polled, while pack measurements stay sourced from the
    BMS block per the mapping.
    """
    merged = _merged_blocks(capture, followup, unit_key)
    observation = _decode_unit(wire_decode, capture, unit_key, blocks=merged)
    bms = merged[_BMS_BASE]

    assert observation.system_soc_pct == pytest.approx(float(merged[_SYSTEM_BASE][17] & 0xFF))
    assert observation.system_soc_pct == pytest.approx(_SPOT[unit_key]["followup_system_soc"])
    assert observation.bms_soc_pct == pytest.approx(float(bms[9]))
    assert observation.pack_voltage_v == pytest.approx(protocol_codec.decode_signed16(bms[6]) * 0.1)


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_common_blocks_alone_fail_closed(
    wire_decode: Any, capture: Any, followup: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-008 / fail-closed on absent IoT telemetry / S0.

    The follow-up capture holds only the common blocks: no BMS block, no
    cells, no identity pair.  Only the system SOC may decode; everything the
    IoT plan sources must be honestly missing and the observation cannot
    qualify.
    """
    blocks = _blocks_of(followup, unit_key)
    observation = _decode_unit(wire_decode, capture, unit_key, blocks=blocks)

    assert observation.system_soc_pct == pytest.approx(_SPOT[unit_key]["followup_system_soc"])
    assert observation.bms_soc_pct is None
    assert observation.quality["bms_soc_pct"] is DataQuality.MISSING
    assert observation.cell_voltages_v == ()
    assert observation.quality["cell_voltages_v"] is DataQuality.MISSING
    assert observation.device_identity is None
    assert observation.safety_data_complete is False


def test_identity_mismatch_fails_closed_without_raising(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-009 / fail-closed identity / S0."""
    observation = _decode_unit(wire_decode, capture, "MID", expected_identity="byd-00000000")

    assert observation.device_identity == _SPOT["MID"]["identity"]
    assert observation.device_identity != "byd-00000000"
    _assert_fully_unqualified(observation)


def test_identity_block_missing_fails_closed(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-010 / fail-closed unverifiable identity / S0."""
    blocks = _blocks_of(capture, "MID")
    del blocks[_RTU_ID_BASE]

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert observation.device_identity is None
    _assert_fully_unqualified(observation)


def test_contradicted_identity_mirror_fails_closed(
    wire_decode: Any, capture: Any, followup: Any
) -> None:
    """T-UNIT-WIRE-011 / contradictory identity evidence / S0.

    PROTOCOL_EVIDENCE section 6 confirms the device-parameter block repeats
    the RTU ID at offsets 4-5 (low word first).  When both are polled and
    disagree, identity is contradicted and nothing may qualify.
    """
    merged = _merged_blocks(capture, followup, "MID")
    parameters = list(merged[_DEVICE_PARAMETERS_BASE])
    parameters[4] ^= 0x00FF
    merged[_DEVICE_PARAMETERS_BASE] = tuple(parameters)

    consistent = _decode_unit(
        wire_decode, capture, "MID", blocks=_merged_blocks(capture, followup, "MID")
    )
    contradicted = _decode_unit(wire_decode, capture, "MID", blocks=merged)

    assert consistent.device_identity == _SPOT["MID"]["identity"]
    assert contradicted.safety_data_complete is False


def test_profile_mismatch_fails_closed_honestly(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-012 / fail-closed profile / S0.

    A unit probed as IoT but configured for another profile must report the
    profile it actually served and never qualify.
    """
    observation = _decode_unit(wire_decode, capture, "MID", expected_profile=_LEGACY_PROFILE)

    assert observation.protocol_profile == _IOT_PROFILE
    _assert_fully_unqualified(observation)


def test_topology_mismatch_fails_closed(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-013 / fail-closed topology / S0.

    RHS is commissioned at 5 BICs (50 cells); expecting MID's 60 cells
    contradicts the probe and the served blocks.
    """
    observation = _decode_unit(wire_decode, capture, "RHS", expected_cell_count=60)

    _assert_fully_unqualified(observation)


def test_invalid_topology_fails_closed(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-014 / invalid layout probe / S0.

    A probe whose BIC count is zero reports an invalid topology; decoding it
    must not raise (the temperature-count expectation would be undecodable).
    """
    blocks = _blocks_of(capture, "MID")
    bms = list(blocks[_BMS_BASE])
    bms[5] = 0
    blocks[_BMS_BASE] = tuple(bms)
    probe = register_layout.detect_layout(blocks[_BMS_BASE][:7])
    assert probe.topology_valid is False

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks, probe=probe)

    _assert_fully_unqualified(observation)


def test_missing_bms_block_reports_missing_fields_honestly(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-015 / honest MISSING per source / S0."""
    blocks = _blocks_of(capture, "RHS")
    del blocks[_BMS_BASE]

    observation = _decode_unit(wire_decode, capture, "RHS", blocks=blocks)

    for field in (
        "system_soc_pct",
        "bms_soc_pct",
        "soh_pct",
        "battery_watts",
        "pack_voltage_v",
        "pack_current_a",
        "dynamic_charge_limit_w",
        "dynamic_discharge_limit_w",
    ):
        assert getattr(observation, field) is None
        assert observation.quality[field] is DataQuality.MISSING
    # Blocks that were served stay honest and good.
    assert observation.quality["cell_voltages_v"] is DataQuality.GOOD
    assert observation.quality["temperatures_c"] is DataQuality.GOOD
    assert observation.safety_data_complete is False


def test_missing_cell_block_reports_missing_cells_honestly(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-016 / honest MISSING per source / S0."""
    blocks = _blocks_of(capture, "MID")
    del blocks[_CELL_VOLTAGE_BASE]

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert observation.cell_voltages_v == ()
    assert observation.quality["cell_voltages_v"] is DataQuality.MISSING
    assert observation.cells_complete is False
    assert observation.quality["bms_soc_pct"] is DataQuality.GOOD
    assert observation.safety_data_complete is False


def test_missing_temperature_block_reports_missing_temperatures_honestly(
    wire_decode: Any, capture: Any
) -> None:
    """T-UNIT-WIRE-017 / honest MISSING per source / S0."""
    blocks = _blocks_of(capture, "LHS")
    del blocks[_CELL_TEMPERATURE_BASE]

    observation = _decode_unit(wire_decode, capture, "LHS", blocks=blocks)

    assert observation.temperatures_c == ()
    assert observation.quality["temperatures_c"] is DataQuality.MISSING
    assert observation.temperatures_complete is False
    assert observation.quality["cell_voltages_v"] is DataQuality.GOOD
    assert observation.safety_data_complete is False


@pytest.mark.parametrize("address", _REQUIRED_BLOCKS)
def test_any_missing_required_block_cannot_qualify(
    wire_decode: Any, capture: Any, address: int
) -> None:
    """T-UNIT-WIRE-018 / required-consumed-blocks rule / S0.

    Every block the observation consumes must be part of the poll for a
    qualifying decode: telemetry, cell, and temperature sources, the fault
    blocks (unread fault words must not be reported as an absence of
    faults), and the identity pair.
    """
    blocks = _blocks_of(capture, "MID")
    del blocks[address]

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert observation.safety_data_complete is False


def test_unconsumed_plan_blocks_do_not_influence_the_observation(
    wire_decode: Any, capture: Any
) -> None:
    """T-UNIT-WIRE-018A / decode consumes only its stated sources / S1.

    The deployed plan also reads PCS live/detail and DCDC live/detail blocks
    plus the ambiguous energy and balance blocks; the observation consumes
    none of them, so their absence must leave the decode bit-identical.
    """
    baseline = _decode_unit(wire_decode, capture, "MID")

    trimmed = _blocks_of(capture, "MID")
    for address in _UNCONSUMED_BLOCKS:
        del trimmed[address]

    assert _decode_unit(wire_decode, capture, "MID", blocks=trimmed) == baseline


def test_short_bms_block_fails_closed(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-019 / truncated block / S0.

    A BMS block shorter than the catalog's 31 registers cannot source the
    fields beyond the served words (here the dynamic limits at offsets 13/14).
    """
    blocks = _blocks_of(capture, "MID")
    blocks[_BMS_BASE] = blocks[_BMS_BASE][:12]

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert observation.dynamic_charge_limit_w is None
    assert observation.dynamic_discharge_limit_w is None
    assert observation.safety_data_complete is False


def test_wrong_cell_count_fails_closed(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-020 / wrong cell count / S0.

    Serving 50 cell words against a 6-BIC/60-cell expectation must not
    fabricate a complete cell picture.
    """
    blocks = _blocks_of(capture, "MID")
    blocks[_CELL_VOLTAGE_BASE] = blocks[_CELL_VOLTAGE_BASE][:50]

    observation = _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert len(observation.cell_voltages_v) != 60
    assert observation.cells_complete is False
    assert observation.quality["cell_voltages_v"] is not DataQuality.GOOD
    assert observation.safety_data_complete is False


@pytest.mark.parametrize(
    ("offset", "field"),
    [(9, "bms_soc_pct"), (10, "soh_pct")],
)
def test_out_of_range_percentage_fails_closed(
    wire_decode: Any, capture: Any, offset: int, field: str
) -> None:
    """T-UNIT-WIRE-021 / garbage registers / S0.

    A wire-plausible word that decodes outside the domain (0xFFFF percent)
    must fail closed to an absent value with BAD quality — never raise and
    never clamp.  The system SOC follows its fallback source.
    """
    blocks = _blocks_of(capture, "RHS")
    bms = list(blocks[_BMS_BASE])
    bms[offset] = 0xFFFF
    blocks[_BMS_BASE] = tuple(bms)

    observation = _decode_unit(wire_decode, capture, "RHS", blocks=blocks)

    assert getattr(observation, field) is None
    assert observation.quality[field] is DataQuality.BAD
    if field == "bms_soc_pct":
        assert observation.system_soc_pct is None
        assert observation.quality["system_soc_pct"] is DataQuality.BAD
    assert observation.safety_data_complete is False


@pytest.mark.parametrize(
    ("offset", "field"),
    [(13, "dynamic_charge_limit_w"), (14, "dynamic_discharge_limit_w")],
)
def test_negative_dynamic_limit_fails_closed(
    wire_decode: Any, capture: Any, offset: int, field: str
) -> None:
    """T-UNIT-WIRE-022 / garbage registers / S0.

    0x8000 decodes to -32768 W; the domain forbids negative limits, so the
    decode must fail closed instead of raising or wrapping.
    """
    blocks = _blocks_of(capture, "LHS")
    bms = list(blocks[_BMS_BASE])
    bms[offset] = 0x8000
    blocks[_BMS_BASE] = tuple(bms)

    observation = _decode_unit(wire_decode, capture, "LHS", blocks=blocks)

    assert getattr(observation, field) is None
    assert observation.quality[field] is DataQuality.BAD
    assert observation.safety_data_complete is False


def test_ambiguous_energy_and_balance_words_stay_out_of_the_observation(
    wire_decode: Any, capture: Any
) -> None:
    """T-UNIT-WIRE-023 / field-mapping S5 A-1,A-15 / S1.

    Energy-counter role labels and balance-word bits are ambiguous; the
    observation has no channel for them and their contents must not influence
    any decoded value.
    """
    baseline = _decode_unit(wire_decode, capture, "MID")

    scrambled = _blocks_of(capture, "MID")
    scrambled[_TOTALS_BASE] = tuple((word + 1) & 0xFFFF for word in scrambled[_TOTALS_BASE])
    scrambled[_BALANCE_BASE] = tuple(0x5555 for _ in scrambled[_BALANCE_BASE])
    scrambled_observation = _decode_unit(wire_decode, capture, "MID", blocks=scrambled)

    assert scrambled_observation == baseline
    ambiguous = {"energy", "balance"}
    assert not {
        field for field in Observation.model_fields if any(tag in field for tag in ambiguous)
    }


def test_decode_leaves_the_served_blocks_untouched(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-024 / decoder purity / S1."""
    blocks = _blocks_of(capture, "MID")
    snapshot = {address: words for address, words in blocks.items()}

    _decode_unit(wire_decode, capture, "MID", blocks=blocks)

    assert blocks == snapshot


# --- excess-solar advisory fields (PCS grid/load power) ------------------------
#
# API_CONTRACTS "Excess-solar accelerated charging (advisory)" and
# PROTOCOL_EVIDENCE section 4c: the per-pod CT power words live in the PCS
# live block — grid at 0x1000+17 and load at 0x1000+20, both int16 unscaled
# watts (SysControl.cs:500/503) — with the live-proven sign NEGATIVE = import,
# POSITIVE = export.  The fields are ADVISORY: honest per-field quality, but
# deliberately outside the safety-critical completeness set so an ordinary
# control decision never fails because a poll did not serve the PCS block.

_PCS_BLOCK_BASE = 0x1000
_PCS_GRID_POWER_OFFSET = 17
_PCS_LOAD_POWER_OFFSET = 20

_GRID_LOAD_SPOT: dict[str, dict[str, float]] = {
    "MID": {"grid_w": -1736.0, "load_w": 1701.0},
    "RHS": {"grid_w": -37.0, "load_w": 1063.0},
    "LHS": {"grid_w": -48.0, "load_w": 1781.0},
}


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_pcs_block_decodes_signed_grid_and_load_power(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-025 / PROTOCOL_EVIDENCE 4c (0x1000+17/+20) / S1.

    The deployed capture carries the PCS live block, so the observation must
    carry its two power words with honest GOOD quality.
    """
    pcs = _blocks_of(capture, unit_key)[_PCS_BLOCK_BASE]
    observation = _decode_unit(wire_decode, capture, unit_key)

    signed = protocol_codec.decode_signed16
    assert observation.grid_power_w == pytest.approx(float(signed(pcs[_PCS_GRID_POWER_OFFSET]))), (
        "grid power must decode from the PCS block at 0x1000+17, signed, unscaled watts"
    )
    assert observation.load_power_w == pytest.approx(float(signed(pcs[_PCS_LOAD_POWER_OFFSET]))), (
        "load power must decode from the PCS block at 0x1000+20, signed, unscaled watts"
    )
    assert observation.quality["grid_power_w"] is DataQuality.GOOD
    assert observation.quality["load_power_w"] is DataQuality.GOOD


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_grid_power_sign_follows_the_live_import_export_convention(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-026 / live-proven sign convention (PROTOCOL_EVIDENCE 4b/4c) / S0.

    Every captured unit was importing at capture time, so the decoded CT power
    must be negative exactly as the wire read it — never sign-flipped.
    """
    observation = _decode_unit(wire_decode, capture, unit_key)

    assert observation.grid_power_w == pytest.approx(_GRID_LOAD_SPOT[unit_key]["grid_w"])
    assert observation.load_power_w == pytest.approx(_GRID_LOAD_SPOT[unit_key]["load_w"])
    assert observation.grid_power_w < 0, "captured units were importing: negative = import"


@pytest.mark.parametrize("unit_key", _UNIT_KEYS)
def test_missing_pcs_block_leaves_grid_and_load_missing_but_qualifying(
    wire_decode: Any, capture: Any, unit_key: str
) -> None:
    """T-UNIT-WIRE-027 / advisory fields outside the safety set / S0.

    A poll without the PCS block must report grid and load honestly MISSING
    while the observation STILL qualifies for ordinary control: export-bounded
    charging is gated by its own fail-closed bound (which collapses to 0
    without grid evidence), never by the general safety completeness set.
    """
    blocks = _blocks_of(capture, unit_key)
    del blocks[_PCS_BLOCK_BASE]
    observation = _decode_unit(wire_decode, capture, unit_key, blocks=blocks)

    assert observation.grid_power_w is None
    assert observation.load_power_w is None
    assert observation.quality["grid_power_w"] is DataQuality.MISSING
    assert observation.quality["load_power_w"] is DataQuality.MISSING
    assert observation.safety_data_complete is True, (
        "advisory fields must not enter the safety-critical completeness set"
    )


def test_quality_map_carries_the_advisory_field_set(wire_decode: Any, capture: Any) -> None:
    """T-UNIT-WIRE-028 / ADVISORY_QUALITY_FIELDS shape / S1.

    The decoder always emits the twelve-key quality shape (ten safety-critical
    plus the two advisory fields); the domain accepts exactly the ten-field or
    the twelve-field shape and nothing else.
    """
    observation = _decode_unit(wire_decode, capture, "MID")

    assert set(observation.quality) == set(Observation.QUALITY_FIELDS) | {
        "grid_power_w",
        "load_power_w",
    }
    assert hasattr(Observation, "ADVISORY_QUALITY_FIELDS")
