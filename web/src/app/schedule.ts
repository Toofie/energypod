/**
 * The schedules surface's shared wire model (DESIGN_SCHEDULES.md §5 +
 * API_CONTRACTS.md "Schedule"): the plan, the policy, the `schedule_state`
 * snapshot projection, the three bus events, and the pure civil-time helpers
 * every schedule surface (the editor, Home's card, the shell) shares.
 *
 * PENDING-BACKEND: the REST routes, the projection, and the events are not
 * live yet — every parser here is built feature-detectively against the
 * contract's pinned shapes (the active_stops / intent-block / adviser_state
 * pattern): an ABSENT field is the feature detection and never an error; a
 * PRESENT-but-unusable datum falls back to its honest default (null / 0 / ""),
 * never to a fabricated figure.
 *
 * Wire truth pinned here (the docs govern):
 * - Entry: `{entry_id, days: ["mon"...], start_local/end_local "HH:MM",
 *   action: charge|discharge|idle, watts | watts_by_unit, unit_ids,
 *   effective_from, effective_until, priority, enabled}` — the entry id IS the
 *   operator-written name. An entry carries EITHER scalar `watts` OR
 *   `watts_by_unit` (one positive integer per selected unit, key set exactly
 *   `unit_ids`); never both, never neither. Cross-midnight windows are ONE
 *   entry (22:30→06:00).
 * - Plan: `{version, timezone, entries}` — one version, one IANA zone.
 * - `schedule_state` (snapshot top level, absent when the config block is
 *   absent): `{version, active, entry_id, held_intent_id, ends_at, ends_in_s,
 *   next, posture, last_action, last_tick_at, reason_codes}`.
 * - Events: `schedule.replaced` `{principal, version, diff}`,
 *   `schedule_window.opened` `{entry_id, version, action, watts |
 *   watts_by_unit, unit_ids, ends_at}`, `schedule_window.closing`
 *   `{entry_id, version, unit_ids, reason}`.
 */
import { formatSeconds, formatWatts } from "../lib/format";
import { isRecord, toWattsByUnit, type WattsByUnit } from "./fleet";

/** The bus vocabulary the schedules surface consumes (transitions only). */
export const SCHEDULE_REPLACED_EVENT = "schedule.replaced" as const;
export const SCHEDULE_WINDOW_OPENED_EVENT = "schedule_window.opened" as const;
export const SCHEDULE_WINDOW_CLOSING_EVENT = "schedule_window.closing" as const;

export const SCHEDULE_EVENT_TYPES: readonly string[] = [
  SCHEDULE_REPLACED_EVENT,
  SCHEDULE_WINDOW_OPENED_EVENT,
  SCHEDULE_WINDOW_CLOSING_EVENT,
];

/** The runner's pinned projection reason vocabulary (§5). */
export const SCHEDULE_REASON_CODES: readonly string[] = [
  "no_plan",
  "no_window_open",
  "window_open",
  "waiting_for_higher_priority",
  "window_ended",
  "plan_changed",
];

/** The canonical day order; the wire's lowercase three-letter names. */
export const SCHEDULE_DAYS: readonly string[] = [
  "mon",
  "tue",
  "wed",
  "thu",
  "fri",
  "sat",
  "sun",
];

const DAY_LABELS: Record<string, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

export type ScheduleAction = "charge" | "discharge" | "idle";

const ACTIONS: readonly ScheduleAction[] = ["charge", "discharge", "idle"];

export type SchedulePosture = "yield" | "partition";

/**
 * The shipped day-only default — the named `DAY_DEFAULT` constant (§3).
 * "Night" means any civil minute OUTSIDE this window, independent of how the
 * operator narrows or widens the policy, so the acknowledgement and the
 * console copy always mean the same thing by "night".
 */
export const DAY_DEFAULT: readonly (readonly [string, string])[] = [["06:00", "20:00"]];

export const MINUTES_PER_DAY = 24 * 60;

// --- parsing ------------------------------------------------------------------

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function finite(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry !== "")
    : [];
}

function oneOfActions(value: unknown, fallback: ScheduleAction): ScheduleAction {
  return typeof value === "string" && (ACTIONS as readonly string[]).includes(value)
    ? (value as ScheduleAction)
    : fallback;
}

/**
 * One schedule entry, parsed null-safely. The dual watt form survives parsing
 * exactly as the wire carries it: `watts` scalar OR `wattsByUnit` map — one of
 * the two is null, and neither is ever derived from the other here (a scalar
 * total is NOT a per-unit figure and must never be stamped per battery).
 */
