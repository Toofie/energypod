/**
 * The "Evening load sharing" Home card (DESIGN_EVENING_LOAD_SHARING §9): the
 * netted-meter fleet discharge program's compact story BESIDE the
 * calibration and health-watch cards — a live line while engaged (work, the
 * netted exchange, the per-pod shares with SoC), the idle reason while not,
 * and the morning-after close line the day following — which also lands on
 * the History console's morning-facts surface.
 *
 * The A14 sibling line, kept on the surface: this program's vocabulary
 * (work, split, weights, import) matches no existing card's (verdicts,
 * targets, classes), so it stands alone exactly like the calibration card.
 *
 * Feature-detected exactly like its siblings: it renders ONLY when the
 * snapshot carries an `evening_load_share_state` projection (the
 * block-presence doctrine — a site without the `evening_load_sharing` block
 * sees nothing at all, and the status route answers its own 409).
 *
 * Honesty rules pinned here (the contract's own):
 *
 * - The pinned §0 sentence and the C16 stop-route line ride the card's
 *   engaged state wherever it renders — the operator's existing authority (a
 *   manual claim preempts instantly) is the stop route; no new kill switch
 *   exists or is wanted.
 * - `mode: advise` names `submits: never` beside the plan (the
 *   invisible-state rule).
 * - Skips render their reason verbatim; `capability_limited` renders the
 *   residual import it stands behind.
 */
import { useId } from "react";
import type { JSX } from "react";
import {
  EVENING_PINNED_SENTENCE,
  EVENING_STOP_ROUTE,
  eveningAlertText,
  eveningCloseText,
  eveningLiveLine,
  eveningReasonText,
  eveningStatusText,
  type EveningShareState,
  type EveningUnitState,
} from "../../app/eveningShare";
import { formatWatts } from "../../lib/format";

export interface EveningShareCardProps {
  /**
   * The snapshot's evening projection; null (or a stale object literal built
   * before the key existed) hides the whole card — the feature detection is
   * the field's own absence.
   */
  evening: EveningShareState | null | undefined;
}

function unitTier(unit: EveningUnitState): "alert" | "notice" | "quiet" {
  if (unit.reason === "not_delivering" || unit.reason === "soc_floor") {
    return "notice";
  }
  return unit.phase === "sharing" ? "quiet" : "quiet";
}

/** One pod's row: the share with its SoC, or its reason verbatim. */
function unitRow(unit: EveningUnitState, keyPrefix: string): JSX.Element {
  const tier = unitTier(unit);
  const sharing = unit.phase === "sharing";
  const soc = unit.socPct === null ? "" : ` · SoC ${unit.socPct.toFixed(0)}%`;
  return (
    <li
      key={`${keyPrefix}-${unit.unitId}`}
      className="home-els-unit"
      data-tier={tier}
      aria-label={`${unit.unitId} evening share row`}
    >
      <span className="home-els-unit-name">{unit.unitId}</span>
      <span className="home-els-share" data-phase={unit.phase}>
        {sharing
          ? `${formatWatts(unit.shareW)}${soc}`
          : eveningReasonText(unit.reason)}
      </span>
      {unit.note !== null && <span className="home-els-note">{unit.note}</span>}
    </li>
  );
}

export function EveningShareCard({ evening }: EveningShareCardProps): JSX.Element | null {
  const headingId = useId();
  if (evening == null) {
    // Feature detection: no evening_load_share_state means the program is
    // not composed on this deployment — nothing renders at all.
    return null;
  }
  const alert = eveningAlertText(evening);
  const live = eveningLiveLine(evening);
  const close = evening.lastClose;
  return (
    <section
      className="home-card home-card--evening"
      aria-labelledby={headingId}
      data-phase={evening.phase}
      data-mode={evening.mode}
      data-alert={alert === null ? "false" : "true"}
    >
      <h2 id={headingId}>Evening load sharing</h2>
      <p className="home-els-status">{eveningStatusText(evening)}</p>
      {evening.submits === "never" && (
        <p className="home-els-submits">Submits: never — this site's program is in its advise posture (a displayed plan that does not act says so beside itself).</p>
      )}
      {alert !== null && (
        <p role="alert" className="home-els-alert">
          {alert}
        </p>
      )}
      {live !== "" && (
        <p className="home-els-live" data-within-tolerance={evening.withinTolerance}>
          {live}
          {evening.commandedTotalW !== null
            ? ` · commanded ${formatWatts(evening.commandedTotalW)} (÷ ${evening.derate.toFixed(2)})`
            : ""}
        </p>
      )}
      {evening.units.length > 0 && (
        <ul className="home-els-units" aria-label="Per-battery evening shares">
          {evening.units.map((unit) => unitRow(unit, headingId))}
        </ul>
      )}
      {evening.phase === "capability_limited" && evening.residualImportW !== null && (
        <p className="home-els-residual">
          The grid covers the remaining {formatWatts(evening.residualImportW)} — the
          fleet never blocks, delays, or prices the grid's remainder.
        </p>
      )}
      {evening.engaged && (
        <div className="home-els-engaged-notes">
          <p role="note" className="home-els-pinned">
            {EVENING_PINNED_SENTENCE}
          </p>
          <p role="note" className="home-els-stop-route">
            {EVENING_STOP_ROUTE}
          </p>
        </div>
      )}
      {close !== null && (
        <p className="home-els-last" data-night={close.night}>
          Last evening ({close.night}): {close.closeSentence ?? eveningCloseText(close)}
        </p>
      )}
    </section>
  );
}
