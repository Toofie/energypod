/**
 * Home view: answers the five household questions, in order
 * (UI_CONTRACTS.md, "Home").
 *
 * 1. Are we safe and connected?  — fleet badge + connection/service facts.
 * 2. What is powering the home?  — per-unit requested / allowed / actual.
 * 3. How full are the batteries? — fleet reserve + per-unit breakdown.
 * 4. What happens next?          — the active intent's direction and watts.
 * 5. Is anything limiting?       — health reasons + snapshot quality.
 *
 * Honesty pins the shapes below:
 *
 * - Wire enums arrive lowercase (`"disarmed"`, `"charge"`, `"good"` ...);
 *   labels shown to humans are capitalized.
 * - Requested, allowed, and actual are three separately labeled figures, each
 *   binding its own magnitude, so a limited action is never presented as
 *   delivered.
 * - Fleet reserve derives from the snapshot units' own `telemetry.soc_pct`
 *   (API_CONTRACTS.md "Application service facade"): the fleet figure is the
 *   average of the pods reporting a charge reading — pinned choice, computed
 *   over reporting pods only — and each breakdown row names its own SOC. A
 *   pod without a reading says "not available"; nothing is ever zero-filled
 *   into a percentage.
 * - No pinned wire source carries intent expiry, so the next-action region
 *   shows direction and watts only.
 * - Missing telemetry is named ("not available"), never zero-filled.
 * - Stale = `telemetry_age_s` past the freshness bound (the service never
 *   emits a "stale" quality); the age is shown next to the value and the value
 *   stays visible.
 * - The event stream reconnects with the last seen sequence as cursor, and a
 *   `resync_required` frame triggers exactly one snapshot refetch plus a
 *   reconnect from the frame's recovery cursor. A snapshot whose sequence does
 *   not advance the picture on screen is never adopted (the shell's shared
 *   stream republishes its latest snapshot frame to every newly mounted view).
 * - Live frames follow the service's published vocabulary exactly
 *   (service.py `_publish`): `unit.armed` / `unit.disarmed` rows carry
 *   `{unit_id, status, reason}`, and a refused row is rendered as a refusal —
 *   never as a lifecycle change; `emergency_stop.latched` inhibits the fleet;
 *   `inhibit.acknowledged` clears one latch and changes no lifecycle (the pod
 *   re-qualifies through stable samples), with a snapshot refetch following
 *   both latch frames.
 * - Displayed ages are the captured `telemetry_age_s` plus elapsed monotonic
 *   time: an age that never grows is a frozen reading, not a current one.
 *   The service sends its snapshot frame exactly once per connection
 *   (rest.py), so the per-cycle liveness the operator should see comes from
 *   `observation.published` frames (composition.py publishes one per telemetry
 *   append): each observation for a unit resets that unit's displayed age to
 *   the moment the reading landed (the healthy sawtooth), and the captured
 *   `telemetry_age_s` plus elapsed time is only the fallback used until the
 *   first observation for that unit arrives in this session. A resumed
 *   connection's first snapshot whose sequence is LOWER than the picture on
 *   screen is a controller restart that renumbered the sequence space from
 *   zero — it is adopted, never refused, so a restart cannot leave the
 *   pre-restart picture frozen on screen.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, Health, StreamEvent } from "../../api/client";
import { formatMillivolts, formatPercent, formatSeconds, formatWatts } from "../../lib/format";
import "./home.css";

export interface HomeViewProps {
  client: ApiClient;
}

type ConnectionState = "connecting" | "live" | "disconnected";
type Phase = "loading" | "ready" | "error";

/** Past this age a reading is shown as stale (age next to the value). */
const FRESHNESS_BOUND_S = 60;
const RECONNECT_BASE_DELAY_MS = 400;
const RECONNECT_MAX_DELAY_MS = 5000;
const AGE_TICK_MS = 1000;
/**
 * Past this long without any fresh reading (no observation, no new snapshot)
 * while the connection claims to be live, the picture is flagged stale: a
 * climbing age must be unmistakable from the healthy sawtooth.
 */
const FRESH_DATA_BOUND_S = 10;

/** A monotonic reading: displayed ages must never run backwards. */
function monotonicNowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/**
 * A ticking "now", mounted only while an on-screen value depends on elapsed
 * time. A backgrounded tab gets its timers throttled by the browser, so the
 * tick also re-reads the clock the moment the tab becomes visible or focused
 * again: the first render the operator sees after coming back carries the true
 * age, never the frozen number the throttled interval last painted.
 */
