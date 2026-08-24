/**
 * The plant-history wire model (DESIGN_PLANT_HISTORY.md §2–§5 +
 * API_CONTRACTS.md "Plant history"): the `GET /api/v1/history` body, the
 * snapshot's feature-detected `history_state` block, the range presets, and
 * the plain-language maps the History view renders from.
 *
 * Honesty rules pinned here (the design's §1, made structural — the flow and
 * energy models' own discipline applied to time series):
 *
 * - GAPS ARE ABSENT ROWS, NEVER INTERPOLATED. The server computes the gap
 *   intervals; this model carries them verbatim and the chart layer renders
 *   them as breaks. Nothing here bridges a gap, fills a hole, or synthesizes
 *   a point between two samples.
 * - NULLS ARE NULLS, NEVER ZERO. A sample whose datum was absent simply has
 *   no point in that series (the wire skips null-valued points); a missing
 *   series entry is the ABSENCE of the field, rendered "not available".
 *   No figure anywhere in this model becomes 0 to please an axis.
 * - EVERY EMITTED POINT IS A REAL STORED SAMPLE (full resolution) or a real
 *   hour's rollup (hourly) — the server's pinned LTTB guarantees it and the
 *   client never resamples, smooths, or re-buckets what arrived.
 * - ONE RESOLUTION PER RESPONSE, chosen by the DATA horizon server-side: the
 *   model reports the tier and the view labels it; the client never asks for
 *   a resolution and never mixes tiers in one series.
 * - WINDOW EXTREMES RIDE BESIDE THE DOWNSAMPLED LINE: a peak the downsample
 *   dropped is still a fact (`window_min`/`window_max` with their instants),
 *   and the operator-facing wording annotates it rather than implying the
 *   drawn line was the whole story.
 * - THE QUALITY WORD RIDES VERBATIM: the window's `quality_worst`
 *   (`missing > bad > stale > suspect > good`) renders as the word it is —
 *   degraded periods are named, never silently clean, and never color-only.
 *   Per-sample quality is NOT on this wire (only the window's worst word is),
 *   so no per-period dimming is fabricated here.
 * - THE COMMANDED TRIPLE IS A SAMPLED PROJECTION, NOT THE RECORD: where it
 *   disagrees with the audit trail, the audit row is the record (§6). An
 *   all-null commanded entry IS the recorded fact "no intent claimed the
 *   unit" and renders as exactly that.
 */
import { formatSeconds, formatWatts, wholeSeconds } from "../lib/format";
import { isRecord } from "./fleet";

// --- vocabularies -------------------------------------------------------------

/** The wire's quality-rollup words, worst-first (the precedence order). */
export const HISTORY_QUALITY_WORDS: readonly string[] = [
  "missing",
  "bad",
  "stale",
  "suspect",
  "good",
];

/** The commanded-triple's source vocabulary (§2.2). */
export const COMMANDED_SOURCES: readonly string[] = [
  "manual",
  "agent",
  "schedule",
  "excess_adviser",
  "night_adviser",
  "optimizer",
];

// --- range presets (§4.1: 6 h / 24 h / 7 d / 30 d, plus today-so-far) ----------

/** The pinned range presets, in nav order; "today" (site-local so far) leads. */
export const HISTORY_RANGE_PRESETS: readonly { id: HistoryRangeId; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "6h", label: "6 h" },
  { id: "24h", label: "24 h" },
  { id: "7d", label: "7 d" },
  { id: "30d", label: "30 d" },
];

export type HistoryRangeId = "today" | "6h" | "24h" | "7d" | "30d";

/** The floor for a live window: `from < to` is a wire rule, not a decoration. */
const MIN_WINDOW_MS = 60_000;

const RANGE_SPANS_MS: Record<Exclude<HistoryRangeId, "today">, number> = {
  "6h": 6 * 3_600_000,
  "24h": 24 * 3_600_000,
  "7d": 7 * 86_400_000,
  "30d": 30 * 86_400_000,
};

