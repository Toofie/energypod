"""The night-charge trust ledger (DESIGN_NIGHT_CHARGE_V2 section 3.2).

The daily unit is a DAY, not a watt-tick: after ``midday_local`` each day the
controller evaluates the morning just finished -- the forecast surplus that
ACTUALLY drove the window's target (archived verbatim in the adviser's
``night_target_set`` audit row, read back through the injected archive port)
against the historian's recorded PRE-BATTERY surplus integrated over
``[window_end, midday)``::

    E_recorded_kwh = sum_slots max(0, fleet_grid_export_w
                                   + fleet_battery_charge_w) * slot_h

The recorded basis is amendment A1's load-bearing correction: the export
channel alone is a POST-battery proxy that reads LOW on exactly the mornings
the forecast was RIGHT, so the integration reconstructs the surplus the
batteries absorbed from both words, fleet-summed under the all-units-reported
rule and floored at zero per timestamp.

The gate (amendment A4) is COMPOUND over the last ``required_days`` scored
days: N days, mean |err| <= tolerance, the ASYMMETRIC signed bias inside
[+over, -under] (over-forecast the dangerous, tighter direction -- a provider
passing the mean while systematically over-forecasting under-charges every
night), and at least ``min_regime_days`` low- AND high-surplus mornings with
the buckets cut at the terciles of THIS SITE'S own recorded distribution.  A
day is EXCLUDED, not failed, when the historian record is incomplete or no
forecast drove the window (``full``-posture and fallback nights archive
nothing to score -- A12): gaps never poison evidence they merely fail to
inform.  The one deliberate exclusion boundary: a morning the batteries
REFUSED charge still scored, because on the pre-battery basis the refused
surplus was exported and measured -- the refusal share adjusts the landing's
accounting, never the surplus error.

This module is the application layer's consumer of shapes only: the
historian rows, the archived mornings, and the durable store arrive through
the structural ports below, and nothing here imports a provider adapter.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.audit import AuditEvent
from energypod.domain.night_trust import NightTrustDayRecord
from energypod.domain.observations import UnitLifecycle

__all__ = [
    "NIGHT_TRUST_EARNED_EVENT_ID",
    "ArchivedMorning",
    "MorningArchivePort",
    "NightTrustDayRecord",
    "NightTrustLedger",
    "NightTrustStore",
    "RecordedMorning",
    "TrustGateSettings",
    "TrustSnapshot",
    "TrustStateWord",
    "integrate_recorded_surplus",
    "record_regime_bucket",
    "tercile_cut_points",
]

TrustStateWord = Literal["provisioning", "earned", "suspended"]

#: The deterministic once-ever event id of the durable promotion receipt (the
#: partition-acknowledgement pattern: one keyed existence check, never a scan).
NIGHT_TRUST_EARNED_EVENT_ID = "night-trust-earned-once"

#: The fleet dynamic charge limit at or below which a surplus-bearing
#: timestamp counts as the BMS's refusal (the observed "rhs reads 0 W when
#: full"; a fleet that could still take charge has not refused anything).
_REFUSAL_FLEET_LIMIT_W = 0.0

_TRUST_PRINCIPAL = "energypod:night-trust-ledger"
_TRUST_POLICY_VERSION = "night-trust-1"
# The advisory-row lifecycle word: the ledger observes and records, it never
# commands a unit -- the row names no actuation state.
_LEDGER_LIFECYCLE = UnitLifecycle.DISARMED


# --- the recorded-surplus integration (A1's corrected basis) ------------------------


class _SurplusRow(Protocol):
    """The historian row shape the integration reads (structural)."""

    @property
    def unit_id(self) -> str: ...

    @property
    def sampled_at(self) -> datetime: ...

    @property
    def grid_power_w(self) -> float | None: ...

    @property
    def battery_watts(self) -> float | None: ...

    @property
    def dynamic_charge_limit_w(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class RecordedMorning:
    """The pre-battery surplus integration over one morning span."""

    e_recorded_kwh: float
    coverage_pct: float
    refused_surplus_kwh: float
    refusal_share: float | None


def integrate_recorded_surplus(
    rows: Sequence[_SurplusRow],
    *,
    unit_ids: Sequence[str],
    window_end: datetime,
    midday: datetime,
    max_sample_gap_s: float,
) -> RecordedMorning | None:
    """Integrate the fleet's recorded PRE-BATTERY surplus over one morning.

    The energy-accountant's zero-order hold, at morning scope: a timestamp
    counts only when EVERY configured unit reported a non-null grid AND
    battery word; each counted anchor's reconstructed surplus
    (``max(0, fleet export + fleet charge)``, A1) covers the interval up to
    the next counted anchor -- or up to ``midday`` itself, the morning's
    known end, when that closing spacing sits inside the gap bound.  The head
    of the span before the first anchor stays UNCOVERED: backward-filling a
    sample's watts over time it never spoke for is fabrication, and a spacing
    above ``max_sample_gap_s`` is a GAP (zero energy, zero coverage, never an
    interpolation).  Refused surplus is the share of covered surplus energy
    that arrived while the FLEET's dynamic charge limit read at or below zero
    -- the BMS's refusal, never the forecast's account.
    """
    units = tuple(unit_ids)
    if not units:
        raise ValueError("unit_ids must be non-empty")
    if window_end.tzinfo is None or midday.tzinfo is None or midday <= window_end:
        raise ValueError("the morning span needs timezone-aware bounds with end after start")
    if not math.isfinite(max_sample_gap_s) or max_sample_gap_s <= 0:
        raise ValueError("max_sample_gap_s must be positive and finite")
    expected = set(units)
    by_timestamp: dict[datetime, dict[str, tuple[float | None, float | None, float | None]]] = {}
    for row in rows:
        if row.unit_id not in expected:
            continue
        if not window_end <= row.sampled_at < midday:
            continue
        by_timestamp.setdefault(row.sampled_at, {})[row.unit_id] = (
            row.grid_power_w,
            row.battery_watts,
            row.dynamic_charge_limit_w,
        )
    counted: list[tuple[datetime, float, bool]] = []
    for moment in sorted(by_timestamp):
        per_unit = by_timestamp[moment]
        if set(per_unit) != expected:
            continue
        grids: list[float] = []
        batteries: list[float] = []
        limits: list[float] = []
        complete = True
        for grid, battery, limit in (per_unit[unit] for unit in units):
            if grid is None or battery is None:
                complete = False
                break
            grids.append(float(grid))
            batteries.append(float(battery))
            # A null dynamic limit cannot testify to a refusal; the timestamp
            # still integrates -- only the refusal attribution stays unknown.
            limits.append(float(limit) if limit is not None else math.inf)
        if not complete:
            continue
        surplus = max(0.0, sum(grids) + sum(max(0.0, -value) for value in batteries))
        refused = surplus > 0.0 and sum(limits) <= _REFUSAL_FLEET_LIMIT_W
        counted.append((moment, surplus, refused))
    if not counted:
        return None
    span_s = (midday - window_end).total_seconds()
    covered_s = 0.0
    energy_ws = 0.0
    refused_ws = 0.0
    for index, (moment, surplus, refused) in enumerate(counted):
        end = counted[index + 1][0] if index + 1 < len(counted) else midday
        dt = (end - moment).total_seconds()
        if dt <= 0 or dt > max_sample_gap_s:
            continue
        covered_s += dt
        energy_ws += surplus * dt
        if refused:
            refused_ws += surplus * dt
    energy_kwh = energy_ws / 3_600_000.0
    refused_kwh = refused_ws / 3_600_000.0
    return RecordedMorning(
        e_recorded_kwh=energy_kwh,
        coverage_pct=(covered_s / span_s * 100.0) if span_s > 0 else 0.0,
        refused_surplus_kwh=refused_kwh,
        refusal_share=(refused_kwh / energy_kwh) if energy_kwh > 0.0 else None,
    )


# --- the regime terciles (A4) --------------------------------------------------------


def tercile_cut_points(values: Sequence[float]) -> tuple[float, float]:
    """The 1/3 and 2/3 cut points of a recorded-surplus distribution.

    Linear interpolation on the sorted values (the inclusive convention): a
    one-value distribution degenerates to its own value at both cuts, which
    buckets that value LOW -- honest for a gate that needs many mornings
    anyway.
    """
    if not values:
        raise ValueError("tercile_cut_points needs at least one value")
    ordered = sorted(float(value) for value in values)
    return _quantile(ordered, 1.0 / 3.0), _quantile(ordered, 2.0 / 3.0)


def _quantile(ordered: list[float], fraction: float) -> float:
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def record_regime_bucket(recorded_kwh: float, low_cut: float, high_cut: float) -> Literal[
    "low", "middle", "high"
]:
    """One morning's bucket against the site's terciles (low/middle/high)."""
    if recorded_kwh <= low_cut:
        return "low"
    if recorded_kwh >= high_cut:
        return "high"
    return "middle"


# --- the ports ------------------------------------------------------------------------


class NightTrustStore(Protocol):
    """The durable day-record port (the energy-ledger precedent)."""

    def record_day(self, record: NightTrustDayRecord) -> None: ...

    def get_day(self, day: date) -> NightTrustDayRecord | None: ...

    def latest_records(self, limit: int) -> tuple[NightTrustDayRecord, ...]: ...

    def record_count(self) -> int: ...


class _HistoryRowsPort(Protocol):
    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ArchivedMorning:
    """The morning's archived forecast arithmetic (the ``night_target_set``
    row's reconstruction payload, read back post-midday)."""

    date: date
    provider: str
    target_policy: str
    quantile: float | None
    window_end: datetime
    midday: datetime
    e_surplus_forecast_kwh: float
    e_deficit_kwh: float


