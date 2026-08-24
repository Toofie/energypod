/**
 * The forecast wire model (ARCHITECTURE section 17, the advisory outlook
 * read): the `GET /api/v1/forecast` body and the plain-language maps the
 * Insights view's solar section renders from.
 *
 * Honesty rules pinned here (the provider round's own doctrine, made
 * structural exactly like the history model's):
 *
 * - ADVISORY ONLY: no control path reads these figures; every wording in
 *   this module says "promise", never "measurement". The measured truth is
 *   the historian's recorded surplus, and ONLY the cross-check figures
 *   compare against it.
 * - CACHED AGE NEVER FRESHENS: `fetched_at` is when the controller fetched;
 *   the staleness report's `stale` verdict is the provider's own threshold,
 *   rendered verbatim (never re-derived, never hidden behind a color alone).
 * - A BAND EXISTS ONLY WHERE THE SOURCE CLAIMS ONE: `q10`/`q90` ride only on
 *   quantiled sources (Solcast); a deterministic source (Open-Meteo) has no
 *   band, and the model words that instead of drawing a fabricated one.
 * - BIAS IS SIGNED WITH THE OPERATOR'S WORDS: positive bias = over-forecast =
 *   an OPTIMISTIC promise — the forecast that under-charges overnight when
 *   trusted, because the morning surplus it promised never arrives.
 * - THE SCORED BASIS IS DISCLOSED, NEVER IMPLIED (the night-v2 panel's
 *   amendment A1): the scorer compares against RECORDED grid export, which is
 *   surplus AFTER the pods absorb it. A morning the pods absorb (the desired
 *   outcome) collapses recorded export and scores as a miss that is not the
 *   forecast's — so every accuracy figure renders with its basis named, and
 *   the corrected basis's raw inputs (recorded export, charging rate, the
 *   reconstructed pre-battery surplus) ride the read as INPUTS, never as a
 *   second verdict. No accuracy number renders as a single unqualified
 *   judgment of the provider.
 * - NO VERDICT THEATER AT SMALL N: the scoreboard's accumulation is per FETCH
 *   and per controller run (`durable: false` says so on every response); at
 *   small record counts the wording says "too few for a verdict" instead of
 *   dressing one fetch's numbers as a judgment.
 * - ABSENT DATA RENDERS ABSENT: a null PV outlook, a null score, a null
 *   scoreboard are each their own honest state with their own words — never
 *   a zero, never a blank.
 */
import { formatSeconds, formatWatts, wholeSeconds } from "../lib/format";
import { isRecord } from "./fleet";
import { localDayTime } from "./history";

// --- the parsed read ----------------------------------------------------------

/** One forecast interval: half-open [start, end), the central watts, the band. */
export interface ForecastInterval {
  readonly start: number;
  readonly end: number;
  readonly w: number;
  /** Null for a deterministic source: no band was claimed, none is drawn. */
  readonly q10: number | null;
  readonly q90: number | null;
}

/** The provider's own honest age report (verbatim; the verdict is its own). */
export interface ProviderStalenessView {
  readonly source: string;
  readonly fetchedAt: number | null;
  readonly ageS: number | null;
  readonly stale: boolean;
  readonly lastError: string | null;
  readonly fetchCount: number;
  readonly errorCount: number;
}

/** The normalized PV outlook, verbatim from the provider's cache. */
export interface PvOutlook {
  readonly source: string;
  readonly variable: string;
  readonly fetchedAt: number | null;
  readonly issuedAt: number | null;
  readonly quantiled: boolean;
  readonly horizonFrom: number | null;
  readonly horizonTo: number | null;
  readonly intervals: readonly ForecastInterval[];
}

/** The corrected basis's raw inputs (amendment A1), served per read. */
export interface ForecastBasisView {
  readonly pairedSamples: number;
  readonly meanForecastW: number | null;
  readonly meanExportW: number | null;
  readonly meanPreBatterySurplusW: number | null;
  readonly meanChargingW: number | null;
}