/**
 * One preset's from/to pair against `now`. "Today" is the operator's own
 * site-local midnight-to-now window (the console renders site-local
 * throughout, and the browser of an operator on site IS the site zone); a
 * just-after-midnight window keeps the wire's `from < to` rule by holding a
 * one-minute floor. Every window ENDS at now — the ranges slide live.
 */
export function rangeWindow(range: HistoryRangeId, now: Date): { from: Date; to: Date } {
  const to = new Date(now.getTime());
  if (range === "today") {
    const midnight = new Date(now.getTime());
    midnight.setHours(0, 0, 0, 0);
    const fromMs = Math.min(midnight.getTime(), to.getTime() - MIN_WINDOW_MS);
    return { from: new Date(fromMs), to };
  }
  return { from: new Date(to.getTime() - RANGE_SPANS_MS[range]), to };
}

/** The preset's label ("Today", "6 h", …); the id verbatim for an unknown id. */
export function rangeLabel(range: HistoryRangeId): string {
  return HISTORY_RANGE_PRESETS.find((preset) => preset.id === range)?.label ?? range;
}

/**
 * The instant a preset's window STARTS at, resolved against the wall clock
 * when the data was fetched (the label under a chart says "since 06:00", not
 * "since -6 h"). Site-local formatting, exactly like every other view.
 */
export function rangeFromInstant(range: HistoryRangeId, fetchedAt: number): number {
  return rangeWindow(range, new Date(fetchedAt)).from.getTime();
}

// --- the request ----------------------------------------------------------------

/**
 * The view's fixed field request: the eight series the charts draw plus the
 * three step-encoded meta-fields, in the vocabulary's own words. Bounded by
 * construction (13 ≤ 18).
 */
export const HISTORY_FIELDS: readonly string[] = [
  "soc_pct",
  "bms_soc_pct",
  "battery_watts",
  "grid_power_w",
  "load_power_w",
  "temperature_min_c",
  "temperature_max_c",
  "cell_min_v",
  "cell_max_v",
  "cell_spread_mv",
  "lifecycle",
  "health_state",
  "commanded",
];

/**
 * The per-series downsample target. The 6 h window at the 30 s cadence holds
 * 720 samples and 24 h holds 2,880 — 900 points keeps a charge burst legible
 * at both; the 7 d/30 d windows answer hourly (≤ 744 points) and never touch
 * the cap. The SERVER owns the algorithm (pinned LTTB); this is only the
 * target.
 */
export const HISTORY_POINTS = 900;

/** The query for one preset window, with the explicit-offset instants the wire demands. */
export function historyQuery(
  range: HistoryRangeId,
  now: Date,
): { from: string; to: string; fields: string; points: number } {
  const window = rangeWindow(range, now);
  return {
    // `toISOString()` ends in Z — an explicit offset, exactly the accepted form.
    from: window.from.toISOString(),
    to: window.to.toISOString(),
    fields: HISTORY_FIELDS.join(","),
    points: HISTORY_POINTS,
  };
}

// --- the parsed window -----------------------------------------------------------

export type HistoryResolution = "full" | "hourly";

/** One full-resolution point: a real stored sample's instant and value. */
export interface HistorySamplePoint {
  readonly t: number;
  readonly v: number;
}

/**
 * One hourly-rollup point: the hour's mean plus its own min/max and sample
 * count — the honest band a mean line alone would flatten. `min`/`max` are
 * null only for a field the hour never carried.
 */
export interface HistoryHourlyPoint {
  readonly t: number;
  readonly v: number;
  readonly min: number | null;
  readonly max: number | null;
  readonly n: number;
}

/** Window-wide extremes over EVERY row — peaks the downsample may have dropped. */
export interface HistorySeriesStats {
  readonly sampleCount: number;
  readonly windowMin: number | null;
  readonly windowMinAt: number | null;
  readonly windowMax: number | null;
  readonly windowMaxAt: number | null;
}

