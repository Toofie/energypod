/**
 * Home's fleet banner while any pod is parked (DESIGN_POD_PARKING §8): the
 * household-facing fact that a battery is standing by under a lease — named
 * per pod, honest about what parking is NOT (the fixed not-isolation sentence
 * rides the banner; the countdown is policy, never safety), and promoting to
 * the alert wording the moment a lease reads expired. Renders nothing at all
 * while no pod is parked or while the parking projection does not speak (the
 * not-commissioned feature detection).
 */
import type { JSX } from "react";
import {
  hmmText,
  leaseIsExpired,
  leaseSecondsRemaining,
  NOT_ISOLATION_SENTENCE,
} from "../../app/park";
import type { ParkStateView } from "../../app/park";

export interface ParkedUnitLine {
  unitId: string;
  park: ParkStateView;
}

export function ParkedFleetBanner({
  parked,
  nowMs,
}: {
  parked: readonly ParkedUnitLine[];
  nowMs: number;
}): JSX.Element | null {
  const standing = parked.filter((entry) => entry.park.parked);
  if (standing.length === 0) {
    return null;
  }
  const expired = standing.some((entry) => leaseIsExpired(entry.park, nowMs));
  const names = standing
    .map((entry) => {
      if (leaseIsExpired(entry.park, nowMs)) {
        return `${entry.unitId} — lease expired, Resume required`;
      }
      const remaining = leaseSecondsRemaining(entry.park, nowMs);
      const countdown = remaining === null ? "" : ` (lease expires in ${hmmText(remaining)})`;
      return `${entry.unitId}${countdown}`;
    })
    .join(" · ");
  return (
    <section
      aria-label="Parked batteries"
      className={expired ? "home-parked home-parked--expired" : "home-parked"}
    >
      <p className="home-parked-line">
        Parked — not isolation: {names}. A parked battery stands by at zero watts; resuming is the
        operator&apos;s act.
      </p>
      <p className="home-parked-fixed">{NOT_ISOLATION_SENTENCE}</p>
    </section>
  );
}
