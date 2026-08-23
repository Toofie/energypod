"""Contract tests for the off-peak night charge strategy (DESIGN_NIGHT_CHARGE).

The module under test is ``energypod.application.night_charge`` (API_CONTRACTS
"Off-peak night charge").  It is an ADVISORY strategy layer in the
excess-adviser pattern: it computes a per-battery charge plan each tick inside
a commissioned civil-time window, submits ordinary short-TTL ``OPTIMIZER``
CHARGE intents through the facade twin, and answers measured site demand above
the threshold per the commissioned POSTURE — ``hold`` keeps every participating
battery in the submission at a small POSITIVE charge (the held objective
replaces the pods' load-matching autonomy), ``standby`` (the operator's
preference) stands units down entirely (zero-watt non-participation, excluded
from the submission).  Both postures resume pacing when demand falls (with
hysteresis), both FAIL CLOSED TO HOLD on bad evidence, and both let batteries
already at the ceiling sit out.  Hand-back at window end is NON-RENEWAL: no
stop triple, no idle intent, ever.

Pinned contract (the red phase fails cleanly while the module is absent)::

    NightChargeSettings(               # every behavioural key of the block
        rate_cap_w, hold_rate_w, demand_threshold_w, demand_exit_hysteresis_w,
        demand_scope, demand_response, pacing, assumed_capacity_wh,
        demand_telemetry_max_age_s, intent_ttl_s, windows, timezone, unit_ids,
    )
    in_window(local_now, windows) -> bool          # civil containment
    next_window_start(at, windows, zone) -> datetime
    open_window_end(at, windows, zone) -> datetime | None
    NightChargeAdviser(*, settings, policy, clock, observations, intents,
                       submit, participation=None)
        async tick() -> NightChargeDecision
    NightChargeController(...)                     # participation + projection

The design's named correctness pins are NAMED tests here:

- the LOAD-word-not-grid-word demand rule — the grid word includes the
  adviser's OWN charging draw and a grid-word implementation would self-hold
  forever at cap rates;
- the one-held-intent invariant — exactly one live ``night-`` intent, ever,
  maintained by remove-then-submit renewal;
- the posture-invariant fail-closed — missing/bad/stale evidence HOLDS under
  the standby posture too (standby answers measured demand, never missing
  data).
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

NOW = 100.0
UNIT_IDS = ("lhs", "mid", "rhs")
ZONE = ZoneInfo("Australia/Brisbane")
DEFAULT_WINDOW = ((time(0, 0), time(6, 0)),)
QUALITY_FIELDS = (
    "system_soc_pct",
    "bms_soc_pct",
    "soh_pct",
    "battery_watts",
    "pack_voltage_v",
    "pack_current_a",
    "dynamic_charge_limit_w",
    "dynamic_discharge_limit_w",
    "cell_voltages_v",
    "temperatures_c",
    "grid_power_w",
    "load_power_w",
)


@pytest.fixture(scope="module")
def api() -> Any:
    importlib.import_module("energypod.domain")
    domain = importlib.import_module("energypod.domain")
    required = ("ControlPolicy", "DataQuality", "Observation", "UnitLifecycle")
    missing = [name for name in required if not hasattr(domain, name)]
    assert not missing, f"public domain contract is not implemented: {', '.join(missing)}"
    return domain


@pytest.fixture(scope="module")
def night() -> Any:
    """Load the strategy contract; report absence as an ordinary failure."""
    try:
        module = importlib.import_module("energypod.application.night_charge")
    except ImportError as error:
        pytest.fail(f"the night-charge strategy contract is not implemented: {error}")
        raise  # pragma: no cover - pytest.fail never returns
    required = (
        "NightChargeAdviser",
        "NightChargeSettings",
        "NightChargeController",
        "in_window",
        "next_window_start",
        "open_window_end",
    )
    missing = [name for name in required if not hasattr(module, name)]
    assert not missing, f"night-charge contract is incomplete: {', '.join(missing)}"
    return module


# --- deterministic fakes -------------------------------------------------------


@dataclass
class FakeClock:
    now: float = NOW
    wall: datetime = datetime(2026, 8, 27, 1, 0, 0, tzinfo=ZONE)

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))
        await asyncio.sleep(0)


@dataclass
class FakeObservations:
    latest: dict[str, Any] = field(default_factory=dict)

    async def all_latest(self) -> dict[str, Any]:
        return dict(self.latest)


@dataclass
class FakeIntents:
    entries: list[Any] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return tuple(item for item in self.entries if item.expires_at_mono > now_mono)

    async def remove(self, intent_id: str) -> None:
        self.removed.append(intent_id)


@dataclass
class FakeSubmit:
    submissions: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        self.submissions.append(
            {
                "unit_ids": sorted(unit_ids),
                "direction": getattr(direction, "value", direction),
                "watts": watts,
                "watts_by_unit": dict(watts_by_unit) if watts_by_unit else None,
                "ttl_s": ttl_s,
            }
        )
        return {"intent_id": f"night-{len(self.submissions)}", "status": "accepted"}


def make_policy(api: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "version": "night-1",
        "static_charge_limit_w_by_unit": {unit: 2_500 for unit in UNIT_IDS},
        "static_discharge_limit_w_by_unit": {unit: 2_500 for unit in UNIT_IDS},
        "fleet_charge_limit_w": 6_000,
        "fleet_discharge_limit_w": 6_000,
        "min_soc_pct": 10.0,
        "max_soc_pct": 95.0,
        "max_soc_jump_pct": 10.0,
        "max_soc_disagreement_pct": 5.0,
        "min_cell_voltage_v": 3.0,
        "max_cell_voltage_v": 3.6,
        "max_cell_imbalance_v": 0.05,
        "expected_cell_count_by_unit": {unit: 4 for unit in UNIT_IDS},
        "min_temperature_c": 0.0,
        "max_temperature_c": 45.0,
        "max_temperature_spread_c": 45.0,
        "max_telemetry_age_s": 5.0,
        "max_cell_age_s": 15.0,
        "authorization_lifetime_s": 2.0,
        "heartbeat_interval_s": 1.5,
        "ramp_limit_w_per_s_by_unit": {unit: 10_000 for unit in UNIT_IDS},
        "apparent_power_limit_va_by_unit": {unit: 5_000 for unit in UNIT_IDS},
        "reactive_limit_var": 0,
        "stable_samples_needed_to_rearm": 3,
        "blocking_fault_codes": frozenset(),
        "blocking_warning_codes": frozenset(),
    }
    values.update(overrides)
    return api.ControlPolicy(**values)


def make_observation(
    api: Any,
    *,
    unit_id: str,
    load_power_w: float | None,
    grid_power_w: float | None = 0.0,
    system_soc_pct: float = 50.0,
    bms_soc_pct: float | None = None,
    dynamic_charge_limit_w: float = 2_500.0,
    lifecycle: Any = None,
    captured_at_mono: float = NOW,
    load_quality: Any = None,
) -> Any:
    quality = {name: api.DataQuality.GOOD for name in QUALITY_FIELDS}
    if load_quality is not None:
        quality["load_power_w"] = load_quality
    return api.Observation(
        unit_id=unit_id,
        wall_timestamp=datetime(2026, 8, 26, tzinfo=UTC),
        captured_at_mono=captured_at_mono,
        sequence=7,
        lifecycle=lifecycle or api.UnitLifecycle.ARMED_IDLE,
        protocol_profile="iot",
        system_soc_pct=system_soc_pct,
        bms_soc_pct=system_soc_pct if bms_soc_pct is None else bms_soc_pct,
        soh_pct=98.0,
        battery_watts=0.0,
        pack_voltage_v=400.0,
        pack_current_a=0.0,
        dynamic_charge_limit_w=dynamic_charge_limit_w,
        dynamic_discharge_limit_w=2_500.0,
        grid_power_w=grid_power_w,
        load_power_w=load_power_w,
        cell_voltages_v=(3.30, 3.31, 3.29, 3.30),
        cell_captured_at_mono=captured_at_mono,
        cell_sequence=4,
        temperatures_c=(24.0, 25.0, 26.0, 25.0),
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality=quality,
    )


def make_fleet(
    api: Any,
    loads: Mapping[str, float | None],
    *,
    socs: Mapping[str, float] | None = None,
    grids: Mapping[str, float | None] | None = None,
    **observation_overrides: Any,
) -> dict[str, Any]:
    """The design's own commissioning fleet: lhs 71 / mid 88 / rhs 98."""
    default_socs = {"lhs": 71.0, "mid": 88.0, "rhs": 98.0}
    socs = default_socs if socs is None else {**default_socs, **socs}
    grids = grids or {}
    overrides_by_unit: dict[str, dict[str, Any]] = {}
    for key, value in observation_overrides.items():
        unit, _, name = key.partition("__")
        overrides_by_unit.setdefault(unit, {})[name] = value
    return {
        unit: make_observation(
            api,
            unit_id=unit,
            load_power_w=load,
            grid_power_w=grids.get(unit, 0.0),
            system_soc_pct=socs.get(unit, 50.0),
            **overrides_by_unit.get(unit, {}),
        )
        for unit, load in loads.items()
    }


