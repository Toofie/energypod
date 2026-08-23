/**
 * Shared fleet model for the shell.
 *
 * The service serializes enums as lowercase StrEnum values (UI_CONTRACTS.md
 * "Wire casing"): lifecycle "boot"/"observe_only"/"disarmed"/"armed_idle"/
 * "active"/"inhibited"/"stopping"/"disconnected". Everything in this module
 * works in those wire values; the human-capitalized labels live next to them
 * and are the only strings shown to operators.
 */
import { formatWatts } from "../lib/format";
export type Lifecycle =
  | "boot"
  | "observe_only"
  | "disarmed"
  | "armed_idle"
  | "active"
  | "inhibited"
  | "stopping"
  | "disconnected";

const LIFECYCLES: readonly Lifecycle[] = [
  "boot",
  "observe_only",
  "disarmed",
  "armed_idle",
  "active",
  "inhibited",
  "stopping",
  "disconnected",
];

/** Human label for a unit lifecycle; capitalized for display, never wire. */
export const UNIT_LABELS: Record<Lifecycle, string> = {
  boot: "Starting up",
  observe_only: "Observe only",
  disarmed: "Disarmed",
  armed_idle: "Armed",
  active: "Active",
  inhibited: "Inhibited",
  stopping: "Stopping",
  disconnected: "No contact",
};

export interface PowerFigure {
  direction: string;
  watts: number;
}

// --- per-unit watt figures (the wire's native `watts_by_unit` form) -----------

/**
 * The wire's per-unit watt map: `watts_by_unit` on intents, and
 * `requested_watts_by_unit` / `authorized_watts_by_unit` on control-decision
 * audit rows and their `audit.appended` bus summaries. One non-negative
 * figure per unit id; the map's key set equals the intent's selected units
 * exactly (rest.py refuses a missing or extra key before the service runs).
 * A null map on the wire is a real fact — the intent was scalar (a fleet
 * total) or the decision minted no batch — never an empty object standing in
 * for "nothing".
 */
export type WattsByUnit = Record<string, number>;

/**
 * The wire's per-unit direction map (`directions_by_unit` on a composed
 * control-decision audit row — a concurrent cycle may run opposite directions
 * on different units — and on the snapshot's `intent` block). One lowercase
 * wire direction per unit id.
 */
export type DirectionsByUnit = Record<string, string>;

/**
 * Narrow a wire per-unit watt map. Absent, non-object, or empty values narrow
 * to null; any non-finite or negative figure rejects the whole map (a map the
 * console cannot trust is rendered not at all, never partially).
 */
export function toWattsByUnit(value: unknown): WattsByUnit | null {
  if (!isRecord(value)) {
    return null;
  }
  const map: WattsByUnit = {};
  for (const [unitId, watts] of Object.entries(value)) {
    if (typeof watts !== "number" || !Number.isFinite(watts) || watts < 0) {
      return null;
    }
    map[unitId] = watts;
  }
  return Object.keys(map).length > 0 ? map : null;
}

/** Narrow a wire per-unit direction map (same rule as the watt maps). */
export function toDirectionsByUnit(value: unknown): DirectionsByUnit | null {
  if (!isRecord(value)) {
    return null;
  }
  const map: DirectionsByUnit = {};
  for (const [unitId, direction] of Object.entries(value)) {
    if (typeof direction !== "string") {
      return null;
    }
    map[unitId] = direction;
  }
  return Object.keys(map).length > 0 ? map : null;
}

/**
 * The request figures the wire attaches to one accepted intent: the fleet
 * total (always present — the facade derives it from the per-unit targets as
 * their sum) plus the per-unit targets when the intent used the per-unit form.
 * `wattsByUnit` is null for scalar intents: the two watt forms are mutually
 * exclusive on the wire, so "no map" is the scalar intent's own shape.
 */
export interface IntentFigures {
  direction: string;
  watts: number;
  wattsByUnit: WattsByUnit | null;
}

/**
 * Narrow the figures an `intent.accepted` payload or the 202 acceptance view's
 * `requested` projection carries (service.py `submit_intent`: `direction` and
 * `watts` always, `watts_by_unit` only when the per-unit form was submitted).
 */
export function toIntentFigures(value: unknown): IntentFigures | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.direction !== "string" || typeof value.watts !== "number") {
    return null;
  }
  return {
    direction: value.direction,
    watts: value.watts,
    wattsByUnit: toWattsByUnit(value.watts_by_unit),
  };
}

