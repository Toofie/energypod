/**
 * The shell's view registry types. Views are INJECTED into the shell (see
 * AppShellProps.views / main.tsx): the shell itself stays hermetic — its
 * behavior suite renders it bare, and the product tree wires the real views
 * through the injection point. A view receives the session's shared client
 * (coalesced reads, fanned-out event stream); the client module is the only
 * shared boundary between the shell and the views.
 */
import type { ComponentType } from "react";
import type { ApiClient } from "../api/client";

/**
 * The one prop every view mounted by the shell receives: the session's shared
 * client (coalesced reads through the SharedDataPlane, fanned-out event
 * stream). A view never builds its own client and never opens its own socket.
 */
export interface ShellViewProps {
  client: ApiClient;
}

export type ShellView = ComponentType<ShellViewProps>;

/** The injection map the shell is handed (main.tsx assigns real views into it). */
export type ViewRegistry = Partial<Record<ViewId, ShellView>>;

export type ViewId =
  | "home"
  | "batteries"
  | "flow"
  | "now"
  | "schedule"
  | "activity"
  | "insights"
  | "objectives";

export interface ViewDescriptor {
  id: ViewId;
  label: string;
}

/** The views built in this milestone, in navigation order. */
export const VIEWS: readonly ViewDescriptor[] = [
  { id: "home", label: "Home" },
  { id: "batteries", label: "Batteries" },
  { id: "flow", label: "Flow" },
  { id: "now", label: "Now" },
  { id: "schedule", label: "Schedule" },
  { id: "activity", label: "Activity" },
  { id: "insights", label: "Insights" },
  { id: "objectives", label: "Objectives" },
];

/**
 * Views contracted but not built in this milestone: honest not-yet entries,
 * never dead links (UI_CONTRACTS.md "Scope").
 *
 * "Schedule" was promoted out of this list when its view landed. The pinned
 * decision (DESIGN_SCHEDULES.md §6 deviates deliberately): the nav link is
 * ALWAYS offered and the view itself answers a not-commissioned deployment
 * honestly — the excess tile's own pattern ("not commissioned in this
 * deployment's config") — instead of hiding behind a not-yet placeholder.
 *
 * "Insights" was promoted out of this list when the energy scorecard's ledger
 * landed (DESIGN_ENERGY_SCORECARD.md §8 W-B), under the same pinned decision:
 * the link is always offered and the view answers a not-commissioned
 * deployment honestly.
 *
 * "Objectives" was promoted out of this list when the night-writer detector's
 * evidence view landed (API_CONTRACTS.md "Night-writer detector"): its read
 * surface composes ALWAYS (no config block exists), so the link is always
 * offered and always has an answer once the detector's backend half lands.
 *
 * "Flow" (Energy flow) was promoted out of this list when the live flow view
 * landed: every datum it renders already lives in the snapshot's per-unit
 * telemetry (grid/load/battery watts, charge level), so the link is always
 * offered and the view always has an answer — its honest states ("not
 * available" per absent datum) are the view's own, never a placeholder.
 */
export const PLANNED_VIEWS: readonly string[] = ["Plan history"];
