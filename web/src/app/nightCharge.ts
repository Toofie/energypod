/**
 * The night-charge surface's shared wire model (DESIGN_NIGHT_CHARGE.md §5 +
 * API_CONTRACTS.md "Off-peak night charge"): the `night_charge_state` snapshot
 * projection, the `night_charge.state_changed` bus event, and the plain-word
 * maps every night surface (Home's Night tile, the shell) shares.
 *
 * PENDING-BACKEND: the projection, the event, and the guarded toggle route are
 * not live yet — every parser here is built feature-detectively against the
 * contract's pinned shapes (the adviser_state / schedule_state pattern): an
 * ABSENT field is the feature detection and never an error; a PRESENT-but-
 * unusable datum falls back to its honest default (null / 0 / ""), never to a
 * fabricated figure. The whole surface renders nothing while the snapshot
 * carries no `night_charge_state`.
 *
 * Wire truth pinned here (the docs govern):
 * - Projection: `{enabled, enabled_origin, acknowledged_partition, posture,
 *   active, phase, window{start_local,end_local,timezone}, window_ends_at,
 *   window_ends_in_s, next_window_at, pacing, rate_cap_w, hold_rate_w,
 *   demand_scope, demand_threshold_w, demand_w, demand_evidence,
 *   held_intent_id, units[{unit_id, soc_pct, phase, target_w, reason}],
 *   last_action, last_tick_at, reason_codes}` — `active` derives from
 *   `held_intent_id`, never a lifecycle guess.
 * - Fleet `phase`: idle | pacing | holding_on_demand | complete | skipped_full;
 *   per-unit adds `sitting_out` (claimed, disarmed, or no headroom this tick).
 * - `demand_evidence`: good | missing | bad | stale, worst-word-wins; a
 *   non-good rollup FAILS CLOSED TO HOLD (the no-cycling guarantee) and is
 *   loudly visible — never a silent never-charges.
 * - Reason vocabulary (ONE): outside_window, window_open, on_plan,
 *   deadline_at_risk, demand_above_threshold, demand_below_exit,
 *   demand_evidence_missing, demand_evidence_bad, demand_evidence_stale,
 *   at_ceiling, no_charge_headroom, target_reached, no_eligible_units,
 *   units_disarmed, yielding_to_higher_priority, disabled_by_config,
 *   disabled_by_runtime, night_acknowledgement_required.
 * - Event `night_charge.state_changed`: published when the semantic tuple
 *   `(enabled, enabled_origin, acknowledged_partition, active, phase,
 *   active_unit_ids, demand_evidence, reason_codes)` changes — watts/SOC ride
 *   but never trigger — with a 30 s `"heartbeat": true` republish while
 *   enabled and NOTHING while disabled. Payload: the projection subset minus
 *   `last_action`/`last_tick_at`, plus `heartbeat`.
 */
import { formatPercent, formatWatts } from "../lib/format";
import { isRecord } from "./fleet";
import { countdownText, localTimeOfInstant, type SchedulePosture } from "./schedule";

/** The bus vocabulary the night surface consumes (its one event type). */
export const NIGHT_CHARGE_STATE_CHANGED_EVENT = "night_charge.state_changed" as const;

/** The fleet-level phase vocabulary (§5's ONE list, verbatim). */
export type NightPhase = "idle" | "pacing" | "holding_on_demand" | "complete" | "skipped_full";

/** The runtime checklist of the fleet-level list (an unknown phase narrows to idle). */
export const NIGHT_PHASES: readonly NightPhase[] = [
  "idle",
  "pacing",
  "holding_on_demand",
  "complete",
  "skipped_full",
];

/** The per-unit phase vocabulary: the fleet list plus `sitting_out`. */
export type NightUnitPhase =
  | "pacing"
  | "holding_on_demand"
  | "skipped_full"
  | "complete"
  | "sitting_out";

/** The runtime checklist of the per-unit list (an unknown phase narrows to sitting_out). */
export const NIGHT_UNIT_PHASES: readonly NightUnitPhase[] = [
  "pacing",
  "holding_on_demand",
  "skipped_full",
  "complete",
  "sitting_out",
];

