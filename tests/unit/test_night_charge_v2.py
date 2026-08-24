"""Contract tests for the night charge adviser's V2 target (DESIGN_NIGHT_CHARGE
V2; the T-NC2 families: TARGET-MATH, SUGGEST, FALLBACK, RETARGET,
DEMAND-INTERPLAY, PROJECTION/EVENTS, ARCHITECTURE).

V2 changes WHAT the overnight charge aims at, never WHEN, never HOW FAST,
never UNDER WHOSE AUTHORITY: a per-battery target derived from the injected
``morning_credit_kwh`` port (the net surplus the morning after the window can
still put into the pack), bounded by the reserve floor below and the
unchanged charge ceiling above, computed at window open, revised mid-window
only on a materially changed forecast under the 2-per-window and 60-minute
caps derived from DURABLE rows, completion ONE-DIRECTIONAL (falls hold, rises
re-open -- A2), and -- below trust and on every forecast failure -- fallen
back to the v1 ceiling loudly with its own reason code.  Under
``forecast_suggest`` the submission math is byte-identical to v1 (a named
test); under ``full`` the whole module is v1 identity.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

NOW = 100.0
UNIT_IDS = ("lhs", "mid", "rhs")
ZONE = ZoneInfo("Australia/Brisbane")
WINDOW = ((time(0, 0), time(6, 0)),)
CAPACITIES = {"lhs": 5000, "mid": 5000, "rhs": 4200}  # the section 2.6 assumption


@pytest.fixture(scope="module")
def api() -> Any:
    domain = importlib.import_module("energypod.domain")
    for name in ("ControlPolicy", "DataQuality", "Observation", "UnitLifecycle"):
        assert hasattr(domain, name), f"public domain contract missing: {name}"
    return domain


@pytest.fixture(scope="module")
def night() -> Any:
    module = importlib.import_module("energypod.application.night_charge")
    for name in (
        "NightChargeAdviser",
        "NightChargeController",
        "NightChargeSettings",
        "MorningCredit",
        "REASON_FORECAST_MISSING",
        "REASON_FORECAST_STALE",
        "REASON_FORECAST_NO_LOAD_BASELINE",
        "REASON_FORECAST_BELOW_TRUST",
        "REASON_WINDOW_CLOSED_BELOW_TARGET",
    ):
        assert hasattr(module, name), f"night V2 contract incomplete: {name}"
    return module


# --- the shared fakes (the v1 file's shapes, extended) ------------------------------


@dataclass
class FakeClock:
    now: float = NOW
    wall: datetime = datetime(2026, 8, 27, 1, 0, 0, tzinfo=ZONE)

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.now


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


@dataclass
class FakeAudit:
    appended: list[Any] = field(default_factory=list)

    async def append(self, event: Any) -> None:
        self.appended.append(event)


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


def make_observation(
    api: Any,
    *,
    unit_id: str,
    load_power_w: float | None,
    system_soc_pct: float = 50.0,
    dynamic_charge_limit_w: float = 2_500.0,
    captured_at_mono: float = NOW,
) -> Any:
    quality = {name: api.DataQuality.GOOD for name in QUALITY_FIELDS}
    return api.Observation(
        unit_id=unit_id,
        wall_timestamp=datetime(2026, 8, 26, tzinfo=UTC),
        captured_at_mono=captured_at_mono,
        sequence=7,
        lifecycle=api.UnitLifecycle.ARMED_IDLE,
        protocol_profile="iot",
        system_soc_pct=system_soc_pct,
        bms_soc_pct=system_soc_pct,
        soh_pct=98.0,
        battery_watts=0.0,
        pack_voltage_v=400.0,
        pack_current_a=0.0,
        dynamic_charge_limit_w=dynamic_charge_limit_w,
        dynamic_discharge_limit_w=2_500.0,
        grid_power_w=0.0,
        load_power_w=load_power_w,
        cell_voltages_v=(3.30, 3.31, 3.29, 3.30),
        cell_captured_at_mono=captured_at_mono,
        cell_sequence=4,
        temperatures_c=(24.0, 25.0, 26.0, 25.0),
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality=quality,
    )


def make_fleet(api: Any, socs: dict[str, float], loads: float = 100.0) -> dict[str, Any]:
    return {
        unit: make_observation(api, unit_id=unit, load_power_w=loads, system_soc_pct=soc)
        for unit, soc in socs.items()
    }


# --- the morning-credit fakes --------------------------------------------------------


def _credit(
    night: Any,
    *,
    e_surplus_kwh: float = 6.0,
    e_deficit_kwh: float = 0.5,
    fetched_at: datetime = datetime(2026, 8, 26, 22, 0, tzinfo=UTC),
    issued_at: datetime | None = datetime(2026, 8, 26, 21, 30, tzinfo=UTC),
    quantile: float | None = 0.1,
    failure: str | None = None,
) -> Any:
    return night.MorningCredit(
        e_surplus_kwh=e_surplus_kwh,
        e_deficit_kwh=e_deficit_kwh,
        slots=(),
        source="solcast",
        quantile=quantile,
        fetched_at=fetched_at,
        issued_at=issued_at,
        failure=failure,
    )


class FakeCreditPort:
    """A scripted morning-credit port: one credit per call, popped in order."""

    def __init__(self, credits: list[Any] | None = None, *, failure: Exception | None = None):
        self.credits = list(credits or [])
        self.failure = failure
        self.calls = 0

    async def __call__(self, window_end: datetime, midday_local: time, quantile: float) -> Any:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if not self.credits:
            raise AssertionError("the scripted credit port ran out")
        return self.credits.pop(0)


@dataclass
class RevisionLedger:
    """The durable retarget-history port fake (window_date -> instants)."""

    revisions: dict[date, list[datetime]] = field(default_factory=dict)

    def record(self, day: date, instant: datetime) -> None:
        self.revisions.setdefault(day, []).append(instant)

    def history(self, day: date) -> tuple[datetime, ...]:
        return tuple(sorted(self.revisions.get(day, ())))


def make_settings(night: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "rate_cap_w": 2_500,
        "hold_rate_w": 100,
        "demand_threshold_w": 1_000,
        "demand_exit_hysteresis_w": 200,
        "demand_scope": "fleet",
        "pacing": "cap_first",
        "assumed_capacity_wh": None,
        "demand_telemetry_max_age_s": 3.0,
        "intent_ttl_s": 10.0,
        "windows": WINDOW,
        "timezone": "Australia/Brisbane",
        "unit_ids": UNIT_IDS,
        "target_policy": "forecast_act",
    }
    values.update(overrides)
    return night.NightChargeSettings(**values)


def make_adviser(
    night: Any,
    api: Any,
    observations: dict[str, Any],
    *,
    settings: Any = None,
    credit: Any = None,
    trust: str = "earned",
    audit: Any = None,
    revisions: RevisionLedger | None = None,
    clock: FakeClock | None = None,
    submit: FakeSubmit | None = None,
) -> tuple[Any, FakeSubmit, FakeClock, Any, RevisionLedger]:
    fake_submit = submit or FakeSubmit()
    fake_clock = clock or FakeClock()
    ledger = revisions or RevisionLedger()
    fake_audit = audit or FakeAudit()
    adviser = night.NightChargeAdviser(
        settings=settings
        or make_settings(night, assumed_capacity_wh=dict(CAPACITIES)),
        policy=make_policy(api),
        clock=fake_clock,
        observations=FakeObservations(latest=observations),
        intents=FakeIntents(),
        submit=fake_submit,
        morning_credit=credit,
        trust_state=lambda: trust,
        audit=fake_audit,
        revision_sink=ledger.record,
        retarget_history=ledger.history,
    )
    return adviser, fake_submit, fake_clock, fake_audit, ledger


def targets(decision: Any) -> dict[str, int]:
    return {
        plan.unit_id: plan.target_w
        for plan in decision.unit_plans
        if plan.phase in ("pacing", "holding_on_demand")
    }


# --- T-NC2-ARCHITECTURE --------------------------------------------------------------


def test_the_adviser_consumes_the_injected_port_and_never_imports_providers() -> None:
    """Section 2.7's pin: the adviser lives in application/ and reaches the
    forecast ONLY through the injected ``morning_credit_kwh`` port -- no
    application module may import adapters.providers (the fitness rule,
    extended to the new consumer)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "energypod"
    for path in sorted((root / "application").rglob("*.py")):
        assert "adapters.providers" not in path.read_text(encoding="utf-8"), path


