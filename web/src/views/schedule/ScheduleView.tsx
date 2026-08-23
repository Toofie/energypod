/**
 * The Schedule view — the v1 whole-plan list editor (DESIGN_SCHEDULES.md §6
 * W-A/W-B, API_CONTRACTS.md "Schedule").
 *
 * The model, in one paragraph: a schedule is a published list of named,
 * weekly-recurring windows — "charge these batteries at these watts, from
 * 06:30 to 18:00, on these days". The console loads the plan, the operator
 * edits a LOCAL DRAFT (nothing lands anywhere), and "Publish changes" sends
 * the COMPLETE list with the version the editor loaded (`expected_version`);
 * the server's CAS refuses a stale publish with 409
 * `schedule_version_conflict` and the console's answer is "the plan changed
 * elsewhere — reload and re-apply", never a silent merge.
 *
 * Honesty rules pinned here:
 *
 * - The v1 picker offers charge and discharge windows with PER-BATTERY watts
 *   (the 2026-08-23 operator ruling — one watts field per selected battery,
 *   the Now dispatch form's pattern); 'hold to zero' (idle) windows and entry
 *   priorities stay wire-supported but out of the picker (advanced
 *   disclosure only). An idle entry that arrives on the wire is rendered as
 *   what it is and never silently flipped to a direction.
 * - The allowed-window guard renders on the form: the policy line plus a
 *   client-side mirror of the containment rule, so a night-time refusal
 *   happens BEFORE any request. The console cannot widen the policy — only a
 *   config revision can — and the copy says so.
 * - The night-posture dialog opens ONLY on the 409
 *   `night_posture_acknowledgement_required` refusal (the refusal IS the
 *   routing, the NET_BILLED pattern): the §8 assertion, a typed
 *   PARTITION_ACKNOWLEDGED, one resend with `night_posture`. It is never
 *   shown pre-emptively.
 * - Every refusal renders inline verbatim (code + message), with the
 *   server's per-entry errors mapped onto the offending rows by entry id.
 * - A 409 `schedule_not_commissioned` renders the honest not-commissioned
 *   state (the excess tile's own wording) — the nav keeps the view visible;
 *   a deployment that did not commission scheduling says so here rather than
 *   hiding the surface (the pinned decision; see DESIGN_SCHEDULES.md §6).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { isRecord } from "../../app/fleet";
import {
  allowedMinuteSet,
  allowedWindowsText,
  countdownText,
  daysSummaryText,
  isWindowAllowed,
  minutesOfHHMM,
  SCHEDULE_DAYS,
  toSchedulePlan,
  toSchedulePolicy,
  toScheduleReplacedEvent,
  toScheduleState,
  touchesNightMinutes,
  wattsSummaryText,
  type ScheduleAction,
  type ScheduleEntry,
  type SchedulePlan,
  type SchedulePolicy,
  type ScheduleState,
} from "../../app/schedule";
import { formatWatts } from "../../lib/format";
import "./schedule.css";

export interface ScheduleViewProps {
  client: ApiClient;
}

type Phase = "loading" | "ready" | "not-commissioned" | "error";

/** The per-battery bound the Now dispatch form pins; schedules mirror it. */
export const PER_BATTERY_WATTS_CAP_W = 2500;

/** The one-time night acknowledgement the contract pins (§3). */
export const PARTITION_ACKNOWLEDGED = "PARTITION_ACKNOWLEDGED";

const DAY_LABELS: Record<string, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

/** The stream loop's reconnect pause (a dropped line is seen retrying, never spun on). */
const STREAM_RECONNECT_MS = 1000;

// --- the draft -----------------------------------------------------------------

/**
 * One editable row. Watts stay STRINGS until validation (the operator's
 * half-typed "25" is field state, not a figure); the wire form is built only
 * at publish time.
 */
export interface DraftEntry {
  /** Local row identity (stable through name edits). */
  key: string;
  entryId: string;
  days: string[];
  startLocal: string;
  endLocal: string;
  action: ScheduleAction;
  wattForm: "per-battery" | "fleet";
  wattsByUnit: Record<string, string>;
  wattsScalar: string;
  unitIds: string[];
  effectiveFrom: string;
  effectiveUntil: string;
  priority: string;
  enabled: boolean;
  advancedOpen: boolean;
}

export interface ScheduleDraft {
  timezone: string;
  entries: DraftEntry[];
}

let rowCounter = 0;
function nextRowKey(): string {
  rowCounter += 1;
  return `schedule-row-${rowCounter}`;
}

function draftEntryFromWire(entry: ScheduleEntry): DraftEntry {
  const wattsByUnit: Record<string, string> = {};
  if (entry.wattsByUnit !== null) {
    for (const [unitId, watts] of Object.entries(entry.wattsByUnit)) {
      wattsByUnit[unitId] = String(watts);
    }
  }
  return {
    key: nextRowKey(),
    entryId: entry.entryId,
    days: [...entry.days],
    startLocal: entry.startLocal,
    endLocal: entry.endLocal,
    action: entry.action,
    wattForm: entry.wattsByUnit !== null ? "per-battery" : "fleet",
    wattsByUnit,
    wattsScalar: entry.watts === null ? "" : String(entry.watts),
    unitIds: [...entry.unitIds],
    effectiveFrom: entry.effectiveFrom ?? "",
    effectiveUntil: entry.effectiveUntil ?? "",
    priority: String(entry.priority),
    enabled: entry.enabled,
    advancedOpen: false,
  };
}

/** A fresh row's defaults: a day-legal charge window on every known battery. */
function newDraftEntry(unitIds: readonly string[]): DraftEntry {
  return {
    key: nextRowKey(),
    entryId: "",
    days: [...SCHEDULE_DAYS],
    startLocal: "07:00",
    endLocal: "19:00",
    action: "charge",
    wattForm: "per-battery",
    wattsByUnit: {},
    wattsScalar: "",
    unitIds: [...unitIds],
    effectiveFrom: "",
    effectiveUntil: "",
    priority: "0",
    enabled: true,
    advancedOpen: false,
  };
}

