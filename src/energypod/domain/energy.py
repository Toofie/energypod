"""Immutable per-site-day energy records (the daily energy scorecard).

DESIGN_ENERGY_SCORECARD section 5: one frozen record per site-day in the site
timezone, carrying per-unit metric blocks, the fleet rollup, the per-metric
source labels, and the A-1 grid-counter cross-check.  Two honesty rules are
structural: every per-unit figure is ``None`` when its source was absent all
day (never zero), and ``solar_production_measured`` is a constant ``False``
(site PV is not wired to the pod inputs; the scorecard's solar story is the
surplus the site exported and the surplus the batteries captured).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.models import _FrozenStringMapping

KIND_IN_PROGRESS: Final[str] = "in_progress"
KIND_COMPLETE: Final[str] = "complete"
KIND_PARTIAL: Final[str] = "partial"
_KINDS: Final[frozenset[str]] = frozenset({KIND_IN_PROGRESS, KIND_COMPLETE, KIND_PARTIAL})

# Per-metric source labels (DESIGN section 2).  The grid figure's source is
# the operator's promotion decision; battery/load are the role-confirmed
# device counters; surplus attribution is measured battery watts over
# adviser-active ticks.
GRID_SOURCE_INTEGRATED: Final[str] = "integrated_ct"
GRID_SOURCE_DEVICE_COUNTER: Final[str] = "device_counter"
GRID_SOURCES: Final[frozenset[str]] = frozenset(
    {GRID_SOURCE_INTEGRATED, GRID_SOURCE_DEVICE_COUNTER}
)
BATTERY_SOURCE: Final[str] = "device_counter"
LOAD_SOURCE: Final[str] = "device_counter"
SURPLUS_SOURCE: Final[str] = "attributed_adviser"

GRID_ROLES_UNPINNED: Final[str] = "unpinned"
GRID_ROLES_VENDOR_LABELS: Final[str] = "vendor_labels"
GRID_ROLES_SWAPPED: Final[str] = "swapped"
GRID_ROLE_SETTINGS: Final[frozenset[str]] = frozenset(
    {GRID_ROLES_UNPINNED, GRID_ROLES_VENDOR_LABELS, GRID_ROLES_SWAPPED}
)

# The A-1 cross-check verdict vocabulary (DESIGN section 3): the vendor-label
# ordering, the swap, an honest "both orderings fit", or no verdict at all
# when the day could not discriminate (low coverage, no export, a reset).
VERDICT_VENDOR_LABELS: Final[str] = "vendor_labels"
VERDICT_SWAPPED: Final[str] = "swapped"
VERDICT_UNDISCRIMINATING: Final[str] = "undiscriminating"

# Counter-reset flag tokens: one ``counter_reset:<metric>`` per affected
# unit-metric-day (the record-side half of the audited reset fact).
COUNTER_RESET_FLAG_PREFIX: Final[str] = "counter_reset:"
_METRICS: Final[tuple[str, ...]] = ("grid_a", "grid_b", "load", "charge", "discharge")


def counter_reset_flag(metric: str) -> str:
    """The record flag for one reset metric (``counter_reset:charge``)."""
    if metric not in _METRICS:
        raise ValueError(f"unknown energy counter metric: {metric!r}")
    return f"{COUNTER_RESET_FLAG_PREFIX}{metric}"


def _kwh(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{label} must be a finite non-negative number or None")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{label} must be a finite non-negative number or None")
    return number


def _percentage(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{label} must be a finite percentage or None")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 100.0:
        raise ValueError(f"{label} must be a percentage in [0, 100] or None")
    return number


@dataclass(frozen=True, slots=True)
class UnitEnergyDay:
    """One unit's figures for one site-day; ``None`` = source absent all day."""

    grid_import_kwh: float | None
    grid_export_kwh: float | None
    battery_charged_kwh: float | None
    battery_discharged_kwh: float | None
    load_kwh: float | None
    charged_from_surplus_kwh: float | None
    coverage_pct: float | None
    metric_flags: frozenset[str] = field(default=frozenset())

    def __post_init__(self) -> None:
        for name in (
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_charged_kwh",
            "battery_discharged_kwh",
            "load_kwh",
            "charged_from_surplus_kwh",
        ):
            _kwh(getattr(self, name), name)
        _percentage(self.coverage_pct, "coverage_pct")
        if type(self.metric_flags) is not frozenset or any(
            not isinstance(flag, str) or not flag.strip() for flag in self.metric_flags
        ):
            raise TypeError("metric_flags must be a frozenset of normalized tokens")
        object.__setattr__(self, "metric_flags", frozenset(self.metric_flags))

    def payload(self) -> dict[str, float | None | list[str]]:
        return {
            "grid_import_kwh": self.grid_import_kwh,
            "grid_export_kwh": self.grid_export_kwh,
            "battery_charged_kwh": self.battery_charged_kwh,
            "battery_discharged_kwh": self.battery_discharged_kwh,
            "load_kwh": self.load_kwh,
            "charged_from_surplus_kwh": self.charged_from_surplus_kwh,
            "coverage_pct": self.coverage_pct,
            "metric_flags": sorted(self.metric_flags),
        }


