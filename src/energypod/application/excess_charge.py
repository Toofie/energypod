"""The excess-solar accelerated-charging adviser (advisory authority only).

API_CONTRACTS "Excess-solar accelerated charging (advisory)".  The scenario
is the operator's: fleet-wide surplus PV — export measured on any phase,
billed net across phases — charges the neediest single-phase battery at a
rate its own per-phase CT-following autonomy can never reach.

This module holds NO new authority anywhere:

- it owns no transport, no authorization path, and no allocator or kernel
  role (it imports no control adapter and cannot);
- it submits ordinary short-TTL ``OPTIMIZER`` charge intents through the
  facade's internal advisory submission, so the arbiter, allocator,
  SafetyKernel, and per-unit authority path stay exactly the existing ones;
- hand-back to the pod's own autonomy is ALWAYS by non-renewal: the adviser
  never writes a register, never posts a stop triple, and never submits an
  idle or zero-watt intent.  When it stops renewing, the intent lapses by
  TTL, the kernel stops minting, the actor stops writing, and the measured
  ~3.5-4.0 s firmware watchdog returns the pod to its own self-consumption.

The beat-autonomy rule comes from the live environment facts (the observed
~-520..-560 W daytime self-charge): while renewed, the adviser's objective
REPLACES the pod's own self-consumption, so commanding less than autonomy
would SLOW charging.  Entry therefore requires
``achievable_w >= assumed_autonomous_charge_w + min_acceleration_w`` and
intervention continues only while
``achievable_w > assumed_autonomous_charge_w + exit_hysteresis_w``
(``exit_hysteresis_w < min_acceleration_w``, validated at configuration
time), so a dip between the thresholds never oscillates across the
watchdog gap.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, TypeGuard

from energypod.domain import DataQuality, Direction, IntentSource, UnitLifecycle

# The arbiter's own priority (emergency stop > manual > agent > optimizer >
# schedule > idle) already displaces the adviser per unit — live-verified
# 2026-08-23, when a console manual intent superseded an in-flight agent
# intent mid-window.  The adviser ADDITIONALLY yields on its own so it never
# spams renewals against a claim it cannot win: since concurrent operations
# (2026-08-24) the yield is PER UNIT — a higher-priority intent claiming the
# adviser's own target (its scope is one unit) withdraws it by removal (never
# a stop triple) and it does not re-post until that claim has expired AND the
# entry hysteresis re-qualifies, while a claim on a DIFFERENT unit leaves the
# advisory charge running in the same cycle.  A live emergency stop dominates
# every unit and always stands the adviser down.
_HIGHER_PRIORITY_SOURCES: frozenset[IntentSource] = frozenset(
    {IntentSource.EMERGENCY_STOP, IntentSource.MANUAL, IntentSource.AGENT}
)

_CONTROLLABLE_LIFECYCLES: frozenset[UnitLifecycle] = frozenset(
    {UnitLifecycle.ARMED_IDLE, UnitLifecycle.ACTIVE}
)

# The action vocabulary of one advisory tick.  "withdraw" is a repository
# removal of the adviser's own intent; it is never a stop triple, an idle
# intent, or a zero-watt submission.
Action = Literal["idle", "propose", "renew", "withdraw"]

# DESIGN_EXCESS_ACTIVATION §1: the projection's vocabulary additions — the
# participation states the tick alone cannot see.  The implemented decision
# codes (below, verbatim in the tick) plus these three are the ONE pinned
# reason vocabulary; two vocabularies for the same facts is the drift class
# the EE-calibration incident taught.
REASON_DISABLED_BY_CONFIG: Final[str] = "disabled_by_config"
REASON_DISABLED_BY_RUNTIME: Final[str] = "disabled_by_runtime"
REASON_ECONOMICS_ACKNOWLEDGEMENT_REQUIRED: Final[str] = "economics_acknowledgement_required"

# The fleet grid rollup's fail-closed words — the kernel's own export-evidence
# spellings — with the pinned "worst word wins" precedence (a unit that served
# no word at all is strictly less knowable than one that served a flagged or
# aged one).
ExportEvidence = Literal["good", "missing", "bad", "stale"]
_EXPORT_EVIDENCE_RANK: Final[dict[str, int]] = {
    "good": 0,
    "stale": 1,
    "bad": 2,
    "missing": 3,
}

HysteresisState = Literal["inactive", "entering", "holding", "exiting"]
EnabledOrigin = Literal["config", "runtime"]


@dataclass(frozen=True, slots=True)
class ExcessChargeSettings:
    """The four behavioural keys of the ``excess_charging`` config block."""

    assumed_autonomous_charge_w: int
    min_acceleration_w: int
    exit_hysteresis_w: int
    intent_ttl_s: float


@dataclass(frozen=True, slots=True)
class ExcessChargeDecision:
    """One advisory tick's outcome, for audit and supervision observability.

    ``export_evidence`` and ``fleet_export_w`` (DESIGN_EXCESS_ACTIVATION §1)
    carry the fleet grid rollup the tick already computed, so the projection
    can never disagree with the bound the tick acted on and no second
    observation read is needed: ``fleet_export_w`` is the floor of the summed
    export while every fleet unit's grid word is GOOD, and null on any
    missing/bad/stale word — never zero-filled.
    """

    action: Action
    target_unit_id: str | None
    eligible_charge_w: int
    proposed_watts: int
    reason_codes: tuple[str, ...]
    export_evidence: ExportEvidence = "missing"
    fleet_export_w: int | None = None


class _Clock(Protocol):
    def monotonic(self) -> float: ...


class _ObservationPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class _SubmitPort(Protocol):
    async def __call__(self, *, unit_ids: Any, direction: Any, watts: Any, ttl_s: Any) -> Any: ...


class _ParticipationPort(Protocol):
    """The tick-start participation read (DESIGN_EXCESS_ACTIVATION §3 P6).

    Returns ``None`` while the adviser participates this tick, or the pinned
    projection reason code (``disabled_by_config`` / ``disabled_by_runtime``
    / ``economics_acknowledgement_required``) while it does not.  The toggle
    flips only the flag behind this port; the next tick observes it.
    """

    def __call__(self) -> str | None: ...


def _finite_number(value: Any) -> TypeGuard[float]:
    """A real, finite measurement — never a bool masquerading as one."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _grid_evidence_word(observation: Any, *, max_age_s: float, now_mono: float) -> ExportEvidence:
    """One unit's grid word classified under the bound's own fail-closed rules.

    The kernel spellings: ``missing`` when the observation or the word itself
    is absent/non-finite, ``bad`` when a present quality key is not GOOD (a
    quality map that does not carry the key — e.g. a raw fleet projection —
    is judged on the value alone), ``stale`` when the capture is older than
    the armed freshness bound, ``good`` otherwise.
    """
    grid_w = getattr(observation, "grid_power_w", None)
    if observation is None or not _finite_number(grid_w):
        return "missing"
    quality = getattr(observation, "quality", None)
    flag = quality.get("grid_power_w") if isinstance(quality, Mapping) else None
    if flag is not None and flag is not DataQuality.GOOD:
        return "bad"
    captured_at_mono = getattr(observation, "captured_at_mono", None)
    if not _finite_number(captured_at_mono):
        return "missing"
    if float(now_mono) - captured_at_mono > float(max_age_s):
        return "stale"
    return "good"


