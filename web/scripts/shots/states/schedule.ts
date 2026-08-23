/**
 * THE SCHEDULE VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the day-only YIELD posture with a
 * published two-entry plan — the v1 editor's own illustrative day window and
 * the trimmed evening discharge — plus the quiet armed snapshot world the
 * view rides (the units the editor offers).
 */
import {
  getScheduleOk,
  scheduleEntry,
  scheduleNextAction,
  scheduleState,
  snapshot,
  unitSnapshot,
  withScheduleState,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

function scheduleWorld() {
  // The snapshot's own projection proves the feature composed — the view's
  // commissioned check keys on it before the plan read renders.
  return withScheduleState(
    snapshot(
      ["lhs", "mid", "rhs"].map((unitId) =>
        unitSnapshot({
          unit_id: unitId,
          lifecycle: "armed_idle",
          quality: "good",
          telemetry_age_s: 3,
          measured_watts: 0,
          telemetry: null,
        }),
      ),
      { captured_at: "2026-08-26T11:20:00+10:00" },
    ),
    scheduleState({ active: false, entry_id: null, held_intent_id: null, ends_at: null, ends_in_s: null }),
  );
}

function publishedPlan() {
  return getScheduleOk({
    acknowledged_night_windows: true,
    plan: {
      version: 4,
      timezone: "Australia/Brisbane",
      entries: [
        scheduleEntry({
          entry_id: "Day charge",
          start_local: "06:30",
          end_local: "18:00",
          action: "charge",
        }),
        scheduleEntry({
          entry_id: "Evening top-up",
          days: ["mon", "tue", "wed", "thu", "fri"],
          start_local: "17:30",
          end_local: "19:00",
          action: "discharge",
          watts_by_unit: { lhs: 800, mid: 800, rhs: 800 },
        }),
      ],
    },
    next_action: scheduleNextAction({
      entry_id: "Evening top-up",
      days: ["mon", "tue", "wed", "thu", "fri"],
      start_local: "17:30",
      end_local: "19:00",
      action: "discharge",
      watts_by_unit: { lhs: 800, mid: 800, rhs: 800 },
      starts_at: "2026-08-26T17:30:00+10:00",
      starts_in_s: 4_200,
    }),
  });
}

export const SCHEDULE_STATES: readonly ShotStateDefinition[] = [
  {
    id: "published-day-plan",
    caption:
      "The published day plan: two entries under the day-only yield posture, the timezone line, the evening discharge next in 4,200 s, and the editor's publish row beneath.",
    world: scheduleWorld,
    schedule: publishedPlan,
  },
];
