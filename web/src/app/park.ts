/**
 * The pod-parking surface's shared wire model (DESIGN_POD_PARKING.md §2/§3/§7
 * + API_CONTRACTS.md "Pod parking"): the per-unit `park_state` projection,
 * the resume 200's `checklist` object, the three guarded routes' typed
 * refusals, and the plain-word maps every parking surface (the Batteries chip
 * and banner, the park/resume dialogs, Home's fleet banner) shares.
 *
 * PENDING-BACKEND: the projection, the routes, and the `unit.parked` /
 * `unit.park_renewed` / `unit.resumed` / `unit.park_expired` bus events are
 * not live yet — every parser here is built feature-detectively against the
 * contract's pinned shapes (the adviser_state / schedule_state pattern): an
 * ABSENT `park_state` key is the not-commissioned feature detection and never
 * an error; a PRESENT-but-partial frame falls back per field to its honest
 * default (null / 0 / ""), never to a fabricated figure. No parking surface
 * renders while the snapshot carries no `park_state`.
 *
 * Wire truth pinned here (the docs govern):
 * - `park_state`: `{parked, origin, parked_at, lease_expires_at, max_total_s,
 *   remaining_cap_s, expired, reason, authorizer, foreign_rewrite?,
 *   write_unverified?, foreign_mode?}` — absent key when uncommissioned.
 * - The lease countdown is POLICY, never safety: no countdown may imply
 *   time-bounded safety, and expiry is the alarm-only act (the controller
 *   performs no write; Resume is an operator act).
 * - Refusals are 409 envelopes with pinned details shapes; every code below
 *   has its own renderer, and an unknown code renders the envelope verbatim
 *   (the view always renders code + message beside the plain sentence).
 * - Takeover is required exactly when the word is parked with no controller
 *   lease (`origin: "foreign" | "unrecorded"`): the 409
 *   `park_foreign_word_acknowledgement_required` IS the routing — the resume
 *   dialog grows its acknowledgement step from either side.
 */
import { isRecord } from "./fleet";

/** The fixed not-isolation sentence, verbatim everywhere (§0/§8). */
export const NOT_ISOLATION_SENTENCE =
  "Parking is not electrical isolation — the battery stays connected at full voltage. Never perform physical work on a parked pod.";

/** The expiry banner's pinned wording (§8): an operator act, not a timer's. */
export const LEASE_EXPIRED_SENTENCE = "lease expired — Resume required";

/** Who the lease names (§3's four-word vocabulary). */
export type ParkOrigin = "operator" | "foreign" | "unrecorded" | "none";

const ORIGINS: readonly ParkOrigin[] = ["operator", "foreign", "unrecorded", "none"];

/** A vendor mode word ∈ {2..6} the controller refuses to normalize (§3). */
export interface ForeignModeView {
  word: number;
  name: string;
  firstObservedAt: string;
}

/** The per-unit `park_state` projection, narrowed per field. */
export interface ParkStateView {
  parked: boolean;
  origin: ParkOrigin;
  parkedAt: string;
  leaseExpiresAt: string;
  maxTotalS: number;
  remainingCapS: number;
  expired: boolean;
  reason: string;
  authorizer: string;
  /** True when a foreign park landed over our unchanged lease — named, no write. */
  foreignRewrite: boolean | null;
  /** The terminal sub-state: our failed write stays ours (§3). */
  writeUnverified: boolean | null;
  foreignMode: ForeignModeView | null;
}

function optionalBoolean(value: unknown, base: boolean | null): boolean | null {
  if (typeof value === "boolean") {
    return value;
  }
  return base;
}

function toForeignMode(value: unknown): ForeignModeView | null {
  if (!isRecord(value)) {
    return null;
  }
  const word = typeof value.word === "number" && Number.isFinite(value.word) ? value.word : null;
  if (word === null) {
    return null;
  }
  return {
    word,
    name: typeof value.name === "string" ? value.name : "",
    firstObservedAt: typeof value.first_observed_at === "string" ? value.first_observed_at : "",
  };
}

