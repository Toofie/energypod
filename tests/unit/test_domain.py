"""Protocol-agnostic contracts for immutable EnergyPod domain values.

Active power is always a ``Direction`` plus a non-negative magnitude here.
Signed values, two's-complement limits, and word order belong to the Modbus
adapter and are deliberately absent from this suite.
"""

from __future__ import annotations

import importlib
import math
from datetime import UTC, datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest


def _models() -> ModuleType:
    return importlib.import_module("energypod.domain.models")


def _assert_frozen(value: object, field: str, replacement: object) -> None:
    original = getattr(value, field)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        setattr(value, field, replacement)
    assert getattr(value, field) == original


def _intent_kwargs(models: ModuleType, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": "intent-001",
        "source": models.IntentSource.MANUAL,
        "selected_unit_ids": frozenset({"mid", "rhs"}),
        "direction": models.Direction.CHARGE,
        "watts": 4_000,
        "duration_s": 5.0,
        "accepted_at_mono": 100.0,
        "acceptance_revision": 42,
        "actor_identity": "operator:owner",
    }
    values.update(overrides)
    return values


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
)


def _quality(models: ModuleType) -> dict[str, object]:
    return {name: models.DataQuality.GOOD for name in QUALITY_FIELDS}


def _observation_kwargs(models: ModuleType, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "unit_id": "mid",
        "connection_epoch": 0,
        "wall_timestamp": datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
        "captured_at_mono": 200.0,
        "sequence": 0,
        "lifecycle": models.UnitLifecycle.ARMED_IDLE,
        "protocol_profile": "iot-v1",
        "system_soc_pct": 54.2,
        "bms_soc_pct": 54.0,
        "soh_pct": 96.5,
        "battery_watts": -1_200.5,
        "pack_voltage_v": 401.2,
        "pack_current_a": -3.0,
        "dynamic_charge_limit_w": 5_000.0,
        "dynamic_discharge_limit_w": 4_500.0,
        "expected_cell_count": 4,
        "cell_voltages_v": (3.280, 3.291, 3.304, 3.299),
        "expected_temperature_count": 3,
        "temperatures_c": (24.0, 25.5, 23.5),
        "active_faults": frozenset(),
        "active_warnings": frozenset(),
        "quality": _quality(models),
    }
    values.update(overrides)
    return values


def test_required_domain_api_is_importable() -> None:
    models = _models()
    for name in (
        "Direction",
        "DataQuality",
        "UnitLifecycle",
        "IntentSource",
        "DecisionStatus",
        "PowerIntent",
        "UnitSetpoint",
        "Observation",
    ):
        assert hasattr(models, name), name


@pytest.mark.parametrize(
    ("enum_name", "required"),
    [
        ("Direction", {"CHARGE": "charge", "DISCHARGE": "discharge", "IDLE": "idle"}),
        (
            "DataQuality",
            {
                "GOOD": "good",
                "STALE": "stale",
                "MISSING": "missing",
                "BAD": "bad",
                "SUSPECT": "suspect",
            },
        ),
        (
            "UnitLifecycle",
            {
                "BOOT": "boot",
                "OBSERVE_ONLY": "observe_only",
                "DISARMED": "disarmed",
                "ARMED_IDLE": "armed_idle",
                "ACTIVE": "active",
                "INHIBITED": "inhibited",
                "STOPPING": "stopping",
                "DISCONNECTED": "disconnected",
            },
        ),
        (
            "IntentSource",
            {
                "EMERGENCY_STOP": "emergency_stop",
                "MANUAL": "manual",
                "AGENT": "agent",
                "OPTIMIZER": "optimizer",
                "SCHEDULE": "schedule",
            },
        ),
        (
            "DecisionStatus",
            {
                "AUTHORIZED": "authorized",
                "CLAMPED": "clamped",
                "REJECTED": "rejected",
                "REVOKED": "revoked",
            },
        ),
    ],
)
def test_enums_include_stable_documented_values(enum_name: str, required: dict[str, str]) -> None:
    """Do not prevent additive enum members required by future adapters."""
    actual = {member.name: member.value for member in getattr(_models(), enum_name)}
    assert actual.items() >= required.items()


