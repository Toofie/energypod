"""Deterministic intent arbitration and emergency-stop latching."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any, ClassVar, Protocol

from energypod.domain import IntentSource, PowerIntent

STOP_ACKNOWLEDGE_SCOPE = "stop-acknowledge"


class IntentRepository(Protocol):
    def remove(self, intent_id: str) -> None: ...


class IntentArbiter:
    """Select the highest-priority live intent without mutating input state."""

    _priority: ClassVar[dict[IntentSource, int]] = {
        IntentSource.EMERGENCY_STOP: 5,
        IntentSource.MANUAL: 4,
        IntentSource.AGENT: 3,
        IntentSource.OPTIMIZER: 2,
        IntentSource.SCHEDULE: 1,
    }

    def __init__(self, intent_repository: IntentRepository | None = None) -> None:
        self._repository = intent_repository
        self._latched_stop: PowerIntent | None = None

    def select(self, intents: Iterable[PowerIntent], now_mono: float) -> PowerIntent | None:
        if isinstance(now_mono, bool) or not isinstance(now_mono, int | float):
            raise TypeError("now_mono must be a finite number")
        if not math.isfinite(now_mono):
            raise ValueError("now_mono must be finite")
        if self._latched_stop is not None:
            return self._latched_stop
        eligible = [intent for intent in intents if self._is_live(intent, now_mono)]
        if not eligible:
            return None
        winner = min(
            eligible,
            key=lambda intent: (
                -self._priority[intent.source],
                -intent.acceptance_revision,
                intent.id,
            ),
        )
        if winner.source is IntentSource.EMERGENCY_STOP:
            self._latched_stop = winner
        return winner

    def acknowledge_emergency_stop(
        self,
        *,
        intent_id: str,
        actor_identity: str,
        operator_scopes: frozenset[str],
    ) -> None:
        if not actor_identity or actor_identity != actor_identity.strip():
            raise ValueError("actor identity must be non-empty and normalized")
        if STOP_ACKNOWLEDGE_SCOPE not in operator_scopes:
            raise PermissionError("stop acknowledgement scope is required")
        if self._latched_stop is None or self._latched_stop.id != intent_id:
            raise KeyError(intent_id)
        if self._repository is None:
            raise RuntimeError("an intent repository is required for acknowledgement")
        self._repository.remove(intent_id)
        self._latched_stop = None

    @staticmethod
    def _is_live(intent: Any, now_mono: float) -> bool:
        return bool(
            intent.accepted_at_mono <= now_mono < intent.accepted_at_mono + intent.duration_s
        )


__all__ = ["STOP_ACKNOWLEDGE_SCOPE", "IntentArbiter"]
