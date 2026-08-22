// Console API client (real implementation).
// SHARED STUB: other view suites mock this same module with these exact names.
// The suite that owns the real implementation replaces the body only; the
// exported names and envelope shapes below are the contract.
// Envelopes come from docs/API_CONTRACTS.md.
//
// Real implementation (owner: src/api/client.test.ts). Pins from
// UI_CONTRACTS.md ("Data sources", "Authentication model") and the live
// service adapter (src/energypod/api/rest.py):
//
// - **Authentication.** The token handed to `createApiClient` lives in memory
//   and is sent as an `Authorization: Bearer <token>` header on every REST
//   call. It is never written into a URL, a query string, or storage.
//   Browsers cannot set headers on a WebSocket, so the event-stream handshake
//   buys a single-use ticket first (API_CONTRACTS.md, browser event-stream
//   handshake): `POST /api/v1/events/session` (Bearer, `observe`) returns
//   `{ticket, expires_in_s}`, and the socket then offers the subprotocols
//   `["energypod-events", ticket]`. Every connection buys its own ticket
//   because tickets are single-use; the socket URL carries only the `after`
//   cursor, and neither the token nor the ticket ever appears in a URL.
// - **Idempotency.** Every mutation carries an `Idempotency-Key` header the
//   service validates against its canonical identifier grammar (rest.py
//   `_ID_PATTERN`). The client generates a fresh key per call; a caller
//   retrying one operator action may pass a single key to reuse so the retry
//   replays as that action instead of creating a new one.
// - **Errors.** Every non-2xx response carrying the API error envelope
//   `{code, message, details, request_id}` rejects as an `ApiClientError`
//   (the TypedError pin; it structurally satisfies `ErrorEnvelope`) holding
//   those fields verbatim plus the HTTP `status`. The thrown error is the
//   single 401 signal: consumers test it with `isUnauthorizedError` and
//   respond by clearing the token and every cached view. Network-level
//   failures reject with code `network_error` and the original failure as
//   `cause`.
// - **Events.** `openEvents(afterSequence)` connects with the last seen
//   sequence as the `after` cursor and yields the authoritative `snapshot`
//   frame first, then ordered events. A `resync_required` frame is yielded
//   with its `reason` (and its `snapshot_sequence` recovery cursor when the
//   server sends one) and iteration then ends, so the caller refetches a
//   snapshot and reconnects with the last sequence as the cursor. A
//   `type: "error"` frame rejects the iteration with that envelope, its
//   `details` preserved verbatim. An unexpected close ends iteration without
//   an error; the caller decides how to retry.

export interface PowerFlow {
  direction: "CHARGE" | "DISCHARGE" | "IDLE";
  watts: number;
}

export interface UnitSnapshot {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number;
  quality: string;
  requested_power: PowerFlow;
  authorized_power: PowerFlow | null;
  measured_watts: number | null;
}

export interface Snapshot {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: UnitSnapshot[];
}

/**
 * The full latest-observation projection `GET /api/v1/units/{unit_id}` returns
 * (API_CONTRACTS.md, "Application service facade" — `unit_detail`): identity,
 * protocol profile, connection epoch, telemetry and cell sequences and capture
 * times, the scalar measurements, the complete cell-voltage and temperature
 * arrays, the per-field quality map, faults, and warnings. Every measurement
 * field is null when that datum was absent — the client passes the projection
 * through without interpretation.
 */
export interface UnitDetail {
  unit_id: string;
  lifecycle: string | null;
  device_identity: string | null;
  protocol_profile: string | null;
  connection_epoch: number | null;
  wall_timestamp: string | null;
  captured_at_mono: number | null;
  sequence: number | null;
  cell_captured_at_mono: number | null;
  cell_sequence: number | null;
  soc_pct: number | null;
  bms_soc_pct: number | null;
  soh_pct: number | null;
  pack_voltage_v: number | null;
  pack_current_a: number | null;
  battery_watts: number | null;
  dynamic_charge_limit_w: number | null;
  dynamic_discharge_limit_w: number | null;
  cell_count: number | null;
  cell_min_v: number | null;
  cell_max_v: number | null;
  cell_spread_mv: number | null;
  temperature_min_c: number | null;
  temperature_max_c: number | null;
  active_faults: string[] | null;
  active_warnings: string[] | null;
  cell_voltages_v: number[] | null;
  temperatures_c: number[] | null;
  quality: Record<string, string> | null;
}

