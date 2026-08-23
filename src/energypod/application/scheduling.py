"""Deterministic evaluation of versioned civil-time schedules."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from energypod.domain.intents import Direction, IntentSource
from energypod.domain.schedule import ScheduleEntry, SchedulePlan, ScheduleValidationError, Weekday


@dataclass(frozen=True, slots=True)
class ScheduleIntent:
    id: str
    source: IntentSource
    direction: Direction
    watts: int
    unit_ids: frozenset[str]
    created_at_monotonic: float
    duration_s: float
    schedule_version: int
    # DESIGN_SCHEDULES §1: the entry's watt form carries verbatim — the
    # per-unit mapping when the entry was published per battery, None for the
    # scalar fleet-total form.  Per-unit targets stay per-unit caps at
    # allocation exactly as on REST dispatch.
    watts_by_unit: Mapping[str, int] | None = None
    entry_id: str = ""

    @property
    def selected_unit_ids(self) -> frozenset[str]:
        return self.unit_ids

    @property
    def expires_at_monotonic(self) -> float:
        return self.created_at_monotonic + self.duration_s


class ScheduleEvaluator:
    def __init__(self, *, intent_ttl_s: float) -> None:
        if isinstance(intent_ttl_s, bool) or not isinstance(intent_ttl_s, int | float):
            raise ScheduleValidationError("intent TTL must be a finite positive number")
        if not math.isfinite(intent_ttl_s) or intent_ttl_s <= 0:
            raise ScheduleValidationError("intent TTL must be a finite positive number")
        self._intent_ttl_s = float(intent_ttl_s)

    def evaluate(
        self, *, schedule: SchedulePlan, at: datetime, now_monotonic: float
    ) -> ScheduleIntent | None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ScheduleValidationError("evaluation instant must be timezone-aware")
        if isinstance(now_monotonic, bool) or not isinstance(now_monotonic, int | float):
            raise ScheduleValidationError("monotonic time must be finite")
        if not math.isfinite(now_monotonic):
            raise ScheduleValidationError("monotonic time must be finite")
        local = at.astimezone(ZoneInfo(schedule.timezone))
        matches = [entry for entry in schedule.entries if self._matches(entry, local)]
        if not matches:
            return None
        selected = max(matches, key=lambda item: (item.priority, item.entry_id))
        return ScheduleIntent(
            id=f"schedule:{schedule.version}:{selected.entry_id}",
            source=IntentSource.SCHEDULE,
            direction=selected.action,
            watts=selected.watts,
            unit_ids=selected.unit_ids,
            created_at_monotonic=float(now_monotonic),
            duration_s=self._intent_ttl_s,
            schedule_version=schedule.version,
            watts_by_unit=selected.watts_by_unit,
            entry_id=selected.entry_id,
        )

    @staticmethod
    def _matches(entry: ScheduleEntry, local: datetime) -> bool:
        if not entry.enabled:
            return False
        local_time = local.timetz().replace(tzinfo=None)
        start_date = local.date()
        if entry.crosses_midnight and local_time < entry.end_local:
            start_date -= timedelta(days=1)
            inside = True
        elif entry.crosses_midnight:
            inside = local_time >= entry.start_local
        else:
            inside = entry.start_local <= local_time < entry.end_local
        if not inside:
            return False
        return (
            Weekday(start_date.weekday()) in entry.days
            and entry.effective_from <= start_date <= entry.effective_until
        )


# --- the pure next-occurrence helpers (DESIGN_SCHEDULES §5.4) -------------------
#
# ``next_start`` and ``window_end`` are the SINGLE implementation of every
# countdown the surface serves (GET's ``next_action``, the ``schedule_state``
# projection, the Home card); no client reimplements civil-time arithmetic.
# Both are pure, deterministic, and DST-honest through the plan's zone.

# The next-occurrence horizon: 7 weekdays plus the boundary on each side, so a
# weekly window is always found inside the search and a cross-midnight window
# that started on the boundary day is not missed.
_NEXT_OCCURRENCE_HORIZON_DAYS = 8


def next_start(plan: SchedulePlan, at: datetime) -> tuple[ScheduleEntry, datetime] | None:
    """The earliest future start among enabled entries, in the plan's zone.

    Searched over the next 8 days; only starts strictly after ``at`` count (a
    window already open belongs to the evaluator, not to this countdown);
    effective date bounds are honoured on the start date; ties at one instant
    resolve by higher priority then entry id.  ``None`` when no enabled entry
    ever occurs again (all disabled, or past every ``effective_until``).
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ScheduleValidationError("next-occurrence instant must be timezone-aware")
    zone = ZoneInfo(plan.timezone)
    local = at.astimezone(zone)
    # (start instant, -priority, entry id, entry): ties at one instant resolve
    # by HIGHER priority first, then the stable entry id.
    best: tuple[datetime, int, str, ScheduleEntry] | None = None
    horizon = local.date() + timedelta(days=_NEXT_OCCURRENCE_HORIZON_DAYS)
    for entry in plan.entries:
        if not entry.enabled:
            continue
        for offset in range(_NEXT_OCCURRENCE_HORIZON_DAYS + 1):
            day = local.date() + timedelta(days=offset)
            if day > horizon:
                break
            if Weekday(day.weekday()) not in entry.days:
                continue
            if not entry.effective_from <= day <= entry.effective_until:
                continue
            candidate = datetime.combine(day, entry.start_local, tzinfo=zone)
            if candidate <= local:
                continue
            key = (candidate, -entry.priority, entry.entry_id, entry)
            if best is None or key[:3] < best[:3]:
                best = key
    if best is None:
        return None
    return best[3], best[0]


def window_end(entry: ScheduleEntry, at: datetime) -> datetime:
    """The local end instant of the currently matching window (aware).

    ``at`` is the aware evaluation instant, already in the plan's zone (the
    evaluator's own contract); the returned end instant carries the same zone.
    A midnight-crossing window that is currently in its tail ends TODAY at
    ``end_local``; one in its head ends TOMORROW at ``end_local`` — one entry,
    one continuous window, never two.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ScheduleValidationError("window-end instant must be timezone-aware")
    local_time = at.timetz().replace(tzinfo=None)
    end_date = at.date()
    if entry.crosses_midnight and local_time >= entry.start_local:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, entry.end_local, tzinfo=at.tzinfo)


def schedule_wire_entry(entry: ScheduleEntry) -> dict[str, Any]:
    """The §5 wire shape of one entry (the editor renders straight from it)."""
    wire: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "days": [_WIRE_DAYS[int(day)] for day in sorted(entry.days)],
        "start_local": entry.start_local.strftime("%H:%M"),
        "end_local": entry.end_local.strftime("%H:%M"),
        "action": entry.action.value,
        "unit_ids": sorted(entry.unit_ids),
        "effective_from": entry.effective_from.isoformat(),
        "effective_until": entry.effective_until.isoformat(),
        "priority": entry.priority,
        "enabled": entry.enabled,
    }
    if entry.watts_by_unit is not None:
        wire["watts_by_unit"] = dict(sorted(entry.watts_by_unit.items()))
    else:
        wire["watts"] = entry.watts
    return wire


def schedule_wire_plan(plan: SchedulePlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "timezone": plan.timezone,
        "entries": [schedule_wire_entry(entry) for entry in plan.entries],
    }


_WIRE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
