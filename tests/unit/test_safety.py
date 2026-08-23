"""S0 contract tests for the deterministic, fail-closed safety kernel.

These tests intentionally exercise only the public ``energypod.domain`` and
``energypod.application`` packages.  Imports are resolved by a fixture so the
suite still collects before the test-first implementation exists.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

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


def make_raw_observation(api: SimpleNamespace, **overrides: Any) -> SimpleNamespace:
    """Bypass domain validation only to probe SafetyKernel's defensive boundary."""

    valid = make_observation(api)
    values = {name: getattr(valid, name) for name in type(valid).model_fields}
    values.update(overrides)
    return SimpleNamespace(**values)


@dataclass(frozen=True)
class ProposedSetpointRecord:
    """Allocator output used to isolate SafetyKernel from FleetAllocator."""

    unit_id: str
    direction: Any
    watts: int
    intent_id: str
    intent_expires_at_mono: float
    reactive_vars: int = 0
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": only the
    # allocator's optimizer-charge proposals carry the export-bounded flag.
    export_bounded: bool = False


def make_proposed_setpoints(
    api: SimpleNamespace,
    *,
    direction: Any | None = None,
    watts: int = 1_000,
    watts_by_unit: dict[str, int] | None = None,
    intent_expires_at_mono: float = 109.0,
    reactive_vars: int = 0,
    export_bounded: bool = False,
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
            reactive_vars=reactive_vars,
            export_bounded=export_bounded,
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
    assert reasons(decision) == ("safety_checks_passed",)
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
            "mid": make_raw_observation(
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
    assert "previous_observation_missing" in reasons(decision)


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
    assert f"quality_{field}" in reasons(decision)


def test_overall_telemetry_age_boundary_is_inclusive_then_stale(api: SimpleNamespace) -> None:
    policy = make_policy(api, max_telemetry_age_s=5.0)
    on_boundary = make_observation(api, captured_at_mono=96.0)
    too_old = make_observation(api, captured_at_mono=95.999)

    allowed = evaluate(api, current_observations={"mid": on_boundary}, policy=policy)
    denied = evaluate(api, current_observations={"mid": too_old}, policy=policy)

    assert allowed.status is api.DecisionStatus.AUTHORIZED
    assert_rejected(denied, api)
    assert "telemetry_stale" in reasons(denied)


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
def test_domain_rejects_nonfinite_safety_data_before_control(
    api: SimpleNamespace, field: str, value: Any
) -> None:
    with pytest.raises(ValueError, match="finite"):
        make_observation(api, **{field: value})


@pytest.mark.parametrize("field", ["max_soc_jump_pct", "max_soc_disagreement_pct"])
def test_policy_rejects_degenerate_zero_soc_tolerances(api: SimpleNamespace, field: str) -> None:
    # CONTINUITY deferred P2: a zero SOC-jump or zero SOC-disagreement
    # tolerance is a degenerate policy — every live observation would violate
    # it and the fleet could never re-arm — so the model refuses it rather
    # than letting a mis-typed config arm a permanent inhibit.
    with pytest.raises(ValidationError, match="positive"):
        make_policy(api, **{field: 0.0})


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
def test_safety_kernel_defensively_rejects_raw_nonfinite_data(
    api: SimpleNamespace, field: str, value: Any
) -> None:
    observation = make_raw_observation(api, **{field: value})
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        cell_sequence=3,
    )
    decision = evaluate(
        api,
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )
    assert_rejected(decision, api)
    assert "nonfinite_safety_data" in reasons(decision)


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


@pytest.mark.parametrize("difference", [5.0, 5.001, -5.001, -40.0, 44.0])
def test_system_and_bms_soc_divergence_never_denies_power(
    api: SimpleNamespace, difference: float
) -> None:
    """2026-08-24 operator ruling: "If there's a disagreement, re-sync based
    on whatever the battery says."  The BMS SOC is the authoritative SOC, so
    a system-vs-BMS divergence -- however large, in either direction -- is
    never a deny reason.  The stale system word (the tiered read plan serves
    the system block once per connection) must not veto power the battery's
    own figure says is safe."""
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

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert "soc_disagreement" not in reasons(decision)
    assert all(setpoint.watts > 0 for setpoint in decision.setpoints)


@pytest.mark.parametrize(
    ("difference", "carries_note"), [(5.0, False), (5.001, True), (-5.001, True)]
)
def test_soc_divergence_note_boundary_is_inclusive_and_informational(
    api: SimpleNamespace, difference: float, carries_note: bool
) -> None:
    """The old ``soc_disagreement`` deny boundary survives as the threshold
    of an INFORMATIONAL signal: beyond it the decision carries
    ``soc_disagreement_observed`` -- an audit/console warning only, never a
    rejection -- and at or under it the divergence is not even worth a note."""
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

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert ("soc_disagreement_observed" in reasons(decision)) is carries_note
    assert "soc_disagreement" not in reasons(decision)


def test_soc_divergence_note_never_appears_on_a_rejected_decision(
    api: SimpleNamespace,
) -> None:
    """The note is warning-tier: a decision rejected for an unrelated cause
    carries only deny reasons, so the informational code can never be
    confused with a blocking one on the rejecting path."""
    observation = make_observation(
        api,
        system_soc_pct=56.0,
        bms_soc_pct=50.0,
        temperatures_c=(25.0, 45.001),
    )
    decision = evaluate(api, current_observations={"mid": observation})

    assert_rejected(decision, api)
    assert "temperature_high" in reasons(decision)
    assert "soc_disagreement" not in reasons(decision)
    assert "soc_disagreement_observed" not in reasons(decision)


def test_zero_watt_and_stop_proposals_are_unaffected_by_soc_divergence(
    api: SimpleNamespace,
) -> None:
    """Zero is always permitted: a stop stays ``stop_authorized`` and a
    zero-watt non-participant with wildly divergent SOCs neither vetoes the
    participating unit nor accrues a denial of its own."""
    stop = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, direction=api.Direction.IDLE, watts=0),
        current_observations={"mid": make_observation(api, system_soc_pct=95.0, bms_soc_pct=5.0)},
    )
    assert stop.status is api.DecisionStatus.AUTHORIZED
    assert reasons(stop) == ("stop_authorized",)

    divergent_mid = make_observation(api, unit_id="mid", system_soc_pct=95.0, bms_soc_pct=5.0)
    clean_lhs = make_observation(api, unit_id="lhs")
    mixed = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts_by_unit={"mid": 0, "lhs": 1_000}),
        current_observations={"mid": divergent_mid, "lhs": clean_lhs},
        previous_observations=make_previous_observations(
            api, {"mid": divergent_mid, "lhs": clean_lhs}
        ),
    )
    assert mixed.status is api.DecisionStatus.AUTHORIZED
    assert setpoints_by_unit(mixed)["mid"].watts == 0
    assert setpoints_by_unit(mixed)["lhs"].watts == 1_000
    assert "soc_disagreement" not in reasons(mixed)


