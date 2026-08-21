"""Deterministic contract tests for civil-time schedule evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

try:
    from energypod.application.scheduling import ScheduleEvaluator
    from energypod.domain.intents import Direction, IntentSource
    from energypod.domain.schedule import (
        ScheduleEntry,
        SchedulePlan,
        ScheduleValidationError,
        Weekday,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    ScheduleEvaluator: Any = None
    Direction: Any = None
    IntentSource: Any = None
    ScheduleEntry: Any = None
    SchedulePlan: Any = None
    ScheduleValidationError: Any = None
    Weekday: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The intended schedule contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _entry(
    *,
    entry_id: str = "weekday-charge",
    days: frozenset[Any] | None = None,
    start: time = time(9, 0),
    end: time = time(10, 0),
    action: Any = None,
    watts: int = 1800,
    units: frozenset[str] = frozenset({"mid", "rhs"}),
    effective_from: date = date(2026, 1, 1),
    effective_until: date = date(2026, 12, 31),
    priority: int = 10,
    enabled: bool = True,
) -> Any:
    _require_contract()
    if days is None:
        days = frozenset({Weekday.MONDAY})
    if action is None:
        action = Direction.CHARGE
    return ScheduleEntry(
        entry_id=entry_id,
        days=days,
        start_local=start,
        end_local=end,
        action=action,
        watts=watts,
        unit_ids=units,
        effective_from=effective_from,
        effective_until=effective_until,
        priority=priority,
        enabled=enabled,
    )


def _plan(*entries: Any, timezone_name: str = "Australia/Brisbane", version: int = 1) -> Any:
    _require_contract()
    return SchedulePlan(version=version, timezone=timezone_name, entries=tuple(entries))


def _evaluate(plan: Any, at: datetime, *, now_monotonic: float = 50.0) -> Any:
    _require_contract()
    evaluator = ScheduleEvaluator(intent_ttl_s=0.5)
    return evaluator.evaluate(schedule=plan, at=at, now_monotonic=now_monotonic)


def test_schedule_uses_its_iana_timezone_not_host_timezone() -> None:
    """T-UNIT-SCHED-001 / REQ-SCHED-001 / S1."""
    plan = _plan(_entry(start=time(0, 0), end=time(1, 0)))
    sunday_utc = datetime(2026, 1, 4, 14, 30, tzinfo=UTC)

    intent = _evaluate(plan, sunday_utc)

    assert intent is not None
    assert intent.direction is Direction.CHARGE


def test_schedule_window_is_start_inclusive_and_end_exclusive() -> None:
    """T-UNIT-SCHED-002 / REQ-SCHED-002 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(_entry())

    assert _evaluate(plan, datetime(2026, 1, 5, 9, 0, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 5, 9, 59, 59, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 5, 10, 0, tzinfo=zone)) is None


def test_day_filter_applies_in_schedule_local_time() -> None:
    """T-UNIT-SCHED-003 / REQ-SCHED-003 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(_entry(days=frozenset({Weekday.MONDAY})))

    assert _evaluate(plan, datetime(2026, 1, 5, 9, 30, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 6, 9, 30, tzinfo=zone)) is None


def test_effective_date_range_is_inclusive() -> None:
    """T-UNIT-SCHED-004 / REQ-SCHED-004 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    entry = _entry(
        days=frozenset({Weekday.MONDAY}),
        effective_from=date(2026, 1, 5),
        effective_until=date(2026, 1, 12),
    )
    plan = _plan(entry)

    assert _evaluate(plan, datetime(2025, 12, 29, 9, 30, tzinfo=zone)) is None
    assert _evaluate(plan, datetime(2026, 1, 5, 9, 30, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 12, 9, 30, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 19, 9, 30, tzinfo=zone)) is None


def test_cross_midnight_tail_belongs_to_window_start_day() -> None:
    """T-UNIT-SCHED-005 / REQ-SCHED-005 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(
        _entry(
            days=frozenset({Weekday.MONDAY}),
            start=time(23, 0),
            end=time(2, 0),
        )
    )

    assert _evaluate(plan, datetime(2026, 1, 5, 23, 30, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 6, 1, 59, tzinfo=zone)) is not None
    assert _evaluate(plan, datetime(2026, 1, 6, 2, 0, tzinfo=zone)) is None
    assert _evaluate(plan, datetime(2026, 1, 7, 1, 0, tzinfo=zone)) is None


def test_cross_midnight_tail_uses_start_days_effective_date() -> None:
    """T-UNIT-SCHED-006 / REQ-SCHED-006 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(
        _entry(
            days=frozenset({Weekday.MONDAY}),
            start=time(23, 0),
            end=time(2, 0),
            effective_from=date(2026, 1, 5),
            effective_until=date(2026, 1, 5),
        )
    )

    assert _evaluate(plan, datetime(2026, 1, 6, 1, 0, tzinfo=zone)) is not None