/** The demand rollup's evidence word, worst-word-wins. */
export type NightDemandEvidence = "good" | "missing" | "bad" | "stale";

/** The commissioned pacing rule (§2.2): Docker-parity default, or deadline-paced. */
export type NightPacingRule = "cap_first" | "even";

/** Whose load words feed the demand rule: the fleet sum, or per-phase. */
export type NightDemandScope = "fleet" | "per_phase";

const PHASES: readonly NightPhase[] = NIGHT_PHASES;
const UNIT_PHASES: readonly NightUnitPhase[] = NIGHT_UNIT_PHASES;
const DEMAND_EVIDENCES: readonly NightDemandEvidence[] = ["good", "missing", "bad", "stale"];
const PACING_RULES: readonly NightPacingRule[] = ["cap_first", "even"];
const DEMAND_SCOPES: readonly NightDemandScope[] = ["fleet", "per_phase"];

/**
 * The projection's ONE pinned reason vocabulary (§5), verbatim. The console's
 * plain sentences map from these codes — the codes are the wire; renderers
 * must treat every code in this list (an unknown code renders honestly, never
 * silently dropped).
 */
export const NIGHT_REASON_CODES: readonly string[] = [
  "outside_window",
  "window_open",
  "on_plan",
  "deadline_at_risk",
  "demand_above_threshold",
  "demand_below_exit",
  "demand_evidence_missing",
  "demand_evidence_bad",
  "demand_evidence_stale",
  "at_ceiling",
  "no_charge_headroom",
  "target_reached",
  "no_eligible_units",
  "units_disarmed",
  "yielding_to_higher_priority",
  "disabled_by_config",
  "disabled_by_runtime",
  "night_acknowledgement_required",
];

// --- parsing -------------------------------------------------------------------

function oneOf<T extends string>(value: unknown, vocabulary: readonly T[], fallback: T): T {
  return typeof value === "string" && (vocabulary as readonly string[]).includes(value)
    ? (value as T)
    : fallback;
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function finite(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/**
 * NULL IS A REAL ANSWER for `demand_w` under a non-good rollup: an explicit
 * wire null means the demand reading did not hold, and only a genuinely absent
 * key inherits the base's figure (the event patch must not erase the snapshot's
 * reading just because the payload omits it) — the fleet_export_w rule.
 */
function nullableFinite(value: unknown, base: number | null): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return value === null ? null : base;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry !== "")
    : [];
}

/** One unit's per-tick plan row: its own SOC, phase, target, and reason. */
export interface NightUnitState {
  unitId: string;
  /** The projection's own per-unit charge reading; null when it carried none. */
  socPct: number | null;
  phase: NightUnitPhase;
  /** This tick's whole-watt target; 0 is the real zero-watt sit-out. */
  targetW: number;
  reason: string;
}

function toNightUnit(value: unknown): NightUnitState | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.unit_id !== "string" || value.unit_id === "") {
    return null;
  }
  return {
    unitId: value.unit_id,
    socPct:
      typeof value.soc_pct === "number" && Number.isFinite(value.soc_pct) ? value.soc_pct : null,
    phase: oneOf(value.phase, UNIT_PHASES, "sitting_out"),
    targetW: Math.max(0, finite(value.target_w, 0)),
    reason: text(value.reason),
  };
}

/** The commissioned civil-time window (one pair, the projection's own form). */
export interface NightWindow {
  startLocal: string;
  endLocal: string;
  /** The IANA zone the window is a civil-time fact in (§3.1, REQUIRED). */
  timezone: string;
}

function toNightWindow(value: unknown, base: NightWindow): NightWindow {
  const window = isRecord(value) ? value : {};
  return {
    startLocal: text(window.start_local, base.startLocal),
    endLocal: text(window.end_local, base.endLocal),
    timezone: text(window.timezone, base.timezone),
  };
}

/**
 * The snapshot's top-level `night_charge_state` projection — the night
 * adviser's whole tick state in one object (the adviser_state mirror). ABSENT
 * on the wire when the `night_charging` config block is absent (the feature
 * detection); present-but-partial frames fall back per field.
 */
