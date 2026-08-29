"""T-ELS-CONFIG — the ``evening_load_sharing:`` block's commissioning gates.

DESIGN_EVENING_LOAD_SHARING §11 (CONTRACT v1.1): every bound and gate with
its named-message refusal — the E6 netting-evidence gate (``mode: act``
without ``act_netting_evidence`` refused naming the rule, and the key's
shape as a ``docs/evidence/`` path), the window-end arithmetic against BOTH
the watch and night walls (refusals naming their numbers), the window order
(same civil day), the floor-ordering rule (a participation floor below the
policy floor refused naming both), the cap inside the static unit limit, the
capacity-map equality across all THREE consumers, the E3/E4 bounds with the
P2 ordering, the TTL-over-control-period rule, the historian prerequisite,
the E10 absence (NO ``grid_telemetry_max_age_s`` key exists anywhere), the
block-presence doctrine (an absent block composes nothing), and advise
carrying no submission path (no runtime toggle for ``mode`` exists).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from energypod.runtime.config import ControllerConfig

from .test_calibration_config import _base


def _evening(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "advise",
        "window_local": "16:00",
        "window_end_local": "22:30",
        "min_share_w": 500,
        "cap_w": 2500,
        "participation_floor_pct": 20.0,
        "soc_exponent": 2.0,
        "spill_tolerance_w": 150,
        "import_tolerance_w": 100,
        "assumed_discharge_over_frac": 1.16,
        "frozen_word_ticks": 8,
        "frozen_flow_delta_w": 200,
        "delivery_move_floor_w": 400,
        "exchange_move_floor_w": 150,
        "non_delivery_ticks": 3,
        "intent_ttl_s": 10.0,
        "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 4200},
    }
    block.update(overrides)
    return block


def _validate(payload: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(payload)


def _with_block(**overrides: Any) -> dict[str, Any]:
    payload = _base()
    payload["evening_load_sharing"] = _evening(**overrides)
    return payload


def refusal(payload: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as raised:
        _validate(payload)
    message = str(raised.value)
    assert "evening_load_sharing" in message
    return message


def _with_watch(payload: dict[str, Any], window: str = "23:00") -> dict[str, Any]:
    payload["battery_health_watch"] = {
        "timezone": "Australia/Brisbane",
        "window_local": window,
        "deadline_local": "23:45",
        "stages": ["census"],
    }
    return payload


def _with_night(
    payload: dict[str, Any], window: tuple[str, str] = ("00:00", "06:00")
) -> dict[str, Any]:
    payload["night_charging"] = {
        "timezone": "Australia/Brisbane",
        "window_local": [window],
        # The only posture that carries a capacity map at all (the night
        # block refuses a stray map under cap_first-full).
        "pacing": "even",
        "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 4200},
    }
    # The night block's own PARTITION grant: the schedule union must cover it.
    payload["schedule"] = {"allowed_windows_local": [["00:00", "06:00"]]}
    return payload


def test_the_defaults_validate_in_advise() -> None:
    config = _validate(_with_block())
    assert config.evening_load_sharing is not None
    assert config.evening_load_sharing.mode == "advise"
    assert config.evening_load_sharing.window_local == "16:00"
    assert config.evening_load_sharing.window_end_local == "22:30"
    assert config.evening_load_sharing.assumed_discharge_over_frac == 1.16


def test_an_absent_block_composes_nothing() -> None:
    config = _validate(_base())
    assert config.evening_load_sharing is None


def test_mode_act_requires_write_enabled() -> None:
    payload = _with_block(mode="act")
    payload["mode"] = "observe_only"
    message = refusal(payload)
    assert "mode act requires mode write_enabled" in message


def test_mode_act_requires_the_netting_evidence_row_e6() -> None:
    message = refusal(_with_block(mode="act"))
    assert "act_netting_evidence" in message
    assert "E6" in message or "netting" in message
    # With the key present and shaped, act validates.
    config = _validate(_with_block(mode="act", act_netting_evidence="docs/evidence/x.md"))
    assert config.evening_load_sharing is not None
    assert config.evening_load_sharing.mode == "act"


def test_the_evidence_key_must_be_a_docs_evidence_path() -> None:
    message = refusal(_with_block(act_netting_evidence="/etc/passwd"))
    assert "docs/evidence/" in message


def test_the_timezone_must_equal_the_sites() -> None:
    message = refusal(_with_block(timezone="Europe/Berlin"))
    assert "must equal site.timezone" in message


def test_the_window_must_be_one_same_day_span() -> None:
    message = refusal(_with_block(window_local="23:00", window_end_local="01:00"))
    assert "same-day civil span" in message


def test_the_end_wall_plus_ttl_must_land_before_the_watch_window() -> None:
    payload = _with_watch(_with_block(), window="22:30")
    message = refusal(payload)
    assert "battery_health_watch.window_local" in message
    assert "22:30 + 10.0 s" in message


def test_the_end_wall_plus_ttl_must_land_before_the_night_window() -> None:
    payload = _with_block(window_end_local="23:56", intent_ttl_s=300.0)
    payload = _with_night(payload)  # opens 00:00
    message = refusal(payload)
    assert "night_charging window open" in message
    assert "00:00" in message


def test_the_participation_floor_orders_against_the_policy_floor() -> None:
    message = refusal(_with_block(participation_floor_pct=5.0))
    assert "minimum_soc_pct" in message
    assert "5.0 < 10.0" in message


def test_the_cap_sits_inside_the_static_unit_limit() -> None:
    message = refusal(_with_block(cap_w=3000))
    assert "max_unit_discharge_w" in message


def test_the_min_share_band_is_nonempty_and_sensing_clear() -> None:
    message = refusal(_with_block(min_share_w=100))
    assert "at least 200" in message or "ge=200" in message or "200" in message
    message = refusal(_with_block(min_share_w=3000))
    assert "cap_w" in message


def test_the_soc_exponent_band_and_derate_band() -> None:
    refusal(_with_block(soc_exponent=0.5))
    refusal(_with_block(soc_exponent=5.0))
    refusal(_with_block(assumed_discharge_over_frac=0.9))
    refusal(_with_block(assumed_discharge_over_frac=1.6))


def test_the_e3_e4_bounds_and_the_p2_ordering() -> None:
    refusal(_with_block(frozen_word_ticks=1))
    refusal(_with_block(frozen_flow_delta_w=0))
    refusal(_with_block(delivery_move_floor_w=0))
    message = refusal(_with_block(exchange_move_floor_w=400))
    assert "delivery_move_floor_w" in message
    refusal(_with_block(non_delivery_ticks=0))
    refusal(_with_block(spill_tolerance_w=0))
    refusal(_with_block(import_tolerance_w=0))


def test_the_ttl_must_exceed_the_control_period() -> None:
    message = refusal(_with_block(intent_ttl_s=0.2))
    assert "timing.control_period_s" in message


def test_the_capacity_map_is_required_and_exactly_the_fleet() -> None:
    message = refusal(_with_block(assumed_capacity_wh=None))
    assert "assumed_capacity_wh is required" in message
    message = refusal(_with_block(assumed_capacity_wh={"lhs": 5000}))
    assert "exactly the fleet units" in message


def test_the_capacity_map_equals_the_night_map() -> None:
    payload = _with_block()
    payload["night_charging"] = {
        "timezone": "Australia/Brisbane",
        "window_local": [("00:00", "06:00")],
        "pacing": "even",  # the only posture that carries a capacity map
        "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 5000},
    }
    payload["schedule"] = {"allowed_windows_local": [["00:00", "06:00"]]}
    message = refusal(payload)
    assert "must EQUAL" in message
    assert "night_charging" in message


def test_the_capacity_map_equals_the_calibration_map() -> None:
    payload = _with_block()
    payload["battery_calibration"] = {
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
            "assumed_capacity_wh": {"lhs": 5000, "mid": 5000, "rhs": 5000},
        },
    }
    message = refusal(payload)
    assert "battery_calibration" in message
    assert "must EQUAL" in message


def test_the_historian_prerequisite() -> None:
    payload = _with_block()
    del payload["plant_history"]
    message = refusal(payload)
    assert "plant_history" in message


def test_there_is_no_grid_telemetry_max_age_s_key_anywhere_e10() -> None:
    """The named test: the key does not exist — on the block, on any block,
    or anywhere in the shipped configuration surface."""
    # The strict model refuses it outright (extra=forbid) — there is no key
    # to drift, and no shipped configuration carries one.
    with pytest.raises(ValidationError) as raised:
        _validate(_with_block(grid_telemetry_max_age_s=3.0))
    assert "Extra inputs are not permitted" in str(raised.value)
    import yaml

    for path in Path("config").glob("*.yaml"):
        assert "grid_telemetry_max_age_s" not in yaml.safe_load(path.read_text(encoding="utf-8")), (
            path
        )


def test_there_is_no_runtime_toggle_for_mode() -> None:
    """The authority ladder is climbed by config revision plus restart only:
    no REST route, no facade method, no bus verb flips the mode."""
    rest = Path("src/energypod/api/rest.py").read_text(encoding="utf-8")
    assert "evening-sharing" in rest  # the status route exists...
    assert "set_evening" not in rest  # ...and no toggle route does
    service = Path("src/energypod/application/service.py").read_text(encoding="utf-8")
    assert "set_evening" not in service


def test_the_live_write_example_carries_no_evening_block() -> None:
    """The evening-share program is DECOMMISSIONED (revision 11, the
    operator's 2026-08-27 directive): the live example carries NO
    `evening_load_sharing:` key at all — absent composes nothing — and the
    E6 evidence gate still refuses an act block that reappears without its
    recorded netting evidence."""
    import yaml

    payload = yaml.safe_load(
        Path("config/config.live-write-example.yaml").read_text(encoding="utf-8")
    )
    assert "evening_load_sharing" not in payload, (
        "the block was decommissioned by operator directive; its return is "
        "a deliberate config revision, not an oversight"
    )
    act_no_evidence = dict(payload)
    act_no_evidence["evening_load_sharing"] = {"mode": "act"}
    with pytest.raises(ValidationError):
        _validate(act_no_evidence)
