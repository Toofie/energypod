"""Immutable observations and explicit telemetry quality."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class DataQuality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    MISSING = "missing"
    BAD = "bad"
    SUSPECT = "suspect"


class UnitLifecycle(StrEnum):
    BOOT = "boot"
    OBSERVE_ONLY = "observe_only"
    DISARMED = "disarmed"
    ARMED_IDLE = "armed_idle"
    ACTIVE = "active"
    INHIBITED = "inhibited"
    STOPPING = "stopping"
    DISCONNECTED = "disconnected"


class _FrozenStringMapping[ValueT](Mapping[str, ValueT]):
    """Small, deterministic and hashable immutable mapping for value objects."""

    __slots__ = ("_items", "_values")

    def __init__(self, values: Mapping[str, ValueT]) -> None:
        self._items = tuple(sorted(values.items()))
        self._values = dict(self._items)

    def __getitem__(self, key: str) -> ValueT:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __hash__(self) -> int:
        return hash(self._items)


class Observation(BaseModel):
    QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "system_soc_pct",
            "bms_soc_pct",
            "soh_pct",
            "battery_watts",
            "pack_voltage_v",
            "pack_current_a",
            "dynamic_charge_limit_w",
            "dynamic_discharge_limit_w",
            "cell_voltages_v",
            "temperatures_c",
        }
    )
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the two
    # per-pod CT power words (PCS 0x1000+17/+20, PROTOCOL_EVIDENCE 4c) are
    # ADVISORY telemetry.  They gate export-bounded control through their own
    # fail-closed bound, never through the safety-critical completeness set,
    # so a deployment whose read plan does not serve the PCS block keeps fully
    # qualified observations for ordinary control.
    ADVISORY_QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset({"grid_power_w", "load_power_w"})
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )

    unit_id: str
    device_identity: str | None = None
    connection_epoch: int = 0
    wall_timestamp: datetime
    captured_at_mono: float
    sequence: int
    lifecycle: UnitLifecycle
    protocol_profile: str
    system_soc_pct: float | None
    bms_soc_pct: float | None
    soh_pct: float | None
    battery_watts: float | None
    pack_voltage_v: float | None
    pack_current_a: float | None
    dynamic_charge_limit_w: float | None
    dynamic_discharge_limit_w: float | None
    # Advisory per-pod CT power (signed; negative grid = import, positive =
    # export — PROTOCOL_EVIDENCE 4b/4c).  ``None`` when the poll did not
    # serve the PCS live block; never zero-filled.
    grid_power_w: float | None = None
    load_power_w: float | None = None
    expected_cell_count: int | None = None
    cell_voltages_v: tuple[float, ...]
    cell_captured_at_mono: float | None = None
    cell_sequence: int | None = None
    expected_temperature_count: int | None = None
    temperatures_c: tuple[float, ...]
    active_faults: frozenset[str]
    active_warnings: frozenset[str]
    quality: Mapping[str, DataQuality]

    @field_validator("unit_id", "protocol_profile")
    @classmethod
    def _normalized_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("text must be non-empty and normalized")
        return value

    @field_validator("device_identity")
    @classmethod
    def _optional_identity(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("device identity must be non-empty and normalized when present")
        return value

    @field_validator("connection_epoch", "sequence")
    @classmethod
    def _nonnegative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("counter must be non-negative")
        return value

    @field_validator("expected_cell_count", "expected_temperature_count")
    @classmethod
    def _optional_positive_count(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("count must be positive")
        return value

    @field_validator("cell_sequence")
    @classmethod
    def _optional_nonnegative_sequence(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("sequence must be non-negative")
        return value

    @field_validator("wall_timestamp")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("wall timestamp must be UTC")
        return value

    @field_validator("captured_at_mono", "cell_captured_at_mono")
    @classmethod
    def _finite_timestamp(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return value

    @field_validator("system_soc_pct", "bms_soc_pct", "soh_pct")
    @classmethod
    def _percentage(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("percentage must be finite and between zero and 100")
        return value

    @field_validator(
        "battery_watts",
        "pack_voltage_v",
        "pack_current_a",
        "grid_power_w",
        "load_power_w",
    )
    @classmethod
    def _finite_measurement(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("measurement must be finite")
        return value

    @field_validator("dynamic_charge_limit_w", "dynamic_discharge_limit_w")
    @classmethod
    def _nonnegative_measurement(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("limit must be finite and non-negative")
        return value

    @field_validator("cell_voltages_v", "temperatures_c")
    @classmethod
    def _finite_tuple(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("measurements must be finite")
        return value

    @field_validator("active_faults", "active_warnings")
    @classmethod
    def _codes(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not code or code != code.strip() for code in value):
            raise ValueError("codes must be non-empty and normalized")
        return value

    @field_validator("quality")
    @classmethod
    def _quality(cls, value: Mapping[str, DataQuality]) -> Mapping[str, DataQuality]:
        copied = dict(value)
        # Exactly the ten safety-critical fields, or those plus the two
        # advisory CT fields: the wire decoder always emits the twelve-key
        # shape (MISSING for unserved sources), while older producers and the
        # ten-field test fixtures keep the original shape.  Anything else is a
        # malformed quality map, not a partial view to forgive.
        allowed = (cls.QUALITY_FIELDS, cls.QUALITY_FIELDS | cls.ADVISORY_QUALITY_FIELDS)
        if set(copied) not in allowed:
            raise ValueError("quality must contain exactly the declared telemetry fields")
        if any(type(item) is not DataQuality for item in copied.values()):
            raise TypeError("quality values must be DataQuality members")
        return _FrozenStringMapping(copied)

    @model_validator(mode="after")
    def _cell_metadata_defaults(self) -> Observation:
        if self.cell_captured_at_mono is None:
            object.__setattr__(self, "cell_captured_at_mono", self.captured_at_mono)
        if self.cell_sequence is None:
            object.__setattr__(self, "cell_sequence", self.sequence)
        return self

    def age_seconds(self, now_mono: float) -> float:
        if type(now_mono) is not float or not math.isfinite(now_mono):
            raise TypeError("current monotonic time must be a finite float")
        age = now_mono - self.captured_at_mono
        if age < 0:
            raise ValueError("current monotonic time precedes capture")
        return age

    @property
    def cell_min_voltage_v(self) -> float | None:
        return min(self.cell_voltages_v, default=None)

    @property
    def cell_max_voltage_v(self) -> float | None:
        return max(self.cell_voltages_v, default=None)

    @property
    def cell_imbalance_v(self) -> float | None:
        low, high = self.cell_min_voltage_v, self.cell_max_voltage_v
        return None if low is None or high is None else high - low

    @property
    def temperature_min_c(self) -> float | None:
        return min(self.temperatures_c, default=None)

    @property
    def temperature_max_c(self) -> float | None:
        return max(self.temperatures_c, default=None)

    @property
    def cells_complete(self) -> bool:
        return (
            self.expected_cell_count is not None
            and len(self.cell_voltages_v) == self.expected_cell_count
            and self.quality["cell_voltages_v"] is DataQuality.GOOD
        )

    @property
    def temperatures_complete(self) -> bool:
        return (
            self.expected_temperature_count is not None
            and len(self.temperatures_c) == self.expected_temperature_count
            and self.quality["temperatures_c"] is DataQuality.GOOD
        )

    @property
    def safety_data_complete(self) -> bool:
        required = (
            self.system_soc_pct,
            self.bms_soc_pct,
            self.soh_pct,
            self.battery_watts,
            self.pack_voltage_v,
            self.pack_current_a,
            self.dynamic_charge_limit_w,
            self.dynamic_discharge_limit_w,
        )
        return (
            all(self.quality[field] is DataQuality.GOOD for field in self.QUALITY_FIELDS)
            and all(value is not None for value in required)
            and self.cells_complete
            and self.temperatures_complete
        )
