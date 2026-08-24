/**
 * The PVOutput reporting surface's shared wire model — the
 * `GET /api/v1/pvoutput/status` health snapshot, the guarded
 * `POST /api/v1/pvoutput` toggle's 200, and the plain-word maps the Home
 * card renders. The backend family is the retiring Docker writer's
 * replacement: one POST per 5-minute slot, per-unit v7-v12 slots (the old
 * container's exact layout) plus the native b1/b2 fleet aggregates, quality
 * gates on every field (nulls are nulls, never zero), and a DURABLE runtime
 * toggle that survives controller restarts.
 *
 * Wire truth pinned here (the backend's status_payload governs):
 * - `{feature: "pvoutput", enabled, enabled_origin: "config"|"runtime",
 *   disabled_reason: null|"auth_failed"|"missing_credentials",
 *   credentials_note, interval_s, unit_slots{unit: [soc, power]},
 *   native_battery_fields, as_of, last_success_at, last_post_age_s,
 *   last_posted_slot, last_error, consecutive_failures, rate_remaining,
 *   slots_skipped_stale}` — every nullable field is null when unknown,
 *   never zero.
 * - A deployment without the `pvoutput` config block refuses the route with
 *   409 `pvoutput_not_commissioned` — the card's honest not-commissioned
 *   sentence renders from that refusal code.
 * - The toggle's 200 carries `{feature, enabled, enabled_origin,
 *   persisted: true, pvoutput_state}` — `persisted` is the durable-toggle
 *   fact on the wire (the night twin answers false; this one is a standing
 *   operator choice that a restart keeps).
 */
import { countdownText } from "./schedule";
import { isRecord } from "./fleet";

/** The typed confirmation the guarded toggle demands for BOTH actions. */
export const PVOUTPUT_CONFIRMATION = "PVOUTPUT" as const;

/** Whose act last set participation: the config file, or the console toggle. */
export type PvOutputEnabledOrigin = "config" | "runtime";

/** The standing reasons the reporter may be unable to post. */
export type PvOutputDisabledReason = "auth_failed" | "missing_credentials";

/** One unit's pinned [SoC slot, power slot] pair (the old layout). */
export type PvOutputUnitSlots = Record<string, [string, string]>;

/** The parsed health snapshot the Home card renders. */
export interface PvOutputStatus {
  enabled: boolean;
  enabledOrigin: PvOutputEnabledOrigin;
  disabledReason: PvOutputDisabledReason | null;
  credentialsNote: string | null;
  intervalS: number;
  unitSlots: PvOutputUnitSlots;
  nativeBatteryFields: boolean;
  /** ISO instant the snapshot was computed at (the age figures' anchor). */
  asOf: string;
  /** ISO instant of the last ACCEPTED post; null before the first. */
  lastSuccessAt: string | null;
  /** Server-computed seconds since that post; null before the first. */
  lastPostAgeS: number | null;
  /** The last posted slot, site-local "YYYY-MM-DD HH:MM"; null before the first. */
  lastPostedSlot: string | null;
  /** The last failure's own words (PVOutput's reason text rides verbatim). */
  lastError: string | null;
  consecutiveFailures: number;
  /** PVOutput's own remaining-post count; null when the answer carried none. */
  rateRemaining: number | null;
  /** Slots skipped whole because no pod was fresh enough — honest gaps. */
  slotsSkippedStale: number;
}

function origin(value: unknown, base: PvOutputEnabledOrigin): PvOutputEnabledOrigin {
  return value === "runtime" ? "runtime" : value === "config" ? "config" : base;
}