def eligible_export_charge_w(observations: Mapping[str, Any], policy: Any, now_mono: float) -> int:
    """The deterministic export bound: one additional min() term, or zero.

    ``min(max_charge_from_export_w, max(0, floor(sum(grid_power_w)) -
    export_headroom_margin_w))`` over EVERY unit in the policy's per-unit
    maps (the whole fleet — the arbitrage is net across phases), fail-closed
    to 0 unless every fleet unit's grid word is finite and its quality GOOD
    (a quality map that does not carry the key — e.g. a raw fleet
    projection — is judged on the value alone; the domain Observation always
    carries the key and an unserved PCS block is independently ``None``) and
    no older than ``export_telemetry_max_age_s``.  One unreadable phase is
    NEVER treated as zero export, and an unarmed policy triple (the
    default) bounds an advisory charge to nothing at all.
    """
    limit = getattr(policy, "export_charge_limit_w", None)
    margin = getattr(policy, "export_headroom_margin_w", None)
    max_age_s = getattr(policy, "export_telemetry_max_age_s", None)
    if limit is None or margin is None or max_age_s is None:
        # The triple is all-or-none; any missing key means not armed.
        return 0
    fleet = getattr(policy, "expected_cell_count_by_unit", None)
    if not isinstance(fleet, Mapping) or not fleet:
        return 0
    total_w = 0.0
    for unit_id in fleet:
        observation = observations.get(unit_id)
        if _grid_evidence_word(observation, max_age_s=max_age_s, now_mono=now_mono) != "good":
            return 0
        total_w += float(getattr(observation, "grid_power_w", 0.0))
    eligible = max(0, math.floor(total_w) - int(margin))
    return min(int(limit), eligible)


