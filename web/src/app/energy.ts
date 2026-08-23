/**
 * The energy scorecard's shared wire model (DESIGN_ENERGY_SCORECARD.md §5/§6 +
 * API_CONTRACTS.md "Energy scorecard"): the `EnergyDayRecord` domain shape,
 * the snapshot's top-level `energy_today` block, the `GET /api/v1/energy/days`
 * body, and the `energy.day_rolled` bus event — plus the plain-language maps
 * every energy surface (Home's Today card, Insights' ledger, the Batteries
 * readthroughs) shares.
 *
 * PENDING-BACKEND: the route, the snapshot block, and the event are not live
 * yet — every parser here is built feature-detectively against the contract's
 * pinned shapes (the active_stops / intent-block / adviser_state /
 * schedule_state pattern): an ABSENT field is the feature detection and never
 * an error; a PRESENT-but-unusable datum falls back to its honest default
 * (null / "" / false), never to a fabricated figure.
 *
 * Honesty rules pinned here (the design's §1, made structural):
 *
 * - NOTHING is ever zero-filled: a `null` per-unit figure means "source absent
 *   all day" and renders "not available", never 0.
 * - The grid counter pair keeps NEUTRAL A/B names end to end. The pair's decode
 *   ORDER is vendor-confirmed but its buy/sell ROLE labels are evidence-open
 *   (field-mapping A-1), so no surface may label a counter "bought" or "sold"
 *   — the daily bought/sold figures come from the record's own
 *   `grid_import_kwh` / `grid_export_kwh`, whose SOURCE the record names.
 * - Solar production is NEVER presented as a measurement: the site's PV inputs
 *   are unwired to the pods (the PV counter is readthrough-only, A-12 noise).
 *   The scorecard's solar story is what IS measured — the surplus the site
 *   exported ("sold") and the surplus the batteries captured ("charged from
 *   surplus"); the footnote says exactly this once.
 * - Provenance is a first-class fact: `sources.grid === "integrated_ct"` means
 *   the figures are OUR OWN CT integration (measured by the controller);
 *   `device_counter` is reachable only AFTER the operator pinned the counter
 *   roles (§7 validation refuses `device_counter` while roles are unpinned —
 *   the A-1 gate made structural), so that source label legitimately carries
 *   "the pods' own meters, roles confirmed".
 */
import { formatKilowattHours, formatPercent } from "../lib/format";
import { isRecord } from "./fleet";

/** The rollover TRANSITION event (the completed record; never a heartbeat). */
export const ENERGY_DAY_ROLLED_EVENT = "energy.day_rolled" as const;

export const ENERGY_EVENT_TYPES: readonly string[] = [ENERGY_DAY_ROLLED_EVENT];

/** The days route's default page (N ∈ 1..31, newest-last). */
export const ENERGY_DAYS_DEFAULT_LIMIT = 8;
/** The route's hard bound; the ledger's paging stops here. */
export const ENERGY_DAYS_MAX_LIMIT = 31;

// --- vocabularies -------------------------------------------------------------

export type EnergyDayKind = "complete" | "partial" | "in_progress";

const DAY_KINDS: readonly EnergyDayKind[] = ["complete", "partial", "in_progress"];

/**
 * The metric sources the record names (§2's provenance classes). `readthrough`
 * and `evidence_only` never appear in a day record's `sources` (they are the
 * PV-counter and cross-check classes) but share the wording table below.
 */
export type EnergyMetricSource =
  | "integrated_ct"
  | "device_counter"
  | "attributed_adviser"
  | "readthrough"
  | "evidence_only";

/** The grid counter role pin (A-1): the operator's config revision, never ours. */
export type EnergyGridCounterRoles = "unpinned" | "vendor_labels" | "swapped";

const GRID_ROLES: readonly EnergyGridCounterRoles[] = ["unpinned", "vendor_labels", "swapped"];

/**
 * The cross-check's consistency verdict — the wire's own pinned vocabulary
 * (API_CONTRACTS.md "Energy scorecard", the 2026-08-26 wire pins): one of the
 * two label orderings fit the day's tolerance test (`vendor_labels` /
 * `swapped`), or the day's usable evidence did not single one out
 * (`undiscriminating`). NULL is the backend's first-class "the day could not
 * discriminate" answer — low coverage, a sub-0.5 kWh side, a grid-pair
 * reset, or neither ordering fits the tolerance — not a missing field.
 */
export type EnergyCrossCheckVerdict = "vendor_labels" | "swapped" | "undiscriminating";

const CROSS_CHECK_VERDICTS: readonly EnergyCrossCheckVerdict[] = [
  "vendor_labels",
  "swapped",
  "undiscriminating",
];

