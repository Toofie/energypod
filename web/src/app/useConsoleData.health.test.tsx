/**
 * Behavior contract for the shell's self-diagnosis surface: the one-glance
 * connection health (live / stale / reconnecting / offline), the staleness
 * bound behind it, background-tab recovery on visibilitychange/focus, and the
 * controller-restart notice a resume-reconnect surfaces (the 2026-08
 * incidents: a throttled background tab froze a stale age for an hour while
 * the backend was healthy, and a controller restart was indistinguishable
 * from a stall).
 *
 * These pin `useConsoleData`'s own contract through the same Probe harness as
 * useConsoleData.test.tsx (one plane per session, mocked client at its exact
 * surface); the AppShell suite pins the badge's rendering on top of it.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../api/client";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import { SharedDataPlane } from "./SharedDataPlane";
import {
  connectionHealth,
  CONTROLLER_RESTART_NOTICE,
  STALE_AFTER_MS,
  useConsoleData,
} from "./useConsoleData";

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
    throw new ApiClientError({
      status: 1011,
      code: "event_stream_error",
      message: "The event stream failed",
      details: null,
      request_id: "req-stream-1",
    });
  })();
}

/** A connection that dies at the transport: the service is unreachable. */
function unreachableStream(): AsyncGenerator<StreamEvent, void, unknown> {
  return (async function* unreachable(): AsyncGenerator<StreamEvent, void, unknown> {
    throw new ApiClientError({
      status: 0,
      code: "network_error",
      message: "The EnergyPod service could not be reached",
      details: null,
      request_id: "",
    });
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
  staleAfterMs,
  livePollMs,
}: {
  client: ApiClient;
  onUnauthorized: () => void;
  retryDelaysMs: readonly number[];
  staleAfterMs?: number;
  livePollMs?: number;
}): React.ReactElement {
  // One plane per session, exactly as the shell builds it (a new plane per
  // render would be a new session per render).
  const [plane] = useState(() => new SharedDataPlane(client));
  const data = useConsoleData(
    plane,
    onUnauthorized,
    staleAfterMs === undefined && livePollMs === undefined
      ? { retryDelaysMs }
      : {
          retryDelaysMs,
          ...(staleAfterMs === undefined ? {} : { staleAfterMs }),
          ...(livePollMs === undefined ? {} : { livePollMs }),
        },
  );
  return (
    <div>
      <output data-testid="status">{data.streamStatus}</output>
      <output data-testid="connection">{data.connection}</output>
      <output data-testid="since-update">
        {data.secondsSinceUpdate === null ? "none" : `${data.secondsSinceUpdate}`}
      </output>
      <output data-testid="restart-notice">{data.restartNotice ?? "none"}</output>
      <button type="button" onClick={data.retryStream}>
        retry stream
      </button>
    </div>
  );
}

describe("useConsoleData — connection health", () => {
  it("transitions live → reconnecting → live through a drop and an automatic recovery", async () => {
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      // The first connection delivers its picture and then dies; the retry
      // lands a healthy one that stays open.
      return connections === 1 ? liveThenEnd() : liveThenQuiet();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    // A first retry wait long enough that the reconnecting phase is actually
    // observable (a 5 ms backoff would restore live before any poll sees it).
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[800]} />);

    // The drop settles for the whole backoff window: the status fact goes down
    // and the health verdict says retrying — not "live" over frozen data, and
    // not offline (the service answered the health poll).
    await waitFor(
      () => {
        expect(screen.getByTestId("status").textContent).toBe("down");
      },
      { timeout: 2000 },
    );
    expect(screen.getByTestId("connection").textContent).toBe("reconnecting");

    // The automatic retry restores the live verdict.
    await waitFor(
      () => {
        expect(screen.getByTestId("connection").textContent).toBe("live");
      },
      { timeout: 4000 },
    );
    expect(connections).toBeGreaterThanOrEqual(2);
  });

  it("reports STALE with the seconds since the last update when a live connection goes quiet", async () => {
    // A tiny injected bound keeps the pin fast; production uses STALE_AFTER_MS.
    expect(STALE_AFTER_MS).toBe(10_000);
    const client = mockClient();
    render(
      <Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} staleAfterMs={20} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("connection").textContent).toBe("live");
    });

    // The stream stays "connected" but silent: the verdict must say stale,
    // with the age of the last update, never a calm "live" over frozen data.
    await waitFor(
      () => {
        expect(screen.getByTestId("connection").textContent).toBe("stale");
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("status").textContent).toBe("live");
    expect(Number(screen.getByTestId("since-update").textContent)).toBeGreaterThan(0);
  });

  it("reports OFFLINE once the reconnects fail at the network level", async () => {
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      if (connections === 1) {
        return liveThenEnd();
      }
      // Every retry dies at the transport: the service itself is unreachable.
      return unreachableStream();
    });
    const client = mockClient({
      openEvents: openEvents as unknown as ApiClient["openEvents"],
      getHealth: vi.fn(() =>
        Promise.reject(
          new ApiClientError({
            status: 0,
            code: "network_error",
            message: "The EnergyPod service could not be reached",
            details: null,
            request_id: "",
          }),
        ),
      ) as unknown as ApiClient["getHealth"],
    });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5, 5, 5]} />);

    // The brief healthy moment is over in milliseconds; the pinned behavior is
    // the settled verdict: every reconnect dies at the transport, so the
    // service is reported unreachable — offline, not merely reconnecting.
    await waitFor(
      () => {
        expect(screen.getByTestId("connection").textContent).toBe("offline");
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("status").textContent).toBe("down");
  });

  it("derives health from the stream facts alone (pure function pins)", () => {
    const base = {
      streamStatus: "live" as const,
      apiReachable: true,
      streamError: null,
      lastEventAtMs: 1_000,
    };
    expect(connectionHealth(base, 1_000 + STALE_AFTER_MS)).toBe("live");
    expect(connectionHealth(base, 1_000 + STALE_AFTER_MS + 1)).toBe("stale");
    expect(connectionHealth(base, 1_000 + STALE_AFTER_MS + 1, 60_000)).toBe("live");
    expect(
      connectionHealth({ ...base, streamStatus: "down" as const, lastEventAtMs: null }, 5_000),
    ).toBe("reconnecting");
    expect(
      connectionHealth({ ...base, streamStatus: "down" as const, apiReachable: false }, 5_000),
    ).toBe("offline");
    expect(
      connectionHealth(
        {
          ...base,
          streamStatus: "down" as const,
          streamError: { code: "network_error", message: "unreachable" },
        },
        5_000,
      ),
    ).toBe("reconnecting");
    // A network-level stream failure is offline evidence only while the
    // health probe has not just answered OK.
    expect(
      connectionHealth(
        {
          ...base,
          streamStatus: "down" as const,
          streamError: { code: "network_error", message: "unreachable" },
          apiReachable: null,
        },
        5_000,
      ),
    ).toBe("offline");
    expect(
      connectionHealth(
        {
          ...base,
          streamStatus: "down" as const,
          streamError: { code: "network_error", message: "unreachable" },
          apiReachable: false,
        },
        5_000,
      ),
    ).toBe("offline");
  });
});

