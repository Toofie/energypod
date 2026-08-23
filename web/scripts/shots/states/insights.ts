/**
 * THE INSIGHTS VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the energy ledger's own day records —
 * four completed days plus the partial that follows a midday controller
 * restart — with the grid counter roles pinned by the A-1 cross-check, and
 * the quiet disarmed snapshot world the view rides.
 */
import {
  energyDayRecord,
  getEnergyDaysOk,
  snapshot,
  unitSnapshot,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

function insightsWorld() {
  return snapshot(
    ["lhs", "mid", "rhs"].map((unitId) =>
      unitSnapshot({ unit_id: unitId, lifecycle: "disarmed", telemetry: null, measured_watts: null }),
    ),
    { captured_at: "2026-08-26T14:03:00+10:00" },
  );
}

/** Four completed days plus one partial, oldest first as the route serves. */
function ledgerDays() {
  return getEnergyDaysOk({
    grid_counter_roles: "vendor_labels",
    days: [
      energyDayRecord({
        date: "2026-08-22",
        kind: "complete",
        units: {
          mid: { grid_import_kwh: 4.1, grid_export_kwh: 12.3, battery_charged_kwh: 7.4, battery_discharged_kwh: 3.1, load_kwh: 6.0, charged_from_surplus_kwh: 6.6, coverage_pct: 99.1, metric_flags: [] },
          rhs: { grid_import_kwh: 3.0, grid_export_kwh: 9.1, battery_charged_kwh: 5.2, battery_discharged_kwh: 2.4, load_kwh: 4.9, charged_from_surplus_kwh: 0, coverage_pct: 100, metric_flags: [] },
          lhs: { grid_import_kwh: 5.5, grid_export_kwh: 7.7, battery_charged_kwh: 3.9, battery_discharged_kwh: 3.3, load_kwh: 7.1, charged_from_surplus_kwh: 0, coverage_pct: 97.8, metric_flags: [] },
        },
      }),
      energyDayRecord({
        date: "2026-08-23",
        kind: "complete",
        units: {
          mid: { grid_import_kwh: 3.8, grid_export_kwh: 13.9, battery_charged_kwh: 8.2, battery_discharged_kwh: 2.9, load_kwh: 5.7, charged_from_surplus_kwh: 7.1, coverage_pct: 99.6, metric_flags: [] },
          rhs: { grid_import_kwh: 2.9, grid_export_kwh: 10.4, battery_charged_kwh: 5.8, battery_discharged_kwh: 2.2, load_kwh: 4.6, charged_from_surplus_kwh: 0, coverage_pct: 100, metric_flags: [] },
          lhs: { grid_import_kwh: 5.1, grid_export_kwh: 8.2, battery_charged_kwh: 4.1, battery_discharged_kwh: 3.0, load_kwh: 6.9, charged_from_surplus_kwh: 0, coverage_pct: 98.4, metric_flags: [] },
        },
      }),
      energyDayRecord({
        date: "2026-08-24",
        kind: "complete",
        units: {
          mid: { grid_import_kwh: 4.0, grid_export_kwh: 11.1, battery_charged_kwh: 6.9, battery_discharged_kwh: 3.4, load_kwh: 6.1, charged_from_surplus_kwh: 5.9, coverage_pct: 99.2, metric_flags: [] },
          rhs: { grid_import_kwh: 3.2, grid_export_kwh: 8.8, battery_charged_kwh: 4.9, battery_discharged_kwh: 2.6, load_kwh: 5.0, charged_from_surplus_kwh: 0, coverage_pct: 99.9, metric_flags: [] },
          lhs: { grid_import_kwh: 5.4, grid_export_kwh: 6.9, battery_charged_kwh: 3.6, battery_discharged_kwh: 3.6, load_kwh: 7.3, charged_from_surplus_kwh: 0, coverage_pct: 61.2, metric_flags: ["grid_counters_reset"] },
        },
      }),
      energyDayRecord({
        date: "2026-08-25",
        kind: "complete",
        units: {
          mid: { grid_import_kwh: 3.6, grid_export_kwh: 14.6, battery_charged_kwh: 8.8, battery_discharged_kwh: 2.7, load_kwh: 5.5, charged_from_surplus_kwh: 7.4, coverage_pct: 99.8, metric_flags: [] },
          rhs: { grid_import_kwh: 2.7, grid_export_kwh: 11.0, battery_charged_kwh: 6.1, battery_discharged_kwh: 2.0, load_kwh: 4.4, charged_from_surplus_kwh: 0, coverage_pct: 100, metric_flags: [] },
          lhs: { grid_import_kwh: 4.9, grid_export_kwh: 8.9, battery_charged_kwh: 4.4, battery_discharged_kwh: 2.8, load_kwh: 6.7, charged_from_surplus_kwh: 0, coverage_pct: 98.9, metric_flags: [] },
        },
      }),
      energyDayRecord({
        date: "2026-08-26",
        kind: "partial",
        units: {
          mid: { grid_import_kwh: 1.2, grid_export_kwh: 6.8, battery_charged_kwh: 3.4, battery_discharged_kwh: 0.7, load_kwh: 5.1, charged_from_surplus_kwh: 3.1, coverage_pct: 99.4, metric_flags: [] },
          rhs: { grid_import_kwh: 2.1, grid_export_kwh: 4.2, battery_charged_kwh: 2.0, battery_discharged_kwh: 1.6, load_kwh: 4.4, charged_from_surplus_kwh: 0, coverage_pct: 100, metric_flags: [] },
          lhs: { grid_import_kwh: 5.1, grid_export_kwh: 1.9, battery_charged_kwh: 0.8, battery_discharged_kwh: 1.8, load_kwh: 5.2, charged_from_surplus_kwh: 0, coverage_pct: 98.7, metric_flags: [] },
        },
      }),
    ],
  });
}

export const INSIGHTS_STATES: readonly ShotStateDefinition[] = [
  {
    id: "week-ledger",
    caption:
      "The week's ledger: four completed days and the partial day after a counter reset (its flag rendered), the roles note naming the pinned counter labels, oldest first with a Load more beneath.",
    world: insightsWorld,
    energyDays: ledgerDays,
  },
];
