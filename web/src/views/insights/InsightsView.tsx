/**
 * Insights view — the energy scorecard's daily ledger
 * (DESIGN_ENERGY_SCORECARD.md §8 W-B): the standing "Insights — not available
 * yet" nav placeholder became this view; its content is the day-by-day energy
 * account from `GET /api/v1/energy/days`.
 *
 * PENDING-BACKEND: the route is not live yet — this view is built
 * feature-detectively against the contract's pinned shapes. The 200 IS the
 * feature detection for the ledger; a 409 `energy_scorecard_not_commissioned`
 * (the schedules precedent verbatim) renders the honest not-commissioned
 * state, because the nav link is always offered (the pinned Schedule-view
 * deviation in app/views.ts).
 *
 * Honesty rules pinned here:
 *
 * - One row per COMPLETED day, in the route's own newest-last order; the
 *   `energy.day_rolled` TRANSITION (never a heartbeat) appends the newest
 *   completed day to an already-loaded ledger, deduped by date — the live
 *   day's figures live on Home's Today card, never here.
 * - Every figure renders from ITS day record's own sources; each row carries
 *   the grid provenance badge ("measured by the controller" while the source
 *   is the CT integration — the pods' grid counters are not the bought/sold
 *   source until their roles are pinned).
 * - The A/B pair stays neutral everywhere it appears: the per-row
 *   cross-check discloses the two counter deltas as "counter A / counter B"
 *   with the record's own consistency verdict — evidence, never a display
 *   source — and the route's `grid_counter_roles` answer carries the one
 *   roles note at the top.
 * - Null figures render "not available" — never 0; a partial day names the
 *   threshold breach with its coverage fraction.
 * - SOLAR IS NEVER A MEASUREMENT: no solar-production column exists; the
 *   footnote says exactly why, once.
 * - Paging is last-N via the route's own `limit` (N ∈ 1..31, page step 8):
 *   "Load more" widens the window; when the window is at the bound or the
 *   route returned fewer days than asked, there is nothing older to load and
 *   the control says so instead of hiding the reason.
 */
import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  ENERGY_DAYS_DEFAULT_LIMIT,
  ENERGY_DAYS_MAX_LIMIT,
  SOLAR_FOOTNOTE,
  counterRolesNote,
  coverageText,
  dayMarkerText,
  kwhText,
  metricFlagText,
  sourceProvenanceText,
  toEnergyDayRolledEvent,
  toEnergyDaysView,
  type EnergyDayRecord,
} from "../../app/energy";
import "./insights.css";

/** How many more days each "Load more" widens the window by. */
const PAGE_STEP = 8;
/** Reconnect pause for the event stream: seen retrying within a glance. */
const RECONNECT_DELAY_MS = 300;

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
    code: "insights_unavailable",
    message: error instanceof Error ? error.message : "The energy ledger could not be loaded.",
    requestId: "",
  };
}

/** Append a rolled day, deduped by date, keeping the newest-last order. */
function appendRolledDay(days: EnergyDayRecord[], rolled: EnergyDayRecord): EnergyDayRecord[] {
  if (rolled.date !== "" && days.some((day) => day.date === rolled.date)) {
    return days;
  }
  return [...days, rolled];
}

/** One grid figure pair in one plain clause; nulls named, never 0. */
function gridFiguresText(record: EnergyDayRecord): string {
  return `Bought ${kwhText(record.fleet.gridImportKwh)} · Sold ${kwhText(
    record.fleet.gridExportKwh,
  )}`;
}

export interface InsightsViewProps {
  client: ApiClient;
}

