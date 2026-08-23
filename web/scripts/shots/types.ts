/**
 * The screenshot harness's shared types (dev tooling only, never shipped).
 *
 * A STATE is one seeded wire world: a complete snapshot envelope built from
 * the shared wire fixtures (web/src/test/wire.ts), so every screenshot is
 * rendered from real wire shapes — the same factories the behavior suites
 * assert against, never a shape invented for the camera.
 */
import type { WireSnapshot } from "../../src/test/wire";

/** One named, deterministic state of one view. */
export interface ShotStateDefinition {
  /** The state's file-safe id — becomes `<id>.<viewport>.png`. */
  readonly id: string;
  /** One human sentence saying what the state exercises (for the manifest). */
  readonly caption: string;
  /** Builds the seeded snapshot world (fresh clone per call). */
  readonly world: () => WireSnapshot;
}
