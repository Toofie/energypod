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

export interface UnitModel {
  unitId: string;
  lifecycle: Lifecycle;
  telemetryAgeS: number | null;
  quality: string;
  requested: PowerFigure;
  authorized: PowerFigure | null;
  measuredWatts: number | null;
}

export interface FleetSnapshot {
  siteId: string;
  sequence: number;
  capturedAt: string;
  units: UnitModel[];
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
