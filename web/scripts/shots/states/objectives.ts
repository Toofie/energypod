/**
 * THE OBJECTIVES VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the detector's own illustrative night
 * window — mid held by a foreign writer across a sustained charge, rhs
 * quietly on the sanctioned nightly charge, lhs idle with no samples — as
 * the observed-objectives route serves it.
 */
import {
  getObservedObjectivesOk,
  snapshot,
  unitSnapshot,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

function objectivesWorld() {
  return snapshot(
    ["lhs", "mid", "rhs"].map((unitId) =>
      unitSnapshot({ unit_id: unitId, lifecycle: "disarmed", telemetry: null, measured_watts: null }),
    ),
    { captured_at: "2026-08-24T06:00:00+10:00" },
  );
}

export const OBJECTIVES_STATES: readonly ShotStateDefinition[] = [
  {
    id: "foreign-night-window",
    caption:
      "The detector's night window: mid's row tinted foreign at a sustained -2,400 W charge, rhs on the sanctioned nightly charge, lhs with no recorded samples — the honesty note always visible.",
    world: objectivesWorld,
    objectives: () => getObservedObjectivesOk({}),
  },
];