export interface NightChargeState {
  /** Participating this process (config at boot, or the toggle since). */
  enabled: boolean;
  /** "runtime" = last changed by the toggle: operational only until restart. */
  enabledOrigin: "config" | "runtime";
  /** The site holds the durable night-partition acknowledgement. */
  acknowledgedPartition: boolean;
  /** The site's derived schedule posture (the partition grant's own word). */
  posture: SchedulePosture;
  /** A live night intent exists right now (derived from held_intent_id). */
  active: boolean;
  phase: NightPhase;
  window: NightWindow;
  /** ISO instant the open window ends; null outside the window. */
  windowEndsAt: string | null;
  /** The snapshot-derived countdown to window end; null outside the window. */
  windowEndsInS: number | null;
  /** ISO instant the NEXT window opens; null while a window is open. */
  nextWindowAt: string | null;
  pacing: NightPacingRule;
  rateCapW: number;
  holdRateW: number;
  demandScope: NightDemandScope;
  demandThresholdW: number;
  /** The measured site demand (the LOAD words, never the grid words); null under failed evidence. */
  demandW: number | null;
  demandEvidence: NightDemandEvidence;
  /** The live night intent id; null when holding nothing. */
  heldIntentId: string | null;
  units: NightUnitState[];
  lastAction: string;
  /** Wall time of the last completed tick (the projection is tick-granular). */
  lastTickAt: string;
  reasonCodes: string[];
}

const DEFAULT_WINDOW: NightWindow = {
  startLocal: "00:00",
  endLocal: "06:00",
  timezone: "",
};

/**
 * Narrow one night projection (a snapshot's `night_charge_state`, a toggle
 * 200's `night_charge_state`, or — through `patchNightChargeState` — a
 * `night_charge.state_changed` payload). Null when the value is not an object:
 * a garbage frame is never half-adopted. Absent fields inherit from `base`
 * when one is given (the event payload carries no `last_action` /
 * `last_tick_at`: those stay whatever the snapshot last said), else take their
 * honest defaults.
 */
export function toNightChargeState(
  value: unknown,
  base: NightChargeState | null = null,
): NightChargeState | null {
  if (!isRecord(value)) {
    return null;
  }
  const units = Array.isArray(value.units)
    ? value.units.map(toNightUnit).filter((unit): unit is NightUnitState => unit !== null)
    : (base?.units ?? []);
  return {
    enabled: typeof value.enabled === "boolean" ? value.enabled : (base?.enabled ?? false),
    enabledOrigin: oneOf(value.enabled_origin, ["config", "runtime"] as const, base?.enabledOrigin ?? "config"),
    acknowledgedPartition:
      typeof value.acknowledged_partition === "boolean"
        ? value.acknowledged_partition
        : (base?.acknowledgedPartition ?? false),
    posture:
      typeof value.posture === "string" && value.posture === "partition" ? "partition" : base?.posture === "partition" ? "partition" : "yield",
    active: typeof value.active === "boolean" ? value.active : (base?.active ?? false),
    phase: oneOf(value.phase, PHASES, base?.phase ?? "idle"),
    window: toNightWindow(value.window, base?.window ?? DEFAULT_WINDOW),
    windowEndsAt: optionalText(value.window_ends_at) ?? (value.window_ends_at === null ? null : base?.windowEndsAt ?? null),
    windowEndsInS:
      typeof value.window_ends_in_s === "number" && Number.isFinite(value.window_ends_in_s)
        ? value.window_ends_in_s
        : (base?.windowEndsInS ?? null),
    nextWindowAt: optionalText(value.next_window_at) ?? (value.next_window_at === null ? null : base?.nextWindowAt ?? null),
    pacing: oneOf(value.pacing, PACING_RULES, base?.pacing ?? "cap_first"),
    rateCapW: finite(value.rate_cap_w, base?.rateCapW ?? 0),
    holdRateW: finite(value.hold_rate_w, base?.holdRateW ?? 0),
    demandScope: oneOf(value.demand_scope, DEMAND_SCOPES, base?.demandScope ?? "fleet"),
    demandThresholdW: finite(value.demand_threshold_w, base?.demandThresholdW ?? 0),
    demandW: nullableFinite(value.demand_w, base?.demandW ?? null),
    demandEvidence: oneOf(value.demand_evidence, DEMAND_EVIDENCES, base?.demandEvidence ?? "missing"),
    heldIntentId: optionalText(value.held_intent_id) ?? (value.held_intent_id === null ? null : base?.heldIntentId ?? null),
    units,
    lastAction: text(value.last_action, base?.lastAction ?? "idle"),
    lastTickAt: text(value.last_tick_at, base?.lastTickAt ?? ""),
    reasonCodes: Array.isArray(value.reason_codes)
      ? stringList(value.reason_codes)
      : (base?.reasonCodes ?? []),
  };
}

