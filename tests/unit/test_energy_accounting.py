"""Energy scorecard accounting contracts (DESIGN_ENERGY_SCORECARD sections 4-5).

The module under test is ``energypod.application.energy`` over the frozen day
records of ``energypod.domain.energy``.  The accountant owns the four honest
disciplines the accepted design pins:

- zero-order-hold integration of the per-pod CT grid word over OBSERVATION
  capture times, sign-split import/export, with inter-sample gaps above
  ``integration_max_gap_s`` excluded -- never interpolated -- and a per-unit
  per-day coverage fraction (worst-unit fleet rollup);
- device-counter daily deltas (charge/discharge/load, grid A/B cross-check)
  where a DECREASING cumulative is a reset, not a negative delta: the
  unit-metric-day re-baselines and carries the ``counter_reset:<metric>``
  flag plus one ``energy_counter_reset_observed`` audit fact;
- day rollover at the site-timezone midnight on the first observation whose
  local date advances (DST transition days are honest 23/25-hour days with
  the day's ``utc_offset_minutes`` stored);
- surplus attribution over ticks where the excess adviser is ACTIVE and
  targets the unit, integrating MEASURED battery watts (what physically
  happened), never authorized watts.

Nothing here opens a socket, contacts hardware, or reads ambient time: every
observation is a constructed domain value with scripted capture metadata.
"""

from __future__ import annotations

import importlib
import itertools
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from energypod.domain.observations import DataQuality, Observation, UnitLifecycle


def _energy_domain() -> ModuleType:
    return importlib.import_module("energypod.domain.energy")


def _energy_application() -> ModuleType:
    return importlib.import_module("energypod.application.energy")


BRISBANE = ZoneInfo("Australia/Brisbane")  # UTC+10, no DST
SYDNEY = ZoneInfo("Australia/Sydney")  # DST: +11 (AEDT) / +10 (AEST)

_WATT_SECONDS_PER_KWH = 3_600_000.0
_UNITS = ("mid", "rhs")
# 2026-08-26 is the design's worked example day.
_DAY = date(2026, 8, 26)
_FIRST_MONO = 1000.0


def _local(
    day: date, hour: int, minute: int = 0, second: int = 0, *, zone: ZoneInfo = BRISBANE
) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone).astimezone(UTC)


class _ScriptedClock:
    """Wall + monotonic clock the tests advance explicitly."""

    def __init__(self, wall: datetime, mono: float = 1_000_000.0) -> None:
        self._wall = wall
        self._mono = mono

    def wall_now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono


_QUALITY: dict[str, DataQuality] = {
    name: DataQuality.GOOD for name in Observation.QUALITY_FIELDS
} | {name: DataQuality.GOOD for name in Observation.ADVISORY_QUALITY_FIELDS}


def _observation(
    unit_id: str,
    *,
    wall: datetime,
    mono: float,
    sequence: int,
    grid_power_w: float | None,
    battery_watts: float | None = 0.0,
    energy_charge_kwh: float | None = None,
    energy_discharge_kwh: float | None = None,
    energy_load_kwh: float | None = None,
    energy_grid_a_kwh: float | None = None,
    energy_grid_b_kwh: float | None = None,
    grid_quality: DataQuality = DataQuality.GOOD,
) -> Observation:
    return Observation(
        unit_id=unit_id,
        wall_timestamp=wall,
        captured_at_mono=mono,
        sequence=sequence,
        lifecycle=UnitLifecycle.ARMED_IDLE,
        protocol_profile="iot",
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
        soh_pct=100.0,
        battery_watts=battery_watts,
        pack_voltage_v=200.0,
        pack_current_a=0.0,
        dynamic_charge_limit_w=3000.0,
        dynamic_discharge_limit_w=3000.0,
        expected_cell_count=2,
        cell_voltages_v=(3.3, 3.3),
        expected_temperature_count=1,
        temperatures_c=(24.0,),
        grid_power_w=grid_power_w,
        load_power_w=None,
        energy_grid_a_kwh=energy_grid_a_kwh,
        energy_grid_b_kwh=energy_grid_b_kwh,
        energy_load_kwh=energy_load_kwh,
        energy_pv_kwh=None,
        energy_charge_kwh=energy_charge_kwh,
        energy_discharge_kwh=energy_discharge_kwh,
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality=_QUALITY | {"grid_power_w": grid_quality},
    )


