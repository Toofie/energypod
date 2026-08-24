"""The parking companions: health, advisers, detector, bias, advisory
(DESIGN_POD_PARKING sections 3 and 7 -- wave B3, part 3).

- T-PARK-HEALTH: the composable PARKED state (``("parked", *healing)``), the
  pinned precedence (fault classes outrank a park), the expiry and
  write-unverified hints, and the transitions both directions.
- The adviser vocabulary: night-charge excludes parked units at selection
  with ``unit_parked`` outranking ``units_disarmed``; the excess adviser
  renders ``unit_parked`` in place of ``no_eligible_target`` when park is
  the only exclusion; the schedule projection carries ``unit_parked`` beside
  ``window_open`` while the runner stays dumb (it still submits).
- T-PARK-FOREIGN's detector half: foreign PQ objectives observed on a parked
  unit carry the ``unit_parked`` annotation -- never silent, never an alert
  by itself.
- ``delivery_bias``: the pinned four-key evidence-only projection.
- ``recovery_advisory``: the pinned shape, present ONLY under the wedge
  signature, with ``commissioned: false`` the uncommissioned-honesty state.

SAFETY: deterministic fakes only; no hardware, no sockets, no restarts.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from .test_excess_charge import (
    FakeClock as ExcessClock,
)
from .test_excess_charge import (
    FakeIntents as ExcessIntents,
)
from .test_excess_charge import (
    FakeObservations as ExcessObservations,
)
from .test_excess_charge import (
    FakeSubmit as ExcessSubmit,
)
from .test_excess_charge import (
    make_fleet as make_excess_fleet,
)
from .test_excess_charge import (
    make_policy as make_excess_policy,
)
from .test_excess_charge import (
    make_settings as make_excess_settings,
)
from .test_night_charge import (
    FakeClock as NightClock,
)
from .test_night_charge import (
    FakeIntents as NightIntents,
)
from .test_night_charge import (
    FakeObservations as NightObservations,
)
from .test_night_charge import (
    FakeSubmit as NightSubmit,
)
from .test_night_charge import (
    make_fleet as make_night_fleet,
)
from .test_night_charge import (
    make_policy as make_night_policy,
)
from .test_night_charge import (
    make_settings as make_night_settings,
)

recovery = importlib.import_module("energypod.application.recovery")
parking_module = importlib.import_module("energypod.application.parking")
delivery_bias_module = importlib.import_module("energypod.application.delivery_bias")
foreign_objective = importlib.import_module("energypod.application.foreign_objective")


# --- the shared recovery-monitor rig ------------------------------------------------


@dataclass
class ManualClock:
    now: float = 1000.0
    wall: datetime = field(default_factory=lambda: datetime(2026, 8, 24, 6, 0, 0, tzinfo=UTC))

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall


class RecordingAudit:
    def __init__(self) -> None:
        self.appended: list[Any] = []

    async def append(self, event: Any) -> None:
        self.appended.append(event)


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, body: dict[str, Any]) -> int:
        self.published.append(dict(body))
        return len(self.published)

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [body for body in self.published if body.get("type") == event_type]


def build_monitor(**settings_overrides: Any) -> Any:
    return recovery.RecoveryMonitor(
        unit_ids=frozenset({"mid"}),
        settings=recovery.RecoverySettings(**settings_overrides),
        clock=ManualClock(),
        audit=RecordingAudit(),
        bus=RecordingBus(),
        process_instance_id="recovery-test-process",
        process_origin_mono=1000.0,
        configuration_version=5,
    )


def observation(**overrides: Any) -> SimpleNamespace:
    facts: dict[str, Any] = {
        "battery_watts": 0.0,
        "cell_imbalance_v": 0.020,
        "authoritative_soc_pct": 99.0,
        "served_active_objective_w": 0,
        "served_reactive_objective_var": 0,
        "objective_captured_at_mono": 1000.0,
        "grid_power_w": 0.0,
        "wall_timestamp": datetime(2026, 8, 24, 6, 0, 0, tzinfo=UTC),
    }
    facts.update(overrides)
    return SimpleNamespace(**facts)


async def cycle(
    mon: Any,
    *,
    parked: bool = False,
    park_expired: bool = False,
    park_write_unverified: bool = False,
    lifecycle: str = "disarmed",
    inhibit_latched: bool = False,
    inhibit_reason: str | None = None,
    **observation_overrides: Any,
) -> Any:
    return await mon.observe_cycle(
        "mid",
        authorized_watts=0,
        authorized_direction=None,
        claimed=False,
        lifecycle=lifecycle,
        inhibit_latched=inhibit_latched,
        inhibit_reason=inhibit_reason,
        observation=observation(**observation_overrides),
        now_mono=1000.0,
        parked=parked,
        park_expired=park_expired,
        park_write_unverified=park_write_unverified,
    )


async def health_view(mon: Any) -> Any:
    states = await mon.unit_health_states()
    return states["mid"]


# --- T-PARK-HEALTH --------------------------------------------------------------------


async def test_a_parked_unit_is_the_composable_parked_state() -> None:
    mon = build_monitor()
    await cycle(mon, parked=True, cell_imbalance_v=0.060)
    view = await health_view(mon)
    assert view.state.value == "parked"
    # The composable shape (section 3): ("parked", *healing) -- a parked pod
    # balancing its cells keeps the balancing visibility.
    assert view.reasons == ("parked", "cell_balancing")


async def test_a_parked_and_expired_unit_carries_the_pinned_hint() -> None:
    mon = build_monitor()
    await cycle(mon, parked=True, park_expired=True)
    view = await health_view(mon)
    assert view.state.value == "parked"
    assert view.reasons == ("parked",)
    assert view.remediation_hint == "lease expired — Resume is an operator act"


async def test_the_write_unverified_posture_names_the_operator_resume() -> None:
    mon = build_monitor()
    await cycle(mon, parked=True, park_write_unverified=True)
    view = await health_view(mon)
    assert view.state.value == "parked"
    assert view.reasons[0] == "park_write_unverified"
    assert "park_write_unverified" in view.reasons
    assert view.remediation_hint is not None and "RESUME" in view.remediation_hint


async def test_fault_classes_outrank_a_park_both_ways() -> None:
    """Pinned precedence (section 3): unreachable, not_responding,
    foreign_writer, inhibited, actuation_incoherent legitimately outrank a
    park -- fault beats operator state, so nobody "fixes" it."""

    mon = build_monitor()
    mon.record_read_outcome("mid", recovery.CONNECT_FAILED)
    await cycle(mon, parked=True)
    assert (await health_view(mon)).state.value == "unreachable"

    mon = build_monitor(unresponsive_attempts=1)
    mon.record_read_outcome("mid", recovery.READ_FAILED)
    await cycle(mon, parked=True)
    assert (await health_view(mon)).state.value == "not_responding"

    mon = build_monitor()
    await cycle(mon, parked=True, inhibit_latched=True, inhibit_reason="external_writer")
    assert (await health_view(mon)).state.value == "foreign_writer"

    mon = build_monitor()
    await cycle(mon, parked=True, inhibit_latched=True, inhibit_reason="identity_mismatch")
    assert (await health_view(mon)).state.value == "inhibited"
    assert (await health_view(mon)).state.value != "parked", (
        "an inhibited parked unit names the fault, not the operator state"
    )


async def test_the_incoherent_wedge_outranks_the_park() -> None:
    """The dispatch/park race's detected-and-alarming outcome (section 4):
    a dispatched-then-parked pod trips the coherence watchdog and the
    INCOHERENT state outranks PARKED -- the watchdog is the fence."""
    mon = build_monitor(actuation_coherence_cycles=2, actuation_coherence_min_movement_w=150)
    # Open an authorization episode from a zero baseline, then park mid-flight:
    # the writes keep ACKing while delivery stays 0.
    await mon.observe_cycle(
        "mid",
        authorized_watts=800,
        authorized_direction="charge",
        claimed=True,
        lifecycle="active",
        inhibit_latched=False,
        inhibit_reason=None,
        observation=observation(battery_watts=0.0),
        now_mono=1000.0,
        parked=True,
    )
    await mon.observe_cycle(
        "mid",
        authorized_watts=800,
        authorized_direction="charge",
        claimed=True,
        lifecycle="active",
        inhibit_latched=False,
        inhibit_reason=None,
        observation=observation(battery_watts=0.0),
        now_mono=1001.0,
        parked=True,
    )
    view = await health_view(mon)
    assert view.state.value == "actuation_incoherent", "the wedge outranks the park"
    assert view.reasons[0] == "authorized_not_actuating"


async def test_health_transitions_carry_both_directions() -> None:
    mon = build_monitor()
    bus = mon._bus  # type: ignore[attr-defined]
    await cycle(mon)
    assert (await health_view(mon)).state.value == "healthy"
    await cycle(mon, parked=True)
    await cycle(mon, parked=True)
    transitions = bus.of_type("unit.health_changed")
    assert [event["payload"]["from"] for event in transitions] == ["healthy"]
    assert [event["payload"]["to"] for event in transitions] == ["parked"]
    await cycle(mon, parked=False)
    transitions = bus.of_type("unit.health_changed")
    assert [(event["payload"]["from"], event["payload"]["to"]) for event in transitions] == [
        ("healthy", "parked"),
        ("parked", "healthy"),
    ], "the transition back publishes too (section 3)"


# --- the adviser vocabulary --------------------------------------------------------------


def _night_adviser(parked: set[str], api: Any, night: Any, fleet: dict[str, Any]) -> Any:
    return night.NightChargeAdviser(
        settings=make_night_settings(night),
        policy=make_night_policy(api),
        clock=NightClock(),
        observations=NightObservations(latest=fleet),
        intents=NightIntents(),
        submit=NightSubmit(),
        parked_units=lambda: frozenset(parked),
    )


async def test_the_night_adviser_excludes_parked_units_at_selection() -> None:
    night = importlib.import_module("energypod.application.night_charge")
    api = importlib.import_module("energypod.domain")
    fleet = make_night_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    adviser = _night_adviser({"mid"}, api, night, fleet)

    decision = await adviser.tick()

    plans = {plan.unit_id: plan for plan in decision.unit_plans}
    assert plans["mid"].phase == "sitting_out"
    assert plans["mid"].reason == "unit_parked", "the stated exclusion cause"
    # The parked unit is NOT in the submission even though it is otherwise
    # the neediest eligible unit.
    assert "mid" not in decision.active_unit_ids


async def test_unit_parked_outranks_units_disarmed_in_the_fleet_reasons() -> None:
    night = importlib.import_module("energypod.application.night_charge")
    api = importlib.import_module("energypod.domain")
    # lhs disarmed, mid parked: the fleet codes carry unit_parked and it
    # outranks units_disarmed (resume, not arm, is the true next step).
    fleet = make_night_fleet(
        api, {"lhs": 100.0, "mid": 100.0}, lhs__lifecycle=api.UnitLifecycle.DISARMED
    )
    adviser = _night_adviser({"mid"}, api, night, fleet)
    decision = await adviser.tick()
    assert "unit_parked" in decision.reason_codes
    assert decision.reason_codes.index("unit_parked") < decision.reason_codes.index(
        "units_disarmed"
    )


async def test_the_excess_adviser_renders_unit_parked_in_place_of_no_eligible_target() -> None:
    excess = importlib.import_module("energypod.application.excess_charge")
    api = importlib.import_module("energypod.domain")
    fleet = make_excess_fleet(api, {"lhs": -300.0, "mid": 800.0, "rhs": 900.0})
    adviser = excess.ExcessChargeAdviser(
        settings=make_excess_settings(excess),
        policy=make_excess_policy(api),
        clock=ExcessClock(),
        observations=ExcessObservations(latest=fleet),
        intents=ExcessIntents(),
        submit=ExcessSubmit(),
        parked_units=lambda: frozenset({"lhs", "mid", "rhs"}),
    )
    decision = await adviser.tick()
    assert decision.target_unit_id is None
    assert decision.reason_codes == ("unit_parked",), (
        "park is the only exclusion cause -- not the generic no-target word"
    )


async def test_the_excess_adviser_selects_the_unparked_twin() -> None:
    excess = importlib.import_module("energypod.application.excess_charge")
    api = importlib.import_module("energypod.domain")
    fleet = make_excess_fleet(api, {"lhs": -300.0, "mid": 800.0, "rhs": 900.0})
    adviser = excess.ExcessChargeAdviser(
        settings=make_excess_settings(excess),
        policy=make_excess_policy(api),
        clock=ExcessClock(),
        observations=ExcessObservations(latest=fleet),
        intents=ExcessIntents(),
        submit=ExcessSubmit(),
        parked_units=lambda: frozenset({"mid"}),  # the neediest is parked
    )
    decision = await adviser.tick()
    assert decision.target_unit_id != "mid", "the parked unit is never selected"
    assert decision.reason_codes != ("unit_parked",), "an available twin is not a park story"


async def test_the_schedule_projection_names_unit_parked_while_the_runner_stays_dumb() -> None:
    scheduling = importlib.import_module("energypod.application.scheduling")
    api = importlib.import_module("energypod.domain")
    from datetime import date
    from datetime import time as dtime

    from energypod.domain.schedule import ScheduleEntry, SchedulePlan, Weekday

    published: dict[str, SchedulePlan | None] = {
        "plan": SchedulePlan(
            version=1,
            timezone="UTC",
            entries=(
                ScheduleEntry(
                    entry_id="night-charge",
                    days=frozenset(Weekday(day) for day in range(7)),
                    start_local=dtime(0, 0),
                    end_local=dtime(6, 0),
                    action=api.Direction.CHARGE,
                    watts=1000,
                    unit_ids=frozenset({"mid"}),
                    effective_from=date(2026, 1, 1),
                    effective_until=date(2027, 1, 1),
                    priority=1,
                    enabled=True,
                ),
            ),
        )
    }
    submissions: list[dict[str, Any]] = []

    async def submit(**kwargs: Any) -> dict[str, Any]:
        submissions.append(dict(kwargs))
        return {"intent_id": "schedule-1"}

    class Store:
        async def get_plan(self) -> SchedulePlan | None:
            return published["plan"]

    class ClockPort:
        def __init__(self) -> None:
            self.now = 100.0
            self.wall = datetime(2026, 8, 27, 1, 30, 0, tzinfo=UTC)

        def monotonic(self) -> float:
            return self.now

        def wall_now(self) -> datetime:
            return self.wall

    class IntentsPort:
        async def active(self, now_mono: float) -> tuple[Any, ...]:
            return ()

        async def remove(self, intent_id: str) -> None:
            return

    runner = scheduling.ScheduleRunner(
        store=Store(),
        evaluator=scheduling.ScheduleEvaluator(intent_ttl_s=10.0),
        clock=ClockPort(),
        submit=submit,
        intents=IntentsPort(),
        parked_units=lambda: frozenset({"mid"}),
    )
    await runner.tick()
    state = runner.state_payload()
    # The runner SUBMITTED (it stays dumb -- the published fact is not a claim
    # check) and the projection names the parked unit beside window_open.
    assert submissions, "the runner submits the published fact regardless"
    assert "unit_parked" in state["reason_codes"]
    assert "window_open" in state["reason_codes"]


# --- T-PARK-FOREIGN: the detector annotation -----------------------------------------------


async def test_a_foreign_objective_on_a_parked_unit_is_annotated_not_alerted() -> None:
    """The night-writer case (section 3): a foreign PQ objective observed on
    a PARKED unit carries the ``unit_parked`` annotation -- never silent,
    never an alert by itself (the classification is untouched)."""
    settings = foreign_objective.ForeignObjectiveSettings()
    bus = RecordingBus()
    monitor = foreign_objective.ForeignObjectiveMonitor(
        unit_ids=frozenset({"mid"}),
        settings=settings,
        clock=ManualClock(),
        audit=RecordingAudit(),
        bus=bus,
        process_instance_id="foreign-test-process",
        process_origin_mono=1000.0,
        configuration_version=5,
    )
    sample_observation = observation(
        served_active_objective_w=-2400,
        served_reactive_objective_var=0,
        objective_captured_at_mono=1000.5,
        grid_power_w=0.0,
    )
    await monitor.observe_cycle(
        "mid",
        lifecycle="armed_idle",
        claimed=False,
        authorized_watts=0,
        observation=sample_observation,
        now_mono=1001.0,
        parked=True,
    )
    record = monitor.unit_last_observed("mid")
    assert record is not None
    assert record.get("parked") is True, "the annotation is present, never silent"
    # The classification stays the detector's own judgment -- the parked
    # annotation adds evidence, it does not escalate.
    assert record["classification"] in {
        "pod_autonomy_objective_observed",
        "foreign_objective_observed",
        "handback_grace",
    }


async def test_an_unparked_sample_carries_no_annotation() -> None:
    settings = foreign_objective.ForeignObjectiveSettings()
    monitor = foreign_objective.ForeignObjectiveMonitor(
        unit_ids=frozenset({"mid"}),
        settings=settings,
        clock=ManualClock(),
        audit=RecordingAudit(),
        bus=RecordingBus(),
        process_instance_id="foreign-test-process",
        process_origin_mono=1000.0,
        configuration_version=5,
    )
    await monitor.observe_cycle(
        "mid",
        lifecycle="armed_idle",
        claimed=False,
        authorized_watts=0,
        observation=observation(
            served_active_objective_w=-2400,
            objective_captured_at_mono=1000.5,
        ),
        now_mono=1001.0,
        parked=False,
    )
    record = monitor.unit_last_observed("mid")
    assert record is not None and "parked" not in record


# --- delivery_bias (section 7) --------------------------------------------------------------


def test_the_delivery_bias_projection_is_the_pinned_four_keys() -> None:
    estimator = delivery_bias_module.DeliveryBiasEstimator(unit_ids=frozenset({"mid"}))
    empty = estimator.projection("mid", now_mono=1000.0)
    assert empty == {
        "mean_bias_pct": None,
        "max_bias_pct": None,
        "sample_count": 0,
        "window_s": 0.0,
    }
    estimator.record("mid", authorized_w=1000.0, measured_w=1150.0, now_mono=1000.0)
    estimator.record("mid", authorized_w=1000.0, measured_w=840.0, now_mono=1090.0)
    projection = estimator.projection("mid", now_mono=1150.0)
    assert set(projection) == {"mean_bias_pct", "max_bias_pct", "sample_count", "window_s"}
    assert projection["sample_count"] == 2
    assert projection["mean_bias_pct"] == -0.5  # (+15% and -16% average)
    assert projection["max_bias_pct"] == 16.0
    assert projection["window_s"] == 150.0


def test_the_window_is_bounded_and_nonpositive_authority_records_nothing() -> None:
    estimator = delivery_bias_module.DeliveryBiasEstimator(unit_ids=frozenset({"mid"}))
    for index in range(700):
        estimator.record(
            "mid", authorized_w=1000.0, measured_w=1000.0, now_mono=1000.0 + index
        )
    projection = estimator.projection("mid", now_mono=2000.0)
    assert projection["sample_count"] == 600, "the ~600-sample cap"
    estimator.record("mid", authorized_w=0.0, measured_w=500.0, now_mono=2100.0)
    estimator.record("mid", authorized_w=1000.0, measured_w=None, now_mono=2100.0)
    assert estimator.projection("mid", now_mono=2200.0)["sample_count"] == 600


# --- recovery_advisory (section 7) ------------------------------------------------------------


class _AdvisoryRecoveryView:
    """The facade's RecoveryView port, scripted for the advisory tests."""

    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    async def unit_health_states(self) -> dict[str, Any]:
        return self._states


