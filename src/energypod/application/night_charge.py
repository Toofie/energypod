"""The off-peak night charge strategy (the night strategy adviser).

API_CONTRACTS "Off-peak night charge" + DESIGN_NIGHT_CHARGE.  The operator's
existing Docker solution force-charges all three batteries at 2,500 W per unit
from 00:00 to 06:00 nightly, dropping to a very low rate when house demand is
high; this module is its replacement — a strategy layer that computes a
per-battery charge plan each tick inside a commissioned civil-time window and
submits ordinary short-TTL ``OPTIMIZER`` charge intents through the facade's
internal night twin.

This module holds NO new authority anywhere (the excess-adviser doctrine):

- it owns no transport, no authorization path, and no allocator or kernel
  role (it imports no control adapter and cannot);
- it submits ordinary ``OPTIMIZER`` charge intents — a night charge is an
  ordinary intent judged by the arbiter, allocator, SafetyKernel, and actor
  exactly as a manual request is;
- hand-back at window end is ALWAYS by non-renewal: the adviser never writes
  a register, never posts a stop triple, and never submits an idle or
  zero-watt intent.  When it stops renewing, the intent lapses by TTL and the
  ~3.5-4.0 s firmware watchdog returns the pod to its own autonomy.

The demand rule's source of truth is the per-pod LOAD CT words
(``load_power_w``, 0x1000+20), NEVER the grid words — the design's one
non-obvious correctness catch: the grid word includes the adviser's OWN
charging draw, so a demand rule on it would read a 3 x 2500 W charge as
7.5 kW of "demand" and hold forever.  Evidence quality is the scorecard's
family and FAILS CLOSED TO HOLD (preserving the no-cycling guarantee):
charging blind into an EV at 7 kW is the exact outcome the operator refused.

The demand RESPONSE is pinned (§2.4, the operator's directive 2026-08-24):
a MEASURED demand hold stands the unit down entirely — zero-watt
non-participation, exclusion at submission time (the 6abd869/d2163a5
doctrine), the TTL lapse plus watchdog handing the pod back to its own
autonomy until demand subsides or the window ends.  The fail-closed
polarity is the SAFETY DOCTRINE, not a selectable posture: missing/bad/
stale evidence HOLDS at ``hold_rate_w`` — the evidence-failure fallback
rate and never a demand behavior — because standby answers measured
demand, never missing data, and must never silently free-run a fleet
into autonomy drain during an EV night.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Final, Literal, Protocol, TypeGuard
from zoneinfo import ZoneInfo

from energypod.domain import DataQuality, Direction, IntentSource, UnitLifecycle

_CONTROLLABLE_LIFECYCLES: frozenset[UnitLifecycle] = frozenset(
    {UnitLifecycle.ARMED_IDLE, UnitLifecycle.ACTIVE}
)

# §4 precedence, pinned: manual requests outrank the adviser per battery (the
# arbiter's own emergency_stop > manual > agent > optimizer > schedule already
# displaces it), the published SCHEDULE fact beats the opportunist (there is
# deliberately NO opt-out flag — this feature has no prior behavior to
# preserve, and refusing the flag refuses the invisible-starvation class at
# birth), and a live not-own OPTIMIZER intent (the excess adviser at the dawn
# corner) excludes its units too — FREE surplus outranks PAID import, one
# direction only.  A live EMERGENCY_STOP claims every unit outright and is
# handled before the per-unit walk.
_CLAIMING_SOURCES: frozenset[IntentSource] = frozenset(
    {IntentSource.MANUAL, IntentSource.AGENT, IntentSource.SCHEDULE, IntentSource.OPTIMIZER}
)
# The facade twin's pinned intent-id prefix: an OPTIMIZER intent carrying it
# is the night adviser's own held intent (seen at the next tick's claim read,
# before the remove-then-submit renewal takes it out) and never a foreign
# claim to exclude.
_OWN_INTENT_PREFIX: Final[str] = "night-"

# The action vocabulary of one night tick (the excess adviser's words:
# "withdraw" is a repository removal of the adviser's own intent — never a
# stop triple, an idle intent, or a zero-watt submission).
Action = Literal["idle", "propose", "renew", "withdraw"]

# §5's ONE phase vocabularies.  ``standing_by_on_demand`` is the measured
# demand stand-down (fleet and unit): the demand rule engaged on a GOOD word
# above the threshold and the unit is stood down entirely — zero-watt
# non-participation, excluded from the submission, the pod back on its own
# autonomy.  ``holding_on_demand`` is the fail-closed evidence hold alone:
# the positive ``hold_rate_w`` charge kept while the demand word is
# missing/bad/stale, never a demand behavior.
NightPhase = Literal[
    "idle", "pacing", "holding_on_demand", "standing_by_on_demand", "complete", "skipped_full"
]
NightUnitPhase = Literal[
    "pacing",
    "holding_on_demand",
    "standing_by_on_demand",
    "skipped_full",
    "complete",
    "sitting_out",
]
EnabledOrigin = Literal["config", "runtime"]
DemandScope = Literal["fleet", "per_phase"]
PacingRule = Literal["cap_first", "even"]

# The participation states the tick alone cannot see (§5's vocabulary
# additions, the excess pattern).
REASON_DISABLED_BY_CONFIG: Final[str] = "disabled_by_config"
REASON_DISABLED_BY_RUNTIME: Final[str] = "disabled_by_runtime"
REASON_NIGHT_ACKNOWLEDGEMENT_REQUIRED: Final[str] = "night_acknowledgement_required"

# The demand rollup's evidence words — the same spellings and the pinned
# worst-word-wins precedence as the excess rollup (a unit that served no word
# at all is strictly less knowable than one that served a flagged or aged
# one).
DemandEvidence = Literal["good", "missing", "bad", "stale"]
_DEMAND_EVIDENCE_RANK: Final[dict[str, int]] = {
    "good": 0,
    "stale": 1,
    "bad": 2,
    "missing": 3,
}

STATE_EVENT_TYPE: Final[str] = "night_charge.state_changed"
# §5: the heartbeat cadence is a constant, not a config key.
STATE_EVENT_HEARTBEAT_S: Final[float] = 30.0


# --- the pure civil-time helpers (§2.3) -----------------------------------------
#
# ``in_window``/``next_window_start``/``open_window_end`` are the SINGLE
# implementation of every countdown the surface serves (the projection's
# ``window_ends_at``/``next_window_at``); no client reimplements civil-time
# arithmetic.  All three are pure, deterministic, and DST-honest through the
# window's own zone.


def _second_of_day(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _pair_covers(second: int, start: int, end: int) -> bool:
    """Whether one civil second lies inside [start, end), wrapping midnight."""
    if start < end:
        return start <= second < end
    return second >= start or second < end


def in_window(local_now: datetime, windows: tuple[tuple[time, time], ...]) -> bool:
    """Civil-time containment in the union of the window pairs.

    ``local_now`` is the evaluation instant in the window's own zone (the
    caller's contract); only its wall clock is consulted, so a
    DST-transition night is judged by the wall clocks the window is defined
    by (23 or 25 h — the window is a civil-time fact).
    """
    second = _second_of_day(local_now.timetz().replace(tzinfo=None))
    return any(
        _pair_covers(second, _second_of_day(start), _second_of_day(end)) for start, end in windows
    )


# The next-occurrence horizon: 8 days covers a weekly rhythm and both sides
# of a cross-midnight boundary.
_NEXT_START_HORIZON_DAYS = 8


def next_window_start(
    at: datetime, windows: tuple[tuple[time, time], ...], zone: ZoneInfo
) -> datetime:
    """The earliest wall-clock start strictly after ``at``, in the zone.

    Only starts AFTER the instant count (a window already open belongs to the
    plan, not to this countdown); a cross-midnight window's tail does not
    count as its own start.
    """
    local = at.astimezone(zone)
    best: datetime | None = None
    for start, _end in windows:
        for offset in range(_NEXT_START_HORIZON_DAYS + 1):
            candidate = datetime.combine(local.date() + timedelta(days=offset), start, tzinfo=zone)
            if candidate <= local:
                continue
            if best is None or candidate < best:
                best = candidate
    if best is None:  # pragma: no cover - windows are non-empty by validation
        raise ValueError("the night window set is empty")
    return best


def open_window_end(
    at: datetime, windows: tuple[tuple[time, time], ...], zone: ZoneInfo
) -> datetime | None:
    """The local end instant of the currently-open window, or ``None``.

    A midnight-crossing window in its head ends TOMORROW at its end wall; one
    in its tail ends TODAY — one pair, one continuous window, never two.
    When several pairs are open at once (a union), the latest end wins: the
    plan's zone runs to the last closing wall.
    """
    local = at.astimezone(zone)
    second = _second_of_day(local.timetz().replace(tzinfo=None))
    best: datetime | None = None
    for start, end in windows:
        start_s, end_s = _second_of_day(start), _second_of_day(end)
        if not _pair_covers(second, start_s, end_s):
            continue
        end_date = local.date()
        if start_s > end_s and second >= start_s:
            end_date += timedelta(days=1)
        candidate = datetime.combine(end_date, end, tzinfo=zone)
        if best is None or candidate > best:
            best = candidate
    return best


# --- the demand rollup (§2.4) — LOAD words, never grid words ---------------------


def _finite_number(value: Any) -> TypeGuard[float]:
    """A real, finite measurement — never a bool masquerading as one."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _load_evidence_word(observation: Any, *, max_age_s: float, now_mono: float) -> DemandEvidence:
    """One unit's LOAD word classified under the rule's own fail-closed rules.

    The kernel spellings: ``missing`` when the observation or the word itself
    is absent/non-finite, ``bad`` when a present quality key is not GOOD, and
    ``stale`` when the capture is older than the commissioned freshness
    bound.  This reads ``load_power_w`` (0x1000+20) — the grid word includes
    the adviser's OWN charging draw and is never consulted here.
    """
    load_w = getattr(observation, "load_power_w", None)
    if observation is None or not _finite_number(load_w):
        return "missing"
    quality = getattr(observation, "quality", None)
    flag = quality.get("load_power_w") if isinstance(quality, Mapping) else None
    if flag is not None and flag is not DataQuality.GOOD:
        return "bad"
    captured_at_mono = getattr(observation, "captured_at_mono", None)
    if not _finite_number(captured_at_mono):
        return "missing"
    if float(now_mono) - captured_at_mono > float(max_age_s):
        return "stale"
    return "good"


