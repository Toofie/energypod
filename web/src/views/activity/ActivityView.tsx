// Activity view: the household's audit timeline.
//
// Contract: docs/UI_CONTRACTS.md "Activity" and "State and error contract";
// the authoritative behavior pins live in ActivityView.test.tsx. The view is
// a calm, newest-first timeline from GET /api/v1/audit with cursor
// pagination, plain language first with raw reason codes on demand, and
// honest empty/loading/disconnected/stale/partial/error states.
//
// Shape notes (client.ts + docs/API_CONTRACTS.md):
// - Pages are { events, next_cursor }; next_cursor null ends the timeline.
// - Audit events carry lowercase wire enums (decision_status "authorized" /
//   "clamped"); the console never capitalizes wire values in logic, only in
//   human labels.
// - Failures reject as ApiClientError carrying the error envelope verbatim
//   (code, message, details, request_id) plus the HTTP status. Every rendered
//   error envelope keeps all four verbatim so the operator can quote them.

import { Fragment } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClientError } from "../../api/client";
import type { ApiClient, AuditEvent } from "../../api/client";
import "./activity.css";

export type ActivityConnection = "connected" | "disconnected";

export interface ActivityViewProps {
  client: ApiClient;
  connection?: ActivityConnection | undefined;
}

/** The audit page size the view asks for; the cursor pagination honors it. */
const AUDIT_PAGE_SIZE = 50;

/** The fleet's named units (UI_CONTRACTS.md "Batteries": MID, RHS, LHS). */
const FLEET_UNIT_IDS: readonly string[] = ["MID", "RHS", "LHS"];

/** Entries older than this are stale: dimmed, never hidden, age kept. */
const STALE_AFTER_MS = 60 * 60_000;

/** Placeholder rows inside the loading status; bounded by the page size. */
const SKELETON_ROWS = 6;

type KindKey =
  | "observations"
  | "decisions"
  | "arming"
  | "stops"
  | "acknowledgements";

const KIND_CHIPS: readonly { readonly id: KindKey; readonly label: string }[] =
  [
    { id: "observations", label: "Observations" },
    { id: "decisions", label: "Decisions" },
    { id: "arming", label: "Arming" },
    { id: "stops", label: "Stops" },
    { id: "acknowledgements", label: "Acknowledgements" },
  ];

/** The five contracted kinds. Arm and disarm are one kind (Arming); every
 * *_acknowledgement is an Acknowledgement, never a Stop. */
function kindOf(type: string): KindKey | "other" {
  if (type.endsWith("_acknowledgement")) {
    return "acknowledgements";
  }
  switch (type) {
    case "observation":
      return "observations";
    case "decision":
      return "decisions";
    case "arm":
    case "disarm":
      return "arming";
    case "stop":
      return "stops";
    default:
      return "other";
  }
}

function stringField(event: AuditEvent, key: string): string | undefined {
  const value = event[key];
  return typeof value === "string" ? value : undefined;
}

