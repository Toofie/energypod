"""Contract tests for the excess-solar accelerated-charging adviser.

The module under test is ``energypod.application.excess_charge`` (API_CONTRACTS
"Excess-solar accelerated charging (advisory)").  It is an ADVISORY component:
it holds no transport, no authorization path, and no allocator or kernel role.
It only computes a deterministic export bound and submits ordinary short-TTL
``OPTIMIZER`` charge intents through the existing intent path, one unit at a
time, renewing while the bound persists and yielding to any higher-priority
intent.  Hand-back to the pod's own autonomy is by NON-RENEWAL: the adviser
never writes a stop triple and never submits an idle intent.

Pinned contract (the red phase fails cleanly while the module is absent)::

    ExcessChargeSettings(            # behavioural keys from the config block
        assumed_autonomous_charge_w: int,
        min_acceleration_w: int,
        exit_hysteresis_w: int,
        intent_ttl_s: float,
    )
    eligible_export_charge_w(observations, policy, now_mono) -> int
        # min(max_charge_from_export_w, max(0, floor(sum(grid_power_w)) - margin));
        # 0 unless EVERY fleet unit's grid evidence is finite, GOOD, and fresh
        # (age <= policy.export_telemetry_max_age_s); 0 when the policy's
        # export triple is not armed.
    ExcessChargeDecision(action, target_unit_id, eligible_charge_w,
                         proposed_watts, reason_codes)
    ExcessChargeAdviser(*, settings, policy, clock, observations, intents, submit)
        async tick() -> ExcessChargeDecision
        # action in {"idle", "propose", "renew", "withdraw"}

Beat-autonomy rule (from the live environment facts): while renewed, the
adviser's objective REPLACES the pod's own self-consumption (~-520..-560 W
observed daytime autonomous charge).  Commanding less than autonomy would SLOW
charging, so the adviser intervenes only when the achievable rate exceeds
autonomy by ``min_acceleration_w`` and keeps intervening only while it stays
above autonomy plus the smaller ``exit_hysteresis_w`` — explicit hysteresis so
a dip between the thresholds never oscillates.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

NOW = 100.0
UNIT_IDS = ("lhs", "mid", "rhs")
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
    """The public domain vocabulary the advisory contract builds on."""
    importlib.import_module("energypod.domain")
    domain = importlib.import_module("energypod.domain")
    required = ("ControlPolicy", "DataQuality", "Observation", "UnitLifecycle")
    missing = [name for name in required if not hasattr(domain, name)]
    assert not missing, f"public domain contract is not implemented: {', '.join(missing)}"
    return domain


@pytest.fixture(scope="module")
def excess() -> Any:
    """Load the advisory contract; report absence as an ordinary failure."""
    try:
        module = importlib.import_module("energypod.application.excess_charge")
    except ImportError as error:
        pytest.fail(f"the excess-charge advisory contract is not implemented: {error}")
        raise  # pragma: no cover - pytest.fail never returns
    missing = [
        name
        for name in ("ExcessChargeAdviser", "ExcessChargeSettings", "eligible_export_charge_w")
        if not hasattr(module, name)
    ]
    assert not missing, f"excess-charge contract is incomplete: {', '.join(missing)}"
    return module


# --- deterministic fakes -------------------------------------------------------


@dataclass
class FakeClock:
    now: float = NOW

    def wall_now(self) -> datetime:
        return datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

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
        self, *, unit_ids: Any, direction: Any, watts: Any, ttl_s: Any
    ) -> dict[str, Any]:
        self.submissions.append(
            {
                "unit_ids": sorted(unit_ids),
                "direction": getattr(direction, "value", direction),
                "watts": watts,
                "ttl_s": ttl_s,
            }
        )
        return {
            "intent_id": f"excess-{len(self.submissions)}",
            "status": "accepted",
        }


def make_policy(api: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "version": "export-1",
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
        "export_charge_limit_w": 2_000,
        "export_headroom_margin_w": 100,
        "export_telemetry_max_age_s": 5.0,
    }
    values.update(overrides)
    return api.ControlPolicy(**values)


def make_observation(
    api: Any,
    *,
    unit_id: str,
    grid_power_w: float | None,
    system_soc_pct: float = 50.0,
    dynamic_charge_limit_w: float = 2_500.0,
    lifecycle: Any = None,
    captured_at_mono: float = NOW,
    grid_quality: Any = None,
) -> Any:
    quality = {name: api.DataQuality.GOOD for name in QUALITY_FIELDS}
    if grid_quality is not None:
        quality["grid_power_w"] = grid_quality
    return api.Observation(
        unit_id=unit_id,
        wall_timestamp=datetime(2026, 8, 23, tzinfo=UTC),
        captured_at_mono=captured_at_mono,
        sequence=7,
        lifecycle=lifecycle or api.UnitLifecycle.ARMED_IDLE,
        protocol_profile="iot",
        system_soc_pct=system_soc_pct,
        bms_soc_pct=system_soc_pct,
        soh_pct=98.0,
        battery_watts=0.0,
        pack_voltage_v=400.0,
        pack_current_a=0.0,
        dynamic_charge_limit_w=dynamic_charge_limit_w,
        dynamic_discharge_limit_w=2_500.0,
        grid_power_w=grid_power_w,
        load_power_w=0.0,
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
    grids: Mapping[str, float | None],
    *,
    socs: Mapping[str, float] | None = None,
    **observation_overrides: Any,
) -> dict[str, Any]:
    default_socs = {"lhs": 57.0, "mid": 12.0, "rhs": 73.0}
    socs = default_socs if socs is None else {**default_socs, **socs}
    overrides_by_unit: dict[str, dict[str, Any]] = {}
    for key, value in observation_overrides.items():
        unit, _, name = key.partition("__")
        overrides_by_unit.setdefault(unit, {})[name] = value
    return {
        unit: make_observation(
            api,
            unit_id=unit,
            grid_power_w=grid,
            system_soc_pct=socs.get(unit, 50.0),
            **overrides_by_unit.get(unit, {}),
        )
        for unit, grid in grids.items()
    }


def make_settings(excess: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "assumed_autonomous_charge_w": 520,
        "min_acceleration_w": 100,
        "exit_hysteresis_w": 50,
        "intent_ttl_s": 10.0,
    }
    values.update(overrides)
    return excess.ExcessChargeSettings(**values)


def make_adviser(
    excess: Any,
    api: Any,
    observations: dict[str, Any],
    *,
    intents: FakeIntents | None = None,
    submit: FakeSubmit | None = None,
    clock: FakeClock | None = None,
    policy: Any | None = None,
    settings: Any | None = None,
) -> tuple[Any, FakeIntents, FakeSubmit, FakeClock]:
    fake_intents = intents or FakeIntents()
    fake_submit = submit or FakeSubmit()
    fake_clock = clock or FakeClock()
    adviser = excess.ExcessChargeAdviser(
        settings=settings or make_settings(excess),
        policy=policy or make_policy(api),
        clock=fake_clock,
        observations=FakeObservations(latest=observations),
        intents=fake_intents,
        submit=fake_submit,
    )
    return adviser, fake_intents, fake_submit, fake_clock


def manual_intent(
    *,
    expires_at_mono: float = 130.0,
    unit_ids: tuple[str, ...] = ("mid",),
    source: str = "MANUAL",
) -> Any:
    from energypod.domain import IntentSource

    return SimpleNamespace(
        id="manual-1",
        source=getattr(IntentSource, source),
        selected_unit_ids=frozenset(unit_ids),
        expires_at_mono=expires_at_mono,
    )


# --- the deterministic export bound --------------------------------------------


@pytest.mark.parametrize(
    ("grids", "overrides", "expected"),
    [
        # 800 + 900 - 300 = 1400 export, minus 100 margin = 1300 eligible.
        ({"lhs": -300.0, "mid": 800.0, "rhs": 900.0}, {}, 1_300),
        # Net import across the fleet: nothing eligible.
        ({"lhs": -900.0, "mid": -300.0, "rhs": -100.0}, {}, 0),
        # The margin swallows the entire export.
        ({"lhs": 30.0, "mid": 30.0, "rhs": 30.0}, {}, 0),
        # The configured cap dominates a large export (6900 - 100 capped at 2000).
        ({"lhs": 2_300.0, "mid": 2_300.0, "rhs": 2_300.0}, {}, 2_000),
        # One stale phase collapses the bound fail-closed (age 4.5 > 3.0).
        (
            {"lhs": -300.0, "mid": 800.0, "rhs": 900.0},
            {"export_telemetry_max_age_s": 3.0, "captured": {"rhs": 95.5}},
            0,
        ),
        # One quality-bad phase collapses the bound fail-closed.
        (
            {"lhs": -300.0, "mid": 800.0, "rhs": 900.0},
            {"grid_quality": "BAD"},
            0,
        ),
        # One unsourced phase (None grid) collapses the bound fail-closed.
        (
            {"lhs": -300.0, "mid": None, "rhs": 900.0},
            {},
            0,
        ),
        # A fleet unit with no observation at all collapses the bound.
        (
            {"lhs": -300.0, "rhs": 900.0},
            {},
            0,
        ),
        # The export triple not armed: no advisory charge, fail-closed default.
        (
            {"lhs": 2_300.0, "mid": 2_300.0, "rhs": 2_300.0},
            {"export_charge_limit_w": None},
            0,
        ),
    ],
)
def test_export_bound_is_deterministic_and_fail_closed(
    excess: Any, api: Any, grids: dict[str, float | None], overrides: dict[str, Any], expected: int
) -> None:
    """Margin-subtracted fleet export, capped, collapsing to 0 on any unusable
    evidence — one unreadable phase is never treated as zero export."""
    if overrides.get("export_charge_limit_w", "armed") is None:
        # The triple is all-or-none: an EXPLICIT None disarms it (empty
        # overrides mean the commissioned defaults, not disarm).
        policy = make_policy(
            api,
            export_charge_limit_w=None,
            export_headroom_margin_w=None,
            export_telemetry_max_age_s=None,
        )
    else:
        policy = make_policy(
            api,
            **{
                key: value
                for key, value in overrides.items()
                if key == "export_telemetry_max_age_s"
            },
        )
    captured = overrides.get("captured", {})
    grid_quality = (
        getattr(api.DataQuality, overrides["grid_quality"]) if "grid_quality" in overrides else None
    )
    observations = {
        unit: make_observation(
            api,
            unit_id=unit,
            grid_power_w=grid,
            captured_at_mono=captured.get(unit, NOW),
            grid_quality=grid_quality,
        )
        for unit, grid in grids.items()
    }

    assert excess.eligible_export_charge_w(observations, policy, NOW) == expected


# --- beat-autonomy hysteresis ---------------------------------------------------


async def test_adviser_proposes_only_above_the_autonomous_rate(excess: Any, api: Any) -> None:
    """Entry: achievable >= autonomy + min_acceleration (520 + 100 = 620 W)."""
    # 1000 W export - 100 margin = 900 W eligible; achievable 900 >= 620.
    fleet = make_fleet(api, {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0})
    adviser, _intents, submit, _clock = make_adviser(excess, api, fleet)

    decision = await adviser.tick()

    assert decision.action == "propose"
    assert decision.target_unit_id == "mid"
    assert decision.eligible_charge_w == 900
    assert decision.proposed_watts == 900
    assert len(submit.submissions) == 1
    assert submit.submissions[0]["unit_ids"] == ["mid"]
    assert submit.submissions[0]["direction"] == "charge"
    assert submit.submissions[0]["watts"] == 900
    assert submit.submissions[0]["ttl_s"] == pytest.approx(10.0)


async def test_adviser_leaves_autonomy_alone_below_the_entry_threshold(
    excess: Any, api: Any
) -> None:
    """Below autonomy + margin the pod's own self-consumption is faster than
    anything the adviser could command: commanding less would SLOW charging."""
    # 700 - 100 = 600 W eligible < 620 W entry threshold.
    fleet = make_fleet(api, {"lhs": 0.0, "mid": 0.0, "rhs": 700.0})
    adviser, _intents, submit, _clock = make_adviser(excess, api, fleet)

    decision = await adviser.tick()

    assert decision.action == "idle"
    assert decision.target_unit_id is None
    assert "no_acceleration_over_autonomy" in decision.reason_codes
    assert submit.submissions == []


async def test_adviser_hysteresis_does_not_oscillate_between_thresholds(
    excess: Any, api: Any
) -> None:
    """A dip between the exit (570 W) and entry (620 W) thresholds keeps the
    intervention alive: leaving and re-entering would hand control to the
    watchdog gap and slow charging."""
    grids_high = {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0}  # eligible 900
    grids_mid = {"lhs": 0.0, "mid": 0.0, "rhs": 700.0}  # eligible 600
    grids_low = {"lhs": 0.0, "mid": 0.0, "rhs": 650.0}  # eligible 550

    observations = FakeObservations(latest=make_fleet(api, grids_high))
    submit = FakeSubmit()
    adviser = excess.ExcessChargeAdviser(
        settings=make_settings(excess),
        policy=make_policy(api),
        clock=FakeClock(),
        observations=observations,
        intents=FakeIntents(),
        submit=submit,
    )

    entry = await adviser.tick()
    assert entry.action == "propose"

    observations.latest = make_fleet(api, grids_mid)
    dip = await adviser.tick()
    assert dip.action == "renew", (
        "an achievable rate of 600 W is above the 570 W exit threshold: the "
        f"adviser must keep intervening, got {dip.action!r}"
    )
    assert len(submit.submissions) == 2

    observations.latest = make_fleet(api, grids_low)
    exit_decision = await adviser.tick()
    assert exit_decision.action == "withdraw"
    assert "below_exit_hysteresis" in exit_decision.reason_codes


# --- target selection -----------------------------------------------------------


async def test_adviser_targets_the_neediest_eligible_unit_one_at_a_time(
    excess: Any, api: Any
) -> None:
    """Lowest SOC with charge headroom; ties break by unit id."""
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_500.0}  # eligible 1400

    # Default fleet: mid (SOC 12) is neediest.
    fleet = make_fleet(api, grids)
    adviser, _i, submit, _c = make_adviser(excess, api, fleet)
    decision = await adviser.tick()
    assert decision.target_unit_id == "mid"

    # mid inhibited: next neediest is lhs (57), never an uncontrollable unit.
    fleet = make_fleet(api, grids)
    fleet["mid"] = make_observation(
        api,
        unit_id="mid",
        grid_power_w=0.0,
        system_soc_pct=12.0,
        lifecycle=api.UnitLifecycle.INHIBITED,
    )
    adviser, _i2, submit2, _c2 = make_adviser(excess, api, fleet)
    decision = await adviser.tick()
    assert decision.target_unit_id == "lhs"
    assert submit2.submissions[0]["unit_ids"] == ["lhs"]

    # mid above the SOC charge ceiling (96 > 95): skipped for lhs.
    fleet = make_fleet(api, grids, socs={"mid": 96.0})
    adviser, _i3, _submit3, _c3 = make_adviser(excess, api, fleet)
    decision = await adviser.tick()
    assert decision.target_unit_id == "lhs"

    # mid without charge headroom (dynamic limit 0): skipped for lhs.
    fleet = make_fleet(api, grids, mid__dynamic_charge_limit_w=0.0)
    adviser, _i4, _submit4, _c4 = make_adviser(excess, api, fleet)
    decision = await adviser.tick()
    assert decision.target_unit_id == "lhs"

    # A SOC tie between mid and lhs breaks toward the lower unit id.
    fleet = make_fleet(api, grids, socs={"mid": 40.0, "lhs": 40.0})
    adviser, _i5, _submit5, _c5 = make_adviser(excess, api, fleet)
    decision = await adviser.tick()
    assert decision.target_unit_id == "lhs"

    for submission in (*submit.submissions, *submit2.submissions):
        assert len(submission["unit_ids"]) == 1, "one unit at a time, never a fleet dispatch"


# --- operator precedence and renewal --------------------------------------------


async def test_adviser_yields_to_a_manual_intent_until_it_expires(excess: Any, api: Any) -> None:
    """Live-verified precedence (2026-08-23): a manual console intent always
    displaces the adviser.  The arbiter's priority (manual > optimizer) already
    picks the manual winner every tick; the adviser additionally withdraws and
    does not re-post until the manual intent has expired AND hysteresis
    re-qualifies."""
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0}
    observations = FakeObservations(latest=make_fleet(api, grids))
    intents = FakeIntents()
    submit = FakeSubmit()
    clock = FakeClock()
    adviser = excess.ExcessChargeAdviser(
        settings=make_settings(excess),
        policy=make_policy(api),
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
    )

    active = await adviser.tick()
    assert active.action == "propose"

    intents.entries.append(manual_intent(expires_at_mono=130.0))
    clock.now = 110.0
    yielding = await adviser.tick()
    assert yielding.action == "withdraw"
    assert "yielding_to_higher_priority" in yielding.reason_codes
    assert submit.submissions[-1]["watts"] > 0, "yield is a removal, never a zero submission"

    clock.now = 111.0
    withheld = await adviser.tick()
    assert withheld.action == "idle"
    assert "yielding_to_higher_priority" in withheld.reason_codes

    clock.now = 131.0  # the manual intent has expired
    # The re-entry tick still judges export freshness: refresh the fake
    # fleet's capture clock (evidence 31 s old would collapse the bound
    # fail-closed for the right reasons and mask the precedence pin).
    observations.latest = make_fleet(
        api, grids, **{f"{unit}__captured_at_mono": 131.0 for unit in grids}
    )
    resumed = await adviser.tick()
    assert resumed.action == "propose"
    assert resumed.target_unit_id == "mid"


async def test_adviser_renews_while_the_bound_persists(excess: Any, api: Any) -> None:
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_200.0}  # eligible 1100
    observations = FakeObservations(latest=make_fleet(api, grids))
    intents = FakeIntents()
    submit = FakeSubmit()
    clock = FakeClock()
    adviser = excess.ExcessChargeAdviser(
        settings=make_settings(excess),
        policy=make_policy(api),
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
    )

    for cycle in range(3):
        clock.now = NOW + cycle * 1.5
        decision = await adviser.tick()
        assert decision.action in {"propose", "renew"}
        assert decision.proposed_watts == 1_100

    assert len(submit.submissions) == 3
    assert all(entry["ttl_s"] == pytest.approx(10.0) for entry in submit.submissions)
    assert len(intents.removed) == 2, "each renewal removes the previous adviser intent"


async def test_bound_collapse_withdraws_by_non_renewal_never_by_a_stop(
    excess: Any, api: Any
) -> None:
    """The designed fail-safe: when the export evidence collapses, the adviser
    withdraws its intent; the TTL lapse plus the firmware watchdog (~3.5-4 s)
    return the pod to its own autonomy.  No stop triple, no idle intent, no
    zero-watt submission — the adviser never commands anything on exit."""
    observations = FakeObservations(
        latest=make_fleet(api, {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0})
    )
    intents = FakeIntents()
    submit = FakeSubmit()
    adviser = excess.ExcessChargeAdviser(
        settings=make_settings(excess),
        policy=make_policy(api),
        clock=FakeClock(),
        observations=observations,
        intents=intents,
        submit=submit,
    )

    entry = await adviser.tick()
    assert entry.action == "propose"

    observations.latest = make_fleet(api, {"lhs": -900.0, "mid": -300.0, "rhs": -100.0})
    collapse = await adviser.tick()

    assert collapse.action == "withdraw"
    assert collapse.eligible_charge_w == 0
    assert "no_export_headroom" in collapse.reason_codes
    assert all(entry["watts"] > 0 for entry in submit.submissions), (
        "the adviser never submits zero watts or an idle intent: hand-back is by non-renewal"
    )
    assert all(entry["direction"] == "charge" for entry in submit.submissions)


# --- per-unit yield under concurrent arbitration (2026-08-24) -------------------
#
# Concurrency changed the precedence rule's SHAPE, not its strength: an
# operator intent now displaces the adviser only on the units IT CLAIMS.  The
# adviser targets exactly one unit, so a manual intent claiming a DIFFERENT
# battery no longer stops the advisory charge -- the arbiter runs both in one
# cycle -- while a manual claim on the adviser's OWN target (or any live
# emergency stop, which dominates every unit) still withdraws it.


async def test_adviser_proceeds_while_a_manual_intent_claims_another_unit(
    excess: Any, api: Any
) -> None:
    """Scenario D of the concurrency proof: an operator discharge on rhs while
    the adviser accelerates mid's charge -- both proceed, one cycle each."""
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0}
    intents = FakeIntents()
    intents.entries.append(manual_intent(unit_ids=("rhs",), expires_at_mono=999.0))
    adviser, fake_intents, submit, _clock = make_adviser(
        excess, api, make_fleet(api, grids), intents=intents
    )
    assert fake_intents.entries

    entry = await adviser.tick()

    assert entry.action == "propose"
    assert entry.target_unit_id == "mid"
    assert submit.submissions[-1]["unit_ids"] == ["mid"]
    assert "yielding_to_higher_priority" not in entry.reason_codes


