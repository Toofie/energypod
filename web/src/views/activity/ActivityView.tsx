// Activity view: the household's audit timeline.
//
// Contract: docs/UI_CONTRACTS.md "Activity" and "State and error contract";
// the authoritative behavior pins live in ActivityView.test.tsx. The view is
// a calm, newest-first timeline from GET /api/v1/audit with cursor
// pagination, plain language first with raw reason codes on demand, and
// honest empty/loading/disconnected/stale/partial/error states. An empty
// Acknowledgements filter says what will appear there (the two *_acknowledged
// audit kinds), so "nothing yet" is never mistaken for "broken".
//
// WIRE TRUTH (service.py `recent_audit` over the audit read model — mirrored
// in web/src/test/wire.ts `WireAuditEvent`): every entry carries the
// `AuditEvent` fields plus the store's integer `sequence`. The kind key is
// `event_type` (there is no `type` field), `principal` is the caller's
// subject string (never an object with a display name), the outcome is
// `result`, the watt figures are the signed `requested_active_w` /
// `authorized_active_w`, and the reasons are `reason_codes`. The event types
// the service writes today are control_decision, intent_accepted, unit_armed,
// unit_disarmed, emergency_stop, stop_acknowledged, inhibit_acknowledged and
// authorization_revoked; anything else is rendered defensively, never
// crashed on, and never invented.
//
// LIVE TRUTH (runtime/composition.py): the audit trail holds NO observation
// entries — observations are published on the event bus (`observation.published`,
// one per telemetry append), never audited — and the REST read is a point-in-time
// page, so a view that only reads once shows nothing new for the whole session.
// This view therefore also subscribes to the session's shared event stream and
// appends what the backend actually emits as it happens: `audit.appended`
// frames become timeline entries immediately (their `event_id` is the same
// identity a later REST page carries, so a refresh never duplicates them),
// and `observation.published` frames become the live observation entries the
// Observations chip has always promised. Session-live entries render above
// the loaded history; they are capped so a long session cannot grow without
// bound.

import { Fragment } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, AuditEvent, StreamEvent } from "../../api/client";
import { toUnexpectedAutonomyEvent } from "../../app/fleet";
import { objectiveReasonText, toForeignObjectiveEvent } from "../../app/objectives";
import {
  localTimeOfInstant,
  toScheduleReplacedEvent,
  toScheduleWindowOpenedEvent,
  wattsSummaryText,
} from "../../app/schedule";
import { formatWatts } from "../../lib/format";
import "./activity.css";

export type ActivityConnection = "connected" | "disconnected";

export interface ActivityViewProps {
  client: ApiClient;
  /**
   * The shell's connection fact for the shared data plane, when the shell
   * provides one. Defaults to "connected" so the view stands alone: the
   * notice is additive and never replaces the REST data the view owns.
   */
  connection?: ActivityConnection | undefined;
}

/** The audit page size the view asks for; the cursor pagination honors it. */
const AUDIT_PAGE_SIZE = 50;

/**
 * The fleet's named units (UI_CONTRACTS.md "Batteries": MID, RHS, LHS) as
 * display labels. The wire's canonical ids are lowercase (`mid`, `rhs`,
 * `lhs` — the audit `unit_id`, the snapshot, and the bus frames all carry
 * them that way), so the chips show the display name but filter by the
 * canonical id; the comparison itself normalizes both sides, because a wire
 * that once carried uppercase ids must not silently empty the filter again.
 */
const FLEET_UNIT_LABELS: readonly string[] = ["MID", "RHS", "LHS"];

/** The canonical form of a unit id: lowercase, trimmed. */
function canonicalUnitId(value: string): string {
  return value.trim().toLowerCase();
}

/** Entries older than this are stale: dimmed, never hidden, age kept. */
const STALE_AFTER_MS = 60 * 60_000;

/** Reconnect pause for the shared event stream: short enough that a dropped
 * line is seen retrying within a glance, never a tight spin. */
const RECONNECT_DELAY_MS = 300;

/**
 * How many session-live entries (bus frames) the timeline keeps. Observations
 * land every telemetry cycle (~2 s), so the live list is bounded: the newest
 * are kept and older ones age out of the session list (the audit REST history
 * remains the durable record for everything audited).
 */
const MAX_LIVE_ENTRIES = 100;

/** Placeholder rows inside the loading status; bounded by the page size. */
const SKELETON_ROWS = 6;

type KindKey =
  | "observations"
  | "decisions"
  | "arming"
  | "stops"
  | "acknowledgements";

const KIND_CHIPS: readonly { readonly id: KindKey; readonly label: string }[] =
  [
    { id: "observations", label: "Observations" },
    { id: "decisions", label: "Decisions" },
    { id: "arming", label: "Arming" },
    { id: "stops", label: "Stops" },
    { id: "acknowledgements", label: "Acknowledgements" },
  ];

/** The `event_type` values the audit trail holds (wire.ts AUDIT_EVENT_TYPES). */
const AUDIT_TYPE_DECISIONS: readonly string[] = [
  "control_decision",
  "intent_accepted",
  // The night strategy's participation toggle (DESIGN_NIGHT_CHARGE §5 audit):
  // an operator control act over the fleet — the Decisions family.
  "night_charging_toggled",
];
const AUDIT_TYPE_ARMING: readonly string[] = ["unit_armed", "unit_disarmed"];
const AUDIT_TYPE_STOPS: readonly string[] = ["emergency_stop", "authorization_revoked"];
const AUDIT_TYPE_ACKNOWLEDGEMENTS: readonly string[] = [
  "stop_acknowledged",
  "inhibit_acknowledged",
  // The one-time night-partition fact (§3.2): an acknowledgement whichever
  // surface captured it — the night toggle's first enable or the schedules
  // publish path. It is one durable site fact, never a stop.
  "schedule_night_windows_acknowledged",
];

