/**
 * The Insights view's solar section — "Solar outlook & forecast accuracy"
 * (the provider round's advisory read, `GET /api/v1/forecast`). Two halves,
 * one honesty doctrine (ARCHITECTURE section 17: forecasts are promises, the
 * historian is the truth):
 *
 * - THE OUTLOOK: the composed provider's next-48-hours PV curve — the central
 *   figure held stair-wise across the provider's own half-open slots, the
 *   [q10, q90] band drawn ONLY where the source actually claims deciles (a
 *   deterministic source words its bandlessness instead of gaining one), the
 *   source named, and the fetched-at age ticking with the provider's own
 *   staleness verdict colored — never re-derived, never hidden.
 * - THE SCOREBOARD: the cross-check scorer's figures against the surplus the
 *   plant ACTUALLY exported. The latest fetch's elapsed-window check, then
 *   the accumulation — per fetch, in this controller run's memory — with the
 *   bias worded in the operator's direction (an OPTIMISTIC forecast
 *   over-promises and under-charges overnight when trusted), the coverage
 *   note, and "evidence accumulating since <date>" honesty at small n: no
 *   verdict theater at one fetch.
 *
 * Absent data renders absent: an uncomposed `forecast_providers` block is the
 * honest not-commissioned note; a missing family or a failed first fetch is
 * the notes' own words; an empty scoreboard says why IT is empty. Nothing
 * here is a measurement, and the section says so once, up top.
 */
import { useCallback, useEffect, useMemo, useState, type JSX } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  BASIS_DISCLOSURE_TEXT,
  basisInputsText,
  biasText,
  coverageText,
  evidenceText,
  fetchedAgeText,
  horizonText,
  NOT_DURABLE_TEXT,
  scoreEmptyText,
  sourceText,
  toForecastOutlook,
  type ForecastOutlookView,
  type PvOutlook,
} from "../../app/forecast";
import { localDayTime } from "../../app/history";
import { formatWatts } from "../../lib/format";
import { buildChart, type ChartSpec } from "../history/chartData";
import { TimeSeriesChart } from "../history/TimeSeriesChart";
import type { ChartAxis, ChartStyle } from "../history/TimeSeriesChart";

/** The live re-read cadence: the same 30 s rhythm the History view polls on. */
const REFRESH_MS = 30_000;
/** The age ticker's cadence (the fetched-at line's "N s ago"). */
const TICK_MS = 1000;

const NOT_COMMISSIONED_CODE = "forecast_providers_not_commissioned";

// The palette names the console's own tokens (styles.css :root); canvas
// strokes cannot read var() directly, so the hex values ARE the tokens.
const SOLAR = "#8a6d1a"; // --armed: the solar family's gold
const BAND_FILL = "rgba(138,109,26,0.10)";

type Phase = "loading" | "ready" | "not-commissioned" | "error";

interface ErrorView {
  code: string;
  message: string;
  requestId: string;
}

function toErrorView(error: unknown): ErrorView {
  if (error instanceof ApiClientError) {
    return { code: error.code, message: error.message, requestId: error.request_id };
  }
  return {
    code: "forecast_unavailable",
    message: error instanceof Error ? error.message : "The solar outlook could not be loaded.",
    requestId: "",
  };
}

export interface SolarOutlookSectionProps {
  client: ApiClient;
}

