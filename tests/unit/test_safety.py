"""S0 contract tests for the deterministic, fail-closed safety kernel.

These tests intentionally exercise only the public ``energypod.domain`` and
``energypod.application`` packages.  Imports are resolved by a fixture so the
suite still collects before the test-first implementation exists.
"""

from __future__ import annotations

import importlib
import itertools
import math
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
        "IntentSource",
        "Observation",
        "PowerIntent",
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
        "blocking_warning_codes": frozenset(
            {"PCS_Warning0_1", "DCDC_Warning0_1"}
        ),
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
        "temperatures_c": (24.0, 25.0, 26.0, 25.0),
        "active_faults": frozenset(),
        "active_warnings": frozenset(),
        "quality": {
            field: api.DataQuality.GOOD for field in REQUIRED_QUALITY_FIELDS
        },
        "previous_system_soc_pct": 50.0,
        "previous_bms_soc_pct": 50.0,
        "previous_captured_at_mono": 99.0,
    }
    values.update(overrides)
    return api.Observation(**values)


def make_intent(
    api: SimpleNamespace,
    *,
    direction: Any | None = None,
    watts: int = 1_000,
    selected_unit_ids: frozenset[str] = frozenset({"mid"}),
    created_at_mono: float = 99.0,
    duration_s: float = 10.0,
) -> Any:
    return api.PowerIntent(
        id="intent-001",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=selected_unit_ids,
        direction=direction or api.Direction.DISCHARGE,
        watts=watts,
        duration_s=duration_s,
        created_at_mono=created_at_mono,
        actor_identity="operator@example.test",
    )


def evaluate(
    api: SimpleNamespace,
    *,
    intent: Any | None = None,
    observations: dict[str, Any] | None = None,
    policy: Any | None = None,
    now_mono: float = NOW,
) -> Any:
    return api.SafetyKernel().evaluate(
        intent or make_intent(api),
        observations
        if observations is not None
        else {"mid": make_observation(api)},
        policy or make_policy(api),
        now_mono,
    )


def reasons(decision: Any) -> tuple[str, ...]:
    return tuple(decision.reason_codes)


def setpoints_by_unit(decision: Any) -> dict[str, Any]:
    return {setpoint.unit_id: setpoint for setpoint in decision.setpoints}


def assert_rejected(decision: Any, api: SimpleNamespace, *expected: str) -> None:
    assert decision.status is api.DecisionStatus.REJECTED
    assert reasons(decision) == expected
    assert all(setpoint.active_watts == 0 for setpoint in decision.setpoints)


@pytest.mark.parametrize("direction, expected_signed_watts", [("CHARGE", -1_000), ("DISCHARGE", 1_000)])
def test_healthy_request_is_authorized_with_protocol_sign_at_the_setpoint_edge(
    api: SimpleNamespace, direction: str, expected_signed_watts: int
) -> None:
    decision = evaluate(api, intent=make_intent(api, direction=getattr(api.Direction, direction)))

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("AUTHORIZED",)
    assert setpoints_by_unit(decision)["mid"].active_watts == expected_signed_watts


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
    intent = make_intent(api, direction=api.Direction.IDLE, watts=0)

    decision = evaluate(api, intent=intent, observations=missing_or_invalid)

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("ZERO_REQUEST",)
    assert setpoints_by_unit(decision)["mid"].active_watts == 0


def test_missing_selected_unit_fails_closed(api: SimpleNamespace) -> None:
    decision = evaluate(api, observations={})
    assert_rejected(decision, api, "OBSERVATION_MISSING")


