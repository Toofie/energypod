/**
 * The shell's live data plane: REST snapshot + health, the single event-stream
 * connection, the resync protocol, and loss-driven reconnection.
 *
 * Wire behavior (UI_CONTRACTS.md "Data sources", rest.py, service.py):
 * - `openEvents(afterSequence?)` yields the authoritative `snapshot` frame
 *   first, then ordered events. A `resync_required` frame ends iteration; the
 *   shell then refetches the snapshot and reconnects with the recovery cursor
 *   (the marker's `snapshot_sequence` when present, else the last seen one).
 * - The snapshot frame arrives exactly once per connection, and the bus
 *   vocabulary carries no lifecycle event for active/inhibited/stopping, so a
 *   healthy session never sees a fresh picture unless the shell asks for one.
 *   The shell therefore refetches the snapshot — coalesced, so a burst of
 *   frames costs one read — on every frame that changes authority:
 *   emergency_stop.latched, authorization.revoked, inhibit.acknowledged,
 *   emergency_stop.acknowledged, unit.armed, unit.disarmed, and the kernel's
 *   per-tick authorizing control_decision audit summaries (the only per-cycle
 *   signal that the requested/allowed figures moved). Every refresh that
 *   lands is republished to the views through the plane as a snapshot frame,
 *   so request panels live-update on every view instead of freezing at the
 *   connect-time picture.
 * - A lost stream marks the event-stream fact down, keeps the last known data,
 *   announces assertively when control was active, and retries automatically
 *   carrying the last seen sequence as the cursor — never a replay from zero —
 *   on a bounded, widening schedule: an unreachable service is reported, not
 *   hammered, and the stream's own error envelopes surface instead of being
 *   swallowed.
 * - A 401 anywhere is the single "session over" signal: it bubbles to the
 *   shell through `onUnauthorized` with the exact error envelope.
 *
 * Self-diagnosis (the 2026-08 background-tab and restart incidents): the shell
 * tracks when the last frame of any kind arrived, so a connection that claims
 * live but has gone quiet is reported STALE (see STALE_AFTER_MS) instead of
 * masquerading as current; a backgrounded tab whose timers the browser
 * throttled re-checks the stream the moment it becomes visible or focused
 * again (re-subscribing with the last sequence as the resume cursor when the
 * data is quiet or the connection is down); and a reconnection that had to
 * resume surfaces as a calm controller-restart notice, because the operator
 * could not otherwise tell a deploy restart from a stall.
 *
 * All reads go through the SharedDataPlane so mounted views share them.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { ApiClientError, isUnauthorizedError } from "../api/client";
import type { ApiClientError as ApiClientErrorType, Health, StreamEvent } from "../api/client";
import { formatWatts } from "../lib/format";
import { isRecord, normalizeSnapshot, type FleetSnapshot, type Lifecycle } from "./fleet";
import type { RealStream, SharedDataPlane } from "./SharedDataPlane";
import { useUnitIntentFigures } from "./useUnitIntentFigures";
import type { DirectionsByUnit, WattsByUnit } from "./fleet";

export type StreamStatus = "connecting" | "live" | "down";

/**
 * The one-glance connection health the shell badge shows. LIVE: connected and
 * frames flowing. STALE: the connection claims live but no frame has arrived
 * for the staleness bound (the frozen-tab signature). RECONNECTING: the
 * connection dropped and is retrying with backoff. OFFLINE: the service
 * itself cannot be reached.
 */
export type ConnectionHealth = "live" | "stale" | "reconnecting" | "offline";

/**
 * No event frame for this long while the connection claims live means the
 * data on screen may no longer be current. The healthy cadence is one
 * observation per ~1.5-2 s per pod, so 10 s is ~5 missed cycles — far past a
 * slow frame, well before an operator would misread a climbing age as a
 * healthy sawtooth.
 */
export const STALE_AFTER_MS = 10_000;

/** The calm notice a resume-reconnect surfaces (a controller restart). */
export const CONTROLLER_RESTART_NOTICE =
  "Connection restored after controller restart — the fleet picture is current again.";

export interface RefusalEnvelope {
  code: string;
  message: string;
}

export interface ConsoleData {
  snapshot: FleetSnapshot | null;
  health: Health | null;
  /**
   * The live request's per-unit figures — the shared tracker's maps
   * (web/src/app/useUnitIntentFigures.ts), fed by the shell's one stream and
   * seeded by the snapshot's `intent` block when the backend sends one. The
   * banner (and any shell-level per-battery claim) derives "Limited" only
   * from a battery's OWN target in these maps, never from the snapshot's
   * repeated fleet total.
   */
  unitFigures: {
    requestedByUnit: WattsByUnit | null;
    authorizedByUnit: WattsByUnit | null;
    directionsByUnit: DirectionsByUnit | null;
  };
  /** null = still checking; false = the API could not be reached. */
  apiReachable: boolean | null;
  snapshotError: RefusalEnvelope | null;
  streamStatus: StreamStatus;
  /** The last envelope the event stream itself failed with, if any. */
  streamError: RefusalEnvelope | null;
  /** True once the automatic reconnect budget is spent; a manual retry remains. */
  streamExhausted: boolean;
  /** The one-glance connection health (see ConnectionHealth). */
  connection: ConnectionHealth;
  /** Seconds since the last event frame landed; null before the first one. */
  secondsSinceUpdate: number | null;
  /**
   * A calm, non-blocking notice that the connection had to resume — the
   * operator-visible trace of a controller restart. Cleared by the next loss.
   */
  restartNotice: string | null;
  polite: string[];
  assertive: string[];
}

