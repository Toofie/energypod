"""The daily energy accountant (DESIGN_ENERGY_SCORECARD sections 4-5).

One application component composes into the fleet loop when the
``energy_scorecard`` block is PRESENT.  It consumes observations and never
writes any control path:

- zero-order-hold integration of the per-pod CT grid word over observation
  capture times, sign-split import/export (negative = import), gaps above
  ``integration_max_gap_s`` excluded -- never interpolated -- with a
  per-unit/day coverage fraction and worst-unit fleet rollup;
- device-counter daily deltas for the role-confirmed charge/discharge/load
  pairs and BOTH neutral grid pairs (the A-1 passive cross-check evidence),
  where a decreasing cumulative is a reset, never a negative delta;
- day rollover at site-timezone midnight on the first observation whose
  local date advances, finalizing + persisting + publishing the completed
  day and requesting ONE promoted cold-ring read of the energy block (the
  B4 cell-refresh precedent) so the new day's counter baseline is at most
  one control period old;
- surplus attribution: the integral of max(0, -measured battery watts) over
  ticks where the excess adviser is active and targets the unit -- measured
  watts, never authorized watts.

The accountant READS the adviser projection (never writes it): the fleet
loop passes the tick's active targets in.  All persistence, bus, and audit
sinks are injected ports; a missing port means the corresponding side effect
is simply absent (tests run the core with none of them).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.audit import AuditEvent
from energypod.domain.energy import (
    BATTERY_SOURCE,
    GRID_ROLE_SETTINGS,
    GRID_ROLES_SWAPPED,
    GRID_ROLES_UNPINNED,
    GRID_SOURCE_DEVICE_COUNTER,
    GRID_SOURCE_INTEGRATED,
    GRID_SOURCES,
    KIND_IN_PROGRESS,
    LOAD_SOURCE,
    SURPLUS_SOURCE,
    VERDICT_SWAPPED,
    VERDICT_UNDISCRIMINATING,
    VERDICT_VENDOR_LABELS,
    CounterCrossCheck,
    EnergyDayRecord,
    EnergyUnitBaseline,
    FleetEnergyDay,
    UnitEnergyDay,
    counter_reset_flag,
)
from energypod.domain.observations import DataQuality, UnitLifecycle

_WATT_SECONDS_PER_KWH: Final[float] = 3_600_000.0
# DESIGN section 3, the pinning test: a day discriminates only with both
# integrated sides >= 0.5 kWh, and an ordering matches when each pair sits
# within max(0.5 kWh, 5 %) of its integrated figure -- for EXACTLY one of
# the two orderings.
_A1_MIN_SIDE_KWH: Final[float] = 0.5
_A1_ABS_TOLERANCE_KWH: Final[float] = 0.5
_A1_RELATIVE_TOLERANCE: Final[float] = 0.05

DAY_ROLLED_EVENT: Final[str] = "energy.day_rolled"
DAY_RECORDED_AUDIT: Final[str] = "energy_day_recorded"
COUNTER_RESET_AUDIT: Final[str] = "energy_counter_reset_observed"

_ENERGY_PRINCIPAL: Final[str] = "energypod:energy-accountant"
_ENERGY_POLICY_VERSION: Final[str] = "energy-scorecard-1"

_COUNTER_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("grid_a", "energy_grid_a_kwh"),
    ("grid_b", "energy_grid_b_kwh"),
    ("load", "energy_load_kwh"),
    ("charge", "energy_charge_kwh"),
    ("discharge", "energy_discharge_kwh"),
)


class EnergyAccountingError(ValueError):
    """The accounting settings or wiring are malformed."""


@dataclass(frozen=True, slots=True)
class EnergyAccountingSettings:
    """The commissioned scorecard knobs (DESIGN section 7).

    The application-level bounds are the arithmetic ones (finite, positive
    gap; a coverage threshold inside (0, 100]); the CONFIGURATION layer adds
    the relations that need the timing block (gap > control period, <= 60 s)
    and the durable-pin gate on ``grid_counter_roles``.  The structural A-1
    gate lives HERE because it needs no external fact: the unpinned pair
    must never become the display source.
    """

    grid_source: str = GRID_SOURCE_INTEGRATED
    grid_counter_roles: str = GRID_ROLES_UNPINNED
    integration_max_gap_s: float = 10.0
    min_day_coverage_pct: float = 95.0

    def __post_init__(self) -> None:
        if self.grid_source not in GRID_SOURCES:
            raise EnergyAccountingError(
                f"grid_source must be one of {sorted(GRID_SOURCES)}, not {self.grid_source!r}"
            )
        if self.grid_counter_roles not in GRID_ROLE_SETTINGS:
            raise EnergyAccountingError(
                "grid_counter_roles must be one of "
                f"{sorted(GRID_ROLE_SETTINGS)}, not {self.grid_counter_roles!r}"
            )
        if (
            isinstance(self.integration_max_gap_s, bool)
            or not isinstance(self.integration_max_gap_s, int | float)
            or not math.isfinite(self.integration_max_gap_s)
            or self.integration_max_gap_s <= 0
        ):
            raise EnergyAccountingError("integration_max_gap_s must be finite and positive")
        if (
            isinstance(self.min_day_coverage_pct, bool)
            or not isinstance(self.min_day_coverage_pct, int | float)
            or not math.isfinite(self.min_day_coverage_pct)
            or not 0 < self.min_day_coverage_pct <= 100
        ):
            raise EnergyAccountingError("min_day_coverage_pct must be inside (0, 100]")
        if (
            self.grid_source is GRID_SOURCE_DEVICE_COUNTER
            and self.grid_counter_roles is GRID_ROLES_UNPINNED
        ):
            raise EnergyAccountingError(
                "grid_source device_counter is refused while grid_counter_roles is "
                "unpinned: the role-open counter pair must never become the display "
                "source (the A-1 gate)"
            )

    def grid_source_label(self) -> str:
        return self.grid_source


class _ClockPort(Protocol):
    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class _BusPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class _AuditPort(Protocol):
    async def append(self, event: Any) -> None: ...


class EnergyLedgerRepository(Protocol):
    """DESIGN section 5: the per-day ledger + durable live-day baseline."""

    def record_day(self, record: EnergyDayRecord) -> None: ...

    def get_day(self, day: date) -> EnergyDayRecord | None: ...

    def latest_days(self, limit: int) -> tuple[EnergyDayRecord, ...]: ...

    def load_baseline(self) -> Mapping[str, EnergyUnitBaseline]: ...

    def save_baseline(self, baselines: Mapping[str, EnergyUnitBaseline]) -> None: ...


@dataclass
class _UnitDayState:
    """One unit's live-day accumulators (mutable by construction)."""

    counter_start: dict[str, float] = field(default_factory=dict)
    counter_last: dict[str, float] = field(default_factory=dict)
    import_watt_seconds: float = 0.0
    export_watt_seconds: float = 0.0
    surplus_watt_seconds: float = 0.0
    sampled_seconds: float = 0.0
    flags: set[str] = field(default_factory=set)
    grid_seen: bool = False
    battery_seen: bool = False
    grid_anchor_mono: float | None = None
    grid_anchor_watts: float | None = None
    battery_anchor_mono: float | None = None
    battery_anchor_watts: float | None = None
    battery_anchor_attribution: bool = False
    day_start_mono: float | None = None
    last_capture_mono: float | None = None
    last_capture_wall: datetime | None = None
    restored_wall: datetime | None = None
    restored_grid_watts: float | None = None