/**
 * Apply one `night_charge.state_changed` payload onto the current projection:
 * the payload carries the state tuple, the units, and every watt/SOC figure
 * but NOT the tick bookkeeping (`last_action` / `last_tick_at`), so absent
 * fields keep the current values. A payload that is not an object changes
 * nothing.
 */
export function patchNightChargeState(
  current: NightChargeState | null,
  payload: unknown,
): NightChargeState | null {
  if (!isRecord(payload)) {
    return current;
  }
  return toNightChargeState(payload, current);
}

/** The parsed event: the patched projection plus the heartbeat flag itself. */
export interface NightChargeStateChangedEvent {
  state: NightChargeState;
  /** True on the 30 s republish: same tuple, fresher figures — silent by design. */
  heartbeat: boolean;
}

/**
 * Narrow one `night_charge.state_changed` payload for an event consumer: the
 * patched projection (against `current`, which may be null on a frame that
 * arrives before any snapshot) plus the payload's own `heartbeat` flag. Null
 * when the payload is not an object — an unusable frame is never half-adopted.
 */
export function toNightChargeStateChangedEvent(
  current: NightChargeState | null,
  payload: unknown,
): NightChargeStateChangedEvent | null {
  if (!isRecord(payload)) {
    return null;
  }
  const state = toNightChargeState(payload, current);
  if (state === null) {
    return null;
  }
  return { state, heartbeat: payload.heartbeat === true };
}

// --- countdowns (snapshot-derived instants, client-ticked) ---------------------

/** Seconds until an ISO instant from a monotonic now; null when unreadable. */
export function secondsUntil(instant: string | null, nowMs: number): number | null {
  if (instant === null) {
    return null;
  }
  const at = Date.parse(instant);
  if (Number.isFinite(at)) {
    return Math.max(0, Math.round((at - nowMs) / 1000));
  }
  return null;
}

/**
 * The window-end countdown in plain words: derived from the projection's own
 * `window_ends_at` instant (so it ticks with the caller's clock), falling back
 * to the snapshot-carried `window_ends_in_s` figure. Null when neither speaks.
 */
export function windowEndsInText(state: NightChargeState, nowMs: number): string | null {
  const derived = secondsUntil(state.windowEndsAt, nowMs);
  const seconds = derived ?? state.windowEndsInS;
  return seconds === null ? null : countdownText(seconds);
}

/**
 * The next-window countdown in plain words (the outside-window answer): the
 * projection's own `next_window_at` instant, ticked by the caller's clock.
 * Null when the projection carries no next window.
 */
export function nextWindowInText(state: NightChargeState, nowMs: number): string | null {
  const seconds = secondsUntil(state.nextWindowAt, nowMs);
  return seconds === null ? null : countdownText(seconds);
}

// --- plain-language maps (the codes are the wire, the sentences are the console) -

/**
 * One unit's row in the pacing sentence and the per-battery list: its target
 * in plain words keyed on its own phase. A skipped-full battery says "full,
 * sitting out" — the design's own wording — and a claimed/disarmed/headroom
 * sit-out says "sitting out" with its reason named in the row.
 */
export function nightUnitPhrase(unit: NightUnitState): string {
  switch (unit.phase) {
    case "pacing":
      return `${unit.unitId} ${formatWatts(unit.targetW)}`;
    case "holding_on_demand":
      return `${unit.unitId} held at ${formatWatts(unit.targetW)}`;
    case "skipped_full":
      return `${unit.unitId} full, sitting out`;
    case "complete":
      return `${unit.unitId} target reached`;
    default:
      return `${unit.unitId} sitting out`;
  }
}