def test_power_intent_is_frozen_and_derives_expiry() -> None:
    models = _models()
    intent = models.PowerIntent(**_intent_kwargs(models))
    assert intent.id == "intent-001"
    assert intent.accepted_at_mono == 100.0
    assert intent.acceptance_revision == 42
    assert intent.expires_at_mono == pytest.approx(105.0)
    assert (intent.direction, intent.watts) == (models.Direction.CHARGE, 4_000)
    _assert_frozen(intent, "watts", 1)


def test_monotonic_timestamp_may_be_negative_but_must_be_finite() -> None:
    models = _models()
    intent = models.PowerIntent(**_intent_kwargs(models, accepted_at_mono=-10.0, duration_s=2.5))
    assert intent.expires_at_mono == pytest.approx(-7.5)


def test_domain_power_is_not_bounded_by_a_signed_wire_register() -> None:
    models = _models()
    assert models.PowerIntent(**_intent_kwargs(models, watts=100_000)).watts == 100_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("id", " intent "),
        ("actor_identity", ""),
        ("actor_identity", " operator "),
        ("selected_unit_ids", frozenset()),
        ("selected_unit_ids", frozenset({" mid "})),
        ("selected_unit_ids", {"mid"}),
        ("source", "manual"),
        ("direction", "charge"),
        ("watts", -1),
        ("watts", 1.0),
        ("watts", True),
        ("duration_s", 0.0),
        ("duration_s", math.nan),
        ("duration_s", math.inf),
        ("duration_s", True),
        ("accepted_at_mono", math.nan),
        ("accepted_at_mono", math.inf),
        ("accepted_at_mono", True),
        ("acceptance_revision", -1),
        ("acceptance_revision", 1.0),
        ("acceptance_revision", True),
    ],
)
def test_power_intent_rejects_malformed_or_coerced_values(field: str, value: object) -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**_intent_kwargs(models, **{field: value}))


def test_power_intent_rejects_signed_protocol_field() -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**_intent_kwargs(models, signed_watts=-1_000))


def test_power_intent_rejects_legacy_ordering_alias() -> None:
    """The clean-slate domain has one canonical server-ordering vocabulary."""
    models = _models()
    values = _intent_kwargs(models)
    values["created_at_mono"] = values.pop("accepted_at_mono")
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**values)


@pytest.mark.parametrize("direction", ["CHARGE", "DISCHARGE"])
def test_non_idle_intent_requires_positive_magnitude(direction: str) -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(
            **_intent_kwargs(models, direction=getattr(models.Direction, direction), watts=0)
        )


def test_idle_intent_requires_exactly_zero_watts() -> None:
    models = _models()
    assert (
        models.PowerIntent(**_intent_kwargs(models, direction=models.Direction.IDLE, watts=0)).watts
        == 0
    )
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**_intent_kwargs(models, direction=models.Direction.IDLE, watts=1))


# --- per-unit watt targets (the 2026-08-23 operator ruling: "I asked for each
# setting to be one thousand, not a total of 1,000") ---------------------------


def test_power_intent_carries_optional_per_unit_watts() -> None:
    """One intent may name a different watt target per selected battery.

    ``watts`` stays the fleet total (the sum of the targets) so every existing
    consumer of the fleet figure is unchanged; the per-unit map is frozen with
    normalized keys, and IDLE intents keep carrying ``None``.
    """
    models = _models()
    intent = models.PowerIntent(
        **_intent_kwargs(
            models,
            selected_unit_ids=frozenset({"mid", "rhs"}),
            watts=2_600,
            watts_by_unit={"mid": 1_000, "rhs": 1_600},
        )
    )
    assert dict(intent.watts_by_unit) == {"mid": 1_000, "rhs": 1_600}
    assert intent.watts == 2_600, "the fleet total must equal the sum of the per-unit targets"
    with pytest.raises(TypeError):
        intent.watts_by_unit["mid"] = 1  # type: ignore[index]
    _assert_frozen(intent, "watts", 1)
    # The default remains the fleet-total-only shape every existing intent uses.
    assert models.PowerIntent(**_intent_kwargs(models)).watts_by_unit is None