/**
 * Narrow one `park_state` (a snapshot unit's or a unit detail's). Null when
 * the value is not an object or carries no boolean `parked` — the ABSENT /
 * unusable key is the not-commissioned feature detection: no parked surface
 * renders and the recovery advisory honestly renders unavailable. A garbage
 * frame is never half-adopted.
 */
export function toParkState(value: unknown): ParkStateView | null {
  if (!isRecord(value) || typeof value.parked !== "boolean") {
    return null;
  }
  const origin =
    typeof value.origin === "string" && (ORIGINS as readonly string[]).includes(value.origin)
      ? (value.origin as ParkOrigin)
      : "none";
  return {
    parked: value.parked,
    origin,
    parkedAt: typeof value.parked_at === "string" ? value.parked_at : "",
    leaseExpiresAt: typeof value.lease_expires_at === "string" ? value.lease_expires_at : "",
    maxTotalS:
      typeof value.max_total_s === "number" && Number.isFinite(value.max_total_s)
        ? Math.max(0, value.max_total_s)
        : 0,
    remainingCapS:
      typeof value.remaining_cap_s === "number" && Number.isFinite(value.remaining_cap_s)
        ? Math.max(0, value.remaining_cap_s)
        : 0,
    expired: value.expired === true,
    reason: typeof value.reason === "string" ? value.reason : "",
    authorizer: typeof value.authorizer === "string" ? value.authorizer : "",
    foreignRewrite: optionalBoolean(value.foreign_rewrite, null),
    writeUnverified: optionalBoolean(value.write_unverified, null),
    foreignMode: toForeignMode(value.foreign_mode),
  };
}

// --- the resume checklist ------------------------------------------------------

/** The resume 200's `checklist` object (§2), narrowed per field. */
export interface ResumeChecklistView {
  commsAgeS: number | null;
  socDriftPct: number | null;
  socPctAtPark: number | null;
  measuredWattsNow: number | null;
  faultsWhileParked: string[] | null;
  faultsRetentionNote: string;
  latchedStops: string[];
  latchedInhibit: boolean;
}

function optionalFinite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const items = value.filter((entry): entry is string => typeof entry === "string" && entry !== "");
  return items;
}

/**
 * Narrow the resume 200's `checklist`. Null when the value is not an object:
 * a resume whose checklist is unusable renders the resume's own honest line,
 * never a fabricated checklist.
 */
export function toResumeChecklist(value: unknown): ResumeChecklistView | null {
  if (!isRecord(value)) {
    return null;
  }
  const faults = value.faults_while_parked;
  return {
    commsAgeS: optionalFinite(value.comms_age_s),
    socDriftPct: optionalFinite(value.soc_drift_pct),
    socPctAtPark: optionalFinite(value.soc_pct_at_park),
    measuredWattsNow: optionalFinite(value.measured_watts_now),
    faultsWhileParked: Array.isArray(faults) ? stringList(faults) : null,
    faultsRetentionNote: typeof value.faults_retention_note === "string" ? value.faults_retention_note : "",
    latchedStops: Array.isArray(value.latched_stops) ? (stringList(value.latched_stops) ?? []) : [],
    latchedInhibit: value.latched_inhibit === true,
  };
}

// --- the countdown (policy, never safety) --------------------------------------

/**
 * Seconds until the lease's own expiry instant from a caller-ticked now; null
 * when the projection carries no readable instant. Clamped at zero: an
 * expired lease never counts negative.
 */
export function leaseSecondsRemaining(park: ParkStateView, nowMs: number): number | null {
  if (park.leaseExpiresAt === "") {
    return null;
  }
  const at = Date.parse(park.leaseExpiresAt);
  if (!Number.isFinite(at)) {
    return null;
  }
  return Math.max(0, Math.floor((at - nowMs) / 1000));
}