export function draftFromPlan(plan: SchedulePlan | null): ScheduleDraft {
  return {
    timezone: plan?.timezone ?? "Australia/Brisbane",
    entries: plan === null ? [] : plan.entries.map(draftEntryFromWire),
  };
}

/** The wire entry one draft row publishes (exactly one watt form, never both). */
export function draftEntryToWire(entry: DraftEntry): Record<string, unknown> {
  const priority = Number(entry.priority.trim());
  const wire: Record<string, unknown> = {
    entry_id: entry.entryId.trim(),
    days: SCHEDULE_DAYS.filter((day) => entry.days.includes(day)),
    start_local: entry.startLocal,
    end_local: entry.endLocal,
    action: entry.action,
    unit_ids: [...entry.unitIds],
    priority: Number.isFinite(priority) ? Math.trunc(priority) : 0,
    enabled: entry.enabled,
  };
  if (entry.action === "idle") {
    // Wire-supported only (the v1 picker never creates these): an idle entry
    // carries scalar watts 0 and no mapping, exactly the domain rule.
    wire.watts = 0;
  } else if (entry.wattForm === "per-battery") {
    const wattsByUnit: Record<string, number> = {};
    for (const unitId of entry.unitIds) {
      const watts = Number(entry.wattsByUnit[unitId] ?? "".trim());
      if (Number.isFinite(watts) && watts > 0) {
        wattsByUnit[unitId] = Math.trunc(watts);
      }
    }
    wire.watts_by_unit = wattsByUnit;
  } else {
    const watts = Number(entry.wattsScalar.trim());
    wire.watts = Number.isFinite(watts) ? Math.trunc(watts) : 0;
  }
  if (entry.effectiveFrom !== "") {
    wire.effective_from = entry.effectiveFrom;
  }
  if (entry.effectiveUntil !== "") {
    wire.effective_until = entry.effectiveUntil;
  }
  return wire;
}

export function draftToWireEntries(draft: ScheduleDraft): Record<string, unknown>[] {
  return draft.entries.map(draftEntryToWire);
}

/** Wire form of a stored entry, for the clean/dirty comparison. */
function wireEntryOf(entry: ScheduleEntry): Record<string, unknown> {
  const draft = draftEntryFromWire(entry);
  return draftEntryToWire({ ...draft, key: "" });
}

/** True when the draft is exactly the published plan (no unsent changes). */
export function draftIsClean(draft: ScheduleDraft, plan: SchedulePlan | null): boolean {
  if (plan === null) {
    return draft.entries.length === 0 && draft.timezone === "Australia/Brisbane";
  }
  if (draft.timezone !== plan.timezone || draft.entries.length !== plan.entries.length) {
    return false;
  }
  return draft.entries.every(
    (row, index) =>
      JSON.stringify(draftEntryToWire(row)) === JSON.stringify(wireEntryOf(plan.entries[index]!)),
  );
}

// --- validation ------------------------------------------------------------------

export interface EntryFieldErrors {
  name: string | null;
  days: string | null;
  times: string | null;
  batteries: string | null;
  watts: string | null;
  priority: string | null;
  dates: string | null;
}

/** Validate one row; null when the row is clean. Name uniqueness is plan-wide. */
export function validateEntry(entry: DraftEntry, siblings: readonly DraftEntry[]): EntryFieldErrors | null {
  const errors: EntryFieldErrors = {
    name: null,
    days: null,
    times: null,
    batteries: null,
    watts: null,
    priority: null,
    dates: null,
  };
  const name = entry.entryId.trim();
  if (name === "") {
    errors.name = "Enter a name for this schedule.";
  } else if (
    siblings.some((other) => other !== entry && other.entryId.trim().toLowerCase() === name.toLowerCase())
  ) {
    errors.name = `A schedule named “${name}” already exists — names must be unique.`;
  }
  if (entry.days.length === 0) {
    errors.days = "Select at least one day.";
  }
  const start = minutesOfHHMM(entry.startLocal);
  const end = minutesOfHHMM(entry.endLocal);
  if (start === null || end === null) {
    errors.times = "Enter the start and end times as HH:MM.";
  } else if (start === end) {
    errors.times = "The start and end times must differ — a window needs length.";
  }
  if (entry.unitIds.length === 0) {
    errors.batteries = "Select at least one battery.";
  }
  if (entry.action !== "idle") {
    if (entry.wattForm === "per-battery") {
      const missing = entry.unitIds.filter(
        (unitId) =>
          !Number.isFinite(Number(entry.wattsByUnit[unitId] ?? "")) ||
          Number(entry.wattsByUnit[unitId] ?? "") <= 0,
      );
      if (missing.length > 0) {
        errors.watts = `Enter positive watts for every selected battery${
          missing.length === 1 ? ` (${missing[0]})` : ""
        }.`;
      } else {
        const tooHigh = entry.unitIds.filter(
          (unitId) => Number(entry.wattsByUnit[unitId] ?? 0) > PER_BATTERY_WATTS_CAP_W,
        );
        if (tooHigh.length > 0) {
          errors.watts = `Watts per battery are too high: the bound is ${formatWatts(
            PER_BATTERY_WATTS_CAP_W,
          )} per battery.`;
        }
      }
    } else {
      const watts = Number(entry.wattsScalar.trim());
      if (!Number.isFinite(watts) || watts <= 0) {
        errors.watts = "Enter a positive number of watts (the fleet total).";
      }
    }
  }
  const priority = entry.priority.trim();
  if (priority !== "" && !/^-?\d+$/.test(priority)) {
    errors.priority = "Priority is a whole number (higher wins among overlapping windows).";
  }
  if (entry.effectiveFrom !== "" || entry.effectiveUntil !== "") {
    const from = entry.effectiveFrom;
    const until = entry.effectiveUntil;
    const dateShape = /^\d{4}-\d{2}-\d{2}$/;
    if ((from !== "" && !dateShape.test(from)) || (until !== "" && !dateShape.test(until))) {
      errors.dates = "Use the date format YYYY-MM-DD.";
    } else if (from !== "" && until !== "" && from > until) {
      errors.dates = "The effective end date must not come before the start date.";
    }
  }
  if (
    errors.name === null &&
    errors.days === null &&
    errors.times === null &&
    errors.batteries === null &&
    errors.watts === null &&
    errors.priority === null &&
    errors.dates === null
  ) {
    return null;
  }
  return errors;
}

