/**
 * The night-charge surface's shared wire model (DESIGN_NIGHT_CHARGE.md §5 +
 * DESIGN_NIGHT_CHARGE_V2.md §7 + API_CONTRACTS.md "Off-peak night charge"): the
 * `night_charge_state` snapshot projection, the `night_charge.state_changed`
 * bus event, and the plain-word maps every night surface (Home's Night tile,
 * the shell) shares. The backend family is LIVE; the whole surface still
 * renders nothing while the snapshot carries no `night_charge_state` (the
 * feature detection), and every parser is absent-tolerant per field.
 *
 * V2 (the forecast-aware top-up target, CONTRACT v2): the projection gains
 * FIVE ADDITIVE keys — `target_policy`, `trust`, `forecast`, `explanation`,
 * `morning_notice` — emitted ONLY under a forecast posture (never under
 * `full`, so v1 consumers see byte-identical frames); the per-unit rows gain
 * `target_soc_pct` (act) / `suggested_target_soc_pct` (suggest); the reason
 * vocabulary gains §3.3's four ladder words and §5.4's honest close. Every V2
 * render follows the design's own honesty laws: a displayed number that does
 * not govern SAYS SO beside itself (the suggest banner); every fallback state
 * names its word AND the v1 charge it lands on ("charging full tonight" —
 * never a mysterious v1-shaped night); targets stop at the ceiling because
 * the pods top the last few percent themselves; the trust line is EVIDENCE
 * (days/regime mix), never a verdict below the required days; and a window
 * that closed below target leaves the A5 morning notice until midday.
 *
 * Wire truth pinned here (the docs govern):
 * - Projection: `{enabled, enabled_origin, acknowledged_partition, posture,
 *   active, phase, window{start_local,end_local,timezone}, window_ends_at,
 *   window_ends_in_s, next_window_at, pacing, rate_cap_w, hold_rate_w,
 *   demand_scope, demand_threshold_w, demand_w, demand_evidence,
 *   held_intent_id, units[{unit_id, soc_pct, phase, target_w, reason,
 *   target_soc_pct | suggested_target_soc_pct}], last_action, last_tick_at,
 *   reason_codes, target_policy?, trust?, forecast?, explanation?,
 *   morning_notice?}` — `active` derives from `held_intent_id`, never a
 *   lifecycle guess.
 * - Fleet `phase`: idle | pacing | holding_on_demand | standing_by_on_demand |
 *   complete | skipped_full; per-unit adds `sitting_out` (claimed, disarmed, or
 *   no headroom this tick). `standing_by_on_demand` is THE demand response: a
 *   MEASURED demand above the threshold stands the unit down entirely (zero-
 *   watt non-participation — excluded from the submission, the pod back on its
 *   own autonomy until demand falls back or the window ends). `holding_on_demand`
 *   is the fail-closed FALLBACK ONLY: missing/bad/stale evidence holds at
 *   `hold_rate_w` (the no-cycling guarantee — standby answers measured demand,
 *   never missing data). There is no `demand_response` selector on the wire.
 * - `demand_evidence`: good | missing | bad | stale, worst-word-wins; a
 *   non-good rollup FAILS CLOSED TO HOLD (the no-cycling guarantee) and is
 *   loudly visible — never a silent never-charges.
 * - Reason vocabulary (ONE): outside_window, window_open, on_plan,
 *   deadline_at_risk, demand_above_threshold, demand_below_exit,
 *   demand_evidence_missing, demand_evidence_bad, demand_evidence_stale,
 *   at_ceiling, no_charge_headroom, target_reached, no_eligible_units,
 *   units_disarmed, yielding_to_higher_priority, disabled_by_config,
 *   disabled_by_runtime, night_acknowledgement_required, forecast_missing,
 *   forecast_stale, forecast_no_load_baseline, forecast_below_trust,
 *   window_closed_below_target.
 * - Event `night_charge.state_changed`: published when the semantic tuple
 *   `(enabled, enabled_origin, acknowledged_partition, active, phase,
 *   active_unit_ids, demand_evidence, reason_codes, target_policy,
 *   trust.state)` changes — the last two are V2's exactly-two new members;
 *   watts/SOC/targets ride but never trigger — with a 30 s `"heartbeat": true`
 *   republish while enabled and NOTHING while disabled. Payload: the
 *   projection subset minus `last_action`/`last_tick_at`, plus `heartbeat`.
 */