interface State {
  snapshot: FleetSnapshot | null;
  health: Health | null;
  apiReachable: boolean | null;
  snapshotError: RefusalEnvelope | null;
  streamStatus: StreamStatus;
  streamError: RefusalEnvelope | null;
  streamExhausted: boolean;
  lastEventAtMs: number | null;
  restartNotice: string | null;
  polite: string[];
  assertive: string[];
}

type Action =
  | { type: "reset" }
  | { type: "snapshot"; snapshot: FleetSnapshot }
  | { type: "snapshot-rest"; snapshot: FleetSnapshot }
  | { type: "snapshot-failed"; refusal: RefusalEnvelope }
  | { type: "health"; health: Health }
  | { type: "health-failed" }
  | { type: "units-patched"; lifecycles: Record<string, Lifecycle> }
  | { type: "units-inhibited"; ids: string[] }
  | { type: "units-demoted"; ids: string[] }
  | { type: "stream"; status: StreamStatus }
  | { type: "stream-error"; refusal: RefusalEnvelope }
  | { type: "stream-exhausted" }
  | { type: "frame-received"; at: number }
  | { type: "restart-notice" }
  | { type: "clear-restart-notice" }
  | { type: "stop-released"; stopId: string }
  | { type: "polite"; text: string }
  | { type: "assertive"; text: string }
  | { type: "clear-assertive" };

const INITIAL: State = {
  snapshot: null,
  health: null,
  apiReachable: null,
  snapshotError: null,
  streamStatus: "connecting",
  streamError: null,
  streamExhausted: false,
  lastEventAtMs: null,
  restartNotice: null,
  polite: [],
  assertive: [],
};

/**
 * Derive the one-glance health from the stream facts. Priority: a stream that
 * is not live is reconnecting, or offline when the service itself cannot be
 * reached — the health poll failing is direct evidence, and a network-level
 * stream failure counts only while the health poll has not just answered OK
 * (a dropped socket with a healthy REST probe is a lost connection, not an
 * unreachable service); a live stream is stale once its last frame is older
 * than the bound; otherwise live.
 */
export function connectionHealth(
  state: Pick<
    State,
    "streamStatus" | "apiReachable" | "streamError" | "lastEventAtMs"
  >,
  nowMs: number,
  staleAfterMs: number = STALE_AFTER_MS,
): ConnectionHealth {
  if (state.streamStatus !== "live") {
    const networkLevelFailure = state.streamError?.code === "network_error";
    const unreachable =
      state.apiReachable === false || (networkLevelFailure && state.apiReachable !== true);
    return unreachable ? "offline" : "reconnecting";
  }
  if (state.lastEventAtMs !== null && nowMs - state.lastEventAtMs > staleAfterMs) {
    return "stale";
  }
  return "live";
}

