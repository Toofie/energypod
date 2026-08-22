/**
 * The composed-app integration suite: the REAL shell, wired exactly as
 * production wires it, talking through the REAL API client to a mocked
 * network.
 *
 * This is the layer whose absence let the seam defects ship: every isolated
 * suite renders one component with its own client stub, so no test ever
 * proved that the shell's injected views actually receive the session's
 * client, that the frames the service publishes survive the SharedDataPlane
 * fan-out into real views, or that a mid-session authority change reaches
 * the banner without a reconnect. The isolated suites stay green through
 * prop drift and wire drift; this suite cannot, because nothing here is
 * stubbed below the network.
 *
 * Composition under test (the exact wiring of web/src/main.tsx):
 * - AppShell with the real view registry — HomeView, BatteriesView, NowView,
 *   ActivityView — assigned directly against `ViewRegistry` so a prop drift
 *   at this seam is a compile error here too (main.tsx mounts on import, so
 *   its registry const cannot be imported; this replica is kept verbatim and
 *   type-checked with the same `satisfies` constraint, never adapted/cast).
 * - The real web/src/api/client.ts: `createApiClient` is NOT mocked. REST
 *   goes through `fetch` (stubbed with vi.stubGlobal) and the event stream
 *   through the browser WebSocket constructor (stubbed with vi.stubGlobal),
 *   so the ticket handshake, the subprotocols, the `after` cursor, the
 *   bearer header, and the idempotency-key grammar are all exercised on the
 *   production code path.
 *
 * Network truth mirrored here (src/energypod/api/rest.py, events.py,
 * service.py — fixtures come from web/src/test/wire.ts, the console's single
 * wire-truth module, never hand-built):
 * - POST /api/v1/events/session (Bearer, observe) returns {ticket,
 *   expires_in_s}; the socket then offers subprotocols
 *   ["energypod-events", ticket]; its URL carries only the `after` cursor.
 * - Every connection's first frame is {type: "snapshot", sequence, data};
 *   then bus envelopes {type, sequence, occurred_at, payload} verbatim.
 * - The bus vocabulary and payloads are the facade's `_publish` calls; the
 *   snapshot/health/audit read models are `snapshot`/`health`/`recent_audit`.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";
import type { ShellView, ViewId, ViewRegistry } from "./views";
import { ActivityView } from "../views/activity/ActivityView";
import { BatteriesView } from "../views/batteries/BatteriesView";
import { HomeView } from "../views/home/HomeView";
import { NowView } from "../views/now/NowView";
import type { Health } from "../api/client";
import {
  auditAppended,
  auditEvent,
  auditPage,
  authorizationRevoked,
  emergencyStopLatched,
  health as wireHealth,
  intentAccepted,
  observationPublished,
  snapshot as wireSnapshot,
  snapshotFrame,
  unitArmed,
  unitSnapshot,
} from "../test/wire";
import type { WireAuditEvent, WireSnapshot } from "../test/wire";

// ---------------------------------------------------------------------------
// The production composition (web/src/main.tsx, replicated verbatim)
// ---------------------------------------------------------------------------

const views: ViewRegistry = {
  home: HomeView,
  batteries: BatteriesView,
  now: NowView,
  activity: ActivityView,
} satisfies Partial<Record<ViewId, ShellView>>;

const OPERATOR_TOKEN = "operator-session-token-7f3a";
const OCCURRED_AT = "2026-08-22T10:00:00Z";
/** The subprotocol the events endpoint accepts (rest.py EVENTS_SUBPROTOCOL). */
const EVENTS_SUBPROTOCOL = "energypod-events";
/** The service's canonical identifier grammar (rest.py `_ID_PATTERN`). */
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

type UserSession = ReturnType<typeof userEvent.setup>;

// ---------------------------------------------------------------------------
// The mocked network: a fetch stub (REST) + a WebSocket stub (events)
// ---------------------------------------------------------------------------