/**
 * The per-unit breakdowns a `control_decision` audit row (or its
 * `audit.appended` bus summary) carries: the intent's own targets
 * (`requested`, null for scalar intents) and the decision's per-unit
 * authorized watts (`authorized`, null when no batch was minted).
 */
export interface AuditUnitWatts {
  requested: WattsByUnit | null;
  authorized: WattsByUnit | null;
}

/** Narrow both per-unit maps off one audit payload or row. */
export function toAuditUnitWatts(value: unknown): AuditUnitWatts {
  if (!isRecord(value)) {
    return { requested: null, authorized: null };
  }
  return {
    requested: toWattsByUnit(value.requested_watts_by_unit),
    authorized: toWattsByUnit(value.authorized_watts_by_unit),
  };
}

// --- the snapshot's intent block (cold-load exactness, feature-detected) ------

/**
 * The snapshot-level `intent` block the backend contract adds: the live
 * request's own per-unit figures, so a console opened mid-intent holds the
 * exact per-battery truth instead of the snapshot's repeated fleet total. The
 * field is ABSENT on today's wire — absence is the feature detection and every
 * consumer keeps its current fallbacks; `null` means "no active request".
 */
export interface SnapshotIntentFigures {
  requestedByUnit: WattsByUnit | null;
  authorizedByUnit: WattsByUnit | null;
  directionsByUnit: DirectionsByUnit | null;
}

/**
 * Narrow the block's OBJECT form (the `intent` value once it is known to be a
 * record). Every map is nullable inside the block — a scalar intent carries no
 * requested map, a not-yet-decided request no authorized map.
 */
export function toSnapshotIntentFigures(value: unknown): SnapshotIntentFigures {
  if (!isRecord(value)) {
    return { requestedByUnit: null, authorizedByUnit: null, directionsByUnit: null };
  }
  return {
    requestedByUnit: toWattsByUnit(value.requested_watts_by_unit),
    authorizedByUnit: toWattsByUnit(value.authorized_watts_by_unit),
    directionsByUnit: toDirectionsByUnit(value.directions_by_unit),
  };
}

// --- the excess-solar adviser projection (DESIGN_EXCESS_ACTIVATION.md §1) -----

export type AdviserEnabledOrigin = "config" | "runtime";
export type AdviserHysteresisState = "inactive" | "entering" | "holding" | "exiting";
export type AdviserExportEvidence = "good" | "missing" | "bad" | "stale";
export type AdviserTickAction = "idle" | "propose" | "renew" | "withdraw";

/**
 * The adviser's ONE pinned reason vocabulary, verbatim from the design contract
 * §1: the implemented tick decision codes plus the projection-only states the
 * tick alone cannot see. The console's plain sentences map from these codes —
 * the codes are the wire. Renderers must treat every code in this list (an
 * unknown code is rendered honestly, never silently dropped).
 */
export const ADVISER_REASON_CODES: readonly string[] = [
  "disabled_by_config",
  "disabled_by_runtime",
  "economics_acknowledgement_required",
  "export_evidence_missing",
  "export_evidence_bad",
  "export_evidence_stale",
  "no_export_headroom",
  "no_acceleration_over_autonomy",
  "below_exit_hysteresis",
  "no_eligible_target",
  "yielding_to_higher_priority",
  "export_headroom_available",
];

/**
 * The snapshot's top-level `adviser_state` projection (the design contract §1):
 * the excess-solar adviser's whole tick-decision state in one object — whether
 * the feature participates, what it last commanded, what the fleet is
 * exporting, and WHY it is idle, in the pinned vocabulary above.
 *
 * The field is ABSENT on today's wire (PENDING-BACKEND): absence is the
 * feature detection — no tile, no toggle, nothing else changes. Once present,
 * parsing is null-safe and absent-field-tolerant per field: a malformed datum
 * falls back to its honest default, never to a fabricated figure.
 */
