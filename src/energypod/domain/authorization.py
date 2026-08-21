"""Single-cycle authorization capabilities."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .intents import Direction


class StaleGenerationError(ValueError):
    pass


class AuthorizedSetpoint(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    unit_id: str
    connection_epoch: int
    generation: int
    cycle_id: str
    intent_id: str
    intent_revision: int
    direction: Direction
    watts: int
    reactive_vars: int = 0
    issued_at_mono: float
    not_before_mono: float
    expires_at_mono: float
    observation_sequence: int
    maximum_observation_age_s: float
    policy_version: str
    configuration_version: int
    decision_id: str

    @field_validator(
        "connection_epoch",
        "generation",
        "intent_revision",
        "watts",
        "observation_sequence",
        "configuration_version",
    )
    @classmethod
    def _nonnegative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be non-negative")
        return value

    @field_validator("unit_id", "cycle_id", "intent_id", "policy_version", "decision_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("identifiers must be non-empty and normalized")
        return value

    @field_validator(
        "issued_at_mono", "not_before_mono", "expires_at_mono", "maximum_observation_age_s"
    )
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value

    @model_validator(mode="after")
    def _valid(self) -> AuthorizedSetpoint:
        if not self.issued_at_mono <= self.not_before_mono < self.expires_at_mono:
            raise ValueError("authorization window is invalid")
        if self.maximum_observation_age_s <= 0:
            raise ValueError("maximum observation age must be positive")
        if (self.direction is Direction.IDLE) != (self.watts == 0):
            raise ValueError("direction and watts disagree")
        return self


class AuthorizationBatch(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    cycle_id: str
    generation: int
    authorizations: tuple[AuthorizedSetpoint, ...]

    @field_validator("cycle_id")
    @classmethod
    def _cycle_id(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("cycle id must be non-empty and normalized")
        return value

    @field_validator("generation")
    @classmethod
    def _generation(cls, value: int) -> int:
        if value < 0:
            raise ValueError("generation must be non-negative")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> AuthorizationBatch:
        unit_ids = [item.unit_id for item in self.authorizations]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("an authorization batch cannot contain duplicate units")
        if any(item.cycle_id != self.cycle_id for item in self.authorizations):
            raise ValueError("authorization cycle does not match its batch")
        if any(item.generation != self.generation for item in self.authorizations):
            raise ValueError("authorization generation does not match its batch")
        return self
