/**
 * Batteries view (docs/UI_CONTRACTS.md, "Batteries").
 *
 * One card per named unit, bound to unit ids — never to list position. Power
 * on a card is what was actually measured (sign carried by the direction
 * word, never a raw negative number); a clamped action is never presented as
 * the request. Fields the wire does not carry are named as missing with their
 * age and never zero-filled. A latched inhibit announces assertively and
 * acknowledges only through a confirmed dialog whose result clears from a
 * snapshot refetch — the server is the authority, never optimistic local
 * state.
 *
 * Wire truth this view is built on (src/energypod/application/service.py,
 * runtime/composition.py, api/rest.py — mirrored in web/src/test/wire.ts):
 *
 * - The snapshot unit carries exactly `unit_id, lifecycle, telemetry_age_s,
 *   quality, requested_power, authorized_power, measured_watts`. There is no
 *   charge, temperature, cell, or warning field anywhere on the snapshot, so
 *   those card fields render "No data" (named, with age) until the service
 *   exposes them — never an invented value.
 * - `quality` is the facade's own projection vocabulary
 *   `good | degraded | bad | missing`; "degraded" already encodes telemetry
 *   past the service's 30 s good bound, so staleness marking keys on it.
 * - The only observation event is `observation.published` with the minimal
 *   payload `{unit_id, connection_epoch, sequence}`: it proves the unit is
 *   publishing and names its telemetry sequence, and carries no readings.
 *   Per-unit observation state below is derived from exactly that.
 * - The audit trail (`GET /api/v1/audit`) is the per-unit event history: the
 *   kind key is `event_type`, reasons are `reason_codes`, and the outcome is
 *   `result` (web/src/test/wire.ts `WireAuditEvent`).
 * - `inhibit` on a unit is the API_CONTRACTS "Inhibit acknowledgement"
 *   facade exposure. It is read defensively: while the snapshot does not
 *   carry it, latch display and the acknowledge control derive from
 *   `lifecycle === "inhibited"` and the latch detail renders not-available —
 *   the server alone says whether a latch cleared.
 */
import "./BatteriesView.css";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type JSX,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { ApiClientError } from "../../api/client";
import type {
  ApiClient,
  AuditEvent,
  AuditPage,
  StreamEvent,
} from "../../api/client";

export type BatteriesConnection = "connected" | "disconnected";

export interface BatteriesViewProps {
  client: ApiClient;
  /**
   * The shell's connection fact for the shared data plane, when the shell
   * provides one. Defaults to "connected" so the view stands alone; the view
   * also detects the loss of its own stream subscription. Either signal is
   * enough to show the disconnected state — the last snapshot stays rendered.
   */
  connection?: BatteriesConnection;
}

// ---------------------------------------------------------------------------
// Wire shapes (facade serializers; enums are lowercase on the wire)
// ---------------------------------------------------------------------------

interface WirePower {
  direction: string;
  watts: number;
}

/** The documented latch exposure; absent while the snapshot does not send it. */
interface LatchState {
  cause_class: string;
  latched: boolean;
  reason_code: string | null;
}

interface ViewUnit {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: WirePower;
  authorized_power: WirePower | null;
  measured_watts: number | null;
  inhibit: LatchState | null;
}

interface FleetState {
  siteId: string;
  sequence: number;
  capturedAt: string;
  units: ViewUnit[];
}

/** What one `observation.published` frame proves about a unit. */
interface ObservationTrack {
  occurredAt: string;
  busSequence: number;
  telemetrySequence: number | null;
  connectionEpoch: number | null;
}

/** One audit entry projected for the Events tab (never fabricated). */
interface AuditEntryView {
  sequence: number;
  eventType: string | null;
  occurredAt: string | null;
  result: string | null;
  reasonCodes: string[];
  unitId: string | null;
}

interface ErrorView {
  code: string;
  message: string;
  request_id: string | null;
}

type Phase = "loading" | "ready" | "error";

type TabKey = "summary" | "cells" | "events" | "details";

const TABS: { key: TabKey; label: string }[] = [
  { key: "summary", label: "Summary" },
  { key: "cells", label: "Cells" },
  { key: "events", label: "Events" },
  { key: "details", label: "Details" },
];

/** Reconnect pause for the event stream: short enough that a dropped line is
 * seen retrying within a user's glance, never a tight spin. */
const RECONNECT_DELAY_MS = 300;

const AUDIT_LIMIT = 50;