export interface AdviserState {
  /** The adviser is participating this process (config at boot, or the toggle since). */
  enabled: boolean;
  /** "runtime" = last changed by the toggle: operational only until restart. */
  enabledOrigin: AdviserEnabledOrigin;
  /** The site has ever captured the net-billing acknowledgement; durable. */
  acknowledgedEconomics: boolean;
  /** A live adviser intent exists right now (derived from held_intent_id). */
  active: boolean;
  hysteresisState: AdviserHysteresisState;
  /** This tick's neediest eligible unit; null when no target qualified. */
  targetUnitId: string | null;
  /** The last tick's achievable command; 0 when not commanding. */
  commandedChargeW: number;
  /** The deterministic export bound from the last tick. */
  eligibleExportChargeW: number;
  /**
   * Σ per-unit `grid_power_w` (positive = export). NULL when any unit's grid
   * evidence is missing/bad/stale — never zero-filled: one unreadable phase is
   * never treated as zero export.
   */
  fleetExportW: number | null;
  exportEvidence: AdviserExportEvidence;
  /** The composed `max_charge_from_export_w` — the commissioned envelope. */
  chargeCapW: number;
  /** The live adviser intent id; null when holding nothing. */
  heldIntentId: string | null;
  lastAction: AdviserTickAction;
  /** Wall time of the last completed tick (the projection is tick-granular). */
  lastTickAt: string;
  /** The pinned vocabulary above; empty never while enabled. */
  reasonCodes: string[];
}

const ENABLED_ORIGINS: readonly AdviserEnabledOrigin[] = ["config", "runtime"];
const HYSTERESIS_STATES: readonly AdviserHysteresisState[] = [
  "inactive",
  "entering",
  "holding",
  "exiting",
];
const EXPORT_EVIDENCES: readonly AdviserExportEvidence[] = ["good", "missing", "bad", "stale"];
const TICK_ACTIONS: readonly AdviserTickAction[] = ["idle", "propose", "renew", "withdraw"];

function oneOf<T extends string>(value: unknown, vocabulary: readonly T[], fallback: T): T {
  return typeof value === "string" && (vocabulary as readonly string[]).includes(value)
    ? (value as T)
    : fallback;
}

function toFiniteWatts(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function toNullableId(value: unknown, fallback: string | null): string | null {
  return typeof value === "string" && value !== "" ? value : fallback;
}

/**
 * Narrow one adviser projection (a snapshot's `adviser_state`, a toggle 200's
 * `adviser_state`, or an `excess_adviser.state_changed` payload). Null when the
 * value is not an object — a garbage frame is never half-adopted. Absent fields
 * inherit from `base` when one is given (the event payload carries no
 * `charge_cap_w` / `last_action` / `last_tick_at`: those stay whatever the
 * snapshot last said), else take their honest defaults.
 */
export function toAdviserState(value: unknown, base: AdviserState | null = null): AdviserState | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    enabled: typeof value.enabled === "boolean" ? value.enabled : (base?.enabled ?? false),
    enabledOrigin: oneOf(value.enabled_origin, ENABLED_ORIGINS, base?.enabledOrigin ?? "config"),
    acknowledgedEconomics:
      typeof value.acknowledged_economics === "boolean"
        ? value.acknowledged_economics
        : (base?.acknowledgedEconomics ?? false),
    active: typeof value.active === "boolean" ? value.active : (base?.active ?? false),
    hysteresisState: oneOf(
      value.hysteresis_state,
      HYSTERESIS_STATES,
      base?.hysteresisState ?? "inactive",
    ),
    targetUnitId: toNullableId(value.target_unit_id, base?.targetUnitId ?? null),
    commandedChargeW: toFiniteWatts(value.commanded_charge_w, base?.commandedChargeW ?? 0),
    eligibleExportChargeW: toFiniteWatts(
      value.eligible_export_charge_w,
      base?.eligibleExportChargeW ?? 0,
    ),
    // NULL IS A REAL ANSWER here: an explicit wire null means the export
    // evidence did not hold, and only a genuinely absent key inherits the
    // base's figure (the event patch must not erase the snapshot's export
    // reading just because the payload omits it).
    fleetExportW:
      typeof value.fleet_export_w === "number" && Number.isFinite(value.fleet_export_w)
        ? value.fleet_export_w
        : value.fleet_export_w === null
          ? null
          : (base?.fleetExportW ?? null),
    exportEvidence: oneOf(value.export_evidence, EXPORT_EVIDENCES, base?.exportEvidence ?? "missing"),
    chargeCapW: toFiniteWatts(value.charge_cap_w, base?.chargeCapW ?? 0),
    heldIntentId: toNullableId(value.held_intent_id, base?.heldIntentId ?? null),
    lastAction: oneOf(value.last_action, TICK_ACTIONS, base?.lastAction ?? "idle"),
    lastTickAt: typeof value.last_tick_at === "string" ? value.last_tick_at : (base?.lastTickAt ?? ""),
    reasonCodes: Array.isArray(value.reason_codes)
      ? value.reason_codes.filter((code): code is string => typeof code === "string")
      : (base?.reasonCodes ?? []),
  };
}