/**
 * The five contracted kinds. The audit trail holds no observation entries
 * today (observations are published on the event bus, never audited), so the
 * Observations chip selects an empty set until the service audits them; arm
 * and disarm are one kind (Arming) and every acknowledgement is an
 * Acknowledgement, never a Stop.
 */
function kindOf(eventType: string): KindKey | "other" {
  if (eventType.startsWith("observation")) {
    return "observations";
  }
  // The night-writer detector's alert fact is recorded evidence of what the
  // battery was doing — the Observations family (the chip is a filter, never
  // a tier; the entry's own wording carries the alert).
  if (eventType === "foreign_objective_observed") {
    return "observations";
  }
  if (AUDIT_TYPE_DECISIONS.includes(eventType)) {
    return "decisions";
  }
  if (AUDIT_TYPE_ARMING.includes(eventType)) {
    return "arming";
  }
  if (AUDIT_TYPE_STOPS.includes(eventType)) {
    return "stops";
  }
  if (AUDIT_TYPE_ACKNOWLEDGEMENTS.includes(eventType)) {
    return "acknowledgements";
  }
  return "other";
}

/**
 * The filtered-empty message. The Acknowledgements chip can be empty because
 * no acknowledgement has ever happened — a state the operator must be able to
 * tell apart from a filter that merely narrowed entries away — so when the
 * loaded timeline holds no acknowledgement at all, the message names what
 * will appear there (the state contract's "empty explains what will appear
 * here"). It stays honest about pagination: with more history unloaded, older
 * acknowledgements may simply be beyond the loaded page. A per-unit view that
 * matches nothing says why the fleet-wide rows are absent, because the audit
 * trail attributes decisions and stops to no unit.
 */
function filteredEmptyText(
  kindFilter: KindKey | null,
  unitFilter: string | null,
  acknowledgementsLoaded: boolean,
  moreHistory: boolean,
  unitlessLoaded: boolean,
): string {
  if (unitFilter !== null) {
    const label = unitFilter.toUpperCase();
    return `No ${label} activity matches these filters.${
      unitlessLoaded
        ? " Fleet-wide decisions and stops carry no unit id, so they appear only under All units."
        : ""
    }`;
  }
  if (kindFilter !== "acknowledgements" || acknowledgementsLoaded) {
    return "No activity matches these filters";
  }
  const whatAppears =
    "One appears here each time a latched emergency stop or a latched unit inhibit is acknowledged.";
  return moreHistory
    ? `No acknowledgements in the activity loaded so far — Load more reaches older entries. ${whatAppears}`
    : `No acknowledgements yet. ${whatAppears}`;
}

// ---------------------------------------------------------------------------
// Defensive field access: the wire shape is read at one boundary, a missing
// field is named as missing, and nothing is ever invented.
// ---------------------------------------------------------------------------

function stringField(event: AuditEvent, key: string): string | undefined {
  const value = event[key];
  return typeof value === "string" && value !== "" ? value : undefined;
}

function numberField(event: AuditEvent, key: string): number | undefined {
  const value = event[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** The kind key on the audit wire is `event_type`; a legacy `type` field is
 * tolerated defensively, and an entry without either still renders. */
function eventTypeOf(event: AuditEvent): string {
  return stringField(event, "event_type") ?? stringField(event, "type") ?? "";
}

function sequenceOf(event: AuditEvent, fallback: number): number {
  return numberField(event, "sequence") ?? fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * One entry's stable identity. The bus summary and the REST read model carry
 * the same `event_id`, so a live-appended audit entry and the REST copy of the
 * same fact dedupe; entries without an event_id (observations) fall back to
 * their type plus sequence, which never collides with the audit store's own
 * sequence space.
 */
function identityOf(event: AuditEvent): string {
  const id = stringField(event, "event_id");
  if (id !== undefined) {
    return `id:${id}`;
  }
  return `seq:${eventTypeOf(event)}:${sequenceOf(event, 0)}`;
}

/**
 * An `audit.appended` frame as a timeline entry: the payload IS the audit
 * summary (event_id, event_type, unit_id, result, reason_codes), and the bus
 * envelope contributes the ordering `sequence` and the `occurred_at` stamp.
 */
function auditEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const payload = frame.payload;
  if (!isRecord(payload)) {
    return null;
  }
  const entry: Record<string, unknown> = { ...payload };
  if (typeof frame.sequence === "number") {
    entry.sequence = frame.sequence;
  }
  if (typeof frame.occurred_at === "string") {
    entry.occurred_at = frame.occurred_at;
  }
  return entry as unknown as AuditEvent;
}

/**
 * An `observation.published` frame as a timeline entry. The frame is minimal
 * by design (unit identity and the telemetry sequence, no readings), so the
 * entry says exactly that — a reading landed for this unit — and invents
 * nothing.
 */
function observationEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const payload = frame.payload;
  if (!isRecord(payload) || typeof payload.unit_id !== "string") {
    return null;
  }
  return {
    event_type: "observation",
    unit_id: payload.unit_id,
    occurred_at: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
    sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
    telemetry_sequence: typeof payload.sequence === "number" ? payload.sequence : null,
  } as unknown as AuditEvent;
}

/**
 * A `unit.unexpected_autonomy` frame as a QUIET timeline entry (the
 * self-healing awareness layer's evidence recorder): the measured watts the
 * backend pinned while NO request claimed the battery — mid's standing
 * uncommanded oscillation class. It is deliberately informational: no banner,
 * no badge, no alarm wording, exactly the recorded evidence the backend is
 * collecting, throttled to one frame per unit per 60 s by the backend itself.
 */
function autonomyEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const evidence = toUnexpectedAutonomyEvent(frame.payload);
  if (evidence === null) {
    return null;
  }
  return {
    event_type: "unexpected_autonomy",
    unit_id: evidence.unitId,
    occurred_at: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
    sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
    measured_watts: evidence.measuredWatts,
  } as unknown as AuditEvent;
}