def fleet_export_evidence(
    observations: Mapping[str, Any], policy: Any, now_mono: float
) -> tuple[ExportEvidence, int | None]:
    """The §1 fleet grid rollup under the bound's own fail-closed rules.

    Returns the worst per-unit grid word (precedence ``missing > bad >
    stale > good``) and the floor of the summed export (positive = export)
    while every word is GOOD — ``None`` on ANY missing/bad/stale word, never
    zero-filled: one unreadable phase is never treated as zero export.  An
    unarmed policy triple has no rollup to make and reads as ``missing``.
    """
    max_age_s = getattr(policy, "export_telemetry_max_age_s", None)
    fleet = getattr(policy, "expected_cell_count_by_unit", None)
    if max_age_s is None or not isinstance(fleet, Mapping) or not fleet:
        return "missing", None
    worst: ExportEvidence = "good"
    total_w = 0.0
    for unit_id in fleet:
        observation = observations.get(unit_id)
        word = _grid_evidence_word(observation, max_age_s=max_age_s, now_mono=now_mono)
        if word != "good":
            if _EXPORT_EVIDENCE_RANK[word] > _EXPORT_EVIDENCE_RANK[worst]:
                worst = word
            continue
        total_w += float(getattr(observation, "grid_power_w", 0.0))
    if worst != "good":
        return worst, None
    return "good", math.floor(total_w)