@dataclass(frozen=True, slots=True)
class FleetEnergyDay:
    """The fleet rollup: the five sums plus charged_from_surplus, and the
    WORST unit's coverage (the evidence-rollup precedence doctrine)."""

    grid_import_kwh: float | None
    grid_export_kwh: float | None
    battery_charged_kwh: float | None
    battery_discharged_kwh: float | None
    load_kwh: float | None
    charged_from_surplus_kwh: float | None
    coverage_pct: float | None

    def __post_init__(self) -> None:
        for name in (
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_charged_kwh",
            "battery_discharged_kwh",
            "load_kwh",
            "charged_from_surplus_kwh",
        ):
            _kwh(getattr(self, name), name)
        _percentage(self.coverage_pct, "coverage_pct")

    def payload(self) -> dict[str, float | None]:
        return {
            "grid_import_kwh": self.grid_import_kwh,
            "grid_export_kwh": self.grid_export_kwh,
            "battery_charged_kwh": self.battery_charged_kwh,
            "battery_discharged_kwh": self.battery_discharged_kwh,
            "load_kwh": self.load_kwh,
            "charged_from_surplus_kwh": self.charged_from_surplus_kwh,
            "coverage_pct": self.coverage_pct,
        }


@dataclass(frozen=True, slots=True)
class CounterCrossCheck:
    """The A-1 passive pinning evidence for one day (DESIGN section 3).

    The vendor-labeled delta of BOTH grid pairs beside the integrated
    bought/sold for the same day and units, plus the day's verdict.  The
    verdict is REPORTED, never applied: a source change is the operator's
    explicit config revision.
    """

    grid_a_delta_kwh: float | None
    grid_b_delta_kwh: float | None
    consistent_with: str | None
    discriminating: bool

    def __post_init__(self) -> None:
        _kwh(self.grid_a_delta_kwh, "grid_a_delta_kwh")
        _kwh(self.grid_b_delta_kwh, "grid_b_delta_kwh")
        if self.consistent_with is not None and self.consistent_with not in {
            VERDICT_VENDOR_LABELS,
            VERDICT_SWAPPED,
            VERDICT_UNDISCRIMINATING,
        }:
            raise ValueError("consistent_with must be a known A-1 verdict or None")
        if type(self.discriminating) is not bool:
            raise TypeError("discriminating must be boolean")

    def payload(self) -> dict[str, float | str | None | bool]:
        return {
            "grid_a_delta_kwh": self.grid_a_delta_kwh,
            "grid_b_delta_kwh": self.grid_b_delta_kwh,
            "consistent_with": self.consistent_with,
            "discriminating": self.discriminating,
        }


