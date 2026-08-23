/**
 * Behavior contract for the "Now" control view (docs/UI_CONTRACTS.md, "Now (control)").
 *
 * PROP SEAM (web/src/app/views.ts `ShellViewProps`): the composed app mounts
 * this view through the shell's injection point with exactly one prop — the
 * session's shared `{ client }` (AppShell builds the client from the
 * operator's token; views never mint their own, because a second client would
 * open a second events socket). The suite injects the client the same way, so
 * what it pins is what the composed app renders.
 *
 * The API client module (web/src/api/client.ts) is mocked at its exact
 * surface with `createApiClient` replaced and every other canonical export
 * preserved, so the view may import ApiClientError/isUnauthorizedError; every
 * rejection below is an ApiClientError carrying the envelope verbatim plus a
 * status, exactly as the canonical client rejects.
 *
 * NOTE on UI_CONTRACTS.md "Test conventions" (fetch/WebSocket boundary): this
 * suite pins the client↔view handoff, so it mocks the client module instead
 * of fetch/WebSocket; the wire shapes themselves (envelope casing, frame
 * nesting, headers, tickets) are pinned at the fetch/WebSocket boundary by
 * src/api/client.test.ts. Envelope fixtures here mirror the real REST/WS wire
 * (src/energypod/application/service.py, src/energypod/api/rest.py,
 * src/energypod/application/events.py):
 *
 * - wire enums are lowercase StrEnum values ("charge"/"discharge"/"idle",
 *   lifecycles "disarmed"/"armed_idle"/"active"/"inhibited", quality
 *   "good"/"stale"/"missing"/"bad"/"suspect");
 * - the first WS frame is the snapshot envelope {type, sequence, data};
 * - event frames carry {type, sequence, occurred_at} with event-specific
 *   fields nested under "payload" (the facade publishes {"type", "payload"},
 *   the bus adds only sequence/occurred_at, rest.py sends it unchanged);
 * - `unit.armed` / `unit.disarmed` payload rows carry
 *   {unit_id, status, reason} where a refused row's status is "refused"
 *   (service.py `_arm_one` / `_disarm_one`): a refusal is never a lifecycle
 *   change;
 * - `emergency_stop.latched` carries {stop_id, unit_ids, reason, generation,
 *   degraded} and `inhibit.acknowledged` carries {unit_id, latch_cleared}
 *   (service.py `_publish`): a latched fleet is inhibited, and a cleared
 *   latch changes no lifecycle until the refreshed snapshot says so;
 * - resync_required frames carry {type, reason, snapshot_sequence?} with a
 *   real reason string (sequence_gap, backpressure, slow_subscriber, ...);
 * - emergency stop's `degraded` is a list of reason strings, never a flag.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { NowView } from "./NowView";

const api = vi.hoisted(() => {
  const client = {
    getSnapshot: vi.fn(),
    getHealth: vi.fn(),
    getAudit: vi.fn(),
    postIntent: vi.fn(),
    postIntentCancel: vi.fn(),
    postArm: vi.fn(),
    postDisarm: vi.fn(),
    postEmergencyStop: vi.fn(),
    postStopAcknowledgement: vi.fn(),
    postInhibitAcknowledgement: vi.fn(),
    openEvents: vi.fn(),
  };
  return { createApiClient: vi.fn(), client };
});

/** The mocked client as the shell hands it to the view (one bridging cast). */
function injectedClient(): ApiClient {
  return api.client as unknown as ApiClient;
}

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  createApiClient: api.createApiClient,
}));

// --- envelope fixtures (shapes from the real service adapter) ---------------

type PowerView = { direction: string; watts: number };

type UnitView = {
  unit_id: string;
  lifecycle: string;
  telemetry_age_s: number | null;
  quality: string;
  requested_power: PowerView;
  authorized_power: PowerView | null;
  measured_watts: number | null;
};

type SnapshotEnvelope = {
  site_id: string;
  snapshot_sequence: number;
  captured_at: string;
  units: UnitView[];
  /** The amended snapshot contract's engaged stops (PENDING backend field). */
  active_stops?: ActiveStopEnvelope[];
  /**
   * The snapshot-level intent block (PENDING backend field): the live
   * request's own per-unit figures for cold-load exactness. Absent = today's
   * wire (feature detection).
   */
  intent?: SnapshotIntentEnvelope | null;
};

type SnapshotIntentEnvelope = {
  requested_watts_by_unit: Record<string, number> | null;
  authorized_watts_by_unit: Record<string, number> | null;
  directions_by_unit: Record<string, string> | null;
};

type ActiveStopEnvelope = {
  stop_id: string;
  latched_at: string;
  principal: string;
  reason_codes: string[];
  unit_ids: string[] | null;
};

function snapshotEnvelope(units: UnitView[], sequence = 41): SnapshotEnvelope {
  return {
    site_id: "site-1",
    snapshot_sequence: sequence,
    captured_at: "2026-08-22T12:00:00+10:00",
    units,
  };
}

/** A snapshot world whose active_stops name one engaged stop. */
function withActiveStop(
  envelope: SnapshotEnvelope,
  stopId: string,
  sequence = envelope.snapshot_sequence,
): SnapshotEnvelope {
  return {
    ...envelope,
    snapshot_sequence: sequence,
    active_stops: [
      {
        stop_id: stopId,
        latched_at: "2026-08-22T23:14:24Z",
        principal: "operator:home",
        reason_codes: ["operator_requested"],
        unit_ids: null,
      },
    ],
  };
}

const ACTIVE_MID: UnitView = {
  unit_id: "MID",
  lifecycle: "active",
  telemetry_age_s: 1.5,
  quality: "good",
  requested_power: { direction: "charge", watts: 1500 },
  authorized_power: { direction: "charge", watts: 1000 },
  measured_watts: 980,
};

const ARMED_MID: UnitView = {
  unit_id: "MID",
  lifecycle: "armed_idle",
  telemetry_age_s: 2,
  quality: "good",
  requested_power: { direction: "idle", watts: 0 },
  authorized_power: null,
  measured_watts: 0,
};

const ARMED_RHS: UnitView = { ...ARMED_MID, unit_id: "RHS" };

const DISARMED_MID: UnitView = { ...ARMED_MID, lifecycle: "disarmed" };

const HEALTH_OK = {
  liveness: { ok: true },
  service_readiness: { ready: true, reasons: [] },
  control_readiness: { ready: true, reasons: [] },
};

const ACCEPTED_CHARGE = {
  intent_id: "intent-2-1.000000",
  acceptance_revision: 2,
  accepted_at_monotonic: 1000.5,
  status: "accepted",
  requested: { direction: "charge", watts: 1500 },
  authorized: null,
  measured: null,
  expires_in_s: 300,
};

type Frame = Record<string, unknown>;

/** A connected stream: yields the frames, then stays open silently. */
function liveStream(frames: Frame[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const frame of frames) {
        yield frame;
      }
      await new Promise<void>(() => {});
    },
  };
}

/** A stream that delivers its frames and then dies (connection loss). */
function dyingStream(frames: Frame[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const frame of frames) {
        yield frame;
      }
      throw new Error("event stream terminated unexpectedly");
    },
  };
}

/** A stream that delivers its frames and then ends cleanly (needs reconnect). */
function endingStream(frames: Frame[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const frame of frames) {
        yield frame;
      }
    },
  };
}

/**
 * A stream that delivers `first`, waits for the gate, then delivers `rest`:
 * used to prove the view adopts the snapshot frame before any event lands.
 */
function gatedStream(first: Frame[], gate: Promise<void>, rest: Frame[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const frame of first) {
        yield frame;
      }
      await gate;
      for (const frame of rest) {
        yield frame;
      }
      await new Promise<void>(() => {});
    },
  };
}

/** A controllable live stream: yields the initial frames, then pushed frames. */
function liveChannel(initial: Frame[]): {
  openEvents: () => AsyncIterable<Frame>;
  push(frame: Frame): void;
} {
  const queue: Frame[] = [...initial];
  let wake: (() => void) | null = null;
  const notify = (): void => {
    const release = wake;
    wake = null;
    release?.();
  };
  return {
    openEvents: () =>
      (async function* channel(): AsyncGenerator<Frame, void, unknown> {
        while (true) {
          while (queue.length > 0) {
            const next = queue.shift();
            if (next !== undefined) {
              yield next;
            }
          }
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
        }
      })(),
    push: (frame) => {
      queue.push(frame);
      notify();
    },
  };
}

function renderNow() {
  // The shell's view contract (views.ts ShellViewProps): the session's client
  // is the one prop, exactly as AppShell mounts this view in the product tree.
  render(<NowView client={injectedClient()} />);
}

function fact(name: string): HTMLElement {
  return screen.getByRole("group", { name });
}

/**
 * The tightest text inside `scope` matching every pattern: the labelled row
 * or composed sentence itself, never an ancestor that merely contains it.
 */
function tightestText(scope: HTMLElement, ...patterns: RegExp[]): string {
  const matches = within(scope)
    .getAllByText((_: string, element: Element | null) =>
      patterns.every((pattern) => pattern.test(element?.textContent ?? "")),
    )
    .map((element: HTMLElement) => element.textContent ?? "");
  if (matches.length === 0) {
    throw new Error(
      `expected an element matching ${patterns.map(String).join(" + ")} within the scope`,
    );
  }
  return matches.reduce(
    (tightest: string, text: string) => (text.length < tightest.length ? text : tightest),
  );
}

