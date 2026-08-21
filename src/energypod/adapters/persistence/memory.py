"""Bounded process-local repositories for observations and capabilities."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable
from threading import RLock
from typing import Any

from energypod.domain.authorization import AuthorizationBatch, StaleGenerationError


class InMemoryObservationRepository:
    def __init__(self, *, max_history_per_unit: int) -> None:
        if type(max_history_per_unit) is not int or max_history_per_unit <= 0:
            raise ValueError("max_history_per_unit must be positive")
        self._history: dict[str, deque[Any]] = defaultdict(
            lambda: deque(maxlen=max_history_per_unit)
        )
        self._lock = RLock()

    def append(self, observation: Any) -> None:
        with self._lock:
            history = self._history[observation.unit_id]
            if history:
                latest = history[-1]
                if observation.connection_epoch < latest.connection_epoch:
                    raise ValueError("connection epoch must not decrease")
                if (
                    observation.connection_epoch == latest.connection_epoch
                    and observation.sequence <= latest.sequence
                ):
                    raise ValueError("observation sequence must increase within an epoch")
            history.append(observation)

    def latest(self, unit_id: str) -> Any | None:
        with self._lock:
            history = self._history.get(unit_id)
            return history[-1] if history else None

    def all_latest(self) -> dict[str, Any]:
        with self._lock:
            return {unit_id: history[-1] for unit_id, history in self._history.items() if history}

    def history(self, unit_id: str) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._history.get(unit_id, ()))


class InMemoryAuthorizationRepository:
    """Atomic, single-use capability publication with permanent generation fences."""

    def __init__(
        self,
        *,
        max_cycles_per_unit_generation: int = 4096,
        max_tracked_units: int = 1024,
    ) -> None:
        if type(max_cycles_per_unit_generation) is not int or max_cycles_per_unit_generation <= 0:
            raise ValueError("max_cycles_per_unit_generation must be positive")
        if type(max_tracked_units) is not int or max_tracked_units <= 0:
            raise ValueError("max_tracked_units must be positive")
        self._current: dict[str, Any] = {}
        self._generation_floor: dict[str, int] = {}
        self._revoked_through: dict[str, int] = {}
        # Only cycles in the newest accepted generation need storage. Older
        # generations are rejected by the permanent generation floor, so their
        # cycle identities can be discarded without making replay possible.
        self._seen_cycles: dict[str, tuple[int, set[str]]] = {}
        self._max_cycles_per_unit_generation = max_cycles_per_unit_generation
        self._max_tracked_units = max_tracked_units
        self._lock = RLock()

    def publish(self, batch: AuthorizationBatch) -> None:
        authorizations = tuple(batch.authorizations)
        if not authorizations:
            raise ValueError("authorization batch must not be empty")
        with self._lock:
            seen_units: set[str] = set()
            new_units: set[str] = set()
            for authorization in authorizations:
                unit_id = authorization.unit_id
                generation = authorization.generation
                if authorization.cycle_id != batch.cycle_id or generation != batch.generation:
                    raise ValueError("authorization does not belong to its batch")
                if unit_id in seen_units:
                    raise ValueError("authorization batch contains duplicate unit")
                seen_units.add(unit_id)
                if unit_id not in self._generation_floor:
                    new_units.add(unit_id)
                floor = self._generation_floor.get(unit_id, -1)
                seen_generation, cycles = self._seen_cycles.get(unit_id, (-1, set()))
                cycle_seen = seen_generation == generation and authorization.cycle_id in cycles
                if (
                    generation < floor
                    or generation <= self._revoked_through.get(unit_id, -1)
                    or cycle_seen
                ):
                    raise StaleGenerationError(unit_id, generation)
                existing = self._current.get(unit_id)
                if existing is not None and generation < existing.generation:
                    raise StaleGenerationError(unit_id, generation)
                if (
                    seen_generation == generation
                    and len(cycles) >= self._max_cycles_per_unit_generation
                ):
                    raise RuntimeError(
                        "authorization cycle ledger exhausted; advance and fence generation"
                    )
            if len(self._generation_floor) + len(new_units) > self._max_tracked_units:
                raise RuntimeError("authorization unit ledger exhausted")

            # Mutation starts only after every member has passed preflight.
            for authorization in authorizations:
                unit_id = authorization.unit_id
                generation = authorization.generation
                self._current[unit_id] = authorization
                seen_generation, cycles = self._seen_cycles.get(unit_id, (-1, set()))
                if seen_generation != generation:
                    cycles = set()
                    self._seen_cycles[unit_id] = (generation, cycles)
                cycles.add(authorization.cycle_id)
                self._generation_floor[unit_id] = max(
                    authorization.generation,
                    self._generation_floor.get(unit_id, -1),
                )

    def current(self, unit_id: str, now_monotonic: float) -> Any | None:
        if not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip():
            raise ValueError("unit_id must be non-empty and normalized")
        if (
            isinstance(now_monotonic, bool)
            or not isinstance(now_monotonic, int | float)
            or not math.isfinite(now_monotonic)
        ):
            raise ValueError("now_monotonic must be finite")
        with self._lock:
            authorization = self._current.get(unit_id)
            if authorization is None:
                return None
            if now_monotonic < authorization.not_before_mono:
                return None
            self._current.pop(unit_id, None)
            if now_monotonic >= authorization.expires_at_mono:
                return None
            return authorization

    def revoke(
        self,
        *,
        unit_id: str | None = None,
        generation: int | None = None,
        unit_ids: Iterable[str] | None = None,
        reason: str | None = None,
    ) -> None:
        del reason
        if generation is not None and (type(generation) is not int or generation < 0):
            raise ValueError("generation must be a non-negative integer")
        if unit_id is not None and (
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
        ):
            raise ValueError("unit_id must be non-empty and normalized")
        supplied_unit_ids = unit_ids is not None
        targets = set(unit_ids or ())
        if any(
            not isinstance(target, str) or not target or target != target.strip()
            for target in targets
        ):
            raise ValueError("unit_ids must contain normalized identifiers")
        with self._lock:
            if unit_id is not None:
                targets.add(unit_id)
            if generation is not None:
                unknown = targets.difference(self._generation_floor)
                if len(self._generation_floor) + len(unknown) > self._max_tracked_units:
                    raise RuntimeError("authorization unit ledger exhausted")
            # An explicitly empty unit set is a no-op. Only omission of every
            # selector means global revocation.
            if not targets and unit_id is None and not supplied_unit_ids:
                targets.update(self._current)
                targets.update(self._generation_floor)
            for target in targets:
                current = self._current.pop(target, None)
                current_generation = current.generation if current is not None else -1
                known_generation = self._generation_floor.get(target, -1)
                fence = (
                    generation
                    if generation is not None
                    else max(current_generation, known_generation)
                )
                if fence < 0:
                    # Revoking an entirely unknown unit without an explicit
                    # fence is a no-op, not a way to grow the tracking ledger.
                    continue
                self._generation_floor[target] = max(self._generation_floor.get(target, -1), fence)
                self._revoked_through[target] = max(self._revoked_through.get(target, -1), fence)
                # The inclusive fence rejects this and every earlier
                # generation, making their replay ledgers safely redundant.
                seen = self._seen_cycles.get(target)
                if seen is not None and seen[0] <= fence:
                    self._seen_cycles.pop(target, None)
