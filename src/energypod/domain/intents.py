"""Operator and advisory requests expressed without wire-level signs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class Direction(StrEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"


class IntentSource(StrEnum):
    EMERGENCY_STOP = "emergency_stop"
    MANUAL = "manual"
    AGENT = "agent"
    OPTIMIZER = "optimizer"
    SCHEDULE = "schedule"


class PowerIntent(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", populate_by_name=True)

    id: str
    source: IntentSource
    selected_unit_ids: frozenset[str]
    direction: Direction
    watts: int
    # Per-unit watt targets (the 2026-08-23 operator ruling: each setting is
    # that battery's own request, never one fleet total).  ``watts`` stays the
    # fleet total -- exactly the sum of these targets -- so every existing
    # fleet-total consumer is unchanged.  IDLE intents carry ``None``.
    watts_by_unit: Mapping[str, int] | None = None
    duration_s: float
    accepted_at_mono: float
    acceptance_revision: int = 0
    actor_identity: str

    @field_validator("id", "actor_identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("identity must be non-empty and normalized")
        return value

    @field_validator("selected_unit_ids")
    @classmethod
    def _units(cls, value: frozenset[str]) -> frozenset[str]:
        if not value or any(not unit or unit != unit.strip() for unit in value):
            raise ValueError("selected unit identifiers must be non-empty and normalized")
        return value

    @field_validator("watts_by_unit")
    @classmethod
    def _per_unit_watts(cls, value: Mapping[str, int] | None) -> Mapping[str, int] | None:
        if value is None:
            return None
        copied = dict(value)
        if not copied:
            raise ValueError("per-unit watts must name every selected unit")
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("per-unit watt keys must be non-empty and normalized")
        if any(item <= 0 for item in copied.values()):
            raise ValueError("per-unit watts must be positive")
        # Deferred import: ``models`` already imports this module for Direction.
        from .models import _FrozenStringMapping

        return _FrozenStringMapping(copied)

    @field_validator("watts", "acceptance_revision")
    @classmethod
    def _nonnegative_integer(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value

    @field_validator("duration_s")
    @classmethod
    def _positive_duration(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("duration must be finite and positive")
        return value

    @field_validator("accepted_at_mono")
    @classmethod
    def _finite_time(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("monotonic timestamp must be finite")
        return value

    @model_validator(mode="after")
    def _direction_matches_magnitude(self) -> PowerIntent:
        if (self.direction is Direction.IDLE) != (self.watts == 0):
            raise ValueError("idle requires zero watts; active directions require positive watts")
        if self.source is IntentSource.EMERGENCY_STOP and self.direction is not Direction.IDLE:
            raise ValueError("emergency stop intents must request idle")
        if self.watts_by_unit is not None:
            if self.direction is Direction.IDLE:
                raise ValueError("idle intents carry no per-unit watts")
            if set(self.watts_by_unit) != set(self.selected_unit_ids):
                raise ValueError("per-unit watt keys must equal the selected units")
            if sum(self.watts_by_unit.values()) != self.watts:
                raise ValueError("per-unit watts must sum to the fleet total")
        return self

    @property
    def expires_at_mono(self) -> float:
        return self.accepted_at_mono + self.duration_s