/** The text of everything an input describes via aria-describedby. */
function describedText(input: HTMLElement): string {
  const ids = (input.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter(Boolean);
  return ids
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

beforeEach(() => {
  vi.resetAllMocks();
  api.createApiClient.mockReturnValue(api.client);
  api.client.getHealth.mockResolvedValue(HEALTH_OK);
  api.client.openEvents.mockImplementation(() => liveStream([]));
});

// --- current request card ----------------------------------------------------

describe("NowView — current request card", () => {
  it("shows requested, allowed, actual, and remaining time as four separate facts", async () => {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ACTIVE_MID]));

    renderNow();
    // The composed app hands the view the session's client: the view reads
    // through exactly that client and never mints its own (a second client
    // would open a second events socket and race the shell for frames).
    expect(api.client.getSnapshot).toHaveBeenCalled();
    expect(api.createApiClient).not.toHaveBeenCalled();

    expect(
      await screen.findByRole("heading", { name: /current request/i }),
    ).toBeInTheDocument();

    const requested = fact("Requested");
    const allowed = fact("Allowed");
    const actual = fact("Actual");

    // Requested: charge at 1,500 W.
    expect(requested.textContent ?? "").toMatch(/charge/i);
    expect(requested.textContent ?? "").toMatch(/1,?500\s*W/);
    // Allowed is its own fact at the authorized 1,000 W — a limited action is
    // never presented as the full request or as what is actually flowing.
    expect(allowed.textContent ?? "").toMatch(/charge/i);
    expect(allowed.textContent ?? "").toMatch(/1,?000\s*W/);
    expect(allowed.textContent ?? "").not.toMatch(/1,?500/);
    // Actual reports the measurement, not the request.
    expect(actual.textContent ?? "").toMatch(/980\s*W/);
    expect(actual.textContent ?? "").not.toMatch(/1,?500/);
    // Remaining time is present as the fourth named fact.
    expect(fact("Remaining time")).toBeInTheDocument();
  });

  it("renders requested, allowed, and actual at the two-decimal display bound: a raw eight-decimal fixture never reaches the operator", async () => {
    // The wire can carry float-decoded power figures at full precision; the
    // shared display-precision module (src/lib/format.ts) is the render
    // boundary, so every fact on this card shows at most two decimals — the
    // charging (negative) measurement keeps its sign and its bound.
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        {
          ...ACTIVE_MID,
          requested_power: { direction: "charge", watts: 1500.12345678 },
          authorized_power: { direction: "charge", watts: 1000.98765432 },
          measured_watts: -980.12345678,
        },
      ]),
    );

    renderNow();

    await waitFor(() => {
      expect(fact("Requested").textContent ?? "").toContain("1,500.12 W");
      expect(fact("Allowed").textContent ?? "").toContain("1,000.99 W");
      expect(fact("Actual").textContent ?? "").toContain("-980.12 W");
    });
    expect(fact("Requested").textContent ?? "").not.toMatch(/\d\.\d{3,}/);
    expect(fact("Allowed").textContent ?? "").not.toMatch(/\d\.\d{3,}/);
    expect(fact("Actual").textContent ?? "").not.toMatch(/\d\.\d{3,}/);
  });

  it("derives the per-battery expectation for a multi-unit request, with the total beside it", async () => {
    // The wire carries the intent's scalar total in every covered unit's
    // requested_power (service.py `_requested_power`), so three units at
    // 3,000 W are one request the operator filed as "1,000 each". The
    // Requested fact derives the split — "≈" because headroom weighting can
    // shift a unit's share — instead of repeating the raw total per unit.
    // A single-unit request keeps its plain figure (pinned by the tests
    // above: "Charge · 1,500 W" for one unit).
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        { ...ACTIVE_MID, requested_power: { direction: "discharge", watts: 3000 } },
        { ...ACTIVE_MID, unit_id: "RHS", requested_power: { direction: "discharge", watts: 3000 } },
        { ...ACTIVE_MID, unit_id: "LHS", requested_power: { direction: "discharge", watts: 3000 } },
      ]),
    );

    renderNow();

    const requested = await screen.findByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toContain("Discharge");
    expect(requested.textContent ?? "").toContain("≈1,000 W per battery (3,000 W total)");
  });

  it("reports allowed as explicitly none when a live request has no authorization", async () => {
    // requested from an accepted intent, authorized_power still null (not yet
    // granted): the requested watts must never echo into the Allowed fact.
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        {
          unit_id: "MID",
          lifecycle: "active",
          telemetry_age_s: 1,
          quality: "good",
          requested_power: { direction: "charge", watts: 1500 },
          authorized_power: null,
          measured_watts: null,
        },
      ]),
    );

    renderNow();

    const allowed = await screen.findByRole("group", { name: "Allowed" });
    expect(allowed.textContent ?? "").toMatch(
      /none|not yet|no authorization|unauthorized|pending|no data|nothing/i,
    );
    expect(allowed.textContent ?? "").not.toMatch(/1,?500/);
  });

  it("adopts the WebSocket snapshot frame and then live intent events", async () => {
    const frame = snapshotEnvelope([ACTIVE_MID]);
    let releaseIntent!: () => void;
    const gate = new Promise<void>((resolve) => {
      releaseIntent = resolve;
    });
    // REST snapshot never answers: the first WS frame is the authoritative view.
    api.client.getSnapshot.mockReturnValue(new Promise<SnapshotEnvelope>(() => {}));
    api.client.openEvents.mockImplementation(() =>
      gatedStream(
        [{ type: "snapshot", sequence: 41, data: frame }],
        gate,
        [
          {
            type: "intent.accepted",
            sequence: 42,
            occurred_at: "2026-08-22T12:00:05+10:00",
            payload: {
              principal: "operator",
              intent_id: "intent-2-1.000000",
              direction: "discharge",
              watts: 1200,
              unit_ids: ["MID"],
            },
          },
        ],
      ),
    );

    renderNow();

    // This view can only come from the snapshot frame: REST is still pending
    // and no event has been delivered yet.
    const requested = await screen.findByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toMatch(/charge/i);
    expect(requested.textContent ?? "").toMatch(/1,?500\s*W/);

    releaseIntent();
    await waitFor(() => {
      const updated = screen.getByRole("group", { name: "Requested" });
      expect(updated.textContent ?? "").toMatch(/discharge/i);
      expect(updated.textContent ?? "").toMatch(/1,?200\s*W/);
    });
  });

  it("refetches the snapshot and reconnects with the last seen sequence after a resync_required discontinuity", async () => {
    const snap = snapshotEnvelope([ACTIVE_MID]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementationOnce(() =>
      endingStream([
        { type: "snapshot", sequence: 41, data: snap },
        { type: "resync_required", reason: "sequence_gap", snapshot_sequence: 45 },
      ]),
    );

    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    await waitFor(() => {
      expect(api.client.getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(api.client.openEvents.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    // The reconnect cursor is the last seen sequence (41, the snapshot frame):
    // the resync marker itself carries no sequence, and 45 is the server's
    // recovery cursor for the snapshot refetch, not a replay cursor.
    expect(api.client.openEvents).toHaveBeenLastCalledWith(41);
  });

  it("renders a loading state with no fabricated values", () => {
    api.client.getSnapshot.mockReturnValue(new Promise<SnapshotEnvelope>(() => {}));

    renderNow();

    expect(screen.getByRole("status")).toHaveTextContent(/loading/i);
    expect(screen.queryByRole("group", { name: "Requested" })).toBeNull();
    expect(screen.queryByText(/W\b/)).toBeNull();
  });

  it("explains an empty fleet and what to do first", async () => {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([]));

    renderNow();

    expect(await screen.findByText(/no units/i)).toBeInTheDocument();
    expect(screen.getByText(/first/i)).toBeInTheDocument();
  });

  it("shows a disconnected notice assertively while last data stays visible", async () => {
    const snap = snapshotEnvelope([ACTIVE_MID]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementationOnce(() =>
      dyingStream([{ type: "snapshot", sequence: 41, data: snap }]),
    );

    renderNow();

    // The unit is active, so connection loss must announce assertively.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/disconnect|connection/i);

    // Last data is dimmed, not hidden: the facts and their age remain.
    const requested = screen.getByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toMatch(/1,?500\s*W/);
    expect(screen.getByText(/age|ago/i)).toBeInTheDocument();
  });

  it("shows the age next to stale values instead of hiding them", async () => {
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        { ...ACTIVE_MID, telemetry_age_s: 600, quality: "stale" },
      ]),
    );

    renderNow();

    const actual = await screen.findByRole("group", { name: "Actual" });
    expect(actual.textContent ?? "").toMatch(/980\s*W/);
    expect(actual.textContent ?? "").toMatch(/600/);
    expect(screen.getByText(/stale|out of date/i)).toBeInTheDocument();
  });

  it("names missing measurements explicitly and never zero-fills them", async () => {
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        {
          unit_id: "MID",
          lifecycle: "disarmed",
          telemetry_age_s: null,
          quality: "missing",
          requested_power: { direction: "idle", watts: 0 },
          authorized_power: null,
          measured_watts: null,
        },
      ]),
    );

    renderNow();

    const actual = await screen.findByRole("group", { name: "Actual" });
    expect(actual.textContent ?? "").toMatch(/not available|unavailable|no (measurement|data)|missing/i);
    expect(actual.textContent ?? "").not.toMatch(/0\s*W/);
  });

  it("surfaces the snapshot error envelope verbatim with a manual retry", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockRejectedValue(
      new ApiClientError({
        code: "internal_error",
        message: "The request could not be completed",
        details: {},
        request_id: "req-8f21",
        status: 500,
      }),
    );

    renderNow();

    expect(await screen.findByText("internal_error")).toBeInTheDocument();
    expect(screen.getByText("The request could not be completed")).toBeInTheDocument();

    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ACTIVE_MID]));
    await user.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByRole("group", { name: "Requested" })).toBeInTheDocument();
    expect(api.client.getSnapshot).toHaveBeenCalledTimes(2);
  });
});

// --- per-unit watt figures from the wire --------------------------------------
//
// The backend's native per-unit form (2026-08-23): an intent submitted as
// `watts_by_unit` carries its map on the 202 acceptance view, the
// `intent.accepted` frame, and every `control_decision` audit summary
// (`requested_watts_by_unit` / `authorized_watts_by_unit`). The card prefers
// those figures over any derivation — the per-battery expectation becomes
// exact, and the Allowed fact finally names WHICH battery a headroom clamp
// hit. Scalar intents keep the total ÷ count derivation (pinned above).

