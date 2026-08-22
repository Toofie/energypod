/**
 * Acceptance contract for the console's guarded-API boundary
 * (`web/src/api/client.ts`), pinned against UI_CONTRACTS.md
 * ("Data sources", "Authentication model") and the envelope shapes of
 * API_CONTRACTS.md. The network boundary itself is mocked: global `fetch`
 * with hand-rolled responses and a minimal `WebSocket` stub mirroring the
 * browser constructor (url + subprotocols — browsers cannot set handshake
 * headers). No server state is constructed outside the documented envelopes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, createApiClient, isUnauthorizedError } from "./client";

const TOKEN = "operator-token-123";
const BEARER = `Bearer ${TOKEN}`;
const SOCKET_BASE = `ws://${window.location.host}/api/v1/events`;
const EVENTS_SUBPROTOCOL = "energypod-events";
// Canonical identifier grammar the service accepts for Idempotency-Key
// values (src/energypod/api/rest.py `_ID_PATTERN`); a non-conforming key is
// refused with 400 idempotency_key_required before any safety action runs.
const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

// Server-side fixtures: exact envelope shapes from the live API
// (src/energypod/application/service.py). The client passes them through
// without interpretation, so they stay untyped JSON here. Health uses the
// facade's real object shapes ({ok} / {ready, reasons}), and enum values use
// the lowercase wire casing the service serializes.
const SNAPSHOT = {
  site_id: "site-1",
  snapshot_sequence: 42,
  captured_at: "2026-08-22T10:00:00Z",
  units: [
    {
      unit_id: "MID",
      lifecycle: "armed_idle",
      telemetry_age_s: 0.4,
      quality: "good",
      requested_power: { direction: "discharge", watts: 1500 },
      authorized_power: { direction: "discharge", watts: 900 },
      measured_watts: -880,
    },
    {
      unit_id: "LHS",
      lifecycle: "inhibited",
      telemetry_age_s: null,
      quality: "missing",
      requested_power: { direction: "idle", watts: 0 },
      authorized_power: null,
      measured_watts: null,
    },
  ],
};

const HEALTH = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: false, reasons: ["no_unit_qualified"] },
};

const AUDIT_PAGE = {
  events: [
    {
      sequence: 42,
      occurred_at: "2026-08-22T10:00:01Z",
      type: "intent.accepted",
      payload: { intent_id: "intent-7" },
    },
    {
      sequence: 41,
      occurred_at: "2026-08-22T09:59:58Z",
      type: "observation.decision",
      payload: { decision: "authorized" },
    },
  ],
  next_cursor: 41,
};

// The browser event-stream handshake cannot carry headers, so the console
// first buys a single-use ticket (API_CONTRACTS.md, browser event-stream
// handshake) and then offers it as a WebSocket subprotocol.
const EVENTS_SESSION = { ticket: "events-ticket-1", expires_in_s: 30 };

// --- hand-rolled fetch boundary ---------------------------------------------

let fetchMock: ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200, statusText = ""): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

interface RecordedCall {
  path: string;
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
}

function lastCall(): RecordedCall {
  const call = fetchMock.mock.calls.at(-1);
  if (call === undefined) {
    throw new Error("expected the client to have called fetch");
  }
  const [path, init] = call as [string, Omit<RecordedCall, "path">];
  return { path, ...init };
}

// --- minimal WebSocket stub ---------------------------------------------------

/** Mirrors the browser WebSocket surface the client may use: a url plus
 * optional subprotocols. There is no headers API in browsers, so the ticket
 * travels as the second subprotocol. */
class WebSocketStub {
  static instances: WebSocketStub[] = [];

  url: string;
  protocols: string | string[] | undefined;
  readyState = 1;
  closedByClient = false;
  private readonly listeners = new Map<string, Array<(event: { data?: unknown }) => void>>();

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = protocols;
    WebSocketStub.instances.push(this);
  }

  addEventListener(type: string, listener: (event: { data?: unknown }) => void): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }

  serverSends(frame: string): void {
    for (const listener of this.listeners.get("message") ?? []) {
      listener({ data: frame });
    }
  }

  serverCloses(): void {
    this.readyState = 3;
    for (const listener of this.listeners.get("close") ?? []) {
      listener({});
    }
  }

  close(): void {
    this.closedByClient = true;
    this.readyState = 3;
  }
}

