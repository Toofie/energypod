/**
 * The shell owns the single real data plane for a session: one snapshot read,
 * one health read, one event-stream connection. Views receive a client whose
 * reads are coalesced through this plane and whose event stream is a fan-out
 * subscription, so mounting a view never opens a second socket or races the
 * shell for frames (one WS ticket per connection — the client's own contract).
 *
 * The shell absorbs `resync_required` discontinuities itself (it refetches the
 * snapshot and reconnects with the recovery cursor) and simply republishes the
 * fresh authoritative snapshot to subscribers — and every forced refresh
 * (`refresh()`) republishes the same way REGARDLESS of the stream's health
 * (marked `stale` while the connection is down: fresh data, never proof of
 * liveness), so a re-read anywhere in the session updates every mounted view,
 * never just the cache — and a lost stream can never freeze the figures.
 *
 * Honesty rules the plane enforces:
 * - A cached snapshot is never handed over as a fresh read. It is servable
 *   only while the one real connection is live and only for a short window
 *   (see SNAPSHOT_CACHE_MS); otherwise the plane goes back to the wire, so a
 *   view mounting an hour into a session reads the world as it is now.
 * - A re-subscribing view can always tell a cached picture from a current
 *   one: snapshot frames carry the wire `captured_at` and a `stale` flag that
 *   is true whenever the picture is a replay delivered while the stream is
 *   down (see PlaneSnapshotFrame).
 */
import type { ApiClient, Health, Snapshot, StreamEvent } from "../api/client";

/**
 * How long a cached snapshot stays servable to a newly mounting view. The
 * service sends its authoritative snapshot frame exactly once per connection
 * (rest.py), so without a window the unlock-time picture would be replayed to
 * every later mount as if it were fresh.
 */
const SNAPSHOT_CACHE_MS = 15000;

/**
 * A snapshot frame as this plane hands it to subscribers. `data` is the
 * service's snapshot envelope, exactly as sent; `captured_at` mirrors its
 * capture stamp; `stale` is true only when the frame is a cached replay
 * queued while the real stream was down — a stale frame is still the last
 * known picture, but it is never evidence that the connection is live.
 */
export interface PlaneSnapshotFrame extends StreamEvent {
  type: "snapshot";
  data: unknown;
  captured_at: string;
  stale: boolean;
}

/** Views narrow a frame with this guard; a cached replay is data, not liveness. */
export function isPlaneSnapshot(frame: StreamEvent): frame is PlaneSnapshotFrame {
  return frame.type === "snapshot" && "stale" in frame;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrow a wire snapshot envelope (REST body or WS `data`) to the client type. */
function asSnapshot(value: unknown): Snapshot | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.site_id !== "string" || typeof value.snapshot_sequence !== "number") {
    return null;
  }
  if (!Array.isArray(value.units)) {
    return null;
  }
  return value as unknown as Snapshot;
}

interface Subscriber {
  queue: StreamEvent[];
  wake: (() => void) | null;
  ended: boolean;
}

/** The shell's handle on the one real event-stream connection. */
export interface RealStream {
  /** Frames from the real connection; ends promptly once `close()` runs. */
  frames: AsyncGenerator<StreamEvent, void, unknown>;
  /**
   * Effect-driven teardown: stop consuming immediately (never "when the next
   * frame happens to arrive") and return the underlying iterator so the
   * client's socket closes as soon as it is able to.
   */
  close(): void;
}

export class SharedDataPlane {
  private readonly real: ApiClient;
  private snapshotCache: Snapshot | null = null;
  private snapshotCachedAt = 0;
  private snapshotInFlight: Promise<Snapshot> | null = null;
  private healthInFlight: Promise<Health> | null = null;
  private readonly subscribers = new Set<Subscriber>();
  private replayFrame: PlaneSnapshotFrame | null = null;
  private live = false;

  constructor(real: ApiClient) {
    this.real = real;
  }

  /** Whether the one real event-stream connection is currently live. */
  isStreamLive(): boolean {
    return this.live;
  }

  /**
   * The one real connection delivered its authoritative snapshot frame: the
   * stream is live. Only the shell calls this — a REST read (however fresh)
   * is data, never liveness.
   */
  markStreamLive(): void {
    this.live = true;
  }

