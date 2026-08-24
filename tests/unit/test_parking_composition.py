"""The composed parking surface (DESIGN_POD_PARKING sections 2-4).

The composition-level pins: the ``parking:`` block composes the controller
and the REST routes (block-presence), an operator PARK through the FACADE
drives the real actor-mailbox named operation against the simulator pod (the
live wire shape: prior read, named write, bounded readback), the snapshot and
unit detail carry ``park_state`` exactly when the block is present, the
dispatch refusal carries parked provenance, control readiness names
``unit:parked``, and the simulator-mode lease survives across a rebuild over
the same in-memory store.

SAFETY: the in-memory simulate composition only -- no socket, no live system,
no process restart.
"""

from __future__ import annotations

import contextlib
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


def _parking_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 12,
        "mode": "write_enabled",
        "site": {
            "site_id": "home",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [_unit_payload("mid", "BEP-MID", "192.168.1.11")],
        "timing": _timing_payload(),
        "policy": {**_policy_payload(), "maximum_cell_imbalance_v": 0.50},
        "authentication": _authentication_payload(),
    }
    payload["parking"] = dict(overrides or {"max_lease_s": 14400, "default_lease_s": 14400})
    return payload


def _compose(parking: dict[str, Any] | None, clock: Any) -> Any:
    payload = _parking_payload(**(parking or {}))
    if parking is None:
        payload.pop("parking")
    return build_runtime(ControllerConfig.model_validate(payload), clock=clock, simulate=True)


class ManualClock:
    """Deterministic time source (the B2 wedge-scenario clock's shape)."""

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.now = 5000.0
        self.wall = datetime(2026, 8, 24, 2, 0, 0, tzinfo=UTC)
        self._waiters: list[tuple[float, Any]] = []

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> Any:
        return self.wall

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now += seconds
        self.wall += timedelta(seconds=seconds)
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
        await future


async def test_an_absent_parking_block_composes_nothing_and_refuses(tmp_path: Path) -> None:
    runtime = _compose(None, ManualClock())
    assert runtime.parking is None
    with pytest.raises(Exception) as caught:
        await runtime.facade.park_unit(
            unit_id="mid",
            confirmation="PARK",
            reason="inverter work",
            principal=OPERATOR,
            idempotency_key="park-absent",
            request_id="park-absent-request",
        )
    assert getattr(caught.value, "code", "") == "park_not_commissioned"
    assert getattr(caught.value, "details", {}) == {"cause": "block_absent"}
    # Absent block: the snapshot carries NO park_state key at all.
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    (unit,) = snapshot["units"]
    assert "park_state" not in unit


async def test_a_present_block_composes_the_controller_and_the_rest_routes(
    tmp_path: Path,
) -> None:
    runtime = _compose({"max_lease_s": 7200, "default_lease_s": 3600}, ManualClock())
    assert runtime.parking is not None
    paths = {
        route.path for route in runtime.app.routes if getattr(route, "path", "").startswith("/api")
    }
    assert "/api/v1/units/{unit_id}/park" in paths
    assert "/api/v1/units/{unit_id}/park/renew" in paths
    assert "/api/v1/units/{unit_id}/resume" in paths