class _StubParking:
    """A ParkControl stub: ``None``-shaped when uncommissioned, present
    otherwise (the facade derives ``commissioned`` from its presence)."""

    def __init__(self, commissioned: bool) -> None:
        self._commissioned = commissioned

    @property
    def commissioning(self) -> Any:
        return parking_module.ParkCommissioning(
            max_lease_s=14400, default_lease_s=14400, mode_write_enabled=True
        )


async def _unit_detail_with_recovery(
    api: Any, view: Any, parking: Any
) -> dict[str, Any]:
    """One facade unit-detail projection over the scripted recovery view."""
    facade = api.EnergyServiceFacade(
        site_id="home",
        clock=ManualClock(),
        intents=_NullIntents(),
        observations=_NullObservations(),
        authorizations=_NullAuthorizations(),
        audit=_NullAudit(),
        events=_NullBus(),
        coordinator=_NullCoordinator(),
        actors={"mid": _StubActor()},
        recovery=view,
        parking=parking,
    )
    return await facade.unit_detail(principal=_OPERATOR(api), unit_id="mid")


def _OPERATOR(api: Any) -> Any:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Operator:
        subject: str = "person:operator"
        scopes: frozenset[str] = frozenset({"observe", "arm"})
        interactive: bool = True
        site_id: str = "home"

    return Operator()


