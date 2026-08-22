/**
 * The shell's live data plane: REST snapshot + health, the single event-stream
 * connection, the resync protocol, and loss-driven reconnection.
 *
 * Wire behavior (UI_CONTRACTS.md "Data sources"):
 * - `openEvents(afterSequence?)` yields the authoritative `snapshot` frame
 *   first, then ordered events. A `resync_required` frame ends iteration; the
 *   shell then refetches the snapshot and reconnects with the recovery cursor
 *   (the marker's `snapshot_sequence` when present, else the last seen one).
 * - A lost stream marks the event-stream fact down, keeps the last known data,
 *   announces assertively when control was active, and retries automatically
 *   carrying the last seen sequence as the cursor — never a replay from zero.
 * - A 401 anywhere is the single "session over" signal: it bubbles to the
 *   shell through `onUnauthorized` with the exact error envelope.
 *
 * All reads go through the SharedDataPlane so mounted views share them.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { ApiClientError, isUnauthorizedError } from "../api/client";
import type { ApiClientError as ApiClientErrorType, Health } from "../api/client";
import { isRecord, normalizeSnapshot, type FleetSnapshot, type Lifecycle } from "./fleet";
import type { SharedDataPlane } from "./SharedDataPlane";

export type StreamStatus = "connecting" | "live" | "down";

export interface RefusalEnvelope {
  code: string;
  message: string;
}

export interface ConsoleData {
  snapshot: FleetSnapshot | null;
  health: Health | null;
  /** null = still checking; false = the API could not be reached. */
  apiReachable: boolean | null;
  snapshotError: RefusalEnvelope | null;
  streamStatus: StreamStatus;
  polite: string[];
  assertive: string[];
}

interface State {
  snapshot: FleetSnapshot | null;
  health: Health | null;
  apiReachable: boolean | null;
  snapshotError: RefusalEnvelope | null;
  streamStatus: StreamStatus;
  polite: string[];
  assertive: string[];
}

type Action =
  | { type: "reset" }
  | { type: "snapshot"; snapshot: FleetSnapshot }
  | { type: "snapshot-failed"; refusal: RefusalEnvelope }
  | { type: "health"; health: Health }
  | { type: "health-failed" }
  | { type: "units-patched"; lifecycles: Record<string, Lifecycle> }
  | { type: "stream"; status: StreamStatus }
  | { type: "polite"; text: string }
  | { type: "assertive"; text: string };

const INITIAL: State = {
  snapshot: null,
  health: null,
  apiReachable: null,
  snapshotError: null,
  streamStatus: "connecting",
  polite: [],
  assertive: [],
};

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "reset":
      return { ...INITIAL, polite: [], assertive: [] };
    case "snapshot":
      return { ...state, snapshot: action.snapshot, snapshotError: null };
    case "snapshot-failed":
      return { ...state, snapshotError: action.refusal };
    case "health":
      return { ...state, health: action.health, apiReachable: true };
    case "health-failed":
      return { ...state, apiReachable: false };
    case "units-patched": {
      if (state.snapshot === null) {
        return state;
      }
      return {
        ...state,
        snapshot: {
          ...state.snapshot,
          units: state.snapshot.units.map((unit) =>
            Object.prototype.hasOwnProperty.call(action.lifecycles, unit.unitId)
              ? { ...unit, lifecycle: action.lifecycles[unit.unitId]! }
              : unit,
          ),
        },
      };
    }
    case "stream":
      return { ...state, streamStatus: action.status };
    case "polite":
      return { ...state, polite: [...state.polite, action.text].slice(-5) };
    case "assertive":
      return { ...state, assertive: [action.text] };
  }
}

function asRefusal(error: unknown): RefusalEnvelope {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message };
  }
  return { code: "unexpected_error", message: "Something unexpected went wrong." };
}

function payloadOf(frame: Record<string, unknown>): Record<string, unknown> {
  return isRecord(frame.payload) ? frame.payload : {};
}