async def test_an_operator_park_drives_the_real_actor_mailbox_operation(
    tmp_path: Path,
) -> None:
    """The wire shape end to end over the simulator: prior read at 0x8100, the
    NAMED 0x8000 write, the bounded readback -- one mailbox operation."""
    clock = ManualClock()
    runtime = _compose({"max_lease_s": 7200, "default_lease_s": 3600}, clock)
    actor = runtime.actors["mid"]
    await actor.start()
    try:
        result = await runtime.facade.park_unit(
            unit_id="mid",
            confirmation="PARK",
            reason="inverter work",
            lease_s=3600,
            principal=OPERATOR,
            idempotency_key="park-1",
            request_id="park-1-request",
        )
        assert result["prior_word"] == 0
        assert result["written_value"] == 1 and result["readback_word"] == 1
        assert result["verified"] is True
        assert result["lease"]["max_total_s"] == 7200
        assert result["lease"]["epoch"] == 1

        # The simulator pod really is parked: the readback word serves 1 and a
        # PQ write still ACKs while delivered power stays 0 (B2's model).
        pod = runtime.simulators["mid"]
        assert pod._debug_mode == 1

        # park_state rides the snapshot and the unit detail.
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        (unit,) = snapshot["units"]
        assert unit["park_state"]["parked"] is True
        assert unit["park_state"]["origin"] == "operator"
        assert unit["park_state"]["reason"] == "inverter work"
        assert unit["park_state"]["remaining_cap_s"] >= 3599
        detail = await runtime.facade.unit_detail(principal=OPERATOR, unit_id="mid")
        assert detail["park_state"]["parked"] is True

        # Control readiness names the parked unit.
        health = await runtime.facade.health(principal=OPERATOR)
        assert "mid:parked" in health["control_readiness"]["reasons"]

        # A fresh poll serves the debug word the dispatch gate judges on
        # (advisory doctrine: absent evidence refuses nothing).
        clock.advance(0.5)
        await actor.poll_once()
        # The dispatch refusal fires on the wire-level gate with provenance.
        with pytest.raises(ValueError, match="device_debug_mode_active") as caught:
            await runtime.facade.submit_intent(
                unit_ids=["mid"],
                direction="charge",
                watts=500,
                ttl_s=30.0,
                principal=OPERATOR,
                idempotency_key="dispatch-parked",
                request_id="dispatch-parked-request",
            )
        details = getattr(caught.value, "details", {})
        assert set(details["mid"]["parked_provenance"]) == {
            "parked_at",
            "authorizer",
            "reason",
            "lease_expires_at",
        }

        # The durable audit trail: pending then parked.
        rows = [
            event
            for event in runtime.audit.recent(limit=32)
            if event.event_type == "unit_parked"
        ]
        assert [row.result for row in rows] == ["parked", "pending"]
        assert rows[0].unit_id == "mid"
        assert "readback_verified" in rows[0].reason_codes

        # Resume through the facade: the exit write, the checklist, the close.
        resumed = await runtime.facade.resume_unit(
            unit_id="mid",
            confirmation="RESUME",
            principal=OPERATOR,
            idempotency_key="resume-1",
            request_id="resume-1-request",
        )
        assert resumed["origin"] == "operator" and resumed["verified"] is True
        assert pod._debug_mode == 0
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        (unit,) = snapshot["units"]
        assert unit["park_state"]["parked"] is False
    finally:
        for handle in runtime.actors.values():
            with contextlib.suppress(Exception):
                await handle.shutdown()


async def test_the_simulator_mode_lease_survives_a_facade_rebuild_over_the_store(
    tmp_path: Path,
) -> None:
    """T-PARK-CRASH/RESTART's simulator-mode survival: the in-memory lease
    store is the process's durable truth -- a controller rebuilt over the
    same store (the composition's simulate runtime holds one repository
    instance for the process lifetime) still sees the parked lease, and boot
    reconstruction never writes."""
    from energypod.application.parking import ParkCommissioning, ParkController

    clock = ManualClock()
    runtime = _compose({"max_lease_s": 7200, "default_lease_s": 3600}, clock)
    actor = runtime.actors["mid"]
    await actor.start()
    try:
        await runtime.facade.park_unit(
            unit_id="mid",
            confirmation="PARK",
            reason="inverter work",
            principal=OPERATOR,
            idempotency_key="park-1",
            request_id="park-1-request",
        )
        store = runtime.parking._store  # the process-lifetime lease store
        revived = ParkController(
            unit_ids=frozenset({"mid", "rhs"}),
            commissioning=ParkCommissioning(
                max_lease_s=7200, default_lease_s=3600, mode_write_enabled=True
            ),
            clock=clock,
            store=store,
            audit=runtime.intents,  # event-only port; boot writes no rows here
            bus=runtime.event_bus,
            actors=runtime.actors,
            observations=runtime.observations,
            intents=runtime.intents,
            process_instance_id="process-2",
            process_origin_mono=clock.monotonic(),
        )
        await revived.reconstruct_at_boot()
        states = await revived.park_states()
        assert states["mid"]["parked"] is True
        assert runtime.simulators["mid"]._debug_mode == 1, "boot never writes"
    finally:
        for handle in runtime.actors.values():
            with contextlib.suppress(Exception):
                await handle.shutdown()