def _fingerprint(facts: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {key: facts[key] for key in sorted(facts)}, sort_keys=True, default=str, allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is UTC else value.astimezone(UTC)


class EnergyAccountant:
    """The advisory daily-energy accountant (single fleet-loop writer)."""

    def __init__(
        self,
        *,
        unit_ids: Iterable[str],
        timezone: str,
        settings: EnergyAccountingSettings,
        clock: _ClockPort,
        ledger: EnergyLedgerRepository | None = None,
        bus: _BusPort | None = None,
        audit: _AuditPort | None = None,
        request_energy_refresh: Callable[[], None] | None = None,
        process_instance_id: str = "energypod-energy",
        process_origin_mono: float = 0.0,
        configuration_version: int = 0,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise EnergyAccountingError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise EnergyAccountingError("unit_ids must be unique")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise EnergyAccountingError("timezone must be an IANA timezone") from exc
        if not isinstance(settings, EnergyAccountingSettings):
            raise EnergyAccountingError("settings must be EnergyAccountingSettings")
        for name in ("wall_now", "monotonic"):
            if not callable(getattr(clock, name, None)):
                raise TypeError("clock must provide wall_now() and monotonic()")
        self._unit_ids = units
        self._zone = zone
        self._settings = settings
        self._clock = clock
        self._ledger = ledger
        self._bus = bus
        self._audit = audit
        self._request_energy_refresh = request_energy_refresh
        self._process_instance_id = process_instance_id
        self._process_origin_mono = float(process_origin_mono)
        self._configuration_version = configuration_version
        self._states: dict[str, _UnitDayState] = {unit: _UnitDayState() for unit in units}
        self._live_date: date | None = None
        self._live_offset_minutes: int | None = None
        self._pending_days: list[EnergyDayRecord] = []
        self._pending_resets: list[tuple[str, str]] = []
        self._dirty = False
        if ledger is not None:
            self._restore_baseline(ledger.load_baseline())

    # --- the observation path ------------------------------------------------

    @property
    def live_day(self) -> date | None:
        return self._live_date

    def observe(
        self,
        observation: Any,
        *,
        adviser_active_targets: frozenset[str],
        now_mono: float,
    ) -> None:
        """Fold one observation into the live day's accumulators.

        Day rollover happens HERE, on the first observation whose local date
        advances: the completed day is finalized into the pending queue (the
        ``flush`` port-call persists, publishes, audits, and requests the one
        promoted energy read).  Observations for unknown units or dates that
        already rolled are ignored -- a straggler from the published day is
        at most one cycle of slop and the day is already durable.
        """
        unit_id = getattr(observation, "unit_id", None)
        if not isinstance(unit_id, str) or unit_id not in self._states:
            return
        wall = getattr(observation, "wall_timestamp", None)
        captured = getattr(observation, "captured_at_mono", None)
        if not isinstance(wall, datetime) or wall.tzinfo is None:
            return
        if captured is None or not math.isfinite(float(captured)):
            return
        local = wall.astimezone(self._zone)
        local_date = local.date()
        if self._live_date is None:
            self._start_day(local_date)
        elif local_date > self._live_date:
            self._pending_days.append(self._finalize(kind=None))
            self._start_day(local_date)
        elif local_date < self._live_date:
            return
        # Fetched AFTER the day lifecycle: a rollover replaced the states
        # with fresh ones, and this observation belongs to the NEW day.
        state = self._states[unit_id]
        if state.last_capture_mono is not None and float(captured) <= state.last_capture_mono:
            return
        self._account(unit_id, state, observation, local, adviser_active_targets)
        state.last_capture_mono = float(captured)
        state.last_capture_wall = _as_utc(wall)
        self._dirty = True

    async def tick(
        self,
        latest: Mapping[str, Any],
        *,
        adviser_active_targets: frozenset[str],
    ) -> None:
        """One fleet-loop tick: observe the latest observations, then flush."""
        now_mono = float(self._clock.monotonic())
        for unit_id in self._unit_ids:
            observation = latest.get(unit_id) if isinstance(latest, Mapping) else None
            if observation is None:
                continue
            self.observe(
                observation, adviser_active_targets=adviser_active_targets, now_mono=now_mono
            )
        await self.flush()

    async def flush(self) -> None:
        """Persist, publish, and audit every completed day and reset fact.

        The durable ledger record comes FIRST (it is the artifact); a ledger
        failure leaves the day pending so the next tick retries, while bus
        and audit failures are suppressed -- observability never blocks the
        accountant (the composition layer bounds and suppresses the tick as
        a whole besides).  Exactly ONE promoted energy read is requested per
        completed day.
        """
        for record in self._pending_days:
            if self._ledger is not None:
                self._ledger.record_day(record)
            with contextlib.suppress(Exception):
                if self._bus is not None:
                    await self._bus.publish({"type": DAY_ROLLED_EVENT, "payload": record.payload()})
            with contextlib.suppress(Exception):
                if self._audit is not None:
                    await self._audit.append(self._day_recorded_event(record))
            if self._request_energy_refresh is not None:
                with contextlib.suppress(Exception):
                    self._request_energy_refresh()
        self._pending_days.clear()
        for unit_id, metric in self._pending_resets:
            with contextlib.suppress(Exception):
                if self._audit is not None:
                    await self._audit.append(self._reset_event(unit_id, metric))
        self._pending_resets.clear()
        if self._ledger is not None and self._dirty:
            with contextlib.suppress(Exception):
                self._ledger.save_baseline(self._baselines())
            self._dirty = False

    # --- the queries ---------------------------------------------------------

    def day_summary(self, local_date: date) -> EnergyDayRecord | None:
        """The record for one local date: the live day, else the ledger's."""
        if self._live_date == local_date:
            return self._finalize(kind=KIND_IN_PROGRESS)
        if self._ledger is not None:
            return self._ledger.get_day(local_date)
        return None

    def today_summary(self) -> EnergyDayRecord:
        """The in-progress record for TODAY (snapshot ``energy_today``).

        Before any observation this is an honest all-null record: the block
        is composed, the figures are not yet known, and nothing is
        zero-filled.
        """
        wall = self._clock.wall_now()
        if not isinstance(wall, datetime) or wall.tzinfo is None:
            raise EnergyAccountingError("clock wall time must be timezone-aware")
        local_date = wall.astimezone(self._zone).date()
        if self._live_date == local_date:
            return self._finalize(kind=KIND_IN_PROGRESS)
        if self._live_date is None or self._live_date < local_date:
            offset = self._midnight_offset_minutes(local_date)
            return EnergyDayRecord(
                date=local_date,
                timezone=str(self._zone),
                utc_offset_minutes=offset,
                kind=KIND_IN_PROGRESS,
                units={
                    unit: UnitEnergyDay(
                        grid_import_kwh=None,
                        grid_export_kwh=None,
                        battery_charged_kwh=None,
                        battery_discharged_kwh=None,
                        load_kwh=None,
                        charged_from_surplus_kwh=None,
                        coverage_pct=None,
                    )
                    for unit in self._unit_ids
                },
                fleet=FleetEnergyDay(
                    grid_import_kwh=None,
                    grid_export_kwh=None,
                    battery_charged_kwh=None,
                    battery_discharged_kwh=None,
                    load_kwh=None,
                    charged_from_surplus_kwh=None,
                    coverage_pct=None,
                ),
                sources=self._sources(),
                counter_cross_check=None,
                solar_production_measured=False,
            )
        return self._finalize(kind=KIND_IN_PROGRESS)

    def grid_counter_roles(self) -> str:
        return self._settings.grid_counter_roles

    # --- internals: day lifecycle --------------------------------------------

    def _start_day(self, local_date: date) -> None:
        self._live_date = local_date
        self._live_offset_minutes = self._midnight_offset_minutes(local_date)
        self._states = {unit: _UnitDayState() for unit in self._unit_ids}

    def _midnight_offset_minutes(self, local_date: date) -> int:
        midnight = datetime.combine(local_date, time(0, 0), tzinfo=self._zone)
        offset = midnight.utcoffset() or timedelta(0)
        return int(offset.total_seconds() // 60)

    def _sources(self) -> dict[str, str]:
        return {
            "grid": self._settings.grid_source_label(),
            "battery": BATTERY_SOURCE,
            "load": LOAD_SOURCE,
            "surplus": SURPLUS_SOURCE,
        }

    def _account(
        self,
        unit_id: str,
        state: _UnitDayState,
        observation: Any,
        local: datetime,
        adviser_active_targets: frozenset[str],
    ) -> None:
        captured = float(observation.captured_at_mono)
        midnight = datetime.combine(local.date(), time(0, 0), tzinfo=self._zone)
        seconds_since_midnight = (local - midnight).total_seconds()
        if state.day_start_mono is None:
            state.day_start_mono = captured - seconds_since_midnight

        # The restart bridge: a baseline restored from the ledger carries the
        # last capture WALL time (monotonic time never crosses processes).
        # Project that anchor into this process's monotonic domain so the
        # ordinary gap rule decides -- a sub-gap restart integrates, a longer
        # one is a gap.  The attribution predicate is NOT restored: whether
        # the adviser was active before the restart is unknown here.
        if state.restored_wall is not None:
            restored_gap = (observation.wall_timestamp - state.restored_wall).total_seconds()
            if state.restored_grid_watts is not None and restored_gap > 0:
                state.grid_anchor_mono = captured - restored_gap
                state.grid_anchor_watts = state.restored_grid_watts
            state.restored_wall = None
            state.restored_grid_watts = None

        grid_watts = observation.grid_power_w
        grid_good = (
            grid_watts is not None and observation.quality.get("grid_power_w") is DataQuality.GOOD
        )
        max_gap = float(self._settings.integration_max_gap_s)
        anchor_mono = state.grid_anchor_mono
        anchor_watts = state.grid_anchor_watts
        if anchor_mono is not None and anchor_watts is not None:
            dt = captured - anchor_mono
            if 0 < dt <= max_gap:
                # Zero-order hold over the observation's own capture times:
                # the PREVIOUS good sample's power covers the interval up to
                # this one -- including an interval that ends on a word this
                # poll could not decode (the anchor was measured good; only
                # the NEXT interval loses its anchor).  A spacing above the
                # commissioned gap is a GAP: zero energy, zero coverage,
                # never an interpolation.
                if anchor_watts < 0:
                    state.import_watt_seconds += -anchor_watts * dt
                elif anchor_watts > 0:
                    state.export_watt_seconds += anchor_watts * dt
                state.sampled_seconds += dt
        if grid_good:
            state.grid_seen = True
            state.grid_anchor_mono = captured
            state.grid_anchor_watts = float(grid_watts)
        else:
            state.grid_anchor_mono = None
            state.grid_anchor_watts = None

        battery_watts = observation.battery_watts
        battery_good = (
            battery_watts is not None
            and observation.quality.get("battery_watts") is DataQuality.GOOD
        )
        if battery_good:
            state.battery_seen = True
            anchor_mono = state.battery_anchor_mono
            anchor_watts = state.battery_anchor_watts
            if anchor_mono is not None and anchor_watts is not None:
                dt = captured - anchor_mono
                if 0 < dt <= max_gap and state.battery_anchor_attribution and anchor_watts < 0:
                    # Measured watts over adviser-active ticks (the anchor
                    # tick's predicate governs the interval it starts, exactly
                    # like the held power does).
                    state.surplus_watt_seconds += -anchor_watts * dt
            state.battery_anchor_mono = captured
            state.battery_anchor_watts = float(battery_watts)
            state.battery_anchor_attribution = unit_id in adviser_active_targets
        else:
            state.battery_anchor_mono = None
            state.battery_anchor_watts = None
            state.battery_anchor_attribution = False

        for metric, field_name in _COUNTER_METRICS:
            value = getattr(observation, field_name, None)
            if value is None or observation.quality.get(field_name) is not DataQuality.GOOD:
                continue
            number = float(value)
            if metric not in state.counter_start:
                state.counter_start[metric] = number
            previous = state.counter_last.get(metric)
            if previous is not None and number < previous:
                # A DECREASING cumulative is a reset, not a negative delta:
                # the unit-metric-day re-baselines from the new (lower)
                # figure and carries the flag plus the audited fact.
                state.flags.add(counter_reset_flag(metric))
                self._pending_resets.append((unit_id, metric))
                state.counter_start[metric] = number
            state.counter_last[metric] = number

    # --- internals: projection -----------------------------------------------

    def _unit_metrics(self, state: _UnitDayState) -> UnitEnergyDay:
        def kwh(watt_seconds: float) -> float:
            return watt_seconds / _WATT_SECONDS_PER_KWH

        coverage: float | None = None
        if state.grid_seen and state.day_start_mono is not None and state.last_capture_mono:
            elapsed = state.last_capture_mono - state.day_start_mono
            if elapsed > 0:
                coverage = min(100.0, max(0.0, state.sampled_seconds / elapsed * 100.0))

        if self._settings.grid_source is GRID_SOURCE_DEVICE_COUNTER:
            roles = self._settings.grid_counter_roles
            pair_a = self._counter_delta(state, "grid_a")
            pair_b = self._counter_delta(state, "grid_b")
            if roles is GRID_ROLES_SWAPPED:
                imported, exported = pair_b, pair_a
            else:  # vendor_labels (unpinned is refused structurally)
                imported, exported = pair_a, pair_b
        else:
            # The default: OUR OWN integration of the control-grade CT word.
            imported = kwh(state.import_watt_seconds) if state.grid_seen else None
            exported = kwh(state.export_watt_seconds) if state.grid_seen else None

        return UnitEnergyDay(
            grid_import_kwh=imported,
            grid_export_kwh=exported,
            battery_charged_kwh=self._counter_delta(state, "charge"),
            battery_discharged_kwh=self._counter_delta(state, "discharge"),
            load_kwh=self._counter_delta(state, "load"),
            charged_from_surplus_kwh=(
                kwh(state.surplus_watt_seconds) if state.battery_seen else None
            ),
            coverage_pct=coverage,
            metric_flags=frozenset(state.flags),
        )

    @staticmethod
    def _counter_delta(state: _UnitDayState, metric: str) -> float | None:
        start = state.counter_start.get(metric)
        last = state.counter_last.get(metric)
        if start is None or last is None:
            return None
        # A reset already re-baselined ``counter_start``; the delta can never
        # be negative by construction.
        return max(0.0, last - start)

    def _finalize(self, *, kind: str | None) -> EnergyDayRecord:
        assert self._live_date is not None
        units = {unit: self._unit_metrics(state) for unit, state in self._states.items()}
        fleet = self._fleet_rollup(units)
        resolved_kind = kind
        if resolved_kind is None:
            coverage = fleet.coverage_pct
            resolved_kind = (
                "complete"
                if coverage is not None and coverage >= self._settings.min_day_coverage_pct
                else "partial"
            )
        return EnergyDayRecord(
            date=self._live_date,
            timezone=str(self._zone),
            utc_offset_minutes=self._live_offset_minutes or 0,
            kind=resolved_kind,
            units=units,
            fleet=fleet,
            sources=self._sources(),
            counter_cross_check=self._cross_check(units, fleet),
            solar_production_measured=False,
        )

    @staticmethod
    def _fleet_rollup(units: Mapping[str, UnitEnergyDay]) -> FleetEnergyDay:
        def total(name: str) -> float | None:
            values = [
                getattr(metrics, name)
                for metrics in units.values()
                if getattr(metrics, name) is not None
            ]
            return sum(values) if values else None

        coverages = [
            metrics.coverage_pct for metrics in units.values() if metrics.coverage_pct is not None
        ]
        return FleetEnergyDay(
            grid_import_kwh=total("grid_import_kwh"),
            grid_export_kwh=total("grid_export_kwh"),
            battery_charged_kwh=total("battery_charged_kwh"),
            battery_discharged_kwh=total("battery_discharged_kwh"),
            load_kwh=total("load_kwh"),
            charged_from_surplus_kwh=total("charged_from_surplus_kwh"),
            # The WORST unit's coverage -- the evidence-rollup precedence.
            coverage_pct=min(coverages) if coverages else None,
        )

    def _cross_check(
        self, units: Mapping[str, UnitEnergyDay], fleet: FleetEnergyDay
    ) -> CounterCrossCheck:
        """The A-1 passive verdict (DESIGN section 3), reported never applied.

        Grid pair deltas come from the live counter state (the per-unit
        projection carries the scorecard metrics, not the raw pairs), summed
        across units that served the pair.
        """
        del units  # the rollup above already carries the integrated figures
        deltas: dict[str, float | None] = {}
        reset_seen = {"grid_a": False, "grid_b": False}
        for state in self._states.values():
            for metric in ("grid_a", "grid_b"):
                if any(flag == counter_reset_flag(metric) for flag in state.flags):
                    reset_seen[metric] = True
        for metric in ("grid_a", "grid_b"):
            values = [
                delta
                for state in self._states.values()
                if (delta := self._counter_delta(state, metric)) is not None
            ]
            deltas[metric] = sum(values) if values else None
        imported, exported = fleet.grid_import_kwh, fleet.grid_export_kwh
        discriminating = (
            fleet.coverage_pct is not None
            and fleet.coverage_pct >= self._settings.min_day_coverage_pct
            and not reset_seen["grid_a"]
            and not reset_seen["grid_b"]
            and imported is not None
            and exported is not None
            and imported >= _A1_MIN_SIDE_KWH
            and exported >= _A1_MIN_SIDE_KWH
            and deltas["grid_a"] is not None
            and deltas["grid_b"] is not None
        )
        verdict: str | None = None
        if discriminating:
            pair_a = deltas["grid_a"]
            pair_b = deltas["grid_b"]
            assert pair_a is not None and pair_b is not None
            assert imported is not None and exported is not None

            def close(measured: float, integral: float) -> bool:
                tolerance = max(_A1_ABS_TOLERANCE_KWH, _A1_RELATIVE_TOLERANCE * integral)
                return abs(measured - integral) <= tolerance

            vendor = close(pair_a, imported) and close(pair_b, exported)
            swapped = close(pair_b, imported) and close(pair_a, exported)
            if vendor and swapped:
                # Both orderings fit: the day cannot discriminate.
                verdict = VERDICT_UNDISCRIMINATING
                discriminating = False
            elif vendor:
                verdict = VERDICT_VENDOR_LABELS
            elif swapped:
                verdict = VERDICT_SWAPPED
            else:
                # Neither ordering fits: the day had the evidence (both
                # sides, full coverage, no resets) but the pairs disagree
                # with the integration beyond the tolerance -- reported
                # without a verdict, never reconciled.
                verdict = None
        return CounterCrossCheck(
            grid_a_delta_kwh=deltas["grid_a"],
            grid_b_delta_kwh=deltas["grid_b"],
            consistent_with=verdict,
            discriminating=discriminating,
        )

    # --- internals: persistence ----------------------------------------------

    def _baselines(self) -> dict[str, EnergyUnitBaseline]:
        baselines: dict[str, EnergyUnitBaseline] = {}
        if self._live_date is None:
            return baselines
        for unit, state in self._states.items():
            baselines[unit] = EnergyUnitBaseline(
                date=self._live_date,
                counter_start=dict(state.counter_start),
                counter_last=dict(state.counter_last),
                import_watt_seconds=state.import_watt_seconds,
                export_watt_seconds=state.export_watt_seconds,
                surplus_watt_seconds=state.surplus_watt_seconds,
                sampled_seconds=state.sampled_seconds,
                last_capture_wall=state.last_capture_wall,
                last_grid_watts=state.grid_anchor_watts,
                flags=frozenset(state.flags),
            )
        return baselines

    def _restore_baseline(self, baselines: Mapping[str, EnergyUnitBaseline]) -> None:
        for unit, baseline in baselines.items():
            state = self._states.get(unit)
            if state is None:
                continue
            self._live_date = baseline.date
            self._live_offset_minutes = None
            state.counter_start = dict(baseline.counter_start)
            state.counter_last = dict(baseline.counter_last)
            state.import_watt_seconds = baseline.import_watt_seconds
            state.export_watt_seconds = baseline.export_watt_seconds
            state.surplus_watt_seconds = baseline.surplus_watt_seconds
            state.sampled_seconds = baseline.sampled_seconds
            state.flags = set(baseline.flags)
            state.grid_seen = bool(baseline.last_grid_watts is not None)
            state.battery_seen = False
            state.restored_wall = baseline.last_capture_wall
            state.restored_grid_watts = baseline.last_grid_watts

    # --- internals: audit facts ----------------------------------------------

    def _base_event(
        self,
        *,
        event_type: str,
        correlation_id: str,
        result: str,
        reason_codes: tuple[str, ...],
        facts: Mapping[str, Any],
        unit_id: str | None = None,
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now()
        if not isinstance(wall, datetime) or wall.tzinfo is None:
            raise EnergyAccountingError("clock wall time must be timezone-aware")
        return AuditEvent(
            event_id=f"energy-{uuid.uuid4().hex}",
            occurred_at=_as_utc(wall),
            monotonic_offset_s=now_mono - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type=event_type,
            unit_id=unit_id,
            principal=_ENERGY_PRINCIPAL,
            correlation_id=correlation_id,
            policy_version=_ENERGY_POLICY_VERSION,
            configuration_version=self._configuration_version,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint({"event_type": event_type, **dict(facts)}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=UnitLifecycle.OBSERVE_ONLY,
        )

    def _day_recorded_event(self, record: EnergyDayRecord) -> AuditEvent:
        fleet = record.fleet.payload()
        return self._base_event(
            event_type=DAY_RECORDED_AUDIT,
            correlation_id=f"energy:day:{record.date.isoformat()}",
            result="recorded",
            reason_codes=(f"kind:{record.kind}",),
            facts={
                "date": record.date.isoformat(),
                "timezone": record.timezone,
                "utc_offset_minutes": record.utc_offset_minutes,
                "kind": record.kind,
                "fleet": fleet,
            },
        )

    def _reset_event(self, unit_id: str, metric: str) -> AuditEvent:
        return self._base_event(
            event_type=COUNTER_RESET_AUDIT,
            correlation_id=f"energy:reset:{unit_id}:{metric}",
            result="detected",
            reason_codes=("counter_reset_observed",),
            facts={"unit_id": unit_id, "metric": metric},
            unit_id=unit_id,
        )