MorningArchivePort = Callable[[date], ArchivedMorning | None]


class _ClockPort(Protocol):
    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class _AuditAppendPort(Protocol):
    async def append(self, event: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class TrustGateSettings:
    """Every gate key of the ``night_charging.trust`` config block."""

    required_days: int = 14
    tolerance_pct: float = 30.0
    max_overforecast_bias_pct: float = 10.0
    max_underforecast_bias_pct: float = 20.0
    min_regime_days: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.required_days, bool) or self.required_days < 1:
            raise ValueError("required_days must be a positive integer")
        if not math.isfinite(self.tolerance_pct) or self.tolerance_pct <= 0:
            raise ValueError("tolerance_pct must be positive and finite")
        for name in ("max_overforecast_bias_pct", "max_underforecast_bias_pct"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.max_overforecast_bias_pct >= self.max_underforecast_bias_pct:
            raise ValueError(
                "max_overforecast_bias_pct must be strictly below "
                "max_underforecast_bias_pct: over-forecast is the dangerous, "
                "tighter direction (A4)"
            )
        if isinstance(self.min_regime_days, bool) or self.min_regime_days < 1:
            raise ValueError("min_regime_days must be a positive integer")


@dataclass(frozen=True, slots=True)
class TrustSnapshot:
    """The projection's trust block (the section 7 shape)."""

    state: TrustStateWord
    days_scored: int
    required_days: int
    mean_abs_err_pct: float | None
    bias_pct: float | None
    low_surplus_days: int
    high_surplus_days: int

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "days_scored": self.days_scored,
            "required_days": self.required_days,
            "mean_abs_err_pct": self.mean_abs_err_pct,
            "bias_pct": self.bias_pct,
            "low_surplus_days": self.low_surplus_days,
            "high_surplus_days": self.high_surplus_days,
        }