class _RecordingLedger:
    """Ledger port double: the durable day records and the live-day baseline."""

    def __init__(self) -> None:
        self.recorded: list[Any] = []
        self.days: dict[date, Any] = {}
        self.saved_baselines: list[Mapping[str, Any]] = []
        self._baseline: dict[str, Any] = {}

    def record_day(self, record: Any) -> None:
        self.recorded.append(record)
        self.days[record.date] = record

    def get_day(self, day: date) -> Any | None:
        return self.days.get(day)

    def latest_days(self, limit: int) -> tuple[Any, ...]:
        return tuple(self.days[day] for day in sorted(self.days, reverse=True)[:limit])

    def load_baseline(self) -> Mapping[str, Any]:
        return dict(self._baseline)

    def save_baseline(self, baselines: Mapping[str, Any]) -> None:
        self.saved_baselines.append(dict(baselines))
        self._baseline = dict(baselines)


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, body: Mapping[str, Any]) -> int:
        self.events.append(dict(body))
        return len(self.events)


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


class _RefreshRequests:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _settings(**overrides: Any) -> Any:
    return _energy_application().EnergyAccountingSettings(**overrides)


def _accountant(
    *,
    wall_start: datetime,
    timezone_name: str = "Australia/Brisbane",
    zone: ZoneInfo = BRISBANE,
    settings: Any | None = None,
    ledger: Any | None = None,
    bus: Any | None = None,
    audit: Any | None = None,
    refresh: Any | None = None,
    unit_ids: tuple[str, ...] = _UNITS,
) -> Any:
    return _energy_application().EnergyAccountant(
        unit_ids=unit_ids,
        timezone=timezone_name,
        settings=settings if settings is not None else _settings(),
        clock=_ScriptedClock(wall_start),
        ledger=ledger,
        bus=bus,
        audit=audit,
        request_energy_refresh=refresh,
        process_instance_id="process-energy-test",
        process_origin_mono=0.0,
        configuration_version=1,
    )


async def _observe(
    accountant: Any,
    observations: Any,
    *,
    adviser_active_targets: frozenset[str] = frozenset(),
) -> None:
    for observation in observations:
        accountant.observe(observation, adviser_active_targets=adviser_active_targets, now_mono=0.0)
    await accountant.flush()


# --- the zero-order-hold integration (DESIGN section 4) -------------------------


