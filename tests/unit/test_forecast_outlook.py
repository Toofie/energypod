"""The forecast read surface (the console's solar outlook + accuracy scoreboard).

The pinned contracts under test: the honest-null payload shapes (no provider
family, a failed fetch with no cache, nothing elapsed to score), the verbatim
interval projection (the central figure and the [q10, q90] band ONLY where the
source issued deciles), the REAL cross-check scorer applied to the elapsed
window, the per-fetch in-memory accumulation (one ``fetched_at`` = one record,
re-scored as the window grows, capped, never durable), and the facade's
block-presence refusal.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energypod.adapters.providers.http import ProviderStaleness
from energypod.adapters.providers.model import ForecastSeries, ForecastValue

try:
    from energypod.application.forecast import (
        FORECAST_PROVIDERS_NOT_COMMISSIONED,
        ForecastOutlookControl,
        ForecastRefusal,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    ForecastOutlookControl: Any = None
    ForecastRefusal: Any = None
    FORECAST_PROVIDERS_NOT_COMMISSIONED = ""
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None

from energypod.domain.history import TelemetrySampleRow

UNITS = ("mid", "rhs")
BASE = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The forecast read surface is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


# --- the fakes ----------------------------------------------------------------------


class FakePv:
    """A scripted PV provider: one series per call, or a failure."""

    source = "solcast"

    def __init__(
        self,
        series: list[ForecastSeries | None] | None = None,
        *,
        failure: Exception | None = None,
        report: ProviderStaleness | None = None,
    ) -> None:
        self._series = list(series or [])
        self._failure = failure
        self._report = report
        self.calls = 0

    async def pv_forecast(self) -> ForecastSeries:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        if not self._series:
            raise AssertionError("the scripted provider ran out of series")
        nxt = self._series.pop(0)
        if nxt is None:
            raise RuntimeError("provider unavailable: no cache and the wire failed")
        return nxt

    def staleness(self) -> ProviderStaleness:
        if self._report is not None:
            return self._report
        return ProviderStaleness(
            source=self.source,
            fetched_at=None,
            age_s=None,
            stale=False,
            last_error=None,
            fetch_count=self.calls,
            error_count=0,
        )


class FakeHistory:
    """The historian read port: scripted rows, recorded windows."""

    def __init__(self, rows: list[TelemetrySampleRow] = ()) -> None:
        self.rows = list(rows)
        self.windows: list[tuple[tuple[str, ...], datetime, datetime]] = []

    def samples(
        self, unit_ids: list[str] | tuple[str, ...], from_at: datetime, to_at: datetime
    ) -> list[TelemetrySampleRow]:
        self.windows.append((tuple(unit_ids), from_at, to_at))
        return [row for row in self.rows if from_at <= row.sampled_at < to_at]


def _row(unit_id: str, sampled_at: datetime, grid_power_w: float) -> TelemetrySampleRow:
    return TelemetrySampleRow(
        unit_id=unit_id,
        sampled_at=sampled_at,
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
        soh_pct=98.0,
        battery_watts=-100.0,
        grid_power_w=grid_power_w,
        load_power_w=None,
        pack_voltage_v=205.0,
        pack_current_a=0.5,
        cell_min_v=3.3,
        cell_max_v=3.35,
        cell_spread_mv=50.0,
        temperature_min_c=22.0,
        temperature_max_c=27.0,
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=2500.0,
        lifecycle="disarmed",
        health_state="healthy",
        quality="good",
        commanded_source=None,
        commanded_direction=None,
        commanded_w=None,
    )


def _fleet_rows(
    moments: list[datetime], per_unit_grid_w: float
) -> list[TelemetrySampleRow]:
    """Both units present at every moment: the fleet-sum the scorer demands."""
    return [
        _row(unit, moment, per_unit_grid_w) for moment in moments for unit in UNITS
    ]


def _series(
    slots: list[tuple[datetime, timedelta, float, float | None, float | None]],
    *,
    source: str = "solcast",
    fetched_at: datetime,
) -> ForecastSeries:
    """One normalized series from (start, length, central, q10, q90) slots."""
    values: list[ForecastValue] = []
    for start, length, central, low, high in slots:
        end = start + length
        for quantile, watts in ((0.5, central), (0.1, low), (0.9, high)):
            if watts is None:
                continue
            values.append(
                ForecastValue(
                    variable="pv_power_w",
                    interval_start=start,
                    interval_end=end,
                    value=watts,
                    source=source,
                    fetched_at=fetched_at,
                    quantile=quantile,
                )
            )
    values.sort(
        key=lambda value: (
            value.interval_start,
            value.interval_end,
            value.quantile if value.quantile is not None else math.inf,
        )
    )
    return ForecastSeries(
        variable="pv_power_w", source=source, fetched_at=fetched_at, values=tuple(values)
    )


def _deterministic_series(
    slots: list[tuple[datetime, timedelta, float]],
    *,
    source: str = "open-meteo",
    fetched_at: datetime,
) -> ForecastSeries:
    values = [
        ForecastValue(
            variable="pv_power_w",
            interval_start=start,
            interval_end=start + length,
            value=watts,
            source=source,
            fetched_at=fetched_at,
            quantile=None,
        )
        for start, length, watts in slots
    ]
    return ForecastSeries(
        variable="pv_power_w", source=source, fetched_at=fetched_at, values=tuple(values)
    )


def _control(
    pv: Any,
    history: Any = None,
    notes: tuple[str, ...] = (),
    max_fetch_records: int | None = None,
) -> ForecastOutlookControl:
    # The REAL scorer, injected exactly as the composition root injects it
    # (the application module never imports the adapter itself).
    from energypod.adapters.providers.cross_check import score_forecast_against_surplus

    return ForecastOutlookControl(
        unit_ids=UNITS,
        pv=pv,
        scorer=score_forecast_against_surplus,
        notes=notes,
        history=history,
        **({} if max_fetch_records is None else {"max_fetch_records": max_fetch_records}),
    )


# --- the outlook projection -----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pv_family_serves_notes_and_honest_nulls() -> None:
    _require_contract()
    control = _control(
        None,
        history=FakeHistory(),
        notes=(
            "solcast: the api key environment variable KEY is not set; the PV provider is absent",
        ),
    )
    payload = await control.outlook_payload(now_utc=BASE)
    assert payload["pv"] is None
    assert payload["provider"] is None
    assert payload["score"] is None
    assert payload["scoreboard"] is None
    assert payload["history_composed"] is True
    assert payload["notes"] == [
        "solcast: the api key environment variable KEY is not set; the PV provider is absent"
    ]
    assert payload["as_of"] == BASE.isoformat()


@pytest.mark.asyncio
async def test_history_absent_is_flagged_so_the_scoreboard_can_word_it() -> None:
    _require_contract()
    control = _control(None, history=None)
    payload = await control.outlook_payload(now_utc=BASE)
    assert payload["history_composed"] is False


@pytest.mark.asyncio
async def test_quantiled_series_projects_intervals_with_the_real_band() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    series = _series(
        [
            (BASE, half, 1000.0, 400.0, 1600.0),
            (BASE + half, half, 1400.0, 600.0, 2100.0),
        ],
        fetched_at=BASE,
    )
    report = ProviderStaleness(
        source="solcast",
        fetched_at=BASE,
        age_s=120.0,
        stale=False,
        last_error=None,
        fetch_count=3,
        error_count=1,
    )
    control = _control(FakePv([series], report=report))
    payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=1))
    pv = payload["pv"]
    assert pv is not None
    assert pv["source"] == "solcast"
    assert pv["variable"] == "pv_power_w"
    assert pv["quantiled"] is True
    assert pv["horizon_from"] == BASE.isoformat()
    assert pv["horizon_to"] == (BASE + timedelta(hours=1)).isoformat()
    assert pv["intervals"] == [
        {
            "start": BASE.isoformat(),
            "end": (BASE + half).isoformat(),
            "w": 1000.0,
            "q10": 400.0,
            "q90": 1600.0,
        },
        {
            "start": (BASE + half).isoformat(),
            "end": (BASE + timedelta(hours=1)).isoformat(),
            "w": 1400.0,
            "q10": 600.0,
            "q90": 2100.0,
        },
    ]
    # The provider's own age report rides verbatim.
    assert payload["provider"] == {
        "source": "solcast",
        "staleness": {
            "fetched_at": BASE.isoformat(),
            "age_s": 120.0,
            "stale": False,
            "last_error": None,
            "fetch_count": 3,
            "error_count": 1,
        },
    }
    # Nothing elapsed yet: the honest empty, never a zero-error score.
    assert payload["score"] is None
    assert payload["scoreboard"] is None


@pytest.mark.asyncio
async def test_deterministic_series_never_gains_a_fabricated_band() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    series = _deterministic_series(
        [(BASE, half, 900.0), (BASE + half, half, 1100.0)],
        fetched_at=BASE,
    )
    control = _control(FakePv([series]))
    payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=1))
    pv = payload["pv"]
    assert pv is not None
    assert pv["quantiled"] is False
    assert all("q10" not in row and "q90" not in row for row in pv["intervals"])
    assert pv["intervals"][0]["w"] == 900.0


@pytest.mark.asyncio
async def test_failed_fetch_with_no_cache_is_an_absence_not_an_error() -> None:
    _require_contract()
    report = ProviderStaleness(
        source="solcast",
        fetched_at=None,
        age_s=None,
        stale=True,
        last_error="request timed out after 10s",
        fetch_count=0,
        error_count=2,
    )
    control = _control(FakePv([], failure=RuntimeError("no cache"), report=report))
    payload = await control.outlook_payload(now_utc=BASE)
    assert payload["pv"] is None
    assert payload["provider"] is not None
    assert payload["provider"]["staleness"]["last_error"] == "request timed out after 10s"
    assert payload["provider"]["staleness"]["error_count"] == 2


# --- the cross-check + the scoreboard -------------------------------------------------


@pytest.mark.asyncio
async def test_score_uses_the_real_scorer_over_the_elapsed_window() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    series = _series(
        [
            (BASE, half, 1000.0, 400.0, 1600.0),
            (BASE + half, half, 1400.0, None, None),
        ],
        fetched_at=BASE,
    )
    history = FakeHistory(
        _fleet_rows(
            [BASE + timedelta(minutes=5), BASE + timedelta(minutes=10)], 350.0
        )
    )
    control = _control(FakePv([series]), history=history)
    payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=15))
    score = payload["score"]
    assert score is not None
    # Two fully-reported timestamps, each pairing with the first interval's
    # central 1000 W against the PRE-BATTERY fleet surplus (A1): the pods
    # charge at 100 W each (the historian's charge-negative battery word), so
    # the recorded export of 700 W reconstructs to 900 W of surplus -- the
    # scorer's own basis since the night-v2 wave, bias +100 over 2 samples.
    assert score["samples"] == 2
    assert score["bias_w"] == pytest.approx(100.0)
    assert score["mae_w"] == pytest.approx(100.0)
    assert score["rmse_w"] == pytest.approx(100.0)
    # The surplus sat inside the claimed [400, 1600] band both times.
    assert score["inside_band"] == pytest.approx(1.0)
    assert score["window_from"] == BASE.isoformat()
    assert score["window_to"] == (BASE + timedelta(minutes=15)).isoformat()
    # The basis decomposition beside the scorer's figures (amendment A1): the
    # scorer itself now rides the pre-battery basis, and the block names it so
    # the console can never mistake which basis the watt figures carry.
    assert score["basis"] == {
        "scorer_basis": "pre_battery",
        "paired_samples": 2,
        "mean_forecast_w": pytest.approx(1000.0),
        "mean_export_w": pytest.approx(700.0),
        "mean_charging_w": pytest.approx(200.0),
        "mean_pre_battery_surplus_w": pytest.approx(900.0),
    }
    # The historian read stays inside the elapsed window and names every unit.
    assert history.windows == [
        (UNITS, BASE, BASE + timedelta(minutes=15)),
    ]
    # The scoreboard carries exactly one record for this fetch.
    board = payload["scoreboard"]
    assert board is not None
    assert board["records"] == 1
    assert board["total_samples"] == 2
    assert board["mean_bias_w"] == pytest.approx(100.0)
    assert board["since"] == BASE.isoformat()
    assert board["durable"] is False


@pytest.mark.asyncio
async def test_no_recorded_rows_score_none_and_accumulate_nothing() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    series = _series([(BASE, half, 1000.0, None, None)], fetched_at=BASE)
    control = _control(FakePv([series]), history=FakeHistory())
    payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=10))
    assert payload["score"] is None
    assert payload["scoreboard"] is None


@pytest.mark.asyncio
async def test_distinct_fetches_accumulate_one_record_each() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    first = _series(
        [(BASE, half, 1000.0, None, None)], fetched_at=BASE
    )
    second = _series(
        [(BASE + half, half, 600.0, None, None)], fetched_at=BASE + half
    )
    history = FakeHistory(
        _fleet_rows(
            [
                BASE + timedelta(minutes=5),
                BASE + half + timedelta(minutes=5),
            ],
            350.0,
        )
    )
    control = _control(FakePv([first, second]), history=history)
    first_payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=10))
    assert first_payload["score"] is not None
    # The pre-battery basis (A1): 1000 promised against 350+350 exported plus
    # 2 x 100 W absorbed = 900 W reconstructed surplus.
    assert first_payload["score"]["bias_w"] == pytest.approx(100.0)
    second_payload = await control.outlook_payload(now_utc=BASE + timedelta(minutes=40))
    assert second_payload["score"] is not None
    assert second_payload["score"]["bias_w"] == pytest.approx(-300.0)
    board = second_payload["scoreboard"]
    assert board is not None
    assert board["records"] == 2
    # One scored timestamp per fetch (the fleet-summed row), two fetches.
    assert board["total_samples"] == 2
    # The mean over both fetches: (+100 + -300) / 2.
    assert board["mean_bias_w"] == pytest.approx(-100.0)
    assert board["mean_mae_w"] == pytest.approx(200.0)
    # No band was ever claimed: the coverage figure stays null, never 0.
    assert board["mean_inside_band"] is None
    # Evidence "since" is the EARLIEST window any record covers.
    assert board["since"] == BASE.isoformat()


@pytest.mark.asyncio
async def test_one_fetch_rerescored_upserts_not_duplicates() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    series = _series([(BASE, half, 1000.0, None, None)], fetched_at=BASE)
    history = FakeHistory(
        _fleet_rows([BASE + timedelta(minutes=5), BASE + timedelta(minutes=20)], 350.0)
    )
    control = _control(FakePv([series, series]), history=history)
    first = await control.outlook_payload(now_utc=BASE + timedelta(minutes=10))
    second = await control.outlook_payload(now_utc=BASE + timedelta(minutes=25))
    assert first["score"] is not None and second["score"] is not None
    assert first["score"]["samples"] == 1
    assert second["score"]["samples"] == 2
    board = second["scoreboard"]
    assert board is not None
    assert board["records"] == 1, "one fetched_at is one evidence row, re-scored"
    assert board["total_samples"] == 2


@pytest.mark.asyncio
async def test_the_record_cap_drops_the_oldest_fetch() -> None:
    _require_contract()
    half = timedelta(minutes=30)
    history = FakeHistory(
        _fleet_rows(
            [
                BASE + timedelta(minutes=5),
                BASE + half + timedelta(minutes=5),
                BASE + 2 * half + timedelta(minutes=5),
            ],
            350.0,
        )
    )
    control = _control(
        FakePv(
            [
                _series([(BASE, half, 1000.0, None, None)], fetched_at=BASE),
                _series([(BASE + half, half, 1000.0, None, None)], fetched_at=BASE + half),
                _series([(BASE + 2 * half, half, 1000.0, None, None)], fetched_at=BASE + 2 * half),
            ]
        ),
        history=history,
        max_fetch_records=2,
    )
    await control.outlook_payload(now_utc=BASE + timedelta(minutes=10))
    await control.outlook_payload(now_utc=BASE + half + timedelta(minutes=10))
    payload = await control.outlook_payload(now_utc=BASE + 2 * half + timedelta(minutes=10))
    board = payload["scoreboard"]
    assert board is not None
    assert board["records"] == 2
    assert board["since"] == (BASE + half).isoformat()


def test_constructor_rejects_bad_wiring() -> None:
    _require_contract()
    with pytest.raises(ValueError, match="unit_ids"):
        ForecastOutlookControl(unit_ids=(), pv=None, scorer=lambda *a, **k: None)
    with pytest.raises(ValueError, match="unit_ids"):
        ForecastOutlookControl(unit_ids=("mid", "mid"), pv=None, scorer=lambda *a, **k: None)
    with pytest.raises(ValueError, match="max_fetch_records"):
        ForecastOutlookControl(
            unit_ids=UNITS, pv=None, scorer=lambda *a, **k: None, max_fetch_records=0
        )


# --- the facade's block-presence doctrine -----------------------------------------------


@pytest.mark.asyncio
async def test_facade_serves_the_outlook_and_refuses_when_uncomposed() -> None:
    _require_contract()
    import importlib
    from types import SimpleNamespace

    from tests.unit.test_service_facade import OPERATOR, make_rig

    service = importlib.import_module("energypod.application.service")
    domain = importlib.import_module("energypod.domain")
    generation = importlib.import_module("energypod.application.generation")
    api = SimpleNamespace(
        EnergyServiceFacade=service.EnergyServiceFacade,
        AuthorityGenerationCoordinator=generation.AuthorityGenerationCoordinator,
        Direction=domain.Direction,
        IntentSource=domain.IntentSource,
        PowerIntent=domain.PowerIntent,
        UnitLifecycle=domain.UnitLifecycle,
        Observation=domain.Observation,
        DataQuality=domain.DataQuality,
    )
    rig = make_rig(api)
    series = _series(
        [(BASE, timedelta(minutes=30), 1000.0, 400.0, 1600.0)], fetched_at=BASE
    )
    surface = _control(FakePv([series]), history=FakeHistory())
    facade = rig.api.EnergyServiceFacade(
        site_id="home",
        clock=rig.clock,
        intents=rig.intents,
        observations=rig.observations,
        authorizations=rig.authorizations,
        audit=rig.audit,
        events=rig.bus,
        coordinator=rig.coordinator,
        actors=rig.handles,
        forecast=surface,
    )
    payload = await facade.get_forecast_outlook(principal=OPERATOR)
    assert payload["pv"] is not None
    assert payload["pv"]["source"] == "solcast"

    bare = make_rig(api).facade
    with pytest.raises(ForecastRefusal) as raised:
        await bare.get_forecast_outlook(principal=OPERATOR)
    assert raised.value.code == FORECAST_PROVIDERS_NOT_COMMISSIONED
