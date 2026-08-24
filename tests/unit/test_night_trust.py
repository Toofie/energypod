"""Contract tests for the night-charge trust ledger (DESIGN_NIGHT_CHARGE_V2
section 3.2, the T-NC2-TRUST family's scorer/gate/persistence half).

The daily unit is a DAY: after ``midday_local`` the controller evaluates the
morning just finished -- the forecast surplus actually used at window open
(archived in the ``night_target_set`` audit row) against the historian's
recorded PRE-BATTERY surplus integrated over ``[window_end, midday)``:

    E_recorded_kwh = sum_slots max(0, fleet_grid_export_w
                                   + fleet_battery_charge_w) * slot_h

Rolling trust over the last ``required_days`` SCORED days earns only on the
COMPOUND gate: N days, mean |err| <= tolerance, the ASYMMETRIC bias inside
[+over, -under] (over-forecast the dangerous, tighter direction -- A4), and
at least ``min_regime_days`` low- AND high-surplus mornings with the buckets
cut at the terciles of THIS SITE'S own recorded distribution.  A day with an
incomplete historian record, or a morning no forecast drove (full-posture
and fallback nights), is EXCLUDED, not failed -- gaps never poison evidence
they merely fail to inform.
"""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

ZONE = ZoneInfo("Australia/Brisbane")
UNITS = ("lhs", "mid", "rhs")
DAY = date(2026, 8, 24)
WINDOW_END = datetime(2026, 8, 24, 6, 0, tzinfo=ZONE)
MIDDAY = datetime(2026, 8, 24, 12, 0, tzinfo=ZONE)
EVALUATED_AT = datetime(2026, 8, 24, 12, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def trust() -> Any:
    try:
        module = importlib.import_module("energypod.application.night_trust")
    except ImportError as error:
        pytest.fail(f"the night-trust contract is not implemented: {error}")
        raise  # pragma: no cover
    for name in (
        "NightTrustDayRecord",
        "NightTrustLedger",
        "TrustGateSettings",
        "TrustSnapshot",
        "integrate_recorded_surplus",
        "record_regime_bucket",
        "tercile_cut_points",
    ):
        assert hasattr(module, name), f"night-trust contract incomplete: {name}"
    return module


@pytest.fixture(scope="module")
def domain_record() -> Any:
    return importlib.import_module("energypod.domain.night_trust")


# --- the fakes -----------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    unit_id: str
    sampled_at: datetime
    grid_power_w: float | None
    battery_watts: float | None = None
    dynamic_charge_limit_w: float | None = None


def _fleet_rows(
    moments: list[datetime],
    *,
    export_w: float = 0.0,
    charge_w: float = 0.0,
    dynamic_limit_w: float | None = 2500.0,
) -> list[Row]:
    """Every unit reporting at every moment: export split evenly, the fleet
    charge word split evenly (the historian's battery word is
    charge-NEGATIVE)."""
    per_unit_export = export_w / len(UNITS)
    per_unit_charge = -charge_w / len(UNITS)
    return [
        Row(unit, moment, per_unit_export, per_unit_charge, dynamic_limit_w)
        for moment in moments
        for unit in UNITS
    ]


def _moments(
    start: datetime, end: datetime, step_s: float = 30.0
) -> list[datetime]:
    span = (end - start).total_seconds()
    return [start + timedelta(seconds=offset) for offset in range(0, int(span), int(step_s))]


class FakeHistory:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.windows: list[tuple[tuple[str, ...], datetime, datetime]] = []

    def samples(self, unit_ids, from_at, to_at):
        self.windows.append((tuple(unit_ids), from_at, to_at))
        return [row for row in self.rows if from_at <= row.sampled_at < to_at]


@dataclass(frozen=True)
class FakeArchivedMorning:
    date: date
    provider: str = "solcast"
    target_policy: str = "forecast_suggest"
    quantile: float | None = 0.1
    window_end: datetime = WINDOW_END
    midday: datetime = MIDDAY
    e_surplus_forecast_kwh: float = 6.0
    e_deficit_kwh: float = 0.5


class FakeArchive:
    def __init__(self, mornings: dict[date, Any] | None = None) -> None:
        self._mornings = dict(mornings or {})

    def __call__(self, day: date) -> Any:
        return self._mornings.get(day)


class FakeAudit:
    def __init__(self, contains: frozenset[str] = frozenset()) -> None:
        self.contains = set(contains)
        self.appended: list[Any] = []

    async def append(self, event: Any) -> None:
        self.appended.append(event)
        self.contains.add(event.event_id)


@dataclass
class FakeClock:
    wall: datetime = EVALUATED_AT
    mono: float = 5000.0

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


class FakeStore:
    """The day-record port: first record per date stands, newest-first reads."""

    def __init__(self) -> None:
        self.records: dict[date, Any] = {}

    def record_day(self, record: Any) -> None:
        self.records.setdefault(record.date, record)

    def get_day(self, day: date) -> Any | None:
        return self.records.get(day)

    def latest_records(self, limit: int) -> tuple[Any, ...]:
        days = sorted(self.records, reverse=True)[:limit]
        return tuple(self.records[day] for day in days)

    def record_count(self) -> int:
        return len(self.records)


def _ledger(
    trust: Any,
    store: FakeStore,
    history: FakeHistory,
    archive: FakeArchive,
    audit: FakeAudit,
    clock: FakeClock | None = None,
    *,
    settings: Any | None = None,
    previously_earned: bool = False,
) -> Any:
    return trust.NightTrustLedger(
        unit_ids=UNITS,
        timezone_name="Australia/Brisbane",
        settings=settings or trust.TrustGateSettings(),
        clock=clock or FakeClock(),
        store=store,
        history=history,
        archive=archive,
        audit=audit,
        previously_earned=previously_earned,
    )


def _archived(day: date, *, forecast_kwh: float = 6.0) -> FakeArchivedMorning:
    return FakeArchivedMorning(
        date=day,
        window_end=datetime(day.year, day.month, day.day, 6, 0, tzinfo=ZONE),
        midday=datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZONE),
        e_surplus_forecast_kwh=forecast_kwh,
    )