async def test_zero_order_hold_integral_is_exact_against_hand_computed_traces() -> None:
    """A constant -2000 W import sampled at t0/t1/t2 integrates P_prev over
    each interval: 2000 W x 3600 s = 2.0 kWh exactly."""
    accountant = _accountant(
        wall_start=_local(_DAY, 12), settings=_settings(integration_max_gap_s=3600.0)
    )
    await _observe(
        accountant,
        [
            _observation(
                "mid", wall=_local(_DAY, 12), mono=_FIRST_MONO, sequence=1, grid_power_w=-2000.0
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 12) + timedelta(seconds=1800),
                mono=_FIRST_MONO + 1800.0,
                sequence=2,
                grid_power_w=-2000.0,
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 12) + timedelta(seconds=3600),
                mono=_FIRST_MONO + 3600.0,
                sequence=3,
                grid_power_w=-2000.0,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    assert unit.grid_import_kwh == pytest.approx(2.0)
    assert unit.grid_export_kwh == pytest.approx(0.0)


async def test_the_sign_split_routes_negative_to_import_and_positive_to_export() -> None:
    """-1000 W for 36 s then +1000 W for 36 s is exactly 0.01 kWh each way."""
    accountant = _accountant(
        wall_start=_local(_DAY, 12), settings=_settings(integration_max_gap_s=60.0)
    )
    await _observe(
        accountant,
        [
            _observation(
                "mid", wall=_local(_DAY, 12), mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 12) + timedelta(seconds=36),
                mono=_FIRST_MONO + 36.0,
                sequence=2,
                grid_power_w=1000.0,
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 12) + timedelta(seconds=72),
                mono=_FIRST_MONO + 72.0,
                sequence=3,
                grid_power_w=1000.0,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    assert unit.grid_import_kwh == pytest.approx(36000 / _WATT_SECONDS_PER_KWH)
    assert unit.grid_export_kwh == pytest.approx(36000 / _WATT_SECONDS_PER_KWH)


async def test_gaps_above_the_max_gap_are_excluded_never_interpolated() -> None:
    """A scripted 40-minute hole at a held -2000 W contributes ZERO energy and
    ZERO coverage -- interpolating the stale figure would invent 1.33 kWh."""
    accountant = _accountant(
        wall_start=_local(_DAY, 12),
        settings=_settings(integration_max_gap_s=10.0),
    )
    start = _local(_DAY, 12)
    await _observe(
        accountant,
        [
            _observation("mid", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-2000.0),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-2000.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=10),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=-2000.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=15),
                mono=_FIRST_MONO + 15.0,
                sequence=4,
                grid_power_w=-2000.0,
            ),
            # The 40-minute hole: the wire held the figure, the accountant
            # must not integrate across the missing interval.
            _observation(
                "mid",
                wall=start + timedelta(seconds=2415),
                mono=_FIRST_MONO + 2415.0,
                sequence=5,
                grid_power_w=-2000.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=2420),
                mono=_FIRST_MONO + 2420.0,
                sequence=6,
                grid_power_w=-2000.0,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    # Sampled seconds: 5 + 5 + 5 (pre-hole) + 5 (post-hole) = 20 s exactly.
    assert unit.grid_import_kwh == pytest.approx(2000.0 * 20.0 / _WATT_SECONDS_PER_KWH)
    # Elapsed since local midnight: 12 h + 2420 s; coverage = 20 / elapsed.
    elapsed = 12 * 3600 + 2420
    assert unit.coverage_pct == pytest.approx(20.0 / elapsed * 100.0, abs=0.01)


async def test_coverage_is_sampled_over_elapsed_and_the_fleet_takes_the_worst_unit() -> None:
    """Per unit: sampled_seconds / elapsed_seconds since LOCAL midnight.
    Fleet: the WORST unit's coverage (the evidence-rollup precedence)."""
    accountant = _accountant(
        wall_start=_local(_DAY, 0),
        settings=_settings(integration_max_gap_s=10.0),
    )
    start = _local(_DAY, 0)
    await _observe(
        accountant,
        [
            # mid: fully sampled, 5 s apart, a 15 s window from midnight.
            _observation("mid", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=10),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=15),
                mono=_FIRST_MONO + 15.0,
                sequence=4,
                grid_power_w=-1000.0,
            ),
            # rhs: two 5 s samples, then a hole to t=40.
            _observation("rhs", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0),
            _observation(
                "rhs",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            _observation(
                "rhs",
                wall=start + timedelta(seconds=40),
                mono=_FIRST_MONO + 40.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
        ],
    )

    record = accountant.day_summary(_DAY)
    assert record.units["mid"].coverage_pct == pytest.approx(100.0, abs=0.01)
    # rhs: 5 s sampled of a 40 s window.
    assert record.units["rhs"].coverage_pct == pytest.approx(12.5, abs=0.01)
    assert record.fleet.coverage_pct == pytest.approx(12.5, abs=0.01), (
        "fleet coverage is the worst unit's, never an average"
    )


async def test_a_day_below_the_commissioned_coverage_renders_partial() -> None:
    """kind is in_progress for the live day, complete only for a rolled day at
    or above min_day_coverage_pct, partial below it."""
    ledger = _RecordingLedger()
    accountant = _accountant(
        wall_start=_local(_DAY, 12),
        settings=_settings(min_day_coverage_pct=95.0),
        ledger=ledger,
    )
    start = _local(_DAY, 12)
    next_day = _DAY + timedelta(days=1)
    await _observe(
        accountant,
        [
            _observation("mid", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0),
            _observation(
                "mid",
                wall=start + timedelta(seconds=10),
                mono=_FIRST_MONO + 10.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            # The rollover trigger: the first observation of the next day.
            _observation(
                "mid",
                wall=_local(next_day, 0, 0, 10),
                mono=_FIRST_MONO + 20.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
        ],
    )

    assert accountant.day_summary(_DAY).kind == "partial"
    assert accountant.day_summary(next_day).kind == "in_progress"


async def test_a_full_coverage_rolled_day_renders_complete() -> None:
    ledger = _RecordingLedger()
    accountant = _accountant(
        wall_start=_local(_DAY, 0, 0, 5),
        settings=_settings(min_day_coverage_pct=95.0, integration_max_gap_s=60.0),
        ledger=ledger,
    )
    next_day = _DAY + timedelta(days=1)
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=_local(_DAY, 0, 0, 5),
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 0, 1, 5),
                mono=_FIRST_MONO + 60.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=_local(_DAY, 0, 2, 5),
                mono=_FIRST_MONO + 120.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=_local(next_day, 0, 0, 10),
                mono=_FIRST_MONO + 180.0,
                sequence=4,
                grid_power_w=-1000.0,
            ),
        ],
    )

    assert accountant.day_summary(_DAY).kind == "complete"


