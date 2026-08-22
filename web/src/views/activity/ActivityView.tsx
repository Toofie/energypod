// Minimal red-phase stub for the Activity view.
// No behavior: returns a placeholder. The suite in ActivityView.test.tsx
// defines the contract this component must satisfy.
import type { ApiClient } from "../../api/client";

export type ActivityConnection = "connected" | "disconnected";

export interface ActivityViewProps {
  client: ApiClient;
  connection?: ActivityConnection | undefined;
}

export function ActivityView({
  client,
  connection = "connected",
}: ActivityViewProps) {
  void client;
  void connection;
  return (
    <section aria-label="Activity view stub">ActivityView placeholder</section>
  );
}
