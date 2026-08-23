/**
 * THE HISTORY VIEW'S SCREENSHOT STATES (dev tooling; the design-polish loop's
 * scenery). Every state is a complete seeded world: a snapshot built from the
 * shared wire fixtures (the recording hint riding history_state) plus the
 * plant-history route's 200 body — real wire shapes, live-verified against
 * the composed controller, never a figure invented for the camera:
 *
 * - battery_watts: NEGATIVE = charging, POSITIVE = discharging (the house
 *   sign convention the charts label at the legend).
 * - grid_power_w: NEGATIVE = import, POSITIVE = export.
 * - gaps are absent rows: the controller-down interval 02:10–03:40 has NO
 *   points anywhere, and the charts must break there — never bridge.
 *
 * The four states pin the view's landing surfaces: the recorded night (full
 * resolution, a charge, a gap, a degraded stretch), the hourly rollup tier a
 * 30-day window honestly serves, the fresh database's empty window (the
 * first thing an operator sees the morning after commissioning), and the
 * honest not-commissioned 409.
 *
 * Figures are physically coherent (grid ≈ battery + house per phase, the
 * fleet the true sum) and deterministic — a fixed UTC day, piecewise-linear
 * stories, no clock or random dependence.
 */
import {
  historyBody,
  historySeries,
  historyState,
  historyUnit,
  snapshot,
  unitSnapshot,
  withHistoryState,
} from "../../../src/test/wire";
import type { WireHistorySeries } from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

/** The seeded day: one fixed UTC window, evening discharge → night charge. */
const FROM = Date.parse("2026-08-23T14:00:00+00:00");
const TO = Date.parse("2026-08-24T06:00:00+00:00");
/** The sample spacing the wire's LTTB leaves at this window (2 min). */
const STEP_S = 120;
/** The controller-down interval — an honest gap on every unit and the fleet. */
const GAP = {
  from: new Date(Date.parse("2026-08-24T02:10:00+00:00")).toISOString(),
  to: new Date(Date.parse("2026-08-24T03:40:00+00:00")).toISOString(),
};

const iso = (epochMs: number): string => new Date(epochMs).toISOString().replace(/\.\d+Z$/, "+00:00");

/** One phase's night story, piecewise linear with a deterministic wiggle. */
interface PhaseProfile {
  /** Evening discharge plateau W (positive), night charge W (negative). */
  dischargeW: number;
  chargeW: number;
  /** Evening SOC start %, floor %, and the night-charge ceiling. */
  socStart: number;
  socFloor: number;
  loadW: number;
}

const PHASES: readonly { id: string; profile: PhaseProfile }[] = [
  { id: "lhs", profile: { dischargeW: 820, chargeW: -2500, socStart: 97, socFloor: 33, loadW: 610 } },
  { id: "mid", profile: { dischargeW: 1480, chargeW: -2500, socStart: 92, socFloor: 24, loadW: 940 } },
  { id: "rhs", profile: { dischargeW: 760, chargeW: -2500, socStart: 99, socFloor: 41, loadW: 380 } },
];

/** Where in the day each phase of the story sits (fractions of the window). */
const EVENING_DISCHARGE_START = Date.parse("2026-08-23T17:30:00+00:00");
const EVENING_END = Date.parse("2026-08-23T21:40:00+00:00");
const CHARGE_START = Date.parse("2026-08-24T00:02:00+00:00");
const CHARGE_END = Date.parse("2026-08-24T04:30:00+00:00");

const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;
const clamp01 = (t: number): number => Math.min(1, Math.max(0, t));

function batteryWattsAt(profile: PhaseProfile, at: number, index: number): number {
  const wiggle = Math.sin(index * 0.7) * 25;
  if (at < EVENING_DISCHARGE_START || (at >= EVENING_END && at < CHARGE_START)) {
    return 0;
  }
  if (at < EVENING_END) {
    return lerp(profile.dischargeW * 0.9, profile.dischargeW * 0.45, clamp01((at - EVENING_DISCHARGE_START) / (EVENING_END - EVENING_DISCHARGE_START))) + wiggle;
  }
  if (at < CHARGE_END) {
    // The night charge: pacing onto the cap, easing off as the ceiling nears.
    const t = clamp01((at - CHARGE_START) / (CHARGE_END - CHARGE_START));
    return profile.chargeW * (t < 0.85 ? 1 : 1 - (t - 0.85) / 0.15 * 0.8) + wiggle * 0.4;
  }
  return 0;
}

function socAt(profile: PhaseProfile, at: number): number {
  if (at < EVENING_DISCHARGE_START) {
    return profile.socStart;
  }
  if (at < EVENING_END) {
    return lerp(profile.socStart, profile.socFloor, clamp01((at - EVENING_DISCHARGE_START) / (EVENING_END - EVENING_DISCHARGE_START)));
  }
  if (at < CHARGE_START) {
    return profile.socFloor;
  }
  if (at < CHARGE_END) {
    return lerp(profile.socFloor, 100, clamp01((at - CHARGE_START) / (CHARGE_END - CHARGE_START)));
  }
  return 100;
}

