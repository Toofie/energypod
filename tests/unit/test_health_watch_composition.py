"""The composed health-watch surface (DESIGN_BATTERY_HEALTH_WATCH §4/§10/§13).

The composition-level pins: the ``battery_health_watch:`` block composes the
controller, the ``health_watch_state`` snapshot projection, and the status
route (block-presence); an ABSENT block composes NOTHING — a byte-identical
snapshot and 409 ``health_watch_not_commissioned`` on the route; the probe's
submission twin mints ``health-`` OPTIMIZER intents under the composed
``energypod:health-adviser`` principal exactly like the adviser twins; and
the architecture pins (no new transport write method; no control path
subscribes to the health events; the recovery stage is recognized but not
composed).

SAFETY: the in-memory simulate composition only — no socket, no live system.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
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


def _watch_payload(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": "23:00",
        "deadline_local": "23:45",
        "stages": ["census", "probe"],
    }
    block.update(overrides)
    return block


def _payload(watch: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 14,
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
    if watch is not None:
        payload["battery_health_watch"] = watch
        if "recovery" in watch.get("stages", ()):
            # §9: the recovery stage composes the parking primitive.
            payload["parking"] = {"max_lease_s": 14400, "default_lease_s": 14400}
    return payload


class ManualClock:
    """Deterministic time source (the wedge-scenario clock's shape)."""

    def __init__(self, *, wall: datetime | None = None) -> None:
        self.now = 5000.0
        self.wall = wall or datetime(2026, 8, 25, 13, 0, 0, tzinfo=UTC)
        self._waiters: list[tuple[float, Any]] = []

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall = self.wall + timedelta(seconds=seconds)
        for deadline, future in self._waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)
        self._waiters = [pair for pair in self._waiters if not pair[1].done()]

    async def sleep(self, seconds: float) -> None:
        import asyncio

        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        with contextlib.suppress(asyncio.CancelledError):
            await future


def _compose(watch: dict[str, Any] | None, clock: Any, tmp_path: Any = None) -> Any:
    payload = _payload(watch)
    if tmp_path is not None:
        payload["storage"] = {
            "database_path": str(tmp_path / "health-watch.sqlite3"),
            "busy_timeout_ms": 250,
        }
    return build_runtime(ControllerConfig.model_validate(payload), clock=clock, simulate=True)


async def test_an_absent_block_composes_nothing_and_the_route_refuses(tmp_path) -> None:
    runtime = _compose(None, ManualClock(), tmp_path)
    assert runtime.health_watch is None
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "health_watch_state" not in snapshot
    with pytest.raises(Exception) as caught:
        await runtime.facade.get_health_watch_status(principal=OPERATOR)
    assert getattr(caught.value, "code", "") == "health_watch_not_commissioned"


async def test_a_present_block_composes_the_projection_and_the_route(tmp_path) -> None:
    runtime = _compose(_watch_payload(), ManualClock(), tmp_path)
    assert runtime.health_watch is not None
    paths = {
        route.path for route in runtime.app.routes if getattr(route, "path", "").startswith("/api")
    }
    assert "/api/v1/health-watch/status" in paths
    status = await runtime.facade.get_health_watch_status(principal=OPERATOR)
    assert status["stages"] == ["census", "probe"]
    assert status["window"] == {"opens_local": "23:00", "deadline_local": "23:45"}
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert snapshot["health_watch_state"] == status


async def test_the_projection_names_the_uncommissioned_recovery_honestly(tmp_path) -> None:
    runtime = _compose(_watch_payload(), ManualClock(), tmp_path)
    status = await runtime.facade.get_health_watch_status(principal=OPERATOR)
    (unit,) = status["units"]
    assert unit["recovery"] == {"mode": "uncommissioned"}


async def test_the_probe_submission_twin_mints_health_intents(tmp_path) -> None:
    """The internal drive: an OPTIMIZER intent with the ``health-`` prefix,
    attributed to the composed health-adviser principal."""
    from energypod.application.health_watch import HEALTH_ADVISER_PRINCIPAL
    from energypod.domain.intents import Direction, IntentSource

    runtime = _compose(_watch_payload(), ManualClock(), tmp_path)
    principal = type("HealthPrincipal", (), {})()
    principal.subject = HEALTH_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch"})
    principal.interactive = False
    principal.site_id = "home"
    result = await runtime.facade.submit_health_intent(
        unit_ids=["mid"],
        direction=Direction.DISCHARGE,
        watts=300,
        ttl_s=4.0,
        principal=principal,
    )
    assert result["intent_id"].startswith("health-")
    active = await runtime.intents.active(runtime.clock.monotonic())
    (intent,) = active
    assert intent.source is IntentSource.OPTIMIZER
    assert intent.direction is Direction.DISCHARGE
    assert intent.watts == 300
    assert intent.actor_identity == HEALTH_ADVISER_PRINCIPAL


def test_no_new_transport_write_method_was_added() -> None:
    """T-BHW-ARCHITECTURE: the watch adds no transport path — the actor
    surface still exposes exactly the parking-era named writes."""
    from pathlib import Path

    source = (
        Path(build_runtime.__code__.co_filename).parent.parent
        / "simulator"
        / "transport.py"
    ).read_text(encoding="utf-8")
    assert "write_debug_mode" in source  # the parking-era primitive stands
    watch = Path(
        build_runtime.__code__.co_filename
    ).parent.parent.joinpath("application", "health_watch.py").read_text(encoding="utf-8")
    for forbidden in ("write_debug_mode(", "request_debug_mode_change("):
        assert forbidden not in watch


