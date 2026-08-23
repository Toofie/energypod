"""The async HTTP provider base contract: timeout, caching, staleness stamps.

ARCHITECTURE section 17 pins the doctrine these tests name: provider adapters
own authentication, rate limits, retries, caching, source timestamps, and
normalization, and *cached data retains original age and never becomes fresh
merely because it was reread*.  The base class below is that doctrine as one
implementation every HTTP adapter composes; no test opens a socket (the
transport is always a fake).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

try:
    from energypod.adapters.providers.http import (
        HttpForecastProvider,
        HttpxForecastTransport,
        ProviderStaleness,
        ProviderTransportError,
        ProviderUnavailableError,
    )
    from energypod.adapters.providers.model import ForecastSeries, ForecastValue
except ImportError as exc:  # pragma: no cover - initial red phase only
    HttpForecastProvider: Any = None
    HttpxForecastTransport: Any = None
    ProviderStaleness: Any = None
    ProviderTransportError: Any = None
    ProviderUnavailableError: Any = None
    ForecastSeries: Any = None
    ForecastValue: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_http_base_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The provider HTTP base contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


REFRESH_S = 100.0
STALE_S = 300.0


class ManualClock:
    """The composition clock shape: monotonic age plus a movable wall clock."""

    def __init__(self, *, start: float = 5000.0, wall: datetime | None = None) -> None:
        self.now = start
        self.wall = wall or datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


@dataclass
class FakeTransport:
    """The transport double: one ordered script, counted wire legs, no socket.

    Each scripted entry is either a JSON payload (a successful leg) or an
    exception instance (a failed leg), consumed strictly in order.
    """

    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    delay_s: float = 0.0

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers or {})})
        if not self.script:
            raise AssertionError("the fake transport ran out of scripted legs")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


class FakeProvider(HttpForecastProvider):
    """The concrete-double: one interval per fetch, wall-stamped by the clock."""

    source = "fake-source"

    async def _request(self) -> Any:
        payload = await self._get_json(
            "https://example.test/forecast",
            params={"hours": "2"},
            headers={"Accept": "application/json"},
        )
        watts = float(payload["watts"])  # type: ignore[index]
        moment = self._clock.wall_now()
        return ForecastSeries(
            variable="pv_power_w",
            source=self.source,
            fetched_at=moment,
            values=(
                ForecastValue(
                    variable="pv_power_w",
                    interval_start=moment,
                    interval_end=moment + timedelta(minutes=30),
                    value=watts,
                    source=self.source,
                    fetched_at=moment,
                ),
            ),
        )


def _provider(
    transport: FakeTransport, clock: ManualClock
) -> tuple[FakeProvider, FakeTransport, ManualClock]:
    provider = FakeProvider(
        transport=transport,
        clock=clock,
        refresh_interval_s=REFRESH_S,
        stale_after_s=STALE_S,
    )
    return provider, transport, clock


class TestFetchAndCache:
    async def test_the_first_fetch_takes_exactly_one_wire_leg(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}])
        provider, wire, clock = _provider(transport, ManualClock())
        series = await provider.fetch()
        assert series.values[0].value == 1200.0
        assert series.fetched_at == clock.wall_now()
        assert len(wire.calls) == 1
        assert wire.calls[0]["url"] == "https://example.test/forecast"
        assert provider.staleness().fetch_count == 1

    async def test_a_fetch_inside_the_refresh_interval_never_rereads_the_wire(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}, {"watts": 999.0}])
        provider, wire, clock = _provider(transport, ManualClock())
        first = await provider.fetch()
        clock.advance(REFRESH_S - 1.0)
        again = await provider.fetch()
        assert len(wire.calls) == 1
        assert again is first

    async def test_a_fetch_after_the_refresh_interval_takes_a_fresh_leg(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}, {"watts": 800.0}])
        provider, wire, clock = _provider(transport, ManualClock())
        await provider.fetch()
        clock.advance(REFRESH_S + 1.0)
        fresh = await provider.fetch()
        assert len(wire.calls) == 2
        assert fresh.values[0].value == 800.0
        assert fresh.fetched_at == clock.wall_now()

    async def test_concurrent_fetches_share_one_wire_leg(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}], delay_s=0.02)
        provider, wire, clock = _provider(transport, ManualClock())
        first, second, third = await asyncio.gather(
            provider.fetch(), provider.fetch(), provider.fetch()
        )
        assert len(wire.calls) == 1
        assert first is second is third


class TestFailureBehavior:
    async def test_a_first_fetch_failure_with_no_cache_is_unavailable_not_a_crash(self) -> None:
        transport = FakeTransport(script=[ProviderTransportError("connection timed out")])
        provider, wire, clock = _provider(transport, ManualClock())
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            await provider.fetch()
        assert len(wire.calls) == 1
        staleness = provider.staleness()
        assert staleness.last_error is not None and "timed out" in staleness.last_error
        assert staleness.fetched_at is None
        assert staleness.error_count == 1

    async def test_a_failed_refresh_keeps_the_cache_with_its_original_fetched_at(self) -> None:
        transport = FakeTransport(
            script=[{"watts": 1200.0}, ProviderTransportError("503 upstream")]
        )
        provider, wire, clock = _provider(transport, ManualClock())
        first = await provider.fetch()
        fetched_at = first.fetched_at
        clock.advance(REFRESH_S + 1.0)
        served = await provider.fetch()
        assert len(wire.calls) == 2
        assert served.values[0].value == 1200.0
        # THE caching honesty rule: a failed reread never restamps the cache.
        assert served.fetched_at == fetched_at
        assert (
            provider.staleness().last_error is not None and "503" in provider.staleness().last_error
        )

    async def test_a_malformed_body_is_a_provider_error_with_cached_fallback(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}, {"unexpected": True}])
        provider, wire, clock = _provider(transport, ManualClock())
        await provider.fetch()
        clock.advance(REFRESH_S + 1.0)
        served = await provider.fetch()
        assert served.values[0].value == 1200.0
        assert provider.staleness().error_count == 1


class TestStalenessReport:
    async def test_the_staleness_stamp_tracks_age_and_the_stale_threshold(self) -> None:
        transport = FakeTransport(script=[{"watts": 1200.0}])
        provider, wire, clock = _provider(transport, ManualClock())
        await provider.fetch()
        fresh = provider.staleness()
        assert isinstance(fresh, ProviderStaleness)
        assert fresh.source == "fake-source"
        assert fresh.fetched_at is not None
        assert fresh.age_s == 0.0
        assert fresh.stale is False
        assert fresh.last_error is None
        clock.advance(STALE_S + 1.0)
        aged = provider.staleness()
        assert aged.age_s is not None and aged.age_s > STALE_S
        assert aged.stale is True

    async def test_a_healthy_refresh_clears_the_last_error(self) -> None:
        transport = FakeTransport(
            script=[
                {"watts": 1200.0},
                ProviderTransportError("transient"),
                {"watts": 900.0},
            ]
        )
        provider, wire, clock = _provider(transport, ManualClock())
        await provider.fetch()
        clock.advance(REFRESH_S + 1.0)
        await provider.fetch()
        clock.advance(REFRESH_S + 1.0)
        await provider.fetch()
        assert provider.staleness().last_error is None
        assert provider.staleness().error_count == 1
        assert provider.staleness().fetch_count == 2


class TestConstructionValidation:
    def test_refresh_and_staleness_bounds_are_validated(self) -> None:
        transport = FakeTransport(script=[])
        clock = ManualClock()
        with pytest.raises(ValueError, match="refresh_interval_s"):
            FakeProvider(
                transport=transport, clock=clock, refresh_interval_s=0.0, stale_after_s=10.0
            )
        with pytest.raises(ValueError, match="stale_after_s"):
            FakeProvider(
                transport=transport,
                clock=clock,
                refresh_interval_s=100.0,
                stale_after_s=99.0,
            )

    async def test_transport_exceptions_are_normalized_to_provider_transport_errors(self) -> None:
        class ExplodingTransport(FakeTransport):
            async def get_json(
                self, url: str, *, params: dict[str, str], headers: dict[str, str] | None = None
            ) -> Any:
                raise OSError("socket exploded")

        provider = FakeProvider(
            transport=ExplodingTransport(),
            clock=ManualClock(),
            refresh_interval_s=REFRESH_S,
            stale_after_s=STALE_S,
        )
        with pytest.raises(ProviderUnavailableError, match="socket exploded"):
            await provider.fetch()


class TestHttpxTransport:
    async def test_a_timeout_is_normalized(self) -> None:
        class TimeoutClient:
            async def get(self, url: str, *, params: Any, headers: Any) -> None:
                raise httpx.TimeoutException("timed out")

        transport = HttpxForecastTransport(timeout_s=5.0, client=TimeoutClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderTransportError, match="timed out"):
            await transport.get_json("https://example.test/x", params={})

    async def test_a_non_200_status_is_an_error(self) -> None:
        @dataclass
        class Response:
            status_code: int

            def json(self) -> Any:
                return {}

        class StatusClient:
            async def get(self, url: str, *, params: Any, headers: Any) -> Any:
                return Response(status_code=429)

        transport = HttpxForecastTransport(timeout_s=5.0, client=StatusClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderTransportError, match="429"):
            await transport.get_json("https://example.test/x", params={})

    async def test_a_non_json_body_is_an_error(self) -> None:
        @dataclass
        class Response:
            status_code: int

            def json(self) -> Any:
                raise ValueError("not JSON")

        class GarbageClient:
            async def get(self, url: str, *, params: Any, headers: Any) -> Any:
                return Response(status_code=200)

        transport = HttpxForecastTransport(timeout_s=5.0, client=GarbageClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderTransportError, match="body"):
            await transport.get_json("https://example.test/x", params={})

    async def test_the_url_params_and_headers_reach_the_client(self) -> None:
        @dataclass
        class Response:
            status_code: int

            def json(self) -> Any:
                return {"ok": True}

        seen: dict[str, Any] = {}

        class RecordingClient:
            async def get(self, url: str, *, params: Any, headers: Any) -> Any:
                seen.update(url=url, params=dict(params), headers=dict(headers))
                return Response(status_code=200)

        transport = HttpxForecastTransport(timeout_s=5.0, client=RecordingClient())  # type: ignore[arg-type]
        payload = await transport.get_json(
            "https://example.test/x", params={"a": "1"}, headers={"Authorization": "Bearer k"}
        )
        assert payload == {"ok": True}
        assert seen["url"] == "https://example.test/x"
        assert seen["params"] == {"a": "1"}
        assert seen["headers"]["Authorization"] == "Bearer k"