async def test_a_provider_failure_is_no_credit_is_fallback_never_a_crash(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0})
    adviser, submit, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort(failure=RuntimeError("provider unavailable"))
    )

    decision = await adviser.tick()

    assert decision.fallback_reason == night.REASON_FORECAST_MISSING
    assert night.REASON_FORECAST_MISSING in decision.reason_codes
    # Every failure of foresight buys MORE off-peak energy: v1 ceiling targets.
    assert targets(decision) == {"lhs": 2_500, "mid": 2_500, "rhs": 2_500}


# --- T-NC2-TARGET-MATH ----------------------------------------------------------------


async def test_the_target_collapses_to_one_fleet_percentage(night: Any, api: Any) -> None:
    """Section 2.2: capacity-proportional share makes every battery's target
    the SAME percentage -- rhs's smaller pack is not handed a deeper one."""
    fleet = make_fleet(api, {"lhs": 40.0, "mid": 40.0, "rhs": 40.0})
    # eta 0.9 x 6.0 surplus - 0.5 deficit = 4.9 kWh credit over 14.2 kWh of
    # fleet capacity: raw = 100 - 100*4.9/14.2 = 65.49...%
    adviser, _, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort([_credit(night)])
    )

    decision = await adviser.tick()

    expected = 100.0 - 100.0 * (0.9 * 6.0 - 0.5) * 1000.0 / sum(CAPACITIES.values())
    assert decision.target_soc_pct == pytest.approx(expected, abs=1e-6)
    per_unit = {plan.unit_id: plan.target_soc_pct for plan in decision.unit_plans}
    assert all(value == pytest.approx(expected, abs=1e-6) for value in per_unit.values())
    assert decision.forecast is not None
    assert decision.forecast["ceiling_bound_by"] is None


async def test_zero_forecast_targets_the_ceiling_by_sky(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 40.0, "mid": 40.0, "rhs": 40.0})
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([_credit(night, e_surplus_kwh=0.0, e_deficit_kwh=0.0)]),
    )

    decision = await adviser.tick()

    assert decision.target_soc_pct == pytest.approx(95.0)
    assert decision.forecast is not None
    assert decision.forecast["ceiling_bound_by"] == "sky"


async def test_real_surplus_eaten_by_the_deficit_binds_by_netting(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 40.0, "mid": 40.0, "rhs": 40.0})
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([_credit(night, e_surplus_kwh=4.0, e_deficit_kwh=4.0)]),
    )

    decision = await adviser.tick()

    assert decision.target_soc_pct == pytest.approx(95.0)
    assert decision.forecast is not None
    assert decision.forecast["ceiling_bound_by"] == "netting", (
        "a real surplus the netting consumed, not a cloudless-sky absence"
    )