import { formatDecimal, formatPercent, formatWatts } from "../lib/format";
import { isRecord } from "./fleet";
import { countdownText, localTimeOfInstant, type SchedulePosture } from "./schedule";

/** The bus vocabulary the night surface consumes (its one event type). */
export const NIGHT_CHARGE_STATE_CHANGED_EVENT = "night_charge.state_changed" as const;

/** The fleet-level phase vocabulary (§5's ONE list, verbatim). */
export type NightPhase =
  | "idle"
  | "pacing"
  | "holding_on_demand"
  | "standing_by_on_demand"
  | "complete"
  | "skipped_full";

/** The runtime checklist of the fleet-level list (an unknown phase narrows to idle). */
export const NIGHT_PHASES: readonly NightPhase[] = [
  "idle",
  "pacing",
  "holding_on_demand",
  "standing_by_on_demand",
  "complete",
  "skipped_full",
];

/**
 * The per-unit phase vocabulary: the fleet list plus `sitting_out`.
 * `standing_by_on_demand` is the MEASURED-demand stand-down (zero-watt
 * non-participation); `holding_on_demand` is the fail-closed hold at
 * `hold_rate_w` when the evidence word did not hold.
 */
export type NightUnitPhase =
  | "pacing"
  | "holding_on_demand"
  | "standing_by_on_demand"
  | "skipped_full"
  | "complete"
  | "sitting_out";

/** The runtime checklist of the per-unit list (an unknown phase narrows to sitting_out). */
export const NIGHT_UNIT_PHASES: readonly NightUnitPhase[] = [
  "pacing",
  "holding_on_demand",
  "standing_by_on_demand",
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

/**
 * V2 §3.1's ONE target-policy key, THREE states: `full` is v1 identity (and
 * the state an absent key means — the decoder's default); `forecast_suggest`
 * computes and DISPLAYS targets but charges v1-full; `forecast_act` lets the
 * computed target govern. The console can never flip this key; there is
 * deliberately no route.
 */
export type NightTargetPolicy = "full" | "forecast_suggest" | "forecast_act";

/**
 * V2 §3.2's trust scoreboard states: `provisioning` is the honest not-enough-
 * days-yet word (never a verdict), `earned` is the compound gate's pass, and
 * `suspended` is a previously-earned trust that breached — ACT demotes itself
 * to full targets, loudly, until the window re-earns.
 */
export type NightTrustState = "provisioning" | "earned" | "suspended";

/**
 * V2 §7's forecast-frame statuses: `ok` (the live arithmetic) or one of §3.3's
 * four fallback rungs — every one of which lands on the v1 full charge, so a
 * fallback night is never mysterious.
 */
export type NightForecastStatus =
  | "ok"
  | "forecast_missing"
  | "forecast_stale"
  | "forecast_no_load_baseline"
  | "forecast_below_trust";

const PHASES: readonly NightPhase[] = NIGHT_PHASES;
const UNIT_PHASES: readonly NightUnitPhase[] = NIGHT_UNIT_PHASES;
const DEMAND_EVIDENCES: readonly NightDemandEvidence[] = ["good", "missing", "bad", "stale"];
const PACING_RULES: readonly NightPacingRule[] = ["cap_first", "even"];
const DEMAND_SCOPES: readonly NightDemandScope[] = ["fleet", "per_phase"];
const TARGET_POLICIES: readonly NightTargetPolicy[] = ["full", "forecast_suggest", "forecast_act"];
const TRUST_STATES: readonly NightTrustState[] = ["provisioning", "earned", "suspended"];
const FORECAST_STATUSES: readonly NightForecastStatus[] = [
  "ok",
  "forecast_missing",
  "forecast_stale",
  "forecast_no_load_baseline",
  "forecast_below_trust",
];

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
  // V2 (§3.3's ladder + §5.4's honest close), additive.
  "forecast_missing",
  "forecast_stale",
  "forecast_no_load_baseline",
  "forecast_below_trust",
  "window_closed_below_target",
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
  /**
   * V2 §7's per-unit SOC target — the fleet-wide number (the §2.2 share
   * collapse makes it one percentage for every battery). The wire key is
   * `target_soc_pct` under ACT and `suggested_target_soc_pct` under SUGGEST (a
   * number that does not govern says so in its own key); null when the frame
   * carried neither (a `full` posture or a fallback window).
   */
  targetSocPct: number | null;
}