/** The allowed-window guard's verdict for one row (the client-side mirror). */
export interface EntryGuard {
  outsideAllowed: boolean;
  /** Any night minute (outside DAY_DEFAULT), whatever the policy grants. */
  night: boolean;
}

/**
 * The guard, judged only while the row's times are valid input — the
 * containment rule applies to ENABLED entries on the server, so the publish
 * block keys on enabled rows; disabled rows still show the warning (the next
 * enable would be refused).
 */
export function entryGuard(entry: DraftEntry, policy: SchedulePolicy): EntryGuard | null {
  const allowed = allowedMinuteSet(policy.allowedWindowsLocal);
  const inside = isWindowAllowed(entry.startLocal, entry.endLocal, allowed);
  if (inside === null) {
    return null;
  }
  return {
    outsideAllowed: !inside,
    night: touchesNightMinutes(entry.startLocal, entry.endLocal) === true,
  };
}

// --- the GET view ----------------------------------------------------------------

export interface PublishedSchedule {
  plan: SchedulePlan | null;
  policy: SchedulePolicy;
  acknowledgedNightWindows: boolean;
  /** The GET's own next occurrence, rendered beside the editor. */
  nextActionSummary: string | null;
}

interface GetView {
  published: PublishedSchedule;
  next: ScheduleState["next"];
}

/** The one-line summary of a next occurrence (the editor's plan line). */
function nextActionSummaryOf(next: ScheduleState["next"]): string | null {
  if (next === null) {
    return null;
  }
  return `${next.entryId} — ${wattsSummaryText(next)} — starts ${next.startLocal} (in ${
    next.startsInS === null ? "…" : countdownText(next.startsInS)
  })`;
}

function toGetView(body: unknown): GetView | null {
  if (body === null || typeof body !== "object") {
    return null;
  }
  const record = body as Record<string, unknown>;
  const policy = toSchedulePolicy(record.policy);
  if (policy === null) {
    return null;
  }
  const next = toScheduleState({ next: record.next_action })?.next ?? null;
  return {
    published: {
      plan: toSchedulePlan(record.plan),
      policy,
      acknowledgedNightWindows: record.acknowledged_night_windows === true,
      nextActionSummary: nextActionSummaryOf(next),
    },
    next,
  };
}

// --- refusals ---------------------------------------------------------------------

/** A refusal envelope, kept verbatim for rendering. */
export interface ScheduleRefusal {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
  status: number;
}

function asRefusal(error: unknown): ScheduleRefusal {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message: error.message,
      details: error.details,
      status: error.status,
    };
  }
  return {
    code: "unexpected_error",
    message: "The publish did not complete.",
    details: null,
    status: 0,
  };
}

/**
 * The per-entry server errors a 422 carries, mapped by entry id. The service
 * nests them under `details.errors` (rest.py's replace_schedule handler,
 * landed with B4); `entries` is read alongside it defensively — the doc pins
 * the shape, the row mapping must survive either spelling.
 */
function serverEntryErrors(refusal: ScheduleRefusal): Record<string, string[]> {
  const mapped: Record<string, string[]> = {};
  const details = refusal.details;
  const rows = Array.isArray(details?.errors)
    ? (details?.errors as unknown[])
    : Array.isArray(details?.entries)
      ? (details?.entries as unknown[])
      : [];
  for (const row of rows) {
    if (row === null || typeof row !== "object") {
      continue;
    }
    const record = row as Record<string, unknown>;
    if (typeof record.entry_id !== "string") {
      // A plan-level error (entry_id null) has no row to sit on; the envelope
      // itself renders it verbatim.
      continue;
    }
    const message =
      typeof record.message === "string"
        ? record.message
        : typeof record.detail === "string"
          ? record.detail
          : "";
    const field = typeof record.field === "string" ? record.field : "";
    const text = message === "" ? "did not validate" : message;
    const line = field === "" ? text : `${field}: ${text}`;
    mapped[record.entry_id] = [...(mapped[record.entry_id] ?? []), line];
  }
  return mapped;
}

// --- the night-posture dialog (the NET_BILLED pattern, refusal-routed) -------------

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

function NightAcknowledgementDialog({
  refusal,
  pending,
  onCancel,
  onConfirm,
}: {
  refusal: ScheduleRefusal | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [typed, setTyped] = useState("");
  const titleId = "schedule-night-title";
  const inputId = "schedule-night-confirmation";
  const typedOk = typed.trim() === PARTITION_ACKNOWLEDGED;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const input = dialog.querySelector<HTMLElement>("#" + inputId);
    (input ?? dialog).focus();
  }, []);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const focusables = focusableIn(dialog);
    if (focusables.length === 0) {
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (first === undefined || last === undefined) {
      return;
    }
    const active = document.activeElement;
    const inside = active instanceof Node && dialog.contains(active);
    if (event.shiftKey) {
      if (active === first || !inside) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !inside) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="schedule-night-dialog"
      onKeyDown={handleKeyDown}
    >
      <h2 id={titleId}>Acknowledge the night window once</h2>
      <p>
        The controller&apos;s config grants this site a night window, and the schedule you are
        publishing uses it. Confirming records one durable fact, captured once and never asked
        again:
      </p>
      <blockquote className="schedule-night-assertion">
        “The external writer applications stand down for the granted window; the controller owns
        it.”
      </blockquote>
      <p>
        If an external writer does not stand down, the arm-time preflight latches and the console
        says so honestly — that is the partition being enforced, not the controller fighting.
      </p>
      <p className="schedule-night-typed">
        <label htmlFor={inputId}>Type {PARTITION_ACKNOWLEDGED} to publish</label>
        <input
          id={inputId}
          type="text"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          disabled={pending}
          onChange={(event) => setTyped(event.target.value)}
        />
      </p>
      {refusal !== null && (
        <div role="alert" className="schedule-refusal schedule-refusal--dialog">
          <p>
            <code>{refusal.code}</code> — <span>{refusal.message}</span>
          </p>
        </div>
      )}
      <div className="schedule-dialog-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button
          type="button"
          className="schedule-night-confirm"
          disabled={!typedOk || pending}
          onClick={onConfirm}
        >
          Publish with the acknowledgement
        </button>
      </div>
    </div>
  );
}

