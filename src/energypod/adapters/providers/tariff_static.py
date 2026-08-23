"""The config-declared static tariff schedule (the TariffProvider family).

The architecture defers the wholesale-price integration; until then the
tariff family's first provider is the operator's own declaration -- day-local
rate windows over a flat default, expanded across the horizon in the site's
civil time.  The expansion owns three honesty rules:

- Contiguity by construction: the candidate boundaries are the union of every
  local midnight, every window start, and every window end, so adjacent
  intervals always share an endpoint -- there are no gaps and no overlaps to
  audit away, including across DST boundaries and midnight-crossing windows
  (the 23 h and 25 h days are simply covered).
- Honest metadata: ``market`` is ``static-config`` and ``quality`` is
  ``static`` on every interval, so no consumer can mistake the operator's
  declaration for a market signal; ``published_at`` stays ``None`` because a
  config revision is not a publication event.
- Equal-rate neighbours merge: the interval list carries rate CHANGES, not
  the mechanical boundary grid behind them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.adapters.providers.http import ProviderClock
from energypod.adapters.providers.model import TariffInterval

__all__ = ["StaticTariffProvider", "TariffRateWindow"]

_MARKET = "static-config"
_QUALITY = "static"
_MINUTES_PER_DAY: Final[int] = 24 * 60
_WALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([0-9]{2}):([0-9]{2})$")


def _wall_minutes(raw: str) -> int:
    if not isinstance(raw, str):
        raise ValueError("window_local walls must be HH:MM strings")
    match = _WALL_PATTERN.match(raw)
    if match is None:
        raise ValueError(f"window_local wall {raw!r} is not an HH:MM wall time")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValueError(f"window_local wall {raw!r} is not a time of day")
    return hours * 60 + minutes


class TariffRateWindow:
    """One declared day-local rate window (immutable, validated).

    ``window_local`` is a ``(start, end)`` pair of ``HH:MM`` walls.  A window
    with ``start > end`` crosses midnight: it runs to the end wall on the
    FOLLOWING local day.
    """

    __slots__ = (
        "_end_minute",
        "_start_minute",
        "export_cents_per_kwh",
        "import_cents_per_kwh",
        "window_local",
    )

    def __init__(
        self,
        *,
        window_local: tuple[str, str],
        import_cents_per_kwh: float,
        export_cents_per_kwh: float,
    ) -> None:
        if not isinstance(window_local, tuple) or len(window_local) != 2:
            raise ValueError("window_local must be a (start, end) pair of HH:MM walls")
        start = _wall_minutes(window_local[0])
        end = _wall_minutes(window_local[1])
        if start == end:
            raise ValueError("window_local pairs must not be zero-length")
        for label, value in (
            ("import_cents_per_kwh", import_cents_per_kwh),
            ("export_cents_per_kwh", export_cents_per_kwh),
        ):
            if not isinstance(value, int | float) or isinstance(value, bool) or float(value) < 0:
                raise ValueError(f"{label} must be a non-negative number")
        self.window_local = (window_local[0], window_local[1])
        self.import_cents_per_kwh = float(import_cents_per_kwh)
        self.export_cents_per_kwh = float(export_cents_per_kwh)
        self._start_minute = start
        self._end_minute = end

    def covers_minute(self, minute: int) -> bool:
        """Whether a minute-of-day falls inside this window (wrap-aware)."""
        if self._start_minute < self._end_minute:
            return self._start_minute <= minute < self._end_minute
        return minute >= self._start_minute or minute < self._end_minute

    def minutes(self) -> frozenset[int]:
        """The occupied minute-of-day set (for overlap detection)."""
        if self._start_minute < self._end_minute:
            return frozenset(range(self._start_minute, self._end_minute))
        return frozenset(
            [*range(self._start_minute, _MINUTES_PER_DAY), *range(0, self._end_minute)]
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"TariffRateWindow(window_local={self.window_local!r}, "
            f"import_cents_per_kwh={self.import_cents_per_kwh}, "
            f"export_cents_per_kwh={self.export_cents_per_kwh})"
        )


class StaticTariffProvider:
    """The operator's declared schedule as normalized tariff intervals."""

    def __init__(
        self,
        *,
        clock: ProviderClock,
        timezone_name: str,
        currency: str,
        default_import_cents_per_kwh: float,
        default_export_cents_per_kwh: float,
        windows: Sequence[TariffRateWindow] = (),
        horizon_s: float = 48 * 3600.0,
    ) -> None:
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"timezone_name must be a valid IANA zone: {exc}") from exc
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        for label, value in (
            ("default_import_cents_per_kwh", default_import_cents_per_kwh),
            ("default_export_cents_per_kwh", default_export_cents_per_kwh),
        ):
            if not isinstance(value, int | float) or isinstance(value, bool) or float(value) < 0:
                raise ValueError(f"{label} must be a non-negative number")
        if not 0.0 < float(horizon_s) <= 31 * 86400.0:
            raise ValueError("horizon_s must lie in (0, 31 days]")
        declared = tuple(windows)
        if any(not isinstance(window, TariffRateWindow) for window in declared):
            raise TypeError("windows must all be TariffRateWindow instances")
        occupied: set[int] = set()
        for window in declared:
            overlap = occupied & window.minutes()
            if overlap:
                minute = min(overlap)
                raise ValueError(
                    f"tariff windows overlap at {minute // 60:02d}:{minute % 60:02d} local; "
                    "a timestamp must have exactly one declared rate"
                )
            occupied |= window.minutes()
        self._clock = clock
        self._zone = zone
        self._currency = currency.upper()
        self._default_import = float(default_import_cents_per_kwh)
        self._default_export = float(default_export_cents_per_kwh)
        self._windows = declared
        self._horizon_s = float(horizon_s)

    async def tariff_intervals(self) -> tuple[TariffInterval, ...]:
        """The declared schedule over ``[now, now + horizon)`` in UTC.

        The horizon is a REAL-TIME span: the arithmetic happens on UTC
        instants, so 48 h means 48 real hours across a DST boundary (aware
        ``+ timedelta`` would add wall-clock hours and lose the spring-forward
        hour).
        """
        now = self._clock.wall_now().astimezone(UTC)
        horizon_end = now + timedelta(seconds=self._horizon_s)
        boundaries = self._boundaries(now, horizon_end)
        intervals: list[TariffInterval] = []
        for start, end in pairwise(boundaries):
            if end <= start:
                continue  # a collapsed DST segment is empty, not negative
            rates = self._rates_at(start)
            if intervals and self._same_rates(intervals[-1], rates):
                previous = intervals[-1]
                intervals[-1] = TariffInterval(
                    interval_start=previous.interval_start,
                    interval_end=end,
                    import_cents_per_kwh=rates[0],
                    export_cents_per_kwh=rates[1],
                    currency=self._currency,
                    market=_MARKET,
                    quality=_QUALITY,
                )
                continue
            intervals.append(
                TariffInterval(
                    interval_start=start,
                    interval_end=end,
                    import_cents_per_kwh=rates[0],
                    export_cents_per_kwh=rates[1],
                    currency=self._currency,
                    market=_MARKET,
                    quality=_QUALITY,
                )
            )
        return tuple(intervals)

    def _boundaries(self, now: datetime, horizon_end: datetime) -> list[datetime]:
        """Every instant the rate can change, clipped to the horizon."""
        local_start = now.astimezone(self._zone).date() - timedelta(days=1)
        local_end = horizon_end.astimezone(self._zone).date() + timedelta(days=2)
        boundaries = {now, horizon_end}
        day = local_start
        while day <= local_end:
            midnight = datetime.combine(day, time(0), tzinfo=self._zone)
            boundaries.add(midnight)
            following = midnight + timedelta(days=1)
            for window in self._windows:
                boundaries.add(midnight + timedelta(minutes=window._start_minute))
                end_wall = (
                    midnight + timedelta(minutes=window._end_minute)
                    if window._start_minute < window._end_minute
                    else following + timedelta(minutes=window._end_minute)
                )
                boundaries.add(end_wall)
            day += timedelta(days=1)
        clipped = sorted(boundary for boundary in boundaries if now <= boundary <= horizon_end)
        return clipped or [now, horizon_end]

    def _rates_at(self, moment: datetime) -> tuple[float, float]:
        local = moment.astimezone(self._zone)
        minute = local.hour * 60 + local.minute
        for window in self._windows:
            if window.covers_minute(minute):
                return window.import_cents_per_kwh, window.export_cents_per_kwh
        return self._default_import, self._default_export

    def _same_rates(self, interval: TariffInterval, rates: tuple[float, float]) -> bool:
        return (
            interval.import_cents_per_kwh == rates[0] and interval.export_cents_per_kwh == rates[1]
        )