interface RecordedRequest {
  path: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

interface WireOutcomeRow {
  unit_id: string;
  status: string;
  reason: string;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function jsonResponse(status: number, payload: unknown): {
  ok: boolean;
  status: number;
  statusText: string;
  json: () => Promise<unknown>;
} {
  return {
    ok: status < 400,
    status,
    statusText: status < 400 ? "OK" : "Error",
    json: async () => clone(payload),
  };
}

function errorBody(code: string, message: string): Record<string, unknown> {
  return { code, message, details: null, request_id: `req-${code}` };
}

class FakeWebSocket {
  readonly url: string;
  readonly protocols: string[];
  readyState = 1;
  closeCalls: Array<{ code: number | undefined; reason: string | undefined }> = [];
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = Array.isArray(protocols) ? [...protocols] : protocols === undefined ? [] : [protocols];
    activeHarness?.onSocket(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  /** A server-sent text frame, exactly as the message event delivers it. */
  emitMessage(data: string): void {
    this.dispatch("message", { data });
  }

  /** The server closed the socket (an unexpected close from the client's view). */
  emitClose(code = 1006): void {
    this.readyState = 3;
    this.dispatch("close", { code });
  }

  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    this.readyState = 3;
  }

  private dispatch(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

/**
 * The fake EnergyPod service: REST routes over the stubbed fetch, plus one
 * events socket per connection that mirrors rest.py — the authoritative
 * snapshot frame first, then whatever the test publishes on the bus.
 */
class ComposedHarness {
  world: WireSnapshot;
  healthValue: Health;
  auditEvents: WireAuditEvent[] = [];
  /** While false, the ticket handshake is refused: the service is down. */
  up = true;
  armRows: WireOutcomeRow[] | null = null;
  intentResponse: Record<string, unknown> | null = null;
  requests: RecordedRequest[] = [];
  sockets: FakeWebSocket[] = [];
  ticketCount = 0;

  constructor(world: WireSnapshot, healthValue: Health = wireHealth()) {
    this.world = world;
    this.healthValue = healthValue;
  }

  install(): void {
    activeHarness = this;
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("fetch", this.fetch as unknown as typeof fetch);
  }

  /** rest.py sends the snapshot envelope to every accepted connection. */
  onSocket(socket: FakeWebSocket): void {
    this.sockets.push(socket);
    if (!this.up) {
      queueMicrotask(() => {
        socket.emitClose(1006);
      });
      return;
    }
    queueMicrotask(() => {
      socket.emitMessage(JSON.stringify(snapshotFrame(this.world)));
    });
  }

  /** Publish a bus envelope on the current connection. */
  publish(frame: unknown): void {
    const current = this.sockets[this.sockets.length - 1];
    if (current === undefined) {
      throw new Error("publish(): no socket has been opened yet");
    }
    current.emitMessage(JSON.stringify(frame));
  }

  closeCurrentSocket(): void {
    const current = this.sockets[this.sockets.length - 1];
    if (current === undefined) {
      throw new Error("closeCurrentSocket(): no socket has been opened yet");
    }
    current.emitClose(1006);
  }

  requestsFor(method: string, pathPrefix: string): RecordedRequest[] {
    return this.requests.filter(
      (request) => request.method === method && request.path.startsWith(pathPrefix),
    );
  }

  ticketsBought(): number {
    return this.requestsFor("POST", "/api/v1/events/session").length;
  }

  private readonly fetch = async (
    input: unknown,
    init?: { method?: string; headers?: unknown; body?: unknown },
  ): Promise<ReturnType<typeof jsonResponse>> => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = typeof init?.body === "string" ? init.body : undefined;
    this.requests.push({ path, method, headers, body });

    if (method === "GET" && path === "/api/v1/snapshot") {
      return jsonResponse(200, this.world);
    }
    if (method === "GET" && path === "/api/v1/health") {
      return jsonResponse(200, this.healthValue);
    }
    if (method === "GET" && path.startsWith("/api/v1/audit")) {
      return jsonResponse(200, auditPage(this.auditEvents, null));
    }
    if (method === "POST" && path === "/api/v1/events/session") {
      if (!this.up) {
        return jsonResponse(
          503,
          errorBody("service_unavailable", "The EnergyPod service could not be reached"),
        );
      }
      this.ticketCount += 1;
      return jsonResponse(200, { ticket: `ticket-${this.ticketCount}`, expires_in_s: 300 });
    }
    if (method === "POST" && path === "/api/v1/arm") {
      if (this.armRows === null) {
        return jsonResponse(500, errorBody("arm_not_configured", "no arm fixture"));
      }
      return jsonResponse(200, { units: this.armRows });
    }
    if (method === "POST" && path === "/api/v1/intents") {
      if (this.intentResponse === null) {
        return jsonResponse(500, errorBody("intent_not_configured", "no intent fixture"));
      }
      return jsonResponse(200, this.intentResponse);
    }
    if (method === "POST" && path === "/api/v1/emergency-stop") {
      return jsonResponse(200, {
        stop_id: "stop-9",
        status: "latched",
        unit_ids: this.world.units.map((unit) => unit.unit_id),
        fenced_generation: 7,
        degraded: [],
      });
    }
    return jsonResponse(500, errorBody("route_not_configured", `${method} ${path}`));
  };
}

let activeHarness: ComposedHarness | null = null;

beforeEach(() => {
  activeHarness = null;
});

afterEach(() => {
  activeHarness = null;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Query helpers (the shell's accessible names, as pinned by AppShell's suite)
// ---------------------------------------------------------------------------

function banner(): HTMLElement {
  return screen.getByRole("region", { name: /fleet status/i });
}

function factText(pattern: RegExp): string {
  const group = screen.getByRole("group", { name: /connection/i });
  return within(group).getByRole("listitem", { name: pattern }).textContent ?? "";
}

async function unlock(user: UserSession, token: string = OPERATOR_TOKEN): Promise<void> {
  const field = screen.getByRole("textbox", { name: /token|access/i });
  await user.click(field);
  await user.paste(token);
  await user.click(screen.getByRole("button", { name: "Unlock" }));
}

/** Advance faked time in act()-wrapped steps until the condition holds. */
async function pumpUntil(ready: () => boolean, budgetMs = 8000, stepMs = 250): Promise<void> {
  let waited = 0;
  while (!ready()) {
    if (waited >= budgetMs) {
      throw new Error("pumpUntil: the composed app never reached the expected state");
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(stepMs);
    });
    waited += stepMs;
  }
}

// ---------------------------------------------------------------------------
// Worlds (wire.ts factories; successive reads return the moved-in world)
// ---------------------------------------------------------------------------

/** MID is actively discharging under a matching authorization. */
function worldActive(): WireSnapshot {
  return wireSnapshot(
    [
      unitSnapshot({
        unit_id: "MID",
        lifecycle: "active",
        telemetry_age_s: 2,
        quality: "good",
        requested_power: { direction: "discharge", watts: 1200 },
        authorized_power: { direction: "discharge", watts: 1200 },
        measured_watts: -1200,
      }),
      unitSnapshot({ unit_id: "RHS", lifecycle: "armed_idle", telemetry_age_s: 3, measured_watts: 0 }),
      unitSnapshot({ unit_id: "LHS", lifecycle: "disarmed", telemetry_age_s: 5, measured_watts: null }),
    ],
    { snapshot_sequence: 4100, captured_at: OCCURRED_AT },
  );
}

function worldAllDisarmed(): WireSnapshot {
  return wireSnapshot(
    [
      unitSnapshot({ unit_id: "MID", lifecycle: "disarmed", telemetry_age_s: 2 }),
      unitSnapshot({ unit_id: "RHS", lifecycle: "disarmed", telemetry_age_s: 3 }),
      unitSnapshot({ unit_id: "LHS", lifecycle: "disarmed", telemetry_age_s: 5 }),
    ],
    { snapshot_sequence: 4100, captured_at: OCCURRED_AT },
  );
}

// ---------------------------------------------------------------------------
// 1. Unlock -> Home renders real wire data through the shell's plane
// ---------------------------------------------------------------------------

describe("Composed console — unlock and the Home view", () => {
  it("renders the operator's fleet through the real shell, client, ticket handshake, and plane", async () => {
    const user = userEvent.setup();
    const harness = new ComposedHarness(worldActive());
    harness.install();
    render(<AppShell views={views} />);

    await unlock(user);

    // The snapshot frame from the (single) real connection is the picture.
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });
    expect(within(banner()).getByText("Active")).toBeInTheDocument();

    // Home answers its power question from the wire values, not defaults:
    // requested and allowed are separately labeled figures.
    const figures = await screen.findAllByText("Discharging 1,200 W");
    expect(figures.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Live updates connected — this picture is current.")).toBeInTheDocument();

    // The authenticated REST path: the pasted token is the bearer on the read.
    const snapshotReads = harness.requestsFor("GET", "/api/v1/snapshot");
    expect(snapshotReads).toHaveLength(1);
    expect(snapshotReads[0]!.headers.Authorization).toBe(`Bearer ${OPERATOR_TOKEN}`);

    // The browser event-stream handshake: one ticket, both subprotocols, the
    // cursor-less first connection. The token never appears in the URL.
    expect(harness.ticketsBought()).toBe(1);
    expect(harness.sockets).toHaveLength(1);
    const eventsUrl = new URL(harness.sockets[0]!.url);
    expect(eventsUrl.pathname).toBe("/api/v1/events");
    expect(harness.sockets[0]!.protocols).toEqual([EVENTS_SUBPROTOCOL, "ticket-1"]);
    expect(harness.sockets[0]!.url).not.toContain(OPERATOR_TOKEN);
  });
});

// ---------------------------------------------------------------------------
// 2. Now: arm -> dispatch through the real client (bearer + idempotency keys)
// ---------------------------------------------------------------------------

describe("Composed console — the Now control surface", () => {
  it("arms and dispatches through the shared client: real headers, refused units never dispatchable", async () => {
    const user = userEvent.setup();
    const worldArmed = wireSnapshot(
      [
        unitSnapshot({ unit_id: "MID", lifecycle: "armed_idle", telemetry_age_s: 2 }),
        unitSnapshot({ unit_id: "RHS", lifecycle: "armed_idle", telemetry_age_s: 3 }),
        unitSnapshot({ unit_id: "LHS", lifecycle: "disarmed", telemetry_age_s: 5 }),
      ],
      { snapshot_sequence: 4110, captured_at: OCCURRED_AT },
    );
    const harness = new ComposedHarness(
      worldAllDisarmed(),
      wireHealth({ control_readiness: { ready: false, reasons: ["no_unit_armed"] } }),
    );
    // The facade publishes every outcome row, refused ones included.
    const armRows: WireOutcomeRow[] = [
      { unit_id: "MID", status: "armed", reason: "armed" },
      { unit_id: "RHS", status: "armed", reason: "armed" },
      { unit_id: "LHS", status: "refused", reason: "not_qualified" },
    ];
    harness.armRows = armRows;
    harness.intentResponse = {
      status: "accepted",
      intent_id: "intent-77",
      expires_in_s: 300,
      requested: { direction: "discharge", watts: 1500 },
    };
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });

    // The shell hands the control view the session's shared client — the
    // composed seam that once left this view reading a token it never got.
    await user.click(screen.getByRole("link", { name: "Now" }));
    await screen.findByText("Current request");

    // --- arm: the dialog's checklist reads the live control readiness ------
    await user.click(screen.getByRole("button", { name: "Arm" }));
    const armDialog = screen.getByRole("dialog", { name: "Arm the pod" });
    expect(within(armDialog).getByText("MID: qualified, latch clear")).toBeInTheDocument();
    expect(within(armDialog).getByText("no_unit_armed")).toBeInTheDocument();

    await user.click(within(armDialog).getByRole("button", { name: "ARM" }));

    const armPost = harness.requestsFor("POST", "/api/v1/arm");
    await waitFor(() => {
      expect(armPost).toHaveLength(1);
    });
    expect(armPost[0]!.headers.Authorization).toBe(`Bearer ${OPERATOR_TOKEN}`);
    expect(armPost[0]!.headers["Idempotency-Key"]).toMatch(ID_PATTERN);
    expect(JSON.parse(armPost[0]!.body ?? "{}")).toEqual({
      unit_ids: ["MID", "RHS", "LHS"],
      confirmation: "ARM",
    });

    // The service publishes the same outcomes on the bus; the post-arm world
    // becomes what successive snapshot reads return.
    harness.world = worldArmed;
    harness.publish(unitArmed(4101, armRows));

    const outcome = await screen.findByRole("region", { name: "Control action outcome" });
    await waitFor(() => {
      expect(outcome.textContent).toContain("MID: armed");
      expect(outcome.textContent).toContain("RHS: armed");
      expect(outcome.textContent).toContain("LHS: refused");
      expect(outcome.textContent).toContain("not_qualified");
    });

    // The shell refetched the snapshot after the authority frame and the
    // banner followed it — mid-session, without any reconnect.
    await waitFor(() => {
      expect(within(banner()).getByText("Armed")).toBeInTheDocument();
      expect(harness.requestsFor("GET", "/api/v1/snapshot").length).toBeGreaterThanOrEqual(2);
    });
    expect(harness.sockets).toHaveLength(1);

    // --- dispatch: the refused unit is not offered ---------------------------
    await user.click(screen.getByRole("button", { name: "Discharge" }));
    const dispatchDialog = screen.getByRole("dialog", { name: "Discharge" });
    const offeredUnits = within(dispatchDialog)
      .getAllByRole("checkbox")
      .map((checkbox) => (checkbox.parentElement?.textContent ?? "").trim());
    expect(offeredUnits).toContain("MID");
    expect(offeredUnits).toContain("RHS");
    expect(offeredUnits).not.toContain("LHS");

    await user.type(within(dispatchDialog).getByLabelText("Watts"), "1500");
    expect(
      within(dispatchDialog).getByText(/Discharge MID, RHS at 1,500 W for 5 min/),
    ).toBeInTheDocument();
    await user.click(within(dispatchDialog).getByRole("button", { name: "Confirm" }));

    const intentPost = harness.requestsFor("POST", "/api/v1/intents");
    await waitFor(() => {
      expect(intentPost).toHaveLength(1);
    });
    expect(intentPost[0]!.headers.Authorization).toBe(`Bearer ${OPERATOR_TOKEN}`);
    expect(intentPost[0]!.headers["Content-Type"]).toBe("application/json");
    expect(intentPost[0]!.headers["Idempotency-Key"]).toMatch(ID_PATTERN);
    // The refused unit never rides along on a dispatch.
    expect(JSON.parse(intentPost[0]!.body ?? "{}")).toEqual({
      unit_ids: ["MID", "RHS"],
      direction: "discharge",
      watts: 1500,
      ttl_s: 300,
    });

    // The service publishes the accepted intent; the view renders the outcome
    // and the countdown captured from the response's expires_in_s.
    harness.publish(
      intentAccepted(4112, {
        intent_id: "intent-77",
        direction: "discharge",
        watts: 1500,
        unit_ids: ["MID", "RHS"],
      }),
    );
    const dispatchOutcome = await screen.findByRole("region", { name: "Dispatch outcome" });
    expect(dispatchOutcome.textContent).toContain("accepted");
    expect(dispatchOutcome.textContent).toContain("intent-77");
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Remaining time" }).textContent).toContain(
        "300 s left",
      );
    });
    const requested = screen.getByRole("group", { name: "Requested" }).textContent ?? "";
    expect(requested).toContain("Discharge");
    expect(requested).toContain("1,500 W");
  });
});

