"""The telemetry historian's sampler contract (DESIGN_PLANT_HISTORY section 2.1-2.2, H2).

These tests name the historian's public behavior before pinning it: cadence
and the boot baseline, the staleness guard (a stale latest writes NO row),
the row projection (the 15 observables with the ``_telemetry_summary``
derivations, the quality rollup's REQUIRED-set scoping and precedence, the
commanded triple's source attribution), first-sample-at-boot, the batch
append, and per-cycle suppression -- a failing tick is a gap, never an
exception into the fleet loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energypod.domain.intents import Direction, IntentSource, PowerIntent

try:
    from energypod.application.history import TelemetryHistorian
    from energypod.domain.observations import DataQuality, Observation, UnitLifecycle
except ImportError as exc:  # pragma: no cover - initial red phase only
    from enum import StrEnum

    TelemetryHistorian: Any = None
    Observation: Any = None

    class _RedQuality(StrEnum):  # keeps module-level parametrize collectable
        GOOD = "good"
        STALE = "stale"
        MISSING = "missing"
        BAD = "bad"
        SUSPECT = "suspect"

    class _RedLifecycle(StrEnum):
        OBSERVE_ONLY = "observe_only"

    DataQuality = _RedQuality
    UnitLifecycle = _RedLifecycle
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


UNIT_A = "mid"
UNIT_B = "rhs"
INTERVAL_S = 30.0
CONTROL_PERIOD_S = 0.40


class ManualClock:
    def __init__(self, *, start: float = 1000.0) -> None:
        self.now = start
        self.wall = datetime(2026, 8, 25, 6, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        del seconds
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


class RecordingHistory:
    """The repository double: records appended batches, can be told to fail."""

    def __init__(self) -> None:
        self.batches: list[tuple[Any, ...]] = []
        self.fail_with: BaseException | None = None
        self.maintained_at: list[datetime] = []

    def append_samples(self, rows: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.batches.append(tuple(rows))

    def maintain(self, now: datetime) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.maintained_at.append(now)

    def rows(self, unit_id: str) -> list[Any]:
        return [row for batch in self.batches for row in batch if row.unit_id == unit_id]


class ObservationPort:
    def __init__(self, latest: dict[str, Any]) -> None:
        self._latest = latest

    async def all_latest(self) -> dict[str, Any]:
        return dict(self._latest)


class IntentsPort:
    def __init__(self, intents: tuple[Any, ...] = ()) -> None:
        self._intents = intents

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return self._intents


@dataclass
class HealthView:
    state: str


class HealthPort:
    def __init__(self, states: Mapping[str, HealthView]) -> None:
        self._states = dict(states)

    async def __call__(self) -> dict[str, HealthView]:
        return dict(self._states)


def _observation(
    unit_id: str,
    *,
    captured_at_mono: float,
    lifecycle: Any = None,
    quality: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Any:
    assert Observation is not None
    if quality is None:
        quality = {}
    if lifecycle is None:
        lifecycle = UnitLifecycle.OBSERVE_ONLY
    values: dict[str, Any] = {
        "system_soc_pct": 51.0,
        "bms_soc_pct": 55.0,
        "soh_pct": 98.0,
        "battery_watts": -1500.0,
        "grid_power_w": None,
        "load_power_w": None,
        "pack_voltage_v": 205.5,
        "pack_current_a": -7.3,
        "debug_mode_w": 0,
        "ctrl_mode_w": 1,
        "work_mode_w": None,
        "run_mode_w": 1,
        "cell_voltages_v": (3.30, 3.35, 3.32),
        "temperatures_c": (22.0, 27.5),
        **dict(overrides or {}),
    }
    quality_map = {
        "system_soc_pct": DataQuality.GOOD,
        "bms_soc_pct": DataQuality.GOOD,
        "soh_pct": DataQuality.GOOD,
        "battery_watts": DataQuality.GOOD,
        "pack_voltage_v": DataQuality.GOOD,
        "pack_current_a": DataQuality.GOOD,
        "dynamic_charge_limit_w": DataQuality.GOOD,
        "dynamic_discharge_limit_w": DataQuality.GOOD,
        "cell_voltages_v": DataQuality.GOOD,
        "temperatures_c": DataQuality.GOOD,
        **dict(quality),
    }
    values.pop("quality", None)
    return Observation(
        unit_id=unit_id,
        wall_timestamp=datetime(2026, 8, 25, 5, 59, 0, tzinfo=UTC),
        captured_at_mono=captured_at_mono,
        sequence=1,
        lifecycle=lifecycle,
        protocol_profile="iot",
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=2500.0,
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality=quality_map,
        **values,
    )


def _historian(
    *,
    clock: ManualClock,
    history: RecordingHistory,
    latest: dict[str, Any],
    intents: tuple[Any, ...] = (),
    health: Mapping[str, HealthView] | None = None,
    adviser_claims: Any = None,
    timezone: str = "UTC",
    interval_s: float = INTERVAL_S,
) -> Any:
    assert TelemetryHistorian is not None
    return TelemetryHistorian(
        unit_ids=(UNIT_A, UNIT_B),
        sample_interval_s=interval_s,
        control_period_s=CONTROL_PERIOD_S,
        clock=clock,
        history=history,
        observations=ObservationPort(latest),
        timezone=timezone,
        intents=IntentsPort(intents),
        health_states=None if health is None else HealthPort(health),
        adviser_claims=adviser_claims,
    )


def _intent(
    *,
    source: IntentSource,
    direction: Direction = Direction.CHARGE,
    watts: int = 2500,
    units: tuple[str, ...] = (UNIT_A,),
    accepted_at: float = 999.0,
    duration: float = 600.0,
) -> PowerIntent:
    return PowerIntent(
        id=f"intent-{source.value}-{accepted_at}",
        source=source,
        selected_unit_ids=frozenset(units),
        direction=direction,
        watts=watts,
        duration_s=duration,
        accepted_at_mono=accepted_at,
        actor_identity="person:operator",
    )


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The historian contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


async def test_first_tick_samples_immediately_then_gates_on_the_interval() -> None:
    """DESIGN section 2.1 / cadence + boot-baseline vectors / S0."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
    historian = _historian(clock=clock, history=history, latest=latest)

    await historian.tick()
    await historian.tick()  # +0 s: not due
    assert len(history.rows(UNIT_A)) == 1, "the boot baseline samples on the first tick"

    clock.advance(INTERVAL_S - 0.01)
    await historian.tick()
    assert len(history.rows(UNIT_A)) == 1, "just under one interval: still not due"

    clock.advance(0.01)
    latest[UNIT_A] = _observation(UNIT_A, captured_at_mono=clock.now)
    await historian.tick()
    assert len(history.rows(UNIT_A)) == 2
    first, second = history.rows(UNIT_A)
    assert first.sampled_at == datetime(2026, 8, 25, 6, 0, 0, tzinfo=UTC)
    assert second.sampled_at == datetime(2026, 8, 25, 6, 0, 30, tzinfo=UTC)


