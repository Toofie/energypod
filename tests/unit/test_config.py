"""Contract tests for strict, fail-closed runtime configuration.

These tests intentionally name the public configuration surface before its implementation exists.
Missing contract modules are reported as ordinary test failures so the suite still collects during
the red TDD phase.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
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


def test_the_pod_autonomy_signature_band_is_optional_and_positive() -> None:
    """ADD-1: the commissioned ceiling the arm preflight attributes to a pod's
    own autonomous charge objective.  Absent keeps the strict preflight; a
    non-positive band is a configuration error."""
    parsed = _validate(_valid_config())
    assert parsed.policy is not None
    assert parsed.policy.autonomous_charge_signature_max_w is None

    payload = _valid_config()
    payload["policy"]["autonomous_charge_signature_max_w"] = 2500
    parsed = _validate(payload)
    assert parsed.policy is not None
    assert parsed.policy.autonomous_charge_signature_max_w == 2500

    payload = _valid_config()
    payload["policy"]["autonomous_charge_signature_max_w"] = 0
    with pytest.raises(ValidationError):
        _validate(payload)


# --- excess-solar accelerated charging gates (API_CONTRACTS "Excess-solar
# --- accelerated charging (advisory)") ----------------------------------------
#
# The feature is OFF by default (an absent block composes nothing), and a
# PRESENT block — enabled or explicitly disabled (P6) — is commissioned only
# for a write-enabled deployment with a policy, coherent hysteresis, a cap
# inside the static unit charge limit, a satisfiable freshness bound, and a
# bounded intent TTL.


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


# --- P6 rescope (DESIGN_EXCESS_ACTIVATION): the commissioning gates bind to
# --- block-PRESENT, not enabled — an explicit disabled block that could
# --- never be enabled safely is refused at validation time; an absent block
# --- still changes nothing anywhere.


def test_the_documented_trial_shape_validates_inside_every_gate() -> None:
    """DESIGN_EXCESS_ACTIVATION §4 / B4: the live-write example's documented
    trial wiring — an explicit `enabled: false` block with the 500 W trial
    cap — parses cleanly against the commissioned policy and timing, so the
    operator can uncomment exactly what the example shows."""
    payload = _valid_config()
    payload["excess_charging"] = {"enabled": False, "max_charge_from_export_w": 500}

    parsed = _validate(payload)

    assert parsed.excess_charging is not None
    assert parsed.excess_charging.enabled is False
    assert parsed.excess_charging.max_charge_from_export_w == 500


def test_a_disabled_excess_block_still_requires_write_enabled_mode() -> None:
    payload = _valid_config(mode="observe_only")
    payload["excess_charging"] = {"enabled": False}
    _assert_excess_rule(payload, message_contains="write_enabled")


def test_a_disabled_excess_block_still_carries_every_commissioning_gate() -> None:
    """The runtime toggle flips participation only (P5) — it can never raise
    the cap or relax a gate — so a disabled block with an unsatisfiable cap
    or incoherent hysteresis must be refused BEFORE it could be enabled."""
    bad_cap = _valid_config()
    bad_cap["excess_charging"] = {"enabled": False, "max_charge_from_export_w": 2501}
    _assert_excess_rule(bad_cap, message_contains="max_unit_charge_w")

    bad_hysteresis = _valid_config()
    bad_hysteresis["excess_charging"] = {"enabled": False, "exit_hysteresis_w": 100}
    _assert_excess_rule(bad_hysteresis, message_contains="hysteresis")

    bad_freshness = _valid_config()
    bad_freshness["excess_charging"] = {"enabled": False, "export_telemetry_max_age_s": 0.50}
    _assert_excess_rule(bad_freshness, message_contains="control_period_s")


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


# --- self-healing awareness layer (2026-08-24 recovery research, R4/P1 vi+iii) ---


def test_policy_carries_the_recovery_detection_defaults() -> None:
    """The actuation-coherence watchdog and the autonomy band commission with
    pinned defaults, so an unchanged policy keeps today's behavior while the
    keys stay explicit on the live-write example."""
    parsed = _validate(_valid_config())

    policy = parsed.policy
    assert policy is not None
    assert policy.actuation_coherence_cycles == 4
    assert policy.actuation_coherence_min_movement_w == 150
    assert tuple(policy.expected_autonomy_band_w) == (-2600, 300)


def test_recovery_detection_keys_are_commissionable() -> None:
    """Every key overrides cleanly; the band accepts the commissioned signed
    pair spelling."""
    payload = _valid_config()
    payload["policy"].update(
        {
            "actuation_coherence_cycles": 6,
            "actuation_coherence_min_movement_w": 200,
            "expected_autonomy_band_w": [-3000, 500],
        }
    )

    parsed = _validate(payload)

    assert parsed.policy is not None
    assert parsed.policy.actuation_coherence_cycles == 6
    assert parsed.policy.actuation_coherence_min_movement_w == 200
    assert tuple(parsed.policy.expected_autonomy_band_w) == (-3000, 500)


def test_actuation_coherence_keys_must_be_positive() -> None:
    """A zero or negative streak/door threshold is a commissioning error: the
    watchdog would alarm on its first ambiguous cycle or never clear."""
    for key, bad in (
        ("actuation_coherence_cycles", 0),
        ("actuation_coherence_cycles", -4),
        ("actuation_coherence_min_movement_w", 0),
        ("actuation_coherence_min_movement_w", -150),
    ):
        payload = _valid_config()
        payload["policy"][key] = bad
        with pytest.raises(ValidationError, match=key):
            _validate(payload)


def test_expected_autonomy_band_must_span_the_self_charge_region() -> None:
    """The band is the commissioned EXPECTED autonomous envelope: negative
    self-charge up to a small positive float.  A pair that does not span zero,
    is not ascending, or is not exactly two integers is refused."""
    for bad in (
        [300, -2600],  # descending
        [-2600, -2600],  # not strictly ascending
        [100, 200],  # no self-charge region: the pods charge themselves negative
        [-300, -100],  # no float region: idle pods sit near zero
        [1, 2, 3],  # not a pair
        [-2600],  # not a pair
        [-2600.5, 300],  # not integers
        ["-2600", 300],  # not integers
    ):
        payload = _valid_config()
        payload["policy"]["expected_autonomy_band_w"] = bad
        with pytest.raises(ValidationError, match="expected_autonomy_band_w"):
            _validate(payload)


# --- night-writer detector (API_CONTRACTS "Night-writer detector") ----------------


def test_policy_carries_the_foreign_objective_defaults() -> None:
    """The detector composes ALWAYS with defaulted policy keys, so an
    unchanged policy keeps the pinned sampling/escalation posture -- and the
    STRICT posture (no expected writer commissioned) until the operator says
    otherwise."""
    parsed = _validate(_valid_config())

    policy = parsed.policy
    assert policy is not None
    assert policy.foreign_objective_sample_interval_s == 30.0
    assert policy.foreign_objective_sustained_samples == 3
    assert policy.foreign_objective_self_charge_class_w == 1000
    assert policy.foreign_objective_handback_grace_s == 12.0
    assert policy.foreign_objective_expected_charge_w is None
    assert policy.foreign_objective_expected_min_units == 2


def test_foreign_objective_keys_are_commissionable() -> None:
    payload = _valid_config()
    payload["policy"].update(
        {
            "foreign_objective_sample_interval_s": 60.0,
            "foreign_objective_sustained_samples": 5,
            "foreign_objective_self_charge_class_w": 1500,
            "foreign_objective_handback_grace_s": 30.0,
            "foreign_objective_expected_charge_w": 2500,
            "foreign_objective_expected_min_units": 3,
        }
    )

    parsed = _validate(payload)

    assert parsed.policy is not None
    assert parsed.policy.foreign_objective_sample_interval_s == 60.0
    assert parsed.policy.foreign_objective_sustained_samples == 5
    assert parsed.policy.foreign_objective_self_charge_class_w == 1500
    assert parsed.policy.foreign_objective_handback_grace_s == 30.0
    assert parsed.policy.foreign_objective_expected_charge_w == 2500
    assert parsed.policy.foreign_objective_expected_min_units == 3


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("foreign_objective_sample_interval_s", 0.0),
        ("foreign_objective_sample_interval_s", 3601.0),
        ("foreign_objective_sustained_samples", 0),
        ("foreign_objective_sustained_samples", 101),
        ("foreign_objective_self_charge_class_w", 0),
        ("foreign_objective_self_charge_class_w", 50001),
        ("foreign_objective_handback_grace_s", 0.0),
        ("foreign_objective_handback_grace_s", 301.0),
        ("foreign_objective_expected_charge_w", 0),
        ("foreign_objective_expected_charge_w", 50001),
        ("foreign_objective_expected_min_units", 1),
        ("foreign_objective_expected_min_units", 51),
    ],
)
def test_foreign_objective_keys_are_bounded(key: str, bad: Any) -> None:
    """Every knob has a sane bound (API_CONTRACTS): a sub-second interval or a
    beyond-hours grace is a commissioning error, refused at configuration
    time rather than misbehaving at runtime."""
    payload = _valid_config()
    payload["policy"][key] = bad
    with pytest.raises(ValidationError, match=key):
        _validate(payload)


def test_the_live_write_example_documents_the_detector_keys() -> None:
    """The deployed example (the live controller's own config) documents the
    six keys -- the four defaults, the site's own scheduled night writer
    commissioned as the expected charge, and its minimum synchronized-group
    size -- so the detector's posture is
    visible to the operator reading their configuration."""
    text = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("config", "config.live-write-example.yaml")
        .read_text(encoding="utf-8")
    )

    for key, value in (
        ("foreign_objective_sample_interval_s", "30.0"),
        ("foreign_objective_sustained_samples", "3"),
        ("foreign_objective_self_charge_class_w", "1000"),
        ("foreign_objective_handback_grace_s", "12.0"),
        ("foreign_objective_expected_charge_w", "2500"),
        ("foreign_objective_expected_min_units", "2"),
    ):
        assert re.search(rf"^\s*{key}:\s*{value}\s*$", text, re.MULTILINE), key


# --- DESIGN_SCHEDULES §3/B5: the schedule config block ---------------------------


def _assert_schedule_rule(payload: dict[str, Any], *, message_contains: str | None = None) -> Any:
    """Validate a payload whose schedule block must be refused by its own rule."""
    with pytest.raises(ValidationError) as error:
        ControllerConfig.model_validate(payload)
    schedule_errors = [
        item for item in error.value.errors() if item["loc"] and item["loc"][0] == "schedule"
    ]
    assert schedule_errors, f"expected a schedule error, got {error.value.errors()!r}"
    if message_contains is not None:
        assert message_contains.lower() in str(schedule_errors[0]["msg"]).lower(), schedule_errors
    return error


def test_schedule_block_is_absent_by_default() -> None:
    parsed = ControllerConfig.model_validate(_valid_config())
    assert parsed.schedule is None, "no schedule block means the surface is entirely absent"


def test_a_present_schedule_block_carries_the_day_default() -> None:
    payload = _valid_config()
    payload["schedule"] = {}
    parsed = ControllerConfig.model_validate(payload)
    assert parsed.schedule is not None
    assert parsed.schedule.allowed_windows_local == (("06:00", "20:00"),)
    assert parsed.schedule.intent_ttl_s == 10.0


def test_a_widened_night_policy_validates_and_is_spelled_canonically() -> None:
    payload = _valid_config()
    payload["schedule"] = {
        "allowed_windows_local": [["00:00", "06:00"], ["06:00", "20:00"]],
        "intent_ttl_s": 12.0,
    }
    parsed = ControllerConfig.model_validate(payload)
    assert parsed.schedule is not None
    assert parsed.schedule.allowed_windows_local == (
        ("00:00", "06:00"),
        ("06:00", "20:00"),
    )
    assert parsed.schedule.intent_ttl_s == 12.0


@pytest.mark.parametrize(
    ("windows", "message"),
    [
        ([], "allowed_windows_local"),
        ([["06:00", "06:00"]], "zero-length"),
        ([["6:00", "20:00"]], "HH:MM"),
        ([["06:00"]], "field required"),
    ],
)
def test_malformed_allowed_windows_are_refused(windows: list[Any], message: str) -> None:
    payload = _valid_config()
    payload["schedule"] = {"allowed_windows_local": windows}
    _assert_schedule_rule(payload, message_contains=message)


@pytest.mark.parametrize("ttl", [0.0, -1.0, 301.0])
def test_schedule_intent_ttl_must_stay_inside_the_commissioned_bounds(ttl: float) -> None:
    payload = _valid_config()
    payload["schedule"] = {"intent_ttl_s": ttl}
    # 0/-1 fall at the field bound; 301 falls at the commissioned cap.
    _assert_schedule_rule(payload, message_contains="intent_ttl_s" if ttl > 1 else None)


def test_schedule_intent_ttl_must_exceed_the_control_period() -> None:
    payload = _valid_config()
    payload["schedule"] = {"intent_ttl_s": 0.10}  # <= control_period_s 0.40
    _assert_schedule_rule(payload, message_contains="control_period")


def test_the_schedule_block_has_no_enabled_key() -> None:
    """The plan IS the state: a second master switch would be a second way to
    be silently off (the invisible-starvation failure class)."""
    payload = _valid_config()
    payload["schedule"] = {"enabled": True}
    with pytest.raises(ValidationError) as error:
        ControllerConfig.model_validate(payload)
    assert any(
        item["type"] == "extra_forbidden" and item["loc"][0] == "schedule"
        for item in error.value.errors()
    )


def test_excess_charging_yield_to_schedule_defaults_true() -> None:
    payload = _valid_config()
    payload["excess_charging"] = {"enabled": False}
    parsed = ControllerConfig.model_validate(payload)
    assert parsed.excess_charging is not None
    assert parsed.excess_charging.yield_to_schedule is True
    payload["excess_charging"] = {"enabled": False, "yield_to_schedule": False}
    parsed_off = ControllerConfig.model_validate(payload)
    assert parsed_off.excess_charging is not None
    assert parsed_off.excess_charging.yield_to_schedule is False


# --- DESIGN_ENERGY_SCORECARD section 7 (E7): the energy_scorecard block ----------


def _assert_energy_rule(payload: dict[str, Any], *, message_contains: str | None = None) -> Any:
    """Validate a payload whose energy_scorecard block must be refused."""
    with pytest.raises(ValidationError) as error:
        ControllerConfig.model_validate(payload)
    energy_errors = [
        item
        for item in error.value.errors()
        if item["loc"] and item["loc"][0] == "energy_scorecard"
    ]
    assert energy_errors, f"expected an energy_scorecard error, got {error.value.errors()!r}"
    if message_contains is not None:
        assert message_contains.lower() in str(energy_errors[0]["msg"]).lower(), energy_errors
    return error


def test_energy_scorecard_block_is_absent_by_default() -> None:
    parsed = ControllerConfig.model_validate(_valid_config())
    assert parsed.energy_scorecard is None, "no block means the surface is entirely absent"


def test_a_present_energy_block_carries_the_pinned_defaults() -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {}
    parsed = ControllerConfig.model_validate(payload)

    assert parsed.energy_scorecard is not None
    assert parsed.energy_scorecard.grid_source == "integrated"
    assert parsed.energy_scorecard.grid_counter_roles == "unpinned"
    assert parsed.energy_scorecard.integration_max_gap_s == 10.0
    assert parsed.energy_scorecard.min_day_coverage_pct == 95.0
    assert parsed.energy_scorecard.tariff is None, "absent tariff = kWh only, no money"


def test_the_commissioned_spellings_validate_and_round_trip() -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {
        "grid_source": "device_counter",
        "grid_counter_roles": "vendor_labels",
        "integration_max_gap_s": 12.5,
        "min_day_coverage_pct": 90.0,
        "tariff": {
            "currency": "AUD",
            "import_cents_per_kwh": 28.0,
            "export_cents_per_kwh": 9.0,
        },
    }
    parsed = ControllerConfig.model_validate(payload)

    assert parsed.energy_scorecard is not None
    assert parsed.energy_scorecard.grid_source == "device_counter"
    assert parsed.energy_scorecard.grid_counter_roles == "vendor_labels"
    assert parsed.energy_scorecard.tariff is not None
    assert parsed.energy_scorecard.tariff.currency == "AUD"


def test_device_counter_is_refused_while_the_roles_are_unpinned() -> None:
    """The structural A-1 gate: the role-open counter pair must never become
    the display source -- pinning the roles first is the operator's act."""
    payload = _valid_config()
    payload["energy_scorecard"] = {"grid_source": "device_counter"}
    _assert_energy_rule(payload, message_contains="unpinned")


@pytest.mark.parametrize("roles", ["vendor_labels", "swapped"])
def test_a_pinned_roles_value_validates_at_config_time(roles: str) -> None:
    """Config validation accepts a pinned roles value -- the DURABLE pinning
    fact is the composition root's keyed boot check (the excess-economics
    precedent), pinned in the composition family."""
    payload = _valid_config()
    payload["energy_scorecard"] = {"grid_counter_roles": roles}
    parsed = ControllerConfig.model_validate(payload)
    assert parsed.energy_scorecard is not None
    assert parsed.energy_scorecard.grid_counter_roles == roles


@pytest.mark.parametrize("gap", [0.0, -10.0, 0.40, 60.5])
def test_integration_gap_must_exceed_the_control_period_and_stay_le_60(gap: float) -> None:
    """The CT stream samples once per control cycle: a gap at or below it
    would exclude every interval; above 60 s is an outage, not a cadence."""
    payload = _valid_config()
    payload["energy_scorecard"] = {"integration_max_gap_s": gap}
    _assert_energy_rule(payload)


def test_integration_gap_just_above_the_control_period_validates() -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {"integration_max_gap_s": 0.41}
    parsed = ControllerConfig.model_validate(payload)
    assert parsed.energy_scorecard is not None
    assert parsed.energy_scorecard.integration_max_gap_s == 0.41


@pytest.mark.parametrize("coverage", [0.0, -1.0, 100.5])
def test_min_day_coverage_must_stay_inside_zero_to_one_hundred(coverage: float) -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {"min_day_coverage_pct": coverage}
    _assert_energy_rule(payload)


@pytest.mark.parametrize(
    "tariff",
    [
        {"currency": "AU", "import_cents_per_kwh": 28.0, "export_cents_per_kwh": 9.0},
        {"currency": "DOLLARS", "import_cents_per_kwh": 28.0, "export_cents_per_kwh": 9.0},
        {"currency": "AUD", "import_cents_per_kwh": -1.0, "export_cents_per_kwh": 9.0},
        {"currency": "AUD", "import_cents_per_kwh": 28.0, "export_cents_per_kwh": -9.0},
        {"currency": "AUD"},
    ],
)
def test_malformed_tariff_keys_are_refused(tariff: dict[str, Any]) -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {"tariff": tariff}
    _assert_energy_rule(payload)


def test_the_energy_block_has_no_enabled_key() -> None:
    """The schedules doctrine: a second master switch is a second way to be
    silently off.  Decommissioning is removing the block; the scorecard is
    advisory-only and needs no gate to start accumulating evidence."""
    payload = _valid_config()
    payload["energy_scorecard"] = {"enabled": True}
    with pytest.raises(ValidationError) as error:
        ControllerConfig.model_validate(payload)
    assert any(
        item["type"] == "extra_forbidden" and item["loc"][0] == "energy_scorecard"
        for item in error.value.errors()
    )


def test_unknown_energy_keys_are_refused() -> None:
    payload = _valid_config()
    payload["energy_scorecard"] = {"integration_max_gap_s": 10.0, "surplus_source": "hoped"}
    with pytest.raises(ValidationError) as error:
        ControllerConfig.model_validate(payload)
    assert any(
        item["type"] == "extra_forbidden" and item["loc"][0] == "energy_scorecard"
        for item in error.value.errors()
    )


def test_the_live_write_examples_energy_block_validates_as_documented() -> None:
    """DESIGN_ENERGY_SCORECARD section 7 / E7: the live-write example's
    commissioned block — advisory defaults, roles unpinned, the passive A-1
    cross-check from day one — parses cleanly, so what the operator reads is
    what the controller will compose."""
    from pathlib import Path

    import yaml

    example = Path(__file__).resolve().parents[2] / "config" / "config.live-write-example.yaml"
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the example must stay one YAML document"
    assert "energy_scorecard" in document, "the scorecard block is commissioned"

    payload = _valid_config()
    payload["energy_scorecard"] = document["energy_scorecard"]
    parsed = ControllerConfig.model_validate(payload)

    assert parsed.energy_scorecard is not None
    assert parsed.energy_scorecard.grid_source == "integrated"
    assert parsed.energy_scorecard.grid_counter_roles == "unpinned"
    assert parsed.energy_scorecard.tariff is None




# --- off-peak night charge (DESIGN_NIGHT_CHARGE §3.1 + API_CONTRACTS
# --- "Off-peak night charge") ---------------------------------------------------
#
# The block follows the block-presence doctrine: ABSENT composes nothing
# (byte-identical), PRESENT composes the machinery with `enabled` gating
# PARTICIPATION.  Every cross-validation binds to block-PRESENCE — a disabled
# block that could never be enabled safely is refused at validation time —
# and the PARTITION grant is one of them: commissioning night charge and
# widening schedule.allowed_windows_local are ONE deliberate config revision.


def _night_payload(**overrides: Any) -> dict[str, Any]:
    """The commissioned §3.1 shape against the partition-granted schedule."""
    payload: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": [["00:00", "06:00"]],
    }
    payload.update(overrides)
    return payload