function useTickingNow(enabled: boolean): number {
  const [nowMs, setNowMs] = useState(monotonicNowMs);
  useEffect(() => {
    if (!enabled) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setNowMs(monotonicNowMs());
    }, AGE_TICK_MS);
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

// --- honest view-model over the wire ---------------------------------------

interface PowerFigureView {
  direction: "charge" | "discharge" | "idle";
  watts: number;
}

/** The snapshot's nullable telemetry block — the reserve question needs SOC;
 * the limiting question needs the cell spread for the imbalance warning. */
interface TelemetrySummaryView {
  socPct: number | null;
  cellSpreadMv: number | null;
}

interface UnitView {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: PowerFigureView;
  authorized_power: PowerFigureView | null;
  measured_watts: number | null;
  telemetry: TelemetrySummaryView | null;
}

interface SnapshotView {
  snapshot_sequence: number;
  units: UnitView[];
}

function readPowerFigure(value: unknown): PowerFigureView | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  const direction = String(record.direction ?? "idle").toLowerCase();
  const watts =
    typeof record.watts === "number" && Number.isFinite(record.watts) ? record.watts : 0;
  return {
    direction: direction === "charge" || direction === "discharge" ? direction : "idle",
    watts,
  };
}

/** The telemetry block's own fields, read defensively; absent stays null. */
function readTelemetrySoc(value: unknown): TelemetrySummaryView | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  const soc = record.soc_pct;
  const spread = record.cell_spread_mv;
  return {
    socPct:
      typeof soc === "number" && Number.isFinite(soc) && soc >= 0 && soc <= 100 ? soc : null,
    cellSpreadMv: typeof spread === "number" && Number.isFinite(spread) ? spread : null,
  };
}

function readUnit(value: unknown): UnitView | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.unit_id !== "string" || record.unit_id === "") {
    return null;
  }
  return {
    unit_id: record.unit_id,
    lifecycle: String(record.lifecycle ?? "").toLowerCase(),
    telemetry_age_s: typeof record.telemetry_age_s === "number" ? record.telemetry_age_s : null,
    quality: String(record.quality ?? "").toLowerCase(),
    requested_power: readPowerFigure(record.requested_power) ?? { direction: "idle", watts: 0 },
    authorized_power: readPowerFigure(record.authorized_power),
    measured_watts: typeof record.measured_watts === "number" ? record.measured_watts : null,
    telemetry: readTelemetrySoc(record.telemetry),
  };
}

function readSnapshot(value: unknown): SnapshotView | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (!Array.isArray(record.units)) {
    return null;
  }
  const units: UnitView[] = [];
  for (const entry of record.units) {
    const unit = readUnit(entry);
    if (unit !== null) {
      units.push(unit);
    }
  }
  return {
    snapshot_sequence: typeof record.snapshot_sequence === "number" ? record.snapshot_sequence : 0,
    units,
  };
}

// --- labels ----------------------------------------------------------------

const BADGE_LABELS: Record<string, string> = {
  observe_only: "Observe only",
  disarmed: "Disarmed",
  armed_idle: "Armed",
  active: "Active",
  inhibited: "Inhibited",
  boot: "Starting",
  stopping: "Stopping",
  disconnected: "Offline",
};

function isLimited(unit: UnitView): boolean {
  if (unit.lifecycle !== "active" || unit.authorized_power === null) {
    return false;
  }
  return unit.authorized_power.watts < unit.requested_power.watts;
}

function unitBadgeLabel(unit: UnitView): string {
  if (isLimited(unit)) {
    return "Limited";
  }
  return BADGE_LABELS[unit.lifecycle] ?? "Unknown";
}

/**
 * The most conservative state wins: observe-only (no control possible at all),
 * then a latched inhibit, then a limited action, then the rest.
 */
function fleetBadgeLabel(units: UnitView[]): string {
  if (units.length === 0) {
    return "No pods yet";
  }
  const lifecycles = new Set(units.map((unit) => unit.lifecycle));
  if (lifecycles.has("observe_only")) {
    return "Observe only";
  }
  if (lifecycles.has("inhibited")) {
    return "Inhibited";
  }
  if (units.some(isLimited)) {
    return "Limited";
  }
  if (lifecycles.has("active")) {
    return "Active";
  }
  if (lifecycles.has("armed_idle")) {
    return "Armed";
  }
  if (lifecycles.has("disarmed")) {
    return "Disarmed";
  }
  return "Starting";
}

function badgeKey(label: string): string {
  const keys: Record<string, string> = {
    "Observe only": "observe-only",
    Disarmed: "disarmed",
    Armed: "armed",
    Active: "active",
    Limited: "limited",
    Inhibited: "inhibited",
  };
  return keys[label] ?? "none";
}

function directionWord(direction: PowerFigureView["direction"]): string {
  if (direction === "charge") {
    return "Charging";
  }
  if (direction === "discharge") {
    return "Discharging";
  }
  return "Idle";
}

/** Stale is an age, not a wire value: the age ticks, so staleness can arrive with time alone. */
function isStale(ageSeconds: number | null): boolean {
  return ageSeconds !== null && ageSeconds > FRESHNESS_BOUND_S;
}

function dataAgeText(ageSeconds: number | null, updatesPaused: boolean): string {
  if (ageSeconds === null) {
    return "Data age: not available (telemetry missing)";
  }
  if (ageSeconds > FRESHNESS_BOUND_S) {
    return `Data age: ${formatSeconds(ageSeconds)} old — this reading is stale`;
  }
  if (updatesPaused) {
    return `Data age: ${formatSeconds(ageSeconds)} old — updates have paused; this reading may be stale`;
  }
  return `Data age: ${formatSeconds(ageSeconds)}`;
}

