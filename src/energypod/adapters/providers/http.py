"""The async HTTP provider base: timeout, caching, and staleness stamps.

ARCHITECTURE section 17 assigns the provider adapters the wire concerns --
authentication, rate limits, retries, caching, source timestamps -- and one
honesty rule overrides them all: *cached data retains original age and never
becomes fresh merely because it was reread*.  This module is that rule as
code, composed by every HTTP adapter:

- One wire leg per refresh interval.  A read inside the interval is served
  from the cache; concurrent readers share a single in-flight leg (an
  :class:`asyncio.Lock` makes the refresh single-flight), so a burst of
  adviser reads can never burn a rate-limited quota.
- A failed refresh is never a crash and never a freshening.  With no cache
  the read raises :class:`ProviderUnavailableError`; with a cache the
  ORIGINAL series is served, its ``fetched_at`` untouched, and the failure is
  stamped onto :meth:`HttpForecastProvider.staleness` for the consumer to
  judge.
- The staleness report carries ``fetched_at``, honest ``age_s`` from the
  monotonic clock, the ``stale`` threshold verdict, the last error, and the
  fetch/error counts.

The transport is a port: :class:`ForecastHttpTransport` is the narrow
``GET -> JSON`` seam, :class:`HttpxForecastTransport` is the production
httpx-backed adapter (timeout configured at construction, one client per
provider), and every test composes a fake -- no socket is ever opened.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

import httpx

from energypod.adapters.providers.model import ForecastSeries

__all__ = [
    "ForecastHttpTransport",
    "HttpForecastProvider",
    "HttpxForecastTransport",
    "ProviderClock",
    "ProviderStaleness",
    "ProviderTransportError",
    "ProviderUnavailableError",
]


class ProviderTransportError(RuntimeError):
    """One wire leg failed: timeout, non-200 status, or an unparseable body."""


class ProviderUnavailableError(RuntimeError):
    """No usable data exists: the fetch failed and nothing is cached.

    The advisory consumer's fallback signal (ARCHITECTURE section 19: an
    optional provider failure invalidates its data and causes optimizer
    fallback; it does not crash direct observation).
    """


class ForecastHttpTransport(Protocol):
    """The narrow wire seam every HTTP provider composes."""

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> object:
        """One GET parsed as JSON; raises :class:`ProviderTransportError`."""
        ...


class ProviderClock(Protocol):
    """The clock port providers read (the composition ``Clock`` shape)."""

    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ProviderStaleness:
    """The honest age report for one provider's latest data."""

    source: str
    fetched_at: datetime | None
    age_s: float | None
    stale: bool
    last_error: str | None
    fetch_count: int
    error_count: int


