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
 * pacing, the schedules card naming the running window). The night-V2 states
 * extend the supervised night with the forecast-aware target's own lines:
 * SUGGEST with a live computation (the banner, the target line, the trust
 * scoreboard mid-earn), the §3.3 fallback word (a v1 night under a live
 * posture, loudly), and the A5 morning notice (the below-target close,
 * latched until midday).
 */
import {
  adviserState,
  energyTariff,
  energyToday,
  lastObjectiveObserved,
  nightChargeState,
  nightForecastFallback,
  nightForecastOk,
  nightMorningNotice,
  nightTrust,
  nightUnitState,
  parkState,
  pvoutputStatus,
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
  type WireNightChargeState,
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

/**
 * The night-V2 household: the supervised night's world with the night
 * projection swapped for a forecast-posture frame (DESIGN_NIGHT_CHARGE_V2
 * §7/§8 — every figure the §7 example pins, the trust scoreboard mid-earn,
 * the commissioned tariff on the energy line).
 */
function nightV2World(night: WireNightChargeState): () => WireSnapshot {
  return () => {
    const base = nightWorld();
    // The commissioned tariff rides the V2 worlds' energy line (the site's own
    // today: the operator's rates render as rates, the night-window gap named).
    const withTariff = withEnergyToday(
      base,
      energyToday({ as_of: "2026-08-27T01:31:00+10:00", kind: "in_progress", tariff: energyTariff() }),
    );
    return withNightChargeState(withTariff, night);
  };
}

/** SUGGEST with a live computation — the commissioning run's own picture. */
function nightSuggestWorld(): WireSnapshot {
  return nightV2World(
    nightChargeState({
      target_policy: "forecast_suggest",
      trust: nightTrust({
        state: "provisioning",
        days_scored: 6,
        mean_abs_err_pct: 18.6,
        bias_pct: -6.9,
        low_surplus_days: 2,
        high_surplus_days: 1,
      }),
      forecast: nightForecastOk(),
      explanation:
        "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 finishes it (solcast p10, issued 18:03)",
      units: [
        nightUnitState({ unit_id: "lhs", soc_pct: 61.8, suggested_target_soc_pct: 65.5, target_w: 1900 }),
        nightUnitState({ unit_id: "mid", soc_pct: 64.0, suggested_target_soc_pct: 65.5, target_w: 1900 }),
        nightUnitState({ unit_id: "rhs", soc_pct: 68.9, phase: "complete", suggested_target_soc_pct: 65.5, target_w: 0, reason: "target_reached" }),
      ],
    }),
  )();
}

/** The §3.3 fallback: a stale forecast, the ladder's word loud, the v1 charge. */
function nightFallbackWorld(): WireSnapshot {
  return nightV2World(
    nightChargeState({
      target_policy: "forecast_suggest",
      trust: nightTrust(),
      forecast: nightForecastFallback("forecast_stale"),
      explanation: null,
      reason_codes: ["window_open", "on_plan", "forecast_stale"],
      units: [
        nightUnitState({ unit_id: "lhs", soc_pct: 61.8, target_w: 2500 }),
        nightUnitState({ unit_id: "mid", soc_pct: 88.0, target_w: 2500 }),
        nightUnitState({ unit_id: "rhs", soc_pct: 98.0, phase: "skipped_full", target_w: 0, reason: "at_ceiling" }),
      ],
    }),
  )();
}

/** The morning after a below-target close: the A5 notice latched until midday. */
function nightMorningNoticeWorld(): WireSnapshot {
  return nightV2World(
    nightChargeState({
      enabled: true,
      enabled_origin: "config",
      active: false,
      phase: "idle",
      held_intent_id: null,
      window_ends_at: null,
      window_ends_in_s: null,
      next_window_at: "2026-08-29T00:00:00+10:00",
      reason_codes: ["outside_window", "window_closed_below_target"],
      target_policy: "forecast_suggest",
      trust: nightTrust({
        state: "provisioning",
        days_scored: 6,
        mean_abs_err_pct: 18.6,
        bias_pct: -6.9,
        low_surplus_days: 2,
        high_surplus_days: 1,
      }),
      forecast: null,
      explanation: null,
      morning_notice: nightMorningNotice({
        date: "2026-08-28",
        target_soc_pct: 65.5,
        units_below_target: ["rhs", "lhs"],
      }),
      // The real outside-window frame carries NO unit rows (the tick's plans
      // are empty outside the window) — the notice stands on its own.
      units: [],
    }),
  )();
}

export const HOME_STATES: readonly ShotStateDefinition[] = [
  {
    id: "afternoon-live",
    caption:
      "The daytime answer: an armed fleet load-serving under a per-battery request, the Today card, the solar tile active on a real 1,930 W export, and the PVOutput reporter composed but off (the config's own setting).",
    world: afternoonWorld,
    pvoutput: () => pvoutputStatus({ enabled: false, enabled_origin: "config" }),
  },
  {
    id: "supervised-night",
    caption:
      "The supervised night: the night tile pacing at the cap, the schedules card naming the running window, one battery self-healing and one flagged foreign, and the PVOutput reporter posting (the console's own enable, kept across restarts).",
    world: nightWorld,
    pvoutput: () =>
      pvoutputStatus({
        enabled: true,
        enabled_origin: "runtime",
        last_success_at: "2026-08-26T15:30:01+00:00",
        last_post_age_s: 112,
        last_posted_slot: "2026-08-27 01:30",
        rate_remaining: 43,
      }),
  },
  {
    id: "night-forecast-suggest",
    caption:
      "Night V2 under forecast_suggest: the suggested target with its arithmetic and the 95-vs-100 clause, the never-silent banner, the reasoning sentence with the forecast's age, and the trust scoreboard mid-earn (6/14 days, never a verdict yet). rhs is complete at target.",
    world: nightSuggestWorld,
  },
  {
    id: "night-forecast-fallback",
    caption:
      "Night V2's fail-safe: the forecast is stale, the ladder's word renders loud beside the still-pacing v1 charge ('charging full tonight'), with the trust scoreboard earned.",
    world: nightFallbackWorld,
  },
  {
    id: "night-morning-notice",
    caption:
      "The A5 morning notice: the window closed below target (rhs and lhs under 65.5%), the notice latched until 12:00 beside the plan rows, the Insights cross-link, and the outside-window status.",
    world: nightMorningNoticeWorld,
  },
  {
    id: "fleet-parked",
    caption:
      "A pod standing by: the parked fleet banner names rhs with its lease countdown and the fixed not-isolation sentence, the other two batteries keep load-serving, and the PVOutput reporter names one honestly-skipped slot.",
    world: parkedAfternoonWorld,
    pvoutput: () =>
      pvoutputStatus({
        enabled: true,
        enabled_origin: "runtime",
        last_success_at: "2026-08-26T04:30:01+00:00",
        last_post_age_s: 392,
        last_posted_slot: "2026-08-26 14:30",
        rate_remaining: 38,
        slots_skipped_stale: 1,
      }),
  },
  {
    id: "pvoutput-failing",
    caption:
      "The reporter's loud failure state: PVOutput refused the key (read-only), so the uploader disabled itself with the refusal's own words beside the toggle -- re-enabling is the operator's retry.",
    world: nightWorld,
    pvoutput: () =>
      pvoutputStatus({
        enabled: true,
        enabled_origin: "runtime",
        disabled_reason: "auth_failed",
        last_error: "Read only key",
        last_success_at: "2026-08-26T13:35:01+00:00",
        last_post_age_s: 5412,
        last_posted_slot: "2026-08-26 23:35",
        consecutive_failures: 3,
      }),
  },
  {
    id: "pvoutput-not-commissioned",
    caption:
      "The structured not-commissioned state: a controller without the pvoutput config block answers the status route with its 409, and the card's one honest sentence says commissioning is a config change -- there is no toggle to present.",
    world: afternoonWorld,
  },
];