def _night_config(**overrides: Any) -> dict[str, Any]:
    payload = _valid_config()
    payload["schedule"] = {"allowed_windows_local": [["00:00", "20:00"]]}
    payload["night_charging"] = _night_payload(**overrides)
    return payload


def _assert_night_rule(payload: dict[str, Any], *, message_contains: str) -> ValidationError:
    """Refuse the block by its OWN commissioning rule, not by key ignorance
    (the excess/schedule pattern: an unimplemented block answers
    ``extra_forbidden`` at the top level, which would be a spurious pass)."""
    with pytest.raises(ValidationError) as caught:
        _validate(payload)
    error = caught.value
    night_errors = [
        item for item in error.errors() if item["loc"] and item["loc"][0] == "night_charging"
    ]
    assert night_errors, f"expected a night_charging error, got {error.errors()!r}"
    assert all(item["type"] != "extra_forbidden" for item in night_errors), (
        "the night_charging block must be a known key refused by its commissioning rule, "
        "not rejected as an unknown key"
    )
    assert message_contains.lower() in str(error).lower()
    return error


def test_night_charging_block_is_absent_by_default() -> None:
    parsed = _validate(_valid_config())
    assert getattr(parsed, "night_charging", "__missing__") is None, (
        "no night_charging block means the feature is entirely absent"
    )