/** The banner's H:MM figure ("3:47", "0:12", "26:03") — hours:whole minutes. */
export function hmmText(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  return `${hours}:${String(minutes).padStart(2, "0")}`;
}

/**
 * Whether the lease reads expired NOW: the wire's own `expired` flag (the
 * controller's alarm-only act) OR the projection's own expiry instant having
 * passed between snapshots — the promotion never waits for the next read.
 */
export function leaseIsExpired(park: ParkStateView, nowMs: number): boolean {
  if (park.expired) {
    return true;
  }
  const remaining = leaseSecondsRemaining(park, nowMs);
  return remaining !== null && remaining <= 0;
}

/**
 * The unit banner's pinned line (§8): "Parked — not isolation · lease expires
 * in H:MM", promoting to the alert wording at expiry. The countdown is
 * policy, never safety — the fixed sentence rides beside it (see
 * `parkedBannerFixedLine`), never inside a time-bounded safety claim.
 */
export function parkedBannerLine(park: ParkStateView, nowMs: number): string {
  if (leaseIsExpired(park, nowMs)) {
    return `Parked — not isolation · ${LEASE_EXPIRED_SENTENCE}`;
  }
  const remaining = leaseSecondsRemaining(park, nowMs);
  const countdown = remaining === null ? "" : ` · lease expires in ${hmmText(remaining)}`;
  return `Parked — not isolation${countdown}`;
}

/**
 * The banner's second line, always: the not-isolation honesty sentence
 * verbatim (§0 pins it for the unit banner), plus the lease's own policy
 * words — a countdown that implied time-bounded safety would be a lie.
 */
export function parkedBannerFixedLine(): string {
  return `${NOT_ISOLATION_SENTENCE} The lease countdown is policy, never safety.`;
}

/** The chip tooltip: the mode word and the pack voltage, never a metaphor. */
export function parkedChipTooltip(
  modeWord: number | null,
  packVoltageV: number | null,
  park: ParkStateView | null,
): string {
  const name =
    park?.foreignMode !== null && park?.foreignMode !== undefined
      ? park.foreignMode.name
      : modeWord === 1
        ? "Standby"
        : modeWord === 0
          ? "Normal"
          : "";
  const word = park?.foreignMode !== null && park?.foreignMode !== undefined ? park.foreignMode.word : modeWord;
  const wordText =
    word === null
      ? "mode word not available"
      : `mode word ${word}${name === "" ? "" : ` (${name})`}`;
  const volts = packVoltageV === null ? "pack voltage not available" : `pack ${packVoltageV} V`;
  return `${wordText} · ${volts} — the battery stays connected at full voltage`;
}

// --- the park dialog's lease choices -------------------------------------------

/** One bounded lease duration the dialog's select offers. */
export interface LeaseChoice {
  seconds: number;
  /** The option's label ("4 h", "90 min", "4 h (site maximum)"). */
  label: string;
}

/** The offered ladder (whole steps the operator reads at a glance). */
const LEASE_LADDER_S: readonly number[] = [
  60, 300, 900, 1800, 3600, 7200, 14400, 21600, 28800, 43200, 86400,
];

/** A duration in operator words: whole hours, else whole minutes. */
export function leaseDurationText(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole >= 3600 && whole % 3600 === 0) {
    return `${whole / 3600} h`;
  }
  return `${Math.max(1, Math.round(whole / 60))} min`;
}

/**
 * The lease-duration choices for a NEW park, bounded by the site's own
 * budget: `lease_s ∈ [60, max_lease_s]` (§2) and never more than the site
 * still has (`remaining_cap_s` — the anti-rollover figure). The site's cap is
 * always offered and named as the cap; the ladder's steps fill in below it.
 * The ladder minimum is 60 s because the wire refuses anything shorter.
 */