async def test_a_huge_forecast_targets_the_floor(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 80.0, "mid": 80.0, "rhs": 80.0})
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([_credit(night, e_surplus_kwh=30.0, e_deficit_kwh=0.0)]),
    )

    decision = await adviser.tick()

    assert decision.target_soc_pct == pytest.approx(50.0), "the reserve floor clamps"
    assert decision.forecast is not None
    assert decision.forecast["ceiling_bound_by"] is None


async def test_an_understated_capacity_map_under_charges_the_named_hazard(
    night: Any, api: Any
) -> None:
    """Section 2.6's direction pin: an understated capacity overstates the
    headroom percentage and UNDER-charges -- the wrong way."""
    fleet = make_fleet(api, {"lhs": 40.0, "mid": 40.0, "rhs": 40.0})
    honest, _, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort([_credit(night)])
    )
    understated, _, _, _, _ = make_adviser(
        night,
        api,
        dict(fleet),
        settings=make_settings(
            night, assumed_capacity_wh={unit: wh // 2 for unit, wh in CAPACITIES.items()}
        ),
        credit=FakeCreditPort([_credit(night)]),
    )

    honest_decision = await honest.tick()
    understated_decision = await understated.tick()

    assert understated_decision.target_soc_pct < honest_decision.target_soc_pct, (
        "the same kWh over half the Wh doubles the headroom percentage: under-charge"
    )


async def test_soc_above_target_is_complete_never_pacing(night: Any, api: Any) -> None:
    """A8's own example: a unit sitting ABOVE the target renders
    complete/target_reached; the target narrows who paces."""
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 68.9, "rhs": 60.0})
    adviser, submit, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort([_credit(night)])
    )

    decision = await adviser.tick()

    phases = {plan.unit_id: plan.phase for plan in decision.unit_plans}
    reasons = {plan.unit_id: plan.reason for plan in decision.unit_plans}
    expected = 100.0 - 100.0 * (0.9 * 6.0 - 0.5) * 1000.0 / sum(CAPACITIES.values())
    assert expected == pytest.approx(65.49, abs=0.2)
    assert phases["mid"] == "complete"
    assert reasons["mid"] == "target_reached"
    assert set(targets(decision)) == {"lhs", "rhs"}, "mid sits above the target: it sits out"
    assert submit.submissions[0]["unit_ids"] == ["lhs", "rhs"]


async def test_completion_is_one_directional_falls_hold_rises_reopen(
    night: Any, api: Any
) -> None:
    """A2: a completed unit never rejoins because the sky improved; a cloudier
    revision that lifts the target above its MEASURED SOC re-opens it."""
    fleet = make_fleet(api, {"lhs": 66.0, "mid": 66.0, "rhs": 66.0})
    clock = FakeClock()
    first = _credit(night, e_surplus_kwh=6.0, e_deficit_kwh=0.5)
    sunnier = _credit(
        night,
        e_surplus_kwh=8.0,
        e_deficit_kwh=0.0,
        fetched_at=datetime(2026, 8, 26, 23, 30, tzinfo=UTC),
    )
    cloudier = _credit(
        night,
        e_surplus_kwh=2.0,
        e_deficit_kwh=1.0,
        fetched_at=datetime(2026, 8, 27, 0, 30, tzinfo=UTC),
    )
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([first, sunnier, cloudier]),
        clock=clock,
    )

    opened = await adviser.tick()
    assert opened.target_soc_pct == pytest.approx(65.49, abs=0.2)
    # Every unit already sits above the 65.49% target: complete at open
    # (A8's comparison), never pacing.
    phases = {plan.unit_id: plan.phase for plan in opened.unit_plans}
    assert set(phases.values()) == {"complete"}

    # The sunnier revision LOWERS the target to the floor: completion is
    # sticky against the fall (the energy is bought; un-charging is not a
    # thing).
    clock.wall = datetime(2026, 8, 27, 2, 0, tzinfo=ZONE)
    revised_down = await adviser.tick()
    assert revised_down.target_soc_pct < opened.target_soc_pct
    phases = {plan.unit_id: plan.phase for plan in revised_down.unit_plans}
    assert set(phases.values()) == {"complete"}, "a fall never un-completes a unit"

    # The cloudier revision RAISES the target above the measured SOC: the
    # completed units re-open and charge the gap (section 5.1's only
    # re-entry).
    clock.wall = datetime(2026, 8, 27, 3, 30, tzinfo=ZONE)
    revised_up = await adviser.tick()
    assert revised_up.target_soc_pct > 66.0, "the cloudier target stands above the SOC"
    phases = {plan.unit_id: plan.phase for plan in revised_up.unit_plans}
    assert phases["lhs"] == "pacing", "a rise above measured SOC re-opens the unit"


# --- T-NC2-SUGGEST ---------------------------------------------------------------------


