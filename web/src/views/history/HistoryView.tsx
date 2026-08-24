/**
 * "History" — the plant-history console view (DESIGN_PLANT_HISTORY.md §4):
 * what the plant actually DID, as it happened — per battery and whole-site
 * power, charge, grid exchange, temperature and cell figures over a chosen
 * window, with the honesty the design pins made structural:
 *
 * - GAPS RENDER AS GAPS (absent rows, never bridged — the chart layer plants
 *   the breaks, the strip and the notes say them in words).
 * - NULLS ARE NULLS (a missing series words "not available"; nothing zeros).
 * - THE RESOLUTION TIER IS THE DATA'S CHOICE, labeled with its caveat ("30 s
 *   samples" vs "hourly rollup" — the boundary is the full-resolution
 *   horizon, so the 7 d/30 d presets honestly serve hourly on a young
 *   database).
 * - THE QUALITY WORD RIDES VERBATIM beside every unit's charts (per-sample
 *   quality is not on this wire; the window's worst word is, and it renders).
 * - THE COMMANDED TRIPLE OVERLAYS THE MEASURED WATTS — what WE asked beside
 *   what happened, the sampled 30 s projection (where it disagrees with the
 *   audit trail, the audit row is the record) — and the all-null periods say
 *   "nothing commanded" instead of drawing a zero.
 * - THE EMPTY WINDOW IS A STATE, not a failure: the first thing a fresh
 *   database's operator sees tomorrow morning is the honest "no samples in
 *   this window — recording is active, pick a shorter range", never fake
 *   data and never a spinner that lies.
 *
 * The view is read-only by construction (its only controls are window,
 * battery and refresh), rides the session's shared client, and re-queries on
 * its own cadence — the historian publishes no bus event by design (samples
 * are projections, not acts), so polling IS the update path.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type JSX } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  commandedBandLabels,
  commandedSegmentText,
  commandedSegments,
  commandedWattsSigned,
  coverageFraction,
  emptyWindowNote,
  extremeText,
  fittingBandLabel,
  gapText,
  GAP_REASON_TEXT,
  HEALTH_NOT_RECORDED_TEXT,
  HISTORY_RANGE_PRESETS,
  historyQuery,
  localDayTime,
  localTime,
  qualityIsClean,
  qualityWordText,
  rangeLabel,
  recordingNote,
  resolutionCaveatText,
  resolutionText,
  segmentListingItems,
  STRIP_HOURLY_ABSENCE_TEXT,
  toHistoryRecordingState,
  toPlantHistoryWindow,
  wordSegments,
  type HistoryGap,
  type StripWordSegment,
  type HistoryRecordingState,
  type HistorySeries,
  type HistoryUnitBlock,
  type PlantHistoryWindow,
} from "../../app/history";
import type { HistoryRangeId } from "../../app/history";
import {
  healthBandWords,
  HEALTH_STATES,
  healthWord,
  lifecycleBandWords,
  lifecycleWord,
  type HealthState,
} from "../../app/fleet";
import { unitHealthSentence } from "../../app/unitHealth";
import {
  formatMillivolts,
  formatPercent,
  formatTemp,
  formatVolts,
  formatWatts,
} from "../../lib/format";
import { buildChart, commandedStepSegments } from "./chartData";
import type { BuiltChart, ChartSpec } from "./chartData";
import { TimeSeriesChart } from "./TimeSeriesChart";
import type { ChartAxis, ChartStyle } from "./TimeSeriesChart";
import "./history.css";

/** The one prop the shell hands every mounted view (views.ts ShellViewProps). */
export interface HistoryViewProps {
  client: ApiClient;
}

type Phase = "loading" | "ready" | "not-commissioned" | "error";

interface ErrorView {
  code: string;
  message: string;
  requestId: string;
}

/** The live re-query cadence: the historian's own default sample interval. */
const REFRESH_MS = 30_000;
/** The age ticker's cadence (the recording note's "last sample N s ago"). */
const TICK_MS = 1000;

const NOT_COMMISSIONED_CODE = "plant_history_not_commissioned";
const FLEET = "fleet";

// The palette names the console's own tokens (styles.css :root); canvas
// strokes cannot read var() directly, so the hex values ARE the tokens.
const COLORS = {
  /** --armed: the battery family's gold. */
  battery: "#8a6d1a",
  /** --active: the grid family's blue. */
  grid: "#1d5fa0",
  /** --ink-soft: the house family's quiet slate. */
  load: "#56637a",
  /** --ink: the commanded overlay's ink (dashed — OUR ask, not a reading). */
  commanded: "#1f2733",
  /** --yes: the authoritative BMS charge level. */
  soc: "#1d7a3e",
  /** --warn: the cell-spread early-warning line. */
  spread: "#8a5a00",
} as const;

const BATTERY_FILL = "rgba(138,109,26,0.12)";
const BAND_FILL = "rgba(29,95,160,0.10)";

