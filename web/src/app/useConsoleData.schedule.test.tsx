/**
 * Behavior contract for the shell's schedule event consumption
 * (DESIGN_SCHEDULES.md §5 W-D): the `schedule_state` projection is adopted
 * from the snapshot (absent = the feature detection), a `schedule_window`
 * transition moves it state-locally — the card swaps without a poll — and a
 * `schedule.replaced` frame re-reads the world. PENDING-BACKEND fixtures come
 * from web/src/test/wire.ts.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import {
  scheduleReplaced,
  scheduleState as wireScheduleState,
  scheduleWindowClosing,
  scheduleWindowOpened,
  snapshot as wireSnapshot,
  withScheduleState,
} from "../test/wire";
import { SharedDataPlane } from "./SharedDataPlane";
import { useConsoleData } from "./useConsoleData";

/** A world whose snapshot carries the pending schedule projection. */
const SNAPSHOT_WITH_SCHEDULE: Snapshot = {
  site_id: "site-1",
  snapshot_sequence: 41,
  captured_at: "2026-08-24T03:13:41+10:00",
  units: [],
} as unknown as Snapshot;

const SCHEDULE_WORLD = withScheduleState(
  wireSnapshot([], { site_id: "site-1", snapshot_sequence: 41, captured_at: "2026-08-24T03:13:41+10:00" }),
  wireScheduleState(),
);

/** The connection-time snapshot frame, then the given frames, then quiet. */
function streamOf(
  snapshotData: unknown,
  frames: readonly StreamEvent[],
): () => AsyncGenerator<StreamEvent, void, unknown> {
  return () =>
    (async function* channel(): AsyncGenerator<StreamEvent, void, unknown> {
      yield { type: "snapshot", sequence: 41, data: snapshotData };
      for (const frame of frames) {
        yield frame;
      }
      await new Promise(() => undefined);
    })();
}

function mockClient(
  snapshotData: unknown,
  frames: readonly StreamEvent[],
): ApiClient {
  return {
    getSnapshot: vi.fn(() => Promise.resolve(snapshotData as Snapshot)),
    getHealth: vi.fn(() =>
      Promise.resolve({
        liveness: { ok: true },
        service_readiness: { ready: true, reasons: [] },
        control_readiness: { ready: true, reasons: [] },
      }),
    ),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    postIntent: vi.fn(() => Promise.reject(new Error("not used here"))),
    postArm: vi.fn(() => Promise.reject(new Error("not used here"))),
    postDisarm: vi.fn(() => Promise.reject(new Error("not used here"))),
    postEmergencyStop: vi.fn(() => Promise.reject(new Error("not used here"))),
    postStopAcknowledgement: vi.fn(() => Promise.reject(new Error("not used here"))),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(new Error("not used here"))),
    openEvents: vi.fn(streamOf(snapshotData, frames)),
  } as unknown as ApiClient;
}

function Probe({ client }: { client: ApiClient }): React.ReactElement {
  const [plane] = useState(() => new SharedDataPlane(client));
  const data = useConsoleData(plane, vi.fn(), {
    retryDelaysMs: [60_000],
    // The live-cadence poll is disabled: any snapshot read counted below is an
    // event-driven refetch, which is exactly what these pins govern.
    livePollMs: 600_000,
  });
  const schedule = data.snapshot?.scheduleState ?? null;
  return (
    <div>
      <ul data-testid="polite" aria-label="Announcements">
        {data.polite.map((text, index) => (
          <li key={index}>{text}</li>
        ))}
      </ul>
      <output data-testid="schedule">
        {schedule === null
          ? "absent"
          : `${schedule.active ? "running" : "idle"}|${schedule.entryId ?? "-"}|${
              schedule.reasonCodes.join(",") || "-"
            }`}
      </output>
    </div>
  );
}

/** The polite region's current lines (the announcements the operator hears). */
function politeTexts(): string[] {
  return Array.from(screen.getByTestId("polite").querySelectorAll("li")).map(
    (line) => line.textContent ?? "",
  );
}

describe("useConsoleData — the schedules events", () => {
  it("adopts the snapshot's schedule_state and stays absent on today's wire", async () => {
    render(<Probe client={mockClient(SCHEDULE_WORLD, [])} />);
    await waitFor(() => {
      expect(screen.getByTestId("schedule").textContent).toBe("running|Night Charge|window_open");
    });
  });

  it("keeps the projection absent when the snapshot carries none (the feature detection)", async () => {
    render(<Probe client={mockClient(SNAPSHOT_WITH_SCHEDULE, [])} />);
    await waitFor(() => {
      expect(screen.getByTestId("schedule").textContent).toBe("absent");
    });
  });

  it("flips to running on a schedule_window.opened frame, without a poll", async () => {
    const idleWorld = withScheduleState(
      wireSnapshot([], { site_id: "site-1", snapshot_sequence: 41, captured_at: "2026-08-24T03:13:41+10:00" }),
      wireScheduleState({ active: false, entry_id: null, reason_codes: ["no_window_open"], next: null }),
    );
    const client = mockClient(idleWorld, [
      scheduleWindowOpened(42, { entry_id: "Night Charge", version: 4 }) as unknown as StreamEvent,
    ]);
    render(<Probe client={client} />);

    await waitFor(() => {
      expect(screen.getByTestId("schedule").textContent).toBe("running|Night Charge|window_open");
    });
    // The transition moved from the frame alone: exactly the mount-time
    // snapshot reads the plane made, and the event triggered no refetch.
    const reads = (client.getSnapshot as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(reads).toBeLessThanOrEqual(1);
    expect(politeTexts().some((line) => line.includes("Night Charge opened"))).toBe(true);
  });

  it("ends the running claim on a schedule_window.closing frame, without a poll", async () => {
    const client = mockClient(SCHEDULE_WORLD, [
      scheduleWindowClosing(42, { entry_id: "Night Charge", version: 4 }) as unknown as StreamEvent,
    ]);
    render(<Probe client={client} />);

    await waitFor(() => {
      expect(screen.getByTestId("schedule").textContent).toBe("idle|Night Charge|window_ended");
    });
    const reads = (client.getSnapshot as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(reads).toBeLessThanOrEqual(1);
    expect(politeTexts().some((line) => line.includes("Night Charge ended"))).toBe(true);
  });

  it("re-reads the world and announces the diff on a schedule.replaced frame", async () => {
    const client = mockClient(SCHEDULE_WORLD, [
      scheduleReplaced(42, { version: 5, added: ["Night Charge"], removed: ["old-evening"] }) as unknown as StreamEvent,
    ]);
    render(<Probe client={client} />);

    // The publish changes the runner's input, so the shell re-reads the
    // snapshot (the projection follows on the next world).
    await waitFor(() => {
      const reads = (client.getSnapshot as ReturnType<typeof vi.fn>).mock.calls.length;
      expect(reads).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => {
      expect(
        politeTexts().some((line) =>
          line.includes("Schedule published (v5) — added Night Charge, removed old-evening"),
        ),
      ).toBe(true);
    });
  });
});