async def test_suggests_intent_stream_is_byte_identical_to_full(night: Any, api: Any) -> None:
    """The named test: under forecast_suggest the submission math is v1's --
    every submitted unit set, watt target, and TTL identical to the full
    posture over the same scenario, while the projection carries the
    suggested fields."""
    socs = {"lhs": 71.0, "mid": 88.0, "rhs": 98.0}

    async def run(policy: str) -> tuple[Any, Any]:
        adviser, submit, _, _, _ = make_adviser(
            night,
            api,
            make_fleet(api, socs),
            settings=make_settings(
                night, target_policy=policy, pacing="even", assumed_capacity_wh=dict(CAPACITIES)
            ),
            credit=FakeCreditPort([_credit(night)]),
            trust="provisioning",
        )
        decision = await adviser.tick()
        return decision, submit

    full_decision, full_submit = await run("full")
    suggest_decision, suggest_submit = await run("forecast_suggest")

    assert full_submit.submissions == suggest_submit.submissions, (
        "the suggest intent stream is byte-identical to v1's"
    )
    assert [tuple(p.target_w for p in full_decision.unit_plans)] == [
        tuple(p.target_w for p in suggest_decision.unit_plans)
    ], "identical per-unit watt targets, not just identical submissions"
    # The DISPLAY differs: the suggested target rides every unit row and the
    # forecast block, and the completion word stays the behavioral v1 one.
    assert suggest_decision.forecast is not None
    suggested = {plan.unit_id: plan.target_soc_pct for plan in suggest_decision.unit_plans}
    assert all(value == pytest.approx(65.49, abs=0.2) for value in suggested.values())
    phases = {plan.unit_id: plan.phase for plan in suggest_decision.unit_plans}
    assert phases["rhs"] == "skipped_full", "the ceiling case keeps its own word"
    assert phases["lhs"] == "pacing", "the submission math is the v1 ceiling's"


# --- T-NC2-FALLBACK ---------------------------------------------------------------------


async def test_the_ladder_lands_on_v1_full_loudly(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0})
    for failure, expected_code in (
        ("forecast_missing", "REASON_FORECAST_MISSING"),
        ("forecast_stale", "REASON_FORECAST_STALE"),
        ("forecast_no_load_baseline", "REASON_FORECAST_NO_LOAD_BASELINE"),
        ("forecast_below_trust", "REASON_FORECAST_BELOW_TRUST"),
    ):
        adviser, _, _, _, _ = make_adviser(
            night, api, dict(fleet), credit=FakeCreditPort([_credit(night, failure=failure)])
        )
        decision = await adviser.tick()
        assert decision.fallback_reason == getattr(night, expected_code), failure
        assert decision.fallback_reason in decision.reason_codes
        assert decision.target_soc_pct is None, "v1 ceiling targets, not a forecast number"
        assert targets(decision) == {"lhs": 2_500, "mid": 2_500, "rhs": 2_500}


async def test_below_trust_is_the_act_postures_own_fallback(night: Any, api: Any) -> None:
    """Section 3.3 bullet 4 scoped to the governing path: an ACT site without
    earned trust runs the fallback (section 6: 'an unearned-trust ACT site
    simply runs the §3.3 fallback with forecast_below_trust'); a SUGGEST
    site with provisioning trust still DISPLAYS (it runs from day one)."""
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0})
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        dict(fleet),
        credit=FakeCreditPort([_credit(night)]),
        trust="provisioning",
    )
    decision = await adviser.tick()
    assert decision.fallback_reason == night.REASON_FORECAST_BELOW_TRUST

    suggest = night.NightChargeAdviser(
        settings=make_settings(night, target_policy="forecast_suggest",
                               assumed_capacity_wh=dict(CAPACITIES)),
        policy=make_policy(api),
        clock=FakeClock(),
        observations=FakeObservations(latest=dict(fleet)),
        intents=FakeIntents(),
        submit=FakeSubmit(),
        morning_credit=FakeCreditPort([_credit(night)]),
        trust_state=lambda: "provisioning",
    )
    suggest_decision = await suggest.tick()
    assert suggest_decision.fallback_reason is None
    assert suggest_decision.forecast is not None, "the suggest display runs from day one"


async def test_a_fallback_window_archives_nothing_to_score(night: Any, api: Any) -> None:
    """A12: full-posture days and §3.3 fallback nights archive nothing -- the
    trust ledger's exclusion, pinned at the writer."""
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0})
    audit = FakeAudit()
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([_credit(night, failure="forecast_stale")]),
        audit=audit,
    )
    await adviser.tick()
    assert [e for e in audit.appended if e.event_type == "night_target_set"] == []

    good_audit = FakeAudit()
    adviser, _, _, _, _ = make_adviser(
        night, api, dict(fleet), credit=FakeCreditPort([_credit(night)]), audit=good_audit
    )
    await adviser.tick()
    rows = [e for e in good_audit.appended if e.event_type == "night_target_set"]
    assert len(rows) == 1
    payload = dict(rows[0].payload or {})
    assert payload["e_surplus_forecast_kwh"] == pytest.approx(6.0)
    assert payload["target_policy"] == "forecast_act"
    assert payload["window_end_local"] == "06:00"
    assert payload["midday_local"] == "12:00"


# --- T-NC2-RETARGET ----------------------------------------------------------------------


async def test_a_revision_below_the_absolute_floor_is_noise(night: Any, api: Any) -> None:
    """Ruling 4/A11: material means >= threshold% of the standing credit AND
    >= 0.5 kWh -- 20% of a tiny forecast is measurement noise."""
    fleet = make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0})
    clock = FakeClock()
    ledger = RevisionLedger()
    tiny = _credit(night, e_surplus_kwh=0.4, e_deficit_kwh=0.0)
    bumped = _credit(
        night, e_surplus_kwh=0.45, e_deficit_kwh=0.0,
        fetched_at=datetime(2026, 8, 26, 23, 30, tzinfo=UTC),
    )
    adviser, _, _, audit, ledger = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([tiny, tiny, bumped, bumped]),
        clock=clock,
        revisions=ledger,
    )
    opened = await adviser.tick()
    clock.wall = datetime(2026, 8, 27, 2, 0, tzinfo=ZONE)
    unchanged = await adviser.tick()
    assert unchanged.target_soc_pct == pytest.approx(opened.target_soc_pct)
    assert [e for e in audit.appended if e.event_type == "night_target_revised"] == []