@pytest.mark.parametrize(
    ("direction", "system_soc", "bms_soc", "permitted"),
    [
        # The BMS figure is authoritative for every SOC bound: the old
        # kernel judged the system word and denied these first two rows.
        ("DISCHARGE", 9.5, 50.0, True),
        ("CHARGE", 95.0, 50.0, True),
        # ... while the floor and ceiling still block exactly as before,
        # now evaluated against the battery's own figure.
        ("DISCHARGE", 50.0, 10.0, False),
        ("DISCHARGE", 50.0, 9.999, False),
        ("CHARGE", 50.0, 90.0, False),
        ("CHARGE", 50.0, 90.001, False),
    ],
)
def test_soc_bounds_are_judged_on_the_authoritative_bms_soc(
    api: SimpleNamespace,
    direction: str,
    system_soc: float,
    bms_soc: float,
    permitted: bool,
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=system_soc,
        bms_soc_pct=bms_soc,
    )
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        system_soc_pct=system_soc,
        bms_soc_pct=bms_soc,
    )
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=getattr(api.Direction, direction)
        ),
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )

    if permitted:
        assert decision.status is api.DecisionStatus.AUTHORIZED
    else:
        assert_rejected(decision, api)
        expected = (
            "soc_above_charge_ceiling" if direction == "CHARGE" else "soc_below_discharge_floor"
        )
        assert expected in reasons(decision)


@pytest.mark.parametrize(
    ("system_now", "bms_now", "permitted"),
    [
        # A jumping system word with a steady BMS figure is not a jump: the
        # stale system SOC (frozen at its one-per-connection read) must not
        # trip the protection when the battery's own figure is coherent.
        (61.0, 50.0, True),
        # The BMS figure jumping beyond the tolerance still denies.
        (50.0, 61.0, False),
        (50.0, 39.999, False),
    ],
)
def test_soc_jump_is_judged_on_the_authoritative_bms_soc(
    api: SimpleNamespace, system_now: float, bms_now: float, permitted: bool
) -> None:
    observation = make_observation(
        api,
        system_soc_pct=system_now,
        bms_soc_pct=bms_now,
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
        assert "soc_jump" in reasons(decision)


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
    observation = (
        make_raw_observation(api, cell_voltages_v=None)
        if cells is None
        else make_observation(api, cell_voltages_v=cells)
    )
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
    assert "cell_data_stale" in reasons(denied)


def test_unchanged_cell_sequence_is_permitted_while_cell_data_is_fresh(
    api: SimpleNamespace,
) -> None:
    """Cell blocks poll slower than the control rate; an unchanged cell sequence
    between consecutive observations must not fail closed on its own."""
    current = make_observation(api)
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=current.cell_captured_at_mono,
        cell_sequence=current.cell_sequence,
    )
    decision = evaluate(
        api,
        current_observations={"mid": current},
        previous_observations={"mid": previous},
    )
    assert decision.status is api.DecisionStatus.AUTHORIZED


def test_regressed_cell_sequence_fails_closed(api: SimpleNamespace) -> None:
    current = make_observation(api)
    previous = make_observation(
        api,
        captured_at_mono=99.0,
        sequence=6,
        cell_captured_at_mono=99.0,
        cell_sequence=current.cell_sequence + 1,
    )
    decision = evaluate(
        api,
        current_observations={"mid": current},
        previous_observations={"mid": previous},
    )
    assert_rejected(decision, api)
    assert "cell_sequence_invalid" in reasons(decision)


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
    observation = (
        make_raw_observation(api, temperatures_c=None)
        if temperatures is None
        else make_observation(api, temperatures_c=temperatures)
    )
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


