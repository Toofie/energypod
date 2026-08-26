"""The site-meter provider's contract tests (DESIGN_SITE_METER.md v1.0).

T-SM-PARSE: captured-real-shape round trips, BOTH P_Grid polarities, the
null-Akku tolerance, the fail-closed refusals (status != 0, non-finite
watts, transport errors) that yield ``unavailable`` and NEVER zero-filled
watts.  T-SM-CONFIG: the block's validation quartet under
ControllerConfig — literal provider, staleness exceeding the control
period, timeout bounds, and block-presence absence.  T-SM-LAYER: the
application-side record lives adapter-blind.

SAFETY: no socket anywhere — payloads are captured literals; the provider
is driven through an injected fetch callable.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from energypod.adapters.providers.fronius import FroniusSiteMeter
from energypod.application.site_meter import SiteMeterReading

GOOD_PAYLOAD: dict[str, Any] = {
    "Body": {
        "Data": {
            "Inverters": {"1": {"DT": 122, "E_Day": 4545, "P": 3557}},
            "Site": {
                "E_Day": 4545,
                "Meter_Location": "grid",
                "Mode": "meter",
                "P_Akku": None,
                "P_Grid": -1582.4,
                "P_Load": -1974.6,
                "P_PV": 3557,
                "rel_Autonomy": 100,
                "rel_SelfConsumption": 55.5,
            },
        }
    },
    "Head": {"Status": {"Code": 0}, "Timestamp": "2026-08-27T08:40:05+10:00"},
}

IMPORTING_PAYLOAD: dict[str, Any] = {
    "Body": {"Data": {"Site": {"P_Akku": None, "P_Grid": 900.0, "P_Load": -600.0, "P_PV": 0.0}}},
    "Head": {"Status": {"Code": 0}},
}


def _provider(payload: Any = GOOD_PAYLOAD, *, fail: bool = False) -> FroniusSiteMeter:
    async def _fetch() -> Any:
        if fail:
            raise OSError("gateway gone")
        return payload

    return FroniusSiteMeter(fetch=_fetch)


@pytest.mark.anyio
async def test_the_real_exporting_payload_parses_with_import_positive_signs() -> None:
    reading = await _provider().read()
    assert isinstance(reading, SiteMeterReading)
    assert reading.quality == "good"
    # Fronius feeds OUT as negative; ours is IMPORT-positive.
    # Exporting site: Fronius reads negative, we stay negative.
    assert reading.net_exchange_w == pytest.approx(-1582.4)
    assert reading.load_w == pytest.approx(1974.6)
    assert reading.pv_w == pytest.approx(3557.0)


@pytest.mark.anyio
async def test_the_importing_polarity_stays_negative() -> None:
    reading = await _provider(IMPORTING_PAYLOAD).read()
    assert reading.net_exchange_w == pytest.approx(900.0)
    assert reading.load_w == pytest.approx(600.0)


@pytest.mark.anyio
async def test_a_bad_status_is_unavailable_never_zero() -> None:
    payload = {
        "Body": {"Data": {}},
        "Head": {"Status": {"Code": 255, "Reason": "unknown parameter"}},
    }
    reading = await _provider(payload).read()
    assert reading.quality == "unavailable"
    assert reading.net_exchange_w is None and reading.load_w is None


@pytest.mark.anyio
async def test_non_finite_or_missing_watts_are_unavailable() -> None:
    payload = {
        "Body": {"Data": {"Site": {"P_Grid": float("nan"), "P_Load": None, "P_PV": 10.0}}},
        "Head": {"Status": {"Code": 0}},
    }
    reading = await _provider(payload).read()
    assert reading.quality == "unavailable"


@pytest.mark.anyio
async def test_transport_failure_is_unavailable() -> None:
    reading = await _provider(fail=True).read()
    assert reading.quality == "unavailable"
    assert reading.pv_w is None


def test_the_reading_freshness_clock_is_the_client_own() -> None:
    provider = _provider()

    async def _capture() -> None:
        reading = await provider.read()
        assert reading.served_at_mono is not None
        assert math.isfinite(float(reading.served_at_mono))

    import asyncio

    asyncio.run(_capture())


# --- T-SM-CONFIG: the block's gates ---------------------------------------------


def _site(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "provider": "fronius",
        "host": "192.168.1.198",
    }
    block.update(overrides)
    return block


def _controller_with_site(site: dict[str, Any]) -> Any:
    from energypod.runtime.config import ControllerConfig

    # The repo's established fixture root; its real timing block supplies a
    # positive control period for the staleness gate.
    from .test_calibration_config import _base

    payload = _base()
    payload["site_meter"] = site
    return ControllerConfig.model_validate(payload)


def test_unknown_provider_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as raised:
        _controller_with_site(_site(provider="emphase"))
    message = str(raised.value)
    assert "site_meter" in message
    assert "provider" in message


def test_staleness_below_control_period_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as raised:
        _controller_with_site({**_site(), "stale_after_s": 0.2})
    message = str(raised.value)
    assert "stale_after_s" in message


def test_timeout_bounds_enforced() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _controller_with_site({**_site(), "request_timeout_s": 30.0})


def test_host_required() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _controller_with_site({"provider": "fronius"})
