"""Deterministic contract tests for civil-time schedule evaluation."""

from __future__ import annotations

import json
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


# --- DESIGN_SCHEDULES §1 B1: the per-battery watt form (the PowerIntent rules) ---


def _per_unit_entry(**overrides: Any) -> Any:
    _require_contract()
    values: dict[str, Any] = {
        "entry_id": "per-battery",
        "days": frozenset({Weekday.MONDAY}),
        "start_local": time(9, 0),
        "end_local": time(10, 0),
        "action": Direction.CHARGE,
        "watts": 5_000,
        "unit_ids": frozenset({"lhs", "mid", "rhs"}),
        "effective_from": date(2026, 1, 1),
        "effective_until": date(2026, 12, 31),
        "priority": 10,
        "enabled": True,
        "watts_by_unit": {"lhs": 1_500, "mid": 2_000, "rhs": 1_500},
    }
    values.update(overrides)
    return ScheduleEntry(**values)


def test_per_unit_watts_form_is_the_native_v1_form() -> None:
    """One positive integer per selected unit; the fleet total is the sum."""
    entry = _per_unit_entry()
    assert entry.watts_by_unit is not None
    assert dict(entry.watts_by_unit) == {"lhs": 1_500, "mid": 2_000, "rhs": 1_500}
    assert entry.watts == 5_000


def test_per_unit_keys_must_be_exactly_the_selected_units() -> None:
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts_by_unit={"lhs": 1_500, "mid": 2_000})
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts_by_unit={"lhs": 1_000, "mid": 2_000, "rhs": 1_500, "spare": 500})


def test_per_unit_values_must_be_positive_integers() -> None:
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts_by_unit={"lhs": 0, "mid": 2_000, "rhs": 1_500}, watts=3_500)
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts_by_unit={"lhs": -100, "mid": 2_000, "rhs": 1_500}, watts=3_400)
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts_by_unit={"lhs": True, "mid": 2_000, "rhs": 1_500}, watts=3_501)


def test_per_unit_watts_must_sum_to_the_fleet_total() -> None:
    """Exactly the PowerIntent rule: the scalar stays the fleet total."""
    assert ScheduleValidationError is not None
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts=4_999)
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(watts=5_001)


def test_idle_entries_carry_scalar_zero_and_never_a_mapping() -> None:
    assert ScheduleValidationError is not None
    idle = _per_unit_entry(action=Direction.IDLE, watts=0, watts_by_unit=None)
    assert idle.watts == 0 and idle.watts_by_unit is None
    with pytest.raises(ScheduleValidationError):
        _per_unit_entry(action=Direction.IDLE, watts_by_unit={"lhs": 0, "mid": 0, "rhs": 0})


def test_per_unit_form_survives_the_evaluator_verbatim() -> None:
    """The entry's form carries onto the evaluated intent, per unit."""
    zone = ZoneInfo("Australia/Brisbane")
    evaluator = ScheduleEvaluator(intent_ttl_s=0.5)
    intent = evaluator.evaluate(
        schedule=_plan(_per_unit_entry(), version=4),
        at=datetime(2026, 1, 5, 9, 30, tzinfo=zone),
        now_monotonic=10.0,
    )
    assert intent is not None
    assert intent.watts_by_unit is not None
    assert dict(intent.watts_by_unit) == {"lhs": 1_500, "mid": 2_000, "rhs": 1_500}
    assert intent.watts == 5_000
    assert intent.entry_id == "per-battery"


def test_scalar_entries_keep_their_exact_prior_behavior() -> None:
    """The extension is additive: every scalar entry decodes unchanged."""
    zone = ZoneInfo("Australia/Brisbane")
    evaluator = ScheduleEvaluator(intent_ttl_s=0.5)
    intent = evaluator.evaluate(
        schedule=_plan(_entry(), version=2),
        at=datetime(2026, 1, 5, 9, 30, tzinfo=zone),
        now_monotonic=10.0,
    )
    assert intent is not None
    assert intent.watts_by_unit is None
    assert intent.watts == 1_800