async def test_day_rolls_at_site_midnight_publishes_persists_and_promotes_one_read() -> None:
    """Rollover on the first observation whose LOCAL date advances: the
    completed record is persisted, published on the bus exactly once, audited,
    and ONE promoted energy read is requested (the B4 precedent)."""
    ledger = _RecordingLedger()
    bus = _RecordingBus()
    audit = _RecordingAudit()
    refresh = _RefreshRequests()
    accountant = _accountant(
        wall_start=_local(_DAY, 12), ledger=ledger, bus=bus, audit=audit, refresh=refresh
    )
    start = _local(_DAY, 12)
    next_day = _DAY + timedelta(days=1)
    await _observe(
        accountant,
        [
            _observation("mid", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=_local(next_day, 0, 0, 5),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
        ],
    )

    rolled = [event for event in bus.events if event["type"] == "energy.day_rolled"]
    assert len(rolled) == 1, "the rollover is a TRANSITION, never a heartbeat"
    assert rolled[0]["payload"]["date"] == _DAY.isoformat()
    assert ledger.recorded and ledger.recorded[0].date == _DAY
    recorded = [event for event in audit.events if event.event_type == "energy_day_recorded"]
    assert recorded, "the completed day leaves a durable audit fact"
    assert refresh.count == 1, "exactly one promoted energy read per rollover"
    assert accountant.day_summary(_DAY).date == _DAY


async def test_dst_boundary_days_are_honest_and_store_their_offset() -> None:
    """Sydney 2026-04-05: DST ends at 03:00 local -- a 25-hour day whose
    midnight offset is +11 (660 minutes); the next day's midnight is +10.
    The roll happens at LOCAL midnight however many UTC hours the day held."""
    assert datetime(2026, 4, 5, 0, 0, tzinfo=SYDNEY).utcoffset() == timedelta(hours=11)
    assert datetime(2026, 4, 6, 0, 0, tzinfo=SYDNEY).utcoffset() == timedelta(hours=10)

    first = datetime(2026, 4, 5, 0, 1, tzinfo=SYDNEY)
    second = datetime(2026, 4, 6, 0, 0, 30, tzinfo=SYDNEY)
    ledger = _RecordingLedger()
    accountant = _accountant(
        wall_start=first.astimezone(UTC),
        timezone_name="Australia/Sydney",
        ledger=ledger,
    )
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=first.astimezone(UTC),
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=first.astimezone(UTC) + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
            ),
            _observation(
                "mid",
                wall=second.astimezone(UTC),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
        ],
    )

    completed = accountant.day_summary(date(2026, 4, 5))
    assert completed is not None
    assert completed.utc_offset_minutes == 660
    assert accountant.day_summary(date(2026, 4, 6)).utc_offset_minutes == 600


# --- the device-counter daily math (DESIGN section 2) ---------------------------


async def test_counter_daily_deltas_are_last_minus_day_baseline() -> None:
    accountant = _accountant(wall_start=_local(_DAY, 12))
    start = _local(_DAY, 12)
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=start,
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=None,
                energy_charge_kwh=100.0,
                energy_discharge_kwh=50.0,
                energy_load_kwh=200.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=None,
                energy_charge_kwh=100.5,
                energy_discharge_kwh=50.2,
                energy_load_kwh=201.0,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    assert unit.battery_charged_kwh == pytest.approx(0.5)
    assert unit.battery_discharged_kwh == pytest.approx(0.2)
    assert unit.load_kwh == pytest.approx(1.0)
    assert unit.metric_flags == frozenset()


async def test_the_durable_baseline_carries_the_day_across_a_restart() -> None:
    """A mid-day restart re-baselines from the last seen cumulatives: counter
    deltas survive BY DESIGN (the device kept counting), and the integration
    clock resumes with a gap counted from the last capture time."""
    ledger = _RecordingLedger()
    morning = _accountant(wall_start=_local(_DAY, 9), ledger=ledger)
    start = _local(_DAY, 9)
    await _observe(
        morning,
        [
            _observation(
                "mid",
                wall=start,
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-1000.0,
                energy_charge_kwh=100.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
                energy_charge_kwh=100.5,
            ),
        ],
    )

    # The restart: a fresh accountant over the SAME ledger, new mono clock.
    afternoon = _accountant(wall_start=start + timedelta(seconds=600), ledger=ledger)
    await _observe(
        afternoon,
        [
            _observation(
                "mid",
                wall=start + timedelta(seconds=600),
                mono=5000.0,
                sequence=1,
                grid_power_w=-1000.0,
                energy_charge_kwh=101.0,
            ),
        ],
    )

    unit = afternoon.day_summary(_DAY).units["mid"]
    assert unit.battery_charged_kwh == pytest.approx(1.0), (
        "100.0 -> 101.0 across the restart: the delta survived"
    )
    # The 595 s restart gap at -1000 W is a GAP: only the sampled 5 s
    # integrated.
    assert unit.grid_import_kwh == pytest.approx(1000.0 * 5.0 / _WATT_SECONDS_PER_KWH)


