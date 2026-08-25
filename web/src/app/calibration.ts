/**
 * The battery calibration cycling program's shared wire model
 * (DESIGN_CALIBRATION_CYCLING.md §8/§9, CONTRACT v1.1): the
 * `calibration_state` snapshot projection plus the plain-word maps the Home
 * Calibration card renders. The program is the periodic bottom-anchor
 * traverse — ONE pod per cycle-night, selected by need — and its own
 * sibling of the health watch (A14: its own block, its own window, its own
 * vocabulary; the two cards share the maintenance row and nothing else).
 *
 * Wire truth pinned here (§8's exact shape):
 * - Projection: `{mode, submits?, window{opens_local, ends_local}, phase,
 *   target?, reason?, due_waived?, as_of, units[], last_cycle,
 *   request_measurement, traverse?, close?}`. `mode: advise` renders the
 *   whole projection with `submits: "never"` named beside it — a displayed
 *   plan that does not act MUST say so beside itself.
 * - `phase`: idle | planned | traversing | closing | holding | complete.
 * - `units[]`: `{unit_id, class, days_since_deep, horizon_bounded,
 *   last_deep_date, horizon_days, due, evidence_short, kind, anchored,
 *   standdown}` with the class figures (`sub_floor_dates_14d`,
 *   `throughput_wh_mean`, `probe_verdict`, `probe_at`) riding their classes.
 * - `class`: eligible | excluded_cycles_daily | deferred_probe_required |
 *   no_control_evidence | not_due | evidence_short — the §3.2 set, rendered
 *   verbatim, never silently.
 * - `last_cycle.verdict`: floor_reached | floor_miss_energy_bound |
 *   floor_miss_deadline | inconclusive_preempted | inconclusive_interrupted
 *   | skipped:<reason> | aborted:<reason> — the §4.4 vocabulary.
 * - `horizon_bounded` rides every due figure (C1: a never-deep pod's
 *   days-since number is an honest LOWER bound).
 *
 * Honesty rules (§9's own): the pinned §0 sentence and the C16 stop-route
 * line ride the card's traverse state wherever it renders; skips and
 * deferrals render their reason verbatim; the stand-down states the
 * operator decision tree; and uncommissioned renders nothing at all (the
 * card keys on the snapshot key's own absence).
 */
import { isRecord } from "./fleet";

/** The bus vocabulary the program publishes (§8). */
export const CALIBRATION_CYCLE_EVENT = "calibration.cycle" as const;

/** The program's per-civil-day phase machine (§8). */
export type CalibrationPhase =
  | "idle"
  | "planned"
  | "traversing"
  | "closing"
  | "holding"
  | "complete";

export const CALIBRATION_PHASES: readonly CalibrationPhase[] = [
  "idle",
  "planned",
  "traversing",
  "closing",
  "holding",
  "complete",
];

/** §9's severity tiers. */
export type CalibrationTier = "notice" | "alert" | "resolved" | null;

/** One battery's plan row: the class chip and its figures (§3). */
export interface CalibrationUnitState {
  unitId: string;
  klass: string;
  daysSinceDeep: number;
  horizonBounded: boolean;
  lastDeepDate: string | null;
  horizonDays: number;
  due: boolean;
  evidenceShort: boolean;
  kind: string;
  anchored: boolean;
  standdown: boolean;
  throughputWhMean: number | null;
  subFloorDates: number | null;
  probeVerdict: string | null;
  probeAt: string | null;
}

/** The live traverse line on cycle nights (§9). */
export interface CalibrationTraverseState {
  unitId: string;
  socPct: number | null;
  floorPct: number;
  energyWh: number;
  energyBoundWh: number;
  rateW: number | null;
  atRisk: boolean;
}

/** The close's own state (§5): the taper observation and its attribution. */
export interface CalibrationCloseState {
  unitId: string;
  night: string;
  verdict: string;
  taperObserved: boolean;
  holdInterrupted: boolean;
  attribution: string | null;
}

/** §9's morning-facts entry: the day-following-a-cycle line (the delta pair
 * and the spread change — the measurement's own before/after figures). */