export type HistorySeries =
  | { kind: "samples"; stats: HistorySeriesStats; points: readonly HistorySamplePoint[] }
  | { kind: "hourly"; stats: HistorySeriesStats; points: readonly HistoryHourlyPoint[] };

/** A gap interval, epoch ms; the space between is absent rows, never bridged. */
export interface HistoryGap {
  readonly from: number;
  readonly to: number;
}

/** One step-encoded change point (`{t, v}`; the first sample is always present). */
export interface HistoryChange {
  readonly t: number;
  readonly v: string;
}

/** The commanded triple at one change point; all-null = no intent claimed the unit. */
export interface CommandedChange {
  readonly t: number;
  readonly source: string | null;
  readonly direction: string | null;
  readonly watts: number | null;
}

export interface HistoryUnitBlock {
  readonly unitId: string;
  readonly firstSampleAt: number | null;
  readonly lastSampleAt: number | null;
  readonly sampleCount: number;
  /** The window's worst quality word over the row rollup; null when no samples. */
  readonly qualityWorst: string | null;
  readonly gaps: readonly HistoryGap[];
  readonly series: Readonly<Record<string, HistorySeries>>;
  readonly lifecycleChanges: readonly HistoryChange[];
  readonly healthStateChanges: readonly HistoryChange[];
  readonly commandedChanges: readonly CommandedChange[];
}

export interface HistoryFleetBlock {
  readonly series: Readonly<Record<string, HistorySeries>>;
  readonly gaps: readonly HistoryGap[];
}

/** The whole 200 body, one resolution, exactly as the route served it. */
export interface PlantHistoryWindow {
  readonly from: number;
  readonly to: number;
  readonly resolution: HistoryResolution;
  readonly points: number;
  readonly fields: readonly string[];
  readonly units: Readonly<Record<string, HistoryUnitBlock>>;
  readonly fleet: HistoryFleetBlock | null;
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function instantOrNull(value: unknown): number | null {
  if (typeof value !== "string" || value === "") {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toSeriesStats(value: unknown): HistorySeriesStats {
  const record = isRecord(value) ? value : {};
  return {
    sampleCount:
      typeof record.sample_count === "number" && Number.isFinite(record.sample_count)
        ? record.sample_count
        : 0,
    windowMin: finiteOrNull(record.window_min),
    windowMinAt: instantOrNull(record.window_min_at),
    windowMax: finiteOrNull(record.window_max),
    windowMaxAt: instantOrNull(record.window_max_at),
  };
}

/**
 * Narrow one series. A point rides only when its value is a real number —
 * the wire never emits null-valued points, and neither does this model (an
 * absent SOC is not SOC 0). An entry that is not an object at all is the
 * ABSENCE of the field: null, never an empty stand-in.
 */
function toSeries(value: unknown, resolution: HistoryResolution): HistorySeries | null {
  if (!isRecord(value) || !Array.isArray(value.points)) {
    return null;
  }
  const stats = toSeriesStats(value);
  if (resolution === "hourly") {
    const points: HistoryHourlyPoint[] = [];
    for (const entry of value.points) {
      if (!isRecord(entry)) {
        continue;
      }
      const t = instantOrNull(entry.t);
      const v = finiteOrNull(entry.v);
      if (t === null || v === null) {
        continue;
      }
      points.push({
        t,
        v,
        min: finiteOrNull(entry.min),
        max: finiteOrNull(entry.max),
        n:
          typeof entry.n === "number" && Number.isFinite(entry.n) && entry.n >= 0
            ? Math.floor(entry.n)
            : 0,
      });
    }
    return { kind: "hourly", stats, points };
  }
  const points: HistorySamplePoint[] = [];
  for (const entry of value.points) {
    if (!isRecord(entry)) {
      continue;
    }
    const t = instantOrNull(entry.t);
    const v = finiteOrNull(entry.v);
    if (t === null || v === null) {
      continue;
    }
    points.push({ t, v });
  }
  return { kind: "samples", stats, points };
}

function toChanges(value: unknown): HistoryChange[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const changes: HistoryChange[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) {
      continue;
    }
    const t = instantOrNull(entry.t);
    const v = entry.v;
    if (t === null || typeof v !== "string") {
      continue;
    }
    changes.push({ t, v });
  }
  return changes;
}

function toCommandedChanges(value: unknown): CommandedChange[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const changes: CommandedChange[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) {
      continue;
    }
    const t = instantOrNull(entry.t);
    if (t === null) {
      continue;
    }
    changes.push({
      t,
      source: typeof entry.source === "string" && entry.source !== "" ? entry.source : null,
      direction: typeof entry.direction === "string" && entry.direction !== "" ? entry.direction : null,
      watts: finiteOrNull(entry.watts),
    });
  }
  return changes;
}

function toGaps(value: unknown): HistoryGap[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const gaps: HistoryGap[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) {
      continue;
    }
    const from = instantOrNull(entry.from);
    const to = instantOrNull(entry.to);
    if (from === null || to === null || to <= from) {
      continue;
    }
    gaps.push({ from, to });
  }
  return gaps;
}