async def test_adviser_yields_when_its_target_unit_is_claimed(excess: Any, api: Any) -> None:
    """The adviser's whole scope (one unit) claimed by a manual intent means
    withdraw for that tick -- per-unit yield, exactly the erosion the arbiter
    would apply anyway, plus the adviser's own anti-spam standdown."""
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0}
    intents = FakeIntents()
    intents.entries.append(manual_intent(unit_ids=("mid", "lhs"), expires_at_mono=999.0))
    adviser, _fake_intents, submit, _clock = make_adviser(
        excess, api, make_fleet(api, grids), intents=intents
    )

    decision = await adviser.tick()

    assert decision.action in {"withdraw", "idle"}
    assert "yielding_to_higher_priority" in decision.reason_codes
    assert submit.submissions == []


async def test_adviser_stands_down_while_any_emergency_stop_is_live(excess: Any, api: Any) -> None:
    """An emergency stop dominates every unit (the arbiter gives it the whole
    cycle), so the adviser withdraws outright rather than renewing against a
    latched stop -- regardless of the stop's own unit scope."""
    grids = {"lhs": 0.0, "mid": 0.0, "rhs": 1_000.0}
    intents = FakeIntents()
    intents.entries.append(
        manual_intent(unit_ids=("rhs",), expires_at_mono=999.0, source="EMERGENCY_STOP")
    )
    adviser, _fake_intents, submit, _clock = make_adviser(
        excess, api, make_fleet(api, grids), intents=intents
    )

    decision = await adviser.tick()

    assert decision.action in {"withdraw", "idle"}
    assert "yielding_to_higher_priority" in decision.reason_codes
    assert submit.submissions == []
