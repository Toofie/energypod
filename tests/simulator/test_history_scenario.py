"""The plant-history simulator scenario (DESIGN_PLANT_HISTORY section 5, H6).

A 48 h compressed scripted walk over the composed simulate runtime with the
``plant_history`` block commissioned: scripted observation streams at
multiple cadences -> sampled rows -> the maintenance rollups -> the query
API.  Every collaborating module is real (the composition root, the
simulator transports, the wire decode, the historian, the history
repository, the query engine); the only injected double is the manual
clock, so the whole walk is deterministic -- the scenario RUNS TWICE and
the two runs must agree exactly.

The scripted trajectory (site-local Brisbane; t = hours from the start):

- idle 0..20 h (both units polled every 30 s);
- controller DOWN 20..22.25 h -- no polls, no ticks (hours 20 and 21 dark);
- degraded rhs 23..24 h (scripted STALE cell quality: rows record dimmed);
- dual cadence 26..32 h (mid polled every 90 s: the staleness guard skips
  every third tick for mid, so mid samples less often than rhs);
- rhs unreachable 33..34 h (link down: rhs's frozen latest writes NO row --
  the honesty guard, an absent row never a fabricated one);
- the night-charge-shaped charge on mid 36..37.5 h (measured -2500 W under
  a manual intent: the commanded triple beside the measured watts, the
  archaeology pin of DESIGN section 6);
- idle to 48 h; the midnight maintenance passes plus one final pass roll
  everything older than the 1-day retention into hourly rollups.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from energypod.adapters.modbus import encode_pq_registers
from energypod.domain.intents import Direction, IntentSource, PowerIntent
from energypod.domain.observations import DataQuality
from energypod.runtime.config import ControllerConfig

SITE_ID = "history-home"
UNITS = ("sim-mid", "sim-rhs")
IDENTITIES = {"sim-mid": "SIM-POD-HIST-MID", "sim-rhs": "SIM-POD-HIST-RHS"}
START = datetime(2026, 8, 24, 14, 0, 0, tzinfo=UTC)  # midnight site-local
SAMPLE_INTERVAL_S = 30.0
CONTROL_PERIOD_S = 0.40
# The device-command lease must outlive one 30 s scenario step so a scripted
# charge holds without per-cycle heartbeat renewal (an observe-only site has
# no renewal obligation to satisfy).
DEVICE_COMMAND_EXPIRY_S = 61.0
HOUR = timedelta(hours=1)

CHARGE_FRAME = encode_pq_registers(-2500, 0)  # charge 2500 W on the wire sign


@dataclass
class ManualClock:
    """The single deterministic time source the composed runtime may read."""

    now: float = 1000.0
    wall: datetime = START

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        del seconds
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


def _config() -> ControllerConfig:
    return ControllerConfig.model_validate(
        {
            "schema_version": 1,
            "revision": 9,
            "mode": "observe_only",
            "site": {
                "site_id": SITE_ID,
                "timezone": "Australia/Brisbane",
                "expected_unit_count": 2,
            },
            "units": [
                {
                    "unit_id": unit_id,
                    "display_name": unit_id,
                    "endpoint": {"host": f"192.168.1.{11 + index}", "port": 4196},
                    "transport_profile": "waveshare_rtu_over_tcp",
                    "protocol_profile": "iot",
                    "device_id": 4,
                    "expected_identity": identity,
                    "expected_cell_count": 60,
                }
                for index, (unit_id, identity) in enumerate(IDENTITIES.items())
            ],
            "timing": {
                "device_command_expiry_s": DEVICE_COMMAND_EXPIRY_S,
                "device_command_expiry_evidence": (
                    "commissioning://history-scenario-watchdog-2026-08/rev-1"
                ),
                "control_period_s": CONTROL_PERIOD_S,
                "essential_read_timeout_s": 0.10,
                "kernel_timeout_s": 0.05,
                "audit_timeout_s": 0.05,
                "write_timeout_s": 0.10,
                "acknowledgement_timeout_s": 0.10,
                "maximum_jitter_s": 0.10,
                "renewal_margin_s": 0.50,
            },
            "storage": {"database_path": "/never/opened-history.sqlite3", "busy_timeout_ms": 250},
            "plant_history": {
                "sample_interval_s": SAMPLE_INTERVAL_S,
                "retention_full_resolution_days": 1,
                "retention_rollup_days": 0,
            },
        }
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _hours(moment: datetime) -> float:
    return (moment - START).total_seconds() / 3600.0


async def run_scenario() -> dict[str, Any]:
    """One full deterministic 48 h walk; returns the canonical facts."""
    from energypod.runtime.composition import build_runtime

    clock = ManualClock()
    runtime = build_runtime(_config(), simulate=True, clock=clock)
    historian = runtime.historian
    repository = runtime.history_repository
    assert historian is not None and repository is not None
    pods = runtime.simulators
    assert pods is not None
    actors = runtime.actors
    for actor in actors.values():
        await actor.start()

    charge_added = False
    horizon_s = 48 * 3600.0
    step = 0
    while True:
        elapsed = clock.now - 1000.0
        if elapsed >= horizon_s:
            break
        t_h = elapsed / 3600.0

        # The controller-down window: the clock jumps past unobserved (no
        # polls, no ticks -- hours 20 and 21 stay dark).
        if abs(t_h - 20.0) < 1e-9:
            clock.advance(2.25 * 3600.0)
            t_h = (clock.now - 1000.0) / 3600.0

        # --- the scripted device conditions for this step --------------------
        for unit_id in UNITS:
            pod = pods[unit_id]
            pod.script_grid_power_w(-300 if unit_id == "sim-mid" else -200)
            pod.script_load_power_w(500 if unit_id == "sim-mid" else 400)
            if unit_id == "sim-rhs":
                if 23.0 <= t_h < 24.0:
                    pod.script_quality("cell_voltages_v", DataQuality.STALE)
                elif t_h >= 24.0:
                    pod.clear_scripted_quality("cell_voltages_v")
                if 33.0 <= t_h < 34.0:
                    pod.drop_link()
                elif t_h >= 34.0 and not pod.link_up:
                    pod.restore_link()

        # --- the polls: rhs every step, mid every 90 s in the dual stretch ---
        with suppress(Exception):
            await actors["sim-rhs"].poll_once()
        dual = 26.0 <= t_h < 32.0
        if not dual or step % 3 == 0:
            with suppress(Exception):
                await actors["sim-mid"].poll_once()

        # --- the night-charge-shaped charge window ----------------------------
        authorized: dict[str, Any] | None = None
        if 36.0 <= t_h < 37.5:
            if not charge_added:
                await runtime.intents.add(
                    PowerIntent(
                        id="history-scenario-charge",
                        source=IntentSource.MANUAL,
                        selected_unit_ids=frozenset({"sim-mid"}),
                        direction=Direction.CHARGE,
                        watts=2500,
                        duration_s=1.5 * 3600.0,
                        accepted_at_mono=clock.now - 1.0,
                        actor_identity="person:operator",
                    )
                )
                charge_added = True
            pods["sim-mid"].apply_pq_frame(CHARGE_FRAME)
            authorized = {"sim-mid": (2500, "charge"), "sim-rhs": None}

        await historian.tick(authorized=authorized)
        step += 1
        if elapsed < horizon_s:
            clock.advance(SAMPLE_INTERVAL_S)

    # The final maintenance pass lands what the midnight passes left (no-op
    # if they already caught it): everything older than the 1-day retention
    # is rolled into hourly rollups.
    repository.maintain(clock.wall_now())

    control = runtime.history_surface
    assert control is not None
    full = control.query_payload(
        range_from=_iso(START + 32 * HOUR),
        range_to=_iso(START + 48 * HOUR),
        fields=("battery_watts", "commanded", "lifecycle"),
        points=600,
    )
    hourly = control.query_payload(
        range_from=_iso(START),
        range_to=_iso(START + 48 * HOUR),
        fields=("battery_watts", "grid_power_w"),
        points=600,
    )

    rows = repository.samples(UNITS, START, START + 48 * HOUR)
    by_unit: dict[str, list[Any]] = {"sim-mid": [], "sim-rhs": []}
    for row in rows:
        by_unit[row.unit_id].append(row)
    charge_rows = [
        row
        for row in by_unit["sim-mid"]
        if START + 36 * HOUR + timedelta(minutes=1) <= row.sampled_at <= START + 37 * HOUR
    ]
    degraded = repository.rollup_hours(("sim-rhs",), START + 23 * HOUR, START + 24 * HOUR)
    digest = hashlib.sha256(
        json.dumps({"full": full, "hourly": hourly}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "digest": digest,
        "full": full,
        "hourly": hourly,
        "row_counts": {unit: len(by_unit[unit]) for unit in UNITS},
        "unreachable_rows": [
            _iso(row.sampled_at)
            for row in by_unit["sim-rhs"]
            if START + 33 * HOUR < row.sampled_at < START + 34 * HOUR
        ],
        "charge_rows": len(charge_rows),
        "charge_shape": [
            (
                row.commanded_source,
                row.commanded_direction,
                row.commanded_w,
                row.battery_watts,
                row.run_mode_w,
            )
            for row in charge_rows[:2] + charge_rows[-2:]
        ],
        "degraded_worst": [rollup.worst_quality for rollup in degraded],
        "last_sample_at": {
            unit: _iso(moment) if moment is not None else None
            for unit, moment in repository.last_sample_at(UNITS).items()
        },
    }


async def test_the_48h_history_scenario_walks_end_to_end_and_repeats_exactly() -> None:
    """DESIGN_PLANT_HISTORY section 5 (H6): rows -> rollups -> query, with
    the archaeology shape, both gap regimes, the fleet all-present rule, and
    the stale-observation guard -- and the whole walk deterministic."""
    first = await run_scenario()
    second = await run_scenario()
    assert first["digest"] == second["digest"], "the scenario must repeat exactly"
    for key in ("row_counts", "unreachable_rows", "charge_rows", "charge_shape", "degraded_worst"):
        assert first[key] == second[key], f"the {key} facts must repeat exactly"

    # --- the sampled rows ------------------------------------------------------
    assert first["row_counts"]["sim-rhs"] > 0 and first["row_counts"]["sim-mid"] > 0
    assert first["row_counts"]["sim-rhs"] > first["row_counts"]["sim-mid"], (
        "mid's dual-cadence stretch polls less often, so fewer sampled rows survive "
        "the staleness guard"
    )
    assert first["unreachable_rows"] == [], (
        "a stale latest writes NO row: the unreachable window is an honest gap"
    )

    # --- the archaeology pin (DESIGN section 6) ---------------------------------
    assert first["charge_rows"] >= 100
    for source, direction, watts, measured, run_mode in first["charge_shape"]:
        assert (source, direction, watts) == ("manual", "charge", 2500)
        assert measured == -2500.0, "the commanded watts beside the measured watts"
        assert run_mode == 1, "run_mode_w 1 (Remote PQ) under any writer's objective"

    # --- the full-resolution query ----------------------------------------------
    full = first["full"]
    assert full["resolution"] == "full"
    mid = full["units"]["sim-mid"]
    # The exact change points: the null triple at the window's first sample,
    # the manual charge at 36 h, back to null when the intent lapses.
    assert [entry["t"] for entry in mid["commanded_changes"]] == [
        _iso(START + 32 * HOUR),
        _iso(START + 36 * HOUR),
        _iso(START + 37 * HOUR + timedelta(minutes=30)),
    ]
    assert mid["commanded_changes"][1] == {
        "t": _iso(START + 36 * HOUR),
        "source": "manual",
        "direction": "charge",
        "watts": 2500,
    }
    battery = mid["series"]["battery_watts"]
    assert battery["window_min"] == -2500.0
    assert battery["window_min_at"] >= _iso(START + 36 * HOUR)
    assert battery["window_max"] == 0.0, "an idle pod's real zero, never a filled one"

    # rhs's unreachable window is a server-computed gap in the full view.
    rhs = full["units"]["sim-rhs"]
    assert rhs["gaps"] == [{"from": _iso(START + 33 * HOUR), "to": _iso(START + 34 * HOUR)}]

    # The fleet sums only where EVERY unit has a row: no fleet battery point
    # exists strictly inside the unreachable window, and the fleet series
    # spans the window's real first/last shared samples.
    fleet_points = [point["t"] for point in full["fleet"]["series"]["battery_watts"]["points"]]
    assert fleet_points[0] == _iso(START + 32 * HOUR)
    assert fleet_points[-1] == _iso(START + 48 * HOUR - timedelta(seconds=SAMPLE_INTERVAL_S))
    unreachable_open = (_iso(START + 33 * HOUR), _iso(START + 34 * HOUR))
    assert not [t for t in fleet_points if unreachable_open[0] < t < unreachable_open[1]], (
        "one unreadable unit is never treated as zero"
    )

    # --- the hourly query ---------------------------------------------------------
    hourly = first["hourly"]
    assert hourly["resolution"] == "hourly", "a window opening before the horizon serves hourly"
    hourly_mid = hourly["units"]["sim-mid"]
    # The dark controller-down hours are missing hours, never zeroed hours.
    assert hourly_mid["gaps"] == [
        {"from": _iso(START + 20 * HOUR), "to": _iso(START + 21 * HOUR)},
        {"from": _iso(START + 21 * HOUR), "to": _iso(START + 22 * HOUR)},
    ]
    hours = [point["t"] for point in hourly_mid["series"]["battery_watts"]["points"]]
    assert hours[0] == _iso(START)
    assert hours[-1] == _iso(START + 23 * HOUR), (
        "only the rolled hours serve; the full-res region stays full-res (the design's stated cost)"
    )
    by_hour = {point["t"]: point for point in hourly_mid["series"]["battery_watts"]["points"]}
    # A fully covered hour at the 30 s cadence holds exactly 120 samples --
    # the design's coverage marker -- and an idle pod's honest zero mean.
    assert by_hour[hours[0]]["n"] == 120
    assert by_hour[hours[0]]["v"] == 0.0
    # The outage ends 15 minutes into hour 22: a PARTIAL hour keeps its
    # honest reduced count, never a zeroed one.
    partial = by_hour[_iso(START + 22 * HOUR)]
    assert 0 < partial["n"] < 120

    # The degraded stretch rolls its worst quality forward: the hour's rows
    # render dimmed, never silently clean.
    assert first["degraded_worst"] == ["stale"]

    # --- the history_state hint ----------------------------------------------------
    assert first["last_sample_at"]["sim-mid"] == first["last_sample_at"]["sim-rhs"]
    assert first["last_sample_at"]["sim-mid"] is not None