export function leaseChoices(park: ParkStateView | null): LeaseChoice[] {
  const cap =
    park === null
      ? 0
      : park.remainingCapS > 0
        ? Math.min(park.maxTotalS, park.remainingCapS)
        : park.maxTotalS;
  if (cap < 60) {
    return [];
  }
  const choices: LeaseChoice[] = [];
  for (const step of LEASE_LADDER_S) {
    if (step <= cap) {
      choices.push({ seconds: step, label: leaseDurationText(step) });
    }
  }
  const exact = choices.some((choice) => choice.seconds === cap);
  if (!exact) {
    choices.push({ seconds: cap, label: `${leaseDurationText(cap)} (site maximum)` });
  }
  choices.sort((a, b) => a.seconds - b.seconds);
  return choices;
}

// --- the takeover rule ---------------------------------------------------------

/**
 * Whether resuming needs the FOREIGN takeover acknowledgement: exactly the
 * word-parked-no-controller-lease states (§1/§2) — `origin: "foreign"` (the
 * vendor-app/foreign-flip class) or `origin: "unrecorded"` (crash-after-write
 * residue). Our own lease — live or expired — resumes with the operator's
 * fresh RESUME alone.
 */
export function takeoverRequired(park: ParkStateView | null): boolean {
  return (
    park !== null &&
    park.parked &&
    (park.origin === "foreign" || park.origin === "unrecorded")
  );
}

// --- the refusal renderers (every pinned 409 shape) ---------------------------

/** The refusal surface the renderers read: code + message + details, verbatim. */
export interface ParkRefusal {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
}

function detailRecord(refusal: ParkRefusal): Record<string, unknown> {
  return refusal.details === null ? {} : refusal.details;
}

function detailText(details: Record<string, unknown>, key: string): string | null {
  const value = details[key];
  return typeof value === "string" && value !== "" ? value : null;
}