def test_a_present_night_block_composes_with_the_pinned_defaults() -> None:
    """§3.1: every key carries its commissioned default except the REQUIRED
    civil-time facts (timezone and the window's canonical spelling)."""
    parsed = _validate(_night_config())

    block = parsed.night_charging
    assert block is not None
    assert block.timezone == "Australia/Brisbane"
    assert block.window_local == (("00:00", "06:00"),)
    assert block.enabled is False, "suspended at boot is the default (the excess doctrine)"
    assert block.rate_cap_w == 2500
    assert block.demand_threshold_w == 1000
    assert block.demand_exit_hysteresis_w == 200
    assert block.hold_rate_w == 100
    assert block.demand_scope == "fleet"
    assert block.pacing == "cap_first"
    assert block.assumed_capacity_wh is None
    assert block.demand_telemetry_max_age_s == 3.0
    assert block.intent_ttl_s == 10.0


def test_the_timezone_is_a_required_iana_fact() -> None:
    missing = _night_config()
    del missing["night_charging"]["timezone"]
    with pytest.raises(ValidationError, match="timezone"):
        _validate(missing)

    _assert_night_rule(_night_config(timezone="Not/AZone"), message_contains="IANA")


@pytest.mark.parametrize(
    ("windows", "message"),
    [
        ([], "window_local"),
        ([["00:00", "00:00"]], "zero-length"),
        ([["0:00", "06:00"]], "HH:MM"),
        ([["00:00"]], "field required"),
    ],
)
def test_malformed_night_windows_are_refused(windows: list[Any], message: str) -> None:
    _assert_night_rule(_night_config(window_local=windows), message_contains=message)