describe("useConsoleData — background-tab recovery", () => {
  it("re-checks the stream immediately when the tab becomes visible while the connection is down", async () => {
    let connections = 0;
    const cursors: (number | undefined)[] = [];
    const openEvents = vi.fn((afterSequence?: number) => {
      connections += 1;
      cursors.push(afterSequence);
      // The healthy first connection, then failures that never deliver.
      return connections === 1 ? liveThenEnd() : failing();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[60_000]} />);

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("down");
    });
    // The backoff is a full minute: without the foreground trigger nothing
    // reconnects during this test.
    const connectionsWhenHidden = connections;

    // The operator comes back to the tab.
    act(() => {
      fireEvent(document, new Event("visibilitychange"));
    });

    // The re-check re-subscribes NOW — not when the minute-long backoff ends —
    // and still with the last seen sequence as the resume cursor.
    await waitFor(() => {
      expect(connections).toBeGreaterThan(connectionsWhenHidden);
    });
    expect(cursors[cursors.length - 1]).toBe(SNAPSHOT.snapshot_sequence);
  });

  it("re-subscribes a quiet-but-live stream on focus, and the resume surfaces the restart notice", async () => {
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      return liveThenQuiet();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[5]} staleAfterMs={20} />);
    await waitFor(
      () => {
        expect(screen.getByTestId("connection").textContent).toBe("stale");
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("restart-notice").textContent).toBe("none");

    // Focus re-checks too (some flows foreground without a visibility change).
    act(() => {
      fireEvent(window, new Event("focus"));
    });

    await waitFor(() => {
      expect(connections).toBeGreaterThanOrEqual(2);
    });
    // The resubscribed connection had to resume its cursor and delivered: the
    // calm controller-restart notice is the operator-visible trace of that.
    await waitFor(() => {
      expect(screen.getByTestId("restart-notice").textContent).toBe(CONTROLLER_RESTART_NOTICE);
    });
  });
});

