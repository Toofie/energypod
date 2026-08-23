"""Immutable civil-time schedule definitions and validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import IntEnum
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.intents import Direction
from energypod.domain.models import _FrozenStringMapping


class ScheduleValidationError(ValueError):
    """A schedule is ambiguous, malformed, or unsafe to evaluate."""


class ScheduleVersionConflict(RuntimeError):
    """A compare-and-swap schedule update used a stale expected version."""


class Weekday(IntEnum):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ScheduleValidationError(f"{label} must be non-empty without surrounding whitespace")
    return value


# DESIGN_SCHEDULES §1: an entry's effective date range is OPTIONAL — "an
# optional date range it is effective within".  The domain always carries
# concrete bounds (every comparison site stays a plain date comparison); an
# absent wire bound maps onto these sentinels, and the wire layer echoes a
# sentinel bound back as null so GET shows exactly what the operator published.
OPEN_EFFECTIVE_FROM: Final[date] = date.min
OPEN_EFFECTIVE_UNTIL: Final[date] = date.max


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One named weekly window; the entry id IS the operator's name for it.

    The watt form is exactly ``PowerIntent``'s dual form: scalar ``watts``
    (the fleet total, distributed capacity-weighted at allocation — the
    original form, unchanged) OR ``watts_by_unit`` (one positive integer per
    selected unit, key set exactly ``unit_ids``, the fleet total being the
    sum — the 2026-08-23 operator ruling).  ``watts`` always carries the
    fleet total so every existing consumer is unchanged; ``idle`` entries
    carry scalar ``watts: 0`` and never a mapping.
    """

    entry_id: str
    days: frozenset[Weekday]
    start_local: time
    end_local: time
    action: Direction
    watts: int
    unit_ids: frozenset[str]
    effective_from: date
    effective_until: date
    priority: int
    enabled: bool
    watts_by_unit: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        _strict_text(self.entry_id, "entry_id")
        if type(self.days) is not frozenset or not self.days:
            raise ScheduleValidationError("days must be a non-empty frozenset")
        if any(type(day) is not Weekday for day in self.days):
            raise ScheduleValidationError("days must contain Weekday members")
        if type(self.start_local) is not time or type(self.end_local) is not time:
            raise ScheduleValidationError("start and end must be civil times")
        if self.start_local.tzinfo is not None or self.end_local.tzinfo is not None:
            raise ScheduleValidationError("entry times must be timezone-naive civil times")
        if self.start_local == self.end_local:
            raise ScheduleValidationError("zero-length schedule windows are ambiguous")
        if type(self.action) is not Direction:
            raise ScheduleValidationError("action must be a Direction")
        if type(self.watts) is not int or self.watts < 0:
            raise ScheduleValidationError("watts must be a non-negative integer")
        if self.action is Direction.IDLE and self.watts != 0:
            raise ScheduleValidationError("idle entries require zero watts")
        if self.action is not Direction.IDLE and self.watts <= 0:
            raise ScheduleValidationError("active entries require positive watts")
        if type(self.unit_ids) is not frozenset or not self.unit_ids:
            raise ScheduleValidationError("unit_ids must be a non-empty frozenset")
        for unit_id in self.unit_ids:
            _strict_text(unit_id, "unit_id")
        if type(self.effective_from) is not date or type(self.effective_until) is not date:
            raise ScheduleValidationError("effective bounds must be dates")
        if self.effective_from > self.effective_until:
            raise ScheduleValidationError("effective_from must not follow effective_until")
        if type(self.priority) is not int:
            raise ScheduleValidationError("priority must be an integer")
        if type(self.enabled) is not bool:
            raise ScheduleValidationError("enabled must be boolean")
        if self.watts_by_unit is not None:
            # The per-unit form (DESIGN_SCHEDULES §1): one positive integer
            # per selected unit, key set exactly unit_ids, summing to the
            # entry's fleet total, never on an idle entry.  Normalized to the
            # deterministic frozen mapping so equality and hashing follow the
            # value, not dict identity.
            if self.action is Direction.IDLE:
                raise ScheduleValidationError("idle entries carry no per-unit watts")
            if not isinstance(self.watts_by_unit, Mapping):
                raise ScheduleValidationError("watts_by_unit must be a mapping of unit to watts")
            for unit_id, watts in self.watts_by_unit.items():
                _strict_text(unit_id, "watts_by_unit key")
                if type(watts) is not int or watts <= 0:
                    raise ScheduleValidationError("per-unit watts must be positive integers")
            if set(self.watts_by_unit) != set(self.unit_ids):
                raise ScheduleValidationError(
                    "watts_by_unit must name every selected unit and no others"
                )
            if sum(self.watts_by_unit.values()) != self.watts:
                raise ScheduleValidationError("per-unit watts must sum to the fleet total")
            object.__setattr__(
                self, "watts_by_unit", _FrozenStringMapping(dict(self.watts_by_unit))
            )

    @property
    def crosses_midnight(self) -> bool:
        return self.end_local < self.start_local


_DAY_MICROSECONDS = 24 * 60 * 60 * 1_000_000


def _time_offset(value: time) -> int:
    return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond


def _segments(entry: ScheduleEntry) -> tuple[tuple[int, int, int, int], ...]:
    """Return (civil weekday, start, end, offset from the window's start date)."""
    result: list[tuple[int, int, int, int]] = []
    for day in entry.days:
        if entry.crosses_midnight:
            result.append((int(day), _time_offset(entry.start_local), _DAY_MICROSECONDS, 0))
            result.append(((int(day) + 1) % 7, 0, _time_offset(entry.end_local), 1))
        else:
            result.append(
                (int(day), _time_offset(entry.start_local), _time_offset(entry.end_local), 0)
            )
    return tuple(result)


def _range_contains_weekday(start: date, end: date, weekday: int) -> bool:
    if start > end:
        return False
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    return first <= end


def _windows_overlap(left: ScheduleEntry, right: ScheduleEntry) -> bool:
    for left_day, left_start, left_end, left_offset in _segments(left):
        for right_day, right_start, right_end, right_offset in _segments(right):
            if left_day != right_day or left_start >= right_end or right_start >= left_end:
                continue
            actual_start = max(
                left.effective_from + timedelta(days=left_offset),
                right.effective_from + timedelta(days=right_offset),
            )
            actual_end = min(
                left.effective_until + timedelta(days=left_offset),
                right.effective_until + timedelta(days=right_offset),
            )
            if _range_contains_weekday(actual_start, actual_end, left_day):
                return True
    return False


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    version: int
    timezone: str
    entries: tuple[ScheduleEntry, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ScheduleValidationError("version must be a positive integer")
        _strict_text(self.timezone, "timezone")
        if self.timezone == "Local":
            raise ScheduleValidationError("timezone must be an IANA timezone")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ScheduleValidationError("timezone must be an IANA timezone") from exc
        if type(self.entries) is not tuple or any(
            type(entry) is not ScheduleEntry for entry in self.entries
        ):
            raise ScheduleValidationError("entries must be a tuple of ScheduleEntry values")
        ids = [entry.entry_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ScheduleValidationError("duplicate schedule entry id")
        enabled = [entry for entry in self.entries if entry.enabled]
        for index, left in enumerate(enabled):
            for right in enabled[index + 1 :]:
                if left.priority == right.priority and _windows_overlap(left, right):
                    raise ScheduleValidationError(
                        f"same-priority schedule entries overlap: {left.entry_id}, {right.entry_id}"
                    )