// --- the view ---------------------------------------------------------------------

export function ScheduleView({ client }: ScheduleViewProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [failure, setFailure] = useState<ScheduleRefusal | null>(null);
  const [published, setPublished] = useState<PublishedSchedule | null>(null);
  const [draft, setDraft] = useState<ScheduleDraft | null>(null);
  const [knownUnits, setKnownUnits] = useState<string[]>([]);
  const [publishing, setPublishing] = useState(false);
  const [publishRefusal, setPublishRefusal] = useState<ScheduleRefusal | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string[]>>({});
  const [nightDialog, setNightDialog] = useState<{ refusal: ScheduleRefusal } | null>(null);
  const [conflict, setConflict] = useState<ScheduleRefusal | null>(null);
  const [notice, setNotice] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);

  // The frames loop and the conflict reload need the CURRENT draft and base
  // synchronously (a re-render may not have flushed between frames).
  const draftRef = useRef<ScheduleDraft | null>(null);
  draftRef.current = draft;
  const publishedRef = useRef<PublishedSchedule | null>(null);
  publishedRef.current = published;

  /** Read the plan (and the fleet's battery ids) from the service. */
  const load = useCallback(async (): Promise<void> => {
    setPhase("loading");
    const [scheduleResult, snapshotResult] = await Promise.allSettled([
      client.getSchedule(),
      client.getSnapshot(),
    ]);
    if (scheduleResult.status === "rejected") {
      const refusal = asRefusal(scheduleResult.reason);
      if (refusal.code === "schedule_not_commissioned") {
        setFailure(refusal);
        setPhase("not-commissioned");
        return;
      }
      setFailure(refusal);
      setPhase("error");
      return;
    }
    const view = toGetView(scheduleResult.value);
    if (view === null) {
      setFailure({
        code: "unreadable_schedule",
        message: "The schedule response was not readable.",
        details: null,
        status: 0,
      });
      setPhase("error");
      return;
    }
    const units = new Set<string>(view.published.plan?.entries.flatMap((entry) => entry.unitIds) ?? []);
    if (snapshotResult.status === "fulfilled" && Array.isArray(snapshotResult.value?.units)) {
      for (const unit of snapshotResult.value.units) {
        if (typeof unit?.unit_id === "string" && unit.unit_id !== "") {
          units.add(unit.unit_id);
        }
      }
    }
    setKnownUnits([...units].sort());
    setPublished(view.published);
    setDraft(draftFromPlan(view.published.plan));
    setFailure(null);
    setPhase("ready");
  }, [client]);

  useEffect(() => {
    void load().catch(() => {
      setFailure({
        code: "unexpected_error",
        message: "The schedule could not be loaded.",
        details: null,
        status: 0,
      });
      setPhase("error");
    });
  }, [load, reloadNonce]);

  /** Adopt a fresh GET view as the published base (the draft is never merged). */
  const adoptPublished = useCallback((view: PublishedSchedule, keepDraft: boolean): void => {
    setPublished(view);
    setDraft((current) => {
      if (keepDraft && current !== null) {
        return current;
      }
      return draftFromPlan(view.plan);
    });
  }, []);

  /**
   * The conflict's "reload": re-read the plan and move the base under the
   * operator's draft, keeping the unsent changes exactly as they are (the
   * honest CAS outcome — re-apply by publishing again, never a merge).
   */
  const reloadKeepingDraft = useCallback(async (): Promise<void> => {
    const fresh = await client.getSchedule().catch(() => null);
    if (fresh === null) {
      return;
    }
    const view = toGetView(fresh);
    if (view === null) {
      return;
    }
    const currentDraft = draftRef.current;
    const clean =
      currentDraft !== null && draftIsClean(currentDraft, publishedRef.current?.plan ?? null);
    adoptPublished(view.published, !clean);
  }, [client, adoptPublished]);

  // The live bus: a publish anywhere (this console or another operator's)
  // re-reads the plan. A clean draft follows the new plan; a dirty draft stays
  // exactly as the operator left it and the base version moves under it — the
  // CAS is the arbiter, never a silent merge.
  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      while (!cancelled) {
        try {
          for await (const frame of client.openEvents()) {
            if (cancelled) {
              return;
            }
            if (frame.type === "resync_required") {
              break;
            }
            if (frame.type !== "schedule.replaced") {
              continue;
            }
            if (toScheduleReplacedEvent(frame.payload) === null) {
              continue;
            }
            const fresh = await client.getSchedule().catch(() => null);
            if (cancelled || fresh === null) {
              continue;
            }
            const view = toGetView(fresh);
            if (view === null) {
              continue;
            }
            const currentDraft = draftRef.current;
            const clean =
              currentDraft !== null &&
              draftIsClean(currentDraft, publishedRef.current?.plan ?? null);
            if (!clean && currentDraft !== null) {
              setNotice(
                `The published plan changed elsewhere (now v${
                  view.published.plan?.version ?? "?"
                }) — your unsent changes are still here; publishing uses the new version.`,
              );
            }
            adoptPublished(view.published, !clean);
          }
        } catch {
          // A failed stream is treated exactly like a dropped one: retry.
        }
        if (cancelled) {
          return;
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, STREAM_RECONNECT_MS);
        });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [client, adoptPublished]);

  // --- draft editing helpers -----------------------------------------------------

  const updateEntry = useCallback((key: string, patch: Partial<DraftEntry>): void => {
    setDraft((current) => {
      if (current === null) {
        return current;
      }
      return {
        ...current,
        entries: current.entries.map((row) => (row.key === key ? { ...row, ...patch } : row)),
      };
    });
  }, []);

  const addEntry = useCallback((): void => {
    setDraft((current) => {
      if (current === null) {
        return current;
      }
      return { ...current, entries: [...current.entries, newDraftEntry(knownUnits)] };
    });
  }, [knownUnits]);

  const removeEntry = useCallback((key: string): void => {
    setDraft((current) => {
      if (current === null) {
        return current;
      }
      return { ...current, entries: current.entries.filter((row) => row.key !== key) };
    });
  }, []);

  // --- publish --------------------------------------------------------------------

  const rowErrors = useMemo(() => {
    if (draft === null) {
      return new Map<string, EntryFieldErrors>();
    }
    const errors = new Map<string, EntryFieldErrors>();
    for (const row of draft.entries) {
      const rowError = validateEntry(row, draft.entries);
      if (rowError !== null) {
        errors.set(row.key, rowError);
      }
    }
    return errors;
  }, [draft]);

  const guards = useMemo(() => {
    if (draft === null || published === null) {
      return new Map<string, EntryGuard>();
    }
    const verdicts = new Map<string, EntryGuard>();
    for (const row of draft.entries) {
      const guard = entryGuard(row, published.policy);
      if (guard !== null) {
        verdicts.set(row.key, guard);
      }
    }
    return verdicts;
  }, [draft, published]);

  const clean = draft !== null && published !== null && draftIsClean(draft, published.plan);
  const blockingGuardRows = useMemo(() => {
    if (draft === null) {
      return [];
    }
    return draft.entries.filter(
      (row) => row.enabled && guards.get(row.key)?.outsideAllowed === true,
    );
  }, [draft, guards]);
  const anyRowErrors = rowErrors.size > 0;
  const canPublish = !publishing && !clean && !anyRowErrors && blockingGuardRows.length === 0;

  const publish = useCallback(
    (nightPosture?: string): void => {
      if (draft === null || published === null || publishing) {
        return;
      }
      setPublishRefusal(null);
      setServerErrors({});
      setConflict(null);
      setNotice("");
      // The client-side pre-check mirrors the server's containment rule: a
      // night-time refusal happens before any request (§6 W-A).
      if (anyRowErrors || blockingGuardRows.length > 0) {
        return;
      }
      const body: Record<string, unknown> = {
        expected_version: published.plan?.version ?? null,
        timezone: draft.timezone.trim() === "" ? "Australia/Brisbane" : draft.timezone.trim(),
        entries: draftToWireEntries(draft),
        ...(nightPosture === undefined ? {} : { night_posture: nightPosture }),
      };
      setPublishing(true);
      // A fresh idempotency key per attempt: the acknowledgement resend is a
      // DIFFERENT request (it carries night_posture), not a retry of the same
      // one, so it must never replay the refused attempt.
      client
        .putSchedule(body)
        .then((response) => {
          setPublishing(false);
          setNightDialog(null);
          const plan = toSchedulePlan(response.plan);
          if (plan === null) {
            setPublishRefusal({
              code: "unreadable_schedule",
              message: "The publish succeeded, but the stored plan it returned was not readable.",
              details: null,
              status: 0,
            });
            void load();
            return;
          }
          const next = toScheduleState({ next: response.next_action })?.next ?? null;
          setPublished({
            plan,
            policy: published.policy,
            acknowledgedNightWindows: response.acknowledged_night_windows === true,
            nextActionSummary: nextActionSummaryOf(next),
          });
          setDraft(draftFromPlan(plan));
          const diff = response.diff;
          const parts: string[] = [];
          if (isRecord(diff)) {
            for (const [key, verb] of [
              ["added", "added"],
              ["removed", "removed"],
              ["changed", "changed"],
            ] as const) {
              const names = Array.isArray(diff[key])
                ? (diff[key] as unknown[]).filter(
                    (name): name is string => typeof name === "string",
                  )
                : [];
              if (names.length > 0) {
                parts.push(`${verb} ${names.join(", ")}`);
              }
            }
          }
          setNotice(
            `Published — Schedule v${response.version ?? plan.version}${
              parts.length === 0 ? " — no entry changes" : `: ${parts.join(", ")}`
            }.`,
          );
        })
        .catch((error: unknown) => {
          setPublishing(false);
          const refusal = asRefusal(error);
          if (refusal.code === "night_posture_acknowledgement_required") {
            // The refusal IS the routing: the dialog opens only here.
            setNightDialog({ refusal });
            return;
          }
          if (refusal.code === "schedule_version_conflict") {
            setConflict(refusal);
            return;
          }
          setPublishRefusal(refusal);
          setServerErrors(serverEntryErrors(refusal));
        });
    },
    [
      draft,
      published,
      publishing,
      anyRowErrors,
      blockingGuardRows.length,
      client,
      load,
      adoptPublished,
    ],
  );

  const retry = useCallback((): void => {
    setFailure(null);
    setPublishRefusal(null);
    setNightDialog(null);
    setConflict(null);
    setNotice("");
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  // --- the honest non-ready states --------------------------------------------------

  if (phase === "loading") {
    return (
      <section className="schedule-view" aria-live="polite">
        <p role="status" className="schedule-loading">
          Loading the schedule…
        </p>
      </section>
    );
  }

  if (phase === "not-commissioned") {
    return (
      <section className="schedule-view">
        <div className="schedule-card schedule-card--absent">
          <h2>Scheduling is not commissioned</h2>
          <p>
            Schedules are not commissioned in this deployment&apos;s config — there is nothing to
            edit here. Commissioning means adding the <code>schedule:</code> block to the
            controller&apos;s config and restarting; the block composes the routes, the runner,
            and the snapshot projection in one step.
          </p>
          {failure !== null && (
            <p className="schedule-refusal-verbatim">
              <code>{failure.code}</code> — <span>{failure.message}</span>
            </p>
          )}
        </div>
      </section>
    );
  }

  if (phase === "error" || published === null || draft === null) {
    const refusal = failure ?? {
      code: "unexpected_error",
      message: "The schedule could not be loaded.",
      details: null,
      status: 0,
    };
    return (
      <section className="schedule-view">
        <div className="schedule-card schedule-card--error">
          <h2>The schedule could not load</h2>
          <p className="schedule-refusal-verbatim">
            <code>{refusal.code}</code> — <span>{refusal.message}</span>
          </p>
          <p className="schedule-error-note">
            Nothing about your schedules is shown, because this request did not succeed. You can
            retry now.
          </p>
          <button type="button" className="schedule-retry" onClick={retry}>
            Retry
          </button>
        </div>
      </section>
    );
  }

  // --- the editor --------------------------------------------------------------------

  const policy = published.policy;
  const windowsText = allowedWindowsText(policy.allowedWindowsLocal);
  const policyText =
    policy.posture === "yield"
      ? `Schedules may command ${windowsText} — day-only posture; the night window belongs to the site's other applications.`
      : `Schedules may command ${windowsText} — partition posture; the night window was granted to the controller by a config revision.`;

  const planLine =
    published.plan === null
      ? "No schedule published yet — the first publish creates the plan."
      : `Published plan v${published.plan.version} · times follow ${published.plan.timezone}.`;
  const nextLine =
    published.nextActionSummary === null
      ? "Nothing is coming up — every entry is paused or past its date range."
      : `Next: ${published.nextActionSummary}.`;

  return (
    <section className="schedule-view" aria-label="Schedules">
      <div className="schedule-card schedule-card--policy">
        <h2>The commissioned windows</h2>
        <p className="schedule-policy-line">{policyText}</p>
        <p className="schedule-plan-line">
          {planLine} {nextLine}
        </p>
        <p className="schedule-timezone-line">
          Timezone: <code>{draft.timezone === "" ? "—" : draft.timezone}</code>
          <label className="schedule-timezone-edit">
            <span className="schedule-visually-hidden">Plan timezone (IANA)</span>
            <input
              type="text"
              value={draft.timezone}
              aria-label="Plan timezone (IANA)"
              onChange={(event) => setDraft({ ...draft, timezone: event.target.value })}
            />
          </label>
        </p>
      </div>

      {notice !== "" && (
        <p role="status" className="schedule-notice">
          {notice}
        </p>
      )}
      {conflict !== null && (
        <div role="alert" className="schedule-conflict">
          <p>
            The plan changed elsewhere — reload and re-apply. Your unsent changes are still here;
            publishing again uses the fresh version.
          </p>
          <p className="schedule-refusal-verbatim">
            <code>{conflict.code}</code> — <span>{conflict.message}</span>
          </p>
          <button type="button" onClick={() => void reloadKeepingDraft()}>
            Reload the published plan
          </button>
        </div>
      )}
      {publishRefusal !== null && (
        <div role="alert" className="schedule-refusal">
          <p className="schedule-refusal-plain">{publishRefusalSentence(publishRefusal, draft)}</p>
          <p className="schedule-refusal-verbatim">
            <code>{publishRefusal.code}</code> — <span>{publishRefusal.message}</span>
          </p>
          {offendingLines(publishRefusal).map((line) => (
            <p key={line} className="schedule-refusal-offending">
              {line}
            </p>
          ))}
        </div>
      )}

      <ul className="schedule-entries" aria-label="Schedule entries">
        {draft.entries.map((row) => (
          <ScheduleEntryCard
            key={row.key}
            row={row}
            knownUnits={knownUnits}
            errors={rowErrors.get(row.key) ?? null}
            guard={guards.get(row.key) ?? null}
            serverLines={serverErrors[row.entryId.trim()] ?? []}
            posture={policy.posture}
            windowsText={windowsText}
            onChange={(patch) => updateEntry(row.key, patch)}
            onRemove={() => removeEntry(row.key)}
          />
        ))}
      </ul>

      <div className="schedule-actions">
        <button type="button" className="schedule-add" onClick={addEntry}>
          Add a schedule
        </button>
        <button
          type="button"
          className="schedule-publish"
          disabled={!canPublish}
          onClick={() => publish()}
        >
          {publishing ? "Publishing…" : "Publish changes"}
        </button>
        <p className="schedule-publish-state">
          {publishing
            ? "Publishing the whole list…"
            : anyRowErrors
              ? "Fix the highlighted entries before publishing."
              : blockingGuardRows.length > 0
                ? `Publishing is held: ${blockingGuardRows
                    .map((row) => row.entryId.trim() === "" ? "an unnamed schedule" : row.entryId.trim())
                    .join(", ")} ${
                    blockingGuardRows.length === 1 ? "falls" : "fall"
                  } outside the commissioned ${windowsText} windows.`
                : clean
                  ? "No unsent changes — the list above is the published plan."
                  : `Publishing sends the whole list (base version ${
                      published.plan?.version ?? "none"
                    }).`}
        </p>
      </div>

      {nightDialog !== null && (
        <NightAcknowledgementDialog
          refusal={nightDialog.refusal}
          pending={publishing}
          onCancel={() => {
            setNightDialog(null);
          }}
          onConfirm={() => publish(PARTITION_ACKNOWLEDGED)}
        />
      )}
    </section>
  );
}

// --- one entry card -------------------------------------------------------------

function ScheduleEntryCard({
  row,
  knownUnits,
  errors,
  guard,
  serverLines,
  posture,
  windowsText,
  onChange,
  onRemove,
}: {
  row: DraftEntry;
  knownUnits: readonly string[];
  errors: EntryFieldErrors | null;
  guard: EntryGuard | null;
  serverLines: string[];
  posture: "yield" | "partition";
  windowsText: string;
  onChange: (patch: Partial<DraftEntry>) => void;
  onRemove: () => void;
}): JSX.Element {
  const unitChoices = [...new Set([...knownUnits, ...row.unitIds])];
  const enteredWatts = perBatteryDraftMap(row) ?? {};
  const enteredCount = Object.keys(enteredWatts).length;
  const perBatteryTotal = Object.values(enteredWatts).reduce((total, watts) => total + watts, 0);
  const guardText =
    guard === null || guard.outsideAllowed === false
      ? guard !== null && guard.night
        ? row.enabled
          ? "This window runs at night — the first night publish needs the one-time acknowledgement."
          : "This paused window runs at night — enabling it needs the one-time acknowledgement on first publish."
        : null
      : `outside the commissioned ${windowsText} windows — publishing will be refused; night windows need the one-time acknowledgement`;

  const toggleDay = (day: string): void => {
    onChange({
      days: row.days.includes(day) ? row.days.filter((d) => d !== day) : [...row.days, day],
    });
  };
  const toggleUnit = (unitId: string): void => {
    onChange({
      unitIds: row.unitIds.includes(unitId)
        ? row.unitIds.filter((id) => id !== unitId)
        : [...row.unitIds, unitId],
    });
  };

  return (
    <li className="schedule-entry" data-enabled={row.enabled ? "true" : "false"}>
      <div className="schedule-entry-head">
        <label className="schedule-entry-name">
          <span>Name</span>
          <input
            type="text"
            value={row.entryId}
            onChange={(event) => onChange({ entryId: event.target.value })}
            placeholder="Night Charge"
          />
        </label>
        <button
          type="button"
          role="switch"
          aria-checked={row.enabled ? "true" : "false"}
          className={row.enabled ? "schedule-switch is-on" : "schedule-switch"}
          onClick={() => onChange({ enabled: !row.enabled })}
        >
          {row.enabled ? "Enabled" : "Paused"}
        </button>
        <button type="button" className="schedule-remove" onClick={onRemove}>
          Remove
        </button>
      </div>
      {errors?.name != null && <FieldError text={errors.name} />}
      <p className="schedule-entry-summary">
        {row.entryId.trim() === "" ? "Unnamed schedule" : row.entryId.trim()} ·{" "}
        {daysSummaryText(row.days)} · {row.startLocal || "—"} to {row.endLocal || "—"} ·{" "}
        {directionLabel(row.action)} ·{" "}
        {wattsSummaryText({
          action: row.action,
          watts: row.wattsScalar.trim() === "" ? null : Number(row.wattsScalar),
          wattsByUnit: perBatteryDraftMap(row),
        })}
      </p>

      <fieldset className="schedule-entry-days">
        <legend>Days</legend>
        {SCHEDULE_DAYS.map((day) => (
          <label key={day} className="schedule-day">
            <input
              type="checkbox"
              checked={row.days.includes(day)}
              onChange={() => toggleDay(day)}
            />{" "}
            {DAY_LABELS[day]}
          </label>
        ))}
      </fieldset>
      {errors?.days != null && <FieldError text={errors.days} />}

      <div className="schedule-entry-times">
        <label>
          Starts
          <input
            type="time"
            value={row.startLocal}
            onChange={(event) => onChange({ startLocal: event.target.value })}
          />
        </label>
        <label>
          Ends
          <input
            type="time"
            value={row.endLocal}
            onChange={(event) => onChange({ endLocal: event.target.value })}
          />
        </label>
      </div>
      {errors?.times != null && <FieldError text={errors.times} />}
      <p className="schedule-time-hint">
        {row.startLocal !== "" && row.endLocal !== "" && row.startLocal > row.endLocal
          ? "This window crosses midnight — it is one window, not two."
          : "A window may cross midnight (22:30 to 06:00 is one window)."}
      </p>

      <fieldset className="schedule-entry-direction">
        <legend>Direction</legend>
        {(["charge", "discharge"] as const).map((action) => (
          <label key={action} className="schedule-direction">
            <input
              type="radio"
              name={`${row.key}-direction`}
              checked={row.action === action}
              onChange={() => onChange({ action })}
            />{" "}
            {directionLabel(action)}
          </label>
        ))}
        {row.action === "idle" && (
          <p className="schedule-idle-note">
            This entry holds to zero (idle) — the v1 picker does not create idle windows; picking a
            direction here makes it a charge or discharge window.
          </p>
        )}
      </fieldset>

      <fieldset className="schedule-entry-units">
        <legend>Batteries</legend>
        {unitChoices.length === 0 ? (
          <p className="schedule-no-units">No batteries are known to the console yet.</p>
        ) : (
          unitChoices.map((unitId) => (
            <label key={unitId} className="schedule-unit">
              <input
                type="checkbox"
                checked={row.unitIds.includes(unitId)}
                onChange={() => toggleUnit(unitId)}
              />{" "}
              {unitId}
              {knownUnits.includes(unitId) ? "" : " (not currently in the fleet)"}
            </label>
          ))
        )}
      </fieldset>
      {errors?.batteries != null && <FieldError text={errors.batteries} />}

      {row.action !== "idle" && (
        <div className="schedule-entry-watts">
          {row.wattForm === "per-battery" ? (
            <>
              <div className="schedule-watts-head">
                <span>Watts per battery</span>
                <label className="schedule-same-for-all">
                  Same for all
                  <input
                    type="number"
                    min={1}
                    aria-label="Watts to apply to every selected battery"
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                      }
                    }}
                    onChange={(event) => {
                      const value = event.target.value;
                      if (value.trim() === "") {
                        return;
                      }
                      const watts = Number(value);
                      if (!Number.isFinite(watts) || watts <= 0) {
                        return;
                      }
                      const filled: Record<string, string> = { ...row.wattsByUnit };
                      for (const unitId of row.unitIds) {
                        filled[unitId] = String(Math.trunc(watts));
                      }
                      onChange({ wattsByUnit: filled });
                    }}
                  />
                </label>
              </div>
              <ul className="schedule-watts-units">
                {row.unitIds.map((unitId) => (
                  <li key={unitId}>
                    <label>
                      {unitId}
                      <input
                        type="number"
                        min={1}
                        value={row.wattsByUnit[unitId] ?? ""}
                        aria-label={`Watts for ${unitId}`}
                        onChange={(event) =>
                          onChange({
                            wattsByUnit: { ...row.wattsByUnit, [unitId]: event.target.value },
                          })
                        }
                      />
                    </label>
                  </li>
                ))}
              </ul>
              <p className="schedule-watts-total">
                {row.unitIds.length === 0
                  ? "Select batteries to set their watts."
                  : enteredCount === row.unitIds.length && enteredCount > 0
                    ? `${row.unitIds
                        .map((unitId) => `${unitId} ${formatWatts(enteredWatts[unitId] ?? 0)}`)
                        .join(" + ")} = ${formatWatts(perBatteryTotal)} in total`
                    : enteredCount > 0
                      ? `${enteredCount} of ${row.unitIds.length} batteries set — fill the rest to see the total.`
                      : "Enter the watts for each selected battery to see the total."}
              </p>
            </>
          ) : (
            <>
              <label className="schedule-watts-scalar">
                Fleet-total watts
                <input
                  type="number"
                  min={1}
                  value={row.wattsScalar}
                  onChange={(event) => onChange({ wattsScalar: event.target.value })}
                />
              </label>
              <p className="schedule-watts-total">
                One total the allocator splits across the selected batteries — the per-battery form
                is the house default.
              </p>
            </>
          )}
          {errors?.watts != null && <FieldError text={errors.watts} />}
        </div>
      )}

      {guardText !== null && (
        <p
          className={`schedule-guard${
            guard?.outsideAllowed === true && row.enabled ? " schedule-guard--blocking" : ""
          }`}
        >
          {guardText}
          {guard?.outsideAllowed === true
            ? ` The console cannot widen the policy — only a config revision can (${posture} posture).`
            : ""}
        </p>
      )}
      {serverLines.map((line) => (
        <FieldError key={line} text={`Server: ${line}`} />
      ))}

      <div className="schedule-advanced">
        <button
          type="button"
          className="schedule-advanced-toggle"
          aria-expanded={row.advancedOpen ? "true" : "false"}
          onClick={() => onChange({ advancedOpen: !row.advancedOpen })}
        >
          {row.advancedOpen ? "Hide advanced" : "Advanced"}
        </button>
        {row.advancedOpen && (
          <div className="schedule-advanced-body">
            <fieldset>
              <legend>Watt form</legend>
              <label className="schedule-direction">
                <input
                  type="radio"
                  name={`${row.key}-wattform`}
                  checked={row.wattForm === "per-battery"}
                  onChange={() => onChange({ wattForm: "per-battery" })}
                />{" "}
                Per battery (default)
              </label>
              <label className="schedule-direction">
                <input
                  type="radio"
                  name={`${row.key}-wattform`}
                  checked={row.wattForm === "fleet"}
                  onChange={() => onChange({ wattForm: "fleet" })}
                />{" "}
                Fleet total (scalar)
              </label>
            </fieldset>
            <label className="schedule-advanced-field">
              Priority (advanced)
              <input
                type="text"
                inputMode="numeric"
                value={row.priority}
                onChange={(event) => onChange({ priority: event.target.value })}
              />
            </label>
            {errors?.priority != null && <FieldError text={errors.priority} />}
            <div className="schedule-entry-dates">
              <label className="schedule-advanced-field">
                Effective from (optional)
                <input
                  type="date"
                  value={row.effectiveFrom}
                  onChange={(event) => onChange({ effectiveFrom: event.target.value })}
                />
              </label>
              <label className="schedule-advanced-field">
                Effective until (optional)
                <input
                  type="date"
                  value={row.effectiveUntil}
                  onChange={(event) => onChange({ effectiveUntil: event.target.value })}
                />
              </label>
            </div>
            {errors?.dates != null && <FieldError text={errors.dates} />}
          </div>
        )}
      </div>
    </li>
  );
}