  /**
   * The snapshot read consumers of this session share. Concurrent callers
   * coalesce into one wire read; a cached picture is served only while it is
   * still current (live stream, inside the cache window), so a later mount
   * never receives the unlock-time picture as though it were fresh.
   */
  snapshot(): Promise<Snapshot> {
    if (this.snapshotInFlight !== null) {
      return this.snapshotInFlight;
    }
    if (this.snapshotCache !== null && this.cacheIsCurrent()) {
      return Promise.resolve(this.snapshotCache);
    }
    const read = this.readThrough();
    this.snapshotInFlight = read;
    return read;
  }

  /** A forced fresh read (a resync discontinuity, an authority change); updates the shared cache. */
  refresh(): Promise<Snapshot> {
    const read = this.readThrough();
    this.snapshotInFlight = read;
    return read;
  }

  private readThrough(): Promise<Snapshot> {
    const read = this.real.getSnapshot().then(
      (value) => {
        this.snapshotCache = value;
        this.snapshotCachedAt = Date.now();
        // A forced read is a fresh authoritative picture: it is republished to
        // every subscriber exactly like a wire snapshot frame — REGARDLESS of
        // the stream's health, because a refresh that only updated the cache
        // is what let every view freeze at its connect-time picture, and a
        // refresh that stops republishing when the stream is down is what
        // froze every figure on screen while the shell kept polling. The
        // frame is marked `stale` while the real connection is down: fresh
        // DATA, never evidence of liveness.
        this.publishSnapshot(value.snapshot_sequence, value, !this.live);
        if (this.snapshotInFlight === read) {
          this.snapshotInFlight = null;
        }
        return value;
      },
      (error: unknown) => {
        if (this.snapshotInFlight === read) {
          this.snapshotInFlight = null;
        }
        throw error;
      },
    );
    return read;
  }

  private cacheIsCurrent(): boolean {
    return this.live && Date.now() - this.snapshotCachedAt < SNAPSHOT_CACHE_MS;
  }

  /** Concurrent health reads coalesce; sequential ones always hit the wire. */
  health(): Promise<Health> {
    if (this.healthInFlight !== null) {
      return this.healthInFlight;
    }
    const read = this.real.getHealth().then(
      (value) => {
        this.healthInFlight = null;
        return value;
      },
      (error: unknown) => {
        this.healthInFlight = null;
        throw error;
      },
    );
    this.healthInFlight = read;
    return read;
  }

  /**
   * The one real event-stream connection, owned by the shell. The returned
   * handle is the only way to consume it: `close()` ends consumption without
   * waiting for the next frame to arrive.
   */
  openRealStream(afterSequence?: number): RealStream {
    const iterator = this.real.openEvents(afterSequence)[Symbol.asyncIterator]();
    let stopStream: () => void = () => undefined;
    const ended = new Promise<void>((resolve) => {
      stopStream = resolve;
    });
    const frames = (async function* planeStream(): AsyncGenerator<
      StreamEvent,
      void,
      unknown
    > {
      try {
        while (true) {
          // Race the next frame against close(): a parked read on a quiet bus
          // must never keep the shell's effect alive until a frame shows up.
          const next: IteratorResult<StreamEvent> | null = await Promise.race([
            iterator.next(),
            ended.then(() => null),
          ]);
          if (next === null || next.done === true) {
            return;
          }
          yield next.value;
        }
      } finally {
        // Ending consumption ends the connection: propagate the return so the
        // client generator's own teardown (which closes its socket) runs.
        void Promise.resolve(iterator.return?.(undefined)).catch(() => undefined);
      }
    })();
    return {
      frames,
      close: (): void => {
        stopStream();
        // Best-effort prompt socket close: the client's generator closes its
        // socket from its `finally`, which runs when the iterator is returned.
        void Promise.resolve(iterator.return?.(undefined)).catch(() => undefined);
      },
    };
  }