# --- the ledger -----------------------------------------------------------------------


class NightTrustLedger:
    """The per-day evaluator and the rolling trust gate.

    Composed exactly when the night strategy runs a forecast posture with the
    historian available; the fleet loop ticks ``evaluate_pending`` once per
    cycle (a bounded, suppressed step beside the historian's own), and the
    adviser plus the projection read ``snapshot``.  All state is DERIVED from
    the durable store plus the once-ever earned fact, so a restart recomputes
    the identical trust -- there is deliberately no runtime cache to lose.
    """

    def __init__(
        self,
        *,
        unit_ids: Sequence[str],
        timezone_name: str,
        settings: TrustGateSettings,
        clock: _ClockPort,
        store: NightTrustStore,
        history: _HistoryRowsPort,
        archive: MorningArchivePort,
        audit: _AuditAppendPort | None = None,
        previously_earned: bool = False,
        max_sample_gap_s: float = 90.0,
        min_coverage_pct: float = 80.0,
        process_instance_id: str = "energypod-night-trust",
        process_origin_mono: float = 0.0,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be an IANA timezone") from exc
        if not isinstance(settings, TrustGateSettings):
            raise ValueError("settings must be TrustGateSettings")
        for name in ("wall_now", "monotonic"):
            if not callable(getattr(clock, name, None)):
                raise TypeError("clock must provide wall_now() and monotonic()")
        if not math.isfinite(max_sample_gap_s) or max_sample_gap_s <= 0:
            raise ValueError("max_sample_gap_s must be positive and finite")
        if not 0 < min_coverage_pct <= 100:
            raise ValueError("min_coverage_pct must lie in (0, 100]")
        self._unit_ids = units
        self._zone = zone
        self._settings = settings
        self._clock = clock
        self._store = store
        self._history = history
        self._archive = archive
        self._audit = audit
        self._earned_once = bool(previously_earned)
        self._max_sample_gap_s = float(max_sample_gap_s)
        self._min_coverage_pct = float(min_coverage_pct)
        self._process_instance_id = process_instance_id
        self._process_origin_mono = float(process_origin_mono)

    # --- the daily evaluation --------------------------------------------

    async def evaluate_pending(self) -> None:
        """Score every scoreable unrecorded morning, oldest first.

        A morning is scoreable when its archived forecast exists (a forecast
        drove the window -- full-posture and fallback nights archive nothing,
        A12) and ``midday`` has passed; a morning whose historian record is
        incomplete (coverage below the floor, or nothing recorded at all) is
        EXCLUDED without a row, exactly like an unarchived one.
        """
        local_now = self._clock.wall_now().astimezone(self._zone)
        for offset in range(self._settings.required_days, -1, -1):
            day = local_now.date() - timedelta(days=offset)
            if self._store.get_day(day) is not None:
                continue
            archived = self._archive(day)
            if archived is None:
                continue
            if local_now < archived.midday.astimezone(self._zone):
                continue
            record = self._score(day, archived)
            if record is None:
                continue
            previous = self.snapshot()
            self._store.record_day(record)
            current = self.snapshot()
            with contextlib.suppress(Exception):
                if self._audit is not None:
                    await self._audit.append(
                        self._evaluated_event(record, previous.state, current)
                    )
            if current.state == "earned":
                await self.note_earned()

    def _score(self, day: date, archived: ArchivedMorning) -> NightTrustDayRecord | None:
        """One morning's record, or None when the day is excluded."""
        rows = self._history.samples(self._unit_ids, archived.window_end, archived.midday)
        recorded = integrate_recorded_surplus(
            rows,
            unit_ids=self._unit_ids,
            window_end=archived.window_end,
            midday=archived.midday,
            max_sample_gap_s=self._max_sample_gap_s,
        )
        if recorded is None or recorded.coverage_pct < self._min_coverage_pct:
            # Incomplete history: excluded, not failed (section 3.2).
            return None
        denominator = max(recorded.e_recorded_kwh, 1.0)
        err_pct = (
            abs(archived.e_surplus_forecast_kwh - recorded.e_recorded_kwh) / denominator * 100.0
        )
        bias_pct = (
            (archived.e_surplus_forecast_kwh - recorded.e_recorded_kwh) / denominator * 100.0
        )
        distribution = [record.e_recorded_kwh for record in self._store.latest_records(3650)]
        distribution.append(recorded.e_recorded_kwh)
        low_cut, high_cut = tercile_cut_points(distribution)
        return NightTrustDayRecord(
            date=day,
            provider=archived.provider,
            target_policy=archived.target_policy,
            quantile=archived.quantile,
            window_end_local=archived.window_end.astimezone(self._zone).strftime("%H:%M"),
            midday_local=archived.midday.astimezone(self._zone).strftime("%H:%M"),
            e_surplus_forecast_kwh=archived.e_surplus_forecast_kwh,
            e_deficit_kwh=archived.e_deficit_kwh,
            e_recorded_kwh=recorded.e_recorded_kwh,
            coverage_pct=recorded.coverage_pct,
            err_pct=err_pct,
            bias_pct=bias_pct,
            refusal_share=recorded.refusal_share,
            regime_bucket=record_regime_bucket(recorded.e_recorded_kwh, low_cut, high_cut),
            evaluated_at=self._utc_now().isoformat(),
        )

    # --- the gate ----------------------------------------------------------

    def snapshot(self) -> TrustSnapshot:
        """The rolling compound gate over the last ``required_days`` scored
        days (amendment A4's statistics), plus the state word.

        ``days_scored`` is the store's TOTAL (the section 7 example shows 19
        against a 14-day window); every gate statistic rides the window.
        Never earned and failing: ``provisioning`` (the honest not-enough-
        days word).  Earned once and failing: ``suspended`` -- the demote-
        itself-loudly path; re-entry is symmetric with entry (the harder
        hysteresis is the accepted P2, section 14).
        """
        records = self._store.latest_records(self._settings.required_days)
        window = tuple(reversed(records))  # oldest-first for readability
        days_scored = self._store.record_count()
        if not window:
            return TrustSnapshot(
                state="provisioning",
                days_scored=days_scored,
                required_days=self._settings.required_days,
                mean_abs_err_pct=None,
                bias_pct=None,
                low_surplus_days=0,
                high_surplus_days=0,
            )
        mean_abs_err = sum(record.err_pct for record in window) / len(window)
        bias = sum(record.bias_pct for record in window) / len(window)
        low_days = sum(1 for record in window if record.regime_bucket == "low")
        high_days = sum(1 for record in window if record.regime_bucket == "high")
        passes = (
            len(window) >= self._settings.required_days
            and mean_abs_err <= self._settings.tolerance_pct
            and -self._settings.max_underforecast_bias_pct
            <= bias
            <= self._settings.max_overforecast_bias_pct
            and low_days >= self._settings.min_regime_days
            and high_days >= self._settings.min_regime_days
        )
        state: TrustStateWord = (
            "earned" if passes else ("suspended" if self._earned_once else "provisioning")
        )
        return TrustSnapshot(
            state=state,
            days_scored=days_scored,
            required_days=self._settings.required_days,
            mean_abs_err_pct=mean_abs_err,
            bias_pct=bias,
            low_surplus_days=low_days,
            high_surplus_days=high_days,
        )

    async def note_earned(self) -> None:
        """Write the durable once-ever promotion receipt (idempotent).

        The deterministic event id is the operator's promotion check in the
        section 9 sequence; a fact that already exists (boot-loaded from the
        audit store or written this process) never writes twice.
        """
        if self._earned_once:
            return
        self._earned_once = True
        if self._audit is None:
            return
        with contextlib.suppress(Exception):
            await self._audit.append(
                self._row(
                    event_type="night_trust_earned",
                    event_id=NIGHT_TRUST_EARNED_EVENT_ID,
                    result="earned",
                    reason_codes=("durable_once",),
                    payload=self.snapshot().payload(),
                )
            )

    # --- the audit rows ----------------------------------------------------

    def _evaluated_event(
        self, record: NightTrustDayRecord, previous_state: str, current: TrustSnapshot
    ) -> AuditEvent:
        return self._row(
            event_type="night_trust_evaluated",
            event_id=f"night-trust-{uuid4().hex}",
            result=current.state,
            reason_codes=(f"state_{previous_state}_to_{current.state}",),
            payload={"day": record.payload(), "rolling": current.payload()},
        )

    def _row(
        self,
        *,
        event_type: str,
        event_id: str,
        result: str,
        reason_codes: tuple[str, ...],
        payload: dict[str, Any],
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        return AuditEvent(
            event_id=event_id,
            occurred_at=self._utc_now(),
            monotonic_offset_s=now_mono - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type=event_type,
            principal=_TRUST_PRINCIPAL,
            correlation_id=f"night-trust:{event_type}:{event_id}",
            policy_version=_TRUST_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"event_type": event_type, **payload}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=_LEDGER_LIFECYCLE,
            payload=payload,
        )

    def _utc_now(self) -> datetime:
        wall = self._clock.wall_now()
        return wall if wall.tzinfo is UTC else wall.astimezone(UTC)


def _fingerprint(facts: dict[str, Any]) -> str:
    encoded = json.dumps(facts, sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
