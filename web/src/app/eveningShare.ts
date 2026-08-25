/**
 * The evening load-sharing program's shared wire model
 * (DESIGN_EVENING_LOAD_SHARING.md §8/§9, CONTRACT v1.1): the
 * `evening_load_share_state` snapshot projection plus the plain-word maps the
 * Home Evening Sharing card renders. The program is the netted-meter fleet
 * discharge adviser — all three phases contribute to the one load the meter
 * sees, SoC²-weighted, engagement on WORK not import — and its own sibling
 * of the calibration card (own block, own window, own vocabulary; the cards
 * share the maintenance row and nothing else).
 *
 * Wire truth pinned here (§8.3's exact shape):
 * - Projection: `{mode, submits?, window{opens_local, ends_local}, phase,
 *   as_of, engaged, work_w?, net_exchange_w?, elsewhere_w?,
 *   within_tolerance, commanded_total_w?, derate, reason_codes[], units[],
 *   held_intent_id, pinned_sentence, stop_route, residual_import_w?,
 *   last_submission?, degraded_note?, last_close?}`. `mode: advise` renders
 *   the whole projection with `submits: "never"` named beside it — a
 *   displayed plan that does not act MUST say so beside itself.
 * - `phase`: idle | sharing | capability_limited | withdrawn.
 * - `within_tolerance` is §3.4's deadband state and NOTHING else (E9): true
 *   when the signed exchange sits inside [−spill_tolerance_w,
 *   +import_tolerance_w] — a display word, never a stop.
 * - `units[]`: `{unit_id, soc_pct, weight, share_w, phase, reason, note?,
 *   delivery_below_pass?}` — skips render their reason verbatim, never
 *   silently.
 * - The E6 boot degrade rides `degraded_note` when an act block's recorded
 *   netting evidence file was missing at boot.
 *
 * Honesty rules (§9's own): the pinned §0 sentence and the C16 stop-route
 * line ride the card's engaged state wherever it renders; and
 * uncommissioned renders nothing at all (the card keys on the snapshot key's
 * own absence).
 */
import { isRecord } from "./fleet";

/** The bus vocabulary the program publishes (§8.2). */
export const EVENING_STATE_EVENT = "evening.state_changed" as const;

/** §0's pinned sentence, VERBATIM — it rides every engaged-state surface. */
export const EVENING_PINNED_SENTENCE =
  "The meter nets — every commanded watt exists to cancel a metered watt; convergence is the weighting's side-effect, and no watt is ever exported for it.";

/** The C16 stop-route line, VERBATIM — it rides wherever the live share renders. */
export const EVENING_STOP_ROUTE =
  "to stop tonight's sharing, claim any pod — a manual command preempts instantly";

/** The program's per-tick fleet phase (§8.3). */
export type EveningPhase = "idle" | "sharing" | "capability_limited" | "withdrawn";

export const EVENING_PHASES: readonly EveningPhase[] = [
  "idle",
  "sharing",
  "capability_limited",
  "withdrawn",
];

/** One pod's per-tick row: the share with its SoC/weight, or its reason. */
export interface EveningUnitState {
  unitId: string;
  socPct: number | null;
  weight: number | null;
  shareW: number;
  phase: "sharing" | "sitting_out";
  reason: string;
  note: string | null;
  deliveryBelowPass: number | null;
}

/** §8.1's close record — the last evening's morning facts. */
export interface EveningCloseState {
  night: string;
  servedWh: Record<string, number>;
  importWh: number;
  spillWh: number;
  convergenceOpen: number | null;
  convergenceClose: number | null;
  money: Record<string, number> | null;
  closeSentence: string | null;
  displacedEstimateWh: number | null;
  displacedProvenance: string | null;
}