def test_partially_eligible_fleet_authorizes_the_participating_units(
    api: SimpleNamespace,
) -> None:
    """2026-08-23 live rejection: MID sits at the 10% discharge floor, so the
    allocator proposes zero watts for it. A zero-watt proposal is explicit
    non-participation — no authority is ever minted for it — so the floor that
    (correctly) disqualifies MID must not veto the units that can deliver."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api,
            direction=api.Direction.DISCHARGE,
            watts_by_unit={"lhs": 250, "mid": 0, "rhs": 250},
        ),
        current_observations={
            "lhs": make_observation(api, unit_id="lhs"),
            "mid": make_observation(api, unit_id="mid", system_soc_pct=9.5, bms_soc_pct=9.5),
            "rhs": make_observation(api, unit_id="rhs"),
        },
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("safety_checks_passed",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["lhs"].watts == 250
    assert setpoints["mid"].watts == 0
    assert setpoints["mid"].direction is api.Direction.DISCHARGE
    assert setpoints["rhs"].watts == 250


def test_partially_eligible_charge_fleet_authorizes_the_participating_units(
    api: SimpleNamespace,
) -> None:
    """The charge twin of the discharge case: units above the SOC ceiling are
    proposed at zero watts and must not veto the units with charge headroom."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api,
            direction=api.Direction.CHARGE,
            watts_by_unit={"lhs": 0, "mid": 500, "rhs": 0},
        ),
        current_observations={
            "lhs": make_observation(api, unit_id="lhs", system_soc_pct=95.0, bms_soc_pct=95.0),
            "mid": make_observation(api, unit_id="mid"),
            "rhs": make_observation(api, unit_id="rhs", system_soc_pct=96.0, bms_soc_pct=96.0),
        },
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("safety_checks_passed",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["lhs"].watts == 0
    assert setpoints["mid"].watts == 500
    assert setpoints["rhs"].watts == 0


def test_zero_watt_non_participant_cannot_veto_the_fleet(api: SimpleNamespace) -> None:
    """An unobservable unit is proposed at zero watts precisely BECAUSE its
    headroom is unknowable; its stale telemetry must not halt the units that
    are commanded (the kernel still refuses to mint anything without coherent
    observations for every selected unit)."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts_by_unit={"lhs": 1_000, "mid": 0}),
        current_observations={
            "lhs": make_observation(api, unit_id="lhs"),
            "mid": make_observation(api, unit_id="mid", captured_at_mono=NOW - 999.0),
        },
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert setpoints_by_unit(decision)["mid"].watts == 0


@pytest.mark.parametrize("direction", ["CHARGE", "DISCHARGE"])
def test_active_intent_with_no_deliverable_watts_is_rejected_not_crashing(
    api: SimpleNamespace, direction: str
) -> None:
    """2026-08-23 live fleet halt: a single-unit charge into a pod whose BMS
    dynamic limit is 0 W (lhs at 98% SOC) yields an ALL-zero allocation. That
    is a legitimate "nothing deliverable" outcome — the honest answer is a
    REJECTED decision the kernel audits each tick, never an exception that
    ends the fleet task."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api,
            direction=getattr(api.Direction, direction),
            watts_by_unit={"lhs": 0},
        ),
        current_observations={"lhs": make_observation(api, unit_id="lhs")},
    )

    assert_rejected(decision, api)
    assert "zero_dynamic_capability" in reasons(decision)


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


def test_fleet_limit_remainder_keeps_the_exact_total(api: SimpleNamespace) -> None:
    proposed = make_proposed_setpoints(
        api,
        watts_by_unit={"lhs": 2_000, "mid": 2_000, "rhs": 2_000},
    )
    current = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    policy = make_policy(api, fleet_discharge_limit_w=4_501)

    decision = evaluate(
        api,
        proposed_setpoints=proposed,
        current_observations=current,
        policy=policy,
    )
    setpoints = setpoints_by_unit(decision)

    assert decision.status is api.DecisionStatus.CLAMPED
    assert_reason_contract(decision)
    assert sum(point.watts for point in setpoints.values()) == 4_501
    assert all(0 <= point.watts <= 2_000 for point in setpoints.values())


def test_fleet_limit_remainder_never_exceeds_requested_per_unit(api: SimpleNamespace) -> None:
    proposed = make_proposed_setpoints(api, watts_by_unit={"lhs": 10, "mid": 10, "rhs": 10})
    current = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    policy = make_policy(api, fleet_discharge_limit_w=29)

    decision = evaluate(
        api, proposed_setpoints=proposed, current_observations=current, policy=policy
    )
    setpoints = setpoints_by_unit(decision)

    assert decision.status is api.DecisionStatus.CLAMPED
    assert sum(point.watts for point in setpoints.values()) == 29
    assert all(0 <= point.watts <= 10 for point in setpoints.values())


def test_ramp_limit_accounts_for_signed_battery_power(api: SimpleNamespace) -> None:
    ramp = {unit: 200 for unit in UNIT_IDS}
    policy = make_policy(api, ramp_limit_w_per_s_by_unit=ramp)
    discharging = make_observation(api, battery_watts=500.0)

    discharge = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=1_000),
        current_observations={"mid": discharging},
        policy=policy,
    )
    assert discharge.status is api.DecisionStatus.CLAMPED
    assert setpoints_by_unit(discharge)["mid"].watts == 700

    charge = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=api.Direction.CHARGE, watts=1_000
        ),
        current_observations={"mid": discharging},
        policy=policy,
    )
    assert_rejected(charge, api)
    assert "zero_dynamic_capability" in reasons(charge)


def test_one_watt_is_the_inclusive_dynamic_capability_floor(api: SimpleNamespace) -> None:
    observation = make_observation(api, dynamic_discharge_limit_w=1)
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=1_000),
        current_observations={"mid": observation},
    )
    assert decision.status is api.DecisionStatus.CLAMPED
    assert setpoints_by_unit(decision)["mid"].watts == 1


def test_fleet_charge_limit_governs_charge_direction(api: SimpleNamespace) -> None:
    proposed = make_proposed_setpoints(
        api,
        direction=api.Direction.CHARGE,
        watts_by_unit={"lhs": 2_000, "mid": 2_000, "rhs": 2_000},
    )
    current = {unit: make_observation(api, unit_id=unit) for unit in UNIT_IDS}
    policy = make_policy(api, fleet_charge_limit_w=3_000)

    decision = evaluate(
        api, proposed_setpoints=proposed, current_observations=current, policy=policy
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert sum(point.watts for point in decision.setpoints) == 3_000


def test_reactive_power_derates_apparent_capability(api: SimpleNamespace) -> None:
    policy = make_policy(api, reactive_limit_var=5_000)
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=2_000, reactive_vars=4_800),
        policy=policy,
    )
    assert decision.status is api.DecisionStatus.CLAMPED
    assert setpoints_by_unit(decision)["mid"].watts == 1_400


