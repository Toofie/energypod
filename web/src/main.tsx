/**
 * Console entry point: the token gate lives in the AppShell; this root wires
 * the real views into the shell's injection point and mounts the tree.
 *
 * Every view is assigned directly against the shell's view-prop contract
 * (app/views.ts `ShellView`): each one receives the session's shared client,
 * and the compiler checks that here — an `unknown`-cast at this seam is what
 * let a view drift to a prop the shell never passes, killing the view in the
 * composed app while every isolated suite stayed green.
 */
import { createRoot } from "react-dom/client";
import { AppShell } from "./app/AppShell";
import type { ShellView, ViewId, ViewRegistry } from "./app/views";
import { ActivityView } from "./views/activity/ActivityView";
import { BatteriesView } from "./views/batteries/BatteriesView";
import { HomeView } from "./views/home/HomeView";
import { InsightsView } from "./views/insights/InsightsView";
import { NowView } from "./views/now/NowView";
import { ScheduleView } from "./views/schedule/ScheduleView";
import "./styles.css";

const views: ViewRegistry = {
  home: HomeView,
  batteries: BatteriesView,
  now: NowView,
  schedule: ScheduleView,
  activity: ActivityView,
  insights: InsightsView,
} satisfies Partial<Record<ViewId, ShellView>>;

const container = document.getElementById("root");
if (container === null) {
  throw new Error("The console needs a #root element to mount into.");
}
createRoot(container).render(<AppShell views={views} />);