def test_per_unit_entries_round_trip_through_the_durable_payload() -> None:
    """The SQLite contract: both forms encode and decode losslessly."""
    from energypod.adapters.persistence.sqlite import SQLiteScheduleRepository

    store = SQLiteScheduleRepository.__new__(SQLiteScheduleRepository)
    plan = _plan(
        _per_unit_entry(),
        _entry(entry_id="scalar", start=time(11, 0), end=time(12, 0)),
        version=3,
    )
    decoded = store._decode(store._encode(plan))
    assert decoded == plan
    per_unit, scalar = decoded.entries
    assert dict(per_unit.watts_by_unit or {}) == {"lhs": 1_500, "mid": 2_000, "rhs": 1_500}
    assert scalar.watts_by_unit is None


def test_scalar_rows_written_before_the_extension_still_decode() -> None:
    """A durable payload without the watts_by_unit key decodes unchanged."""
    from energypod.adapters.persistence.sqlite import SQLiteScheduleRepository

    legacy_payload = json.dumps(
        {
            "version": 2,
            "timezone": "Australia/Brisbane",
            "entries": [
                {
                    "entry_id": "old",
                    "days": [0],
                    "start_local": "09:00:00",
                    "end_local": "10:00:00",
                    "action": "charge",
                    "watts": 1800,
                    "unit_ids": ["mid"],
                    "effective_from": "2026-01-01",
                    "effective_until": "2026-12-31",
                    "priority": 10,
                    "enabled": True,
                }
            ],
        }
    )
    store = SQLiteScheduleRepository.__new__(SQLiteScheduleRepository)
    decoded = store._decode(legacy_payload)
    assert decoded.entries[0].watts_by_unit is None
    assert decoded.entries[0].watts == 1800


# --- DESIGN_SCHEDULES §5.4 B1: the pure next-occurrence helpers -----------------


def _helpers() -> Any:
    _require_contract()
    from energypod.application import scheduling

    return scheduling


def test_next_start_finds_the_earliest_future_occurrence() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(_entry(days=frozenset({Weekday.MONDAY, Weekday.THURSDAY})))

    found = helpers.next_start(plan, datetime(2026, 1, 5, 8, 0, tzinfo=zone))

    assert found is not None
    entry, starts_at = found
    assert entry.entry_id == "weekday-charge"
    assert starts_at == datetime(2026, 1, 5, 9, 0, tzinfo=zone)


def test_next_start_skips_a_window_already_open_and_finds_next_week() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    plan = _plan(_entry(days=frozenset({Weekday.MONDAY})))

    found = helpers.next_start(plan, datetime(2026, 1, 5, 9, 30, tzinfo=zone))

    assert found is not None
    assert found[1] == datetime(2026, 1, 12, 9, 0, tzinfo=zone)


def test_next_start_honours_effective_bounds_on_the_start_date() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    entry = _entry(
        days=frozenset({Weekday.MONDAY}),
        effective_from=date(2026, 1, 5),
        effective_until=date(2026, 1, 5),
    )
    plan = _plan(entry)

    # A start date still inside the effective bounds is a future occurrence
    # even when `at` precedes the bounds entirely.
    before = helpers.next_start(plan, datetime(2025, 12, 29, 8, 0, tzinfo=zone))
    inside = helpers.next_start(plan, datetime(2026, 1, 5, 8, 0, tzinfo=zone))
    after = helpers.next_start(plan, datetime(2026, 1, 5, 10, 0, tzinfo=zone))
    past = helpers.next_start(plan, datetime(2026, 1, 12, 8, 0, tzinfo=zone))

    assert before is not None and before[1] == datetime(2026, 1, 5, 9, 0, tzinfo=zone)
    assert inside is not None and inside[1] == datetime(2026, 1, 5, 9, 0, tzinfo=zone)
    assert after is None
    assert past is None


def test_next_start_ignores_disabled_entries_and_reports_none_when_nothing_comes() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    assert (
        helpers.next_start(_plan(_entry(enabled=False)), datetime(2026, 1, 5, 8, 0, tzinfo=zone))
        is None
    )