@dataclass(frozen=True, slots=True)
class DemandReading:
    """The §2.4 rollup over the fleet's LOAD words.

    ``evidence`` is the worst word (precedence ``missing > bad > stale >
    good``); ``demand_w`` is ``max(0, floor(sum(load_power_w)))`` while every
    word is GOOD and ``None`` on any non-good word — never zero-filled, the
    console renders the null as "not available".  The per-unit words and
    figures feed the ``per_phase`` scope (only the pod(s) whose phase shows
    the demand hold).
    """

    evidence: DemandEvidence
    demand_w: int | None
    per_unit: Mapping[str, DemandEvidence]
    per_unit_w: Mapping[str, int | None]


def demand_reading(
    observations: Mapping[str, Any],
    unit_ids: tuple[str, ...],
    *,
    max_age_s: float,
    now_mono: float,
) -> DemandReading:
    """The fleet LOAD-word rollup under the rule's own fail-closed rules."""
    worst: DemandEvidence = "good"
    per_unit: dict[str, DemandEvidence] = {}
    per_unit_w: dict[str, int | None] = {}
    total_w = 0.0
    for unit_id in unit_ids:
        observation = observations.get(unit_id)
        word = _load_evidence_word(observation, max_age_s=max_age_s, now_mono=now_mono)
        per_unit[unit_id] = word
        if word != "good":
            per_unit_w[unit_id] = None
            if _DEMAND_EVIDENCE_RANK[word] > _DEMAND_EVIDENCE_RANK[worst]:
                worst = word
            continue
        load_w = float(getattr(observation, "load_power_w", 0.0))
        per_unit_w[unit_id] = max(0, math.floor(load_w))
        total_w += load_w
    demand_w = max(0, math.floor(total_w)) if worst == "good" else None
    return DemandReading(
        evidence=worst, demand_w=demand_w, per_unit=per_unit, per_unit_w=per_unit_w
    )


