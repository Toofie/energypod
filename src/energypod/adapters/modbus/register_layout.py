"""Register plans and evidence-gated topology assessment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class RegisterLayoutError(ValueError):
    """Raised for malformed register-layout inputs."""


class ProtocolLayout(Enum):
    LEGACY = "legacy"
    IOT = "iot"


class EvidenceStatus(Enum):
    CONFIRMED_VENDOR = "confirmed_vendor"
    CORROBORATED_OPERATIONALLY = "corroborated_operationally"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RegisterBlock:
    address: int
    count: int
    function_code: int
    live_actuation_eligible: bool = False

    def __post_init__(self) -> None:
        if type(self.address) is not int or not 0 <= self.address <= 0xFFFF:
            raise RegisterLayoutError("register address must be an unsigned 16-bit integer")
        if type(self.count) is not int or not 1 <= self.count <= 125:
            raise RegisterLayoutError("register count must be between 1 and 125")
        if self.address + self.count - 1 > 0xFFFF:
            raise RegisterLayoutError("register block exceeds the Modbus address space")
        if self.function_code not in (3, 16) or type(self.function_code) is not int:
            raise RegisterLayoutError("only evidenced function codes 03 and 16 are supported")


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    evidence_status: EvidenceStatus
    implementation_eligible: bool = False
    live_actuation_eligible: bool = False


@dataclass(frozen=True, slots=True)
class LayoutProbe:
    layout: ProtocolLayout
    enable_mask: int
    bic_count: int
    becu_count: int
    topology_valid: bool
    live_actuation_eligible: bool = False


@dataclass(frozen=True, slots=True)
class CellMapAssessment:
    vendor_voltage_count: int
    temperature_count: int
    balance_count: int
    voltage_end_exclusive: int
    temperature_end_exclusive: int
    map_non_overlapping: bool
    observed_count_matches_vendor_formula: bool
    ineligibility_reasons: tuple[str, ...]
    live_actuation_eligible: bool = False


def _wire_register(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise RegisterLayoutError(f"{name} must be an unsigned 16-bit integer")
    return value


def _read(address: int, count: int) -> RegisterBlock:
    return RegisterBlock(address, count, 3)


_LAYOUT_PROBE = _read(0x5000, 7)
_COMMON_READS = (
    _read(0x0100, 61),
    _read(0x8100, 1),
    _read(0x8139, 1),
    _read(0x8106, 2),
    _read(0x8102, 56),
)
# The structural write whitelist (DESIGN_POD_PARKING section 5, item 4): the
# evidenced PQ objective plus the sanctioned standby debug-mode word -- TWO
# named operations, no more.  Every other maintenance/debug write the vendor
# code names stays absent from this catalog, which is the single admission
# list.  NOTE the deliberate address asymmetry: the debug-mode word is
# WRITTEN at 0x8000 and READ BACK one block higher at 0x8100 (a common_reads
# block above) -- both spellings live-proven on rhs 2026-08-24
# (docs/evidence/standby-cycle-2026-08-24.md).  Do not "normalize" the pair.
_WRITE_BLOCKS: Mapping[str, RegisterBlock] = MappingProxyType(
    {
        "pq_objective": RegisterBlock(0x0200, 3, 16, False),
        "debug_mode": RegisterBlock(0x8000, 1, 16, False),
    }
)
_CLAIM_STATUSES = {
    "waveshare_rtu_over_tcp_framing": EvidenceStatus.CORROBORATED_OPERATIONALLY,
    "device_id_4": EvidenceStatus.CONFIRMED_VENDOR,
    "tcp_device_id_ignored": EvidenceStatus.UNKNOWN,
    "full_pq_frame": EvidenceStatus.CONFIRMED_VENDOR,
    "active_power_sign": EvidenceStatus.CORROBORATED_OPERATIONALLY,
    "reactive_power_semantics": EvidenceStatus.UNKNOWN,
    "firmware_lease_duration": EvidenceStatus.UNKNOWN,
    "firmware_fallback": EvidenceStatus.UNKNOWN,
    "active_only_write": EvidenceStatus.CORROBORATED_OPERATIONALLY,
    "pcs_run_mode_precondition": EvidenceStatus.UNKNOWN,
    "deployed_iot_layout": EvidenceStatus.ASSUMED,
    "deployed_cell_count_59": EvidenceStatus.ASSUMED,
    "iot_cell_layout_above_six_bics": EvidenceStatus.UNKNOWN,
    "debug_mode_functions": EvidenceStatus.UNKNOWN,
    "cell_imbalance_threshold": EvidenceStatus.UNKNOWN,
}
_IMPLEMENTATION_ELIGIBLE = frozenset({"full_pq_frame", "waveshare_rtu_over_tcp_framing"})
_CLAIMS: Mapping[str, EvidenceClaim] = MappingProxyType(
    {
        name: EvidenceClaim(status, implementation_eligible=name in _IMPLEMENTATION_ELIGIBLE)
        for name, status in _CLAIM_STATUSES.items()
    }
)


class RegisterCatalog:
    """Immutable register catalog.

    The writable set is exactly the two named blocks -- the evidenced
    three-register PQ objective and the sanctioned standby debug-mode word
    (DESIGN_POD_PARKING section 5; live rhs cycle 2026-08-24) -- reachable
    only through the transport's separately named methods.  Every other
    maintenance or debug write the vendor code names stays absent.
    """

    __slots__ = ()

    layout_probe = _LAYOUT_PROBE
    common_reads = _COMMON_READS
    write_blocks = _WRITE_BLOCKS
    claims = _CLAIMS

    def iot_reads(self, *, bic_count: int) -> tuple[RegisterBlock, ...]:
        bic = _wire_register(bic_count, "bic_count")
        if not 1 <= bic <= 6:
            raise RegisterLayoutError("IoT read plans require a non-overlapping 1..6 BIC topology")
        return (
            _read(0x1000, 21),
            _read(0x1040, 22),
            _read(0x1060, 32),
            _read(0x2000, 13),
            _read(0x2040, 22),
            _read(0x2060, 19),
            _read(0x4101, 12),
            _read(0x5000, 31),
            _read(0x5040, 22),
            _read(0x5200, min(bic * 10, 100)),
            _read(0x523C, bic * 3),
            _read(0x524E, bic),
        )

    def legacy_reads(self, *, bic_count: int, becu_count: int) -> tuple[RegisterBlock, ...]:
        bic = _wire_register(bic_count, "bic_count")
        becu = _wire_register(becu_count, "becu_count")
        if not 1 <= bic <= 62:
            raise RegisterLayoutError("legacy BIC count cannot produce valid FC03 blocks")
        if not 1 <= becu <= 8:
            raise RegisterLayoutError("legacy BECU count must fit the eight-bit enable mask")
        blocks = [
            _read(0x1000, 60),
            _read(0x1040, 60),
            _read(0x3000, 35),
            _read(0x3040, 22),
            _read(0x5000, 21),
            _read(0x5040, 34),
        ]
        for index in range(becu):
            stride = 0x0800 * index
            blocks.extend(
                (
                    _read(0x6000 + stride, 35),
                    _read(0x6040 + stride, 20),
                    _read(0x6200 + stride, min(bic * 10, 100)),
                    _read(0x6300 + stride, min(bic * 10, 100)),
                    _read(0x6400 + stride, bic),
                    _read(0x6500 + stride, bic * 2),
                )
            )
        return tuple(blocks)


def detect_layout(registers: object) -> LayoutProbe:
    if not isinstance(registers, tuple | list) or len(registers) != 7:
        raise RegisterLayoutError("layout probe requires exactly seven registers")
    values = tuple(
        _wire_register(value, f"register[{index}]") for index, value in enumerate(registers)
    )
    layout = ProtocolLayout.IOT if values[0] > 10 else ProtocolLayout.LEGACY
    enable_offset, bic_offset = (4, 5) if layout is ProtocolLayout.IOT else (2, 6)
    enable_mask = values[enable_offset] & 0xFF
    raw_bic = values[bic_offset]
    signed_bic = raw_bic - 0x10000 if raw_bic & 0x8000 else raw_bic
    bic_count = max(0, signed_bic)
    becu_count = max(1, enable_mask.bit_count())
    topology_valid = bic_count > 0 and (layout is ProtocolLayout.LEGACY or bic_count <= 6)
    return LayoutProbe(
        layout=layout,
        enable_mask=enable_mask,
        bic_count=bic_count,
        becu_count=becu_count,
        topology_valid=topology_valid,
    )


def assess_iot_cell_map(*, bic_count: int, observed_cell_count: int) -> CellMapAssessment:
    bic = _wire_register(bic_count, "bic_count")
    observed = _wire_register(observed_cell_count, "observed_cell_count")
    voltages = min(bic * 10, 100)
    temperatures = bic * 3
    voltage_end = 0x5200 + voltages
    temperature_end = 0x523C + temperatures
    non_overlapping = voltage_end <= 0x523C and temperature_end <= 0x524E
    count_matches = observed == voltages
    reasons: list[str] = []
    if bic == 0:
        reasons.append("invalid_bic_count")
    if not non_overlapping:
        reasons.append("overlapping_cell_map")
    if not count_matches:
        reasons.append("cell_count")
    reasons.append("actuation_not_commissioned")
    return CellMapAssessment(
        vendor_voltage_count=voltages,
        temperature_count=temperatures,
        balance_count=bic,
        voltage_end_exclusive=voltage_end,
        temperature_end_exclusive=temperature_end,
        map_non_overlapping=non_overlapping,
        observed_count_matches_vendor_formula=count_matches,
        ineligibility_reasons=tuple(reasons),
    )
