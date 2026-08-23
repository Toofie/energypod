/**
 * THE NOW (CONTROL) VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: a cold load mid-intent — the snapshot
 * shows batteries carrying a request whose acceptance this session never saw,
 * so the honest fleet-figure fallback row renders beside the controls, with
 * the shared tracker's snapshot `intent` block (feature-detected) supplying
 * the exact per-battery figures.
 */
import {
  snapshot,
  telemetrySummary,
  unitSnapshot,
  withSnapshotIntent,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

function coldLoadMidIntent() {
  const units = [
    unitSnapshot({
      unit_id: "lhs",
      lifecycle: "active",
      quality: "good",
      telemetry_age_s: 2,
      requested_power: { direction: "charge", watts: 5000 },
      authorized_power: { direction: "charge", watts: 2500 },
      measured_watts: -2488,
      telemetry: telemetrySummary({ soc_pct: 71, battery_watts: -2488 }),
    }),
    unitSnapshot({
      unit_id: "mid",
      lifecycle: "active",
      quality: "good",
      telemetry_age_s: 2,
      requested_power: { direction: "charge", watts: 5000 },
      authorized_power: { direction: "charge", watts: 2400 },
      measured_watts: -2396,
      telemetry: telemetrySummary({ soc_pct: 88, battery_watts: -2396 }),
    }),
    unitSnapshot({
      unit_id: "rhs",
      lifecycle: "armed_idle",
      quality: "good",
      telemetry_age_s: 4,
      measured_watts: 0,
      telemetry: telemetrySummary({ soc_pct: 98, battery_watts: 0 }),
    }),
  ];
  return withSnapshotIntent(
    snapshot(units, { captured_at: "2026-08-27T01:44:00+10:00", snapshot_sequence: 5402 }),
    {
      requested_watts_by_unit: { lhs: 2500, mid: 2500 },
      authorized_watts_by_unit: { lhs: 2500, mid: 2400 },
      directions_by_unit: { lhs: "charge", mid: "charge" },
    },
  );
}

export const NOW_STATES: readonly ShotStateDefinition[] = [
  {
    id: "cold-load-mid-intent",
    caption:
      "A cold load mid-intent: the honest fallback row with the intent block's exact per-battery figures, one battery named as headroom-clamped, and the full control row beneath.",
    world: coldLoadMidIntent,
  },
];
