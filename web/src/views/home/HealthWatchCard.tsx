/**
 * The "Battery health watch" Home card (DESIGN_BATTERY_HEALTH_WATCH §13):
 * the nightly program's whole story in one glance — the program state, one
 * row per battery (census chip / probe verdict with its one-line figures /
 * recovery verdict with its figures), the recovery advisory surface, the
 * morning-after line, and the honesty the contract pins.
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
 * - A16's soft note (`phase_idle_or_ct_silent`) renders as the
 *   informational note it is — its age and the `ct_link_suspect` annotator
 *   on the chip, notice styling, never a flag, never promoting (§5).
 * - Skipped units render their skip reason verbatim; a disarmed unit renders
 *   as the arm instruction it is (the program never arms outside the ONE
 *   bounded verification re-arm inside an `auto` cycle — §6.1/§7.4).
 * - The recovery surface names the posture's own truth: `advise` renders the
 *   operator walkthrough (this posture writes nothing, ever); `auto` renders
 *   what the program did and the morning re-arm the operator owes.
 * - The §11 defined-restart advisory rides beside every alert-tier outcome,
 *   and the BMU cross-check note rides beside any cycle that ran.
 * - The §6.3 export note names the probe's brief feed-in blip so the export
 *   meter is never a surprise.
 * - The not-isolation sentence rides the card's parked-state styling
 *   wherever a parked unit's state shows (§13's pinned rule).
 */
import { useId } from "react";
import type { JSX } from "react";
import { NOT_ISOLATION_SENTENCE } from "../../app/park";
import {
  HEALTH_WATCH_BMU_CROSS_CHECK,
  HEALTH_WATCH_EXPORT_NOTE,
  HEALTH_WATCH_RESTART_ADVISORY,
  anyUnitParked,
  censusChipText,
  healthWatchAlertText,
  healthWatchStatusText,
  healthWatchStagesText,
  probeVerdictText,
  recoveryMorningText,
  recoveryRouteText,
  recoveryVerdictText,
  recoveryWalkthroughText,
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
  if (
    unit.census.tier === "alert" ||
    (unit.probe.verdict ?? "").startsWith("fail") ||
    unit.recovery.tier === "alert"
  ) {
    return "alert";
  }
  if (
    unit.census.verdict === "stuck_suspected" ||
    // A16's soft note: informational, so it carries the notice styling —
    // never the alert styling, at any age.
    unit.census.verdict === "phase_idle_or_ct_silent" ||
    unit.recovery.verdict === "recovered"
  ) {
    return "notice";
  }
  return "quiet";
}

/** One battery's row: the census chip, the probe line, the recovery verdict. */
function unitRow(unit: HealthWatchUnitState, keyPrefix: string): JSX.Element {
  const tier = unitTier(unit);
  const recovery =
    unit.recovery.mode === "uncommissioned"
      ? "recovery not commissioned"
      : recoveryVerdictText(unit);
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
      <span
        className="home-health-recovery"
        data-verdict={unit.recovery.verdict ?? undefined}
        data-tier={unit.recovery.tier ?? undefined}
      >
        {recovery}
      </span>
    </li>
  );
}

/** The per-unit recovery advisory surface (§13): the posture's own truth.
 * Skipped outcomes render their reason on the row itself — the advisory
 * card belongs to the outcomes that owe the operator an act. */
function unitAdvisory(
  unit: HealthWatchUnitState,
  keyPrefix: string,
): JSX.Element | null {
  if (
    unit.recovery.mode === "uncommissioned" ||
    unit.recovery.verdict === null ||
    unit.recovery.verdict.startsWith("skipped:")
  ) {
    return null;
  }
  const walkthrough = recoveryWalkthroughText(unit);
  const morning = recoveryMorningText(unit);
  const route = recoveryRouteText(unit);
  return (
    <div
      key={`${keyPrefix}-${unit.unitId}-advisory`}
      className="home-health-recovery-advisory"
      data-mode={unit.recovery.mode}
      data-verdict={unit.recovery.verdict}
      data-route={unit.recovery.route ?? undefined}
    >
      <p className="home-health-recovery-title">
        {unit.unitId} — recovery ({unit.recovery.mode})
        {unit.recovery.leftArmed ? " — LEFT ARMED: the closing disarm refused, disarm it now" : ""}
      </p>
      {route !== null && <p className="home-health-route">{route}</p>}
      {walkthrough !== null && <p className="home-health-walkthrough">{walkthrough}</p>}
      {morning !== null && <p className="home-health-morning">{morning}</p>}
    </div>
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
  const advisories = health.units
    .map((unit) => unitAdvisory(unit, headingId))
    .filter((node): node is JSX.Element => node !== null);
  const cycled = health.units.some((unit) => unit.recovery.attemptsTotal > 0);
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
      {advisories.length > 0 && advisories}
      {cycled && (
        <p role="note" className="home-health-bmu-note">
          {HEALTH_WATCH_BMU_CROSS_CHECK}
        </p>
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