class _NullIntents:
    async def add(self, intent: Any) -> None:
        return None

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return ()

    async def remove(self, intent_id: str) -> None:
        return None


class _NullObservations:
    async def latest(self, unit_id: str) -> None:
        return None

    async def all_latest(self) -> dict[str, Any]:
        return {}


class _NullAuthorizations:
    async def peek(self, unit_id: str) -> None:
        return None

    async def revoke(self, **kwargs: Any) -> None:
        return None


class _NullAudit:
    async def append(self, event: Any) -> None:
        return None

    async def recent(self, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]:
        return ()


class _NullBus:
    async def publish(self, body: dict[str, Any]) -> int:
        return 0

    def snapshot_sequence(self) -> int:
        return 0


class _NullCoordinator:
    async def snapshot(self) -> None:
        return None

    async def advance(self, *, reason: str) -> None:
        return None


class _StubActor:
    unit_id = "mid"
    lifecycle = None
    inhibit_latched = False

    async def arm(self, *, takeover_acknowledged: bool = False) -> None:
        return None

    async def disarm(self) -> None:
        return None

    async def acknowledge_inhibit(self) -> None:
        return None

    async def request_bounded_zero(self, reason: str) -> None:
        return None

    async def refresh_mode_words(self) -> tuple[int, int]:
        return (1, 6)


