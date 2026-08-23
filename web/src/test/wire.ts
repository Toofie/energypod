/**
 * The console's single source of wire truth for tests (and for views that
 * need the shapes the shared client types do not carry yet).
 *
 * Every constant, field name, and default below is derived from the SERVER
 * code, never from a view's preference:
 *
 * - Event vocabulary and payloads: the facade's `_publish` calls
 *   (src/energypod/application/service.py) and the composition-owned
 *   repositories (src/energypod/runtime/composition.py). A publisher sends
 *   `{"type", "payload"}`; the event bus (src/energypod/application/events.py
 *   `_build_envelope`) adds exactly `sequence` and `occurred_at` and forwards
 *   the body unchanged, so every event frame on the socket is
 *   `{type, sequence, occurred_at, payload}` with the unit facts nested under
 *   `payload` — never flat.
 * - Stream frames: src/energypod/api/rest.py (`snapshot`, `resync_required`,
 *   `error`). `snapshot` is `{type, sequence, data}`; `resync_required` is
 *   `{type, reason, snapshot_sequence?}` and carries NO sequence.
 * - Audit read model: `_SequencedAuditEvent` (composition.py) — every
 *   `AuditEvent` field (src/energypod/domain/audit.py) plus the store's own
 *   integer `sequence`. The wire key for the kind of an entry is `event_type`
 *   (there is no `type` field on the audit wire).
 * - Snapshot, unit view, and health: `EnergyServiceFacade.snapshot` /
 *   `_unit_view` / `health` (service.py). The snapshot unit carries
 *   `unit_id, lifecycle, telemetry_age_s, quality, requested_power,
 *   authorized_power, measured_watts` plus the amended-contract nullable
 *   `telemetry` summary projection (API_CONTRACTS.md "Application service
 *   facade": `soc_pct` … `active_warnings`, every field null when that datum
 *   is absent, never zero-filled) — still no `inhibit` field — and `quality`
 *   is the facade's own projection vocabulary `good | degraded | bad |
 *   missing` (never `stale`/`suspect`).
 * - Unit detail: `unit_detail(principal, unit_id)` — REST
 *   `GET /api/v1/units/{unit_id}` — the full latest-observation projection:
 *   identity (`device_identity`), `protocol_profile`, `connection_epoch`,
 *   telemetry and cell sequences and capture times, every scalar the
 *   telemetry summary carries, the complete `cell_voltages_v` and
 *   `temperatures_c` arrays, the per-field `quality` map, faults, and
 *   warnings. Unknown unit ids are refused with the structured envelope.
 *
 * Views and suites import from here so a fabricated wire shape cannot be
 * written twice. Anything the server does not send is absent from these
 * factories; asserting on it is a compile error, not a green test.
 */
import type { AuditEvent, AuditPage, Health, StreamEvent } from "../api/client";

// ---------------------------------------------------------------------------
// Event vocabulary (the only type strings the service ever publishes)
// ---------------------------------------------------------------------------

/** A unit's telemetry cycle landed; the payload is deliberately minimal. */
export const OBSERVATION_PUBLISHED = "observation.published" as const;

/** One durable audit fact was appended; the payload is its summary. */
export const AUDIT_APPENDED = "audit.appended" as const;

/** Every outstanding authorization was revoked (a fence, stop, or shutdown). */
export const AUTHORIZATION_REVOKED = "authorization.revoked" as const;

/**
 * A batch of authority landed in the store (composition.py
 * `_AsyncAuthorizationRepository.publish`, live on the bus since 2026-08-23 —
 * live-captured 2026-08-23: one frame per control cycle while a request runs):
 * `{cycle_id, generation, unit_ids, watts_by_unit, directions_by_unit}` —
 * each unit's OWN authorized watts and direction for the cycle that granted
 * them.
 */
export const AUTHORIZATION_GRANTED = "authorization.granted" as const;

/** The facade accepted one dispatch intent; it grants nothing by itself. */
export const INTENT_ACCEPTED = "intent.accepted" as const;

/**
 * A stored intent lapsed (its ttl ran out): the intent repository's tracking
 * wrapper announces each newly lapsed intent exactly once (composition.py
 * `_TrackingIntentRepository`), naming the intent, its units, and its figures.
 */
export const INTENT_EXPIRED = "intent.expired" as const;

/**
 * One intent was cancelled through the live cancel endpoint (service.py
 * `cancel_intent`): `{principal, intent_id, unit_ids}`. Removal (cancel, stop
 * acknowledgement) never announces an expiry — the two endings are disjoint.
 */
export const INTENT_CANCELLED = "intent.cancelled" as const;

export const UNIT_ARMED = "unit.armed" as const;
export const UNIT_DISARMED = "unit.disarmed" as const;
export const EMERGENCY_STOP_LATCHED = "emergency_stop.latched" as const;
export const EMERGENCY_STOP_ACKNOWLEDGED = "emergency_stop.acknowledged" as const;
export const INHIBIT_ACKNOWLEDGED = "inhibit.acknowledged" as const;

/**
 * The self-healing awareness layer's bus vocabulary (2026-08-24,
 * energypod.application.recovery): one `unit.health_changed` per state
 * transition, one `actuation.incoherent` per watchdog episode (plus one
 * echo-classified follow-up frame of the same type), and the QUIET-TIER
 * `unit.unexpected_autonomy` evidence payload — never an alarm vocabulary
 * entry.
 */
export const UNIT_HEALTH_CHANGED = "unit.health_changed" as const;
export const ACTUATION_INCOHERENT = "actuation.incoherent" as const;
export const UNIT_UNEXPECTED_AUTONOMY = "unit.unexpected_autonomy" as const;

/**
 * The excess-solar adviser's state announcement (DESIGN_EXCESS_ACTIVATION.md
 * §2): published only when the semantic state tuple changes, plus a 30 s
 * `"heartbeat": true` republish while enabled. PENDING-BACKEND — the backend
 * half of the package is not live yet, so this type is not in
 * PUBLISHED_EVENT_TYPES; suites attach it with `excessAdviserStateChanged`.
 */
export const EXCESS_ADVISER_STATE_CHANGED = "excess_adviser.state_changed" as const;

/** Every event type the composed service publishes, as a runtime checklist. */
export const PUBLISHED_EVENT_TYPES: readonly string[] = [
  OBSERVATION_PUBLISHED,
  AUDIT_APPENDED,
  AUTHORIZATION_REVOKED,
  AUTHORIZATION_GRANTED,
  INTENT_ACCEPTED,
  INTENT_EXPIRED,
  INTENT_CANCELLED,
  UNIT_ARMED,
  UNIT_DISARMED,
  EMERGENCY_STOP_LATCHED,
  EMERGENCY_STOP_ACKNOWLEDGED,
  INHIBIT_ACKNOWLEDGED,
  UNIT_HEALTH_CHANGED,
  ACTUATION_INCOHERENT,
  UNIT_UNEXPECTED_AUTONOMY,
];

// Stream frames the REST relay builds itself (rest.py); only `snapshot` and
// `resync_required`/`error` reach a browser.
export const SNAPSHOT_FRAME = "snapshot" as const;
export const RESYNC_REQUIRED_FRAME = "resync_required" as const;
export const ERROR_FRAME = "error" as const;

/** Wire vocabularies the views map into words (lowercase StrEnum values). */
export const LIFECYCLE_VALUES: readonly string[] = [
  "boot",
  "observe_only",
  "disarmed",
  "armed_idle",
  "active",
  "inhibited",
  "stopping",
  "disconnected",
];

/** The facade's snapshot quality projection (`_quality_projection`). */
export const SNAPSHOT_QUALITY_VALUES: readonly string[] = [
  "good",
  "degraded",
  "bad",
  "missing",
];

export const DIRECTION_VALUES: readonly string[] = ["charge", "discharge", "idle"];

/** `DecisionStatus` plus every facade mutation result. */
export const AUDIT_RESULT_VALUES: readonly string[] = [
  "authorized",
  "clamped",
  "rejected",
  "revoked",
  "accepted",
  "armed",
  "disarmed",
  "refused",
  "latched",
  "acknowledged",
];

/**
 * Every `event_type` the audit trail holds today: the kernel's control
 * decision (application/audit.py) and the facade/composition mutations.
 * There is no observation entry in the audit trail — observations are
 * published on the event bus, never audited.
 */
export const AUDIT_EVENT_TYPES: readonly string[] = [
  "control_decision",
  "intent_accepted",
  "unit_armed",
  "unit_disarmed",
  "emergency_stop",
  "stop_acknowledged",
  "inhibit_acknowledged",
  "authorization_revoked",
];

/** Reason codes the safety kernel and the facade actually emit. */
export const REASON_CODES: readonly string[] = [
  "safety_checks_passed",
  "power_clamped",
  "stop_authorized",
  "no_setpoints",
  "zero_dynamic_capability",
  "observation_missing",
  "previous_observation_missing",
  "telemetry_stale",
  "telemetry_from_future",
  "cell_data_missing",
  "cell_data_stale",
  "cell_imbalance",
  "cell_voltage_low",
  "cell_voltage_high",
  "cell_count_invalid",
  "temperature_low",
  "temperature_high",
  "temperature_spread",
  "temperatures_missing",
  "blocking_fault",
  "blocking_warning",
  "lifecycle_not_controllable",
  "soc_disagreement",
  "soc_jump",
  "soc_below_discharge_floor",
  "soc_above_charge_ceiling",
  "accepted",
  "armed",
  "disarmed",
  "acknowledged",
  "latched",
  "latch_cleared",
  "not_latched",
  "unknown_unit",
  "inhibit_latched",
  "not_qualified",
  "qualification_unknown",
  "actor_failure",
  "revoked",
];