/** One fetch's cross-check against the recorded surplus (the elapsed window). */
export interface ForecastScoreView {
  readonly source: string;
  readonly fetchedAt: number | null;
  readonly windowFrom: number | null;
  readonly windowTo: number | null;
  readonly samples: number;
  readonly maeW: number;
  /** Positive = over-forecast (an optimistic promise). */
  readonly biasW: number;
  readonly rmseW: number;
  /** The share of recorded surplus inside [q10, q90]; null when none claimed. */
  readonly insideBand: number | null;
  /** The corrected basis's raw inputs; absent when the pairing is empty. */
  readonly basis: ForecastBasisView | null;
}

/** The accumulating per-fetch evidence (in memory only). */
export interface ForecastScoreboardView {
  readonly source: string;
  readonly since: number | null;
  readonly records: number;
  readonly totalSamples: number;
  readonly meanBiasW: number | null;
  readonly meanMaeW: number | null;
  readonly meanInsideBand: number | null;
  readonly durable: boolean;
}

/** The whole 200 body, narrowed. */
export interface ForecastOutlookView {
  readonly asOf: number;
  readonly historyComposed: boolean;
  readonly pv: PvOutlook | null;
  readonly provider: ProviderStalenessView | null;
  readonly notes: readonly string[];
  readonly score: ForecastScoreView | null;
  readonly scoreboard: ForecastScoreboardView | null;
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

function toStaleness(source: string, value: unknown): ProviderStalenessView {
  const record = isRecord(value) ? value : {};
  return {
    source,
    fetchedAt: instantOrNull(record.fetched_at),
    ageS: finiteOrNull(record.age_s),
    stale: record.stale === true,
    lastError: typeof record.last_error === "string" && record.last_error !== ""
      ? record.last_error
      : null,
    fetchCount: finiteOrNull(record.fetch_count) ?? 0,
    errorCount: finiteOrNull(record.error_count) ?? 0,
  };
}

function toIntervals(value: unknown): ForecastInterval[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const intervals: ForecastInterval[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) {
      continue;
    }
    const start = instantOrNull(entry.start);
    const end = instantOrNull(entry.end);
    const w = finiteOrNull(entry.w);
    if (start === null || end === null || w === null || end <= start) {
      continue;
    }
    intervals.push({
      start,
      end,
      w,
      q10: finiteOrNull(entry.q10),
      q90: finiteOrNull(entry.q90),
    });
  }
  return intervals;
}

function toPv(value: unknown): PvOutlook | null {
  if (!isRecord(value)) {
    return null;
  }
  const intervals = toIntervals(value.intervals);
  return {
    source: typeof value.source === "string" && value.source !== "" ? value.source : "",
    variable: typeof value.variable === "string" && value.variable !== "" ? value.variable : "",
    fetchedAt: instantOrNull(value.fetched_at),
    issuedAt: instantOrNull(value.issued_at),
    quantiled: value.quantiled === true,
    horizonFrom: instantOrNull(value.horizon_from),
    horizonTo: instantOrNull(value.horizon_to),
    intervals,
  };
}

function toBasis(value: unknown): ForecastBasisView | null {
  if (!isRecord(value)) {
    return null;
  }
  const paired = finiteOrNull(value.paired_samples);
  if (paired === null) {
    return null;
  }
  return {
    pairedSamples: Math.floor(paired),
    meanForecastW: finiteOrNull(value.mean_forecast_w),
    meanExportW: finiteOrNull(value.mean_export_w),
    meanPreBatterySurplusW: finiteOrNull(value.mean_pre_battery_surplus_w),
    meanChargingW: finiteOrNull(value.mean_charging_w),
  };
}

function toScore(value: unknown): ForecastScoreView | null {
  if (!isRecord(value)) {
    return null;
  }
  const samples = finiteOrNull(value.samples);
  const mae = finiteOrNull(value.mae_w);
  const bias = finiteOrNull(value.bias_w);
  const rmse = finiteOrNull(value.rmse_w);
  if (samples === null || mae === null || bias === null || rmse === null) {
    return null;
  }
  return {
    source: typeof value.source === "string" && value.source !== "" ? value.source : "",
    fetchedAt: instantOrNull(value.fetched_at),
    windowFrom: instantOrNull(value.window_from),
    windowTo: instantOrNull(value.window_to),
    samples: Math.floor(samples),
    maeW: mae,
    biasW: bias,
    rmseW: rmse,
    insideBand: finiteOrNull(value.inside_band),
    basis: value.basis === undefined || value.basis === null ? null : toBasis(value.basis),
  };
}

