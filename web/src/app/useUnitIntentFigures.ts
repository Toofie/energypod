/**
 * The live request's per-unit figures — the ONE tracker every view that speaks
 * per-battery numbers consumes (Home's power cards, Now's request cards, the
 * shell's fleet banner, and the Batteries figures). Extracted from NowView so
 * the surfaces can never drift apart again.
 *
 * Why these maps exist at all (2026-08-23 live defect): the snapshot's
 * per-unit `requested_power` repeats the intent's FLEET TOTAL once per covered
 * unit (service.py `_requested_power` projects the intent's own figure, never
 * a per-unit split), while `authorized_power` is genuinely per-unit — so a
 * view that compares the two per battery reports "Requested 3,000 W / Allowed
 * 1,000 W / Limited" for a 3 × 1,000 W dispatch the safety system authorized
 * in full. The wire's own per-unit truth lives in these maps:
 *
 * 1. the 202 acceptance view's `requested` projection — `watts_by_unit` rides
 *    it when the dispatch used the backend's native per-unit form (rest.py
 *    refuses the scalar and per-unit watt forms together, so "no map" IS the
 *    scalar intent's own shape, never a parse failure);
 * 2. the `intent.accepted` frame's payload (same shape) — the accepted
 *    intent's own figures take over ITS units (a newer intent's targets
 *    replace whatever an older one left on those units) and nothing is
 *    authorized for it yet;
 * 3. every `control_decision` audit summary on the bus (`audit.appended`):
 *    `requested_watts_by_unit` (each unit's WINNING intent's own target, over
 *    its surviving scope) and `authorized_watts_by_unit` (the decision's
 *    per-unit authorized watts — the only source that names WHICH battery a
 *    headroom clamp hit), plus `directions_by_unit` (a concurrent cycle may
 *    run opposite directions on different units). Other audit kinds carry no
 *    maps and never disturb the live request's.
 * 3b. every `authorization.granted` announcement (live on the bus): the
 *    granted batch's per-unit watts (`watts_by_unit`) and directions
 *    (`directions_by_unit`) — the freshest authorized figure, landing at the
 *    moment of the grant itself.
 *
 * CONCURRENT REQUESTS (2026-08-24 backend contract): several intents coexist,
 * each driving its own batteries. The maps are therefore CYCLE-LEVEL — they
 * hold every represented intent's surviving per-unit entries at once — and a
 * consumer attributes an entry to one request by UNIT MEMBERSHIP (the audit
 * rows correlate to their cycle via `correlation_id: "cycle:..."`, never to a
 * single intent id). The map-level rules that keep them honest under
 * concurrency:
 * - an acceptance only touches ITS units (upsert its targets there, drop
 *   whatever an older intent claimed on them, and drop their authorized
 *   entries) — another intent's figures on other units survive;
 * - a cycle's decision maps replace wholesale (they are the full current
 *   truth across every represented intent);
 * - a request ending (`intent.expired`, `intent.cancelled`) or an authority
 *   ending (`authorization.revoked`) drops exactly the frame's `unit_ids`
 *   entries — another intent may still hold other units, and the kernel's
 *   next decision re-seeds anything it still grants; `emergency_stop.latched`
 *   clears everything (the fence ends every request at once).
 *
 * Both maps are seeded for COLD LOAD by the snapshot's `intent` block when
 * the backend sends one (`adoptSnapshot`): an absent field (today's wire) is
 * the feature detection and changes nothing, a `null` block means "no active
 * request" and clears, and a present block seeds the exact per-unit figures so
 * a fresh page load mid-intent never has to wait for the next decision. A
 * view that unmounts and remounts starts from the snapshot's block and the
 * next frame; until either lands, the views label the snapshot's repeated
 * total AS the fleet total — never as a per-battery figure, and never compared
 * per-unit.
 *
 * Wire parsing is the shared fleet module's (`toIntentFigures` /
 * `toAuditUnitWatts` / `toSnapshotIntentFigures` / `WattsByUnit` /
 * `DirectionsByUnit` in ./fleet); this hook adds no shapes of its own. Its
 * callbacks are stable (setState-only), so stream effects may close over them
 * for the life of a connection.
 */
import { useCallback, useState } from "react";
import {
  isRecord,
  toAuditUnitWatts,
  toDirectionsByUnit,
  toIntentFigures,
  toSnapshotIntentFigures,
  toWattsByUnit,
  type DirectionsByUnit,
  type WattsByUnit,
} from "./fleet";

