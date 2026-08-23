/**
 * THE FLOW VIEW'S SCREENSHOT STATES (dev tooling; the design-polish loop's
 * scenery). Every state is a complete snapshot world built from the shared
 * wire fixtures (web/src/test/wire.ts) — real wire shapes, real sign
 * conventions, never a figure invented for the camera:
 *
 * - grid_power_w: NEGATIVE = import, POSITIVE = export, null = no PCS block.
 * - battery_watts: NEGATIVE = charging, POSITIVE = discharging, null = absent.
 * - load_power_w: the pod's measured local load (the house on that phase).
 * - soc_pct / the CT figures are null when absent — never zero-filled.
 *
 * The nine states pin the view's whole state matrix, one per behavior family:
 * the resting site, the fleet charge, batteries pulling opposite ways, the
 * phases splitting import and export, the night window's two cadences (pacing
 * and the demand hold), the solar-surplus adviser commanding, a phase that
 * stopped reporting, and a live request's commanded-vs-measured overlay.
 *
 * Figures are physically coherent per phase (grid ≈ battery + house ± losses)
 * so the arrows' thicknesses and directions tell one believable story per
 * state — the design review judges real pictures, not wire salad.
 */
import {
  adviserState,
  nightChargeState,
  nightUnitState,
  snapshot,
  telemetrySummary,
  unitSnapshot,
  withAdviserState,
  withNightChargeState,
  withSnapshotIntent,
} from "../../../src/test/wire";
import type { WireSnapshot, WireUnitSnapshot } from "../../../src/test/wire";
import type { ShotStateDefinition } from "../types";

/** One phase's measured flow figures for a shot (signs per the wire pins). */
interface PhaseFigures {
  /** Signed grid CT watts: negative import, positive export, null absent. */
  readonly grid: number | null;
  /** Signed battery watts: negative charging, positive discharging, null absent. */
  readonly battery: number | null;
  /** The phase's local load watts. */
  readonly load: number | null;
  /** The same observation's charge reading, clamped 0-100; null absent. */
  readonly soc: number | null;
  /** The unit's lifecycle word (drives the phase note when it needs one). */
  readonly lifecycle?: string;
  /** The wire's genuinely per-unit commanded figure (the overlay's source). */
  readonly authorized?: { direction: string; watts: number } | null;
}

/**
 * One phase as the wire carries it: the shared `unitSnapshot` fixture with the
 * shared `telemetrySummary` — every other telemetry field keeps the fixture's
 * own honest defaults (the live capture's pack figures), exactly as a real
 * poll answer would.
 */
function phase(unitId: string, figures: PhaseFigures): WireUnitSnapshot {
  return unitSnapshot({
    unit_id: unitId,
    lifecycle: figures.lifecycle ?? "active",
    telemetry: telemetrySummary({
      soc_pct: figures.soc,
      battery_watts: figures.battery,
      grid_power_w: figures.grid,
      load_power_w: figures.load,
    }),
    authorized_power: figures.authorized ?? null,
    measured_watts: null,
  });
}

/** The three-phase world, pinned to one fixed capture moment. */
function world(units: readonly WireUnitSnapshot[]): WireSnapshot {
  return snapshot([...units], { captured_at: "2026-08-27T14:03:00+10:00" });
}

/** A phase whose poll served nothing at all — an absent observation. */
function phaseWithNoObservation(unitId: string): WireUnitSnapshot {
  return unitSnapshot({
    unit_id: unitId,
    lifecycle: "active",
    telemetry: null,
    measured_watts: null,
    telemetry_age_s: null,
  });
}