async def test_a_decreasing_cumulative_is_a_reset_never_a_negative_delta() -> None:
    clock_ledger = _RecordingLedger()
    audit = _RecordingAudit()
    accountant = _accountant(wall_start=_local(_DAY, 12), ledger=clock_ledger, audit=audit)
    start = _local(_DAY, 12)
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=start,
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=None,
                energy_charge_kwh=100.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=None,
                energy_charge_kwh=100.5,
            ),
            # An external service cleared the counter mid-day (the A-1
            # hypothesis (a) class): the cumulative DECREASES.
            _observation(
                "mid",
                wall=start + timedelta(seconds=10),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=None,
                energy_charge_kwh=0.2,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=15),
                mono=_FIRST_MONO + 15.0,
                sequence=4,
                grid_power_w=None,
                energy_charge_kwh=0.7,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    assert unit.battery_charged_kwh == pytest.approx(0.5), (
        "recorded from the NEW baseline (0.7 - 0.2), never negative"
    )
    assert "counter_reset:charge" in unit.metric_flags
    resets = [
        event for event in audit.events if event.event_type == "energy_counter_reset_observed"
    ]
    assert resets and resets[0].unit_id == "mid"


async def test_a_reset_on_a_grid_pair_disables_that_days_discrimination() -> None:
    accountant = _accountant(
        wall_start=_local(_DAY, 0, 0, 5),
        settings=_settings(integration_max_gap_s=60.0),
    )
    start = _local(_DAY, 0, 0, 5)
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=start,
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-2000.0,
                energy_grid_a_kwh=100.0,
                energy_grid_b_kwh=50.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=60),
                mono=_FIRST_MONO + 60.0,
                sequence=2,
                grid_power_w=-2000.0,
                energy_grid_a_kwh=100.2,
                energy_grid_b_kwh=50.3,
            ),
            # Pair A cleared mid-day.
            _observation(
                "mid",
                wall=start + timedelta(seconds=120),
                mono=_FIRST_MONO + 120.0,
                sequence=3,
                grid_power_w=-2000.0,
                energy_grid_a_kwh=0.1,
                energy_grid_b_kwh=50.6,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    assert "counter_reset:grid_a" in unit.metric_flags
    check = accountant.day_summary(_DAY).counter_cross_check
    assert check is not None
    assert check.discriminating is False


# --- surplus attribution (DESIGN section 2, the graduation evidence) ------------


async def test_attribution_integrates_measured_watts_over_adviser_active_ticks() -> None:
    """charged_from_surplus accumulates max(0, -measured_battery_w) over ticks
    where the adviser is ACTIVE and targets THIS unit -- measured, not
    authorized watts; inactive or off-target ticks and discharging ticks
    contribute nothing."""
    accountant = _accountant(
        wall_start=_local(_DAY, 12), settings=_settings(integration_max_gap_s=60.0)
    )
    start = _local(_DAY, 12)
    seq = itertools.count(1)

    def at(seconds: float, battery_w: float, targets: frozenset[str]) -> None:
        accountant.observe(
            _observation(
                "mid",
                wall=start + timedelta(seconds=seconds),
                mono=_FIRST_MONO + seconds,
                sequence=next(seq),
                grid_power_w=-1000.0,
                battery_watts=battery_w,
            ),
            adviser_active_targets=targets,
            now_mono=0.0,
        )

    at(0.0, -1500.0, frozenset())  # adviser inactive
    at(36.0, -1500.0, frozenset({"mid"}))  # the active window opens
    at(72.0, -1500.0, frozenset({"rhs"}))  # active but on the OTHER battery
    at(108.0, 1000.0, frozenset({"mid"}))  # active but DISCHARGING
    await accountant.flush()

    unit = accountant.day_summary(_DAY).units["mid"]
    # Only the [36, 72) interval attributed: 1500 W x 36 s.
    assert unit.charged_from_surplus_kwh == pytest.approx(1500.0 * 36.0 / _WATT_SECONDS_PER_KWH)


# --- honest projections (DESIGN section 5: never zero-filled) --------------------


async def test_absent_sources_project_null_never_zero() -> None:
    """A unit whose counters were absent all day has NO counter figure; a unit
    that never published carries null everywhere; fleet sums skip nulls and
    stay null when every source was absent."""
    accountant = _accountant(wall_start=_local(_DAY, 12))
    await _observe(
        accountant,
        [
            # mid publishes the CT word only; rhs never publishes at all.
            _observation(
                "mid",
                wall=_local(_DAY, 12),
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-1000.0,
                battery_watts=None,
            ),
        ],
    )

    record = accountant.day_summary(_DAY)
    mid, rhs = record.units["mid"], record.units["rhs"]
    assert mid.battery_charged_kwh is None
    assert mid.load_kwh is None
    assert mid.charged_from_surplus_kwh is None
    assert rhs.grid_import_kwh is None
    assert rhs.coverage_pct is None
    assert record.fleet.battery_charged_kwh is None
    assert record.fleet.grid_export_kwh == pytest.approx(0.0)


async def test_a_fleet_sum_stays_null_when_every_unit_was_absent() -> None:
    accountant = _accountant(wall_start=_local(_DAY, 12))
    record = accountant.today_summary()
    assert record.fleet.grid_import_kwh is None
    assert record.fleet.load_kwh is None
    assert record.fleet.coverage_pct is None


async def test_an_unmeasured_grid_word_is_a_gap_not_a_sample() -> None:
    """A grid word whose quality is not GOOD is no sample: the interval
    neither integrates nor counts toward coverage."""
    accountant = _accountant(wall_start=_local(_DAY, 0))
    start = _local(_DAY, 0)
    await _observe(
        accountant,
        [
            _observation("mid", wall=start, mono=_FIRST_MONO, sequence=1, grid_power_w=-1000.0),
            _observation(
                "mid",
                wall=start + timedelta(seconds=5),
                mono=_FIRST_MONO + 5.0,
                sequence=2,
                grid_power_w=-1000.0,
                grid_quality=DataQuality.BAD,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=10),
                mono=_FIRST_MONO + 10.0,
                sequence=3,
                grid_power_w=-1000.0,
            ),
        ],
    )

    unit = accountant.day_summary(_DAY).units["mid"]
    # Only [0, 5) integrated; [5, 10) was anchored on a BAD word.
    assert unit.grid_import_kwh == pytest.approx(1000.0 * 5.0 / _WATT_SECONDS_PER_KWH)
    assert unit.coverage_pct == pytest.approx(50.0, abs=0.01)


