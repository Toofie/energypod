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

/** The one prop every view mounted by the shell receives. */
export interface ShellViewProps {
  client: ApiClient;
}

export type ShellView = ComponentType<ShellViewProps>;

export type ViewId = "home" | "batteries" | "now" | "activity";

export interface ViewDescriptor {
  id: ViewId;
  label: string;
}

/** The views built in this milestone, in navigation order. */
export const VIEWS: readonly ViewDescriptor[] = [
  { id: "home", label: "Home" },
  { id: "batteries", label: "Batteries" },
  { id: "now", label: "Now" },
  { id: "activity", label: "Activity" },
];

/**
 * Views contracted but not built in this milestone: honest not-yet entries,
 * never dead links (UI_CONTRACTS.md "Scope").
 */
export const PLANNED_VIEWS: readonly string[] = ["Energy flow", "Insights", "Schedule", "Plan history"];