// ---------------------------------------------------------------------------
// Event frames: {type, sequence, occurred_at, payload}
// ---------------------------------------------------------------------------

/**
 * A type alias (not an interface) so frames stay assignable to the client's
 * `StreamEvent` index signature without a cast at every fixture site.
 */
export type EventFrame<Payload> = {
  readonly type: string;
  readonly sequence: number;
  readonly occurred_at: string;
  readonly payload: Payload;
};

const DEFAULT_OCCURRED_AT = "2026-08-22T10:00:00Z";

function frame<Payload>(
  type: string,
  sequence: number,
  payload: Payload,
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<Payload> {
  return { type, sequence, occurred_at: occurredAt, payload };
}

/** A published observation: identity and ordering only, no telemetry detail. */
export interface ObservationPublishedPayload {
  readonly unit_id: string;
  readonly connection_epoch: number | null;
  readonly sequence: number | null;
}

export function observationPublished(
  sequence: number,
  unitId: string,
  options: { occurredAt?: string; connectionEpoch?: number | null; telemetrySequence?: number | null } = {},
): EventFrame<ObservationPublishedPayload> {
  return frame(
    OBSERVATION_PUBLISHED,
    sequence,
    {
      unit_id: unitId,
      connection_epoch: options.connectionEpoch ?? 3,
      sequence: options.telemetrySequence ?? sequence * 10,
    },
    options.occurredAt,
  );
}

/**
 * The summary the audit port publishes for every durable append
 * (composition.py `_AsyncAuditRepository.append`): the watt figures render
 * straight off the stream, and the per-unit breakdowns ride the same payload —
 * `requested_watts_by_unit` is each unit's WINNING intent's own target over
 * its surviving scope (null for scalar intents, cycle-level across the
 * concurrent intents a composed cycle represented), `authorized_watts_by_unit`
 * the decision's per-unit authorized watts (null when no batch was minted),
 * and `directions_by_unit` each unit's winning direction (a concurrent cycle
 * may run opposite directions on different units; null off composed rows).
 */
export interface AuditAppendedPayload {
  readonly event_id: string;
  readonly event_type: string;
  readonly unit_id: string | null;
  readonly generation: number | null;
  readonly result: string;
  readonly reason_codes: string[];
  readonly requested_active_w: number;
  readonly authorized_active_w: number;
  readonly requested_watts_by_unit: Record<string, number> | null;
  readonly authorized_watts_by_unit: Record<string, number> | null;
  readonly directions_by_unit: Record<string, string> | null;
}

export function auditAppended(
  sequence: number,
  summary: {
    event_id?: string;
    event_type?: string;
    unit_id?: string | null;
    generation?: number | null;
    result?: string;
    reason_codes?: string[];
    requested_active_w?: number;
    authorized_active_w?: number;
    requested_watts_by_unit?: Record<string, number> | null;
    authorized_watts_by_unit?: Record<string, number> | null;
    directions_by_unit?: Record<string, string> | null;
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<AuditAppendedPayload> {
  return frame(
    AUDIT_APPENDED,
    sequence,
    {
      event_id: summary.event_id ?? `facade-${sequence}`,
      event_type: summary.event_type ?? "control_decision",
      unit_id: summary.unit_id ?? null,
      generation: summary.generation ?? null,
      result: summary.result ?? "authorized",
      reason_codes: summary.reason_codes ?? ["safety_checks_passed"],
      requested_active_w: summary.requested_active_w ?? 0,
      authorized_active_w: summary.authorized_active_w ?? 0,
      requested_watts_by_unit: summary.requested_watts_by_unit ?? null,
      authorized_watts_by_unit: summary.authorized_watts_by_unit ?? null,
      directions_by_unit: summary.directions_by_unit ?? null,
    },
    occurredAt,
  );
}

export function authorizationRevoked(
  sequence: number,
  unitIds: readonly string[],
  reason: string | null = "supervision_shutdown",
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ reason: string | null; unit_ids: string[] }> {
  return frame(AUTHORIZATION_REVOKED, sequence, { reason, unit_ids: [...unitIds] }, occurredAt);
}

/**
 * An authority grant, exactly as the composition's authorization repository
 * announces it (live-captured 2026-08-23): the cycle, the generation, the
 * units the batch carries, and each unit's OWN authorized watts and
 * direction. `watts_by_unit` is the authorized figure per unit (NOT the
 * request) — the freshest bus source for the allowed map.
 */
export interface AuthorizationGrantedPayload {
  readonly cycle_id: string | null;
  readonly generation: number | null;
  readonly unit_ids: readonly string[];
  readonly watts_by_unit: Record<string, number>;
  readonly directions_by_unit: Record<string, string>;
}

export function authorizationGranted(
  sequence: number,
  grant: {
    cycle_id?: string | null;
    generation?: number | null;
    unit_ids?: readonly string[];
    watts_by_unit?: Record<string, number>;
    directions_by_unit?: Record<string, string>;
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<AuthorizationGrantedPayload> {
  const unitIds = [...(grant.unit_ids ?? ["MID"])];
  const watts = grant.watts_by_unit ?? Object.fromEntries(unitIds.map((id) => [id, 1000]));
  const directions =
    grant.directions_by_unit ?? Object.fromEntries(unitIds.map((id) => [id, "discharge"]));
  return frame(
    AUTHORIZATION_GRANTED,
    sequence,
    {
      cycle_id: grant.cycle_id ?? `cycle-${sequence}`,
      generation: grant.generation ?? 2,
      unit_ids: unitIds,
      watts_by_unit: watts,
      directions_by_unit: directions,
    },
    occurredAt,
  );
}

/**
 * The intent acceptance frame. `watts` is always the derived fleet total;
 * `watts_by_unit` rides the payload only when the intent used the per-unit
 * form (service.py `submit_intent` spreads it in exactly then — the two watt
 * forms are mutually exclusive on the wire).
 */
export function intentAccepted(
  sequence: number,
  intent: {
    principal?: string;
    intent_id?: string;
    direction?: string;
    watts?: number;
    watts_by_unit?: Record<string, number>;
    unit_ids?: readonly string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  principal: string;
  intent_id: string;
  direction: string;
  watts: number;
  watts_by_unit?: Record<string, number>;
  unit_ids: string[];
}> {
  return frame(
    INTENT_ACCEPTED,
    sequence,
    {
      principal: intent.principal ?? "operator:home",
      intent_id: intent.intent_id ?? `intent-${sequence}`,
      direction: intent.direction ?? "discharge",
      watts: intent.watts ?? 1000,
      ...(intent.watts_by_unit === undefined ? {} : { watts_by_unit: intent.watts_by_unit }),
      unit_ids: [...(intent.unit_ids ?? ["MID"])],
    },
    occurredAt,
  );
}

/**
 * A lapsed intent's announcement (composition.py `_TrackingIntentRepository`):
 * the intent's own id, source, direction, fleet total, and units.
 */
export function intentExpired(
  sequence: number,
  intent: {
    intent_id?: string;
    source?: string;
    direction?: string;
    watts?: number | null;
    unit_ids?: readonly string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  intent_id: string | null;
  source: string | null;
  direction: string | null;
  watts: number | null;
  unit_ids: string[];
}> {
  return frame(
    INTENT_EXPIRED,
    sequence,
    {
      intent_id: intent.intent_id ?? `intent-${sequence}`,
      source: intent.source ?? "manual",
      direction: intent.direction ?? "discharge",
      watts: intent.watts ?? 1000,
      unit_ids: [...(intent.unit_ids ?? ["MID"])],
    },
    occurredAt,
  );
}

/** A cancellation's announcement (service.py `cancel_intent`). */
export function intentCancelled(
  sequence: number,
  intent: {
    principal?: string;
    intent_id?: string;
    unit_ids?: readonly string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ principal: string; intent_id: string; unit_ids: string[] }> {
  return frame(
    INTENT_CANCELLED,
    sequence,
    {
      principal: intent.principal ?? "operator:home",
      intent_id: intent.intent_id ?? `intent-${sequence}`,
      unit_ids: [...(intent.unit_ids ?? ["MID"])],
    },
    occurredAt,
  );
}

/** One per-unit outcome exactly as the facade reports arm/disarm. */
export interface UnitOutcome {
  readonly unit_id: string;
  readonly status: string;
  readonly reason: string;
}

export function unitArmed(
  sequence: number,
  outcomes: readonly UnitOutcome[],
  principal: string = "operator:home",
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ principal: string; units: UnitOutcome[] }> {
  return frame(UNIT_ARMED, sequence, { principal, units: [...outcomes] }, occurredAt);
}

export function unitDisarmed(
  sequence: number,
  outcomes: readonly UnitOutcome[],
  principal: string = "operator:home",
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ principal: string; units: UnitOutcome[] }> {
  return frame(UNIT_DISARMED, sequence, { principal, units: [...outcomes] }, occurredAt);
}

export function emergencyStopLatched(
  sequence: number,
  stop: {
    principal?: string;
    stop_id?: string;
    unit_ids?: readonly string[];
    reason?: string;
    generation?: number | null;
    degraded?: string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  principal: string;
  stop_id: string;
  unit_ids: string[];
  reason: string;
  generation: number | null;
  degraded: string[];
}> {
  return frame(
    EMERGENCY_STOP_LATCHED,
    sequence,
    {
      principal: stop.principal ?? "operator:home",
      stop_id: stop.stop_id ?? `stop-${sequence}`,
      unit_ids: [...(stop.unit_ids ?? ["MID", "RHS", "LHS"])],
      reason: stop.reason ?? "Operator pressed stop",
      generation: stop.generation ?? 7,
      degraded: stop.degraded ?? [],
    },
    occurredAt,
  );
}

export function emergencyStopAcknowledged(
  sequence: number,
  stopId: string = "stop-7",
  principal: string = "operator:home",
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ principal: string; stop_id: string }> {
  return frame(EMERGENCY_STOP_ACKNOWLEDGED, sequence, { principal, stop_id: stopId }, occurredAt);
}

export function inhibitAcknowledged(
  sequence: number,
  unitId: string,
  latchCleared: boolean = true,
  principal: string = "operator:home",
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ principal: string; unit_id: string; latch_cleared: boolean }> {
  return frame(
    INHIBIT_ACKNOWLEDGED,
    sequence,
    { principal, unit_id: unitId, latch_cleared: latchCleared },
    occurredAt,
  );
}

/**
 * One `excess_adviser.state_changed` payload exactly as the design contract §2
 * spells it: the §1 projection's state tuple and watt figures plus
 * `heartbeat`, and deliberately WITHOUT `charge_cap_w` / `last_action` /
 * `last_tick_at` (the payload is a patch onto the snapshot's projection, not a
 * replacement — consumers must keep the snapshot's composed cap). Defaults are
 * the contract's own illustrative holding example. PENDING-BACKEND.
 */
export interface ExcessAdviserStateChangedPayload {
  readonly enabled: boolean;
  readonly enabled_origin: "config" | "runtime";
  readonly acknowledged_economics: boolean;
  readonly active: boolean;
  readonly hysteresis_state: "inactive" | "entering" | "holding" | "exiting";
  readonly target_unit_id: string | null;
  readonly commanded_charge_w: number;
  readonly eligible_export_charge_w: number;
  readonly fleet_export_w: number | null;
  readonly export_evidence: "good" | "missing" | "bad" | "stale";
  readonly reason_codes: readonly string[];
  readonly held_intent_id: string | null;
  readonly heartbeat: boolean;
}

export function excessAdviserStateChanged(
  sequence: number,
  payload: Partial<ExcessAdviserStateChangedPayload> = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<ExcessAdviserStateChangedPayload> {
  const base = adviserState(payload);
  return frame(
    EXCESS_ADVISER_STATE_CHANGED,
    sequence,
    {
      enabled: base.enabled,
      enabled_origin: base.enabled_origin,
      acknowledged_economics: base.acknowledged_economics,
      active: base.active,
      hysteresis_state: base.hysteresis_state,
      target_unit_id: base.target_unit_id,
      commanded_charge_w: base.commanded_charge_w,
      eligible_export_charge_w: base.eligible_export_charge_w,
      fleet_export_w: base.fleet_export_w,
      export_evidence: base.export_evidence,
      reason_codes: base.reason_codes,
      held_intent_id: base.held_intent_id,
      heartbeat: payload.heartbeat ?? false,
    },
    occurredAt,
  );
}

/**
 * The derived recovery vocabulary the monitor serves (recovery.py
 * `HealthState`, lowercase StrEnum values) — the only `health_state` values
 * the wire ever carries.
 */
export const HEALTH_STATE_VALUES: readonly string[] = [
  "healthy",
  "self_healing",
  "actuation_incoherent",
  "not_responding",
  "unreachable",
  "foreign_writer",
  "inhibited",
];

/** The self-healing reason codes the classifier derives (recovery.py). */
export const HEALTH_REASON_CODES: readonly string[] = [
  "gateway_unreachable",
  "reads_timing_out",
  "external_writer_latched",
  "inhibited",
  "authorized_not_actuating",
  "requalifying_after_inhibit",
  "cell_balancing",
  "autonomous_self_charge",
];

/** The objective-echo classifications (recovery.py; `external_writer` rides
 * the existing latch vocabulary as its classification). */
export const ECHO_CLASSIFICATIONS: readonly string[] = [
  "echo_matches_write",
  "external_writer",
  "objective_not_served",
  "echo_unreadable",
];

/**
 * One `unit.health_changed` frame exactly as the monitor publishes it on a
 * derived-state transition (recovery.py `observe_cycle`):
 * `{unit_id, from, to, reasons}` — the console's live transition source.
 */
export function unitHealthChanged(
  sequence: number,
  transition: {
    unit_id?: string;
    from?: string;
    to: string;
    reasons?: readonly string[];
  },
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{ unit_id: string; from: string; to: string; reasons: string[] }> {
  return frame(
    UNIT_HEALTH_CHANGED,
    sequence,
    {
      unit_id: transition.unit_id ?? "mid",
      from: transition.from ?? "healthy",
      to: transition.to,
      reasons: [...(transition.reasons ?? [])],
    },
    occurredAt,
  );
}

/**
 * One `actuation.incoherent` frame (recovery.py `_incoherent_payload`). The
 * episode's opening frame carries the watchdog figures; passing `echo`
 * produces the follow-up frame the objective echo read-back publishes once it
 * classifies — same type, plus the discriminator fields. The payload's
 * figures are nullable on the wire (no usable measurement yet); explicit
 * nulls are preserved.
 */
export function actuationIncoherent(
  sequence: number,
  detection: {
    unit_id?: string;
    cycles?: number | null;
    authorized_watts?: number | null;
    authorized_direction?: string | null;
    measured_watts?: number | null;
    baseline_watts?: number | null;
    movement_watts?: number | null;
    echo?: {
      classification: string;
      served_active_w?: number | null;
      served_reactive_var?: number | null;
    };
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<Record<string, unknown>> {
  const payload: Record<string, unknown> = {
    unit_id: detection.unit_id ?? "mid",
    cycles: detection.cycles === undefined ? 4 : detection.cycles,
    authorized_watts:
      detection.authorized_watts === undefined ? 1000 : detection.authorized_watts,
    authorized_direction: detection.authorized_direction === undefined ? "discharge" : detection.authorized_direction,
    measured_watts: detection.measured_watts === undefined ? 12 : detection.measured_watts,
    baseline_watts: detection.baseline_watts === undefined ? 8 : detection.baseline_watts,
    movement_watts: detection.movement_watts === undefined ? 4 : detection.movement_watts,
  };
  if (detection.echo !== undefined) {
    payload.echo_classification = detection.echo.classification;
    payload.served_active_w = detection.echo.served_active_w ?? 1000;
    payload.served_reactive_var = detection.echo.served_reactive_var ?? 0;
  }
  return frame(ACTUATION_INCOHERENT, sequence, payload, occurredAt);
}

/**
 * One `unit.unexpected_autonomy` frame (recovery.py
 * `_record_unexpected_autonomy`): the quiet-tier evidence payload — the
 * measured watts and the mode words, throttled to one per unit per 60 s.
 * Every figure nullable on the wire.
 */
export function unitUnexpectedAutonomy(
  sequence: number,
  evidence: {
    unit_id?: string;
    measured_watts?: number | null;
    soc_pct?: number | null;
    debug_mode_w?: number | null;
    ctrl_mode_w?: number | null;
    work_mode_w?: number | null;
    run_mode_w?: number | null;
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<Record<string, unknown>> {
  return frame(
    UNIT_UNEXPECTED_AUTONOMY,
    sequence,
    {
      unit_id: evidence.unit_id ?? "mid",
      measured_watts: evidence.measured_watts === undefined ? 1411.2 : evidence.measured_watts,
      soc_pct: evidence.soc_pct === undefined ? 10 : evidence.soc_pct,
      debug_mode_w: evidence.debug_mode_w === undefined ? null : evidence.debug_mode_w,
      ctrl_mode_w: evidence.ctrl_mode_w === undefined ? 1 : evidence.ctrl_mode_w,
      work_mode_w: evidence.work_mode_w === undefined ? 7 : evidence.work_mode_w,
      run_mode_w: evidence.run_mode_w === undefined ? 0 : evidence.run_mode_w,
    },
    occurredAt,
  );
}

// ---------------------------------------------------------------------------
// Stream frames the REST relay builds (rest.py)
// ---------------------------------------------------------------------------

/** The authoritative first frame of every connection. */
export function snapshotFrame(state: WireSnapshot): StreamEvent {
  return {
    type: SNAPSHOT_FRAME,
    sequence: state.snapshot_sequence,
    data: state,
  } as unknown as StreamEvent;
}

/** A discontinuity: the caller refetches a snapshot and reconnects. No sequence. */
export function resyncRequired(
  reason: string = "retention_window_exceeded",
  snapshotSequence?: number,
): StreamEvent {
  const marker: Record<string, unknown> = { type: RESYNC_REQUIRED_FRAME, reason };
  if (snapshotSequence !== undefined) {
    marker.snapshot_sequence = snapshotSequence;
  }
  return marker as unknown as StreamEvent;
}

/** A frame-level failure after accept (rest.py sends the error envelope). */
export function errorFrame(
  code: string = "internal_error",
  message: string = "The event stream failed",
  requestId: string = "req-stream",
): StreamEvent {
  return {
    type: ERROR_FRAME,
    code,
    message,
    details: null,
    request_id: requestId,
  } as unknown as StreamEvent;
}

// ---------------------------------------------------------------------------
// Snapshot / unit / health (service.py snapshot, _unit_view, health)
// ---------------------------------------------------------------------------

export interface WirePower {
  readonly direction: string;
  readonly watts: number;
}

/**
 * Exactly the fields `_unit_view` emits. `inhibit` is the API_CONTRACTS
 * "Inhibit acknowledgement" facade exposure; use `withInhibit` to attach it
 * and treat it as optional everywhere — today's snapshot does not carry it.
 */
export interface WireUnitSnapshot {
  readonly unit_id: string;
  readonly lifecycle: string;
  readonly telemetry_age_s: number | null;
  readonly quality: string;
  readonly requested_power: WirePower;
  readonly authorized_power: WirePower | null;
  readonly measured_watts: number | null;
  /**
   * The amended-contract nullable telemetry summary projection from the latest
   * observation. Absent (`null`) when the unit has no observation at all;
   * present-but-null fields mean that single datum was absent — never
   * zero-filled (API_CONTRACTS.md "Application service facade").
   */
  readonly telemetry: WireTelemetrySummary | null;
  readonly inhibit?: WireInhibitState | null;
  /**
   * The amended snapshot contract's per-unit latch exposure (2026-08-23
   * incident fix, PENDING like active_stops): `inhibit_latched` bool +
   * `inhibit_cause` reason code or null. Absent from today's wire.
   */
  readonly inhibit_latched?: boolean;
  readonly inhibit_cause?: string | null;
  /**
   * The self-healing awareness layer's derived per-unit fields (2026-08-24,
   * service.py `_health_projection`): `health_state` (the vocabulary above),
   * `health_reasons`, and `remediation_hint` — non-null only where remote
   * recovery is genuinely exhausted. The keys are ALWAYS emitted once the
   * recovery port is composed, with nulls when the projection is absent;
   * before that composition (and in the older snapshots) the keys are absent
   * entirely — the feature detection. Attach with `withHealth`.
   */
  readonly health_state?: string | null;
  readonly health_reasons?: readonly string[] | null;
  readonly remediation_hint?: string | null;
}

/** The documented latch exposure: cause class, latched flag, reason code. */
export interface WireInhibitState {
  readonly cause_class: string;
  readonly latched: boolean;
  readonly reason_code: string | null;
}

export function unitSnapshot(spec: Partial<WireUnitSnapshot> & { unit_id: string }): WireUnitSnapshot {
  const base: Omit<WireUnitSnapshot, "inhibit"> = {
    unit_id: spec.unit_id,
    lifecycle: spec.lifecycle ?? "disarmed",
    // Explicit nulls are meaningful on the wire (telemetry never captured);
    // only an absent key takes the default.
    telemetry_age_s: spec.telemetry_age_s === undefined ? 3 : spec.telemetry_age_s,
    quality: spec.quality ?? "good",
    requested_power: spec.requested_power ?? { direction: "idle", watts: 0 },
    authorized_power: spec.authorized_power === undefined ? null : spec.authorized_power,
    measured_watts: spec.measured_watts === undefined ? 0 : spec.measured_watts,
    telemetry: spec.telemetry === undefined ? null : spec.telemetry,
  };
  return spec.inhibit === undefined ? base : { ...base, inhibit: spec.inhibit };
}

/** Attach the documented (not-yet-emitted) inhibit exposure to a unit view. */
export function withInhibit(
  unit: WireUnitSnapshot,
  inhibit: { cause_class?: string; latched?: boolean; reason_code?: string | null },
): WireUnitSnapshot {
  return {
    ...unit,
    inhibit: {
      cause_class: inhibit.cause_class ?? "latched",
      latched: inhibit.latched ?? true,
      reason_code: inhibit.reason_code ?? null,
    },
  };
}

/**
 * Attach the recovery-health fields to a snapshot unit, exactly as the
 * composed facade projects them: the state, its reasons, and the remediation
 * hint (null unless the wedge class is proven). Explicit nulls are preserved;
 * an omitted state defaults to the nulls the facade emits for an absent
 * projection (the feature-present, state-absent answer).
 */
export function withHealth(
  unit: WireUnitSnapshot,
  health: {
    state?: string | null;
    reasons?: readonly string[] | null;
    remediation_hint?: string | null;
  } = {},
): WireUnitSnapshot {
  return {
    ...unit,
    health_state: health.state === undefined ? null : health.state,
    health_reasons: health.reasons === undefined ? null : [...(health.reasons ?? [])],
    remediation_hint: health.remediation_hint === undefined ? null : health.remediation_hint,
  };
}

export interface WireSnapshot {
  readonly site_id: string;
  readonly snapshot_sequence: number;
  readonly captured_at: string;
  readonly units: WireUnitSnapshot[];
  /**
   * The amended snapshot contract's engaged emergency stops (2026-08-23
   * incident fix): `[{stop_id, latched_at, principal, reason_codes,
   * unit_ids | null}]`, `null` unit_ids = fleet-wide. PENDING — the backend
   * field has not landed yet, so the default snapshot omits it entirely (an
   * absent field is today's wire truth); attach it with `withActiveStops`.
   */
  readonly active_stops?: readonly WireActiveStop[];
  /**
   * The snapshot-level intent block (PENDING, feature-detected): the live
   * request's own per-unit figures for cold-load exactness —
   * `requested_watts_by_unit` (the intent's own `watts_by_unit`; null for a
   * scalar intent), `authorized_watts_by_unit` (the decision's per-unit
   * authorized watts), `directions_by_unit` (each unit's winning direction —
   * a concurrent cycle may run opposite directions). `null` = no active
   * request; ABSENT (the default, today's wire) = the field has not landed,
   * and every consumer keeps its current fallbacks.
   */
  readonly intent?: WireSnapshotIntent | null;
  /**
   * The excess-solar adviser projection (PENDING, feature-detected, §1):
   * present whenever the `excess_charging` block is composed — including while
   * suspended — and ABSENT (the default, today's wire) when nothing is
   * composed: no adviser, no tile, no toggle. Attach with `withAdviserState`.
   */
  readonly adviser_state?: WireAdviserState;
  /**
   * The schedules projection (PENDING, feature-detected): present whenever the
   * `schedule:` config block is composed, ABSENT (the default, today's wire)
   * otherwise — no Schedule card, nothing else changes. Attach with
   * `withScheduleState`.
   */
  readonly schedule_state?: WireScheduleState;
}

/** The snapshot `intent` block's object form; every map nullable inside. */
export interface WireSnapshotIntent {
  readonly requested_watts_by_unit: Record<string, number> | null;
  readonly authorized_watts_by_unit: Record<string, number> | null;
  readonly directions_by_unit: Record<string, string> | null;
}

/**
 * Attach the pending intent block to a snapshot world. `null` (no active
 * request) is a real wire answer, distinct from the field being absent.
 */
export function withSnapshotIntent(
  world: WireSnapshot,
  intent: WireSnapshotIntent | null,
): WireSnapshot {
  return { ...world, intent };
}

/**
 * One engaged (latched, not yet acknowledged) emergency stop, exactly as the
 * amended snapshot contract spells it. PENDING (see WireSnapshot).
 */
export interface WireActiveStop {
  readonly stop_id: string;
  readonly latched_at: string;
  readonly principal: string;
  readonly reason_codes: readonly string[];
  /** null = fleet-wide. */
  readonly unit_ids: readonly string[] | null;
}

export function activeStop(
  spec: Partial<WireActiveStop> & { stop_id: string },
): WireActiveStop {
  return {
    stop_id: spec.stop_id,
    latched_at: spec.latched_at ?? DEFAULT_OCCURRED_AT,
    principal: spec.principal ?? "operator:home",
    reason_codes: spec.reason_codes ?? ["operator_requested"],
    unit_ids: spec.unit_ids === undefined ? null : spec.unit_ids,
  };
}

/** Attach the pending engaged-stop exposure to a snapshot world. */
export function withActiveStops(
  world: WireSnapshot,
  stops: readonly WireActiveStop[],
): WireSnapshot {
  return { ...world, active_stops: [...stops] };
}

/**
 * The excess-solar adviser projection (DESIGN_EXCESS_ACTIVATION.md §1): the
 * snapshot's top-level `adviser_state` block, present whenever the
 * `excess_charging` config block is composed, ABSENT otherwise (the default
 * snapshot omits it — an absent field is today's wire truth). PENDING-BACKEND:
 * attach it with `withAdviserState` and treat it as optional everywhere.
 */
export interface WireAdviserState {
  readonly enabled: boolean;
  readonly enabled_origin: "config" | "runtime";
  readonly acknowledged_economics: boolean;
  readonly active: boolean;
  readonly hysteresis_state: "inactive" | "entering" | "holding" | "exiting";
  readonly target_unit_id: string | null;
  readonly commanded_charge_w: number;
  readonly eligible_export_charge_w: number;
  /** Σ per-unit grid_power_w; NULL when any unit's grid evidence fails — never 0. */
  readonly fleet_export_w: number | null;
  readonly export_evidence: "good" | "missing" | "bad" | "stale";
  readonly charge_cap_w: number;
  readonly held_intent_id: string | null;
  readonly last_action: "idle" | "propose" | "renew" | "withdraw";
  readonly last_tick_at: string;
  readonly reason_codes: readonly string[];
}

/**
 * An adviser-projection fixture. Defaults are the contract's own illustrative
 * holding example (§1's JSON, verbatim); explicit nulls are preserved — an
 * explicit `fleet_export_w: null` is the fail-closed export answer, distinct
 * from leaving the default.
 */
export function adviserState(spec: Partial<WireAdviserState> = {}): WireAdviserState {
  return {
    enabled: spec.enabled ?? true,
    enabled_origin: spec.enabled_origin ?? "runtime",
    acknowledged_economics: spec.acknowledged_economics ?? true,
    active: spec.active ?? true,
    hysteresis_state: spec.hysteresis_state ?? "holding",
    target_unit_id: spec.target_unit_id === undefined ? "mid" : spec.target_unit_id,
    commanded_charge_w: spec.commanded_charge_w ?? 1400,
    eligible_export_charge_w: spec.eligible_export_charge_w ?? 1600,
    fleet_export_w: spec.fleet_export_w === undefined ? 1800 : spec.fleet_export_w,
    export_evidence: spec.export_evidence ?? "good",
    charge_cap_w: spec.charge_cap_w ?? 2500,
    held_intent_id: spec.held_intent_id === undefined ? "opt-3f9c21" : spec.held_intent_id,
    last_action: spec.last_action ?? "renew",
    last_tick_at: spec.last_tick_at ?? "2026-08-25T11:04:31+10:00",
    reason_codes: spec.reason_codes ?? ["export_headroom_available"],
  };
}

/** Attach the pending adviser projection to a snapshot world. */
export function withAdviserState(world: WireSnapshot, state: WireAdviserState): WireSnapshot {
  return { ...world, adviser_state: state };
}

// ---------------------------------------------------------------------------
// Schedules (DESIGN_SCHEDULES.md §5, API_CONTRACTS.md "Schedule") — the whole
// family is PENDING-BACKEND: the REST routes, the schedule_state projection,
// and the bus vocabulary are not live yet, so none of these types is in
// PUBLISHED_EVENT_TYPES and the default snapshot omits `schedule_state`
// entirely (an absent field is today's wire truth). Attach with
// `withScheduleState`; build refusals with `scheduleRefusalEnvelope`.
// ---------------------------------------------------------------------------

/** The bus vocabulary the schedules surface publishes (transitions only). */
export const SCHEDULE_REPLACED = "schedule.replaced" as const;
export const SCHEDULE_WINDOW_OPENED = "schedule_window.opened" as const;
export const SCHEDULE_WINDOW_CLOSING = "schedule_window.closing" as const;

/** One plan entry exactly as the wire carries it: EITHER watt form, never both. */
export interface WireScheduleEntry {
  readonly entry_id: string;
  readonly days: readonly string[];
  readonly start_local: string;
  readonly end_local: string;
  readonly action: "charge" | "discharge" | "idle";
  readonly watts?: number;
  readonly watts_by_unit?: Readonly<Record<string, number>>;
  readonly unit_ids: readonly string[];
  readonly effective_from: string | null;
  readonly effective_until: string | null;
  readonly priority: number;
  readonly enabled: boolean;
}

/** The days vocabulary (lowercase three-letter names, the wire's own order). */
export const SCHEDULE_DAY_VALUES: readonly string[] = [
  "mon",
  "tue",
  "wed",
  "thu",
  "fri",
  "sat",
  "sun",
];

/**
 * A schedule-entry fixture. Defaults are the doc's own illustrative day-window
 * example (§1's wording, made day-legal): "Day charge", every day, 06:30 to
 * 18:00, charging the three captured units at 2,500 W each — the per-battery
 * form the v1 editor defaults to. Pass `watts` for the scalar form; an
 * explicit `watts_by_unit` wins over the default map.
 */
export function scheduleEntry(spec: Partial<WireScheduleEntry> = {}): WireScheduleEntry {
  const unitIds = [...(spec.unit_ids ?? ["lhs", "mid", "rhs"])];
  const wattsByUnit =
    spec.watts_by_unit ??
    Object.fromEntries(unitIds.map((unitId) => [unitId, 2500]));
  const wattForm =
    spec.watts !== undefined
      ? { watts: spec.watts }
      : { watts_by_unit: wattsByUnit as Record<string, number> };
  return {
    entry_id: spec.entry_id ?? "Day charge",
    days: [...(spec.days ?? SCHEDULE_DAY_VALUES)],
    start_local: spec.start_local ?? "06:30",
    end_local: spec.end_local ?? "18:00",
    action: spec.action ?? "charge",
    ...wattForm,
    unit_ids: unitIds,
    effective_from: spec.effective_from ?? null,
    effective_until: spec.effective_until ?? null,
    priority: spec.priority ?? 0,
    enabled: spec.enabled ?? true,
  };
}

/** The published plan: one version, one IANA timezone, the entry list. */
export interface WireSchedulePlan {
  readonly version: number;
  readonly timezone: string;
  readonly entries: readonly WireScheduleEntry[];
}

export function schedulePlan(
  entries: readonly WireScheduleEntry[],
  spec: { version?: number; timezone?: string } = {},
): WireSchedulePlan {
  return {
    version: spec.version ?? 4,
    timezone: spec.timezone ?? "Australia/Brisbane",
    entries: [...entries],
  };
}

/** The derived, read-only policy GET carries beside the plan. */
export interface WireSchedulePolicy {
  readonly posture: "yield" | "partition";
  readonly allowed_windows_local: readonly (readonly [string, string])[];
  readonly intent_ttl_s: number;
}

/** A policy fixture; defaults are the shipped day-only YIELD posture (§3). */
export function schedulePolicy(
  spec: Partial<WireSchedulePolicy> = {},
): WireSchedulePolicy {
  return {
    posture: spec.posture ?? "yield",
    allowed_windows_local:
      spec.allowed_windows_local ?? ([["06:00", "20:00"]] as readonly (readonly [string, string])[]),
    intent_ttl_s: spec.intent_ttl_s ?? 10,
  };
}

/**
 * The pure next-occurrence object (§5.4) — GET's `next_action` and the
 * projection's `next` share this shape. Defaults follow §5's Night Charge
 * example, one hour out.
 */
export interface WireScheduleNextAction {
  readonly entry_id: string;
  readonly days: readonly string[];
  readonly start_local: string;
  readonly end_local: string;
  readonly action: "charge" | "discharge" | "idle";
  readonly watts?: number;
  readonly watts_by_unit?: Readonly<Record<string, number>>;
  readonly unit_ids: readonly string[];
  readonly starts_at: string;
  readonly starts_in_s: number;
}

export function scheduleNextAction(
  spec: Partial<WireScheduleNextAction> = {},
): WireScheduleNextAction {
  const unitIds = [...(spec.unit_ids ?? ["lhs", "mid", "rhs"])];
  const wattForm =
    spec.watts !== undefined
      ? { watts: spec.watts }
      : {
          watts_by_unit:
            spec.watts_by_unit ??
            (Object.fromEntries(unitIds.map((unitId) => [unitId, 2500])) as Record<string, number>),
        };
  return {
    entry_id: spec.entry_id ?? "Night Charge",
    days: [...(spec.days ?? SCHEDULE_DAY_VALUES)],
    start_local: spec.start_local ?? "00:01",
    end_local: spec.end_local ?? "05:59",
    action: spec.action ?? "charge",
    ...wattForm,
    unit_ids: unitIds,
    starts_at: spec.starts_at ?? "2026-08-24T00:01:00+10:00",
    starts_in_s: spec.starts_in_s ?? 3600,
  };
}

/**
 * The snapshot's top-level `schedule_state` projection (§5), verbatim from the
 * contract's own holding example: Night Charge running under the partition
 * posture, ~46 min left. `next` defaults to null exactly like the example
 * (the next occurrence after the running window).
 */
export interface WireScheduleState {
  readonly version: number | null;
  readonly active: boolean;
  readonly entry_id: string | null;
  readonly held_intent_id: string | null;
  readonly ends_at: string | null;
  readonly ends_in_s: number | null;
  readonly next: WireScheduleNextAction | null;
  readonly posture: "yield" | "partition";
  readonly last_action: "idle" | "submit" | "renew" | "remove";
  readonly last_tick_at: string;
  readonly reason_codes: readonly string[];
}

export function scheduleState(spec: Partial<WireScheduleState> = {}): WireScheduleState {
  return {
    version: spec.version === undefined ? 4 : spec.version,
    active: spec.active ?? true,
    entry_id: spec.entry_id === undefined ? "Night Charge" : spec.entry_id,
    held_intent_id: spec.held_intent_id === undefined ? "schedule-4-881.234117" : spec.held_intent_id,
    ends_at: spec.ends_at === undefined ? "2026-08-24T05:59:00+10:00" : spec.ends_at,
    ends_in_s: spec.ends_in_s === undefined ? 2743 : spec.ends_in_s,
    next: spec.next === undefined ? null : spec.next,
    posture: spec.posture ?? "partition",
    last_action: spec.last_action ?? "renew",
    last_tick_at: spec.last_tick_at ?? "2026-08-24T03:13:41+10:00",
    reason_codes: [...(spec.reason_codes ?? ["window_open"])],
  };
}

/** Attach the pending schedule projection to a snapshot world. */
export function withScheduleState(
  world: WireSnapshot,
  state: WireScheduleState,
): WireSnapshot {
  return { ...world, schedule_state: state };
}

/**
 * The `schedule.replaced` frame (`{principal, version, diff}` — one per
 * publish; `version` is the NEW plan version). PENDING-BACKEND.
 */
export function scheduleReplaced(
  sequence: number,
  payload: {
    principal?: string;
    version?: number;
    added?: readonly string[];
    removed?: readonly string[];
    changed?: readonly string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  principal: string;
  version: number;
  diff: { added: string[]; removed: string[]; changed: string[] };
}> {
  return frame(
    SCHEDULE_REPLACED,
    sequence,
    {
      principal: payload.principal ?? "operator:home",
      version: payload.version ?? 5,
      diff: {
        added: [...(payload.added ?? ["Night Charge"])],
        removed: [...(payload.removed ?? [])],
        changed: [...(payload.changed ?? [])],
      },
    },
    occurredAt,
  );
}

/**
 * The `schedule_window.opened` frame — the runner's first submit for a window
 * key, carrying the window's whole command. PENDING-BACKEND.
 */
export function scheduleWindowOpened(
  sequence: number,
  payload: {
    entry_id?: string;
    version?: number;
    action?: "charge" | "discharge" | "idle";
    watts?: number;
    watts_by_unit?: Readonly<Record<string, number>>;
    unit_ids?: readonly string[];
    ends_at?: string;
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<Record<string, unknown>> {
  const unitIds = [...(payload.unit_ids ?? ["lhs", "mid", "rhs"])];
  return frame(
    SCHEDULE_WINDOW_OPENED,
    sequence,
    {
      entry_id: payload.entry_id ?? "Night Charge",
      version: payload.version ?? 4,
      action: payload.action ?? "charge",
      ...(payload.watts !== undefined
        ? { watts: payload.watts }
        : {
            watts_by_unit:
              payload.watts_by_unit ??
              (Object.fromEntries(unitIds.map((unitId) => [unitId, 2500])) as Record<string, number>),
          }),
      unit_ids: unitIds,
      ends_at: payload.ends_at ?? "2026-08-24T05:59:00+10:00",
    },
    occurredAt,
  );
}

/**
 * The `schedule_window.closing` frame — the removal tick: the window's command
 * just ended (`window_ended | plan_replaced | no_plan`). PENDING-BACKEND.
 */
export function scheduleWindowClosing(
  sequence: number,
  payload: {
    entry_id?: string;
    version?: number;
    unit_ids?: readonly string[];
    reason?: "window_ended" | "plan_replaced" | "no_plan";
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  entry_id: string;
  version: number;
  unit_ids: string[];
  reason: string;
}> {
  return frame(
    SCHEDULE_WINDOW_CLOSING,
    sequence,
    {
      entry_id: payload.entry_id ?? "Night Charge",
      version: payload.version ?? 4,
      unit_ids: [...(payload.unit_ids ?? ["lhs", "mid", "rhs"])],
      reason: payload.reason ?? "window_ended",
    },
    occurredAt,
  );
}

/** The GET /api/v1/schedule 200 body. PENDING-BACKEND. */
export function getScheduleOk(view: {
  plan?: WireSchedulePlan | null;
  policy?: WireSchedulePolicy;
  acknowledged_night_windows?: boolean;
  next_action?: WireScheduleNextAction | null;
}): Record<string, unknown> {
  return {
    plan: view.plan === undefined ? null : view.plan,
    policy: view.policy ?? schedulePolicy(),
    acknowledged_night_windows: view.acknowledged_night_windows ?? false,
    next_action: view.next_action === undefined ? null : view.next_action,
  };
}

/** The PUT /api/v1/schedule 200 body (the stored plan, the diff, the next). */
export function putScheduleOk(view: {
  version: number;
  plan: WireSchedulePlan;
  added?: readonly string[];
  removed?: readonly string[];
  changed?: readonly string[];
  timezone_changed?: boolean;
  acknowledged_night_windows?: boolean;
  next_action?: WireScheduleNextAction | null;
}): Record<string, unknown> {
  return {
    version: view.version,
    plan: view.plan,
    diff: {
      added: [...(view.added ?? [])],
      removed: [...(view.removed ?? [])],
      changed: [...(view.changed ?? [])],
      timezone_changed: view.timezone_changed ?? false,
    },
    acknowledged_night_windows: view.acknowledged_night_windows ?? false,
    next_action: view.next_action === undefined ? null : view.next_action,
  };
}

/**
 * A refusal envelope for the schedule routes, shaped exactly as the thrown
 * `ApiClientError` carries it (tests wrap: `new ApiClientError({...}))`).
 * The two named shapes carry their contract-pinned details verbatim (§3).
 */
export function scheduleRefusalEnvelope(
  code:
    | "schedule_not_commissioned"
    | "schedule_window_not_allowed"
    | "night_posture_acknowledgement_required"
    | "schedule_version_conflict"
    | "validation_error",
  options: {
    message?: string;
    details?: Record<string, unknown>;
    status?: number;
  } = {},
): { status: number; code: string; message: string; details: Record<string, unknown> | null; request_id: string } {
  const defaults: Record<string, { status: number; message: string; details: Record<string, unknown> | null }> = {
    schedule_not_commissioned: {
      status: 409,
      message: "Scheduling is not commissioned in this deployment's config.",
      details: null,
    },
    schedule_window_not_allowed: {
      status: 409,
      message:
        "Night Charge (00:01–05:59) falls outside the allowed windows 06:00–20:00 — the night window belongs to the site's other writer applications (day-only posture). Trim the entry to the allowed windows, or make the partition choice: stand the external writers down and widen allowed_windows_local in config, then acknowledge once in the console.",
      details: {
        posture: "yield",
        allowed_windows_local: [["06:00", "20:00"]],
        offending: [{ entry_id: "Night Charge", start_local: "00:01", end_local: "05:59" }],
      },
    },
    night_posture_acknowledgement_required: {
      status: 409,
      message:
        "The first night-window publish needs the one-time partition acknowledgement.",
      details: { acknowledgement: "PARTITION_ACKNOWLEDGED" },
    },
    schedule_version_conflict: {
      status: 409,
      message: "The plan changed elsewhere while you were editing.",
      details: { current_version: 5 },
    },
    validation_error: {
      status: 422,
      message: "The schedule entries did not validate.",
      details: {
        entries: [
          { entry_id: "Night Charge", field: "end_local", message: "window must outlast its start" },
        ],
      },
    },
  };
  const pinned = defaults[code]!;
  return {
    status: options.status ?? pinned.status,
    code,
    message: options.message ?? pinned.message,
    details: options.details ?? pinned.details,
    request_id: `req-${code}`,
  };
}

/**
 * The guarded toggle's 200 body (§3), either action: the feature id, the
 * post-toggle participation facts, `persisted: false` spelled anyway (the
 * contract states the non-persistence policy on every response), and the full
 * §1 projection for optimistic adoption. PENDING-BACKEND.
 */
export function excessChargingToggleOk(state: WireAdviserState): Record<string, unknown> {
  return {
    feature: "excess_charging",
    enabled: state.enabled,
    enabled_origin: state.enabled_origin,
    persisted: false,
    acknowledged_economics: state.acknowledged_economics,
    adviser_state: state,
  };
}

export function snapshot(
  units: readonly WireUnitSnapshot[],
  spec: { site_id?: string; snapshot_sequence?: number; captured_at?: string } = {},
): WireSnapshot {
  return {
    site_id: spec.site_id ?? "home-1",
    snapshot_sequence: spec.snapshot_sequence ?? 4100,
    captured_at: spec.captured_at ?? DEFAULT_OCCURRED_AT,
    units: [...units],
  };
}

// ---------------------------------------------------------------------------
// Telemetry summary + unit detail (API_CONTRACTS.md "Application service
// facade"; observation fields: src/energypod/domain/observations.py)
// ---------------------------------------------------------------------------

/**
 * The observation's own telemetry fields, exactly as the domain's per-field
 * `quality` map keys them (Observation.QUALITY_FIELDS). Note the observation
 * spells SOC `system_soc_pct` while the projections spell it `soc_pct`.
 */
export const TELEMETRY_QUALITY_FIELDS: readonly string[] = [
  "system_soc_pct",
  "bms_soc_pct",
  "soh_pct",
  "battery_watts",
  "pack_voltage_v",
  "pack_current_a",
  "dynamic_charge_limit_w",
  "dynamic_discharge_limit_w",
  "cell_voltages_v",
  "temperatures_c",
];

/** The observation's DataQuality vocabulary (lowercase StrEnum values). */
export const DATA_QUALITY_VALUES: readonly string[] = [
  "good",
  "stale",
  "missing",
  "bad",
  "suspect",
];

/**
 * The nullable per-unit telemetry summary the amended snapshot contract adds:
 * every field is null when that datum is absent from the observation, never
 * zero-filled or fabricated.
 */
/** Wire sign convention (PROTOCOL_EVIDENCE 4b, live-proven): battery
 * watts/current POSITIVE = DISCHARGE, negative = CHARGE. */
export interface WireTelemetrySummary {
  readonly soc_pct: number | null;
  readonly bms_soc_pct: number | null;
  readonly soh_pct: number | null;
  readonly pack_voltage_v: number | null;
  readonly pack_current_a: number | null;
  readonly battery_watts: number | null;
  readonly dynamic_charge_limit_w: number | null;
  readonly dynamic_discharge_limit_w: number | null;
  readonly cell_count: number | null;
  readonly cell_min_v: number | null;
  readonly cell_max_v: number | null;
  readonly cell_spread_mv: number | null;
  readonly temperature_min_c: number | null;
  readonly temperature_max_c: number | null;
  readonly active_faults: readonly string[] | null;
  readonly active_warnings: readonly string[] | null;
  /**
   * Advisory per-pod CT power, readthrough-style (service.py
   * `_telemetry_summary`, live on today's wire): `grid_power_w` is signed —
   * negative = import, positive = export — and `load_power_w` is the pod's
   * local load. Both null when the poll served no PCS live block, never
   * zero-filled.
   */
  readonly grid_power_w: number | null;
  readonly load_power_w: number | null;
}

/**
 * A telemetry summary fixture. Defaults are the mandated live-capture decode
 * for MID (docs/evidence/field-mapping-2026-08-22.md): SOC 10 %, pack
 * 192.4 V, 60 cells spanning 3.205-3.209 V, 23-28 °C, the two fleet-wide
 * calibration warnings, no faults. Scalars the mandate does not name stay
 * null — an honest absent datum, never a stand-in value.
 */
export function telemetrySummary(spec: Partial<WireTelemetrySummary> = {}): WireTelemetrySummary {
  return {
    soc_pct: spec.soc_pct === undefined ? 10 : spec.soc_pct,
    bms_soc_pct: spec.bms_soc_pct === undefined ? null : spec.bms_soc_pct,
    soh_pct: spec.soh_pct === undefined ? null : spec.soh_pct,
    pack_voltage_v: spec.pack_voltage_v === undefined ? 192.4 : spec.pack_voltage_v,
    pack_current_a: spec.pack_current_a === undefined ? null : spec.pack_current_a,
    battery_watts: spec.battery_watts === undefined ? null : spec.battery_watts,
    dynamic_charge_limit_w:
      spec.dynamic_charge_limit_w === undefined ? null : spec.dynamic_charge_limit_w,
    dynamic_discharge_limit_w:
      spec.dynamic_discharge_limit_w === undefined ? null : spec.dynamic_discharge_limit_w,
    cell_count: spec.cell_count === undefined ? 60 : spec.cell_count,
    cell_min_v: spec.cell_min_v === undefined ? 3.205 : spec.cell_min_v,
    cell_max_v: spec.cell_max_v === undefined ? 3.209 : spec.cell_max_v,
    cell_spread_mv: spec.cell_spread_mv === undefined ? 4 : spec.cell_spread_mv,
    temperature_min_c: spec.temperature_min_c === undefined ? 23 : spec.temperature_min_c,
    temperature_max_c: spec.temperature_max_c === undefined ? 28 : spec.temperature_max_c,
    // Null by default: the commissioning capture's poll served no PCS live
    // block, and an absent datum is never a stand-in value. Tests that need
    // per-phase figures set them explicitly.
    grid_power_w: spec.grid_power_w === undefined ? null : spec.grid_power_w,
    load_power_w: spec.load_power_w === undefined ? null : spec.load_power_w,
    active_faults: spec.active_faults === undefined ? [] : spec.active_faults,
    active_warnings:
      spec.active_warnings === undefined
        ? ["PCS_Warning0_1", "DCDC_Warning0_1"]
        : spec.active_warnings,
  };
}

/**
 * The full latest-observation projection `GET /api/v1/units/{unit_id}` returns
 * (API_CONTRACTS.md "Application service facade"): identity, protocol profile,
 * connection epoch, telemetry and cell sequences and capture times, every
 * scalar the summary carries, the complete cell-voltage and temperature
 * arrays, the per-field quality map, faults, and warnings.
 */
export interface WireUnitDetail {
  readonly unit_id: string;
  readonly lifecycle: string | null;
  readonly device_identity: string | null;
  readonly protocol_profile: string | null;
  readonly connection_epoch: number | null;
  readonly wall_timestamp: string | null;
  readonly captured_at_mono: number | null;
  readonly sequence: number | null;
  readonly cell_captured_at_mono: number | null;
  readonly cell_sequence: number | null;
  readonly soc_pct: number | null;
  readonly bms_soc_pct: number | null;
  readonly soh_pct: number | null;
  readonly pack_voltage_v: number | null;
  readonly pack_current_a: number | null;
  readonly battery_watts: number | null;
  readonly dynamic_charge_limit_w: number | null;
  readonly dynamic_discharge_limit_w: number | null;
  readonly cell_count: number | null;
  readonly cell_min_v: number | null;
  readonly cell_max_v: number | null;
  readonly cell_spread_mv: number | null;
  readonly temperature_min_c: number | null;
  readonly temperature_max_c: number | null;
  /** Same readthrough fields as the snapshot's telemetry block (see there). */
  readonly grid_power_w: number | null;
  readonly load_power_w: number | null;
  readonly active_faults: readonly string[] | null;
  readonly active_warnings: readonly string[] | null;
  readonly cell_voltages_v: readonly number[] | null;
  readonly temperatures_c: readonly number[] | null;
  readonly quality: Readonly<Record<string, string>> | null;
}

/**
 * Per-unit facts from the 2026-08-22 live capture (the mandate's decoded
 * values, with the remaining scalars anchored in
 * docs/evidence/field-mapping-2026-08-22.md: SOH 100 % everywhere, BMS
 * dynamic power limits, ASCII serial numbers, RTU ids). Battery watts and
 * pack current are signed: negative discharges, so V x I = P holds.
 */
interface FleetUnitFacts {
  readonly soc: number;
  readonly bmsSoc: number;
  readonly soh: number;
  readonly packVoltageV: number;
  readonly packCurrentA: number;
  readonly batteryWatts: number;
  readonly chargeLimitW: number;
  readonly dischargeLimitW: number;
  readonly cellCount: number;
  readonly cellMinV: number;
  readonly cellMaxV: number;
  readonly tempMinC: number;
  readonly tempMaxC: number;
  readonly serial: string;
  readonly rtuId: string;
}

const FLEET_FACTS: Record<string, FleetUnitFacts> = {
  MID: {
    soc: 10,
    bmsSoc: 10,
    soh: 100,
    packVoltageV: 192.4,
    packCurrentA: 0,
    batteryWatts: 0,
    chargeLimitW: 7692,
    // SOC 10 %: the BMS inhibits discharge (limit 0 W) — the captured state.
    dischargeLimitW: 0,
    cellCount: 60,
    cellMinV: 3.205,
    cellMaxV: 3.209,
    tempMinC: 23,
    tempMaxC: 28,
    serial: "BEP0005KXX11B10500151",
    rtuId: "0x2C225097",
  },
  RHS: {
    soc: 68,
    bmsSoc: 68,
    soh: 100,
    packVoltageV: 164.5,
    packCurrentA: 6.9,
    batteryWatts: 1132,
    chargeLimitW: 6532,
    dischargeLimitW: 6532,
    cellCount: 50,
    cellMinV: 3.289,
    cellMaxV: 3.292,
    tempMinC: 23,
    tempMaxC: 27,
    serial: "BEP0005KXX11B10500118",
    rtuId: "0x2C225076",
  },
  LHS: {
    soc: 48,
    bmsSoc: 48,
    soh: 100,
    packVoltageV: 196.8,
    packCurrentA: 9.4,
    batteryWatts: 1846,
    chargeLimitW: 7812,
    dischargeLimitW: 7812,
    cellCount: 60,
    cellMinV: 3.277,
    cellMaxV: 3.283,
    tempMinC: 23,
    tempMaxC: 27,
    serial: "BEP0005KXX11B10500149",
    rtuId: "0x2C225095",
  },
};

/** Evenly distributed cell voltages in mV resolution (the wire stores mV). */
function cellVoltages(count: number, minV: number, maxV: number): number[] {
  const minMv = Math.round(minV * 1000);
  const maxMv = Math.round(maxV * 1000);
  const values: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const millivolts =
      count === 1 ? minMv : Math.round(minMv + ((maxMv - minMv) * index) / (count - 1));
    values.push(millivolts / 1000);
  }
  return values;
}

/** Sensor temperatures across the pack (pole+ / cells / pole- triples). */
function temperatures(count: number, minC: number, maxC: number): number[] {
  const values: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const raw = count === 1 ? minC : minC + ((maxC - minC) * index) / (count - 1);
    values.push(Math.round(raw));
  }
  return values;
}

function allGoodQuality(): Record<string, string> {
  const quality: Record<string, string> = {};
  for (const field of TELEMETRY_QUALITY_FIELDS) {
    quality[field] = "good";
  }
  return quality;
}

/** The capture's commissioned topology: BIC x 3 temperature sensors per unit. */
function temperatureSensorCount(cellCount: number): number {
  return (cellCount / 10) * 3;
}

/**
 * A unit-detail fixture for one of the three captured units (MID / RHS / LHS);
 * any other id gets the same generic shape with an honest absent identity.
 * Overrides win field-by-field, and explicit nulls are preserved.
 */
export function unitDetail(unitId: string, spec: Partial<WireUnitDetail> = {}): WireUnitDetail {
  const facts = FLEET_FACTS[unitId];
  const cellCount = facts?.cellCount ?? 60;
  const cells = cellVoltages(cellCount, facts?.cellMinV ?? 3.205, facts?.cellMaxV ?? 3.209);
  const temperatureCount = temperatureSensorCount(facts?.cellCount ?? 60);
  const temps = temperatures(temperatureCount, facts?.tempMinC ?? 23, facts?.tempMaxC ?? 28);
  const base: WireUnitDetail = {
    unit_id: unitId,
    lifecycle: "disarmed",
    device_identity: facts === undefined ? null : `${facts.serial} (RTU ${facts.rtuId})`,
    protocol_profile: "iot",
    connection_epoch: 3,
    wall_timestamp: DEFAULT_OCCURRED_AT,
    captured_at_mono: 75_231.5,
    sequence: 410_200,
    cell_captured_at_mono: 75_228.0,
    cell_sequence: 41_019,
    soc_pct: facts === undefined ? null : facts.soc,
    bms_soc_pct: facts === undefined ? null : facts.bmsSoc,
    soh_pct: facts === undefined ? null : facts.soh,
    pack_voltage_v: facts === undefined ? null : facts.packVoltageV,
    pack_current_a: facts === undefined ? null : facts.packCurrentA,
    battery_watts: facts === undefined ? null : facts.batteryWatts,
    dynamic_charge_limit_w: facts === undefined ? null : facts.chargeLimitW,
    dynamic_discharge_limit_w: facts === undefined ? null : facts.dischargeLimitW,
    cell_count: facts === undefined ? null : facts.cellCount,
    cell_min_v: facts === undefined ? null : facts.cellMinV,
    cell_max_v: facts === undefined ? null : facts.cellMaxV,
    cell_spread_mv:
      facts === undefined
        ? null
        : Math.round((facts.cellMaxV - facts.cellMinV) * 1000),
    temperature_min_c: facts === undefined ? null : facts.tempMinC,
    temperature_max_c: facts === undefined ? null : facts.tempMaxC,
    grid_power_w: spec.grid_power_w === undefined ? null : spec.grid_power_w,
    load_power_w: spec.load_power_w === undefined ? null : spec.load_power_w,
    active_faults: [],
    active_warnings: ["PCS_Warning0_1", "DCDC_Warning0_1"],
    cell_voltages_v: cells,
    temperatures_c: temps,
    quality: allGoodQuality(),
  };
  return { ...base, ...spec };
}

/**
 * The projection with every datum absent — the honest shape the facade builds
 * for a commissioned unit that has not published measurements (contract and
 * service.py `_unit_projection`: null, never zero-filled, never empty arrays
 * standing in for absent data).
 */
export function emptyUnitDetail(unitId: string): WireUnitDetail {
  return {
    unit_id: unitId,
    lifecycle: null,
    device_identity: null,
    protocol_profile: null,
    connection_epoch: null,
    wall_timestamp: null,
    captured_at_mono: null,
    sequence: null,
    cell_captured_at_mono: null,
    cell_sequence: null,
    soc_pct: null,
    bms_soc_pct: null,
    soh_pct: null,
    pack_voltage_v: null,
    pack_current_a: null,
    battery_watts: null,
    dynamic_charge_limit_w: null,
    dynamic_discharge_limit_w: null,
    cell_count: null,
    cell_min_v: null,
    cell_max_v: null,
    cell_spread_mv: null,
    temperature_min_c: null,
    temperature_max_c: null,
    grid_power_w: null,
    load_power_w: null,
    active_faults: null,
    active_warnings: null,
    cell_voltages_v: null,
    temperatures_c: null,
    quality: null,
  };
}

/**
 * One per-unit recovery row in the health view's `units` block
 * (service.py `health`, composed with the recovery port): the derived state,
 * its reasons (`reasons`, not `health_reasons`, inside this block), and the
 * remediation hint.
 */
export interface WireHealthUnit {
  readonly unit_id: string;
  readonly health_state: string | null;
  readonly reasons: readonly string[] | null;
  readonly remediation_hint: string | null;
}

/** The health envelope exactly as the facade serializes it (three facts,
 * plus the awareness layer's per-unit recovery block when composed). */
export function health(
  spec: {
    service_readiness?: { ready?: boolean; reasons?: string[] };
    control_readiness?: { ready?: boolean; reasons?: string[] };
    /** The recovery block; omit it entirely for a not-composed facade. */
    units?: readonly WireHealthUnit[];
  } = {},
): Health {
  const service = spec.service_readiness ?? {};
  const control = spec.control_readiness ?? {};
  return {
    liveness: { ok: true },
    service_readiness: {
      ready: service.ready ?? true,
      reasons: service.reasons ?? [],
    },
    control_readiness: {
      ready: control.ready ?? false,
      reasons: control.reasons ?? ["no_unit_armed"],
    },
    ...(spec.units === undefined ? {} : { units: [...spec.units] }),
  } as unknown as Health;
}

// ---------------------------------------------------------------------------
// Audit read model: AuditEvent fields + the store's integer sequence
// ---------------------------------------------------------------------------

/**
 * One audit entry as GET /api/v1/audit serializes it: the write model's
 * fields (`AuditEvent`) plus the store's own ordering key (`sequence`).
 * The kind key on the wire is `event_type`; `principal` is the caller's
 * subject string.
 */
export interface WireAuditEvent {
  readonly event_id: string;
  readonly occurred_at: string;
  readonly monotonic_offset_s: number;
  readonly process_instance_id: string;
  readonly event_type: string;
  readonly unit_id: string | null;
  readonly connection_epoch: number | null;
  readonly generation: number | null;
  readonly cycle_id: string | null;
  readonly principal: string;
  readonly source: string | null;
  readonly correlation_id: string;
  readonly intent_id: string | null;
  readonly policy_version: string | number;
  readonly configuration_version: number;
  readonly observation_sequences: Record<string, number>;
  readonly reason_codes: string[];
  readonly requested_active_w: number;
  readonly authorized_active_w: number;
  /**
   * Per-unit watt breakdowns (2026-08-23 fleet-row opacity fix): the intent's
   * own target map (null for scalar intents) and the decision's per-unit
   * authorized watts (null when no batch was minted). Pydantic always
   * serializes both — with null defaults for durable rows written before the
   * fields existed. A row composed from several concurrent intents (2026-08-24)
   * correlates to its cycle (`correlation_id: "cycle:..."`) and carries
   * `intent_id: null` — no single intent can honestly be named — plus each
   * unit's winning direction in `directions_by_unit`.
   */
  readonly requested_watts_by_unit: Record<string, number> | null;
  readonly authorized_watts_by_unit: Record<string, number> | null;
  readonly directions_by_unit: Record<string, string> | null;
  readonly request_fingerprint: string;
  readonly response_fingerprint: string;
  readonly result: string;
  readonly lifecycle: string;
  readonly sequence: number;
}

export function auditEvent(
  spec: Partial<WireAuditEvent> & { sequence: number; event_type: string },
): WireAuditEvent {
  const type = spec.event_type;
  const unitId = spec.unit_id ?? null;
  const unitSequences: Record<string, number> =
    spec.observation_sequences ?? (unitId === null ? {} : { [unitId]: spec.sequence * 10 });
  return {
    event_id: spec.event_id ?? `facade-${spec.sequence.toString(16)}`,
    occurred_at: spec.occurred_at ?? DEFAULT_OCCURRED_AT,
    monotonic_offset_s: spec.monotonic_offset_s ?? 12.5,
    process_instance_id: spec.process_instance_id ?? "energypod-9f2c",
    event_type: type,
    unit_id: spec.unit_id ?? null,
    connection_epoch: spec.connection_epoch ?? null,
    generation: spec.generation ?? null,
    cycle_id: spec.cycle_id ?? null,
    principal: spec.principal ?? "operator:home",
    source: spec.source ?? "manual",
    correlation_id: spec.correlation_id ?? `facade:${type}:req-${spec.sequence}`,
    intent_id: spec.intent_id ?? null,
    policy_version: spec.policy_version ?? "home-policy-1",
    configuration_version: spec.configuration_version ?? 4,
    observation_sequences: unitSequences,
    reason_codes: spec.reason_codes ?? [],
    requested_active_w: spec.requested_active_w ?? 0,
    authorized_active_w: spec.authorized_active_w ?? 0,
    requested_watts_by_unit: spec.requested_watts_by_unit ?? null,
    authorized_watts_by_unit: spec.authorized_watts_by_unit ?? null,
    directions_by_unit: spec.directions_by_unit ?? null,
    request_fingerprint: spec.request_fingerprint ?? "a1b2c3d4",
    response_fingerprint: spec.response_fingerprint ?? "e5f6a7b8",
    result: spec.result ?? defaultResultFor(type),
    lifecycle: spec.lifecycle ?? defaultLifecycleFor(type),
    sequence: spec.sequence,
  };
}

function defaultResultFor(eventType: string): string {
  switch (eventType) {
    case "control_decision":
      return "authorized";
    case "intent_accepted":
      return "accepted";
    case "unit_armed":
      return "armed";
    case "unit_disarmed":
      return "disarmed";
    case "emergency_stop":
      return "latched";
    case "stop_acknowledged":
    case "inhibit_acknowledged":
      return "acknowledged";
    case "authorization_revoked":
      return "revoked";
    default:
      return "authorized";
  }
}

function defaultLifecycleFor(eventType: string): string {
  switch (eventType) {
    case "emergency_stop":
      return "inhibited";
    case "stop_acknowledged":
    case "inhibit_acknowledged":
    case "authorization_revoked":
    case "unit_disarmed":
      return "disarmed";
    case "unit_armed":
      return "armed_idle";
    case "control_decision":
      return "active";
    default:
      return "disarmed";
  }
}

/**
 * Build an audit page for a mocked `getAudit`. The cast lives here and only
 * here: the shared client's `AuditEvent` annotation still names a `type`
 * field the wire does not carry (the wire key is `event_type`), so the true
 * shape is asserted once at this boundary instead of being fabricated per
 * suite.
 */
export function auditPage(events: readonly WireAuditEvent[], nextCursor: number | null = null): AuditPage {
  return { events: [...events] as unknown as AuditEvent[], next_cursor: nextCursor };
}
