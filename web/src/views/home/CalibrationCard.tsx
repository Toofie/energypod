/**
 * The "Battery calibration" Home card (DESIGN_CALIBRATION_CYCLING §9): the
 * maintenance program's compact story BESIDE the health-watch card — a
 * per-unit row (class chip, due date or excluded/deferred reason), the
 * active traverse's live line on cycle nights (target, rate, SoC vs floor,
 * energy vs bound), and the morning-after line the day following a cycle.
 *
 * A14's sibling line, kept on the surface: the two programs share the
 * maintenance row on the console (one visual neighborhood, one alert styling
 * family) and nothing else — this card speaks class, due-date, and
 * cycle-state, never the watch's verdict vocabulary.
 *
 * Feature-detected exactly like the Health Watch card: it renders ONLY when
 * the snapshot carries a `calibration_state` projection (the block-presence
 * doctrine — a site without the `battery_calibration` block sees nothing at
 * all, and the status route answers its own 409).
 *
 * Honesty rules pinned here (the contract's own):
 *
 * - The pinned §0 sentence and the C16 stop-route line ride the card's
 *   traverse state wherever it renders — no new kill switch exists or is
 *   wanted, and the surface says how the operator's existing authority
 *   reaches the act.
 * - `mode: advise` names `submits: never` beside the plan (the
 *   invisible-state rule).
 * - Skips and deferrals render their reason verbatim; a stood-down pod
 *   states the operator decision tree (§6.3).
 * - `horizon_bounded` rides every due figure (C1: an honest lower bound).
 */
import { useId } from "react";
import type { JSX } from "react";
import {
  CALIBRATION_EVENT_NOTE,
  CALIBRATION_PINNED_SENTENCE,
  CALIBRATION_STOP_ROUTE,
  calibrationAlertText,
  calibrationAttributionText,
  calibrationClassText,
  calibrationDueText,
  calibrationStatusText,
  calibrationVerdictText,
  type CalibrationState,
  type CalibrationUnitState,
} from "../../app/calibration";
import { formatWatts } from "../../lib/format";

export interface CalibrationCardProps {
  /**
   * The snapshot's calibration projection; null (or a stale object literal
   * built before the key existed) hides the whole card — the feature
   * detection is the field's own absence.
   */
  calibration: CalibrationState | null | undefined;
}

function unitTier(unit: CalibrationUnitState): "alert" | "notice" | "quiet" {
  if (unit.standdown) {
    return "alert";
  }
  if (unit.due && !unit.evidenceShort) {
    return "notice";
  }
  return "quiet";
}

/** One battery's row: the class chip and the due line (the §3.2 set). */
function unitRow(unit: CalibrationUnitState, keyPrefix: string): JSX.Element {
  const tier = unitTier(unit);
  const due = calibrationDueText(unit);
  return (
    <li
      key={`${keyPrefix}-${unit.unitId}`}
      className="home-cal-unit"
      data-tier={tier}
      aria-label={`${unit.unitId} calibration row`}
    >
      <span className="home-cal-unit-name">{unit.unitId}</span>
      <span className="home-cal-class" data-class={unit.klass}>
        {calibrationClassText(unit)}
      </span>
      {due !== "" && <span className="home-cal-due">{due}</span>}
    </li>
  );
}

/** The live traverse line on cycle nights (§9): target, rate, SoC vs floor,
 * energy vs bound — with the pinned sentence and the C16 stop route. */
function traverseLine(state: CalibrationState, idPrefix: string): JSX.Element | null {
  const traverse = state.traverse;
  if (traverse === null) {
    return null;
  }
  const soc =
    traverse.socPct === null
      ? "not available"
      : `${traverse.socPct.toFixed(1)}% (floor ${traverse.floorPct.toFixed(0)}%)`;
  const energy = `${Math.round(traverse.energyWh)} of ${Math.round(
    traverse.energyBoundWh,
  )} Wh`;
  const rate = traverse.rateW === null ? "" : ` at ${formatWatts(traverse.rateW)}`;
  return (
    <div key={`${idPrefix}-traverse`} className="home-cal-traverse" data-at-risk={traverse.atRisk}>
      <p className="home-cal-traverse-line">
        Traversing {traverse.unitId}
        {rate} — SoC {soc}, delivered {energy}.
      </p>
      <p role="note" className="home-cal-pinned">
        {CALIBRATION_PINNED_SENTENCE}
      </p>
      <p role="note" className="home-cal-stop-route">
        {CALIBRATION_STOP_ROUTE}
      </p>
    </div>
  );
}

export function CalibrationCard({ calibration }: CalibrationCardProps): JSX.Element | null {
  const headingId = useId();
  if (calibration == null) {
    // Feature detection: no calibration_state means the program is not
    // composed on this deployment — nothing renders at all.
    return null;
  }
  const alert = calibrationAlertText(calibration);
  const last = calibration.lastCycle;
  const close = calibration.close;
  const economics = last?.economicsCents ?? null;
  return (
    <section
      className="home-card home-card--calibration"
      aria-labelledby={headingId}
      data-phase={calibration.phase}
      data-mode={calibration.mode}
      data-alert={alert === null ? "false" : "true"}
    >
      <h2 id={headingId}>Battery calibration</h2>
      <p className="home-cal-status">{calibrationStatusText(calibration)}</p>
      {calibration.submits === "never" && (
        <p className="home-cal-submits">Submits: never — this site's program is in its advise posture (a displayed plan that does not act says so beside itself).</p>
      )}
      {alert !== null && (
        <p role="alert" className="home-cal-alert">
          {alert}
        </p>
      )}
      {calibration.units.length > 0 && (
        <ul className="home-cal-units" aria-label="Per-battery calibration rows">
          {calibration.units.map((unit) => unitRow(unit, headingId))}
        </ul>
      )}
      {traverseLine(calibration, headingId)}
      {close !== null && (
        <p className="home-cal-close" data-attribution={close.attribution ?? undefined}>
          {close.taperObserved
            ? `The top anchor landed on ${close.unitId}${close.holdInterrupted ? " — the hold was interrupted by house draw (recorded honestly; the anchor was had at the taper)" : ""}.`
            : close.attribution !== null
              ? `${close.unitId}: ${calibrationAttributionText(close.attribution)}.`
              : `Observing ${close.unitId}'s close until the midday taper deadline.`}
        </p>
      )}
      {last !== null && (
        <p className="home-cal-last" data-verdict={last.verdict} data-tier={last.tier ?? undefined}>
          Last cycle ({last.night}, {last.kind}): {calibrationVerdictText(last.verdict)}
          {last.traceClass !== "" ? ` — SoC word ${last.traceClass}` : ""}
          {last.energyWh === null ? "" : `, ${Math.round(last.energyWh)} Wh delivered`}
          {economics !== null &&
          typeof economics.net_house_absorbed_cents === "number" &&
          typeof economics.net_fully_exported_cents === "number"
            ? ` · ≈ +${economics.net_house_absorbed_cents.toFixed(0)} c if the house absorbed it, ≈ ${economics.net_fully_exported_cents.toFixed(0)} c if fully exported`
            : ""}
          .
        </p>
      )}
      {calibration.requestMeasurement !== null && (
        <p className="home-cal-request">
          Operator measurement request stands for {calibration.requestMeasurement.unit}
          {calibration.requestMeasurement.consumed ? " (consumed — one-shot)" : ""}.
        </p>
      )}
      {last !== null && (
        <p role="note" className="home-cal-event-note">
          {CALIBRATION_EVENT_NOTE}.
        </p>
      )}
    </section>
  );
}