/**
 * A `foreign_objective.observed` frame as the night-writer detector's
 * ALERT-TIER timeline entry: another writer is commanding this battery. The
 * entry carries the objective's words and the detector's one-word reason (in
 * plain words in the line, raw under the disclosure); the event fires exactly
 * once per (episode, reason), so one row is one real alert — the continuous
 * evidence lives in the Objectives view, and the durable audit fact of the
 * same episode arrives separately as an `audit.appended` row with its own
 * event_id (the two never dedupe; they are two facts).
 */
function foreignObjectiveEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const event = toForeignObjectiveEvent(frame.payload);
  if (event === null) {
    return null;
  }
  return {
    event_type: "foreign_objective_observed",
    unit_id: event.unitId,
    occurred_at: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
    sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
    active_w: event.activeW,
    reactive_var: event.reactiveVar,
    reason: event.reason ?? undefined,
    lifecycle: event.lifecycle ?? undefined,
    run_mode_w: event.runModeW ?? undefined,
  } as unknown as AuditEvent;
}

/**
 * A `schedule.replaced` frame as a QUIET timeline entry (§6 W-D): one row per
 * publish, the plain diff the payload carries ("Schedule v2→v3 — added Night
 * Charge, removed old-evening"). Informational only — publishing is an
 * operator act, not an alarm; the same sentence the audit row will carry.
 */
function scheduleReplacedEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const event = toScheduleReplacedEvent(frame.payload);
  if (event === null) {
    return null;
  }
  return {
    event_type: "schedule_replaced",
    occurred_at: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
    sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
    principal: event.principal ?? undefined,
    version: event.version ?? undefined,
    added: [...event.diff.added],
    removed: [...event.diff.removed],
    changed: [...event.diff.changed],
  } as unknown as AuditEvent;
}

/**
 * A `schedule_window.opened` frame as a QUIET timeline entry: a published
 * window's command began — the entry, its direction, its per-battery watts,
 * and the local time it ends. The window's end is the intent lifecycle's own
 * business (the runner simply stops renewing); no closing row is invented.
 */
function scheduleWindowOpenedEntryFromFrame(frame: StreamEvent): AuditEvent | null {
  const event = toScheduleWindowOpenedEvent(frame.payload);
  if (event === null) {
    return null;
  }
  return {
    event_type: "schedule_window_opened",
    occurred_at: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
    sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
    entry_id: event.entryId,
    action: event.action,
    ...(event.wattsByUnit === null ? {} : { watts_by_unit: event.wattsByUnit }),
    ...(event.watts === null ? {} : { watts: event.watts }),
    unit_ids: [...event.unitIds],
    ends_at: event.endsAt ?? undefined,
  } as unknown as AuditEvent;
}

/** A raw wire code as calm words: telemetry_stale -> "Telemetry stale". */
function humanize(code: string): string {
  const words = code.toLowerCase().split(/[_\s]+/).filter((word) => word !== "");
  if (words.length === 0) {
    return code;
  }
  const first = words[0] ?? "";
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(
    " ",
  );
}

/** The entry's age in household words ("2 hours ago"), "" when unknown. */
function ageText(occurredAt: string | undefined, now: number): string {
  if (occurredAt === undefined) {
    return "";
  }
  const when = Date.parse(occurredAt);
  if (Number.isNaN(when)) {
    return "";
  }
  const minutes = Math.max(0, Math.floor((now - when) / 60_000));
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function isStale(occurredAt: string | undefined, now: number): boolean {
  if (occurredAt === undefined) {
    return false;
  }
  const when = Date.parse(occurredAt);
  return !Number.isNaN(when) && now - when >= STALE_AFTER_MS;
}

/** The timeline is newest-first: highest sequence on top, stably. */
function newestFirst(events: AuditEvent[]): AuditEvent[] {
  return [...events].sort(
    (a, b) => sequenceOf(b, 0) - sequenceOf(a, 0),
  );
}

/** Older pages append below without duplicating an already-loaded sequence. */
function mergeBySequence(
  existing: AuditEvent[],
  incoming: AuditEvent[],
): AuditEvent[] {
  const seen = new Set(existing.map((event) => sequenceOf(event, 0)));
  const fresh = incoming.filter((event) => !seen.has(sequenceOf(event, 0)));
  return newestFirst([...existing, ...fresh]);
}

function reasonCodesOf(event: AuditEvent): string[] {
  const value = event.reason_codes;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (code): code is string => typeof code === "string" && code !== "",
  );
}

/** `principal` is the caller's subject string; it is shown verbatim. */
function principalName(event: AuditEvent): string | null {
  const principal = stringField(event, "principal");
  return principal ?? null;
}