class ExcessChargeAdviser:
    """One target, one short-TTL intent, renewed once per fleet cycle.

    Renewal is remove-previous-then-submit-fresh: any failure to renew —
    adviser stall, process death, bound collapse, staleness, yield, or
    hysteresis exit — ends the intent by TTL and the firmware watchdog
    returns the pod to its own autonomy.  The target is exactly one unit at
    a time (the neediest: lowest SOC with charge headroom, ties by unit id),
    never a fleet-wide dispatch.
    """

    def __init__(
        self,
        *,
        settings: ExcessChargeSettings,
        policy: Any,
        clock: _Clock,
        observations: _ObservationPort,
        intents: _IntentPort,
        submit: _SubmitPort,
        participation: _ParticipationPort | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._clock = clock
        self._observations = observations
        self._intents = intents
        self._submit = submit
        self._participation = participation
        # Hysteresis state: which intent id the adviser currently holds and
        # whether it is intervening (inside the hysteresis band).
        self._held_intent_id: str | None = None
        self._intervening = False

    @property
    def held_intent_id(self) -> str | None:
        """The live adviser intent id, or ``None`` while holding nothing.

        The projection derives ``active`` from THIS fact — never a lifecycle
        guess — so it can never claim inactive while an adviser intent is
        still live (the withdraw-then-tick race).
        """
        return self._held_intent_id

    @property
    def intervening(self) -> bool:
        """Whether the adviser is inside the hysteresis band."""
        return self._intervening

    async def tick(self) -> ExcessChargeDecision:
        """Evaluate the bound once and act; never raises past its ports.

        The participation port is consumed AT TICK START (P6): a disabled
        tick still computes the bound and the evidence rollup (the suspended
        console keeps its what-would-it-do figures live), withdraws-if-held
        exactly once by removal, and then idles carrying the projection's own
        participation reason code.
        """
        now_mono = float(self._clock.monotonic())
        latest = await self._observations.all_latest()
        bound_w = eligible_export_charge_w(latest, self._policy, now_mono)
        evidence, fleet_export_w = fleet_export_evidence(latest, self._policy, now_mono)
        verdict = None if self._participation is None else self._participation()
        if verdict is not None:
            if self._held_intent_id is not None or self._intervening:
                return await self._withdraw(bound_w, (verdict,), evidence, fleet_export_w)
            return self._idle(bound_w, (verdict,), evidence, fleet_export_w)
        active = await self._intents.active(now_mono)
        target = self._select_target(latest)
        if self._target_claimed(target, active):
            # Operator precedence, PER UNIT (2026-08-24 concurrent operations):
            # a higher-priority intent claiming the adviser's own target (its
            # whole scope is one unit) displaces it for that tick -- withdraw
            # by removal (never a stop triple), keep the reason on the
            # decision, and reset the hysteresis so re-entry must clear the
            # ENTRY threshold again once the claim has expired.  A claim on a
            # DIFFERENT unit no longer stands the adviser down: the arbiter
            # runs both in one cycle now.  A live emergency stop claims every
            # unit (it dominates the whole cycle), so it always yields.
            return await self._withdraw(
                bound_w, ("yielding_to_higher_priority",), evidence, fleet_export_w
            )
        achievable_w = self._achievable_w(bound_w, target, latest)
        entry_w = self._settings.assumed_autonomous_charge_w + self._settings.min_acceleration_w
        exit_w = self._settings.assumed_autonomous_charge_w + self._settings.exit_hysteresis_w

        if self._intervening:
            if target is not None and achievable_w > exit_w:
                return await self._renew(target, achievable_w, bound_w, evidence, fleet_export_w)
            if bound_w <= 0:
                return await self._withdraw(
                    bound_w, ("no_export_headroom",), evidence, fleet_export_w
                )
            if target is None:
                return await self._withdraw(
                    bound_w, ("no_eligible_target",), evidence, fleet_export_w
                )
            return await self._withdraw(
                bound_w, ("below_exit_hysteresis",), evidence, fleet_export_w
            )

        if bound_w <= 0:
            return self._idle(bound_w, ("no_export_headroom",), evidence, fleet_export_w)
        if target is None:
            return self._idle(bound_w, ("no_eligible_target",), evidence, fleet_export_w)
        if achievable_w < entry_w:
            # Below autonomy + margin the pod's own self-consumption is
            # faster than anything the adviser could command: commanding
            # less would SLOW charging, so autonomy is left untouched.
            return self._idle(bound_w, ("no_acceleration_over_autonomy",), evidence, fleet_export_w)
        return await self._renew(target, achievable_w, bound_w, evidence, fleet_export_w)

    @staticmethod
    def _target_claimed(target: str | None, active: tuple[Any, ...]) -> bool:
        """Whether a higher-priority live intent claims the adviser's target.

        An emergency stop dominates every unit regardless of its own scope, so
        any live stop claims the target outright.  With no target there is
        nothing to claim and no advisory work this tick regardless.
        """
        for intent in active:
            source = getattr(intent, "source", None)
            if source is IntentSource.EMERGENCY_STOP:
                return True
            if source not in _HIGHER_PRIORITY_SOURCES or target is None:
                continue
            claimed = getattr(intent, "selected_unit_ids", None)
            if claimed is not None and target in frozenset(claimed):
                return True
        return False

    # --- internals -------------------------------------------------------

    def _select_target(self, latest: Mapping[str, Any]) -> str | None:
        """The neediest eligible unit; ties break by unit id.

        Eligible means: a latest observation exists, its lifecycle is
        controllable (ARMED_IDLE/ACTIVE — inhibited, disarmed and
        observe-only units are skipped), its SOC is below the policy's
        charge ceiling, and it has positive charge headroom under both the
        BMS dynamic limit and the static unit cap.  The kernel's existing
        deny reasons remain the backstop for anything this misses.
        """
        best: tuple[float, str] | None = None
        for unit_id in sorted(latest):
            observation = latest[unit_id]
            lifecycle = getattr(observation, "lifecycle", None)
            if lifecycle not in _CONTROLLABLE_LIFECYCLES:
                continue
            # The BMS SOC is the authoritative SOC (2026-08-24 operator
            # ruling): neediness and the ceiling skip follow the battery's
            # own figure, never the system word the tiered read plan serves
            # once per connection and may hold stale for hours.
            soc_pct = getattr(observation, "authoritative_soc_pct", None)
            if not _finite_number(soc_pct):
                continue
            if soc_pct >= self._policy.max_soc_pct:
                continue
            if self._achievable_w(1 << 62, unit_id, latest) <= 0:
                continue
            key = (soc_pct, str(unit_id))
            if best is None or key < best:
                best = key
        return None if best is None else best[1]

    def _achievable_w(self, bound_w: int, unit_id: str | None, latest: Mapping[str, Any]) -> int:
        """min(bound, target dynamic charge headroom, static unit cap)."""
        if unit_id is None:
            return 0
        observation = latest.get(unit_id)
        dynamic = getattr(observation, "dynamic_charge_limit_w", None)
        if not _finite_number(dynamic):
            return 0
        dynamic_w = math.floor(dynamic)
        static = self._policy.static_charge_limit_w_by_unit.get(unit_id)
        if not isinstance(static, int):
            return 0
        return max(0, min(int(bound_w), dynamic_w, int(static)))

    async def _renew(
        self,
        target: str,
        achievable_w: int,
        bound_w: int,
        evidence: ExportEvidence,
        fleet_export_w: int | None,
    ) -> ExcessChargeDecision:
        """Remove the previous adviser intent, then submit a fresh one."""
        action: Action = "renew" if self._intervening else "propose"
        await self._remove_held()
        result = await self._submit(
            unit_ids=[target],
            direction=Direction.CHARGE,
            watts=achievable_w,
            ttl_s=self._settings.intent_ttl_s,
        )
        submitted_id = result.get("intent_id") if isinstance(result, Mapping) else None
        self._held_intent_id = submitted_id if isinstance(submitted_id, str) else None
        self._intervening = True
        return ExcessChargeDecision(
            action=action,
            target_unit_id=target,
            eligible_charge_w=bound_w,
            proposed_watts=achievable_w,
            reason_codes=("export_headroom_available",),
            export_evidence=evidence,
            fleet_export_w=fleet_export_w,
        )

    async def _withdraw(
        self,
        bound_w: int,
        reasons: tuple[str, ...],
        evidence: ExportEvidence,
        fleet_export_w: int | None,
    ) -> ExcessChargeDecision:
        """Withdraw by non-renewal: remove the held intent, submit nothing.

        No stop triple, no idle intent, no zero-watt submission — the TTL
        lapse plus the firmware watchdog are the designed hand-back.  The
        action is "withdraw" exactly when the adviser held something to
        withdraw; otherwise standing down is already "idle".
        """
        withdrawing = self._held_intent_id is not None or self._intervening
        await self._remove_held()
        self._intervening = False
        return ExcessChargeDecision(
            action="withdraw" if withdrawing else "idle",
            target_unit_id=None,
            eligible_charge_w=bound_w,
            proposed_watts=0,
            reason_codes=reasons,
            export_evidence=evidence,
            fleet_export_w=fleet_export_w,
        )

    def _idle(
        self,
        bound_w: int,
        reasons: tuple[str, ...],
        evidence: ExportEvidence,
        fleet_export_w: int | None,
    ) -> ExcessChargeDecision:
        return ExcessChargeDecision(
            action="idle",
            target_unit_id=None,
            eligible_charge_w=bound_w,
            proposed_watts=0,
            reason_codes=reasons,
            export_evidence=evidence,
            fleet_export_w=fleet_export_w,
        )

    async def _remove_held(self) -> None:
        held = self._held_intent_id
        self._held_intent_id = None
        if held is None:
            return
        # Removal is opportunistic churn control, never the safety path: an
        # intent the store no longer knows (already expired and evicted)
        # must not fail the tick — the TTL lapse is the designed hand-back.
        with contextlib.suppress(Exception):
            await self._intents.remove(held)


# --- the operator-facing projection (DESIGN_EXCESS_ACTIVATION §1/§2) ------------


class ExcessChargingRefusal(Exception):
    """A guarded-toggle refusal carrying its wire code and details.

    The facade raises exactly this for the three 409 shapes of §3; the
    guarded boundary maps ``code`` onto the error envelope verbatim.
    """

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ExcessAdviserState:
    """The frozen §1 projection: the feature's whole story in one object.

    Single writer: the fleet loop's post-tick update through
    ``ExcessAdviserController.observe_tick`` owns every tick-derived field;
    the toggle flips only the participation flag; ``active`` and
    ``held_intent_id`` are composed from the adviser's LIVE held fact so the
    projection can never claim inactive while an adviser intent is live.
    """

    enabled: bool
    enabled_origin: EnabledOrigin
    acknowledged_economics: bool
    active: bool
    hysteresis_state: HysteresisState
    target_unit_id: str | None
    commanded_charge_w: int
    eligible_export_charge_w: int
    fleet_export_w: int | None
    export_evidence: ExportEvidence
    charge_cap_w: int
    held_intent_id: str | None
    last_action: Action
    last_tick_at: str
    reason_codes: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """The §1 JSON shape (values JSON-native, codes as a list)."""
        return {
            "enabled": self.enabled,
            "enabled_origin": self.enabled_origin,
            "acknowledged_economics": self.acknowledged_economics,
            "active": self.active,
            "hysteresis_state": self.hysteresis_state,
            "target_unit_id": self.target_unit_id,
            "commanded_charge_w": self.commanded_charge_w,
            "eligible_export_charge_w": self.eligible_export_charge_w,
            "fleet_export_w": self.fleet_export_w,
            "export_evidence": self.export_evidence,
            "charge_cap_w": self.charge_cap_w,
            "held_intent_id": self.held_intent_id,
            "last_action": self.last_action,
            "last_tick_at": self.last_tick_at,
            "reason_codes": list(self.reason_codes),
        }

    def event_payload(self) -> dict[str, Any]:
        """The §2 ``excess_adviser.state_changed`` payload (the §1 subset the
        event contract carries: no ``charge_cap_w``, ``last_action``, or
        ``last_tick_at`` — the snapshot owns those)."""
        return {
            "enabled": self.enabled,
            "enabled_origin": self.enabled_origin,
            "acknowledged_economics": self.acknowledged_economics,
            "active": self.active,
            "hysteresis_state": self.hysteresis_state,
            "target_unit_id": self.target_unit_id,
            "commanded_charge_w": self.commanded_charge_w,
            "eligible_export_charge_w": self.eligible_export_charge_w,
            "fleet_export_w": self.fleet_export_w,
            "export_evidence": self.export_evidence,
            "reason_codes": list(self.reason_codes),
            "held_intent_id": self.held_intent_id,
        }

    def semantic_tuple(self) -> tuple[Any, ...]:
        """The §2 throttle tuple: the semantic state that triggers an event.

        The watt figures are deliberately absent — they ride every
        publication but never trigger one.
        """
        return (
            self.enabled,
            self.enabled_origin,
            self.acknowledged_economics,
            self.active,
            self.hysteresis_state,
            self.target_unit_id,
            self.export_evidence,
            self.reason_codes,
        )


class _EventPublisherPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class _WallClock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> Any: ...


# §2: the heartbeat cadence is a constant, not a config key.
STATE_EVENT_HEARTBEAT_S: Final[float] = 30.0
STATE_EVENT_TYPE: Final[str] = "excess_adviser.state_changed"


class ExcessAdviserController:
    """Participation flag, acknowledgement latch, projection, and events.

    Composition wiring order is pinned: the controller is built FIRST (it
    owns the participation flag the guarded toggle flips), the adviser is
    then constructed with ``participation_verdict`` as its tick-start port,
    and the controller binds the adviser for the live held-intent read.

    Writers are split by ownership, never by race: the fleet loop's
    ``observe_tick`` is the single writer of every tick-derived field; the
    facade's toggle is the only writer of the participation flag and the
    acknowledgement latch; readers (facade snapshot, toggle response, event
    publication) compose a frozen view from both plus the adviser's live
    ``held_intent_id``.
    """

    def __init__(
        self,
        *,
        charge_cap_w: int,
        clock: _WallClock,
        acknowledged_economics: bool,
        config_enabled: bool,
        bus: _EventPublisherPort | None = None,
        heartbeat_period_s: float = STATE_EVENT_HEARTBEAT_S,
    ) -> None:
        if isinstance(charge_cap_w, bool) or not isinstance(charge_cap_w, int) or charge_cap_w <= 0:
            raise ValueError("charge_cap_w must be a positive integer")
        if isinstance(heartbeat_period_s, bool) or not heartbeat_period_s > 0:
            raise ValueError("heartbeat_period_s must be positive")
        self._charge_cap_w = charge_cap_w
        self._clock = clock
        self._bus = bus
        self._heartbeat_period_s = float(heartbeat_period_s)
        # Participation state (toggle-owned; P1: never persisted — boot
        # recomposes from config, which is why the boot value is captured
        # here and the origin reads "config" until a toggle changes it).
        self._enabled = bool(config_enabled)
        self._enabled_origin: EnabledOrigin = "config"
        # The once-ever durable net-billing fact (§3 P3), boot-loaded from
        # the audit store and flipped only by the audited first enable.
        self._acknowledged = bool(acknowledged_economics)
        self._adviser: ExcessChargeAdviser | None = None
        # Tick-derived fields (fleet-loop-owned single writer).  Before the
        # first tick the honest frame is a zero bound over MISSING evidence:
        # nothing has been read yet, and the first tick replaces it within
        # one cycle.
        self._last_action: Action = "idle"
        self._last_target: str | None = None
        self._last_proposed_w: int = 0
        self._last_bound_w: int = 0
        self._last_evidence: ExportEvidence = "missing"
        self._last_fleet_export_w: int | None = None
        self._last_reason_codes: tuple[str, ...] = ("export_evidence_missing",)
        self._last_tick_at = clock.wall_now().isoformat()
        # §2 publication state.
        self._published_tuple: tuple[Any, ...] | None = None
        self._last_publish_mono: float | None = None

    def bind_adviser(self, adviser: ExcessChargeAdviser) -> None:
        """Bind the adviser for the live held-intent read (exactly once)."""
        if self._adviser is not None:
            raise RuntimeError("the excess adviser controller is already bound")
        self._adviser = adviser

    # --- participation (the toggle's half) --------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def enabled_origin(self) -> EnabledOrigin:
        return self._enabled_origin

    @property
    def acknowledged_economics(self) -> bool:
        return self._acknowledged

    def set_participation(self, *, enabled: bool) -> None:
        """Flip the participation flag; the next tick observes it.

        The origin becomes ``runtime`` — the honest "until restart" marker.
        P5: this flips participation ONLY; every commissioned envelope (cap,
        export triple, read plan, hysteresis, TTL, mode) is a composition
        fact the toggle can never touch.
        """
        self._enabled = bool(enabled)
        self._enabled_origin = "runtime"

    def mark_acknowledged(self) -> None:
        """Latch the captured net-billing fact (after its durable append)."""
        self._acknowledged = True

    def participation_verdict(self) -> str | None:
        """``None`` while the adviser participates; else its reason code.

        Effective participation is the flag AND the acknowledgement: an
        unacknowledged site composes SUSPENDED even with the config block
        enabled (P3's fail-closed gate), and a runtime disable outranks the
        pending acknowledgement in the vocabulary.
        """
        if not self._enabled:
            if self._enabled_origin == "runtime":
                return REASON_DISABLED_BY_RUNTIME
            return REASON_DISABLED_BY_CONFIG
        if not self._acknowledged:
            return REASON_ECONOMICS_ACKNOWLEDGEMENT_REQUIRED
        return None

    # --- the projection (the fleet loop's half) ----------------------------

    def state(self) -> ExcessAdviserState:
        """Compose the frozen §1 view from the live participation and held
        facts plus the last tick's decision."""
        verdict = self.participation_verdict()
        held = self._adviser.held_intent_id if self._adviser is not None else None
        active = held is not None
        if active:
            # The held fact outranks everything: the projection must never
            # claim inactive (nor "inactive" hysteresis) while an adviser
            # intent is still live — the withdraw-then-tick race.
            hysteresis: HysteresisState = "holding"
        elif verdict is not None:
            hysteresis = "inactive"
        elif self._last_action == "withdraw":
            hysteresis = "exiting"
        else:
            hysteresis = "entering"
        if verdict is None or active:
            # Participating (or still holding through the pre-withdraw
            # transient): the tick's own codes and target, VERBATIM.
            reason_codes = self._last_reason_codes
            target_unit_id = self._last_target
            commanded = self._last_proposed_w if self._last_action in ("propose", "renew") else 0
        else:
            reason_codes = (verdict,)
            target_unit_id = None
            commanded = 0
        return ExcessAdviserState(
            enabled=self._enabled,
            enabled_origin=self._enabled_origin,
            acknowledged_economics=self._acknowledged,
            active=active,
            hysteresis_state=hysteresis,
            target_unit_id=target_unit_id,
            commanded_charge_w=commanded,
            eligible_export_charge_w=self._last_bound_w,
            fleet_export_w=self._last_fleet_export_w,
            export_evidence=self._last_evidence,
            charge_cap_w=self._charge_cap_w,
            held_intent_id=held,
            last_action=self._last_action,
            last_tick_at=self._last_tick_at,
            reason_codes=reason_codes,
        )

    def state_payload(self) -> dict[str, Any]:
        """The §1 JSON shape (the facade/toggle read surface)."""
        return self.state().payload()

    async def observe_tick(self, decision: ExcessChargeDecision) -> None:
        """The single-writer post-tick update, then the §2 publication.

        Publishes ``excess_adviser.state_changed`` only when the semantic
        tuple changes — watt figures ride but never trigger — and, while
        ``enabled`` is true, republishes the full payload as a heartbeat
        every ``heartbeat_period_s``.  While disabled, no heartbeat: the
        state_changed to disabled is the last event.  A publication failure
        propagates to the fleet loop's suppression (the projection write
        above has already landed); it never gates control.
        """
        self._last_action = decision.action
        self._last_target = decision.target_unit_id
        self._last_proposed_w = int(decision.proposed_watts)
        self._last_bound_w = int(decision.eligible_charge_w)
        self._last_evidence = decision.export_evidence
        self._last_fleet_export_w = decision.fleet_export_w
        self._last_reason_codes = tuple(decision.reason_codes)
        self._last_tick_at = self._clock.wall_now().isoformat()
        if self._bus is None:
            return
        state = self.state()
        semantic = state.semantic_tuple()
        now_mono = float(self._clock.monotonic())
        heartbeat = False
        if semantic != self._published_tuple:
            heartbeat = False
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
        self._last_publish_mono = now_mono


__all__ = [
    "REASON_DISABLED_BY_CONFIG",
    "REASON_DISABLED_BY_RUNTIME",
    "REASON_ECONOMICS_ACKNOWLEDGEMENT_REQUIRED",
    "STATE_EVENT_HEARTBEAT_S",
    "STATE_EVENT_TYPE",
    "Action",
    "EnabledOrigin",
    "ExcessAdviserController",
    "ExcessAdviserState",
    "ExcessChargeAdviser",
    "ExcessChargeDecision",
    "ExcessChargeSettings",
    "ExcessChargingRefusal",
    "ExportEvidence",
    "HysteresisState",
    "eligible_export_charge_w",
    "fleet_export_evidence",
]