function toUnitBlock(unitId: string, value: unknown, resolution: HistoryResolution): HistoryUnitBlock {
  const record = isRecord(value) ? value : {};
  const series: Record<string, HistorySeries> = {};
  if (isRecord(record.series)) {
    for (const [field, entry] of Object.entries(record.series)) {
      const parsed = toSeries(entry, resolution);
      if (parsed !== null) {
        series[field] = parsed;
      }
    }
  }
  return {
    unitId,
    firstSampleAt: instantOrNull(record.first_sample_at),
    lastSampleAt: instantOrNull(record.last_sample_at),
    sampleCount:
      typeof record.sample_count === "number" && Number.isFinite(record.sample_count)
        ? record.sample_count
        : 0,
    qualityWorst: typeof record.quality_worst === "string" && record.quality_worst !== ""
      ? record.quality_worst
      : null,
    gaps: toGaps(record.gaps),
    series,
    lifecycleChanges: toChanges(record.lifecycle_changes),
    healthStateChanges: toChanges(record.health_state_changes),
    commandedChanges: toCommandedChanges(record.commanded_changes),
  };
}

/**
 * Narrow the route's 200 body. Null when the value is not an object at all
 * (the caller words that as its unreadable-response state); a present-but-
 * thin body keeps every honest default — empty series, null firsts, no gaps —
 * never a fabricated figure.
 */
export function toPlantHistoryWindow(body: unknown): PlantHistoryWindow | null {
  if (!isRecord(body)) {
    return null;
  }
  const resolution: HistoryResolution = body.resolution === "hourly" ? "hourly" : "full";
  const units: Record<string, HistoryUnitBlock> = {};
  if (isRecord(body.units)) {
    for (const [unitId, entry] of Object.entries(body.units)) {
      units[unitId] = toUnitBlock(unitId, entry, resolution);
    }
  }
  let fleet: HistoryFleetBlock | null = null;
  if (isRecord(body.fleet)) {
    const series: Record<string, HistorySeries> = {};
    if (isRecord(body.fleet.series)) {
      for (const [field, entry] of Object.entries(body.fleet.series)) {
        const parsed = toSeries(entry, resolution);
        if (parsed !== null) {
          series[field] = parsed;
        }
      }
    }
    fleet = { series, gaps: toGaps(body.fleet.gaps) };
  }
  return {
    from: instantOrNull(body.from) ?? 0,
    to: instantOrNull(body.to) ?? 0,
    resolution,
    points: finiteOrNull(body.points) ?? 0,
    fields: Array.isArray(body.fields)
      ? body.fields.filter((field): field is string => typeof field === "string")
      : [],
    units,
    fleet,
  };
}

