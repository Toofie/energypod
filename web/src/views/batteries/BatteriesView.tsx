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
 * - The snapshot unit carries `unit_id, lifecycle, telemetry_age_s, quality,
 *   requested_power, authorized_power, measured_watts` plus the amended
 *   nullable `telemetry` summary projection (API_CONTRACTS.md "Application
 *   service facade"): `soc_pct` … `active_warnings`, every field null when
 *   that datum is absent — never zero-filled. Card fields read exactly that
 *   projection; a null telemetry block or a null field renders "No data"
 *   (named, with age), never an invented value.
 * - `GET /api/v1/units/{unit_id}` returns the full latest-observation
 *   projection for one unit: identity (`device_identity`),
 *   `protocol_profile`, `connection_epoch`, telemetry and cell sequences and
 *   capture times, the scalar measurements, the complete `cell_voltages_v`
 *   and `temperatures_c` arrays, the per-field `quality` map, faults, and
 *   warnings. The view fetches it on demand when a unit is opened (the Cells
 *   tab's distribution and the Summary tab's identity come from it) with its
 *   own loading / error / disconnected states; unknown ids are refused with
 *   the structured envelope, rendered verbatim.
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
import { isPlaneSnapshot } from "../../app/SharedDataPlane";
import { useUnitIntentFigures } from "../../app/useUnitIntentFigures";
import {
  applyHealthPatch,
  gridLoadText,
  toActuationIncoherentEvent,
  toHealthChangedEvent,
  toUnitHealth,
  type UnitHealth,
  type WattsByUnit,
} from "../../app/fleet";
import {
  commandedBySomeoneElseText,
  isForeignObjective,
  objectiveFromForeignEvent,
  observedObjectiveDetailText,
  toForeignObjectiveEvent,
  toUnitObjective,
  type UnitObjective,
} from "../../app/objectives";
import {
  GRID_COUNTER_A_LABEL,
  GRID_COUNTER_B_LABEL,
  PV_READTHROUGH_NOTE,
  kwhText,
  toEnergyToday,
  type EnergyToday,
  type EnergyUnitDay,
} from "../../app/energy";
import { UnitHealthTag } from "../../app/unitHealth";
import { toParkState, toResumeChecklist, type ParkStateView, type ResumeChecklistView } from "../../app/park";
import {
  DeliveryBiasReadout,
  ParkDialog,
  ParkedBanner,
  ParkedChip,
  type ParkRefusalView,
  RecoveryAdvisoryCard,
  ResumeChecklistCard,
  ResumeDialog,
  type RecoveryAdvisoryView,
  type DeliveryBiasView,
} from "./Parking";
import {
  formatKilowattHours,
  formatMillivolts,
  formatPercent,
  formatTemp,
  formatVolts,
  formatWatts,
  wholeSeconds,
} from "../../lib/format";

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

/**
 * The nullable telemetry summary projection the snapshot carries (defensively
 * parsed; every field null when that datum was absent from the observation).
 */
interface TelemetryView {
  readonly socPct: number | null;
  readonly bmsSocPct: number | null;
  readonly sohPct: number | null;
  readonly packVoltageV: number | null;
  readonly packCurrentA: number | null;
  readonly batteryWatts: number | null;
  readonly dynamicChargeLimitW: number | null;
  readonly dynamicDischargeLimitW: number | null;
  readonly cellCount: number | null;
  readonly cellMinV: number | null;
  readonly cellMaxV: number | null;
  readonly cellSpreadMv: number | null;
  readonly temperatureMinC: number | null;
  readonly temperatureMaxC: number | null;
  /**
   * The advisory per-pod CT readthrough (service.py `_telemetry_summary`,
   * live on today's wire): `gridPowerW` signed (negative = import, positive =
   * export), `loadPowerW` the pod's local load. Null when the poll served no
   * PCS live block — named as missing, never zero-filled.
   */
  readonly gridPowerW: number | null;
  readonly loadPowerW: number | null;
  readonly activeFaults: readonly string[] | null;
  readonly activeWarnings: readonly string[] | null;
  /**
   * The vendor debug-mode readback (`debug_mode_w`, 0x8100+0 — the
   * parking/standby word), readthrough-style: null when the block was not
   * served. The parked chip's tooltip names this word and the pack voltage —
   * telemetry's own figures, never a status metaphor.
   */
  readonly debugModeW: number | null;
}

interface ViewUnit {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: WirePower;
  authorized_power: WirePower | null;
  measured_watts: number | null;
  telemetry: TelemetryView | null;
  inhibit: LatchState | null;
  /**
   * The self-healing awareness layer's derived recovery view; null when the
   * snapshot carries no usable health fields (the feature detection — the
   * card's health badge renders nothing at all).
   */
  health: UnitHealth | null;
  /**
   * The night-writer detector's per-unit summary (PENDING, feature-detected):
   * the most recent recorded sample of the served objective; null when the
   * snapshot carries none. The card's quiet line renders ONLY on the
   * detector's foreign classification — in-band autonomy is evidence, not a
   * card fact (the detail panel and the Objectives view carry it).
   */
  objective: UnitObjective | null;
  /**
   * The pod-parking projection (PENDING, feature-detected): present on every
   * unit once the `parking:` config block is commissioned — null when the key
   * is absent (the not-commissioned feature detection: no chip, no banner,
   * and no Park/Resume affordance renders at all).
   */
  park: ParkStateView | null;
}

/** The GET /api/v1/units/{id} projection, parsed just as defensively. */
interface UnitDetailView {
  readonly deviceIdentity: string | null;
  readonly protocolProfile: string | null;
  readonly connectionEpoch: number | null;
  readonly wallTimestamp: string | null;
  readonly sequence: number | null;
  readonly cellSequence: number | null;
  readonly telemetry: TelemetryView;
  readonly cellVoltages: readonly number[];
  readonly temperatures: readonly number[];
  readonly quality: Readonly<Record<string, string>>;
  /**
   * The six cumulative-energy readthroughs (PENDING on the wire): lifetime
   * kWh counters, null when the `0x4101` energy block was not served —
   * never zero-filled. The grid pair keeps its NEUTRAL A/B names: which
   * counter is "bought" is evidence-open (A-1), so the rows never say.
   */
  readonly energyGridAKwh: number | null;
  readonly energyGridBKwh: number | null;
  readonly energyLoadKwh: number | null;
  readonly energyPvKwh: number | null;
  readonly energyChargeKwh: number | null;
  readonly energyDischargeKwh: number | null;
  /** The parking projection on the detail read (absent = not commissioned). */
  readonly park: ParkStateView | null;
  /** The wedge-signature recovery advisory; null when the read carries none. */
  readonly recoveryAdvisory: RecoveryAdvisoryView | null;
  /** The delivery-bias evidence window; null when the read carries none. */
  readonly deliveryBias: DeliveryBiasView | null;
}

type DetailPhase = "loading" | "ready" | "error";

interface FleetState {
  siteId: string;
  sequence: number;
  capturedAt: string;
  units: ViewUnit[];
  /**
   * The snapshot's top-level `energy_today` block (PENDING, feature-detected):
   * null when the field is absent — the scorecard is not composed here and no
   * per-card energy row renders at all.
   */
  energyToday: EnergyToday | null;
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

/** The once-a-second re-render every displayed data age needs (never an
 * animation: a frozen "3 seconds old" reads as current forever). */
const AGE_TICK_MS = 1000;

/** A monotonic reading: displayed ages must never run backwards. */
function monotonicNowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/**
 * A ticking "now" while an on-screen value depends on elapsed time. The
 * snapshot's `telemetry_age_s` is the age AT CAPTURE: without the tick the
 * card's "N seconds old" line freezes at the value the last snapshot arrived
 * with (Home's and Now's pinned rule — an age that never grows is a frozen
 * reading, not a current one). A backgrounded tab gets its timers throttled,
 * so the clock is also re-read the moment the tab becomes visible or focused
 * again: the first render after coming back carries the true age.
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

/**
 * The cell-imbalance EARLY-WARNING line (2026-08-23): the live policy's
 * imbalance bound was operator-relaxed tenfold (0.050 V -> 0.500 V) so a wide
 * spread stops vetoing fleet dispatch on its own — the absolute per-cell
 * voltage bounds remain the real over/under-charge protection.  The console
 * keeps the OLD 50 mV figure as the warning line: any unit spreading wider
 * than it is called out per-unit here, so a drifting pack is never lost in
 * the relaxed policy's silence.  Display-only — the kernel stays the authority.
 */
const CELL_IMBALANCE_WARNING_MV = 50;

// ---------------------------------------------------------------------------
// Plain-language mappings (words first, raw codes only on demand)
// ---------------------------------------------------------------------------

const LIFECYCLE_WORDS: Record<string, string> = {
  boot: "Starting up",
  disarmed: "Disarmed",
  armed: "Armed",
  armed_idle: "Armed and idle",
  observe_only: "Observe only",
  active: "Active",
  inhibited: "Inhibited",
  stopping: "Stopping",
  disconnected: "No contact",
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
// Formatting (values are human words; units are explicit, signs by direction).
// Every figure renders through the shared display-precision module
// (src/lib/format.ts): at most two decimals, integers as integers — the card
// carries the sign in its direction word, so the watt call sites pass the
// magnitude and the shared formatWatts adds the unit.
// ---------------------------------------------------------------------------

function directionWord(direction: string): string {
  if (direction === "charge") {
    return "Charging";
  }
  if (direction === "discharge") {
    return "Discharging";
  }
  return "Idle";
}

/** A battery warning/fault code with its underscores as spaces (the codes are
 * device families like `PCS_Warning0_1`; no vendor text table exists). */
function warningWord(code: string): string {
  return code.replace(/_+/g, " ");
}

/** Card power phrase: the measured figure only, its sign carried by the
 * direction word — a clamped action must never read as the request. The
 * telemetry block's `battery_watts` is the observation's own signed figure;
 * `measured_watts` is the same datum the facade projects, so either may carry
 * the row. A unit whose telemetry is marked missing carries no measurement at
 * all, so a zeroed wire field is never dressed up as a reading. */
function cardPowerText(unit: ViewUnit): string {
  const telemetryMissing = unit.quality === "missing" || unit.lifecycle === "disconnected";
  const fromTelemetry = telemetryMissing ? null : unit.telemetry?.batteryWatts ?? null;
  const measured = fromTelemetry ?? (telemetryMissing ? null : unit.measured_watts);
  if (measured === null) {
    return "No data";
  }
  // Wire convention (PROTOCOL_EVIDENCE 4b, live-proven): a POSITIVE battery
  // watt figure is DISCHARGE, negative is CHARGE.
  if (measured > 0) {
    return `Discharging ${formatWatts(measured)}`;
  }
  if (measured < 0) {
    return `Charging ${formatWatts(Math.abs(measured))}`;
  }
  const fallback = unit.authorized_power?.direction ?? unit.requested_power.direction;
  return `${directionWord(fallback)} ${formatWatts(0)}`;
}

/** Charge row: the telemetry block's SOC, or the named gap. */
function cardChargeText(unit: ViewUnit, ageSeconds: number | null): string {
  const soc = unit.telemetry?.socPct;
  if (soc === null || soc === undefined) {
    return missingText(ageSeconds);
  }
  return formatPercent(soc);
}

/** Pack voltage row: present only as the observation reported it. */
function cardPackVoltageText(unit: ViewUnit, ageSeconds: number | null): string {
  const volts = unit.telemetry?.packVoltageV;
  if (volts === null || volts === undefined) {
    return missingText(ageSeconds);
  }
  return formatVolts(volts);
}

/** Cell spread row: spread first, the population it was measured over named. */
function cardCellSpreadText(unit: ViewUnit, ageSeconds: number | null): string {
  const telemetry = unit.telemetry;
  const spread = telemetry?.cellSpreadMv ?? null;
  const count = telemetry?.cellCount ?? null;
  if (spread === null && count === null) {
    return missingText(ageSeconds);
  }
  if (spread === null) {
    return `${count} cells`;
  }
  if (count === null) {
    return `${formatMillivolts(spread)} spread`;
  }
  return `${formatMillivolts(spread)} across ${count} cells`;
}

/**
 * The per-unit imbalance warning line: exactly the units whose measured
 * spread crosses the early-warning line, with the measured figure.  Null for
 * a unit with no spread reading or a spread inside the line — an absent
 * datum never fabricates a warning.
 */
function cellImbalanceWarning(unit: ViewUnit): string | null {
  const spread = unit.telemetry?.cellSpreadMv ?? null;
  if (spread === null || spread <= CELL_IMBALANCE_WARNING_MV) {
    return null;
  }
  return `Cell imbalance warning: ${formatMillivolts(spread)} spread (above the ${CELL_IMBALANCE_WARNING_MV} mV early-warning line; not blocking dispatch on its own)`;
}

/** Temperature row: the observed range, min to max. */
function cardTemperatureText(unit: ViewUnit, ageSeconds: number | null): string {
  const min = unit.telemetry?.temperatureMinC ?? null;
  const max = unit.telemetry?.temperatureMaxC ?? null;
  if (min === null && max === null) {
    return missingText(ageSeconds);
  }
  if (min === null) {
    return `up to ${formatTemp(max as number)}`;
  }
  if (max === null) {
    return `from ${formatTemp(min)}`;
  }
  return `${formatTemp(min)} to ${formatTemp(max)}`;
}

/**
 * The grid-tie row (the excess-solar package's W-D): the pod's own advisory
 * CT readthrough, with the SAME import/export wording as the Home tile's
 * per-phase rows ("grid -800 W import · load 210 W"). An absent datum reads
 * "not available" — the fields are live on the wire but null whenever the
 * poll served no PCS live block, and that gap is named, never zero-filled.
 */
function cardGridLoadText(unit: ViewUnit): string {
  return gridLoadText(unit.telemetry?.gridPowerW ?? null, unit.telemetry?.loadPowerW ?? null);
}

/** Warnings row: the block's own words; an empty list is a real "None". */
function cardWarningsText(unit: ViewUnit): string {
  const warnings = unit.telemetry?.activeWarnings;
  if (warnings === null || warnings === undefined) {
    return "No data";
  }
  if (warnings.length === 0) {
    return "None";
  }
  return warnings.map(warningWord).join(", ");
}

function ageText(seconds: number | null): string {
  if (seconds === null) {
    return "age unknown";
  }
  const whole = wholeSeconds(seconds);
  return `${whole} ${whole === 1 ? "second" : "seconds"} old`;
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
 * but "good" is data past its freshness bound or otherwise unusable. The age
 * is the DISPLAYED one (captured plus elapsed): a reading whose age has grown
 * past the bound between snapshots is stale on screen too, never merely
 * waiting for the next refresh to say so. */
function isStaleData(unit: ViewUnit, ageSeconds: number | null): boolean {
  if (unit.quality !== "good") {
    return true;
  }
  if (ageSeconds === null) {
    return true;
  }
  return ageSeconds > GOOD_TELEMETRY_MAX_AGE_S;
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

function parseStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  return value.filter((code): code is string => typeof code === "string" && code !== "");
}

/**
 * The telemetry projection's own field names — shared by the snapshot's
 * nested `telemetry` block and the unit detail's flat projection — with every
 * absent datum staying null.
 */
function parseTelemetryFields(value: Record<string, unknown>): TelemetryView {
  return {
    socPct: parseNumber(value.soc_pct),
    bmsSocPct: parseNumber(value.bms_soc_pct),
    sohPct: parseNumber(value.soh_pct),
    packVoltageV: parseNumber(value.pack_voltage_v),
    packCurrentA: parseNumber(value.pack_current_a),
    batteryWatts: parseNumber(value.battery_watts),
    dynamicChargeLimitW: parseNumber(value.dynamic_charge_limit_w),
    dynamicDischargeLimitW: parseNumber(value.dynamic_discharge_limit_w),
    cellCount:
      typeof value.cell_count === "number" &&
      Number.isInteger(value.cell_count) &&
      value.cell_count >= 0
        ? value.cell_count
        : null,
    cellMinV: parseNumber(value.cell_min_v),
    cellMaxV: parseNumber(value.cell_max_v),
    cellSpreadMv: parseNumber(value.cell_spread_mv),
    temperatureMinC: parseNumber(value.temperature_min_c),
    temperatureMaxC: parseNumber(value.temperature_max_c),
    gridPowerW: parseNumber(value.grid_power_w),
    loadPowerW: parseNumber(value.load_power_w),
    activeFaults: parseStringArray(value.active_faults),
    activeWarnings: parseStringArray(value.active_warnings),
    debugModeW: parseNumber(value.debug_mode_w),
  };
}

/** The snapshot's nested, nullable telemetry block. */
function parseTelemetry(value: unknown): TelemetryView | null {
  return isRecord(value) ? parseTelemetryFields(value) : null;
}

function parseNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (entry): entry is number => typeof entry === "number" && Number.isFinite(entry),
  );
}

function parseQualityMap(value: unknown): Record<string, string> {
  if (!isRecord(value)) {
    return {};
  }
  const quality: Record<string, string> = {};
  for (const [field, word] of Object.entries(value)) {
    if (typeof word === "string" && word !== "") {
      quality[field] = word;
    }
  }
  return quality;
}

/** The GET /api/v1/units/{id} projection; nothing is trusted, nothing made up. */
function parseUnitDetail(raw: unknown): UnitDetailView | null {
  if (!isRecord(raw)) {
    return null;
  }
  return {
    deviceIdentity: typeof raw.device_identity === "string" ? raw.device_identity : null,
    protocolProfile: typeof raw.protocol_profile === "string" ? raw.protocol_profile : null,
    connectionEpoch: parseNumber(raw.connection_epoch),
    wallTimestamp: typeof raw.wall_timestamp === "string" ? raw.wall_timestamp : null,
    sequence: parseNumber(raw.sequence),
    cellSequence: parseNumber(raw.cell_sequence),
    telemetry: parseTelemetryFields(raw),
    cellVoltages: parseNumberArray(raw.cell_voltages_v),
    temperatures: parseNumberArray(raw.temperatures_c),
    quality: parseQualityMap(raw.quality),
    energyGridAKwh: parseNumber(raw.energy_grid_a_kwh),
    energyGridBKwh: parseNumber(raw.energy_grid_b_kwh),
    energyLoadKwh: parseNumber(raw.energy_load_kwh),
    energyPvKwh: parseNumber(raw.energy_pv_kwh),
    energyChargeKwh: parseNumber(raw.energy_charge_kwh),
    energyDischargeKwh: parseNumber(raw.energy_discharge_kwh),
    park: toParkState(raw.park_state),
    recoveryAdvisory: parseRecoveryAdvisory(raw.recovery_advisory),
    deliveryBias: parseDeliveryBias(raw.delivery_bias),
  };
}

/**
 * The wedge-signature advisory (DESIGN_POD_PARKING §7): presence of the key
 * is the render signal; the echo classifications are the only inner field the
 * card's wording reads, and they are read defensively — the contract pins the
 * advisory's existence, not its inner shape.
 */
function parseRecoveryAdvisory(value: unknown): RecoveryAdvisoryView | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    echoClassifications: parseStringArray(value.echo_classifications) ?? [],
  };
}