function FieldError({ text }: { text: string }): JSX.Element {
  return (
    <p role="alert" className="schedule-field-error">
      {text}
    </p>
  );
}

function directionLabel(action: ScheduleAction): string {
  if (action === "charge") {
    return "Charge";
  }
  if (action === "discharge") {
    return "Discharge";
  }
  return "Hold to zero";
}

/**
 * The per-battery figures a draft row's summary renders: only the values the
 * operator has actually entered (positive, finite) — an empty field never
 * renders as a 0 W figure.
 */
function perBatteryDraftMap(row: DraftEntry): Record<string, number> | null {
  if (row.wattForm !== "per-battery") {
    return null;
  }
  const map: Record<string, number> = {};
  for (const unitId of row.unitIds) {
    const watts = Number(row.wattsByUnit[unitId] ?? "");
    if (Number.isFinite(watts) && watts > 0) {
      map[unitId] = watts;
    }
  }
  return Object.keys(map).length > 0 ? map : null;
}

/** The plain sentence for a refused publish; the envelope renders beside it. */
function publishRefusalSentence(refusal: ScheduleRefusal, draft: ScheduleDraft | null): string {
  if (refusal.code === "schedule_window_not_allowed") {
    return "The commissioned windows refuse part of this plan — trim the offending entries to the allowed windows, or make the partition choice in config (stand the external writers down, widen the policy, restart), then acknowledge once.";
  }
  if (refusal.code === "validation_error") {
    return "The service refused the plan — the highlighted entries carry the reasons.";
  }
  if (refusal.code === "schedule_not_commissioned") {
    return "Scheduling is not commissioned in this deployment's config.";
  }
  if (draft !== null && refusal.code === "network_error") {
    return "The controller could not be reached — nothing was published; you can publish again.";
  }
  return "The publish was refused — nothing was published.";
}

/** The offending entries a window refusal names, one line each. */
function offendingLines(refusal: ScheduleRefusal): string[] {
  const details = refusal.details;
  const rows = Array.isArray(details?.offending) ? (details?.offending as unknown[]) : [];
  const lines: string[] = [];
  for (const row of rows) {
    if (row === null || typeof row !== "object") {
      continue;
    }
    const record = row as Record<string, unknown>;
    if (typeof record.entry_id !== "string") {
      continue;
    }
    const start = typeof record.start_local === "string" ? record.start_local : "?";
    const end = typeof record.end_local === "string" ? record.end_local : "?";
    lines.push(`${record.entry_id} (${start}–${end}) falls outside the allowed windows.`);
  }
  return lines;
}