// --- the snapshot's recording state (feature-detected) ---------------------------

/** The snapshot's `history_state` block: the live "history is recording" hint. */
export interface HistoryRecordingState {
  readonly sampleIntervalS: number | null;
  readonly retentionFullResolutionDays: number | null;
  /** One epoch-ms instant per configured unit; null = not yet sampled. */
  readonly lastSampleAt: Readonly<Record<string, number | null>>;
  /** The fleet's newest sample instant, null while no unit has been sampled. */
  readonly lastSampleAtMax: number | null;
}

/**
 * Narrow the block. The whole BLOCK is the feature detection (absent key =
 * the historian is not composed — the route's 409 owns that state); inside a
 * present block every field is absent-tolerant per field.
 */
export function toHistoryRecordingState(snapshot: unknown): HistoryRecordingState | null {
  if (!isRecord(snapshot) || !isRecord(snapshot.history_state)) {
    return null;
  }
  const block = snapshot.history_state;
  const lastSampleAt: Record<string, number | null> = {};
  if (isRecord(block.last_sample_at)) {
    for (const [unitId, value] of Object.entries(block.last_sample_at)) {
      lastSampleAt[unitId] = instantOrNull(value);
    }
  }
  let newest: number | null = null;
  for (const at of Object.values(lastSampleAt)) {
    if (at !== null && (newest === null || at > newest)) {
      newest = at;
    }
  }
  return {
    sampleIntervalS: finiteOrNull(block.sample_interval_s),
    retentionFullResolutionDays: finiteOrNull(block.retention_full_resolution_days),
    lastSampleAt,
    lastSampleAtMax: newest,
  };
}

// --- the window's honesty figures --------------------------------------------------

/**
 * The share of the window the samples actually cover (the design's "98 % of
 * the hour covered" line): sampled time over window time. At full resolution
 * each sample vouches for one cadence interval; at hourly resolution each
 * rollup point vouches for `n` cadence intervals (never a full hour — a
 * half-covered hour is half-covered). Null when the cadence is unknown or the
 * window is empty; NEVER a zero standing in for "no data".
 */
export function coverageFraction(
  unit: HistoryUnitBlock,
  resolution: HistoryResolution,
  sampleIntervalS: number | null,
  windowMs: number,
): number | null {
  if (sampleIntervalS === null || sampleIntervalS <= 0 || windowMs <= 0) {
    return null;
  }
  let coveredS = 0;
  if (resolution === "hourly") {
    for (const series of Object.values(unit.series)) {
      if (series.kind !== "hourly") {
        continue;
      }
      coveredS = series.points.reduce((sum, point) => sum + point.n * sampleIntervalS, 0);
      break;
    }
  } else {
    coveredS = unit.sampleCount * sampleIntervalS;
  }
  if (coveredS <= 0) {
    return null;
  }
  return Math.min(1, coveredS / (windowMs / 1000));
}

/**
 * The window's extreme in the operator's words — "peak 2,510 W at 01:23" —
 * from the wire's own window extremes, so a peak the downsample dropped is
 * still named. "" when the series carries no extremes.
 */
export function extremeText(
  series: HistorySeries | undefined,
  unit: (value: number) => string,
): string {
  if (series === undefined) {
    return "";
  }
  const parts: string[] = [];
  if (series.stats.windowMin !== null) {
    const at = series.stats.windowMinAt;
    parts.push(
      `low ${unit(series.stats.windowMin)}${at === null ? "" : ` at ${localTime(at)}`}`,
    );
  }
  if (series.stats.windowMax !== null) {
    const at = series.stats.windowMaxAt;
    parts.push(
      `high ${unit(series.stats.windowMax)}${at === null ? "" : ` at ${localTime(at)}`}`,
    );
  }
  return parts.join(" · ");
}

