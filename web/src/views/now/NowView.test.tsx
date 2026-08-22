/**
 * Behavior contract for the "Now" control view (docs/UI_CONTRACTS.md, "Now (control)").
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
 * - resync_required frames carry {type, reason, snapshot_sequence?} with a
 *   real reason string (sequence_gap, backpressure, slow_subscriber, ...);
 * - emergency stop's `degraded` is a list of reason strings, never a flag.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import { NowView } from "./NowView";

const api = vi.hoisted(() => {
  const client = {
    getSnapshot: vi.fn(),
    getHealth: vi.fn(),
    getAudit: vi.fn(),
    postIntent: vi.fn(),
    postArm: vi.fn(),
    postDisarm: vi.fn(),
    postEmergencyStop: vi.fn(),
    postStopAcknowledgement: vi.fn(),
    postInhibitAcknowledgement: vi.fn(),
    openEvents: vi.fn(),
  };
  return { createApiClient: vi.fn(), client };
});

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
};

function snapshotEnvelope(units: UnitView[], sequence = 41): SnapshotEnvelope {
  return {
    site_id: "site-1",
    snapshot_sequence: sequence,
    captured_at: "2026-08-22T12:00:00+10:00",
    units,
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

function renderNow() {
  render(<NowView token="operator-session-token" />);
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
    // The view builds its client from the token it was handed.
    expect(api.createApiClient).toHaveBeenCalledWith("operator-session-token");

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

    const watts = within(dialog).getByLabelText(/watts/i);
    await user.clear(watts);
    await user.type(watts, "1500");
    const minutes = within(dialog).getByLabelText(/minutes|duration|ttl/i);
    await user.clear(minutes);
    await user.type(minutes, "5");
    const unit = within(dialog).getByRole("checkbox", { name: /MID/ });
    expect(unit).toBeChecked();

    // One composed preview sentence carries the direction, the typed watts,
    // the selected unit, the limit, and the expiry: static helper copy cannot
    // satisfy it because it contains the operator's own inputs.
    const preview = tightestText(dialog, /charge/i, /1,?500\s*W/, /MID/);
    expect(preview).toMatch(/limit/i);
    expect(preview).toMatch(/5\s*min|300\s*s/i);

    await user.click(within(dialog).getByRole("button", { name: /confirm/i }));

    expect(api.client.postIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        unit_ids: ["MID"],
        direction: "charge",
        watts: 1500,
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
      expect.objectContaining({ direction: "discharge", watts: 800, ttl_s: 300 }),
      // One idempotency key per operator action is part of the pinned call.
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
          errors: [
            { location: ["body", "watts"], message: "Input should be greater than 0", type: "greater_than" },
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
