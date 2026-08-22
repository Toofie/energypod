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
 *   `_unit_view` / `health` (service.py). The snapshot unit carries exactly
 *   `unit_id, lifecycle, telemetry_age_s, quality, requested_power,
 *   authorized_power, measured_watts` — no charge, temperature, cell, or
 *   inhibit fields — and `quality` is the facade's own projection vocabulary
 *   `good | degraded | bad | missing` (never `stale`/`suspect`).
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

/** The facade accepted one dispatch intent; it grants nothing by itself. */
export const INTENT_ACCEPTED = "intent.accepted" as const;

export const UNIT_ARMED = "unit.armed" as const;
export const UNIT_DISARMED = "unit.disarmed" as const;
export const EMERGENCY_STOP_LATCHED = "emergency_stop.latched" as const;
export const EMERGENCY_STOP_ACKNOWLEDGED = "emergency_stop.acknowledged" as const;
export const INHIBIT_ACKNOWLEDGED = "inhibit.acknowledged" as const;

/** Every event type the composed service publishes, as a runtime checklist. */
export const PUBLISHED_EVENT_TYPES: readonly string[] = [
  OBSERVATION_PUBLISHED,
  AUDIT_APPENDED,
  AUTHORIZATION_REVOKED,
  INTENT_ACCEPTED,
  UNIT_ARMED,
  UNIT_DISARMED,
  EMERGENCY_STOP_LATCHED,
  EMERGENCY_STOP_ACKNOWLEDGED,
  INHIBIT_ACKNOWLEDGED,
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

/** The summary the audit port publishes for every durable append. */
export interface AuditAppendedPayload {
  readonly event_id: string;
  readonly event_type: string;
  readonly unit_id: string | null;
  readonly generation: number | null;
  readonly result: string;
  readonly reason_codes: string[];
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

export function intentAccepted(
  sequence: number,
  intent: {
    principal?: string;
    intent_id?: string;
    direction?: string;
    watts?: number;
    unit_ids?: readonly string[];
  } = {},
  occurredAt: string = DEFAULT_OCCURRED_AT,
): EventFrame<{
  principal: string;
  intent_id: string;
  direction: string;
  watts: number;
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
  readonly inhibit?: WireInhibitState | null;
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

export interface WireSnapshot {
  readonly site_id: string;
  readonly snapshot_sequence: number;
  readonly captured_at: string;
  readonly units: WireUnitSnapshot[];
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

/** The health envelope exactly as the facade serializes it (three facts). */
export function health(
  spec: {
    service_readiness?: { ready?: boolean; reasons?: string[] };
    control_readiness?: { ready?: boolean; reasons?: string[] };
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
  };
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