export interface UnitIntentFigures {
  /**
   * The live request's per-unit targets (`watts_by_unit`, cycle-level across
   * concurrent intents). Null while no per-unit intent is known: scalar
   * intents and pre-intent states.
   */
  requestedByUnit: WattsByUnit | null;
  /**
   * The decision's per-unit authorized watts (`authorized_watts_by_unit`).
   * Null until a minted batch has been seen for the live request.
   */
  authorizedByUnit: WattsByUnit | null;
  /**
   * Each unit's winning direction (`directions_by_unit` on a composed
   * cycle's decision summary and the snapshot `intent` block): a concurrent
   * cycle may carry opposite directions on different units. Null when no
   * direction map has been seen.
   */
  directionsByUnit: DirectionsByUnit | null;
  /**
   * Apply one event-bus frame's effect on the maps. A no-op for every frame
   * that carries none; `intent.accepted` and `control_decision` audits feed
   * the maps, and the request-ending frames (expiry / cancel / revocation /
   * stop) clear their units' entries.
   */
  consumeEvent: (frame: unknown) => void;
  /**
   * Adopt a dispatch's 202 acceptance view (the response's `requested`
   * projection) for the units the dispatch selected: the per-unit figures
   * exist the moment the response lands, before any frame or refetch. A
   * scalar acceptance drops those units' requested entries (the request
   * carries no per-unit form) and their authorized entries (nothing is
   * authorized for the new request yet). A shape that does not parse changes
   * nothing.
   */
  adoptAcceptance: (requested: unknown, unitIds?: readonly string[]) => void;
  /**
   * Adopt a raw snapshot envelope's `intent` block (cold-load exactness,
   * feature-detected): an absent field changes nothing (today's wire), a
   * `null` block clears the maps (no active request), and a present block
   * seeds the per-unit figures the snapshot itself carries.
   */
  adoptSnapshot: (rawSnapshot: unknown) => void;
  /** Drop all three maps (the request they describe is over). */
  clear: () => void;
}

/** Drop `unitIds`' entries from a map; null stays null, an empty result stays. */
function withoutUnits<T extends Record<string, unknown>>(map: T | null, unitIds: string[]): T | null {
  if (map === null || unitIds.length === 0) {
    return map;
  }
  const drop = new Set(unitIds);
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(map)) {
    if (!drop.has(key)) {
      next[key] = value;
    }
  }
  return Object.keys(next).length > 0 ? (next as T) : null;
}

