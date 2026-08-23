"""The Open-Meteo weather + PV adapters (keyless).

Open-Meteo is the keyless reference source, which makes it the testable
member of the weather/PV families.  The wire facts this adapter encodes
(documented at https://open-meteo.com/en/docs, verified live 2026-08-24):

- One endpoint serves everything: ``GET https://api.open-meteo.com/v1/forecast``.
  No key below 10,000 calls/day, non-commercial use, CC BY 4.0 -- the
  attribution line must accompany any surface that shows the data.
- With ``timezone=auto`` the ``hourly.time`` stamps are LOCAL ISO strings and
  the response carries ``utc_offset_seconds``; UTC = local minus the offset.
- ``shortwave_radiation`` is W/m2, ``cloud_cover`` percent, ``temperature_2m``
  degrees Celsius; a null entry is an absent hour, never a zero.
- There is NO PV power variable (``hourly=power`` answers 400).  The PV
  adapter therefore derives watts client-side from
  ``global_tilted_irradiance`` (W/m2, computed by Open-Meteo from the
  ``tilt``/``azimuth`` request parameters) as
  ``capacity_kw * GTI * derate`` -- a proportional plane-of-array model whose
  temperature/soiling/inverter losses the operator folds into ``derate``.
- The azimuth convention is the DOCS-PAGE one the live A/B confirmed
  (0 = south, -90 = east, +90 = west, +-180 = north); the OpenAPI YAML's
  "North=0" wording is wrong and is not encoded here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from energypod.adapters.providers.http import (
    ForecastHttpTransport,
    HttpForecastProvider,
    ProviderClock,
)
from energypod.adapters.providers.model import ForecastSeries, ForecastValue

__all__ = [
    "OPEN_METEO_ATTRIBUTION",
    "OPEN_METEO_BASE_URL",
    "OpenMeteoPvForecast",
    "OpenMeteoWeather",
]

OPEN_METEO_BASE_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"
#: The CC BY 4.0 attribution obligation (https://open-meteo.com/en/licence):
#: link this text to https://open-meteo.com/ wherever the data is shown.
OPEN_METEO_ATTRIBUTION: Final[str] = "Weather data by Open-Meteo.com"

_SOURCE: Final[str] = "open-meteo"
# The served hourly variables, in request order, mapped onto the normalized
# vocabulary (units ride the variable names).
_WEATHER_VARIABLES: Final[tuple[tuple[str, str], ...]] = (
    ("shortwave_radiation", "irradiance_w_m2"),
    ("cloud_cover", "cloud_cover_pct"),
    ("temperature_2m", "temperature_c"),
)
_GTI_VARIABLE: Final[str] = "global_tilted_irradiance"


def _geo(value: float, label: str, bound: float) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not -bound <= float(value) <= bound
    ):
        raise ValueError(f"{label} must lie in [{-bound}, {bound}] degrees")
    return float(value)


def _parse_hourly_times(payload: Any) -> tuple[list[datetime], list[timedelta]]:
    """The UTC interval starts and step sizes behind ``hourly.time``."""
    if not isinstance(payload, dict):
        raise ValueError("the Open-Meteo payload must be an object")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError("the payload carries no hourly.time array")
    offset_raw = payload.get("utc_offset_seconds")
    if not isinstance(offset_raw, int | float):
        raise ValueError("the payload carries no utc_offset_seconds")
    offset = timedelta(seconds=float(offset_raw))
    starts: list[datetime] = []
    for raw in hourly["time"]:
        local = datetime.fromisoformat(str(raw)).replace(tzinfo=None)
        starts.append((local - offset).replace(tzinfo=UTC))
    steps: list[timedelta] = []
    for index in range(len(starts)):
        if index + 1 < len(starts):
            steps.append(starts[index + 1] - starts[index])
        else:
            previous = steps[-1] if steps else timedelta(hours=1)
            steps.append(previous)
    return starts, steps


def _hourly_values(hourly: dict[str, Any], wire_name: str) -> list[Any]:
    values = hourly.get(wire_name)
    if not isinstance(values, list):
        raise ValueError(f"the payload carries no hourly.{wire_name} array")
    return values


def _series_from_hourly(
    payload: Any,
    *,
    wire_name: str,
    variable: str,
    fetched_at: datetime,
) -> ForecastSeries:
    """One normalized series from one hourly variable (nulls skipped)."""
    starts, steps = _parse_hourly_times(payload)
    values = _hourly_values(payload["hourly"], wire_name)
    if len(values) != len(starts):
        raise ValueError(f"hourly.{wire_name} and hourly.time disagree in length")
    rows: list[ForecastValue] = []
    for moment, step, raw in zip(starts, steps, values, strict=True):
        if raw is None:
            continue  # an absent hour is absent, never zero
        rows.append(
            ForecastValue(
                variable=variable,
                interval_start=moment,
                interval_end=moment + step,
                value=float(raw),
                source=_SOURCE,
                fetched_at=fetched_at,
            )
        )
    return ForecastSeries(
        variable=variable, source=_SOURCE, fetched_at=fetched_at, values=tuple(rows)
    )


class OpenMeteoWeather(HttpForecastProvider[dict[str, ForecastSeries]]):
    """The keyless weather family adapter: one leg, every served variable."""

    source = _SOURCE

    def __init__(
        self,
        *,
        transport: ForecastHttpTransport,
        clock: ProviderClock,
        latitude: float,
        longitude: float,
        forecast_days: int = 2,
        refresh_interval_s: float = 900.0,
        stale_after_s: float = 3600.0,
    ) -> None:
        if (
            not isinstance(forecast_days, int)
            or isinstance(forecast_days, bool)
            or not (1 <= forecast_days <= 16)
        ):
            raise ValueError("forecast_days must lie in [1, 16]")
        super().__init__(
            transport=transport,
            clock=clock,
            refresh_interval_s=refresh_interval_s,
            stale_after_s=stale_after_s,
        )
        self._latitude = _geo(latitude, "latitude", 90.0)
        self._longitude = _geo(longitude, "longitude", 180.0)
        self._forecast_days = forecast_days

    async def weather_forecast(self) -> dict[str, ForecastSeries]:
        """Every served weather variable, one wire leg, one shared stamp."""
        return await self.fetch()

    async def _request(self) -> dict[str, ForecastSeries]:
        payload = await self._get_json(
            OPEN_METEO_BASE_URL,
            params={
                "latitude": str(self._latitude),
                "longitude": str(self._longitude),
                "hourly": ",".join(wire for wire, _ in _WEATHER_VARIABLES),
                "timezone": "auto",
                "forecast_days": str(self._forecast_days),
            },
        )
        fetched = self._clock.wall_now()
        return {
            variable: _series_from_hourly(
                payload, wire_name=wire, variable=variable, fetched_at=fetched
            )
            for wire, variable in _WEATHER_VARIABLES
        }


class OpenMeteoPvForecast(HttpForecastProvider[ForecastSeries]):
    """The keyless PV family adapter: GTI-derived watts (no power variable)."""

    source = _SOURCE

    def __init__(
        self,
        *,
        transport: ForecastHttpTransport,
        clock: ProviderClock,
        latitude: float,
        longitude: float,
        tilt_deg: float,
        azimuth_deg: float,
        capacity_kw: float,
        derate: float = 0.9,
        forecast_days: int = 2,
        refresh_interval_s: float = 900.0,
        stale_after_s: float = 3600.0,
    ) -> None:
        if (
            not isinstance(capacity_kw, int | float)
            or isinstance(capacity_kw, bool)
            or not (float(capacity_kw) > 0)
        ):
            raise ValueError("capacity_kw must be positive (the installed kWp)")
        if (
            not isinstance(derate, int | float)
            or isinstance(derate, bool)
            or not (0.0 < float(derate) <= 1.0)
        ):
            raise ValueError("derate must lie in (0, 1] (the plane-to-AC loss factor)")
        if (
            not isinstance(forecast_days, int)
            or isinstance(forecast_days, bool)
            or not (1 <= forecast_days <= 16)
        ):
            raise ValueError("forecast_days must lie in [1, 16]")
        super().__init__(
            transport=transport,
            clock=clock,
            refresh_interval_s=refresh_interval_s,
            stale_after_s=stale_after_s,
        )
        self._latitude = _geo(latitude, "latitude", 90.0)
        self._longitude = _geo(longitude, "longitude", 180.0)
        if not 0.0 <= float(tilt_deg) <= 90.0:
            raise ValueError("tilt_deg must lie in [0, 90] (0 horizontal, 90 vertical)")
        self._tilt_deg = float(tilt_deg)
        # The docs-page azimuth convention: 0 = south, -90 = east, +90 =
        # west, +-180 = north (NOT the OpenAPI YAML's "North=0" wording).
        self._azimuth_deg = _geo(azimuth_deg, "azimuth_deg", 180.0)
        self._capacity_kw = float(capacity_kw)
        self._derate = float(derate)
        self._forecast_days = forecast_days

    async def pv_forecast(self) -> ForecastSeries:
        """The derived PV power forecast in watts (deterministic, no quantiles)."""
        return await self.fetch()

    async def _request(self) -> ForecastSeries:
        payload = await self._get_json(
            OPEN_METEO_BASE_URL,
            params={
                "latitude": str(self._latitude),
                "longitude": str(self._longitude),
                "hourly": _GTI_VARIABLE,
                "tilt": str(self._tilt_deg),
                "azimuth": str(self._azimuth_deg),
                "timezone": "auto",
                "forecast_days": str(self._forecast_days),
            },
        )
        fetched = self._clock.wall_now()
        gti = _series_from_hourly(
            payload,
            wire_name=_GTI_VARIABLE,
            variable="irradiance_w_m2",
            fetched_at=fetched,
        )
        watts = [
            ForecastValue(
                variable="pv_power_w",
                interval_start=value.interval_start,
                interval_end=value.interval_end,
                value=self._capacity_kw * value.value * self._derate,
                source=_SOURCE,
                fetched_at=fetched,
            )
            for value in gti.values
        ]
        return ForecastSeries(
            variable="pv_power_w", source=_SOURCE, fetched_at=fetched, values=tuple(watts)
        )