export interface CalibrationMorningFacts {
  night: string;
  unitId: string;
  verdict: string;
  graduation: string;
  taperObserved: boolean;
  attribution: string | null;
  deltaBefore: number | null;
  deltaAfter: number | null;
  deltaChange: number | null;
  spreadBeforeMv: number | null;
  spreadAfterMv: number | null;
  tier: CalibrationTier;
}

/** §6.1's cycle record (the completed row's own shape). */
export interface CalibrationCycleRecord {
  night: string;
  kind: string;
  verdict: string;
  tier: CalibrationTier;
  traceClass: string;
  energyWh: number | null;
  economicsCents: Record<string, number> | null;
}

export interface CalibrationState {
  mode: string;
  submits: string | null;
  window: { opensLocal: string; endsLocal: string };
  phase: CalibrationPhase;
  target: string | null;
  reason: string | null;
  dueWaived: string | null;
  asOf: string;
  units: CalibrationUnitState[];
  lastCycle: CalibrationCycleRecord | null;
  morning: CalibrationMorningFacts | null;
  requestMeasurement: { unit: string; note: string; consumed: boolean } | null;
  traverse: CalibrationTraverseState | null;
  close: CalibrationCloseState | null;
}

// --- parsing -------------------------------------------------------------------

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function intOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : fallback;
}

function toUnit(value: unknown): CalibrationUnitState | null {
  if (!isRecord(value) || typeof value.unit_id !== "string" || value.unit_id === "") {
    return null;
  }
  const record = value as Record<string, unknown>;
  const subFloorDates = Object.keys(record).find((key) =>
    key.startsWith("sub_floor_dates_"),
  );
  return {
    unitId: value.unit_id,
    klass: text(record.class, "not_due"),
    daysSinceDeep: intOr(record.days_since_deep, 0),
    horizonBounded: record.horizon_bounded === true,
    lastDeepDate: optionalText(record.last_deep_date),
    horizonDays: intOr(record.horizon_days, 0),
    due: record.due === true,
    evidenceShort: record.evidence_short === true,
    kind: text(record.kind, "measurement"),
    anchored: record.anchored === true,
    standdown: record.standdown === true,
    throughputWhMean: optionalNumber(record.throughput_wh_mean),
    subFloorDates:
      subFloorDates !== undefined && typeof record[subFloorDates] === "number"
        ? Math.round(record[subFloorDates] as number)
        : null,
    probeVerdict: optionalText(record.probe_verdict),
    probeAt: optionalText(record.probe_at),
  };
}

function toMorning(value: unknown): CalibrationMorningFacts | null {
  if (!isRecord(value) || typeof value.unit_id !== "string") {
    return null;
  }
  const delta = isRecord(value.delta_pct) ? value.delta_pct : {};
  const spread = isRecord(value.spread) ? value.spread : {};
  return {
    night: text(value.night),
    unitId: value.unit_id,
    verdict: text(value.verdict),
    graduation: text(value.graduation),
    taperObserved: value.taper_observed === true,
    attribution: optionalText(value.attribution),
    deltaBefore: optionalNumber(delta.before),
    deltaAfter: optionalNumber(delta.after),
    deltaChange: optionalNumber(delta.change),
    spreadBeforeMv: optionalNumber(spread.before_mv),
    spreadAfterMv: optionalNumber(spread.after_mv),
    tier:
      value.tier === "alert" || value.tier === "notice" || value.tier === "resolved"
        ? value.tier
        : null,
  };
}

function toTraverse(value: unknown): CalibrationTraverseState | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    unitId: text(value.unit_id),
    socPct: optionalNumber(value.soc_pct),
    floorPct: optionalNumber(value.floor_pct) ?? 10,
    energyWh: optionalNumber(value.energy_wh) ?? 0,
    energyBoundWh: optionalNumber(value.energy_bound_wh) ?? 0,
    rateW: optionalNumber(value.rate_w),
    atRisk: value.at_risk === true,
  };
}

