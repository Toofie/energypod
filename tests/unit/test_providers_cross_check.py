"""The forecast-vs-historian cross-check contract.

The architecture's forecast lifecycle says models graduate on evidence:
strategies run in shadow mode and compare proposed actions with actual
outcomes.  The scoring hook here is that comparison's first, smallest piece
-- a PV (or load) forecast scored against the surplus the telemetry historian
actually recorded.  The measured truth is the fleet-summed grid word with the
excess adviser's own sign convention (positive = export, the only surplus
proxy the historian holds), and a timestamp counts only when EVERY configured
unit reported a non-null grid word (the history surface's fleet doctrine --
summing survivors would understate).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

try:
    from energypod.adapters.providers.cross_check import (
        ForecastScore,
        score_forecast_against_surplus,
    )
    from energypod.adapters.providers.model import ForecastSeries, ForecastValue
except ImportError as exc:  # pragma: no cover - initial red phase only
    ForecastScore: Any = None
    score_forecast_against_surplus: Any = None
    ForecastSeries: Any = None
    ForecastValue: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_cross_check_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The cross-check contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


UNITS = ("mid", "rhs")
BASE = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 23, 23, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    unit_id: str
    sampled_at: datetime
    grid_power_w: float | None


def _point_series(values: list[float], *, variable: str = "pv_power_w") -> Any:
    rows = [
        ForecastValue(
            variable=variable,
            interval_start=BASE + timedelta(minutes=30 * index),
            interval_end=BASE + timedelta(minutes=30 * (index + 1)),
            value=value,
            source="solcast",
            fetched_at=FETCHED,
        )
        for index, value in enumerate(values)
    ]
    return ForecastSeries(
        variable=variable, source="solcast", fetched_at=FETCHED, values=tuple(rows)
    )


def _quantile_series(central: list[float], low: list[float], high: list[float]) -> Any:
    rows: list[Any] = []
    for index in range(len(central)):
        start = BASE + timedelta(minutes=30 * index)
        end = start + timedelta(minutes=30)
        for quantile, value in (
            (0.1, low[index]),
            (0.5, central[index]),
            (0.9, high[index]),
        ):
            rows.append(
                ForecastValue(
                    variable="pv_power_w",
                    interval_start=start,
                    interval_end=end,
                    value=value,
                    source="solcast",
                    fetched_at=FETCHED,
                    quantile=quantile,
                )
            )
    return ForecastSeries(
        variable="pv_power_w", source="solcast", fetched_at=FETCHED, values=tuple(rows)
    )


def _rows(grid_by_time: dict[int, dict[str, float | None]]) -> list[Row]:
    return [
        Row(unit_id=unit, sampled_at=BASE + timedelta(minutes=minute), grid_power_w=word)
        for minute, per_unit in grid_by_time.items()
        for unit, word in per_unit.items()
    ]


class TestScoring:
    def test_a_perfect_forecast_scores_zero_error(self) -> None:
        series = _point_series([1000.0, 1000.0])
        rows = _rows(
            {
                5: {"mid": 600.0, "rhs": 400.0},  # fleet surplus 1000
                35: {"mid": 600.0, "rhs": 400.0},
            }
        )
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert isinstance(score, ForecastScore)
        assert score.samples == 2
        assert score.mae_w == pytest.approx(0.0)
        assert score.bias_w == pytest.approx(0.0)
        assert score.rmse_w == pytest.approx(0.0)

    def test_bias_is_signed_and_mae_is_not(self) -> None:
        series = _point_series([1000.0, 1000.0])
        rows = _rows({5: {"mid": 500.0, "rhs": 300.0}, 35: {"mid": 800.0, "rhs": 500.0}})
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        # Errors: +200 (over-forecast), -300 (under-forecast).
        assert score.mae_w == pytest.approx(250.0)
        assert score.bias_w == pytest.approx(-50.0)
        assert score.rmse_w == pytest.approx(math.sqrt((200.0**2 + 300.0**2) / 2))

    def test_import_is_negative_and_scores_honestly(self) -> None:
        series = _point_series([500.0])
        rows = _rows({5: {"mid": -900.0, "rhs": -400.0}})  # importing -1300
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.bias_w == pytest.approx(1800.0)
        assert score.samples == 1

    def test_measured_points_outside_the_forecast_are_ignored(self) -> None:
        series = _point_series([1000.0])
        rows = _rows(
            {
                5: {"mid": 600.0, "rhs": 400.0},
                90: {"mid": 600.0, "rhs": 400.0},  # past the single interval
            }
        )
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.samples == 1

    def test_a_timestamp_missing_any_unit_is_excluded(self) -> None:
        series = _point_series([1000.0])
        rows = _rows({5: {"mid": 600.0}, 20: {"mid": 600.0, "rhs": 400.0}})
        # rhs never reported at :05, so that timestamp is not evidence;
        # the fully-reported :20 lands in the same interval instead.
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.samples == 1
        assert score.mae_w == pytest.approx(0.0)

    def test_a_null_grid_word_excludes_the_timestamp(self) -> None:
        series = _point_series([1000.0])
        rows = _rows({5: {"mid": 600.0, "rhs": None}, 20: {"mid": 600.0, "rhs": 400.0}})
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.samples == 1
        assert score.mae_w == pytest.approx(0.0)

    def test_the_band_coverage_is_the_fraction_inside_the_deciles(self) -> None:
        series = _quantile_series(
            [1000.0, 1000.0, 1000.0],
            [500.0, 500.0, 500.0],
            [1500.0, 1500.0, 1500.0],
        )
        rows = _rows(
            {
                5: {"mid": 600.0, "rhs": 400.0},  # 1000, inside
                35: {"mid": 900.0, "rhs": 900.0},  # 1800, outside the band
                65: {"mid": 300.0, "rhs": 400.0},  # 700, inside
            }
        )
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.inside_band == pytest.approx(2.0 / 3.0)

    def test_a_point_series_carries_no_band_claim(self) -> None:
        series = _point_series([1000.0])
        rows = _rows({5: {"mid": 600.0, "rhs": 400.0}})
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.inside_band is None

    def test_the_central_slice_is_the_median_when_quantiles_exist(self) -> None:
        series = _quantile_series([1000.0], [400.0], [2000.0])
        rows = _rows({5: {"mid": 600.0, "rhs": 400.0}})
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.bias_w == pytest.approx(0.0)

    def test_the_score_names_its_variable(self) -> None:
        series = _point_series([500.0], variable="load_power_w")
        rows = _rows({5: {"mid": 300.0, "rhs": 200.0}})
        score = score_forecast_against_surplus(series, rows, unit_ids=UNITS)
        assert score.variable == "load_power_w"


class TestRefusals:
    def test_no_alignment_is_an_error_never_a_fabricated_zero(self) -> None:
        series = _point_series([1000.0])
        with pytest.raises(ValueError, match="no measured"):
            score_forecast_against_surplus(series, [], unit_ids=UNITS)

    def test_a_non_watt_variable_cannot_be_scored_against_surplus(self) -> None:
        series = _point_series([300.0], variable="irradiance_w_m2")
        rows = _rows({5: {"mid": 600.0, "rhs": 400.0}})
        with pytest.raises(ValueError, match="watt"):
            score_forecast_against_surplus(series, rows, unit_ids=UNITS)

    def test_empty_unit_ids_is_refused(self) -> None:
        series = _point_series([1000.0])
        rows = _rows({5: {"mid": 600.0, "rhs": 400.0}})
        with pytest.raises(ValueError, match="unit_ids"):
            score_forecast_against_surplus(series, rows, unit_ids=())