/** Wire shape from the facade: liveness, dependency readiness, and control
 * readiness are three separate facts, each with its own reason list. */
export interface Health {
  liveness: { ok: boolean };
  service_readiness: { ready: boolean; reasons: string[] };
  control_readiness: { ready: boolean; reasons: string[] };
}

export interface AuditEvent {
  type: string;
  sequence: number;
  occurred_at: string;
  [key: string]: unknown;
}

export interface AuditPage {
  events: AuditEvent[];
  next_cursor: number | null;
}

export interface StreamEvent {
  type: string;
  sequence: number;
  [key: string]: unknown;
}

export interface ErrorEnvelope {
  code: string;
  message: string;
  details: Record<string, unknown> | null;
  request_id: string;
}

/**
 * The TypedError pin: every failure crossing this boundary rejects with this
 * error. It carries the API error envelope verbatim plus the HTTP `status`.
 */
export class ApiClientError extends Error implements ErrorEnvelope {
  readonly code: string;
  readonly details: Record<string, unknown> | null;
  readonly request_id: string;
  readonly status: number;

  constructor(envelope: ErrorEnvelope & { status?: number; cause?: unknown }) {
    super(envelope.message, envelope.cause === undefined ? undefined : { cause: envelope.cause });
    this.name = "ApiClientError";
    this.code = envelope.code;
    this.details = envelope.details;
    this.request_id = envelope.request_id;
    this.status = envelope.status ?? 0;
  }
}

/**
 * The token-cleared state is triggered by the thrown error alone: a 401
 * rejection is unauthorized, anything else is not. There is no second channel.
 */
export function isUnauthorizedError(error: unknown): error is ApiClientError {
  return error instanceof ApiClientError && error.status === 401;
}

export interface ApiClient {
  getSnapshot(): Promise<Snapshot>;
  /**
   * Optional forced fresh read (the shell's shared client supplies it; plain
   * clients need not). A view that just saw a frame proving the world moved
   * uses it to bypass the shell's short-lived snapshot cache.
   */
  refreshSnapshot?(): Promise<Snapshot>;
  getHealth(): Promise<Health>;
  /** One unit's full latest-observation projection (observe scope). */
  getUnitDetail(unitId: string): Promise<UnitDetail>;
  getAudit(limit: number, afterSequence?: number): Promise<AuditPage>;
  /** The optional key reuses one idempotency key across retries of a single
   * operator action; omitted, a fresh key is generated per call. */
  postIntent(
    body: Record<string, unknown>,
    idempotencyKey?: string,
  ): Promise<Record<string, unknown>>;
  postArm(unitIds: string[], idempotencyKey?: string): Promise<Record<string, unknown>>;
  postDisarm(unitIds: string[], idempotencyKey?: string): Promise<Record<string, unknown>>;
  postEmergencyStop(
    unitIds: string[],
    reason: string,
    idempotencyKey?: string,
  ): Promise<Record<string, unknown>>;
  postStopAcknowledgement(
    stopId: string,
    idempotencyKey?: string,
  ): Promise<Record<string, unknown>>;
  postInhibitAcknowledgement(
    unitId: string,
    idempotencyKey?: string,
  ): Promise<Record<string, unknown>>;
  openEvents(afterSequence?: number): AsyncIterable<StreamEvent>;
}

/** The subprotocol the events endpoint accepts (API_CONTRACTS.md). */
const EVENTS_SUBPROTOCOL = "energypod-events";

let idempotencyCounter = 0;

/**
 * A fresh idempotency key per call, kept inside the service's canonical
 * identifier grammar (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`, rest.py
 * `_ID_PATTERN`): mutations without it are refused with
 * `idempotency_key_required` before any safety action runs.
 */