/**
 * Fleet reserve, pinned derivation: the average of the pods that ARE
 * reporting a charge reading (`telemetry.soc_pct`). Pods without a reading
 * never contribute — a silent pod is not an empty battery — and a fleet with
 * no readings at all says so instead of inventing a percentage.
 */
function fleetReserveText(units: UnitView[]): string {
  const readings = units
    .map((unit) => unit.telemetry?.socPct ?? null)
    .filter((soc): soc is number => soc !== null);
  if (readings.length === 0) {
    return "not available — no pod is reporting a charge reading yet";
  }
  const average = readings.reduce((total, soc) => total + soc, 0) / readings.length;
  const scope =
    readings.length === units.length
      ? "on average across all pods"
      : `on average across the ${readings.length} pod${
          readings.length === 1 ? "" : "s"
        } reporting a charge reading`;
  return `${formatPercent(average)} ${scope}`;
}

/** One pod's own charge figure, or the named gap. */
function unitReserveText(unit: UnitView): string {
  const soc = unit.telemetry?.socPct ?? null;
  return soc === null ? "charge level not available" : `${formatPercent(soc)} charged`;
}

function allowedText(unit: UnitView): string {
  if (unit.authorized_power !== null) {
    const authorized = unit.authorized_power;
    return `${directionWord(authorized.direction)} ${formatWatts(authorized.watts)}`;
  }
  if (unit.requested_power.direction === "idle" && unit.requested_power.watts === 0) {
    return "Not needed while idle";
  }
  return "Not available";
}

function connectionText(connection: ConnectionState): string {
  if (connection === "live") {
    return "Live updates connected — this picture is current.";
  }
  if (connection === "disconnected") {
    return "Connection lost — showing the last known readings with their age; reconnecting automatically.";
  }
  return "Connecting to live updates…";
}

function serviceText(health: Health | null): string {
  if (health === null) {
    return "Service health: not available yet.";
  }
  return health.liveness.ok ? "Service health: healthy." : "Service health: not reporting healthy.";
}

// --- limiting factors --------------------------------------------------------

interface LimitingFactor {
  key: string;
  raw: string;
  plain: string;
}

/** Plain language first; the raw reason code is revealed only on demand. */
function plainLanguage(raw: string): string {
  const separator = raw.indexOf(":");
  const unit = separator > 0 ? raw.slice(0, separator) : "";
  const code = separator > 0 ? raw.slice(separator + 1) : raw;
  let text: string;
  if (code.includes("inhibit")) {
    text = "is held by a safety latch and needs an acknowledgement before it can take part again.";
  } else if (code.includes("not_qualified")) {
    text = "is not qualified yet — its readiness checks have not passed, so it cannot be armed.";
  } else if (code === "no_unit_armed") {
    text = "No pod is armed yet, so none can act when the home needs power.";
  } else if (code.includes("cell_imbalance")) {
    text =
      "is flagged for cell imbalance — its cell voltages spread wider than the early-warning line, so a power request may be held back.";
  } else {
    text = "is being held back by the safety system.";
  }
  return unit === "" ? text : `${unit} ${text}`;
}

/**
 * The cell-imbalance early-warning line (2026-08-23): the live policy's
 * imbalance bound was operator-relaxed tenfold (0.050 V -> 0.500 V) so spread
 * alone no longer vetoes dispatch — the absolute per-cell voltage bounds
 * remain the real protection.  The console keeps the OLD 50 mV figure as the
 * warning line so the operator always sees the condition that used to block
 * (and still would, past the relaxed bound), per-unit, with the figure.
 */
const CELL_IMBALANCE_WARNING_MV = 50;

function collectFactors(units: UnitView[], health: Health | null): LimitingFactor[] {
  const factors: LimitingFactor[] = [];
  const seen = new Set<string>();
  const add = (raw: string, plain: string): void => {
    if (seen.has(raw)) {
      return;
    }
    seen.add(raw);
    factors.push({ key: raw, raw, plain });
  };
  if (health !== null) {
    const reasons = [...health.service_readiness.reasons, ...health.control_readiness.reasons];
    for (const reason of reasons) {
      if (typeof reason === "string" && reason !== "") {
        add(reason, plainLanguage(reason));
      }
    }
  }
  for (const unit of units) {
    if (unit.quality === "bad") {
      add(
        `${unit.unit_id}:telemetry_bad`,
        `${unit.unit_id} is sending poor-quality data, so its readings cannot be trusted right now.`,
      );
    } else if (unit.quality === "missing") {
      add(
        `${unit.unit_id}:telemetry_missing`,
        `${unit.unit_id} is not sending telemetry, so its state cannot be confirmed right now.`,
      );
    }
    const spread = unit.telemetry?.cellSpreadMv ?? null;
    if (spread !== null && spread > CELL_IMBALANCE_WARNING_MV) {
      add(
        `${unit.unit_id}:cell_imbalance`,
        `${unit.unit_id} cell imbalance warning: ${formatMillivolts(spread)} spread is above the ${CELL_IMBALANCE_WARNING_MV} mV early-warning line — not blocking dispatch on its own, but the pack needs attention.`,
      );
    }
  }
  return factors;
}

