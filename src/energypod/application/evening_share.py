"""The evening load-sharing program — the netted-meter fleet discharge adviser.

DESIGN_EVENING_LOAD_SHARING (CONTRACT v1.1).  An ``EveningShareAdviser``
composed exactly when the ``evening_load_sharing:`` block is PRESENT, ticking
once per fleet cycle inside the existing bounded supervision pass (no new task
class), inside the evening window (16:00-22:30 by default).

What this module is, pinned (the contract's own §0/§2):

- **An ADVISER, not a schedule entry.**  The split is submitted as ONE renewed
  short-TTL ``OPTIMIZER`` DISCHARGE intent with ``watts_by_unit`` through the
  facade's internal evening twin under the composed principal
  ``energypod:evening-adviser`` — judged by the arbiter, allocator,
  SafetyKernel, and actor exactly as the night adviser's charge intents are.
  Every stop is NON-RENEWAL: no stop triples, no idle intents, no zero-watt
  submissions, ever.
- **NO mode register anywhere.**  Values 2-6 of ``0x8000`` stay permanently
  prohibited; this module holds no transport, no arm reach, and no write
  primitive of its own.
- **Fail-closed is WITHDRAW, not hold** (§2): a discharge held on stale words
  is unbounded export risk, so the unjudgeable state is non-renewal — the
  intents lapse, the watchdog hands every pod back to its own CT-following
  autonomy (the status quo ante).
- **The control basis is DERIVED, both words measured** (§3.3): the AC-bus
  conservation identity ``work_w = served_w + SIGNED net exchange``, with the
  excluded units' flow subtracted (``elsewhere_w``, E2) and NO clipping
  anywhere in the loop (E1 — zero netted exchange is the loop's sole
  equilibrium; the export direction is the self-correcting one).
- **Convergence is the WEIGHTING, never a watt** (§5): SoC^exponent x capacity
  shares, the participant set the smallest highest-weight set that can carry
  the total inside ``[min_share_w, cap_w]`` — efficiency-aware by construction
  (E7: larger shares on fewer pods).

The pinned sentence (§0), verbatim on every surface this feature adds:
*"The meter nets — every commanded watt exists to cancel a metered watt;
convergence is the weighting's side-effect, and no watt is ever exported
for it."*
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from energypod.application.site_meter import SiteMeterPort
from energypod.domain.audit import AuditEvent
from energypod.domain.intents import Direction
from energypod.domain.observations import UnitLifecycle

# The composed automation principal (composition supplies the real principal
# for the facade twin; audit rows under this subject are the evening
# adviser's own).
EVENING_ADVISER_PRINCIPAL: Final[str] = "energypod:evening-adviser"
_EVENING_POLICY_VERSION: Final[str] = "evening-share-1"

#: The facade twin's pinned intent-id prefix: an OPTIMIZER intent carrying it
#: is this adviser's own held intent (C3 — never a foreign claim to skip on;
#: the third twin after ``night-`` and ``cal-``).
_OWN_INTENT_PREFIX: Final[str] = "els-"

#: §0's pinned sentence, VERBATIM — it rides every surface this feature adds.
PINNED_SENTENCE: Final[str] = (
    "The meter nets — every commanded watt exists to cancel a metered watt; "
    "convergence is the weighting's side-effect, and no watt is ever exported "
    "for it."
)
#: The C16 stop route, named wherever the live share renders: the operator's
#: existing authority (a manual claim preempts instantly) is the route; no new
#: kill switch exists or is wanted.
STOP_ROUTE_SENTENCE: Final[str] = (
    "to stop tonight's sharing, claim any pod — a manual command preempts "
    "instantly"
)

EVENING_STATE_EVENT_TYPE: Final[str] = "evening.state_changed"
EVENING_NOT_COMMISSIONED: Final[str] = "evening_share_not_commissioned"

#: §2: the heartbeat cadence is a constant, not a config key (the night
#: publication discipline).
STATE_EVENT_HEARTBEAT_S: Final[float] = 30.0

# §9's severity tiers (the health watch's own words).
TIER_NOTICE: Final[str] = "notice"
TIER_ALERT: Final[str] = "alert"
TIER_RESOLVED: Final[str] = "resolved"

# §6.2's E4 constant: the delivery pass fraction (a stated constant, never a
# key — a participant below this fraction of its commanded share for
# ``non_delivery_ticks`` consecutive ticks is excluded ``not_delivering``).
DELIVERY_PASS_FRACTION: Final[float] = 0.5

# §5.3's stated constants (never keys): the join/leave hysteresis factor and
# the §6.1 disengage edge it mirrors.
JOIN_HYSTERESIS_FRACTION: Final[float] = 0.8

# The claim-settle debounce multiplier (E5): a unit enters the participant set
# only after ``2 x intent_ttl_s`` continuously claim-free — a stated constant,
# never a key.
CLAIM_SETTLE_TTL_MULTIPLE: Final[float] = 2.0

_CLAIMING_SOURCES: Final[frozenset[str]] = frozenset(
    {"manual", "agent", "schedule", "optimizer", "emergency_stop"}
)
_PREEMPTING_SOURCES: Final[frozenset[str]] = frozenset({"manual", "agent", "emergency_stop"})

# The basis's fail-closed words — the excess rollup's own family (worst word
# wins: missing > bad > stale > good), reused under the kernel's freshness
# bound (E10: ``policy.max_telemetry_age_s`` is the ONE freshness truth; there
# is deliberately no ``grid_telemetry_max_age_s`` key anywhere).
BasisEvidence = str  # "good" | "stale" | "bad" | "missing"
_BASIS_EVIDENCE_RANK: Final[dict[str, int]] = {
    "good": 0,
    "stale": 1,
    "bad": 2,
    "missing": 3,
}

Phase = str  # idle | sharing | capability_limited | withdrawn
UnitPhase = str  # sharing | sitting_out
AdviseMode = str  # "advise" | "act"

# The reason vocabulary (§6.2/§6.3/§7.1), pinned verbatim.
REASON_ON_PLAN: Final[str] = "on_plan"
REASON_OUTSIDE_WINDOW: Final[str] = "outside_window"
REASON_BELOW_FLOOR: Final[str] = "below_one_pod_floor"
REASON_NO_ELIGIBLE: Final[str] = "no_eligible_units"
REASON_YIELDING: Final[str] = "yielding_to_higher_priority"
REASON_CAPABILITY: Final[str] = "capability_limited"
REASON_CLAIM_SETTLING: Final[str] = "claim_settling"
REASON_NOT_DELIVERING: Final[str] = "not_delivering"
REASON_SOC_FLOOR: Final[str] = "soc_floor"
REASON_IMPLAUSIBLE: Final[str] = "grid_evidence_implausible"
# v1.2/A1/A8: the one additive code — the fresh site-meter basis was LOST
# (stale/unavailable reading) and the pod-word derived basis took over for
# this tick.  Latched per loss-episode so it is loud once, not per-tick spam.
REASON_SITE_METER_DEGRADED: Final[str] = "site_meter_degraded"


class EveningShareRefusal(Exception):
    """The status surface refused a read (the block-presence doctrine)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if not code or code != code.strip():
            raise ValueError("refusal code must be non-empty and normalized")
        self.code = code
        self.message = message


# --- settings -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EveningShareSettings:
    """Every behavioural key of the ``evening_load_sharing:`` block (§11)."""

    timezone: str
    mode: AdviseMode = "advise"
    window_local: time = time(16, 0)
    window_end_local: time = time(22, 30)
    min_share_w: int = 500
    cap_w: int = 2500
    participation_floor_pct: float = 20.0
    soc_exponent: float = 2.0
    spill_tolerance_w: int = 150
    import_tolerance_w: int = 100
    assumed_discharge_over_frac: float = 1.16
    # v1.2/A5: the largest allowed move of the FILED total between consecutive
    # meter-fresh ticks (the anti-duty-cycle bound); v1.2/A8's one setting.
    slew_cap_w: int = 500
    # v1.2/A1: how long a site-meter reading may govern after its capture.
    site_meter_stale_after_s: float = 5.0
    frozen_word_ticks: int = 8
    frozen_flow_delta_w: int = 200
    delivery_move_floor_w: int = 400
    exchange_move_floor_w: int = 150
    non_delivery_ticks: int = 3
    intent_ttl_s: float = 10.0
    assumed_capacity_wh: Mapping[str, int] = field(default_factory=dict)
    act_netting_evidence: str | None = None
    unit_ids: tuple[str, ...] = ()
    # E6's boot half (composition-owned, never config): the loud note a
    # degraded-to-advise act block carries on every projection.
    degraded_note: str | None = None


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