function newIdempotencyKey(): string {
  idempotencyCounter += 1;
  return [
    "console",
    Date.now().toString(36),
    idempotencyCounter.toString(36),
    Math.random().toString(36).slice(2, 10),
  ].join("-");
}

function asDetails(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

async function toApiClientError(response: Response): Promise<ApiClientError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const envelope =
    payload !== null && typeof payload === "object" ? (payload as Record<string, unknown>) : null;
  const code =
    envelope !== null && typeof envelope.code === "string" && envelope.code !== ""
      ? envelope.code
      : `http_${response.status}`;
  const message =
    envelope !== null && typeof envelope.message === "string" && envelope.message !== ""
      ? envelope.message
      : response.statusText !== ""
        ? response.statusText
        : "The request failed";
  return new ApiClientError({
    status: response.status,
    code,
    message,
    details: envelope === null ? null : asDetails(envelope.details),
    request_id:
      envelope !== null && typeof envelope.request_id === "string" ? envelope.request_id : "",
  });
}

function parseFrame(raw: string): StreamEvent | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      if (typeof record.type === "string" && record.type !== "") {
        return record as unknown as StreamEvent;
      }
    }
  } catch {
    // An unreadable frame is reported as an invalid-frame rejection below.
  }
  return null;
}

function eventsUrl(afterSequence?: number): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const cursor =
    afterSequence === undefined ? "" : `?after=${encodeURIComponent(String(afterSequence))}`;
  return `${scheme}//${window.location.host}/api/v1/events${cursor}`;
}

async function* streamEvents(
  url: string,
  fetchTicket: () => Promise<string>,
): AsyncGenerator<StreamEvent, void, unknown> {
  // Browsers cannot set handshake headers, so the credential for this socket
  // is the single-use ticket bought with the bearer token just before
  // connecting. A refused ticket request rejects here, before any socket is
  // opened, through the same error channel as every other call.
  const ticket = await fetchTicket();
  const socket = new globalThis.WebSocket(url, [EVENTS_SUBPROTOCOL, ticket]);
  const pending: StreamEvent[] = [];
  let failure: unknown = null;
  let finished = false;
  let wake: (() => void) | null = null;
  const release = (): void => {
    const notify = wake;
    wake = null;
    notify?.();
  };

  socket.addEventListener("message", (event) => {
    const frame = parseFrame(String(event.data));
    if (frame === null) {
      failure = new ApiClientError({
        status: 0,
        code: "invalid_event_frame",
        message: "The event stream sent an unreadable message",
        details: null,
        request_id: "",
      });
      finished = true;
    } else if (frame.type === "resync_required") {
      // The discontinuity itself is data: surface the reason (and recovery
      // cursor), then end iteration so the caller resynchronizes.
      pending.push(frame);
      finished = true;
    } else if (frame.type === "error") {
      failure = new ApiClientError({
        status: 1011,
        code:
          typeof frame.code === "string" && frame.code !== "" ? frame.code : "event_stream_error",
        message:
          typeof frame.message === "string" && frame.message !== ""
            ? frame.message
            : "The event stream failed",
        details: asDetails(frame.details),
        request_id: typeof frame.request_id === "string" ? frame.request_id : "",
      });
      finished = true;
    } else {
      pending.push(frame);
    }
    release();
  });
  socket.addEventListener("close", () => {
    // A lost connection ends iteration; it is never an error by itself.
    finished = true;
    release();
  });
  socket.addEventListener("error", () => {
    // A transport error is always followed by close; wait for it so already
    // accepted frames are still delivered first.
  });

  try {
    while (true) {
      // Register the waiter before draining so a frame arriving between reads
      // is never missed.
      const notified = new Promise<void>((resolve) => {
        wake = resolve;
      });
      while (pending.length > 0) {
        const next = pending.shift();
        if (next !== undefined) {
          yield next;
        }
      }
      if (failure !== null) {
        throw failure;
      }
      if (finished) {
        return;
      }
      await notified;
    }
  } finally {
    if (socket.readyState === 0 || socket.readyState === 1) {
      socket.close(1000, "console closed the stream");
    }
  }
}