let client: ReturnType<typeof createApiClient>;

beforeEach(() => {
  fetchMock = vi.fn();
  // Default: every event-stream connection can buy a single-use ticket.
  fetchMock.mockResolvedValue(jsonResponse(EVENTS_SESSION));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", WebSocketStub);
  WebSocketStub.instances.length = 0;
  client = createApiClient(TOKEN);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function rejectionOf(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error("expected the call to reject");
    },
    (error: unknown) => error,
  );
}

/** Start consuming the stream and wait until the client has opened the socket. */
async function connectStream(afterSequence?: number) {
  const existing = WebSocketStub.instances.length;
  const iterator = client.openEvents(afterSequence)[Symbol.asyncIterator]();
  const first = iterator.next();
  // Wait for a NEW socket: a test may connect more than once.
  await vi.waitUntil(() => WebSocketStub.instances.length > existing);
  const socket = WebSocketStub.instances.at(-1);
  if (socket === undefined) {
    throw new Error("expected the client to have opened an event socket");
  }
  return { iterator, first, socket };
}

describe("createApiClient REST boundary", () => {
  it("fetches the fleet snapshot with the in-memory token as a bearer header", async () => {
    fetchMock.mockResolvedValue(jsonResponse(SNAPSHOT));
    const snapshot = await client.getSnapshot();
    expect(snapshot).toEqual(SNAPSHOT);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = lastCall();
    expect(call.path).toBe("/api/v1/snapshot");
    expect(call.method).toBe("GET");
    expect(call.headers?.Authorization).toBe(BEARER);
  });

  it("fetches health with its three readiness facts separated", async () => {
    fetchMock.mockResolvedValue(jsonResponse(HEALTH));
    const health = await client.getHealth();
    expect(health).toEqual(HEALTH);
    const call = lastCall();
    expect(call.path).toBe("/api/v1/health");
    expect(call.headers?.Authorization).toBe(BEARER);
  });

  it("loads a page of audit history with a limit and no cursor on the first page", async () => {
    fetchMock.mockResolvedValue(jsonResponse(AUDIT_PAGE));
    const page = await client.getAudit(50);
    expect(page).toEqual(AUDIT_PAGE);
    const call = lastCall();
    expect(call.path).toBe("/api/v1/audit?limit=50");
  });

  it("resumes Load more after the previous page's next_cursor", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(AUDIT_PAGE))
      .mockResolvedValueOnce(jsonResponse({ events: [], next_cursor: null }));
    const firstPage = await client.getAudit(50);
    expect(firstPage.next_cursor).toBe(AUDIT_PAGE.next_cursor);
    await client.getAudit(50, firstPage.next_cursor ?? 0);
    const call = lastCall();
    expect(call.path).toBe(`/api/v1/audit?limit=50&after_sequence=${AUDIT_PAGE.next_cursor}`);
  });

  it("sends the bearer token from the in-memory token on every endpoint call", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await client.getSnapshot();
    await client.getHealth();
    await client.getAudit(10);
    await client.postIntent({ unit_ids: ["MID"], direction: "charge", watts: 900, ttl_s: 60 });
    await client.postArm(["MID"]);
    await client.postDisarm(["MID"]);
    await client.postEmergencyStop(["MID"], "test stop");
    await client.postStopAcknowledgement("stop-1");
    await client.postInhibitAcknowledgement("LHS");
    expect(fetchMock).toHaveBeenCalledTimes(9);
    for (const call of fetchMock.mock.calls) {
      const init = (call[1] ?? {}) as { headers?: Record<string, string> };
      expect(init.headers?.Authorization).toBe(BEARER);
    }
  });

  it("generates a fresh, canonical Idempotency-Key for every mutation and none for reads", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await client.getSnapshot();
    await client.getHealth();
    await client.getAudit(10);
    await client.postIntent({ unit_ids: ["MID"], direction: "charge", watts: 900, ttl_s: 60 });
    await client.postArm(["MID"]);
    await client.postDisarm(["MID"]);
    await client.postEmergencyStop(["MID"], "test stop");
    await client.postStopAcknowledgement("stop-1");
    await client.postInhibitAcknowledgement("LHS");
    expect(fetchMock).toHaveBeenCalledTimes(9);
    const keys: string[] = [];
    for (const [index, call] of fetchMock.mock.calls.entries()) {
      const init = (call[1] ?? {}) as { headers?: Record<string, string> };
      const key = init.headers?.["Idempotency-Key"];
      if (index < 3) {
        // Reads carry no key: only mutations are idempotancy-guarded.
        expect(key).toBeUndefined();
      } else {
        expect(key).toMatch(IDEMPOTENCY_KEY_PATTERN);
        keys.push(String(key));
      }
    }
    // One operator action = one key: every generated key is distinct.
    expect(new Set(keys).size).toBe(6);
  });

  it("reuses a caller-supplied Idempotency-Key so one operator action retries safely", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ stop_id: "stop-17" }, 202));
    // The caller retries the same emergency stop (e.g. after an unreadable
    // response) under one key, so the service replays the original action.
    await client.postEmergencyStop(["MID"], "grid work", "operator-retry-7");
    await client.postEmergencyStop(["MID"], "grid work", "operator-retry-7");
    const keys = fetchMock.mock.calls.map(
      (call) =>
        ((call[1] ?? {}) as { headers?: Record<string, string> }).headers?.["Idempotency-Key"],
    );
    expect(keys).toEqual(["operator-retry-7", "operator-retry-7"]);
  });

  it("submits a dispatch intent to the intents endpoint unchanged", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ intent_id: "intent-7", status: "accepted" }, 202));
    const body = {
      unit_ids: ["MID", "RHS"],
      direction: "discharge",
      watts: 1500,
      ttl_s: 120,
      reason: "evening peak",
    };
    const result = await client.postIntent(body);
    expect(result).toEqual({ intent_id: "intent-7", status: "accepted" });
    const call = lastCall();
    expect(call.path).toBe("/api/v1/intents");
    expect(call.method).toBe("POST");
    expect(JSON.parse(String(call.body))).toEqual(body);
  });

  it("arms units with the explicit ARM confirmation", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ results: [{ unit_id: "MID", status: "armed" }] }));
    await client.postArm(["MID", "RHS"]);
    const call = lastCall();
    expect(call.path).toBe("/api/v1/arm");
    expect(call.method).toBe("POST");
    expect(JSON.parse(String(call.body))).toEqual({
      unit_ids: ["MID", "RHS"],
      confirmation: "ARM",
    });
  });

  it("disarms units on the disarm endpoint with the contracted body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ results: [] }));
    await client.postDisarm(["MID"]);
    const call = lastCall();
    expect(call.path).toBe("/api/v1/disarm");
    expect(call.method).toBe("POST");
    // API_CONTRACTS.md: POST /api/v1/disarm mirrors arm (same scope, no
    // interactive-principal requirement); the body is exactly {unit_ids}.
    expect(JSON.parse(String(call.body))).toEqual({ unit_ids: ["MID"] });
  });

  it("requests an emergency stop with the affected units and the reason", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ stop_id: "stop-17" }, 202));
    await client.postEmergencyStop(["MID", "RHS", "LHS"], "grid work");
    const call = lastCall();
    expect(call.path).toBe("/api/v1/emergency-stop");
    expect(call.method).toBe("POST");
    expect(JSON.parse(String(call.body))).toEqual({
      unit_ids: ["MID", "RHS", "LHS"],
      reason: "grid work",
    });
  });

  it("acknowledges a stop by its exact stop id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ acknowledged: true }));
    await client.postStopAcknowledgement("stop-17");
    const call = lastCall();
    expect(call.path).toBe("/api/v1/emergency-stop/stop-17/acknowledge");
    expect(call.method).toBe("POST");
    expect(JSON.parse(String(call.body))).toEqual({ confirmation: "ACKNOWLEDGE" });
  });

  it("acknowledges a latched inhibit for exactly one unit", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ acknowledged: true }));
    await client.postInhibitAcknowledgement("LHS");
    const call = lastCall();
    expect(call.path).toBe("/api/v1/units/LHS/inhibit/acknowledge");
    expect(call.method).toBe("POST");
    expect(JSON.parse(String(call.body))).toEqual({ confirmation: "ACKNOWLEDGE" });
  });
});

