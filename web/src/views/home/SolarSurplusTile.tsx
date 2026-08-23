/**
 * The "Solar surplus" Home tile — the excess-solar activation feature's whole
 * story in one glance (DESIGN_EXCESS_ACTIVATION.md §5 W-A).
 *
 * Feature-detected, exactly like the snapshot's `intent` block: the tile
 * renders ONLY when the snapshot carries an `adviser_state` projection. An
 * absent projection (today's backend; a deployment with no `excess_charging`
 * config block) renders NOTHING — no heading, no rows, no empty state.
 *
 * Honesty rules pinned here:
 *
 * - ACTIVE names its three facts: the target battery, the commanded watts,
 *   and the fleet's export reading ("Charging mid at 1,400 W from 1,800 W
 *   export"). A null `fleet_export_w` (any unit's grid evidence failed) says
 *   "not available" — one unreadable phase is never treated as zero export.
 * - INACTIVE states are honest one-sentence answers rendered from the FIRST
 *   reason code in the projection's pinned vocabulary (§1's table; the codes
 *   are the wire, the sentences are the console's). An unknown code is named
 *   verbatim, never silently dropped.
 * - The cap line renders the composed `charge_cap_w` as the bare fact it is
 *   ("cap 500 W" for the trial — no invented label).
 * - Per-phase figures come from the snapshot's per-unit telemetry readthrough
 *   (`grid_power_w` / `load_power_w`): one row per unit, negative grid =
 *   import, positive = export, absent = "not available" — the sentence that
 *   makes the feature legible ("that export is what charged rhs").
 */
import { useId } from "react";
import type { JSX } from "react";
import { gridLoadText, type AdviserState } from "../../app/fleet";
import { formatWatts } from "../../lib/format";

/** One unit's readthrough figures, already narrowed by the view's own parse. */
export interface SolarSurplusUnitReadings {
  unitId: string;
  gridPowerW: number | null;
  loadPowerW: number | null;
}

export interface SolarSurplusTileProps {
  /** The snapshot's adviser projection; null hides the whole tile. */
  adviser: AdviserState | null;
  /** Every fleet unit's grid/load readthrough (one row each, any order). */
  units: readonly SolarSurplusUnitReadings[];
}

/**
 * The plain-language sentence for one adviser reason code — the design
 * contract §5 W-A's own wording, keyed on the pinned §1 vocabulary. The
 * builder takes the state so the sentence can carry its own figures.
 */
function reasonSentence(code: string, state: AdviserState): string {
  switch (code) {
    case "disabled_by_config":
      return "Charging from solar surplus is off (config).";
    case "disabled_by_runtime":
      // The projection carries no boot-value flag, so the console states the
      // fact it CAN see: this off came from the runtime toggle, and a restart
      // re-reads the commissioned config.
      return "Charging from solar surplus is off until the controller restarts — the config's own setting takes over again at boot.";
    case "economics_acknowledgement_required":
      return "Waiting on the one-time net-billing confirmation before solar-surplus charging can start.";
    case "export_evidence_missing":
    case "export_evidence_bad":
    case "export_evidence_stale":
      // fleet_export_w is null by contract under every one of these codes.
      return "Export reading unavailable on the fleet — standing down (fail-closed). Export figure: not available.";
    case "no_export_headroom":
      return `Exporting ${
        state.fleetExportW === null ? "an unavailable figure" : formatWatts(state.fleetExportW)
      } — below the headroom margin, nothing to charge from.`;
    case "no_acceleration_over_autonomy":
      return "Surplus too small — taking over would charge slower than the pod does by itself.";
    case "below_exit_hysteresis":
      return "Surplus is falling — handing back to the pod's own charging.";
    case "no_eligible_target":
      return "Solar surplus available, but no battery needs charging (full, inhibited, or not armed).";
    case "yielding_to_higher_priority":
      return `Standing down — a manual request has ${
        state.targetUnitId ?? "the target battery"
      }.`;
    case "export_headroom_available":
      // The commanding code; an inactive projection carrying it still speaks
      // the participation truth.
      return "Charging from solar surplus.";
    default:
      // The vocabulary is pinned and one list (ADVISER_REASON_CODES); an
      // unknown code is rendered honestly instead of guessed at.
      return `Standing down from solar surplus (${code}).`;
  }
}

/**
 * The tile's one-line story: active names its figures; inactive names its
 * first reason code's sentence (an enabled adviser always says why it did
 * what it did — an empty list is never the wire's answer).
 */
export function adviserStatusText(state: AdviserState): string {
  if (state.active) {
    const target = state.targetUnitId ?? "the neediest battery";
    const exportFigure =
      state.fleetExportW === null
        ? "an export figure that is not available"
        : `${formatWatts(state.fleetExportW)} export`;
    return `Charging ${target} at ${formatWatts(state.commandedChargeW)} from ${exportFigure}.`;
  }
  const first = state.reasonCodes[0];
  return first === undefined
    ? "Charging from solar surplus is standing by."
    : reasonSentence(first, state);
}

/**
 * The secondary figure line: the composed cap, rendered as the bare fact (the
 * trial shows "cap 500 W" with no invented label). Null when the adviser is
 * not commanding — the cap belongs to the active sentence.
 */
export function adviserCapText(state: AdviserState): string | null {
  if (!state.active) {
    return null;
  }
  return `cap ${formatWatts(state.chargeCapW)}`;
}

export function SolarSurplusTile({ adviser, units }: SolarSurplusTileProps): JSX.Element | null {
  const headingId = useId();
  if (adviser === null) {
    // Feature detection: no adviser_state anywhere means the feature is not
    // composed on this deployment — nothing renders at all.
    return null;
  }
  return (
    <section className="home-card home-card--solar" aria-labelledby={headingId}>
      <h2 id={headingId}>Solar surplus</h2>
      <p className="home-solar-status" data-active={adviser.active ? "true" : "false"}>
        {adviserStatusText(adviser)}
      </p>
      {adviserCapText(adviser) !== null && (
        <p className="home-solar-cap">{adviserCapText(adviser)}</p>
      )}
      {units.length > 0 && (
        <ul className="home-solar-units" aria-label="Per-unit grid and load">
          {units.map((unit) => (
            <li key={unit.unitId} aria-label={`${unit.unitId} grid and load`}>
              {unit.unitId}: {gridLoadText(unit.gridPowerW, unit.loadPowerW)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