export function createApiClient(token: string): ApiClient {
  const bearer = `Bearer ${token}`;

  async function request<T>(
    path: string,
    init: { method?: string; body?: unknown; idempotencyKey?: string } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Authorization: bearer };
    if (init.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = init.idempotencyKey;
    }
    let body: string | undefined;
    if (init.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(init.body);
    }
    let response: Response;
    const fetchInit: RequestInit & { headers: Record<string, string> } = {
      method: init.method ?? "GET",
      headers,
    };
    if (body !== undefined) {
      fetchInit.body = body;
    }
    try {
      response = await fetch(path, fetchInit);
    } catch (cause) {
      throw new ApiClientError({
        status: 0,
        code: "network_error",
        message: "The EnergyPod service could not be reached",
        details: null,
        request_id: "",
        cause,
      });
    }
    if (!response.ok) {
      throw await toApiClientError(response);
    }
    return (await response.json()) as T;
  }

  /** Buy the single-use events ticket with the bearer token; each connection
   * needs its own because a consumed or expired ticket is refused. */
  const fetchEventsTicket = async (): Promise<string> => {
    const session = await request<{ ticket?: unknown }>("/api/v1/events/session", {
      method: "POST",
    });
    if (typeof session.ticket !== "string" || session.ticket === "") {
      throw new ApiClientError({
        status: 0,
        code: "event_session_invalid",
        message: "The event session response carried no ticket",
        details: null,
        request_id: "",
      });
    }
    return session.ticket;
  };

  const withKey = (idempotencyKey: string | undefined): string =>
    idempotencyKey ?? newIdempotencyKey();

  return {
    getSnapshot: () => request<Snapshot>("/api/v1/snapshot"),
    getHealth: () => request<Health>("/api/v1/health"),
    getUnitDetail: (unitId: string) =>
      request<UnitDetail>(`/api/v1/units/${encodeURIComponent(unitId)}`),
    getAudit: (limit: number, afterSequence?: number) => {
      const params = new URLSearchParams({ limit: String(limit) });
      if (afterSequence !== undefined) {
        params.set("after_sequence", String(afterSequence));
      }
      return request<AuditPage>(`/api/v1/audit?${params.toString()}`);
    },
    postIntent: (body: Record<string, unknown>, idempotencyKey?: string) =>
      request<Record<string, unknown>>("/api/v1/intents", {
        method: "POST",
        body,
        idempotencyKey: withKey(idempotencyKey),
      }),
    postArm: (unitIds: string[], idempotencyKey?: string) =>
      request<Record<string, unknown>>("/api/v1/arm", {
        method: "POST",
        body: { unit_ids: unitIds, confirmation: "ARM" },
        idempotencyKey: withKey(idempotencyKey),
      }),
    postDisarm: (unitIds: string[], idempotencyKey?: string) =>
      request<Record<string, unknown>>("/api/v1/disarm", {
        method: "POST",
        body: { unit_ids: unitIds },
        idempotencyKey: withKey(idempotencyKey),
      }),
    postEmergencyStop: (unitIds: string[], reason: string, idempotencyKey?: string) =>
      request<Record<string, unknown>>("/api/v1/emergency-stop", {
        method: "POST",
        body: { unit_ids: unitIds, reason },
        idempotencyKey: withKey(idempotencyKey),
      }),
    postStopAcknowledgement: (stopId: string, idempotencyKey?: string) =>
      request<Record<string, unknown>>(
        `/api/v1/emergency-stop/${encodeURIComponent(stopId)}/acknowledge`,
        {
          method: "POST",
          body: { confirmation: "ACKNOWLEDGE" },
          idempotencyKey: withKey(idempotencyKey),
        },
      ),
    postInhibitAcknowledgement: (unitId: string, idempotencyKey?: string) =>
      request<Record<string, unknown>>(
        `/api/v1/units/${encodeURIComponent(unitId)}/inhibit/acknowledge`,
        {
          method: "POST",
          body: { confirmation: "ACKNOWLEDGE" },
          idempotencyKey: withKey(idempotencyKey),
        },
      ),
    openEvents: (afterSequence?: number) =>
      streamEvents(eventsUrl(afterSequence), fetchEventsTicket),
  };
}