# --- the recorded-surplus integration (the pre-battery basis, A1) ---------------------


class TestIntegration:
    def test_a_constant_surplus_integrates_to_kwh(self, trust: Any) -> None:
        rows = _fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=1000.0)
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        # 1000 W held over the full 6 h span = 6 kWh.
        assert result.e_recorded_kwh == pytest.approx(6.0)
        assert result.coverage_pct == pytest.approx(100.0)
        assert result.refusal_share == pytest.approx(0.0), "no refusal while the fleet takes charge"

    def test_absorbed_surplus_is_surplus_the_export_only_failure_named(
        self, trust: Any
    ) -> None:
        """A1's named failure, pinned at the integration layer: a morning the
        batteries took every watt (the meter shows NOTHING) records the full
        surplus -- an export-only integration would score it as zero."""
        rows = _fleet_rows(_moments(WINDOW_END, MIDDAY), charge_w=1000.0)
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.e_recorded_kwh == pytest.approx(6.0)

    def test_partial_absorption_reconstructs_the_pre_battery_surplus(
        self, trust: Any
    ) -> None:
        rows = _fleet_rows(
            _moments(WINDOW_END, MIDDAY), export_w=400.0, charge_w=600.0
        )
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.e_recorded_kwh == pytest.approx(6.0)

    def test_battery_discharge_never_counts_as_surplus(self, trust: Any) -> None:
        rows = _fleet_rows(
            _moments(WINDOW_END, MIDDAY), export_w=0.0, charge_w=-500.0
        )
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.e_recorded_kwh == pytest.approx(0.0)

    def test_an_importing_moment_floors_at_zero_per_timestamp(self, trust: Any) -> None:
        rows = _fleet_rows(
            _moments(WINDOW_END, MIDDAY), export_w=-800.0, charge_w=200.0
        )
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.e_recorded_kwh == pytest.approx(0.0)

    def test_rows_outside_the_morning_span_are_not_evidence(self, trust: Any) -> None:
        rows = _fleet_rows(
            _moments(WINDOW_END - timedelta(hours=1), WINDOW_END), export_w=5000.0
        ) + _fleet_rows(
            _moments(MIDDAY, MIDDAY + timedelta(hours=1)), export_w=5000.0
        )
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is None, "nothing inside the span: the honest empty"

    def test_a_timestamp_missing_a_unit_breaks_the_interval(self, trust: Any) -> None:
        moments = _moments(WINDOW_END, MIDDAY)
        rows = _fleet_rows(moments, export_w=1000.0)
        # Drop every rhs row in the second half: those timestamps are not
        # fleet-reported, so the second half is a gap, not evidence.
        boundary = moments[0] + timedelta(hours=3)
        kept = [row for row in rows if not (row.unit_id == "rhs" and row.sampled_at >= boundary)]
        result = trust.integrate_recorded_surplus(
            kept,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        # The counted anchors run 06:00..08:59:30 (each holding its 30 s), the
        # tail to midday is a gap, and the second half never reports.
        assert result.e_recorded_kwh == pytest.approx(1000.0 * (3.0 - 30.0 / 3600.0) / 1000.0)
        assert result.coverage_pct < 60.0

    def test_a_spacing_above_the_gap_bound_is_a_gap_never_interpolated(
        self, trust: Any
    ) -> None:
        early = _fleet_rows(_moments(WINDOW_END, WINDOW_END + timedelta(hours=1)), export_w=1000.0)
        late = _fleet_rows(
            _moments(WINDOW_END + timedelta(hours=3), MIDDAY), export_w=1000.0
        )
        result = trust.integrate_recorded_surplus(
            early + late,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        # 1 h at the head (less its final sample-to-gap seam) + the full 3 h
        # tail = 14370 s covered; the 2 h hole in the middle is uncovered,
        # never interpolated.
        assert result.e_recorded_kwh == pytest.approx(1000.0 * 14370.0 / 3_600_000.0)
        assert result.coverage_pct == pytest.approx(14370.0 / 21_600.0 * 100.0)

    def test_the_anchor_sample_holds_forward_to_the_next_sample(self, trust: Any) -> None:
        rows = _fleet_rows(_moments(WINDOW_END, MIDDAY, step_s=1800.0), export_w=2000.0)
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=1800.0,
        )
        assert result is not None
        # Each sample holds its half-hour (the last one to the midday boundary
        # -- the morning's known end): 6 h at 2000 W.
        assert result.e_recorded_kwh == pytest.approx(12.0)
        assert result.coverage_pct == pytest.approx(100.0)

    def test_the_refusal_attribution_splits_missed_from_refused(self, trust: Any) -> None:
        """A1's landing attribution: surplus that arrived while the fleet's
        dynamic charge limit read ~0 is the BMS's refusal, never the
        forecast's account."""
        morning = _moments(WINDOW_END, MIDDAY)
        refused = _fleet_rows(morning[: len(morning) // 2], export_w=1000.0, dynamic_limit_w=0.0)
        accepted = _fleet_rows(morning[len(morning) // 2 :], export_w=1000.0)
        result = trust.integrate_recorded_surplus(
            refused + accepted,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.e_recorded_kwh == pytest.approx(6.0)
        assert result.refused_surplus_kwh == pytest.approx(3.0)
        assert result.refusal_share == pytest.approx(0.5)

    def test_a_partially_refusing_fleet_is_not_a_refusal(self, trust: Any) -> None:
        """rhs reading 0 W while lhs/mid still take charge: the FLEET could
        absorb the surplus, so the miss is real and none of it is charged to
        the BMS."""
        morning = _moments(WINDOW_END, MIDDAY)
        rows: list[Row] = []
        for moment in morning:
            for unit in UNITS:
                limit = 0.0 if unit == "rhs" else 2500.0
                rows.append(Row(unit, moment, 1000.0 / 3, -1000.0 / 3 / 3, limit))
        result = trust.integrate_recorded_surplus(
            rows,
            unit_ids=UNITS,
            window_end=WINDOW_END,
            midday=MIDDAY,
            max_sample_gap_s=90.0,
        )
        assert result is not None
        assert result.refusal_share == pytest.approx(0.0)


# --- the regime terciles (A4) ---------------------------------------------------------


class TestTerciles:
    def test_the_buckets_cut_at_the_sites_own_terciles(self, trust: Any) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        low, high = trust.tercile_cut_points(values)
        assert low == pytest.approx(3.0 + 2.0 / 3.0)
        assert high == pytest.approx(6.0 + 1.0 / 3.0)
        assert trust.record_regime_bucket(2.0, low, high) == "low"
        assert trust.record_regime_bucket(5.0, low, high) == "middle"
        assert trust.record_regime_bucket(9.0, low, high) == "high"

    def test_a_degenerate_distribution_still_buckets_honestly(self, trust: Any) -> None:
        low, high = trust.tercile_cut_points([4.0])
        assert trust.record_regime_bucket(4.0, low, high) == "low"


# --- the per-day evaluation -----------------------------------------------------------


class TestEvaluation:
    async def test_a_scored_morning_writes_one_durable_record_and_one_audit_row(
        self, trust: Any, domain_record: Any
    ) -> None:
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(_fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=1000.0)),
            FakeArchive({DAY: _archived(DAY, forecast_kwh=6.0)}),
            FakeAudit(),
        )
        ledger = _ledger(trust, store, history, archive, audit)

        await ledger.evaluate_pending()

        record = store.get_day(DAY)
        assert isinstance(record, domain_record.NightTrustDayRecord)
        assert record.provider == "solcast"
        assert record.e_surplus_forecast_kwh == pytest.approx(6.0)
        assert record.e_recorded_kwh == pytest.approx(6.0)
        assert record.err_pct == pytest.approx(0.0)
        assert record.bias_pct == pytest.approx(0.0)
        assert record.regime_bucket in {"low", "middle", "high"}
        # The morning's history read names the fleet over [window_end, midday).
        assert history.windows == [(UNITS, WINDOW_END, MIDDAY)]
        events = [event for event in audit.appended if event.event_type == "night_trust_evaluated"]
        assert len(events) == 1

    async def test_the_err_is_normalized_by_the_recorded_energy(
        self, trust: Any
    ) -> None:
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(_fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=500.0)),
            FakeArchive({DAY: _archived(DAY, forecast_kwh=6.0)}),
            FakeAudit(),
        )
        await _ledger(trust, store, history, archive, audit).evaluate_pending()
        record = store.get_day(DAY)
        # Recorded 3 kWh against 6 promised: |err| = bias = 100%.
        assert record.e_recorded_kwh == pytest.approx(3.0)
        assert record.err_pct == pytest.approx(100.0)
        assert record.bias_pct == pytest.approx(100.0), "over-forecast is positive"

    async def test_evaluation_is_idempotent_one_row_per_scored_morning(
        self, trust: Any
    ) -> None:
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(_fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=1000.0)),
            FakeArchive({DAY: _archived(DAY)}),
            FakeAudit(),
        )
        ledger = _ledger(trust, store, history, archive, audit)
        await ledger.evaluate_pending()
        await ledger.evaluate_pending()
        assert store.records == {DAY: store.get_day(DAY)}
        events = [e for e in audit.appended if e.event_type == "night_trust_evaluated"]
        assert len(events) == 1

    async def test_the_regime_bucket_is_cut_at_scoring_time_terciles(
        self, trust: Any
    ) -> None:
        """The stored bucket is the morning's own scored fact, cut against the
        site's recorded distribution as it stood that day -- the gate reads it
        verbatim, so a morning's regime never re-buckets under it."""
        store, audit = FakeStore(), FakeAudit()

        async def _evaluate(day: date, export_w: float) -> None:
            window_end = datetime(day.year, day.month, day.day, 6, 0, tzinfo=ZONE)
            midday = datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZONE)
            history = FakeHistory(_fleet_rows(_moments(window_end, midday), export_w=export_w))
            clock = FakeClock(wall=midday + timedelta(minutes=5))
            ledger = _ledger(
                trust,
                store,
                history,
                FakeArchive({day: _archived(day, forecast_kwh=export_w * 6.0 / 1000.0)}),
                audit,
                clock,
            )
            await ledger.evaluate_pending()

        # Day one: the degenerate one-day distribution buckets its own value
        # low; day two at triple the surplus lands high against [3, 9].
        await _evaluate(DAY, export_w=500.0)  # 3 kWh
        await _evaluate(DAY + timedelta(days=1), export_w=1500.0)  # 9 kWh
        assert store.get_day(DAY).regime_bucket == "low"
        assert store.get_day(DAY + timedelta(days=1)).regime_bucket == "high"

    async def test_a_full_posture_morning_is_excluded_not_failed(
        self, trust: Any
    ) -> None:
        """A12: ``full``-posture days and fallback nights archive nothing to
        score -- the day leaves NO record and never counts against trust."""
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(_fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=1000.0)),
            FakeArchive(),  # no archived morning for the date
            FakeAudit(),
        )
        await _ledger(trust, store, history, archive, audit).evaluate_pending()
        assert store.records == {}
        assert audit.appended == []

    async def test_an_incomplete_historian_record_excludes_the_day(
        self, trust: Any
    ) -> None:
        # Only 90 minutes of the 6-hour morning recorded: coverage 25%.
        rows = _fleet_rows(
            _moments(WINDOW_END, WINDOW_END + timedelta(minutes=90)), export_w=1000.0
        )
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(rows),
            FakeArchive({DAY: _archived(DAY)}),
            FakeAudit(),
        )
        await _ledger(trust, store, history, archive, audit).evaluate_pending()
        assert store.records == {}, "excluded, not failed -- no poisoned evidence"

    async def test_before_midday_the_morning_is_not_yet_scoreable(
        self, trust: Any
    ) -> None:
        store, history, archive, audit = (
            FakeStore(),
            FakeHistory(_fleet_rows(_moments(WINDOW_END, MIDDAY), export_w=1000.0)),
            FakeArchive({DAY: _archived(DAY)}),
            FakeAudit(),
        )
        clock = FakeClock(wall=datetime(2026, 8, 24, 9, 0, tzinfo=ZONE))
        await _ledger(trust, store, history, archive, audit, clock).evaluate_pending()
        assert store.records == {}

    async def test_a_controller_down_at_midday_catches_up(self, trust: Any) -> None:
        store, audit = FakeStore(), FakeAudit()
        archive_mornings = {}
        for offset in (2, 1, 0):
            day = DAY - timedelta(days=offset)
            archive_mornings[day] = _archived(day)
        # Two full days recorded plus today's partial morning still unscoreable.
        rows: list[Row] = []
        for offset in (2, 1):
            day = DAY - timedelta(days=offset)
            window_end = datetime(day.year, day.month, day.day, 6, 0, tzinfo=ZONE)
            midday = datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZONE)
            rows.extend(_fleet_rows(_moments(window_end, midday), export_w=1000.0))
        ledger = _ledger(trust, store, FakeHistory(rows), FakeArchive(archive_mornings), audit)
        await ledger.evaluate_pending()
        assert set(store.records) == {DAY - timedelta(days=2), DAY - timedelta(days=1)}


