/**
 * The Energy Flow view's shared model: the per-phase flow facts pulled from
 * the snapshot's own per-unit telemetry, the fleet rollup, and the
 * plain-language wording every flow surface renders.
 *
 * SIGN CONVENTIONS, pinned from the wire (never re-derived here):
 * - `grid_power_w` is signed: POSITIVE = EXPORT, negative = IMPORT
 *   (service.py `_telemetry_summary`, the live capture's own convention).
 * - `battery_watts` (and the unit-level `measured_watts`, which the service
 *   derives from the same observation datum) is signed: POSITIVE =
 *   DISCHARGING, negative = CHARGING (PROTOCOL_EVIDENCE 4b, live-proven).
 * - `load_power_w` is the pod's measured local load (the house on that phase).
 *
 * THE OPERATOR NEVER SEES A SIGN. Every figure is rendered through a
 * direction WORD plus the absolute magnitude ("Importing 412 W",
 * "Charging 1,900 W") — the sign-to-words discipline this module owns and
 * flow.test.ts pins. The lib's formatters carry the display precision
 * (two-decimal bound, integers as integers, thousands grouped).
 *
 * SOLAR HONESTY (the pinned project fact): the site's PV inputs are not wired
 * to the pods' sensors, so no solar node is ever drawn. Export is labeled
 * "exporting" — the surplus leaving the site — and never attributed to solar;
 * the one footnote below says exactly this once per surface.
 *
 * HONEST NULLS: an absent datum ("not available") is never zero-filled, and a
 * measured 0 W is neither import nor export nor charge nor discharge — it is
 * "Idle". Fleet figures sum the phases that report the datum and name their
 * scope when some phase does not.
 */
import { formatPercent, formatWatts } from "../../lib/format";
import { isRecord, patchAdviserState, toAdviserState, type AdviserState } from "../../app/fleet";
import type { DirectionsByUnit, WattsByUnit } from "../../app/fleet";
import {
  patchNightChargeState,
  toNightChargeState,
  type NightChargeState,
} from "../../app/nightCharge";

/** The one solar-honesty footnote the view carries. */
export const FLOW_SOLAR_FOOTNOTE =
  "Solar panels are not wired to the pods' sensors — nothing here is a solar measurement. 'Exporting' is simply surplus power leaving the site.";

// --- the per-phase flow facts ---------------------------------------------------

