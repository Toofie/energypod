"""The site-meter reading record and port (DESIGN_SITE_METER.md).

ONE authoritative whole-site power eye composed optionally from the
Fronius Solar API.  The APPLICATION layer sees only this module: an
immutable reading with every watt ``None`` unless quality is ``good``
(fail-closed — never zero-filled), plus the freshness rule the consuming
programs share.  Producing readings is the Fronius ADAPTER's job (see
DESIGN_SITE_METER.md §1/§3); it is injected at composition and this module
stays adapter-blind, symmetric with every other injected port.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

Quality = Literal["good", "unavailable"]


@dataclass(frozen=True, slots=True)
class SiteMeterReading:
    """One whole-site observation, translated to HOUSE conventions.

    ``net_exchange_w`` is IMPORT-positive (what the fleet's netted identity
    wants); ``load_w`` positive-served; ``pv_w`` production.  Every watt is
    ``None`` exactly when quality is not ``good`` — an absent fact is never
    a zero.
    """

    net_exchange_w: float | None
    load_w: float | None
    pv_w: float | None
    served_at_mono: float | None
    wall_now: datetime | None
    quality: Quality = "good"

    def fresh(self, now_mono: float, stale_after_s: float) -> bool:
        """The shared freshness gate: good, timed, inside the staleness bound."""
        if self.quality != "good" or self.served_at_mono is None:
            return False
        return (now_mono - self.served_at_mono) <= stale_after_s


def unavailable_reading(*, served_at_mono: float | None = None,
                        wall_now: datetime | None = None) -> SiteMeterReading:
    """The honest absence: all watts None, never zeros."""
    return SiteMeterReading(
        net_exchange_w=None,
        load_w=None,
        pv_w=None,
        served_at_mono=served_at_mono,
        wall_now=wall_now,
        quality="unavailable",
    )


class SiteMeterPort(Protocol):
    """The one async word consumers get; the adapter owns cadence."""

    async def latest(self) -> SiteMeterReading: ...


class SiteMeterControl:
    """The composed façade-side twin: port for the adviser, projection for
    the snapshot (the history/health-control pattern).

    ``latest()`` delegates to the provider AND records what came back so
    ``state_payload()`` can answer honestly without polling again.
    """

    def __init__(
        self,
        *,
        provider: Any,
        stale_after_s: float,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        import time as _time

        self._provider = provider
        self._stale_after_s = float(stale_after_s)
        self._monotonic = monotonic or _time.monotonic
        self._last_reading: SiteMeterReading | None = None
        self._last_error: str | None = None

    @property
    def stale_after_s(self) -> float:
        return self._stale_after_s

    async def latest(self) -> SiteMeterReading:
        reading = await self._provider.read()
        if not isinstance(reading, SiteMeterReading):
            self._last_reading = None
            self._last_error = "bad record shape"
            return unavailable_reading()
        self._last_reading = reading
        self._last_error = None if reading.quality == "good" else "unavailable"
        return reading

    def state_payload(self) -> dict[str, Any]:
        reading = self._last_reading
        fresh = bool(
            reading is not None
            and reading.fresh(self._monotonic(), self._stale_after_s)
        )
        last_at = None
        if reading is not None and reading.wall_now is not None:
            last_at = reading.wall_now.isoformat()
        return {
            "available": True,
            "fresh": fresh,
            "stale_after_s": self._stale_after_s,
            "last_reading_quality": None if reading is None else reading.quality,
            "last_reading_at": last_at,
            "last_error": self._last_error,
        }


__all__ = ["SiteMeterPort", "SiteMeterReading", "unavailable_reading"]
