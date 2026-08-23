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
from typing import Any, Literal, Protocol, TypeGuard

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


@dataclass(frozen=True, slots=True)
class ExcessChargeSettings:
    """The four behavioural keys of the ``excess_charging`` config block."""

    assumed_autonomous_charge_w: int
    min_acceleration_w: int
    exit_hysteresis_w: int
    intent_ttl_s: float


@dataclass(frozen=True, slots=True)
class ExcessChargeDecision:
    """One advisory tick's outcome, for audit and supervision observability."""

    action: Action
    target_unit_id: str | None
    eligible_charge_w: int
    proposed_watts: int
    reason_codes: tuple[str, ...]


class _Clock(Protocol):
    def monotonic(self) -> float: ...


class _ObservationPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class _SubmitPort(Protocol):
    async def __call__(self, *, unit_ids: Any, direction: Any, watts: Any, ttl_s: Any) -> Any: ...


def _finite_number(value: Any) -> TypeGuard[float]:
    """A real, finite measurement — never a bool masquerading as one."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


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
        grid_w = getattr(observation, "grid_power_w", None)
        if observation is None or not _finite_number(grid_w):
            return 0
        quality = getattr(observation, "quality", None)
        flag = quality.get("grid_power_w") if isinstance(quality, Mapping) else None
        if flag is not None and flag is not DataQuality.GOOD:
            return 0
        captured_at_mono = getattr(observation, "captured_at_mono", None)
        if not _finite_number(captured_at_mono):
            return 0
        age_s = float(now_mono) - captured_at_mono
        if age_s > float(max_age_s):
            return 0
        total_w += float(grid_w)
    eligible = max(0, math.floor(total_w) - int(margin))
    return min(int(limit), eligible)


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
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._clock = clock
        self._observations = observations
        self._intents = intents
        self._submit = submit
        # Hysteresis state: which intent id the adviser currently holds and
        # whether it is intervening (inside the hysteresis band).
        self._held_intent_id: str | None = None
        self._intervening = False

    async def tick(self) -> ExcessChargeDecision:
        """Evaluate the bound once and act; never raises past its ports."""
        now_mono = float(self._clock.monotonic())
        latest = await self._observations.all_latest()
        bound_w = eligible_export_charge_w(latest, self._policy, now_mono)
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
            return await self._withdraw(bound_w, ("yielding_to_higher_priority",))
        achievable_w = self._achievable_w(bound_w, target, latest)
        entry_w = self._settings.assumed_autonomous_charge_w + self._settings.min_acceleration_w
        exit_w = self._settings.assumed_autonomous_charge_w + self._settings.exit_hysteresis_w

        if self._intervening:
            if target is not None and achievable_w > exit_w:
                return await self._renew(target, achievable_w, bound_w)
            if bound_w <= 0:
                return await self._withdraw(bound_w, ("no_export_headroom",))
            if target is None:
                return await self._withdraw(bound_w, ("no_eligible_target",))
            return await self._withdraw(bound_w, ("below_exit_hysteresis",))

        if bound_w <= 0:
            return self._idle(bound_w, ("no_export_headroom",))
        if target is None:
            return self._idle(bound_w, ("no_eligible_target",))
        if achievable_w < entry_w:
            # Below autonomy + margin the pod's own self-consumption is
            # faster than anything the adviser could command: commanding
            # less would SLOW charging, so autonomy is left untouched.
            return self._idle(bound_w, ("no_acceleration_over_autonomy",))
        return await self._renew(target, achievable_w, bound_w)

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
            soc_pct = getattr(observation, "system_soc_pct", None)
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

    async def _renew(self, target: str, achievable_w: int, bound_w: int) -> ExcessChargeDecision:
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
        )

    async def _withdraw(self, bound_w: int, reasons: tuple[str, ...]) -> ExcessChargeDecision:
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
        )

    def _idle(self, bound_w: int, reasons: tuple[str, ...]) -> ExcessChargeDecision:
        return ExcessChargeDecision(
            action="idle",
            target_unit_id=None,
            eligible_charge_w=bound_w,
            proposed_watts=0,
            reason_codes=reasons,
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


__all__ = [
    "Action",
    "ExcessChargeAdviser",
    "ExcessChargeDecision",
    "ExcessChargeSettings",
    "eligible_export_charge_w",
]
