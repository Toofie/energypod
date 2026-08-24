/**
 * THE HOME VIEW'S SCREENSHOT STATES (dev tooling; the design-polish loop's
 * scenery). Every state is a complete seeded world built from the shared wire
 * fixtures — real wire shapes, never a figure invented for the camera:
 *
 * - battery_watts: NEGATIVE = charging, POSITIVE = discharging.
 * - grid_power_w: NEGATIVE = import, POSITIVE = export.
 *
 * Two states pin the household's two halves: the daytime answer (an armed
 * fleet load-serving under an operator request, the Today card, the solar
 * tile active on real export) and the supervised-night answer (the night tile
 * pacing, the schedules card naming the running window).
 */
import {
  adviserState,
  energyToday,
  lastObjectiveObserved,
  nightChargeState,
  parkState,
  scheduleNextAction,
  scheduleState,
  snapshot,
  telemetrySummary,
  unitSnapshot,
  withAdviserState,
  withEnergyToday,
  withHealth,
  withNightChargeState,
  withObjective,
  withParkState,
  withScheduleState,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

const CAPTURED_AT = "2026-08-26T14:03:00+10:00";

/** The fleet's three phases, load-serving under a 1,000 W-per-battery request. */
function afternoonWorld() {
  const units = [
    unitSnapshot({
      unit_id: "lhs",
      lifecycle: "active",
      quality: "good",
      telemetry_age_s: 2,
      requested_power: { direction: "discharge", watts: 3000 },
      authorized_power: { direction: "discharge", watts: 1000 },
      measured_watts: 980,
      telemetry: telemetrySummary({
        soc_pct: 64,
        battery_watts: 980,
        grid_power_w: -610,
        load_power_w: 610,
        cell_spread_mv: 18,
        active_warnings: [],
      }),
    }),
    unitSnapshot({
      unit_id: "mid",
      lifecycle: "active",
      quality: "good",
      telemetry_age_s: 2,
      requested_power: { direction: "discharge", watts: 3000 },
      authorized_power: { direction: "discharge", watts: 1000 },
      measured_watts: 1004,
      telemetry: telemetrySummary({
        soc_pct: 71,
        battery_watts: 1004,
        grid_power_w: -940,
        load_power_w: 940,
        cell_spread_mv: 22,
        active_warnings: [],
      }),
    }),
    unitSnapshot({
      unit_id: "rhs",
      lifecycle: "armed_idle",
      quality: "good",
      telemetry_age_s: 4,
      measured_watts: 0,
      telemetry: telemetrySummary({
        soc_pct: 97,
        battery_watts: 0,
        grid_power_w: -380,
        load_power_w: 380,
        cell_spread_mv: 9,
        active_warnings: [],
      }),
    }),
  ];
  let world = snapshot(units, { captured_at: CAPTURED_AT, snapshot_sequence: 4106 });
  world = withEnergyToday(world, energyToday({ as_of: CAPTURED_AT }));
  world = withAdviserState(
    world,
    adviserState({
      active: true,
      hysteresis_state: "holding",
      commanded_charge_w: 1400,
      eligible_export_charge_w: 1600,
      fleet_export_w: 1930,
    }),
  );
  return world;
}

/** The supervised night: the night tile pacing, the running window named. */
function nightWorld() {
  const units = ["lhs", "mid", "rhs"].map((unitId, index) =>
    unitSnapshot({
      unit_id: unitId,
      lifecycle: index === 2 ? "armed_idle" : "active",
      quality: "good",
      telemetry_age_s: 3,
      requested_power:
        index === 2
          ? { direction: "idle", watts: 0 }
          : { direction: "charge", watts: 5000 },
      authorized_power:
        index === 2 ? null : { direction: "charge", watts: 2500 },
      measured_watts: index === 2 ? 0 : -2500,
      telemetry: telemetrySummary({
        soc_pct: [71.4, 88, 98][index]!,
        battery_watts: index === 2 ? 0 : -2500,
        grid_power_w: [-1612, -1612, -388][index]!,
        load_power_w: [112, 112, 388][index]!,
        cell_spread_mv: 26,
        active_warnings: [],
      }),
    }),
  );
  let world = snapshot(units, { captured_at: "2026-08-27T01:31:00+10:00", snapshot_sequence: 5310 });
  world = withNightChargeState(world, nightChargeState());
  world = withScheduleState(
    world,
    scheduleState({
      next: scheduleNextAction({
        entry_id: "Day charge",
        start_local: "06:30",
        end_local: "18:00",
        action: "charge",
        starts_at: "2026-08-27T06:30:00+10:00",
        starts_in_s: 18_540,
      }),
    }),
  );
  world = withEnergyToday(
    world,
    energyToday({ as_of: "2026-08-27T01:31:00+10:00", kind: "in_progress" }),
  );
  // lhs carries the detector's foreign summary — the quiet caution line
  // renders; mid self-heals — the quiet-positive badge renders.
  world = {
    ...world,
    units: world.units.map((unit, index) =>
      index === 0
        ? withObjective(
            unit,
            lastObjectiveObserved({
              observed_at: "2026-08-27T01:12:00+10:00",
              active_w: -2400,
            }),
          )
        : index === 1
          ? withHealth(unit, { state: "self_healing", reasons: ["gateway_unreachable"] })
          : unit,
    ),
  };
  return world;
}

/**
 * The parked household (DESIGN_POD_PARKING §8): the afternoon world with rhs
 * standing by under a live operator lease — the fleet banner names it with
 * its countdown and carries the fixed not-isolation sentence, while the other
 * two batteries keep load-serving. Every unit carries the park projection
 * (the commissioned site's own truth).
 */
function parkedAfternoonWorld() {
  const base = afternoonWorld();
  return {
    ...base,
    units: base.units.map((unit) =>
      unit.unit_id === "rhs"
        ? withParkState(
            unit,
            parkState({
              reason: "evening standby",
              // A live lease ~2 h out with capture slack, so the floored H:MM
              // holds through the shot (a fixed 2026-08-24 instant would read
              // expired the moment the wall clock moved past it).
              lease_expires_at: new Date(Date.now() + (2 * 3600 + 30) * 1000).toISOString(),
            }),
          )
        : withParkState(unit, parkState({ parked: false })),
    ),
  };
}

export const HOME_STATES: readonly ShotStateDefinition[] = [
  {
    id: "afternoon-live",
    caption:
      "The daytime answer: an armed fleet load-serving under a per-battery request, the Today card, and the solar tile active on a real 1,930 W export.",
    world: afternoonWorld,
  },
  {
    id: "supervised-night",
    caption:
      "The supervised night: the night tile pacing at the cap, the schedules card naming the running window, one battery self-healing and one flagged foreign.",
    world: nightWorld,
  },
  {
    id: "fleet-parked",
    caption:
      "A pod standing by: the parked fleet banner names rhs with its lease countdown and the fixed not-isolation sentence, while the other two batteries keep load-serving.",
    world: parkedAfternoonWorld,
  },
];
