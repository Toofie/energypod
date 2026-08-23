"""Bounded process-local repositories for observations, capabilities, and intents."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from energypod.domain import IntentSource
from energypod.domain.authorization import AuthorizationBatch, StaleGenerationError
from energypod.domain.energy import EnergyDayRecord, EnergyUnitBaseline
from energypod.domain.history import (
    HISTORY_NUMERIC_FIELDS,
    MaintenanceResult,
    TelemetryRollupHour,
    TelemetrySampleRow,
    format_history_timestamp,
    hour_start_of,
    parse_history_timestamp,
)
from energypod.domain.history import worst_quality as worst_of


class InMemoryEnergyLedgerRepository:
    """Process-local energy ledger: day records keyed by date + the baseline.

    DESIGN_ENERGY_SCORECARD section 5: ``record_day`` is idempotent by date
    (the first record for a site-day stands -- the accountant finalizes a day
    exactly once), ``latest_days`` returns newest-first within the caller's
    bound, and the durable live-day baseline round-trips per unit.
    """

    def __init__(self) -> None:
        self._days: dict[Any, EnergyDayRecord] = {}
        self._baseline: dict[str, EnergyUnitBaseline] = {}
        self._lock = RLock()

    def record_day(self, record: EnergyDayRecord) -> None:
        if type(record) is not EnergyDayRecord:
            raise TypeError("record must be an EnergyDayRecord")
        with self._lock:
            self._days.setdefault(record.date, record)

    def get_day(self, day: Any) -> EnergyDayRecord | None:
        self._require_date(day)
        with self._lock:
            return self._days.get(day)

    def latest_days(self, limit: int) -> tuple[EnergyDayRecord, ...]:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            days = sorted(self._days, reverse=True)[:limit]
            return tuple(self._days[day] for day in days)

    def load_baseline(self) -> dict[str, EnergyUnitBaseline]:
        with self._lock:
            return dict(self._baseline)

    def save_baseline(self, baselines: Any) -> None:
        if not isinstance(baselines, dict):
            raise TypeError("baselines must map unit ids to EnergyUnitBaseline values")
        if any(type(value) is not EnergyUnitBaseline for value in baselines.values()):
            raise TypeError("baselines must map unit ids to EnergyUnitBaseline values")
        with self._lock:
            self._baseline = dict(baselines)

    @staticmethod
    def _require_date(day: object) -> None:
        from datetime import date as _date

        if type(day) is not _date:
            raise TypeError("day must be a civil date")


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
        clock: Any | None = None,
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
        # Injected monotonic clock for the non-consuming projection read.  All
        # timing stays injected; this repository never reads ambient time.
        self._clock = clock
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

    def peek(self, unit_id: str) -> Any | None:
        """Non-consuming projection read for snapshot views; control uses current().

        Returns the currently valid capability -- not-before satisfied and
        unexpired -- or None.  Without an injected clock a capability's
        validity window cannot be established, so the projection fails closed
        rather than showing a possibly expired capability as live authority.
        """
        if not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip():
            raise ValueError("unit_id must be non-empty and normalized")
        if self._clock is None:
            return None
        with self._lock:
            authorization = self._current.get(unit_id)
            if authorization is None:
                return None
            now = self._clock.monotonic()
            if now < authorization.not_before_mono:
                return None
            if authorization.expires_at_mono <= now:
                return None
            return authorization

    def revoked_through(self, unit_ids: Iterable[str]) -> int:
        """The highest generation permanently fenced for these units.

        Returns ``-1`` when no member has ever been fenced.  This is the
        reconciliation input for the authority-generation coordinator: a
        mint at or below this value will be fenced by ``publish``, so the
        epoch the kernel consults must stand strictly beyond it (2026-08-23
        publish-fence generation desync).
        """
        targets = set(unit_ids)
        if any(
            not isinstance(target, str) or not target or target != target.strip()
            for target in targets
        ):
            raise ValueError("unit_ids must contain normalized identifiers")
        with self._lock:
            return max((self._revoked_through.get(target, -1) for target in targets), default=-1)

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


class InMemoryIntentRepository:
    """Process-local intent store with latched emergency stops.

    ``active`` filters by the monotonic acceptance window, except that an
    emergency-stop intent stays active until it is explicitly removed by the
    scoped exact-id acknowledgement; it never expires by TTL.
    """

    def __init__(self, *, max_stored_intents: int | None = None) -> None:
        if max_stored_intents is not None and (
            type(max_stored_intents) is not int or max_stored_intents <= 0
        ):
            raise ValueError("max_stored_intents must be positive when provided")
        self._intents: dict[str, Any] = {}
        self._max_stored_intents = max_stored_intents
        self._lock = RLock()

    def add(self, intent: Any) -> None:
        intent_id = getattr(intent, "id", None)
        if not isinstance(intent_id, str) or not intent_id or intent_id != intent_id.strip():
            raise ValueError("intent id must be non-empty and normalized")
        with self._lock:
            if intent_id in self._intents:
                raise ValueError("intent id is already stored")
            if (
                self._max_stored_intents is not None
                and len(self._intents) >= self._max_stored_intents
            ):
                # Never evict: a capacity failure is loud, a silently dropped
                # latched stop would be a safety regression.
                raise RuntimeError("intent store capacity exhausted")
            self._intents[intent_id] = intent

    def active(self, now_mono: float) -> tuple[Any, ...]:
        if (
            isinstance(now_mono, bool)
            or not isinstance(now_mono, int | float)
            or not math.isfinite(now_mono)
        ):
            raise ValueError("now_mono must be finite")
        with self._lock:
            return tuple(
                intent for intent in self._intents.values() if self._is_active(intent, now_mono)
            )

    def remove(self, intent_id: str) -> None:
        if not isinstance(intent_id, str) or not intent_id or intent_id != intent_id.strip():
            raise ValueError("intent id must be non-empty and normalized")
        with self._lock:
            if intent_id not in self._intents:
                raise LookupError(intent_id)
            del self._intents[intent_id]

    def _is_active(self, intent: Any, now_mono: float) -> bool:
        accepted_at = getattr(intent, "accepted_at_mono", None)
        if isinstance(accepted_at, bool) or not isinstance(accepted_at, int | float):
            # An intent without a sound acceptance time can never authorize.
            return False
        if accepted_at > now_mono:
            return False
        if self._is_emergency_stop(intent):
            return True
        duration = getattr(intent, "duration_s", None)
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            return False
        return now_mono < accepted_at + duration

    @staticmethod
    def _is_emergency_stop(intent: Any) -> bool:
        # Duck-typed so structural fakes may carry either the enum or its value.
        source = getattr(intent, "source", None)
        return getattr(source, "value", source) == IntentSource.EMERGENCY_STOP.value


class InMemoryTelemetryHistoryRepository:
    """Process-local telemetry historian (DESIGN_PLANT_HISTORY sections 2.2-2.4).

    The simulate-mode and database-less deployment twin of the durable store:
    explicitly non-durable (rows vanish at restart), with EXACTLY the durable
    contract's semantics -- batch append, inclusive windowed reads, and the
    one-transaction rollup-then-prune maintenance pass with the same
    hour-boundary and first-rollup-stands rules.
    """

    def __init__(
        self,
        *,
        retention_full_resolution_s: float = 14 * 86_400.0,
        retention_rollup_s: float | None = None,
    ) -> None:
        if not isinstance(retention_full_resolution_s, int | float) or (
            not math.isfinite(float(retention_full_resolution_s))
            or retention_full_resolution_s <= 0
        ):
            raise ValueError("retention_full_resolution_s must be a positive finite number")
        if retention_rollup_s is not None and (
            not isinstance(retention_rollup_s, int | float)
            or not math.isfinite(float(retention_rollup_s))
            or retention_rollup_s <= 0
        ):
            raise ValueError("retention_rollup_s must be a positive finite number or None")
        self._retention_full_resolution_s = float(retention_full_resolution_s)
        self._retention_rollup_s = None if retention_rollup_s is None else float(retention_rollup_s)
        # Keyed by the STORED spelling so the fixed-width ISO ordering that
        # makes the durable primary key correct does the same work here.
        self._samples: dict[str, dict[str, TelemetrySampleRow]] = defaultdict(dict)
        self._rollups: dict[tuple[str, str], TelemetryRollupHour] = {}
        self._lock = RLock()

    def append_samples(self, rows: Sequence[TelemetrySampleRow]) -> None:
        if any(type(row) is not TelemetrySampleRow for row in rows):
            raise TypeError("rows must be TelemetrySampleRow values")
        if not rows:
            return
        with self._lock:
            for row in rows:
                # The first sample for a (unit, tick) stands: a retried tick
                # after a suppressed failure is the durable ON CONFLICT rule.
                self._samples[row.unit_id].setdefault(format_history_timestamp(row.sampled_at), row)

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[TelemetrySampleRow, ...]:
        normalized = self._require_units(unit_ids)
        start, end = (
            format_history_timestamp(from_at),
            format_history_timestamp(to_at),
        )
        with self._lock:
            selected: list[TelemetrySampleRow] = []
            # Ascending unit id, ascending time: the durable store's own
            # clustering order, so both adapters answer in one shape.
            for unit_id in sorted(normalized):
                history = self._samples.get(unit_id, {})
                selected.extend(history[key] for key in sorted(history) if start <= key <= end)
            return tuple(selected)

    def rollup_hours(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[TelemetryRollupHour, ...]:
        normalized = self._require_units(unit_ids)
        start = format_history_timestamp(hour_start_of(from_at))
        end = format_history_timestamp(to_at)
        with self._lock:
            selected = [
                rollup
                for (unit_id, hour_key) in sorted(self._rollups)
                if unit_id in normalized and start <= hour_key <= end
                for rollup in (self._rollups[(unit_id, hour_key)],)
            ]
            return tuple(selected)

    def oldest_full_res_at(self) -> datetime | None:
        with self._lock:
            oldest: str | None = None
            for history in self._samples.values():
                for key in history:
                    if oldest is None or key < oldest:
                        oldest = key
            return None if oldest is None else parse_history_timestamp(oldest)

    def last_sample_at(self, unit_ids: Sequence[str]) -> dict[str, datetime | None]:
        normalized = self._require_units(unit_ids)
        latest: dict[str, datetime | None] = {}
        with self._lock:
            for unit_id in normalized:
                history = self._samples.get(unit_id)
                latest[unit_id] = None if not history else parse_history_timestamp(max(history))
        return latest

    def maintain(self, now: datetime) -> MaintenanceResult:
        """Rollup then prune with the durable pass's exact semantics."""
        from energypod.domain.observations import DataQuality

        moment = now if now.tzinfo is UTC else now.astimezone(UTC)
        horizon = hour_start_of(moment - timedelta(seconds=self._retention_full_resolution_s))
        horizon_key = format_history_timestamp(horizon)
        rolled = pruned = pruned_rollups = 0
        with self._lock:
            buckets: dict[tuple[str, str], list[TelemetrySampleRow]] = defaultdict(list)
            for unit_id, history in self._samples.items():
                for key, row in history.items():
                    if key < horizon_key:
                        buckets[
                            (unit_id, format_history_timestamp(hour_start_of(row.sampled_at)))
                        ].append(row)
            for (unit_id, hour_key), bucket in sorted(buckets.items()):
                if (unit_id, hour_key) in self._rollups:
                    continue
                metrics: dict[str, tuple[float | None, float | None, float | None]] = {}
                for field in HISTORY_NUMERIC_FIELDS:
                    values = [
                        value
                        for value in (getattr(row, field) for row in bucket)
                        if value is not None
                    ]
                    metrics[field] = (
                        (min(values), max(values), sum(values) / len(values))
                        if values
                        else (None, None, None)
                    )
                self._rollups[(unit_id, hour_key)] = TelemetryRollupHour(
                    unit_id=unit_id,
                    hour_start=parse_history_timestamp(hour_key),
                    metrics=metrics,
                    sample_count=len(bucket),
                    worst_quality=worst_of(DataQuality(row.quality) for row in bucket).value,
                )
                rolled += 1
            for history in self._samples.values():
                for key in [key for key in history if key < horizon_key]:
                    del history[key]
                    pruned += 1
            if self._retention_rollup_s is not None:
                rollup_horizon = format_history_timestamp(
                    hour_start_of(moment - timedelta(seconds=self._retention_rollup_s))
                )
                stale = [
                    rollup_key for rollup_key in self._rollups if rollup_key[1] < rollup_horizon
                ]
                for rollup_key in stale:
                    del self._rollups[rollup_key]
                    pruned_rollups += 1
        return MaintenanceResult(
            rolled_hours=rolled,
            pruned_samples=pruned,
            pruned_rollups=pruned_rollups,
        )

    @staticmethod
    def _require_units(unit_ids: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(unit_ids)
        if not normalized or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in normalized
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("unit_ids must be unique")
        return normalized