function patchUnits(
  state: State,
  lifecycles: Record<string, Lifecycle>,
): FleetSnapshot | null {
  if (state.snapshot === null) {
    return null;
  }
  return {
    ...state.snapshot,
    units: state.snapshot.units.map((unit) =>
      Object.prototype.hasOwnProperty.call(lifecycles, unit.unitId)
        ? { ...unit, lifecycle: lifecycles[unit.unitId]! }
        : unit,
    ),
  };
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "reset":
      return { ...INITIAL };
    case "snapshot":
      // A snapshot frame is the service's authoritative picture for this
      // connection, so it always wins — including after a service restart
      // that renumbers the sequence space from zero.
      return { ...state, snapshot: action.snapshot, snapshotError: null };
    case "snapshot-rest": {
      // A REST read is a refresh of the same timeline: never let an older
      // response overwrite newer frames already applied.
      if (state.snapshot !== null && action.snapshot.sequence < state.snapshot.sequence) {
        return state;
      }
      return { ...state, snapshot: action.snapshot, snapshotError: null };
    }
    case "snapshot-failed":
      return { ...state, snapshotError: action.refusal };
    case "health":
      return { ...state, health: action.health, apiReachable: true };
    case "health-failed":
      return { ...state, apiReachable: false };
    case "units-patched": {
      const snapshot = patchUnits(state, action.lifecycles);
      return snapshot === null ? state : { ...state, snapshot };
    }
    case "units-inhibited": {
      // A latched stop or inhibit is the most conservative state there is; it
      // is applied unconditionally so a stopped fleet is never still presented
      // as armed or active while the refetch is in flight.
      const lifecycles: Record<string, Lifecycle> = {};
      for (const id of action.ids) {
        lifecycles[id] = "inhibited";
      }
      const snapshot = patchUnits(state, lifecycles);
      return snapshot === null ? state : { ...state, snapshot };
    }
    case "units-demoted": {
      // A routine revocation ends active authority (the actor demotes ACTIVE
      // to ARMED_IDLE before revoking); it must never leave "active" on screen.
      const demote: Record<string, Lifecycle> = {};
      for (const unit of state.snapshot?.units ?? []) {
        if (unit.lifecycle === "active" && action.ids.includes(unit.unitId)) {
          demote[unit.unitId] = "armed_idle";
        }
      }
      const snapshot = patchUnits(state, demote);
      return snapshot === null ? state : { ...state, snapshot };
    }
    case "stream":
      return action.status === "live"
        ? {
            ...state,
            streamStatus: action.status,
            streamError: null,
            streamExhausted: false,
          }
        : { ...state, streamStatus: action.status };
    case "stream-error":
      return { ...state, streamError: action.refusal };
    case "stream-exhausted":
      return { ...state, streamStatus: "down", streamExhausted: true };
    case "frame-received":
      // Every frame of any kind is liveness evidence: the staleness clock
      // restarts here, not at the last snapshot.
      return { ...state, lastEventAtMs: action.at };
    case "restart-notice":
      return { ...state, restartNotice: CONTROLLER_RESTART_NOTICE };
    case "clear-restart-notice":
      return state.restartNotice === null ? state : { ...state, restartNotice: null };
    case "stop-released": {
      // The acknowledgement came back 200: the latch banner may come down NOW,
      // optimistically — the next snapshot (whose active_stops no longer name
      // this stop) is the confirm. A snapshot that still carries the stop
      // simply brings the banner back, which is the honest answer.
      if (state.snapshot === null) {
        return state;
      }
      const remaining = state.snapshot.activeStops.filter(
        (stop) => stop.stopId !== action.stopId,
      );
      if (remaining.length === state.snapshot.activeStops.length) {
        return state;
      }
      return {
        ...state,
        snapshot: { ...state.snapshot, activeStops: remaining },
        polite: [
          ...state.polite,
          `Emergency stop ${action.stopId} acknowledged — the fleet picture is updating.`,
        ].slice(-5),
      };
    }
    case "polite":
      return { ...state, polite: [...state.polite, action.text].slice(-5) };
    case "assertive":
      return { ...state, assertive: [action.text] };
    case "clear-assertive":
      // An acknowledged latch must not linger in the assertive region as a
      // ghost of a state that no longer exists.
      return state.assertive.length === 0 ? state : { ...state, assertive: [] };
  }
}

function asRefusal(error: unknown): RefusalEnvelope {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message };
  }
  return { code: "unexpected_error", message: "Something unexpected went wrong." };
}

function payloadOf(frame: StreamEvent): Record<string, unknown> {
  return isRecord(frame.payload) ? frame.payload : {};
}

interface UnitOutcome {
  unitId: string;
  applied: boolean;
  reason: string;
}

function outcomeUnits(payload: Record<string, unknown>): UnitOutcome[] {
  // unit.armed / unit.disarmed carry `{units: [{unit_id, status, reason}]}` —
  // the facade publishes refused rows too, and a refused unit must never be
  // patched into the state the operator asked for.
  const units = Array.isArray(payload.units) ? payload.units : [];
  const outcomes: UnitOutcome[] = [];
  for (const entry of units) {
    if (!isRecord(entry) || typeof entry.unit_id !== "string") {
      continue;
    }
    outcomes.push({
      unitId: entry.unit_id,
      applied: entry.status === "armed" || entry.status === "disarmed",
      reason: typeof entry.reason === "string" ? entry.reason : "",
    });
  }
  return outcomes;
}

function directUnitIds(payload: Record<string, unknown>): string[] {
  // emergency_stop.latched / authorization.revoked carry `{unit_ids: [...]}`.
  const ids = Array.isArray(payload.unit_ids) ? payload.unit_ids : [];
  return ids.filter((id): id is string => typeof id === "string");
}

function reasonOf(payload: Record<string, unknown>): string {
  return typeof payload.reason === "string" ? payload.reason : "";
}

function nameUnits(ids: string[]): string {
  return ids.length === 0 ? "the fleet" : ids.join(", ");
}

/**
 * Revocation reasons that mean a latched inhibit. Only the actor's latched
 * paths publish these (actor.py `_inhibit_owned(..., InhibitCause.LATCHED)`);
 * every other reason riding an `authorization.revoked` frame — intent expiry,
 * disarm, a generation fence, shutdown — is a routine end of authority and
 * must never be announced as a latch.
 */