/** A per-unit reason code in plain words; unknown codes render verbatim. */
export function nightUnitReasonText(reason: string): string {
  switch (reason) {
    case "on_plan":
      return "on plan";
    case "at_ceiling":
      return "at the charge ceiling";
    case "no_charge_headroom":
      return "no charge headroom — the battery's own limit says full";
    case "units_disarmed":
      return "disarmed";
    case "yielding_to_higher_priority":
      return "another request has this battery";
    case "target_reached":
      return "target reached";
    case "demand_above_threshold":
      return "demand hold";
    default:
      return reason;
  }
}

/** The per-unit evidence row: its own charge figure, target, and reason. */
export function nightUnitRowText(unit: NightUnitState): string {
  const soc =
    unit.socPct === null ? "charge level not available" : `${formatPercent(unit.socPct)} charged`;
  const plan = nightUnitPhrase(unit);
  const reason = unit.reason === "" ? "" : ` (${nightUnitReasonText(unit.reason)})`;
  return `${unit.unitId} — ${soc} · ${plan}${reason}`;
}

/**
 * The demand-reading line: the projection's own demand figure and threshold —
 * the LOAD-word reading, never the grid word — with the evidence word always
 * named (a non-good rollup is loudly visible, never a silent hold). A null
 * figure under failed evidence says "not available", never 0.
 */
export function nightDemandText(state: NightChargeState): string {
  const demand =
    state.demandW === null ? "not available" : formatWatts(state.demandW);
  const threshold =
    state.demandThresholdW > 0 ? `holds above ${formatWatts(state.demandThresholdW)}` : "";
  const parts = [`House demand ${demand}`];
  if (threshold !== "") {
    parts.push(threshold);
  }
  parts.push(`reading ${state.demandEvidence}`);
  return parts.join(" · ");
}

/**
 * The tile's one-line phase story for an ACTIVE window (the design §7 W2's own
 * wording): pacing names the per-battery targets toward the window's end,
 * holding names the demand rule's guarantee, complete names the moment, and
 * skipped_full states the window had nothing to charge.
 */
export function nightPhaseText(state: NightChargeState): string {
  const endLocal = state.window.endLocal;
  switch (state.phase) {
    case "pacing": {
      const units = state.units.map(nightUnitPhrase).join(" · ");
      return `Charging toward full by ${endLocal}${
        units === "" ? "" : `: ${units}`
      }.`;
    }
    case "holding_on_demand": {
      const demand =
        state.demandW === null
          ? `the demand reading is ${state.demandEvidence}`
          : `house demand ${formatWatts(state.demandW)}`;
      return `Holding — ${demand}: batteries neither drain nor cycle while the grid meets the spike.`;
    }
    case "complete": {
      const at = localTimeOfInstant(state.windowEndsAt);
      return `Batteries full — window complete${at === "" ? "" : ` at ${at}`}.`;
    }
    case "skipped_full":
      return "Batteries were already full — nothing to charge this window.";
    default:
      return nightReasonText(state);
  }
}

/**
 * The inactive story, rendered from the FIRST reason code in the pinned
 * vocabulary (§5's table; the codes are the wire, the sentences are the
 * console's). An unknown code is named verbatim, never silently dropped. The
 * builder takes the state so the sentence can carry its own figures.
 */
