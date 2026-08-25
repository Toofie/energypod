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

The demand RESPONSE is re-pinned (§2.4, the operator's directive
2026-08-26, the ~00:30 EV-class night): a MEASURED demand engage is an
ACTIVE STAND-DOWN — every demand-eligible unit STAYS IN the submission at
the commissioned ``hold_rate_w``, its renewed charge objective replacing
the pod's CT-following autonomy (the beat-autonomy doctrine), so the
battery discharges nothing and the cheap off-peak grid serves the heavy
load while the stored solar evening is preserved.  The hold rides the
ordinary intent path (never park, never 0x8000), is renewed on the tick
cadence, and lifts below ``threshold - hysteresis`` to the capped pace —
or releases at window close by non-renewal.  The fail-closed polarity is
the unchanged SAFETY DOCTRINE: missing/bad/stale evidence HOLDS at
``hold_rate_w`` too — the evidence-failure fallback rate and never a
demand behavior — because standby answers measured demand, never missing
data.  Both arms land on the same positive hold rate, named apart by
their phase word and evidence code.

V2 (DESIGN_NIGHT_CHARGE_V2) changes WHAT the overnight charge aims at,
never WHEN, never HOW FAST, never UNDER WHOSE AUTHORITY: a per-battery
top-up target computed at window open from the INJECTED
``morning_credit_kwh`` port (section 2.7 — the application layer never
imports a provider adapter), bounded by the reserve floor below and the
unchanged charge ceiling above, one fleet percentage by the
capacity-proportional share collapse (section 2.2), revised mid-window
only on a materially changed forecast under the 2-per-window and
60-minute caps derived from DURABLE revision rows (A6 — a restart mid-
window must not reset the re-target budget), completion ONE-DIRECTIONAL
(falls hold, rises above the measured SOC re-open — A2), and — below
trust or on any forecast failure — fallen back to the v1 ceiling loudly
with its own reason code (section 3.3: every failure of foresight buys
MORE off-peak energy, never less).  ``forecast_suggest`` keeps the
submission math byte-identical to v1 while the projection carries the
suggested target; ``full`` (the default) is v1 identity throughout.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Literal, Protocol, TypeGuard
from uuid import uuid4
from zoneinfo import ZoneInfo

from energypod.domain import DataQuality, Direction, IntentSource, UnitLifecycle
from energypod.domain.audit import AuditEvent

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
# above the threshold and the unit stands down INTO THE HOLD — it stays in
# the submission at ``hold_rate_w``, its renewed objective overriding the
# pod's CT-following autonomy so the grid serves the heavy load (the
# operator's directive 2026-08-26).  ``holding_on_demand`` is the fail-closed
# evidence hold alone: the same positive ``hold_rate_w`` charge kept while
# the demand word is missing/bad/stale, never a demand behavior.
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

# --- V2: the forecast-aware target (DESIGN_NIGHT_CHARGE_V2) -------------------------
#
# The three-state posture (§3.1): ``full`` is v1 identity (the absent-key
# default); ``forecast_suggest`` computes and displays but charges v1
# (byte-identical submission math — a named test); ``forecast_act`` lets the
# computed target govern, entered ONLY by config revision + restart.
TargetPolicy = Literal["full", "forecast_suggest", "forecast_act"]

# §3.3's fail-safe ladder, every rung landing on v1-full targets, loudly:
# the reason codes are additive vocabulary and render as their own tile
# states, never as silence.
REASON_FORECAST_MISSING: Final[str] = "forecast_missing"
REASON_FORECAST_STALE: Final[str] = "forecast_stale"
REASON_FORECAST_NO_LOAD_BASELINE: Final[str] = "forecast_no_load_baseline"
REASON_FORECAST_BELOW_TRUST: Final[str] = "forecast_below_trust"
# §5.4's honest close: the window ended with a unit below its target -- lost
# window time is unrecoverable, the morning solar takes what it takes, and
# the scoreboard prices the miss.
REASON_WINDOW_CLOSED_BELOW_TARGET: Final[str] = "window_closed_below_target"

# §5.2's re-target materiality: |dE_credit| >= max(threshold% of the standing
# credit, the absolute floor) — the floor keeps tiny-forecast noise from
# re-targeting (ruling 4).
_RETARGET_ABSOLUTE_FLOOR_KWH: Final[float] = 0.5
# §5.2's pinned caps: at most TWO re-targets per window, minimum 60 minutes
# apart — derived from DURABLE rows, never runtime counters (A6).
_RETARGET_MAX_PER_WINDOW: Final[int] = 2

_FALLBACK_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_FORECAST_MISSING,
        REASON_FORECAST_STALE,
        REASON_FORECAST_NO_LOAD_BASELINE,
        REASON_FORECAST_BELOW_TRUST,
    }
)

_NIGHT_PRINCIPAL = "energypod:night-adviser"
_NIGHT_POLICY_VERSION = "night-1"