function headlineFor(eventType: string): string {
  switch (eventType) {
    case "control_decision":
      return "Power decision";
    case "intent_accepted":
      return "Dispatch request accepted";
    case "observation":
      return "Observation";
    case "unexpected_autonomy":
      // Quiet-tier evidence (the awareness layer's recorder): the headline is
      // the whole alarm budget this entry ever gets.
      return "Uncommanded activity";
    case "foreign_objective_observed":
      // The night-writer detector's ALERT TIER: another writer is commanding
      // this battery. The headline states it plainly; the line below carries
      // the evidence (the objective's words and the pattern reason).
      return "Commanded by something else";
    case "schedule_replaced":
      // Quiet informational (§6 W-D): a publish is an operator act, and the
      // diff below is the whole story.
      return "Schedule published";
    case "schedule_window_opened":
      return "Schedule window opened";
    case "night_charging_toggled":
      // Quiet informational (DESIGN_NIGHT_CHARGE §5 audit): the participation
      // gate flipped — an operator act, never an alarm.
      return "Night charging toggle";
    case "schedule_night_windows_acknowledged":
      // The one-time night-partition fact, whichever surface captured it.
      return "Night windows acknowledged";
    case "unit_armed":
      return "Arm request";
    case "unit_disarmed":
      return "Disarm request";
    case "emergency_stop":
      return "Emergency stop";
    case "stop_acknowledged":
      return "Stop acknowledgement";
    case "inhibit_acknowledged":
      return "Inhibit acknowledgement";
    case "authorization_revoked":
      return "Authorization revoked";
    default:
      return eventType === "" ? "Unknown event" : humanize(eventType);
  }
}

/**
 * The decision reason code that means "an engaged emergency stop is holding
 * every cycle at 0 W" (safety.py: the kernel authorizes the zero setpoints a
 * latched stop leaves behind). The stop-holding cycles must never read as an
 * ordinary "power allowed" grant.
 */
const STOP_AUTHORIZED = "stop_authorized";

/** A decision row the stop is holding: the honest line, with the stop named. */
function stopHeldLine(stopId: string | null): string {
  return stopId === null
    ? "Emergency stop active — all power held at 0 W"
    : `Emergency stop ${stopId} active — all power held at 0 W`;
}

function isStopHeldDecision(event: AuditEvent): boolean {
  return (
    eventTypeOf(event) === "control_decision" &&
    reasonCodesOf(event).includes(STOP_AUTHORIZED)
  );
}

/**
 * The id of the newest latched stop in the loaded timeline. The audit
 * `emergency_stop` row carries the stop id as its `intent_id`, so the
 * stop-holding decision rows can name the stop that held them. Null when no
 * stop row is loaded — the honest line then names no id, never an invented
 * one.
 */
function newestStopId(events: AuditEvent[]): string | null {
  let best: { sequence: number; stopId: string } | null = null;
  for (const event of events) {
    if (eventTypeOf(event) !== "emergency_stop" || stringField(event, "result") !== "latched") {
      continue;
    }
    const stopId = stringField(event, "intent_id");
    if (stopId === undefined) {
      continue;
    }
    const sequence = sequenceOf(event, 0);
    if (best === null || sequence > best.sequence) {
      best = { sequence, stopId };
    }
  }
  return best === null ? null : best.stopId;
}

/** What was decided, from the decision status (`result`) and the signed watt
 * figures the audit record carries. Charge figures are negative on the wire;
 * the household sees magnitudes with the direction named in words. A cycle a
 * latched stop held at 0 W says the stop held it — never "allowed 0 W of the
 * 0 W requested". */
function decidedLine(event: AuditEvent, stopId: string | null): string | null {
  const eventType = eventTypeOf(event);
  const result = stringField(event, "result");
  if (result === undefined) {
    return null;
  }
  if (eventType === "control_decision" && (result === "clamped" || result === "authorized")) {
    const requested = numberField(event, "requested_active_w");
    const authorized = numberField(event, "authorized_active_w");
    if (requested !== undefined && authorized !== undefined) {
      if (isStopHeldDecision(event)) {
        return stopHeldLine(stopId);
      }
      return result === "clamped"
        ? `Reduced to ${formatWatts(Math.abs(authorized))} of the ${formatWatts(
            Math.abs(requested),
          )} requested`
        : `Allowed ${formatWatts(Math.abs(authorized))} of the ${formatWatts(
            Math.abs(requested),
          )} requested`;
    }
  }
  switch (result) {
    case "authorized":
    case "clamped":
      return "Allowed as requested";
    case "rejected":
    case "refused":
      return "Not allowed";
    case "accepted":
      return "Request accepted";
    case "armed":
      return "Arm allowed";
    case "disarmed":
      return "Disarm allowed";
    case "latched":
      return "Stop accepted";
    case "acknowledged":
      return "Acknowledgement accepted";
    default:
      return humanize(result);
  }
}

/** What happened. The audit record's own `result`, in words — a missing
 * result is named as missing, never a fabricated measurement. A live
 * observation entry has no result at all: what happened is that a reading
 * landed, named with the telemetry sequence the frame carries. A stop-held
 * cycle says the stop held it, never "power allowed". */
