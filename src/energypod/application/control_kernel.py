"""One fail-closed control-orchestration cycle."""

from __future__ import annotations

import asyncio
import itertools
import math
from contextlib import suppress
from typing import Any, NoReturn

from energypod.domain import (
    AuthorizationBatch,
    AuthorizedSetpoint,
    DecisionStatus,
    Direction,
    IntentSource,
    UnitLifecycle,
)
from energypod.domain.audit import AuditEvent
from energypod.domain.authorization import StaleGenerationError

from .generation import AuthorityGenerationCoordinator


class _FixedGenerationSource:
    """Explicit fixed source retained only for isolated deterministic composition."""

    def __init__(self, epoch: int) -> None:
        from .generation import AuthorityGenerationSnapshot

        self._snapshot = AuthorityGenerationSnapshot(epoch)

    async def snapshot(self) -> Any:
        return self._snapshot


class _ImmutableSequenceMap(dict[str, int]):
    """A JSON-native mapping that rejects mutation after construction."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("audit observation sequences are immutable")

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def __setitem__(self, key: str, value: int) -> None:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: str, default: Any = None) -> int:
        self._immutable()

    def popitem(self) -> tuple[str, int]:
        self._immutable()

    def setdefault(self, key: str, default: int = 0) -> int:
        self._immutable()

    def update(self, *args: Any, **kwargs: int) -> None:
        self._immutable()


class ControlKernel:
    """Mint capabilities only after allocation, safety evaluation, and audit."""

    def __init__(
        self,
        *,
        clock: Any,
        unit_ids: frozenset[str],
        intents: Any,
        observations: Any,
        authorizations: Any,
        audit: Any,
        arbiter: Any,
        allocator: Any,
        safety: Any,
        policy: Any,
        generation_coordinator: AuthorityGenerationCoordinator | None = None,
        generation: int | None = None,
        configuration_version: int,
        audit_event_factory: Any,
    ) -> None:
        if not unit_ids or any(
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
            for unit_id in unit_ids
        ):
            raise ValueError("unit_ids must contain normalized identifiers")
        if type(configuration_version) is not int or configuration_version < 0:
            raise ValueError("configuration_version must be a non-negative integer")
        if audit_event_factory is None:
            # Authority must never be granted on a degraded audit trail. A
            # composition without a canonical factory is a construction error.
            raise TypeError("audit_event_factory is required for canonical auditing")
        self._clock = clock
        self._unit_ids = frozenset(unit_ids)
        self._intents = intents
        self._observations = observations
        self._authorizations = authorizations
        self._audit = audit
        self._arbiter = arbiter
        self._allocator = allocator
        self._safety = safety
        self._policy = policy
        if (generation_coordinator is None) == (generation is None):
            raise ValueError("provide exactly one authority generation source")
        self._generation_coordinator = (
            generation_coordinator
            if generation_coordinator is not None
            else _FixedGenerationSource(generation)  # type: ignore[arg-type]
        )
        self._configuration_version = configuration_version
        self._audit_event_factory = audit_event_factory
        self._cycle_numbers = itertools.count(1)
        self._decision_numbers = itertools.count(1)
        # A control kernel is a single authority. Overlapping ticks could both
        # evaluate the same evidence and race audit/publication, so serialize
        # the complete authority-granting transaction rather than only publish.
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> Any | None:
        async with self._tick_lock:
            return await self._tick_owned()

    async def _tick_owned(self) -> Any | None:
        issued_at = self._clock.monotonic()
        if not self._finite(issued_at):
            await self._revoke_after_failure("invalid_monotonic_time")
            raise ValueError("the monotonic clock must return a finite number")
        try:
            intents = await self._intents.active(issued_at)
            current = await self._observations.all_latest()
            previous = await self._observations.all_previous()
            winner = self._arbiter.select(intents, issued_at)
            if winner is None:
                await self._revoke("no_active_intent")
                return None

            # API_CONTRACTS "Excess-solar accelerated charging (advisory)":
            # the allocator receives the tick's own monotonic time so the
            # export bound judges grid-evidence freshness at the moment of
            # allocation, exactly as the kernel's staleness checks do.
            proposals = tuple(self._allocator.allocate(winner, current, self._policy, issued_at))
            if not self._proposals_match_intent(winner, proposals):
                raise ValueError("allocator output does not match the selected intent")
            decision = self._safety.evaluate(proposals, current, previous, self._policy, issued_at)
            emergency = winner.source is IntentSource.EMERGENCY_STOP
            if emergency:
                # Revocation must never wait behind durable audit I/O.
                await self._revoke("emergency_stop")

            # 2026-08-23 publish-fence generation desync: every kernel
            # revocation fences the repository through the last published
            # generation, and minting at an epoch at or below that fence is
            # rejected by the publish CAS forever.  Reconcile the epoch the
            # kernel consults past the repository's permanent fence BEFORE
            # minting, so a fresh intent publishes on its first cycle.
            selected = self._selected_units(winner)
            if selected <= self._unit_ids:
                await self._reconcile_generation(selected, "authority_generation_reconciled")
            generation_before_mint = (await self._generation_coordinator.snapshot()).epoch
            # Stable per-authority sequencing makes canonical fingerprints
            # reproducible while preserving uniqueness within the sole runtime
            # kernel. UUID suffixes remain unnecessary in the single-process
            # authority model.
            cycle_id = f"cycle-{next(self._cycle_numbers):020d}"
            decision_id = f"decision-{next(self._decision_numbers):020d}"
            batch = self._mint_batch(
                winner,
                decision,
                current,
                previous,
                issued_at,
                generation_before_mint,
                cycle_id,
                decision_id,
            )
            generation_before_audit = (await self._generation_coordinator.snapshot()).epoch
            if generation_before_audit != generation_before_mint:
                # Revocation fences first; the fenced cycle still receives a
                # durable audit record afterward, with the minted batch treated
                # as never granted (authorized watts zero).
                await self._revoke("authority_generation_changed")
                fenced_event = self._create_audit_event(
                    intent=winner,
                    decision=decision,
                    batch=None,
                    observations=current,
                    generation=generation_before_mint,
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                    decided_at_mono=issued_at,
                )
                await self._audit.append(fenced_event)
                return decision
            audit_event = self._create_audit_event(
                intent=winner,
                decision=decision,
                batch=batch,
                observations=current,
                generation=generation_before_mint,
                cycle_id=cycle_id,
                decision_id=decision_id,
                decided_at_mono=issued_at,
            )
            await self._audit.append(audit_event)

            if batch is None or emergency:
                if not emergency:
                    await self._revoke("control_decision_not_authorized")
                return decision

            deadline = min(item.expires_at_mono for item in batch.authorizations)
            if self._clock.monotonic() >= deadline:
                await self._revoke("control_cycle_deadline_expired")
                return decision
            generation_before_publish = (await self._generation_coordinator.snapshot()).epoch
            if generation_before_publish != generation_before_mint:
                await self._revoke("authority_generation_changed")
                return decision
            try:
                await self._authorizations.publish(batch)
            except StaleGenerationError:
                # The repository's generation CAS fenced this cycle between the
                # last snapshot and publication — the same outcome as the
                # mint-to-audit snapshot checks, arrived one await later. A
                # legitimate fence is not a component failure: revoke, append
                # the durable zero-authorized audit record, and let the next
                # tick run in the new generation instead of halting
                # supervision.
                await self._revoke("authority_generation_changed")
                fenced_event = self._create_audit_event(
                    intent=winner,
                    decision=decision,
                    batch=None,
                    observations=current,
                    generation=generation_before_mint,
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                    decided_at_mono=issued_at,
                )
                await self._audit.append(fenced_event)
                return decision
            return decision
        except BaseException:
            await self._revoke_after_failure("control_cycle_failed")
            raise

    def _mint_batch(
        self,
        intent: Any,
        decision: Any,
        current: dict[str, Any],
        previous: dict[str, Any],
        issued_at: float,
        generation: int,
        cycle_id: str,
        decision_id: str,
    ) -> AuthorizationBatch | None:
        selected = self._selected_units(intent)
        setpoints = tuple(getattr(decision, "setpoints", ()))
        if not self._eligible(intent, decision, selected, setpoints, current, previous, issued_at):
            return None

        capabilities = tuple(
            AuthorizedSetpoint(
                unit_id=setpoint.unit_id,
                connection_epoch=current[setpoint.unit_id].connection_epoch,
                generation=generation,
                cycle_id=cycle_id,
                intent_id=intent.id,
                intent_revision=intent.acceptance_revision,
                direction=setpoint.direction,
                watts=setpoint.watts,
                reactive_vars=getattr(setpoint, "reactive_vars", 0),
                issued_at_mono=issued_at,
                not_before_mono=issued_at,
                expires_at_mono=setpoint.authorization_expires_at_mono,
                observation_sequence=current[setpoint.unit_id].sequence,
                maximum_observation_age_s=self._policy.max_telemetry_age_s,
                policy_version=self._policy.version,
                configuration_version=self._configuration_version,
                decision_id=decision_id,
            )
            # Zero-watt setpoints are non-participants: they stay in the
            # audited decision but never carry authority.
            for setpoint in sorted(setpoints, key=lambda item: item.unit_id)
            if setpoint.watts > 0
        )
        return AuthorizationBatch(
            cycle_id=cycle_id,
            generation=generation,
            authorizations=capabilities,
        )

    def _create_audit_event(
        self,
        *,
        intent: Any,
        decision: Any,
        batch: AuthorizationBatch | None,
        observations: dict[str, Any],
        generation: int,
        cycle_id: str,
        decision_id: str,
        decided_at_mono: float,
    ) -> Any:
        # Console per-unit attribution (2026-08-23 Activity view): a decision
        # that selected exactly one unit carries that unit's id on its audit
        # row; a genuinely multi-unit decision stays fleet-level -- one row
        # cannot honestly name one of several units.
        selected = self._selected_units(intent)
        attributed_unit: str | None = next(iter(selected)) if len(selected) == 1 else None
        event = self._audit_event_factory.create(
            authorization_batch=batch,
            configuration_version=self._configuration_version,
            cycle_id=cycle_id,
            decided_at_mono=decided_at_mono,
            decision=decision,
            decision_id=decision_id,
            generation=generation,
            intent=intent,
            observations=observations,
            policy_version=self._policy.version,
            unit_id=attributed_unit,
        )
        if type(event) is not AuditEvent:
            raise TypeError("audit event factory must return an exact AuditEvent")
        # The domain's read-only Mapping implementation is intentionally
        # defensive but Pydantic cannot serialize that arbitrary type. Replace
        # it with an equally immutable dict subclass at this application
        # boundary so durable repositories can emit canonical JSON.
        event = event.model_copy(
            update={"observation_sequences": _ImmutableSequenceMap(event.observation_sequences)}
        )
        # The audit basis is every observation the kernel actually held for
        # this cycle; selected units without telemetry cannot contribute
        # sequences and must not crash the audit path.
        expected_sequences = {
            unit_id: observations[unit_id].sequence for unit_id in sorted(observations)
        }
        setpoints = tuple(sorted(getattr(decision, "setpoints", ()), key=lambda item: item.unit_id))
        expected_authorized = (
            sum(
                (-item.watts if item.direction is Direction.CHARGE else item.watts)
                for item in decision.setpoints
            )
            if batch is not None
            else 0
        )
        expected_requested = (
            (-intent.watts if intent.direction is Direction.CHARGE else intent.watts)
            if intent.direction is not Direction.IDLE
            else 0
        )
        expected_requested_map = getattr(intent, "watts_by_unit", None)
        expected_authorized_map = (
            {item.unit_id: item.watts for item in sorted(setpoints, key=lambda item: item.unit_id)}
            if batch is not None
            else None
        )
        expected_lifecycle = (
            UnitLifecycle.INHIBITED
            if intent.source is IntentSource.EMERGENCY_STOP
            else UnitLifecycle.ARMED_IDLE
            if intent.direction is Direction.IDLE or decision.status is DecisionStatus.REJECTED
            else UnitLifecycle.ACTIVE
            if decision.status in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED}
            else UnitLifecycle.INHIBITED
        )
        if (
            event.event_type != "control_decision"
            or event.unit_id != attributed_unit
            or event.connection_epoch is not None
            or event.generation != generation
            or event.cycle_id != cycle_id
            or event.principal != intent.actor_identity
            or event.source is not intent.source
            or event.intent_id != intent.id
            or event.policy_version != self._policy.version
            or event.configuration_version != self._configuration_version
            or dict(event.observation_sequences) != expected_sequences
            or event.reason_codes != tuple(decision.reason_codes)
            or event.requested_active_w != expected_requested
            or event.authorized_active_w != expected_authorized
            or event.requested_watts_by_unit
            != (None if expected_requested_map is None else dict(expected_requested_map))
            or event.authorized_watts_by_unit
            != (None if expected_authorized_map is None else dict(expected_authorized_map))
            or event.result != decision.status.value
            or event.lifecycle is not expected_lifecycle
        ):
            raise ValueError("audit event does not match control cycle facts")
        return event

    def _eligible(
        self,
        intent: Any,
        decision: Any,
        selected: frozenset[str],
        setpoints: tuple[Any, ...],
        current: dict[str, Any],
        previous: dict[str, Any],
        issued_at: float,
    ) -> bool:
        if (
            intent.source is IntentSource.EMERGENCY_STOP
            or not selected
            or not selected <= self._unit_ids
            or not selected <= current.keys()
            or not selected <= previous.keys()
            or decision.status not in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED}
            or len(setpoints) != len(selected)
            or {getattr(item, "unit_id", None) for item in setpoints} != selected
        ):
            return False

        seen: set[str] = set()
        total_watts = 0
        participating = 0
        for setpoint in setpoints:
            unit_id = getattr(setpoint, "unit_id", None)
            expiry = getattr(setpoint, "authorization_expires_at_mono", None)
            watts = getattr(setpoint, "watts", None)
            reactive_vars = getattr(setpoint, "reactive_vars", 0)
            expiry_value = (
                float(expiry)
                if isinstance(expiry, int | float) and not isinstance(expiry, bool)
                else -math.inf
            )
            if (
                not isinstance(unit_id, str)
                or unit_id in seen
                or getattr(setpoint, "intent_id", None) != intent.id
                or getattr(setpoint, "direction", None) is not intent.direction
                or intent.direction is Direction.IDLE
                or type(watts) is not int
                or watts < 0
                or type(reactive_vars) is not int
            ):
                return False
            if watts == 0:
                # Zero watts with an active direction is explicit
                # NON-participation (a selected unit with no usable headroom —
                # 2026-08-23 live rejections). No authority is minted for it,
                # so the authority-granting checks below do not apply; the
                # unit's observations still join the evidence-coherence gate.
                seen.add(unit_id)
                continue
            if (
                not self._finite(expiry)
                or expiry_value <= issued_at
                or expiry_value > intent.expires_at_mono
            ):
                return False
            seen.add(unit_id)
            participating += 1
            total_watts += watts
        # An active intent that participates nowhere mints no authority at
        # all: fail-closed, never an empty capability batch.
        if participating == 0:
            return False
        return total_watts <= intent.watts and self._evidence_is_coherent(
            selected, current, previous
        )

    @staticmethod
    def _evidence_is_coherent(
        selected: frozenset[str], current: dict[str, Any], previous: dict[str, Any]
    ) -> bool:
        for unit_id in selected:
            current_item = current[unit_id]
            previous_item = previous[unit_id]
            current_epoch = getattr(current_item, "connection_epoch", None)
            previous_epoch = getattr(previous_item, "connection_epoch", None)
            current_sequence = getattr(current_item, "sequence", None)
            previous_sequence = getattr(previous_item, "sequence", None)
            if (
                getattr(current_item, "unit_id", None) != unit_id
                or getattr(previous_item, "unit_id", None) != unit_id
                or type(current_epoch) is not int
                or type(previous_epoch) is not int
                or type(current_sequence) is not int
                or type(previous_sequence) is not int
                or current_epoch < 0
                or previous_epoch < 0
                or previous_sequence < 0
                or current_sequence < 0
                or (previous_epoch == current_epoch and current_sequence <= previous_sequence)
            ):
                return False
        return True

    def _proposals_match_intent(self, intent: Any, proposals: Any) -> bool:
        """Reject confused or malicious allocator output before safety evaluation."""
        try:
            items = tuple(proposals)
            selected = self._selected_units(intent)
            per_unit_targets = getattr(intent, "watts_by_unit", None)
        except (AttributeError, TypeError, ValueError):
            return False
        if not selected or len(items) != len(selected):
            return False
        if per_unit_targets is not None and set(per_unit_targets) != selected:
            return False
        seen: set[str] = set()
        total_watts = 0
        for item in items:
            unit_id = getattr(item, "unit_id", None)
            watts = getattr(item, "watts", None)
            if (
                not isinstance(unit_id, str)
                or unit_id not in selected
                or unit_id in seen
                or getattr(item, "intent_id", None) != intent.id
                or getattr(item, "direction", None) is not intent.direction
                or type(watts) is not int
                or watts < 0
            ):
                return False
            if per_unit_targets is not None:
                # A per-unit intent makes every unit's target that unit's own
                # cap (the 2026-08-23 operator ruling): a proposal may never
                # exceed the target the operator named for THIS battery, even
                # when the fleet total still fits.
                target = per_unit_targets.get(unit_id)
                if type(target) is not int or watts > target:
                    return False
            # A zero-watt entry is legitimate for three reasons the domain
            # allocator contract pins: an IDLE intent carries all zeros, a
            # partially eligible fleet carries zero watts for selected units
            # with no usable headroom (2026-08-23 live halt: a 3-unit charge
            # with two units above the SOC ceiling), and a fully ineligible
            # selection carries ALL zeros (2026-08-23 live fleet halt: a
            # single-unit charge into a pod whose BMS dynamic limit was 0 W
            # raised here and ended the fleet task). Zero watts is inherently
            # safe — the safety kernel rejects an all-zero active allocation
            # and `_eligible` never mints authority for a zero-watt setpoint —
            # so the matcher enforces only unit-set, identity, direction,
            # per-target, and total bounds.
            seen.add(unit_id)
            total_watts += watts
        if intent.direction is Direction.IDLE and total_watts != 0:
            return False
        return seen == selected and total_watts <= intent.watts

    @staticmethod
    def _selected_units(intent: Any) -> frozenset[str]:
        selected = getattr(intent, "selected_unit_ids", None)
        if selected is None:
            selected = intent.unit_ids
        return frozenset(selected)

    @staticmethod
    def _finite(value: Any) -> bool:
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )

    async def _revoke(self, reason: str) -> None:
        await self._authorizations.revoke(self._unit_ids, reason=reason)
        # The revocation just fenced the repository through the last
        # published generation; the epoch consulted for the next mint must
        # reconcile strictly beyond it, or that mint is permanently fenced
        # (the 2026-08-23 authorized-but-never-dispatched live incident).
        await self._reconcile_generation(self._unit_ids, reason)

    async def _reconcile_generation(self, unit_ids: frozenset[str], reason: str) -> None:
        """Advance the epoch this kernel consults past the repository's fence.

        Generation fencing is the core invariant and stays fully intact: the
        defect was minting AT a fenced epoch, never the fence's existence.
        The repository owns the authoritative permanent fence, so its
        revoked-through watermark — not the coordinator's own counter —
        decides what is dead: any revocation path, in this kernel or in
        another component, is reflected here by advancing the coordinator
        strictly beyond that watermark.  Everything at or below the fence
        stays permanently unpublishable; the next tick mints at a live epoch.

        A legitimate fence that lands mid-cycle, between this reconciliation
        and publication, is still caught by the repository's publish CAS:
        that cycle audits its zero-authorized fenced record and the NEXT
        cycle reconciles and succeeds.  Ports without these operations
        (isolated deterministic compositions pinned to a fixed generation,
        structural fakes) keep their exact current behavior; a malformed
        fence read is skipped so it can never jeopardize the revocation
        itself — the fence at publish remains the fail-closed backstop.
        """
        reader = getattr(self._authorizations, "revoked_through", None)
        reconciler = getattr(self._generation_coordinator, "advance_past", None)
        if reader is None or reconciler is None:
            return
        fence = await reader(unit_ids)
        if type(fence) is int and not isinstance(fence, bool) and fence >= 0:
            await reconciler(fence, reason=reason)

    async def _revoke_after_failure(self, reason: str) -> None:
        task = asyncio.create_task(self._revoke(reason))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        except Exception:
            return


__all__ = ["ControlKernel"]