const LATCHED_INHIBIT_REASONS: readonly string[] = ["blocking_fault_active", "identity_mismatch"];

/**
 * Apply one wire frame to the shell's own state.
 *
 * Returns true when the frame changes authority, meaning the snapshot must be
 * refetched: the bus has no lifecycle event for active/inhibited/stopping, so
 * without a refetch the banner, the stop control, and the connection facts
 * would freeze on the connect-time picture for the whole session.
 *
 * `audit.appended` frames of an authorizing kind also count: the kernel audits
 * a control_decision on every tick it grants or clamps power (control_kernel),
 * and those summaries are the only per-cycle bus signal that the
 * requested/allowed figures moved — without them every view's request panel
 * freezes at the connect-time snapshot for the whole session (the snapshot
 * frame arrives exactly once per connection and no bus frame carries watt
 * figures). Idle refusals (no_setpoints and friends) do not trigger a read.
 */
function applyEventFrame(frame: StreamEvent, dispatch: (action: Action) => void): boolean {
  switch (frame.type) {
    case "snapshot": {
      dispatch({ type: "snapshot", snapshot: normalizeSnapshot(frame.data) });
      dispatch({ type: "stream", status: "live" });
      dispatch({ type: "polite", text: "Fleet picture loaded." });
      return false;
    }
    case "unit.armed":
    case "unit.disarmed": {
      const armed = frame.type === "unit.armed";
      const outcomes = outcomeUnits(payloadOf(frame));
      if (outcomes.length === 0) {
        return false;
      }
      const lifecycles: Record<string, Lifecycle> = {};
      const applied: string[] = [];
      const refused: string[] = [];
      for (const outcome of outcomes) {
        if (outcome.applied) {
          applied.push(outcome.unitId);
          lifecycles[outcome.unitId] = armed ? "armed_idle" : "disarmed";
        } else {
          refused.push(outcome.unitId);
        }
      }
      if (applied.length > 0) {
        dispatch({ type: "units-patched", lifecycles });
        dispatch({
          type: "polite",
          text: `${nameUnits(applied)} ${applied.length > 1 ? "are" : "is"} now ${
            armed ? "armed" : "disarmed"
          }.`,
        });
      }
      if (refused.length > 0) {
        const reason = outcomes.find((outcome) => !outcome.applied)?.reason ?? "";
        dispatch({
          type: "polite",
          text: `${nameUnits(refused)} ${refused.length > 1 ? "were" : "was"} not ${
            armed ? "armed" : "disarmed"
          }${reason === "" ? "" : ` — ${reason}`}.`,
        });
      }
      return true;
    }
    case "emergency_stop.latched": {
      const payload = payloadOf(frame);
      const ids = directUnitIds(payload);
      const reason = reasonOf(payload);
      dispatch({ type: "units-inhibited", ids });
      dispatch({
        type: "assertive",
        text: `Emergency stop latched on ${nameUnits(ids)}${
          reason === "" ? "" : ` — ${reason}`
        }.`,
      });
      return true;
    }
    case "authorization.revoked": {
      const payload = payloadOf(frame);
      const ids = directUnitIds(payload);
      const reason = reasonOf(payload);
      if (LATCHED_INHIBIT_REASONS.includes(reason)) {
        // The latched-inhibit path surfaces on the bus as a revoked
        // authorization whose reason is the latched cause.
        dispatch({ type: "units-inhibited", ids });
        dispatch({
          type: "assertive",
          text: `Inhibit latched on ${nameUnits(ids)} — authorization held${
            reason === "" ? "" : ` (${reason})`
          }.`,
        });
        return true;
      }
      // Routine revocation (intent expiry, disarm, generation fence): the end
      // of authority is worth a polite line, never an emergency announcement.
      dispatch({ type: "units-demoted", ids });
      dispatch({
        type: "polite",
        text: `Authorization ended on ${nameUnits(ids)}${
          reason === "" ? "" : ` — ${reason}`
        }.`,
      });
      return true;
    }
    case "inhibit.acknowledged": {
      const payload = payloadOf(frame);
      const unitId = typeof payload.unit_id === "string" ? payload.unit_id : "";
      dispatch({ type: "clear-assertive" });
      dispatch({
        type: "polite",
        text: `Inhibit latch acknowledged on ${
          unitId === "" ? "the fleet" : unitId
        }; recovery follows stable samples.`,
      });
      return true;
    }
    case "emergency_stop.acknowledged": {
      const payload = payloadOf(frame);
      const stopId = typeof payload.stop_id === "string" ? payload.stop_id : "";
      dispatch({ type: "clear-assertive" });
      dispatch({
        type: "polite",
        text: `Emergency stop acknowledged${stopId === "" ? "" : ` (${stopId})`}.`,
      });
      return true;
    }
    case "audit.appended": {
      // The kernel's per-tick decision summaries: an authorizing or clamping
      // decision (or an accepted dispatch) means the requested/allowed figures
      // just moved, so the world must be re-read — the snapshot frame arrives
      // once per connection and no other bus frame carries the figures.
      const payload = payloadOf(frame);
      const eventType = typeof payload.event_type === "string" ? payload.event_type : "";
      const result = typeof payload.result === "string" ? payload.result : "";
      const authorizing =
        eventType === "intent_accepted" ||
        (eventType === "control_decision" && (result === "authorized" || result === "clamped"));
      return authorizing;
    }
    case "intent.accepted": {
      // The request landed: the kernel grants authority on its next tick, and
      // the accepted payload already names the request (direction, watts,
      // units). Announce it and re-read — the next world carries the figures.
      const payload = payloadOf(frame);
      const unitIds = Array.isArray(payload.unit_ids)
        ? payload.unit_ids.filter((id): id is string => typeof id === "string")
        : [];
      const watts = typeof payload.watts === "number" ? payload.watts : null;
      const direction = typeof payload.direction === "string" ? payload.direction : "";
      dispatch({
        type: "polite",
        // The announcement is a display string composed once and never
        // re-formatted, so its watt figure is formatted here — the shared
        // display-precision module, never a raw wire float.
        text: `Power request accepted${
          watts === null ? "" : ` — ${direction} ${formatWatts(watts)}`
        } for ${nameUnits(unitIds)}.`,
      });
      return true;
    }
    case "intent.expired":
      // Feature-detected: the backend publishes the end of a request this way
      // once its intent-lifecycle event lands; treat it as authority changed
      // either way (today the expiry revocation arrives as
      // authorization.revoked instead).
      dispatch({ type: "polite", text: "The power request ended." });
      return true;
    case "intent.cancelled":
      // A cancelled request (this session or another operator's): the live
      // end of a power request is authority changing, so the world re-reads.
      dispatch({ type: "polite", text: "The power request was cancelled." });
      return true;
    case "authorization.granted":
      // Feature-detected: an authority grant publishes nothing today (the
      // grant is only visible in the next snapshot); when the backend adds
      // the grant event this re-reads immediately so the figures move at the
      // moment of the grant instead of the next refresh.
      return true;
    default:
      // observation.published, audit.appended refusals, ... are not
      // shell-level facts; subscribers that care read them through the feed.
      return false;
  }
}

