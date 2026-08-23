"""Deterministic construction of canonical control-decision audit events."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from energypod.domain import DecisionStatus, Direction, IntentSource, UnitLifecycle
from energypod.domain.audit import AuditEvent

_SCHEMA = "energypod.control-decision.v1"


def _required_identity(name: str, value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty normalized string")
    return value


def _canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _signed_watts(direction: Direction, watts: int) -> int:
    if direction is Direction.CHARGE:
        return -watts
    if direction is Direction.DISCHARGE:
        return watts
    return 0


def _aggregate_lifecycle(intent: Any, decision: Any) -> UnitLifecycle:
    if intent.source is IntentSource.EMERGENCY_STOP:
        return UnitLifecycle.INHIBITED
    if intent.direction is Direction.IDLE or decision.status is DecisionStatus.REJECTED:
        return UnitLifecycle.ARMED_IDLE
    if decision.status in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED}:
        return UnitLifecycle.ACTIVE
    return UnitLifecycle.INHIBITED


class AuditEventFactory:
    """Create immutable audit facts from an explicit, secret-free allowlist.

    Identity, wall time, and process origin are injected so tests and production
    composition can control every nondeterministic input. Fingerprints are
    deterministic projections and deliberately omit credentials and ambient
    request state.
    """

    def __init__(
        self,
        *,
        process_instance_id: str,
        process_origin_mono: float,
        wall_now: Callable[[], datetime],
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._process_instance_id = _required_identity("process_instance_id", process_instance_id)
        if (
            not isinstance(process_origin_mono, int | float)
            or isinstance(process_origin_mono, bool)
            or not math.isfinite(process_origin_mono)
        ):
            raise ValueError("process_origin_mono must be finite")
        if not callable(wall_now):
            raise TypeError("wall_now must be callable")
        if event_id_factory is not None and not callable(event_id_factory):
            raise TypeError("event_id_factory must be callable")
        self._process_origin_mono = float(process_origin_mono)
        self._wall_now = wall_now
        self._event_id_factory = event_id_factory or (lambda: f"event-{uuid4()}")

    def create(
        self,
        *,
        authorization_batch: Any | None,
        configuration_version: int,
        cycle_id: str,
        decided_at_mono: float,
        decision: Any,
        decision_id: str,
        generation: int,
        intent: Any,
        observations: Mapping[str, Any],
        policy_version: str | int,
        unit_id: str | None = None,
        composition: Any | None = None,
    ) -> AuditEvent:
        """Build one canonical event without performing I/O.

        ``unit_id`` attributes the decision to exactly one unit when the
        cycle selected one (2026-08-23 console Activity per-unit filters);
        a multi-unit decision stays fleet-level (``None``).

        ``composition`` is the cycle's per-unit arbitration (2026-08-24
        concurrent operations).  A cycle composed from exactly one intent --
        ``composition`` absent or holding one intent -- builds the SAME row
        that row has always been, byte for byte.  A cycle composed from
        several intents is cycle-level: no single intent can honestly be
        named, so the row correlates to its cycle, joins the represented
        principals, keeps the dominant source, and carries each unit's own
        winning direction in ``directions_by_unit`` next to the per-unit watt
        breakdowns.
        """
        occurred_at = self._wall_now()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("wall_now must return a timezone-aware datetime")
        occurred_at = occurred_at.astimezone(UTC)
        event_id = _required_identity("event_id", self._event_id_factory())
        cycle_id = _required_identity("cycle_id", cycle_id)
        decision_id = _required_identity("decision_id", decision_id)
        if unit_id is not None:
            unit_id = _required_identity("unit_id", unit_id)

        represented: list[Any] = (
            [intent] if composition is None else list(getattr(composition, "ranked", ()))
        ) or [intent]
        composed = len(represented) > 1

        setpoints = tuple(sorted(decision.setpoints, key=lambda item: item.unit_id))
        authorized = (
            sum(_signed_watts(item.direction, item.watts) for item in setpoints)
            if authorization_batch is not None
            else 0
        )
        response_projection = {
            "schema": _SCHEMA,
            "authorized_active_w": authorized,
            "decision_id": decision_id,
            "reason_codes": list(decision.reason_codes),
            "setpoints": [
                {
                    "direction": item.direction.value,
                    "reactive_vars": getattr(item, "reactive_vars", 0),
                    "unit_id": item.unit_id,
                    "watts": item.watts,
                }
                for item in setpoints
            ],
            "status": decision.status.value,
        }
        if not composed:
            return self._single_intent_event(
                authorization_batch=authorization_batch,
                composed_intent=intent,
                configuration_version=configuration_version,
                cycle_id=cycle_id,
                decision=decision,
                decision_id=decision_id,
                event_id=event_id,
                generation=generation,
                occurred_at=occurred_at,
                decided_at_mono=decided_at_mono,
                observations=observations,
                policy_version=policy_version,
                response_projection=response_projection,
                unit_id=unit_id,
                authorized=authorized,
            )
        scopes = getattr(composition, "scopes", {})
        winners = getattr(composition, "winners", {})
        # Per-unit watt breakdowns (2026-08-23 fleet-row opacity fix), now
        # composed: each unit's winner's own target when it carried per-unit
        # targets, and each intent's request re-summed over its SURVIVING
        # scope (an eroded unit's target left with its claimer).  Unsigned,
        # like the domain; the projections stay canonical (sorted keys).
        requested_active_w = 0
        requested_watts_by_unit: dict[str, int] = {}
        intents_projection: list[dict[str, Any]] = []
        for represented_intent in represented:
            scope = scopes.get(represented_intent.id, frozenset())
            targets = getattr(represented_intent, "watts_by_unit", None)
            request = (
                represented_intent.watts
                if targets is None
                else sum(targets[claimed] for claimed in scope if claimed in targets)
            )
            requested_active_w += _signed_watts(represented_intent.direction, request)
            if targets is not None:
                for claimed in scope:
                    if claimed in targets:
                        requested_watts_by_unit[claimed] = targets[claimed]
            projection = {
                "acceptance_revision": represented_intent.acceptance_revision,
                "direction": represented_intent.direction.value,
                "intent_id": represented_intent.id,
                "principal": represented_intent.actor_identity,
                "source": represented_intent.source.value,
                "unit_ids": sorted(scope),
                "watts": request,
            }
            intents_projection.append(projection)
        directions_by_unit = {
            claimed: winners[claimed].direction.value for claimed in sorted(winners)
        }
        request_projection = {
            "schema": _SCHEMA,
            "intents": sorted(intents_projection, key=lambda item: item["intent_id"]),
            "unit_ids": sorted(winners),
        }
        principal = ",".join(sorted({item.actor_identity for item in represented}))
        dominant = represented[0]
        if dominant.source is IntentSource.EMERGENCY_STOP:  # pragma: no cover - stops compose alone
            lifecycle = UnitLifecycle.INHIBITED
        elif all(item.direction is Direction.IDLE for item in represented) or (
            decision.status is DecisionStatus.REJECTED
        ):
            lifecycle = UnitLifecycle.ARMED_IDLE
        elif decision.status in {DecisionStatus.AUTHORIZED, DecisionStatus.CLAMPED}:
            lifecycle = UnitLifecycle.ACTIVE
        else:
            lifecycle = UnitLifecycle.INHIBITED
        return AuditEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            monotonic_offset_s=float(decided_at_mono) - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type="control_decision",
            unit_id=unit_id,
            connection_epoch=None,
            generation=generation,
            cycle_id=cycle_id,
            principal=principal,
            source=dominant.source,
            # A row composed from several intents cannot honestly name one:
            # it correlates to its cycle and carries no single intent id.
            correlation_id=f"cycle:{cycle_id}",
            intent_id=None,
            policy_version=policy_version,
            configuration_version=configuration_version,
            observation_sequences={
                unit_id: observation.sequence
                for unit_id, observation in sorted(observations.items())
            },
            reason_codes=tuple(decision.reason_codes),
            requested_active_w=requested_active_w,
            authorized_active_w=authorized,
            requested_watts_by_unit=(
                None
                if not requested_watts_by_unit
                else dict(sorted(requested_watts_by_unit.items()))
            ),
            authorized_watts_by_unit=(
                {item.unit_id: item.watts for item in setpoints}
                if authorization_batch is not None
                else None
            ),
            directions_by_unit=directions_by_unit,
            request_fingerprint=_canonical_fingerprint(request_projection),
            response_fingerprint=_canonical_fingerprint(response_projection),
            result=decision.status.value,
            lifecycle=lifecycle,
        )

    def _single_intent_event(
        self,
        *,
        authorization_batch: Any | None,
        composed_intent: Any,
        configuration_version: int,
        cycle_id: str,
        decision: Any,
        decision_id: str,
        event_id: str,
        generation: int,
        occurred_at: Any,
        decided_at_mono: float,
        observations: Mapping[str, Any],
        policy_version: str | int,
        response_projection: Mapping[str, Any],
        unit_id: str | None,
        authorized: int,
    ) -> AuditEvent:
        intent = composed_intent
        setpoints = tuple(sorted(decision.setpoints, key=lambda item: item.unit_id))
        selected_unit_ids = getattr(intent, "selected_unit_ids", None)
        if selected_unit_ids is None:
            selected_unit_ids = intent.unit_ids
        # Per-unit watt breakdowns (2026-08-23 fleet-row opacity fix): the
        # intent's own targets when it carried them, and the decision's per-
        # unit authorized watts whenever a batch was minted.  Unsigned, like
        # the domain; the fingerprint projections stay canonical (sorted
        # keys, integers only) and gain the breakdown only when present so a
        # scalar intent's fingerprint is byte-identical to before.
        requested_watts_by_unit = getattr(intent, "watts_by_unit", None)
        request_projection = {
            "schema": _SCHEMA,
            "acceptance_revision": intent.acceptance_revision,
            "direction": intent.direction.value,
            "intent_id": intent.id,
            "principal": intent.actor_identity,
            "source": intent.source.value,
            "unit_ids": sorted(selected_unit_ids),
            "watts": intent.watts,
        }
        if requested_watts_by_unit is not None:
            request_projection["watts_by_unit"] = dict(sorted(requested_watts_by_unit.items()))
        return AuditEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            monotonic_offset_s=float(decided_at_mono) - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type="control_decision",
            unit_id=unit_id,
            connection_epoch=None,
            generation=generation,
            cycle_id=cycle_id,
            principal=intent.actor_identity,
            source=intent.source,
            # A decision held by a latched stop correlates to the stop itself
            # (2026-08-23 console Activity): the row names its stop without
            # guessing from the newest latch event.
            correlation_id=(
                f"emergency_stop:{intent.id}"
                if intent.source is IntentSource.EMERGENCY_STOP
                else f"intent:{intent.id}:revision:{intent.acceptance_revision}"
            ),
            intent_id=intent.id,
            policy_version=policy_version,
            configuration_version=configuration_version,
            observation_sequences={
                unit_id: observation.sequence
                for unit_id, observation in sorted(observations.items())
            },
            reason_codes=tuple(decision.reason_codes),
            requested_active_w=_signed_watts(intent.direction, intent.watts),
            authorized_active_w=authorized,
            requested_watts_by_unit=(
                None
                if requested_watts_by_unit is None
                else dict(sorted(requested_watts_by_unit.items()))
            ),
            authorized_watts_by_unit=(
                {item.unit_id: item.watts for item in setpoints}
                if authorization_batch is not None
                else None
            ),
            request_fingerprint=_canonical_fingerprint(request_projection),
            response_fingerprint=_canonical_fingerprint(response_projection),
            result=decision.status.value,
            lifecycle=_aggregate_lifecycle(intent, decision),
        )


__all__ = ["AuditEventFactory"]
