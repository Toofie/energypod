"""Fault-word locations, conservative descriptions, and transition tracking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType


class FaultDecodeError(ValueError):
    """Raised for an unknown word identity or malformed register value."""


class FaultSequenceError(ValueError):
    """Raised when observations for one fault word do not advance."""


class FaultBlock(Enum):
    IOT_PCS = "iot_pcs"
    LEGACY_PCS = "legacy_pcs"
    IOT_DCDC = "iot_dcdc"
    LEGACY_DCDC = "legacy_dcdc"
    IOT_BMS = "iot_bms"
    LEGACY_BMS = "legacy_bms"
    LEGACY_BECU = "legacy_becu"


class FaultSeverity(Enum):
    UNKNOWN = "unknown"


class FaultTransition(Enum):
    HAPPEN = "happen"
    DISAPPEAR = "disappear"


@dataclass(frozen=True, slots=True)
class FaultBlockLayout:
    register_count: int
    warning_offsets: Mapping[str, int]
    fault_offsets: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class FaultBitDefinition:
    bit: int
    description: str
    known: bool
    reserved: bool
    severity: FaultSeverity = FaultSeverity.UNKNOWN
    remediation: str | None = None
    actuation_policy_eligible: bool = False


@dataclass(frozen=True, slots=True)
class FaultWordDefinition:
    prefix: str
    bits: tuple[FaultBitDefinition, ...]

    def bit(self, bit: int) -> FaultBitDefinition:
        if type(bit) is not int or not 0 <= bit < 16:
            raise FaultDecodeError("fault bit must be between 0 and 15")
        return self.bits[bit]


@dataclass(frozen=True, slots=True)
class FaultSignal:
    prefix: str
    bit: int
    code: str
    description: str
    known: bool
    reserved: bool
    raw_word: int


@dataclass(frozen=True, slots=True)
class FaultEvent:
    prefix: str
    bit: int
    code: str
    description: str
    known: bool
    reserved: bool
    raw_word: int
    transition: FaultTransition
    observed_at: datetime
    sequence: int


def _offsets(prefix: str, start: int, count: int) -> Mapping[str, int]:
    return MappingProxyType({f"{prefix}{index}": start + index for index in range(count)})


def _combined(*mappings: Mapping[str, int]) -> Mapping[str, int]:
    values: dict[str, int] = {}
    for mapping in mappings:
        values.update(mapping)
    return MappingProxyType(values)


FAULT_BLOCK_LAYOUTS: Mapping[FaultBlock, FaultBlockLayout] = MappingProxyType(
    {
        FaultBlock.IOT_PCS: FaultBlockLayout(
            22, _offsets("PCS_Warning", 0, 4), _offsets("PCS_Fault", 16, 6)
        ),
        FaultBlock.LEGACY_PCS: FaultBlockLayout(
            60, _offsets("PCS_Warning", 0, 4), _offsets("PCS_Fault", 16, 6)
        ),
        FaultBlock.IOT_DCDC: FaultBlockLayout(
            22, _offsets("DCDC_Warning", 0, 4), _offsets("DCDC_Fault", 16, 6)
        ),
        FaultBlock.LEGACY_DCDC: FaultBlockLayout(
            22, _offsets("DCDC_Warning", 0, 4), _offsets("DCDC_Fault", 10, 6)
        ),
        FaultBlock.IOT_BMS: FaultBlockLayout(
            22,
            MappingProxyType({"Stack_Warning0": 0, "Becu0_Warning0": 1, "Becu0_Warning1": 2}),
            MappingProxyType(
                {
                    "Stack_Fault0": 16,
                    "Stack_Fault1": 17,
                    "Becu0_Fault0": 18,
                    "Becu0_Fault1": 19,
                    "Becu0_Fault2": 20,
                    "Becu0_Fault3": 21,
                }
            ),
        ),
        FaultBlock.LEGACY_BMS: FaultBlockLayout(
            34,
            _combined(
                MappingProxyType({"Stack_Warning0": 0}),
                MappingProxyType(
                    {
                        f"Becu{becu}_Warning{word}": 1 + becu * 2 + word
                        for becu in range(4)
                        for word in range(2)
                    }
                ),
            ),
            _combined(
                MappingProxyType({"Stack_Fault0": 16, "Stack_Fault1": 17}),
                MappingProxyType(
                    {
                        f"Becu{becu}_Fault{word}": 18 + becu * 4 + word
                        for becu in range(4)
                        for word in range(4)
                    }
                ),
            ),
        ),
        FaultBlock.LEGACY_BECU: FaultBlockLayout(
            20, _offsets("Becu0_Warning", 0, 2), _offsets("Becu0_Fault", 16, 4)
        ),
    }
)


_KNOWN_BITS: dict[str, set[int]] = {
    "PCS_Fault0": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15},
    "PCS_Fault1": set(range(14)),
    "PCS_Fault3": {0, 1, 2, 3, 4},
    "DCDC_Fault0": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15},
    "DCDC_Fault1": {0, 1, 4, 5, 6, 7, 8, 9, 10, 11},
    "Becu0_Fault0": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15},
    "Becu0_Fault2": set(range(16)),
    "Becu0_Fault3": set(range(16)),
    "Stack_Fault0": {0, 1, 2, 3, 4},
    "Stack_Fault1": set(range(16)),
    "Stack_Warning0": {11, 12, 13, 14},
    "PCS_Warning0": {0, 1},
    "DCDC_Warning0": {0, 1},
}

_DESCRIPTIONS: dict[tuple[str, int], str] = {
    ("Stack_Fault0", 0): "No Battery BECU Available",
    ("Stack_Fault0", 1): "Start Fail",
    ("Stack_Fault0", 2): "Stop Fail",
    ("Stack_Fault0", 3): "PCS CAN disconnected",
    ("Stack_Fault0", 4): "DCDC CAN disconnected",
    ("Stack_Warning0", 11): "No Remote Dispatch",
    ("Stack_Warning0", 12): "No TCP Connection",
    ("Stack_Warning0", 13): "Electricity Meter Communication Disconnected",
    ("Stack_Warning0", 14): "DRED Communication Disconnected",
    ("PCS_Warning0", 0): "EE Configuration Parameter Out of Range",
    ("PCS_Warning0", 1): "EE Calibration Parameter Out of Range",
    ("DCDC_Warning0", 0): "EEPROM Configuration Parameter Out of Range",
    ("DCDC_Warning0", 1): "EEPROM Calibration Parameter Out of Range",
}


def _definition(prefix: str, known_bits: set[int] | None = None) -> FaultWordDefinition:
    known = known_bits or set()
    return FaultWordDefinition(
        prefix,
        tuple(
            FaultBitDefinition(
                bit=bit,
                description=_DESCRIPTIONS.get(
                    (prefix, bit), f"{prefix} bit {bit}" if bit in known else "Reserved"
                ),
                known=bit in known,
                reserved=bit not in known,
            )
            for bit in range(16)
        ),
    )


_all_prefixes = {
    prefix
    for block in FAULT_BLOCK_LAYOUTS.values()
    for prefix in (*block.warning_offsets.keys(), *block.fault_offsets.keys())
}
FAULT_CATALOG: Mapping[str, FaultWordDefinition] = MappingProxyType(
    {prefix: _definition(prefix, _KNOWN_BITS.get(prefix)) for prefix in sorted(_all_prefixes)}
)


def _word(raw_word: object) -> int:
    if type(raw_word) is not int or not 0 <= raw_word <= 0xFFFF:
        raise FaultDecodeError("fault word must be an unsigned 16-bit integer")
    return raw_word


def decode_fault_word(prefix: str, raw_word: int) -> tuple[FaultSignal, ...]:
    if type(prefix) is not str or prefix not in FAULT_CATALOG:
        raise FaultDecodeError("unknown fault-word identity")
    raw = _word(raw_word)
    definition = FAULT_CATALOG[prefix]
    return tuple(
        FaultSignal(
            prefix=prefix,
            bit=bit,
            code=f"{prefix}_{bit}",
            description=definition.bit(bit).description,
            known=definition.bit(bit).known,
            reserved=definition.bit(bit).reserved,
            raw_word=raw,
        )
        for bit in range(16)
        if raw & (1 << bit)
    )


def extract_fault_block(block: FaultBlock, registers: object) -> dict[str, int]:
    if not isinstance(block, FaultBlock):
        raise FaultDecodeError("unknown fault block")
    spec = FAULT_BLOCK_LAYOUTS[block]
    if not isinstance(registers, tuple | list) or len(registers) != spec.register_count:
        raise FaultDecodeError(f"{block.value} requires exactly {spec.register_count} registers")
    values = tuple(_word(value) for value in registers)
    offsets = {**spec.warning_offsets, **spec.fault_offsets}
    return {prefix: values[offset] for prefix, offset in offsets.items()}


class FaultTracker:
    """Tracks transitions independently for each fault-word prefix."""

    def __init__(self) -> None:
        self._words: dict[str, int] = {}
        self._sequences: dict[str, int] = {}

    @property
    def active(self) -> tuple[FaultSignal, ...]:
        return tuple(
            signal
            for prefix in sorted(self._words)
            for signal in decode_fault_word(prefix, self._words[prefix])
        )

    def observe(
        self,
        *,
        prefix: str,
        raw_word: int,
        observed_at: datetime,
        sequence: int,
    ) -> tuple[FaultEvent, ...]:
        decoded_word = _word(raw_word)
        if type(sequence) is not int:
            raise FaultSequenceError("sequence must be an integer")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise FaultDecodeError("observed_at must be a timezone-aware datetime")
        previous_sequence = self._sequences.get(prefix)
        if previous_sequence is not None and sequence <= previous_sequence:
            raise FaultSequenceError("sequence must increase for each fault word")
        # Validate identity before changing tracker state.
        definition = FAULT_CATALOG.get(prefix)
        if definition is None:
            raise FaultDecodeError("unknown fault-word identity")
        previous = self._words.get(prefix, 0)
        changed = previous ^ decoded_word
        events = tuple(
            FaultEvent(
                prefix=prefix,
                bit=bit,
                code=f"{prefix}_{bit}",
                description=definition.bit(bit).description,
                known=definition.bit(bit).known,
                reserved=definition.bit(bit).reserved,
                raw_word=decoded_word,
                transition=(
                    FaultTransition.HAPPEN
                    if decoded_word & (1 << bit)
                    else FaultTransition.DISAPPEAR
                ),
                observed_at=observed_at,
                sequence=sequence,
            )
            for bit in range(16)
            if changed & (1 << bit)
        )
        self._words[prefix] = decoded_word
        self._sequences[prefix] = sequence
        return events
