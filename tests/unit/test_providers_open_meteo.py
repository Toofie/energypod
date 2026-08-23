"""The Open-Meteo weather + PV adapter contracts (keyless, so testable).

Open-Meteo serves one forecast endpoint (https://api.open-meteo.com/v1/forecast,
no key below 10k calls/day, CC BY 4.0 attribution).  With ``timezone=auto`` the
``hourly.time`` stamps are LOCAL and the response carries
``utc_offset_seconds``; radiation is W/m2.  There is NO PV power variable --
the PV adapter derives watts client-side from ``global_tilted_irradiance``
under the site's declared tilt/azimuth/capacity/derate, and the azimuth
convention is the docs-page one the live A/B verified (0 = south, -90 = east,
+90 = west), not the wrong OpenAPI-YAML wording.  These tests pin the request
shapes, the local-to-UTC normalization, null-skipping, and the derivation --
all against a scripted fake transport, never a socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

try:
    from energypod.adapters.providers.http import ProviderUnavailableError
    from energypod.adapters.providers.model import ForecastVariable
    from energypod.adapters.providers.open_meteo import (
        OPEN_METEO_ATTRIBUTION,
        OpenMeteoPvForecast,
        OpenMeteoWeather,
    )
    from energypod.adapters.providers.ports import PvForecastProvider, WeatherProvider
except ImportError as exc:  # pragma: no cover - initial red phase only
    OPEN_METEO_ATTRIBUTION: Any = None
    OpenMeteoPvForecast: Any = None
    OpenMeteoWeather: Any = None
    PvForecastProvider: Any = None
    WeatherProvider: Any = None
    ProviderUnavailableError: Any = None
    ForecastVariable: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_open_meteo_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The Open-Meteo adapter contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


BASE = "https://api.open-meteo.com/v1/forecast"
WALL = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
LAT = -27.4698
LON = 153.0251


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return WALL


@dataclass
class FakeTransport:
    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def get_json(
        self, url: str, *, params: dict[str, str], headers: dict[str, str] | None = None
    ) -> Any:
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers or {})})
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _weather_payload() -> dict[str, Any]:
    # Brisbane: utc_offset_seconds 36000 == +10:00 local.  Local 10:00 is
    # 00:00 UTC; one hourly slot per variable is null (absent, not zero).
    return {
        "latitude": LAT,
        "longitude": LON,
        "utc_offset_seconds": 36000,
        "timezone": "Australia/Brisbane",
        "hourly_units": {
            "time": "iso8601",
            "shortwave_radiation": "W/m²",
            "cloud_cover": "%",
            "temperature_2m": "°C",
        },
        "hourly": {
            "time": ["2026-08-24T10:00", "2026-08-24T11:00"],
            "shortwave_radiation": [650.0, None],
            "cloud_cover": [20, None],
            "temperature_2m": [24.5, None],
        },
    }


def _weather(transport: FakeTransport) -> Any:
    return OpenMeteoWeather(
        transport=transport,
        clock=ManualClock(),
        latitude=LAT,
        longitude=LON,
        forecast_days=2,
        refresh_interval_s=900.0,
        stale_after_s=3600.0,
    )


class TestWeatherAdapter:
    async def test_the_request_shape_is_the_documented_one(self) -> None:
        transport = FakeTransport(script=[_weather_payload()])
        provider = _weather(transport)
        await provider.weather_forecast()
        call = transport.calls[0]
        assert call["url"] == BASE
        assert call["params"]["latitude"] == str(LAT)
        assert call["params"]["longitude"] == str(LON)
        assert call["params"]["timezone"] == "auto"
        assert call["params"]["forecast_days"] == "2"
        assert set(call["params"]["hourly"].split(",")) == {
            "shortwave_radiation",
            "cloud_cover",
            "temperature_2m",
        }
        # Keyless: no authorization material anywhere on the leg.
        assert "Authorization" not in call["headers"]

    async def test_local_hours_normalize_to_utc_intervals(self) -> None:
        transport = FakeTransport(script=[_weather_payload()])
        provider = _weather(transport)
        bundle = await provider.weather_forecast()
        irradiance = bundle["irradiance_w_m2"]
        assert irradiance.variable == "irradiance_w_m2"
        assert irradiance.source == "open-meteo"
        # Local 10:00 at +10:00 == 00:00 UTC, one hour slot.
        assert irradiance.values[0].interval_start == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
        assert irradiance.values[0].interval_end == datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
        assert irradiance.values[0].value == 650.0
        # The null second hour is absent, never zero-filled.
        assert len(irradiance.values) == 1

    async def test_the_bundle_carries_every_weather_variable(self) -> None:
        transport = FakeTransport(script=[_weather_payload()])
        provider = _weather(transport)
        bundle = await provider.weather_forecast()
        assert set(bundle) == {"irradiance_w_m2", "cloud_cover_pct", "temperature_c"}
        assert bundle["cloud_cover_pct"].values[0].value == 20.0
        assert bundle["temperature_c"].values[0].value == 24.5
        for series in bundle.values():
            assert series.fetched_at == WALL
            assert all(value.quantile is None for value in series.values)

    async def test_every_series_in_the_bundle_shares_one_wire_leg(self) -> None:
        transport = FakeTransport(script=[_weather_payload(), _weather_payload()])
        provider = _weather(transport)
        first = await provider.weather_forecast()
        again = await provider.weather_forecast()
        assert len(transport.calls) == 1
        assert first["irradiance_w_m2"] is again["irradiance_w_m2"]

    async def test_a_malformed_payload_is_unavailable_not_a_crash(self) -> None:
        transport = FakeTransport(script=[{"unexpected": True}])
        provider = _weather(transport)
        with pytest.raises(ProviderUnavailableError):
            await provider.weather_forecast()

    def test_the_attribution_line_is_pinned(self) -> None:
        assert "Open-Meteo.com" in OPEN_METEO_ATTRIBUTION

    def test_the_adapter_satisfies_the_weather_port(self) -> None:
        assert isinstance(_weather(FakeTransport(script=[])), WeatherProvider)


def _pv_payload() -> dict[str, Any]:
    return {
        "latitude": LAT,
        "longitude": LON,
        "utc_offset_seconds": 36000,
        "timezone": "Australia/Brisbane",
        "hourly_units": {"time": "iso8601", "global_tilted_irradiance": "W/m²"},
        "hourly": {
            "time": ["2026-08-24T10:00", "2026-08-24T11:00"],
            "global_tilted_irradiance": [650.0, 800.0],
        },
    }


def _pv(transport: FakeTransport, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "transport": transport,
        "clock": ManualClock(),
        "latitude": LAT,
        "longitude": LON,
        "tilt_deg": 25.0,
        "azimuth_deg": 10.0,
        "capacity_kw": 5.0,
        "derate": 0.9,
        "refresh_interval_s": 900.0,
        "stale_after_s": 3600.0,
    }
    fields.update(overrides)
    return OpenMeteoPvForecast(**fields)


class TestPvAdapter:
    async def test_the_request_declares_the_plane_and_the_gti_variable(self) -> None:
        transport = FakeTransport(script=[_pv_payload()])
        provider = _pv(transport)
        await provider.pv_forecast()
        call = transport.calls[0]
        assert call["url"] == BASE
        assert call["params"]["hourly"] == "global_tilted_irradiance"
        assert call["params"]["tilt"] == "25.0"
        assert call["params"]["azimuth"] == "10.0"
        assert "Authorization" not in call["headers"]

    async def test_watts_are_derived_from_gti_at_capacity_and_derate(self) -> None:
        transport = FakeTransport(script=[_pv_payload()])
        provider = _pv(transport)
        series = await provider.pv_forecast()
        assert series.variable == "pv_power_w"
        # 5 kWp x 650 W/m2 x 0.9 derate = 2925 W; second hour 3600 W.
        assert [value.value for value in series.values] == [2925.0, 3600.0]
        assert series.source == "open-meteo"
        assert all(value.quantile is None for value in series.values)

    async def test_null_gti_hours_are_absent(self) -> None:
        payload = _pv_payload()
        payload["hourly"]["global_tilted_irradiance"] = [None, 800.0]
        transport = FakeTransport(script=[payload])
        series = await _pv(transport).pv_forecast()
        assert len(series.values) == 1
        assert series.values[0].interval_start == datetime(2026, 8, 24, 1, 0, tzinfo=UTC)

    def test_the_derivation_inputs_are_validated(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="latitude"):
            _pv(transport, latitude=91.0)
        with pytest.raises(ValueError, match="longitude"):
            _pv(transport, longitude=181.0)
        with pytest.raises(ValueError, match="capacity_kw"):
            _pv(transport, capacity_kw=0.0)
        with pytest.raises(ValueError, match="derate"):
            _pv(transport, derate=1.5)
        with pytest.raises(ValueError, match="tilt_deg"):
            _pv(transport, tilt_deg=-1.0)
        with pytest.raises(ValueError, match="azimuth_deg"):
            _pv(transport, azimuth_deg=200.0)

    def test_the_adapter_satisfies_the_pv_port(self) -> None:
        assert isinstance(_pv(FakeTransport(script=[])), PvForecastProvider)
