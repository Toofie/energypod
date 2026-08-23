/**
 * Home's "Next scheduled action" card (DESIGN_SCHEDULES.md §6 W-C): the
 * schedules feature's one-glance answer on Home — what a published schedule
 * will do next, or what it is doing now.
 *
 * Feature-detected exactly like the solar-surplus tile: the card renders ONLY
 * when the snapshot carries a `schedule_state` projection. An absent
 * projection (today's backend; a deployment with no `schedule:` config block)
 * renders NOTHING — no heading, no empty state, no posture line.
 *
 * Honesty rules pinned here:
 *
 * - The RUNNING window names its entry and its end ("Night Charge is running
 *   now — ends in 45 min, at 05:59") and deliberately does NOT duplicate the
 *   request figures: a running window's intent is an ordinary SCHEDULE-sourced
 *   request that already appears through the existing intent paths (the
 *   "What is powering the home?" figures and Now's request cards). One
 *   sentence points there; the card never re-renders the watts as its own.
 * - `waiting_for_higher_priority` renders the honest waiting sentence (a
 *   schedule is the lowest-priority source; waiting is the design, not a
 *   fault).
 * - The NEXT occurrence carries its own figures from the projection's
 *   `next` (the pure next-occurrence object — the single implementation of
 *   every countdown).
 * - Countdowns are snapshot-derived and client-recomputed between snapshots
 *   (§5): a marker captured at adoption ticks once a second, and the live
 *   cadence read reconciles. Plain words, reduced-motion-safe by
 *   construction — a stable string, never an animation.
 * - The posture line states the commissioned windows; when the policy read is
 *   unavailable the line is omitted rather than guessed (DAY_DEFAULT is the
 *   config's default, not this console's assumption).
 */
import { useRef } from "react";
import type { JSX } from "react";
import {
  allowedWindowsText,
  countdownText,
  localTimeOfInstant,
  toSchedulePlan,
  toSchedulePolicy,
  wattsSummaryText,
  type SchedulePlan,
  type SchedulePolicy,
  type ScheduleState,
} from "../../app/schedule";

/** The policy + plan facts Home's one schedule read carries (no watts guessing). */
export interface ScheduleFacts {
  policy: SchedulePolicy;
  plan: SchedulePlan | null;
}

/** Narrow the GET body Home uses; null when unusable (the card omits its extras). */
export function toScheduleFacts(body: unknown): ScheduleFacts | null {
  if (body === null || typeof body !== "object") {
    return null;
  }
  const record = body as Record<string, unknown>;
  const policy = toSchedulePolicy(record.policy);
  if (policy === null) {
    return null;
  }
  return { policy, plan: toSchedulePlan(record.plan) };
}

/** A monotonic reading: countdown markers must never run backwards. */
function monotonicNowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

export interface NextScheduleCardProps {
  /** The snapshot's schedule projection; null hides the whole card. */
  schedule: ScheduleState | null;
  /** The policy + plan from Home's own schedule read; null = unavailable. */
  facts: ScheduleFacts | null;
  /** The parent's ticking clock (already running while any countdown shows). */
  nowMs: number;
}

/**
 * A remaining-seconds marker that runs down: re-captured whenever the wire
 * delivers a fresh figure or the window it belongs to changes, ticked locally
 * between snapshots. Null when the wire carries no countdown.
 */
function useCountdownMarker(
  remainingS: number | null,
  resetKey: string,
  nowMs: number,
): number | null {
  const markerRef = useRef<{ remainingS: number; atMs: number; resetKey: string } | null>(null);
  if (remainingS === null) {
    markerRef.current = null;
  } else if (
    markerRef.current === null ||
    markerRef.current.remainingS !== remainingS ||
    markerRef.current.resetKey !== resetKey
  ) {
    markerRef.current = { remainingS, atMs: monotonicNowMs(), resetKey };
  }
  const marker = markerRef.current;
  if (marker === null) {
    return null;
  }
  return Math.max(0, marker.remainingS - Math.max(0, (nowMs - marker.atMs) / 1000));
}

export function NextScheduleCard({ schedule, facts, nowMs }: NextScheduleCardProps): JSX.Element | null {
  // A countdown marker per live figure, keyed so a new window resets it.
  const endsIn = useCountdownMarker(
    schedule?.active === true ? schedule.endsInS : null,
    `running:${schedule?.entryId ?? ""}`,
    nowMs,
  );
  const startsIn = useCountdownMarker(
    schedule?.active === true ? null : (schedule?.next?.startsInS ?? null),
    `next:${schedule?.next?.entryId ?? ""}`,
    nowMs,
  );

  if (schedule === null) {
    // Feature detection: no schedule_state means the schedules feature is not
    // composed on this deployment — nothing renders at all.
    return null;
  }

  const waiting = schedule.reasonCodes.includes("waiting_for_higher_priority");
  const ended = !schedule.active && schedule.reasonCodes.includes("window_ended");
  const next = schedule.next;

  let status: JSX.Element;
  if (waiting && schedule.entryId !== null) {
    // A waiting window still HOLDS its intent (active is true while every one
    // of its units is claimed by a higher-priority live intent) — the honest
    // waiting sentence outranks the running one.
    status = (
      <p className="home-schedule-status" data-state="waiting">
        <strong>{schedule.entryId}</strong> is waiting — a manual request holds its batteries. It
        takes over the moment the other request ends.
      </p>
    );
  } else if (schedule.active && schedule.entryId !== null) {
    const endsText =
      endsIn === null
        ? "until its window ends"
        : `ends in ${countdownText(endsIn)}`;
    const endsAt = localTimeOfInstant(schedule.endsAt);
    status = (
      <p className="home-schedule-status" data-state="running">
        <strong>{schedule.entryId}</strong> is running now — {endsText}
        {endsAt === "" ? "" : ` (at ${endsAt})`}. Its command rides the normal request path — the
        same figures as any request, on the cards above and in Now.
      </p>
    );
  } else if (ended && schedule.entryId !== null) {
    status = (
      <p className="home-schedule-status" data-state="ended">
        <strong>{schedule.entryId}</strong>&apos;s window just ended — the next picture follows.
      </p>
    );
  } else if (next !== null) {
    const startsAtWord = next.startsInS === null ? "" : `, in ${countdownText(startsIn ?? 0)}`;
    status = (
      <p className="home-schedule-status" data-state="next">
        Next: <strong>{next.entryId}</strong> — {wattsSummaryText(next)} — starts {next.startLocal}
        {startsAtWord}.
      </p>
    );
  } else {
    const planLine =
      facts?.plan == null
        ? "No schedules yet — add one in Schedule."
        : facts.plan.entries.length === 0
          ? "No schedules yet — add one in Schedule."
          : "Nothing is coming up — every schedule is paused or past its date range.";
    status = (
      <p className="home-schedule-status" data-state="empty">
        {planLine}
      </p>
    );
  }

  const postureLine =
    facts === null
      ? null
      : facts.policy.posture === "yield"
        ? `Schedules run ${allowedWindowsText(facts.policy.allowedWindowsLocal)} local — day-only; the night window stays with the site's other applications.`
        : `Schedules run ${allowedWindowsText(facts.policy.allowedWindowsLocal)} local — night granted to the controller by config.`;

  return (
    <section className="home-card home-card--schedule" aria-label="Next scheduled action">
      <h2>Next scheduled action</h2>
      {status}
      {postureLine !== null && <p className="home-schedule-posture">{postureLine}</p>}
    </section>
  );
}