def test_control_decision_is_immutable(api: SimpleNamespace) -> None:
    decision = evaluate(api)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        decision.status = api.DecisionStatus.REVOKED
    with pytest.raises((AttributeError, TypeError, ValueError)):
        decision.reason_codes = ()


def test_expired_intent_boundary_is_rejected_at_evaluation(api: SimpleNamespace) -> None:
    expired = evaluate(
        api, proposed_setpoints=make_proposed_setpoints(api, intent_expires_at_mono=NOW)
    )
    assert_rejected(expired, api)
    assert "intent_expired" in reasons(expired)
    expired_one_watt = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=1, intent_expires_at_mono=NOW),
    )
    assert_rejected(expired_one_watt, api)
    assert "intent_expired" in reasons(expired_one_watt)
    live = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, intent_expires_at_mono=NOW + 0.001),
    )
    assert live.status is api.DecisionStatus.AUTHORIZED


def test_noninteger_cell_sequence_is_rejected_defensively(api: SimpleNamespace) -> None:
    observation = make_raw_observation(api, cell_sequence="4")
    previous = make_observation(
        api, captured_at_mono=99.0, sequence=6, cell_captured_at_mono=99.0, cell_sequence=3
    )
    decision = evaluate(
        api,
        current_observations={"mid": observation},
        previous_observations={"mid": previous},
    )
    assert_rejected(decision, api)
    assert "cell_sequence_invalid" in reasons(decision)


def test_one_unit_proposed_twice_in_opposite_directions_is_rejected(api: SimpleNamespace) -> None:
    """Per-unit coherence replaces the fleet-wide ``mixed_directions``
    rejection (2026-08-24 concurrent operations): DIFFERENT units may run
    different directions in one cycle, but ONE unit can never be proposed
    twice -- the duplicate-unit defense still rejects the confused cycle."""
    mixed = (
        make_proposed_setpoints(api, direction=api.Direction.CHARGE, watts=500)[0],
        make_proposed_setpoints(api, direction=api.Direction.DISCHARGE, watts=500)[0],
    )
    assert mixed[0].unit_id == mixed[1].unit_id == "mid"
    decision = evaluate(api, proposed_setpoints=mixed)
    assert_rejected(decision, api)
    assert "duplicate_unit_setpoint" in reasons(decision)
    assert "mixed_directions" not in reasons(decision)


def test_noninteger_power_is_rejected_defensively(api: SimpleNamespace) -> None:
    invalid = ProposedSetpointRecord(
        unit_id="mid",
        direction=api.Direction.DISCHARGE,
        watts="5",  # type: ignore[arg-type]
        intent_id="intent-001",
        intent_expires_at_mono=109.0,
    )
    decision = evaluate(api, proposed_setpoints=(invalid,))
    assert_rejected(decision, api)
    assert "invalid_power" in reasons(decision)


def test_capture_times_equal_to_now_are_not_from_the_future(api: SimpleNamespace) -> None:
    observation = make_observation(api, captured_at_mono=NOW, cell_captured_at_mono=NOW)
    decision = evaluate(api, current_observations={"mid": observation})
    assert decision.status is api.DecisionStatus.AUTHORIZED


def test_core_reason_codes_are_stable_machine_vocabulary(api: SimpleNamespace) -> None:
    empty = evaluate(api, proposed_setpoints=())
    assert empty.status is api.DecisionStatus.REVOKED
    assert reasons(empty) == ("no_setpoints",)

    stop = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, direction=api.Direction.IDLE, watts=0),
    )
    assert stop.status is api.DecisionStatus.AUTHORIZED
    assert reasons(stop) == ("stop_authorized",)

    missing = evaluate(api, current_observations={})
    assert_rejected(missing, api)
    assert "observation_missing" in reasons(missing)

    duplicated = make_proposed_setpoints(api)
    duplicate = evaluate(api, proposed_setpoints=duplicated + duplicated)
    assert_rejected(duplicate, api)
    assert "duplicate_unit_setpoint" in reasons(duplicate)

    clamped = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, watts=100_000),
    )
    assert clamped.status is api.DecisionStatus.CLAMPED
    assert reasons(clamped) == ("power_clamped",)