/** One phase's measured flows (a unit is a phase on this three-phase site). */
export interface FlowUnit {
  unitId: string;
  lifecycle: string;
  /** Signed: positive = export, negative = import; null = no PCS live block. */
  gridPowerW: number | null;
  /** The pod's measured local load; null when not served. */
  loadPowerW: number | null;
  /** Signed: positive = discharging, negative = charging; null = absent. */
  batteryWatts: number | null;
  /** The same observation's charge reading, clamped to 0-100; null = absent. */
  socPct: number | null;
  /**
   * The snapshot's own per-unit commanded figure (`authorized_power`, which is
   * genuinely per-unit on the wire) — the commanded side of the overlay.
   */
  authorized: { direction: string; watts: number } | null;
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Narrow one snapshot unit into its flow facts. The telemetry block is the
 * source; the unit-level `measured_watts` is the same datum the service
 * projects from `battery_watts`, so it stands in honestly when the summary
 * block omitted the field. Nothing is ever zero-filled.
 */
export function toFlowUnit(raw: unknown): FlowUnit | null {
  if (!isRecord(raw) || typeof raw.unit_id !== "string" || raw.unit_id === "") {
    return null;
  }
  const telemetry = isRecord(raw.telemetry) ? raw.telemetry : {};
  const authorizedRaw = isRecord(raw.authorized_power) ? raw.authorized_power : null;
  const soc = finiteOrNull(telemetry.soc_pct);
  return {
    unitId: raw.unit_id,
    lifecycle: typeof raw.lifecycle === "string" ? raw.lifecycle : "",
    gridPowerW: finiteOrNull(telemetry.grid_power_w),
    loadPowerW: finiteOrNull(telemetry.load_power_w),
    batteryWatts: finiteOrNull(telemetry.battery_watts) ?? finiteOrNull(raw.measured_watts),
    socPct: soc !== null && soc >= 0 && soc <= 100 ? soc : null,
    authorized:
      authorizedRaw !== null &&
      typeof authorizedRaw.direction === "string" &&
      typeof authorizedRaw.watts === "number"
        ? { direction: authorizedRaw.direction, watts: authorizedRaw.watts }
        : null,
  };
}

/** The flow view's whole snapshot view-model. */
export interface FlowSnapshot {
  sequence: number;
  units: FlowUnit[];
  /** The excess-solar adviser projection; null when the snapshot carries none. */
  adviserState: AdviserState | null;
  /** The night-strategy projection; null when the snapshot carries none. */
  nightState: NightChargeState | null;
}

/** Narrow a whole snapshot envelope (REST body or WS `data`). */
export function toFlowSnapshot(value: unknown): FlowSnapshot | null {
  if (!isRecord(value) || !Array.isArray(value.units)) {
    return null;
  }
  const units: FlowUnit[] = [];
  for (const entry of value.units) {
    const unit = toFlowUnit(entry);
    if (unit !== null) {
      units.push(unit);
    }
  }
  return {
    sequence:
      typeof value.snapshot_sequence === "number" ? value.snapshot_sequence : 0,
    units,
    adviserState: isRecord(value.adviser_state) ? toAdviserState(value.adviser_state) : null,
    nightState: isRecord(value.night_charge_state)
      ? toNightChargeState(value.night_charge_state)
      : null,
  };
}

/** Apply one `excess_adviser.state_changed` payload onto a flow snapshot's adviser slice. */
export function patchFlowAdviser(
  current: AdviserState | null,
  payload: unknown,
): AdviserState | null {
  return patchAdviserState(current, payload);
}

/** Apply one `night_charge.state_changed` payload onto a flow snapshot's night slice. */
export function patchFlowNight(
  current: NightChargeState | null,
  payload: unknown,
): NightChargeState | null {
  return patchNightChargeState(current, payload);
}

// --- the fleet rollup -----------------------------------------------------------

/** The fleet's summed flows. Each side is null when NO phase reports the datum. */
export interface FleetFlow {
  /** Σ |grid_power_w| over importing phases; 0 when every reporting phase exports. */
  importW: number | null;
  /** Σ grid_power_w over exporting phases; 0 when every reporting phase imports. */
  exportW: number | null;
  importPhases: string[];
  exportPhases: string[];
  /** Σ |battery_watts| over charging phases; 0 when none is charging. */
  chargingW: number | null;
  /** Σ battery_watts over discharging phases; 0 when none is discharging. */
  dischargingW: number | null;
  chargingPhases: string[];
  dischargingPhases: string[];
  /** Σ load_power_w over reporting phases; null when none reports a load. */
  houseW: number | null;
  gridReporting: number;
  batteryReporting: number;
  loadReporting: number;
  unitCount: number;
}

/** Sum the fleet's measured flows. Sums cover reporting phases only. */
export function fleetFlow(units: FlowUnit[]): FleetFlow {
  const gridUnits = units.filter((unit) => unit.gridPowerW !== null);
  const batteryUnits = units.filter((unit) => unit.batteryWatts !== null);
  const loadUnits = units.filter((unit) => unit.loadPowerW !== null);
  const importing = gridUnits.filter((unit) => (unit.gridPowerW ?? 0) < 0);
  const exporting = gridUnits.filter((unit) => (unit.gridPowerW ?? 0) > 0);
  const charging = batteryUnits.filter((unit) => (unit.batteryWatts ?? 0) < 0);
  const discharging = batteryUnits.filter((unit) => (unit.batteryWatts ?? 0) > 0);
  return {
    importW: gridUnits.length > 0 ? importing.reduce((sum, unit) => sum + Math.abs(unit.gridPowerW ?? 0), 0) : null,
    exportW: gridUnits.length > 0 ? exporting.reduce((sum, unit) => sum + (unit.gridPowerW ?? 0), 0) : null,
    importPhases: importing.map((unit) => unit.unitId),
    exportPhases: exporting.map((unit) => unit.unitId),
    chargingW: batteryUnits.length > 0 ? charging.reduce((sum, unit) => sum + Math.abs(unit.batteryWatts ?? 0), 0) : null,
    dischargingW: batteryUnits.length > 0 ? discharging.reduce((sum, unit) => sum + (unit.batteryWatts ?? 0), 0) : null,
    chargingPhases: charging.map((unit) => unit.unitId),
    dischargingPhases: discharging.map((unit) => unit.unitId),
    houseW:
      loadUnits.length > 0 ? loadUnits.reduce((sum, unit) => sum + (unit.loadPowerW ?? 0), 0) : null,
    gridReporting: gridUnits.length,
    batteryReporting: batteryUnits.length,
    loadReporting: loadUnits.length,
    unitCount: units.length,
  };
}

// --- sign-to-words (the discipline: no raw sign ever reaches the operator) ------

/**
 * One worded piece of a figure line: the direction word plus the magnitude it
 * words ("Importing", 412). The diagram renders every figure from these parts
 * so the number tick (§c motion) can tween the magnitude while the word — the
 * worded truth — swaps instantly on a state change, never rolls.
 */
export interface FlowFigurePart {
  readonly word: string;
  readonly watts: number | null;
}

/**
 * The operator's worded line from parts, exactly as the pinned text functions
 * render it ("Importing 412 W", "Charging 3,800 W · Discharging 800 W"). The
 * one join every figure renders through — the diagram invents no formatting.
 */
export function figurePartsText(parts: readonly FlowFigurePart[]): string {
  return parts
    .map((part) => (part.watts === null ? part.word : `${part.word} ${formatWatts(part.watts)}`))
    .join(" · ");
}

/** The grid's one-word direction for a phase, from the signed figure. */
export type GridFlow = "import" | "export" | "idle" | "unknown";

/** The battery's one-word direction for a phase, from the signed figure. */
export type BatteryFlow = "charge" | "discharge" | "idle" | "unknown";

export function gridFlowDirection(gridPowerW: number | null): GridFlow {
  if (gridPowerW === null) {
    return "unknown";
  }
  if (gridPowerW > 0) {
    return "export";
  }
  return gridPowerW < 0 ? "import" : "idle";
}

export function batteryFlowDirection(batteryWatts: number | null): BatteryFlow {
  if (batteryWatts === null) {
    return "unknown";
  }
  if (batteryWatts > 0) {
    return "discharge";
  }
  return batteryWatts < 0 ? "charge" : "idle";
}

/** The grid figure as parts (the single source the pinned text composes from). */
export function gridFigureParts(gridPowerW: number | null): FlowFigurePart[] {
  if (gridPowerW === null) {
    return [{ word: "not available", watts: null }];
  }
  if (gridPowerW > 0) {
    return [{ word: "Exporting", watts: gridPowerW }];
  }
  return gridPowerW < 0 ? [{ word: "Importing", watts: Math.abs(gridPowerW) }] : [{ word: "Idle", watts: null }];
}

/** The battery figure as parts. */
export function batteryFigureParts(batteryWatts: number | null): FlowFigurePart[] {
  if (batteryWatts === null) {
    return [{ word: "not available", watts: null }];
  }
  if (batteryWatts > 0) {
    return [{ word: "Discharging", watts: batteryWatts }];
  }
  return batteryWatts < 0 ? [{ word: "Charging", watts: Math.abs(batteryWatts) }] : [{ word: "Idle", watts: null }];
}

/** The house figure as parts: "Using 340 W" — a measured 0 W stays honest. */
export function houseFigureParts(loadPowerW: number | null): FlowFigurePart[] {
  return loadPowerW === null ? [{ word: "not available", watts: null }] : [{ word: "Using", watts: loadPowerW }];
}

/** The grid figure in words: "Importing 412 W" / "Exporting 300 W" / "Idle". */
export function gridFlowText(gridPowerW: number | null): string {
  return figurePartsText(gridFigureParts(gridPowerW));
}

/** The battery figure in words: "Charging 1,900 W" / "Discharging 800 W" / "Idle". */
export function batteryFlowText(batteryWatts: number | null): string {
  return figurePartsText(batteryFigureParts(batteryWatts));
}

/** The house figure in words: "Using 340 W" — a measured 0 W stays honest. */
export function houseFlowText(loadPowerW: number | null): string {
  return figurePartsText(houseFigureParts(loadPowerW));
}

/** The battery's charge reading beside its flow figure; "" when absent. */
export function socText(socPct: number | null): string {
  return socPct === null ? "" : `${formatPercent(socPct)} charged`;
}

/** The fleet's grid figure as parts — BOTH sides when the phases pull apart. */
export function fleetGridFigureParts(fleet: FleetFlow): FlowFigurePart[] {
  if (fleet.gridReporting === 0) {
    return [{ word: "not available", watts: null }];
  }
  const parts: FlowFigurePart[] = [];
  if (fleet.importW !== null && fleet.importW > 0) {
    parts.push({ word: "Importing", watts: fleet.importW });
  }
  if (fleet.exportW !== null && fleet.exportW > 0) {
    parts.push({ word: "Exporting", watts: fleet.exportW });
  }
  return parts.length > 0 ? parts : [{ word: "Idle", watts: null }];
}

/** The fleet's battery figure as parts — BOTH sides when phases disagree. */
export function fleetBatteryFigureParts(fleet: FleetFlow): FlowFigurePart[] {
  if (fleet.batteryReporting === 0) {
    return [{ word: "not available", watts: null }];
  }
  const parts: FlowFigurePart[] = [];
  if (fleet.chargingW !== null && fleet.chargingW > 0) {
    parts.push({ word: "Charging", watts: fleet.chargingW });
  }
  if (fleet.dischargingW !== null && fleet.dischargingW > 0) {
    parts.push({ word: "Discharging", watts: fleet.dischargingW });
  }
  return parts.length > 0 ? parts : [{ word: "Idle", watts: null }];
}

/** The fleet's house figure as parts. */
export function fleetHouseFigureParts(fleet: FleetFlow): FlowFigurePart[] {
  return fleet.houseW === null ? [{ word: "not available", watts: null }] : [{ word: "Using", watts: fleet.houseW }];
}

/** The fleet house figure's scope suffix; "" when every phase reports a load. */
export function fleetHouseScope(fleet: FleetFlow): string {
  if (fleet.houseW === null) {
    return "";
  }
  return fleet.loadReporting === fleet.unitCount || fleet.unitCount === 0
    ? ""
    : ` — across the ${fleet.loadReporting} of ${fleet.unitCount} phases reporting`;
}

/** The fleet's grid figure in words — BOTH sides when the phases pull apart. */
export function fleetGridText(fleet: FleetFlow): string {
  return figurePartsText(fleetGridFigureParts(fleet));
}

/** The fleet's battery figure in words — BOTH sides when phases disagree. */
export function fleetBatteryText(fleet: FleetFlow): string {
  return figurePartsText(fleetBatteryFigureParts(fleet));
}

/** The fleet's house figure in words, with its scope named when partial. */
export function fleetHouseText(fleet: FleetFlow): string {
  return `${figurePartsText(fleetHouseFigureParts(fleet))}${fleetHouseScope(fleet)}`;
}

// --- the arrow geometry (magnitude made visible; purely decorative) -------------

/** The arrow stroke bounds: visible-but-thin at the floor, bold at the max. */
export const ARROW_MIN_PX = 2;
export const ARROW_MAX_PX = 10;

/**
 * A flow's stroke width, proportional to its watts against the view's largest
 * flow (so thicknesses compare across phases). Idle and unknown draw no arrow
 * (the dashed stub carries the node's presence); a zero scale never divides.
 */
export function arrowWidthPx(watts: number | null, scaleMaxW: number): number {
  if (watts === null || watts === 0 || scaleMaxW <= 0) {
    return 0;
  }
  const share = Math.min(1, Math.abs(watts) / scaleMaxW);
  return ARROW_MIN_PX + (ARROW_MAX_PX - ARROW_MIN_PX) * share;
}

/** The view's common arrow scale: the largest measured flow anywhere on screen. */
export function arrowScaleW(units: FlowUnit[], fleet: FleetFlow): number {
  let max = 0;
  for (const unit of units) {
    max = Math.max(max, Math.abs(unit.gridPowerW ?? 0), Math.abs(unit.batteryWatts ?? 0), unit.loadPowerW ?? 0);
  }
  for (const watts of [fleet.importW, fleet.exportW, fleet.chargingW, fleet.dischargingW, fleet.houseW]) {
    max = Math.max(max, watts ?? 0);
  }
  return max;
}

// --- the story line -------------------------------------------------------------

/** "mid", "mid and rhs", "mid, rhs and lhs" — the operator's phase list. */
export function phaseListText(phases: string[]): string {
  if (phases.length === 0) {
    return "no phase";
  }
  if (phases.length === 1) {
    return phases[0]!;
  }
  return `${phases.slice(0, -1).join(", ")} and ${phases[phases.length - 1]}`;
}

/** "{phases} are" / "{phase} is" — the verb agrees with the phase count. */
function phasesVerb(phases: string[]): string {
  return `${phaseListText(phases)} ${phases.length === 1 ? "is" : "are"}`;
}

function chargingClause(fleet: FleetFlow, all: boolean): string {
  if (fleet.chargingW === null || fleet.chargingW === 0) {
    return "";
  }
  const subject = all ? "All the batteries are" : `${phasesVerb(fleet.chargingPhases)}`;
  return `${subject} charging ${formatWatts(fleet.chargingW)} in total`;
}

function dischargingClause(fleet: FleetFlow, all: boolean): string {
  if (fleet.dischargingW === null || fleet.dischargingW === 0) {
    return "";
  }
  const subject = all ? "The batteries are" : `${phasesVerb(fleet.dischargingPhases)}`;
  return `${subject} discharging ${formatWatts(fleet.dischargingW)} in total`;
}

function houseClause(fleet: FleetFlow): string {
  return fleet.houseW === null || fleet.houseW === 0
    ? ""
    : `, the house using ${formatWatts(fleet.houseW)}`;
}

function gridClause(fleet: FleetFlow): string {
  const importing = (fleet.importW ?? 0) > 0;
  const exporting = (fleet.exportW ?? 0) > 0;
  if (importing && exporting) {
    return `, importing ${formatWatts(fleet.importW ?? 0)} on ${phaseListText(fleet.importPhases)} while exporting ${formatWatts(fleet.exportW ?? 0)} on ${phaseListText(fleet.exportPhases)}`;
  }
  if (importing) {
    return `, the site importing ${formatWatts(fleet.importW ?? 0)}`;
  }
  if (exporting) {
    return `, the site exporting ${formatWatts(fleet.exportW ?? 0)}`;
  }
  return "";
}

/** True when every phase with a battery reading is on that side. */
function coversAllReporting(fleet: FleetFlow, phases: string[]): boolean {
  return fleet.batteryReporting > 0 && phases.length === fleet.batteryReporting;
}

/**
 * The story line — one plain sentence composing the measured state, the
 * advisers' words riding on top when their projections are present
 * (feature-detected: an absent projection changes nothing).
 *
 * Adviser precedence mirrors the site's own: the night window and the
 * solar-surplus window never share a minute (export exists only in daylight,
 * the night window is darkness), and a manual request coexists with either —
 * so the story names whichever strategy projection says it is ACTIVE, then
 * falls back to the plain measured composition, and a live request prefixes
 * "Carrying out a power request —" when neither adviser speaks.
 */
export function flowStory(units: FlowUnit[], fleet: FleetFlow): string {
  return measuredStory(units, fleet);
}

/** The measured composition the advisers' stories are built from. */
export function measuredStory(units: FlowUnit[], fleet: FleetFlow): string {
  if (units.length === 0) {
    return "";
  }
  const anyMeasured =
    fleet.gridReporting > 0 || fleet.batteryReporting > 0 || fleet.loadReporting > 0;
  if (!anyMeasured) {
    return "No power readings yet — the pods have not reported a measurement.";
  }
  const splitPhases =
    (fleet.importW ?? 0) > 0 && (fleet.exportW ?? 0) > 0;
  const chargingW = fleet.chargingW ?? 0;
  const dischargingW = fleet.dischargingW ?? 0;

  if (splitPhases && chargingW === 0 && dischargingW === 0) {
    return `The phases are pulling different ways${gridClause(fleet)}${houseClause(fleet)}.`;
  }
  if (chargingW > 0 && dischargingW > 0) {
    // Mixed directions: name each side's phases plainly ("lhs is discharging
    // 800 W while mid and rhs charge 1,900 W in total").
    const dischargingSide = `${phasesVerb(fleet.dischargingPhases)} discharging ${formatWatts(dischargingW)}`;
    const chargingSide = `${phasesVerb(fleet.chargingPhases)} charging ${formatWatts(chargingW)} in total`;
    return `${dischargingSide} while ${chargingSide}${houseClause(fleet)}${splitPhases ? gridClause(fleet) : ""}.`;
  }
  if (chargingW > 0) {
    const all = coversAllReporting(fleet, fleet.chargingPhases);
    let source = "";
    if ((fleet.importW ?? 0) > 0) {
      source = " from the grid";
    } else if ((fleet.exportW ?? 0) > 0) {
      // Charging while exporting: the honest measured fact only — the export
      // is surplus leaving the site, and PV is NOT measured (the footnote's
      // own pin), so no sentence here may call it solar.
      source = `, while the site exports ${formatWatts(fleet.exportW ?? 0)}`;
    }
    return `${chargingClause(fleet, all)}${source}${houseClause(fleet)}.`;
  }
  if (dischargingW > 0) {
    const all = coversAllReporting(fleet, fleet.dischargingPhases);
    return `${dischargingClause(fleet, all)}${houseClause(fleet)}${gridClause(fleet)}.`;
  }
  if ((fleet.houseW ?? 0) > 0) {
    return `The batteries are idle — the house is using ${formatWatts(fleet.houseW ?? 0)}${gridIdleText(fleet)}.`;
  }
  return "Nothing is flowing — the grid connection, the batteries and the house are all idle.";
}

function gridIdleText(fleet: FleetFlow): string {
  if ((fleet.importW ?? 0) > 0) {
    return `, imported from the grid (${formatWatts(fleet.importW ?? 0)})`;
  }
  if ((fleet.exportW ?? 0) > 0) {
    return `, while the site exports ${formatWatts(fleet.exportW ?? 0)}`;
  }
  if (fleet.gridReporting > 0) {
    return ", with the grid connection idle too";
  }
  return "";
}

/**
 * The night strategy's story when its projection says a window is running
 * (feature-detected: null projection, an idle phase, or a disabled adviser
 * never claims the story). The stand-by story names the active stand-down's
 * truth (the grid serves the heavy load; charging resumes below
 * threshold − hysteresis); the fail-closed hold's guarantee is
 * the design's own wording: batteries neither drain nor cycle while the grid
 * meets the spike. A unit parked in true standby rides no submission and its
 * silence is named beside every running story — the fleet sums cannot speak
 * for a battery that takes no writes.
 */
export function nightStory(night: NightChargeState, fleet: FleetFlow): string | null {
  if (!night.enabled || night.phase === "idle") {
    return null;
  }
  // The true standby's clause (2026-08-26): any row parked by the per-phase
  // demand rule is named in the operator's own list shape, whatever the fleet
  // phase is doing around it.
  const parked = night.units
    .filter((unit) => unit.phase === "standing_by_parked")
    .map((unit) => unit.unitId);
  const parkedText = parked.length === 0 ? "" : ` ${parkedStoryClause(parked)}`;
  switch (night.phase) {
    case "pacing": {
      const charging = chargingClause(fleet, coversAllReporting(fleet, fleet.chargingPhases));
      return `Night charging is running${charging === "" ? "" : ` — ${charging}`}${houseClause(fleet)}.${parkedText}`;
    }
    case "standing_by_on_demand": {
      const demand =
        night.demandW === null
          ? "the demand reading is not available"
          : `house demand is ${formatWatts(night.demandW)}`;
      // Same wire-figure rule as the tile: watts only from the projection.
      const bound =
        night.demandExitHysteresisW > 0 && night.demandThresholdW > night.demandExitHysteresisW
          ? `below ${formatWatts(night.demandThresholdW - night.demandExitHysteresisW)}`
          : "once demand falls back";
      return `Night charging is standing by — ${demand}, so the grid serves the heavy load and charging resumes ${bound}.${parkedText}`;
    }
    case "holding_on_demand": {
      const demand =
        night.demandW === null
          ? "the demand reading is not available"
          : `house demand is ${formatWatts(night.demandW)}`;
      return `Night charging is holding — ${demand}, so the batteries neither drain nor cycle while the grid meets the house.${parkedText}`;
    }
    case "complete":
      return "Night charging is complete — the batteries are full.";
    case "skipped_full":
      return "The night window found the batteries already full — nothing to charge.";
    default:
      return null;
  }
}

/**
 * The parked batteries' own clause: who, and the exact silence they are in —
 * neither charge nor discharge until their own good word falls back below the
 * line. The verb agrees with the count; the names render the operator's way
 * ("mid", "mid and rhs").
 */
function parkedStoryClause(unitIds: string[]): string {
  const who = phaseListText(unitIds);
  const is = unitIds.length === 1 ? "is" : "are";
  const answers = unitIds.length === 1 ? "answers" : "answer";
  return `${who} ${is} parked in standby — ${answers} neither charge nor discharge until house demand falls.`;
}

/**
 * The solar-surplus adviser's story when its projection says it is commanding
 * (feature-detected). Export is the adviser's own export reading when it
 * holds, the measured fleet sum otherwise — never zero-filled, and never
 * called solar production (PV is not measured at this site).
 */
export function excessStory(adviser: AdviserState, fleet: FleetFlow): string | null {
  if (!adviser.active) {
    return null;
  }
  const exportW = adviser.fleetExportW ?? (fleet.exportW !== null && fleet.exportW > 0 ? fleet.exportW : null);
  const charging = chargingClause(fleet, coversAllReporting(fleet, fleet.chargingPhases));
  const lead = charging === "" ? "the batteries are holding their charge" : charging;
  return `Solar-surplus charging is active — ${lead}${
    exportW !== null && exportW > 0 ? `, while the site exports ${formatWatts(exportW)}` : ""
  }.`;
}

/** True when any phase carries a live commanded figure (the overlay's gate). */
export function hasLiveCommand(units: FlowUnit[], authorizedByUnit: WattsByUnit | null): boolean {
  return units.some((unit) => {
    const mapped = authorizedByUnit?.[unit.unitId];
    if (mapped !== undefined) {
      return mapped > 0;
    }
    return unit.authorized !== null && unit.authorized.direction !== "idle" && unit.authorized.watts > 0;
  });
}

/** One commanded-vs-measured row, exactly as the overlay renders it. */
export interface CommandRow {
  unitId: string;
  direction: "charge" | "discharge";
  commandedW: number;
  measuredW: number | null;
  text: string;
}

/**
 * The command overlay: one row per phase carrying a live commanded figure,
 * resolved through the shared per-unit figures machinery (the tracker's
 * authorized map first — the one source that names which battery a headroom
 * clamp hit — then the snapshot's own per-unit `authorized_power`, which the
 * wire makes genuinely per-unit). The measured side is the same observation's
 * battery figure, worded by direction; a phase moving the OPPOSITE way to its
 * command says so — never a silent mismatch.
 */
export function commandRows(
  units: FlowUnit[],
  authorizedByUnit: WattsByUnit | null,
  directionsByUnit: DirectionsByUnit | null,
): CommandRow[] {
  const rows: CommandRow[] = [];
  for (const unit of units) {
    const mappedWatts = authorizedByUnit?.[unit.unitId];
    const commandedW = mappedWatts ?? unit.authorized?.watts ?? null;
    const direction =
      directionsByUnit?.[unit.unitId] ?? unit.authorized?.direction ?? "idle";
    if (commandedW === null || commandedW <= 0) {
      continue;
    }
    if (direction !== "charge" && direction !== "discharge") {
      continue;
    }
    const measured = unit.batteryWatts;
    let measuredText: string;
    if (measured === null) {
      measuredText = "no measurement yet";
    } else if (direction === "charge") {
      measuredText =
        measured < 0
          ? `charging at ${formatWatts(Math.abs(measured))}`
          : measured > 0
            ? `but currently discharging ${formatWatts(measured)}`
            : "not moving yet";
    } else {
      measuredText =
        measured > 0
          ? `delivering ${formatWatts(measured)}`
          : measured < 0
            ? `but currently charging ${formatWatts(Math.abs(measured))}`
            : "not moving yet";
    }
    rows.push({
      unitId: unit.unitId,
      direction,
      commandedW,
      measuredW: measured,
      text: `${unit.unitId} — commanded ${formatWatts(commandedW)} ${direction === "charge" ? "charge" : "discharge"}, ${measuredText}`,
    });
  }
  return rows;
}

/** The per-phase lifecycle note; null when the phase needs no word. */
export function lifecycleNote(lifecycle: string): string | null {
  switch (lifecycle) {
    case "disconnected":
      return "No contact — these figures are the last known";
    case "inhibited":
      return "Held by a safety latch";
    case "observe_only":
      return "Observe only";
    case "stopping":
      return "Stopping";
    case "boot":
      return "Starting up";
    default:
      return null;
  }
}