async def test_the_retarget_caps_come_from_durable_rows_across_a_restart(
    night: Any, api: Any
) -> None:
    """A6: at most TWO re-targets per window, minimum 60 minutes apart, both
    derived from the DURABLE revision rows -- a mid-window restart must not
    reset the budget."""
    fleet = make_fleet(api, {"lhs": 20.0, "mid": 20.0, "rhs": 20.0})
    window_day = date(2026, 8, 27)
    ledger = RevisionLedger(
        {
            window_day: [
                datetime(2026, 8, 27, 1, 0, tzinfo=ZONE),
                datetime(2026, 8, 27, 2, 30, tzinfo=ZONE),
            ]
        }
    )
    third = _credit(
        night, e_surplus_kwh=1.0, e_deficit_kwh=0.0,
        fetched_at=datetime(2026, 8, 27, 3, 0, tzinfo=UTC),
    )
    adviser, _, _, audit, _ = make_adviser(
        night,
        api,
        fleet,
        credit=FakeCreditPort([_credit(night), third, third]),
        revisions=ledger,
    )
    opened = await adviser.tick()  # window-open target stands on the old fetch
    refused = await adviser.tick()  # the durable rows already hold two revisions
    assert refused.target_soc_pct == pytest.approx(opened.target_soc_pct)
    assert [e for e in audit.appended if e.event_type == "night_target_revised"] == []

    # The 60-minute gap: one durable revision 20 minutes ago, a new arrival
    # inside the gap window is refused on the gap alone.
    close_ledger = RevisionLedger({window_day: [datetime(2026, 8, 27, 2, 0, tzinfo=ZONE)]})
    adviser, _, _, audit, _ = make_adviser(
        night,
        api,
        dict(fleet),
        credit=FakeCreditPort([_credit(night), third, third]),
        revisions=close_ledger,
        clock=FakeClock(wall=datetime(2026, 8, 27, 2, 20, tzinfo=ZONE)),
    )
    gap_opened = await adviser.tick()
    gap_refused = await adviser.tick()
    assert gap_refused.target_soc_pct == pytest.approx(gap_opened.target_soc_pct)


async def test_a_material_revision_moves_the_target_and_writes_its_row(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 20.0, "mid": 20.0, "rhs": 20.0})
    clock = FakeClock()
    ledger = RevisionLedger()
    first = _credit(night, e_surplus_kwh=6.0, e_deficit_kwh=0.5)
    later = _credit(
        night, e_surplus_kwh=1.0, e_deficit_kwh=1.0,
        fetched_at=datetime(2026, 8, 26, 23, 59, tzinfo=UTC),
    )
    adviser, _, _, audit, ledger = make_adviser(
        night, api, fleet, credit=FakeCreditPort([first, later]), clock=clock, revisions=ledger
    )
    opened = await adviser.tick()
    clock.wall = datetime(2026, 8, 27, 1, 30, tzinfo=ZONE)
    revised = await adviser.tick()
    assert revised.target_soc_pct > opened.target_soc_pct, "the cloudier revision raises"
    rows = [e for e in audit.appended if e.event_type == "night_target_revised"]
    assert len(rows) == 1
    payload = dict(rows[0].payload or {})
    assert payload["direction"] == "raise"
    assert ledger.history(date(2026, 8, 27)) == (datetime(2026, 8, 27, 1, 30, tzinfo=ZONE),)
    # The projection's provenance moves with the revision.
    assert revised.forecast is not None
    assert revised.forecast["fetched_at"] == later.fetched_at.isoformat()


async def test_a_stood_down_unit_inherits_the_revised_target(night: Any, api: Any) -> None:
    """A6: the demand rule still gates WHEN, but on resume the unit charges
    toward the REVISED fleet target, never the window-open one."""
    fleet = make_fleet(api, {"lhs": 20.0, "mid": 20.0, "rhs": 20.0})
    clock = FakeClock()
    first = _credit(night, e_surplus_kwh=6.0, e_deficit_kwh=0.5)
    cloudier = _credit(
        night,
        e_surplus_kwh=1.0,
        e_deficit_kwh=1.0,
        fetched_at=datetime(2026, 8, 26, 23, 59, tzinfo=UTC),
    )
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        fleet,
        settings=make_settings(night, pacing="even", assumed_capacity_wh=dict(CAPACITIES)),
        credit=FakeCreditPort([first, first, cloudier, cloudier]),
        clock=clock,
    )
    opened = await adviser.tick()
    assert opened.target_soc_pct == pytest.approx(65.49, abs=0.2)

    # The EV arrives: the fleet stands down on MEASURED demand (the same
    # fetch, so no revision yet).
    clock.wall = datetime(2026, 8, 27, 1, 30, tzinfo=ZONE)
    for unit in fleet.values():
        object.__setattr__(unit, "load_power_w", 2_000.0)
    stood_down = await adviser.tick()
    assert stood_down.phase == "standing_by_on_demand"

    # The cloudier revision lands while the fleet stands down: the fleet
    # target moves (the demand rule gates WHEN, never WHAT).
    revised = await adviser.tick()
    assert revised.target_soc_pct > opened.target_soc_pct

    # Demand falls: the resumed tick paces toward the REVISED target -- the
    # stand-down inherited it, exactly as section 5.2 pins.
    clock.wall = datetime(2026, 8, 27, 4, 0, tzinfo=ZONE)
    for unit in fleet.values():
        object.__setattr__(unit, "load_power_w", 100.0)
    resumed = await adviser.tick()
    assert resumed.target_soc_pct == pytest.approx(revised.target_soc_pct)
    assert resumed.phase == "pacing"
    assert "demand_below_exit" in resumed.reason_codes


