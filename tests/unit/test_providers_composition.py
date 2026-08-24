"""The forecast-provider composition contract (advisory-only wiring).

Block-presence doctrine at the composition root: an ABSENT block and a
PRESENT-but-disabled block compose NOTHING -- ``runtime.forecast_providers``
stays ``None`` and nothing else in the composed runtime changes.  A PRESENT,
ENABLED block composes the registry of adapter instances -- and that is all:
constructing an adapter opens no socket, no task starts, no facade surface or
snapshot key appears, and the fleet loop is untouched.  The Solcast key
REFERENCE resolves at composition from the environment; an unset reference
omits the provider with a visible note instead of failing the boot (an
advisory provider's absence degrades its own data only).
"""

from __future__ import annotations

from typing import Any

import pytest

from energypod.runtime.composition import build_runtime
from energypod.runtime.config import ControllerConfig

try:
    from energypod.adapters.providers.load_baseline import HistorianLoadForecast
    from energypod.adapters.providers.open_meteo import OpenMeteoPvForecast, OpenMeteoWeather
    from energypod.adapters.providers.registry import ForecastProviderRegistry
    from energypod.adapters.providers.tariff_static import StaticTariffProvider
except ImportError as exc:  # pragma: no cover - initial red phase only
    ForecastProviderRegistry: Any = None
    OpenMeteoPvForecast: Any = None
    OpenMeteoWeather: Any = None
    HistorianLoadForecast: Any = None
    StaticTariffProvider: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_provider_composition_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The provider composition contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _config_payload() -> dict[str, Any]:
    return {
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
            "database_path": "var/providers-composition-test.sqlite3",
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
        "plant_history": {},
    }


def _build(payload: dict[str, Any]) -> Any:
    return build_runtime(ControllerConfig.model_validate(payload), simulate=True)


class TestBedtimeDoctrine:
    def test_an_absent_block_composes_nothing(self) -> None:
        runtime = _build(_config_payload())
        assert runtime.forecast_providers is None
        assert runtime.historian is not None  # siblings unaffected

    def test_a_disabled_block_composes_nothing(self) -> None:
        payload = _config_payload()
        payload["forecast_providers"] = {
            "enabled": False,
            "open_meteo": {
                "latitude": -27.4698,
                "longitude": 153.0251,
                "pv": {"tilt_deg": 25.0, "azimuth_deg": 10.0, "capacity_kw": 5.0},
            },
        }
        runtime = _build(payload)
        assert runtime.forecast_providers is None

    def test_an_enabled_block_composes_the_declared_families(self) -> None:
        payload = _config_payload()
        payload["forecast_providers"] = {
            "enabled": True,
            "open_meteo": {
                "latitude": -27.4698,
                "longitude": 153.0251,
                "pv": {"tilt_deg": 25.0, "azimuth_deg": 10.0, "capacity_kw": 5.0},
            },
            "load_baseline": {},
            "tariff": {
                "currency": "AUD",
                "default_import_cents_per_kwh": 28.0,
                "default_export_cents_per_kwh": 9.0,
            },
        }
        runtime = _build(payload)
        registry = runtime.forecast_providers
        assert isinstance(registry, ForecastProviderRegistry)
        assert isinstance(registry.weather, OpenMeteoWeather)
        assert isinstance(registry.pv, OpenMeteoPvForecast)
        assert isinstance(registry.load, HistorianLoadForecast)
        assert isinstance(registry.tariff, StaticTariffProvider)
        assert registry.notes == ()

    def test_composing_the_registry_adds_no_fleet_or_surface_trace(self) -> None:
        """The advisory-only pin: an enabled block changes NOTHING else."""
        baseline_payload = _config_payload()
        enabled_payload = _config_payload()
        enabled_payload["forecast_providers"] = {
            "enabled": True,
            "open_meteo": {"latitude": -27.4698, "longitude": 153.0251},
        }
        baseline = _build(baseline_payload)
        enabled = _build(enabled_payload)
        assert enabled.forecast_providers is not None
        assert baseline.forecast_providers is None
        # The composition's own surface vocabulary is unchanged: same routes,
        # same facade class, same fleet handles.
        baseline_paths = {route.path for route in baseline.app.routes}
        enabled_paths = {route.path for route in enabled.app.routes}
        assert baseline_paths == enabled_paths
        assert type(enabled.facade) is type(baseline.facade)
        assert set(enabled.actors) == set(baseline.actors)


