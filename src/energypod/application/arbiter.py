"""Deterministic intent arbitration and emergency-stop latching.

Concurrent per-unit operation (the 2026-08-24 operator requirement): the
arbiter selects a PER-UNIT WINNER SET, not one fleet winner.  For each unit,
the highest-priority live intent claiming it wins that unit; an intent's
effective scope is its selection minus the units higher-priority intents
claimed; an intent whose entire scope was claimed away is simply not
represented that cycle.  The priority order, the equal-priority tie rules
(newest acceptance revision, then stable id), expiry semantics, and
emergency-stop domination are unchanged -- applied per unit.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from energypod.domain import IntentSource, PowerIntent

STOP_ACKNOWLEDGE_SCOPE = "stop-acknowledge"


class IntentRepository(Protocol):
    def remove(self, intent_id: str) -> None: ...


@dataclass(frozen=True)
class CycleArbitration:
    """One control cycle's per-unit arbitration result.

    ``ranked`` holds every represented intent in arbitration order (priority,
    then acceptance revision, then id) -- an intent appears here exactly when
    at least one of its units survived the higher-priority claims.  ``scopes``
    maps each represented intent to the units it still holds, ``winners`` maps
    every claimed unit to its winning intent, and ``emergency`` carries the
    dominating stop when one is live or latched (a stop is the whole cycle:
    no other intent is represented, exactly as under single-winner selection).
    """

    ranked: tuple[PowerIntent, ...] = ()
    scopes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    winners: Mapping[str, PowerIntent] = field(default_factory=dict)
    emergency: PowerIntent | None = None

    @property
    def units(self) -> frozenset[str]:
        """Every unit this cycle selected (exactly the winners' keys)."""
        return frozenset(self.winners)

    @property
    def single(self) -> PowerIntent | None:
        """The one represented intent, when the cycle composed exactly one."""
        return self.ranked[0] if len(self.ranked) == 1 else None


class IntentArbiter:
    """Select the per-unit winner set without mutating input state."""

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
        """The legacy single-winner view (the fleet-wide highest-priority live
        intent).  The control kernel composes cycles through :meth:`arbitrate`
        instead; this view remains the pinned equivalence anchor for a cycle
        held by exactly one intent."""
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

    def arbitrate(self, intents: Iterable[PowerIntent], now_mono: float) -> CycleArbitration:
        """Compose one cycle's per-unit winner set from the live intents.

        Deterministic and pure in the intent set: intents are ranked by the
        existing fleet-wide rules, then processed in rank order so each unit is
        claimed by the first (highest-ranked) intent that names it.  A live or
        latched emergency stop is the whole cycle, exactly as under
        single-winner selection, and latches on first selection.
        """
        if isinstance(now_mono, bool) or not isinstance(now_mono, int | float):
            raise TypeError("now_mono must be a finite number")
        if not math.isfinite(now_mono):
            raise ValueError("now_mono must be finite")
        if self._latched_stop is not None:
            return self._stop_cycle(self._latched_stop)
        eligible = [intent for intent in intents if self._is_live(intent, now_mono)]
        if not eligible:
            return CycleArbitration()
        ranked = sorted(
            eligible,
            key=lambda intent: (
                -self._priority[intent.source],
                -intent.acceptance_revision,
                intent.id,
            ),
        )
        if ranked[0].source is IntentSource.EMERGENCY_STOP:
            # A stop dominates everything (unchanged): it is the entire cycle,
            # it claims exactly its own units, and it latches.
            stop = ranked[0]
            self._latched_stop = stop
            return self._stop_cycle(stop)
        claimed: set[str] = set()
        winners: dict[str, PowerIntent] = {}
        scopes: dict[str, frozenset[str]] = {}
        represented: list[PowerIntent] = []
        for intent in ranked:
            surviving = frozenset(
                unit_id for unit_id in intent.selected_unit_ids if unit_id not in claimed
            )
            # The intent claims its WHOLE selection against every lower-ranked
            # intent, even when some of those units were itself eroded away.
            claimed |= intent.selected_unit_ids
            if not surviving:
                # Entire scope claimed by higher-priority intents: not
                # represented this cycle; it returns when a claimer lapses.
                continue
            scopes[intent.id] = surviving
            represented.append(intent)
            for unit_id in surviving:
                winners[unit_id] = intent
        return CycleArbitration(
            ranked=tuple(represented),
            scopes=scopes,
            winners=winners,
            emergency=None,
        )

    @staticmethod
    def _stop_cycle(stop: PowerIntent) -> CycleArbitration:
        units = frozenset(stop.selected_unit_ids)
        return CycleArbitration(
            ranked=(stop,),
            scopes={stop.id: units},
            winners={unit_id: stop for unit_id in units},
            emergency=stop,
        )

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


__all__ = ["STOP_ACKNOWLEDGE_SCOPE", "CycleArbitration", "IntentArbiter"]