# --- the A-1 passive cross-check verdict (DESIGN section 3) ---------------------


def _cross_check_day(
    *,
    import_kwh: float,
    export_kwh: float,
    grid_a_delta_kwh: float,
    grid_b_delta_kwh: float,
) -> tuple[Any, list[Any]]:
    """One unit's day: a 1800 s import window then a 1800 s export window,
    with the grid counter pair scripted to the requested deltas."""
    accountant = _accountant(
        wall_start=_local(_DAY, 0, 0, 5),
        settings=_settings(integration_max_gap_s=3600.0),
    )
    start = _local(_DAY, 0, 0, 5)
    window_s = 1800.0
    import_w = -import_kwh * _WATT_SECONDS_PER_KWH / window_s if import_kwh else 0.0
    export_w = export_kwh * _WATT_SECONDS_PER_KWH / window_s if export_kwh else 0.0
    observations = [
        _observation(
            "mid",
            wall=start,
            mono=_FIRST_MONO,
            sequence=1,
            grid_power_w=import_w,
            energy_grid_a_kwh=1000.0,
            energy_grid_b_kwh=500.0,
        ),
        _observation(
            "mid",
            wall=start + timedelta(seconds=window_s),
            mono=_FIRST_MONO + window_s,
            sequence=2,
            grid_power_w=export_w,
            energy_grid_a_kwh=1000.0 + grid_a_delta_kwh,
            energy_grid_b_kwh=500.0 + grid_b_delta_kwh,
        ),
        _observation(
            "mid",
            wall=start + timedelta(seconds=2 * window_s),
            mono=_FIRST_MONO + 2 * window_s,
            sequence=3,
            grid_power_w=export_w,
            energy_grid_a_kwh=1000.0 + grid_a_delta_kwh,
            energy_grid_b_kwh=500.0 + grid_b_delta_kwh,
        ),
    ]
    return accountant, observations


async def test_a1_verdict_marks_a_vendor_labels_day() -> None:
    accountant, observations = _cross_check_day(
        import_kwh=2.0, export_kwh=3.0, grid_a_delta_kwh=2.0, grid_b_delta_kwh=3.0
    )
    await _observe(accountant, observations)

    check = accountant.day_summary(_DAY).counter_cross_check
    assert check is not None
    assert check.grid_a_delta_kwh == pytest.approx(2.0)
    assert check.grid_b_delta_kwh == pytest.approx(3.0)
    assert check.consistent_with == "vendor_labels"
    assert check.discriminating is True