function toErrorView(error: unknown): ErrorView {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message, requestId: error.request_id };
  }
  return {
    code: "history_unavailable",
    message: error instanceof Error ? error.message : "The history window could not be loaded.",
    requestId: "",
  };
}

export function HistoryView({ client }: HistoryViewProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<ErrorView | null>(null);
  const [range, setRange] = useState<HistoryRangeId>("today");
  const [scope, setScope] = useState<string>(FLEET);
  const [window, setWindow] = useState<PlantHistoryWindow | null>(null);
  const [recording, setRecording] = useState<HistoryRecordingState | null>(null);
  const [unitIds, setUnitIds] = useState<string[]>([]);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  const [reloadNonce, setReloadNonce] = useState(0);

  const refresh = useCallback((): void => {
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  /** A range switch is a NEW window: the old picture never stands in for it. */
  const selectRange = useCallback((next: HistoryRangeId): void => {
    setRange(next);
    setWindow(null);
    setError(null);
    setPhase("loading");
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  // The load: one snapshot read (the unit list + the recording hint — both
  // feature-detected) and one history read for the window. Only the range,
  // the client's identity, or a refresh re-run it; a range switch returns to
  // the loading state so the window never mixes two ranges' figures.
  useEffect(() => {
    let cancelled = false;
    setPhase((current) => (current === "error" || current === "not-commissioned" ? "loading" : current));
    void (async () => {
      try {
        const snapshot = await client.getSnapshot();
        if (cancelled) {
          return;
        }
        setRecording(toHistoryRecordingState(snapshot));
        setUnitIds(snapshot.units.map((unit) => unit.unit_id));
      } catch {
        // The history route answers on its own; a failed snapshot read must
        // not kill the charts (the recording note words the absence).
        if (!cancelled) {
          setRecording(null);
        }
      }
      try {
        const body = await client.getPlantHistory(historyQuery(range, new Date()));
        if (cancelled) {
          return;
        }
        const parsed = toPlantHistoryWindow(body);
        if (parsed === null) {
          setError({
            code: "unreadable_history_window",
            message: "The history response could not be read.",
            requestId: "",
          });
          setPhase("error");
          return;
        }
        setWindow(parsed);
        setFetchedAt(Date.now());
        setError(null);
        setPhase("ready");
      } catch (failure) {
        if (cancelled) {
          return;
        }
        const view = toErrorView(failure);
        setError(view);
        setPhase(view.code === NOT_COMMISSIONED_CODE ? "not-commissioned" : "error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, range, reloadNonce]);

  // The live re-query: while the tab is visible, the window slides forward
  // on the historian's own cadence (a hidden tab does not poll — its timers
  // are throttled anyway; a manual Refresh and any range switch re-fetch now).
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") {
        refresh();
      }
    }, REFRESH_MS);
    return () => {
      clearInterval(timer);
    };
  }, [refresh]);

  // The age ticker: one render a second while the view is mounted, feeding
  // only the recording note's figure — the chart specs below are memoized on
  // the window's own identity, so nothing under them rebuilds on the tick.
  useEffect(() => {
    const timer = setInterval(() => {
      setNowMs(Date.now());
    }, TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);

  const multiDay = useMemo(
    () => (fetchedAt ?? nowMs) - (window?.from ?? 0) > 2 * 86_400_000,
    [fetchedAt, nowMs, window],
  );

  const headingId = "history-heading";

  if (phase === "loading" && window === null) {
    return (
      <section className="history-view" aria-labelledby={headingId}>
        <h2 id={headingId}>History</h2>
        <p role="status" className="history-loading">
          Loading the recorded window…
        </p>
      </section>
    );
  }

  if (phase === "not-commissioned") {
    return (
      <section className="history-view" aria-labelledby={headingId}>
        <h2 id={headingId}>History</h2>
        <div role="note" className="history-not-commissioned">
          <h3>Plant history is not commissioned</h3>
          <p>
            The telemetry historian is not commissioned in this deployment&apos;s config — nothing is
            being recorded, so there is nothing to show here. Commissioning is a config change on
            the controller: add the <code>plant_history</code> block (it uses the existing
            database path) and restart, and this view starts filling the moment the controller
            boots.
          </p>
          <p className="history-error-code">
            Code: <code>{error?.code ?? NOT_COMMISSIONED_CODE}</code>
          </p>
          {error !== null && <p className="history-error-message">{error.message}</p>}
          <button type="button" className="history-retry" onClick={refresh}>
            Check again
          </button>
        </div>
      </section>
    );
  }

  if (phase === "error" && window === null) {
    return (
      <section className="history-view" aria-labelledby={headingId}>
        <h2 id={headingId}>History</h2>
        <div role="alert" className="history-error">
          <p className="history-error-title">We couldn&apos;t load the recorded window.</p>
          <p className="history-error-code">
            Code: <code>{error?.code ?? "unknown"}</code>
          </p>
          {error !== null && <p className="history-error-message">{error.message}</p>}
          {error !== null && error.requestId !== "" && (
            <p className="history-error-request">
              Request ID: <code>{error.requestId}</code>
            </p>
          )}
          <button type="button" className="history-retry" onClick={refresh}>
            Try again
          </button>
        </div>
      </section>
    );
  }

  if (window === null) {
    return (
      <section className="history-view" aria-labelledby={headingId}>
        <h2 id={headingId}>History</h2>
        <p role="status" className="history-loading">
          Loading the recorded window…
        </p>
      </section>
    );
  }

  const intervalS = recording?.sampleIntervalS ?? null;
  const anyData =
    Object.values(window.units).some((unit) => unit.sampleCount > 0) ||
    Object.values(window.fleet?.series ?? {}).some((series) => series.points.length > 0);
  const scopeUnit = scope === FLEET ? null : (window.units[scope] ?? null);

  return (
    <section className="history-view" aria-labelledby={headingId}>
      <div className="history-heading-row">
        <h2 id={headingId}>History</h2>
        <p role="status" className="history-recording">
          {recordingNote(recording, nowMs)}
        </p>
      </div>

      {/* The window controls — the view's only controls, all read-only. */}
      <div className="history-controls">
        <fieldset className="history-ranges">
          <legend>Window</legend>
          {HISTORY_RANGE_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              className="history-range"
              aria-pressed={range === preset.id}
              onClick={() => {
                selectRange(preset.id);
              }}
            >
              {preset.label}
            </button>
          ))}
        </fieldset>
        <fieldset className="history-scopes">
          <legend>Battery</legend>
          <button
            type="button"
            className="history-scope"
            aria-pressed={scope === FLEET}
            onClick={() => {
              setScope(FLEET);
            }}
          >
            Whole site
          </button>
          {unitIds.map((unitId) => (
            <button
              key={unitId}
              type="button"
              className="history-scope"
              aria-pressed={scope === unitId}
              onClick={() => {
                setScope(unitId);
              }}
            >
              {unitId}
            </button>
          ))}
        </fieldset>
        <button type="button" className="history-refresh" onClick={refresh}>
          Refresh
        </button>
      </div>

      {/* The honesty line: the resolution tier the DATA chose, its caveat,
          and the window's own bounds. */}
      <p className="history-resolution" role="note">
        <b className="history-resolution-word">{resolutionText(window.resolution, intervalS)}</b>
        {" — "}
        {resolutionCaveatText(window.resolution)} · window {localTime(window.from)}–
        {localTime(window.to)}
        {fetchedAt !== null ? `, loaded ${localTime(fetchedAt)}` : ""}
      </p>

      {anyData ? null : (
        <div role="note" className="history-empty">
          <h3>No recorded samples in this window</h3>
          <p>{emptyWindowNote(window, recording)}</p>
        </div>
      )}

      {/* A refresh that failed while a window was already on screen: the
          last-known picture STAYS (dimmed by its note, never dropped), and
          the refusal rides above it with its own retry. */}
      {error !== null && phase === "error" ? (
        <div role="alert" className="history-error history-error--refresh">
          <p className="history-error-title">The latest refresh failed — showing the last loaded window.</p>
          <p className="history-error-code">
            Code: <code>{error.code}</code>
          </p>
          <p className="history-error-message">{error.message}</p>
          <button type="button" className="history-retry" onClick={refresh}>
            Try again
          </button>
        </div>
      ) : null}

      {/* THE FLEET BLOCK — the glance path: summed power for the whole site. */}
      {scope === FLEET ? (
        <FleetSection window={window} multiDay={multiDay} />
      ) : scopeUnit === null ? (
        <div role="note" className="history-empty">
          <h3>{scope} has no samples in this window</h3>
          <p>
            Nothing was recorded for this battery inside {rangeLabel(range)}. A gap is honest — the
            space stays empty rather than borrowing another battery&apos;s figures.
          </p>
        </div>
      ) : (
        <UnitSection unit={scopeUnit} window={window} intervalS={intervalS} multiDay={multiDay} />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// the fleet block
// ---------------------------------------------------------------------------

function FleetSection({
  window,
  multiDay,
}: {
  window: PlantHistoryWindow;
  multiDay: boolean;
}): JSX.Element | null {
  const fleet = window.fleet;
  const specs = useMemo(
    () => (fleet === null ? null : buildFlowChart(window, fleet.series, fleet.gaps, null, false)),
    [fleet, window],
  );
  if (fleet === null || specs === null) {
    return null;
  }
  return (
    <section className="history-fleet" aria-label="Whole-site history">
      <h3>Whole site — power</h3>
      <p className="history-fleet-note">
        The summed flows across every configured battery. A fleet point exists only where every
        battery has a row — one unreadable phase is never treated as zero.
      </p>
      <TimeSeriesChart
        label="Whole-site power over the window"
        from={window.from}
        to={window.to}
        built={specs.built}
        styles={specs.styles}
        axes={specs.axes}
        multiDay={multiDay}
        summary={specs.summary}
        summaryLabel="Whole-site power over the window"
        gapNotes={fleet.gaps.map((gap) => `${gapText(gap, multiDay)} — ${GAP_REASON_TEXT}`)}
      />
      <p className="history-fleet-soc-note">
        Charge levels, temperatures and cell figures are per battery — pick one above.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// the per-battery block
// ---------------------------------------------------------------------------

function UnitSection({
  unit,
  window,
  intervalS,
  multiDay,
}: {
  unit: HistoryUnitBlock;
  window: PlantHistoryWindow;
  intervalS: number | null;
  multiDay: boolean;
}): JSX.Element {
  // One memo per chart, keyed on the window's identity — a re-fetch replaces
  // the window, the age ticker replaces nothing.
  const power = useMemo(
    () => buildFlowChart(window, unit.series, unit.gaps, unit, true),
    [window, unit],
  );
  const soc = useMemo(() => buildSocChart(window, unit), [window, unit]);
  const temperature = useMemo(() => buildTemperatureChart(window, unit), [window, unit]);
  const cells = useMemo(() => buildCellsChart(window, unit), [window, unit]);

  const coverage = coverageFraction(unit, window.resolution, intervalS, window.to - window.from);
  const gapNotes = unit.gaps.map((gap) => `${gapText(gap, multiDay)} — ${GAP_REASON_TEXT}`);

  return (
    <>
      <p className="history-unit-facts">
        <b className="history-unit-id">{unit.unitId}</b> — {unit.sampleCount.toLocaleString()}{" "}
        samples
        {coverage === null ? "" : ` · ${formatPercent(coverage * 100)} of the window covered`} ·{" "}
        <span
          className={
            qualityIsClean(unit.qualityWorst) || unit.qualityWorst === null
              ? "history-quality"
              : "history-quality history-quality--degraded"
          }
        >
          quality {qualityWordText(unit.qualityWorst)}
        </span>
      </p>

      <section className="history-block" aria-label={`${unit.unitId} battery power and grid exchange`}>
        <h3>Battery power, grid and house</h3>
        <TimeSeriesChart
          label={`${unit.unitId} power over the window`}
          from={window.from}
          to={window.to}
          built={power.built}
          styles={power.styles}
          axes={power.axes}
          multiDay={multiDay}
          summary={power.summary}
          summaryLabel={`${unit.unitId} power over the window`}
          gapNotes={gapNotes}
        />
      </section>

      <section className="history-block" aria-label={`${unit.unitId} charge level`}>
        <h3>Charge level</h3>
        <TimeSeriesChart
          label={`${unit.unitId} charge level over the window`}
          from={window.from}
          to={window.to}
          built={soc.built}
          styles={soc.styles}
          axes={soc.axes}
          height={180}
          multiDay={multiDay}
          summary={soc.summary}
          summaryLabel={`${unit.unitId} charge level over the window`}
          gapNotes={gapNotes}
        />
      </section>

      {/* THE ARCHAEOLOGY STRIP — the step series under the charts: what the
          unit was, how it was judged, and what WE commanded, as held bands
          whose every word also survives for the screen reader (the offpage
          listing) and for the hovering operator (every band's tooltip). */}
      <StepStrip unit={unit} window={window} multiDay={multiDay} />

      {/* The secondary group: cells and temperature — the homeowner scans
          power and charge first; these are the diagnosis figures. */}
      <details className="history-secondary">
        <summary>Cells and temperature</summary>
        <section className="history-block" aria-label={`${unit.unitId} temperatures`}>
          <h4>Temperatures</h4>
          <TimeSeriesChart
            label={`${unit.unitId} temperatures over the window`}
            from={window.from}
            to={window.to}
            built={temperature.built}
            styles={temperature.styles}
            axes={temperature.axes}
            height={180}
            multiDay={multiDay}
            summary={temperature.summary}
            summaryLabel={`${unit.unitId} temperatures over the window`}
            gapNotes={gapNotes}
          />
        </section>
        <section className="history-block" aria-label={`${unit.unitId} cell voltages`}>
          <h4>Cell voltages and spread</h4>
          <TimeSeriesChart
            label={`${unit.unitId} cell voltages over the window`}
            from={window.from}
            to={window.to}
            built={cells.built}
            styles={cells.styles}
            axes={cells.axes}
            height={200}
            multiDay={multiDay}
            summary={cells.summary}
            summaryLabel={`${unit.unitId} cell voltages over the window`}
            gapNotes={gapNotes}
          />
        </section>
      </details>
    </>
  );
}

// ---------------------------------------------------------------------------
// the archaeology strip (fitted bands; the listing is assistive-tech-only)
// ---------------------------------------------------------------------------

/** The strip's one muted explainer: what each row asserts, where each fact
 * lives now that the bar is the whole visual story (word on the band where
 * it fits, the full sentence + times on every band's tooltip), plus the
 * design's §6 caveat (the sampled view versus the audit trail) said where it
 * counts. */
const STRIP_EXPLAINER_TEXT =
  "Lifecycle is the state the controller held; health is the recovery monitor's judgment; commanded is what this console asked of the battery. Each band carries the fullest word its width holds; hover any band for its full sentence and times. The strip is a 30-second sampled view — where it disagrees with the audit trail, the audit row is the record.";

function StepStrip({
  unit,
  window,
  multiDay,
}: {
  unit: HistoryUnitBlock;
  window: PlantHistoryWindow;
  multiDay: boolean;
}): JSX.Element | null {
  // The hourly tier (7 d/30 d presets) keeps the numeric series but drops
  // the step-encoded words: the section renders its absence — never silence.
  if (window.resolution === "hourly") {
    return (
      <section className="history-strip" aria-label="Lifecycle, health and command history">
        <h3>What the battery was doing</h3>
        <p className="history-strip-absence" role="note">
          {STRIP_HOURLY_ABSENCE_TEXT}.
        </p>
      </section>
    );
  }
  const commanded = commandedSegments(unit.commandedChanges, window.to);
  const lifecycle = wordSegments(unit.lifecycleChanges, window.to);
  const health = wordSegments(unit.healthStateChanges, window.to);
  // A unit that recorded samples but no health words: the recovery monitor
  // is not part of this deployment — the row says so, never silence.
  const healthAbsent = health.length === 0 && unit.sampleCount > 0;
  if (commanded.length === 0 && lifecycle.length === 0 && !healthAbsent) {
    return null;
  }
  const lifecycleRows = lifecycle.map((segment) => {
    const word = lifecycleWord(segment.v);
    return {
      from: segment.from,
      to: segment.to,
      bandLabels: lifecycleBandWords(segment.v),
      quietBand: false,
      title: word,
      clause: word,
    };
  });
  const healthRows = health.map((segment) => {
    const word = healthWord(segment.v);
    // The tooltip carries the tag's own sentence where the tag speaks
    // (a reasons-less health gets the generic line, never a fabricated
    // reason); the tag's silent states keep their short word.
    const sentence = (HEALTH_STATES as readonly string[]).includes(segment.v)
      ? unitHealthSentence({
          state: segment.v as HealthState,
          reasons: [],
          remediationHint: null,
        })
      : null;
    return {
      from: segment.from,
      to: segment.to,
      bandLabels: healthBandWords(segment.v),
      quietBand: false,
      title: sentence ?? word,
      clause: word,
    };
  });
  const commandedRows = commanded.map((segment) => {
    const clause = commandedSegmentText(segment);
    return {
      from: segment.from,
      to: segment.to,
      // The bar carries the SOURCE's short words; the clause is the tooltip's
      // and the listing's half (commandedBandLabels' own doctrine).
      bandLabels: commandedBandLabels(segment),
      // Quiet exactly where the clause itself says "nothing commanded" (the
      // same null rule the tiers key on — the bar may never argue with its
      // own listing item).
      quietBand: segment.source === null || segment.direction === null,
      title: clause,
      clause,
    };
  });
  const span = Math.max(1, window.to - window.from);
  return (
    <section className="history-strip" aria-label="Lifecycle, health and command history">
      <h3>What the battery was doing</h3>
      <p className="history-strip-explainer">{STRIP_EXPLAINER_TEXT}</p>
      <dl className="history-strip-rows">
        {lifecycleRows.length > 0 ? (
          <StripRow label="Lifecycle">
            <StripSegments segments={lifecycleRows} span={span} windowFrom={window.from} tone="lifecycle" multiDay={multiDay} />
          </StripRow>
        ) : null}
        {healthRows.length > 0 || healthAbsent ? (
          <StripRow label="Health">
            {healthAbsent ? (
              <p className="history-strip-absence">{HEALTH_NOT_RECORDED_TEXT}.</p>
            ) : (
              <StripSegments segments={healthRows} span={span} windowFrom={window.from} tone="health" multiDay={multiDay} />
            )}
          </StripRow>
        ) : null}
        {commandedRows.length > 0 ? (
          <StripRow label="Commanded">
            <StripSegments segments={commandedRows} span={span} windowFrom={window.from} tone="command" multiDay={multiDay} />
          </StripRow>
        ) : null}
      </dl>
    </section>
  );
}

/** One strip row's labeled pair (the dl's dt/dd with the row's name). */
function StripRow({ label, children }: { label: string; children: JSX.Element }): JSX.Element {
  return (
    <div className="history-strip-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * One strip row's two halves: the proportional bands (visual, labels fitted
 * to measured pixels, fuller sentence + instants on the tooltip) and the
 * change-point listing — the row's ACCESSIBLE surface, one item per segment
 * in the gap-notes rhythm, every segment's word and time announced to
 * assistive tech and NEVER inked on paper. The listing spent three rounds as
 * a visible line under the bar; the operator's verdict was the same each
 * time — text after the bars reads as raw duplicated data, and a night of
 * health flapping (60+ alternations) printed it as a wall. The bar is the
 * visual instrument now; the tooltip is the detail view; the listing is the
 * accessibility tree's copy, off the page for good.
 */
function StripSegments({
  segments,
  span,
  windowFrom,
  tone,
  multiDay,
}: {
  segments: readonly StripWordSegment[];
  span: number;
  /** The window's start instant — band geometry is RELATIVE to it (the
   * epoch-millisecond segments divided by the window's DURATION would land
   * every band at an absurd left%; the bands rendered nowhere, exactly the
   * empty-boxes defect the operator reported twice). */
  windowFrom: number;
  tone: "lifecycle" | "health" | "command";
  multiDay: boolean;
}): JSX.Element {
  const time = multiDay ? localDayTime : localTime;
  const name = tone === "lifecycle" ? "Lifecycle" : tone === "health" ? "Health" : "Commanded";
  const listings = segmentListingItems(segments, multiDay);
  return (
    <>
      <StripBands segments={segments} span={span} windowFrom={windowFrom} tone={tone} time={time} />
      <ul className="history-strip-listing history-strip-listing--offpage" aria-label={name}>
        {listings.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </>
  );
}

/**
 * One strip row's band half. The word each band carries is CHOSEN AGAINST
 * MEASURED PIXELS, the axis-tick doctrine: every candidate word is laid out
 * once in a hidden measurer carrying the band's own font rules, the row's
 * width is read at mount and held current by a ResizeObserver, and each band
 * shows the fullest tier that fits — a shorter honest word when only that
 * fits, and NO word when even the shortest cannot (a 12-minute spell in a
 * 16-hour window is a sliver; an ellipsis mid-word there reads as an error,
 * and the segment still lives in its tooltip and the offpage listing). Where
 * widths cannot be measured at all (a canvas-less test DOM whose boxes
 * report 0), the fullest tier renders — a word is suppressed only by a
 * measured refusal, never by ignorance (see `fittingBandLabel`).
 */
function StripBands({
  segments,
  span,
  windowFrom,
  tone,
  time,
}: {
  segments: readonly StripWordSegment[];
  span: number;
  windowFrom: number;
  tone: "lifecycle" | "health" | "command";
  time: (epochMs: number) => string;
}): JSX.Element {
  const bandsRef = useRef<HTMLUListElement | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);
  const [rowWidthPx, setRowWidthPx] = useState<number | null>(null);
  const [labelWidths, setLabelWidths] = useState<ReadonlyMap<string, number> | null>(null);

  // The row's own width: read once before first paint, then held current by
  // a ResizeObserver — a percentage-width band's pixels change whenever the
  // viewport or the window's layout moves under it.
  useLayoutEffect(() => {
    const element = bandsRef.current;
    if (element === null) {
      return;
    }
    const px = element.getBoundingClientRect().width;
    if (px > 0) {
      setRowWidthPx(px);
    }
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      const next = entry === undefined ? 0 : entry.contentRect.width;
      setRowWidthPx(next > 0 ? next : null);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);

  // The candidate words, deduped across the row's segments — the measurer's
  // contents and the effect's key, so a re-worded window re-measures.
  const candidates = useMemo(() => {
    const unique: string[] = [];
    const seen = new Set<string>();
    for (const segment of segments) {
      for (const word of segment.bandLabels) {
        if (!seen.has(word)) {
          seen.add(word);
          unique.push(word);
        }
      }
    }
    return unique;
  }, [segments]);

  // The words' rendered widths: each candidate lays out once inside the
  // hidden measurer (same font rules as a band label — history.css), and its
  // offsetWidth IS its width in the operator's own font — no per-platform
  // glyph guessing. A span that reports no width voids the whole map, which
  // falls the fit back to the fullest word rather than suppressing on
  // ignorance.
  useLayoutEffect(() => {
    const host = measureRef.current;
    if (host === null) {
      return;
    }
    const widths = new Map<string, number>();
    for (const span of host.querySelectorAll<HTMLSpanElement>("span")) {
      const width = span.offsetWidth;
      if (width <= 0) {
        setLabelWidths(null);
        return;
      }
      widths.set(span.textContent ?? "", width);
    }
    setLabelWidths(widths);
  }, [candidates]);

  // Every band's geometry and fitted label in ONE memo — the row's single
  // truth, rendered straight onto the bands (the listing no longer reads it;
  // it is off the page for good, carrying the same clauses to assistive tech).
  const fitted = useMemo(
    () =>
      segments.map((segment) => {
        // Window-relative percentages, clamped into the box: a change that
        // predates the window renders from 0, never a negative left.
        const rawLeft = ((segment.from - windowFrom) / span) * 100;
        const left = Math.max(0, rawLeft);
        const width = Math.max(
          0,
          ((segment.to - segment.from) / span) * 100 - (left - rawLeft),
        );
        const label = fittingBandLabel(
          segment.bandLabels,
          rowWidthPx === null ? null : (width / 100) * rowWidthPx,
          labelWidths,
        );
        return { left, width, label };
      }),
    [segments, span, windowFrom, rowWidthPx, labelWidths],
  );

  return (
    <>
      <ul ref={bandsRef} className={`history-bands history-bands--${tone}`} aria-hidden="true">
        {segments.map((segment, index) => {
          const entry = fitted[index]!;
          return (
            <li
              key={`${segment.from}-${index}`}
              className={segment.quietBand ? "history-band history-band--quiet" : "history-band"}
              style={{ left: `${entry.left}%`, width: `${entry.width}%` }}
              title={`${segment.title} · ${time(segment.from)}–${time(segment.to)}`}
            >
              {entry.label === "" ? null : (
                <span className="history-band-label">{entry.label}</span>
              )}
            </li>
          );
        })}
      </ul>
      <div ref={measureRef} className="history-band-measure" aria-hidden="true">
        {candidates.map((word) => (
          <span key={word}>{word}</span>
        ))}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// the chart specs (pure builders; the sections memoize them per window)
// ---------------------------------------------------------------------------

interface ChartSpecs {
  readonly built: BuiltChart;
  readonly styles: readonly ChartStyle[];
  readonly axes: readonly ChartAxis[];
  readonly summary: readonly { key: string; label: string; text: string }[];
}

/** The wire's series kind narrowed to the chart's resolution tier. */
function toSpec(
  key: string,
  label: string,
  series: HistorySeries,
  hourly: boolean,
): ChartSpec | null {
  if (hourly) {
    return series.kind === "hourly"
      ? { kind: "hourly", key, label, points: series.points }
      : null;
  }
  return series.kind === "samples" ? { kind: "line", key, label, points: series.points } : null;
}

/** One series' honest summary row: extremes with instants, sample count. */
function summaryRow(
  key: string,
  label: string,
  series: HistorySeries | undefined,
  unit: (value: number) => string,
): { key: string; label: string; text: string } {
  if (series === undefined) {
    return { key, label, text: "not available" };
  }
  const extremes = extremeText(series, unit);
  const band = series.kind === "hourly" ? " · hourly means with their own min–max bands" : "";
  const count = series.stats.sampleCount;
  if (series.points.length === 0) {
    return {
      key,
      label,
      text: count === 0 ? "not available in this window" : "no points survived this read",
    };
  }
  return {
    key,
    label,
    text: `${extremes === "" ? "" : `${extremes} · `}${count.toLocaleString()} sample${count === 1 ? "" : "s"}${band}`,
  };
}

const FLOW_FIELDS: readonly { field: string; label: string; color: string; fill?: string }[] = [
  {
    field: "battery_watts",
    label: "Battery (charge −, discharge +)",
    color: COLORS.battery,
    fill: BATTERY_FILL,
  },
  { field: "grid_power_w", label: "Grid (import −, export +)", color: COLORS.grid },
  { field: "load_power_w", label: "House load", color: COLORS.load },
];

/**
 * The power chart, shared by the fleet block and the per-battery block: the
 * flow lines (hourly adds each line's own min–max band edges), and — per
 * battery only — the COMMANDED OVERLAY: what we asked, stepped and dashed,
 * against the measured watts.
 */
function buildFlowChart(
  window: PlantHistoryWindow,
  series: Readonly<Record<string, HistorySeries>>,
  gaps: readonly HistoryGap[],
  unit: HistoryUnitBlock | null,
  withCommanded: boolean,
): ChartSpecs {
  const hourly = window.resolution === "hourly";
  const specs: ChartSpec[] = [];
  const summary: { key: string; label: string; text: string }[] = [];
  const styles: ChartStyle[] = [];

  for (const { field, label, color, fill } of FLOW_FIELDS) {
    const entry = series[field];
    const spec = entry === undefined ? null : toSpec(field, label, entry, hourly);
    if (spec !== null) {
      specs.push(spec);
    }
    summary.push(summaryRow(field, label, entry, formatWatts));
    styles.push({ key: field, color, ...(fill === undefined ? {} : { fill }) });
    if (hourly && spec !== null) {
      // The band edges: quiet, thin, paired by the renderer.
      styles.push({ key: `${field}#min`, color, width: 1, quiet: true });
      styles.push({ key: `${field}#max`, color, width: 1, quiet: true, bandTo: `${field}#min`, fill: BAND_FILL });
    }
  }

  if (withCommanded && unit !== null) {
    const commanded = commandedSegments(unit.commandedChanges, window.to);
    if (commanded.length > 0) {
      specs.push({
        kind: "step",
        key: "commanded_w",
        label: "Commanded (what we asked)",
        segments: commandedStepSegments(commanded, commandedWattsSigned),
      });
      styles.push({
        key: "commanded_w",
        color: COLORS.commanded,
        dash: [6, 4],
        stepped: true,
        width: 1.5,
      });
      // The overlay's summary row: the latest COMMANDED truth, with its end
      // named when the window ran on uncommanded after it.
      const lastCommanded = [...commanded].reverse().find((segment) => segment.source !== null);
      if (lastCommanded === undefined) {
        summary.push({
          key: "commanded_w",
          label: "Commanded",
          text: "nothing commanded in this window",
        });
      } else if (lastCommanded === commanded[commanded.length - 1]) {
        summary.push({
          key: "commanded_w",
          label: "Commanded",
          text: `latest: ${commandedSegmentText(lastCommanded)}`,
        });
      } else {
        summary.push({
          key: "commanded_w",
          label: "Commanded",
          text: `${commandedSegmentText(lastCommanded)} until ${localTime(lastCommanded.to)}; nothing commanded since`,
        });
      }
    }
  }

  return {
    built: buildChart(specs, gaps),
    styles,
    axes: [{ scale: "y", unit: "watts" }],
    summary,
  };
}

/** The charge-level chart: the BMS figure authoritative on a 0–100 axis. */
function buildSocChart(window: PlantHistoryWindow, unit: HistoryUnitBlock): ChartSpecs {
  const hourly = window.resolution === "hourly";
  const specs: ChartSpec[] = [];
  const summary: { key: string; label: string; text: string }[] = [];
  const styles: ChartStyle[] = [];

  const bms = unit.series.bms_soc_pct;
  const bmsSpec = bms === undefined ? null : toSpec("bms_soc_pct", "Charge level (BMS)", bms, hourly);
  if (bmsSpec !== null) {
    specs.push(bmsSpec);
  }
  summary.push(summaryRow("bms_soc_pct", "Charge level (BMS)", bms, formatPercent));
  styles.push({ key: "bms_soc_pct", color: COLORS.soc });
  if (hourly && bmsSpec !== null) {
    styles.push({ key: "bms_soc_pct#min", color: COLORS.soc, width: 1, quiet: true });
    styles.push({ key: "bms_soc_pct#max", color: COLORS.soc, width: 1, quiet: true, bandTo: "bms_soc_pct#min", fill: BAND_FILL });
  }

  // The advisory figure rides ONLY as a dashed overlay at full resolution
  // (its hourly rollup is not the authoritative story; its cold-ring
  // staleness is documented doctrine, never a degradation mark).
  const advisory = unit.series.soc_pct;
  if (!hourly && advisory !== undefined && advisory.kind === "samples" && advisory.points.length > 0) {
    specs.push({ kind: "line", key: "soc_pct", label: "Advisory (cold ring)", points: advisory.points });
    styles.push({ key: "soc_pct", color: COLORS.load, dash: [4, 4], width: 1 });
  }

  return {
    built: buildChart(specs, unit.gaps),
    styles,
    axes: [{ scale: "y", unit: "percent", domain: [0, 100] }],
    summary,
  };
}

/** The temperature chart: the min–max band with both edges drawn. */
function buildTemperatureChart(window: PlantHistoryWindow, unit: HistoryUnitBlock): ChartSpecs {
  const hourly = window.resolution === "hourly";
  const specs: ChartSpec[] = [];
  const summary: { key: string; label: string; text: string }[] = [];
  const styles: ChartStyle[] = [];

  for (const [field, label] of [
    ["temperature_min_c", "Coolest"],
    ["temperature_max_c", "Warmest"],
  ] as const) {
    const entry = unit.series[field];
    const spec = entry === undefined ? null : toSpec(field, label, entry, hourly);
    if (spec !== null) {
      specs.push(spec);
    }
    summary.push(summaryRow(field, label, entry, formatTemp));
    styles.push({
      key: field,
      color: COLORS.grid,
      width: 1.25,
      ...(field === "temperature_max_c" ? { bandTo: "temperature_min_c", fill: BAND_FILL } : {}),
    });
  }

  return {
    built: buildChart(specs, unit.gaps),
    styles,
    axes: [{ scale: "y", unit: "celsius" }],
    summary,
  };
}

/** The cells chart: min/max cell voltage on one axis, spread on its own. */
function buildCellsChart(window: PlantHistoryWindow, unit: HistoryUnitBlock): ChartSpecs {
  const hourly = window.resolution === "hourly";
  const specs: ChartSpec[] = [];
  const summary: { key: string; label: string; text: string }[] = [];
  const styles: ChartStyle[] = [];

  for (const [field, label] of [
    ["cell_min_v", "Lowest cell"],
    ["cell_max_v", "Highest cell"],
  ] as const) {
    const entry = unit.series[field];
    const spec = entry === undefined ? null : toSpec(field, label, entry, hourly);
    if (spec !== null) {
      specs.push(spec);
    }
    summary.push(summaryRow(field, label, entry, formatVolts));
    styles.push({ key: field, color: field === "cell_min_v" ? COLORS.grid : COLORS.soc, width: 1.25 });
  }

  const spread = unit.series.cell_spread_mv;
  const spreadSpec = spread === undefined ? null : toSpec("cell_spread_mv", "Spread", spread, hourly);
  if (spreadSpec !== null) {
    specs.push(spreadSpec);
  }
  summary.push(summaryRow("cell_spread_mv", "Spread", spread, formatMillivolts));
  styles.push({ key: "cell_spread_mv", color: COLORS.spread, width: 1.25, scale: "spread" });

  return {
    built: buildChart(specs, unit.gaps),
    styles,
    axes: [
      { scale: "y", unit: "volts" },
      { scale: "spread", unit: "millivolts" },
    ],
    summary,
  };
}