@pytest.mark.parametrize(
    ("quality", "expected_reason"),
    [
        ("MISSING", "SYSTEM_SOC_MISSING"),
        ("STALE", "SYSTEM_SOC_STALE"),
        ("INVALID", "SYSTEM_SOC_INVALID"),
        ("SUSPECT", "SYSTEM_SOC_SUSPECT"),
    ],
)
def test_every_non_good_quality_class_fails_closed(
    api: SimpleNamespace, quality: str, expected_reason: str
) -> None:
    quality_map = {
        field: api.DataQuality.GOOD for field in REQUIRED_QUALITY_FIELDS
    }
    quality_map["system_soc_pct"] = getattr(api.DataQuality, quality)
    observation = make_observation(api, quality=quality_map)

    decision = evaluate(api, observations={"mid": observation})

    assert_rejected(decision, api, expected_reason)


def test_overall_telemetry_age_boundary_is_inclusive_then_stale(api: SimpleNamespace) -> None:
    policy = make_policy(api, max_telemetry_age_s=5.0)
    on_boundary = make_observation(api, captured_at_mono=96.0)
    too_old = make_observation(api, captured_at_mono=95.999)

    allowed = evaluate(api, observations={"mid": on_boundary}, policy=policy)
    denied = evaluate(api, observations={"mid": too_old}, policy=policy)

    assert allowed.status is api.DecisionStatus.AUTHORIZED
    assert_rejected(denied, api, "TELEMETRY_STALE")


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("system_soc_pct", math.nan, "SYSTEM_SOC_NONFINITE"),
        ("bms_soc_pct", math.inf, "BMS_SOC_NONFINITE"),
        ("soh_pct", -math.inf, "SOH_NONFINITE"),
        ("battery_watts", math.nan, "BATTERY_POWER_NONFINITE"),
        ("pack_voltage_v", math.inf, "PACK_VOLTAGE_NONFINITE"),
        ("pack_current_a", math.nan, "PACK_CURRENT_NONFINITE"),
        ("dynamic_charge_limit_w", math.inf, "DYNAMIC_CHARGE_LIMIT_NONFINITE"),
        ("dynamic_discharge_limit_w", math.nan, "DYNAMIC_DISCHARGE_LIMIT_NONFINITE"),
        ("cell_voltages_v", (3.3, 3.3, math.nan, 3.3), "CELL_VOLTAGE_NONFINITE"),
        ("temperatures_c", (25.0, math.inf), "TEMPERATURE_NONFINITE"),
    ],
)
def test_nonfinite_safety_data_is_never_clamped_or_coerced(
    api: SimpleNamespace, field: str, value: Any, expected_reason: str
) -> None:
    observation = make_observation(api, **{field: value})
    decision = evaluate(api, observations={"mid": observation})
    assert_rejected(decision, api, expected_reason)


@pytest.mark.parametrize(
    ("direction", "soc", "permitted", "reason"),
    [
        ("DISCHARGE", 10.001, True, None),
        ("DISCHARGE", 10.0, False, "SOC_AT_OR_BELOW_MINIMUM"),
        ("DISCHARGE", 9.999, False, "SOC_AT_OR_BELOW_MINIMUM"),
        ("CHARGE", 89.999, True, None),
        ("CHARGE", 90.0, False, "SOC_AT_OR_ABOVE_MAXIMUM"),
        ("CHARGE", 90.001, False, "SOC_AT_OR_ABOVE_MAXIMUM"),
    ],
)
def test_soc_directional_boundaries_are_exact(
    api: SimpleNamespace, direction: str, soc: float, permitted: bool, reason: str | None
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=soc,
        bms_soc_pct=soc,
        previous_system_soc_pct=soc,
        previous_bms_soc_pct=soc,
    )
    intent = make_intent(api, direction=getattr(api.Direction, direction))
    decision = evaluate(api, intent=intent, observations={"mid": observation})

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api, reason)


@pytest.mark.parametrize("delta, permitted", [(10.0, True), (10.001, False), (-10.001, False)])
def test_soc_jump_threshold_is_inclusive_and_direction_independent(
    api: SimpleNamespace, delta: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=50.0 + delta,
        bms_soc_pct=50.0 + delta,
        previous_system_soc_pct=50.0,
        previous_bms_soc_pct=50.0,
    )
    decision = evaluate(api, observations={"mid": observation})

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api, "SOC_JUMP_DETECTED")


