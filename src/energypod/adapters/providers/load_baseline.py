"""The historian-statistics load baseline (the LoadForecastProvider family).

No external load-forecast API is wired, so the load family's first provider
is deliberately naive and local: for every forecast slot ahead, the SAME SLOT
``weeks_back`` weeks ago is read from the telemetry historian and reduced to
the mean of the fleet-summed load words.  It is a baseline to graduate from
(the architecture's forecast-model lifecycle), not a model -- and every
honesty rule is structural:

- The fleet doctrine mirrors the history query surface: a timestamp counts
  only when EVERY configured unit has a row with a non-null ``load_power_w``.
  A missing unit or a null word excludes the timestamp entirely -- summing
  the survivors would silently understate site load.
- A slot with no usable history is ABSENT from the series, never zero-filled
  (an absent datum is not a zero).
- Every call rereads the historian: the baseline tracks the record it stands
  on, with no cache to go stale behind it.

The window alignment is exactly ``weeks_back * 7 * 24 h`` in UTC.  A DST
shift of up to one hour therefore lands outside the slot on transition days;
that is an accepted error for a baseline (the docstring is the disclosure),
and a graduating model replaces the whole strategy rather than patching it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from energypod.adapters.providers.http import ProviderClock
from energypod.adapters.providers.model import ForecastSeries, ForecastValue

__all__ = ["HistorianLoadForecast", "HistoryLoadSource", "LoadSampleRow"]

_SOURCE_NAME = "historian-baseline"
_WEEK = timedelta(days=7)


class LoadSampleRow(Protocol):
    """The historian row shape the baseline reads (structural, not imported)."""

    @property
    def unit_id(self) -> str: ...

    @property
    def sampled_at(self) -> datetime: ...

    @property
    def load_power_w(self) -> float | None: ...


class HistoryLoadSource(Protocol):
    """The historian read port (the shipped history repositories satisfy it)."""

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> Sequence[LoadSampleRow]: ...


class HistorianLoadForecast:
    """Last-week's same-slot fleet load as tomorrow's baseline."""

    def __init__(
        self,
        *,
        source: HistoryLoadSource,
        unit_ids: Sequence[str],
        clock: ProviderClock,
        slot_s: float = 1800.0,
        horizon_s: float = 12 * 3600.0,
        weeks_back: int = 1,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(not isinstance(unit, str) or not unit for unit in units):
            raise ValueError("unit_ids must be a non-empty sequence of unit identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        if not 0.0 < float(slot_s) <= 86400.0:
            raise ValueError("slot_s must lie in (0, 86400] seconds")
        if not 0.0 < float(horizon_s) <= 14 * 86400.0:
            raise ValueError("horizon_s must lie in (0, 14 days]")
        if not isinstance(weeks_back, int) or isinstance(weeks_back, bool) or weeks_back < 1:
            raise ValueError("weeks_back must be a positive integer")
        self._source = source
        self._unit_ids = units
        self._clock = clock
        self._slot_s = float(slot_s)
        self._horizon_s = float(horizon_s)
        self._weeks_back = weeks_back

    async def load_forecast(self) -> ForecastSeries:
        """The same-slot-last-week baseline over the configured horizon."""
        now = self._clock.wall_now()
        fetched = now.replace(microsecond=0)
        slot = timedelta(seconds=self._slot_s)
        offset = _WEEK * self._weeks_back
        slot_count = int(self._horizon_s // self._slot_s)
        grid_start = _floor_to_grid(now, self._slot_s)
        values: list[ForecastValue] = []
        for step in range(slot_count):
            start = grid_start + slot * step
            end = start + slot
            rows = self._source.samples(self._unit_ids, start - offset, end - offset)
            mean = _fleet_mean_watts(rows, self._unit_ids)
            if mean is None:
                continue  # an absent slot stays absent, never zero
            values.append(
                ForecastValue(
                    variable="load_power_w",
                    interval_start=start,
                    interval_end=end,
                    value=mean,
                    source=_SOURCE_NAME,
                    fetched_at=fetched,
                )
            )
        return ForecastSeries(
            variable="load_power_w",
            source=_SOURCE_NAME,
            fetched_at=fetched,
            values=tuple(values),
        )


def _floor_to_grid(moment: datetime, slot_s: float) -> datetime:
    """The grid origin: ``now`` floored onto multiples of ``slot_s`` in UTC."""
    seconds = moment.timestamp()
    floored = seconds - (seconds % slot_s)
    return datetime.fromtimestamp(floored, tz=moment.tzinfo)


def _fleet_mean_watts(rows: Sequence[LoadSampleRow], unit_ids: Sequence[str]) -> float | None:
    """The mean fleet-summed load, counting only fully-reported timestamps."""
    expected = set(unit_ids)
    by_timestamp: dict[datetime, dict[str, float | None]] = {}
    for row in rows:
        if row.unit_id not in expected:
            continue
        by_timestamp.setdefault(row.sampled_at, {})[row.unit_id] = row.load_power_w
    sums: list[float] = []
    for per_unit in by_timestamp.values():
        if set(per_unit) != expected:
            continue
        if any(load is None for load in per_unit.values()):
            continue
        sums.append(sum(float(load) for load in per_unit.values() if load is not None))
    if not sums:
        return None
    return sum(sums) / len(sums)