describe("error envelope contract", () => {
  it("surfaces a refused action's error envelope verbatim", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "unit_not_qualified",
          message: "The unit is not qualified for arming",
          details: { unit_id: "LHS", reason: "inhibit_latched" },
          request_id: "req-4042",
        },
        422,
      ),
    );
    const error = await rejectionOf(client.postArm(["LHS"]));
    expect(error).toBeInstanceOf(ApiClientError);
    expect(error).toMatchObject({
      code: "unit_not_qualified",
      message: "The unit is not qualified for arming",
      details: { unit_id: "LHS", reason: "inhibit_latched" },
      request_id: "req-4042",
      status: 422,
    });
  });

  it("signals a 401 as a typed unauthorized error so the console clears its session", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "authentication_required",
          message: "Bearer authentication is required",
          details: {},
          request_id: "req-77",
        },
        401,
      ),
    );
    const error = await rejectionOf(client.getSnapshot());
    expect(error).toBeInstanceOf(ApiClientError);
    expect(isUnauthorizedError(error)).toBe(true);
    expect(error).toMatchObject({
      code: "authentication_required",
      message: "Bearer authentication is required",
      request_id: "req-77",
      status: 401,
    });
  });

  it("treats a 401 on any call, including control actions, as unauthorized", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "authentication_required",
          message: "Bearer authentication is required",
          details: {},
          request_id: "req-78",
        },
        401,
      ),
    );
    const error = await rejectionOf(client.postEmergencyStop(["MID"], "stop"));
    expect(isUnauthorizedError(error)).toBe(true);
  });

  it("does not misread a scope refusal as an authorization loss", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "insufficient_scope",
          message: "The credential lacks permission",
          details: { required: "dispatch" },
          request_id: "req-79",
        },
        403,
      ),
    );
    const error = await rejectionOf(
      client.postIntent({ unit_ids: ["MID"], direction: "charge", watts: 500, ttl_s: 60 }),
    );
    expect(isUnauthorizedError(error)).toBe(false);
    expect(error).toMatchObject({
      code: "insufficient_scope",
      message: "The credential lacks permission",
      status: 403,
    });
  });

  it("wraps a network failure in the typed error instead of leaking the raw TypeError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await rejectionOf(client.getSnapshot());
    expect(error).toBeInstanceOf(ApiClientError);
    expect(isUnauthorizedError(error)).toBe(false);
    const apiError = error as ApiClientError;
    expect(apiError.code).toBe("network_error");
    expect(apiError.cause).toBeInstanceOf(TypeError);
  });

  it("still rejects with a typed envelope when the server's error body is unreadable", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: () => Promise.reject(new SyntaxError("Unexpected token '<'")),
    } as unknown as Response);
    const error = await rejectionOf(client.getHealth());
    expect(error).toBeInstanceOf(ApiClientError);
    const apiError = error as ApiClientError;
    expect(apiError.code).toBe("http_502");
    expect(apiError.message).toBe("Bad Gateway");
    expect(apiError.status).toBe(502);
  });
});