  publishSnapshot(sequence: number, data: unknown, stale = false): void {
    const capturedAt =
      isRecord(data) && typeof data.captured_at === "string" ? data.captured_at : "";
    this.replayFrame = { type: "snapshot", sequence, data, captured_at: capturedAt, stale };
    const cached = asSnapshot(data);
    if (cached !== null) {
      this.snapshotCache = cached;
      this.snapshotCachedAt = Date.now();
    }
    this.deliver(this.replayFrame);
  }

  publishEvent(frame: StreamEvent): void {
    this.deliver(frame);
  }

  /** The stream went away: mark the plane down and end every live subscription. */
  streamLost(): void {
    this.live = false;
    for (const subscriber of this.subscribers) {
      subscriber.ended = true;
      this.release(subscriber);
    }
  }

  /**
   * A fan-out subscription. The cached picture arrives first — marked `stale`
   * while the real stream is down, so a view can adopt the data without ever
   * mistaking it for proof the connection is live — then live frames.
   */
  subscribe(): AsyncGenerator<StreamEvent, void, unknown> {
    const subscriber: Subscriber = { queue: [], wake: null, ended: false };
    this.subscribers.add(subscriber);
    if (this.replayFrame !== null) {
      subscriber.queue.push({ ...this.replayFrame, stale: !this.live });
    }
    const plane = this;
    return (async function* feed(): AsyncGenerator<StreamEvent, void, unknown> {
      try {
        while (true) {
          while (subscriber.queue.length > 0) {
            const next = subscriber.queue.shift();
            if (next !== undefined) {
              yield next;
            }
          }
          if (subscriber.ended) {
            return;
          }
          await new Promise<void>((resolve) => {
            subscriber.wake = resolve;
          });
        }
      } finally {
        plane.subscribers.delete(subscriber);
      }
    })();
  }

  private deliver(frame: StreamEvent): void {
    for (const subscriber of this.subscribers) {
      subscriber.queue.push(frame);
      this.release(subscriber);
    }
  }

  private release(subscriber: Subscriber): void {
    const wake = subscriber.wake;
    subscriber.wake = null;
    wake?.();
  }
}

/** The client views receive inside the shell: shared reads, fanned-out stream. */
export function sharedClient(plane: SharedDataPlane, real: ApiClient): ApiClient {
  return {
    getSnapshot: () => plane.snapshot(),
    // Views may force a fresh wire read (bypassing the short cache) when a
    // frame proves the world moved: a stale cached picture must never answer.
    refreshSnapshot: () => plane.refresh(),
    getHealth: () => plane.health(),
    getUnitDetail: (unitId) => real.getUnitDetail(unitId),
    getAudit: (limit, afterSequence) => real.getAudit(limit, afterSequence),
    // The energy ledger read passes straight through: it is a plain observe
    // read with no coalescing value (the ledger is slow-moving and per-view).
    getEnergyDays: (limit) => real.getEnergyDays(limit),
    // The night-writer session view passes through the same way: a plain
    // observe read, per-view, no coalescing value.
    getObservedObjectives: (last) => real.getObservedObjectives(last),
    postIntent: (body, idempotencyKey) => real.postIntent(body, idempotencyKey),
    postIntentCancel: (intentId, idempotencyKey) =>
      real.postIntentCancel(intentId, idempotencyKey),
    postArm: (unitIds, idempotencyKey) => real.postArm(unitIds, idempotencyKey),
    postDisarm: (unitIds, idempotencyKey) => real.postDisarm(unitIds, idempotencyKey),
    postEmergencyStop: (unitIds, reason, idempotencyKey) =>
      real.postEmergencyStop(unitIds, reason, idempotencyKey),
    postStopAcknowledgement: (stopId, idempotencyKey) =>
      real.postStopAcknowledgement(stopId, idempotencyKey),
    postInhibitAcknowledgement: (unitId, idempotencyKey) =>
      real.postInhibitAcknowledgement(unitId, idempotencyKey),
    postExcessCharging: (action, options) => real.postExcessCharging(action, options),
    postNightCharging: (action, options) => real.postNightCharging(action, options),
    getSchedule: () => real.getSchedule(),
    putSchedule: (body, idempotencyKey) => real.putSchedule(body, idempotencyKey),
    openEvents: () => plane.subscribe(),
  };
}