async def test_every_unit_sampled_in_one_tick_shares_one_whole_second_timestamp() -> None:
    """DESIGN section 2.1 / the shared tick wall clock / S0."""
    _require_contract()
    clock = ManualClock(start=2000.0)
    clock.wall = datetime(2026, 8, 25, 7, 13, 12, 900000, tzinfo=UTC)
    history = RecordingHistory()
    latest = {
        UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now),
        UNIT_B: _observation(UNIT_B, captured_at_mono=clock.now),
    }
    historian = _historian(clock=clock, history=history, latest=latest)

    await historian.tick()
    batch = history.batches[0]
    assert {row.unit_id for row in batch} == {UNIT_A, UNIT_B}
    assert {row.sampled_at for row in batch} == {datetime(2026, 8, 25, 7, 13, 12, tzinfo=UTC)}, (
        "truncated to whole seconds and shared by every unit in the tick"
    )


async def test_a_stale_latest_writes_no_row() -> None:
    """DESIGN section 2.1 / the staleness guard / S0.

    The bound is max(3 x control_period_s, sample_interval_s) = 30 s here: a
    latest observation frozen 30+ s old contributes NO row -- an honest gap,
    never a fabricated continuity under a fresh timestamp.
    """
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    frozen = _observation(UNIT_A, captured_at_mono=clock.now)
    latest = {UNIT_A: frozen}
    historian = _historian(clock=clock, history=history, latest=latest)

    await historian.tick()  # fresh at the boot tick: recorded
    assert len(history.rows(UNIT_A)) == 1

    clock.advance(INTERVAL_S + 0.01)
    await historian.tick()  # the frozen latest is now past the bound
    assert len(history.rows(UNIT_A)) == 1, "a stale latest contributes no row"

    clock.advance(0.01)
    await historian.tick()
    assert len(history.rows(UNIT_A)) == 1, "the retry keeps being refused while stale"

    latest[UNIT_A] = _observation(UNIT_A, captured_at_mono=clock.now - 1.0)
    await historian.tick()
    assert len(history.rows(UNIT_A)) == 2, "fresh evidence records on the retry tick"