// --- the quality word --------------------------------------------------------------

/**
 * The window's quality word as the operator reads it. The WIRE's word rides
 * verbatim (the design pins the vocabulary in the operator's face on purpose
 * — "stale" must say stale); this map only adds the one clause of context
 * and the honest gap word when there were no samples to judge.
 */
export function qualityWordText(word: string | null): string {
  switch (word) {
    case "good":
      return "good — every sample's readings were fresh";
    case "suspect":
      return "suspect — some readings were questionable";
    case "stale":
      return "stale — some readings were old when recorded";
    case "bad":
      return "bad — some readings failed their checks";
    case "missing":
      return "missing — some expected readings never arrived";
    default:
      return "no samples in this window";
  }
}

/** True only for the one word that owes no caveat (an unknown word is not it). */
export function qualityIsClean(word: string | null): boolean {
  return word === "good";
}

// --- the resolution tier --------------------------------------------------------

/**
 * The resolution badge: "30 s samples" vs "hourly rollup" — the tier the DATA
 * chose (the full-resolution horizon), never a client wish. The cadence
 * figure comes from the snapshot's own recording state; null keeps the
 * honest word without a number.
 */
export function resolutionText(
  resolution: HistoryResolution,
  sampleIntervalS: number | null,
): string {
  if (resolution === "hourly") {
    return "hourly rollup";
  }
  return sampleIntervalS === null ? "full-resolution samples" : `${sampleIntervalS} s samples`;
}

/**
 * The badge's honest caveat, stated once beside the tier: a window that opens
 * before the full-resolution horizon serves hourly FOR ITS ENTIREITY (§3.1 —
 * a mixed-resolution chart would change shape mid-window), and where no
 * rollup hours exist yet (the first 14 days of a new database) an hourly
 * window is legitimately empty.
 */
export function resolutionCaveatText(resolution: HistoryResolution): string {
  return resolution === "hourly"
    ? "this window opens before the full-resolution horizon, so the window serves hourly rollups for its entirety"
    : "every point is a real stored sample";
}

// --- the commanded triple ----------------------------------------------------------

/** One commanded segment: the triple held from one change point to the next. */
export interface CommandedSegment {
  readonly from: number;
  readonly to: number;
  readonly source: string | null;
  readonly direction: string | null;
  readonly watts: number | null;
}

/**
 * The commanded change points as held segments — the step encoding the chart
 * and the strip render. A segment runs from its change point to the next
 * (the last runs to the window's end — the sampled projection held until
 * proven otherwise by a later change or the window edge). All-null triple =
 * "no intent claimed the unit" and the watts stay NULL (never 0): the chart
 * shows nothing there and the strip says "nothing commanded".
 */
export function commandedSegments(
  changes: readonly CommandedChange[],
  windowTo: number,
): CommandedSegment[] {
  const segments: CommandedSegment[] = [];
  for (let index = 0; index < changes.length; index += 1) {
    const change = changes[index]!;
    const next = changes[index + 1];
    const to = Math.min(next === undefined ? windowTo : next.t, windowTo);
    if (to <= change.t) {
      continue;
    }
    segments.push({
      from: change.t,
      to,
      source: change.source,
      direction: change.direction,
      watts: change.watts,
    });
  }
  return segments;
}

/**
 * The commanded figure in the HOUSE sign convention the power charts draw
 * (charge negative, discharge positive — the wire's own battery-watts
 * convention, so the overlay and the measured line share one zero). Null —
 * never 0 — when nothing was claimed; `idle` commands an honest 0.
 */
export function commandedWattsSigned(
  segment: Pick<CommandedSegment, "direction" | "watts">,
): number | null {
  if (segment.direction === null || segment.watts === null) {
    return null;
  }
  if (segment.direction === "idle") {
    return 0;
  }
  return segment.direction === "charge" ? -segment.watts : segment.watts;
}

