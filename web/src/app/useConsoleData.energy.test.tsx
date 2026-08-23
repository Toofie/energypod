/**
 * Behavior contract for the shell's energy-scorecard event consumption
 * (DESIGN_ENERGY_SCORECARD.md §6 + §8): the snapshot's `energy_today` block
 * is adopted by the shell's own model (absent = the feature detection, the
 * active_stops / adviser_state / schedule_state pattern), and the
 * `energy.day_rolled` TRANSITION re-reads the world — the completed day is
 * history, so the block on screen is stale by definition — with one polite
 * line naming the rolled day. PENDING-BACKEND fixtures come from
 * web/src/test/wire.ts.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient, Snapshot, StreamEvent } from "../api/client";
import {
  energyDayRolled,
  energyDayRecord,
  energyToday,
  snapshot as wireSnapshot,
  withEnergyToday,
} from "../test/wire";
import { SharedDataPlane } from "./SharedDataPlane";
import { useConsoleData } from "./useConsoleData";

const BASE = wireSnapshot([], {
  site_id: "site-1",
  snapshot_sequence: 41,
  captured_at: "2026-08-26T14:03:00+10:00",
});

const ENERGY_WORLD = withEnergyToday(BASE, energyToday());

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

/**
 * A client whose snapshot world can be swapped mid-test: the plane's forced
 * refresh reads through `getSnapshot` (SharedDataPlane `readThrough`), so the
 * mock serves whatever the "controller" currently holds — exactly the fresh
 * picture a real refetch would land after the rollover.
 */
function mockClient(
  snapshotData: unknown,
  frames: readonly StreamEvent[] = [],
): { client: ApiClient; setWorld(world: unknown): void } {
  let world: unknown = snapshotData;
  const client: ApiClient = {
    getSnapshot: vi.fn(() => Promise.resolve(world as Snapshot)),
    getHealth: vi.fn(() =>
      Promise.resolve({
        liveness: { ok: true },
        service_readiness: { ready: true, reasons: [] },
        control_readiness: { ready: true, reasons: [] },
      }),
    ),
    getAudit: vi.fn(() => Promise.resolve({ events: [], next_cursor: null })),
    getEnergyDays: vi.fn(() => Promise.resolve({ days: [] })),
    postIntent: vi.fn(() => Promise.reject(new Error("not used here"))),
    postArm: vi.fn(() => Promise.reject(new Error("not used here"))),
    postDisarm: vi.fn(() => Promise.reject(new Error("not used here"))),
    postEmergencyStop: vi.fn(() => Promise.reject(new Error("not used here"))),
    postStopAcknowledgement: vi.fn(() => Promise.reject(new Error("not used here"))),
    postInhibitAcknowledgement: vi.fn(() => Promise.reject(new Error("not used here"))),
    openEvents: vi.fn(streamOf(snapshotData, frames)),
  } as unknown as ApiClient;
  return { client, setWorld: (next) => { world = next; } };
}

function Probe({ client }: { client: ApiClient }): React.ReactElement {
  const [plane] = useState(() => new SharedDataPlane(client));
  const data = useConsoleData(plane, vi.fn(), {
    retryDelaysMs: [60_000],
    // The live-cadence poll is disabled: any snapshot read counted below is an
    // event-driven refetch, which is exactly what these pins govern.
    livePollMs: 600_000,
  });
  const today = data.snapshot?.energyToday ?? null;
  return (
    <div>
      <ul data-testid="polite" aria-label="Announcements">
        {data.polite.map((text, index) => (
          <li key={index}>{text}</li>
        ))}
      </ul>
      <output data-testid="today">
        {today === null
          ? "absent"
          : `${today.date}|${today.kind}|${today.fleet.gridImportKwh}`}
      </output>
    </div>
  );
}

function politeTexts(): string[] {
  return Array.from(screen.getByTestId("polite").querySelectorAll("li")).map(
    (line) => line.textContent ?? "",
  );
}

describe("useConsoleData — the energy scorecard events", () => {
  it("adopts the snapshot's energy_today; an absent block stays absent (the feature detection)", async () => {
    const { client } = mockClient(ENERGY_WORLD);
    render(<Probe client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("today").textContent).toBe("2026-08-26|in_progress|8.4");
    });
  });

  it("keeps the block absent when the snapshot carries none (today's wire)", async () => {
    const { client } = mockClient(BASE);
    render(<Probe client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("today").textContent).toBe("absent");
    });
  });

  it("re-reads the world on energy.day_rolled and announces the rolled day once", async () => {
    const harness = mockClient(ENERGY_WORLD, [
      energyDayRolled(4102, energyDayRecord({ date: "2026-08-26", kind: "complete" })),
    ]);
    render(<Probe client={harness.client} />);

    // The rollover lands: the controller's next read carries the NEW day's
    // block (sequence 42), and the shell's authority refetch adopts it.
    harness.setWorld(
      withEnergyToday(
        wireSnapshot([], {
          site_id: "site-1",
          snapshot_sequence: 42,
          captured_at: "2026-08-27T00:00:30+10:00",
        }),
        energyToday({ date: "2026-08-27" }),
      ),
    );
    await waitFor(() => {
      expect(screen.getByTestId("today").textContent).toBe("2026-08-27|in_progress|8.4");
    });
    expect(
      politeTexts().some((text) =>
        text.includes("2026-08-26's energy figures are in Insights"),
      ),
    ).toBe(true);
  });
});