# Mutation testing showed reason-code strings survive whenever tests only
# assert rejection. Each row below pins the exact machine code the kernel
# emits for one triggering condition, because operators and audit consumers
# match on these names.
REASON_CODE_CASES: tuple[pytest.Param, ...] = (
    pytest.param(
        "invalid_direction",
        lambda api: evaluate(
            api, proposed_setpoints=make_proposed_setpoints(api, direction="reverse")
        ),
        id="invalid_direction",
    ),
    pytest.param(
        "direction_power_mismatch",
        lambda api: evaluate(
            api,
            proposed_setpoints=make_proposed_setpoints(
                api, direction=api.Direction.IDLE, watts=500
            ),
        ),
        id="direction_power_mismatch",
    ),
    pytest.param(
        "unit_policy_missing",
        lambda api: evaluate(
            api,
            proposed_setpoints=make_proposed_setpoints(api, watts_by_unit={"ghost": 1_000}),
            current_observations={},
            previous_observations={},
        ),
        id="unit_policy_missing",
    ),
    pytest.param(
        "previous_observation_missing",
        lambda api: evaluate(api, previous_observations={}),
        id="previous_observation_missing",
    ),
    pytest.param(
        "lifecycle_not_controllable",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, lifecycle=api.UnitLifecycle.OBSERVE_ONLY)
            },
        ),
        id="lifecycle_not_controllable",
    ),
    pytest.param(
        "telemetry_from_future",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, captured_at_mono=105.0, cell_captured_at_mono=100.0)
            },
        ),
        id="telemetry_from_future",
    ),
    pytest.param(
        "cell_data_missing",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_raw_observation(api, cell_captured_at_mono=math.nan)},
            previous_observations={
                "mid": make_observation(
                    api,
                    captured_at_mono=99.0,
                    sequence=6,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                )
            },
        ),
        id="cell_data_missing",
    ),
    pytest.param(
        "cell_data_from_future",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api, cell_captured_at_mono=NOW + 0.5)},
        ),
        id="cell_data_from_future",
    ),
    pytest.param(
        "observation_time_invalid",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api)},
            previous_observations={
                "mid": make_raw_observation(
                    api,
                    captured_at_mono=math.nan,
                    sequence=6,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                )
            },
        ),
        id="observation_time_invalid",
    ),
    pytest.param(
        "observation_order_invalid",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api)},
            previous_observations={
                "mid": make_observation(
                    api,
                    captured_at_mono=100.0,
                    sequence=6,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                )
            },
        ),
        id="observation_order_invalid",
    ),
    pytest.param(
        "observation_sequence_invalid",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api)},
            previous_observations={
                "mid": make_observation(
                    api,
                    captured_at_mono=99.0,
                    sequence=7,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                )
            },
        ),
        id="observation_sequence_invalid",
    ),
    pytest.param(
        "observation_epoch_changed",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api)},
            previous_observations={
                "mid": make_observation(
                    api,
                    captured_at_mono=99.0,
                    sequence=6,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                    connection_epoch=5,
                )
            },
        ),
        id="observation_epoch_changed",
    ),
    pytest.param(
        "soc_below_discharge_floor",
        lambda api: evaluate(
            api,
            # The BMS SOC is authoritative for the floor (2026-08-24): the
            # system word sits comfortably above it while the battery's own
            # figure is below -- and the protection still denies.
            current_observations={
                "mid": make_observation(api, system_soc_pct=50.0, bms_soc_pct=9.5)
            },
        ),
        id="soc_below_discharge_floor",
    ),
    pytest.param(
        "soc_above_charge_ceiling",
        lambda api: evaluate(
            api,
            proposed_setpoints=make_proposed_setpoints(api, direction=api.Direction.CHARGE),
            current_observations={
                "mid": make_observation(api, system_soc_pct=50.0, bms_soc_pct=95.0)
            },
        ),
        id="soc_above_charge_ceiling",
    ),
    pytest.param(
        "soc_jump",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, system_soc_pct=50.0, bms_soc_pct=61.0)
            },
            previous_observations={
                "mid": make_observation(
                    api,
                    captured_at_mono=99.0,
                    sequence=6,
                    cell_captured_at_mono=99.0,
                    cell_sequence=3,
                    system_soc_pct=50.0,
                    bms_soc_pct=50.0,
                )
            },
        ),
        id="soc_jump",
    ),
    pytest.param(
        "cell_count_invalid",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api, cell_voltages_v=(3.30, 3.31, 3.29))},
        ),
        id="cell_count_invalid",
    ),
    pytest.param(
        "cell_voltage_low",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, cell_voltages_v=(2.999, 3.30, 3.30, 3.30))
            },
        ),
        id="cell_voltage_low",
    ),
    pytest.param(
        "cell_voltage_high",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, cell_voltages_v=(3.601, 3.30, 3.30, 3.30))
            },
        ),
        id="cell_voltage_high",
    ),
    pytest.param(
        "cell_imbalance",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, cell_voltages_v=(3.300, 3.351, 3.325, 3.330))
            },
        ),
        id="cell_imbalance",
    ),
    pytest.param(
        "temperatures_missing",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_raw_observation(api, temperatures_c=None)},
        ),
        id="temperatures_missing",
    ),
    pytest.param(
        "temperature_low",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api, temperatures_c=(-0.001, 5.0))},
        ),
        id="temperature_low",
    ),
    pytest.param(
        "temperature_high",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api, temperatures_c=(25.0, 45.001))},
        ),
        id="temperature_high",
    ),
    pytest.param(
        "temperature_spread",
        lambda api: evaluate(
            api,
            current_observations={"mid": make_observation(api, temperatures_c=(20.0, 30.001))},
        ),
        id="temperature_spread",
    ),
    pytest.param(
        "blocking_fault",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, active_faults=frozenset({"BMS_CRITICAL"}))
            },
        ),
        id="blocking_fault",
    ),
    pytest.param(
        "blocking_warning",
        lambda api: evaluate(
            api,
            current_observations={
                "mid": make_observation(api, active_warnings=frozenset({"PCS_Warning0_1"}))
            },
        ),
        id="blocking_warning",
    ),
)


@pytest.mark.parametrize(("expected_code", "evaluate_case"), REASON_CODE_CASES)
def test_each_triggering_condition_emits_its_pinned_reason_code(
    api: SimpleNamespace, expected_code: str, evaluate_case: Callable[[SimpleNamespace], Any]
) -> None:
    decision = evaluate_case(api)
    assert_rejected(decision, api)
    assert expected_code in reasons(decision)