export interface ScheduleEntry {
  /** The operator-written name — the entry id itself (no separate name). */
  entryId: string;
  days: string[];
  startLocal: string;
  endLocal: string;
  action: ScheduleAction;
  /** Scalar fleet-total form; null when the entry uses the per-unit form. */
  watts: number | null;
  /** Per-battery form; null when the entry is scalar. */
  wattsByUnit: WattsByUnit | null;
  unitIds: string[];
  effectiveFrom: string | null;
  effectiveUntil: string | null;
  priority: number;
  enabled: boolean;
}

/**
 * Narrow one wire entry. Null when the value is not an object or carries no
 * usable entry id — a garbage entry is never half-adopted into the editor.
 */
export function toScheduleEntry(value: unknown): ScheduleEntry | null {
  if (!isRecord(value)) {
    return null;
  }
  const entryId = typeof value.entry_id === "string" ? value.entry_id : "";
  if (entryId === "") {
    return null;
  }
  const wattsByUnit = toWattsByUnit(value.watts_by_unit);
  return {
    entryId,
    days: stringList(value.days).filter((day) => SCHEDULE_DAYS.includes(day)),
    startLocal: text(value.start_local),
    endLocal: text(value.end_local),
    action: oneOfActions(value.action, "charge"),
    // The two watt forms are mutually exclusive on the wire; an entry that
    // carries both keeps the per-unit map (the operator's own figures) and
    // drops the scalar, never blends them.
    watts: wattsByUnit === null && typeof value.watts === "number" ? value.watts : null,
    wattsByUnit,
    unitIds: stringList(value.unit_ids),
    effectiveFrom: optionalText(value.effective_from),
    effectiveUntil: optionalText(value.effective_until),
    priority:
      typeof value.priority === "number" && Number.isFinite(value.priority) ? value.priority : 0,
    enabled: typeof value.enabled === "boolean" ? value.enabled : true,
  };
}

/** The whole published plan: one version, one IANA timezone, the entry list. */
export interface SchedulePlan {
  version: number;
  timezone: string;
  entries: ScheduleEntry[];
}

/** Narrow a wire plan; null when the value is not a usable object. */
export function toSchedulePlan(value: unknown): SchedulePlan | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.version !== "number" || !Array.isArray(value.entries)) {
    return null;
  }
  return {
    version: value.version,
    timezone: text(value.timezone),
    entries: value.entries
      .map(toScheduleEntry)
      .filter((entry): entry is ScheduleEntry => entry !== null),
  };
}

/** The derived, read-only policy GET carries beside the plan. */
export interface SchedulePolicy {
  posture: SchedulePosture;
  /** The union of these pairs is the allowed command set (local civil walls). */
  allowedWindowsLocal: [string, string][];
  intentTtlS: number;
}

export function toSchedulePolicy(value: unknown): SchedulePolicy | null {
  if (!isRecord(value)) {
    return null;
  }
  const windows: [string, string][] = [];
  if (Array.isArray(value.allowed_windows_local)) {
    for (const pair of value.allowed_windows_local) {
      if (
        Array.isArray(pair) &&
        typeof pair[0] === "string" &&
        typeof pair[1] === "string" &&
        pair[0] !== "" &&
        pair[1] !== ""
      ) {
        windows.push([pair[0], pair[1]]);
      }
    }
  }
  return {
    posture:
      typeof value.posture === "string" && value.posture === "partition" ? "partition" : "yield",
    allowedWindowsLocal: windows.length > 0 ? windows : [...DAY_DEFAULT.map((p) => [...p] as [string, string])],
    intentTtlS: finite(value.intent_ttl_s, 10),
  };
}

/**
 * The pure next-occurrence object (§5.4): the single implementation of every
 * countdown — GET's `next_action`, the projection's `next`, Home's card.
 * `starts_at` is an ISO-8601 local instant carrying the plan's zone offset;
 * `starts_in_s` the server-computed countdown, client-recomputed per frame.
 */
export interface ScheduleNextAction {
  entryId: string;
  days: string[];
  startLocal: string;
  endLocal: string;
  action: ScheduleAction;
  watts: number | null;
  wattsByUnit: WattsByUnit | null;
  unitIds: string[];
  startsAt: string | null;
  startsInS: number | null;
}