/** The commanded source in the operator's words (§2.2's vocabulary, decoded). */
export function commandedSourceText(source: string | null): string {
  switch (source) {
    case "manual":
      return "a manual request";
    case "agent":
      return "an agent";
    case "schedule":
      return "the schedule";
    case "excess_adviser":
      return "the solar-surplus adviser";
    case "night_adviser":
      return "the night-charge adviser";
    case "optimizer":
      return "the optimizer";
    default:
      return "no command";
  }
}

/** One commanded segment as a plain clause; the all-null triple says its own fact. */
export function commandedSegmentText(segment: CommandedSegment): string {
  if (segment.source === null || segment.direction === null) {
    return "nothing commanded";
  }
  const watts = segment.watts === null ? "" : ` ${formatWatts(segment.watts)}`;
  const direction =
    segment.direction === "charge"
      ? "charging"
      : segment.direction === "discharge"
        ? "discharging"
        : "idle";
  return `${commandedSourceText(segment.source)} — ${direction}${watts}`;
}

/** One held word segment (the strip's rows are made of these). */
export interface WordSegment {
  readonly from: number;
  readonly to: number;
  readonly v: string;
}

/**
 * Word change points as held segments — the lifecycle and health strips'
 * step encoding, the commanded segments' exact mechanics applied to plain
 * words. The last change holds to the window's end (the state was still true
 * when the window closed); the space BEFORE the first change renders empty,
 * never back-filled.
 */
export function wordSegments(
  changes: readonly HistoryChange[],
  windowTo: number,
): WordSegment[] {
  const segments: WordSegment[] = [];
  for (let index = 0; index < changes.length; index += 1) {
    const change = changes[index]!;
    const next = changes[index + 1];
    const to = Math.min(next === undefined ? windowTo : next.t, windowTo);
    if (to <= change.t) {
      continue;
    }
    segments.push({ from: change.t, to, v: change.v });
  }
  return segments;
}

// --- the archaeology strip's text half (the change-point listings) ---------------

/** One strip segment ready for wording: the band's label, the tooltip's fuller sentence, the listing's clause. */
export interface StripWordSegment {
  readonly from: number;
  readonly to: number;
  /** The band's short label — the word map's word, never the raw code. */
  readonly label: string;
  /** The tooltip's fuller sentence (the UnitHealthTag sentence, the full clause). */
  readonly title: string;
  /** The change-point listing's clause ("Disarmed", "nothing commanded"). */
  readonly clause: string;
}

/**
 * The strip's change-point listing, one item per segment, in the gap-notes
 * rhythm: the FIRST segment says its word "until" the next change, a middle
 * one carries both instants, the last says its word "since" its start (the
 * state was still true when the window closed — `wordSegments`' own
 * doctrine). This listing IS the strip's accessible half: every segment's
 * word and time survive here even when its proportional band is an
 * invisible sliver. Only the row's first item reads as the start of the
 * line; the rest keep their clause's own case.
 */
export function segmentListingItems(
  segments: readonly Pick<StripWordSegment, "from" | "to" | "clause">[],
  multiDay: boolean,
): string[] {
  const time = multiDay ? localDayTime : localTime;
  return segments.map((segment, index) => {
    const first = index === 0;
    const last = index === segments.length - 1;
    const clause =
      index === 0
        ? segment.clause.charAt(0).toUpperCase() + segment.clause.slice(1)
        : segment.clause;
    if (first && !last) {
      return `${clause} until ${time(segment.to)}`;
    }
    if (!first && !last) {
      return `${clause} ${time(segment.from)}–${time(segment.to)}`;
    }
    return `${clause} since ${time(segment.from)}`;
  });
}

/**
 * The hourly tier's strip absence: the rollups keep the numeric series but
 * drop the step-encoded words, so the section SAYS so instead of silently
 * vanishing (the 7 d/30 d presets serve hourly on a young database).
 */
