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
 *
 * Prop seam (web/src/app/views.ts `ShellViewProps`): the shell owns the
 * session, mints the client from the operator's token, and hands every mounted
 * view the session's shared client (coalesced reads, one fanned-out stream).
 * This view never builds its own client — a second client would open a second
 * events socket (one ticket per connection) and race the shell for frames.
 *
 * Live-frame truth (src/energypod/application/service.py `_publish`):
 * `unit.armed` / `unit.disarmed` carry `{units: [{unit_id, status, reason}]}`
 * where `status` is "armed"/"disarmed" on success and "refused" with the
 * refusal reason otherwise — a refused row is never a lifecycle change, so it
 * is rendered as a refusal and the unit's lifecycle is left untouched.
 * `emergency_stop.latched` carries `{stop_id, unit_ids, reason, generation,
 * degraded}` and fences the fleet to inhibited; `inhibit.acknowledged` clears
 * exactly one latch and changes no lifecycle (the unit re-qualifies through
 * stable samples). Both latch frames also trigger a snapshot refetch: the
 * event updates the picture immediately, and the refreshed snapshot — adopted
 * only when its sequence advances — is the world that wins.
 *
 * Engaged stops as state (2026-08-23 incident fix): the amended snapshot
 * contract adds `active_stops`, and while a stop is engaged this view renders
 * arm/disarm/charge/discharge DISABLED with the stop named — never silently
 * absent. A snapshot carrying the field is authoritative; while the backend
 * sends none, the latch frames hold the state instead (feature detection: an
 * absent field is "no information", an empty array is "nothing engaged").
 *
 * Freshness truth (src/energypod/api/rest.py + runtime/composition.py): the
 * service sends its snapshot frame exactly once per connection and then one
 * `observation.published` frame per telemetry append. The "…s ago" figure on
 * the Actual fact therefore ticks from each unit's latest observation once one
 * has been seen in this session (the healthy sawtooth); the captured
 * `telemetry_age_s` plus elapsed time is only the pre-observation fallback. A
 * resumed connection's first snapshot carrying a LOWER sequence than the
 * picture on screen is a controller restart that renumbered the sequence
 * space — adopted, never refused, so a restart cannot freeze this view.
 *
 * Dispatch semantics (2026-08-23 operator ruling — "I asked for each setting
 * to be one thousand, not a total of 1,000"): the dispatch form's watts field
 * is PER BATTERY, never a fleet total. The multiplication is shown live
 * before confirm ("1,000 W × 3 batteries selected = 3,000 W total"), the
 * confirm preview states both figures plainly ("Each battery: up to 1,000 W ·
 * Total: 3,000 W"), and the Requested fact derives the same per-battery
 * expectation for a multi-unit request. The SUBMITTED payload keeps the
 * backend contract exactly as it stands — the scalar fleet-total `watts`,
 * per-battery × selected count; the `watts_by_unit` extension lands
 * separately and is deliberately not pre-implemented here.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, Health } from "../../api/client";
import { formatSeconds, formatWatts } from "../../lib/format";
import "./now.css";

/** The one prop the shell hands every mounted view (views.ts ShellViewProps). */
export interface NowViewProps {
  client: ApiClient;
}

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

/**
 * One engaged emergency stop, exactly as the amended snapshot contract's
 * `active_stops` array carries it (`null` unit ids = fleet-wide).
 */
interface WireActiveStop {
  stop_id: string;
  latched_at: string;
  principal: string;
  reason_codes: string[];
  unit_ids: string[] | null;
}

interface WireSnapshot {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: WireUnit[];
  /**
   * The snapshot's engaged stops, or null while the backend sends no
   * `active_stops` at all (the feature detection: null never means "no stops
   * engaged", an empty array does).
   */
  active_stops: WireActiveStop[] | null;
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

/**
 * The `active_stops` array: null when the field is absent (today's backend —
 * feature detection), the narrowed stops when it is present.
 */
function asActiveStops(value: unknown): WireActiveStop[] | null {
  if (!Array.isArray(value)) return null;
  const stops: WireActiveStop[] = [];
  for (const entry of value) {
    if (!isRecord(entry) || typeof entry.stop_id !== "string" || entry.stop_id === "") continue;
    stops.push({
      stop_id: entry.stop_id,
      latched_at: typeof entry.latched_at === "string" ? entry.latched_at : "",
      principal: typeof entry.principal === "string" ? entry.principal : "",
      reason_codes: Array.isArray(entry.reason_codes)
        ? entry.reason_codes.filter((code): code is string => typeof code === "string")
        : [],
      unit_ids: Array.isArray(entry.unit_ids)
        ? entry.unit_ids.filter((id): id is string => typeof id === "string")
        : null,
    });
  }
  return stops;
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
    active_stops: asActiveStops(value.active_stops),
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
// monotonic time: ages and countdowns tick from captured markers, never freeze
// ---------------------------------------------------------------------------

/** A monotonic reading: ages must never run backwards when the wall clock jumps. */
function monotonicNowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

const TICK_MS = 1000;

/**
 * A ticking "now" so displayed ages advance after render. The interval runs
 * only while something on screen is derived from elapsed time and is always
 * cleared on unmount; without it a 300 s countdown would sit frozen at the
 * value captured when the frame arrived. A backgrounded tab gets its timers
 * throttled, so the clock is also re-read the moment the tab becomes visible
 * or focused again — the first render after coming back carries the true age.
 */
function useTickingNow(enabled: boolean): number {
  const [nowMs, setNowMs] = useState(monotonicNowMs);
  useEffect(() => {
    if (!enabled) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setNowMs(monotonicNowMs());
    }, TICK_MS);
    const refreshNow = (): void => {
      if (document.visibilityState === "visible") {
        setNowMs(monotonicNowMs());
      }
    };
    document.addEventListener("visibilitychange", refreshNow);
    window.addEventListener("focus", refreshNow);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshNow);
      window.removeEventListener("focus", refreshNow);
    };
  }, [enabled]);
  return nowMs;
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