# --- the compound gate (section 3.2 / A4) ----------------------------------------------


def _seed_records(
    trust: Any,
    store: FakeStore,
    *,
    count: int,
    start_day: date,
    err_pct: float = 10.0,
    bias_pct: float = 2.0,
    buckets: str | list[str] = "high",
) -> None:
    """Seed scored days straight into the store: the stored ``regime_bucket``
    is the record's own scored-morning fact (cut at scoring time against the
    site's then-current terciles), and the gate reads it verbatim."""
    for index in range(count):
        day = start_day + timedelta(days=index)
        bucket = buckets if isinstance(buckets, str) else buckets[index % len(buckets)]
        store.record_day(
            trust.NightTrustDayRecord(
                date=day,
                provider="solcast",
                target_policy="forecast_suggest",
                quantile=0.1,
                window_end_local="06:00",
                midday_local="12:00",
                e_surplus_forecast_kwh=6.0,
                e_deficit_kwh=0.5,
                e_recorded_kwh=6.0,
                coverage_pct=100.0,
                err_pct=err_pct,
                bias_pct=bias_pct,
                refusal_share=None,
                regime_bucket=bucket,
                evaluated_at=EVALUATED_AT.isoformat(),
            )
        )


_MIXED_BUCKETS = ["low"] * 4 + ["high"] * 4 + ["middle"] * 6