def make_settings(night: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "rate_cap_w": 2_500,
        "hold_rate_w": 100,
        "demand_threshold_w": 1_000,
        "demand_exit_hysteresis_w": 200,
        "demand_scope": "fleet",
        "demand_response": "hold",
        "pacing": "cap_first",
        "assumed_capacity_wh": None,
        "demand_telemetry_max_age_s": 3.0,
        "intent_ttl_s": 10.0,
        "windows": DEFAULT_WINDOW,
        "timezone": "Australia/Brisbane",
        "unit_ids": UNIT_IDS,
    }
    values.update(overrides)
    return night.NightChargeSettings(**values)


def make_adviser(
    night: Any,
    api: Any,
    observations: dict[str, Any],
    *,
    intents: FakeIntents | None = None,
    submit: FakeSubmit | None = None,
    clock: FakeClock | None = None,
    policy: Any | None = None,
    settings: Any | None = None,
    participation: Any = None,
) -> tuple[Any, FakeIntents, FakeSubmit, FakeClock]:
    fake_intents = intents or FakeIntents()
    fake_submit = submit or FakeSubmit()
    fake_clock = clock or FakeClock()
    adviser = night.NightChargeAdviser(
        settings=settings or make_settings(night),
        policy=policy or make_policy(api),
        clock=fake_clock,
        observations=FakeObservations(latest=observations),
        intents=fake_intents,
        submit=fake_submit,
        participation=participation,
    )
    return adviser, fake_intents, fake_submit, fake_clock


def claimed_intent(
    *,
    source: str = "MANUAL",
    unit_ids: tuple[str, ...] = ("mid",),
    intent_id: str = "claim-1",
    expires_at_mono: float = 190.0,
) -> Any:
    from energypod.domain import IntentSource

    return SimpleNamespace(
        id=intent_id,
        source=getattr(IntentSource, source),
        selected_unit_ids=frozenset(unit_ids),
        expires_at_mono=expires_at_mono,
    )


