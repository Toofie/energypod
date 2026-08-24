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
   * The deterministic interaction the harness performs AFTER the settled
   * render and BEFORE the capture (the pod-parking round's addition): a
   * plain-data step list interpreted through the accessibility tree — clicks
   * by role+name, fills by label, select-option by label — so a dialog or a
   * post-action inline surface is on camera without the camera pretending to
   * be the operator's state. Absent = the settled static picture (every
   * earlier state).
   */
  readonly interact?: readonly ShotStep[];
  /**
   * The seeded guarded-route answers (PENDING wire family): the park and
   * resume 200 bodies a parking interaction plays against. Absent = the
   * seeded world refuses the routes loudly.
   */
  readonly park?: {
    readonly park?: () => Record<string, unknown>;
    readonly resume?: () => Record<string, unknown>;
  };
  /**
   * The seeded unit-detail read (GET /api/v1/units/{id}, keyed by unit id):
   * returning undefined refuses loudly (the honest error state the default
   * seeded client answers with).
   */
  readonly unitDetail?: (unitId: string) => Record<string, unknown> | undefined;
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

/**
 * One deterministic interaction step (plain data so it crosses the page →
 * orchestrator manifest boundary untouched): click a named control, fill a
 * labelled field, choose a labelled select option, or pause for a settled
 * re-render. Selectors are the accessibility tree's own — role+name and
 * label — never CSS.
 */
export type ShotStep =
  | {
      readonly click: {
        readonly role: "button" | "checkbox" | "tab";
        readonly name: string;
        /** Match the accessible name exactly (disambiguates "mid" from "Park mid"). */
        readonly exact?: boolean;
        /** Target inside the open dialog (its confirm, beside a same-named card control). */
        readonly inDialog?: boolean;
      };
    }
  | { readonly fill: { readonly label: string; readonly value: string } }
  | { readonly select: { readonly label: string; readonly value: string } }
  | { readonly pauseMs: number };