// --- error rendering ---------------------------------------------------------

function describeError(failure: unknown): { code: string; message: string } {
  if (failure instanceof ApiClientError) {
    return { code: failure.code, message: failure.message };
  }
  if (failure instanceof Error) {
    return { code: "unexpected_error", message: failure.message };
  }
  return { code: "unexpected_error", message: "An unexpected failure occurred." };
}

// --- component ---------------------------------------------------------------

export function HomeView({ client }: HomeViewProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [failure, setFailure] = useState<unknown>(null);
  const [snapshot, setSnapshot] = useState<SnapshotView | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [announcement, setAnnouncement] = useState("");
  const [urgentNotice, setUrgentNotice] = useState("");
  const [expandedFactors, setExpandedFactors] = useState<Record<string, boolean>>({});
  const [reloadNonce, setReloadNonce] = useState(0);
  /**
   * The monotonic moment each unit's latest `observation.published` landed.
   * The service snapshots once per connection, so these per-cycle frames are
   * the live freshness signal: a unit's displayed age sawtooths from its own
   * last observation instead of climbing from the connect-time snapshot.
   */
  const [observations, setObservations] = useState<Record<string, number>>({});
  const lastSequenceRef = useRef<number | undefined>(undefined);
  // The sequence of the picture on screen (adoption guard) and the monotonic
  // marker the displayed ages tick from.
  const adoptedSequenceRef = useRef<number | null>(null);
  const capturedAtRef = useRef<number>(monotonicNowMs());

  // Heading ids are created up front so hook order is stable across the
  // loading / error / ready branches below.
  const liveHeadingId = useId();
  const errorHeadingId = useId();
  const safetyHeadingId = useId();
  const powerHeadingId = useId();
  const reserveHeadingId = useId();
  const nextHeadingId = useId();
  const limitingHeadingId = useId();

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectAttempt = 0;
    setObservations({});

    /**
     * The single adoption path, guarded by sequence: a snapshot that does not
     * advance the picture on screen (the shell republishes its latest snapshot
     * frame to every newly mounted view; a REST read can resolve after a newer
     * stream frame) never replaces a newer world. The one sanctioned regression
     * is a controller restart, which renumbers the sequence space from zero: a
     * resumed connection's first snapshot whose sequence is lower than the
     * picture on screen is the new world, not an old one.
     */
    const applySnapshot = (
      value: unknown,
      sequence: number | null,
      allowRenumber = false,
    ): boolean => {
      const parsed = readSnapshot(value);
      if (parsed === null) {
        return false;
      }
      const incoming = sequence ?? parsed.snapshot_sequence;
      const current = adoptedSequenceRef.current;
      if (
        current !== null &&
        incoming <= current &&
        !(allowRenumber && incoming < current)
      ) {
        return false;
      }
      adoptedSequenceRef.current = incoming;
      capturedAtRef.current = monotonicNowMs();
      setSnapshot(parsed);
      lastSequenceRef.current = incoming;
      return true;
    };

    /** A fresher read after a world-moving frame: adopted only if it advances.
     * The read bypasses the shell's short-lived snapshot cache when the shared
     * client allows it — a cached picture must never answer a frame that just
     * proved the world moved. */
    const refetchSnapshot = (): void => {
      const read =
        typeof client.refreshSnapshot === "function"
          ? client.refreshSnapshot()
          : client.getSnapshot();
      read
        .then((value) => {
          if (!cancelled) {
            applySnapshot(value, null);
          }
        })
        .catch(() => {
          // The last known picture stays; the reconnect path retries anyway.
        });
    };

    /** The per-unit rows a unit.armed / unit.disarmed frame carries. */
    const outcomeRows = (frame: StreamEvent): { unitId: string; status: string; reason: string }[] => {
      const payload: unknown = frame.payload;
      const listed =
        payload !== null &&
        typeof payload === "object" &&
        Array.isArray((payload as Record<string, unknown>).units)
          ? ((payload as Record<string, unknown>).units as unknown[])
          : [];
      const rows: { unitId: string; status: string; reason: string }[] = [];
      for (const entry of listed) {
        if (
          entry !== null &&
          typeof entry === "object" &&
          typeof (entry as Record<string, unknown>).unit_id === "string" &&
          typeof (entry as Record<string, unknown>).status === "string"
        ) {
          const record = entry as Record<string, unknown>;
          rows.push({
            unitId: record.unit_id as string,
            status: record.status as string,
            reason: typeof record.reason === "string" ? record.reason : "",
          });
        }
      }
      return rows;
    };

    const patchLifecycles = (unitIds: string[], lifecycle: string): void => {
      setSnapshot((previous) => {
        if (previous === null) {
          return previous;
        }
        return {
          ...previous,
          units: previous.units.map((unit) =>
            unitIds.includes(unit.unit_id) ? { ...unit, lifecycle } : unit,
          ),
        };
      });
    };

    /** Patch the requested figure for the units an intent names. */
    const patchRequested = (
      unitIds: string[],
      direction: PowerFigureView["direction"],
      watts: number,
    ): void => {
      setSnapshot((previous) => {
        if (previous === null) {
          return previous;
        }
        return {
          ...previous,
          units: previous.units.map((unit) =>
            unitIds.includes(unit.unit_id)
              ? { ...unit, requested_power: { direction, watts } }
              : unit,
          ),
        };
      });
    };

    /** Status changes arriving over the socket reach non-visual operators. */
    const applyEventFrame = (frame: StreamEvent): void => {
      if (frame.type === "unit.armed" || frame.type === "unit.disarmed") {
        const armed = frame.type === "unit.armed";
        const successStatus = armed ? "armed" : "disarmed";
        const rows = outcomeRows(frame);
        const succeeded = rows.filter((row) => row.status === successStatus);
        const refused = rows.filter((row) => row.status !== successStatus);
        if (succeeded.length > 0) {
          patchLifecycles(
            succeeded.map((row) => row.unitId),
            armed ? "armed_idle" : "disarmed",
          );
          setAnnouncement(
            succeeded
              .map((row) => `${row.unitId} is now ${armed ? "armed" : "disarmed"}.`)
              .join(" "),
          );
        }
        if (refused.length > 0) {
          // A refused row is a refusal, never a lifecycle change: the pods stay
          // exactly as they are and the reason reaches the operator.
          setAnnouncement(
            refused
              .map(
                (row) =>
                  `${row.unitId} ${armed ? "arm" : "disarm"} was refused${
                    row.reason === "" ? "" : ` — ${row.reason}`
                  }.`,
              )
              .join(" "),
          );
        }
        return;
      }
      if (frame.type === "emergency_stop.latched") {
        // The latched fleet is inhibited: nothing may present as armed or
        // active until the stop is acknowledged and the pods re-qualify.
        const payload: unknown = frame.payload;
        const unitIds =
          payload !== null &&
          typeof payload === "object" &&
          Array.isArray((payload as Record<string, unknown>).unit_ids)
            ? ((payload as Record<string, unknown>).unit_ids as unknown[]).filter(
                (entry): entry is string => typeof entry === "string",
              )
            : [];
        const named = unitIds.length > 0 ? unitIds.join(", ") : "the fleet";
        if (unitIds.length > 0) {
          patchLifecycles(unitIds, "inhibited");
        }
        setUrgentNotice(
          `Emergency stop latched on ${named} — the pods are inhibited until the stop is acknowledged.`,
        );
        refetchSnapshot();
        return;
      }
      if (frame.type === "emergency_stop.acknowledged") {
        // The latch is gone: stop announcing it and let the refreshed snapshot
        // say where the fleet stands (the pods re-qualify, they do not resume).
        setUrgentNotice("");
        setAnnouncement("The emergency stop was acknowledged.");
        refetchSnapshot();
        return;
      }
      if (frame.type === "authorization.revoked") {
        // The runtime publishes this for every unit that still held authority,
        // and revocation is routine (intent expiry, disarm, generation fences)
        // — not only a latched inhibit. The frame says authority changed, so
        // the picture is re-read; no lifecycle is invented from it.
        refetchSnapshot();
        return;
      }
      if (frame.type === "intent.accepted") {
        // The request landed: the payload names it (direction, watts, units),
        // so the request figures render the moment the frame arrives — the
        // authorized figures follow with the next refreshed snapshot.
        const payload: unknown = frame.payload;
        const record = payload !== null && typeof payload === "object" ? payload as Record<string, unknown> : {};
        const unitIds = Array.isArray(record.unit_ids)
          ? record.unit_ids.filter((id): id is string => typeof id === "string")
          : [];
        const watts = typeof record.watts === "number" ? record.watts : null;
        const rawDirection = typeof record.direction === "string" ? record.direction : "";
        const direction: PowerFigureView["direction"] | null =
          rawDirection === "charge" || rawDirection === "discharge" || rawDirection === "idle"
            ? rawDirection
            : null;
        if (unitIds.length > 0 && watts !== null && direction !== null) {
          patchRequested(unitIds, direction, watts);
          setAnnouncement(
            `Power request accepted — ${direction} ${formatWatts(watts)} for ${unitIds.join(", ")}.`,
          );
        }
        refetchSnapshot();
        return;
      }
      if (frame.type === "intent.expired") {
        // Feature-detected: the backend publishes the end of a request this
        // way once its intent-lifecycle event lands. Today the same fact
        // arrives as a routine authorization.revoked; both re-read the world.
        setAnnouncement("The power request ended.");
        refetchSnapshot();
        return;
      }
      if (frame.type === "inhibit.acknowledged") {
        // Clearing the latch changes no lifecycle: the pod re-qualifies through
        // stable samples, so only a refreshed snapshot may move it.
        const payload: unknown = frame.payload;
        const unitId =
          payload !== null &&
          typeof payload === "object" &&
          typeof (payload as Record<string, unknown>).unit_id === "string"
            ? ((payload as Record<string, unknown>).unit_id as string)
            : null;
        if (unitId !== null) {
          setAnnouncement(
            `${unitId} inhibit acknowledged — the latch is clear; the pod re-qualifies through stable samples.`,
          );
        }
        refetchSnapshot();
      }
    };

    const onStreamLost = (): void => {
      if (cancelled) {
        return;
      }
      reconnectAttempt += 1;
      setConnection("disconnected");
      const delay = Math.min(
        RECONNECT_BASE_DELAY_MS * 2 ** (reconnectAttempt - 1),
        RECONNECT_MAX_DELAY_MS,
      );
      const cursor = lastSequenceRef.current;
      reconnectTimer = setTimeout(() => {
        void connect(cursor);
      }, delay);
    };

    async function connect(cursor: number | undefined): Promise<void> {
      if (cancelled) {
        return;
      }
      // A connection opened with a cursor is a resume: its first snapshot may
      // carry a renumbered (lower) sequence after a controller restart. A
      // later frame on the SAME connection with a lower sequence is a stale
      // republish, never a restart — it stays refused.
      const resumed = cursor !== undefined;
      let firstSnapshot = true;
      try {
        const stream = client.openEvents(cursor);
        for await (const frame of stream) {
          if (cancelled) {
            return;
          }
          if (frame.type === "resync_required") {
            // The discontinuity is data: refetch one snapshot, then reconnect
            // from the recovery cursor — never a replay from zero.
            const recovery =
              typeof frame.snapshot_sequence === "number"
                ? frame.snapshot_sequence
                : lastSequenceRef.current;
            try {
              const fresh = await client.getSnapshot();
              if (cancelled) {
                return;
              }
              applySnapshot(fresh, null);
              void connect(recovery);
            } catch {
              onStreamLost();
            }
            return;
          }
          if (frame.type === "observation.published") {
            // The per-cycle liveness frame (composition.py publishes one per
            // telemetry append): it carries no readings, but it proves this
            // unit's data just landed, so the unit's displayed age restarts
            // from now instead of climbing from the connect-time snapshot.
            const payload: unknown = frame.payload;
            const unitId =
              payload !== null &&
              typeof payload === "object" &&
              typeof (payload as Record<string, unknown>).unit_id === "string"
                ? ((payload as Record<string, unknown>).unit_id as string)
                : null;
            if (unitId !== null) {
              const at = monotonicNowMs();
              setObservations((previous) =>
                previous[unitId] === at ? previous : { ...previous, [unitId]: at },
              );
            }
            setConnection("live");
            if (typeof frame.sequence === "number") {
              lastSequenceRef.current = Math.max(
                lastSequenceRef.current ?? frame.sequence,
                frame.sequence,
              );
            }
            continue;
          }
          if (frame.type === "snapshot") {
            if (typeof frame.sequence === "number") {
              lastSequenceRef.current = frame.sequence;
            }
            applySnapshot(
              frame.data,
              typeof frame.sequence === "number" ? frame.sequence : null,
              resumed && firstSnapshot,
            );
            firstSnapshot = false;
            setConnection("live");
            continue;
          }
          setConnection("live");
          if (typeof frame.sequence === "number") {
            lastSequenceRef.current = frame.sequence;
          }
          if (
            typeof frame.sequence === "number" &&
            adoptedSequenceRef.current !== null &&
            frame.sequence <= adoptedSequenceRef.current
          ) {
            // Already part of the picture on screen (a republished frame from
            // the shared stream): applying it again could rewind a newer world.
            continue;
          }
          applyEventFrame(frame);
          if (typeof frame.sequence === "number") {
            adoptedSequenceRef.current = Math.max(
              adoptedSequenceRef.current ?? frame.sequence,
              frame.sequence,
            );
          }
        }
        // The iterator ended without an error: the stream went away.
        onStreamLost();
      } catch {
        // A transport failure: never leak its message; show the designed
        // disconnected notice and retry with the last seen sequence as cursor.
        onStreamLost();
      }
    }

    async function load(): Promise<void> {
      setPhase("loading");
      setConnection("connecting");
      const [snapshotResult, healthResult] = await Promise.allSettled([
        client.getSnapshot(),
        client.getHealth(),
      ]);
      if (cancelled) {
        return;
      }
      if (snapshotResult.status === "rejected") {
        setFailure(snapshotResult.reason);
        setPhase("error");
        return;
      }
      const parsed = readSnapshot(snapshotResult.value);
      if (parsed === null) {
        setFailure(new Error("The snapshot response was not readable."));
        setPhase("error");
        return;
      }
      applySnapshot(snapshotResult.value, null);
      setHealth(healthResult.status === "fulfilled" ? healthResult.value : null);
      setPhase("ready");
      void connect(parsed.snapshot_sequence);
    }

    void load();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
    };
  }, [client, reloadNonce]);

  const retry = useCallback(() => {
    setFailure(null);
    setSnapshot(null);
    setHealth(null);
    setConnection("connecting");
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  const toggleFactor = useCallback((key: string) => {
    setExpandedFactors((previous) => ({ ...previous, [key]: !(previous[key] ?? false) }));
  }, []);

  // Data ages are captured values plus elapsed monotonic time: without the
  // tick, every "Data age: N s" line would freeze at the value the snapshot
  // arrived with and read as current forever. Once a unit's own
  // observation.published frames have been seen, that unit's age ticks from
  // its latest observation instead (the healthy sawtooth).
  const needsAgeTick =
    (snapshot?.units.some((unit) => unit.telemetry_age_s !== null) ?? false) ||
    Object.keys(observations).length > 0;
  const nowMs = useTickingNow(needsAgeTick);
  const ageTickS = Math.max(0, (nowMs - capturedAtRef.current) / 1000);
  /** A unit's displayed data age: from its own last observation once seen. */
  const unitAgeSeconds = (unit: UnitView): number | null => {
    if (unit.telemetry_age_s === null) {
      return null;
    }
    const observedAt = observations[unit.unit_id];
    if (observedAt !== undefined) {
      return Math.max(0, Math.floor((nowMs - observedAt) / 1000));
    }
    return Math.floor(unit.telemetry_age_s + ageTickS);
  };
  // Staleness of the picture itself: while the connection claims to be live,
  // no fresh reading (no observation, no new snapshot) for the bound means the
  // numbers on screen may no longer be current — a climbing age must never
  // read as a healthy live view.
  const freshestObservationMs =
    Object.keys(observations).length > 0 ? Math.max(...Object.values(observations)) : null;
  const lastFreshDataMs = freshestObservationMs ?? capturedAtRef.current;
  const secondsSinceFreshData = (nowMs - lastFreshDataMs) / 1000;
  const dataStale = connection === "live" && secondsSinceFreshData > FRESH_DATA_BOUND_S;

  const liveRegion = (
    <p role="status" aria-live="polite" className="home-live-region">
      {announcement}
    </p>
  );

  // A lost connection while a pod is actively powering the home, and a latched
  // emergency stop, are the two home-side facts announced assertively; the
  // region always exists so screen readers (and the role query) see a stable
  // target.
  const urgent =
    snapshot !== null &&
    connection === "disconnected" &&
    snapshot.units.some((unit) => unit.lifecycle === "active");
  const urgentRegion = (
    <p role="alert" aria-live="assertive" className="home-alert-region">
      {urgent
        ? "Connection lost while a pod is actively powering the home — the last known readings are shown with their age and the live feed reconnects automatically."
        : urgentNotice}
    </p>
  );

  if (phase === "error") {
    const described = describeError(failure);
    return (
      <div className="home-view">
        {liveRegion}
        {urgentRegion}
        <section className="home-card home-card--error" aria-labelledby={errorHeadingId}>
          <h2 id={errorHeadingId}>The home view could not load</h2>
          <p className="home-error-detail">
            <code className="home-error-code">{described.code}</code>
            <span className="home-error-message">{described.message}</span>
          </p>
          <p className="home-error-note">
            Nothing about your pods is shown, because this request did not succeed. You can retry
            now.
          </p>
          <button type="button" className="home-retry" onClick={retry}>
            Retry
          </button>
        </section>
      </div>
    );
  }

  if (snapshot === null) {
    return (
      <div className="home-view">
        {liveRegion}
        {urgentRegion}
        <section className="home-card home-card--loading" aria-labelledby={liveHeadingId}>
          <h2 id={liveHeadingId}>Your EnergyPod at a glance</h2>
          <p role="status" className="home-loading-line">
            Loading your pods…
          </p>
          <p className="home-loading-hint">
            The five answers about your home will appear here as soon as the first snapshot
            arrives.
          </p>
        </section>
      </div>
    );
  }

  const units = snapshot.units;
  const badge = fleetBadgeLabel(units);
  const activeUnits = units.filter(
    (unit) => unit.lifecycle === "active" && unit.requested_power.direction !== "idle",
  );
  const nextAction = activeUnits[0] ?? null;
  const factors = collectFactors(units, health);
  const systemHealthy =
    health !== null &&
    health.liveness.ok &&
    health.service_readiness.ready &&
    health.control_readiness.ready;

  return (
    <div className="home-view" data-connection={connection}>
      {liveRegion}
      {urgentRegion}

      <section className="home-card" aria-labelledby={safetyHeadingId}>
        <h2 id={safetyHeadingId}>Are we safe and connected?</h2>
        <p className="home-fleet-line">
          Fleet status:{" "}
          <strong className={`home-badge home-badge--${badgeKey(badge)}`}>{badge}</strong>
        </p>
        <p role="status" className={`home-connection home-connection--${connection}`}>
          {connectionText(connection)}
        </p>
        {dataStale && (
          <p role="status" className="home-connection home-connection--stale">
            No fresh readings for {formatSeconds(secondsSinceFreshData)} — the numbers below may
            be out of date until updates return.
          </p>
        )}
        <p className="home-service-line">{serviceText(health)}</p>
      </section>

      <section className="home-card" aria-labelledby={powerHeadingId}>
        <h2 id={powerHeadingId}>What is powering the home?</h2>
        {units.length === 0 ? (
          <p className="home-empty">
            No batteries yet — what is powering the home will appear here once a pod connects.
          </p>
        ) : (
          <ul className="home-units">
            {units.map((unit) => (
              <UnitPowerEntry
                key={unit.unit_id}
                unit={unit}
                ageSeconds={unitAgeSeconds(unit)}
                updatesPaused={dataStale}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="home-card" aria-labelledby={reserveHeadingId}>
        <h2 id={reserveHeadingId}>How full are the batteries?</h2>
        {units.length === 0 ? (
          <p className="home-empty">
            No batteries yet. Each pod&apos;s charge level will appear here as soon as one
            connects — the first step is to connect or enrol a pod, or simply wait for it to check
            in.
          </p>
        ) : (
          <>
            <p className="home-reserve-total">
              Fleet charge level: {fleetReserveText(units)}.
            </p>
            <ul className="home-reserve-list" aria-label="Per-unit battery breakdown">
              {units.map((unit) => (
                <li
                  key={unit.unit_id}
                  className="home-reserve-item"
                  aria-label={`${unit.unit_id} charge level`}
                >
                  {unit.unit_id}: {unitReserveText(unit)}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="home-card" aria-labelledby={nextHeadingId}>
        <h2 id={nextHeadingId}>What happens next?</h2>
        {nextAction === null ? (
          <p className="home-next-none">
            No planned action — nothing is scheduled for the pods right now.
          </p>
        ) : (
          <p className="home-next-action">
            {directionWord(nextAction.requested_power.direction)} at{" "}
            {formatWatts(nextAction.requested_power.watts)} per pod (
            {activeUnits.map((unit) => unit.unit_id).join(", ")}).
          </p>
        )}
      </section>

      <section className="home-card" aria-labelledby={limitingHeadingId}>
        <h2 id={limitingHeadingId}>Is anything limiting operation?</h2>
        {factors.length === 0 ? (
          systemHealthy ? (
            <p className="home-limits-none">
              Nothing is limiting operation — the pods are operating normally.
            </p>
          ) : (
            <p className="home-limits-unknown">
              Operation limits are not available yet — the health of the system could not be read.
            </p>
          )
        ) : (
          <ul className="home-factors">
            {factors.map((factor) => (
              <LimitingFactorItem
                key={factor.key}
                factor={factor}
                expanded={expandedFactors[factor.key] ?? false}
                onToggle={toggleFactor}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/**
 * One unit's three separately labeled figures. Each magnitude lives inside its
 * own named figure, so the allowed amount can never be presented under the
 * requested label. The age line is the unit's own displayed data age, and it
 * renders in the stale style whenever the reading itself is past its freshness
 * bound OR no fresh reading has landed for the paused-updates bound.
 */
function UnitPowerEntry({
  unit,
  ageSeconds,
  updatesPaused,
}: {
  unit: UnitView;
  ageSeconds: number | null;
  updatesPaused: boolean;
}) {
  const limited = isLimited(unit);
  const badge = unitBadgeLabel(unit);
  const requested = unit.requested_power;
  const authorized = unit.authorized_power;
  const stale = isStale(ageSeconds) || updatesPaused;
  return (
    <li className="home-unit" aria-label={`${unit.unit_id} power`}>
      <div className="home-unit-head">
        <h3 className="home-unit-name">{unit.unit_id}</h3>
        <span className={`home-badge home-badge--${badgeKey(badge)}`}>{badge}</span>
      </div>
      <div className="home-figures">
        <div className="home-figure" role="figure" aria-label="Requested">
          <span className="home-figure-label">Requested</span>
          <span className="home-figure-value">
            {directionWord(requested.direction)} {formatWatts(requested.watts)}
          </span>
        </div>
        <div className="home-figure" role="figure" aria-label="Allowed">
          <span className="home-figure-label">Allowed</span>
          <span className="home-figure-value">{allowedText(unit)}</span>
        </div>
        <div className="home-figure" role="figure" aria-label="Actual">
          <span className="home-figure-label">Actual</span>
          <span className="home-figure-value" data-stale={stale ? "true" : undefined}>
            {unit.measured_watts === null ? "Not available" : formatWatts(unit.measured_watts)}
          </span>
        </div>
      </div>
      {limited && authorized !== null ? (
        <p className="home-limit-note">
          Limited to {formatWatts(authorized.watts)} — the safety system is holding back part of
          the request.
        </p>
      ) : null}
      <p className={stale ? "home-age home-age--stale" : "home-age"}>
        {dataAgeText(ageSeconds, updatesPaused)}
      </p>
    </li>
  );
}

/** Plain language first; the raw reason code appears only after expanding. */
function LimitingFactorItem({
  factor,
  expanded,
  onToggle,
}: {
  factor: LimitingFactor;
  expanded: boolean;
  onToggle: (key: string) => void;
}) {
  const detailId = useId();
  return (
    <li className="home-factor">
      <p className="home-factor-plain">{factor.plain}</p>
      <button
        type="button"
        className="home-factor-reveal"
        aria-expanded={expanded ? "true" : "false"}
        aria-controls={detailId}
        onClick={() => onToggle(factor.key)}
      >
        {expanded ? "Hide detail" : "Show detail"}
      </button>
      {expanded ? (
        <p className="home-factor-detail" id={detailId}>
          Raw code: <code className="home-factor-code">{factor.raw}</code>
        </p>
      ) : null}
    </li>
  );
}