@pytest.mark.parametrize("difference, permitted", [(5.0, True), (5.001, False), (-5.001, False)])
def test_system_and_bms_soc_disagreement_boundary(
    api: SimpleNamespace, difference: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=50.0 + difference,
        bms_soc_pct=50.0,
        previous_system_soc_pct=50.0 + difference,
        previous_bms_soc_pct=50.0,
    )
    decision = evaluate(api, observations={"mid": observation})

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api, "SOC_SOURCES_DISAGREE")


@pytest.mark.parametrize(
    ("cells", "expected_reason"),
    [
        (None, "CELL_DATA_MISSING"),
        ((3.30, 3.31, 3.29), "CELL_DATA_INCOMPLETE"),
        ((2.999, 3.30, 3.30, 3.30), "CELL_VOLTAGE_BELOW_MINIMUM"),
        ((3.601, 3.30, 3.30, 3.30), "CELL_VOLTAGE_ABOVE_MAXIMUM"),
        ((3.300, 3.351, 3.325, 3.330), "CELL_IMBALANCE_ABOVE_MAXIMUM"),
    ],
)
def test_cell_safety_failures_have_exact_reasons(
    api: SimpleNamespace, cells: tuple[float, ...] | None, expected_reason: str
) -> None:
    observation = make_observation(api, cell_voltages_v=cells)
    decision = evaluate(api, observations={"mid": observation})
    assert_rejected(decision, api, expected_reason)


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
    decision = evaluate(api, observations={"mid": make_observation(api, cell_voltages_v=cells)})
    assert decision.status is api.DecisionStatus.AUTHORIZED


def test_cell_age_boundary_is_inclusive_then_stale(api: SimpleNamespace) -> None:
    policy = make_policy(api, max_cell_age_s=5.0)
    allowed = evaluate(
        api,
        observations={"mid": make_observation(api, cell_captured_at_mono=96.0)},
        policy=policy,
    )
    denied = evaluate(
        api,
        observations={"mid": make_observation(api, cell_captured_at_mono=95.999)},
        policy=policy,
    )

    assert allowed.status is api.DecisionStatus.AUTHORIZED
    assert_rejected(denied, api, "CELL_DATA_STALE")


@pytest.mark.parametrize(
    ("temperatures", "expected_reason"),
    [
        (None, "TEMPERATURE_DATA_MISSING"),
        ((-0.001, 5.0), "TEMPERATURE_BELOW_MINIMUM"),
        ((25.0, 45.001), "TEMPERATURE_ABOVE_MAXIMUM"),
        ((20.0, 30.001), "TEMPERATURE_SPREAD_ABOVE_MAXIMUM"),
    ],
)
def test_temperature_failures_have_exact_reasons(
    api: SimpleNamespace,
    temperatures: tuple[float, ...] | None,
    expected_reason: str,
) -> None:
    observation = make_observation(api, temperatures_c=temperatures)
    decision = evaluate(api, observations={"mid": observation})
    assert_rejected(decision, api, expected_reason)


@pytest.mark.parametrize("temperatures", [(0.0, 10.0), (35.0, 45.0)])
def test_temperature_exact_boundaries_are_permitted(
    api: SimpleNamespace, temperatures: tuple[float, ...]
) -> None:
    decision = evaluate(
        api, observations={"mid": make_observation(api, temperatures_c=temperatures)}
    )
    assert decision.status is api.DecisionStatus.AUTHORIZED