function toNightUnit(value: unknown): NightUnitState | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.unit_id !== "string" || value.unit_id === "") {
    return null;
  }
  const socTarget =
    typeof value.target_soc_pct === "number" && Number.isFinite(value.target_soc_pct)
      ? value.target_soc_pct
      : typeof value.suggested_target_soc_pct === "number" &&
          Number.isFinite(value.suggested_target_soc_pct)
        ? value.suggested_target_soc_pct
        : null;
  return {
    unitId: value.unit_id,
    socPct:
      typeof value.soc_pct === "number" && Number.isFinite(value.soc_pct) ? value.soc_pct : null,
    phase: oneOf(value.phase, UNIT_PHASES, "sitting_out"),
    targetW: Math.max(0, finite(value.target_w, 0)),
    reason: text(value.reason),
    targetSocPct: socTarget,
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
 * V2 §3.2's trust scoreboard block — the EVIDENCE the promotion gate reads,
 * rendered as evidence: the state word, the day counts, the rolling figures
 * (null before any scored window), and the A4 regime mix. The console never
 * derives a verdict from these figures; it renders the wire's own state word.
 */
export interface NightTrust {
  state: NightTrustState;
  /** The store's total scored days (the §7 example carries 19 of a required 14). */
  daysScored: number;
  requiredDays: number;
  meanAbsErrPct: number | null;
  /** Signed: positive = over-forecast (the dangerous, tighter-bound direction). */
  biasPct: number | null;
  lowSurplusDays: number;
  highSurplusDays: number;
}

function toNightTrust(value: unknown): NightTrust | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    state: oneOf(value.state, TRUST_STATES, "provisioning"),
    daysScored: Math.max(0, Math.round(finite(value.days_scored, 0))),
    requiredDays: Math.max(0, Math.round(finite(value.required_days, 0))),
    meanAbsErrPct:
      typeof value.mean_abs_err_pct === "number" && Number.isFinite(value.mean_abs_err_pct)
        ? value.mean_abs_err_pct
        : null,
    biasPct:
      typeof value.bias_pct === "number" && Number.isFinite(value.bias_pct)
        ? value.bias_pct
        : null,
    lowSurplusDays: Math.max(0, Math.round(finite(value.low_surplus_days, 0))),
    highSurplusDays: Math.max(0, Math.round(finite(value.high_surplus_days, 0))),
  };
}

/**
 * V2 §7's forecast frame. `status: "ok"` carries the morning-credit arithmetic
 * actually used (net surplus/deficit/credit kWh, the midday finish line, the
 * A10 `ceilingBoundBy` decomposition, and the provider provenance); every
 * other status is one of §3.3's fallback rungs — the word rides with the
 * commissioned quantile and midday and NO figures (a fallback night leaves
 * nothing to misread, and the charge lands on v1-full).
 */
export interface NightForecast {
  status: NightForecastStatus;
  /** The provider's own name (e.g. "solcast"); null when unknown. */
  source: string | null;
  /** The DRIVER slice (p10 by commission); the p50 slice explains elsewhere. */
  quantile: number | null;
  issuedAt: string | null;
  fetchedAt: string | null;
  eSurplusKwh: number | null;
  eDeficitKwh: number | null;
  /** η·surplus − deficit: the one number the target arithmetic turns on. */
  eCreditKwh: number | null;
  /** The operator's declared finish line (civil, "12:00" by commission). */
  middayLocal: string;
  /** A10's decomposition: "sky" | "netting" when the target clamped to the ceiling. */
  ceilingBoundBy: "sky" | "netting" | null;
}