function toScoreboard(value: unknown): ForecastScoreboardView | null {
  if (!isRecord(value)) {
    return null;
  }
  const records = finiteOrNull(value.records);
  if (records === null) {
    return null;
  }
  return {
    source: typeof value.source === "string" && value.source !== "" ? value.source : "",
    since: instantOrNull(value.since),
    records: Math.floor(records),
    totalSamples: finiteOrNull(value.total_samples) ?? 0,
    meanBiasW: finiteOrNull(value.mean_bias_w),
    meanMaeW: finiteOrNull(value.mean_mae_w),
    meanInsideBand: finiteOrNull(value.mean_inside_band),
    durable: value.durable === true,
  };
}

/**
 * Narrow the route's 200 body. Null when the value is not an object at all
 * (the caller words that as its unreadable-response state); a present-but-
 * thin body keeps every honest default — no PV, no score, no scoreboard —
 * never a fabricated figure.
 */
export function toForecastOutlook(body: unknown): ForecastOutlookView | null {
  if (!isRecord(body)) {
    return null;
  }
  const provider: ProviderStalenessView | null = isRecord(body.provider)
    ? toStaleness(
        typeof body.provider.source === "string" && body.provider.source !== ""
          ? body.provider.source
          : "",
        body.provider.staleness,
      )
    : null;
  return {
    asOf: instantOrNull(body.as_of) ?? 0,
    historyComposed: body.history_composed === true,
    pv: body.pv === null || body.pv === undefined ? null : toPv(body.pv),
    provider,
    notes: Array.isArray(body.notes)
      ? body.notes.filter((note): note is string => typeof note === "string" && note !== "")
      : [],
    score: body.score === null || body.score === undefined ? null : toScore(body.score),
    scoreboard:
      body.scoreboard === null || body.scoreboard === undefined
        ? null
        : toScoreboard(body.scoreboard),
  };
}

// --- the words ------------------------------------------------------------------

/** The source in the operator's words (the wire's own name, decoded). */
export function sourceText(source: string): string {
  switch (source) {
    case "solcast":
      return "Solcast (the rooftop PV forecast)";
    case "open-meteo":
      return "Open-Meteo (the derived PV forecast)";
    case "historian-baseline":
      return "the historian baseline";
    default:
      return source === "" ? "the forecast provider" : source;
  }
}

/**
 * The signed bias in the operator's words. Positive bias = over-forecast =
 * OPTIMISTIC — the promise that under-charges overnight when trusted, the
 * exact failure mode this scoreboard exists to surface. The magnitude rides
 * with the word; a bias of exactly 0 is the honest "even".
 */
export function biasText(biasW: number): string {
  const magnitude = formatWatts(Math.abs(biasW));
  if (biasW > 0) {
    return `over-forecast by ${magnitude} on average — an optimistic promise; a night plan that trusts it under-charges`;
  }
  if (biasW < 0) {
    return `under-forecast by ${magnitude} on average — a pessimistic promise; a night plan that trusts it over-charges`;
  }
  return `even on average (${magnitude} bias)`;
}

/** The coverage figure, or the honest no-band answer. */
export function coverageText(insideBand: number | null): string {
  if (insideBand === null) {
    return "the source claims no uncertainty band, so there is no coverage figure";
  }
  return `${Math.round(insideBand * 100)}% of the recorded surplus fell inside its claimed band`;
}

/**
 * The fetched-at age line; null ages word themselves, never "0 s ago". With a
 * wall clock the age TICKS from the fetch instant (the age of the DATA, which
 * only a new fetch can reset — rereading the cache never freshens it). When
 * the wall clock disagrees with the fetch stamp (an age at or below zero —
 * clock skew), the provider's own reported age is the honest figure instead.
 */