async def test_a1_verdict_marks_a_swapped_day() -> None:
    accountant, observations = _cross_check_day(
        import_kwh=2.0, export_kwh=3.0, grid_a_delta_kwh=3.0, grid_b_delta_kwh=2.0
    )
    await _observe(accountant, observations)

    check = accountant.day_summary(_DAY).counter_cross_check
    assert check.consistent_with == "swapped"
    assert check.discriminating is True


async def test_a1_verdict_is_undiscriminating_when_both_orderings_match() -> None:
    accountant, observations = _cross_check_day(
        import_kwh=2.0, export_kwh=2.0, grid_a_delta_kwh=2.0, grid_b_delta_kwh=2.0
    )
    await _observe(accountant, observations)

    check = accountant.day_summary(_DAY).counter_cross_check
    assert check.consistent_with == "undiscriminating"
    assert check.discriminating is False


async def test_a1_verdict_requires_both_sides_above_half_a_kwh() -> None:
    """A day with no export cannot discriminate (DESIGN section 3)."""
    accountant, observations = _cross_check_day(
        import_kwh=2.0, export_kwh=0.0, grid_a_delta_kwh=2.0, grid_b_delta_kwh=0.0
    )
    await _observe(accountant, observations)

    check = accountant.day_summary(_DAY).counter_cross_check
    assert check.discriminating is False
    assert check.consistent_with is None


async def test_a1_verdict_tolerates_max_of_half_a_kwh_or_five_percent() -> None:
    """|delta - integral| <= max(0.5 kWh, 5 %): a 2 kWh figure tolerates 0.5."""
    close = _cross_check_day(
        import_kwh=2.0, export_kwh=3.0, grid_a_delta_kwh=2.4, grid_b_delta_kwh=3.0
    )
    await _observe(close[0], close[1])
    assert close[0].day_summary(_DAY).counter_cross_check.consistent_with == "vendor_labels"

    far = _cross_check_day(
        import_kwh=2.0, export_kwh=3.0, grid_a_delta_kwh=2.6, grid_b_delta_kwh=3.0
    )
    await _observe(far[0], far[1])
    far_check = far[0].day_summary(_DAY).counter_cross_check
    assert far_check.consistent_with is None, "matches neither ordering"
    assert far_check.discriminating is True, (
        "the day HAD the evidence; the pairs simply disagree beyond tolerance"
    )


# --- settings + record shapes ---------------------------------------------------


def test_settings_validate_the_commissioned_bounds() -> None:
    application = _energy_application()
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(grid_source="nonsense")
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(grid_counter_roles="guessed")
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(integration_max_gap_s=0.0)
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(min_day_coverage_pct=0.0)
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(min_day_coverage_pct=100.5)
    # The structural A-1 gate: the unpinned pair must never be the source.
    with pytest.raises((TypeError, ValueError)):
        application.EnergyAccountingSettings(
            grid_source=application.GRID_SOURCE_DEVICE_COUNTER,
            grid_counter_roles=application.GRID_ROLES_UNPINNED,
        )
    pinned = application.EnergyAccountingSettings(
        grid_source=application.GRID_SOURCE_DEVICE_COUNTER,
        grid_counter_roles="vendor_labels",
    )
    assert pinned.grid_source == "device_counter"


async def test_device_counter_source_populates_grid_figures_from_the_pinned_roles() -> None:
    """After the operator promotes to device_counter with pinned roles, the
    per-unit grid figures come from the counter deltas under the pinned
    mapping while the integration stays recorded as the cross-check."""
    application = _energy_application()
    accountant = _accountant(
        wall_start=_local(_DAY, 12),
        settings=application.EnergyAccountingSettings(
            grid_source=application.GRID_SOURCE_DEVICE_COUNTER,
            grid_counter_roles="swapped",
            integration_max_gap_s=60.0,
        ),
    )
    start = _local(_DAY, 12)
    await _observe(
        accountant,
        [
            _observation(
                "mid",
                wall=start,
                mono=_FIRST_MONO,
                sequence=1,
                grid_power_w=-1000.0,
                energy_grid_a_kwh=100.0,
                energy_grid_b_kwh=50.0,
            ),
            _observation(
                "mid",
                wall=start + timedelta(seconds=36),
                mono=_FIRST_MONO + 36.0,
                sequence=2,
                grid_power_w=-1000.0,
                energy_grid_a_kwh=103.0,
                energy_grid_b_kwh=50.5,
            ),
        ],
    )

    record = accountant.day_summary(_DAY)
    unit = record.units["mid"]
    # swapped: pair B accumulates imports (0.5), pair A exports (3.0).
    assert unit.grid_import_kwh == pytest.approx(0.5)
    assert unit.grid_export_kwh == pytest.approx(3.0)
    assert record.sources["grid"] == "device_counter"
    # The integration itself still ran and rides the cross-check.
    assert record.counter_cross_check is not None


