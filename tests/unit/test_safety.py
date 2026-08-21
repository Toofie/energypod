"""S0 contract tests for the deterministic, fail-closed safety kernel.

These tests intentionally exercise only the public ``energypod.domain`` and
``energypod.application`` packages.  Imports are resolved by a fixture so the
suite still collects before the test-first implementation exists.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

NOW = 101.0
UNIT_IDS = ("lhs", "mid", "rhs")
REQUIRED_QUALITY_FIELDS = (
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
)


@pytest.fixture(scope="module")
def api() -> SimpleNamespace:
    """Load the deliberately small public contract without collection errors."""

    domain = importlib.import_module("energypod.domain")
    application = importlib.import_module("energypod.application")
    required_domain = (
        "ControlPolicy",
        "DataQuality",
        "DecisionStatus",
        "Direction",
        "Observation",
        "UnitLifecycle",
    )
    missing = [name for name in required_domain if not hasattr(domain, name)]
    if not hasattr(application, "SafetyKernel"):
        missing.append("SafetyKernel")
    assert not missing, f"public safety contract is not implemented: {', '.join(missing)}"
    return SimpleNamespace(
        **{name: getattr(domain, name) for name in required_domain},
        SafetyKernel=application.SafetyKernel,
    )


def make_policy(api: SimpleNamespace, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "version": "policy-1",
        "static_charge_limit_w_by_unit": {unit: 3_000 for unit in UNIT_IDS},
        "static_discharge_limit_w_by_unit": {unit: 3_000 for unit in UNIT_IDS},
        "fleet_charge_limit_w": 7_500,
        "fleet_discharge_limit_w": 7_500,
        "min_soc_pct": 10.0,
        "max_soc_pct": 90.0,
        "max_soc_jump_pct": 10.0,
        "max_soc_disagreement_pct": 5.0,
        "min_cell_voltage_v": 3.0,
        "max_cell_voltage_v": 3.6,
        "max_cell_imbalance_v": 0.05,
        "expected_cell_count_by_unit": {unit: 4 for unit in UNIT_IDS},
        "min_temperature_c": 0.0,
        "max_temperature_c": 45.0,
        "max_temperature_spread_c": 10.0,
        "max_telemetry_age_s": 5.0,
        "max_cell_age_s": 5.0,
        "authorization_lifetime_s": 1.5,
        "heartbeat_interval_s": 1.0,
        "ramp_limit_w_per_s_by_unit": {unit: 10_000 for unit in UNIT_IDS},
        "apparent_power_limit_va_by_unit": {unit: 5_000 for unit in UNIT_IDS},
        "reactive_limit_var": 0,
        "stable_samples_needed_to_rearm": 3,
        "blocking_fault_codes": frozenset({"BMS_CRITICAL", "PCS_CRITICAL"}),
        "blocking_warning_codes": frozenset({"PCS_Warning0_1", "DCDC_Warning0_1"}),
        "debug_mode_enabled": False,
    }
    values.update(overrides)
    return api.ControlPolicy(**values)


def make_observation(api: SimpleNamespace, **overrides: Any) -> Any:
    unit_id = overrides.get("unit_id", "mid")
    values: dict[str, Any] = {
        "unit_id": unit_id,
        "wall_timestamp": datetime(2026, 8, 21, tzinfo=UTC),
        "captured_at_mono": 100.0,
        "sequence": 7,
        "lifecycle": api.UnitLifecycle.ARMED_IDLE,
        "protocol_profile": "waveshare-iot-v1",
        "system_soc_pct": 50.0,
        "bms_soc_pct": 50.0,
        "soh_pct": 98.0,
        "battery_watts": 0.0,
        "pack_voltage_v": 400.0,
        "pack_current_a": 0.0,
        "dynamic_charge_limit_w": 5_000,
        "dynamic_discharge_limit_w": 5_000,
        "cell_voltages_v": (3.30, 3.31, 3.29, 3.30),
        "cell_captured_at_mono": 100.0,
        "cell_sequence": 4,
        "temperatures_c": (24.0, 25.0, 26.0, 25.0),
        "active_faults": frozenset(),
        "active_warnings": frozenset(),
        "quality": {field: api.DataQuality.GOOD for field in REQUIRED_QUALITY_FIELDS},
    }
    values.update(overrides)
    if "captured_at_mono" in overrides and "cell_captured_at_mono" not in overrides:
        values["cell_captured_at_mono"] = values["captured_at_mono"]
    if "sequence" in overrides and "cell_sequence" not in overrides:
        values["cell_sequence"] = max(0, values["sequence"] - 3)
    return api.Observation(**values)


@dataclass(frozen=True)
class ProposedSetpointRecord:
    """Allocator output used to isolate SafetyKernel from FleetAllocator."""

    unit_id: str
    direction: Any
    watts: int
    intent_id: str
    intent_expires_at_mono: float


def make_proposed_setpoints(
    api: SimpleNamespace,
    *,
    direction: Any | None = None,
    watts: int = 1_000,
    watts_by_unit: dict[str, int] | None = None,
    intent_expires_at_mono: float = 109.0,
) -> tuple[ProposedSetpointRecord, ...]:
    selected_direction = direction or api.Direction.DISCHARGE
    allocation = watts_by_unit if watts_by_unit is not None else {"mid": watts}
    return tuple(
        ProposedSetpointRecord(
            unit_id=unit_id,
            direction=selected_direction,
            watts=unit_watts,
            intent_id="intent-001",
            intent_expires_at_mono=intent_expires_at_mono,
        )
        for unit_id, unit_watts in sorted(allocation.items())
    )


def make_previous_observations(
    api: SimpleNamespace, current_observations: dict[str, Any]
) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    for unit_id, current in current_observations.items():
        previous[unit_id] = make_observation(
            api,
            unit_id=unit_id,
            captured_at_mono=current.captured_at_mono - 1.0,
            sequence=current.sequence - 1,
            system_soc_pct=current.system_soc_pct,
            bms_soc_pct=current.bms_soc_pct,
            cell_captured_at_mono=min(
                current.cell_captured_at_mono,
                current.captured_at_mono - 1.0,
            ),
            cell_sequence=current.cell_sequence - 1,
        )
    return previous


def evaluate(
    api: SimpleNamespace,
    *,
    proposed_setpoints: tuple[ProposedSetpointRecord, ...] | None = None,
    current_observations: dict[str, Any] | None = None,
    previous_observations: dict[str, Any] | None = None,
    policy: Any | None = None,
    now_mono: float = NOW,
) -> Any:
    current = (
        current_observations if current_observations is not None else {"mid": make_observation(api)}
    )
    previous = (
        previous_observations
        if previous_observations is not None
        else make_previous_observations(api, current)
    )
    return api.SafetyKernel().evaluate(
        proposed_setpoints if proposed_setpoints is not None else make_proposed_setpoints(api),
        current,
        previous,
        policy or make_policy(api),
        now_mono,
    )


def reasons(decision: Any) -> tuple[str, ...]:
    return tuple(decision.reason_codes)


def setpoints_by_unit(decision: Any) -> dict[str, Any]:
    return {setpoint.unit_id: setpoint for setpoint in decision.setpoints}


def assert_reason_contract(decision: Any) -> None:
    """Reasons are stable machine codes, without inventing an undocumented vocabulary."""

    decision_reasons = reasons(decision)
    assert decision_reasons
    assert len(decision_reasons) == len(set(decision_reasons))
    assert all(isinstance(reason, str) and reason.strip() for reason in decision_reasons)


def assert_rejected(decision: Any, api: SimpleNamespace) -> None:
    assert decision.status is api.DecisionStatus.REJECTED
    assert_reason_contract(decision)
    assert all(setpoint.watts == 0 for setpoint in decision.setpoints)


@pytest.mark.parametrize("direction", ["CHARGE", "DISCHARGE"])
def test_healthy_request_preserves_direction_and_magnitude_without_reversing_it(
    api: SimpleNamespace, direction: str
) -> None:
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=getattr(api.Direction, direction)
        ),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert_reason_contract(decision)
    setpoint = setpoints_by_unit(decision)["mid"]
    assert setpoint.direction is getattr(api.Direction, direction)
    assert setpoint.watts == 1_000


@pytest.mark.parametrize("observations", [{}, None])
def test_zero_is_permitted_even_without_usable_telemetry(
    api: SimpleNamespace, observations: dict[str, Any] | None
) -> None:
    missing_or_invalid = (
        {}
        if observations == {}
        else {
            "mid": make_observation(
                api,
                system_soc_pct=math.nan,
                active_faults=frozenset({"BMS_CRITICAL"}),
            )
        }
    )
    proposed = make_proposed_setpoints(api, direction=api.Direction.IDLE, watts=0)

    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations=missing_or_invalid,
        previous_observations={},
    )

    assert decision.status in {api.DecisionStatus.AUTHORIZED, api.DecisionStatus.REVOKED}
    assert_reason_contract(decision)
    assert all(setpoint.watts == 0 for setpoint in decision.setpoints)


def test_missing_selected_unit_fails_closed(api: SimpleNamespace) -> None:
    decision = evaluate(api, current_observations={}, previous_observations={})
    assert_rejected(decision, api)


def test_missing_previous_observation_fails_closed_for_nonzero_power(
    api: SimpleNamespace,
) -> None:
    decision = evaluate(api, previous_observations={})
    assert_rejected(decision, api)


@pytest.mark.parametrize(
    "quality",
    [
        "MISSING",
        "STALE",
        "BAD",
        "SUSPECT",
    ],
)
@pytest.mark.parametrize("field", REQUIRED_QUALITY_FIELDS)
def test_every_required_field_and_non_good_quality_class_fails_closed(
    api: SimpleNamespace, quality: str, field: str
) -> None:
    quality_map = {field: api.DataQuality.GOOD for field in REQUIRED_QUALITY_FIELDS}
    quality_map[field] = getattr(api.DataQuality, quality)
    observation = make_observation(api, quality=quality_map)

    decision = evaluate(api, current_observations={"mid": observation})

    assert_rejected(decision, api)


def test_overall_telemetry_age_boundary_is_inclusive_then_stale(api: SimpleNamespace) -> None:
    policy = make_policy(api, max_telemetry_age_s=5.0)
    on_boundary = make_observation(api, captured_at_mono=96.0)
    too_old = make_observation(api, captured_at_mono=95.999)

    allowed = evaluate(api, current_observations={"mid": on_boundary}, policy=policy)
    denied = evaluate(api, current_observations={"mid": too_old}, policy=policy)

    assert allowed.status is api.DecisionStatus.AUTHORIZED
    assert_rejected(denied, api)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_soc_pct", math.nan),
        ("bms_soc_pct", math.inf),
        ("soh_pct", -math.inf),
        ("battery_watts", math.nan),
        ("pack_voltage_v", math.inf),
        ("pack_current_a", math.nan),
        ("dynamic_charge_limit_w", math.inf),
        ("dynamic_discharge_limit_w", math.nan),
        ("cell_voltages_v", (3.3, 3.3, math.nan, 3.3)),
        ("temperatures_c", (25.0, math.inf)),
    ],
)
def test_nonfinite_safety_data_is_never_clamped_or_coerced(
    api: SimpleNamespace, field: str, value: Any
) -> None:
    observation = make_observation(api, **{field: value})
    decision = evaluate(api, current_observations={"mid": observation})
    assert_rejected(decision, api)


@pytest.mark.parametrize(
    ("direction", "soc", "permitted"),
    [
        ("DISCHARGE", 10.001, True),
        ("DISCHARGE", 10.0, False),
        ("DISCHARGE", 9.999, False),
        ("CHARGE", 89.999, True),
        ("CHARGE", 90.0, False),
        ("CHARGE", 90.001, False),
    ],
)
def test_soc_directional_boundaries_are_exact(
    api: SimpleNamespace, direction: str, soc: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=soc,
        bms_soc_pct=soc,
    )
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        system_soc_pct=soc,
        bms_soc_pct=soc,
    )
    proposed = make_proposed_setpoints(api, direction=getattr(api.Direction, direction))
    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api)


@pytest.mark.parametrize("delta, permitted", [(10.0, True), (10.001, False), (-10.001, False)])
def test_soc_jump_threshold_is_inclusive_and_direction_independent(
    api: SimpleNamespace, delta: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=50.0 + delta,
        bms_soc_pct=50.0 + delta,
    )
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
    )
    decision = evaluate(
        api,
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api)


@pytest.mark.parametrize("difference, permitted", [(5.0, True), (5.001, False), (-5.001, False)])
def test_system_and_bms_soc_disagreement_boundary(
    api: SimpleNamespace, difference: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=50.0 + difference,
        bms_soc_pct=50.0,
    )
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        system_soc_pct=50.0 + difference,
        bms_soc_pct=50.0,
    )
    decision = evaluate(
        api,
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api)


@pytest.mark.parametrize(
    "cells",
    [
        None,
        (3.30, 3.31, 3.29),
        (2.999, 3.30, 3.30, 3.30),
        (3.601, 3.30, 3.30, 3.30),
        (3.300, 3.351, 3.325, 3.330),
    ],
)
def test_cell_safety_failures_fail_closed(
    api: SimpleNamespace, cells: tuple[float, ...] | None
) -> None:
    observation = make_observation(api, cell_voltages_v=cells)
    decision = evaluate(api, current_observations={"mid": observation})
    assert_rejected(decision, api)


@pytest.mark.parametrize(
    "cells",
    [
        (3.000, 3.000, 3.000, 3.000),
        (3.600, 3.600, 3.600, 3.600),
        (3.300, 3.350, 3.325, 3.330),
    ],
)
def test_cell_voltage_and_imbalance_exact_boundaries_are_permitted(
    api: SimpleNamespace, cells: tuple[float, ...]
) -> None:
    decision = evaluate(
        api,
        current_observations={"mid": make_observation(api, cell_voltages_v=cells)},
    )
    assert decision.status is api.DecisionStatus.AUTHORIZED


def test_cell_age_boundary_is_inclusive_then_stale(api: SimpleNamespace) -> None:
    policy = make_policy(api, max_cell_age_s=5.0)
    allowed = evaluate(
        api,
        current_observations={"mid": make_observation(api, cell_captured_at_mono=96.0)},
        policy=policy,
    )
    denied = evaluate(
        api,
        current_observations={"mid": make_observation(api, cell_captured_at_mono=95.999)},
        policy=policy,
    )

    assert allowed.status is api.DecisionStatus.AUTHORIZED
    assert_rejected(denied, api)


@pytest.mark.parametrize(
    "temperatures",
    [
        None,
        (-0.001, 5.0),
        (25.0, 45.001),
        (20.0, 30.001),
    ],
)
def test_temperature_failures_have_exact_reasons(
    api: SimpleNamespace,
    temperatures: tuple[float, ...] | None,
) -> None:
    observation = make_observation(api, temperatures_c=temperatures)
    decision = evaluate(api, current_observations={"mid": observation})
    assert_rejected(decision, api)


@pytest.mark.parametrize("temperatures", [(0.0, 10.0), (35.0, 45.0)])
def test_temperature_exact_boundaries_are_permitted(
    api: SimpleNamespace, temperatures: tuple[float, ...]
) -> None:
    decision = evaluate(
        api,
        current_observations={"mid": make_observation(api, temperatures_c=temperatures)},
    )
    assert decision.status is api.DecisionStatus.AUTHORIZED


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("active_faults", "BMS_CRITICAL"),
        ("active_warnings", "PCS_Warning0_1"),
        ("active_warnings", "DCDC_Warning0_1"),
    ],
)
def test_blocking_faults_and_ee_calibration_warnings_fail_closed(
    api: SimpleNamespace, field: str, code: str
) -> None:
    observation = make_observation(api, **{field: frozenset({code})})
    decision = evaluate(api, current_observations={"mid": observation})
    assert_rejected(decision, api)


@pytest.mark.parametrize(
    ("direction", "observation_changes", "policy_changes", "expected_watts"),
    [
        ("CHARGE", {"dynamic_charge_limit_w": 1_200}, {}, 1_200),
        ("DISCHARGE", {"dynamic_discharge_limit_w": 1_300}, {}, 1_300),
        (
            "CHARGE",
            {},
            {"static_charge_limit_w_by_unit": {unit: 1_400 for unit in UNIT_IDS}},
            1_400,
        ),
        (
            "DISCHARGE",
            {},
            {"static_discharge_limit_w_by_unit": {unit: 1_500 for unit in UNIT_IDS}},
            1_500,
        ),
        (
            "DISCHARGE",
            {},
            {"apparent_power_limit_va_by_unit": {unit: 1_100 for unit in UNIT_IDS}},
            1_100,
        ),
    ],
)
def test_each_power_limit_clamps_directionally_and_explains_the_clamp(
    api: SimpleNamespace,
    direction: str,
    observation_changes: dict[str, Any],
    policy_changes: dict[str, Any],
    expected_watts: int,
) -> None:
    proposed = make_proposed_setpoints(
        api,
        direction=getattr(api.Direction, direction),
        watts=2_000,
    )
    observation = make_observation(api, **observation_changes)
    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations={"mid": observation},
        policy=make_policy(api, **policy_changes),
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert_reason_contract(decision)
    setpoint = setpoints_by_unit(decision)["mid"]
    assert setpoint.direction is getattr(api.Direction, direction)
    assert setpoint.watts == expected_watts


@pytest.mark.parametrize(
    ("direction", "field"),
    [
        ("CHARGE", "dynamic_charge_limit_w"),
        ("DISCHARGE", "dynamic_discharge_limit_w"),
    ],
)
def test_zero_dynamic_capability_rejects_nonzero_power(
    api: SimpleNamespace, direction: str, field: str
) -> None:
    proposed = make_proposed_setpoints(api, direction=getattr(api.Direction, direction))
    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations={"mid": make_observation(api, **{field: 0})},
    )
    assert_rejected(decision, api)


def test_ramp_limit_uses_current_signed_battery_power_and_control_interval(
    api: SimpleNamespace,
) -> None:
    ramp_limits = {unit: 500 for unit in UNIT_IDS}
    policy = make_policy(api, ramp_limit_w_per_s_by_unit=ramp_limits)
    observation = make_observation(api, battery_watts=0.0)
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=2_000),
        current_observations={"mid": observation},
        policy=policy,
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert_reason_contract(decision)
    assert setpoints_by_unit(decision)["mid"].watts == 500


def test_fleet_limit_clamps_total_without_multiplication(api: SimpleNamespace) -> None:
    proposed = make_proposed_setpoints(
        api,
        watts_by_unit={"lhs": 2_000, "mid": 2_000, "rhs": 2_000},
    )
    current = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    policy = make_policy(api, fleet_discharge_limit_w=4_500)

    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations=current,
        policy=policy,
    )
    setpoints = setpoints_by_unit(decision)

    assert decision.status is api.DecisionStatus.CLAMPED
    assert_reason_contract(decision)
    assert sum(point.watts for point in setpoints.values()) == 4_500
    assert all(0 <= point.watts <= 3_000 for point in setpoints.values())


def test_authorization_expiry_is_minimum_of_kernel_ttl_and_intent_expiry(
    api: SimpleNamespace,
) -> None:
    policy = make_policy(api, authorization_lifetime_s=3.0)
    proposed = make_proposed_setpoints(api, intent_expires_at_mono=101.25)
    decision = evaluate(api, proposed_setpoints=proposed, policy=policy)
    assert setpoints_by_unit(decision)["mid"].authorization_expires_at_mono == 101.25


def test_authorization_never_outlives_telemetry_or_cell_freshness(api: SimpleNamespace) -> None:
    policy = make_policy(
        api,
        authorization_lifetime_s=10.0,
        max_telemetry_age_s=1.25,
        max_cell_age_s=1.5,
    )
    observation = make_observation(api, captured_at_mono=100.0, cell_captured_at_mono=100.0)
    decision = evaluate(api, current_observations={"mid": observation}, policy=policy)
    assert setpoints_by_unit(decision)["mid"].authorization_expires_at_mono == 101.25


def test_simultaneous_denials_retain_every_independently_observed_cause(
    api: SimpleNamespace,
) -> None:
    combined_observation = make_observation(
        api,
        captured_at_mono=90.0,
        system_soc_pct=95.0,
        bms_soc_pct=50.0,
        active_faults=frozenset({"BMS_CRITICAL"}),
    )
    combined_previous = make_observation(
        api,
        captured_at_mono=89.0,
        sequence=6,
        cell_captured_at_mono=89.0,
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
    )
    proposed = make_proposed_setpoints(api, direction=api.Direction.CHARGE)
    individual_cases = (
        (
            make_observation(api, captured_at_mono=90.0),
            make_observation(api, captured_at_mono=89.0, sequence=6),
        ),
        (
            make_observation(api, active_faults=frozenset({"BMS_CRITICAL"})),
            make_observation(api, captured_at_mono=99.0, sequence=6),
        ),
        (
            make_observation(api, system_soc_pct=61.0, bms_soc_pct=61.0),
            make_observation(
                api,
                captured_at_mono=99.0,
                sequence=6,
                system_soc_pct=50.0,
                bms_soc_pct=50.0,
            ),
        ),
        (
            make_observation(api, system_soc_pct=56.0, bms_soc_pct=50.0),
            make_observation(
                api,
                captured_at_mono=99.0,
                sequence=6,
                system_soc_pct=56.0,
                bms_soc_pct=50.0,
            ),
        ),
        (
            make_observation(api, system_soc_pct=95.0, bms_soc_pct=95.0),
            make_observation(
                api,
                captured_at_mono=99.0,
                sequence=6,
                system_soc_pct=95.0,
                bms_soc_pct=95.0,
            ),
        ),
    )
    individual_decisions = [
        evaluate(
            api,
            proposed_setpoints=proposed,
            current_observations={"mid": current},
            previous_observations={"mid": previous},
        )
        for current, previous in individual_cases
    ]
    combined = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations={"mid": combined_observation},
        previous_observations={"mid": combined_previous},
    )

    for decision in (*individual_decisions, combined):
        assert_rejected(decision, api)
    expected_causes = {reason for decision in individual_decisions for reason in reasons(decision)}
    assert set(reasons(combined)) == expected_causes
    assert reasons(combined) == reasons(
        evaluate(
            api,
            proposed_setpoints=proposed,
            current_observations={"mid": combined_observation},
            previous_observations={"mid": combined_previous},
        )
    )


def test_evaluation_is_deterministic_and_does_not_mutate_inputs(api: SimpleNamespace) -> None:
    proposed = make_proposed_setpoints(api)
    current = {"mid": make_observation(api)}
    previous = make_previous_observations(api, current)
    policy = make_policy(api)
    before_proposed = tuple(proposed)
    before_current = dict(current)
    before_previous = dict(previous)

    first = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations=current,
        previous_observations=previous,
        policy=policy,
    )
    second = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations=current,
        previous_observations=previous,
        policy=policy,
    )

    assert first == second
    assert proposed == before_proposed
    assert current == before_current
    assert previous == before_previous