async def test_the_row_projects_the_observables_with_nulls_never_zeroed() -> None:
    """DESIGN section 2.2 / field provenance via the ``_telemetry_summary``
    derivations; absent data is null, never zero."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {
        UNIT_A: _observation(
            UNIT_A,
            captured_at_mono=clock.now,
            lifecycle=UnitLifecycle.OBSERVE_ONLY,
            overrides={"grid_power_w": -2870.0, "load_power_w": 410.0},
        )
    }
    historian = _historian(clock=clock, history=history, latest=latest)
    await historian.tick()
    row = history.rows(UNIT_A)[0]
    assert row.system_soc_pct == 51.0
    assert row.bms_soc_pct == 55.0
    assert row.soh_pct == 98.0
    assert row.battery_watts == -1500.0
    assert row.grid_power_w == -2870.0
    assert row.load_power_w == 410.0
    assert row.pack_voltage_v == 205.5
    assert row.pack_current_a == -7.3
    assert row.cell_min_v == 3.30
    assert row.cell_max_v == 3.35
    assert row.cell_spread_mv == pytest.approx(50.0)
    assert row.temperature_min_c == 22.0
    assert row.temperature_max_c == 27.5
    assert row.dynamic_charge_limit_w == 2500.0
    assert row.dynamic_discharge_limit_w == 2500.0
    assert row.lifecycle == "observe_only"
    assert (row.debug_mode_w, row.ctrl_mode_w, row.work_mode_w, row.run_mode_w) == (0, 1, None, 1)

    # The absent CT block: null grid/load words stay null (never zero), and
    # an empty cell array projects null min/max/spread.
    clock = ManualClock()
    history = RecordingHistory()
    latest = {
        UNIT_B: _observation(UNIT_B, captured_at_mono=clock.now, overrides={"cell_voltages_v": ()})
    }
    historian = _historian(clock=clock, history=history, latest=latest)
    await historian.tick()
    bare = history.rows(UNIT_B)[0]
    assert bare.grid_power_w is None and bare.load_power_w is None
    assert bare.cell_min_v is None and bare.cell_max_v is None and bare.cell_spread_mv is None


@pytest.mark.parametrize(
    ("quality_overrides", "expected"),
    [
        ({}, "good"),
        ({"system_soc_pct": DataQuality.STALE, "soh_pct": DataQuality.STALE}, "good"),
        # An absent CT block keeps the rollup clean: the pair joins only
        # when the composed plan serves it (the keys exist in the map).
        ({"battery_watts": DataQuality.SUSPECT}, "suspect"),
        ({"battery_watts": DataQuality.SUSPECT, "cell_voltages_v": DataQuality.STALE}, "stale"),
        ({"cell_voltages_v": DataQuality.BAD, "temperatures_c": DataQuality.STALE}, "bad"),
        (
            {"grid_power_w": DataQuality.MISSING, "load_power_w": DataQuality.MISSING},
            "missing",
        ),
        (
            {
                "grid_power_w": DataQuality.MISSING,
                "load_power_w": DataQuality.MISSING,
                "battery_watts": DataQuality.BAD,
            },
            "missing",
        ),
    ],
)
async def test_the_quality_rollup_scope_and_precedence(
    quality_overrides: Mapping[str, Any], expected: str
) -> None:
    """DESIGN section 2.2 / the rollup is the worst over the CONTROL-RATE
    fields only: ``REQUIRED_SAFETY_QUALITY_FIELDS`` minus the cold-ring
    ``system_soc_pct``/``soh_pct``, plus the CT pair WHEN COMPOSED, with the
    precedence missing > bad > stale > suspect > good."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now, quality=quality_overrides)}
    historian = _historian(clock=clock, history=history, latest=latest)
    await historian.tick()
    assert history.rows(UNIT_A)[0].quality == expected