def test_spring_dst_gap_is_evaluated_from_real_instants() -> None:
    """T-UNIT-SCHED-007 / REQ-SCHED-007 / S1."""
    plan = _plan(
        _entry(
            days=frozenset({Weekday.SUNDAY}),
            start=time(2, 30),
            end=time(3, 30),
            effective_from=date(2026, 3, 8),
            effective_until=date(2026, 3, 8),
        ),
        timezone_name="America/New_York",
    )

    # 02:30 never occurs. The first matching real instant is 03:00 local.
    assert _evaluate(plan, datetime(2026, 3, 8, 6, 59, tzinfo=UTC)) is None
    assert _evaluate(plan, datetime(2026, 3, 8, 7, 0, tzinfo=UTC)) is not None
    assert _evaluate(plan, datetime(2026, 3, 8, 7, 15, tzinfo=UTC)) is not None
    assert _evaluate(plan, datetime(2026, 3, 8, 7, 30, tzinfo=UTC)) is None


def test_autumn_dst_fold_covers_both_real_occurrences() -> None:
    """T-UNIT-SCHED-008 / REQ-SCHED-008 / S1."""
    plan = _plan(
        _entry(
            days=frozenset({Weekday.SUNDAY}),
            start=time(1, 0),
            end=time(2, 0),
            effective_from=date(2026, 11, 1),
            effective_until=date(2026, 11, 1),
        ),
        timezone_name="America/New_York",
    )

    first_0130 = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    second_0130 = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert _evaluate(plan, first_0130) is not None
    assert _evaluate(plan, second_0130) is not None
    assert _evaluate(plan, datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) is None


def test_brisbane_has_stable_offset_in_summer_and_winter() -> None:
    """T-UNIT-SCHED-009 / REQ-SCHED-009 / S2."""
    zone = ZoneInfo("Australia/Brisbane")
    assert datetime(2026, 1, 1, tzinfo=zone).utcoffset().total_seconds() == 10 * 3600
    assert datetime(2026, 7, 1, tzinfo=zone).utcoffset().total_seconds() == 10 * 3600


@pytest.mark.parametrize("timezone_name", ["", "Local", "Australia/NotAZone"])
def test_invalid_or_non_iana_timezone_is_rejected(timezone_name: str) -> None:
    """T-UNIT-SCHED-010 / REQ-SCHED-010 / S1."""
    _require_contract()
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _plan(_entry(), timezone_name=timezone_name)


def test_naive_evaluation_instant_is_rejected() -> None:
    """T-UNIT-SCHED-011 / REQ-SCHED-011 / S1."""
    _require_contract()
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _evaluate(_plan(_entry()), datetime(2026, 1, 5, 9, 30))


@pytest.mark.parametrize(
    "entries",
    [
        (
            {"entry_id": "a", "start": time(9, 0), "end": time(11, 0)},
            {"entry_id": "b", "start": time(10, 0), "end": time(12, 0)},
        ),
        (
            {"entry_id": "a", "start": time(9, 0), "end": time(12, 0)},
            {"entry_id": "b", "start": time(10, 0), "end": time(11, 0)},
        ),
        (
            {"entry_id": "a", "start": time(9, 0), "end": time(10, 0)},
            {"entry_id": "b", "start": time(9, 0), "end": time(10, 0)},
        ),
    ],
)
def test_same_priority_overlaps_are_rejected(entries: tuple[dict[str, Any], ...]) -> None:
    """T-UNIT-SCHED-012 / REQ-SCHED-012 / S1."""
    _require_contract()
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _plan(*(_entry(**spec) for spec in entries))


def test_cross_midnight_overlap_with_following_day_is_rejected() -> None:
    """T-UNIT-SCHED-013 / REQ-SCHED-013 / S1."""
    _require_contract()
    assert ScheduleValidationError is not None
    overnight = _entry(
        entry_id="overnight",
        days=frozenset({Weekday.MONDAY}),
        start=time(23, 0),
        end=time(2, 0),
    )
    tuesday = _entry(
        entry_id="tuesday",
        days=frozenset({Weekday.TUESDAY}),
        start=time(1, 0),
        end=time(3, 0),
    )

    with pytest.raises(ScheduleValidationError):
        _plan(overnight, tuesday)


def test_cross_midnight_overlap_is_detected_across_week_boundary() -> None:
    """T-UNIT-SCHED-013A / REQ-SCHED-013 / S1."""
    overnight = _entry(
        entry_id="sunday-night",
        days=frozenset({Weekday.SUNDAY}),
        start=time(23, 0),
        end=time(2, 0),
    )
    monday = _entry(
        entry_id="monday-tail",
        days=frozenset({Weekday.MONDAY}),
        start=time(1, 0),
        end=time(3, 0),
    )

    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _plan(overnight, monday)