describe("NowView — per-unit watt figures from the wire", () => {
  /** A live channel over a three-armed-battery world. */
  function armedFleetChannel() {
    const snap = snapshotEnvelope([ARMED_MID, ARMED_RHS, { ...ARMED_MID, unit_id: "LHS" }]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());
    return channel;
  }

  it("renders Requested exactly from the intent's own per-unit targets — no approximation", async () => {
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    // The frame the facade publishes for a per-unit intent: the derived total
    // plus the per-unit targets (service.py submit_intent).
    channel.push({
      type: "intent.accepted",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        principal: "operator:home",
        intent_id: "intent-9-1.000000",
        direction: "discharge",
        watts: 3000,
        watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        unit_ids: ["MID", "RHS", "LHS"],
      },
    });

    const requested = await waitFor(() => {
      const node = screen.getByRole("group", { name: "Requested" });
      expect(node.textContent).toContain("1,000 W per battery (3,000 W total)");
      return node;
    });
    // Each target is that battery's own allocation cap on the wire now — the
    // "≈" hedge belonged to the derived scalar split.
    expect(requested.textContent).toContain("Discharge");
    expect(requested.textContent).not.toContain("≈");
  });

  it("names the clamped battery on Allowed from the decision's per-unit authorized map", async () => {
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push({
      type: "intent.accepted",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        principal: "operator:home",
        intent_id: "intent-9-1.000000",
        direction: "discharge",
        watts: 3000,
        watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        unit_ids: ["MID", "RHS", "LHS"],
      },
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Requested" }).textContent).toContain(
        "1,000 W per battery",
      );
    });

    // The kernel's decision summary rides the audit bus: the site headroom
    // only stretched to 400 W for RHS. The card names the battery.
    channel.push({
      type: "audit.appended",
      sequence: 43,
      occurred_at: "2026-08-22T12:00:07+10:00",
      payload: {
        event_id: "facade-43",
        event_type: "control_decision",
        unit_id: null,
        generation: 9,
        result: "clamped",
        reason_codes: ["power_clamped"],
        requested_active_w: 3000,
        authorized_active_w: 2400,
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 400, LHS: 1000 },
      },
    });

    const allowed = await waitFor(() => {
      const node = screen.getByRole("group", { name: "Allowed" });
      expect(node.textContent).toContain("RHS 400 W (headroom)");
      return node;
    });
    expect(allowed.textContent).toContain("Discharge");
    expect(allowed.textContent).toContain("MID 1,000 W");
    expect(allowed.textContent).toContain("LHS 1,000 W");
    // Only the clamped battery is marked; the plain-language note names it.
    expect(allowed.textContent).not.toContain("MID 1,000 W (headroom)");
    expect(allowed.textContent).toMatch(/RHS was held back.*headroom/i);
  });

  it("adopts the acceptance response's own per-unit targets before any frame lands", async () => {
    const user = userEvent.setup();
    // Before: armed and idle. After: the request is on the wire (the scalar
    // total still repeats per covered unit in the snapshot) and the response's
    // `requested` projection carries the per-unit targets.
    const before = snapshotEnvelope([ARMED_MID, ARMED_RHS], 41);
    const after = snapshotEnvelope(
      [
        { ...ACTIVE_MID, requested_power: { direction: "charge", watts: 3000 } },
        { ...ACTIVE_MID, unit_id: "RHS", requested_power: { direction: "charge", watts: 3000 } },
      ],
      42,
    );
    let calls = 0;
    api.client.getSnapshot.mockImplementation(() => {
      calls += 1;
      return Promise.resolve(calls >= 3 ? after : before);
    });
    api.client.postIntent.mockResolvedValue({
      ...ACCEPTED_CHARGE,
      requested: {
        direction: "charge",
        watts: 3000,
        watts_by_unit: { MID: 1500, RHS: 1500 },
      },
    });
    renderNow();

    await user.click(await screen.findByRole("button", { name: /^charge/i }));
    const dialog = screen.getByRole("dialog");
    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    // The response's own map is the source: exact per-battery figures, no
    // derived "≈" split even though the snapshot repeats the scalar total.
    await waitFor(() => {
      const requested = screen.getByRole("group", { name: "Requested" });
      expect(requested.textContent).toContain("1,500 W per battery (3,000 W total)");
      expect(requested.textContent).not.toContain("≈");
    });
  });

  it("keeps the derived scalar split for a scalar intent arriving over the stream", async () => {
    // Backwards compatibility: an intent sent the scalar way (another client,
    // an older console, an in-flight request) carries no per-unit map — the
    // card falls back to total ÷ count, honestly marked "≈".
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push({
      type: "intent.accepted",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        principal: "operator:home",
        intent_id: "intent-10-1.000000",
        direction: "discharge",
        watts: 3000,
        unit_ids: ["MID", "RHS", "LHS"],
      },
    });

    await waitFor(() => {
      const requested = screen.getByRole("group", { name: "Requested" });
      expect(requested.textContent).toContain("≈1,000 W per battery (3,000 W total)");
    });
  });
});

// --- concurrent requests: one card per active intent ---------------------------
//
// The pod runs CONCURRENT per-battery requests (2026-08-24 backend contract):
// several intents coexist, each driving its own batteries through per-unit
// arbitration. The request surface is one card per live intent, keyed by the
// intent id the wire itself names; a newer same-priority request claiming a
// battery renders on the older card as a take-over, never as the older request
// having been superseded everywhere.

describe("NowView — concurrent request cards", () => {
  /** A live channel over a three-armed-battery world. */
  function armedFleetChannel() {
    const snap = snapshotEnvelope([ARMED_MID, ARMED_RHS, { ...ARMED_MID, unit_id: "LHS" }]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());
    return channel;
  }

  /** The frame the facade publishes for one accepted per-unit intent. */
  function acceptedFrame(
    sequence: number,
    intentId: string,
    direction: "charge" | "discharge",
    wattsByUnit: Record<string, number>,
    occurredAt = "2026-08-22T12:00:05+10:00",
  ): Frame {
    const unitIds = Object.keys(wattsByUnit);
    const watts = unitIds.reduce((total, unitId) => total + wattsByUnit[unitId]!, 0);
    return {
      type: "intent.accepted",
      sequence,
      occurred_at: occurredAt,
      payload: {
        principal: "operator:home",
        intent_id: intentId,
        direction,
        watts,
        watts_by_unit: wattsByUnit,
        unit_ids: unitIds,
      },
    };
  }

  function card(intentId: string): HTMLElement {
    return screen.getByRole("region", { name: `Power request ${intentId}` });
  }

  async function remainingSeconds(scope: HTMLElement): Promise<number> {
    const text = within(scope).getByRole("group", { name: "Remaining time" }).textContent ?? "";
    const match = /(\d+)\s*s/.exec(text);
    expect(match).not.toBeNull();
    return Number(match![1]);
  }

  it("renders two concurrent intents as two cards with opposite directions and independent countdowns", async () => {
    const user = userEvent.setup();
    armedFleetChannel();
    // Two separate acceptances: a 5-minute charge over MID+RHS and a 2-minute
    // discharge over LHS — concurrent, opposite, disjoint.
    api.client.postIntent
      .mockResolvedValueOnce({
        intent_id: "intent-2-1.000000",
        acceptance_revision: 2,
        accepted_at_monotonic: 1000.5,
        status: "accepted",
        requested: { direction: "charge", watts: 2000, watts_by_unit: { MID: 1000, RHS: 1000 } },
        authorized: null,
        measured: null,
        expires_in_s: 300,
      })
      .mockResolvedValueOnce({
        intent_id: "intent-3-1.000000",
        acceptance_revision: 3,
        accepted_at_monotonic: 1001.5,
        status: "accepted",
        requested: { direction: "discharge", watts: 800, watts_by_unit: { LHS: 800 } },
        authorized: null,
        measured: null,
        expires_in_s: 120,
      });
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    // Request one (this console): charge MID and RHS at 1,000 W each, 5 min.
    await user.click(screen.getByRole("button", { name: /^charge/i }));
    let dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox", { name: /LHS/ }));
    await user.type(within(dialog).getByLabelText(/watts/i), "1000");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    // Request two (this console): discharge LHS alone at 800 W, 2 min — the
    // opposite direction, on batteries the first request did not name.
    await user.click(screen.getByRole("button", { name: /^discharge/i }));
    dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox", { name: /MID/ }));
    await user.click(within(dialog).getByRole("checkbox", { name: /RHS/ }));
    await user.clear(within(dialog).getByLabelText(/watts/i));
    await user.type(within(dialog).getByLabelText(/watts/i), "800");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    const first = await screen.findByRole("region", { name: "Power request intent-2-1.000000" });
    const second = card("intent-3-1.000000");

    // Each card carries ITS batteries' direction and per-battery figures.
    const firstRequested = within(first).getByRole("group", { name: "Requested" }).textContent ?? "";
    expect(firstRequested).toContain("Charge");
    expect(firstRequested).toContain("1,000 W per battery (2,000 W total)");
    const secondRequested =
      within(second).getByRole("group", { name: "Requested" }).textContent ?? "";
    expect(secondRequested).toContain("Discharge");
    expect(secondRequested).toContain("800 W");

    // Independent countdowns: each runs down from ITS acceptance's expiry.
    const firstRemaining = await remainingSeconds(first);
    const secondRemaining = await remainingSeconds(second);
    expect(firstRemaining).toBeLessThanOrEqual(300);
    expect(firstRemaining).toBeGreaterThan(290);
    expect(secondRemaining).toBeLessThanOrEqual(120);
    expect(secondRemaining).toBeGreaterThan(110);

    // Neither card visually supersedes the other on units it did not name.
    expect(within(first).queryByText(/LHS/)).toBeNull();
    expect(within(second).queryByText(/MID|2000|2,000/)).toBeNull();

    // Each card owns its own cancel control.
    expect(screen.getAllByRole("button", { name: /cancel request/i })).toHaveLength(2);
  });

  it("shows a battery claimed by a newer same-priority request as taken over, while the older card's other batteries continue", async () => {
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    // Request one covers MID and RHS; a newer request then claims RHS alone.
    channel.push(acceptedFrame(42, "intent-9-1.000000", "discharge", { MID: 1000, RHS: 1000 }));
    await screen.findByRole("region", { name: "Power request intent-9-1.000000" });
    channel.push(acceptedFrame(43, "intent-10-1.000000", "charge", { RHS: 500 }, "2026-08-22T12:00:07+10:00"));
    const newer = await screen.findByRole("region", { name: "Power request intent-10-1.000000" });

    const older = card("intent-9-1.000000");

    // The older card keeps its surviving battery and its own request figures.
    const olderUnits = within(older).getByRole("list", { name: /batteries in/i });
    expect(olderUnits.textContent ?? "").toMatch(/MID.*requested 1,000 W/);
    expect(olderUnits.textContent ?? "").toMatch(/RHS.*taken over by a newer request/);
    const olderRequested = within(older).getByRole("group", { name: "Requested" }).textContent ?? "";
    expect(olderRequested).toContain("1,000 W per battery");

    // The newer card carries the battery it claimed, with its own direction.
    const newerUnits = within(newer).getByRole("list", { name: /batteries in/i });
    expect(newerUnits.textContent ?? "").toMatch(/RHS.*requested 500 W/);
    expect(newerUnits.textContent ?? "").not.toMatch(/taken over/);
  });

  it("attributes the cycle-level decision maps to each card by unit membership", async () => {
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push(acceptedFrame(42, "intent-9-1.000000", "discharge", { MID: 1000, RHS: 1000 }));
    channel.push(acceptedFrame(43, "intent-10-1.000000", "charge", { LHS: 800 }, "2026-08-22T12:00:07+10:00"));

    // One cycle composed from both intents: the maps are cycle-level (the
    // audit row correlates to `cycle:...`, not to any intent id), so each
    // card reads ITS units' entries out of them.
    channel.push({
      type: "audit.appended",
      sequence: 44,
      occurred_at: "2026-08-22T12:00:09+10:00",
      payload: {
        event_id: "facade-44",
        event_type: "control_decision",
        unit_id: null,
        generation: 9,
        result: "clamped",
        reason_codes: ["power_clamped"],
        requested_active_w: 2800,
        authorized_active_w: 2200,
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 800 },
        authorized_watts_by_unit: { MID: 1000, RHS: 400, LHS: 800 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "charge" },
      },
    });

    const dischargeCard = await waitFor(() => {
      const scope = card("intent-9-1.000000");
      const allowed = within(scope).getByRole("group", { name: "Allowed" }).textContent ?? "";
      expect(allowed).toContain("MID 1,000 W");
      expect(allowed).toContain("RHS 400 W (headroom)");
      // The charge request's battery never rides this card's figures.
      expect(allowed).not.toContain("LHS");
      return scope;
    });
    expect(
      within(dischargeCard).getByRole("group", { name: "Allowed" }).textContent ?? "",
    ).toMatch(/RHS was held back/i);

    const chargeAllowed =
      within(card("intent-10-1.000000")).getByRole("group", { name: "Allowed" }).textContent ?? "";
    expect(chargeAllowed).toContain("LHS 800 W");
    expect(chargeAllowed).not.toContain("MID");
    expect(chargeAllowed).not.toContain("RHS");
  });

  it("cancels one card's request through the live cancel endpoint and clears exactly that card", async () => {
    const user = userEvent.setup();
    const channel = armedFleetChannel();
    api.client.postIntentCancel.mockResolvedValue({
      intent_id: "intent-9-1.000000",
      status: "cancelled",
      unit_ids: ["MID", "RHS"],
    });
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push(acceptedFrame(42, "intent-9-1.000000", "discharge", { MID: 1000, RHS: 1000 }));
    channel.push(acceptedFrame(43, "intent-10-1.000000", "charge", { LHS: 800 }, "2026-08-22T12:00:07+10:00"));
    await screen.findByRole("region", { name: "Power request intent-9-1.000000" });

    await user.click(within(card("intent-9-1.000000")).getByRole("button", { name: /cancel request/i }));

    // The exact request body the endpoint's schema pins: the intent id alone.
    expect(api.client.postIntentCancel).toHaveBeenCalledWith("intent-9-1.000000");
    // The cancelled card clears; the concurrent card stays.
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Power request intent-9-1.000000" })).toBeNull();
    });
    expect(screen.getByRole("region", { name: "Power request intent-10-1.000000" })).toBeInTheDocument();
  });

  it("renders a cancel refusal's envelope inside the card and keeps the request on screen", async () => {
    const user = userEvent.setup();
    const channel = armedFleetChannel();
    api.client.postIntentCancel.mockRejectedValue(
      new ApiClientError({
        code: "intent_not_found",
        message: "The intent identifier is not active",
        details: {},
        request_id: "req-cancel-3",
        status: 404,
      }),
    );
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push(acceptedFrame(42, "intent-9-1.000000", "discharge", { MID: 1000 }));
    const scope = await screen.findByRole("region", { name: "Power request intent-9-1.000000" });

    await user.click(within(scope).getByRole("button", { name: /cancel request/i }));

    // The refusal is the envelope's own words, in the card it belongs to.
    expect(await within(scope).findByText("intent_not_found")).toBeInTheDocument();
    expect(within(scope).getByText("The intent identifier is not active")).toBeInTheDocument();
    expect(
      within(scope).getByRole("group", { name: "Remaining time" }).textContent ?? "",
    ).toContain("Not available");
  });

  it("clears a card when another operator cancels it over the bus", async () => {
    const channel = armedFleetChannel();
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push(acceptedFrame(42, "intent-9-1.000000", "discharge", { MID: 1000 }));
    await screen.findByRole("region", { name: "Power request intent-9-1.000000" });

    // The facade's cancellation announcement (service.py cancel_intent):
    // {principal, intent_id, unit_ids}.
    channel.push({
      type: "intent.cancelled",
      sequence: 43,
      occurred_at: "2026-08-22T12:01:00+10:00",
      payload: { principal: "operator:other", intent_id: "intent-9-1.000000", unit_ids: ["MID"] },
    });

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Power request intent-9-1.000000" })).toBeNull();
    });
    const notice = await screen.findByRole("status");
    expect(notice.textContent ?? "").toMatch(/intent-9-1\.000000 was cancelled/);
  });

  it("seeds exact per-unit figures from the snapshot intent block at cold load (feature-detected)", async () => {
    // A console opened mid-intent knows no card (no acceptance was seen in
    // this session), but the snapshot's `intent` block carries the request's
    // own per-unit figures: the fallback row renders them exactly, with no
    // derived "≈" split, instead of stamping the repeated fleet total.
    const snap: SnapshotEnvelope = {
      ...snapshotEnvelope([
        { ...ACTIVE_MID, requested_power: { direction: "discharge", watts: 3000 } },
        { ...ACTIVE_MID, unit_id: "RHS", requested_power: { direction: "discharge", watts: 3000 } },
        { ...ACTIVE_MID, unit_id: "LHS", requested_power: { direction: "discharge", watts: 3000 } },
      ]),
      intent: {
        requested_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        authorized_watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        directions_by_unit: { MID: "discharge", RHS: "discharge", LHS: "discharge" },
      },
    };
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() =>
      liveStream([{ type: "snapshot", sequence: 41, data: snap }]),
    );

    renderNow();

    const requested = await screen.findByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toContain("1,000 W per battery (3,000 W total)");
    expect(requested.textContent ?? "").not.toContain("≈");
    const allowed = screen.getByRole("group", { name: "Allowed" });
    expect(allowed.textContent ?? "").toContain("MID 1,000 W");
    expect(allowed.textContent ?? "").not.toContain("headroom");
  });
});

