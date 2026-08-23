/**
 * The screenshot harness's registry (dev tooling): one entry per view the
 * design loop shoots, each carrying its own states file. Adding a view to
 * the loop = write `states/<view>.ts` beside flow.ts, add one entry here —
 * the orchestrator (scripts/screenshot.mjs) reads this list through the
 * page's manifest and needs no changes.
 */
import type { ShellView } from "../../src/app/views";
import { ActivityView } from "../../src/views/activity/ActivityView";
import { BatteriesView } from "../../src/views/batteries/BatteriesView";
import { FlowView } from "../../src/views/flow/FlowView";
import { HistoryView } from "../../src/views/history/HistoryView";
import { HomeView } from "../../src/views/home/HomeView";
import { InsightsView } from "../../src/views/insights/InsightsView";
import { NowView } from "../../src/views/now/NowView";
import { ObjectivesView } from "../../src/views/objectives/ObjectivesView";
import { ScheduleView } from "../../src/views/schedule/ScheduleView";
import { ACTIVITY_STATES } from "./states/activity";
import { BATTERIES_STATES } from "./states/batteries";
import { FLOW_STATES } from "./states/flow";
import { HISTORY_STATES } from "./states/history";
import { HOME_STATES } from "./states/home";
import { INSIGHTS_STATES } from "./states/insights";
import { NOW_STATES } from "./states/now";
import { OBJECTIVES_STATES } from "./states/objectives";
import { SCHEDULE_STATES } from "./states/schedule";
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
 * The capture matrix's viewport half: a desktop canvas, a tablet, and a phone.
 * The narrow width is the design's hardest constraint (390 px — a common phone
 * width) and the tablet (768 px) exercises the diagram's two-column rhythm
 * where the Whole-site lead takes the wider first track — both captured at 2x
 * so the review sees crisp text.
 */
export interface ShotViewport {
  readonly id: string;
  readonly width: number;
  readonly height: number;
  readonly deviceScaleFactor: number;
}

export const SHOT_VIEWPORTS: readonly ShotViewport[] = [
  { id: "desktop-1440", width: 1440, height: 1000, deviceScaleFactor: 1 },
  { id: "tablet-768", width: 768, height: 1030, deviceScaleFactor: 2 },
  { id: "narrow-390", width: 390, height: 844, deviceScaleFactor: 2 },
];

export const SHOT_VIEWS: readonly ShotViewDefinition[] = [
  { id: "home", label: "Home", Component: HomeView, states: HOME_STATES },
  { id: "batteries", label: "Batteries", Component: BatteriesView, states: BATTERIES_STATES },
  { id: "flow", label: "Flow", Component: FlowView, states: FLOW_STATES },
  { id: "history", label: "History", Component: HistoryView, states: HISTORY_STATES },
  { id: "now", label: "Now", Component: NowView, states: NOW_STATES },
  { id: "schedule", label: "Schedule", Component: ScheduleView, states: SCHEDULE_STATES },
  { id: "activity", label: "Activity", Component: ActivityView, states: ACTIVITY_STATES },
  { id: "insights", label: "Insights", Component: InsightsView, states: INSIGHTS_STATES },
  { id: "objectives", label: "Objectives", Component: ObjectivesView, states: OBJECTIVES_STATES },
];
