"""The battery calibration cycling program — the periodic bottom-anchor traverse.

DESIGN_CALIBRATION_CYCLING (CONTRACT v1.1).  A ``CalibrationAdviser`` composed
exactly when the ``battery_calibration:`` block is PRESENT, ticking once per
fleet cycle inside the existing bounded supervision pass (no new task class),
carrying the per-civil-day frame::

    plan_local (the trigger + eligibility + selection) -> window_local
    -> the traverse (one pod, deadline-paced, the three-member stop set)
    -> the close (taper observation -> at-full hold -> the record) -> idle

What this module is, pinned (the contract's own §0/§2):

- **An ADVISER, not a schedule entry.**  The traverse submits ordinary
  short-TTL ``OPTIMIZER`` DISCHARGE intents through the facade's internal
  calibration twin under the composed principal
  ``energypod:calibration-adviser`` — judged by the arbiter, allocator,
  SafetyKernel, and actor exactly as the night adviser's charge intents are.
  Every stop is NON-RENEWAL: no stop triples, no idle intents, no zero-watt
  submissions, ever.
- **NO mode register anywhere.**  Values 2-6 of ``0x8000`` stay permanently
  prohibited; this module holds no transport, no park reach, and no write
  primitive of any kind — the anchor is provided with ORDINARY DISPATCH or it
  is not provided.
- **Measurement-first.**  A pod's first traverse is a measurement; only a
  passing re-anchor signature admits it to routine rotation, and the
  admission is derived from durable rows, never a config flag.
- **A SIBLING of the health watch, never a stage** (A14): its own window,
  its own vocabulary, its own card.  The one thing it reads from the sibling
  is a durable ``health_probe_completed`` row — one-directional, fail-defer.

The C2 retire-the-night rule is structural: at traverse start the adviser
writes the durable ``calibration_traverse_opened`` row BEFORE the first
intent; a boot that finds one uncompleted reconstructs the night as
``inconclusive_interrupted`` and NEVER resumes — a restart retires the night
unambiguously, the same-word rule as every once-per-night budget here.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from energypod.domain.audit import AuditEvent
from energypod.domain.intents import Direction
from energypod.domain.observations import UnitLifecycle

# The composed automation principal (composition supplies the real principal
# for the facade twin; audit rows under this subject are the calibration
# adviser's own).
CALIBRATION_ADVISER_PRINCIPAL: Final[str] = "energypod:calibration-adviser"
_CALIBRATION_POLICY_VERSION: Final[str] = "calibration-1"

#: The facade twin's pinned intent-id prefix: an OPTIMIZER intent carrying it
#: is this adviser's own held intent (C3 — never a foreign claim to skip on).
_OWN_INTENT_PREFIX: Final[str] = "cal-"

# The one pinned sentence (§0), verbatim on every surface this feature adds.
PINNED_SENTENCE: Final[str] = (
    "Partial cycles anchor nothing — the floor is the anchor, and the anchor "
    "is a floor, not a finish line: the traverse stops at it and never "
    "beneath it."
)
# C16: the standing stop route, named wherever traverse styling renders — no
# new kill switch exists or is wanted; the operator's existing authority (a
# manual claim preempts instantly) is the route.
STOP_ROUTE_SENTENCE: Final[str] = (
    "to stop tonight's traverse, claim the pod — any manual command preempts "
    "instantly"
)
# §6.2's honesty clause: the Modbus inventory exposes no SoC-calibration event
# register, so the program says so on every surface that would otherwise
# imply it and instruments the observable deltas instead.
CALIBRATION_EVENT_HONESTY_NOTE: Final[str] = (
    "no SoC-calibration event register exists in the read inventory — the "
    "program instruments the observable deltas (the system-vs-BMS pair, the "
    "SoC word's discontinuities, the cell spread at the anchors); the BMU "
    "event log remains the operator's on-device cross-check"
)

CALIBRATION_EVENT_TYPE: Final[str] = "calibration.cycle"
CALIBRATION_NOT_COMMISSIONED: Final[str] = "calibration_not_commissioned"

# §9's severity tiers (the health watch's own words).
TIER_NOTICE: Final[str] = "notice"
TIER_ALERT: Final[str] = "alert"
TIER_RESOLVED: Final[str] = "resolved"

# The traverse verdict vocabulary (§4.4).
VERDICT_FLOOR_REACHED: Final[str] = "floor_reached"
VERDICT_FLOOR_MISS_ENERGY: Final[str] = "floor_miss_energy_bound"
VERDICT_FLOOR_MISS_DEADLINE: Final[str] = "floor_miss_deadline"
VERDICT_PREEMPTED: Final[str] = "inconclusive_preempted"
VERDICT_INTERRUPTED: Final[str] = "inconclusive_interrupted"

# The top-anchor attribution words (§5.2/§5.4) and the graduation words (§6.3).
TOP_ANCHOR_MISSED_SOLAR: Final[str] = "top_anchor_missed_solar"
TAPER_NEVER_OBSERVED: Final[str] = "taper_never_observed"
GRADUATION_ANCHORED: Final[str] = "anchored"
GRADUATION_NOT_OBSERVED: Final[str] = "reanchor_not_observed"

# The eligibility classes (§3.2) — derived from data, never unit ids.
CLASS_EXCLUDED_CYCLES: Final[str] = "excluded_cycles_daily"
CLASS_ELIGIBLE: Final[str] = "eligible"
CLASS_DEFERRED_PROBE: Final[str] = "deferred_probe_required"
CLASS_NO_CONTROL_EVIDENCE: Final[str] = "no_control_evidence"
CLASS_NOT_DUE: Final[str] = "not_due"
CLASS_EVIDENCE_SHORT: Final[str] = "evidence_short"
KIND_MEASUREMENT: Final[str] = "measurement"
KIND_ROUTINE: Final[str] = "routine"

# The audit-row scan bound for the durable-row derivations (the health
# watch's own bounded window: anything pushed beyond it is honestly NOT
# FOUND — the conservative direction, never a fabricated answer).
_ROW_SCAN_LIMIT: Final[int] = 1000

# The trace classes (§6.1): the SoC word's behavior across the traverse.
TRACE_MONOTONE: Final[str] = "monotone"
TRACE_STEPPED: Final[str] = "stepped"
TRACE_FROZEN: Final[str] = "frozen"
# A word that never moves this far while energy flows is frozen (metering
# noise sits far beneath it; the true mid pathology sits at exactly zero).
_TRACE_FROZEN_BAND_PCT: Final[float] = 0.5
# A single-sample move at or beyond this size is a STEP (the resync class).
_TRACE_STEP_PCT: Final[float] = 2.0

_CLAIMING_SOURCES: Final[frozenset[str]] = frozenset(
    {"manual", "agent", "schedule", "optimizer", "emergency_stop"}
)
_PREEMPTING_SOURCES: Final[frozenset[str]] = frozenset({"manual", "agent", "emergency_stop"})

Phase = str  # idle | planned | traversing | closing | holding | complete

AdviseMode = str  # "advise" | "act"


class CalibrationRefusal(Exception):
    """The status surface refused a read (the block-presence doctrine)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if not code or code != code.strip():
            raise ValueError("refusal code must be non-empty and normalized")
        self.code = code
        self.message = message


# --- settings -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationTriggerSettings:
    """§3's trigger and eligibility knobs (the ``trigger:`` block)."""

    trigger_floor_pct: float = 30.0
    trigger_after_days: int = 60
    eligibility_window_days: int = 14
    cycles_daily_floor_pct: float = 25.0
    cycles_daily_min_days: int = 5
    min_daily_throughput_wh: float = 1000.0
    throughput_min_days: int = 3
    probe_pass_window_days: int = 7


@dataclass(frozen=True, slots=True)
class CalibrationTraverseSettings:
    """§4's traverse keys (the ``traverse:`` block)."""

    floor_pct: float = 10.0
    discharge_w: int = 800
    min_discharge_w: int = 200
    intent_ttl_s: float = 10.0
    assumed_delivery_frac: float = 0.8
    integration_max_gap_s: float = 10.0
    energy_margin_wh: float = 100.0
    metering_allowance_wh: float = 100.0
    assumed_capacity_wh: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CalibrationTopAnchorSettings:
    """§5's top-anchor observation keys (the ``top_anchor:`` block)."""

    taper_deadline_local: time = time(12, 0)
    taper_soc_pct: float = 99.0
    taper_limit_w: int = 0
    taper_sustain_s: int = 600
    hold_min_s: int = 2700
    hold_float_w: int = 100
    poor_surplus_kwh: float = 3.0


@dataclass(frozen=True, slots=True)
class CalibrationMeasurementSettings:
    """§6's measurement keys (the ``measurement:`` block)."""

    reanchor_delta_pct: float = 2.0
    floor_epsilon_pct: float = 2.0


@dataclass(frozen=True, slots=True)
class CalibrationSettings:
    """Every behavioural key of the ``battery_calibration:`` block."""

    timezone: str
    mode: AdviseMode = "advise"
    window_local: time = time(15, 0)
    traverse_end_local: time = time(22, 30)
    plan_local: time = time(14, 0)
    request_measurement: tuple[str, str] | None = None  # (unit_id, note)
    trigger: CalibrationTriggerSettings = CalibrationTriggerSettings()
    traverse: CalibrationTraverseSettings = CalibrationTraverseSettings()
    top_anchor: CalibrationTopAnchorSettings = CalibrationTopAnchorSettings()
    measurement: CalibrationMeasurementSettings = CalibrationMeasurementSettings()
    unit_ids: tuple[str, ...] = ()


# --- ports ----------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class _ObservationsPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentsPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class _SubmitPort(Protocol):
    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> Any: ...


class _HistoryPort(Protocol):
    """The historian read port (the load-baseline pattern: the application
    layer imports no adapter)."""

    def rollup_hours(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]: ...

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]: ...