function toClose(value: unknown): CalibrationCloseState | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    unitId: text(value.unit_id),
    night: text(value.night),
    verdict: text(value.verdict),
    taperObserved: value.taper_observed === true,
    holdInterrupted: value.hold_interrupted === true,
    attribution: optionalText(value.attribution),
  };
}

function toLastCycle(value: unknown): CalibrationCycleRecord | null {
  if (!isRecord(value) || typeof value.verdict !== "string") {
    return null;
  }
  const economics = isRecord(value.economics_cents)
    ? (Object.fromEntries(
        Object.entries(value.economics_cents).filter(
          (entry): entry is [string, number] => typeof entry[1] === "number",
        ),
      ) as Record<string, number>)
    : null;
  return {
    night: text(value.night),
    kind: text(value.kind, "measurement"),
    verdict: value.verdict,
    tier:
      value.tier === "alert" || value.tier === "notice" || value.tier === "resolved"
        ? value.tier
        : null,
    traceClass: text(value.trace_class),
    energyWh: optionalNumber(value.energy_wh),
    economicsCents: economics,
  };
}

/**
 * Narrow one calibration projection (a snapshot's `calibration_state` or the
 * status route's body). Null when the value is not an object: a garbage
 * frame is never half-adopted.
 */
export function toCalibrationState(value: unknown): CalibrationState | null {
  if (!isRecord(value)) {
    return null;
  }
  const units = Array.isArray(value.units)
    ? (value.units as unknown[])
        .map(toUnit)
        .filter((unit): unit is CalibrationUnitState => unit !== null)
    : [];
  const window = isRecord(value.window) ? value.window : {};
  const request = isRecord(value.request_measurement)
    ? {
        unit: text(value.request_measurement.unit),
        note: text(value.request_measurement.note),
        consumed: value.request_measurement.consumed === true,
      }
    : null;
  return {
    mode: text(value.mode, "advise"),
    submits: optionalText(value.submits),
    window: {
      opensLocal: text(window.opens_local, "15:00"),
      endsLocal: text(window.ends_local, "22:30"),
    },
    phase: CALIBRATION_PHASES.includes(value.phase as CalibrationPhase)
      ? (value.phase as CalibrationPhase)
      : "idle",
    target: optionalText(value.target),
    reason: optionalText(value.reason),
    dueWaived: optionalText(value.due_waived),
    asOf: text(value.as_of),
    units,
    lastCycle: toLastCycle(value.last_cycle),
    morning: toMorning(value.morning),
    requestMeasurement: request,
    traverse: toTraverse(value.traverse),
    close: toClose(value.close),
  };
}

// --- plain-language maps ---------------------------------------------------------

/** §0's pinned sentence, VERBATIM — it rides every traverse-state surface. */
export const CALIBRATION_PINNED_SENTENCE =
  "Partial cycles anchor nothing — the floor is the anchor, and the anchor is a floor, not a finish line: the traverse stops at it and never beneath it.";

/** C16's stop-route line, VERBATIM — it rides wherever traverse styling renders. */
export const CALIBRATION_STOP_ROUTE =
  "to stop tonight's traverse, claim the pod — any manual command preempts instantly";

/**
 * The card's status sentence: the phase in plain words, with `advise` naming
 * its own truth (a displayed plan that does not act says so beside itself).
 */
export function calibrationStatusText(state: CalibrationState): string {
  const advise = state.submits === "never" ? " (advise — computes and displays, submits nothing)" : "";
  switch (state.phase) {
    case "planned":
      return `Tonight's calibration traverse opens at ${state.window.opensLocal}${advise}.`;
    case "traversing":
      return `The calibration traverse is running${advise}.`;
    case "closing":
      return `Observing tonight's close — the morning taper that proves the top anchor${advise}.`;
    case "holding":
      return `The top anchor landed; observing the at-full hold (the balancer's courtesy window)${advise}.`;
    case "complete":
      return "The calibration cycle is complete — the record is on the card.";
    default:
      return `The calibration program idles until the ${state.window.opensLocal} window${advise}.`;
  }
}