/** One unit's full-resolution series map, coherent with its profile. */
function phaseSeries(profile: PhaseProfile): Record<string, WireHistorySeries> {
  const battery: { t: string; v: number }[] = [];
  const soc: { t: string; v: number }[] = [];
  const grid: { t: string; v: number }[] = [];
  const load: { t: string; v: number }[] = [];
  const tempMin: { t: string; v: number }[] = [];
  const tempMax: { t: string; v: number }[] = [];
  const cellMin: { t: string; v: number }[] = [];
  const cellMax: { t: string; v: number }[] = [];
  const spread: { t: string; v: number }[] = [];
  let index = 0;
  for (let at = FROM; at <= TO; at += STEP_S * 1000, index += 1) {
    if (Date.parse(GAP.from) <= at && at < Date.parse(GAP.to)) {
      continue; // the controller was down: no rows, never bridged
    }
    const watts = batteryWattsAt(profile, at, index);
    const socNow = socAt(profile, at);
    const house = profile.loadW + Math.sin(index * 0.31) * 45;
    const t = iso(at);
    battery.push({ t, v: Math.round(watts) });
    soc.push({ t, v: Math.round(socNow * 10) / 10 });
    grid.push({ t, v: Math.round(watts + house) });
    load.push({ t, v: Math.round(house) });
    const warmth = 21 + Math.abs(watts) / 500 + Math.sin(index * 0.05);
    tempMin.push({ t, v: Math.round(warmth * 10) / 10 });
    tempMax.push({ t, v: Math.round((warmth + 3.4) * 10) / 10 });
    const sag = socNow < 30 ? 0.04 : 0;
    cellMin.push({ t, v: Math.round((3.32 - sag + Math.sin(index * 0.11) * 0.004) * 1000) / 1000 });
    cellMax.push({ t, v: Math.round((3.36 - sag + Math.cos(index * 0.09) * 0.003) * 1000) / 1000 });
    spread.push({ t, v: Math.round(28 + (socNow < 30 ? 22 : 6) + Math.sin(index * 0.13) * 3) });
  }
  return {
    soc_pct: historySeries(soc),
    bms_soc_pct: historySeries(soc),
    battery_watts: historySeries(battery),
    grid_power_w: historySeries(grid),
    load_power_w: historySeries(load),
    temperature_min_c: historySeries(tempMin),
    temperature_max_c: historySeries(tempMax),
    cell_min_v: historySeries(cellMin),
    cell_max_v: historySeries(cellMax),
    cell_spread_mv: historySeries(spread),
  };
}

/** The fleet's summed flows: the true sum of the three phases' rows. */
function fleetSeries(): Record<string, WireHistorySeries> {
  const perPhase = PHASES.map(({ profile }) => phaseSeries(profile));
  const battery: { t: string; v: number }[] = [];
  const grid: { t: string; v: number }[] = [];
  const load: { t: string; v: number }[] = [];
  const stamps = perPhase[0]!.battery_watts.points.map((point) => point.t);
  for (const t of stamps) {
    const sumOf = (field: string): number =>
      perPhase.reduce(
        (sum, series) => sum + (series[field]!.points.find((point) => point.t === t)?.v ?? 0),
        0,
      );
    battery.push({ t, v: sumOf("battery_watts") });
    grid.push({ t, v: sumOf("grid_power_w") });
    load.push({ t, v: sumOf("load_power_w") });
  }
  return {
    battery_watts: historySeries(battery),
    grid_power_w: historySeries(grid),
    load_power_w: historySeries(load),
  };
}

/** The recorded-night world: full resolution, a charge, a gap, a stale stretch. */
function recordedNight(): Record<string, unknown> {
  return historyBody({
    resolution: "full",
    from: iso(FROM),
    to: iso(TO),
    units: Object.fromEntries(
      PHASES.map(({ id, profile }, position) => [
        id,
        historyUnit({
          first_sample_at: iso(FROM),
          last_sample_at: iso(TO),
          sample_count: 430,
          quality_worst: position === 1 ? "stale" : "good",
          gaps: [GAP],
          series: phaseSeries(profile),
          lifecycle_changes: [{ t: iso(FROM), v: "disarmed" }],
          health_state_changes: [
            { t: iso(FROM), v: "healthy" },
            { t: GAP.to, v: "self_healing" },
            { t: iso(Date.parse("2026-08-24T03:52:00+00:00")), v: "healthy" },
          ],
          commanded_changes:
            position === 1
              ? [
                  { t: iso(FROM), source: null, direction: null, watts: null },
                  { t: iso(CHARGE_START), source: "night_adviser", direction: "charge", watts: 2500 },
                  { t: iso(CHARGE_END), source: null, direction: null, watts: null },
                ]
              : [{ t: iso(FROM), source: null, direction: null, watts: null }],
        }),
      ]),
    ),
    fleet: { series: fleetSeries(), gaps: [GAP] },
  }) as unknown as Record<string, unknown>;
}

