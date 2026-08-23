"""Composition contracts for the night-writer detector (foreign-objective watch).

Drives ``build_runtime`` the way ``tests/unit/test_composition.py`` does — the
composed fleet loop, the real actors, the real supervisor — for the detector's
three composition facts (API_CONTRACTS "Night-writer detector"):

1. the detector composes for EVERY deployment (observe-only with no policy
   block at all, write-enabled, simulate);
2. supervision itself samples and classifies: a scripted foreign objective on
   the simulated wire produces the session evidence, exactly one alert audit
   fact, exactly one bus event, and the read surfaces carry it — and a cleared
   objective closes the episode silently;
3. a unit claimed by our own intent is never sampled (we are the writer on the
   wire while the claim lives).

The harness is imported from the composition suite (the same precedent the
event-contract suite uses for the bus fakes).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.unit.test_composition import (
    OPERATOR,
    ScriptedClock,
    _bus_events,
    _LifespanSession,
    compose,
    compose_write_enabled,
)


def _objective_policy_overrides() -> dict[str, Any]:
    """Detector knobs shrunk to the scripted 0.40 s control cadence so three
    sustained samples land within seconds of scripted time."""
    return {
        "foreign_objective_sample_interval_s": 1.0,
        "foreign_objective_sustained_samples": 3,
        "foreign_objective_handback_grace_s": 1.0,
    }


def test_the_detector_composes_for_every_deployment(tmp_path: Path) -> None:
    """Composed ALWAYS: observe-only (no policy block at all), write-enabled,
    and simulate each hold a live monitor."""
    observe_only = compose(tmp_path / "observe.sqlite3")
    write_enabled = compose_write_enabled(tmp_path / "write.sqlite3")
    simulated = compose(tmp_path / "simulate.sqlite3", simulate=True)

    for runtime in (observe_only, write_enabled, simulated):
        assert runtime.foreign_objective is not None


def _detector_events(runtime: Any) -> list[Any]:
    return [
        event
        for event in runtime.audit.recent(limit=256)
        if event.event_type == "foreign_objective_observed"
    ]


async def test_supervision_samples_and_escalates_a_scripted_foreign_objective(
    tmp_path: Path,
) -> None:
    """THE night scenario, end to end through the composed fleet loop: a
    foreign -2400 W charge objective appears on the wire while every unit is
    disarmed and unclaimed, the loop's bounded pass samples it, the evidence
    accumulates in the session record, and the pinned pattern rules escalate
    to exactly one audit fact and one bus event.  Clearing the objective
    closes the episode silently."""
    runtime = compose_write_enabled(
        tmp_path / "night.sqlite3",
        simulate=True,
        clock=ScriptedClock(),
        policy_overrides={
            "maximum_cell_imbalance_v": 0.50,
            **_objective_policy_overrides(),
        },
    )
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"
        await session.pump_until(
            lambda: all(
                actor.lifecycle.value in {"disarmed", "observe_only"}
                for actor in runtime.actors.values()
            ),
            message="the simulated fleet never reached its uncommanded lifecycle",
        )

        # A quiet fleet records nothing: the simulated pods serve (0, 0).
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        for unit in snapshot["units"]:
            assert unit["last_objective_observed"] is None, unit

        # The night writer arrives: -2400 W on mid's served wire.
        pod = runtime.simulators["mid"]
        pod.script_objective(-2400, 0)

        await session.pump_until(
            lambda: bool(_detector_events(runtime)),
            message="supervision never escalated the scripted foreign objective",
        )
        (event,) = _detector_events(runtime)
        assert event.unit_id == "mid"
        assert event.reason_codes in (
            ("sustained_remote_mode_objective",),
            ("sustained_charge_without_pv_evidence",),
        )

        alerts = [
            body
            for body in await _bus_events(runtime, limit=256)
            if body["type"] == "foreign_objective.observed"
        ]
        assert len(alerts) == 1, "evidence every sample, exactly one alert"
        assert alerts[0]["payload"]["active_w"] == -2400

        # The surfaces carry it: the compact snapshot summary and the window
        # characterization read.
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        units = {unit["unit_id"]: unit for unit in snapshot["units"]}
        summary = units["mid"]["last_objective_observed"]
        assert summary is not None and summary["active_w"] == -2400
        assert set(summary) == {
            "observed_at",
            "active_w",
            "reactive_var",
            "classification",
            "reason",
        }
        payload = await runtime.facade.get_observed_objectives(principal=OPERATOR, last="24h")
        entry = next(unit for unit in payload["units"] if unit["unit_id"] == "mid")
        assert entry["sample_count"] >= 3
        assert entry["min_active_w"] == -2400
        assert entry["foreign_active"] is True
        other = next(unit for unit in payload["units"] if unit["unit_id"] != "mid")
        assert other["sample_count"] == 0, "an uncommanded quiet pod records nothing"

        # The writer stands down: the episode closes silently, no new alert.
        pod.clear_scripted_objective()

        def episode_closed() -> bool:
            view = runtime.foreign_objective.window_payload(last_hours=24)
            entry = next(unit for unit in view["units"] if unit["unit_id"] == "mid")
            return entry["foreign_active"] is False

        await session.pump_until(
            episode_closed, message="the cleared objective never closed the episode"
        )
        assert len(_detector_events(runtime)) == 1

        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        assert session.seen("lifespan.shutdown.complete"), f"shutdown failed: {session.events!r}"
    finally:
        await session.close()


async def test_a_unit_claimed_by_our_own_intent_is_never_sampled(tmp_path: Path) -> None:
    """Our own intent serving: while the claim is live the detector does not
    sample the unit at all -- the words on the wire are ours -- and the
    session record for that unit stays empty through the whole command."""
    runtime = compose_write_enabled(
        tmp_path / "claimed.sqlite3",
        simulate=True,
        clock=ScriptedClock(),
        policy_overrides={
            "maximum_cell_imbalance_v": 0.50,
            **_objective_policy_overrides(),
        },
    )
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"
        await session.pump_until(
            lambda: runtime.actors["mid"].lifecycle.value == "disarmed",
            message="mid never qualified to its disarmed lifecycle",
        )
        await runtime.facade.arm(
            unit_ids=["mid"],
            principal=OPERATOR,
            idempotency_key="objective-claim-arm",
            request_id="objective-claim-arm-request",
        )
        await runtime.facade.submit_intent(
            unit_ids=["mid"],
            direction="charge",
            watts=1200,
            ttl_s=8.0,
            reason="objective claim scenario",
            principal=OPERATOR,
            idempotency_key="objective-claim-dispatch",
            request_id="objective-claim-dispatch-request",
        )
        await session.pump_until(
            lambda: runtime.actors["mid"].lifecycle.value == "active",
            message="mid never went active under the claim",
        )
        # Hold the claim for well past several sample intervals.
        for _ in range(200):
            await asyncio.sleep(0)

        view = runtime.foreign_objective.window_payload(last_hours=24)
        entry = next(unit for unit in view["units"] if unit["unit_id"] == "mid")
        assert entry["sample_count"] == 0, "a claimed unit is never sampled"
        assert entry["last_objective_observed"] is None
        assert _detector_events(runtime) == []

        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
    finally:
        await session.close()
