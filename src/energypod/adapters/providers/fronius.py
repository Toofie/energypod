"""The Fronius site-meter adapter (DESIGN_SITE_METER.md §1/§3).

ONE bounded GET of the Solar API's PowerFlow document per poll, translated
AT THE ADAPTER into house conventions (the Fronius signs are inverted from
ours: P_Grid negative = exporting, P_Load negative = load being served).
Fail-closed throughout: transport errors, non-zero Head status, missing or
non-finite watts, or a broken JSON body produce the ``unavailable`` reading
with every watt ``None`` — never zero-filled, the forecast-provider rule.
A null ``P_Akku`` is legitimate (no battery attached on this site) and is
not an error.

Layering: this module imports the APPLICATION record
(``energypod.application.site_meter``) — adapters serve ports, never the
reverse — and holds no state beyond the optional monotonic source used to
stamp freshness.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

from energypod.application.site_meter import (
    SiteMeterReading,
    unavailable_reading,
)

_POWERFLOW_PATH: Final[str] = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"


def _finite(value: Any) -> float | None:
    """A real finite number — NaN/None/strings are absences, never zeros."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _status_ok(head: Any) -> bool:
    if not isinstance(head, dict):
        return False
    status = head.get("Status")
    if not isinstance(status, dict):
        return False
    code = status.get("Code")
    return isinstance(code, int) and code == 0


class FroniusSiteMeter:
    """The Solar API v1 PowerFlow reader behind the application port."""

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 80,
        request_timeout_s: float = 2.0,
        fetch: Callable[[], Awaitable[Any]] | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_now: Callable[[], datetime] | None = None,
    ) -> None:
        # ``host`` is required only for the composed HTTP path; an injected
        # ``fetch`` double (tests, offline probes) never needs it.
        self._host = host
        self._port = int(port)
        self._timeout_s = float(request_timeout_s)
        # The injectable fetch seam is the test double point; production
        # composes an httpx GET of {host}:{port}{_POWERFLOW_PATH}.
        self._fetch = fetch
        self._monotonic = monotonic  # default wired in latest()
        self._wall_now = wall_now

    async def latest(self) -> SiteMeterReading:
        """One poll, translated; failures are words, never exceptions past here."""
        now_wall = (self._wall_now or (lambda: datetime.now(tz=UTC)))()
        try:
            payload = await (
                self._fetch() if self._fetch is not None else self._http_get()
            )
            return self._translate(payload)
        except Exception:
            return unavailable_reading(wall_now=now_wall)

    async def read(self) -> SiteMeterReading:
        """Alias kept for the probe/test vocabulary; same semantics."""
        return await self.latest()

    async def _http_get(self) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.get(
                f"http://{self._host}:{self._port}{_POWERFLOW_PATH}"
            )
        if response.status_code != 200:
            raise ValueError(f"site meter HTTP {response.status_code}")
        return response.json()

    def _translate(self, payload: Any) -> SiteMeterReading:
        mono = (self._monotonic or (lambda: 0.0))()
        now_wall = (self._wall_now or (lambda: datetime.now(tz=UTC)))()
        try:
            body = payload["Body"]["Data"]
            head = payload["Head"]
        except (KeyError, TypeError):
            return unavailable_reading(served_at_mono=mono, wall_now=now_wall)
        if not _status_ok(head):
            return unavailable_reading(served_at_mono=mono, wall_now=now_wall)
        site = body.get("Site") if isinstance(body, dict) else None
        if not isinstance(site, dict):
            return unavailable_reading(served_at_mono=mono, wall_now=now_wall)
        grid = _finite(site.get("P_Grid"))
        load = _finite(site.get("P_Load"))
        pv = _finite(site.get("P_PV"))
        if grid is None or load is None or pv is None:
            return unavailable_reading(served_at_mono=mono, wall_now=now_wall)
        return SiteMeterReading(
            # Fronius shares OUR exchange polarity: positive = importing,
            # negative = feeding the grid (verified live 2026-08-27: an
            # exporting site read P_Grid -1582).  Loads invert.
            net_exchange_w=grid,
            load_w=-load,
            pv_w=pv,
            served_at_mono=mono,
            wall_now=now_wall,
            quality="good",
        )


__all__ = ["FroniusSiteMeter"]