class HttpxForecastTransport:
    """The production transport: httpx GET with a configured timeout.

    The client is injectable so the composition root owns its lifecycle and a
    test can substitute a stub; when none is supplied a private client is
    created lazily and closed with the transport.  Timeouts, transport
    failures, non-200 statuses, and non-JSON bodies all normalize to
    :class:`ProviderTransportError` -- the provider base never sees an
    httpx-specific exception.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
        base_headers: Mapping[str, str] | None = None,
    ) -> None:
        if not 0.0 < float(timeout_s) < 600.0:
            raise ValueError("timeout_s must lie in (0, 600)")
        self._timeout_s = float(timeout_s)
        self._client = client
        self._owns_client = client is None
        self._base_headers: dict[str, str] = {
            "Accept": "application/json",
            **dict(base_headers or {}),
        }

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> object:
        client = self._client if self._client is not None else self._make_client()
        merged = {**self._base_headers, **dict(headers or {})}
        try:
            response = await client.get(url, params=dict(params), headers=merged)
        except httpx.TimeoutException as exc:
            raise ProviderTransportError(f"request timed out after {self._timeout_s:g}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderTransportError(f"transport failure: {exc}") from exc
        if response.status_code != 200:
            raise ProviderTransportError(f"provider answered HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise ProviderTransportError(f"provider body was not JSON: {exc}") from exc

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _make_client(self) -> httpx.AsyncClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s),
            follow_redirects=True,
        )
        return self._client


class HttpForecastProvider:
    """The caching/staleness base every HTTP forecast adapter composes.

    Subclasses declare their ``source`` name and implement :meth:`_request`
    (build the URL/params, call :meth:`_get_json`, parse and normalize).  The
    base owns everything else: the refresh gate, the single-flight lock, the
    fail-soft cache, and the staleness stamps.  ``refresh_interval_s`` is both
    the cache lifetime and the minimum spacing between wire legs -- the
    rate-limit budget -- and ``stale_after_s`` (>= the refresh interval) is
    when the report calls the data stale.
    """

    source: ClassVar[str] = ""

    def __init__(
        self,
        *,
        transport: ForecastHttpTransport,
        clock: ProviderClock,
        refresh_interval_s: float,
        stale_after_s: float,
    ) -> None:
        if not isinstance(refresh_interval_s, int | float) or refresh_interval_s <= 0:
            raise ValueError("refresh_interval_s must be positive (the wire-leg budget)")
        if not isinstance(stale_after_s, int | float) or stale_after_s < refresh_interval_s:
            raise ValueError(
                "stale_after_s must be at least refresh_interval_s (data turns stale no "
                "earlier than the first allowed reread)"
            )
        self._transport = transport
        self._clock = clock
        self._refresh_interval_s = float(refresh_interval_s)
        self._stale_after_s = float(stale_after_s)
        self._cached: ForecastSeries | None = None
        self._fetched_mono: float | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._error_count = 0
        self._flight: asyncio.Lock = asyncio.Lock()

    async def fetch(self) -> ForecastSeries:
        """The provider's current series, refreshing only when the gate opens.

        Raises :class:`ProviderUnavailableError` only when there is no cache
        and the wire failed; a cached series survives failed refreshes with
        its original ``fetched_at``.
        """
        if self._cached is not None and self._cache_is_fresh():
            assert self._cached is not None
            return self._cached
        async with self._flight:
            if self._cached is not None and self._cache_is_fresh():
                assert self._cached is not None
                return self._cached
            try:
                series = await self._request()
            except ProviderTransportError as exc:
                self._error_count += 1
                self._last_error = str(exc)
                if self._cached is None:
                    raise ProviderUnavailableError(str(exc)) from exc
                return self._cached
            except (KeyError, TypeError, ValueError) as exc:
                # A malformed provider body is a provider failure, never a
                # crash into an adviser.
                self._error_count += 1
                self._last_error = f"malformed provider payload: {exc}"
                if self._cached is None:
                    raise ProviderUnavailableError(self._last_error) from exc
                return self._cached
            self._fetch_count += 1
            self._cached = series
            self._fetched_mono = self._clock.monotonic()
            self._last_error = None
            return series

    def staleness(self) -> ProviderStaleness:
        """The honest age report for the latest data."""
        age = (
            None
            if self._fetched_mono is None
            else max(0.0, self._clock.monotonic() - self._fetched_mono)
        )
        return ProviderStaleness(
            source=self.source,
            fetched_at=None if self._cached is None else self._cached.fetched_at,
            age_s=age,
            stale=age is not None and age > self._stale_after_s,
            last_error=self._last_error,
            fetch_count=self._fetch_count,
            error_count=self._error_count,
        )

    async def _request(self) -> ForecastSeries:
        """One wire leg, parsed and normalized (the subclass's whole job)."""
        raise NotImplementedError("every HTTP provider implements _request")

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """The subclasses' only wire entry point, normalized to typed errors."""
        try:
            return await self._transport.get_json(url, params=params, headers=headers)
        except ProviderTransportError:
            raise
        except Exception as exc:  # a fake or unexpected transport failure
            raise ProviderTransportError(str(exc)) from exc

    def _cache_is_fresh(self) -> bool:
        assert self._fetched_mono is not None
        return (self._clock.monotonic() - self._fetched_mono) < self._refresh_interval_s
