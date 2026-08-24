/**
 * THE BATTERIES VIEW'S SCREENSHOT STATES (dev tooling). A complete seeded
 * world from the shared wire fixtures: the commissioned three-phase fleet —
 * two cards healthy and load-serving, one past the cell-imbalance early-
 * warning line with its warning rendered, plus the audit page the Events tab
 * reads (the seeded detail route still refuses, so opening a unit shows its
 * honest error state rather than a fabricated projection).
 *
 * The pod-parking round adds the parking family (DESIGN_POD_PARKING §7/§8,
 * PENDING wire shapes from web/src/test/wire.ts): a commissioned site — every
 * unit carries a `park_state` — with rhs standing by under a live lease, the
 * expired-lease alert with the honest write_unverified / foreign_rewrite
 * sub-state words, the guarded park dialog (typed into, so the fixed
 * not-isolation sentence and the enabled confirm are on camera), the
 * post-resume checklist (played against a seeded resume 200 carrying a
 * latched stop), and the wedge-signature advisory card with the delivery-bias
 * evidence (a seeded unit-detail read).
 */
import {
  auditEvent,
  deliveryBias,
  parkOk,
  parkState,
  recoveryAdvisory,
  resumeChecklist,
  resumeOk,
  snapshot,
  telemetrySummary,
  unitDetail,
  unitSnapshot,
  withHealth,
  withParkState,
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

/**
 * The commissioned parking world: every unit carries a `park_state` (the
 * absent key would be the not-commissioned feature detection). rhs stands by
 * under a live operator lease (built with slack so the floored H:MM is
 * stable across the capture delay); the other two hold the un-parked
 * projection and the Park affordance.
 */
function parkedWorld(rhsPark: ReturnType<typeof parkState>, rhsTelemetryExtra = {}) {
  const base = fleetWorld();
  const rhs = base.units.find((unit) => unit.unit_id === "rhs")!;
  const lhs = base.units.find((unit) => unit.unit_id === "lhs")!;
  const mid = base.units.find((unit) => unit.unit_id === "mid")!;
  return {
    ...base,
    units: [
      withParkState(lhs, parkState({ parked: false })),
      withParkState(mid, parkState({ parked: false })),
      withParkState(
        {
          ...rhs,
          lifecycle: "disarmed",
          telemetry: telemetrySummary({
            soc_pct: 64,
            battery_watts: 0,
            grid_power_w: -380,
            load_power_w: 380,
            cell_spread_mv: 9,
            // 166 V observed while parked — the chip tooltip's own figure —
            // and the mode word readback (1 = Standby).
            pack_voltage_v: 166.4,
            debug_mode_w: 1,
            active_warnings: [],
            ...rhsTelemetryExtra,
          }),
        },
        rhsPark,
      ),
    ],
  };
}

/** A live lease ~2 h out, with slack so the floored minute holds through capture. */
function liveLease(): ReturnType<typeof parkState> {
  return parkState({
    reason: "evening standby",
    lease_expires_at: new Date(Date.now() + (2 * 3600 + 30) * 1000).toISOString(),
  });
}

/** The resume 200 the checklist state plays against: one latched stop named. */
function resumeWithStop(): Record<string, unknown> {
  return resumeOk({
    checklist: resumeChecklist({
      comms_age_s: 1.8,
      soc_drift_pct: -0.6,
      soc_pct_at_park: 64,
      measured_watts_now: 0,
      latched_stops: ["stop-7"],
    }),
  });
}

export const BATTERIES_STATES: readonly ShotStateDefinition[] = [
  {
    id: "three-phase-fleet",
    caption:
      "The commissioned three-phase fleet: two cards load-serving, one degraded with the 78 mV cell-imbalance early-warning line and the self-healing badge.",
    world: fleetWorld,
    audit: fleetAudit,
  },
  {
    id: "parked-standby",
    caption:
      "rhs parked under a live operator lease: the Parked chip (tooltip names mode word 1 and the 166.4 V pack), the not-isolation banner with the 2:00 countdown, and the Resume affordance — lhs and mid hold the Park affordance.",
    world: () => parkedWorld(liveLease()),
    audit: fleetAudit,
  },
  {
    id: "parked-lease-expired",
    caption:
      "The lease's own alarm: rhs's banner promotes to the alert wording (lease expired — Resume required), and mid's write_unverified / foreign_rewrite sub-states get their own honest words.",
    world: () => {
      const world = parkedWorld(parkState({ expired: true, reason: "evening standby" }));
      return {
        ...world,
        units: world.units.map((unit) =>
          unit.unit_id === "mid"
            ? withParkState(
                {
                  ...unit,
                  // An honestly parked mid: stood down, not load-serving.
                  lifecycle: "disarmed",
                  requested_power: { direction: "idle", watts: 0 },
                  authorized_power: null,
                  measured_watts: 0,
                  telemetry: telemetrySummary({
                    soc_pct: 71,
                    battery_watts: 0,
                    grid_power_w: -940,
                    load_power_w: 940,
                    cell_spread_mv: 78,
                    active_warnings: ["PCS_Warning0_1"],
                  }),
                },
                parkState({
                  parked: true,
                  origin: "operator",
                  write_unverified: true,
                  foreign_rewrite: true,
                  reason: "grid work",
                  lease_expires_at: new Date(Date.now() + (3600 + 30) * 1000).toISOString(),
                }),
              )
            : unit,
        ),
      };
    },
    audit: fleetAudit,
  },
  {
    id: "park-dialog",
    caption:
      "The guarded park dialog, typed into: the fixed not-isolation sentence above the enabled confirm, the required reason, and the lease select bounded by the site's 4 h budget.",
    world: () => parkedWorld(parkState({ parked: false })),
    audit: fleetAudit,
    park: { park: () => parkOk() },
    interact: [
      { click: { role: "button", name: "Park rhs" } },
      { fill: { label: "Reason (required)", value: "evening standby while the grid work runs" } },
      { fill: { label: "Type rhs to enable park", value: "rhs" } },
      { pauseMs: 250 },
    ],
  },
  {
    id: "resume-checklist",
    caption:
      "After the resume: the after-park checklist rendered inline — comms age, charge drift since park, faults note, and the pinned latched-stops line (remains stopped by stop-7 — acknowledge separately).",
    world: () => parkedWorld(liveLease()),
    audit: fleetAudit,
    park: { resume: () => resumeWithStop() },
    interact: [
      // Open the resume dialog from the card, then confirm INSIDE it (the
      // card keeps its own same-named control while the dialog is open).
      { click: { role: "button", name: "Resume rhs", exact: true } },
      { click: { role: "button", name: "Resume rhs", inDialog: true } },
      { pauseMs: 500 },
    ],
  },
  {
    id: "wedge-advisory",
    caption:
      "The wedge-signature advisory card on the unit detail: the disarm → park → resume walkthrough with the operator-at-the-pod rule, beside the delivery-bias evidence readout (evidence-only, never a warning).",
    world: () => {
      const world = parkedWorld(liveLease());
      return {
        ...world,
        units: world.units.map((unit) =>
          unit.unit_id === "mid"
            ? withHealth(unit, {
                state: "actuation_incoherent",
                reasons: ["authorized_not_actuating", "echo_matches_write"],
              })
            : unit,
        ),
      };
    },
    audit: fleetAudit,
    unitDetail: (unitId) =>
      unitId === "mid"
        ? (unitDetail("mid", {
            device_identity: "BEP0005KXX11B10500151 (RTU 0x2C225097)",
            recovery_advisory: recoveryAdvisory({ echo_classifications: ["echo_matches_write"] }),
            delivery_bias: deliveryBias(),
          }) as unknown as Record<string, unknown>)
        : undefined,
    interact: [
      // The card title (exact: the Park action is also named after the unit).
      { click: { role: "button", name: "mid", exact: true } },
      { pauseMs: 400 },
    ],
  },
];