@pytest.mark.parametrize(
    ("watts", "per_unit"),
    [
        (2_500, {"mid": 1_000}),  # a selected unit is missing its target
        (2_600, {"mid": 1_000, "rhs": 1_000, "lhs": 600}),  # a target names an unselected unit
        (1_000, {"mid": 0, "rhs": 1_000}),  # a per-unit target must be positive
        (900, {"mid": -100, "rhs": 1_000}),
        (2_600, {"mid": 1.0, "rhs": 1_600}),  # coerced float magnitude
        (2_600, {"mid": True, "rhs": 1_600}),  # boolean magnitude
        (2_600, {" mid ": 1_000, "rhs": 1_600}),  # unnormalized key
        (2_500, {"mid": 1_000, "rhs": 1_400}),  # targets do not sum to the fleet total
        (0, {}),
    ],
)
def test_power_intent_rejects_malformed_per_unit_watts(watts: int, per_unit: object) -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(
            **_intent_kwargs(
                models,
                selected_unit_ids=frozenset({"mid", "rhs"}),
                watts=watts,
                watts_by_unit=per_unit,
            )
        )


def test_power_intent_rejects_non_mapping_per_unit_watts() -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**_intent_kwargs(models, watts_by_unit=[("mid", 1_000)]))
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(**_intent_kwargs(models, watts_by_unit="mid:1000"))


def test_idle_intent_carries_no_per_unit_watts() -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.PowerIntent(
            **_intent_kwargs(
                models,
                direction=models.Direction.IDLE,
                watts=0,
                watts_by_unit={"mid": 1, "rhs": 1},
            )
        )


def _setpoint(models: ModuleType, **overrides: Any) -> object:
    values: dict[str, Any] = {
        "unit_id": "mid",
        "direction": models.Direction.CHARGE,
        "watts": 1_500,
        "generation": 0,
        "intent_id": "intent-001",
        "authorization_expires_at_mono": -1.0,
    }
    values.update(overrides)
    return models.UnitSetpoint(**values)


@pytest.mark.parametrize("direction", ["CHARGE", "DISCHARGE"])
def test_unit_setpoint_uses_direction_and_unsigned_magnitude(direction: str) -> None:
    models = _models()
    setpoint = _setpoint(models, direction=getattr(models.Direction, direction))
    assert setpoint.direction is getattr(models.Direction, direction)
    assert setpoint.watts == 1_500
    assert not hasattr(setpoint, "active_watts")
    _assert_frozen(setpoint, "watts", 1)


def test_setpoint_generation_can_start_at_zero_and_power_is_protocol_agnostic() -> None:
    models = _models()
    setpoint = _setpoint(models, watts=100_000, generation=0)
    assert (setpoint.watts, setpoint.generation) == (100_000, 0)