function happenedLine(event: AuditEvent, stopId: string | null): string | null {
  const eventType = eventTypeOf(event);
  if (eventType === "foreign_objective_observed") {
    // The alert tier's evidence line: the objective's own words with the
    // detector's pattern reason in plain words. A bus frame carries the words
    // (active_w/reason); the durable audit row of the same episode does not
    // (its payload is the audit summary), so it gets the honest figureless
    // line — the Objectives view carries the full window either way.
    const active = numberField(event, "active_w");
    const reason = stringField(event, "reason");
    const reasonText = reason === undefined ? "" : ` — ${objectiveReasonText(reason)}`;
    if (active !== undefined) {
      return `Another writer is commanding this battery: ${formatWatts(active)}${reasonText}`;
    }
    return `Another writer's objective was recorded${reasonText}`;
  }
  if (kindOf(eventType) === "observations") {
    const telemetrySequence = numberField(event, "telemetry_sequence");
    return telemetrySequence !== undefined
      ? `Latest reading received (telemetry sequence ${telemetrySequence})`
      : "Latest reading received";
  }
  if (eventType === "unexpected_autonomy") {
    // The recorded evidence itself, in household words: the measured figure
    // while nothing claimed the battery. Informational wording only — this
    // entry exists so the operator can SEE the evidence the backend collects.
    const measured = numberField(event, "measured_watts");
    return measured !== undefined
      ? `Measured ${formatWatts(measured)} with no request claiming this battery`
      : "Measured with no request claiming this battery";
  }
  if (eventType === "schedule_replaced") {
    // The plain diff sentence the audit row carries too: versions, then the
    // entry-level changes. A REST-loaded audit row may spell the versions
    // version_from/version_to; a bus frame carries the new `version`.
    const from = numberField(event, "version_from");
    const to = numberField(event, "version_to") ?? numberField(event, "version");
    const source = isRecord(event.diff) ? event.diff : event;
    const parts: string[] = [];
    for (const [key, verb] of [
      ["added", "added"],
      ["removed", "removed"],
      ["changed", "changed"],
    ] as const) {
      const names = Array.isArray(source[key])
        ? (source[key] as unknown[]).filter((name): name is string => typeof name === "string")
        : [];
      if (names.length > 0) {
        parts.push(`${verb} ${names.join(", ")}`);
      }
    }
    const versions =
      from !== undefined && to !== undefined
        ? `v${from}→v${to}`
        : to !== undefined
          ? `v${to}`
          : "a new version";
    return `Schedule ${versions} published${parts.length === 0 ? " — no entry changes" : ` — ${parts.join(", ")}`}`;
  }
  if (eventType === "schedule_window_opened") {
    // The window's own command: the entry, its direction and figures, and the
    // local time it ends. Quiet wording — the request it holds is an ordinary
    // request and already has its own rows while it runs.
    const entryId = stringField(event, "entry_id");
    const wattsByUnit =
      isRecord(event.watts_by_unit) ? (event.watts_by_unit as Record<string, number>) : null;
    const summary = wattsSummaryText({
      action: (stringField(event, "action") ?? "charge") as "charge" | "discharge" | "idle",
      watts: numberField(event, "watts") ?? null,
      wattsByUnit,
    });
    const endsAt = stringField(event, "ends_at");
    const endsAtText = localTimeOfInstant(endsAt ?? null);
    const endsWord = endsAtText === "" ? "" : ` until ${endsAtText}`;
    return `${entryId ?? "A scheduled window"} began — ${summary}${endsWord}`;
  }
  if (eventType === "night_charging_toggled") {
    // The participation gate's own result vocabulary (§5 audit:
    // enabled/disabled/noop), in household words. Quiet informational — the
    // tile and the projection carry the live story.
    const result = stringField(event, "result");
    if (result === "enabled") {
      return "Night charging turned on";
    }
    if (result === "disabled") {
      return "Night charging turned off";
    }
    if (result === "noop") {
      return "No change — night charging was already in that state";
    }
    return "Night charging toggled";
  }
  if (eventType === "schedule_night_windows_acknowledged") {
    // The durable once-only fact: the night window belongs to the controller
    // and the external writers stand down. Never re-prompted — the row is the
    // history, not a question.
    return "The one-time night-partition acknowledgement was captured — the night window belongs to the controller";
  }
  if (isStopHeldDecision(event)) {
    return stopHeldLine(stopId);
  }
  const result = stringField(event, "result");
  if (result === undefined) {
    return "Result not recorded yet";
  }
  switch (result) {
    case "authorized":
      return eventType === "control_decision" ? "Power allowed" : "Allowed";
    case "clamped":
      return "Power reduced by the safety system";
    case "rejected":
    case "refused":
      return "Refused — nothing was authorized";
    case "accepted":
      return "Accepted";
    case "armed":
      return "Armed";
    case "disarmed":
      return "Disarmed";
    case "latched": {
      const stopId = stringField(event, "intent_id");
      return stopId === undefined ? "Stop latched" : `Stop latched (${stopId})`;
    }
    case "acknowledged":
      return kindOf(eventType) === "acknowledgements" ? "Acknowledged" : humanize(result);
    case "revoked":
      return "Authorization revoked";
    default:
      return humanize(result);
  }
}

function whyLine(codes: string[]): string | null {
  return codes.length > 0 ? codes.map(humanize).join(", ") : null;
}

/** The API's error envelope, kept verbatim for rendering. */
interface ErrorView {
  code: string;
  message: string;
  requestId: string;
}

function toErrorView(error: unknown): ErrorView {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message: error.message,
      requestId: error.request_id,
    };
  }
  return {
    code: "activity_unavailable",
    message:
      error instanceof Error
        ? error.message
        : "The activity could not be loaded",
    requestId: "",
  };
}

type Phase = "first-load" | "ready" | "error";