function toNightForecast(value: unknown): NightForecast | null {
  if (!isRecord(value)) {
    return null;
  }
  const status = oneOf(value.status, FORECAST_STATUSES, "ok");
  if (typeof value.status !== "string" || value.status !== status) {
    // An unreadable status word is never half-adopted as a live computation.
    return null;
  }
  return {
    status,
    source: optionalText(value.source),
    quantile:
      typeof value.quantile === "number" && Number.isFinite(value.quantile) ? value.quantile : null,
    issuedAt: optionalText(value.issued_at),
    fetchedAt: optionalText(value.fetched_at),
    eSurplusKwh:
      typeof value.e_surplus_kwh === "number" && Number.isFinite(value.e_surplus_kwh)
        ? value.e_surplus_kwh
        : null,
    eDeficitKwh:
      typeof value.e_deficit_kwh === "number" && Number.isFinite(value.e_deficit_kwh)
        ? value.e_deficit_kwh
        : null,
    eCreditKwh:
      typeof value.e_credit_kwh === "number" && Number.isFinite(value.e_credit_kwh)
        ? value.e_credit_kwh
        : null,
    middayLocal: text(value.midday_local, ""),
    ceilingBoundBy: value.ceiling_bound_by === "sky" || value.ceiling_bound_by === "netting"
      ? value.ceiling_bound_by
      : null,
  };
}

/**
 * V2 §5.4/A5's morning notice: a forecast window that closed below target
 * leaves this on the projection until `middayLocal` (the backend clears it at
 * the line; `untilLocal` restates it). "Solar is finishing what it can; the
 * landing is visible after midday" — the scoreboard prices the miss.
 */
export interface NightMorningNotice {
  /** The civil morning the closed window belonged to (ISO date). */
  date: string;
  targetSocPct: number | null;
  unitsBelowTarget: string[];
  untilLocal: string;
}

function toNightMorningNotice(value: unknown): NightMorningNotice | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    date: text(value.date, ""),
    targetSocPct:
      typeof value.target_soc_pct === "number" && Number.isFinite(value.target_soc_pct)
        ? value.target_soc_pct
        : null,
    unitsBelowTarget: Array.isArray(value.units_below_target)
      ? value.units_below_target.filter(
          (entry): entry is string => typeof entry === "string" && entry !== "",
        )
      : [],
    untilLocal: text(value.until_local, ""),
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
  // --- V2 (§7, additive): the five keys below are ABSENT on the wire under
  // `full` — the absent key is the feature detection, and the honest defaults
  // below keep a v1-shaped parse exactly v1. ---
  /** The commissioned target policy; "full" for an absent key (v1 identity). */
  targetPolicy: NightTargetPolicy;
  /** The trust scoreboard; null when the wire carries no block. */
  trust: NightTrust | null;
  /** The forecast frame (live arithmetic or a fallback rung); null when absent. */
  forecast: NightForecast | null;
  /** §8's one-sentence arithmetic, verbatim from the projection; null when absent. */
  explanation: string | null;
  /** The A5 below-target close notice, latched until midday; null when absent. */
  morningNotice: NightMorningNotice | null;
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
    // --- V2: the backend emits these five keys ONLY under a forecast posture,
    // so KEY-PRESENCE is the truth — an absent key inherits the base (a v1
    // event never erases a V2 snapshot's facts), an explicit null WINS (the
    // frame's own honest "no trust view / no notice this tick" answer). ---
    targetPolicy: oneOf(value.target_policy, TARGET_POLICIES, base?.targetPolicy ?? "full"),
    trust: "trust" in value ? toNightTrust(value.trust) : (base?.trust ?? null),
    forecast:
      "forecast" in value ? toNightForecast(value.forecast) : (base?.forecast ?? null),
    explanation:
      "explanation" in value
        ? optionalText(value.explanation)
        : (base?.explanation ?? null),
    morningNotice:
      "morning_notice" in value
        ? toNightMorningNotice(value.morning_notice)
        : (base?.morningNotice ?? null),
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
 * sitting out" — the design's own wording — a measured-demand stand-down says
 * "standing by" (the zero-watt non-participation named as what it is), and a
 * claimed/disarmed/headroom sit-out says "sitting out" with its reason named
 * in the row.
 */