def test_a_cross_midnight_night_window_is_canonical_and_commissionable() -> None:
    payload = _night_config(window_local=[["22:00", "04:00"]])
    payload["schedule"] = {"allowed_windows_local": [["20:00", "06:00"]]}
    parsed = _validate(payload)
    assert parsed.night_charging is not None
    assert parsed.night_charging.window_local == (("22:00", "04:00"),)


def test_a_disabled_night_block_still_requires_write_enabled_mode() -> None:
    payload = _night_config(enabled=False)
    payload["mode"] = "observe_only"
    payload.pop("authentication", None)
    _assert_night_rule(payload, message_contains="write_enabled")


def test_a_disabled_night_block_still_requires_the_policy_block() -> None:
    payload = _night_config(enabled=False)
    payload.pop("policy")
    _assert_night_rule(payload, message_contains="policy")


def test_night_rate_cap_must_not_exceed_the_static_unit_charge_limit() -> None:
    _assert_night_rule(_night_config(rate_cap_w=2501), message_contains="max_unit_charge_w")


def test_night_hold_rate_must_be_a_positive_charge_below_the_cap() -> None:
    """§2.4's pin: the hold is a small POSITIVE charge — zero hands the pod
    back to matching autonomy, the opposite of the operator's intent."""
    _assert_night_rule(_night_config(hold_rate_w=0), message_contains="hold_rate_w")
    _assert_night_rule(_night_config(hold_rate_w=2500), message_contains="hold_rate_w")


