/**
 * Home's "Today" card — the energy scorecard's one-glance answer
 * (DESIGN_ENERGY_SCORECARD.md §1 + §8 W-A): the day's energy account so far,
 * placed directly after "What is powering the home?".
 *
 * Feature-detected exactly like the schedule card and the solar-surplus tile:
 * the card renders ONLY when the snapshot carries an `energy_today` block. An
 * absent block (today's backend; a deployment with no `energy_scorecard`
 * config) renders NOTHING — no heading, no empty state, no footnote.
 *
 * Honesty rules pinned here:
 *
 * - Every figure is the record's own, from ITS pinned source (§2), and each
 *   family's provenance is NAMED on the card: grid bought/sold are "measured
 *   by the controller" while the source is the CT integration (the pods' two
 *   grid counters display as counter A / counter B until their roles are
 *   pinned — never guessed bought/sold from), battery and house-load figures
 *   are "recorded by the pods' own energy counters".
 * - A null figure renders "not available" — never 0, never hidden.
 * - The day-kind marker is always present: an in-progress day says "so far
 *   today", a partial day names the threshold breach, and the day's coverage
 *   fraction rides the marker (§1 rule 1).
 * - The charged-from-surplus line ties into the live `adviser_state` (the
 *   excess-solar tile's own projection): a figure renders the design's
 *   sentence, and an absent figure is explained by the adviser's actual state
 *   — enabled-but-quiet, switched off, or not composed — never fabricated.
 * - SOLAR IS NEVER A MEASUREMENT here: the card carries no solar-production
 *   line at all, and the footnote says exactly why, once (§1 rule 3).
 * - Money (the design's optional tariff line) is deliberately absent: no
 *   pinned wire shape carries the commissioned tariff (neither the
 *   `energy_today` record nor the days route includes it), so the console
 *   cannot render it without inventing the operator's rates. Reported as a
 *   contract gap; nothing is improvised around it.
 */
import { useId } from "react";
import type { JSX } from "react";
import {
  SOLAR_FOOTNOTE,
  coverageText,
  dayMarkerText,
  gridProvenanceNote,
  kwhText,
  metricFlagText,
  sourceProvenanceText,
  type EnergyToday,
  type EnergyUnitDay,
} from "../../app/energy";
import type { AdviserState } from "../../app/fleet";
import { localTimeOfInstant } from "../../app/schedule";

/** The per-unit row's figures, in one plain sentence (nulls named, never 0). */
function unitRowText(unitId: string, unit: EnergyUnitDay): string {
  const parts = [
    `bought ${kwhText(unit.gridImportKwh)}`,
    `sold ${kwhText(unit.gridExportKwh)}`,
    `charged ${kwhText(unit.batteryChargedKwh)}`,
    `discharged ${kwhText(unit.batteryDischargedKwh)}`,
    `house load ${kwhText(unit.loadKwh)}`,
    `charged from surplus ${kwhText(unit.chargedFromSurplusKwh)}`,
  ];
  const coverage = unit.coveragePct === null ? "" : `, ${coverageText(unit.coveragePct)} coverage`;
  const resets = unit.metricFlags.length === 0
    ? ""
    : ` (${unit.metricFlags.map(metricFlagText).join("; ")})`;
  return `${unitId} — ${parts.join(" · ")}${coverage}${resets}`;
}

/**
 * The surplus line, tied to the live adviser projection: the record's figure
 * when the day has one (a real 0 included — an active adviser that moved
 * nothing is an honest 0), and an explanation phrased by the adviser's actual
 * state when it does not.
 */
function surplusLine(today: EnergyToday, adviser: AdviserState | null): string {
  const figure = today.fleet.chargedFromSurplusKwh;
  if (figure !== null) {
    return `Solar-surplus charging moved ${kwhText(figure)} that would have been exported.`;
  }
  if (adviser === null) {
    return "Charged from solar surplus: not available.";
  }
  if (!adviser.enabled) {
    return "Solar-surplus charging is switched off, so no surplus energy was captured today.";
  }
  return "Solar-surplus charging is enabled but has not captured energy yet today.";
}

export interface TodayCardProps {
  /** The snapshot's `energy_today` block; null hides the whole card. */
  today: EnergyToday | null;
  /** The snapshot's adviser projection (may itself be null — feature-detected). */
  adviser: AdviserState | null;
}

export function TodayCard({ today, adviser }: TodayCardProps): JSX.Element | null {
  const headingId = useId();
  const detailId = useId();
  if (today === null) {
    // Feature detection: no `energy_today` means the scorecard is not
    // composed on this deployment — nothing renders at all.
    return null;
  }
  const fleet = today.fleet;
  const asOfClock = localTimeOfInstant(today.asOf);
  const marker = dayMarkerText(today.kind, fleet.coveragePct);
  const unitIds = Object.keys(today.units);
  return (
    <section className="home-card home-card--energy" aria-labelledby={headingId}>
      <h2 id={headingId}>Today (so far)</h2>
      <p className="home-energy-line">
        Bought from grid <strong>{kwhText(fleet.gridImportKwh)}</strong> · Sold to grid{" "}
        <strong>{kwhText(fleet.gridExportKwh)}</strong>
      </p>
      <p className="home-energy-line">
        Charged <strong>{kwhText(fleet.batteryChargedKwh)}</strong> · Discharged{" "}
        <strong>{kwhText(fleet.batteryDischargedKwh)}</strong> · House load{" "}
        <strong>{kwhText(fleet.loadKwh)}</strong>
      </p>
      <p className="home-energy-line home-energy-line--surplus">{surplusLine(today, adviser)}</p>
      <p className="home-energy-marker" data-partial={today.kind === "partial" ? "true" : undefined}>
        {marker}
        {asOfClock === "" ? "" : `, figures at ${asOfClock}`}
        {today.timezone === "" ? "" : ` (${today.timezone})`}.
      </p>
      <p className="home-energy-provenance">
        Grid figures {sourceProvenanceText(today.sources.grid)}; battery and house-load figures{" "}
        {sourceProvenanceText(today.sources.battery)}. {gridProvenanceNote(today.sources.grid)}
      </p>
      {unitIds.length > 0 && (
        <details className="home-energy-units">
          <summary id={detailId}>Per-battery figures for today</summary>
          <ul aria-labelledby={detailId}>
            {unitIds.map((unitId) => (
              <li key={unitId}>{unitRowText(unitId, today.units[unitId]!)}</li>
            ))}
          </ul>
        </details>
      )}
      <p className="home-energy-footnote">{SOLAR_FOOTNOTE}</p>
    </section>
  );
}
