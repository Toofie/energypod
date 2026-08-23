"""Immutable plant-history records (DESIGN_PLANT_HISTORY section 2.2).

One frozen sample row per unit per historian tick and one frozen hourly
rollup per unit-hour.  Two honesty rules are structural, mirroring the
energy ledger's doctrine: every numeric figure is ``None`` when its source
was absent (never zero-filled -- an absent datum is not a zero), and an hour
with zero samples simply has NO rollup row (an absent hour is a gap, never a
zeroed hour).

Samples are projections, not acts: nothing in this module reaches a control
path, and the audit trail stays the sole record of decisions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from energypod.domain.observations import DataQuality

# The 15 numeric observables, in the telemetry_sample column order (DESIGN
# section 2.2).  ``soc_pct`` -- the query vocabulary's name for the advisory
# system SOC figure -- maps onto ``system_soc_pct`` here.
HISTORY_NUMERIC_FIELDS: Final[tuple[str, ...]] = (
    "system_soc_pct",
    "bms_soc_pct",
    "soh_pct",
    "battery_watts",
    "grid_power_w",
    "load_power_w",
    "pack_voltage_v",
    "pack_current_a",
    "cell_min_v",
    "cell_max_v",
    "cell_spread_mv",
    "temperature_min_c",
    "temperature_max_c",
    "dynamic_charge_limit_w",
    "dynamic_discharge_limit_w",
)

# The query vocabulary (DESIGN section 3.2): the 15 numeric fields -- with
# ``soc_pct`` the advisory system figure's public name -- plus the three
# step-encoded meta-fields.
QUERY_NUMERIC_FIELDS: Final[dict[str, str]] = {
    **{name: name for name in HISTORY_NUMERIC_FIELDS},
    "soc_pct": "system_soc_pct",
}
QUERY_META_FIELDS: Final[frozenset[str]] = frozenset({"lifecycle", "health_state", "commanded"})
QUERY_FIELD_VOCABULARY: Final[frozenset[str]] = frozenset(QUERY_NUMERIC_FIELDS) | QUERY_META_FIELDS

DEFAULT_QUERY_FIELDS: Final[tuple[str, ...]] = (
    "bms_soc_pct",
    "battery_watts",
    "grid_power_w",
    "temperature_min_c",
    "temperature_max_c",
)

# The commanded-triple vocabularies (DESIGN section 2.2): the attribution
# words for the per-unit winner at sample time.
COMMANDED_SOURCES: Final[frozenset[str]] = frozenset(
    {"manual", "agent", "schedule", "excess_adviser", "night_adviser", "optimizer"}
)
COMMANDED_DIRECTIONS: Final[frozenset[str]] = frozenset({"charge", "discharge", "idle"})

# The quality-rollup precedence, worst first (the scorecard's export-evidence
# ordering): missing > bad > stale > suspect > good.
QUALITY_PRECEDENCE: Final[dict[DataQuality, int]] = {
    DataQuality.MISSING: 5,
    DataQuality.BAD: 4,
    DataQuality.STALE: 3,
    DataQuality.SUSPECT: 2,
    DataQuality.GOOD: 1,
}


def worst_quality(qualities: Iterable[DataQuality | str]) -> DataQuality:
    """The worst ``DataQuality`` present, by the pinned precedence.

    An empty iterable is an error, not a silent ``good``: a row rollup with
    no judged fields would hide degradation rather than report it.
    """
    worst: DataQuality | None = None
    rank = 0
    for quality in qualities:
        value = quality if isinstance(quality, DataQuality) else DataQuality(str(quality))
        precedence = QUALITY_PRECEDENCE[value]
        if worst is None or precedence > rank:
            worst, rank = value, precedence
    if worst is None:
        raise ValueError("worst_quality requires at least one quality value")
    return worst


def format_history_timestamp(moment: datetime) -> str:
    """The stored UTC ISO-8601 spelling: second precision, fixed width.

    Truncating to whole seconds and always rendering the ``+00:00`` offset
    keeps every stored timestamp lexicographically sortable and the same
    width, so the primary-key clustering and the SQL window scans stay
    correct by construction.
    """
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("history timestamps must be timezone-aware datetimes")
    utc = moment if moment.tzinfo is UTC else moment.astimezone(UTC)
    return utc.replace(microsecond=0).isoformat()


def parse_history_timestamp(raw: str) -> datetime:
    """Decode the stored spelling back into a UTC datetime."""
    moment = datetime.fromisoformat(raw)
    return moment if moment.tzinfo is UTC else moment.astimezone(UTC)


def hour_start_of(moment: datetime) -> datetime:
    """The UTC hour bucket one timestamp belongs to."""
    utc = moment if moment.tzinfo is UTC else moment.astimezone(UTC)
    return utc.replace(minute=0, second=0, microsecond=0)


ONE_HOUR: Final[timedelta] = timedelta(hours=1)


def _optional_finite(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{label} must be a finite number or None")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number or None")
    return number


_QUALITY_WORDS: Final[frozenset[str]] = frozenset(item.value for item in DataQuality)


def _optional_word(value: object, label: str, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{label} must be one of {sorted(allowed)} or None")
    return value


@dataclass(frozen=True, slots=True)
class TelemetrySampleRow:
    """One unit's sampled telemetry projection at one historian tick.

    An immutable record in the energy-ledger style: constructed with keyword
    arguments, validated in ``__post_init__``, never mutated.  ``sampled_at``
    is UTC at whole-second precision -- one timestamp shared by every unit
    sampled in the same tick (DESIGN section 2.1).
    """

    unit_id: str
    sampled_at: datetime
    system_soc_pct: float | None
    bms_soc_pct: float | None
    soh_pct: float | None
    battery_watts: float | None
    grid_power_w: float | None
    load_power_w: float | None
    pack_voltage_v: float | None
    pack_current_a: float | None
    cell_min_v: float | None
    cell_max_v: float | None
    cell_spread_mv: float | None
    temperature_min_c: float | None
    temperature_max_c: float | None
    dynamic_charge_limit_w: float | None
    dynamic_discharge_limit_w: float | None
    lifecycle: str
    health_state: str | None
    quality: str
    commanded_source: str | None = None
    commanded_direction: str | None = None
    commanded_w: int | None = None
    debug_mode_w: int | None = None
    ctrl_mode_w: int | None = None
    work_mode_w: int | None = None
    run_mode_w: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.unit_id, str)
            or not self.unit_id
            or self.unit_id != self.unit_id.strip()
        ):
            raise ValueError("unit_id must be a non-empty normalized identifier")
        if not isinstance(self.sampled_at, datetime) or self.sampled_at.tzinfo is None:
            raise ValueError("sampled_at must be a timezone-aware datetime")
        utc = self.sampled_at.astimezone(UTC)
        if utc.microsecond != 0:
            raise ValueError("sampled_at must be truncated to whole seconds")
        object.__setattr__(self, "sampled_at", utc)
        if not isinstance(self.lifecycle, str) or not self.lifecycle:
            raise ValueError("lifecycle must be a non-empty word")
        if self.health_state is not None and (
            not isinstance(self.health_state, str) or not self.health_state
        ):
            raise ValueError("health_state must be a non-empty word or None")
        if not isinstance(self.quality, str) or self.quality not in _QUALITY_WORDS:
            raise ValueError("quality must be a DataQuality value")
        for name in HISTORY_NUMERIC_FIELDS:
            object.__setattr__(self, name, _optional_finite(getattr(self, name), name))
        for name, allowed in (
            ("commanded_source", COMMANDED_SOURCES),
            ("commanded_direction", COMMANDED_DIRECTIONS),
        ):
            object.__setattr__(self, name, _optional_word(getattr(self, name), name, allowed))
        if self.commanded_w is not None and (
            isinstance(self.commanded_w, bool) or not isinstance(self.commanded_w, int)
        ):
            raise TypeError("commanded_w must be an integer or None")
        for name in ("debug_mode_w", "ctrl_mode_w", "work_mode_w", "run_mode_w"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF
            ):
                raise ValueError(f"{name} must be an unsigned 16-bit word or None")
            object.__setattr__(self, name, value)

    def field_value(self, column: str) -> float | None:
        """One numeric column's value (the query vocabulary's column name)."""
        try:
            value: float | None = getattr(self, column)
        except AttributeError as exc:
            raise KeyError(f"unknown history numeric column: {column!r}") from exc
        return value


@dataclass(frozen=True, slots=True)
class TelemetryRollupHour:
    """One unit-hour's aggregate over the full-resolution rows.

    ``metrics`` maps every numeric field to its ``(min, max, mean)`` triple,
    each ``None`` when the hour held no non-null value for that field.
    ``sample_count`` is the hour's ROW count -- the coverage honesty marker
    (120 at the 30 s default means a fully covered hour).
    """

    unit_id: str
    hour_start: datetime
    metrics: dict[str, tuple[float | None, float | None, float | None]]
    sample_count: int
    worst_quality: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.unit_id, str)
            or not self.unit_id
            or self.unit_id != self.unit_id.strip()
        ):
            raise ValueError("unit_id must be a non-empty normalized identifier")
        if not isinstance(self.hour_start, datetime) or self.hour_start.tzinfo is None:
            raise ValueError("hour_start must be a timezone-aware datetime")
        utc = self.hour_start.astimezone(UTC)
        if utc.minute or utc.second or utc.microsecond:
            raise ValueError("hour_start must be a whole UTC hour")
        object.__setattr__(self, "hour_start", utc)
        if not isinstance(self.metrics, dict) or set(self.metrics) != set(HISTORY_NUMERIC_FIELDS):
            raise ValueError("metrics must carry exactly the numeric history fields")
        for name, triple in self.metrics.items():
            if not isinstance(triple, tuple) or len(triple) != 3:
                raise TypeError(f"metric {name!r} must be a (min, max, mean) triple")
            if triple != (None, None, None) and any(item is None for item in triple):
                raise ValueError(f"metric {name!r} is partially null")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 1
        ):
            raise ValueError("sample_count must be a positive integer")
        if not isinstance(self.worst_quality, str) or self.worst_quality not in _QUALITY_WORDS:
            raise ValueError("worst_quality must be a DataQuality value")
        object.__setattr__(self, "metrics", dict(self.metrics))


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """The outcome of one rollup-then-prune pass (both counts zero on a no-op)."""

    rolled_hours: int
    pruned_samples: int
    pruned_rollups: int

    def __post_init__(self) -> None:
        for name in ("rolled_hours", "pruned_samples", "pruned_rollups"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