export function nightUnitPhrase(unit: NightUnitState): string {
  switch (unit.phase) {
    case "pacing":
      return `${unit.unitId} ${formatWatts(unit.targetW)}`;
    case "holding_on_demand":
      return `${unit.unitId} held at ${formatWatts(unit.targetW)}`;
    case "standing_by_on_demand":
      return `${unit.unitId} standing by`;
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
    default:
      return reason;
  }
}

/**
 * One unit's reason in plain words, phase-aware where the wire shares ONE code
 * across behaviors: `demand_above_threshold` rides BOTH demand arms — a
 * stand-by answers MEASURED demand ("house demand high"), while the hold at
 * `hold_rate_w` is the fail-closed fallback a failed evidence word triggered
 * ("the demand reading did not hold" — never a demand figure the wire does
 * not have).
 */
function unitReasonText(unit: NightUnitState): string {
  if (unit.reason === "demand_above_threshold") {
    return unit.phase === "standing_by_on_demand"
      ? "house demand high"
      : "the demand reading did not hold";
  }
  return nightUnitReasonText(unit.reason);
}

/**
 * The per-unit evidence row: its own charge figure, target, and reason. Under
 * a forecast posture the row carries the V2 target beside the SOC (§8's
 * "target vs SOC" — the §2.2 collapse makes it the fleet's one number); the
 * row never claims whether the number governs, because the tile's own banner
 * beside it says so (the suggest MUST-SAY-SO law).
 */