export function useUnitIntentFigures(): UnitIntentFigures {
  const [requestedByUnit, setRequestedByUnit] = useState<WattsByUnit | null>(null);
  const [authorizedByUnit, setAuthorizedByUnit] = useState<WattsByUnit | null>(null);
  const [directionsByUnit, setDirectionsByUnit] = useState<DirectionsByUnit | null>(null);

  const consumeEvent = useCallback((frame: unknown): void => {
    if (!isRecord(frame)) {
      return;
    }
    const type = typeof frame.type === "string" ? frame.type : "";
    const payload = isRecord(frame.payload) ? frame.payload : null;
    if (type === "intent.accepted") {
      // The newest intent's own figures, scoped to ITS units: another
      // concurrent intent's entries on other units survive. The wire's
      // acceptance always carries `direction` and `watts`; a frame without
      // them is not an acceptance this tracker can trust, so the maps stay.
      if (payload === null) {
        return;
      }
      if (typeof payload.direction !== "string" || typeof payload.watts !== "number") {
        return;
      }
      const unitIds = Array.isArray(payload.unit_ids)
        ? payload.unit_ids.filter((entry): entry is string => typeof entry === "string")
        : [];
      const figures = toIntentFigures(payload);
      setRequestedByUnit((previous) => {
        const next: WattsByUnit = { ...(previous ?? {}) };
        for (const unitId of unitIds) {
          delete next[unitId];
        }
        if (figures !== null && figures.wattsByUnit !== null) {
          // Scalar intents carry no map: dropping the units' stale entries IS
          // their fact, never "keep whatever was there".
          for (const [unitId, watts] of Object.entries(figures.wattsByUnit)) {
            next[unitId] = watts;
          }
        }
        return Object.keys(next).length > 0 ? next : null;
      });
      setAuthorizedByUnit((previous) => withoutUnits(previous, unitIds));
      setDirectionsByUnit((previous) => withoutUnits(previous, unitIds));
    } else if (type === "audit.appended") {
      // Only the kernel's per-tick decision summaries carry the maps; other
      // audit kinds never disturb the live request's figures. A cycle's maps
      // are the full current truth across every represented intent, so a
      // present map replaces wholesale.
      if (payload !== null && payload.event_type === "control_decision") {
        const unitWatts = toAuditUnitWatts(payload);
        if (unitWatts.requested !== null) {
          setRequestedByUnit(unitWatts.requested);
        }
        if (unitWatts.authorized !== null) {
          setAuthorizedByUnit(unitWatts.authorized);
        }
        const directions = toDirectionsByUnit(payload.directions_by_unit);
        if (directions !== null) {
          setDirectionsByUnit(directions);
        }
      }
    } else if (type === "authorization.granted") {
      // The authority-grant announcement (composition.py `_AsyncAuthorization
      // Repository.publish`, live on the bus since 2026-08-23): the cycle's
      // per-unit authorized watts (`watts_by_unit`) and each unit's own
      // direction (`directions_by_unit`). It is the freshest bus source for
      // the authorized map — it lands at the moment of the grant, not with
      // the next decision summary or snapshot — and its maps cover exactly
      // the batch the cycle minted, so a present map replaces wholesale.
      if (payload !== null) {
        const authorized = toWattsByUnit(payload.watts_by_unit);
        if (authorized !== null) {
          setAuthorizedByUnit(authorized);
        }
        const directions = toDirectionsByUnit(payload.directions_by_unit);
        if (directions !== null) {
          setDirectionsByUnit(directions);
        }
      }
    } else if (
      type === "intent.expired" ||
      type === "intent.cancelled" ||
      type === "authorization.revoked"
    ) {
      // The request (or its authority) ended for exactly the frame's units:
      // its figures must not linger there as a ghost, while another intent's
      // entries on other units survive. The kernel's next decision re-seeds
      // anything a still-live intent still holds. A frame naming no units
      // ends everything this tracker knew.
      const unitIds =
        payload !== null && Array.isArray(payload.unit_ids)
          ? payload.unit_ids.filter((entry): entry is string => typeof entry === "string")
          : [];
      if (unitIds.length === 0) {
        setRequestedByUnit(null);
        setAuthorizedByUnit(null);
        setDirectionsByUnit(null);
        return;
      }
      setRequestedByUnit((previous) => withoutUnits(previous, unitIds));
      setAuthorizedByUnit((previous) => withoutUnits(previous, unitIds));
      setDirectionsByUnit((previous) => withoutUnits(previous, unitIds));
    } else if (type === "emergency_stop.latched") {
      // The fence ends every request at once: no figure may survive it.
      setRequestedByUnit(null);
      setAuthorizedByUnit(null);
      setDirectionsByUnit(null);
    }
  }, []);

  const adoptAcceptance = useCallback((requested: unknown, unitIds: readonly string[] = []): void => {
    const figures = toIntentFigures(requested);
    if (figures === null) {
      return;
    }
    setRequestedByUnit((previous) => {
      const next: WattsByUnit = { ...(previous ?? {}) };
      for (const unitId of unitIds) {
        delete next[unitId];
      }
      if (figures.wattsByUnit !== null) {
        for (const [unitId, watts] of Object.entries(figures.wattsByUnit)) {
          next[unitId] = watts;
        }
      }
      return Object.keys(next).length > 0 ? next : null;
    });
    setAuthorizedByUnit((previous) => withoutUnits(previous, [...unitIds]));
    setDirectionsByUnit((previous) => withoutUnits(previous, [...unitIds]));
  }, []);

  const adoptSnapshot = useCallback((rawSnapshot: unknown): void => {
    if (!isRecord(rawSnapshot) || !("intent" in rawSnapshot)) {
      // Feature detection: today's snapshot carries no `intent` block, and an
      // absent field is no information — the current maps (bus-fed) stand.
      return;
    }
    const intent = rawSnapshot.intent;
    if (intent === null) {
      // The snapshot's own word: no active request. Nothing may survive it.
      setRequestedByUnit(null);
      setAuthorizedByUnit(null);
      setDirectionsByUnit(null);
      return;
    }
    const figures = toSnapshotIntentFigures(intent);
    if (figures.requestedByUnit !== null) {
      setRequestedByUnit(figures.requestedByUnit);
    }
    if (figures.authorizedByUnit !== null) {
      setAuthorizedByUnit(figures.authorizedByUnit);
    }
    if (figures.directionsByUnit !== null) {
      setDirectionsByUnit(figures.directionsByUnit);
    }
  }, []);

  const clear = useCallback((): void => {
    setRequestedByUnit(null);
    setAuthorizedByUnit(null);
    setDirectionsByUnit(null);
  }, []);

  return {
    requestedByUnit,
    authorizedByUnit,
    directionsByUnit,
    consumeEvent,
    adoptAcceptance,
    adoptSnapshot,
    clear,
  };
}
