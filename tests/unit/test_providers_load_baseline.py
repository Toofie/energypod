"""The historian-statistics load baseline contract (LoadForecastProvider).

No external load-forecast API is wired, so the load family's first provider
is deliberately naive: the SAME SLOT LAST WEEK, read from the telemetry
historian the controller already keeps.  These tests pin what that means --
the slot grid, the fleet-sum doctrine (a timestamp counts only when EVERY
configured unit reported a load word), mean-of-window aggregation, and the
honesty rules (absent history is an absent slot, never a zero; every call
rereads, so the baseline tracks the historian it stands on).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

try:
    from energypod.adapters.providers.load_baseline import HistorianLoadForecast
except ImportError as exc:  # pragma: no cover - initial red phase only
    HistorianLoadForecast: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None

try:
    from energypod.adapters.providers.ports import LoadForecastProvider
except ImportError:  # pragma: no cover - initial red phase only
    LoadForecastProvider: Any = None


def test_the_load_baseline_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The load baseline contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


SLOT_S = 1800.0
HORIZON_S = 3 * SLOT_S
WEEK = timedelta(days=7)
UNITS = ("mid", "rhs")

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self, wall: datetime) -> None:
        self.wall = wall

    def monotonic(self) -> float:
        return 0.0

    def wall_now(self) -> datetime:
        return self.wall


@dataclass(frozen=True)
class SampleRow:
    unit_id: str
    sampled_at: datetime
    load_power_w: float | None


@dataclass
class FakeHistory:
    rows: list[SampleRow]
    reads: list[tuple[tuple[str, ...], datetime, datetime]] = field(default_factory=list)

    def samples(
        self, unit_ids: tuple[str, ...], from_at: datetime, to_at: datetime
    ) -> tuple[SampleRow, ...]:
        self.reads.append((unit_ids, from_at, to_at))
        return tuple(
            row
            for row in self.rows
            if row.unit_id in unit_ids and from_at <= row.sampled_at < to_at
        )


def _provider(history: FakeHistory) -> Any:
    return HistorianLoadForecast(
        source=history,
        unit_ids=UNITS,
        clock=ManualClock(NOW),
        slot_s=SLOT_S,
        horizon_s=HORIZON_S,
    )


def _last_week_rows(
    slot_start: datetime, loads_by_unit: dict[str, list[float | None]]
) -> list[SampleRow]:
    rows: list[SampleRow] = []
    for unit, loads in loads_by_unit.items():
        for index, load in enumerate(loads):
            rows.append(
                SampleRow(
                    unit_id=unit,
                    sampled_at=slot_start - WEEK + timedelta(seconds=30 * index),
                    load_power_w=load,
                )
            )
    return rows


def _seeded_history() -> FakeHistory:
    """Every slot of the horizon carries one fully-reported timestamp."""
    grid = NOW.replace(minute=0, second=0)
    rows: list[SampleRow] = []
    for step in range(3):
        rows.extend(
            _last_week_rows(
                grid + timedelta(seconds=SLOT_S * step), {"mid": [400.0], "rhs": [300.0]}
            )
        )
    return FakeHistory(rows=rows)


class TestSlotGridAndValues:
    async def test_slots_start_on_the_slot_grid_and_cover_the_horizon(self) -> None:
        history = _seeded_history()
        series = await _provider(history).load_forecast()
        starts = [value.interval_start for value in series.values]
        expected_first = NOW.replace(minute=0, second=0)  # 12:00 is already on the grid
        assert starts == [expected_first + timedelta(seconds=SLOT_S * step) for step in range(3)]
        assert series.values[-1].interval_end == expected_first + timedelta(seconds=HORIZON_S)

    async def test_the_mid_slot_offset_rounds_down_onto_the_grid(self) -> None:
        clock = ManualClock(NOW.replace(minute=10))
        history = _seeded_history()
        series = await HistorianLoadForecast(
            source=history,
            unit_ids=UNITS,
            clock=clock,
            slot_s=SLOT_S,
            horizon_s=HORIZON_S,
        ).load_forecast()
        assert series.values[0].interval_start == NOW.replace(minute=0)

    async def test_each_slot_is_the_mean_of_last_weeks_fleet_sums(self) -> None:
        slot = NOW.replace(minute=0)
        history = FakeHistory(
            rows=_last_week_rows(
                slot,
                {
                    "mid": [400.0, 600.0],  # fleet sums: 700, 900 -> mean 800
                    "rhs": [300.0, 300.0],
                },
            )
        )
        series = await _provider(history).load_forecast()
        first = series.values[0]
        assert first.value == pytest.approx(800.0)
        assert first.variable == "load_power_w"
        assert first.source == "historian-baseline"
        assert first.quantile is None
        assert first.fetched_at == NOW

    async def test_the_read_window_is_exactly_one_week_behind_the_slot(self) -> None:
        slot = NOW.replace(minute=0)
        history = FakeHistory(rows=[])
        await _provider(history).load_forecast()
        _, from_at, to_at = history.reads[0]
        assert from_at == slot - WEEK
        assert to_at == slot - WEEK + timedelta(seconds=SLOT_S)


class TestHonestyRules:
    async def test_a_timestamp_missing_any_unit_is_excluded(self) -> None:
        slot = NOW.replace(minute=0)
        rows = _last_week_rows(slot, {"mid": [400.0, 500.0], "rhs": [300.0]})
        # rhs's second timestamp is gone: only the first fleet sum (700) counts.
        history = FakeHistory(rows=rows)
        series = await _provider(history).load_forecast()
        assert series.values[0].value == pytest.approx(700.0)

    async def test_a_null_load_word_excludes_the_timestamp_never_zero_fills(self) -> None:
        slot = NOW.replace(minute=0)
        rows = _last_week_rows(slot, {"mid": [400.0, None], "rhs": [300.0, 500.0]})
        history = FakeHistory(rows=rows)
        series = await _provider(history).load_forecast()
        assert series.values[0].value == pytest.approx(700.0)

    async def test_a_slot_with_no_usable_history_is_absent_not_zero(self) -> None:
        slot = NOW.replace(minute=0)
        rows = _last_week_rows(
            slot + timedelta(seconds=SLOT_S * 2), {"mid": [100.0], "rhs": [100.0]}
        )
        history = FakeHistory(rows=rows)
        series = await _provider(history).load_forecast()
        assert [value.value for value in series.values] == [pytest.approx(200.0)]
        assert series.values[0].interval_start == slot + timedelta(seconds=SLOT_S * 2)

    async def test_an_empty_historian_yields_an_empty_series(self) -> None:
        series = await _provider(FakeHistory(rows=[])).load_forecast()
        assert series.values == ()
        assert series.source == "historian-baseline"

    async def test_every_call_rereads_the_historian(self) -> None:
        slot = NOW.replace(minute=0)
        history = FakeHistory(rows=_last_week_rows(slot, {"mid": [400.0], "rhs": [300.0]}))
        provider = _provider(history)
        first = await provider.load_forecast()
        assert first.values[0].value == pytest.approx(700.0)
        # One more fully-reported timestamp lands a minute into last week's
        # slot; the reread must pick it up (mean of 700 and 1100).
        history.rows.extend(
            _last_week_rows(slot + timedelta(seconds=60), {"mid": [600.0], "rhs": [500.0]})
        )
        second = await provider.load_forecast()
        assert second.values[0].value == pytest.approx(900.0)
        assert len(history.reads) == 6


def test_the_baseline_satisfies_the_load_port() -> None:
    provider = _provider(FakeHistory(rows=[]))
    assert isinstance(provider, LoadForecastProvider)


class TestConstructionValidation:
    def test_the_slot_horizon_and_units_are_validated(self) -> None:
        history = FakeHistory(rows=[])
        with pytest.raises(ValueError, match="slot_s"):
            HistorianLoadForecast(
                source=history,
                unit_ids=UNITS,
                clock=ManualClock(NOW),
                slot_s=0,
                horizon_s=HORIZON_S,
            )
        with pytest.raises(ValueError, match="horizon_s"):
            HistorianLoadForecast(
                source=history,
                unit_ids=UNITS,
                clock=ManualClock(NOW),
                slot_s=SLOT_S,
                horizon_s=0,
            )
        with pytest.raises(ValueError, match="unit_ids"):
            HistorianLoadForecast(
                source=history,
                unit_ids=(),
                clock=ManualClock(NOW),
                slot_s=SLOT_S,
                horizon_s=HORIZON_S,
            )
        with pytest.raises(ValueError, match="weeks_back"):
            HistorianLoadForecast(
                source=history,
                unit_ids=UNITS,
                clock=ManualClock(NOW),
                slot_s=SLOT_S,
                horizon_s=HORIZON_S,
                weeks_back=0,
            )

    async def test_two_weeks_back_reads_fourteen_days_behind(self) -> None:
        slot = NOW.replace(minute=0)
        history = FakeHistory(rows=[])
        await HistorianLoadForecast(
            source=history,
            unit_ids=UNITS,
            clock=ManualClock(NOW),
            slot_s=SLOT_S,
            horizon_s=SLOT_S,
            weeks_back=2,
        ).load_forecast()
        _, from_at, to_at = history.reads[0]
        assert from_at == slot - 2 * WEEK
        assert to_at == slot - 2 * WEEK + timedelta(seconds=SLOT_S)