/**
 * The Requested fact for the units carrying an active request.
 *
 * The wire carries the newest intent's SCALAR total in every covered unit's
 * `requested_power.watts` (service.py `_requested_power` projects the intent's
 * own figure, never a per-unit split), so a request covering several units
 * reads on the wire as the same total repeated once per unit — the exact
 * fleet-total reading the 2026-08-23 operator ruling rejected. Units carrying
 * the identical direction and watts figure are the units one intent covers:
 * for them the fact derives the per-battery expectation from the split —
 * total ÷ covered units, marked "≈" because the allocator's headroom
 * weighting can shift any unit's share — alongside the plain total. A
 * single-unit request renders exactly as before: direction and its own
 * figure, no derived phrasing.
 */
function requestedFactText(activeUnits: WireUnit[]): string {
  const groups: { direction: string; watts: number; count: number }[] = [];
  for (const unit of activeUnits) {
    const figure = unit.requested_power;
    const shared = groups.find(
      (group) => group.direction === figure.direction && group.watts === figure.watts,
    );
    if (shared === undefined) {
      groups.push({ direction: figure.direction, watts: figure.watts, count: 1 });
    } else {
      shared.count += 1;
    }
  }
  return groups
    .map((group) => {
      if (group.count === 1) {
        return `${displayDirection(group.direction)} · ${formatWatts(group.watts)}`;
      }
      return `${displayDirection(group.direction)} · ≈${formatWatts(group.watts / group.count)} per battery (${formatWatts(group.watts)} total)`;
    })
    .join("; ");
}

// Watt and second figures render through the shared display-precision module
// (src/lib/format.ts): locale-style grouping without ICU data, at most two
// decimals, integers as integers.

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

/**
 * The per-unit rows a `unit.armed` / `unit.disarmed` frame carries in
 * `payload.units` (same shape the arm/disarm endpoints return).
 */
