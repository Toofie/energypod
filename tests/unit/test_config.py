"""Contract tests for strict, fail-closed runtime configuration.

These tests intentionally name the public configuration surface before its implementation exists.
Missing contract modules are reported as ordinary test failures so the suite still collects during
the red TDD phase.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

try:
    from energypod.runtime.config import ControllerConfig
except ImportError as exc:  # pragma: no cover - exercised only during the initial red phase
    ControllerConfig: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def _valid_config(*, mode: str = "write_enabled") -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema_version": 1,
        "revision": 7,
        "mode": mode,
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 3,
        },
        "units": [
            {
                "unit_id": "mid",
                "display_name": "Middle",
                "endpoint": {"host": "192.168.1.11", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": "BEP-MID",
                "expected_cell_count": 59,
            },
            {
                "unit_id": "rhs",
                "display_name": "Right",
                "endpoint": {"host": "192.168.1.12", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": "BEP-RHS",
                "expected_cell_count": 59,
            },
            {
                "unit_id": "lhs",
                "display_name": "Left",
                "endpoint": {"host": "192.168.1.13", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": "BEP-LHS",
                "expected_cell_count": 59,
            },
        ],
        "timing": {
            # Commissioning fixture, not a claim that the firmware lease is two seconds.
            # The evidence spelling is the measured live-trial reference a
            # write-enabled baseline must now carry (API_CONTRACTS
            # "Write-enabled run mode", bullet 1).
            "device_command_expiry_s": 2.35,
            "device_command_expiry_evidence": "live-trial://direction-2026-08-22/rev-1",
            "control_period_s": 0.40,
            "essential_read_timeout_s": 0.10,
            "kernel_timeout_s": 0.05,
            "audit_timeout_s": 0.05,
            "write_timeout_s": 0.10,
            "acknowledgement_timeout_s": 0.10,
            "maximum_jitter_s": 0.10,
            "renewal_margin_s": 0.50,
        },
        "policy": {
            "version": 3,
            "threshold_provenance": "commissioning-record-2026-08",
            "max_fleet_charge_w": 6000,
            "max_fleet_discharge_w": 6000,
            "max_unit_charge_w": 2500,
            "max_unit_discharge_w": 2500,
            "minimum_soc_pct": 10.0,
            "maximum_soc_pct": 95.0,
            "minimum_cell_v": 2.80,
            "maximum_cell_v": 3.65,
            "maximum_cell_imbalance_v": 0.050,
            "minimum_temperature_c": 0.0,
            "maximum_temperature_c": 45.0,
            "maximum_soc_difference_pct": 5.0,
            "maximum_soc_jump_pct": 10.0,
            "maximum_telemetry_age_s": 1.0,
            "maximum_cell_data_age_s": 5.0,
            "authorization_lifetime_s": 0.75,
            "ramp_limit_w_per_s": 1000,
            "stable_samples_to_rearm": 5,
            "reactive_power_limit_var": 0,
            "blocking_fault_codes": [
                "PCS_EE_CALIBRATION_OUT_OF_RANGE",
                "DCDC_EE_CALIBRATION_OUT_OF_RANGE",
            ],
            "debug_modes_enabled": False,
        },
        "authentication": {
            "enabled": True,
            "operator_credential_ref": "secret://energypod/operator-api",
            "trusted_proxy_cidrs": ["192.168.1.0/24"],
        },
        "storage": {
            "database_path": "/data/energypod.sqlite3",
            "busy_timeout_ms": 250,
        },
    }
    if mode == "observe_only":
        config.pop("authentication")
    return config


def _validate(payload: dict[str, Any]) -> Any:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The intended configuration contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )
    assert ControllerConfig is not None
    return ControllerConfig.model_validate(payload)


def _assert_invalid(payload: dict[str, Any], *, location_contains: str) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _validate(payload)

    locations = {".".join(str(part) for part in error["loc"]) for error in caught.value.errors()}
    assert any(location_contains in location for location in locations), (
        f"expected an error location containing {location_contains!r}, got {sorted(locations)!r}"
    )
    return caught.value


@pytest.mark.parametrize(
    ("path", "unknown_key"),
    [
        ((), "surprise"),
        (("site",), "ambient_timezone"),
        (("units", 0), "friendly_magic"),
        (("units", 0, "endpoint"), "protocol"),
        (("timing",), "retry_forever"),
        (("policy",), "ignore_warnings"),
        (("authentication",), "anonymous_operator"),
        (("storage",), "unsafe_fast_mode"),
    ],
)
def test_strict_config_rejects_unknown_fields_at_every_level(
    path: tuple[str | int, ...], unknown_key: str
) -> None:
    """T-UNIT-CONFIG-001 / REQ-CONFIG-001 / S1: extra fields never disappear silently."""
    payload = _valid_config()
    target: Any = payload
    for component in path:
        target = target[component]
    target[unknown_key] = True

    error = _assert_invalid(payload, location_contains=unknown_key)

    assert any(item["type"] == "extra_forbidden" for item in error.errors())


@pytest.mark.parametrize(
    ("duplicate_field", "duplicate_value"),
    [
        ("unit_id", "mid"),
        ("expected_identity", "BEP-MID"),
    ],
)
def test_config_rejects_duplicate_unit_identity_fields(
    duplicate_field: str, duplicate_value: str
) -> None:
    """T-UNIT-CONFIG-002 / INV-IDENTITY-001 / S0: logical and observed IDs are unique."""
    payload = _valid_config()
    payload["units"][1][duplicate_field] = duplicate_value

    error = _assert_invalid(payload, location_contains="units")

    assert "duplicate" in str(error).lower()


def test_config_rejects_duplicate_gateway_endpoints() -> None:
    """T-UNIT-CONFIG-003 / INV-ACTOR-001 / S0: one endpoint cannot have two owners."""
    payload = _valid_config()
    payload["units"][1]["endpoint"] = deepcopy(payload["units"][0]["endpoint"])

    error = _assert_invalid(payload, location_contains="units")

    assert "duplicate" in str(error).lower()


@pytest.mark.parametrize("unit_count", [0, 1, 2, 4])
def test_unit_count_must_match_the_declared_site_topology(unit_count: int) -> None:
    """T-UNIT-CONFIG-004 / REQ-CONFIG-002 / S1: deployment topology is explicit."""
    payload = _valid_config()
    prototype = payload["units"][0]
    payload["units"] = [deepcopy(prototype) for _ in range(unit_count)]
    for index, unit in enumerate(payload["units"]):
        unit["unit_id"] = f"unit-{index}"
        unit["endpoint"]["host"] = f"192.168.1.{20 + index}"
        unit["expected_identity"] = f"BEP-{index}"

    _assert_invalid(payload, location_contains="units")


def test_unit_count_is_not_hard_coded_to_this_three_unit_deployment() -> None:
    """T-UNIT-CONFIG-004A / REQ-CONFIG-002 / S1: the schema remains extensible."""
    payload = _valid_config()
    fourth = deepcopy(payload["units"][0])
    fourth.update(
        unit_id="future-unit",
        display_name="Future unit",
        expected_identity="BEP-FUTURE",
    )
    fourth["endpoint"]["host"] = "192.168.1.14"
    payload["units"].append(fourth)
    payload["site"]["expected_unit_count"] = 4

    parsed = _validate(payload)

    assert len(parsed.units) == parsed.site.expected_unit_count == 4


@pytest.mark.parametrize(
    ("missing_path", "expected_location"),
    [
        (("policy",), "policy"),
        (("authentication",), "authentication"),
        (("authentication", "operator_credential_ref"), "operator_credential_ref"),
    ],
)
def test_write_enabled_mode_requires_complete_policy_and_credentials(
    missing_path: tuple[str, ...], expected_location: str
) -> None:
    """T-UNIT-CONFIG-005 / REQ-AUTH-001 / S0: write mode cannot start unguarded."""
    payload = _valid_config(mode="write_enabled")
    target = payload
    for component in missing_path[:-1]:
        target = target[component]
    target.pop(missing_path[-1])

    _assert_invalid(payload, location_contains=expected_location)


@pytest.mark.parametrize("credential", ["", "   ", "changeme", "default", "secret://"])
def test_write_enabled_mode_rejects_empty_or_default_credentials(credential: str) -> None:
    """T-UNIT-CONFIG-006 / REQ-AUTH-002 / S1: network control needs a real secret reference."""
    payload = _valid_config(mode="write_enabled")
    payload["authentication"]["operator_credential_ref"] = credential

    _assert_invalid(payload, location_contains="operator_credential_ref")


def test_write_enabled_mode_rejects_disabled_authentication() -> None:
    """T-UNIT-CONFIG-007 / REQ-AUTH-003 / S1: authentication cannot be disabled for writes."""
    payload = _valid_config(mode="write_enabled")
    payload["authentication"]["enabled"] = False

    error = _assert_invalid(payload, location_contains="authentication")

    assert "write" in str(error).lower()


def test_observe_only_mode_does_not_require_control_credentials() -> None:
    """T-UNIT-CONFIG-008 / REQ-CONFIG-003 / S2: read-only startup has no write credential."""
    parsed = _validate(_valid_config(mode="observe_only"))

    assert parsed.mode.value == "observe_only"
    assert parsed.authentication is None


@pytest.mark.parametrize(
    "field",
    [
        "device_command_expiry_s",
        "control_period_s",
        "essential_read_timeout_s",
        "kernel_timeout_s",
        "audit_timeout_s",
        "write_timeout_s",
        "acknowledgement_timeout_s",
        "maximum_jitter_s",
        "renewal_margin_s",
    ],
)
@pytest.mark.parametrize("value", [0, -0.001])
def test_timing_values_must_be_positive(field: str, value: float) -> None:
    """T-UNIT-CONFIG-009 / REQ-TIME-001 / S0: no zero or negative timing budgets."""
    payload = _valid_config()
    payload["timing"][field] = value

    _assert_invalid(payload, location_contains=field)


def test_complete_worst_case_cycle_must_fit_device_expiry_window() -> None:
    """T-UNIT-CONFIG-010 / INV-LEASE-001 / S0: configured renewals need safe headroom."""
    payload = _valid_config()
    payload["timing"].update(
        {
            "device_command_expiry_s": 1.90,
            "essential_read_timeout_s": 0.45,
            "kernel_timeout_s": 0.20,
            "audit_timeout_s": 0.25,
            "write_timeout_s": 0.35,
            "acknowledgement_timeout_s": 0.35,
            "maximum_jitter_s": 0.20,
            "renewal_margin_s": 0.30,
        }
    )

    error = _assert_invalid(payload, location_contains="timing")

    message = str(error).lower()
    assert "budget" in message
    assert "expiry" in message or "renewal" in message


def test_control_period_must_be_shorter_than_authorization_lifetime() -> None:
    """T-UNIT-CONFIG-011 / INV-LEASE-002 / S0: each cycle gets fresh authority."""
    payload = _valid_config()
    payload["timing"]["control_period_s"] = 0.75
    payload["policy"]["authorization_lifetime_s"] = 0.75

    error = _assert_invalid(payload, location_contains="policy")

    assert "authorization" in str(error).lower()


def test_independent_heartbeat_setting_is_rejected() -> None:
    """T-UNIT-CONFIG-011A / INV-LEASE-002 / S0: no driver-owned renewal loop."""
    payload = _valid_config()
    payload["policy"]["heartbeat_interval_s"] = 0.40

    error = _assert_invalid(payload, location_contains="heartbeat_interval_s")

    assert any(item["type"] == "extra_forbidden" for item in error.errors())


def test_authorization_lifetime_must_be_inside_device_expiry_window() -> None:
    """T-UNIT-CONFIG-012 / INV-LEASE-003 / S0: software authority cannot exceed hardware lease."""
    payload = _valid_config()
    payload["policy"]["authorization_lifetime_s"] = payload["timing"]["device_command_expiry_s"]

    error = _assert_invalid(payload, location_contains="policy")

    assert "expiry" in str(error).lower() or "authorization" in str(error).lower()


def test_device_expiry_is_explicit_evidence_backed_and_not_a_two_second_default() -> None:
    """T-UNIT-CONFIG-012A / EVID-PROTO-PQ-LEASE / S0."""
    missing = _valid_config()
    missing["timing"].pop("device_command_expiry_s")
    _assert_invalid(missing, location_contains="device_command_expiry_s")

    missing_evidence = _valid_config()
    missing_evidence["timing"].pop("device_command_expiry_evidence")
    _assert_invalid(missing_evidence, location_contains="device_command_expiry_evidence")

    for commissioned_value in (1.40, 3.70):
        payload = _valid_config()
        payload["timing"]["device_command_expiry_s"] = commissioned_value
        parsed = _validate(payload)
        assert parsed.timing.device_command_expiry_s == commissioned_value


def test_control_period_jitter_and_margin_fit_the_commissioned_window() -> None:
    """T-UNIT-CONFIG-012B / INV-LEASE-001 / S0: cadence also has headroom."""
    payload = _valid_config()
    payload["timing"]["control_period_s"] = 1.76
    payload["policy"]["authorization_lifetime_s"] = 2.0

    _assert_invalid(payload, location_contains="timing")


def test_valid_config_is_frozen_and_retains_explicit_profiles() -> None:
    """T-UNIT-CONFIG-013 / REQ-CONFIG-004 / S1: validated revisions cannot mutate in place."""
    parsed = _validate(_valid_config())

    assert parsed.units[0].transport_profile.value == "waveshare_rtu_over_tcp"
    assert parsed.units[0].protocol_profile.value == "iot"
    with pytest.raises((ValidationError, AttributeError, TypeError)):
        parsed.revision = 8


# --- SYNC_RESILIENCE_AUDIT S1: blocking warning codes are configurable ----------
#
# The decoder generates fault/warning codes as "{prefix}_{bit}" over the fault
# catalog's word prefixes (PCS_Warning0_1, DCDC_Warning0_1, PCS_Fault0_0,
# Stack_Warning0_12, ...), so the old human-name config entries could never
# match anything.  The EE-calibration signals the operator meant to block are
# WARNING bits (PROTOCOL_EVIDENCE 9), and the composition hard-wired
# blocking_warning_codes to the empty set -- the block was silently inert.


def test_blocking_warning_codes_are_configurable_and_default_empty() -> None:
    parsed = _validate(_valid_config())
    assert parsed.policy is not None
    assert parsed.policy.blocking_warning_codes == ()

    payload = _valid_config()
    payload["policy"]["blocking_warning_codes"] = ["PCS_Warning0_1", "DCDC_Warning0_1"]
    parsed = _validate(payload)
    assert parsed.policy is not None
    assert parsed.policy.blocking_warning_codes == ("PCS_Warning0_1", "DCDC_Warning0_1")


def test_blocking_warning_codes_must_be_unique_and_normalized() -> None:
    payload = _valid_config()
    payload["policy"]["blocking_warning_codes"] = ["PCS_Warning0_1", "PCS_Warning0_1"]
    with pytest.raises(ValidationError, match="unique"):
        _validate(payload)

    payload = _valid_config()
    payload["policy"]["blocking_warning_codes"] = [" PCS_Warning0_1"]
    with pytest.raises(ValidationError):
        _validate(payload)


# --- excess-solar accelerated charging gates (API_CONTRACTS "Excess-solar
# --- accelerated charging (advisory)") ----------------------------------------
#
# The feature is OFF by default (an absent block is identical to disabled),
# and an enabled block is commissioned only for a write-enabled deployment
# with a policy, coherent hysteresis, a cap inside the static unit charge
# limit, a satisfiable freshness bound, and a bounded intent TTL.


def _excess_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"enabled": True}
    payload.update(overrides)
    return payload


def _assert_excess_rule(payload: dict[str, Any], *, message_contains: str) -> ValidationError:
    """Refuse the block by its OWN commissioning rule, not by key ignorance.

    Today the block is unknown, so pydantic answers ``extra_forbidden`` while
    echoing the (substring-bearing) input back — a spurious pass.  The rule is
    only implemented when the refusal is a real value error whose location is
    the excess_charging block itself.
    """
    with pytest.raises(ValidationError) as caught:
        _validate(payload)
    error = caught.value
    excess_errors = [
        item for item in error.errors() if item["loc"] and item["loc"][0] == "excess_charging"
    ]
    assert excess_errors, f"expected an excess_charging error, got {error.errors()!r}"
    assert all(item["type"] != "extra_forbidden" for item in excess_errors), (
        "the excess_charging block must be a known key refused by its commissioning rule, "
        "not rejected as an unknown key"
    )
    assert message_contains in str(error).lower()
    return error


def test_excess_charging_is_disabled_by_default() -> None:
    """T-UNIT-CONFIG-014 / advisory feature default-off / S0."""
    parsed = _validate(_valid_config())

    assert getattr(parsed, "excess_charging", "__missing__") is None, (
        "no excess_charging block means the feature is entirely absent"
    )

    explicit_off = _valid_config()
    explicit_off["excess_charging"] = {"enabled": False}
    parsed_off = _validate(explicit_off)
    assert parsed_off.excess_charging.enabled is False


def test_excess_charging_enabled_validates_and_carries_pinned_defaults() -> None:
    """T-UNIT-CONFIG-015 / advisory block defaults / S1."""
    payload = _valid_config()
    payload["excess_charging"] = _excess_payload()

    parsed = _validate(payload)

    block = parsed.excess_charging
    assert block.enabled is True
    assert block.export_headroom_margin_w == 200
    assert block.max_charge_from_export_w == 2500
    assert block.export_telemetry_max_age_s == 3.0
    assert block.assumed_autonomous_charge_w == 520
    assert block.min_acceleration_w == 100
    assert block.exit_hysteresis_w == 50
    assert block.intent_ttl_s == 10.0


def test_excess_charging_requires_write_enabled_mode() -> None:
    """T-UNIT-CONFIG-016 / advisory needs an actuating composition / S0."""
    payload = _valid_config(mode="observe_only")
    payload.pop("policy", None)
    payload["excess_charging"] = _excess_payload()

    _assert_excess_rule(payload, message_contains="write_enabled")


def test_excess_charging_requires_a_policy_block() -> None:
    """T-UNIT-CONFIG-016A / advisory bounds derive from the policy / S0.

    The write-enabled-without-policy refusal already exists; this test pins
    that the EXCESS block itself names the policy requirement when enabled.
    """
    payload = _valid_config(mode="write_enabled")
    payload.pop("policy")
    payload["excess_charging"] = _excess_payload()

    _assert_excess_rule(payload, message_contains="policy")


def test_excess_charging_hysteresis_must_be_coherent() -> None:
    """T-UNIT-CONFIG-017 / entry margin strictly above exit margin / S0."""
    payload = _valid_config()
    payload["excess_charging"] = _excess_payload(exit_hysteresis_w=100)

    _assert_excess_rule(payload, message_contains="hysteresis")


def test_excess_charging_cap_must_not_exceed_the_static_unit_charge_limit() -> None:
    """T-UNIT-CONFIG-018 / an additional min() term only / S0."""
    payload = _valid_config()
    payload["excess_charging"] = _excess_payload(max_charge_from_export_w=2501)

    _assert_excess_rule(payload, message_contains="max_unit_charge_w")


def test_excess_charging_freshness_must_be_satisfiable_by_the_polling_loop() -> None:
    """T-UNIT-CONFIG-019 / a fresher demand than the plan serves collapses the
    bound permanently: refuse it at configuration time / S0."""
    payload = _valid_config()
    # control 0.40 + essential read 0.10 = 0.50; a bound of exactly 0.50 can
    # never be strictly satisfied cycle over cycle.
    payload["excess_charging"] = _excess_payload(export_telemetry_max_age_s=0.50)

    _assert_excess_rule(payload, message_contains="control_period_s")


def test_excess_charging_intent_ttl_is_bounded() -> None:
    """T-UNIT-CONFIG-020 / short-lived intents only / S1."""
    too_long = _valid_config()
    too_long["excess_charging"] = _excess_payload(intent_ttl_s=300.5)
    _assert_excess_rule(too_long, message_contains="300")

    not_renewable = _valid_config()
    not_renewable["excess_charging"] = _excess_payload(intent_ttl_s=0.20)
    _assert_excess_rule(not_renewable, message_contains="control_period_s")
