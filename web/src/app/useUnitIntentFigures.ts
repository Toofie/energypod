/**
 * The live intent's per-unit watt figures — the ONE tracker every view that
 * speaks per-battery numbers consumes (Home's power cards and the Now request
 * card). Extracted from NowView so the two views can never drift apart again.
 *
 * Why these maps exist at all (2026-08-23 live defect): the snapshot's
 * per-unit `requested_power` repeats the intent's FLEET TOTAL once per covered
 * unit (service.py `_requested_power` projects the intent's own figure, never
 * a per-unit split), while `authorized_power` is genuinely per-unit — so a
 * view that compares the two per battery reports "Requested 3,000 W / Allowed
 * 1,000 W / Limited" for a 3 × 1,000 W dispatch the safety system authorized
 * in full. The wire's own per-unit truth lives in exactly three places, and
 * this hook is the single tracker for all of them:
 *
 * 1. the 202 acceptance view's `requested` projection — `watts_by_unit` rides
 *    it when the dispatch used the backend's native per-unit form (rest.py
 *    refuses the scalar and per-unit watt forms together, so "no map" IS the
 *    scalar intent's own shape, never a parse failure);
 * 2. the `intent.accepted` frame's payload (same shape) — a new acceptance
 *    RESETS both maps: the previous request's figures must never survive a
 *    new one, and nothing is authorized for the new request until the
 *    kernel's next decision;
 * 3. every `control_decision` audit summary on the bus (`audit.appended`):
 *    `requested_watts_by_unit` (the intent's own targets) and
 *    `authorized_watts_by_unit` (the decision's per-unit authorized watts —
 *    the only source that names WHICH battery a headroom clamp hit). Other
 *    audit kinds carry no maps and never disturb the live request's.
 *
 * Both maps are cleared the moment the request they describe ends —
 * `intent.expired`, `authorization.revoked` (the runtime publishes it for
 * intent expiry, disarm and generation fences alike), and
 * `emergency_stop.latched` — so an ended request's figures never linger as a
 * ghost. A view that unmounts and remounts starts empty: a fresh page load
 * mid-intent holds no maps until one of the three sources lands again (the
 * kernel audits every cycle, so the next `control_decision` is at most a
 * cycle away), and until then the views label the snapshot's repeated total
 * AS the fleet total — never as a per-battery figure, and never compared
 * per-unit. The structural cold-load gap is the snapshot itself carrying no
 * per-unit maps (backend follow-up; service.py `_requested_power`).
 *
 * Wire parsing is the shared fleet module's (`toIntentFigures` /
 * `toAuditUnitWatts` / `WattsByUnit` in ./fleet); this hook adds no shapes of
 * its own. Its callbacks are stable (setState-only), so stream effects may
 * close over them for the life of a connection.
 */
import { useCallback, useState } from "react";
import { isRecord, toAuditUnitWatts, toIntentFigures, type WattsByUnit } from "./fleet";

export interface UnitIntentFigures {
  /**
   * The live intent's own per-unit targets (`watts_by_unit`). Null while no
   * per-unit intent is known: scalar intents and pre-intent states.
   */
  requestedByUnit: WattsByUnit | null;
  /**
   * The decision's per-unit authorized watts (`authorized_watts_by_unit`).
   * Null until a minted batch has been seen for the live intent.
   */
  authorizedByUnit: WattsByUnit | null;
  /**
   * Apply one event-bus frame's effect on the maps. A no-op for every frame
   * that carries none; `intent.accepted` and `control_decision` audits feed
   * the maps, and the request-ending frames (expiry / revocation / stop)
   * clear them.
   */
  consumeEvent: (frame: unknown) => void;
  /**
   * Adopt a dispatch's 202 acceptance view (the response's `requested`
   * projection): the per-unit figures exist the moment the response lands,
   * before any frame or refetch. A scalar acceptance resets the requested
   * map (the request carries no per-unit form) and drops the authorized map
   * (nothing is authorized for the new request yet). A shape that does not
   * parse changes nothing.
   */
  adoptAcceptance: (requested: unknown) => void;
  /** Drop both maps (the request they describe is over). */
  clear: () => void;
}

export function useUnitIntentFigures(): UnitIntentFigures {
  const [requestedByUnit, setRequestedByUnit] = useState<WattsByUnit | null>(null);
  const [authorizedByUnit, setAuthorizedByUnit] = useState<WattsByUnit | null>(null);

  const consumeEvent = useCallback((frame: unknown): void => {
    if (!isRecord(frame)) {
      return;
    }
    const type = typeof frame.type === "string" ? frame.type : "";
    const payload = isRecord(frame.payload) ? frame.payload : null;
    if (type === "intent.accepted") {
      // The newest intent's own figures. The wire's acceptance always carries
      // `direction` and `watts`; a frame without them is not an acceptance
      // this tracker can trust, so the maps stay as they are.
      if (payload === null) {
        return;
      }
      if (typeof payload.direction !== "string" || typeof payload.watts !== "number") {
        return;
      }
      const figures = toIntentFigures(payload);
      // Scalar intents carry no map: the reset to null IS their fact, not a
      // "keep whatever was there".
      setRequestedByUnit(figures?.wattsByUnit ?? null);
      setAuthorizedByUnit(null);
    } else if (type === "audit.appended") {
      // Only the kernel's per-tick decision summaries carry the maps; other
      // audit kinds never disturb the live request's figures.
      if (payload !== null && payload.event_type === "control_decision") {
        const unitWatts = toAuditUnitWatts(payload);
        if (unitWatts.requested !== null) {
          setRequestedByUnit(unitWatts.requested);
        }
        if (unitWatts.authorized !== null) {
          setAuthorizedByUnit(unitWatts.authorized);
        }
      }
    } else if (
      type === "intent.expired" ||
      type === "authorization.revoked" ||
      type === "emergency_stop.latched"
    ) {
      // The request ended outright: its per-unit figures must not linger as
      // a ghost of an intent that no longer exists.
      setRequestedByUnit(null);
      setAuthorizedByUnit(null);
    }
  }, []);

  const adoptAcceptance = useCallback((requested: unknown): void => {
    const figures = toIntentFigures(requested);
    if (figures === null) {
      return;
    }
    setRequestedByUnit(figures.wattsByUnit);
    setAuthorizedByUnit(null);
  }, []);

  const clear = useCallback((): void => {
    setRequestedByUnit(null);
    setAuthorizedByUnit(null);
  }, []);

  return { requestedByUnit, authorizedByUnit, consumeEvent, adoptAcceptance, clear };
}