def _second_of_day(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


# --- the pure arithmetic (exported for the named tests) -------------------------


def unit_basis_word(
    observation: Any, *, max_age_s: float, now_mono: float
) -> tuple[BasisEvidence, BasisEvidence]:
    """One unit's (grid, battery) word pair under the kernel's own bound.

    The excess rollup's classification, reused word for word (§3.3): the
    freshness bound IS ``policy.max_telemetry_age_s`` (E10 — one freshness
    truth, no third key to drift), ``missing`` for an absent/non-finite word,
    ``bad`` for a present quality flag that is not GOOD, ``stale`` beyond the
    kernel's commissioned freshness bound, ``good`` otherwise.
    """
    words: list[BasisEvidence] = []
    for name in ("grid_power_w", "battery_watts"):
        # ``_finite`` returns the VALUE (0.0 is a REAL reading, never an
        # absence) — truthiness is never the test here, the None-ness is.
        value = None if observation is None else _finite(getattr(observation, name, None))
        if value is None:
            words.append("missing")
            continue
        quality = getattr(observation, "quality", None)
        flag = quality.get(name) if isinstance(quality, Mapping) else None
        if flag is not None and _enum_text(flag) != "good":
            words.append("bad")
            continue
        captured = _finite(getattr(observation, "captured_at_mono", None))
        if captured is None:
            words.append("missing")
        elif float(now_mono) - captured > float(max_age_s):
            words.append("stale")
        else:
            words.append("good")
    return words[0], words[1]


@dataclass(frozen=True, slots=True)
class BasisFrame:
    """The §3.3 identity's per-tick frame — the loop's whole control basis."""

    net_exchange_w: float  # SIGNED, import-positive (the words are export-positive)
    served_w: float  # Σ max(0, battery_watts) over EVERY pod
    elsewhere_w: float  # Σ max(0, battery_watts) over NON-participants (E2)
    work_w: float  # served_w + net_exchange_w — the PV-netted draw
    desired_output_w: float  # max(0, work_w - elsewhere_w)


def basis_frame(
    *,
    grid_by_unit: Mapping[str, float],
    battery_by_unit: Mapping[str, float],
    participants: Sequence[str],
) -> BasisFrame:
    """The §3.3 identity, E1's signed form and E2's subtraction.

    ``net_exchange_w`` is the NEGATED summed grid word — the words are
    export-positive, the identity wants import-positive, and the SIGN IS KEPT
    BOTH WAYS: a measured import shortfall ADDS to the next command, a
    measured export overshoot SUBTRACTS (zero netted exchange is the loop's
    sole equilibrium; no clipping anywhere).
    """
    member = frozenset(participants)
    net_exchange = -sum(float(grid_by_unit.get(unit, 0.0)) for unit in grid_by_unit)
    served = sum(max(0.0, float(value)) for value in battery_by_unit.values())
    elsewhere = sum(
        max(0.0, float(value))
        for unit, value in battery_by_unit.items()
        if unit not in member
    )
    work = served + net_exchange
    desired = max(0.0, work - elsewhere)
    return BasisFrame(
        net_exchange_w=net_exchange,
        served_w=served,
        elsewhere_w=elsewhere,
        work_w=work,
        desired_output_w=desired,
    )


def commanded_total_w(
    *, desired_output_w: float, assumed_discharge_over_frac: float, fleet_limit_w: int
) -> int:
    """§3.3/§5.2: the derated, fleet-bounded command total.

    ``commanded = round(desired / derate)`` — the +15-16% filed delivery
    overshoot divided out at the range's SAFE edge — additionally bounded by
    the kernel's per-direction fleet limit so the kernel's proportional clamp
    is never the thing that saves the plan.
    """
    if desired_output_w <= 0.0:
        return 0
    total = round(desired_output_w / max(float(assumed_discharge_over_frac), 1e-6))
    return max(0, min(int(total), int(fleet_limit_w)))


def share_weight(
    *, soc_pct: float, capacity_wh: float, soc_exponent: float
) -> float:
    """§5.1: ``(SoC/100)^exponent x capacity`` — the percentage-space weight."""
    fraction = min(max(float(soc_pct) / 100.0, 0.0), 1.0)
    return float(fraction**float(soc_exponent) * float(capacity_wh))


@dataclass(frozen=True, slots=True)
class SplitResult:
    """§5.3's split: the ordered shares plus its own honesty flags."""

    shares: Mapping[str, int]
    participants: tuple[str, ...]
    capability_limited: bool
    floor_clamped: tuple[str, ...]
    cap_clamped: tuple[str, ...]


def participant_split(
    *,
    total_w: int,
    weights: Mapping[str, float],
    min_share_w: int,
    cap_w: int,
    latched: frozenset[str] = frozenset(),
) -> SplitResult | None:
    """§5.3's participant-set rule + the E8 clamp/renormalize order.

    (1) SELECT: the smallest set of the HIGHEST-WEIGHT pods that can CARRY
    ``total_w`` with every share inside ``[min_share_w, cap_w]`` — carrying
    means both bounds (a pod pinned at cap is ``capability_limited``, never a
    carrier; E7 makes the smallest-set preference the efficiency mitigation:
    larger shares on fewer pods).  The §5.3 join/leave latch keeps members in
    through dips (membership only, never arithmetic); the latch is DROPPED
    whole when the clamped floors alone would exceed the total (E8: arithmetic
    outranks the latch).
    (2) SPLIT by weight over the set.
    (3) CLAMP each share to ``[min_share_w, cap_w]`` — a latched member whose
    raw share fell below the floor is clamped UP.
    (4) RENORMALIZE the unclamped members so Σ = total EXACTLY (re-split by
    weight over the unclamped, re-capped, iterated once — the kernel's
    ``_apply_fleet_limit`` shape).  With every member at cap, Σ sits below
    the total honestly (``capability_limited``) — never PAST it.

    Returns ``None`` when there is no servable set (the total below one pod's
    floor): §5.3's honest idle.
    """
    floor_w = int(min_share_w)
    ceiling_w = int(cap_w)
    if total_w < floor_w:
        return None
    ordered = sorted(
        ((unit, float(weight)) for unit, weight in weights.items()),
        key=lambda item: (-item[1], item[0]),
    )
    if not ordered:
        return None

    def _shares(units: Sequence[tuple[str, float]]) -> dict[str, float]:
        total_weight = sum(weight for _unit, weight in units) or 1.0
        return {
            unit: float(total_w) * weight / total_weight for unit, weight in units
        }

    def _carries(units: Sequence[tuple[str, float]]) -> bool:
        return all(floor_w <= value <= ceiling_w for value in _shares(units).values())

    # (1) selection — the latch first, then extension by weight while the set
    # cannot carry the total.
    latched_ordered = [item for item in ordered if item[0] in latched]
    chosen: list[tuple[str, float]] = latched_ordered or [ordered[0]]
    remaining = [item for item in ordered if item not in chosen]
    if latched_ordered:
        # E8's latch-release: a latched member whose raw share fell below the
        # 0.8 x floor join edge LEAVES (the §5.3 hysteresis).
        edge = JOIN_HYSTERESIS_FRACTION * float(floor_w)
        raw = _shares(chosen)
        chosen = [item for item in chosen if raw[item[0]] >= edge] or [ordered[0]]
        remaining = [item for item in ordered if item not in chosen]
    while not _carries(chosen) and remaining:
        chosen.append(remaining.pop(0))

    # (3)/(4) clamp + renormalize, the E8 pinned order.
    shares = _shares(chosen)
    clamped: dict[str, int] = {}
    floor_hits: list[str] = []
    cap_hits: list[str] = []
    for unit, value in shares.items():
        if value < floor_w:
            clamped[unit] = floor_w
            floor_hits.append(unit)
        elif value > ceiling_w:
            clamped[unit] = ceiling_w
            cap_hits.append(unit)
        else:
            clamped[unit] = round(value)
    if sum(clamped.values()) > int(total_w) and floor_hits:
        # E8's override: clamped floors alone exceed the commanded total —
        # re-select WITHOUT the latch (arithmetic outranks membership).
        if latched_ordered:
            return participant_split(
                total_w=total_w,
                weights=weights,
                min_share_w=min_share_w,
                cap_w=cap_w,
                latched=frozenset(),
            )
        return None
    for _pass in range(2):
        free = [unit for unit in clamped if unit not in floor_hits and unit not in cap_hits]
        residual = int(total_w) - sum(clamped[unit] for unit in clamped if unit not in free)
        if not free or residual < 0:
            break
        free_weight = sum(dict(chosen)[unit] for unit in free) or 1.0
        landed = False
        for unit in free:
            target = residual * dict(chosen)[unit] / free_weight
            if target > ceiling_w:
                clamped[unit] = ceiling_w
                cap_hits.append(unit)
                landed = True
            elif target < floor_w:
                clamped[unit] = floor_w
                floor_hits.append(unit)
                landed = True
            else:
                clamped[unit] = math.floor(target)
        if landed:
            continue
        # Largest-remainder rounding so Σ over the free members equals the
        # residual EXACTLY — the E8 invariant holds to the watt, never past
        # the total (independent per-unit rounding can drift ±1 W).
        shortfall = residual - sum(clamped[unit] for unit in free)

        def _remainder(
            unit: str,
            *,
            residual_w: float,
            weight_sum: float,
            weight_of: Mapping[str, float],
        ) -> float:
            exact = residual_w * weight_of[unit] / weight_sum
            return exact - math.floor(exact)

        chosen_weight_of = dict(chosen)
        by_remainder = sorted(
            free,
            key=lambda unit: (
                -_remainder(
                    unit,
                    residual_w=residual,
                    weight_sum=free_weight,
                    weight_of=chosen_weight_of,
                ),
                unit,
            ),
        )
        for unit in by_remainder:
            if shortfall <= 0:
                break
            if clamped[unit] < ceiling_w:
                clamped[unit] += 1
                shortfall -= 1
        break
    capability_limited = sum(clamped.values()) < int(total_w)
    return SplitResult(
        shares={unit: clamped[unit] for unit in sorted(clamped)},
        participants=tuple(unit for unit, _weight in sorted(chosen, key=lambda i: i[0])),
        capability_limited=capability_limited,
        floor_clamped=tuple(sorted(floor_hits)),
        cap_clamped=tuple(sorted(cap_hits)),
    )


@dataclass(frozen=True, slots=True)
class _WordSpan:
    """The plausibility guard's ring-buffer span (E3's P1/P2 window)."""

    grid: tuple[float, ...]
    battery: tuple[float, ...]


def plausibility_verdict(
    *,
    spans: Mapping[str, _WordSpan],
    net_exchange_history: Sequence[float],
    settings: EveningShareSettings,
) -> str | None:
    """E3's two predicates, both fail-closed to ``grid_evidence_implausible``.

    P1 the frozen word: a unit's grid word numerically unchanged across the
    span while its battery word moved beyond ``frozen_flow_delta_w`` (and the
    mirror) — a fresh-stamped word that never twitched while its phase flowed
    is a stuck word, and the identity must never convert it into a command.
    P2 the reconciliation, ONE-SIDED by design: a fleet battery move at or
    beyond ``delivery_move_floor_w`` answered by LESS than
    ``exchange_move_floor_w`` of signed exchange movement — only a meter that
    fails to answer the fleet's own delivery is condemned by it (a kettle
    moves the exchange MORE, which is the loop's own signal, never fault).
    """
    ticks = int(settings.frozen_word_ticks)
    delta = float(settings.frozen_flow_delta_w)
    for span in spans.values():
        if len(span.grid) < max(2, ticks) or len(span.battery) < max(2, ticks):
            continue
        grid_window = span.grid[-ticks:]
        battery_window = span.battery[-ticks:]
        grid_range = max(grid_window) - min(grid_window)
        battery_range = max(battery_window) - min(battery_window)
        if grid_range <= 0.0 and battery_range > delta:
            return REASON_IMPLAUSIBLE
        if battery_range <= 0.0 and grid_range > delta:
            return REASON_IMPLAUSIBLE
    if len(net_exchange_history) >= max(2, ticks):
        exchange_window = net_exchange_history[-ticks:]
        exchange_move = abs(exchange_window[-1] - exchange_window[0])
        battery_move = 0.0
        for span in spans.values():
            if len(span.battery) >= max(2, ticks):
                battery_window = span.battery[-ticks:]
                battery_move += abs(battery_window[-1] - battery_window[0])
        if (
            battery_move >= float(settings.delivery_move_floor_w)
            and exchange_move < float(settings.exchange_move_floor_w)
        ):
            return REASON_IMPLAUSIBLE
    return None


def evening_money_cents(
    *, import_wh: float, spill_wh: float, tariff: Mapping[str, float] | None
) -> dict[str, float] | None:
    """§8.1's measured money line: import paid against spill earned, both
    carried (never waved at); ``None`` without a tariff truth (kWh-only)."""
    if tariff is None:
        return None
    return {
        "import_paid_cents": round(
            float(import_wh) * float(tariff.get("import_cents_per_kwh", 0.0)) / 1000.0, 1
        ),
        "spill_earned_cents": round(
            float(spill_wh) * float(tariff.get("export_cents_per_kwh", 0.0)) / 1000.0, 1
        ),
    }


# --- the per-unit view rows ------------------------------------------------------


@dataclass(slots=True)
class _UnitTickRow:
    """One unit's per-tick row (the projection's own shape)."""

    unit_id: str
    soc_pct: float | None = None
    weight: float | None = None
    share_w: int = 0
    phase: UnitPhase = "sitting_out"
    reason: str = REASON_ON_PLAN
    note: str | None = None


@dataclass(frozen=True, slots=True)
class _ClaimView:
    manual: frozenset[str]
    agent: frozenset[str]
    schedule: frozenset[str]
    optimizer: frozenset[str]
    emergency: frozenset[str]


# --- the adviser -----------------------------------------------------------------


class EveningShareAdviser:
    """The evening load-sharing program: measure, split, renew — one tick.

    Ticks once per fleet cycle, fully suppressed by the supervision pass: a
    failure inside it is a missed tick, never a delay to control.  Holds no
    transport of its own — the split's intent submission is the one dispatch
    act, through the facade's internal evening twin.  In the ``advise``
    posture the whole program runs and renders and submits NOTHING, ever, on
    any tick, under any input (the §11 named test).

    The TOTAL arithmetic is STATELESS BY CONSTRUCTION (§6.2): every watt is
    recomputed from the measured words each tick — a restart mid-evening
    loses nothing of the control plan.  The three pieces of window-scoped
    state (the join/leave latch, the claim-settle debounce, the E4 counter)
    touch MEMBERSHIP only and reset at window boundaries.
    """

    def __init__(
        self,
        *,
        settings: EveningShareSettings,
        policy: Any,
        clock: Clock,
        observations: _ObservationsPort,
        intents: _IntentsPort,
        submit: _SubmitPort,
        history: _HistoryPort,
        audit: _AuditPort,
        bus: _BusPort,
        health_states: Callable[[], Awaitable[Mapping[str, Any]]],
        parked_units: Callable[[], frozenset[str]] | None = None,
        latched_stop_units: Callable[[], frozenset[str]] | None = None,
        site_meter: SiteMeterPort | None = None,
        tariff: Mapping[str, float] | None = None,
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
        self._parked_units = parked_units
        self._latched_stop_units = latched_stop_units
        # v1.2/A1: the optional authoritative basis.  ``None`` (the absent
        # block) composes byte-identical v1.1 behavior; when composed, a FRESH
        # reading replaces the pod-word need numbers for the tick and a lost
        # reading degrades loudly once (A8's single additive code).
        self._site_meter = site_meter
        self._site_meter_ok: bool | None = None  # tri-state across ticks
        self._divergence_ticks = 0
        self._tariff = dict(tariff) if tariff is not None else None
        self._process_instance_id = process_instance_id
        self._zone = ZoneInfo(settings.timezone)
        # Window-scoped adviser state (membership only, §6.2's three pieces).
        self._night: date | None = None
        self._engaged = False
        self._standing_total_w: int | None = None
        self._held_intent_id: str | None = None
        self._latched_members: frozenset[str] = frozenset()
        self._foreign_claim_since: dict[str, float] = {}  # unit -> mono of last claim
        self._non_delivery_counts: dict[str, int] = {}
        self._not_delivering_dropped: set[str] = set()
        self._last_shares: dict[str, int] = {}
        # The evidence ring buffers (E3's P1/P2 window) — window-scoped.
        self._grid_history: dict[str, deque[float]] = {}
        self._battery_history: dict[str, deque[float]] = {}
        self._exchange_history: deque[float] = deque(maxlen=64)
        # Bookkeeping (§8.1): the claim timeline attributes the historian's
        # watts; a restart loses the pre-restart timeline (nothing material —
        # an interrupted evening is a shorter evening).
        self._claim_windows: list[tuple[datetime, datetime, tuple[str, ...]]] = []
        self._window_open_soc: dict[str, float] | None = None
        self._open_work_w: float | None = None
        self._close_record: dict[str, Any] | None = None
        self._closed_nights: set[date] = set()
        # §8.2's publication discipline (semantic tuple + 30 s heartbeat).
        self._published_tuple: tuple[Any, ...] | None = None
        self._last_publish_mono: float | None = None
        # The per-tick frame the projection renders.
        self._last_frame: dict[str, Any] | None = None
        self._alert_since: dict[str, datetime] = {}
        # v1.2: slew memory for the meter-fresh filing path (A5).
        self._last_filed_total: int | None = None

    # --- the fleet-loop tick --------------------------------------------------

    async def tick(self) -> None:
        """Advance the program by one bounded step; never raises."""
        try:
            await self._tick()
        except Exception as error:
            print(f"SUPERVISED EVENING-SHARE TICK FAILURE: {error!r}", flush=True)

    async def _tick(self) -> None:
        wall = self._clock.wall_now()
        local = wall.astimezone(self._zone)
        today = local.date()
        second = _second_of_day(local.timetz().replace(tzinfo=None))
        in_window = (
            _second_of_day(self._settings.window_local)
            <= second
            < _second_of_day(self._settings.window_end_local)
        )
        await self._roll_night(today, wall)
        if not in_window:
            await self._handle_outside_window(wall, today)
            return
        now_mono = float(self._clock.monotonic())
        latest = await self._observations.all_latest()
        active = await self._intents.active(now_mono)
        # Step 3 first in effect (the fleet-wide stand-down): a live
        # EMERGENCY_STOP claim withdraws everything, verbatim the night walk.
        if self._any_emergency_stop(active):
            await self._withdraw(wall, (REASON_YIELDING,))
            await self._frame(wall, "withdrawn", latest, {}, (REASON_YIELDING,))
            return
        # Step 1: classify every unit's grid and battery words (§3.3's
        # evidence rules, the kernel's own freshness bound).  Any non-good
        # word makes the basis UNJUDGEABLE — withdraw, never zero-filled.
        worst = "good"
        grid_by_unit: dict[str, float] = {}
        battery_by_unit: dict[str, float] = {}
        for unit_id in self._settings.unit_ids:
            observation = latest.get(unit_id)
            grid_word, battery_word = unit_basis_word(
                observation,
                max_age_s=float(self._policy.max_telemetry_age_s),
                now_mono=now_mono,
            )
            for word in (grid_word, battery_word):
                if _BASIS_EVIDENCE_RANK[word] > _BASIS_EVIDENCE_RANK[worst]:
                    worst = word
            if grid_word == "good" and observation is not None:
                grid_by_unit[unit_id] = float(observation.grid_power_w)
            if battery_word == "good" and observation is not None:
                battery_by_unit[unit_id] = float(observation.battery_watts)
        self._record_word_history(grid_by_unit, battery_by_unit)
        if worst != "good":
            await self._withdraw(wall, (f"grid_evidence_{worst}",))
            await self._frame(
                wall, "withdrawn", latest, {}, (f"grid_evidence_{worst}",)
            )
            await self._evidence_alert(wall, worst)
            return
        net_exchange = -sum(grid_by_unit.values())

        # v1.2/A1+A6: the authoritative site-meter consultation.  A FRESH
        # reading overrides the pod-word basis entirely (served/net replaced),
        # and its disagreement with the summed pod words is counted — enough
        # consecutive divergence holds the program silent on the existing
        # implausibility code rather than discharging into an untrusted tale.
        # Anything else (absent port, stale, unavailable) keeps the legacy
        # basis, degrading loudly exactly once per loss episode.
        meter = None if self._site_meter is None else await self._site_meter.latest()
        use_site_basis = False
        site_codes: tuple[str, ...] = ()
        if self._site_meter is not None:
            fresh_reading = (
                meter is not None
                and meter.quality == "good"
                and meter.net_exchange_w is not None
                and meter.load_w is not None
                and meter.fresh(now_mono, float(self._settings.site_meter_stale_after_s))
            )
            if fresh_reading and meter is not None:
                assert meter.net_exchange_w is not None  # narrowed above
                meter_net = float(meter.net_exchange_w)
                # A6 (site-truth form): a standing DISCHARGE while the
                # authoritative meter reads a meaningful EXPORT is the one
                # physical impossibility worth halting on — our filed watts
                # are landing outside the house. Level differences between
                # pod sums and the site eye are NOT suspicious here (the
                # household's loads live behind unseen circuits); direction
                # contradiction is.
                exporting_against_us = (
                    meter_net < -float(int(self._settings.spill_tolerance_w))
                    and bool(self._last_shares)
                )
                if exporting_against_us:
                    await self._withdraw(wall, (REASON_IMPLAUSIBLE,))
                    await self._frame(
                        wall, "withdrawn", latest, {}, (REASON_IMPLAUSIBLE,)
                    )
                    await self._evidence_alert(wall, "implausible")
                    return
                use_site_basis = True
            else:
                self._divergence_ticks = 0
            # The one-shot degrade notice rides whichever codes the tick files.
            was_ok = self._site_meter_ok
            if fresh_reading and was_ok is False:
                pass  # recovered: silence again
            elif not fresh_reading:
                site_codes = (REASON_SITE_METER_DEGRADED,)
            self._site_meter_ok = fresh_reading
        self._exchange_history.append(net_exchange)

        def _with_site(codes: tuple[str, ...]) -> tuple[str, ...]:
            return (*site_codes, *codes)
        # E3's plausibility guard (fresh-stamped frozen words).
        implausible = plausibility_verdict(
            spans={
                unit_id: _WordSpan(
                    grid=tuple(self._grid_history.get(unit_id, ())),
                    battery=tuple(self._battery_history.get(unit_id, ())),
                )
                for unit_id in self._settings.unit_ids
            },
            net_exchange_history=tuple(self._exchange_history),
            settings=self._settings,
        )
        if implausible is not None:
            await self._withdraw(wall, (implausible,))
            await self._frame(wall, "withdrawn", latest, {}, (implausible,))
            await self._evidence_alert(wall, "implausible")
            return
        # Step 4: the candidate set (the §7.1 skip-if walk + the floor).
        # ONE claim read per tick (the E5 debounce's clock stamps here too).
        claims = self._claiming_units(active, now_mono)
        soc_by_unit: dict[str, float] = {}
        skip_reasons: dict[str, str] = {}
        for unit_id in self._settings.unit_ids:
            observation = latest.get(unit_id)
            soc = _finite(getattr(observation, "authoritative_soc_pct", None))
            if soc is not None:
                soc_by_unit[unit_id] = soc
            skip = await self._skip_reason(
                unit_id, observation, claims=claims, now_mono=now_mono
            )
            if skip is None and soc is not None and soc <= self._settings.participation_floor_pct:
                skip = REASON_SOC_FLOOR
            if skip is not None:
                skip_reasons[unit_id] = skip
        # Step 5: the E5 claim-settle debounce (membership only).
        settling: dict[str, str] = {}
        debounce_s = CLAIM_SETTLE_TTL_MULTIPLE * float(self._settings.intent_ttl_s)
        for unit_id in self._settings.unit_ids:
            if unit_id in skip_reasons:
                continue
            last_claim = self._foreign_claim_since.get(unit_id)
            if last_claim is not None and (now_mono - last_claim) < debounce_s:
                settling[unit_id] = REASON_CLAIM_SETTLING
        # Step 6: the E4 non-delivery drop over LAST tick's participants.
        # The drop renders ``not_delivering`` until the unit actually re-joins
        # the participant set (re-entry is the §5.3 join rule, nothing
        # gentler — the reason must not evaporate the tick after the drop).
        delivering: dict[str, bool] = {}
        for unit_id in self._settings.unit_ids:
            if unit_id in skip_reasons or unit_id in settling:
                continue
            count = self._non_delivery_counts.get(unit_id, 0)
            commanded = self._last_shares.get(unit_id, 0)
            measured = max(0.0, battery_by_unit.get(unit_id, 0.0))
            if commanded > 0 and measured < DELIVERY_PASS_FRACTION * commanded:
                count += 1
            else:
                count = 0
            self._non_delivery_counts[unit_id] = count
            delivering[unit_id] = count < int(self._settings.non_delivery_ticks)
            if not delivering[unit_id]:
                skip_reasons[unit_id] = REASON_NOT_DELIVERING
                self._not_delivering_dropped.add(unit_id)
        candidates = [
            unit_id
            for unit_id in self._settings.unit_ids
            if unit_id not in skip_reasons and unit_id not in settling
        ]
        # Step 7: the identity over the CANDIDATE set (the eventual
        # participants are a subset; elsewhere_w counts every non-participant
        # flow — E2's one answer to every flavor of "someone else is
        # flowing").
        provisional = basis_frame(
            grid_by_unit=grid_by_unit,
            battery_by_unit=battery_by_unit,
            participants=candidates,
        )
        # Step 8: engagement on WORK, not import (§6.1) — with the §5.3
        # hysteresis on the disengage side.
        desired = provisional.desired_output_w
        if use_site_basis and meter is not None:
            # A2's basis swap on TRUTH: the command cancels METERED EXCHANGE
            # and nothing else — an import stands as need, an export residual
            # contributes zero (no watt is ever filed to chase an export).
            # The served-load word is intentionally NOT re-added: the identity
            # already nets delivery against demand at one instrument.
            meter_net = float(meter.net_exchange_w or 0.0)
            desired = max(0.0, meter_net)
        def _codes(base: tuple[str, ...]) -> tuple[str, ...]:
            return (*site_codes, *base)
        edge = JOIN_HYSTERESIS_FRACTION * float(self._settings.min_share_w)
        if not self._engaged and desired < float(self._settings.min_share_w):
            await self._idle_frame(
                wall, latest, skip_reasons | settling, _codes((REASON_BELOW_FLOOR,)), grid_by_unit
            )
            return
        if self._engaged and desired < edge:
            await self._withdraw(wall, (REASON_BELOW_FLOOR,))
            self._engaged = False
            await self._idle_frame(
                wall, latest, skip_reasons | settling, _codes((REASON_BELOW_FLOOR,)), grid_by_unit
            )
            return
        if not candidates:
            await self._withdraw(wall, (REASON_NO_ELIGIBLE,))
            await self._idle_frame(
                wall, latest, skip_reasons | settling, (REASON_NO_ELIGIBLE,), grid_by_unit
            )
            return
        # The final participant set for THIS tick's identity: re-select it
        # (the split needs the weights), then recompute elsewhere_w over the
        # FINAL set so the identity and the submission agree.
        weights = {
            unit_id: share_weight(
                soc_pct=soc_by_unit[unit_id],
                capacity_wh=self._capacity_wh(unit_id),
                soc_exponent=float(self._settings.soc_exponent),
            )
            for unit_id in candidates
            if unit_id in soc_by_unit
        }
        if not weights:
            await self._withdraw(wall, (REASON_NO_ELIGIBLE,))
            await self._idle_frame(
                wall, latest, skip_reasons | settling, (REASON_NO_ELIGIBLE,), grid_by_unit
            )
            return
        # Step 9: the deadband (§3.4) — inside [-spill, +import] with a
        # standing command, HOLD the total; outside, recompute BOTH ways.
        band = self._in_band(meter_net if use_site_basis else net_exchange)
        if band and self._standing_total_w is not None:
            total = int(self._standing_total_w)
            fleet_clipped = False
        elif use_site_basis:
            # v1.2/A2: the need ceiling — a fresh-meter filing is EXACTLY the
            # rounded need.  No derate division above it, ever: the derate
            # doctrine assumed delivery undershoots; measured delivery does
            # not, and the miss landed as exported battery energy.
            total = round(desired)
            unbounded = total
            fleet_clipped = unbounded > int(self._policy.fleet_discharge_limit_w)
            # A5's slew: consecutive filings move at most slew_cap_w apart,
            # killing the burst duty-cycling without latching.
            previous = self._last_filed_total
            if previous is not None:
                slew = int(self._settings.slew_cap_w)
                low, high = previous - slew, previous + slew
                total = max(low, min(total, high))
            self._last_filed_total = total
        else:
            unbounded = round(
                desired / max(float(self._settings.assumed_discharge_over_frac), 1e-6)
            )
            total = commanded_total_w(
                desired_output_w=desired,
                assumed_discharge_over_frac=self._settings.assumed_discharge_over_frac,
                fleet_limit_w=int(self._policy.fleet_discharge_limit_w),
            )
            # §6.4: an evening beyond the fleet bound is capability_limited —
            # the servable share stands, the residual import is the grid's.
            fleet_clipped = unbounded > int(self._policy.fleet_discharge_limit_w)
        # Step 10: select, split, clamp, renormalize (E8's pinned order).
        split = participant_split(
            total_w=int(total),
            weights=weights,
            min_share_w=int(self._settings.min_share_w),
            cap_w=int(self._settings.cap_w),
            latched=self._latched_members,
        )
        if split is None:
            await self._withdraw(wall, (REASON_BELOW_FLOOR,))
            await self._idle_frame(
                wall, latest, skip_reasons | settling, (REASON_BELOW_FLOOR,), grid_by_unit
            )
            return
        # The projection's identity rides the CANDIDATE frame (§8.3's own
        # example: a sitting-out candidate's flow is NOT elsewhere_w — only
        # the excluded units' flow is).
        frame = provisional
        within_band = self._in_band(frame.net_exchange_w)
        capability_limited = split.capability_limited or fleet_clipped
        reasons: tuple[str, ...] = _codes(
            (REASON_CAPABILITY,) if capability_limited else (REASON_ON_PLAN,)
        )
        if within_band:
            reasons = (*reasons, "within_tolerance")
        # Step 11: renew — remove the held intent, submit ONE els- intent.
        if not self._engaged:
            self._engaged = True
            await self._record_open(wall, today, frame, split, weights)
            if self._window_open_soc is None:
                self._window_open_soc = dict(soc_by_unit)
        set_changed = set(split.participants) != set(self._last_shares) or (
            self._last_shares and any(
                self._last_shares.get(unit, 0) != split.shares.get(unit, 0)
                for unit in set(split.participants) | set(self._last_shares)
            )
        )
        if set_changed and self._last_shares:
            await self._record_revision(wall, today, split, skip_reasons | settling)
        if self._settings.mode == "act":
            submitted = await self._renew(wall, split)
        else:
            # §11's named test: advise submits NOTHING on any tick under any
            # input — the whole loop runs, the rows land with the marker.
            submitted = None
        self._standing_total_w = int(total)
        self._latched_members = frozenset(split.participants)
        self._last_shares = dict(split.shares)
        self._record_claim_window(wall, tuple(split.participants))
        # Step 12: the projection's per-tick frame.
        await self._frame(
            wall,
            "capability_limited" if capability_limited else "sharing",
            latest,
            skip_reasons | settling,
            reasons,
            frame=frame,
            split=split,
            weights=weights,
            soc_by_unit=soc_by_unit,
            submitted=submitted,
        )

    # --- the window's edges -----------------------------------------------------

    async def _roll_night(self, today: date, wall: datetime) -> None:
        """Reset the window-scoped membership state on civil rollover (§6.2)."""
        if self._night == today:
            return
        self._night = today
        self._engaged = False
        self._standing_total_w = None
        self._latched_members = frozenset()
        self._non_delivery_counts = {}
        self._not_delivering_dropped = set()
        self._last_shares = {}
        self._grid_history = {}
        self._battery_history = {}
        self._exchange_history.clear()
        self._claim_windows = []
        self._window_open_soc = None
        self._open_work_w = None
        self._alert_since = {}
        # v1.2 state resets ride the window edge with everything else.
        self._divergence_ticks = 0
        self._site_meter_ok = None
        self._last_filed_total = None

    async def _handle_outside_window(self, wall: datetime, today: date) -> None:
        """§6.2 step 2: outside the window is NON-RENEWAL; the window's close
        row lands once per civil night at the end wall."""
        if self._held_intent_id is not None or self._engaged:
            await self._withdraw(wall, (REASON_OUTSIDE_WINDOW,))
            self._engaged = False
        if today not in self._closed_nights and self._claim_windows:
            self._closed_nights.add(today)
            record = await self._compute_close(wall, today)
            if record is not None:
                await self._append_row(
                    self._row(
                        event_type="evening_window_closed",
                        reason_codes=(REASON_OUTSIDE_WINDOW,),
                        result="closed",
                        payload=record,
                    )
                )
                await self._publish_event(record | {"tier": TIER_RESOLVED}, TIER_RESOLVED)
        # The standing exit renders: idle, outside_window, nothing held.
        if self._last_frame is None or self._last_frame.get("reason_codes") != [
            REASON_OUTSIDE_WINDOW
        ]:
            latest: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                latest = dict(await self._observations.all_latest())
            await self._frame(wall, "idle", latest, {}, (REASON_OUTSIDE_WINDOW,))

    def _in_band(self, net_exchange_w: float) -> bool:
        """§3.4's deadband: the SIGNED exchange inside
        ``[-spill_tolerance_w, +import_tolerance_w]``."""
        return -float(self._settings.spill_tolerance_w) <= net_exchange_w <= float(
            self._settings.import_tolerance_w
        )

    # --- the skip-if walk (§7.1) -------------------------------------------------

    async def _skip_reason(
        self,
        unit_id: str,
        latest: Any,
        *,
        claims: _ClaimView,
        now_mono: float,
    ) -> str | None:
        """The standing guards, one honest reason per unit — every skip a
        recorded reason on the projection, never silence, never a retry."""
        if unit_id in self._parked_view():
            return "unit_parked"
        if unit_id in self._latched_view():
            return "latched_stop"
        word = getattr(latest, "debug_mode_w", None)
        if isinstance(word, int) and not isinstance(word, bool) and word in {2, 3, 4, 5, 6}:
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
        if unit_id in claims.manual or unit_id in claims.agent:
            return "under_intent"
        if unit_id in claims.schedule:
            return "schedule_claim"
        if unit_id in claims.optimizer:
            # A live NOT-OWN OPTIMIZER claim (the excess adviser's ``excess-``
            # charge, the calibration traverse's ``cal-`` intent): FREE
            # surplus and the sibling's traverse outrank paid support, one
            # direction only — this program waits and never contests.
            return "optimizer_claim"
        if unit_id in claims.emergency:
            return REASON_YIELDING
        return None

    def _claiming_units(self, active: Sequence[Any], now_mono: float) -> _ClaimView:
        """The C3 claim view (MANUAL/AGENT/SCHEDULE + not-own OPTIMIZER), and
        the E5 debounce's own bookkeeping: every FOREIGN claim sighting stamps
        the unit's settle clock."""
        manual: set[str] = set()
        agent: set[str] = set()
        schedule: set[str] = set()
        optimizer: set[str] = set()
        emergency: set[str] = set()
        for intent in active:
            source = _enum_text(getattr(intent, "source", None))
            if source not in _CLAIMING_SOURCES:
                continue
            intent_id = str(getattr(intent, "id", "") or "")
            own = source == "optimizer" and intent_id.startswith(_OWN_INTENT_PREFIX)
            for unit_id in getattr(intent, "selected_unit_ids", ()) or ():
                unit = str(unit_id)
                if source == "emergency_stop":
                    emergency.add(unit)
                elif own:
                    continue  # this adviser's own held intent, never foreign
                elif source == "manual":
                    manual.add(unit)
                elif source == "agent":
                    agent.add(unit)
                elif source == "schedule":
                    schedule.add(unit)
                else:
                    optimizer.add(unit)
                # E5: a foreign claim sighting stamps the settle clock — the
                # unit re-enters only after 2 x ttl CONTINUOUSLY claim-free.
                self._foreign_claim_since[unit] = now_mono
        return _ClaimView(
            frozenset(manual),
            frozenset(agent),
            frozenset(schedule),
            frozenset(optimizer),
            frozenset(emergency),
        )

    def _any_emergency_stop(self, active: Sequence[Any]) -> bool:
        for intent in active:
            if _enum_text(getattr(intent, "source", None)) == "emergency_stop":
                return True
        return False

    def _capacity_wh(self, unit_id: str) -> float:
        capacities = self._settings.assumed_capacity_wh or {}
        value = capacities.get(unit_id)
        return float(value) if isinstance(value, int) else 5000.0

    def _record_word_history(
        self, grid_by_unit: Mapping[str, float], battery_by_unit: Mapping[str, float]
    ) -> None:
        """The E3 ring buffers, one entry per judgeable tick."""
        span = max(2, int(self._settings.frozen_word_ticks))
        for unit_id in self._settings.unit_ids:
            self._grid_history.setdefault(unit_id, deque(maxlen=span))
            self._battery_history.setdefault(unit_id, deque(maxlen=span))
            if unit_id in grid_by_unit:
                self._grid_history[unit_id].append(grid_by_unit[unit_id])
            if unit_id in battery_by_unit:
                self._battery_history[unit_id].append(battery_by_unit[unit_id])

    # --- renewal / withdrawal ----------------------------------------------------

    async def _renew(self, wall: datetime, split: SplitResult) -> dict[str, Any] | None:
        """Remove the held intent, then submit ONE OPTIMIZER DISCHARGE intent
        with ``watts_by_unit`` for the participant set (§6.2 step 11)."""
        await self._remove_held()
        try:
            result = await self._submit(
                unit_ids=sorted(split.participants),
                direction=Direction.DISCHARGE,
                watts=None,
                ttl_s=float(self._settings.intent_ttl_s),
                watts_by_unit=dict(split.shares),
            )
        except Exception:
            self._held_intent_id = None
            self._standing_total_w = None
            return None
        submitted = result.get("intent_id") if isinstance(result, Mapping) else None
        self._held_intent_id = submitted if isinstance(submitted, str) else None
        return result if isinstance(result, dict) else None

    async def _withdraw(self, wall: datetime, reasons: tuple[str, ...]) -> None:
        """Withdraw by NON-RENEWAL: remove the held intent, submit nothing.

        No stop triple, no idle intent, no zero-watt submission — the TTL
        lapse plus the firmware watchdog hand every pod back to its own
        CT-following autonomy (the status quo ante, §2's fail-closed
        direction)."""
        await self._remove_held()
        self._standing_total_w = None
        self._latched_members = frozenset()
        self._last_shares = {}

    async def _remove_held(self) -> None:
        held = self._held_intent_id
        self._held_intent_id = None
        if held is None:
            return
        with contextlib.suppress(Exception):
            await self._intents.remove(held)

    # --- the bookkeeping (§8.1) ---------------------------------------------------

    def _record_claim_window(self, wall: datetime, participants: tuple[str, ...]) -> None:
        """The attribution timeline: [from, to) spans naming participants."""
        if not participants:
            return
        now = wall.astimezone(UTC)
        if self._claim_windows and self._claim_windows[-1][2] == participants:
            self._claim_windows[-1] = (
                self._claim_windows[-1][0],
                now,
                participants,
            )
        else:
            if self._claim_windows:
                self._claim_windows[-1] = (
                    self._claim_windows[-1][0],
                    now,
                    self._claim_windows[-1][2],
                )
            self._claim_windows.append((now, now, participants))

    def _integral_wh(
        self,
        samples: Sequence[Any],
        pick: Callable[[datetime, dict[str, float]], float],
    ) -> float:
        """The gap-excluded integral over the historian's per-tick rows (the
        scorecard's own discipline: consecutive same-timestamp per-unit rows
        form one tick; a gap contributes nothing, never an interpolation)."""
        by_tick: dict[datetime, dict[str, float]] = {}
        for row in samples:
            unit_id = str(getattr(row, "unit_id", ""))
            sampled_at = getattr(row, "sampled_at", None)
            if not isinstance(sampled_at, datetime):
                continue
            moment = sampled_at.astimezone(UTC)
            tick = by_tick.setdefault(moment, {})
            tick[unit_id] = float(getattr(row, "battery_watts", 0.0) or 0.0)
            tick[f"grid:{unit_id}"] = float(getattr(row, "grid_power_w", 0.0) or 0.0)
            soc = _finite(getattr(row, "bms_soc_pct", None))
            if soc is not None:
                tick[f"soc:{unit_id}"] = soc
        ticks = sorted(by_tick)
        total = 0.0
        for previous, current in itertools.pairwise(ticks):
            span_h = (current - previous).total_seconds() / 3600.0
            if span_h <= 0 or span_h > 0.25:  # a gap beyond 15 min contributes nothing
                continue
            total += pick(previous, by_tick[previous]) * span_h
        return total

    async def _compute_close(self, wall: datetime, night: date) -> dict[str, Any] | None:
        """§8.1's measurement record, computed at window close from HISTORIAN
        rows — the loop is stateless, so the bookkeeping is derived, never
        accumulated (a restart mid-evening loses nothing material)."""
        start = datetime.combine(night, self._settings.window_local, tzinfo=self._zone)
        end = datetime.combine(night, self._settings.window_end_local, tzinfo=self._zone)
        try:
            to_at = min(end, wall.astimezone(UTC) + timedelta(seconds=1))
            samples = tuple(
                self._history.samples(
                    self._settings.unit_ids, start.astimezone(UTC), to_at.astimezone(UTC)
                )
            )
        except Exception:
            samples = ()
        if not samples:
            return None
        claim_spans = [
            (from_at, to_at, frozenset(units)) for from_at, to_at, units in self._claim_windows
        ]
        # Per-pod attributed Wh: the integral of battery_watts over the
        # window's ticks where a live els- intent named the unit.
        per_unit_wh: dict[str, float] = {}
        for unit_id in self._settings.unit_ids:
            per_unit_wh[unit_id] = round(
                self._attributed_wh(samples, unit_id, claim_spans), 1
            )
        import_wh = round(
            max(
                0.0,
                self._integral_wh(
                    samples,
                    lambda _moment, tick: max(
                        0.0,
                        -sum(value for key, value in tick.items() if key.startswith("grid:")),
                    ),
                ),
            ),
            2,
        )
        # §8.1's spill: the integral of EXPORT while engaged (the §3.4
        # tolerance's observable) — only the ticks an els- claim stood for.
        def _spill_pick(moment: datetime, tick: dict[str, float]) -> float:
            if not any(
                units and from_at <= moment < to_at for from_at, to_at, units in claim_spans
            ):
                return 0.0
            return max(
                0.0, sum(value for key, value in tick.items() if key.startswith("grid:"))
            )

        spill_wh = round(max(0.0, self._integral_wh(samples, _spill_pick)), 2)
        open_delta = self._soc_spread(self._window_open_soc)
        close_delta = self._soc_spread_from_samples(samples)
        baseline_wh = self._baseline_import_wh(night, samples)
        money = evening_money_cents(
            import_wh=import_wh, spill_wh=spill_wh, tariff=self._tariff
        )
        displaced_estimate_cents: float | None = None
        if baseline_wh is not None and self._tariff is not None:
            displaced_estimate_cents = round(
                max(0.0, baseline_wh - import_wh)
                * float(self._tariff.get("import_cents_per_kwh", 0.0))
                / 1000.0,
                1,
            )
        served_total = round(sum(per_unit_wh.values()), 1)
        sentence = (
            f"served {served_total / 1000.0:.2f} kWh across "
            f"{sum(1 for value in per_unit_wh.values() if value > 0)} pods; "
            f"import {import_wh / 1000.0:.2f} kWh"
        )
        if money is not None:
            sentence += f" at {money['import_paid_cents']:.0f} c"
        if open_delta is not None and close_delta is not None:
            sentence += f"; convergence Δ {open_delta:.1f}→{close_delta:.1f} pct"
        record: dict[str, Any] = {
            "night": night.isoformat(),
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.window_end_local.strftime("%H:%M"),
            },
            "served_wh": per_unit_wh,
            "import_wh": import_wh,
            "spill_wh": spill_wh,
            "displaced_import_estimate_wh": (
                None if baseline_wh is None else round(max(0.0, baseline_wh - import_wh), 2)
            ),
            "displaced_import_estimate_provenance": (
                None
                if baseline_wh is None
                else "same-slot-last-week (the load-baseline family) — an "
                "ESTIMATE, never a measured saving"
            ),
            "displaced_import_estimate_cents": displaced_estimate_cents,
            "convergence_delta_pct": {"open": open_delta, "close": close_delta},
            "spill_tolerance_w": self._settings.spill_tolerance_w,
            "close_sentence": sentence,
            "pinned_sentence": PINNED_SENTENCE,
            "stop_route": STOP_ROUTE_SENTENCE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        if money is not None:
            record["money"] = money
        self._close_record = record
        return record

    def _attributed_wh(
        self,
        samples: Sequence[Any],
        unit_id: str,
        claim_spans: Sequence[tuple[datetime, datetime, frozenset[str]]],
    ) -> float:
        """The ATTRIBUTED integral: battery_watts over the ticks a live els-
        intent named the unit (the kitchen pod's autonomy service before and
        between claims is NOT this program's)."""
        ticks: list[tuple[datetime, float]] = []
        for row in samples:
            if str(getattr(row, "unit_id", "")) != unit_id:
                continue
            sampled_at = getattr(row, "sampled_at", None)
            if not isinstance(sampled_at, datetime):
                continue
            ticks.append(
                (sampled_at.astimezone(UTC), float(getattr(row, "battery_watts", 0.0) or 0.0))
            )
        ticks.sort()
        total = 0.0
        for (previous_at, previous_w), (current_at, _current_w) in itertools.pairwise(ticks):
            span_h = (current_at - previous_at).total_seconds() / 3600.0
            if span_h <= 0 or span_h > 0.25:
                continue
            if any(
                units and unit_id in units and from_at <= previous_at < to_at
                for from_at, to_at, units in claim_spans
            ):
                total += max(0.0, previous_w) * span_h
        return total

    def _soc_spread(self, soc_by_unit: Mapping[str, float] | None) -> float | None:
        if not soc_by_unit:
            return None
        values = [float(value) for value in soc_by_unit.values()]
        return round(max(values) - min(values), 2)

    def _soc_spread_from_samples(self, samples: Sequence[Any]) -> float | None:
        latest: dict[str, float] = {}
        latest_at: dict[str, datetime] = {}
        for row in samples:
            unit_id = str(getattr(row, "unit_id", ""))
            sampled_at = getattr(row, "sampled_at", None)
            soc = _finite(getattr(row, "bms_soc_pct", None))
            if not isinstance(sampled_at, datetime) or soc is None:
                continue
            if unit_id not in latest_at or sampled_at > latest_at[unit_id]:
                latest[unit_id] = soc
                latest_at[unit_id] = sampled_at
        return self._soc_spread(latest)

    def _baseline_import_wh(self, night: date, samples: Sequence[Any]) -> float | None:
        """§3.5/§8.1: the same-slot-last-week prior — last week's same-evening
        fleet load integral, gap-excluded, an ESTIMATE's basis (never a
        gate, never a command)."""
        try:
            start = datetime.combine(
                night - timedelta(days=7), self._settings.window_local, tzinfo=self._zone
            )
            end = datetime.combine(
                night - timedelta(days=7),
                self._settings.window_end_local,
                tzinfo=self._zone,
            )
            prior = tuple(
                self._history.samples(
                    self._settings.unit_ids, start.astimezone(UTC), end.astimezone(UTC)
                )
            )
        except Exception:
            return None
        if not prior:
            return None
        ticks: list[tuple[datetime, float]] = []
        by_tick: dict[datetime, list[float]] = {}
        for row in prior:
            sampled_at = getattr(row, "sampled_at", None)
            load = _finite(getattr(row, "load_power_w", None))
            if not isinstance(sampled_at, datetime) or load is None:
                continue
            by_tick.setdefault(sampled_at.astimezone(UTC), []).append(load)
        ticks = sorted(
            (moment, sum(values) / len(values)) for moment, values in by_tick.items()
        )
        total = 0.0
        seen = False
        for (previous_at, previous_w), (current_at, _current_w) in itertools.pairwise(ticks):
            span_h = (current_at - previous_at).total_seconds() / 3600.0
            if span_h <= 0 or span_h > 0.25:
                continue
            total += previous_w * span_h
            seen = True
        return round(total, 2) if seen else None

    # --- audit rows + events -------------------------------------------------------

    def _row(
        self,
        *,
        event_type: str,
        reason_codes: tuple[str, ...],
        result: str,
        payload: dict[str, Any],
        unit_id: str | None = None,
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now().astimezone(UTC)
        return AuditEvent(
            event_id=f"evening-{event_type}-{uuid.uuid4().hex}",
            occurred_at=wall,
            monotonic_offset_s=now_mono,
            process_instance_id=self._process_instance_id or EVENING_ADVISER_PRINCIPAL,
            event_type=event_type,
            unit_id=unit_id,
            principal=EVENING_ADVISER_PRINCIPAL,
            correlation_id=f"evening:{event_type}",
            policy_version=_EVENING_POLICY_VERSION,
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
                {"type": EVENING_STATE_EVENT_TYPE, "payload": dict(payload) | {"tier": tier}}
            )

    async def _record_open(
        self,
        wall: datetime,
        night: date,
        frame: BasisFrame,
        split: SplitResult,
        weights: Mapping[str, float],
    ) -> None:
        """§8.2's reconstruction row, at FIRST engagement each window."""
        payload: dict[str, Any] = {
            "night": night.isoformat(),
            "mode": self._settings.mode,
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.window_end_local.strftime("%H:%M"),
            },
            "work_w": round(frame.work_w, 1),
            "net_exchange_w": round(frame.net_exchange_w, 1),
            "elsewhere_w": round(frame.elsewhere_w, 1),
            "commanded_total_w": int(sum(split.shares.values())),
            "derate": float(self._settings.assumed_discharge_over_frac),
            "participants": {
                unit: {
                    "weight": round(weights.get(unit, 0.0), 1),
                    "share_w": split.shares.get(unit, 0),
                }
                for unit in split.participants
            },
            "submits": "never" if self._settings.mode == "advise" else "act",
            "pinned_sentence": PINNED_SENTENCE,
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._row(
                event_type="evening_window_opened",
                reason_codes=("window_open",),
                result="opened",
                payload=payload,
            )
        )

    async def _record_revision(
        self,
        wall: datetime,
        night: date,
        split: SplitResult,
        causes: Mapping[str, str],
    ) -> None:
        """§8.2's durable set-history row: each participant-set change (join,
        leave, floor drop, guard skip) with its cause and the before/after."""
        before = dict(self._last_shares)
        joined = sorted(set(split.participants) - set(before))
        left = sorted(set(before) - set(split.participants))
        if not joined and not left:
            return
        for unit_id in (*joined, *left):
            cause = causes.get(unit_id, "joined")
            payload = {
                "night": night.isoformat(),
                "unit_id": unit_id,
                "cause": cause,
                "before_share_w": before.get(unit_id, 0),
                "after_share_w": split.shares.get(unit_id, 0),
                "before_set": sorted(before),
                "after_set": sorted(split.participants),
                "as_of": wall.astimezone(UTC).isoformat(),
            }
            await self._append_row(
                self._row(
                    event_type="evening_share_revised",
                    reason_codes=(cause,),
                    result="revised",
                    payload=payload,
                    unit_id=unit_id,
                )
            )

    async def _evidence_alert(self, wall: datetime, word: str) -> None:
        """§9's alert tier: ``grid_evidence_*`` persisting ≥ 10 min inside the
        window — the program is withdrawn and the evening is unserved; the
        event names the standing autonomy fallback."""
        code = f"grid_evidence_{word}"
        first = self._alert_since.get(code)
        if first is None:
            self._alert_since[code] = wall
            return
        if wall - first < timedelta(minutes=10):
            return
        self._alert_since.pop(code, None)
        await self._publish_event(
            {
                "reason": code,
                "note": (
                    "the control basis is unjudgeable — the program is withdrawn "
                    "and every pod's own CT-following autonomy serves on (the "
                    "incumbent state)"
                ),
                "as_of": wall.astimezone(UTC).isoformat(),
            },
            TIER_ALERT,
        )

    # --- §10's commissioning evidence row ---------------------------------------

    async def record_phase_map(self, *, evenings: int = 14) -> dict[str, Any]:
        """§10's map: per pod, the mean and evening peak of ``load_power_w``
        over a trailing window of evenings, ranked — evidence about the
        mechanism, recorded and never read by any control path."""
        wall = self._clock.wall_now()
        tonight = wall.astimezone(self._zone).date()
        start = datetime.combine(
            tonight - timedelta(days=evenings),
            self._settings.window_local,
            tzinfo=self._zone,
        )
        end = datetime.combine(
            tonight, self._settings.window_end_local, tzinfo=self._zone
        )
        rows: tuple[Any, ...] = ()
        with contextlib.suppress(Exception):
            rows = tuple(
                self._history.samples(
                    self._settings.unit_ids, start.astimezone(UTC), end.astimezone(UTC)
                )
            )
        per_unit: dict[str, list[float]] = {}
        for row in rows:
            unit_id = str(getattr(row, "unit_id", ""))
            load = _finite(getattr(row, "load_power_w", None))
            if load is not None:
                per_unit.setdefault(unit_id, []).append(load)
        figures = {
            unit_id: {
                "mean_load_w": round(sum(values) / len(values), 1),
                "peak_load_w": round(max(values), 1),
                "samples": len(values),
            }
            for unit_id, values in sorted(per_unit.items())
        }
        ranked = sorted(
            figures, key=lambda unit: (-figures[unit]["peak_load_w"], unit)
        )
        payload = {
            "nights": evenings,
            "ranked_by_evening_peak": ranked,
            "per_pod": figures,
            "note": (
                "the phase map is commissioning EVIDENCE about the mechanism "
                "(which pod serves which phase); no control path reads it — "
                "the netted meter makes any pod's discharge equivalent to any "
                "other's at the bill"
            ),
            "as_of": wall.astimezone(UTC).isoformat(),
        }
        await self._append_row(
            self._row(
                event_type="evening_phase_map_recorded",
                reason_codes=("phase_map_recorded",),
                result="recorded",
                payload=payload,
            )
        )
        return payload

    # --- the projection (§8.3) -----------------------------------------------------

    async def _idle_frame(
        self,
        wall: datetime,
        latest: Mapping[str, Any],
        reasons: Mapping[str, str],
        codes: tuple[str, ...],
        grid_by_unit: Mapping[str, float],
    ) -> None:
        net_exchange = -sum(float(value) for value in grid_by_unit.values())
        await self._frame(
            wall, "idle", latest, reasons, codes, frame=BasisFrame(
                net_exchange_w=net_exchange,
                served_w=0.0,
                elsewhere_w=0.0,
                work_w=0.0,
                desired_output_w=0.0,
            ),
        )

    async def _frame(
        self,
        wall: datetime,
        phase: Phase,
        latest: Mapping[str, Any],
        reasons: Mapping[str, str],
        codes: tuple[str, ...],
        *,
        frame: BasisFrame | None = None,
        split: SplitResult | None = None,
        weights: Mapping[str, float] | None = None,
        soc_by_unit: Mapping[str, float] | None = None,
        submitted: Mapping[str, Any] | None = None,
    ) -> None:
        units: list[dict[str, Any]] = []
        for unit_id in self._settings.unit_ids:
            observation = latest.get(unit_id)
            soc = (
                _finite(getattr(observation, "authoritative_soc_pct", None))
                if observation is not None
                else None
            )
            sharing = split is not None and unit_id in split.shares
            if sharing:
                reason = REASON_ON_PLAN
                self._not_delivering_dropped.discard(unit_id)
            elif unit_id in reasons:
                reason = reasons[unit_id]
            elif unit_id in self._not_delivering_dropped:
                reason = REASON_NOT_DELIVERING
            elif soc is not None and soc <= self._settings.participation_floor_pct:
                reason = REASON_SOC_FLOOR
            else:
                # An eligible candidate the set rule kept out: its raw share
                # would breach min_share_w (§8.3's own example vocabulary).
                reason = "share_below_floor"
            row: dict[str, Any] = {
                "unit_id": unit_id,
                "soc_pct": None if soc is None else round(soc, 1),
                "weight": (
                    round(float(weights.get(unit_id, 0.0)), 0)
                    if weights is not None and unit_id in weights
                    else None
                ),
                "share_w": int(split.shares.get(unit_id, 0)) if split is not None else 0,
                "phase": "sharing" if sharing else "sitting_out",
                "reason": reason,
            }
            if reason == "share_below_floor":
                row["note"] = (
                    "the smallest-set rule keeps this pod out at this load — a "
                    "share below min_share_w buys the same watts inside the "
                    "partial-load efficiency collapse"
                )
            if sharing is False and unit_id in self._non_delivery_counts:
                row["delivery_below_pass"] = self._non_delivery_counts.get(unit_id, 0)
            units.append(row)
        payload: dict[str, Any] = {
            "mode": self._settings.mode,
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.window_end_local.strftime("%H:%M"),
            },
            "phase": phase,
            "as_of": wall.astimezone(UTC).isoformat(),
            "engaged": self._engaged,
            "work_w": None if frame is None else round(frame.work_w, 0),
            "net_exchange_w": None if frame is None else round(frame.net_exchange_w, 0),
            "elsewhere_w": None if frame is None else round(frame.elsewhere_w, 0),
            "within_tolerance": (
                False if frame is None else self._in_band(frame.net_exchange_w)
            ),
            "commanded_total_w": (
                int(sum(split.shares.values())) if split is not None else self._standing_total_w
            ),
            "derate": float(self._settings.assumed_discharge_over_frac),
            "reason_codes": list(codes),
            "units": units,
            "held_intent_id": self._held_intent_id,
            "pinned_sentence": PINNED_SENTENCE,
            "stop_route": STOP_ROUTE_SENTENCE,
        }
        if self._settings.mode == "advise":
            payload["submits"] = "never"
        if self._settings.degraded_note is not None:
            payload["degraded_note"] = self._settings.degraded_note
        if split is not None and REASON_CAPABILITY in codes and frame is not None:
            # §6.4's honest residual: the measured work the fleet could not
            # serve — the grid covers the remainder, never a block.
            residual = round(frame.desired_output_w) - int(sum(split.shares.values()))
            payload["residual_import_w"] = max(0, residual)
        if submitted is not None and isinstance(submitted, Mapping):
            payload["last_submission"] = {
                "intent_id": submitted.get("intent_id"),
                "expires_in_s": submitted.get("expires_in_s"),
            }
        if self._close_record is not None:
            payload["last_close"] = {
                "night": self._close_record.get("night"),
                "served_wh": self._close_record.get("served_wh"),
                "import_wh": self._close_record.get("import_wh"),
                "spill_wh": self._close_record.get("spill_wh"),
                "convergence_delta_pct": self._close_record.get("convergence_delta_pct"),
                "money": self._close_record.get("money"),
            }
        self._last_frame = payload
        await self._publish_state(payload)

    async def _publish_state(self, payload: Mapping[str, Any]) -> None:
        """§8.2's publication discipline: semantic-tuple-triggered with a 30 s
        heartbeat while ENGAGED; nothing while idle-by-window (an unengaged
        evening never publishes — the projection carries the frame)."""
        if self._bus is None or not self._engaged:
            return
        semantic = self._semantic_tuple(payload)
        now_mono = float(self._clock.monotonic())
        heartbeat = False
        if semantic != self._published_tuple:
            heartbeat = False
        elif self._last_publish_mono is None or (
            now_mono - self._last_publish_mono >= STATE_EVENT_HEARTBEAT_S
        ):
            heartbeat = True
        else:
            return
        await self._publish_event(
            dict(payload) | {"heartbeat": heartbeat}, TIER_NOTICE
        )
        self._published_tuple = semantic
        self._last_publish_mono = now_mono

    @staticmethod
    def _semantic_tuple(payload: Mapping[str, Any]) -> tuple[Any, ...]:
        """The §8.2 throttle tuple: the semantic state that triggers an event
        (the watt figures ride every publication but never trigger one)."""
        units = payload.get("units")
        return (
            payload.get("mode"),
            payload.get("phase"),
            payload.get("engaged"),
            payload.get("within_tolerance"),
            tuple(sorted(payload.get("reason_codes", ()))),
            tuple(
                (unit.get("unit_id"), unit.get("phase"), unit.get("reason"))
                for unit in units
            )
            if isinstance(units, list)
            else (),
        )

    def state_payload(self) -> dict[str, Any]:
        """§8.3's ``evening_load_share_state`` projection (the snapshot's own
        key).  Present exactly when the block composes; ``mode: advise``
        renders the whole projection with ``submits: never`` named beside it
        — a displayed plan that does not act MUST say so beside itself."""
        if self._last_frame is not None:
            return dict(self._last_frame)
        wall = self._clock.wall_now()
        payload: dict[str, Any] = {
            "mode": self._settings.mode,
            "window": {
                "opens_local": self._settings.window_local.strftime("%H:%M"),
                "ends_local": self._settings.window_end_local.strftime("%H:%M"),
            },
            "phase": "idle",
            "as_of": wall.astimezone(UTC).isoformat(),
            "engaged": False,
            "work_w": None,
            "net_exchange_w": None,
            "elsewhere_w": None,
            "within_tolerance": False,
            "commanded_total_w": None,
            "derate": float(self._settings.assumed_discharge_over_frac),
            "reason_codes": [REASON_OUTSIDE_WINDOW],
            "units": [
                {"unit_id": unit_id, "soc_pct": None, "weight": None, "share_w": 0,
                 "phase": "sitting_out", "reason": REASON_OUTSIDE_WINDOW}
                for unit_id in self._settings.unit_ids
            ],
            "held_intent_id": None,
            "pinned_sentence": PINNED_SENTENCE,
            "stop_route": STOP_ROUTE_SENTENCE,
        }
        if self._settings.mode == "advise":
            payload["submits"] = "never"
        if self._settings.degraded_note is not None:
            payload["degraded_note"] = self._settings.degraded_note
        if self._close_record is not None:
            payload["last_close"] = {
                "night": self._close_record.get("night"),
                "served_wh": self._close_record.get("served_wh"),
                "import_wh": self._close_record.get("import_wh"),
                "spill_wh": self._close_record.get("spill_wh"),
                "convergence_delta_pct": self._close_record.get("convergence_delta_pct"),
                "money": self._close_record.get("money"),
            }
        return payload

    def morning_payload(self) -> dict[str, Any] | None:
        """§8.1's morning-facts entry: the day-following line on the shipped
        morning-facts surface (the History console's morning states)."""
        if self._close_record is None:
            return None
        record = self._close_record
        return {
            "night": record.get("night"),
            "served_wh": record.get("served_wh"),
            "import_wh": record.get("import_wh"),
            "spill_wh": record.get("spill_wh"),
            "convergence_delta_pct": record.get("convergence_delta_pct"),
            "money": record.get("money"),
            "close_sentence": record.get("close_sentence"),
            "displaced_import_estimate_wh": record.get("displaced_import_estimate_wh"),
            "displaced_import_estimate_provenance": record.get(
                "displaced_import_estimate_provenance"
            ),
            "tier": TIER_RESOLVED,
            "as_of": record.get("as_of"),
        }

    # --- views -----------------------------------------------------------------

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


__all__ = [
    "CLAIM_SETTLE_TTL_MULTIPLE",
    "DELIVERY_PASS_FRACTION",
    "EVENING_ADVISER_PRINCIPAL",
    "EVENING_NOT_COMMISSIONED",
    "EVENING_STATE_EVENT_TYPE",
    "JOIN_HYSTERESIS_FRACTION",
    "PINNED_SENTENCE",
    "REASON_BELOW_FLOOR",
    "REASON_CAPABILITY",
    "REASON_CLAIM_SETTLING",
    "REASON_IMPLAUSIBLE",
    "REASON_NOT_DELIVERING",
    "REASON_NO_ELIGIBLE",
    "REASON_ON_PLAN",
    "REASON_OUTSIDE_WINDOW",
    "REASON_SOC_FLOOR",
    "REASON_YIELDING",
    "STATE_EVENT_HEARTBEAT_S",
    "STOP_ROUTE_SENTENCE",
    "TIER_ALERT",
    "TIER_NOTICE",
    "TIER_RESOLVED",
    "BasisEvidence",
    "BasisFrame",
    "EveningShareAdviser",
    "EveningShareRefusal",
    "EveningShareSettings",
    "SplitResult",
    "basis_frame",
    "commanded_total_w",
    "evening_money_cents",
    "participant_split",
    "plausibility_verdict",
    "share_weight",
    "unit_basis_word",
]