/** The service's own snapshot good-telemetry bound (service.py
 * `_SNAPSHOT_GOOD_TELEMETRY_MAX_AGE_S`): telemetry older than this is never
 * labelled good, so the card marks it stale rather than re-deriving its own
 * freshness clock. */
const GOOD_TELEMETRY_MAX_AGE_S = 30;

// ---------------------------------------------------------------------------
// Plain-language mappings (words first, raw codes only on demand)
// ---------------------------------------------------------------------------

const LIFECYCLE_WORDS: Record<string, string> = {
  boot: "Starting up",
  disarmed: "Standby",
  armed: "Armed",
  armed_idle: "Armed and idle",
  observe_only: "Observe only",
  active: "Active",
  inhibited: "Inhibited",
  stopping: "Stopping",
  disconnected: "Disconnected",
};

/** Audit `event_type` values the service writes (see wire.ts AUDIT_EVENT_TYPES). */
const AUDIT_TYPE_WORDS: Record<string, string> = {
  control_decision: "Power decision",
  intent_accepted: "Dispatch request accepted",
  unit_armed: "Arm request",
  unit_disarmed: "Disarm request",
  emergency_stop: "Emergency stop",
  stop_acknowledged: "Stop acknowledgement",
  inhibit_acknowledged: "Inhibit acknowledgement",
  authorization_revoked: "Authorization revoked",
};

/** Audit `result` values (DecisionStatus plus the facade mutation results). */
const RESULT_WORDS: Record<string, string> = {
  authorized: "Allowed",
  clamped: "Reduced",
  rejected: "Refused",
  revoked: "Revoked",
  accepted: "Accepted",
  armed: "Armed",
  disarmed: "Disarmed",
  refused: "Refused",
  latched: "Latched",
  acknowledged: "Acknowledged",
};

function availabilityWord(lifecycle: string): string {
  return LIFECYCLE_WORDS[lifecycle] ?? lifecycle.replace(/_/g, " ");
}

function humanizeCode(code: string): string {
  return code.toLowerCase().replace(/_+/g, " ").trim();
}

