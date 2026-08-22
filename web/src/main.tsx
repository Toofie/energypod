/**
 * Console entry point: the token gate lives in the AppShell; this root wires
 * the real views into the shell's injection point and mounts the tree.
 */
import { createRoot } from "react-dom/client";
import { AppShell } from "./app/AppShell";
import type { ShellView, ViewId } from "./app/views";
import { ActivityView } from "./views/activity/ActivityView";
import { BatteriesView } from "./views/batteries/BatteriesView";
import { HomeView } from "./views/home/HomeView";
import { NowView } from "./views/now/NowView";
import "./styles.css";

/**
 * Sibling views are adapted here to the shell's uniform view prop (each
 * receives the session's shared client); this is the only place the shell's
 * tree reaches outside its own directory.
 */
function adapt(component: unknown): ShellView {
  return component as ShellView;
}

const views: Partial<Record<ViewId, ShellView>> = {
  home: adapt(HomeView),
  batteries: adapt(BatteriesView),
  now: adapt(NowView),
  activity: adapt(ActivityView),
};

const container = document.getElementById("root");
if (container === null) {
  throw new Error("The console needs a #root element to mount into.");
}
createRoot(container).render(<AppShell views={views} />);
