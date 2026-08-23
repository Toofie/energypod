/**
 * THE BATTERIES VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the commissioned three-phase fleet —
 * two cards healthy and load-serving, one past the cell-imbalance early-
 * warning line with its warning rendered, plus the audit page the Events tab
 * reads (the seeded detail route still refuses, so opening a unit shows its
 * honest error state rather than a fabricated projection).
 */
import {
  auditEvent,
  snapshot,
  telemetrySummary,
  unitSnapshot,
  withHealth,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

function fleetWorld() {
  const units = [
    unitSnapshot({
      unit_id: "lhs",
      lifecycle: "active",
      quality: "good",
      telemetry_age_s: 2,
      requested_power: { direction: "discharge", watts: 3000 },
      authorized_power: { direction: "discharge", watts: 1000 },
      measured_watts: 984,
      telemetry: telemetrySummary({
        soc_pct: 64,
        battery_watts: 984,
        grid_power_w: -610,
        load_power_w: 610,
        cell_spread_mv: 18,
        active_warnings: [],
      }),
    }),
    unitSnapshot({
      unit_id: "mid",
      lifecycle: "active",
      quality: "degraded",
      telemetry_age_s: 44,
      requested_power: { direction: "discharge", watts: 3000 },
      authorized_power: { direction: "discharge", watts: 1000 },
      measured_watts: 1002,
      telemetry: telemetrySummary({
        soc_pct: 71,
        battery_watts: 1002,
        grid_power_w: -940,
        load_power_w: 940,
        // Past the 50 mV early-warning line: the amber caution renders.
        cell_spread_mv: 78,
        active_warnings: ["PCS_Warning0_1"],
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
  const withBadge = {
    ...snapshot(units, { captured_at: "2026-08-26T14:03:00+10:00", snapshot_sequence: 4106 }),
    units: units.map((unit, index) =>
      index === 1 ? withHealth(unit, { state: "self_healing", reasons: ["gateway_unreachable"] }) : unit,
    ),
  };
  return withBadge;
}

/** The Events tab's durable history: one page of the audit trail's own shapes. */
function fleetAudit() {
  return [
    auditEvent({
      sequence: 4102,
      event_type: "control_decision",
      result: "clamped",
      reason_codes: ["power_clamped"],
      requested_active_w: -5000,
      authorized_active_w: -4900,
      requested_watts_by_unit: { lhs: 2500, mid: 2500 },
      authorized_watts_by_unit: { lhs: 2500, mid: 2400 },
      occurred_at: "2026-08-26T13:58:00+10:00",
    }),
    auditEvent({
      sequence: 4100,
      event_type: "intent_accepted",
      result: "accepted",
      requested_active_w: -5000,
      authorized_active_w: -5000,
      occurred_at: "2026-08-26T13:52:00+10:00",
    }),
    auditEvent({
      sequence: 4090,
      event_type: "unit_armed",
      unit_id: "lhs",
      result: "armed",
      occurred_at: "2026-08-26T13:40:00+10:00",
    }),
  ];
}

export const BATTERIES_STATES: readonly ShotStateDefinition[] = [
  {
    id: "three-phase-fleet",
    caption:
      "The commissioned three-phase fleet: two cards load-serving, one degraded with the 78 mV cell-imbalance early-warning line and the self-healing badge.",
    world: fleetWorld,
    audit: fleetAudit,
  },
];
