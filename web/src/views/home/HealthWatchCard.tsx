/**
 * The "Battery health watch" Home card (DESIGN_BATTERY_HEALTH_WATCH §13):
 * the nightly program's whole story in one glance — the program state, one
 * row per battery (census chip / probe verdict with its one-line figures /
 * recovery word), the morning-after alert line, and the honesty the contract
 * pins.
 *
 * Feature-detected exactly like the Night tile: it renders ONLY when the
 * snapshot carries a `health_watch_state` projection (the block-presence
 * doctrine — a site without the `battery_health_watch` block sees nothing at
 * all, and the status route answers its own 409).
 *
 * Honesty rules pinned here (the contract's own):
 *
 * - Uncommissioned stages render "not commissioned" where their UI would be
 *   — a site without Stage R never sees "recovery" offered as a suggestion
 *   it cannot execute (§13's uncommissioned honesty).
 * - Skipped units render their skip reason verbatim; a disarmed unit renders
 *   as the arm instruction it is (the program NEVER arms — §6.1).
 * - A probe failure is ALERT-tier styling and says what it did: recorded and
 *   alerted, nothing more — NO automated recovery exists in this deployment,
 *   and no code path responds to a verdict with any write.
 * - The §11 defined-restart advisory rides beside an alert-tier failure so
 *   the escalation ladder's terminal text is on the card, not in a log
 *   directory.
 * - The §6.3 export note names the probe's brief feed-in blip so the export
 *   meter is never a surprise.
 * - The not-isolation sentence rides the card's parked-state styling
 *   wherever a parked unit's state shows (§13's pinned rule).
 */
import { useId } from "react";
import type { JSX } from "react";
import { NOT_ISOLATION_SENTENCE } from "../../app/park";
import {
  HEALTH_WATCH_EXPORT_NOTE,
  HEALTH_WATCH_RESTART_ADVISORY,
  anyUnitParked,
  censusChipText,
  healthWatchAlertText,
  healthWatchStatusText,
  healthWatchStagesText,
  probeVerdictText,
  type HealthWatchState,
  type HealthWatchUnitState,
} from "../../app/healthWatch";

export interface HealthWatchCardProps {
  /**
   * The snapshot's health-watch projection; null (or a stale object literal
   * built before the key existed) hides the whole card — the feature
   * detection is the field's own absence.
   */
  health: HealthWatchState | null | undefined;
}

function unitTier(unit: HealthWatchUnitState): "alert" | "notice" | "quiet" {
  if (unit.census.tier === "alert" || (unit.probe.verdict ?? "").startsWith("fail")) {
    return "alert";
  }
  if (unit.census.verdict === "stuck_suspected") {
    return "notice";
  }
  return "quiet";
}

/** One battery's row: the census chip, the probe line, the recovery word. */
function unitRow(unit: HealthWatchUnitState, keyPrefix: string): JSX.Element {
  const tier = unitTier(unit);
  const recovery =
    unit.recovery.mode === "uncommissioned"
      ? "recovery not commissioned"
      : `recovery ${unit.recovery.mode}${unit.recovery.note === null ? "" : ` — ${unit.recovery.note}`}`;
  return (
    <li
      key={`${keyPrefix}-${unit.unitId}`}
      className="home-health-unit"
      data-tier={tier}
      aria-label={`${unit.unitId} health watch row`}
    >
      <span className="home-health-unit-name">{unit.unitId}</span>
      <span className="home-health-census" data-tier={unit.census.tier ?? undefined}>
        {censusChipText(unit)}
      </span>
      <span className="home-health-probe" data-verdict={unit.probe.verdict ?? undefined}>
        {probeVerdictText(unit)}
      </span>
      <span className="home-health-recovery">{recovery}</span>
    </li>
  );
}

export function HealthWatchCard({ health }: HealthWatchCardProps): JSX.Element | null {
  const headingId = useId();
  if (health == null) {
    // Feature detection: no health_watch_state means the watch is not
    // composed on this deployment — nothing renders at all.
    return null;
  }
  const alert = healthWatchAlertText(health);
  const probed = health.units.some((unit) => unit.probe.verdict !== null);
  const parked = anyUnitParked(health);
  return (
    <section
      className="home-card home-card--health"
      aria-labelledby={headingId}
      data-phase={health.phase}
      data-alert={alert === null ? "false" : "true"}
    >
      <h2 id={headingId}>Battery health watch</h2>
      <p className="home-health-status">{healthWatchStatusText(health)}</p>
      <p className="home-health-stages">{healthWatchStagesText(health)}</p>
      {alert !== null && (
        <p role="alert" className="home-health-alert">
          {alert}
        </p>
      )}
      {alert !== null && (
        <p className="home-health-advisory">{HEALTH_WATCH_RESTART_ADVISORY}</p>
      )}
      {health.units.length > 0 && (
        <ul className="home-health-units" aria-label="Per-battery health watch rows">
          {health.units.map((unit) => unitRow(unit, headingId))}
        </ul>
      )}
      {probed && (
        <p role="note" className="home-health-export-note">
          {HEALTH_WATCH_EXPORT_NOTE}.
        </p>
      )}
      {parked && <p className="home-health-parked-note">{NOT_ISOLATION_SENTENCE}</p>}
    </section>
  );
}