export function InsightsView({ client }: InsightsViewProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [days, setDays] = useState<EnergyDayRecord[]>([]);
  const [roles, setRoles] = useState<"unpinned" | "vendor_labels" | "swapped">("unpinned");
  const [limit, setLimit] = useState(ENERGY_DAYS_DEFAULT_LIMIT);
  const [error, setError] = useState<ErrorView | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<ErrorView | null>(null);
  /** No more history exists beyond the loaded window (route-answered). */
  const [exhausted, setExhausted] = useState(false);
  /** One rollover announcement; repeated frames never repeat it. */
  const [rollNotice, setRollNotice] = useState<string | null>(null);
  /**
   * The current window by reference: the mount read and the stream's resync
   * re-read use it without re-subscribing when "Load more" widens it.
   */
  const limitRef = useRef(limit);
  limitRef.current = limit;

  const applyPage = useCallback((body: unknown, askedLimit: number): boolean => {
    const view = toEnergyDaysView(body);
    if (view === null) {
      return false;
    }
    setDays(view.days);
    setRoles(view.gridCounterRoles);
    // The route answered fewer days than the window asked for: there is
    // nothing older to load — the bound alone never decides this.
    setExhausted(view.days.length < askedLimit);
    return true;
  }, []);

  const readPage = useCallback(
    async (askedLimit: number): Promise<boolean> => {
      try {
        const body = await client.getEnergyDays(askedLimit);
        return applyPage(body, askedLimit);
      } catch (failure) {
        const view = toErrorView(failure);
        setError(view);
        setPhase(view.code === "energy_scorecard_not_commissioned" ? "not-commissioned" : "error");
        return false;
      }
    },
    [applyPage, client],
  );

  // The first load. Only the client's identity re-runs it; retries go through
  // `retry`, and "Load more" applies its own wider page without a phase flash.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const ok = await readPage(limitRef.current);
      if (!cancelled && ok) {
        setPhase("ready");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [readPage]);

  const retry = useCallback((): void => {
    setError(null);
    setMoreError(null);
    setPhase("loading");
    void readPage(limitRef.current).then((ok) => {
      if (ok) {
        setPhase("ready");
      }
    });
  }, [readPage]);

  const loadMore = useCallback(async (): Promise<void> => {
    if (loadingMore || limit >= ENERGY_DAYS_MAX_LIMIT || exhausted) {
      return;
    }
    const nextLimit = Math.min(limit + PAGE_STEP, ENERGY_DAYS_MAX_LIMIT);
    setLoadingMore(true);
    setMoreError(null);
    try {
      const body = await client.getEnergyDays(nextLimit);
      if (!applyPage(body, nextLimit)) {
        setMoreError({
          code: "unreadable_energy_days",
          message: "The energy ledger response could not be read.",
          requestId: "",
        });
      } else {
        setLimit(nextLimit);
      }
    } catch (failure) {
      setMoreError(toErrorView(failure));
    } finally {
      setLoadingMore(false);
    }
  }, [applyPage, client, exhausted, limit, loadingMore]);

  // The live bus: a rollover appends the completed day to the loaded ledger
  // (§8 W-B). Without this subscription the ledger is a point-in-time page —
  // a console left open overnight would never show the day that just rolled.
  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      while (!cancelled) {
        let resync = false;
        try {
          for await (const frame of client.openEvents()) {
            if (cancelled) {
              return;
            }
            if (frame.type === "resync_required") {
              resync = true;
              break;
            }
            if (frame.type === "energy.day_rolled") {
              const rolled = toEnergyDayRolledEvent(frame.payload);
              if (rolled !== null) {
                setDays((previous) => appendRolledDay(previous, rolled));
                setRollNotice(
                  rolled.date === ""
                    ? "A new day rolled over — its figures are in the ledger below."
                    : `${rolled.date} was added to the ledger below.`,
                );
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
          // A discontinuity means the ledger may be out of step; one quiet
          // re-read replaces it wholesale (the route is the truth here).
          try {
            const body = await client.getEnergyDays(limitRef.current);
            applyPage(body, limitRef.current);
          } catch {
            // The manual retry remains; the loaded days stay.
          }
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
  }, [client, applyPage]);

  if (phase === "loading") {
    return (
      <section className="insights-view" aria-labelledby="insights-heading">
        <h2 id="insights-heading">Insights</h2>
        <p role="status" className="insights-loading">
          Loading the energy ledger…
        </p>
      </section>
    );
  }

  if (phase === "not-commissioned") {
    return (
      <section className="insights-view" aria-labelledby="insights-heading">
        <h2 id="insights-heading">Insights</h2>
        <div role="note" className="insights-not-commissioned">
          <h3>The energy scorecard is not commissioned</h3>
          <p>
            The daily energy ledger is not commissioned in this deployment&apos;s config — there is
            nothing to show here yet. Commissioning is a config change on the controller: add the{" "}
            <code>energy_scorecard</code> block and restart, and the Today card (Home) and this
            ledger appear; each day rolls at site-timezone midnight.
          </p>
          <p className="insights-error-code">
            Code: <code>{error?.code ?? "energy_scorecard_not_commissioned"}</code>
          </p>
          {error !== null && <p className="insights-error-message">{error.message}</p>}
          <button type="button" className="insights-retry" onClick={retry}>
            Check again
          </button>
        </div>
      </section>
    );
  }

  if (phase === "error") {
    return (
      <section className="insights-view" aria-labelledby="insights-heading">
        <h2 id="insights-heading">Insights</h2>
        <div role="alert" className="insights-error">
          <p className="insights-error-title">We couldn&apos;t load the energy ledger.</p>
          <p className="insights-error-code">
            Code: <code>{error?.code ?? "unknown"}</code>
          </p>
          {error !== null && <p className="insights-error-message">{error.message}</p>}
          {error !== null && error.requestId !== "" && (
            <p className="insights-error-request">
              Request ID: <code>{error.requestId}</code>
            </p>
          )}
          <button type="button" className="insights-retry" onClick={retry}>
            Try again
          </button>
        </div>
      </section>
    );
  }

  const atBound = limit >= ENERGY_DAYS_MAX_LIMIT;
  const nothingOlder = exhausted || atBound;

  return (
    <section className="insights-view" aria-labelledby="insights-heading">
      <h2 id="insights-heading">Insights</h2>
      <p className="insights-intro">
        One row per completed day — the energy account the pods and the controller measured, oldest
        first. Today&apos;s figures (so far) live on the Home view.
      </p>
      <p className="insights-roles" role="note">
        {counterRolesNote(roles)}
      </p>
      {rollNotice !== null && (
        <p role="status" className="insights-roll-notice">
          {rollNotice}
        </p>
      )}
      {days.length === 0 ? (
        <p className="insights-empty">
          No days recorded yet — the first row appears after the next midnight rollover.
        </p>
      ) : (
        <ol className="insights-ledger" aria-label="Daily energy ledger">
          {days.map((day, index) => (
            <InsightsDayRow
              key={day.date === "" ? `untitled-${index}` : day.date}
              day={day}
            />
          ))}
        </ol>
      )}
      <div className="insights-more-row">
        {nothingOlder ? (
          days.length > 0 && (
            <p className="insights-more-note">
              {atBound
                ? `Showing the most recent ${days.length} day${days.length === 1 ? "" : "s"} — the ledger keeps at most ${ENERGY_DAYS_MAX_LIMIT} days per read.`
                : "No older days are recorded."}
            </p>
          )
        ) : (
          <button
            type="button"
            className="insights-load-more"
            onClick={() => void loadMore()}
            disabled={loadingMore}
          >
            Load more days
          </button>
        )}
      </div>
      {moreError !== null && (
        <div role="alert" className="insights-error insights-error--more">
          <p className="insights-error-title">We couldn&apos;t load more days.</p>
          <p className="insights-error-code">
            Code: <code>{moreError.code}</code>
          </p>
          <p className="insights-error-message">{moreError.message}</p>
        </div>
      )}
      <p className="insights-footnote">{SOLAR_FOOTNOTE}</p>
    </section>
  );
}

/** One completed day: the date, the figures, the coverage, the provenance. */
function InsightsDayRow({ day }: { day: EnergyDayRecord }): JSX.Element {
  const crossCheck = day.counterCrossCheck;
  const unitIds = Object.keys(day.units);
  return (
    <li className="insights-day" aria-label={`${day.date === "" ? "an undated day" : day.date} energy`}>
      <div className="insights-day-head">
        <time className="insights-day-date" dateTime={day.date === "" ? undefined : day.date}>
          {day.date === "" ? "date not available" : day.date}
        </time>
        <span
          className="insights-kind"
          data-kind={day.kind}
        >
          {dayMarkerText(day.kind, null)}
        </span>
        <span className="insights-provenance" title={`Grid figures ${sourceProvenanceText(day.sources.grid)}`}>
          {sourceProvenanceText(day.sources.grid)}
        </span>
      </div>
      <p className="insights-grid">{gridFiguresText(day)}</p>
      <p className="insights-batteries">
        Charged {kwhText(day.fleet.batteryChargedKwh)} · Discharged{" "}
        {kwhText(day.fleet.batteryDischargedKwh)} · House load {kwhText(day.fleet.loadKwh)} ·
        Charged from surplus {kwhText(day.fleet.chargedFromSurplusKwh)}
      </p>
      {unitIds.length > 0 && (
        <ul className="insights-units" aria-label="Per-battery figures">
          {unitIds.map((unitId) => {
            const unit = day.units[unitId]!;
            const surplus =
              unit.chargedFromSurplusKwh === null
                ? ""
                : ` · surplus ${kwhText(unit.chargedFromSurplusKwh)}`;
            const resets = unit.metricFlags.length === 0
              ? ""
              : ` (${unit.metricFlags.map(metricFlagText).join("; ")})`;
            return (
              <li key={unitId}>
                {unitId}: charged {kwhText(unit.batteryChargedKwh)}, discharged{" "}
                {kwhText(unit.batteryDischargedKwh)}
                {surplus}
                {resets}
              </li>
            );
          })}
        </ul>
      )}
      <p className="insights-coverage">
        {day.fleet.coveragePct === null
          ? "Coverage not available"
          : `${coverageText(day.fleet.coveragePct)} coverage`}
      </p>
      {crossCheck !== null && (
        <details className="insights-crosscheck">
          <summary>Grid counter cross-check (evidence)</summary>
          <p>
            Counter A {kwhText(crossCheck.gridADeltaKwh)} · Counter B{" "}
            {kwhText(crossCheck.gridBDeltaKwh)}
            {crossCheck.consistentWith === ""
              ? ""
              : ` — consistent with ${crossCheck.consistentWith}`}
            {crossCheck.discriminating === null
              ? ""
              : crossCheck.discriminating
                ? " (a discriminating day)"
                : " (not discriminating)"}
            . Recorded as cross-check evidence only; the figures above never come from these
            counters while their roles are unpinned.
          </p>
        </details>
      )}
    </li>
  );
}
