"""The provider family ports (ARCHITECTURE sections 13 and 17).

The architecture reserves four advisory provider families and names the
normalized data each owes: tariff intervals, weather observations, PV power
forecast quantiles, and load demand quantiles.  These protocols are that
reservation as importable contracts -- the interfaces a future adviser or
projection consumes, with no method that could actuate anything.

Every method returns already-normalized model data; every implementation owns
its own fetching, caching, and staleness story (the HTTP base for wire
sources, plain reads for local ones).  The ports are deliberately minimal:
one read per family, no configuration surface, no lifecycle methods.  A
provider that cannot serve data raises :class:`ProviderUnavailableError`;
consumers fall back, they never retry-storm.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from energypod.adapters.providers.model import ForecastSeries, TariffInterval

__all__ = [
    "LoadForecastProvider",
    "PvForecastProvider",
    "TariffProvider",
    "WeatherProvider",
]


@runtime_checkable
class TariffProvider(Protocol):
    """Import/export price intervals with currency, market, and quality."""

    async def tariff_intervals(self) -> tuple[TariffInterval, ...]:
        """The price intervals currently in force and ahead, interval-sorted."""
        ...


@runtime_checkable
class WeatherProvider(Protocol):
    """Irradiance, cloud, and temperature by interval.

    Weather is inherently multi-variable, so one fetch returns a mapping
    keyed by the :class:`~energypod.adapters.providers.model.ForecastVariable`
    names present -- every member a fully normalized series sharing the same
    fetch stamp.
    """

    async def weather_forecast(self) -> Mapping[str, ForecastSeries]:
        """The site weather forecast, one series per served variable."""
        ...


@runtime_checkable
class PvForecastProvider(Protocol):
    """Site PV power quantiles by interval (kW-scale sources normalize to W)."""

    async def pv_forecast(self) -> ForecastSeries:
        """The site PV forecast, quantiles included when the source has them."""
        ...


@runtime_checkable
class LoadForecastProvider(Protocol):
    """Site load demand by interval (quantiles when the model has them)."""

    async def load_forecast(self) -> ForecastSeries:
        """The site load forecast as normalized interval values."""
        ...