/**
 * Apply one `excess_adviser.state_changed` payload onto the current projection
 * (the design contract §2): the payload carries the state tuple and the watt
 * figures but NOT the composed cap or the tick bookkeeping, so absent fields
 * keep the current values. A payload that is not an object changes nothing.
 */
export function patchAdviserState(
  current: AdviserState | null,
  payload: unknown,
): AdviserState | null {
  if (!isRecord(payload)) {
    return current;
  }
  return toAdviserState(payload, current);
}

/**
 * The per-unit grid-tie row the Home tile and the Batteries figures share, in
 * the design contract's own wording ("mid: grid +620 W export · load 340 W"):
 * `grid_power_w` is signed (negative = import, positive = export — the live
 * capture's own convention), `load_power_w` is the pod's local load. An absent
 * datum reads "not available" — never zero-filled, and a 0 W grid figure is
 * neither import nor export.
 */
export function gridLoadText(gridPowerW: number | null, loadPowerW: number | null): string {
  const grid =
    gridPowerW === null
      ? "grid not available"
      : gridPowerW > 0
        ? `grid +${formatWatts(gridPowerW)} export`
        : gridPowerW < 0
          ? `grid ${formatWatts(gridPowerW)} import`
          : "grid 0 W";
  const load = loadPowerW === null ? "load not available" : `load ${formatWatts(loadPowerW)}`;
  return `${grid} · ${load}`;
}

export interface UnitModel {
  unitId: string;
  lifecycle: Lifecycle;
  telemetryAgeS: number | null;
  quality: string;
  requested: PowerFigure;
  authorized: PowerFigure | null;
  measuredWatts: number | null;
  /**
   * The per-unit inhibit latch the amended snapshot contract adds
   * (`inhibit_latched` / `inhibit_cause`). Null while the backend does not
   * send the field — never guessed from the lifecycle, which is inhibited for
   * latched stops and per-unit inhibits alike.
   */
  inhibitLatched: boolean | null;
  inhibitCause: string | null;
}

/**
 * One engaged (latched, not yet acknowledged) emergency stop, exactly as the
 * snapshot's `active_stops` array carries it: the stop id an acknowledgement
 * must type back, when it latched, who engaged it, why, and the units it
 * holds (`null` unit ids = the whole fleet).
 */
export interface ActiveStop {
  stopId: string;
  latchedAt: string;
  principal: string;
  reasonCodes: string[];
  /** null = fleet-wide. */
  unitIds: string[] | null;
}

export interface FleetSnapshot {
  siteId: string;
  sequence: number;
  capturedAt: string;
  units: UnitModel[];
  /**
   * The snapshot's engaged emergency stops. Empty when the snapshot carries no
   * `active_stops` (today's backend) — the absence is the feature detection:
   * the latch banner stays hidden and nothing else changes until the field
   * lands.
   */
  activeStops: ActiveStop[];
  /**
   * The snapshot `intent` block's per-unit figures when the field is present
   * and names an active request; null when absent (today's backend) or null
   * (no active request). Null NEVER means "no request is active" on its own —
   * the units' own figures still speak — it means "this snapshot carries no
   * per-unit truth for it".
   */
  intentFigures: SnapshotIntentFigures | null;
  /**
   * The snapshot's top-level `adviser_state` projection when the field is
   * present (PENDING-BACKEND: the excess_charging block's commissioning
   * contract); null when absent — today's wire — and that null IS the feature
   * detection: no tile, no toggle, nothing else renders.
   */
  adviserState: AdviserState | null;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toLifecycle(value: unknown): Lifecycle {
  // An unrecognized lifecycle is never presented optimistically: a unit whose
  // state we cannot name is treated as having no contact.
  return typeof value === "string" && (LIFECYCLES as readonly string[]).includes(value)
    ? (value as Lifecycle)
    : "disconnected";
}

function toFigure(value: unknown): PowerFigure | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    direction: typeof value.direction === "string" ? value.direction : "idle",
    watts: typeof value.watts === "number" ? value.watts : 0,
  };
}