function mutationRowsFromPayload(payload: Record<string, unknown> | null): MutationRow[] {
  if (payload === null || !Array.isArray(payload.units)) return [];
  const rows: MutationRow[] = [];
  for (const entry of payload.units) {
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

/** `payload.unit_ids` as the strings it is (service publishes sorted ids). */
function unitIdsFromPayload(payload: Record<string, unknown> | null): string[] {
  if (payload === null || !Array.isArray(payload.unit_ids)) return [];
  return payload.unit_ids.filter((entry): entry is string => typeof entry === "string");
}

/** A refusal that arrived over the stream: rendered as a refusal, never a lifecycle change. */
type LiveRefusal = { action: "arm" | "disarm"; rows: MutationRow[] };

/** An emergency stop another operator (or the system) latched, seen on the stream. */
type LatchedStop = {
  stopId: string | null;
  reason: string | null;
  degraded: string[];
  unitIds: string[];
};

type DispatchOutcome = { status: string; intentId: string | null; expiresInSeconds: number | null };

function asDispatchOutcome(value: unknown): DispatchOutcome | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  return {
    status: value.status,
    intentId: typeof value.intent_id === "string" ? value.intent_id : null,
    expiresInSeconds: typeof value.expires_in_s === "number" ? value.expires_in_s : null,
  };
}

/** A remaining-time countdown is a marker, not a value: it must run down. */
type ExpiryMarker = { remainingS: number; atMs: number };

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

/**
 * UI-side per-battery bound mirroring the site policy's per-unit static
 * dispatch cap (2500 W today). Shown as guidance on the field and enforced as
 * a pre-submit guard, so an over-cap request is named and refused here before
 * any POST — never silently rescaled. The wire carries no cap figure yet;
 * this constant stands in until the server exposes one.
 */
const PER_UNIT_WATTS_CAP_W = 2500;

const RECONNECT_DELAY_MS = 3000;

export function NowView({ client }: NowViewProps) {
  // --- data state -----------------------------------------------------------
  const [snapshot, setSnapshot] = useState<WireSnapshot | null>(null);
  const [loadFailed, setLoadFailed] = useState<ApiClientError | null>(null);
  const [snapshotNonce, setSnapshotNonce] = useState(0);
  const [healthNonce, setHealthNonce] = useState(0);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [health, setHealth] = useState<Health | null>(null);
  const [expiry, setExpiry] = useState<ExpiryMarker | null>(null);
  const [liveRefusal, setLiveRefusal] = useState<LiveRefusal | null>(null);
  const [latchedStop, setLatchedStop] = useState<LatchedStop | null>(null);
  /**
   * Engaged emergency stops as the bus announced them (`emergency_stop.latched`
   * sets one, `emergency_stop.acknowledged` retires it). A snapshot that
   * carries `active_stops` replaces this list wholesale; while the backend
   * sends no such field, the frames are the only live latch signal this view
   * has — the controls it holds are disabled and say why, never silently dead.
   */
  const [heldStops, setHeldStops] = useState<WireActiveStop[]>([]);
  /** A latch the bus just announced that the newest adopted picture predates. */
  const pendingStopIdRef = useRef<string | null>(null);
  const [revokedNotice, setRevokedNotice] = useState<string | null>(null);
  const [inhibitNotice, setInhibitNotice] = useState<string | null>(null);

  // The sequence of the picture currently on screen and the monotonic marker
  // it was captured at: the snapshot guard and the ticking ages both hang off
  // these.
  const pictureSequenceRef = useRef<number | null>(null);
  const capturedAtRef = useRef<number>(monotonicNowMs());
  /**
   * The monotonic moment each unit's latest `observation.published` landed.
   * The snapshot frame arrives once per connection, so these per-cycle frames
   * are what keeps the "…s ago" figure honest while the connection stays up:
   * each unit's age ticks from its own last observation, never from mount.
   */
  const [observations, setObservations] = useState<Record<string, number>>({});

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

  // --- snapshot adoption (guarded: only an advancing sequence may replace) ----

  /**
   * The single adoption path. A snapshot whose sequence does not advance the
   * picture on screen is never adopted: the shell's shared stream republishes
   * its latest snapshot frame to every newly mounted view, and a REST read can
   * resolve after a newer stream frame — neither may rewind a newer world. The
   * one sanctioned regression is a controller restart, which renumbers the
   * sequence space from zero: a resumed connection's first snapshot carrying a
   * lower sequence is the new world, not an old one.
   */
  const adoptSnapshot = useCallback(
    (wire: WireSnapshot, sequence: number | null, allowRenumber = false): boolean => {
      const incoming = sequence ?? wire.snapshot_sequence;
      const current = pictureSequenceRef.current;
      if (current !== null && incoming <= current && !(allowRenumber && incoming < current)) {
        return false;
      }
      pictureSequenceRef.current = incoming;
      capturedAtRef.current = monotonicNowMs();
      setSnapshot(wire);
      setLoadFailed(null);
      if (wire.active_stops !== null) {
        // A snapshot that carries the field is authoritative about engaged
        // stops — except when it predates a latch the bus just announced (a
        // read already in flight when the frame landed): that latch stands
        // until the read the frame triggered arrives and carries it.
        const pending = pendingStopIdRef.current;
        const carried =
          pending !== null && wire.active_stops.some((stop) => stop.stop_id === pending);
        const stopsFromSnapshot = wire.active_stops;
        setHeldStops((previous) => (pending !== null && !carried ? previous : stopsFromSnapshot));
      }
      return true;
    },
    [],
  );

  // --- REST snapshot (initial load + manual retry) ----------------------------

  useEffect(() => {
    let cancelled = false;
    client
      .getSnapshot()
      .then((value: unknown) => {
        if (cancelled) return;
        const wire = asSnapshot(value);
        if (wire !== null) {
          adoptSnapshot(wire, null);
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
  }, [client, snapshotNonce, adoptSnapshot]);

  // --- health (control readiness reasons are the live latch signal) -----------

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
  }, [client, healthNonce]);

  /**
   * A guarded fresh read. Without it, Allowed and Actual freeze at connect
   * time for the whole session while the indicator says "live": the snapshot
   * is re-read after every successful mutation and whenever the arm gate (the
   * one dialog whose answer is a safety question) is opened.
   */
  const refreshSnapshot = useCallback((): void => {
    setSnapshotNonce((nonce) => nonce + 1);
  }, []);

  /** The arm gate must read the current latch state, not the mount-time one. */
  const refreshReadiness = useCallback((): void => {
    setSnapshotNonce((nonce) => nonce + 1);
    setHealthNonce((nonce) => nonce + 1);
  }, []);

  // --- event stream: snapshot frame first, then events; resync refetches -------

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: number | undefined;
    let resyncing = false;
    let lastSequence: number | undefined;

    const refetchSnapshot = (): void => {
      client
        .getSnapshot()
        .then((value: unknown) => {
          if (cancelled) return;
          const wire = asSnapshot(value);
          if (wire !== null) adoptSnapshot(wire, null);
        })
        .catch((error: unknown) => {
          if (!cancelled) setLoadFailed(toApiClientError(error));
        });
    };

    /** Success rows move the lifecycle; refused rows are refusals, never moves. */
    const applyLifecycleRows = (armed: boolean, rows: MutationRow[]): void => {
      const successStatus = armed ? "armed" : "disarmed";
      const succeeded = rows.filter((row) => row.status === successStatus);
      const refused = rows.filter((row) => row.status !== successStatus);
      const changed = new Set(succeeded.map((row) => row.unitId));
      if (changed.size > 0) {
        const lifecycle = armed ? "armed_idle" : "disarmed";
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
      if (refused.length > 0) {
        // The frame's own outcome is the refusal with its reason: the unit is
        // NOT armed/disarmed, so its lifecycle on screen must not change.
        setLiveRefusal({ action: armed ? "arm" : "disarm", rows: refused });
      }
    };

    const applyEvent = (frame: Record<string, unknown>): void => {
      const type = typeof frame.type === "string" ? frame.type : "";
      const payload = isRecord(frame.payload) ? frame.payload : null;
      if (type === "intent.accepted" && payload !== null) {
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
          // The published payload carries no expiry; the accepted response's
          // expires_in_s is the countdown source. Kept defensive for a future
          // wire addition.
          if (typeof payload.expires_in_s === "number") {
            setExpiry({ remainingS: payload.expires_in_s, atMs: monotonicNowMs() });
          }
        }
      } else if (type === "unit.armed" || type === "unit.disarmed") {
        applyLifecycleRows(type === "unit.armed", mutationRowsFromPayload(payload));
      } else if (type === "emergency_stop.latched") {
        // The fleet is fenced: no unit may present as armed or dispatchable,
        // the revoked request is gone, and a fresh snapshot is on its way.
        const unitIds = unitIdsFromPayload(payload);
        const fenced = new Set(unitIds);
        if (fenced.size > 0) {
          setSnapshot((previous) =>
            previous === null
              ? previous
              : {
                  ...previous,
                  units: previous.units.map((unit) =>
                    fenced.has(unit.unit_id)
                      ? {
                          ...unit,
                          lifecycle: "inhibited",
                          requested_power: { direction: "idle", watts: 0 },
                          authorized_power: null,
                        }
                      : unit,
                  ),
                },
          );
        }
        setExpiry(null);
        const frameStopId =
          payload !== null && typeof payload.stop_id === "string" ? payload.stop_id : "";
        setLatchedStop({
          stopId: frameStopId === "" ? null : frameStopId,
          reason: payload !== null && typeof payload.reason === "string" ? payload.reason : null,
          degraded:
            payload !== null && Array.isArray(payload.degraded)
              ? payload.degraded.filter((entry): entry is string => typeof entry === "string")
              : [],
          unitIds,
        });
        // The same latch as a control-holding fact: arm/disarm/charge/discharge
        // are held with the stop named until it is acknowledged.
        if (frameStopId !== "") {
          pendingStopIdRef.current = frameStopId;
        }
        setHeldStops((previous) => [
          ...previous.filter((stop) => stop.stop_id !== frameStopId),
          {
            stop_id: frameStopId,
            latched_at:
              typeof frame.occurred_at === "string" ? frame.occurred_at : "",
            principal: payload !== null && typeof payload.principal === "string" ? payload.principal : "",
            reason_codes: [],
            unit_ids: unitIds,
          },
        ]);
        refetchSnapshot();
      } else if (type === "authorization.revoked") {
        // The runtime publishes this for every unit that still held authority
        // when a revocation ran (composition.py `_AsyncAuthorizationRepository`),
        // and revocation is routine — intent expiry, disarm, generation fences —
        // not only a latched inhibit. So the frame says "authority changed",
        // never "this unit latched": the view re-reads the snapshot and says
        // exactly that, without inventing a lifecycle the frame does not carry.
        // The request itself is over: the countdown must not linger on screen
        // as a ghost of an intent that no longer exists.
        setExpiry(null);
        const unitIds = unitIdsFromPayload(payload);
        const reason =
          payload !== null && typeof payload.reason === "string" && payload.reason !== ""
            ? payload.reason
            : "authorization revoked";
        if (unitIds.length > 0) {
          setRevokedNotice(
            `Authorization was held on ${unitIds.join(", ")} (${reason}) — re-reading the current pod state.`,
          );
        }
        refetchSnapshot();
      } else if (type === "intent.expired") {
        // Feature-detected: the backend publishes the end of a request this
        // way once its intent-lifecycle event lands. The request card must not
        // linger as a ghost: the countdown goes and the world is re-read.
        setExpiry(null);
        setRevokedNotice("The power request ended.");
        refetchSnapshot();
      } else if (type === "authorization.granted") {
        // Feature-detected: an authority grant publishes nothing today (the
        // grant is only visible in the next snapshot); when the backend adds
        // the grant event this re-reads so Allowed/Actual move at the moment
        // of the grant.
        refetchSnapshot();
      } else if (type === "emergency_stop.acknowledged") {
        // Another operator (or the system) cleared the latch: the notice goes,
        // the held controls are released, and the refreshed snapshot decides
        // where the fleet stands now.
        setLatchedStop(null);
        const ackedStopId =
          payload !== null && typeof payload.stop_id === "string" ? payload.stop_id : "";
        if (ackedStopId !== "" && pendingStopIdRef.current === ackedStopId) {
          pendingStopIdRef.current = null;
        }
        setHeldStops((previous) => previous.filter((stop) => stop.stop_id !== ackedStopId));
        refetchSnapshot();
      } else if (type === "inhibit.acknowledged") {
        // Clearing the latch changes no lifecycle: the unit re-qualifies through
        // stable samples, so only the refreshed snapshot may move it.
        const unitId = payload !== null && typeof payload.unit_id === "string" ? payload.unit_id : null;
        if (unitId !== null) {
          setInhibitNotice(
            `Inhibit acknowledged on ${unitId} — the latch is clear; the pod re-qualifies through stable samples before it can be armed again.`,
          );
        }
        refetchSnapshot();
      }
    };

    const connect = (): void => {
      if (cancelled) return;
      // A connection opened with a cursor is a resume: its first snapshot may
      // carry a renumbered (lower) sequence after a controller restart. A
      // later frame on the SAME connection with a lower sequence is a stale
      // republish, never a restart — it stays refused.
      const resumed = lastSequence !== undefined;
      let firstSnapshot = true;
      const iterator = client.openEvents(lastSequence);
      void (async () => {
        try {
          for await (const frame of iterator) {
            if (cancelled) return;
            if (typeof frame.sequence === "number") {
              lastSequence = frame.sequence;
            }
            if (frame.type === "snapshot") {
              const wire = asSnapshot(frame.data);
              if (wire !== null) {
                adoptSnapshot(wire, wire.snapshot_sequence, resumed && firstSnapshot);
              }
              firstSnapshot = false;
              setConnection("live");
            } else if (frame.type === "observation.published") {
              // The per-cycle liveness frame: it carries no readings, but it
              // proves this unit just published, resetting that unit's
              // "…s ago" figure to now instead of climbing from mount time.
              const payload = isRecord(frame.payload) ? frame.payload : null;
              const unitId =
                payload !== null && typeof payload.unit_id === "string" ? payload.unit_id : null;
              if (unitId !== null) {
                const at = monotonicNowMs();
                setObservations((previous) =>
                  previous[unitId] === at ? previous : { ...previous, [unitId]: at },
                );
              }
              setConnection("live");
            } else if (frame.type === "resync_required") {
              // The pinned client ends iteration right after this marker; the
              // view resynchronizes from a fresh snapshot and reconnects with
              // the last seen sequence as the cursor.
              resyncing = true;
            } else if (
              typeof frame.sequence === "number" &&
              pictureSequenceRef.current !== null &&
              frame.sequence <= pictureSequenceRef.current
            ) {
              // Already part of the picture on screen (a republished frame from
              // the shared stream, or a replay behind the adopted sequence):
              // applying it again could rewind a newer world. With no picture
              // yet, every frame is new to this view.
            } else {
              applyEvent(frame as Record<string, unknown>);
              if (typeof frame.sequence === "number") {
                // The applied event is now part of the picture: a snapshot (or
                // replay) older than it may never replace what is on screen.
                pictureSequenceRef.current = Math.max(
                  pictureSequenceRef.current ?? frame.sequence,
                  frame.sequence,
                );
              }
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

  // Ages are captured values plus elapsed monotonic time: `telemetry_age_s` is
  // the age at capture, so it must keep growing after render or a 300 s
  // countdown (and every "x s ago") would sit frozen forever. Once a unit's
  // own observation.published frames have been seen, that unit's age ticks
  // from its latest observation instead — the healthy sawtooth, never a
  // number that climbs past minutes while the pod is publishing fine.
  const needsTick =
    (snapshot?.units.some((unit) => unit.telemetry_age_s !== null) ?? false) ||
    expiry !== null ||
    Object.keys(observations).length > 0;
  const nowMs = useTickingNow(needsTick);
  const elapsedSeconds = Math.max(0, (nowMs - capturedAtRef.current) / 1000);
  const displayedAge = (unit: WireUnit): number | null => {
    if (unit.telemetry_age_s === null) {
      return null;
    }
    const observedAt = observations[unit.unit_id];
    if (observedAt !== undefined) {
      return Math.max(0, Math.floor((nowMs - observedAt) / 1000));
    }
    return Math.floor(unit.telemetry_age_s + elapsedSeconds);
  };

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

  // The Requested fact derives the per-battery expectation for a multi-unit
  // request (see requestedFactText); "None" is the honest no-request state.
  const requestedText = activeUnits.length > 0 ? requestedFactText(activeUnits) : "None";
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
            const ageSeconds = displayedAge(unit);
            const age = ageSeconds !== null ? ` (${formatSeconds(ageSeconds)} ago)` : "";
            return `${formatWatts(unit.measured_watts)}${age}`;
          })
          .join("; ")
      : "No measurement available";
  const remainingSeconds =
    expiry === null
      ? null
      : Math.max(
          0,
          Math.ceil(expiry.remainingS - Math.max(0, (nowMs - expiry.atMs) / 1000)),
        );
  const remainingText =
    remainingSeconds !== null ? `${formatSeconds(remainingSeconds)} left` : "Not available";

  const controlReasons = health?.control_readiness.reasons ?? [];

  // --- the engaged-stop hold ---------------------------------------------------
  //
  // A snapshot that carries `active_stops` is authoritative; while the backend
  // sends no such field, the bus frames are. Either way the held controls are
  // rendered disabled WITH the stop named — never silently gone.

  const snapshotStops = snapshot?.active_stops ?? null;
  const heldStopsNow = snapshotStops ?? heldStops;
  const heldStop = heldStopsNow[0] ?? null;
  const heldFromSnapshot = snapshotStops !== null;
  const heldName =
    heldStop === null ? null : heldStop.stop_id === "" ? "an emergency stop" : heldStop.stop_id;
  const heldReason =
    heldName === null
      ? null
      : `Held by emergency stop ${heldName} — ${
          heldFromSnapshot
            ? "acknowledge on the banner to release"
            : "acknowledge the stop to release"
        }`;

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
    // The unit list this dialog offers is a live question: re-read the world
    // before asking the operator to confirm against it.
    refreshSnapshot();
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
        // The outcome rows speak for the request; the snapshot re-read speaks
        // for the world (authorized/measured/lifecycle) the request produced.
        refreshSnapshot();
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
        refreshSnapshot();
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
      errors.watts = "Enter a positive number of watts per battery (greater than 0).";
    } else if (wattsValue > PER_UNIT_WATTS_CAP_W) {
      errors.watts = `Watts per battery are too high: the bound is ${formatWatts(PER_UNIT_WATTS_CAP_W)} per battery.`;
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
    // The form is per battery; the submitted watts stays the backend contract
    // exactly as it stands — the scalar fleet-total figure the intent endpoint
    // defines. Per-battery × selected count IS that total; the `watts_by_unit`
    // extension lands separately and is not pre-implemented here.
    const body: Record<string, unknown> = {
      unit_ids: [...selectedUnitIds],
      direction,
      watts: wattsValue * selectedUnitIds.length,
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
          // The countdown is captured as a marker (remaining + monotonic now),
          // not as a frozen string: it must run down from here.
          if (outcome.expiresInSeconds !== null) {
            setExpiry({ remainingS: outcome.expiresInSeconds, atMs: monotonicNowMs() });
          }
        }
        closeDialog();
        // Allowed and Actual are the two facts the API alone can answer after a
        // dispatch; without this read they stay frozen at connect time.
        refreshSnapshot();
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
        refreshSnapshot();
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
        refreshSnapshot();
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
        // The latch is clear; the pod's lifecycle is the refreshed snapshot's
        // to say (it re-qualifies through stable samples, never instantly).
        refreshSnapshot();
      })
      .catch((error: unknown) => {
        setArmError(toApiClientError(error));
      });
  };

  // --- dispatch preview ----------------------------------------------------------
  //
  // The form is per battery (the 2026-08-23 operator ruling): the live math
  // line shows the multiplication as it is typed and as units are ticked, and
  // the confirm preview states both figures plainly — per battery and total —
  // so the submitted scalar total is never a silent surprise.

  const wattsNumber = Number(dispatchWatts.trim());
  const minutesNumber = Number(dispatchMinutes.trim());
  const wattsValid = dispatchWatts.trim() !== "" && Number.isFinite(wattsNumber) && wattsNumber > 0;
  const minutesValid =
    dispatchMinutes.trim() !== "" && Number.isFinite(minutesNumber) && minutesNumber > 0;
  const selectedCount = selectedUnitIds.length;
  const batteryWord = selectedCount === 1 ? "battery" : "batteries";
  /** The submitted scalar total: per-battery watts × selected count. */
  const totalWatts = wattsValid ? wattsNumber * selectedCount : null;
  const wattsMathText = wattsValid
    ? `${formatWatts(wattsNumber)} × ${selectedCount} ${batteryWord} selected = ${formatWatts(totalWatts ?? 0)} total`
    : `Enter the watts per battery to see the total for the ${selectedCount} ${batteryWord} selected.`;
  const dispatchDirection =
    dialog?.kind === "dispatch"
      ? displayDirection(dialog.direction)
      : dialog?.kind === "inhibit"
        ? "Inhibit"
        : "Control";
  const previewUnits = selectedCount > 0 ? selectedUnitIds.join(", ") : "no units";
  const previewPerBattery = wattsValid ? `up to ${formatWatts(wattsNumber)}` : "the watts you set";
  const previewTotal =
    totalWatts !== null ? formatWatts(totalWatts) : "the watts you set × the batteries you select";
  const previewDuration = minutesValid
    ? `${dispatchMinutes.trim()} min`
    : "the duration you set";
  const previewSentence = `${dispatchDirection} ${previewUnits}. Each battery: ${previewPerBattery} · Total: ${previewTotal}. For ${previewDuration}, subject to the site power limit, expiring ${previewDuration} after acceptance.`;

  const wattsDescribedBy = [
    "now-dispatch-watts-hint",
    "now-dispatch-watts-math",
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

          {latchedStop !== null ? (
            <div role="alert" className="latched-stop-note">
              <p>
                Emergency stop {latchedStop.stopId ?? "unknown"} latched on{" "}
                {latchedStop.unitIds.length > 0 ? latchedStop.unitIds.join(", ") : "the fleet"}
                {latchedStop.reason !== null && latchedStop.reason !== ""
                  ? ` — ${latchedStop.reason}`
                  : ""}
                . The fleet is inhibited until the stop is acknowledged.
              </p>
              {latchedStop.degraded.length > 0 ? (
                <p>Degraded: {latchedStop.degraded.join(", ")}</p>
              ) : null}
            </div>
          ) : null}

          {revokedNotice !== null ? (
            <p role="status" className="inhibit-note">
              {revokedNotice}
            </p>
          ) : null}

          {inhibitNotice !== null ? (
            <p role="status" className="inhibit-note">
              {inhibitNotice}
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
            {heldStop !== null ? (
              <>
                {/* While a stop holds the fleet, every control stays on screen
                    and says why it cannot be used — vanishing buttons are the
                    defect that left an operator with nothing to click. */}
                <p id="now-held-reason" role="status" className="held-reason">
                  {heldReason}
                </p>
                <button type="button" disabled aria-describedby="now-held-reason">
                  Arm
                </button>
                <button type="button" disabled aria-describedby="now-held-reason">
                  Disarm
                </button>
                <button type="button" disabled aria-describedby="now-held-reason">
                  Charge
                </button>
                <button type="button" disabled aria-describedby="now-held-reason">
                  Discharge
                </button>
              </>
            ) : (
              <>
                {armableUnits.length > 0 ? (
                  <button
                    type="button"
                    onClick={() => {
                      // The arm gate is a safety question answered by current state:
                      // refresh the snapshot and the readiness reasons before the
                      // checklist renders, so a latch that appeared mid-session is
                      // visible instead of green-lit from mount-time data.
                      refreshReadiness();
                      setDialog({ kind: "arm" });
                    }}
                  >
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
              </>
            )}
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

      {liveRefusal !== null ? (
        <section aria-label="Live control refusal" className="outcome outcome--refusal">
          <p>
            The {liveRefusal.action} request was refused for{" "}
            {liveRefusal.rows.map((row) => row.unitId).join(", ")}:
          </p>
          <ul>
            {liveRefusal.rows.map((row) => (
              <li key={row.unitId}>
                {row.unitId}: <strong>refused</strong>
                {row.reason !== null ? (
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
            {armableUnits.map((unit) => {
              // The latch and qualification lines come from the live control
              // readiness reasons ("<unit>:inhibit_latched",
              // "<unit>:not_qualified" — service.py `_control_readiness_reasons`),
              // not from a lifecycle predicate this list can never see: every
              // unit here is disarmed by construction, so "lifecycle ===
              // inhibited" would be a test no row could ever fail.
              const reasons = new Set(controlReasons);
              const latched =
                reasons.has(`${unit.unit_id}:inhibit_latched`) ||
                reasons.has(`${unit.unit_id}:inhibited`);
              const notQualified =
                reasons.has(`${unit.unit_id}:not_qualified`) ||
                reasons.has(`${unit.unit_id}:qualification_unknown`);
              const qualifiedText = notQualified
                ? `not qualified (${reasons.has(`${unit.unit_id}:not_qualified`) ? "not_qualified" : "qualification_unknown"})`
                : unit.quality === "good"
                  ? "qualified"
                  : `not qualified (quality ${unit.quality})`;
              return (
                <li key={unit.unit_id}>
                  {unit.unit_id}: {qualifiedText},{" "}
                  {latched ? "inhibit latched" : "latch clear"}
                </li>
              );
            })}
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
            <label htmlFor="now-dispatch-watts">Watts per battery</label>
            <input
              id="now-dispatch-watts"
              type="text"
              inputMode="decimal"
              value={dispatchWatts}
              aria-describedby={wattsDescribedBy}
              onChange={(event) => setDispatchWatts(event.target.value)}
            />
            <p id="now-dispatch-watts-hint" className="field-hint">
              Positive watts per battery (greater than 0), max{" "}
              {formatWatts(PER_UNIT_WATTS_CAP_W)} per battery.
            </p>
            {/* The live math: per-battery × selected count, re-derived as the
                operator types or ticks units — the submitted total is shown,
                never silent. */}
            <p id="now-dispatch-watts-math" className="field-hint">
              {wattsMathText}
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