@dataclass(frozen=True, slots=True)
class CreditSlot:
    """One morning slot's netting inputs (the archive row's reconstruction)."""

    start: datetime
    end: datetime
    pv_w: float
    load_w: float


@dataclass(frozen=True, slots=True)
class MorningCredit:
    """The morning AFTER the window, in kWh (§2.7's port result).

    ``e_surplus_kwh``/``e_deficit_kwh`` are the SLOT-NETTED figures over
    [window_end, midday) -- sum(max(0, pv - load) x slot_h) and its mirror --
    with the load half from the same-slot-last-week baseline and NO
    degrade-to-PV-only path (§3.3).  ``failure`` carries the §3.3 ladder's
    word when the credit could not be computed honestly; the adviser falls
    back to v1 targets and never fabricates a zero.
    """

    e_surplus_kwh: float | None
    e_deficit_kwh: float | None
    slots: tuple[CreditSlot, ...]
    source: str | None
    quantile: float | None
    fetched_at: datetime | None
    issued_at: datetime | None
    failure: str | None = None


class _MorningCreditPort(Protocol):
    """§2.7's injected forecast port: ``morning_credit_kwh`` by name and by
    unit, composed from the provider registry by the composition root — the
    application layer never imports a provider adapter (the fitness pin)."""

    async def __call__(
        self, window_end: datetime, midday_local: time, quantile: float
    ) -> MorningCredit: ...


class _TrustStatePort(Protocol):
    """The trust ledger's live word: provisioning | earned | suspended."""

    def __call__(self) -> str: ...


class _AuditAppendPort(Protocol):
    async def append(self, event: Any) -> None: ...