def test_no_control_path_subscribes_to_the_health_events() -> None:
    """T-BHW-ARCHITECTURE: the health events are observability only."""
    from pathlib import Path

    composition = Path(build_runtime.__code__.co_filename).read_text(encoding="utf-8")
    assert "health.census" not in composition
    assert "health.probe" not in composition


async def test_the_recovery_stage_is_recognized_but_not_composed(tmp_path) -> None:
    """Staging recovery with the advise posture validates (§16 revision
    three's shape) and composes the controller; the stage has no machinery
    in this wave — the projection renders the configured posture with a null
    verdict, never an invented one."""
    runtime = _compose(
        _watch_payload(stages=["census", "probe", "recovery"]), ManualClock(), tmp_path
    )
    assert runtime.health_watch is not None
    status = await runtime.facade.get_health_watch_status(principal=OPERATOR)
    (unit,) = status["units"]
    assert unit["recovery"]["mode"] == "advise"
    assert unit["recovery"]["verdict"] is None


async def test_an_auto_receipt_that_does_not_exist_degrades_to_advise(tmp_path) -> None:
    """A6's boot half: a missing receipt file degrades that unit's recovery
    posture to advise with the loud note — never a boot failure."""
    runtime = _compose(
        _watch_payload(
            stages=["census", "probe", "recovery"],
            recovery={
                "mode": "auto",
                "auto_receipts": {"mid": "docs/evidence/does-not-exist-yet.md"},
            },
        ),
        ManualClock(),
        tmp_path,
    )
    assert runtime.health_watch is not None
    status = await runtime.facade.get_health_watch_status(principal=OPERATOR)
    (unit,) = status["units"]
    assert unit["recovery"]["mode"] == "advise"
    assert "receipt missing" in unit["recovery"]["note"]


# --- Stage R's composed surfaces (DESIGN_BATTERY_HEALTH_WATCH §7/§12/§17) ----------


async def test_a_staged_recovery_composes_the_park_composer(tmp_path) -> None:
    """The recovery stage composes the ParkController as its ONE mode-write
    reach (§12: an internal composer, never an API) plus the facade's disarm
    twin; the advise posture wires no re-arm port at all — structurally,
    no re-arm by the program can ever occur."""
    from energypod.application.health_watch import HEALTH_ADVISER_PRINCIPAL

    runtime = _compose(
        _watch_payload(stages=["census", "probe", "recovery"]), ManualClock(), tmp_path
    )
    assert runtime.health_watch is not None
    assert runtime.health_watch._park_control is not None
    assert runtime.health_watch._disarm_port is not None
    assert runtime.health_watch._arm_port is None  # advise: no arm path exists

    principal = type("HealthPrincipal", (), {})()
    principal.subject = HEALTH_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch", "arm"})
    principal.interactive = False
    principal.site_id = "home"
    disarmed = await runtime.facade.submit_health_disarm(
        unit_ids=["mid"], principal=principal
    )
    # The simulate rig never starts the fleet task, so the actor's own
    # answer is environment-dependent; the pin is the ATTRIBUTION — the
    # row lands under the health principal with the watch's reason code.
    assert disarmed["units"][0]["status"] in {"disarmed", "refused"}
    rows = [
        event
        for event in runtime.audit.recent(limit=32)
        if event.event_type == "unit_disarmed"
    ]
    assert rows and rows[-1].principal == HEALTH_ADVISER_PRINCIPAL
    assert "health_watch_recovery" in rows[-1].reason_codes


async def test_the_auto_posture_wires_the_one_bounded_rearm_twin(tmp_path) -> None:
    """Auto (with the A6 receipts) composes the re-arm port — the ONLY
    automation arm authority — and it lands an audited unit_armed row with
    the bounded marker and no takeover acknowledgement, ever."""
    from energypod.application.health_watch import HEALTH_ADVISER_PRINCIPAL

    runtime = _compose(
        _watch_payload(
            stages=["census", "probe", "recovery"],
            recovery={
                "mode": "auto",
                "auto_receipts": {"mid": "excluded"},
            },
        ),
        ManualClock(),
        tmp_path,
    )
    assert runtime.health_watch is not None
    assert runtime.health_watch._arm_port is not None
    principal = type("HealthPrincipal", (), {})()
    principal.subject = HEALTH_ADVISER_PRINCIPAL
    principal.scopes = frozenset({"observe", "dispatch", "arm"})
    principal.interactive = False
    principal.site_id = "home"
    outcome = await runtime.facade.submit_health_rearm(unit_id="mid", principal=principal)
    assert outcome["status"] in {"armed", "refused"}
    rows = [
        event for event in runtime.audit.recent(limit=32) if event.event_type == "unit_armed"
    ]
    assert rows and rows[-1].principal == HEALTH_ADVISER_PRINCIPAL
    assert "health_watch_verification_rearm" in rows[-1].reason_codes


def test_the_health_lifecycle_twins_are_never_routed() -> None:
    """§12: the amendment adds an INTERNAL composer, it opens no API — the
    REST surface exposes no health disarm/re-arm route."""
    from pathlib import Path

    from energypod.runtime.composition import build_runtime

    rest = Path(build_runtime.__code__.co_filename).parent.joinpath("..", "api", "rest.py")
    source = rest.resolve().read_text(encoding="utf-8")
    assert "submit_health_disarm" not in source
    assert "submit_health_rearm" not in source


def test_no_control_path_subscribes_to_the_recovery_event() -> None:
    """T-BHW-ARCHITECTURE: the health.recovery event is observability only."""
    from pathlib import Path

    composition = Path(build_runtime.__code__.co_filename).read_text(encoding="utf-8")
    assert "health.recovery" not in composition