// --- arm / disarm ------------------------------------------------------------

describe("NowView — arm flow", () => {
  function renderArmable() {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([DISARMED_MID]));
    api.client.getHealth.mockResolvedValue({
      ...HEALTH_OK,
      control_readiness: { ready: false, reasons: ["no_unit_armed"] },
    });
    renderNow();
  }

  it("runs a keyboard-only arm: readiness checklist, explicit ARM, per-unit outcome, focus restored", async () => {
    const user = userEvent.setup();
    renderArmable();
    api.client.postArm.mockResolvedValue({
      units: [{ unit_id: "MID", status: "armed", reason: "armed" }],
    });

    const armButton = await screen.findByRole("button", { name: /^arm\b/i });
    armButton.focus();
    await user.keyboard("{Enter}");

    const dialog = screen.getByRole("dialog");
    // The checklist carries per-unit state (the unit beside its qualification
    // and latch state on one row), not generic advice.
    const readinessRow = tightestText(dialog, /MID/, /qualified/i);
    expect(readinessRow).toMatch(/latch/i);
    expect(readinessRow).not.toMatch(/must|all units|every unit/i);
    expect(dialog.textContent ?? "").toMatch(/policy/i);
    // The fleet's actual readiness reason surfaces, not static copy.
    expect(await within(dialog).findByText(/no_unit_armed|no unit armed/i)).toBeInTheDocument();

    // The dialog traps keyboard focus.
    for (let i = 0; i < 3; i += 1) {
      await user.keyboard("{Tab}");
      expect(dialog.contains(document.activeElement)).toBe(true);
    }

    // Explicit confirmation is the button named exactly ARM, operable by keyboard.
    const confirm = within(dialog).getByRole("button", { name: "ARM" });
    for (let i = 0; i < 10 && document.activeElement !== confirm; i += 1) {
      await user.keyboard("{Tab}");
    }
    expect(document.activeElement).toBe(confirm);
    await user.keyboard("{Enter}");

    expect(api.client.postArm).toHaveBeenCalledWith(["MID"]);
    // Per-unit outcome is visible and the dialog closes.
    expect(await screen.findByText(/^armed$/i)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    // Focus returns to the control that opened the dialog.
    expect(document.activeElement).toBe(armButton);
  });

  it("surfaces per-unit refusal reasons from the arm outcome verbatim", async () => {
    const user = userEvent.setup();
    renderArmable();
    api.client.postArm.mockResolvedValue({
      units: [{ unit_id: "MID", status: "refused", reason: "inhibit_latched" }],
    });

    await user.click(await screen.findByRole("button", { name: /^arm\b/i }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "ARM" }));

    expect(await screen.findByText(/^refused$/i)).toBeInTheDocument();
    expect(screen.getByText("inhibit_latched")).toBeInTheDocument();
  });

  it("surfaces a refused arm's error envelope code and message verbatim", async () => {
    const user = userEvent.setup();
    renderArmable();
    api.client.postArm.mockRejectedValue(
      new ApiClientError({
        code: "interactive_operator_required",
        message: "Interactive operator required",
        details: {},
        request_id: "req-arm-17",
        status: 403,
      }),
    );

    await user.click(await screen.findByRole("button", { name: /^arm\b/i }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "ARM" }));

    expect(await screen.findByText("interactive_operator_required")).toBeInTheDocument();
    expect(screen.getByText("Interactive operator required")).toBeInTheDocument();
  });
});

describe("NowView — disarm", () => {
  it("offers disarm for an armed unit and confirms before posting", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ARMED_MID]));
    api.client.postDisarm.mockResolvedValue({
      units: [{ unit_id: "MID", status: "disarmed", reason: "disarmed" }],
    });

    renderNow();
    await user.click(await screen.findByRole("button", { name: /disarm/i }));

    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent ?? "").toContain("MID");

    await user.click(within(dialog).getByRole("button", { name: /disarm/i }));

    expect(api.client.postDisarm).toHaveBeenCalledWith(["MID"]);
    expect(await screen.findByText(/^disarmed$/i)).toBeInTheDocument();
  });

  it("hides disarm when nothing is armed but keeps stop reachable", async () => {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([DISARMED_MID]));

    renderNow();
    await screen.findByRole("heading", { name: /current request/i });

    expect(screen.queryByRole("button", { name: /disarm/i })).toBeNull();
    // Stop remains available whenever control is displayed.
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });
});

// --- dispatch ------------------------------------------------------------------