# --- T-NC2-DEMAND-INTERPLAY ---------------------------------------------------------------


async def test_a_short_window_closes_below_target_with_the_morning_notice(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 20.0, "mid": 20.0, "rhs": 20.0})
    clock = FakeClock(wall=datetime(2026, 8, 27, 5, 59, tzinfo=ZONE))
    adviser, _, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort([_credit(night)]), clock=clock
    )
    in_window = await adviser.tick()
    assert in_window.in_window is True

    clock.wall = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    closed = await adviser.tick()
    assert closed.in_window is False
    assert night.REASON_WINDOW_CLOSED_BELOW_TARGET in closed.reason_codes
    assert closed.morning_notice is not None
    notice = dict(closed.morning_notice)
    assert notice["date"] == "2026-08-27"
    assert set(notice["units_below_target"]) == {"lhs", "mid", "rhs"}
    assert notice["until_local"] == "12:00"


async def test_a_window_that_reached_target_closes_without_a_notice(night: Any, api: Any) -> None:
    fleet = make_fleet(api, {"lhs": 90.0, "mid": 90.0, "rhs": 90.0})
    clock = FakeClock(wall=datetime(2026, 8, 27, 5, 59, tzinfo=ZONE))
    adviser, _, _, _, _ = make_adviser(
        night, api, fleet, credit=FakeCreditPort([_credit(night)]), clock=clock
    )
    await adviser.tick()
    clock.wall = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    closed = await adviser.tick()
    assert night.REASON_WINDOW_CLOSED_BELOW_TARGET not in closed.reason_codes
    assert closed.morning_notice is None


# --- T-NC2-PROJECTION/EVENTS ---------------------------------------------------------------


def _controller(night: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "pacing": "cap_first",
        "rate_cap_w": 2_500,
        "hold_rate_w": 100,
        "demand_scope": "fleet",
        "demand_threshold_w": 1_000,
        "windows": WINDOW,
        "timezone": "Australia/Brisbane",
        "posture": "partition",
        "clock": FakeClock(),
        "acknowledged_partition": True,
        "config_enabled": True,
    }
    values.update(overrides)
    return night.NightChargeController(**values)


def test_full_frames_stay_byte_identical_to_v1(night: Any) -> None:
    controller = _controller(night)  # target_policy defaults to full
    payload = controller.state_payload()
    assert "target_policy" not in payload
    assert "trust" not in payload
    assert "forecast" not in payload
    assert "explanation" not in payload
    assert "morning_notice" not in payload
    for unit in payload["units"]:
        assert "target_soc_pct" not in unit and "suggested_target_soc_pct" not in unit
    # The semantic tuple gains exactly two members and watts never trigger.
    base = controller.state().semantic_tuple()
    assert base[-2:] == ("full", None)


def test_forecast_frames_carry_the_additive_vocabulary(night: Any) -> None:
    trust_payload = {
        "state": "earned",
        "days_scored": 19,
        "required_days": 14,
        "mean_abs_err_pct": 21.4,
        "bias_pct": -4.2,
        "low_surplus_days": 5,
        "high_surplus_days": 4,
    }
    controller = _controller(
        night,
        target_policy="forecast_act",
        trust_view=lambda: dict(trust_payload),
        midday_local=time(12, 0),
    )
    payload = controller.state_payload()
    assert payload["target_policy"] == "forecast_act"
    assert payload["trust"] == trust_payload
    semantic = controller.state().semantic_tuple()
    assert semantic[-2:] == ("forecast_act", "earned"), (
        "the tuple's two new members: target_policy and trust.state"
    )


async def test_the_act_projection_renders_the_section_7_forecast_block(
    night: Any, api: Any
) -> None:
    fleet = make_fleet(api, {"lhs": 61.8, "mid": 61.8, "rhs": 68.9})
    bus_events: list[dict[str, Any]] = []

    class Bus:
        async def publish(self, body: dict[str, Any]) -> int:
            bus_events.append(body)
            return 1

    controller = _controller(
        night,
        target_policy="forecast_act",
        trust_view=lambda: {
            "state": "earned", "days_scored": 19, "required_days": 14,
            "mean_abs_err_pct": 21.4, "bias_pct": -4.2,
            "low_surplus_days": 5, "high_surplus_days": 4,
        },
        bus=Bus(),
    )
    adviser, _, _, _, _ = make_adviser(night, api, fleet, credit=FakeCreditPort([_credit(night)]))
    controller.bind_adviser(adviser)
    decision = await adviser.tick()
    await controller.observe_tick(decision)

    payload = controller.state_payload()
    assert payload["target_policy"] == "forecast_act"
    assert payload["trust"]["state"] == "earned"
    assert payload["forecast"]["source"] == "solcast"
    assert payload["forecast"]["quantile"] == 0.1
    assert payload["forecast"]["e_surplus_kwh"] == pytest.approx(6.0)
    assert payload["forecast"]["e_deficit_kwh"] == pytest.approx(0.5)
    assert payload["forecast"]["e_credit_kwh"] == pytest.approx(4.9)
    assert payload["forecast"]["midday_local"] == "12:00"
    units = {row["unit_id"]: row for row in payload["units"]}
    assert units["rhs"]["phase"] == "complete"
    assert units["rhs"]["reason"] == "target_reached"
    assert units["rhs"]["target_soc_pct"] == pytest.approx(65.49, abs=0.2)
    assert "target_soc_pct" in units["lhs"]
    assert payload["explanation"] is not None
    assert "4.9 kWh" in payload["explanation"]
    # One publication, carrying the two new members through the event.
    assert bus_events and bus_events[0]["type"] == "night_charge.state_changed"
    assert bus_events[0]["payload"]["target_policy"] == "forecast_act"


