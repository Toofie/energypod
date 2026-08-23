/**
 * Objectives view — the night-writer detector's evidence table
 * (API_CONTRACTS.md "Night-writer detector (foreign-objective observation)",
 * the read surface `GET /api/v1/objectives/observed`): the per-unit session
 * record of what commanded the batteries while we commanded nothing.
 *
 * PENDING-BACKEND: the route is not live yet — this view is built
 * feature-detectively against the contract's pinned response shape. Once the
 * detector composes it is ALWAYS on (no config block exists), so this view
 * has no not-commissioned state: the honest states are loading, the evidence
 * table, an empty window (no recorded samples), and the error envelope with a
 * retry (an older backend without the route answers 404 — rendered verbatim,
 * never swallowed).
 *
 * Honesty rules pinned here:
 *
 * - This is an EVIDENCE table, not an alarm surface: no badges, no banners,
 *   no per-row severity — the words carry the classification, and the one
 *   pinned honesty note (the signature's limit) sits at the top, always.
 * - Every figure renders from the endpoint's own rollup: the typical figure
 *   is the backend's LOWER median (never re-derived here), the sample counts
 *   are NONZERO samples only (zeros are not samples), and the sign split is
 *   the backend's own charge/discharge counts. Null figures read "not
 *   available" — never 0, never fabricated.
 * - The window is the caller's own ask (`last`, Nh/Nd within the route's
 *   1 h..168 h range); switching it re-reads, and the answer's own `as_of`
 *   and echoed `last` are shown with the data, never a local clock.
 * - `foreign_active` renders the detector's standing assertion with its
 *   reason in plain words; a foreign EPISODE COUNT is history, not a
 *   current state, and is rendered as the count it is.
 * - The mode words and our lifecycle/claim state at each sample stay
 *   server-side (the contract's response shape carries the rollup only) —
 *   nothing here invents them.
 */
import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import {
  OBJECTIVE_CLASSIFICATIONS,
  OBJECTIVE_SIGNATURE_HONESTY_NOTE,
  OBJECTIVE_WINDOW_OPTIONS,
  classificationCountText,
  objectiveClassificationText,
  objectiveReasonText,
  toObservedObjectivesView,
  type ObservedObjectivesUnit,
} from "../../app/objectives";
import { formatWatts } from "../../lib/format";
import "./objectives.css";

type Phase = "loading" | "ready" | "error";

/**
 * Reconnect pause for the shared event stream: seen retrying within a glance,
 * never a tight spin (the Insights/Schedule views' own constant).
 */
const STREAM_RECONNECT_MS = 1000;
/**
 * The alert-driven re-read's debounce: the detector fires once per
 * (episode, reason), but a first-contact burst can land several units at
 * once — one read serves the burst.
 */
const ALERT_REREAD_DELAY_MS = 500;

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
    code: "objectives_unavailable",
    message: error instanceof Error ? error.message : "The observed objectives could not be loaded.",
    requestId: "",
  };
}

/** The window selector's own words for the three pinned spellings. */
const WINDOW_LABELS: Record<string, string> = {
  "24h": "24 hours",
  "3d": "3 days",
  "7d": "7 days",
};

function windowLabel(last: string): string {
  return WINDOW_LABELS[last] ?? `last ${last}`;
}

/** The echoed window + the answer's own stamp, as one honest caption clause. */
function windowCaptionText(view: { last: string | null; asOf: string | null }): string {
  const window = view.last === null ? "" : windowLabel(view.last);
  const asOf =
    view.asOf === null || view.asOf === "" ? "" : `, as of ${view.asOf.slice(11, 16)} local`;
  return `Window: ${window}${asOf}`;
}

/** "23:31 – 05:12" from the unit's own stamps; nulls named, never guessed. */
function seenText(unit: ObservedObjectivesUnit): string {
  const first = unit.firstSeenAt === null ? "" : unit.firstSeenAt.slice(11, 16);
  const last = unit.lastSeenAt === null ? "" : unit.lastSeenAt.slice(11, 16);
  if (first === "" && last === "") {
    return "not available";
  }
  if (first === "") {
    return `until ${last}`;
  }
  if (last === "") {
    return `from ${first}`;
  }
  return `${first} – ${last}`;
}

/** The sign distribution as the backend counted it (nonzero samples only). */
function samplesText(unit: ObservedObjectivesUnit): string {
  if (unit.sampleCount === 0) {
    return "no samples recorded";
  }
  return `${unit.sampleCount} (${unit.chargeSampleCount} charging, ${unit.dischargeSampleCount} discharging)`;
}