def test_next_start_resolves_same_instant_ties_by_priority_then_id() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    low = _entry(entry_id="a-low", priority=10)
    high = _entry(entry_id="b-high", priority=20)
    # Same window, different priorities: overlaps are legal across priorities.
    plan = _plan(low, high)

    found = helpers.next_start(plan, datetime(2026, 1, 5, 8, 0, tzinfo=zone))

    assert found is not None
    assert found[0].entry_id == "b-high"


def test_next_start_is_dst_honest_through_the_plan_zone() -> None:
    """A 02:30 window on the US spring-forward Sunday still reports its day."""
    helpers = _helpers()
    zone = ZoneInfo("America/New_York")
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

    found = helpers.next_start(plan, datetime(2026, 3, 7, 12, 0, tzinfo=zone))

    assert found is not None
    assert found[1].date() == date(2026, 3, 8)
    assert found[1].timetz().replace(tzinfo=None) == time(2, 30)


def test_window_end_returns_the_local_end_of_the_matching_window() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    entry = _entry()

    ends_at = helpers.window_end(entry, datetime(2026, 1, 5, 9, 30, tzinfo=zone))

    assert ends_at == datetime(2026, 1, 5, 10, 0, tzinfo=zone)


def test_window_end_of_a_cross_midnight_tail_ends_today() -> None:
    helpers = _helpers()
    zone = ZoneInfo("Australia/Brisbane")
    overnight = _entry(days=frozenset({Weekday.MONDAY}), start=time(23, 0), end=time(2, 0))

    head = helpers.window_end(overnight, datetime(2026, 1, 5, 23, 30, tzinfo=zone))
    tail = helpers.window_end(overnight, datetime(2026, 1, 6, 1, 0, tzinfo=zone))

    assert head == datetime(2026, 1, 6, 2, 0, tzinfo=zone)
    assert tail == datetime(2026, 1, 6, 2, 0, tzinfo=zone)


# --- DESIGN_SCHEDULES §2 B2: the ScheduleRunner state machine -------------------

from dataclasses import dataclass, field  # noqa: E402


@dataclass
class RunnerClock:
    mono: float = 50.0
    wall: datetime = field(
        default_factory=lambda: datetime(2026, 1, 5, 9, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
    )

    def monotonic(self) -> float:
        return self.mono

    def wall_now(self) -> datetime:
        return self.wall


@dataclass
class RunnerStore:
    plan: Any = None

    async def get(self) -> Any:
        return self.plan


@dataclass
class RunnerSubmit:
    submissions: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        if self.fail:
            raise OSError("submit unavailable")
        self.submissions.append(
            {
                "unit_ids": list(unit_ids),
                "direction": getattr(direction, "value", direction),
                "watts": watts,
                "ttl_s": ttl_s,
                "watts_by_unit": dict(watts_by_unit) if watts_by_unit else None,
            }
        )
        return {"intent_id": f"schedule-{len(self.submissions)}"}


@dataclass
class RunnerIntents:
    live: list[Any] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return tuple(self.live)

    async def remove(self, intent_id: str) -> None:
        self.removed.append(intent_id)


@dataclass
class RunnerBus:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, body: Any) -> int:
        self.events.append(dict(body))
        return len(self.events)


def _make_runner(
    *,
    plan: Any = None,
    clock: RunnerClock | None = None,
    submit: RunnerSubmit | None = None,
    intents: RunnerIntents | None = None,
    bus: RunnerBus | None = None,
    ttl_s: float = 10.0,
) -> tuple[Any, RunnerStore, RunnerClock, RunnerSubmit, RunnerIntents, RunnerBus]:
    scheduling = _helpers()
    store = RunnerStore(plan=plan)
    runner_clock = clock or RunnerClock()
    runner_submit = submit or RunnerSubmit()
    runner_intents = intents or RunnerIntents()
    runner_bus = bus or RunnerBus()
    runner = scheduling.ScheduleRunner(
        store=store,
        evaluator=scheduling.ScheduleEvaluator(intent_ttl_s=ttl_s),
        clock=runner_clock,
        submit=runner_submit,
        intents=runner_intents,
        bus=runner_bus,
    )
    return runner, store, runner_clock, runner_submit, runner_intents, runner_bus