const HEALTH_POLL_MS = 15000;
/**
 * Interim live cadence: while the stream is live and the tab visible, the
 * shell re-reads and republishes the snapshot every few seconds. Authority
 * GRANTS publish nothing on the bus today (the grant is only visible in the
 * next snapshot) and intent expiry is silent, so without this cadence a
 * console that misses an audit summary still freezes its request panels until
 * the next reconnect. When the backend's queued intent/authority events land,
 * this becomes a safety net rather than the primary source.
 */
export const LIVE_SNAPSHOT_POLL_MS = 2500;
/** How often the staleness clock re-renders: the badge's "last update N s ago"
 * must move once a second while it is showing, and never at all while live. */
const HEALTH_TICK_MS = 1000;
/** Two foreground signals firing back-to-back (switching to the tab fires
 * both visibilitychange and focus) count as one re-check. */
const RESYNC_DEBOUNCE_MS = 1000;
/** A discontinuity does not tear the current world off the screen mid-glance:
 * the pre-discontinuity picture renders, then the refetch runs. */
const RESYNC_REFETCH_DELAY_MS = 60;
/** Authority frames arrive in bursts (a stop also revokes and audits), so the
 * refetch is debounced: one read serves the whole burst. */
const AUTHORITY_REFETCH_DELAY_MS = 120;

/**
 * The reconnect budget: a bounded run of widening waits. An unreachable or
 * failing stream is retried this many times and no more — a permanent tight
 * reconnect loop would hammer the service and buy nothing.
 */
export const STREAM_RETRY_DELAYS_MS: readonly number[] = [500, 1000, 2000, 4000, 8000];

/** The wait before the next reconnect attempt, or null once the budget is spent. */
export function nextStreamRetryDelayMs(attemptsMade: number): number | null {
  return attemptsMade < STREAM_RETRY_DELAYS_MS.length
    ? (STREAM_RETRY_DELAYS_MS[attemptsMade] ?? null)
    : null;
}

/** Injection points the behavior suite uses to keep time testable. */
export interface ConsoleDataOptions {
  /** Reconnect waits, in attempt order; the last entry is the final attempt. */
  retryDelaysMs?: readonly number[];
  /** How long without a frame a "live" connection may go before it is stale. */
  staleAfterMs?: number;
  /** The live-cadence snapshot poll; a huge value disables it for a test. */
  livePollMs?: number;
}