class _AuditPort(Protocol):
    async def append(self, event: Any) -> None: ...

    async def recent(
        self, *, limit: int, after_sequence: int | None = None
    ) -> tuple[Any, ...]: ...


class _BusPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


# --- small helpers --------------------------------------------------------------


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_text(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _finite(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


def _at_most(value: float | None, bound: float) -> bool:
    """A None-safe ``<=``: an absent figure never compares (an empty pack's
    0.0 is a REAL reading, not an absence)."""
    return value is not None and value <= bound


def _at_least(value: float | None, bound: float) -> bool:
    """A None-safe ``>=`` with the same absence rule."""
    return value is not None and value >= bound


def _word(raw: Any) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return int(raw)
    return None


def _second_of_day(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


# --- the pure arithmetic (exported for the named tests) -------------------------


def deadline_rate_w(
    *,
    soc_pct: float,
    floor_pct: float,
    capacity_wh: float,
    remaining_s: float,
    cap_w: int,
    min_w: int,
) -> tuple[int, bool]:
    """The §4.2 deadline rate — the night adviser's ``even_rate_w`` with the
    direction flipped and the target set to the floor.

    Returns ``(rate_w, at_risk)``: ``required_w = ceil((soc - floor)/100 x
    capacity x 3600 / remaining_s)`` recomputed from MEASURED SoC every tick,
    clamped to ``[min_w, cap_w]``.  The floor clamp is the sensing-clear
    minimum (a commanded rate inside the ~50 W/module band can meter as 0 W);
    the ceil carries the same floating-point tolerance as its night twin.
    """
    if remaining_s <= 0:
        return int(cap_w), True
    exact = (soc_pct - floor_pct) / 100.0 * float(capacity_wh) * 3600.0 / float(remaining_s)
    required = math.ceil(exact - 1e-9)
    rate = min(max(required, int(min_w)), int(cap_w))
    return rate, required > int(cap_w)


def energy_bound_wh(
    *, start_soc_pct: float, floor_pct: float, capacity_wh: float, margin_wh: float
) -> float:
    """§4.3's co-computed lying-word bound: the energy the anchor needs plus
    the margin — the traverse stops at this many delivered Wh whatever the
    SoC word says."""
    return (start_soc_pct - floor_pct) / 100.0 * float(capacity_wh) + float(margin_wh)


def sum_rule_headroom_wh(
    *, floor_pct: float, capacity_wh: float, margin_wh: float, allowance_wh: float
) -> float:
    """C8's SUM sizing: the band edge minus the margin+allowance sum.  A
    non-negative result keeps the frozen-word stop inside the science's band
    even behind an undercounting meter; negative is the refused direction."""
    return (floor_pct - 5.0) / 100.0 * float(capacity_wh) - (
        float(margin_wh) + float(allowance_wh)
    )


@dataclass(frozen=True, slots=True)
class DayFigures:
    """One unit's per-civil-date figures over the hourly rollups (§3.1/§3.2).

    A date whose rollups are missing or degraded is EXCLUDED from every
    figure (never interpolated): only usable rollups — a ``good`` worst
    quality with a served BMS SoC figure — contribute.
    """

    soc_min_pct: float | None = None
    throughput_wh: float | None = None


@dataclass(frozen=True, slots=True)
class TriggerVector:
    """§3.1's per-unit vector, the C1 arithmetic exactly.

    ``days_since_deep`` is the HORIZON AGE for a never-deep pod (flagged
    ``horizon_bounded`` — an honest lower bound); ``None`` exists nowhere in
    the due arithmetic.  ``evidence_short`` is the one unjudgeable state: the
    historian horizon younger than N defers the night, never a verdict.
    """

    horizon_days: int
    last_deep_date: date | None
    days_since_deep: int
    horizon_bounded: bool
    evidence_short: bool
    due: bool


def trigger_vector(
    daily: Mapping[date, DayFigures], *, today: date, trigger_floor_pct: float, after_days: int
) -> TriggerVector:
    """The §3.1 computation over one unit's per-date figures.

    ``daily`` holds the unit's judgeable dates (the caller already excluded
    missing/degraded ones).  ``horizon_days`` runs from the earliest
    judgeable date; a never-deep pod is due on its HORIZON AGE once the
    horizon is judgeable (C1's blocker fix) — never ineligible by absence.
    """
    judgeable = sorted(daily)
    if not judgeable:
        return TriggerVector(0, None, 0, False, True, False)
    horizon_days = (today - judgeable[0]).days
    if horizon_days < after_days:
        return TriggerVector(horizon_days, None, 0, False, True, False)
    deep_dates = [
        day
        for day in judgeable
        if _at_most(daily[day].soc_min_pct, trigger_floor_pct)
    ]
    if deep_dates:
        last_deep = deep_dates[-1]
        return TriggerVector(
            horizon_days,
            last_deep,
            (today - last_deep).days,
            False,
            False,
            True,
        )
    return TriggerVector(horizon_days, None, horizon_days, True, False, True)


def trace_class(samples: Sequence[float]) -> str:
    """§6.1's SoC-word trace class across the traverse.

    ``frozen``: the word never moved beyond the noise band (the mid
    pathology — §4.3's energy-bound headline).  ``stepped``: the movement is
    dominated by jumps at or beyond the step size (the resync signature).
    ``monotone``: the word moved steadily.  A trace that never spoke is
    ``frozen`` — a word that never moved never anchored.
    """
    if len(samples) < 2:
        return TRACE_FROZEN
    if max(samples) - min(samples) < _TRACE_FROZEN_BAND_PCT:
        return TRACE_FROZEN
    steps = [abs(b - a) for a, b in itertools.pairwise(samples)]
    jumps = sum(1 for step in steps if step >= _TRACE_STEP_PCT)
    fine = sum(1 for step in steps if 0.0 < step < _TRACE_STEP_PCT)
    return TRACE_STEPPED if jumps > fine else TRACE_MONOTONE


def graduation(
    *,
    trace: str,
    delta_change_pct: float | None,
    delta_quality_ok: bool,
    verdict: str,
    late_step_pct: float | None,
    floor_pct: float,
    floor_epsilon_pct: float,
    taper_observed: bool,
    attribution: str | None,
) -> tuple[bool, str | None]:
    """§6.3's signature: ALL four members, or the stand-down.

    (a) the word moved (never ``frozen``); (b) a discontinuity or a material
    delta change — QUALITY-GATED (C10: both endpoints' system-word samples
    GOOD and fresh, or (b) cannot be satisfied); (c) the anchor was DELIVERED
    — EITHER stop member (C4: ``floor_reached``, or the energy-bound stop
    followed within the close by a word step landing at or below
    ``floor + epsilon``); (d) the taper landed — with C5's semantics
    (``top_anchor_missed_solar`` SATISFIES, ``taper_never_observed`` FAILS).
    Returns ``(anchored, failed_member)``.
    """
    if trace == TRACE_FROZEN:
        return False, "a"
    if delta_change_pct is None or not delta_quality_ok:
        return False, "b"
    delivered = verdict == VERDICT_FLOOR_REACHED or (
        verdict == VERDICT_FLOOR_MISS_ENERGY
        and late_step_pct is not None
        and late_step_pct <= floor_pct + floor_epsilon_pct
    )
    if not delivered:
        return False, "c"
    if not (taper_observed or attribution == TOP_ANCHOR_MISSED_SOLAR):
        return False, "d"
    return True, None


def cycle_economics_cents(
    *,
    discharge_kwh: float,
    refill_wh: float,
    charge_efficiency: float,
    import_cents_per_kwh: float,
    offpeak_cents_per_kwh: float,
    export_cents_per_kwh: float,
) -> dict[str, float]:
    """§5.1's economics, BOTH branches carried (C7): the house-absorbed
    branch displaces general-usage imports; the fully-exported quiet-house
    worst case receives the feed-in rate against the same refill — a trivial
    but real minus, carried honestly rather than waved at."""
    refill_kwh = float(refill_wh) / 1000.0 / max(float(charge_efficiency), 1e-6)
    refill_cost = refill_kwh * float(offpeak_cents_per_kwh)
    displaced = float(discharge_kwh) * float(import_cents_per_kwh)
    exported = float(discharge_kwh) * float(export_cents_per_kwh)
    return {
        "displaced_import_cents": round(displaced, 1),
        "exported_receipt_cents": round(exported, 1),
        "refill_cost_cents": round(refill_cost, 1),
        "net_house_absorbed_cents": round(displaced - refill_cost, 1),
        "net_fully_exported_cents": round(exported - refill_cost, 1),
    }


def cycle_tier(verdict: str) -> str:
    """§8's event tiers: alert on every ``floor_miss_*``, notice otherwise."""
    return TIER_ALERT if verdict.startswith("floor_miss") else TIER_NOTICE


def close_tier(graduation_word: str, attribution: str | None) -> str:
    """§8's close tiers: a failed signature and the pod-refused top anchor
    are alert tier; the solar attribution is the sky's account, a notice."""
    if graduation_word == GRADUATION_NOT_OBSERVED:
        return TIER_ALERT
    if attribution == TAPER_NEVER_OBSERVED:
        return TIER_ALERT
    return TIER_NOTICE


def rollup_daily(
    rollups: Sequence[Any],
    *,
    unit_id: str,
    zone: ZoneInfo,
) -> dict[date, DayFigures]:
    """Group one unit's hourly rollups into per-civil-date figures.

    Only USABLE rollups contribute (a ``good`` worst quality with the BMS SoC
    minimum served): a date whose rollups are missing or degraded is
    excluded from the scan, never interpolated (§3.1).  Daily throughput is
    |mean watts| integrated over each covered hour.
    """
    soc_mins: dict[date, list[float]] = {}
    throughput: dict[date, list[float]] = {}
    for rollup in rollups:
        if str(getattr(rollup, "unit_id", "")) != unit_id:
            continue
        if str(getattr(rollup, "worst_quality", "")) != "good":
            continue
        metrics = getattr(rollup, "metrics", None)
        if not isinstance(metrics, Mapping):
            continue
        soc_triple = metrics.get("bms_soc_pct")
        watts_triple = metrics.get("battery_watts")
        if not isinstance(soc_triple, tuple):
            continue
        soc_min = _finite(soc_triple[0]) if soc_triple else None
        if soc_min is None:
            continue
        local_day = rollup.hour_start.astimezone(zone).date()
        soc_mins.setdefault(local_day, []).append(soc_min)
        # The metric triple is (min, max, mean) — the MEAN watts integrated
        # over the covered hour is the day's throughput share.
        if isinstance(watts_triple, tuple) and _finite(watts_triple[2]) is not None:
            throughput.setdefault(local_day, []).append(abs(float(watts_triple[2])))
    daily: dict[date, DayFigures] = {}
    for day, mins in soc_mins.items():
        daily[day] = DayFigures(
            soc_min_pct=min(mins),
            throughput_wh=(
                sum(throughput.get(day, ())) if throughput.get(day) else None
            ),
        )
    return daily


# --- the per-night records --------------------------------------------------------


@dataclass(slots=True)
class _UnitPlan:
    """One unit's plan-tick row: the §3 vector plus its class figures."""

    unit_id: str
    vector: TriggerVector
    klass: str = CLASS_ELIGIBLE
    throughput_wh_mean: float | None = None
    throughput_days: int = 0
    sub_floor_dates: int = 0
    probe_verdict: str | None = None
    probe_at: str | None = None
    kind: str = KIND_MEASUREMENT
    anchored: bool = False
    standdown: bool = False


@dataclass(slots=True)
class _TraverseLeg:
    """One in-flight traverse (§4), one tick at a time."""

    unit_id: str
    night: date
    opened_at: datetime
    start_soc_bms: float
    start_soc_system: float | None
    start_system_quality_good: bool
    energy_bound_wh: float
    energy_wh: float = 0.0
    last_sample_mono: float | None = None
    soc_trace: list[float] = field(default_factory=list)
    jumps: list[dict[str, Any]] = field(default_factory=list)
    rates: list[int] = field(default_factory=list)
    at_risk: bool = False
    intent_id: str | None = None
    verdict: str | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(slots=True)
class _CloseState:
    """The §5 close: the taper observation, the hold, the graduation.

    Created at traverse OPEN (the figures the close needs are the traverse's
    own opening words) and surviving the civil rollover by design — the
    taper deadline is a MORNING fact up to a civil day after the traverse
    that opened it (§8).
    """

    night: date
    unit_id: str
    kind: str
    started_at: datetime
    verdict: str = "pending"
    trace_class_word: str = TRACE_FROZEN
    spread_before_mv: float | None = None
    start_delta_pct: float | None = None
    start_quality_good: bool = False
    taper_since_mono: float | None = None
    taper_observed_at: datetime | None = None
    taper_ccl_w: float | None = None
    hold_started_mono: float | None = None
    hold_interrupted: dict[str, Any] | None = None
    hold_completed: bool = False
    late_step_pct: float | None = None
    last_soc: float | None = None
    attribution: str | None = None
    morning_surplus_kwh: float | None = None
    finished: bool = False


@dataclass(frozen=True, slots=True)
class _ClaimView:
    manual: frozenset[str]
    agent: frozenset[str]
    schedule: frozenset[str]
    optimizer: frozenset[str]


# --- the adviser -----------------------------------------------------------------


class CalibrationAdviser:
    """The calibration program: plan, traverse, close — one tick at a time.

    Ticks once per fleet cycle, fully suppressed by the supervision pass: a
    failure inside it is a missed night's evidence, never a delay to control.
    Holds no transport of its own — the traverse's intent submission is the
    one dispatch act, through the facade's internal calibration twin.  In the
    ``advise`` posture the whole program runs and renders and submits
    NOTHING, ever, on any tick, under any input (§7's named test).
    """

    def __init__(
        self,
        *,
        settings: CalibrationSettings,
        policy: Any,
        clock: Clock,
        observations: _ObservationsPort,
        intents: _IntentsPort,
        submit: _SubmitPort,
        history: _HistoryPort,
        audit: _AuditPort,
        bus: _BusPort,
        health_states: Callable[[], Awaitable[Mapping[str, Any]]],
        health_watch_stages: Callable[[], tuple[str, ...]],
        parked_units: Callable[[], frozenset[str]] | None = None,
        latched_stop_units: Callable[[], frozenset[str]] | None = None,
        tariff: Mapping[str, float] | None = None,
        night_window_end_local: time | None = None,
        process_instance_id: str = "",
    ) -> None:
        units = tuple(settings.unit_ids)
        if not units:
            raise ValueError("unit_ids must not be empty")
        self._settings = settings
        self._policy = policy
        self._clock = clock
        self._observations = observations
        self._intents = intents
        self._submit = submit
        self._history = history
        self._audit = audit
        self._bus = bus
        self._health_states = health_states
        self._health_watch_stages = health_watch_stages
        self._parked_units = parked_units
        self._latched_stop_units = latched_stop_units
        self._tariff = dict(tariff) if tariff is not None else None
        self._night_window_end = night_window_end_local
        self._process_instance_id = process_instance_id
        self._zone = ZoneInfo(settings.timezone)
        # Per-civil-day state (reset on rollover; rows are the truth).
        self._night: date | None = None
        self._plans: dict[str, _UnitPlan] = {}
        self._target: str | None = None
        self._target_reason: str | None = None
        self._due_waived: str | None = None
        self._planned_date: date | None = None
        self._request_consumed: tuple[str, str] | None = None
        self._night_retired = False
        self._leg: _TraverseLeg | None = None
        self._close: _CloseState | None = None
        self._last_cycle: dict[str, Any] | None = None
        self._morning: dict[str, Any] | None = None
        # Durable-row derivations, boot-loaded and refreshed as rows land.
        self._anchored: frozenset[str] = frozenset()
        self._standdown: frozenset[str] = frozenset()
        self._boot_reconstructed = False

    # --- the fleet-loop tick --------------------------------------------------

    async def tick(self) -> None:
        """Advance the program by one bounded step; never raises."""
        try:
            await self._tick()
        except Exception as error:
            print(f"SUPERVISED CALIBRATION TICK FAILURE: {error!r}", flush=True)

    async def _tick(self) -> None:
        wall = self._clock.wall_now()
        local = wall.astimezone(self._zone)
        today = local.date()
        await self._roll_day(today, wall)
        second = _second_of_day(local.timetz().replace(tzinfo=None))
        if self._planned_date != today and second >= _second_of_day(self._settings.plan_local):
            await self._run_plan(wall, today)
        if self._leg is not None:
            await self._advance_leg(wall, local)
            return
        if self._close is not None:
            if not self._close.finished:
                await self._advance_close(wall, local)
            return
        # Window open: nothing opens a leg unless the plan named a target,
        # the night is not retired, and the posture is ACT — advise plans the
        # traverse and says so, still no intent (§10 step 1).
        if (
            second >= _second_of_day(self._settings.window_local)
            and second < _second_of_day(self._settings.traverse_end_local)
            and self._target is not None
            and not self._night_retired
            and self._settings.mode == "act"
        ):
            await self._open_leg(wall, today)
            if self._leg is not None:
                # The open tick carries the first renewal too: the open row
                # landed BEFORE the first intent (C2), then the traverse
                # begins on this same tick.
                await self._advance_leg(wall, local)
            return

    async def _roll_day(self, today: date, wall: datetime) -> None:
        """Reset the per-civil-day plan state on rollover.

        The CLOSE survives the rollover by design (§8: the taper row lands
        up to a civil day after its traverse).  A leg still alive at rollover
        is retired interrupted — the window validation makes this unreachable
        (the traverse end lands before every sibling window) and honesty
        costs one branch.
        """
        if self._night == today:
            return
        stale_leg = self._leg
        self._night = today
        self._plans = {}
        self._target = None
        self._target_reason = None
        self._due_waived = None
        self._night_retired = False
        if stale_leg is not None:
            self._leg = stale_leg
            with contextlib.suppress(Exception):
                await self._finish_leg(
                    wall, stale_leg, VERDICT_INTERRUPTED, ("civil_day_rolled",)
                )

    # --- §3: the plan tick ------------------------------------------------------

    async def _run_plan(self, wall: datetime, today: date) -> None:
        """The once-per-civil-day plan: trigger vector, classes, selection."""
        self._planned_date = today
        trigger = self._settings.trigger
        scan_from = datetime.combine(
            today - timedelta(days=trigger.trigger_after_days + 90),
            time(0, 0),
            tzinfo=self._zone,
        )
        rollups: tuple[Any, ...] = ()
        with contextlib.suppress(Exception):
            rollups = tuple(
                self._history.rollup_hours(
                    self._settings.unit_ids,
                    scan_from.astimezone(UTC) - timedelta(days=1),
                    wall.astimezone(UTC),
                )
            )
        probe_rows = await self._recent_passing_probes(today)
        anchored, standdown = await self._derive_durable_facts()
        self._anchored, self._standdown = anchored, standdown
        request = self._settings.request_measurement
        consumed_prior = await self._request_already_consumed(request)
        selected: str | None = None
        reason: str | None = None
        due_waived: str | None = None
        plans: dict[str, _UnitPlan] = {}
        for unit_id in self._settings.unit_ids:
            daily = rollup_daily(rollups, unit_id=unit_id, zone=self._zone)
            vector = trigger_vector(
                daily,
                today=today,
                trigger_floor_pct=trigger.trigger_floor_pct,
                after_days=trigger.trigger_after_days,
            )
            window_start = today - timedelta(days=trigger.eligibility_window_days - 1)
            window_days = [day for day in sorted(daily) if window_start <= day <= today]
            sub_floor = sum(
                1
                for day in window_days
                if _at_most(daily[day].soc_min_pct, trigger.cycles_daily_floor_pct)
            )
            throughput_days = [
                day
                for day in window_days
                if _at_least(daily[day].throughput_wh, trigger.min_daily_throughput_wh)
            ]
            throughput_mean = (
                sum(daily[day].throughput_wh or 0.0 for day in throughput_days)
                / len(throughput_days)
                if throughput_days
                else None
            )
            plan = _UnitPlan(
                unit_id=unit_id,
                vector=vector,
                sub_floor_dates=sub_floor,
                throughput_wh_mean=throughput_mean,
                throughput_days=len(throughput_days),
                anchored=unit_id in anchored,
                standdown=unit_id in standdown,
            )
            plan.probe_verdict, plan.probe_at = probe_rows.get(unit_id, (None, None))
            plan.klass = self._classify(plan)
            plan.kind = KIND_ROUTINE if plan.anchored else KIND_MEASUREMENT
            plans[unit_id] = plan
        # Selection (§3.3): the one-shot overrides SELECTION ONLY — the
        # waiver is of the `due` requirement, and `evidence_short` is that
        # requirement's unjudgeability (§3.1), so the named unit's class is
        # derived with the waiver in force (§10 step 0: mid-first
        # commissioning is blocked only until this one-shot exists).  Every
        # §3.2 class gate still applies on the evidence the window holds —
        # the lhs-exclusion and the rhs-class probe ladder defer verbatim.
        if request is not None and not consumed_prior:
            named_plan = plans.get(request[0])
            if named_plan is not None:
                named_plan.klass = self._classify(named_plan, due_waived=True)
            if named_plan is None:
                reason = "request_unit_not_eligible"
            elif named_plan.klass in {
                CLASS_DEFERRED_PROBE,
                CLASS_NO_CONTROL_EVIDENCE,
                CLASS_EXCLUDED_CYCLES,
            }:
                reason = named_plan.klass
            else:
                selected = request[0]
                due_waived = "request_measurement"
                self._request_consumed = request
        if selected is None:
            eligible = [
                plan
                for plan in plans.values()
                if plan.vector.due and plan.klass == CLASS_ELIGIBLE
            ]
            if eligible:
                best = min(eligible, key=lambda plan: (-plan.vector.days_since_deep, plan.unit_id))
                selected = best.unit_id
            else:
                ordered = sorted(plans.values(), key=lambda plan: plan.unit_id)
                due_classes = [
                    plan.klass
                    for plan in ordered
                    if plan.vector.due and plan.klass != CLASS_ELIGIBLE
                ]
                if due_classes:
                    # The honest none-reason: the DUE units' own class word
                    # (a due-but-deferred fleet says deferred, never a
                    # blanket silence).
                    reason = reason or due_classes[0]
                elif any(plan.vector.evidence_short for plan in ordered):
                    # The §10 quiescence: nobody is judgeable yet.
                    reason = reason or "evidence_short"
                else:
                    reason = reason or "not_due"
        self._plans = plans
        self._target = selected
        self._target_reason = None if selected is not None else (reason or "not_due")
        self._due_waived = due_waived
        if await self._night_has_rows(today):
            # The once-per-night budget, derived from durable rows: a night
            # with a traverse row is retired before this process ever opened
            # one (a restart never re-budgets the night).
            self._night_retired = True
            self._target = None
            self._target_reason = "night_retired"
        payload = {
            "night": today.isoformat(),
            "mode": self._settings.mode,
            "units": [
                self._plan_row(plan)
                for plan in sorted(plans.values(), key=lambda plan: plan.unit_id)
            ],
            "selected": selected,
            "reason": None if selected is not None else (reason or "not_due"),
            "due_waived": due_waived,
            "request_measurement": (
                None
                if request is None
                else {
                    "unit": request[0],
                    "note": request[1],
                    "consumed": self._request_consumed == request and not consumed_prior,
                    "consumed_prior": consumed_prior,
                }
            ),
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.traverse_end_local.strftime("%H:%M"),
            },
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._calibration_row(
                event_type="calibration_trigger_evaluated",
                unit_id=None,
                reason_codes=() if selected else (str(payload["reason"]),),
                result="evaluated",
                payload=payload,
            )
        )

    def _classify(self, plan: _UnitPlan, *, due_waived: bool = False) -> str:
        """§3.2's class derivation, in evaluation order — from data, not ids.

        ``due_waived`` is the one-shot's lens (§3.3/§10 step 0): the waiver
        is of the ``due`` requirement, and ``evidence_short`` is §3.1's
        unjudgeability OF ``due`` — so the waiver subsumes it and the class
        gates judge on the evidence the window holds.  The lhs-exclusion and
        the rhs-class probe ladder stand exactly as written either way: a
        young window is not a refused window (the exclusion sees fewer
        dates and cannot exclude — the conservative direction; the
        throughput ladder counts the dates that exist).
        """
        trigger = self._settings.trigger
        if plan.sub_floor_dates >= trigger.cycles_daily_min_days:
            return CLASS_EXCLUDED_CYCLES
        if not due_waived:
            if plan.vector.evidence_short:
                return CLASS_EVIDENCE_SHORT
            if not plan.vector.due:
                return CLASS_NOT_DUE
        if plan.throughput_days >= trigger.throughput_min_days:
            return CLASS_ELIGIBLE
        # Throughput evidence absent: the rhs-class interlock — a passing
        # probe row inside the window UNLOCKS (C11); a missing watch defers
        # as no_control_evidence, both defer, neither guesses.
        if plan.probe_verdict == "pass":
            return CLASS_ELIGIBLE
        if "probe" not in self._health_watch_stages():
            return CLASS_NO_CONTROL_EVIDENCE
        return CLASS_DEFERRED_PROBE

    def _plan_row(self, plan: _UnitPlan) -> dict[str, Any]:
        vector = plan.vector
        row: dict[str, Any] = {
            "unit_id": plan.unit_id,
            "class": plan.klass,
            "days_since_deep": vector.days_since_deep,
            "horizon_bounded": vector.horizon_bounded,
            "last_deep_date": (
                None if vector.last_deep_date is None else vector.last_deep_date.isoformat()
            ),
            "horizon_days": vector.horizon_days,
            "due": vector.due,
            "evidence_short": vector.evidence_short,
            "kind": plan.kind,
            "anchored": plan.anchored,
            "standdown": plan.standdown,
        }
        if plan.klass == CLASS_EXCLUDED_CYCLES:
            row[f"sub_floor_dates_{self._settings.trigger.eligibility_window_days}d"] = (
                plan.sub_floor_dates
            )
        if plan.throughput_wh_mean is not None:
            row["throughput_wh_mean"] = round(plan.throughput_wh_mean, 1)
        if plan.probe_verdict is not None:
            row["probe_verdict"] = plan.probe_verdict
            row["probe_at"] = plan.probe_at
        return row

    # --- §4: the traverse ------------------------------------------------------

    async def _open_leg(self, wall: datetime, today: date) -> None:
        """Window open: re-check the gates, write the open row, then submit."""
        target = self._target
        assert target is not None  # the caller's guard is the call site's
        plan = self._plans.get(target)
        if plan is not None and plan.standdown:
            # §6.3: a stood-down pod never cycles until the acknowledgement.
            self._target = None
            self._target_reason = "standdown"
            return
        now_mono = float(self._clock.monotonic())
        latest = await self._observations.all_latest()
        active = await self._intents.active(now_mono)
        observation = latest.get(target)
        skip = await self._skip_reason(
            target, observation, active=active, now_mono=now_mono, plan=plan
        )
        soc = _finite(getattr(observation, "authoritative_soc_pct", None))
        if skip is not None:
            self._target = None
            self._target_reason = f"skipped:{skip}"
            await self._record_skipped(wall, today, target, skip)
            return
        if soc is None:
            self._target = None
            self._target_reason = "skipped:telemetry_stale"
            await self._record_skipped(wall, today, target, "telemetry_stale")
            return
        system = _finite(getattr(observation, "system_soc_pct", None))
        quality = getattr(observation, "quality", None)
        system_good = (
            isinstance(quality, Mapping) and str(quality.get("system_soc_pct", "")) == "good"
        )
        capacity = self._capacity_wh(target)
        spread = self._latest_spread_mv(observation)
        kind = plan.kind if plan is not None else KIND_MEASUREMENT
        # C2: the durable open row lands BEFORE the first intent — a boot
        # that finds it uncompleted reconstructs, never resumes.
        await self._append_row(
            self._calibration_row(
                event_type="calibration_traverse_opened",
                unit_id=target,
                reason_codes=("window_open",),
                result="opened",
                payload={
                    "night": today.isoformat(),
                    "kind": kind,
                    "start_soc_bms_pct": round(soc, 2),
                    "start_soc_system_pct": None if system is None else round(system, 2),
                    "start_system_quality_good": system_good,
                    "energy_bound_wh": round(
                        energy_bound_wh(
                            start_soc_pct=soc,
                            floor_pct=float(self._settings.traverse.floor_pct),
                            capacity_wh=capacity,
                            margin_wh=float(self._settings.traverse.energy_margin_wh),
                        ),
                        1,
                    ),
                    "floor_pct": self._settings.traverse.floor_pct,
                    "window": {
                        "opens_local": self._settings.window_local.strftime("%H:%M"),
                        "ends_local": self._settings.traverse_end_local.strftime("%H:%M"),
                    },
                    "assumed_capacity_wh": capacity,
                    "cell_spread_mv_before": spread,
                    "as_of": wall.astimezone(UTC).isoformat(),
                },
            )
        )
        self._leg = _TraverseLeg(
            unit_id=target,
            night=today,
            opened_at=wall,
            start_soc_bms=soc,
            start_soc_system=system,
            start_system_quality_good=system_good,
            energy_bound_wh=energy_bound_wh(
                start_soc_pct=soc,
                floor_pct=float(self._settings.traverse.floor_pct),
                capacity_wh=capacity,
                margin_wh=float(self._settings.traverse.energy_margin_wh),
            ),
        )
        self._leg.soc_trace.append(soc)
        self._close = _CloseState(
            night=today,
            unit_id=target,
            kind=kind,
            started_at=wall,
            spread_before_mv=spread,
            start_delta_pct=None if system is None else round(system - soc, 2),
            start_quality_good=system_good,
        )

    async def _advance_leg(self, wall: datetime, local: datetime) -> None:
        """One bounded step of the traverse: integrate, stop-set, renew."""
        leg = self._leg
        assert leg is not None
        now_mono = float(self._clock.monotonic())
        latest = await self._observations.all_latest()
        observation = latest.get(leg.unit_id)
        active = await self._intents.active(now_mono)
        second = _second_of_day(local.timetz().replace(tzinfo=None))
        # Preemption first (§4.3's tail): a MANUAL/AGENT claim or the
        # emergency stop preempts instantly — no depth credit, no re-run.
        preemption = self._preempted_by(active, leg.unit_id)
        if preemption is not None:
            await self._finish_leg(
                wall, leg, VERDICT_PREEMPTED, (f"preempted_by_{preemption}",)
            )
            return
        soc = _finite(getattr(observation, "authoritative_soc_pct", None))
        watts = _finite(getattr(observation, "battery_watts", None))
        captured = _finite(getattr(observation, "captured_at_mono", None))
        # C9's integration discipline: the LIVE battery_watts word at tick
        # cadence; a sample gap beyond the bound is by construction a
        # staleness event that DENIES the intent at the kernel, so the gap
        # ENDS the leg — excluded-never-interpolated, and the bound is never
        # evaluated across it.
        if leg.last_sample_mono is not None and captured is not None:
            gap = captured - leg.last_sample_mono
            if gap > float(self._settings.traverse.integration_max_gap_s):
                await self._finish_leg(wall, leg, "aborted:telemetry_lost", ("integration_gap",))
                return
            if watts is not None and gap > 0:
                # The signed coulomb book: discharge (positive) accrues the
                # anchor's energy, charge (negative) gives it back.
                leg.energy_wh += watts * gap / 3600.0
        if soc is not None:
            previous = leg.soc_trace[-1] if leg.soc_trace else None
            leg.soc_trace.append(soc)
            if previous is not None:
                delta = soc - previous
                jump_pct = float(getattr(self._policy, "maximum_soc_jump_pct", 5.0))
                if abs(delta) >= jump_pct:
                    leg.jumps.append(
                        {
                            "at": wall.astimezone(UTC).isoformat(),
                            "from_pct": round(previous, 2),
                            "to_pct": round(soc, 2),
                            "delta_pct": round(delta, 2),
                        }
                    )
        leg.last_sample_mono = captured
        # --- the stop set, ordered by construction, BEFORE submission -----
        # 1. FLOOR (the anchor): pre-submission, so the kernel's
        #    soc_below_discharge_floor never sees an at-or-below-floor intent.
        if soc is not None and soc <= float(self._settings.traverse.floor_pct):
            await self._finish_leg(wall, leg, VERDICT_FLOOR_REACHED, ("floor_reached",), soc)
            return
        # 2. ENERGY BOUND (the lying-word guard).
        if leg.energy_wh >= leg.energy_bound_wh:
            await self._finish_leg(wall, leg, VERDICT_FLOOR_MISS_ENERGY, ("energy_bound",), soc)
            return
        # 3. DEADLINE (the no-new-renewal boundary).
        if second >= _second_of_day(self._settings.traverse_end_local):
            await self._finish_leg(wall, leg, VERDICT_FLOOR_MISS_DEADLINE, ("deadline",), soc)
            return
        if soc is None or watts is None or captured is None:
            await self._finish_leg(wall, leg, "aborted:telemetry_lost", ("telemetry_stale",))
            return
        # The standing guards, re-checked mid-leg: a refusal ends the leg
        # with the guard's word, never a battery failure, never a retry.
        skip = await self._skip_reason(
            leg.unit_id,
            observation,
            active=active,
            now_mono=now_mono,
            plan=self._plans.get(leg.unit_id),
        )
        if skip is not None:
            await self._finish_leg(wall, leg, f"aborted:{skip}", (skip,))
            return
        rate, at_risk = deadline_rate_w(
            soc_pct=soc,
            floor_pct=float(self._settings.traverse.floor_pct),
            capacity_wh=self._capacity_wh(leg.unit_id),
            remaining_s=self._remaining_s(wall),
            cap_w=int(self._settings.traverse.discharge_w),
            min_w=int(self._settings.traverse.min_discharge_w),
        )
        leg.rates.append(rate)
        leg.at_risk = at_risk
        await self._renew(leg, rate)

    def _remaining_s(self, wall: datetime) -> float:
        local = wall.astimezone(self._zone)
        end = datetime.combine(local.date(), self._settings.traverse_end_local, tzinfo=self._zone)
        return max(1.0, (end - local).total_seconds())

    def _capacity_wh(self, unit_id: str) -> float:
        capacities = self._settings.traverse.assumed_capacity_wh or {}
        value = capacities.get(unit_id)
        return float(value) if isinstance(value, int) else 5000.0

    async def _renew(self, leg: _TraverseLeg, rate_w: int) -> None:
        """Renew the one held intent remove-then-submit (§2's doctrine).

        A refused submission ends the leg with the guard's word — every
        standing guard judges these intents through the ordinary path, and a
        refusal is an abort, never a failure of the battery.
        """
        await self._remove_held(leg)
        try:
            result = await self._submit(
                unit_ids=[leg.unit_id],
                direction=Direction.DISCHARGE,
                watts=int(rate_w),
                ttl_s=float(self._settings.traverse.intent_ttl_s),
            )
        except Exception:
            leg.intent_id = None
            await self._finish_leg(
                self._clock.wall_now(), leg, "aborted:dispatch_refused", ("dispatch_refused",)
            )
            return
        submitted = result.get("intent_id") if isinstance(result, Mapping) else None
        leg.intent_id = submitted if isinstance(submitted, str) else None
        if leg.intent_id is None:
            await self._finish_leg(
                self._clock.wall_now(), leg, "aborted:dispatch_refused", ("dispatch_refused",)
            )

    async def _remove_held(self, leg: _TraverseLeg) -> None:
        held = leg.intent_id
        leg.intent_id = None
        if held is None:
            return
        with contextlib.suppress(Exception):
            await self._intents.remove(held)

    async def _finish_leg(
        self,
        wall: datetime,
        leg: _TraverseLeg,
        verdict: str,
        reason_codes: tuple[str, ...],
        end_soc: float | None = None,
    ) -> None:
        """Close the traverse: withdraw, then the full §6.1 record row."""
        await self._remove_held(leg)
        latest = await self._observations.all_latest()
        observation = latest.get(leg.unit_id)
        end_soc_bms = (
            end_soc
            if end_soc is not None
            else _finite(getattr(observation, "authoritative_soc_pct", None))
        )
        end_soc_system = _finite(getattr(observation, "system_soc_pct", None))
        leg.verdict = verdict
        leg.reason_codes = reason_codes
        self._leg = None
        self._night_retired = True
        self._target = None
        plan = self._plans.get(leg.unit_id)
        kind = plan.kind if plan is not None and plan.anchored else KIND_MEASUREMENT
        trace = trace_class(leg.soc_trace)
        close = self._close
        if close is not None and close.night == leg.night and close.unit_id == leg.unit_id:
            close.verdict = verdict
            close.trace_class_word = trace
        economics = self._cycle_economics(leg)
        payload: dict[str, Any] = {
            "night": leg.night.isoformat(),
            "kind": kind,
            "verdict": verdict,
            "tier": cycle_tier(verdict),
            "trace_class": trace,
            "start": {
                "at": leg.opened_at.astimezone(UTC).isoformat(),
                "bms_soc_pct": round(leg.start_soc_bms, 2),
                "system_soc_pct": (
                    None if leg.start_soc_system is None else round(leg.start_soc_system, 2)
                ),
                "system_quality_good": leg.start_system_quality_good,
            },
            "end": {
                "at": wall.astimezone(UTC).isoformat(),
                "bms_soc_pct": None if end_soc_bms is None else round(end_soc_bms, 2),
                "system_soc_pct": (
                    None if end_soc_system is None else round(end_soc_system, 2)
                ),
            },
            "energy_wh": round(max(0.0, leg.energy_wh), 1),
            "energy_bound_wh": round(leg.energy_bound_wh, 1),
            "rates": {
                "first_w": leg.rates[0] if leg.rates else None,
                "last_w": leg.rates[-1] if leg.rates else None,
                "max_w": max(leg.rates) if leg.rates else None,
                "at_risk": leg.at_risk,
            },
            "soc_jumps": leg.jumps,
            "cell_spread_mv_before": close.spread_before_mv if close is not None else None,
            "stop_members": {
                "floor_reached": verdict == VERDICT_FLOOR_REACHED,
                "energy_bound": verdict == VERDICT_FLOOR_MISS_ENERGY,
                "deadline": verdict == VERDICT_FLOOR_MISS_DEADLINE,
            },
            "pinned_sentence": PINNED_SENTENCE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        if economics is not None:
            payload["economics_cents"] = economics
        self._last_cycle = payload
        await self._append_row(
            self._calibration_row(
                event_type="calibration_cycle_completed",
                unit_id=leg.unit_id,
                reason_codes=reason_codes or (verdict,),
                result=verdict,
                payload=payload,
            )
        )
        await self._publish_event(payload | {"unit_id": leg.unit_id}, cycle_tier(verdict))

    def _cycle_economics(self, leg: _TraverseLeg) -> dict[str, float] | None:
        """§5.1's figures, both branches (C7) — only with a tariff truth."""
        tariff = self._tariff
        if tariff is None:
            return None
        ceiling = float(getattr(self._policy, "maximum_soc_pct", 95.0))
        refill_wh = (
            (ceiling - float(self._settings.traverse.floor_pct)) / 100.0
            * self._capacity_wh(leg.unit_id)
        )
        return cycle_economics_cents(
            discharge_kwh=max(0.0, leg.energy_wh) / 1000.0,
            refill_wh=refill_wh,
            charge_efficiency=0.9,
            import_cents_per_kwh=float(tariff.get("import_cents_per_kwh", 0.0)),
            offpeak_cents_per_kwh=float(tariff.get("offpeak_cents_per_kwh", 0.0)),
            export_cents_per_kwh=float(tariff.get("export_cents_per_kwh", 0.0)),
        )

    async def _record_skipped(self, wall: datetime, night: date, unit_id: str, reason: str) -> None:
        """§3.3: a night with no eligible unit renders idle with its reason —
        never silence; the row retires the night's traverse budget."""
        self._night_retired = True
        payload = {
            "night": night.isoformat(),
            "unit_id": unit_id,
            "verdict": f"skipped:{reason}",
            "tier": TIER_NOTICE,
            "reason": reason,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._calibration_row(
                event_type="calibration_cycle_completed",
                unit_id=unit_id,
                reason_codes=(reason,),
                result="skipped",
                payload=payload,
            )
        )
        self._last_cycle = payload

    # --- §5: the close ---------------------------------------------------------

    async def _advance_close(self, wall: datetime, local: datetime) -> None:
        """The taper observation, the hold, and the close's end (§5.2/§5.3)."""
        close = self._close
        assert close is not None
        anchor = self._settings.top_anchor
        now_mono = float(self._clock.monotonic())
        deadline = datetime.combine(
            close.night + timedelta(days=1), anchor.taper_deadline_local, tzinfo=self._zone
        )
        latest = await self._observations.all_latest()
        observation = latest.get(close.unit_id)
        soc = _finite(getattr(observation, "authoritative_soc_pct", None))
        system = _finite(getattr(observation, "system_soc_pct", None))
        ccl = _finite(getattr(observation, "dynamic_charge_limit_w", None))
        watts = _finite(getattr(observation, "battery_watts", None))
        quality = getattr(observation, "quality", None)
        system_good = (
            isinstance(quality, Mapping) and str(quality.get("system_soc_pct", "")) == "good"
        )
        # C4's late-step watch: a word that steps only AFTER the energy
        # bound already stopped the traverse is the anchor's second witness.
        if (
            close.last_soc is not None
            and soc is not None
            and abs(soc - close.last_soc) >= _TRACE_STEP_PCT
            and close.late_step_pct is None
        ):
            close.late_step_pct = soc
        if soc is not None:
            close.last_soc = soc
        # The taper signature (§5.2): SoC at/above the bound AND the CCL
        # collapsed, sustained the vendor protocol's own rest class.  No
        # write of any kind occurs in this phase.
        signature = (
            soc is not None
            and ccl is not None
            and soc >= float(anchor.taper_soc_pct)
            and ccl <= float(anchor.taper_limit_w)
        )
        if signature:
            if close.taper_since_mono is None:
                close.taper_since_mono = now_mono
            elif (
                close.taper_observed_at is None
                and now_mono - close.taper_since_mono >= float(anchor.taper_sustain_s)
            ):
                close.taper_observed_at = wall
                close.taper_ccl_w = ccl
        else:
            close.taper_since_mono = None
        # The at-full hold (§5.3): observed, honestly interruptible — never
        # a write to protect it (the anchor was had at the taper).
        if close.taper_observed_at is not None and not close.hold_completed:
            inside = (
                soc is not None
                and ccl is not None
                and watts is not None
                and soc >= float(anchor.taper_soc_pct)
                and ccl <= float(anchor.taper_limit_w)
                and abs(watts) <= float(anchor.hold_float_w)
            )
            if close.hold_started_mono is None and inside:
                close.hold_started_mono = now_mono
            elif close.hold_started_mono is not None and not inside:
                held_s = now_mono - close.hold_started_mono
                close.hold_interrupted = {
                    "held_s": round(held_s, 1),
                    "fraction": round(held_s / float(anchor.hold_min_s), 3),
                    "soc_pct": None if soc is None else round(soc, 2),
                    "battery_watts": None if watts is None else round(watts, 1),
                }
                close.hold_completed = True
            elif close.hold_started_mono is not None and (
                now_mono - close.hold_started_mono >= float(anchor.hold_min_s)
            ):
                close.hold_completed = True
        if close.hold_completed or wall >= deadline:
            await self._finish_close(wall, close, soc, system, system_good)

    async def _finish_close(
        self,
        wall: datetime,
        close: _CloseState,
        soc: float | None,
        system: float | None,
        system_quality_good: bool,
    ) -> None:
        """The close's end: the attribution split, the record, graduation."""
        anchor = self._settings.top_anchor
        close.finished = True
        attribution: str | None = None
        if close.taper_observed_at is None:
            surplus = await self._morning_surplus_kwh(close)
            attribution = (
                TOP_ANCHOR_MISSED_SOLAR
                if surplus is not None and surplus < float(anchor.poor_surplus_kwh)
                else TAPER_NEVER_OBSERVED
            )
            close.attribution = attribution
        # The AFTER half of the delta instrument (§6.1) — the close's own
        # read, quality-gated by construction (C10 rides the row).
        latest = await self._observations.all_latest()
        spread_after = self._latest_spread_mv(latest.get(close.unit_id))
        delta_after = None if (system is None or soc is None) else round(system - soc, 2)
        delta_change = (
            None
            if delta_after is None or close.start_delta_pct is None
            else round(abs(delta_after - close.start_delta_pct), 2)
        )
        anchored, failed = graduation(
            trace=close.trace_class_word,
            delta_change_pct=delta_change,
            delta_quality_ok=system_quality_good and close.start_quality_good,
            verdict=close.verdict,
            late_step_pct=close.late_step_pct,
            floor_pct=float(self._settings.traverse.floor_pct),
            floor_epsilon_pct=float(self._settings.measurement.floor_epsilon_pct),
            taper_observed=close.taper_observed_at is not None,
            attribution=attribution,
        )
        word = GRADUATION_ANCHORED if anchored else GRADUATION_NOT_OBSERVED
        payload: dict[str, Any] = {
            "night": close.night.isoformat(),
            "unit_id": close.unit_id,
            "kind": close.kind,
            "verdict": close.verdict,
            "taper_observed_at": (
                None
                if close.taper_observed_at is None
                else close.taper_observed_at.astimezone(UTC).isoformat()
            ),
            "taper_ccl_w": close.taper_ccl_w,
            "hold": (
                {"completed": True, "hold_min_s": anchor.hold_min_s}
                if close.hold_completed and close.hold_interrupted is None
                else (
                    {"interrupted": close.hold_interrupted}
                    if close.hold_interrupted is not None
                    else None
                )
            ),
            "attribution": attribution,
            "morning_surplus_kwh": close.morning_surplus_kwh,
            "spread": {"before_mv": close.spread_before_mv, "after_mv": spread_after},
            "delta_pct": {
                "before": close.start_delta_pct,
                "after": delta_after,
                "change": delta_change,
                "quality_gated": system_quality_good and close.start_quality_good,
            },
            "late_step_pct": close.late_step_pct,
            "graduation": word,
            "graduation_failed_member": failed,
            "calibration_event_note": CALIBRATION_EVENT_HONESTY_NOTE,
            "stop_route": STOP_ROUTE_SENTENCE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        tier = close_tier(word, attribution)
        if anchored and close.kind == KIND_ROUTINE and close.hold_interrupted is None:
            tier = TIER_RESOLVED
        # §9's morning-facts entry: the day-following-a-cycle line that also
        # lands on the History console's morning states — unit, verdict, the
        # delta pair, the spread change.
        self._morning = {
            "night": close.night.isoformat(),
            "unit_id": close.unit_id,
            "verdict": close.verdict,
            "graduation": word,
            "taper_observed": close.taper_observed_at is not None,
            "attribution": attribution,
            "delta_pct": payload["delta_pct"],
            "spread": payload["spread"],
            "tier": tier,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._calibration_row(
                event_type="calibration_taper_observed",
                unit_id=close.unit_id,
                reason_codes=("anchored",) if anchored else (word,),
                result=word,
                payload=payload,
            )
        )
        await self._publish_event(payload, tier)
        if anchored:
            # The durable once-fact (§8): the routine-rotation receipt,
            # derived from the completed record — never a config flag.
            await self._append_row(
                self._calibration_row(
                    event_type="calibration_anchored",
                    unit_id=close.unit_id,
                    reason_codes=("graduated",),
                    result="anchored",
                    payload={
                        "night": close.night.isoformat(),
                        "kind": close.kind,
                        "as_of": wall.astimezone(UTC).isoformat(),
                    },
                )
            )
            with contextlib.suppress(Exception):
                self._anchored = frozenset({*self._anchored, close.unit_id})
        elif close.kind == KIND_MEASUREMENT:
            # §6.3's stand-down: the pod does not rotate routinely until an
            # operator acknowledgement — durable-row-derived either way.
            with contextlib.suppress(Exception):
                self._standdown = frozenset({*self._standdown, close.unit_id})
        self._close = None

    async def _morning_surplus_kwh(self, close: _CloseState) -> float | None:
        """§5.4's attribution basis: the recorded PRE-BATTERY surplus over
        the morning span, the trust scoreboard's own integration."""
        from .night_trust import integrate_recorded_surplus

        anchor = self._settings.top_anchor
        morning = close.night + timedelta(days=1)
        midday = datetime.combine(morning, anchor.taper_deadline_local, tzinfo=self._zone)
        start = datetime.combine(
            morning, self._night_window_end or time(0, 0), tzinfo=self._zone
        )
        if start >= midday:
            start = midday - timedelta(hours=6)
        rows: tuple[Any, ...] = ()
        with contextlib.suppress(Exception):
            rows = tuple(
                self._history.samples(
                    self._settings.unit_ids, start.astimezone(UTC), midday.astimezone(UTC)
                )
            )
        if not rows:
            return None
        recorded = integrate_recorded_surplus(
            rows,
            unit_ids=self._settings.unit_ids,
            window_end=start.astimezone(UTC),
            midday=midday.astimezone(UTC),
            max_sample_gap_s=180.0,
        )
        surplus = None if recorded is None else round(recorded.e_recorded_kwh, 2)
        close.morning_surplus_kwh = surplus
        return surplus

    @staticmethod
    def _latest_spread_mv(observation: Any) -> float | None:
        return _finite(getattr(observation, "cell_spread_mv", None))

    # --- the skip-if set (§3.3) -------------------------------------------------

    async def _skip_reason(
        self,
        unit_id: str,
        latest: Any,
        *,
        active: Sequence[Any],
        now_mono: float,
        plan: _UnitPlan | None,
    ) -> str | None:
        """The standing guards, one honest reason per unit — every skip a
        recorded verdict with its reason, never a retry that night."""
        if unit_id in self._parked_view():
            return "unit_parked"
        if unit_id in self._latched_view():
            return "latched_stop"
        word = _word(getattr(latest, "debug_mode_w", None))
        if word is not None and word in {2, 3, 4, 5, 6}:
            return "vendor_mode"
        health = await self._health_states()
        state = getattr(health.get(unit_id), "state", None)
        state_word = _enum_text(state) if state is not None else ""
        if state_word == "unreachable":
            return "unreachable"
        if state_word == "not_responding":
            return "not_responding"
        if state_word == "foreign_writer":
            return "foreign_writer"
        if state_word in {"inhibited", "actuation_incoherent"}:
            return "inhibited"
        captured = _finite(getattr(latest, "captured_at_mono", None))
        if latest is None or captured is None or (
            now_mono - captured > float(self._policy.max_telemetry_age_s)
        ):
            return "telemetry_stale"
        lifecycle = _enum_text(getattr(latest, "lifecycle", None))
        if lifecycle not in {"armed_idle", "active"}:
            return "unit_disarmed"
        claims = self._claiming_units(active)
        if unit_id in claims.manual or unit_id in claims.agent:
            return "under_intent"
        if unit_id in claims.schedule:
            return "schedule_claim"
        if unit_id in claims.optimizer:
            # C3: FREE surplus outranks the anchor, one direction only — the
            # anchor waits, it never contests.
            return "optimizer_claim"
        if plan is not None and plan.klass == CLASS_DEFERRED_PROBE:
            # The §3.2 gate re-checked at window open (§3.3's own tail).
            return CLASS_DEFERRED_PROBE
        return None

    def _claiming_units(self, active: Sequence[Any]) -> _ClaimView:
        """The C3 claim view: MANUAL/AGENT/SCHEDULE claims and not-own
        OPTIMIZER claims (the excess adviser's charge from surplus)."""
        manual: set[str] = set()
        agent: set[str] = set()
        schedule: set[str] = set()
        optimizer: set[str] = set()
        for intent in active:
            source = _enum_text(getattr(intent, "source", None))
            if source not in _CLAIMING_SOURCES:
                continue
            intent_id = str(getattr(intent, "id", "") or "")
            if source == "optimizer" and intent_id.startswith(_OWN_INTENT_PREFIX):
                continue  # the adviser's own held intent, never foreign
            for unit_id in getattr(intent, "selected_unit_ids", ()) or ():
                if source == "manual":
                    manual.add(str(unit_id))
                elif source == "agent":
                    agent.add(str(unit_id))
                elif source == "schedule":
                    schedule.add(str(unit_id))
                else:
                    optimizer.add(str(unit_id))
        return _ClaimView(
            frozenset(manual), frozenset(agent), frozenset(schedule), frozenset(optimizer)
        )

    def _preempted_by(self, active: Sequence[Any], unit_id: str) -> str | None:
        """§4.3's preemption test: MANUAL / AGENT / emergency-stop only."""
        for intent in active:
            source = _enum_text(getattr(intent, "source", None))
            if source not in _PREEMPTING_SOURCES:
                continue
            if unit_id in {str(unit) for unit in getattr(intent, "selected_unit_ids", ()) or ()}:
                return source
        return None

    # --- the durable-row derivations -------------------------------------------

    async def _recent_rows(self) -> tuple[Any, ...]:
        with contextlib.suppress(Exception):
            rows = await self._audit.recent(limit=_ROW_SCAN_LIMIT)
            return tuple(
                row
                for row in rows
                if str(getattr(row, "event_type", "")).startswith("calibration_")
            )
        return ()

    async def _recent_passing_probes(self, today: date) -> dict[str, tuple[str, str]]:
        """The §3.2 interlock: the sibling's own durable verdict rows, read
        one-directionally (the A14 boundary kept: one row, not a stage)."""
        rows: tuple[Any, ...] = ()
        with contextlib.suppress(Exception):
            rows = await self._audit.recent(limit=_ROW_SCAN_LIMIT)
        window_start = today - timedelta(days=self._settings.trigger.probe_pass_window_days)
        passing: dict[str, tuple[str, str]] = {}
        for row in rows:
            if str(getattr(row, "event_type", "")) != "health_probe_completed":
                continue
            payload = getattr(row, "payload", None)
            if not isinstance(payload, Mapping) or payload.get("verdict") != "pass":
                continue
            unit_id = getattr(row, "unit_id", None)
            night = payload.get("night")
            if not isinstance(unit_id, str) or not isinstance(night, str):
                continue
            try:
                night_date = date.fromisoformat(night)
            except ValueError:
                continue
            if night_date >= window_start:
                passing.setdefault(unit_id, ("pass", str(payload.get("as_of") or night)))
        return passing

    async def _derive_durable_facts(self) -> tuple[frozenset[str], frozenset[str]]:
        """The anchored receipts and the standing stand-downs, from rows.

        The stand-down is durable-row-derived: a failed first cycle holds it
        until a LATER acknowledgement row lifts it (a restart neither grants
        nor lifts it).
        """
        rows = await self._recent_rows()
        anchored: set[str] = set()
        failures: dict[str, datetime] = {}
        acks: dict[str, datetime] = {}
        for row in rows:
            event_type = str(getattr(row, "event_type", ""))
            unit_id = getattr(row, "unit_id", None)
            if not isinstance(unit_id, str):
                continue
            occurred = getattr(row, "occurred_at", None)
            occurred = occurred if isinstance(occurred, datetime) else None
            payload = getattr(row, "payload", None)
            if event_type == "calibration_anchored":
                anchored.add(unit_id)
            elif event_type == "calibration_taper_observed" and isinstance(payload, Mapping):
                if payload.get("graduation") == GRADUATION_NOT_OBSERVED and occurred is not None:
                    failures[unit_id] = occurred
            elif event_type == "calibration_standdown_acknowledged" and occurred is not None:
                acks[unit_id] = occurred
        standdown = {
            unit_id
            for unit_id, failed_at in failures.items()
            if unit_id not in acks or acks[unit_id] < failed_at
        }
        return frozenset(anchored), frozenset(standdown)

    async def _request_already_consumed(self, request: tuple[str, str] | None) -> bool:
        """C6's one-shot property: consumption is durable — a restart (or a
        re-read of the same config revision) never consumes the same request
        twice."""
        if request is None:
            return False
        rows = await self._recent_rows()
        for row in rows:
            if str(getattr(row, "event_type", "")) != "calibration_trigger_evaluated":
                continue
            payload = getattr(row, "payload", None)
            if not isinstance(payload, Mapping):
                continue
            recorded = payload.get("request_measurement")
            if not isinstance(recorded, Mapping):
                continue
            if (
                recorded.get("unit") == request[0]
                and recorded.get("note") == request[1]
                and recorded.get("consumed") is True
            ):
                return True
        return False

    async def _night_has_rows(self, night: date) -> bool:
        """The once-per-night budget: any traverse row for this civil night
        retires it (the night-V2 retarget rule, and C2's retire)."""
        rows = await self._recent_rows()
        for row in rows:
            if str(getattr(row, "event_type", "")) not in {
                "calibration_traverse_opened",
                "calibration_cycle_completed",
            }:
                continue
            payload = getattr(row, "payload", None)
            if isinstance(payload, Mapping) and payload.get("night") == night.isoformat():
                return True
        return False

    # --- the C2 boot reconstruction ---------------------------------------------

    async def reconstruct_at_boot(self) -> None:
        """A boot that finds an open row retires the night, never resumes.

        Writes the completing ``calibration_cycle_completed`` row as
        ``inconclusive_interrupted`` from whatever figures the open row and
        the historian hold, raises the morning alert naming the state the
        pod was actually left in (notice tier normally; alert tier when the
        historian shows the pod deeper than floor + reanchor_delta_pct).
        """
        if self._boot_reconstructed:
            return
        self._boot_reconstructed = True
        with contextlib.suppress(Exception):
            rows = await self._recent_rows()
            completed: set[tuple[str, str]] = set()
            for row in rows:
                payload = getattr(row, "payload", None)
                if not isinstance(payload, Mapping):
                    continue
                if str(getattr(row, "event_type", "")) == "calibration_cycle_completed":
                    unit = getattr(row, "unit_id", None)
                    if isinstance(unit, str):
                        completed.add((unit, str(payload.get("night"))))
            for row in rows:
                payload = getattr(row, "payload", None)
                if str(getattr(row, "event_type", "")) != "calibration_traverse_opened":
                    continue
                if not isinstance(payload, Mapping):
                    continue
                unit_id = getattr(row, "unit_id", None)
                if not isinstance(unit_id, str):
                    continue
                if (unit_id, str(payload.get("night"))) in completed:
                    continue
                await self._reconstruct_night(unit_id, payload)

    async def _reconstruct_night(self, unit_id: str, payload: Mapping[str, Any]) -> None:
        wall = self._clock.wall_now()
        night_raw = str(payload.get("night") or "")
        try:
            night = date.fromisoformat(night_raw)
        except ValueError:
            return
        floor = float(self._settings.traverse.floor_pct)
        # The state the pod was actually left in: the historian's own word
        # for where the interrupted night bottomed out.
        depth = await self._night_min_soc(unit_id, night)
        deeper = depth is not None and depth < floor + float(
            self._settings.measurement.reanchor_delta_pct
        )
        tier = TIER_ALERT if deeper else TIER_NOTICE
        body: dict[str, Any] = {
            "night": night_raw,
            "unit_id": unit_id,
            "kind": str(payload.get("kind") or KIND_MEASUREMENT),
            "verdict": VERDICT_INTERRUPTED,
            "tier": tier,
            "reconstructed": True,
            "start_soc_bms_pct": payload.get("start_soc_bms_pct"),
            "energy_bound_wh": payload.get("energy_bound_wh"),
            "min_soc_pct_at_interruption": depth,
            "left_deeper_than_graceful": deeper,
            "morning_alert": (
                "a restart retired the traverse night — the pod's next chance "
                "is the next eligible selection, starting from wherever it "
                f"stands; {STOP_ROUTE_SENTENCE}"
            ),
            "pinned_sentence": PINNED_SENTENCE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._calibration_row(
                event_type="calibration_cycle_completed",
                unit_id=unit_id,
                reason_codes=("restart_interrupted",),
                result=VERDICT_INTERRUPTED,
                payload=body,
            )
        )
        await self._publish_event(body | {"unit_id": unit_id}, tier)

    async def _night_min_soc(self, unit_id: str, night: date) -> float | None:
        with contextlib.suppress(Exception):
            start = datetime.combine(night, time(0, 0), tzinfo=self._zone).astimezone(UTC)
            rollups = tuple(
                self._history.rollup_hours([unit_id], start, start + timedelta(hours=24))
            )
            figures = rollup_daily(rollups, unit_id=unit_id, zone=self._zone).get(night)
            if figures is not None:
                return figures.soc_min_pct
        return None

    # --- the acknowledge-inhibit path (§6.3) ------------------------------------

    async def acknowledge_standdown(self, unit_id: str) -> dict[str, Any]:
        """The operator's stand-down reset: a durable acknowledgement row.

        The stand-down itself is durable-row-derived (a restart neither
        grants nor lifts it); only this audited operator act lifts it, and
        the next plan tick re-derives the class from the rows.
        """
        if unit_id not in set(self._settings.unit_ids):
            raise LookupError(f"no unit with id {unit_id!r}")
        wall = self._clock.wall_now()
        await self._append_row(
            self._calibration_row(
                event_type="calibration_standdown_acknowledged",
                unit_id=unit_id,
                reason_codes=("standdown_acknowledged",),
                result="acknowledged",
                payload={
                    "night": wall.astimezone(self._zone).date().isoformat(),
                    "decision_tree": (
                        "re-run the measurement after a physical restart "
                        "advisory, or escalate to the installer with the record"
                    ),
                    "as_of": wall.astimezone(UTC).isoformat(),
                },
            )
        )
        with contextlib.suppress(Exception):
            self._standdown = frozenset(set(self._standdown) - {unit_id})
        return {"unit_id": unit_id, "standdown": unit_id in self._standdown}

    # --- audit rows + events -----------------------------------------------------

    def _calibration_row(
        self,
        *,
        event_type: str,
        unit_id: str | None,
        reason_codes: tuple[str, ...],
        result: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now().astimezone(UTC)
        return AuditEvent(
            event_id=f"calibration-{event_type}-{uuid.uuid4().hex}",
            occurred_at=wall,
            monotonic_offset_s=now_mono,
            process_instance_id=self._process_instance_id or CALIBRATION_ADVISER_PRINCIPAL,
            event_type=event_type,
            unit_id=unit_id,
            principal=CALIBRATION_ADVISER_PRINCIPAL,
            correlation_id=f"calibration:{event_type}",
            policy_version=_CALIBRATION_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"event_type": event_type, **payload}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=UnitLifecycle.DISARMED,
            payload=payload,
        )

    async def _append_row(self, event: AuditEvent) -> None:
        with contextlib.suppress(Exception):
            await self._audit.append(event)

    async def _publish_event(self, payload: Mapping[str, Any], tier: str) -> None:
        with contextlib.suppress(Exception):
            await self._bus.publish(
                {"type": CALIBRATION_EVENT_TYPE, "payload": dict(payload) | {"tier": tier}}
            )

    # --- views -------------------------------------------------------------------

    def _parked_view(self) -> frozenset[str]:
        if self._parked_units is None:
            return frozenset()
        with contextlib.suppress(Exception):
            return frozenset(self._parked_units())
        return frozenset()

    def _latched_view(self) -> frozenset[str]:
        if self._latched_stop_units is None:
            return frozenset()
        with contextlib.suppress(Exception):
            return frozenset(self._latched_stop_units())
        return frozenset()

    def state_payload(self) -> dict[str, Any]:
        """§8's ``calibration_state`` projection (the snapshot's own key).

        Present exactly when the block composes; ``mode: advise`` renders the
        whole projection with ``submits: never`` named beside it — a
        displayed plan that does not act MUST say so beside itself.
        """
        wall = self._clock.wall_now()
        request = self._settings.request_measurement
        request_view = (
            None
            if request is None
            else {
                "unit": request[0],
                "note": request[1],
                "consumed": self._request_consumed == request,
            }
        )
        payload: dict[str, Any] = {
            "mode": self._settings.mode,
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.traverse_end_local.strftime("%H:%M"),
            },
            "phase": self._phase_word(),
            "target": self._target,
            "reason": self._target_reason,
            "due_waived": self._due_waived,
            "as_of": wall.astimezone(UTC).isoformat(),
            "units": [
                self._plan_row(plan)
                for plan in sorted(self._plans.values(), key=lambda plan: plan.unit_id)
            ],
            "last_cycle": self._last_cycle,
            "morning": self._morning,
            "request_measurement": request_view,
        }
        if self._settings.mode == "advise":
            payload["submits"] = "never"
        leg = self._leg
        if leg is not None:
            payload["traverse"] = {
                "unit_id": leg.unit_id,
                "soc_pct": None if not leg.soc_trace else round(leg.soc_trace[-1], 2),
                "floor_pct": self._settings.traverse.floor_pct,
                "energy_wh": round(max(0.0, leg.energy_wh), 1),
                "energy_bound_wh": round(leg.energy_bound_wh, 1),
                "rate_w": leg.rates[-1] if leg.rates else None,
                "at_risk": leg.at_risk,
                "pinned_sentence": PINNED_SENTENCE,
                "stop_route": STOP_ROUTE_SENTENCE,
            }
        close = self._close
        if close is not None and not close.finished:
            payload["close"] = {
                "unit_id": close.unit_id,
                "night": close.night.isoformat(),
                "verdict": close.verdict,
                "taper_observed": close.taper_observed_at is not None,
                "hold_interrupted": close.hold_interrupted is not None,
                "attribution": close.attribution,
            }
        return payload

    def _phase_word(self) -> Phase:
        if self._leg is not None:
            return "traversing"
        close = self._close
        if close is not None:
            if close.finished:
                return "complete"
            if close.taper_observed_at is not None:
                return "holding"
            return "closing"
        if self._target is not None:
            return "planned"
        return "idle"

    async def refresh_durable_facts(self) -> None:
        """Re-derive the anchored/stand-down sets from the durable rows.

        Called at composition (the boot half of the durable derivations) and
        after an acknowledgement, so the projection never lags a row.
        """
        with contextlib.suppress(Exception):
            anchored, standdown = await self._derive_durable_facts()
            self._anchored, self._standdown = anchored, standdown


__all__ = [
    "CALIBRATION_ADVISER_PRINCIPAL",
    "CALIBRATION_EVENT_HONESTY_NOTE",
    "CALIBRATION_EVENT_TYPE",
    "CALIBRATION_NOT_COMMISSIONED",
    "CLASS_DEFERRED_PROBE",
    "CLASS_ELIGIBLE",
    "CLASS_EVIDENCE_SHORT",
    "CLASS_EXCLUDED_CYCLES",
    "CLASS_NOT_DUE",
    "CLASS_NO_CONTROL_EVIDENCE",
    "GRADUATION_ANCHORED",
    "GRADUATION_NOT_OBSERVED",
    "KIND_MEASUREMENT",
    "KIND_ROUTINE",
    "PINNED_SENTENCE",
    "STOP_ROUTE_SENTENCE",
    "TAPER_NEVER_OBSERVED",
    "TIER_ALERT",
    "TIER_NOTICE",
    "TIER_RESOLVED",
    "TOP_ANCHOR_MISSED_SOLAR",
    "TRACE_FROZEN",
    "TRACE_MONOTONE",
    "TRACE_STEPPED",
    "VERDICT_FLOOR_MISS_DEADLINE",
    "VERDICT_FLOOR_MISS_ENERGY",
    "VERDICT_FLOOR_REACHED",
    "VERDICT_INTERRUPTED",
    "VERDICT_PREEMPTED",
    "CalibrationAdviser",
    "CalibrationMeasurementSettings",
    "CalibrationRefusal",
    "CalibrationSettings",
    "CalibrationTopAnchorSettings",
    "CalibrationTraverseSettings",
    "CalibrationTriggerSettings",
    "DayFigures",
    "TriggerVector",
    "close_tier",
    "cycle_economics_cents",
    "cycle_tier",
    "deadline_rate_w",
    "energy_bound_wh",
    "graduation",
    "rollup_daily",
    "sum_rule_headroom_wh",
    "trace_class",
    "trigger_vector",
]