export function ActivityView({ client, connection = "connected" }: ActivityViewProps) {
  const [phase, setPhase] = useState<Phase>("first-load");
  const [skeletonConfirmed, setSkeletonConfirmed] = useState(false);
  const settledRef = useRef(false);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [pageError, setPageError] = useState<ErrorView | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<ErrorView | null>(null);
  const [kindFilter, setKindFilter] = useState<KindKey | null>(null);
  const [unitFilter, setUnitFilter] = useState<string | null>(null);
  /**
   * Session-live entries appended from the shared event stream, newest first.
   * The REST page is the durable history; these are the facts the backend
   * emits while the operator watches (audit.appended and, the thing the audit
   * trail never holds, observation.published).
   */
  const [liveEntries, setLiveEntries] = useState<AuditEvent[]>([]);

  const applyFirstPage = (page: { events: AuditEvent[]; next_cursor: number | null }) => {
    setEvents(newestFirst(page.events));
    setNextCursor(page.next_cursor);
  };

  /** Append one live entry, deduped by identity and capped to the bound. */
  const appendLiveEntry = useCallback((entry: AuditEvent) => {
    setLiveEntries((previous) => {
      const key = identityOf(entry);
      if (previous.some((existing) => identityOf(existing) === key)) {
        return previous;
      }
      return [entry, ...previous].slice(0, MAX_LIVE_ENTRIES);
    });
  }, []);

  // First load: REST is the view's own source of truth, whatever the socket
  // is doing (the disconnected notice is additive, never a substitute).
  useEffect(() => {
    let cancelled = false;
    settledRef.current = false;
    const load = async () => {
      try {
        const page = await client.getAudit(AUDIT_PAGE_SIZE);
        settledRef.current = true;
        if (cancelled) {
          return;
        }
        applyFirstPage(page);
        setPageError(null);
        setPhase("ready");
      } catch (error) {
        settledRef.current = true;
        if (cancelled) {
          return;
        }
        setPageError(toErrorView(error));
        setPhase("error");
      }
    };
    void load();
    // The skeleton placeholder rows wait one tick: a page that answers
    // within the current task never flashes placeholder entries (no layout
    // shift, no fake rows), while any real wait still gets the full
    // skeleton inside the loading status. The settlement check runs after
    // the fetch's own continuation when the promise is already settled.
    queueMicrotask(() => {
      if (!cancelled && !settledRef.current) {
        setSkeletonConfirmed(true);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // The live bus: new facts appear on the timeline as they happen. Without
  // this subscription the view is a point-in-time REST page — an operator who
  // opens Activity and then acts sees nothing change for the whole session.
  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      while (!cancelled) {
        let resync = false;
        try {
          for await (const frame of client.openEvents()) {
            if (cancelled) {
              return;
            }
            if (frame.type === "resync_required") {
              resync = true;
              break;
            }
            if (frame.type === "audit.appended") {
              const entry = auditEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            } else if (frame.type === "observation.published") {
              const entry = observationEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            } else if (frame.type === "unit.unexpected_autonomy") {
              // Quiet-tier evidence, never an alarm: the timeline entry is
              // the whole console surface for this frame.
              const entry = autonomyEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            } else if (frame.type === "foreign_objective.observed") {
              // The night-writer detector's ALERT TIER: one row per real
              // alert (the backend fires exactly once per episode+reason).
              const entry = foreignObjectiveEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            } else if (frame.type === "schedule.replaced") {
              // Quiet informational (§6 W-D): one timeline row per publish,
              // the plain diff the payload carries.
              const entry = scheduleReplacedEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            } else if (frame.type === "schedule_window.opened") {
              // Quiet informational: a published window's command began. The
              // window's end needs no row of its own — it is the intent
              // lifecycle's ordinary end (non-renewal).
              const entry = scheduleWindowOpenedEntryFromFrame(frame);
              if (entry !== null) {
                appendLiveEntry(entry);
              }
            }
          }
        } catch {
          // A failed stream is treated exactly like a dropped one: retry.
        }
        if (cancelled) {
          return;
        }
        if (resync) {
          // A discontinuity means the loaded history may be out of step; one
          // quiet refresh re-reads it (merged, so loaded history is kept).
          try {
            const page = await client.getAudit(AUDIT_PAGE_SIZE);
            if (cancelled) {
              return;
            }
            setEvents((previous) => mergeBySequence(previous, page.events));
            setNextCursor(page.next_cursor);
          } catch {
            // The manual refresh remains available; live entries keep flowing.
          }
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, RECONNECT_DELAY_MS);
        });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [client, appendLiveEntry]);

  // Retry with nothing loaded: the error stays on screen (no skeleton flash,
  // no fake entries) until the new page actually lands.
  const retryFirstPage = useCallback(async () => {
    if (retrying) {
      return;
    }
    setRetrying(true);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE);
      applyFirstPage(page);
      setPageError(null);
      setPhase("ready");
    } catch (error) {
      setPageError(toErrorView(error));
      setPhase("error");
    } finally {
      setRetrying(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, retrying]);

  // Manual REST refresh while data is on screen: entries stay visible; the
  // fresh page merges in so loaded history is never dropped.
  const refreshPage = useCallback(async () => {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setPageError(null);
    setMoreError(null);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE);
      setEvents((previous) => mergeBySequence(previous, page.events));
      setNextCursor(page.next_cursor);
    } catch (error) {
      setPageError(toErrorView(error));
    } finally {
      setRefreshing(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, refreshing]);

  const retry = useCallback(() => {
    if (phase === "ready") {
      void refreshPage();
    } else {
      void retryFirstPage();
    }
  }, [phase, refreshPage, retryFirstPage]);

  // Load more passes the previous page's next_cursor; a null cursor means
  // the end of the timeline and the control disappears.
  const loadMore = useCallback(async () => {
    if (nextCursor === null || loadingMore) {
      return;
    }
    setLoadingMore(true);
    setPageError(null);
    setMoreError(null);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE, nextCursor);
      setEvents((previous) => mergeBySequence(previous, page.events));
      setNextCursor(page.next_cursor);
    } catch (error) {
      setMoreError(toErrorView(error));
    } finally {
      setLoadingMore(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, nextCursor, loadingMore]);

  /** REST entries whose identity a live frame already delivered: a later
   * refresh must not duplicate what the bus already appended. */
  const liveIdentities = useMemo(() => new Set(liveEntries.map(identityOf)), [liveEntries]);
  /** The rendered timeline: session-live entries first (they happened after
   * everything the REST page loaded), then the loaded history beneath. */
  const timeline = useMemo(() => {
    const rest = events.filter((event) => !liveIdentities.has(identityOf(event)));
    return [...liveEntries, ...rest];
  }, [events, liveEntries, liveIdentities]);
  const visibleEvents = useMemo(
    () =>
      timeline.filter(
        (event) =>
          (kindFilter === null || kindOf(eventTypeOf(event)) === kindFilter) &&
          (unitFilter === null ||
            canonicalUnitId(stringField(event, "unit_id") ?? "") ===
              unitFilter),
      ),
    [timeline, kindFilter, unitFilter],
  );
  const acknowledgementsLoaded = useMemo(
    () =>
      timeline.some(
        (event) => kindOf(eventTypeOf(event)) === "acknowledgements",
      ),
    [timeline],
  );
  /** Fleet-wide rows (decisions, stops) carry no unit id; a per-unit view
   * that matches nothing names them so their absence is explained, not hid. */
  const unitlessLoaded = useMemo(
    () =>
      timeline.some((event) => stringField(event, "unit_id") === undefined),
    [timeline],
  );
  /** The newest latched stop the loaded timeline names: stop-held decision
   * rows say which stop held them instead of "power allowed". */
  const newestLatchedStopId = useMemo(() => newestStopId(timeline), [timeline]);

  const toggleKind = useCallback((id: KindKey) => {
    setKindFilter((previous) => (previous === id ? null : id));
  }, []);

  const toggleUnit = useCallback((label: string) => {
    // The chip carries the display name; the filter holds the canonical id.
    const canonical = canonicalUnitId(label);
    setUnitFilter((previous) => (previous === canonical ? null : canonical));
  }, []);

  const clearUnit = useCallback(() => {
    setUnitFilter(null);
  }, []);

  const now = Date.now();

  return (
    <section className="activity-view" aria-labelledby="activity-view-heading">
      <header className="activity-view__header">
        <h2 id="activity-view-heading">Activity</h2>
        <p className="activity-view__intro">
          Everything your EnergyPod did, newest first.
        </p>
      </header>

      {connection === "disconnected" && (
        <div role="status" className="activity-notice">
          <p className="activity-notice__title">Connection lost</p>
          <p className="activity-notice__body">
            We're showing the activity we have, with how old each entry is.
          </p>
          {pageError === null && moreError === null && (
            <button
              type="button"
              className="activity-notice__retry"
              onClick={retry}
              disabled={refreshing}
            >
              Try again
            </button>
          )}
        </div>
      )}

      {phase === "first-load" && <LoadingSkeleton placeholders={skeletonConfirmed} />}

      {phase === "error" && pageError !== null && (
        <ActivityError
          title="We couldn't load the activity just now."
          error={pageError}
          retryDisabled={retrying}
          onRetry={retry}
        />
      )}

      {phase === "ready" && (
        <>
          <ActivityFilters
            kindFilter={kindFilter}
            unitFilter={unitFilter}
            onToggleKind={toggleKind}
            onToggleUnit={toggleUnit}
            onClearUnit={clearUnit}
          />

          {pageError !== null && (
            <ActivityError
              title="We couldn't refresh the activity."
              error={pageError}
              retryDisabled={refreshing}
              onRetry={retry}
            />
          )}

          {timeline.length === 0 ? (
            <EmptyActivity />
          ) : visibleEvents.length === 0 ? (
            <p className="activity-filtered-empty">
              {filteredEmptyText(
                kindFilter,
                unitFilter,
                acknowledgementsLoaded,
                nextCursor !== null,
                unitlessLoaded,
              )}
            </p>
          ) : (
            <ol
              className={
                connection === "disconnected"
                  ? "activity-timeline activity-timeline--dimmed"
                  : "activity-timeline"
              }
            >
              {visibleEvents.map((event) => (
                <ActivityEntry
                  key={identityOf(event)}
                  event={event}
                  now={now}
                  latchedStopId={newestLatchedStopId}
                />
              ))}
            </ol>
          )}

          {nextCursor !== null && moreError === null && (
            <div className="activity-load-more-row">
              <button
                type="button"
                className="activity-load-more"
                onClick={() => void loadMore()}
                disabled={loadingMore}
              >
                Load more
              </button>
            </div>
          )}

          {moreError !== null && (
            <ActivityError
              title="We couldn't load more activity."
              error={moreError}
              retryDisabled={loadingMore}
              onRetry={() => void loadMore()}
            />
          )}
        </>
      )}
    </section>
  );
}

/** Skeleton placeholder entries inside the loading status region: structural
 * rows that carry no data and never name a unit. The rows appear once the
 * wait has outlived the current task, so an instantly answered page never
 * flashes placeholders. */
function LoadingSkeleton({ placeholders }: { placeholders: boolean }) {
  return (
    <div role="status" aria-label="Loading activity" className="activity-loading">
      <p className="activity-loading__text">Loading activity…</p>
      {placeholders && (
        <ul className="activity-loading__list">
          {Array.from({ length: SKELETON_ROWS }, (_, index) => (
            <li key={index} className="activity-loading__row" />
          ))}
        </ul>
      )}
    </div>
  );
}

function ActivityFilters({
  kindFilter,
  unitFilter,
  onToggleKind,
  onToggleUnit,
  onClearUnit,
}: {
  kindFilter: KindKey | null;
  unitFilter: string | null;
  onToggleKind: (id: KindKey) => void;
  onToggleUnit: (id: string) => void;
  onClearUnit: () => void;
}) {
  return (
    <div className="activity-filters">
      <div className="activity-filters__group" role="group" aria-label="Filter by kind">
        {KIND_CHIPS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            className="activity-chip"
            aria-pressed={kindFilter === id}
            onClick={() => onToggleKind(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="activity-filters__group" role="group" aria-label="Filter by unit">
        <button
          type="button"
          className="activity-chip"
          aria-pressed={unitFilter === null}
          onClick={onClearUnit}
        >
          All units
        </button>
        {FLEET_UNIT_LABELS.map((label) => (
          <button
            key={label}
            type="button"
            className="activity-chip"
            aria-pressed={unitFilter === canonicalUnitId(label)}
            onClick={() => onToggleUnit(label)}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

function EmptyActivity() {
  return (
    <div className="activity-empty">
      <h3 className="activity-empty__title">Nothing here yet</h3>
      <p className="activity-empty__body">
        Decisions, observations, arming, stops and acknowledgements will appear
        here as your EnergyPod runs.
      </p>
      <p className="activity-empty__first">
        To make your first request, open the Now view.
      </p>
    </div>
  );
}

/** The API's error envelope verbatim — code, message, request id — with the
 * manual retry. Never a stack trace, never a silent failure. */
function ActivityError({
  title,
  error,
  retryDisabled,
  onRetry,
}: {
  title: string;
  error: ErrorView;
  retryDisabled: boolean;
  onRetry: () => void;
}) {
  return (
    <div role="alert" className="activity-error">
      <p className="activity-error__title">{title}</p>
      <p className="activity-error__code">
        Code: <code>{error.code}</code>
      </p>
      <p className="activity-error__message">{error.message}</p>
      {error.requestId !== "" && (
        <p className="activity-error__request">
          Request ID: <code>{error.requestId}</code>
        </p>
      )}
      <button
        type="button"
        className="activity-error__retry"
        onClick={onRetry}
        disabled={retryDisabled}
      >
        Try again
      </button>
    </div>
  );
}

/** One timeline entry: who requested it, what was decided, what happened,
 * and why — plain language first, raw codes behind a disclosure. */
function ActivityEntry({
  event,
  now,
  latchedStopId,
}: {
  event: AuditEvent;
  now: number;
  /** The newest latched stop the loaded timeline names (see newestStopId). */
  latchedStopId: string | null;
}) {
  const [detailOpen, setDetailOpen] = useState(false);
  const eventType = eventTypeOf(event);
  const unitId = stringField(event, "unit_id");
  const occurredAt = stringField(event, "occurred_at");
  const age = ageText(occurredAt, now);
  const stale = isStale(occurredAt, now);
  const who = principalName(event);
  const decided = decidedLine(event, latchedStopId);
  const happened = happenedLine(event, latchedStopId);
  const codes = reasonCodesOf(event);
  const why = whyLine(codes);
  const detailId = `activity-detail-${sequenceOf(event, 0)}`;

  return (
    <li
      className={
        stale ? "activity-entry activity-entry--stale" : "activity-entry"
      }
    >
      <p className="activity-entry__head">
        <span className="activity-entry__kind">{headlineFor(eventType)}</span>
        {unitId !== undefined && (
          <span className="activity-entry__unit">{unitId}</span>
        )}
        {age !== "" && (
          <time className="activity-entry__age" dateTime={occurredAt}>
            {age}
          </time>
        )}
      </p>
      {who !== null && (
        <p className="activity-entry__who">
          Requested by <span className="activity-entry__principal">{who}</span>
        </p>
      )}
      {(decided !== null || happened !== null || why !== null) && (
        <dl className="activity-entry__facts">
          {decided !== null && (
            <div className="activity-entry__fact">
              <dt>What was decided</dt>
              <dd>{decided}</dd>
            </div>
          )}
          {happened !== null && (
            <div className="activity-entry__fact">
              <dt>What happened</dt>
              <dd>{happened}</dd>
            </div>
          )}
          {why !== null && (
            <div className="activity-entry__fact">
              <dt>Why</dt>
              <dd className="activity-entry__why">{why}</dd>
            </div>
          )}
        </dl>
      )}
      {codes.length > 0 && (
        <div className="activity-entry__technical">
          <button
            type="button"
            className="activity-entry__toggle"
            aria-expanded={detailOpen}
            aria-controls={detailId}
            onClick={() => setDetailOpen((open) => !open)}
          >
            Show technical detail
          </button>
          <details
            id={detailId}
            open={detailOpen}
            className="activity-entry__detail"
          >
            <div className="activity-entry__codes">
              <span className="activity-entry__codes-label">Reason codes: </span>
              {codes.map((code, index) => (
                <Fragment key={`${code}-${index}`}>
                  {index > 0 ? ", " : ""}
                  <code>{code}</code>
                </Fragment>
              ))}
            </div>
          </details>
        </div>
      )}
    </li>
  );
}