export function useConsoleData(
  plane: SharedDataPlane | null,
  onUnauthorized: (error: ApiClientErrorType) => void,
  options: ConsoleDataOptions = {},
): ConsoleData & {
  retrySnapshot: () => void;
  retryStream: () => void;
  /** An acknowledge 200 landed: clear that latch optimistically, then confirm. */
  releaseStopLatch: (stopId: string) => void;
} {
  const retryDelays = options.retryDelaysMs ?? STREAM_RETRY_DELAYS_MS;
  const staleAfterMs = options.staleAfterMs ?? STALE_AFTER_MS;
  const livePollMs = options.livePollMs ?? LIVE_SNAPSHOT_POLL_MS;
  const [state, dispatch] = useReducer(reducer, INITIAL);
  // The shared per-unit figure tracker (one instance per session, fed by the
  // shell's one stream): every frame passes through it, and every snapshot
  // read seeds it from the snapshot's own `intent` block when the backend
  // sends one. The banner consumes its maps through the returned ConsoleData.
  const unitFigures = useUnitIntentFigures();
  const adoptFigures = unitFigures.adoptSnapshot;
  const consumeFigures = unitFigures.consumeEvent;
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  const stateRef = useRef(state);
  stateRef.current = state;
  /**
   * The last sequence this session consumed. It outlives a single connection
   * so a manual restart still reconnects from the live edge rather than
   * dropping back to a cursor-less connection; a new session resets it.
   */
  const lastSequenceRef = useRef<number | null>(null);

  const dropSession = useCallback((error: unknown): void => {
    if (error instanceof ApiClientError) {
      onUnauthorizedRef.current(error);
    }
  }, []);

  /** A manual REST-only recovery: refresh the picture, never rebuild the stream. */
  const retrySnapshot = useCallback((): void => {
    if (plane === null) {
      return;
    }
    plane.refresh().then(
      (raw) => {
        adoptFigures(raw);
        dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(raw) });
      },
      (error: unknown) => {
        if (isUnauthorizedError(error)) {
          dropSession(error);
          return;
        }
        dispatch({ type: "snapshot-failed", refusal: asRefusal(error) });
      },
    );
  }, [plane, dropSession, adoptFigures]);

  /**
   * An acknowledge call returned 200: drop that stop from the on-screen latch
   * state now (the optimistic half) and immediately re-read the world (the
   * confirming half — the next snapshot's `active_stops` is the truth; the
   * interim live-cadence poll would confirm it anyway). A failed read changes
   * nothing: the next poll retries.
   */
  const releaseStopLatch = useCallback(
    (stopId: string): void => {
      dispatch({ type: "stop-released", stopId });
      if (plane !== null) {
        plane
          .refresh()
          .then((raw) => {
            adoptFigures(raw);
            dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(raw) });
          })
          .catch((error: unknown) => {
            if (isUnauthorizedError(error)) {
              dropSession(error);
            }
          });
      }
    },
    [plane, dropSession, adoptFigures],
  );

  /** A manual stream restart once the automatic budget is spent. */
  const retryStream = useCallback((): void => {
    setStreamEpoch((value) => value + 1);
  }, []);

  // --- the self-diagnosis clock ------------------------------------------------
  //
  // The staleness derivation needs a once-per-second render while it is
  // showing ("last update N s ago"); while the connection is live and frames
  // flow, the derived health does not change and no extra render is needed —
  // the interval's state write only has to exist, and every frame already
  // re-renders through the reducer.
  useEffect(() => {
    const timer = setInterval(() => {
      setNowMs(Date.now());
    }, HEALTH_TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);

  // --- the interim live cadence ------------------------------------------------
  //
  // While the stream is live and the tab visible, re-read the snapshot on a
  // steady cadence and let the plane republish it: authority grants publish
  // nothing on the bus today, so this is what keeps every element on screen
  // moving between bus events. A hidden tab does not poll (its timers are
  // throttled anyway and the visibility re-check resynchronizes on return).
  useEffect(() => {
    if (plane === null) {
      return undefined;
    }
    let inFlight = false;
    const timer = setInterval(() => {
      if (inFlight || stateRef.current.streamStatus !== "live") {
        return;
      }
      if (document.visibilityState !== "visible") {
        return;
      }
      inFlight = true;
      plane
        .refresh()
        .then(
          (raw) => {
            if (stateRef.current.snapshot === null || raw.snapshot_sequence >= stateRef.current.snapshot.sequence) {
              adoptFigures(raw);
              dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(raw) });
            }
          },
          (error: unknown) => {
            if (isUnauthorizedError(error)) {
              dropSession(error);
            }
            // A failed poll is not reported on its own: the stream's own
            // loss path owns the disconnected story.
          },
        )
        .finally(() => {
          inFlight = false;
        });
    }, livePollMs);
    return () => {
      clearInterval(timer);
    };
  }, [plane, livePollMs, dropSession, adoptFigures]);

  /**
   * Background-tab recovery: a browser throttles a hidden tab's timers to
   * ~once a minute, so a dropped stream can sit unnoticed and the last painted
   * age can freeze on screen for as long as the tab stays hidden. The moment
   * the tab becomes visible or focused again the stream is re-checked NOW: a
   * re-render re-derives every age from the current clock, and when the data
   * is quiet or the connection is down the stream is re-subscribed
   * immediately (the last seen sequence as the resume cursor) instead of
   * waiting out the remaining backoff.
   */
  useEffect(() => {
    let lastResyncAt = 0;
    const recheck = (): void => {
      // Always force a fresh render first: the operator's first glance must
      // carry the true age, never the number the throttled tab last painted.
      setNowMs(Date.now());
      const current = stateRef.current;
      const quiet =
        current.lastEventAtMs === null || Date.now() - current.lastEventAtMs > staleAfterMs;
      if (current.streamStatus === "live" && !quiet) {
        return;
      }
      const now = Date.now();
      if (now - lastResyncAt < RESYNC_DEBOUNCE_MS) {
        return;
      }
      lastResyncAt = now;
      setStreamEpoch((value) => value + 1);
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") {
        recheck();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", recheck);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", recheck);
    };
  }, [staleAfterMs]);

  // --- snapshot + health: shared reads through the plane ---------------------
  useEffect(() => {
    // A new session (or a return to the entry screen) starts a new timeline.
    lastSequenceRef.current = null;
    if (plane === null) {
      dispatch({ type: "reset" });
      return;
    }
    let cancelled = false;

    const pollHealth = (): void => {
      plane.health().then(
        (health) => {
          if (!cancelled) {
            dispatch({ type: "health", health });
          }
        },
        (error: unknown) => {
          if (isUnauthorizedError(error)) {
            dropSession(error);
            return;
          }
          if (!cancelled) {
            dispatch({ type: "health-failed" });
          }
        },
      );
    };
    pollHealth();
    const healthTimer = setInterval(pollHealth, HEALTH_POLL_MS);

    plane.snapshot().then(
      (raw) => {
        if (!cancelled) {
          adoptFigures(raw);
          dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(raw) });
        }
      },
      (error: unknown) => {
        if (isUnauthorizedError(error)) {
          dropSession(error);
          return;
        }
        if (!cancelled) {
          dispatch({ type: "snapshot-failed", refusal: asRefusal(error) });
        }
      },
    );

    return () => {
      cancelled = true;
      clearInterval(healthTimer);
    };
  }, [plane, dropSession, adoptFigures]);

  // --- the one real event-stream connection ---------------------------------
  useEffect(() => {
    if (plane === null) {
      return;
    }
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let refetchTimer: ReturnType<typeof setTimeout> | null = null;
    let refetchInFlight = false;
    let refetchQueued = false;
    let active: RealStream | null = null;

    const wait = (ms: number): Promise<void> =>
      new Promise((resolve) => {
        retryTimer = setTimeout(resolve, ms);
      });

    const runAuthorityRefresh = (): void => {
      if (refetchInFlight) {
        refetchQueued = true;
        return;
      }
      refetchInFlight = true;
      plane
        .refresh()
        .then(
          (raw) => {
            if (!cancelled) {
              adoptFigures(raw);
              dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(raw) });
            }
          },
          (error: unknown) => {
            if (isUnauthorizedError(error)) {
              dropSession(error);
              return;
            }
            // The refetch failed; the last known picture stays and the next
            // authority frame schedules another attempt.
          },
        )
        .finally(() => {
          refetchInFlight = false;
          if (refetchQueued && !cancelled) {
            refetchQueued = false;
            refetchTimer = setTimeout(runAuthorityRefresh, AUTHORITY_REFETCH_DELAY_MS);
          }
        });
    };

    const scheduleAuthorityRefresh = (): void => {
      if (refetchTimer !== null) {
        return; // one read is already scheduled: the burst coalesces into it
      }
      refetchTimer = setTimeout(() => {
        refetchTimer = null;
        runAuthorityRefresh();
      }, AUTHORITY_REFETCH_DELAY_MS);
    };

    const announceLoss = (): void => {
      dispatch({ type: "stream", status: "down" });
      // A new loss supersedes any earlier restore notice: the restart message
      // must never linger beside a connection that is down again.
      dispatch({ type: "clear-restart-notice" });
      plane.streamLost();
      // "Active" is a snapshot claim that can lag the wire, so a unit with a
      // live non-idle request counts as under control too: losing the stream
      // while power is flowing is exactly the assertive case.
      const controlling = (stateRef.current.snapshot?.units ?? []).filter(
        (unit) =>
          unit.lifecycle === "active" ||
          (unit.requested.direction !== "idle" && unit.requested.watts > 0),
      );
      if (controlling.length > 0) {
        const names = controlling.map((unit) => unit.unitId).join(", ");
        dispatch({
          type: "assertive",
          text: `Live updates lost while ${names} ${
            controlling.length > 1 ? "were" : "was"
          } under active control — control is unavailable until the connection returns.`,
        });
      }
    };

    void (async () => {
      let cursor: number | undefined = lastSequenceRef.current ?? undefined;
      let lastSequence: number | null = lastSequenceRef.current;
      let attempt = 0;
      while (!cancelled) {
        dispatch({ type: "stream", status: "connecting" });
        let resync: { cursor: number | null } | null = null;
        let received = false;
        // A connection opened with a cursor is a resume after this session had
        // already consumed frames — the operator-visible trace of the stream
        // having been away (a controller restart swaps the process and the
        // socket with it). Its first delivered frame surfaces the calm notice.
        const resumed = cursor !== undefined;
        let noticedRestore = false;
        active = plane.openRealStream(cursor);
        try {
          for await (const frame of active.frames) {
            if (cancelled) {
              return;
            }
            received = true;
            if (!noticedRestore) {
              noticedRestore = true;
              if (resumed) {
                dispatch({ type: "restart-notice" });
              }
            }
            dispatch({ type: "frame-received", at: Date.now() });
            // Every frame feeds the shared per-unit figure tracker (its own
            // no-ops carry most kinds), and a snapshot frame seeds it from the
            // snapshot's own `intent` block when the backend sends one.
            consumeFigures(frame);
            if (frame.type === "resync_required") {
              resync = {
                cursor: typeof frame.snapshot_sequence === "number" ? frame.snapshot_sequence : null,
              };
              break;
            }
            lastSequence = Math.max(lastSequence ?? frame.sequence, frame.sequence);
            lastSequenceRef.current = lastSequence;
            if (frame.type === "snapshot") {
              adoptFigures(frame.data);
              plane.publishSnapshot(frame.sequence, frame.data);
            } else {
              plane.publishEvent(frame);
            }
            if (applyEventFrame(frame, dispatch)) {
              scheduleAuthorityRefresh();
            }
          }
        } catch (error) {
          if (cancelled) {
            return;
          }
          if (isUnauthorizedError(error)) {
            dropSession(error);
            return;
          }
          // The stream's own failure envelope surfaces — an operator staring
          // at "reconnecting" deserves to know what actually went wrong.
          dispatch({ type: "stream-error", refusal: asRefusal(error) });
        }
        if (cancelled) {
          return;
        }
        if (resync !== null) {
          // Discontinuity: debounce the refetch so the world the operator was
          // just looking at commits first (a marker storm collapses into one
          // refetch), then reconnect from the recovery cursor — never a
          // replay from zero.
          await wait(RESYNC_REFETCH_DELAY_MS);
          if (cancelled) {
            return;
          }
          try {
            const fresh = await plane.refresh();
            if (cancelled) {
              return;
            }
            adoptFigures(fresh);
            dispatch({ type: "snapshot-rest", snapshot: normalizeSnapshot(fresh) });
            dispatch({ type: "polite", text: "Resynchronized — the fleet picture is current." });
          } catch (error) {
            if (cancelled) {
              return;
            }
            if (isUnauthorizedError(error)) {
              dropSession(error);
              return;
            }
            // The refetch failed but the last known picture stays; reconnect.
          }
          cursor = resync.cursor ?? lastSequence ?? undefined;
          attempt = 0;
          continue;
        }
        announceLoss();
        // A connection that actually delivered frames was healthy: the next
        // loss starts a fresh budget rather than inheriting old attempts.
        if (received) {
          attempt = 0;
        }
        const delay = attempt < retryDelays.length ? (retryDelays[attempt] ?? null) : null;
        if (delay === null) {
          // The budget is spent: stop asking, say so, and leave the retry to
          // the operator rather than hammering a failing endpoint forever.
          dispatch({ type: "stream-exhausted" });
          return;
        }
        attempt += 1;
        await wait(delay);
        cursor = lastSequence ?? undefined;
      }
    })();

    return () => {
      // Effect-driven teardown: sign-out, unmount, or a manual restart closes
      // the authenticated connection now, not whenever a frame next arrives.
      cancelled = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
      }
      if (refetchTimer !== null) {
        clearTimeout(refetchTimer);
      }
      active?.close();
      plane.streamLost();
    };
  }, [plane, streamEpoch, dropSession, adoptFigures, consumeFigures]);

  const connection = connectionHealth(state, nowMs, staleAfterMs);
  const secondsSinceUpdate =
    state.lastEventAtMs === null
      ? null
      : Math.max(0, Math.round((nowMs - state.lastEventAtMs) / 1000));
  return {
    ...state,
    unitFigures: {
      requestedByUnit: unitFigures.requestedByUnit,
      authorizedByUnit: unitFigures.authorizedByUnit,
      directionsByUnit: unitFigures.directionsByUnit,
    },
    connection,
    secondsSinceUpdate,
    retrySnapshot,
    retryStream,
    releaseStopLatch,
  };
}
