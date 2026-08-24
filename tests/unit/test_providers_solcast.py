"""The Solcast PV adapter contract (Bearer-keyed, quantiled, site-addressed).

The wire facts here are LIVE-observed (2026-08-24, the operator's key): the
hobbyist tier refuses the ``world_pv_power`` endpoints with HTTP 403
("Hobbyist accounts are not allowed to access this endpoint") and serves
the rooftop-sites flow --
``GET https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts?hours=N``
-- with ``Authorization: Bearer`` and ``Accept: application/json``.  Rows
arrive as ``forecasts[]`` carrying ``pv_estimate``/``pv_estimate10``/
``pv_estimate90`` in kW plus a UTC ``period_end`` (7 fractional digits) and
the averaging ``period``; the registered site record holds the geography
and plane, so the request carries NEITHER latitude, longitude, capacity,
nor output parameters.  Free hobbyist keys are quota-capped at 10 requests
per UTC day, which is exactly why the refresh gate lives in the shared
base.  These tests pin the request shape, the kW-to-watt normalization, the
half-open interval reconstruction from ``period_end``/``period``, the three
quantile slices, and the error classes (the 403 tier refusal among them)
surfacing honestly as provider-unavailable -- against a scripted fake
transport; no test opens a socket or spends quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

try:
    from energypod.adapters.providers.http import (
        ProviderTransportError,
        ProviderUnavailableError,
    )
    from energypod.adapters.providers.ports import PvForecastProvider
    from energypod.adapters.providers.solcast import (
        SOLCAST_ATTRIBUTION,
        SOLCAST_ROOFTOP_SITES_URL,
        SolcastPvForecast,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    SOLCAST_ROOFTOP_SITES_URL: Any = None
    SOLCAST_ATTRIBUTION: Any = None
    SolcastPvForecast: Any = None
    PvForecastProvider: Any = None
    ProviderTransportError: Any = None
    ProviderUnavailableError: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_solcast_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The Solcast adapter contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


WALL = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
RESOURCE_ID = "b6bf-9d1d-0680-4078"
FORECASTS_URL = f"{SOLCAST_ROOFTOP_SITES_URL}/{RESOURCE_ID}/forecasts"


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
    # kW values with the live-observed 7-fractional-digit UTC period_end stamps.
    return {
        "forecasts": [
            {
                "pv_estimate": 0.711,
                "pv_estimate10": 0.51,
                "pv_estimate90": 0.95,
                "period_end": "2026-08-24T02:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.696,
                "pv_estimate10": 0.48,
                "pv_estimate90": 0.9,
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
        "resource_id": RESOURCE_ID,
        "hours": 48,
        "period": "PT30M",
        "refresh_interval_s": 900.0,
        "stale_after_s": 3600.0,
    }
    fields.update(overrides)
    return SolcastPvForecast(**fields)


class TestRequestShape:
    async def test_the_request_is_the_live_observed_rooftop_sites_shape(self) -> None:
        transport = FakeTransport(script=[_payload()])
        provider = _provider(transport)
        await provider.pv_forecast()
        call = transport.calls[0]
        assert call["url"] == FORECASTS_URL
        assert call["url"] == (
            "https://api.solcast.com.au/rooftop_sites/b6bf-9d1d-0680-4078/forecasts"
        )
        assert call["params"] == {"hours": "48", "period": "PT30M"}

    async def test_the_site_facts_stay_at_solcast_never_on_the_wire(self) -> None:
        """The registered site holds lat/long/capacity — the resource_id
        addresses them, so none of them rides the request (a client-side
        copy could only drift from what Solcast actually models)."""
        transport = FakeTransport(script=[_payload()])
        await _provider(transport).pv_forecast()
        params = transport.calls[0]["params"]
        for absent in ("latitude", "longitude", "capacity", "output_parameters", "format"):
            assert absent not in params, absent

    async def test_the_key_rides_the_bearer_header_never_the_query(self) -> None:
        transport = FakeTransport(script=[_payload()])
        provider = _provider(transport)
        await provider.pv_forecast()
        call = transport.calls[0]
        assert call["headers"]["Authorization"] == "Bearer test-key-material"
        assert call["headers"]["Accept"] == "application/json"
        assert "api_key" not in call["params"]
        assert "api_key" not in call["url"]

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

    async def test_the_live_observed_sundown_shape_normalizes(self) -> None:
        """The verbatim live observation (2026-08-24, after sundown): all
        three estimates zero at a 30-minute period_end — still three honest
        quantile rows, never a gap or a fabricated confidence."""
        payload = {
            "forecasts": [
                {
                    "pv_estimate": 0,
                    "pv_estimate10": 0,
                    "pv_estimate90": 0,
                    "period_end": "2026-08-24T09:00:00.0000000Z",
                    "period": "PT30M",
                }
            ]
        }
        transport = FakeTransport(script=[payload])
        series = await _provider(transport).pv_forecast()
        assert [(value.quantile, value.value) for value in series.values] == [
            (0.1, 0.0),
            (0.5, 0.0),
            (0.9, 0.0),
        ]
        assert series.values[0].interval_start == datetime(2026, 8, 24, 8, 30, tzinfo=UTC)
        assert series.values[0].interval_end == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

    async def test_rows_without_quantile_fields_yield_only_the_central_slice(self) -> None:
        payload = {
            "forecasts": [
                {
                    "pv_estimate": 0.711,
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
                    "pv_estimate": 0.711,
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


class TestErrorShapes:
    @pytest.mark.parametrize("status", [403, 402, 429])
    async def test_http_refusals_surface_honestly_as_unavailable(self, status: int) -> None:
        """The 403 tier refusal ("Hobbyist accounts are not allowed to
        access this endpoint", discovered live 2026-08-24), the 402
        plan-limit, and the 429 quota signals all arrive through the
        transport as typed errors; with nothing cached the read is
        ProviderUnavailableError carrying the status -- never a crash and
        never a silent zero."""
        transport = FakeTransport(
            script=[
                ProviderTransportError(
                    f"provider answered HTTP {status} for {FORECASTS_URL}"
                )
            ]
        )
        provider = _provider(transport)
        with pytest.raises(ProviderUnavailableError, match=f"HTTP {status}"):
            await provider.pv_forecast()
        report = provider.staleness()
        assert report.error_count == 1
        assert str(status) in str(report.last_error)

    async def test_a_refusal_after_a_success_serves_the_cache_its_age_kept(self) -> None:
        """The caching honesty rule under the 403 class: a failed refresh
        never freshens -- the ORIGINAL series is served with its ORIGINAL
        fetched_at, and the refusal lands in the staleness report."""
        transport = FakeTransport(
            script=[
                _payload(),
                ProviderTransportError(f"provider answered HTTP 403 for {FORECASTS_URL}"),
            ]
        )
        clock = ManualClock()
        provider = _provider(transport, clock=clock, refresh_interval_s=100.0)
        first = await provider.pv_forecast()
        clock.now += 200.0  # past the refresh gate: the next read retries
        second = await provider.pv_forecast()
        assert second.values == first.values
        assert second.fetched_at == first.fetched_at == WALL
        report = provider.staleness()
        assert report.fetch_count == 1
        assert report.error_count == 1
        assert "403" in str(report.last_error)


class TestConstructionValidation:
    def test_the_key_and_inputs_are_validated(self) -> None:
        transport = FakeTransport(script=[])
        with pytest.raises(ValueError, match="api_key"):
            _provider(transport, api_key="  ")
        with pytest.raises(ValueError, match="resource_id"):
            _provider(transport, resource_id="  ")
        with pytest.raises(ValueError, match="resource_id"):
            _provider(transport, resource_id="b6bf/9d1d")
        with pytest.raises(ValueError, match="hours"):
            _provider(transport, hours=0)
        with pytest.raises(ValueError, match="hours"):
            _provider(transport, hours=337)
        with pytest.raises(ValueError, match="period"):
            _provider(transport, period="PT7M")

    def test_the_adapter_satisfies_the_pv_port(self) -> None:
        assert isinstance(_provider(FakeTransport(script=[])), PvForecastProvider)