/** The 30-day world: hourly rollups, bands, and the early-hours absence. */
function hourlyMonth(): Record<string, unknown> {
  const hours: { t: string; v: number; min: number | null; max: number | null; n: number }[] = [];
  for (let at = FROM - 13 * 86_400_000; at <= TO; at += 3_600_000) {
    const hourOfDay = new Date(at).getUTCHours();
    const charging = hourOfDay >= 0 && hourOfDay < 5;
    const mean = charging ? -2100 : hourOfDay >= 17 ? 900 : 0;
    hours.push({
      t: iso(at),
      v: mean,
      min: mean === 0 ? null : mean - 380,
      max: mean === 0 ? null : mean + 320,
      n: at < FROM - 12 * 86_400_000 ? 0 : charging || hourOfDay >= 17 ? 112 : 118,
    });
  }
  const series: Record<string, WireHistorySeries> = {
    battery_watts: {
      points: hours,
      sample_count: hours.reduce((sum, hour) => sum + hour.n, 0),
      window_min: -2503,
      window_min_at: iso(FROM - 2 * 86_400_000 + 3_600_000),
      window_max: 2_510,
      window_max_at: iso(FROM - 3 * 86_400_000 + 20 * 3_600_000),
    },
  };
  return historyBody({
    resolution: "hourly",
    from: iso(FROM - 13 * 86_400_000),
    to: iso(TO),
    units: {
      mid: historyUnit({
        first_sample_at: iso(FROM - 13 * 86_400_000),
        last_sample_at: iso(TO),
        sample_count: 8_361,
        quality_worst: "good",
        series,
      }),
    },
    fleet: {
      series: {
        battery_watts: {
          points: hours.map((hour) => ({ ...hour, v: hour.v * 3, min: hour.min === null ? null : hour.min * 3, max: hour.max === null ? null : hour.max * 3 })),
          sample_count: 8_361,
          window_min: -7_503,
          window_min_at: null,
          window_max: 7_510,
          window_max_at: null,
        },
      },
      gaps: [],
    },
  }) as unknown as Record<string, unknown>;
}

/** The snapshot world every history state rides: a quiet disarmed fleet. */
function historyWorld(lastSampleAt: string | null): () => ReturnType<typeof snapshot> {
  return () =>
    withHistoryState(
      snapshot(
        PHASES.map(({ id }) =>
          unitSnapshot({ unit_id: id, lifecycle: "disarmed", telemetry: null, measured_watts: null }),
        ),
        { captured_at: iso(TO) },
      ),
      historyState({
        last_sample_at:
          lastSampleAt === null
            ? { lhs: null, mid: null, rhs: null }
            : { lhs: lastSampleAt, mid: lastSampleAt, rhs: lastSampleAt },
      }),
    );
}

export const HISTORY_STATES: readonly ShotStateDefinition[] = [
  {
    id: "recorded-night-full-resolution",
    caption:
      "The whole site's recorded night at full resolution: the evening discharge, the 00:02 night charge onto the 2,500 W cap, the controller-down gap 02:10–03:40 left visibly empty, and the resolution badge saying 30 s samples.",
    world: historyWorld(iso(TO)),
    history: recordedNight,
  },
  {
    id: "hourly-rollup-month",
    caption:
      "The 30-day window the data horizon honestly serves as hourly rollups — the badge says so, and every summary row carries the hours' own min–max bands.",
    world: historyWorld(iso(TO)),
    history: hourlyMonth,
  },
  {
    id: "fresh-database-empty-window",
    caption:
      "The morning after commissioning: a young database whose 7-day/30-day windows are legitimately empty — the view says recording is active and offers the shorter range, never fake data.",
    world: historyWorld(iso(TO)),
    history: () =>
      historyBody({
        resolution: "hourly",
        units: Object.fromEntries(PHASES.map(({ id }) => [id, historyUnit()])),
      }) as unknown as Record<string, unknown>,
  },
  {
    id: "not-commissioned",
    caption:
      "A deployment without the plant_history config block: the route's honest 409, the commissioning steps in plain words, and a Check again — never a dead link.",
    world: () =>
      // No recording hint on this wire: the block is absent because the
      // historian is not composed.
      snapshot(
        PHASES.map(({ id }) =>
          unitSnapshot({ unit_id: id, lifecycle: "disarmed", telemetry: null, measured_watts: null }),
        ),
        { captured_at: iso(TO) },
      ),
    historyRefusal: {
      status: 409,
      body: {
        code: "plant_history_not_commissioned",
        message: "Plant history is not commissioned in this deployment's config",
        details: null,
        request_id: "req-shot-1",
      },
    },
  },
];
