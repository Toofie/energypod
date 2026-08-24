/**
 * The self-healing awareness layer's operator wording and one shared badge
 * (API_CONTRACTS.md "Self-healing awareness layer (recovery detection)",
 * energypod.application.recovery).
 *
 * The console's whole job here is honest visibility into self-healing and its
 * FAILURE, in plain language, with the design's own quietness rules:
 *
 * - SILENCE IS THE DESIGN for a healthy unit — no tag, no line, nothing. A
 *   healthy battery is the unremarkable case and must not add noise.
 * - `self_healing` is quiet and positive: the battery is managing itself
 *   (solar self-charge, re-qualification after an inhibit, cell balancing)
 *   and needs nothing from the operator.
 * - `actuation_incoherent` is a warning with the plain story: we commanded
 *   power and the battery is not moving.
 * - `not_responding` is the prominent honest terminal: remote recovery is
 *   exhausted, and the backend's `remediation_hint` renders VERBATIM (the
 *   wedge-class physical-restart checklist — re-wording it would be lying).
 * - `unreachable` names the gateway class (the pod behind it may be fine).
 * - `foreign_writer` and `inhibited` render NOTHING here: the existing
 *   surfaces (the inhibit latch line and its acknowledge control on
 *   Batteries, the Inhibited badge on Home, the shell's latch announcements)
 *   already tell those stories, and duplicating them would be noise.
 * - `parked` (DESIGN_POD_PARKING §3) renders NOTHING by itself — the parked
 *   chip, the not-isolation banner, and Home's fleet banner own that story —
 *   but it is the COMPOSABLE state: the badge still renders the healing
 *   reasons the classifier carried ("parked", *healing), so a parked pod that
 *   is balancing keeps its balancing visibility (the design's own pin).
 *
 * The same module owns the incoherence-alarm phrasings the shell announces
 * (one polite line per episode) so the badge, the announcement, and the
 * discriminator wording can never drift apart.
 */
import type { JSX } from "react";
import { formatWatts } from "../lib/format";
import type { UnitHealth } from "./fleet";

/**
 * The quiet-positive wording for a self-healing battery, keyed by the
 * backend's own reason codes (recovery.py `_derive`). The first recognized
 * reason decides; an unknown reason still earns the honest generic line —
 * never silence, never a raw code. `autonomous_self_charge` is the one code
 * that also reads the battery's own measured figure: config rev 5 widened
 * the uncommanded band to the pods' evening behavior, where mid/rhs gently
 * self-charge while lhs load-serves, so the sentence follows the direction
 * the battery itself reports.
 */
const SELF_HEALING_WORDS: Record<string, string> = {
  requalifying_after_inhibit: "Re-qualifying",
  cell_balancing: "Cell balancing",
};

/** The generic quiet line when the reasons carry no recognized code. */
const SELF_HEALING_GENERIC = "Managing itself";

/**
 * Wire convention (live-proven): a POSITIVE measured watt figure is DISCHARGE
 * — the battery powering the home — negative is CHARGE. A figure inside the
 * deadband carries no direction worth naming, and no figure at all keeps
 * today's sentence: the tag never guesses a direction.
 */
const SELF_CHARGE_DEADBAND_W = 10;

export function selfHealingText(health: UnitHealth, measuredWatts: number | null = null): string {
  for (const reason of health.reasons) {
    if (reason === "autonomous_self_charge") {
      return measuredWatts !== null && measuredWatts >= SELF_CHARGE_DEADBAND_W
        ? "Managing itself — powering the home"
        : "Managing itself — solar self-charge";
    }
    const words = SELF_HEALING_WORDS[reason];
    if (words !== undefined) {
      return words;
    }
  }
  return SELF_HEALING_GENERIC;
}

/**
 * The plain-language story for an actuation-incoherent battery. The commanded
 * figure is the battery's OWN authorized watts when the caller has it; the
 * story never fabricates a number.
 */
