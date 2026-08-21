"""Evidence-backed contracts for register blocks, layout probing, and topology gating."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from typing import Any

import pytest


class _MissingContract:
    def __init__(self, message: str) -> None:
        self.message = message

    def __getattr__(self, name: str) -> Any:
        pytest.fail(self.message, pytrace=False)


@pytest.fixture(scope="module")
def layout() -> Any:
    try:
        return importlib.import_module("energypod.adapters.modbus.register_layout")
    except (ImportError, AttributeError) as error:
        return _MissingContract(f"Register-layout contract is not implemented: {error}")


def _pairs(blocks: Iterable[Any]) -> set[tuple[int, int]]:
    materialized = tuple(blocks)
    assert all(block.function_code == 3 for block in materialized)
    return {(block.address, block.count) for block in materialized}


def test_layout_probe_and_common_read_blocks_match_literal_vendor_calls(layout: Any) -> None:
    """T-UNIT-LAYOUT-001 / V-LAYOUT,V-SYS,V-WRITE / S1."""
    catalog = layout.RegisterCatalog()

    assert (catalog.layout_probe.address, catalog.layout_probe.count) == (0x5000, 7)
    assert catalog.layout_probe.function_code == 3
    assert _pairs(catalog.common_reads) == {
        (0x0100, 61),
        (0x8100, 1),
        (0x8139, 1),
        (0x8106, 2),
        (0x8102, 56),
    }


def test_iot_read_plan_matches_vendor_addresses_and_six_bic_counts(layout: Any) -> None:
    """T-UNIT-LAYOUT-002 / V-IOT / S1."""
    blocks = layout.RegisterCatalog().iot_reads(bic_count=6)

    assert _pairs(blocks) == {
        (0x1000, 21),
        (0x1040, 22),
        (0x1060, 32),
        (0x2000, 13),
        (0x2040, 22),
        (0x2060, 19),
        (0x4101, 12),
        (0x5000, 31),
        (0x5040, 22),
        (0x5200, 60),
        (0x523C, 18),
        (0x524E, 6),
    }


def test_legacy_read_plan_matches_fixed_and_per_becu_vendor_calls(layout: Any) -> None:
    """T-UNIT-LAYOUT-003 / V-LEGACY / S1."""
    blocks = layout.RegisterCatalog().legacy_reads(bic_count=6, becu_count=2)
    expected = {
        (0x1000, 60),
        (0x1040, 60),
        (0x3000, 35),
        (0x3040, 22),
        (0x5000, 21),
        (0x5040, 34),
    }
    for index in range(2):
        stride = 0x0800 * index
        expected.update(
            {
                (0x6000 + stride, 35),
                (0x6040 + stride, 20),
                (0x6200 + stride, 60),
                (0x6300 + stride, 60),
                (0x6400 + stride, 6),
                (0x6500 + stride, 12),
            }
        )

    assert _pairs(blocks) == expected


def test_production_write_catalog_exposes_only_dormant_pq_objective(layout: Any) -> None:
    """T-UNIT-LAYOUT-004 / V-WRITE / S0.

    Vendor-code evidence for a maintenance write does not justify putting that write in the
    production adapter.  The production catalog is deliberately narrower than the evidence ledger.
    """
    writes = layout.RegisterCatalog().write_blocks

    assert {name: (spec.address, spec.count) for name, spec in writes.items()} == {
        "pq_objective": (0x0200, 3)
    }
    assert all(spec.function_code == 16 for spec in writes.values())
    assert all(not spec.live_actuation_eligible for spec in writes.values())
    forbidden_maintenance_addresses = {
        0x8000,
        0x8001,
        0x8002,
        0x8008,
        0x8018,
        0x8034,
        0x8035,
        0x8036,
        0x8037,
    }
    assert forbidden_maintenance_addresses.isdisjoint(spec.address for spec in writes.values())


@pytest.mark.parametrize(
    ("first_word", "expected_layout"),
    [(0, "LEGACY"), (10, "LEGACY"), (11, "IOT"), (0xFFFF, "IOT")],
)
def test_layout_discriminator_is_strictly_greater_than_ten(
    layout: Any, first_word: int, expected_layout: str
) -> None:
    """T-UNIT-LAYOUT-005 / V-LAYOUT / S1."""
    registers = [first_word, 0, 0x0005, 0, 0x0003, 6, 6]
    result = layout.detect_layout(registers)

    assert result.layout is getattr(layout.ProtocolLayout, expected_layout)


def test_iot_probe_uses_offsets_four_and_five_and_counts_mask_bits(layout: Any) -> None:
    """T-UNIT-LAYOUT-006 / V-LAYOUT / S1."""
    result = layout.detect_layout([0x0100, 0, 0x00FF, 0, 0x0105, 6, 99])

    assert result.layout is layout.ProtocolLayout.IOT
    assert result.enable_mask == 0x05  # vendor casts the register to byte
    assert result.bic_count == 6
    assert result.becu_count == 2
    assert result.live_actuation_eligible is False


def test_legacy_probe_uses_offsets_two_and_six(layout: Any) -> None:
    """T-UNIT-LAYOUT-007 / V-LAYOUT / S1."""
    result = layout.detect_layout([10, 0, 0x0008, 0, 0x00FF, 99, 6])

    assert result.layout is layout.ProtocolLayout.LEGACY
    assert result.enable_mask == 0x08
    assert result.bic_count == 6
    assert result.becu_count == 1
    assert result.live_actuation_eligible is False


def test_vendor_forces_detected_becu_count_to_at_least_one(layout: Any) -> None:
    """T-UNIT-LAYOUT-008 / V-LAYOUT / S1."""
    result = layout.detect_layout([11, 0, 0, 0, 0, 6, 0])

    assert result.enable_mask == 0
    assert result.becu_count == 1
    assert result.live_actuation_eligible is False


def test_vendor_negative_bic_normalization_remains_observe_only(layout: Any) -> None:
    """T-UNIT-LAYOUT-009 / V-LAYOUT / S0."""
    result = layout.detect_layout([11, 0, 0, 0, 0, 0xFFFF, 0])

    assert result.bic_count == 0
    assert result.topology_valid is False
    assert result.live_actuation_eligible is False


@pytest.mark.parametrize(
    "registers",
    [
        [],
        [11] * 6,
        [11] * 8,
        [True, 0, 0, 0, 0, 6, 0],
        [11, 0, 0, 0, 0, -1, 0],
        [11, 0, 0, 0, 0, 65536, 0],
    ],
)
def test_layout_probe_requires_exactly_seven_valid_registers(
    layout: Any, registers: list[int]
) -> None:
    """T-UNIT-LAYOUT-010 / INV-DECODE-002 / S1."""
    with pytest.raises(layout.RegisterLayoutError):
        layout.detect_layout(registers)


def test_six_bic_iot_map_is_contiguous_and_non_overlapping(layout: Any) -> None:
    """T-UNIT-LAYOUT-011 / V-IOT / S1."""
    topology = layout.assess_iot_cell_map(bic_count=6, observed_cell_count=60)

    assert topology.vendor_voltage_count == 60
    assert topology.temperature_count == 18
    assert topology.balance_count == 6
    assert topology.voltage_end_exclusive == 0x523C
    assert topology.temperature_end_exclusive == 0x524E
    assert topology.map_non_overlapping is True
    assert topology.observed_count_matches_vendor_formula is True
    assert topology.live_actuation_eligible is False


def test_observed_59_cells_is_not_promoted_to_a_protocol_contract(layout: Any) -> None:
    """T-UNIT-LAYOUT-012 / P-MAP / S0: historical 59-cell reads remain unproven."""
    topology = layout.assess_iot_cell_map(bic_count=6, observed_cell_count=59)

    assert topology.vendor_voltage_count == 60
    assert topology.observed_count_matches_vendor_formula is False
    assert topology.live_actuation_eligible is False
    assert "cell_count" in topology.ineligibility_reasons


def test_iot_cell_map_above_six_bics_is_detected_as_overlapping(layout: Any) -> None:
    """T-UNIT-LAYOUT-013 / V-IOT / S0: ambiguous registers cannot authorize writes."""
    topology = layout.assess_iot_cell_map(bic_count=7, observed_cell_count=70)

    assert topology.vendor_voltage_count == 70
    assert topology.voltage_end_exclusive > 0x523C
    assert topology.temperature_end_exclusive > 0x524E
    assert topology.map_non_overlapping is False
    assert topology.live_actuation_eligible is False
    assert "overlapping_cell_map" in topology.ineligibility_reasons


@pytest.mark.parametrize("bic_count", [-1, 65536, True, 6.0, "6"])
def test_cell_map_assessment_rejects_non_register_or_untyped_bic_counts(
    layout: Any, bic_count: Any
) -> None:
    """T-UNIT-LAYOUT-015 / INV-DECODE-004 / S0."""
    with pytest.raises(layout.RegisterLayoutError):
        layout.assess_iot_cell_map(bic_count=bic_count, observed_cell_count=60)


@pytest.mark.parametrize("observed_cell_count", [-1, 65536, True, 60.0, "60"])
def test_cell_map_assessment_rejects_impossible_or_untyped_observed_counts(
    layout: Any, observed_cell_count: Any
) -> None:
    """T-UNIT-LAYOUT-016 / INV-DECODE-005 / S0."""
    with pytest.raises(layout.RegisterLayoutError):
        layout.assess_iot_cell_map(bic_count=6, observed_cell_count=observed_cell_count)


@pytest.mark.parametrize(("bic_count", "observed_count"), [(0, 0), (7, 70), (101, 100)])
def test_wire_valid_but_unsafe_topologies_remain_observable_and_ineligible(
    layout: Any, bic_count: int, observed_count: int
) -> None:
    """T-UNIT-LAYOUT-017 / INV-EVIDENCE-002 / S0."""
    topology = layout.assess_iot_cell_map(bic_count=bic_count, observed_cell_count=observed_count)

    assert topology.live_actuation_eligible is False
    assert topology.ineligibility_reasons


def test_evidence_ledger_keeps_unproven_semantics_out_of_actuation(layout: Any) -> None:
    """T-UNIT-LAYOUT-014 / INV-EVIDENCE-001 / S0."""
    claims = layout.RegisterCatalog().claims
    expected_status = {
        "waveshare_rtu_over_tcp_framing": "corroborated_operationally",
        "device_id_4": "confirmed_vendor",
        "tcp_device_id_ignored": "unknown",
        "full_pq_frame": "confirmed_vendor",
        "active_power_sign": "corroborated_operationally",
        "reactive_power_semantics": "unknown",
        "firmware_lease_duration": "unknown",
        "firmware_fallback": "unknown",
        "active_only_write": "corroborated_operationally",
        "pcs_run_mode_precondition": "unknown",
        "deployed_iot_layout": "assumed",
        "deployed_cell_count_59": "assumed",
        "iot_cell_layout_above_six_bics": "unknown",
        "debug_mode_functions": "unknown",
        "cell_imbalance_threshold": "unknown",
    }

    observed_status = {name: claim.evidence_status.value for name, claim in claims.items()}
    assert observed_status.items() >= expected_status.items()
    for name in expected_status:
        assert claims[name].live_actuation_eligible is False

    implementation_eligible = {"full_pq_frame", "waveshare_rtu_over_tcp_framing"}
    for name in implementation_eligible:
        assert claims[name].implementation_eligible is True
    for name in expected_status.keys() - implementation_eligible:
        assert claims[name].implementation_eligible is False