@pytest.mark.parametrize(
    ("field", "code", "expected_reason"),
    [
        ("active_faults", "BMS_CRITICAL", "BLOCKING_FAULT:BMS_CRITICAL"),
        ("active_warnings", "PCS_Warning0_1", "BLOCKING_WARNING:PCS_Warning0_1"),
        ("active_warnings", "DCDC_Warning0_1", "BLOCKING_WARNING:DCDC_Warning0_1"),
    ],
)
def test_blocking_faults_and_ee_calibration_warnings_fail_closed(
    api: SimpleNamespace, field: str, code: str, expected_reason: str
) -> None:
    observation = make_observation(api, **{field: frozenset({code})})
    decision = evaluate(api, observations={"mid": observation})
    assert_rejected(decision, api, expected_reason)


def test_nonblocking_warning_remains_visible_but_does_not_inhibit(api: SimpleNamespace) -> None:
    observation = make_observation(api, active_warnings=frozenset({"FILTER_SERVICE_DUE"}))
    decision = evaluate(api, observations={"mid": observation})
    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("AUTHORIZED",)


@pytest.mark.parametrize(
    ("direction", "observation_changes", "policy_changes", "expected_watts", "reason"),
    [
        ("CHARGE", {"dynamic_charge_limit_w": 1_200}, {}, -1_200, "DYNAMIC_CHARGE_LIMIT"),
        ("DISCHARGE", {"dynamic_discharge_limit_w": 1_300}, {}, 1_300, "DYNAMIC_DISCHARGE_LIMIT"),
        (
            "CHARGE",
            {},
            {"static_charge_limit_w_by_unit": {unit: 1_400 for unit in UNIT_IDS}},
            -1_400,
            "STATIC_UNIT_CHARGE_LIMIT",
        ),
        (
            "DISCHARGE",
            {},
            {"static_discharge_limit_w_by_unit": {unit: 1_500 for unit in UNIT_IDS}},
            1_500,
            "STATIC_UNIT_DISCHARGE_LIMIT",
        ),
        (
            "DISCHARGE",
            {},
            {"apparent_power_limit_va_by_unit": {unit: 1_100 for unit in UNIT_IDS}},
            1_100,
            "APPARENT_POWER_LIMIT",
        ),
    ],
)
def test_each_power_limit_clamps_directionally_with_an_exact_reason(
    api: SimpleNamespace,
    direction: str,
    observation_changes: dict[str, Any],
    policy_changes: dict[str, Any],
    expected_watts: int,
    reason: str,
) -> None:
    intent = make_intent(api, direction=getattr(api.Direction, direction), watts=2_000)
    observation = make_observation(api, **observation_changes)
    decision = evaluate(
        api,
        intent=intent,
        observations={"mid": observation},
        policy=make_policy(api, **policy_changes),
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert reasons(decision) == (reason,)
    assert setpoints_by_unit(decision)["mid"].active_watts == expected_watts


@pytest.mark.parametrize(
    ("direction", "field", "reason"),
    [
        ("CHARGE", "dynamic_charge_limit_w", "DYNAMIC_CHARGE_LIMIT_ZERO"),
        ("DISCHARGE", "dynamic_discharge_limit_w", "DYNAMIC_DISCHARGE_LIMIT_ZERO"),
    ],
)
def test_zero_dynamic_capability_rejects_nonzero_power(
    api: SimpleNamespace, direction: str, field: str, reason: str
) -> None:
    intent = make_intent(api, direction=getattr(api.Direction, direction))
    decision = evaluate(
        api,
        intent=intent,
        observations={"mid": make_observation(api, **{field: 0})},
    )
    assert_rejected(decision, api, reason)


def test_ramp_limit_uses_current_signed_battery_power_and_control_interval(
    api: SimpleNamespace,
) -> None:
    ramp_limits = {unit: 500 for unit in UNIT_IDS}
    policy = make_policy(api, ramp_limit_w_per_s_by_unit=ramp_limits)
    observation = make_observation(api, battery_watts=0.0)
    decision = evaluate(
        api,
        intent=make_intent(api, watts=2_000),
        observations={"mid": observation},
        policy=policy,
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert reasons(decision) == ("RAMP_LIMIT",)
    assert setpoints_by_unit(decision)["mid"].active_watts == 500


def test_fleet_limit_clamps_total_without_multiplication(api: SimpleNamespace) -> None:
    intent = make_intent(
        api,
        watts=6_000,
        selected_unit_ids=frozenset({"lhs", "mid", "rhs"}),
    )
    observations = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    policy = make_policy(api, fleet_discharge_limit_w=4_500)

    decision = evaluate(api, intent=intent, observations=observations, policy=policy)
    setpoints = setpoints_by_unit(decision)

    assert decision.status is api.DecisionStatus.CLAMPED
    assert reasons(decision) == ("FLEET_DISCHARGE_LIMIT",)
    assert sum(point.active_watts for point in setpoints.values()) == 4_500
    assert all(0 <= point.active_watts <= 3_000 for point in setpoints.values())


def test_fleet_allocation_is_permutation_invariant_and_exact(api: SimpleNamespace) -> None:
    intent = make_intent(
        api,
        watts=4_001,
        selected_unit_ids=frozenset({"lhs", "mid", "rhs"}),
    )
    observations = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    expected: dict[str, int] | None = None

    for order in itertools.permutations(UNIT_IDS):
        ordered = {unit: observations[unit] for unit in order}
        decision = evaluate(api, intent=intent, observations=ordered)
        allocation = {
            unit: point.active_watts for unit, point in setpoints_by_unit(decision).items()
        }
        assert sum(allocation.values()) == 4_001
        if expected is None:
            expected = allocation
        else:
            assert allocation == expected


def test_authorization_expiry_is_minimum_of_kernel_ttl_and_intent_expiry(
    api: SimpleNamespace,
) -> None:
    policy = make_policy(api, authorization_lifetime_s=3.0)
    intent = make_intent(api, created_at_mono=100.0, duration_s=1.25)
    decision = evaluate(api, intent=intent, policy=policy)
    assert setpoints_by_unit(decision)["mid"].authorization_expires_at_mono == 101.25


def test_authorization_never_outlives_telemetry_or_cell_freshness(api: SimpleNamespace) -> None:
    policy = make_policy(
        api,
        authorization_lifetime_s=10.0,
        max_telemetry_age_s=1.25,
        max_cell_age_s=1.5,
    )
    observation = make_observation(api, captured_at_mono=100.0, cell_captured_at_mono=100.0)
    decision = evaluate(api, observations={"mid": observation}, policy=policy)
    assert setpoints_by_unit(decision)["mid"].authorization_expires_at_mono == 101.25


def test_simultaneous_denials_preserve_pipeline_reason_order(api: SimpleNamespace) -> None:
    observation = make_observation(
        api,
        captured_at_mono=90.0,
        system_soc_pct=95.0,
        bms_soc_pct=50.0,
        previous_system_soc_pct=50.0,
        active_faults=frozenset({"BMS_CRITICAL"}),
    )
    intent = make_intent(api, direction=api.Direction.CHARGE)
    decision = evaluate(api, intent=intent, observations={"mid": observation})

    assert_rejected(
        decision,
        api,
        "TELEMETRY_STALE",
        "BLOCKING_FAULT:BMS_CRITICAL",
        "SOC_JUMP_DETECTED",
        "SOC_SOURCES_DISAGREE",
        "SOC_AT_OR_ABOVE_MAXIMUM",
    )


def test_evaluation_is_deterministic_and_does_not_mutate_inputs(api: SimpleNamespace) -> None:
    intent = make_intent(api)
    observations = {"mid": make_observation(api)}
    policy = make_policy(api)
    before_observations = dict(observations)

    first = evaluate(api, intent=intent, observations=observations, policy=policy)
    second = evaluate(api, intent=intent, observations=observations, policy=policy)

    assert first == second
    assert observations == before_observations