def test_night_hysteresis_must_sit_strictly_inside_the_threshold() -> None:
    _assert_night_rule(_night_config(demand_exit_hysteresis_w=0), message_contains="hysteresis")
    _assert_night_rule(_night_config(demand_exit_hysteresis_w=1000), message_contains="hysteresis")


def test_night_demand_freshness_must_be_satisfiable_by_the_polling_loop() -> None:
    # control 0.40 + essential read 0.10 = 0.50; the bound must EXCEED it.
    _assert_night_rule(
        _night_config(demand_telemetry_max_age_s=0.50), message_contains="control_period_s"
    )


@pytest.mark.parametrize("ttl", [0.20, 300.5])
def test_night_intent_ttl_must_stay_inside_the_commissioned_bounds(ttl: float) -> None:
    _assert_night_rule(
        _night_config(intent_ttl_s=ttl),
        message_contains="300" if ttl > 1 else "control_period_s",
    )


def test_even_pacing_requires_the_capacity_map_with_exactly_the_fleet_units() -> None:
    payload = _night_config(
        pacing="even", assumed_capacity_wh={"mid": 5000, "rhs": 5000, "lhs": 5000}
    )
    parsed = _validate(payload)
    assert parsed.night_charging is not None
    assert parsed.night_charging.assumed_capacity_wh == {"mid": 5000, "rhs": 5000, "lhs": 5000}

    _assert_night_rule(_night_config(pacing="even"), message_contains="assumed_capacity_wh")
    wrong_keys = _night_config(
        pacing="even", assumed_capacity_wh={"mid": 5000, "rhs": 5000, "lhs": 5000, "ghost": 5000}
    )
    _assert_night_rule(wrong_keys, message_contains="fleet")