def test_identical_civil_times_with_disjoint_effective_dates_do_not_overlap() -> None:
    """T-UNIT-SCHED-013B / REQ-SCHED-012 / S1: overlap uses full validity."""
    old = _entry(
        entry_id="old",
        effective_from=date(2026, 1, 1),
        effective_until=date(2026, 6, 30),
    )
    new = _entry(
        entry_id="new",
        effective_from=date(2026, 7, 1),
        effective_until=date(2026, 12, 31),
    )

    assert len(_plan(old, new).entries) == 2


def test_adjacent_windows_do_not_overlap() -> None:
    """T-UNIT-SCHED-014 / REQ-SCHED-014 / S1."""
    plan = _plan(
        _entry(entry_id="first", start=time(9, 0), end=time(10, 0)),
        _entry(entry_id="second", start=time(10, 0), end=time(11, 0)),
    )

    assert len(plan.entries) == 2


def test_different_priorities_may_overlap_and_highest_priority_wins() -> None:
    """T-UNIT-SCHED-015 / REQ-SCHED-015 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    low = _entry(entry_id="low", priority=10, action=Direction.CHARGE, watts=1000)
    high = _entry(entry_id="high", priority=20, action=Direction.DISCHARGE, watts=700)

    intent = _evaluate(_plan(low, high), datetime(2026, 1, 5, 9, 30, tzinfo=zone))

    assert intent.direction is Direction.DISCHARGE
    assert intent.watts == 700


@pytest.mark.parametrize(
    ("action_name", "watts"),
    [("CHARGE", 1800), ("DISCHARGE", 1200), ("IDLE", 0)],
)
def test_action_power_and_unit_scope_survive_schedule_to_intent(
    action_name: str, watts: int
) -> None:
    """T-UNIT-SCHED-016 / INV-SCHED-001 / S1: action is never dropped or inverted."""
    _require_contract()
    zone = ZoneInfo("Australia/Brisbane")
    action = getattr(Direction, action_name)
    intent = _evaluate(
        _plan(_entry(action=action, watts=watts, units=frozenset({"lhs", "mid"})), version=9),
        datetime(2026, 1, 5, 9, 30, tzinfo=zone),
        now_monotonic=123.0,
    )

    assert intent is not None
    assert intent.source is IntentSource.SCHEDULE
    assert intent.direction is action
    assert intent.watts == watts
    assert intent.unit_ids == frozenset({"lhs", "mid"})
    assert intent.created_at_monotonic == 123.0
    assert intent.duration_s == 0.5
    assert intent.schedule_version == 9


def test_disabled_entry_never_produces_an_intent() -> None:
    """T-UNIT-SCHED-017 / REQ-SCHED-016 / S1."""
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(_entry(enabled=False))

    assert _evaluate(plan, datetime(2026, 1, 5, 9, 30, tzinfo=zone)) is None


def test_entry_input_order_does_not_change_resolution() -> None:
    """T-UNIT-SCHED-018 / INV-SCHED-002 / S1: evaluation is deterministic."""
    zone = ZoneInfo("Australia/Brisbane")
    low = _entry(entry_id="low", priority=10, action=Direction.CHARGE, watts=1000)
    high = _entry(entry_id="high", priority=20, action=Direction.DISCHARGE, watts=900)
    instant = datetime(2026, 1, 5, 9, 30, tzinfo=zone)

    first = _evaluate(_plan(low, high), instant)
    second = _evaluate(_plan(high, low), instant)

    assert first.direction is second.direction is Direction.DISCHARGE
    assert first.watts == second.watts == 900


@pytest.mark.parametrize(
    "entry",
    [
        {"start": time(9, 0), "end": time(9, 0)},
        {"effective_from": date(2026, 2, 1), "effective_until": date(2026, 1, 31)},
        {"action": "IDLE", "watts": 1},
        {"units": frozenset()},
    ],
)
def test_ambiguous_or_non_actionable_entries_are_rejected(entry: dict[str, Any]) -> None:
    """T-UNIT-SCHED-019 / REQ-SCHED-017 / S1."""
    _require_contract()
    assert ScheduleValidationError is not None
    if entry.get("action") == "IDLE":
        entry["action"] = Direction.IDLE
    with pytest.raises(ScheduleValidationError):
        _entry(**entry)


def test_duplicate_entry_ids_are_rejected_even_when_windows_do_not_overlap() -> None:
    """T-UNIT-SCHED-020 / INV-SCHED-003 / S1: IDs are stable versioned identities."""
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _plan(
            _entry(entry_id="duplicate", start=time(9, 0), end=time(10, 0)),
            _entry(entry_id="duplicate", start=time(11, 0), end=time(12, 0)),
        )
