"""The forecast read surface (the console's solar outlook + accuracy scoreboard).

ARCHITECTURE section 17 composes the advisory forecast providers behind a
strict doctrine: advisory-only, no fleet-loop slot, no control path reads.
This module is the registry's FIRST consumer and keeps that doctrine -- a
pure READ surface the facade serves at ``GET /api/v1/forecast``:

- The application layer never imports the provider adapters (section 24's
  advisory-only pin): the composition root injects the PV provider, the
  provider notes, and the provider round's cross-check SCORER through the
  structural ports below, so this module stays a consumer of shapes, never
  of adapter modules.
- The provider's own cache gate owns the wire budget.  A read fetches through
  ``pv_forecast()`` (fresh inside the refresh interval, the cached series --
  its ORIGINAL ``fetched_at`` -- otherwise), so a console polling faster than
  the provider's refresh interval burns no quota.
- The outlook projects the normalized series verbatim: half-open intervals,
  the central figure (the 0.5 quantile when the source claims one, the point
  value otherwise), and the [q10, q90] band ONLY where the source actually
  issued deciles.  A deterministic source never gains a fabricated band.
- The scoreboard is the cross-check scorer applied to the ELAPSED window:
  the historian's recorded fleet surplus is the only truth, and only the
  portion of the current fetch whose intervals have already passed can be
  scored.  A forecast with no elapsed minutes yet scores NULL -- an honest
  empty, never a zero-error verdict.
- BASIS HONESTY (the night-v2 challenge panel's amendment A1, 2026-08-24):
  the scorer's recorded basis -- fleet grid export -- is surplus AFTER the
  batteries absorb it.  When the pods absorb the morning (the desired
  outcome), recorded export collapses and every sunny interval scores as a
  forecast miss that is not the forecast's.  This surface therefore carries
  the CORRECTED BASIS'S RAW INPUTS beside the scorer's figures -- the mean
  recorded export, the mean fleet charging rate, the mean reconstructed
  PRE-battery surplus (max(0, export + charging)), and the mean forecast
  watts over the same paired timestamps -- so the night-v2 round can rebuild
  the scorer itself without a second console round.  The inputs are means,
  never integrated kWh: integration owns a cadence assumption this module
  does not, and night-v2 owns that choice.
- Accumulation is PER FETCH and in memory.  Each distinct ``fetched_at``
  contributes one record, re-scored (upserted) as its elapsed window grows;
  a restart starts the evidence over (``durable: false`` says so on every
  response).  Day-scale per-day kWh records need a persistence round this
  module deliberately does not attempt.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "FORECAST_PROVIDERS_NOT_COMMISSIONED",
    "ForecastOutlookControl",
    "ForecastRefusal",
]


class ForecastRefusal(Exception):
    """The forecast surface refused a read (the block-presence doctrine).

    Mirrors the schedule, scorecard, and history refusal types: the REST
    boundary maps it to 409 with the pinned code.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if not code or code != code.strip():
            raise ValueError("refusal code must be non-empty and normalized")
        self.code = code
        self.message = message


FORECAST_PROVIDERS_NOT_COMMISSIONED = "forecast_providers_not_commissioned"

#: The most recent fetches one source keeps in the in-memory scoreboard.
_MAX_RECORDS = 256


class _ForecastValuePort(Protocol):
    """One normalized forecast figure (the model row's structural shape)."""

    @property
    def interval_start(self) -> datetime: ...

    @property
    def interval_end(self) -> datetime: ...

    @property
    def value(self) -> float: ...

    @property
    def quantile(self) -> float | None: ...


