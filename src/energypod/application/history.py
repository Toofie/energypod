"""The telemetry historian (DESIGN_PLANT_HISTORY sections 2.1-2.2).

One application component composes into the fleet cycle exactly like the
energy accountant: one bounded, fully suppressed tick per fleet cycle, AFTER
the polls and the energy-accountant step and BEFORE the kernel tick.  It is
observability only -- no control path reads it, nothing restores from it at
boot, it writes no audit facts, and it publishes no bus events (samples are
projections, not acts).

The tick samples the observation stream's LATEST per-unit observation at the
configured cadence into the durable history repository:

- cadence: a unit is sampled when ``sample_interval_s`` has elapsed since
  that unit's last RECORDED row (a per-unit clock seeded at boot so the
  first tick samples immediately; a failed append does not advance it, so
  the next tick is the retry -- the worst honest outcome is a gap of one
  interval);
- the shared tick wall clock: ``sampled_at`` is the tick's own wall time
  truncated to whole seconds, ONE timestamp for every unit sampled in that
  tick, so per-timestamp fleet summation stays exact;
- the staleness guard: a unit's LATEST observation is sampled only when it
  is no older than ``max(3 x control_period_s, sample_interval_s)`` -- a
  frozen latest under a fresh timestamp would fabricate continuity, so a
  stale latest contributes NO row (an honest gap, exactly like the
  scorecard's excluded windows);
- the row: the 15 numeric observables with the ``_telemetry_summary``
  derivations (never zero-filled), the lifecycle word, the recovery
  monitor's ``health_state``, the four mode words, the quality rollup (the
  worst per-field quality over the CONTROL-RATE fields only), and the
  commanded triple (the per-unit winner set + the cycle's peeked
  authority).

The historian also owns the maintenance cadence (DESIGN section 2.4): one
``maintain`` pass on the first tick after each site-local midnight, beside
the boot pass the composition root runs after the store opens.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.history import (
    TelemetrySampleRow,
    format_history_timestamp,
    worst_quality,
)
from energypod.domain.intents import IntentSource
from energypod.domain.observations import DataQuality, Observation

# The staleness bound's control-period multiple (DESIGN section 2.1): a
# module constant, deliberately not a config key.
_STALENESS_CONTROL_PERIODS: Final[int] = 3

# The cold-ring advisory fields excluded from the row's quality rollup (the
# B1 demotion): their staleness is documented behavior, not a degraded
# period, and marking every run-mode row "stale" would hide real degradation.
_ROW_QUALITY_EXCLUSIONS: Final[frozenset[str]] = frozenset({"system_soc_pct", "soh_pct"})

_SOURCE_WORDS: Final[dict[IntentSource, str]] = {
    IntentSource.MANUAL: "manual",
    IntentSource.AGENT: "agent",
    IntentSource.SCHEDULE: "schedule",
    IntentSource.OPTIMIZER: "optimizer",
}


class HistoryRepository(Protocol):
    """The persistence port the historian writes through (sync, like the ledger)."""

    def append_samples(self, rows: Sequence[TelemetrySampleRow]) -> None: ...

    def maintain(self, now: datetime) -> Any: ...


class _ObservationsPort(Protocol):
    async def all_latest(self) -> dict[str, Any]: ...


class _IntentsPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...


class _ClockPort(Protocol):
    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class TelemetryHistorian:
    """The fleet-loop sampler: one bounded, suppressed tick per cycle."""

    def __init__(
        self,
        *,
        unit_ids: Sequence[str],
        sample_interval_s: float,
        control_period_s: float,
        clock: _ClockPort,
        history: HistoryRepository,
        observations: _ObservationsPort,
        timezone: str = "UTC",
        intents: _IntentsPort | None = None,
        health_states: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
        adviser_claims: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        for name, value in (("sample_interval_s", sample_interval_s),):
            if not isinstance(value, int | float) or not 0 < float(value) <= 3600:
                raise ValueError(f"{name} must be a positive number up to 3600 seconds")
        if not isinstance(control_period_s, int | float) or float(control_period_s) <= 0:
            raise ValueError("control_period_s must be a positive number")
        for name, method in (("clock", ("wall_now", "monotonic")),):
            if not all(callable(getattr(clock, item, None)) for item in method):
                raise TypeError(f"{name} must provide wall_now() and monotonic()")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        self._unit_ids = units
        self._sample_interval_s = float(sample_interval_s)
        self._clock = clock
        self._history = history
        self._observations = observations
        self._zone = zone
        self._intents = intents
        self._health_states = health_states
        self._adviser_claims = adviser_claims
        # The staleness guard's bound (DESIGN section 2.1).
        self._max_latest_age_s = max(
            _STALENESS_CONTROL_PERIODS * float(control_period_s), self._sample_interval_s
        )
        # Per-unit last-RECORDED clocks: None = the boot baseline, due
        # immediately, so the restart gap is bounded by downtime plus one
        # interval.
        self._last_sample_mono: dict[str, float | None] = dict.fromkeys(units)
        self._last_local_date: date | None = None

    # --- the fleet-loop tick --------------------------------------------------

    async def tick(
        self,
        *,
        authorized: Mapping[str, tuple[int, str | None] | None] | None = None,
    ) -> None:
        """One suppressed sampling tick; never raises into the fleet loop."""
        try:
            await self._tick(authorized)
        except Exception as error:
            # Survived but never invisible (the suppressed-heartbeat
            # precedent): one process log line, nothing else.
            print(f"SUPERVISED HISTORY TICK FAILURE: {error!r}", flush=True)

    async def _tick(self, authorized: Mapping[str, tuple[int, str | None] | None] | None) -> None:
        wall = self._clock.wall_now()
        self._maintain_after_midnight(wall)
        now_mono = float(self._clock.monotonic())
        due = [
            unit_id
            for unit_id in self._unit_ids
            if self._last_sample_mono[unit_id] is None
            or now_mono - (self._last_sample_mono[unit_id] or 0.0) >= self._sample_interval_s
        ]
        if not due:
            return
        latest: Mapping[str, Any] = {}
        with contextlib.suppress(Exception):
            latest = await self._observations.all_latest()
        sampled_at = wall.astimezone(UTC).replace(microsecond=0)
        candidates: list[tuple[str, TelemetrySampleRow]] = []
        for unit_id in due:
            row = self._row_for_latest(unit_id, latest.get(unit_id), now_mono, sampled_at)
            if row is not None:
                candidates.append((unit_id, row))
        if not candidates:
            return
        # The optional ports are read only when at least one due unit has
        # fresh evidence: an all-stale tick costs nothing beyond the one
        # observations read.
        health = await self._read_suppressed(self._read_health_states)
        winners = await self._read_suppressed(lambda: self._winner_set(now_mono))
        rows = [
            self._finalize(row, unit_id, winners.get(unit_id), authorized, health)
            for unit_id, row in candidates
        ]
        self._history.append_samples(rows)
        # Advance only the units whose rows landed: a failed append leaves
        # every clock where it was, so the next tick is the retry.
        for unit_id, _row in candidates:
            self._last_sample_mono[unit_id] = now_mono

    # --- maintenance cadence ----------------------------------------------------

    def _maintain_after_midnight(self, wall: datetime) -> None:
        """One ``maintain`` pass on the first tick after each site midnight."""
        local_date = wall.astimezone(self._zone).date()
        if self._last_local_date is None:
            self._last_local_date = local_date
            return
        if local_date <= self._last_local_date:
            return
        self._last_local_date = local_date
        with contextlib.suppress(Exception):
            self._history.maintain(wall.astimezone(UTC))

    # --- projection -------------------------------------------------------------

    def _row_for_latest(
        self, unit_id: str, observation: Any, now_mono: float, sampled_at: datetime
    ) -> TelemetrySampleRow | None:
        """One unit's row, or None: no observation or a stale latest."""
        if observation is None:
            return None
        captured = getattr(observation, "captured_at_mono", None)
        if not isinstance(captured, int | float) or isinstance(captured, bool):
            return None
        if now_mono - float(captured) > self._max_latest_age_s:
            return None
        cells = _numeric_tuple(getattr(observation, "cell_voltages_v", None))
        temperatures = _numeric_tuple(getattr(observation, "temperatures_c", None))
        cell_min = min(cells, default=None)
        cell_max = max(cells, default=None)
        spread_v = None if cell_min is None or cell_max is None else cell_max - cell_min
        lifecycle = getattr(observation, "lifecycle", None)
        return TelemetrySampleRow(
            unit_id=unit_id,
            sampled_at=sampled_at,
            system_soc_pct=_optional(getattr(observation, "system_soc_pct", None)),
            bms_soc_pct=_optional(getattr(observation, "bms_soc_pct", None)),
            soh_pct=_optional(getattr(observation, "soh_pct", None)),
            battery_watts=_optional(getattr(observation, "battery_watts", None)),
            grid_power_w=_optional(getattr(observation, "grid_power_w", None)),
            load_power_w=_optional(getattr(observation, "load_power_w", None)),
            pack_voltage_v=_optional(getattr(observation, "pack_voltage_v", None)),
            pack_current_a=_optional(getattr(observation, "pack_current_a", None)),
            cell_min_v=cell_min,
            cell_max_v=cell_max,
            cell_spread_mv=None if spread_v is None else spread_v * 1000.0,
            temperature_min_c=min(temperatures, default=None),
            temperature_max_c=max(temperatures, default=None),
            dynamic_charge_limit_w=_optional(getattr(observation, "dynamic_charge_limit_w", None)),
            dynamic_discharge_limit_w=_optional(
                getattr(observation, "dynamic_discharge_limit_w", None)
            ),
            lifecycle=str(getattr(lifecycle, "value", lifecycle)),
            health_state=None,
            quality=self._quality_rollup(observation).value,
            debug_mode_w=_optional_word(getattr(observation, "debug_mode_w", None)),
            ctrl_mode_w=_optional_word(getattr(observation, "ctrl_mode_w", None)),
            work_mode_w=_optional_word(getattr(observation, "work_mode_w", None)),
            run_mode_w=_optional_word(getattr(observation, "run_mode_w", None)),
        )

    @staticmethod
    def _quality_rollup(observation: Any) -> DataQuality:
        """The worst per-field quality over the row's CONTROL-RATE fields.

        Exactly ``REQUIRED_SAFETY_QUALITY_FIELDS`` minus the cold-ring
        exclusions, plus the CT pair when the composed plan serves it (the
        quality map carries the keys the producer emits), with the pinned
        precedence missing > bad > stale > suspect > good.
        """
        quality = getattr(observation, "quality", None)
        if not isinstance(quality, Mapping):
            return DataQuality.MISSING
        keys = set(quality)
        fields = (Observation.REQUIRED_SAFETY_QUALITY_FIELDS - _ROW_QUALITY_EXCLUSIONS) & keys
        fields |= Observation.CT_QUALITY_FIELDS & keys
        if not fields:
            return DataQuality.MISSING
        return worst_quality(quality[field] for field in sorted(fields))

    def _finalize(
        self,
        row: TelemetrySampleRow,
        unit_id: str,
        winner: tuple[str, str] | None,
        authorized: Mapping[str, tuple[int, str | None] | None] | None,
        health: Mapping[str, Any],
    ) -> TelemetrySampleRow:
        """Attach health_state and the commanded triple to one candidate row.

        No winner (or an emergency-stop winner, which fences rather than
        commands) records the null triple -- "we commanded nothing" is itself
        the recorded fact.  A winner whose unit holds no live capability yet
        records the source and direction with NULL watts: authority is minted
        by the kernel tick, never assumed.
        """
        source, direction = winner if winner is not None else (None, None)
        if source is None or direction is None:
            return replace(row, health_state=self._health_state(health, unit_id))
        peek = None if authorized is None else authorized.get(unit_id)
        watts = None if peek is None else int(peek[0])
        return replace(
            row,
            health_state=self._health_state(health, unit_id),
            commanded_source=source,
            commanded_direction=direction,
            commanded_w=watts,
        )

    @staticmethod
    def _health_state(health: Mapping[str, Any], unit_id: str) -> str | None:
        view = health.get(unit_id)
        state = getattr(view, "state", None)
        return None if state is None else str(getattr(state, "value", state))

    @staticmethod
    async def _read_suppressed(
        read: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        """One optional-port read: a failure contributes nothing, never a crash."""
        with contextlib.suppress(Exception):
            return await read()
        return {}

    async def _read_health_states(self) -> Mapping[str, Any]:
        if self._health_states is None:
            return {}
        return await self._health_states()

    def _read_adviser_claims(self) -> Mapping[str, str]:
        if self._adviser_claims is None:
            return {}
        return self._adviser_claims()

    async def _winner_set(self, now_mono: float) -> Mapping[str, tuple[str, str]]:
        """The per-unit winner words (the snapshot intent view's arbitration).

        A fresh, throwaway arbiter per tick: the historian only ever projects
        the same ranking the kernel's own cycle uses.  An OPTIMIZER winner is
        attributed the claiming adviser's tag when a single-writer projection
        claims the unit, else the bare ``optimizer``; an EMERGENCY_STOP
        winner is deliberately absent (the fence commands nothing).
        """
        if self._intents is None:
            return {}
        from energypod.application.arbiter import IntentArbiter

        active = await self._intents.active(now_mono)
        selection = IntentArbiter().arbitrate(active, now_mono)
        winners: dict[str, tuple[str, str]] = {}
        for intent in selection.ranked:
            source = getattr(intent, "source", None)
            if source is IntentSource.EMERGENCY_STOP:
                continue
            word = _SOURCE_WORDS.get(source) if isinstance(source, IntentSource) else None
            if word is None:
                continue
            if word == "optimizer":
                claims = self._read_adviser_claims()
                for unit_id in sorted(selection.scopes.get(getattr(intent, "id", ""), ()) or ()):
                    claim = claims.get(unit_id)
                    winners[unit_id] = (
                        claim if claim in ("excess_adviser", "night_adviser") else "optimizer",
                        _direction_word(getattr(intent, "direction", None)),
                    )
                continue
            for unit_id in sorted(selection.scopes.get(getattr(intent, "id", ""), ()) or ()):
                winners[unit_id] = (word, _direction_word(getattr(intent, "direction", None)))
        return winners


class PlantHistoryRefusal(Exception):
    """The history surface refused a read (the block-presence doctrine).

    Mirrors the schedule and scorecard refusal types: the REST boundary maps
    it to 409 with the pinned code, so the shape stays one per feature.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        if not code or code != code.strip():
            raise ValueError("refusal code must be non-empty and normalized")
        self.code = code
        self.message = message


PLANT_HISTORY_NOT_COMMISSIONED = "plant_history_not_commissioned"


class PlantHistoryQueryRepository(Protocol):
    """The read port the query surface projects through."""

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[TelemetrySampleRow, ...]: ...

    def rollup_hours(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]: ...

    def oldest_full_res_at(self) -> datetime | None: ...

    def last_sample_at(self, unit_ids: Sequence[str]) -> dict[str, datetime | None]: ...


class PlantHistoryControl:
    """The facade-facing history surface (H3: the snapshot's ``history_state``).

    Composed exactly when the ``plant_history`` block is PRESENT; the query
    engine (DESIGN section 3) joins in H4 through the same control.
    """

    def __init__(
        self,
        *,
        unit_ids: Sequence[str],
        sample_interval_s: float,
        retention_full_resolution_days: int,
        repository: PlantHistoryQueryRepository,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        self._unit_ids = units
        self._sample_interval_s = float(sample_interval_s)
        self._retention_full_resolution_days = int(retention_full_resolution_days)
        self._repository = repository

    def state_payload(self) -> dict[str, Any]:
        """The snapshot's feature-detected ``history_state`` projection."""
        latest = self._repository.last_sample_at(self._unit_ids)
        return {
            "sample_interval_s": self._sample_interval_s,
            "retention_full_resolution_days": self._retention_full_resolution_days,
            "last_sample_at": {
                unit: (
                    None if latest.get(unit) is None else format_history_timestamp(latest[unit])  # type: ignore[arg-type]
                )
                for unit in self._unit_ids
            },
        }


def _direction_word(direction: Any) -> str:
    return str(getattr(direction, "value", direction))


def _numeric_tuple(raw: Any) -> tuple[float, ...]:
    """Numeric view of a cell/temperature array; empty arrays project ()."""
    if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
        return ()
    return tuple(
        float(item) for item in raw if isinstance(item, int | float) and not isinstance(item, bool)
    )


def _optional(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def _optional_word(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    word: int | None = int(raw)
    return word