/**
 * The delivery-bias evidence window (§7): mean/max bias, sample count, and
 * the window, every figure nullable — never zero-filled. The contract pins
 * the figures; the key spellings below are the natural ones the decoder
 * reads defensively.
 */
function parseDeliveryBias(value: unknown): DeliveryBiasView | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    meanBiasPct: parseNumber(value.mean_bias_pct),
    maxBiasPct: parseNumber(value.max_bias_pct),
    sampleCount:
      typeof value.sample_count === "number" && Number.isFinite(value.sample_count)
        ? Math.max(0, value.sample_count)
        : 0,
    windowS: parseNumber(value.window_s),
  };
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
    telemetry: parseTelemetry(record.telemetry),
    inhibit,
    health: toUnitHealth(record),
    objective: toUnitObjective(record.last_objective_observed),
    park: toParkState(record.park_state),
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
    // Feature detection: an absent `energy_today` (today's backend) is null —
    // no per-card energy row renders at all.
    energyToday: isRecord(raw.energy_today) ? toEnergyToday(raw.energy_today) : null,
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

/**
 * An `audit.appended` frame as an Events-tab entry: the payload IS the audit
 * summary (event_id, event_type, unit_id, result, reason_codes), and the bus
 * envelope contributes the ordering `sequence` and the `occurred_at` stamp.
 * A live entry lands at the TOP (it is newer than every loaded REST row by
 * construction); a later REST refresh that carries the same `event_id`
 * dedupes against it.
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

/** An audit row's `event_id`, the identity a live frame and a REST row share. */
function auditEventId(event: AuditEvent): string | null {
  return typeof event.event_id === "string" && event.event_id !== "" ? event.event_id : null;
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
  /**
   * The unit's DISPLAYED data age (captured `telemetry_age_s` plus elapsed
   * monotonic time since the snapshot landed) — it ticks, so staleness can
   * arrive with time alone and never freezes between refreshes.
   */
  ageSeconds: number | null;
  /**
   * The WALL-clock now the lease countdown derives from (policy, not safety):
   * distinct from the monotonic tick that drives re-renders — a lease instant
   * is an epoch time and must never be compared against a monotonic clock.
   */
  nowWallMs: number;
  observation: ObservationTrack | undefined;
  /**
   * The snapshot's `energy_today` block (PENDING, feature-detected): null
   * when the scorecard is not composed — the card's today row renders
   * nothing at all.
   */
  today: EnergyToday | null;
  onOpenDetail: (unitId: string) => void;
  onAcknowledge: (unitId: string, opener: HTMLElement) => void;
  /** Opens the guarded park dialog (offered only where parking is commissioned). */
  onPark: (unitId: string, opener: HTMLElement) => void;
  /** Opens the resume dialog (offered only where parking is commissioned). */
  onResume: (unitId: string, opener: HTMLElement) => void;
}