class _ForecastSeriesPort(Protocol):
    """One fetch of one variable from one source (the series' shape)."""

    @property
    def variable(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def fetched_at(self) -> datetime: ...

    @property
    def issued_at(self) -> datetime | None: ...

    @property
    def values(self) -> Sequence[_ForecastValuePort]: ...


class _PvForecastPort(Protocol):
    """The registry's PV member (the provider port the surface reads)."""

    async def pv_forecast(self) -> _ForecastSeriesPort: ...


class _StalenessReport(Protocol):
    """The honest age report HTTP providers carry (``ProviderStaleness``)."""

    @property
    def source(self) -> str: ...

    @property
    def fetched_at(self) -> datetime | None: ...

    @property
    def age_s(self) -> float | None: ...

    @property
    def stale(self) -> bool: ...

    @property
    def last_error(self) -> str | None: ...

    @property
    def fetch_count(self) -> int: ...

    @property
    def error_count(self) -> int: ...


class _HistoryRowsPort(Protocol):
    """The historian read port the scorer's truth comes through."""

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> Sequence[Any]: ...


class _ScoredForecast(Protocol):
    """The scorer's result (the cross-check score's structural shape)."""

    @property
    def samples(self) -> int: ...

    @property
    def mae_w(self) -> float: ...

    @property
    def bias_w(self) -> float: ...

    @property
    def rmse_w(self) -> float: ...

    @property
    def inside_band(self) -> float | None: ...


class _SurplusScorer(Protocol):
    """The provider round's cross-check scorer, injected by the composition.

    Raises ``ValueError`` when nothing recorded aligns with the forecast's
    intervals -- the surface treats that as the honest empty.
    """

    def __call__(
        self, forecast: Any, samples: Sequence[Any], *, unit_ids: Sequence[str]
    ) -> _ScoredForecast: ...


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is UTC else moment.astimezone(UTC)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else _utc(moment).isoformat()


class ForecastOutlookControl:
    """The facade-facing forecast read surface (the outlook + the scoreboard).

    Composed exactly when the ``forecast_providers`` block is PRESENT and
    ENABLED (the registry's own gate); NOTHING on this surface mutates
    anything -- it fetches through the providers' cache, reads the historian,
    and projects.
    """

    def __init__(
        self,
        *,
        unit_ids: Sequence[str],
        pv: _PvForecastPort | None,
        scorer: _SurplusScorer,
        notes: Sequence[str] = (),
        history: _HistoryRowsPort | None = None,
        max_fetch_records: int = _MAX_RECORDS,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        if isinstance(max_fetch_records, bool) or not 1 <= int(max_fetch_records) <= 10_000:
            raise ValueError("max_fetch_records must lie in [1, 10000]")
        self._unit_ids = units
        self._pv = pv
        self._scorer = scorer
        self._notes = tuple(str(note) for note in notes)
        self._history = history
        self._max_records = int(max_fetch_records)
        # The in-memory scoreboard: source -> {fetched_at iso -> record}.
        # Insertion order is fetch order; the cap drops the OLDEST fetch.
        self._records: dict[str, dict[str, dict[str, Any]]] = {}

    # --- the read ----------------------------------------------------------------

    async def outlook_payload(self, *, now_utc: datetime | None = None) -> dict[str, Any]:
        """One outlook + scoreboard projection; never raises provider failures."""
        now = _utc(now_utc) if now_utc is not None else datetime.now(UTC)
        payload: dict[str, Any] = {
            "as_of": now.isoformat(),
            # Whether the historian exists at all: without it NO accuracy
            # evidence can ever accumulate, and the console words that state
            # instead of showing a forever-empty scoreboard.
            "history_composed": self._history is not None,
            "pv": None,
            "provider": None,
            "notes": list(self._notes),
            "score": None,
            "scoreboard": None,
        }
        if self._pv is None:
            return payload
        series: _ForecastSeriesPort | None
        try:
            series = await self._pv.pv_forecast()
        except Exception:
            # A failed fetch with no cache is an honest absence; the provider's
            # own staleness report (below) carries the failure's words.
            series = None
        report = self._staleness()
        if report is not None:
            payload["provider"] = {
                "source": report.source,
                "staleness": {
                    "fetched_at": _iso(report.fetched_at),
                    "age_s": report.age_s,
                    "stale": bool(report.stale),
                    "last_error": report.last_error,
                    "fetch_count": report.fetch_count,
                    "error_count": report.error_count,
                },
            }
        if series is None:
            return payload
        payload["pv"] = self._series_payload(series)
        payload["score"] = self._score_and_remember(series, now)
        payload["scoreboard"] = self._scoreboard_payload(series.source)
        return payload

    # --- the outlook projection ---------------------------------------------------

    @staticmethod
    def _series_payload(series: _ForecastSeriesPort) -> dict[str, Any]:
        """The normalized series verbatim: intervals, central, and real bands."""
        central: dict[tuple[datetime, datetime], float] = {}
        lows: dict[tuple[datetime, datetime], float] = {}
        highs: dict[tuple[datetime, datetime], float] = {}
        quantiled = False
        for value in series.values:
            key = (value.interval_start, value.interval_end)
            if value.quantile is None:
                central.setdefault(key, value.value)
            elif value.quantile == 0.5:
                central[key] = value.value
            elif value.quantile == 0.1:
                lows[key] = value.value
            elif value.quantile == 0.9:
                highs[key] = value.value
            else:  # pragma: no cover - no composed source serves other slices
                continue
        intervals: list[dict[str, Any]] = []
        for (start, end), watts in sorted(central.items()):
            row: dict[str, Any] = {
                "start": _utc(start).isoformat(),
                "end": _utc(end).isoformat(),
                "w": watts,
            }
            if (start, end) in lows and (start, end) in highs:
                row["q10"] = lows[(start, end)]
                row["q90"] = highs[(start, end)]
                quantiled = True
            intervals.append(row)
        return {
            "source": series.source,
            "variable": series.variable,
            "fetched_at": _iso(series.fetched_at),
            "issued_at": _iso(series.issued_at),
            "quantiled": quantiled,
            "horizon_from": _iso(series.values[0].interval_start) if series.values else None,
            "horizon_to": _iso(series.values[-1].interval_end) if series.values else None,
            "intervals": intervals,
        }

    # --- the cross-check + the in-memory scoreboard ---------------------------------

    def _score_and_remember(
        self, series: _ForecastSeriesPort, now: datetime
    ) -> dict[str, Any] | None:
        """Score the fetch's ELAPSED window against the recorded surplus."""
        if self._history is None or not series.values:
            return None
        horizon_from = series.values[0].interval_start
        window_to = min(now, series.values[-1].interval_end)
        if window_to <= horizon_from:
            return None
        rows = self._history.samples(self._unit_ids, horizon_from, window_to)
        try:
            score = self._scorer(series, rows, unit_ids=self._unit_ids)
        except ValueError:
            # Nothing recorded inside the elapsed intervals yet (a fresh fetch,
            # or the historian has no rows there): the honest empty, never a
            # fabricated zero-error score.
            return None
        record: dict[str, Any] = {
            "source": series.source,
            "fetched_at": _iso(series.fetched_at),
            "scored_at": now.isoformat(),
            "window_from": _utc(horizon_from).isoformat(),
            "window_to": _utc(window_to).isoformat(),
            "samples": score.samples,
            "mae_w": score.mae_w,
            "bias_w": score.bias_w,
            "rmse_w": score.rmse_w,
            "inside_band": score.inside_band,
        }
        self._remember(series.source, record)
        payload = {
            key: record[key]
            for key in (
                "source",
                "fetched_at",
                "window_from",
                "window_to",
                "samples",
                "mae_w",
                "bias_w",
                "rmse_w",
                "inside_band",
            )
        }
        # The corrected basis's raw inputs over the SAME paired timestamps
        # (amendment A1): the scorer's own fleet rule, re-stated here so the
        # reconstruction rides the read.  Means only -- never integrated kWh.
        basis = _pre_battery_basis(series, rows, self._unit_ids)
        if basis is not None:
            payload["basis"] = basis
        return payload

    def _remember(self, source: str, record: dict[str, Any]) -> None:
        """Upsert one fetch's record; one ``fetched_at`` is one evidence row."""
        records = self._records.setdefault(source, {})
        fetched = record["fetched_at"]
        if fetched not in records and len(records) >= self._max_records:
            records.pop(next(iter(records)))
        records[fetched] = record

    def _scoreboard_payload(self, source: str) -> dict[str, Any] | None:
        records = list(self._records.get(source, {}).values())
        if not records:
            return None
        count = len(records)
        mean_bias = sum(float(record["bias_w"]) for record in records) / count
        mean_mae = sum(float(record["mae_w"]) for record in records) / count
        bands = [
            float(record["inside_band"])
            for record in records
            if record["inside_band"] is not None
        ]
        return {
            "source": source,
            "since": min(str(record["window_from"]) for record in records),
            "records": count,
            "total_samples": sum(int(record["samples"]) for record in records),
            "mean_bias_w": mean_bias,
            "mean_mae_w": mean_mae,
            "mean_inside_band": (sum(bands) / len(bands)) if bands else None,
            # The accumulation is this process's memory only: a restart starts
            # the evidence over, and the console must say so rather than imply
            # a durable ledger.
            "durable": False,
        }

    def _staleness(self) -> _StalenessReport | None:
        """The provider's age report when it carries one (HTTP members do)."""
        reader = getattr(self._pv, "staleness", None)
        if not callable(reader):
            return None
        try:
            report = reader()
        except Exception:  # pragma: no cover - the report is a plain read
            return None
        return report if report is not None else None


def _pre_battery_basis(
    series: _ForecastSeriesPort,
    rows: Sequence[Any],
    unit_ids: Sequence[str],
) -> dict[str, Any] | None:
    """The corrected basis's raw inputs over the scorer's own pairing.

    The scorer's fleet rule, restated: a timestamp counts only when EVERY
    configured unit has a row with a non-null grid word, and only timestamps
    inside a forecast interval are evidence.  Over those pairs this carries
    the mean recorded export, the mean fleet CHARGING rate (positive; the
    historian's ``battery_watts`` is charge-negative, discharge-positive),
    the mean reconstructed PRE-battery surplus -- ``max(0, export + charging)``,
    amendment A1 -- and the mean forecast watts.  A timestamp whose battery
    words are not fully reported contributes to the export/forecast means but
    not the pre-battery ones (each mean divides by its own count, and the
    counts are served so the consumer can see the difference).  None when the
    pairing is empty -- the caller already words that state.
    """
    expected = set(unit_ids)
    by_timestamp: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        # The historian's row always carries these fields (a frozen dataclass).
        unit = row.unit_id
        if unit not in expected:
            continue
        by_timestamp.setdefault(row.sampled_at, {})[unit] = row
    central: dict[tuple[datetime, datetime], float] = {}
    for value in series.values:
        if value.quantile is None or value.quantile == 0.5:
            central.setdefault((value.interval_start, value.interval_end), value.value)
    exports: list[float] = []
    forecasts: list[float] = []
    pre_battery: list[float] = []
    charging: list[float] = []
    for moment in sorted(by_timestamp):
        holders = by_timestamp[moment]
        if set(holders) != expected:
            continue
        grids: list[Any] = [holders[unit].grid_power_w for unit in expected]
        if any(grid is None for grid in grids):
            continue
        forecast_w = _central_at(central, moment)
        if forecast_w is None:
            continue  # outside every interval: not evidence (the scorer's rule)
        export = sum(float(grid) for grid in grids)
        exports.append(export)
        forecasts.append(forecast_w)
        # The historian's row always carries the battery word (a frozen
        # dataclass field); a null VALUE is the honest absence, caught below.
        batteries = [holders[unit].battery_watts for unit in expected]
        if any(battery is None for battery in batteries):
            continue
        charge_rate = sum(max(0.0, -float(battery)) for battery in batteries)
        charging.append(charge_rate)
        pre_battery.append(max(0.0, export + charge_rate))
    if not exports:
        return None

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    basis: dict[str, Any] = {
        "paired_samples": len(exports),
        "mean_forecast_w": mean(forecasts),
        "mean_export_w": mean(exports),
        "mean_pre_battery_surplus_w": (mean(pre_battery) if pre_battery else None),
        "mean_charging_w": (mean(charging) if charging else None),
    }
    return basis


def _central_at(
    central: dict[tuple[datetime, datetime], float], moment: datetime
) -> float | None:
    """The central forecast watts of the interval containing ``moment``."""
    for (start, end), watts in sorted(central.items()):
        if start <= moment < end:
            return watts
    return None
