"""Stable public domain model surface."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .intents import Direction, IntentSource, PowerIntent
from .observations import DataQuality, Observation, UnitLifecycle


class _FrozenStringMapping[ValueT](dict[str, ValueT]):
    """Deterministic immutable mapping used inside frozen value objects.

    A ``dict`` subclass so pydantic and FastAPI serialize it natively (a bare
    ``Mapping`` ABC cannot be encoded and 500s the audit boundary); every
    mutator is disabled, preserving immutability, and construction sorts so
    iteration, equality, and hashing stay deterministic.
    """

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, ValueT]) -> None:
        self._items = tuple(sorted(values.items()))
        super().__init__(self._items)

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("frozen mapping")

    def __setitem__(self, key: str, value: ValueT) -> None:
        self._immutable()

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: str, *default: ValueT) -> ValueT:  # type: ignore[override]
        self._immutable()

    def popitem(self) -> tuple[str, ValueT]:
        self._immutable()

    def setdefault(self, key: str, default: ValueT | None = None) -> ValueT:
        self._immutable()

    def update(  # type: ignore[override]
        self, *args: Mapping[str, ValueT], **kwargs: ValueT
    ) -> None:
        self._immutable()

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(self._items)


class DecisionStatus(StrEnum):
    AUTHORIZED = "authorized"
    CLAMPED = "clamped"
    REJECTED = "rejected"
    REVOKED = "revoked"


class UnitSetpoint(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    unit_id: str
    direction: Direction
    watts: int
    reactive_vars: int = 0
    generation: int
    intent_id: str
    authorization_expires_at_mono: float

    @field_validator("unit_id", "intent_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("identity must be non-empty and normalized")
        return value

    @field_validator("watts", "generation")
    @classmethod
    def _nonnegative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value

    @field_validator("authorization_expires_at_mono")
    @classmethod
    def _finite_expiry(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("expiry must be finite")
        return value

    @model_validator(mode="after")
    def _direction_matches_magnitude(self) -> UnitSetpoint:
        # IDLE must carry zero watts. The reverse coupling (active directions
        # must be positive) is deliberately absent: zero watts with an active
        # direction is explicit NON-participation — a selected unit with no
        # usable headroom inside an otherwise deliverable fleet intent
        # (2026-08-23 live rejection: fleet dispatches with units at the SOC
        # floor/ceiling). No authority is ever minted for a zero-watt
        # setpoint, so representing it is safe.
        if self.direction is Direction.IDLE and self.watts != 0:
            raise ValueError("idle requires zero watts; active directions may carry zero")
        return self


class ControlPolicy(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )
    version: str
    static_charge_limit_w_by_unit: Mapping[str, int]
    static_discharge_limit_w_by_unit: Mapping[str, int]
    fleet_charge_limit_w: int
    fleet_discharge_limit_w: int
    min_soc_pct: float
    max_soc_pct: float
    max_soc_jump_pct: float
    max_soc_disagreement_pct: float
    min_cell_voltage_v: float
    max_cell_voltage_v: float
    max_cell_imbalance_v: float
    expected_cell_count_by_unit: Mapping[str, int]
    min_temperature_c: float
    max_temperature_c: float
    max_temperature_spread_c: float
    max_telemetry_age_s: float
    max_cell_age_s: float
    authorization_lifetime_s: float
    heartbeat_interval_s: float
    ramp_limit_w_per_s_by_unit: Mapping[str, int]
    apparent_power_limit_va_by_unit: Mapping[str, int]
    reactive_limit_var: int = 0
    stable_samples_needed_to_rearm: int
    blocking_fault_codes: frozenset[str]
    blocking_warning_codes: frozenset[str]
    debug_mode_enabled: bool = False
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the
    # all-or-none export bound triple.  All three None (the default) means
    # export bounding is not armed and the bound for an optimizer charge
    # intent is 0 — an advisory charge may flow only from measured, armed
    # export evidence.
    export_charge_limit_w: int | None = None
    export_headroom_margin_w: int | None = None
    export_telemetry_max_age_s: float | None = None

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("policy version must be non-empty and normalized")
        return value

    @field_validator(
        "static_charge_limit_w_by_unit",
        "static_discharge_limit_w_by_unit",
        "expected_cell_count_by_unit",
        "ramp_limit_w_per_s_by_unit",
        "apparent_power_limit_va_by_unit",
    )
    @classmethod
    def _unit_integer_map(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        copied = dict(value)
        if not copied:
            raise ValueError("per-unit policy maps must not be empty")
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("unit identifiers must be non-empty and normalized")
        if any(item <= 0 for item in copied.values()):
            raise ValueError("per-unit policy values must be positive")
        return _FrozenStringMapping(copied)

    @field_validator("blocking_fault_codes", "blocking_warning_codes")
    @classmethod
    def _codes(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not code or code != code.strip() for code in value):
            raise ValueError("codes must be non-empty and normalized")
        return value

    @field_validator("fleet_charge_limit_w", "fleet_discharge_limit_w")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("fleet limits must be positive")
        return value

    @field_validator("reactive_limit_var")
    @classmethod
    def _nonnegative_reactive_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("reactive limit must be non-negative")
        return value

    @field_validator("stable_samples_needed_to_rearm")
    @classmethod
    def _positive_sample_count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("stable sample count must be positive")
        return value

    @field_validator("export_charge_limit_w")
    @classmethod
    def _positive_export_limit(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("the armed export charge limit must be positive")
        return value

    @field_validator("export_headroom_margin_w")
    @classmethod
    def _nonnegative_export_margin(cls, value: int | None) -> int | None:
        # A zero margin is legal commissioning (every measured watt of export
        # is then eligible); only a negative margin would invent export.
        if value is not None and value < 0:
            raise ValueError("the armed export headroom margin must be non-negative")
        return value

    @field_validator("export_telemetry_max_age_s")
    @classmethod
    def _positive_export_age(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("the armed export telemetry age bound must be positive")
        return value

    @field_validator(
        "min_soc_pct",
        "max_soc_pct",
        "max_soc_jump_pct",
        "max_soc_disagreement_pct",
    )
    @classmethod
    def _percentage(cls, value: float) -> float:
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("percentage policy values must be between zero and 100")
        return value

    @field_validator(
        "min_cell_voltage_v",
        "max_cell_voltage_v",
        "max_cell_imbalance_v",
        "min_temperature_c",
        "max_temperature_c",
        "max_temperature_spread_c",
        "max_telemetry_age_s",
        "max_cell_age_s",
        "authorization_lifetime_s",
        "heartbeat_interval_s",
    )
    @classmethod
    def _finite_float(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("policy values must be finite")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> ControlPolicy:
        maps = (
            self.static_charge_limit_w_by_unit,
            self.static_discharge_limit_w_by_unit,
            self.expected_cell_count_by_unit,
            self.ramp_limit_w_per_s_by_unit,
            self.apparent_power_limit_va_by_unit,
        )
        if any(set(item) != set(maps[0]) for item in maps[1:]):
            raise ValueError("all per-unit policy maps must cover exactly the same units")
        if self.min_soc_pct >= self.max_soc_pct:
            raise ValueError("minimum SOC must be below maximum SOC")
        if self.min_cell_voltage_v >= self.max_cell_voltage_v:
            raise ValueError("minimum cell voltage must be below maximum cell voltage")
        if self.min_temperature_c >= self.max_temperature_c:
            raise ValueError("minimum temperature must be below maximum temperature")
        positive = (
            self.max_cell_imbalance_v,
            self.max_temperature_spread_c,
            self.max_telemetry_age_s,
            self.max_cell_age_s,
            self.authorization_lifetime_s,
            self.heartbeat_interval_s,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("policy thresholds and durations must be positive")
        if self.heartbeat_interval_s >= self.authorization_lifetime_s:
            raise ValueError("heartbeat interval must be shorter than authorization lifetime")
        if self.debug_mode_enabled:
            raise ValueError("debug mode is excluded from the production control policy")
        # The export bound triple is all-or-none: a partially armed triple
        # would let the bound run with, say, a missing freshness bound and no
        # way to judge staleness.  Disarmed means all three keys are None.
        export_armed = (
            self.export_charge_limit_w is not None,
            self.export_headroom_margin_w is not None,
            self.export_telemetry_max_age_s is not None,
        )
        if any(export_armed) and not all(export_armed):
            raise ValueError(
                "the export bound triple must be armed all-or-none: export_charge_limit_w, "
                "export_headroom_margin_w, export_telemetry_max_age_s"
            )
        return self


__all__ = [
    "ControlPolicy",
    "DataQuality",
    "DecisionStatus",
    "Direction",
    "IntentSource",
    "Observation",
    "PowerIntent",
    "UnitLifecycle",
    "UnitSetpoint",
]
