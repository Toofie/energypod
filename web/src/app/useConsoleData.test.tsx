/**
 * Behavior contract for the shell's live-data hook: the reconnect budget, the
 * REST-only snapshot retry, and the surfaced stream error envelope.
 *
 * These are the composed-app rules the AppShell suite cannot pin on its own
 * (it exercises the first failure and the first retry, not the whole budget):
 * retries are bounded and widening — never a permanent hammer on a failing
 * endpoint — the stream's own error envelope surfaces in state, a snapshot
 * retry never tears down and rebuilds the event stream, and every reconnect
 * carries the last seen sequence as its cursor.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../api/client";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import { adviserState, excessAdviserStateChanged } from "../test/wire";
import { SharedDataPlane } from "./SharedDataPlane";
import { nextStreamRetryDelayMs, STREAM_RETRY_DELAYS_MS, useConsoleData } from "./useConsoleData";
import type { ConsoleData } from "./useConsoleData";

const SNAPSHOT: Snapshot = {
  site_id: "site-1",
  snapshot_sequence: 41,
  captured_at: "2026-08-22T10:00:00Z",
  units: [
    {
      unit_id: "MID",
      lifecycle: "armed_idle",
      telemetry_age_s: 2,
      quality: "good",
      requested_power: { direction: "IDLE", watts: 0 },
      authorized_power: null,
      measured_watts: 0,
    },
  ],
};

function streamError(): ApiClientError {
  return new ApiClientError({
    status: 1011,
    code: "event_stream_error",
    message: "The event stream failed",
    details: null,
    request_id: "req-stream-1",
  });
}

/** A connection that yields the connection-time snapshot frame, then ends
 * cleanly: the client ends iteration on an unexpected close. */
function liveThenEnd(): AsyncGenerator<StreamEvent, void, unknown> {
  return (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
    yield { type: "snapshot", sequence: SNAPSHOT.snapshot_sequence, data: SNAPSHOT };
  })();
}

/** A healthy connection that parks: nothing more ever arrives. */
function liveThenQuiet(): AsyncGenerator<StreamEvent, void, unknown> {
  return (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
    yield { type: "snapshot", sequence: SNAPSHOT.snapshot_sequence, data: SNAPSHOT };
    await new Promise(() => undefined);
  })();
}

/** A connection that dies straight away with the stream's error envelope. */
function failing(): AsyncGenerator<StreamEvent, void, unknown> {
  return (async function* dead(): AsyncGenerator<StreamEvent, void, unknown> {
    throw streamError();
  })();
}

function mockClient(overrides: Partial<ApiClient> = {}): ApiClient {
  const refused = new Error("not used by this test");
  return {
    getSnapshot: vi.fn(() => Promise.resolve(SNAPSHOT)),
    getHealth: vi.fn(() =>
      Promise.resolve({
        liveness: { ok: true },
        service_readiness: { ready: true, reasons: [] },
        control_readiness: { ready: true, reasons: [] },
      }),
    ),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    postIntent: vi.fn(() => Promise.reject(refused)),
    postArm: vi.fn(() => Promise.reject(refused)),
    postDisarm: vi.fn(() => Promise.reject(refused)),
    postEmergencyStop: vi.fn(() => Promise.reject(refused)),
    postStopAcknowledgement: vi.fn(() => Promise.reject(refused)),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(refused)),
    openEvents: vi.fn(() => liveThenQuiet()),
    ...overrides,
  } as unknown as ApiClient;
}

function Probe({
  client,
  onUnauthorized,
  retryDelaysMs,
  livePollMs,
}: {
  client: ApiClient;
  onUnauthorized: () => void;
  retryDelaysMs: readonly number[];
  /** A huge value disables the live-cadence poll for a test (the default). */
  livePollMs?: number;
}): React.ReactElement {
  // One plane per session, exactly as the shell builds it (a new plane per
  // render would be a new session per render).
  const [plane] = useState(() => new SharedDataPlane(client));
  const data = useConsoleData(plane, onUnauthorized, {
    retryDelaysMs,
    ...(livePollMs === undefined ? {} : { livePollMs }),
  });
  const adviser = data.snapshot?.adviserState ?? null;
  return (
    <div>
      <output data-testid="status">{data.streamStatus}</output>
      <output data-testid="error">
        {data.streamError === null ? "none" : `${data.streamError.code}: ${data.streamError.message}`}
      </output>
      <output data-testid="exhausted">{String(data.streamExhausted)}</output>
      <output data-testid="sequence">{data.snapshot?.sequence ?? -1}</output>
      {/* The shell's adviser-state slice, in one probe-readable line:
          "absent" is the feature detection; otherwise the participation and
          activity flags plus the watt figures the events refresh. */}
      <output data-testid="adviser">
        {adviser === null
          ? "absent"
          : `${adviser.enabled ? "on" : "off"}|${adviser.active ? "active" : "idle"}|${
              adviser.commandedChargeW
            }W|cap ${adviser.chargeCapW}W`}
      </output>
      <button type="button" onClick={data.retryStream}>
        retry stream
      </button>
      <button type="button" onClick={data.retrySnapshot}>
        retry snapshot
      </button>
    </div>
  );
}