def test_setpoint_idle_and_non_idle_magnitude_rules() -> None:
    models = _models()
    assert _setpoint(models, direction=models.Direction.IDLE, watts=0).watts == 0
    # Zero watts with an ACTIVE direction is explicit non-participation: a
    # selected unit with no usable headroom (2026-08-23 live rejection — units
    # at the SOC floor/ceiling inside an otherwise deliverable fleet intent).
    # No authority is ever minted for such a setpoint, so representing it is
    # safe; IDLE with nonzero watts remains the one forbidden combination.
    for direction in (models.Direction.CHARGE, models.Direction.DISCHARGE):
        assert _setpoint(models, direction=direction, watts=0).watts == 0
    with pytest.raises((TypeError, ValueError)):
        _setpoint(models, direction=models.Direction.IDLE, watts=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_id", ""),
        ("unit_id", " mid "),
        ("intent_id", ""),
        ("direction", "charge"),
        ("watts", -1),
        ("watts", 1.0),
        ("watts", True),
        ("generation", -1),
        ("generation", 1.0),
        ("generation", True),
        ("authorization_expires_at_mono", math.nan),
        ("authorization_expires_at_mono", math.inf),
        ("authorization_expires_at_mono", True),
    ],
)
def test_setpoint_rejects_malformed_or_coerced_values(field: str, value: object) -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        _setpoint(models, **{field: value})


def test_setpoint_rejects_adapter_only_signed_field() -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        _setpoint(models, active_watts=-1_500)


def test_observation_derives_statistics_and_completeness() -> None:
    models = _models()
    observation = models.Observation(**_observation_kwargs(models))
    assert observation.cell_min_voltage_v == pytest.approx(3.280)
    assert observation.cell_max_voltage_v == pytest.approx(3.304)
    assert observation.cell_imbalance_v == pytest.approx(0.024)
    assert observation.temperature_min_c == pytest.approx(23.5)
    assert observation.temperature_max_c == pytest.approx(25.5)
    assert observation.cells_complete and observation.temperatures_complete
    assert observation.safety_data_complete


def test_observation_authoritative_soc_is_the_bms_figure() -> None:
    """2026-08-24 operator ruling: the battery's own BMS SOC is the
    authoritative SOC for every policy bound.  The derived property spells
    that figure for advisory consumers, and a system-vs-BMS divergence --
    however large -- never displaces it."""
    models = _models()
    observation = models.Observation(**_observation_kwargs(models))
    assert observation.authoritative_soc_pct == pytest.approx(54.0)

    divergent = models.Observation(
        **_observation_kwargs(models, system_soc_pct=95.0, bms_soc_pct=41.5)
    )
    assert divergent.authoritative_soc_pct == pytest.approx(41.5)

    unserved = models.Observation(
        **_observation_kwargs(
            models,
            bms_soc_pct=None,
            quality=_quality(models) | {"bms_soc_pct": models.DataQuality.MISSING},
        )
    )
    assert unserved.authoritative_soc_pct is None


def test_observation_age_uses_only_monotonic_time() -> None:
    models = _models()
    first = models.Observation(**_observation_kwargs(models, captured_at_mono=-2.0))
    shifted = models.Observation(
        **_observation_kwargs(
            models,
            captured_at_mono=-2.0,
            wall_timestamp=datetime(2036, 8, 21, 2, 0, tzinfo=UTC),
        )
    )
    assert first.age_seconds(1.25) == shifted.age_seconds(1.25) == pytest.approx(3.25)
    for now in (-2.01, math.nan, math.inf, True):
        with pytest.raises((TypeError, ValueError)):
            first.age_seconds(now)


def test_missing_values_are_explicit_and_never_fabricated_as_zero() -> None:
    models = _models()
    quality = _quality(models)
    for field in ("system_soc_pct", "cell_voltages_v", "temperatures_c"):
        quality[field] = models.DataQuality.MISSING
    observation = models.Observation(
        **_observation_kwargs(
            models,
            system_soc_pct=None,
            cell_voltages_v=(),
            temperatures_c=(),
            quality=quality,
        )
    )
    assert observation.system_soc_pct is None
    assert observation.cell_min_voltage_v is None
    assert observation.temperature_min_c is None
    assert not observation.cells_complete and not observation.temperatures_complete
    assert not observation.safety_data_complete