class TestSolcastKeyResolution:
    def test_an_unset_key_reference_omits_the_provider_with_a_note(self) -> None:
        payload = _config_payload()
        payload["forecast_providers"] = {
            "enabled": True,
            "solcast": {
                "api_key_env": "ENERGYPOD_TEST_SOLCAST_KEY_UNSET",
                "resource_id": "b6bf-9d1d-0680-4078",
            },
        }
        runtime = _build(payload)
        registry = runtime.forecast_providers
        assert registry is not None
        assert registry.pv is None
        assert registry.notes and "ENERGYPOD_TEST_SOLCAST_KEY_UNSET" in registry.notes[0]

    def test_a_resolved_key_reference_composes_the_solcast_adapter(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ENERGYPOD_TEST_SOLCAST_KEY", "key-material-for-tests")
        payload = _config_payload()
        payload["forecast_providers"] = {
            "enabled": True,
            # The hobbyist budget override beside the family's own key.
            "stale_after_s": 43200.0,
            "solcast": {
                "api_key_env": "ENERGYPOD_TEST_SOLCAST_KEY",
                "resource_id": "b6bf-9d1d-0680-4078",
                "refresh_interval_s": 43200.0,
            },
        }
        runtime = _build(payload)
        registry = runtime.forecast_providers
        assert registry is not None
        from energypod.adapters.providers.solcast import SolcastPvForecast

        assert isinstance(registry.pv, SolcastPvForecast)
        assert registry.notes == ()

    def test_no_wire_leg_is_taken_by_composition_alone(self, monkeypatch: Any) -> None:
        """Constructing the registry must not touch the network."""
        import energypod.adapters.providers.http as http_module

        leg_calls: list[Any] = []

        async def _exploding_get_json(*args: Any, **kwargs: Any) -> Any:
            leg_calls.append((args, kwargs))
            raise AssertionError("composition must never take a provider wire leg")

        original = http_module.HttpxForecastTransport.get_json
        monkeypatch.setattr(http_module.HttpxForecastTransport, "get_json", _exploding_get_json)
        try:
            payload = _config_payload()
            payload["forecast_providers"] = {
                "enabled": True,
                "open_meteo": {"latitude": -27.4698, "longitude": 153.0251},
            }
            runtime = _build(payload)
            assert runtime.forecast_providers is not None
        finally:
            monkeypatch.setattr(http_module.HttpxForecastTransport, "get_json", original)
        assert leg_calls == []


def test_the_registry_reports_wire_member_staleness_only(monkeypatch: Any) -> None:
    monkeypatch.setenv("ENERGYPOD_TEST_SOLCAST_KEY", "key-material-for-tests")
    payload = _config_payload()
    payload["forecast_providers"] = {
        "enabled": True,
        "open_meteo": {"latitude": -27.4698, "longitude": 153.0251},
    }
    runtime = _build(payload)
    registry = runtime.forecast_providers
    assert registry is not None
    reports = registry.staleness_reports()
    # Only the wire-backed weather member reports; the local members
    # (baseline, tariff) recompute per read and carry no cache.
    assert [report.source for report in reports] == ["open-meteo"]
    assert all(report.fetched_at is None for report in reports)


@pytest.mark.parametrize("extra_block", [None, {"enabled": False}])
def test_both_staged_postures_leave_the_historian_siblings_intact(extra_block: Any) -> None:
    payload = _config_payload()
    if extra_block is not None:
        payload["forecast_providers"] = extra_block
    runtime = _build(payload)
    assert runtime.historian is not None
    assert runtime.history_repository is not None
    assert runtime.forecast_providers is None