def test_nonfinite_evaluation_time_fails_closed_under_its_own_name(
    api: SimpleNamespace,
) -> None:
    """A non-finite clock fails closed even though the code cannot surface yet.

    The zero setpoints minted on rejection expire at ``now``, so the domain's
    finite-expiry validation raises before a decision carrying
    ``invalid_evaluation_time`` could be returned: an invalid clock is still
    fail-closed, just by exception. The vocabulary is therefore pinned on the
    proposal gate itself so the machine code cannot drift silently while that
    construction order holds.
    """

    try:
        decision = evaluate(api, now_mono=math.nan)
    except (TypeError, ValueError):
        decision = None

    if decision is None:
        gate = api.SafetyKernel._proposal_reasons(  # white-box pin; see docstring
            make_proposed_setpoints(api), make_policy(api), math.nan
        )
        assert "invalid_evaluation_time" in gate
    else:
        assert_rejected(decision, api)
        assert "invalid_evaluation_time" in reasons(decision)


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
        system_soc_pct=50.0,
        bms_soc_pct=95.0,
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
            make_observation(api, system_soc_pct=50.0, bms_soc_pct=61.0),
            make_observation(
                api,
                captured_at_mono=99.0,
                sequence=6,
                system_soc_pct=50.0,
                bms_soc_pct=50.0,
            ),
        ),
        (
            make_observation(api, system_soc_pct=50.0, bms_soc_pct=95.0),
            make_observation(
                api,
                captured_at_mono=99.0,
                sequence=6,
                system_soc_pct=50.0,
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


# --- excess-solar export evidence (API_CONTRACTS "Excess-solar accelerated
# --- charging (advisory)") ----------------------------------------------------
#
# Defense in depth behind the allocator's export bound: the kernel itself
# refuses a NON-ZERO export-bounded charge proposal whose fleet grid evidence
# is missing, quality-bad, or stale, mirroring the telemetry_stale pattern.
# The policy carries the all-or-none export triple; the observations carry the
# advisory grid_power_w (raw namespaces so the KERNEL's logic is under test,
# not the domain constructor).  Zero-watt proposals never accrue export
# reasons: zero is always permitted and all-zero allocations stay legitimate.

EXPORT_POLICY_KEYS: dict[str, Any] = {
    "export_charge_limit_w": 2_000,
    "export_headroom_margin_w": 200,
    "export_telemetry_max_age_s": 3.0,
}


def make_export_policy(api: SimpleNamespace) -> Any:
    try:
        return make_policy(api, **EXPORT_POLICY_KEYS)
    except ValidationError as error:
        pytest.fail(f"the ControlPolicy export triple is not implemented: {error}", pytrace=False)
        raise  # pragma: no cover - pytest.fail never returns


def make_export_fleet(
    api: SimpleNamespace,
    grids: Mapping[str, float | None],
    *,
    grid_quality: Any = None,
    captured_at_mono_by_unit: Mapping[str, float] | None = None,
    drop_quality_key: bool = False,
) -> dict[str, Any]:
    """Fleet of raw observations carrying per-pod grid power (export evidence)."""
    fleet: dict[str, Any] = {}
    for unit_id, grid in grids.items():
        quality: dict[str, Any] = {field: api.DataQuality.GOOD for field in REQUIRED_QUALITY_FIELDS}
        if not drop_quality_key:
            quality["grid_power_w"] = (
                grid_quality if grid_quality is not None else api.DataQuality.GOOD
            )
        captured = (
            100.0
            if captured_at_mono_by_unit is None
            else captured_at_mono_by_unit.get(unit_id, 100.0)
        )
        fleet[unit_id] = make_raw_observation(
            api,
            unit_id=unit_id,
            grid_power_w=grid,
            captured_at_mono=captured,
            quality=quality,
        )
    return fleet


_EXPORT_GRIDS = {"lhs": -300.0, "mid": 800.0, "rhs": 900.0}


def test_export_bounded_charge_authorizes_on_fresh_good_fleet_grid_evidence(
    api: SimpleNamespace,
) -> None:
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=api.Direction.CHARGE, export_bounded=True
        ),
        current_observations=make_export_fleet(api, _EXPORT_GRIDS),
        policy=make_export_policy(api),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("safety_checks_passed",)
    assert setpoints_by_unit(decision)["mid"].watts == 1_000


@pytest.mark.parametrize(
    ("grids", "grid_quality", "captured", "drop_key", "expected_reason"),
    [
        # A fleet unit with no observation at all: one unreadable phase is
        # never treated as zero export.
        (
            {k: v for k, v in _EXPORT_GRIDS.items() if k != "rhs"},
            None,
            None,
            False,
            "export_evidence_missing",
        ),
        # A None (unsourced) grid value with otherwise GOOD quality.
        ({**_EXPORT_GRIDS, "rhs": None}, None, None, False, "export_evidence_missing"),
        # An old-shape observation whose quality map never carried the key.
        (_EXPORT_GRIDS, None, None, True, "export_evidence_missing"),
        # Quality-bad and quality-suspect grid words.
        (_EXPORT_GRIDS, "BAD", None, False, "export_evidence_bad"),
        (_EXPORT_GRIDS, "SUSPECT", None, False, "export_evidence_bad"),
        # Stale export evidence: rhs captured 96.0 is exactly ON the regular
        # telemetry-stale boundary (age 5.0 of 5.0) but 2 s past the export
        # freshness bound, isolating the export reason.
        (_EXPORT_GRIDS, None, {"rhs": 96.0}, False, "export_evidence_stale"),
    ],
)
def test_export_bounded_charge_fails_closed_on_any_unusable_grid_evidence(
    api: SimpleNamespace,
    grids: dict[str, float | None],
    grid_quality: Any,
    captured: dict[str, float] | None,
    drop_key: bool,
    expected_reason: str,
) -> None:
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=api.Direction.CHARGE, export_bounded=True
        ),
        current_observations=make_export_fleet(
            api,
            grids,
            grid_quality=None if grid_quality is None else getattr(api.DataQuality, grid_quality),
            captured_at_mono_by_unit=captured,
            drop_quality_key=drop_key,
        ),
        policy=make_export_policy(api),
    )

    assert_rejected(decision, api)
    assert expected_reason in reasons(decision)