export function toScheduleNextAction(value: unknown): ScheduleNextAction | null {
  if (!isRecord(value)) {
    return null;
  }
  const entryId = typeof value.entry_id === "string" ? value.entry_id : "";
  if (entryId === "") {
    return null;
  }
  const wattsByUnit = toWattsByUnit(value.watts_by_unit);
  return {
    entryId,
    days: stringList(value.days).filter((day) => SCHEDULE_DAYS.includes(day)),
    startLocal: text(value.start_local),
    endLocal: text(value.end_local),
    action: oneOfActions(value.action, "charge"),
    watts: wattsByUnit === null && typeof value.watts === "number" ? value.watts : null,
    wattsByUnit,
    unitIds: stringList(value.unit_ids),
    startsAt: optionalText(value.starts_at),
    startsInS:
      typeof value.starts_in_s === "number" && Number.isFinite(value.starts_in_s)
        ? value.starts_in_s
        : null,
  };
}

/**
 * The snapshot's top-level `schedule_state` projection — the runner's whole
 * tick state. ABSENT on the wire when the config block is absent (the feature
 * detection); present-but-partial frames fall back per field.
 */
export interface ScheduleState {
  version: number | null;
  /** True exactly while a schedule intent is live (derived from held_intent_id). */
  active: boolean;
  /** The running window's entry name; null when nothing runs. */
  entryId: string | null;
  heldIntentId: string | null;
  /** ISO instant the running window ends; null when nothing runs. */
  endsAt: string | null;
  endsInS: number | null;
  /** The next occurrence AFTER the running window; null when nothing comes. */
  next: ScheduleNextAction | null;
  posture: SchedulePosture;
  lastAction: string;
  lastTickAt: string;
  reasonCodes: string[];
}

export function toScheduleState(value: unknown): ScheduleState | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    version: typeof value.version === "number" ? value.version : null,
    active: typeof value.active === "boolean" ? value.active : false,
    entryId: optionalText(value.entry_id),
    heldIntentId: optionalText(value.held_intent_id),
    endsAt: optionalText(value.ends_at),
    endsInS: typeof value.ends_in_s === "number" && Number.isFinite(value.ends_in_s) ? value.ends_in_s : null,
    next: toScheduleNextAction(value.next),
    posture:
      typeof value.posture === "string" && value.posture === "partition" ? "partition" : "yield",
    lastAction: text(value.last_action, "idle"),
    lastTickAt: text(value.last_tick_at),
    reasonCodes: stringList(value.reason_codes),
  };
}

// --- the bus events ------------------------------------------------------------

/** `schedule.replaced` `{principal, version, diff}` — one frame per publish. */
export interface ScheduleReplacedEvent {
  principal: string | null;
  /** The NEW plan version. */
  version: number | null;
  diff: { added: string[]; removed: string[]; changed: string[] };
}

export function toScheduleReplacedEvent(payload: unknown): ScheduleReplacedEvent | null {
  if (!isRecord(payload)) {
    return null;
  }
  const diff = isRecord(payload.diff) ? payload.diff : {};
  return {
    principal: optionalText(payload.principal),
    version: typeof payload.version === "number" ? payload.version : null,
    diff: {
      added: stringList(diff.added),
      removed: stringList(diff.removed),
      changed: stringList(diff.changed),
    },
  };
}

/**
 * `schedule_window.opened` — the runner's first submit for a window key. The
 * payload carries the window's whole command (action, watt form, units, end)
 * — the only bus source of the RUNNING window's figures.
 */
export interface ScheduleWindowOpenedEvent {
  entryId: string;
  version: number | null;
  action: ScheduleAction;
  watts: number | null;
  wattsByUnit: WattsByUnit | null;
  unitIds: string[];
  endsAt: string | null;
}

export function toScheduleWindowOpenedEvent(payload: unknown): ScheduleWindowOpenedEvent | null {
  if (!isRecord(payload)) {
    return null;
  }
  const entryId = typeof payload.entry_id === "string" ? payload.entry_id : "";
  if (entryId === "") {
    return null;
  }
  const wattsByUnit = toWattsByUnit(payload.watts_by_unit);
  return {
    entryId,
    version: typeof payload.version === "number" ? payload.version : null,
    action: oneOfActions(payload.action, "charge"),
    watts: wattsByUnit === null && typeof payload.watts === "number" ? payload.watts : null,
    wattsByUnit,
    unitIds: stringList(payload.unit_ids),
    endsAt: optionalText(payload.ends_at),
  };
}

/** `schedule_window.closing` — the removal tick: the window's command just ended. */
export interface ScheduleWindowClosingEvent {
  entryId: string;
  version: number | null;
  unitIds: string[];
  reason: string;
}

