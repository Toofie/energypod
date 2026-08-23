/**
 * THE ACTIVITY VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the audit trail's own event shapes —
 * decisions with watt figures, arming, a stop and its acknowledgement, the
 * schedule publish and its opened window — newest first, plus the quiet
 * disarmed snapshot world every activity state rides.
 */
import {
  auditEvent,
  snapshot,
  unitSnapshot,
} from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

/** The snapshot world the timeline rides: a quiet disarmed fleet. */
function activityWorld() {
  return snapshot(
    ["lhs", "mid", "rhs"].map((unitId) =>
      unitSnapshot({ unit_id: unitId, lifecycle: "disarmed", telemetry: null, measured_watts: null }),
    ),
    { captured_at: "2026-08-27T06:12:00+10:00" },
  );
}

/** One evening's durable history across the trail's own event shapes. */
function eveningAudit() {
  return [
    auditEvent({
      sequence: 5410,
      event_type: "schedule_window_opened",
      occurred_at: "2026-08-27T00:02:00+10:00",
    }),
    auditEvent({
      sequence: 5408,
      event_type: "schedule_replaced",
      occurred_at: "2026-08-26T21:44:00+10:00",
    }),
    auditEvent({
      sequence: 5390,
      event_type: "control_decision",
      result: "clamped",
      reason_codes: ["power_clamped"],
      requested_active_w: -5000,
      authorized_active_w: -4900,
      occurred_at: "2026-08-26T19:58:00+10:00",
    }),
    auditEvent({
      sequence: 5380,
      event_type: "intent_accepted",
      result: "accepted",
      requested_active_w: -5000,
      authorized_active_w: -5000,
      occurred_at: "2026-08-26T17:30:00+10:00",
    }),
    auditEvent({
      sequence: 5360,
      event_type: "unit_armed",
      unit_id: "lhs",
      result: "armed",
      occurred_at: "2026-08-26T17:28:00+10:00",
    }),
    auditEvent({
      sequence: 5350,
      event_type: "unit_armed",
      unit_id: "mid",
      result: "armed",
      occurred_at: "2026-08-26T17:28:00+10:00",
    }),
    auditEvent({
      sequence: 5320,
      event_type: "stop_acknowledged",
      result: "acknowledged",
      intent_id: "stop-7",
      occurred_at: "2026-08-26T09:12:00+10:00",
    }),
    auditEvent({
      sequence: 5318,
      event_type: "emergency_stop",
      result: "latched",
      intent_id: "stop-7",
      reason_codes: ["operator_requested"],
      occurred_at: "2026-08-26T09:11:00+10:00",
    }),
  ];
}

export const ACTIVITY_STATES: readonly ShotStateDefinition[] = [
  {
    id: "evening-history",
    caption:
      "One evening's audit history: an accepted dispatch, the clamped decision beneath it, arming rows, a latched stop with its acknowledgement, and the schedule publish and opened window — newest first with ages.",
    world: activityWorld,
    audit: eveningAudit,
  },
];