def targets(decision: Any) -> dict[str, int]:
    """The decision's participating unit -> whole-watt target map."""
    return {
        plan.unit_id: plan.target_w
        for plan in decision.unit_plans
        if plan.phase in ("pacing", "holding_on_demand")
    }


# --- the pure civil-time helpers (§2.3) -----------------------------------------


def test_in_window_is_civil_containment_including_cross_midnight(night: Any) -> None:
    from datetime import time

    windows = ((time(0, 0), time(6, 0)),)
    inside = datetime(2026, 8, 27, 0, 0, tzinfo=ZONE)
    edge = datetime(2026, 8, 27, 5, 59, tzinfo=ZONE)
    outside = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    assert night.in_window(inside, windows) is True
    assert night.in_window(edge, windows) is True
    assert night.in_window(outside, windows) is False, "window end is exclusive"

    cross = ((time(22, 0), time(4, 0)),)
    assert night.in_window(datetime(2026, 8, 27, 23, 30, tzinfo=ZONE), cross) is True
    assert night.in_window(datetime(2026, 8, 27, 3, 59, tzinfo=ZONE), cross) is True
    assert night.in_window(datetime(2026, 8, 27, 4, 0, tzinfo=ZONE), cross) is False
    assert night.in_window(datetime(2026, 8, 27, 12, 0, tzinfo=ZONE), cross) is False


def test_next_window_start_and_open_window_end_are_dst_honest(night: Any) -> None:
    from datetime import time

    windows = ((time(0, 0), time(6, 0)),)
    evening = datetime(2026, 8, 27, 22, 0, tzinfo=ZONE)
    starts = night.next_window_start(evening, windows, ZONE)
    assert starts == datetime(2026, 8, 28, 0, 0, tzinfo=ZONE)
    assert night.open_window_end(evening, windows, ZONE) is None

    mid_window = datetime(2026, 8, 27, 1, 31, tzinfo=ZONE)
    ends = night.open_window_end(mid_window, windows, ZONE)
    assert ends == datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    assert night.next_window_start(mid_window, windows, ZONE) == datetime(
        2026, 8, 28, 0, 0, tzinfo=ZONE
    )


# --- the charge plan: pacing math under both rules (§2.2) -----------------------


async def test_cap_first_paces_at_min_of_cap_dynamic_and_static(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    adviser, _, submit, _ = make_adviser(
        night, api, fleet, settings=make_settings(night, pacing="cap_first")
    )

    decision = await adviser.tick()

    assert decision.phase == "pacing"
    # rhs sits above the 95% ceiling: zero-watt non-participation, never in
    # the submitted unit set ("batteries already at the ceiling sit out").
    assert targets(decision) == {"lhs": 2_500, "mid": 2_500}
    assert {p.unit_id: p.phase for p in decision.unit_plans}["rhs"] == "skipped_full"
    assert {p.unit_id: p.reason for p in decision.unit_plans}["rhs"] == "at_ceiling"
    assert submit.submissions[0]["unit_ids"] == ["lhs", "mid"]
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 2_500, "mid": 2_500}
    assert submit.submissions[0]["direction"] == "charge"
    assert submit.submissions[0]["ttl_s"] == 10.0


