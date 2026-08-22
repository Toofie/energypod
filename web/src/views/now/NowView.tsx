/**
 * "Now" (control) view — docs/UI_CONTRACTS.md, "Now (control)".
 *
 * The control surface of the console. It renders the current request as four
 * separate facts (requested / allowed / actual / remaining time), adopts the
 * WebSocket snapshot frame as the authoritative first picture and then applies
 * live intent events, and drives the deliberate control flows: arm (per-unit
 * readiness checklist + explicit "ARM" confirmation), disarm, charge and
 * discharge as separate dispatch actions with a composed preview and
 * constrained inputs, emergency stop with exact-id acknowledgement, and
 * inhibit acknowledgement. Every refusal is surfaced verbatim (the envelope's
 * code and message); the view never invents a reason the API did not give.
 *
 * Wire casing (UI_CONTRACTS.md "Wire casing"): enums arrive as lowercase
 * strings ("charge"/"discharge"/"idle", "disarmed"/"armed_idle"/"active"/
 * "inhibited", quality "good"/"stale"/...). Logic compares the lowercase wire
 * values; labels shown to humans are capitalized. The client module's TS types
 * spell directions in uppercase, so this view narrows frames defensively to
 * the real wire shape rather than trusting those annotations.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClientError, createApiClient } from "../../api/client";
import type { Health } from "../../api/client";

// ---------------------------------------------------------------------------
// wire shapes (runtime truth: lowercase enums) and narrowing
// ---------------------------------------------------------------------------

interface WirePower {
  direction: string;
  watts: number;
}

interface WireUnit {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: WirePower;
  authorized_power: WirePower | null;
  measured_watts: number | null;
}

interface WireSnapshot {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: WireUnit[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asPower(value: unknown): WirePower | null {
  if (!isRecord(value)) return null;
  if (typeof value.direction !== "string" || typeof value.watts !== "number") {
    return null;
  }
  return { direction: value.direction, watts: value.watts };
}

function asUnit(value: unknown): WireUnit | null {
  if (!isRecord(value)) return null;
  const requested = asPower(value.requested_power);
  if (typeof value.unit_id !== "string" || typeof value.lifecycle !== "string" || requested === null) {
    return null;
  }
  return {
    unit_id: value.unit_id,
    lifecycle: value.lifecycle,
    telemetry_age_s: typeof value.telemetry_age_s === "number" ? value.telemetry_age_s : null,
    quality: typeof value.quality === "string" ? value.quality : "unknown",
    requested_power: requested,
    authorized_power: asPower(value.authorized_power),
    measured_watts: typeof value.measured_watts === "number" ? value.measured_watts : null,
  };
}

function asSnapshot(value: unknown): WireSnapshot | null {
  if (!isRecord(value) || !Array.isArray(value.units)) return null;
  if (typeof value.site_id !== "string" || typeof value.snapshot_sequence !== "number") {
    return null;
  }
  const units: WireUnit[] = [];
  for (const entry of value.units) {
    const unit = asUnit(entry);
    if (unit !== null) units.push(unit);
  }
  return {
    site_id: value.site_id,
    snapshot_sequence: value.snapshot_sequence,
    captured_at: typeof value.captured_at === "string" ? value.captured_at : "",
    units,
  };
}

function toApiClientError(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) return error;
  return new ApiClientError({
    code: "unexpected_failure",
    message: error instanceof Error ? error.message : "The request failed unexpectedly",
    details: null,
    request_id: "",
    status: 0,
  });
}

// ---------------------------------------------------------------------------
// display helpers — lowercase wire values in, human-capitalized labels out
// ---------------------------------------------------------------------------

function displayDirection(direction: string): string {
  const normalized = direction.toLowerCase();
  if (normalized === "charge") return "Charge";
  if (normalized === "discharge") return "Discharge";
  return "Idle";
}

/** Locale-style grouping without depending on ICU data: 1500 -> "1,500 W". */
function formatWatts(watts: number): string {
  const grouped = Math.round(watts)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${grouped} W`;
}

function formatSeconds(seconds: number): string {
  return `${seconds} s`;
}

/** Fits the service's canonical identifier grammar (rest.py `_ID_PATTERN`). */
let dispatchKeyCounter = 0;
function newDispatchKey(): string {
  dispatchKeyCounter += 1;
  return [
    "console-dispatch",
    Date.now().toString(36),
    dispatchKeyCounter.toString(36),
    Math.random().toString(36).slice(2, 10),
  ].join("-");
}

/** Mutation outcomes the arm/disarm endpoints return: {units: [{unit_id, status, reason}]}. */
type MutationRow = { unitId: string; status: string; reason: string | null };

function asMutationRows(value: unknown): MutationRow[] {
  if (!isRecord(value) || !Array.isArray(value.units)) return [];
  const rows: MutationRow[] = [];
  for (const entry of value.units) {
    if (!isRecord(entry)) continue;
    if (typeof entry.unit_id !== "string" || typeof entry.status !== "string") continue;
    rows.push({
      unitId: entry.unit_id,
      status: entry.status,
      reason: typeof entry.reason === "string" ? entry.reason : null,
    });
  }
  return rows;
}

type DispatchOutcome = { status: string; intentId: string | null; expiresInSeconds: number | null };

function asDispatchOutcome(value: unknown): DispatchOutcome | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  return {
    status: value.status,
    intentId: typeof value.intent_id === "string" ? value.intent_id : null,
    expiresInSeconds: typeof value.expires_in_s === "number" ? value.expires_in_s : null,
  };
}

type StopOutcome = {
  stopId: string | null;
  status: string;
  degraded: string[];
  fencedGeneration: number | null;
};

function asStopOutcome(value: unknown): StopOutcome | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  return {
    stopId: typeof value.stop_id === "string" ? value.stop_id : null,
    status: value.status,
    degraded: Array.isArray(value.degraded)
      ? value.degraded.filter((entry): entry is string => typeof entry === "string")
      : [],
    fencedGeneration: typeof value.fenced_generation === "number" ? value.fenced_generation : null,
  };
}

function asAcknowledgedStatus(value: unknown): string | null {
  if (isRecord(value) && typeof value.status === "string") return value.status;
  return null;
}

type InhibitOutcome = { unitId: string; status: string; latchCleared: boolean };

function asInhibitOutcome(value: unknown): InhibitOutcome | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  return {
    unitId: typeof value.unit_id === "string" ? value.unit_id : "",
    status: value.status,
    latchCleared: value.latch_cleared === true,
  };
}

/** Which dispatch input an API validation error belongs to (envelope `details.errors[].location`). */
function apiErrorField(error: ApiClientError): "watts" | "minutes" | null {
  if (!isRecord(error.details) || !Array.isArray(error.details.errors)) return null;
  for (const entry of error.details.errors) {
    if (!isRecord(entry) || !Array.isArray(entry.location)) continue;
    const path = entry.location.map((segment) => String(segment));
    if (path.includes("watts")) return "watts";
    if (path.some((segment) => segment === "ttl_s" || segment === "ttl" || segment === "minutes" || segment === "duration")) {
      return "minutes";
    }
  }
  return null;
}

function apiFieldMessages(error: ApiClientError): string[] {
  if (!isRecord(error.details) || !Array.isArray(error.details.errors)) return [];
  const messages: string[] = [];
  for (const entry of error.details.errors) {
    if (isRecord(entry) && typeof entry.message === "string") messages.push(entry.message);
  }
  return messages;
}

// ---------------------------------------------------------------------------
// dialog primitive: focus trap, Escape to decline, focus restored to the opener
// ---------------------------------------------------------------------------

function focusableIn(container: HTMLElement): HTMLElement[] {
  const nodes = container.querySelectorAll<HTMLElement>(
    [
      "button:not([disabled])",
      "input:not([disabled])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      'a[href]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(", "),
  );
  return Array.from(nodes);
}

function Dialog({
  title,
  onDecline,
  children,
}: {
  title: string;
  onDecline: () => void;
  children: ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const container = containerRef.current;
    const focusables = container !== null ? focusableIn(container) : [];
    (focusables[0] ?? container)?.focus();
    return () => {
      opener?.focus();
    };
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onDecline();
      return;
    }
    if (event.key === "Tab") {
      // jsdom and user-event never move focus for Tab themselves: the dialog
      // owns the key and cycles focus inside itself.
      event.preventDefault();
      const container = containerRef.current;
      if (container === null) return;
      const focusables = focusableIn(container);
      if (focusables.length === 0) {
        container.focus();
        return;
      }
      const current = document.activeElement;
      const index = focusables.indexOf(current instanceof HTMLElement ? current : container);
      let next: number;
      if (index === -1) {
        next = event.shiftKey ? focusables.length - 1 : 0;
      } else if (event.shiftKey) {
        next = index === 0 ? focusables.length - 1 : index - 1;
      } else {
        next = index === focusables.length - 1 ? 0 : index + 1;
      }
      focusables[next]?.focus();
    }
  };

  return (
    <div className="dialog-backdrop">
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="dialog"
      >
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

/**
 * The API error envelope rendered verbatim: the code and the message each in
 * their own element (exact-text findable), optionally followed by per-field
 * detail messages from `details.errors`.
 */
function ApiErrorBlock({
  id,
  error,
  messages = [],
}: {
  id: string;
  error: ApiClientError;
  messages?: string[];
}) {
  return (
    <div id={id} className="api-error">
      <p>
        <code>{error.code}</code>
      </p>
      <p>{error.message}</p>
      {messages.map((message, index) => (
        <p key={index}>{message}</p>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// the view
// ---------------------------------------------------------------------------

type ConnectionState = "connecting" | "live" | "lost";

type DialogState =
  | { kind: "arm" }
  | { kind: "disarm" }
  | { kind: "dispatch"; direction: "charge" | "discharge" }
  | { kind: "stop" }
  | { kind: "inhibit"; unitId: string };

type FieldErrors = { watts: string | null; minutes: string | null };
type DispatchApiError = { error: ApiClientError; field: "watts" | "minutes" | null };

/** UI-side bound mirroring the API's ttl_s <= 300 validation. */
const MAX_DURATION_MINUTES = 5;
const RECONNECT_DELAY_MS = 3000;

export function NowView({ token }: { token: string }) {
  const client = useMemo(() => createApiClient(token), [token]);

  // --- data state -----------------------------------------------------------
  const [snapshot, setSnapshot] = useState<WireSnapshot | null>(null);
  const [loadFailed, setLoadFailed] = useState<ApiClientError | null>(null);
  const [snapshotNonce, setSnapshotNonce] = useState(0);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [health, setHealth] = useState<Health | null>(null);
  const [expirySeconds, setExpirySeconds] = useState<number | null>(null);

  // --- control state ----------------------------------------------------------
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [mutationRows, setMutationRows] = useState<MutationRow[] | null>(null);
  const [armError, setArmError] = useState<ApiClientError | null>(null);
  const [stopDialogError, setStopDialogError] = useState<ApiClientError | null>(null);
  const [dispatchOutcome, setDispatchOutcome] = useState<DispatchOutcome | null>(null);
  const [dispatchWatts, setDispatchWatts] = useState("");
  const [dispatchMinutes, setDispatchMinutes] = useState("5");
  const [selectedUnitIds, setSelectedUnitIds] = useState<string[]>([]);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors | null>(null);
  const [dispatchApiError, setDispatchApiError] = useState<DispatchApiError | null>(null);
  const dispatchKeyRef = useRef<string | null>(null);
  const [stopOutcome, setStopOutcome] = useState<StopOutcome | null>(null);
  const [stopAckInput, setStopAckInput] = useState("");
  const [stopAckError, setStopAckError] = useState<ApiClientError | null>(null);
  const [stopAckOutcome, setStopAckOutcome] = useState<string | null>(null);
  const [inhibitOutcome, setInhibitOutcome] = useState<InhibitOutcome | null>(null);

  // --- REST snapshot (initial load + manual retry) ----------------------------

  useEffect(() => {
    let cancelled = false;
    client
      .getSnapshot()
      .then((value: unknown) => {
        if (cancelled) return;
        const wire = asSnapshot(value);
        if (wire !== null) {
          setSnapshot(wire);
          setLoadFailed(null);
        } else {
          setLoadFailed(
            new ApiClientError({
              code: "invalid_snapshot",
              message: "The snapshot response was not readable",
              details: null,
              request_id: "",
              status: 0,
            }),
          );
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadFailed(toApiClientError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [client, snapshotNonce]);

  // --- health (control readiness reasons feed the arm checklist) --------------

  useEffect(() => {
    let cancelled = false;
    client
      .getHealth()
      .then((value: Health) => {
        if (!cancelled) setHealth(value);
      })
      .catch(() => {
        // Health is advisory here; the arm dialog simply shows no extra reasons.
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  // --- event stream: snapshot frame first, then events; resync refetches -------

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: number | undefined;
    let resyncing = false;
    let lastSequence: number | undefined;

    const adoptSnapshot = (value: unknown): void => {
      const wire = asSnapshot(value);
      if (wire !== null) {
        setSnapshot(wire);
        setLoadFailed(null);
      }
    };

    const applyEvent = (frame: Record<string, unknown>): void => {
      const payload = isRecord(frame.payload) ? frame.payload : null;
      if (frame.type === "intent.accepted" && payload !== null) {
        const direction = typeof payload.direction === "string" ? payload.direction : null;
        const watts = typeof payload.watts === "number" ? payload.watts : null;
        const unitIds = Array.isArray(payload.unit_ids)
          ? payload.unit_ids.filter((entry): entry is string => typeof entry === "string")
          : [];
        if (direction !== null && watts !== null) {
          setSnapshot((previous) =>
            previous === null
              ? previous
              : {
                  ...previous,
                  units: previous.units.map((unit) =>
                    unitIds.includes(unit.unit_id)
                      ? { ...unit, requested_power: { direction, watts } }
                      : unit,
                  ),
                },
          );
          if (typeof payload.expires_in_s === "number") setExpirySeconds(payload.expires_in_s);
        }
      } else if (frame.type === "unit.armed" || frame.type === "unit.disarmed") {
        const lifecycle = frame.type === "unit.armed" ? "armed_idle" : "disarmed";
        const changed = new Set(
          payload !== null && Array.isArray(payload.units)
            ? payload.units
                .filter((entry): entry is Record<string, unknown> => isRecord(entry))
                .map((entry) => (typeof entry.unit_id === "string" ? entry.unit_id : ""))
                .filter((unitId) => unitId !== "")
            : [],
        );
        if (changed.size > 0) {
          setSnapshot((previous) =>
            previous === null
              ? previous
              : {
                  ...previous,
                  units: previous.units.map((unit) =>
                    changed.has(unit.unit_id) ? { ...unit, lifecycle } : unit,
                  ),
                },
          );
        }
      }
    };

    const refetchSnapshot = (): void => {
      client
        .getSnapshot()
        .then(adoptSnapshot)
        .catch((error: unknown) => {
          if (!cancelled) setLoadFailed(toApiClientError(error));
        });
    };

    const connect = (): void => {
      if (cancelled) return;
      const iterator = client.openEvents(lastSequence);
      void (async () => {
        try {
          for await (const frame of iterator) {
            if (cancelled) return;
            if (typeof frame.sequence === "number") {
              lastSequence = frame.sequence;
            }
            if (frame.type === "snapshot") {
              adoptSnapshot(frame.data);
              setConnection("live");
            } else if (frame.type === "resync_required") {
              // The pinned client ends iteration right after this marker; the
              // view resynchronizes from a fresh snapshot and reconnects with
              // the last seen sequence as the cursor.
              resyncing = true;
            } else {
              applyEvent(frame);
              setConnection("live");
            }
          }
          onStreamClosed();
        } catch {
          onStreamClosed();
        }
      })();
    };

    const onStreamClosed = (): void => {
      if (cancelled) return;
      if (resyncing) {
        resyncing = false;
        setConnection("connecting");
        refetchSnapshot();
        connect();
        return;
      }
      setConnection("lost");
      reconnectTimer = window.setTimeout(connect, RECONNECT_DELAY_MS);
    };

    connect();
    return () => {
      cancelled = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    };
  }, [client]);

  // --- derived fleet state -----------------------------------------------------

  const units = snapshot?.units ?? [];
  const armableUnits = units.filter((unit) => unit.lifecycle === "disarmed");
  const armedUnits = units.filter(
    (unit) => unit.lifecycle === "armed_idle" || unit.lifecycle === "active",
  );
  const inhibitedUnits = units.filter((unit) => unit.lifecycle === "inhibited");

  const activeUnits = units.filter(
    (unit) => unit.requested_power.direction !== "idle" || unit.requested_power.watts > 0,
  );
  const authorizedUnits = units.filter(
    (unit): unit is WireUnit & { authorized_power: WirePower } => unit.authorized_power !== null,
  );
  const measuredUnits = units.filter(
    (unit): unit is WireUnit & { measured_watts: number } => unit.measured_watts !== null,
  );

  const requestedText =
    activeUnits.length > 0
      ? activeUnits
          .map(
            (unit) =>
              `${displayDirection(unit.requested_power.direction)} · ${formatWatts(unit.requested_power.watts)}`,
          )
          .join("; ")
      : "None";
  const allowedText =
    authorizedUnits.length > 0
      ? authorizedUnits
          .map(
            (unit) =>
              `${displayDirection(unit.authorized_power.direction)} · ${formatWatts(unit.authorized_power.watts)}`,
          )
          .join("; ")
      : activeUnits.length > 0
        ? "None yet"
        : "None";
  const actualText =
    measuredUnits.length > 0
      ? measuredUnits
          .map((unit) => {
            const age =
              unit.telemetry_age_s !== null ? ` (${formatSeconds(unit.telemetry_age_s)} ago)` : "";
            return `${formatWatts(unit.measured_watts)}${age}`;
          })
          .join("; ")
      : "No measurement available";
  const remainingText =
    expirySeconds !== null ? `${formatSeconds(expirySeconds)} left` : "Not available";

  const controlReasons = health?.control_readiness.reasons ?? [];

  // --- dialog plumbing -----------------------------------------------------------

  const closeDialog = (): void => {
    setDialog(null);
    setArmError(null);
    setStopDialogError(null);
    setFieldErrors(null);
    setDispatchApiError(null);
    dispatchKeyRef.current = null;
  };

  const openDispatchDialog = (direction: "charge" | "discharge"): void => {
    setDispatchWatts("");
    setDispatchMinutes("5");
    setSelectedUnitIds(armedUnits.map((unit) => unit.unit_id));
    setFieldErrors(null);
    setDispatchApiError(null);
    dispatchKeyRef.current = null;
    setDialog({ kind: "dispatch", direction });
  };

  // --- mutations -----------------------------------------------------------------

  const submitArm = (): void => {
    const unitIds = armableUnits.map((unit) => unit.unit_id);
    client
      .postArm(unitIds)
      .then((result: Record<string, unknown>) => {
        setMutationRows(asMutationRows(result));
        closeDialog();
      })
      .catch((error: unknown) => {
        setArmError(toApiClientError(error));
      });
  };

  const submitDisarm = (): void => {
    const unitIds = armedUnits.map((unit) => unit.unit_id);
    client
      .postDisarm(unitIds)
      .then((result: Record<string, unknown>) => {
        setMutationRows(asMutationRows(result));
        closeDialog();
      })
      .catch((error: unknown) => {
        setArmError(toApiClientError(error));
      });
  };

  const submitDispatch = (): void => {
    if (dialog?.kind !== "dispatch") return;
    const direction = dialog.direction;
    const wattsValue = Number(dispatchWatts.trim());
    const minutesValue = Number(dispatchMinutes.trim());
    const errors: FieldErrors = { watts: null, minutes: null };
    if (!Number.isFinite(wattsValue) || wattsValue <= 0) {
      errors.watts = "Enter a positive number of watts (greater than 0).";
    }
    if (!Number.isFinite(minutesValue) || minutesValue <= 0) {
      errors.minutes = "Enter a duration of at least 1 minute.";
    } else if (minutesValue > MAX_DURATION_MINUTES) {
      errors.minutes = `Duration is too long: the bound is ${MAX_DURATION_MINUTES} minutes (${MAX_DURATION_MINUTES * 60} s).`;
    }
    if (errors.watts !== null || errors.minutes !== null) {
      setFieldErrors(errors);
      setDispatchApiError(null);
      return;
    }
    setFieldErrors(null);
    if (selectedUnitIds.length === 0) {
      setDispatchApiError({
        error: new ApiClientError({
          code: "no_units_selected",
          message: "Select at least one unit before dispatching.",
          details: null,
          request_id: "",
          status: 0,
        }),
        field: null,
      });
      return;
    }
    const body: Record<string, unknown> = {
      unit_ids: [...selectedUnitIds],
      direction,
      watts: wattsValue,
      ttl_s: Math.round(minutesValue * 60),
    };
    // One caller-supplied idempotency key per operator action: a retry of the
    // same confirm reuses it so the service replays the action instead of
    // dispatching twice (UI_CONTRACTS.md "Data sources").
    const key = dispatchKeyRef.current ?? newDispatchKey();
    dispatchKeyRef.current = key;
    client
      .postIntent(body, key)
      .then((result: Record<string, unknown>) => {
        const outcome = asDispatchOutcome(result);
        if (outcome !== null) {
          setDispatchOutcome(outcome);
          if (outcome.expiresInSeconds !== null) setExpirySeconds(outcome.expiresInSeconds);
        }
        closeDialog();
      })
      .catch((error: unknown) => {
        const apiError = toApiClientError(error);
        setDispatchApiError({ error: apiError, field: apiErrorField(apiError) });
      });
  };

  const submitStop = (): void => {
    const unitIds = units.map((unit) => unit.unit_id);
    client
      .postEmergencyStop(unitIds, "operator requested from the console")
      .then((result: Record<string, unknown>) => {
        const outcome = asStopOutcome(result);
        if (outcome !== null) {
          setStopOutcome(outcome);
          setStopAckInput("");
          setStopAckOutcome(null);
          setStopAckError(null);
        }
        closeDialog();
      })
      .catch((error: unknown) => {
        setStopDialogError(toApiClientError(error));
      });
  };

  const submitStopAcknowledgement = (): void => {
    if (stopOutcome === null || stopOutcome.stopId === null) return;
    client
      .postStopAcknowledgement(stopOutcome.stopId)
      .then((result: Record<string, unknown>) => {
        setStopAckOutcome(asAcknowledgedStatus(result) ?? "acknowledged");
        setStopAckError(null);
      })
      .catch((error: unknown) => {
        setStopAckError(toApiClientError(error));
      });
  };

  const submitInhibitAcknowledgement = (unitId: string): void => {
    client
      .postInhibitAcknowledgement(unitId)
      .then((result: Record<string, unknown>) => {
        const outcome = asInhibitOutcome(result);
        if (outcome !== null) setInhibitOutcome(outcome);
        closeDialog();
      })
      .catch((error: unknown) => {
        setArmError(toApiClientError(error));
      });
  };

  // --- dispatch preview ----------------------------------------------------------

  const wattsNumber = Number(dispatchWatts.trim());
  const minutesNumber = Number(dispatchMinutes.trim());
  const wattsValid = dispatchWatts.trim() !== "" && Number.isFinite(wattsNumber) && wattsNumber > 0;
  const minutesValid =
    dispatchMinutes.trim() !== "" && Number.isFinite(minutesNumber) && minutesNumber > 0;
  const dispatchDirection =
    dialog?.kind === "dispatch"
      ? displayDirection(dialog.direction)
      : dialog?.kind === "inhibit"
        ? "Inhibit"
        : "Control";
  const previewUnits = selectedUnitIds.length > 0 ? selectedUnitIds.join(", ") : "no units";
  const previewWatts = wattsValid ? formatWatts(wattsNumber) : "the watts you set";
  const previewDuration = minutesValid
    ? `${dispatchMinutes.trim()} min`
    : "the duration you set";
  const previewSentence = `${dispatchDirection} ${previewUnits} at ${previewWatts} for ${previewDuration}, subject to the site power limit, expiring ${previewDuration} after acceptance.`;

  const wattsDescribedBy = [
    "now-dispatch-watts-hint",
    fieldErrors !== null && fieldErrors.watts !== null ? "now-dispatch-watts-error" : null,
    dispatchApiError !== null && dispatchApiError.field === "watts" ? "now-dispatch-watts-api" : null,
  ]
    .filter((id): id is string => id !== null)
    .join(" ");
  const minutesDescribedBy = [
    "now-dispatch-minutes-hint",
    fieldErrors !== null && fieldErrors.minutes !== null ? "now-dispatch-minutes-error" : null,
    dispatchApiError !== null && dispatchApiError.field === "minutes"
      ? "now-dispatch-minutes-api"
      : null,
  ]
    .filter((id): id is string => id !== null)
    .join(" ");

  // --- render ----------------------------------------------------------------------

  return (
    <section className="now-view" aria-labelledby="now-current-request-heading">
      <h2 id="now-current-request-heading">Current request</h2>

      {snapshot === null && loadFailed !== null ? (
        <div role="alert" className="load-error">
          <ApiErrorBlock id="now-snapshot-error" error={loadFailed} />
          <button
            type="button"
            onClick={() => {
              setSnapshotNonce((nonce) => nonce + 1);
            }}
          >
            Retry
          </button>
        </div>
      ) : snapshot === null ? (
        <p role="status" className="loading">
          Loading the current pod state…
        </p>
      ) : snapshot.units.length === 0 ? (
        <p className="empty-note">
          No units are connected yet. Add a unit first to arm and dispatch.
        </p>
      ) : (
        <>
          <div className={connection === "lost" ? "facts facts-dimmed" : "facts"}>
            <div role="group" aria-label="Requested">
              <p className="fact-label">Requested</p>
              <p className="fact-value">{requestedText}</p>
            </div>
            <div role="group" aria-label="Allowed">
              <p className="fact-label">Allowed</p>
              <p className="fact-value">{allowedText}</p>
            </div>
            <div role="group" aria-label="Actual">
              <p className="fact-label">Actual</p>
              <p className="fact-value">{actualText}</p>
            </div>
            <div role="group" aria-label="Remaining time">
              <p className="fact-label">Remaining time</p>
              <p className="fact-value">{remainingText}</p>
            </div>
          </div>

          {connection === "lost" ? (
            <p role="alert" className="disconnect-note">
              Connection lost. The last known values are still shown; reconnecting automatically.
            </p>
          ) : null}

          {units
            .filter((unit) => unit.quality !== "good")
            .map((unit) => (
              <p key={unit.unit_id} className="quality-note">
                Quality: {unit.unit_id} {unit.quality}
              </p>
            ))}

          {inhibitedUnits.map((unit) => (
            <button
              key={unit.unit_id}
              type="button"
              onClick={() => {
                setDialog({ kind: "inhibit", unitId: unit.unit_id });
              }}
            >
              Acknowledge {unit.unit_id}
            </button>
          ))}

          <div className="now-controls">
            {armableUnits.length > 0 ? (
              <button type="button" onClick={() => setDialog({ kind: "arm" })}>
                Arm
              </button>
            ) : null}
            {armedUnits.length > 0 ? (
              <button type="button" onClick={() => setDialog({ kind: "disarm" })}>
                Disarm
              </button>
            ) : null}
            {armedUnits.length > 0 ? (
              <button type="button" onClick={() => openDispatchDialog("charge")}>
                Charge
              </button>
            ) : null}
            {armedUnits.length > 0 ? (
              <button type="button" onClick={() => openDispatchDialog("discharge")}>
                Discharge
              </button>
            ) : null}
            {units.length > 0 ? (
              <button type="button" onClick={() => setDialog({ kind: "stop" })}>
                Emergency stop
              </button>
            ) : null}
          </div>
        </>
      )}

      {snapshot !== null && loadFailed !== null ? (
        <div className="refetch-error">
          <ApiErrorBlock id="now-refetch-error" error={loadFailed} />
          <button
            type="button"
            onClick={() => {
              setSnapshotNonce((nonce) => nonce + 1);
            }}
          >
            Retry
          </button>
        </div>
      ) : null}

      {mutationRows !== null ? (
        <section aria-label="Control action outcome" className="outcome">
          <ul>
            {mutationRows.map((row) => (
              <li key={row.unitId}>
                {row.unitId}: <strong>{row.status}</strong>
                {row.reason !== null && row.reason !== row.status ? (
                  <>
                    {" "}
                    — <code>{row.reason}</code>
                  </>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {dispatchOutcome !== null ? (
        <section aria-label="Dispatch outcome" className="outcome">
          <p>
            <strong>{dispatchOutcome.status}</strong>
            {dispatchOutcome.intentId !== null ? <> — {dispatchOutcome.intentId}</> : null}
          </p>
        </section>
      ) : null}

      {inhibitOutcome !== null ? (
        <section aria-label="Inhibit acknowledgement outcome" className="outcome">
          <p>
            {inhibitOutcome.unitId}: <strong>{inhibitOutcome.status}</strong>
          </p>
          {inhibitOutcome.latchCleared ? <p>Latch cleared</p> : null}
        </section>
      ) : null}

      {stopOutcome !== null ? (
        <section aria-label="Emergency stop outcome" className="outcome">
          <p role="alert">
            Emergency stop {stopOutcome.stopId ?? "unknown"} {stopOutcome.status}
          </p>
          {stopOutcome.fencedGeneration !== null ? (
            <p>Fenced generation {stopOutcome.fencedGeneration}</p>
          ) : null}
          {stopOutcome.degraded.length > 0 ? (
            <p>
              Degraded: {stopOutcome.degraded.join(", ")}
            </p>
          ) : null}
          {stopAckOutcome === null ? (
            <div className="stop-ack">
              <label htmlFor="now-stop-ack-field">Stop id</label>
              <input
                id="now-stop-ack-field"
                type="text"
                value={stopAckInput}
                onChange={(event) => setStopAckInput(event.target.value)}
              />
              <button
                type="button"
                disabled={
                  stopOutcome.stopId === null || stopAckInput.trim() !== stopOutcome.stopId
                }
                onClick={submitStopAcknowledgement}
              >
                Acknowledge
              </button>
              {stopAckError !== null ? (
                <ApiErrorBlock id="now-stop-ack-error" error={stopAckError} />
              ) : null}
            </div>
          ) : (
            <p>
              <strong>{stopAckOutcome}</strong>
            </p>
          )}
        </section>
      ) : null}

      {/* --- dialogs --------------------------------------------------------- */}

      {dialog?.kind === "arm" ? (
        <Dialog title="Arm the pod" onDecline={closeDialog}>
          <h4>Readiness</h4>
          <ul>
            {armableUnits.map((unit) => (
              <li key={unit.unit_id}>
                {unit.unit_id}:{" "}
                {unit.quality === "good"
                  ? "qualified"
                  : `not qualified (quality ${unit.quality})`}
                ,{" "}
                {unit.lifecycle === "inhibited"
                  ? "inhibit latched"
                  : "latch clear"}
              </li>
            ))}
          </ul>
          <p className="arm-policy">
            Arming follows the site safety policy: each unit stays fenced to the site limit until
            an operator confirms.
          </p>
          <h4>Control readiness</h4>
          {controlReasons.length > 0 ? (
            <ul>
              {controlReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : (
            <p>Ready</p>
          )}
          {armError !== null ? <ApiErrorBlock id="now-arm-error" error={armError} /> : null}
          <div className="dialog-actions">
            <button type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button type="button" onClick={submitArm}>
              ARM
            </button>
          </div>
        </Dialog>
      ) : null}

      {dialog?.kind === "disarm" ? (
        <Dialog title="Disarm" onDecline={closeDialog}>
          <p>
            Disarm {armedUnits.map((unit) => unit.unit_id).join(", ")}? The units stop following
            requests.
          </p>
          {armError !== null ? <ApiErrorBlock id="now-disarm-error" error={armError} /> : null}
          <div className="dialog-actions">
            <button type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button type="button" onClick={submitDisarm}>
              Disarm
            </button>
          </div>
        </Dialog>
      ) : null}

      {dialog?.kind === "dispatch" ? (
        <Dialog
          title={dialog.direction === "charge" ? "Charge" : "Discharge"}
          onDecline={closeDialog}
        >
          <div className="dispatch-fields">
            <label htmlFor="now-dispatch-watts">Watts</label>
            <input
              id="now-dispatch-watts"
              type="text"
              inputMode="decimal"
              value={dispatchWatts}
              aria-describedby={wattsDescribedBy}
              onChange={(event) => setDispatchWatts(event.target.value)}
            />
            <p id="now-dispatch-watts-hint" className="field-hint">
              Positive watts (greater than 0).
            </p>
            {fieldErrors !== null && fieldErrors.watts !== null ? (
              <p id="now-dispatch-watts-error" className="field-error">
                {fieldErrors.watts}
              </p>
            ) : null}
            {dispatchApiError !== null && dispatchApiError.field === "watts" ? (
              <ApiErrorBlock
                id="now-dispatch-watts-api"
                error={dispatchApiError.error}
                messages={apiFieldMessages(dispatchApiError.error)}
              />
            ) : null}

            <label htmlFor="now-dispatch-minutes">Minutes</label>
            <input
              id="now-dispatch-minutes"
              type="text"
              inputMode="numeric"
              value={dispatchMinutes}
              aria-describedby={minutesDescribedBy}
              onChange={(event) => setDispatchMinutes(event.target.value)}
            />
            <p id="now-dispatch-minutes-hint" className="field-hint">
              Up to {MAX_DURATION_MINUTES} minutes ({MAX_DURATION_MINUTES * 60} s).
            </p>
            {fieldErrors !== null && fieldErrors.minutes !== null ? (
              <p id="now-dispatch-minutes-error" className="field-error">
                {fieldErrors.minutes}
              </p>
            ) : null}
            {dispatchApiError !== null && dispatchApiError.field === "minutes" ? (
              <ApiErrorBlock
                id="now-dispatch-minutes-api"
                error={dispatchApiError.error}
                messages={apiFieldMessages(dispatchApiError.error)}
              />
            ) : null}

            <fieldset>
              <legend>Units</legend>
              {armedUnits.map((unit) => (
                <label key={unit.unit_id}>
                  <input
                    type="checkbox"
                    checked={selectedUnitIds.includes(unit.unit_id)}
                    onChange={(event) => {
                      setSelectedUnitIds((previous) =>
                        event.target.checked
                          ? [...previous, unit.unit_id]
                          : previous.filter((unitId) => unitId !== unit.unit_id),
                      );
                    }}
                  />{" "}
                  {unit.unit_id}
                </label>
              ))}
            </fieldset>

            <p className="dispatch-preview">{previewSentence}</p>

            {dispatchApiError !== null && dispatchApiError.field === null ? (
              <ApiErrorBlock
                id="now-dispatch-api"
                error={dispatchApiError.error}
                messages={apiFieldMessages(dispatchApiError.error)}
              />
            ) : null}
          </div>
          <div className="dialog-actions">
            <button type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button type="button" onClick={submitDispatch}>
              Confirm
            </button>
          </div>
        </Dialog>
      ) : null}

      {dialog?.kind === "stop" ? (
        <Dialog title="Emergency stop" onDecline={closeDialog}>
          <p>
            Emergency stop {units.map((unit) => unit.unit_id).join(", ")}? This latches every unit
            and halts control.
          </p>
          {stopDialogError !== null ? (
            <ApiErrorBlock id="now-stop-error" error={stopDialogError} />
          ) : null}
          <div className="dialog-actions">
            <button type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button type="button" onClick={submitStop}>
              Confirm stop
            </button>
          </div>
        </Dialog>
      ) : null}

      {dialog?.kind === "inhibit" ? (
        <Dialog title="Acknowledge inhibit" onDecline={closeDialog}>
          <p>
            Acknowledging clears the inhibit latch on {dialog.unitId}. Arming {dialog.unitId} is a
            separate step and still needs its own confirmation.
          </p>
          {armError !== null ? <ApiErrorBlock id="now-inhibit-error" error={armError} /> : null}
          <div className="dialog-actions">
            <button type="button" onClick={closeDialog}>
              Cancel
            </button>
            <button type="button" onClick={() => submitInhibitAcknowledgement(dialog.unitId)}>
              Confirm
            </button>
          </div>
        </Dialog>
      ) : null}
    </section>
  );
}
