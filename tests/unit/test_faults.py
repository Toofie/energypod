"""Protocol fault-word and lifecycle contracts derived independently from vendor behavior."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


class _MissingContract:
    def __init__(self, message: str) -> None:
        self.message = message

    def __getattr__(self, name: str) -> Any:
        pytest.fail(self.message, pytrace=False)


@pytest.fixture(scope="module")
def faults() -> Any:
    try:
        return importlib.import_module("energypod.adapters.modbus.faults")
    except (ImportError, AttributeError) as error:
        return _MissingContract(f"Fault contract is not implemented: {error}")


@pytest.mark.parametrize(
    ("block_name", "length", "warnings", "fault_words"),
    [
        ("IOT_PCS", 22, tuple(range(0, 4)), tuple(range(16, 22))),
        ("LEGACY_PCS", 60, tuple(range(0, 4)), tuple(range(16, 22))),
        ("IOT_DCDC", 22, tuple(range(0, 4)), tuple(range(16, 22))),
        ("LEGACY_DCDC", 22, tuple(range(0, 4)), tuple(range(10, 16))),
        ("LEGACY_BECU", 20, tuple(range(0, 2)), tuple(range(16, 20))),
    ],
)
def test_fault_block_offsets_match_vendor_array_expressions(
    faults: Any,
    block_name: str,
    length: int,
    warnings: tuple[int, ...],
    fault_words: tuple[int, ...],
) -> None:
    """T-UNIT-FAULT-001 / V-FAULTS / S1."""
    spec = faults.FAULT_BLOCK_LAYOUTS[getattr(faults.FaultBlock, block_name)]
    assert spec.register_count == length
    assert tuple(sorted(spec.warning_offsets.values())) == warnings
    assert tuple(sorted(spec.fault_offsets.values())) == fault_words


def test_iot_and_legacy_bms_fault_offsets_preserve_stack_and_becu_words(faults: Any) -> None:
    """T-UNIT-FAULT-002 / V-FAULTS / S1."""
    iot = faults.FAULT_BLOCK_LAYOUTS[faults.FaultBlock.IOT_BMS]
    legacy = faults.FAULT_BLOCK_LAYOUTS[faults.FaultBlock.LEGACY_BMS]

    assert iot.register_count == 22
    assert iot.warning_offsets == {
        "Stack_Warning0": 0,
        "Becu0_Warning0": 1,
        "Becu0_Warning1": 2,
    }
    assert iot.fault_offsets == {
        "Stack_Fault0": 16,
        "Stack_Fault1": 17,
        "Becu0_Fault0": 18,
        "Becu0_Fault1": 19,
        "Becu0_Fault2": 20,
        "Becu0_Fault3": 21,
    }

    assert legacy.register_count == 34
    assert legacy.warning_offsets["Stack_Warning0"] == 0
    assert legacy.warning_offsets["Becu3_Warning1"] == 8
    assert legacy.fault_offsets["Stack_Fault0"] == 16
    assert legacy.fault_offsets["Stack_Fault1"] == 17
    assert legacy.fault_offsets["Becu3_Fault3"] == 33


def test_bms_fault_zero_map_is_not_shifted(faults: Any) -> None:
    """T-UNIT-FAULT-003 / O-FAULT-MAP / S0."""
    expected = {
        0: "No Battery BECU Available",
        1: "Start Fail",
        2: "Stop Fail",
        3: "PCS CAN disconnected",
        4: "DCDC CAN disconnected",
    }
    definition = faults.FAULT_CATALOG["Stack_Fault0"]

    assert {bit: definition.bit(bit).description for bit in expected} == expected
    decoded = faults.decode_fault_word("Stack_Fault0", 0b1_0001)
    assert [(signal.bit, signal.code) for signal in decoded] == [
        (0, "Stack_Fault0_0"),
        (4, "Stack_Fault0_4"),
    ]


def test_calibration_warning_bits_have_distinct_pcs_and_dcdc_labels(faults: Any) -> None:
    """T-UNIT-FAULT-004 / O-FAULT-MAP,O-LOGS / S0."""
    pcs = faults.FAULT_CATALOG["PCS_Warning0"].bit(1)
    dcdc = faults.FAULT_CATALOG["DCDC_Warning0"].bit(1)

    assert pcs.description == "EE Calibration Parameter Out of Range"
    assert dcdc.description == "EEPROM Calibration Parameter Out of Range"
    assert pcs.known is dcdc.known is True


def test_calibration_labels_do_not_invent_severity_remediation_or_actuation_policy(
    faults: Any,
) -> None:
    """T-UNIT-FAULT-005 / UNC-CALIBRATION / S0."""
    for prefix in ("PCS_Warning0", "DCDC_Warning0"):
        calibration = faults.FAULT_CATALOG[prefix].bit(1)
        assert calibration.actuation_policy_eligible is False
        assert calibration.severity.value == "unknown"
        assert calibration.remediation is None


def test_pcs_fault_one_sampling_bits_end_at_thirteen(faults: Any) -> None:
    """T-UNIT-FAULT-006 / O-FAULT-MAP / S0."""
    definition = faults.FAULT_CATALOG["PCS_Fault1"]

    assert all(definition.bit(bit).known for bit in range(4, 14))
    assert definition.bit(14).reserved is True
    assert definition.bit(15).reserved is True


@pytest.mark.parametrize(
    ("prefix", "known_bits"),
    [
        ("PCS_Fault0", {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15}),
        ("PCS_Fault1", set(range(14))),
        ("PCS_Fault3", {0, 1, 2, 3, 4}),
        ("DCDC_Fault0", {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15}),
        ("DCDC_Fault1", {0, 1, 4, 5, 6, 7, 8, 9, 10, 11}),
        ("Becu0_Fault0", {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15}),
        ("Becu0_Fault2", set(range(16))),
        ("Becu0_Fault3", set(range(16))),
    ],
)
def test_full_non_reserved_fault_bit_sets_match_workbook(
    faults: Any, prefix: str, known_bits: set[int]
) -> None:
    """T-UNIT-FAULT-016 / O-FAULT-MAP / S0."""
    definition = faults.FAULT_CATALOG[prefix]

    assert {bit for bit in range(16) if definition.bit(bit).known} == known_bits
    assert {bit for bit in range(16) if definition.bit(bit).reserved} == set(range(16)) - known_bits


def test_bms_warning_remote_dispatch_and_communications_bits(faults: Any) -> None:
    """T-UNIT-FAULT-007 / O-FAULT-MAP / S1."""
    definition = faults.FAULT_CATALOG["Stack_Warning0"]
    assert definition.bit(11).description == "No Remote Dispatch"
    assert definition.bit(12).description == "No TCP Connection"
    assert definition.bit(13).description == "Electricity Meter Communication Disconnected"
    assert definition.bit(14).description == "DRED Communication Disconnected"


def test_decoder_preserves_reserved_and_unknown_active_bits(faults: Any) -> None:
    """T-UNIT-FAULT-008 / INV-FAULT-001 / S1."""
    decoded = faults.decode_fault_word("PCS_Fault1", 0xC010)

    assert [signal.bit for signal in decoded] == [4, 14, 15]
    assert decoded[0].known is True
    assert decoded[1].known is False and decoded[1].reserved is True
    assert decoded[2].known is False and decoded[2].reserved is True
    assert all(signal.raw_word == 0xC010 for signal in decoded)


@pytest.mark.parametrize("raw_word", [-1, 65536, True, 1.5, "1", None])
def test_fault_decoder_rejects_non_register_words(faults: Any, raw_word: Any) -> None:
    """T-UNIT-FAULT-009 / INV-RANGE-004 / S1."""
    with pytest.raises(faults.FaultDecodeError):
        faults.decode_fault_word("PCS_Fault0", raw_word)


def test_fault_block_extraction_requires_the_exact_vendor_length(faults: Any) -> None:
    """T-UNIT-FAULT-010 / INV-DECODE-003 / S1."""
    registers = list(range(22))
    extracted = faults.extract_fault_block(faults.FaultBlock.IOT_DCDC, registers)

    assert extracted["DCDC_Warning0"] == 0
    assert extracted["DCDC_Warning3"] == 3
    assert extracted["DCDC_Fault0"] == 16
    assert extracted["DCDC_Fault5"] == 21
    with pytest.raises(faults.FaultDecodeError):
        faults.extract_fault_block(faults.FaultBlock.IOT_DCDC, registers[:-1])


def test_fault_tracker_emits_happen_once_and_disappear_once(faults: Any) -> None:
    """T-UNIT-FAULT-011 / V-FAULTS / S1."""
    tracker = faults.FaultTracker()
    observed_at = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)

    happened = tracker.observe(
        prefix="PCS_Warning0", raw_word=0x0002, observed_at=observed_at, sequence=1
    )
    repeated = tracker.observe(
        prefix="PCS_Warning0",
        raw_word=0x0002,
        observed_at=observed_at + timedelta(seconds=1),
        sequence=2,
    )
    disappeared = tracker.observe(
        prefix="PCS_Warning0",
        raw_word=0,
        observed_at=observed_at + timedelta(seconds=2),
        sequence=3,
    )

    assert [(event.code, event.transition.value) for event in happened] == [
        ("PCS_Warning0_1", "happen")
    ]
    assert repeated == ()
    assert [(event.code, event.transition.value) for event in disappeared] == [
        ("PCS_Warning0_1", "disappear")
    ]
    assert tracker.active == ()
    assert happened[0].observed_at == observed_at
    assert happened[0].sequence == 1


def test_fault_tracker_rejects_non_monotonic_observation_sequences(faults: Any) -> None:
    """T-UNIT-FAULT-012 / INV-FAULT-002 / S1."""
    tracker = faults.FaultTracker()
    observed_at = datetime(2026, 8, 21, tzinfo=UTC)
    tracker.observe(prefix="Stack_Fault0", raw_word=1, observed_at=observed_at, sequence=10)

    with pytest.raises(faults.FaultSequenceError):
        tracker.observe(prefix="Stack_Fault0", raw_word=0, observed_at=observed_at, sequence=10)


def test_fault_tracker_accepts_same_observation_sequence_for_distinct_words(faults: Any) -> None:
    """T-UNIT-FAULT-013 / INV-FAULT-003 / S1.

    One register-block observation contains several fault words. Sequence monotonicity is therefore
    per prefix, not a global rule that would reject the second word from the same observation.
    """
    tracker = faults.FaultTracker()
    observed_at = datetime(2026, 8, 21, tzinfo=UTC)

    first = tracker.observe(prefix="Stack_Fault0", raw_word=1, observed_at=observed_at, sequence=7)
    second = tracker.observe(prefix="Stack_Fault1", raw_word=2, observed_at=observed_at, sequence=7)

    assert [event.code for event in first] == ["Stack_Fault0_0"]
    assert [event.code for event in second] == ["Stack_Fault1_1"]


def test_fault_tracker_emits_all_transitions_in_stable_bit_order(faults: Any) -> None:
    """T-UNIT-FAULT-014 / INV-FAULT-004 / S1."""
    tracker = faults.FaultTracker()
    observed_at = datetime(2026, 8, 21, tzinfo=UTC)

    happened = tracker.observe(
        prefix="PCS_Fault1", raw_word=0xC011, observed_at=observed_at, sequence=1
    )
    changed = tracker.observe(
        prefix="PCS_Fault1", raw_word=0x4002, observed_at=observed_at, sequence=2
    )

    assert [(event.bit, event.transition.value) for event in happened] == [
        (0, "happen"),
        (4, "happen"),
        (14, "happen"),
        (15, "happen"),
    ]
    assert [(event.bit, event.transition.value) for event in changed] == [
        (0, "disappear"),
        (1, "happen"),
        (4, "disappear"),
        (15, "disappear"),
    ]


@pytest.mark.parametrize("prefix", ["", "Unknown_Fault0", "PCS_Fault99", None])
def test_fault_decoder_rejects_unknown_word_identity(faults: Any, prefix: Any) -> None:
    """T-UNIT-FAULT-015 / INV-FAULT-005 / S1."""
    with pytest.raises(faults.FaultDecodeError):
        faults.decode_fault_word(prefix, 1)