async def test_the_commanded_triple_attribution() -> None:
    """DESIGN section 2.2 / source/direction from the winner set, watts from
    the cycle's peeked authority; all null when no intent claims the unit."""
    _require_contract()
    peeked = {UNIT_A: (2500, "charge"), UNIT_B: (0, None)}

    cases: list[tuple[tuple[Any, ...], Any, str | None, str | None, int | None, int | None]] = [
        # (intents, adviser claims, source, direction, watts, expected per case)
        ((), None, None, None, None, None),
        ((_intent(source=IntentSource.MANUAL),), None, "manual", "charge", 2500, 2500),
        (
            (_intent(source=IntentSource.SCHEDULE, direction=Direction.DISCHARGE),),
            None,
            "schedule",
            "discharge",
            2500,
            2500,
        ),
        ((_intent(source=IntentSource.OPTIMIZER),), None, "optimizer", "charge", 2500, 2500),
        (
            (_intent(source=IntentSource.OPTIMIZER),),
            {UNIT_A: "excess_adviser"},
            "excess_adviser",
            "charge",
            2500,
            2500,
        ),
        (
            (_intent(source=IntentSource.OPTIMIZER),),
            {UNIT_A: "night_adviser"},
            "night_adviser",
            "charge",
            2500,
            2500,
        ),
        (
            (_intent(source=IntentSource.MANUAL),),
            {UNIT_A: "excess_adviser"},
            "manual",
            "charge",
            2500,
            2500,
        ),
    ]
    for intents, claims, source, direction, _, watts in cases:
        clock = ManualClock()
        history = RecordingHistory()
        latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
        historian = _historian(
            clock=clock,
            history=history,
            latest=latest,
            intents=intents,
            adviser_claims=(None if claims is None else (lambda mapping=claims: mapping)),
        )
        await historian.tick(authorized=peeked)
        row = history.rows(UNIT_A)[0]
        assert (row.commanded_source, row.commanded_direction, row.commanded_w) == (
            source,
            direction,
            watts,
        ), f"attribution for intents={intents!r}, claims={claims!r}"

    # A winner with no peeked authority yet records the source and direction
    # with NULL watts: authority is minted by the kernel tick, not assumed.
    clock = ManualClock()
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
    historian = _historian(
        clock=clock,
        history=history,
        latest=latest,
        intents=(_intent(source=IntentSource.SCHEDULE),),
    )
    await historian.tick(authorized={UNIT_A: None})
    row = history.rows(UNIT_A)[0]
    assert row.commanded_source == "schedule"
    assert row.commanded_direction == "charge"
    assert row.commanded_w is None


async def test_the_commanded_triple_is_null_while_an_emergency_stop_holds() -> None:
    """A latched stop fences; it commands nothing on the wire, so the row's
    honest triple is the null one -- the stop itself is the audit trail's
    fact, not history's."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
    stop = PowerIntent(
        id="stop-1",
        source=IntentSource.EMERGENCY_STOP,
        selected_unit_ids=frozenset({UNIT_A}),
        direction=Direction.IDLE,
        watts=0,
        duration_s=3600.0,
        accepted_at_mono=999.0,
        actor_identity="person:operator",
    )
    historian = _historian(clock=clock, history=history, latest=latest, intents=(stop,))
    await historian.tick(authorized={UNIT_A: (0, None)})
    row = history.rows(UNIT_A)[0]
    assert (row.commanded_source, row.commanded_direction, row.commanded_w) == (None, None, None)


async def test_health_state_rides_the_recovery_projection() -> None:
    """DESIGN section 2.2 / ``health_state`` is the recovery monitor's latest
    per-unit projection, null when no monitor is wired or the unit is absent."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {
        UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now),
        UNIT_B: _observation(UNIT_B, captured_at_mono=clock.now),
    }
    historian = _historian(
        clock=clock,
        history=history,
        latest=latest,
        health={UNIT_A: HealthView(state="self_healing")},
    )
    await historian.tick()
    by_unit = {row.unit_id: row for batch in history.batches for row in batch}
    assert by_unit[UNIT_A].health_state == "self_healing"
    assert by_unit[UNIT_B].health_state is None


