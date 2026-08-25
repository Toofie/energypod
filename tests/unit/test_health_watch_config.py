"""T-BHW-CONFIG — the ``battery_health_watch:`` block's commissioning gates.

DESIGN_BATTERY_HEALTH_WATCH §9 (CONTRACT v1.1): every gate with its
named-message refusal — the strict-prefix rule, the quiet-hour disjointness
(night window + the schedule union), the TWO deadline-arithmetic refusals
(A1: program-fit and worst-act-before-night, each naming its arithmetic),
the one-zone truth (A9), ``min_soc_hours``/``evidence_window_h`` with
equality allowed (A9), the per-stage prerequisites (census needs
plant_history; probe needs write_enabled + the policy discharge bound;
recovery needs parking + write_enabled), the A6 receipt map, and the
block-presence doctrine (an absent block composes nothing, and there is no
runtime toggle anywhere).
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
        "revision": 8,
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
        # The census's historian prerequisite (§9).
        "plant_history": {
            "sample_interval_s": 30.0,
            "retention_full_resolution_days": 14,
            "retention_rollup_days": 0,
        },
    }
    if mode == "observe_only":
        config.pop("authentication")
    return config


def _watch(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": "23:00",
        "deadline_local": "23:45",
        "stages": ["census"],
    }
    block.update(overrides)
    return block


def _validate(payload: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(payload)


def _night_partition(payload: dict[str, Any]) -> None:
    """Grant the night PARTITION (the night block's own prerequisite)."""
    payload["schedule"] = {"allowed_windows_local": [["00:00", "20:00"]]}
    payload["night_charging"] = {"timezone": "Australia/Brisbane"}


# --- the block-presence doctrine -------------------------------------------------


def test_absent_block_composes_nothing_and_defaults_are_the_contract() -> None:
    """An ABSENT block is valid and carries no surface anywhere."""
    config = _validate(_base())
    assert config.battery_health_watch is None


def test_present_block_defaults_match_the_contract_section_9() -> None:
    """The §9 defaults: the stuck vector, the probe bounds, advise recovery."""
    config = _validate(_base() | {"battery_health_watch": _watch()})
    watch = config.battery_health_watch
    assert watch is not None
    assert watch.stages == ("census",)
    assert watch.stuck.evidence_window_h == 6
    assert watch.stuck.min_soc_hours == 6
    assert watch.stuck.soc_floor_pct == 95.0
    assert watch.stuck.still_w == 50
    assert watch.stuck.grid_import_w == 500
    assert watch.stuck.load_floor_w == 30
    assert watch.stuck.sibling_load_w == 100
    assert watch.stuck.phase_live_window_h == 168
    assert watch.stuck.flag_persistence_nights == 2
    assert watch.probe.probe_w == 300
    assert watch.probe.settle_s == 20
    assert watch.probe.sustain_s == 30
    assert watch.probe.pass_fraction == 0.5
    assert watch.probe.pass_sample_frac == 0.8
    assert watch.probe.return_band_w == 150
    assert watch.probe.baseline_return_s == 30
    assert watch.probe.quiet_load_w == 1000
    assert watch.probe.load_move_w == 300
    assert watch.probe.inter_unit_gap_s == 30
    assert watch.recovery.mode == "advise"
    assert watch.recovery.hold_s == 90
    assert watch.recovery.consecutive_fail_limit == 3
    assert watch.recovery.probe_fail_nights == 2


# --- the strict-prefix rule -------------------------------------------------------


@pytest.mark.parametrize(
    ("stages", "refused"),
    [
        ((["probe"]), "strict prefix"),
        ((["recovery"]), "strict prefix"),
        ((["census", "recovery"]), "strict prefix"),
        ((["probe", "census"]), "strict prefix"),
        ((["census", "probe", "recovery", "census"]), "strict prefix"),
        (([]), "at least one stage"),
        ((["census", "audit"]), "Input should be 'census', 'probe' or 'recovery'"),
    ],
)
def test_stages_must_be_a_strict_prefix(stages: list[str], refused: str) -> None:
    payload = _base()
    payload["parking"] = {}
    with pytest.raises(ValidationError, match=refused):
        _validate(payload | {"battery_health_watch": _watch(stages=stages)})


def test_the_full_prefix_with_advise_recovery_is_valid() -> None:
    """§16 revision three's shape validates (Stage R's code is a later wave;
    the block's recovery keys are RECOGNIZED from day one)."""
    payload = _base() | {"parking": {}}
    config = _validate(
        payload | {"battery_health_watch": _watch(stages=["census", "probe", "recovery"])}
    )
    assert config.battery_health_watch is not None
    assert config.battery_health_watch.stages == ("census", "probe", "recovery")


# --- the window's own shape -------------------------------------------------------


def test_window_must_precede_deadline() -> None:
    payload = _base()
    with pytest.raises(ValidationError, match="window_local must sit strictly before"):
        _validate(payload | {"battery_health_watch": _watch(deadline_local="22:45")})


def test_min_soc_hours_equality_is_intended_and_allowed() -> None:
    """A9: 6 = 6 validates; a horizon beyond the lookback is refused."""
    payload = _base()
    config = _validate(
        payload | {"battery_health_watch": _watch(stuck={"min_soc_hours": 6})}
    )
    assert config.battery_health_watch is not None
    with pytest.raises(ValidationError, match="min_soc_hours must not exceed"):
        _validate(payload | {"battery_health_watch": _watch(stuck={"min_soc_hours": 7})})


def test_phase_live_window_h_must_cover_the_evidence_window() -> None:
    """A16: a liveliness window shorter than the evidence window it
    annotates is nonsense — the refusal names the rule; equality and the 168
    default validate (6 = 6 covers it, 168 = one week comfortably)."""
    payload = _base()
    config = _validate(
        payload | {"battery_health_watch": _watch(stuck={"phase_live_window_h": 6})}
    )
    assert config.battery_health_watch is not None
    assert config.battery_health_watch.stuck.phase_live_window_h == 6
    with pytest.raises(ValidationError, match="phase_live_window_h must be at least"):
        _validate(payload | {"battery_health_watch": _watch(stuck={"phase_live_window_h": 5})})


def test_phase_live_window_h_must_stay_within_the_historian_retention() -> None:
    """A16: a liveliness window beyond the historian's full-resolution
    retention is unknowable — the annotator would read already-rolled-up
    history as a CT word that never moved, so the revision is refused naming
    the retention arithmetic."""
    payload = _base()
    payload["plant_history"]["retention_full_resolution_days"] = 3  # 72 h
    with pytest.raises(ValidationError, match="full-resolution retention"):
        _validate(
            payload | {"battery_health_watch": _watch(stuck={"phase_live_window_h": 168})}
        )
    # The commissioned default (168 h against 14 days) validates.
    payload["plant_history"]["retention_full_resolution_days"] = 14
    config = _validate(payload | {"battery_health_watch": _watch()})
    assert config.battery_health_watch is not None


def test_probe_fail_nights_is_at_least_two() -> None:
    """A16 route B: a single night NEVER suffices — the named refusal; the
    default 2 and any higher count validate."""
    payload = _base()
    with pytest.raises(ValidationError, match="probe_fail_nights must be at least 2"):
        _validate(
            payload | {"battery_health_watch": _watch(recovery={"probe_fail_nights": 1})}
        )
    config = _validate(
        payload | {"battery_health_watch": _watch(recovery={"probe_fail_nights": 3})}
    )
    assert config.battery_health_watch is not None
    assert config.battery_health_watch.recovery.probe_fail_nights == 3


def test_return_band_must_exceed_the_still_band() -> None:
    payload = _base()
    with pytest.raises(ValidationError, match="return_band_w must exceed"):
        _validate(payload | {"battery_health_watch": _watch(probe={"return_band_w": 50})})


def test_settle_plus_sustain_is_bounded() -> None:
    payload = _base()
    with pytest.raises(ValidationError, match="settle_s \\+ sustain_s must stay"):
        block = _watch(probe={"settle_s": 60, "sustain_s": 60})
        _validate(payload | {"battery_health_watch": block})


def test_timezone_divergence_from_the_site_is_refused() -> None:
    """A9: one civil-time truth per site; the refusal names both values."""
    payload = _base()
    with pytest.raises(ValidationError, match=r"must equal site.timezone"):
        _validate(payload | {"battery_health_watch": _watch(timezone="Australia/Sydney")})


# --- the quiet-hour disjointness ---------------------------------------------------


def test_the_window_must_not_overlap_the_night_charge_window() -> None:
    payload = _base()
    _night_partition(payload)
    payload["night_charging"]["window_local"] = [["23:15", "06:00"]]
    payload["schedule"] = {"allowed_windows_local": [["23:00", "20:00"]]}
    with pytest.raises(ValidationError, match="overlaps the night_charging window"):
        _validate(payload | {"battery_health_watch": _watch()})


def test_the_window_must_not_overlap_any_schedule_allowed_window() -> None:
    payload = _base()
    payload["schedule"] = {"allowed_windows_local": [["06:00", "20:00"], ["23:10", "23:40"]]}
    with pytest.raises(ValidationError, match="overlaps the schedule allowed window"):
        _validate(payload | {"battery_health_watch": _watch()})


def test_touching_windows_do_not_overlap() -> None:
    """Half-open intervals: a schedule window ending AT 23:00 (the program's
    start wall) is disjoint, not an overlap."""
    payload = _base()
    payload["schedule"] = {"allowed_windows_local": [["06:00", "23:00"]]}
    config = _validate(payload | {"battery_health_watch": _watch()})
    assert config.battery_health_watch is not None


# --- the TWO deadline-arithmetic refusals (A1) --------------------------------------


def test_program_fit_refusal_names_its_arithmetic() -> None:
    """A1 check (a): window start + the whole-program bound <= deadline.

    3 probes at ~2.5 min (worst case + gap) + census cannot fit before a
    23:10 deadline — the refusal names the bound and the overflow.
    """
    payload = _base() | {"parking": {}}
    payload["battery_health_watch"] = _watch(
        window_local="23:00", deadline_local="23:10", stages=["census", "probe", "recovery"]
    )
    with pytest.raises(ValidationError, match="whole-program bound does not fit"):
        _validate(payload)


def test_worst_act_before_night_refusal_names_its_arithmetic() -> None:
    """A1 check (b): deadline + the worst-case in-flight act <= night open.

    A deadline at 23:58 leaves 2 minutes before a 00:00 night window — less
    than the ~5-minute worst-case act, so the revision is refused.
    """
    payload = _base()
    _night_partition(payload)
    payload["battery_health_watch"] = _watch(
        window_local="23:00", deadline_local="23:58", stages=["census"]
    )
    with pytest.raises(ValidationError, match="worst-case in-flight act does not"):
        _validate(payload)


def test_the_contract_defaults_pass_both_arithmetic_checks() -> None:
    """23:00 + ~21 min program, 23:45 + ~5 min act: the §4's own numbers."""
    payload = _base() | {"parking": {}}
    _night_partition(payload)
    payload["battery_health_watch"] = _watch(stages=["census", "probe", "recovery"])
    config = _validate(payload)
    assert config.battery_health_watch is not None


# --- the per-stage prerequisites ----------------------------------------------------


def test_census_requires_the_plant_history_block() -> None:
    payload = _base()
    del payload["plant_history"]
    with pytest.raises(ValidationError, match="census requires the plant_history block"):
        _validate(payload | {"battery_health_watch": _watch()})


def test_census_alone_is_commissionable_on_an_observe_only_site() -> None:
    """§0: Stage C alone is safe on any site — write mode gates only P/R."""
    payload = _base(mode="observe_only")
    payload.pop("policy", None)
    config = _validate(payload | {"battery_health_watch": _watch()})
    assert config.battery_health_watch is not None


def test_probe_requires_write_enabled() -> None:
    payload = _base(mode="observe_only")
    with pytest.raises(ValidationError, match="including probe requires mode write_enabled"):
        _validate(payload | {"battery_health_watch": _watch(stages=["census", "probe"])})


def test_probe_w_must_stay_inside_the_policy_discharge_limit() -> None:
    payload = _base()
    payload["policy"]["max_unit_discharge_w"] = 250
    with pytest.raises(ValidationError, match="probe_w must not exceed the policy"):
        _validate(payload | {"battery_health_watch": _watch(stages=["census", "probe"],
                                                             probe={"probe_w": 300})})


def test_probe_w_bounds_are_the_contract_band() -> None:
    payload = _base()
    with pytest.raises(ValidationError):
        _validate(payload | {"battery_health_watch": _watch(stages=["census", "probe"],
                                                             probe={"probe_w": 99})})
    with pytest.raises(ValidationError):
        _validate(payload | {"battery_health_watch": _watch(stages=["census", "probe"],
                                                             probe={"probe_w": 501})})


def test_recovery_requires_the_parking_block() -> None:
    """A write-enabled site with probe staged but no parking block: the
    recovery stage is refused at validation, never silently incapable."""
    payload = _base()
    with pytest.raises(ValidationError, match="including recovery requires the parking"):
        _validate(
            payload | {"battery_health_watch": _watch(stages=["census", "probe", "recovery"])}
        )


# --- the A6 receipt map ------------------------------------------------------------


def test_auto_recovery_requires_probe_in_stages() -> None:
    payload = _base() | {"parking": {}}
    with pytest.raises(ValidationError, match="mode auto requires probe in stages"):
        _validate(
            payload
            | {"battery_health_watch": _watch(stages=["census"], recovery={"mode": "auto"})}
        )


def test_auto_recovery_requires_a_receipt_key_for_every_fleet_unit() -> None:
    """A6: the missing-key refusal names the units; one edit cannot skip the
    supervised-verification sequencing."""
    payload = _base() | {"parking": {}}
    payload["battery_health_watch"] = _watch(
        stages=["census", "probe", "recovery"],
        recovery={
            "mode": "auto",
            "auto_receipts": {
                "lhs": "excluded",
                # mid and rhs missing: the refusal names them.
            },
        },
    )
    with pytest.raises(ValidationError, match=r"missing: \['mid', 'rhs'\]"):
        _validate(payload)


def test_auto_receipt_values_must_be_evidence_paths_or_excluded() -> None:
    payload = _base() | {"parking": {}}
    with pytest.raises(ValidationError, match="docs/evidence/"):
        _validate(
            payload
            | {
                "battery_health_watch": _watch(
                    stages=["census", "probe", "recovery"],
                    recovery={
                        "mode": "auto",
                        "auto_receipts": {
                            unit: "var/evidence.md" for unit in ("lhs", "mid", "rhs")
                        },
                    },
                )
            }
        )


def test_auto_receipts_with_every_unit_validate() -> None:
    payload = _base() | {"parking": {}}
    payload["battery_health_watch"] = _watch(
        stages=["census", "probe", "recovery"],
        recovery={
            "mode": "auto",
            "auto_receipts": {
                "lhs": "docs/evidence/standby-cycle-2026-08-24.md",
                "mid": "excluded",
                "rhs": "excluded",
            },
        },
    )
    config = _validate(payload)
    assert config.battery_health_watch is not None
    assert config.battery_health_watch.recovery.mode == "auto"


def test_advise_recovery_needs_no_receipts() -> None:
    """Recovery NOT in stages, or in stages as advise: both fine (A6 binds
    only the auto posture)."""
    payload = _base()
    config = _validate(
        payload | {"battery_health_watch": _watch(recovery={"mode": "advise"})}
    )
    assert config.battery_health_watch is not None


# --- no runtime toggle anywhere -----------------------------------------------------


def test_there_is_no_enabled_key_and_no_runtime_toggle() -> None:
    """The block carries no ``enabled`` key at all: an unknown key is refused
    (the strict schema), and the source carries no toggle route for stages."""
    payload = _base()
    block = _watch() | {"enabled": True}
    with pytest.raises(ValidationError):
        _validate(payload | {"battery_health_watch": block})


def test_the_live_write_example_carries_the_commissioned_wave_two_block() -> None:
    """§16 revision three (RECOVERY-ADVISE): the example file commissions
    ``[census, probe, recovery]`` with ``recovery.mode: advise`` — the
    standing posture that writes nothing, ever.  ``auto`` stays two
    deliberate steps away (the §16 step-5 supervised live verification,
    then revision four with the A6 receipts map), so ``auto_receipts`` is
    deliberately ABSENT: its presence would let one edit skip the
    sequencing the config file itself enforces."""
    from pathlib import Path

    import yaml

    path = (
        Path(__file__).resolve().parents[2] / "config" / "config.live-write-example.yaml"
    )
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    watch = payload.get("battery_health_watch")
    assert isinstance(watch, dict)
    assert watch["stages"] == ["census", "probe", "recovery"]
    assert watch["recovery"]["mode"] == "advise"
    assert "auto_receipts" not in watch["recovery"]
    # A16's documented keys at their defaults: the annotator window and
    # route B's repetition bound.
    assert watch["stuck"]["phase_live_window_h"] == 168
    assert watch["recovery"]["probe_fail_nights"] == 2
    assert "RECOVERY-ADVISE" in path.read_text(encoding="utf-8")


def test_the_block_is_frozen() -> None:
    """The frozen model: no runtime mutation of the commissioned stages."""
    from pydantic import ValidationError as ModelValidationError

    config = _validate(deepcopy(_base()) | {"battery_health_watch": _watch()})
    assert config.battery_health_watch is not None
    with pytest.raises(ModelValidationError):
        config.battery_health_watch.stages = ("recovery",)  # type: ignore[misc]