_NO_READING: Final[DemandReading] = DemandReading("missing", None, {}, {})


# --- the per-unit charge plan (§2.2) --------------------------------------------


def even_rate_w(
    *,
    soc_pct: float,
    target_soc_pct: float,
    capacity_wh: float,
    remaining_s: float,
    cap_w: int,
) -> tuple[int, bool]:
    """The ``even`` deadline rate, clamped to ``[1, cap_w]``.

    Returns ``(rate_w, at_risk)``: ``required_w = ceil((target - soc)/100 x
    capacity x 3600 / remaining_s)`` recomputed from MEASURED SOC every tick,
    so it self-corrects — an optimistic capacity estimate or a demand hold
    pushes ``required_w`` up next tick and the rule converges to the cap
    exactly when behind (``at_risk``), with no separate escalation mode.
    The ceil carries a floating-point tolerance so an exact-whole-watt
    requirement (0.07 x 5000 x 0.2 = 70 W) never rounds up on binary noise.
    """
    if remaining_s <= 0:
        return int(cap_w), True
    exact = (target_soc_pct - soc_pct) / 100.0 * float(capacity_wh) * 3600.0 / float(remaining_s)
    required = math.ceil(exact - _CEIL_EPSILON)
    rate = min(max(required, 1), int(cap_w))
    return rate, required > int(cap_w)


# One millionth of a watt: generous against binary noise, invisible against
# any genuine fractional requirement.
_CEIL_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class NightUnitPlan:
    """One unit's per-tick row: its own SOC, phase, target, and reason."""

    unit_id: str
    soc_pct: float | None
    phase: NightUnitPhase
    target_w: int
    reason: str


@dataclass(frozen=True, slots=True)
class NightChargeDecision:
    """One night tick's outcome, for the projection's single-writer update."""

    action: Action
    phase: NightPhase
    in_window: bool
    unit_plans: tuple[NightUnitPlan, ...]
    active_unit_ids: tuple[str, ...]
    demand_w: int | None
    demand_evidence: DemandEvidence
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NightChargeSettings:
    """Every behavioural key of the ``night_charging`` config block."""

    rate_cap_w: int
    hold_rate_w: int
    demand_threshold_w: int
    demand_exit_hysteresis_w: int
    demand_scope: DemandScope
    pacing: PacingRule
    assumed_capacity_wh: Mapping[str, int] | None
    demand_telemetry_max_age_s: float
    intent_ttl_s: float
    windows: tuple[tuple[time, time], ...]
    timezone: str
    unit_ids: tuple[str, ...]


class _Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class _ObservationPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentPort(Protocol):
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


class _ParticipationPort(Protocol):
    """The tick-start participation read (the excess P6 pattern).

    Returns ``None`` while the adviser participates this tick, or the pinned
    projection reason code (``disabled_by_config`` / ``disabled_by_runtime``
    / ``night_acknowledgement_required``) while it does not.  The toggle
    flips only the flag behind this port; the next tick observes it.
    """

    def __call__(self) -> str | None: ...