@dataclass(frozen=True, slots=True)
class EnergyUnitBaseline:
    """One unit's durable live-day baseline (DESIGN section 5).

    The live day survives a mid-day restart BY DESIGN: the device kept
    counting, so counter deltas resume from the last seen cumulatives, and
    the integration's coverage clock resumes with a gap counted from the
    last capture WALL time (monotonic time never crosses processes).  The
    attribution predicate is deliberately absent -- whether the adviser was
    active before the restart is not the ledger's to claim.
    """

    date: date
    counter_start: Mapping[str, float]
    counter_last: Mapping[str, float]
    import_watt_seconds: float
    export_watt_seconds: float
    surplus_watt_seconds: float
    sampled_seconds: float
    last_capture_wall: datetime | None
    last_grid_watts: float | None
    flags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        from datetime import datetime as _datetime

        if type(self.date) is not date:
            raise TypeError("date must be a civil date")
        for name in ("counter_start", "counter_last"):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                for key, value in values.items()
            ):
                raise TypeError(f"{name} must map metric names to finite numbers")
            object.__setattr__(self, name, _FrozenStringMapping(dict(values)))
        for name in (
            "import_watt_seconds",
            "export_watt_seconds",
            "surplus_watt_seconds",
            "sampled_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.last_capture_wall is not None and (
            not isinstance(self.last_capture_wall, _datetime)
            or self.last_capture_wall.tzinfo is None
        ):
            raise ValueError("last_capture_wall must be a timezone-aware datetime or None")
        if self.last_grid_watts is not None and (
            isinstance(self.last_grid_watts, bool)
            or not isinstance(self.last_grid_watts, int | float)
            or not math.isfinite(float(self.last_grid_watts))
        ):
            raise ValueError("last_grid_watts must be a finite number or None")
        if type(self.flags) is not frozenset or any(
            not isinstance(flag, str) or not flag.strip() for flag in self.flags
        ):
            raise TypeError("flags must be a frozenset of normalized tokens")


def _normalized_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty and normalized")
    return value


def _valid_timezone(value: object) -> str:
    name = _normalized_text(value, "timezone")
    if name == "Local":
        raise ValueError("timezone must be an IANA timezone")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA timezone") from exc
    return name


@dataclass(frozen=True, slots=True)
class EnergyDayRecord:
    """One site-day's scorecard: the snapshot/REST/persistence shape."""

    date: date
    timezone: str
    utc_offset_minutes: int
    kind: str
    units: Mapping[str, UnitEnergyDay]
    fleet: FleetEnergyDay
    sources: Mapping[str, str]
    counter_cross_check: CounterCrossCheck | None
    solar_production_measured: bool

    def __post_init__(self) -> None:
        if type(self.date) is not date:
            raise TypeError("date must be a civil date")
        _valid_timezone(self.timezone)
        if type(self.utc_offset_minutes) is not int or not -1440 <= self.utc_offset_minutes <= 1440:
            raise ValueError("utc_offset_minutes must be within +/- one day of UTC")
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}")
        if not isinstance(self.units, Mapping) or any(
            not isinstance(key, str) or type(value) is not UnitEnergyDay
            for key, value in self.units.items()
        ):
            raise TypeError("units must map unit ids to UnitEnergyDay values")
        if type(self.fleet) is not FleetEnergyDay:
            raise TypeError("fleet must be a FleetEnergyDay")
        expected_sources = {"grid", "battery", "load", "surplus"}
        if not isinstance(self.sources, Mapping) or set(self.sources) != expected_sources:
            raise ValueError("sources must name exactly grid, battery, load, and surplus")
        if self.sources["grid"] not in GRID_SOURCES:
            raise ValueError("the grid source must be a known grid source label")
        for key in ("battery", "load"):
            if self.sources[key] != BATTERY_SOURCE:
                raise ValueError(f"the {key} source is the device counter")
        if self.sources["surplus"] != SURPLUS_SOURCE:
            raise ValueError("the surplus source is the attribution integral")
        if (
            self.counter_cross_check is not None
            and type(self.counter_cross_check) is not CounterCrossCheck
        ):
            raise TypeError("counter_cross_check must be a CounterCrossCheck or None")
        if self.solar_production_measured:
            raise ValueError("solar production is never presented as measured at this site")
        object.__setattr__(self, "units", _FrozenStringMapping(dict(self.units)))
        object.__setattr__(self, "sources", _FrozenStringMapping(dict(self.sources)))

    def payload(self) -> dict[str, object]:
        """The wire/persistence JSON shape (API_CONTRACTS "Energy scorecard")."""
        return {
            "date": self.date.isoformat(),
            "timezone": self.timezone,
            "utc_offset_minutes": self.utc_offset_minutes,
            "kind": self.kind,
            "units": {unit: metrics.payload() for unit, metrics in self.units.items()},
            "fleet": self.fleet.payload(),
            "sources": dict(self.sources),
            "counter_cross_check": (
                None if self.counter_cross_check is None else self.counter_cross_check.payload()
            ),
            "solar_production_measured": self.solar_production_measured,
        }
