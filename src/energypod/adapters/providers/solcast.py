"""The Solcast PV adapter (Bearer-keyed, quantiled).

The wire facts encoded here come from Solcast's current docs and their
OpenAPI spec (https://docs.solcast.com.au, parsed from
https://api.solcast.com.au/openapi/v1/openapi.json, verified 2026-08-24):

- ``GET https://api.solcast.com.au/data/forecast/rooftop_pv_power`` with
  required ``latitude``/``longitude`` and ``capacity`` (kW, the greater of
  inverter AC or module DC); ``hours`` bounds the horizon; ``period`` is an
  ISO-8601 duration from the enum PT5M/PT10M/PT15M/PT20M/PT30M/PT60M.
- ``output_parameters`` selects ``pv_power_rooftop`` (the central estimate),
  ``pv_power_rooftop10`` (cloudy-biased) and ``pv_power_rooftop90``
  (clear-sky-biased) -- ALL in kW of inverter AC output.
- Rows arrive as ``forecasts[]`` with a UTC ``period_end`` (ISO 8601,
  7 fractional digits, ``Z``) and the averaging ``period``; a row covers
  ``[period_end - period, period_end)``.
- Auth prefers the ``Authorization: Bearer`` header; the ``api_key`` query
  parameter exists but leaks into server logs and is never used here.  429
  is the rate-limit/quota signal (free hobbyist keys allow 10 requests per
  UTC day) and 402 the plan-limit signal; both surface through the transport
  as typed provider errors, and the refresh gate in the shared base is the
  quota budget.
- Attribution (free tier): link "solar irradiance data" back to Solcast.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Final

from energypod.adapters.providers.http import (
    ForecastHttpTransport,
    HttpForecastProvider,
    ProviderClock,
)
from energypod.adapters.providers.model import ForecastSeries, ForecastValue

__all__ = ["SOLCAST_API_URL", "SOLCAST_ATTRIBUTION", "SolcastPvForecast"]

SOLCAST_API_URL: Final[str] = "https://api.solcast.com.au/data/forecast/rooftop_pv_power"
#: The free-tier attribution obligation: link this text to
#: https://solcast.com.au wherever the forecast is shown.
SOLCAST_ATTRIBUTION: Final[str] = "solar irradiance data by Solcast"

_SOURCE: Final[str] = "solcast"
#: The documented averaging-period enum (the OpenAPI ``period`` parameter).
SOLCAST_PERIODS: Final[frozenset[str]] = frozenset(
    {"PT5M", "PT10M", "PT15M", "PT20M", "PT30M", "PT60M"}
)
# The output parameters and the quantile each one claims: ``pv_power_rooftop``
# is Solcast's central estimate, ``...10`` the cloudy-biased decile, ``...90``
# the clear-sky-biased one -- never a fabricated confidence.
_QUANTILE_FIELDS: Final[tuple[tuple[str, float], ...]] = (
    ("pv_power_rooftop", 0.5),
    ("pv_power_rooftop10", 0.1),
    ("pv_power_rooftop90", 0.9),
)
_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^P(?:(?P<days>[0-9]+)D)?(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?"
    r"(?:(?P<seconds>[0-9]+(?:\.[0-9]+)?)S)?)?$"
)


def _iso_duration(raw: Any) -> timedelta:
    """Parse the ISO-8601 durations Solcast's ``period`` uses (PT30M...)."""
    if not isinstance(raw, str):
        raise ValueError(f"period {raw!r} is not an ISO-8601 duration")
    match = _DURATION_PATTERN.match(raw)
    if match is None or raw == "P" or raw.endswith("T"):
        raise ValueError(f"period {raw!r} is not a parsable ISO-8601 duration")
    parts = match.groupdict()
    if all(value is None for value in parts.values()):
        raise ValueError(f"period {raw!r} carries no time component")
    return timedelta(
        days=int(parts["days"] or 0),
        hours=int(parts["hours"] or 0),
        minutes=int(parts["minutes"] or 0),
        seconds=float(parts["seconds"] or 0.0),
    )


def _geo(value: float, label: str, bound: float) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not -bound <= float(value) <= bound
    ):
        raise ValueError(f"{label} must lie in [{-bound}, {bound}] degrees")
    return float(value)


class SolcastPvForecast(HttpForecastProvider[ForecastSeries]):
    """The commercial rooftop PV forecast, normalized to watt quantiles.

    The API key arrives as constructor material -- resolved from a secret
    reference by the composition root, never stored in configuration -- and
    rides the ``Authorization: Bearer`` header on every leg.
    """

    source = _SOURCE

    def __init__(
        self,
        *,
        transport: ForecastHttpTransport,
        clock: ProviderClock,
        api_key: str,
        latitude: float,
        longitude: float,
        capacity_kw: float,
        hours: int = 48,
        period: str = "PT30M",
        refresh_interval_s: float = 900.0,
        stale_after_s: float = 3600.0,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be non-empty key material")
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 336:
            raise ValueError("hours must lie in [1, 336] (the 14-day horizon)")
        if not isinstance(period, str) or period not in SOLCAST_PERIODS:
            raise ValueError(
                f"period must be one of {sorted(SOLCAST_PERIODS)} (the documented enum)"
            )
        if (
            not isinstance(capacity_kw, int | float)
            or isinstance(capacity_kw, bool)
            or not (float(capacity_kw) > 0)
        ):
            raise ValueError("capacity_kw must be positive (the installed kWp)")
        super().__init__(
            transport=transport,
            clock=clock,
            refresh_interval_s=refresh_interval_s,
            stale_after_s=stale_after_s,
        )
        self._api_key = api_key.strip()
        self._latitude = _geo(latitude, "latitude", 90.0)
        self._longitude = _geo(longitude, "longitude", 180.0)
        self._capacity_kw = float(capacity_kw)
        self._hours = hours
        self._period = period

    async def pv_forecast(self) -> ForecastSeries:
        """The rooftop forecast as watt quantiles by half-open interval."""
        return await self.fetch()

    async def _request(self) -> ForecastSeries:
        payload = await self._get_json(
            SOLCAST_API_URL,
            params={
                "latitude": str(self._latitude),
                "longitude": str(self._longitude),
                "capacity": str(self._capacity_kw),
                "hours": str(self._hours),
                "period": self._period,
                "output_parameters": ",".join(field for field, _ in _QUANTILE_FIELDS),
                "format": "json",
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("forecasts"), list):
            raise ValueError("the payload carries no forecasts array")
        fetched = self._clock.wall_now()
        values: list[ForecastValue] = []
        for row in payload["forecasts"]:
            if not isinstance(row, dict):
                raise ValueError("a forecast row is not an object")
            length = _iso_duration(row.get("period"))
            end_raw = row.get("period_end")
            if not isinstance(end_raw, str):
                raise ValueError("a forecast row carries no period_end")
            end = datetime.fromisoformat(end_raw)
            if end.tzinfo is None:
                raise ValueError("period_end must carry its UTC offset")
            start = end - length
            for field, quantile in _QUANTILE_FIELDS:
                kilowatts = row.get(field)
                if kilowatts is None:
                    continue  # an absent slice stays absent
                values.append(
                    ForecastValue(
                        variable="pv_power_w",
                        interval_start=start,
                        interval_end=end,
                        value=float(kilowatts) * 1000.0,
                        source=_SOURCE,
                        fetched_at=fetched,
                        quantile=quantile,
                    )
                )
        values.sort(key=lambda value: (value.interval_start, value.quantile or 0.0))
        return ForecastSeries(
            variable="pv_power_w", source=_SOURCE, fetched_at=fetched, values=tuple(values)
        )