def _monday_window(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "entry_id": "day-charge",
        "days": frozenset({Weekday.MONDAY}),
        "start": time(9, 0),
        "end": time(17, 0),
        "watts": 3_000,
        "units": frozenset({"lhs", "mid", "rhs"}),
    }
    values.update(overrides)
    return _entry(**values)


async def test_runner_opens_a_window_and_submits_one_schedule_intent() -> None:
    plan = _plan(_monday_window(), version=3)
    runner, *_rig = _make_runner(plan=plan)

    await runner.tick()

    submit = _rig[2]
    assert len(submit.submissions) == 1
    assert submit.submissions[0]["direction"] == "charge"
    assert submit.submissions[0]["watts"] == 3_000
    assert submit.submissions[0]["ttl_s"] == 10.0
    assert runner.held_intent_id == "schedule-1"
    state = runner.state_payload()
    assert state["active"] is True
    assert state["entry_id"] == "day-charge"
    assert state["version"] == 3
    assert state["last_action"] == "submit"
    assert state["reason_codes"] == ["window_open"]
    assert state["ends_at"] is not None and state["ends_in_s"] > 0


async def test_runner_publishes_window_opened_with_the_entry_facts() -> None:
    plan = _plan(_monday_window(), version=2)
    runner, _store, _clock, _submit, _intents, bus = _make_runner(plan=plan)

    await runner.tick()

    opened = [event for event in bus.events if event["type"] == "schedule_window.opened"]
    assert len(opened) == 1
    assert opened[0]["payload"]["entry_id"] == "day-charge"
    assert opened[0]["payload"]["version"] == 2
    assert opened[0]["payload"]["action"] == "charge"
    assert opened[0]["payload"]["watts"] == 3_000
    assert opened[0]["payload"]["unit_ids"] == ["lhs", "mid", "rhs"]
    assert opened[0]["payload"]["ends_at"].startswith("2026-01-05T17:00")


async def test_runner_renews_by_remove_then_submit_with_exactly_one_live() -> None:
    plan = _plan(_monday_window(), version=3)
    runner, _store, _clock, submit, intents, bus = _make_runner(plan=plan)

    await runner.tick()
    await runner.tick()
    await runner.tick()

    # The named invariant: one held intent at a time; every renewal removes
    # the previous id BEFORE submitting the fresh one.
    assert len(submit.submissions) == 3
    assert intents.removed == ["schedule-1", "schedule-2"]
    assert runner.held_intent_id == "schedule-3"
    assert runner.state_payload()["last_action"] == "renew"
    # "opened" publishes on the FIRST submit for a window key only.
    assert len([e for e in bus.events if e["type"] == "schedule_window.opened"]) == 1


async def test_runner_closes_the_window_at_window_end_by_removal_only() -> None:
    plan = _plan(_monday_window(), version=3)
    runner, store, clock, submit, _intents, bus = _make_runner(plan=plan)
    await runner.tick()
    assert submit.submissions

    clock.wall = datetime(2026, 1, 5, 17, 0, tzinfo=ZoneInfo("Australia/Brisbane"))
    await runner.tick()

    state = runner.state_payload()
    assert runner.held_intent_id is None
    assert state["active"] is False
    assert state["last_action"] == "remove"
    assert state["reason_codes"] == ["window_ended"]
    closing = [event for event in bus.events if event["type"] == "schedule_window.closing"]
    assert len(closing) == 1
    assert closing[0]["payload"]["reason"] == "window_ended"
    assert closing[0]["payload"]["entry_id"] == "day-charge"
    assert len(submit.submissions) == 1


async def test_runner_reports_no_plan_and_closes_with_reason_no_plan() -> None:
    plan = _plan(_monday_window(), version=1)
    runner, store, _clock, _submit, _intents, bus = _make_runner(plan=plan)
    await runner.tick()

    store.plan = None
    await runner.tick()

    state = runner.state_payload()
    assert state["active"] is False
    assert state["reason_codes"] == ["no_plan"]
    closing = [event for event in bus.events if event["type"] == "schedule_window.closing"]
    assert closing[-1]["payload"]["reason"] == "no_plan"