def test_cap_first_pacing_refuses_a_stray_capacity_map() -> None:
    """`assumed_capacity_wh` present IFF `pacing: even` — a stray map under
    cap_first is a configuration that lies about which pacing rule runs."""
    _assert_night_rule(
        _night_config(assumed_capacity_wh={"mid": 5000, "rhs": 5000, "lhs": 5000}),
        message_contains="assumed_capacity_wh",
    )


def test_the_partition_grant_is_required_and_names_the_widening_path() -> None:
    """§3.2: the schedule block must be PRESENT and its allowed union must
    cover the night window ENTIRELY — one deliberate revision, both blocks."""
    no_schedule = _valid_config()
    no_schedule["night_charging"] = _night_payload()
    _assert_night_rule(no_schedule, message_contains="allowed_windows_local")

    day_only = _valid_config()
    day_only["schedule"] = {"allowed_windows_local": [["06:00", "20:00"]]}
    day_only["night_charging"] = _night_payload()
    error = _assert_night_rule(day_only, message_contains="allowed_windows_local")
    assert "widen" in str(error).lower(), "the refusal names the partition path"


def test_the_partition_grant_joins_two_windows_that_cover_the_night() -> None:
    """The UNION of the allowed pairs covers the night window — two day
    windows plus the granted night hour are a legal partition grant."""
    payload = _night_config(window_local=[["00:00", "06:00"]])
    payload["schedule"] = {
        "allowed_windows_local": [["00:00", "01:00"], ["01:00", "06:00"], ["06:00", "20:00"]]
    }
    parsed = _validate(payload)
    assert parsed.night_charging is not None