describe("useConsoleData — the reconnect budget", () => {
  it("is bounded and widening: a finite list of increasing waits, never an endless hammer", () => {
    expect(STREAM_RETRY_DELAYS_MS.length).toBeGreaterThan(0);
    expect(STREAM_RETRY_DELAYS_MS.length).toBeLessThanOrEqual(6);
    for (let index = 1; index < STREAM_RETRY_DELAYS_MS.length; index += 1) {
      expect(STREAM_RETRY_DELAYS_MS[index]).toBeGreaterThan(STREAM_RETRY_DELAYS_MS[index - 1]!);
    }
    // The budget runs out: a caller that keeps failing is told to stop.
    const last = STREAM_RETRY_DELAYS_MS.length;
    expect(nextStreamRetryDelayMs(last)).toBeNull();
    expect(nextStreamRetryDelayMs(last + 500)).toBeNull();
    expect(nextStreamRetryDelayMs(0)).toBe(STREAM_RETRY_DELAYS_MS[0]);
  });

  it("stops reconnecting once the budget is spent, says so, and surfaces the error envelope", async () => {
    const user = userEvent.setup();
    let connections = 0;
    const cursors: (number | undefined)[] = [];
    const openEvents = vi.fn((afterSequence?: number) => {
      connections += 1;
      // Record the cursor each connection was opened with.
      cursors.push(afterSequence);
      // The first connection delivers its picture and then dies; every
      // reconnect from then on fails immediately with the stream envelope.
      return connections === 1 ? liveThenEnd() : failing();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    render(
      <Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5, 5, 5]} />,
    );

    // The healthy connection lands its picture first.
    await waitFor(() => {
      expect(screen.getByTestId("sequence").textContent).toBe(String(SNAPSHOT.snapshot_sequence));
    });

    // Every reconnect from now on fails immediately with the stream envelope.
    // With a three-attempt budget the hook must open exactly four connections
    // (the healthy one plus three retries) and then stop — no fifth, ever.
    await waitFor(
      () => {
        expect(screen.getByTestId("exhausted").textContent).toBe("true");
      },
      { timeout: 3000 },
    );
    expect(connections).toBe(4);
    expect(screen.getByTestId("status").textContent).toBe("down");
    expect(screen.getByTestId("error").textContent).toContain("event_stream_error");
    expect(screen.getByTestId("error").textContent).toContain("The event stream failed");

    // Nothing more happens while the operator leaves it alone.
    await new Promise((resolve) => {
      setTimeout(resolve, 40);
    });
    expect(connections).toBe(4);

    // The manual retry is the operator's, and it carries the last seen cursor.
    const cursorsBefore = cursors.length;
    await user.click(screen.getByRole("button", { name: "retry stream" }));
    await waitFor(() => {
      expect(cursors.length).toBeGreaterThan(cursorsBefore);
    });
    expect(cursors[cursors.length - 1]).toBe(SNAPSHOT.snapshot_sequence);
  });

  it("retries a lost stream automatically, carrying the last seen sequence as the cursor", async () => {
    let connections = 0;
    const cursors: (number | undefined)[] = [];
    const openEvents = vi.fn((afterSequence?: number) => {
      connections += 1;
      cursors.push(afterSequence);
      return connections <= 2 ? liveThenEnd() : failing();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5, 5, 5]} />);

    // The first connection is torn down as a loss; the automatic retry must
    // reconnect from the last seen sequence — never a replay from zero.
    await waitFor(
      () => {
        expect(connections).toBeGreaterThanOrEqual(2);
      },
      { timeout: 3000 },
    );
    expect(cursors[0]).toBeUndefined();
    expect(cursors[1]).toBe(SNAPSHOT.snapshot_sequence);
  });
});

describe("useConsoleData — the snapshot retry", () => {
  it("refreshes the picture over REST without tearing down and rebuilding the event stream", async () => {
    const user = userEvent.setup();
    const openEvents = vi.fn(() => liveThenQuiet());
    const getSnapshot = vi.fn(() => Promise.resolve(SNAPSHOT));
    const client = mockClient({
      openEvents,
      getSnapshot: getSnapshot as unknown as ApiClient["getSnapshot"],
    });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} />);

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("live");
    });
    const streamsOpened = openEvents.mock.calls.length;
    const reads = getSnapshot.mock.calls.length;

    await user.click(screen.getByRole("button", { name: "retry snapshot" }));

    // One more REST read, and the stream is never reopened for a REST action.
    await waitFor(() => {
      expect(getSnapshot.mock.calls.length).toBe(reads + 1);
    });
    expect(openEvents.mock.calls.length).toBe(streamsOpened);
    expect(screen.getByTestId("status").textContent).toBe("live");
  });
});