async def test_runner_rekeys_when_a_publish_lands_mid_window() -> None:
    plan = _plan(_monday_window(), version=1)
    runner, store, clock, submit, _intents, bus = _make_runner(plan=plan)
    await runner.tick()

    edited = _monday_window(watts=2_400)
    store.plan = _plan(edited, version=2)
    clock.mono += 1.0
    await runner.tick()

    # A same-entry watts edit takes effect the same tick: the version inside
    # the key forces the remove-and-resubmit.
    assert len(submit.submissions) == 2
    assert submit.submissions[-1]["watts"] == 2_400
    assert runner.state_payload()["version"] == 2


async def test_runner_names_plan_changed_when_a_publish_removes_the_running_entry() -> None:
    plan = _plan(_monday_window(), version=1)
    runner, store, _clock, _submit, _intents, bus = _make_runner(plan=plan)
    await runner.tick()

    store.plan = _plan(
        _monday_window(entry_id="other", start=time(20, 0), end=time(21, 0)), version=2
    )
    await runner.tick()

    state = runner.state_payload()
    assert state["active"] is False
    assert state["reason_codes"] == ["plan_changed"]
    closing = [event for event in bus.events if event["type"] == "schedule_window.closing"]
    assert closing[-1]["payload"]["reason"] == "plan_replaced"


async def test_runner_waits_under_a_higher_priority_intent_without_withdrawing() -> None:
    from types import SimpleNamespace

    from energypod.domain import IntentSource

    plan = _plan(_monday_window(), version=1)
    runner, _store, _clock, submit, intents, _bus = _make_runner(plan=plan)
    # A manual intent claims every unit of the window, live through the tick.
    intents.live = [
        SimpleNamespace(
            id="manual-1",
            source=IntentSource.MANUAL,
            selected_unit_ids=frozenset({"lhs", "mid", "rhs"}),
        )
    ]

    await runner.tick()

    # The window OPENS regardless — the runner never checks claims to decide
    # control — and the projection says honestly that it is waiting.
    assert len(submit.submissions) == 1
    assert runner.state_payload()["reason_codes"] == [
        "window_open",
        "waiting_for_higher_priority",
    ]


async def test_runner_waiting_never_fires_when_only_one_unit_is_claimed() -> None:
    from types import SimpleNamespace

    from energypod.domain import IntentSource

    plan = _plan(_monday_window(), version=1)
    runner, _store, _clock, _submit, _intents, _bus = _make_runner(plan=plan)
    runner._intents.live = [
        SimpleNamespace(
            id="manual-1",
            source=IntentSource.MANUAL,
            selected_unit_ids=frozenset({"lhs"}),
        )
    ]

    await runner.tick()

    assert runner.state_payload()["reason_codes"] == ["window_open"]


async def test_runner_carries_per_unit_watts_verbatim_onto_the_intent() -> None:
    entry = ScheduleEntry(
        entry_id="per-battery",
        days=frozenset({Weekday.MONDAY}),
        start_local=time(9, 0),
        end_local=time(17, 0),
        action=Direction.CHARGE,
        watts=6_000,
        unit_ids=frozenset({"lhs", "mid", "rhs"}),
        effective_from=date(2026, 1, 1),
        effective_until=date(2026, 12, 31),
        priority=10,
        enabled=True,
        watts_by_unit={"lhs": 2_000, "mid": 2_000, "rhs": 2_000},
    )
    plan = _plan(entry, version=5)
    runner, _store, _clock, submit, _intents, bus = _make_runner(plan=plan)

    await runner.tick()

    assert submit.submissions[0]["watts"] == 6_000
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 2_000, "mid": 2_000, "rhs": 2_000}
    opened = next(e for e in bus.events if e["type"] == "schedule_window.opened")
    assert opened["payload"]["watts_by_unit"] == {"lhs": 2_000, "mid": 2_000, "rhs": 2_000}