function disabledReason(value: unknown): PvOutputDisabledReason | null {
  return value === "auth_failed" || value === "missing_credentials" ? value : null;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toUnitSlots(value: unknown, base: PvOutputUnitSlots): PvOutputUnitSlots {
  if (!isRecord(value)) {
    return base;
  }
  const slots: PvOutputUnitSlots = {};
  for (const [unitId, pair] of Object.entries(value)) {
    if (
      Array.isArray(pair) &&
      pair.length === 2 &&
      typeof pair[0] === "string" &&
      typeof pair[1] === "string"
    ) {
      slots[unitId] = [pair[0], pair[1]];
    }
  }
  return Object.keys(slots).length > 0 ? slots : base;
}

/**
 * Narrow one health snapshot (a status 200, or a toggle 200's
 * `pvoutput_state`). Null when the value is not an object — a garbage frame
 * is never half-adopted. Absent fields fall back to `base` when one is
 * given (the toggle's state is a full snapshot, so the fallback only guards
 * partial frames), else to their honest nulls.
 */
export function toPvOutputStatus(
  value: unknown,
  base: PvOutputStatus | null = null,
): PvOutputStatus | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    enabled: typeof value.enabled === "boolean" ? value.enabled : (base?.enabled ?? false),
    enabledOrigin: origin(value.enabled_origin, base?.enabledOrigin ?? "config"),
    disabledReason:
      "disabled_reason" in value
        ? disabledReason(value.disabled_reason)
        : (base?.disabledReason ?? null),
    credentialsNote:
      "credentials_note" in value
        ? optionalText(value.credentials_note)
        : (base?.credentialsNote ?? null),
    intervalS: nullableNumber(value.interval_s) ?? (base?.intervalS ?? 300),
    unitSlots: toUnitSlots(value.unit_slots, base?.unitSlots ?? {}),
    nativeBatteryFields:
      typeof value.native_battery_fields === "boolean"
        ? value.native_battery_fields
        : (base?.nativeBatteryFields ?? true),
    asOf: optionalText(value.as_of) ?? (base?.asOf ?? ""),
    lastSuccessAt:
      optionalText(value.last_success_at) ??
      (value.last_success_at === null ? null : base?.lastSuccessAt ?? null),
    lastPostAgeS:
      nullableNumber(value.last_post_age_s) ??
      (value.last_post_age_s === null ? null : base?.lastPostAgeS ?? null),
    lastPostedSlot:
      optionalText(value.last_posted_slot) ??
      (value.last_posted_slot === null ? null : base?.lastPostedSlot ?? null),
    lastError:
      optionalText(value.last_error) ??
      (value.last_error === null ? null : base?.lastError ?? null),
    consecutiveFailures:
      typeof value.consecutive_failures === "number" &&
      Number.isFinite(value.consecutive_failures)
        ? Math.max(0, Math.round(value.consecutive_failures))
        : (base?.consecutiveFailures ?? 0),
    rateRemaining:
      nullableNumber(value.rate_remaining) ??
      (value.rate_remaining === null ? null : base?.rateRemaining ?? null),
    slotsSkippedStale:
      typeof value.slots_skipped_stale === "number" &&
      Number.isFinite(value.slots_skipped_stale)
        ? Math.max(0, Math.round(value.slots_skipped_stale))
        : (base?.slotsSkippedStale ?? 0),
  };
}

// --- plain-language maps (the codes are the wire, the sentences are the console) ---

/**
 * The toggle's current-state phrase. The durable-toggle honesty the night
 * tile inverts: a runtime act here is KEPT across restarts, and the phrase
 * says so ("kept across restarts") instead of the night twin's "until
 * restart".
 */
export function pvoutputToggleStateText(status: PvOutputStatus): string {
  const kept = status.enabledOrigin === "runtime" ? " — kept across restarts" : " (config)";
  return status.enabled ? `On${kept}` : `Off${kept}`;
}

/**
 * The last-post line: the server-computed age of the last ACCEPTED post
 * (never a client-guessed clock), the slot it stamped, and — while PVOutput
 * reports it — the hourly post budget remaining. A null figure says "not
 * yet", never zero.
 */
export function pvoutputLastPostText(status: PvOutputStatus): string {
  if (status.lastSuccessAt === null) {
    return "No post yet — the first 5-minute slot has not landed.";
  }
  const age = status.lastPostAgeS === null ? "" : ` ${countdownText(status.lastPostAgeS)} ago`;
  const slot = status.lastPostedSlot === null ? "" : ` · slot ${status.lastPostedSlot}`;
  const rate =
    status.rateRemaining === null ? "" : ` · ${status.rateRemaining} posts left this hour`;
  return `Last post${age}${slot}${rate}.`;
}

/**
 * The success/failure word: quiet while posting cleanly, loud while failing.
 * PVOutput's own refusal wording rides verbatim in `lastError`; this map
 * only names the STANDING causes (a credential that may never write, absent
 * credentials) in plain words beside it.
 */
export function pvoutputHealthText(status: PvOutputStatus): string {
  if (status.disabledReason === "auth_failed") {
    return `Stopped — PVOutput refused the credentials, so the uploader disabled itself: ${
      status.lastError ?? "the refusal carried no reason text"
    }.`;
  }
  if (status.disabledReason === "missing_credentials") {
    return "Stopped — the PVOutput credentials are not set on the controller, so there is nothing to post with.";
  }
  if (status.lastError !== null) {
    const streak =
      status.consecutiveFailures > 1 ? ` (${status.consecutiveFailures} in a row)` : "";
    return `Failing${streak} — ${status.lastError}. The next slot retries on its own.`;
  }
  if (status.enabled) {
    return `Posting every ${countdownText(status.intervalS)} — each battery's charge level and power on its own dashboard slot.`;
  }
  return "Standing by — nothing is posted while PVOutput reporting is off.";
}

/**
 * The stale-gap note: whole slots skipped because no pod was fresh enough
 * are honest gaps on the dashboard (never zero-filled); the count is worth
 * one quiet sentence exactly when it is non-zero.
 */
export function pvoutputGapsText(status: PvOutputStatus): string | null {
  if (status.slotsSkippedStale <= 0) {
    return null;
  }
  const plural = status.slotsSkippedStale === 1 ? "slot" : "slots";
  return `${status.slotsSkippedStale} ${plural} skipped with no fresh pod reading — honest gaps on the dashboard, never zero-filled.`;
}

/**
 * The not-commissioned sentence, rendered from the structured 409 refusal:
 * commissioning is a config change on the controller, never a console act.
 */
export const PVOUTPUT_NOT_COMMISSIONED_TEXT =
  "PVOutput reporting is not commissioned on this controller — there is nothing to turn on until the pvoutput block is added to the controller's config and it is restarted.";
