"""The Solcast PV adapter contract (Bearer-keyed, quantiled).

Solcast's documented commercial endpoint is
``GET https://api.solcast.com.au/data/forecast/rooftop_pv_power`` (OpenAPI at
https://api.solcast.com.au/openapi/v1/openapi.json): ``latitude``/``longitude``
required, ``capacity`` in kW, ``period`` an ISO-8601 duration (PT5M..PT60M),
``output_parameters`` selecting ``pv_power_rooftop``/``pv_power_rooftop10``/
``pv_power_rooftop90`` -- all kW, all arriving as ``forecasts[]`` rows with a
UTC ``period_end`` and the averaging ``period``.  Auth prefers the
``Authorization: Bearer`` header over the ``api_key`` query param (query
strings leak into logs).  Free hobbyist keys are quota-capped at 10 requests
per UTC day, which is exactly why the refresh gate lives in the shared base.
These tests pin the request shape, the kW-to-watt normalization, the
half-open interval reconstruction from ``period_end``/``period``, and the
three quantile slices -- against a scripted fake transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

try:
    from energypod.adapters.providers.http import ProviderUnavailableError
    from energypod.adapters.providers.ports import PvForecastProvider
    from energypod.adapters.providers.solcast import (
        SOLCAST_API_URL,
        SOLCAST_ATTRIBUTION,
        SolcastPvForecast,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    SOLCAST_API_URL: Any = None
    SOLCAST_ATTRIBUTION: Any = None
    SolcastPvForecast: Any = None
    PvForecastProvider: Any = None
    ProviderUnavailableError: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_solcast_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The Solcast adapter contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


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


def _payload() -> dict[str, Any]:
    # kW values with the documented 7-fractional-digit UTC period_end stamps.
    return {
        "forecasts": [
            {
                "pv_power_rooftop": 0.711,
                "pv_power_rooftop10": 0.51,
                "pv_power_rooftop90": 0.95,
                "period_end": "2026-08-24T02:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_power_rooftop": 0.696,
                "pv_power_rooftop10": 0.48,
                "pv_power_rooftop90": 0.9,
                "period_end": "2026-08-24T03:00:00.0000000Z",
                "period": "PT30M",
            },
        ]
    }


def _provider(transport: FakeTransport, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "transport": transport,
        "clock": ManualClock(),
        "api_key": "test-key-material",
        "latitude": LAT,
        "longitude": LON,
        "capacity_kw": 5.0,
        "hours": 48,
        "period": "PT30M",
        "refresh_interval_s": 900.0,
        "stale_after_s": 3600.0,
    }
    fields.update(overrides)
    return SolcastPvForecast(**fields)


class TestRequestShape:
    async def test_the_request_is_the_documented_commercial_shape(self) -> None:
        transport = FakeTransport(script=[_payload()])
        provider = _provider(transport)
        await provider.pv_forecast()
        call = transport.calls[0]
        assert call["url"] == SOLCAST_API_URL
        assert call["url"] == "https://api.solcast.com.au/data/forecast/rooftop_pv_power"
        assert call["params"]["latitude"] == str(LAT)
        assert call["params"]["longitude"] == str(LON)
        assert call["params"]["capacity"] == "5.0"
        assert call["params"]["hours"] == "48"
        assert call["params"]["period"] == "PT30M"
        assert call["params"]["output_parameters"] == (
            "pv_power_rooftop,pv_power_rooftop10,pv_power_rooftop90"
        )
        assert call["params"]["format"] == "json"

    async def test_the_key_rides_the_bearer_header_never_the_query(self) -> None:
        transport = FakeTransport(script=[_payload()])
        provider = _provider(transport)
        await provider.pv_forecast()
        call = transport.calls[0]
        assert call["headers"]["Authorization"] == "Bearer test-key-material"
        assert "api_key" not in call["params"]

    def test_the_attribution_line_is_pinned(self) -> None:
        assert "Solcast" in SOLCAST_ATTRIBUTION


class TestParsing:
    async def test_kw_rows_normalize_to_watt_quantile_intervals(self) -> None:
        transport = FakeTransport(script=[_payload()])
        provider = _provider(transport)
        series = await provider.pv_forecast()
        assert series.variable == "pv_power_w"
        assert series.source == "solcast"
        assert series.fetched_at == WALL
        by_quantile = {(value.quantile, value.interval_start): value for value in series.values}
        first_slot = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
        second_slot = datetime(2026, 8, 24, 2, 30, tzinfo=UTC)
        # period_end is the interval END: [02:00, 02:30) and [02:30, 03:00).
        assert by_quantile[(0.5, first_slot)].value == 711.0
        assert by_quantile[(0.1, first_slot)].value == 510.0
        assert by_quantile[(0.9, first_slot)].value == 950.0
        assert by_quantile[(0.5, second_slot)].value == 696.0
        for (quantile, start), value in by_quantile.items():
            del quantile
            assert value.interval_end == start + timedelta(minutes=30)

    async def test_rows_without_quantile_fields_yield_only_the_central_slice(self) -> None:
        payload = {
            "forecasts": [
                {
                    "pv_power_rooftop": 0.711,
                    "period_end": "2026-08-24T02:30:00.0000000Z",
                    "period": "PT60M",
                }
            ]
        }
        transport = FakeTransport(script=[payload])
        series = await _provider(transport).pv_forecast()
        assert len(series.values) == 1
        assert series.values[0].quantile == 0.5
        assert series.values[0].interval_start == datetime(2026, 8, 24, 1, 30, tzinfo=UTC)

    async def test_a_bad_period_duration_is_a_provider_error(self) -> None:
        payload = {
            "forecasts": [
                {
                    "pv_power_rooftop": 0.711,
                    "period_end": "2026-08-24T02:30:00.0000000Z",
                    "period": "garbage",
                }
            ]
        }
        transport = FakeTransport(script=[payload])
        provider = _provider(transport)
        with pytest.raises(ProviderUnavailableError, match="period"):
            await provider.pv_forecast()

    async def test_a_malformed_payload_is_unavailable_not_a_crash(self) -> None:
        transport = FakeTransport(script=[{"unexpected": True}])
        provider = _provider(transport)
        with pytest.raises(ProviderUnavailableError):
            await provider.pv_forecast()


class TestConstructionValidation:
    def test_the_key_and_inputs_are_validated(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="api_key"):
            _provider(transport, api_key="  ")
        with pytest.raises(ValueError, match="latitude"):
            _provider(transport, latitude=91.0)
        with pytest.raises(ValueError, match="capacity_kw"):
            _provider(transport, capacity_kw=0.0)
        with pytest.raises(ValueError, match="hours"):
            _provider(transport, hours=0)
        with pytest.raises(ValueError, match="period"):
            _provider(transport, period="PT7M")

    def test_the_adapter_satisfies_the_pv_port(self) -> None:
        assert isinstance(_provider(FakeTransport(script=[])), PvForecastProvider)