function plainCode(code: string): string {
  const words = humanizeCode(code);
  if (words === "") {
    return code;
  }
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function plainAuditType(eventType: string): string {
  return AUDIT_TYPE_WORDS[eventType] ?? plainCode(eventType);
}

function plainResult(result: string): string {
  return RESULT_WORDS[result] ?? plainCode(result);
}

// ---------------------------------------------------------------------------
// Formatting (values are human words; units are explicit, signs by direction)
// ---------------------------------------------------------------------------

function formatWatts(watts: number): string {
  return Math.round(Math.abs(watts)).toLocaleString("en-US");
}

function directionWord(direction: string): string {
  if (direction === "charge") {
    return "Charging";
  }
  if (direction === "discharge") {
    return "Discharging";
  }
  return "Idle";
}

/** Card power phrase: the measured figure only, its sign carried by the
 * direction word — a clamped action must never read as the request. A unit
 * whose telemetry is marked missing carries no measurement at all, so a
 * zeroed wire field is never dressed up as a reading. */
function cardPowerText(unit: ViewUnit): string {
  const telemetryMissing = unit.quality === "missing" || unit.lifecycle === "disconnected";
  const measured = telemetryMissing ? null : unit.measured_watts;
  if (measured === null) {
    return "No data";
  }
  if (measured > 0) {
    return `Charging ${formatWatts(measured)} W`;
  }
  if (measured < 0) {
    return `Discharging ${formatWatts(measured)} W`;
  }
  const fallback = unit.authorized_power?.direction ?? unit.requested_power.direction;
  return `${directionWord(fallback)} 0 W`;
}

function ageText(seconds: number | null): string {
  if (seconds === null) {
    return "age unknown";
  }
  return `${seconds} ${seconds === 1 ? "second" : "seconds"} old`;
}

function missingText(ageSeconds: number | null): string {
  if (ageSeconds === null) {
    return "No data";
  }
  return `No data (last update ${ageText(ageSeconds)})`;
}

function clockLabel(iso: string): string {
  return `${iso.slice(11, 16)} UTC`;
}

function observationText(track: ObservationTrack | undefined): string {
  if (track === undefined) {
    return "No observation received in this session";
  }
  // A frame without its wall stamp names the gap rather than guessing a time.
  const when = track.occurredAt === "" ? "time not available" : clockLabel(track.occurredAt);
  const sequence =
    track.telemetrySequence === null ? "" : ` (telemetry sequence ${track.telemetrySequence})`;
  return `${when}${sequence}`;
}

/** The service's own quality projection already encodes staleness: anything
 * but "good" is data past its freshness bound or otherwise unusable. */
function isStaleData(unit: ViewUnit): boolean {
  if (unit.quality !== "good") {
    return true;
  }
  return unit.telemetry_age_s === null || unit.telemetry_age_s > GOOD_TELEMETRY_MAX_AGE_S;
}

// ---------------------------------------------------------------------------
// Defensive wire parsing (never trusts a frame; never fabricates a value)
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parsePower(value: unknown): WirePower | null {
  if (isRecord(value) && typeof value.direction === "string") {
    const watts = parseNumber(value.watts);
    if (watts !== null) {
      return { direction: value.direction, watts };
    }
  }
  return null;
}

function parseUnit(record: Record<string, unknown>): ViewUnit | null {
  if (typeof record.unit_id !== "string") {
    return null;
  }
  const inhibitRaw = record.inhibit;
  const inhibit =
    isRecord(inhibitRaw) &&
    typeof inhibitRaw.cause_class === "string" &&
    typeof inhibitRaw.latched === "boolean"
      ? {
          cause_class: inhibitRaw.cause_class,
          latched: inhibitRaw.latched,
          reason_code:
            typeof inhibitRaw.reason_code === "string" ? inhibitRaw.reason_code : null,
        }
      : null;
  return {
    unit_id: record.unit_id,
    lifecycle: typeof record.lifecycle === "string" ? record.lifecycle : "unknown",
    telemetry_age_s: parseNumber(record.telemetry_age_s),
    quality: typeof record.quality === "string" ? record.quality : "unknown",
    requested_power: parsePower(record.requested_power) ?? { direction: "idle", watts: 0 },
    authorized_power: parsePower(record.authorized_power),
    measured_watts: parseNumber(record.measured_watts),
    inhibit,
  };
}

function parseSnapshot(raw: unknown): FleetState | null {
  if (!isRecord(raw)) {
    return null;
  }
  const unitsRaw = Array.isArray(raw.units) ? raw.units : [];
  const units = unitsRaw
    .filter(isRecord)
    .map(parseUnit)
    .filter((unit): unit is ViewUnit => unit !== null);
  return {
    siteId: typeof raw.site_id === "string" ? raw.site_id : "",
    sequence: parseNumber(raw.snapshot_sequence) ?? 0,
    capturedAt: typeof raw.captured_at === "string" ? raw.captured_at : "",
    units,
  };
}

/** The only observation event the service publishes: `observation.published`
 * with `{unit_id, connection_epoch, sequence}` under `payload`. It carries no
 * readings, so nothing here can fabricate one. */
function parseObservationPublished(
  frame: StreamEvent,
): { unitId: string; track: ObservationTrack } | null {
  const payload = frame.payload;
  if (!isRecord(payload) || typeof payload.unit_id !== "string") {
    return null;
  }
  return {
    unitId: payload.unit_id,
    track: {
      occurredAt: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
      busSequence: parseNumber(frame.sequence) ?? 0,
      telemetrySequence: parseNumber(payload.sequence),
      connectionEpoch: parseNumber(payload.connection_epoch),
    },
  };
}

function parseAuditEntry(event: AuditEvent): AuditEntryView {
  return {
    sequence: typeof event.sequence === "number" ? event.sequence : 0,
    eventType: typeof event.event_type === "string" ? event.event_type : null,
    occurredAt: typeof event.occurred_at === "string" ? event.occurred_at : null,
    result: typeof event.result === "string" ? event.result : null,
    reasonCodes: Array.isArray(event.reason_codes)
      ? event.reason_codes.filter((code): code is string => typeof code === "string" && code !== "")
      : [],
    unitId: typeof event.unit_id === "string" ? event.unit_id : null,
  };
}

function toErrorView(error: unknown): ErrorView {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message, request_id: error.request_id };
  }
  return {
    code: "unexpected_error",
    message: "Something went wrong while talking to the EnergyPod service.",
    request_id: null,
  };
}

// ---------------------------------------------------------------------------
// Fleet card
// ---------------------------------------------------------------------------

interface FleetCardProps {
  unit: ViewUnit;
  observation: ObservationTrack | undefined;
  onOpenDetail: (unitId: string) => void;
  onAcknowledge: (unitId: string, opener: HTMLElement) => void;
}