async def test_a_busy_or_failing_store_is_a_survived_gap_and_a_retry() -> None:
    """DESIGN section 2.1 / a failed append is suppressed; the per-unit clock
    does not advance, so the next tick IS the retry."""
    _require_contract()
    clock = ManualClock()
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
    historian = _historian(clock=clock, history=history, latest=latest)

    await historian.tick()
    assert len(history.rows(UNIT_A)) == 1

    clock.advance(INTERVAL_S)
    latest[UNIT_A] = _observation(UNIT_A, captured_at_mono=clock.now)
    history.fail_with = RuntimeError("store exploded")
    await historian.tick()  # suppressed: no raise, no batch
    history.fail_with = None
    assert len(history.rows(UNIT_A)) == 1

    await historian.tick()  # still due: the retry lands one tick later
    assert len(history.rows(UNIT_A)) == 2


async def test_the_tick_never_raises_into_the_loop() -> None:
    """DESIGN section 2.1 / every failure inside the tick is suppressed."""
    _require_contract()

    class ExplodingPort:
        async def all_latest(self) -> dict[str, Any]:
            raise RuntimeError("observation port failed")

    class ExplodingIntents:
        async def active(self, now_mono: float) -> tuple[Any, ...]:
            raise RuntimeError("intent port failed")

    class ExplodingHealth:
        async def __call__(self) -> dict[str, Any]:
            raise RuntimeError("health port failed")

    assert TelemetryHistorian is not None
    clock = ManualClock()
    history = RecordingHistory()
    for port, kwargs in (
        (ExplodingPort(), {"observations": ExplodingPort()}),
        (ExplodingIntents(), {"intents": ExplodingIntents()}),
    ):
        del port
        historian = TelemetryHistorian(
            unit_ids=(UNIT_A,),
            sample_interval_s=INTERVAL_S,
            control_period_s=CONTROL_PERIOD_S,
            clock=clock,
            history=history,
            observations=kwargs.get(  # type: ignore[union-attr]
                "observations", ObservationPort({})
            ),
            intents=kwargs.get("intents", IntentsPort(())),  # type: ignore[union-attr]
            health_states=ExplodingHealth(),
        )
        await historian.tick()

    clock = ManualClock()
    historian = TelemetryHistorian(
        unit_ids=(UNIT_A,),
        sample_interval_s=INTERVAL_S,
        control_period_s=CONTROL_PERIOD_S,
        clock=clock,
        history=history,
        observations=ObservationPort({UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}),
        health_states=ExplodingHealth(),
    )
    await historian.tick()  # the health read alone must not sink the sample
    assert len(history.rows(UNIT_A)) == 1


async def test_maintenance_runs_on_the_first_tick_after_site_midnight() -> None:
    """DESIGN section 2.4 / the historian owns the cadence: one maintain pass
    on the first tick whose wall clock enters a new site-local date."""
    _require_contract()
    clock = ManualClock()
    clock.wall = datetime(2026, 8, 25, 13, 59, 10, tzinfo=UTC)
    history = RecordingHistory()
    latest = {UNIT_A: _observation(UNIT_A, captured_at_mono=clock.now)}
    historian = _historian(
        clock=clock, history=history, latest=latest, timezone="Australia/Brisbane"
    )

    await historian.tick()
    assert len(history.maintained_at) == 0, "the boot pass belongs to composition, not the tick"

    clock.advance(30.0)  # 23:59:40 local: same local date
    await historian.tick()
    assert len(history.maintained_at) == 0, "no midnight crossed: no maintenance"

    clock.advance(30.0)  # 00:00:10 local 2026-08-26: first tick after midnight
    await historian.tick()
    assert history.maintained_at == [clock.wall_now()]

    clock.advance(INTERVAL_S)
    await historian.tick()
    assert len(history.maintained_at) == 1, "exactly one pass per crossed midnight"
