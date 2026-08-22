/**
 * Presentational shell chrome: the always-visible fleet banner, the four-fact
 * connection indicator, the one-glance live-data badge, the controller-restart
 * notice, and the operator-facing disconnected notice. All state lives in
 * AppShell; these components only render it.
 */
import type { ReactElement } from "react";
import type { UnitModel } from "./fleet";
import { fleetBanner, UNIT_LABELS } from "./fleet";
import type { ConnectionHealth, ConsoleData, RefusalEnvelope } from "./useConsoleData";

const BADGE_ICONS: Record<string, string> = {
  Inhibited: "■",
  "Observe only": "◌",
  Limited: "◑",
  Active: "●",
  Stopping: "◒",
  Armed: "◐",
  Disarmed: "○",
  "No units yet": "·",
};

function ageText(unit: UnitModel): string {
  return unit.telemetryAgeS === null ? "" : ` · ${unit.telemetryAgeS} s old`;
}

export function FleetBanner({ units }: { units: UnitModel[] }): ReactElement {
  const banner = fleetBanner(units);
  return (
    <section aria-label="Fleet status" className="banner">
      {banner.badge !== null && (
        <p className="banner-badge">
          <span role="img" aria-label={banner.badge} className="badge-icon">
            {BADGE_ICONS[banner.badge] ?? "·"}
          </span>{" "}
          {banner.badge}
        </p>
      )}
      {banner.contactLost.length > 0 && (
        <p className="banner-contact">
          Contact lost with {banner.contactLost.length === 1 ? "1 unit" : `${banner.contactLost.length} units`} —
          their state is unknown.
        </p>
      )}
      {units.length === 0 ? (
        <p className="banner-units">No units are paired yet.</p>
      ) : (
        <ul className="banner-units">
          {units.map((unit) => (
            <li key={unit.unitId}>
              {unit.unitId} — {UNIT_LABELS[unit.lifecycle]}
              {ageText(unit)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function factClass(yes: boolean | null): string {
  return yes === null ? "fact pending" : yes ? "fact yes" : "fact no";
}

export function ConnectionFacts({ data }: { data: ConsoleData }): ReactElement {
  const health = data.health;
  const apiReachable = data.apiReachable;
  const controlBlocked =
    health !== null && !health.control_readiness.ready ? health.control_readiness.reasons : null;
  const controlValue =
    apiReachable === false
      ? "No — cannot check while the API is unreachable"
      : health === null
        ? "Checking"
        : controlBlocked === null
          ? "Yes"
          : `No — blocked (${controlBlocked.join(", ")})`;
  const streamValue =
    data.streamStatus === "live"
      ? "Yes — live"
      : data.streamStatus === "connecting"
        ? "Connecting"
        : "No — connection lost, reconnecting";
  return (
    <div role="group" aria-label="Connection" className="connection">
      <ul>
        <li className={factClass(true)} aria-label="Page loaded: Yes">
          Page loaded: <strong>Yes</strong>
        </li>
        <li
          className={factClass(apiReachable)}
          aria-label={`API reachable: ${
            apiReachable === null ? "Checking" : apiReachable ? "Yes" : "No — unreachable"
          }`}
        >
          API reachable:{" "}
          <strong>
            {apiReachable === null ? "Checking" : apiReachable ? "Yes" : "No — unreachable"}
          </strong>
        </li>
        <li
          className={factClass(data.streamStatus === "live")}
          aria-label={`Event stream: ${streamValue}`}
        >
          Event stream: <strong>{streamValue}</strong>
        </li>
        <li className={factClass(controlValue.startsWith("Yes"))} aria-label={`Control: ${controlValue}`}>
          Control: <strong>{controlValue}</strong>
        </li>
      </ul>
    </div>
  );
}

/**
 * The one-glance live-data badge, always visible on every view: never a
 * technical word in sight — "connection", not the transport. LIVE means data
 * is flowing; STALE names how long ago the last update landed (a climbing age
 * must be unmistakable from a healthy picture); RECONNECTING/OFFLINE say the
 * connection dropped / the service cannot be reached. The dot carries the
 * state by color AND the word carries it by text, never color alone.
 */
export function ConnectionStatusBadge({
  health,
  secondsSinceUpdate,
}: {
  health: ConnectionHealth;
  secondsSinceUpdate: number | null;
}): ReactElement {
  let text: string;
  if (health === "live") {
    text = "Live";
  } else if (health === "stale") {
    text = `Stale — last update ${secondsSinceUpdate ?? "many"} s ago`;
  } else if (health === "offline") {
    text = "Offline — the EnergyPod service cannot be reached";
  } else {
    // The very first connection is still being made; anything later is a
    // reconnection after a loss.
    text = secondsSinceUpdate === null ? "Connecting" : "Reconnecting";
  }
  return (
    <p aria-label="Live data" className={`connection-badge connection-badge--${health}`}>
      <span aria-hidden="true" className="connection-badge-dot" />
      {text}
    </p>
  );
}

/**
 * The calm, non-blocking trace of a connection that had to resume — the
 * operator-visible answer to "when did the controller restart?". It blocks
 * nothing and is cleared by the next connection loss.
 */
export function RestartNotice({ text }: { text: string }): ReactElement {
  return (
    <p role="status" className="restart-notice">
      {text}
    </p>
  );
}

/**
 * The designed disconnected state: a notice apart from the four-fact
 * indicator, while the last known data stays on screen (dimmed) with its age.
 * The stream's own error envelope — when there is one — is surfaced here
 * verbatim instead of being swallowed by the retry loop, and once the
 * automatic reconnect budget is spent the operator is left a manual retry
 * rather than a silent, endless hammering of a failing endpoint.
 */
export function DisconnectedNotice({
  error,
  exhausted,
  onRetry,
}: {
  error: RefusalEnvelope | null;
  exhausted: boolean;
  onRetry: () => void;
}): ReactElement {
  return (
    <section aria-label="Live updates" className="disconnected-notice">
      <p>
        {exhausted
          ? "Live updates: connection lost, and automatic reconnection has paused after repeated failures. The last known picture is still shown — readings may be old."
          : "Live updates: connection lost. The last known picture is still shown — readings may be old. Reconnecting automatically…"}
      </p>
      {error !== null && (
        <p className="stream-error-envelope">
          <span>{error.code}</span> — <span>{error.message}</span>
        </p>
      )}
      {exhausted && (
        <button type="button" className="stream-retry" onClick={onRetry}>
          Try reconnecting
        </button>
      )}
    </section>
  );
}
