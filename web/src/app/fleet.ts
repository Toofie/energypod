/**
 * Shared fleet model for the shell.
 *
 * The service serializes enums as lowercase StrEnum values (UI_CONTRACTS.md
 * "Wire casing"): lifecycle "boot"/"observe_only"/"disarmed"/"armed_idle"/
 * "active"/"inhibited"/"stopping"/"disconnected". Everything in this module
 * works in those wire values; the human-capitalized labels live next to them
 * and are the only strings shown to operators.
 */
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
  };
}

/** A unit whose allowance is smaller than its request: never "fully delivered". */
export function isLimited(unit: UnitModel): boolean {
  if (unit.authorized === null) {
    return true;
  }
  return unit.authorized.direction !== unit.requested.direction ||
    unit.authorized.watts !== unit.requested.watts;
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

export function fleetBanner(units: UnitModel[]): BannerModel {
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
        badge: known.some((unit) => unit.lifecycle === "active" && isLimited(unit))
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
