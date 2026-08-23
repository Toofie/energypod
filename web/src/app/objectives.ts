/**
 * The night-writer detector's shared wire model (API_CONTRACTS.md
 * "Night-writer detector (foreign-objective observation)", PRODUCT_NEXT.md S2):
 * what commanded the batteries when we did not.
 *
 * PENDING-BACKEND: the detector is not composed yet — every parser here is
 * built feature-detectively against the contract's pinned shapes (the
 * active_stops / intent-block / adviser_state pattern): an ABSENT or null
 * field is the honest "no recorded sample" answer and never an error; a
 * PRESENT-but-unusable datum falls back to null, never to a fabricated
 * figure. Once composed the detector is ALWAYS on (no config block exists),
 * so `last_objective_observed` is never absent — only null — but the parsing
 * stays defensive either way.
 *
 * The three wire surfaces this module owns (contract-pinned):
 *
 * - The per-unit `last_objective_observed` summary on every snapshot unit
 *   (and every health() units entry): `{observed_at, active_w, reactive_var,
 *   classification, reason}` — null before any recorded sample.
 * - The ALERT-TIER bus event `foreign_objective.observed` — exactly one per
 *   (episode, reason), payload `{unit_id, observed_at, active_w,
 *   reactive_var, classification, reason, lifecycle, claimed, run_mode_w,
 *   ctrl_mode_w, work_mode_w, debug_mode_w, grid_power_w, pv_evidence}`.
 *   QUIET-TIER EVIDENCE IS NEVER PUBLISHED (the contract's own pin): in-band
 *   pod-autonomy samples and handback-grace samples accumulate server-side in
 *   the session record behind the read surface — the console's only quiet-tier
 *   window is that view.
 * - The session view `GET /api/v1/objectives/observed?last=24h`: the window
 *   characterization per unit (first/last seen, sample counts by sign,
 *   min/typical/max active watts, foreign episodes and their standing state).
 *
 * Classification vocabulary (the contract's own words): the quiet in-band
 * evidence `pod_autonomy_objective_observed`, the site's own scheduled
 * 00:00-06:00 writer `expected_nightly_charge` (KNOWN and EXPECTED — a quiet
 * characterization that OUTRANKS the pattern escalations), our own lapsed
 * command's residue `handback_grace`, and the alert tier
 * `foreign_objective_observed`. Reasons ride ONE `reason` word (never a
 * list): `reactive_objective_observed`, `outside_autonomy_band`,
 * `sustained_remote_mode_objective`, `sustained_charge_without_pv_evidence`.
 *
 * Wire sign convention (live-proven): the served objective's active word is
 * NEGATIVE = CHARGE, positive = DISCHARGE — the same convention as measured
 * battery watts.
 */
import { formatWatts } from "../lib/format";
import { localTimeOfInstant } from "./schedule";

// ---------------------------------------------------------------------------
// Wire vocabularies
// ---------------------------------------------------------------------------

/**
 * The alert-tier bus event (note the DOT — the bus type; the AUDIT fact of
 * the same episode is the underscore word `foreign_objective_observed`).
 * Exactly one publication per (episode, reason): opening a foreign episode or
 * a reason change inside one; a sustained foreign objective never re-alerts
 * per sample. PENDING-BACKEND.
 */
export const FOREIGN_OBJECTIVE_OBSERVED_EVENT = "foreign_objective.observed" as const;

/**
 * The audit fact's `event_type` (the durable record the same episode writes):
 * `foreign_objective_observed`. PENDING-BACKEND.
 */
export const FOREIGN_OBJECTIVE_AUDIT_TYPE = "foreign_objective_observed" as const;

/**
 * The classification vocabulary, as a runtime checklist (the contract's rule
 * list, first match wins — `expected_nightly_charge` OUTRANKS the pattern
 * escalations: the site's own scheduled 00:00-06:00 writer is KNOWN and
 * EXPECTED, a quiet characterization). A summary carrying any other word
 * still parses — its word is rendered honestly and it is NEVER treated as
 * foreign evidence (the backend alone asserts "someone else is commanding
 * this battery").
 */
