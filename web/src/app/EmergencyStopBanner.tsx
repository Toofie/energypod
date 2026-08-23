/**
 * The always-visible emergency-stop latch banner (the 2026-08-23 incident
 * fix): an engaged stop is announced from the SNAPSHOT's `active_stops`, not
 * from session state or a bus event, so every console — including one opened
 * after the stop was pressed — sees the latch and can release it.
 *
 * Where the release used to live (NowView's stopOutcome) only the browser
 * session that pressed the stop ever saw it, and the latch notice only
 * appeared if the console was connected at the latch moment. This banner
 * renders at shell-chrome level, on every view, from the shared snapshot
 * (initial read + republished refreshes + the live-cadence poll), and carries
 * the type-it-back release inline: the Acknowledge button unlocks only when
 * the typed text equals the stop id exactly — the same deliberate friction
 * the service's own acknowledgement requires.
 *
 * The acknowledge call goes through the session client
 * (`postStopAcknowledgement`), which always sends the Idempotency-Key header
 * with a fresh key per call, so the service's `idempotency_key_required` 400
 * can never happen. On 200 the banner calls `onReleased`, which clears the
 * latch from the shell's snapshot state optimistically; the next snapshot
 * read (forced immediately, else the live-cadence poll) confirms — and a
 * snapshot that still names the stop brings the banner back, honestly.
 *
 * Renders nothing while the snapshot carries no engaged stops — including
 * today's backend, which sends no `active_stops` at all: absence of the field
 * is the feature detection, and the console behaves exactly as before until
 * the contract lands.
 */
import { useState } from "react";
import type { ReactElement } from "react";
import { ApiClientError, isUnauthorizedError } from "../api/client";
import type { ApiClient } from "../api/client";
import type { ActiveStop } from "./fleet";

export interface EmergencyStopBannerProps {
  /** The snapshot's engaged stops; the banner renders nothing when empty. */
  stops: ActiveStop[];
  client: ApiClient;
  onUnauthorized: (error: ApiClientError) => void;
  /** An acknowledge returned 200 for this stop: clear the latch state. */
  onReleased: (stopId: string) => void;
}

/** "2026-08-23T23:14:24Z" -> "23:14 UTC"; an unreadable stamp stays verbatim. */
export function latchedAtText(latchedAt: string): string {
  const when = new Date(latchedAt);
  if (Number.isNaN(when.getTime())) {
    return latchedAt === "" ? "an unknown time" : latchedAt;
  }
  const hours = when.getUTCHours().toString().padStart(2, "0");
  const minutes = when.getUTCMinutes().toString().padStart(2, "0");
  return `${hours}:${minutes} UTC`;
}

/**
 * One stop's row: the held-power statement, the id, who engaged it and when,
 * and the inline release. Each stop releases separately — the input gates on
 * that row's own stop id.
 */
function StopRow({
  stop,
  client,
  onUnauthorized,
  onReleased,
}: {
  stop: ActiveStop;
  client: ApiClient;
  onUnauthorized: (error: ApiClientError) => void;
  onReleased: (stopId: string) => void;
}): ReactElement {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);
  const inputId = `stop-release-input-${stop.stopId}`;

  const acknowledge = (): void => {
    if (busy || typed.trim() !== stop.stopId) {
      return;
    }
    setBusy(true);
    setFailure(null);
    // The client mints the Idempotency-Key header itself (a fresh key per
    // call), so the mutation can never trip idempotency_key_required.
    client
      .postStopAcknowledgement(stop.stopId)
      .then(() => {
        setBusy(false);
        setTyped("");
        onReleased(stop.stopId);
      })
      .catch((error: unknown) => {
        setBusy(false);
        if (isUnauthorizedError(error)) {
          onUnauthorized(error);
          return;
        }
        if (error instanceof ApiClientError) {
          setFailure({ code: error.code, message: error.message });
          return;
        }
        setFailure({
          code: "unexpected_error",
          message: "The acknowledgement did not complete.",
        });
      });
  };

  return (
    <div className="stop-latch-row">
      <p className="stop-latch-statement">
        Emergency stop active — all battery power is held at 0 W
      </p>
      <p className="stop-latch-facts">
        Stop <code>{stop.stopId}</code>
        {stop.principal !== "" ? <> · engaged by {stop.principal}</> : null}
        {stop.latchedAt !== "" ? <> · at {latchedAtText(stop.latchedAt)}</> : null}
        {stop.unitIds === null ? " · the whole fleet" : null}
      </p>
      <div className="stop-latch-release">
        <label htmlFor={inputId}>Stop id</label>
        <input
          id={inputId}
          type="text"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          onChange={(event) => setTyped(event.target.value)}
        />
        <button type="button" disabled={busy || typed.trim() !== stop.stopId} onClick={acknowledge}>
          Acknowledge
        </button>
        <p className="stop-latch-hint">
          Type the stop id exactly as shown above to release it.
        </p>
        {failure !== null && (
          <p className="refusal">
            <span>{failure.code}</span> — <span>{failure.message}</span>
          </p>
        )}
      </div>
    </div>
  );
}

export function EmergencyStopBanner({
  stops,
  client,
  onUnauthorized,
  onReleased,
}: EmergencyStopBannerProps): ReactElement {
  if (stops.length === 0) {
    return <></>;
  }
  return (
    <section role="alert" aria-label="Emergency stop active" className="stop-latch-banner">
      {stops.map((stop) => (
        <StopRow
          key={stop.stopId}
          stop={stop}
          client={client}
          onUnauthorized={onUnauthorized}
          onReleased={onReleased}
        />
      ))}
    </section>
  );
}