describe("NowView — dispatch", () => {
  function renderArmed() {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ARMED_MID]));
    renderNow();
  }

  async function openDispatch(user: ReturnType<typeof userEvent.setup>, direction: RegExp) {
    await user.click(await screen.findByRole("button", { name: direction }));
    return screen.getByRole("dialog");
  }

  it("previews direction, watts, units, limit, and expiry before an explicit charge confirm", async () => {
    const user = userEvent.setup();
    renderArmed();
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);

    const dialog = await openDispatch(user, /^charge/i);

    const watts = within(dialog).getByLabelText(/watts per battery/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    const minutes = within(dialog).getByLabelText(/minutes|duration|ttl/i);
    await user.clear(minutes);
    await user.type(minutes, "5");
    const unit = within(dialog).getByRole("checkbox", { name: /MID/ });
    expect(unit).toBeChecked();

    // One composed preview sentence carries the direction, the typed watts,
    // the selected unit, the limit, and the expiry: static helper copy cannot
    // satisfy it because it contains the operator's own inputs. The watts are
    // PER BATTERY (2026-08-23 operator ruling) and now travel natively as each
    // battery's own target, so the figures are exactly true — no "up to".
    const preview = tightestText(dialog, /charge/i, /1,?500\s*W/, /MID/);
    expect(preview).toContain("Each battery: 1,500 W · Total: 1,500 W");
    expect(preview).toMatch(/limit/i);
    expect(preview).toMatch(/5\s*min|300\s*s/i);

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(api.client.postIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        unit_ids: ["MID"],
        direction: "charge",
        watts_by_unit: { MID: 1500 },
        ttl_s: 300,
      }),
      // One idempotency key per operator action is part of the pinned call.
      expect.any(String),
    );
    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    expect(fact("Remaining time").textContent ?? "").toMatch(/(300|5)\b/);
  });

  it("keeps discharge as a separate action posting direction discharge", async () => {
    const user = userEvent.setup();
    renderArmed();
    api.client.postIntent.mockResolvedValue({
      intent_id: "intent-3-1.000000",
      acceptance_revision: 3,
      accepted_at_monotonic: 1001,
      status: "accepted",
      requested: { direction: "discharge", watts: 800 },
      authorized: null,
      measured: null,
      expires_in_s: 300,
    });

    // Two unmistakable, separate actions.
    expect(await screen.findByRole("button", { name: /^charge/i })).toBeInTheDocument();
    const dialog = await openDispatch(user, /^discharge/i);

    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "800");
    const minutes = within(dialog).getByLabelText(/minutes|duration|ttl/i);
    await user.clear(minutes);
    await user.type(minutes, "5");

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(api.client.postIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        direction: "discharge",
        watts_by_unit: { MID: 800 },
        ttl_s: 300,
      }),
      // One idempotency key per operator action is part of the pinned call.
      expect.any(String),
    );
  });

  it("submits the per-battery entry natively as watts_by_unit — one entry per selected unit, no scalar watts field", async () => {
    // The operator's ruling verbatim: "I asked for each setting to be one
    // thousand, not a total of 1,000." The backend's native per-unit form now
    // carries that meaning on the wire: every selected unit gets the
    // operator's entry as its own target, the scalar `watts` field is OMITTED
    // (the two forms are mutually exclusive — both together is a 422), and the
    // facade derives the 3,000 W fleet total itself.
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([ARMED_MID, ARMED_RHS, { ...ARMED_MID, unit_id: "LHS" }]),
    );
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);
    renderNow();

    const dialog = await openDispatch(user, /^charge/i);
    const watts = within(dialog).getByLabelText(/watts per battery/i);
    await user.clear(watts);
    await user.type(watts, "1000");

    // The multiplication is on screen before any confirm.
    expect(
      within(dialog).getByText("1,000 W × 3 batteries selected = 3,000 W total"),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(api.client.postIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        unit_ids: ["MID", "RHS", "LHS"],
        direction: "charge",
        watts_by_unit: { MID: 1000, RHS: 1000, LHS: 1000 },
        ttl_s: 300,
      }),
      expect.any(String),
    );
    const [body] = api.client.postIntent.mock.calls[0] as unknown as [
      Record<string, unknown>,
      string | undefined,
    ];
    // The exact-key-set rule (rest.py IntentRequest): the map names every
    // selected unit — all of them — and nothing else.
    expect(Object.keys(body.watts_by_unit as Record<string, number>).sort()).toEqual([
      "LHS",
      "MID",
      "RHS",
    ]);
    // The scalar form is never sent alongside: both together is a 422.
    expect(body).not.toHaveProperty("watts");
  });

  it("keys watts_by_unit to exactly the batteries still selected when one is unticked", async () => {
    // The client-side mirror of the wire's exact-key-set validation: a
    // deselected battery must not ride along in the per-unit map, and every
    // remaining selection must be present.
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([ARMED_MID, ARMED_RHS, { ...ARMED_MID, unit_id: "LHS" }]),
    );
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);
    renderNow();

    const dialog = await openDispatch(user, /^charge/i);
    await user.click(within(dialog).getByRole("checkbox", { name: /LHS/ }));
    const watts = within(dialog).getByLabelText(/watts per battery/i);
    await user.clear(watts);
    await user.type(watts, "1000");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(api.client.postIntent).toHaveBeenCalledTimes(1);
    const [body] = api.client.postIntent.mock.calls[0] as unknown as [
      Record<string, unknown>,
      string | undefined,
    ];
    expect(body.unit_ids).toEqual(["MID", "RHS"]);
    const map = body.watts_by_unit as Record<string, number>;
    expect(Object.keys(map).sort()).toEqual(["MID", "RHS"]);
    expect(map).toEqual({ MID: 1000, RHS: 1000 });
    expect(body).not.toHaveProperty("watts");
  });

  it("re-derives the live per-battery math as the unit selection changes", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([ARMED_MID, ARMED_RHS, { ...ARMED_MID, unit_id: "LHS" }]),
    );
    renderNow();

    const dialog = await openDispatch(user, /^discharge/i);
    const watts = within(dialog).getByLabelText(/watts per battery/i);
    await user.clear(watts);
    await user.type(watts, "1000");

    expect(
      within(dialog).getByText("1,000 W × 3 batteries selected = 3,000 W total"),
    ).toBeInTheDocument();

    // Unticking one battery re-derives the total live: the figure beside
    // Confirm is never a stale multiplication.
    await user.click(within(dialog).getByRole("checkbox", { name: /RHS/ }));
    expect(
      within(dialog).getByText("1,000 W × 2 batteries selected = 2,000 W total"),
    ).toBeInTheDocument();
  });

  it("refuses watts above the per-battery cap with the bound named, and accepts the boundary value", async () => {
    const user = userEvent.setup();
    renderArmed();
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);

    const dialog = await openDispatch(user, /^charge/i);
    const watts = within(dialog).getByLabelText(/watts per battery/i);

    // The guidance names the per-battery bound up front.
    expect(describedText(watts)).toMatch(/max 2,?500 W per battery/i);

    await user.clear(watts);
    await user.type(watts, "2501");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    // Over the cap: refused before any POST, with the bound named on the
    // field itself — never silently clamped.
    expect(
      await within(dialog).findByText("Watts per battery are too high: the bound is 2,500 W per battery."),
    ).toBeInTheDocument();
    expect(api.client.postIntent).not.toHaveBeenCalled();

    // The boundary itself is within the cap and posts as-is.
    await user.clear(watts);
    await user.type(watts, "2500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    await waitFor(() => {
      expect(api.client.postIntent).toHaveBeenCalledTimes(1);
    });
    expect(api.client.postIntent).toHaveBeenCalledWith(
      expect.objectContaining({ watts_by_unit: { MID: 2500 }, ttl_s: 300 }),
      expect.any(String),
    );
  });

  it("retries one failed dispatch as the same operator action under one idempotency key", async () => {
    const user = userEvent.setup();
    renderArmed();
    api.client.postIntent
      .mockRejectedValueOnce(
        new ApiClientError({
          code: "internal_error",
          message: "The request could not be completed",
          details: {},
          request_id: "req-dispatch-4",
          status: 500,
        }),
      )
      .mockResolvedValue(ACCEPTED_CHARGE);

    const dialog = await openDispatch(user, /^charge/i);
    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    const minutes = within(dialog).getByLabelText(/minutes|duration|ttl/i);
    await user.clear(minutes);
    await user.type(minutes, "5");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    // The failure surfaces with the dialog kept open for the operator.
    expect(await within(dialog).findByText("internal_error")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    await waitFor(() => expect(api.client.postIntent).toHaveBeenCalledTimes(2));
    const calls = api.client.postIntent.mock.calls as unknown as Array<
      [Record<string, unknown>, string | undefined]
    >;
    const [firstBody, firstKey] = calls[0]!;
    const [secondBody, secondKey] = calls[1]!;
    // Same action retried: the same body AND the same caller-supplied
    // idempotency key, so the retry replays as one operator action instead of
    // dispatching twice (UI_CONTRACTS.md "Data sources").
    expect(secondBody).toEqual(firstBody);
    expect(firstKey).toBeDefined();
    expect(secondKey).toBe(firstKey);
  });

  it("constrains inputs inline: non-positive watts and over-bound duration never post", async () => {
    const user = userEvent.setup();
    renderArmed();

    const dialog = await openDispatch(user, /^charge/i);

    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "0");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));
    // The guard is associated with the watts field itself, not just present
    // somewhere in the dialog.
    await waitFor(() => {
      expect(describedText(watts)).toMatch(
        /positive|greater than 0|above zero|more than 0/i,
      );
    });
    expect(api.client.postIntent).not.toHaveBeenCalled();

    await user.clear(watts);
    await user.type(watts, "1500");
    const minutes = within(dialog).getByLabelText(/minutes|duration|ttl/i);
    await user.clear(minutes);
    await user.type(minutes, "999");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));
    await waitFor(() => {
      expect(describedText(minutes)).toMatch(/5 minutes|300|too long|bound/i);
    });
    expect(api.client.postIntent).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renders an API validation error inline with the field, verbatim", async () => {
    const user = userEvent.setup();
    renderArmed();
    api.client.postIntent.mockRejectedValue(
      new ApiClientError({
        code: "validation_error",
        message: "Request validation failed",
        details: {
          // The form submits the per-unit form, so the API names that field;
          // the per-battery input owns the error either way (a location naming
          // the legacy scalar `watts` maps to the same field).
          errors: [
            { location: ["body", "watts_by_unit"], message: "Input should be greater than 0", type: "greater_than" },
          ],
        },
        request_id: "req-val-31",
        status: 422,
      }),
    );

    const dialog = await openDispatch(user, /^charge/i);
    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    // The envelope's code and message render associated with the field the
    // API rejected, and the dialog stays open.
    await within(dialog).findByText("validation_error");
    expect(describedText(watts)).toMatch(/validation_error/);
    expect(describedText(watts)).toContain("Request validation failed");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(api.client.postIntent).toHaveBeenCalledTimes(1);
  });
});