/** The acknowledge control is offered exactly when a latch is established:
 * either the documented inhibit exposure says `latched`, or the unit is
 * inhibited and the snapshot does not expose the latch detail (the server
 * alone reports whether acknowledging cleared anything). */
function acknowledgeOffered(unit: ViewUnit): boolean {
  if (unit.inhibit !== null) {
    return unit.inhibit.latched;
  }
  return unit.lifecycle === "inhibited";
}

function FleetCard({
  unit,
  observation,
  onOpenDetail,
  onAcknowledge,
}: FleetCardProps): JSX.Element {
  const telemetryAge = unit.telemetry_age_s;
  const dimmed = isStaleData(unit);
  const charge = missingText(telemetryAge);
  const temperature = missingText(telemetryAge);
  const cellSpread = missingText(telemetryAge);
  return (
    <div
      role="group"
      aria-label={unit.unit_id}
      className={dimmed ? "battery-card dimmed" : "battery-card"}
    >
      <div className="card-title">
        <button type="button" onClick={() => onOpenDetail(unit.unit_id)}>
          {unit.unit_id}
        </button>
      </div>
      <p>
        <b>Availability:</b> {availabilityWord(unit.lifecycle)}
      </p>
      <p>
        <b>Charge level:</b> {charge}
      </p>
      <p>
        <b>Power:</b> {cardPowerText(unit)}
      </p>
      <p>
        <b>Temperature:</b> {temperature}
      </p>
      <p>
        <b>Cell spread:</b> {cellSpread}
      </p>
      <p>
        <b>Data age:</b> {ageText(telemetryAge)}
        {dimmed ? " (stale)" : ""}
      </p>
      <p>
        <b>Last observation:</b> {observationText(observation)}
      </p>
      <p className="warnings">
        <b>Warnings:</b> No data
      </p>
      {unit.inhibit !== null ? (
        <p className="latch">
          <b>Inhibit:</b>{" "}
          {unit.inhibit.latched
            ? `Latched (${plainCode(unit.inhibit.reason_code ?? unit.inhibit.cause_class)})`
            : "not latched"}
        </p>
      ) : (
        unit.lifecycle === "inhibited" && (
          <p className="latch">
            <b>Inhibit:</b> latched state not available from this snapshot
          </p>
        )
      )}
      {acknowledgeOffered(unit) && (
        <button
          type="button"
          className="acknowledge"
          onClick={(event) => onAcknowledge(unit.unit_id, event.currentTarget)}
        >
          Acknowledge inhibit
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unit detail (tabs are keyboard-operable: arrows move focus, Enter selects)
// ---------------------------------------------------------------------------

interface UnitDetailProps {
  unit: ViewUnit;
  observation: ObservationTrack | undefined;
  siteId: string;
  capturedAt: string;
  tab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onBack: () => void;
  auditPage: AuditPage | null;
  auditError: ErrorView | null;
  onRetryAudit: () => void;
}

function SummaryPanel({
  unit,
  observation,
}: {
  unit: ViewUnit;
  observation: ObservationTrack | undefined;
}): JSX.Element {
  const requested = unit.requested_power;
  const allowed = unit.authorized_power;
  const measured = unit.measured_watts;
  const actual =
    measured === null
      ? "no data"
      : `${directionWord(measured > 0 ? "charge" : measured < 0 ? "discharge" : "idle").toLowerCase()} ${formatWatts(measured)} W`;
  return (
    <div>
      <p>
        <b>Condition:</b> {availabilityWord(unit.lifecycle)} (data quality: {unit.quality})
      </p>
      <p>
        <b>Power:</b> requested {directionWord(requested.direction).toLowerCase()}{" "}
        {formatWatts(requested.watts)} W; allowed{" "}
        {allowed === null
          ? "none recorded"
          : `${directionWord(allowed.direction).toLowerCase()} ${formatWatts(allowed.watts)} W`}
        ; delivering {actual}
      </p>
      <p>
        <b>Limits:</b>{" "}
        {allowed === null
          ? "No authorized power recorded"
          : `Allowed ${directionWord(allowed.direction).toLowerCase()} up to ${formatWatts(allowed.watts)} W`}
      </p>
      <p>
        <b>Communications:</b> last telemetry {ageText(unit.telemetry_age_s)}, quality{" "}
        {unit.quality}; last observation {observationText(observation)}
      </p>
      <p>
        <b>Recent trend:</b> no trend history is available from the API yet
      </p>
    </div>
  );
}

function CellsPanel({
  unit,
}: {
  unit: ViewUnit;
}): JSX.Element {
  const age = unit.telemetry_age_s;
  return (
    <div>
      <p>
        <b>Minimum cell voltage:</b> {missingText(age)}
      </p>
      <p>
        <b>Maximum cell voltage:</b> {missingText(age)}
      </p>
      <p>
        <b>Voltage spread:</b> {missingText(age)}
      </p>
      <p>
        <b>Temperature range:</b> {missingText(age)}
      </p>
      <p>
        <b>Data completeness:</b> the snapshot and the observation event carry no cell or
        temperature readings, so every value above is named as missing rather than estimated
        ({ageText(age)}).
      </p>
    </div>
  );
}

function EventsPanel({
  unitId,
  auditPage,
  auditError,
  onRetry,
}: {
  unitId: string;
  auditPage: AuditPage | null;
  auditError: ErrorView | null;
  onRetry: () => void;
}): JSX.Element {
  const [revealed, setRevealed] = useState<Record<number, boolean>>({});
  if (auditPage === null) {
    if (auditError !== null) {
      return (
        <div role="alert">
          <p>{auditError.code}</p>
          <p>{auditError.message}</p>
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      );
    }
    return (
      <p role="status" aria-label="Loading events">
        Loading events…
      </p>
    );
  }
  const entries = auditPage.events
    .map(parseAuditEntry)
    .filter((entry) => entry.unitId === unitId);
  if (entries.length === 0) {
    return <p>No audit events recorded for this unit yet.</p>;
  }
  return (
    <ul className="event-list">
      {entries.map((entry: AuditEntryView) => {
        const typeText = entry.eventType === null ? "Unknown event" : plainAuditType(entry.eventType);
        const resultText = entry.result === null ? "result not recorded" : plainResult(entry.result);
        const when = entry.occurredAt !== null ? ` (${clockLabel(entry.occurredAt)})` : "";
        const raw = [
          entry.eventType === null ? "event_type unavailable" : `event_type: ${entry.eventType}`,
          ...entry.reasonCodes.map((code) => code),
        ].join(", ");
        return (
          <li key={entry.sequence}>
            <p>
              {typeText}: {resultText}
              {entry.reasonCodes.length > 0
                ? ` — ${entry.reasonCodes.map(plainCode).join(", ")}`
                : ""}
              {when}
            </p>
            <button
              type="button"
              onClick={() =>
                setRevealed((previous) => ({
                  ...previous,
                  [entry.sequence]: !previous[entry.sequence],
                }))
              }
            >
              Technical details
            </button>
            {revealed[entry.sequence] === true && <p className="raw-code">{raw}</p>}
          </li>
        );
      })}
    </ul>
  );
}

function DetailsPanel({
  unit,
  observation,
  siteId,
  capturedAt,
}: {
  unit: ViewUnit;
  observation: ObservationTrack | undefined;
  siteId: string;
  capturedAt: string;
}): JSX.Element {
  const observationEpoch =
    observation?.connectionEpoch === null || observation === undefined
      ? "not available"
      : String(observation.connectionEpoch);
  return (
    <div>
      <p>
        <b>Data quality:</b> {unit.quality}
        {isStaleData(unit) ? " (stale)" : ""}
      </p>
      <p>
        <b>Telemetry age:</b> {ageText(unit.telemetry_age_s)}
      </p>
      <p>
        <b>Observation detail:</b> last observation {observationText(observation)}; connection
        epoch {observationEpoch}; measurement detail (charge, cells, temperatures) not carried
        by the snapshot or the observation event
      </p>
      <p>
        <b>Measurement completeness:</b>{" "}
        {unit.measured_watts === null ? "measured power missing" : "measured power present"};
        charge, cell, and temperature detail missing
      </p>
      <p>
        <b>Unit ID:</b> {unit.unit_id}
      </p>
      <p>
        <b>Lifecycle (wire value):</b> {unit.lifecycle}
      </p>
      <p>
        <b>Site:</b> {siteId !== "" ? siteId : "unknown"}
      </p>
      <p>
        <b>Snapshot captured at:</b>{" "}
        {capturedAt !== "" ? clockLabel(capturedAt) : "unknown"}
      </p>
      <p>
        <b>Inhibit:</b>{" "}
        {unit.inhibit === null
          ? "not exposed by this snapshot"
          : `${unit.inhibit.cause_class}${unit.inhibit.latched ? ", latched" : ""}${
              unit.inhibit.reason_code !== null ? `, reason ${unit.inhibit.reason_code}` : ""
            }`}
      </p>
    </div>
  );
}

function UnitDetail({
  unit,
  observation,
  siteId,
  capturedAt,
  tab,
  onTabChange,
  onBack,
  auditPage,
  auditError,
  onRetryAudit,
}: UnitDetailProps): JSX.Element {
  const tabRefs = useRef<Partial<Record<TabKey, HTMLButtonElement>>>({});

  // Opening a unit moves focus to its first section.
  useEffect(() => {
    tabRefs.current.summary?.focus();
  }, []);

  const handleTablistKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
      return;
    }
    event.preventDefault();
    const focused = TABS.find((entry) => tabRefs.current[entry.key] === document.activeElement);
    const current = TABS.findIndex((entry) => entry.key === (focused?.key ?? tab));
    if (current === -1) {
      return;
    }
    const offset = event.key === "ArrowRight" ? 1 : -1;
    const next = TABS[(current + offset + TABS.length) % TABS.length];
    if (next !== undefined) {
      tabRefs.current[next.key]?.focus();
    }
  };

  return (
    <div className="unit-detail">
      <h2>{unit.unit_id}</h2>
      <button type="button" className="back" onClick={onBack}>
        Back to all batteries
      </button>
      <div
        role="tablist"
        aria-label={`${unit.unit_id} detail sections`}
        onKeyDown={handleTablistKeyDown}
      >
        {TABS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            role="tab"
            id={`tab-${unit.unit_id}-${entry.key}`}
            aria-selected={tab === entry.key}
            aria-controls={`panel-${unit.unit_id}-${entry.key}`}
            tabIndex={tab === entry.key ? 0 : -1}
            ref={(element) => {
              if (element !== null) {
                tabRefs.current[entry.key] = element;
              }
            }}
            onClick={() => onTabChange(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`panel-${unit.unit_id}-${tab}`}
        aria-labelledby={`tab-${unit.unit_id}-${tab}`}
      >
        {tab === "summary" && <SummaryPanel unit={unit} observation={observation} />}
        {tab === "cells" && <CellsPanel unit={unit} />}
        {tab === "events" && (
          <EventsPanel
            unitId={unit.unit_id}
            auditPage={auditPage}
            auditError={auditError}
            onRetry={onRetryAudit}
          />
        )}
        {tab === "details" && (
          <DetailsPanel
            unit={unit}
            observation={observation}
            siteId={siteId}
            capturedAt={capturedAt}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Latched-inhibit acknowledgement dialog (focus trapped, focus restored)
// ---------------------------------------------------------------------------

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

interface AcknowledgeDialogProps {
  unitId: string;
  reasonText: string;
  error: ErrorView | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

function AcknowledgeDialog({
  unitId,
  reasonText,
  error,
  pending,
  onCancel,
  onConfirm,
}: AcknowledgeDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    const focusables = focusableIn(dialog);
    (focusables[0] ?? dialog).focus();
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
      aria-labelledby="inhibit-ack-title"
      className="dialog"
      onKeyDown={handleKeyDown}
    >
      <h2 id="inhibit-ack-title">Acknowledge inhibit on {unitId}</h2>
      <p>
        <b>Reason:</b> {reasonText}
      </p>
      <p>
        Acknowledging clears the latched inhibit. Re-arming the unit is a separate deliberate
        step.
      </p>
      {error !== null && (
        <div role="alert" className="dialog-error">
          <p>{error.code}</p>
          <p>{error.message}</p>
        </div>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button type="button" onClick={onConfirm} disabled={pending}>
          Confirm acknowledgement
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export function BatteriesView({
  client,
  connection = "connected",
}: BatteriesViewProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [snapshotError, setSnapshotError] = useState<ErrorView | null>(null);
  const [fleet, setFleet] = useState<FleetState | null>(null);
  /**
   * The first snapshot render is held for one macrotask so the event stream's
   * opening burst (snapshot frame + pending observations, all microtasks) is
   * already merged when a card first paints — a card never appears with a
   * value it is about to correct.
   */
  const [fleetReady, setFleetReady] = useState(false);
  const [observations, setObservations] = useState<Record<string, ObservationTrack>>({});
  const [streamLost, setStreamLost] = useState(false);
  const [streamOn, setStreamOn] = useState(false);
  const [detail, setDetail] = useState<{ unitId: string; tab: TabKey } | null>(null);
  const [ackUnitId, setAckUnitId] = useState<string | null>(null);
  const [ackError, setAckError] = useState<ErrorView | null>(null);
  const [ackPending, setAckPending] = useState(false);
  const [auditPage, setAuditPage] = useState<AuditPage | null>(null);
  const [auditError, setAuditError] = useState<ErrorView | null>(null);

  const cursorRef = useRef<number | undefined>(undefined);
  const streamStartedRef = useRef(false);
  const openerRef = useRef<HTMLElement | null>(null);

  const applySnapshot = useCallback((raw: unknown): void => {
    const parsed = parseSnapshot(raw);
    if (parsed === null) {
      return;
    }
    setFleet(parsed);
    if (!streamStartedRef.current) {
      cursorRef.current = parsed.sequence;
    }
  }, []);

  const loadSnapshot = useCallback(async (): Promise<boolean> => {
    try {
      const snapshot = await client.getSnapshot();
      applySnapshot(snapshot);
      setSnapshotError(null);
      return true;
    } catch (error) {
      setSnapshotError(toErrorView(error));
      return false;
    }
  }, [applySnapshot, client]);

  const loadSnapshotRef = useRef(loadSnapshot);
  useEffect(() => {
    loadSnapshotRef.current = loadSnapshot;
  }, [loadSnapshot]);

  useEffect(() => {
    if (fleet === null || fleetReady) {
      return;
    }
    const settle = setTimeout(() => {
      setFleetReady(true);
    }, 0);
    return () => {
      clearTimeout(settle);
    };
  }, [fleet, fleetReady]);

  // Initial snapshot load; the event stream starts from its sequence.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const ok = await loadSnapshot();
      if (cancelled) {
        return;
      }
      if (ok) {
        streamStartedRef.current = true;
        setPhase("ready");
        setStreamOn(true);
      } else {
        setPhase("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadSnapshot]);

  // Unit history for the Events tab (plain language first, raw code on demand).
  const fetchAudit = useCallback(async (): Promise<void> => {
    try {
      const page = await client.getAudit(AUDIT_LIMIT);
      setAuditPage(page);
      setAuditError(null);
    } catch (error) {
      setAuditError(toErrorView(error));
    }
  }, [client]);

  useEffect(() => {
    void fetchAudit();
  }, [fetchAudit]);

  // Live events: resync when the service says so, retry with the last
  // delivered sequence as the cursor when the connection drops. Only frames
  // the service actually publishes are interpreted.
  useEffect(() => {
    if (!streamOn) {
      return;
    }
    let cancelled = false;
    const run = async (): Promise<void> => {
      while (!cancelled) {
        let resync = false;
        try {
          for await (const frame of client.openEvents(cursorRef.current)) {
            if (cancelled) {
              return;
            }
            setStreamLost(false);
            if (frame.type === "resync_required") {
              const recovery = frame.snapshot_sequence;
              if (typeof recovery === "number") {
                cursorRef.current = recovery;
              }
              resync = true;
              break;
            }
            if (typeof frame.sequence === "number") {
              cursorRef.current = frame.sequence;
            }
            if (frame.type === "snapshot") {
              applySnapshot(frame.data);
            } else if (frame.type === "observation.published") {
              const parsed = parseObservationPublished(frame);
              if (parsed !== null) {
                setObservations((previous) => ({ ...previous, [parsed.unitId]: parsed.track }));
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
          await loadSnapshotRef.current();
        } else {
          setStreamLost(true);
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
  }, [applySnapshot, client, streamOn]);

  const retrySnapshot = (): void => {
    setPhase("loading");
    void (async () => {
      const ok = await loadSnapshot();
      if (ok) {
        streamStartedRef.current = true;
        setPhase("ready");
        setStreamOn(true);
      } else {
        setPhase("error");
      }
    })();
  };

  const openAcknowledge = (unitId: string, opener: HTMLElement): void => {
    openerRef.current = opener;
    setAckError(null);
    setAckPending(false);
    setAckUnitId(unitId);
  };

  const cancelAcknowledge = (): void => {
    setAckUnitId(null);
    setAckError(null);
    setAckPending(false);
    openerRef.current?.focus();
  };

  const confirmAcknowledge = (): void => {
    if (ackUnitId === null || ackPending) {
      return;
    }
    setAckPending(true);
    setAckError(null);
    const unitId = ackUnitId;
    void (async () => {
      try {
        await client.postInhibitAcknowledgement(unitId);
        const opener = openerRef.current;
        setAckUnitId(null);
        setAckPending(false);
        opener?.focus();
        // Post-acknowledge state is server authority: the latch clears only
        // through a snapshot refetch, never optimistic local state.
        await loadSnapshot();
      } catch (error) {
        setAckPending(false);
        setAckError(toErrorView(error));
      }
    })();
  };

  if (fleet === null) {
    if (phase === "error" && snapshotError !== null) {
      return (
        <div className="batteries-view">
          <div role="alert" className="error-panel">
            <h2>Battery snapshot unavailable</h2>
            <p>{snapshotError.code}</p>
            <p>{snapshotError.message}</p>
            <button type="button" onClick={retrySnapshot}>
              Retry
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="batteries-view">
        <div role="status" aria-label="Loading batteries" className="skeleton">
          <p>Loading the battery fleet…</p>
          <div aria-hidden="true" className="skeleton-row" />
          <div aria-hidden="true" className="skeleton-row" />
          <div aria-hidden="true" className="skeleton-row" />
        </div>
      </div>
    );
  }

  if (!fleetReady) {
    return (
      <div className="batteries-view">
        <div role="status" aria-label="Loading batteries" className="skeleton">
          <p>Loading the battery fleet…</p>
          <div aria-hidden="true" className="skeleton-row" />
          <div aria-hidden="true" className="skeleton-row" />
          <div aria-hidden="true" className="skeleton-row" />
        </div>
      </div>
    );
  }

  const units = fleet?.units ?? [];
  const latchedUnits = units.filter(acknowledgeOffered);
  const disconnected = connection === "disconnected" || streamLost;
  const detailUnit =
    detail === null ? undefined : units.find((unit) => unit.unit_id === detail.unitId);
  const ackUnit =
    ackUnitId === null ? undefined : units.find((unit) => unit.unit_id === ackUnitId);

  return (
    <div className="batteries-view">
      {latchedUnits.length > 0 && (
        <div role="alert" className="latch-alert">
          Inhibit latched on {latchedUnits.map((unit) => unit.unit_id).join(", ")}: acknowledge
          to clear the latch. Re-arming is a separate step.
        </div>
      )}
      {disconnected && (
        <p role="status" className="disconnected">
          Disconnected from live updates — showing the last known readings while the
          connection retries automatically.
        </p>
      )}
      {fleet !== null && snapshotError !== null && (
        <div role="status" className="refresh-error">
          <p>
            Could not refresh the snapshot: {snapshotError.code} — {snapshotError.message}
          </p>
          <button type="button" onClick={retrySnapshot}>
            Try again
          </button>
        </div>
      )}
      {units.length === 0 && fleet !== null && (
        <div className="empty">
          <h2>No batteries yet</h2>
          <p>Battery cards will appear here once you connect a battery unit.</p>
        </div>
      )}
      <div className="fleet">
        {units.map((unit) => (
          <FleetCard
            key={unit.unit_id}
            unit={unit}
            observation={observations[unit.unit_id]}
            onOpenDetail={(unitId) => setDetail({ unitId, tab: "summary" })}
            onAcknowledge={openAcknowledge}
          />
        ))}
      </div>
      {detail !== null &&
        (detailUnit === undefined ? (
          <p role="status">This unit is no longer in the current snapshot.</p>
        ) : (
          <UnitDetail
            key={detailUnit.unit_id}
            unit={detailUnit}
            observation={observations[detailUnit.unit_id]}
            siteId={fleet?.siteId ?? ""}
            capturedAt={fleet?.capturedAt ?? ""}
            tab={detail.tab}
            onTabChange={(tab) =>
              setDetail((previous) => (previous === null ? previous : { ...previous, tab }))
            }
            onBack={() => setDetail(null)}
            auditPage={auditPage}
            auditError={auditError}
            onRetryAudit={() => void fetchAudit()}
          />
        ))}
      {ackUnitId !== null && ackUnit !== undefined && (
        <AcknowledgeDialog
          unitId={ackUnit.unit_id}
          reasonText={
            ackUnit.inhibit?.reason_code !== null && ackUnit.inhibit?.reason_code !== undefined
              ? plainCode(ackUnit.inhibit.reason_code)
              : "latch reason not available from this snapshot"
          }
          error={ackError}
          pending={ackPending}
          onCancel={cancelAcknowledge}
          onConfirm={confirmAcknowledge}
        />
      )}
    </div>
  );
}