async def test_cap_first_respects_the_dynamic_headroom(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    fleet["lhs"] = make_observation(
        api, unit_id="lhs", load_power_w=100.0, dynamic_charge_limit_w=700.0
    )
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert targets(decision)["lhs"] == 700


async def test_even_pacing_computes_the_deadline_rate_from_measured_soc(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    capacities = {unit: 5_000 for unit in UNIT_IDS}
    # Wall 01:00, window ends 06:00 -> 5 h remaining.
    adviser, _, submit, _ = make_adviser(
        night,
        api,
        fleet,
        settings=make_settings(night, pacing="even", assumed_capacity_wh=capacities),
    )

    decision = await adviser.tick()

    # lhs: ceil((95 - 71)/100 * 5000 * 3600 / 18000) = ceil(240) = 240 W.
    # mid: ceil((95 - 88)/100 * 5000 * 3600 / 18000) = ceil(70) = 70 W.
    assert targets(decision) == {"lhs": 240, "mid": 70}
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 240, "mid": 70}


async def test_even_pacing_self_corrects_after_a_demand_hold(night: Any, api: Any) -> None:
    """The design's own graduation property: the pause has already raised
    ``required_w`` — recomputed from MEASURED SOC every tick, so it converges
    to cap exactly when behind, with no separate escalation mode."""
    loads = {"lhs": 100.0, "mid": 100.0, "rhs": 100.0}
    fleet = make_fleet(api, loads)
    clock = FakeClock()
    adviser, _, submit, _ = make_adviser(
        night,
        api,
        fleet,
        clock=clock,
        settings=make_settings(
            night, pacing="even", assumed_capacity_wh={unit: 5_000 for unit in UNIT_IDS}
        ),
    )

    first = await adviser.tick()
    assert targets(first) == {"lhs": 240, "mid": 70}

    # The EV arrives mid-window: two hours of hold at the small positive rate.
    clock.wall = datetime(2026, 8, 27, 2, 0, tzinfo=ZONE)
    for unit in fleet.values():
        object.__setattr__(unit, "load_power_w", 600.0)
    held = await adviser.tick()
    assert decision_phase(held) == "holding_on_demand"
    assert targets(held) == {"lhs": 100, "mid": 100}

    # Demand falls back below the exit bound at 04:00: 2 h remain, and the
    # pause moved nothing (the hold is a charge, SOC still rises slowly), so
    # required_w is recomputed against the SHORTER remaining time.
    clock.wall = datetime(2026, 8, 27, 4, 0, tzinfo=ZONE)
    for unit in fleet.values():
        object.__setattr__(unit, "load_power_w", 100.0)
    resumed = await adviser.tick()
    assert decision_phase(resumed) == "pacing"
    assert "demand_below_exit" in resumed.reason_codes
    lhs_required = targets(resumed)["lhs"]
    assert lhs_required > 240, "the pause raised the deadline rate (self-correction)"

    # Behind enough, the rule converges to exactly the cap — no escalation mode.
    clock.wall = datetime(2026, 8, 27, 5, 55, tzinfo=ZONE)
    converged = await adviser.tick()
    assert targets(converged)["lhs"] == 2_500
    assert "deadline_at_risk" in converged.reason_codes


def decision_phase(decision: Any) -> str:
    return decision.phase


async def test_even_pacing_never_asks_below_one_watt_or_above_the_caps(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, socs={"mid": 94.9})
    adviser, _, submit, _ = make_adviser(
        night,
        api,
        fleet,
        clock=FakeClock(wall=datetime(2026, 8, 27, 5, 59, tzinfo=ZONE)),
        settings=make_settings(
            night, pacing="even", assumed_capacity_wh={unit: 5_000 for unit in UNIT_IDS}
        ),
    )

    decision = await adviser.tick()

    # lhs is far behind with 60 s left: the raw deadline rate (72 kW) clamps
    # to the cap and the tick says deadline_at_risk; mid one tenth of a
    # percent from the ceiling needs only 300 W — the deadline rule, not a
    # blanket cap.
    assert targets(decision) == {"lhs": 2_500, "mid": 300}
    assert "deadline_at_risk" in decision.reason_codes


# --- eligibility: the honest sit-outs (§2.2 step 1) -----------------------------


async def test_zero_dynamic_headroom_sits_out_with_the_bms_refusal(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, socs={"rhs": 80.0})
    fleet["rhs"] = make_observation(
        api, unit_id="rhs", load_power_w=100.0, dynamic_charge_limit_w=0.0, system_soc_pct=80.0
    )
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    by_unit = {plan.unit_id: plan for plan in decision.unit_plans}
    assert by_unit["rhs"].phase == "sitting_out"
    assert by_unit["rhs"].reason == "no_charge_headroom"
    assert "rhs" not in submit.submissions[0]["watts_by_unit"]


async def test_a_disarmed_fleet_renders_units_disarmed_and_submits_nothing(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    for unit in fleet:
        object.__setattr__(fleet[unit], "lifecycle", api.UnitLifecycle.DISARMED)
    adviser, intents, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert decision.phase == "idle"
    assert "units_disarmed" in decision.reason_codes
    assert submit.submissions == [], "the runner can never self-arm"
    assert intents.removed == []


async def test_units_disarmed_names_only_the_disarmed_units(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    object.__setattr__(fleet["mid"], "lifecycle", api.UnitLifecycle.DISARMED)
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    by_unit = {plan.unit_id: plan for plan in decision.unit_plans}
    assert by_unit["mid"].phase == "sitting_out"
    assert by_unit["mid"].reason == "units_disarmed"
    assert set(submit.submissions[0]["watts_by_unit"]) == {"lhs"}


# --- the demand rule: threshold, hold, hysteresis (§2.4) ------------------------


async def test_demand_above_threshold_holds_every_participating_unit(night: Any, api: Any) -> None:
    # 400 + 400 + 300 = 1100 W of house load: the EV class of demand.
    fleet = make_fleet(api, {"lhs": 400.0, "mid": 400.0, "rhs": 300.0})
    adviser, intents, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert decision.phase == "holding_on_demand"
    assert decision.demand_w == 1_100
    assert decision.demand_evidence == "good"
    assert "demand_above_threshold" in decision.reason_codes
    assert targets(decision) == {"lhs": 100, "mid": 100}
    # The hold objective REPLACES pod autonomy: a small POSITIVE charge, so
    # the battery neither discharges into the EV nor cycles.
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 100, "mid": 100}


async def test_the_hysteresis_band_never_flaps(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 600.0, "mid": 600.0, "rhs": 100.0})
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    held = await adviser.tick()
    assert decision_phase(held) == "holding_on_demand"

    # Demand falls INTO the band (1000 > 900 > 800): still held, no toggle.
    for unit, load in (("lhs", 500.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    band = await adviser.tick()
    assert decision_phase(band) == "holding_on_demand"
    assert band.demand_w == 900

    # Below the exit bound (threshold - hysteresis = 800): pacing resumes.
    for unit, load in (("lhs", 350.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    resumed = await adviser.tick()
    assert decision_phase(resumed) == "pacing"
    assert "demand_below_exit" in resumed.reason_codes
    # An oscillation back to just-under the threshold does not re-hold: the
    # engage rule is strictly `> threshold`.
    for unit, load in (("lhs", 450.0), ("mid", 350.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    steady = await adviser.tick()
    assert decision_phase(steady) == "pacing"


async def test_per_phase_scope_holds_only_the_phase_showing_the_demand(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 1_200.0, "mid": 300.0, "rhs": 300.0})
    adviser, _, submit, _ = make_adviser(
        night, api, fleet, settings=make_settings(night, demand_scope="per_phase")
    )

    decision = await adviser.tick()

    assert decision.demand_w == 1_800
    assert targets(decision) == {"lhs": 100, "mid": 2_500}
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 100, "mid": 2_500}


@pytest.mark.parametrize(
    ("loads", "word"),
    [
        ({"lhs": 100.0, "mid": 100.0, "rhs": None}, "missing"),
        ({"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, "bad"),
        ({"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, "stale"),
    ],
)
async def test_bad_evidence_fails_closed_to_hold(
    night: Any, api: Any, loads: Mapping[str, float | None], word: str
) -> None:
    """§2.4's pinned polarity: there bad evidence must stop a charge; HERE
    bad evidence must PRESERVE the no-cycling guarantee — charging blind into
    an EV at 7 kW is the exact outcome the operator refused."""
    overrides: dict[str, Any] = {}
    if word == "bad":
        overrides["rhs__load_quality"] = api.DataQuality.BAD
    if word == "stale":
        overrides["rhs__captured_at_mono"] = NOW - 5.0
    fleet = make_fleet(api, loads, **overrides)
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert decision.demand_evidence == word
    assert decision.demand_w is None, "a non-good rollup never serves a figure"
    assert decision.phase == "holding_on_demand"
    assert f"demand_evidence_{word}" in decision.reason_codes
    assert targets(decision) == {"lhs": 100, "mid": 100}, "fail-closed to HOLD"


async def test_a_unit_with_no_observation_at_all_is_missing_evidence(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0})
    adviser, _, _, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert decision.demand_evidence == "missing"
    assert decision.phase == "holding_on_demand"


async def test_the_demand_rule_reads_the_load_words_never_the_grid_words(
    night: Any, api: Any
) -> None:
    """THE DESIGN'S ONE NON-OBVIOUS CORRECTNESS CATCH, pinned by name.

    The grid word includes the adviser's OWN charging draw: when three pods
    charge at 2,500 W the grid word reads ~-7,500 W of import while the load
    CTs do not move.  A demand rule on the grid word would read that as
    7.5 kW of "demand" and hold forever — the feature could never charge.
    Here the fleet charges at FULL RATE with the grid words screaming import
    and the rule must keep pacing; a modest real house load on the load words
    alone must engage the hold.
    """
    charging = make_fleet(
        api,
        {"lhs": 90.0, "mid": 90.0, "rhs": 90.0},
        grids={"lhs": -2_500.0, "mid": -2_500.0, "rhs": -2_500.0},
    )
    adviser, _, submit, _ = make_adviser(night, api, charging)

    full_rate = await adviser.tick()

    assert full_rate.phase == "pacing", "our own 7.5 kW draw is not house demand"
    assert full_rate.demand_w == 270
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 2_500, "mid": 2_500}

    ev_night = make_fleet(
        api,
        {"lhs": 500.0, "mid": 500.0, "rhs": 300.0},
        grids={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
    )
    adviser_two, _, submit_two, _ = make_adviser(night, api, ev_night)
    held = await adviser_two.tick()
    assert held.phase == "holding_on_demand"
    assert held.demand_w == 1_300
    assert submit_two.submissions[0]["watts_by_unit"] == {"lhs": 100, "mid": 100}


# --- the demand postures: standby vs hold (§2.4, the operator's choice) ----------


async def test_standby_stands_measured_demand_holds_down_to_non_participation(
    night: Any, api: Any
) -> None:
    """The operator's preferred posture: MEASURED demand above the line
    stands every participating unit down entirely — zero-watt
    non-participation, never a zero-watt submission (the facade refuses
    those) — so nothing is submitted and the fleet phase names the
    stand-by."""
    # 400 + 400 + 300 = 1100 W of house load: the EV class of demand.
    fleet = make_fleet(api, {"lhs": 400.0, "mid": 400.0, "rhs": 300.0})
    adviser, intents, submit, _ = make_adviser(
        night, api, fleet, settings=make_settings(night, demand_response="standby")
    )

    decision = await adviser.tick()

    assert decision.phase == "standing_by_on_demand"
    assert decision.demand_w == 1_100
    assert decision.demand_evidence == "good"
    assert decision.reason_codes == ("window_open", "demand_above_threshold")
    assert decision.active_unit_ids == ()
    by_unit = {plan.unit_id: plan for plan in decision.unit_plans}
    assert by_unit["lhs"].phase == "standing_by_on_demand"
    assert by_unit["lhs"].target_w == 0
    assert by_unit["lhs"].reason == "demand_above_threshold"
    assert submit.submissions == [], "zero-watt non-participation, never a submission"
    assert intents.removed == []


async def test_standby_withdraws_the_held_intent_and_resumes_on_hysteresis(
    night: Any, api: Any
) -> None:
    """The full standby arc: pacing -> the EV arrives (the held intent is
    WITHDRAWN, the TTL lapse plus watchdog hand the pods back) -> demand in
    the hysteresis band keeps the stand-by (no flapping) -> below the exit
    bound the units rejoin the submission and pacing resumes."""
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    adviser, intents, submit, _ = make_adviser(
        night, api, fleet, settings=make_settings(night, demand_response="standby")
    )

    pacing = await adviser.tick()
    assert pacing.phase == "pacing"
    held_id = adviser.held_intent_id

    # The EV arrives mid-window: 1100 W measured.
    for unit, load in (("lhs", 400.0), ("mid", 400.0), ("rhs", 300.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    stood_down = await adviser.tick()

    assert stood_down.phase == "standing_by_on_demand"
    assert stood_down.action == "withdraw", "the charge intent lapses by design"
    assert held_id in intents.removed
    assert adviser.held_intent_id is None
    assert submit.submissions[-1] is submit.submissions[0], "no renewal while stood down"

    # Demand falls INTO the band (1000 > 900 > 800): still stood down.
    for unit, load in (("lhs", 500.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    band = await adviser.tick()
    assert band.phase == "standing_by_on_demand"
    assert band.demand_w == 900
    assert len(submit.submissions) == 1

    # Below the exit bound (800): the units rejoin and pacing resumes.
    for unit, load in (("lhs", 350.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    resumed = await adviser.tick()

    assert resumed.phase == "pacing"
    assert "demand_below_exit" in resumed.reason_codes
    assert set(resumed.active_unit_ids) == {"lhs", "mid"}
    assert submit.submissions[-1]["watts_by_unit"] == {"lhs": 2_500, "mid": 2_500}


async def test_standby_ends_at_the_window_boundary_and_reopens_fresh(
    night: Any, api: Any
) -> None:
    """The stand-by lasts only to the window's end (non-renewal, the pods'
    own autonomy until the next window), and the hold latch does not carry
    into the next window even if demand stays high."""
    fleet = make_fleet(api, {"lhs": 400.0, "mid": 400.0, "rhs": 300.0})
    clock = FakeClock()
    adviser, intents, submit, _ = make_adviser(
        night,
        api,
        fleet,
        clock=clock,
        settings=make_settings(night, demand_response="standby"),
    )

    stood_down = await adviser.tick()
    assert stood_down.phase == "standing_by_on_demand"

    clock.wall = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    ended = await adviser.tick()
    assert ended.action == "idle", "nothing was held while stood down"
    assert ended.phase == "idle"
    assert ended.reason_codes == ("outside_window",)

    # The next window opens onto FRESH demand below the engage line: pacing
    # immediately, the previous window's stand-by latch reset at the boundary.
    clock.wall = datetime(2026, 8, 28, 0, 30, tzinfo=ZONE)
    for unit, load in (("lhs", 300.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    fresh = await adviser.tick()

    assert fresh.phase == "pacing"
    assert fresh.reason_codes == ("window_open", "on_plan")
    assert submit.submissions[-1]["watts_by_unit"] == {"lhs": 2_500, "mid": 2_500}


@pytest.mark.parametrize(
    ("loads", "word"),
    [
        ({"lhs": 100.0, "mid": 100.0, "rhs": None}, "missing"),
        ({"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, "bad"),
        ({"lhs": 100.0, "mid": 100.0, "rhs": 100.0}, "stale"),
    ],
)
async def test_standby_fails_closed_to_the_positive_hold(
    night: Any, api: Any, loads: Mapping[str, float | None], word: str
) -> None:
    """THE POSTURE-INVARIANT FAIL-CLOSED PIN: standby is a response to
    MEASURED demand, never to missing data.  A non-good word keeps (or
    engages) the HOLD at hold_rate_w — the renewed objective preserves the
    no-cycling guarantee under BOTH postures."""
    overrides: dict[str, Any] = {}
    if word == "bad":
        overrides["rhs__load_quality"] = api.DataQuality.BAD
    if word == "stale":
        overrides["rhs__captured_at_mono"] = NOW - 5.0
    fleet = make_fleet(api, loads, **overrides)
    adviser, _, submit, _ = make_adviser(
        night, api, fleet, settings=make_settings(night, demand_response="standby")
    )

    decision = await adviser.tick()

    assert decision.demand_evidence == word
    assert decision.demand_w is None
    assert decision.phase == "holding_on_demand", "fail-closed holds, never stands by"
    assert f"demand_evidence_{word}" in decision.reason_codes
    assert targets(decision) == {"lhs": 100, "mid": 100}
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 100, "mid": 100}


async def test_standby_per_phase_stands_down_only_the_phase_showing_demand(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 1_200.0, "mid": 300.0, "rhs": 300.0})
    adviser, _, submit, _ = make_adviser(
        night,
        api,
        fleet,
        settings=make_settings(night, demand_response="standby", demand_scope="per_phase"),
    )

    decision = await adviser.tick()

    assert decision.demand_w == 1_800
    assert decision.phase == "standing_by_on_demand"
    by_unit = {plan.unit_id: plan for plan in decision.unit_plans}
    assert by_unit["lhs"].phase == "standing_by_on_demand"
    # The clean phase keeps charging; the demanded phase is excluded from the
    # submission entirely (zero-watt non-participation).
    assert submit.submissions[0]["watts_by_unit"] == {"mid": 2_500}


async def test_standby_per_phase_fails_closed_on_its_own_bad_word(
    night: Any, api: Any
) -> None:
    """A phase whose OWN word went non-good holds at the positive rate (fail
    closed) even under the standby posture, while the clean phase paces."""
    fleet = make_fleet(api, {"lhs": 1_200.0, "mid": 300.0, "rhs": 300.0})
    tainted = {**fleet["lhs"].quality, "load_power_w": api.DataQuality.BAD}
    object.__setattr__(fleet["lhs"], "quality", tainted)
    adviser, _, submit, _ = make_adviser(
        night,
        api,
        fleet,
        settings=make_settings(night, demand_response="standby", demand_scope="per_phase"),
    )

    decision = await adviser.tick()

    assert decision.phase == "holding_on_demand"
    assert "demand_evidence_bad" in decision.reason_codes
    assert submit.submissions[0]["watts_by_unit"] == {"lhs": 100, "mid": 2_500}


# --- precedence: claims and the dawn corner (§4) --------------------------------


@pytest.mark.parametrize("source", ["MANUAL", "AGENT", "SCHEDULE"])
async def test_every_claim_class_excludes_only_its_units_at_submission(
    night: Any, api: Any, source: str
) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    intents = FakeIntents(entries=[claimed_intent(source=source, unit_ids=("mid",))])
    adviser, _, submit, _ = make_adviser(night, api, fleet, intents=intents)

    decision = await adviser.tick()

    by_unit = {plan.unit_id: plan for plan in decision.unit_plans}
    assert by_unit["mid"].phase == "sitting_out"
    assert by_unit["mid"].reason == "yielding_to_higher_priority"
    # rhs sits above the ceiling regardless (skipped_full), so the claim on
    # mid leaves exactly lhs charging.
    assert set(submit.submissions[0]["watts_by_unit"]) == {"lhs"}
    assert decision.phase == "pacing"


async def test_a_not_own_optimizer_intent_excludes_its_units_the_dawn_corner(
    night: Any, api: Any
) -> None:
    """§4.2: FREE surplus outranks PAID import — a live OPTIMIZER intent that
    is not ours (the excess adviser, identified by claim at tick time under
    the pinned ordering) excludes its units from this tick's submission, and
    the exclusion keeps the equal-priority tie deterministic (no
    newest-revision flap)."""
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    intents = FakeIntents(
        entries=[claimed_intent(source="OPTIMIZER", unit_ids=("mid",), intent_id="opt-77")]
    )
    adviser, _, submit, _ = make_adviser(night, api, fleet, intents=intents)

    await adviser.tick()

    assert set(submit.submissions[0]["watts_by_unit"]) == {"lhs"}


async def test_the_advisers_own_held_intent_does_not_exclude_itself(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    intents = FakeIntents()
    adviser, intents, submit, _ = make_adviser(night, api, fleet, intents=intents)

    await adviser.tick()
    # The renewal tick sees its OWN held intent in the active set (remove-
    # then-submit): the pinned ``night-`` prefix means it must not treat
    # itself as a foreign optimizer claim.
    intents.entries.append(
        claimed_intent(
            source="OPTIMIZER",
            unit_ids=("lhs", "mid"),
            intent_id=adviser.held_intent_id or "night-1",
        )
    )
    second = await adviser.tick()

    assert set(submit.submissions[-1]["watts_by_unit"]) == {"lhs", "mid"}
    assert second.phase == "pacing"


async def test_a_live_emergency_stop_withdraws_entirely(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    stop = claimed_intent(source="EMERGENCY_STOP", unit_ids=UNIT_IDS, intent_id="stop-1")
    intents = FakeIntents()
    submit = FakeSubmit()
    adviser, intents, submit, _ = make_adviser(night, api, fleet, intents=intents, submit=submit)

    first = await adviser.tick()
    assert first.phase == "pacing"
    held_id = adviser.held_intent_id
    intents.entries.append(stop)

    withdrawn = await adviser.tick()

    assert withdrawn.action == "withdraw"
    assert held_id in intents.removed
    assert submit.submissions[-1] is submit.submissions[0], "no renewal under a stop"
    assert "yielding_to_higher_priority" in withdrawn.reason_codes


# --- the held-intent invariant and window end (§2.5) ----------------------------


async def test_the_one_held_intent_invariant(night: Any, api: Any) -> None:
    """THE DESIGN'S SECOND NAMED PIN: exactly one live ``night-`` intent,
    ever.  Every renewal removes the previous submission before the fresh
    one; a no-participant tick removes-if-held and submits nothing; and the
    store's live set never holds two night intents at once."""
    fleet = make_fleet(api, {"lhs": 400.0, "mid": 400.0, "rhs": 300.0})
    intents = FakeIntents()
    adviser, intents, submit, _ = make_adviser(night, api, fleet, intents=intents)

    await adviser.tick()  # propose under the hold
    first_id = adviser.held_intent_id
    assert first_id == "night-1"

    await adviser.tick()  # renew under the hold
    assert adviser.held_intent_id == "night-2"
    assert intents.removed == [first_id], "remove-then-submit, never two live"

    # Every submitted id (except any already removed) is at most one.
    live: set[str] = set()
    for submission_index in range(len(submit.submissions)):
        submitted = f"night-{submission_index + 1}"
        live.discard(intents.removed[submission_index - 1]) if submission_index else None
        live.add(submitted)
        assert len(live) == 1

    # Demand falls away, the window completes (all at the ceiling): removal,
    # and no idle/zero-watt submission ever.
    for unit in fleet.values():
        object.__setattr__(unit, "bms_soc_pct", 95.0)
        object.__setattr__(unit, "system_soc_pct", 95.0)
    complete = await adviser.tick()

    assert complete.action == "withdraw"
    assert adviser.held_intent_id is None
    assert submit.submissions[-1]["watts_by_unit"] != {}, "never a zero-watt intent"
    assert all(submission["watts_by_unit"] for submission in submit.submissions), (
        "no idle submissions ever"
    )


async def test_window_end_is_non_renewal(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    clock = FakeClock()
    intents = FakeIntents()
    adviser, intents, submit, _ = make_adviser(night, api, fleet, intents=intents, clock=clock)

    pacing = await adviser.tick()
    assert pacing.phase == "pacing"
    held_id = adviser.held_intent_id

    clock.wall = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    ended = await adviser.tick()

    assert ended.action == "withdraw"
    assert ended.phase == "idle"
    assert ended.reason_codes == ("outside_window",)
    assert adviser.held_intent_id is None
    assert held_id in intents.removed
    assert len(submit.submissions) == 1, "no stop triple, no idle intent, ever"
    assert not any(plan.phase in ("pacing", "holding_on_demand") for plan in ended.unit_plans)


async def test_completion_before_window_end_idles_with_target_reached(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 100.0, "mid": 100.0, "rhs": 100.0})
    intents = FakeIntents()
    adviser, intents, submit, _ = make_adviser(night, api, fleet, intents=intents)

    await adviser.tick()
    for unit in fleet.values():
        object.__setattr__(unit, "bms_soc_pct", 96.0)
        object.__setattr__(unit, "system_soc_pct", 96.0)

    complete = await adviser.tick()

    assert complete.phase == "complete"
    assert "target_reached" in complete.reason_codes
    assert targets(complete) == {}
    assert adviser.held_intent_id is None
    # Something charged this window: complete, not skipped_full.
    by_unit = {plan.unit_id: plan for plan in complete.unit_plans}
    assert by_unit["lhs"].phase == "complete"
    assert by_unit["lhs"].reason == "target_reached"


async def test_a_window_that_opens_with_everything_full_is_skipped_full(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(
        api,
        {"lhs": 100.0, "mid": 100.0, "rhs": 100.0},
        socs={"lhs": 96.0, "mid": 95.5, "rhs": 98.0},
    )
    adviser, _, submit, _ = make_adviser(night, api, fleet)

    decision = await adviser.tick()

    assert decision.phase == "skipped_full"
    assert "at_ceiling" in decision.reason_codes
    assert submit.submissions == []


async def test_the_hold_latch_resets_at_the_window_boundaries(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 600.0, "mid": 600.0, "rhs": 100.0})
    clock = FakeClock()
    adviser, _, _, _ = make_adviser(night, api, fleet, clock=clock)

    held = await adviser.tick()
    assert decision_phase(held) == "holding_on_demand"

    # Past the window end and back into the next night: the latch must not
    # carry the previous window's hold.
    clock.wall = datetime(2026, 8, 27, 12, 0, tzinfo=ZONE)
    await adviser.tick()
    clock.wall = datetime(2026, 8, 28, 0, 30, tzinfo=ZONE)
    for unit, load in (("lhs", 300.0), ("mid", 300.0), ("rhs", 100.0)):
        object.__setattr__(fleet[unit], "load_power_w", load)
    fresh = await adviser.tick()

    assert decision_phase(fresh) == "pacing", "700 W < 1000 W, latch reset at the boundary"


# --- participation and the projection controller (§5 mirror) ---------------------


def make_controller(night: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "pacing": "cap_first",
        "rate_cap_w": 2_500,
        "hold_rate_w": 100,
        "demand_scope": "fleet",
        "demand_response": "hold",
        "demand_threshold_w": 1_000,
        "windows": DEFAULT_WINDOW,
        "timezone": "Australia/Brisbane",
        "posture": "partition",
        "clock": FakeClock(),
        "acknowledged_partition": True,
        "config_enabled": False,
    }
    values.update(overrides)
    return night.NightChargeController(**values)


def test_the_controller_projects_the_participation_states() -> None:
    night_module = importlib.import_module("energypod.application.night_charge")
    controller = make_controller(night_module)

    assert controller.enabled is False
    assert controller.enabled_origin == "config"
    assert controller.participation_verdict() == "disabled_by_config"

    controller.set_participation(enabled=True)
    assert controller.participation_verdict() is None
    assert controller.enabled_origin == "runtime"

    controller.set_participation(enabled=False)
    assert controller.participation_verdict() == "disabled_by_runtime"

    unacknowledged = make_controller(
        night_module, config_enabled=True, acknowledged_partition=False
    )
    assert unacknowledged.participation_verdict() == "night_acknowledgement_required"


def test_the_state_payload_is_the_section_five_shape() -> None:
    night_module = importlib.import_module("energypod.application.night_charge")
    controller = make_controller(night_module)

    payload = controller.state_payload()

    assert set(payload) == {
        "enabled",
        "enabled_origin",
        "acknowledged_partition",
        "posture",
        "active",
        "phase",
        "window",
        "window_ends_at",
        "window_ends_in_s",
        "next_window_at",
        "pacing",
        "rate_cap_w",
        "hold_rate_w",
        "demand_scope",
        "demand_response",
        "demand_threshold_w",
        "demand_w",
        "demand_evidence",
        "held_intent_id",
        "units",
        "last_action",
        "last_tick_at",
        "reason_codes",
    }
    assert payload["enabled"] is False
    assert payload["posture"] == "partition"
    assert payload["demand_response"] == "hold", "the designed default posture"
    assert make_controller(night_module, demand_response="standby").state_payload()[
        "demand_response"
    ] == "standby"
    assert payload["window"] == {
        "start_local": "00:00",
        "end_local": "06:00",
        "timezone": "Australia/Brisbane",
    }
    assert payload["reason_codes"] == ["disabled_by_config"]
    assert payload["demand_w"] is None
    assert payload["demand_evidence"] == "missing"


def test_the_event_payload_is_the_projection_minus_tick_bookkeeping() -> None:
    night_module = importlib.import_module("energypod.application.night_charge")
    controller = make_controller(night_module)

    state = controller.state()
    event = state.event_payload()
    assert "last_action" not in event
    assert "last_tick_at" not in event
    # The §5 semantic tuple: participation facts, active, phase, the active
    # unit ids, the evidence word, the codes — never the watt/SOC figures.
    semantic = state.semantic_tuple()
    assert len(semantic) == 8
    assert semantic[0] is False  # enabled
    assert semantic[6] == "missing"  # demand_evidence
