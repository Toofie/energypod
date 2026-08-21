"""Deterministic evaluation of versioned civil-time schedules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
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