export const FLOW_STATES: readonly ShotStateDefinition[] = [
  {
    id: "all-idle",
    caption:
      "The resting site: every grid CT reads a true 0 W, every battery is idle, no house draw — the honest nothing.",
    world: () =>
      world([
        phase("lhs", { grid: 0, battery: 0, load: 0, soc: 98 }),
        phase("mid", { grid: 0, battery: 0, load: 0, soc: 100 }),
        phase("rhs", { grid: 0, battery: 0, load: 0, soc: 96 }),
      ]),
  },
  {
    id: "fleet-charging",
    caption:
      "All three phases charging at 2,500 W from the grid (a bulk charge with no strategy projection attached).",
    world: () =>
      world([
        phase("lhs", { grid: -2560, battery: -2500, load: 60, soc: 44 }),
        phase("mid", { grid: -2615, battery: -2500, load: 115, soc: 71 }),
        phase("rhs", { grid: -2580, battery: -2500, load: 80, soc: 88 }),
      ]),
  },
  {
    id: "mixed-directions",
    caption:
      "lhs discharging 800 W while mid charges 1,900 W and rhs sits idle — the batteries pulling opposite ways, one grid direction.",
    world: () =>
      world([
        phase("lhs", { grid: -110, battery: 800, load: 620, soc: 62 }),
        phase("mid", { grid: -2340, battery: -1900, load: 440, soc: 35 }),
        phase("rhs", { grid: -510, battery: 0, load: 510, soc: 90 }),
      ]),
  },
  {
    id: "split-import-export",
    caption:
      "The phases pulling different ways on the grid: lhs exporting 1,900 W of surplus while mid and rhs import — the fleet's two-sided arrows.",
    world: () =>
      world([
        phase("lhs", { grid: 1900, battery: 0, load: 85, soc: 100 }),
        phase("mid", { grid: -412, battery: 0, load: 412, soc: 96 }),
        phase("rhs", { grid: -780, battery: 0, load: 780, soc: 100 }),
      ]),
  },
  {
    id: "night-pacing",
    caption:
      "The night window pacing: lhs and mid charging at the 2,500 W cap, rhs sitting out full at 98% — the night projection's own story on top.",
    world: () =>
      withNightChargeState(
        world([
          phase("lhs", { grid: -2560, battery: -2500, load: 60, soc: 71.4 }),
          phase("mid", { grid: -2620, battery: -2500, load: 120, soc: 88 }),
          phase("rhs", { grid: -420, battery: 0, load: 420, soc: 98 }),
        ]),
        nightChargeState({
          phase: "pacing",
          demand_w: 600,
          demand_evidence: "good",
          reason_codes: ["window_open", "on_plan"],
          units: [
            nightUnitState({ unit_id: "lhs", soc_pct: 71.4, phase: "pacing", target_w: 2500 }),
            nightUnitState({ unit_id: "mid", soc_pct: 88, phase: "pacing", target_w: 2500 }),
            nightUnitState({
              unit_id: "rhs",
              soc_pct: 98,
              phase: "skipped_full",
              target_w: 0,
              reason: "at_ceiling",
            }),
          ],
        }),
      ),
  },
  {
    id: "night-demand-hold",
    caption:
      "The night window holding on a demand spike: 1,860 W of house draw, the batteries neither draining nor cycling while the grid meets the house.",
    world: () =>
      withNightChargeState(
        world([
          phase("lhs", { grid: -620, battery: 0, load: 620, soc: 71.4 }),
          phase("mid", { grid: -890, battery: 0, load: 890, soc: 88 }),
          phase("rhs", { grid: -350, battery: 0, load: 350, soc: 98 }),
        ]),
        nightChargeState({
          phase: "holding_on_demand",
          demand_w: 1860,
          demand_evidence: "good",
          reason_codes: ["window_open", "demand_above_threshold"],
          units: [
            nightUnitState({
              unit_id: "lhs",
              soc_pct: 71.4,
              phase: "holding_on_demand",
              target_w: 0,
              reason: "demand_above_threshold",
            }),
            nightUnitState({
              unit_id: "mid",
              soc_pct: 88,
              phase: "holding_on_demand",
              target_w: 0,
              reason: "demand_above_threshold",
            }),
            nightUnitState({
              unit_id: "rhs",
              soc_pct: 98,
              phase: "skipped_full",
              target_w: 0,
              reason: "at_ceiling",
            }),
          ],
        }),
      ),
  },
  {
    id: "excess-active",
    caption:
      "The solar-surplus adviser holding: mid commanded to absorb 1,400 W while the site still exports 1,800 W — the adviser's story and its commanded phase.",
    world: () =>
      withAdviserState(
        world([
          phase("lhs", { grid: 1200, battery: 0, load: 90, soc: 82 }),
          phase("mid", { grid: 1900, battery: -1400, load: 140, soc: 55 }),
          phase("rhs", { grid: -260, battery: 0, load: 260, soc: 90 }),
        ]),
        adviserState({
          active: true,
          hysteresis_state: "holding",
          target_unit_id: "mid",
          commanded_charge_w: 1400,
          eligible_export_charge_w: 1600,
          fleet_export_w: 1800,
          export_evidence: "good",
          charge_cap_w: 2500,
          held_intent_id: "opt-3f9c21",
          last_action: "renew",
          last_tick_at: "2026-08-27T14:02:41+10:00",
          reason_codes: ["export_headroom_available"],
        }),
      ),
  },
  {
    id: "one-phase-not-reporting",
    caption:
      "mid has no observation at all — every figure 'not available', the dashed unknown stubs beside two healthy phases, and the fleet naming its partial scope.",
    world: () =>
      world([
        phase("lhs", { grid: -310, battery: 0, load: 310, soc: 77 }),
        phaseWithNoObservation("mid"),
        phase("rhs", { grid: -330, battery: 0, load: 330, soc: 64 }),
      ]),
  },
  {
    id: "commanded-vs-measured-overlay",
    caption:
      "A live request across all three phases: two delivering close to command and rhs moving against its discharge — the overlay's commanded-vs-delivering rows, including the honest mismatch.",
    world: () =>
      withSnapshotIntent(
        world([
          phase("lhs", {
            grid: 460,
            battery: 780,
            load: 340,
            soc: 62,
            authorized: { direction: "discharge", watts: 800 },
          }),
          phase("mid", {
            grid: -2890,
            battery: -2460,
            load: 430,
            soc: 35,
            authorized: { direction: "charge", watts: 2500 },
          }),
          phase("rhs", {
            grid: -590,
            battery: -220,
            load: 370,
            soc: 90,
            authorized: { direction: "discharge", watts: 1500 },
          }),
        ]),
        {
          requested_watts_by_unit: { lhs: 800, mid: 2500, rhs: 1500 },
          authorized_watts_by_unit: { lhs: 800, mid: 2500, rhs: 1500 },
          directions_by_unit: { lhs: "discharge", mid: "charge", rhs: "discharge" },
        },
      ),
  },
];
