/**
 * Behavior contract for the SharedDataPlane: the one snapshot read, the one
 * health read, and the one event-stream connection every view in a session
 * shares. Pins the honesty rules the composed app needs and the isolated view
 * suites cannot see:
 *
 * - A cached snapshot is never handed over as a fresh read. It is servable
 *   only while the real stream is live and only inside a short window, so a
 *   view mounting later in the session reads the world as it is now.
 * - A re-subscribing view can always tell a cached picture from a current
 *   one: the replayed snapshot frame carries the wire `captured_at` and a
 *   `stale` flag that is true exactly while the stream is down.
 * - `openRealStream()` returns a handle whose `close()` ends consumption at
 *   once — a quiet bus must never keep the shell parked on a dead session.
 */
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import { SharedDataPlane, isPlaneSnapshot, sharedClient } from "./SharedDataPlane";

function wireSnapshot(sequence: number, capturedAt: string): Snapshot {
  return {
    site_id: "site-1",
    snapshot_sequence: sequence,
    captured_at: capturedAt,
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
}

function mockClient(overrides: Partial<ApiClient> = {}): ApiClient {
  const refused = new Error("not used by this test");
  return {
    getSnapshot: vi.fn(() => Promise.resolve(wireSnapshot(41, "2026-08-22T10:00:00Z"))),
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
    openEvents: vi.fn(() => (async function* idle(): AsyncGenerator<StreamEvent, void, unknown> {})()),
    ...overrides,
  } as unknown as ApiClient;
}

async function readAll(
  iterator: AsyncIterator<StreamEvent>,
  max = 8,
): Promise<StreamEvent[]> {
  const frames: StreamEvent[] = [];
  for (let index = 0; index < max; index += 1) {
    const next = await Promise.race([
      iterator.next(),
      new Promise<"parked">((resolve) => {
        setTimeout(() => resolve("parked"), 10);
      }),
    ]);
    if (next === "parked" || next.done === true) {
      break;
    }
    frames.push(next.value as StreamEvent);
  }
  return frames;
}

describe("SharedDataPlane — cached snapshots are never served as fresh", () => {
  it("serves the cached read to concurrent consumers but goes back to the wire once the window passes", async () => {
    vi.useFakeTimers();
    try {
      const first = wireSnapshot(41, "2026-08-22T10:00:00Z");
      const second = wireSnapshot(77, "2026-08-22T11:00:00Z");
      let world: Snapshot = first;
      let reads = 0;
      const client = mockClient({
        getSnapshot: vi.fn(() => {
          reads += 1;
          return Promise.resolve(world);
        }),
      });
      const plane = new SharedDataPlane(client);

      // A live stream with a published picture makes the cache servable, and
      // the published picture is itself the cache: no REST read happens.
      // (Liveness is the SHELL's declaration — it marks the stream live when
      // the real connection delivers its snapshot frame; publication alone is
      // data, never liveness.)
      plane.markStreamLive();
      plane.publishSnapshot(first.snapshot_sequence, first);
      expect(await plane.snapshot()).toBe(first);
      expect(await plane.snapshot()).toBe(first); // served from the current cache
      expect(reads).toBe(0);

      vi.advanceTimersByTime(60_000); // an hour passes in spirit: the cache expires
      world = second;
      await expect(plane.snapshot()).resolves.toBe(second);
      expect(reads).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not serve the cache while the stream is down: the plane re-reads instead of replaying the old world", async () => {
    const first = wireSnapshot(41, "2026-08-22T10:00:00Z");
    const second = wireSnapshot(45, "2026-08-22T10:01:00Z");
    let reads = 0;
    const client = mockClient({
      getSnapshot: vi.fn(() => {
        reads += 1;
        return Promise.resolve(reads === 1 ? first : second);
      }),
    });
    const plane = new SharedDataPlane(client);
    await plane.snapshot();
    plane.publishSnapshot(first.snapshot_sequence, first);
    plane.markStreamLive();
    plane.streamLost();

    await expect(plane.snapshot()).resolves.toBe(second);
    expect(reads).toBe(2);
  });
});

describe("SharedDataPlane — fan-out subscriptions can tell cached from current", () => {
  it("replays the cached picture to a new subscriber marked stale while the stream is down", async () => {
    const plane = new SharedDataPlane(mockClient());
    const picture = wireSnapshot(41, "2026-08-22T10:00:00Z");
    plane.publishSnapshot(picture.snapshot_sequence, picture);
    plane.streamLost();

    const frames = await readAll(plane.subscribe());
    expect(frames).toHaveLength(1);
    const frame = frames[0];
    if (frame === undefined || !isPlaneSnapshot(frame)) {
      throw new Error("the replayed picture must be a plane snapshot frame");
    }
    expect(frame.stale).toBe(true);
    expect(frame.captured_at).toBe("2026-08-22T10:00:00Z");
    expect(frame.sequence).toBe(41);
    expect(frame.data).toEqual(picture);
  });

  it("marks the replay current again once the real stream has republished a picture", async () => {
    const plane = new SharedDataPlane(mockClient());
    plane.publishSnapshot(41, wireSnapshot(41, "2026-08-22T10:00:00Z"));
    plane.streamLost();
    // The shell's sequence on a recovered connection: the wire snapshot frame
    // marks the stream live again, then the picture is published.
    plane.markStreamLive();
    plane.publishSnapshot(42, wireSnapshot(42, "2026-08-22T10:00:05Z"));

    const frames = await readAll(plane.subscribe());
    const frame = frames[0];
    if (frame === undefined || !isPlaneSnapshot(frame)) {
      throw new Error("the replayed picture must be a plane snapshot frame");
    }
    expect(frame.stale).toBe(false);
    expect(frame.sequence).toBe(42);
  });

  it("hands live frames to every subscriber and ends them when the stream is lost", async () => {
    const plane = new SharedDataPlane(mockClient());
    const view = sharedClient(plane, mockClient());
    const first = view.openEvents()[Symbol.asyncIterator]();
    const second = view.openEvents()[Symbol.asyncIterator]();

    const event: StreamEvent = { type: "unit.armed", sequence: 42, payload: {} };
    plane.publishEvent(event);
    expect(await readAll(first)).toEqual([event]);
    expect(await readAll(second)).toEqual([event]);

    plane.streamLost();
    expect((await first.next()).done).toBe(true);
    expect((await second.next()).done).toBe(true);
  });

  it("delivers nothing before the first picture: no invented snapshot exists", async () => {
    const plane = new SharedDataPlane(mockClient());
    const frames = await readAll(plane.subscribe());
    expect(frames).toEqual([]);
  });

  it("republishes a REST refresh even while the stream is down, marked stale (data, never liveness)", async () => {
    // The freeze defect this pins: a refresh that only updated the cache (or
    // only republished while the connection was live) left every figure on
    // screen frozen whenever the stream went down, while local timers kept
    // ticking. A forced read is data for the views in EVERY stream state:
    // a subscriber that re-subscribes after the loss (the views' reconnect
    // path) receives the refreshed picture immediately, marked stale.
    const first = wireSnapshot(41, "2026-08-22T10:00:00Z");
    const second = wireSnapshot(45, "2026-08-22T10:00:07Z");
    let reads = 0;
    const client = mockClient({
      getSnapshot: vi.fn(() => {
        reads += 1;
        return Promise.resolve(reads === 1 ? first : second);
      }),
    });
    const plane = new SharedDataPlane(client);
    await plane.snapshot();
    plane.publishSnapshot(first.snapshot_sequence, first);
    plane.markStreamLive();
    plane.streamLost();

    // The cadence poll re-reads with the stream down: the refresh must still
    // publish (the cache alone would freeze every view).
    await plane.refresh();

    const view = sharedClient(plane, mockClient());
    const delivered = await readAll(view.openEvents()[Symbol.asyncIterator]());
    expect(delivered).toHaveLength(1);
    const frame = delivered[0];
    if (frame === undefined || !isPlaneSnapshot(frame)) {
      throw new Error("the refreshed picture must arrive as a plane snapshot frame");
    }
    expect(frame.stale).toBe(true); // the stream is down: data, not liveness
    expect(frame.sequence).toBe(45);
    expect(frame.data).toEqual(second);
  });
});

describe("SharedDataPlane — the real stream handle", () => {
  it("close() ends consumption at once, with no frame arriving, and returns the iterator", async () => {
    let returnRequested = 0;
    const iterator = {
      next: () => new Promise<IteratorResult<StreamEvent>>(() => undefined), // parked forever: a quiet bus
      return: () => {
        returnRequested += 1;
        return Promise.resolve({ done: true as const, value: undefined });
      },
    };
    const client = mockClient({
      openEvents: vi.fn(
        () =>
          ({ [Symbol.asyncIterator]: () => iterator }) as unknown as AsyncIterable<StreamEvent>,
      ),
    });
    const plane = new SharedDataPlane(client);

    const handle = plane.openRealStream();
    const consumed: StreamEvent[] = [];
    const consumer = (async () => {
      for await (const frame of handle.frames) {
        consumed.push(frame);
      }
    })();
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    }); // the consumer is parked on the quiet bus
    handle.close();
    await consumer;

    expect(consumed).toEqual([]);
    expect(returnRequested).toBeGreaterThan(0);
  });

  it("relays frames from the real connection while it is open", async () => {
    const frame: StreamEvent = { type: "snapshot", sequence: 41, data: {} };
    const client = mockClient({
      openEvents: vi.fn(() =>
        (async function* one(): AsyncGenerator<StreamEvent, void, unknown> {
          yield frame;
          await new Promise(() => undefined);
        })(),
      ),
    });
    const plane = new SharedDataPlane(client);
    const handle = plane.openRealStream();
    const received = handle.frames[Symbol.asyncIterator]();
    expect(await received.next()).toEqual({ done: false, value: frame });
    handle.close();
  });
});