/** The one per-metric flag the record defines (§2's reset handling). */
export const ENERGY_METRIC_FLAGS: readonly string[] = ["counter_reset_observed"];

// --- the day record ------------------------------------------------------------

/** One unit's day: every figure nullable, coverage a fraction in percent. */
export interface EnergyUnitDay {
  gridImportKwh: number | null;
  gridExportKwh: number | null;
  batteryChargedKwh: number | null;
  batteryDischargedKwh: number | null;
  loadKwh: number | null;
  chargedFromSurplusKwh: number | null;
  coveragePct: number | null;
  /** e.g. ["counter_reset_observed"] — the affected metrics are in the flags. */
  metricFlags: string[];
}

/** The fleet rollup: the sums plus the WORST unit's coverage (§4). */
export interface EnergyFleetDay {
  gridImportKwh: number | null;
  gridExportKwh: number | null;
  batteryChargedKwh: number | null;
  batteryDischargedKwh: number | null;
  loadKwh: number | null;
  chargedFromSurplusKwh: number | null;
  coveragePct: number | null;
}

/** Which source each metric family came from — rendered, never guessed. */
export interface EnergyDaySources {
  grid: EnergyMetricSource;
  battery: EnergyMetricSource;
  load: EnergyMetricSource;
  surplus: EnergyMetricSource;
}

/**
 * The A-1 passive cross-check record (§3): both grid-counter deltas alongside
 * the integrated figures, plus the consistency verdict the record reports —
 * never auto-applied. `consistentWith` is the pinned verdict vocabulary or
 * null (the backend's could-not-discriminate answer, and this model's honest
 * no-verdict for a value outside the vocabulary); a verdict is never guessed.
 */
export interface EnergyCounterCrossCheck {
  gridADeltaKwh: number | null;
  gridBDeltaKwh: number | null;
  consistentWith: EnergyCrossCheckVerdict | null;
  discriminating: boolean | null;
}

/** One site-day, per the frozen domain contract (§5's JSON, verbatim keys). */
export interface EnergyDayRecord {
  date: string;
  timezone: string;
  utcOffsetMinutes: number | null;
  kind: EnergyDayKind;
  units: Record<string, EnergyUnitDay>;
  fleet: EnergyFleetDay;
  sources: EnergyDaySources;
  counterCrossCheck: EnergyCounterCrossCheck | null;
  /** Always false on this site's wire; null when the field is absent. */
  solarProductionMeasured: boolean | null;
}

/** The snapshot's `energy_today` block: the live in-progress day plus `as_of`. */
export interface EnergyToday extends EnergyDayRecord {
  asOf: string | null;
}

/** The days route's 200 body. */
export interface EnergyDaysView {
  /** Newest-last, exactly as the route serves them. */
  days: EnergyDayRecord[];
  gridCounterRoles: EnergyGridCounterRoles;
  solarProductionMeasured: boolean;
}

// --- parsing (null-safe, absent-tolerant) --------------------------------------

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function flagsOf(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((flag): flag is string => typeof flag === "string" && flag !== "")
    : [];
}

function sourceOf(value: unknown, fallback: EnergyMetricSource): EnergyMetricSource {
  return typeof value === "string" && value !== "" ? (value as EnergyMetricSource) : fallback;
}

function toUnitDay(value: unknown): EnergyUnitDay {
  const record = isRecord(value) ? value : {};
  return {
    gridImportKwh: finiteOrNull(record.grid_import_kwh),
    gridExportKwh: finiteOrNull(record.grid_export_kwh),
    batteryChargedKwh: finiteOrNull(record.battery_charged_kwh),
    batteryDischargedKwh: finiteOrNull(record.battery_discharged_kwh),
    loadKwh: finiteOrNull(record.load_kwh),
    chargedFromSurplusKwh: finiteOrNull(record.charged_from_surplus_kwh),
    coveragePct: finiteOrNull(record.coverage_pct),
    metricFlags: flagsOf(record.metric_flags),
  };
}

function toFleetDay(value: unknown): EnergyFleetDay {
  const record = isRecord(value) ? value : {};
  return {
    gridImportKwh: finiteOrNull(record.grid_import_kwh),
    gridExportKwh: finiteOrNull(record.grid_export_kwh),
    batteryChargedKwh: finiteOrNull(record.battery_charged_kwh),
    batteryDischargedKwh: finiteOrNull(record.battery_discharged_kwh),
    loadKwh: finiteOrNull(record.load_kwh),
    chargedFromSurplusKwh: finiteOrNull(record.charged_from_surplus_kwh),
    coveragePct: finiteOrNull(record.coverage_pct),
  };
}