export function toScheduleWindowClosingEvent(payload: unknown): ScheduleWindowClosingEvent | null {
  if (!isRecord(payload)) {
    return null;
  }
  const entryId = typeof payload.entry_id === "string" ? payload.entry_id : "";
  if (entryId === "") {
    return null;
  }
  return {
    entryId,
    version: typeof payload.version === "number" ? payload.version : null,
    unitIds: stringList(payload.unit_ids),
    reason: text(payload.reason),
  };
}

// --- state-local projections (the adviser_state patch pattern) ------------------

/**
 * Apply a `schedule_window.opened` frame onto the current projection: the
 * window is running NOW — the card swaps without waiting for a poll. The
 * frame carries no `next`; the stale next is dropped (the next snapshot, at
 * the console's live cadence, names the following window) and `ends_in_s` is
 * derived from the payload's own `ends_at` instant.
 */
export function applyWindowOpened(
  current: ScheduleState | null,
  event: ScheduleWindowOpenedEvent,
  nowMs: number,
): ScheduleState {
  const endsInS =
    event.endsAt !== null && Number.isFinite(Date.parse(event.endsAt))
      ? Math.max(0, Math.round((Date.parse(event.endsAt) - nowMs) / 1000))
      : null;
  return {
    version: event.version ?? current?.version ?? null,
    active: true,
    entryId: event.entryId,
    heldIntentId: current?.heldIntentId ?? null,
    endsAt: event.endsAt,
    endsInS,
    next: null,
    posture: current?.posture ?? "yield",
    lastAction: "submit",
    lastTickAt: current?.lastTickAt ?? "",
    reasonCodes: ["window_open"],
  };
}

/**
 * Apply a `schedule_window.closing` frame: the window's command ended. The
 * following window (the projection's `next`) is not on the frame, so the next
 * snapshot names it; `window_ended` is the honest interim reason.
 */
export function applyWindowClosing(
  current: ScheduleState | null,
  event: ScheduleWindowClosingEvent,
): ScheduleState {
  return {
    version: event.version ?? current?.version ?? null,
    active: false,
    // The frame names the window that ended; the projection keeps naming it
    // (with active false) so the card can say which window just closed.
    entryId: event.entryId,
    heldIntentId: null,
    endsAt: null,
    endsInS: null,
    next: current?.next ?? null,
    posture: current?.posture ?? "yield",
    lastAction: "remove",
    lastTickAt: current?.lastTickAt ?? "",
    reasonCodes: ["window_ended"],
  };
}

// --- pure civil-time helpers (the containment rule, client-side mirror) --------

/** "HH:MM" -> minutes past midnight; null when not a valid civil time. */
export function minutesOfHHMM(value: string): number | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (match === null) {
    return null;
  }
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  // Civil times are 00:00 through 23:59 ("24:00" is 00:00 of the next day and
  // never a wall time a picker or the policy vocabulary emits).
  if (hours > 23 || minutes > 59) {
    return null;
  }
  return hours * 60 + minutes;
}