def _wire_record() -> Any:
    domain = _energy_domain()
    return domain.EnergyDayRecord(
        date=_DAY,
        timezone="Australia/Brisbane",
        utc_offset_minutes=600,
        kind="in_progress",
        units={
            "mid": domain.UnitEnergyDay(
                grid_import_kwh=1.2,
                grid_export_kwh=6.8,
                battery_charged_kwh=3.4,
                battery_discharged_kwh=0.7,
                load_kwh=5.1,
                charged_from_surplus_kwh=3.1,
                coverage_pct=99.4,
                metric_flags=frozenset(),
            )
        },
        fleet=domain.FleetEnergyDay(
            grid_import_kwh=1.2,
            grid_export_kwh=6.8,
            battery_charged_kwh=3.4,
            battery_discharged_kwh=0.7,
            load_kwh=5.1,
            charged_from_surplus_kwh=3.1,
            coverage_pct=99.4,
        ),
        sources={
            "grid": "integrated_ct",
            "battery": "device_counter",
            "load": "device_counter",
            "surplus": "attributed_adviser",
        },
        counter_cross_check=domain.CounterCrossCheck(
            grid_a_delta_kwh=1.1,
            grid_b_delta_kwh=6.9,
            consistent_with="vendor_labels",
            discriminating=True,
        ),
        solar_production_measured=False,
    )


def test_record_payload_is_the_pinned_wire_shape() -> None:
    payload = _wire_record().payload()
    assert payload["date"] == "2026-08-26"
    assert payload["timezone"] == "Australia/Brisbane"
    assert payload["utc_offset_minutes"] == 600
    assert payload["kind"] == "in_progress"
    assert payload["units"]["mid"]["grid_import_kwh"] == pytest.approx(1.2)
    assert payload["units"]["mid"]["coverage_pct"] == pytest.approx(99.4)
    assert payload["units"]["mid"]["metric_flags"] == []
    assert payload["fleet"]["charged_from_surplus_kwh"] == pytest.approx(3.1)
    assert payload["sources"] == {
        "grid": "integrated_ct",
        "battery": "device_counter",
        "load": "device_counter",
        "surplus": "attributed_adviser",
    }
    assert payload["counter_cross_check"]["consistent_with"] == "vendor_labels"
    assert payload["counter_cross_check"]["discriminating"] is True
    assert payload["solar_production_measured"] is False
    assert set(payload) == {
        "date",
        "timezone",
        "utc_offset_minutes",
        "kind",
        "units",
        "fleet",
        "sources",
        "counter_cross_check",
        "solar_production_measured",
    }


def test_record_rejects_malformed_shapes() -> None:
    domain = _energy_domain()
    record = _wire_record()
    base = {
        "date": record.date,
        "timezone": record.timezone,
        "utc_offset_minutes": record.utc_offset_minutes,
        "kind": record.kind,
        "units": record.units,
        "fleet": record.fleet,
        "sources": record.sources,
        "counter_cross_check": record.counter_cross_check,
        "solar_production_measured": record.solar_production_measured,
    }
    with pytest.raises((TypeError, ValueError)):
        domain.EnergyDayRecord(**{**base, "kind": "finished"})
    with pytest.raises((TypeError, ValueError)):
        domain.EnergyDayRecord(**{**base, "timezone": "Not/AZone"})
    with pytest.raises((TypeError, ValueError)):
        domain.EnergyDayRecord(**{**base, "sources": {"grid": "integrated_ct"}})
    with pytest.raises((TypeError, ValueError)):
        (
            domain.EnergyDayRecord(**{**base, "solar_production_measured": True}),
            ("site PV is never presented as measured"),
        )


def test_energy_figures_are_finite_and_non_negative() -> None:
    domain = _energy_domain()
    blank = dict(
        grid_export_kwh=None,
        battery_charged_kwh=None,
        battery_discharged_kwh=None,
        load_kwh=None,
        charged_from_surplus_kwh=None,
        coverage_pct=None,
    )
    for bad in (
        {"grid_import_kwh": math.nan},
        {"grid_import_kwh": -0.5},
        {"coverage_pct": 101.0},
        {"coverage_pct": -1.0},
    ):
        with pytest.raises((TypeError, ValueError)):
            domain.UnitEnergyDay(**{**blank, **bad})