function numberField(event: AuditEvent, key: string): number | undefined {
  const value = event[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** A raw wire code as calm words: SITE_EXPORT_LIMIT -> "Site export limit". */
function humanize(code: string): string {
  const words = code.toLowerCase().split(/[_\s]+/).filter((word) => word !== "");
  if (words.length === 0) {
    return code;
  }
  const first = words[0] ?? "";
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(
    " ",
  );
}

/** The entry's age in household words ("2 hours ago"), "" when unknown. */
function ageText(occurredAt: string | undefined, now: number): string {
  if (occurredAt === undefined) {
    return "";
  }
  const when = Date.parse(occurredAt);
  if (Number.isNaN(when)) {
    return "";
  }
  const minutes = Math.max(0, Math.floor((now - when) / 60_000));
  if (minutes < 1) {
    return "just now";
  }
  if (minutes < 60) {
    return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function isStale(occurredAt: string | undefined, now: number): boolean {
  if (occurredAt === undefined) {
    return false;
  }
  const when = Date.parse(occurredAt);
  return !Number.isNaN(when) && now - when >= STALE_AFTER_MS;
}

/** The timeline is newest-first: highest sequence on top, stably. */
function newestFirst(events: AuditEvent[]): AuditEvent[] {
  return [...events].sort((a, b) => b.sequence - a.sequence);
}

/** Older pages append below without duplicating an already-loaded sequence. */
function mergeBySequence(
  existing: AuditEvent[],
  incoming: AuditEvent[],
): AuditEvent[] {
  const seen = new Set(existing.map((event) => event.sequence));
  const fresh = incoming.filter((event) => !seen.has(event.sequence));
  return newestFirst([...existing, ...fresh]);
}

function principalName(event: AuditEvent): string | null {
  const principal = event.principal;
  if (principal === null || typeof principal !== "object" || Array.isArray(principal)) {
    return null;
  }
  const displayName = (principal as Record<string, unknown>)["display_name"];
  return typeof displayName === "string" && displayName !== "" ? displayName : null;
}

function reasonCodesOf(event: AuditEvent): string[] {
  const value = event.reason_codes;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (code): code is string => typeof code === "string" && code !== "",
  );
}

function headlineFor(type: string): string {
  switch (type) {
    case "decision":
      return "Power request";
    case "observation":
      return "Observation";
    case "arm":
      return "Arm request";
    case "disarm":
      return "Disarm request";
    case "stop":
      return "Emergency stop";
    default:
      return kindOf(type) === "acknowledgements"
        ? "Acknowledgement"
        : humanize(type);
  }
}

/** Was the control action allowed through (or reduced), not refused? */
function statusAllows(event: AuditEvent): boolean {
  const status = stringField(event, "decision_status");
  return status === undefined || status === "authorized" || status === "clamped";
}

/** What was decided, mapped from decision status and the watt figures. */
function decidedLine(event: AuditEvent): string | null {
  const status = stringField(event, "decision_status");
  if (status === undefined) {
    return null;
  }
  if (event.type === "decision") {
    const requested = numberField(event, "requested_watts");
    const authorized = numberField(event, "authorized_watts");
    if (requested !== undefined && authorized !== undefined) {
      return status === "clamped"
        ? `Reduced to ${authorized} W of the ${requested} W requested`
        : `Allowed ${authorized} W of the ${requested} W requested`;
    }
  }
  if (status === "authorized" || status === "clamped") {
    return "Allowed as requested";
  }
  return humanize(status);
}

/** What happened. A missing result is named as missing, never invented. */
function happenedLine(event: AuditEvent): string | null {
  const type = event.type;
  if (type === "decision") {
    const measured = numberField(event, "measured_watts");
    return measured !== undefined ? `Delivering ${measured} W` : "Result not recorded yet";
  }
  if (stringField(event, "decision_status") === undefined) {
    return null;
  }
  if (!statusAllows(event)) {
    return "Result not recorded yet";
  }
  if (type === "arm") {
    return "Armed";
  }
  if (type === "disarm") {
    return "Disarmed";
  }
  if (type === "stop") {
    const stopId = stringField(event, "stop_id");
    return stopId === undefined ? "Stop latched" : `Stop latched (${stopId})`;
  }
  if (kindOf(type) === "acknowledgements") {
    return "Acknowledged";
  }
  return "Result not recorded yet";
}

function whyLine(codes: string[]): string | null {
  return codes.length > 0 ? codes.map(humanize).join(", ") : null;
}

/** The API's error envelope, kept verbatim for rendering. */
interface ErrorView {
  code: string;
  message: string;
  requestId: string;
}

function toErrorView(error: unknown): ErrorView {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message: error.message,
      requestId: error.request_id,
    };
  }
  return {
    code: "activity_unavailable",
    message:
      error instanceof Error
        ? error.message
        : "The activity could not be loaded",
    requestId: "",
  };
}

type Phase = "first-load" | "ready" | "error";

export function ActivityView({ client, connection = "connected" }: ActivityViewProps) {
  const [phase, setPhase] = useState<Phase>("first-load");
  const [skeletonConfirmed, setSkeletonConfirmed] = useState(false);
  const settledRef = useRef(false);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [pageError, setPageError] = useState<ErrorView | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<ErrorView | null>(null);
  const [kindFilter, setKindFilter] = useState<KindKey | null>(null);
  const [unitFilter, setUnitFilter] = useState<string | null>(null);

  const applyFirstPage = (page: { events: AuditEvent[]; next_cursor: number | null }) => {
    setEvents(newestFirst(page.events));
    setNextCursor(page.next_cursor);
  };

  // First load: REST is the view's own source of truth, whatever the socket
  // is doing (the disconnected notice is additive, never a substitute).
  useEffect(() => {
    let cancelled = false;
    settledRef.current = false;
    const load = async () => {
      try {
        const page = await client.getAudit(AUDIT_PAGE_SIZE);
        settledRef.current = true;
        if (cancelled) {
          return;
        }
        applyFirstPage(page);
        setPageError(null);
        setPhase("ready");
      } catch (error) {
        settledRef.current = true;
        if (cancelled) {
          return;
        }
        setPageError(toErrorView(error));
        setPhase("error");
      }
    };
    void load();
    // The skeleton placeholder rows wait one tick: a page that answers
    // within the current task never flashes placeholder entries (no layout
    // shift, no fake rows), while any real wait still gets the full
    // skeleton inside the loading status. The settlement check runs after
    // the fetch's own continuation when the promise is already settled.
    queueMicrotask(() => {
      if (!cancelled && !settledRef.current) {
        setSkeletonConfirmed(true);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // Retry with nothing loaded: the error stays on screen (no skeleton flash,
  // no fake entries) until the new page actually lands.
  const retryFirstPage = useCallback(async () => {
    if (retrying) {
      return;
    }
    setRetrying(true);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE);
      applyFirstPage(page);
      setPageError(null);
      setPhase("ready");
    } catch (error) {
      setPageError(toErrorView(error));
      setPhase("error");
    } finally {
      setRetrying(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, retrying]);

  // Manual REST refresh while data is on screen: entries stay visible; the
  // fresh page merges in so loaded history is never dropped.
  const refreshPage = useCallback(async () => {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setPageError(null);
    setMoreError(null);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE);
      setEvents((previous) => mergeBySequence(previous, page.events));
      setNextCursor(page.next_cursor);
    } catch (error) {
      setPageError(toErrorView(error));
    } finally {
      setRefreshing(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, refreshing]);

  const retry = useCallback(() => {
    if (phase === "ready") {
      void refreshPage();
    } else {
      void retryFirstPage();
    }
  }, [phase, refreshPage, retryFirstPage]);

  // Load more passes the previous page's next_cursor; a null cursor means
  // the end of the timeline and the control disappears.
  const loadMore = useCallback(async () => {
    if (nextCursor === null || loadingMore) {
      return;
    }
    setLoadingMore(true);
    setPageError(null);
    setMoreError(null);
    try {
      const page = await client.getAudit(AUDIT_PAGE_SIZE, nextCursor);
      setEvents((previous) => mergeBySequence(previous, page.events));
      setNextCursor(page.next_cursor);
    } catch (error) {
      setMoreError(toErrorView(error));
    } finally {
      setLoadingMore(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, nextCursor, loadingMore]);

  const visibleEvents = useMemo(
    () =>
      events.filter(
        (event) =>
          (kindFilter === null || kindOf(event.type) === kindFilter) &&
          (unitFilter === null || stringField(event, "unit_id") === unitFilter),
      ),
    [events, kindFilter, unitFilter],
  );

  const toggleKind = useCallback((id: KindKey) => {
    setKindFilter((previous) => (previous === id ? null : id));
  }, []);

  const toggleUnit = useCallback((id: string) => {
    setUnitFilter((previous) => (previous === id ? null : id));
  }, []);

  const clearUnit = useCallback(() => {
    setUnitFilter(null);
  }, []);

  const now = Date.now();

  return (
    <section className="activity-view" aria-labelledby="activity-view-heading">
      <header className="activity-view__header">
        <h2 id="activity-view-heading">Activity</h2>
        <p className="activity-view__intro">
          Everything your EnergyPod did, newest first.
        </p>
      </header>

      {connection === "disconnected" && (
        <div role="status" className="activity-notice">
          <p className="activity-notice__title">Connection lost</p>
          <p className="activity-notice__body">
            We're showing the activity we have, with how old each entry is.
          </p>
          {pageError === null && moreError === null && (
            <button
              type="button"
              className="activity-notice__retry"
              onClick={retry}
              disabled={refreshing}
            >
              Try again
            </button>
          )}
        </div>
      )}

      {phase === "first-load" && <LoadingSkeleton placeholders={skeletonConfirmed} />}

      {phase === "error" && pageError !== null && (
        <ActivityError
          title="We couldn't load the activity just now."
          error={pageError}
          retryDisabled={retrying}
          onRetry={retry}
        />
      )}

      {phase === "ready" && (
        <>
          <ActivityFilters
            kindFilter={kindFilter}
            unitFilter={unitFilter}
            onToggleKind={toggleKind}
            onToggleUnit={toggleUnit}
            onClearUnit={clearUnit}
          />

          {pageError !== null && (
            <ActivityError
              title="We couldn't refresh the activity."
              error={pageError}
              retryDisabled={refreshing}
              onRetry={retry}
            />
          )}

          {events.length === 0 ? (
            <EmptyActivity />
          ) : visibleEvents.length === 0 ? (
            <p className="activity-filtered-empty">No activity matches these filters</p>
          ) : (
            <ol
              className={
                connection === "disconnected"
                  ? "activity-timeline activity-timeline--dimmed"
                  : "activity-timeline"
              }
            >
              {visibleEvents.map((event) => (
                <ActivityEntry key={event.sequence} event={event} now={now} />
              ))}
            </ol>
          )}

          {nextCursor !== null && moreError === null && (
            <div className="activity-load-more-row">
              <button
                type="button"
                className="activity-load-more"
                onClick={() => void loadMore()}
                disabled={loadingMore}
              >
                Load more
              </button>
            </div>
          )}

          {moreError !== null && (
            <ActivityError
              title="We couldn't load more activity."
              error={moreError}
              retryDisabled={loadingMore}
              onRetry={() => void loadMore()}
            />
          )}
        </>
      )}
    </section>
  );
}

/** Skeleton placeholder entries inside the loading status region: structural
 * rows that carry no data and never name a unit. The rows appear once the
 * wait has outlived the current task, so an instantly answered page never
 * flashes placeholders. */
function LoadingSkeleton({ placeholders }: { placeholders: boolean }) {
  return (
    <div role="status" aria-label="Loading activity" className="activity-loading">
      <p className="activity-loading__text">Loading activity…</p>
      {placeholders && (
        <ul className="activity-loading__list">
          {Array.from({ length: SKELETON_ROWS }, (_, index) => (
            <li key={index} className="activity-loading__row" />
          ))}
        </ul>
      )}
    </div>
  );
}

function ActivityFilters({
  kindFilter,
  unitFilter,
  onToggleKind,
  onToggleUnit,
  onClearUnit,
}: {
  kindFilter: KindKey | null;
  unitFilter: string | null;
  onToggleKind: (id: KindKey) => void;
  onToggleUnit: (id: string) => void;
  onClearUnit: () => void;
}) {
  return (
    <div className="activity-filters">
      <div className="activity-filters__group" role="group" aria-label="Filter by kind">
        {KIND_CHIPS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            className="activity-chip"
            aria-pressed={kindFilter === id}
            onClick={() => onToggleKind(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="activity-filters__group" role="group" aria-label="Filter by unit">
        <button
          type="button"
          className="activity-chip"
          aria-pressed={unitFilter === null}
          onClick={onClearUnit}
        >
          All units
        </button>
        {FLEET_UNIT_IDS.map((unitId) => (
          <button
            key={unitId}
            type="button"
            className="activity-chip"
            aria-pressed={unitFilter === unitId}
            onClick={() => onToggleUnit(unitId)}
          >
            {unitId}
          </button>
        ))}
      </div>
    </div>
  );
}

function EmptyActivity() {
  return (
    <div className="activity-empty">
      <h3 className="activity-empty__title">Nothing here yet</h3>
      <p className="activity-empty__body">
        Decisions, observations, arming, stops and acknowledgements will appear
        here as your EnergyPod runs.
      </p>
      <p className="activity-empty__first">
        To make your first request, open the Now view.
      </p>
    </div>
  );
}

/** The API's error envelope verbatim — code, message, request id — with the
 * manual retry. Never a stack trace, never a silent failure. */
function ActivityError({
  title,
  error,
  retryDisabled,
  onRetry,
}: {
  title: string;
  error: ErrorView;
  retryDisabled: boolean;
  onRetry: () => void;
}) {
  return (
    <div role="alert" className="activity-error">
      <p className="activity-error__title">{title}</p>
      <p className="activity-error__code">
        Code: <code>{error.code}</code>
      </p>
      <p className="activity-error__message">{error.message}</p>
      {error.requestId !== "" && (
        <p className="activity-error__request">
          Request ID: <code>{error.requestId}</code>
        </p>
      )}
      <button
        type="button"
        className="activity-error__retry"
        onClick={onRetry}
        disabled={retryDisabled}
      >
        Try again
      </button>
    </div>
  );
}

/** One timeline entry: who requested it, what was decided, what happened,
 * and why — plain language first, raw codes behind a disclosure. */
function ActivityEntry({ event, now }: { event: AuditEvent; now: number }) {
  const [detailOpen, setDetailOpen] = useState(false);
  const unitId = stringField(event, "unit_id");
  const occurredAt = stringField(event, "occurred_at");
  const age = ageText(occurredAt, now);
  const stale = isStale(occurredAt, now);
  const who = principalName(event);
  const decided = decidedLine(event);
  const happened = happenedLine(event);
  const codes = reasonCodesOf(event);
  const why = whyLine(codes);
  const detailId = `activity-detail-${event.sequence}`;

  return (
    <li
      className={
        stale ? "activity-entry activity-entry--stale" : "activity-entry"
      }
    >
      <p className="activity-entry__head">
        <span className="activity-entry__kind">{headlineFor(event.type)}</span>
        {unitId !== undefined && (
          <span className="activity-entry__unit">{unitId}</span>
        )}
        {age !== "" && (
          <time className="activity-entry__age" dateTime={occurredAt}>
            {age}
          </time>
        )}
      </p>
      {who !== null && (
        <p className="activity-entry__who">
          Requested by <span className="activity-entry__principal">{who}</span>
        </p>
      )}
      {(decided !== null || happened !== null || why !== null) && (
        <dl className="activity-entry__facts">
          {decided !== null && (
            <div className="activity-entry__fact">
              <dt>What was decided</dt>
              <dd>{decided}</dd>
            </div>
          )}
          {happened !== null && (
            <div className="activity-entry__fact">
              <dt>What happened</dt>
              <dd>{happened}</dd>
            </div>
          )}
          {why !== null && (
            <div className="activity-entry__fact">
              <dt>Why</dt>
              <dd className="activity-entry__why">{why}</dd>
            </div>
          )}
        </dl>
      )}
      {codes.length > 0 && (
        <div className="activity-entry__technical">
          <button
            type="button"
            className="activity-entry__toggle"
            aria-expanded={detailOpen}
            aria-controls={detailId}
            onClick={() => setDetailOpen((open) => !open)}
          >
            Show technical detail
          </button>
          <details
            id={detailId}
            open={detailOpen}
            className="activity-entry__detail"
          >
            <div className="activity-entry__codes">
              <span className="activity-entry__codes-label">Reason codes: </span>
              {codes.map((code, index) => (
                <Fragment key={`${code}-${index}`}>
                  {index > 0 ? ", " : ""}
                  <code>{code}</code>
                </Fragment>
              ))}
            </div>
          </details>
        </div>
      )}
    </li>
  );
}