def test_the_suggest_projection_names_the_suggested_key(night: Any, api: Any) -> None:
    controller = _controller(night, target_policy="forecast_suggest", trust_view=lambda: None)
    payload = controller.state_payload()
    assert payload["target_policy"] == "forecast_suggest"


async def test_nothing_publishes_while_disabled(night: Any, api: Any) -> None:
    published: list[dict[str, Any]] = []

    class Bus:
        async def publish(self, body: dict[str, Any]) -> int:
            published.append(body)
            return 1

    controller = _controller(night, target_policy="forecast_act", config_enabled=False, bus=Bus())
    adviser, _, _, _, _ = make_adviser(
        night,
        api,
        make_fleet(api, {"lhs": 60.0, "mid": 60.0, "rhs": 60.0}),
        credit=FakeCreditPort([_credit(night)]),
    )
    controller.bind_adviser(adviser)
    decision = await adviser.tick()
    await controller.observe_tick(decision)
    assert published == [], "a boot-composed disabled site never publishes"


# --- the composed morning-credit port (T-NC2-FALLBACK's A9 pin lives here) ------------


from energypod.adapters.providers.model import ForecastSeries, ForecastValue  # noqa: E402


def _pv_series(
    watts: list[float],
    *,
    fetched_at: datetime,
    quantile: float | None = 0.1,
    start: datetime | None = None,
    slot_minutes: int = 30,
) -> Any:
    base = start or datetime(2026, 8, 26, 20, 0, tzinfo=UTC)  # covers 06:00+10:00 onward
    values = [
        ForecastValue(
            variable="pv_power_w",
            interval_start=base + timedelta(minutes=slot_minutes * index),
            interval_end=base + timedelta(minutes=slot_minutes * (index + 1)),
            value=value,
            source="solcast",
            fetched_at=fetched_at,
            quantile=quantile,
        )
        for index, value in enumerate(watts)
    ]
    return ForecastSeries(
        variable="pv_power_w", source="solcast", fetched_at=fetched_at, values=tuple(values)
    )


def _load_series(
    watts: list[float | None],
    *,
    fetched_at: datetime,
    start: datetime | None = None,
    slot_minutes: int = 30,
) -> Any:
    base = start or datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    values = []
    for index, value in enumerate(watts):
        if value is None:
            continue  # an absent baseline slot, never zero-filled
        values.append(
            ForecastValue(
                variable="load_power_w",
                interval_start=base + timedelta(minutes=slot_minutes * index),
                interval_end=base + timedelta(minutes=slot_minutes * (index + 1)),
                value=value,
                source="historian-baseline",
                fetched_at=fetched_at,
            )
        )
    return ForecastSeries(
        variable="load_power_w",
        source="historian-baseline",
        fetched_at=fetched_at,
        values=tuple(values),
    )


class _PortPv:
    def __init__(self, series: Any) -> None:
        self._series = series
        self.calls = 0

    async def pv_forecast(self) -> Any:
        self.calls += 1
        return self._series


class _PortLoad:
    def __init__(self, series: Any) -> None:
        self._series = series
        self.calls = 0

    async def load_forecast(self) -> Any:
        self.calls += 1
        return self._series


def _composed_port(pv: Any, load: Any, *, stale_after_s: float = 43200.0) -> Any:
    from energypod.runtime.composition import _RegistryMorningCredit

    return _RegistryMorningCredit(
        pv=pv, load=load, clock=FakeClock(wall=datetime(2026, 8, 26, 22, 30, tzinfo=UTC)),
        stale_after_s=stale_after_s,
    )