function listedUnitIds(payload: Record<string, unknown>): string[] {
  // unit.armed / unit.disarmed carry `{units: [{unit_id, status, ...}]}`.
  const units = Array.isArray(payload.units) ? payload.units : [];
  const ids: string[] = [];
  for (const entry of units) {
    if (isRecord(entry) && typeof entry.unit_id === "string") {
      ids.push(entry.unit_id);
    }
  }
  return ids;
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

function applyEventFrame(frame: Record<string, unknown>, dispatch: (action: Action) => void): void {
  switch (frame.type) {
    case "snapshot": {
      dispatch({ type: "snapshot", snapshot: normalizeSnapshot(frame.data) });
      dispatch({ type: "stream", status: "live" });
      dispatch({ type: "polite", text: "Fleet picture loaded." });
      return;
    }
    case "unit.armed": {
      const ids = listedUnitIds(payloadOf(frame));
      if (ids.length === 0) {
        return;
      }
      const lifecycles: Record<string, Lifecycle> = {};
      for (const id of ids) {
        lifecycles[id] = "armed_idle";
      }
      dispatch({ type: "units-patched", lifecycles });
      dispatch({
        type: "polite",
        text: `${nameUnits(ids)} ${ids.length > 1 ? "are" : "is"} now armed.`,
      });
      return;
    }
    case "unit.disarmed": {
      const ids = listedUnitIds(payloadOf(frame));
      if (ids.length === 0) {
        return;
      }
      const lifecycles: Record<string, Lifecycle> = {};
      for (const id of ids) {
        lifecycles[id] = "disarmed";
      }
      dispatch({ type: "units-patched", lifecycles });
      dispatch({
        type: "polite",
        text: `${nameUnits(ids)} ${ids.length > 1 ? "are" : "is"} now disarmed.`,
      });
      return;
    }
    case "emergency_stop.latched": {
      const payload = payloadOf(frame);
      const reason = reasonOf(payload);
      dispatch({
        type: "assertive",
        text: `Emergency stop latched on ${nameUnits(directUnitIds(payload))}${
          reason === "" ? "" : ` — ${reason}`
        }.`,
      });
      return;
    }
    case "authorization.revoked": {
      // The latched-inhibit path surfaces on the bus as a revoked authorization
      // whose reason is the latched cause (see the suite's reconciliation pin).
      const payload = payloadOf(frame);
      const reason = reasonOf(payload);
      dispatch({
        type: "assertive",
        text: `Inhibit latched on ${nameUnits(directUnitIds(payload))} — authorization held${
          reason === "" ? "" : ` (${reason})`
        }.`,
      });
      return;
    }
    default:
      // observation.published, audit.appended, intent.accepted, ... are not
      // shell-level facts; subscribers that care read them through the feed.
      return;
  }
}

const HEALTH_POLL_MS = 15000;
const RETRY_DELAYS_MS: readonly number[] = [300, 800, 1600, 3200];
/** A discontinuity does not tear the current world off the screen mid-glance:
 * the pre-discontinuity picture renders, then the refetch runs. */
const RESYNC_REFETCH_DELAY_MS = 60;

export function useConsoleData(
  plane: SharedDataPlane | null,
  onUnauthorized: (error: ApiClientErrorType) => void,
): ConsoleData & { retrySnapshot: () => void } {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const [epoch, setEpoch] = useState(0);
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  const stateRef = useRef(state);
  stateRef.current = state;

  const retrySnapshot = useCallback(() => {
    setEpoch((value) => value + 1);
  }, []);

  useEffect(() => {
    if (plane === null) {
      dispatch({ type: "reset" });
      return;
    }
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const dropSession = (error: unknown): void => {
      if (error instanceof ApiClientError) {
        onUnauthorizedRef.current(error);
      }
    };

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
          dispatch({ type: "snapshot", snapshot: normalizeSnapshot(raw) });
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

    const wait = (ms: number): Promise<void> =>
      new Promise((resolve) => {
        retryTimer = setTimeout(resolve, ms);
      });

    const announceLoss = (): void => {
      dispatch({ type: "stream", status: "down" });
      plane.streamLost();
      const activeUnits = (stateRef.current.snapshot?.units ?? []).filter(
        (unit) => unit.lifecycle === "active",
      );
      if (activeUnits.length > 0) {
        const names = activeUnits.map((unit) => unit.unitId).join(", ");
        dispatch({
          type: "assertive",
          text: `Live updates lost while ${names} ${
            activeUnits.length > 1 ? "were" : "was"
          } active — control is unavailable until the connection returns.`,
        });
      }
    };

    void (async () => {
      let cursor: number | undefined = undefined;
      let lastSequence: number | null = null;
      let attempt = 0;
      while (!cancelled) {
        dispatch({ type: "stream", status: "connecting" });
        let resync: { cursor: number | null } | null = null;
        try {
          for await (const frame of plane.openRealStream(cursor)) {
            if (cancelled) {
              return;
            }
            if (!isRecord(frame)) {
              continue;
            }
            if (typeof frame.sequence === "number") {
              lastSequence = Math.max(lastSequence ?? frame.sequence, frame.sequence);
            }
            if (frame.type === "resync_required") {
              resync = {
                cursor: typeof frame.snapshot_sequence === "number" ? frame.snapshot_sequence : null,
              };
              break;
            }
            if (frame.type === "snapshot") {
              plane.publishSnapshot(
                typeof frame.sequence === "number" ? frame.sequence : 0,
                frame.data,
              );
            } else {
              plane.publishEvent(frame);
            }
            applyEventFrame(frame, dispatch);
          }
        } catch (error) {
          if (cancelled) {
            return;
          }
          if (isUnauthorizedError(error)) {
            dropSession(error);
            return;
          }
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
            dispatch({ type: "snapshot", snapshot: normalizeSnapshot(fresh) });
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
        const delay = RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)] ?? 3200;
        attempt += 1;
        await wait(delay);
        cursor = lastSequence ?? undefined;
      }
    })();

    return () => {
      cancelled = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
      }
      clearInterval(healthTimer);
    };
  }, [plane, epoch]);

  return { ...state, retrySnapshot };
}