// --- emergency stop and acknowledgements ---------------------------------------

describe("NowView — emergency stop", () => {
  async function stopFleet(user: ReturnType<typeof userEvent.setup>) {
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ACTIVE_MID, ARMED_RHS]));
    api.client.postEmergencyStop.mockResolvedValue({
      stop_id: "stop-7",
      status: "latched",
      unit_ids: ["MID", "RHS"],
      fenced_generation: 4,
      degraded: [],
    });
    renderNow();

    await user.click(await screen.findByRole("button", { name: /stop/i }));
    const dialog = screen.getByRole("dialog");
    // Confirmation names every affected unit.
    const copy = dialog.textContent ?? "";
    expect(copy).toContain("MID");
    expect(copy).toContain("RHS");
    await user.click(within(dialog).getByRole("button", { name: /confirm|stop/i }));
    return dialog;
  }

  it("confirms the affected units, announces the latch, and shows the stop id", async () => {
    const user = userEvent.setup();
    await stopFleet(user);

    expect(api.client.postEmergencyStop).toHaveBeenCalledWith(
      ["MID", "RHS"],
      expect.any(String),
    );
    // The stop id is shown after stopping.
    expect(await screen.findByText(/stop-7/)).toBeInTheDocument();
    // A latched stop is an emergency event: it announces assertively.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/stop/i);
  });

  it("surfaces a degraded stop's reasons faithfully", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ACTIVE_MID, ARMED_RHS]));
    api.client.postEmergencyStop.mockResolvedValue({
      stop_id: "stop-9",
      status: "latched",
      unit_ids: ["MID", "RHS"],
      fenced_generation: 5,
      degraded: ["audit_unavailable"],
    });
    renderNow();

    await user.click(await screen.findByRole("button", { name: /stop/i }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /confirm|stop/i }),
    );

    expect(await screen.findByText(/stop-9/)).toBeInTheDocument();
    // `degraded` is a list of reason strings: each one reaches the operator.
    expect(await screen.findByText(/audit_unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/degrad/i)).toBeInTheDocument();
  });

  it("requires the exact stop id for acknowledgement, operable by keyboard", async () => {
    const user = userEvent.setup();
    await stopFleet(user);
    api.client.postStopAcknowledgement.mockResolvedValue({
      stop_id: "stop-7",
      status: "acknowledged",
    });

    const field = await screen.findByLabelText(/stop id/i);
    await user.type(field, "stop-8");
    const acknowledge = screen.getByRole("button", { name: /acknowledge/i });
    // A different id must not satisfy the confirmation.
    expect(acknowledge).toBeDisabled();

    await user.clear(field);
    await user.type(field, "stop-7");
    expect(acknowledge).toBeEnabled();

    acknowledge.focus();
    await user.keyboard("{Enter}");

    expect(api.client.postStopAcknowledgement).toHaveBeenCalledWith("stop-7");
    expect(await screen.findByText(/^acknowledged$/i)).toBeInTheDocument();
  });
});

describe("NowView — inhibit acknowledgement", () => {
  it("explains the latch before confirming and reports the cleared latch", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([
        ACTIVE_MID,
        {
          unit_id: "LHS",
          lifecycle: "inhibited",
          telemetry_age_s: 2,
          quality: "suspect",
          requested_power: { direction: "idle", watts: 0 },
          authorized_power: null,
          measured_watts: null,
        },
      ]),
    );
    api.client.postInhibitAcknowledgement.mockResolvedValue({
      unit_id: "LHS",
      status: "acknowledged",
      latch_cleared: true,
    });

    renderNow();
    await user.click(await screen.findByRole("button", { name: /acknowledge/i }));

    const dialog = screen.getByRole("dialog");
    const copy = dialog.textContent ?? "";
    // The copy explains the effect: the latch clears, arming stays separate.
    expect(copy).toMatch(/latch/i);
    expect(copy).toMatch(/arm/i);
    expect(copy).toMatch(/separate|re-?arm|still/i);

    await user.click(within(dialog).getByRole("button", { name: /confirm|acknowledge/i }));

    expect(api.client.postInhibitAcknowledgement).toHaveBeenCalledWith("LHS");
    expect(await screen.findByText(/cleared/i)).toBeInTheDocument();
  });
});

// --- composed-app regressions ---------------------------------------------------
//
// The isolated pins above held while the composed console still showed a
// refused unit as armed, a latched fleet as dispatchable, and a countdown that
// never moved. These tests pin the composed behavior directly.

describe("NowView — refusals arriving over the stream", () => {
  it.each([
    {
      action: "arm" as const,
      frameType: "unit.armed",
      before: DISARMED_MID,
      // MID stays disarmed: arming is still offered, dispatching is not.
      stillPresent: [/^arm\b/i],
      stillAbsent: [/^disarm/i, /^charge/i, /^discharge/i],
    },
    {
      action: "disarm" as const,
      frameType: "unit.disarmed",
      before: ARMED_MID,
      // MID stays armed: disarm and dispatch stay available, arming does not.
      stillPresent: [/^disarm/i, /^charge/i, /^discharge/i],
      stillAbsent: [/^arm\b/i],
    },
  ])(
    "renders a refused $action row as a refusal with its reason, never as a lifecycle change",
    async ({ action, frameType, before, stillPresent, stillAbsent }) => {
      const snap = snapshotEnvelope([before]);
      const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
      api.client.getSnapshot.mockResolvedValue(snap);
      api.client.openEvents.mockImplementation(() => channel.openEvents());

      renderNow();
      await screen.findByRole("group", { name: "Requested" });

      channel.push({
        type: frameType,
        sequence: 42,
        occurred_at: "2026-08-22T12:00:05+10:00",
        payload: {
          principal: "operator-7",
          units: [{ unit_id: "MID", status: "refused", reason: "inhibit_latched" }],
        },
      });

      const refusal = await screen.findByRole("region", { name: /live control refusal/i });
      expect(within(refusal).getByText(/^refused$/i)).toBeInTheDocument();
      expect(within(refusal).getByText("inhibit_latched")).toBeInTheDocument();
      expect(refusal.textContent ?? "").toContain(action);

      // The refused outcome is NOT a lifecycle change: the unit stays exactly
      // where it was, so the controls still describe the truth. A refused arm
      // never leaves a unit armed (or dispatchable), and a refused disarm never
      // offers arming for a unit that is still armed.
      await waitFor(() => {
        for (const pattern of stillPresent) {
          expect(screen.getByRole("button", { name: pattern })).toBeInTheDocument();
        }
      });
      for (const pattern of stillAbsent) {
        expect(screen.queryByRole("button", { name: pattern })).toBeNull();
      }
    },
  );

  it("keeps a successful row from the same frame working next to a refused one", async () => {
    const snap = snapshotEnvelope([DISARMED_MID, { ...DISARMED_MID, unit_id: "RHS" }]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    channel.push({
      type: "unit.armed",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        principal: "operator-7",
        units: [
          { unit_id: "MID", status: "refused", reason: "inhibit_latched" },
          { unit_id: "RHS", status: "armed", reason: "armed" },
        ],
      },
    });

    const refusal = await screen.findByRole("region", { name: /live control refusal/i });
    expect(within(refusal).getByText("inhibit_latched")).toBeInTheDocument();
    expect(within(refusal).queryByText(/RHS/)).toBeNull();
    // RHS really armed (disarm/dispatch now possible), MID did not.
    expect(await screen.findByRole("button", { name: /^disarm/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^arm\b/i })).toBeInTheDocument();
  });
});