function detailNumber(details: Record<string, unknown>, key: string): number | null {
  const value = details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A local-time reading of an ISO instant ("" when unreadable). */
function localTime(instant: string | null): string {
  if (instant === null) {
    return "";
  }
  const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(instant);
  return match === null ? "" : (match[1] ?? "");
}

/** One conflict row of `park_conflict_refused`'s `units` list. */
function conflictRows(details: Record<string, unknown>): string[] {
  const units = Array.isArray(details.units) ? details.units : [];
  const rows: string[] = [];
  for (const entry of units) {
    if (!isRecord(entry) || typeof entry.unit_id !== "string") {
      continue;
    }
    const cause = typeof entry.cause === "string" && entry.cause !== "" ? entry.cause : "not parkable";
    rows.push(`${entry.unit_id} — ${cause.replace(/_+/g, " ")}`);
  }
  return rows;
}

/**
 * One pinned refusal in plain words, keyed on the envelope's own code. The
 * view renders this BESIDE the envelope verbatim (code + message) — the plain
 * sentence explains, the envelope is the record. "" for an unknown code: the
 * envelope alone speaks, never a guessed sentence.
 */
export function parkRefusalText(refusal: ParkRefusal): string {
  const details = detailRecord(refusal);
  switch (refusal.code) {
    case "park_not_commissioned": {
      const cause = detailText(details, "cause");
      if (cause === "mode_not_write_enabled") {
        return "Parking is not commissioned — this site is not in write-enabled mode.";
      }
      return "Parking is not commissioned — the parking config block is absent on this site.";
    }
    case "park_conflict_refused": {
      const rows = conflictRows(details);
      return rows.length === 0
        ? "Parking refused — the battery is not in a state where parking is permitted (disarm it first)."
        : `Parking refused — ${rows.join("; ")}. Disarm or acknowledge first; park is its own deliberate act.`;
    }
    case "park_already_parked":
      return "Already parked under an open lease — resume it, or renew the lease, instead of parking again.";
    case "park_mode_out_of_scope": {
      const vendor = detailText(details, "vendor_name") ?? "a vendor-directed mode";
      const word = detailNumber(details, "prior_word");
      const wordText = word === null ? "" : ` (mode word ${word})`;
      return `The device is in the vendor's ${vendor} mode${wordText} — the controller never transitions a mode it did not set.`;
    }
    case "park_write_failed": {
      const errorClass = detailText(details, "error_class") ?? "transport refused";
      return `The park write did not go through (${errorClass}) — nothing was changed. It is safe to try again.`;
    }
    case "park_readback_unverified": {
      const written = detailNumber(details, "written_value");
      const readback = detailNumber(details, "readback_word");
      const retries = detailNumber(details, "retries");
      const words = `wrote ${written ?? "?"}, read back ${readback ?? "?"}${
        retries === null ? "" : ` after ${retries} retr${retries === 1 ? "y" : "ies"}`
      }`;
      return `The park write could not be verified — ${words}. No lease was minted; check the pod before trying again.`;
    }
    case "park_foreign_word_acknowledgement_required": {
      const since = localTime(detailText(details, "observed_since"));
      const sinceText = since === "" ? "" : `, observed since ${since}`;
      return `This battery is parked with no lease from this controller${sinceText}. Taking it over is deliberate — confirm the foreign-park acknowledgement, then resume.`;
    }
    case "park_lease_cap_reached": {
      const parkedAt = localTime(detailText(details, "parked_at"));
      const maxTotal = detailNumber(details, "max_total_s");
      const capText = maxTotal === null ? "" : ` (maximum ${leaseDurationText(maxTotal)})`;
      return `The lease cannot be extended further${capText}${
        parkedAt === "" ? "" : ` — parked at ${parkedAt}, and renewal never runs past that plus the maximum`
      }.`;
    }
    case "park_lease_absent": {
      // The contract pins that the details "carry the closing row's origin
      // and time" but not the key spellings; read the natural pair
      // defensively and say only what the wire actually carried.
      const origin =
        detailText(details, "origin") ??
        detailText(details, "closing_origin") ??
        detailText(details, "result");
      const closedAt =
        detailText(details, "closed_at") ??
        detailText(details, "ended_at") ??
        detailText(details, "closed_row_at");
      const time = localTime(closedAt);
      const byText = origin === null ? "" : ` (closed by ${origin.replace(/_+/g, " ")})`;
      const atText = time === "" ? "" : ` at ${time}`;
      return `No open lease to act on${byText}${atText} — start a fresh park if one is needed.`;
    }
    case "resume_stop_latched": {
      const stopIds = stringList(details.stop_ids) ?? [];
      const names = stopIds.length === 0 ? "a latched emergency stop" : stopIds.join(", ");
      return `Resume refused — ${names} still holds this battery. Acknowledge the stop first; resume would re-enable autonomy under a standing stop.`;
    }
    default:
      return "";
  }
}

/**
 * The resume dialog's routing answer: true exactly when the refusal is the
 * takeover acknowledgement requirement, so the dialog grows its
 * acknowledgement step from the 409 itself (the arm-takeover pattern — the
 * refusal IS the routing).
 */
export function isTakeoverRefusal(refusal: ParkRefusal): boolean {
  return refusal.code === "park_foreign_word_acknowledgement_required";
}

// --- plain-language maps (the codes are the wire, the sentences the console) ---

/** The lease's own origin in plain words. */
export function parkOriginText(origin: ParkOrigin): string {
  switch (origin) {
    case "operator":
      return "parked from this console";
    case "foreign":
      return "parked by another writer";
    case "unrecorded":
      return "parked, with no recorded lease";
    default:
      return "not parked";
  }
}

/** The write_unverified honesty line (§3: our failed write stays ours). */
export function writeUnverifiedText(): string {
  return "The last park write could not be verified — the pod may or may not be parked. Resume is the operator act that settles it.";
}

/** The foreign_rewrite honesty line (§3: named, no write, never fought). */
export function foreignRewriteText(): string {
  return "Another writer parked over this lease — the controller names it and does not act. Resume takes the pod back.";
}
