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
    ) -> AuditEvent:
        """Build one canonical event without performing I/O.

        ``unit_id`` attributes the decision to exactly one unit when the
        cycle selected one (2026-08-23 console Activity per-unit filters);
        a multi-unit decision stays fleet-level (``None``).
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

        setpoints = tuple(sorted(decision.setpoints, key=lambda item: item.unit_id))
        selected_unit_ids = getattr(intent, "selected_unit_ids", None)
        if selected_unit_ids is None:
            selected_unit_ids = intent.unit_ids
        authorized = (
            sum(_signed_watts(item.direction, item.watts) for item in setpoints)
            if authorization_batch is not None
            else 0
        )
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