export const OBJECTIVE_CLASSIFICATIONS: readonly string[] = [
  "pod_autonomy_objective_observed",
  "expected_nightly_charge",
  "handback_grace",
  "foreign_objective_observed",
];

/**
 * The one-word pattern reasons the detector pins (the alert rules): a
 * reactive component, an out-of-band magnitude, or one of the two sustained
 * in-band escalations. An unknown reason is rendered honestly from its own
 * words, never dropped, never guessed at.
 */
export const OBJECTIVE_REASONS: readonly string[] = [
  "reactive_objective_observed",
  "outside_autonomy_band",
  "sustained_remote_mode_objective",
  "sustained_charge_without_pv_evidence",
];

// ---------------------------------------------------------------------------
// Parsing (null-safe; nothing is ever derived beyond what the wire carries)
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalFinite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalInstant(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function optionalWord(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function optionalBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/**
 * The per-unit `last_objective_observed` summary: the detector's most recent
 * RECORDED sample — when it was taken, the objective's own words, the
 * classification, and the one-word reason. Null when the field is absent,
 * null, or carries no classification: that is the honest "no recorded sample"
 * answer (absence is not evidence of a writer), and the quiet line never
 * renders on a null.
 */
export interface UnitObjective {
  /** When this sample was taken (site-local ISO string). */
  readonly observedAt: string | null;
  /** The objective's active word, signed: negative = charge. */
  readonly activeW: number | null;
  /** The objective's reactive word; the pod's own signature carries Q = 0. */
  readonly reactiveVar: number | null;
  /** The detector's classification word (the vocabulary above). */
  readonly classification: string;
  /** The one-word pattern reason; null on quiet classifications. */
  readonly reason: string | null;
}

export function toUnitObjective(raw: unknown): UnitObjective | null {
  if (!isRecord(raw)) {
    return null;
  }
  const classification = raw.classification;
  if (typeof classification !== "string" || classification === "") {
    return null;
  }
  return {
    observedAt: optionalInstant(raw.observed_at),
    activeW: optionalFinite(raw.active_w),
    reactiveVar: optionalFinite(raw.reactive_var),
    classification,
    reason: optionalWord(raw.reason),
  };
}

/**
 * One `foreign_objective.observed` payload — the alert tier's whole evidence
 * frame: the objective's words, the classification and reason, our lifecycle
 * and claim state at the time, the four mode words, the grid word, and the
 * per-sample PV-evidence verdict. Null when the payload carries no unit id;
 * every figure stays nullable, never zero-filled.
 */
export interface ForeignObjectiveEvent {
  readonly unitId: string;
  readonly observedAt: string | null;
  readonly activeW: number | null;
  readonly reactiveVar: number | null;
  readonly classification: string | null;
  readonly reason: string | null;
  /** Our own lifecycle word at the sample (the detector only samples uncommanded states). */
  readonly lifecycle: string | null;
  /** Whether any live intent claimed the unit at the sample (it never does — pinned). */
  readonly claimed: boolean | null;
  readonly runModeW: number | null;
  readonly ctrlModeW: number | null;
  readonly workModeW: number | null;
  readonly debugModeW: number | null;
  /** The same observation's advisory grid word (negative = import). */
  readonly gridPowerW: number | null;
  /** The sample's PV-evidence verdict: true only when grid_power_w > 0 (export). */
  readonly pvEvidence: boolean | null;
}

export function toForeignObjectiveEvent(payload: unknown): ForeignObjectiveEvent | null {
  if (!isRecord(payload) || typeof payload.unit_id !== "string" || payload.unit_id === "") {
    return null;
  }
  return {
    unitId: payload.unit_id,
    observedAt: optionalInstant(payload.observed_at),
    activeW: optionalFinite(payload.active_w),
    reactiveVar: optionalFinite(payload.reactive_var),
    classification: optionalWord(payload.classification),
    reason: optionalWord(payload.reason),
    lifecycle: optionalWord(payload.lifecycle),
    claimed: optionalBoolean(payload.claimed),
    runModeW: optionalFinite(payload.run_mode_w),
    ctrlModeW: optionalFinite(payload.ctrl_mode_w),
    workModeW: optionalFinite(payload.work_mode_w),
    debugModeW: optionalFinite(payload.debug_mode_w),
    gridPowerW: optionalFinite(payload.grid_power_w),
    pvEvidence: optionalBoolean(payload.pv_evidence),
  };
}

/**
 * The summary a foreign-objective event implies for its unit — the state-local
 * patch that moves the quiet line the moment the alert lands (the snapshot's
 * own `last_objective_observed` reconciles on its next read). The event TYPE
 * is the detector's foreign assertion, so the classification is
 * `foreign_objective_observed`, never read off the payload's optional copy.
 */
export function objectiveFromForeignEvent(event: ForeignObjectiveEvent): UnitObjective {
  return {
    observedAt: event.observedAt,
    activeW: event.activeW,
    reactiveVar: event.reactiveVar,
    classification: FOREIGN_OBJECTIVE_AUDIT_TYPE,
    reason: event.reason,
  };
}

/**
 * The one classification that renders the quiet per-unit line: the backend's
 * own assertion that a writer other than this controller and other than the
 * pod's own autonomy holds an objective on this battery. Any other word —
 * including an unknown future vocabulary word — is not that assertion and
 * stays off the cards (the Objectives view still shows the raw word). The
 * predicate narrows to a non-null objective, so a guarded render site can use
 * the summary directly.
 */
export function isForeignObjective(
  objective: UnitObjective | null,
): objective is UnitObjective {
  return objective !== null && objective.classification === FOREIGN_OBJECTIVE_AUDIT_TYPE;
}

// ---------------------------------------------------------------------------
// The session view (GET /api/v1/objectives/observed)
// ---------------------------------------------------------------------------

/**
 * One unit's window characterization as the read surface rolls it up. The
 * contract's shape verbatim: the counts split by the active word's sign, the
 * per-classification counts (the nightly window's characterization — the
 * `expected_nightly_charge` count is exactly what the night-partition
 * decision reads), the min/typical/max of the recorded samples (the typical
 * figure is the backend's own LOWER median — never re-derived here), the
 * foreign episode count and standing state, and the endpoint's own
 * `last_objective_observed` (the full evidence record; the five compact
 * fields are what the console reads).
 */
export interface ObservedObjectivesUnit {
  readonly unitId: string;
  readonly firstSeenAt: string | null;
  readonly lastSeenAt: string | null;
  /** NONZERO recorded samples only — zeros are not samples (the contract's pin). */
  readonly sampleCount: number;
  readonly chargeSampleCount: number;
  readonly dischargeSampleCount: number;
  readonly minActiveW: number | null;
  readonly typicalActiveW: number | null;
  readonly maxActiveW: number | null;
  /** In-window counts per classification word (the four pinned words, verbatim keys). */
  readonly classificationCounts: Readonly<Record<string, number>>;
  readonly foreignEpisodeCount: number;
  readonly foreignActive: boolean | null;
  readonly foreignReason: string | null;
  readonly lastObjective: UnitObjective | null;
}

/** The read surface's whole answer: the window's facts and per-unit rollups. */
export interface ObservedObjectivesView {
  readonly asOf: string | null;
  /** The echoed window spelling the caller asked for ("24h"); null when absent. */
  readonly last: string | null;
  readonly windowS: number | null;
  readonly units: ObservedObjectivesUnit[];
}

function toClassificationCounts(value: unknown): Readonly<Record<string, number>> {
  if (!isRecord(value)) {
    return {};
  }
  const counts: Record<string, number> = {};
  for (const [classification, count] of Object.entries(value)) {
    if (typeof count === "number" && Number.isFinite(count)) {
      counts[classification] = count;
    }
  }
  return counts;
}

function toObservedObjectivesUnit(raw: unknown): ObservedObjectivesUnit | null {
  if (!isRecord(raw) || typeof raw.unit_id !== "string" || raw.unit_id === "") {
    return null;
  }
  return {
    unitId: raw.unit_id,
    firstSeenAt: optionalInstant(raw.first_seen_at),
    lastSeenAt: optionalInstant(raw.last_seen_at),
    sampleCount: typeof raw.sample_count === "number" ? raw.sample_count : 0,
    chargeSampleCount:
      typeof raw.charge_sample_count === "number" ? raw.charge_sample_count : 0,
    dischargeSampleCount:
      typeof raw.discharge_sample_count === "number" ? raw.discharge_sample_count : 0,
    minActiveW: optionalFinite(raw.min_active_w),
    typicalActiveW: optionalFinite(raw.typical_active_w),
    maxActiveW: optionalFinite(raw.max_active_w),
    classificationCounts: toClassificationCounts(raw.classification_counts),
    foreignEpisodeCount:
      typeof raw.foreign_episode_count === "number" ? raw.foreign_episode_count : 0,
    foreignActive: optionalBoolean(raw.foreign_active),
    foreignReason: optionalWord(raw.foreign_reason),
    lastObjective: toUnitObjective(raw.last_objective_observed),
  };
}

/**
 * Narrow the read surface's 200 body. Null when it is not an object or carries
 * no units array — a garbage body is never half-adopted.
 */
export function toObservedObjectivesView(body: unknown): ObservedObjectivesView | null {
  if (!isRecord(body) || !Array.isArray(body.units)) {
    return null;
  }
  return {
    asOf: optionalInstant(body.as_of),
    last: optionalWord(body.last),
    windowS: optionalFinite(body.window_s),
    units: body.units
      .map(toObservedObjectivesUnit)
      .filter((entry): entry is ObservedObjectivesUnit => entry !== null),
  };
}

/**
 * The window spellings the view offers, all inside the route's legal
 * 1 h..168 h range (`Nh`/`Nd`): one glance (24 h), the partition decision's
 * overnight horizon (3 d), and the retention edge (7 d). The default is the
 * route's own default, 24 h.
 */
export const OBJECTIVE_WINDOW_OPTIONS: readonly string[] = ["24h", "3d", "7d"];

// ---------------------------------------------------------------------------
// Plain-language maps (words first, raw codes only on demand)
// ---------------------------------------------------------------------------

/** A wire code as calm lowercase words: outside_autonomy_band -> "outside autonomy band". */
function humanizeCode(code: string): string {
  const words = code.toLowerCase().replace(/_+/g, " ").trim();
  return words === "" ? code : words;
}

/**
 * One pattern reason in the operator's words. Each pinned rule carries its
 * own plain clause; an unknown reason is rendered honestly from its own words
 * — never dropped, never guessed at.
 */
export function objectiveReasonText(reason: string): string {
  switch (reason) {
    case "reactive_objective_observed":
      return "carries reactive power, which the pod's own self-charge never does";
    case "outside_autonomy_band":
      return "outside the pod's own operating range";
    case "sustained_remote_mode_objective":
      return "held continuously in remote-power mode — a written objective, not the pod's own load-following";
    case "sustained_charge_without_pv_evidence":
      return "an external charge pattern — sustained charging while the site imported, with no solar surplus";
    default:
      return humanizeCode(reason);
  }
}

/**
 * The classification in the operator's words — the Objectives view's label and
 * the detail panel's line. An unknown word is named honestly from its own
 * letters, never re-worded into an accusation or an assurance.
 */
export function objectiveClassificationText(classification: string): string {
  switch (classification) {
    case "pod_autonomy_objective_observed":
      return "the pod's own self-charge";
    case "expected_nightly_charge":
      return "the site's scheduled nightly charge";
    case "handback_grace":
      return "the tail of our own command";
    case "foreign_objective_observed":
      return "an external writer";
    default:
      return humanizeCode(classification);
  }
}

/**
 * The classification's SHORT words for the session table's count cells (the
 * full sentences live on the battery surfaces): "nightly charge 720 · pod
 * self-charge 12 · handback 3 · foreign 1".
 */
export const OBJECTIVE_COUNT_WORDS: Record<string, string> = {
  pod_autonomy_objective_observed: "pod self-charge",
  expected_nightly_charge: "nightly charge",
  handback_grace: "handback",
  foreign_objective_observed: "foreign",
};

/** One classification's count words, or null for a zero/unknown count. */
export function classificationCountText(classification: string, count: number): string | null {
  if (count <= 0) {
    return null;
  }
  const words = OBJECTIVE_COUNT_WORDS[classification] ?? humanizeCode(classification);
  return `${words} ${count}`;
}

/**
 * THE QUIET PER-UNIT LINE (the night-writer detector's one standing answer on
 * the cards): the pattern reason in plain words, with the observed figure and
 * the sample's own time. Rendered ONLY where the detector classified an
 * external writer (see isForeignObjective) — a line, never a badge, never a
 * banner: the operator reads the evidence, the console does not shout it. The
 * watt figure keeps the wire's own sign (negative = charge) and the time is
 * the SAMPLE's own stamp ("at 23:40" — the summary carries observed_at, not
 * the episode's first sighting; the Objectives view carries the full window).
 */
export function commandedBySomeoneElseText(objective: UnitObjective): string {
  const watts =
    objective.activeW === null ? "power not available" : formatWatts(objective.activeW);
  const at = localTimeOfInstant(objective.observedAt);
  const reason =
    objective.reason === null ? "" : ` (${objectiveReasonText(objective.reason)})`;
  return `Commanded by something else: ${watts}${at === "" ? "" : ` at ${at}`}${reason}`;
}

/**
 * THE HONESTY LINE the Objectives view pins (the contract's own stated limit,
 * spelled for the operator): an in-band charge objective — negative watts
 * inside the pod's self-charge range, no reactive word — fits the pod's own
 * signature AND a modest external night charger equally. The detector records
 * such objectives as quiet evidence and never attributes them; attribution
 * exists only where the pattern breaks the pod's own signature, and even then
 * it names "an external writer", never a specific application.
 */
export const OBJECTIVE_SIGNATURE_HONESTY_NOTE =
  "A charge objective inside the pod's own self-charge range cannot be attributed to the pod or to an external writer by its signature alone — the figure and sign fit both. In-band objectives are recorded here as evidence without attribution; only a pattern the pod's own behavior never shows (reactive power, an out-of-range magnitude, or a sustained written-objective mode) is attributed as an external writer — and never named to a specific application.";

/**
 * The DETAIL-panel line — the one place in-band autonomy is visible on a
 * battery surface (the Objectives view is the evidence table): the observed
 * words, the classification in plain words, the sample's time, and the reason
 * when the detector pinned one. Quiet by construction: a text row in the
 * summary tab, never a badge, never the card face.
 */
export function observedObjectiveDetailText(objective: UnitObjective): string {
  const watts =
    objective.activeW === null ? "power not available" : formatWatts(objective.activeW);
  const at = localTimeOfInstant(objective.observedAt);
  const words = objectiveClassificationText(objective.classification);
  const reason =
    objective.reason === null ? "" : ` — ${objectiveReasonText(objective.reason)}`;
  return `${watts} held by ${words}${at === "" ? "" : ` at ${at}`}${reason}`;
}