/**
 * The classification counts — the window's characterization, exactly what the
 * night-partition decision reads (the expected-nightly-charge count is the
 * known writer's own signature). Zero counts are omitted; a window with no
 * characterized samples says so.
 */
function characterizationText(unit: ObservedObjectivesUnit): string {
  const counts = OBJECTIVE_CLASSIFICATIONS.map((classification) =>
    classificationCountText(classification, unit.classificationCounts[classification] ?? 0),
  ).filter((entry): entry is string => entry !== null);
  if (counts.length === 0) {
    return unit.sampleCount === 0 ? "no samples recorded" : "none characterized";
  }
  return counts.join(" · ");
}

/** The min–typical–max span of the recorded samples; nulls named per part. */
function powerSpanText(unit: ObservedObjectivesUnit): string {
  const parts = [unit.minActiveW, unit.typicalActiveW, unit.maxActiveW].map((watts) =>
    watts === null ? "not available" : formatWatts(watts),
  );
  const [minimum, typical, maximum] = parts as [string, string, string];
  return `min ${minimum} · typical ${typical} · max ${maximum}`;
}

/** The episode history and the detector's standing assertion, in plain words. */
function foreignText(unit: ObservedObjectivesUnit): string {
  const count =
    unit.foreignEpisodeCount === 1
      ? "1 foreign episode"
      : `${unit.foreignEpisodeCount} foreign episodes`;
  if (unit.foreignActive === true) {
    const reason =
      unit.foreignReason === null ? "" : ` — ${objectiveReasonText(unit.foreignReason)}`;
    return `${count}; foreign now${reason}`;
  }
  return `${count}; none active`;
}

/** The last recorded sample: words, classification, time — the summary itself. */
function lastObjectiveText(unit: ObservedObjectivesUnit): string {
  const objective = unit.lastObjective;
  if (objective === null) {
    return "no recorded sample";
  }
  const watts =
    objective.activeW === null ? "" : `${formatWatts(objective.activeW)} held by `;
  const at = objective.observedAt === null || objective.observedAt === "" ? "" : ` at ${objective.observedAt.slice(11, 16)}`;
  return `${watts}${objectiveClassificationText(objective.classification)}${at}`;
}

export interface ObjectivesViewProps {
  client: ApiClient;
}