export interface EveningShareState {
  mode: string;
  submits: string | null;
  window: { opensLocal: string; endsLocal: string };
  phase: EveningPhase;
  asOf: string;
  engaged: boolean;
  workW: number | null;
  netExchangeW: number | null;
  elsewhereW: number | null;
  withinTolerance: boolean;
  commandedTotalW: number | null;
  derate: number;
  reasonCodes: string[];
  units: EveningUnitState[];
  heldIntentId: string | null;
  residualImportW: number | null;
  degradedNote: string | null;
  lastClose: EveningCloseState | null;
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

function toUnit(value: unknown): EveningUnitState | null {
  if (!isRecord(value) || typeof value.unit_id !== "string" || value.unit_id === "") {
    return null;
  }
  return {
    unitId: value.unit_id,
    socPct: optionalNumber(value.soc_pct),
    weight: optionalNumber(value.weight),
    shareW: optionalNumber(value.share_w) ?? 0,
    phase: value.phase === "sharing" ? "sharing" : "sitting_out",
    reason: text(value.reason, "sitting_out"),
    note: optionalText(value.note),
    deliveryBelowPass: optionalNumber(value.delivery_below_pass),
  };
}

function toClose(value: unknown): EveningCloseState | null {
  if (!isRecord(value) || typeof value.night !== "string") {
    return null;
  }
  const convergence = isRecord(value.convergence_delta_pct)
    ? value.convergence_delta_pct
    : {};
  const served = isRecord(value.served_wh)
    ? (Object.fromEntries(
        Object.entries(value.served_wh).filter(
          (entry): entry is [string, number] => typeof entry[1] === "number",
        ),
      ) as Record<string, number>)
    : {};
  const money = isRecord(value.money)
    ? (Object.fromEntries(
        Object.entries(value.money).filter(
          (entry): entry is [string, number] => typeof entry[1] === "number",
        ),
      ) as Record<string, number>)
    : null;
  return {
    night: value.night,
    servedWh: served,
    importWh: optionalNumber(value.import_wh) ?? 0,
    spillWh: optionalNumber(value.spill_wh) ?? 0,
    convergenceOpen: optionalNumber(convergence.open),
    convergenceClose: optionalNumber(convergence.close),
    money,
    closeSentence: optionalText(value.close_sentence),
    displacedEstimateWh: optionalNumber(value.displaced_import_estimate_wh),
    displacedProvenance: optionalText(value.displaced_import_estimate_provenance),
  };
}

/**
 * Narrow one evening projection (a snapshot's `evening_load_share_state` or
 * the status route's body). Null when the value is not an object: a garbage
 * frame is never half-adopted.
 */
export function toEveningShareState(value: unknown): EveningShareState | null {
  if (!isRecord(value)) {
    return null;
  }
  const units = Array.isArray(value.units)
    ? (value.units as unknown[])
        .map(toUnit)
        .filter((unit): unit is EveningUnitState => unit !== null)
    : [];
  const window = isRecord(value.window) ? value.window : {};
  return {
    mode: text(value.mode, "advise"),
    submits: optionalText(value.submits),
    window: {
      opensLocal: text(window.opens_local, "16:00"),
      endsLocal: text(window.ends_local, "22:30"),
    },
    phase: EVENING_PHASES.includes(value.phase as EveningPhase)
      ? (value.phase as EveningPhase)
      : "idle",
    asOf: text(value.as_of),
    engaged: value.engaged === true,
    workW: optionalNumber(value.work_w),
    netExchangeW: optionalNumber(value.net_exchange_w),
    elsewhereW: optionalNumber(value.elsewhere_w),
    withinTolerance: value.within_tolerance === true,
    commandedTotalW: optionalNumber(value.commanded_total_w),
    derate: optionalNumber(value.derate) ?? 1.16,
    reasonCodes: Array.isArray(value.reason_codes)
      ? (value.reason_codes as unknown[]).filter(
          (code): code is string => typeof code === "string",
        )
      : [],
    units,
    heldIntentId: optionalText(value.held_intent_id),
    residualImportW: optionalNumber(value.residual_import_w),
    degradedNote: optionalText(value.degraded_note),
    lastClose: toClose(value.last_close),
  };
}

// --- plain-language maps ---------------------------------------------------------

/** One skip reason in plain words (§7.1's set, rendered verbatim). */
export function eveningReasonText(reason: string): string {
  switch (reason) {
    case "on_plan":
      return "on plan";
    case "optimizer_claim":
      return "claimed by another adviser — free surplus or a sibling traverse outranks paid support";
    case "under_intent":
      return "under a manual or agent command (preempts instantly)";
    case "schedule_claim":
      return "claimed by the published schedule";
    case "claim_settling":
      return "claim settling — re-entering after the 20 s debounce";
    case "not_delivering":
      return "not delivering its commanded share";
    case "soc_floor":
      return "at the participation floor — dropped out, share redistributed";
    case "unit_disarmed":
      return "disarmed (dispatch requires arm; the program never arms)";
    case "unit_parked":
      return "parked";
    case "latched_stop":
      return "under a latched stop";
    case "share_below_floor":
      return "sitting out — a share below the 500 W efficiency floor buys the same watts worse";
    default:
      return reason.replace(/_/g, " ");
  }
}

/**
 * The card's status sentence: the phase in plain words, with `advise` naming
 * its own truth (a displayed plan that does not act says so beside itself).
 */
export function eveningStatusText(state: EveningShareState): string {
  const advise =
    state.submits === "never" ? " (advise — computes and displays, submits nothing)" : "";
  switch (state.phase) {
    case "sharing":
      return `Sharing the evening load across the fleet${advise}.`;
    case "capability_limited":
      return `The fleet is at its caps and the grid covers the rest${advise}.`;
    case "withdrawn":
      return `Withdrawn — every pod's own autonomy serves on${advise}.`;
    default:
      return `Idle until the ${state.window.opensLocal} window${advise}.`;
  }
}

/** The live line while engaged: work, the netted exchange, the split. */
export function eveningLiveLine(state: EveningShareState): string {
  if (state.workW === null) {
    return "";
  }
  const exchange =
    state.netExchangeW === null
      ? ""
      : state.netExchangeW >= 0
        ? `import ${Math.round(state.netExchangeW)} W`
        : `export ${Math.round(-state.netExchangeW)} W`;
  const elsewhere =
    state.elsewhereW !== null && state.elsewhereW > 0
      ? ` · elsewhere ${Math.round(state.elsewhereW)} W`
      : "";
  const parts = [`work ${Math.round(state.workW)} W`];
  if (exchange !== "") {
    parts.push(exchange);
  }
  return parts.join(" · ") + elsewhere;
}

/** The morning-after close line (§8.1's honest one-sentence close). */
export function eveningCloseText(close: EveningCloseState): string {
  const served = Object.values(close.servedWh).reduce((sum, value) => sum + value, 0);
  const pods = Object.values(close.servedWh).filter((value) => value > 0).length;
  const money =
    close.money !== null && typeof close.money.import_paid_cents === "number"
      ? ` at ${close.money.import_paid_cents.toFixed(0)} c`
      : "";
  const convergence =
    close.convergenceOpen !== null && close.convergenceClose !== null
      ? ` · convergence Δ ${close.convergenceOpen.toFixed(1)}→${close.convergenceClose.toFixed(1)} pct`
      : "";
  return `served ${(served / 1000).toFixed(2)} kWh across ${pods} pods · import ${(
    close.importWh / 1000
  ).toFixed(2)} kWh${money}${convergence}`;
}

/**
 * The card's alert story: the first alert-tier fact, for the card's own
 * alert styling (§9's tiers). Null while nothing is at alert tier.
 */
export function eveningAlertText(state: EveningShareState): string | null {
  if (state.degradedNote !== null) {
    return state.degradedNote;
  }
  const evidence = state.reasonCodes.find((code) => code.startsWith("grid_evidence_"));
  if (evidence !== undefined) {
    return `the control basis is unjudgeable (${evidence.replace(/_/g, " ")}) — the program is withdrawn and every pod's own autonomy serves on`;
  }
  return null;
}