/**
 * The card's today row (DESIGN_ENERGY_SCORECARD.md §8 W-C): charged and
 * discharged TODAY for THIS battery, from the day record's own per-unit
 * figures — null figures read "not available", never 0, and a unit the record
 * does not carry yet says so.
 */
function cardTodayText(today: EnergyToday, unitId: string): string {
  const unitDay: EnergyUnitDay | undefined = today.units[unitId];
  if (unitDay === undefined) {
    return "no figures for this battery yet today";
  }
  const resets =
    unitDay.metricFlags.length === 0
      ? ""
      : ` (a counter was reset during the day — the figures restart from the new baseline)`;
  return `charged ${kwhText(unitDay.batteryChargedKwh)}, discharged ${kwhText(
    unitDay.batteryDischargedKwh,
  )}${resets}`;
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
  ageSeconds,
  nowWallMs,
  observation,
  today,
  onOpenDetail,
  onAcknowledge,
  onPark,
  onResume,
}: FleetCardProps): JSX.Element {
  const dimmed = isStaleData(unit, ageSeconds);
  const imbalanceWarning = cellImbalanceWarning(unit);
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
        {/* The parked chip: telemetry's own figures in the tooltip (the mode
            word and the pack voltage), never a status metaphor. Renders only
            where the parking projection speaks (feature detection). */}
        {unit.park?.parked === true && (
          <ParkedChip
            park={unit.park}
            modeWord={unit.telemetry?.debugModeW ?? null}
            packVoltageV={unit.telemetry?.packVoltageV ?? null}
          />
        )}
      </div>
      {/* The unit banner: the pinned countdown line (alert wording at expiry)
          with the fixed not-isolation sentence always beside it. */}
      {unit.park?.parked === true && <ParkedBanner park={unit.park} nowMs={nowWallMs} />}
      {/* The self-healing awareness badge: silent while healthy (and for the
          states the inhibit surfaces already tell), quiet-positive while the
          battery manages itself, the honest terminal when recovery fails.
          The unit's own measured figure feeds the self-charge sentence's
          direction (evening lhs load-serves at positive watts while mid/rhs
          gently charge). */}
      <UnitHealthTag
        health={unit.health}
        authorizedWatts={unit.authorized_power?.watts ?? null}
        measuredWatts={unit.measured_watts}
      />
      {/* The night-writer detector's quiet line: ONLY where the detector
          classified an external writer. A quiet note, never a badge and never
          a banner — the operator reads the evidence (the pattern reason is
          the detector's own, in plain words). In-band pod autonomy renders
          NOTHING here by design: it is evidence for the Objectives view, not
          a card fact. */}
      {isForeignObjective(unit.objective) && (
        <p role="note" className="objective-foreign">
          {commandedBySomeoneElseText(unit.objective)}
        </p>
      )}
      <p>
        <b>Availability:</b> {availabilityWord(unit.lifecycle)}
      </p>
      <p>
        <b>Charge level:</b> {cardChargeText(unit, ageSeconds)}
      </p>
      <p>
        <b>Power:</b> {cardPowerText(unit)}
      </p>
      <p>
        <b>Pack voltage:</b> {cardPackVoltageText(unit, ageSeconds)}
      </p>
      <p>
        <b>Temperature:</b> {cardTemperatureText(unit, ageSeconds)}
      </p>
      <p>
        <b>Cell spread:</b> {cardCellSpreadText(unit, ageSeconds)}
      </p>
      <p>
        <b>Grid and load:</b> {cardGridLoadText(unit)}
      </p>
      {today !== null && (
        <p className="today-energy">
          <b>Today (so far):</b> {cardTodayText(today, unit.unit_id)}
        </p>
      )}
      {imbalanceWarning !== null && (
        <p role="note" className="cell-imbalance-warning">
          {imbalanceWarning}
        </p>
      )}
      <p>
        <b>Data age:</b> {ageText(ageSeconds)}
        {dimmed ? " (stale)" : ""}
      </p>
      <p>
        <b>Last observation:</b> {observationText(observation)}
      </p>
      <p className="warnings">
        <b>Warnings:</b> {cardWarningsText(unit)}
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
      {/* The park/resume affordance: offered exactly where the parking
          projection speaks (the absent key is the not-commissioned feature
          detection — a button that can only 409 is noise, not honesty). */}
      {unit.park !== null &&
        (unit.park.parked ? (
          <button
            type="button"
            className="park-action"
            onClick={(event) => onResume(unit.unit_id, event.currentTarget)}
          >
            Resume {unit.unit_id}
          </button>
        ) : (
          <button
            type="button"
            className="park-action"
            onClick={(event) => onPark(unit.unit_id, event.currentTarget)}
          >
            Park {unit.unit_id}
          </button>
        ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unit detail (tabs are keyboard-operable: arrows move focus, Enter selects)
// ---------------------------------------------------------------------------

interface UnitDetailProps {
  unit: ViewUnit;
  /** The unit's DISPLAYED (ticking) data age — the panels never re-derive it. */
  ageSeconds: number | null;
  /** The WALL-clock now the lease countdown derives from (see FleetCard). */
  nowWallMs: number;
  /** The whole fleet snapshot: a shared requested figure is a fleet total. */
  units: ViewUnit[];
  requestedByUnit: WattsByUnit | null;
  authorizedByUnit: WattsByUnit | null;
  observation: ObservationTrack | undefined;
  /** The snapshot's `energy_today` block; null = not composed. */
  today: EnergyToday | null;
  siteId: string;
  capturedAt: string;
  tab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onBack: () => void;
  onPark: (unitId: string, opener: HTMLElement) => void;
  onResume: (unitId: string, opener: HTMLElement) => void;
  auditPage: AuditPage | null;
  auditError: ErrorView | null;
  onRetryAudit: () => void;
  detailData: UnitDetailView | null;
  detailPhase: DetailPhase;
  detailError: ErrorView | null;
  onRetryDetail: () => void;
  detailDisconnected: boolean;
}

function SummaryPanel({
  unit,
  units,
  ageSeconds,
  nowWallMs,
  requestedByUnit,
  authorizedByUnit,
  observation,
  today,
  detailData,
  detailPhase,
}: {
  unit: ViewUnit;
  units: ViewUnit[];
  ageSeconds: number | null;
  nowWallMs: number;
  requestedByUnit: WattsByUnit | null;
  authorizedByUnit: WattsByUnit | null;
  observation: ObservationTrack | undefined;
  /** The snapshot's `energy_today` block; null = not composed (no surplus row). */
  today: EnergyToday | null;
  detailData: UnitDetailView | null;
  detailPhase: DetailPhase;
}): JSX.Element {
  const requested = unit.requested_power;
  const allowed = unit.authorized_power;
  const measured = unit.measured_watts;
  const actual =
    measured === null
      ? "no data"
      : `${directionWord(measured > 0 ? "discharge" : measured < 0 ? "charge" : "idle").toLowerCase()} ${formatWatts(Math.abs(measured))}`;
  const telemetry = unit.telemetry;
  const soc = telemetry?.socPct ?? null;
  const soh = telemetry?.sohPct ?? null;
  const chargeLimit = telemetry?.dynamicChargeLimitW ?? null;
  const dischargeLimit = telemetry?.dynamicDischargeLimitW ?? null;
  const identity =
    detailPhase === "loading"
      ? "loading the unit's identity…"
      : detailData === null || detailData.deviceIdentity === null
        ? "not available from this snapshot"
        : `${detailData.deviceIdentity}${
            detailData.protocolProfile === null ? "" : `, profile ${detailData.protocolProfile}`
          }`;
  // The requested figure is the battery's OWN target whenever a per-unit
  // source exists (the shared tracker's map — the intent's `watts_by_unit`,
  // seeded live by the decision summaries and cold by the snapshot `intent`
  // block). The snapshot's per-unit `requested_power` repeats the intent's
  // FLEET TOTAL once per covered unit (service.py `_requested_power`), so a
  // figure several batteries share is stated AS the fleet total, never as
  // this battery's request; a figure no other battery shares is this
  // battery's own (one battery's fleet total IS its per-battery figure).
  const mappedRequested = requestedByUnit !== null ? requestedByUnit[unit.unit_id] : undefined;
  const requestLive = requested.direction !== "idle" || requested.watts > 0;
  const sharingRequest = requestLive
    ? units.filter(
        (other) =>
          other.requested_power.direction === requested.direction &&
          other.requested_power.watts === requested.watts,
      ).length
    : 1;
  const requestedFigureText =
    mappedRequested !== undefined
      ? `${directionWord(requested.direction).toLowerCase()} ${formatWatts(mappedRequested)}`
      : sharingRequest > 1
        ? `${directionWord(requested.direction).toLowerCase()} — fleet total ${formatWatts(
            requested.watts,
          )} across ${sharingRequest} batteries (this battery's own share is not available from this snapshot)`
        : `${directionWord(requested.direction).toLowerCase()} ${formatWatts(requested.watts)}`;
  // The allowance is genuinely per-unit on the wire; the decision's map wins
  // when present (it is the same figure, captured at the decision itself).
  const mappedAuthorized =
    authorizedByUnit !== null ? authorizedByUnit[unit.unit_id] : undefined;
  const allowedWatts = mappedAuthorized ?? allowed?.watts ?? null;
  return (
    <div>
      <p>
        <b>Condition:</b> {availabilityWord(unit.lifecycle)} (data quality: {unit.quality})
      </p>
      <p>
        <b>Charge level:</b>{" "}
        {soc === null ? missingText(ageSeconds) : formatPercent(soc)}
        {soh === null ? "" : `; state of health ${formatPercent(soh)}`}
      </p>
      <p>
        <b>Power:</b> requested {requestedFigureText}; allowed{" "}
        {allowed === null && allowedWatts === null
          ? "none recorded"
          : `${directionWord(allowed?.direction ?? requested.direction).toLowerCase()} ${
              allowedWatts === null ? "no data" : formatWatts(allowedWatts)
            }`}
        ; delivering {actual}
      </p>
      <p>
        <b>Limits:</b>{" "}
        {allowed === null && allowedWatts === null
          ? "No authorized power recorded"
          : `Allowed ${directionWord(allowed?.direction ?? requested.direction).toLowerCase()} up to ${
              allowedWatts === null ? "no data" : formatWatts(allowedWatts)
            }`}
      </p>
      <p>
        <b>Device limits (dynamic):</b>{" "}
        {chargeLimit === null && dischargeLimit === null
          ? missingText(ageSeconds)
          : `charge up to ${chargeLimit === null ? "no data" : formatWatts(chargeLimit)}, discharge up to ${
              dischargeLimit === null ? "no data" : formatWatts(dischargeLimit)
            }`}
      </p>
      {today?.units[unit.unit_id]?.chargedFromSurplusKwh != null && (
        <p className="surplus-energy">
          <b>Charged from solar surplus today:</b>{" "}
          {formatKilowattHours(today.units[unit.unit_id]!.chargedFromSurplusKwh!)} — energy that
          would have been exported, captured while solar-surplus charging was active.
        </p>
      )}
      <p>
        <b>Identity:</b> {identity}
      </p>
      <p>
        <b>Communications:</b> last telemetry {ageText(ageSeconds)}, quality{" "}
        {unit.quality}; last observation {observationText(observation)}
      </p>
      {/* The night-writer detector's recorded sample — the DETAIL surface, the
          one battery-facing place in-band autonomy is visible at all (it
          never badges, never cards; the Objectives view carries the window).
          Absent (no recorded sample) renders the honest nothing. */}
      {unit.objective !== null && (
        <p className="objective-detail">
          <b>Observed objective:</b> {observedObjectiveDetailText(unit.objective)}
        </p>
      )}
      <p>
        <b>Recent trend:</b> no trend history is available from the API yet
      </p>
      {/* The parked banner on the detail surface too: the pinned countdown
          line (alert wording at expiry) with the fixed sentence beside it. */}
      {unit.park?.parked === true && <ParkedBanner park={unit.park} nowMs={nowWallMs} />}
      {/* The wedge-signature advisory (DESIGN_POD_PARKING §7): rendered where
          the unit's read carries it — and honestly UNAVAILABLE where the site
          has not commissioned parking, never a suggestion it cannot execute. */}
      {detailData?.recoveryAdvisory != null && (
        <RecoveryAdvisoryCard
          advisory={detailData.recoveryAdvisory}
          parkingComposed={unit.park !== null}
        />
      )}
      {/* The delivery-bias evidence window: evidence styling, the
          evidence-only label, never a warning. */}
      {detailData?.deliveryBias != null && (
        <DeliveryBiasReadout bias={detailData.deliveryBias} />
      )}
      <LifetimeEnergyReadthroughs detailData={detailData} detailPhase={detailPhase} />
    </div>
  );
}

/**
 * The six cumulative counter readthroughs on the unit's detail rows
 * (DESIGN_ENERGY_SCORECARD.md §8 W-C): "lifetime through this pod", raw and
 * readthrough-style. The grid pair keeps its NEUTRAL A/B names — which
 * counter is "bought" is never labeled here (the wire's own naming; the
 * site's counter-role confirmation lives on Insights) — and the PV counter
 * carries its unwired-inputs note so it can never read as the site's solar
 * production. Nulls read "not available", never 0.
 */
function LifetimeEnergyReadthroughs({
  detailData,
  detailPhase,
}: {
  detailData: UnitDetailView | null;
  detailPhase: DetailPhase;
}): JSX.Element | null {
  if (detailPhase === "loading") {
    return (
      <p className="lifetime-energy">
        <b>Lifetime energy through this pod:</b> loading…
      </p>
    );
  }
  if (detailData === null) {
    return (
      <p className="lifetime-energy">
        <b>Lifetime energy through this pod:</b> not available from this read
      </p>
    );
  }
  return (
    <div className="lifetime-energy">
      <p>
        <b>Lifetime energy through this pod</b> (raw device counters, readthrough):
      </p>
      <ul>
        <li>
          {GRID_COUNTER_A_LABEL}: {kwhText(detailData.energyGridAKwh)}
        </li>
        <li>
          {GRID_COUNTER_B_LABEL}: {kwhText(detailData.energyGridBKwh)}
        </li>
        <li>House load: {kwhText(detailData.energyLoadKwh)}</li>
        <li>
          Solar (PV) counter: {kwhText(detailData.energyPvKwh)} — {PV_READTHROUGH_NOTE}
        </li>
        <li>Battery charged: {kwhText(detailData.energyChargeKwh)}</li>
        <li>Battery discharged: {kwhText(detailData.energyDischargeKwh)}</li>
      </ul>
      <p className="lifetime-energy-note">
        The two grid counters are shown as counter A and counter B — which one is &quot;bought&quot;
        is not labeled here; see Insights for the site&apos;s counter-role confirmation.
      </p>
    </div>
  );
}

/** Present-or-missing per field of the snapshot's telemetry summary block. */
function summaryCompleteness(telemetry: TelemetryView): Record<string, string> {
  return {
    "state of charge": telemetry.socPct === null ? "missing" : "present",
    "state of health": telemetry.sohPct === null ? "missing" : "present",
    "pack voltage": telemetry.packVoltageV === null ? "missing" : "present",
    "pack current": telemetry.packCurrentA === null ? "missing" : "present",
    "battery power": telemetry.batteryWatts === null ? "missing" : "present",
    "dynamic limits":
      telemetry.dynamicChargeLimitW === null && telemetry.dynamicDischargeLimitW === null
        ? "missing"
        : "present",
    "cell readings": telemetry.cellCount === null ? "missing" : "present",
    temperatures:
      telemetry.temperatureMinC === null && telemetry.temperatureMaxC === null
        ? "missing"
        : "present",
    faults: telemetry.activeFaults === null ? "missing" : "present",
    warnings: telemetry.activeWarnings === null ? "missing" : "present",
  };
}

/** Data completeness from the detail's own arrays and quality map. */
function completenessText(detail: UnitDetailView): string {
  const cells = detail.cellVoltages.length;
  const expectedCells = detail.telemetry.cellCount;
  const temps = detail.temperatures.length;
  const qualityEntries = Object.entries(detail.quality);
  const notGood = qualityEntries.filter(([, word]) => word !== "good");
  if (cells === 0 && temps === 0 && qualityEntries.length === 0) {
    return "no cell or temperature readings are available for this unit yet";
  }
  const parts: string[] = [];
  parts.push(
    expectedCells === null
      ? `${cells} cell voltages reported`
      : `${cells} of ${expectedCells} cells reporting`,
  );
  parts.push(`${temps} temperature reading${temps === 1 ? "" : "s"}`);
  if (qualityEntries.length === 0) {
    parts.push("quality map not carried");
  } else if (notGood.length === 0) {
    parts.push("all telemetry fields good quality");
  } else {
    parts.push(
      `quality: ${notGood.map(([field, word]) => `${field} ${word}`).join(", ")}`,
    );
  }
  return parts.join("; ");
}

function CellsPanel({
  ageSeconds,
  detailData,
  detailPhase,
  detailError,
  onRetry,
  disconnected,
}: {
  ageSeconds: number | null;
  detailData: UnitDetailView | null;
  detailPhase: DetailPhase;
  detailError: ErrorView | null;
  onRetry: () => void;
  disconnected: boolean;
}): JSX.Element {
  const age = ageSeconds;
  if (detailPhase === "loading") {
    return (
      <div>
        <p role="status" aria-label="Loading cell detail">
          Loading the cell and temperature detail…
        </p>
      </div>
    );
  }
  if (detailData === null) {
    if (detailError !== null) {
      return (
        <div role="alert">
          <p>{detailError.code}</p>
          <p>{detailError.message}</p>
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      );
    }
    return (
      <p>No cell detail is available for this unit right now. Open the unit again to retry.</p>
    );
  }
  const telemetry = detailData.telemetry;
  const cellMin = telemetry.cellMinV;
  const cellMax = telemetry.cellMaxV;
  const spread = telemetry.cellSpreadMv;
  const tempMin = telemetry.temperatureMinC;
  const tempMax = telemetry.temperatureMaxC;
  return (
    <div>
      {disconnected && (
        <p role="status" className="disconnected">
          Disconnected from live updates — showing the last known cell readings.
        </p>
      )}
      <p>
        <b>Minimum cell voltage:</b>{" "}
        {cellMin === null ? missingText(age) : formatVolts(cellMin)}
      </p>
      <p>
        <b>Maximum cell voltage:</b>{" "}
        {cellMax === null ? missingText(age) : formatVolts(cellMax)}
      </p>
      <p>
        <b>Voltage spread:</b> {spread === null ? missingText(age) : formatMillivolts(spread)}
      </p>
      <p>
        <b>Temperature range:</b>{" "}
        {tempMin === null && tempMax === null
          ? missingText(age)
          : `${tempMin === null ? "no data" : formatTemp(tempMin)} to ${
              tempMax === null ? "no data" : formatTemp(tempMax)
            }`}
      </p>
      {detailData.cellVoltages.length > 0 ? (
        <ul className="cell-grid" aria-label="Cell voltages">
          {detailData.cellVoltages.map((volts, index) => (
            <li key={index}>{`Cell ${index + 1}: ${formatVolts(volts)}`}</li>
          ))}
        </ul>
      ) : (
        <p>No individual cell voltages are carried by this reading.</p>
      )}
      {detailData.temperatures.length > 0 ? (
        <ul className="temp-grid" aria-label="Temperature sensors">
          {detailData.temperatures.map((celsius, index) => (
            <li key={index}>{`Sensor ${index + 1}: ${formatTemp(celsius)}`}</li>
          ))}
        </ul>
      ) : (
        <p>No individual temperature readings are carried by this observation.</p>
      )}
      <p>
        <b>Data completeness:</b> {completenessText(detailData)} ({ageText(age)}).
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
  ageSeconds,
  observation,
  siteId,
  capturedAt,
  detailData,
}: {
  unit: ViewUnit;
  ageSeconds: number | null;
  observation: ObservationTrack | undefined;
  siteId: string;
  capturedAt: string;
  detailData: UnitDetailView | null;
}): JSX.Element {
  const observationEpoch =
    observation?.connectionEpoch === null || observation === undefined
      ? "not available"
      : String(observation.connectionEpoch);
  const detailEpoch =
    detailData === null || detailData.connectionEpoch === null
      ? "not available"
      : String(detailData.connectionEpoch);
  const detailSequence = detailData === null ? null : detailData.sequence;
  const detailCellSequence = detailData === null ? null : detailData.cellSequence;
  const qualityMap = detailData === null ? null : detailData.quality;
  const qualityText =
    qualityMap === null
      ? "not carried by this reading"
      : Object.entries(qualityMap)
          .map(([field, word]) => `${field}: ${word}`)
          .join(", ");
  return (
    <div>
      <p>
        <b>Data quality:</b> {unit.quality}
        {isStaleData(unit, ageSeconds) ? " (stale)" : ""}
      </p>
      <p>
        <b>Telemetry age:</b> {ageText(ageSeconds)}
      </p>
      <p>
        <b>Observation detail:</b> last observation {observationText(observation)}; connection
        epoch {observationEpoch}; observation connection epoch {detailEpoch}
        {detailSequence === null ? "" : `; telemetry sequence ${detailSequence}`}
        {detailCellSequence === null ? "" : `; cell sequence ${detailCellSequence}`}
      </p>
      <p>
        <b>Measurement completeness:</b>{" "}
        {unit.measured_watts === null ? "measured power missing" : "measured power present"};
        {unit.telemetry === null
          ? " telemetry summary not carried by this snapshot"
          : ` telemetry summary carried (${Object.entries(
              summaryCompleteness(unit.telemetry),
            )
              .map(([field, word]) => `${field} ${word}`)
              .join(", ")})`}
      </p>
      <p>
        <b>Per-field quality map:</b> {qualityText}
      </p>
      <p>
        <b>Unit ID:</b> {unit.unit_id}
      </p>
      <p>
        <b>Mode word (debug):</b>{" "}
        {unit.telemetry?.debugModeW == null
          ? "not available"
          : `${unit.telemetry.debugModeW}${
              unit.telemetry.debugModeW === 1
                ? " (Standby)"
                : unit.telemetry.debugModeW === 0
                  ? " (Normal)"
                  : ""
            }`}
        {unit.park?.foreignMode != null && unit.park.foreignMode.name !== ""
          ? ` — vendor mode ${unit.park.foreignMode.name} (word ${unit.park.foreignMode.word}), unexposed by this controller`
          : ""}
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
  ageSeconds,
  nowWallMs,
  units,
  requestedByUnit,
  authorizedByUnit,
  observation,
  today,
  siteId,
  capturedAt,
  tab,
  onTabChange,
  onBack,
  onPark,
  onResume,
  auditPage,
  auditError,
  onRetryAudit,
  detailData,
  detailPhase,
  detailError,
  onRetryDetail,
  detailDisconnected,
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
      {/* The parked chip + banner on the detail surface: the same pinned
          surfaces the card renders, on the view that stays open. */}
      {unit.park?.parked === true && (
        <ParkedChip
          park={unit.park}
          modeWord={unit.telemetry?.debugModeW ?? null}
          packVoltageV={unit.telemetry?.packVoltageV ?? null}
        />
      )}
      {unit.park !== null &&
        (unit.park.parked ? (
          <button
            type="button"
            className="park-action"
            onClick={(event) => onResume(unit.unit_id, event.currentTarget)}
          >
            Resume {unit.unit_id}
          </button>
        ) : (
          <button
            type="button"
            className="park-action"
            onClick={(event) => onPark(unit.unit_id, event.currentTarget)}
          >
            Park {unit.unit_id}
          </button>
        ))}
      {unit.park?.parked === true && <ParkedBanner park={unit.park} nowMs={nowWallMs} />}
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
        {tab === "summary" && (
          <SummaryPanel
            unit={unit}
            units={units}
            ageSeconds={ageSeconds}
            nowWallMs={nowWallMs}
            requestedByUnit={requestedByUnit}
            authorizedByUnit={authorizedByUnit}
            observation={observation}
            today={today}
            detailData={detailData}
            detailPhase={detailPhase}
          />
        )}
        {tab === "cells" && (
          <CellsPanel
            ageSeconds={ageSeconds}
            detailData={detailData}
            detailPhase={detailPhase}
            detailError={detailError}
            onRetry={onRetryDetail}
            disconnected={detailDisconnected}
          />
        )}
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
            ageSeconds={ageSeconds}
            observation={observation}
            siteId={siteId}
            capturedAt={capturedAt}
            detailData={detailData}
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
    <div className="dialog-backdrop">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="inhibit-ack-title"
        className="dialog"
        onKeyDown={handleKeyDown}
      >
        {/* The view family's pinned-footer pattern (see BatteriesView.css):
            the reason and any refusal scroll in the body, the actions pin. */}
        <div className="dialog-body">
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
        </div>
        <div className="dialog-footer">
          <div className="dialog-actions">
            <button type="button" onClick={onCancel} disabled={pending}>
              Cancel
            </button>
            <button type="button" onClick={onConfirm} disabled={pending}>
              Confirm acknowledgement
            </button>
          </div>
        </div>
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
  /**
   * The on-demand unit-detail read (GET /api/v1/units/{id}): fetched when a
   * unit is opened, with its own loading / error phases so the Cells and
   * Summary tabs can name exactly where their facts came from.
   */
  const [detailData, setDetailData] = useState<UnitDetailView | null>(null);
  const [detailPhase, setDetailPhase] = useState<DetailPhase>("loading");
  const [detailError, setDetailError] = useState<ErrorView | null>(null);
  /**
   * The guarded parking actions (DESIGN_POD_PARKING §8): one dialog open at a
   * time, its refusal rendered beside the envelope verbatim, and the opener
   * element kept so focus returns when the dialog closes. The park dialog's
   * confirm stays disabled until the operator types the unit id; the resume
   * dialog grows the foreign-takeover acknowledgement step exactly when the
   * park wasn't ours.
   */
  const [parkUnitId, setParkUnitId] = useState<string | null>(null);
  const [parkError, setParkError] = useState<ParkRefusalView | null>(null);
  const [parkPending, setParkPending] = useState(false);
  const [resumeUnitId, setResumeUnitId] = useState<string | null>(null);
  const [resumeError, setResumeError] = useState<ParkRefusalView | null>(null);
  const [resumePending, setResumePending] = useState(false);
  /** The last resume's checklist, rendered inline until the operator dismisses it. */
  const [checklist, setChecklist] = useState<{ unitId: string; checklist: ResumeChecklistView } | null>(null);
  const parkOpenerRef = useRef<HTMLElement | null>(null);

  const cursorRef = useRef<number | undefined>(undefined);
  const streamStartedRef = useRef(false);
  const openerRef = useRef<HTMLElement | null>(null);
  /**
   * The monotonic moment the picture on screen was captured: every displayed
   * data age is the snapshot's own `telemetry_age_s` PLUS elapsed time from
   * here, so an age keeps growing between refreshes instead of freezing at
   * the value the last snapshot arrived with.
   */
  const capturedAtRef = useRef<number>(monotonicNowMs());
  /**
   * The open detail, by reference: the stream effect below re-reads the
   * unit's full projection whenever the world advances while its detail is
   * open, without re-subscribing per open/close.
   */
  const detailRef = useRef<{ unitId: string; tab: TabKey } | null>(null);
  detailRef.current = detail;
  /** One detail read in flight at a time (the cadence can outrun a slow read). */
  const detailReadInFlightRef = useRef(false);
  /**
   * The live request's per-unit figures — the ONE shared tracker
   * (web/src/app/useUnitIntentFigures.ts): the intent's own `watts_by_unit`
   * and the decision's `authorized_watts_by_unit` from the stream, seeded for
   * a cold load by the snapshot's `intent` block when the backend sends one
   * (feature-detected; an absent block keeps the snapshot fallbacks). The
   * Summary figures below render a battery's OWN target from it and state a
   * shared snapshot figure AS the fleet total — never one battery's request.
   */
  const unitFigures = useUnitIntentFigures();
  const adoptFigures = unitFigures.adoptSnapshot;
  const consumeFigures = unitFigures.consumeEvent;

  const applySnapshot = useCallback(
    (raw: unknown): void => {
      const parsed = parseSnapshot(raw);
      if (parsed === null) {
        return;
      }
      adoptFigures(raw);
      capturedAtRef.current = monotonicNowMs();
      setFleet(parsed);
      if (!streamStartedRef.current) {
        cursorRef.current = parsed.sequence;
      }
    },
    [adoptFigures],
  );

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

  /**
   * A live recovery transition (unit.health_changed, or the badge state a
   * watchdog detection implies): patch the one unit's health state-locally —
   * the badge moves without waiting for a poll, and the periodic snapshot
   * read stays the reconciler. The merge keeps the snapshot's remediation
   * hint while the state is unchanged (bus transitions carry no hint).
   */
  const patchHealth = useCallback((unitId: string, patch: UnitHealth): void => {
    setFleet((previous) =>
      previous === null
        ? previous
        : {
            ...previous,
            units: previous.units.map((unit) =>
              unit.unit_id === unitId
                ? { ...unit, health: applyHealthPatch(unit.health, patch) }
                : unit,
            ),
          },
    );
  }, []);

  /**
   * The night-writer detector's alert frame (foreign_objective.observed): the
   * unit's observed-objective summary moves NOW, state-locally, so the quiet
   * line follows the alert — no refetch, no announcement (the periodic
   * snapshot read carries the detector's own summary once composed).
   */
  const patchObjective = useCallback((unitId: string, objective: UnitObjective): void => {
    setFleet((previous) =>
      previous === null
        ? previous
        : {
            ...previous,
            units: previous.units.map((unit) =>
              unit.unit_id === unitId ? { ...unit, objective } : unit,
            ),
          },
    );
  }, []);

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

  /**
   * The on-demand unit-detail read; the server alone says what it carries.
   * A SILENT read (the live refresh while the detail is open) never flashes
   * a loading phase over data that is already on screen and never replaces
   * it with an error on a transient failure — the next world advance retries.
   */
  const fetchUnitDetail = useCallback(
    async (unitId: string, options: { silent?: boolean } = {}): Promise<void> => {
      const silent = options.silent === true;
      if (!silent) {
        setDetailPhase("loading");
        setDetailError(null);
      }
      try {
        const raw = await client.getUnitDetail(unitId);
        const parsed = parseUnitDetail(raw);
        if (parsed === null) {
          if (!silent) {
            setDetailPhase("error");
            setDetailError({
              code: "unreadable_unit_detail",
              message: "The unit detail response could not be read.",
              request_id: null,
            });
          }
          return;
        }
        setDetailData(parsed);
        setDetailPhase("ready");
      } catch (error) {
        if (!silent) {
          setDetailPhase("error");
          setDetailError(toErrorView(error));
        }
      }
    },
    [client],
  );

  /**
   * The detail reader by reference: the stream effect re-reads the open
   * unit's full projection whenever the world advances, without depending on
   * (and re-subscribing for) the reader or the open detail.
   */
  const fetchUnitDetailRef = useRef(fetchUnitDetail);
  useEffect(() => {
    fetchUnitDetailRef.current = fetchUnitDetail;
  }, [fetchUnitDetail]);

  // Opening a unit fetches its full projection; the live path below keeps it
  // current while it stays open, and the retry path re-runs it.
  const openUnitId = detail === null ? null : detail.unitId;
  useEffect(() => {
    if (openUnitId === null) {
      return;
    }
    void fetchUnitDetail(openUnitId);
  }, [openUnitId, fetchUnitDetail]);

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
            // A plane-republished REST read is data, never liveness: the plane
            // marks its cached replay `stale` exactly while the one real
            // stream is down, and honoring that flag is what keeps the
            // disconnected notice on screen through the reconnect retries —
            // an unconditional clear here used to flip the view back to
            // "connected" ~300 ms into every outage (Home/Now/Flow's rule).
            if (!(isPlaneSnapshot(frame) && frame.stale)) {
              setStreamLost(false);
            }
            // Every frame feeds the shared per-unit figure tracker (its own
            // no-ops carry most kinds): acceptances, control-decision audits,
            // and authority grants move the maps, request-ending frames clear
            // their units.
            consumeFigures(frame);
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
              // The world advanced: a detail that is open re-reads its full
              // projection, so the Cells tab's arrays and the Summary tab's
              // identity never sit frozen at the open-time picture while the
              // unit keeps publishing (its age line alone would tick — the
              // classic illusion of updating).
              const open = detailRef.current;
              if (open !== null && !detailReadInFlightRef.current) {
                detailReadInFlightRef.current = true;
                void fetchUnitDetailRef
                  .current(open.unitId, { silent: true })
                  .catch(() => undefined)
                  .finally(() => {
                    detailReadInFlightRef.current = false;
                  });
              }
            } else if (frame.type === "observation.published") {
              const parsed = parseObservationPublished(frame);
              if (parsed !== null) {
                setObservations((previous) => ({ ...previous, [parsed.unitId]: parsed.track }));
              }
            } else if (frame.type === "unit.health_changed") {
              // A live recovery transition: the card's health badge moves
              // now, from the frame — the snapshot read stays the reconciler.
              const transition = toHealthChangedEvent(frame.payload);
              if (transition !== null) {
                patchHealth(transition.unitId, {
                  state: transition.to,
                  reasons: transition.reasons,
                  remediationHint: null,
                });
              }
            } else if (frame.type === "actuation.incoherent") {
              // The watchdog verdict: the badge state moves to the warning
              // even before the backend's own health_changed arrives, and the
              // echo classification (on the follow-up frame of the same type)
              // rides the reasons exactly as the backend derives them.
              const detection = toActuationIncoherentEvent(frame.payload);
              if (detection !== null) {
                patchHealth(detection.unitId, {
                  state: "actuation_incoherent",
                  reasons:
                    detection.echoClassification === null
                      ? ["authorized_not_actuating"]
                      : ["authorized_not_actuating", detection.echoClassification],
                  remediationHint: null,
                });
              }
            } else if (frame.type === "foreign_objective.observed") {
              // The night-writer detector's ALERT TIER: the quiet line moves
              // NOW from the frame (the event type is the detector's foreign
              // assertion); quiet evidence never publishes — it reaches this
              // view only through the snapshot's own summary.
              const detection = toForeignObjectiveEvent(frame.payload);
              if (detection !== null) {
                patchObjective(detection.unitId, objectiveFromForeignEvent(detection));
              }
            } else if (frame.type === "audit.appended") {
              // The Events tab is a LIVE surface: a durable fact the backend
              // just appended lands on the timeline now, not at the next
              // mount. Deduped by the event_id a later REST page shares.
              const entry = auditEntryFromFrame(frame);
              const id = entry !== null ? auditEventId(entry) : null;
              if (entry !== null) {
                setAuditPage((previous) => {
                  if (
                    previous !== null &&
                    id !== null &&
                    previous.events.some((event) => auditEventId(event) === id)
                  ) {
                    return previous;
                  }
                  return {
                    events: previous === null ? [entry] : [entry, ...previous.events],
                    next_cursor: previous === null ? null : previous.next_cursor,
                  };
                });
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
  }, [applySnapshot, client, streamOn, consumeFigures, patchHealth, patchObjective]);

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

  // --- the guarded parking actions --------------------------------------------

  /** A parking refusal as the dialog renders it: plain sentence + envelope. */
  const asParkRefusal = (error: unknown): ParkRefusalView => {
    if (error instanceof ApiClientError) {
      return { code: error.code, message: error.message, details: error.details };
    }
    return {
      code: "unexpected_error",
      message: "Something went wrong while talking to the EnergyPod service.",
      details: null,
    };
  };

  const openPark = (unitId: string, opener: HTMLElement): void => {
    parkOpenerRef.current = opener;
    setParkError(null);
    setParkPending(false);
    setParkUnitId(unitId);
  };

  const cancelPark = (): void => {
    setParkUnitId(null);
    setParkError(null);
    setParkPending(false);
    parkOpenerRef.current?.focus();
  };

  const confirmPark = (reason: string, leaseS: number): void => {
    if (parkUnitId === null || parkPending) {
      return;
    }
    setParkPending(true);
    setParkError(null);
    const unitId = parkUnitId;
    void (async () => {
      try {
        await client.postPark(unitId, { reason, leaseS });
        const opener = parkOpenerRef.current;
        setParkUnitId(null);
        setParkPending(false);
        opener?.focus();
        // Post-park state is server authority: the chip, the banner, and the
        // lease arrive through the refetched world, never optimistic local
        // state. An open detail re-reads its full projection silently.
        await loadSnapshot();
        if (detailRef.current?.unitId === unitId && !detailReadInFlightRef.current) {
          detailReadInFlightRef.current = true;
          void fetchUnitDetailRef
            .current(unitId, { silent: true })
            .catch(() => undefined)
            .finally(() => {
              detailReadInFlightRef.current = false;
            });
        }
      } catch (error) {
        setParkPending(false);
        setParkError(asParkRefusal(error));
      }
    })();
  };

  const openResume = (unitId: string, opener: HTMLElement): void => {
    parkOpenerRef.current = opener;
    setResumeError(null);
    setResumePending(false);
    setResumeUnitId(unitId);
  };

  const cancelResume = (): void => {
    setResumeUnitId(null);
    setResumeError(null);
    setResumePending(false);
    parkOpenerRef.current?.focus();
  };

  const confirmResume = (takeover: boolean): void => {
    if (resumeUnitId === null || resumePending) {
      return;
    }
    setResumePending(true);
    setResumeError(null);
    const unitId = resumeUnitId;
    void (async () => {
      try {
        const body = await client.postResume(unitId, takeover ? { takeover: true } : {});
        const record = body as Record<string, unknown>;
        const list = toResumeChecklist(record.checklist);
        if (list !== null) {
          setChecklist({ unitId, checklist: list });
        }
        const opener = parkOpenerRef.current;
        setResumeUnitId(null);
        setResumePending(false);
        opener?.focus();
        await loadSnapshot();
        if (detailRef.current?.unitId === unitId && !detailReadInFlightRef.current) {
          detailReadInFlightRef.current = true;
          void fetchUnitDetailRef
            .current(unitId, { silent: true })
            .catch(() => undefined)
            .finally(() => {
              detailReadInFlightRef.current = false;
            });
        }
      } catch (error) {
        // The refusal stays in the dialog: a takeover requirement grows the
        // acknowledgement step (the 409 IS the routing); every other refusal
        // renders its plain sentence beside the envelope verbatim.
        setResumePending(false);
        setResumeError(asParkRefusal(error));
      }
    })();
  };

  // Displayed data ages are captured values plus elapsed monotonic time: the
  // shared plane republishes a fresh snapshot every couple of seconds while
  // the session is healthy, and between refreshes (and through any outage)
  // the age keeps ticking instead of freezing at the captured figure. A
  // parked banner's lease countdown rides the same once-a-second clock —
  // policy time that must read as running, never frozen.
  const needsAgeTick =
    (fleet?.units.some((unit) => unit.telemetry_age_s !== null) ?? false) ||
    (fleet?.units.some((unit) => unit.park?.parked === true) ?? false);
  const nowMs = useTickingNow(needsAgeTick);
  // The wall-clock now the parked banners' lease countdowns derive from: the
  // monotonic tick above only drives WHEN this line recomputes — the value it
  // needs is an epoch instant, never a monotonic reading.
  const nowWallMs = Date.now();
  const elapsedSeconds = Math.max(0, (nowMs - capturedAtRef.current) / 1000);
  /** A unit's displayed data age: the captured age plus elapsed time. The
   * row's own `ageText` lands on whole seconds (the display bound's rule). */
  const ageOf = (unit: ViewUnit): number | null =>
    unit.telemetry_age_s === null ? null : unit.telemetry_age_s + elapsedSeconds;

  if (fleet === null) {
    if (phase === "error" && snapshotError !== null) {
      return (
        <div className="batteries-view">
          <div role="alert" className="error-panel">
            <h2>Battery snapshot unavailable</h2>
            <p>{snapshotError.code}</p>
            <p>{snapshotError.message}</p>
            <button type="button" onClick={retrySnapshot}>
              Try again
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
            ageSeconds={ageOf(unit)}
            nowWallMs={nowWallMs}
            observation={observations[unit.unit_id]}
            today={fleet?.energyToday ?? null}
            onOpenDetail={(unitId) => setDetail({ unitId, tab: "summary" })}
            onAcknowledge={openAcknowledge}
            onPark={openPark}
            onResume={openResume}
          />
        ))}
      </div>
      {/* The last resume's checklist, rendered inline until dismissed: the
          after-park facts the operator reads before trusting the pod again. */}
      {checklist !== null && (
        <ResumeChecklistCard
          unitId={checklist.unitId}
          checklist={checklist.checklist}
          onDismiss={() => setChecklist(null)}
        />
      )}
      {detail !== null &&
        (detailUnit === undefined ? (
          <p role="status">This unit is no longer in the current snapshot.</p>
        ) : (
          <UnitDetail
            key={detailUnit.unit_id}
            unit={detailUnit}
            ageSeconds={ageOf(detailUnit)}
            nowWallMs={nowWallMs}
            units={units}
            requestedByUnit={unitFigures.requestedByUnit}
            authorizedByUnit={unitFigures.authorizedByUnit}
            observation={observations[detailUnit.unit_id]}
            today={fleet?.energyToday ?? null}
            siteId={fleet?.siteId ?? ""}
            capturedAt={fleet?.capturedAt ?? ""}
            tab={detail.tab}
            onTabChange={(tab) =>
              setDetail((previous) => (previous === null ? previous : { ...previous, tab }))
            }
            onBack={() => setDetail(null)}
            onPark={openPark}
            onResume={openResume}
            auditPage={auditPage}
            auditError={auditError}
            onRetryAudit={() => void fetchAudit()}
            detailData={detailData}
            detailPhase={detailPhase}
            detailError={detailError}
            onRetryDetail={() => {
              if (openUnitId !== null) {
                void fetchUnitDetail(openUnitId);
              }
            }}
            detailDisconnected={connection === "disconnected" || streamLost}
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
      {parkUnitId !== null &&
        (() => {
          const unit = units.find((entry) => entry.unit_id === parkUnitId);
          return (
            <ParkDialog
              unitId={parkUnitId}
              park={unit?.park ?? null}
              error={parkError}
              pending={parkPending}
              onCancel={cancelPark}
              onConfirm={confirmPark}
            />
          );
        })()}
      {resumeUnitId !== null &&
        (() => {
          const unit = units.find((entry) => entry.unit_id === resumeUnitId);
          return (
            <ResumeDialog
              unitId={resumeUnitId}
              park={unit?.park ?? null}
              error={resumeError}
              pending={resumePending}
              onCancel={cancelResume}
              onConfirm={confirmResume}
            />
          );
        })()}
    </div>
  );
}
