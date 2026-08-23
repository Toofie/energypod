/**
 * The seeded stand-in client the screenshot harness mounts every view with
 * (dev tooling only): the REAL ApiClient surface, answered from one frozen
 * wire world — the view cannot tell it from a live controller, and the
 * pictures it renders are exactly what that wire would produce.
 *
 * Determinism by construction:
 * - `getSnapshot` / `refreshSnapshot` always answer a fresh clone of the same
 *   world (no clock, no counters — a second read is byte-identical).
 * - `openEvents` yields exactly one authoritative `snapshot` frame (the same
 *   world, through web/src/test/wire.ts `snapshotFrame`) and then never
 *   settles: the connection reads "live" and the seeded world never moves,
 *   so a screenshot taken even seconds later is the same picture.
 * - Surfaces the seeded world does not carry REJECT with a marked envelope —
 *   a view that reaches for one shows its honest error state on camera
 *   instead of a silent blank.
 *
 * `client.ready` resolves once the world has been read AND its first stream
 * frame delivered — the harness's render gate (main.tsx) waits on it before
 * flipping the page's readiness attribute for Playwright.
 */
import { ApiClientError } from "../../src/api/client";
import type { ApiClient, StreamEvent } from "../../src/api/client";
import { auditPage, health, snapshotFrame } from "../../src/test/wire";
import type { WireSnapshot } from "../../src/test/wire";

/** The harness client: the full ApiClient surface plus the readiness gate. */
export type SeededClient = ApiClient & { readonly ready: Promise<void> };

function absent(surface: string): ApiClientError {
  return new ApiClientError({
    code: "screenshot_harness_surface_absent",
    message: `The screenshot harness's seeded world does not carry ${surface}.`,
    details: null,
    request_id: "req-screenshot-harness",
    status: 0,
  });
}

/**
 * Build the client for one seeded world. Every method a view may call during
 * a static render either answers from the world or refuses loudly; nothing
 * ever contacts the network.
 *
 * `connection: "lost"` seeds the connection's fate instead of its liveness:
 * the first stream delivers its authoritative frame and then ENDS (the view
 * reads "lost", stops its march, and dims its last-known picture), and every
 * stream the view's own reconnect loop opens afterwards neither delivers nor
 * settles — the lost picture is the settled one, deterministically.
 */
export function seededClient(
  world: WireSnapshot,
  options: {
    connection?: "live" | "lost";
    history?: () => Record<string, unknown>;
    historyRefusal?: { status: number; body: Record<string, unknown> };
  } = {},
): SeededClient {
  const lost = options.connection === "lost";
  let snapshotRead = false;
  let streamFrameDelivered = false;
  /**
   * The History view reads history instead of the stream (the historian
   * publishes no bus event by design — polling IS the update path), so the
   * gate accepts EITHER consumption path: snapshot + first stream frame, or
   * snapshot + one settled history read (resolved OR refused — the honest
   * not-commissioned/error states are settled pictures too).
   */
  let historySettled = false;
  let streamOpens = 0;
  let resolveReady: () => void = () => {};
  const ready = new Promise<void>((resolve) => {
    resolveReady = resolve;
  });
  const maybeReady = (): void => {
    if (snapshotRead && (streamFrameDelivered || historySettled)) {
      resolveReady();
    }
  };

  const client = {
    ready,
    getSnapshot: async () => {
      snapshotRead = true;
      maybeReady();
      return structuredClone(world);
    },
    refreshSnapshot: async () => structuredClone(world),
    getHealth: async () => health(),
    getUnitDetail: async () => {
      throw absent("a unit detail");
    },
    getAudit: async () => auditPage([]),
    postIntent: async () => {
      throw absent("a dispatch");
    },
    postIntentCancel: async () => {
      throw absent("a cancel");
    },
    postArm: async () => {
      throw absent("an arm");
    },
    postDisarm: async () => {
      throw absent("a disarm");
    },
    postEmergencyStop: async () => {
      throw absent("a stop");
    },
    postStopAcknowledgement: async () => {
      throw absent("a stop acknowledgement");
    },
    postInhibitAcknowledgement: async () => {
      throw absent("an inhibit acknowledgement");
    },
    postExcessCharging: async () => {
      throw absent("the excess toggle");
    },
    postNightCharging: async () => {
      throw absent("the night toggle");
    },
    getSchedule: async () => {
      throw absent("the schedule plan");
    },
    putSchedule: async () => {
      throw absent("a schedule publish");
    },
    getEnergyDays: async () => {
      throw absent("the energy ledger");
    },
    getObservedObjectives: async () => {
      throw absent("the observed-objectives window");
    },
    getPlantHistory: (() => {
      const settle = (): void => {
        historySettled = true;
        maybeReady();
      };
      if (options.historyRefusal !== undefined) {
        const { status, body } = options.historyRefusal;
        return async () => {
          try {
            throw new ApiClientError({ status, ...body });
          } finally {
            settle();
          }
        };
      }
      if (options.history !== undefined) {
        return async () => {
          try {
            return structuredClone(options.history()) as Record<string, unknown>;
          } finally {
            settle();
          }
        };
      }
      return async () => {
        try {
          throw absent("the history window");
        } finally {
          settle();
        }
      };
    })(),
    openEvents: (): AsyncIterable<StreamEvent> => {
      const isInitialStream = streamOpens === 0;
      streamOpens += 1;
      async function* stream(): AsyncGenerator<StreamEvent, void, unknown> {
        if (lost && !isInitialStream) {
          // A reconnect attempt against a still-dead connection: it neither
          // delivers nor fails, so the view waits in its lost picture.
          await new Promise<never>(() => {});
        }
        // The authoritative first frame of every connection, then silence:
        // the seeded world is a held breath, not a simulation.
        yield snapshotFrame(structuredClone(world));
        streamFrameDelivered = true;
        maybeReady();
        if (lost) {
          // The seeded connection drops after its authoritative frame — the
          // one shot whose liveness cue is the loss itself.
          return;
        }
        await new Promise<never>(() => {});
      }
      return stream();
    },
  };
  return client as unknown as SeededClient;
}
