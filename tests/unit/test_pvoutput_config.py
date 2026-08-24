"""The pvoutput.org reporting block's configuration contract.

The block follows the block-presence doctrine (the night pattern): ABSENT
composes nothing, PRESENT composes the reporting surface with ``enabled``
gating whether any POST is made.  Every validator here binds to
block-PRESENCE -- a disabled block that could never be enabled safely is
refused at validation time, because the runtime toggle can raise
participation but never a slot layout or a freshness bound.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "unit"))

from test_config import _valid_config

from energypod.runtime.config import ControllerConfig


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "enabled": False,
        "api_key_env": "PVOUTPUT_API_KEY",
        "system_id_env": "PVOUTPUT_SYSTEM_ID",
        "interval_s": 300.0,
        "max_sample_age_s": 120.0,
        "request_timeout_s": 10.0,
        "retry_max": 1,
        "unit_slots": {"lhs": ["v7", "v8"], "rhs": ["v9", "v10"], "mid": ["v11", "v12"]},
        "native_battery_fields": True,
    }
    block.update(overrides)
    return block


def _config_with(block: dict[str, Any] | None) -> ControllerConfig:
    payload = _valid_config()
    payload["storage"] = {"database_path": "var/test.sqlite3", "busy_timeout_ms": 250}
    if block is not None:
        payload["pvoutput"] = block
    return ControllerConfig.model_validate(payload)


class TestBlockShape:
    def test_the_pinned_defaults_are_the_old_containers_layout(self) -> None:
        parsed = _config_with(_block()).pvoutput
        assert parsed is not None
        assert parsed.enabled is False
        assert parsed.api_key_env == "PVOUTPUT_API_KEY"
        assert parsed.system_id_env == "PVOUTPUT_SYSTEM_ID"
        assert parsed.interval_s == 300.0
        assert parsed.max_sample_age_s == 120.0
        assert parsed.retry_max == 1
        assert parsed.native_battery_fields is True
        assert parsed.unit_slots == {
            "lhs": ("v7", "v8"),
            "rhs": ("v9", "v10"),
            "mid": ("v11", "v12"),
        }

    def test_an_absent_block_composes_nothing(self) -> None:
        assert _config_with(None).pvoutput is None

    def test_the_live_write_examples_block_validates_as_documented(self) -> None:
        """The commissioned example (the live controller's own config) parses
        cleanly with ``enabled: false`` -- what the operator reads is what
        the controller composes (the surface, participation off)."""
        import yaml

        example = Path(__file__).resolve().parents[2] / "config" / "config.live-write-example.yaml"
        document = yaml.safe_load(example.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        assert document["pvoutput"]["enabled"] is False
        payload = _valid_config()
        payload["storage"] = {"database_path": "var/x.sqlite3", "busy_timeout_ms": 250}
        payload["pvoutput"] = document["pvoutput"]
        parsed = ControllerConfig.model_validate(payload).pvoutput
        assert parsed is not None and parsed.enabled is False
        assert parsed.unit_slots["lhs"] == ("v7", "v8")


class TestEnvReferenceShape:
    @pytest.mark.parametrize(
        "bad",
        ["", "  ", "PVOUTPUT API KEY", "has-dash", "1STARTS"],
    )
    def test_a_non_identifier_env_name_is_refused(self, bad: str) -> None:
        """Empty and padded names fall to the plainness rule; the rest to the
        POSIX identifier shape -- a reference can never smuggle whitespace or
        shell syntax into an ``os.environ`` lookup."""
        with pytest.raises(ValidationError, match="api_key_env"):
            _config_with(_block(api_key_env=bad))

    def test_surrounding_whitespace_on_a_reference_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _config_with(_block(system_id_env=" PVOUTPUT_SYSTEM_ID "))


class TestIntervalBounds:
    @pytest.mark.parametrize("bad", [299.0, 60.0, 3601.0])
    def test_the_interval_stays_on_the_posting_grid(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            _config_with(_block(interval_s=bad))

    @pytest.mark.parametrize("good", [300.0, 600.0, 3600.0])
    def test_the_documented_intervals_validate(self, good: float) -> None:
        assert _config_with(_block(interval_s=good)).pvoutput is not None


class TestUnitSlots:
    def test_the_slots_must_cover_exactly_the_configured_units(self) -> None:
        with pytest.raises(ValidationError, match="exactly the configured units"):
            _config_with(_block(unit_slots={"lhs": ["v7", "v8"], "mid": ["v11", "v12"]}))

    def test_a_stray_unit_name_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="names no battery"):
            _config_with(
                _block(
                    unit_slots={
                        "lhs": ["v7", "v8"],
                        "rhs": ["v9", "v10"],
                        "ghost": ["v11", "v12"],  # well-formed slots, no such battery
                    }
                )
            )

    def test_a_slot_collision_across_units_is_refused(self) -> None:
        """One v-slot means one battery's one field: a collision would make
        the dashboard slot mean two batteries at once."""
        with pytest.raises(ValidationError, match="collide"):
            _config_with(
                _block(
                    unit_slots={
                        "lhs": ["v7", "v8"],
                        "rhs": ["v7", "v10"],  # v7 twice
                        "mid": ["v11", "v12"],
                    }
                )
            )

    def test_a_pair_reusing_one_slot_for_both_fields_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="DISTINCT"):
            _config_with(
                _block(
                    unit_slots={"lhs": ["v7", "v7"], "rhs": ["v9", "v10"], "mid": ["v11", "v12"]}
                )
            )

    def test_only_the_v7_to_v12_vocabulary_is_accepted(self) -> None:
        with pytest.raises(ValidationError, match="v7.*v12"):
            _config_with(
                _block(
                    unit_slots={"lhs": ["v6", "v8"], "rhs": ["v9", "v10"], "mid": ["v11", "v12"]}
                )
            )

    def test_an_empty_slot_map_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least one unit"):
            _config_with(_block(unit_slots={}))


class TestCommissioningGates:
    def test_a_freshness_bound_below_one_fleet_cycle_is_refused(self) -> None:
        payload = _valid_config()
        payload["storage"] = {"database_path": "var/x.sqlite3", "busy_timeout_ms": 250}
        payload["pvoutput"] = _block(max_sample_age_s=0.30)  # control_period_s is 0.40
        with pytest.raises(ValidationError, match="control_period_s"):
            ControllerConfig.model_validate(payload)

    def test_the_storage_block_is_required_for_the_durable_toggle(self) -> None:
        payload = _valid_config()
        payload.pop("storage", None)  # the shared fixture carries one
        payload["pvoutput"] = _block()
        with pytest.raises(ValidationError, match="storage block"):
            ControllerConfig.model_validate(payload)

    def test_the_retry_budget_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            _config_with(_block(retry_max=11))
