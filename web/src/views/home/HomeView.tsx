import type { ApiClient } from "../../api/client";

export interface HomeViewProps {
  client: ApiClient;
}

/** Red-phase stub: replaced by the Home implementation in the green phase. */
export function HomeView(_props: HomeViewProps) {
  return <div>HomeView</div>;
}
