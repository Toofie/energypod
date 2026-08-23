/**
 * The screenshot harness's registry (dev tooling): one entry per view the
 * design loop shoots, each carrying its own states file. Adding a view to
 * the loop = write `states/<view>.ts` beside flow.ts, add one entry here —
 * the orchestrator (scripts/screenshot.mjs) reads this list through the
 * page's manifest and needs no changes.
 */
import type { ShellView } from "../../src/app/views";
import { FlowView } from "../../src/views/flow/FlowView";
import { FLOW_STATES } from "./states/flow";
import type { ShotStateDefinition } from "./types";

/** One shootable view: the real component plus its state matrix. */
export interface ShotViewDefinition {
  /** The view's file-safe id — the output subdirectory under .design-shots/. */
  readonly id: string;
  /** The shell's own label for the view (the <h1> the real shell renders). */
  readonly label: string;
  /** The REAL view component, mounted exactly as the shell mounts it. */
  readonly Component: ShellView;
  readonly states: readonly ShotStateDefinition[];
}

/**
 * The capture matrix's viewport half: a desktop canvas and a phone. The
 * narrow width is the design's hardest constraint (390 px — a common phone
 * width), captured at 2x so the review sees phone-crisp text.
 */
export interface ShotViewport {
  readonly id: string;
  readonly width: number;
  readonly height: number;
  readonly deviceScaleFactor: number;
}

export const SHOT_VIEWPORTS: readonly ShotViewport[] = [
  { id: "desktop-1440", width: 1440, height: 1000, deviceScaleFactor: 1 },
  { id: "narrow-390", width: 390, height: 844, deviceScaleFactor: 2 },
];

export const SHOT_VIEWS: readonly ShotViewDefinition[] = [
  { id: "flow", label: "Flow", Component: FlowView, states: FLOW_STATES },
];