describe("NowView — latches arriving over the stream", () => {
  it("applies an emergency_stop.latched frame: the fleet is inhibited, not dispatchable, and a snapshot refetch follows", async () => {
    const snap = snapshotEnvelope([ARMED_MID, ARMED_RHS]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    // The refetch resolves the pre-latch world again: the guard must keep the
    // latched (newer) picture, exactly as a cached shared read would.
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(screen.getByRole("button", { name: /^disarm/i })).toBeInTheDocument();

    channel.push({
      type: "emergency_stop.latched",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:09+10:00",
      payload: {
        principal: "operator-7",
        stop_id: "stop-11-1001.000000",
        unit_ids: ["MID", "RHS"],
        reason: "operator requested from the console",
        generation: 7,
        degraded: ["audit_unavailable"],
      },
    });

    // A latched stop is an emergency: it announces assertively with its id, and
    // a degraded stop's reasons arrive with it (degraded is a list, not a flag).
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/audit_unavailable/);
    expect(alert).toHaveTextContent(/emergency stop/i);
    expect(alert).toHaveTextContent(/stop-11-1001\.000000/);
    expect(alert).toHaveTextContent(/MID/);

    // No unit may present as armed or dispatchable while the latch holds —
    // but the controls stay on screen, disabled with the stop named (never
    // silently dead: a vanished button left the operator nothing to read).
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^disarm/i })).toBeDisabled();
    });
    expect(screen.getByRole("button", { name: /^charge/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^discharge/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^arm\b/i })).toBeDisabled();
    expect(
      screen.getByText(/held by emergency stop stop-11-1001\.000000/i),
    ).toBeInTheDocument();
    // Stopping is always possible: the stop control itself stays usable.
    expect(screen.getByRole("button", { name: /emergency stop/i })).toBeEnabled();
    // The latched fleet is acknowledgeable, unit by unit.
    expect(screen.getByRole("button", { name: /acknowledge MID/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /acknowledge RHS/i })).toBeInTheDocument();

    // Both halves of the contract: the event drives the picture immediately,
    // and the snapshot is refetched right after (a fresher snapshot would win).
    await waitFor(() => {
      expect(api.client.getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    const requested = screen.getByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toMatch(/none/i);
  });

  it("clears the latch picture when the stop is acknowledged and the refreshed snapshot advances", async () => {
    const armed = snapshotEnvelope([ARMED_MID], 41);
    // The post-acknowledgement snapshot is newer than both latch frames, so it
    // wins; while the latch holds, the refetch answers the pre-latch world and
    // the guard keeps the latched picture.
    const recovered = snapshotEnvelope([DISARMED_MID], 45);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: armed }]);
    let acknowledged = false;
    api.client.getSnapshot.mockImplementation(() =>
      Promise.resolve(acknowledged ? recovered : armed),
    );
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(screen.getByRole("button", { name: /^disarm/i })).toBeInTheDocument();

    channel.push({
      type: "emergency_stop.latched",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:09+10:00",
      payload: {
        principal: "operator-7",
        stop_id: "stop-11-1001.000000",
        unit_ids: ["MID"],
        reason: "operator requested from the console",
        generation: 7,
        degraded: [],
      },
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^disarm/i })).toBeDisabled();
    });
    expect(screen.getByRole("button", { name: /acknowledge MID/i })).toBeInTheDocument();

    acknowledged = true;
    channel.push({
      type: "emergency_stop.acknowledged",
      sequence: 44,
      occurred_at: "2026-08-22T12:00:20+10:00",
      payload: { principal: "operator-7", stop_id: "stop-11-1001.000000" },
    });

    // The acknowledgement retires the latch notice and the refreshed snapshot
    // (a newer sequence) says the pod re-qualified to disarmed.
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /acknowledge MID/i })).toBeNull();
    });
    expect(screen.getByRole("button", { name: /^arm\b/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^disarm/i })).toBeNull();
    expect(screen.queryByText(/stop-11-1001/i)).toBeNull();
  });

  it("re-reads the world an authorization.revoked frame changes, without inventing a lifecycle", async () => {
    const snap = snapshotEnvelope([ARMED_MID, ARMED_RHS]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(screen.getByRole("button", { name: /^disarm/i })).toBeInTheDocument();

    // The runtime publishes this for every unit that still held authority, and
    // revocation is routine (intent expiry, disarm, generation fences) — not
    // only a latched inhibit. The frame says authority changed; it does not say
    // the unit latched, so the view may not label it inhibited.
    channel.push({
      type: "authorization.revoked",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:15+10:00",
      payload: { reason: "identity_mismatch", unit_ids: ["MID"] },
    });

    const notice = await screen.findByRole("status");
    expect(notice.textContent ?? "").toMatch(/MID/);
    expect(notice.textContent ?? "").toMatch(/identity_mismatch/);

    // No invented latch: the refreshed snapshot alone may move the lifecycle,
    // so no acknowledge control appears and no assertive latch alert does.
    expect(screen.queryByRole("button", { name: /acknowledge MID/i })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => {
      expect(api.client.getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("treats an inhibit.acknowledged frame as a cleared latch, never as a lifecycle change, and refetches", async () => {
    const latched: UnitView = {
      unit_id: "LHS",
      lifecycle: "inhibited",
      telemetry_age_s: 2,
      quality: "good",
      requested_power: { direction: "idle", watts: 0 },
      authorized_power: null,
      measured_watts: null,
    };
    const snap = snapshotEnvelope([ARMED_MID, latched]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(screen.getByRole("button", { name: /acknowledge LHS/i })).toBeInTheDocument();

    channel.push({
      type: "inhibit.acknowledged",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:12+10:00",
      payload: { principal: "operator-7", unit_id: "LHS", latch_cleared: true },
    });

    const notice = await screen.findByRole("status");
    expect(notice.textContent ?? "").toMatch(/LHS/);
    expect(notice.textContent ?? "").toMatch(/latch/i);

    // The unit is NOT relabeled disarmed or armed by the acknowledgement: only
    // the refreshed snapshot may move its lifecycle (the pod re-qualifies
    // through stable samples first).
    await waitFor(() => {
      expect(api.client.getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    expect(screen.queryByRole("button", { name: /^arm\b/i })).toBeNull();
    expect(screen.getByRole("button", { name: /acknowledge LHS/i })).toBeInTheDocument();
  });
});

// --- controls held by a snapshot-reported emergency stop -----------------------
//
// The amended snapshot contract's active_stops (2026-08-23 incident fix): a
// console opened mid-latch learns the stop from the world itself, and the
// controls it holds are disabled WITH the stop named — never silently dead.

describe("NowView — controls held by a snapshot-reported emergency stop", () => {
  const STOP_ID = "stop-5-3753.297000";

  it("disables arm, disarm, charge and discharge with the stop named; the stop control stays usable", async () => {
    const stopped = withActiveStop(
      snapshotEnvelope([
        { ...DISARMED_MID, lifecycle: "inhibited" },
        { ...ARMED_RHS, lifecycle: "inhibited" },
      ]),
      STOP_ID,
    );
    api.client.getSnapshot.mockResolvedValue(stopped);
    api.client.openEvents.mockImplementation(() =>
      liveStream([{ type: "snapshot", sequence: stopped.snapshot_sequence, data: stopped }]),
    );
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    // The hold is on the record: the reason names the stop and points at the
    // shell's latch banner, where the release lives.
    expect(
      await screen.findByText(
        `Held by emergency stop ${STOP_ID} — acknowledge on the banner to release`,
      ),
    ).toBeInTheDocument();

    for (const name of [/^arm\b/i, /^disarm/i, /^charge/i, /^discharge/i]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      // The disabled control explains itself to assistive technology too.
      expect(describedText(button)).toContain(STOP_ID);
    }
    // Stopping is always possible.
    expect(screen.getByRole("button", { name: /emergency stop/i })).toBeEnabled();
  });

  it("releases the hold when the acknowledgement clears it in an advancing snapshot", async () => {
    const held = withActiveStop(
      snapshotEnvelope([{ ...ARMED_MID, lifecycle: "inhibited" }]),
      STOP_ID,
    );
    const recovered = snapshotEnvelope([DISARMED_MID], 45);
    const channel = liveChannel([
      { type: "snapshot", sequence: held.snapshot_sequence, data: held },
    ]);
    let acknowledged = false;
    api.client.getSnapshot.mockImplementation(() =>
      Promise.resolve(acknowledged ? recovered : held),
    );
    api.client.openEvents.mockImplementation(() => channel.openEvents());
    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(screen.getByRole("button", { name: /^arm\b/i })).toBeDisabled();

    // The acknowledgement (another session, the banner, or this one): the
    // frame retires the hold immediately, and the refreshed, newer snapshot
    // confirms a world with no engaged stops.
    acknowledged = true;
    channel.push({
      type: "emergency_stop.acknowledged",
      sequence: 44,
      occurred_at: "2026-08-22T12:00:20+10:00",
      payload: { principal: "operator-7", stop_id: STOP_ID },
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^arm\b/i })).toBeEnabled();
    });
    expect(screen.getByRole("button", { name: /^arm\b/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^discharge/i })).toBeNull();
    expect(screen.queryByText(/Held by emergency stop/)).toBeNull();
  });

  it("feature-detects: an inhibited fleet with no active_stops field is never guessed into a stop hold", async () => {
    // Today's backend sends no active_stops; an inhibited lifecycle alone must
    // not fabricate a named stop (the reason would name an id nobody knows).
    const snap = snapshotEnvelope([{ ...DISARMED_MID, lifecycle: "inhibited" }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() =>
      liveStream([{ type: "snapshot", sequence: 41, data: snap }]),
    );
    renderNow();
    await screen.findByRole("group", { name: "Requested" });

    expect(screen.queryByText(/Held by emergency stop/)).toBeNull();
    // Inhibited units are not armable or dispatchable — the controls are
    // simply absent, exactly as before the contract lands.
    expect(screen.queryByRole("button", { name: /^arm\b/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^discharge/i })).toBeNull();
  });
});


// --- structural reactivity: the request card appears and disappears -----------
//
// The request card's remaining time once lingered as a ghost after the intent
// ended (the expiry publishes authorization.revoked today; a dedicated
// intent.expired event is queued backend-side). Both endings must clear it.

describe("NowView — the request card clears when the request ends", () => {
  it("counts down while the request lives and returns to not-available on the expiry revocation", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ARMED_MID]));
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);
    const channel = liveChannel([]);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await user.click(await screen.findByRole("button", { name: /^charge/i }));
    const dialog = screen.getByRole("dialog");
    await user.clear(within(dialog).getByLabelText(/watts/i));
    await user.type(within(dialog).getByLabelText(/watts/i), "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));
    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();

    // The card is live: a remaining time that counts down.
    expect(screen.getByRole("group", { name: "Remaining time" }).textContent ?? "").toMatch(/\d+ s left/);

    // The intent expires: the runtime revokes the held authorization, and the
    // ghost countdown must go — the honest no-request state returns.
    channel.push({
      type: "authorization.revoked",
      sequence: 44,
      occurred_at: "2026-08-22T12:05:00+10:00",
      payload: { reason: "no_active_intent", unit_ids: ["MID"] },
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Remaining time" }).textContent ?? "").toContain(
        "Not available",
      );
    });
  });

  it("clears the countdown on the backend's future intent.expired event the moment it arrives", async () => {
    const user = userEvent.setup();
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ARMED_MID]));
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);
    const channel = liveChannel([]);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await user.click(await screen.findByRole("button", { name: /^charge/i }));
    const dialog = screen.getByRole("dialog");
    await user.clear(within(dialog).getByLabelText(/watts/i));
    await user.type(within(dialog).getByLabelText(/watts/i), "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));
    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Remaining time" }).textContent ?? "").toMatch(/\d+ s left/);

    // The dedicated expiry event: the wire's payload names the lapsed intent
    // by id (composition.py `_TrackingIntentRepository`), so the card goes by
    // its own identity.
    channel.push({
      type: "intent.expired",
      sequence: 45,
      occurred_at: "2026-08-22T12:05:00+10:00",
      payload: {
        intent_id: "intent-2-1.000000",
        source: "manual",
        direction: "charge",
        watts: 1500,
        unit_ids: ["MID"],
      },
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Remaining time" }).textContent ?? "").toContain(
        "Not available",
      );
    });
  });
});

describe("NowView — ages run from captured monotonic markers", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** The seconds currently shown in the Remaining time fact. */
  function remainingSecondsShown(): number {
    const text = fact("Remaining time").textContent ?? "";
    const match = /(\d+)\s*s/.exec(text);
    if (match === null) {
      throw new Error(`the Remaining time fact carries no seconds: ${text}`);
    }
    return Number(match[1]);
  }

  it("runs the remaining-time countdown down instead of freezing it at the accepted value", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    api.client.getSnapshot.mockResolvedValue(snapshotEnvelope([ARMED_MID]));
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);

    renderNow();
    await user.click(await screen.findByRole("button", { name: /^charge/i }));
    const dialog = screen.getByRole("dialog");
    await user.clear(within(dialog).getByLabelText(/watts/i));
    await user.type(within(dialog).getByLabelText(/watts/i), "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    const initial = remainingSecondsShown();
    expect(initial).toBeLessThanOrEqual(300);
    expect(initial).toBeGreaterThan(290);

    // Five seconds later the countdown has moved: a 300 s request that never
    // counts down is a contract violation, not a display detail.
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    await waitFor(() => {
      const later = remainingSecondsShown();
      expect(later).toBeLessThan(initial);
      expect(initial - later).toBeGreaterThanOrEqual(4);
    });
  });

  it("advances a telemetry age from the moment the snapshot was captured", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([{ ...ACTIVE_MID, telemetry_age_s: 10 }]),
    );

    renderNow();
    const actual = await screen.findByRole("group", { name: "Actual" });
    expect(actual.textContent ?? "").toMatch(/10 s ago/);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Actual" }).textContent ?? "").toMatch(/1[45] s ago/);
    });
  });
});

