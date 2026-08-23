/**
 * The screenshot harness's shared types (dev tooling only, never shipped).
 *
 * A STATE is one seeded wire world: a complete snapshot envelope built from
 * the shared wire fixtures (web/src/test/wire.ts), so every screenshot is
 * rendered from real wire shapes — the same factories the behavior suites
 * assert against, never a shape invented for the camera.
 */
import type { WireAuditEvent } from "../../src/test/wire";
import type { WireSnapshot } from "../../src/test/wire";

/** One named, deterministic state of one view. */
export interface ShotStateDefinition {
  /** The state's file-safe id — becomes `<id>.<viewport>.png`. */
  readonly id: string;
  /** One human sentence saying what the state exercises (for the manifest). */
  readonly caption: string;
  /** Builds the seeded snapshot world (fresh clone per call). */
  readonly world: () => WireSnapshot;
  /**
   * The seeded connection's fate (default "live"): "lost" ends the stream
   * after its authoritative first frame, so the view settles into its
   * connection-lost picture — march stopped, last-known figures dimmed —
   * while its own reconnect loop waits on a stream that never answers.
   */
  readonly connection?: "live" | "lost";
  /**
   * The seeded history window (the plant-history route's 200 body, built from
   * the shared wire fixtures): present only for views that read history —
   * absent means the seeded world refuses the route loudly (the view's honest
   * error state on camera). Built fresh per call, like the world.
   */
  readonly history?: () => Record<string, unknown>;
  /**
   * The seeded history route's REFUSAL (status + the structured envelope),
   * for the not-commissioned shot: the view reads its honest 409 state.
   */
  readonly historyRefusal?: { status: number; body: Record<string, unknown> };
  /**
   * The seeded audit page (GET /api/v1/audit, built from the shared wire
   * fixtures): present only for views that read the audit trail — absent
   * means the seeded world answers an empty page (Activity's honest empty
   * state is a real picture too).
   */
  readonly audit?: () => readonly WireAuditEvent[];
  /**
   * The seeded GET /api/v1/schedule 200 body (wire.ts `getScheduleOk`).
   * Absent means the route refuses loudly — the Schedule view's honest
   * not-commissioned state on camera.
   */
  readonly schedule?: () => Record<string, unknown>;
  /**
   * The seeded GET /api/v1/energy/days 200 body (wire.ts
   * `getEnergyDaysOk`). Absent means the route refuses loudly.
   */
  readonly energyDays?: () => Record<string, unknown>;
  /**
   * The seeded GET /api/v1/objectives/observed 200 body (wire.ts
   * `getObservedObjectivesOk`). Absent means the route refuses loudly.
   */
  readonly objectives?: () => Record<string, unknown>;
}