describe("event stream boundary (openEvents)", () => {
  it("buys a single-use ticket with the bearer token and offers it as the events subprotocol", async () => {
    const { iterator, first, socket } = await connectStream(41);
    // The ticket is bought first, with the bearer token, over REST.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const sessionCall = lastCall();
    expect(sessionCall.path).toBe("/api/v1/events/session");
    expect(sessionCall.method).toBe("POST");
    expect(sessionCall.headers?.Authorization).toBe(BEARER);
    // Browsers cannot set handshake headers: the ticket travels as the
    // second offered subprotocol, never in the URL.
    expect(socket.protocols).toEqual([EVENTS_SUBPROTOCOL, EVENTS_SESSION.ticket]);
    expect(socket.url).toBe(`${SOCKET_BASE}?after=41`);
    expect(socket.url).not.toContain(TOKEN);
    expect(socket.url).not.toContain(EVENTS_SESSION.ticket);
    socket.serverSends(JSON.stringify({ type: "snapshot", sequence: 41, data: SNAPSHOT }));
    await first;
    socket.serverCloses();
    await expect(iterator.next()).resolves.toMatchObject({ done: true });
  });

  it("buys a fresh single-use ticket for every connection", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ticket: "events-ticket-a", expires_in_s: 30 }))
      .mockResolvedValueOnce(jsonResponse({ ticket: "events-ticket-b", expires_in_s: 30 }));
    const first = await connectStream(41);
    expect(first.socket.protocols).toEqual([EVENTS_SUBPROTOCOL, "events-ticket-a"]);
    first.socket.serverCloses();
    await expect(first.iterator.next()).resolves.toMatchObject({ done: true });
    const second = await connectStream(41);
    expect(second.socket.protocols).toEqual([EVENTS_SUBPROTOCOL, "events-ticket-b"]);
    second.socket.serverCloses();
    await expect(second.iterator.next()).resolves.toMatchObject({ done: true });
  });

  it("rejects before connecting when the ticket request is refused as a 401", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          code: "authentication_required",
          message: "Bearer authentication is required",
          details: {},
          request_id: "req-81",
        },
        401,
      ),
    );
    const iterator = client.openEvents()[Symbol.asyncIterator]();
    const error = await rejectionOf(iterator.next());
    expect(error).toBeInstanceOf(ApiClientError);
    expect(isUnauthorizedError(error)).toBe(true);
    expect(error).toMatchObject({
      code: "authentication_required",
      request_id: "req-81",
      status: 401,
    });
    // No socket is opened without a valid ticket.
    expect(WebSocketStub.instances.length).toBe(0);
  });

  it("yields the authoritative snapshot first and then ordered events", async () => {
    const { iterator, first, socket } = await connectStream(41);
    socket.serverSends(JSON.stringify({ type: "snapshot", sequence: 41, data: SNAPSHOT }));
    const snapshotFrame = await first;
    expect(snapshotFrame.done).toBe(false);
    expect(snapshotFrame.value).toEqual({ type: "snapshot", sequence: 41, data: SNAPSHOT });

    const secondRead = iterator.next();
    socket.serverSends(
      JSON.stringify({
        type: "intent.accepted",
        sequence: 42,
        occurred_at: "2026-08-22T10:00:01Z",
        payload: { intent_id: "intent-7" },
      }),
    );
    const eventFrame = await secondRead;
    expect(eventFrame.value).toMatchObject({ type: "intent.accepted", sequence: 42 });

    const thirdRead = iterator.next();
    socket.serverCloses();
    await expect(thirdRead).resolves.toMatchObject({ done: true });
    expect(socket.closedByClient).toBe(false);
  });

  it("surfaces a resync discontinuity with its reason and recovery cursor, then ends iteration", async () => {
    const { iterator, first, socket } = await connectStream(41);
    socket.serverSends(JSON.stringify({ type: "snapshot", sequence: 41, data: SNAPSHOT }));
    await first;
    socket.serverSends(
      JSON.stringify({ type: "resync_required", reason: "sequence_gap", snapshot_sequence: 57 }),
    );
    const resync = await iterator.next();
    expect(resync.value).toMatchObject({
      type: "resync_required",
      reason: "sequence_gap",
      snapshot_sequence: 57,
    });
    await expect(iterator.next()).resolves.toMatchObject({ done: true });
    expect(socket.closedByClient).toBe(true);
  });

  it("ends iteration after a resync marker even when it is the very first frame", async () => {
    const { iterator, first, socket } = await connectStream();
    expect(socket.url).toBe(SOCKET_BASE);
    socket.serverSends(JSON.stringify({ type: "resync_required", reason: "invalid_snapshot" }));
    const resync = await first;
    expect(resync.value).toMatchObject({ type: "resync_required", reason: "invalid_snapshot" });
    await expect(iterator.next()).resolves.toMatchObject({ done: true });
  });

  it("rejects with the error envelope, details included, when the stream fails after accept", async () => {
    const { first, socket } = await connectStream(41);
    socket.serverSends(
      JSON.stringify({
        type: "error",
        code: "internal_error",
        message: "The event stream failed",
        details: { unit_id: "MID", sequence: 42 },
        request_id: "req-55",
      }),
    );
    const error = await rejectionOf(first);
    expect(error).toBeInstanceOf(ApiClientError);
    expect(error).toMatchObject({
      code: "internal_error",
      message: "The event stream failed",
      details: { unit_id: "MID", sequence: 42 },
      request_id: "req-55",
    });
    expect(isUnauthorizedError(error)).toBe(false);
  });
});
