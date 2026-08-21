"""Evidence-backed primitive codecs for EnergyPod Modbus registers.

Power direction is deliberately absent: this module only preserves the signed
wire value observed in the reference application.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final


class ProtocolValueError(ValueError):
    """Raised when a value cannot be represented by the evidenced wire format."""


class WordOrder(Enum):
    HIGH_WORD_FIRST = "high_word_first"
    LOW_WORD_FIRST = "low_word_first"


class UInt32Field(Enum):
    SYSTEM_HISTORY = "system_history"
    LEGACY_PCS_ENERGY = "legacy_pcs_energy"
    LEGACY_BMS_ENERGY = "legacy_bms_energy"
    LEGACY_BECU_ENERGY = "legacy_becu_energy"
    LEGACY_BECU_CAPACITY = "legacy_becu_capacity"
    IOT_BMS_ENERGY = "iot_bms_energy"
    IOT_TOTAL_ENERGY = "iot_total_energy"
    RTU_ID = "rtu_id"


@dataclass(frozen=True, slots=True)
class UInt32Codec:
    word_order: WordOrder
    scale: float
    engineering_unit: str | None


UINT32_CODECS: Final = MappingProxyType(
    {
        UInt32Field.SYSTEM_HISTORY: UInt32Codec(WordOrder.HIGH_WORD_FIRST, 1.0, None),
        UInt32Field.LEGACY_PCS_ENERGY: UInt32Codec(WordOrder.HIGH_WORD_FIRST, 0.1, None),
        UInt32Field.LEGACY_BMS_ENERGY: UInt32Codec(WordOrder.HIGH_WORD_FIRST, 0.1, None),
        UInt32Field.LEGACY_BECU_ENERGY: UInt32Codec(WordOrder.HIGH_WORD_FIRST, 0.001, None),
        UInt32Field.LEGACY_BECU_CAPACITY: UInt32Codec(WordOrder.HIGH_WORD_FIRST, 1.0, None),
        UInt32Field.IOT_BMS_ENERGY: UInt32Codec(WordOrder.LOW_WORD_FIRST, 0.1, None),
        UInt32Field.IOT_TOTAL_ENERGY: UInt32Codec(WordOrder.LOW_WORD_FIRST, 0.1, None),
        UInt32Field.RTU_ID: UInt32Codec(WordOrder.LOW_WORD_FIRST, 1.0, None),
    }
)


def _register(value: object, *, name: str = "register") -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise ProtocolValueError(f"{name} must be an unsigned 16-bit integer")
    return value


def decode_signed16(raw: int) -> int:
    value = _register(raw)
    return value - 0x10000 if value & 0x8000 else value


def encode_signed16(value: int) -> int:
    if type(value) is not int or not -0x8000 <= value <= 0x7FFF:
        raise ProtocolValueError("value must be a signed 16-bit integer")
    return value & 0xFFFF


def decode_uint32(field: UInt32Field, words: object) -> int | float:
    if not isinstance(field, UInt32Field):
        raise ProtocolValueError("field must identify an explicit uint32 family")
    if not isinstance(words, tuple | list) or len(words) != 2:
        raise ProtocolValueError("uint32 decoding requires exactly two registers")
    first = _register(words[0], name="first word")
    second = _register(words[1], name="second word")
    spec = UINT32_CODECS[field]
    high, low = (first, second) if spec.word_order is WordOrder.HIGH_WORD_FIRST else (second, first)
    raw = (high << 16) | low
    return raw if spec.scale == 1.0 else raw * spec.scale


def encode_pq_registers(active_w: int, reactive_var: int) -> tuple[int, int, int]:
    return (1, encode_signed16(active_w), encode_signed16(reactive_var))


def encode_stop_registers() -> tuple[int, int, int]:
    return (1, 0, 0)