@pytest.mark.parametrize("commissioned", [True, False])
async def test_the_recovery_advisory_renders_under_the_wedge_signature(commissioned: bool) -> None:
    api = importlib.import_module("energypod.application.service")
    view = _AdvisoryRecoveryView(
        {
            "mid": recovery.UnitHealthView(
                unit_id="mid",
                state=recovery.HealthState.ACTUATION_INCOHERENT,
                reasons=("authorized_not_actuating", "objective_not_served"),
                remediation_hint=None,
            )
        }
    )
    detail = await _unit_detail_with_recovery(
        api, view, _StubParking(True) if commissioned else None
    )
    advisory = detail["recovery_advisory"]
    # The pinned shape (the wave C decoder contract).
    assert set(advisory) == {"commissioned", "echo_classifications"}
    assert advisory["commissioned"] is commissioned
    assert advisory["echo_classifications"] == ["objective_not_served"]


async def test_no_wedge_signature_means_no_advisory_key_at_all() -> None:
    api = importlib.import_module("energypod.application.service")
    healthy = _AdvisoryRecoveryView(
        {
            "mid": recovery.UnitHealthView(
                unit_id="mid",
                state=recovery.HealthState.HEALTHY,
                reasons=(),
                remediation_hint=None,
            )
        }
    )
    detail = await _unit_detail_with_recovery(api, healthy, _StubParking(True))
    assert "recovery_advisory" not in detail, "absence IS the no-advisory state"

    incoherent_without_echo = _AdvisoryRecoveryView(
        {
            "mid": recovery.UnitHealthView(
                unit_id="mid",
                state=recovery.HealthState.ACTUATION_INCOHERENT,
                reasons=("authorized_not_actuating",),
                remediation_hint=None,
            )
        }
    )
    detail = await _unit_detail_with_recovery(api, incoherent_without_echo, _StubParking(True))
    assert "recovery_advisory" not in detail, "no discriminating echo, no advisory"
