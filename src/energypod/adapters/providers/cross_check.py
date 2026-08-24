"""The forecast-vs-historian cross-check scorer.

ARCHITECTURE section 17's forecast lifecycle makes evidence the graduation
path: strategies run in shadow mode and compare proposals with actual
outcomes.  This module is that comparison's first piece -- a watt forecast
(any source: Solcast quantiles, the Open-Meteo derivation, the historian
baseline) scored against the SURPLUS the telemetry historian actually
recorded.

The measured truth is deliberately specific, and since the night-charge V2
panel (DESIGN_NIGHT_CHARGE_V2 section 3.2, amendment A1, 2026-08-24) it is
PRE-BATTERY:

- It is the fleet-summed ``grid_power_w`` (positive = export, the excess
  adviser's sign convention) PLUS the fleet battery CHARGE word (the
  historian's ``battery_watts`` is charge-negative, discharge-positive),
  floored at zero per timestamp.  Surplus that lands on the AC bus splits two
  ways -- exported, or absorbed by the batteries -- and the export channel
  alone is a POST-battery proxy that reads LOW on exactly the mornings the
  forecast was RIGHT: the batteries were taking the surplus, so the meter had
  nothing to show, and an export-only scorer would call the feature's best
  hits its worst misses -- suspension by measurement artifact.
- A timestamp counts only when EVERY configured unit has a row with a
  non-null grid word AND a non-null battery word (the history surface's fleet
  doctrine; summing the survivors would understate site surplus, and an
  absent battery word cannot honestly reconstruct what was absorbed).
- Each counted timestamp pairs with the forecast interval that contains it;
  timestamps outside every interval are simply not evidence.

The score carries the honest minimum: sample count, signed bias (positive =
over-forecast), MAE, RMSE, and -- when the forecast actually claims deciles
-- the fraction of measured points inside its [q10, q90] band.  A forecast
with no overlap raises rather than fabricating a zero-error score.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from energypod.adapters.providers.model import ForecastSeries

__all__ = ["ForecastScore", "SurplusSampleRow", "score_forecast_against_surplus"]

#: The variables whose watts can be compared with a watt surplus.  Irradiance
#: and friends are not commensurable and are refused, not silently scored.
_SCORABLE_VARIABLES = frozenset({"pv_power_w", "load_power_w"})


class SurplusSampleRow(Protocol):
    """The historian row shape the scorer reads (structural, not imported)."""

    @property
    def unit_id(self) -> str: ...

    @property
    def sampled_at(self) -> datetime: ...

    @property
    def grid_power_w(self) -> float | None: ...

    @property
    def battery_watts(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class ForecastScore:
    """One forecast's agreement with the recorded pre-battery surplus."""

    variable: str
    samples: int
    mae_w: float
    bias_w: float
    rmse_w: float
    inside_band: float | None


def score_forecast_against_surplus(
    forecast: ForecastSeries,
    samples: Sequence[SurplusSampleRow],
    *,
    unit_ids: Sequence[str],
) -> ForecastScore:
    """Score one watt forecast against the historian's recorded surplus."""
    units = tuple(unit_ids)
    if not units or any(not isinstance(unit, str) or not unit for unit in units):
        raise ValueError("unit_ids must be a non-empty sequence of unit identifiers")
    if forecast.variable not in _SCORABLE_VARIABLES:
        raise ValueError(
            f"only watt variables {_sorted(_SCORABLE_VARIABLES)} can be scored against a "
            f"watt surplus (got {forecast.variable!r})"
        )
    central = (
        forecast.at_quantile(0.5) if _has_quantile(forecast, 0.5) else forecast.at_quantile(None)
    )
    measured = _measured_surplus(samples, units)
    pairs: list[tuple[float, float]] = []  # (forecast_w, measured_w)
    band_counts = [0, 0]  # [inside, total] over intervals carrying both deciles
    lows = _quantile_by_interval(forecast, 0.1)
    highs = _quantile_by_interval(forecast, 0.9)
    central_by_interval = {
        (value.interval_start, value.interval_end): value.value for value in central.values
    }
    for moment, surplus in measured:
        for (start, end), forecast_w in central_by_interval.items():
            if start <= moment < end:
                pairs.append((forecast_w, surplus))
                if (start, end) in lows and (start, end) in highs:
                    band_counts[1] += 1
                    if lows[(start, end)] <= surplus <= highs[(start, end)]:
                        band_counts[0] += 1
                break
    if not pairs:
        raise ValueError(
            "no measured surplus timestamp aligns with the forecast intervals; "
            "there is nothing to score and no zero-error score may be fabricated"
        )
    errors = [forecast_w - measured_w for forecast_w, measured_w in pairs]
    count = len(errors)
    mae = sum(abs(error) for error in errors) / count
    bias = sum(errors) / count
    rmse = math.sqrt(sum(error * error for error in errors) / count)
    inside_band = None if band_counts[1] == 0 else band_counts[0] / band_counts[1]
    return ForecastScore(
        variable=forecast.variable,
        samples=count,
        mae_w=mae,
        bias_w=bias,
        rmse_w=rmse,
        inside_band=inside_band,
    )


def _sorted(values: frozenset[str]) -> list[str]:
    return sorted(values)


def _has_quantile(series: ForecastSeries, quantile: float) -> bool:
    return any(value.quantile == quantile for value in series.values)


def _quantile_by_interval(
    series: ForecastSeries, quantile: float
) -> dict[tuple[datetime, datetime], float]:
    return {
        (value.interval_start, value.interval_end): value.value
        for value in series.values
        if value.quantile == quantile
    }


def _measured_surplus(
    samples: Sequence[SurplusSampleRow], unit_ids: tuple[str, ...]
) -> list[tuple[datetime, float]]:
    """The PRE-BATTERY fleet surplus, only at fully-reported timestamps.

    Amendment A1's reconstruction: ``max(0, fleet_export + fleet_charge)``
    where the charge word is the charge-NEGATIVE half of each unit's
    ``battery_watts`` (a discharging pod serves the house and adds nothing).
    A timestamp missing ANY unit's grid or battery word is not evidence.
    """
    expected = set(unit_ids)
    by_timestamp: dict[datetime, dict[str, tuple[float | None, float | None]]] = {}
    for row in samples:
        if row.unit_id not in expected:
            continue
        by_timestamp.setdefault(row.sampled_at, {})[row.unit_id] = (
            row.grid_power_w,
            row.battery_watts,
        )
    measured: list[tuple[datetime, float]] = []
    for moment in sorted(by_timestamp):
        per_unit = by_timestamp[moment]
        if set(per_unit) != expected:
            continue
        grids: list[float] = []
        batteries: list[float] = []
        complete = True
        for grid, battery in (per_unit[unit] for unit in expected):
            if grid is None or battery is None:
                complete = False
                break
            grids.append(float(grid))
            batteries.append(float(battery))
        if not complete:
            continue
        export = sum(grids)
        charge = sum(max(0.0, -value) for value in batteries)
        measured.append((moment, max(0.0, export + charge)))
    return measured