export function nightReasonText(state: NightChargeState): string {
  const code = state.reasonCodes[0];
  if (code === undefined) {
    return "Night charging is standing by.";
  }
  const window = `${state.window.startLocal}–${state.window.endLocal}`;
  switch (code) {
    case "outside_window": {
      const next = localTimeOfInstant(state.nextWindowAt);
      return `Outside the charging window (${window})${
        next === "" ? "" : ` — the next window opens at ${next}`
      }.`;
    }
    case "window_open":
      return `The charging window (${window}) is open.`;
    case "on_plan":
      return "Charging to plan.";
    case "deadline_at_risk":
      return "Behind the plan — charging at the cap to reach full by the window's end.";
    case "demand_above_threshold":
      return "House demand is above the hold line — batteries held, not cycling.";
    case "demand_below_exit":
      return "House demand has fallen back below the hold line — charging resumes.";
    case "demand_evidence_missing":
    case "demand_evidence_bad":
    case "demand_evidence_stale":
      // demand_w is null by contract under every one of these codes, and the
      // hold that preserves the no-cycling guarantee is LOUD, never silent.
      return `Demand reading ${state.demandEvidence} — holding so the batteries neither drain nor cycle.`;
    case "at_ceiling":
      return "At the charge ceiling — nothing to charge.";
    case "no_charge_headroom":
      return "The battery's own charge limit says full — nothing it will accept.";
    case "target_reached":
      return "Targets reached — nothing left to charge.";
    case "no_eligible_units":
      return "Window open, but no battery can charge (full, held by another request, or no headroom).";
    case "units_disarmed":
      // Rendered as the arm instruction it is (§7 W2): the runner can never
      // arm itself, and every controller restart needs a fresh arm.
      return "The batteries are disarmed — arm them before the window opens. Arming is a human act; the controller cannot arm itself, and every restart disarms again.";
    case "yielding_to_higher_priority":
      return "Standing down — another request has priority on the batteries.";
    case "disabled_by_config":
      return "Night charging is off (config).";
    case "disabled_by_runtime":
      return "Night charging is off until the controller restarts — the config's own setting takes over again at boot.";
    case "night_acknowledgement_required":
      return "Waiting on the one-time night-partition acknowledgement before night charging can start.";
    default:
      return `Night charging is standing down (${code}).`;
  }
}

/**
 * The tile's status sentence: an enabled window-state names its phase story;
 * everything else names its first reason code's sentence.
 */
export function nightStatusText(state: NightChargeState): string {
  if (state.enabled && state.phase !== "idle") {
    return nightPhaseText(state);
  }
  return nightReasonText(state);
}

/**
 * The window line: the open window's end countdown while one runs, else the
 * next window's opening countdown. Null when the projection carries neither.
 */
export function nightWindowLineText(state: NightChargeState, nowMs: number): string | null {
  const window = `${state.window.startLocal}–${state.window.endLocal}`;
  if (state.phase !== "idle") {
    const ends = windowEndsInText(state, nowMs);
    const clock = localTimeOfInstant(state.windowEndsAt);
    return `Window ${window}${clock === "" ? "" : ` ends ${clock}`}${
      ends === null ? "" : ` (in ${ends})`
    }`;
  }
  const next = localTimeOfInstant(state.nextWindowAt);
  const inS = nextWindowInText(state, nowMs);
  if (next === "" && inS === null) {
    return null;
  }
  return `Window ${window}${
    next === "" ? "" : ` — next opens ${next}`
  }${inS === null ? "" : ` (in ${inS})`}`;
}

/**
 * The toggle's current-state phrase from `enabled` + `enabled_origin` (the
 * excess toggle's four states, mirrored): the runtime origin is the honest
 * "until restart" marker — a runtime enable or disable is operational only
 * until the controller restarts and the config takes over again.
 */
export function nightToggleStateText(state: NightChargeState): string {
  if (state.enabled) {
    return state.enabledOrigin === "config" ? "On (config)" : "On — until restart";
  }
  return state.enabledOrigin === "config" ? "Off (config)" : "Off — until restart";
}

/**
 * The live-region announcement for one phase transition (the excess tile's
 * active-flip pattern): phase changes announce, heartbeats never do. Null when
 * the transition is not worth the operator's ear (no change, or the first
 * frame of a session).
 */
export function nightPhaseAnnouncement(
  previous: NightPhase,
  next: NightPhase,
): string | null {
  if (previous === next) {
    return null;
  }
  switch (next) {
    case "pacing":
      return previous === "idle"
        ? "Night charging started — pacing toward full."
        : "Night charging resumed — pacing toward full.";
    case "holding_on_demand":
      return "Night charging is holding — house demand is high; the batteries neither drain nor cycle.";
    case "complete":
      return "Night charging complete — the batteries are full.";
    case "skipped_full":
      return "The night window opened with nothing to charge — the batteries were already full.";
    default:
      // Into idle: the window ended or the feature stood down — the batteries
      // are back on their own autonomy either way (non-renewal hand-back).
      return "Night charging stood down — the batteries are back on their own.";
  }
}