async def test_a_dead_runner_hands_back_by_ttl_lapse() -> None:
    """The fail-safe: if the runner stops ticking mid-window, nothing renews
    and the intent dies by its own TTL (the watchdog hand-back)."""
    plan = _plan(_monday_window(), version=1)
    runner, _store, _clock, submit, _intents, _bus = _make_runner(plan=plan, ttl_s=10.0)

    await runner.tick()

    submission = submit.submissions[0]
    assert submission["ttl_s"] == 10.0
    # No further tick happens: the projection keeps its last honest frame,
    # and the store's own TTL semantics are the hand-back.
    state = runner.state_payload()
    assert state["active"] is True
    assert state["last_action"] == "submit"


async def test_runner_survives_a_failed_submission_and_resubmits_next_tick() -> None:
    plan = _plan(_monday_window(), version=1)
    submit = RunnerSubmit(fail=True)
    runner, _store, _clock, submit, _intents, _bus = _make_runner(plan=plan, submit=submit)

    with pytest.raises(OSError):
        await runner.tick()

    submit.fail = False
    await runner.tick()
    assert runner.held_intent_id == "schedule-1"


async def test_runner_boot_frame_is_honest_before_the_first_tick() -> None:
    scheduling = _helpers()
    plan = _plan(_monday_window(), version=7)
    store = RunnerStore(plan=plan)
    clock = RunnerClock(wall=datetime(2026, 1, 5, 8, 0, tzinfo=ZoneInfo("Australia/Brisbane")))
    runner = scheduling.ScheduleRunner(
        store=store,
        evaluator=scheduling.ScheduleEvaluator(intent_ttl_s=10.0),
        clock=clock,
        submit=RunnerSubmit(),
        intents=RunnerIntents(),
        initial_plan=plan,
    )

    state = runner.state_payload()

    assert state["active"] is False
    assert state["version"] == 7
    assert state["reason_codes"] == ["no_window_open"]
    assert state["next"]["entry_id"] == "day-charge"
    assert state["next"]["starts_at"].startswith("2026-01-05T09:00")
    assert state["posture"] == "yield"


# --- DESIGN_SCHEDULES §3 B2/B5: the posture policy as pure logic ---------------


def test_day_default_is_the_shipped_yield_posture() -> None:
    scheduling = _helpers()
    policy = scheduling.SchedulePolicy(
        allowed_windows_local=(scheduling.DAY_DEFAULT,),
        intent_ttl_s=10.0,
        timezone="Australia/Brisbane",
    )
    assert policy.posture == "yield"
    assert policy.wire_windows() == [["06:00", "20:00"]]


def test_any_night_second_in_the_allowed_set_is_the_partition_posture() -> None:
    scheduling = _helpers()
    policy = scheduling.SchedulePolicy(
        allowed_windows_local=(
            (scheduling.parse_hhmm("05:00"), scheduling.parse_hhmm("08:00")),
            scheduling.DAY_DEFAULT,
        ),
        intent_ttl_s=10.0,
        timezone="Australia/Brisbane",
    )
    assert policy.posture == "partition"


def test_containment_judges_crossing_windows_as_both_halves() -> None:
    scheduling = _helpers()
    day = (scheduling.DAY_DEFAULT,)
    policy = scheduling.SchedulePolicy(
        allowed_windows_local=day, intent_ttl_s=10.0, timezone="Australia/Brisbane"
    )
    # A window crossing OUT of the allowed union is refused even though both
    # of its endpoints sit inside it.
    inside = scheduling.parse_hhmm
    assert policy.contains_entry(_entry(start=inside("07:00"), end=inside("19:00")))
    assert not policy.contains_entry(_entry(start=inside("19:00"), end=inside("07:00")))
    # A night-granting policy contains the night entry and is partition.
    night_policy = scheduling.SchedulePolicy(
        allowed_windows_local=(
            (inside("00:00"), inside("06:00")),
            scheduling.DAY_DEFAULT,
        ),
        intent_ttl_s=10.0,
        timezone="Australia/Brisbane",
    )
    assert night_policy.contains_entry(_entry(start=inside("00:01"), end=inside("05:59")))
    assert night_policy.entry_is_night(_entry(start=inside("00:01"), end=inside("05:59")))
    assert not policy.entry_is_night(_entry(start=inside("07:00"), end=inside("19:00")))
