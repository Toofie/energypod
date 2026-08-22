/**
 * Presentational shell chrome: the always-visible fleet banner, the four-fact
 * connection indicator, and the operator-facing disconnected notice. All state
 * lives in AppShell; these components only render it.
 */
import type { ReactElement } from "react";
import type { UnitModel } from "./fleet";
import { fleetBanner, UNIT_LABELS } from "./fleet";
import type { ConsoleData } from "./useConsoleData";

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
 * The designed disconnected state: a notice apart from the four-fact
 * indicator, while the last known data stays on screen (dimmed) with its age.
 */
export function DisconnectedNotice(): ReactElement {
  return (
    <section aria-label="Live updates" className="disconnected-notice">
      <p>
        Live updates: connection lost. The last known picture is still shown —
        readings may be old. Reconnecting automatically…
      </p>
    </section>
  );
}
