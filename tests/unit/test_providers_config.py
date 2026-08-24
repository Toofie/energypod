"""The forecast-providers configuration block contract.

Block-presence doctrine, symmetric with every advisory feature before it: a
PRESENT ``forecast_providers`` block declares the advisory provider stack; an
ABSENT block composes nothing.  ``enabled`` defaults to ``false`` and gates
composition only (staged posture — the network-egress families are off until
the operator turns them on; runtime state never persists).  The secrets rule
is structural: the Solcast key is a REFERENCE (an environment variable name),
never key material in a config file.  The load baseline requires the
``plant_history`` block it reads, and the two PV sources are mutually
exclusive — a site has exactly one PV truth.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

try:
    from energypod.runtime.config import ControllerConfig
except ImportError as exc:  # pragma: no cover - initial red phase only
    ControllerConfig: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_provider_config_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The provider configuration contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _valid_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema_version": 1,
        "revision": 7,
        "mode": "observe_only",
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
            for index, unit in enumerate(("mid", "rhs", "lhs"))
        ],
        "storage": {
            "database_path": "/data/energypod.sqlite3",
            "busy_timeout_ms": 250,
        },
        "timing": {
            "device_command_expiry_s": 9.0,
            "device_command_expiry_evidence": "commissioning://observe-only-2026-08",
            "control_period_s": 1.0,
            "essential_read_timeout_s": 0.30,
            "kernel_timeout_s": 0.10,
            "audit_timeout_s": 0.10,
            "write_timeout_s": 0.20,
            "acknowledgement_timeout_s": 0.20,
            "maximum_jitter_s": 0.10,
            "renewal_margin_s": 0.50,
        },
    }
    return config


def _validate(payload: dict[str, Any]) -> Any:
    assert ControllerConfig is not None
    return ControllerConfig.model_validate(payload)


def _assert_invalid(payload: dict[str, Any], *, location_contains: str) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _validate(payload)
    locations = {".".join(str(part) for part in error["loc"]) for error in caught.value.errors()}
    assert any(location_contains in location for location in locations), (
        f"expected an error mentioning {location_contains!r}; got {sorted(locations)}"
    )
    return caught.value


OPEN_METEO_BLOCK = {
    "latitude": -27.4698,
    "longitude": 153.0251,
    "forecast_days": 2,
    "pv": {"tilt_deg": 25.0, "azimuth_deg": 10.0, "capacity_kw": 5.0, "derate": 0.9},
}
SOLCAST_BLOCK = {
    "api_key_env": "SOLCAST_API_KEY",
    "resource_id": "b6bf-9d1d-0680-4078",
}
TARIFF_BLOCK = {
    "currency": "AUD",
    "default_import_cents_per_kwh": 28.0,
    "default_export_cents_per_kwh": 9.0,
    "windows": [
        {
            "window_local": ("00:00", "06:00"),
            "import_cents_per_kwh": 12.0,
            "export_cents_per_kwh": 5.0,
        }
    ],
}


class TestBlockValidation:
    def test_defaults_validate_disabled(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {"enabled": False}
        parsed = _validate(payload)
        assert parsed.forecast_providers is not None
        assert parsed.forecast_providers.enabled is False
        assert parsed.forecast_providers.refresh_interval_s == 900.0
        assert parsed.forecast_providers.stale_after_s == 3600.0
        assert parsed.forecast_providers.request_timeout_s == 10.0

    def test_a_full_declaration_validates(self) -> None:
        payload = _valid_config()
        payload["plant_history"] = {}
        payload["forecast_providers"] = {
            "enabled": True,
            "open_meteo": OPEN_METEO_BLOCK,
            "load_baseline": {},
            "tariff": TARIFF_BLOCK,
        }
        parsed = _validate(payload)
        block = parsed.forecast_providers
        assert block is not None and block.enabled is True
        assert block.open_meteo is not None and block.open_meteo.pv is not None
        assert block.load_baseline is not None and block.load_baseline.slot_s == 1800.0
        assert block.tariff is not None and block.tariff.currency == "AUD"

    def test_staleness_may_not_hit_before_the_refresh_gate(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {
            "refresh_interval_s": 600.0,
            "stale_after_s": 599.0,
        }
        # The block-level relation lands on the block itself.
        _assert_invalid(payload, location_contains="forecast_providers")

    def test_the_two_pv_sources_are_mutually_exclusive(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {
            "open_meteo": OPEN_METEO_BLOCK,
            "solcast": SOLCAST_BLOCK,
        }
        _assert_invalid(payload, location_contains="forecast_providers")

    def test_an_enabled_block_needs_at_least_one_family(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {"enabled": True}
        _assert_invalid(payload, location_contains="forecast_providers")

    def test_the_solcast_key_is_a_reference_never_material(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {"solcast": {**SOLCAST_BLOCK, "api_key_env": "  "}}
        _assert_invalid(payload, location_contains="api_key_env")

    def test_the_solcast_site_facts_live_at_solcast_not_here(self) -> None:
        """The live-verified tier is site-addressed: the registered rooftop
        record holds latitude/longitude/capacity, so those keys are REFUSED
        here (a client-side copy could only drift from what Solcast models)."""
        payload = _valid_config()
        payload["forecast_providers"] = {
            "solcast": {**SOLCAST_BLOCK, "latitude": -27.59906, "capacity_kw": 5.0}
        }
        _assert_invalid(payload, location_contains="solcast")

    @pytest.mark.parametrize(
        ("resource_id", "why"),
        [
            ("", "empty"),
            ("  b6bf-9d1d-0680-4078  ", "surrounding whitespace"),
            ("b6bf/9d1d", "a path separator would escape the URL segment"),
            ("b6bf?id=1", "a query separator would escape the URL segment"),
        ],
    )
    def test_the_resource_id_is_one_url_path_segment(
        self, resource_id: str, why: str
    ) -> None:
        del why  # the parameter documents the case, not the assertion
        payload = _valid_config()
        payload["forecast_providers"] = {"solcast": {**SOLCAST_BLOCK, "resource_id": resource_id}}
        _assert_invalid(payload, location_contains="resource_id")

    def test_a_family_refresh_override_must_stay_positive(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {
            "solcast": {**SOLCAST_BLOCK, "refresh_interval_s": 0.0}
        }
        _assert_invalid(payload, location_contains="refresh_interval_s")

    def test_staleness_may_not_hit_before_a_family_refresh_gate(self) -> None:
        """The floor binds to every DECLARED family's effective refresh: a
        12-hour Solcast budget against a 1-hour stale threshold would flag
        the gate's own cadence stale forever."""
        payload = _valid_config()
        payload["forecast_providers"] = {
            "refresh_interval_s": 900.0,
            "stale_after_s": 3600.0,
            "solcast": {**SOLCAST_BLOCK, "refresh_interval_s": 43200.0},
        }
        _assert_invalid(payload, location_contains="forecast_providers")

    def test_the_stale_floor_rides_the_slowest_declared_family(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {
            "enabled": True,
            "refresh_interval_s": 900.0,
            "stale_after_s": 43200.0,
            "open_meteo": {"latitude": -27.59906, "longitude": 153.068355},
            "solcast": {**SOLCAST_BLOCK, "refresh_interval_s": 43200.0},
        }
        parsed = _validate(payload)
        block = parsed.forecast_providers
        assert block is not None and block.enabled is True
        assert block.solcast is not None
        assert block.solcast.refresh_interval_s == 43200.0
        assert block.open_meteo is not None and block.open_meteo.refresh_interval_s is None

    def test_unknown_keys_are_refused(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {"open_meteo": {**OPEN_METEO_BLOCK, "apikey": "x"}}
        _assert_invalid(payload, location_contains="open_meteo")

    @pytest.mark.parametrize(
        ("block", "field", "value"),
        [
            ({"open_meteo": {**OPEN_METEO_BLOCK, "latitude": 91.0}}, "latitude", 91.0),
            (
                {
                    "open_meteo": {
                        **OPEN_METEO_BLOCK,
                        "pv": {"tilt_deg": 95.0, "azimuth_deg": 10.0, "capacity_kw": 5.0},
                    }
                },
                "tilt_deg",
                95.0,
            ),
            (
                {"open_meteo": {**OPEN_METEO_BLOCK, "refresh_interval_s": -1.0}},
                "refresh_interval_s",
                -1.0,
            ),
            ({"solcast": {**SOLCAST_BLOCK, "hours": 0}}, "hours", 0),
            ({"solcast": {**SOLCAST_BLOCK, "hours": 337}}, "hours", 337),
            ({"solcast": {**SOLCAST_BLOCK, "period": "PT7M"}}, "period", "PT7M"),
            ({"tariff": {**TARIFF_BLOCK, "currency": "AUDD"}}, "currency", "AUDD"),
            ({"load_baseline": {"slot_s": 0.0}}, "slot_s", 0.0),
            ({"request_timeout_s": 0.0}, "request_timeout_s", 0.0),
        ],
    )
    def test_the_bounds_matrix(self, block: dict[str, Any], field: str, value: Any) -> None:
        payload = _valid_config()
        payload["plant_history"] = {}
        payload["forecast_providers"] = block
        _assert_invalid(payload, location_contains=field)

    def test_the_load_baseline_requires_the_historian_it_reads(self) -> None:
        payload = _valid_config()
        payload["forecast_providers"] = {"load_baseline": {}}
        _assert_invalid(payload, location_contains="forecast_providers")

    def test_the_load_baseline_composes_with_plant_history_present(self) -> None:
        payload = _valid_config()
        payload["plant_history"] = {}
        payload["forecast_providers"] = {"load_baseline": {}}
        parsed = _validate(payload)
        assert parsed.forecast_providers is not None


class TestExampleConfig:
    def test_the_live_write_example_commissions_the_discovered_layer(self) -> None:
        """The block is PRESENT and ENABLED (2026-08-24, the live-discovered
        hobbyist contract): solcast as the one PV truth addressed by
        resource_id on the 12-hour hobbyist budget, Open-Meteo as weather
        context only — and the example parses cleanly, so what the operator
        reads is what the controller composes."""
        import yaml

        path = Path(__file__).resolve().parents[2] / "config" / "config.live-write-example.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), "the example must stay one YAML document"
        assert "forecast_providers" in document, "the providers block is commissioned"
        block_text = path.read_text(encoding="utf-8").split("forecast_providers:", 1)[1]
        assert re.search(r"^\s+enabled:\s*true(?:\s+#.*)?$", block_text, flags=re.MULTILINE), (
            "the example block must be explicitly on (enabled: true)"
        )

        payload = _valid_config()
        payload["forecast_providers"] = document["forecast_providers"]
        parsed = _validate(payload)
        block = parsed.forecast_providers
        assert block is not None and block.enabled is True
        assert block.solcast is not None
        assert block.solcast.api_key_env == "SOLCAST_API_KEY"
        assert block.solcast.resource_id == "b6bf-9d1d-0680-4078"
        assert block.solcast.hours == 48
        assert block.solcast.period == "PT30M"
        # 43200 s = 2 scheduled legs/day inside the 10/day hobbyist budget
        # (each process start also fetches once) — the budget math comment's
        # number is the composed number.
        assert block.solcast.refresh_interval_s == 43200.0
        assert block.stale_after_s == 43200.0
        assert block.open_meteo is not None
        assert block.open_meteo.pv is None, "solcast is the one PV truth: no open_meteo.pv"
        assert block.open_meteo.latitude == -27.59906
        assert block.open_meteo.longitude == 153.068355
        assert block.refresh_interval_s == 900.0