describe("useConsoleData — the controller-restart notice", () => {
  it("surfaces the notice when a lost connection resumes, and clears it on the next loss", async () => {
    let connections = 0;
    const openEvents = vi.fn(() => {
      connections += 1;
      // Healthy → lost (the retry budget then pauses) → the foreground
      // re-check resumes and delivers → the manual restart after it fails at
      // the transport.
      if (connections === 1) {
        return liveThenEnd();
      }
      if (connections === 2) {
        return liveThenQuiet();
      }
      return unreachableStream();
    });
    const client = mockClient({ openEvents: openEvents as unknown as ApiClient["openEvents"] });
    // A minute-long backoff keeps the loss parked until the test foregrounds.
    render(<Probe client={client} onUnauthorized={vi.fn()} retryDelaysMs={[60_000]} />);

    // The first connection delivered and died: it never resumed anything, so
    // no notice exists while the connection sits down.
    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("down");
    });
    expect(screen.getByTestId("restart-notice").textContent).toBe("none");

    // The operator comes back to the tab; the re-check re-subscribes with the
    // cursor and the first delivered frame surfaces the notice — a restart is
    // no longer indistinguishable from a stall.
    act(() => {
      fireEvent(document, new Event("visibilitychange"));
    });
    await waitFor(
      () => {
        expect(screen.getByTestId("restart-notice").textContent).toBe(CONTROLLER_RESTART_NOTICE);
      },
      { timeout: 4000 },
    );
    expect(screen.getByTestId("connection").textContent).toBe("live");

    // A later loss supersedes the restore message.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "retry stream" }));
    await waitFor(
      () => {
        expect(screen.getByTestId("restart-notice").textContent).toBe("none");
      },
      { timeout: 4000 },
    );
  });
});

describe("useConsoleData — the interim live cadence", () => {
  it("re-reads and republishes the snapshot on a steady cadence while live, and an idle audit refusal adds no read", async () => {
    const worlds = [SNAPSHOT, { ...SNAPSHOT, snapshot_sequence: 42 }, { ...SNAPSHOT, snapshot_sequence: 43 }];
    let reads = 0;
    const client = mockClient({
      getSnapshot: vi.fn(() => {
        const world = worlds[Math.min(reads, worlds.length - 1)]!;
        reads += 1;
        return Promise.resolve(world);
      }) as unknown as ApiClient["getSnapshot"],
    });
    render(
      <Probe
        client={client}
        onUnauthorized={vi.fn()}
        retryDelaysMs={[5]}
        staleAfterMs={60_000}
        livePollMs={120}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("live");
    });

    // The cadence poll keeps re-reading while the connection is live: every
    // element on screen moves even between bus events (authority grants
    // publish nothing today). The idle-refusal filter (a rejected
    // control_decision must not add a read of its own) is pinned end-to-end
    // in the composed suite, which owns the channel plumbing for frames.
    await waitFor(
      () => {
        expect(reads).toBeGreaterThanOrEqual(3);
      },
      { timeout: 4000 },
    );
  });
});