# The durable re-target row sink and read-back (A6: the caps derive from
# DURABLE rows across restarts, never runtime counters).
_RevisionSinkPort = Callable[[date, datetime], None]
_RetargetHistoryPort = Callable[[date], tuple[datetime, ...]]


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
    """One unit's per-tick row: its own SOC, phase, target, and reason.

    ``target_soc_pct`` is the V2 fleet target the row stands against — the
    GOVERNING number under ``forecast_act``, the SUGGESTED display number
    under ``forecast_suggest`` (the submission math stays v1's), and ``None``
    under ``full`` (v1 frames carry no new keys).
    """

    unit_id: str
    soc_pct: float | None
    phase: NightUnitPhase
    target_w: int
    reason: str
    target_soc_pct: float | None = None


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
    # --- V2 (absent under ``full``: v1 consumers see identical frames) -----
    target_soc_pct: float | None = None
    fallback_reason: str | None = None
    forecast: Mapping[str, Any] | None = None
    explanation: str | None = None
    morning_notice: Mapping[str, Any] | None = None


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
    # --- V2 (DESIGN_NIGHT_CHARGE_V2 §6; the defaults keep ``full`` at exact
    # v1 identity — the new keys are never consulted under ``full``) --------
    target_policy: TargetPolicy = "full"
    forecast_quantile: float = 0.1
    midday_local: time = time(12, 0)
    floor_pct: float = 50.0
    charge_efficiency: float = 0.9
    retarget_threshold_pct: float = 20.0
    retarget_min_gap_min: int = 60
    stale_after_s: float = 3600.0


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
        morning_credit: _MorningCreditPort | None = None,
        trust_state: _TrustStatePort | None = None,
        audit: _AuditAppendPort | None = None,
        revision_sink: _RevisionSinkPort | None = None,
        retarget_history: _RetargetHistoryPort | None = None,
        morning_archive_sink: Callable[[Any], None] | None = None,
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
        # --- V2: the injected forecast port (§2.7), the trust word, and the
        # durable revision rows (A6).  All optional so isolated v1
        # compositions stay byte-identical; a forecast posture without its
        # port fails safe to full targets with forecast_missing.
        self._morning_credit = morning_credit
        self._trust_state = trust_state
        self._audit = audit
        self._revision_sink = revision_sink
        self._retarget_history = retarget_history
        self._morning_archive_sink = morning_archive_sink
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
        # --- V2 window-scoped target state (§2/§5.2/§5.1) ------------------
        # The fleet percentage the window aims at (None = v1 ceiling), the
        # basis it stands on, the §3.3 ladder's word once the computation is
        # abandoned for the window, and the one-directional completion latch
        # (per-unit target at completion — a RISE above the measured SOC is
        # the only re-entry, A2).
        self._window_target: float | None = None
        self._window_credit: MorningCredit | None = None
        self._window_evaluated_fetch: datetime | None = None
        self._window_archived: bool = False
        self._window_abandoned: str | None = None
        self._completed_targets: dict[str, float] = {}
        self._window_final_soc: dict[str, float] = {}
        self._window_morning_date: date | None = None
        self._pending_notice: dict[str, Any] | None = None

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
            # ~3.5-4.0 s watchdog return each pod to its own autonomy.  A
            # forecast window that closed below target leaves the §5.4/A5
            # morning notice on this close frame (the projection latches it
            # until midday_local).
            notice = self._pending_notice
            self._pending_notice = None
            codes: tuple[str, ...] = ("outside_window",)
            if notice is not None:
                codes = ("outside_window", REASON_WINDOW_CLOSED_BELOW_TARGET)
            return await self._standby(
                "idle",
                False,
                _NO_READING,
                codes,
                (),
                morning_notice=notice,
            )

        # --- V2: the window's forecast evaluation (§2 at window open, §5.2
        # on a qualifying revision).  BEFORE the observation walk: the §7
        # ``night_target_set`` row is written at the FIRST in-window tick
        # whatever the fleet's eligibility, and the ladder's failures ride
        # every downstream reason code.
        await self._evaluate_forecast(wall)

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
            # The ACTIVE STAND-DOWN participates: a measured-demand hold rides
            # the same submission as the pacing charge (at ``hold_rate_w``),
            # which is exactly what overrides the pod's own autonomy.
            if plan.phase in ("pacing", "holding_on_demand", "standing_by_on_demand"):
                participating.append(unit_id)
            if unit_held:
                held_units.append(unit_id)
                if plan.phase == "standing_by_on_demand":
                    standing_units.append(unit_id)
        for plan in plans:
            # The §5.4 close evaluation reads each participant's last
            # measured SOC this window.
            if plan.soc_pct is not None and _finite_number(plan.soc_pct):
                self._window_final_soc[plan.unit_id] = float(plan.soc_pct)

        if not participating:
            # A stand-down unit always participates now (its hold IS the
            # submission), so this is the completion/sit-out close only.
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
        if self._window_abandoned is not None:
            # §3.3's loud fallback: the ladder's word rides every frame the
            # window publishes after the computation was abandoned.
            reason_codes = (*reason_codes, self._window_abandoned)

        action: Action = "renew" if self._submitted_this_window else "propose"
        targets_by_unit = {
            plan.unit_id: plan.target_w
            for plan in plans
            if plan.phase in ("pacing", "holding_on_demand", "standing_by_on_demand")
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
            target_soc_pct=self._decision_target_pct(),
            fallback_reason=self._window_abandoned,
            forecast=self._forecast_projection(),
            explanation=self._explanation(),
        )

    # --- internals -------------------------------------------------------

    def _roll_window_state(self, window_now: bool) -> None:
        """Reset the window-scoped latches at every boundary crossing."""
        if window_now == self._window_open:
            return
        if self._window_open and not window_now and self._window_target is not None:
            # §5.4/A5: a forecast window closing below target leaves the
            # honest notice -- lost window time is unrecoverable, the morning
            # solar takes what it takes, and the scoreboard prices the miss.
            # Only PARTICIPANTS can have fallen short; a completed unit
            # reached what it reached.
            below = sorted(
                unit_id
                for unit_id in self._participated
                if self._window_final_soc.get(unit_id, 0.0) < (self._window_target or 0.0)
            )
            if below and self._window_morning_date is not None:
                self._pending_notice = {
                    "date": self._window_morning_date.isoformat(),
                    "target_soc_pct": self._window_target,
                    "units_below_target": below,
                    "until_local": self._settings.midday_local.strftime("%H:%M"),
                }
        self._window_open = window_now
        self._holding_fleet = False
        self._holding_units = frozenset()
        self._participated = frozenset()
        self._submitted_this_window = False
        self._window_target = None
        self._window_credit = None
        self._window_evaluated_fetch = None
        self._window_archived = False
        self._window_abandoned = None
        self._completed_targets = {}
        self._window_final_soc = {}
        self._window_morning_date = None

    # --- V2: the forecast evaluation (§2 at open, §5.2 on revision) ------

    async def _evaluate_forecast(self, wall: datetime) -> None:
        """Set or revise the window's fleet target from the morning credit.

        At the first in-window tick this is the §2 computation (with the §7
        ``night_target_set`` archive row); on a NEW ``fetched_at`` it is the
        §5.2 revision under the materiality rule and the durable caps.  Any
        §3.3 rung abandons the computation FOR THE WINDOW: targets revert to
        the v1 ceiling for every unit, loudly, and no archive row is written
        (A12 — a fallback night leaves nothing to score).
        """
        if self._settings.target_policy == "full":
            return  # v1 identity: no forecast is consumed at all
        window_end = open_window_end(wall, self._settings.windows, self._zone)
        if window_end is None:  # pragma: no cover - tick only runs in-window
            return
        morning_date = window_end.astimezone(self._zone).date()
        credit: MorningCredit
        if self._morning_credit is None:
            credit = _no_credit(REASON_FORECAST_MISSING)
        else:
            try:
                credit = await self._morning_credit(
                    window_end, self._settings.midday_local, self._settings.forecast_quantile
                )
            except Exception:
                # T-NC2-ARCHITECTURE: a provider failure is no-credit is
                # fallback — never a crash, never a fabricated zero.
                credit = _no_credit(REASON_FORECAST_MISSING)
        if self._window_morning_date is None:
            self._window_morning_date = morning_date
        if credit.failure is not None:
            self._abandon(credit.failure)
            return
        if (
            credit.fetched_at is None
            or credit.e_surplus_kwh is None
            or credit.e_deficit_kwh is None
        ):
            self._abandon(REASON_FORECAST_MISSING)
            return
        now = self._clock.wall_now()
        if (now - credit.fetched_at).total_seconds() > float(self._settings.stale_after_s):
            # §3.3: a cached 12 h-old forecast is legitimate; a 26 h-old one
            # is not (the provider layer's own stale_after_s).
            self._abandon(REASON_FORECAST_STALE)
            return
        # §3.3's last rung, scoped to the governing path (the §3.1 display
        # runs from day one under SUGGEST; an unearned ACT site runs the
        # fallback per §6 until the scoreboard earns).
        trust = self._trust_state() if self._trust_state is not None else "provisioning"
        if trust == "suspended" or (
            self._settings.target_policy == "forecast_act" and trust != "earned"
        ):
            self._abandon(REASON_FORECAST_BELOW_TRUST)
            return
        if self._window_credit is None:
            await self._set_window_target(credit, morning_date, window_end)
            return
        if (
            self._window_evaluated_fetch is not None
            and credit.fetched_at != self._window_evaluated_fetch
        ):
            await self._maybe_revise(credit, morning_date, now)

    def _abandon(self, reason: str) -> None:
        """§3.3: abandon the forecast computation for the window, loudly."""
        self._window_abandoned = reason
        self._window_target = None
        self._window_credit = None

    async def _set_window_target(
        self, credit: MorningCredit, morning: date, window_end: datetime
    ) -> None:
        """The §2 computation at window open, plus the §7 archive row."""
        target, bound_by = self._compute_target(credit)
        self._window_target = target
        self._window_credit = credit
        self._window_evaluated_fetch = credit.fetched_at
        trust = self._trust_state() if self._trust_state is not None else None
        await self._window_archive_row(
            credit, morning, window_end, target, bound_by, trust, event_type="night_target_set"
        )
        self._window_archived = True
        if self._morning_archive_sink is not None:
            # The durable machine-truth twin (the park-lease doctrine): the
            # trust evaluator scores THIS morning from it post-midday.
            from energypod.domain.night_trust import ArchivedMorning

            with contextlib.suppress(Exception):
                self._morning_archive_sink(
                    ArchivedMorning(
                        date=morning,
                        provider=credit.source or "forecast",
                        target_policy=self._settings.target_policy,
                        quantile=credit.quantile,
                        window_end=window_end.astimezone(UTC),
                        midday=self._midday_instant(window_end).astimezone(UTC),
                        e_surplus_forecast_kwh=credit.e_surplus_kwh or 0.0,
                        e_deficit_kwh=credit.e_deficit_kwh or 0.0,
                    )
                )

    async def _maybe_revise(
        self, credit: MorningCredit, morning: date, now: datetime
    ) -> None:
        """§5.2: a materially changed forecast re-targets the still-charging
        units, both directions, under the DURABLE caps (A6)."""
        assert self._window_credit is not None  # guarded by the caller
        standing = self._credit_kwh(self._window_credit)
        arriving = self._credit_kwh(credit)
        delta = abs(arriving - standing)
        floor = max(
            float(self._settings.retarget_threshold_pct) / 100.0 * abs(standing),
            _RETARGET_ABSOLUTE_FLOOR_KWH,
        )
        if delta < floor:
            # Tiny-forecast noise (ruling 4's absolute floor): the fetch is
            # seen, the target stands.
            self._window_evaluated_fetch = credit.fetched_at
            return
        history = (
            ()
            if self._retarget_history is None
            else tuple(self._retarget_history(morning))
        )
        if len(history) >= _RETARGET_MAX_PER_WINDOW:
            return  # the window's durable budget is spent
        if history:
            gap_s = (now - history[-1]).total_seconds()
            if gap_s < float(self._settings.retarget_min_gap_min) * 60.0:
                return  # inside the pinned minimum gap
        previous_target = self._window_target
        previous = self._window_credit
        target, _bound = self._compute_target(credit)
        self._window_target = target
        self._window_credit = credit
        self._window_evaluated_fetch = credit.fetched_at
        if self._revision_sink is not None:
            with contextlib.suppress(Exception):
                self._revision_sink(morning, now)
        trust = self._trust_state() if self._trust_state is not None else None
        await self._revision_row(
            credit,
            morning,
            now,
            previous_target=previous_target,
            new_target=target,
            delta_kwh=arriving - standing,
            trust=trust,
            previous_fetched_at=None if previous is None else previous.fetched_at,
        )

    def _credit_kwh(self, credit: MorningCredit) -> float:
        """§2.1's credit: the η-derated surplus minus the full-value deficit."""
        surplus = credit.e_surplus_kwh or 0.0
        deficit = credit.e_deficit_kwh or 0.0
        return float(self._settings.charge_efficiency) * surplus - deficit

    def _compute_target(self, credit: MorningCredit) -> tuple[float, str | None]:
        """The §2.1 formula and §7's ``ceiling_bound_by`` decomposition.

        The capacity-proportional share collapses algebraically to ONE fleet
        percentage (section 2.2): 100 - 100 x E_credit x 1000 / fleet Wh, clamped to
        [floor_pct, min(100, policy.max_soc_pct)].
        """
        capacities = self._settings.assumed_capacity_wh or {}
        total_wh = sum(int(value) for value in capacities.values() if isinstance(value, int))
        if total_wh <= 0:  # pragma: no cover - config validation requires the map
            return float(self._policy.max_soc_pct), "sky"
        raw = 100.0 - 100.0 * self._credit_kwh(credit) * 1000.0 / float(total_wh)
        ceiling = min(100.0, float(self._policy.max_soc_pct))
        floor = float(self._settings.floor_pct)
        target = min(max(raw, floor), ceiling)
        bound_by: str | None = None
        if raw > ceiling:
            # A10's decomposition: the SKY gave no surplus at all, or a real
            # surplus was eaten by the deficit/η netting.
            bound_by = "sky" if (credit.e_surplus_kwh or 0.0) <= 0.0 else "netting"
        return target, bound_by

    def _decision_target_pct(self) -> float | None:
        """The frame's target number: the governing target under ACT, the
        suggested number under SUGGEST (v1 ceiling behavior either way)."""
        if self._settings.target_policy == "full" or self._window_abandoned is not None:
            return None
        return self._window_target

    def _unit_target_pct(self) -> float | None:
        """The per-unit display target; None under full or fallback."""
        return self._decision_target_pct()

    def _governing_target_pct(self) -> float | None:
        """The target that GOVERNS behavior: ACT with a live forecast only —
        under SUGGEST the submission math is v1's (the named byte-identity),
        and a fallback window is v1 by §3.3."""
        if (
            self._settings.target_policy == "forecast_act"
            and self._window_abandoned is None
            and self._window_target is not None
        ):
            return self._window_target
        return None

    def _forecast_projection(self) -> dict[str, Any] | None:
        """The §7 ``forecast`` block (absent under full)."""
        if self._settings.target_policy == "full":
            return None
        credit = self._window_credit
        if credit is None or self._window_abandoned is not None:
            if self._window_abandoned is None:
                return None
            return {
                "status": self._window_abandoned,
                "quantile": self._settings.forecast_quantile,
                "midday_local": self._settings.midday_local.strftime("%H:%M"),
            }
        return {
            "status": "ok",
            "source": credit.source,
            "quantile": credit.quantile,
            "issued_at": None if credit.issued_at is None else credit.issued_at.isoformat(),
            "fetched_at": None
            if credit.fetched_at is None
            else credit.fetched_at.isoformat(),
            "e_surplus_kwh": credit.e_surplus_kwh,
            "e_deficit_kwh": credit.e_deficit_kwh,
            "e_credit_kwh": self._credit_kwh(credit),
            "midday_local": self._settings.midday_local.strftime("%H:%M"),
            "ceiling_bound_by": self._compute_target(credit)[1],
        }

    def _explanation(self) -> str | None:
        """The one-sentence §8 reasoning line, verbatim on the projection."""
        if self._settings.target_policy == "full" or self._window_abandoned is not None:
            return None
        credit = self._window_credit
        target = self._window_target
        if credit is None or target is None:
            return None
        first_unit = self._settings.unit_ids[0] if self._settings.unit_ids else "fleet"
        window_end = open_window_end(
            self._clock.wall_now(), self._settings.windows, self._zone
        )
        end_wall = "--:--" if window_end is None else window_end.astimezone(self._zone).strftime(
            "%H:%M"
        )
        source = credit.source or "forecast"
        quantile_word = (
            "p50"
            if credit.quantile is None or credit.quantile == 0.5
            else f"p{round((credit.quantile or 0.0) * 100)}"
        )
        issued = "" if credit.issued_at is None else f", issued {credit.issued_at.isoformat()}"
        return (
            f"{first_unit} to {target:.0f}% by {end_wall} — "
            f"{self._credit_kwh(credit):.1f} kWh forecast surplus by "
            f"{self._settings.midday_local.strftime('%H:%M')} finishes it "
            f"({source} {quantile_word}{issued})"
        )

    # --- V2: the audit rows (§7, the accountant pattern) ------------------

    def _night_row(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        reason_codes: tuple[str, ...],
        result: str,
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now().astimezone(UTC)
        return AuditEvent(
            event_id=f"night-{event_type}-{uuid4().hex}",
            occurred_at=wall,
            monotonic_offset_s=now_mono,
            process_instance_id=_NIGHT_PRINCIPAL,
            event_type=event_type,
            principal=_NIGHT_PRINCIPAL,
            correlation_id=f"night:{event_type}",
            policy_version=_NIGHT_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_night_fingerprint({"event_type": event_type, **payload}),
            response_fingerprint=_night_fingerprint({"result": result}),
            result=result,
            lifecycle=UnitLifecycle.DISARMED,
            payload=payload,
        )

    async def _window_archive_row(
        self,
        credit: MorningCredit,
        morning: date,
        window_end: datetime,
        target: float,
        bound_by: str | None,
        trust: str | None,
        *,
        event_type: str,
    ) -> None:
        """The §7 ``night_target_set`` reconstruction row: the full
        arithmetic, the forecast series verbatim, and the trust snapshot."""
        payload = {
            "window_date": morning.isoformat(),
            "window_end": window_end.astimezone(UTC).isoformat(),
            "midday": self._midday_instant(window_end).isoformat(),
            "window_end_local": window_end.astimezone(self._zone).strftime("%H:%M"),
            "midday_local": self._settings.midday_local.strftime("%H:%M"),
            "target_policy": self._settings.target_policy,
            "quantile": self._settings.forecast_quantile,
            "e_surplus_forecast_kwh": credit.e_surplus_kwh,
            "e_deficit_kwh": credit.e_deficit_kwh,
            "e_credit_kwh": self._credit_kwh(credit),
            "target_soc_pct": target,
            "ceiling_bound_by": bound_by,
            "share_model": "capacity_proportional",
            "assumed_capacity_wh": dict(sorted((self._settings.assumed_capacity_wh or {}).items())),
            "charge_efficiency": self._settings.charge_efficiency,
            "floor_pct": self._settings.floor_pct,
            "trust_state": trust,
            "forecast": {
                "source": credit.source,
                "quantile": credit.quantile,
                "fetched_at": None
                if credit.fetched_at is None
                else credit.fetched_at.isoformat(),
                "issued_at": None
                if credit.issued_at is None
                else credit.issued_at.isoformat(),
            },
            "slots": [
                {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "pv_w": slot.pv_w,
                    "load_w": slot.load_w,
                }
                for slot in credit.slots
            ],
        }
        await self._append_night_row(event_type, payload, ("window_open",), "target_set")

    async def _revision_row(
        self,
        credit: MorningCredit,
        morning: date,
        revised_at: datetime,
        *,
        previous_target: float | None,
        new_target: float,
        delta_kwh: float,
        trust: str | None,
        previous_fetched_at: datetime | None,
    ) -> None:
        direction = "raise" if new_target > (previous_target or 0.0) else "lower"
        payload = {
            "window_date": morning.isoformat(),
            "revised_at": revised_at.astimezone(UTC).isoformat(),
            "direction": direction,
            "previous_target_soc_pct": previous_target,
            "target_soc_pct": new_target,
            "delta_credit_kwh": delta_kwh,
            "trust_state": trust,
            "forecast": {
                "source": credit.source,
                "quantile": credit.quantile,
                "fetched_at": None
                if credit.fetched_at is None
                else credit.fetched_at.isoformat(),
                "previous_fetched_at": None
                if previous_fetched_at is None
                else previous_fetched_at.isoformat(),
                "issued_at": None
                if credit.issued_at is None
                else credit.issued_at.isoformat(),
            },
        }
        await self._append_night_row("night_target_revised", payload, (direction,), "revised")

    async def _append_night_row(
        self, event_type: str, payload: dict[str, Any], reasons: tuple[str, ...], result: str
    ) -> None:
        if self._audit is None:
            return
        event = self._night_row(
            event_type=event_type, payload=payload, reason_codes=reasons, result=result
        )
        with contextlib.suppress(Exception):
            await self._audit.append(event)

    def _midday_instant(self, window_end: datetime) -> datetime:
        """The civil midday of the MORNING the window belongs to."""
        local_end = window_end.astimezone(self._zone)
        return datetime.combine(local_end.date(), self._settings.midday_local, tzinfo=self._zone)

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
        ``_plan_unit`` names the RESPONSE (the measured active stand-down or
        the evidence-failure hold — both at ``hold_rate_w``, named apart by
        phase and code).
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
        only a MEASURED demand names its phase the active stand-down
        (``standing_by_on_demand``) — a fail-closed hold (any non-good
        word) is the evidence-failure phase (``holding_on_demand``), never
        dressed up as a response to demand the adviser cannot see.
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
        display_target = self._unit_target_pct()
        governing = self._governing_target_pct()
        # V2's widened completion word (§5.1): the CEILING case keeps its own
        # words (at_ceiling + skipped_full); a unit at or above a governing
        # forecast target is complete/target_reached -- SOC above target is
        # complete, NEVER pacing (A8).  Completion is ONE-DIRECTIONAL (A2):
        # sticky against target FALLS, re-opened only by a target RISE above
        # the unit's MEASURED SOC.
        completed_target = self._completed_targets.get(unit_id)
        if (
            completed_target is not None
            and governing is not None
            and governing > float(soc_pct)
            and governing > completed_target
        ):
            del self._completed_targets[unit_id]  # the only re-entry (§5.1)
            completed_target = None
        if governing is not None and float(soc_pct) >= governing and completed_target is None:
            self._completed_targets[unit_id] = governing
            completed_target = governing
        if soc_pct >= self._policy.max_soc_pct:
            if unit_id in self._participated or completed_target is not None:
                # It charged this window and reached the target: the honest
                # completion row, not a from-the-start skip.
                return (
                    NightUnitPlan(
                        unit_id, soc_pct, "complete", 0, "target_reached", display_target
                    ),
                    False,
                    False,
                )
            return (
                NightUnitPlan(unit_id, soc_pct, "skipped_full", 0, "at_ceiling", display_target),
                False,
                False,
            )
        if completed_target is not None:
            # Sticky against the fall: the unit stays complete even if its SOC
            # dipped under a LOWERED target (un-charging is not a thing).
            return (
                NightUnitPlan(unit_id, soc_pct, "complete", 0, "target_reached", display_target),
                False,
                False,
            )
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
                # MEASURED demand engages the ACTIVE STAND-DOWN (the
                # operator's directive 2026-08-26, the ~00:30 EV-class
                # night): the unit STAYS IN the submission at
                # ``hold_rate_w`` — its renewed charge objective replaces
                # the pod's CT-following autonomy (beat-autonomy doctrine),
                # so the battery discharges nothing and the cheap off-peak
                # grid serves the heavy load while the stored solar evening
                # is preserved.  Ordinary intent traffic — the kernel's own
                # denials still apply untouched and a denied unit is simply
                # not held, never fought; the hold lifts below
                # threshold - hysteresis back to the capped pace, or
                # releases at window close by non-renewal.
                return (
                    NightUnitPlan(
                        unit_id,
                        soc_pct,
                        "standing_by_on_demand",
                        int(self._settings.hold_rate_w),
                        "demand_above_threshold",
                        display_target,
                    ),
                    False,
                    True,
                )
            # The fail-closed evidence hold: the same positive
            # ``hold_rate_w`` charge the measured stand-down dispatches,
            # but keyed on NOTHING (the evidence-failure fallback rate
            # alone), its renewed objective preserving the no-cycling
            # guarantee until a GOOD word returns.
            return (
                NightUnitPlan(
                    unit_id,
                    soc_pct,
                    "holding_on_demand",
                    int(self._settings.hold_rate_w),
                    "demand_above_threshold",
                    display_target,
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
            # §4-5: under `even` the forecast target is just a smaller
            # target_soc_pct in the same self-correcting deadline rule; under
            # suggest/fallback the ceiling stays the deadline's aim.
            rate, at_risk = even_rate_w(
                soc_pct=float(soc_pct),
                target_soc_pct=float(
                    governing if governing is not None else self._policy.max_soc_pct
                ),
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
                    display_target,
                ),
                at_risk,
                False,
            )
        return (
            NightUnitPlan(unit_id, soc_pct, "pacing", achievable, "on_plan", display_target),
            False,
            False,
        )

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
            unit_id,
            soc if _finite_number(soc) else None,
            "sitting_out",
            0,
            reason,
            self._unit_target_pct(),
        )

    async def _standby(
        self,
        phase: NightPhase,
        in_window: bool,
        reading: DemandReading,
        reason_codes: tuple[str, ...],
        plans: tuple[NightUnitPlan, ...],
        *,
        morning_notice: Mapping[str, Any] | None = None,
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
            target_soc_pct=None if not in_window else self._decision_target_pct(),
            fallback_reason=None if not in_window else self._window_abandoned,
            forecast=self._forecast_projection() if in_window else None,
            explanation=None if not in_window else self._explanation(),
            morning_notice=morning_notice,
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


def _no_credit(failure: str) -> MorningCredit:
    """The honest absence: no figures, no provenance, the ladder's word."""
    return MorningCredit(
        e_surplus_kwh=None,
        e_deficit_kwh=None,
        slots=(),
        source=None,
        quantile=None,
        fetched_at=None,
        issued_at=None,
        failure=failure,
    )


def _night_fingerprint(facts: Mapping[str, Any]) -> str:
    import hashlib
    import json

    encoded = json.dumps(facts, sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    # ADDITIVE (the 2026-08-26 active stand-down): the exit bound is
    # ``threshold - hysteresis`` and the tile says it in watts — the figure
    # rides the wire so no client ever fabricates the resume number.
    demand_exit_hysteresis_w: int
    demand_w: int | None
    demand_evidence: DemandEvidence
    held_intent_id: str | None
    units: tuple[dict[str, Any], ...]
    last_action: Action
    last_tick_at: str
    reason_codes: tuple[str, ...]
    active_unit_ids: tuple[str, ...] = ()
    # --- V2 (§7, additive; ABSENT under ``full`` so v1 consumers see
    # byte-identical frames) -----------------------------------------------
    target_policy: TargetPolicy = "full"
    trust: Mapping[str, Any] | None = None
    forecast: Mapping[str, Any] | None = None
    explanation: str | None = None
    morning_notice: Mapping[str, Any] | None = None
    suggest_posture: bool = False

    def payload(self) -> dict[str, Any]:
        """The §5 JSON shape (values JSON-native, codes as a list)."""
        payload = {
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
            "demand_exit_hysteresis_w": self.demand_exit_hysteresis_w,
            "demand_w": self.demand_w,
            "demand_evidence": self.demand_evidence,
            "held_intent_id": self.held_intent_id,
            "units": [dict(unit) for unit in self.units],
            "last_action": self.last_action,
            "last_tick_at": self.last_tick_at,
            "reason_codes": list(self.reason_codes),
        }
        if self.target_policy != "full":
            payload["target_policy"] = self.target_policy
            payload["trust"] = None if self.trust is None else dict(self.trust)
            payload["forecast"] = None if self.forecast is None else dict(self.forecast)
            payload["explanation"] = self.explanation
            payload["morning_notice"] = (
                None if self.morning_notice is None else dict(self.morning_notice)
            )
        return payload

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
            # §7's exactly-two V2 members: the posture and the trust word.
            # Targets and figures ride every publication but never trigger.
            self.target_policy,
            None if self.trust is None else self.trust.get("state"),
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
        demand_exit_hysteresis_w: int,
        windows: tuple[tuple[time, time], ...],
        timezone: str,
        posture: str,
        clock: _WallClock,
        acknowledged_partition: bool,
        config_enabled: bool,
        bus: _EventPublisherPort | None = None,
        heartbeat_period_s: float = STATE_EVENT_HEARTBEAT_S,
        target_policy: TargetPolicy = "full",
        midday_local: time = time(12, 0),
        trust_view: Callable[[], Mapping[str, Any] | None] | None = None,
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
        self._demand_exit_hysteresis_w = int(demand_exit_hysteresis_w)
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
        # --- V2: the posture the projection names, the morning notice's
        # clear line (midday), and the live trust view.  The defaults keep
        # v1 frames byte-identical (the additive keys never appear).
        self._target_policy: TargetPolicy = target_policy
        self._midday_local = midday_local
        self._trust_view = trust_view
        self._morning_notice: dict[str, Any] | None = None
        self._last_forecast: Mapping[str, Any] | None = None
        self._last_explanation: str | None = None
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
            demand_exit_hysteresis_w=self._demand_exit_hysteresis_w,
            demand_w=self._last_demand_w,
            demand_evidence=self._last_evidence,
            held_intent_id=held,
            units=tuple(self._unit_rows(plan) for plan in units),
            last_action=self._last_action,
            last_tick_at=self._last_tick_at,
            reason_codes=reason_codes,
            active_unit_ids=active_units,
            target_policy=self._target_policy,
            trust=self._trust_payload(),
            forecast=self._last_forecast,
            explanation=self._last_explanation,
            morning_notice=self._live_morning_notice(wall),
        )

    def _unit_rows(self, plan: NightUnitPlan) -> dict[str, Any]:
        """One unit's §7 row; the V2 target key is ADDITIVE and named by the
        posture (``suggested_`` under SUGGEST — a number that does not govern
        MUST say so beside itself)."""
        row: dict[str, Any] = {
            "unit_id": plan.unit_id,
            "soc_pct": plan.soc_pct,
            "phase": plan.phase,
            "target_w": plan.target_w,
            "reason": plan.reason,
        }
        if self._target_policy != "full" and plan.target_soc_pct is not None:
            key = (
                "suggested_target_soc_pct"
                if self._target_policy == "forecast_suggest"
                else "target_soc_pct"
            )
            row[key] = plan.target_soc_pct
        return row

    def _trust_payload(self) -> Mapping[str, Any] | None:
        """The live trust view (the ledger's §7 block), read per frame."""
        if self._target_policy == "full" or self._trust_view is None:
            return None
        try:
            payload = self._trust_view()
        except Exception:
            return None
        return payload

    def _live_morning_notice(self, wall: Any) -> Mapping[str, Any] | None:
        """The latched §8/A5 morning notice, cleared at ``midday_local``.

        The notice persists on the Night tile until midday -- the window's
        honest below-target close stays visible while solar finishes what it
        can, then the Insights landing line takes over.
        """
        if self._morning_notice is None:
            return None
        local = wall.astimezone(self._zone)
        notice_date = self._morning_notice.get("date")
        if isinstance(notice_date, str):
            try:
                morning = date.fromisoformat(notice_date)
            except ValueError:
                morning = None  # pragma: no cover - the adviser writes ISO dates
            if morning is not None:
                midday = datetime.combine(morning, self._midday_local, tzinfo=self._zone)
                if local >= midday:
                    self._morning_notice = None
                    return None
        return self._morning_notice

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
        # --- V2: the forecast mirror and the once-per-window morning notice
        # latch (§8/A5 -- the projection holds it until midday_local).
        self._last_forecast = (
            None if decision.forecast is None else dict(decision.forecast)
        )
        self._last_explanation = decision.explanation
        if decision.morning_notice is not None:
            self._morning_notice = dict(decision.morning_notice)
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