function toUnit(raw: unknown): UnitModel | null {
  if (!isRecord(raw)) {
    return null;
  }
  if (typeof raw.unit_id !== "string" || raw.unit_id === "") {
    return null;
  }
  return {
    unitId: raw.unit_id,
    lifecycle: toLifecycle(raw.lifecycle),
    telemetryAgeS: typeof raw.telemetry_age_s === "number" ? raw.telemetry_age_s : null,
    quality: typeof raw.quality === "string" ? raw.quality : "missing",
    requested: toFigure(raw.requested_power) ?? { direction: "idle", watts: 0 },
    authorized: toFigure(raw.authorized_power),
    measuredWatts: typeof raw.measured_watts === "number" ? raw.measured_watts : null,
    inhibitLatched: typeof raw.inhibit_latched === "boolean" ? raw.inhibit_latched : null,
    inhibitCause: typeof raw.inhibit_cause === "string" ? raw.inhibit_cause : null,
  };
}

function toActiveStop(raw: unknown): ActiveStop | null {
  if (!isRecord(raw)) {
    return null;
  }
  if (typeof raw.stop_id !== "string" || raw.stop_id === "") {
    return null;
  }
  return {
    stopId: raw.stop_id,
    latchedAt: typeof raw.latched_at === "string" ? raw.latched_at : "",
    principal: typeof raw.principal === "string" ? raw.principal : "",
    reasonCodes: Array.isArray(raw.reason_codes)
      ? raw.reason_codes.filter((code): code is string => typeof code === "string")
      : [],
    unitIds: Array.isArray(raw.unit_ids)
      ? raw.unit_ids.filter((id): id is string => typeof id === "string")
      : null,
  };
}

/** Normalize any snapshot envelope (REST or WS first frame) to the shell model. */
export function normalizeSnapshot(raw: unknown): FleetSnapshot {
  const record = isRecord(raw) ? raw : {};
  const units = (Array.isArray(record.units) ? record.units : [])
    .map(toUnit)
    .filter((entry): entry is UnitModel => entry !== null);
  return {
    siteId: typeof record.site_id === "string" ? record.site_id : "",
    sequence: typeof record.snapshot_sequence === "number" ? record.snapshot_sequence : 0,
    capturedAt: typeof record.captured_at === "string" ? record.captured_at : "",
    units,
    // Feature detection: a snapshot with no active_stops (today's backend)
    // normalizes to "no engaged stops known", never to an error.
    activeStops: (Array.isArray(record.active_stops) ? record.active_stops : [])
      .map(toActiveStop)
      .filter((entry): entry is ActiveStop => entry !== null),
    // Feature detection: an absent or null `intent` field normalizes to "no
    // per-unit figures from this snapshot"; the units' own figures and the
    // bus-fed maps stay the truth either way.
    intentFigures: isRecord(record.intent) ? toSnapshotIntentFigures(record.intent) : null,
    // Feature detection: an absent `adviser_state` (today's backend) normalizes
    // to "the excess-solar feature is not composed here" — the tile, the
    // toggle, and every adviser-derived surface stay hidden.
    adviserState: isRecord(record.adviser_state) ? toAdviserState(record.adviser_state) : null,
  };
}

/**
 * The unit's OWN requested target, never the fleet total the snapshot repeats
 * per covered unit (service.py `_requested_power` projects the intent's own
 * figure). Three sources, in order: the live per-unit maps (the shared
 * tracker's `watts_by_unit` — the exact truth), the snapshot `intent` block's
 * requested map (cold-load exactness, feature-detected), and a snapshot figure
 * no OTHER unit shares (one battery's fleet total IS its own figure). A figure
 * shared by several units is their intent's total, not any one battery's
 * request: null, so no surface may compare it against a per-unit allowance.
 */
export function perUnitRequestedWatts(
  unit: UnitModel,
  units: UnitModel[],
  requestedByUnit: WattsByUnit | null,
): number | null {
  const mapped = requestedByUnit !== null ? requestedByUnit[unit.unitId] : undefined;
  if (mapped !== undefined) {
    return mapped;
  }
  if (unit.requested.direction === "idle" && unit.requested.watts <= 0) {
    return null;
  }
  const shared = units.filter(
    (other) =>
      other.requested.direction === unit.requested.direction &&
      other.requested.watts === unit.requested.watts,
  );
  return shared.length === 1 ? unit.requested.watts : null;
}

