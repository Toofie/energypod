"""The composed calibration surface (DESIGN_CALIBRATION_CYCLING §7/§8).

The composition-level pins: the ``battery_calibration:`` block composes the
adviser, the ``calibration_state`` snapshot projection, and the status route
(block-presence); an ABSENT block composes NOTHING — a byte-identical
snapshot and 409 ``calibration_not_commissioned`` on the route; the
traverse's submission twin mints ``cal-`` OPTIMIZER intents under the
composed ``energypod:calibration-adviser`` principal exactly like the
adviser twins (the night-charge class, no mode register anywhere on the
path); the supervision pass carries the tick beside the health watch's slot;
and the architecture pins — the historian's claims attribution is untouched
(the night-writer detector stays quiet through a traverse; the block adds no
write method anywhere on the path).

T-CAL-NIGHT-INTERPLAY's no-intent-alive-at-midnight leg lives here too: the
composed TTL plus the validated end wall mean the last traverse intent dies
long before the night window opens — proven at the composition that shipped.

SAFETY: the in-memory simulate composition only — no socket, no live system.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from energypod.runtime.composition import build_runtime
from energypod.runtime.config import ControllerConfig

from .test_composition import (
    OPERATOR,
    _authentication_payload,
    _policy_payload,
    _timing_payload,
    _unit_payload,
)


def _calibration_payload(**overrides: Any) -> dict[str, Any]:
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
            "assumed_capacity_wh": {"mid": 5000},
        },
    }
    block.update(overrides)
    return block


def _payload(calibration: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 15,
        "mode": "write_enabled",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [_unit_payload("mid", "BEP-MID", "192.168.1.11")],
        "timing": _timing_payload(),
        "policy": _policy_payload(),
        "authentication": _authentication_payload(),
        "plant_history": {
            "sample_interval_s": 30.0,
            "retention_full_resolution_days": 14,
            "retention_rollup_days": 0,
        },
    }
    if calibration is not None:
        payload["battery_calibration"] = calibration
    return payload


class ManualClock:
    """Deterministic time source (the wedge-scenario clock's shape)."""

    def __init__(self, *, wall: datetime | None = None) -> None:
        self.now = 5000.0
        self.wall = wall or datetime(2026, 8, 25, 13, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall


def _compose(
    calibration: dict[str, Any] | None, clock: Any, tmp_path: Any = None
) -> Any:
    payload = _payload(calibration)
    if tmp_path is not None:
        payload["storage"] = {
            "database_path": str(tmp_path / "calibration.sqlite3"),
            "busy_timeout_ms": 250,
        }
    return build_runtime(ControllerConfig.model_validate(payload), clock=clock, simulate=True)


async def test_an_absent_block_composes_nothing_and_the_route_refuses(tmp_path) -> None:
    runtime = _compose(None, ManualClock(), tmp_path)
    assert runtime.calibration is None
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "calibration_state" not in snapshot
    with pytest.raises(Exception) as caught:
        await runtime.facade.get_calibration_status(principal=OPERATOR)
    assert getattr(caught.value, "code", "") == "calibration_not_commissioned"
    with pytest.raises(Exception) as caught_ack:
        await runtime.facade.acknowledge_calibration_standdown(
            unit_id="mid", principal=OPERATOR
        )
    assert getattr(caught_ack.value, "code", "") == "calibration_not_commissioned"


async def test_a_present_block_composes_the_projection_and_the_route(tmp_path) -> None:
    runtime = _compose(_calibration_payload(), ManualClock(), tmp_path)
    assert runtime.calibration is not None
    paths = {
        route.path for route in runtime.app.routes if getattr(route, "path", "").startswith("/api")
    }
    assert "/api/v1/calibration/status" in paths
    status = await runtime.facade.get_calibration_status(principal=OPERATOR)
    assert status["mode"] == "advise"
    assert status["submits"] == "never"
    assert status["window"] == {"opens_local": "15:00", "ends_local": "22:30"}
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert snapshot["calibration_state"] == status


async def test_the_traverse_submission_twin_mints_cal_intents(tmp_path) -> None:
    """The internal drive: an OPTIMIZER intent with the ``cal-`` prefix,
    attributed to the composed calibration-adviser principal — the
    night-charge class exactly, judged by everything downstream."""
    from energypod.application.calibration import CALIBRATION_ADVISER_PRINCIPAL
    from energypod.domain.intents import Direction, IntentSource

    runtime = _compose(_calibration_payload(mode="act"), ManualClock(), tmp_path)
    principal = type("CalibrationPrincipal", (), {})()
    principal.subject = CALIBRATION_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch"})
    principal.interactive = False
    principal.site_id = "home"
    result = await runtime.facade.submit_calibration_intent(
        unit_ids=["mid"],
        direction=Direction.DISCHARGE,
        watts=800,
        ttl_s=10.0,
        principal=principal,
    )
    assert result["intent_id"].startswith("cal-")
    active = await runtime.intents.active(runtime.clock.monotonic())
    (intent,) = active
    assert intent.source is IntentSource.OPTIMIZER
    assert intent.direction is Direction.DISCHARGE
    assert intent.watts == 800
    assert intent.actor_identity == CALIBRATION_ADVISER_PRINCIPAL


async def test_no_calibration_intent_is_alive_at_midnight(tmp_path) -> None:
    """T-CAL-NIGHT-INTERPLAY: end+ttl < the night window open, proven — the
    last submitted intent dies by TTL long before 00:00 (the intent store
    holds nothing at the deadline plus one TTL)."""
    from energypod.application.calibration import CALIBRATION_ADVISER_PRINCIPAL
    from energypod.domain.intents import Direction

    runtime = _compose(_calibration_payload(mode="act"), ManualClock(), tmp_path)
    principal = type("CalibrationPrincipal", (), {})()
    principal.subject = CALIBRATION_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch"})
    principal.interactive = False
    principal.site_id = "home"
    # The LAST intent the adviser could ever submit, at the no-new-renewal
    # boundary itself: 22:30 local (12:30 UTC) + the composed 10 s TTL.
    clock = runtime.clock
    clock.wall = datetime(2026, 8, 25, 12, 30, 0, tzinfo=UTC)
    await runtime.facade.submit_calibration_intent(
        unit_ids=["mid"],
        direction=Direction.DISCHARGE,
        watts=200,
        ttl_s=10.0,
        principal=principal,
    )
    midnight = datetime(2026, 8, 25, 14, 0, 0, tzinfo=UTC)  # 00:00 local
    clock.wall = midnight
    clock.now += (midnight - datetime(2026, 8, 25, 12, 30, 0, tzinfo=UTC)).total_seconds()
    active = await runtime.intents.active(clock.monotonic())
    assert active == ()
    # The composed figures make the property structural: end 22:30 + 10 s
    # lands at 22:30:10, ninety minutes before the window.
    assert clock.wall - datetime(2026, 8, 25, 12, 30, 10, tzinfo=UTC) > timedelta(minutes=89)


async def test_the_historian_claims_attribution_is_untouched_and_no_write_method(
    tmp_path,
) -> None:
    """T-CAL-ARCHITECTURE: the night-writer detector stays quiet through a
    traverse — the composition does NOT wire the calibration controller into
    the historian's adviser-claims attribution (the cal adviser's intents
    are ordinary claims the standing monitors already judge), and the block
    adds no write method anywhere on the path (the 0x8000 composer set is
    exactly the parking block's own)."""
    source = Path("src/energypod/runtime/composition.py").read_text(encoding="utf-8")
    composition_cal = [
        line
        for line in source.splitlines()
        if "calibration" in line and "claims" in line
    ]
    assert composition_cal == []  # never a claims-attribution writer
    # The debug-mode write composer is exactly the parking block's twin.
    write_debug_writers = [
        line for line in source.splitlines() if "write_debug_mode" in line
    ]
    assert write_debug_writers, "the parking block's own writer remains"
    # Every write_debug_mode reach in the composition names the parking write
    # in its own docstring — the calibration block adds none of them.
    assert all("calibration" not in line for line in write_debug_writers)


async def test_the_supervision_pass_carries_the_tick_beside_the_watch(tmp_path) -> None:
    """T-CAL-ARCHITECTURE: one tick per fleet cycle inside the existing
    bounded supervision pass — no new task class; the reconstruction runs at
    supervision start (the parking pattern)."""
    source = Path("src/energypod/runtime/composition.py").read_text(encoding="utf-8")
    assert "self._calibration.tick()" in source
    assert "self._calibration.reconstruct_at_boot()" in source