// ---------------------------------------------------------------------------
// 3. Emergency stop mid-session: the banner follows the fleet (the freeze P0)
// ---------------------------------------------------------------------------

describe("Composed console — a mid-session emergency stop", () => {
  it("latches over the bus, refetches the snapshot, and the banner follows the fleet", async () => {
    const user = userEvent.setup();
    // The post-stop world the next snapshot read returns: every unit fenced,
    // plus a unit that only this read could reveal.
    const worldStopped = wireSnapshot(
      [
        unitSnapshot({ unit_id: "MID", lifecycle: "inhibited", telemetry_age_s: 2, measured_watts: 0 }),
        unitSnapshot({ unit_id: "RHS", lifecycle: "inhibited", telemetry_age_s: 3, measured_watts: 0 }),
        unitSnapshot({ unit_id: "LHS", lifecycle: "inhibited", telemetry_age_s: 5, measured_watts: 0 }),
        unitSnapshot({ unit_id: "AUX", lifecycle: "inhibited", telemetry_age_s: 1, measured_watts: 0 }),
      ],
      { snapshot_sequence: 4110, captured_at: OCCURRED_AT },
    );
    const harness = new ComposedHarness(worldActive());
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);
    await waitFor(() => {
      expect(within(banner()).getByText("Active")).toBeInTheDocument();
    });
    const readsBefore = harness.requestsFor("GET", "/api/v1/snapshot").length;

    // The server's stop order: the held authorization is revoked first (with
    // the stop-scoped reason), then the latch is published.
    harness.world = worldStopped;
    harness.publish(authorizationRevoked(4101, ["MID", "RHS", "LHS"], "emergency_stop:stop-9"));
    harness.publish(
      emergencyStopLatched(4102, {
        stop_id: "stop-9",
        unit_ids: ["MID", "RHS", "LHS"],
        reason: "Operator pressed stop",
      }),
    );

    // The latch is announced assertively, exactly as the frame carries it.
    await waitFor(() => {
      expect(
        screen.getByText("Emergency stop latched on MID, RHS, LHS — Operator pressed stop."),
      ).toBeInTheDocument();
    });

    // The stopped fleet is never presented as running, and the refetched
    // world (which only the new read could know) lands without a reconnect.
    await waitFor(() => {
      expect(within(banner()).getByText("Inhibited")).toBeInTheDocument();
      expect(within(banner()).getByText(/AUX — Inhibited/)).toBeInTheDocument();
      expect(within(banner()).getByText(/MID — Inhibited/)).toBeInTheDocument();
    });
    expect(harness.requestsFor("GET", "/api/v1/snapshot").length).toBeGreaterThan(readsBefore);
    expect(harness.sockets).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 4. Batteries and Activity render the REAL event/audit shapes
// ---------------------------------------------------------------------------

describe("Composed console — Batteries on the real wire", () => {
  it("renders cards from the snapshot, observation.published tracks, and the audit Events tab holds", async () => {
    const user = userEvent.setup();
    const harness = new ComposedHarness(worldActive());
    harness.auditEvents = [
      auditEvent({
        sequence: 301,
        event_type: "emergency_stop",
        unit_id: "MID",
        result: "latched",
        reason_codes: ["blocking_fault"],
      }),
      auditEvent({
        sequence: 302,
        event_type: "unit_armed",
        unit_id: "MID",
        result: "armed",
      }),
      auditEvent({
        sequence: 303,
        event_type: "control_decision",
        unit_id: "RHS",
        result: "authorized",
      }),
    ];
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });

    await user.click(screen.getByRole("link", { name: "Batteries" }));

    // The real snapshot shape: measured power renders; charge/temperature are
    // named as missing (the wire carries no such fields), never invented.
    const midCard = await screen.findByRole("group", { name: "MID" });
    expect(within(midCard).getByText(/Discharging 1,200 W/)).toBeInTheDocument();
    // Charge, temperature, and cell spread: three separately named gaps.
    await waitFor(() => {
      expect(within(midCard).getAllByText(/^No data \(last update/)).toHaveLength(3);
    });
    expect(within(midCard).getByText("No data", { exact: true })).toBeInTheDocument();

    // The only observation event the service publishes: minimal payload, no
    // readings — the card answers with wall time and the telemetry sequence.
    harness.publish(observationPublished(4105, "MID", { telemetrySequence: 41050 }));
    await waitFor(() => {
      expect(within(midCard).getByText(/telemetry sequence 41050/)).toBeInTheDocument();
    });
    expect(within(midCard).getByText(/10:00 UTC/)).toBeInTheDocument();

    // A bus summary the view does not consume is ignored, never fatal.
    harness.publish(auditAppended(4106, { event_type: "unit_armed", unit_id: "MID" }));

    // The Events tab reads the audit read model (event_type / result).
    await user.click(within(midCard).getByRole("button", { name: "MID" }));
    await user.click(screen.getByRole("tab", { name: "Events" }));
    const eventsPanel = await screen.findByText(/Emergency stop: Latched/);
    expect(eventsPanel).toBeInTheDocument();
    expect(screen.getByText(/Arm request: Armed/)).toBeInTheDocument();
    // RHS's entry is another unit's history: not shown under MID.
    expect(screen.queryByText(/Power decision/)).toBeNull();
  });
});

describe("Composed console — Activity on the real audit read model", () => {
  it("renders every audited event type the service writes without crashing, newest first", async () => {
    const user = userEvent.setup();
    const harness = new ComposedHarness(worldActive());
    harness.auditEvents = [
      auditEvent({
        sequence: 601,
        event_type: "control_decision",
        unit_id: "MID",
        result: "clamped",
        requested_active_w: 1500,
        authorized_active_w: 1200,
        reason_codes: ["power_clamped"],
      }),
      auditEvent({
        sequence: 602,
        event_type: "intent_accepted",
        unit_id: "MID",
        result: "accepted",
        principal: "operator:home",
      }),
      auditEvent({
        sequence: 603,
        event_type: "unit_armed",
        unit_id: "RHS",
        result: "armed",
        principal: "operator:home",
      }),
      auditEvent({
        sequence: 604,
        event_type: "emergency_stop",
        result: "latched",
        principal: "supervisor:ops",
        reason_codes: ["blocking_fault"],
      }),
      auditEvent({
        sequence: 605,
        event_type: "authorization_revoked",
        unit_id: "LHS",
        result: "revoked",
      }),
      auditEvent({
        sequence: 606,
        event_type: "inhibit_acknowledged",
        unit_id: "LHS",
        result: "acknowledged",
      }),
    ];
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);

    await user.click(screen.getByRole("link", { name: "Activity" }));

    const timeline = await waitFor(() => {
      const node = document.querySelector("ol.activity-timeline");
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });

    // The whole real read model renders: `event_type` headlines, the string
    // `principal`, the signed watt figures, and `result` in words.
    expect(within(timeline).getByText("Power decision")).toBeInTheDocument();
    expect(within(timeline).getByText("Reduced to 1200 W of the 1500 W requested")).toBeInTheDocument();
    expect(within(timeline).getAllByText(/Requested by/).length).toBeGreaterThanOrEqual(2);
    // The default principal is the audit write model's own subject string.
    expect(within(timeline).getAllByText("operator:home")).toHaveLength(5);
    expect(within(timeline).getAllByText("supervisor:ops")).toHaveLength(1);
    expect(within(timeline).getByText("Dispatch request accepted")).toBeInTheDocument();
    expect(within(timeline).getByText("Arm request")).toBeInTheDocument();
    expect(within(timeline).getByText("Emergency stop")).toBeInTheDocument();
    expect(within(timeline).getByText("Stop latched", { exact: true })).toBeInTheDocument();
    expect(within(timeline).getAllByText("Authorization revoked").length).toBeGreaterThanOrEqual(2);
    expect(within(timeline).getByText("Inhibit acknowledgement")).toBeInTheDocument();
    expect(within(timeline).getByText("Acknowledged", { exact: true })).toBeInTheDocument();

    // Newest first: the highest sequence is the timeline's first entry.
    const entries = within(timeline).getAllByRole("listitem");
    expect(entries).toHaveLength(6);
    expect(entries[0]!.textContent).toContain("Inhibit acknowledgement");
    // The page held every entry: no pagination control remains.
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    // The audit read went out authenticated over the real client.
    const auditReads = harness.requestsFor("GET", "/api/v1/audit");
    expect(auditReads).toHaveLength(1);
    expect(auditReads[0]!.headers.Authorization).toBe(`Bearer ${OPERATOR_TOKEN}`);
  });
});

// ---------------------------------------------------------------------------
// 5. Sign-out ends consumption of the authenticated stream (teardown pin)
// ---------------------------------------------------------------------------

describe("Composed console — sign-out teardown", () => {
  it("returns the connection and consumes nothing further on the abandoned socket", async () => {
    const user = userEvent.setup();
    const harness = new ComposedHarness(worldActive());
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });
    const readsBefore = harness.requestsFor("GET", "/api/v1/snapshot").length;
    expect(harness.ticketsBought()).toBe(1);
    expect(harness.sockets).toHaveLength(1);
    const abandoned = harness.sockets[0]!;

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    // Back at the gate; the session's secrets never echo into the DOM.
    expect(await screen.findByRole("button", { name: "Unlock" })).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain(OPERATOR_TOKEN);

    // A frame arriving on the abandoned socket is consumed by no one: no
    // state change, no snapshot refetch, no new ticket, no new connection.
    abandoned.emitMessage(
      JSON.stringify(
        emergencyStopLatched(4110, {
          stop_id: "stop-after-signout",
          unit_ids: ["MID", "RHS", "LHS"],
          reason: "should never be seen",
        }),
      ),
    );
    await new Promise((resolve) => {
      setTimeout(resolve, 250);
    });
    expect(screen.queryByText(/Emergency stop latched/)).toBeNull();
    expect(harness.requestsFor("GET", "/api/v1/snapshot")).toHaveLength(readsBefore);
    expect(harness.ticketsBought()).toBe(1);
    expect(harness.sockets).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 6. Stream loss: cached picture, bounded retry, recovery on reconnect
// ---------------------------------------------------------------------------

describe("Composed console — stream loss and recovery", () => {
  it("keeps the last picture, announces active control, reconnects with the cursor, and recovers", async () => {
    const user = userEvent.setup();
    const worldRecovered = wireSnapshot(
      [
        unitSnapshot({ unit_id: "MID", lifecycle: "armed_idle", telemetry_age_s: 4, measured_watts: 0 }),
        unitSnapshot({ unit_id: "RHS", lifecycle: "armed_idle", telemetry_age_s: 4, measured_watts: 0 }),
        unitSnapshot({ unit_id: "LHS", lifecycle: "disarmed", telemetry_age_s: 6, measured_watts: null }),
      ],
      { snapshot_sequence: 4200, captured_at: OCCURRED_AT },
    );
    const harness = new ComposedHarness(worldActive());
    harness.install();
    render(<AppShell views={views} />);
    await unlock(user);
    await waitFor(() => {
      expect(within(banner()).getByText("Active")).toBeInTheDocument();
    });
    expect(screen.getByText("Live updates connected — this picture is current.")).toBeInTheDocument();

    // The server drops the connection without warning.
    harness.world = worldRecovered;
    harness.closeCurrentSocket();

    // The disconnected operator state: a notice apart from the facts, the
    // main pane dimmed, the last known picture still on screen — and losing
    // the stream while a pod is under active control is announced assertively.
    const notice = await screen.findByRole("region", { name: /live updates/i });
    expect(notice.textContent).toContain("connection lost");
    expect(notice.textContent).toContain("readings may be old");
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("No — connection lost, reconnecting");
    });
    expect(screen.getByRole("main").className).toContain("dimmed");
    expect(
      screen.getByText(
        "Live updates lost while MID was under active control — control is unavailable until the connection returns.",
      ),
    ).toBeInTheDocument();
    expect(within(banner()).getByText("MID — Active · 2 s old")).toBeInTheDocument();

    // The automatic retry buys a new ticket and reconnects with the last seen
    // sequence as the cursor — never a replay from zero.
    await waitFor(
      () => {
        expect(harness.sockets.length).toBe(2);
      },
      { timeout: 2500 },
    );
    expect(harness.ticketsBought()).toBe(2);
    expect(new URL(harness.sockets[1]!.url).searchParams.get("after")).toBe("4100");

    // The reconnected stream delivers the moved-in world and the shell
    // recovers: the notice goes, the facts go live, the banner follows.
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });
    expect(screen.queryByRole("region", { name: /live updates/i })).toBeNull();
    await waitFor(() => {
      expect(within(banner()).getByText("Armed")).toBeInTheDocument();
    });
    expect(screen.getByRole("main").className).not.toContain("dimmed");
  });

  it("spends the reconnect budget, pauses honestly, and recovers on the operator's retry", { timeout: 30000 }, async () => {
    const user = userEvent.setup();
    const worldRecovered = wireSnapshot(
      [unitSnapshot({ unit_id: "MID", lifecycle: "armed_idle", telemetry_age_s: 4 })],
      { snapshot_sequence: 4200, captured_at: OCCURRED_AT },
    );
    const harness = new ComposedHarness(worldAllDisarmed());
    harness.install();
    render(<AppShell views={views} />);

    // Unlock on the real clock (userEvent's own delays), then fake only the
    // reconnect schedule: five widening waits is 15.5 s no test should wait.
    await unlock(user);
    await waitFor(() => {
      expect(factText(/event stream/i)).toContain("Yes — live");
    });
    expect(harness.sockets).toHaveLength(1);
    vi.useFakeTimers();

    // The service goes away, then the live connection drops.
    harness.up = false;
    harness.world = worldRecovered;
    harness.closeCurrentSocket();
    await pumpUntil(() => factText(/event stream/i).includes("connection lost"));

    // Five widening retries (500 ms -> 8 s), each refused at the handshake,
    // then the budget is spent: an honest paused message and a manual retry.
    await pumpUntil(
      () => (document.body.textContent ?? "").includes("automatic reconnection has paused"),
      25000,
    );
    const notice = screen.getByRole("region", { name: /live updates/i });
    expect(notice.textContent).toContain("The last known picture is still shown");
    // The refusals' own envelope surfaces instead of being swallowed.
    expect(within(notice).getByText("service_unavailable")).toBeInTheDocument();
    const retryButton = within(notice).getByRole("button", { name: "Try reconnecting" });
    // One initial ticket plus exactly five retries — no sixth attempt ever.
    expect(harness.ticketsBought()).toBe(6);
    expect(harness.sockets).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(harness.ticketsBought()).toBe(6);

    // The operator's retry reconnects when the service returns — still with
    // the last seen cursor — and the moved-in world is adopted.
    harness.up = true;
    fireEvent.click(retryButton);
    await pumpUntil(() => factText(/event stream/i).includes("Yes — live"));
    expect(harness.ticketsBought()).toBe(7);
    expect(harness.sockets).toHaveLength(2);
    expect(new URL(harness.sockets[1]!.url).searchParams.get("after")).toBe("4100");
    await pumpUntil(() => (banner().textContent ?? "").includes("Armed"));
    expect(screen.queryByRole("region", { name: /live updates/i })).toBeNull();
  });
});
