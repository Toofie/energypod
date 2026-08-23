"""The normalized forecast value model (ARCHITECTURE section 17).

Every provider family normalizes onto the shapes in this module before any
adviser or projection ever sees the data.  The normalization contract:

- Intervals are half-open ``[interval_start, interval_end)`` and every stamp
  is a timezone-aware :class:`~datetime.datetime` normalized to UTC, so
  series from different sources align by plain comparison.
- ``quantile`` is the probabilistic claim the source actually makes:
  ``0.5``/``0.1``/``0.9`` for Solcast's ``pv_estimate`` family, ``None`` for
  a deterministic point forecast (Open-Meteo, the historian baseline) --
  never a fabricated confidence.
- ``variable`` carries its unit in its name (``pv_power_w``); no caller ever
  has to ask what a number means.
- ``fetched_at`` is when WE fetched the data and ``issued_at`` (when known)
  when the SOURCE issued the forecast.  A cached value keeps its ORIGINAL
  ``fetched_at`` forever: rereading a cache never makes data fresh
  (ARCHITECTURE section 17, the caching honesty rule).

Nothing in this module reaches a control path.  Providers are advisory-only
inputs to advisers and projections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "FORECAST_QUALITY_WORDS",
    "TARIFF_MARKET_WORDS",
    "ForecastSeries",
    "ForecastValue",
    "ForecastVariable",
    "TariffInterval",
]


class ForecastVariable(StrEnum):
    """The normalized variable vocabulary, units in the names.

    ``irradiance_w_m2`` is the plane-of-array-relevant global figure the
    weather family serves (Open-Meteo ``shortwave_radiation`` today); the
    other weather variables ride the same shape so a weather consumer reads
    one series API.
    """

    PV_POWER_W = "pv_power_w"
    LOAD_POWER_W = "load_power_w"
    IRRADIANCE_W_M2 = "irradiance_w_m2"
    CLOUD_COVER_PCT = "cloud_cover_pct"
    TEMPERATURE_C = "temperature_c"


VARIABLE_WORDS: Final[frozenset[str]] = frozenset(item.value for item in ForecastVariable)

# The tariff quality vocabulary: an operator-declared static schedule is
# honest about being one, and a market feed is honest about being live.
# Nothing else exists yet; the vocabulary grows only with a real source.
FORECAST_QUALITY_WORDS: Final[frozenset[str]] = frozenset({"static", "market"})
TARIFF_MARKET_WORDS: Final[frozenset[str]] = frozenset({"static-config", "wholesale"})


def _utc(moment: datetime, label: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return moment if moment.tzinfo is UTC else moment.astimezone(UTC)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _word(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty normalized word")
    return value


@dataclass(frozen=True, slots=True)
class ForecastValue:
    """One normalized forecast figure for one interval and quantile."""

    variable: str
    interval_start: datetime
    interval_end: datetime
    value: float
    source: str
    fetched_at: datetime
    quantile: float | None = None
    issued_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.variable not in VARIABLE_WORDS:
            raise ValueError(
                f"variable must be one of {sorted(VARIABLE_WORDS)}, got {self.variable!r}"
            )
        start = _utc(self.interval_start, "interval_start")
        end = _utc(self.interval_end, "interval_end")
        if end <= start:
            raise ValueError(
                "interval_start must precede interval_end (half-open [start, end) slots); "
                f"a zero-length or reversed interval is not a forecast slot "
                f"(got {start.isoformat()}..{end.isoformat()})"
            )
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        object.__setattr__(self, "value", _finite(self.value, "value"))
        object.__setattr__(self, "source", _word(self.source, "source"))
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))
        if self.issued_at is not None:
            object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        if self.quantile is not None:
            quantile = _finite(self.quantile, "quantile")
            if not 0.0 <= quantile <= 1.0:
                raise ValueError(f"quantile must lie in [0.0, 1.0], got {quantile}")
            object.__setattr__(self, "quantile", quantile)


@dataclass(frozen=True, slots=True)
class ForecastSeries:
    """One fetch of one variable from one source, immutable and sorted.

    Every member value must agree with the series header on ``variable``,
    ``source``, and ``fetched_at`` -- the header is what a consumer reads
    first, so a disagreement would be two series pretending to be one.  The
    values arrive sorted by ``(interval_start, interval_end, quantile)`` with
    no duplicate ``(interval, quantile)`` row, which is exactly the order the
    HTTP adapters emit and the cross-check scorer consumes.
    """

    variable: str
    source: str
    fetched_at: datetime
    values: tuple[ForecastValue, ...]
    issued_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.variable not in VARIABLE_WORDS:
            raise ValueError(f"variable must be one of {sorted(VARIABLE_WORDS)}")
        object.__setattr__(self, "source", _word(self.source, "source"))
        fetched = _utc(self.fetched_at, "fetched_at")
        object.__setattr__(self, "fetched_at", fetched)
        if self.issued_at is not None:
            object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        seen: set[tuple[datetime, datetime, float | None]] = set()
        previous: tuple[float, float, float] | None = None
        for value in self.values:
            if not isinstance(value, ForecastValue):
                raise TypeError("values must all be ForecastValue rows")
            if value.variable != self.variable:
                raise ValueError(
                    f"every value must carry the series variable {self.variable!r} "
                    f"(got {value.variable!r})"
                )
            if value.source != self.source:
                raise ValueError(
                    f"every value must carry the series source {self.source!r} "
                    f"(got {value.source!r})"
                )
            if value.fetched_at != fetched:
                raise ValueError(
                    "every value must carry the series fetched_at "
                    f"{fetched.isoformat()} (got {value.fetched_at.isoformat()})"
                )
            key = (value.interval_start, value.interval_end, value.quantile)
            if key in seen:
                raise ValueError(
                    f"duplicate (interval, quantile) row at {value.interval_start.isoformat()}"
                )
            seen.add(key)
            sort_key = (
                value.interval_start.timestamp(),
                value.interval_end.timestamp(),
                value.quantile if value.quantile is not None else math.inf,
            )
            if previous is not None and sort_key < previous:
                raise ValueError("values must arrive sorted by interval then quantile")
            previous = sort_key

    def at_quantile(self, quantile: float | None) -> ForecastSeries:
        """The sub-series at one probability slice (``None`` = point values).

        The header (source, fetched_at, issued_at) is kept verbatim: selecting
        a slice is a read, not a refetch, so the age honesty travels with it.
        """
        selected = tuple(value for value in self.values if value.quantile == quantile)
        return ForecastSeries(
            variable=self.variable,
            source=self.source,
            fetched_at=self.fetched_at,
            issued_at=self.issued_at,
            values=selected,
        )


def _finite_nonneg(value: object, label: str) -> float:
    number = _finite(value, label)
    if number < 0:
        raise ValueError(f"{label} must be non-negative")
    return number


@dataclass(frozen=True, slots=True)
class TariffInterval:
    """One normalized price interval (ARCHITECTURE section 17, Tariff row).

    ``import_cents_per_kwh``/``export_cents_per_kwh`` are cents per kWh -- the
    spelling the site's own scorecard tariff keys already use.  ``market``
    names where the numbers came from (``static-config`` today, a wholesale
    feed later) and ``quality`` stays inside the pinned vocabulary, so a
    consumer can refuse to treat an operator's flat guess as a market signal.
    """

    interval_start: datetime
    interval_end: datetime
    import_cents_per_kwh: float
    export_cents_per_kwh: float
    currency: str
    market: str
    quality: str
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        start = _utc(self.interval_start, "interval_start")
        end = _utc(self.interval_end, "interval_end")
        if end <= start:
            raise ValueError(
                "interval_start must precede interval_end "
                "(half-open [start, end) tariff windows are never empty)"
            )
        object.__setattr__(self, "interval_start", start)
        object.__setattr__(self, "interval_end", end)
        object.__setattr__(
            self,
            "import_cents_per_kwh",
            _finite_nonneg(self.import_cents_per_kwh, "import_cents_per_kwh"),
        )
        object.__setattr__(
            self,
            "export_cents_per_kwh",
            _finite_nonneg(self.export_cents_per_kwh, "export_cents_per_kwh"),
        )
        currency = _word(self.currency, "currency").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        object.__setattr__(self, "currency", currency)
        if self.market not in TARIFF_MARKET_WORDS:
            raise ValueError(f"market must be one of {sorted(TARIFF_MARKET_WORDS)}")
        if self.quality not in FORECAST_QUALITY_WORDS:
            raise ValueError(f"quality must be one of {sorted(FORECAST_QUALITY_WORDS)}")
        if self.published_at is not None:
            object.__setattr__(self, "published_at", _utc(self.published_at, "published_at"))