class NightChargeAdviser:
    """One short-TTL CHARGE intent per tick, renewed remove-then-submit.

    The plan is recomputed every tick from measured SOC, measured demand,
    live claims, and time remaining (a strategy, never a published fact).
    Renewal is remove-previous-then-submit-fresh; any failure to renew —
    adviser stall, process death, participation loss, window end — ends the
    intent by TTL and the firmware watchdog returns the pod to its own
    autonomy.  Exactly one held intent, ever; per-unit exclusion happens at
    SUBMISSION time, never by idle "placeholder" intents.
    """

    def __init__(
        self,
        *,
        settings: NightChargeSettings,
        policy: Any,
        clock: _Clock,
        observations: _ObservationPort,
        intents: _IntentPort,
        submit: _SubmitPort,
        participation: _ParticipationPort | None = None,
        parked_units: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._clock = clock
        self._observations = observations
        self._intents = intents
        self._submit = submit
        self._participation = participation
        # DESIGN_POD_PARKING section 3 (coordinator ruling 2026-08-24): the
        # night adviser EXCLUDES parked units at selection -- resume, not
        # arm, is the true next step, and repeatedly submitting into a
        # standing refusal would violate the never-retry-a-denied-dispatch
        # doctrine.  ``None`` (isolated compositions) excludes nothing.
        self._parked_units = parked_units
        self._zone = ZoneInfo(settings.timezone)
        self._held_intent_id: str | None = None
        # Window-scoped state, reset at every window boundary (§2.4: the hold
        # latch is adviser state, reset at window open and window close).
        self._window_open = False
        self._holding_fleet = False
        self._holding_units: frozenset[str] = frozenset()
        self._participated: frozenset[str] = frozenset()
        self._submitted_this_window = False
        self._resumed_this_tick = False

    @property
    def held_intent_id(self) -> str | None:
        """The live night intent id, or ``None`` while holding nothing.

        The projection derives ``active`` from THIS fact — never a lifecycle
        guess — so it can never claim inactive while a night intent is live.
        """
        return self._held_intent_id

    async def tick(self) -> NightChargeDecision:
        """Evaluate the plan once and act; never raises past its ports."""
        wall = self._clock.wall_now()
        local = wall.astimezone(self._zone)
        now_mono = float(self._clock.monotonic())
        window_now = in_window(local, self._settings.windows)
        self._roll_window_state(window_now)
        self._resumed_this_tick = False

        verdict = None if self._participation is None else self._participation()
        if verdict is not None:
            # Participation is read AT TICK START (the excess P6 shape): a
            # disabled tick withdraws-if-held exactly once by removal and
            # idles carrying the projection's own participation reason code.
            return await self._standby("idle", window_now, _NO_READING, (verdict,), ())
        if not window_now:
            # Window end is NON-RENEWAL: remove, then the TTL lapse and the
            # ~3.5-4.0 s watchdog return each pod to its own autonomy.
            return await self._standby("idle", False, _NO_READING, ("outside_window",), ())

        latest = await self._observations.all_latest()
        reading = demand_reading(
            latest,
            self._settings.unit_ids,
            max_age_s=self._settings.demand_telemetry_max_age_s,
            now_mono=now_mono,
        )
        active = await self._intents.active(now_mono)
        if self._any_emergency_stop(active):
            # §4.5: a latched stop claims its units and dominates the whole
            # cycle while it holds — withdraw entirely, re-plan after the
            # acknowledgement (the window may still complete).
            stood_down = tuple(
                self._sitting_out(unit_id, "yielding_to_higher_priority", latest.get(unit_id))
                for unit_id in self._settings.unit_ids
            )
            return await self._standby(
                "idle", True, reading, ("window_open", "yielding_to_higher_priority"), stood_down
            )
        claimed = self._claimed_units(active)

        plans: list[NightUnitPlan] = []
        participating: list[str] = []
        held_units: list[str] = []
        standing_units: list[str] = []
        at_risk = False
        remaining_s = self._remaining_s(wall)
        for unit_id in self._settings.unit_ids:
            plan, unit_at_risk, unit_held = self._plan_unit(
                unit_id, latest.get(unit_id), claimed, reading, remaining_s
            )
            at_risk = at_risk or unit_at_risk
            plans.append(plan)
            if plan.phase in ("pacing", "holding_on_demand"):
                participating.append(unit_id)
            if unit_held:
                held_units.append(unit_id)
                if plan.phase == "standing_by_on_demand":
                    standing_units.append(unit_id)

        if not participating:
            if standing_units:
                # Every demand-eligible unit stood down on MEASURED demand:
                # zero-watt non-participation IS the response, so the demand
                # code rides (a stand-down is by definition measured; a
                # fail-closed hold would be participating).
                return await self._standby(
                    "standing_by_on_demand",
                    True,
                    reading,
                    ("window_open", "demand_above_threshold"),
                    tuple(plans),
                )
            reasons = ("window_open", *_no_participant_reasons(tuple(plans)))
            return await self._standby(
                _completion_phase(tuple(plans)), True, reading, reasons, tuple(plans)
            )

        # The demand response's fleet code (either posture): the evidence
        # word's code outranks the plain threshold code (the loud
        # never-silent-hold requirement), and the tick that releases the
        # response says demand_below_exit (the resumed units are pacing by
        # then — the code names the transition).
        if held_units:
            if reading.evidence != "good":
                hold_code = f"demand_evidence_{reading.evidence}"
            else:
                hold_code = "demand_above_threshold"
            reason_codes: tuple[str, ...] = ("window_open", hold_code)
        elif self._resumed_this_tick:
            reason_codes = ("window_open", "demand_below_exit")
        elif at_risk:
            reason_codes = ("window_open", "deadline_at_risk")
        else:
            reason_codes = ("window_open", "on_plan")

        action: Action = "renew" if self._submitted_this_window else "propose"
        targets_by_unit = {
            plan.unit_id: plan.target_w
            for plan in plans
            if plan.phase in ("pacing", "holding_on_demand")
        }
        await self._remove_held()
        result = await self._submit(
            unit_ids=sorted(participating),
            direction=Direction.CHARGE,
            watts=None,
            ttl_s=self._settings.intent_ttl_s,
            watts_by_unit=dict(sorted(targets_by_unit.items())),
        )
        submitted_id = result.get("intent_id") if isinstance(result, Mapping) else None
        self._held_intent_id = submitted_id if isinstance(submitted_id, str) else None
        self._submitted_this_window = True
        self._participated = self._participated | frozenset(participating)
        if standing_units:
            phase: NightPhase = "standing_by_on_demand"
        elif held_units:
            phase = "holding_on_demand"
        else:
            phase = "pacing"
        return NightChargeDecision(
            action=action,
            phase=phase,
            in_window=True,
            unit_plans=tuple(plans),
            active_unit_ids=tuple(sorted(participating)),
            demand_w=reading.demand_w,
            demand_evidence=reading.evidence,
            reason_codes=reason_codes,
        )

    # --- internals -------------------------------------------------------

    def _roll_window_state(self, window_now: bool) -> None:
        """Reset the window-scoped latches at every boundary crossing."""
        if window_now == self._window_open:
            return
        self._window_open = window_now
        self._holding_fleet = False
        self._holding_units = frozenset()
        self._participated = frozenset()
        self._submitted_this_window = False

    def _remaining_s(self, wall: datetime) -> float:
        ends = open_window_end(wall, self._settings.windows, self._zone)
        if ends is None:  # pragma: no cover - tick only runs inside the window
            return 0.0
        return max(0.0, (ends - wall).total_seconds())

    def _any_emergency_stop(self, active: tuple[Any, ...]) -> bool:
        return any(
            getattr(intent, "source", None) is IntentSource.EMERGENCY_STOP for intent in active
        )

    def _claimed_units(self, active: tuple[Any, ...]) -> frozenset[str]:
        """Units claimed by a live higher-priority or not-own optimizer intent.

        Exclusion at submission time is what keeps the equal-priority
        OPTIMIZER corner (the dawn excess adviser) deterministic — no
        newest-revision tie flap.  The adviser's own held intent (the
        ``night-`` prefix) is never a foreign claim.
        """
        claimed: set[str] = set()
        for intent in active:
            source = getattr(intent, "source", None)
            if source not in _CLAIMING_SOURCES:
                continue
            intent_id = getattr(intent, "id", "") or ""
            if source is IntentSource.OPTIMIZER and intent_id.startswith(_OWN_INTENT_PREFIX):
                continue
            selected = getattr(intent, "selected_unit_ids", None)
            if selected:
                claimed.update(selected)
        return frozenset(claimed)

    def _unit_is_held(self, unit_id: str, reading: DemandReading) -> tuple[bool, bool]:
        """The §2.4 hold decision for one unit: ``(held, resumed)``.

        Hysteresis is the pinned rule — engage strictly above the threshold,
        resume only strictly below ``threshold - hysteresis`` — and the latch
        is adviser state (``_holding_fleet`` for the global scope,
        ``_holding_units`` per phase) reset at the window boundaries.  A
        non-good word FAILS CLOSED to the hold: bad evidence must PRESERVE
        the no-cycling guarantee.  The DECISION here is posture-invariant;
        ``_plan_unit`` chooses the RESPONSE (hold rate or stand-down).
        """
        threshold = self._settings.demand_threshold_w
        exit_bound = threshold - self._settings.demand_exit_hysteresis_w
        if self._settings.demand_scope == "per_phase":
            word = reading.per_unit.get(unit_id, "missing")
            unit_w = reading.per_unit_w.get(unit_id)
            if word != "good" or unit_w is None:
                return True, False
            if unit_id in self._holding_units:
                if unit_w < exit_bound:
                    return False, True
                return True, False
            return unit_w > threshold, False
        if reading.evidence != "good" or reading.demand_w is None:
            return True, False
        if self._holding_fleet:
            if reading.demand_w < exit_bound:
                return False, True
            return True, False
        return reading.demand_w > threshold, False

    def _driving_word_good(self, unit_id: str, reading: DemandReading) -> bool:
        """Whether the word DRIVING this unit's hold decision is GOOD.

        The ``fleet`` scope holds on the rollup's worst word; ``per_phase``
        holds on the unit's own word.  This is the stand-down's one gate:
        only a MEASURED demand stand-down may hand a pod back — a fail-closed
        hold (any non-good word) keeps the positive charge.
        """
        if self._settings.demand_scope == "per_phase":
            return reading.per_unit.get(unit_id, "missing") == "good"
        return reading.evidence == "good"

    def _plan_unit(
        self,
        unit_id: str,
        observation: Any,
        claimed: frozenset[str],
        reading: DemandReading,
        remaining_s: float,
    ) -> tuple[NightUnitPlan, bool, bool]:
        """One unit's eligibility, rate, and phase (§2.2's walk, in order).

        Returns ``(plan, at_risk, held)``.
        """
        # 1. Eligibility, each sit-out honest (never a fabricated target).
        if observation is None:
            return self._sitting_out(unit_id, "no_charge_headroom", None), False, False
        if unit_id in self._parked_view():
            # DESIGN_POD_PARKING section 3: the parked exclusion outranks
            # units_disarmed for a parked unit -- resume is the next step.
            return self._sitting_out(unit_id, "unit_parked", observation), False, False
        lifecycle = getattr(observation, "lifecycle", None)
        if lifecycle not in _CONTROLLABLE_LIFECYCLES:
            return self._sitting_out(unit_id, "units_disarmed", observation), False, False
        soc_pct = getattr(observation, "authoritative_soc_pct", None)
        if not _finite_number(soc_pct):
            return self._sitting_out(unit_id, "no_charge_headroom", observation), False, False
        if soc_pct >= self._policy.max_soc_pct:
            if unit_id in self._participated:
                # It charged this window and reached the target: the honest
                # completion row, not a from-the-start skip.
                return (
                    NightUnitPlan(unit_id, soc_pct, "complete", 0, "target_reached"),
                    False,
                    False,
                )
            return (NightUnitPlan(unit_id, soc_pct, "skipped_full", 0, "at_ceiling"), False, False)
        dynamic = getattr(observation, "dynamic_charge_limit_w", None)
        static = self._policy.static_charge_limit_w_by_unit.get(unit_id)
        achievable = (
            max(0, min(int(self._settings.rate_cap_w), math.floor(dynamic), int(static)))
            if _finite_number(dynamic) and isinstance(static, int)
            else 0
        )
        if achievable <= 0:
            # The BMS's own honest refusal — the adviser does not ask what
            # the battery just refused (rhs tonight: dynamic limit 0 W).
            return self._sitting_out(unit_id, "no_charge_headroom", observation), False, False
        if unit_id in claimed:
            return (
                self._sitting_out(unit_id, "yielding_to_higher_priority", observation),
                False,
                False,
            )

        # 2/3. The rate under the pinned pacing rule, then the demand response.
        held, resumed = self._unit_is_held(unit_id, reading)
        self._latch_held(unit_id, held)
        if resumed:
            self._resumed_this_tick = True
        if held:
            if self._driving_word_good(unit_id, reading):
                # MEASURED demand stands the unit down entirely — zero-watt
                # non-participation, excluded from the submission (a
                # zero-watt per-unit target is refused by the facade), the
                # TTL lapse plus watchdog returning the pod to its own
                # autonomy until demand subsides or the window ends.
                # Missing/bad/stale evidence NEVER takes this arm: it fails
                # closed to the positive hold below — the safety doctrine,
                # never silently standing the fleet down into autonomy
                # drain on data it cannot see.
                return (
                    NightUnitPlan(
                        unit_id,
                        soc_pct,
                        "standing_by_on_demand",
                        0,
                        "demand_above_threshold",
                    ),
                    False,
                    True,
                )
            # The fail-closed evidence hold: the one behavior that still
            # charges at ``hold_rate_w`` — the evidence-failure fallback
            # rate alone, its renewed objective preserving the no-cycling
            # guarantee until a GOOD word returns.
            return (
                NightUnitPlan(
                    unit_id,
                    soc_pct,
                    "holding_on_demand",
                    int(self._settings.hold_rate_w),
                    "demand_above_threshold",
                ),
                False,
                True,
            )
        if self._settings.pacing == "even":
            capacities = self._settings.assumed_capacity_wh or {}
            capacity = capacities.get(unit_id)
            if not isinstance(capacity, int):
                return (
                    self._sitting_out(unit_id, "no_charge_headroom", observation),
                    False,
                    False,
                )
            rate, at_risk = even_rate_w(
                soc_pct=float(soc_pct),
                target_soc_pct=float(self._policy.max_soc_pct),
                capacity_wh=capacity,
                remaining_s=remaining_s,
                cap_w=achievable,
            )
            return (
                NightUnitPlan(
                    unit_id,
                    soc_pct,
                    "pacing",
                    rate,
                    "deadline_at_risk" if at_risk else "on_plan",
                ),
                at_risk,
                False,
            )
        return (NightUnitPlan(unit_id, soc_pct, "pacing", achievable, "on_plan"), False, False)

    def _latch_held(self, unit_id: str, held: bool) -> None:
        """Persist this tick's hold decision into the adviser-state latch.

        The latch is what the hysteresis band rides on across ticks; it is
        reset at the window boundaries (never mid-window, so a demand load
        oscillating around the threshold never toggles the rate per tick).
        """
        if self._settings.demand_scope == "per_phase":
            members = set(self._holding_units)
            if held:
                members.add(unit_id)
            else:
                members.discard(unit_id)
            self._holding_units = frozenset(members)
        else:
            self._holding_fleet = held

    def _parked_view(self) -> frozenset[str]:
        """The parked-unit selection view; a failing view excludes nothing."""
        if self._parked_units is None:
            return frozenset()
        try:
            units = self._parked_units()
        except Exception:
            return frozenset()
        return frozenset(units)

    def _sitting_out(self, unit_id: str, reason: str, observation: Any) -> NightUnitPlan:
        soc = getattr(observation, "authoritative_soc_pct", None)
        return NightUnitPlan(
            unit_id, soc if _finite_number(soc) else None, "sitting_out", 0, reason
        )

    async def _standby(
        self,
        phase: NightPhase,
        in_window: bool,
        reading: DemandReading,
        reason_codes: tuple[str, ...],
        plans: tuple[NightUnitPlan, ...],
    ) -> NightChargeDecision:
        """Remove-if-held and submit nothing (non-renewal, the only exit).

        The action is ``withdraw`` exactly when the adviser held something to
        withdraw; standing down with nothing held is already ``idle``.  No
        stop triple, no idle intent, no zero-watt submission — ever.
        """
        action: Action = "withdraw" if self._held_intent_id is not None else "idle"
        await self._remove_held()
        if not in_window:
            self._submitted_this_window = False
        return NightChargeDecision(
            action=action,
            phase=phase,
            in_window=in_window,
            unit_plans=plans,
            active_unit_ids=(),
            demand_w=reading.demand_w,
            demand_evidence=reading.evidence,
            reason_codes=reason_codes,
        )

    async def _remove_held(self) -> None:
        held = self._held_intent_id
        self._held_intent_id = None
        if held is None:
            return
        # Removal is opportunistic churn control, never the safety path: an
        # intent the store no longer knows must not fail the tick — the TTL
        # lapse is the designed hand-back.
        with contextlib.suppress(Exception):
            await self._intents.remove(held)


def _no_participant_reasons(plans: tuple[NightUnitPlan, ...]) -> tuple[str, ...]:
    """The fleet codes for a window open with nothing able to charge."""
    if not plans:
        return ("no_eligible_units",)
    reasons = {plan.reason for plan in plans}
    if reasons == {"unit_parked"}:
        # DESIGN_POD_PARKING section 3: additive, and it outranks
        # units_disarmed -- a parked unit cannot be armed usefully anyway.
        return ("unit_parked",)
    if "unit_parked" in reasons and "units_disarmed" in reasons:
        return ("unit_parked", "units_disarmed")
    if reasons == {"units_disarmed"}:
        return ("units_disarmed",)
    if reasons <= {"at_ceiling"}:
        return ("at_ceiling",)
    if "target_reached" in reasons:
        return ("target_reached",)
    if "yielding_to_higher_priority" in reasons:
        return ("no_eligible_units", "yielding_to_higher_priority")
    return ("no_eligible_units",)


def _completion_phase(plans: tuple[NightUnitPlan, ...]) -> NightPhase:
    """The fleet phase for a window open with nothing charging."""
    phases = {plan.phase for plan in plans}
    if phases and phases <= {"skipped_full", "complete", "sitting_out"}:
        if any(plan.phase == "complete" for plan in plans):
            return "complete"
        if phases == {"skipped_full"}:
            return "skipped_full"
    return "idle"


# --- the operator-facing projection (§5, the adviser-state mirror) ---------------


class NightChargingRefusal(Exception):
    """A guarded-toggle refusal carrying its wire code and details.

    The facade raises exactly this for the 409 shapes; the guarded boundary
    maps ``code`` onto the error envelope verbatim.
    """

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class NightChargeState:
    """The frozen §5 projection: the night strategy's whole story."""

    enabled: bool
    enabled_origin: EnabledOrigin
    acknowledged_partition: bool
    posture: str
    active: bool
    phase: NightPhase
    window: dict[str, str]
    window_ends_at: str | None
    window_ends_in_s: int | None
    next_window_at: str | None
    pacing: PacingRule
    rate_cap_w: int
    hold_rate_w: int
    demand_scope: DemandScope
    demand_threshold_w: int
    demand_w: int | None
    demand_evidence: DemandEvidence
    held_intent_id: str | None
    units: tuple[dict[str, Any], ...]
    last_action: Action
    last_tick_at: str
    reason_codes: tuple[str, ...]
    active_unit_ids: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        """The §5 JSON shape (values JSON-native, codes as a list)."""
        return {
            "enabled": self.enabled,
            "enabled_origin": self.enabled_origin,
            "acknowledged_partition": self.acknowledged_partition,
            "posture": self.posture,
            "active": self.active,
            "phase": self.phase,
            "window": dict(self.window),
            "window_ends_at": self.window_ends_at,
            "window_ends_in_s": self.window_ends_in_s,
            "next_window_at": self.next_window_at,
            "pacing": self.pacing,
            "rate_cap_w": self.rate_cap_w,
            "hold_rate_w": self.hold_rate_w,
            "demand_scope": self.demand_scope,
            "demand_threshold_w": self.demand_threshold_w,
            "demand_w": self.demand_w,
            "demand_evidence": self.demand_evidence,
            "held_intent_id": self.held_intent_id,
            "units": [dict(unit) for unit in self.units],
            "last_action": self.last_action,
            "last_tick_at": self.last_tick_at,
            "reason_codes": list(self.reason_codes),
        }

    def event_payload(self) -> dict[str, Any]:
        """The §5 ``night_charge.state_changed`` payload: the projection
        subset minus ``last_action``/``last_tick_at`` (the snapshot's own)."""
        payload = self.payload()
        del payload["last_action"], payload["last_tick_at"]
        return payload

    def semantic_tuple(self) -> tuple[Any, ...]:
        """The §5 throttle tuple: the semantic state that triggers an event.

        The watt and SOC figures are deliberately absent — they ride every
        publication but never trigger one.
        """
        return (
            self.enabled,
            self.enabled_origin,
            self.acknowledged_partition,
            self.active,
            self.phase,
            self.active_unit_ids,
            self.demand_evidence,
            self.reason_codes,
        )


class _EventPublisherPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class _WallClock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> Any: ...


class NightChargeController:
    """Participation flag, partition acknowledgement, projection, events.

    The ``ExcessAdviserController`` mirror: composition wires this FIRST (it
    owns the participation flag the guarded toggle flips), the adviser is
    then constructed with ``participation_verdict`` as its tick-start port,
    and the controller binds the adviser for the live held-intent read.
    Writers are split by ownership, never by race — the fleet loop's
    ``observe_tick`` is the single writer of every tick-derived field; the
    facade's toggle is the only writer of the participation flag and the
    acknowledgement latch.
    """

    def __init__(
        self,
        *,
        pacing: PacingRule,
        rate_cap_w: int,
        hold_rate_w: int,
        demand_scope: DemandScope,
        demand_threshold_w: int,
        windows: tuple[tuple[time, time], ...],
        timezone: str,
        posture: str,
        clock: _WallClock,
        acknowledged_partition: bool,
        config_enabled: bool,
        bus: _EventPublisherPort | None = None,
        heartbeat_period_s: float = STATE_EVENT_HEARTBEAT_S,
    ) -> None:
        if isinstance(rate_cap_w, bool) or not isinstance(rate_cap_w, int) or rate_cap_w <= 0:
            raise ValueError("rate_cap_w must be a positive integer")
        if isinstance(heartbeat_period_s, bool) or not heartbeat_period_s > 0:
            raise ValueError("heartbeat_period_s must be positive")
        self._pacing = pacing
        self._rate_cap_w = int(rate_cap_w)
        self._hold_rate_w = int(hold_rate_w)
        self._demand_scope = demand_scope
        self._demand_threshold_w = int(demand_threshold_w)
        self._windows = windows
        self._timezone = timezone
        self._posture = posture
        self._zone = ZoneInfo(timezone)
        self._clock = clock
        self._bus = bus
        self._heartbeat_period_s = float(heartbeat_period_s)
        # Participation state (toggle-owned; never persisted — boot recomposes
        # from config, which is why the boot value is captured here and the
        # origin reads "config" until a toggle changes it).
        self._enabled = bool(config_enabled)
        self._enabled_origin: EnabledOrigin = "config"
        # The once-ever durable night-partition fact (§3.2), boot-loaded from
        # the audit store and latched by either surface's audited capture.
        self._acknowledged = bool(acknowledged_partition)
        self._adviser: NightChargeAdviser | None = None
        # Tick-derived fields (fleet-loop-owned single writer).  Before the
        # first tick the honest frame is an idle projection over MISSING
        # evidence: nothing has been read yet, and the first tick replaces it
        # within one cycle.
        self._last_action: Action = "idle"
        self._last_phase: NightPhase = "idle"
        self._last_units: tuple[NightUnitPlan, ...] = ()
        self._last_active_units: tuple[str, ...] = ()
        self._last_demand_w: int | None = None
        self._last_evidence: DemandEvidence = "missing"
        self._last_reason_codes: tuple[str, ...] = ("outside_window",)
        self._last_tick_at = self._clock.wall_now().isoformat()
        # §5 publication state.
        self._published_tuple: tuple[Any, ...] | None = None
        self._published_enabled: bool | None = None
        self._last_publish_mono: float | None = None

    def bind_adviser(self, adviser: NightChargeAdviser) -> None:
        """Bind the adviser for the live held-intent read (exactly once)."""
        if self._adviser is not None:
            raise RuntimeError("the night charge controller is already bound")
        self._adviser = adviser

    # --- participation (the toggle's half) --------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def enabled_origin(self) -> EnabledOrigin:
        return self._enabled_origin

    @property
    def acknowledged_partition(self) -> bool:
        return self._acknowledged

    def set_participation(self, *, enabled: bool) -> None:
        """Flip the participation flag; the next tick observes it.

        The origin becomes ``runtime`` — the honest "until restart" marker.
        This flips participation ONLY; every commissioned envelope (cap,
        window, pacing choice, grant, TTL) is a composition fact the toggle
        can never touch.
        """
        self._enabled = bool(enabled)
        self._enabled_origin = "runtime"

    def mark_acknowledged(self) -> None:
        """Latch the captured partition fact (after its durable append)."""
        self._acknowledged = True

    def participation_verdict(self) -> str | None:
        """``None`` while the adviser participates; else its reason code.

        Effective participation is the flag AND the acknowledgement: an
        unacknowledged site composes SUSPENDED even with the config block
        enabled, and a runtime disable outranks the pending acknowledgement
        in the vocabulary.
        """
        if not self._enabled:
            if self._enabled_origin == "runtime":
                return REASON_DISABLED_BY_RUNTIME
            return REASON_DISABLED_BY_CONFIG
        if not self._acknowledged:
            return REASON_NIGHT_ACKNOWLEDGEMENT_REQUIRED
        return None

    # --- the projection (the fleet loop's half) ----------------------------

    def state(self) -> NightChargeState:
        """Compose the frozen §5 view from the live participation and held
        facts plus the last tick's decision."""
        verdict = self.participation_verdict()
        held = self._adviser.held_intent_id if self._adviser is not None else None
        active = held is not None
        if verdict is not None:
            units: tuple[NightUnitPlan, ...] = ()
            active_units: tuple[str, ...] = ()
            reason_codes: tuple[str, ...] = (verdict,)
        else:
            units = self._last_units
            active_units = self._last_active_units
            reason_codes = self._last_reason_codes
        wall = self._clock.wall_now()
        ends_at = open_window_end(wall, self._windows, self._zone)
        next_at = (
            None if ends_at is not None else next_window_start(wall, self._windows, self._zone)
        )
        return NightChargeState(
            enabled=self._enabled,
            enabled_origin=self._enabled_origin,
            acknowledged_partition=self._acknowledged,
            posture=self._posture,
            active=active,
            phase=self._last_phase,
            window=self._projection_window(wall),
            window_ends_at=ends_at.isoformat() if ends_at is not None else None,
            window_ends_in_s=(
                max(0, int((ends_at - wall).total_seconds())) if ends_at is not None else None
            ),
            next_window_at=next_at.isoformat() if next_at is not None else None,
            pacing=self._pacing,
            rate_cap_w=self._rate_cap_w,
            hold_rate_w=self._hold_rate_w,
            demand_scope=self._demand_scope,
            demand_threshold_w=self._demand_threshold_w,
            demand_w=self._last_demand_w,
            demand_evidence=self._last_evidence,
            held_intent_id=held,
            units=tuple(
                {
                    "unit_id": plan.unit_id,
                    "soc_pct": plan.soc_pct,
                    "phase": plan.phase,
                    "target_w": plan.target_w,
                    "reason": plan.reason,
                }
                for plan in units
            ),
            last_action=self._last_action,
            last_tick_at=self._last_tick_at,
            reason_codes=reason_codes,
            active_unit_ids=active_units,
        )

    def _projection_window(self, wall: Any) -> dict[str, str]:
        """The window the projection names: the open one, else the next."""
        local = wall.astimezone(self._zone)
        second = _second_of_day(local.timetz().replace(tzinfo=None))
        for start, end in self._windows:
            if _pair_covers(second, _second_of_day(start), _second_of_day(end)):
                return {
                    "start_local": start.strftime("%H:%M"),
                    "end_local": end.strftime("%H:%M"),
                    "timezone": self._timezone,
                }
        next_at = next_window_start(wall, self._windows, self._zone)
        next_local = next_at.astimezone(self._zone).timetz().replace(tzinfo=None)
        for start, end in self._windows:
            if start == next_local:
                return {
                    "start_local": start.strftime("%H:%M"),
                    "end_local": end.strftime("%H:%M"),
                    "timezone": self._timezone,
                }
        start, end = self._windows[0]  # pragma: no cover - the pair was found above
        return {
            "start_local": start.strftime("%H:%M"),
            "end_local": end.strftime("%H:%M"),
            "timezone": self._timezone,
        }

    def state_payload(self) -> dict[str, Any]:
        """The §5 JSON shape (the facade/toggle read surface)."""
        return self.state().payload()

    async def observe_tick(self, decision: NightChargeDecision) -> None:
        """The single-writer post-tick update, then the §5 publication.

        Publishes ``night_charge.state_changed`` only when the semantic tuple
        changes — watt and SOC figures ride but never trigger — and, while
        ``enabled``, republishes the full payload as a heartbeat every
        ``heartbeat_period_s``.  While disabled, nothing publishes at all,
        before or after: the state_changed that CARRIES the disable is the
        last event (a boot-composed disabled site never publishes — the
        stream's first snapshot frame carries the projection instead).
        """
        self._last_action = decision.action
        self._last_phase = decision.phase
        self._last_units = tuple(decision.unit_plans)
        self._last_active_units = tuple(decision.active_unit_ids)
        self._last_demand_w = decision.demand_w
        self._last_evidence = decision.demand_evidence
        self._last_reason_codes = tuple(decision.reason_codes)
        self._last_tick_at = self._clock.wall_now().isoformat()
        if self._bus is None:
            return
        state = self.state()
        semantic = state.semantic_tuple()
        now_mono = float(self._clock.monotonic())
        heartbeat = False
        if semantic != self._published_tuple:
            if not state.enabled and self._published_enabled is not True:
                # Disabled (or boot-composed disabled) with no enabled state
                # published before it: no transition to announce.
                return
        elif state.enabled and (
            self._last_publish_mono is None
            or now_mono - self._last_publish_mono >= self._heartbeat_period_s
        ):
            heartbeat = True
        else:
            return
        await self._bus.publish(
            {
                "type": STATE_EVENT_TYPE,
                "payload": {**state.event_payload(), "heartbeat": heartbeat},
            }
        )
        self._published_tuple = semantic
        self._published_enabled = state.enabled
        self._last_publish_mono = now_mono


__all__ = [
    "REASON_DISABLED_BY_CONFIG",
    "REASON_DISABLED_BY_RUNTIME",
    "REASON_NIGHT_ACKNOWLEDGEMENT_REQUIRED",
    "STATE_EVENT_HEARTBEAT_S",
    "STATE_EVENT_TYPE",
    "Action",
    "DemandEvidence",
    "DemandReading",
    "DemandScope",
    "EnabledOrigin",
    "NightChargeAdviser",
    "NightChargeController",
    "NightChargeDecision",
    "NightChargeSettings",
    "NightChargeState",
    "NightChargingRefusal",
    "NightPhase",
    "NightUnitPhase",
    "NightUnitPlan",
    "PacingRule",
    "demand_reading",
    "even_rate_w",
    "in_window",
    "next_window_start",
    "open_window_end",
]
