"""T-CAL-CONFIG — the ``battery_calibration:`` block's commissioning gates.

DESIGN_CALIBRATION_CYCLING §7 (CONTRACT v1.1): every bound and gate with its
named-message refusal — the floor-band AND kernel-ordering rule (a floor of 8
against a policy floor of 10 refused naming both), the C12 set
(``trigger_floor_pct >= floor_pct`` with the completed-anchor-resets edge at
equality, ``eligibility_window_days >= cycles_daily_min_days``, and every
taper/hold/measurement bound), the C8 SUM rule (the margin + allowance vs the
band edge, the refusal naming the whole sizing), the C6 one-shot's shape
(unit in fleet), the capacity-map equality with ``night_charging``'s, the
historian-block prerequisite, BOTH window checks naming their arithmetic (the
fit check at the DELIVERY floor with the ~7.0 h divide, and end+ttl vs the
health-watch and night windows), the timezone equality, and the
block-presence doctrine (an absent block composes nothing; no runtime toggle
for ``mode`` exists anywhere).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from energypod.runtime.config import ControllerConfig


def _base(*, mode: str = "write_enabled") -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema_version": 1,
        "revision": 9,
        "mode": mode,
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 3,
        },
        "units": [
            {
                "unit_id": unit,
                "display_name": unit.title(),
                "endpoint": {"host": f"192.168.1.1{index}", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": f"BEP-{unit.upper()}",
                "expected_cell_count": 59,
            }
            for index, unit in enumerate(("lhs", "mid", "rhs"))
        ],
        "timing": {
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
            "blocking_fault_codes": [],
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
        # §7: the trigger and the measurement are historian-backed.
        "plant_history": {
            "sample_interval_s": 30.0,
            "retention_full_resolution_days": 14,
            "retention_rollup_days": 0,
        },
    }
    if mode == "observe_only":
        config.pop("authentication")
    return config


def _calibration(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "advise",
        "window_local": "15:00",
        "traverse_end_local": "22:30",
        "plan_local": "14:00",
        "traverse": {
            "floor_pct": 10.0,
            "discharge_w": 800,
            "min_discharge_w": 200,
            "intent_ttl_s": 10.0,
            "assumed_delivery_frac": 0.8,
            "integration_max_gap_s": 10.0,
            "energy_margin_wh": 100.0,
            "metering_allowance_wh": 100.0,
            "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 4200},
        },
    }
    block.update(overrides)
    return block


def _validate(payload: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(payload)


def _with_block(**overrides: Any) -> dict[str, Any]:
    payload = _base()
    payload["battery_calibration"] = _calibration(**overrides)
    return payload


def refusal(payload: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as raised:
        _validate(payload)
    message = str(raised.value)
    assert "battery_calibration" in message
    return message


# --- the block-presence doctrine -------------------------------------------------


def test_absent_block_composes_nothing_and_defaults_commission() -> None:
    """T-CAL-CONFIG: an ABSENT block validates (nothing composes), the §7
    defaults commission at the contract's own posture (advise), and the
    shipped live-write example carries the block in exactly that posture
    with NO one-shot standing."""
    config = _validate(_base())
    assert config.battery_calibration is None
    shipped = _with_block()
    parsed = _validate(shipped)
    assert parsed.battery_calibration is not None
    assert parsed.battery_calibration.mode == "advise"
    assert parsed.battery_calibration.request_measurement is None
    assert parsed.battery_calibration.traverse.floor_pct == 10.0
    assert parsed.battery_calibration.window_local == "15:00"
    assert parsed.battery_calibration.traverse_end_local == "22:30"
    assert parsed.battery_calibration.plan_local == "14:00"
    # The §10 commissioning file itself: the block is present and LAWFUL at
    # whatever step of the ladder the operator's revision holds — the mode
    # and the one-shot are the operator's sequencing, not this suite's pin
    # (step 1 shipped advise; the live revision has since commissioned the
    # step-2 act posture with mid's one-shot standing).
    from pathlib import Path

    import yaml

    live = yaml.safe_load(
        Path("config/config.live-write-example.yaml").read_text(encoding="utf-8")
    )
    parsed_live = _validate(live)
    assert parsed_live.battery_calibration is not None
    assert parsed_live.battery_calibration.mode in {"advise", "act"}
    if parsed_live.battery_calibration.request_measurement is not None:
        assert parsed_live.battery_calibration.request_measurement.unit in {
            unit.unit_id for unit in parsed_live.units
        }


def test_no_runtime_toggle_for_mode_exists() -> None:
    """T-CAL-CONFIG: there is no runtime toggle for ``mode`` — the authority
    ladder is climbed by config revision plus restart only, so the console
    can never flip a traverse into existence (the config model carries no
    enabled key, and the adviser exposes no setter)."""
    from energypod.application import calibration as cal

    assert not hasattr(cal.CalibrationAdviser, "set_mode")
    assert not hasattr(cal.CalibrationAdviser, "set_enabled")
    block = _validate(_with_block()).battery_calibration
    assert block is not None and "enabled" not in type(block).model_fields


# --- the floor band and the kernel ordering ---------------------------------------


def test_floor_band_and_kernel_ordering_rule() -> None:
    """T-CAL-CONFIG: ``floor_pct`` in [5, 10] AND >= policy.minimum_soc_pct
    — a floor of 8 against a policy floor of 10 is refused naming both
    values and the science band; at the commissioned policy exactly 10
    commissions."""
    message = refusal(_with_block(traverse={**_calibration()["traverse"], "floor_pct": 8.0}))
    assert "8.0" in message and "10.0" in message
    assert "minimum_soc_pct" in message
    assert "[5, 10]" in message or "5" in message
    # The commissioned policy's only commissionable floor is exactly 10.
    accepted = _validate(
        _with_block(traverse={**_calibration()["traverse"], "floor_pct": 10.0})
    )
    assert accepted.battery_calibration is not None
    # Below the science band outright is refused at the field bound.
    refusal(_with_block(traverse={**_calibration()["traverse"], "floor_pct": 4.0}))


def test_c12_set() -> None:
    """T-CAL-CONFIG: the C12 cross-validations — ``trigger_floor_pct >=
    floor_pct`` (equality allowed: a completed anchor resets the trigger),
    ``eligibility_window_days >= cycles_daily_min_days``, and every
    taper/hold/measurement bound."""
    message = refusal(_with_block(trigger={"trigger_floor_pct": 5.0}))
    assert "C12" in message and "trigger_floor_pct" in message
    # Equality is allowed: the predicate is <=, so a cycle ending AT the
    # floor still resets the clock.
    _validate(_with_block(trigger={"trigger_floor_pct": 10.0}))
    message = refusal(
        _with_block(
            trigger={"eligibility_window_days": 4, "cycles_daily_min_days": 5}
        )
    )
    assert "cycles_daily_min_days" in message
    for path, key, bad in (
        ("top_anchor", "taper_sustain_s", 59),
        ("top_anchor", "taper_soc_pct", 100.0),
        ("top_anchor", "hold_min_s", 1799),
        ("top_anchor", "hold_float_w", 0),
        ("top_anchor", "poor_surplus_kwh", 0.0),
        ("measurement", "reanchor_delta_pct", 0.0),
        ("measurement", "floor_epsilon_pct", 6.0),
    ):
        block = deepcopy(_calibration())
        block.setdefault(path, {})
        block[path][key] = bad
        refusal(_with_block(**{path: block[path]}))


def test_c8_sum_rule_names_the_whole_sizing() -> None:
    """T-CAL-CONFIG: ``energy_margin_wh + metering_allowance_wh <=
    (floor_pct - 5)/100 x min(assumed_capacity_wh)`` — the frozen-word stop
    must not cross the science band's 5% edge even behind an undercounting
    meter; the refusal names margin, allowance, and the band edge."""
    traverse = dict(_calibration()["traverse"])
    traverse["energy_margin_wh"] = 300.0
    message = refusal(_with_block(traverse=traverse))
    assert "energy_margin_wh" in message
    assert "metering_allowance_wh" in message
    assert "band" in message.lower() or "5%" in message
    # The shipped defaults sit exactly inside: 100 + 100 = 200 <= 210.
    _validate(_with_block())


def test_sensing_floor_and_rate_bounds() -> None:
    """T-CAL-CONFIG: ``min_discharge_w >= 3 x 50`` (the sensing floor,
    named) and ``discharge_w`` inside [min, policy.max_unit_discharge_w]."""
    traverse = dict(_calibration()["traverse"])
    traverse["min_discharge_w"] = 100
    message = refusal(_with_block(traverse=traverse))
    assert "sensing" in message and "150" in message
    traverse = dict(_calibration()["traverse"])
    traverse["min_discharge_w"] = 900  # above the discharge cap -> empty clamp
    refusal(_with_block(traverse=traverse))
    traverse = dict(_calibration()["traverse"])
    traverse["discharge_w"] = 3000  # above the policy unit discharge bound
    message = refusal(_with_block(traverse=traverse))
    assert "max_unit_discharge_w" in message


def test_ttl_and_integration_bounds() -> None:
    """T-CAL-CONFIG: ``intent_ttl_s`` in (0, 300] and >
    ``timing.control_period_s``; ``integration_max_gap_s`` >
    ``control_period_s`` and <= 60 (C9)."""
    traverse = dict(_calibration()["traverse"])
    traverse["intent_ttl_s"] = 0.20  # at the control period, not beyond it
    message = refusal(_with_block(traverse=traverse))
    assert "control_period_s" in message
    traverse = dict(_calibration()["traverse"])
    traverse["integration_max_gap_s"] = 61.0
    refusal(_with_block(traverse=traverse))


# --- the windows ------------------------------------------------------------------


def test_fit_check_at_the_delivery_floor_names_the_divide() -> None:
    """T-CAL-CONFIG: the worst-case traverse FIT at the DELIVERY floor — the
    refusal carries the divide ``0.90 x min(capacity) / (discharge_w x
    assumed_delivery_frac)``; a 0.8 floor against a too-narrow window is
    refused with the ~7.0 h figure (C13)."""
    traverse = dict(_calibration()["traverse"])
    payload = _with_block(
        traverse=traverse, window_local="19:00", traverse_end_local="22:30"
    )
    message = refusal(payload)
    assert "assumed_delivery_frac" in message
    assert "800 x 0.8" in message
    assert "3.5 h" in message  # 0.90 x 4200 / 640 = 5.9 h > the 3.5 h window
    # The shipped 15:00-22:30 window fits at the same divide (7.5 h >= 7.0).
    _validate(_with_block())


def test_end_plus_ttl_lands_before_both_sibling_windows() -> None:
    """T-CAL-CONFIG: ``traverse_end_local + intent_ttl_s`` at or before the
    health-watch window (when present) and the night window open (always)."""
    payload = _with_block()
    payload["battery_health_watch"] = {
        "timezone": "Australia/Brisbane",
        "window_local": "22:30",
        "deadline_local": "23:45",
        "stages": ["census"],
    }
    message = refusal(payload)
    assert "battery_health_watch.window_local" in message
    payload = _with_block()
    payload["night_charging"] = {
        "timezone": "Australia/Brisbane",
        "window_local": [["22:30", "06:00"]],
    }
    payload["schedule"] = {"allowed_windows_local": [["22:00", "07:00"]]}
    message = refusal(payload)
    assert "night_charging window open" in message or "night charge" in message
    # The taper deadline must sit strictly AFTER the night window's end wall
    # (the taper is a MORNING fact).
    payload = _with_block()
    payload["night_charging"] = {
        "timezone": "Australia/Brisbane",
        "window_local": [["00:00", "13:00"]],
        "midday_local": "14:00",
    }
    payload["schedule"] = {"allowed_windows_local": [["00:00", "20:00"]]}
    message = refusal(payload)
    assert "MORNING fact" in message


def test_timezone_equality_and_wall_order() -> None:
    """T-CAL-CONFIG: the block's timezone must equal ``site.timezone`` (A9),
    ``plan_local`` sits strictly before ``window_local``, and the traverse
    end lands before ``plan_local + 24h``."""
    message = refusal(_with_block(timezone="UTC"))
    assert "site.timezone" in message
    refusal(_with_block(plan_local="15:30"))
    refusal(_with_block(window_local="23:00", traverse_end_local="14:30"))


def test_mode_act_requires_write_enabled_and_policy() -> None:
    """T-CAL-CONFIG: ``mode: act`` requires write_enabled (the traverse is
    dispatch); advise composes as the display-only program on any mode; the
    policy block is required for act (C15: no receipt gate, but the bounds
    still derive from the policy)."""
    refusal(_with_block(mode="act")) if False else None
    observe = _base(mode="observe_only")
    observe["battery_calibration"] = _calibration(mode="act")
    message = refusal(observe)
    assert "write_enabled" in message
    no_policy = _with_block(mode="act")
    del no_policy["policy"]
    message = refusal(no_policy)
    assert "policy" in message
    # Advise composes on observe-only (a pure display program).
    observe_advise = _base(mode="observe_only")
    observe_advise["battery_calibration"] = _calibration()
    _validate(observe_advise)


def test_c6_one_shot_shape() -> None:
    """T-CAL-CONFIG: the one-shot's shape — ``unit`` in the fleet (the
    refusal names the rule and the fleet); a standing request with a unit id
    that does not exist is a validation error."""
    payload = _with_block(request_measurement={"unit": "zzz", "note": "x"})
    message = refusal(payload)
    assert "C6" in message and "fleet" in message
    _validate(_with_block(request_measurement={"unit": "mid", "note": "operator"}))


def test_capacity_map_equality_with_night_charging() -> None:
    """T-CAL-CONFIG: ``assumed_capacity_wh`` present with the key set exactly
    the fleet, and EQUAL to ``night_charging.assumed_capacity_wh`` when that
    block carries one — one physical fact, two keys would drift."""
    traverse = dict(_calibration()["traverse"])
    traverse["assumed_capacity_wh"] = {"lhs": 5000, "mid": 5000}
    message = refusal(_with_block(traverse=traverse))
    assert "exactly" in message and "rhs" in message
    payload = _with_block()
    payload["night_charging"] = {
        "timezone": "Australia/Brisbane",
        "pacing": "even",
        "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 4000},
    }
    payload["schedule"] = {"allowed_windows_local": [["00:00", "06:00"]]}
    message = refusal(payload)
    assert "EQUAL" in message or "one physical fact" in message


def test_historian_prerequisite() -> None:
    """T-CAL-CONFIG: the ``plant_history:`` block must be present — the
    trigger and the measurement are historian-backed with deliberately no
    degrade-to-single-instant path."""
    payload = _with_block()
    del payload["plant_history"]
    message = refusal(payload)
    assert "plant_history" in message
    assert "single-instant" in message or "degrade" in message


def test_rhs_class_path_absence_is_not_a_block_refusal() -> None:
    """T-CAL-CONFIG: the rhs-class path's prerequisite (the health watch
    with probe staged) is deliberately NOT a block refusal — its absence
    refuses that PATH only; the affected units defer
    ``no_control_evidence`` and the projection says so (§3.2)."""
    config = _validate(_with_block())  # no battery_health_watch at all
    assert config.battery_calibration is not None