def test_a_partial_cover_is_refused() -> None:
    payload = _night_config(window_local=[["00:00", "06:00"]])
    payload["schedule"] = {"allowed_windows_local": [["00:00", "03:00"], ["06:00", "20:00"]]}
    _assert_night_rule(payload, message_contains="allowed_windows_local")


def test_unknown_night_keys_are_refused() -> None:
    payload = _night_config(surprise=True)
    with pytest.raises(ValidationError) as caught:
        _validate(payload)
    assert any(
        item["type"] == "extra_forbidden" and item["loc"][0] == "night_charging"
        for item in caught.value.errors()
# --- plant history (DESIGN_PLANT_HISTORY section 2.5, H3) ------------------------


def test_plant_history_defaults_validate_and_round_trip() -> None:
    """DESIGN section 2.5: the three keys with their commissioned defaults;
    every key may be stated explicitly too."""
    payload = _valid_config()
    payload["plant_history"] = {}
    parsed = _validate(payload)
    assert parsed.plant_history is not None
    assert parsed.plant_history.sample_interval_s == 30.0
    assert parsed.plant_history.retention_full_resolution_days == 14
    assert parsed.plant_history.retention_rollup_days == 0

    payload["plant_history"] = {
        "sample_interval_s": 15.0,
        "retention_full_resolution_days": 30,
        "retention_rollup_days": 365,
    }
    parsed = _validate(payload)
    assert parsed.plant_history is not None
    assert parsed.plant_history.sample_interval_s == 15.0
    assert parsed.plant_history.retention_full_resolution_days == 30
    assert parsed.plant_history.retention_rollup_days == 365


@pytest.mark.parametrize(
    ("block", "location_contains"),
    [
        # sample_interval_s must exceed the control period (0.40 s here) and
        # stay at or below one hour.
        ({"sample_interval_s": 0.40}, "plant_history"),
        ({"sample_interval_s": 3600.5}, "plant_history"),
        ({"sample_interval_s": 0.0}, "plant_history"),
        # retention_full_resolution_days is 1..3650.
        ({"retention_full_resolution_days": 0}, "retention_full_resolution_days"),
        ({"retention_full_resolution_days": 3651}, "retention_full_resolution_days"),
        # retention_rollup_days is 0 (forever) or a positive day count.
        ({"retention_rollup_days": -1}, "retention_rollup_days"),
        # There is deliberately NO enabled key (the block-presence doctrine).
        ({"enabled": True}, "enabled"),
    ],
)
def test_plant_history_matrix_rejects_out_of_bounds_and_unknown_keys(
    block: dict[str, Any], location_contains: str
) -> None:
    payload = _valid_config()
    payload["plant_history"] = block
    _assert_invalid(payload, location_contains=location_contains)


def test_a_present_plant_history_block_requires_the_storage_block() -> None:
    """DESIGN section 2.5: durable history is the entire point -- a historian
    silently keeping its rows in the memory of a database-less deployment is
    exactly the invisible-off class this project refuses."""
    payload = _valid_config()
    payload.pop("storage")
    payload["plant_history"] = {}
    error = _assert_invalid(payload, location_contains="plant_history")
    assert any("storage" in str(item["msg"]) for item in error.errors()), (
        "the refusal must name the missing storage block"
    )