export function SolarOutlookSection({ client }: SolarOutlookSectionProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [outlook, setOutlook] = useState<ForecastOutlookView | null>(null);
  const [error, setError] = useState<ErrorView | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const refresh = useCallback((): void => {
    setReloadNonce((nonce) => nonce + 1);
  }, []);

  // The load: one forecast read. Only the client's identity or a refresh
  // re-runs it; a failed refresh while an outlook is on screen keeps the last
  // picture with its refusal riding above (the History view's own pattern).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const body = await client.getForecast();
        if (cancelled) {
          return;
        }
        const parsed = toForecastOutlook(body);
        if (parsed === null) {
          setError({
            code: "unreadable_forecast",
            message: "The solar outlook response could not be read.",
            requestId: "",
          });
          setPhase("error");
          return;
        }
        setOutlook(parsed);
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
  }, [client, reloadNonce]);

  // The live re-read: while the tab is visible, the outlook refreshes on the
  // house cadence (a hidden tab does not poll — its timers are throttled
  // anyway; the provider's own cache gate means this never burns quota).
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

  // The age ticker: one render a second while the section is mounted, feeding
  // only the fetched-at line — the data's age ticks, the data itself does not.
  useEffect(() => {
    const timer = setInterval(() => {
      setNowMs(Date.now());
    }, TICK_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);

  const headingId = "solar-outlook-heading";

  if (phase === "loading") {
    return (
      <section className="insights-solar" aria-labelledby={headingId}>
        <h3 id={headingId}>Solar outlook &amp; forecast accuracy</h3>
        <p role="status" className="insights-solar-loading">
          Loading the solar outlook…
        </p>
      </section>
    );
  }

  if (phase === "not-commissioned") {
    return (
      <section className="insights-solar" aria-labelledby={headingId}>
        <h3 id={headingId}>Solar outlook &amp; forecast accuracy</h3>
        <div role="note" className="insights-solar-not-commissioned">
          <h4>The forecast providers are not commissioned</h4>
          <p>
            No solar outlook or forecast-accuracy evidence is being served — the advisory forecast
            providers are not commissioned in this deployment&apos;s config. Commissioning is a
            config change on the controller: add the <code>forecast_providers</code> block (with
            at least one provider family, e.g. <code>open_meteo</code>) and restart, and this
            section appears. Forecasts are advisory only — no control path reads them.
          </p>
          <p className="insights-solar-error-code">
            Code: <code>{error?.code ?? NOT_COMMISSIONED_CODE}</code>
          </p>
          {error !== null && <p className="insights-solar-error-message">{error.message}</p>}
          <button type="button" className="insights-solar-retry" onClick={refresh}>
            Check again
          </button>
        </div>
      </section>
    );
  }

  if (phase === "error" && outlook === null) {
    return (
      <section className="insights-solar" aria-labelledby={headingId}>
        <h3 id={headingId}>Solar outlook &amp; forecast accuracy</h3>
        <div role="alert" className="insights-solar-error">
          <p className="insights-solar-error-title">We couldn&apos;t load the solar outlook.</p>
          <p className="insights-solar-error-code">
            Code: <code>{error?.code ?? "unknown"}</code>
          </p>
          {error !== null && <p className="insights-solar-error-message">{error.message}</p>}
          {error !== null && error.requestId !== "" && (
            <p className="insights-solar-error-request">
              Request ID: <code>{error.requestId}</code>
            </p>
          )}
          <button type="button" className="insights-solar-retry" onClick={refresh}>
            Try again
          </button>
        </div>
      </section>
    );
  }

  if (outlook === null) {
    return (
      <section className="insights-solar" aria-labelledby={headingId}>
        <h3 id={headingId}>Solar outlook &amp; forecast accuracy</h3>
        <p role="status" className="insights-solar-loading">
          Loading the solar outlook…
        </p>
      </section>
    );
  }

  return (
    <section className="insights-solar" aria-labelledby={headingId}>
      <h3 id={headingId}>Solar outlook &amp; forecast accuracy</h3>
      <p className="insights-solar-intro">
        What the forecast providers promise ahead — and how those promises measure against the
        surplus the plant actually exported. Advisory only: no control path reads these figures;
        the recorded truth stays the historian&apos;s own.
      </p>
      <div className="insights-solar-refresh-row">
        <button type="button" className="insights-solar-retry" onClick={refresh}>
          Refresh
        </button>
      </div>

      {/* A refresh that failed while an outlook was already on screen: the
          last-known picture stays and the refusal rides above it. */}
      {error !== null && phase === "error" ? (
        <div role="alert" className="insights-solar-error insights-solar-error--refresh">
          <p className="insights-solar-error-title">
            The latest outlook refresh failed — showing the last loaded one.
          </p>
          <p className="insights-solar-error-code">
            Code: <code>{error.code}</code>
          </p>
          <button type="button" className="insights-solar-retry" onClick={refresh}>
            Try again
          </button>
        </div>
      ) : null}

      <OutlookBlock outlook={outlook} nowMs={nowMs} />
      <ScoreboardBlock outlook={outlook} />
    </section>
  );
}

// ---------------------------------------------------------------------------
// the outlook half
// ---------------------------------------------------------------------------

function OutlookBlock({
  outlook,
  nowMs,
}: {
  outlook: ForecastOutlookView;
  nowMs: number;
}): JSX.Element {
  const pv = outlook.pv;
  const staleness = outlook.provider;
  if (pv === null || pv.intervals.length === 0) {
    return (
      <div className="insights-solar-outlook insights-solar-outlook--empty">
        <h4>Solar outlook</h4>
        <p className="insights-solar-absent">No PV forecast is available right now.</p>
        {staleness !== null && staleness.lastError !== null && (
          <p className="insights-solar-provider-error">
            The provider&apos;s last attempt failed: {staleness.lastError}
          </p>
        )}
        {outlook.notes.length > 0 && (
          <ul className="insights-solar-notes">
            {outlook.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return (
    <div className="insights-solar-outlook">
      <h4>Solar outlook</h4>
      <p className="insights-solar-facts">
        <b className="insights-solar-source">{sourceText(pv.source)}</b>
        {staleness !== null ? (
          <>
            {" — "}
            <span
              className={
                staleness.stale
                  ? "insights-solar-fetched insights-solar-fetched--stale"
                  : "insights-solar-fetched"
              }
              data-stale={staleness.stale ? "true" : "false"}
            >
              {fetchedAgeText(staleness, nowMs)}
              {staleness.stale ? " — stale by the provider's own threshold" : ""}
            </span>
          </>
        ) : null}
        {" · "}
        {horizonText(pv)}
        {staleness !== null && staleness.errorCount > 0
          ? ` · ${staleness.errorCount} fetch ${
              staleness.errorCount === 1 ? "failure" : "failures"
            } so far`
          : ""}
      </p>
      <ForecastChart pv={pv} />
      {outlook.notes.length > 0 && (
        <ul className="insights-solar-notes">
          {outlook.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The forward curve: the central stair plus the claimed band, never more. */
function ForecastChart({ pv }: { pv: PvOutlook }): JSX.Element {
  const built = useMemo(
    () => buildChart(outlookSpecs(pv)),
    [pv],
  );
  const styles = useMemo(() => outlookStyles(pv), [pv]);
  const from = pv.horizonFrom ?? pv.intervals[0]!.start;
  const to = pv.horizonTo ?? pv.intervals[pv.intervals.length - 1]!.end;
  return (
    <TimeSeriesChart
      label="Forecast PV power over the horizon"
      from={from}
      to={to}
      built={built}
      styles={styles}
      axes={outlookAxes()}
      height={200}
      multiDay={true}
      summary={outlookSummary(pv)}
      summaryLabel="Forecast PV power over the horizon"
      gapNotes={[]}
      className="insights-solar-chart"
    />
  );
}

/** The chart's specs: the central stair, plus the band edges when claimed. */
function outlookSpecs(pv: PvOutlook): ChartSpec[] {
  const specs: ChartSpec[] = [
    {
      kind: "step",
      key: "pv",
      label: "Forecast PV",
      segments: pv.intervals.map((interval) => ({
        from: interval.start,
        to: interval.end,
        value: interval.w,
      })),
    },
  ];
  if (pv.intervals.some((interval) => interval.q10 !== null && interval.q90 !== null)) {
    specs.push({
      kind: "line",
      key: "pv#low",
      label: "Band low (q10)",
      points: pv.intervals
        .filter((interval) => interval.q10 !== null)
        .map((interval) => ({ t: interval.start, v: interval.q10! })),
    });
    specs.push({
      kind: "line",
      key: "pv#high",
      label: "Band high (q90)",
      points: pv.intervals
        .filter((interval) => interval.q90 !== null)
        .map((interval) => ({ t: interval.start, v: interval.q90! })),
    });
  }
  return specs;
}

function outlookStyles(pv: PvOutlook): ChartStyle[] {
  const styles: ChartStyle[] = [
    { key: "pv", color: SOLAR, width: 2, stepped: true, fill: BAND_FILL },
  ];
  if (pv.intervals.some((interval) => interval.q10 !== null && interval.q90 !== null)) {
    styles.push({ key: "pv#low", color: SOLAR, width: 1, quiet: true, stepped: true });
    styles.push({
      key: "pv#high",
      color: SOLAR,
      width: 1,
      quiet: true,
      stepped: true,
      bandTo: "pv#low",
      fill: BAND_FILL,
    });
  }
  return styles;
}

function outlookAxes(): ChartAxis[] {
  return [{ scale: "y", unit: "watts" }];
}

/** The chart's text alternative: the peak, the horizon, the band's existence. */
function outlookSummary(pv: PvOutlook): { key: string; label: string; text: string }[] {
  let peak = Number.NEGATIVE_INFINITY;
  let peakAt: number | null = null;
  for (const interval of pv.intervals) {
    if (interval.w > peak) {
      peak = interval.w;
      peakAt = interval.start;
    }
  }
  const banded = pv.intervals.some((interval) => interval.q10 !== null && interval.q90 !== null);
  const slots = pv.intervals.length === 1 ? "1 slot" : `${pv.intervals.length} slots`;
  return [
    {
      key: "pv",
      label: "Forecast PV",
      text:
        peakAt === null
          ? "not available in this read"
          : `peak ${formatWatts(peak)} at ${localDayTime(peakAt)} · ${slots} · ${horizonText(pv)}`,
    },
    {
      key: "pv-band",
      label: "Uncertainty band",
      text: banded
        ? "a q10–q90 band claimed by the source on every slot"
        : "none claimed — a deterministic source, so no band is drawn",
    },
  ];
}

// ---------------------------------------------------------------------------
// the scoreboard half
// ---------------------------------------------------------------------------

function ScoreboardBlock({ outlook }: { outlook: ForecastOutlookView }): JSX.Element {
  const score = outlook.score;
  const board = outlook.scoreboard;
  return (
    <div className="insights-solar-scoreboard">
      <h4>Forecast accuracy — promise vs recorded surplus</h4>
      <p className="insights-solar-score">
        {score === null ? (
          <>
            <b>Latest fetch:</b> {scoreEmptyText(outlook)}
          </>
        ) : (
          <>
            <b>Latest fetch:</b> {score.samples} recorded{" "}
            {score.samples === 1 ? "sample" : "samples"} align with its elapsed{" "}
            {localDayTime(score.windowFrom ?? outlook.asOf)}–
            {localDayTime(score.windowTo ?? outlook.asOf)} — against recorded export,{" "}
            <span className="insights-solar-bias">{biasText(score.biasW)}</span>; mean absolute
            error {formatWatts(score.maeW)}. {coverageText(score.insideBand)}.
          </>
        )}
      </p>
      {/* The corrected basis's raw inputs (amendment A1): evidence for the
          night-v2 rebuild, rendered as INPUTS — never a second verdict. */}
      {score !== null && score.basis !== null && (
        <p className="insights-solar-basis-inputs">{basisInputsText(score.basis)}</p>
      )}
      {score !== null && <p className="insights-solar-basis">{BASIS_DISCLOSURE_TEXT}</p>}
      {board === null ? (
        <p className="insights-solar-board-note">
          {outlook.historyComposed
            ? "No accuracy evidence has accumulated yet — it starts with the first fetch that outlives its own refresh interval."
            : scoreEmptyText(outlook)}
        </p>
      ) : (
        <div className="insights-solar-board">
          <p className="insights-solar-board-main">
            <b>{sourceText(board.source)}</b> (against recorded export):{" "}
            {board.meanBiasW === null ? "no mean bias yet" : biasText(board.meanBiasW)}
            {board.meanMaeW === null ? "" : `; mean absolute error ${formatWatts(board.meanMaeW)}`}
            {" · "}
            {coverageText(board.meanInsideBand)}
            {" · "}
            {board.totalSamples.toLocaleString()} recorded{" "}
            {board.totalSamples === 1 ? "sample" : "samples"}.
          </p>
          <p className="insights-solar-board-evidence">{evidenceText(board)}</p>
          {!board.durable && <p className="insights-solar-board-durable">{NOT_DURABLE_TEXT}</p>}
        </div>
      )}
      <p className="insights-solar-night-note">
        The night-charge plan is the consumer this evidence is for: an optimistic outlook
        under-charges the pods overnight, and the scoreboard is how that promise gets audited.
      </p>
    </div>
  );
}