async def test_the_port_nets_the_morning_per_slot() -> None:
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    pv = _PortPv(_pv_series([2000.0] * 12, fetched_at=fetched))
    load = _PortLoad(_load_series([500.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load)

    window_end = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)
    credit = await port(window_end, time(12, 0), 0.1)

    assert credit.failure is None
    assert credit.e_surplus_kwh == pytest.approx(1_500.0 * 6.0 / 1000.0)
    assert credit.e_deficit_kwh == pytest.approx(0.0)
    assert credit.source == "solcast"
    assert credit.quantile == 0.1
    assert len(credit.slots) == 12


async def test_the_port_charges_the_deficit_at_full_value() -> None:
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    pv = _PortPv(_pv_series([200.0] * 12, fetched_at=fetched))
    load = _PortLoad(_load_series([700.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load)

    credit = await port(datetime(2026, 8, 27, 6, 0, tzinfo=ZONE), time(12, 0), 0.1)

    assert credit.failure is None
    assert credit.e_surplus_kwh == pytest.approx(0.0)
    assert credit.e_deficit_kwh == pytest.approx(500.0 * 6.0 / 1000.0)


async def test_a_single_baseline_slot_hole_fails_the_whole_morning() -> None:
    """A9's pin, at the port that owns the netting: ONE unavailable
    same-slot-last-week baseline is a hole in the netting, never a spot to
    interpolate around -- and there is deliberately NO degrade-to-PV-only
    path (gross PV over-credits the morning and under-charges)."""
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    pv = _PortPv(_pv_series([2000.0] * 12, fetched_at=fetched))
    load_values = [500.0] * 12
    load_values[5] = None  # the single hole
    load = _PortLoad(_load_series(load_values, fetched_at=fetched))
    port = _composed_port(pv, load)

    credit = await port(datetime(2026, 8, 27, 6, 0, tzinfo=ZONE), time(12, 0), 0.1)

    assert credit.failure == "forecast_no_load_baseline"


async def test_a_pv_coverage_gap_is_forecast_missing() -> None:
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    # Only 10 slots from 20:00 UTC: the morning span's tail is uncovered.
    pv = _PortPv(_pv_series([2000.0] * 10, fetched_at=fetched))
    load = _PortLoad(_load_series([500.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load)

    credit = await port(datetime(2026, 8, 27, 6, 0, tzinfo=ZONE), time(12, 0), 0.1)
    assert credit.failure == "forecast_missing"


async def test_an_aged_fetch_is_stale_even_from_the_cache() -> None:
    fetched = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)  # 18.5 h old at 22:30
    pv = _PortPv(_pv_series([2000.0] * 12, fetched_at=fetched))
    load = _PortLoad(_load_series([500.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load, stale_after_s=43200.0)

    credit = await port(datetime(2026, 8, 27, 6, 0, tzinfo=ZONE), time(12, 0), 0.1)
    assert credit.failure == "forecast_stale"


async def test_the_result_caches_per_fetched_at_so_ticks_burn_no_reads() -> None:
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    pv = _PortPv(_pv_series([2000.0] * 12, fetched_at=fetched))
    load = _PortLoad(_load_series([500.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load)
    window_end = datetime(2026, 8, 27, 6, 0, tzinfo=ZONE)

    first = await port(window_end, time(12, 0), 0.1)
    second = await port(window_end, time(12, 0), 0.1)

    assert first.failure is None and second.failure is None
    assert pv.calls == 2  # the provider's own cache gate owns the wire budget
    assert load.calls == 1, "the historian reread happens only on a cache miss"


async def test_a_point_forecast_serves_with_an_honest_quantile_of_none() -> None:
    fetched = datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    pv = _PortPv(_pv_series([2000.0] * 12, fetched_at=fetched, quantile=None))
    load = _PortLoad(_load_series([500.0] * 12, fetched_at=fetched))
    port = _composed_port(pv, load)

    credit = await port(datetime(2026, 8, 27, 6, 0, tzinfo=ZONE), time(12, 0), 0.1)
    assert credit.failure is None
    assert credit.quantile is None, "a point source never gains a fabricated slice"
    assert credit.e_surplus_kwh == pytest.approx(9.0)


# --- the composition smoke (the wiring the config validation promises) -----------------


def _composition_payload(*, policy: str) -> dict[str, Any]:
    from tests.unit.test_config import _valid_config

    payload = _valid_config()
    payload["schedule"] = {"allowed_windows_local": [["00:00", "20:00"]]}
    payload["plant_history"] = {}
    payload["forecast_providers"] = {
        "enabled": True,
        "stale_after_s": 43200.0,
        "solcast": {
            "api_key_env": "ENERGYPOD_TEST_NIGHT_KEY_UNSET",
            "resource_id": "b6bf-9d1d-0680-4078",
        },
        "load_baseline": {},
    }
    night: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": [["00:00", "06:00"]],
        "target_policy": policy,
    }
    if policy != "full":
        # The widened capacity-map IFF: a forecast posture needs the map even
        # under cap_first; full refuses it as a stray.
        night["assumed_capacity_wh"] = {"mid": 5000, "rhs": 4200, "lhs": 5000}
    payload["night_charging"] = night
    return payload


def test_a_forecast_posture_composes_the_whole_v2_wiring() -> None:
    """The adviser consumes the injected port, the trust ledger is composed
    over the durable store, the projection carries the trust view, and the
    fleet loop holds the bounded trust step -- nothing fetches at boot."""
    from energypod.runtime.composition import build_runtime
    from energypod.runtime.config import ControllerConfig

    payload = _composition_payload(policy="forecast_suggest")
    runtime = build_runtime(ControllerConfig.model_validate(payload), simulate=True)
    assert runtime.night_adviser is not None
    assert runtime.night_controller is not None
    payload = runtime.night_controller.state_payload()
    assert payload["target_policy"] == "forecast_suggest"
    assert payload["trust"]["state"] == "provisioning"
    assert payload["trust"]["required_days"] == 14
    # The projection names the suggested key, never the governing one.
    assert all(
        "suggested_target_soc_pct" in unit or unit["phase"] == "idle"
        for unit in payload["units"]
    ) or payload["units"] == ()


def test_a_full_posture_composes_no_trust_machinery() -> None:
    from energypod.runtime.composition import build_runtime
    from energypod.runtime.config import ControllerConfig

    runtime = build_runtime(
        ControllerConfig.model_validate(_composition_payload(policy="full")), simulate=True
    )
    assert runtime.night_adviser is not None
    payload = runtime.night_controller.state_payload()
    assert "target_policy" not in payload, "v1 frames stay byte-identical under full"