/** One unit's class chip in plain words (the §3.2 set, rendered verbatim). */
export function calibrationClassText(unit: CalibrationUnitState): string {
  switch (unit.klass) {
    case "eligible":
      return unit.standdown
        ? "stood down (awaiting your acknowledgement)"
        : unit.anchored
          ? "in rotation"
          : "eligible — first cycle is a measurement";
    case "excluded_cycles_daily":
      return `excluded — cycles daily (below 25% on ${unit.subFloorDates ?? 0} of the last 14 dates)`;
    case "deferred_probe_required":
      return "deferred — no passing health-watch probe in the window";
    case "no_control_evidence":
      return "deferred — no control-works evidence (and no health watch to prove it)";
    case "evidence_short":
      return "waiting — the historian horizon is younger than N days";
    default:
      return "not due";
  }
}

/** The due line: the honest number, with C1's lower-bound flag named. */
export function calibrationDueText(unit: CalibrationUnitState): string {
  if (!unit.due) {
    return "";
  }
  const bounded = unit.horizonBounded ? " at least" : "";
  const since = unit.horizonBounded
    ? `${unit.daysSinceDeep} days without crossing below the trigger line`
    : `${unit.daysSinceDeep} days since its last deep date`;
  return `${bounded} ${since}`;
}

/** The traverse verdict in plain words (§4.4's vocabulary). */
export function calibrationVerdictText(verdict: string): string {
  if (verdict.startsWith("skipped:")) {
    return `skipped — ${verdict.slice("skipped:".length).replace(/_/g, " ")}`;
  }
  if (verdict.startsWith("aborted:")) {
    return `aborted — ${verdict.slice("aborted:".length).replace(/_/g, " ")} (a guard ended the leg, never a battery failure)`;
  }
  switch (verdict) {
    case "floor_reached":
      return "anchor delivered — the traverse stopped at the floor";
    case "floor_miss_energy_bound":
      return "the SoC word did not reach the floor and the coulomb book says the anchor's energy was delivered — the lying-word finding, alert tier";
    case "floor_miss_deadline":
      return "the deadline arrived with the floor unmet — a partial traverse anchors nothing, and the record says so";
    case "inconclusive_preempted":
      return "inconclusive — an operator's own act ended it (no depth credit, no re-run)";
    case "inconclusive_interrupted":
      return "inconclusive — a restart retired the night; the pod's next chance is the next eligible selection";
    default:
      return verdict;
  }
}

/** The close's attribution word in plain words (§5.4's split). */
export function calibrationAttributionText(word: string): string {
  switch (word) {
    case "top_anchor_missed_solar":
      return "the top anchor missed on the sky's account — poor morning surplus; retry is the next eligible night";
    case "taper_never_observed":
      return "the top anchor missed on the pod's account — good surplus, the pod stayed below full with the charge limit open; the health watch's stuck predicates are the next diagnostic";
    default:
      return word;
  }
}

/**
 * The card's alert story: the first alert-tier fact, for the card's own
 * alert styling (§9's tiers). Null while nothing is at alert tier.
 */
export function calibrationAlertText(state: CalibrationState): string | null {
  const stoodDown = state.units.filter((unit) => unit.standdown);
  if (stoodDown.length > 0) {
    return `${stoodDown.map((unit) => unit.unitId).join(", ")} stood down from rotation — the first cycle's re-anchor signature was absent; the operator acknowledgement (the standing surfaces) is the exit.`;
  }
  const missed = state.units.filter((unit) => unit.due && !unit.evidenceShort);
  const last = state.lastCycle;
  if (last !== null && last.tier === "alert") {
    return `${calibrationVerdictText(last.verdict)} on the last cycle.`;
  }
  if (missed.length > 0) {
    return null; // a due flag alone is the notice-tier coming-attraction line
  }
  return null;
}

/** §6.2's honesty clause, VERBATIM from the wire model. */
export const CALIBRATION_EVENT_NOTE =
  "no SoC-calibration event register exists in the read inventory — the program instruments the observable deltas; the BMU event log remains the on-device cross-check";