@pytest.mark.parametrize("quality_name", ["STALE", "MISSING", "BAD", "SUSPECT"])
def test_non_good_required_quality_is_never_safety_complete(quality_name: str) -> None:
    models = _models()
    quality = _quality(models)
    quality["system_soc_pct"] = getattr(models.DataQuality, quality_name)
    assert not models.Observation(
        **_observation_kwargs(models, quality=quality)
    ).safety_data_complete


def test_partial_arrays_are_visible_but_not_complete() -> None:
    models = _models()
    quality = _quality(models)
    quality["cell_voltages_v"] = quality["temperatures_c"] = models.DataQuality.SUSPECT
    observation = models.Observation(
        **_observation_kwargs(
            models,
            cell_voltages_v=(3.28, 3.29),
            temperatures_c=(24.0,),
            quality=quality,
        )
    )
    assert observation.cell_min_voltage_v == pytest.approx(3.28)
    assert not observation.cells_complete and not observation.temperatures_complete
    assert not observation.safety_data_complete


def test_observation_nested_state_is_defensively_copied_and_frozen() -> None:
    models = _models()
    quality = _quality(models)
    observation = models.Observation(**_observation_kwargs(models, quality=quality))
    quality["system_soc_pct"] = models.DataQuality.BAD
    assert observation.quality["system_soc_pct"] is models.DataQuality.GOOD
    with pytest.raises(TypeError):
        observation.quality["system_soc_pct"] = models.DataQuality.BAD
    _assert_frozen(observation, "sequence", 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_id", ""),
        ("unit_id", " mid "),
        ("connection_epoch", -1),
        ("connection_epoch", 1.0),
        ("connection_epoch", True),
        ("sequence", -1),
        ("sequence", 1.0),
        ("sequence", True),
        ("captured_at_mono", math.nan),
        ("captured_at_mono", math.inf),
        ("captured_at_mono", True),
        ("wall_timestamp", datetime(2026, 8, 21, 2, 0)),
        ("wall_timestamp", datetime(2026, 8, 21, 2, 0, tzinfo=timezone(timedelta(hours=10)))),
        ("lifecycle", "armed_idle"),
        ("protocol_profile", " iot-v1 "),
        ("system_soc_pct", -0.01),
        ("system_soc_pct", 100.01),
        ("system_soc_pct", True),
        ("bms_soc_pct", math.nan),
        ("bms_soc_pct", True),
        ("soh_pct", -0.01),
        ("soh_pct", 100.01),
        ("soh_pct", math.inf),
        ("battery_watts", math.nan),
        ("battery_watts", True),
        ("pack_voltage_v", math.nan),
        ("pack_voltage_v", True),
        ("pack_current_a", math.inf),
        ("pack_current_a", True),
        ("dynamic_charge_limit_w", -1),
        ("dynamic_charge_limit_w", math.inf),
        ("dynamic_charge_limit_w", True),
        ("dynamic_discharge_limit_w", -1),
        ("dynamic_discharge_limit_w", math.nan),
        ("dynamic_discharge_limit_w", True),
        ("expected_cell_count", 0),
        ("expected_cell_count", True),
        ("expected_temperature_count", 0),
        ("expected_temperature_count", True),
        ("cell_voltages_v", [3.28]),
        ("cell_voltages_v", (3.28, math.nan)),
        ("cell_voltages_v", (3.28, True)),
        ("temperatures_c", [24.0]),
        ("temperatures_c", (math.inf,)),
        ("temperatures_c", (True,)),
        ("active_faults", {"fault"}),
        ("active_faults", frozenset({" fault "})),
        ("active_warnings", frozenset({""})),
        ("active_warnings", frozenset({True})),
    ],
)
def test_observation_rejects_malformed_or_coerced_values(field: str, value: object) -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.Observation(**_observation_kwargs(models, **{field: value}))