def test_zero_watt_export_bounded_proposal_never_accrues_export_reasons(
    api: SimpleNamespace,
) -> None:
    """Zero is always permitted; an all-zero allocation stays a legitimate,
    honestly-rejected representation and must not be blamed on export evidence."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, direction=api.Direction.CHARGE, watts=0, export_bounded=True
        ),
        current_observations=make_export_fleet(
            api, _EXPORT_GRIDS, captured_at_mono_by_unit={"rhs": 96.0}
        ),
        policy=make_export_policy(api),
    )

    assert_rejected(decision, api)
    assert reasons(decision) == ("zero_dynamic_capability",)
    assert not [reason for reason in reasons(decision) if reason.startswith("export_")]


def test_ordinary_charge_is_untouched_by_export_evidence_rules(
    api: SimpleNamespace,
) -> None:
    """Without the export-bounded flag the export evidence is irrelevant."""
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(api, direction=api.Direction.CHARGE),
        current_observations={},
        previous_observations={},
        policy=make_export_policy(api),
    )

    assert_rejected(decision, api)
    assert "observation_missing" in reasons(decision)
    assert not [reason for reason in reasons(decision) if reason.startswith("export_")]


# --- concurrent per-unit operation (2026-08-24 operator requirement) -----------
#
# "I instructed MID to charge at 2,000 watts and RHS to discharge at 1,000
# watts. Only one operation functions at a time. I require both to function
# concurrently whenever a battery request is made."  One cycle may now carry
# DIFFERENT directions on different units -- charging one pod while
# discharging another is physically legitimate (independent phases).  The
# old fleet-wide ``mixed_directions`` rejection is replaced by per-unit
# coherence: each proposal's direction is judged against its own unit (the
# control kernel's matcher binds every proposal to its unit's winning
# intent), fleet limits apply PER DIRECTION over that direction's subtotal,
# and every per-unit deny reason zeroes ONLY its own unit -- a denied unit is
# a zero-watt non-participant for its direction (the 6abd869/d2163a5
# non-participation doctrine extended to concurrency) while the other units
# still run.


def make_mixed_proposals(
    api: SimpleNamespace,
    *,
    charge: dict[str, int] | None = None,
    discharge: dict[str, int] | None = None,
    idle: tuple[str, ...] = (),
    intent_expires_at_mono: float = 109.0,
) -> tuple[ProposedSetpointRecord, ...]:
    """Compose per-unit proposals carrying each unit's own direction."""
    proposals: list[ProposedSetpointRecord] = []
    for unit_id, watts in sorted((charge or {}).items()):
        proposals.append(
            ProposedSetpointRecord(
                unit_id=unit_id,
                direction=api.Direction.CHARGE,
                watts=watts,
                intent_id=f"intent-charge-{unit_id}",
                intent_expires_at_mono=intent_expires_at_mono,
            )
        )
    for unit_id, watts in sorted((discharge or {}).items()):
        proposals.append(
            ProposedSetpointRecord(
                unit_id=unit_id,
                direction=api.Direction.DISCHARGE,
                watts=watts,
                intent_id=f"intent-discharge-{unit_id}",
                intent_expires_at_mono=intent_expires_at_mono,
            )
        )
    for unit_id in sorted(idle):
        proposals.append(
            ProposedSetpointRecord(
                unit_id=unit_id,
                direction=api.Direction.IDLE,
                watts=0,
                intent_id=f"intent-idle-{unit_id}",
                intent_expires_at_mono=intent_expires_at_mono,
            )
        )
    return tuple(proposals)


def _unit_routed_overrides(unit_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Per-unit observation overrides keyed as unit-colon-id-colon-field."""
    prefix = f"unit:{unit_id}:"
    return {key[len(prefix) :]: value for key, value in overrides.items() if key.startswith(prefix)}


def make_fleet_observations(api: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    """Observations for every fleet unit, with per-unit overrides routed by key."""
    return {
        unit_id: make_observation(
            api, unit_id=unit_id, **_unit_routed_overrides(unit_id, overrides)
        )
        for unit_id in UNIT_IDS
    }


def test_mixed_direction_cycle_is_authorized_when_each_unit_is_coherent(
    api: SimpleNamespace,
) -> None:
    """The operator's exact scenario: MID charges 2,000 W while RHS discharges
    1,000 W in ONE authorized decision -- each proposal keeps its own unit's
    direction and watts, nothing is reversed or blended."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(api),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("safety_checks_passed",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["mid"].direction is api.Direction.CHARGE
    assert setpoints["mid"].watts == 2_000
    assert setpoints["rhs"].direction is api.Direction.DISCHARGE
    assert setpoints["rhs"].watts == 1_000


def test_per_unit_soc_bound_applies_against_each_units_own_direction(
    api: SimpleNamespace,
) -> None:
    """RHS at the SOC floor cannot discharge (its own direction's bound) but
    that same floor never touches MID's charge in the same cycle."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(
            api,
            **{
                "unit:rhs:system_soc_pct": 9.5,
                "unit:rhs:bms_soc_pct": 9.5,
            },
        ),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert "soc_below_discharge_floor" in reasons(decision)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["mid"].direction is api.Direction.CHARGE
    assert setpoints["mid"].watts == 2_000
    # The denied unit is a zero-watt non-participant for ITS direction; the
    # other unit still runs.
    assert setpoints["rhs"].watts == 0


def test_one_units_denial_zeroes_only_that_unit_in_a_mixed_cycle(
    api: SimpleNamespace,
) -> None:
    """A blocking fault on the charging unit stops that unit's charge only;
    the discharging unit keeps its full authority in the same decision."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(
            api, **{"unit:mid:active_faults": frozenset({"BMS_CRITICAL"})}
        ),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert "blocking_fault" in reasons(decision)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["mid"].watts == 0
    assert setpoints["rhs"].direction is api.Direction.DISCHARGE
    assert setpoints["rhs"].watts == 1_000


def test_every_unit_denied_in_a_mixed_cycle_fails_closed_to_rejection(
    api: SimpleNamespace,
) -> None:
    """When NO unit can participate the decision is rejected whole (fail
    closed), never an authorized empty grant."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(
            api,
            **{
                "unit:mid:active_faults": frozenset({"BMS_CRITICAL"}),
                "unit:rhs:system_soc_pct": 9.5,
                "unit:rhs:bms_soc_pct": 9.5,
            },
        ),
    )

    assert_rejected(decision, api)
    assert "blocking_fault" in reasons(decision)
    assert "soc_below_discharge_floor" in reasons(decision)


def test_zero_dynamic_capability_zeroes_only_that_unit_in_a_mixed_cycle(
    api: SimpleNamespace,
) -> None:
    """A unit whose BMS reports no charge headroom becomes a non-participant;
    the discharging unit in the same cycle is untouched."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(api, **{"unit:mid:dynamic_charge_limit_w": 0}),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert "zero_dynamic_capability" in reasons(decision)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["mid"].watts == 0
    assert setpoints["rhs"].watts == 1_000


