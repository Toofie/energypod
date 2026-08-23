"""The composed forecast-provider registry (the advisory handle).

One frozen record the composition root hands to whoever consumes forecasts
next -- today nobody (advisory-only, deliberately unwired), tomorrow an
adviser's fetch loop.  The registry is a value, not a service: it starts no
tasks, holds no sockets (constructing an HTTP adapter opens nothing), and
carries ``notes`` for every provider the configuration declared but the
environment could not deliver (a missing Solcast key, for instance) so the
omission is visible instead of silent.
"""

from __future__ import annotations

from dataclasses import dataclass

from energypod.adapters.providers.http import HttpForecastProvider, ProviderStaleness
from energypod.adapters.providers.ports import (
    LoadForecastProvider,
    PvForecastProvider,
    TariffProvider,
    WeatherProvider,
)

__all__ = ["ForecastProviderRegistry"]


@dataclass(frozen=True, slots=True)
class ForecastProviderRegistry:
    """The advisory provider families one composition could deliver."""

    weather: WeatherProvider | None = None
    pv: PvForecastProvider | None = None
    load: LoadForecastProvider | None = None
    tariff: TariffProvider | None = None
    notes: tuple[str, ...] = ()

    def staleness_reports(self) -> tuple[ProviderStaleness, ...]:
        """The honest age report for every wire-backed member, in family order.

        Local members (the historian baseline, the static tariff) recompute on
        every read and carry no cache, so they report nothing here.
        """
        reports: list[ProviderStaleness] = []
        for provider in (self.weather, self.pv):
            if isinstance(provider, HttpForecastProvider):
                reports.append(provider.staleness())
        return tuple(reports)