export function ObjectivesView({ client }: ObjectivesViewProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<ErrorView | null>(null);
  const [window, setWindow] = useState<string>(OBJECTIVE_WINDOW_OPTIONS[0] ?? "24h");
  const [view, setView] = useState<{
    units: ObservedObjectivesUnit[];
    last: string | null;
    asOf: string | null;
  } | null>(null);
  /**
   * The current window by reference: the live re-read below reads whatever
   * window is selected when it fires, without re-subscribing on a switch.
   */
  const windowRef = useRef(window);
  windowRef.current = window;

  const read = useCallback(
    async (last: string): Promise<boolean> => {
      try {
        const body = await client.getObservedObjectives(last);
        const parsed = toObservedObjectivesView(body);
        if (parsed === null) {
          setError({
            code: "unreadable_observed_objectives",
            message: "The observed objectives response could not be read.",
            requestId: "",
          });
          setPhase("error");
          return false;
        }
        setView({ units: parsed.units, last: parsed.last, asOf: parsed.asOf });
        setError(null);
        setPhase("ready");
        return true;
      } catch (failure) {
        const described = toErrorView(failure);
        setError(described);
        setPhase("error");
        return false;
      }
    },
    [client],
  );

  // The first load, and every window switch re-reads (each ask is its own
  // read; the answer's own echo is the caption).
  useEffect(() => {
    let cancelled = false;
    void read(window).then((ok) => {
      if (!cancelled && ok) {
        setPhase("ready");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [read, window]);

  const retry = useCallback((): void => {
    setPhase("loading");
    void read(window);
  }, [read, window]);

  /**
   * The silent re-read the live path uses: no loading flash over data that is
   * already on screen, and a transient failure changes nothing — the table
   * keeps its last honest picture (its `as of` caption says when it was
   * taken), and the next alert frame or the operator's retry re-reads.
   */
  const reread = useCallback(async (): Promise<void> => {
    const askedWindow = windowRef.current;
    try {
      const body = await client.getObservedObjectives(askedWindow);
      if (askedWindow !== windowRef.current) {
        return; // the operator switched windows while the read was in flight
      }
      const parsed = toObservedObjectivesView(body);
      if (parsed === null) {
        return;
      }
      setView({ units: parsed.units, last: parsed.last, asOf: parsed.asOf });
      setError(null);
    } catch {
      // The last table stays; the caption keeps naming its own as-of stamp.
    }
  }, [client]);

  // The live bus: the detector's ALERT frame (`foreign_objective.observed`)
  // is exactly the moment this evidence table's world changed — a sustained
  // foreign objective never re-fires, so without this subscription a console
  // left open on Objectives showed the mount-time window for the whole
  // session while Home/Batteries/Activity all moved (Insights' day_rolled
  // pattern: the transition drives one quiet re-read).
  useEffect(() => {
    let cancelled = false;
    let rereadTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleReread = (): void => {
      if (rereadTimer !== null) {
        return; // one read serves the burst
      }
      rereadTimer = setTimeout(() => {
        rereadTimer = null;
        if (!cancelled) {
          void reread();
        }
      }, ALERT_REREAD_DELAY_MS);
    };
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
            if (frame.type === "foreign_objective.observed") {
              scheduleReread();
            }
          }
        } catch {
          // A failed stream is treated exactly like a dropped one: retry.
        }
        if (cancelled) {
          return;
        }
        if (resync) {
          // A discontinuity means the window may be out of step; one quiet
          // re-read replaces it (the route is the truth here).
          void reread();
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, STREAM_RECONNECT_MS);
        });
      }
    };
    void run();
    return () => {
      cancelled = true;
      if (rereadTimer !== null) {
        clearTimeout(rereadTimer);
      }
    };
  }, [client, reread]);

  return (
    <section className="objectives-view" aria-labelledby="objectives-heading">
      <h2 id="objectives-heading">Objectives</h2>
      <p className="objectives-intro">
        What commanded the batteries while we commanded nothing — the served objective each pod
        was holding, sampled passively and recorded as evidence. The truth table for the
        night-partition decision lives here.
      </p>
      <p role="note" className="objectives-honesty">
        {OBJECTIVE_SIGNATURE_HONESTY_NOTE}
      </p>
      <div className="objectives-window" role="group" aria-label="Evidence window">
        {OBJECTIVE_WINDOW_OPTIONS.map((option) => (
          <button
            key={option}
            type="button"
            className="objectives-window-option"
            aria-pressed={window === option}
            onClick={() => setWindow(option)}
          >
            {windowLabel(option)}
          </button>
        ))}
      </div>

      {phase === "loading" && (
        <p role="status" aria-label="Loading observed objectives" className="objectives-loading">
          Loading the observed objectives…
        </p>
      )}

      {phase === "error" && error !== null && (
        <div role="alert" className="objectives-error">
          <p className="objectives-error-title">We couldn&apos;t load the observed objectives.</p>
          <p className="objectives-error-code">
            Code: <code>{error.code}</code>
          </p>
          <p className="objectives-error-message">{error.message}</p>
          {error.requestId !== "" && (
            <p className="objectives-error-request">
              Request ID: <code>{error.requestId}</code>
            </p>
          )}
          <button type="button" className="objectives-retry" onClick={retry}>
            Try again
          </button>
        </div>
      )}

      {phase === "ready" && view !== null && (
        <>
          <p className="objectives-caption">{windowCaptionText(view)}</p>
          {view.units.length === 0 ? (
            <p className="objectives-empty">
              No batteries are recorded in this window — samples appear once a pod holds an
              objective while nothing of ours commands it.
            </p>
          ) : (
            <table className="objectives-table">
              <caption className="objectives-table-caption">
                What we observed — the recorded evidence, per battery
              </caption>
              <thead>
                <tr>
                  <th scope="col">Battery</th>
                  <th scope="col">Seen (local)</th>
                  <th scope="col">Samples</th>
                  <th scope="col">Characterized</th>
                  <th scope="col">Power observed</th>
                  <th scope="col">Foreign writers</th>
                  <th scope="col">Last recorded sample</th>
                </tr>
              </thead>
              <tbody>
                {view.units.map((unit) => (
                  <tr key={unit.unitId} data-foreign-now={unit.foreignActive === true ? "true" : undefined}>
                    <th scope="row">{unit.unitId}</th>
                    <td>{seenText(unit)}</td>
                    <td>{samplesText(unit)}</td>
                    <td>{characterizationText(unit)}</td>
                    <td>{powerSpanText(unit)}</td>
                    <td>{foreignText(unit)}</td>
                    <td>{lastObjectiveText(unit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