describe("useConsoleData — data returned to the shell", () => {
  it("adopts the connection-time snapshot and reports the live stream", async () => {
    const client = mockClient();
    const captured: { data?: ConsoleData } = {};
    function Capture(): React.ReactNode {
      const [plane] = useState(() => new SharedDataPlane(client));
      captured.data = useConsoleData(plane, vi.fn(), { retryDelaysMs: [5] });
      return null;
    }
    render(<Capture />);
    await waitFor(() => {
      expect(captured.data?.snapshot?.sequence).toBe(SNAPSHOT.snapshot_sequence);
    });
    expect(captured.data?.streamStatus).toBe("live");
    expect(captured.data?.streamError).toBeNull();
    expect(captured.data?.streamExhausted).toBe(false);
  });
});

// --- the excess-adviser event (feature-detected, §5 W-C) ----------------------
//
// `excess_adviser.state_changed` patches the shell's adviser-state slice from
// the payload, and the debounced authority refetch runs ONLY when
// `active`/`enabled` changed — the watt figures ride every publication
// (heartbeats included) and are their own refresh, so figure wander must never
// put a REST read per tick on the wire.

describe("useConsoleData — the excess-adviser event", () => {
  /** A controllable stream: yields the initial frames, then pushed frames. */
  function eventChannel(initial: StreamEvent[]): {
    open(): AsyncGenerator<StreamEvent, void, unknown>;
    push(frame: StreamEvent): void;
  } {
    const queue: StreamEvent[] = [...initial];
    let wake: (() => void) | null = null;
    const notify = (): void => {
      const release = wake;
      wake = null;
      release?.();
    };
    return {
      open: () =>
        (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
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

  it("keeps the adviser slice absent while the snapshot carries no adviser_state (feature detection)", async () => {
    const client = mockClient({ openEvents: vi.fn(() => liveThenQuiet()) });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} />);
    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("live");
    });
    expect(screen.getByTestId("adviser").textContent).toBe("absent");
  });

  it("patches the adviser slice from the payload and refetches authority when activity flips", async () => {
    const world = {
      ...SNAPSHOT,
      adviser_state: adviserState({
        enabled: true,
        active: false,
        hysteresis_state: "entering",
        commanded_charge_w: 0,
        fleet_export_w: 800,
        charge_cap_w: 500,
        held_intent_id: null,
        reason_codes: ["no_export_headroom"],
      }),
    } as unknown as Snapshot;
    const channel = eventChannel([
      { type: "snapshot", sequence: world.snapshot_sequence, data: world },
    ]);
    const getSnapshot = vi.fn(() => Promise.resolve(world));
    const client = mockClient({
      getSnapshot: getSnapshot as unknown as ApiClient["getSnapshot"],
      openEvents: vi.fn(() => channel.open()),
    });
    render(
      <Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} livePollMs={100_000} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("adviser").textContent).toBe("on|idle|0W|cap 500W");
    });
    const reads = getSnapshot.mock.calls.length;

    act(() => {
      channel.push(
        excessAdviserStateChanged(42, {
          active: true,
          hysteresis_state: "holding",
          target_unit_id: "MID",
          commanded_charge_w: 400,
          fleet_export_w: 1800,
          held_intent_id: "opt-3f9c21",
          reason_codes: ["export_headroom_available"],
        }) as unknown as StreamEvent,
      );
    });

    // The payload patches the slice: participation unchanged, now active, the
    // commanded figure refreshed, and the composed cap PRESERVED (the event
    // payload carries no charge_cap_w).
    await waitFor(() => {
      expect(screen.getByTestId("adviser").textContent).toBe("on|active|400W|cap 500W");
    });
    // The active flip changed authority: the debounced refetch re-read the
    // world through the plane.
    await waitFor(() => {
      expect(getSnapshot.mock.calls.length).toBeGreaterThan(reads);
    });
  });

  it("refreshes figures from a heartbeat without a refetch (figure wander never re-reads)", async () => {
    const world = {
      ...SNAPSHOT,
      adviser_state: adviserState({
        enabled: true,
        active: true,
        commanded_charge_w: 400,
        fleet_export_w: 1800,
        charge_cap_w: 500,
        reason_codes: ["export_headroom_available"],
      }),
    } as unknown as Snapshot;
    const channel = eventChannel([
      { type: "snapshot", sequence: world.snapshot_sequence, data: world },
    ]);
    const getSnapshot = vi.fn(() => Promise.resolve(world));
    const client = mockClient({
      getSnapshot: getSnapshot as unknown as ApiClient["getSnapshot"],
      openEvents: vi.fn(() => channel.open()),
    });
    render(
      <Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} livePollMs={100_000} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("adviser").textContent).toBe("on|active|400W|cap 500W");
    });
    const reads = getSnapshot.mock.calls.length;

    act(() => {
      channel.push(
        excessAdviserStateChanged(42, {
          heartbeat: true,
          active: true,
          commanded_charge_w: 500,
          fleet_export_w: 1900,
          reason_codes: ["export_headroom_available"],
        }) as unknown as StreamEvent,
      );
    });

    // The figures move from the payload alone…
    await waitFor(() => {
      expect(screen.getByTestId("adviser").textContent).toBe("on|active|500W|cap 500W");
    });
    // …and the state tuple did not change, so no authority refetch ran.
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
    expect(getSnapshot.mock.calls.length).toBe(reads);
  });
});