class TestTheCompoundGate:
    def test_below_required_days_is_provisioning_the_honest_word(self, trust: Any) -> None:
        store = FakeStore()
        _seed_records(trust, store, count=13, start_day=DAY - timedelta(days=13))
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        snapshot = ledger.snapshot()
        assert snapshot.state == "provisioning"
        assert snapshot.days_scored == 13
        assert snapshot.required_days == 14

    def test_earned_needs_the_mean_the_bias_and_the_regime_mix(self, trust: Any) -> None:
        store = FakeStore()
        # 4 low, 4 high, 6 middle: the 3-and-3 regime mix satisfied, mean err
        # 10%, bias +2%.
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            buckets=_MIXED_BUCKETS,
        )
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        snapshot = ledger.snapshot()
        assert snapshot.state == "earned"
        assert snapshot.days_scored == 14
        assert snapshot.mean_abs_err_pct == pytest.approx(10.0)
        assert snapshot.bias_pct == pytest.approx(2.0)
        assert snapshot.low_surplus_days == 4
        assert snapshot.high_surplus_days == 4

    def test_the_one_stable_high_fortnight_proves_nothing(self, trust: Any) -> None:
        """A4's named kill: 14 correlated fair-weather passes with every
        morning in ONE regime bucket never earn -- the gate must see both
        kinds of sky."""
        store = FakeStore()
        _seed_records(trust, store, count=14, start_day=DAY - timedelta(days=14))
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        snapshot = ledger.snapshot()
        assert snapshot.state == "provisioning"
        assert snapshot.high_surplus_days == 14
        assert snapshot.low_surplus_days == 0

    def test_overforecast_bias_is_the_tighter_direction(self, trust: Any) -> None:
        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            bias_pct=10.5,
            buckets=_MIXED_BUCKETS,
        )
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        assert ledger.snapshot().state == "provisioning", "+10.5% over-forecast fails"

        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            bias_pct=-19.0,
            buckets=_MIXED_BUCKETS,
        )
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        assert ledger.snapshot().state == "earned", "-19% under-forecast still passes"

        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            bias_pct=-21.0,
            buckets=_MIXED_BUCKETS,
        )
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        assert ledger.snapshot().state == "provisioning", "-21% fails the wide bound too"

    def test_a_breaching_window_suspends_a_previously_earned_trust(self, trust: Any) -> None:
        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            buckets=_MIXED_BUCKETS,
        )
        # The durable once-ever fact exists (the operator promoted once).
        ledger = _ledger(
            trust, store, FakeHistory([]), FakeArchive(), FakeAudit(), previously_earned=True
        )
        assert ledger.snapshot().state == "earned"

        # Seven correlated busts roll into the window: the mean breaches.
        _seed_records(
            trust,
            store,
            count=7,
            start_day=DAY,
            err_pct=85.0,
            bias_pct=85.0,
            buckets="middle",
        )
        snapshot = ledger.snapshot()
        assert snapshot.state == "suspended"
        assert snapshot.days_scored == 21

    def test_a_suspended_trust_re_earns_when_the_window_recovers(self, trust: Any) -> None:
        """Section 14: the asymmetric hysteresis is deliberately unbuilt --
        re-entry is symmetric with entry, and the demote-itself-loudly
        behavior is v2's protection."""
        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=7,
            start_day=DAY - timedelta(days=21),
            err_pct=60.0,
            buckets="middle",
        )
        ledger = _ledger(
            trust, store, FakeHistory([]), FakeArchive(), FakeAudit(), previously_earned=True
        )
        assert ledger.snapshot().state == "suspended"
        # A fresh passing fortnight with the 3-and-3 mix ages into the window:
        # the last 14 scored days now pass every arm.
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=13),
            err_pct=5.0,
            bias_pct=1.0,
            buckets=_MIXED_BUCKETS,
        )
        assert ledger.snapshot().state == "earned"

    async def test_the_earned_fact_is_durable_once(self, trust: Any) -> None:
        store, history, archive = FakeStore(), FakeHistory([]), FakeArchive()
        audit = FakeAudit(contains=frozenset({"night-trust-earned-once"}))
        ledger = _ledger(trust, store, history, archive, audit, previously_earned=True)
        await ledger.note_earned()
        assert [e for e in audit.appended if e.event_type == "night_trust_earned"] == []

        fresh_audit = FakeAudit()
        ledger = _ledger(trust, store, history, archive, fresh_audit)
        await ledger.note_earned()
        events = [e for e in fresh_audit.appended if e.event_type == "night_trust_earned"]
        assert len(events) == 1
        assert events[0].event_id == "night-trust-earned-once"

    def test_the_snapshot_payload_is_the_section_7_shape(self, trust: Any) -> None:
        store = FakeStore()
        ledger = _ledger(trust, store, FakeHistory([]), FakeArchive(), FakeAudit())
        payload = ledger.snapshot().payload()
        assert payload == {
            "state": "provisioning",
            "days_scored": 0,
            "required_days": 14,
            "mean_abs_err_pct": None,
            "bias_pct": None,
            "low_surplus_days": 0,
            "high_surplus_days": 0,
        }

    def test_the_state_survives_a_restart(self, trust: Any) -> None:
        """Restart-safety: a fresh ledger over the same durable store and the
        same once-ever fact recomputes the identical state."""
        store = FakeStore()
        _seed_records(
            trust,
            store,
            count=14,
            start_day=DAY - timedelta(days=14),
            buckets=_MIXED_BUCKETS,
        )
        first = _ledger(
            trust, store, FakeHistory([]), FakeArchive(), FakeAudit(), previously_earned=True
        )
        second = _ledger(
            trust, store, FakeHistory([]), FakeArchive(), FakeAudit(), previously_earned=True
        )
        assert first.snapshot() == second.snapshot()