/** Minutes past midnight -> "HH:MM" (24 h civil form). */
export function hhmmOfMinutes(minutes: number): string {
  const wrapped = ((minutes % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
  const hours = Math.floor(wrapped / 60);
  const mins = wrapped % 60;
  return `${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

/** The minute set a ["HH:MM","HH:MM"] window covers (cross-midnight aware). */
function minuteSetOf(startLocal: string, endLocal: string): Set<number> | null {
  const start = minutesOfHHMM(startLocal);
  const end = minutesOfHHMM(endLocal);
  if (start === null || end === null) {
    return null;
  }
  const minutes = new Set<number>();
  if (start === end) {
    // A pair like 00:00→00:00 is the full-day window (24 h), matching the
    // policy's own cross-midnight semantics.
    for (let minute = 0; minute < MINUTES_PER_DAY; minute += 1) {
      minutes.add(minute);
    }
    return minutes;
  }
  let cursor = start;
  while (cursor !== end) {
    minutes.add(cursor);
    cursor = (cursor + 1) % MINUTES_PER_DAY;
  }
  return minutes;
}

/** The union of the allowed windows' minutes — the allowed command set. */
export function allowedMinuteSet(windows: readonly (readonly [string, string])[]): Set<number> {
  const allowed = new Set<number>();
  for (const [startLocal, endLocal] of windows) {
    const minutes = minuteSetOf(startLocal, endLocal);
    if (minutes !== null) {
      for (const minute of minutes) {
        allowed.add(minute);
      }
    }
  }
  return allowed;
}

/**
 * The containment rule (§3), mirrored client-side: an entry's window — split
 * across midnight when it crosses — must lie ENTIRELY inside the union of the
 * allowed windows. Null when either time is unusable (invalid input is the
 * field validation's own error, never a containment answer).
 */
export function isWindowAllowed(
  startLocal: string,
  endLocal: string,
  allowed: Set<number>,
): boolean | null {
  const minutes = minuteSetOf(startLocal, endLocal);
  if (minutes === null) {
    return null;
  }
  if (minutes.size === MINUTES_PER_DAY) {
    // The full-day entry: allowed only when the policy grants every minute.
    return allowed.size >= MINUTES_PER_DAY;
  }
  for (const minute of minutes) {
    if (!allowed.has(minute)) {
      return false;
    }
  }
  return true;
}

/**
 * Does the window touch any NIGHT minute — any civil minute outside
 * DAY_DEFAULT, whatever the policy says (§3's named-constant pin)? Under the
 * yield posture such an entry is refused; under partition its FIRST publish
 * needs the one-time acknowledgement.
 */
export function touchesNightMinutes(startLocal: string, endLocal: string): boolean | null {
  const minutes = minuteSetOf(startLocal, endLocal);
  if (minutes === null) {
    return null;
  }
  const day = allowedMinuteSet(DAY_DEFAULT);
  for (const minute of minutes) {
    if (!day.has(minute)) {
      return true;
    }
  }
  return false;
}

// --- plain-language summaries (the operator's words, the lib's figures) --------

/**
 * The site-local wall time of an ISO instant the plan's own zone stamped
 * ("2026-08-24T05:59:00+10:00" -> "05:59"): the offset on the wire IS the
 * site's zone, so the clock time is read straight off the string — never
 * re-zoned into the browser's locale. "" when the instant is unreadable.
 */
export function localTimeOfInstant(instant: string | null): string {
  if (instant === null) {
    return "";
  }
  const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(instant);
  return match === null ? "" : match[1]!;
}

/** "06:00–20:00" or "06:00–08:00 and 18:00–20:00" — the policy's own walls. */
export function allowedWindowsText(windows: readonly (readonly [string, string])[]): string {
  return windows.map(([start, end]) => `${start}–${end}`).join(" and ");
}

/** The days line: "every day", "weekdays", "Sat, Sun", "Mon, Wed, Fri". */
export function daysSummaryText(days: readonly string[]): string {
  const ordered = SCHEDULE_DAYS.filter((day) => days.includes(day));
  if (ordered.length === SCHEDULE_DAYS.length) {
    return "every day";
  }
  if (ordered.length === 0) {
    return "no days";
  }
  if (
    ordered.length === 5 &&
    ["mon", "tue", "wed", "thu", "fri"].every((day) => ordered.includes(day))
  ) {
    return "weekdays";
  }
  if (ordered.length === 2 && ordered.includes("sat") && ordered.includes("sun")) {
    return "weekends";
  }
  return ordered.map((day) => DAY_LABELS[day] ?? day).join(", ");
}

/**
 * The per-battery watts summary (the house doctrine): equal figures say
 * "2,500 W per battery (lhs, mid, rhs)"; mixed figures name each battery; a
 * scalar entry says "fleet total 3,000 W"; idle says "hold to zero".
 */
export function wattsSummaryText(entry: {
  action: ScheduleAction;
  watts: number | null;
  wattsByUnit: WattsByUnit | null;
}): string {
  if (entry.action === "idle") {
    return "hold to zero";
  }
  const map = entry.wattsByUnit;
  if (map === null) {
    return entry.watts === null ? "watts not available" : `fleet total ${formatWatts(entry.watts)}`;
  }
  const unitIds = Object.keys(map);
  const values = unitIds.map((unitId) => map[unitId]!);
  if (values.length > 0 && values.every((watts) => watts === values[0])) {
    return unitIds.length === 1
      ? `${formatWatts(values[0]!)} (${unitIds[0]})`
      : `${formatWatts(values[0]!)} per battery (${unitIds.join(", ")})`;
  }
  return unitIds.map((unitId) => `${unitId} ${formatWatts(map[unitId]!)}`).join(", ");
}

/**
 * A countdown in plain words, from whole seconds: "45 s", "12 min",
 * "1 h 05 min". Reduced-motion-safe by construction — a stable string
 * refreshed by the caller on the snapshot cadence, never an animation.
 */
export function countdownText(seconds: number): string {
  const remaining = Math.max(0, Math.round(seconds));
  if (remaining < 60) {
    return formatSeconds(remaining);
  }
  if (remaining < 3600) {
    return `${Math.floor(remaining / 60)} min`;
  }
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  return minutes === 0 ? `${hours} h` : `${hours} h ${String(minutes).padStart(2, "0")} min`;
}