def test_fleet_limits_apply_per_direction_across_each_directions_subtotal(
    api: SimpleNamespace,
) -> None:
    """fleet_charge_limit_w bounds the charge subtotal and
    fleet_discharge_limit_w the discharge subtotal -- never one blended budget.
    lhs+mid charge 2,500 W each against a 3,000 W charge limit while rhs
    discharges 1,000 W inside a 7,500 W discharge limit: the charges clamp to
    3,000 W together and the discharge is untouched."""
    policy = make_policy(api, fleet_charge_limit_w=3_000)
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"lhs": 2_500, "mid": 2_500}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(api),
        policy=policy,
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert reasons(decision) == ("power_clamped",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["lhs"].watts + setpoints["mid"].watts == 3_000
    assert setpoints["lhs"].direction is api.Direction.CHARGE
    assert setpoints["mid"].direction is api.Direction.CHARGE
    assert setpoints["rhs"].direction is api.Direction.DISCHARGE
    assert setpoints["rhs"].watts == 1_000


def test_a_starved_charge_limit_does_not_veto_the_discharge_side(
    api: SimpleNamespace,
) -> None:
    """A charge budget starved to its last watt clamps the charge side to 1 W
    without touching the discharge side's own budget -- the per-direction
    doctrine across directions."""
    policy = make_policy(api, fleet_charge_limit_w=1)
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(api),
        policy=policy,
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    assert reasons(decision) == ("power_clamped",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["mid"].watts == 1
    assert setpoints["mid"].direction is api.Direction.CHARGE
    assert setpoints["rhs"].watts == 1_000
    assert setpoints["rhs"].direction is api.Direction.DISCHARGE


def test_each_direction_clamps_under_its_own_limit_in_one_cycle(
    api: SimpleNamespace,
) -> None:
    """Both directions clamp in the same decision, each against its own
    budget, and the exact-integer remainder distribution holds per side."""
    policy = make_policy(api, fleet_charge_limit_w=1_500, fleet_discharge_limit_w=700)
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"lhs": 2_000, "mid": 2_000}, discharge={"rhs": 1_000}
        ),
        current_observations=make_fleet_observations(api),
        policy=policy,
    )

    assert decision.status is api.DecisionStatus.CLAMPED
    setpoints = setpoints_by_unit(decision)
    assert setpoints["lhs"].watts + setpoints["mid"].watts == 1_500
    assert setpoints["lhs"].direction is setpoints["mid"].direction is api.Direction.CHARGE
    assert setpoints["rhs"].watts == 700


def test_zero_watt_idle_proposal_rides_along_an_active_mixed_cycle(
    api: SimpleNamespace,
) -> None:
    """A unit held idle by its own winning intent is a zero-watt rider: it
    neither vetoes nor joins the two active directions."""
    decision = evaluate(
        api,
        proposed_setpoints=make_mixed_proposals(
            api, charge={"mid": 2_000}, discharge={"rhs": 1_000}, idle=("lhs",)
        ),
        current_observations=make_fleet_observations(api),
    )

    assert decision.status is api.DecisionStatus.AUTHORIZED
    assert reasons(decision) == ("safety_checks_passed",)
    setpoints = setpoints_by_unit(decision)
    assert setpoints["lhs"].watts == 0
    assert setpoints["mid"].watts == 2_000
    assert setpoints["rhs"].watts == 1_000


def test_mixed_cycle_rejects_when_a_structural_flaw_remains(
    api: SimpleNamespace,
) -> None:
    """Structural proposal flaws (unknown direction, an expired intent) still
    reject the WHOLE composed cycle before any per-unit evaluation --
    concurrency never weakens the structural gate."""
    flawed = make_mixed_proposals(api, charge={"mid": 2_000}, discharge={"rhs": 1_000})
    expired = (
        flawed[0],
        ProposedSetpointRecord(
            unit_id="rhs",
            direction=api.Direction.DISCHARGE,
            watts=1_000,
            intent_id="intent-discharge-rhs",
            intent_expires_at_mono=NOW,
        ),
    )
    decision = evaluate(api, proposed_setpoints=expired)
    assert_rejected(decision, api)
    assert "intent_expired" in reasons(decision)

    invalid_direction = (
        flawed[0],
        ProposedSetpointRecord(
            unit_id="rhs",
            direction="reverse",
            watts=1_000,
            intent_id="intent-discharge-rhs",
            intent_expires_at_mono=109.0,
        ),
    )
    decision = evaluate(api, proposed_setpoints=invalid_direction)
    assert_rejected(decision, api)
    assert "invalid_direction" in reasons(decision)


def test_single_direction_fleet_behavior_is_unchanged_by_the_per_unit_doctrine(
    api: SimpleNamespace,
) -> None:
    """The regression anchor: a homogeneous three-unit discharge clamps to the
    fleet discharge limit exactly as before concurrency."""
    policy = make_policy(api, fleet_discharge_limit_w=4_500)
    decision = evaluate(
        api,
        proposed_setpoints=make_proposed_setpoints(
            api, watts_by_unit={"lhs": 2_000, "mid": 2_000, "rhs": 2_000}
        ),
        current_observations=make_fleet_observations(api),
        policy=policy,
    )
    setpoints = setpoints_by_unit(decision)
    assert decision.status is api.DecisionStatus.CLAMPED
    assert sum(point.watts for point in setpoints.values()) == 4_500
