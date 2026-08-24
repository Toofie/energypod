"""Immutable audit facts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from .intents import IntentSource
from .models import _FrozenStringMapping
from .observations import UnitLifecycle


class DuplicateAuditEventError(ValueError):
    pass


class AuditEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )
    event_id: str
    occurred_at: datetime
    monotonic_offset_s: float
    process_instance_id: str
    event_type: str
    unit_id: str | None = None
    connection_epoch: int | None = None
    generation: int | None = None
    cycle_id: str | None = None
    principal: str
    source: IntentSource | None = None
    correlation_id: str
    intent_id: str | None = None
    policy_version: str | int
    configuration_version: int
    observation_sequences: Mapping[str, int]
    reason_codes: tuple[str, ...]
    requested_active_w: int
    authorized_active_w: int
    # Per-unit watt breakdowns (2026-08-23 fleet-row opacity fix): unsigned
    # per-unit magnitudes.  ``requested_watts_by_unit`` is the intent's own
    # per-unit target map (null for scalar fleet-total intents);
    # ``authorized_watts_by_unit`` is the decision's per-unit authorized watts
    # (null when no batch was minted, matching authorized_active_w == 0).
    # Both are optional so durable rows written before these fields existed
    # keep decoding with null defaults.
    requested_watts_by_unit: Mapping[str, int] | None = None
    authorized_watts_by_unit: Mapping[str, int] | None = None
    # Per-unit direction breakdown (2026-08-24 concurrent operations): a cycle
    # composed from several intents may run DIFFERENT directions on different
    # units, so the row names each unit's own winning direction next to its
    # watts.  Null on single-intent rows (the intent's direction already says
    # it) and on durable rows written before this field existed.
    directions_by_unit: Mapping[str, str] | None = None
    request_fingerprint: str
    response_fingerprint: str
    result: str
    lifecycle: UnitLifecycle
    # Advisory rows with reconstruction value (the night-v2 wave, 2026-08-24:
    # the night adviser's ``night_target_set`` archive and the trust ledger's
    # daily evaluations): a free-form but JSON-native payload whose DATA is
    # the row's point -- a later decision replays from it.  Null on every
    # control-decision row ever written: the field is additive, and durable
    # rows from before it decode with the null default (the known-optional
    # omission rule).
    payload: Mapping[str, Any] | None = None

    @field_validator("observation_sequences")
    @classmethod
    def _freeze_sequences(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        copied = dict(value)
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("observation unit identifiers must be normalized")
        if any(sequence < 0 for sequence in copied.values()):
            raise ValueError("observation sequences must be non-negative")
        return _FrozenStringMapping(copied)

    @field_validator("requested_watts_by_unit", "authorized_watts_by_unit")
    @classmethod
    def _freeze_watts_by_unit(cls, value: Mapping[str, int] | None) -> Mapping[str, int] | None:
        if value is None:
            return None
        copied = dict(value)
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("per-unit watt keys must be normalized")
        if any(watts < 0 for watts in copied.values()):
            raise ValueError("per-unit watts must be non-negative")
        return _FrozenStringMapping(copied)

    @field_validator("directions_by_unit")
    @classmethod
    def _freeze_directions_by_unit(
        cls, value: Mapping[str, str] | None
    ) -> Mapping[str, str] | None:
        if value is None:
            return None
        copied = dict(value)
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("per-unit direction keys must be normalized")
        if any(direction not in {"charge", "discharge", "idle"} for direction in copied.values()):
            raise ValueError("per-unit directions must be charge, discharge, or idle")
        return _FrozenStringMapping(copied)

    @field_validator(
        "event_id",
        "process_instance_id",
        "event_type",
        "principal",
        "correlation_id",
        "request_fingerprint",
        "response_fingerprint",
        "result",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("audit text fields must be non-empty and normalized")
        return value

    @field_validator("unit_id", "cycle_id", "intent_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("optional audit identifiers must be normalized")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("audit timestamp must be UTC")
        return value

    @field_validator("monotonic_offset_s")
    @classmethod
    def _finite_offset(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("monotonic offset must be finite")
        return value

    @field_validator("connection_epoch", "generation")
    @classmethod
    def _optional_counter(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("audit counters must be non-negative")
        return value

    @field_validator("configuration_version")
    @classmethod
    def _configuration_version(cls, value: int) -> int:
        if value < 0:
            raise ValueError("configuration version must be non-negative")
        return value

    @field_validator("policy_version")
    @classmethod
    def _policy_version(cls, value: str | int) -> str | int:
        if type(value) is int and value < 0:
            raise ValueError("numeric policy version must be non-negative")
        if type(value) is str and (not value or value != value.strip()):
            raise ValueError("policy version must be normalized")
        return value

    @field_validator("reason_codes")
    @classmethod
    def _reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not code or code != code.strip() for code in value):
            raise ValueError("reason codes must be normalized")
        return value

    @field_validator("payload")
    @classmethod
    def _freeze_payload(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if value is None:
            return None
        if any(type(key) is not str or not key for key in value):
            raise ValueError("payload keys must be non-empty strings")
        return _FrozenStringMapping(dict(value))