export function incoherenceStory(authorizedWatts: number | null): string {
  return authorizedWatts !== null
    ? `Commanded ${formatWatts(authorizedWatts)} but the battery isn't moving — investigating`
    : "The battery isn't moving as commanded — investigating";
}

/**
 * The polite-region alarm for one incoherence EPISODE (the shell announces it
 * exactly once per episode; the backend publishes the detection once and
 * throttles until a coherent cycle re-arms it).
 */
export function incoherenceAnnouncement(unitId: string, authorizedWatts: number | null): string {
  return authorizedWatts !== null
    ? `${unitId} was commanded ${formatWatts(authorizedWatts)} but isn't responding to control — check the battery.`
    : `${unitId} was commanded power but isn't responding to control — check the battery.`;
}

/**
 * The discriminator the objective echo read-back names, in plain words
 * (recovery.py's four classifications). The echo follow-up frame carries the
 * classification; an unknown classification is named honestly, never guessed.
 */
export function echoDiscriminatorText(unitId: string, classification: string): string {
  switch (classification) {
    case "echo_matches_write":
      return `${unitId}: the battery confirms receiving our command — the transport is fine, the fault is inside the battery; remote recovery is exhausted.`;
    case "external_writer":
      return `${unitId}: another writer is present — something else is commanding this battery.`;
    case "objective_not_served":
      return `${unitId}: the battery is not serving any command — its own mode may be blocking dispatch (check Debug Mode and SysControlMode in the vendor app).`;
    case "echo_unreadable":
      return `${unitId}: the battery's command readback could not be read — still investigating.`;
    default:
      return `${unitId}: the command readback reported "${classification}" — still investigating.`;
  }
}

/** The tag's visual weight: quiet, warning, or the prominent terminal. */
type HealthTagKind = "healing" | "warning" | "critical";

interface UnitHealthTagProps {
  health: UnitHealth | null;
  /**
   * The battery's OWN authorized watts, for the incoherence story's commanded
   * figure; null when no authorization is on screen.
   */
  authorizedWatts?: number | null;
  /**
   * The battery's OWN measured watts, for the self-healing line's direction
   * (positive = powering the home, negative = solar self-charge). Null or
   * absent keeps the generic sentence — the tag never guesses a direction.
   */
  measuredWatts?: number | null;
}

/**
 * The per-unit recovery badge shared by the Batteries cards and Home's
 * per-unit power entries. Renders NOTHING for a feature-absent health (null),
 * a healthy unit (silence is the design), and the two states whose stories
 * the existing inhibit surfaces already tell (foreign_writer, inhibited).
 */
export function UnitHealthTag({
  health,
  authorizedWatts = null,
  measuredWatts = null,
}: UnitHealthTagProps): JSX.Element | null {
  if (health === null) {
    return null;
  }
  let kind: HealthTagKind;
  let text: string;
  switch (health.state) {
    case "healthy":
      return null;
    case "foreign_writer":
    case "inhibited":
      // Already spoken for by the inhibit latch surfaces; duplicating them
      // here would be noise, never clarity.
      return null;
    case "parked":
      // The parked surfaces (chip, banner, Home's fleet banner) tell the
      // park; this badge renders only the composable healing reasons the
      // classifier carried alongside it. No healing reasons: silence.
      if (health.reasons.length === 0) {
        return null;
      }
      kind = "healing";
      text = selfHealingText(health, measuredWatts);
      break;
    case "self_healing":
      kind = "healing";
      text = selfHealingText(health, measuredWatts);
      break;
    case "actuation_incoherent":
      kind = "warning";
      text = incoherenceStory(authorizedWatts);
      break;
    case "not_responding":
      kind = "critical";
      text = "Not responding — remote recovery exhausted";
      break;
    case "unreachable":
      kind = "critical";
      text = "Gateway unreachable";
      break;
  }
  return (
    <p role="note" className={`unit-health unit-health--${kind}`}>
      {text}
      {health.state === "not_responding" && health.remediationHint !== null && (
        // Verbatim, always: this is the backend's honest terminal guidance.
        <span className="unit-health-hint">{health.remediationHint}</span>
      )}
    </p>
  );
}