describe("NowView — post-mutation refresh: Allowed and Actual follow the request", () => {
  it("re-reads the snapshot after a dispatch so the allowed and measured facts update", async () => {
    const user = userEvent.setup();
    // Before: armed and idle (allowed "None"). After: the safety system granted
    // 1,000 W and the pod is measured at 980 W — a newer sequence, so adopting
    // it is exactly what the guard must allow.
    const before = snapshotEnvelope([ARMED_MID], 41);
    const after = snapshotEnvelope([ACTIVE_MID], 42);
    let calls = 0;
    api.client.getSnapshot.mockImplementation(() => {
      calls += 1;
      return Promise.resolve(calls >= 3 ? after : before);
    });
    api.client.postIntent.mockResolvedValue(ACCEPTED_CHARGE);

    renderNow();
    await user.click(await screen.findByRole("button", { name: /^charge/i }));
    const dialog = screen.getByRole("dialog");
    const allowedBefore = fact("Allowed").textContent ?? "";
    expect(allowedBefore).not.toMatch(/1,?000/);

    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(await screen.findByText(/^accepted$/i)).toBeInTheDocument();
    // Allowed and Actual are the two facts only the API can answer after a
    // dispatch; the re-read makes them current instead of connect-time frozen.
    await waitFor(() => {
      expect(fact("Allowed").textContent ?? "").toMatch(/1,?000\s*W/);
    });
    expect(fact("Actual").textContent ?? "").toMatch(/980\s*W/);
    expect(api.client.getSnapshot.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});

describe("NowView — the arm gate reads current readiness", () => {
  it("derives the latch line from live control readiness reasons, and re-reads them on open", async () => {
    const user = userEvent.setup();
    const snap = snapshotEnvelope([DISARMED_MID]);
    const latched = {
      ...HEALTH_OK,
      control_readiness: { ready: false, reasons: ["MID:inhibit_latched", "no_unit_armed"] },
    };
    let healthCalls = 0;
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.getHealth.mockImplementation(() => {
      healthCalls += 1;
      return Promise.resolve(healthCalls === 1 ? HEALTH_OK : latched);
    });

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    await user.click(screen.getByRole("button", { name: /^arm\b/i }));

    const dialog = screen.getByRole("dialog");
    // The latch the service reports reaches the checklist row itself...
    const row = tightestText(dialog, /MID/, /qualified/i);
    expect(row).toMatch(/inhibit latched/i);
    expect(row).not.toMatch(/latch clear/);
    // ...and the raw reason is listed beside it, not hidden.
    expect(await within(dialog).findByText(/MID:inhibit_latched/)).toBeInTheDocument();
    // Opening the gate re-read readiness: the second health call happened.
    expect(healthCalls).toBeGreaterThanOrEqual(2);
  });

  it("reports a not-qualified unit from the readiness reason instead of green-lighting it", async () => {
    const user = userEvent.setup();
    const snap = snapshotEnvelope([DISARMED_MID]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.getHealth.mockResolvedValue({
      ...HEALTH_OK,
      control_readiness: { ready: false, reasons: ["MID:not_qualified", "no_unit_armed"] },
    });

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    await user.click(screen.getByRole("button", { name: /^arm\b/i }));

    // Requiring the latch phrase keeps the match on the checklist row itself
    // rather than the shorter raw-reason list item.
    const row = tightestText(screen.getByRole("dialog"), /MID/, /qualified/i, /latch/);
    expect(row).toMatch(/not qualified \(not_qualified\)/i);
    expect(row).toMatch(/latch clear/);
  });
});

describe("NowView — snapshot adoption is guarded by sequence", () => {
  it("ignores a snapshot whose sequence does not advance the picture on screen", async () => {
    const newer = snapshotEnvelope([ACTIVE_MID], 42);
    const older = snapshotEnvelope(
      [{ ...ACTIVE_MID, requested_power: { direction: "discharge", watts: 900 } }],
      41,
    );
    const channel = liveChannel([{ type: "snapshot", sequence: 42, data: newer }]);
    // The REST read resolves the older world after the newer frame landed
    // (exactly how the shell's cached shared read can answer a late refetch).
    api.client.getSnapshot.mockResolvedValue(older);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    const requested = await screen.findByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toMatch(/charge/i);

    // A republished stale frame (the shared plane hands every newly mounted
    // view its latest snapshot frame) must not rewind the newer picture.
    channel.push({ type: "snapshot", sequence: 41, data: older });

    await waitFor(() => {
      expect(api.client.getSnapshot).toHaveBeenCalled();
    });
    const still = screen.getByRole("group", { name: "Requested" });
    expect(still.textContent ?? "").toMatch(/charge/i);
    expect(still.textContent ?? "").toMatch(/1,?500\s*W/);
    expect(still.textContent ?? "").not.toMatch(/900/);
  });
});

// --- live-update regression pins (2026-08-23 incident) -----------------------
//
// The snapshot frame arrives exactly once per connection (rest.py); the
// per-cycle observation.published frames are the live freshness signal
// (composition.py). Now once computed "…s ago" from the mount-time snapshot,
// so the figure climbed past minutes while the pod was publishing fine. These
// pin the corrected binding and the controller-restart adoption.

describe("NowView — live observations keep the actual age current", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resets the actual-age when an observation arrives and keeps ticking from it", async () => {
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
    });
    api.client.getSnapshot.mockResolvedValue(
      snapshotEnvelope([{ ...ACTIVE_MID, telemetry_age_s: 10 }]),
    );
    const channel = liveChannel([
      { type: "snapshot", sequence: 41, data: snapshotEnvelope([{ ...ACTIVE_MID, telemetry_age_s: 10 }]) },
    ]);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    const actual = await screen.findByRole("group", { name: "Actual" });
    expect(actual.textContent ?? "").toMatch(/10 s ago/);

    // The pod just published: the measurement is fresh NOW, so the age
    // restarts from zero instead of climbing to 11, 12…
    channel.push({
      type: "observation.published",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:10+10:00",
      payload: { unit_id: "MID", connection_epoch: 3, sequence: 420 },
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Actual" }).textContent ?? "").toMatch(/[0-2] s ago/);
    });

    // And it keeps ticking from the observation — the healthy sawtooth.
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Actual" }).textContent ?? "").toMatch(/[3-9] s ago/);
    });
  });

  it("adopts the renumbered snapshot after a controller restart instead of freezing the pre-restart picture", async () => {
    const before = snapshotEnvelope([ACTIVE_MID], 42);
    const after = snapshotEnvelope(
      [
        {
          ...ACTIVE_MID,
          requested_power: { direction: "charge", watts: 700 },
        },
      ],
      5,
    );
    let connections = 0;
    api.client.getSnapshot.mockResolvedValue(before);
    api.client.openEvents.mockImplementation(() => {
      connections += 1;
      if (connections === 1) {
        // Healthy, then the controller process is swapped: the socket dies.
        return endingStream([{ type: "snapshot", sequence: 42, data: before }]);
      }
      // The resume carries the pre-restart cursor; the restarted service
      // answers with its renumbered world (sequence 5 < 42).
      return liveStream([{ type: "snapshot", sequence: 5, data: after }]);
    });

    renderNow();
    const requested = await screen.findByRole("group", { name: "Requested" });
    expect(requested.textContent ?? "").toMatch(/1,?500\s*W/);

    // The reconnect lands the new world: never a frozen pre-restart picture.
    await waitFor(
      () => {
        expect(screen.getByRole("group", { name: "Requested" }).textContent ?? "").toMatch(/700 W/);
      },
      { timeout: 5000 },
    );
  });
});

// --- the authority-grant bus frame (live on the wire since 2026-08-23) -------
//
// Live capture (2026-08-23, discharge intent-3): the composition publishes
// one `authorization.granted` frame per control cycle, carrying the batch's
// per-unit authorized watts and directions. It is the FRESHEST bus source for
// the Allowed figure — it lands at the moment of the grant, before any
// snapshot or decision summary — so the shared tracker must eat it.

describe("NowView — allowed figures from the bus's authorization.granted", () => {
  it("moves the Allowed fact the moment a grant lands, and again when a later grant clamps it", async () => {
    // An accepted request with NO authorization yet: the snapshot carries the
    // request, authorized_power is null, and no intent-block map exists.
    const snap = snapshotEnvelope([
      { ...ACTIVE_MID, authorized_power: null, measured_watts: null },
    ]);
    const channel = liveChannel([{ type: "snapshot", sequence: 41, data: snap }]);
    api.client.getSnapshot.mockResolvedValue(snap);
    api.client.openEvents.mockImplementation(() => channel.openEvents());

    renderNow();
    await screen.findByRole("group", { name: "Requested" });
    expect(fact("Allowed").textContent ?? "").toMatch(/none yet/i);

    // The kernel grants authority: the per-unit figure renders immediately,
    // from the frame alone — no snapshot adoption in between.
    channel.push({
      type: "authorization.granted",
      sequence: 42,
      occurred_at: "2026-08-22T12:00:05+10:00",
      payload: {
        cycle_id: "cycle-00000000000000000141",
        generation: 2,
        unit_ids: ["MID"],
        watts_by_unit: { MID: 1000 },
        directions_by_unit: { MID: "charge" },
      },
    });
    await waitFor(() => {
      expect(fact("Allowed").textContent ?? "").toMatch(/1,?000\s*W/);
    });

    // A later cycle clamps the same battery: the figure moves again (a
    // mount-time or once-only binding would freeze at the first grant).
    channel.push({
      type: "authorization.granted",
      sequence: 43,
      occurred_at: "2026-08-22T12:00:08+10:00",
      payload: {
        cycle_id: "cycle-00000000000000000142",
        generation: 2,
        unit_ids: ["MID"],
        watts_by_unit: { MID: 600 },
        directions_by_unit: { MID: "charge" },
      },
    });
    await waitFor(() => {
      expect(fact("Allowed").textContent ?? "").toMatch(/600\s*W/);
    });
    expect(fact("Allowed").textContent ?? "").not.toMatch(/1,?000/);
  });
});
