"""The Solcast PV adapter (Bearer-keyed, quantiled, site-addressed).

LIVE-DISCOVERED CONTRACT (2026-08-24, verified with the operator's key):
the hobbyist tier REFUSES the ``world_pv_power`` data endpoints -- HTTP 403
with the body ``"Hobbyist accounts are not allowed to access this
endpoint"`` -- and serves the rooftop-sites flow instead:

- ``GET https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts``
  with ``Authorization: Bearer`` auth and ``Accept: application/json``;
  ``hours`` bounds the horizon and ``period`` the averaging enum
  (PT5M/PT10M/PT15M/PT20M/PT30M/PT60M).
- The registered SITE record holds the geography and the plane (this
  site: "Home", capacity 5 kW AC / 6.5 kW DC, azimuth 0 -- Solcast's
  equator-facing convention, which is NORTH for this southern-hemisphere
  site -- tilt 30, loss factor 0.9).  That is why latitude, longitude,
  and capacity are NOT inputs here or in the configuration: the
  ``resource_id`` addresses them all, and duplicating them client-side
  could only drift from what Solcast actually models.
- Rows arrive as ``forecasts[]`` carrying ``pv_estimate`` (the central
  estimate), ``pv_estimate10`` (cloudy-biased) and ``pv_estimate90``
  (clear-sky-biased) -- all in kW -- plus a UTC ``period_end`` (ISO 8601,
  7 fractional digits, ``Z``) and the averaging ``period``; a row covers
  ``[period_end - period, period_end)``.  Observed live: all-zero
  estimates after sundown at ``period_end`` 2026-08-24T09:00:00.0000000Z.
- Auth prefers the ``Authorization: Bearer`` header; the ``api_key`` query
  parameter exists but leaks into server logs and is never used here.
  429 is the rate-limit/quota signal (free hobbyist keys allow 10 requests
  per UTC day), 402 the plan-limit signal, and 403 the tier-refusal class
  discovered live; all surface through the transport as typed provider
  errors, and the refresh gate in the shared base is the quota budget.
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

__all__ = ["SOLCAST_ATTRIBUTION", "SOLCAST_ROOFTOP_SITES_URL", "SolcastPvForecast"]

#: The rooftop-sites flow root; the registered site's resource_id and the
#: ``forecasts`` segment complete the path.
SOLCAST_ROOFTOP_SITES_URL: Final[str] = "https://api.solcast.com.au/rooftop_sites"
#: The free-tier attribution obligation: link this text to
#: https://solcast.com.au wherever the forecast is shown.
SOLCAST_ATTRIBUTION: Final[str] = "solar irradiance data by Solcast"

_SOURCE: Final[str] = "solcast"
#: The documented averaging-period enum (the OpenAPI ``period`` parameter).
SOLCAST_PERIODS: Final[frozenset[str]] = frozenset(
    {"PT5M", "PT10M", "PT15M", "PT20M", "PT30M", "PT60M"}
)
# The estimate fields and the quantile each one claims: ``pv_estimate`` is
# Solcast's central estimate, ``...10`` the cloudy-biased decile, ``...90``
# the clear-sky-biased one -- never a fabricated confidence.
_QUANTILE_FIELDS: Final[tuple[tuple[str, float], ...]] = (
    ("pv_estimate", 0.5),
    ("pv_estimate10", 0.1),
    ("pv_estimate90", 0.9),
)
#: A resource_id is interpolated into the request PATH, so the characters
#: that would escape a path segment refuse construction instead of building
#: a request to somewhere else.
_RESOURCE_ID_FORBIDDEN: Final[frozenset[str]] = frozenset("/?#&=%")
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


def _checked_resource_id(raw: str) -> str:
    """Validate the registered site identifier that rides the request path."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("resource_id must be non-empty with no surrounding whitespace")
    offenders = sorted(set(raw) & _RESOURCE_ID_FORBIDDEN)
    if offenders:
        raise ValueError(
            f"resource_id must be a single URL path segment (carries {offenders}; the "
            "identifier names the registered rooftop site, e.g. b6bf-9d1d-0680-4078)"
        )
    return raw


class SolcastPvForecast(HttpForecastProvider[ForecastSeries]):
    """The quantiled rooftop PV forecast, normalized to watt quantiles.

    The API key arrives as constructor material -- resolved from a secret
    reference by the composition root, never stored in configuration -- and
    rides the ``Authorization: Bearer`` header on every leg.  The
    ``resource_id`` is the registered Solcast site: the site record is the
    one holder of the geography and plane, so this adapter sends neither.
    """

    source = _SOURCE

    def __init__(
        self,
        *,
        transport: ForecastHttpTransport,
        clock: ProviderClock,
        api_key: str,
        resource_id: str,
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
        super().__init__(
            transport=transport,
            clock=clock,
            refresh_interval_s=refresh_interval_s,
            stale_after_s=stale_after_s,
        )
        self._api_key = api_key.strip()
        self._resource_id = _checked_resource_id(resource_id)
        self._hours = hours
        self._period = period

    async def pv_forecast(self) -> ForecastSeries:
        """The rooftop forecast as watt quantiles by half-open interval."""
        return await self.fetch()

    async def _request(self) -> ForecastSeries:
        payload = await self._get_json(
            f"{SOLCAST_ROOFTOP_SITES_URL}/{self._resource_id}/forecasts",
            params={
                "hours": str(self._hours),
                "period": self._period,
            },
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
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