/**
 * A unit whose allowance is smaller than its OWN request: never "fully
 * delivered", and never "Limited" from a fleet-vs-unit comparison. The
 * requested side of the comparison must be the battery's own target (see
 * `perUnitRequestedWatts`); a shared fleet total with no per-unit truth
 * derives no Limited state at all — the 3 × 1,000 W dispatch authorized in
 * full is Active, not Limited. An authorized direction opposite the request is
 * a limit regardless of the maps.
 */
export function isLimited(
  unit: UnitModel,
  units: UnitModel[] = [unit],
  requestedByUnit: WattsByUnit | null = null,
  authorizedByUnit: WattsByUnit | null = null,
): boolean {
  if (unit.authorized === null) {
    return true;
  }
  if (unit.authorized.direction !== unit.requested.direction) {
    return true;
  }
  const requestedWatts = perUnitRequestedWatts(unit, units, requestedByUnit);
  if (requestedWatts === null) {
    return false;
  }
  const authorizedWatts =
    (authorizedByUnit !== null ? authorizedByUnit[unit.unitId] : undefined) ??
    unit.authorized.watts;
  return authorizedWatts < requestedWatts;
}

/**
 * Conservatism ordering for the fleet banner (most conservative wins):
 * inhibited > observe-only > active (or limited) > stopping > armed > disarmed.
 * Units the snapshot marks disconnected or booting never lift the banner into
 * an armed/active claim; their disconnection is named instead.
 */
const RANK: Record<Lifecycle, number> = {
  disconnected: 0,
  boot: 0,
  disarmed: 2,
  armed_idle: 3,
  stopping: 3.5,
  active: 4,
  observe_only: 5,
  inhibited: 6,
};

export type Badge =
  | "Inhibited"
  | "Observe only"
  | "Limited"
  | "Active"
  | "Stopping"
  | "Armed"
  | "Disarmed"
  | "No units yet";

export interface BannerModel {
  /** Most conservative fleet badge; null when no unit's state is known. */
  badge: Badge | null;
  /** Units with no contact: named in the banner, never swallowed. */
  contactLost: UnitModel[];
}

export function fleetBanner(
  units: UnitModel[],
  requestedByUnit: WattsByUnit | null = null,
  authorizedByUnit: WattsByUnit | null = null,
): BannerModel {
  const contactLost = units.filter((unit) => unit.lifecycle === "disconnected");
  const known = units.filter(
    (unit) => unit.lifecycle !== "disconnected" && unit.lifecycle !== "boot",
  );
  if (units.length === 0) {
    return { badge: "No units yet", contactLost: [] };
  }
  if (known.length === 0) {
    // Every unit is disconnected or still booting: no lifecycle claim at all.
    return { badge: null, contactLost };
  }
  let top: UnitModel = known[0]!;
  for (const unit of known.slice(1)) {
    if (RANK[unit.lifecycle] > RANK[top.lifecycle]) {
      top = unit;
    }
  }
  switch (top.lifecycle) {
    case "inhibited":
      return { badge: "Inhibited", contactLost };
    case "observe_only":
      return { badge: "Observe only", contactLost };
    case "active":
      return {
        // "Limited" is a per-unit truth only: the requested side of every
        // comparison is the battery's own target, never the fleet total the
        // snapshot repeats per covered unit (see isLimited).
        badge: known.some(
          (unit) =>
            unit.lifecycle === "active" && isLimited(unit, units, requestedByUnit, authorizedByUnit),
        )
          ? "Limited"
          : "Active",
        contactLost,
      };
    case "stopping":
      return { badge: "Stopping", contactLost };
    case "armed_idle":
      return { badge: "Armed", contactLost };
    default:
      return { badge: "Disarmed", contactLost };
  }
}

/** Units an emergency stop would act on: every unit still in a control state. */
const STOPPABLE: readonly Lifecycle[] = ["armed_idle", "active", "inhibited", "stopping"];

export function stoppableUnits(units: UnitModel[]): UnitModel[] {
  return units.filter((unit) => STOPPABLE.includes(unit.lifecycle));
}
