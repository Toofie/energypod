"""Pure, deterministic fleet safety evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from energypod.domain import (
    DataQuality,
    DecisionStatus,
    Direction,
    Observation,
    UnitLifecycle,
    UnitSetpoint,
)


@dataclass(frozen=True, slots=True)
class ControlDecision:
    status: DecisionStatus
    setpoints: tuple[UnitSetpoint, ...]
    reason_codes: tuple[str, ...]


class SafetyKernel:
    """Turn allocator proposals into bounded, short-lived authorizations."""

    # SYNC_RESILIENCE_AUDIT B1 (2026-08-24): every field here is served by the
    # control-rate core tier, so non-GOOD genuinely means "this cycle's read
    # is contradictory or unreadable" (class D, fail-closed correct).  The
    # system controller's SOC word -- served once per process and merged from
    # cache thereafter -- is deliberately absent: it is advisory telemetry
    # (see ``_system_soc_untrusted``), with the BMS SOC as the authoritative
    # figure every SOC bound consumes.
    _required_quality = tuple(sorted(Observation.REQUIRED_SAFETY_QUALITY_FIELDS))

    def evaluate(
        self,
        proposed_setpoints: tuple[Any, ...],
        current_observations: dict[str, Any],
        previous_observations: dict[str, Any],
        policy: Any,
        now_mono: float,
    ) -> ControlDecision:
        proposals = tuple(proposed_setpoints)
        if not proposals:
            return ControlDecision(DecisionStatus.REVOKED, (), ("no_setpoints",))
        proposal_reasons = self._proposal_reasons(proposals, policy, now_mono)
        if proposal_reasons:
            return ControlDecision(
                DecisionStatus.REJECTED,
                tuple(self._zero_setpoint(p, now_mono) for p in proposals),
                tuple(sorted(proposal_reasons)),
            )
        if all(p.direction is Direction.IDLE and p.watts == 0 for p in proposals):
            return ControlDecision(
                DecisionStatus.AUTHORIZED,
                tuple(self._zero_setpoint(p, now_mono) for p in proposals),
                ("stop_authorized",),
            )
        if all(p.watts == 0 for p in proposals):
            # An ACTIVE intent whose allocation contains no deliverable watts
            # anywhere (2026-08-23 live fleet halt: a single-unit charge into
            # lhs while its BMS dynamic charge limit was 0 W). The honest
            # answer is a rejected decision the kernel audits every tick —
            # never an exception, and never an "authorized" empty grant.
            return ControlDecision(
                DecisionStatus.REJECTED,
                tuple(self._zero_setpoint(p, now_mono) for p in proposals),
                ("zero_dynamic_capability",),
            )

        # Concurrent per-unit operation (2026-08-24): every deny reason is
        # PER UNIT.  A denied unit becomes a zero-watt non-participant for its
        # own direction -- the 6abd869/d2163a5 non-participation doctrine
        # extended from zero-watt PROPOSALS to denied units -- while the other
        # units (including units running the OPPOSITE direction) still run.
        # Different units may carry different directions in one cycle; each
        # proposal is judged against its own unit's evidence exactly as today.
        denied: dict[str, set[str]] = {}
        limits: dict[str, int] = {}
        expiries: dict[str, float] = {}
        divergence_observed = False
        system_soc_untrusted = False
        for proposal in proposals:
            observation = current_observations.get(proposal.unit_id)
            previous = previous_observations.get(proposal.unit_id)
            if getattr(proposal, "watts", None) == 0:
                # A zero-watt proposal is explicit NON-participation: the
                # allocator had no usable headroom for this unit (2026-08-23
                # live rejection: a fleet discharge with MID at the SOC floor,
                # a fleet charge with units above the ceiling). No authority is
                # ever minted for it, so its telemetry cannot endanger
                # actuation and it must not veto the participating units.
                # The control kernel still refuses to mint ANY authority
                # without coherent observations for every selected unit.
                limits[proposal.unit_id] = 0
                expiries[proposal.unit_id] = now_mono
                continue
            if observation is not None and self._soc_divergence_observed(observation, policy):
                # 2026-08-24 operator ruling: the system-vs-BMS SOC
                # divergence is a WARNING, never a denial.  The stale system
                # word must not block power the battery's own figure says is
                # safe; the note below rides the decision's reason codes so
                # the audit trail and console keep seeing the disagreement.
                divergence_observed = True
            if observation is not None and self._system_soc_untrusted(observation):
                # B1's informational sibling: the once-per-process system-SOC
                # word decoded BAD/SUSPECT/absent while the BMS SOC is fresh
                # and GOOD.  Advisory only -- the note rides authorizing
                # decisions exactly like the divergence note.
                system_soc_untrusted = True
            unit_reasons = self._deny_reasons(proposal, observation, previous, policy, now_mono)
            if not unit_reasons and getattr(proposal, "export_bounded", False):
                # API_CONTRACTS "Excess-solar accelerated charging
                # (advisory)": defense in depth behind the allocator's export
                # bound.  A NON-ZERO export-bounded proposal re-derives the
                # fleet grid evidence here, over the observations the kernel
                # already holds, so a proposal that outran its evidence is
                # refused even if some other path minted it.  The bound's own
                # evidence gap zeroes the advisory units alone: an operator's
                # units in the same cycle never depended on it.
                unit_reasons = self._export_evidence_reasons(current_observations, policy, now_mono)
            if unit_reasons or observation is None:
                # ``_deny_reasons`` already denies a missing observation, so
                # the None arm is the type-visible spelling of that fact.
                denied[proposal.unit_id] = unit_reasons or {"observation_missing"}
                limits[proposal.unit_id] = 0
                expiries[proposal.unit_id] = now_mono
                continue
            limits[proposal.unit_id] = self._unit_limit(proposal, observation, policy)
            expiries[proposal.unit_id] = min(
                now_mono + policy.authorization_lifetime_s,
                proposal.intent_expires_at_mono,
                observation.captured_at_mono + policy.max_telemetry_age_s,
                observation.cell_captured_at_mono + policy.max_cell_age_s,
            )
            if limits[proposal.unit_id] <= 0:
                # Only units ASKED to deliver power can fail the
                # dynamic-capability check; a zero-watt proposal was already
                # skipped above as a non-participant by definition.
                denied[proposal.unit_id] = {"zero_dynamic_capability"}

        participating = [p for p in proposals if p.watts > 0 and p.unit_id not in denied]
        if not participating:
            # Nothing is deliverable anywhere: fail closed exactly like the
            # all-zero allocation -- a REJECTED decision the kernel audits
            # every tick, never an authorized empty grant.
            combined: set[str] = set()
            for unit_reasons in denied.values():
                combined.update(unit_reasons)
            return ControlDecision(
                DecisionStatus.REJECTED,
                tuple(self._zero_setpoint(p, now_mono) for p in proposals),
                tuple(sorted(combined)) or ("zero_dynamic_capability",),
            )

        bounded = {p.unit_id: min(p.watts, limits[p.unit_id]) for p in participating}
        # Fleet limits apply PER DIRECTION across that direction's subtotal in
        # this cycle (2026-08-24): fleet_charge_limit_w bounds the charge
        # subtotal, fleet_discharge_limit_w the discharge subtotal -- never one
        # blended budget.  A direction starved to zero by its own limit leaves
        # its units as zero-watt non-participants while the other direction
        # keeps its full budget.
        clamped = False
        by_direction: dict[Direction, list[str]] = {}
        for proposal in participating:
            by_direction.setdefault(proposal.direction, []).append(proposal.unit_id)
        for direction, unit_ids in sorted(by_direction.items(), key=lambda item: item[0].value):
            fleet_limit = (
                policy.fleet_charge_limit_w
                if direction is Direction.CHARGE
                else policy.fleet_discharge_limit_w
            )
            group = self._apply_fleet_limit({u: bounded[u] for u in unit_ids}, fleet_limit)
            clamped = clamped or any(
                group[u] != next(p.watts for p in participating if p.unit_id == u) for u in unit_ids
            )
            bounded.update(group)
        outcome_reasons: set[str] = set()
        for unit_reasons in denied.values():
            outcome_reasons.update(unit_reasons)
        if clamped:
            outcome_reasons.add("power_clamped")
        if divergence_observed:
            # Informational only (2026-08-24): the note rides an AUTHORITATIVE
            # decision -- this path is unreachable for a whole-cycle rejection
            # -- so ``soc_disagreement_observed`` can never be mistaken for a
            # blocking reason on the rejecting path.
            outcome_reasons.add("soc_disagreement_observed")
        if system_soc_untrusted:
            # Informational only (B1), same doctrine and same unreachable-on-
            # rejection path as the divergence note above.
            outcome_reasons.add("system_soc_untrusted")
        status = DecisionStatus.CLAMPED if clamped else DecisionStatus.AUTHORIZED
        reason_codes = tuple(sorted(outcome_reasons)) or ("safety_checks_passed",)
        setpoints = tuple(
            UnitSetpoint(
                unit_id=p.unit_id,
                direction=p.direction,
                watts=bounded.get(p.unit_id, 0),
                generation=getattr(p, "generation", 0),
                intent_id=p.intent_id,
                authorization_expires_at_mono=expiries[p.unit_id],
            )
            for p in proposals
        )
        return ControlDecision(status, setpoints, reason_codes)

    @staticmethod
    def _proposal_reasons(proposals: tuple[Any, ...], policy: Any, now: float) -> set[str]:
        reasons: set[str] = set()
        if not SafetyKernel._finite(now):
            reasons.add("invalid_evaluation_time")
        unit_ids = [getattr(proposal, "unit_id", None) for proposal in proposals]
        if len(set(unit_ids)) != len(unit_ids):
            reasons.add("duplicate_unit_setpoint")
        # Concurrent per-unit operation (2026-08-24): different units MAY carry
        # different directions in one proposal set -- the fleet-wide
        # ``mixed_directions`` rejection is gone.  Per-unit coherence is the
        # control kernel's matcher (every proposal must bind to its unit's
        # winning intent, direction included); the duplicate-unit defense here
        # still refuses one unit proposed twice in any direction.
        for proposal in proposals:
            direction = getattr(proposal, "direction", None)
            watts = getattr(proposal, "watts", None)
            if direction not in {Direction.CHARGE, Direction.DISCHARGE, Direction.IDLE}:
                reasons.add("invalid_direction")
            if type(watts) is not int or watts < 0:
                reasons.add("invalid_power")
            elif direction is Direction.IDLE and watts != 0:
                # IDLE must carry zero watts. The reverse coupling (active
                # direction must be positive) is deliberately absent: a
                # partially eligible fleet legitimately proposes ZERO watts
                # for selected units with no usable headroom (2026-08-23
                # live halt/rejection: units above the SOC ceiling). Zero is
                # always permitted; a zero-watt proposal can never carry
                # authority (the kernel's eligibility check refuses it).
                reasons.add("direction_power_mismatch")
            expiry = getattr(proposal, "intent_expires_at_mono", None)
            expiry_value = (
                float(cast(int | float, expiry)) if SafetyKernel._finite(expiry) else None
            )
            active = direction in {Direction.CHARGE, Direction.DISCHARGE} and watts != 0
            if active and (expiry_value is None or expiry_value <= now):
                reasons.add("intent_expired")
            unit_id = getattr(proposal, "unit_id", None)
            required_maps = (
                policy.static_charge_limit_w_by_unit,
                policy.static_discharge_limit_w_by_unit,
                policy.ramp_limit_w_per_s_by_unit,
                policy.apparent_power_limit_va_by_unit,
                policy.expected_cell_count_by_unit,
            )
            if (
                not isinstance(unit_id, str)
                or not unit_id
                or (active and any(unit_id not in values for values in required_maps))
            ):
                reasons.add("unit_policy_missing")
        return reasons

    def _deny_reasons(
        self, proposal: Any, observation: Any | None, previous: Any | None, policy: Any, now: float
    ) -> set[str]:
        if observation is None:
            return {"observation_missing"}
        reasons: set[str] = set()
        if previous is None:
            reasons.add("previous_observation_missing")
        for field in self._required_quality:
            if observation.quality.get(field) is not DataQuality.GOOD:
                reasons.add(f"quality_{field}")
        if observation.lifecycle not in {UnitLifecycle.ARMED_IDLE, UnitLifecycle.ACTIVE}:
            reasons.add("lifecycle_not_controllable")
        numeric = (
            observation.bms_soc_pct,
            observation.soh_pct,
            observation.battery_watts,
            observation.pack_voltage_v,
            observation.pack_current_a,
            observation.dynamic_charge_limit_w,
            observation.dynamic_discharge_limit_w,
        )
        if not all(self._finite(value) for value in numeric):
            reasons.add("nonfinite_safety_data")
        if self._finite(observation.captured_at_mono) and (
            now - observation.captured_at_mono > policy.max_telemetry_age_s
        ):
            reasons.add("telemetry_stale")
        if self._finite(observation.captured_at_mono) and observation.captured_at_mono > now:
            reasons.add("telemetry_from_future")
        if not self._finite(observation.cell_captured_at_mono):
            reasons.add("cell_data_missing")
        elif now - observation.cell_captured_at_mono > policy.max_cell_age_s:
            reasons.add("cell_data_stale")
        if (
            self._finite(observation.cell_captured_at_mono)
            and observation.cell_captured_at_mono > now
        ):
            reasons.add("cell_data_from_future")
        if previous is not None:
            if not self._finite(previous.captured_at_mono) or not self._finite(
                observation.captured_at_mono
            ):
                reasons.add("observation_time_invalid")
            elif float(previous.captured_at_mono) >= float(observation.captured_at_mono):
                reasons.add("observation_order_invalid")
            if previous.sequence >= observation.sequence:
                reasons.add("observation_sequence_invalid")
            if getattr(previous, "connection_epoch", None) != getattr(
                observation, "connection_epoch", None
            ):
                reasons.add("observation_epoch_changed")
            previous_cell_sequence = getattr(previous, "cell_sequence", None)
            current_cell_sequence = getattr(observation, "cell_sequence", None)
            if (
                type(previous_cell_sequence) is not int
                or type(current_cell_sequence) is not int
                # An unchanged cell sequence means the cell block was not
                # re-polled between control cycles (cells are polled less
                # frequently than the control rate). Freshness is enforced by
                # max_cell_age_s; only a regression contradicts the evidence.
                or previous_cell_sequence > current_cell_sequence
            ):
                reasons.add("cell_sequence_invalid")
        # The BMS SOC is the AUTHORITATIVE SOC for every policy bound
        # (2026-08-24 operator ruling: "If there's a disagreement, re-sync
        # based on whatever the battery says").  The system controller's SOC
        # word -- served once per connection by the tiered read plan -- is
        # not a second safety opinion: it may be hours stale on a cycled
        # unit, and the divergence it manufactures is informational only
        # (see ``_soc_divergence_observed``; the domain's
        # ``authoritative_soc_pct`` property spells the same figure).
        if self._finite(observation.bms_soc_pct):
            if (
                proposal.direction is Direction.DISCHARGE
                and observation.bms_soc_pct <= policy.min_soc_pct
            ):
                reasons.add("soc_below_discharge_floor")
            if (
                proposal.direction is Direction.CHARGE
                and observation.bms_soc_pct >= policy.max_soc_pct
            ):
                reasons.add("soc_above_charge_ceiling")
        if (
            self._finite(observation.bms_soc_pct)
            and previous is not None
            and self._finite(previous.bms_soc_pct)
            and abs(observation.bms_soc_pct - previous.bms_soc_pct) > policy.max_soc_jump_pct
        ):
            reasons.add("soc_jump")

        cells = observation.cell_voltages_v
        expected = policy.expected_cell_count_by_unit.get(proposal.unit_id)
        if cells is None or len(cells) != expected:
            reasons.add("cell_count_invalid")
        elif not all(self._finite(value) for value in cells):
            reasons.add("nonfinite_safety_data")
        else:
            if min(cells) < policy.min_cell_voltage_v:
                reasons.add("cell_voltage_low")
            if max(cells) > policy.max_cell_voltage_v:
                reasons.add("cell_voltage_high")
            imbalance = max(cells) - min(cells)
            if imbalance > policy.max_cell_imbalance_v and not math.isclose(
                imbalance,
                policy.max_cell_imbalance_v,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                reasons.add("cell_imbalance")
        temperatures = observation.temperatures_c
        if temperatures is None or not temperatures:
            reasons.add("temperatures_missing")
        elif not all(self._finite(value) for value in temperatures):
            reasons.add("nonfinite_safety_data")
        else:
            if min(temperatures) < policy.min_temperature_c:
                reasons.add("temperature_low")
            if max(temperatures) > policy.max_temperature_c:
                reasons.add("temperature_high")
            if max(temperatures) - min(temperatures) > policy.max_temperature_spread_c:
                reasons.add("temperature_spread")
        if observation.active_faults & policy.blocking_fault_codes:
            reasons.add("blocking_fault")
        if observation.active_warnings & policy.blocking_warning_codes:
            reasons.add("blocking_warning")
        return reasons

    @staticmethod
    def _system_soc_untrusted(observation: Any) -> bool:
        """Whether the advisory system-SOC word failed to decode GOOD.

        SYNC_RESILIENCE_AUDIT B1 (2026-08-24): the system controller's SOC
        block is the once-per-process tier, so a BAD/SUSPECT decode is cached
        for the process lifetime while the BMS SOC reads fresh and GOOD every
        cycle.  The figure is no longer consumed by any bound (the BMS SOC is
        authoritative), so its quality is surfaced as the informational
        ``system_soc_untrusted`` note on authorizing decisions -- never a
        denial and never a qualification reset.  The BMS SOC keeps its full
        fail-closed gate (``quality_bms_soc_pct``).
        """
        quality = getattr(observation, "quality", None)
        flag = quality.get("system_soc_pct") if isinstance(quality, Mapping) else None
        return flag is not None and flag is not DataQuality.GOOD

    @staticmethod
    def _soc_divergence_observed(observation: Any, policy: Any) -> bool:
        """Whether the system and BMS SOC figures disagree beyond tolerance.

        The old ``soc_disagreement`` deny reason is gone (2026-08-24 operator
        ruling: the battery's own BMS SOC is authoritative and a disagreement
        must not block power -- the system word the tiered read plan serves
        once per connection may be hours stale on a cycled unit, so the
        "disagreement" was mostly staleness).  The same inclusive boundary
        now thresholds an informational ``soc_disagreement_observed`` note on
        authorizing decisions: an audit/console warning, never a denial.
        """
        system = getattr(observation, "system_soc_pct", None)
        bms = getattr(observation, "bms_soc_pct", None)
        return (
            SafetyKernel._finite(system)
            and SafetyKernel._finite(bms)
            and (
                abs(float(cast(int | float, system)) - float(cast(int | float, bms)))
                > policy.max_soc_disagreement_pct
            )
        )

    def _export_evidence_reasons(
        self, current_observations: dict[str, Any], policy: Any, now_mono: float
    ) -> set[str]:
        """Fleet-wide grid-evidence denial for an export-bounded proposal.

        The export bound is computed from EVERY fleet unit's per-pod CT power
        (net across phases), so its evidence check is fleet-wide too: one
        unreadable phase is never treated as zero export.  Missing, bad and
        stale mirror the kernel's existing spelling — a unit with no
        observation, a ``None``/non-finite ``grid_power_w``, or a quality map
        without the key is ``export_evidence_missing``; a present-but-not-GOOD
        quality is ``export_evidence_bad``; evidence older than the armed
        ``export_telemetry_max_age_s`` is ``export_evidence_stale``.  An
        unarmed policy triple carries no bound to violate and yields nothing
        (the allocator never flags a proposal against one).
        """
        if policy.export_telemetry_max_age_s is None:
            return set()
        reasons: set[str] = set()
        for unit_id in policy.expected_cell_count_by_unit:
            observation = current_observations.get(unit_id)
            if observation is None:
                reasons.add("export_evidence_missing")
                continue
            grid = getattr(observation, "grid_power_w", None)
            quality = getattr(observation, "quality", None)
            flag = quality.get("grid_power_w") if isinstance(quality, Mapping) else None
            if grid is None or not self._finite(grid) or flag is None:
                reasons.add("export_evidence_missing")
                continue
            if flag is not DataQuality.GOOD:
                reasons.add("export_evidence_bad")
                continue
            captured = getattr(observation, "captured_at_mono", None)
            if self._finite(captured) and now_mono - float(cast(int | float, captured)) > (
                policy.export_telemetry_max_age_s
            ):
                reasons.add("export_evidence_stale")
        return reasons

    @staticmethod
    def _finite(value: Any) -> bool:
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )

    @staticmethod
    def _unit_limit(proposal: Any, observation: Any, policy: Any) -> int:
        if proposal.direction is Direction.CHARGE:
            static = policy.static_charge_limit_w_by_unit[proposal.unit_id]
            dynamic = observation.dynamic_charge_limit_w
        else:
            static = policy.static_discharge_limit_w_by_unit[proposal.unit_id]
            dynamic = observation.dynamic_discharge_limit_w
        apparent_va = policy.apparent_power_limit_va_by_unit[proposal.unit_id]
        reactive = abs(getattr(proposal, "reactive_vars", 0))
        if reactive > policy.reactive_limit_var or reactive > apparent_va:
            apparent = 0.0
        else:
            apparent = math.sqrt(max(0.0, apparent_va**2 - reactive**2))
        delta = policy.ramp_limit_w_per_s_by_unit[proposal.unit_id] * policy.heartbeat_interval_s
        # The evidence-corroborated telemetry convention is positive discharge and
        # negative charge. Commissioning must verify it before enabling actuation.
        current = observation.battery_watts
        ramp = (
            max(0.0, current + delta)
            if proposal.direction is Direction.DISCHARGE
            else max(0.0, -current + delta)
        )
        return max(0, int(min(static, dynamic, apparent, ramp)))

    @staticmethod
    def _apply_fleet_limit(values: dict[str, int], fleet_limit: int) -> dict[str, int]:
        total = sum(values.values())
        if total <= fleet_limit:
            return values
        result = {unit: value * fleet_limit // total for unit, value in values.items()}
        remainder = fleet_limit - sum(result.values())
        for unit in sorted(result):
            addition = min(values[unit] - result[unit], remainder)
            result[unit] += addition
            remainder -= addition
            if remainder == 0:
                break
        return result

    @staticmethod
    def _zero_setpoint(proposal: Any, now_mono: float) -> UnitSetpoint:
        return UnitSetpoint(
            unit_id=proposal.unit_id,
            direction=Direction.IDLE,
            watts=0,
            generation=getattr(proposal, "generation", 0),
            intent_id=proposal.intent_id,
            authorization_expires_at_mono=now_mono,
        )


__all__ = ["ControlDecision", "SafetyKernel"]
