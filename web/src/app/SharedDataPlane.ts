/**
 * The shell owns the single real data plane for a session: one snapshot read,
 * one health read, one event-stream connection. Views receive a client whose
 * reads are coalesced through this plane and whose event stream is a fan-out
 * subscription, so mounting a view never opens a second socket or races the
 * shell for frames (one WS ticket per connection — the client's own contract).
 *
 * The shell absorbs `resync_required` discontinuities itself (it refetches the
 * snapshot and reconnects with the recovery cursor) and simply republishes the
 * fresh authoritative snapshot to subscribers.
 */
import type { ApiClient, Health, Snapshot, StreamEvent } from "../api/client";

export interface FeedFrame {
  type: string;
  sequence?: number;
  [field: string]: unknown;
}

interface Subscriber {
  queue: FeedFrame[];
  wake: (() => void) | null;
  ended: boolean;
}

export class SharedDataPlane {
  private readonly real: ApiClient;
  private snapshotCache: Snapshot | null = null;
  private snapshotInFlight: Promise<Snapshot> | null = null;
  private healthInFlight: Promise<Health> | null = null;
  private readonly subscribers = new Set<Subscriber>();
  private latestSnapshotFrame: FeedFrame | null = null;

  constructor(real: ApiClient) {
    this.real = real;
  }

  /** The snapshot read every consumer of this session shares until refreshed. */
  snapshot(): Promise<Snapshot> {
    if (this.snapshotInFlight !== null) {
      return this.snapshotInFlight;
    }
    if (this.snapshotCache !== null) {
      return Promise.resolve(this.snapshotCache);
    }
    const read = this.real.getSnapshot().then(
      (value) => {
        this.snapshotCache = value;
        this.snapshotInFlight = null;
        return value;
      },
      (error: unknown) => {
        this.snapshotInFlight = null;
        throw error;
      },
    );
    this.snapshotInFlight = read;
    return read;
  }

  /** A forced fresh read (a resync discontinuity); updates the shared cache. */
  refresh(): Promise<Snapshot> {
    const read = this.real.getSnapshot().then(
      (value) => {
        this.snapshotCache = value;
        this.snapshotInFlight = null;
        return value;
      },
      (error: unknown) => {
        this.snapshotInFlight = null;
        throw error;
      },
    );
    this.snapshotInFlight = read;
    return read;
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

  /** The one real event-stream connection, owned by the shell. */
  openRealStream(afterSequence?: number): AsyncIterable<StreamEvent> {
    return this.real.openEvents(afterSequence);
  }

  publishSnapshot(sequence: number, data: unknown): void {
    this.latestSnapshotFrame = { type: "snapshot", sequence, data };
    this.deliver(this.latestSnapshotFrame);
  }

  publishEvent(frame: FeedFrame): void {
    this.deliver(frame);
  }

  /** End every live subscription: the stream went away. Views decide to retry. */
  streamLost(): void {
    for (const subscriber of this.subscribers) {
      subscriber.ended = true;
      this.release(subscriber);
    }
  }

  /** A fan-out subscription: the latest snapshot first, then live frames. */
  subscribe(): AsyncGenerator<StreamEvent, void, unknown> {
    const subscriber: Subscriber = { queue: [], wake: null, ended: false };
    this.subscribers.add(subscriber);
    if (this.latestSnapshotFrame !== null) {
      subscriber.queue.push(this.latestSnapshotFrame);
    }
    const plane = this;
    return (async function* feed(): AsyncGenerator<StreamEvent, void, unknown> {
      try {
        while (true) {
          while (subscriber.queue.length > 0) {
            const next = subscriber.queue.shift();
            if (next !== undefined) {
              yield next as StreamEvent;
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

  private deliver(frame: FeedFrame): void {
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
    getHealth: () => plane.health(),
    getAudit: (limit, afterSequence) => real.getAudit(limit, afterSequence),
    postIntent: (body, idempotencyKey) => real.postIntent(body, idempotencyKey),
    postArm: (unitIds, idempotencyKey) => real.postArm(unitIds, idempotencyKey),
    postDisarm: (unitIds, idempotencyKey) => real.postDisarm(unitIds, idempotencyKey),
    postEmergencyStop: (unitIds, reason, idempotencyKey) =>
      real.postEmergencyStop(unitIds, reason, idempotencyKey),
    postStopAcknowledgement: (stopId, idempotencyKey) =>
      real.postStopAcknowledgement(stopId, idempotencyKey),
    postInhibitAcknowledgement: (unitId, idempotencyKey) =>
      real.postInhibitAcknowledgement(unitId, idempotencyKey),
    openEvents: () => plane.subscribe(),
  };
}