export function fetchedAgeText(
  staleness: ProviderStalenessView | null,
  nowMs?: number,
): string {
  if (staleness === null || staleness.fetchedAt === null) {
    return "never fetched";
  }
  let ageS: number | null = staleness.ageS;
  if (nowMs !== undefined) {
    const wallAgeS = (nowMs - staleness.fetchedAt) / 1000;
    if (wallAgeS > 0) {
      ageS = wallAgeS;
    }
  }
  if (ageS === null) {
    return "fetched (age not reported)";
  }
  return `fetched ${formatSeconds(wholeSeconds(Math.max(0, ageS)))} ago`;
}

/** The horizon clause ("reaches Aug 27 · 12:00"); absent horizons word themselves. */
export function horizonText(pv: PvOutlook): string {
  if (pv.horizonTo === null) {
    return "horizon not available";
  }
  return `reaches ${localDayTime(pv.horizonTo)}`;
}

/**
 * The scoreboard's accumulation honesty. NO VERDICT THEATER AT SMALL N: under
 * the small-sample bound the wording counts the evidence and declines the
 * verdict; above it the wording still names the count and the start date.
 */
export const SCOREBOARD_SMALL_N = 5;

export function evidenceText(board: ForecastScoreboardView): string {
  const since = board.since === null ? "an unknown start" : localDayTime(board.since);
  const fetches = board.records === 1 ? "1 fetch" : `${board.records} fetches`;
  if (board.records === 0) {
    return "no forecast fetch has been checked against the recorded surplus yet";
  }
  if (board.records < SCOREBOARD_SMALL_N) {
    return `evidence accumulating since ${since} — ${fetches} checked so far, too few for a verdict`;
  }
  return `evidence accumulating since ${since} — ${fetches} checked`;
}

/** The not-durable line, worded once per scoreboard render. */
export const NOT_DURABLE_TEXT =
  "the accumulation is this controller run's memory only — a restart starts the evidence over";

/** The history-absent line (the scorer's truth is the historian's own rows). */
export const HISTORY_ABSENT_TEXT =
  "the plant-history block is not composed on this site, so no accuracy evidence can accumulate here";

/** The score's honest empty: which of the two empties this is. */
export function scoreEmptyText(outlook: ForecastOutlookView): string {
  if (!outlook.historyComposed) {
    return HISTORY_ABSENT_TEXT;
  }
  return "no elapsed minutes of this fetch have recorded surplus to check yet — the evidence grows as the fetch ages";
}

/**
 * The basis disclosure, stated once wherever an accuracy figure renders: the
 * scorer's recorded basis is POST-battery, so absorption (the desired
 * outcome) reads as a miss. The corrected pre-battery basis is the night-v2
 * round's rebuild; the inputs ride this read.
 */
export const BASIS_DISCLOSURE_TEXT =
  "Basis honesty: these figures compare the forecast against recorded grid export — surplus AFTER the pods absorb it. A morning the pods absorb (the desired outcome) collapses recorded export and scores as a miss that is not the forecast's; the corrected pre-battery basis (recorded export + charging) is the next round's rebuild, and its raw inputs ride this read.";

/**
 * The corrected basis's inputs as one plain clause — INPUTS, never a verdict:
 * the operator reads the reconstruction the night-v2 round will build on,
 * worded as what it is. "" when the basis is absent.
 */
export function basisInputsText(basis: ForecastBasisView | null): string {
  if (basis === null || basis.meanForecastW === null || basis.meanExportW === null) {
    return "";
  }
  const parts: string[] = [
    `promised ${formatWatts(basis.meanForecastW)} on average`,
    `recorded export ${formatWatts(basis.meanExportW)}`,
  ];
  if (basis.meanPreBatterySurplusW !== null) {
    parts.push(
      `pre-battery surplus ${formatWatts(basis.meanPreBatterySurplusW)} (export + charging, reconstructed)`,
    );
  }
  if (basis.meanChargingW !== null) {
    parts.push(`charging ${formatWatts(basis.meanChargingW)}`);
  }
  return `${parts.join(" · ")} — over ${basis.pairedSamples} paired ${
    basis.pairedSamples === 1 ? "sample" : "samples"
  }`;
}
