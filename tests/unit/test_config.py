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
            "device_command_expiry_s": 2.35,
            "device_command_expiry_evidence": "commissioning://watchdog-trial-2026-08/rev-1",
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