function toCrossCheck(value: unknown): EnergyCounterCrossCheck | null {
  if (!isRecord(value)) {
    return null;
  }
  const verdict = value.consistent_with;
  return {
    gridADeltaKwh: finiteOrNull(value.grid_a_delta_kwh),
    gridBDeltaKwh: finiteOrNull(value.grid_b_delta_kwh),
    consistentWith:
      typeof verdict === "string" && (CROSS_CHECK_VERDICTS as readonly string[]).includes(verdict)
        ? (verdict as EnergyCrossCheckVerdict)
        : null,
    discriminating: typeof value.discriminating === "boolean" ? value.discriminating : null,
  };
}

/**
 * Narrow one day record. Null only when the value is not an object at all —
 * a record that is present but missing fields keeps every figure null (the
 * honest "source absent all day"), never a zero, never a fabrication.
 */
export function toEnergyDayRecord(value: unknown): EnergyDayRecord | null {
  if (!isRecord(value)) {
    return null;
  }
  const units: Record<string, EnergyUnitDay> = {};
  if (isRecord(value.units)) {
    for (const [unitId, unitDay] of Object.entries(value.units)) {
      units[unitId] = toUnitDay(unitDay);
    }
  }
  const sources = isRecord(value.sources) ? value.sources : {};
  const kind =
    typeof value.kind === "string" && (DAY_KINDS as readonly string[]).includes(value.kind)
      ? (value.kind as EnergyDayKind)
      : "complete";
  return {
    date: typeof value.date === "string" ? value.date : "",
    timezone: typeof value.timezone === "string" ? value.timezone : "",
    utcOffsetMinutes:
      typeof value.utc_offset_minutes === "number" && Number.isFinite(value.utc_offset_minutes)
        ? value.utc_offset_minutes
        : null,
    kind,
    units,
    fleet: toFleetDay(value.fleet),
    sources: {
      grid: sourceOf(sources.grid, "integrated_ct"),
      battery: sourceOf(sources.battery, "device_counter"),
      load: sourceOf(sources.load, "device_counter"),
      surplus: sourceOf(sources.surplus, "attributed_adviser"),
    },
    counterCrossCheck: toCrossCheck(value.counter_cross_check),
    solarProductionMeasured:
      typeof value.solar_production_measured === "boolean" ? value.solar_production_measured : null,
  };
}

/**
 * Narrow the snapshot's `energy_today`. The whole BLOCK is the feature
 * detection (absent key = the scorecard is not composed — nothing renders);
 * inside a present block every field is absent-tolerant per field.
 */
export function toEnergyToday(value: unknown): EnergyToday | null {
  const record = toEnergyDayRecord(value);
  if (record === null) {
    return null;
  }
  const asOf = isRecord(value) && typeof value.as_of === "string" ? value.as_of : null;
  return { ...record, asOf };
}

/** Narrow the days route's 200 body; null when the value is not an object. */
export function toEnergyDaysView(value: unknown): EnergyDaysView | null {
  if (!isRecord(value)) {
    return null;
  }
  const days = (Array.isArray(value.days) ? value.days : [])
    .map(toEnergyDayRecord)
    .filter((day): day is EnergyDayRecord => day !== null);
  const roles = value.grid_counter_roles;
  return {
    days,
    gridCounterRoles:
      typeof roles === "string" && (GRID_ROLES as readonly string[]).includes(roles)
        ? (roles as EnergyGridCounterRoles)
        : "unpinned",
    solarProductionMeasured: value.solar_production_measured === true,
  };
}

/**
 * The `energy.day_rolled` payload IS the completed record (§6). Null when the
 * payload carries no usable record — the frame is then ignored, never
 * half-applied.
 */
export function toEnergyDayRolledEvent(payload: unknown): EnergyDayRecord | null {
  return toEnergyDayRecord(payload);
}

// --- plain-language maps (the operator's words, the lib's figures) --------------

/** A kWh figure or the named gap — never 0 standing in for an absent source. */
export function kwhText(value: number | null): string {
  return value === null ? "not available" : formatKilowattHours(value);
}

/** A coverage percentage, or "" when the day carries none. */
export function coverageText(value: number | null): string {
  return value === null ? "" : formatPercent(value);
}

/**
 * The day-kind marker sentence (§1 rule 1): an in-progress day always says
 * "so far"; a partial day names the threshold breach; coverage rides either.
 * The task's pinned example shape: "so far today — 87% coverage".
 */