export const STRIP_HOURLY_ABSENCE_TEXT =
  "State words are kept only inside the full-resolution window — pick a shorter range to see them";

/**
 * The health row's honest word when a unit recorded samples but no health
 * words: the recovery monitor is not part of this deployment — an absence
 * named, never a row that silently disappears.
 */
export const HEALTH_NOT_RECORDED_TEXT = "health was not recorded on this deployment";

// --- the recording note (the view's data-age line) -----------------------------------

/**
 * The History view's data-age line, keyed on the snapshot's own recording
 * state — the same honesty as the shell's live badge, aged by SAMPLES (the
 * historian's cadence), not by view events. The age rides as a ticking
 * figure; a recording gap past three cadences is named, never left to look
 * like a quiet chart.
 */
export function recordingNote(state: HistoryRecordingState | null, nowMs: number): string {
  if (state === null) {
    return "Recording state not available from the snapshot.";
  }
  const interval = state.sampleIntervalS;
  if (state.lastSampleAtMax === null) {
    return interval === null
      ? "History is recording — no samples have landed yet."
      : `History is recording — no samples have landed yet; the first appear within ${formatSeconds(interval)} of the controller starting.`;
  }
  const ageS = Math.max(0, (nowMs - state.lastSampleAtMax) / 1000);
  const staleAfterS = interval === null ? null : interval * 3;
  const word =
    staleAfterS !== null && ageS > staleAfterS
      ? "Recording has fallen quiet"
      : "History is recording";
  return `${word} — last sample ${formatSeconds(wholeSeconds(ageS))} ago`;
}

// --- the empty window's honest note ----------------------------------------------------

/**
 * The empty-window note — the FIRST state a fresh database shows its
 * operator (the historian has been recording for under an hour; a 7 d/30 d
 * window legitimately answers empty while no rollup hours exist). The note
 * says exactly which of the three honest empties this is, keyed on the
 * response's own facts, never on a guess.
 */
export function emptyWindowNote(
  window: PlantHistoryWindow,
  state: HistoryRecordingState | null,
): string {
  const recording =
    state !== null && state.lastSampleAtMax !== null
      ? "Recording is active — samples exist in the full-resolution tier."
      : state !== null
        ? "Recording is active — no samples have landed yet; the first appears within a cadence of the controller starting."
        : "Recording state is not available from the snapshot.";
  if (window.resolution === "hourly") {
    return `No rollup hours cover this window. ${recording} Hourly points appear once hours roll up past the full-resolution horizon (14 days by default) — pick the shortest range (6 h) to see the live samples${state !== null && state.retentionFullResolutionDays !== null ? ` (full-resolution samples are kept ${state.retentionFullResolutionDays} days)` : ""}.`;
  }
  return `No samples were recorded in this window. ${recording}`;
}

// --- local time (site-local rendering, the console's standing convention) --------------

/**
 * One instant as site-local clock time ("01:23", "14:05" — the operator's
 * wall clock, matching every other view's local rendering; the design's
 * examples are local times with the zone implied by the console itself).
 */
export function localTime(epochMs: number): string {
  const date = new Date(epochMs);
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${String(date.getHours()).padStart(2, "0")}:${minutes}`;
}

/** One instant as site-local day + clock time ("Aug 24 · 01:23") for multi-day windows. */
export function localDayTime(epochMs: number): string {
  const date = new Date(epochMs);
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${day} · ${localTime(epochMs)}`;
}

/** A gap interval as the accessible note the design pins ("no samples 02:10–03:40"). */
export function gapText(gap: HistoryGap, multiDay: boolean): string {
  const format = multiDay ? localDayTime : localTime;
  return `no samples ${format(gap.from)}–${format(gap.to)}`;
}

/** Why a gap exists, in the design's own words (the accessible note's second clause). */
export const GAP_REASON_TEXT =
  "controller down or unit unreachable — the space is left empty, never bridged";