export function nightUnitRowText(unit: NightUnitState): string {
  const soc =
    unit.socPct === null ? "charge level not available" : `${formatPercent(unit.socPct)} charged`;
  const target =
    unit.targetSocPct === null ? "" : ` · target ${formatPercent(unit.targetSocPct)}`;
  const plan = nightUnitPhrase(unit);
  const reason = unit.reason === "" ? "" : ` (${unitReasonText(unit)})`;
  return `${unit.unitId} — ${soc}${target} · ${plan}${reason}`;
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
 * standing by names the measured-demand stand-down with its honest trade (the
 * pods answer the house on their own), holding names the fail-closed
 * guarantee, complete names the moment, and skipped_full states the window had
 * nothing to charge.
 */
export function nightPhaseText(state: NightChargeState): string {
  const endLocal = state.window.endLocal;
  switch (state.phase) {
    case "pacing": {
      const units = state.units.map(nightUnitPhrase).join(" · ");
      // Under ACT a live forecast target GOVERNS, so the story names the number
      // actually being charged toward. Under SUGGEST the submission math is
      // v1's (the ceiling), so the story keeps saying "full" — the suggested
      // number says so beside itself in its own line, never here.
      const governing =
        state.targetPolicy === "forecast_act" ? fleetTargetSocPct(state) : null;
      const toward =
        governing === null ? "full" : `${formatPercent(governing)} (the rest by solar)`;
      return `Charging toward ${toward} by ${endLocal}${
        units === "" ? "" : `: ${units}`
      }.`;
    }
    case "standing_by_on_demand": {
      const demand =
        state.demandW === null
          ? `the demand reading is ${state.demandEvidence}`
          : `house demand ${formatWatts(state.demandW)}`;
      return `Standing by — ${demand}: the batteries stand down at zero watts and the pods answer the house on their own until demand falls back.`;
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
      return "House demand is above the hold line — batteries stood down until it falls back.";
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
    // --- V2 §3.3's ladder: every rung lands on the v1 full charge, LOUDLY —
    // "charging full tonight" is the umbrella doctrine's own polarity (bad
    // foresight must not stop the CHARGE, only the discount), so a
    // v1-behaving night is never mysterious. ---
    case "forecast_missing":
      return "No forecast covers this morning — charging full tonight (the v1 charge).";
    case "forecast_stale":
      return "The forecast is too old to steer with — charging full tonight (the v1 charge).";
    case "forecast_no_load_baseline":
      return "No load baseline for the morning — charging full tonight (the v1 charge).";
    case "forecast_below_trust":
      return "Forecast trust has not been earned — charging full tonight (the v1 charge).";
    case "window_closed_below_target":
      // §5.4/A5's honest close: lost window time is unrecoverable; the morning
      // notice on the tile carries the landing story until midday.
      return "The window closed below target — solar is finishing what it can.";
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
    case "standing_by_on_demand":
      return "Night charging is standing by — house demand is high; the batteries stand down until it passes.";
    case "holding_on_demand":
      // The fail-closed fallback: the evidence word did not hold, so the
      // announcement never claims a demand figure it does not have.
      return "Night charging is holding — the demand reading did not hold, so the batteries neither drain nor cycle.";
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

// --- V2 plain-language maps (the forecast-aware target's own tile lines) ------

/**
 * The one fleet-wide SOC target on the frame (§2.2's capacity-proportional
 * share collapses the arithmetic to ONE percentage for every battery): the
 * first unit row that carries it. Null when no row does (a `full` posture, a
 * fallback window, or an outside-window frame).
 */
export function fleetTargetSocPct(state: NightChargeState): number | null {
  for (const unit of state.units) {
    if (unit.targetSocPct !== null) {
      return unit.targetSocPct;
    }
  }
  return null;
}

/**
 * §8's 95-vs-100 clause, pinned once: targets stop at the charge ceiling
 * because the pods' own autonomy carries the last few percent to 100 — the
 * span above the ceiling belongs to the pods and the excess adviser, never to
 * this adviser's writes.
 */
export const NIGHT_CEILING_CLAUSE =
  "targets stop at the ceiling — the pods top the last few percent themselves";

/**
 * The tile's suggest/act line (§8's target line): the fleet's one target with
 * the §2.1 arithmetic that produced it and the 95-vs-100 clause standing
 * beside it. `Suggested` under SUGGEST — the label itself is part of the
 * MUST-SAY-SO law (a displayed number that does not govern says so beside
 * itself; the banner below completes it). Null when the frame carries no live
 * computation (a `full` posture, a fallback window, or no per-unit target).
 */
export function nightTargetLineText(state: NightChargeState): string | null {
  if (state.targetPolicy === "full") {
    return null;
  }
  const forecast = state.forecast;
  const target = fleetTargetSocPct(state);
  if (forecast === null || forecast.status !== "ok" || target === null) {
    return null;
  }
  const label = state.targetPolicy === "forecast_suggest" ? "Suggested target" : "Target";
  const midday = forecast.middayLocal === "" ? "" : ` by ${forecast.middayLocal}`;
  const credit =
    forecast.eCreditKwh === null
      ? ""
      : ` — ${formatDecimal(forecast.eCreditKwh)} kWh forecast surplus${midday} finishes it`;
  return `${label}: ${formatPercent(target)}${credit}. ${capitalize(NIGHT_CEILING_CLAUSE)}.`;
}

/**
 * §8's suggest banner, VERBATIM: under `forecast_suggest` the submission math
 * is byte-identical to v1 (the ceiling), and the displayed number must say so
 * beside itself — promotion is the operator's config revision, never a
 * console action (there is deliberately no route).
 */
export const NIGHT_SUGGEST_BANNER_TEXT =
  "Showing forecast targets — charging to 95% (v1) until trust is earned; promotion is a config revision";

/**
 * §3.3's fallback words, one per ladder rung: each names its honest cause AND
 * the v1 charge the window lands on ("charging full tonight"), so a
 * v1-behaving night under a live forecast posture is legible as such — never
 * silence, never mystery. Null while the frame carries a live computation.
 */
export function nightForecastFallbackText(forecast: NightForecast): string | null {
  switch (forecast.status) {
    case "forecast_missing":
      return "No forecast covers this morning — charging full tonight (the v1 charge).";
    case "forecast_stale":
      return "The forecast is too old to steer with — charging full tonight (the v1 charge).";
    case "forecast_no_load_baseline":
      return "No load baseline for the morning — charging full tonight (the v1 charge).";
    case "forecast_below_trust":
      return "Forecast trust has not been earned — charging full tonight (the v1 charge).";
    default:
      return null;
  }
}

/**
 * A10's decomposition line: when the computed target clamped to the ceiling,
 * the tile says WHICH term bound it — the sky gave no surplus at all, or a
 * real surplus was eaten by the deficit/η netting. Null when the target sits
 * strictly between floor and ceiling (`ceiling_bound_by` is null on the wire).
 */
export function nightDecompositionText(forecast: NightForecast): string | null {
  if (forecast.status !== "ok" || forecast.ceilingBoundBy === null) {
    return null;
  }
  return forecast.ceilingBoundBy === "sky"
    ? "Charging to full — the sky gave no surplus worth leaving room for."
    : "Charging to full — the morning deficit bound it, not the sky.";
}

/** A signed percentage for the bias figure (positive = over-forecast). */
function signedPercent(value: number): string {
  return `${value > 0 ? "+" : ""}${formatPercent(value)}`;
}

/**
 * §8's trust line, rendered as the EVIDENCE it is (the regime counts ride it,
 * A4 — a passing mean over one kind of sky must not look like evidence), and
 * never a verdict below the required days: `provisioning` says so in as many
 * words, `earned` names the pass, and `suspended` names the loud demotion to
 * full targets until the rolling window re-earns.
 */
export function nightTrustText(trust: NightTrust): string {
  const days = `${trust.daysScored}/${trust.requiredDays} days scored`;
  const mean =
    trust.meanAbsErrPct === null ? "" : ` · mean err ${formatPercent(trust.meanAbsErrPct)}`;
  const bias = trust.biasPct === null ? "" : ` · bias ${signedPercent(trust.biasPct)}`;
  const regime = ` · ${trust.lowSurplusDays} low / ${trust.highSurplusDays} high mornings`;
  if (trust.state === "earned") {
    return `Forecast trust: earned — ${days}${mean}${bias}${regime}.`;
  }
  if (trust.state === "suspended") {
    return `Forecast trust: SUSPENDED — ${days}${mean}${bias}${regime}; charging full until the rolling window re-earns it.`;
  }
  const notVerdict =
    trust.requiredDays > 0 ? ` (not a verdict until ${trust.requiredDays} days)` : "";
  return `Forecast trust: provisioning — ${days}${mean}${bias}${regime}${notVerdict}.`;
}

/**
 * §8/A5's morning notice: the window closed below target, solar is finishing
 * what it can, and the landing is visible after midday (the notice rides the
 * projection until the midday line — the backend clears it; `untilLocal`
 * restates the line so the console can say it).
 */
export function nightMorningNoticeText(notice: NightMorningNotice): string {
  const below = notice.unitsBelowTarget.join(", ");
  const target =
    notice.targetSocPct === null ? "" : ` under ${formatPercent(notice.targetSocPct)}`;
  const who = below === "" ? "" : ` (${below}${target})`;
  const until = notice.untilLocal === "" ? "" : ` (until ${notice.untilLocal})`;
  return `Ended the night below target${who} — solar is finishing what it can; landing visible after midday${until}.`;
}

/**
 * §8's reasoning sentence: the projection's own `explanation` VERBATIM, with
 * the age of the forecast beside it (§2.6's provider honesty — a cached value
 * never freshens by rereading, and the operator sees how old the steer is).
 * Null when the frame carries no sentence.
 */
export function nightExplanationText(
  state: NightChargeState,
  nowMs: number,
): string | null {
  const sentence = state.explanation;
  if (sentence === null || sentence === "") {
    return null;
  }
  const forecast = state.forecast;
  if (forecast === null || forecast.status !== "ok" || forecast.fetchedAt === null) {
    return sentence;
  }
  const at = Date.parse(forecast.fetchedAt);
  if (!Number.isFinite(at)) {
    return sentence;
  }
  const ageS = Math.max(0, Math.round((nowMs - at) / 1000));
  // The countdown formatter's plain words ("26 min", "1 h 5 min") — an age and
  // a countdown read the same, never a bare four-digit second count.
  return `${sentence} (forecast fetched ${countdownText(ageS)} ago)`;
}

/** The tile's line-initial capital for a pinned lowercase clause. */
function capitalize(sentence: string): string {
  return sentence === "" ? sentence : sentence[0]!.toUpperCase() + sentence.slice(1);
}