export function dayMarkerText(kind: EnergyDayKind, coveragePct: number | null): string {
  const coverage = coverageText(coveragePct);
  const coverageClause = coverage === "" ? "" : ` — ${coverage} coverage`;
  if (kind === "in_progress") {
    return `so far today${coverageClause}`;
  }
  if (kind === "partial") {
    return `partial day${coverageClause} — below the commissioned coverage threshold, so the figures are incomplete`;
  }
  return coverage === "" ? "full day recorded" : `full day${coverageClause}`;
}

/**
 * The provenance sentence for one metric source (§2's provenance classes in
 * the operator's words). `integrated_ct` is OUR OWN CT integration — the
 * phrase "measured by the controller" is the design's own label.
 */
export function sourceProvenanceText(source: EnergyMetricSource): string {
  switch (source) {
    case "integrated_ct":
      return "measured by the controller";
    case "device_counter":
      return "recorded by the pods' own energy counters";
    case "attributed_adviser":
      return "attributed from measured watts while solar-surplus charging was active";
    case "readthrough":
      return "a raw device counter, shown for completeness";
    case "evidence_only":
      return "recorded as cross-check evidence only";
    default:
      return "source not named";
  }
}

/**
 * The today card's grid-provenance line (the A/B unpinned note, where
 * applicable): with the integrated source the pods' grid counters are NOT the
 * display source, so the card says so and names their neutral A/B display —
 * never which one is "bought". With the device-counter source the roles ARE
 * pinned (validation refuses the promotion while unpinned), so the line says
 * that instead.
 */
export function gridProvenanceNote(gridSource: EnergyMetricSource): string {
  if (gridSource === "device_counter") {
    return "Bought/sold come from the pods' own grid counters (counter roles confirmed), with the controller's own measurement kept as the cross-check.";
  }
  return "Bought/sold are measured by the controller from its own grid sensors — the pods' two grid counters display as counter A and counter B until their roles are pinned, and nothing here is read from them.";
}

/**
 * The ledger's counter-roles note, keyed on the route's own
 * `grid_counter_roles` answer (the one wire source of the pin state).
 */
export function counterRolesNote(roles: EnergyGridCounterRoles): string {
  switch (roles) {
    case "vendor_labels":
      return "The pods' grid counter roles are confirmed as the vendor's labels (counter A = bought, counter B = sold).";
    case "swapped":
      return "The pods' grid counter roles are confirmed SWAPPED from the vendor's labels (counter A = sold, counter B = bought).";
    default:
      return "The pods' two grid counters are recorded as counter A and counter B — which one counts as 'bought' is not confirmed yet, so the bought/sold figures are measured by the controller instead.";
  }
}

/**
 * The cross-check verdict in the operator's words (the A-1 evidence gate §3):
 * the two pin verdicts say which label ordering the day's evidence fits, and
 * null gets the honest "not yet discriminating" line — the backend emits null
 * exactly when the day could not discriminate (low coverage, a sub-0.5 kWh
 * side, a grid-pair reset, or neither ordering fits the tolerance). No
 * wording here assigns a bought/sold role to either counter: the verdict is
 * evidence, never a pin.
 */
export function crossCheckVerdictText(verdict: EnergyCrossCheckVerdict | null): string {
  switch (verdict) {
    case "vendor_labels":
      return "consistent with the vendor's labels";
    case "swapped":
      return "consistent with the vendor's labels swapped";
    case "undiscriminating":
      return "undiscriminating — the day's evidence did not single out one label ordering";
    default:
      return "not yet discriminating — this day could not tell the counters apart (low coverage, a side under 0.5 kWh, a counter reset, or neither ordering fit the tolerance)";
  }
}

/**
 * THE SOLAR PIN (§1 rule 3): the one footnote the card and the ledger each
 * carry once. Site PV is not wired to the pod inputs — nothing anywhere may
 * present a solar-production figure as measured.
 */
export const SOLAR_FOOTNOTE =
  "Solar panels are not measured by the pods — 'sold' is the surplus the site exported.";

/** The neutral names for the readthrough pair — the only labels they ever get. */
export const GRID_COUNTER_A_LABEL = "Grid counter A";
export const GRID_COUNTER_B_LABEL = "Grid counter B";

/**
 * The lifetime PV readthrough's own note (§2's PV row: present for
 * completeness, never a scorecard line, the A-12 noise named).
 */
export const PV_READTHROUGH_NOTE =
  "The pods' solar inputs are not wired at this site, so this counter is not a measurement of the site's panels.";

/** One metric flag in the operator's words. */
export function metricFlagText(flag: string): string {
  return flag === "counter_reset_observed"
    ? "a counter was reset during this day — the figure restarts from the new baseline"
    : flag;
}