# --- the durable store (schema v5, the park_leases precedent) --------------------------


class TestPersistence:
    def test_the_schema_migrates_a_version_four_database_in_place(
        self, tmp_path: Path
    ) -> None:
        """v5 added the day records; v6 added the morning archive and the
        re-target instants (the machine-truth twins of the night audit rows):
        a stamped-v4 database with live rows upgrades in place to the latest
        stamped version, existing tables untouched."""
        from energypod.adapters.persistence.sqlite import SQLiteDatabase
        from energypod.db.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION == 6
        path = tmp_path / "trust-migrate.sqlite3"
        raw = sqlite3.connect(path)
        try:
            raw.execute(
                "CREATE TABLE schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
                "version INTEGER NOT NULL UNIQUE)"
            )
            raw.execute("INSERT INTO schema_version(singleton, version) VALUES (1, 4)")
            raw.execute("CREATE TABLE park_leases (unit_id TEXT PRIMARY KEY)")
            raw.execute("INSERT INTO park_leases(unit_id) VALUES ('rhs')")
            raw.commit()
        finally:
            raw.close()

        database = SQLiteDatabase(path)
        database.open()
        try:
            stamped = database.connection.execute(
                "SELECT version FROM schema_version WHERE singleton = 1"
            ).fetchone()
            assert stamped == (6,)
            assert database.connection.execute(
                "SELECT COUNT(*) FROM park_leases"
            ).fetchone() == (1,), "an in-place upgrade touches no existing table's rows"
            for table in ("night_trust_day", "night_morning_archive", "night_target_revisions"):
                present = database.connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                assert present is not None, table
        finally:
            database.close()

    def test_records_round_trip_and_the_first_record_per_day_stands(
        self, trust: Any, tmp_path: Path
    ) -> None:
        from energypod.adapters.persistence.sqlite import (
            SQLiteDatabase,
            SQLiteNightTrustRepository,
        )

        database = SQLiteDatabase(tmp_path / "trust.sqlite3")
        database.open()
        try:
            repository = SQLiteNightTrustRepository(database)
            record = trust.NightTrustDayRecord(
                date=DAY,
                provider="solcast",
                target_policy="forecast_suggest",
                quantile=0.1,
                window_end_local="06:00",
                midday_local="12:00",
                e_surplus_forecast_kwh=6.0,
                e_deficit_kwh=0.5,
                e_recorded_kwh=5.8,
                coverage_pct=100.0,
                err_pct=3.4482758620689653,
                bias_pct=3.4482758620689653,
                refusal_share=0.25,
                regime_bucket="high",
                evaluated_at=EVALUATED_AT.isoformat(),
            )
            repository.record_day(record)
            # A second record for the same date is the idempotence refusal:
            # one row per scored morning, ever.
            repository.record_day(
                trust.NightTrustDayRecord(
                    date=DAY,
                    provider="solcast",
                    target_policy="forecast_suggest",
                    quantile=0.1,
                    window_end_local="06:00",
                    midday_local="12:00",
                    e_surplus_forecast_kwh=9.0,
                    e_deficit_kwh=0.0,
                    e_recorded_kwh=9.0,
                    coverage_pct=100.0,
                    err_pct=0.0,
                    bias_pct=0.0,
                    refusal_share=None,
                    regime_bucket="low",
                    evaluated_at=EVALUATED_AT.isoformat(),
                )
            )
            loaded = repository.get_day(DAY)
            assert loaded is not None
            assert loaded.e_recorded_kwh == pytest.approx(5.8)
            assert loaded.regime_bucket == "high"
            assert loaded == record
            later = repository.get_day(DAY + timedelta(days=1))
            assert later is None
            other = trust.NightTrustDayRecord(
                date=DAY - timedelta(days=1),
                provider="solcast",
                target_policy="forecast_suggest",
                quantile=0.1,
                window_end_local="06:00",
                midday_local="12:00",
                e_surplus_forecast_kwh=6.0,
                e_deficit_kwh=0.5,
                e_recorded_kwh=6.0,
                coverage_pct=100.0,
                err_pct=0.0,
                bias_pct=0.0,
                refusal_share=None,
                regime_bucket="middle",
                evaluated_at=EVALUATED_AT.isoformat(),
            )
            repository.record_day(other)
            assert repository.latest_records(10) == (record, other)
        finally:
            database.close()