@pytest.mark.parametrize("field", ["system_soc_pct", "bms_soc_pct", "soh_pct"])
@pytest.mark.parametrize("boundary", [0.0, 100.0])
def test_percentage_boundaries_are_inclusive(field: str, boundary: float) -> None:
    models = _models()
    observation = models.Observation(**_observation_kwargs(models, **{field: boundary}))
    assert getattr(observation, field) == boundary


def test_quality_map_requires_exact_keys_and_typed_values() -> None:
    models = _models()
    missing = _quality(models)
    missing.pop("system_soc_pct")
    untyped = _quality(models)
    untyped["system_soc_pct"] = "good"
    extra = _quality(models)
    extra["invented"] = models.DataQuality.GOOD
    for quality in ({}, missing, untyped, extra):
        with pytest.raises((TypeError, ValueError)):
            models.Observation(**_observation_kwargs(models, quality=quality))


def test_observation_rejects_undeclared_protocol_fields() -> None:
    models = _models()
    with pytest.raises((TypeError, ValueError)):
        models.Observation(**_observation_kwargs(models, signed_setpoint_watts=-1_000))


# --- per-unit direction breakdown on audit facts (2026-08-24) ------------------
#
# Concurrent cycles compose several intents whose units may run DIFFERENT
# directions; one decision row must therefore carry each unit's own direction
# next to its watts.  ``directions_by_unit`` is optional exactly like the
# per-unit watt maps so durable rows written before it existed keep decoding.


def _audit_kwargs(audit: ModuleType, models: ModuleType, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": "event-0001",
        "occurred_at": datetime(2026, 8, 24, tzinfo=UTC),
        "monotonic_offset_s": 1.0,
        "process_instance_id": "process-1",
        "event_type": "control_decision",
        "unit_id": None,
        "generation": 1,
        "cycle_id": "cycle-00000000000000000001",
        "principal": "operator:owner",
        "source": models.IntentSource.MANUAL,
        "correlation_id": "cycle:cycle-1",
        "intent_id": None,
        "policy_version": "policy-1",
        "configuration_version": 1,
        "observation_sequences": {"mid": 3, "rhs": 4},
        "reason_codes": ("safety_checks_passed",),
        "requested_active_w": -1_000,
        "authorized_active_w": -1_000,
        "requested_watts_by_unit": {"mid": 2_000, "rhs": 1_000},
        "authorized_watts_by_unit": {"mid": 2_000, "rhs": 1_000},
        "directions_by_unit": {"mid": "charge", "rhs": "discharge"},
        "request_fingerprint": "0" * 64,
        "response_fingerprint": "1" * 64,
        "result": "authorized",
        "lifecycle": models.UnitLifecycle.ACTIVE,
    }
    values.update(overrides)
    return values


def test_audit_event_carries_the_per_unit_direction_breakdown() -> None:
    models = _models()
    audit = importlib.import_module("energypod.domain.audit")
    event = audit.AuditEvent(**_audit_kwargs(audit, models))
    assert dict(event.directions_by_unit) == {"mid": "charge", "rhs": "discharge"}
    _assert_frozen(event, "directions_by_unit", {"mid": "discharge"})
    with pytest.raises((TypeError, ValueError)):
        event.directions_by_unit["mid"] = "discharge"  # type: ignore[index]


def test_audit_directions_by_unit_is_optional_and_defaults_to_none() -> None:
    models = _models()
    audit = importlib.import_module("energypod.domain.audit")
    values = _audit_kwargs(audit, models)
    values.pop("directions_by_unit")
    event = audit.AuditEvent(**values)
    assert event.directions_by_unit is None


def test_audit_directions_by_unit_rejects_unknown_directions_and_bad_keys() -> None:
    models = _models()
    audit = importlib.import_module("energypod.domain.audit")
    for bad in (
        {"mid": "reverse"},
        {"mid": None},
        {" mid": "charge"},
        {"": "charge"},
    ):
        with pytest.raises((TypeError, ValueError)):
            audit.AuditEvent(**_audit_kwargs(audit, models, directions_by_unit=bad))
